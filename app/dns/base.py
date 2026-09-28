"""The DNS-provider contract.

A DNS provider (Cloudflare, Namecheap, Porkbun, DigitalOcean, Route 53, Google
Cloud DNS, or the keyless "Manual" fallback) is a DROP-IN: one file under
``app/dns/providers/<id>.py`` exposing a module-level ``PROVIDER`` (an instance
of a ``BaseDNSProvider`` subclass) plus a ``FEATURE`` header. The registry
(``app/dns/registry.py``) auto-discovers it — no central edit. This mirrors the
deploy-target contract (``app/deploy/base.py``) field-for-field so the two panels
share a mental model; only the VERBS differ (a deploy target creates/destroys a
server; a DNS provider reads/writes the records that point a name at one).

Each provider declares two field lists that drive the panel's connect form:

  * ``config_fields``     — NON-secret settings (an account username, a region).
                            Stored in ``data/config/dns.json``.
  * ``credential_fields`` — the API keys/tokens. Secrets land in the encrypted
                            vault (``app/dns/credentials.py``); a blank secret on
                            save keeps the stored value.

A field is ``{key, label, type, secret?, required?, placeholder?, options?,
default?, hint?, tip?, link?}`` where ``type`` is one of ``text`` / ``password``
/ ``textarea`` / ``select`` / ``checkbox`` / ``number`` — the SAME schema
deploy.js already renders, so the DNS panel reuses that field renderer.

The verbs, all admin-gated by the router:

  * ``test(config, creds)``                 — verify the key works. {ok, detail}.
  * ``list_zones(config, creds)``           — the domains this account controls.
                                              {ok, zones:[{id, name}], detail}.
  * ``list_records(config, creds, zone)``   — existing records in one zone (for
                                              display / conflict detection).
                                              {ok, records:[…], detail}.
  * ``upsert_record(config, creds, zone,    — create-or-update ONE record,
      record)``                               streaming progress events; the
                                              FINAL event is ``done({ok, …})``.
  * ``delete_record(config, creds, zone,    — remove one record, streaming.
      record)``

A ``zone`` is ``{"id", "name"}`` (the id is whatever the provider's API needs to
address it — a Cloudflare zone id, a Route 53 hosted-zone id, or just the domain
name for APIs that key on the name). A ``record`` the panel passes is
``{"name", "type", "value", "ttl", "proxied"}``:

  * ``name``    — the host, as a FQDN ("app.example.com") or "@" for the apex.
  * ``type``    — "A" (an IPv4 address) is all the point-a-server flow needs; the
                  contract allows others so a provider can list them.
  * ``value``   — the IPv4 address for an A record.
  * ``ttl``     — seconds (300 is a sensible low default so changes propagate).
  * ``proxied`` — Cloudflare-only: run the record through Cloudflare's proxy
                  (orange cloud) or leave it DNS-only (grey). Ignored elsewhere.

Progress events are ``{"phase", "message", "level"}`` (level ∈ info/ok/warn/err),
the SAME vocabulary the deploy + Commit ⭐ streams use, so the frontend reader is
shared in spirit. ``done`` carries at least ``{ok, message}``; the point-a-server
flow adds ``{host, ip, public_url, record}`` so the panel can show the result and
kick off the resolution check.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict, List, Tuple


def ev(message: str, *, phase: str = "step", level: str = "info") -> Dict[str, Any]:
    """Build one progress event (same shape as the deploy stream)."""
    return {"phase": phase, "message": message, "level": level}


def done(result: Dict[str, Any]) -> Dict[str, Any]:
    """Build the terminal event that carries the final result dict."""
    return {"phase": "done", "result": result}


class BaseDNSProvider:
    # ── identity / display (override on the subclass) ──
    id: str = ""
    display_name: str = ""
    icon: str = "globe"            # a lucide icon name
    summary: str = ""
    requires: List[str] = []       # human-readable prerequisites

    # A "manual" provider talks to NO API: it has no credentials, cannot list the
    # account's domains, and instead HANDS BACK the exact A-record the admin must
    # create at whatever registrar they use, then relies on the shared verify step
    # to confirm it took. The panel reads this to drop the connect/key section and
    # the "choose a domain" picker, showing an instruction card + a raw domain box
    # instead. It works for ANY provider, including ones with no public API.
    manual: bool = False

    config_fields: List[Dict[str, Any]] = []
    credential_fields: List[Dict[str, Any]] = []
    # Which credential keys must be present to count "connected". Defaults to every
    # secret field when None (mirrors the deploy contract's ``credential_required``).
    credential_required: List[str] | None = None

    # ── capability ──
    def available(self) -> Tuple[bool, str]:
        """(True, "") when this provider can run here, else (False, reason).

        For providers that need an optional Python library (Route 53 → boto3).
        The panel shows the reason and the other providers still work — the exact
        role the deploy contract's ``available`` hook plays. Providers that drive a
        plain REST API with httpx (already a dependency) just return True."""
        return True, ""

    # ── verbs (override) ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        """Validate credentials without changing anything. {ok, detail}."""
        return {"ok": False, "detail": "not implemented"}

    async def list_zones(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        """Every domain (zone) this account controls.

        Returns ``{ok, zones, detail}`` where each zone is at least
        ``{"id", "name"}``. ``name`` is the bare domain ("example.com"); ``id`` is
        whatever ``upsert_record`` needs to address it."""
        return {"ok": False, "zones": [], "detail": "This provider cannot list domains."}

    async def list_records(self, config: Dict[str, Any], creds: Dict[str, Any],
                           zone: Dict[str, Any]) -> Dict[str, Any]:
        """Existing records in one zone, so the panel can show what's there and
        warn before overwriting. ``{ok, records, detail}`` where each record is
        ``{id, name, type, value, ttl, proxied}``. Optional: default is empty."""
        return {"ok": True, "records": [], "detail": ""}

    async def upsert_record(self, config: Dict[str, Any], creds: Dict[str, Any],
                            zone: Dict[str, Any], record: Dict[str, Any]
                            ) -> AsyncIterator[Dict[str, Any]]:
        """Create the record if absent, else update it in place, streaming events.
        End with a ``done`` event carrying ``{ok, message}`` (plus the resolved
        ``record``). Same name+type is treated as the SAME record (upsert), so
        re-pointing a domain never piles up duplicates."""
        raise NotImplementedError
        yield  # pragma: no cover  (makes this an async generator)

    async def delete_record(self, config: Dict[str, Any], creds: Dict[str, Any],
                            zone: Dict[str, Any], record: Dict[str, Any]
                            ) -> AsyncIterator[Dict[str, Any]]:
        """Remove one record, streaming events. Default: not supported."""
        yield done({"ok": False, "message": "This provider has no record deletion."})

"""DNS provider: Cloudflare.

The reference provider. Cloudflare's API is a plain token-authenticated REST API,
so this drives it directly with ``httpx`` (already a dependency) — no extra
package. The admin pastes ONE API token scoped to edit DNS; everything else (list
domains, read + write A records) rides that token.

Cloudflare-specific wrinkle — the "proxied" flag (the orange cloud). A proxied
record hides the origin IP behind Cloudflare's edge and Cloudflare serves its own
certificate, which can COLLIDE with the origin server issuing its own via the
HTTP challenge. So the point-a-server flow defaults ``proxied`` OFF (grey cloud,
DNS-only): the name resolves straight to the server, and the server's own web
layer gets a normal certificate. The admin can still turn the proxy on later in
Cloudflare if they want the edge.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

import httpx

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import clean_zone, fqdn

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "cloudflare",
    "display_name": "Cloudflare",
    "category": "dns",
    "status": "beta",
    "summary": "Point a domain you manage on Cloudflare at your server.",
    "requires": ["A Cloudflare account with the domain added", "An API token that can edit DNS"],
}

_API = "https://api.cloudflare.com/client/v4"


def _headers(creds: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {(creds.get('api_token') or '').strip()}",
            "Content-Type": "application/json"}


def _err(r: httpx.Response) -> str:
    try:
        j = r.json()
        errs = j.get("errors") or []
        if errs:
            return "; ".join(str(e.get("message") or e) for e in errs)
    except Exception:
        pass
    return f"HTTP {r.status_code}"


class CloudflareProvider(BaseDNSProvider):
    id = "cloudflare"
    display_name = "Cloudflare"
    icon = "cloud"
    summary = ("Point a domain you already manage on Cloudflare at your server. "
               "Paste one DNS-edit API token; the app lists your domains and sets the record.")
    requires = [
        "A Cloudflare account with your domain added to it",
        "An API token with the Zone · DNS · Edit permission",
    ]

    credential_fields = [
        {"key": "api_token", "label": "Cloudflare API token", "type": "password", "secret": True,
         "required": True, "placeholder": "paste your API token",
         "link": {"url": "https://dash.cloudflare.com/profile/api-tokens",
                  "label": "Create an API token ↗"},
         "tip": "In the Cloudflare dashboard open My Profile → API Tokens → Create Token, use "
                "the 'Edit zone DNS' template, and include the domain you want. Paste the token "
                "here. Stored encrypted; it is never shown back to the browser."},
    ]

    # ── test ──
    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        if not (creds.get("api_token") or "").strip():
            return {"ok": False, "detail": "Paste your Cloudflare API token first."}
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{_API}/user/tokens/verify", headers=_headers(creds))
        except Exception as e:
            return {"ok": False, "detail": f"Could not reach Cloudflare: {e}"}
        if r.status_code == 200 and (r.json().get("result") or {}).get("status") == "active":
            return {"ok": True, "detail": "Token is valid and active."}
        return {"ok": False, "detail": _err(r) or "That token was rejected."}

    # ── list_zones ──
    async def list_zones(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        zones = []
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                page = 1
                while True:
                    r = await c.get(f"{_API}/zones", headers=_headers(creds),
                                    params={"per_page": 50, "page": page})
                    if r.status_code != 200:
                        return {"ok": False, "zones": [], "detail": _err(r)}
                    j = r.json()
                    for z in j.get("result") or []:
                        zones.append({"id": z.get("id"), "name": z.get("name")})
                    info = j.get("result_info") or {}
                    if page >= int(info.get("total_pages") or 1):
                        break
                    page += 1
        except Exception as e:
            return {"ok": False, "zones": [], "detail": str(e)}
        return {"ok": True, "zones": zones, "detail": ""}

    # ── list_records ──
    async def list_records(self, config, creds, zone) -> Dict[str, Any]:
        zid = zone.get("id")
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{_API}/zones/{zid}/dns_records", headers=_headers(creds),
                                params={"type": "A", "per_page": 100})
            if r.status_code != 200:
                return {"ok": False, "records": [], "detail": _err(r)}
            recs = [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type"),
                     "value": x.get("content"), "ttl": x.get("ttl"),
                     "proxied": bool(x.get("proxied"))}
                    for x in (r.json().get("result") or [])]
            return {"ok": True, "records": recs, "detail": ""}
        except Exception as e:
            return {"ok": False, "records": [], "detail": str(e)}

    # ── upsert_record ──
    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        zid = zone.get("id")
        zname = zone.get("name") or ""
        name = fqdn(record.get("name", "@"), zname)
        ip = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 300)
        proxied = bool(record.get("proxied", False))
        body = {"type": "A", "name": name, "content": ip, "ttl": ttl, "proxied": proxied}
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                # Find an existing A record with this exact name → update it, else create.
                yield ev(f"Looking for an existing record for {name}…", phase="check")
                r = await c.get(f"{_API}/zones/{zid}/dns_records", headers=_headers(creds),
                                params={"type": "A", "name": name})
                if r.status_code != 200:
                    yield done({"ok": False, "message": _err(r)})
                    return
                existing = r.json().get("result") or []
                if existing:
                    rid = existing[0]["id"]
                    yield ev(f"Updating the A record for {name} → {ip}…", phase="write")
                    w = await c.put(f"{_API}/zones/{zid}/dns_records/{rid}",
                                    headers=_headers(creds), json=body)
                else:
                    yield ev(f"Creating an A record for {name} → {ip}…", phase="write")
                    w = await c.post(f"{_API}/zones/{zid}/dns_records",
                                     headers=_headers(creds), json=body)
            if w.status_code not in (200, 201):
                yield done({"ok": False, "message": _err(w)})
                return
        except Exception as e:
            logger.exception("cloudflare upsert failed")
            yield done({"ok": False, "message": str(e)})
            return
        yield ev("Cloudflare accepted the change.", phase="done", level="ok")
        yield done({"ok": True, "message": f"{name} now points at {ip} on Cloudflare.",
                    "record": {"name": name, "type": "A", "value": ip, "ttl": ttl, "proxied": proxied}})


PROVIDER = CloudflareProvider()

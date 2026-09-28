"""DNS orchestration — the glue between the API and a provider.

Builds the Domains panel's catalog (every drop-in provider + its connect form +
whether its key is present + the last domain it pointed), lists the account's
domains + records on demand, and runs the streaming "point a domain at a server"
flow: load the saved config + vault credentials, drive the provider's
``upsert_record`` generator, then run the provider-independent resolution check
and stamp the result. Mirrors ``app/deploy/manager.py`` in shape.

It also offers SUGGESTED TARGETS — the servers this app already knows about,
gleaned from the deploy subsystem's last-deployment records and saved SSH servers
— so the panel can pre-fill "point at 203.0.113.10 (the server you just
deployed)" instead of making the admin retype an IP.
"""

from __future__ import annotations

import ipaddress
import logging
from typing import Any, AsyncIterator, Dict, List

from app.dns import credentials, store, verify
from app.dns.base import done, ev
from app.dns.providers._common import clean_host, clean_zone
from app.dns.registry import get_provider, list_providers

logger = logging.getLogger(__name__)


def _field_defaults(fields) -> Dict[str, Any]:
    return {f["key"]: f["default"] for f in fields if "default" in f}


def _provider_config(p) -> Dict[str, Any]:
    return {**_field_defaults(p.config_fields), **store.get_config(p.id)}


# ── Catalog ─────────────────────────────────────────────────────────────────

async def build_catalog() -> Dict[str, Any]:
    """Everything the panel needs to render, for every discovered provider."""
    providers = []
    for p in list_providers():
        ok_avail, reason = p.available()
        cred_view = await credentials.public_view(p.id, p.credential_fields, p.credential_required)
        providers.append({
            "id": p.id,
            "display_name": p.display_name,
            "icon": p.icon,
            "summary": p.summary,
            "requires": p.requires,
            "manual": bool(getattr(p, "manual", False)),
            "available": ok_avail,
            "unavailable_reason": reason,
            "config_fields": p.config_fields,
            "credential_fields": [
                {k: v for k, v in f.items() if k != "secret"} | {"secret": bool(f.get("secret"))}
                for f in p.credential_fields
            ],
            "config": _provider_config(p),
            "credentials_set": cred_view["secrets_set"],
            "configured": cred_view["configured"],
            "pointing": store.get_pointing(p.id),
        })
    return {
        "providers": providers,
        "active_provider": store.get_active_provider() or (providers[0]["id"] if providers else ""),
        "targets": _suggested_targets(),
    }


def _suggested_targets() -> List[Dict[str, str]]:
    """Servers this app already knows about, to pre-fill the 'point at' box: the
    last deployment each deploy target recorded, plus any saved SSH servers. Only
    entries with a usable IPv4 are offered. Best-effort — never raises."""
    out: List[Dict[str, str]] = []
    seen = set()

    def _add(label: str, ip: str, url: str = "") -> None:
        ip = (ip or "").strip()
        try:
            ipaddress.IPv4Address(ip)
        except Exception:
            return
        if ip in seen:
            return
        seen.add(ip)
        out.append({"label": label or ip, "ip": ip, "url": url or ""})

    try:
        from app.deploy import store as deploy_store
        from app.deploy.registry import list_providers as deploy_providers
        for dp in deploy_providers():
            rec = deploy_store.get_deployment(dp.id)
            if rec:
                _add(f"{dp.display_name}: {rec.get('server') or rec.get('ip') or ''}".strip(": "),
                     rec.get("ip", ""), rec.get("public_url", ""))
            # Saved SSH servers carry a host that may be a raw IP.
            for sid, srv in (deploy_store.list_servers(dp.id) or {}).items():
                host = (srv.get("host") or "").strip()
                _add(srv.get("label") or host, host)
    except Exception as e:  # noqa: BLE001
        logger.debug("suggested targets gather failed: %s", e)
    return out


# ── Connect (save/clear/test) ───────────────────────────────────────────────

async def save_connection(provider_id: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Save what the connect form collected: config-field keys → dns.json, secret
    keys → the vault (blank secret keeps the stored one)."""
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown provider."}
    values = values or {}
    config_keys = {f["key"] for f in p.config_fields}
    cred_keys = {f["key"] for f in p.credential_fields}

    cfg_updates = {k: v for k, v in values.items() if k in config_keys}
    if cfg_updates:
        store.save_config(provider_id, {**store.get_config(provider_id), **cfg_updates})

    cred_updates = {k: v for k, v in values.items() if k in cred_keys}
    if cred_updates:
        if not await credentials.save(provider_id, p.credential_fields, cred_updates):
            return {"ok": False, "detail": "Could not save the key."}

    view = await credentials.public_view(provider_id, p.credential_fields, p.credential_required)
    return {"ok": True, "configured": view["configured"], "credentials_set": view["secrets_set"]}


async def disconnect(provider_id: str) -> Dict[str, Any]:
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown provider."}
    await credentials.forget(provider_id)
    return {"ok": True, "detail": "Disconnected — the key was removed from the vault."}


async def test(provider_id: str) -> Dict[str, Any]:
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "detail": "Unknown provider."}
    ok_avail, reason = p.available()
    if not ok_avail:
        return {"ok": False, "detail": reason or "This provider is unavailable here."}
    config = _provider_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)
    try:
        return await p.test(config, creds)
    except Exception as e:
        logger.exception("dns test failed for %s", provider_id)
        return {"ok": False, "detail": str(e)}


# ── Domains + records (on demand) ───────────────────────────────────────────

async def list_zones(provider_id: str) -> Dict[str, Any]:
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "zones": [], "detail": "Unknown provider."}
    ok_avail, reason = p.available()
    if not ok_avail:
        return {"ok": False, "zones": [], "detail": reason}
    config = _provider_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)
    try:
        return await p.list_zones(config, creds)
    except Exception as e:
        logger.exception("dns list_zones failed for %s", provider_id)
        return {"ok": False, "zones": [], "detail": str(e)}


async def list_records(provider_id: str, zone: Dict[str, Any]) -> Dict[str, Any]:
    p = get_provider(provider_id)
    if not p:
        return {"ok": False, "records": [], "detail": "Unknown provider."}
    config = _provider_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)
    try:
        return await p.list_records(config, creds, zone or {})
    except Exception as e:
        logger.exception("dns list_records failed for %s", provider_id)
        return {"ok": False, "records": [], "detail": str(e)}


# ── Point a domain at a server (streaming) ──────────────────────────────────

async def _drive_upsert(p, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
    """Yield the provider's progress events live and swallow its terminal ``done``,
    stashing the result on the generator via the sentinel dict passed in
    ``record['_out']`` (so the caller reads the outcome without the done leaking
    into the merged stream)."""
    out = record.pop("_out")
    async for evt in p.upsert_record(config, creds, zone, record):
        if evt.get("phase") == "done":
            out.update(evt.get("result", {}) or {})
        else:
            yield evt


async def run_point(provider_id: str, params: Dict[str, Any]) -> AsyncIterator[Dict[str, Any]]:
    """Point a host at an IP: upsert the A record (+ optional www), verify, stamp.

    ``params``: {zone_id, zone_name, host, ip, ttl, proxied, also_www}.
    ``host`` may be the apex (the bare domain) or a subdomain; ``zone_name`` is the
    domain the record lives under. For the manual provider, zone_name defaults to
    the host's domain and the upsert just prints instructions."""
    p = get_provider(provider_id)
    if not p:
        yield done({"ok": False, "message": "Unknown provider."})
        return
    ok_avail, reason = p.available()
    if not ok_avail:
        yield done({"ok": False, "message": reason or "This provider is unavailable."})
        return

    ip = (params.get("ip") or "").strip()
    try:
        ipaddress.IPv4Address(ip)
    except Exception:
        yield done({"ok": False, "message": "Enter a valid IPv4 address for the server (e.g. 203.0.113.10)."})
        return

    zone_name = clean_zone(params.get("zone_name") or "")
    host = clean_host(params.get("host") or "") or zone_name
    if not host:
        yield done({"ok": False, "message": "Enter the domain or subdomain to point."})
        return
    if not zone_name:
        # Fall back to the host itself as the zone (manual provider / apex).
        zone_name = host
    ttl = int(params.get("ttl") or 300)
    proxied = bool(params.get("proxied", False))
    also_www = bool(params.get("also_www", False))

    zone = {"id": params.get("zone_id") or zone_name, "name": zone_name}
    config = _provider_config(p)
    creds = await credentials.read(provider_id, p.credential_fields)

    # The records to set: the host itself, plus optionally a www.<domain> alias when
    # pointing the apex. Each is an independent A record to the same IP.
    records = [{"name": host, "type": "A", "value": ip, "ttl": ttl, "proxied": proxied}]
    if also_www and host == zone_name:
        records.append({"name": f"www.{zone_name}", "type": "A", "value": ip, "ttl": ttl, "proxied": proxied})

    all_ok = True
    for rec in records:
        result: Dict[str, Any] = {"ok": False, "message": ""}
        rec["_out"] = result
        async for evt in _drive_upsert(p, config, creds, zone, rec):
            yield evt
        if not result.get("ok"):
            all_ok = False
            yield ev(result.get("message") or "The record could not be set.", phase="err", level="err")
            break
        yield ev(result.get("message") or "Record set.", phase="set", level="ok")

    if not all_ok:
        yield done({"ok": False, "host": host, "ip": ip, "message": "Could not point the domain — see above."})
        return

    public_url = f"https://{host}"
    manual = bool(getattr(p, "manual", False))

    # Verify resolution (skipped-in-spirit for manual: we still check, since the
    # admin may have just set it, but we don't fail if it hasn't propagated).
    yield ev(f"Checking whether {host} resolves to {ip}…", phase="verify")
    v = await verify.check(host, ip)
    yield ev(v.get("detail", ""), phase="verify",
             level=("ok" if v.get("matches") else "warn"))

    store.set_pointing(provider_id, {
        "provider": provider_id, "domain": zone_name, "host": host, "ip": ip,
        "public_url": public_url, "record": f"{host} A {ip}",
        "state": "verified" if v.get("matches") else ("manual" if manual else "pending"),
        "verified": bool(v.get("matches")),
    })
    store.set_active_provider(provider_id)

    if v.get("matches"):
        msg = (f"{host} points at {ip}. Once your server has that domain configured, HTTPS is "
               f"issued automatically — open {public_url}.")
    else:
        msg = (f"The record is set. {host} isn't resolving to {ip} just yet — DNS can take a few "
               f"minutes to a few hours. Use Verify to re-check; HTTPS issues automatically once it does.")
    yield done({"ok": True, "host": host, "ip": ip, "public_url": public_url,
                "verified": bool(v.get("matches")), "message": msg})


async def run_verify(provider_id: str, host: str, ip: str = "") -> Dict[str, Any]:
    """Re-check resolution for the last-pointed (or a given) host, and refresh the
    stored state so the panel's status line stays honest."""
    host = clean_host(host or "")
    if not host:
        rec = store.get_pointing(provider_id)
        host = rec.get("host", "")
        ip = ip or rec.get("ip", "")
    if not host:
        return {"ok": False, "detail": "Nothing to verify yet — point a domain first."}
    v = await verify.check(host, ip)
    rec = store.get_pointing(provider_id)
    if rec and rec.get("host") == host:
        rec["verified"] = bool(v.get("matches"))
        rec["state"] = "verified" if v.get("matches") else rec.get("state", "pending")
        store.set_pointing(provider_id, rec)
    return {"ok": True, **v}

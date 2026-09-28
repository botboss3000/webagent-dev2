"""DNS provider: DigitalOcean.

DigitalOcean's API is a plain Bearer-token REST API, driven directly with
``httpx``. The admin pastes one personal access token with read+write scope; the
domain must already be added under Networking → Domains in the DO control panel
(DigitalOcean manages DNS for a domain whose nameservers point at DO).

DigitalOcean expresses a record's host as the RELATIVE label below the domain,
with "@" meaning the apex — the ``_common.split_host`` split.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

import httpx

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import split_host

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "digitalocean",
    "display_name": "DigitalOcean",
    "category": "dns",
    "status": "beta",
    "summary": "Point a domain managed under DigitalOcean DNS at your server.",
    "requires": ["A DigitalOcean account with the domain added", "A read/write API token"],
}

_API = "https://api.digitalocean.com/v2"


def _headers(creds: Dict[str, Any]) -> Dict[str, str]:
    return {"Authorization": f"Bearer {(creds.get('api_token') or '').strip()}",
            "Content-Type": "application/json"}


def _err(r: httpx.Response) -> str:
    try:
        return (r.json().get("message") or f"HTTP {r.status_code}").strip()
    except Exception:
        return f"HTTP {r.status_code}"


class DigitalOceanProvider(BaseDNSProvider):
    id = "digitalocean"
    display_name = "DigitalOcean"
    icon = "cloud"
    summary = ("Point a domain whose DNS is managed by DigitalOcean at your server. "
               "Paste a read/write API token; the app lists your domains and sets the record.")
    requires = [
        "A DigitalOcean account with your domain added under Networking → Domains",
        "A personal access token with read and write scope",
    ]

    credential_fields = [
        {"key": "api_token", "label": "DigitalOcean API token", "type": "password", "secret": True,
         "required": True, "placeholder": "dop_v1_…",
         "link": {"url": "https://cloud.digitalocean.com/account/api/tokens",
                  "label": "Create an API token ↗"},
         "tip": "In the DigitalOcean control panel open API → Tokens → Generate New Token with "
                "read AND write scope, and paste it here. Your domain must already be added under "
                "Networking → Domains. Stored encrypted."},
    ]

    async def test(self, config, creds) -> Dict[str, Any]:
        if not (creds.get("api_token") or "").strip():
            return {"ok": False, "detail": "Paste your DigitalOcean API token first."}
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{_API}/account", headers=_headers(creds))
        except Exception as e:
            return {"ok": False, "detail": f"Could not reach DigitalOcean: {e}"}
        if r.status_code == 200:
            return {"ok": True, "detail": "Token is valid."}
        return {"ok": False, "detail": _err(r) or "That token was rejected."}

    async def list_zones(self, config, creds) -> Dict[str, Any]:
        zones = []
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                url = f"{_API}/domains?per_page=200"
                while url:
                    r = await c.get(url, headers=_headers(creds))
                    if r.status_code != 200:
                        return {"ok": False, "zones": [], "detail": _err(r)}
                    j = r.json()
                    for d in j.get("domains") or []:
                        zones.append({"id": d.get("name"), "name": d.get("name")})
                    url = ((j.get("links") or {}).get("pages") or {}).get("next") or ""
        except Exception as e:
            return {"ok": False, "zones": [], "detail": str(e)}
        return {"ok": True, "zones": zones, "detail": ""}

    async def list_records(self, config, creds, zone) -> Dict[str, Any]:
        name = zone.get("name")
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{_API}/domains/{name}/records",
                                headers=_headers(creds), params={"type": "A", "per_page": 200})
            if r.status_code != 200:
                return {"ok": False, "records": [], "detail": _err(r)}
            recs = [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type"),
                     "value": x.get("data"), "ttl": x.get("ttl"), "proxied": False}
                    for x in (r.json().get("domain_records") or [])]
            return {"ok": True, "records": recs, "detail": ""}
        except Exception as e:
            return {"ok": False, "records": [], "detail": str(e)}

    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        domain = zone.get("name") or ""
        label, is_apex = split_host(record.get("name", "@"), domain)
        ip = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 300)
        body = {"type": "A", "name": label, "data": ip, "ttl": ttl}
        shown = domain if is_apex else f"{label}.{domain}"
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                yield ev(f"Looking for an existing record for {shown}…", phase="check")
                r = await c.get(f"{_API}/domains/{domain}/records",
                                headers=_headers(creds), params={"type": "A", "name": shown, "per_page": 200})
                if r.status_code != 200:
                    yield done({"ok": False, "message": _err(r)})
                    return
                match = None
                for x in r.json().get("domain_records") or []:
                    if (x.get("name") or "") == label:
                        match = x
                        break
                if match:
                    yield ev(f"Updating the A record for {shown} → {ip}…", phase="write")
                    w = await c.put(f"{_API}/domains/{domain}/records/{match['id']}",
                                    headers=_headers(creds), json=body)
                else:
                    yield ev(f"Creating an A record for {shown} → {ip}…", phase="write")
                    w = await c.post(f"{_API}/domains/{domain}/records",
                                     headers=_headers(creds), json=body)
            if w.status_code not in (200, 201):
                yield done({"ok": False, "message": _err(w)})
                return
        except Exception as e:
            logger.exception("digitalocean upsert failed")
            yield done({"ok": False, "message": str(e)})
            return
        yield ev("DigitalOcean accepted the change.", phase="done", level="ok")
        yield done({"ok": True, "message": f"{shown} now points at {ip} on DigitalOcean.",
                    "record": {"name": shown, "type": "A", "value": ip, "ttl": ttl}})


PROVIDER = DigitalOceanProvider()

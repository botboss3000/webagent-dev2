"""DNS provider: Porkbun.

Porkbun's API authenticates by posting BOTH an API key and a secret key in the
JSON body of every request (there's no header auth), so this provider carries two
secret fields. Driven with ``httpx``. The domain must be registered at (or have
its nameservers pointed at) Porkbun for its DNS API to manage it. API access must
be switched on for the domain in the Porkbun panel.

Porkbun expresses a record's host as the RELATIVE label below the domain ("" /
absent = the apex) — the ``_common.split_host`` split, with "@" mapped to "".
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

import httpx

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import split_host

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "porkbun",
    "display_name": "Porkbun",
    "category": "dns",
    "status": "beta",
    "summary": "Point a domain registered at Porkbun at your server.",
    "requires": ["A Porkbun domain with API access enabled", "An API key + secret key pair"],
}

_API = "https://api.porkbun.com/api/json/v3"


def _auth(creds: Dict[str, Any], extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    body = {"apikey": (creds.get("api_key") or "").strip(),
            "secretapikey": (creds.get("secret_key") or "").strip()}
    if extra:
        body.update(extra)
    return body


def _err(r: httpx.Response) -> str:
    try:
        return (r.json().get("message") or f"HTTP {r.status_code}").strip()
    except Exception:
        return f"HTTP {r.status_code}"


class PorkbunProvider(BaseDNSProvider):
    id = "porkbun"
    display_name = "Porkbun"
    icon = "globe"
    summary = ("Point a domain registered at Porkbun at your server. Paste your API key and "
               "secret key; the app lists your domains and sets the record.")
    requires = [
        "A domain registered at Porkbun (or using Porkbun's nameservers)",
        "API access enabled for that domain in the Porkbun panel",
        "An API key and secret key pair",
    ]

    credential_fields = [
        {"key": "api_key", "label": "Porkbun API key", "type": "password", "secret": True,
         "required": True, "placeholder": "pk1_…",
         "link": {"url": "https://porkbun.com/account/api", "label": "Get API keys ↗"},
         "tip": "In Porkbun open Account → API Access, create a key pair, and paste the API key "
                "here. Then open the domain's DNS settings and switch API access ON for it. Stored encrypted."},
        {"key": "secret_key", "label": "Porkbun secret key", "type": "password", "secret": True,
         "required": True, "placeholder": "sk1_…",
         "tip": "The secret key shown next to the API key when you created the pair. Stored encrypted."},
    ]

    async def test(self, config, creds) -> Dict[str, Any]:
        if not ((creds.get("api_key") or "").strip() and (creds.get("secret_key") or "").strip()):
            return {"ok": False, "detail": "Paste both your Porkbun API key and secret key first."}
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(f"{_API}/ping", json=_auth(creds))
        except Exception as e:
            return {"ok": False, "detail": f"Could not reach Porkbun: {e}"}
        if r.status_code == 200 and (r.json().get("status") == "SUCCESS"):
            return {"ok": True, "detail": "Keys are valid."}
        return {"ok": False, "detail": _err(r) or "Those keys were rejected."}

    async def list_zones(self, config, creds) -> Dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                r = await c.post(f"{_API}/domain/listAll", json=_auth(creds))
            if r.status_code != 200 or r.json().get("status") != "SUCCESS":
                return {"ok": False, "zones": [], "detail": _err(r)}
            zones = [{"id": d.get("domain"), "name": d.get("domain")}
                     for d in (r.json().get("domains") or [])]
            return {"ok": True, "zones": zones, "detail": ""}
        except Exception as e:
            return {"ok": False, "zones": [], "detail": str(e)}

    async def list_records(self, config, creds, zone) -> Dict[str, Any]:
        domain = zone.get("name")
        try:
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.post(f"{_API}/dns/retrieve/{domain}", json=_auth(creds))
            if r.status_code != 200 or r.json().get("status") != "SUCCESS":
                return {"ok": False, "records": [], "detail": _err(r)}
            recs = [{"id": x.get("id"), "name": x.get("name"), "type": x.get("type"),
                     "value": x.get("content"), "ttl": x.get("ttl"), "proxied": False}
                    for x in (r.json().get("records") or []) if x.get("type") == "A"]
            return {"ok": True, "records": recs, "detail": ""}
        except Exception as e:
            return {"ok": False, "records": [], "detail": str(e)}

    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        domain = zone.get("name") or ""
        label, is_apex = split_host(record.get("name", "@"), domain)
        sub = "" if is_apex else label            # Porkbun: apex = empty subdomain
        ip = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 600)       # Porkbun's minimum TTL is 600
        shown = domain if is_apex else f"{label}.{domain}"
        try:
            async with httpx.AsyncClient(timeout=25.0) as c:
                # Porkbun edits by record id; find an existing A record for this host.
                yield ev(f"Looking for an existing record for {shown}…", phase="check")
                rr = await c.post(f"{_API}/dns/retrieve/{domain}", json=_auth(creds))
                if rr.status_code != 200 or rr.json().get("status") != "SUCCESS":
                    yield done({"ok": False, "message": _err(rr)})
                    return
                want = shown
                match = None
                for x in rr.json().get("records") or []:
                    if x.get("type") == "A" and (x.get("name") or "") == want:
                        match = x
                        break
                payload = _auth(creds, {"name": sub, "type": "A", "content": ip, "ttl": str(ttl)})
                if match:
                    yield ev(f"Updating the A record for {shown} → {ip}…", phase="write")
                    w = await c.post(f"{_API}/dns/edit/{domain}/{match['id']}", json=payload)
                else:
                    yield ev(f"Creating an A record for {shown} → {ip}…", phase="write")
                    w = await c.post(f"{_API}/dns/create/{domain}", json=payload)
            if w.status_code != 200 or w.json().get("status") != "SUCCESS":
                yield done({"ok": False, "message": _err(w)})
                return
        except Exception as e:
            logger.exception("porkbun upsert failed")
            yield done({"ok": False, "message": str(e)})
            return
        yield ev("Porkbun accepted the change.", phase="done", level="ok")
        yield done({"ok": True, "message": f"{shown} now points at {ip} on Porkbun.",
                    "record": {"name": shown, "type": "A", "value": ip, "ttl": ttl}})


PROVIDER = PorkbunProvider()

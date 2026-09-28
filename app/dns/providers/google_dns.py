"""DNS provider: Google Cloud DNS.

Same key style as the existing Google Compute VM deploy target: the admin pastes a
service-account JSON, and we exchange it for an OAuth access token via the RFC 7523
JWT-bearer flow. Rather than re-implement that signing, we REUSE the deploy
target's ``_access_token`` — one signing path for both, scoped to
``cloud-platform`` which covers Cloud DNS. The Cloud DNS REST API is then driven
directly with ``httpx`` (no extra dependency).

Google Cloud DNS models a domain as a "managed zone": a resource with a short
``name`` (the id the API addresses, e.g. "example-com") and a ``dnsName`` (the
real domain, "example.com."). We show the dnsName and act on the resource name.

Updating a record isn't a PUT — Cloud DNS takes a "change" transaction: you
DELETE the old record set and ADD the new one in a single call. So an upsert here
reads the current A record set for the name and sends deletions+additions
together.
"""

from __future__ import annotations

import logging
from typing import Any, AsyncIterator, Dict

import httpx

from app.dns.base import BaseDNSProvider, done, ev
# Reuse the deploy Google VM target's service-account → access-token exchange so
# there's ONE Google signing path in the codebase (scope: cloud-platform, which
# includes Cloud DNS). See app/deploy/providers/google_vm.py.
from app.deploy.providers.google_vm import _access_token as _google_access_token

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "google_dns",
    "display_name": "Google Cloud DNS",
    "category": "dns",
    "status": "beta",
    "summary": "Point a domain hosted in Google Cloud DNS at your server.",
    "requires": ["A Google Cloud project with a Cloud DNS managed zone",
                 "A service-account JSON with the DNS Administrator role"],
}

_DNS = "https://dns.googleapis.com/dns/v1/projects"


def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _err(r: httpx.Response) -> str:
    try:
        return ((r.json().get("error") or {}).get("message") or f"HTTP {r.status_code}").strip()
    except Exception:
        return f"HTTP {r.status_code}"


def _abs(name: str) -> str:
    """Cloud DNS record names are absolute (trailing dot)."""
    name = (name or "").strip()
    return name if name.endswith(".") else name + "."


class GoogleDNSProvider(BaseDNSProvider):
    id = "google_dns"
    display_name = "Google Cloud DNS"
    icon = "cloud"
    summary = ("Point a domain whose DNS is hosted in Google Cloud DNS at your server. Paste a "
               "service-account JSON with the DNS Administrator role; the app lists your managed zones.")
    requires = [
        "A Google Cloud project with a Cloud DNS managed zone for your domain",
        "A service-account JSON key with the DNS Administrator role",
    ]

    config_fields = [
        {"key": "project_id", "label": "Google Cloud project ID", "type": "text", "required": True,
         "placeholder": "my-project-123456",
         "link": {"url": "https://console.cloud.google.com/net-services/dns/zones",
                  "label": "Open Cloud DNS ↗"},
         "tip": "The project ID (not its display name) that holds your Cloud DNS managed zone. "
                "Find it in the Google Cloud console top-bar project picker."},
    ]
    credential_fields = [
        {"key": "service_account_json", "label": "Service-account JSON", "type": "textarea",
         "secret": True, "required": True,
         "placeholder": '{ "type": "service_account", "project_id": …, "private_key": … }',
         "tip": "Paste the whole downloaded service-account key file. Give the service account the "
                "DNS Administrator role on the project. Stored encrypted; never shown back."},
    ]

    async def test(self, config, creds) -> Dict[str, Any]:
        project = (config.get("project_id") or "").strip()
        if not project:
            return {"ok": False, "detail": "Enter your Google Cloud project ID first."}
        if not (creds.get("service_account_json") or "").strip():
            return {"ok": False, "detail": "Paste the service-account JSON first."}
        try:
            token = await _google_access_token(creds)
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{_DNS}/{project}/managedZones", headers=_headers(token),
                                params={"maxResults": 1})
        except Exception as e:
            return {"ok": False, "detail": f"Google rejected the request: {e}"}
        if r.status_code == 200:
            return {"ok": True, "detail": "Credentials are valid."}
        return {"ok": False, "detail": _err(r)}

    async def list_zones(self, config, creds) -> Dict[str, Any]:
        project = (config.get("project_id") or "").strip()
        if not project:
            return {"ok": False, "zones": [], "detail": "Enter your Google Cloud project ID first."}
        zones = []
        try:
            token = await _google_access_token(creds)
            async with httpx.AsyncClient(timeout=20.0) as c:
                page = None
                while True:
                    params = {"maxResults": 100}
                    if page:
                        params["pageToken"] = page
                    r = await c.get(f"{_DNS}/{project}/managedZones",
                                    headers=_headers(token), params=params)
                    if r.status_code != 200:
                        return {"ok": False, "zones": [], "detail": _err(r)}
                    j = r.json()
                    for z in j.get("managedZones") or []:
                        zones.append({"id": z.get("name"),
                                      "name": (z.get("dnsName") or "").rstrip(".")})
                    page = j.get("nextPageToken")
                    if not page:
                        break
        except Exception as e:
            return {"ok": False, "zones": [], "detail": str(e)}
        return {"ok": True, "zones": zones, "detail": ""}

    async def list_records(self, config, creds, zone) -> Dict[str, Any]:
        project = (config.get("project_id") or "").strip()
        zid = zone.get("id")
        try:
            token = await _google_access_token(creds)
            async with httpx.AsyncClient(timeout=20.0) as c:
                r = await c.get(f"{_DNS}/{project}/managedZones/{zid}/rrsets",
                                headers=_headers(token), params={"type": "A"})
            if r.status_code != 200:
                return {"ok": False, "records": [], "detail": _err(r)}
            recs = [{"id": x.get("name"), "name": (x.get("name") or "").rstrip("."),
                     "type": x.get("type"), "value": ", ".join(x.get("rrdatas") or []),
                     "ttl": x.get("ttl"), "proxied": False}
                    for x in (r.json().get("rrsets") or []) if x.get("type") == "A"]
            return {"ok": True, "records": recs, "detail": ""}
        except Exception as e:
            return {"ok": False, "records": [], "detail": str(e)}

    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        project = (config.get("project_id") or "").strip()
        zid = zone.get("id")
        zname = zone.get("name") or ""
        from app.dns.providers._common import fqdn
        name = _abs(fqdn(record.get("name", "@"), zname))
        ip = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 300)
        try:
            token = await _google_access_token(creds)
            async with httpx.AsyncClient(timeout=25.0) as c:
                # Find the current A rrset for this name so we can delete-and-add it.
                yield ev(f"Looking for an existing record for {name.rstrip('.')}…", phase="check")
                r = await c.get(f"{_DNS}/{project}/managedZones/{zid}/rrsets",
                                headers=_headers(token), params={"name": name, "type": "A"})
                deletions = []
                if r.status_code == 200:
                    for x in r.json().get("rrsets") or []:
                        if x.get("type") == "A" and x.get("name") == name:
                            deletions.append({"name": x["name"], "type": "A",
                                              "ttl": x.get("ttl", ttl), "rrdatas": x.get("rrdatas", [])})
                additions = [{"name": name, "type": "A", "ttl": ttl, "rrdatas": [ip]}]
                change = {"additions": additions, "deletions": deletions}
                yield ev(f"{'Updating' if deletions else 'Creating'} the A record for "
                         f"{name.rstrip('.')} → {ip}…", phase="write")
                w = await c.post(f"{_DNS}/{project}/managedZones/{zid}/changes",
                                 headers=_headers(token), json=change)
            if w.status_code not in (200, 201):
                yield done({"ok": False, "message": _err(w)})
                return
        except Exception as e:
            logger.exception("google_dns upsert failed")
            yield done({"ok": False, "message": str(e)})
            return
        shown = name.rstrip(".")
        yield ev("Cloud DNS accepted the change.", phase="done", level="ok")
        yield done({"ok": True, "message": f"{shown} now points at {ip} on Google Cloud DNS.",
                    "record": {"name": shown, "type": "A", "value": ip, "ttl": ttl}})


PROVIDER = GoogleDNSProvider()

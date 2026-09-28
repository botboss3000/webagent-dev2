"""DNS provider TEMPLATE — copy this file to add a new registrar/DNS host.

Adding a provider is a DROP-IN: copy this to ``app/dns/providers/<id>.py``, fill in
the pieces below, and the registry auto-discovers it — no edit anywhere else. The
Domains panel renders your connect form, lists your domains, and runs the point
flow entirely from what you declare here. (This file starts with "_" so the
registry skips it.)

Fill in:
  1. ``FEATURE`` header + the class identity (id/display_name/icon/summary/requires).
  2. ``config_fields`` (non-secret) and ``credential_fields`` (secret) — same field
     schema the deploy panel uses: {key,label,type,secret?,required?,placeholder?,
     options?,default?,tip?,link?}.
  3. The verbs: ``test`` (validate the key), ``list_zones`` (the account's domains),
     optional ``list_records``, and ``upsert_record`` (create-or-update ONE A
     record, streaming events, ending with ``done``).

Use ``httpx`` (already a dependency) for a REST API; if you need an optional SDK,
gate it in ``available()`` like the Route 53 provider gates boto3. See
``cloudflare.py`` for the cleanest worked example, ``namecheap.py`` for a read-
modify-write API, and ``google_dns.py`` for service-account auth.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import fqdn, split_host  # name helpers  # noqa: F401

FEATURE = {
    "id": "example",                 # unique id (matches the filename)
    "display_name": "Example DNS",
    "category": "dns",
    "status": "experimental",        # stable | beta | experimental
    "summary": "One-line summary shown on the provider chip.",
    "requires": ["What the admin needs before this can work"],
}


class ExampleDNSProvider(BaseDNSProvider):
    id = "example"
    display_name = "Example DNS"
    icon = "globe"
    summary = "Point a domain managed at Example at your server."
    requires = ["An Example account", "An API token"]

    config_fields = []               # non-secret settings (a username, a region)
    credential_fields = [
        {"key": "api_token", "label": "API token", "type": "password", "secret": True,
         "required": True, "placeholder": "paste your token"},
    ]

    async def test(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "detail": "not implemented"}

    async def list_zones(self, config: Dict[str, Any], creds: Dict[str, Any]) -> Dict[str, Any]:
        return {"ok": False, "zones": [], "detail": "not implemented"}

    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        name = fqdn(record.get("name", "@"), zone.get("name", ""))
        ip = (record.get("value") or "").strip()
        yield ev(f"Setting {name} → {ip}…", phase="write")
        yield done({"ok": False, "message": "not implemented"})


# Uncomment to activate once implemented:
# PROVIDER = ExampleDNSProvider()

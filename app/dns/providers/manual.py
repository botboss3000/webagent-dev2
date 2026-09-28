"""DNS provider: Manual (any registrar, no API).

The universal fallback. Some registrars have no API, a paid-only API, or one the
admin would rather not wire up. This provider talks to nothing: given the domain
and the server IP, it HANDS BACK the exact A record to create — the host, the
type, the value, a sensible TTL — as an instruction the admin follows in their own
registrar's dashboard. The shared verify step (``app/dns/verify.py``) then confirms
the change actually resolved, so even with no API the panel can tell the admin
"yes, it's pointing now / not yet".

Because it has no key and can't enumerate domains, ``manual = True`` tells the
panel to drop the connect + domain-picker UI and show an instruction card driven
straight off the domain + IP the admin types.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Dict

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import clean_host, split_host

FEATURE = {
    "id": "manual",
    "display_name": "Other / manual",
    "category": "dns",
    "status": "stable",
    "summary": "No API — get the exact A record to set yourself, then verify it.",
    "requires": ["Access to your domain's DNS settings at whatever registrar you use"],
}


class ManualDNSProvider(BaseDNSProvider):
    id = "manual"
    display_name = "Other / manual"
    icon = "list-checks"
    manual = True
    summary = ("For any registrar without an API, or when you'd rather set the record yourself. "
               "The app tells you the exact A record to create, then verifies it has taken effect.")
    requires = ["Access to your domain's DNS settings at whatever registrar you use"]

    # No credentials and no connect config — the panel drives it off the domain +
    # IP the admin types into the point form.
    config_fields = []
    credential_fields = []
    credential_required: list = []

    async def test(self, config, creds) -> Dict[str, Any]:
        return {"ok": True, "detail": "No connection needed — enter your domain and set the record yourself."}

    async def list_zones(self, config, creds) -> Dict[str, Any]:
        # Nothing to enumerate — the admin types the domain directly.
        return {"ok": True, "zones": [], "detail": "manual"}

    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        # We can't write anything — instead spell out precisely what to create, so
        # the shared verify step can then confirm it. host is the full name; the
        # "host to enter" is the label most dashboards ask for ("@" for the apex).
        domain = (zone.get("name") or "").strip() or clean_host(record.get("name", ""))
        host_full = clean_host(record.get("name", "")) or domain
        label, is_apex = split_host(host_full, domain)
        ip = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 300)
        host_field = "@" if is_apex else label

        yield ev("Create this record in your registrar's DNS settings:", phase="instructions", level="ok")
        yield ev(f"Type: A", phase="rec")
        yield ev(f"Host / Name: {host_field}", phase="rec")
        yield ev(f"Value / Points to: {ip}", phase="rec")
        yield ev(f"TTL: {ttl} (or the lowest your registrar allows)", phase="rec")
        yield ev("Save it there, then use Verify below to check it's live.", phase="next", level="warn")
        yield done({
            "ok": True,
            "state": "manual",
            "message": f"Set an A record for {host_full} → {ip} at your registrar, then verify.",
            "record": {"name": host_full, "type": "A", "value": ip, "ttl": ttl},
        })


PROVIDER = ManualDNSProvider()

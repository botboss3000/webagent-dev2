"""DNS provider: AWS Route 53.

Route 53 signs every request with AWS SigV4, which is fiddly to hand-roll, so this
provider uses ``boto3`` — the official SDK — when it's installed, and reports
clearly via ``available()`` when it isn't (the same optional-dependency pattern the
SSH deploy target uses for paramiko). boto3's client is synchronous, so every call
runs in a worker thread (``asyncio.to_thread``) to keep the event loop free.

Auth is an access-key id + secret access key for an IAM identity allowed to
``route53:ListHostedZones`` / ``ListResourceRecordSets`` / ``ChangeResourceRecordSets``.
Route 53 is a global service, so there's no region to pick.

Route 53 addresses a zone by its hosted-zone id and wants the record's FULL name.
An UPSERT change handles create-or-update in one call, so no separate lookup is
needed for the write.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, AsyncIterator, Dict, Tuple

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import fqdn

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "route53",
    "display_name": "AWS Route 53",
    "category": "dns",
    "status": "beta",
    "summary": "Point a domain hosted in AWS Route 53 at your server.",
    "requires": ["An AWS Route 53 hosted zone for the domain",
                 "An access key allowed to change record sets", "The boto3 package"],
}


def _import_boto3():
    try:
        import boto3  # noqa: F401
        return boto3
    except Exception:
        return None


class Route53Provider(BaseDNSProvider):
    id = "route53"
    display_name = "AWS Route 53"
    icon = "cloud"
    summary = ("Point a domain hosted in AWS Route 53 at your server. Provide an access key "
               "allowed to change record sets; the app lists your hosted zones and sets the record.")
    requires = [
        "A Route 53 hosted zone for your domain",
        "An IAM access key id + secret allowed to list and change record sets",
    ]

    config_fields = []
    credential_fields = [
        {"key": "aws_access_key_id", "label": "AWS access key ID", "type": "text", "secret": True,
         "required": True, "placeholder": "AKIA…",
         "tip": "The access key id of an IAM user or role permitted to manage Route 53 record "
                "sets. Stored encrypted."},
        {"key": "aws_secret_access_key", "label": "AWS secret access key", "type": "password",
         "secret": True, "required": True, "placeholder": "your secret access key",
         "link": {"url": "https://console.aws.amazon.com/iam/home#/security_credentials",
                  "label": "AWS security credentials ↗"},
         "tip": "The secret access key that pairs with the id above. Give its IAM identity the "
                "Route 53 change-record-sets permission. Stored encrypted."},
    ]

    def available(self) -> Tuple[bool, str]:
        if _import_boto3() is None:
            return (False, "Managing Route 53 needs the 'boto3' package on the server. Add it "
                           "(pip install boto3) and restart WebAgent, or use another provider.")
        return True, ""

    def _client(self, creds: Dict[str, Any]):
        boto3 = _import_boto3()
        return boto3.client(
            "route53",
            aws_access_key_id=(creds.get("aws_access_key_id") or "").strip(),
            aws_secret_access_key=(creds.get("aws_secret_access_key") or "").strip(),
        )

    # ── test ──
    async def test(self, config, creds) -> Dict[str, Any]:
        if _import_boto3() is None:
            return {"ok": False, "detail": "The 'boto3' package is not installed on the server."}
        if not ((creds.get("aws_access_key_id") or "").strip()
                and (creds.get("aws_secret_access_key") or "").strip()):
            return {"ok": False, "detail": "Enter both the AWS access key ID and secret first."}
        try:
            client = self._client(creds)
            await asyncio.to_thread(lambda: client.list_hosted_zones(MaxItems="1"))
        except Exception as e:
            return {"ok": False, "detail": f"AWS rejected the request: {e}"}
        return {"ok": True, "detail": "Credentials are valid."}

    # ── list_zones ──
    async def list_zones(self, config, creds) -> Dict[str, Any]:
        if _import_boto3() is None:
            return {"ok": False, "zones": [], "detail": "The 'boto3' package is not installed."}
        try:
            client = self._client(creds)

            def _list():
                out, marker = [], None
                while True:
                    kw = {"MaxItems": "100"}
                    if marker:
                        kw["Marker"] = marker
                    resp = client.list_hosted_zones(**kw)
                    for z in resp.get("HostedZones", []):
                        # Skip private zones — they don't serve the public internet.
                        if (z.get("Config") or {}).get("PrivateZone"):
                            continue
                        out.append({"id": z["Id"], "name": (z["Name"] or "").rstrip(".")})
                    if resp.get("IsTruncated"):
                        marker = resp.get("NextMarker")
                    else:
                        break
                return out

            zones = await asyncio.to_thread(_list)
        except Exception as e:
            return {"ok": False, "zones": [], "detail": str(e)}
        return {"ok": True, "zones": zones, "detail": ""}

    # ── list_records ──
    async def list_records(self, config, creds, zone) -> Dict[str, Any]:
        if _import_boto3() is None:
            return {"ok": False, "records": [], "detail": "The 'boto3' package is not installed."}
        zid = zone.get("id")
        try:
            client = self._client(creds)

            def _list():
                resp = client.list_resource_record_sets(HostedZoneId=zid, MaxItems="200")
                out = []
                for r in resp.get("ResourceRecordSets", []):
                    if r.get("Type") != "A":
                        continue
                    vals = ", ".join(v.get("Value", "") for v in r.get("ResourceRecords", []))
                    out.append({"id": r.get("Name"), "name": (r.get("Name") or "").rstrip("."),
                                "type": "A", "value": vals, "ttl": r.get("TTL"), "proxied": False})
                return out

            recs = await asyncio.to_thread(_list)
        except Exception as e:
            return {"ok": False, "records": [], "detail": str(e)}
        return {"ok": True, "records": recs, "detail": ""}

    # ── upsert (a single UPSERT change does create-or-update) ──
    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        if _import_boto3() is None:
            yield done({"ok": False, "message": "The 'boto3' package is not installed on the server."})
            return
        zid = zone.get("id")
        zname = zone.get("name") or ""
        name = fqdn(record.get("name", "@"), zname)
        ip = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 300)
        change = {
            "Changes": [{
                "Action": "UPSERT",
                "ResourceRecordSet": {
                    "Name": name, "Type": "A", "TTL": ttl,
                    "ResourceRecords": [{"Value": ip}],
                },
            }],
            "Comment": "Set by WebAgent Domains panel",
        }
        yield ev(f"Sending an UPSERT for {name} → {ip}…", phase="write")
        try:
            client = self._client(creds)
            await asyncio.to_thread(
                lambda: client.change_resource_record_sets(HostedZoneId=zid, ChangeBatch=change))
        except Exception as e:
            logger.exception("route53 upsert failed")
            yield done({"ok": False, "message": str(e)})
            return
        yield ev("Route 53 accepted the change.", phase="done", level="ok")
        yield done({"ok": True, "message": f"{name} now points at {ip} on Route 53.",
                    "record": {"name": name, "type": "A", "value": ip, "ttl": ttl}})


PROVIDER = Route53Provider()

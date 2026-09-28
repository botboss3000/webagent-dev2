"""DNS provider: Namecheap.

Namecheap's API is the awkward one, so this provider earns its length:

  * It's an XML API (``https://api.namecheap.com/xml.response``), not JSON — every
    call returns an ``<ApiResponse Status="OK|ERROR">`` document we parse.
  * Auth is three plaintext-ish values on the query string: the API user, the API
    key (the secret), and the account username (usually the same as the API user).
  * IT ONLY ANSWERS REQUESTS FROM A WHITELISTED IP. The admin must add THIS
    server's public IP to Namecheap's API allow-list, and every call must pass that
    same IP as ``ClientIp``. We auto-detect the server's public IP (and let the
    admin override it) — if it isn't whitelisted, Namecheap says so and we surface
    that verbatim, because it's the single most common failure.
  * Writing a record uses ``setHosts``, which REPLACES THE ENTIRE host list for a
    domain. Sending only the one record we care about would delete every other
    record. So an upsert here is READ-MODIFY-WRITE: getHosts → merge our A record
    into the existing set → setHosts the whole set back. This is the classic
    Namecheap footgun; the merge below is the guard against it.

Also enable API access under Profile → Tools → API Access, which needs a small
account balance or domain count on Namecheap's side.
"""

from __future__ import annotations

import logging
import xml.etree.ElementTree as ET
from typing import Any, AsyncIterator, Dict, List

import httpx

from app.dns.base import BaseDNSProvider, done, ev
from app.dns.providers._common import split_host, split_sld_tld

logger = logging.getLogger(__name__)

FEATURE = {
    "id": "namecheap",
    "display_name": "Namecheap",
    "category": "dns",
    "status": "beta",
    "summary": "Point a domain registered at Namecheap at your server.",
    "requires": ["A Namecheap account with API access enabled",
                 "This server's public IP added to Namecheap's API allow-list"],
}

_API = "https://api.namecheap.com/xml.response"


def _local(tag: str) -> str:
    """Strip the XML namespace ElementTree prepends ('{ns}Tag' → 'Tag')."""
    return tag.rsplit("}", 1)[-1]


def _find(el, name: str):
    for child in el.iter():
        if _local(child.tag) == name:
            return child
    return None


def _parse(text: str):
    """Return (root, ok, error_message). Never raises on a normal API error."""
    try:
        root = ET.fromstring(text)
    except Exception as e:
        return None, False, f"Namecheap returned an unreadable response ({e})."
    status = (root.attrib.get("Status") or "").upper()
    if status == "OK":
        return root, True, ""
    # Collect any <Error> text.
    errs: List[str] = []
    for e in root.iter():
        if _local(e.tag) == "Error" and (e.text or "").strip():
            errs.append(e.text.strip())
    return root, False, "; ".join(errs) or "Namecheap reported an error."


async def _public_ip(config: Dict[str, Any]) -> str:
    """This server's public IP — the value Namecheap must have whitelisted and
    that every call passes as ClientIp. An explicit override wins; otherwise ask
    ipify. Blank on failure (the call then fails with Namecheap's own IP error)."""
    override = (config.get("client_ip") or "").strip()
    if override:
        return override
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get("https://api.ipify.org", params={"format": "text"})
        if r.status_code == 200:
            return (r.text or "").strip()
    except Exception:
        pass
    return ""


class NamecheapProvider(BaseDNSProvider):
    id = "namecheap"
    display_name = "Namecheap"
    icon = "globe"
    summary = ("Point a domain registered at Namecheap at your server. Needs API access turned "
               "on and this server's public IP added to Namecheap's API allow-list.")
    requires = [
        "A domain registered at Namecheap using Namecheap's BasicDNS",
        "API access enabled under Profile → Tools → API Access",
        "This server's public IP added to the API allow-list",
    ]

    config_fields = [
        {"key": "api_user", "label": "Namecheap username", "type": "text", "required": True,
         "placeholder": "your Namecheap account username",
         "tip": "Your Namecheap account username — the same one you log in with. Used as both "
                "the API user and the account username for the API."},
        {"key": "client_ip", "label": "This server's public IP (optional)", "type": "text",
         "placeholder": "auto-detected — only set to override",
         "tip": "Namecheap only accepts API calls from an IP you've added to its allow-list, and "
                "every call must send that same IP. The app auto-detects this server's public IP; "
                "only fill this in to force a specific one. Add this IP under Profile → Tools → "
                "API Access → Manage → Whitelisted IPs."},
    ]
    credential_fields = [
        {"key": "api_key", "label": "Namecheap API key", "type": "password", "secret": True,
         "required": True, "placeholder": "your API key",
         "link": {"url": "https://ap.www.namecheap.com/settings/tools/apiaccess/",
                  "label": "Open API Access ↗"},
         "tip": "Under Profile → Tools → API Access, switch API access ON and copy the API key. "
                "Also add this server's public IP to the whitelist there. Stored encrypted."},
    ]

    def _base_params(self, config, creds, client_ip: str) -> Dict[str, str]:
        user = (config.get("api_user") or "").strip()
        return {"ApiUser": user, "ApiKey": (creds.get("api_key") or "").strip(),
                "UserName": user, "ClientIp": client_ip}

    async def _call(self, config, creds, client_ip, command, extra=None):
        params = self._base_params(config, creds, client_ip)
        params["Command"] = command
        if extra:
            params.update(extra)
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.get(_API, params=params)
        return _parse(r.text)

    # ── test ──
    async def test(self, config, creds) -> Dict[str, Any]:
        if not (config.get("api_user") or "").strip():
            return {"ok": False, "detail": "Enter your Namecheap username first."}
        if not (creds.get("api_key") or "").strip():
            return {"ok": False, "detail": "Paste your Namecheap API key first."}
        ip = await _public_ip(config)
        if not ip:
            return {"ok": False, "detail": "Could not determine this server's public IP — set it "
                                           "manually in the field above."}
        try:
            _, ok, err = await self._call(config, creds, ip, "namecheap.domains.getList",
                                          {"PageSize": "2"})
        except Exception as e:
            return {"ok": False, "detail": f"Could not reach Namecheap: {e}"}
        if ok:
            return {"ok": True, "detail": f"Connected (calling from {ip})."}
        return {"ok": False, "detail": err or "Namecheap rejected the request.",
                "hint_ip": ip}

    # ── list_zones ──
    async def list_zones(self, config, creds) -> Dict[str, Any]:
        ip = await _public_ip(config)
        if not ip:
            return {"ok": False, "zones": [], "detail": "Could not determine this server's public IP."}
        zones: List[Dict[str, str]] = []
        try:
            page = 1
            while True:
                root, ok, err = await self._call(config, creds, ip, "namecheap.domains.getList",
                                                 {"PageSize": "100", "Page": str(page)})
                if not ok:
                    return {"ok": False, "zones": [], "detail": err}
                got = 0
                for el in (root.iter() if root is not None else []):
                    if _local(el.tag) == "Domain" and el.attrib.get("Name"):
                        zones.append({"id": el.attrib["Name"], "name": el.attrib["Name"]})
                        got += 1
                # Namecheap paginates; stop when a page returns nothing.
                if got == 0 or page >= 20:
                    break
                page += 1
        except Exception as e:
            return {"ok": False, "zones": [], "detail": str(e)}
        # De-dupe (defensive) and sort.
        seen, uniq = set(), []
        for z in zones:
            if z["name"] not in seen:
                seen.add(z["name"])
                uniq.append(z)
        uniq.sort(key=lambda z: z["name"])
        return {"ok": True, "zones": uniq, "detail": ""}

    # ── read the full host set (used by both list_records and the upsert merge) ──
    async def _get_hosts(self, config, creds, ip, sld, tld):
        root, ok, err = await self._call(config, creds, ip, "namecheap.domains.dns.getHosts",
                                         {"SLD": sld, "TLD": tld})
        if not ok:
            return None, err
        hosts = []
        for el in (root.iter() if root is not None else []):
            if _local(el.tag) == "host":
                a = el.attrib
                hosts.append({"id": a.get("HostId"), "name": a.get("Name"), "type": a.get("Type"),
                              "value": a.get("Address"), "ttl": a.get("TTL"),
                              "mxpref": a.get("MXPref")})
        return hosts, ""

    async def list_records(self, config, creds, zone) -> Dict[str, Any]:
        ip = await _public_ip(config)
        if not ip:
            return {"ok": False, "records": [], "detail": "Could not determine this server's public IP."}
        sld, tld = split_sld_tld(zone.get("name") or "")
        hosts, err = await self._get_hosts(config, creds, ip, sld, tld)
        if hosts is None:
            return {"ok": False, "records": [], "detail": err}
        recs = [{"id": h.get("id"), "name": h.get("name"), "type": h.get("type"),
                 "value": h.get("value"), "ttl": h.get("ttl"), "proxied": False}
                for h in hosts if h.get("type") == "A"]
        return {"ok": True, "records": recs, "detail": ""}

    # ── upsert (READ-MODIFY-WRITE so setHosts doesn't wipe the zone) ──
    async def upsert_record(self, config, creds, zone, record) -> AsyncIterator[Dict[str, Any]]:
        domain = zone.get("name") or ""
        sld, tld = split_sld_tld(domain)
        label, is_apex = split_host(record.get("name", "@"), domain)
        host_name = "@" if is_apex else label
        ip_val = (record.get("value") or "").strip()
        ttl = int(record.get("ttl") or 300)
        shown = domain if is_apex else f"{label}.{domain}"

        client_ip = await _public_ip(config)
        if not client_ip:
            yield done({"ok": False, "message": "Could not determine this server's public IP — set "
                                                "it manually and make sure it's whitelisted at Namecheap."})
            return
        yield ev(f"Reading the current records for {domain}…", phase="check")
        hosts, err = await self._get_hosts(config, creds, client_ip, sld, tld)
        if hosts is None:
            yield done({"ok": False, "message": err})
            return

        # Merge: keep every existing host, replace the A record that matches our
        # host name, or append ours if none matched. Missing setHosts would drop the
        # rest — this is the whole reason for the read-modify-write.
        merged: List[Dict[str, str]] = []
        replaced = False
        for h in hosts:
            if h.get("type") == "A" and (h.get("name") or "") == host_name:
                merged.append({"name": host_name, "type": "A", "value": ip_val, "ttl": str(ttl),
                               "mxpref": h.get("mxpref") or "10"})
                replaced = True
            else:
                merged.append({"name": h.get("name") or "@", "type": h.get("type") or "A",
                               "value": h.get("value") or "", "ttl": h.get("ttl") or "1800",
                               "mxpref": h.get("mxpref") or "10"})
        if not replaced:
            merged.append({"name": host_name, "type": "A", "value": ip_val, "ttl": str(ttl),
                           "mxpref": "10"})

        # Build the setHosts params: HostName1/RecordType1/Address1/TTL1/MXPref1, …
        params: Dict[str, str] = {"SLD": sld, "TLD": tld}
        for i, h in enumerate(merged, start=1):
            params[f"HostName{i}"] = h["name"]
            params[f"RecordType{i}"] = h["type"]
            params[f"Address{i}"] = h["value"]
            params[f"TTL{i}"] = str(h["ttl"])
            if h["type"] == "MX":
                params[f"MXPref{i}"] = str(h.get("mxpref") or "10")

        yield ev(f"{'Updating' if replaced else 'Adding'} the A record for {shown} → {ip_val}…",
                 phase="write")
        try:
            _, ok, err = await self._call(config, creds, client_ip,
                                          "namecheap.domains.dns.setHosts", params)
        except Exception as e:
            logger.exception("namecheap setHosts failed")
            yield done({"ok": False, "message": str(e)})
            return
        if not ok:
            yield done({"ok": False, "message": err})
            return
        yield ev("Namecheap accepted the change.", phase="done", level="ok")
        yield done({"ok": True, "message": f"{shown} now points at {ip_val} on Namecheap.",
                    "record": {"name": shown, "type": "A", "value": ip_val, "ttl": ttl}})


PROVIDER = NamecheapProvider()

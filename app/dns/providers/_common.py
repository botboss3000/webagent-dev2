"""Shared helpers for DNS providers — the name math every provider repeats.

A domain (zone) is "example.com". A record the admin wants points a HOST at an
IP: the host is either the apex ("example.com" itself) or a subdomain
("app.example.com"). Providers disagree on how they want that host expressed:

  * Cloudflare / Route 53 want the FULL name ("app.example.com").
  * DigitalOcean / Porkbun / Namecheap / Google want the RELATIVE part — the
    label BELOW the zone — with "@" (or "") meaning the apex.

So this module centralises the split so each provider just picks the shape it
needs. It also normalises whatever the admin typed (trailing dots, an accidental
"https://", the zone typed into the host box) into a clean host.
"""

from __future__ import annotations

from typing import Tuple


def clean_host(raw: str) -> str:
    """Normalise a typed host: strip scheme, path, spaces, trailing dot, lowercase."""
    h = (raw or "").strip().lower()
    if "://" in h:
        h = h.split("://", 1)[1]
    h = h.split("/", 1)[0].strip()
    return h.rstrip(".")


def clean_zone(raw: str) -> str:
    """Normalise a zone/domain name the same way."""
    return clean_host(raw)


def split_host(host: str, zone_name: str) -> Tuple[str, bool]:
    """Return (relative_label, is_apex) for ``host`` within ``zone_name``.

    "example.com"        in "example.com" → ("@", True)
    "app.example.com"    in "example.com" → ("app", False)
    "a.b.example.com"    in "example.com" → ("a.b", False)
    "@" / "" / zone_name                  → ("@", True)

    If ``host`` doesn't end in the zone it's treated as already-relative (the
    admin typed just the label), so "app" in "example.com" → ("app", False).
    """
    host = clean_host(host)
    zone = clean_zone(zone_name)
    if host in ("", "@", zone):
        return "@", True
    suffix = "." + zone
    if host.endswith(suffix):
        label = host[: -len(suffix)]
        return (label or "@", label == "")
    # Already a bare label (no zone suffix) — keep it, it's a subdomain.
    return host, False


def fqdn(host: str, zone_name: str) -> str:
    """The full name for ``host`` in ``zone_name`` (apex → the zone itself)."""
    label, is_apex = split_host(host, zone_name)
    zone = clean_zone(zone_name)
    return zone if is_apex else f"{label}.{zone}"


def split_sld_tld(zone_name: str) -> Tuple[str, str]:
    """Split a bare domain into (SLD, TLD) the way Namecheap's API wants them:
    "example.com" → ("example", "com"); "example.co.uk" → ("example", "co.uk")."""
    zone = clean_zone(zone_name)
    parts = zone.split(".")
    if len(parts) <= 2:
        return parts[0], ".".join(parts[1:]) if len(parts) > 1 else ""
    # Two-label public suffixes (co.uk, com.au, …) — keep the last two as the TLD.
    return ".".join(parts[:-2]), ".".join(parts[-2:])

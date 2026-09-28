"""Provider-independent DNS resolution check.

After ANY provider writes an A record — or after the Manual provider tells the
admin to write one themselves — the panel needs the same neutral question: does
``host`` actually resolve to ``ip`` yet? This module answers it without caring
which provider (or registrar) made the change, so it lives OUTSIDE the providers.

Two strategies, best-effort:

  * If ``dnspython`` is installed, query PUBLIC resolvers (Cloudflare 1.1.1.1 and
    Google 8.8.8.8) directly. This dodges the local machine's own DNS cache, so a
    just-written record shows up as soon as those resolvers see it — the honest
    signal for "has it propagated".
  * Otherwise fall back to the standard library's ``getaddrinfo`` (the system
    resolver). Correct, but subject to local caching, so it can lag a freshly
    changed record by the old record's TTL. We say so in the wording.

Everything runs in a worker thread (``asyncio.to_thread``) because both paths are
blocking sockets — never block the event loop, per the chat-hotpath latency rule.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Dict, List

logger = logging.getLogger(__name__)

_PUBLIC_RESOLVERS = ["1.1.1.1", "8.8.8.8"]


def _resolve_dnspython(host: str) -> List[str]:
    """Query public resolvers directly for the host's A records. Raises ImportError
    if dnspython is absent (the caller then falls back to getaddrinfo)."""
    import dns.resolver  # type: ignore  # noqa: F401

    ips: List[str] = []
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = list(_PUBLIC_RESOLVERS)
    r.lifetime = 6.0
    r.timeout = 3.0
    try:
        answer = r.resolve(host, "A")
        ips = sorted({rr.address for rr in answer})
    except Exception:
        ips = []
    return ips


def _resolve_getaddrinfo(host: str) -> List[str]:
    """System-resolver fallback (may be cached)."""
    try:
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
        return sorted({i[4][0] for i in infos})
    except Exception:
        return []


def _resolve(host: str) -> Dict[str, object]:
    host = (host or "").strip().rstrip(".")
    if not host:
        return {"ips": [], "via": "none"}
    try:
        ips = _resolve_dnspython(host)
        if ips:
            return {"ips": ips, "via": "public"}
        # dnspython present but no answer yet — still "public", just empty.
        return {"ips": [], "via": "public"}
    except ImportError:
        return {"ips": _resolve_getaddrinfo(host), "via": "system"}
    except Exception as e:  # noqa: BLE001
        logger.debug("dns verify resolve failed for %s: %s", host, e)
        return {"ips": _resolve_getaddrinfo(host), "via": "system"}


async def check(host: str, expected_ip: str = "") -> Dict[str, object]:
    """Resolve ``host`` and (optionally) compare against ``expected_ip``.

    Returns ``{ok, matches, ips, via, detail}``:
      * ``ok``      — the host resolved to at least one address.
      * ``matches`` — ``expected_ip`` is among them (False if none given/found).
      * ``ips``     — every A address seen.
      * ``via``     — "public" (dnspython) | "system" (getaddrinfo) | "none".
      * ``detail``  — a human sentence the panel can show verbatim.
    """
    expected_ip = (expected_ip or "").strip()
    res = await asyncio.to_thread(_resolve, host)
    ips = list(res.get("ips") or [])
    via = str(res.get("via") or "none")
    ok = bool(ips)
    matches = bool(expected_ip and expected_ip in ips)

    lag = " (your machine's DNS cache can lag by the record's TTL)" if via == "system" else ""
    if not ok:
        detail = (f"{host} doesn't resolve yet — DNS changes can take a few minutes "
                  f"to a few hours to spread{lag}.")
    elif expected_ip and matches:
        detail = f"{host} now points at {expected_ip}. HTTPS will be issued automatically once it has."
    elif expected_ip and not matches:
        detail = (f"{host} currently resolves to {', '.join(ips)}, not {expected_ip} yet — "
                  f"the change may still be spreading{lag}.")
    else:
        detail = f"{host} resolves to {', '.join(ips)}."
    return {"ok": ok, "matches": matches, "ips": ips, "via": via, "detail": detail}

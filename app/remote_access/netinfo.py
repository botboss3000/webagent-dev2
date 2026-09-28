"""Local-network reachability info for the Remote Access card.

Detects the machine's LAN address + the port the app is bound to so a phone on
the same Wi-Fi can reach this server directly, and renders a QR code for the
address (the QR is a progressive enhancement — if the optional ``qrcode``
package isn't installed the card just shows the address text).
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import List, Optional


def get_port() -> int:
    """The TCP port this app is bound to (matches app/main.py's PORT default)."""
    try:
        return int(os.environ.get("PORT", "8080"))
    except (TypeError, ValueError):
        return 8080


def get_lan_ip() -> str:
    """Primary LAN IPv4 of this machine (the interface used for outbound traffic).

    Uses the standard UDP-connect trick: connecting a datagram socket sets the
    OS routing without sending a packet, then the local side of the socket is
    our address on the network. Falls back to loopback if offline.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        try:
            s.close()
        except Exception:
            pass


def _is_reachable_lan_ip(ip: str, *, is_primary: bool = False) -> bool:
    """True if ``ip`` is plausibly reachable by another device on the same Wi-Fi.

    The primary (internet-facing) address is always trusted. For the *extra*
    addresses the OS reports we drop the ones that belong to virtual adapters —
    WSL, Hyper-V's Default Switch, Docker, VirtualBox, VMware — which build
    private NAT networks that only exist *inside* this PC. A phone on the Wi-Fi
    can never reach those, so listing them just muddies "which one do I use".

    The tell: a virtual switch makes the Windows host the *gateway* of its NAT
    subnet, so the host's own address ends in ``.1``. A real DHCP-assigned
    Wi-Fi/Ethernet address is never ``.1`` (that's the router) — so a private
    ``.1`` address we did *not* route the internet through is a virtual adapter.
    """
    try:
        addr = ipaddress.IPv4Address(ip)
    except (ipaddress.AddressValueError, ValueError):
        return False
    if addr.is_loopback or addr.is_link_local or addr.is_unspecified or addr.is_multicast:
        return False
    if is_primary:
        return True
    if addr.is_private and (int(addr) & 0xFF) == 1:
        return False
    return True


def list_lan_ips() -> List[str]:
    """Real LAN IPv4 addresses a same-Wi-Fi device can reach, primary first.

    Virtual-adapter addresses (WSL / Hyper-V / Docker / VM host-only networks)
    are filtered out — see :func:`_is_reachable_lan_ip` — so the Remote Access
    card only lists addresses a phone on the same Wi-Fi can actually open.
    """
    ips: List[str] = []
    primary = get_lan_ip()
    if primary and _is_reachable_lan_ip(primary, is_primary=True):
        ips.append(primary)
    try:
        host = socket.gethostname()
        for info in socket.getaddrinfo(host, None, socket.AF_INET):
            ip = info[4][0]
            if ip and ip not in ips and _is_reachable_lan_ip(ip):
                ips.append(ip)
    except Exception:
        pass
    return ips


def same_network_urls(scheme: str = "http") -> List[str]:
    """Candidate ``http://<ip>:<port>`` addresses for same-network devices."""
    port = get_port()
    return [f"{scheme}://{ip}:{port}" for ip in list_lan_ips()]


def primary_same_network_url(scheme: str = "http") -> str:
    """The single best same-network address to surface to the user."""
    return f"{scheme}://{get_lan_ip()}:{get_port()}"


def qr_svg(data: str) -> Optional[str]:
    """An inline SVG QR code for ``data``, or None if ``qrcode`` isn't installed.

    Kept dependency-optional on purpose: the feature works without it (the card
    falls back to plain address text + a copy button), so a missing package
    never breaks Remote Access.
    """
    if not data:
        return None
    try:
        import io
        import qrcode
        import qrcode.image.svg

        factory = qrcode.image.svg.SvgPathImage
        img = qrcode.make(data, image_factory=factory, box_size=10, border=2)
        buf = io.BytesIO()
        img.save(buf)
        svg = buf.getvalue().decode("utf-8")
        # Strip the XML prolog so it embeds cleanly inside HTML.
        idx = svg.find("<svg")
        return svg[idx:] if idx != -1 else svg
    except Exception:
        return None

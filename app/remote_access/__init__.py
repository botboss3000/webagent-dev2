"""Remote Access — let a phone (or any device) reach this WebAgent server.

Offers several connection methods through one App Config card:

* **same_network** — show this PC's LAN address + QR for devices on the same Wi-Fi
* **ngrok** — app-managed outbound tunnel (reserved domain = stable address)
* **cloudflare** — app-managed named tunnel on a domain you own (stable HTTPS)
* **tailscale** — private mesh; status + guidance (system service)
* **manual** — your own port-forward; status + reachability probe

Plus a multi-tenant **signpost** directory so a fixed phone bookmark always
resolves to the PC's current address — usable just for yourself, or hosted for
other users (each keyed by their own rendezvous key).

See ``manager`` for the orchestrator and the ``start_remote_access`` /
``stop_remote_access`` lifecycle hooks.
"""

from .manager import (  # noqa: F401
    get_manager,
    start_remote_access,
    stop_remote_access,
)

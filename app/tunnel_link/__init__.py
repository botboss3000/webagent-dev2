"""Tunnel Link — the WebAgent side of the tunnel.webagent.live feature.

This instance registers itself with the hosted relay (the separate
`webagent-tunnel` service), keeps its Cloudflare tunnel address current there,
and mints short-lived pairing codes that the App Settings → Tunnel panel shows
as a QR. A phone scans that QR at the relay to link itself and then view this
instance's app. See store.py for what's persisted and app/admin/tunnel_link.py
for the admin API.
"""

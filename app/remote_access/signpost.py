"""Signpost client — pushes this PC's current public address to the directory.

When a managed tunnel's address changes, the PC calls this to update the
always-on signpost server (default: webagent.live) so the phone's fixed
bookmark always lands on the live address. The push is authenticated by the
secret push token bound to the rendezvous key.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


async def push_pointer(server_url: str, key: str, push_token: str, url: str, label: str = "") -> bool:
    """POST the current address to ``<server_url>/api/v1/remote-access/pointer``.

    Returns True on success. Never raises — a signpost outage must not take the
    tunnel down.
    """
    if not server_url or not key or not url:
        return False
    endpoint = server_url.rstrip("/") + "/api/v1/remote-access/pointer"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                endpoint,
                json={"key": key, "push_token": push_token, "url": url, "label": label},
            )
        if resp.status_code == 200 and resp.json().get("ok"):
            return True
        logger.warning("signpost push rejected (%s): %s", resp.status_code, resp.text[:200])
        return False
    except Exception as e:
        logger.warning("signpost push failed: %s", e)
        return False

"""P2P Mirror ability — thin shim wrapping app/p2p core for agent use.

Discoverable by the ability loader via the three standard hooks:
  • FEATURE        — catalog + UI (in p2p.json)
  • build_tools()  — returns {tool_name: handler} dict
  • TOOL_SCHEMAS   — JSON Schema per tool
"""

from __future__ import annotations

import asyncio
from typing import Any, Dict, Optional

# Populated by build_tools(), read by the loader AFTER the call.
TOOL_SCHEMAS: Dict[str, Any] = {}
DESTRUCTIVE: set = set()


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the p2p tools."""

    # ── add_peer ────────────────────────────────────────────────────────────
    async def _add_peer(url: str, name: str) -> str:
        """Add a peer instance for P2P data mirroring.

        Performs an Ed25519 key exchange handshake with the remote instance and
        stores the peer. Once added, the background worker keeps data/ in sync.

        Args:
            url: The peer's base URL (e.g. https://other-instance.example.com)
            name: A friendly name for the peer

        Returns:
            Status message with peer id and sync state.
        """
        import json
        import httpx
        from app.p2p import identity
        from app.p2p import store as peer_store

        url = url.rstrip("/")
        my_info = {
            "instance_id": identity.instance_id(),
            "public_key": identity.public_key_hex(),
            "name": identity.instance_id(),
            "url": "",  # filled by the peer if it knows our URL
        }

        try:
            async with httpx.AsyncClient(timeout=15.0, verify=False) as client:
                # Phase 1: GET /status to get remote info
                resp = await client.get(f"{url}/api/v1/p2p/status")
                if resp.status_code != 200:
                    return f"Error: peer at {url} returned {resp.status_code}"
                remote_info = resp.json()

                # Phase 2: POST /handshake to register ourselves
                handshake_resp = await client.post(
                    f"{url}/api/v1/p2p/handshake",
                    json=my_info,
                )
                if handshake_resp.status_code != 200:
                    return f"Error: handshake failed ({handshake_resp.status_code})"

                hs_data = handshake_resp.json()

            # Store the peer locally
            stored = peer_store.add_peer(
                url=url,
                name=name,
                public_key_hex=remote_info["public_key"],
                remote_instance_id=remote_info["instance_id"],
            )

            return (
                f"Peer '{name}' added successfully.\n"
                f"  Peer ID: {stored['id']}\n"
                f"  Remote instance: {remote_info['instance_id']}\n"
                f"  URL: {url}\n"
                f"  Sync will begin within 60 seconds."
            )
        except httpx.ConnectError:
            return f"Error: could not connect to {url} — check the URL and ensure the remote instance is running."
        except Exception as e:
            return f"Error adding peer: {e}"

    # ── remove_peer ─────────────────────────────────────────────────────────
    async def _remove_peer(peer_id: str) -> str:
        """Remove a configured P2P peer."""
        from app.p2p import store as peer_store
        ok = peer_store.remove_peer(peer_id)
        if ok:
            return f"Peer {peer_id} removed."
        return f"Peer {peer_id} not found."

    # ── list_peers ──────────────────────────────────────────────────────────
    async def _list_peers() -> str:
        """List all configured P2P sync peers with their status."""
        from app.p2p import store as peer_store
        peers = peer_store.list_peers()
        if not peers:
            return "No P2P peers configured. Use add_peer(url, name) to add one."
        lines = [f"{len(peers)} peer(s):"]
        for p in peers:
            sync = p.get("sync", {})
            status = sync.get("status", "unknown")
            last = sync.get("last_sync_at") or "never"
            lines.append(
                f"  • {p['name']} ({p['id']}) — {status} — last sync: {last}"
            )
        return "\n".join(lines)

    # ── sync_now ────────────────────────────────────────────────────────────
    async def _sync_now(peer_id: str) -> str:
        """Trigger an immediate sync with a peer.  Wakes the background worker.

        Args:
            peer_id: The peer to sync with (use list_peers to see ids)

        Returns:
            Status message. The actual sync runs in the background.
        """
        from app.p2p import store as peer_store
        from app.p2p.worker import kick as kick_worker

        peer = peer_store.get_peer(peer_id)
        if not peer:
            return f"Peer {peer_id} not found."

        # Kick the worker so it syncs now instead of on the timer
        kick_worker()
        return f"Sync kick sent for peer '{peer.get('name', peer_id)}'. The worker will sync within a few seconds."

    # ── Build response ──────────────────────────────────────────────────────
    handlers = {
        "add_peer": _add_peer,
        "remove_peer": _remove_peer,
        "list_peers": _list_peers,
        "sync_now": _sync_now,
    }

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "add_peer": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Base URL of the peer instance (e.g. https://other-instance.example.com)"},
                "name": {"type": "string", "description": "Friendly name for the peer"},
            },
            "required": ["url", "name"],
        },
        "remove_peer": {
            "type": "object",
            "properties": {
                "peer_id": {"type": "string", "description": "ID of the peer to remove (from list_peers)"},
            },
            "required": ["peer_id"],
        },
        "list_peers": {
            "type": "object",
            "properties": {},
            "required": [],
        },
        "sync_now": {
            "type": "object",
            "properties": {
                "peer_id": {"type": "string", "description": "ID of the peer to sync (from list_peers)"},
            },
            "required": ["peer_id"],
        },
    })

    DESTRUCTIVE.clear()
    DESTRUCTIVE.update({"add_peer", "remove_peer", "sync_now"})

    return handlers

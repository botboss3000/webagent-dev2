"""Remote Tunnel ability — SELF-CONTAINED drop-in.

This file is the whole capability: descriptor (the .json sibling), runtime
handlers, and schemas all live here. There is NO core factory — ``build_tools``
defines the two handlers inline. All heavy imports (app.remote_access.*) stay
LAZY inside the handlers, so merely importing this module never pulls in the app.

Discovered generically by core via the optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • the .json sibling — catalog + UI + which tool names it gates
  • build_tools()      — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools() runs

What it does: lets an agent open a **Cloudflare quick tunnel** to this machine —
the same ephemeral ``https://<name>.trycloudflare.com`` address the Remote Access
card in App Settings offers — and read the live address back.

It deliberately reuses the ONE shared tunnel owner, ``RemoteAccessManager``
(app/remote_access/manager.py), rather than spawning its own ``cloudflared``.
That singleton is the same object the Data Settings card drives, so the agent and
the UI never fight over two competing tunnels, and whichever side started it can
see and stop it. ``start_tunnel`` forces quick-mode for this run only (via the
manager's ``opts_override``), so it never clobbers an admin's saved *named*-tunnel
configuration.

Two tools:
  • start_tunnel   — launch the quick tunnel, wait for its address, return it.
  • get_tunnel_url — re-read the current tunnel's address + running status.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() so the loader (which reads TOOL_SCHEMAS AFTER
# the call) sees the live schemas. start_tunnel exposes the host to the public
# internet, so it is marked destructive (⇒ requires_confirmation); the read-only
# get_tunnel_url is not.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = {"start_tunnel"}


# Module-level JSON schemas, defined once; build_tools copies them into
# TOOL_SCHEMAS each call.
_START_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "wait_seconds": {
            "type": "integer",
            "description": "How long to wait for the tunnel to report its public "
                           "address before returning (default 15, max 40). If it "
                           "hasn't appeared yet you can call get_tunnel_url shortly "
                           "after to fetch it.",
        },
    },
    "required": [],
}

_GET_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {},
    "required": [],
}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for the Remote Tunnel tools.

    Both handlers close over nothing app-specific and keep every heavy import
    (app.remote_access.manager / .tunnels / .store) lazy inside the call.
    """

    async def start_tunnel(wait_seconds: Optional[int] = None, **_legacy) -> str:
        """Launch a Cloudflare quick tunnel and return its public URL."""
        try:
            from app.remote_access import tunnels
            from app.remote_access.manager import get_manager
            from app.remote_access import store

            cfg = store.load_config()
            binp = tunnels.cloudflared_available(cfg.get("cloudflare", {}).get("bin_path", ""))
            if not binp:
                return json.dumps({
                    "status": "error",
                    "message": "cloudflared is not installed or not on PATH on this machine. "
                               "Install Cloudflare's 'cloudflared' first (no account needed for a quick tunnel), "
                               "then try again.",
                })

            try:
                wait = max(1, min(int(wait_seconds if wait_seconds is not None else 15), 40))
            except Exception:
                wait = 15

            # Reuse the ONE shared tunnel owner; force quick-mode for this run
            # only so a saved named-tunnel config is left untouched. The manager
            # blocks up to ~15 tries (≈15s) for the address itself; we optionally
            # keep polling its resolver a little longer if the caller asked.
            result = await get_manager().start_method("cloudflare", opts_override={"quick": True})
            if not result.get("ok"):
                return json.dumps({
                    "status": "error",
                    "message": result.get("error") or "failed to start the tunnel",
                    "output": result.get("output", ""),
                })

            url = result.get("public_url", "")
            if not url and wait > 15:
                # Give a slow-to-announce tunnel a little more grace.
                import asyncio
                mgr = get_manager()
                for _ in range(wait - 15):
                    await asyncio.sleep(1)
                    url = (await mgr.runtime_status()).get("public_url", "")
                    if url:
                        break

            if not url:
                return json.dumps({
                    "status": "starting",
                    "message": "The tunnel is launching but hasn't reported its address yet. "
                               "Call get_tunnel_url again in a few seconds.",
                })
            return json.dumps({
                "status": "ok",
                "public_url": url,
                "method": "cloudflare_quick",
                "message": f"Quick tunnel is live at {url}",
            })
        except Exception as e:
            logger.warning("start_tunnel failed: %s", e)
            return json.dumps({"status": "error", "message": str(e)})

    async def get_tunnel_url(**_legacy) -> str:
        """Report the current tunnel's public URL and whether it is running."""
        try:
            from app.remote_access.manager import get_manager
            rt = await get_manager().runtime_status()
            running = bool(rt.get("running"))
            url = rt.get("public_url", "")
            if running and url:
                return json.dumps({
                    "status": "ok",
                    "running": True,
                    "public_url": url,
                    "method": rt.get("active_method", ""),
                    "message": f"Tunnel is live at {url}",
                })
            return json.dumps({
                "status": "ok",
                "running": running,
                "public_url": url,
                "method": rt.get("active_method", ""),
                "error": rt.get("error", ""),
                "message": "No public tunnel is currently running. Use start_tunnel to open one."
                           if not running else
                           "The tunnel is running but hasn't reported an address yet — try again shortly.",
            })
        except Exception as e:
            logger.warning("get_tunnel_url failed: %s", e)
            return json.dumps({"status": "error", "message": str(e)})

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "start_tunnel": _START_PARAMETERS,
        "get_tunnel_url": _GET_PARAMETERS,
    })

    return {"start_tunnel": start_tunnel, "get_tunnel_url": get_tunnel_url}

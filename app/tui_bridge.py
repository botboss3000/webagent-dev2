"""TUI Bridge — forwards chat messages to the TUI agent when the user is chatting
with the "Web Agent TUI" agent.

The TUI (Server Manager) runs a small HTTP bridge server on a random localhost port.
When the web app detects that a session is bound to the "web-agent-tui" agent, it
forwards the message to the TUI bridge instead of running the normal agent loop.

Architecture:
  TUI process ──→ bridge server (localhost:XXXXX) ←── web app (FastAPI)
                     POST /bridge/send
                     GET  /bridge/health
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)

# ── In-memory bridge registry ────────────────────────────────────────────────
# Maps user_id → {"port": int, "registered_at": float}
_bridge_registry: Dict[str, Dict[str, Any]] = {}

# How long since last registration before we consider a bridge stale
_BRIDGE_TTL_SECONDS = 30.0


def register_bridge(user_id: str, port: int) -> None:
    """Register (or refresh) a TUI bridge for a user."""
    _bridge_registry[user_id] = {
        "port": port,
        "registered_at": time.time(),
    }
    logger.info("TUI bridge registered for user %s on port %d", user_id[:12], port)


def unregister_bridge(user_id: str) -> None:
    """Remove a TUI bridge registration."""
    _bridge_registry.pop(user_id, None)
    logger.info("TUI bridge unregistered for user %s", user_id[:12])


def get_bridge_port(user_id: str) -> Optional[int]:
    """Get the bridge port for a user, or None if not registered / stale."""
    entry = _bridge_registry.get(user_id)
    if entry is None:
        return None
    age = time.time() - entry["registered_at"]
    if age > _BRIDGE_TTL_SECONDS:
        logger.debug("TUI bridge for user %s is stale (age=%.1fs)", user_id[:12], age)
        return None
    return entry["port"]


def is_bridge_alive(user_id: str) -> bool:
    """Check if a TUI bridge is registered and not stale."""
    return get_bridge_port(user_id) is not None


async def forward_to_bridge(
    user_id: str,
    session_id: str,
    message: str,
    execution_mode: str = "ask",
) -> Optional[str]:
    """Forward a message to the TUI bridge and return the reply.
    
    ``execution_mode`` is passed through so the TUI can apply the
    frontend's read/write/auto selection. The web app's own guardrails
    (destructive tool gates, turn limits, etc.) are NOT applied — the
    TUI handles its own guardrails internally.
    
    Returns None if the bridge is not available.
    """
    port = get_bridge_port(user_id)
    if port is None:
        return None

    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=3.0)) as c:
            r = await c.post(
                f"http://127.0.0.1:{port}/bridge/send",
                json={
                    "session_id": session_id,
                    "message": message,
                    "user_id": user_id,
                    "execution_mode": execution_mode,
                },
            )
            if r.status_code == 200:
                data = r.json()
                return data.get("reply", "")
            else:
                logger.warning(
                    "TUI bridge returned %d for session %s: %s",
                    r.status_code, session_id[:12], r.text[:200],
                )
                return None
    except httpx.ConnectError:
        logger.warning("TUI bridge not reachable on port %d for user %s", port, user_id[:12])
        unregister_bridge(user_id)
        return None
    except Exception as e:
        logger.error("TUI bridge error for user %s: %s", user_id[:12], e)
        return None

"""Application event delivery, independent of conversation publication."""
import asyncio
from typing import Any, Dict, List

# ── Bounded listener sends ─────────────────────────────────────────────────────
# A listener socket that cannot accept a frame within SEND_TIMEOUT is presumed
# dead (full TCP buffer — a stalled browser tab) and MUST be evicted. One stuck
# listener must never block every other client's events, the run loop's
# broadcasts, or the chat send path: a single wedged browser socket previously
# hung every admin /send inside _reset_agent_model_switcher's model_override
# emit (grep WS-LISTENER-WEDGE for the full incident). Same convention as
# app/api/terminal.py's SEND_TIMEOUT.
SEND_TIMEOUT = 10  # seconds — bound any single send to a listener


async def _bounded_send(websocket: Any, payload: str) -> bool:
    """Send one frame with a hard timeout. Returns False when the peer is not
    draining — the caller must evict it (unregister + best-effort close)."""
    try:
        await asyncio.wait_for(websocket.send_text(payload), timeout=SEND_TIMEOUT)
        return True
    except Exception:
        return False


async def _close_evicted(websocket: Any) -> None:
    """Best-effort close of an evicted listener socket. Never blocks."""
    try:
        await asyncio.wait_for(websocket.close(), timeout=2)
    except Exception:
        pass


# ── User listener registry (per-user) ──
_user_listeners: Dict[str, List[tuple[Any, str]]] = {}


def register_user_listener(user_id: str, websocket: Any, *, device_id: str = ""):
    """Register a WebSocket that receives events for all of a user's sessions."""
    if user_id not in _user_listeners:
        _user_listeners[user_id] = []
    _user_listeners[user_id].append((websocket, str(device_id or "")))

def unregister_user_listener(user_id: str, websocket: Any):
    """Remove a WebSocket from the per-user listeners."""
    if user_id in _user_listeners:
        _user_listeners[user_id] = [
            entry for entry in _user_listeners[user_id] if entry[0] is not websocket
        ]
        if not _user_listeners[user_id]:
            del _user_listeners[user_id]


def user_listener_stats() -> dict[str, int]:
    """Return cheap in-process counts for the runtime observability dashboard."""
    return {
        "connections": sum(len(listeners) for listeners in _user_listeners.values()),
        "users": len(_user_listeners),
    }


async def _emit_to_user_listeners(user_id: str, event: Dict[str, Any]):
    """Push an event to all per-user listeners."""
    import json
    listeners = _user_listeners.get(user_id, [])
    disconnected = []
    for ws, _device_id in listeners:
        if not await _bounded_send(ws, json.dumps(event)):
            disconnected.append(ws)
    for ws in disconnected:
        unregister_user_listener(user_id, ws)
        await _close_evicted(ws)


async def notify_user(user_id: str, event: Dict[str, Any]) -> None:
    """Public, fire-and-forget broadcast of an out-of-band event to every live
    WebSocket a user has open (all tabs / devices). Thin wrapper over the
    per-user listener registry so other modules (agent CRUD endpoints, run-state
    bookkeeping) don't reach into the private emitter. Never raises — a delivery
    failure must not break the operation that triggered the notification."""
    if not user_id:
        return
    try:
        await _emit_to_user_listeners(user_id, event)
    except Exception:
        pass


async def revoke_user_device_connections(
    user_id: str,
    device_ids: list[str],
    event: Dict[str, Any],
) -> None:
    """Deliver a final purge directive, then close matching live device sockets."""
    import json

    targets = {str(value) for value in device_ids if value}
    if not user_id or not targets:
        return
    for websocket, device_id in list(_user_listeners.get(user_id, [])):
        if device_id not in targets:
            continue
        if await _bounded_send(websocket, json.dumps(event)):
            try:
                await websocket.close(code=4401, reason="Device session revoked")
            except Exception:
                pass
        else:
            await _close_evicted(websocket)
        unregister_user_listener(user_id, websocket)

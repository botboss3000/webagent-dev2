"""
Browser CONNECTOR backend — drive a remote user's REAL browser via an extension.

This is the THIRD browser backend, beside the in-app headless Chromium and the
same-machine "local" CDP attach (both in app/tools/browser.py). Here the user
installs a small browser extension that holds a WebSocket back to this server;
the agent's ``browser_action`` commands are forwarded over that socket and run in
the user's own, logged-in browser — even when that browser is on a DIFFERENT
machine, across the internet, from the server.

Layering (mirrors how app/api/browser_stream.py is a thin route over the engine
in app/tools/browser.py):
  • THIS file is the ENGINE half — registry + RPC + executor — importable with no
    FastAPI dependency, so app/tools/browser.py can call it.
  • app/api/connector_ws.py is the thin WebSocket ROUTE that authenticates the
    extension, registers its connection here, and pumps incoming messages in.

Keying: a command is ONLY ever sent to the authenticated owner's own
connection(s), so one user can never drive another's browser. Within a single
user, several connections (tabs / devices) may be open at once; we prefer the one
that already handled this ``bs_id``, else the most recently connected.

The reply normalisation hands back the SAME dict shape that
``app/tools/browser.browser_action`` returns (success / result / url / title /
error [/ mime_type]), so the agent cannot tell which backend ran the command.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# How long to wait for the extension to answer a single command before giving up.
# Generous because the command crosses the public internet to the user's device.
DEFAULT_CMD_TIMEOUT = 30.0


class ConnectorConn:
    """One live extension WebSocket for a user, plus its in-flight RPC futures.

    The route layer constructs this with a ``send_json`` coroutine bound to the
    open socket, registers it, then feeds every inbound frame to
    ``handle_incoming``. All command dispatch goes the other way, via ``rpc``.
    """

    def __init__(self, user_id: str,
                 send_json: Callable[[dict], Awaitable[None]],
                 version: str = ""):
        self.user_id = user_id
        self._send_json = send_json
        self.version = version
        self.installation_id = ""
        self.browser = "Chromium"
        self.browser_version = ""
        self.platform = ""
        self.mobile = False
        self.capabilities: Dict[str, Any] = {}
        self.settings: Dict[str, Any] = {}
        self.connected_at = time.time()
        self.last_seen = time.time()
        # bs_ids this connection has handled — so we keep routing them here.
        self.bs_ids: set[str] = set()
        # RPC id -> Future awaiting the matching {type:"result"} frame.
        self._pending: Dict[str, asyncio.Future] = {}

    async def _send(self, msg: dict) -> None:
        await self._send_json(msg)

    def resolve(self, rpc_id: str, payload: dict) -> None:
        fut = self._pending.pop(rpc_id, None)
        if fut is not None and not fut.done():
            fut.set_result(payload)

    def fail_all(self, error: str) -> None:
        """Resolve every pending RPC with an error — used on disconnect so no
        awaiting command hangs forever."""
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_result({"success": False, "error": error, "url": "", "title": ""})
        self._pending.clear()

    async def rpc(self, action: str, bs_id: str, params: dict, timeout: float) -> dict:
        """Send one command and await its reply (or a timeout)."""
        rpc_id = uuid.uuid4().hex
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rpc_id] = fut
        self.bs_ids.add(bs_id)
        try:
            await self._send({
                "type": "cmd", "id": rpc_id, "bs_id": bs_id,
                "action": action, "params": params,
            })
        except Exception as e:  # noqa: BLE001
            self._pending.pop(rpc_id, None)
            return {"success": False, "error": f"could not reach browser extension: {e}",
                    "url": "", "title": ""}
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(rpc_id, None)
            return {"success": False, "error": "browser extension timed out",
                    "url": "", "title": ""}


# user_id -> list of live connections
_connectors: Dict[str, List[ConnectorConn]] = {}


def register(conn: ConnectorConn) -> None:
    _connectors.setdefault(conn.user_id, []).append(conn)
    logger.info("connector: registered for %s (now %d open)", conn.user_id,
                len(_connectors[conn.user_id]))


def unregister(conn: ConnectorConn) -> None:
    conn.fail_all("browser extension disconnected")
    lst = _connectors.get(conn.user_id)
    if not lst:
        return
    remaining = [c for c in lst if c is not conn]
    if remaining:
        _connectors[conn.user_id] = remaining
    else:
        _connectors.pop(conn.user_id, None)
    logger.info("connector: unregistered for %s", conn.user_id)


def _pick(user_id: str, bs_id: Optional[str]) -> Optional[ConnectorConn]:
    conns = _connectors.get(user_id) or []
    if not conns:
        return None
    if bs_id:
        for c in conns:
            if bs_id in c.bs_ids:
                return c
    return max(conns, key=lambda c: c.connected_at)


def connector_connected(user_id: str) -> bool:
    """Whether this user has at least one live extension connection."""
    return bool(_connectors.get(user_id))


def connector_info(user_id: str) -> dict:
    """Small presence summary for the Browser page's status badge / agent checks."""
    conns = _connectors.get(user_id) or []
    if not conns:
        return {"connected": False}
    c = max(conns, key=lambda x: x.connected_at)
    return {
        "connected": True,
        "connections": len(conns),
        "version": c.version,
        "since": int(c.connected_at * 1000),
        "last_seen": int(c.last_seen * 1000),
        "installation_id": c.installation_id,
        "browser": c.browser,
        "browser_version": c.browser_version,
        "platform": c.platform,
        "mobile": c.mobile,
        "capabilities": dict(c.capabilities),
        "settings": dict(c.settings),
        "devices": [
            {
                "installation_id": item.installation_id,
                "browser": item.browser,
                "browser_version": item.browser_version,
                "platform": item.platform,
                "mobile": item.mobile,
                "version": item.version,
                "since": int(item.connected_at * 1000),
                "last_seen": int(item.last_seen * 1000),
                "capabilities": dict(item.capabilities),
                "settings": dict(item.settings),
            }
            for item in sorted(conns, key=lambda x: x.connected_at, reverse=True)
        ],
    }


async def control_connectors(user_id: str, action: str,
                             settings: Optional[dict] = None) -> int:
    """Send a lifecycle/config command to all of a user's live extensions."""
    conns = tuple(_connectors.get(user_id) or [])
    if not conns:
        return 0
    payload: Dict[str, Any] = {"type": "control", "action": action}
    if settings is not None:
        payload["settings"] = settings
    results = await asyncio.gather(
        *(conn._send(payload) for conn in conns), return_exceptions=True
    )
    return sum(not isinstance(result, Exception) for result in results)


async def stop_connectors(user_id: str) -> int:
    """Ask extensions to stop reconnecting and release them immediately."""
    conns = tuple(_connectors.get(user_id) or [])
    await control_connectors(user_id, "shutdown")
    for conn in conns:
        unregister(conn)
    return len(conns)


def handle_incoming(conn: ConnectorConn, msg: dict) -> None:
    """Route a frame the extension sent us: results resolve pending RPCs, hello
    records the version, ping just refreshes last-seen. Never raises."""
    conn.last_seen = time.time()
    try:
        mtype = msg.get("type")
        if mtype == "result":
            rpc_id = msg.get("id")
            if rpc_id:
                conn.resolve(rpc_id, msg)
        elif mtype == "hello":
            conn.version = str(msg.get("version") or conn.version)
            conn.installation_id = str(msg.get("installation_id") or conn.installation_id)
            conn.browser = str(msg.get("browser") or conn.browser)
            conn.browser_version = str(msg.get("browser_version") or conn.browser_version)
            conn.platform = str(msg.get("platform") or conn.platform)
            conn.mobile = bool(msg.get("mobile", conn.mobile))
            if isinstance(msg.get("capabilities"), dict):
                conn.capabilities = dict(msg["capabilities"])
            if isinstance(msg.get("settings"), dict):
                conn.settings = dict(msg["settings"])
        # "ping" needs nothing beyond the last_seen refresh above.
    except Exception:  # noqa: BLE001
        logger.debug("connector handle_incoming failed", exc_info=True)


async def connector_execute(user_id: str, bs_id: str, action: str,
                            params: Optional[dict] = None,
                            timeout: float = DEFAULT_CMD_TIMEOUT) -> dict:
    """Forward one ``browser_action`` to the user's extension and normalise the
    reply into the ``browser_action`` contract.

    On no connection / timeout this returns ``success=False`` with a clear error
    — NO silent fall-back to headless (predictable; the agent can switch backends
    explicitly).
    """
    conn = _pick(user_id, bs_id)
    if conn is None:
        return {"success": False, "error": "browser extension not connected",
                "url": "", "title": ""}

    reply = await conn.rpc(action, bs_id, params or {}, timeout)

    out: Dict[str, Any] = {
        "success": bool(reply.get("success")),
        "result": reply.get("result", ""),
        "url": reply.get("url", ""),
        "title": reply.get("title", ""),
    }
    if reply.get("error"):
        out["error"] = reply.get("error")

    # Screenshot: the extension returns a base64 PNG (optionally a data: URL).
    # Persist it into the owner's home exactly like the headless path does and
    # hand back a normal URL, so the agent's image pipeline is unchanged.
    if action == "screenshot" and out["success"]:
        saved = await _save_screenshot(user_id, reply.get("result", ""))
        if saved:
            out["result"] = saved
            out["mime_type"] = "image/png"
        else:
            out["success"] = False
            out["error"] = "could not save screenshot from extension"
    return out


async def _save_screenshot(user_id: str, data: str) -> Optional[str]:
    """Decode a base64 PNG from the extension into the owner's screenshots home
    and return its public URL — mirrors the headless screenshot path in
    app/tools/browser.py (per-user dir + public_url)."""
    if not data:
        return None
    try:
        b64 = data.split(",", 1)[1] if data.startswith("data:") else data
        raw = base64.b64decode(b64)
    except Exception:  # noqa: BLE001
        return None
    try:
        from app import user_workspace as _ws
        from app.encryption.workspace_files import write_encrypted_file
        fname = f"{uuid.uuid4().hex}.png"
        shots_dir = _ws.user_dir(user_id, "screenshots")
        await write_encrypted_file(user_id, shots_dir / fname, raw)
        return _ws.public_url(user_id, "screenshots", fname)
    except Exception:  # noqa: BLE001
        logger.debug("connector screenshot save failed", exc_info=True)
        return None

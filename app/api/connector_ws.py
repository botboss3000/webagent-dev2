"""
Browser CONNECTOR WebSocket — the server end of the browser-extension link.

A thin route over the engine in app/tools/browser_connector.py, exactly as
app/api/browser_stream.py is a thin route over app/tools/browser.py. A user
installs the WebAgent browser extension; it opens ONE authenticated WebSocket
here and waits for ``{type:"cmd"}`` frames, runs each command in the user's real
browser, and replies ``{type:"result"}``. The agent's ``browser_action``
forwards to this link whenever a session's backend is "connector" (see the
connector branch in app/tools/browser.py).

Auth mirrors the browser WS in browser_stream.py: a JWT in ``?token=`` (the
extension can't set headers on the WebSocket handshake from the background page),
with the open-mode tokenless fallback so a single-user / local box works without
minting a JWT. A command is only ever routed to the authenticated owner's own
connection, so the socket carries no cross-tenant risk.
"""

import asyncio
import logging

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Request
from fastapi.responses import JSONResponse

from app.auth.jwt import decode_token
from app.tools import browser_connector as _conn

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_token(token: str):
    """Resolve the caller's user_id from a JWT, else None. Same shape as
    browser_stream._verify_token."""
    if token:
        try:
            payload = decode_token(token)
        except Exception:
            payload = None
        if payload:
            uid = payload.get("user_id") or payload.get("sub")
            if uid:
                return uid
    return None


@router.websocket("/api/v1/connector/ws")
async def connector_ws(websocket: WebSocket):
    # Authenticate BEFORE accepting so an unauthenticated client never opens a link.
    token = websocket.query_params.get("token", "")
    user_id = _verify_token(token)
    if not user_id:
        await websocket.close(code=4401)
        return

    await websocket.accept()

    # One coroutine sends to the socket; guard it with a lock so concurrent
    # command dispatches to the same connection never interleave a frame.
    send_lock = asyncio.Lock()

    async def _send_json(msg: dict):
        async with send_lock:
            await websocket.send_json(msg)

    conn = _conn.ConnectorConn(
        user_id=user_id,
        send_json=_send_json,
        version=websocket.query_params.get("v", ""),
    )
    _conn.register(conn)
    try:
        await _send_json({"type": "welcome", "user_id": user_id})
    except Exception:
        pass

    try:
        while True:
            msg = await websocket.receive_json()
            _conn.handle_incoming(conn, msg)
    except WebSocketDisconnect:
        pass
    except Exception as e:  # noqa: BLE001
        logger.info("connector_ws closed: %s", e)
    finally:
        _conn.unregister(conn)


@router.get("/api/v1/connector/pair")
async def connector_pair(request: Request):
    """Mint a long-lived token the user pastes into the browser extension once.

    The connector WebSocket needs an identity. In every access mode EXCEPT
    'open' there is no tokenless shortcut, so the extension MUST present a JWT —
    but a non-technical user has no easy way to obtain one. This endpoint is the
    bridge: the user is already signed in to the app (so request_user_id resolves
    them here), and we hand back a JWT scoped to that same user with a long
    lifetime so they set the extension up once and it doesn't expire a day later.

    No new privilege is granted: the token carries the caller's own user_id, and
    a connector command is only ever routed to that user's own browser. Refused
    for anonymous / not-signed-in callers — they have no browser to lend.
    """
    from app.auth.identity import request_user_id, _is_anonymous
    uid = request_user_id(request)
    if _is_anonymous(uid):
        return JSONResponse(
            {"error": "sign_in_required",
             "message": "Sign in to the app first, then pair your browser extension."},
            status_code=401,
        )
    from app.auth.jwt import create_access_token
    # ~1 year — long enough to feel permanent, bounded so a leaked token expires.
    token = create_access_token(username=uid, user_id=uid, expires_minutes=60 * 24 * 365)
    return {"token": token, "user_id": uid}


@router.get("/api/v1/connector/status")
async def connector_status(request: Request):
    """Whether the calling user has a live browser-extension connection — drives
    the Browser page's status badge. Resolves the caller the standard way (header
    or ?token=, open-mode tokenless → bootstrap admin)."""
    from app.auth.identity import request_user_id
    uid = request_user_id(request)
    if not uid:
        return {"connected": False}
    return _conn.connector_info(uid)

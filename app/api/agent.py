"""
WebSocket endpoint — user event subscriber (receive-only).

Frontend connects once per user. Server broadcasts ALL agent events
(session responses, tool calls, pipeline steps) to the user's WebSocket.
The frontend filters which events to display where:
  - "stream" / "response" → chat bubbles (only for current session)
  - "tool_call" / "tool_result" / "pipeline" / "db" → stream/loop/flow panels
"""

import asyncio
import json
import logging
import datetime
from typing import Any
from websockets.exceptions import ConnectionClosedOK

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.events.delivery import register_user_listener, unregister_user_listener, SEND_TIMEOUT
from app.agent.run_buffer import get_registry as get_run_buffer_registry

logger = logging.getLogger(__name__)

router = APIRouter()


def _json_default(obj: Any) -> Any:
    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    # Handle common agent objects
    if hasattr(obj, 'to_dict'):
        return obj.to_dict()
    if hasattr(obj, '__dict__'):
        return obj.__dict__
    raise TypeError(f"Object of type {obj.__class__.__name__} is not JSON serializable")


@router.websocket("/api/v1/agent/ws")
async def agent_websocket(websocket: WebSocket):
    """
    Per-user event subscriber WebSocket.

    Client sends on connect:
      {
        "mode": "user_subscriber",
        "user_id": "uuid-here",
        "token": "<jwt>",
        // OPTIONAL — resume any in-flight runs the client knows about.
        // Map of session_id -> last_session_seq the client has already seen.
        // Server replays buffered events with session_seq > that value
        // BEFORE switching to live streaming.
        "resume": { "<session_id>": <int_seq>, ... }
      }

    Server replies with heartbeat pings and streams all agent events
    (stream, response, tool_call, tool_result, pipeline, db, etc.)
    for ALL sessions belonging to that user.
    """
    await websocket.accept()

    user_id: str | None = None
    HEARTBEAT_INTERVAL = 25  # seconds

    async def _heartbeat():
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                try:
                    await asyncio.wait_for(
                        websocket.send_text(json.dumps({"type": "ping"}, default=_json_default)),
                        timeout=SEND_TIMEOUT,
                    )
                except (WebSocketDisconnect, ConnectionClosedOK):
                    break
                except Exception:
                    break
        except asyncio.CancelledError:
            pass

    heartbeat_task = asyncio.create_task(_heartbeat())

    try:
        # ── Wait for initial handshake ──
        raw = await websocket.receive_text()
        data = json.loads(raw)

        mode = data.get("mode", "").strip()
        claimed_user_id = data.get("user_id", "").strip()
        token = (data.get("token") or "").strip()
        device_id = ""

        if mode != "user_subscriber" or not claimed_user_id:
            await websocket.send_text(json.dumps({
                "type": "error",
                "message": "First message must be {\"mode\": \"user_subscriber\", \"user_id\": \"<uuid>\", \"token\": \"<jwt>\"}",
            }, default=_json_default))
            return

        # The WebSocket endpoint is exempted from the HTTP auth middleware
        # (`PUBLIC_PREFIXES` in app/auth/middleware.py), so we have to verify
        # the caller's identity ourselves here. Without this, an authenticated
        # user A could subscribe with user_id=B and start receiving every
        # event broadcast for user B — full cross-tenant leak. The handshake
        # MUST carry a JWT whose subject matches `claimed_user_id`.
        #
        # Exception: in "open" access mode (single-user / local convenience),
        # there is only one user so no cross-tenant risk exists. This allows
        # the subscriber to work over Cloudflare Tunnel etc. where the
        # frontend cannot mint a fresh server-side JWT (open-login rejects
        # non-localhost requests).
        from app.admin.settings import get_access_mode
        if get_access_mode() == "open":
            user_id = claimed_user_id
        else:
            from app.auth.identity import verify_token_matches_user
            verified = verify_token_matches_user(token, claimed_user_id)
            if not verified:
                await websocket.send_text(json.dumps({
                    "type": "error",
                    "message": "Invalid or missing token, or token subject does not match user_id",
                }, default=_json_default))
                return
            user_id = verified
            from app.auth.jwt import decode_signed_token, decode_token
            token_payload = decode_token(token) or {}
            device_id = str((decode_signed_token(token) or {}).get("device_id") or "")
            if str(user_id).startswith("anon_"):
                from app.api.rate_limit import client_source_hash
                if (
                    token_payload.get("anon_admission") is not True
                    or token_payload.get("anon_source") != client_source_hash(websocket)
                ):
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "code": "anonymous_admission_required",
                        "message": "Refresh this guest session or register and sign in to continue.",
                    }, default=_json_default))
                    await websocket.close(code=4401, reason="Anonymous admission required")
                    return

        # User BYOD: pin data-plane DB routing to this verified user for the
        # whole WS connection, so pre-loop reads (replay/reconcile) and the turn
        # itself resolve to the user's own database. No-op in single-tenant mode.
        try:
            from app.auth.identity import set_verified_caller_uid
            set_verified_caller_uid(user_id)
        except Exception:  # noqa: BLE001
            pass

        # Register as a per-user listener — receives ALL events for this user
        register_user_listener(user_id, websocket, device_id=device_id)
        logger.info(f"User subscriber registered for user {user_id}")

        # ── Replay buffered events from active in-flight runs ──
        # On (re)connect we ALWAYS try to replay events for any session this
        # user owns that has a live or recently-completed RunBuffer. The
        # client's optional `resume` map gives us per-session "last seq I
        # already saw" so we can skip events the client already rendered;
        # any session NOT in the map gets replayed from seq=0 (the client
        # has nothing locally — common case after a hard refresh).
        resume_map = data.get("resume") or {}
        if not isinstance(resume_map, dict):
            resume_map = {}
        replayed_summary: dict = {}
        try:
            reg = get_run_buffer_registry()
            # Union of (a) sessions the client asked about and (b) sessions
            # the server is currently buffering for this user.
            candidate_sids = set(k for k in resume_map.keys() if isinstance(k, str))
            for sid, buf in reg._buffers.items():
                if buf.user_id == user_id:
                    candidate_sids.add(sid)
            for sid in candidate_sids:
                buf = reg.get(sid)
                if buf is None:
                    continue
                if buf.user_id and buf.user_id != user_id:
                    # Tenancy guard.
                    continue
                try:
                    last_seq_int = int(resume_map.get(sid, 0) or 0)
                except (TypeError, ValueError):
                    last_seq_int = 0
                missed = buf.replay_after(last_seq_int)
                if not missed:
                    continue
                replayed_summary[sid] = len(missed)
                for ev in missed:
                    try:
                        await asyncio.wait_for(websocket.send_text(json.dumps(
                            # Always include session_id on replayed events so
                            # the client can filter/route them; live events
                            # already carry it via _emit_to_visualizers.
                            {**ev, "replayed": True, "session_id": sid},
                            default=_json_default,
                        )), timeout=SEND_TIMEOUT)
                    except Exception:
                        logger.warning(
                            "User subscriber replay stalled for %s — evicting socket", sid[:12])
                        try:
                            await websocket.close()
                        except Exception:
                            pass
                        return
        except Exception as _re:
            logger.warning("Resume replay failed for user %s: %s", user_id, _re)

        # Inform the client about every active session, not only sessions that
        # currently have replayable stream events buffered. Tool/model work can
        # be in progress between emitted events, in which case its RunBuffer may
        # be absent while the durable run-state row is still `running`.
        active_sessions: set[str] = set()
        try:
            active_sessions.update(
                sid for sid, b in get_run_buffer_registry()._buffers.items()
                if b.completed_at is None and b.user_id == user_id
            )
        except Exception:
            pass
        try:
            from app.db import get_db
            active_sessions.update(
                str(row["session_id"])
                for row in await get_db().run_state_list_active(user_id)
                if row.get("session_id")
            )
        except Exception:
            logger.warning("Could not read active run state for user %s", user_id)
        # A run stopped involuntarily is marked terminal before the watchdog or
        # boot sweep re-ignites it. Report those rows as active too, so a browser
        # that reconnects mid-recovery still gets the session spinner and the
        # open chat's "re-assessing…" activity bar instead of a settled transcript.
        try:
            from app.agent.runner import recovery_pending
            active_sessions.update(
                str(row["session_id"])
                for row in await get_db().run_state_list_resumable()
                if row.get("session_id")
                and str(row.get("user_id") or "") == str(user_id)
                and recovery_pending(row)
            )
        except Exception:
            pass
        active_sessions = sorted(active_sessions)

        await websocket.send_text(json.dumps({
            "type": "subscribed",
            "user_id": user_id,
            "message": f"Receiving events for user {user_id}",
            "replayed": replayed_summary,
            "active_sessions": active_sessions,
        }, default=_json_default))

        # Flight-recorder: WebSocket subscribe (connect) into the `ws` category.
        try:
            from app.agent.diagnostics import record_ws_event
            record_ws_event("subscribe", user_id=user_id, detail={
                "replayed": replayed_summary, "active_sessions": active_sessions,
                "client": (websocket.client.host if websocket.client else None),
            })
        except Exception:
            pass

        # ── Stay connected until client disconnects ──
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")
            if mtype == "disconnect":
                break
            # Client can also request mid-session replay (e.g. when the user
            # switches back into a session it previously left). Payload:
            #   {"type": "resume", "session_id": "...", "last_session_seq": N}
            if mtype == "resume":
                sid = data.get("session_id")
                try:
                    last_seq_int = int(data.get("last_session_seq") or 0)
                except (TypeError, ValueError):
                    last_seq_int = 0
                if isinstance(sid, str):
                    try:
                        reg = get_run_buffer_registry()
                        buf = reg.get(sid)
                        if buf is not None and (not buf.user_id or buf.user_id == user_id):
                            for ev in buf.replay_after(last_seq_int):
                                try:
                                    await asyncio.wait_for(websocket.send_text(json.dumps(
                                        {**ev, "replayed": True},
                                        default=_json_default,
                                    )), timeout=SEND_TIMEOUT)
                                except Exception:
                                    logger.warning(
                                        "User subscriber resume replay stalled for %s — evicting socket",
                                        sid[:12])
                                    try:
                                        await websocket.close()
                                    except Exception:
                                        pass
                                    return
                    except Exception as _re2:
                        logger.warning("Mid-session resume failed: %s", _re2)

    except (WebSocketDisconnect, ConnectionClosedOK):
        logger.info(f"User subscriber [{user_id}]: disconnected")
        try:
            await websocket.close()
        except Exception:
            pass
    except json.JSONDecodeError:
        try:
            await websocket.send_text(json.dumps({
                "type": "error", "message": "Invalid JSON",
            }, default=_json_default))
        except Exception:
            pass
    except Exception as e:
        logger.error(f"User subscriber error [{user_id}]: {e}", exc_info=True)
    finally:
        if user_id:
            unregister_user_listener(user_id, websocket)
            try:
                from app.agent.diagnostics import record_ws_event
                record_ws_event("disconnect", user_id=user_id)
            except Exception:
                pass
        if heartbeat_task and not heartbeat_task.done():
            heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        logger.info(f"User subscriber [{user_id}]: cleaned up")

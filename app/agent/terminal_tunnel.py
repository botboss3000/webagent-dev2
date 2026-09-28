"""Terminal tunnel — let the USER drive a terminal program through chat.

Terminal *control* (plugins/abilities/Administrator/terminal_control.py) lets the
agent drive a terminal.
This is the mirror image: a **tunnel** binds a chat session to a live terminal so
the human's chat messages become keystrokes for that program and the program's
output streams back into the chat. While a tunnel is active the agent steps
aside entirely — the chat loop is bypassed.

Two ways in (both supported):

  * **Agent-mediated** — an agent with the ``terminal_control`` ability opens a
    program and calls the ``terminal_tunnel`` tool to hand the wheel to the user
    (e.g. "start claude and let me drive it"). The agent is the middle-man.
  * **Template / full tunnel** — a one-click "Terminal Tunnel" launch opens a
    command and tunnels immediately, with no agent reasoning at all.

Everything typed/streamed during a tunnel **is persisted** to the session
transcript, but tagged with ``TUNNEL_SOURCE`` so the LLM-context builder
(app/agent/session_history.py) keeps it OUT of the agent's context window. That
"in the record, out of the live context" tag is the seed of the coming
context-compaction feature.

Responsibilities split cleanly:

  * **Display** is handled by the browser's embedded read-only terminal, which
    subscribes to the existing terminal WebSocket for the bound session — so a
    full-screen TUI (Claude Code) renders properly, with scrollback + reconnect
    for free.
  * **Record** is this module's pump: it subscribes to the same PTY output
    broadcast, coalesces frames, and persists them as tunnel-tagged rows so a
    reloaded chat shows the terminal history inline.
  * **Control** events (``tunnel_state``) are emitted on the chat WebSocket so
    the UI mounts/unmounts the banner + embedded terminal.
  * **Input** flows in via the chat composer (routed by ``route_user_line``) and
    the key palette (``send_keys``).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Rows written for tunnel traffic carry this `source`. The context builder skips
# rows with this source, so tunnel chatter is persisted (visible on reload,
# auditable) but never enters the agent's LLM context. Imported by
# app/agent/session_history.py for the skip, and by app/api/chat.py for tagging.
TUNNEL_SOURCE = "terminal_tunnel"

# Key under sessions.metadata that holds the tunnel binding (mirrors how
# "active_skills" / "active_tools" live in session metadata).
TUNNEL_META_KEY = "tunnel"

# Coalescing thresholds for the record pump — flush accumulated output to one
# transcript row once it reaches this many bytes OR after this many idle seconds.
_FLUSH_BYTES = 8 * 1024
_FLUSH_IDLE_SECS = 1.5

# Live record pumps, keyed by chat session id. One per tunnelled chat session.
_pumps: Dict[str, "TunnelPump"] = {}
_pumps_lock = asyncio.Lock()


# ── Live-event emit (chat WebSocket) ─────────────────────────────────────────

async def _emit(chat_session_id: str, user_id: str, event: Dict[str, Any]) -> None:
    """Push a control event to the chat session's listeners. Lazy import keeps
    this module free of an app.api.chat import cycle."""
    try:
        from app.api.chat import _emit_to_visualizers
        await _emit_to_visualizers(chat_session_id, event, user_id=user_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("tunnel emit failed for %s: %s", chat_session_id, e)


def _state_event(state: str, chat_session_id: str, cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "type": "tunnel_state",
        "state": state,                       # "active" | "ended"
        "session_id": chat_session_id,
        "terminal_session_id": cfg.get("terminal_session_id"),
        "command": cfg.get("command", ""),
        "name": cfg.get("name", ""),
        "mediated": cfg.get("mediated", False),
        "reason": cfg.get("reason", ""),
    }


# ── Record pump ──────────────────────────────────────────────────────────────

class TunnelPump:
    """Subscribes to a terminal session's output broadcast and persists coalesced
    frames as tunnel-tagged transcript rows. Display is the browser's job; this
    is purely the durable record (so a reloaded chat shows terminal history)."""

    def __init__(self, chat_session_id: str, terminal_session_id: str, user_id: str):
        self.chat_session_id = chat_session_id
        self.terminal_session_id = terminal_session_id
        self.user_id = user_id
        self._task: Optional[asyncio.Task] = None
        self._buf = bytearray()
        self._stopped = False

    async def start(self) -> None:
        self._task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    async def _run(self) -> None:
        from app.api import terminal as _term
        sess = await _term.get_owned_session(self.terminal_session_id, self.user_id)
        if sess is None:
            return
        queue, _snapshot = await sess.subscribe()
        try:
            while not self._stopped:
                try:
                    data = await asyncio.wait_for(queue.get(), timeout=_FLUSH_IDLE_SECS)
                except asyncio.TimeoutError:
                    await self._flush()          # idle — commit what we have
                    continue
                if data is None:                 # child process exited
                    await self._flush()
                    return
                if not data:
                    continue
                self._buf.extend(data)
                if len(self._buf) >= _FLUSH_BYTES:
                    await self._flush()
        except asyncio.CancelledError:
            pass
        finally:
            try:
                sess.unsubscribe(queue)
            except Exception:
                pass
            await self._flush()

    async def _flush(self) -> None:
        if not self._buf:
            return
        raw = bytes(self._buf)
        self._buf.clear()
        # Strip ANSI for the stored text (the live display keeps the raw bytes).
        try:
            from app.api.terminal import _strip_ansi
            text = _strip_ansi(raw.decode("utf-8", errors="replace"))
        except Exception:
            text = raw.decode("utf-8", errors="replace")
        text = text.strip("\r\n")
        if not text:
            return
        try:
            db = _get_db()
            await db.insert_interaction(
                self.user_id, self.chat_session_id, role="tool",
                content=text, tool_name="terminal_output",
                channel="web_portal", source=TUNNEL_SOURCE,
                metadata=json.dumps({"terminal_session_id": self.terminal_session_id}),
            )
        except Exception as e:
            logger.debug("tunnel record flush failed: %s", e)


def _get_db():
    from app.db import get_db
    return get_db()


# ── Public API ───────────────────────────────────────────────────────────────

async def get_tunnel_cfg(db, chat_session_id: str) -> Dict[str, Any]:
    """Return the tunnel binding for a chat session, or {} if none/disabled."""
    if not chat_session_id:
        return {}
    try:
        cfg = await db.get_session_tunnel(chat_session_id)
    except Exception:
        return {}
    return cfg if isinstance(cfg, dict) and cfg.get("enabled") else {}


async def enable_tunnel(
    db, user_id: str, chat_session_id: str, terminal_session_id: str,
    *, mediated: bool = True, command: str = "",
) -> Dict[str, Any]:
    """Bind `chat_session_id` to `terminal_session_id`: from now on the user's
    chat messages are keystrokes for that program and its output streams into
    the chat. Starts the record pump and announces the state to the UI.

    `mediated` marks an agent-initiated tunnel (vs. a full template tunnel) —
    purely informational; both kinds are persisted and context-excluded the
    same way."""
    from app.api import terminal as _term
    sess = await _term.get_owned_session(terminal_session_id, user_id)
    if sess is None:
        raise ValueError("terminal session not found (it may have closed)")

    cfg = {
        "enabled": True,
        "terminal_session_id": terminal_session_id,
        "mediated": bool(mediated),
        "command": (command or sess.launch_command or "").strip(),
        "name": sess.name or "",
        "bound_at": time.time(),
    }
    await db.set_session_tunnel(chat_session_id, cfg)

    async with _pumps_lock:
        old = _pumps.pop(chat_session_id, None)
        if old is not None:
            await old.stop()
        pump = TunnelPump(chat_session_id, terminal_session_id, user_id)
        _pumps[chat_session_id] = pump
        await pump.start()

    await _emit(chat_session_id, user_id, _state_event("active", chat_session_id, cfg))
    return cfg


async def disable_tunnel(
    db, user_id: str, chat_session_id: str, *, reason: str = "handed back",
) -> bool:
    """Unbind the chat session — control returns to the agent. Stops the pump,
    clears the binding, and tells the UI to tear down the banner/terminal."""
    async with _pumps_lock:
        pump = _pumps.pop(chat_session_id, None)
    if pump is not None:
        await pump.stop()

    cfg = {}
    try:
        cfg = await db.get_session_tunnel(chat_session_id) or {}
    except Exception:
        pass
    try:
        await db.set_session_tunnel(chat_session_id, None)
    except Exception as e:
        logger.debug("clear tunnel meta failed: %s", e)

    cfg = dict(cfg)
    cfg["reason"] = reason
    await _emit(chat_session_id, user_id, _state_event("ended", chat_session_id, cfg))
    return True


async def route_user_line(db, user_id: str, chat_session_id: str, text: str) -> bool:
    """If this chat is tunnelled, persist the line (tunnel-tagged, so it stays
    out of the agent's context) and type it into the bound program. Returns True
    if the line was handled here (caller must NOT run the agent loop), False if
    the session isn't tunnelled (caller proceeds normally)."""
    cfg = await get_tunnel_cfg(db, chat_session_id)
    if not cfg:
        return False
    terminal_session_id = cfg.get("terminal_session_id") or ""

    # Persist the keystroke line for the record / compaction, tagged so the
    # context builder skips it. Best-effort — never block input on a DB hiccup.
    try:
        await db.insert_interaction(
            user_id, chat_session_id, role="user", content=text,
            channel="web_portal", source=TUNNEL_SOURCE,
            metadata=json.dumps({"terminal_session_id": terminal_session_id}),
        )
    except Exception as e:
        logger.debug("tunnel input persist failed: %s", e)

    from app.api import terminal as _term
    sess = await _term.get_owned_session(terminal_session_id, user_id)
    if sess is None or sess.ended:
        await disable_tunnel(db, user_id, chat_session_id, reason="terminal closed")
        return True
    # The pause lock guards against the AGENT fighting a human; the tunnel user
    # IS the human, so we type regardless of pause.
    if text:
        sess.send_text(text, enter=True)
    else:
        sess.send_keys(["enter"])
    return True


async def send_keys(
    db, user_id: str, chat_session_id: str, keys: List[str],
) -> Tuple[bool, Any]:
    """Send named special keys (Enter, Esc, Ctrl+C, arrows, …) from the key
    palette into the bound program. Returns (ok, unknown_keys_or_error)."""
    cfg = await get_tunnel_cfg(db, chat_session_id)
    if not cfg:
        return False, "tunnel not active"
    from app.api import terminal as _term
    sess = await _term.get_owned_session(cfg.get("terminal_session_id") or "", user_id)
    if sess is None or sess.ended:
        await disable_tunnel(db, user_id, chat_session_id, reason="terminal closed")
        return False, "terminal closed"
    unknown = sess.send_keys([str(k) for k in (keys or [])])
    return True, unknown

"""
Terminal Chat engine — drives a terminal program (Claude Code, shell, etc.) and
streams its raw output into WebAgent's chat panel as a live xterm.js view.

An agent whose record carries ``metadata.engine = "terminal_chat"`` runs its
whole turn here instead of WebAgent's LLM loop. We spawn a persistent PTY
(via the same session registry the browser terminal uses), pipe the user's
chat messages to its stdin, and stream its stdout back as terminal events.

The frontend detects this engine and swaps the chat bubble area for an xterm.js
terminal overlay — same pill, same session, same header, just a terminal where
the bubbles would be.

Architecture:
  Chat pill → POST /api/v1/chat/send → _prepare_send → loop.py
    → stream_agent_events → engine dispatch → terminal_chat.stream()
      → PTY stdin
  PTY stdout → subscriber queue → terminal_stream events → chat WS
    → frontend xterm.js

The PTY session is persistent across turns (stored in chat session metadata),
so the program keeps running between messages. On reload, the frontend
reconnects to the terminal WS and gets the scrollback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, AsyncGenerator, Dict, Optional

logger = logging.getLogger(__name__)

ENGINE_ID = "terminal_chat"

# How long to wait for more PTY output before considering the program "settled"
# and ending the turn. The program may still be running — the next pill message
# will wake the engine again.
_SETTLE_SECS = 2.0

# Max bytes to accumulate before yielding a terminal_stream chunk (keeps the
# frontend feeling live without spamming tiny events).
_CHUNK_BYTES = 512

# Key in chat session metadata that stores the terminal session id.
_META_KEY = "terminal_chat_session_id"


# ── PTY helpers (delegates to the shared terminal session registry) ──────────

async def _get_or_create_pty(chat_session_id: str, user_id: str,
                             command: str = "") -> str:
    """Get the existing terminal session for this chat, or spawn a new one.
    Returns the terminal session id."""
    from app.api.terminal import get_or_create_session
    import uuid

    tsid = await _get_stored_tsid(chat_session_id)
    if tsid:
        # Reattach — the session may still be alive.
        try:
            sess = await get_or_create_session(tsid, user_id)
            if sess.is_alive:
                return tsid
        except Exception:
            pass

    # Spawn a fresh PTY.
    tsid = uuid.uuid4().hex
    sess = await get_or_create_session(tsid, user_id)
    if command:
        sess.write_input((command + "\n").encode("utf-8"))
    await _store_tsid(chat_session_id, tsid)
    return tsid


async def _get_stored_tsid(chat_session_id: str) -> Optional[str]:
    """Read the terminal session id from the chat session's metadata."""
    try:
        from app.db import get_db
        db = get_db()
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id=?",
                (chat_session_id,),
            ).fetchone()
            if row and row[0]:
                meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                return (meta or {}).get(_META_KEY)
        finally:
            conn.close()
    except Exception as e:
        logger.debug("terminal_chat: could not read stored tsid: %s", e)
    return None


async def _store_tsid(chat_session_id: str, tsid: str) -> None:
    """Persist the terminal session id in the chat session's metadata."""
    try:
        from app.db import get_db
        db = get_db()
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM sessions WHERE id=?",
                (chat_session_id,),
            ).fetchone()
            meta = {}
            if row and row[0]:
                meta = json.loads(row[0]) if isinstance(row[0], str) else row[0]
                if not isinstance(meta, dict):
                    meta = {}
            meta[_META_KEY] = tsid
            conn.execute(
                "UPDATE sessions SET metadata=? WHERE id=?",
                (json.dumps(meta), chat_session_id),
            )
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        logger.debug("terminal_chat: could not store tsid: %s", e)


async def _subscribe(tsid: str) -> "asyncio.Queue":
    """Subscribe to a terminal session's output broadcast. Returns a queue that
    receives bytes chunks (and a None sentinel when the session ends)."""
    from app.api.terminal import _sessions, _sessions_lock

    async with _sessions_lock:
        sess = _sessions.get(tsid)
        if sess is None:
            q: asyncio.Queue = asyncio.Queue()
            await q.put(None)
            return q
        q: asyncio.Queue = asyncio.Queue()
        sess._subscribers.add(q)
        # Replay scrollback so the subscriber sees what's already on screen.
        sb = sess.get_scrollback()
        if sb:
            await q.put(sb)
        return q


def _unsubscribe(tsid: str, q: "asyncio.Queue") -> None:
    """Remove a subscriber queue from a terminal session."""
    try:
        from app.api.terminal import _sessions, _sessions_lock
        # Best-effort — the session may already be gone.
        sess = _sessions.get(tsid)
        if sess is not None:
            sess._subscribers.discard(q)
    except Exception:
        pass


# ── Config ──────────────────────────────────────────────────────────────────

def _read_cfg(agent_rec: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Pull the terminal_chat config block out of the agent's metadata."""
    meta: Dict[str, Any] = {}
    if agent_rec:
        raw = agent_rec.get("metadata")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw or "{}")
            except (TypeError, ValueError):
                meta = {}
        elif isinstance(raw, dict):
            meta = raw
    tc = meta.get("terminal_chat") if isinstance(meta.get("terminal_chat"), dict) else {}
    return {
        "command": str(tc.get("command") or "").strip(),
        "cwd": str(tc.get("cwd") or "").strip(),
    }


# ── The engine ──────────────────────────────────────────────────────────────

async def stream(
    *,
    user_id: str,
    session_id: str,
    agent_id: str,
    user_message: Any,
    agent_rec: Optional[Dict[str, Any]] = None,
    db: Any = None,
    system_prompt: str = "",
    channel: Optional[str] = None,
    parent_interaction_id: Optional[str] = None,
    interrupt_event: Optional[asyncio.Event] = None,
    attachment_docs: Optional[list] = None,
    execution_mode: str = "ask",
    **_ignored: Any,
) -> AsyncGenerator[Dict[str, Any], None]:
    """Run one chat turn through a live terminal, yielding terminal_stream events
    and a terminal_chat_state event so the frontend mounts xterm.js."""

    # 1. Admin gate.
    try:
        is_admin = await db.is_user_admin(user_id)
    except Exception:
        is_admin = False
    if not is_admin:
        yield {"type": "response", "level": "agent",
               "content": "**Terminal Chat** agents are admin-only."}
        return

    # 2. Resolve config.
    cfg = _read_cfg(agent_rec)
    command = cfg["command"]
    cwd = cfg["cwd"]

    # 3. Coerce the user message to plain text.
    if isinstance(user_message, str):
        text = user_message.strip()
    elif isinstance(user_message, list):
        parts = []
        for block in user_message:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        text = "\n".join(p for p in parts if p).strip()
    elif isinstance(user_message, dict):
        text = str(user_message.get("text") or user_message.get("content") or "").strip()
    else:
        text = str(user_message or "").strip()

    if not text:
        yield {"type": "response", "level": "agent",
               "content": "I didn't get any message text to send to the terminal."}
        return

    # 4. Get or create the persistent PTY.
    tsid = await _get_or_create_pty(session_id, user_id, command=command)

    # 5. Emit the terminal_chat_state event so the frontend knows to mount xterm.js.
    yield {
        "type": "terminal_chat_state",
        "state": "active",
        "terminal_session_id": tsid,
        "command": command,
    }

    # 6. Subscribe to PTY output BEFORE writing input (so we don't miss output).
    q = await _subscribe(tsid)

    # 7. Write the user's message to PTY stdin.
    try:
        from app.api.terminal import _sessions, _sessions_lock
        async with _sessions_lock:
            sess = _sessions.get(tsid)
        if sess and sess.is_alive:
            # Send the text as a line of input.
            sess.write_input((text + "\n").encode("utf-8"))
        else:
            yield {"type": "terminal_stream", "data": "\r\n[Terminal session ended — send another message to restart]\r\n"}
            _unsubscribe(tsid, q)
            return
    except Exception as e:
        yield {"type": "terminal_stream", "data": f"\r\n[Error writing to terminal: {e}]\r\n"}
        _unsubscribe(tsid, q)
        return

    # 8. Drain PTY output, yielding terminal_stream chunks.
    try:
        buf = ""
        last_output = time.monotonic()

        while True:
            # Check for interrupt (stop button).
            if interrupt_event is not None and interrupt_event.is_set():
                yield {"type": "terminal_stream", "data": "\r\n[Interrupted]\r\n"}
                break

            try:
                chunk = await asyncio.wait_for(q.get(), timeout=0.3)
            except asyncio.TimeoutError:
                # No new output — check if we've settled.
                if buf:
                    # Flush any remaining buffer.
                    yield {"type": "terminal_stream", "data": buf}
                    buf = ""
                if time.monotonic() - last_output > _SETTLE_SECS:
                    break
                continue

            if chunk is None:
                # PTY ended.
                if buf:
                    yield {"type": "terminal_stream", "data": buf}
                yield {"type": "terminal_stream", "data": "\r\n[Program exited]\r\n"}
                break

            # Decode bytes to string (keep ANSI intact).
            try:
                text_chunk = chunk.decode("utf-8", errors="replace")
            except Exception:
                text_chunk = str(chunk)

            buf += text_chunk
            last_output = time.monotonic()

            # Yield chunks to keep the frontend feeling live.
            while len(buf) >= _CHUNK_BYTES:
                yield {"type": "terminal_stream", "data": buf[:_CHUNK_BYTES]}
                buf = buf[_CHUNK_BYTES:]

        # Flush any remaining buffer.
        if buf:
            yield {"type": "terminal_stream", "data": buf}

    finally:
        _unsubscribe(tsid, q)

    # 9. Signal the turn is done (so the frontend knows the program is waiting).
    yield {"type": "terminal_step_end"}
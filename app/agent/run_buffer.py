"""
In-memory per-session run buffer for live WS/SSE replay.

When a chat turn begins, a RunBuffer is created for the session. Every
event emitted during the turn (stream chunks, tool calls, tool results,
pipeline steps) is appended to the buffer and stamped with monotonic
session_seq / turn_seq counters. WebSocket subscribers can replay
events newer than a known session_seq on reconnect (refresh, session
switch back) without reading from the DB.

Buffer retention: after a turn completes, the buffer stays in memory for
`stream_buffer_retention_seconds` (default 60s, configurable via
app-settings.json) so a refresh right after the turn ends still gets a
RAM replay. A periodic sweeper drops stale buffers.

DB persistence is independent — handled by the existing insert_interaction
calls in app/agent/loop.py and app/api/chat.py. This module only
provides the replay buffer + seq counters that those callers can stamp
their rows with.
"""

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_SECONDS = 60
SWEEP_INTERVAL_SECONDS = 30
MAX_EVENTS_PER_BUFFER = 5000  # safety cap — protects against runaway loops
SETTINGS_FILE = "data/config/app-settings.json"


def _read_retention_seconds() -> int:
    """Read stream_buffer_retention_seconds.

    A value in data/config/debug-config.json (the consolidated debug file) wins;
    otherwise fall back to data/config/app-settings.json, then the default.
    """
    try:
        from app.admin.debug_config import debug_overrides
        ov = debug_overrides().get("stream_buffer_retention_seconds")
        if isinstance(ov, int) and ov >= 0:
            return ov
    except Exception:
        pass
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f) or {}
            v = s.get("stream_buffer_retention_seconds")
            if isinstance(v, int) and v >= 0:
                return v
    except Exception as e:
        logger.debug("Failed to read retention from %s: %s", SETTINGS_FILE, e)
    return DEFAULT_RETENTION_SECONDS


class RunBuffer:
    """One per active or recently-completed turn within a session."""

    def __init__(self, session_id: str, user_id: str, turn_id: str,
                 starting_seq: int):
        self.session_id = session_id
        self.user_id = user_id
        self.turn_id = turn_id
        self._session_seq = starting_seq  # next value to hand out
        self._turn_seq = 0
        self.events: List[Dict[str, Any]] = []
        self.completed_at: Optional[float] = None
        self.created_at: float = time.time()

    @property
    def starting_seq(self) -> int:
        """The session_seq value the FIRST event of this turn received."""
        return self.events[0].get("session_seq") if self.events else self._session_seq

    @property
    def latest_seq(self) -> int:
        """Highest session_seq stamped so far (0 if no events)."""
        return self._session_seq - 1 if self._session_seq else 0

    def next_seq(self) -> Tuple[int, int]:
        """Allocate (session_seq, turn_seq) for the next row or event."""
        ss = self._session_seq
        ts = self._turn_seq
        self._session_seq += 1
        self._turn_seq += 1
        return ss, ts

    def stamp_event(self, event: Dict[str, Any]) -> Dict[str, Any]:
        """Append session_seq/turn_id/turn_seq/emit_time to event and store.

        Returns the same dict object (mutated). Caller broadcasts the stamped
        event to clients so they can update their last-seen seq pointer.
        """
        if len(self.events) >= MAX_EVENTS_PER_BUFFER:
            # Drop oldest to bound memory under runaway loops.
            self.events = self.events[-(MAX_EVENTS_PER_BUFFER // 2):]
            logger.warning(
                "RunBuffer %s exceeded %d events — dropped oldest half",
                self.session_id, MAX_EVENTS_PER_BUFFER,
            )
        ss, ts = self.next_seq()
        # ``event_seq`` is the transport/replay cursor. Keep ``session_seq`` as
        # a compatibility alias for older clients; transcript placement uses
        # the explicit ``interaction_seq`` attached to persisted-row events.
        event["event_seq"] = ss
        event["session_seq"] = ss
        event["turn_id"] = self.turn_id
        event["turn_seq"] = ts
        event.setdefault("emit_time", datetime.now(timezone.utc).isoformat())
        self.events.append(event)
        return event

    def mark_completed(self) -> None:
        self.completed_at = time.time()

    def replay_after(self, after_session_seq: int) -> List[Dict[str, Any]]:
        """Return events with session_seq > after_session_seq, in order."""
        return [e for e in self.events
                if (e.get("session_seq") or 0) > after_session_seq]


class RunBufferRegistry:
    """Global per-session registry of RunBuffers."""

    def __init__(self):
        self._buffers: Dict[str, RunBuffer] = {}
        self._lock = asyncio.Lock()
        self._sweeper_task: Optional[asyncio.Task] = None

    async def start_turn(
        self,
        session_id: str,
        user_id: str,
        turn_id: str,
        db: Any,
    ) -> RunBuffer:
        """Create a new buffer for a new turn.

        If a buffer already exists for the session (e.g. previous turn's
        retention window hasn't expired), the new buffer continues from
        the previous session_seq counter to avoid collisions.
        """
        async with self._lock:
            prev = self._buffers.get(session_id)
            if prev is not None and prev.completed_at is None:
                logger.warning(
                    "start_turn for session %s but previous buffer still active; "
                    "force-completing previous", session_id,
                )
                prev.mark_completed()

            if prev is not None:
                starting = prev._session_seq
            else:
                try:
                    starting = await db.next_session_seq(session_id)
                except Exception as e:
                    logger.debug("next_session_seq bootstrap failed: %s", e)
                    starting = 1

            buf = RunBuffer(session_id, user_id, turn_id, starting)
            self._buffers[session_id] = buf
            self._ensure_sweeper()
            return buf

    def get(self, session_id: str) -> Optional[RunBuffer]:
        return self._buffers.get(session_id)

    async def reserve_external_seq(self, session_id: str, db: Any) -> int:
        """Reserve a durable-row sequence without racing a retained buffer.

        Recovery notices are written outside the normal streaming path. When a
        live (or retained) buffer exists, its counter may already be ahead of
        the durable manifest, so allocating directly from the database can
        re-issue a sequence that the buffer has reserved. Consume the buffer's
        next slot in that case; otherwise use the normal durable allocator.
        """
        async with self._lock:
            buf = self._buffers.get(session_id)
            if buf is not None:
                seq, _ = buf.next_seq()
                try:
                    await db.persist_session_manifest_seq(session_id, buf._session_seq)
                except Exception:
                    logger.debug("Failed to persist external seq for session %s", session_id[:12])
                return seq
        return await db.next_session_seq(session_id)

    async def end_turn(self, session_id: str, db: Any = None) -> None:
        """Mark the active buffer for session as completed (starts retention timer).

        When ``db`` is supplied, also persists the buffer's high-water mark into
        session_manifests so the manifest never goes stale, AND stamps the durable
        run-state's ``latest_session_seq`` so a reloaded / cold-device WS resume
        cursor is accurate.  Without this a server restart (or buffer sweep) can
        cause ``next_session_seq`` to re-issue already-used numbers, producing
        duplicated messages.
        """
        async with self._lock:
            buf = self._buffers.get(session_id)
            if buf:
                buf.mark_completed()
                if db is not None:
                    high_water = buf._session_seq
                    try:
                        await db.persist_session_manifest_seq(
                            session_id, high_water)
                    except Exception:
                        pass
                    # Also close the run-state's seq cursor — it drifts behind the
                    # manifest when the throttled per-event update (chat.py L2573)
                    # fires less recently than the last few events of the turn.
                    try:
                        await db.run_state_update_seq(session_id, high_water)
                    except Exception:
                        pass

    def _ensure_sweeper(self) -> None:
        if self._sweeper_task and not self._sweeper_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._sweeper_task = loop.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
                await self._sweep_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("RunBuffer sweep error: %s", e)

    async def _sweep_once(self) -> None:
        retention = _read_retention_seconds()
        now = time.time()
        drop: List[str] = []
        async with self._lock:
            for sid, buf in self._buffers.items():
                if buf.completed_at is None:
                    continue
                if (now - buf.completed_at) > retention:
                    drop.append(sid)
            for sid in drop:
                self._buffers.pop(sid, None)
        if drop:
            logger.debug("Swept %d run buffers (retention=%ds)", len(drop), retention)


# ── Module-level singleton ──

_registry: Optional[RunBufferRegistry] = None


def get_registry() -> RunBufferRegistry:
    global _registry
    if _registry is None:
        _registry = RunBufferRegistry()
    return _registry

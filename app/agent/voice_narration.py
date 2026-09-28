"""Low-latency, non-durable narration of agent lifecycle events.

The normal agent event stream is intentionally much more detailed than a useful
spoken status feed.  This module converts those events into a small, stable
taxonomy and applies user-selected detail, de-duplication, and throttling before
putting them on a per-session priority queue.

Nothing in this module persists an event or changes the run.  Consumers may
disconnect at any time without affecting the agent loop.
"""

from __future__ import annotations

import asyncio
import heapq
import re
import threading
import time
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Mapping


class NarrationEvent(str, Enum):
    RUN_QUEUED = "run_queued"
    RUN_STARTED = "run_started"
    AGENT_ASSIGNED = "agent_assigned"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    MILESTONE = "milestone"
    APPROVAL_REQUIRED = "approval_required"
    RUN_RESUMED = "run_resumed"
    RUN_COMPLETED = "run_completed"
    RUN_INTERRUPTED = "run_interrupted"
    RUN_FAILED = "run_failed"


class NarrationPriority(IntEnum):
    """Lower numbers are delivered first when several updates are waiting."""

    CRITICAL = 0
    MILESTONE = 1
    DETAIL = 2


class NarrationDetail(str, Enum):
    QUIET = "quiet"
    NORMAL = "normal"
    DETAILED = "detailed"


_DETAIL_LIMIT = {
    NarrationDetail.QUIET: NarrationPriority.CRITICAL,
    NarrationDetail.NORMAL: NarrationPriority.MILESTONE,
    NarrationDetail.DETAILED: NarrationPriority.DETAIL,
}


@dataclass(frozen=True)
class NarrationItem:
    session_id: str
    event: NarrationEvent
    priority: NarrationPriority
    text: str
    source_type: str
    created_at: float
    dedupe_key: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "type": "voice_narration",
            "session_id": self.session_id,
            "event": self.event.value,
            "priority": self.priority.name.lower(),
            "text": self.text,
            "created_at": self.created_at,
            "metadata": dict(self.metadata),
        }


_SPACE_RE = re.compile(r"\s+")
_MARKDOWN_RE = re.compile(r"[*_~`#>|]+")


def _clean_fragment(value: Any, limit: int = 280) -> str:
    text = _MARKDOWN_RE.sub(" ", str(value or ""))
    text = _SPACE_RE.sub(" ", text).strip()
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def _tool_label(value: Any) -> str:
    label = _clean_fragment(value, 80).replace("_", " ").replace("-", " ")
    return label or "a tool"


def normalize_agent_event(
    session_id: str,
    event: Mapping[str, Any],
    *,
    now: float | None = None,
) -> NarrationItem | None:
    """Convert a raw chat/agent event into a safe, speakable lifecycle item."""

    source_type = str(event.get("type") or "").strip().lower()
    status = str(event.get("status") or "").strip().lower()
    step = str(event.get("step") or "").strip().lower()
    created_at = time.monotonic() if now is None else now

    kind: NarrationEvent
    priority: NarrationPriority
    text: str
    metadata: dict[str, Any] = {}

    if source_type == "agent_status" and status == "queued":
        kind, priority, text = NarrationEvent.RUN_QUEUED, NarrationPriority.MILESTONE, "Your request is queued."
    elif source_type == "agent_status" and status == "running":
        kind, priority, text = NarrationEvent.RUN_STARTED, NarrationPriority.MILESTONE, "I started working on it."
    elif source_type == "agent_status" and status in {"waiting_approval", "approval_required"}:
        kind, priority, text = (
            NarrationEvent.APPROVAL_REQUIRED,
            NarrationPriority.CRITICAL,
            "I need your approval before I continue.",
        )
    elif source_type == "pipeline" and step == "agent_assigned":
        agent = _clean_fragment(event.get("agent") or event.get("agent_name"), 80)
        kind, priority = NarrationEvent.AGENT_ASSIGNED, NarrationPriority.MILESTONE
        text = f"{agent} is taking this on." if agent else "I assigned the next agent."
        if agent:
            metadata["agent"] = agent
    elif source_type == "tool_call":
        tool = _tool_label(event.get("tool"))
        kind, priority, text = NarrationEvent.TOOL_STARTED, NarrationPriority.DETAIL, f"I’m using {tool}."
        metadata["tool"] = tool
    elif source_type == "tool_result":
        tool = _tool_label(event.get("tool"))
        kind, priority, text = NarrationEvent.TOOL_FINISHED, NarrationPriority.DETAIL, f"I finished using {tool}."
        metadata["tool"] = tool
    elif source_type == "pipeline" and step in {
        "working_intro_ready",
        "memory_search_end",
        "attachment_describe_end",
        "compact_done",
    }:
        labels = {
            "working_intro_ready": "I have a plan and I’m moving into the main work.",
            "memory_search_end": "I finished checking the relevant context.",
            "attachment_describe_end": "I finished reviewing the attachment.",
            "compact_done": "I finished organizing the session context.",
        }
        kind, priority, text = NarrationEvent.MILESTONE, NarrationPriority.MILESTONE, labels[step]
        metadata["step"] = step
    elif source_type in {"approval_required", "confirmation_required"}:
        kind, priority, text = (
            NarrationEvent.APPROVAL_REQUIRED,
            NarrationPriority.CRITICAL,
            "I need your approval before I continue.",
        )
    elif source_type == "resumed":
        kind, priority, text = NarrationEvent.RUN_RESUMED, NarrationPriority.MILESTONE, "I resumed the run."
    elif source_type in {"response", "agent_message"}:
        content = _clean_fragment(event.get("content") or event.get("message"), 360)
        if not content:
            return None
        kind, priority, text = NarrationEvent.RUN_COMPLETED, NarrationPriority.CRITICAL, content
    elif source_type == "interrupted":
        message = _clean_fragment(event.get("message"), 180)
        kind, priority = NarrationEvent.RUN_INTERRUPTED, NarrationPriority.CRITICAL
        text = message or "I stopped the current run."
    elif source_type == "error":
        message = _clean_fragment(event.get("message"), 180)
        kind, priority = NarrationEvent.RUN_FAILED, NarrationPriority.CRITICAL
        text = f"I hit a problem. {message}" if message else "I hit a problem and couldn’t continue."
    else:
        return None

    discriminator = metadata.get("tool") or metadata.get("step") or text
    dedupe_key = f"{kind.value}:{str(discriminator).lower()}"
    return NarrationItem(
        session_id=session_id,
        event=kind,
        priority=priority,
        text=text,
        source_type=source_type,
        created_at=created_at,
        dedupe_key=dedupe_key,
        metadata=metadata,
    )


class VoiceNarrationBroker:
    """Process-local priority queues for optional voice status consumers."""

    def __init__(
        self,
        *,
        max_queue_size: int = 64,
        max_sessions: int = 1000,
        dedupe_seconds: float = 8.0,
        throttle_seconds: float = 0.8,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_queue_size = max(1, max_queue_size)
        self.max_sessions = max(1, max_sessions)
        self.dedupe_seconds = max(0.0, dedupe_seconds)
        self.throttle_seconds = max(0.0, throttle_seconds)
        self._clock = clock
        self._lock = threading.RLock()
        self._queues: dict[str, list[tuple[int, int, NarrationItem]]] = {}
        self._seen: dict[str, dict[str, float]] = {}
        self._last_kind: dict[str, dict[NarrationEvent, float]] = {}
        self._last_activity: dict[str, float] = {}
        self._sequence = 0
        self._waiters: dict[str, asyncio.Event] = {}

    def publish_nowait(
        self,
        session_id: str,
        event: Mapping[str, Any],
        *,
        detail: NarrationDetail | str = NarrationDetail.NORMAL,
    ) -> NarrationItem | None:
        try:
            detail_level = detail if isinstance(detail, NarrationDetail) else NarrationDetail(detail)
        except ValueError:
            detail_level = NarrationDetail.NORMAL

        now = self._clock()
        item = normalize_agent_event(session_id, event, now=now)
        if item is None or item.priority > _DETAIL_LIMIT[detail_level]:
            return None

        with self._lock:
            if session_id not in self._queues and len(self._queues) >= self.max_sessions:
                oldest = min(self._queues, key=lambda key: self._last_activity.get(key, 0.0))
                self._queues.pop(oldest, None)
                self._seen.pop(oldest, None)
                self._last_kind.pop(oldest, None)
                self._last_activity.pop(oldest, None)
                waiter = self._waiters.pop(oldest, None)
                if waiter is not None:
                    waiter.set()
            seen = self._seen.setdefault(session_id, {})
            last_seen = seen.get(item.dedupe_key)
            if last_seen is not None and now - last_seen < self.dedupe_seconds:
                return None

            by_kind = self._last_kind.setdefault(session_id, {})
            last_kind = by_kind.get(item.event)
            if (
                item.priority != NarrationPriority.CRITICAL
                and last_kind is not None
                and now - last_kind < self.throttle_seconds
            ):
                return None

            queue = self._queues.setdefault(session_id, [])
            self._sequence += 1
            entry = (int(item.priority), self._sequence, item)
            if len(queue) >= self.max_queue_size:
                worst_index = max(range(len(queue)), key=lambda index: (queue[index][0], queue[index][1]))
                worst = queue[worst_index]
                if entry[:2] >= worst[:2]:
                    return None
                queue[worst_index] = entry
                heapq.heapify(queue)
            else:
                heapq.heappush(queue, entry)

            seen[item.dedupe_key] = now
            self._last_activity[session_id] = now
            by_kind[item.event] = now
            stale_before = now - self.dedupe_seconds
            if len(seen) > self.max_queue_size * 4:
                self._seen[session_id] = {key: stamp for key, stamp in seen.items() if stamp >= stale_before}
            waiter = self._waiters.get(session_id)
            if waiter is not None:
                waiter.set()
            return item

    publish = publish_nowait

    def get_nowait(self, session_id: str) -> NarrationItem | None:
        with self._lock:
            queue = self._queues.get(session_id)
            if not queue:
                return None
            return heapq.heappop(queue)[2]

    async def get(self, session_id: str, *, timeout: float | None = None) -> NarrationItem | None:
        item = self.get_nowait(session_id)
        if item is not None:
            return item

        with self._lock:
            waiter = self._waiters.setdefault(session_id, asyncio.Event())
            waiter.clear()
        try:
            if timeout is None:
                await waiter.wait()
            else:
                await asyncio.wait_for(waiter.wait(), timeout=max(0.0, timeout))
        except TimeoutError:
            return None
        return self.get_nowait(session_id)

    def drain(self, session_id: str) -> list[NarrationItem]:
        items: list[NarrationItem] = []
        while (item := self.get_nowait(session_id)) is not None:
            items.append(item)
        return items

    def clear(self, session_id: str) -> None:
        with self._lock:
            self._queues.pop(session_id, None)
            self._seen.pop(session_id, None)
            self._last_kind.pop(session_id, None)
            self._last_activity.pop(session_id, None)
            waiter = self._waiters.pop(session_id, None)
            if waiter is not None:
                waiter.set()

    def queue_size(self, session_id: str) -> int:
        with self._lock:
            return len(self._queues.get(session_id, ()))


_BROKER = VoiceNarrationBroker()


def get_voice_narration_broker() -> VoiceNarrationBroker:
    return _BROKER

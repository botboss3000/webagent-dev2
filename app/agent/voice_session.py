"""Process-local, TTL-scoped context for conversational voice sessions.

Voice sessions are deliberately ephemeral. They mirror a bounded durable-chat
snapshot and recent agent events, but never write audio, transcripts, summaries,
or routing state to the interactions table (or any other persistent store).
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

from app.agent.voice_config import DEFAULT_VOICE_CONVERSATION, normalize_voice_conversation


DEFAULT_TTL_SECONDS = 15 * 60
MAX_DURABLE_MESSAGES = 12
MAX_AGENT_EVENTS = 20
MAX_VOICE_TURNS = 12
MAX_SUMMARY_CHARS = 3_000
MAX_VOICE_SESSIONS = 1_000
DEFAULT_PROPOSAL_TTL_SECONDS = 90
MAX_PROPOSAL_TTL_SECONDS = 5 * 60


def _ttl_from_env() -> int:
    try:
        return max(60, min(3600, int(os.environ.get("VOICE_SESSION_TTL_SECONDS", DEFAULT_TTL_SECONDS))))
    except (TypeError, ValueError):
        return DEFAULT_TTL_SECONDS


def _clean_text(value: Any, limit: int = 1200) -> str:
    return " ".join(str(value or "").split()).strip()[:limit]


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _proposal_copy(value: Dict[str, Any]) -> Dict[str, Any]:
    public = dict(value)
    public.pop("expires_at_epoch", None)
    return public


@dataclass
class VoiceSession:
    voice_session_id: str
    user_id: str
    chat_session_id: str
    active_run_id: Optional[str]
    last_agent_event_cursor: int
    durable_context: List[Dict[str, str]]
    conversation_summary: str
    conversation_config: Dict[str, Any]
    ttl_seconds: int
    pending_intent: Optional[Dict[str, Any]]
    pending_proposal: Optional[Dict[str, Any]] = None
    recent_agent_events: List[Dict[str, Any]] = field(default_factory=list)
    voice_turns: List[Dict[str, str]] = field(default_factory=list)
    created_at: float = 0.0
    updated_at: float = 0.0
    expires_at: float = 0.0

    def public_snapshot(self) -> Dict[str, Any]:
        value = asdict(self)
        if value.get("pending_proposal"):
            value["pending_proposal"] = _proposal_copy(value["pending_proposal"])
        value["created_at"] = _iso(self.created_at)
        value["updated_at"] = _iso(self.updated_at)
        value["expires_at"] = _iso(self.expires_at)
        return value


class VoiceSessionRegistry:
    """Bounded in-memory registry keyed by opaque voice-session IDs."""

    def __init__(
        self,
        *,
        ttl_seconds: Optional[int] = None,
        max_sessions: int = MAX_VOICE_SESSIONS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.ttl_seconds = int(ttl_seconds or _ttl_from_env())
        self.max_sessions = max(1, int(max_sessions))
        self.clock = clock
        self._sessions: Dict[str, VoiceSession] = {}
        self._lock = threading.RLock()

    def _purge_locked(self, now: float) -> int:
        expired = [key for key, value in self._sessions.items() if value.expires_at <= now]
        for key in expired:
            self._sessions.pop(key, None)
        return len(expired)

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_locked(self.clock())

    def _touch_locked(self, session: VoiceSession, now: Optional[float] = None) -> None:
        current = self.clock() if now is None else now
        session.updated_at = current
        session.expires_at = current + session.ttl_seconds

    def get(
        self,
        voice_session_id: str,
        *,
        user_id: Optional[str] = None,
        chat_session_id: Optional[str] = None,
        touch: bool = True,
    ) -> Optional[VoiceSession]:
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(str(voice_session_id or ""))
            if session is None:
                return None
            if user_id is not None and session.user_id != user_id:
                raise PermissionError("Voice session does not belong to this user")
            if chat_session_id is not None and session.chat_session_id != chat_session_id:
                raise PermissionError("Voice session belongs to a different chat")
            if touch:
                self._touch_locked(session, now)
            return session

    async def get_or_create(
        self,
        user_id: str,
        chat_session_id: str,
        *,
        voice_session_id: Optional[str] = None,
        db: Any = None,
        config: Optional[Dict[str, Any]] = None,
    ) -> VoiceSession:
        normalized_config = normalize_voice_conversation(config)
        configured_ttl = (
            int(normalized_config["session_timeout_minutes"]) * 60
            if config is not None else self.ttl_seconds
        )
        if voice_session_id:
            existing = self.get(
                voice_session_id,
                user_id=user_id,
                chat_session_id=chat_session_id,
            )
            if existing is not None:
                if config is not None:
                    with self._lock:
                        existing.conversation_config = normalized_config
                        existing.ttl_seconds = configured_ttl
                        self._trim_context_locked(existing)
                if db is not None:
                    await self.refresh(existing.voice_session_id, db)
                return existing

        now = self.clock()
        session = VoiceSession(
            voice_session_id=str(uuid.uuid4()),
            user_id=user_id,
            chat_session_id=chat_session_id,
            active_run_id=None,
            last_agent_event_cursor=0,
            durable_context=[],
            conversation_config=normalized_config,
            ttl_seconds=configured_ttl,
            conversation_summary="",
            pending_intent=None,
            pending_proposal=None,
            created_at=now,
            updated_at=now,
            expires_at=now + configured_ttl,
        )
        with self._lock:
            self._purge_locked(now)
            if len(self._sessions) >= self.max_sessions:
                oldest_id = min(
                    self._sessions,
                    key=lambda key: self._sessions[key].updated_at,
                )
                self._sessions.pop(oldest_id, None)
            self._sessions[session.voice_session_id] = session
        if db is not None:
            await self.refresh(session.voice_session_id, db)
        return session

    async def refresh(self, voice_session_id: str, db: Any) -> VoiceSession:
        session = self.get(voice_session_id)
        if session is None:
            raise KeyError("Voice session expired")

        run: Optional[Dict[str, Any]] = None
        history: List[Dict[str, Any]] = []
        try:
            run = await db.run_state_get(session.chat_session_id)
        except Exception:
            run = None
        try:
            rows = await db.fetch_interactions(
                session.user_id, session.chat_session_id,
            )
            context_limit = int(session.conversation_config.get("max_durable_messages", MAX_DURABLE_MESSAGES))
            for row in rows[-context_limit:] if context_limit else []:
                if isinstance(row, dict):
                    history.append({
                        "role": row.get("role"),
                        "content": row.get("content"),
                    })
                else:
                    history.append({
                        "role": getattr(row, "role", None),
                        "content": getattr(row, "content", None),
                    })
        except Exception:
            history = []

        durable: List[Dict[str, str]] = []
        context_limit = int(session.conversation_config.get("max_durable_messages", MAX_DURABLE_MESSAGES))
        for message in history[-context_limit:] if context_limit else []:
            role = str(message.get("role") or "")
            content = message.get("content")
            if role not in {"user", "assistant", "system"} or not isinstance(content, str):
                continue
            clean = _clean_text(content)
            if clean:
                durable.append({"role": role, "content": clean})

        with self._lock:
            current = self._sessions.get(voice_session_id)
            if current is None:
                raise KeyError("Voice session expired")
            current.durable_context = durable
            if run:
                run_status = str(run.get("status") or "").lower()
                current.active_run_id = (
                    str(run.get("turn_id") or "") or None
                    if run_status in {"running", "queued"}
                    else None
                )
                try:
                    cursor = int(run.get("latest_session_seq") or 0)
                except (TypeError, ValueError):
                    cursor = 0
                current.last_agent_event_cursor = max(current.last_agent_event_cursor, cursor)
            self._touch_locked(current)
            return current

    def record_utterance(
        self,
        voice_session_id: str,
        text: str,
        *,
        pending_kind: str = "unrouted",
    ) -> None:
        clean = _clean_text(text)
        if not clean:
            return
        with self._lock:
            session = self._sessions.get(voice_session_id)
            if session is None:
                return
            session.voice_turns.append({"role": "user", "content": clean})
            session.pending_intent = {"kind": pending_kind, "text": clean}
            self._trim_context_locked(session)
            self._touch_locked(session)

    def record_response(self, voice_session_id: str, text: str) -> None:
        clean = _clean_text(text)
        if not clean:
            return
        with self._lock:
            session = self._sessions.get(voice_session_id)
            if session is None:
                return
            session.voice_turns.append({"role": "assistant", "content": clean})
            session.pending_intent = None
            self._trim_context_locked(session)
            self._touch_locked(session)

    def set_pending_intent(
        self,
        voice_session_id: str,
        intent: Optional[Dict[str, Any]],
    ) -> None:
        with self._lock:
            session = self._sessions.get(voice_session_id)
            if session is None:
                return
            session.pending_intent = dict(intent) if intent else None
            self._touch_locked(session)

    def create_pending_proposal(
        self,
        voice_session_id: str,
        *,
        intent: str,
        action: str,
        utterance: str,
        ttl_seconds: int = DEFAULT_PROPOSAL_TTL_SECONDS,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Create one short-lived confirmation proposal for a voice session.

        An unresolved proposal must be explicitly cleared or resolved before it
        can be replaced. This avoids a later utterance silently changing what a
        user's "yes" would approve.
        """
        clean = _clean_text(utterance)
        if not clean:
            raise ValueError("Proposal utterance is required")
        lifetime = max(1, min(MAX_PROPOSAL_TTL_SECONDS, int(ttl_seconds)))
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(voice_session_id)
            if session is None:
                raise KeyError("Voice session expired")
            current = session.pending_proposal
            if current and current.get("state") == "pending":
                if float(current.get("expires_at_epoch") or 0) > now:
                    raise RuntimeError("A voice proposal is already awaiting confirmation")
            proposal = {
                "proposal_id": str(uuid.uuid4()),
                "intent": _clean_text(intent, 80),
                "action": _clean_text(action, 80),
                "utterance": clean,
                "state": "pending",
                "created_at": _iso(now),
                "expires_at": _iso(now + lifetime),
                # Kept internally so expiry comparisons do not parse timestamps.
                "expires_at_epoch": now + lifetime,
                "metadata": dict(metadata or {}),
            }
            session.pending_proposal = proposal
            self._touch_locked(session, now)
            return _proposal_copy(proposal)

    def get_pending_proposal(
        self,
        voice_session_id: str,
        *,
        proposal_id: Optional[str] = None,
        include_resolved: bool = False,
    ) -> Optional[Dict[str, Any]]:
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(voice_session_id)
            if session is None:
                return None
            proposal = session.pending_proposal
            if proposal is None:
                return None
            if proposal_id and proposal.get("proposal_id") != proposal_id:
                return None
            if (
                proposal.get("state") == "pending"
                and float(proposal.get("expires_at_epoch") or 0) <= now
            ):
                session.pending_proposal = None
                return None
            if proposal.get("state") != "pending" and not include_resolved:
                return None
            return _proposal_copy(proposal)

    def resolve_pending_proposal(
        self,
        voice_session_id: str,
        proposal_id: str,
        resolution: str,
    ) -> Optional[Dict[str, Any]]:
        """Atomically resolve a proposal; identical retries are idempotent."""
        normalized = str(resolution or "").strip().lower()
        if normalized not in {"accepted", "rejected", "cancelled"}:
            raise ValueError("resolution must be accepted, rejected, or cancelled")
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            session = self._sessions.get(voice_session_id)
            if session is None:
                return None
            proposal = session.pending_proposal
            if proposal is None or proposal.get("proposal_id") != proposal_id:
                return None
            state = str(proposal.get("state") or "")
            if state != "pending":
                if state == normalized:
                    return _proposal_copy(proposal)
                raise ValueError(f"Proposal was already resolved as {state}")
            if float(proposal.get("expires_at_epoch") or 0) <= now:
                session.pending_proposal = None
                return None
            proposal["state"] = normalized
            proposal["resolved_at"] = _iso(now)
            self._touch_locked(session, now)
            return _proposal_copy(proposal)

    def clear_pending_proposal(
        self,
        voice_session_id: str,
        *,
        proposal_id: Optional[str] = None,
    ) -> bool:
        with self._lock:
            session = self._sessions.get(voice_session_id)
            if session is None or session.pending_proposal is None:
                return False
            if proposal_id and session.pending_proposal.get("proposal_id") != proposal_id:
                return False
            session.pending_proposal = None
            self._touch_locked(session)
            return True

    def observe_agent_event(
        self,
        chat_session_id: str,
        user_id: Optional[str],
        event: Dict[str, Any],
    ) -> None:
        """Mirror a small, sanitized event tail into matching live voice sessions."""
        try:
            cursor = int(event.get("event_seq") or event.get("session_seq") or 0)
        except (TypeError, ValueError):
            cursor = 0
        event_type = str(event.get("type") or "")
        compact = {
            "type": event_type,
            "cursor": cursor,
            "turn_id": str(event.get("turn_id") or ""),
            "tool": _clean_text(event.get("tool"), 120),
            "status": _clean_text(event.get("status"), 120),
            "message": _clean_text(event.get("message") or event.get("note"), 360),
        }
        compact = {key: value for key, value in compact.items() if value not in ("", 0, None)}
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            for session in self._sessions.values():
                if session.chat_session_id != chat_session_id:
                    continue
                if user_id and session.user_id != user_id:
                    continue
                session.last_agent_event_cursor = max(session.last_agent_event_cursor, cursor)
                turn_id = str(event.get("turn_id") or "")
                if turn_id:
                    session.active_run_id = turn_id
                if compact:
                    session.recent_agent_events.append(compact)
                    self._trim_context_locked(session)
                if event_type in {"response", "interrupted", "error"}:
                    session.active_run_id = None
                self._touch_locked(session, now)

    def has_live_session(
        self,
        chat_session_id: str,
        user_id: Optional[str] = None,
    ) -> bool:
        """Return whether an unexpired ephemeral voice lane is listening here."""
        now = self.clock()
        with self._lock:
            self._purge_locked(now)
            return any(
                session.chat_session_id == chat_session_id
                and (not user_id or session.user_id == user_id)
                for session in self._sessions.values()
            )

    def _trim_context_locked(self, session: VoiceSession) -> None:
        config = session.conversation_config or DEFAULT_VOICE_CONVERSATION
        voice_limit = int(config.get("max_voice_turns", MAX_VOICE_TURNS))
        event_limit = int(config.get("max_agent_events", MAX_AGENT_EVENTS))
        durable_limit = int(config.get("max_durable_messages", MAX_DURABLE_MESSAGES))
        session.voice_turns = session.voice_turns[-voice_limit:] if voice_limit else []
        session.recent_agent_events = session.recent_agent_events[-event_limit:] if event_limit else []
        session.durable_context = session.durable_context[-durable_limit:] if durable_limit else []
        self._rebuild_summary_locked(session)

    def _rebuild_summary_locked(self, session: VoiceSession) -> None:
        lines = [
            f"{turn['role'].title()}: {turn['content']}"
            for turn in session.voice_turns
        ]
        session.conversation_summary = "\n".join(lines)[-MAX_SUMMARY_CHARS:]

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()


_registry = VoiceSessionRegistry()


def get_voice_session_registry() -> VoiceSessionRegistry:
    return _registry

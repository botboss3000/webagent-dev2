"""Guardrailed run controls for the parallel voice conversation lane.

This module deliberately does not write chat interactions.  Commands that must
enter the agent loop (approval, rejection, or an accepted change) are returned
as structured dispatch specifications so the chat API can persist them through
the same durable-turn path as typed input.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Dict, Optional


CONTROL_ACTIONS = frozenset({
    "pause", "resume", "stop", "approve", "reject", "apply_change",
})
_DISPATCH_ACTIONS = frozenset({"approve", "reject", "apply_change"})
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class VoiceControlResult:
    action: str
    status: str
    code: str
    message: str
    dispatch: Optional[Dict[str, Any]] = None
    audit: Optional[Dict[str, Any]] = None
    replayed: bool = False

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

    def as_dict(self) -> Dict[str, Any]:
        return {
            "action": self.action,
            "status": self.status,
            "code": self.code,
            "message": self.message,
            "dispatch": self.dispatch,
            "audit": self.audit,
            "replayed": self.replayed,
        }


@dataclass(frozen=True)
class _Receipt:
    fingerprint: str
    result: VoiceControlResult
    expires_at: float


ManualResume = Callable[[str], Awaitable[bool]]


class VoiceControlCoordinator:
    """Execute direct run controls and prepare normal-loop control turns.

    Idempotency receipts are tenant- and session-scoped, bounded, and
    process-local.  A production multi-worker deployment should route a voice
    session consistently or replace this cache with a shared atomic store.
    """

    def __init__(
        self,
        *,
        run_manager: Optional[Any] = None,
        manual_resume: Optional[ManualResume] = None,
        receipt_ttl_seconds: float = 300.0,
        max_receipts: int = 1000,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._run_manager = run_manager
        self._manual_resume = manual_resume
        self._receipt_ttl = max(1.0, float(receipt_ttl_seconds))
        self._max_receipts = max(1, int(max_receipts))
        self._clock = clock
        self._receipts: "OrderedDict[tuple[str, str, str], _Receipt]" = OrderedDict()
        self._lock = asyncio.Lock()

    async def execute(
        self,
        *,
        action: str,
        user_id: str,
        session_id: str,
        idempotency_key: str,
        db: Optional[Any] = None,
        command: str = "",
        target_turn_id: Optional[str] = None,
        approval_id: Optional[str] = None,
    ) -> VoiceControlResult:
        """Execute one control exactly once for an idempotency key."""
        action = str(action or "").strip().lower()
        user_id = str(user_id or "").strip()
        session_id = str(session_id or "").strip()
        idempotency_key = str(idempotency_key or "").strip()
        target_turn_id = _optional_text(target_turn_id, 256)
        approval_id = _optional_text(approval_id, 256)
        command = _clean_command(command)

        invalid = _validate_request(action, user_id, session_id, idempotency_key)
        if invalid:
            return _rejected(action, "invalid_request", invalid)

        fingerprint = _fingerprint(
            action, command, target_turn_id, approval_id,
        )
        key = (user_id, session_id, idempotency_key)
        async with self._lock:
            self._prune()
            receipt = self._receipts.get(key)
            if receipt is not None:
                self._receipts.move_to_end(key)
                if receipt.fingerprint != fingerprint:
                    return _rejected(
                        action,
                        "idempotency_conflict",
                        "That idempotency key was already used for a different control.",
                    )
                return replace(receipt.result, replayed=True)

            result = await self._execute_uncached(
                action=action,
                user_id=user_id,
                session_id=session_id,
                db=db,
                command=command,
                target_turn_id=target_turn_id,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )
            self._receipts[key] = _Receipt(
                fingerprint=fingerprint,
                result=result,
                expires_at=self._clock() + self._receipt_ttl,
            )
            self._receipts.move_to_end(key)
            while len(self._receipts) > self._max_receipts:
                self._receipts.popitem(last=False)
            return result

    async def _execute_uncached(
        self,
        *,
        action: str,
        user_id: str,
        session_id: str,
        db: Optional[Any],
        command: str,
        target_turn_id: Optional[str],
        approval_id: Optional[str],
        idempotency_key: str,
    ) -> VoiceControlResult:
        if action in {"pause", "stop"}:
            if db is None:
                return _rejected(action, "storage_required", "Run storage is unavailable.")
            manager = self._manager()
            active_turn = manager.active_turn_id(session_id)
            if target_turn_id and active_turn != target_turn_id:
                return _rejected(
                    action,
                    "stale_target",
                    "The targeted run is no longer active; no control was applied.",
                )
            if action == "pause":
                changed = await manager.pause(session_id, db)
                message = "Run paused. Say resume when you want it to continue."
            else:
                changed = await manager.interrupt(session_id, db, cause="user_stop")
                message = "Run stopped."
            if not changed:
                return VoiceControlResult(
                    action, "noop", "no_active_run", "There is no active run to control."
                )
            return VoiceControlResult(
                action,
                "accepted",
                "run_paused" if action == "pause" else "run_stopped",
                message,
                audit=_audit_payload(action, target_turn_id, None, idempotency_key),
            )

        if action == "resume":
            resumed = await self._resume(session_id)
            if not resumed:
                return VoiceControlResult(
                    action, "noop", "not_resumable", "There is no paused run to resume."
                )
            return VoiceControlResult(
                action,
                "accepted",
                "run_resumed",
                "Run resumed.",
                audit=_audit_payload(action, target_turn_id, None, idempotency_key),
            )

        if action in {"approve", "reject"}:
            if not approval_id:
                return _rejected(
                    action,
                    "approval_required",
                    "A specific pending approval is required.",
                )
            verb = "Approve" if action == "approve" else "Reject"
            content = f"{verb} pending action {approval_id}."
            return self._dispatch_result(
                action=action,
                content=content,
                session_id=session_id,
                user_id=user_id,
                target_turn_id=target_turn_id,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )

        if action == "apply_change":
            if not approval_id:
                return _rejected(
                    action,
                    "approval_required",
                    "Confirm the proposed change before applying it.",
                )
            if not command:
                return _rejected(action, "command_required", "No change command was provided.")
            return self._dispatch_result(
                action=action,
                content=command,
                session_id=session_id,
                user_id=user_id,
                target_turn_id=target_turn_id,
                approval_id=approval_id,
                idempotency_key=idempotency_key,
            )

        return _rejected(action, "unsupported_action", "Unsupported voice control action.")

    def _dispatch_result(
        self,
        *,
        action: str,
        content: str,
        session_id: str,
        user_id: str,
        target_turn_id: Optional[str],
        approval_id: str,
        idempotency_key: str,
    ) -> VoiceControlResult:
        # This is a specification, not an execution path.  The API must send it
        # through the regular durable chat-turn machinery and its guardrails.
        dispatch = {
            "kind": "structured_user_turn",
            "session_id": session_id,
            "user_id": user_id,
            "content": content,
            "source": "voice:control",
            "metadata": {
                "control_action": action,
                "approval_id": approval_id,
                "target_turn_id": target_turn_id,
                "idempotency_key": idempotency_key,
            },
        }
        return VoiceControlResult(
            action,
            "accepted",
            "dispatch_required",
            "Control accepted and ready for the agent loop.",
            dispatch=dispatch,
            audit=_audit_payload(action, target_turn_id, approval_id, idempotency_key),
        )

    def _manager(self) -> Any:
        if self._run_manager is not None:
            return self._run_manager
        from app.agent.run_manager import get_run_manager
        return get_run_manager()

    async def _resume(self, session_id: str) -> bool:
        if self._manual_resume is not None:
            return bool(await self._manual_resume(session_id))
        from app.agent.runner import manual_resume
        return bool(await manual_resume(session_id))

    def _prune(self) -> None:
        now = self._clock()
        stale = [key for key, receipt in self._receipts.items() if receipt.expires_at <= now]
        for key in stale:
            self._receipts.pop(key, None)


def _validate_request(action: str, user_id: str, session_id: str, key: str) -> str:
    if action not in CONTROL_ACTIONS:
        return "Unsupported voice control action."
    if not user_id or not session_id:
        return "A user and chat session are required."
    if len(key) < 8 or len(key) > 128:
        return "The idempotency key must be between 8 and 128 characters."
    return ""


def _clean_command(value: Any) -> str:
    command = _CONTROL_CHARS.sub("", str(value or "")).strip()
    return command[:4000]


def _optional_text(value: Any, limit: int) -> Optional[str]:
    text = _CONTROL_CHARS.sub("", str(value or "")).strip()
    return text[:limit] or None


def _fingerprint(
    action: str,
    command: str,
    target_turn_id: Optional[str],
    approval_id: Optional[str],
) -> str:
    payload = json.dumps(
        [action, command, target_turn_id, approval_id],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _audit_payload(
    action: str,
    target_turn_id: Optional[str],
    approval_id: Optional[str],
    idempotency_key: str,
) -> Dict[str, Any]:
    """Return audit-safe qualifiers, intentionally excluding any transcript."""
    return {
        "source": "voice_control",
        "action": action,
        "target_turn_id": target_turn_id,
        "approval_id": approval_id,
        "idempotency_key": idempotency_key,
    }


def _rejected(action: str, code: str, message: str) -> VoiceControlResult:
    return VoiceControlResult(action, "rejected", code, message)


_voice_control_coordinator: Optional[VoiceControlCoordinator] = None


def get_voice_control_coordinator() -> VoiceControlCoordinator:
    global _voice_control_coordinator
    if _voice_control_coordinator is None:
        _voice_control_coordinator = VoiceControlCoordinator()
    return _voice_control_coordinator

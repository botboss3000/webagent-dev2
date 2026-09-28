"""Fail-closed routing for ephemeral conversational voice input.

This module only decides what may happen next. It never dispatches work,
interrupts a run, writes an interaction, or calls a model. Ambiguous and
state-changing requests therefore remain harmless until the API layer either
asks for confirmation or clarification.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Dict, Optional


class VoiceIntent(str, Enum):
    CONVERSATION_QUESTION = "conversation_question"
    RUN_STATUS = "run_status"
    NEW_WORK = "new_work"
    MODIFY_ACTIVE_WORK = "modify_active_work"
    STOP_OR_PAUSE = "stop_or_pause"
    CONFIRMATION = "confirmation"
    UNCERTAIN = "uncertain"


class VoiceAction(str, Enum):
    ANSWER = "answer"
    DISPATCH_WORK = "dispatch_work"
    INTERRUPT = "interrupt"
    REQUEST_CONFIRMATION = "request_confirmation"
    REQUEST_CLARIFICATION = "request_clarification"
    REJECT_CONFIRMATION = "reject_confirmation"
    NOOP = "noop"


@dataclass(frozen=True)
class VoiceRoute:
    intent: VoiceIntent
    action: VoiceAction
    confidence: float
    utterance: str
    reason: str
    proposal: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["intent"] = self.intent.value
        value["action"] = self.action.value
        return value


_STATUS_PATTERNS = (
    r"\b(?:status|progress)\b",
    r"\bwhat(?:'s| is) (?:happening|it doing|the agent doing)\b",
    r"\bwhat are you doing\b",
    r"\bhow (?:is it going|far along are you)\b",
    r"\b(?:is it|are you) (?:done|finished|still (?:running|working))\b",
)
_STOP_PATTERNS = (
    r"^(?:please )?(?:stop|cancel|abort)(?: (?:the )?(?:run|task|work|job|agent))?[.!]?$",
    r"^(?:please )?(?:stop|cancel|abort) (?:what you(?:'re| are) doing|this)[.!]?$",
)
_PAUSE_PATTERNS = (
    r"\b(?:pause|hold on|wait a moment|wait a second)\b",
)
_RESUME_PATTERNS = (
    r"^(?:please )?(?:resume|continue)(?: (?:the )?(?:run|task|work|job|agent))?[.!]?$",
)
_MODIFY_PATTERNS = (
    r"\b(?:instead|rather than|change|modify|revise|switch|replace|remove|rename|update)\b",
    r"\b(?:also|add|include|make sure|don't|do not)\b",
    r"\b(?:use|stop using)\s+\S+",
)
_WORK_PATTERNS = (
    r"^(?:please\s+)?(?:build|create|implement|fix|write|make|add|remove|update|run|test|deploy|research|find|open|send|generate)\b",
    r"^(?:can|could|would) you\s+(?:please\s+)?(?:build|create|implement|fix|write|make|add|remove|update|run|test|deploy|research|find|open|send|generate)\b",
)
_YES = re.compile(r"^(?:yes|yeah|yep|sure|okay|ok|do it|go ahead|confirm|approved?)[.!]?$", re.I)
_NO = re.compile(r"^(?:no|nope|nah|cancel|never mind|nevermind|reject|don't|do not)[.!]?$", re.I)


def _matches(patterns: tuple[str, ...], text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


def _proposal_payload(intent: VoiceIntent, utterance: str) -> Dict[str, Any]:
    return {
        "intent": intent.value,
        "action": VoiceAction.DISPATCH_WORK.value,
        "utterance": utterance,
    }


def route_voice_utterance(
    utterance: str,
    *,
    active_run: bool = False,
    pending_proposal: Optional[Dict[str, Any]] = None,
) -> VoiceRoute:
    """Classify an utterance using conservative, deterministic rules.

    The caller must supply a live proposal before a bare yes/no can have any
    effect. Modifications and any new work while a run is active always require
    confirmation. Explicit pause and stop controls may affect only a live run.
    """
    clean = " ".join(str(utterance or "").split()).strip()
    lowered = clean.lower()
    if not clean:
        return VoiceRoute(
            VoiceIntent.UNCERTAIN,
            VoiceAction.REQUEST_CLARIFICATION,
            1.0,
            clean,
            "empty_utterance",
        )

    if _YES.fullmatch(clean) or _NO.fullmatch(clean):
        if not pending_proposal or pending_proposal.get("state", "pending") != "pending":
            return VoiceRoute(
                VoiceIntent.CONFIRMATION,
                VoiceAction.REJECT_CONFIRMATION,
                1.0,
                clean,
                "no_pending_proposal",
            )
        accepted = bool(_YES.fullmatch(clean))
        return VoiceRoute(
            VoiceIntent.CONFIRMATION,
            VoiceAction.DISPATCH_WORK if accepted else VoiceAction.NOOP,
            1.0,
            clean,
            "proposal_accepted" if accepted else "proposal_rejected",
            proposal=dict(pending_proposal),
        )

    if _matches(_STOP_PATTERNS, lowered):
        return VoiceRoute(
            VoiceIntent.STOP_OR_PAUSE,
            VoiceAction.INTERRUPT if active_run else VoiceAction.NOOP,
            0.99,
            clean,
            "explicit_stop" if active_run else "no_active_run",
        )

    if _matches(_PAUSE_PATTERNS, lowered):
        return VoiceRoute(
            VoiceIntent.STOP_OR_PAUSE,
            VoiceAction.INTERRUPT if active_run else VoiceAction.NOOP,
            0.95,
            clean,
            "explicit_pause" if active_run else "no_active_run",
        )

    if _matches(_RESUME_PATTERNS, lowered):
        return VoiceRoute(
            VoiceIntent.STOP_OR_PAUSE,
            VoiceAction.NOOP if active_run else VoiceAction.INTERRUPT,
            0.98,
            clean,
            "already_active" if active_run else "explicit_resume",
        )

    if _matches(_STATUS_PATTERNS, lowered):
        return VoiceRoute(
            VoiceIntent.RUN_STATUS,
            VoiceAction.ANSWER,
            0.98,
            clean,
            "status_question",
        )

    if active_run and _matches(_MODIFY_PATTERNS, lowered):
        intent = VoiceIntent.MODIFY_ACTIVE_WORK
        return VoiceRoute(
            intent,
            VoiceAction.REQUEST_CONFIRMATION,
            0.92,
            clean,
            "active_run_change_requires_confirmation",
            proposal=_proposal_payload(intent, clean),
        )

    if _matches(_WORK_PATTERNS, lowered):
        intent = VoiceIntent.NEW_WORK
        if active_run:
            return VoiceRoute(
                intent,
                VoiceAction.REQUEST_CONFIRMATION,
                0.94,
                clean,
                "new_work_during_active_run_requires_confirmation",
                proposal=_proposal_payload(intent, clean),
            )
        return VoiceRoute(
            intent,
            VoiceAction.DISPATCH_WORK,
            0.94,
            clean,
            "explicit_work_request",
        )

    if clean.endswith("?") or re.match(
        r"^(?:what|why|when|where|who|how|is|are|can|could|do|does|did|tell me|explain)\b",
        lowered,
    ):
        return VoiceRoute(
            VoiceIntent.CONVERSATION_QUESTION,
            VoiceAction.ANSWER,
            0.82,
            clean,
            "read_only_question",
        )

    return VoiceRoute(
        VoiceIntent.UNCERTAIN,
        VoiceAction.REQUEST_CLARIFICATION,
        0.45,
        clean,
        "no_safe_deterministic_route",
    )


def route_model_intent(
    utterance: str,
    intent: VoiceIntent,
    *,
    active_run: bool = False,
) -> VoiceRoute:
    """Map a model classification onto the same fail-closed action contract.

    The model may distinguish conversation from work, but it never chooses a
    direct run control. Active-run changes still require confirmation here.
    """
    clean = " ".join(str(utterance or "").split()).strip()
    if intent is VoiceIntent.CONVERSATION_QUESTION:
        return VoiceRoute(
            intent, VoiceAction.ANSWER, 0.8, clean, "model_conversation",
        )
    if intent in {VoiceIntent.NEW_WORK, VoiceIntent.MODIFY_ACTIVE_WORK}:
        if active_run:
            return VoiceRoute(
                intent,
                VoiceAction.REQUEST_CONFIRMATION,
                0.8,
                clean,
                "model_active_run_change_requires_confirmation",
                proposal=_proposal_payload(intent, clean),
            )
        return VoiceRoute(
            intent,
            VoiceAction.DISPATCH_WORK,
            0.8,
            clean,
            "model_work_request",
        )
    return VoiceRoute(
        VoiceIntent.UNCERTAIN,
        VoiceAction.REQUEST_CLARIFICATION,
        0.4,
        clean,
        "model_intent_ambiguous",
    )


def classify_voice_intent(
    utterance: str,
    *,
    active_run: bool = False,
    pending_proposal: Optional[Dict[str, Any]] = None,
) -> VoiceIntent:
    """Compatibility helper for callers that only need the intent label."""
    return route_voice_utterance(
        utterance,
        active_run=active_run,
        pending_proposal=pending_proposal,
    ).intent


# A concise alias for API layers that name the operation after its result.
route_voice_intent = route_voice_utterance

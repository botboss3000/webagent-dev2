"""Conservative voice-intent routing and proposal lifecycle coverage."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.voice_intent import (
    VoiceAction,
    VoiceIntent,
    classify_voice_intent,
    route_voice_utterance,
    route_model_intent,
)
from app.agent.voice_session import VoiceSessionRegistry


@pytest.mark.parametrize(
    ("utterance", "intent", "action"),
    [
        ("What's happening?", VoiceIntent.RUN_STATUS, VoiceAction.ANSWER),
        ("Why did the test fail?", VoiceIntent.CONVERSATION_QUESTION, VoiceAction.ANSWER),
        ("Build a settings screen", VoiceIntent.NEW_WORK, VoiceAction.DISPATCH_WORK),
        ("maybe the blue one", VoiceIntent.UNCERTAIN, VoiceAction.REQUEST_CLARIFICATION),
        ("pause", VoiceIntent.STOP_OR_PAUSE, VoiceAction.NOOP),
    ],
)
def test_routes_safe_non_active_run_intents(utterance, intent, action) -> None:
    route = route_voice_utterance(utterance)
    assert route.intent is intent
    assert route.action is action
    assert route.to_dict()["intent"] == intent.value
    assert classify_voice_intent(utterance) is intent


def test_active_run_changes_require_confirmation() -> None:
    modification = route_voice_utterance(
        "Stop using SQLite and use Postgres instead",
        active_run=True,
    )
    assert modification.intent is VoiceIntent.MODIFY_ACTIVE_WORK
    assert modification.action is VoiceAction.REQUEST_CONFIRMATION
    assert modification.proposal == {
        "intent": "modify_active_work",
        "action": "dispatch_work",
        "utterance": "Stop using SQLite and use Postgres instead",
    }

    new_work = route_voice_utterance("Create a landing page", active_run=True)
    assert new_work.intent is VoiceIntent.NEW_WORK
    assert new_work.action is VoiceAction.REQUEST_CONFIRMATION

def test_model_intent_routes_natural_speech_without_bypassing_safety() -> None:
    conversation = route_model_intent(
        "Hello there", VoiceIntent.CONVERSATION_QUESTION,
    )
    assert conversation.action is VoiceAction.ANSWER

    work = route_model_intent(
        "I would like you to check the tests", VoiceIntent.NEW_WORK,
    )
    assert work.action is VoiceAction.DISPATCH_WORK

    active_change = route_model_intent(
        "Please look at the current code",
        VoiceIntent.MODIFY_ACTIVE_WORK,
        active_run=True,
    )
    assert active_change.action is VoiceAction.REQUEST_CONFIRMATION
    assert active_change.proposal["utterance"] == "Please look at the current code"


def test_only_unambiguous_stop_interrupts_an_active_run() -> None:
    stop = route_voice_utterance("stop the run", active_run=True)
    assert stop.intent is VoiceIntent.STOP_OR_PAUSE
    assert stop.action is VoiceAction.INTERRUPT

    pause = route_voice_utterance("pause", active_run=True)
    assert pause.intent is VoiceIntent.STOP_OR_PAUSE
    assert pause.action is VoiceAction.INTERRUPT
    assert pause.reason == "explicit_pause"

    resume = route_voice_utterance("resume", active_run=False)
    assert resume.action is VoiceAction.INTERRUPT
    assert resume.reason == "explicit_resume"

    no_run = route_voice_utterance("stop", active_run=False)
    assert no_run.action is VoiceAction.NOOP

    modification = route_voice_utterance("stop using SQLite", active_run=True)
    assert modification.intent is VoiceIntent.MODIFY_ACTIVE_WORK
    assert modification.action is VoiceAction.REQUEST_CONFIRMATION


def test_confirmation_is_inert_without_a_live_proposal() -> None:
    orphan = route_voice_utterance("yes")
    assert orphan.intent is VoiceIntent.CONFIRMATION
    assert orphan.action is VoiceAction.REJECT_CONFIRMATION

    proposal = {
        "proposal_id": "proposal-1",
        "state": "pending",
        "utterance": "Build it",
    }
    accepted = route_voice_utterance("go ahead", pending_proposal=proposal)
    rejected = route_voice_utterance("nope", pending_proposal=proposal)
    assert accepted.action is VoiceAction.DISPATCH_WORK
    assert accepted.proposal == proposal
    assert rejected.action is VoiceAction.NOOP
    assert rejected.proposal == proposal


def test_proposal_lifecycle_is_ttl_bound_and_resolution_is_idempotent() -> None:
    now = [100.0]
    registry = VoiceSessionRegistry(ttl_seconds=60, clock=lambda: now[0])
    session = asyncio.run(registry.get_or_create("user-1", "chat-1"))
    proposal = registry.create_pending_proposal(
        session.voice_session_id,
        intent="new_work",
        action="dispatch_work",
        utterance="Build a dashboard",
        ttl_seconds=10,
        metadata={"source": "voice"},
    )

    current = registry.get_pending_proposal(session.voice_session_id)
    assert current is not None
    assert current["proposal_id"] == proposal["proposal_id"]
    assert current["metadata"] == {"source": "voice"}

    with pytest.raises(RuntimeError):
        registry.create_pending_proposal(
            session.voice_session_id,
            intent="new_work",
            action="dispatch_work",
            utterance="Replace the pending proposal",
        )

    first = registry.resolve_pending_proposal(
        session.voice_session_id, proposal["proposal_id"], "accepted",
    )
    retry = registry.resolve_pending_proposal(
        session.voice_session_id, proposal["proposal_id"], "accepted",
    )
    assert first == retry
    assert first is not None and first["state"] == "accepted"
    assert registry.get_pending_proposal(session.voice_session_id) is None
    assert registry.get_pending_proposal(
        session.voice_session_id, include_resolved=True,
    )["state"] == "accepted"
    with pytest.raises(ValueError):
        registry.resolve_pending_proposal(
            session.voice_session_id, proposal["proposal_id"], "rejected",
        )

    assert registry.clear_pending_proposal(
        session.voice_session_id, proposal_id=proposal["proposal_id"],
    )
    assert registry.get_pending_proposal(
        session.voice_session_id, include_resolved=True,
    ) is None


def test_expired_proposal_cannot_be_confirmed() -> None:
    now = [200.0]
    registry = VoiceSessionRegistry(ttl_seconds=60, clock=lambda: now[0])
    session = asyncio.run(registry.get_or_create("user-1", "chat-1"))
    proposal = registry.create_pending_proposal(
        session.voice_session_id,
        intent="modify_active_work",
        action="dispatch_work",
        utterance="Use a different database",
        ttl_seconds=2,
    )
    now[0] += 3
    assert registry.get_pending_proposal(session.voice_session_id) is None
    assert registry.resolve_pending_proposal(
        session.voice_session_id, proposal["proposal_id"], "accepted",
    ) is None

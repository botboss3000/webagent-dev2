"""Ephemeral voice-session lifecycle, context, and isolation coverage."""

from __future__ import annotations

import asyncio

import pytest

from app.agent.voice_config import normalize_voice_conversation
from app.agent.voice_session import VoiceSessionRegistry


class ReadOnlyDb:
    def __init__(self) -> None:
        self.calls = []

    async def run_state_get(self, session_id):
        self.calls.append(("run_state_get", session_id))
        return {
            "turn_id": "run-turn-1",
            "status": "running",
            "latest_session_seq": 41,
        }

    async def fetch_interactions(self, user_id, session_id):
        self.calls.append(("fetch_interactions", user_id, session_id))
        return [
            {"role": "user", "content": "Build the dashboard"},
            {"role": "assistant", "content": "I started the work"},
            {"role": "tool", "content": "must not enter voice context"},
        ]

    def __getattr__(self, name):
        if any(term in name for term in ("insert", "update", "add", "save", "write")):
            raise AssertionError(f"unexpected persistence call: {name}")
        raise AttributeError(name)


def test_voice_session_hydrates_read_only_context_and_expires() -> None:
    now = [1000.0]
    registry = VoiceSessionRegistry(ttl_seconds=10, clock=lambda: now[0])
    db = ReadOnlyDb()

    session = asyncio.run(registry.get_or_create("user-1", "chat-1", db=db))
    assert session.active_run_id == "run-turn-1"
    assert session.last_agent_event_cursor == 41
    assert session.durable_context == [
        {"role": "user", "content": "Build the dashboard"},
        {"role": "assistant", "content": "I started the work"},
    ]
    assert db.calls == [
        ("run_state_get", "chat-1"),
        ("fetch_interactions", "user-1", "chat-1"),
    ]

    now[0] += 11
    assert registry.get(session.voice_session_id, touch=False) is None
    assert registry.purge_expired() == 0


def test_voice_session_summary_event_cursor_and_identity_isolation() -> None:
    registry = VoiceSessionRegistry(ttl_seconds=60)
    first = asyncio.run(registry.get_or_create("user-1", "chat-1"))
    second = asyncio.run(registry.get_or_create("user-2", "chat-2"))

    registry.record_utterance(first.voice_session_id, "What is it doing?")
    registry.set_pending_intent(
        first.voice_session_id,
        {"kind": "question", "text": "What is it doing?"},
    )
    registry.record_response(first.voice_session_id, "It is running the tests.")
    registry.observe_agent_event(
        "chat-1",
        "user-1",
        {
            "type": "tool_call",
            "tool": "pytest",
            "event_seq": 44,
            "turn_id": "run-turn-1",
        },
    )

    current = registry.get(first.voice_session_id, touch=False)
    assert current is not None
    assert current.pending_intent is None
    assert current.active_run_id == "run-turn-1"
    assert current.last_agent_event_cursor == 44
    assert "User: What is it doing?" in current.conversation_summary
    assert "Assistant: It is running the tests." in current.conversation_summary
    assert current.recent_agent_events[-1]["tool"] == "pytest"

    untouched = registry.get(second.voice_session_id, touch=False)
    assert untouched is not None
    assert untouched.last_agent_event_cursor == 0
    assert untouched.recent_agent_events == []
    with pytest.raises(PermissionError):
        registry.get(first.voice_session_id, user_id="user-2")
    with pytest.raises(PermissionError):
        registry.get(first.voice_session_id, chat_session_id="chat-2")

    registry.observe_agent_event(
        "chat-1", "user-1", {"type": "response", "event_seq": 45},
    )
    assert registry.get(first.voice_session_id, touch=False).active_run_id is None


def test_expired_voice_session_id_is_replaced_not_resurrected() -> None:
    now = [50.0]
    registry = VoiceSessionRegistry(ttl_seconds=5, clock=lambda: now[0])
    old = asyncio.run(registry.get_or_create("user-1", "chat-1"))
    now[0] += 6
    replacement = asyncio.run(registry.get_or_create(
        "user-1", "chat-1", voice_session_id=old.voice_session_id,
    ))
    assert replacement.voice_session_id != old.voice_session_id
    assert registry.get(old.voice_session_id, touch=False) is None


def test_voice_session_registry_evicts_the_least_recently_used_session() -> None:
    now = [1.0]
    registry = VoiceSessionRegistry(
        ttl_seconds=60, max_sessions=2, clock=lambda: now[0],
    )
    first = asyncio.run(registry.get_or_create("user-1", "chat-1"))
    now[0] += 1
    second = asyncio.run(registry.get_or_create("user-1", "chat-2"))
    now[0] += 1
    registry.get(first.voice_session_id)  # first is now the most recently used
    now[0] += 1
    third = asyncio.run(registry.get_or_create("user-1", "chat-3"))
    assert registry.get(first.voice_session_id, touch=False) is not None
    assert registry.get(second.voice_session_id, touch=False) is None
    assert registry.get(third.voice_session_id, touch=False) is not None


def test_voice_conversation_config_is_closed_and_bounded() -> None:
    config = normalize_voice_conversation({
        "continuous_context": False,
        "session_timeout_minutes": 999,
        "max_voice_turns": 1,
        "max_durable_messages": -3,
        "max_agent_events": 99,
        "response_length": "invalid",
        "response_instructions": "x" * 2500,
        "unknown": "discarded",
    })
    assert config["continuous_context"] is False
    assert config["session_timeout_minutes"] == 60
    assert config["max_voice_turns"] == 2
    assert config["max_durable_messages"] == 0
    assert config["max_agent_events"] == 30
    assert config["response_length"] == "brief"
    assert len(config["response_instructions"]) == 2000
    assert "unknown" not in config


def test_voice_session_applies_per_agent_context_and_expiry_limits() -> None:
    now = [100.0]
    registry = VoiceSessionRegistry(ttl_seconds=10, clock=lambda: now[0])
    session = asyncio.run(registry.get_or_create(
        "user-1", "chat-1",
        config={
            "session_timeout_minutes": 2,
            "max_voice_turns": 2,
            "max_agent_events": 1,
        },
    ))
    registry.record_utterance(session.voice_session_id, "first")
    registry.record_response(session.voice_session_id, "second")
    registry.record_utterance(session.voice_session_id, "third")
    current = registry.get(session.voice_session_id, touch=False)
    assert [turn["content"] for turn in current.voice_turns] == ["second", "third"]
    assert current.expires_at == 220.0

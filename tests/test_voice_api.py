"""End-to-end API boundaries for ephemeral voice routing and controls."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat import execution as chat
from app.agent.voice_session import get_voice_session_registry


class _VoiceDb:
    def __init__(self, *, running: bool = True) -> None:
        self.running = running

    async def run_state_get(self, _session_id: str):
        if not self.running:
            return {"status": "complete", "turn_id": "turn-old"}
        return {"status": "running", "turn_id": "turn-active"}

    async def fetch_interactions(self, _user_id: str, _session_id: str):
        return []


def _client(monkeypatch, db: _VoiceDb) -> TestClient:
    async def require_access(_request, claimed_user_id, _session_id):
        return claimed_user_id or "user-1", db

    monkeypatch.setattr(chat, "_require_chat_session_access", require_access)
    app = FastAPI()
    app.include_router(chat.router)
    return TestClient(app)


def _patch_voice_config(monkeypatch) -> None:
    from app.api import transcription

    async def resolve(_user_id, _session_id=""):
        return {"model": "voice-model", "base_url": "https://voice.test/v1", "api_key": "key"}

    monkeypatch.setattr(transcription, "_resolve_transcription_config", resolve)


def test_voice_route_confirms_active_change_before_durable_dispatch(monkeypatch) -> None:
    registry = get_voice_session_registry()
    registry.clear()
    with _client(monkeypatch, _VoiceDb(running=True)) as client:
        proposed = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1",
            "session_id": "chat-1",
            "utterance": "Change the database to Postgres",
        })
        assert proposed.status_code == 200
        body = proposed.json()
        assert body["action"] == "request_confirmation"
        assert body["command"] is None
        assert body["ephemeral"] is True
        assert body["proposal"]["state"] == "pending"
        assert body["response_source"] == "system"
        assert body["issue"] is None

        accepted = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1",
            "session_id": "chat-1",
            "voice_session_id": body["voice_session_id"],
            "proposal_id": body["proposal"]["proposal_id"],
            "utterance": "yes",
        })
        assert accepted.status_code == 200
        accepted_body = accepted.json()
        assert accepted_body["action"] == "dispatch_work"
        assert accepted_body["command"] == "Change the database to Postgres"
        assert accepted_body["proposal"]["state"] == "accepted"


def test_voice_route_question_stays_ephemeral(monkeypatch) -> None:
    registry = get_voice_session_registry()
    registry.clear()

    async def answer(*_args, **_kwargs):
        from app.agent.voice_coordinator import VoiceReply
        return VoiceReply("The run is active and testing the change.", "model")

    from app.agent import voice_coordinator
    monkeypatch.setattr(voice_coordinator, "answer_voice_question_result", answer)
    _patch_voice_config(monkeypatch)
    with _client(monkeypatch, _VoiceDb(running=True)) as client:
        response = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1",
            "session_id": "chat-1",
            "utterance": "What is the status?",
        })
    assert response.status_code == 200
    body = response.json()
    assert body["action"] == "answer"
    assert body["answer"] == "The run is active and testing the change."
    assert body["command"] is None
    assert body["ephemeral"] is True
    assert body["response_source"] == "model"
    assert body["issue"] is None

def test_natural_voice_message_uses_model_intent_classifier(monkeypatch) -> None:
    registry = get_voice_session_registry()
    registry.clear()
    _patch_voice_config(monkeypatch)

    async def classify(utterance, *, active_run, config):
        from app.agent.voice_intent import VoiceIntent
        assert utterance == "I would like you to check the tests"
        assert active_run is False
        assert config["model"] == "voice-model"
        return VoiceIntent.NEW_WORK

    from app.agent import voice_coordinator
    monkeypatch.setattr(
        voice_coordinator, "classify_natural_voice_intent", classify,
    )
    with _client(monkeypatch, _VoiceDb(running=False)) as client:
        response = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1",
            "session_id": "chat-1",
            "utterance": "I would like you to check the tests",
        })
    body = response.json()
    assert body["action"] == "dispatch_work"
    assert body["command"] == "I would like you to check the tests"
    assert body["response_source"] == "system"
    assert body["issue"] is None


def test_voice_classifier_failure_is_an_explicit_system_issue(monkeypatch) -> None:
    registry = get_voice_session_registry()
    registry.clear()
    _patch_voice_config(monkeypatch)

    async def classify(*_args, **_kwargs):
        from app.agent.voice_coordinator import VoiceCoordinatorUnavailable
        raise VoiceCoordinatorUnavailable("provider unavailable")

    from app.agent import voice_coordinator
    monkeypatch.setattr(
        voice_coordinator, "classify_natural_voice_intent", classify,
    )
    with _client(monkeypatch, _VoiceDb(running=False)) as client:
        response = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1",
            "session_id": "chat-1",
            "utterance": "Hello there",
        })
    body = response.json()
    assert body["action"] == "request_clarification"
    assert body["response_source"] == "system"
    assert body["issue"] == "intent_classifier_unavailable"
    assert "unavailable" in body["answer"].lower()


def test_voice_route_maps_pause_and_resume_to_explicit_controls(monkeypatch) -> None:
    registry = get_voice_session_registry()
    registry.clear()
    with _client(monkeypatch, _VoiceDb(running=True)) as client:
        paused = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1", "session_id": "chat-1", "utterance": "pause",
        }).json()
    assert paused["action"] == "interrupt"
    assert paused["control_action"] == "pause"

    registry.clear()
    with _client(monkeypatch, _VoiceDb(running=False)) as client:
        resumed = client.post("/api/v1/chat/voice/route", json={
            "user_id": "user-1", "session_id": "chat-1", "utterance": "resume",
        }).json()
    assert resumed["action"] == "interrupt"
    assert resumed["control_action"] == "resume"

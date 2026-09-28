import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import chat


class FakeDB:
    def __init__(self):
        self.model_writes = []
        self.effort_writes = []

    async def assert_session_owned(self, _user_id, _session_id):
        return None

    async def set_session_llm_override(self, session_id, selection):
        self.model_writes.append((session_id, selection))
        return selection or {}

    async def set_session_model_effort(self, session_id, slot_ref, effort):
        self.effort_writes.append((session_id, slot_ref, effort))
        return {"model_effort": {slot_ref: effort or "default"}}


def _request():
    return Request({"type": "http", "method": "POST", "path": "/", "headers": []})


def test_disallowed_session_slot_is_rejected_before_write(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr("app.auth.identity.assert_caller_is", AsyncMock(return_value="user"))
    monkeypatch.setattr(chat, "get_db", lambda: db)
    monkeypatch.setattr(chat, "_entitled_session_model_options", AsyncMock(return_value={
        "slots": [{"type": "role", "role": "standard", "model": "standard"}],
    }))
    req = chat.SessionModelRequest(
        user_id="user", session_id="session", selection_type="role", role="premium"
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(chat.set_session_model(req, _request()))

    assert error.value.detail["code"] == "model_not_allowed"
    assert db.model_writes == []


def test_custom_session_slot_persists_stable_entry_id(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr("app.auth.identity.assert_caller_is", AsyncMock(return_value="user"))
    monkeypatch.setattr(chat, "get_db", lambda: db)
    monkeypatch.setattr(chat, "_entitled_session_model_options", AsyncMock(return_value={
        "slots": [{"type": "custom", "position": 1, "entry_id": "stable-model",
                   "model": "custom"}],
    }))
    req = chat.SessionModelRequest(
        user_id="user", session_id="session", selection_type="custom",
        custom_position=1, entry_id="stable-model",
    )

    asyncio.run(chat.set_session_model(req, _request()))

    assert db.model_writes == [("session", {
        "type": "custom", "position": 1, "entry_id": "stable-model",
    })]


def test_effort_above_tier_cap_is_rejected_before_write(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(chat, "_require_chat_session_access", AsyncMock(return_value=("user", db)))
    monkeypatch.setattr(chat, "_entitled_session_model_options", AsyncMock(return_value={
        "slots": [{"type": "role", "role": "standard", "model": "standard"}],
    }))

    async def capabilities(_user_id, **_kwargs):
        return {"models": {"max_reasoning_effort": "medium"}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    req = chat.SessionEffortRequest(
        user_id="user", session_id="session", slot_ref="role:standard",
        reasoning_effort="high",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(chat.set_session_model_effort(req, _request()))

    assert error.value.detail == {
        "code": "model_not_allowed", "reason": "reasoning_effort", "maximum": "medium",
    }
    assert db.effort_writes == []

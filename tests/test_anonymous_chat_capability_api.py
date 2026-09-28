import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.api import chat


class FakeDB:
    def __init__(self):
        self.activated = []

    async def get_session_active_abilities(self, _session_id):
        return ["codebase_admin"]

    async def get_session_suppressed_abilities(self, _session_id):
        return []

    async def get_agent_ability_modes(self, _agent_id):
        return {}

    async def get_agent_discovery_default(self, _agent_id):
        return "discoverable"

    async def get_agent_connections(self, _agent_id):
        return [{"section": "ability", "enabled": True, "connection_type": "codebase_admin"}]

    async def set_session_active_skill(self, session_id, name, active):
        self.activated.append((session_id, name, active))
        return [name] if active else []

    async def set_session_suppressed_ability(self, *_args):
        raise AssertionError("forbidden ability must not mutate session state")


def run(coro):
    return asyncio.run(coro)


def install_access_stubs(monkeypatch, db):
    async def session_access(_request, user_id, _session_id):
        return user_id, db

    async def agent_access(*_args, **_kwargs):
        return None

    monkeypatch.setattr(chat, "_require_chat_session_access", session_access)
    monkeypatch.setattr(chat, "_require_chat_agent_access", agent_access)


def test_anonymous_skill_catalog_is_empty(monkeypatch):
    db = FakeDB()
    install_access_stubs(monkeypatch, db)
    result = run(chat.chat_skills(
        SimpleNamespace(), "anon_test", "session-1", "shared_default",
    ))
    assert result == {"active": [], "skills": []}


def test_anonymous_cannot_activate_authored_skill(monkeypatch):
    db = FakeDB()
    install_access_stubs(monkeypatch, db)
    req = chat.SkillActivateRequest(
        user_id="anon_test", session_id="session-1", name="codebase_admin_skill",
    )
    with pytest.raises(HTTPException) as exc:
        run(chat.chat_skill_activate(req, SimpleNamespace()))
    assert exc.value.status_code == 403
    assert db.activated == []


def test_tier_denied_ability_is_neither_listed_nor_activatable(monkeypatch):
    db = FakeDB()
    install_access_stubs(monkeypatch, db)

    async def denied(*_args, **_kwargs):
        return set()

    monkeypatch.setattr("app.agent.ability_access.filter_abilities_for_caller", denied)
    monkeypatch.setattr("app.abilities.ui_catalog", lambda: {
        "abilities": {"codebase_admin": {"display_name": "Codebase Admin"}},
    })

    result = run(chat.chat_abilities(
        SimpleNamespace(), "anon_test", "session-1", "shared_default",
    ))
    assert result == {"active": [], "abilities": []}

    req = chat.AbilityToggleRequest(
        user_id="anon_test", session_id="session-1",
        ability_id="codebase_admin", agent_id="shared_default",
    )
    with pytest.raises(HTTPException) as exc:
        run(chat.chat_ability_activate(req, SimpleNamespace()))
    assert exc.value.status_code == 403
    assert exc.value.detail["code"] == "ability_not_available"

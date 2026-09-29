import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.agent.anonymous_data import enforce_anonymous_data_policy
from app.agent.prompts import build_system_prompt_parts
from app.agent.public_policy import public_funding_status, validate_publication
from app.auth.guest_credentials import issue, recover_and_rotate


class SQLiteDB:
    def __init__(self, path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def run(coro):
    return asyncio.run(coro)


def test_guest_credential_recovers_identity_rotates_and_rejects_old_token(tmp_path):
    db = SQLiteDB(tmp_path / "guest.sqlite")
    first = issue(
        db, user_id="anon_1", channel="web_public",
        external_id="browser-1", agent_id="agent-1",
    )
    recovered = recover_and_rotate(
        db, first, channel="web_public", agent_id="agent-1",
    )
    assert recovered["user_id"] == "anon_1"
    assert recovered["external_id"] == "browser-1"
    assert recovered["guest_credential"] != first
    assert recover_and_rotate(
        db, first, channel="web_public", agent_id="agent-1",
    ) is None
    assert recover_and_rotate(
        db, recovered["guest_credential"], channel="web_public", agent_id="other",
    ) is None


def test_custom_public_agent_requires_funding_or_inherited_global_model(monkeypatch):
    from app.agent import public_policy

    async def no_inherited_model(_owner_id):
        return False

    monkeypatch.setattr(public_policy, "_has_inherited_global_model", no_inherited_model)
    agent = {"id": "agent-1", "metadata": {"owner_user_id": "owner-1"}}
    with pytest.raises(HTTPException) as error:
        run(validate_publication(SQLiteDB(":memory:"), agent, {}))
    assert error.value.detail["code"] == "public_agent_funding_required"


def test_inherited_global_model_satisfies_public_funding(monkeypatch):
    from app.admin import settings

    async def global_model(_owner_id):
        # Platform-managed credentials are not exposed as a raw api_key here.
        return {"provider": "platform", "model": "shared-global-model"}

    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"extend_llm_to_agents": True})
    monkeypatch.setattr(settings, "_resolve_user_config", global_model)
    agent = {
        "id": "agent-1",
        "metadata": {"public_access": {"funding": {"owner_user_id": "owner-1"}}},
    }
    status = run(public_funding_status(SQLiteDB(":memory:"), agent))
    assert status["valid"] is True
    assert status["mode"] == "inherited_global"
    assert status["reason"] == "inherited_global"
    published = run(validate_publication(
        SQLiteDB(":memory:"), agent, {"funding": {"owner_user_id": "owner-1"}},
    ))
    assert published["funding"]["mode"] == ""


def test_dedicated_agent_key_satisfies_public_funding():
    agent = {
        "id": "agent-1",
        "metadata": {
            "llm_config": {"use_default": False, "api_key": "secret"},
            "public_access": {"funding": {"mode": "dedicated_key"}},
        },
    }
    status = run(public_funding_status(SQLiteDB(":memory:"), agent))
    assert status["valid"] is True
    assert status["mode"] == "dedicated_key"


def test_anonymous_transcripts_expire_without_deleting_identity(tmp_path):
    db = SQLiteDB(tmp_path / "data.sqlite")
    conn = db._get_conn()
    conn.executescript(
        """CREATE TABLE sessions (
               id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT,
               updated_at TEXT, created_at TEXT
           );
           CREATE TABLE interactions (
               id TEXT PRIMARY KEY, session_id TEXT, content TEXT
           );"""
    )
    old = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("old", "anon_1", "agent-1", old, old))
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("active", "anon_1", "agent-1", now, now))
    conn.execute("INSERT INTO interactions VALUES (?,?,?)", ("i1", "old", "expired"))
    conn.execute("INSERT INTO interactions VALUES (?,?,?)", ("i2", "active", "kept"))
    conn.commit()
    conn.close()

    agent = {
        "id": "agent-1",
        "metadata": {"public_access": {
            "data": {"session_retention_days": 14},
            "funding": {"mode": "dedicated_key"},
        }},
    }
    result = enforce_anonymous_data_policy(
        db, user_id="anon_1", session_id="active", agent=agent,
    )
    assert result["purged_sessions"] == 1
    conn = db._get_conn()
    assert conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()[0]["id"] == "active"
    assert conn.execute("SELECT content FROM interactions").fetchone()["content"] == "kept"
    conn.close()


def test_funded_custom_public_agent_keeps_authored_prompt():
    parts = run(build_system_prompt_parts(
        [{"context_type": "agent", "content": "ACME SUPPORT PERSONA"}],
        user_id="anon_test", agent_id="agent-1",
    ))
    assert "ACME SUPPORT PERSONA" in parts.render()


def test_dedicated_public_key_is_not_clamped_by_anonymous_entitlements(monkeypatch):
    from app.admin import settings

    agent = {
        "id": "agent-1",
        "metadata": {
            "llm_config": {
                "use_default": False,
                "multi_providers": [{
                    "entry_id": "owner-row", "provider": "openai",
                    "model": "owner-public-model", "api_key": "owner-secret",
                    "enabled": True, "text_capable": True,
                }],
                # The owner LITERALLY assigned this row to the Standard role; only
                # a literal assignment may serve as the automatic chat model.
                "role_assignments": {"standard": "owner-row"},
            },
            "public_access": {"funding": {"mode": "dedicated_key"}},
        },
    }

    async def base(_user_id):
        return {"provider": "platform", "model": "showcase-model", "api_key": "platform-secret"}

    async def unchanged(config, _user_id):
        return config

    async def session_override(_session_id):
        return {"use_default": False, "model": "visitor-selected-model"}

    async def capabilities(_user_id, **_kwargs):
        return {"models": {"max_reasoning_effort": "default"}}

    async def already_hydrated(record):
        return record

    monkeypatch.setattr(settings, "_resolve_user_config", base)
    monkeypatch.setattr(settings, "_ensure_tool_capable", unchanged)
    monkeypatch.setattr(settings, "_load_session_override", session_override)
    monkeypatch.setattr(settings, "_hydrate_agent_record_llm", already_hydrated)
    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)

    effective = run(settings.apply_provider_for_run(
        "anon_visitor", agent, "session-1", apply_env=False,
    ))
    assert effective["model"] == "owner-public-model"
    assert effective["api_key"] == "owner-secret"


def test_public_agent_total_transcript_cap_is_enforced(tmp_path):
    db = SQLiteDB(tmp_path / "total-data.sqlite")
    conn = db._get_conn()
    conn.executescript(
        """CREATE TABLE sessions (
               id TEXT PRIMARY KEY, user_id TEXT, agent_id TEXT,
               updated_at TEXT, created_at TEXT
           );
           CREATE TABLE interactions (
               id TEXT PRIMARY KEY, session_id TEXT, content TEXT
           );"""
    )
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("a", "anon_1", "agent-1", now, now))
    conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?)", ("b", "anon_2", "agent-1", now, now))
    conn.execute("INSERT INTO interactions VALUES (?,?,?)", ("i1", "a", "12345"))
    conn.execute("INSERT INTO interactions VALUES (?,?,?)", ("i2", "b", "67890"))
    conn.commit()
    conn.close()
    agent = {"id": "agent-1", "metadata": {"public_access": {"data": {
        "max_transcript_bytes_per_guest": 100,
        "max_total_storage_bytes": 10,
    }}}}
    with pytest.raises(HTTPException) as error:
        enforce_anonymous_data_policy(db, user_id="anon_1", session_id="a", agent=agent)
    assert error.value.detail["code"] == "public_agent_storage_exhausted"


def test_anonymous_billing_gate_reserves_agent_budget_not_visitor_wallet(monkeypatch):
    from app.chat import execution as chat

    calls = []

    async def consume(db, agent, user_id, message):
        calls.append((db, agent["id"], user_id, message))
        return {"valid": True}

    async def visitor_billing(*_args, **_kwargs):
        raise AssertionError("anonymous visitor billing must not be evaluated")

    monkeypatch.setattr("app.agent.public_policy.consume_public_turn_budget", consume)
    monkeypatch.setattr(chat, "billing_check_access", visitor_billing)
    db = object()
    run(chat._enforce_billing_access(db, {"id": "agent-1"}, "anon_1", "hello"))
    assert calls == [(db, "agent-1", "anon_1", "hello")]

"""Phase 4 browser-chat parity and interrupt ownership regressions."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from starlette.requests import Request

from app.api import browser_storage, chat
from app.db.local import LocalBackend


def _request() -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "headers": [],
            "query_string": b"",
        }
    )


def test_browser_session_ids_are_bounded_opaque_identifiers():
    base = {
        "agent_id": "agent",
        "new_message": "hello",
        "idempotency_key": "12345678",
    }
    browser_storage.BrowserChatRequest(**base, session_id="browser-abc_1")
    for invalid in ("../tenant", "has space", "x" * 129):
        with pytest.raises(ValidationError):
            browser_storage.BrowserChatRequest(**base, session_id=invalid)


def test_browser_config_dto_excludes_server_only_and_callable_fields():
    sentinel = "PHASE4_CREDENTIAL_SENTINEL"
    agent = {
        "id": "agent",
        "name": "Safe name",
        "description": "Safe description",
        "model": "safe-model",
        "_config_hash": "abc",
        "context_documents": [{"content": sentinel}],
        "provider_api_key": sentinel,
        "metadata": {"credential": sentinel},
        "abilities_list": ["search"],
    }
    tool = SimpleNamespace(
        handler=lambda: sentinel,
        parameters={"secret": sentinel},
        requires_confirmation=True,
        destructive=True,
        provider_token=sentinel,
    )
    result = browser_storage._public_browser_config(agent, {"search": tool})
    encoded = result.model_dump_json()
    assert sentinel not in encoded
    assert json.loads(encoded)["tools"] == [
        {
            "name": "search",
            "requires_confirmation": True,
            "destructive": True,
        }
    ]


def test_billing_policy_backend_failure_fails_closed():
    with patch.object(
        chat, "billing_check_access", AsyncMock(side_effect=RuntimeError("down"))
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(chat._enforce_billing_access(object(), {"id": "a"}, "u"))
    assert exc.value.status_code == 503
    assert exc.value.detail["code"] == "billing_policy_unavailable"


def test_accessory_session_gate_rejects_wrong_owner_before_mutation():
    db = SimpleNamespace(
        assert_session_owned=AsyncMock(side_effect=PermissionError("denied")),
        is_session_participant=AsyncMock(return_value=False),
    )
    with (
        patch("app.auth.identity.assert_caller_is", AsyncMock(return_value="user-b")),
        patch.object(chat, "get_db", return_value=db),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                chat._require_chat_session_access(
                    _request(), "user-b", "session-a"
                )
            )
    assert exc.value.status_code == 404


def test_interrupt_route_never_calls_run_manager_for_foreign_session():
    db = SimpleNamespace(
        assert_session_owned=AsyncMock(side_effect=PermissionError("denied")),
        is_session_participant=AsyncMock(return_value=False),
    )
    manager = Mock()
    manager.interrupt = AsyncMock()
    with (
        patch("app.auth.identity.assert_caller_is", AsyncMock(return_value="user-b")),
        patch.object(chat, "get_db", return_value=db),
        patch.object(chat, "get_run_manager", return_value=manager),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(
                chat.interrupt_chat(
                    chat.InterruptRequest(session_id="session-a"), _request()
                )
            )
    assert exc.value.status_code == 404
    manager.interrupt.assert_not_awaited()


def test_local_interrupt_missing_session_creates_zero_rows(tmp_path: Path):
    db = LocalBackend(db_path=str(tmp_path / "missing.db"), seed=False)
    with pytest.raises(LookupError):
        asyncio.run(db.set_interrupt("missing"))
    conn = db._get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM session_interrupts"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_interrupt_migration_deletes_only_proven_placeholders(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                title TEXT,
                metadata TEXT,
                agent_id TEXT,
                participants TEXT DEFAULT '[]',
                sort_order INTEGER,
                status TEXT DEFAULT 'active',
                created_at TEXT,
                updated_at TEXT
            );
            CREATE TABLE session_interrupts (
                session_id TEXT PRIMARY KEY,
                interrupt_requested INTEGER,
                created_at TEXT
            );
            INSERT INTO sessions (id, user_id, created_at, updated_at)
                VALUES ('placeholder', '', datetime('now'), datetime('now'));
            INSERT INTO session_interrupts
                VALUES ('placeholder', 1, datetime('now'));
            INSERT INTO sessions
                (id, user_id, title, created_at, updated_at)
                VALUES ('ambiguous', '', 'keep me', datetime('now'), datetime('now'));
            INSERT INTO sessions (id, user_id, created_at, updated_at)
                VALUES ('empty_without_interrupt', '', datetime('now'), datetime('now'));
            INSERT INTO sessions (id, user_id, created_at, updated_at)
                VALUES ('valid', 'user-a', datetime('now'), datetime('now'));
            INSERT INTO session_interrupts
                VALUES ('valid', 1, datetime('now'));
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = LocalBackend(db_path=str(db_path), seed=False)
    conn = db._get_conn()
    try:
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id='placeholder'"
        ).fetchone() is None
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id='ambiguous'"
        ).fetchone() is not None
        assert conn.execute(
            "SELECT 1 FROM sessions WHERE id='empty_without_interrupt'"
        ).fetchone() is not None
        row = conn.execute(
            "SELECT user_id FROM session_interrupts WHERE session_id='valid'"
        ).fetchone()
        assert row["user_id"] == "user-a"
    finally:
        conn.close()

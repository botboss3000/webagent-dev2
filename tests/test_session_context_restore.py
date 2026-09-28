"""Regression coverage for authoritative composer context restoration."""

import asyncio
import sqlite3
from pathlib import Path

import app.admin.settings as settings
import app.db


def test_session_ledger_restores_model_with_context_count() -> None:
    source = (
        Path(__file__).parents[1]
        / "ui" / "shared" / "js" / "chat-activity.js"
    ).read_text(encoding="utf-8")
    assert "if (d.context_model) _currentModelName = String(d.context_model);" in source
    assert "if (model) _currentModelName = String(model);" in source

    loader = (
        Path(__file__).parents[1]
        / "ui" / "chat" / "js" / "session-load.js"
    ).read_text(encoding="utf-8")
    assert "app.setContextTokens(data.context_tokens, data.context_model || '')" in loader
    assert "app.setContextTokens(cached.contextTokens, cached.contextModel || '')" in loader

    backend = (
        Path(__file__).parents[1]
        / "app" / "api" / "db_viewer.py"
    ).read_text(encoding="utf-8")
    assert "if context_tokens and not context_model:" in backend
    assert "context_model = str(_meta[\"model\"])" in backend


def test_session_cost_uses_chronology_not_uuid_order(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "control.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        """CREATE TABLE usage_events (
               id TEXT PRIMARY KEY, user_id TEXT, session_id TEXT, model TEXT,
               input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL,
               cost_source TEXT, source TEXT, created_at TEXT
           )"""
    )
    # Lexically-later UUID/id is chronologically older: ORDER BY id returned the
    # stale 30.7k-style value seen in the composer journey.
    conn.execute(
        "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("z-old", "admin", "s1", "model", 100, 1, 0.01, "catalog", "chat",
         "2026-08-25T10:00:00Z"),
    )
    conn.execute(
        "INSERT INTO usage_events VALUES (?,?,?,?,?,?,?,?,?,?)",
        ("a-new", "admin", "s1", "model", 55, 1, 0.01, "catalog", "chat",
         "2026-08-25T11:00:00Z"),
    )
    conn.commit()
    conn.close()

    class FakeDB:
        def _get_conn(self):
            return sqlite3.connect(db_path)

    monkeypatch.setattr(app.db, "get_app_db", lambda: FakeDB())
    monkeypatch.setattr(settings, "_resolve_user_id", lambda *_args, **_kwargs: "admin")

    result = asyncio.run(
        settings.get_session_cost(
            session_id="s1", authorization="test", token=None,
        )
    )

    assert result["error"] is None
    assert result["context_tokens"] == 55
    assert result["context_model"] == "model"

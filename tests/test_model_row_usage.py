"""MODEL-ROW-USAGE regression tests.

Two saved model rows can share the same provider+model but differ by credential,
reasoning effort, or injected prompt. Per-row usage must key on the stable roster
``entry_id``, never on ``model``/``provider`` — otherwise both rows show the same
combined token/cost total.

These exercise the aggregate endpoints against a real in-memory SQLite
``usage_events`` table via the same ``_get_conn`` surface the production backend
exposes.
"""

import asyncio
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.admin import settings as settings_mod


class FileDB:
    """Tiny shim mirroring LocalBackend's ``_get_conn`` surface."""

    def __init__(self, path):
        self._db_path = path

    def _get_conn(self):
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn


def _seed(path, rows):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE usage_events (
            id TEXT PRIMARY KEY,
            agent_id TEXT NOT NULL,
            user_id TEXT NOT NULL,
            interaction_id TEXT,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            provider_cost_cents INTEGER NOT NULL DEFAULT 0,
            end_user_charge_cents INTEGER NOT NULL DEFAULT 0,
            agent_admin_earnings_cents INTEGER NOT NULL DEFAULT 0,
            strategy TEXT NOT NULL DEFAULT 'free',
            is_byo_llm INTEGER NOT NULL DEFAULT 0,
            is_trial INTEGER NOT NULL DEFAULT 0,
            is_exempt INTEGER NOT NULL DEFAULT 0,
            model TEXT,
            provider TEXT,
            entry_id TEXT,
            cost_usd REAL NOT NULL DEFAULT 0,
            cost_source TEXT,
            credit_units REAL NOT NULL DEFAULT 0,
            credit_source TEXT,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            uncached_input_tokens INTEGER,
            reasoning_tokens INTEGER NOT NULL DEFAULT 0,
            session_id TEXT,
            source TEXT NOT NULL DEFAULT 'chat',
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        """
    )
    conn.executemany(
        "INSERT INTO usage_events (id, agent_id, user_id, input_tokens, output_tokens, "
        "provider_cost_cents, cost_usd, model, provider, entry_id, source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        rows,
    )
    conn.commit()
    conn.close()


def _run(coro):
    return asyncio.run(coro)


def test_usage_events_schema_and_migration_carry_entry_id():
    """The stored-row identity must exist in every schema surface."""
    from pathlib import Path

    from app.db.schema import TABLES

    usage = next(t for t in TABLES if t.name == "usage_events")
    assert any(c.name == "entry_id" for c in usage.columns)
    assert "entry_id" in Path("app/db/local.py").read_text(encoding="utf-8")
    assert "entry_id" in Path("migrations/038_usage_entry_id.sql").read_text(encoding="utf-8")


def test_record_and_charge_persists_entry_id(monkeypatch, tmp_path):
    """The chat/image charge path must stamp the saved row identity."""
    import types

    path = str(tmp_path / "usage.db")
    _seed(path, [])

    class BillingDB:
        def __init__(self):
            self._db_path = path

        def _get_conn(self):
            conn = sqlite3.connect(self._db_path)
            conn.row_factory = sqlite3.Row
            return conn

        async def get_agent_by_id(self, agent_id):
            return {"id": agent_id, "metadata": {}}

        async def get_agent_roles(self, agent_id):
            return {"admin_users": []}

    import app.db as app_db
    monkeypatch.setattr(app_db, "get_app_db", lambda: BillingDB())

    from plugins.billing import charge as charge_mod

    async def _resolve(agent, billing_user_id, usage, cdb, own_llm=None):
        return types.SimpleNamespace(
            end_user_charge_cents=0, wallet_charge_cents=0, trial_used_cents=0,
            is_trial=False, is_exempt=True, is_byo_llm=False, strategy="free",
        )

    monkeypatch.setattr(charge_mod.pricing_mod, "resolve_charge", _resolve)

    async def _split(*a, **k):
        return (0, 0)

    monkeypatch.setattr(charge_mod, "apply_split", _split)

    async def _record(*a, **k):
        return None

    monkeypatch.setattr(charge_mod, "apply_record", _record)

    result = _run(charge_mod.record_and_charge(
        BillingDB(), "agent-1", "admin",
        input_tokens=7, output_tokens=3, model="gpt-5",
        provider="openai_subscription", entry_id="row-a",
    ))
    assert result is not None

    conn = sqlite3.connect(path)
    try:
        row = conn.execute(
            "SELECT model, provider, entry_id, input_tokens FROM usage_events"
        ).fetchone()
    finally:
        conn.close()
    assert row == ("gpt-5", "openai_subscription", "row-a", 7)


def test_model_usage_separates_same_model_by_entry_id(monkeypatch, tmp_path):
    path = str(tmp_path / "usage.db")
    _seed(path, [
        # Two rows, identical provider+model, different saved connections.
        ("u1", "a1", "admin", 100, 10, 0, 0.01, "gpt-5", "openai_subscription", "row-a", "chat"),
        ("u2", "a1", "admin", 900, 90, 0, 0.09, "gpt-5", "openai_subscription", "row-b", "chat"),
        # Legacy row written before entry_id existed: NULL identity.
        ("u3", "a1", "admin", 50, 5, 0, 0.005, "gpt-5", "openai_subscription", None, "chat"),
    ])
    monkeypatch.setattr(settings_mod, "_resolve_user_id", lambda *a, **k: "admin")

    import app.db as app_db
    monkeypatch.setattr(app_db, "get_app_db", lambda: FileDB(path))

    row_a = _run(settings_mod.get_model_usage(
        model="gpt-5", provider="openai_subscription", entry_id="row-a",
        include_unassigned=0, scope="user"))
    row_b = _run(settings_mod.get_model_usage(
        model="gpt-5", provider="openai_subscription", entry_id="row-b",
        include_unassigned=0, scope="user"))

    assert row_a["total_input_tokens"] == 100 and row_a["total_output_tokens"] == 10
    assert row_b["total_input_tokens"] == 900 and row_b["total_output_tokens"] == 90
    assert row_a["total_input_tokens"] != row_b["total_input_tokens"]

    # The sole-row fallback folds the legacy NULL-entry bucket back in.
    row_b_legacy = _run(settings_mod.get_model_usage(
        model="gpt-5", provider="openai_subscription", entry_id="row-b",
        include_unassigned=1, scope="user"))
    assert row_b_legacy["total_input_tokens"] == 950


def test_agent_usage_breaks_down_by_entry_id(monkeypatch, tmp_path):
    path = str(tmp_path / "usage.db")
    _seed(path, [
        ("u1", "agent-1", "admin", 100, 10, 0, 0.01, "gpt-5", "openai_subscription", "row-a", "chat"),
        ("u2", "agent-1", "admin", 900, 90, 0, 0.09, "gpt-5", "openai_subscription", "row-b", "chat"),
        ("u3", "agent-1", "admin", 50, 5, 0, 0.005, "gpt-5", "openai_subscription", None, "chat"),
        ("u4", "agent-other", "admin", 5, 5, 0, 0.001, "gpt-5", "openai_subscription", "row-a", "chat"),
    ])
    monkeypatch.setattr(settings_mod, "_resolve_user_id", lambda *a, **k: "admin")

    import app.db as app_db
    monkeypatch.setattr(app_db, "get_app_db", lambda: FileDB(path))

    payload = _run(settings_mod.get_agent_usage(agent_id="agent-1"))
    assert payload["by_entry"]["row-a"]["input"] == 100
    assert payload["by_entry"]["row-b"]["input"] == 900
    # by_model keeps ALL rows for the model — it is the legacy fallback bucket.
    assert payload["by_model"]["gpt-5"]["input"] == 1050

import asyncio
from pathlib import Path

import pytest

from app import abilities
from plugins.abilities.Memory.sql_databases import sql_databases


def run(coro):
    return asyncio.run(coro)


def test_sql_databases_is_real_knowledge_ability_and_legacy_placeholders_are_gone():
    abilities.reload()
    catalog = abilities.ui_catalog()
    knowledge = next(group for group in catalog["groups"] if group["id"] == "memory")
    item = catalog["abilities"]["sql_databases"]

    assert item["placeholder"] is False
    assert item["ui_panel"] == "sql_connections"
    assert item["risk_class"] == "read"
    assert "sql_databases" in knowledge["members"]
    assert "memory" in knowledge["members"]
    assert "context_control" in knowledge["members"]
    assert "wiki_context" in knowledge["members"]
    assert "notion" not in catalog["abilities"]
    assert "airtable" not in catalog["abilities"]
    assert "google_sheets" not in catalog["abilities"]

    root = Path(__file__).resolve().parents[1]
    assert not (root / "ui/shared/js/data-sources.js").exists()
    assert not (root / "app/api/data_sources.py").exists()
    assert not (root / "app/connectors/__init__.py").exists()
    assert "data-sources.js" not in (root / "index.html").read_text(encoding="utf-8")


def test_sql_ability_router_is_discovered():
    abilities.reload()
    assert any(item["id"] == "sql_databases" for item in abilities.ability_routers())


def test_read_query_validation_enforces_statement_and_table_allowlist():
    safe = sql_databases._validate_read_query(
        "WITH recent AS (SELECT id FROM sales.orders) SELECT * FROM recent",
        ["sales.orders"],
    )
    assert "sales.orders" in safe.lower()

    with pytest.raises(ValueError, match="not approved"):
        sql_databases._validate_read_query(
            "SELECT * FROM private.payroll",
            ["sales.orders"],
        )

    with pytest.raises(ValueError, match="not approved"):
        sql_databases._validate_read_query(
            "SELECT * FROM private.orders",
            ["sales.orders"],
        )

    with pytest.raises(ValueError, match="not approved"):
        sql_databases._validate_read_query(
            "SELECT * FROM orders",
            ["sales.orders"],
        )

    with pytest.raises(ValueError, match="read-only"):
        sql_databases._validate_read_query(
            "DELETE FROM sales.orders",
            ["sales.orders"],
        )


def test_sql_query_tool_respects_agent_settings_and_result_cap(monkeypatch):
    profile = {
        "id": "pg_primary", "name": "Primary", "allowed_tables": ["public.orders"],
        "row_limit": 500,
    }
    calls = []

    async def find(_agent_id, _ref, include_secret=True):
        return profile

    async def execute(_profile, query, params, row_limit=None):
        calls.append((query, params, row_limit))
        return [{"id": 1}]

    async def settings(_agent_id):
        return {"sql_query_enabled": True, "sql_max_rows": 25}

    monkeypatch.setattr(sql_databases, "_find_profile", find)
    monkeypatch.setattr(sql_databases, "_run_query", execute)
    monkeypatch.setattr(sql_databases, "_runtime_settings", settings)

    tools = sql_databases.build_tools(agent_id="agent-1")
    result = run(tools["sql_query"](
        connection="Primary", query="SELECT * FROM public.orders", params=[],
    ))
    payload = __import__("json").loads(result)

    assert payload["status"] == "ok"
    assert payload["rows"] == [{"id": 1}]
    assert calls == [("SELECT * FROM public.orders", [], 25)]

    async def disabled(_agent_id):
        return {"sql_query_enabled": False}

    monkeypatch.setattr(sql_databases, "_runtime_settings", disabled)
    denied = __import__("json").loads(run(tools["sql_query"](
        connection="Primary", query="SELECT * FROM public.orders",
    )))
    assert denied["status"] == "error"
    assert "disabled" in denied["message"].lower()


def test_sql_prompt_context_is_bounded_and_cites_source(monkeypatch):
    profile = {
        "id": "pg_primary", "name": "Product DB", "password": "secret",
        "auto_recall": True, "allowed_tables": ["public.products"],
        "recall_table": "public.products", "recall_title_column": "name",
        "recall_content_columns": ["description"],
    }

    async def settings(_agent_id):
        return {"sql_recall_enabled": True}

    async def profiles(_agent_id, include_secret=False):
        return [profile]

    async def search(_profile, _query, _limit):
        return [{"_identity": 42, "_title": "Enterprise", "_content": "Supports SSO."}]

    monkeypatch.setattr(sql_databases, "_runtime_settings", settings)
    monkeypatch.setattr(sql_databases, "_profiles", profiles)
    monkeypatch.setattr(sql_databases, "_search_profile", search)

    context = run(sql_databases.build_prompt_context(
        agent_id="agent-1", user_id="user-1", query="Does it support SSO?",
    ))

    assert "Relevant SQL knowledge" in context
    assert "Product DB · public.products · row 42" in context
    assert "Supports SSO." in context

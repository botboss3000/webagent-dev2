from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import json
from types import SimpleNamespace

from app.api.agents import SoftAbilityRequest, _validate_soft_ability
from app.db.local import LocalBackend


def run(coro):
    return asyncio.run(coro)


def test_soft_ability_validation_rejects_ungranted_workflow_tool():
    body = SoftAbilityRequest(
        user_id="u",
        slug="dropbox_manager",
        display_name="Dropbox Manager",
        skill_body="Manage Dropbox safely.",
        status="ready",
        allowed_tools=["browser_navigate"],
        workflow={"steps": [{"action": "tool.call", "tool": "terminal_exec"}]},
    )
    errors = _validate_soft_ability(body)
    assert any("not present in allowed_tools" in error for error in errors)


def test_soft_ability_storage_round_trip_and_versioning():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "soft.db")
        db = LocalBackend(db_path=path)
        conn = sqlite3.connect(path)
        conn.execute("INSERT INTO agents(id, name) VALUES ('agent-1', 'Agent')")
        conn.commit()
        conn.close()

        payload = {
            "agent_id": "agent-1",
            "slug": "dropbox_manager",
            "display_name": "Dropbox Manager",
            "skill_summary": "Use for Dropbox tasks.",
            "skill_body": "Manage Dropbox safely.",
            "allowed_tools": ["browser_navigate"],
            "workflow": {"steps": [{"action": "tool.call", "tool": "browser_navigate"}]},
            "status": "ready",
            "created_by": "user-1",
        }
        first = run(db.upsert_agent_soft_ability(payload))
        assert first["version"] == 1
        assert first["allowed_tools"] == ["browser_navigate"]
        assert len(run(db.get_agent_soft_abilities("agent-1", enabled_only=True))) == 1

        first["description"] = "Updated"
        first["created_by"] = "user-1"
        second = run(db.upsert_agent_soft_ability(first))
        assert second["version"] == 2
        assert second["description"] == "Updated"

        assert run(db.delete_agent_soft_ability("agent-1", first["id"])) is True
        assert run(db.get_agent_soft_abilities("agent-1")) == []


def test_executor_intersects_allowlist_and_resolves_inputs():
    async def scenario():
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "execute.db")
            db = LocalBackend(db_path=path)
            conn = sqlite3.connect(path)
            conn.execute("INSERT INTO agents(id, name) VALUES ('agent-1', 'Agent')")
            conn.commit()
            conn.close()
            saved = await db.upsert_agent_soft_ability({
                "agent_id": "agent-1", "slug": "echo_workflow", "display_name": "Echo",
                "skill_body": "Echo a value.", "allowed_tools": ["echo"], "status": "ready",
                "workflow": {"steps": [
                    {"id": "echoed", "action": "tool.call", "tool": "echo",
                     "args": {"value": "${inputs.message}"}},
                    {"action": "return", "value": "${steps.echoed}"},
                ]}, "created_by": "user-1",
            })
            from app.abilities.soft_workflows import execute_soft_ability

            async def fake_load_tools(*args, **kwargs):
                async def echo(value):
                    return {"echo": value}
                async def forbidden():
                    return "should never run"
                return {
                    "echo": SimpleNamespace(handler=echo),
                    "forbidden": SimpleNamespace(handler=forbidden),
                }
            result = json.loads(await execute_soft_ability(
                ability_id=saved["id"], inputs={"message": "hello"},
                user_id="user-1", agent_id="agent-1", session_id="session-1",
                _db=db, _load_tools=fake_load_tools))
            assert result["status"] == "ok"
            assert result["result"] == {"echo": "hello"}
            assert result["tools_executed"] == ["echo"]
            conn = sqlite3.connect(path)
            audit = conn.execute("SELECT ability_version, status, tools FROM soft_ability_runs").fetchone()
            conn.close()
            assert audit == (1, "ok", '["echo"]')

    run(scenario())

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.agent import task_state
from app.agent.turn_reservations import stable_key
from app.mcp.bridge import execute_mcp_tool
from app.tools.execution_context import current_tool_context
from tests.test_task_state import Db


def seeded(tmp_path):
    db = Db(tmp_path / "mcp.db")
    task_state.begin_task(db, session_id="s", turn_id="t1", task_id="task1",
                         source_messages=[{"interaction_id": "u", "content": "Inspect and change A"}],
                         mode="auto")
    state = task_state.current_state(db, "s")
    task_state.update_checkpoint(db, "s", "t1", state["revision"], {
        "steps": [{"id": "P1", "text": "Inspect and change A", "requirement_ids": ["R1"],
                   "depends_on": [], "verification": "Inspect result"}],
        "active_step": "P1", "next_action": "Apply change",
    })
    return db


def test_checkpoint_handler_receives_pinned_turn_key(tmp_path):
    db = seeded(tmp_path)
    async def handler():
        ctx = current_tool_context()
        assert ctx.turn_key == stable_key("chat-turn", "user", "s", "t1")
        state = task_state.current_state(db, "s")
        task_state.update_checkpoint(db, "s", "t1", state["revision"], {"next_action": "Inspect"})
        return {"status": "ok"}
    result = asyncio.run(execute_mcp_tool(
        "update_task_checkpoint", {}, {"update_task_checkpoint": (handler, {})},
        "user", "s", expected_turn_id="t1", db=db,
    ))
    assert not result["isError"]
    state = task_state.current_state(db, "s")
    assert state["checkpoint"]["next_action"] == "Inspect"
    assert state["evidence"] == []


def test_replaced_mcp_process_cannot_execute_or_rebind(tmp_path):
    db = seeded(tmp_path)
    task_state.begin_task(db, session_id="s", turn_id="t2", task_id="task2",
                         source_messages=[{"interaction_id": "u2", "content": "Other task"}],
                         mode="auto")
    handler = AsyncMock(return_value="forbidden")
    result = asyncio.run(execute_mcp_tool(
        "write_source", {}, {"write_source": (handler, {"destructive": True})},
        "user", "s", expected_turn_id="t1", db=db,
    ))
    assert result["isError"]
    handler.assert_not_called()
    assert task_state.current_state(db, "s")["turn_id"] == "t2"


def test_mutation_intent_precedes_handler_and_records_outcome(tmp_path):
    db = seeded(tmp_path)
    call_ids = []
    async def handler():
        state = task_state.current_state(db, "s")
        call_ids.append(current_tool_context().tool_call_id)
        assert state["checkpoint"]["pending_operations"][0]["id"] == call_ids[-1]
        return {"success": True}
    for _ in range(2):
        result = asyncio.run(execute_mcp_tool(
            "write_source", {}, {"write_source": (handler, {"destructive": True})},
            "user", "s", expected_turn_id="t1", db=db,
        ))
        assert not result["isError"]
    state = task_state.current_state(db, "s")
    assert len(set(call_ids)) == 2
    assert state["checkpoint"]["pending_operations"] == []
    assert len(state["evidence"]) == 2
    assert state["evidence"][-1]["tool_call_id"] == call_ids[-1]


def test_handler_failure_retains_uncertainty_and_failed_evidence(tmp_path):
    db = seeded(tmp_path)
    async def handler():
        raise RuntimeError("partial failure")
    result = asyncio.run(execute_mcp_tool(
        "write_source", {}, {"write_source": (handler, {"destructive": True})},
        "user", "s", expected_turn_id="t1", db=db,
    ))
    assert result["isError"]
    state = task_state.current_state(db, "s")
    assert state["checkpoint"]["pending_operations"][0]["status"] == "uncertain"
    assert state["evidence"][0]["success"] is False


def test_reported_failure_is_not_successful_mutation(tmp_path):
    db = seeded(tmp_path)
    result = asyncio.run(execute_mcp_tool(
        "run_command", {}, {"run_command": (AsyncMock(return_value={"exit_code": 1}),
                                           {"destructive": True})},
        "user", "s", expected_turn_id="t1", db=db,
    ))
    state = task_state.current_state(db, "s")
    assert state["checkpoint"]["pending_operations"][0]["status"] == "uncertain"
    assert state["evidence"][0]["success"] is False


def test_legacy_call_without_task_never_opens_db(monkeypatch):
    import app.db
    monkeypatch.setattr(app.db, "get_db", lambda: (_ for _ in ()).throw(AssertionError("no DB")))
    result = asyncio.run(execute_mcp_tool(
        "inspect", {}, {"inspect": (AsyncMock(return_value="ok"), {})}, "user", "s",
    ))
    assert not result["isError"]


def test_server_pins_generation_once_even_with_empty_tool_set(tmp_path, monkeypatch):
    from app.mcp import server, bridge
    import app.db
    db = seeded(tmp_path)
    monkeypatch.setattr(app.db, "get_db", lambda: db)
    loader = AsyncMock(return_value={})
    monkeypatch.setattr(bridge, "resolve_tools_for_engine", loader)
    monkeypatch.setattr(server, "_tools_initialized", False)
    monkeypatch.setattr(server, "_tools_lock", asyncio.Lock())
    monkeypatch.setattr(server, "_tools", {})
    monkeypatch.setattr(server, "_expected_turn_id", None)
    monkeypatch.setattr(server, "_ARGS", SimpleNamespace(
        user_id="user", agent_id="agent", session_id="s", allowed_tools="",
    ), raising=False)
    asyncio.run(server._ensure_tools())
    task_state.begin_task(db, session_id="s", turn_id="t2", task_id="task2",
                         source_messages=[{"interaction_id": "u2", "content": "Other task"}],
                         mode="auto")
    asyncio.run(server._ensure_tools())
    assert server._expected_turn_id == "t1"
    loader.assert_awaited_once()


def test_mutation_without_actionable_plan_is_blocked(tmp_path):
    db = seeded(tmp_path)
    state = task_state.current_state(db, "s")
    task_state.update_checkpoint(db, "s", "t1", state["revision"],
                                 {"active_step": "", "next_action": ""})
    handler = AsyncMock(return_value="must not run")
    result = asyncio.run(execute_mcp_tool(
        "write_source", {}, {"write_source": (handler, {"destructive": True})},
        "user", "s", expected_turn_id="t1", db=db,
    ))
    assert result["isError"]
    handler.assert_not_called()
    assert task_state.current_state(db, "s")["checkpoint"]["pending_operations"] == []

def test_dispatch_preserves_launcher_user_workspace(monkeypatch):
    from app.mcp import server
    from app.auth.workspace import current_workspace
    monkeypatch.setattr(server, "_ARGS", SimpleNamespace(user_id="owner"), raising=False)
    async def handler(msg):
        assert current_workspace().subject_id == "owner"
        return {"ok": True}
    monkeypatch.setitem(server._METHOD_TABLE, "tools/list", handler)
    assert asyncio.run(server._dispatch({"id":1,"method":"tools/list"})) == {"ok":True}
    assert current_workspace() is None

def test_read_checkpoint_does_not_invalidate_returned_revision(tmp_path):
    db=seeded(tmp_path)
    async def handler():
        return task_state.current_state(db,"s")
    before=task_state.current_state(db,"s")
    result=asyncio.run(execute_mcp_tool(
        "get_task_checkpoint",{},{"get_task_checkpoint":(handler,{})},
        "user","s",expected_turn_id="t1",db=db))
    assert not result["isError"]
    assert task_state.current_state(db,"s")["revision"] == before["revision"]

def test_mcp_requires_checkpoint_after_three_outcomes(tmp_path):
    db=seeded(tmp_path)
    for _ in range(3):
        task_state.record_evidence(db,"s","t1",{"kind":"inspection","success":True})
    handler=AsyncMock(return_value="should not execute")
    result=asyncio.run(execute_mcp_tool(
        "read_source",{},{"read_source":(handler,{})},
        "user","s",expected_turn_id="t1",db=db))
    assert result["isError"]
    assert "Checkpoint due" in result["content"][0]["text"]
    handler.assert_not_called()

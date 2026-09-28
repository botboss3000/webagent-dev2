import asyncio
import json
import sqlite3
from pathlib import Path

from app.agent import run_handoff, run_scout


class _Db:
    def __init__(self, path: Path):
        self.path = str(path)
        self.run_state = None

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def run_state_get(self, _session_id):
        return self.run_state

    async def get_session_execution_mode_history(self, _session_id):
        return ["plan", "auto"]


def _begin(db, turn="u1", message="Build it", agent_rec=None):
    return asyncio.run(run_scout.begin_turn(
        db, session_id="s1", user_id="owner", agent_id="a1",
        turn_id=turn, message=message, execution_mode="auto",
        replaced=False, agent_rec=agent_rec,
    ))


def teardown_function(_fn):
    run_scout.reset_task_registry_for_tests()


def test_capsule_persists_and_round_trips_machine_fields(tmp_path):
    db = _Db(tmp_path / "handoff.db")
    _begin(db)
    db.run_state = {"turn_id": "u1", "stop_cause": "complete"}

    saved = asyncio.run(run_handoff.persist_capsule(
        db, session_id="s1", turn_id="u1", run_id="a-final",
        status="complete", execution_mode="auto", objective="Build it",
        completed=["Implemented"], open_requirements=["Document it"],
        decisions=["Use one-shot calls"], changed_paths=["app/x.py"],
        verification=[{"tool": "pytest", "success": True}],
        next_action="Document it", summary="Implementation is ready.",
        source="closer",
    ))

    assert saved["task_id"].startswith("scout-")
    assert saved["run_id"] == "a-final"
    assert saved["mode_history"] == ["plan", "auto"]
    assert saved["open_requirements"] == ["Document it"]
    assert run_handoff.recent_capsules(db, "s1")[0]["summary"] == "Implementation is ready."


def test_user_stop_never_writes_handoff(tmp_path):
    db = _Db(tmp_path / "handoff.db")
    _begin(db)
    db.run_state = {"turn_id": "u1", "stop_cause": "user_stop"}

    saved = asyncio.run(run_handoff.persist_run_outcome(
        db, "u1", "interrupted", "user_stop",
    ))

    assert saved is None
    assert run_handoff.recent_capsules(db, "s1") == []


def test_stale_generation_cannot_write_handoff(tmp_path):
    db = _Db(tmp_path / "handoff.db")
    _begin(db)
    db.run_state = {"turn_id": "u2", "stop_cause": None}

    saved = asyncio.run(run_handoff.persist_capsule(
        db, session_id="s1", turn_id="u1", status="complete",
    ))

    assert saved is None
    assert run_handoff.recent_capsules(db, "s1") == []


def test_next_starter_receives_prior_capsule_in_existing_model_call(tmp_path, monkeypatch):
    db = _Db(tmp_path / "handoff.db")
    _begin(db)
    db.run_state = {"turn_id": "u1", "stop_cause": "complete"}
    capsule = asyncio.run(run_handoff.persist_capsule(
        db, session_id="s1", turn_id="u1", status="complete",
        objective="Build the manager loop", open_requirements=["Add UI"],
        summary="Backend is finished.",
    ))
    agent = {"metadata": {"manager": {"starter": {
        "enabled": True, "inherit_prior_summary": True,
        "seed_plan": True, "seed_checklist": True,
    }}}}
    second = _begin(db, turn="u2", message="Now add the UI", agent_rec=agent)
    captured = {}

    class _Message:
        content = json.dumps({
            "objective": "Add the UI", "task_type": "change",
            "relationship": "continue",
            "linked_prior_task_id": capsule["task_id"],
            "linked_capsule_id": capsule["id"],
            "relationship_confidence": 0.95,
            "relationship_reason": "It completes the same feature.",
        })
    class _Response:
        choices = [type("Choice", (), {"message": _Message()})()]
        usage = None
    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Response()
    class _Client:
        chat = type("Chat", (), {"completions": _Completions()})()

    async def _resolve(_user_id):
        return "fast-model", "provider", _Client()

    monkeypatch.setattr("app.agent.manager._resolve_llm", _resolve)
    artifact, *_ = asyncio.run(run_scout._call_model(second))

    assert capsule["id"] in captured["messages"][1]["content"]
    assert "Add UI" in captured["messages"][1]["content"]
    assert "tools" not in captured
    assert artifact["relationship"] == "continue"
    assert artifact["linked_capsule_id"] == capsule["id"]


def test_wait_before_write_returns_only_configured_seed_fields(tmp_path):
    db = _Db(tmp_path / "handoff.db")
    agent = {"metadata": {"manager": {"starter": {
        "enabled": True, "wait_before_write": True,
        "seed_plan": False, "seed_checklist": True,
    }}}}
    row = _begin(db, agent_rec=agent)
    assert run_scout.persist_analysis(db, row["id"], row["revision"], {
        "objective": "Build it", "task_type": "change",
        "plan": [{"id": "P1", "text": "Edit code"}],
        "success_criteria": [{"id": "C1", "text": "Tests pass"}],
        "relationship": "new",
    })

    seeded = asyncio.run(run_scout.await_write_ready(db, "u1"))

    assert seeded["ready"] is True
    assert seeded["plan"] == []
    assert seeded["checklist"] == [{"id": "C1", "text": "Tests pass"}]


def test_main_loop_consumes_starter_at_first_write_gate():
    source = Path("app/agent/loop.py").read_text(encoding="utf-8")
    assert "await_write_ready as _await_starter_for_write" in source
    assert 'if is_edit_tool(tool_name) and not _manager_state.get("starter_write_checked")' in source
    assert "starter_context_ready" in source

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.agent import run_scout


class _Db:
    def __init__(self, path: Path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _begin(db, *, turn="u1", message="Build the feature", replaced=False, mode="ask"):
    return asyncio.run(run_scout.begin_turn(
        db,
        session_id="session-1",
        user_id="user-1",
        agent_id="agent-1",
        turn_id=turn,
        message=message,
        execution_mode=mode,
        replaced=replaced,
    ))


def _artifact(objective="Build the requested feature"):
    return {
        "objective": objective,
        "task_type": "change",
        "constraints": ["Run tests"],
        "assumptions": [],
        "questions": [],
        "success_criteria": [{"id": "C1", "text": "Focused tests pass"}],
        "plan": [{"id": "P1", "text": "Implement phase one"}],
        "expected_outputs": ["Code", "Tests"],
        "risks": [],
        "title_candidate": "Run Scout Phase One",
    }


def teardown_function(_fn):
    run_scout.reset_task_registry_for_tests()


def test_first_message_creates_one_durable_starter(tmp_path):
    db = _Db(tmp_path / "scout.db")

    row = _begin(db)

    assert row["revision"] == 1
    assert row["root_interaction_id"] == "u1"
    assert row["active_turn_id"] == "u1"
    assert row["combined_request"] == "Build the feature"
    assert row["source_messages"] == [{
        "interaction_id": "u1",
        "content": "Build the feature",
        "received_at": row["source_messages"][0]["received_at"],
    }]
    assert row["analysis_status"] == "queued"
    assert row["run_status"] == "running"


def test_replacement_messages_become_one_combined_starter(tmp_path):
    db = _Db(tmp_path / "scout.db")
    first = _begin(db)

    second = _begin(
        db, turn="u2", message="Also cover server restarts", replaced=True,
        mode="plan",
    )
    third = _begin(
        db, turn="u3", message="And preserve Stop semantics", replaced=True,
        mode="auto",
    )

    assert second["id"] == first["id"]
    assert third["id"] == first["id"]
    assert third["revision"] == 3
    assert third["root_interaction_id"] == "u1"
    assert third["active_turn_id"] == "u3"
    assert third["execution_mode"] == "auto"
    assert [m["interaction_id"] for m in third["source_messages"]] == ["u1", "u2", "u3"]
    assert "[Opening message]\nBuild the feature" in third["combined_request"]
    assert "[Follow-up 1]\nAlso cover server restarts" in third["combined_request"]
    assert "[Follow-up 2]\nAnd preserve Stop semantics" in third["combined_request"]


def test_stale_scout_completion_cannot_overwrite_newer_intent(tmp_path):
    db = _Db(tmp_path / "scout.db")
    first = _begin(db)
    second = _begin(db, turn="u2", message="Use a different approach", replaced=True)

    assert not run_scout.persist_analysis(db, first["id"], 1, _artifact("Old objective"))
    assert run_scout.persist_analysis(db, second["id"], 2, _artifact("Combined objective"))

    current = run_scout.latest_artifact(db, "session-1")
    assert current["artifact"]["objective"] == "Combined objective"
    assert current["analysis_status"] == "complete"


def test_voluntary_stop_closes_bundle_and_next_message_starts_fresh(tmp_path):
    db = _Db(tmp_path / "scout.db")
    first = _begin(db)

    asyncio.run(run_scout.stop_turn(db, "session-1", "u1", "user_stop"))
    stopped = run_scout.latest_artifact(db, "session-1")
    second = _begin(db, turn="u2", message="A genuinely new task", replaced=False)

    assert stopped["run_status"] == "stopped"
    assert stopped["analysis_status"] == "stopped"
    assert second["id"] != first["id"]
    assert second["revision"] == 1
    assert second["combined_request"] == "A genuinely new task"


def test_message_after_recoverable_error_extends_existing_starter(tmp_path):
    db = _Db(tmp_path / "scout.db")
    first = _begin(db)
    asyncio.run(run_scout.mark_run_outcome(db, "u1", "error", "crash"))

    continued = _begin(
        db, turn="u2", message="Continue, but avoid the failed route", replaced=False,
    )

    assert continued["id"] == first["id"]
    assert continued["revision"] == 2
    assert [m["interaction_id"] for m in continued["source_messages"]] == ["u1", "u2"]


def test_revive_reuses_bundle_and_retries_only_unfinished_analysis(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    first = _begin(db)
    asyncio.run(run_scout.mark_run_outcome(db, "u1", "error", "server_restart"))
    launched = []
    monkeypatch.setattr(run_scout, "launch_analysis", lambda _db, row: launched.append(row))

    revived = asyncio.run(run_scout.revive_turn(db, "u1"))

    assert revived["id"] == first["id"]
    assert revived["revision"] == 1
    assert launched and launched[0]["id"] == first["id"]
    assert run_scout.latest_artifact(db, "session-1")["run_status"] == "resuming"


def test_revive_does_not_rerun_completed_analysis(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    row = _begin(db)
    assert run_scout.persist_analysis(db, row["id"], row["revision"], _artifact())
    asyncio.run(run_scout.mark_run_outcome(db, "u1", "error", "frozen"))
    launched = []
    monkeypatch.setattr(run_scout, "launch_analysis", lambda _db, value: launched.append(value))

    revived = asyncio.run(run_scout.revive_turn(db, "u1"))

    assert revived["analysis_status"] == "complete"
    assert launched == []


def test_tool_free_analysis_normalizes_and_persists_model_result(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    row = _begin(db)

    async def _fake_call(_row):
        return _artifact(), None, "fake-model", "fake-provider"

    monkeypatch.setattr(run_scout, "_scout_enabled", lambda: True)
    monkeypatch.setattr(run_scout, "_call_model", _fake_call)

    async def _run():
        task = run_scout.launch_analysis(db, row)
        await task

    asyncio.run(_run())
    saved = run_scout.latest_artifact(db, "session-1")

    assert saved["analysis_status"] == "complete"
    assert saved["artifact"]["version"] == 1
    assert "first_response" not in saved["artifact"]
    assert saved["artifact"]["success_criteria"] == [{
        "id": "C1", "text": "Focused tests pass",
    }]
    assert saved["artifact"]["plan"] == [{
        "id": "P1", "text": "Implement phase one", "status": "pending",
    }]


def test_model_prompt_keeps_literal_json_and_exposes_no_tools(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    row = _begin(db)
    captured = {}

    class _Message:
        content = '{"objective":"Do it","task_type":"change"}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]
        usage = None

    class _Completions:
        async def create(self, **kwargs):
            captured.update(kwargs)
            return _Response()

    class _Client:
        class _Chat:
            completions = _Completions()

        chat = _Chat()

    async def _resolve(_user_id):
        return "model", "provider", _Client()

    monkeypatch.setattr("app.agent.manager._resolve_llm", _resolve)
    parsed, _response, _model, _provider = asyncio.run(run_scout._call_model(row))

    assert parsed["objective"] == "Do it"
    assert "first_response" not in parsed
    assert '"objective": "one concrete description' in captured["messages"][0]["content"]
    assert "first_response" not in captured["messages"][0]["content"]
    assert "Build the feature" in captured["messages"][1]["content"]
    assert "tools" not in captured


def test_subscription_scout_uses_shared_completion(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    row = _begin(db)
    from app.agent import provider_completion
    from app.agent.provider_completion import SubscriptionResponseClient

    class _Message:
        content = '{"objective":"Do it","task_type":"change"}'

    class _Response:
        choices = [type("Choice", (), {"message": _Message()})()]
        usage = None

    client = SubscriptionResponseClient(
        scope="platform", entry_id="entry-1", user_id="user-1", agent_id="",
    )
    seen = {}

    async def _resolve(_user_id):
        return "gpt-5.6-terra", "openai_subscription", client

    async def _complete(actual_client, **kwargs):
        seen["client"] = actual_client
        seen.update(kwargs)
        return _Response()

    monkeypatch.setattr("app.agent.manager._resolve_llm", _resolve)
    monkeypatch.setattr(provider_completion, "complete_text", _complete)

    parsed, _response, model, provider = asyncio.run(run_scout._call_model(row))

    assert parsed["objective"] == "Do it"
    assert model == "gpt-5.6-terra"
    assert provider == "openai_subscription"
    assert seen["client"] is client
    assert seen["messages"][0]["role"] == "system"


def test_recovery_sweep_revives_scout_even_when_main_run_completed(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    row = _begin(db)
    asyncio.run(run_scout.mark_run_outcome(db, "u1", "complete", "complete"))
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn = db._get_conn()
    conn.execute("UPDATE run_scout_artifacts SET updated_at=? WHERE id=?", (old, row["id"]))
    conn.commit()
    conn.close()
    launched = []
    monkeypatch.setattr(run_scout, "_scout_enabled", lambda: True)

    def _launch(_db, candidate):
        launched.append(candidate)
        return object()

    monkeypatch.setattr(run_scout, "launch_analysis", _launch)

    count = asyncio.run(run_scout._sweep_once(db))

    assert count == 1
    assert launched[0]["id"] == row["id"]


def test_recovery_sweep_never_revives_user_stopped_starter(tmp_path, monkeypatch):
    db = _Db(tmp_path / "scout.db")
    row = _begin(db)
    asyncio.run(run_scout.stop_turn(db, "session-1", "u1", "user_stop"))
    old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    conn = db._get_conn()
    conn.execute("UPDATE run_scout_artifacts SET updated_at=? WHERE id=?", (old, row["id"]))
    conn.commit()
    conn.close()
    monkeypatch.setattr(run_scout, "_scout_enabled", lambda: True)
    launched = []
    monkeypatch.setattr(run_scout, "launch_analysis", lambda _db, value: launched.append(value))

    count = asyncio.run(run_scout._sweep_once(db))

    assert count == 0
    assert launched == []

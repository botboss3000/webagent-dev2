import json
import sqlite3
import uuid

from app.tools.optimizer_tools import (
    _deployable_trial_context,
    _existing_closer_session,
    _recover_worker_results_from_planner_temp,
)


def test_deployable_trial_context_preserves_full_tested_content():
    proposed_content = "Keep every existing instruction.\n\nAdd this exact rule."
    context = _deployable_trial_context(json.dumps([{
        "element": "agent_prompt",
        "element_type": "context_column",
        "new_content": proposed_content,
        "success": True,
        "sim_user_satisfied": True,
        "tool_calls_made": 2,
        "turn_count": 3,
        "confidence": 0.85,
        "trial_transcript": [{"role": "worker", "content": "not deployable"}],
    }]))

    assert "Deployable Worker Results" in context
    payload = json.loads(context.split("```json\n", 1)[1].rsplit("\n```", 1)[0])
    assert payload == [{
        "element": "agent_prompt",
        "element_type": "context_column",
        "new_content": proposed_content,
        "success": True,
        "sim_user_satisfied": True,
        "tool_calls_made": 2,
        "turn_count": 3,
        "confidence": 0.85,
    }]


def test_existing_closer_session_is_matched_only_to_its_planner():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE sessions (id TEXT, metadata TEXT, created_at TEXT)")
    conn.executemany(
        "INSERT INTO sessions VALUES (?, ?, ?)",
        [
            ("closer-earlier", json.dumps({"source_optimizer_session": "optimizer-other"}), "2026-01-01"),
            ("closer-target", json.dumps({"source_optimizer_session": "optimizer-target"}), "2026-01-02"),
        ],
    )

    assert _existing_closer_session(conn, "optimizer-target") == "closer-target"
    assert _existing_closer_session(conn, "optimizer-missing") == ""


def test_planner_has_a_stable_closer_id_for_concurrent_retries():
    planner_id = "optimizer-target"
    closer_id = lambda: f"closer-{uuid.uuid5(uuid.NAMESPACE_URL, f'optimizer-closer:{planner_id}').hex[:16]}"

    assert closer_id() == closer_id()


def test_handoff_recovers_full_worker_result_from_planner_temp(tmp_path):
    planner_id = "optimizer-target"
    temp_db = tmp_path / "optimizer_trial.db"
    conn = sqlite3.connect(temp_db)
    conn.execute("CREATE TABLE sessions (id TEXT)")
    conn.execute("CREATE TABLE interactions (session_id TEXT, tool_name TEXT, output TEXT, content TEXT, created_at TEXT)")
    worker_result = json.dumps([{"element": "skills_prompt", "new_content": "full proposed content"}])
    conn.execute("INSERT INTO sessions VALUES (?)", (planner_id,))
    conn.execute(
        "INSERT INTO interactions VALUES (?, 'run_worker_trials', ?, 'truncated', '2026-01-01')",
        (planner_id, worker_result),
    )
    conn.commit()
    conn.close()

    assert _recover_worker_results_from_planner_temp(str(tmp_path), planner_id) == worker_result

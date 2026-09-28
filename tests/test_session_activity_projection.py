import json
import sqlite3

from app.db.session_activity import (
    ACTIVITY_PROJECTION_VERSION,
    activity_summary_for_interaction,
    ensure_session_activity_summaries,
    project_interaction_activity,
    rebuild_session_activity_summaries,
    backfill_legacy_activity_summaries,
)


def _connection():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE interactions (id TEXT, session_id TEXT, role TEXT, content TEXT, "
        "tool_name TEXT, metadata TEXT, output TEXT, source TEXT, parent_id TEXT, "
        "session_seq INTEGER, turn_id TEXT, status TEXT, created_at TEXT, chat_render_kind TEXT)"
    )
    return conn


def _insert(conn, row):
    role, metadata, source = row[2], row[5], row[7]
    phase = json.loads(metadata or "{}").get("message_phase")
    kind = (
        "user" if role == "user" else
        "promoted_answer" if role == "assistant" and phase in {"main", "final"} and row[3] else
        "notice" if role == "system" and source in {"system:model", "system:mode"} else
        "activity"
    )
    conn.execute(
        "INSERT INTO interactions VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (*row, kind),
    )


def test_activity_projection_preserves_user_and_agent_boundaries():
    conn = _connection()
    _insert(conn, ("user-1", "s", "user", "request", None, None, None, None, None, 1, "user-1", "complete", "2026-01-01"))
    _insert(conn, ("assistant-1", "s", "assistant", "", None,
                   json.dumps({"message_phase": "progress"}),
                   json.dumps({"tool_calls": [{"id": "a"}, {"id": "b"}]}),
                   None, "user-1", 2, "user-1", "complete", "2026-01-01"))
    _insert(conn, ("tool-1", "s", "tool", "very large result", "search", None, None,
                   None, "assistant-1", 3, "user-1", "complete", "2026-01-01"))
    _insert(conn, ("agent-answer", "s", "assistant", "done", None,
                   json.dumps({"message_phase": "main"}), None,
                   None, "user-1", 4, "user-1", "complete", "2026-01-01"))
    _insert(conn, ("model", "s", "system", "Premium model enabled", None, None, None,
                   "system:model", None, 5, "user-1", "complete", "2026-01-01"))
    _insert(conn, ("user-2", "s", "user", "next", None, None, None, None, None, 6, "user-2", "complete", "2026-01-01"))

    rebuild_session_activity_summaries(conn, "s")
    rows = conn.execute(
        "SELECT start_seq, end_seq, owner_turn_id, tool_count, progress_count, model_count, activity_count, is_open "
        "FROM session_activity_summaries ORDER BY start_seq"
    ).fetchall()

    assert rows == [(2, 3, "user-1", 2, 0, 0, 2, 0)]


def test_tail_activity_projection_is_marked_open_until_a_visible_boundary():
    conn = _connection()
    _insert(conn, ("user-1", "s", "user", "request", None, None, None, None, None, 1, "user-1", "complete", "2026-01-01"))
    _insert(conn, ("progress", "s", "assistant", "Working", None,
                   json.dumps({"message_phase": "progress"}), None,
                   None, "user-1", 2, "user-1", "complete", "2026-01-01"))

    rebuild_session_activity_summaries(conn, "s")
    assert conn.execute(
        "SELECT start_seq, end_seq, progress_count, is_open FROM session_activity_summaries"
    ).fetchone() == (2, 2, 1, 1)


def test_completed_streaming_row_uses_final_tool_and_message_counts():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    _insert(conn, ("user", "s", "user", "request", None, None, None,
                   None, None, 1, "user", "complete", "2026-01-01"))
    _insert(conn, ("step", "s", "assistant", "", None, None, None,
                   None, "user", 2, "user", "streaming", "2026-01-01"))
    project_interaction_activity(conn, "user")
    project_interaction_activity(conn, "step")
    assert conn.execute("SELECT COUNT(*) FROM session_activity_summaries").fetchone() == (0,)

    conn.execute(
        "UPDATE interactions SET content=?, output=?, status='complete' WHERE id='step'",
        ("Checking the index", json.dumps({"tool_calls": [{"id": "a"}, {"id": "b"}]})),
    )
    project_interaction_activity(conn, "step")

    assert conn.execute(
        "SELECT tool_count, progress_count, activity_count FROM session_activity_summaries"
    ).fetchone() == (2, 1, 1)

def test_incremental_projection_closes_and_splits_at_real_boundaries():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user-1", "s", "user", "request", None, None, None, None, None, 1, "user-1", "complete", "2026-01-01"),
        ("progress-1", "s", "assistant", "Thinking", None,
         json.dumps({"message_phase": "progress"}), None, None, "user-1", 2, "user-1", "complete", "2026-01-01"),
        ("answer-1", "s", "assistant", "first answer", None,
         json.dumps({"message_phase": "main"}), None, None, "user-1", 3, "user-1", "complete", "2026-01-01"),
        ("mode-1", "s", "system", "Plan mode enabled", None, None, None,
         "system:mode", None, 4, "user-1", "complete", "2026-01-01"),
        ("answer-2", "s", "assistant", "second answer", None,
         json.dumps({"message_phase": "main"}), None, None, "user-1", 5, "user-1", "complete", "2026-01-01"),
        ("user-2", "s", "user", "next", None, None, None, None, None, 6, "user-2", "complete", "2026-01-01"),
        ("tool-2", "s", "tool", "result", "search", None, None, None,
         "user-2", 7, "user-2", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    summaries = conn.execute(
        "SELECT group_id, start_seq, end_seq, owner_turn_id, progress_count, mode_count, "
        "is_open, following_boundary_id FROM session_activity_summaries ORDER BY start_seq"
    ).fetchall()
    assert summaries == [
        ("activity-summary:progress-1", 2, 2, "user-1", 1, 0, 0, "answer-1"), ("activity-summary:tool-2", 7, 7, "user-2", 0, 0, 1, None)]


def test_activity_snapshot_uses_the_durable_phase_identity_and_counts():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
        ("tool", "s", "tool", "ok", "search", None, None, None, "progress", 3, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])
    snapshot = activity_summary_for_interaction(conn, "tool")
    assert snapshot == {
        "group_id": "activity-summary:progress", "session_id": "s",
        "start_seq": 2, "end_seq": 3, "owner_turn_id": "user",
        "summary_counts": {"tools": 0, "messages": 1, "mode": False, "model": False,
                           "compactions": 0, "updates": 2},
        "activity_open": True,
    }


def test_two_promoted_updates_create_three_durable_activity_phases():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress-a", "s", "assistant", "Working A", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
        ("tool-a", "s", "tool", "ok", "search", None, None, None, "progress-a", 3, "user", "complete", "2026-01-01"),
        ("update-a", "s", "assistant", "First update", None, json.dumps({"message_phase": "main", "mid_run": True}), None, None, "user", 4, "user", "complete", "2026-01-01"),
        ("progress-b", "s", "assistant", "Working B", None, json.dumps({"message_phase": "progress"}), None, None, "user", 5, "user", "complete", "2026-01-01"),
        ("update-b", "s", "assistant", "Second update", None, json.dumps({"message_phase": "main", "mid_run": True}), None, None, "user", 6, "user", "complete", "2026-01-01"),
        ("progress-c", "s", "assistant", "Working C", None, json.dumps({"message_phase": "progress"}), None, None, "user", 7, "user", "complete", "2026-01-01"),
        ("final", "s", "assistant", "Done", None, json.dumps({"message_phase": "final"}), None, None, "user", 8, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT group_id, start_seq, end_seq, following_boundary_id, is_open "
        "FROM session_activity_summaries ORDER BY start_seq"
    ).fetchall() == [
        ("activity-summary:progress-a", 2, 3, "update-a", 0),
        ("activity-summary:progress-b", 5, 5, "update-b", 0),
        ("activity-summary:progress-c", 7, 7, "final", 0),
    ]


def test_empty_main_marker_does_not_split_one_visible_activity_phase():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress-a", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
        ("silent-main", "s", "assistant", "", None, json.dumps({"message_phase": "main"}), None, None, "user", 3, "user", "complete", "2026-01-01"),
        ("tool-a", "s", "tool", "ok", "search", None, None, None, "progress-a", 4, "user", "complete", "2026-01-01"),
        ("final", "s", "assistant", "Done", None, json.dumps({"message_phase": "final"}), None, None, "user", 5, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT start_seq, end_seq, following_boundary_id, activity_count "
        "FROM session_activity_summaries"
    ).fetchall() == [(2, 4, "final", 3)]


def test_empty_final_marker_does_not_split_one_visible_activity_phase():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress-a", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
        ("silent-final", "s", "assistant", "", None, json.dumps({"message_phase": "final"}), None, None, "user", 3, "user", "complete", "2026-01-01"),
        ("tool-a", "s", "tool", "ok", "search", None, None, None, "progress-a", 4, "user", "complete", "2026-01-01"),
        ("final", "s", "assistant", "Done", None, json.dumps({"message_phase": "final"}), None, None, "user", 5, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT start_seq, end_seq, following_boundary_id, activity_count "
        "FROM session_activity_summaries"
    ).fetchall() == [(2, 4, "final", 3)]


def test_internal_turn_changes_do_not_split_one_visible_activity_phase():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress-a", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "parent-turn", "complete", "2026-01-01"),
        ("tool-a", "s", "tool", "ok", "search", None, None, None, "progress-a", 3, "child-turn", "complete", "2026-01-01"),
        ("progress-b", "s", "assistant", "Still working", None, json.dumps({"message_phase": "progress"}), None, None, "tool-a", 4, "retry-turn", "complete", "2026-01-01"),
        ("final", "s", "assistant", "Done", None, json.dumps({"message_phase": "final"}), None, None, "user", 5, "parent-turn", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT start_seq, end_seq, owner_turn_id, activity_count, following_boundary_id "
        "FROM session_activity_summaries"
    ).fetchall() == [(2, 4, "parent-turn", 3, "final")]


def test_durable_pending_rows_stay_in_the_hidden_activity_phase():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress-a", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
        ("pending-a", "s", "assistant", "internal attempt", None, json.dumps({"message_phase": "pending"}), None, None, "progress-a", 3, "retry", "complete", "2026-01-01"),
        ("tool-a", "s", "tool", "ok", "search", None, None, None, "pending-a", 4, "child", "complete", "2026-01-01"),
        ("final", "s", "assistant", "Done", None, json.dumps({"message_phase": "final"}), None, None, "user", 5, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT start_seq, end_seq, activity_count, following_boundary_id "
        "FROM session_activity_summaries"
    ).fetchall() == [(2, 4, 3, "final")]


def test_phase_less_assistant_rows_stay_in_the_hidden_activity_phase():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress-a", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
        ("legacy-attempt", "s", "assistant", "internal attempt", None, None, None, None, "progress-a", 3, "retry", "complete", "2026-01-01"),
        ("tool-a", "s", "tool", "ok", "search", None, None, None, "legacy-attempt", 4, "child", "complete", "2026-01-01"),
        ("final", "s", "assistant", "Done", None, json.dumps({"message_phase": "final"}), None, None, "user", 5, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT start_seq, end_seq, activity_count, following_boundary_id "
        "FROM session_activity_summaries"
    ).fetchall() == [(2, 4, 3, "final")]


def test_new_projection_is_stamped_for_future_repair():
    conn = _connection()
    ensure_session_activity_summaries(conn)
    rows = [
        ("user", "s", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"),
        ("progress", "s", "assistant", "Working", None, json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"),
    ]
    for row in rows:
        _insert(conn, row)
        project_interaction_activity(conn, row[0])

    assert conn.execute(
        "SELECT projection_version FROM session_activity_summaries"
    ).fetchone() == (ACTIVITY_PROJECTION_VERSION,)


def test_deferred_backfill_projects_legacy_file_once(tmp_path):
    root = tmp_path / "user_data"
    db_path = root / "member" / "member.db"
    db_path.parent.mkdir(parents=True)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE interactions (id TEXT, session_id TEXT, role TEXT, content TEXT, "
        "tool_name TEXT, metadata TEXT, output TEXT, source TEXT, parent_id TEXT, "
        "session_seq INTEGER, turn_id TEXT, status TEXT, created_at TEXT, chat_render_kind TEXT)"
    )
    _insert(conn, ("user", "legacy", "user", "request", None, None, None, None, None, 1, "user", "complete", "2026-01-01"))
    _insert(conn, ("progress", "legacy", "assistant", "Working", None,
                   json.dumps({"message_phase": "progress"}), None, None, "user", 2, "user", "complete", "2026-01-01"))
    _insert(conn, ("answer", "legacy", "assistant", "done", None,
                   json.dumps({"message_phase": "main"}), None, None, "user", 3, "user", "complete", "2026-01-01"))
    conn.commit()
    conn.close()

    assert backfill_legacy_activity_summaries(root) == 1
    conn = sqlite3.connect(db_path)
    assert conn.execute(
        "SELECT start_seq, end_seq, progress_count, is_open FROM session_activity_summaries"
    ).fetchone() == (2, 2, 1, 0)
    conn.close()
    assert backfill_legacy_activity_summaries(root) == 0

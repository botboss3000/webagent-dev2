"""Durable, incremental transcript activity projection.

Activity summaries are projections over authoritative interaction ranges, never
synthetic interaction rows.  The write path classifies a row once and advances
one open summary in the same SQLite transaction; the reader therefore never
needs to inspect historic tool payloads merely to render a collapsed disclosure.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any


ACTIVITY_PROJECTION_VERSION = 6

ACTIVITY_SUMMARY_SQL = """
CREATE TABLE IF NOT EXISTS session_activity_summaries (
    session_id TEXT NOT NULL,
    group_id TEXT,
    start_seq INTEGER NOT NULL,
    end_seq INTEGER NOT NULL,
    first_interaction_id TEXT NOT NULL,
    owner_turn_id TEXT NOT NULL DEFAULT '',
    preceding_boundary_id TEXT,
    following_boundary_id TEXT,
    created_at TEXT NOT NULL,
    tool_count INTEGER NOT NULL DEFAULT 0,
    progress_count INTEGER NOT NULL DEFAULT 0,
    mode_count INTEGER NOT NULL DEFAULT 0,
    model_count INTEGER NOT NULL DEFAULT 0,
    compaction_count INTEGER NOT NULL DEFAULT 0,
    activity_count INTEGER NOT NULL DEFAULT 1,
    is_open INTEGER NOT NULL DEFAULT 0,
    projection_version INTEGER NOT NULL DEFAULT 6,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (session_id, start_seq)
);
CREATE INDEX IF NOT EXISTS idx_activity_summaries_session_seq
    ON session_activity_summaries(session_id, start_seq, end_seq);
CREATE INDEX IF NOT EXISTS idx_activity_summaries_session_end
    ON session_activity_summaries(session_id, end_seq);
"""


def ensure_session_activity_summaries(conn: Any) -> None:
    """Create and minimally migrate the local-only summary projection."""
    conn.executescript(ACTIVITY_SUMMARY_SQL)
    columns = {row[1] for row in conn.execute(
        "PRAGMA table_info(session_activity_summaries)"
    ).fetchall()}
    additions = {
        "group_id": "TEXT",
        "preceding_boundary_id": "TEXT",
        "following_boundary_id": "TEXT",
        # Existing projected ranges need a truthful fallback label on their next
        # open; their contiguous bounds contain only activity rows.
        "projection_version": "INTEGER",
        "activity_count": "INTEGER",
    }
    for name, spec in additions.items():
        if name not in columns:
            conn.execute(f"ALTER TABLE session_activity_summaries ADD COLUMN {name} {spec}")
    conn.execute(
        "UPDATE session_activity_summaries SET group_id='activity-summary:' || first_interaction_id "
        "WHERE group_id IS NULL OR group_id=''"
    )
    conn.execute(
        "UPDATE session_activity_summaries SET activity_count=MAX(1, end_seq-start_seq+1) "
        "WHERE activity_count IS NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_activity_summaries_session_group "
        "ON session_activity_summaries(session_id, group_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_activity_summaries_session_end "
        "ON session_activity_summaries(session_id, end_seq)"
    )


def _json_object(value: Any) -> dict:
    try:
        parsed = json.loads(value) if isinstance(value, str) else (value or {})
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}



def _tool_count(row: dict) -> int:
    if row["role"] != "assistant":
        return 0
    calls = _json_object(row["output"]).get("tool_calls") or []
    return len(calls) if isinstance(calls, list) else 0


def _synthetic_tool(row: dict) -> bool:
    meta = _json_object(row["metadata"])
    parent_role = str(row.get("parent_role") or "")
    return (
        row["tool_name"] in {"process_image", "route_attachment", "app_control"}
        and parent_role == "user"
    ) or (
        row["tool_name"] == "resume_session" and row["source"] == "system:recovery"
    ) or (
        row["tool_name"] in {"memory_search", "memory_save"}
        and bool(meta.get("brain")) and not bool(meta.get("skipped"))
    )



def _counts(row: dict) -> tuple[int, int, int, int, int]:
    # This function summarizes only rows the caller already established as
    # ``activity``. It deliberately does not inspect message-phase metadata.
    message = (
        row["role"] == "assistant"
        and bool(str(row["content"] or "").strip())
    )
    return (
        _tool_count(row) + (1 if row["role"] == "tool" and _synthetic_tool(row) else 0),
        1 if message else 0,
        1 if row["source"] == "system:mode" else 0,
        1 if row["source"] == "system:model" else 0,
        1 if row["source"] == "system:compaction" else 0,
    )


def _dict_row(cursor: Any, row: Any) -> dict | None:
    if row is None:
        return None
    return dict(row) if hasattr(row, "keys") else dict(zip([c[0] for c in cursor.description], row))


def _row_for_projection(conn: Any, interaction_id: str) -> dict | None:
    try:
        cursor = conn.execute(
            "SELECT i.id, i.session_id, i.role, i.content, i.tool_name, i.metadata, i.output, "
            "i.source, i.parent_id, i.session_seq, i.turn_id, i.status, i.created_at, i.chat_render_kind, "
            "p.role AS parent_role FROM interactions i LEFT JOIN interactions p ON p.id=i.parent_id "
            "WHERE i.id=?",
            (interaction_id,),
        )
    except sqlite3.OperationalError:
        # Test/import fixtures and pre-migration databases are V1 and do not yet
        # have the durable render column. Their legacy projection remains intact.
        cursor = conn.execute(
            "SELECT i.id, i.session_id, i.role, i.content, i.tool_name, i.metadata, i.output, "
            "i.source, i.parent_id, i.session_seq, i.turn_id, i.status, i.created_at, "
            "p.role AS parent_role FROM interactions i LEFT JOIN interactions p ON p.id=i.parent_id "
            "WHERE i.id=?",
            (interaction_id,),
        )
    row = cursor.fetchone()
    return _dict_row(cursor, row)



def _project_v2_activity(conn: Any, row: dict) -> None:
    """Project V2 exclusively from the durable render kind, never phase metadata."""
    session_id = str(row["session_id"])
    seq = int(row["session_seq"])
    # A streaming assistant row is an incomplete mutation, not transcript
    # activity. Its eventual completion supplies the tool-call payload and
    # final progress text, so projecting it now would cache zero counts for a
    # row whose semantic shape has not been written yet.
    if str(row.get("status") or "") == "streaming":
        return
    kind = str(row.get("chat_render_kind") or "activity")
    covered = conn.execute(
        "SELECT 1 FROM session_activity_summaries WHERE session_id=? AND ? BETWEEN start_seq AND end_seq LIMIT 1",
        (session_id, seq),
    ).fetchone()
    if kind in {"user", "promoted_answer", "notice", "error"}:
        # A streamed assistant row is initially activity. Its terminal promotion
        # changes the range boundary, so rebuild the session atomically instead of
        # leaving the promoted row inside a durable activity summary.
        if covered:
            rebuild_session_activity_summaries(conn, session_id)
            return
        open_cursor = conn.execute(
            "SELECT * FROM session_activity_summaries WHERE session_id=? AND is_open=1 ORDER BY end_seq DESC LIMIT 1",
            (session_id,),
        )
        open_row = _dict_row(open_cursor, open_cursor.fetchone())
        if open_row is not None:
            conn.execute(
                "UPDATE session_activity_summaries SET is_open=0, following_boundary_id=?, updated_at=datetime('now') WHERE session_id=? AND start_seq=?",
                (row["id"], session_id, open_row["start_seq"]),
            )
        return
    if kind != "activity":
        # hidden is intentionally neither a direct item nor an activity extent.
        return
    if covered:
        # Interactions are mutable while a run streams. A completed rewrite can
        # add tool calls, text, or a visibility boundary to an already projected
        # row. Rebuild from the authoritative interaction ledger so the durable
        # header and lazy detail projection always share one truth.
        rebuild_session_activity_summaries(conn, session_id)
        return
    open_cursor = conn.execute(
        "SELECT * FROM session_activity_summaries WHERE session_id=? AND is_open=1 ORDER BY end_seq DESC LIMIT 1",
        (session_id,),
    )
    open_row = _dict_row(open_cursor, open_cursor.fetchone())
    tools, progress, mode, model, compaction = _counts(row)
    if open_row is not None:
        conn.execute(
            "UPDATE session_activity_summaries SET end_seq=?, tool_count=tool_count+?, progress_count=progress_count+?, mode_count=mode_count+?, model_count=model_count+?, compaction_count=compaction_count+?, activity_count=COALESCE(activity_count, 0)+1, following_boundary_id=NULL, updated_at=datetime('now') WHERE session_id=? AND start_seq=?",
            (seq, tools, progress, mode, model, compaction, session_id, open_row["start_seq"]),
        )
        return
    preceding = conn.execute(
        "SELECT id FROM interactions WHERE session_id=? AND session_seq < ? ORDER BY session_seq DESC LIMIT 1",
        (session_id, seq),
    ).fetchone()
    owner = str(row.get("turn_id") or row.get("parent_id") or "")
    conn.execute(
        "INSERT INTO session_activity_summaries (session_id,group_id,start_seq,end_seq,first_interaction_id,owner_turn_id,preceding_boundary_id,created_at,tool_count,progress_count,mode_count,model_count,compaction_count,activity_count,is_open,projection_version) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)",
        (session_id, "activity-summary:" + str(row["id"]), seq, seq, row["id"], owner,
         preceding[0] if preceding else None, row["created_at"], tools, progress, mode, model, compaction, 1,
         ACTIVITY_PROJECTION_VERSION),
    )


def project_interaction_activity(conn: Any, interaction_id: str) -> None:
    """Advance one session projection from one newly durable interaction.

    Grouping is solely chat_render_kind. Legacy phase metadata is migration
    input only and is never a runtime display or grouping authority.
    """
    row = _row_for_projection(conn, interaction_id)
    if not row or row.get("session_seq") is None:
        return
    _project_v2_activity(conn, row)


def activity_summary_for_interaction(conn: Any, interaction_id: str) -> dict | None:
    """Return the durable activity group that owns one persisted interaction."""
    cursor = conn.execute(
        "SELECT i.session_id, s.group_id, s.start_seq, s.end_seq, s.owner_turn_id, "
        "s.tool_count, s.progress_count, s.mode_count, s.model_count, "
        "s.compaction_count, s.activity_count, s.is_open "
        "FROM interactions i JOIN session_activity_summaries s "
        "ON s.session_id=i.session_id AND i.session_seq BETWEEN s.start_seq AND s.end_seq "
        "WHERE i.id=? LIMIT 1",
        (interaction_id,),
    )
    row = _dict_row(cursor, cursor.fetchone())
    if row is None:
        return None
    return {
        "group_id": str(row["group_id"]),
        "session_id": str(row["session_id"]),
        "start_seq": int(row["start_seq"]),
        "end_seq": int(row["end_seq"]),
        "owner_turn_id": str(row["owner_turn_id"] or ""),
        "summary_counts": {
            "tools": int(row["tool_count"] or 0),
            "messages": int(row["progress_count"] or 0),
            "mode": int(row["mode_count"] or 0),
            "model": int(row["model_count"] or 0),
            "compactions": int(row["compaction_count"] or 0),
            "updates": int(row["activity_count"] or 1),
        },
        "activity_open": bool(row["is_open"]),
    }

def rebuild_session_activity_summaries(conn: Any, session_id: str) -> None:
    """Explicit repair/import backfill for legacy or out-of-order data only."""
    ensure_session_activity_summaries(conn)
    rows = conn.execute(
        "SELECT id FROM interactions WHERE session_id=? AND session_seq IS NOT NULL "
        "ORDER BY session_seq ASC, created_at ASC, rowid ASC",
        (session_id,),
    ).fetchall()
    conn.execute("DELETE FROM session_activity_summaries WHERE session_id=?", (session_id,))
    for row in rows:
        project_interaction_activity(conn, row[0])


def backfill_legacy_activity_summaries(
    user_data_root: str | Path, *, max_sessions_per_db: int = 250,
) -> int:
    """Persist projections for legacy user sessions after the server is ready.

    The operation is intentionally outside a request: each database is bounded,
    skips sessions that already have any projection, and commits all of a
    session's raw-range repair atomically. A later startup can continue the
    bounded work without redoing already-projected sessions.
    """
    root = Path(user_data_root)
    repaired = 0
    if not root.exists():
        return repaired
    for db_path in sorted(root.glob("*/*.db")):
        conn = None
        try:
            conn = sqlite3.connect(db_path, timeout=5)
            conn.row_factory = sqlite3.Row
            columns = {r[1] for r in conn.execute("PRAGMA table_info(interactions)").fetchall()}
            if "chat_render_kind" not in columns:
                conn.execute("ALTER TABLE interactions ADD COLUMN chat_render_kind TEXT NOT NULL DEFAULT 'activity'")
                conn.execute("UPDATE interactions SET chat_render_kind='user' WHERE role='user'")
                conn.execute("UPDATE interactions SET chat_render_kind='promoted_answer' WHERE role='assistant' AND content <> '' AND (metadata LIKE '%\"closing_promoted\": true%' OR metadata LIKE '%\"message_phase\": \"main\"%' OR metadata LIKE '%\"message_phase\": \"final\"%')")
                conn.execute("UPDATE interactions SET chat_render_kind='notice' WHERE role='system' AND source IN ('system:closer','system:summary','system:overview','system:mode','system:model')")
                conn.execute("UPDATE interactions SET chat_render_kind='error' WHERE role='system' AND source LIKE 'system:closing_partial%'")
                conn.execute("CREATE INDEX IF NOT EXISTS idx_interactions_render_v2 ON interactions(session_id, chat_render_kind, session_seq)")
            ensure_session_activity_summaries(conn)
            sessions = conn.execute(
                "SELECT DISTINCT i.session_id FROM interactions i "
                "WHERE i.session_seq IS NOT NULL "
                "AND (i.role IN ('assistant','tool') OR i.source IN ('system:compaction','system:mode','system:model')) "
                "AND (NOT EXISTS (SELECT 1 FROM session_activity_summaries s WHERE s.session_id=i.session_id) "
                "OR EXISTS (SELECT 1 FROM session_activity_summaries s WHERE s.session_id=i.session_id "
                "AND COALESCE(s.projection_version, 0) < ?)) LIMIT ?",
                (ACTIVITY_PROJECTION_VERSION, max(1, int(max_sessions_per_db))),
            ).fetchall()
            for session in sessions:
                rebuild_session_activity_summaries(conn, str(session[0]))
                repaired += 1
            conn.commit()
        except sqlite3.Error:
            if conn is not None:
                conn.rollback()
        finally:
            if conn is not None:
                conn.close()
    return repaired

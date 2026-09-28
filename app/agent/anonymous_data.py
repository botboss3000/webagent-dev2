"""Retention and bounded transcript storage for durable anonymous identities."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import HTTPException

from app.agent.public_policy import normalize_public_access


def _cutoff(days: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat().replace("+00:00", "Z")


_SESSION_DATA_TABLES = (
    "interactions", "messages", "pipeline_events", "session_summaries",
    "session_runs", "session_interrupts", "session_notifications", "attachments",
    "run_contract_checks", "run_contract_state",
)


def _delete_session(conn: Any, session_id: str) -> None:
    """Delete known session-owned rows, tolerating optional plugin tables."""
    for table in _SESSION_DATA_TABLES:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,),
        ).fetchone()
        if not exists:
            continue
        columns = {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
        if "session_id" in columns:
            conn.execute(f'DELETE FROM "{table}" WHERE session_id=?', (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))


def enforce_anonymous_data_policy(
    db: Any,
    *,
    user_id: str,
    session_id: str,
    agent: dict,
) -> dict:
    """Purge expired sessions and enforce per-guest transcript/session caps."""
    if not str(user_id or "").startswith("anon_"):
        return {"purged_sessions": 0}
    policy = normalize_public_access(agent)["data"]
    retention = max(1, int(policy["session_retention_days"]))
    max_sessions = max(1, int(policy["max_sessions_per_guest"]))
    max_bytes = max(0, int(policy["max_transcript_bytes_per_guest"]))
    max_total_bytes = max(0, int(policy["max_total_storage_bytes"]))
    agent_id = str(agent.get("id") or "")
    conn = db._get_conn()
    purged = 0
    try:
        # Clean a bounded batch for the whole public agent, so expired guest data
        # is removed even when a particular browser never returns.
        expired = conn.execute(
            """SELECT id FROM sessions
               WHERE user_id LIKE 'anon_%' AND agent_id=? AND id<>? AND updated_at < ?
               ORDER BY updated_at ASC LIMIT 250""",
            (agent_id, session_id, _cutoff(retention)),
        ).fetchall()
        for row in expired:
            sid = str(row["id"])
            _delete_session(conn, sid)
            purged += 1
        conn.commit()

        rows = conn.execute(
            """SELECT id FROM sessions WHERE user_id=? AND agent_id=? AND id<>?
               ORDER BY updated_at DESC""",
            (user_id, agent_id, session_id),
        ).fetchall()
        for row in rows[max(0, max_sessions - 1):]:
            sid = str(row["id"])
            _delete_session(conn, sid)
            purged += 1
        conn.commit()

        size_row = conn.execute(
            """SELECT COALESCE(SUM(LENGTH(i.content)),0) AS transcript_bytes
               FROM interactions i JOIN sessions s ON s.id=i.session_id
               WHERE s.user_id=? AND s.agent_id=?""",
            (user_id, agent_id),
        ).fetchone()
        used = int(size_row["transcript_bytes"] if size_row else 0)
        if max_bytes and used >= max_bytes:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "anonymous_storage_exhausted",
                    "message": "This guest chat has reached its saved-conversation limit.",
                    "limit_bytes": max_bytes,
                },
            )
        total_row = conn.execute(
            """SELECT COALESCE(SUM(LENGTH(i.content)),0) AS transcript_bytes
               FROM interactions i JOIN sessions s ON s.id=i.session_id
               WHERE s.user_id LIKE 'anon_%' AND s.agent_id=?""",
            (agent_id,),
        ).fetchone()
        total_used = int(total_row["transcript_bytes"] if total_row else 0)
        if max_total_bytes and total_used >= max_total_bytes:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "public_agent_storage_exhausted",
                    "message": "This public agent has reached its saved-conversation limit.",
                    "limit_bytes": max_total_bytes,
                },
            )
        return {
            "purged_sessions": purged,
            "transcript_bytes": used,
            "limit_bytes": max_bytes,
            "agent_transcript_bytes": total_used,
            "agent_limit_bytes": max_total_bytes,
        }
    finally:
        conn.close()

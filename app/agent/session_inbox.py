"""Durable safe-boundary inbox for messages received during an active run."""
from __future__ import annotations
import json
from typing import Any

def _is_compaction_row(metadata: Any) -> bool:
    try:
        parsed = json.loads(metadata or "{}")
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("queued_for") == "compact"

def claim_pending(db: Any, session_id: str, *, limit: int = 8) -> list[dict[str, str]]:
    """Atomically claim normal queued user messages in transcript order."""
    if not callable(getattr(db, "_get_conn", None)):
        return []
    conn = db._get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT id, content, metadata FROM interactions "
            "WHERE session_id=? AND role='user' AND status='queued' "
            "ORDER BY created_at ASC LIMIT ?",
            (session_id, max(1, min(int(limit), 32))),
        ).fetchall()
        claimed = [row for row in rows if not _is_compaction_row(row[2])]
        for row in claimed:
            conn.execute("UPDATE interactions SET status='complete' WHERE id=?", (row[0],))
        conn.commit()
        return [{"id": str(row[0]), "content": str(row[1] or "")} for row in claimed]
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

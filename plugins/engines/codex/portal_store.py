"""Plugin-owned link registry for Codex Portal virtual sessions."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

_DB_PATH = Path(__file__).resolve().parents[3] / "data" / "engine_state" / "codex" / "portal_links.sqlite"


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS portal_links (
            user_id TEXT NOT NULL,
            agent_id TEXT NOT NULL,
            thread_id TEXT NOT NULL,
            added_at TEXT NOT NULL,
            pinned INTEGER NOT NULL DEFAULT 0,
            hidden INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER,
            PRIMARY KEY (user_id, agent_id, thread_id)
        )"""
    )
    return conn


def add_links(user_id: str, agent_id: str, thread_ids: list[str]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = [(user_id, agent_id, thread_id, now) for thread_id in dict.fromkeys(thread_ids) if thread_id]
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO portal_links (user_id, agent_id, thread_id, added_at) VALUES (?, ?, ?, ?)",
            rows,
        )
        return conn.total_changes - before


def remove_link(user_id: str, agent_id: str, thread_id: str) -> bool:
    with _connect() as conn:
        cur = conn.execute(
            "DELETE FROM portal_links WHERE user_id=? AND agent_id=? AND thread_id=?",
            (user_id, agent_id, thread_id),
        )
        return cur.rowcount > 0


def list_links(user_id: str, agent_id: str | None = None) -> list[dict]:
    with _connect() as conn:
        if agent_id:
            rows = conn.execute(
                "SELECT * FROM portal_links WHERE user_id=? AND agent_id=? ORDER BY pinned DESC, sort_order, added_at DESC",
                (user_id, agent_id),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM portal_links WHERE user_id=? ORDER BY pinned DESC, sort_order, added_at DESC",
                (user_id,),
            ).fetchall()
    return [dict(row) for row in rows]


def has_link(user_id: str, agent_id: str, thread_id: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM portal_links WHERE user_id=? AND agent_id=? AND thread_id=?",
            (user_id, agent_id, thread_id),
        ).fetchone()
    return row is not None


def update_link(user_id: str, agent_id: str, thread_id: str, values: dict) -> bool:
    allowed = {key: values[key] for key in ("pinned", "hidden", "sort_order") if key in values}
    if not allowed:
        return False
    columns = []
    params = []
    for key, value in allowed.items():
        columns.append(f"{key}=?")
        params.append(int(bool(value)) if key in {"pinned", "hidden"} else value)
    params.extend([user_id, agent_id, thread_id])
    with _connect() as conn:
        cur = conn.execute(
            f"UPDATE portal_links SET {', '.join(columns)} WHERE user_id=? AND agent_id=? AND thread_id=?",
            params,
        )
        return cur.rowcount > 0

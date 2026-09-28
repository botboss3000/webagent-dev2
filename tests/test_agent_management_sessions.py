"""Regression coverage for the Agent Management session-list tool."""

import asyncio
import json
import sqlite3

from plugins.abilities.Core.agent_management import agent_management


class _UserPlaneDb:
    """Minimal split-storage user plane: deliberately has no ``agents`` table."""

    def __init__(self, path):
        self.path = str(path)
        self.agent_lookups = []

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def get_agent_by_id(self, agent_id):
        self.agent_lookups.append(agent_id)
        return {"id": agent_id, "name": "Session Assistant"}


def test_list_user_sessions_uses_agent_authority_not_user_plane_join(tmp_path, monkeypatch):
    """A split user DB has sessions but intentionally no co-located agents table."""
    path = tmp_path / "user.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY,
            title TEXT,
            user_id TEXT NOT NULL,
            participants TEXT,
            agent_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            status TEXT,
            hidden INTEGER
        );
        CREATE TABLE interactions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            session_seq INTEGER
        );
        """
    )
    conn.execute(
        """INSERT INTO sessions
           (id, title, user_id, participants, agent_id, created_at, updated_at, status, hidden)
           VALUES ('session-1', 'Storage Diagnosis', 'user-1', '[]', 'agent-1',
                   '2026-01-01T00:00:00+00:00', '2026-01-01T00:01:00+00:00', 'active', 0)"""
    )
    conn.execute(
        """INSERT INTO interactions
           (id, session_id, role, content, created_at, session_seq)
           VALUES ('interaction-1', 'session-1', 'user', 'Please inspect my session list.',
                   '2026-01-01T00:01:00+00:00', 1)"""
    )
    conn.commit()
    conn.close()

    db = _UserPlaneDb(path)
    monkeypatch.setattr("app.db.get_db", lambda: db)

    result = json.loads(asyncio.run(agent_management.list_user_sessions(
        user_id="user-1", limit=15,
    )))

    assert result["status"] == "ok"
    assert result["count"] == 1
    assert result["sessions"][0]["agent_name"] == "Session Assistant"
    assert db.agent_lookups == ["agent-1"]

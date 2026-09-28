from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from app.agent import profiles


def _database():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(profiles.SCHEMA_SQL)
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO agent_profiles VALUES (?,?,?,?,?,?,?,?,?)", ("profile", "agent", "visitor", "Visitor", "", "builtin", "{}", now, now))
    old = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    conn.execute("INSERT INTO agent_members VALUES (?,?,?,?,?,'active',0,?,?)", ("member", "agent", "profile", "", "", old, old))
    conn.execute("INSERT INTO agent_guest_credentials VALUES (?,?,?,?,?)", ("hash", "agent", "member", old, old))
    conn.commit()
    return conn


def test_prune_removes_only_never_used_guest_admissions(tmp_path, monkeypatch) -> None:
    conn = _database()
    profiles._GUEST_PRUNE_LAST.clear()
    monkeypatch.setattr(profiles, "_conn", lambda _agent_id: conn)
    monkeypatch.setattr(profiles, "member_db_path", lambda _agent_id, _member_id: tmp_path / "missing.db")

    assert profiles._prune_never_used_guest_members("agent") == 1
    assert conn.execute("SELECT 1 FROM agent_members WHERE id='member'").fetchone() is None
    assert conn.execute("SELECT 1 FROM agent_guest_credentials WHERE member_id='member'").fetchone() is None


def test_prune_preserves_guest_with_workspace(tmp_path, monkeypatch) -> None:
    conn = _database()
    workspace = tmp_path / "member.db"
    workspace.touch()
    profiles._GUEST_PRUNE_LAST.clear()
    monkeypatch.setattr(profiles, "_conn", lambda _agent_id: conn)
    monkeypatch.setattr(profiles, "member_db_path", lambda _agent_id, _member_id: workspace)

    assert profiles._prune_never_used_guest_members("agent") == 0
    assert conn.execute("SELECT 1 FROM agent_members WHERE id='member'").fetchone() is not None


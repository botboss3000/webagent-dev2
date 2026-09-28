from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

from app.agent import prompts
from app.api import db_viewer
from app.tools.core_tools import session_search


ROOT = Path(__file__).resolve().parents[1]


def _run(coro):
    return asyncio.run(coro)


def _seed_db(path: Path, *, agent_id: str | None = "agent-1") -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE sessions (
            id TEXT PRIMARY KEY, user_id TEXT, title TEXT, metadata TEXT,
            agent_id TEXT, participants TEXT, status TEXT,
            created_at TEXT, updated_at TEXT
        );
        CREATE TABLE interactions (
            id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT, role TEXT,
            content TEXT, tool_name TEXT, tool_call_id TEXT, channel TEXT,
            metadata TEXT, output TEXT, source TEXT, from_id TEXT, to_id TEXT,
            session_seq INTEGER, turn_id TEXT, turn_seq INTEGER, status TEXT,
            created_at TEXT
        );
        """
    )
    conn.execute(
        "INSERT INTO sessions VALUES (?, ?, ?, ?, ?, ?, 'active', datetime('now'), datetime('now'))",
        ("source", "user-1", "Deploy audit", json.dumps({
            "llm_override": {"model": "example/model"},
            "active_skills": ["review"],
            "execution_mode": "auto",
            "remote_executor": {"instance_id": "remote-1"},
        }), agent_id, "[]"),
    )
    conn.execute(
        "INSERT INTO interactions VALUES (?, ?, NULL, 'user', ?, NULL, NULL, 'web_portal', '{}', NULL, NULL, ?, ?, 1, ?, 0, 'complete', datetime('now'))",
        ("i1", "source", "deploy it", "user-1", agent_id, "i1"),
    )
    conn.execute(
        "INSERT INTO interactions VALUES (?, ?, ?, 'assistant', ?, NULL, NULL, 'web_portal', '{}', NULL, NULL, ?, ?, 2, ?, 1, 'streaming', datetime('now'))",
        ("i2", "source", "i1", "still checking", agent_id, "user-1", "i1"),
    )
    conn.commit()
    conn.close()


def _fork(tmp_path, monkeypatch, *, kind: str, interaction_id: str | None = None):
    db_path = tmp_path / "btw.db"
    _seed_db(db_path)
    monkeypatch.setattr(db_viewer, "_hybrid_backend_for", lambda _db: None)
    monkeypatch.setattr(
        db_viewer, "_open",
        lambda _db: (sqlite3.connect(db_path), "sqlite"),
    )
    monkeypatch.setattr(db_viewer, "_session_access_ok", lambda *_args: True)
    result = _run(db_viewer.fork_session(
        "source",
        db_viewer.SessionForkRequest(
            user_id="user-1", fork_kind=kind,
            up_to_interaction_id=interaction_id,
        ),
        SimpleNamespace(),
        db="btw.db",
    ))
    return db_path, result


def test_btw_forks_latest_snapshot_and_tags_metadata(tmp_path, monkeypatch):
    db_path, result = _fork(tmp_path, monkeypatch, kind="btw")
    assert result["title"] == "btw: Deploy audit"
    assert result["interactions_copied"] == 2
    assert result["snapshot_session_seq"] == 2
    assert result["source_session_seq"] == 2
    assert result["agent_id"] == "agent-1"

    conn = sqlite3.connect(db_path)
    session = conn.execute(
        "SELECT title, metadata, agent_id FROM sessions WHERE id = ?",
        (result["session_id"],),
    ).fetchone()
    rows = conn.execute(
        "SELECT parent_id, session_seq, status FROM interactions "
        "WHERE session_id = ? ORDER BY session_seq",
        (result["session_id"],),
    ).fetchall()
    conn.close()

    metadata = json.loads(session[1])
    assert session[0] == "btw: Deploy audit"
    assert session[2] == "agent-1"
    assert metadata["btw"]["source_session_id"] == "source"
    assert metadata["btw"]["source_interaction_id"] == "i2"
    assert metadata["auto_title_locked"] is True
    assert metadata["llm_override"] == {"model": "example/model"}
    assert "execution_mode" not in metadata
    assert "remote_executor" not in metadata
    assert rows[0][0] is None
    assert rows[1][0] is not None
    assert rows[1][2] == "interrupted"


def test_existing_fork_contract_still_requires_anchor_and_uses_fork_title(tmp_path, monkeypatch):
    _db_path, result = _fork(tmp_path, monkeypatch, kind="fork", interaction_id="i1")
    assert result["title"] == "Fork: Deploy audit"
    assert result["interactions_copied"] == 1
    assert result["fork_kind"] == "fork"


def test_btw_prompt_overlay_is_turn_scoped(monkeypatch):
    async def directive(session_id, user_id):
        assert session_id == "btw-session"
        assert user_id == "user-1"
        return "# [BTW SIDEBAR SESSION]\nsource session: source"

    monkeypatch.setattr(prompts, "_session_btw_directive", directive)
    parts = _run(prompts.build_system_prompt_parts(
        [{"context_type": "agent", "content": "AGENT"}],
        user_id="user-1", session_id="btw-session",
    ))
    assert "BTW SIDEBAR SESSION" in parts.turn_context
    assert "BTW SIDEBAR SESSION" not in parts.agent_context


def test_btw_prompt_overlay_reads_owned_session_metadata(tmp_path, monkeypatch):
    db_path = tmp_path / "prompt.db"
    _seed_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute(
        "UPDATE sessions SET metadata = ? WHERE id = 'source'",
        (json.dumps({"btw": {
            "source_session_id": "original",
            "source_title": "Original work",
            "source_interaction_id": "turn-7",
            "source_session_seq": 42,
        }}),),
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr("app.db.get_db", lambda: _PathDb(db_path))

    directive = _run(prompts._session_btw_directive("source", "user-1"))
    assert "Original work (id: original)" in directive
    assert "session_seq 42, interaction turn-7" in directive
    assert "do not poll proactively" in directive
    assert _run(prompts._session_btw_directive("source", "other-user")) == ""


class _PathDb:
    def __init__(self, path: Path):
        self.path = path

    def _get_conn(self):
        return sqlite3.connect(self.path)


def test_session_search_can_incrementally_inspect_exact_owned_session(tmp_path, monkeypatch):
    db_path = tmp_path / "inspect.db"
    _seed_db(db_path)
    monkeypatch.setattr("app.db.get_db", lambda: _PathDb(db_path))
    payload = json.loads(_run(session_search(
        user_id="user-1", session_id="source", after_session_seq=1, limit=10,
    )))
    assert payload["status"] == "ok"
    assert payload["latest_session_seq"] == 2
    assert [row["id"] for row in payload["results"]] == ["i2"]
    assert payload["results"][0]["status"] == "streaming"

    denied = json.loads(_run(session_search(
        user_id="someone-else", session_id="source",
    )))
    assert denied["status"] == "error"


def test_frontend_btw_contract_is_source_session_safe():
    send = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")
    widget = (ROOT / "ui/chat-widget/js/chat-widget.js").read_text(encoding="utf-8")
    assert "^\\/btw(?:\\s+([\\s\\S]*))?$" in send
    assert "fork_kind: 'btw'" in send
    assert "sessionId: data.session_id" in send
    assert "initialSessionSeq: data.snapshot_session_seq" in send
    assert "executionMode: 'ask'" in send
    assert "switchToSession" not in send[send.index("async function _startBtwSession"):send.index("async function sendMessage")]
    assert "initialSessionSeq = 0" in widget
    assert "lastSeq: Math.max(0, Number(initialSessionSeq) || 0)" in widget


def test_widget_ports_panel_transcript_and_composer_contracts():
    widget = (ROOT / "ui/chat-widget/js/chat-widget.js").read_text(encoding="utf-8")
    surface = (ROOT / "ui/shared/js/chat-surface.js").read_text(encoding="utf-8")
    styles = (ROOT / "ui/chat-widget/chat-widget.css").read_text(encoding="utf-8")

    assert "buildActivityEntryRow" in widget
    assert "'bubble-tool-calls'" in widget
    assert "'tool-only', 'activity-group'" in widget
    assert "st.activityItems.push(toolEntry)" in widget
    assert "_recordAgentStep(ev, key)" in widget
    assert "_isProgressMessage(msg)" in widget
    assert "chat-pill-stop cw-stop-btn" in widget
    assert "chat-pill-continue cw-continue-btn" in widget
    assert "stopBtn, stop: stopBtn, contBtn, continue: contBtn" in widget
    assert widget.count("st.toolCalls.push(toolEntry);") == 1
    assert "chat-stop-btn cw-stop-btn" not in widget
    assert "stop: els.stop" in surface
    assert "continue: els.continue" in surface
    assert "display: flex;\n  flex-direction: column;" in styles
    assert "width: 100%;\n}" not in styles[styles.index(".cw-body > *"):styles.index(".cw-bubble.streaming")]

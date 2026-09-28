"""Focused integration coverage for Codex context-mode continuity.

The database, durable task store, snapshot store, packet renderer, engine mode
fence, and Closer checkpoint writer are real.  Only the external Codex process
is replaced with a deterministic JSONL-producing fake.
"""

import asyncio
import json
import sqlite3
import subprocess

from app.agent import output_closer
from plugins.engines.codex import codex as engine
from plugins.engines.codex.context_store import task_state_for_interaction


class _SQLiteDb:
    def __init__(self, path):
        self.path = str(path)
        self.native_reads = 0
        self.native_writes = []
        self._next_interaction = 1

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def seed(self):
        conn = self._get_conn()
        conn.executescript(
            """
            CREATE TABLE sessions (
              id TEXT PRIMARY KEY, metadata TEXT, updated_at TEXT
            );
            CREATE TABLE interactions (
              id TEXT PRIMARY KEY, session_id TEXT, parent_id TEXT,
              role TEXT, content TEXT, source TEXT,
              status TEXT DEFAULT 'complete', session_seq INTEGER,
              created_at TEXT, tool_name TEXT, tool_call_id TEXT,
              metadata TEXT, output_data TEXT, channel TEXT,
              sender_id TEXT, receiver_id TEXT
            );
            INSERT INTO sessions (id, metadata, updated_at)
            VALUES (
              'session',
              '{"codex_thread_id":"stale-native-thread","keep":true}',
              datetime('now')
            );
            INSERT INTO interactions (
              id, session_id, role, content, source, status, session_seq,
              created_at
            ) VALUES (
              'user-1', 'session', 'user', 'Implement the wrapper flow',
              'user', 'complete', 1, datetime('now')
            );
            """
        )
        conn.commit()
        conn.close()

    async def get_session_codex_id(self, session_id):
        self.native_reads += 1
        conn = self._get_conn()
        row = conn.execute(
            "SELECT metadata FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        conn.close()
        return json.loads(row["metadata"] or "{}").get("codex_thread_id")

    async def get_session_codex_reseed(self, _session_id):
        self.native_reads += 1
        return None

    async def set_session_codex_id(self, session_id, thread_id):
        self.native_writes.append(thread_id)
        conn = self._get_conn()
        row = conn.execute(
            "SELECT metadata FROM sessions WHERE id=?", (session_id,),
        ).fetchone()
        metadata = json.loads(row["metadata"] or "{}")
        metadata["codex_thread_id"] = thread_id
        conn.execute(
            "UPDATE sessions SET metadata=?, updated_at=datetime('now') WHERE id=?",
            (json.dumps(metadata), session_id),
        )
        conn.commit()
        conn.close()

    async def insert_interaction(self, _user_id, session_id, **values):
        interaction_id = f"generated-{self._next_interaction}"
        self._next_interaction += 1
        conn = self._get_conn()
        seq = conn.execute(
            "SELECT COALESCE(MAX(session_seq), 0) + 1 FROM interactions WHERE session_id=?",
            (session_id,),
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO interactions (
              id, session_id, parent_id, role, content, source, status,
              session_seq, created_at, tool_name, tool_call_id, metadata,
              output_data, channel, sender_id, receiver_id
            ) VALUES (?, ?, ?, ?, ?, 'agent', 'complete', ?, datetime('now'),
                      ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction_id, session_id, values.get("parent_id"),
                values.get("role"), values.get("content", ""), seq,
                values.get("tool_name"), values.get("tool_call_id"),
                values.get("metadata"), values.get("output_data"),
                values.get("channel"), values.get("sender_id"),
                values.get("receiver_id"),
            ),
        )
        conn.commit()
        conn.close()
        return interaction_id

    async def get_agent_by_id(self, _agent_id):
        return {
            "id": "agent",
            "metadata": json.dumps({"codex_code": {"closer_enabled": True}}),
        }


class _Input:
    def __init__(self):
        self.value = ""
        self.closed = False

    def write(self, value):
        self.value += value

    def close(self):
        self.closed = True


def _install_fake_codex(monkeypatch, invocations):
    class _Proc:
        stderr = None
        returncode = None

        def __init__(self, cmd, **kwargs):
            index = len(invocations)
            thread_id = "fresh-native-thread" if index == 2 else f"wrapper-{index}"
            records = (
                {"type": "thread.started", "thread_id": thread_id},
                {"type": "item.completed", "item": {
                    "id": f"message-{index}", "type": "agent_message",
                    "text": f"response-{index}",
                }},
            )
            self.stdout = iter(json.dumps(record) + "\n" for record in records)
            self.stdin = _Input() if kwargs.get("stdin") == subprocess.PIPE else None
            invocations.append({"cmd": cmd, "kwargs": kwargs, "proc": self})

        def wait(self):
            self.returncode = 0
            return 0

        def poll(self):
            return self.returncode

    monkeypatch.setattr(engine.shutil, "which", lambda _name: "codex")
    monkeypatch.setattr(engine.subprocess, "Popen", _Proc)


async def _turn(db, agent, *, message, history):
    return [event async for event in engine.stream(
        user_id="user", session_id="session", agent_id="agent",
        user_message=message, agent_rec=agent, db=db, history=history,
        parent_interaction_id="user-1",
    )]


def test_wrapper_checkpoint_and_native_transition_are_one_durable_flow(
    monkeypatch, tmp_path,
):
    db = _SQLiteDb(tmp_path / "wrapper-e2e.db")
    db.seed()
    invocations = []
    _install_fake_codex(monkeypatch, invocations)
    wrapper_agent = {"metadata": json.dumps({"codex_code": {
        "context_mode": "webagent_wrapper",
    }})}
    history = [{"role": "user", "content": "Earlier durable context"}]

    first_events = asyncio.run(_turn(
        db, wrapper_agent, message="Implement it", history=history,
    ))
    final_id = next(event["asst_id"] for event in first_events
                    if event.get("type") == "response")

    first = invocations[0]
    assert first["cmd"][:3] == ["codex", "exec", "--ephemeral"]
    assert "resume" not in first["cmd"]
    assert first["kwargs"]["stdin"] == subprocess.PIPE
    assert first["proc"].stdin.closed
    assert "Earlier durable context" in first["proc"].stdin.value
    assert db.native_reads == 0
    assert db.native_writes == []

    conn = db._get_conn()
    task = conn.execute(
        "SELECT id FROM codex_context_tasks WHERE session_id='session'",
    ).fetchone()
    snapshot = conn.execute(
        "SELECT task_id, turn_id, packet_chars FROM codex_context_snapshots "
        "WHERE session_id='session'",
    ).fetchone()
    conn.close()
    assert task is not None
    assert snapshot["task_id"] == task["id"]
    assert snapshot["turn_id"] == "user-1"
    assert snapshot["packet_chars"] == len(first["proc"].stdin.value)

    target = task_state_for_interaction(db, "session", final_id)
    assert target and target["task_id"] == task["id"]
    saved = asyncio.run(output_closer._save_codex_closer_checkpoint(
        db=db, agent_id="agent", session_id="session", target=target,
        request="Implement it", summary="Wrapper flow is implemented.",
        audit_eligible=True, audit_verdict="pass", audit_missing=[],
        final_asst_id=final_id, closer_row_id="closer-1",
    ))
    assert saved

    asyncio.run(_turn(
        db, wrapper_agent, message="Continue wrapped", history=history,
    ))
    second = invocations[1]
    assert second["cmd"][:3] == ["codex", "exec", "--ephemeral"]
    assert "Durable task checkpoint" in second["proc"].stdin.value
    assert '"user_summary":"Wrapper flow is implemented."' in second["proc"].stdin.value
    assert '"verdict":"pass"' in second["proc"].stdin.value
    assert db.native_reads == 0
    assert db.native_writes == []

    native_agent = {"metadata": json.dumps({"codex_code": {
        "context_mode": "native_codex",
    }})}
    asyncio.run(_turn(
        db, native_agent, message="Continue natively", history=history,
    ))
    third = invocations[2]
    assert third["cmd"][:2] == ["codex", "exec"]
    assert "resume" not in third["cmd"]
    assert "--ephemeral" not in third["cmd"]
    assert "stale-native-thread" not in third["cmd"]
    assert third["kwargs"]["stdin"] == subprocess.PIPE
    assert "Durable task checkpoint" in third["proc"].stdin.value
    assert "Earlier durable context" in third["proc"].stdin.value
    assert db.native_reads == 0
    # Generation-fenced persistence writes atomically through the context store
    # instead of the legacy unfenced setter.
    assert db.native_writes == []

    conn = db._get_conn()
    metadata = json.loads(conn.execute(
        "SELECT metadata FROM sessions WHERE id='session'",
    ).fetchone()[0])
    mode = conn.execute(
        "SELECT context_mode, generation FROM codex_session_context_modes "
        "WHERE session_id='session'",
    ).fetchone()
    snapshot_count = conn.execute(
        "SELECT COUNT(*) FROM codex_context_snapshots WHERE session_id='session'",
    ).fetchone()[0]
    conn.close()
    assert metadata["codex_thread_id"] == "fresh-native-thread"
    assert metadata["keep"] is True
    assert mode["context_mode"] == "native_codex"
    assert mode["generation"] == 2
    assert snapshot_count == 3

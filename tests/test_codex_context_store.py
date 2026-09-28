import sqlite3
import threading

from plugins.engines.codex.context_store import (
    claim_native_run,
    current_checkpoint,
    invalidate_native_thread,
    materialize_session_tasks,
    note_session_mode,
    record_snapshot,
    persist_native_thread_for_run,
    release_native_run,
    save_checkpoint,
)


class _Db:
    def __init__(self, path):
        self.path = str(path)

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _seed(db):
    conn = db._get_conn()
    conn.executescript(
        """
        CREATE TABLE interactions (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
          source TEXT, status TEXT DEFAULT 'complete', session_seq INTEGER,
          created_at TEXT
        );
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY, metadata TEXT, updated_at TEXT
        );
        INSERT INTO sessions (id, metadata, updated_at)
        VALUES ('s', '{"codex_thread_id":"stale","keep":true}', datetime('now'));
        """
    )
    rows = [
        ("u1", "s", "user", "Implement the wrapper", "user", 1),
        ("a1", "s", "assistant", "I will implement it.", "agent", 2),
        ("t1", "s", "tool", "large output", "agent", 3),
        ("u2", "s", "user", "also add tests", "user", 4),
        ("a2", "s", "assistant", "Tests added.", "agent", 5),
        ("u3", "s", "user", "Explain quantum gravity", "user", 6),
    ]
    conn.executemany(
        "INSERT INTO interactions "
        "(id,session_id,role,content,source,session_seq,created_at) "
        "VALUES (?,?,?,?,?,?,datetime('now'))", rows,
    )
    conn.commit()
    conn.close()


def test_materialized_tasks_are_stable_and_assign_non_user_rows(tmp_path):
    db = _Db(tmp_path / "ctx.db")
    _seed(db)
    latest = materialize_session_tasks(db, "s")

    conn = db._get_conn()
    tasks = conn.execute(
        "SELECT id, root_interaction_id FROM codex_context_tasks ORDER BY created_at, root_interaction_id"
    ).fetchall()
    assignments = conn.execute(
        "SELECT interaction_id, task_id FROM codex_context_assignments"
    ).fetchall()
    conn.close()
    by_row = {r["interaction_id"]: r["task_id"] for r in assignments}

    assert len(tasks) == 2
    assert by_row["u1"] == by_row["a1"] == by_row["t1"] == by_row["u2"] == by_row["a2"]
    assert by_row["u3"] == latest
    assert by_row["u3"] != by_row["u1"]
    assert materialize_session_tasks(db, "s") == latest


def test_checkpoint_compare_and_swap_and_snapshot(tmp_path):
    db = _Db(tmp_path / "ctx.db")
    _seed(db)
    task_id = materialize_session_tasks(db, "s")
    assert task_id
    assert save_checkpoint(db, task_id, {"status": "needs_input", "remaining": ["approval"]},
                           expected_revision=0)
    assert not save_checkpoint(db, task_id, {"status": "complete"}, expected_revision=0)
    saved = current_checkpoint(db, "s")
    assert saved["status"] == "needs_input"
    assert saved["revision"] == 1
    assert saved["checkpoint"]["remaining"] == ["approval"]

    snapshot_id = record_snapshot(
        db, session_id="s", task_id=task_id, turn_id="u3",
        packet="bounded packet", included_ids=["u3"], omitted_ids=["t1"],
    )
    assert snapshot_id and snapshot_id.startswith("codex-snapshot-")
    conn = db._get_conn()
    row = conn.execute(
        "SELECT packet_chars, included_json, omitted_json FROM codex_context_snapshots WHERE id=?",
        (snapshot_id,),
    ).fetchone()
    conn.close()
    assert row["packet_chars"] == len("bounded packet")
    assert row["included_json"] == '["u3"]'
    assert row["omitted_json"] == '["t1"]'


def test_session_mode_transition_is_generation_fenced(tmp_path):
    db = _Db(tmp_path / "ctx.db")
    _seed(db)
    assert note_session_mode(db, "s", "native_codex") is None
    assert note_session_mode(db, "s", "native_codex") is None
    assert note_session_mode(db, "s", "webagent_wrapper") == "native_codex"
    assert note_session_mode(db, "s", "native_codex") == "webagent_wrapper"
    conn = db._get_conn()
    row = conn.execute(
        "SELECT context_mode, generation FROM codex_session_context_modes WHERE session_id='s'"
    ).fetchone()
    transitioned_meta = conn.execute(
        "SELECT metadata FROM sessions WHERE id='s'"
    ).fetchone()[0]
    conn.close()
    assert row["context_mode"] == "native_codex"
    assert row["generation"] == 3
    assert 'codex_thread_id' not in transitioned_meta
    # The standalone invalidator remains idempotent for repair/migration calls.
    assert invalidate_native_thread(db, "s")
    conn = db._get_conn()
    meta = conn.execute("SELECT metadata FROM sessions WHERE id='s'").fetchone()[0]
    conn.close()
    assert 'codex_thread_id' not in meta
    assert '"keep": true' in meta


def test_native_run_claim_serializes_and_fences_thread_persistence(tmp_path):
    db = _Db(tmp_path / "ctx.db")
    _seed(db)
    note_session_mode(db, "s", "native_codex")
    generation = claim_native_run(db, "s", "run-one")
    assert generation == 1
    assert claim_native_run(db, "s", "run-two") is None
    assert persist_native_thread_for_run(
        db, "s", generation, "run-one", "thread-one",
    )

    # A mode change invalidates the in-flight generation before it can write.
    assert note_session_mode(db, "s", "webagent_wrapper") == "native_codex"
    assert not persist_native_thread_for_run(
        db, "s", generation, "run-one", "stale-thread",
    )
    assert note_session_mode(db, "s", "native_codex") == "webagent_wrapper"
    next_generation = claim_native_run(db, "s", "run-three")
    assert next_generation == 3
    release_native_run(db, "s", next_generation, "run-three")


def test_hybrid_backend_uses_local_mirror_for_engine_tables(tmp_path):
    local = _Db(tmp_path / "ctx.db")
    _seed(local)

    class _Hybrid:
        _local = local

    assert materialize_session_tasks(_Hybrid(), "s")
    assert note_session_mode(_Hybrid(), "s", "webagent_wrapper") is None


def test_schema_bootstrap_runs_once_under_contention(tmp_path):
    class _CountingDb(_Db):
        def __init__(self, path):
            super().__init__(path)
            self.connections = 0

        def _get_conn(self):
            self.connections += 1
            return super()._get_conn()

    db = _CountingDb(tmp_path / "ctx.db")
    _seed(db)
    baseline = db.connections
    threads = [threading.Thread(target=materialize_session_tasks, args=(db, "s"))
               for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # One connection performs DDL. Other connections are ordinary materialize
    # work; repeated ensure_schema calls do not add further DDL connections.
    after_parallel = db.connections
    assert materialize_session_tasks(db, "s")
    assert db.connections == after_parallel + 1
    assert after_parallel >= baseline + 7

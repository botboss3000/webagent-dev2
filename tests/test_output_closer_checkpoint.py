import asyncio
import json
import sqlite3

from app.agent import output_closer
from plugins.engines.codex.context_store import (
    current_checkpoint,
    materialize_session_tasks,
    save_checkpoint,
    task_state_for_interaction,
)


class _Db:
    def __init__(self, path, *, enabled=True):
        self.path = str(path)
        self.enabled = enabled

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    async def get_agent_by_id(self, _agent_id):
        return {
            "id": "codex-agent",
            "metadata": json.dumps({
                "codex_code": {"closer_enabled": self.enabled},
            }),
        }


def _seed(db):
    conn = db._get_conn()
    conn.executescript(
        """
        CREATE TABLE interactions (
          id TEXT PRIMARY KEY, session_id TEXT, role TEXT, content TEXT,
          source TEXT, status TEXT DEFAULT 'complete', session_seq INTEGER,
          created_at TEXT, tool_name TEXT, tool_call_id TEXT, metadata TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO interactions "
        "(id,session_id,role,content,source,status,session_seq,created_at,"
        "tool_name,tool_call_id,metadata) "
        "VALUES (?,?,?,?,?,?,?,datetime('now'),?,?,?)",
        [
            ("u1", "s", "user", "Implement durable checkpoints", "user",
             "complete", 1, None, None, None),
            ("t1", "s", "tool", "secret raw output that must not be copied",
             "agent", "complete", 2, "run_tests", "call-1",
             json.dumps({"success": True, "exit_code": 0, "unsafe": "omit me"})),
            ("a1", "s", "assistant", "Implemented and tested.", "agent",
             "complete", 3, None, None, None),
        ],
    )
    conn.commit()
    conn.close()
    materialize_session_tasks(db, "s")


def _target(db):
    target = task_state_for_interaction(db, "s", "a1")
    assert target
    return target


def _save(db, target, *, verdict, missing=(), eligible=True):
    return asyncio.run(output_closer._save_codex_closer_checkpoint(
        db=db,
        agent_id="codex-agent",
        session_id="s",
        target=target,
        request="Implement durable checkpoints",
        summary="Checkpoint support is implemented and verified.",
        audit_eligible=eligible,
        audit_verdict=verdict,
        audit_missing=list(missing),
        final_asst_id="a1",
        closer_row_id="closer-1",
    ))


def test_pass_checkpoint_is_complete_and_payload_free(tmp_path):
    db = _Db(tmp_path / "pass.db")
    _seed(db)

    assert _save(db, _target(db), verdict="pass")
    saved = current_checkpoint(db, "s")
    body = saved["checkpoint"]
    assert saved["status"] == "complete"
    assert body["audit"] == {"verdict": "pass", "missing": []}
    assert body["references"] == {
        "final_interaction_id": "a1",
        "closer_interaction_id": "closer-1",
        "final_session_seq": 3,
    }
    assert body["completed"]
    assert body["remaining"] == []
    assert body["tool_evidence"][0]["tool"] == "run_tests"
    assert body["tool_evidence"][0]["outcome"] == {
        "success": True, "exit_code": 0,
    }
    assert "secret raw output" not in json.dumps(body)
    assert "unsafe" not in json.dumps(body)


def test_failed_audit_checkpoint_records_remaining_work(tmp_path):
    db = _Db(tmp_path / "fail.db")
    _seed(db)

    assert _save(db, _target(db), verdict="fail", missing=["integration test"])
    saved = current_checkpoint(db, "s")
    assert saved["status"] == "needs_work"
    assert saved["checkpoint"]["completed"] == []
    assert saved["checkpoint"]["remaining"] == ["integration test"]
    assert saved["checkpoint"]["audit"]["verdict"] == "fail"


def test_stale_closer_cannot_overwrite_newer_checkpoint(tmp_path):
    db = _Db(tmp_path / "stale.db")
    _seed(db)
    stale_target = _target(db)
    assert save_checkpoint(
        db, stale_target["task_id"],
        {"status": "complete", "user_summary": "newer result"},
        expected_revision=stale_target["revision"],
    )

    assert not _save(db, stale_target, verdict="pass")
    saved = current_checkpoint(db, "s")
    assert saved["checkpoint"]["user_summary"] == "newer result"
    assert saved["revision"] == 1


def _add_newer_final(db):
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO interactions "
        "(id,session_id,role,content,source,status,session_seq,created_at,metadata) "
        "VALUES ('a2','s','assistant','Newer result','agent','complete',4,"
        "datetime('now'),'{}')"
    )
    conn.commit()
    conn.close()
    materialize_session_tasks(db, "s")


def _ordered_checkpoint(seq, summary):
    return {
        "status": "complete",
        "user_summary": summary,
        "references": {"final_interaction_id": f"a{seq}",
                       "final_session_seq": seq},
    }


def test_newer_turn_supersedes_older_checkpoint_that_won_cas_first(tmp_path):
    db = _Db(tmp_path / "old-first.db")
    _seed(db)
    _add_newer_final(db)
    old = task_state_for_interaction(db, "s", "a1")
    new = task_state_for_interaction(db, "s", "a2")
    assert old["revision"] == new["revision"] == 0

    assert save_checkpoint(
        db, old["task_id"], _ordered_checkpoint(3, "old"),
        expected_revision=old["revision"],
    )
    # Its captured revision is stale, but its final turn is durably newer.
    assert save_checkpoint(
        db, new["task_id"], _ordered_checkpoint(4, "new"),
        expected_revision=new["revision"],
    )
    assert current_checkpoint(db, "s")["checkpoint"]["user_summary"] == "new"


def test_older_turn_cannot_overwrite_newer_checkpoint_that_finishes_first(tmp_path):
    db = _Db(tmp_path / "new-first.db")
    _seed(db)
    _add_newer_final(db)
    old = task_state_for_interaction(db, "s", "a1")
    new = task_state_for_interaction(db, "s", "a2")

    assert save_checkpoint(
        db, new["task_id"], _ordered_checkpoint(4, "new"),
        expected_revision=new["revision"],
    )
    assert not save_checkpoint(
        db, old["task_id"], _ordered_checkpoint(3, "old"),
        expected_revision=old["revision"],
    )
    assert current_checkpoint(db, "s")["checkpoint"]["user_summary"] == "new"


def test_delayed_older_closer_is_rejected_even_if_it_captures_latest_revision(tmp_path):
    db = _Db(tmp_path / "delayed-old.db")
    _seed(db)
    _add_newer_final(db)
    new = task_state_for_interaction(db, "s", "a2")
    assert save_checkpoint(
        db, new["task_id"], _ordered_checkpoint(4, "new"),
        expected_revision=new["revision"],
    )
    delayed_old = task_state_for_interaction(db, "s", "a1")
    assert delayed_old["revision"] == 1
    assert not save_checkpoint(
        db, delayed_old["task_id"], _ordered_checkpoint(3, "old"),
        expected_revision=delayed_old["revision"],
    )
    assert current_checkpoint(db, "s")["checkpoint"]["user_summary"] == "new"


def test_disabled_closer_does_not_write_checkpoint(tmp_path):
    db = _Db(tmp_path / "disabled.db", enabled=False)
    _seed(db)

    assert not _save(db, _target(db), verdict="pass")
    saved = current_checkpoint(db, "s")
    assert saved["revision"] == 0
    assert saved["checkpoint"] == {}


def test_missing_or_inconclusive_audit_never_completes(tmp_path):
    for verdict in (None, "inconclusive", "skipped"):
        db = _Db(tmp_path / f"audit-{verdict}.db")
        _seed(db)
        assert _save(db, _target(db), verdict=verdict)
        assert current_checkpoint(db, "s")["status"] == "needs_work"


def test_pass_with_missing_requirement_never_completes(tmp_path):
    db = _Db(tmp_path / "missing.db")
    _seed(db)
    assert _save(db, _target(db), verdict="pass", missing=["unverified requirement"])
    assert current_checkpoint(db, "s")["status"] == "needs_work"

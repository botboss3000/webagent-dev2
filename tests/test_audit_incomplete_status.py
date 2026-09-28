"""Close-out status for an audit-failed run.

A FAIL verdict used to fall through to the normal summary write, which left the
durable run row as ``complete`` — a false green: the summary told the user the
work was unfinished while the machine row claimed success, so neither the user
nor the recovery ladder could see that anything was outstanding. Worse, the
send-back budget is small (the execution-mode contract resolves to
``max_rounds=1``), so the *second* audit failure in a task always took that path
— observed in production as the repeated
``checklist FAIL round 2/1; closing with N item(s) still missing`` log line.

These tests pin the replacement behaviour: an audit-failed close always records
WHY it stopped, and stays recoverable only when a retry could actually help.
"""

import asyncio
import inspect

from app.agent import output_closer
from app.agent.runner import _RESUMABLE_CAUSES
from app.db.local import LocalBackend


class _FakeDB:
    """Minimal stand-in capturing the terminal run-state write."""

    def __init__(self, explode: bool = False):
        self.calls = []
        self.explode = explode

    async def run_state_finish(
        self, session_id, status="complete", error=None, stop_cause=None, **kwargs
    ):
        if self.explode:
            raise RuntimeError("db down")
        self.calls.append({
            "session_id": session_id,
            "status": status,
            "error": error,
            "stop_cause": stop_cause,
        })
        return True


def _record(missing, *, blocked, round_no=2, max_rounds=1, explode=False):
    db = _FakeDB(explode=explode)
    asyncio.run(output_closer._record_audit_blockage(
        db, "sess-1", missing,
        round_no=round_no, max_rounds=max_rounds, blocked=blocked,
    ))
    return db


def test_audit_incomplete_waits_for_a_fresh_user_turn():
    # The status remains durable and actionable but never retries the same turn.
    assert "audit_incomplete" not in _RESUMABLE_CAUSES
    assert "audit_incomplete" not in LocalBackend._RESUMABLE_CAUSES


def test_manual_resume_cause_is_never_auto_resumed():
    assert "needs_manual_resume" not in _RESUMABLE_CAUSES
    assert "needs_manual_resume" not in LocalBackend._RESUMABLE_CAUSES


def test_recoverable_shortfall_never_closes_as_complete():
    db = _record(["Step A missing", "Step B missing"], blocked=False)
    assert len(db.calls) == 1
    call = db.calls[0]
    assert call["status"] != "complete"
    assert call["status"] == "interrupted"
    assert call["stop_cause"] == "audit_incomplete"
    assert "2 checklist item" in call["error"]
    assert "Step A missing" in call["error"]


def test_prohibited_execution_holds_for_a_human_instead_of_retrying():
    missing = ["No requested implementation or other mutating action was executed"]
    assert output_closer._read_only_execution_violation(missing) is True
    db = _record(missing, blocked=True)
    call = db.calls[0]
    assert call["stop_cause"] == "needs_manual_resume"
    assert "blocked" in call["error"]


def test_reason_records_the_round_budget():
    db = _record(["x"], blocked=False, round_no=2, max_rounds=1)
    assert "round 2/1" in db.calls[0]["error"]


def test_long_missing_lists_are_counted_but_truncated():
    db = _record([f"item {i}" for i in range(6)], blocked=False)
    error = db.calls[0]["error"]
    assert "6 checklist item" in error
    assert "item 0" in error
    assert "item 3" not in error


def test_bookkeeping_failure_never_breaks_close_out():
    _record(["x"], blocked=False, explode=True)  # must not raise


def test_fail_branch_is_wired_to_the_recorder():
    # Guard the call site: the summary path below the audit verdict is exactly
    # what used to leave an audit-failed run reading as 'complete'.
    src = inspect.getsource(output_closer.run_output_closer)
    assert "_record_audit_blockage(" in src

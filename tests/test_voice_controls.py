import asyncio
from unittest.mock import AsyncMock

from app.agent.run_manager import RunManager
from app.agent.runner import RunOutcome, _classify_resume, _finish_run
from app.agent.voice_controls import VoiceControlCoordinator


class _Manager:
    def __init__(self, turn_id="turn-1", changed=True):
        self.turn_id = turn_id
        self.changed = changed
        self.pause_calls = []
        self.interrupt_calls = []

    def active_turn_id(self, session_id):
        return self.turn_id

    async def pause(self, session_id, db):
        self.pause_calls.append((session_id, db))
        return self.changed

    async def interrupt(self, session_id, db, cause="user_stop"):
        self.interrupt_calls.append((session_id, db, cause))
        return self.changed


def test_pause_uses_user_pause_path_and_is_idempotent():
    manager = _Manager()
    coordinator = VoiceControlCoordinator(run_manager=manager)
    db = object()

    first = asyncio.run(coordinator.execute(
        action="pause", user_id="u1", session_id="s1",
        target_turn_id="turn-1", idempotency_key="pause-key", db=db,
    ))
    second = asyncio.run(coordinator.execute(
        action="pause", user_id="u1", session_id="s1",
        target_turn_id="turn-1", idempotency_key="pause-key", db=db,
    ))

    assert first.accepted
    assert first.code == "run_paused"
    assert second.accepted and second.replayed
    assert manager.pause_calls == [("s1", db)]
    assert first.audit["source"] == "voice_control"


def test_stale_target_is_rejected_without_touching_run():
    manager = _Manager(turn_id="new-turn")
    coordinator = VoiceControlCoordinator(run_manager=manager)

    result = asyncio.run(coordinator.execute(
        action="stop", user_id="u1", session_id="s1",
        target_turn_id="old-turn", idempotency_key="stale-key", db=object(),
    ))

    assert result.status == "rejected"
    assert result.code == "stale_target"
    assert manager.interrupt_calls == []


def test_stop_calls_existing_guardrailed_interrupt():
    manager = _Manager()
    coordinator = VoiceControlCoordinator(run_manager=manager)
    db = object()

    result = asyncio.run(coordinator.execute(
        action="stop", user_id="u1", session_id="s1",
        idempotency_key="stop-key-1", db=db,
    ))

    assert result.accepted
    assert manager.interrupt_calls == [("s1", db, "user_stop")]


def test_resume_is_explicit_and_idempotent():
    resume = AsyncMock(return_value=True)
    coordinator = VoiceControlCoordinator(manual_resume=resume)

    first = asyncio.run(coordinator.execute(
        action="resume", user_id="u1", session_id="s1",
        idempotency_key="resume-key",
    ))
    second = asyncio.run(coordinator.execute(
        action="resume", user_id="u1", session_id="s1",
        idempotency_key="resume-key",
    ))

    assert first.code == "run_resumed"
    assert second.replayed
    resume.assert_awaited_once_with("s1")


def test_user_pause_is_never_auto_resumable():
    result = asyncio.run(_classify_resume(
        object(), {"session_id": "s1", "origin": "web", "stop_cause": "user_pause"}
    ))

    assert result == ("skip", "voluntary stop cause 'user_pause'")


def test_contract_blocked_waits_for_a_fresh_user_turn():
    result = asyncio.run(_classify_resume(
        object(), {"session_id": "s1", "origin": "web", "stop_cause": "contract_blocked"}
    ))

    assert result == ("skip", "cause 'contract_blocked' not resumable")


def test_work_not_started_waits_for_a_fresh_user_turn():
    result = asyncio.run(_classify_resume(
        object(), {"session_id": "s1", "origin": "web", "stop_cause": "work_not_started"}
    ))

    assert result == ("skip", "cause 'work_not_started' not resumable")




def test_run_finalization_preserves_recorded_user_pause_over_crash():
    class Db:
        def __init__(self):
            self.finished = None

        async def run_state_get(self, _session_id):
            return {"stop_cause": "user_pause", "resume_attempts": 0}

        async def run_state_finish(self, session_id, **values):
            self.finished = (session_id, values)

    db = Db()
    asyncio.run(_finish_run(
        db, "s1", RunOutcome(status="error", stop_cause="crash", error="boom")
    ))

    assert db.finished[0] == "s1"
    assert db.finished[1]["stop_cause"] == "user_pause"


def test_run_finalization_preserves_watchdog_frozen_cause_over_generic_interrupt():
    """A hard watchdog cancel raises CancelledError, whose runner outcome has
    no cause.  The prior durable frozen classification must survive so the
    terminal watchdog sweep can launch the remaining recovery attempt."""
    class Db:
        def __init__(self):
            self.finished = None

        async def run_state_get(self, _session_id):
            return {"stop_cause": "frozen", "resume_attempts": 2}

        async def run_state_finish(self, session_id, **values):
            self.finished = (session_id, values)

    db = Db()
    asyncio.run(_finish_run(db, "s1", RunOutcome(status="interrupted")))

    assert db.finished[0] == "s1"
    assert db.finished[1]["stop_cause"] == "frozen"


def test_run_finalization_marks_unattributed_cancellation_resumable():
    """A cancelled worker with no user-stop marker must enter recovery."""
    class Db:
        def __init__(self):
            self.finished = None

        async def run_state_get(self, _session_id):
            return {"stop_cause": None, "resume_attempts": 0}

        async def run_state_finish(self, session_id, **values):
            self.finished = (session_id, values)

    db = Db()
    asyncio.run(_finish_run(
        db, "s1", RunOutcome(status="interrupted", stop_cause="crash")
    ))

    assert db.finished[0] == "s1"
    assert db.finished[1]["status"] == "interrupted"
    assert db.finished[1]["stop_cause"] == "crash"


def test_approval_becomes_structured_turn_not_direct_execution():
    coordinator = VoiceControlCoordinator()

    result = asyncio.run(coordinator.execute(
        action="approve", user_id="u1", session_id="s1",
        approval_id="proposal-7", target_turn_id="turn-1",
        idempotency_key="approve-1",
    ))

    assert result.accepted
    assert result.code == "dispatch_required"
    assert result.dispatch["kind"] == "structured_user_turn"
    assert result.dispatch["source"] == "voice:control"
    assert result.dispatch["metadata"]["approval_id"] == "proposal-7"
    assert result.dispatch["content"] == "Approve pending action proposal-7."


def test_change_requires_confirmation_and_only_audits_qualifiers():
    coordinator = VoiceControlCoordinator()
    utterance = "Use SQLite\x00 and delete the old adapter"

    unconfirmed = asyncio.run(coordinator.execute(
        action="apply_change", user_id="u1", session_id="s1",
        command=utterance, idempotency_key="change-01",
    ))
    confirmed = asyncio.run(coordinator.execute(
        action="apply_change", user_id="u1", session_id="s1",
        command=utterance, approval_id="proposal-9", idempotency_key="change-02",
    ))

    assert unconfirmed.code == "approval_required"
    assert confirmed.accepted
    assert confirmed.dispatch["content"] == "Use SQLite and delete the old adapter"
    assert utterance not in repr(confirmed.audit)
    assert "content" not in confirmed.audit


def test_idempotency_key_payload_conflict_is_rejected():
    coordinator = VoiceControlCoordinator()

    asyncio.run(coordinator.execute(
        action="reject", user_id="u1", session_id="s1",
        approval_id="proposal-1", idempotency_key="shared-key",
    ))
    conflict = asyncio.run(coordinator.execute(
        action="approve", user_id="u1", session_id="s1",
        approval_id="proposal-1", idempotency_key="shared-key",
    ))

    assert conflict.status == "rejected"
    assert conflict.code == "idempotency_conflict"


def test_idempotency_is_tenant_scoped():
    resume = AsyncMock(return_value=True)
    coordinator = VoiceControlCoordinator(manual_resume=resume)

    for user_id in ("u1", "u2"):
        result = asyncio.run(coordinator.execute(
            action="resume", user_id=user_id, session_id="s1",
            idempotency_key="tenant-key",
        ))
        assert not result.replayed

    assert resume.await_count == 2


def test_run_manager_pause_tags_user_pause():
    manager = RunManager()
    manager.interrupt = AsyncMock(return_value=True)
    db = object()

    assert asyncio.run(manager.pause("s1", db)) is True
    manager.interrupt.assert_awaited_once_with("s1", db, cause="user_pause")

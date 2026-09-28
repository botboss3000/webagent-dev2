import asyncio
import sqlite3
import tempfile
from pathlib import Path

from app.agent.run_fence import resolve_run_turn_id, side_effects_allowed
from app.agent.run_manager import RunManager


class _FenceDb:
    def __init__(self, row=None, db_path=None):
        self.row = row
        self.db_path = db_path
        self.causes = []
        self.interrupts = []

    def _get_conn(self):
        if self.db_path is None:
            raise RuntimeError("interaction lookup unavailable")
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def run_state_get(self, _session_id):
        return self.row

    async def run_state_set_cause(self, _session_id, cause):
        self.causes.append(cause)
        if self.row is not None:
            self.row["stop_cause"] = cause

    async def set_interrupt(self, session_id):
        self.interrupts.append(session_id)


def test_durable_fence_rejects_replaced_generation():
    db = _FenceDb({"turn_id": "turn-2", "stop_cause": None})
    assert not asyncio.run(side_effects_allowed(
        db, "session", expected_turn_id="turn-1"))


def test_durable_fence_rejects_user_stop():
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "user_stop"})
    assert not asyncio.run(side_effects_allowed(
        db, "session", expected_turn_id="turn-1"))


def test_intermediate_parent_resolves_to_durable_turn_before_fencing():
    with tempfile.TemporaryDirectory() as tmp:
        path = str(Path(tmp) / "fence.db")
        conn = sqlite3.connect(path)
        try:
            conn.execute("CREATE TABLE interactions (id TEXT PRIMARY KEY, turn_id TEXT)")
            conn.execute(
                "INSERT INTO interactions (id, turn_id) VALUES (?, ?)",
                ("memory-search-1", "user-turn-1"),
            )
            conn.commit()
        finally:
            conn.close()

        db = _FenceDb(
            {"turn_id": "user-turn-1", "stop_cause": None},
            db_path=path,
        )

        # This was the production failure: the transcript parent is not the run
        # generation and is correctly rejected when compared directly.
        assert not asyncio.run(side_effects_allowed(
            db, "session", expected_turn_id="memory-search-1"))

        resolved = resolve_run_turn_id(db, interaction_id="memory-search-1")
        assert resolved == "user-turn-1"
        assert asyncio.run(side_effects_allowed(
            db, "session", expected_turn_id=resolved))


def test_run_turn_resolution_prefers_explicit_id_and_avoids_unsafe_fallback():
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": None})
    assert resolve_run_turn_id(
        db,
        explicit_turn_id="turn-1",
        interaction_id="memory-search-1",
    ) == "turn-1"
    assert resolve_run_turn_id(
        db,
        interaction_id="legacy-parent",
    ) is None
    assert asyncio.run(side_effects_allowed(
        db, "session", expected_turn_id=None))

    stopped = _FenceDb({"turn_id": "turn-1", "stop_cause": "user_stop"})
    assert not asyncio.run(side_effects_allowed(
        stopped, "session", expected_turn_id=None))


def test_stop_button_cancels_auxiliary_without_poisoning_next_turn():
    asyncio.run(_assert_stop_button_cancels_auxiliary())


async def _assert_stop_button_cancels_auxiliary():
    manager = RunManager()
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "complete"})
    started = asyncio.Event()

    async def one_shot():
        manager.register_auxiliary("session", turn_id="turn-1")
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(one_shot())
    await started.wait()
    assert manager.has_auxiliary("session")

    assert await manager.interrupt("session", db, cause="user_stop")
    await asyncio.sleep(0)

    assert task.cancelled()
    assert not manager.has_auxiliary("session")
    assert db.causes == ["user_stop"]
    # There was no main agent loop to consume an interrupt flag.
    assert db.interrupts == []


def test_app_cancel_all_cancels_auxiliary_only_session():
    asyncio.run(_assert_app_cancel_all_cancels_auxiliary())


async def _assert_app_cancel_all_cancels_auxiliary():
    manager = RunManager()
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "complete"})
    db.run_state_tag_resumable_as_user_stop = _return_zero
    started = asyncio.Event()

    async def one_shot():
        manager.register_auxiliary("session", turn_id="turn-1")
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(one_shot())
    await started.wait()

    assert await manager.cancel_all(db) == 0
    await asyncio.sleep(0)
    assert task.cancelled()
    assert db.causes == ["user_stop"]


async def _return_zero():
    return 0


def test_footer_kill_switch_hard_cancels_live_session():
    asyncio.run(_assert_footer_kill_switch_hard_cancels_live_session())


async def _assert_footer_kill_switch_hard_cancels_live_session():
    manager = RunManager()
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": None})
    started = asyncio.Event()

    async def one_shot():
        manager.register_auxiliary("session", turn_id="turn-1")
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(one_shot())
    await started.wait()
    assert await manager.hard_stop_session_tree("session", db)
    await asyncio.sleep(0)
    assert task.cancelled()
    assert db.causes == ["user_stop"]
    assert db.interrupts == ["session"]


def test_footer_kill_switch_idle_session_does_not_create_interrupt():
    manager = RunManager()
    db = _FenceDb({"turn_id": "turn-1", "stop_cause": "complete"})
    assert not asyncio.run(manager.hard_stop_session_tree("session", db))
    assert db.causes == []
    assert db.interrupts == []

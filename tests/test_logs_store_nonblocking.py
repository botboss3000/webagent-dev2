import asyncio
import concurrent.futures
import sqlite3
import threading

from app.agent.diagnostics import DiagnosticRecorder, PRUNE_BATCH_ROWS
from app.db.logs_store import LogStore


class _Cursor:
    rowcount = 0


class _BlockingConnection:
    def __init__(self, started: threading.Event, release: threading.Event):
        self.started = started
        self.release = release
        self.closed = False
        self.thread_name = None

    def execute(self, *_args, **_kwargs):
        self.thread_name = threading.current_thread().name
        self.started.set()
        assert self.release.wait(timeout=2), "test did not release blocking SQLite call"
        return _Cursor()

    def commit(self):
        return None

    def close(self):
        self.closed = True


def test_log_prune_does_not_block_event_loop():
    async def scenario():
        started = threading.Event()
        release = threading.Event()
        conn = _BlockingConnection(started, release)
        store = object.__new__(LogStore)
        store._connect = lambda _path: conn
        lock = asyncio.Lock()

        prune = asyncio.create_task(
            store._prune(lock, "ignored.db", "diagnostics", 0, 60.0)
        )
        assert await asyncio.to_thread(started.wait, 1), "prune worker never started"

        # If SQLite is still running on the ASGI event loop this yield never
        # completes until the fake query is released (the production freeze).
        event_loop_turned = False

        async def ticker():
            nonlocal event_loop_turned
            await asyncio.sleep(0)
            event_loop_turned = True

        await asyncio.wait_for(ticker(), timeout=0.2)
        assert event_loop_turned
        assert not prune.done()

        release.set()
        assert await asyncio.wait_for(prune, timeout=1) == 0
        assert conn.closed
        assert conn.thread_name.startswith("log-maintenance")

    asyncio.run(scenario())


def test_log_prune_is_incremental(tmp_path):
    db_path = str(tmp_path / "logs.db")
    conn = sqlite3.connect(db_path)
    conn.execute("CREATE TABLE diagnostics (id TEXT PRIMARY KEY, ts TEXT NOT NULL)")
    conn.executemany(
        "INSERT INTO diagnostics (id, ts) VALUES (?, ?)",
        [(str(i), f"2026-01-01T00:00:{i:02d}+00:00") for i in range(25)],
    )
    conn.commit()
    conn.close()

    async def scenario():
        store = object.__new__(LogStore)
        store._connect = lambda path: sqlite3.connect(path)
        store._maintenance_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="log-maintenance-test"
        )
        lock = asyncio.Lock()
        try:
            assert await store._prune(
                lock, db_path, "diagnostics", 10, None, batch_rows=7
            ) == 7
            with sqlite3.connect(db_path) as check:
                assert check.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0] == 18
            assert await store._prune(
                lock, db_path, "diagnostics", 10, None, batch_rows=7
            ) == 7
            assert await store._prune(
                lock, db_path, "diagnostics", 10, None, batch_rows=7
            ) == 1
            with sqlite3.connect(db_path) as check:
                assert check.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0] == 10
        finally:
            store._maintenance_executor.shutdown(wait=True)

    asyncio.run(scenario())


def test_diagnostic_recorder_retries_only_when_a_full_batch_was_removed():
    class _Store:
        def __init__(self, diagnostics_deleted):
            self.diagnostics_deleted = diagnostics_deleted
            self.calls = []

        async def prune_diagnostics(self, **kwargs):
            self.calls.append(("diagnostics", kwargs))
            return self.diagnostics_deleted

        async def prune_tool_executions(self, **kwargs):
            self.calls.append(("tools", kwargs))
            return 0

    async def scenario(deleted):
        store = _Store(deleted)
        recorder = object.__new__(DiagnosticRecorder)
        recorder.retention_rows = 200000
        recorder.retention_hours = 168
        recorder._db = lambda: store
        backlog = await recorder._prune_once()
        assert all(call[1]["batch_rows"] == PRUNE_BATCH_ROWS for call in store.calls)
        return backlog

    assert asyncio.run(scenario(PRUNE_BATCH_ROWS)) is True
    assert asyncio.run(scenario(PRUNE_BATCH_ROWS - 1)) is False

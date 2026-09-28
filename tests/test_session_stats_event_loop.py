import asyncio
import sqlite3
import threading
from unittest.mock import patch

from starlette.requests import Request

from app.api import db_viewer


def _request() -> Request:
    return Request({
        "type": "http",
        "method": "GET",
        "path": "/api/v1/db/session-stats",
        "query_string": b"",
        "headers": [],
    })


def _reset_cache() -> None:
    with db_viewer._SESSION_STATS_CACHE_LOCK:
        assert not db_viewer._SESSION_STATS_INFLIGHT
        db_viewer._SESSION_STATS_CACHE.clear()
        db_viewer._SESSION_STATS_CACHE_EPOCH += 1


def test_session_stats_worker_binds_requested_user_before_opening_user_db():
    from app.db.local import get_db_user_context, set_db_user_context

    prior_user_id = get_db_user_context()
    seen_user_ids = []

    def open_user(_db):
        seen_user_ids.append(get_db_user_context())
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript("""
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY,
                user_id TEXT,
                title TEXT,
                agent_id TEXT,
                metadata TEXT,
                created_at TEXT,
                updated_at TEXT,
                status TEXT,
                pinned INTEGER DEFAULT 0,
                sort_order INTEGER
            );
            CREATE TABLE interactions (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                role TEXT,
                metadata TEXT,
                created_at TEXT
            );
            INSERT INTO sessions (
                id, user_id, title, created_at, updated_at, status
            ) VALUES (
                'guest-session', 'anon_guest', 'Guest session',
                '2026-08-22T12:00:00+00:00',
                '2026-08-22T12:00:00+00:00', 'active'
            );
        """)
        return conn, "sqlite"

    try:
        with patch.object(db_viewer, "_open_read", side_effect=open_user):
            result = db_viewer._build_session_stats_sync(
                "anon_guest", "user.db", "active"
            )
    finally:
        set_db_user_context(prior_user_id)

    assert seen_user_ids == ["anon_guest"]
    assert [row["session_id"] for row in result["sessions"]] == ["guest-session"]


def test_session_stats_summary_does_not_block_event_loop():
    _reset_cache()
    entered = threading.Event()
    release = threading.Event()

    def held_builder(user_id, db, status):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release held summary worker")
        return {"sessions": [], "db": db}

    async def exercise():
        with (
            patch.object(db_viewer, "_is_open_access_mode", return_value=True),
            patch.object(
                db_viewer,
                "_build_session_stats_sync",
                side_effect=held_builder,
            ),
        ):
            request_task = asyncio.create_task(db_viewer.session_stats(
                request=_request(),
                user_id="admin",
                db="user.db",
                status="active",
            ))

            deadline = asyncio.get_running_loop().time() + 1
            while not entered.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.005)

            heartbeat = asyncio.Event()

            async def tick():
                await asyncio.sleep(0)
                heartbeat.set()

            asyncio.create_task(tick())
            await asyncio.wait_for(heartbeat.wait(), timeout=0.25)
            assert not request_task.done()

            release.set()
            assert await asyncio.wait_for(request_task, timeout=1) == {
                "sessions": [],
                "db": "user.db",
            }

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    _reset_cache()


def test_warm_session_stats_reuses_cached_summary():
    _reset_cache()
    calls = 0

    def builder(user_id, db, status):
        nonlocal calls
        calls += 1
        return {"sessions": [{"session_id": "cached"}], "db": db}

    async def exercise():
        with (
            patch.object(db_viewer, "_is_open_access_mode", return_value=True),
            patch.object(db_viewer, "_build_session_stats_sync", side_effect=builder),
        ):
            first = await db_viewer.session_stats(_request(), "admin", "user.db", "active")
            second = await db_viewer.session_stats(_request(), "admin", "user.db", "active")
        assert first == second

    asyncio.run(exercise())
    assert calls == 1
    _reset_cache()


def test_concurrent_cold_callers_share_one_summary_build():
    _reset_cache()
    entered = threading.Event()
    release = threading.Event()
    calls = 0

    def builder(user_id, db, status):
        nonlocal calls
        calls += 1
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release held summary worker")
        return {"sessions": [], "db": db}

    async def exercise():
        with (
            patch.object(db_viewer, "_is_open_access_mode", return_value=True),
            patch.object(db_viewer, "_build_session_stats_sync", side_effect=builder),
        ):
            tasks = [
                asyncio.create_task(db_viewer.session_stats(
                    _request(), "admin", "user.db", "active"
                ))
                for _ in range(2)
            ]
            deadline = asyncio.get_running_loop().time() + 1
            while not entered.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.005)
            assert calls == 1
            release.set()
            await asyncio.gather(*tasks)

    try:
        asyncio.run(exercise())
    finally:
        release.set()
    assert calls == 1
    _reset_cache()


def test_stale_session_stats_returns_while_single_refresh_runs():
    _reset_cache()
    key = db_viewer._session_stats_cache_key("admin", "user.db", "active")
    stale = {"sessions": [{"session_id": "stale"}], "db": "user.db"}
    with db_viewer._SESSION_STATS_CACHE_LOCK:
        db_viewer._SESSION_STATS_CACHE[key] = (
            db_viewer.time.monotonic() - db_viewer._SESSION_STATS_CACHE_TTL_S - 1,
            stale,
        )

    entered = threading.Event()
    release = threading.Event()

    def builder(user_id, db, status):
        entered.set()
        if not release.wait(timeout=5):
            raise TimeoutError("test did not release held summary worker")
        return {"sessions": [{"session_id": "fresh"}], "db": db}

    async def exercise():
        with (
            patch.object(db_viewer, "_is_open_access_mode", return_value=True),
            patch.object(db_viewer, "_build_session_stats_sync", side_effect=builder),
        ):
            result = await asyncio.wait_for(
                db_viewer.session_stats(_request(), "admin", "user.db", "active"),
                timeout=0.25,
            )
            assert result == stale
            deadline = asyncio.get_running_loop().time() + 1
            while not entered.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0.005)
            release.set()

    try:
        asyncio.run(exercise())
    finally:
        release.set()

    future = db_viewer._SESSION_STATS_INFLIGHT.get(key)
    if future is not None:
        future.result(timeout=1)
    with db_viewer._SESSION_STATS_CACHE_LOCK:
        assert db_viewer._SESSION_STATS_CACHE[key][1]["sessions"][0]["session_id"] == "fresh"
    _reset_cache()

from __future__ import annotations

import asyncio
import threading
import time

from app.api import db_viewer


def test_ordinary_session_message_poll_skips_full_manifest_hash() -> None:
    assert db_viewer._session_messages_needs_manifest(False, None, None) is False
    assert db_viewer._session_messages_needs_manifest(True, None, None) is True
    assert db_viewer._session_messages_needs_manifest(False, 4, "hash") is True


def test_manifest_rebuild_runs_outside_event_loop(monkeypatch) -> None:
    worker_thread = []
    started = threading.Event()

    def slow_manifest(_db_name: str, session_id: str) -> dict:
        worker_thread.append(threading.get_ident())
        started.set()
        time.sleep(0.15)
        return {"session_id": session_id}

    monkeypatch.setattr(db_viewer, "_compute_session_manifest_for_db", slow_manifest)

    async def scenario():
        caller_thread = threading.get_ident()
        task = asyncio.create_task(db_viewer._compute_session_manifest_offloop("user.db", "s1"))
        for _ in range(20):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        await asyncio.sleep(0.01)
        assert not task.done()
        result = await task
        return caller_thread, result

    caller_thread, result = asyncio.run(scenario())
    assert worker_thread and worker_thread[0] != caller_thread
    assert result == {"session_id": "s1"}


def test_session_list_legacy_query_cannot_block_serving_loop(monkeypatch) -> None:
    started = threading.Event()

    async def slow_legacy(**_kwargs):
        started.set()
        time.sleep(0.15)
        return {"sessions": []}

    monkeypatch.setattr(db_viewer, "_list_sessions_impl", slow_legacy)

    async def scenario():
        task = asyncio.create_task(db_viewer.list_sessions(
            request=None,
            user_id="user",
            db="user.db",
            agent_id=None,
            limit=20,
            include_hidden=False,
            q=None,
            include_recycled=False,
            include_manifest=False,
        ))
        for _ in range(20):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        await asyncio.sleep(0.01)
        assert not task.done()
        return await task

    assert asyncio.run(scenario()) == {"sessions": []}

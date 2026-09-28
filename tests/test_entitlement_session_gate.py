import asyncio

import pytest

from app.agent import session_gate


@pytest.fixture(autouse=True)
def reset_gate():
    session_gate._waiters.clear()
    session_gate._holders.clear()
    yield
    session_gate._waiters.clear()
    session_gate._holders.clear()


def test_same_user_sessions_respect_tier_cap(monkeypatch):
    monkeypatch.setattr(session_gate, "_cap", lambda: 10)

    async def tier_cap(_user_id):
        return 1

    monkeypatch.setattr(session_gate, "_tier_cap", tier_cap)

    async def scenario():
        await session_gate.acquire("s1", user_id="u1")
        waiting = asyncio.create_task(session_gate.acquire("s2", user_id="u1"))
        await asyncio.sleep(0)
        assert not waiting.done()
        await session_gate.release("s1")
        assert await asyncio.wait_for(waiting, 1)

    asyncio.run(scenario())


def test_different_users_can_use_separate_tier_slots(monkeypatch):
    monkeypatch.setattr(session_gate, "_cap", lambda: 10)

    async def tier_cap(_user_id):
        return 1

    monkeypatch.setattr(session_gate, "_tier_cap", tier_cap)

    async def scenario():
        await session_gate.acquire("s1", user_id="u1")
        await session_gate.acquire("s2", user_id="u2")
        assert session_gate.stats() == (2, 0)

    asyncio.run(scenario())


def test_effective_behavior_is_global_min_user(monkeypatch):
    monkeypatch.setattr(session_gate, "_cap", lambda: 1)

    async def tier_cap(_user_id):
        return 4

    monkeypatch.setattr(session_gate, "_tier_cap", tier_cap)

    async def scenario():
        await session_gate.acquire("s1", user_id="u1")
        waiting = asyncio.create_task(session_gate.acquire("s2", user_id="u2"))
        await asyncio.sleep(0)
        assert not waiting.done()
        await session_gate.release("s1")
        assert await asyncio.wait_for(waiting, 1)

    asyncio.run(scenario())


def test_force_run_cannot_bypass_user_tier_cap(monkeypatch):
    monkeypatch.setattr(session_gate, "_cap", lambda: 1)

    async def tier_cap(_user_id):
        return 1

    monkeypatch.setattr(session_gate, "_tier_cap", tier_cap)

    async def scenario():
        await session_gate.acquire("s1", user_id="u1")
        waiting = asyncio.create_task(session_gate.acquire("s2", user_id="u1"))
        await asyncio.sleep(0)
        assert await session_gate.force_acquire("s2", user_id="u1") is False
        assert not waiting.done()
        await session_gate.release("s1")
        assert await asyncio.wait_for(waiting, 1)

    asyncio.run(scenario())

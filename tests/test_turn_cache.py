import asyncio

from app.db.turn_cache import turn_cache_scope, turn_cached
from app.db.offload import db_offload


def test_turn_cache_coalesces_concurrent_identical_reads():
    class Backend:
        def __init__(self):
            self.calls = 0

        @turn_cached
        async def read(self, key):
            self.calls += 1
            await asyncio.sleep(0.01)
            return {"key": key}

    async def run():
        db = Backend()
        with turn_cache_scope():
            values = await asyncio.gather(*(db.read("same") for _ in range(12)))
        assert db.calls == 1
        assert values == [{"key": "same"}] * 12

    asyncio.run(run())


def test_turn_cache_does_not_memoize_failures():
    class Backend:
        def __init__(self):
            self.calls = 0

        @turn_cached
        async def read(self):
            self.calls += 1
            raise RuntimeError("temporary failure")

    async def run():
        db = Backend()
        with turn_cache_scope():
            for _ in range(2):
                try:
                    await db.read()
                except RuntimeError:
                    pass
        assert db.calls == 2

    asyncio.run(run())


def test_turn_cache_coalesces_reads_across_db_worker_loops():
    class Backend:
        def __init__(self):
            self.calls = 0

        @turn_cached
        async def read(self):
            self.calls += 1
            await asyncio.sleep(0.01)
            return "shared"

    async def run():
        db = Backend()
        with turn_cache_scope():
            values = await asyncio.gather(
                *(db_offload(lambda: db.read()) for _ in range(12))
            )
        assert db.calls == 1
        assert values == ["shared"] * 12

    asyncio.run(run())

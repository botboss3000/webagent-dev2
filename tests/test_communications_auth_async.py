import asyncio
import time
from types import SimpleNamespace


class _SlowQuery:
    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def execute(self):
        time.sleep(0.08)
        return SimpleNamespace(data=[])


class _SlowRaw:
    def table(self, _name):
        return _SlowQuery()


class _SlowDB:
    def get_raw_client(self):
        return _SlowRaw()


def test_identity_lookup_yields_while_sync_database_query_runs(monkeypatch):
    from app.communications import auth

    monkeypatch.setattr(auth, "get_app_db", lambda: _SlowDB())

    async def scenario():
        lookup = asyncio.create_task(auth.get_identity("web_public", "browser-1"))
        await asyncio.sleep(0.01)
        assert not lookup.done()
        assert await lookup is None

    asyncio.run(scenario())

from __future__ import annotations

import asyncio
import threading
import time

from app.auth import identity


def test_caller_identity_revocation_check_runs_off_event_loop(monkeypatch) -> None:
    started = threading.Event()
    worker_threads = []
    downstream = []

    def slow_decode(_token: str):
        worker_threads.append(threading.get_ident())
        started.set()
        time.sleep(0.15)
        return {"user_id": "verified-user", "sub": "verified-user"}

    async def app(scope, _receive, _send):
        downstream.append(scope["state"].get("auth_payload"))

    monkeypatch.setattr(identity, "decode_token", slow_decode)
    middleware = identity.CallerIdentityMiddleware(app)

    async def scenario():
        caller_thread = threading.get_ident()
        scope = {
            "type": "http",
            "headers": [(b"authorization", b"Bearer test-token")],
            "query_string": b"",
        }
        task = asyncio.create_task(middleware(scope, None, None))
        for _ in range(20):
            if started.is_set():
                break
            await asyncio.sleep(0.005)
        assert started.is_set()
        await asyncio.sleep(0.01)
        assert not task.done()
        await task
        return caller_thread

    caller_thread = asyncio.run(scenario())
    assert worker_threads[0] != caller_thread
    assert downstream == [{"user_id": "verified-user", "sub": "verified-user"}]
class _Request:
    def __init__(self, user_id: str = ""):
        self.state = type("State", (), {
            "auth_payload": {"user_id": user_id} if user_id else None,
        })()
        self.headers = {}
        self.query_params = {}


def test_require_platform_admin_rejects_missing_and_non_admin(monkeypatch) -> None:
    async def not_admin(_user_id: str) -> bool:
        return False

    monkeypatch.setattr(identity, "_is_admin", not_admin)

    async def scenario():
        import fastapi

        try:
            await identity.require_platform_admin(_Request())
            raise AssertionError("missing identity was accepted")
        except fastapi.HTTPException as exc:
            assert exc.status_code == 401

        try:
            await identity.require_platform_admin(_Request("ordinary-user"))
            raise AssertionError("ordinary user was accepted")
        except fastapi.HTTPException as exc:
            assert exc.status_code == 403

    asyncio.run(scenario())


def test_require_platform_admin_accepts_admin(monkeypatch) -> None:
    async def is_admin(user_id: str) -> bool:
        return user_id == "admin-user"

    monkeypatch.setattr(identity, "_is_admin", is_admin)
    assert asyncio.run(identity.require_platform_admin(_Request("admin-user"))) == "admin-user"


def test_restart_route_declares_platform_admin_dependency() -> None:
    from pathlib import Path

    source = Path("/app/app/main.py").read_text(encoding="utf-8")
    block = source[source.index('@restart_router.post("/restart")'):source.index("app.include_router(restart_router)")]
    assert "Depends(require_platform_admin)" in block

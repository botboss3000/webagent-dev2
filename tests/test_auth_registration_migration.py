import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from starlette.requests import Request

import app.auth as auth


def test_registration_migrates_anonymous_conversation(monkeypatch):
    user = SimpleNamespace(
        user_id="registered-user",
        username="person@example.test",
        display_name="Person",
    )
    monkeypatch.setattr(auth, "register_user", AsyncMock(return_value=user))
    monkeypatch.setattr(auth, "create_access_token", lambda *_args: "registered-token")
    monkeypatch.setattr(auth, "_record_auth_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("app.admin.settings.get_access_mode", lambda: "public_registered")
    monkeypatch.setattr("app.auth.identity.request_user_id", lambda _request: "anon_before_register")
    migrate = AsyncMock(return_value=3)
    monkeypatch.setattr("app.communications.auth.migrate_anonymous_to_user", migrate)

    class DB:
        async def upsert_user_profile(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(auth, "get_db", lambda: DB())
    monkeypatch.setattr("app.api.rate_limit.enforce_public_registration", AsyncMock())
    request = Request({"type": "http", "method": "POST", "path": "/register", "headers": []})

    response = asyncio.run(auth.register(
        auth.RegisterRequest(
            email="person@example.test",
            password="example-password",
            display_name="Person",
        ),
        request,
    ))

    assert response.user_id == "registered-user"
    assert response.access_token == "registered-token"
    migrate.assert_awaited_once_with("anon_before_register", "registered-user")

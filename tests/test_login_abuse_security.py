from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.api import rate_limit
from app.auth import LoginRequest, RegisterRequest


class _Client:
    host = "203.0.113.8"


class _Request:
    client = _Client()
    headers = {}


def test_login_attempt_uses_opaque_source_and_account_buckets(monkeypatch) -> None:
    hits = []
    monkeypatch.setattr(rate_limit, "client_source_hash", lambda _request: "source-digest")
    monkeypatch.setattr(rate_limit, "_persistent_bucket_status", lambda _key, _window: {"count": 0, "resets_in": 0})
    monkeypatch.setattr(rate_limit, "_persistent_hit", lambda key, limit, window: hits.append((key, limit, window)) or True)

    key = asyncio.run(rate_limit.begin_login_attempt(_Request(), "Person@Example.test"))

    assert "Person@Example.test" not in key
    assert key.startswith("login-failure:source-digest:")
    assert hits == [("login-source:source-digest", 20, 300), ("login-global", 100, 300)]


def test_login_failure_bucket_stops_bcrypt_work(monkeypatch) -> None:
    monkeypatch.setattr(rate_limit, "client_source_hash", lambda _request: "source-digest")
    monkeypatch.setattr(rate_limit, "_persistent_bucket_status", lambda _key, _window: {"count": 5, "resets_in": 91})

    with pytest.raises(HTTPException) as captured:
        asyncio.run(rate_limit.begin_login_attempt(_Request(), "person@example.test"))

    assert captured.value.status_code == 429
    assert captured.value.headers["Retry-After"] == "91"


@pytest.mark.parametrize("model", [LoginRequest, RegisterRequest])
def test_password_is_bounded_before_bcrypt(model) -> None:
    with pytest.raises(ValidationError):
        model(email="person@example.test", password="x" * 73)


def test_login_identifiers_are_bounded() -> None:
    with pytest.raises(ValidationError):
        LoginRequest(email="x" * 321, password="valid")
    with pytest.raises(ValidationError):
        RegisterRequest(email="a@example.test", password="valid", display_name="x" * 129)


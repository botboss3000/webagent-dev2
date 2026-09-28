import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import uploads


class FakeDB:
    async def assert_session_owned(self, _user_id, _session_id):
        return None


def _request():
    return Request({"type": "http", "method": "POST", "path": "/upload", "headers": []})


def test_tier_attachment_limit_is_enforced(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(uploads, "get_db", lambda: db)
    monkeypatch.setattr("app.auth.identity.assert_caller_is", AsyncMock(return_value="user"))

    async def capabilities(_user_id, **_kwargs):
        return {
            "subject": {"class": "registered"}, "features": {"attachments": True},
            "limits": {"max_attachment_bytes": 10, "max_storage_bytes": None},
        }

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    with pytest.raises(HTTPException) as error:
        asyncio.run(uploads._upload_context(_request(), "user", "session", 11))
    assert error.value.status_code == 413
    assert error.value.detail["code"] == "quota_exceeded"


def test_attachment_feature_denial_happens_before_storage(monkeypatch):
    db = FakeDB()
    monkeypatch.setattr(uploads, "get_db", lambda: db)
    monkeypatch.setattr("app.auth.identity.assert_caller_is", AsyncMock(return_value="anon_guest"))
    async def capabilities(_user_id, **_kwargs):
        return {"subject": {"class": "anonymous"}, "features": {}, "limits": {}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", capabilities)
    with pytest.raises(HTTPException) as error:
        asyncio.run(uploads._upload_context(_request(), "anon_guest", "session", 1))
    assert error.value.detail["code"] == "authentication_required"


class FakeUpload:
    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.read_sizes = []

    async def read(self, size):
        self.read_sizes.append(size)
        return self.chunks.pop(0) if self.chunks else b""


def test_upload_reader_stops_as_soon_as_limit_is_exceeded():
    upload = FakeUpload([b"12345678", b"901", b"must-not-be-read"])
    with pytest.raises(HTTPException) as error:
        asyncio.run(uploads._read_upload_bytes(upload, 10))
    assert error.value.status_code == 413
    assert upload.read_sizes == [1024 * 1024, 1024 * 1024]

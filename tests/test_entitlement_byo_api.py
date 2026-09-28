import asyncio
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.api import entitlements


def _request():
    return Request({"type": "http", "method": "PUT", "path": "/", "headers": []})


def test_free_user_cannot_save_more_than_one_byo_model(monkeypatch):
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "user")
    monkeypatch.setattr("app.db.get_db", lambda: object())
    monkeypatch.setattr(entitlements, "resolve_capabilities", AsyncMock(return_value={
        "models": {"allow_byo": True, "max_byo_entries": 1},
        "features": {"llm_byo": True},
    }))
    body = entitlements.ByoModelsRequest(entries=[
        {"provider": "openai", "model": "one", "api_key": "k1"},
        {"provider": "openai", "model": "two", "api_key": "k2"},
    ])

    with pytest.raises(HTTPException) as error:
        asyncio.run(entitlements.set_my_byo_models(body, _request()))

    assert error.value.status_code == 409
    assert error.value.detail["code"] == "quota_exceeded"


def test_byo_read_masks_credentials(monkeypatch):
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "user")
    config = {"multi_providers": [{
        "entry_id": "one", "provider": "openai", "model": "gpt", "api_key": "secret",
    }]}
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", AsyncMock(return_value=config))

    result = asyncio.run(entitlements.get_my_byo_models(_request()))

    assert result["entries"][0]["api_key"] == ""
    assert result["entries"][0]["credential_configured"] is True
    assert result["default_entry_id"] == "one"
    assert "secret" not in str(result)


def test_new_byo_entry_keeps_credential_for_vault_persistence(monkeypatch):
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "user")
    monkeypatch.setattr("app.db.get_db", lambda: object())
    monkeypatch.setattr(entitlements, "resolve_capabilities", AsyncMock(return_value={
        "models": {"allow_byo": True, "max_byo_entries": 1},
        "features": {"llm_byo": True},
    }))
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", AsyncMock(return_value=None))
    persist = AsyncMock()
    monkeypatch.setattr("app.admin.settings._persist_llm_config", persist)
    body = entitlements.ByoModelsRequest(
        entries=[{"provider": "openai", "model": "gpt", "display_name": "My GPT", "api_key": "new-secret"}],
        default_entry_id="",
    )

    result = asyncio.run(entitlements.set_my_byo_models(body, _request()))

    assert result["default_entry_id"]
    assert result["entries"][0]["entry_id"] == result["default_entry_id"]
    saved = persist.await_args.args[1]
    assert saved["multi_providers"][0]["api_key"] == "new-secret"
    assert saved["multi_providers"][0]["display_name"] == "My GPT"
    assert result["entries"][0]["display_name"] == "My GPT"
    assert saved["multi_providers"][0]["enabled"] is True
    assert "provider" not in saved
    assert "model" not in saved
    assert "_user_default_entry_id" not in saved


def test_byo_rejects_unknown_default_entry(monkeypatch):
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "user")
    monkeypatch.setattr("app.db.get_db", lambda: object())
    monkeypatch.setattr(entitlements, "resolve_capabilities", AsyncMock(return_value={
        "models": {"allow_byo": True, "max_byo_entries": 1}, "features": {},
    }))
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", AsyncMock(return_value=None))
    monkeypatch.setattr("app.admin.settings._persist_llm_config", AsyncMock())
    body = entitlements.ByoModelsRequest(
        entries=[{"provider": "openai", "model": "gpt", "api_key": "k"}],
        default_entry_id="missing",
    )

    with pytest.raises(HTTPException) as error:
        asyncio.run(entitlements.set_my_byo_models(body, _request()))

    assert error.value.detail["code"] == "invalid_default_entry"

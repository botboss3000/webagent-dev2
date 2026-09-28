"""Tests for app-wide voice and chat-panel settings."""

import asyncio
from unittest.mock import patch

import pytest

from app.admin import settings
from app.auth import UiConfigResponse


def test_voice_dictation_policy_defaults_and_validation() -> None:
    defaults = settings.AppSettings()
    assert defaults.voice_dictation_llm_enabled is True
    assert defaults.voice_dictation_mode == "browser_then_llm"

    with patch.object(
        settings,
        "_load_app_settings",
        lambda: {
            "voice_dictation_llm_enabled": False,
            "voice_dictation_mode": "invalid-mode",
        },
    ):
        assert settings.get_voice_dictation_config() == {
            "llm_enabled": False,
            "mode": "browser_then_llm",
        }

    assert "voice_dictation_llm_enabled" in settings.AppSettings.model_fields
    assert "voice_dictation_mode" in settings.AppSettings.model_fields


def test_chat_panel_width_defaults_validation_and_legacy_fallback(monkeypatch) -> None:
    assert settings.AppSettings().chat_panel_width == "380px"
    assert "chat_panel_width" in UiConfigResponse.model_fields
    assert settings.normalize_chat_panel_width("420px") == "420px"
    assert settings.normalize_chat_panel_width("35%") == "35%"

    for invalid in ("", "0px", "-1px", "12rem", "calc(100%)", "35% ; color:red"):
        with pytest.raises(ValueError):
            settings.normalize_chat_panel_width(invalid)

    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"chat_panel_width": "calc(100%)"})
    assert settings.get_appearance_config()["chat_panel_width"] == "380px"


def test_set_app_settings_returns_the_persisted_chat_width(monkeypatch) -> None:
    """The save acknowledgement must represent storage, not the request body."""
    import app.db as db_module

    stored = {"chat_panel_width": "380px"}

    class FakeDb:
        async def is_user_admin(self, _caller_id):
            return True

    class FakeRequest:
        headers = {}
        query_params = {}

        async def json(self):
            return {"chat_panel_width": "33%"}

    def load_settings():
        return dict(stored)

    def save_settings(data):
        stored.update(data)
        # Simulate the exact file value read after a write. A mismatch here must
        # reach the caller so the browser never marks the requested value saved.
        stored["chat_panel_width"] = "35%"

    monkeypatch.setattr(db_module, "get_db", lambda: FakeDb())
    monkeypatch.setattr(settings, "_load_app_settings", load_settings)
    monkeypatch.setattr(settings, "_save_app_settings", save_settings)

    result = asyncio.run(settings.set_app_settings(FakeRequest()))

    assert result.chat_panel_width == "35%"
    assert stored["chat_panel_width"] == "35%"


def test_chat_panel_width_save_surfaces_auth_and_backend_failure_detail() -> None:
    """The width control must not reduce an auth/API failure to 'Not saved'."""
    source = (
        __import__("pathlib").Path("ui/main-panel/app/settings/app-settings/app-settings.js")
        .read_text(encoding="utf-8")
    )
    width_handler = source[source.index("function _wireChatPanelWidth()"):]

    assert "if (!isAdmin())" in width_handler
    assert "showRestrictedModal();" in width_handler
    assert "payload?.detail" in source

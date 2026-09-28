import json

from app.admin import settings


def test_browser_session_policy_defaults(monkeypatch, tmp_path):
    path = tmp_path / "app-settings.json"
    monkeypatch.setattr(settings, "APP_SETTINGS_FILE", path)

    assert settings.get_browser_session_policy() == {
        "max_concurrent_sessions": 3,
        "idle_timeout_seconds": 300,
        "idle_cleanup_enabled": True,
    }


def test_browser_session_policy_clamps_invalid_values(monkeypatch, tmp_path):
    path = tmp_path / "app-settings.json"
    path.write_text(json.dumps({
        "browser_max_concurrent_sessions": 999,
        "browser_idle_timeout_seconds": 1,
        "browser_idle_cleanup_enabled": False,
    }))
    monkeypatch.setattr(settings, "APP_SETTINGS_FILE", path)

    assert settings.get_browser_session_policy() == {
        "max_concurrent_sessions": 20,
        "idle_timeout_seconds": 60,
        "idle_cleanup_enabled": False,
    }


def test_app_settings_model_exposes_requested_browser_defaults():
    model = settings.AppSettings()

    assert model.browser_max_concurrent_sessions == 3
    assert model.browser_idle_timeout_seconds == 300
    assert model.browser_idle_cleanup_enabled is True

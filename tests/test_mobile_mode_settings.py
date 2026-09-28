from app.admin import settings


def test_mobile_mode_defaults_on(monkeypatch):
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {})
    assert settings.get_mobile_mode() is True
    assert settings.AppSettings().mobile_mode is True


def test_mobile_mode_can_be_explicitly_disabled(monkeypatch):
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"mobile_mode": False})
    assert settings.get_mobile_mode() is False

    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"mobile_mode": True})
    assert settings.get_mobile_mode() is True

    monkeypatch.setattr(settings, "_load_app_settings", lambda: {"mobile_mode": "true"})
    assert settings.get_mobile_mode() is False


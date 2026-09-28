from app.admin import settings


def test_anon_limit_reads_persisted_setting_without_env_prefix(monkeypatch):
    monkeypatch.delenv("WEBAGENT_ANON_IDENTITY_MAX", raising=False)
    monkeypatch.setattr(
        settings,
        "_load_app_settings",
        lambda: {"anon_identity_max": 7},
    )

    assert settings.get_anon_identity_max() == 7


def test_anon_limit_environment_override_wins(monkeypatch):
    monkeypatch.setenv("WEBAGENT_ANON_IDENTITY_MAX", "9")
    monkeypatch.setattr(
        settings,
        "_load_app_settings",
        lambda: {"anon_identity_max": 7},
    )

    assert settings.get_anon_identity_max() == 9


def test_anon_identity_default_allows_several_browsers_per_network(monkeypatch):
    monkeypatch.delenv("WEBAGENT_ANON_IDENTITY_MAX", raising=False)
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {})

    assert settings.AppSettings().anon_identity_max == 5
    assert settings.get_anon_identity_max() == 5


def test_distributed_anonymous_breaker_defaults(monkeypatch):
    for name in (
        "WEBAGENT_ANON_GLOBAL_SESSION_MAX",
        "WEBAGENT_ANON_GLOBAL_CHAT_MAX",
        "WEBAGENT_ANON_DAILY_CHAT_MAX",
        "WEBAGENT_PUBLIC_REGISTRATION_IP_MAX",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {})

    defaults = settings.AppSettings()
    assert defaults.anon_global_session_max == 25
    assert settings.get_anon_global_session_max() == 25
    assert defaults.anon_global_chat_max == 50
    assert settings.get_anon_global_chat_max() == 50
    assert defaults.anon_daily_chat_max == 10
    assert settings.get_anon_daily_chat_max() == 10
    assert defaults.public_registration_ip_max == 5
    assert settings.get_public_registration_ip_max() == 5
    assert defaults.public_registration_global_max == 100
    assert settings.get_public_registration_global_max() == 100


def test_anonymous_kill_switch_and_hard_budget_defaults(monkeypatch):
    for name in (
        "WEBAGENT_ANONYMOUS_CHAT_ENABLED",
        "WEBAGENT_ANON_BUDGET_MAX",
        "WEBAGENT_ANON_BUDGET_WINDOW",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {})

    defaults = settings.AppSettings()
    assert defaults.anonymous_chat_enabled is True
    assert settings.get_anonymous_chat_enabled() is True
    assert defaults.anon_budget_max == 100
    assert settings.get_anon_budget_max() == 100
    assert defaults.anon_budget_window == 86400
    assert settings.get_anon_budget_window() == 86400


def test_native_anonymous_control_defaults(monkeypatch):
    monkeypatch.setattr(settings, "_load_app_settings", lambda: {})
    for name in (
        "WEBAGENT_ANON_TOKEN_USER_MAX",
        "WEBAGENT_ANON_TOKEN_SOURCE_MAX",
        "WEBAGENT_ANON_TOKEN_GLOBAL_MAX",
        "WEBAGENT_ANON_MAX_CONCURRENT_RUNS",
        "WEBAGENT_ANON_AUTO_CLOSE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)

    controls = settings.get_anon_native_controls()
    assert controls["token_user_max"] == 100000
    assert controls["token_source_max"] == 100000
    assert controls["token_global_max"] == 1000000
    assert controls["cost_user_microusd_max"] == 250000
    assert controls["cost_source_microusd_max"] == 250000
    assert controls["cost_global_microusd_max"] == 2500000
    assert controls["spend_window"] == 86400
    assert controls["max_concurrent_runs"] == 2
    assert controls["auto_close_enabled"] is True
    assert controls["error_max"] == 10
    assert controls["error_window"] == 300

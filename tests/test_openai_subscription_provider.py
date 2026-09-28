import asyncio
from pathlib import Path

from app.admin import settings
from app.agent.loop import _provider_engine_id
from app.api import codex_auth
from app.entitlements.rosters import (
    RosterError,
    config_to_roster,
    roster_to_config,
    validate_roster_entries,
)
from plugins.engines.codex.app_server import CodexAppServer
from plugins.engines.codex.codex import _resolved_cfg


def test_app_server_uses_managed_chatgpt_auth_and_dynamic_model_catalog(monkeypatch):
    server = CodexAppServer()
    calls = []

    async def request(method, params=None, **_kwargs):
        calls.append((method, params))
        if method == "account/login/start":
            return {
                "type": "chatgptDeviceCode",
                "verificationUrl": "https://auth.example/device",
                "userCode": "ABCD-EFGH9",
                "loginId": "login-1",
            }
        if method == "model/list":
            return {"data": [{"id": "gpt-test"}]}
        return {}

    monkeypatch.setattr(server, "request", request)

    async def run():
        await server.account(refresh=True)
        login = await server.start_chatgpt_login()
        models = await server.models()
        await server.rate_limits()
        await server.logout()
        return login, models

    login, models = asyncio.run(run())
    assert login["verificationUrl"] == "https://auth.example/device"
    assert login["userCode"] == "ABCD-EFGH9"
    assert models == [{"id": "gpt-test"}]
    assert calls == [
        ("account/read", {"refreshToken": True}),
        ("account/login/start", {"type": "chatgptDeviceCode"}),
        ("model/list", {"limit": 200, "includeHidden": False}),
        ("account/rateLimits/read", {}),
        ("account/logout", {}),
    ]


def test_subscription_auth_routes_return_browser_url_and_account_models(monkeypatch):
    async def require_admin(_request):
        return None

    async def account(*, refresh=False):
        assert refresh is False
        return {"account": {"type": "chatgpt", "planType": "plus"}}

    async def start_login():
        return {
            "verificationUrl": "https://auth.example/device",
            "userCode": "ABCD-EFGH9",
            "loginId": "login-1",
        }

    async def models():
        return [{
            "id": "gpt-test", "displayName": "GPT Test",
            "inputModalities": ["text", "image"],
            "supportedReasoningEfforts": ["low", "high"],
            "defaultReasoningEffort": "low", "isDefault": True,
        }]

    async def rate_limits():
        return {"primary": {"usedPercent": 12}}

    monkeypatch.setattr(codex_auth, "_require_admin", require_admin)
    monkeypatch.setattr("plugins.engines.codex.app_server.app_server.account", account)
    monkeypatch.setattr("plugins.engines.codex.app_server.app_server.start_chatgpt_login", start_login)
    monkeypatch.setattr("plugins.engines.codex.app_server.app_server.models", models)
    monkeypatch.setattr("plugins.engines.codex.app_server.app_server.rate_limits", rate_limits)

    async def run():
        return (
            await codex_auth.auth_status(object()),
            await codex_auth.login_start(object()),
            await codex_auth.subscription_models(object()),
        )

    status, login, catalog = asyncio.run(run())
    assert status["signed_in"] is True
    assert status["chatgpt_authorized"] is True
    assert status["plan_type"] == "plus"
    assert status["rate_limits"]["primary"]["usedPercent"] == 12
    assert login["verification_url"] == "https://auth.example/device"
    assert login["user_code"] == "ABCD-EFGH9"
    assert "auth_url" not in login
    assert catalog["plan_type"] == "plus"
    assert catalog["models"] == [{
        "id": "gpt-test", "name": "GPT Test", "description": "",
        "text_capable": True, "image_capable": True,
        "image_out_capable": False, "voice_capable": False,
        "reasoning_efforts": ["low", "high"],
        "default_reasoning_effort": "low", "is_default": True,
    }]


def test_subscription_ui_uses_device_code_instead_of_loopback_oauth():
    model_table = Path("ui/shared/js/model-table.js").read_text(encoding="utf-8")
    codex_card = Path("ui/main-panel/agents/js/codex-agent.js").read_text(encoding="utf-8")

    for source in (model_table, codex_card):
        assert "verification_url" in source
        assert "user_code" in source
        assert "d.auth_url" not in source
        assert "localhost:1455" not in source


def test_subscription_survives_platform_roster_and_agent_inheritance():
    row = {
        "provider": "openai_subscription",
        "auth_mode": "chatgpt",
        "base_url": "codex://chatgpt",
        "api_key": "",
        "model": "gpt-test",
        "enabled": True,
        "text_capable": True,
    }
    config = {**row, "multi_providers": [row]}
    entries, default_id, secret = config_to_roster(config, "roster-admin")
    validate_roster_entries(entries, require_provider_model=True)
    restored = roster_to_config({
        "id": "roster-admin", "entries_json": entries,
        "default_entry_id": default_id,
    }, secret)

    assert secret == ""
    assert restored["provider"] == "openai_subscription"
    assert restored["auth_mode"] == "chatgpt"
    assert restored["base_url"] == "codex://chatgpt"
    assert restored["multi_providers"][0]["auth_mode"] == "chatgpt"

    inherited = settings._merge_agent_override(restored, {
        "use_default": False,
        "provider": "openai_subscription",
        "auth_mode": "chatgpt",
        "base_url": "codex://chatgpt",
        "model": "gpt-test",
        "multi_providers": [row],
    })
    assert inherited["auth_mode"] == "chatgpt"
    assert inherited["api_key"] == ""


def test_user_byo_may_reference_subscription_transport_without_claiming_platform_scope():
    row = {
        "entry_id": "subscription", "provider": "openai_subscription",
        "auth_mode": "chatgpt", "base_url": "codex://chatgpt", "model": "gpt-test",
        "credential_scope": "user",
    }
    validated = validate_roster_entries([row], untrusted_urls=True)
    assert validated[0]["credential_scope"] == "user"


def test_subscription_stays_in_webagent_loop_without_api_credentials():
    provider = {
        "provider": "openai_subscription",
        "auth_mode": "chatgpt",
        "model": "gpt-test",
        "reasoning_effort": "high",
        "api_key": "",
    }
    cfg = _resolved_cfg({"metadata": {}}, provider)

    assert _provider_engine_id(provider) == ""
    assert _provider_engine_id({"provider": "openai"}) == ""
    assert cfg["model"] == "gpt-test"
    assert cfg["effort"] == "high"
    assert cfg["context_mode"] == "webagent_wrapper"
    assert cfg["append_persona"] is True
    assert cfg["mcp_enabled"] is True


def test_selecting_subscription_never_carries_an_api_key(monkeypatch):
    persisted = {}

    async def require_admin(_request):
        return "admin"

    async def load_existing(_user_id):
        return {
            "provider": "openai", "auth_mode": "api_key",
            "base_url": "https://api.openai.com/v1", "api_key": "secret-api-key",
            "model": "gpt-api", "providers": {}, "multi_providers": [],
        }

    async def persist(_user_id, config):
        persisted.update(config)

    monkeypatch.setattr(settings, "_require_provider_admin", require_admin)
    monkeypatch.setattr(settings, "_load_own_llm_config", load_existing)
    monkeypatch.setattr(settings, "_persist_llm_config", persist)

    body = settings.ProviderConfig(
        provider="openai_subscription", auth_mode="chatgpt",
        base_url="codex://chatgpt", api_key="", model="gpt-test",
    )
    asyncio.run(settings.set_provider(object(), body))

    assert "provider" not in persisted
    assert "model" not in persisted
    assert "api_key" not in persisted
    assert persisted["providers"]["openai_subscription"]["api_key"] == ""
    assert "model" not in persisted["providers"]["openai_subscription"]

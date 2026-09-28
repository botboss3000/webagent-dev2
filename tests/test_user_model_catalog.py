import asyncio

from app.api import entitlements


def test_catalog_entry_is_role_and_credential_free():
    entry = entitlements._user_catalog_entry({
        "entry_id": "byo-id",
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "https://api.example.test/v1",
        "api_key": "super-secret",
        "credential_suffix": "cret",
        "credential_configured": True,
        "credential_scope": "user",
        "enabled": True,
        "high_effort_capable": True,
        "use_for_image": True,
        "use_for_image_out": True,
        "use_for_system": True,
        "use_for_voice": True,
        "text_capable": True,
        "image_capable": True,
    }, "byo")

    assert entry == {
        "entry_id": "byo-id",
        "provider": "openai",
        "model": "gpt-test",
        "base_url": "https://api.example.test/v1",
        "text_capable": True,
        "image_capable": True,
        "catalog_origin": "byo",
    }


def test_user_catalog_merges_only_callers_entitlement_and_byo(monkeypatch):
    async def own_config(user_id):
        assert user_id == "user-a"
        return {"user_catalog_platform_entry_ids": ["platform-a"], "multi_providers": [{
            "entry_id": "byo-a", "provider": "openai", "model": "my-byo-model",
            "api_key": "user-a-secret", "enabled": True,
        }]}

    async def entitled_config(user_id, *, db):
        assert user_id == "user-a"
        assert db is sentinel_db
        return {"multi_providers": [{
            "entry_id": "platform-a", "provider": "openrouter", "model": "credit-model",
            "api_key": "platform-secret", "high_effort_capable": True,
            "credit_input_per_million": 0.12,
        }]}

    class SentinelDb:
        async def list_agents_for_user(self, user_id, **_kwargs):
            assert user_id == "user-a"
            return []

    sentinel_db = SentinelDb()
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "user-a")
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", own_config)
    monkeypatch.setattr("app.entitlements.rosters.resolve_platform_roster_config", entitled_config)
    monkeypatch.setattr("app.db.get_db", lambda: sentinel_db)

    payload = asyncio.run(entitlements.get_my_model_catalog(object()))

    assert [(row["entry_id"], row["catalog_origin"]) for row in payload["entries"]] == [
        ("platform-a", "platform"), ("byo-a", "byo"),
    ]
    assert payload["entries"][0]["credit_input_per_million"] == 0.12
    forbidden = {
        "api_key", "credential_suffix", "credential_configured", "credential_scope",
        "enabled", "high_effort_capable", "use_for_image", "use_for_image_out",
        "use_for_system", "use_for_voice",
    }
    assert not forbidden.intersection(payload["entries"][0])
    assert not forbidden.intersection(payload["entries"][1])


def test_platform_catalog_projects_runtime_equivalent_credit_defaults(monkeypatch):
    def cost_for(_model, input_tokens, output_tokens, _provider):
        # Published costs are USD; the catalog/runtimes express house credits
        # as USD * 1000 per million tokens.
        return ((0.001 if input_tokens else 0.002), "published")

    monkeypatch.setattr("app.model_catalog.cost_for", cost_for)
    flash_reference = {
        "entry_id": "flash", "provider": "deepseek", "model": "deepseek-flash",
        "display_name": "DeepSeek Flash",
    }
    projected = entitlements._user_catalog_entry({
        "entry_id": "catalog-row", "provider": "example", "model": "example-model",
    }, "platform", platform_entries=[flash_reference])

    assert projected["credit_schedule"] == "flash"
    assert projected["credit_input_per_million"] == 1.0
    assert projected["credit_output_per_million"] == 2.0


def test_catalog_backfills_hydrated_admin_agent_roster(monkeypatch):
    async def own_config(_user_id):
        return {"multi_providers": []}

    async def entitled_config(_user_id, *, db):
        return {"multi_providers": []}

    class SentinelDb:
        async def list_agents_for_user(self, user_id, **kwargs):
            assert user_id == "admin-user"
            assert kwargs["include_admin"] is True
            assert kwargs["include_templates"] is False
            return [{"id": "admin-agent", "metadata": {"llm_config": {"multi_providers": []}}}]

    async def hydrated(agent_id, config):
        assert agent_id == "admin-agent"
        assert config == {"multi_providers": []}
        return {"multi_providers": [{
            "entry_id": "agent-byo", "provider": "anthropic", "model": "claude-private",
            "api_key": "agent-secret", "enabled": True,
        }]}

    sentinel_db = SentinelDb()
    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "admin-user")
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", own_config)
    monkeypatch.setattr("app.admin.settings._load_agent_llm_config", hydrated)
    monkeypatch.setattr("app.entitlements.rosters.resolve_platform_roster_config", entitled_config)
    monkeypatch.setattr("app.db.get_db", lambda: sentinel_db)

    payload = asyncio.run(entitlements.get_my_model_catalog(object()))

    assert payload["entries"] == [{
        "entry_id": "agent-byo", "provider": "anthropic", "model": "claude-private",
        "catalog_origin": "byo", "catalog_entry_source": "agent",
    }]
    assert "api_key" not in payload["entries"][0]


def test_platform_entry_separates_model_name_from_the_users_own_display_name():
    """MODEL-NAME-LEVELS: a catalog row carries the catalog's own model name and,
    layered over it, the caller's own display name."""
    row = {
        "entry_id": "flash", "provider": "deepseek", "model": "deepseek-flash",
        "display_name": "DeepSeek Flash", "high_effort_capable": False,
    }

    entry = entitlements._user_catalog_entry(row, "platform", platform_entries=[row])
    assert entry["catalog_name"] == "DeepSeek Flash"
    assert entry["display_name"] == "DeepSeek Flash"

    renamed = entitlements._user_catalog_entry(
        row, "platform", platform_entries=[row], alias="Support Bot",
    )
    assert renamed["catalog_name"] == "DeepSeek Flash"   # level 1 stays the catalog's
    assert renamed["display_name"] == "Support Bot"      # level 2 is the caller's

    blank = entitlements._user_catalog_entry(
        row, "platform", platform_entries=[row], alias="   ",
    )
    assert blank["display_name"] == "DeepSeek Flash"     # a blank alias is no alias


def test_model_catalog_request_carries_platform_aliases():
    body = entitlements.UserModelCatalogRequest(
        platform_entry_ids=["flash"], platform_aliases={"flash": "Support Bot"},
        default_entry_id="flash",
    )
    assert body.platform_aliases == {"flash": "Support Bot"}
    assert body.default_entry_id == "flash"
    assert entitlements.UserModelCatalogRequest().platform_aliases == {}


def test_catalog_returns_persisted_platform_default(monkeypatch):
    async def own_config(_user_id):
        return {
            "user_catalog_platform_entry_ids": ["platform-a"],
            "user_catalog_default_entry_id": "platform-a",
        }

    async def entitled_config(_user_id, *, db):
        return {"multi_providers": [{
            "entry_id": "platform-a", "provider": "deepseek", "model": "flash",
        }]}

    class Db:
        async def list_agents_for_user(self, *_args, **_kwargs):
            return []

    monkeypatch.setattr(entitlements, "request_user_id", lambda _request: "user-a")
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", own_config)
    monkeypatch.setattr("app.entitlements.rosters.resolve_platform_roster_config", entitled_config)
    monkeypatch.setattr("app.db.get_db", lambda: Db())

    payload = asyncio.run(entitlements.get_my_model_catalog(object()))
    assert payload["default_entry_id"] == "platform-a"


def test_agent_platform_row_keeps_the_callers_own_display_name():
    """MODEL-NAME-LEVELS: provider/model/credentials always come from the entitled
    platform row, while the caller's own display name — or their account alias —
    survives instead of being overwritten by the catalog name."""
    from app.api.agents import catalog_account_aliases, merge_entitled_catalog_row

    platform_row = {
        "entry_id": "platform-a", "provider": "deepseek",
        "model": "deepseek-v4", "display_name": "Catalog Name",
        "base_url": "", "api_key": "",
    }
    by_id = {"platform-a": platform_row}

    merged = merge_entitled_catalog_row(
        {"entry_id": "platform-a", "display_name": "Support Bot",
         "provider": "spoofed", "model": "spoofed"},
        by_id,
    )
    assert merged["display_name"] == "Support Bot"   # the caller's own name wins
    assert merged["provider"] == "deepseek"          # never the client's
    assert merged["model"] == "deepseek-v4"

    # No name of their own: the account-level alias applies.
    assert merge_entitled_catalog_row(
        {"entry_id": "platform-a"}, by_id, {"platform-a": "Acct"},
    )["display_name"] == "Acct"

    # Neither: the catalog's own name survives.
    assert merge_entitled_catalog_row(
        {"entry_id": "platform-a", "display_name": "  "}, by_id,
    )["display_name"] == "Catalog Name"

    # An entry the caller is not entitled to is left for the validator to reject.
    assert merge_entitled_catalog_row({"entry_id": "nope"}, by_id) == {"entry_id": "nope"}

    # Blank account aliases are not names.
    assert catalog_account_aliases(
        {"user_catalog_platform_aliases": {"a": " Name ", "b": "  ", "": "x"}}
    ) == {"a": "Name"}


def test_catalog_platform_aliases_persist_and_reject_unknown_entries(monkeypatch):
    """MODEL-NAME-LEVELS: the caller's own display name for an adopted platform
    entry persists with their catalog, a blank clears it, and an alias for an
    entry they are not entitled to is rejected."""
    saved = {}

    async def registered(_request):
        return "user-a"

    async def platform_config(_uid, db=None):
        return {"multi_providers": [{
            "entry_id": "platform-a", "provider": "deepseek",
            "model": "deepseek-flash", "display_name": "DeepSeek Flash",
        }]}

    async def noop_byo(_body, _request):
        return None

    async def own_config(_uid):
        return {"user_catalog_platform_entry_ids": ["platform-a"]}

    async def echo_catalog(_request):
        return {"entries": []}

    async def persist(_uid, config):
        saved.clear()
        saved.update(config)

    monkeypatch.setattr(entitlements, "_registered_caller", registered)
    monkeypatch.setattr(entitlements, "set_my_byo_models", noop_byo)
    monkeypatch.setattr(entitlements, "get_my_model_catalog", echo_catalog)
    monkeypatch.setattr("app.entitlements.rosters.resolve_platform_roster_config", platform_config)
    monkeypatch.setattr("app.admin.settings._load_own_llm_config", own_config)
    monkeypatch.setattr("app.admin.settings._persist_llm_config", persist)
    monkeypatch.setattr("app.db.get_db", lambda: object())

    adopt = entitlements.UserModelCatalogRequest(
        platform_entry_ids=["platform-a"], platform_aliases={"platform-a": "Support Bot"},
    )
    asyncio.run(entitlements.set_my_model_catalog(adopt, object()))
    assert saved["user_catalog_platform_entry_ids"] == ["platform-a"]
    assert saved["user_catalog_platform_aliases"] == {"platform-a": "Support Bot"}

    default_platform = entitlements.UserModelCatalogRequest(
        platform_entry_ids=["platform-a"], default_entry_id="platform-a",
    )
    asyncio.run(entitlements.set_my_model_catalog(default_platform, object()))
    assert saved["user_catalog_default_entry_id"] == "platform-a"

    clear = entitlements.UserModelCatalogRequest(
        platform_entry_ids=["platform-a"], platform_aliases={"platform-a": "   "},
    )
    asyncio.run(entitlements.set_my_model_catalog(clear, object()))
    assert saved["user_catalog_platform_aliases"] == {}

    unknown = entitlements.UserModelCatalogRequest(
        platform_entry_ids=["platform-a"], platform_aliases={"not-entitled": "x"},
    )
    try:
        asyncio.run(entitlements.set_my_model_catalog(unknown, object()))
        raise AssertionError("expected an unknown_catalog_entry rejection")
    except entitlements.HTTPException as exc:
        assert exc.status_code == 422

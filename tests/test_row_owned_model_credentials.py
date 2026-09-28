from pathlib import Path
import asyncio
import re

import pytest

from app.admin.settings import _assign_slots, _runtime_config_from_roster
from app.entitlements.rosters import (
    RosterError, assign_entry_ids, validate_live_voice_assignments,
)


UUID4 = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)


def test_backend_assigns_distinct_random_ids_to_identical_new_rows():
    rows = [
        {"provider": "openai", "base_url": "https://api.openai.com/v1",
         "model": "gpt-5.6", "api_key": "same"},
        {"provider": "openai", "base_url": "https://api.openai.com/v1",
         "model": "gpt-5.6", "api_key": "same"},
    ]

    saved = assign_entry_ids(rows, existing_entries=[])

    assert saved[0]["entry_id"] != saved[1]["entry_id"]
    assert all(UUID4.fullmatch(row["entry_id"]) for row in saved)


def test_backend_preserves_owned_and_server_catalog_ids_but_rejects_unknown_or_duplicate_ids():
    owned = {"entry_id": "8a3366fa-f05d-4b01-ad4c-9f09627578e5",
             "provider": "openai", "model": "gpt"}
    platform_catalog = {"entry_id": "platform-catalog-terra",
                        "provider": "openai_subscription", "model": "gpt-5.6-terra"}
    # Agent adoption carries the immutable ID of the server-resolved inherited
    # catalog row; it must not be reissued as a new client-owned identity.
    saved = assign_entry_ids(
        [dict(owned), dict(platform_catalog)],
        existing_entries=[owned, platform_catalog],
    )
    assert [row["entry_id"] for row in saved] == [
        owned["entry_id"], platform_catalog["entry_id"],
    ]

    with pytest.raises(RosterError, match="unknown roster entry_id"):
        assign_entry_ids([
            {"entry_id": "94a60a69-b249-49a3-8ee8-75b84323e416",
             "provider": "openai", "model": "gpt"},
        ], existing_entries=[owned, platform_catalog])

    with pytest.raises(RosterError, match="duplicate roster entry_id"):
        assign_entry_ids([dict(owned), dict(owned)], existing_entries=[owned, platform_catalog])


def test_live_voice_role_rejects_batch_only_models():
    with pytest.raises(RosterError, match="batch-only models"):
        validate_live_voice_assignments([{
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash-lite:batch",
            "voice_capable": True,
            "use_for_voice": True,
        }])


def test_live_voice_role_requires_audio_input_capability():
    with pytest.raises(RosterError, match="not marked as accepting audio input"):
        validate_live_voice_assignments([{
            "provider": "openrouter", "model": "text-only",
            "voice_capable": False, "use_for_voice": True,
        }])


def test_non_voice_batch_model_remains_valid_for_other_roles():
    validate_live_voice_assignments([{
        "model": "google/gemini-2.5-flash-lite:batch",
        "voice_capable": True, "use_for_voice": False,
    }])


def test_runtime_voice_role_ignores_legacy_batch_assignment():
    runtime = _runtime_config_from_roster({
        "multi_providers": [{
            "provider": "openrouter",
            "model": "google/gemini-2.5-flash-lite:batch",
            "voice_capable": True,
            "use_for_voice": True,
        }],
    }, role="voice")
    assert runtime["model"] == ""


def test_slot_identity_is_entry_id_even_when_models_and_credentials_match():
    rows = [
        {"entry_id": "one", "provider": "openai", "base_url": "https://api.openai.com/v1",
         "model": "gpt", "enabled": True},
        {"entry_id": "two", "provider": "openai", "base_url": "https://api.openai.com/v1",
         "model": "gpt", "enabled": False},
    ]

    slots = _assign_slots(rows)

    assert [slot["entry_id"] for slot in slots["slot_list"]] == ["one", "two"]
    from app.admin.settings import _resolve_slot
    assert _resolve_slot(slots, "role", role="standard")["entry_id"] == "one"




def test_encrypted_api_keys_are_keyed_by_entry_id_not_roster_position():
    import json
    from app.admin.settings import _pack_llm_secret, _rehydrate_llm_keys

    original = {"multi_providers": [
        {"entry_id": "one", "provider": "openai", "model": "same", "api_key": "key-one"},
        {"entry_id": "two", "provider": "openai", "model": "same", "api_key": "key-two"},
    ]}
    secret = _pack_llm_secret(original)
    payload = json.loads(secret)
    assert payload["v"] == 2
    assert payload["entries"] == {"one": "key-one", "two": "key-two"}

    reordered = {"multi_providers": [
        {"entry_id": "two", "provider": "openai", "model": "same", "api_key": ""},
        {"entry_id": "one", "provider": "openai", "model": "same", "api_key": ""},
    ]}
    _rehydrate_llm_keys(reordered, secret)
    assert [row["api_key"] for row in reordered["multi_providers"]] == [
        "key-two", "key-one",
    ]


def test_subscription_auth_requires_an_owned_saved_row(monkeypatch):
    from app.api import openai_subscription_auth as api
    from fastapi import HTTPException

    async def scope(_request, _scope, _agent_id):
        return {"scope": "user", "user_id": "user-one"}

    async def roster(_target):
        return [{"entry_id": "owned", "provider": "openai_subscription"}]

    monkeypatch.setattr(api, "_authorized_scope", scope)
    monkeypatch.setattr(api, "_scope_roster", roster)

    target = asyncio.run(api._authorize_entry(object(), "user", "", "owned"))
    assert target == {"scope": "user", "user_id": "user-one", "entry_id": "owned"}

    with pytest.raises(HTTPException) as error:
        asyncio.run(api._authorize_entry(object(), "user", "", "forged"))
    assert error.value.status_code == 404


def test_subscription_login_can_reserve_a_server_owned_id_before_model_selection(monkeypatch):
    from app.api import openai_subscription_auth as api
    from app import openai_subscription

    async def scope(_request, _scope, _agent_id):
        return {"scope": "user", "user_id": "user-one"}

    calls = []

    async def reserve(**target):
        calls.append(("reserve", target))

    async def start(**target):
        calls.append(("start", target))
        return {"verification_url": "https://auth.example/device", "user_code": "CODE-123"}

    monkeypatch.setattr(api, "_authorized_scope", scope)
    monkeypatch.setattr(openai_subscription, "reserve_entry", reserve)
    monkeypatch.setattr(openai_subscription, "start_device_login", start)

    result = asyncio.run(api.login_start(object(), scope="user"))

    assert UUID4.fullmatch(result["entry_id"])
    expected = {"scope": "user", "user_id": "user-one", "entry_id": result["entry_id"]}
    assert calls == [("reserve", expected), ("start", expected)]
    assert result["verification_url"] == "https://auth.example/device"


def test_reserved_subscription_ids_are_accepted_only_for_the_same_tenant(monkeypatch):
    from app import openai_subscription

    async def reserved(scope, *, entry_id, **target):
        return (
            scope == "user"
            and target.get("user_id") == "user-one"
            and entry_id == "reserved-id"
        )

    monkeypatch.setattr(openai_subscription, "entry_is_reserved", reserved)
    entries = [
        {"entry_id": "reserved-id", "provider": "openai_subscription", "model": "gpt"},
        {"entry_id": "forged-id", "provider": "openai", "model": "gpt"},
    ]
    existing = asyncio.run(openai_subscription.existing_entries_with_reservations(
        entries, [], scope="user", user_id="user-one",
    ))

    assert existing == [{"entry_id": "reserved-id"}]


def test_model_table_allows_subscription_connection_before_model_selection():
    source = Path("/app/ui/shared/js/model-table.js").read_text(encoding="utf-8")

    assert "Click to Issue verification code" in source
    assert "subscriptionConnect.disabled = !row && !S.selectedModel" not in source
    assert "pendingSubscriptionRow" in source
    assert "row?.entry_id || ''" in source
    assert "data.entry_id" in source
    assert (
        "entry_id: subscription && pendingSubscriptionRow?.entry_id "
        "? pendingSubscriptionRow.entry_id : ''"
    ) in source


def test_disconnect_deletes_only_the_row_token_and_disables_its_roles(monkeypatch):
    from app.api import openai_subscription_auth as api
    from app import openai_subscription

    async def authorize(_request, _scope, _agent_id, entry_id, **_options):
        return {"scope": "user", "user_id": "user-one", "entry_id": entry_id}

    calls = []

    async def sign_out(**target):
        calls.append(("token", target))

    async def disable(target, entry_id):
        calls.append(("roles", target, entry_id))

    monkeypatch.setattr(api, "_authorize_entry", authorize)
    monkeypatch.setattr(api, "_disable_entry_roles", disable)
    monkeypatch.setattr(openai_subscription, "sign_out", sign_out)

    result = asyncio.run(api.auth_signout(object(), scope="user", entry_id="row-one"))
    assert result == {"ok": True}
    assert calls == [
        ("token", {"scope": "user", "user_id": "user-one", "entry_id": "row-one"}),
        ("roles", {"scope": "user", "user_id": "user-one", "entry_id": "row-one"}, "row-one"),
    ]
def test_subscription_reset_cancels_only_the_pending_login(monkeypatch):
    from app.api import openai_subscription_auth as api
    from app import openai_subscription

    async def authorize(_request, _scope, _agent_id, entry_id, **_options):
        return {"scope": "user", "user_id": "user-one", "entry_id": entry_id}

    calls = []

    async def reset(**target):
        calls.append(target)
        return True

    monkeypatch.setattr(api, "_authorize_entry", authorize)
    monkeypatch.setattr(openai_subscription, "reset_device_login", reset)

    result = asyncio.run(api.login_reset(object(), scope="user", entry_id="row-one"))

    assert result == {"ok": True, "reservation_removed": True}
    assert calls == [{
        "scope": "user", "user_id": "user-one", "entry_id": "row-one",
    }]


def test_model_table_uses_server_ids_and_row_owned_subscription_controls():
    source = Path("/app/ui/shared/js/model-table.js").read_text(encoding="utf-8")

    assert "subscriptionProfileInput" not in source
    assert "credential_profile_id" not in source
    assert "entry_id: randomUUID()" not in source
    assert "credential_email" in source
    assert "Use other account" in source
    assert "Click to Issue verification code" in source
    assert "ensureSubscriptionRow" not in source
    assert "login/start', row?.entry_id || ''" in source
    assert "Connected to " in source
    assert "function renderSubscriptionAuthorization" in source
    assert 'ac-ability-name">Connection</span>' in source
    assert "const code = document.createElement('span')" in source
    assert "code.setAttribute('role', 'button')" in source
    assert "Open Verification page and paste code" in source
    assert "open.className = 'ac-btn" not in source
    assert "Check Authorization" in source
    assert "check.className = 'ac-btn ac-btn-primary ac-btn-sm'" in source
    assert "Reset" in source
    assert "Copy verification code" in source
    assert "copyText(data.user_code)" in source
    assert "expires_in_seconds" in source
    assert "Number(data.expires_in_seconds) || 900" in source
    assert "'Use within ' + minutes + ':' + seconds" in source
    assert "'/api/v1/openai-subscription/login/reset'" in source
    assert "popover.insertBefore(connectionSection, rolesSection)" not in source
    assert "p.provider === 'openai_subscription' && !p.credential_configured" not in source
    assert "'/api/v1/openai-subscription/auth/signout'" not in source
    assert "credential_suffix" in source
    # MODEL-NAME-LEVELS: the Auth line is gone; the panel names the model with the
    # catalog alias and layers the caller's own display name on top of it.
    assert "Auth:" not in source
    assert "metaLine('Model name:', metaValue(modelNameOf(p)))" in source
    assert "metaLine('Provider:', metaValue(provName))" in source
    assert "displayNameInput" in source
    assert "display_name: displayNameInput.value.trim()" in source

    css = Path("/app/ui/shared/css/app3.css").read_text(encoding="utf-8")
    assert "flex: 0 0 116px" in css
    assert ".ac-subscription-auth:empty { display: none; }" in css
    assert "gap: 8px" in css
    assert ".ac-model-config .ac-saved-row > .ac-ability-label" in css
    assert "position: fixed" in css
    assert "bottom: calc(100% + 4px)" not in css
    assert "function _positionRolesPopover" in source
    assert "_positionRolesPopover(rolesBtn, popover)" in source


def test_issuing_subscription_code_does_not_open_a_browser():
    source = Path("/app/ui/shared/js/model-table.js").read_text(encoding="utf-8")

    issue_handler = source.split(
        "async function startSubscriptionLogin()", 1
    )[1].split("subscriptionConnect.addEventListener", 1)[0]
    assert "await subscriptionRequest(" in issue_handler
    assert "window.open" not in issue_handler
    assert "openSubscriptionAuthWindow" not in source
    assert "open.href = data.verification_url" in source
    assert "open.target = '_blank'" in source




def test_public_provider_config_exposes_only_five_character_key_suffixes():
    from app.admin.settings import _public_provider_config

    public = _public_provider_config({
        "api_key": "top-key-ABCDE",
        "providers": {"openai": {"api_key": "provider-12345"}},
        "multi_providers": [{
            "entry_id": "row-one", "provider": "openai", "model": "gpt-test",
            "api_key": "roster-XYZ89",
        }],
    })

    assert public["api_key"] == ""
    assert public["credential_suffix"] == "ABCDE"
    assert public["providers"]["openai"]["api_key"] == ""
    assert public["providers"]["openai"]["credential_suffix"] == "12345"
    assert public["multi_providers"][0]["api_key"] == ""
    assert public["multi_providers"][0]["credential_suffix"] == "XYZ89"
    assert "top-key-ABCDE" not in repr(public)
    assert "provider-12345" not in repr(public)
    assert "roster-XYZ89" not in repr(public)



def test_optional_display_name_is_preserved_in_slots_and_redacts_public_picker_metadata():
    from app.admin.settings import _public_model_slot

    slots = _assign_slots([{
        "entry_id": "basic-id", "provider": "secret-provider",
        "base_url": "https://private.example/v1", "model": "vendor/real-model",
        "display_name": "Basic", "enabled": True,
    }])

    internal = slots["slot_list"][0]
    assert internal["model"] == "vendor/real-model"
    assert internal["display_name"] == "Basic"
    public = _public_model_slot(internal)
    assert public["model"] == "Basic"
    assert public["display_name"] == "Basic"
    assert "provider" not in public
    assert "base_url" not in public
    assert "auth_mode" not in public
    assert "vendor/real-model" not in repr(public)
    assert "secret-provider" not in repr(public)


def test_public_picker_falls_back_to_actual_model_without_display_name():
    from app.admin.settings import _public_model_slot

    public = _public_model_slot({
        "type": "custom", "position": 1, "entry_id": "plain-id",
        "provider": "openai", "model": "gpt-plain",
    })

    assert public["model"] == "gpt-plain"
    assert public["display_name"] == ""
    assert "provider" not in public


def test_model_table_supports_optional_alias_and_hides_inherited_details():
    source = Path("/app/ui/shared/js/model-table.js").read_text(encoding="utf-8")

    assert "Display name (optional)" not in source
    assert "Display name" in source
    assert "displayNameInput.placeholder = 'Optional'" in source
    assert "aliasInput.placeholder = 'Optional'" in source
    assert "tipBadge(displayNameHelp)" in source
    # MODEL-NAME-LEVELS: the tip describes the caller's own name layered over the
    # model's own name.
    assert "Your own name for this model, shown to users wherever the model appears." in source
    # A row shows the caller's own display name, falls back to the model's own name
    # (the catalog alias for a catalog row, the real model id for a BYO row) and
    # never prints a catalog row's real provider/model id.
    assert "const rowDisplayName = (p) => userDisplayName(p) || modelNameOf(p) || '—';" in source
    assert "Inherited model details are managed by the app administrator" in source


def test_model_table_separates_the_model_name_from_the_users_display_name():
    """MODEL-NAME-LEVELS: a row's MODEL NAME is the catalog's own alias for a
    catalog row (the real model id for a BYO row); its DISPLAY NAME is the
    caller's own second-level alias — edited in place and blank until they set one.
    """
    source = Path("/app/ui/shared/js/model-table.js").read_text(encoding="utf-8")
    # Level 1 — the model's own name.
    assert "const catalogModelName = (p) => String(p?.catalog_name ?? p?.display_name ?? '').trim()" in source
    assert "const modelNameOf = (p) => (isCatalogRow(p) ? catalogModelName(p) : (String(p?.model || '').trim() || '—'));" in source
    # Level 2 — the caller's own name, blank when unset: the rehydration retains
    # the catalog name when they have set none, so equality means "not set".
    assert "return isCatalogRow(p) && stored === modelNameOf(p) ? '' : stored;" in source
    # The panel shows Model name → Provider (the Auth line is gone) and the
    # editable Display name.
    assert "metaLine('Model name:', metaValue(modelNameOf(p)))" in source
    assert "Auth:" not in source
    assert "${esc(userDisplayName(p) || '—')}" in source
    # Renaming edits the caller's own value, never the stored catalog-name copy.
    assert "const current = userDisplayName(p);" in source
    # The normalized row carries the catalog's model name for rendering.
    assert "catalog_name: p.catalog_name || ''" in source

    projection = Path("/app/ui/main-panel/agents/configs/content.js").read_text(encoding="utf-8")
    assert "catalog_name: platform.catalog_name || platform.display_name || ''" in projection
    assert "display_name: row.display_name || platform.display_name || ''" in projection
    # The catalog model name is a projection, never roster data.
    assert "catalog_name," in projection

    catalog = Path("/app/ui/main-panel/app/settings/agent-settings/agent-settings.js").read_text(encoding="utf-8")
    assert "platform_aliases[String(row.entry_id || '')] = stored && stored !== catalogName ? stored : '';" in catalog
    assert "catalog_name: undefined" in catalog


def test_resolve_agent_models_exposes_alias_only(monkeypatch):
    from unittest.mock import AsyncMock, patch
    from app.admin import settings

    base = {
        "entry_id": "basic-id", "provider": "secret-provider",
        "base_url": "https://private.example/v1", "api_key": "hidden-key",
        "model": "vendor/real-model", "display_name": "Basic",
        "_platform_roster_id": "roster-free",
        "multi_providers": [{
            "entry_id": "basic-id", "provider": "secret-provider",
            "base_url": "https://private.example/v1", "api_key": "hidden-key",
            "model": "vendor/real-model", "display_name": "Basic",
            "enabled": True,
        }],
    }
    capabilities = {"models": {
        "roster_id": "roster-free", "allowed_entry_ids": ["basic-id"],
        "max_reasoning_effort": "high",
    }}
    with patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
         patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
         patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
        result = asyncio.run(settings.resolve_agent_models("ordinary-user"))

    serialized = repr(result)
    assert result["slots"][0]["model"] == "Basic"
    assert "vendor/real-model" not in serialized
    assert "secret-provider" not in serialized
    assert "private.example" not in serialized
    assert "hidden-key" not in serialized


def test_current_model_info_returns_alias_without_provider_or_true_model(monkeypatch):
    from app.admin import settings
    from app import model_catalog

    async def access(_request, _agent_id, _session_id):
        return "ordinary-user", None

    async def active(_user_id, _agent_rec, _session_id):
        return {
            "model": "vendor/real-model", "provider": "secret-provider",
            "display_name": "Basic", "reasoning_effort": "medium",
        }

    async def fresh():
        return None

    monkeypatch.setattr(settings, "_require_model_view_access", access)
    monkeypatch.setattr(settings, "resolve_active_model", active)
    monkeypatch.setattr(model_catalog, "ensure_fresh", fresh)
    monkeypatch.setattr(model_catalog, "lookup", lambda *_args, **_kwargs: {"context": 123})

    result = asyncio.run(settings.get_current_model_info(object()))

    assert result["model"] == "Basic"
    assert result["display_name"] == "Basic"
    assert result["provider"] == ""
    assert "vendor/real-model" not in repr(result)
    assert "secret-provider" not in repr(result)
    assert result["context"] == 123


def test_concrete_model_override_rebinds_the_selected_alias():
    from unittest.mock import patch
    from app.admin.settings import _merge_agent_override

    base = {
        "model": "vendor/basic-real", "display_name": "Basic",
        "multi_providers": [
            {"entry_id": "basic", "provider": "openai", "model": "vendor/basic-real",
             "display_name": "Basic", "enabled": True},
            {"entry_id": "pro", "provider": "openai", "model": "vendor/pro-real",
             "display_name": "Pro", "enabled": False},
        ],
    }
    with patch("app.admin.settings._load_app_settings", return_value={"extend_llm_to_agents": True}):
        result = _merge_agent_override(base, {
            "use_default": False, "model": "vendor/pro-real",
        })

    assert result["model"] == "vendor/pro-real"
    assert result["display_name"] == "Pro"


def test_model_switcher_capabilities_preserve_display_names(monkeypatch):
    """Model Switcher resolves aliases from the same runtime capability payload."""
    from unittest.mock import AsyncMock, patch
    from app.admin import settings

    base = {
        "model": "vendor/standard-real", "display_name": "Standard",
        "multi_providers": [{
            "model": "vendor/premium-real", "display_name": "Premium",
            "provider": "openai", "high_effort_capable": True,
        }],
    }
    with patch.object(settings, "_hydrate_agent_record_llm", AsyncMock(return_value=None)), \
         patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
         patch.object(settings, "_clamp_config_to_entitlements", AsyncMock(
             return_value=(base, {}))):
        caps = asyncio.run(settings.load_llm_capabilities_for_user("ordinary-user"))

    assert caps["default"]["model"] == "vendor/standard-real"
    assert caps["default"]["display_name"] == "Standard"
    assert caps["racers"][0]["model"] == "vendor/premium-real"
    assert caps["racers"][0]["display_name"] == "Premium"


def test_credential_save_is_prompt_free_and_signals_verified_success():
    source = Path("/app/ui/shared/js/model-table.js").read_text(encoding="utf-8")
    save_start = source.index("  async function saveCurrentCredential()")
    save_end = source.index("\n\n  // ── Saved-models", save_start)
    save = source[save_start:save_end]

    assert "window.prompt" not in save
    assert "const label = `${S.presets[provider]?.name || provider} credential`;" in save
    assert save.index("await saveSavedCredential") < save.index("flashCheck(credentialSaveCheck, true)")
    assert save.index("flashCheck(credentialSaveCheck, true)") < save.index("pulseAdvancedCredential()")
    css = Path("/app/ui/shared/css/app3.css").read_text(encoding="utf-8")
    assert "animation: ac-model-credential-glow 0.8s ease-in-out 2;" in css
    assert "setTimeout(() => advToggle.classList.remove('ac-model-credential-pulse'), 1800)" in source

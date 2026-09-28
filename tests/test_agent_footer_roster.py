import asyncio
from unittest.mock import AsyncMock, patch


def test_agent_footer_exposes_only_its_persisted_roster():
    """The user catalog/default roster must never widen an agent footer."""
    from app.admin import settings

    inherited = {
        "entry_id": "user-catalog", "provider": "openai",
        "base_url": "https://api.example/v1", "api_key": "catalog-key",
        "model": "user-catalog-model", "display_name": "User catalog model",
        "text_capable": True,
    }
    agent_owned = {
        "entry_id": "agent-only", "provider": "openai",
        "base_url": "https://api.example/v1", "api_key": "agent-key",
        "model": "agent-only-model", "display_name": "Agent model",
        "text_capable": True,
    }
    base = {"multi_providers": [inherited, agent_owned]}
    agent = {"id": "agent-1", "metadata": {"llm_config": {
        "use_default": False, "multi_providers": [agent_owned],
    }}}
    capabilities = {"models": {
        "allowed_entry_ids": ["user-catalog", "agent-only"],
        "allow_byo": True, "max_byo_entries": 10,
    }}

    with patch.object(settings, "_hydrate_agent_record_llm", AsyncMock(return_value=agent)), \
         patch.object(settings, "_resolve_user_config", AsyncMock(return_value=base)), \
         patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
         patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
        result = asyncio.run(settings.resolve_agent_models("user-1", agent_rec=agent))

    assert [slot["entry_id"] for slot in result["slots"]] == ["agent-only"]
    assert all("user-catalog-model" not in repr(slot) for slot in result["slots"])


def test_flat_footer_roster_preserves_order_and_exposes_roles():
    """Roles decorate one ordered roster row; they never create priority rows."""
    from app.admin.settings import _assign_slots, _resolve_slot

    rows = [
        {"entry_id": "catalog", "provider": "webagent_catalog", "model": "catalog"},
        {"entry_id": "flash", "provider": "openai", "model": "flash"},
        {"entry_id": "terra", "provider": "openai", "model": "terra"},
        {"entry_id": "sol", "provider": "openai", "model": "sol"},
    ]
    slots = _assign_slots(rows, {
        "system": "flash", "standard": "terra", "image_in": "terra", "premium": "sol",
    })

    assert [slot["entry_id"] for slot in slots["slot_list"]] == ["catalog", "flash", "terra", "sol"]
    assert slots["slot_list"][1]["roles"] == ["system"]
    assert slots["slot_list"][2]["roles"] == ["standard", "image_in"]
    assert slots["slot_list"][3]["roles"] == ["premium"]
    assert _resolve_slot(slots, "role", role="image_in")["entry_id"] == "terra"



def test_footer_default_active_slot_is_standard_not_first_roster_entry():
    """Roster order is presentation-only; a new chat starts on Standard."""
    from app.admin import settings

    flash = {"entry_id": "flash", "provider": "openai", "model": "v4.1-flash"}
    terra = {"entry_id": "terra", "provider": "openai", "model": "gpt-5.6-terra"}
    agent = {"id": "agent-1", "metadata": {"llm_config": {
        "use_default": False, "multi_providers": [flash, terra],
        "role_assignments": {"standard": "terra"},
    }}}
    capabilities = {"models": {
        "allowed_entry_ids": ["flash", "terra"], "allow_byo": True,
        "max_byo_entries": 10,
    }}

    with patch.object(settings, "_hydrate_agent_record_llm", AsyncMock(return_value=agent)), \
         patch.object(settings, "_resolve_user_config", AsyncMock(return_value={"multi_providers": [flash, terra]})), \
         patch.object(settings, "_load_session_override", AsyncMock(return_value=None)), \
         patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
        result = asyncio.run(settings.resolve_agent_models("user-1", agent_rec=agent))

    assert [slot["entry_id"] for slot in result["slots"]] == ["flash", "terra"]
    assert result["active_slot"]["entry_id"] == "terra"
    assert result["active_slot"]["role"] == "standard"

def test_effort_is_entry_owned_with_legacy_role_fallback():
    """The picker and run use one entry key; old role saves still take effect."""
    from app.admin import settings

    assert settings._resolve_session_effort(
        {"model_effort": {"entry:terra": "medium", "role:standard": "high"}},
        slot_ref="entry:terra", legacy_slot_ref="role:standard",
    ) == "medium"
    assert settings._resolve_session_effort(
        {"model_effort": {"role:standard": "high"}},
        slot_ref="entry:terra", legacy_slot_ref="role:standard",
    ) == "high"

    terra = {
        "entry_id": "terra", "provider": "openai", "model": "terra-model",
        "display_name": "Terra", "api_key": "key",
    }
    agent = {"id": "agent-1", "metadata": {"llm_config": {
        "use_default": False, "multi_providers": [terra],
        "role_assignments": {"standard": "terra"},
    }}}
    capabilities = {"models": {
        "allowed_entry_ids": ["terra"], "allow_byo": True,
        "max_byo_entries": 10, "max_reasoning_effort": "high",
    }}
    override = {"selection_type": "role", "role": "standard",
                "model_effort": {"role:standard": "high"}}
    with patch.object(settings, "_hydrate_agent_record_llm", AsyncMock(return_value=agent)), \
         patch.object(settings, "_resolve_user_config", AsyncMock(return_value={"multi_providers": [terra]})), \
         patch.object(settings, "_load_session_override", AsyncMock(return_value=override)), \
         patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
        result = asyncio.run(settings.resolve_agent_models("user-1", agent_rec=agent, session_id="session-1"))

    assert result["active_slot"]["effort_ref"] == "entry:terra"
    assert result["model_effort"]["entry:terra"] == "high"

def test_entitlement_clamp_keeps_distinct_agent_credential_by_entry_id():
    """A same-named platform model cannot replace an agent-owned credential."""
    from app.admin import settings

    platform = {
        "entry_id": "platform-terra", "provider": "openai_subscription",
        "base_url": "codex://chatgpt", "model": "gpt-5.6-terra",
        "credential_scope": "platform",
    }
    agent_row = {
        "entry_id": "agent-terra", "provider": "openai_subscription",
        "base_url": "codex://chatgpt", "model": "gpt-5.6-terra",
        "credential_scope": "agent", "api_key": "agent-only-credential",
    }
    effective = {
        "multi_providers": [agent_row], "entry_id": "agent-terra",
        "provider": "openai_subscription", "base_url": "codex://chatgpt",
        "model": "gpt-5.6-terra", "credential_scope": "agent",
    }
    capabilities = {"models": {
        "available": True, "allowed_entry_ids": ["platform-terra"],
        "allow_byo": True, "max_byo_entries": 10,
    }}

    with patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=capabilities)):
        clamped, _ = asyncio.run(settings._clamp_config_to_entitlements(
            "user-1", {"_platform_roster_id": "tier", "multi_providers": [platform]}, effective,
        ))

    assert clamped["entry_id"] == "agent-terra"
    assert clamped["model"] == "gpt-5.6-terra"
    assert clamped["api_key"] == "agent-only-credential"


def test_entry_ids_prevent_cross_credential_matching():
    """Identical provider/model rows are distinct once they have entry IDs."""
    from app.admin.settings import _borrow_roster_credentials, _same_roster_entry

    first = {
        "entry_id": "credential-a", "provider": "openai",
        "base_url": "https://api.example/v1", "model": "same-model",
        "credential_scope": "agent", "api_key": "key-a",
    }
    second = {
        "entry_id": "credential-b", "provider": "openai",
        "base_url": "https://api.example/v1", "model": "same-model",
        "credential_scope": "agent", "api_key": "key-b",
    }

    assert not _same_roster_entry(first, second)
    adopted = {**second, "api_key": ""}
    _borrow_roster_credentials([adopted], [first])
    assert adopted["api_key"] == ""

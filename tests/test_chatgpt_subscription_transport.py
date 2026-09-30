import asyncio
import base64
import json
import time

import httpx

from app import openai_subscription
from app.agent.loop import _provider_engine_id
from app.agent.response_chaining import open_responses_stream, response_chain_capability


def _jwt(claims):
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}.signature"


def test_subscription_uses_webagent_harness_and_responses_transport():
    config = {"provider": "openai_subscription", "base_url": "codex://chatgpt"}
    assert _provider_engine_id(config) == ""
    capability = response_chain_capability(
        provider="openai_subscription", base_url="codex://chatgpt", model="gpt-5.5",
    )
    assert capability.enabled is True
    assert capability.transport == "responses"


def test_device_login_matches_openai_fifteen_minute_expiry(monkeypatch):
    async def start_device(_url, _payload):
        return {"user_code": "CODE-123", "device_auth_id": "device-123"}

    monkeypatch.setattr(openai_subscription, "_post_json", start_device)
    result = asyncio.run(openai_subscription.start_device_login(
        scope="platform", entry_id="timer-test",
    ))

    assert result["expires_in_seconds"] == 900
    assert time.time() + 895 <= result["expires_at"] <= time.time() + 900


def test_reset_device_login_deletes_only_an_unconnected_reservation(monkeypatch):
    from app import model_credentials

    deleted = []

    async def pending(*_args, **_kwargs):
        return {"config": {"pending": True}, "secret_ref": ""}

    async def delete(*_args, **kwargs):
        deleted.append(kwargs["label"])
        return True

    monkeypatch.setattr(model_credentials, "get_owned", pending)
    monkeypatch.setattr(model_credentials, "delete_owned", delete)

    removed = asyncio.run(openai_subscription.reset_device_login(
        scope="platform", entry_id="pending-reset",
    ))

    assert removed is True
    assert deleted == ["pending-reset"]


def test_reset_device_login_preserves_an_existing_connected_credential(monkeypatch):
    from app import model_credentials

    async def connected(*_args, **_kwargs):
        return {"config": {"email": "person@example.com"}, "secret_ref": "tokens"}

    async def unexpected_delete(*_args, **_kwargs):
        raise AssertionError("connected credential must not be deleted")

    monkeypatch.setattr(model_credentials, "get_owned", connected)
    monkeypatch.setattr(model_credentials, "delete_owned", unexpected_delete)

    removed = asyncio.run(openai_subscription.reset_device_login(
        scope="platform", entry_id="connected-reset",
    ))

    assert removed is False


def test_subscription_catalog_exposes_verified_roster_capabilities():
    models = openai_subscription.models()
    assert {model["id"] for model in models} == {
        "gpt-5.3-codex-spark",
        "gpt-5.4",
        "gpt-5.4-mini",
        "gpt-5.5",
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
        "gpt-6.1-sol",
        "gpt-6-astra",
    }
    for model in models:
        assert model["text_capable"] is True
        assert model["image_capable"] is True
        assert model["image_out_capable"] is False
        assert model["voice_capable"] is False
        assert model["input_modalities"] == ["text", "image"]
        assert model["output_modalities"] == ["text"]
        assert model["tool_capable"] is True
        assert model["modality_known"] is True


def test_stale_sol_capabilities_normalize_and_oauth_worker_stays_selectable():
    from app.admin.settings import (
        _normalize_subscription_model_capabilities,
        pick_vision_model,
    )

    stale = {
        "entry_id": "sol-row",
        "model": "gpt-5.6-sol",
        "provider": "openai_subscription",
        "auth_mode": "chatgpt",
        "credential_scope": "agent",
        "base_url": "codex://chatgpt",
        "image_capable": False,
        "text_capable": False,
    }
    normalized = _normalize_subscription_model_capabilities(stale)
    assert normalized["image_capable"] is True
    assert normalized["text_capable"] is True

    picked = pick_vision_model({
        "default": normalized,
        "racers": [],
        "credential_context": {"user_id": "user-9", "agent_id": "agent-7"},
    })
    assert picked == {
        "entry_id": "sol-row",
        "model": "gpt-5.6-sol",
        "provider": "openai_subscription",
        "auth_mode": "chatgpt",
        "credential_scope": "agent",
        "base_url": "codex://chatgpt",
        "api_key": "",
        "user_id": "user-9",
        "agent_id": "agent-7",
    }


def test_text_only_subscription_row_is_not_selected_for_vision():
    from app.admin.settings import pick_vision_model

    caps = {
        "default": {
            "entry_id": "text-row",
            "model": "subscription-text-only",
            "provider": "openai_subscription",
            "credential_scope": "platform",
            "image_capable": False,
        },
        "racers": [],
    }
    assert pick_vision_model(caps) is None


def test_model_table_keeps_text_roles_and_gates_media_roles_by_capability():
    source = open("ui/shared/js/model-table.js", encoding="utf-8").read()
    assert "_buildRoleToggle(rolesSection, p.text_capable !== false, hasRole(p, 'premium')" in source
    assert "_buildRoleToggle(rolesSection, p.text_capable !== false, hasRole(p, 'system')" in source
    assert "_buildRoleToggle(rolesSection, liveVoiceCapable, hasRole(p, 'voice')" in source
    assert "voice_capable: capVoice" in source


def test_admin_roster_adapter_preserves_voice_capability():
    source = open(
        "ui/main-panel/app/settings/agent-settings/agent-settings.js",
        encoding="utf-8",
    ).read()
    assert "voice_capable: single.voice_capable" in source
    assert "voice_capable: !!p.voice_capable" in source


def test_account_metadata_matches_opencode_claim_extraction():
    access = _jwt({"https://api.openai.com/auth": {
        "chatgpt_account_id": "acct_123", "chatgpt_compute_residency": "us",
        "chatgpt_plan_type": "plus",
    }, "email": "person@example.com"})
    metadata = openai_subscription.token_metadata({"access_token": access})
    assert metadata == {
        "account_id": "acct_123", "residency": "us",
        "plan_type": "plus", "email": "person@example.com",
    }


def test_refresh_preserves_existing_refresh_token(monkeypatch):
    saved = {}
    async def load_tokens(owner, entry_id, **target):
        assert (owner, entry_id) == ("_platform", "row-one")
        return {"access_token": "expired", "refresh_token": "old", "expires_at": 1}
    async def save_tokens(tokens, owner, entry_id, **target):
        assert (owner, entry_id) == ("_platform", "row-one")
        saved.update(tokens)
    async def post_form(_url, data):
        assert data["refresh_token"] == "old"
        return {"access_token": "fresh", "expires_in": 3600}
    monkeypatch.setattr(openai_subscription, "_load_tokens", load_tokens)
    monkeypatch.setattr(openai_subscription, "_save_tokens", save_tokens)
    monkeypatch.setattr(openai_subscription, "_post_form", post_form)
    auth = asyncio.run(openai_subscription.transport_auth(entry_id="row-one"))
    assert auth["access_token"] == "fresh"
    assert saved["refresh_token"] == "old"
    assert saved["expires_at"] > time.time()



def test_subscription_credentials_resolve_to_tenant_owners():
    assert openai_subscription.credential_owner("platform") == "_platform"
    assert openai_subscription.credential_owner("agent", agent_id="agent-7") == "_agent:agent-7"
    assert openai_subscription.credential_owner("user", user_id="user-9") == "user-9"


def test_subscription_credentials_reject_missing_or_invalid_tenant_context():
    for scope, kwargs in (("agent", {}), ("user", {}), ("root", {})):
        try:
            openai_subscription.credential_owner(scope, **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(f"{scope} should have been rejected")


def test_transport_auth_loads_selected_tenant(monkeypatch):
    seen = {}

    async def load_tokens(owner, entry_id, **target):
        seen.update(owner=owner, entry_id=entry_id, **target)
        return {
            "access_token": "fresh", "refresh_token": "refresh",
            "expires_at": time.time() + 3600,
        }

    monkeypatch.setattr(openai_subscription, "_load_tokens", load_tokens)
    auth = asyncio.run(openai_subscription.transport_auth(
        scope="agent", agent_id="agent-7", entry_id="row-primary",
    ))
    assert auth["access_token"] == "fresh"
    assert seen == {
        "owner": "_agent:agent-7", "entry_id": "row-primary",
        "scope": "agent", "user_id": "", "agent_id": "agent-7",
    }


def test_tenant_scopes_are_wired_to_each_configuration_surface():
    model_table = open("ui/shared/js/model-table.js", encoding="utf-8").read()
    agent_ui = open("ui/main-panel/agents/configs/content.js", encoding="utf-8").read()
    account_ui = open("ui/shared/js/my-models.js", encoding="utf-8").read()
    assert "subscriptionScope = opts.subscriptionScope || 'platform'" in model_table
    assert "scope=agent&agent_id=" in agent_ui
    assert "scope=user" in account_ui
    assert "profile_id" not in model_table

    model_table = open("ui/shared/js/model-table.js", encoding="utf-8").read()
    agent_ui = open("ui/main-panel/agents/configs/content.js", encoding="utf-8").read()
    account_ui = open("ui/shared/js/my-models.js", encoding="utf-8").read()
    assert "subscriptionScope = opts.subscriptionScope || 'platform'" in model_table
    assert "scope=agent&agent_id=" in agent_ui
    assert "scope=user" in account_ui
    assert "profile_id" not in model_table


def test_status_exposes_email_metadata_without_tokens(monkeypatch):
    access = _jwt({"email": "owner@example.com", "https://api.openai.com/auth": {
        "chatgpt_plan_type": "plus",
    }})

    async def load_tokens(owner, entry_id, **target):
        assert (owner, entry_id) == ("user-9", "row-one")
        return {
            "access_token": access, "refresh_token": "do-not-return",
            "email": "owner@example.com", "plan_type": "plus",
        }

    monkeypatch.setattr(openai_subscription, "_load_tokens", load_tokens)
    result = asyncio.run(openai_subscription.status(
        scope="user", user_id="user-9", entry_id="row-one",
    ))
    assert result["account"]["email"] == "owner@example.com"
    assert result["plan_type"] == "plus"
    assert result["entry_id"] == "row-one"
    assert "do-not-return" not in json.dumps(result)


def test_same_model_rows_remain_independent_by_entry_id():
    from app.admin.settings import _assign_slots, _resolve_slot

    rows = [
        {"entry_id": "one", "provider": "openai_subscription", "base_url": "codex://chatgpt",
         "model": "gpt-5.6-sol", "credential_scope": "user", "enabled": True},
        {"entry_id": "two", "provider": "openai_subscription", "base_url": "codex://chatgpt",
         "model": "gpt-5.6-sol", "credential_scope": "user", "enabled": False},
    ]
    slots = _assign_slots(rows)
    assert len(slots["slot_list"]) == 2
    custom = _resolve_slot(slots, "custom", custom_position=1, entry_id="two")
    assert custom["entry_id"] == "two"


def test_model_table_uses_row_owned_subscription_credentials():
    source = open("ui/shared/js/model-table.js", encoding="utf-8").read()
    assert "subscriptionProfileInput" not in source
    assert "credential_profile_id" not in source
    assert "Click to Issue verification code" in source
    assert "credential_email" in source
    assert "Managed by app admin" in source


def test_user_byo_accepts_subscription_url_and_tenant_fields():
    from app.entitlements.rosters import validate_roster_entries
    rows = validate_roster_entries([{
        "provider": "openai_subscription", "auth_mode": "chatgpt",
        "base_url": "codex://chatgpt", "model": "gpt-5.6-sol",
        "credential_scope": "user",
    }], require_provider_model=True, untrusted_urls=True)
    assert rows[0]["credential_scope"] == "user"

def test_subscription_stream_uses_exact_endpoint_and_headers():
    async def handler(request):
        assert str(request.url) == openai_subscription.RESPONSES_ENDPOINT
        assert request.headers["authorization"] == "Bearer access-token"
        assert request.headers["chatgpt-account-id"] == "acct_123"
        assert request.headers["originator"] == "webagent"
        assert request.headers["session-id"] == "wa-session"
        return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"ok"}\n\n')
    async def run():
        stream = await open_responses_stream(
            base_url="codex://chatgpt", api_key="access-token", family="openai",
            body={"model": "gpt-5.5", "input": [], "stream": True},
            request_url=openai_subscription.RESPONSES_ENDPOINT,
            extra_headers={"ChatGPT-Account-Id": "acct_123",
                           "originator": "webagent", "session-id": "wa-session"},
            transport=httpx.MockTransport(handler),
        )
        chunks = [chunk async for chunk in stream]
        await stream.close()
        return chunks

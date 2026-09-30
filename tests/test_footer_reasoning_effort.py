import asyncio
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import openai_subscription
from app.admin import settings
from app.agent.loop import _effective_reasoning_effort
from app.agent.response_chaining import build_responses_request, open_responses_stream
from app.chat import api, execution
from app.entitlements.policy import ADMIN_OVERLAY, normalize_policy
from app.entitlements.tiers import seed_policy
from app.reasoning_effort import normalize_effort, supported_efforts

LEVELS = ["low", "medium", "high", "xhigh", "max"]


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-astra"])
def test_native_levels_and_legacy_minimal(model):
    assert supported_efforts(model, "openai") == LEVELS
    assert supported_efforts(model, "openai_subscription") == LEVELS
    assert normalize_effort("minimal", model, "openai_subscription") == "low"
    assert normalize_effort("none", model, "openai") is None
    assert normalize_effort("default", model, "openai") is None
    assert normalize_effort("minimal", model, "openai", maximum="minimal") is None
    row = next(row for row in openai_subscription.models() if row["id"] == model)
    assert row["reasoning_efforts"] == LEVELS
    public = settings._public_model_slot({
        "entry_id": "sol", "model": model, "provider": "openai_subscription",
        "display_name": "sol botboss",
    })
    assert public["model"] == "sol botboss"
    assert public["reasoning_efforts"] == LEVELS
    assert "provider" not in public


def test_policy_supports_extended_levels_without_raising_existing_tier_limits():
    assert ADMIN_OVERLAY["models"]["max_reasoning_effort"] == "max"
    for level in ["xhigh", "max"]:
        policy = seed_policy("free")
        policy["models"]["max_reasoning_effort"] = level
        assert normalize_policy(policy)["models"]["max_reasoning_effort"] == level
    assert settings._clamp_reasoning_effort("max", {"models": {"max_reasoning_effort": "high"}}) == "high"
    assert settings._clamp_reasoning_effort("bogus", {"models": {"max_reasoning_effort": "max"}}) is None


@pytest.fixture
def footer_api(monkeypatch):
    effort_map = {}
    class DB:
        async def set_session_model_effort(self, session_id, ref, level):
            if level and level != "default":
                effort_map[ref] = level
            else:
                effort_map.pop(ref, None)
            return {"model_effort": dict(effort_map)}
    db = DB()
    repo = type("Repo", (), {"publish_session_notice": AsyncMock()})()
    maximum = {"models": {"max_reasoning_effort": "max"}}
    monkeypatch.setattr(api, "scope", AsyncMock(return_value=("admin", repo, {})))
    monkeypatch.setattr(execution, "_require_chat_session_access", AsyncMock(return_value=("admin", db)))
    monkeypatch.setattr(execution, "_entitled_session_model_options", AsyncMock(return_value={
        "slots": [{"entry_id": "sol", "model": "sol botboss", "roles": ["standard"],
                   "reasoning_efforts": LEVELS}],
    }))
    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", AsyncMock(return_value=maximum))
    app = FastAPI()
    app.include_router(api.router)
    return TestClient(app), effort_map, maximum


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-astra"])
@pytest.mark.parametrize("level", LEVELS)
def test_v2_footer_save_reaches_subscription_request(footer_api, model, level):
    client, saved, _ = footer_api
    response = client.post("/api/v2/chat/sessions/test-session/model-effort", json={
        "slot_ref": "entry:sol", "reasoning_effort": level,
    })
    assert response.status_code == 200
    resolved = settings._resolve_session_effort(
        {"model_effort": saved}, model=model, slot_ref="entry:sol",
    )
    provider_effort = _effective_reasoning_effort(resolved, "openai_subscription", "responses", model)
    body = build_responses_request(model=model,
        messages=[{"role": "user", "content": "Reply ok"}], tools=None,
        max_output_tokens=64, reasoning_effort=provider_effort)
    async def handler(request):
        payload = json.loads(request.content)
        assert payload["reasoning"] == {"effort": level}
        assert payload["model"] == model
        assert "reasoning_effort" not in payload
        return httpx.Response(200, text='data: {"type":"response.output_text.delta","delta":"ok"}\n\n')
    async def send():
        stream = await open_responses_stream(
            base_url="codex://chatgpt", api_key="test-token", family="openai",
            body=body, request_url=openai_subscription.RESPONSES_ENDPOINT,
            transport=httpx.MockTransport(handler),
        )
        chunks = [chunk async for chunk in stream]
        await stream.close()
        return chunks
    assert asyncio.run(send())
    response = client.post("/api/v2/chat/sessions/test-session/model-effort", json={
        "slot_ref": "entry:sol", "reasoning_effort": "default",
    })
    assert response.status_code == 200
    assert saved == {}
    assert settings._resolve_session_effort({"model_effort": saved}, slot_ref="entry:sol") is None
    default_body = build_responses_request(model=model,
        messages=[{"role": "user", "content": "Reply ok"}], tools=None,
        max_output_tokens=64, reasoning_effort=None)
    assert "reasoning" not in default_body


@pytest.mark.parametrize("level", ["none", "minimal"])
def test_v2_footer_rejects_unsupported_levels(footer_api, level):
    client, saved, _ = footer_api
    response = client.post("/api/v2/chat/sessions/test-session/model-effort", json={
        "slot_ref": "entry:sol", "reasoning_effort": level,
    })
    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "unsupported_reasoning_effort"
    assert saved == {}


def test_v2_footer_preserves_tier_limit(footer_api):
    client, saved, maximum = footer_api
    maximum["models"]["max_reasoning_effort"] = "high"
    response = client.post("/api/v2/chat/sessions/test-session/model-effort", json={
        "slot_ref": "entry:sol", "reasoning_effort": "max",
    })
    assert response.status_code == 403
    assert saved == {}


@pytest.mark.parametrize("maximum,expected", [
    ("high", ["low", "medium", "high"]),
    ("max", LEVELS),
])
def test_footer_projection_uses_actual_model_and_tier(monkeypatch, maximum, expected):
    row = {"entry_id": "sol", "provider": "openai_subscription",
           "model": "gpt-6.1-sol", "display_name": "sol botboss"}
    agent = {"id": "agent", "metadata": {"llm_config": {
        "multi_providers": [row], "role_assignments": {"standard": "sol"},
    }}}
    monkeypatch.setattr(settings, "_hydrate_agent_record_llm", AsyncMock(return_value=agent))
    monkeypatch.setattr(settings, "_resolve_user_config", AsyncMock(return_value={"multi_providers": [row]}))
    monkeypatch.setattr(settings, "_load_session_override", AsyncMock(return_value={
        "model_effort": {"entry:sol": "minimal"},
    }))
    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
        "models": {"allowed_entry_ids": ["sol"], "allow_byo": True,
                   "max_byo_entries": 10, "max_reasoning_effort": maximum},
    }))
    result = asyncio.run(settings.resolve_agent_models("admin", agent, "session"))
    assert result["slots"][0]["model"] == "sol botboss"
    assert result["slots"][0]["reasoning_efforts"] == expected
    assert result["model_effort"]["entry:sol"] == "low"


@pytest.mark.parametrize("level", ["xhigh", "max"])
def test_native_chat_completion_loop_uses_openai_effort_parameter(level):
    from test_loop_tool_choice_fallback import ToolChoiceFallbackTests
    harness = ToolChoiceFallbackTests()
    calls, _ = asyncio.run(harness._run(
        [{"text": "ok"}], reasoning_model=True,
        provider_config={"provider": "openai", "base_url": "https://api.openai.com/v1",
                         "api_key": "test-key", "model": "gpt-6.1-sol",
                         "stateful_responses": "disabled", "reasoning_effort": level},
    ))
    assert calls
    assert calls[0]["reasoning_effort"] == level
    assert "temperature" not in calls[0]
    assert "reasoning" not in (calls[0].get("extra_body") or {})

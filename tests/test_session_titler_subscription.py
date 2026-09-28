"""Regression coverage for the Session Namer's ChatGPT subscription transport."""

import asyncio
from types import SimpleNamespace

from app import openai_subscription
from app.agent import response_chaining
from plugins.app_functions.session_titler import session_titler


def test_subscription_title_uses_subscription_transport(monkeypatch):
    """``codex://chatgpt`` must never be passed to the generic OpenAI client."""
    seen = {}

    async def fake_system_prompt(agent_id=""):
        return "Write a concise title."

    async def fake_subscription_title(messages, *, model, provider_config, user_id,
                                      session_id, agent_id):
        seen.update({
            "messages": messages,
            "model": model,
            "provider_config": provider_config,
            "user_id": user_id,
            "session_id": session_id,
            "agent_id": agent_id,
        })
        return "Subscription Session Title"

    def unexpected_generic_client(*_args, **_kwargs):
        raise AssertionError("subscription titling must not construct AsyncOpenAI")

    monkeypatch.setattr(session_titler, "_system_prompt", fake_system_prompt)
    monkeypatch.setattr(session_titler, "_subscription_title", fake_subscription_title)
    monkeypatch.setattr("app.agent.loop._get_client", unexpected_generic_client)

    title = asyncio.run(session_titler._llm_title(
        ["Please diagnose my session title issue."],
        model="gpt-5.6-terra",
        user_id="admin",
        session_id="session-123",
        agent_id="shared_default",
        provider_config={
            "provider": "openai_subscription",
            "base_url": "codex://chatgpt",
            "entry_id": "subscription-row",
            "credential_scope": "platform",
        },
    ))

    assert title == "Subscription Session Title"
    assert seen["model"] == "gpt-5.6-terra"
    assert seen["provider_config"]["base_url"] == "codex://chatgpt"
    assert seen["session_id"] == "session-123"


def test_subscription_title_uses_chatgpt_oauth_responses_endpoint(monkeypatch):
    """The lower-level helper uses the normal loop's OAuth Responses contract."""
    seen = {}

    async def fake_auth(**kwargs):
        seen["auth"] = kwargs
        return {"access_token": "test-token", "account_id": "account-1", "residency": "us"}

    class FakeStream:
        async def close(self):
            seen["closed"] = True

        def __aiter__(self):
            async def chunks():
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="Diagnose ")
                )])
                yield SimpleNamespace(choices=[SimpleNamespace(
                    delta=SimpleNamespace(content="Session Titles")
                )])
            return chunks()

    async def fake_open_stream(**kwargs):
        seen["request"] = kwargs
        return FakeStream()

    monkeypatch.setattr(openai_subscription, "transport_auth", fake_auth)
    monkeypatch.setattr(response_chaining, "open_responses_stream", fake_open_stream)

    title = asyncio.run(session_titler._subscription_title(
        [{"role": "user", "content": "Diagnose session titles"}],
        model="gpt-5.6-terra",
        provider_config={
            "provider": "openai_subscription",
            "base_url": "codex://chatgpt",
            "entry_id": "subscription-row",
            "credential_scope": "platform",
        },
        user_id="admin",
        session_id="session-123",
        agent_id="shared_default",
    ))

    assert title == "Diagnose Session Titles"
    assert seen["auth"] == {
        "scope": "platform", "entry_id": "subscription-row",
        "user_id": "admin", "agent_id": "shared_default",
    }
    assert seen["request"]["request_url"] == openai_subscription.RESPONSES_ENDPOINT
    assert seen["request"]["api_key"] == "test-token"
    assert seen["request"]["extra_headers"]["ChatGPT-Account-Id"] == "account-1"
    assert seen["request"]["body"]["store"] is False
    assert "max_output_tokens" not in seen["request"]["body"]
    assert seen["closed"] is True

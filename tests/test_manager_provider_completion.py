import asyncio

from app.agent import manager
from app.agent.provider_completion import SubscriptionResponseClient


def test_subscription_model_override_resolves_transport(monkeypatch):
    from app.admin import settings

    async def _capabilities(_user_id, *, agent_rec=None):
        return {
            "default": {
                "provider": "openai_subscription",
                "model": "gpt-5.6-terra",
                "base_url": "codex://chatgpt",
                "credential_scope": "platform",
                "entry_id": "entry-1",
                "enabled": True,
                "text_capable": True,
            },
            "racers": [],
        }

    monkeypatch.setattr(settings, "load_llm_capabilities_for_user", _capabilities)

    model, provider, client = asyncio.run(manager._resolve_llm(
        "user-1", "gpt-5.6-terra", {"id": "agent-1"},
    ))

    assert model == "gpt-5.6-terra"
    assert provider == "openai_subscription"
    assert isinstance(client, SubscriptionResponseClient)
    assert client.entry_id == "entry-1"
    assert client.user_id == "user-1"

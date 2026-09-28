"""Focused tests for the provider-capped Closing Message finalizer."""

import asyncio
from types import SimpleNamespace

from app.agent import closer_finalizer, provider_completion


def _response(content: str, finish: str = "stop"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content, reasoning_content=None),
        finish_reason=finish,
    )])


def test_finalizer_passes_the_configured_cap_without_a_fallback_expansion(monkeypatch):
    seen = {}

    async def fake_complete(client, **kwargs):
        seen["client"] = client
        seen.update(kwargs)
        return _response("- Done\nFLAG: complete\nReady")

    monkeypatch.setattr(closer_finalizer, "complete_text", fake_complete)
    result = asyncio.run(closer_finalizer.finalize(
        "client",
        model="test-model",
        provider="test-provider",
        base_url="https://provider.example/v1",
        draft="A deliberately overlong working draft.",
        policy={
            "max_tokens": 37,
            "instruction": "End with a valid FLAG line and status line.",
            "rejection_message": "Use concise bullet points only.",
        },
    ))

    assert result == "- Done\nFLAG: complete\nReady"
    assert seen["max_tokens"] == 37
    assert seen["strict_max_tokens"] is True
    assert seen["temperature"] == 0.0
    prompt = seen["messages"][0]["content"]
    assert "hard maximum of 37 output tokens" in prompt
    assert "Use concise bullet points only." in prompt
    assert seen["messages"][1]["content"].endswith("A deliberately overlong working draft.")


def test_finalizer_skips_disabled_limit(monkeypatch):
    async def unexpected(*args, **kwargs):
        raise AssertionError("provider must not run when the cap is disabled")

    monkeypatch.setattr(closer_finalizer, "complete_text", unexpected)
    result = asyncio.run(closer_finalizer.finalize(
        "client", model="m", provider="p", base_url="", draft="draft", policy={"max_tokens": 0},
    ))
    assert result is None


def test_strict_provider_completion_never_expands_a_length_stopped_budget():
    calls = []

    class Completions:
        async def create(self, **kwargs):
            calls.append(kwargs)
            return _response("", finish="length")

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    response = asyncio.run(provider_completion.safe_chat_completion(
        client,
        model="test-model",
        messages=[{"role": "user", "content": "hi"}],
        max_tokens=9,
        strict_max_tokens=True,
    ))

    assert response.choices[0].finish_reason == "length"
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 9

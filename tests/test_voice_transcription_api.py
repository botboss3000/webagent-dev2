"""Tests for the server-side cross-browser dictation fallback."""

from __future__ import annotations

import asyncio
import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.api import transcription


class _Upload:
    filename = "dictation.webm"
    content_type = "audio/webm;codecs=opus"

    async def read(self, _limit: int) -> bytes:
        return b"recorded-audio"


class _Client:
    def __init__(self, response: httpx.Response, calls: list):
        self.response = response
        self.calls = calls

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


def test_openrouter_transcription_payload() -> None:
    calls = []

    async def resolve(_user_id: str, **_kwargs):
        return {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "test-key",
            "model": "openai/whisper-large-v3",
        }

    with (
        patch("app.auth.identity.assert_caller_is", AsyncMock(return_value="user-1")),
        patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
            "features": {"voice_llm": True},
        })),
        patch.object(transcription, "_resolve_user_config", resolve),
        patch.object(
            transcription,
            "get_voice_dictation_config",
            lambda: {"llm_enabled": True, "mode": "browser_then_llm"},
        ),
        patch.object(
            transcription.httpx,
            "AsyncClient",
            lambda **_kwargs: _Client(httpx.Response(200, json={"text": "test 1 2 3"}), calls),
        ),
        patch("app.model_catalog.ensure_fresh", AsyncMock()),
        patch("app.model_catalog.lookup", return_value={
            "input_modalities": ["audio"], "output_modalities": ["transcription"],
        }),
    ):
        result = asyncio.run(
            transcription.transcribe_audio(
                request=SimpleNamespace(),
                file=_Upload(),
                user_id="user-1",
                language="en-US",
            )
        )

    assert result == {"text": "test 1 2 3"}
    url, request = calls[0]
    assert url == "https://openrouter.ai/api/v1/audio/transcriptions"
    assert request["json"]["model"] == "openai/whisper-large-v3"
    assert request["json"]["input_audio"] == {
        "data": base64.b64encode(b"recorded-audio").decode("ascii"),
        "format": "webm",
    }
    assert request["json"]["language"] == "en"


def test_openrouter_audio_input_model_uses_chat_completions() -> None:
    calls = []
    config = {
        "provider": "openrouter",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "test-key",
        "model": "google/gemini-2.5-flash-lite:batch",
    }
    response = httpx.Response(200, json={
        "choices": [{"message": {"content": "hello from audio"}}],
    })

    with (
        patch.object(
            transcription, "_resolve_transcription_config",
            AsyncMock(return_value=config),
        ),
        patch.object(
            transcription.httpx, "AsyncClient",
            lambda **_kwargs: _Client(response, calls),
        ),
        patch("app.model_catalog.ensure_fresh", AsyncMock()),
        patch("app.model_catalog.lookup", return_value={
            "input_modalities": ["text", "audio"], "output_modalities": ["text"],
        }),
    ):
        result = asyncio.run(transcription._transcribe_bytes(
            "user-1", b"recorded-audio", "audio/webm", "en-US",
        ))

    assert result == "hello from audio"
    url, request = calls[0]
    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert request["json"]["model"] == "google/gemini-2.5-flash-lite:batch"
    content = request["json"]["messages"][0]["content"]
    assert content[0]["type"] == "text"
    assert "transcribe" in content[0]["text"].lower()
    assert content[1] == {
        "type": "input_audio",
        "input_audio": {
            "data": base64.b64encode(b"recorded-audio").decode("ascii"),
            "format": "webm",
        },
    }


def test_transcription_rejects_when_admin_disables_llm_voice() -> None:
    with (
        patch("app.auth.identity.assert_caller_is", AsyncMock(return_value="user-1")),
        patch("app.entitlements.service.resolve_capabilities", AsyncMock(return_value={
            "features": {"voice_llm": True},
        })),
        patch.object(
            transcription,
            "get_voice_dictation_config",
            lambda: {"llm_enabled": False, "mode": "browser_then_llm"},
        ),
    ):
        try:
            asyncio.run(
                transcription.transcribe_audio(
                    request=SimpleNamespace(),
                    file=_Upload(),
                    user_id="user-1",
                    language="en",
                )
            )
        except HTTPException as exc:
            assert exc.status_code == 403
        else:
            raise AssertionError("disabled LLM voice dictation was accepted")

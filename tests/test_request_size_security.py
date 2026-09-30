from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError

from app.api.rate_limit import enforce_tier_chat
from app.models.schemas import ChatRequest
from app.security import RequestSecurityMiddleware, WebSecurityPolicy


def _chat(message: str) -> ChatRequest:
    return ChatRequest(user_id="user", session_id="session", message=message)


def test_registered_chat_schema_caps_messages_at_200k() -> None:
    assert len(_chat("x" * 200_000).message) == 200_000
    with pytest.raises(ValidationError):
        _chat("x" * 200_001)


def test_anonymous_chat_caps_messages_at_32k_before_budget_work() -> None:
    with pytest.raises(HTTPException) as captured:
        asyncio.run(enforce_tier_chat("anon_test", None, message="x" * 32_001))
    assert captured.value.status_code == 413
    assert captured.value.detail == {"code": "prompt_too_large", "maximum": 32_000}


def test_json_body_limit_rejects_before_endpoint_dispatch() -> None:
    app = FastAPI()
    mutations = []

    @app.post("/mutate")
    async def mutate():
        mutations.append(True)
        return {"ok": True}

    app.add_middleware(
        RequestSecurityMiddleware,
        policy=WebSecurityPolicy(csp_mode="disabled", max_json_body_bytes=16),
    )

    async def scenario():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver",
        ) as client:
            return await client.post(
                "/mutate",
                content=b'{"value":"this is too large"}',
                headers={"Content-Type": "application/json", "Origin": "http://testserver"},
            )

    response = asyncio.run(scenario())
    assert response.status_code == 413
    assert response.json() == {"detail": "JSON request body is too large"}
    assert mutations == []


def test_anonymous_frontends_reject_oversized_prompts_before_fetch() -> None:
    root = Path(__file__).resolve().parents[1]
    chat_source = (root / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")
    embed_source = (root / "ui/embed/embed-chat.js").read_text(encoding="utf-8")

    assert "32000" in chat_source
    assert "100000" in chat_source
    assert "text.length>promptLimit" in chat_source
    assert "32000" in embed_source
    assert "text.length > 32000" in embed_source

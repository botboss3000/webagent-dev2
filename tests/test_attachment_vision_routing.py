"""Regression coverage for agent-aware image attachment routing."""

import asyncio
from unittest.mock import patch

from app.chat.execution import _maybe_describe_images


class _LoopConfig:
    def is_enabled(self, name):
        return name == "attachment_describe"


async def _collect(generator):
    return [event async for event in generator]


def test_active_agent_multimodal_override_keeps_original_image_inline():
    agent = {"id": "agent-sol", "metadata": {"llm_config": {"model": "gpt-5.6-sol"}}}
    image = {
        "id": "image-1",
        "type": "image",
        "mime_type": "image/png",
        "original_name": "photo.png",
    }
    caps = {
        "default": {
            "entry_id": "sol-row",
            "model": "gpt-5.6-sol",
            "provider": "openai_subscription",
            "image_capable": True,
        },
        "racers": [],
        "role_assignments": {},
    }
    seen = {}

    async def load_capabilities(user_id, agent_rec=None):
        seen["user_id"] = user_id
        seen["agent_rec"] = agent_rec
        return caps

    out = {}
    with patch(
        "app.admin.settings.load_llm_capabilities_for_user",
        new=load_capabilities,
    ):
        events = asyncio.run(_collect(_maybe_describe_images(
            object(),
            "user-9",
            "what is in this image?",
            "interaction-1",
            _LoopConfig(),
            [image],
            out,
            agent_id=agent["id"],
            agent_rec=agent,
        )))

    assert seen == {"user_id": "user-9", "agent_rec": agent}
    assert events == []
    assert out["message_text"] == "what is in this image?"
    assert out["inline_docs"] == [image]
    assert out["inline_docs"][0] is image

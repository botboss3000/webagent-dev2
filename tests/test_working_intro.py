import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.working_intro import fallback_intro, generate_working_intro, normalize_intro_result


def test_normalizer_rejects_generic_or_malformed_introductions():
    assert normalize_intro_result({"requires_work": True, "message": "Getting oriented now"}) is None
    assert normalize_intro_result({"requires_work": False, "message": "ignored"}) == {
        "requires_work": False, "task_type": "mixed", "message": "",
    }
    result = normalize_intro_result({
        "requires_work": True,
        "task_type": "research",
        "message": "I’ll inspect the primary sources and compare the evidence before answering.",
    })
    assert result["task_type"] == "research"
    assert "primary sources" in result["message"]


def test_intro_uses_primary_model_and_recent_conversation_without_tools():
    calls = []

    async def completion(**kwargs):
        calls.append(kwargs)
        message = SimpleNamespace(content=(
            '{"requires_work":true,"task_type":"code",'
            '"message":"I’ll inspect the Scout event path and run its focused browser tests."}'
        ))
        usage = SimpleNamespace(prompt_tokens=20, completion_tokens=15)
        return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    db = SimpleNamespace(fetch_interactions=AsyncMock(return_value=[
        SimpleNamespace(id="old-u", role="user", content="Fix the Scout transcript"),
        SimpleNamespace(id="old-a", role="assistant", content="I found the event path"),
        SimpleNamespace(id="current", role="user", content="build it"),
    ]))
    with (
        patch("app.admin.settings.apply_provider_for_run", new=AsyncMock(return_value={
            "model": "provider/primary-model", "provider": "provider",
            "base_url": "https://example.invalid/v1", "api_key": "secret",
        })),
        patch("app.agent.loop._get_client", return_value=client),
        patch("app.agent.loop._record_billing_usage", new=AsyncMock()),
    ):
        result = asyncio.run(generate_working_intro(
            db=db, user_id="u", session_id="s", agent_rec={"id": "a", "metadata": {}},
            user_message="build it", current_interaction_id="current",
            execution_mode="auto", timeout_seconds=5,
        ))

    assert result["status"] == "complete"
    assert result["model"] == "primary-model"
    assert "tools" not in calls[0]
    assert calls[0]["extra_body"] == {"reasoning": {"effort": "low"}}
    assert calls[0]["max_tokens"] == 96
    prompt = calls[0]["messages"][1]["content"]
    assert "Fix the Scout transcript" in prompt
    assert "CURRENT USER REQUEST:\nbuild it" in prompt


def test_intro_timeout_fails_open():
    async def completion(**_kwargs):
        await asyncio.sleep(1)

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=completion)))
    db = SimpleNamespace(fetch_interactions=AsyncMock(return_value=[]))
    with (
        patch("app.admin.settings.apply_provider_for_run", new=AsyncMock(return_value={
            "model": "primary", "provider": "p", "base_url": "https://example.invalid/v1",
            "api_key": "secret",
        })),
        patch("app.agent.loop._get_client", return_value=client),
    ):
        result = asyncio.run(generate_working_intro(
            db=db, user_id="u", session_id="s", agent_rec={"id": "a", "metadata": {}},
            user_message="do work", current_interaction_id="current",
            execution_mode="auto", timeout_seconds=1,
        ))
    assert result["status"] == "complete"
    assert result["fallback"] is True
    assert "do work" in result["message"]


def test_intro_fallback_is_specific_to_code_research_and_action_requests():
    code = fallback_intro("Fix the Scout sequencing bug in the repository")
    research = fallback_intro("Find current sources about the Scout design")
    action = fallback_intro("Send the approved report to the project owner")
    assert code["task_type"] == "code" and "Scout sequencing" in code["message"]
    assert research["task_type"] == "research" and "available sources" in research["message"]
    assert action["task_type"] == "action" and "tools and permissions" in action["message"]


def test_existing_intro_is_reused_after_resume():
    row = SimpleNamespace(
        id="intro-1", role="assistant", parent_id="current", turn_id="current",
        content="I’ll inspect the saved state and continue the verification.",
        metadata='{"message_phase":"progress","progress_kind":"working_intro"}',
    )
    db = SimpleNamespace(fetch_interactions=AsyncMock(return_value=[row]))
    with (
        patch("app.admin.settings.apply_provider_for_run", new=AsyncMock(return_value={
            "model": "primary", "provider": "p", "base_url": "https://example.invalid/v1",
            "api_key": "secret",
        })),
        patch("app.agent.loop._get_client") as client,
    ):
        result = asyncio.run(generate_working_intro(
            db=db, user_id="u", session_id="s", agent_rec={"id": "a", "metadata": {}},
            user_message="continue", current_interaction_id="current",
            execution_mode="auto", timeout_seconds=1,
        ))
    assert result["status"] == "existing"
    assert result["interaction_id"] == "intro-1"
    client.assert_not_called()

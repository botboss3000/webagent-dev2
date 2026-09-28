"""Focused tests for ephemeral voice routing and run-state narration."""

from __future__ import annotations

import subprocess
import asyncio
from pathlib import Path

from app.agent.voice_coordinator import (
    answer_voice_question,
    describe_run_state,
    is_status_question,
)


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "ui" / "shared" / "js" / "voice-conversation-core.js"
VOICE_SETTINGS = ROOT / "ui" / "main-panel" / "agents" / "js" / "voice-conversation-settings.js"
CONVERSATION = ROOT / "ui" / "shared" / "js" / "voice-conversation.js"


def test_browser_voice_routing_has_one_server_authority() -> None:
    source = CONVERSATION.read_text(encoding="utf-8")
    core = CORE.read_text(encoding="utf-8")
    assert "/api/v1/chat/voice/route" in source
    assert "/api/v1/chat/voice/query" not in source
    assert "route?.response_source === 'system'" in source
    assert "if (!route?.issue) _speak(_systemText(text))" in source
    assert "_showCaption(_systemText(text), { kind: 'system'" in source
    assert "System: " in source
    assert "classifyVoiceIntent" not in source + core


def test_voice_speech_text_is_sanitized_for_playback() -> None:
    script = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');

(async () => {
  const { textForSpeech } = await import(moduleUrl);
  assert.equal(textForSpeech('## Done\n- Changed `app.py`'), 'Done Changed app.py');
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script, str(CORE)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_voice_status_answers_are_short_and_deterministic() -> None:
    assert is_status_question("What is it doing right now?")
    assert is_status_question("How is the progress?")
    assert not is_status_question("Why did you choose SQLite?")

    assert describe_run_state(None) == "There is no run for this session yet."
    assert describe_run_state({"status": "running", "current_op": None}) == (
        "The run is active and still working."
    )
    assert describe_run_state({
        "status": "running",
        "current_op": '{"tool":"pytest","note":"Toolcall pytest"}',
    }) == "The run is active and is currently using pytest."
    assert describe_run_state({"status": "complete"}) == (
        "The most recent run is complete."
    )


def test_status_question_reads_run_state_without_writing_interactions() -> None:
    class ReadOnlyDb:
        def __init__(self):
            self.calls = []

        async def run_state_get(self, session_id):
            self.calls.append(("run_state_get", session_id))
            return {"status": "running", "current_op": None}

        def __getattr__(self, name):
            if "insert" in name or "update" in name or "add" in name:
                raise AssertionError(f"unexpected persistence call: {name}")
            raise AttributeError(name)

    db = ReadOnlyDb()
    answer = asyncio.run(answer_voice_question(
        db, "user-1", "session-1", "What is the status?"
    ))
    assert answer == "The run is active and still working."
    assert db.calls == [("run_state_get", "session-1")]


def test_agents_config_exposes_real_voice_conversation_controls() -> None:
    source = VOICE_SETTINGS.read_text(encoding="utf-8")
    tab = (ROOT / "ui/main-panel/agents/js/tab-config.js").read_text(encoding="utf-8")
    api = (ROOT / "app/api/agents.py").read_text(encoding="utf-8")
    assert "Voice Conversation" in tab
    assert "renderVoiceConversationSettings(voiceTopic, agent)" in tab
    for label in (
        "Remember this voice conversation",
        "Include recent text chat",
        "Include live agent events",
        "Session timeout",
        "Response length",
        "Response guidance",
    ):
        assert label in source
    assert "voice_conversation: Optional[Dict[str, Any]]" in api


def test_voice_coordinator_honors_context_and_response_settings(monkeypatch) -> None:
    from types import SimpleNamespace
    from app.agent import voice_coordinator

    calls = {}

    class Completions:
        async def create(self, **kwargs):
            calls.update(kwargs)
            return SimpleNamespace(
                choices=[SimpleNamespace(message=SimpleNamespace(content="Configured answer"))],
                usage=None,
            )

    client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
    monkeypatch.setattr(voice_coordinator, "_openai_client", lambda config: (client, "voice-model"))

    class Db:
        async def run_state_get(self, session_id):
            return None

    reply = asyncio.run(voice_coordinator.answer_voice_question_result(
        Db(), "user-1", "session-1", "Explain the architecture",
        voice_session={
            "durable_context": [{"role": "user", "content": "durable secret"}],
            "conversation_summary": "prior voice turn",
            "recent_agent_events": [{"type": "tool_call", "tool": "pytest"}],
            "conversation_config": {
                "continuous_context": False,
                "include_durable_chat": False,
                "include_agent_events": False,
                "response_length": "detailed",
                "response_instructions": "Use plain language.",
            },
        },
        llm_config={"model": "unused", "base_url": "unused", "api_key": "unused"},
    ))

    assert reply.text == "Configured answer"
    assert reply.source == "model"
    assert calls["max_tokens"] == 500
    user_prompt = calls["messages"][1]["content"]
    assert "Recent durable chat" not in user_prompt
    assert "Current ephemeral voice exchange" not in user_prompt
    assert "Recent agent events" not in user_prompt
    system_prompt = calls["messages"][0]["content"]
    assert "two short paragraphs" in system_prompt
    assert "Use plain language." in system_prompt

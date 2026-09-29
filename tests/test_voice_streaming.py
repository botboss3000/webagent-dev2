"""Streaming voice transport, server VAD, and partial transcript coverage."""

from __future__ import annotations

import asyncio
import json
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import transcription
from app.api.transcription import PcmVoiceActivityDetector, pcm16_rms, pcm16_wav


ROOT = Path(__file__).resolve().parents[1]
STREAM_JS = ROOT / "ui" / "shared" / "js" / "voice-stream.js"
MIC_DIAGNOSTICS_JS = ROOT / "ui" / "shared" / "js" / "microphone-diagnostics.js"


def test_voice_authorization_creates_a_brand_new_chat_session(monkeypatch) -> None:
    class FakeDb:
        def __init__(self) -> None:
            self.exists = False
            self.bound_agent_id = None

        async def assert_session_owned(self, user_id: str, session_id: str) -> None:
            assert (user_id, session_id) == ("user-1", "chat-new")
            if not self.exists:
                raise PermissionError("missing")

        async def is_session_participant(
            self, session_id: str, user_id: str, role: str,
        ) -> bool:
            assert (session_id, user_id, role) == ("chat-new", "user-1", "user")
            return False

        async def get_session_agent_id(self, session_id: str):
            assert session_id == "chat-new"
            return self.bound_agent_id

        async def bind_session_to_agent(self, session_id: str, agent_id: str) -> None:
            assert (session_id, agent_id) == ("chat-new", "agent-1")
            self.bound_agent_id = agent_id

    db = FakeDb()

    async def ensure_session(received_db, user_id, session_id, title=None):
        assert (received_db, user_id, session_id, title) == (
            db, "user-1", "chat-new", None,
        )
        db.exists = True
        return True

    from app import db as db_module
    from app.admin import settings
    from app.chat import execution as chat
    from app.entitlements import service

    async def capabilities(_user_id, *, db):
        return {"features": {"voice_llm": True}}

    async def require_agent(received_db, agent_id, user_id):
        assert (received_db, agent_id, user_id) == (db, "agent-1", "user-1")
        return {"id": agent_id}

    monkeypatch.setattr(chat, "_ensure_session", ensure_session)
    monkeypatch.setattr(chat, "_require_chat_agent_access", require_agent)
    monkeypatch.setattr(db_module, "get_db", lambda: db)
    monkeypatch.setattr(settings, "get_access_mode", lambda: "open")
    monkeypatch.setattr(service, "resolve_capabilities", capabilities)
    monkeypatch.setattr(
        transcription, "get_voice_dictation_config",
        lambda: {"llm_enabled": True},
    )

    authorized = asyncio.run(transcription._authorize_voice_websocket(
        object(), "user-1", "", "chat-new", "agent-1",
    ))
    assert authorized == "user-1"
    assert db.exists is True
    assert db.bound_agent_id == "agent-1"


def test_voice_authorization_rejects_switching_a_bound_session_agent(
    monkeypatch,
) -> None:
    class FakeDb:
        async def assert_session_owned(self, _user_id, _session_id):
            return None

        async def get_session_agent_id(self, session_id):
            assert session_id == "chat-1"
            return "agent-1"

    async def require_agent(_db, agent_id, user_id):
        assert (agent_id, user_id) == ("agent-2", "user-1")
        return {"id": agent_id}

    from app.chat import execution as chat
    monkeypatch.setattr(chat, "_require_chat_agent_access", require_agent)

    with pytest.raises(PermissionError, match="different agent"):
        asyncio.run(transcription._ensure_voice_chat_session(
            FakeDb(), "user-1", "chat-1", "agent-2",
        ))



def _pcm(amplitude: int, milliseconds: int, sample_rate: int = 16_000) -> bytes:
    count = int(sample_rate * milliseconds / 1000)
    return struct.pack(f"<{count}h", *([amplitude] * count))


def test_session_agent_voice_roster_overrides_empty_user_roster(monkeypatch) -> None:
    class FakeDb:
        async def assert_session_owned(self, user_id: str, session_id: str) -> None:
            assert (user_id, session_id) == ("user-1", "session-1")

        async def get_session_agent_id(self, session_id: str) -> str:
            assert session_id == "session-1"
            return "agent-1"

        async def get_agent_by_id(self, agent_id: str) -> dict:
            assert agent_id == "agent-1"
            return {"id": agent_id, "metadata": {"llm_config": {}}}

    agent_roster = {
        "multi_providers": [{
            "model": "agent-voice-model", "provider": "agent-voice-provider",
            "base_url": "https://agent-voice.example/v1", "api_key": "agent-voice-key",
            "enabled": True, "voice_capable": True, "use_for_voice": True,
        }],
    }

    async def hydrate(_agent: dict) -> dict:
        return {"id": "agent-1", "metadata": {"llm_config": agent_roster}}

    async def user_config(*_args, **_kwargs) -> dict:
        return {}

    from app import db as db_module
    monkeypatch.setattr(db_module, "get_db", lambda: FakeDb())
    monkeypatch.setattr(transcription, "_hydrate_agent_record_llm", hydrate)
    monkeypatch.setattr(transcription, "_resolve_user_config", user_config)

    config = asyncio.run(transcription._resolve_transcription_config("user-1", "session-1"))
    assert config["model"] == "agent-voice-model"
    assert config["provider"] == "agent-voice-provider"
    assert config["base_url"] == "https://agent-voice.example/v1"
    assert config["api_key"] == "agent-voice-key"


def test_voice_runtime_config_uses_the_voice_roster_entry(monkeypatch) -> None:
    from app.admin import settings

    roster = {
        "multi_providers": [
            {
                "model": "standard-model",
                "provider": "standard-provider",
                "base_url": "https://standard.example/v1",
                "api_key": "standard-key",
                "enabled": True,
                "text_capable": True,
            },
            {
                "model": "voice-model",
                "provider": "voice-provider",
                "base_url": "https://voice.example/v1",
                "api_key": "voice-key",
                # Standard and Voice are independent roster roles. A model
                # assigned only to Voice is intentionally not Standard-enabled.
                "enabled": False,
                "voice_capable": True,
                "use_for_voice": True,
            },
        ],
    }

    async def own_config(_user_id: str) -> dict:
        return roster

    monkeypatch.setattr(settings, "_load_own_llm_config", own_config)
    config = asyncio.run(settings._resolve_user_config("user-1", role="voice"))

    assert config["model"] == "voice-model"
    assert config["provider"] == "voice-provider"
    assert config["base_url"] == "https://voice.example/v1"
    assert config["api_key"] == "voice-key"


def test_pcm_vad_detects_speech_and_silence_turn_boundary() -> None:
    detector = PcmVoiceActivityDetector()
    assert pcm16_rms(_pcm(0, 100)) == 0.0
    assert pcm16_rms(_pcm(12_000, 100)) > detector.speech_threshold
    assert detector.feed(_pcm(12_000, 100)) is None
    assert detector.feed(_pcm(12_000, 100)) == "speech_start"
    for _ in range(6):
        assert detector.feed(_pcm(0, 100)) is None
    assert detector.feed(_pcm(0, 100)) == "turn_end"
    wav = pcm16_wav(_pcm(1_000, 100))
    assert wav[:4] == b"RIFF"
    assert wav[8:12] == b"WAVE"


def test_voice_stream_websocket_emits_partial_and_final_transcripts(monkeypatch) -> None:
    async def authorize(*_args, **_kwargs):
        return "user-1"

    async def transcribe(_user_id, contents, mime_type, _language="", _session_id=""):
        assert mime_type == "audio/wav"
        assert contents[:4] == b"RIFF"
        return "live partial transcript"

    monkeypatch.setattr(transcription, "_authorize_voice_websocket", authorize)
    monkeypatch.setattr(transcription, "_transcribe_bytes", transcribe)
    app = FastAPI()
    app.include_router(transcription.router)

    with TestClient(app) as client:
        with client.websocket_connect("/api/v1/transcribe/stream") as websocket:
            websocket.send_text(json.dumps({
                "mode": "voice_stream",
                "user_id": "user-1",
                "session_id": "session-1",
                "token": "test",
                "sample_rate": 16_000,
                "language": "en-US",
            }))
            ready = websocket.receive_json()
            assert ready["type"] == "ready"
            assert ready["voice_session_id"]
            assert ready["last_agent_event_cursor"] >= 0

            websocket.send_bytes(_pcm(12_000, 100))
            websocket.send_bytes(_pcm(12_000, 100))
            assert websocket.receive_json()["type"] == "speech_start"
            for _ in range(12):
                websocket.send_bytes(_pcm(12_000, 100))

            partial = websocket.receive_json()
            assert partial == {
                "type": "transcript",
                "text": "live partial transcript",
                "final": False,
                "turn_id": 1,
                "revision": 1,
            }

            for _ in range(7):
                websocket.send_bytes(_pcm(0, 100))
            assert websocket.receive_json() == {"type": "turn_end", "turn_id": 1}
            final = websocket.receive_json()
            assert final["type"] == "transcript"
            assert final["text"] == "live partial transcript"
            assert final["final"] is True
            assert final["turn_id"] == 1
            from app.agent.voice_session import get_voice_session_registry
            ephemeral = get_voice_session_registry().get(
                ready["voice_session_id"], touch=False,
            )
            assert ephemeral is not None
            assert "live partial transcript" in ephemeral.conversation_summary


def test_browser_pcm_downsampling_is_bounded_and_little_endian() -> None:
    script = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.replace(/import \{ voiceStreamWsUrl \} from[^;]+;/, '');
source = source.replace(/import \{ getAuthToken \} from[^;]+;/, '');
source = source.replace(/import\s*\{[^}]+\}\s*from '.\/microphone-diagnostics\.js';/, '');
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');

(async () => {
  const { downsampleToPcm16 } = await import(moduleUrl);
  const input = new Float32Array(4800);
  input.fill(0.5);
  const buffer = downsampleToPcm16(input, 48000, 16000);
  const output = new Int16Array(buffer);
  assert.equal(output.length, 1600);
  assert.equal(output[0] >= 16383 && output[0] <= 16384, true);
  input.fill(-2);
  const clipped = new Int16Array(downsampleToPcm16(input, 48000, 16000));
  assert.equal(clipped[0], -32768);
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script, str(STREAM_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_browser_stream_transport_handshake_barge_in_and_reconnect() -> None:
    script = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
let source = fs.readFileSync(process.argv[1], 'utf8');
source = source.replace(/import \{ voiceStreamWsUrl \} from[^;]+;/, '');
source = source.replace(/import \{ getAuthToken \} from[^;]+;/, '');
source = source.replace(/import\s*\{[^}]+\}\s*from '.\/microphone-diagnostics\.js';/, '');
source = `const voiceStreamWsUrl = () => 'ws://voice'; const getAuthToken = () => 'token';
const microphoneAccessMessage = async () => ''; const requestMicrophoneAccess = constraints => navigator.mediaDevices.getUserMedia(constraints);
${source}`;

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) { this.url = url; this.readyState = 0; this.sent = []; sockets.push(this); }
  send(value) { this.sent.push(value); }
  close() { this.readyState = 3; }
}
class FakeAudioContext {
  constructor() { this.sampleRate = 48000; this.state = 'running'; this.destination = {}; }
  createMediaStreamSource() { return { connect() {}, disconnect() {} }; }
  createScriptProcessor() {
    this.processor = { connect() {}, disconnect() {}, onaudioprocess: null };
    return this.processor;
  }
  createGain() { return { gain: { value: 1 }, connect() {}, disconnect() {} }; }
  close() { this.state = 'closed'; }
}
const stopped = [];
Object.defineProperty(globalThis, 'window', { value: {
  isSecureContext: true,
  WebSocket: FakeWebSocket,
  AudioContext: FakeAudioContext,
}});
Object.defineProperty(globalThis, 'WebSocket', { value: FakeWebSocket });
Object.defineProperty(globalThis, 'navigator', { value: {
  language: 'en-US',
  mediaDevices: { getUserMedia: async () => ({ getTracks: () => [{ stop: () => stopped.push(true) }] }) },
}});
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');

(async () => {
  const { StreamingVoiceTransport } = await import(moduleUrl);
  const states = [], partials = [], completed = [];
  let barges = 0;
  const transport = new StreamingVoiceTransport();
  assert.equal(await transport.start({
    userId: 'user-1', sessionId: 'session-1', agentId: 'agent-1',
    onState: value => states.push(value),
    onPartial: value => partials.push(value),
    onComplete: value => completed.push(value),
    onSpeechStart: () => barges++,
  }), true);
  const first = sockets[0];
  first.readyState = FakeWebSocket.OPEN;
  first.onopen();
  const handshake = JSON.parse(first.sent[0]);
  assert.equal(handshake.mode, 'voice_stream');
  assert.equal(handshake.token, 'token');
  assert.equal(handshake.agent_id, 'agent-1');
  first.onmessage({ data: JSON.stringify({ type: 'ready', voice_session_id: 'voice-1' }) });
  assert.equal(handshake.voice_session_id, undefined);
  first.onmessage({ data: JSON.stringify({ type: 'speech_start', turn_id: 1 }) });
  first.onmessage({ data: JSON.stringify({ type: 'transcript', text: 'hello', final: false, turn_id: 1, revision: 1 }) });
  assert.equal(barges, 1);
  assert.deepEqual(partials, ['hello']);

  // A dropped live socket reconnects while capture remains active.
  first.readyState = 3;
  first.onclose();
  await new Promise(resolve => setTimeout(resolve, 380));
  assert.equal(sockets.length, 2);
  const second = sockets[1];
  second.readyState = FakeWebSocket.OPEN;
  second.onopen();
  assert.equal(JSON.parse(second.sent[0]).voice_session_id, 'voice-1');
  second.onmessage({ data: JSON.stringify({ type: 'ready', voice_session_id: 'voice-1' }) });
  second.onmessage({ data: JSON.stringify({ type: 'transcript', text: 'hello world', final: true, turn_id: 1, revision: 2 }) });
  assert.deepEqual(completed, ['hello world']);
  assert.equal(transport.active, false);
  assert.equal(states.includes('reconnecting'), true);
  assert.equal(stopped.length > 0, true);

  // A rejected stale session ID is cleared and retried once without an ID.
  const retryTransport = new StreamingVoiceTransport();
  assert.equal(await retryTransport.start({
    userId: 'user-2', sessionId: 'session-2', voiceSessionId: 'stale-session',
  }), true);
  const stale = sockets[2];
  stale.readyState = FakeWebSocket.OPEN;
  stale.onopen();
  assert.equal(JSON.parse(stale.sent[0]).voice_session_id, 'stale-session');
  stale.onmessage({ data: JSON.stringify({ type: 'error', code: 'voice_session_mismatch' }) });
  assert.equal(sockets.length, 4);
  const fresh = sockets[3];
  fresh.readyState = FakeWebSocket.OPEN;
  fresh.onopen();
  assert.equal(JSON.parse(fresh.sent[0]).voice_session_id, undefined);
  fresh.onmessage({ data: JSON.stringify({ type: 'error', code: 'voice_session_mismatch' }) });
  assert.equal(retryTransport.active, false);
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script, str(STREAM_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_microphone_access_checks_permission_before_requesting_stream() -> None:
    script = r"""
const fs = require('node:fs');
const assert = require('node:assert/strict');
const source = fs.readFileSync(process.argv[1], 'utf8');
const moduleUrl = 'data:text/javascript;base64,' + Buffer.from(source).toString('base64');
const events = [];
const stream = { getTracks: () => [] };
let permissionState = 'prompt';
Object.defineProperty(globalThis, 'navigator', { value: {
  permissions: { query: async ({ name }) => {
    events.push(`query:${name}`);
    return { state: permissionState };
  }},
  mediaDevices: { getUserMedia: async constraints => {
    events.push('request');
    assert.deepEqual(constraints, { audio: true });
    return stream;
  }},
}});

(async () => {
  const { requestMicrophoneAccess } = await import(moduleUrl);
  assert.equal(await requestMicrophoneAccess({ audio: true }), stream);
  assert.deepEqual(events, ['query:microphone', 'request']);

  permissionState = 'denied';
  events.length = 0;
  await assert.rejects(
    requestMicrophoneAccess(),
    error => error.name === 'NotAllowedError',
  );
  assert.deepEqual(events, ['query:microphone']);
})().catch(error => {
  console.error(error);
  process.exit(1);
});
"""
    completed = subprocess.run(
        ["node", "-e", script, str(MIC_DIAGNOSTICS_JS)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

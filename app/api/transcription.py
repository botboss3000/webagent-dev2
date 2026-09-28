"""Audio-input transport for the parallel voice conversation lane."""

from __future__ import annotations

import base64
import asyncio
import io
import json
import logging
import math
import os
import struct
import wave
from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect

from app.admin.settings import (
    _hydrate_agent_record_llm,
    _resolve_user_config,
    _runtime_config_from_roster,
    get_voice_dictation_config,
)


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/transcribe", tags=["transcription"])

_MAX_AUDIO_BYTES = 10 * 1024 * 1024
_VOICE_SAMPLE_RATE = 16_000
_AUDIO_FORMATS = {
    "audio/webm": "webm",
    "audio/ogg": "ogg",
    "audio/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/flac": "flac",
}


def _provider_error(response: httpx.Response) -> str:
    try:
        body = response.json()
        detail: Any = body.get("error") or body.get("detail") or body
        if isinstance(detail, dict):
            detail = detail.get("message") or detail.get("detail") or detail
        text = str(detail)
    except Exception:
        text = response.text or response.reason_phrase
    return text[:240]


def pcm16_rms(contents: bytes) -> float:
    """Return normalized RMS for little-endian signed 16-bit mono PCM."""
    sample_count = len(contents) // 2
    if sample_count <= 0:
        return 0.0
    samples = struct.unpack(f"<{sample_count}h", contents[:sample_count * 2])
    mean_square = sum(sample * sample for sample in samples) / sample_count
    return math.sqrt(mean_square) / 32768.0


def pcm16_wav(contents: bytes, sample_rate: int = _VOICE_SAMPLE_RATE) -> bytes:
    """Wrap streamed PCM in a provider-compatible in-memory WAV container."""
    target = io.BytesIO()
    with wave.open(target, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(contents)
    return target.getvalue()


@dataclass
class PcmVoiceActivityDetector:
    """Small server-side VAD state machine tuned for conversational speech."""

    sample_rate: int = _VOICE_SAMPLE_RATE
    speech_threshold: float = 0.018
    speech_start_ms: float = 120.0
    silence_end_ms: float = 700.0
    max_turn_ms: float = 30_000.0
    speaking: bool = False
    voiced_ms: float = 0.0
    silence_ms: float = 0.0
    turn_ms: float = 0.0

    def feed(self, contents: bytes) -> str | None:
        duration_ms = (len(contents) / 2 / self.sample_rate) * 1000.0
        if duration_ms <= 0:
            return None
        voiced = pcm16_rms(contents) >= self.speech_threshold
        if not self.speaking:
            self.voiced_ms = self.voiced_ms + duration_ms if voiced else 0.0
            if self.voiced_ms >= self.speech_start_ms:
                self.speaking = True
                self.turn_ms = self.voiced_ms
                self.silence_ms = 0.0
                return "speech_start"
            return None

        self.turn_ms += duration_ms
        self.silence_ms = 0.0 if voiced else self.silence_ms + duration_ms
        if self.silence_ms >= self.silence_end_ms or self.turn_ms >= self.max_turn_ms:
            return "turn_end"
        return None

    def reset(self) -> None:
        self.speaking = False
        self.voiced_ms = 0.0
        self.silence_ms = 0.0
        self.turn_ms = 0.0


async def _resolve_transcription_config(user_id: str, session_id: str = "") -> dict:
    """Resolve the current chat agent's Voice roster before user-level fallback."""
    if session_id:
        try:
            from app.db import get_db

            db = get_db()
            try:
                await db.assert_session_owned(user_id, session_id)
            except PermissionError:
                if not await db.is_session_participant(session_id, user_id, "user"):
                    raise
            agent_id = await db.get_session_agent_id(session_id)
            if agent_id:
                agent = await db.get_agent_by_id(agent_id)
                hydrated = await _hydrate_agent_record_llm(agent)
                metadata = (hydrated or {}).get("metadata") or {}
                if isinstance(metadata, str):
                    metadata = json.loads(metadata or "{}")
                agent_config = metadata.get("llm_config") if isinstance(metadata, dict) else None
                if isinstance(agent_config, dict):
                    voice_config = _runtime_config_from_roster(agent_config, role="voice")
                    if voice_config.get("model"):
                        return voice_config
        except PermissionError:
            raise
        except Exception:
            logger.warning("Could not resolve the session agent Voice roster", exc_info=True)
    return await _resolve_user_config(user_id, role="voice")


async def _openrouter_audio_route(model: str) -> str:
    """Choose the OpenRouter endpoint from the model's declared modalities."""
    try:
        from app import model_catalog

        await model_catalog.ensure_fresh()
        metadata = model_catalog.lookup(model, provider_hint="openrouter") or {}
        inputs = {str(value).lower() for value in metadata.get("input_modalities") or []}
        outputs = {str(value).lower() for value in metadata.get("output_modalities") or []}
        if "transcription" in outputs:
            return "transcription"
        if "audio" in inputs:
            return "chat"
    except Exception:
        logger.debug("Could not resolve OpenRouter audio modalities", exc_info=True)
    return "transcription"


def _chat_completion_text(result: dict) -> str:
    """Extract text from an OpenAI-compatible chat completion."""
    choices = result.get("choices") or []
    message = (choices[0].get("message") or {}) if choices else {}
    content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return "".join(
            str(item.get("text") or "") for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ).strip()
    return ""


async def _transcribe_bytes(
    user_id: str,
    contents: bytes,
    mime_type: str,
    language: str = "",
    session_id: str = "",
) -> str:
    """Convert one audio turn to text with the current chat agent's Voice model."""
    audio_format = _AUDIO_FORMATS.get(mime_type)
    if not audio_format:
        raise HTTPException(status_code=400, detail=f"Unsupported audio type: {mime_type or 'unknown'}")
    if not contents:
        raise HTTPException(status_code=400, detail="The recording was empty")
    if len(contents) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large (10MB maximum)")

    config = await _resolve_transcription_config(user_id, session_id)
    provider = str(config.get("provider") or "").lower()
    base_url = str(config.get("base_url") or "").rstrip("/")
    api_key = str(config.get("api_key") or "")
    if not base_url or not api_key:
        raise HTTPException(
            status_code=503,
            detail="Configure a live Voice model before starting a voice conversation",
        )

    is_openrouter = provider == "openrouter" or "openrouter.ai" in base_url
    endpoint = f"{base_url}/audio/transcriptions"
    headers = {"Authorization": f"Bearer {api_key}"}
    language_code = (language or "").strip().lower().split("-", 1)[0][:3]
    openrouter_route = "transcription"

    try:
        async with httpx.AsyncClient(timeout=75.0) as client:
            if is_openrouter:
                model = str(
                    config.get("model")
                    or os.environ.get("VOICE_TRANSCRIPTION_MODEL")
                    or "openai/whisper-large-v3"
                )
                openrouter_route = await _openrouter_audio_route(model)
                encoded_audio = base64.b64encode(contents).decode("ascii")
                if openrouter_route == "chat":
                    endpoint = f"{base_url}/chat/completions"
                    instruction = (
                        "Transcribe the attached audio verbatim. Return only the "
                        "transcript with no commentary."
                    )
                    if language_code:
                        instruction += f" The spoken language is {language_code}."
                    payload: dict[str, Any] = {
                        "model": model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": instruction},
                                {
                                    "type": "input_audio",
                                    "input_audio": {
                                        "data": encoded_audio,
                                        "format": audio_format,
                                    },
                                },
                            ],
                        }],
                    }
                else:
                    payload = {
                        "model": model,
                        "input_audio": {
                            "data": encoded_audio,
                            "format": audio_format,
                        },
                    }
                    if language_code:
                        payload["language"] = language_code
                response = await client.post(endpoint, json=payload, headers=headers)
            else:
                model = str(
                    config.get("model")
                    or os.environ.get("VOICE_TRANSCRIPTION_MODEL")
                    or "gpt-4o-mini-transcribe"
                )
                data = {"model": model}
                if language_code:
                    data["language"] = language_code
                response = await client.post(
                    endpoint,
                    data=data,
                    files={"file": (f"voice.{audio_format}", contents, mime_type)},
                    headers=headers,
                )
    except httpx.RequestError as exc:
        logger.warning("Voice transcription request failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not reach the Voice input provider") from exc

    if response.status_code >= 400:
        provider_detail = _provider_error(response)
        logger.warning("Voice input provider returned %s: %s", response.status_code, provider_detail)
        raise HTTPException(
            status_code=502,
            detail=f"Voice input provider rejected the recording: {provider_detail}",
        )
    try:
        result = response.json()
        if is_openrouter and openrouter_route == "chat":
            return _chat_completion_text(result)
        return str(result.get("text") or "").strip()
    except Exception as exc:
        raise HTTPException(status_code=502, detail="Voice input provider returned an invalid response") from exc


@router.post("")
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),
    user_id: str = Form(...),
    language: str = Form(""),
    session_id: str = Form(""),
):
    """Transcribe a short composer recording with the configured AI provider."""
    from app.auth.identity import assert_caller_is
    from app.db import get_db
    from app.entitlements.service import resolve_capabilities

    user_id = await assert_caller_is(request, user_id)
    capabilities = await resolve_capabilities(user_id, db=get_db())
    if not (capabilities.get("features") or {}).get("voice_llm"):
        raise HTTPException(
            status_code=403,
            detail={"code": "upgrade_required", "feature": "voice_llm"},
        )
    if not get_voice_dictation_config()["llm_enabled"]:
        raise HTTPException(status_code=403, detail="LLM voice dictation is disabled by the administrator")

    mime_type = (file.content_type or "").split(";", 1)[0].lower()
    audio_format = _AUDIO_FORMATS.get(mime_type)
    if not audio_format:
        raise HTTPException(status_code=400, detail=f"Unsupported audio type: {mime_type or 'unknown'}")

    contents = await file.read(_MAX_AUDIO_BYTES + 1)
    if not contents:
        raise HTTPException(status_code=400, detail="The recording was empty")
    if len(contents) > _MAX_AUDIO_BYTES:
        raise HTTPException(status_code=413, detail="Recording is too large (10MB maximum)")

    text = await _transcribe_bytes(user_id, contents, mime_type, language, session_id)
    if not text:
        raise HTTPException(status_code=422, detail="No speech was detected in the recording")
    return {"text": text}


async def _ensure_voice_chat_session(
    db: Any,
    user_id: str,
    session_id: str,
    agent_id: str = "",
) -> None:
    """Authorize the chat and bind its selected agent for voice-first sessions."""
    has_access = False
    try:
        await db.assert_session_owned(user_id, session_id)
        has_access = True
    except PermissionError:
        try:
            has_access = await db.is_session_participant(
                session_id, user_id, "user",
            )
        except Exception:
            has_access = False

    if not has_access:
        # The browser creates a session ID before the first durable message.
        # Voice can be that first interaction, so use the same creation path as
        # text chat and verify ownership again before authorizing the socket.
        from app.api.chat import _ensure_session

        try:
            await _ensure_session(db, user_id, session_id)
            await db.assert_session_owned(user_id, session_id)
        except PermissionError as exc:
            raise PermissionError("Voice session was not found") from exc

    requested_agent_id = str(agent_id or "").strip()
    if not requested_agent_id:
        return

    from app.api.chat import _require_chat_agent_access

    try:
        agent = await _require_chat_agent_access(
            db, requested_agent_id, user_id,
        )
    except HTTPException as exc:
        raise PermissionError("Voice agent was not found") from exc
    resolved_agent_id = str((agent or {}).get("id") or "")
    if not resolved_agent_id:
        raise PermissionError("Voice agent was not found")

    existing_agent_id = await db.get_session_agent_id(session_id)
    if existing_agent_id and existing_agent_id != resolved_agent_id:
        raise PermissionError("Voice session belongs to a different agent")
    if not existing_agent_id:
        await db.bind_session_to_agent(session_id, resolved_agent_id)


async def _authorize_voice_websocket(
    websocket: WebSocket,
    claimed_user_id: str,
    token: str,
    session_id: str,
    agent_id: str = "",
) -> str:
    """Authenticate a voice socket and verify access to its durable chat session."""
    from app.admin.settings import get_access_mode
    from app.db import get_db
    from app.entitlements.service import resolve_capabilities

    if get_access_mode() == "open":
        user_id = claimed_user_id
    else:
        from app.auth.identity import verify_token_matches_user

        user_id = verify_token_matches_user(token, claimed_user_id) or ""
        if not user_id:
            raise PermissionError("Invalid or missing voice-session credentials")
        if user_id.startswith("anon_"):
            from app.api.rate_limit import client_source_hash
            from app.auth.jwt import decode_token

            payload = decode_token(token) or {}
            if (
                payload.get("anon_admission") is not True
                or payload.get("anon_source") != client_source_hash(websocket)
            ):
                raise PermissionError("Anonymous voice-session admission expired")

    try:
        from app.auth.identity import set_verified_caller_uid
        set_verified_caller_uid(user_id)
    except Exception:  # noqa: BLE001
        pass

    db = get_db()
    await _ensure_voice_chat_session(db, user_id, session_id, agent_id)

    capabilities = await resolve_capabilities(user_id, db=db)
    if not (capabilities.get("features") or {}).get("voice_llm"):
        raise PermissionError("Voice streaming is not available for this account")
    if not get_voice_dictation_config()["llm_enabled"]:
        raise PermissionError("LLM voice dictation is disabled by the administrator")
    return user_id


@router.websocket("/stream")
async def stream_voice_audio(websocket: WebSocket) -> None:
    """Stream PCM16 audio, detect turns server-side, and emit partial transcripts."""
    await websocket.accept()
    send_lock = asyncio.Lock()
    transcription_tasks: set[asyncio.Task[Any]] = set()
    finalizing_turns: set[int] = set()

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload))

    try:
        raw_handshake = await asyncio.wait_for(websocket.receive_text(), timeout=10.0)
        handshake = json.loads(raw_handshake)
        claimed_user_id = str(handshake.get("user_id") or "").strip()
        session_id = str(handshake.get("session_id") or "").strip()
        token = str(handshake.get("token") or "").strip()
        agent_id = str(handshake.get("agent_id") or "").strip()
        if handshake.get("mode") != "voice_stream" or not claimed_user_id or not session_id:
            await send({"type": "error", "code": "invalid_handshake", "message": "Invalid voice-stream handshake"})
            await websocket.close(code=4400)
            return

        try:
            sample_rate = int(handshake.get("sample_rate") or _VOICE_SAMPLE_RATE)
        except (TypeError, ValueError):
            sample_rate = 0
        if sample_rate < 8_000 or sample_rate > 48_000:
            await send({"type": "error", "code": "invalid_sample_rate", "message": "Unsupported voice sample rate"})
            await websocket.close(code=4400)
            return
        language = str(handshake.get("language") or "")[:16]

        try:
            user_id = await _authorize_voice_websocket(
                websocket, claimed_user_id, token, session_id, agent_id,
            )
        except PermissionError as exc:
            await send({"type": "error", "code": "voice_access_denied", "message": str(exc)})
            await websocket.close(code=4403)
            return

        from app.agent.voice_session import get_voice_session_registry
        from app.agent.voice_config import voice_conversation_for_session
        from app.db import get_db

        voice_db = get_db()
        conversation_config = await voice_conversation_for_session(voice_db, session_id)

        voice_registry = get_voice_session_registry()
        requested_voice_session_id = str(handshake.get("voice_session_id") or "").strip() or None
        try:
            voice_session = await voice_registry.get_or_create(
                user_id,
                session_id,
                voice_session_id=requested_voice_session_id,
                db=voice_db,
                config=conversation_config,
            )
        except PermissionError:
            await send({"type": "error", "code": "voice_session_mismatch", "message": "Voice session needs to be refreshed"})
            await websocket.close(code=4403)
            return

        detector = PcmVoiceActivityDetector(sample_rate=sample_rate)
        pre_roll = bytearray()
        turn_audio = bytearray()
        turn_id = 0
        partial_revision = 0
        next_partial_ms = 1_400.0
        pre_roll_limit = int(sample_rate * 2 * 0.45)

        async def transcribe_snapshot(
            pcm: bytes,
            target_turn: int,
            revision: int,
            final: bool,
        ) -> None:
            try:
                text = await _transcribe_bytes(
                    user_id, pcm16_wav(pcm, sample_rate), "audio/wav", language, session_id,
                )
                if not final and target_turn in finalizing_turns:
                    return
                if text:
                    if final:
                        voice_registry.record_utterance(
                            voice_session.voice_session_id,
                            text,
                        )
                    await send({
                        "type": "transcript",
                        "text": text,
                        "final": final,
                        "turn_id": target_turn,
                        "revision": revision,
                    })
                elif final:
                    await send({"type": "no_speech", "turn_id": target_turn})
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if final:
                    detail = exc.detail if isinstance(exc, HTTPException) else str(exc)
                    await send({
                        "type": "error",
                        "code": "transcription_failed",
                        "message": str(detail or "Voice transcription failed")[:240],
                    })

        def launch_transcription(pcm: bytes, final: bool) -> None:
            nonlocal partial_revision
            partial_revision += 1
            task = asyncio.create_task(
                transcribe_snapshot(pcm, turn_id, partial_revision, final),
            )
            transcription_tasks.add(task)
            task.add_done_callback(transcription_tasks.discard)

        async def finish_turn() -> None:
            nonlocal turn_audio, next_partial_ms
            if not detector.speaking or not turn_audio:
                detector.reset()
                turn_audio = bytearray()
                await send({"type": "no_speech", "turn_id": turn_id})
                return
            snapshot = bytes(turn_audio)
            finalizing_turns.add(turn_id)
            await send({"type": "turn_end", "turn_id": turn_id})
            launch_transcription(snapshot, final=True)
            detector.reset()
            turn_audio = bytearray()
            next_partial_ms = 1_400.0

        await send({
            "type": "ready",
            "sample_rate": sample_rate,
            "vad": {"silence_ms": detector.silence_end_ms},
            "voice_session_id": voice_session.voice_session_id,
            "expires_at": voice_session.public_snapshot()["expires_at"],
            "last_agent_event_cursor": voice_session.last_agent_event_cursor,
        })

        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            text_message = message.get("text")
            if text_message is not None:
                try:
                    control = json.loads(text_message)
                except json.JSONDecodeError:
                    continue
                control_type = control.get("type")
                if control_type == "stop":
                    await finish_turn()
                elif control_type == "cancel":
                    return
                elif control_type == "ping":
                    await send({"type": "pong"})
                continue

            chunk = message.get("bytes") or b""
            if not chunk:
                continue
            if len(chunk) > 256 * 1024 or len(chunk) % 2:
                await send({"type": "error", "code": "invalid_audio", "message": "Invalid PCM audio frame"})
                continue

            was_speaking = detector.speaking
            event = detector.feed(chunk)
            if was_speaking:
                turn_audio.extend(chunk)
            else:
                pre_roll.extend(chunk)
                if len(pre_roll) > pre_roll_limit:
                    del pre_roll[:-pre_roll_limit]
                if event == "speech_start":
                    turn_id += 1
                    partial_revision = 0
                    turn_audio = bytearray(pre_roll)
                    pre_roll.clear()
                    await send({"type": "speech_start", "turn_id": turn_id})

            if detector.speaking and len(turn_audio) > _MAX_AUDIO_BYTES:
                await finish_turn()
                continue
            if event == "turn_end":
                await finish_turn()
                continue
            if detector.speaking:
                duration_ms = (len(turn_audio) / 2 / sample_rate) * 1000.0
                if duration_ms >= next_partial_ms:
                    launch_transcription(bytes(turn_audio), final=False)
                    next_partial_ms += 2_000.0

    except (WebSocketDisconnect, asyncio.TimeoutError):
        pass
    except json.JSONDecodeError:
        try:
            await send({"type": "error", "code": "invalid_json", "message": "Invalid voice-stream message"})
        except Exception:  # noqa: BLE001
            pass
    except Exception as exc:  # noqa: BLE001
        logger.warning("Voice stream failed: %s", exc, exc_info=True)
        try:
            await send({"type": "error", "code": "stream_failed", "message": "The voice connection failed"})
        except Exception:  # noqa: BLE001
            pass
    finally:
        for task in tuple(transcription_tasks):
            task.cancel()
        if transcription_tasks:
            await asyncio.gather(*transcription_tasks, return_exceptions=True)

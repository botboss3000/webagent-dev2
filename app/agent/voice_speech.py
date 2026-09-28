"""Provider-neutral, cancellable streaming text-to-speech primitives."""

from __future__ import annotations

import asyncio
import html
import os
import re
from dataclasses import dataclass
from typing import AsyncIterator, Protocol

import httpx


_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^\)]+\)")
_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_RE = re.compile(r"[*_~#>|]+")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SPACE_RE = re.compile(r"[ \t]+")
_SECRET_RE = re.compile(
    r"(?i)\b(?:bearer\s+)?(?:sk-[a-z0-9_-]{12,}|api[_ -]?key\s*[:=]\s*\S+|token\s*[:=]\s*[a-z0-9._-]{12,})"
)


def sanitize_speech_text(text: str, *, max_chars: int = 4096) -> str:
    """Turn rich agent output into bounded text that is safe to read aloud.

    Code bodies, URLs, HTML, control characters, and credential-shaped strings
    are never sent to a speech provider.  This is a presentation boundary, not a
    replacement for the application's normal output security controls.
    """

    cleaned = html.unescape(str(text or ""))
    cleaned = _CODE_BLOCK_RE.sub(" Code omitted. ", cleaned)
    cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    cleaned = _LINK_RE.sub(r"\1", cleaned)
    cleaned = _URL_RE.sub(" link omitted ", cleaned)
    cleaned = _HTML_TAG_RE.sub(" ", cleaned)
    cleaned = _SECRET_RE.sub("[redacted]", cleaned)
    cleaned = _CONTROL_RE.sub(" ", cleaned)
    cleaned = _MARKDOWN_RE.sub(" ", cleaned)
    cleaned = _SPACE_RE.sub(" ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    limit = max(1, min(int(max_chars), 4096))
    if len(cleaned) > limit:
        clipped = cleaned[:limit]
        sentence_end = max(clipped.rfind(". "), clipped.rfind("? "), clipped.rfind("! "))
        if sentence_end >= limit // 2:
            clipped = clipped[: sentence_end + 1]
        cleaned = clipped.rstrip() + "…"
    return cleaned


@dataclass(frozen=True)
class SpeechAvailability:
    available: bool
    provider: str
    browser_fallback: bool
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "provider": self.provider,
            "browser_fallback": self.browser_fallback,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class SpeechChunk:
    data: bytes
    content_type: str
    audio_format: str
    sequence: int


class SpeechCancellationToken:
    def __init__(self) -> None:
        self._event = asyncio.Event()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def cancel(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class SpeechProvider(Protocol):
    def availability(self) -> SpeechAvailability: ...

    async def stream(
        self,
        text: str,
        *,
        cancel: SpeechCancellationToken | None = None,
    ) -> AsyncIterator[SpeechChunk]: ...


class SpeechUnavailable(RuntimeError):
    def __init__(self, availability: SpeechAvailability):
        super().__init__(availability.reason or "Server speech is unavailable")
        self.availability = availability


_CONTENT_TYPES = {
    "mp3": "audio/mpeg",
    "opus": "audio/ogg; codecs=opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "wav": "audio/wav",
    "pcm": "audio/pcm; rate=24000; encoding=signed-integer; bits=16",
}


class OpenAICompatibleSpeechProvider:
    """Stream audio bytes from an OpenAI-compatible ``/audio/speech`` API."""

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4o-mini-tts",
        voice: str = "alloy",
        audio_format: str = "mp3",
        speed: float = 1.0,
        instructions: str | None = None,
        timeout_seconds: float = 75.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or "gpt-4o-mini-tts"
        self.voice = voice.strip() or "alloy"
        self.audio_format = audio_format if audio_format in _CONTENT_TYPES else "mp3"
        self.speed = min(4.0, max(0.25, float(speed)))
        self.instructions = sanitize_speech_text(instructions or "", max_chars=1000) or None
        self.timeout_seconds = max(1.0, timeout_seconds)
        self._client = client

    @classmethod
    def from_environment(cls, *, client: httpx.AsyncClient | None = None) -> "OpenAICompatibleSpeechProvider":
        api_key = os.getenv("VOICE_TTS_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
        base_url = os.getenv("VOICE_TTS_BASE_URL") or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        return cls(
            api_key=api_key,
            base_url=base_url,
            model=os.getenv("VOICE_TTS_MODEL", "gpt-4o-mini-tts"),
            voice=os.getenv("VOICE_TTS_VOICE", "alloy"),
            audio_format=os.getenv("VOICE_TTS_FORMAT", "mp3").lower(),
            speed=float(os.getenv("VOICE_TTS_SPEED", "1.0")),
            instructions=os.getenv("VOICE_TTS_INSTRUCTIONS"),
            client=client,
        )

    def availability(self) -> SpeechAvailability:
        if not self.api_key:
            return SpeechAvailability(
                available=False,
                provider="browser",
                browser_fallback=True,
                reason="No server text-to-speech API key is configured.",
            )
        if not self.base_url:
            return SpeechAvailability(
                available=False,
                provider="browser",
                browser_fallback=True,
                reason="No server text-to-speech endpoint is configured.",
            )
        return SpeechAvailability(available=True, provider="openai_compatible", browser_fallback=False)

    @property
    def endpoint(self) -> str:
        if self.base_url.endswith("/audio/speech"):
            return self.base_url
        return f"{self.base_url}/audio/speech"

    async def stream(
        self,
        text: str,
        *,
        cancel: SpeechCancellationToken | None = None,
    ) -> AsyncIterator[SpeechChunk]:
        availability = self.availability()
        if not availability.available:
            raise SpeechUnavailable(availability)

        spoken = sanitize_speech_text(text)
        if not spoken:
            return
        if cancel is not None and cancel.cancelled:
            return

        payload: dict[str, object] = {
            "model": self.model,
            "voice": self.voice,
            "input": spoken,
            "response_format": self.audio_format,
            "speed": self.speed,
            "stream_format": "audio",
        }
        if self.instructions:
            payload["instructions"] = self.instructions
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        owns_client = self._client is None
        client = self._client or httpx.AsyncClient(timeout=self.timeout_seconds)
        try:
            async with client.stream("POST", self.endpoint, json=payload, headers=headers) as response:
                response.raise_for_status()
                sequence = 0
                async for data in response.aiter_bytes():
                    if cancel is not None and cancel.cancelled:
                        break
                    if not data:
                        continue
                    sequence += 1
                    yield SpeechChunk(
                        data=data,
                        content_type=_CONTENT_TYPES[self.audio_format],
                        audio_format=self.audio_format,
                        sequence=sequence,
                    )
        finally:
            if owns_client:
                await client.aclose()


class VoiceSpeechService:
    """Thin orchestration boundary so another provider can be substituted later."""

    def __init__(self, provider: SpeechProvider | None = None) -> None:
        self.provider = provider or OpenAICompatibleSpeechProvider.from_environment()

    def availability(self) -> SpeechAvailability:
        return self.provider.availability()

    async def stream(
        self,
        text: str,
        *,
        cancel: SpeechCancellationToken | None = None,
    ) -> AsyncIterator[SpeechChunk]:
        async for chunk in self.provider.stream(text, cancel=cancel):
            yield chunk


import json
import unittest

import httpx

from app.agent.voice_speech import (
    OpenAICompatibleSpeechProvider,
    SpeechCancellationToken,
    SpeechUnavailable,
    VoiceSpeechService,
    sanitize_speech_text,
)


class VoiceSpeechTests(unittest.IsolatedAsyncioTestCase):
    def test_sanitizes_markdown_code_urls_controls_and_secrets(self):
        source = "# Result\n```python\nprint('secret')\n``` Visit https://example.test/a?q=1 \x07 API key: sk-abcdefghijklmnopqrstuvwxyz"
        clean = sanitize_speech_text(source)
        self.assertIn("Code omitted", clean)
        self.assertIn("link omitted", clean)
        self.assertIn("[redacted]", clean)
        self.assertNotIn("print", clean)
        self.assertNotIn("https://", clean)
        self.assertNotIn("\x07", clean)

    def test_sanitizer_enforces_provider_limit(self):
        clean = sanitize_speech_text("x" * 5000)
        self.assertLessEqual(len(clean), 4097)
        self.assertTrue(clean.endswith("…"))

    async def test_unconfigured_provider_returns_browser_fallback_signal(self):
        provider = OpenAICompatibleSpeechProvider(api_key="")
        availability = provider.availability()
        self.assertFalse(availability.available)
        self.assertEqual(availability.provider, "browser")
        self.assertTrue(availability.browser_fallback)
        with self.assertRaises(SpeechUnavailable) as raised:
            _ = [chunk async for chunk in provider.stream("hello")]
        self.assertTrue(raised.exception.availability.browser_fallback)

    async def test_streams_openai_compatible_audio_and_sanitized_payload(self):
        captured = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["authorization"] = request.headers.get("authorization")
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, content=b"audio-bytes", headers={"content-type": "audio/mpeg"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        provider = OpenAICompatibleSpeechProvider(
            api_key="tts-secret",
            base_url="https://speech.example/v1",
            client=client,
        )
        chunks = [chunk async for chunk in provider.stream("Hello https://secret.example/path")]
        self.assertEqual(b"".join(chunk.data for chunk in chunks), b"audio-bytes")
        self.assertEqual(chunks[0].sequence, 1)
        self.assertEqual(captured["authorization"], "Bearer tts-secret")
        self.assertEqual(captured["body"]["input"], "Hello link omitted")
        self.assertEqual(captured["body"]["stream_format"], "audio")

    async def test_cancellation_before_request_performs_no_network_call(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            return httpx.Response(200, content=b"should-not-arrive")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        token = SpeechCancellationToken()
        token.cancel()
        provider = OpenAICompatibleSpeechProvider(api_key="key", client=client)
        chunks = [chunk async for chunk in provider.stream("hello", cancel=token)]
        self.assertEqual(chunks, [])
        self.assertEqual(calls, [])

    async def test_service_is_provider_neutral(self):
        provider = OpenAICompatibleSpeechProvider(api_key="")
        service = VoiceSpeechService(provider)
        self.assertEqual(service.availability(), provider.availability())


if __name__ == "__main__":
    unittest.main()


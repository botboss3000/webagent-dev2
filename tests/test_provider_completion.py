"""The shared one-shot completion route.

Everything that is not the streaming tool loop goes through ``complete_text``:
manager verdicts, closer audits, compaction, titling, suggestions, workers. The
provider knowledge they each needed — where the answer lands, whether reasoning
may be sent back, and what an error means — lives here once instead.
"""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.provider_completion import (
    SubscriptionResponseClient,
    _completion_is_empty,
    ask_model,
    complete_text,
    completion_answer,
    prepare_completion_messages,
    resolve_completion_profile,
)


def _response(content=None, reasoning=None, finish="stop"):
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(message=message, finish_reason=finish)])


class _Completions:
    def __init__(self, calls, response=None, error=None):
        self._calls, self._response, self._error = calls, response, error

    async def create(self, **kwargs):
        self._calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


class _Client:
    def __init__(self, base_url="https://api.deepseek.com/v1", response=None, error=None):
        self.base_url, self.calls, self._response, self._error = base_url, [], response, error
        self.chat = SimpleNamespace(completions=_Completions(self.calls, response, error))


class ProfileResolution(unittest.TestCase):
    def test_endpoint_comes_from_the_client(self):
        deepseek = resolve_completion_profile(client=_Client("https://api.deepseek.com/v1"))
        self.assertEqual(deepseek.family, "deepseek")

        openai = resolve_completion_profile(client=_Client("https://api.openai.com/v1"))
        self.assertEqual(openai.family, "openai")

    def test_explicit_provider_beats_the_hostname(self):
        # A proxy of DeepSeek behind someone else's domain.
        profile = resolve_completion_profile(
            client=_Client("https://proxy.example.com/v1"), provider="deepseek")
        self.assertEqual(profile.family, "deepseek")

    def test_subscription_calls_resolve_as_responses(self):
        client = SubscriptionResponseClient(scope="p", entry_id="", user_id="u", agent_id="")
        profile = resolve_completion_profile(
            client=client, provider="openai_subscription", transport="responses")
        self.assertTrue(profile.is_responses)


class OutgoingHygiene(unittest.TestCase):
    def test_reasoning_survives_only_for_the_endpoint_that_owns_it(self):
        messages = [{"role": "assistant", "content": "x", "reasoning_content": "why"}]
        deepseek = resolve_completion_profile(client=_Client("https://api.deepseek.com/v1"))
        other = resolve_completion_profile(client=_Client("https://api.openai.com/v1"))
        self.assertIn("reasoning_content", prepare_completion_messages(messages, deepseek)[0])
        self.assertNotIn("reasoning_content", prepare_completion_messages(messages, other)[0])

    def test_the_caller_messages_are_not_mutated(self):
        messages = [{"role": "assistant", "content": "x", "reasoning_content": "why"}]
        other = resolve_completion_profile(client=_Client("https://api.openai.com/v1"))
        prepare_completion_messages(messages, other)
        self.assertIn("reasoning_content", messages[0])


class AnswerSlot(unittest.TestCase):
    def test_content_wins(self):
        self.assertEqual(completion_answer(_response("answer", "thinking")), "answer")

    def test_reasoning_slot_is_read_when_content_is_empty(self):
        self.assertEqual(completion_answer(_response(None, "the real answer")), "the real answer")
        self.assertEqual(completion_answer(_response("   ", "the real answer")), "the real answer")

    def test_nothing_anywhere_is_empty(self):
        self.assertEqual(completion_answer(_response(None, None)), "")
        self.assertEqual(completion_answer(SimpleNamespace(choices=[])), "")

    def test_a_reasoning_only_reply_is_not_an_empty_completion(self):
        self.assertEqual(_completion_is_empty(_response(None, "answer")), (False, "stop"))
        self.assertEqual(_completion_is_empty(_response(None, None, "length")), (True, "length"))


class CompleteTextRoute(unittest.IsolatedAsyncioTestCase):
    async def test_the_profile_is_applied_to_what_is_sent(self):
        client = _Client("https://api.openai.com/v1", response=_response("ok"))
        await complete_text(
            client, model="gpt-5.6", max_tokens=10, temperature=0.2, timeout=5.0,
            messages=[{"role": "assistant", "content": "x", "reasoning_content": "why"}],
        )
        self.assertEqual(len(client.calls), 1)
        self.assertNotIn("reasoning_content", client.calls[0]["messages"][0])

    async def test_deepseek_keeps_its_own_reasoning(self):
        client = _Client("https://api.deepseek.com/v1", response=_response("ok"))
        await complete_text(
            client, model="deepseek-flash", max_tokens=10, temperature=0.2, timeout=5.0,
            messages=[{"role": "assistant", "content": "x", "reasoning_content": "why"}],
        )
        self.assertIn("reasoning_content", client.calls[0]["messages"][0])

    async def test_params_are_passed_through(self):
        client = _Client("https://api.deepseek.com/v1", response=_response("ok"))
        await complete_text(
            client, model="deepseek-flash", max_tokens=42, temperature=0.7, timeout=5.0,
            messages=[{"role": "user", "content": "hi"}],
        )
        call = client.calls[0]
        self.assertEqual(call["max_tokens"], 42)
        self.assertEqual(call["temperature"], 0.7)

    async def test_a_failed_call_carries_its_classification(self):
        error = RuntimeError(
            'Responses API HTTP 400: {"error":{"message":"The `reasoning_text` in '
            'the thinking mode must be passed back to the API.",'
            '"code":"invalid_request_error"}}')
        client = _Client("https://api.deepseek.com/v1", error=error)
        with self.assertRaises(RuntimeError) as caught:
            await complete_text(
                client, model="deepseek-flash", max_tokens=10, temperature=0.2,
                timeout=5.0, messages=[{"role": "user", "content": "hi"}],
            )
        info = getattr(caught.exception, "provider_error", None)
        self.assertIsNotNone(info, "callers must not have to parse the message")
        self.assertEqual(info.kind.value, "thinking_replay_rejected")


class AskModel(unittest.IsolatedAsyncioTestCase):
    async def test_the_reasoning_slot_answer_is_returned(self):
        # The quirk three callers had each guarded locally: a successful reply can
        # carry its whole text in the reasoning slot.
        with patch("app.agent.provider_completion.complete_text",
                   new=AsyncMock(return_value=_response(None, "described"))):
            text = await ask_model(
                {"model": "m", "base_url": "https://api.openai.com/v1", "api_key": "k"},
                "system", "user",
            )
        self.assertEqual(text, "described")

    async def test_incomplete_config_is_not_an_error(self):
        sink = []
        self.assertIsNone(await ask_model({}, "s", "u", error_sink=sink))
        self.assertTrue(sink)

    async def test_failure_returns_none_and_reports_why(self):
        sink = []
        with patch("app.agent.provider_completion.complete_text",
                   new=AsyncMock(side_effect=RuntimeError("no remaining credits"))):
            text = await ask_model(
                {"model": "m", "base_url": "https://api.openai.com/v1", "api_key": "k"},
                "s", "u", error_sink=sink,
            )
        self.assertIsNone(text)
        self.assertIn("no remaining credits", " ".join(sink))


if __name__ == "__main__":
    unittest.main()

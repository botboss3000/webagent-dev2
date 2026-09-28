"""The provider error table.

The outage's second half was a wording match: the recovery branch tested for
``"reasoning_content"``, which is the Chat Completions field name, so the same
rule under the Responses name (``reasoning_text``) never matched and a
deterministic 400 became a crash the resume loop replayed three times. These
tests pin both wordings to the same action, and pin deterministic client errors
to "do not retry unchanged".
"""
import unittest

from app.agent.provider_errors import (
    Action,
    Kind,
    classify_provider_error,
    provider_stop_cause,
)

_REAL_DEEPSEEK_RESPONSES = (
    'Responses API HTTP 400: {"error":{"message":"The `reasoning_text` in the '
    'thinking mode must be passed back to the API.","type":"invalid_request_error",'
    '"param":null,"code":"invalid_request_error"}}'
)
_REAL_DEEPSEEK_CHAT = (
    'Responses API HTTP 400: {"error":{"message":"The `reasoning_content` in the '
    'thinking mode must be passed back to the API.","type":"invalid_request_error"}}'
)


class ThinkingReplayRejection(unittest.TestCase):
    def test_responses_wording_is_recognised(self):
        info = classify_provider_error(RuntimeError(_REAL_DEEPSEEK_RESPONSES))
        self.assertIs(info.kind, Kind.THINKING_REPLAY_REJECTED)
        self.assertIs(info.action, Action.REBUILD_REPLAYABLE_HISTORY)
        self.assertEqual(info.status, 400)
        self.assertFalse(info.identical_retry_ok)

    def test_chat_completions_wording_is_recognised_too(self):
        # This is the case the old predicate covered; keeping both means the
        # field being renamed cannot silently drop the recovery again.
        info = classify_provider_error(RuntimeError(_REAL_DEEPSEEK_CHAT))
        self.assertIs(info.kind, Kind.THINKING_REPLAY_REJECTED)
        self.assertIs(info.action, Action.REBUILD_REPLAYABLE_HISTORY)

    def test_field_less_wording_still_matches_the_rule(self):
        info = classify_provider_error(
            RuntimeError('HTTP 400: {"error":{"message":"reasoning must be passed back"}}'))
        self.assertIs(info.kind, Kind.THINKING_REPLAY_REJECTED)

    def test_a_thinking_replay_error_is_worth_resuming(self):
        # It carries a repair, so the run may retry — the repair changes the
        # request, and the pre-send check stops the poisoned body being rebuilt.
        cause = provider_stop_cause(RuntimeError(_REAL_DEEPSEEK_RESPONSES))
        self.assertEqual(cause, "crash")


class OtherClientErrors(unittest.TestCase):
    def test_encrypted_history_rejection_rebases(self):
        exc = RuntimeError('HTTP 400: {"error":{"code":"invalid_encrypted_content"}}')
        info = classify_provider_error(exc)
        self.assertIs(info.kind, Kind.ENCRYPTED_HISTORY_REJECTED)
        self.assertIs(info.action, Action.REBASE_HISTORY)

    def test_stale_chain_rebases_the_chain(self):
        info = classify_provider_error(RuntimeError("previous_response_not_found"))
        self.assertIs(info.kind, Kind.STALE_CHAIN)
        self.assertIs(info.action, Action.REBASE_CHAIN)

    def test_chain_parameter_rejection_rebases_the_chain(self):
        # OpenRouter validates ``previous_response_id`` as null-only, so the request
        # SHAPE is accepted and only the chain is refused. Classified as a stale
        # chain so the turn is rebuilt from the local transcript instead of dying on
        # a 400 that used to fall through to "unknown client error".
        exc = RuntimeError(
            'Responses API HTTP 400: {"error":{"message":"Invalid Responses API '
            'request","code":"invalid_prompt"},"path":"previous_response_id",'
            '"detail":"Invalid input: expected null, received string"}'
        )
        info = classify_provider_error(exc)
        self.assertIs(info.kind, Kind.STALE_CHAIN)
        self.assertIs(info.action, Action.REBASE_CHAIN)
        self.assertFalse(info.identical_retry_ok)

    def test_tool_choice_rejection_drops_the_force(self):
        exc = RuntimeError('Responses API HTTP 400: Thinking mode does not support this tool_choice')
        info = classify_provider_error(exc)
        self.assertIs(info.kind, Kind.TOOL_CHOICE_REJECTED)
        self.assertIs(info.action, Action.DROP_FORCED_TOOL_CHOICE)

    def test_model_scoped_rejection_falls_back_transport(self):
        info = classify_provider_error(RuntimeError("Responses API HTTP 400: Model Not Exist"))
        self.assertIs(info.action, Action.FALLBACK_TRANSPORT)

    def test_unknown_400_is_terminal_and_not_retried_unchanged(self):
        info = classify_provider_error(RuntimeError('HTTP 400: {"error":{"message":"nope"}}'))
        self.assertIs(info.kind, Kind.UNKNOWN_CLIENT)
        self.assertIs(info.action, Action.TERMINAL)
        self.assertFalse(info.identical_retry_ok)
        self.assertTrue(info.is_terminal)

    def test_unknown_client_error_is_not_resumable(self):
        # Three identical resumes were spent on exactly this shape of failure.
        self.assertEqual(
            provider_stop_cause(RuntimeError('HTTP 400: {"error":{"message":"nope"}}')),
            "provider_error",
        )

    def test_empty_input_400_is_terminal(self):
        # DeepSeek /responses rejects a body whose input array is empty. It carries
        # no repair, so an unchanged retry cannot help: it must stop the loop rather
        # than become a resumable 'crash' the resume ladder replays.
        exc = RuntimeError(
            'Responses API HTTP 400: {"error":{"message":"Input items array must not '
            'be empty","type":"invalid_request_error"}}')
        info = classify_provider_error(exc)
        self.assertIs(info.action, Action.TERMINAL)
        self.assertFalse(info.identical_retry_ok)
        self.assertEqual(provider_stop_cause(exc), "provider_error")


class AuthQuotaAndTransient(unittest.TestCase):
    def test_unauthorized_is_terminal(self):
        info = classify_provider_error(RuntimeError("HTTP 401: Unauthorized"))
        self.assertIs(info.kind, Kind.AUTH)
        self.assertIs(info.action, Action.TERMINAL)

    def test_exhausted_quota_is_terminal(self):
        exc = RuntimeError('Responses API HTTP 429: {"error":{"type":"usage_limit_reached"}}')
        info = classify_provider_error(exc)
        self.assertIs(info.kind, Kind.QUOTA)
        self.assertIs(info.action, Action.TERMINAL)
        self.assertFalse(info.identical_retry_ok)

    def test_plain_rate_limit_may_retry(self):
        info = classify_provider_error(RuntimeError("HTTP 429: rate limit exceeded"))
        self.assertIs(info.kind, Kind.QUOTA)
        self.assertIs(info.action, Action.RETRY_BACKOFF)
        self.assertTrue(info.identical_retry_ok)

    def test_server_error_may_retry(self):
        info = classify_provider_error(RuntimeError("Responses API HTTP 503: upstream down"))
        self.assertIs(info.kind, Kind.TRANSIENT)
        self.assertTrue(info.identical_retry_ok)

    def test_transport_error_without_status_may_retry(self):
        info = classify_provider_error(ConnectionError("connection reset"))
        self.assertIs(info.kind, Kind.TRANSIENT)
        self.assertIsNone(info.status)
        self.assertTrue(info.identical_retry_ok)


class StopCause(unittest.TestCase):
    def test_internal_bugs_still_resume(self):
        # Only errors that actually carry an HTTP status are re-classified, so an
        # ordinary exception keeps the resumable "crash" cause.
        self.assertEqual(provider_stop_cause(KeyError("boom")), "crash")

    def test_transient_provider_errors_still_resume(self):
        self.assertEqual(provider_stop_cause(RuntimeError("HTTP 503: down")), "crash")

if __name__ == "__main__":
    unittest.main()

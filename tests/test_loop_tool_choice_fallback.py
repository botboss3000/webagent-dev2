"""Regression coverage for the forced-tool_choice fallback.

A thinking-mode reasoning model (e.g. DeepSeek's ``deepseek-v4-flash-vision-exp``)
rejects an enforced ``tool_choice`` outright with HTTP 400 "Thinking mode does
not support this tool_choice". The agent's own guardrails force a tool call
after a prose-only reply, so before the fix that 400 was recorded as a resumable
crash and replayed by every resume until the retry budget was spent.

Two layers are covered here:
  * the provider-error retry ladder degrades a rejected forced choice to
    ``'auto'`` and retries in-turn instead of failing the run; and
  * the guardrail is never armed with a forced choice when the catalog flags the
    model as reasoning-capable.

These tests fail against the pre-fix loop (the run dies with an "LLM call
failed" error event and no retry happens).
"""
import asyncio
import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db.local import LocalBackend
from app.tools.loader import ToolInfo
from app.agent.loop import (
    _stream_agent_events_impl,
    _model_allows_forced_tool_choice,
    _effective_reasoning_effort,
)

# The exact production failure text (DeepSeek, thinking mode + forced choice).
_THINKING_MODE_400 = (
    "Error code: 400 - {'error': {'message': 'Thinking mode does not support "
    "this tool_choice', 'type': 'invalid_request_error'}}"
)


class _Stream:
    def __init__(self, chunk):
        self._chunk = chunk
        self.reasoning_items = []
        self.response_id = None

    def __aiter__(self):
        async def _items():
            yield self._chunk
        return _items()

    async def close(self):
        return None


class _SilentStream:
    """A Responses stream that opens successfully but never yields an event."""

    reasoning_items = []
    response_id = None

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise asyncio.TimeoutError

    async def close(self):
        return None


def _tool_chunk(name, args, call_id="call-1"):
    delta = SimpleNamespace(
        content=None,
        reasoning_content=None,
        tool_calls=[SimpleNamespace(
            index=0,
            id=call_id,
            function=SimpleNamespace(name=name, arguments=json.dumps(args)),
        )],
    )
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def _text_chunk(text):
    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


class ToolChoiceFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self,
        actions,
        reasoning_model=False,
        max_turns=12,
        provider_config=None,
        responses_opener=None,
        open_errors=None,
    ):
        with tempfile.TemporaryDirectory() as td:
            db = LocalBackend(str(Path(td) / "loop.db"), seed=False)
            conn = db._get_conn()
            conn.execute(
                "INSERT INTO sessions (id, user_id, metadata) VALUES (?, ?, ?)",
                ("session-1", "admin", json.dumps({"execution_mode": "auto"})),
            )
            conn.commit()
            conn.close()

            provider_calls = []
            action_queue = list(actions)

            async def read_source(**kwargs):
                return "source result"

            async def _noop(**kwargs):
                return "{}"

            tools = {
                "read_source": ToolInfo(
                    name="read_source",
                    handler=read_source,
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                "update_task_checkpoint": ToolInfo(
                    name="update_task_checkpoint",
                    handler=_noop,
                    parameters={"type": "object", "properties": {
                        "expected_revision": {"type": "integer"},
                        "update": {"type": "object"},
                    }, "required": ["expected_revision", "update"]},
                ),
                "get_task_checkpoint": ToolInfo(
                    name="get_task_checkpoint",
                    handler=_noop,
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
            }

            async def fake_open_stream(create_kwargs, timeout_s, client=None):
                provider_calls.append(dict(create_kwargs))
                model_error = (open_errors or {}).get(create_kwargs.get("model"))
                if isinstance(model_error, BaseException):
                    raise model_error
                if model_error:
                    raise RuntimeError(model_error)
                # Emulate a thinking-mode provider that refuses an enforced
                # tool choice outright — the exact production 400.
                if create_kwargs.get("tool_choice") == "required":
                    raise Exception(_THINKING_MODE_400)
                action = action_queue.pop(0) if action_queue else {"text": "done"}
                if action.get("silent"):
                    return _SilentStream()
                if action.get("tool"):
                    chunk = _tool_chunk(action["tool"], action.get("args", {}))
                else:
                    chunk = _text_chunk(action.get("text", "done"))
                return _Stream(chunk)

            async def fake_provider(*args, **kwargs):
                return provider_config or {
                    "provider": "test",
                    "base_url": "https://provider.example/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                }

            lookup_return = {"reasoning": True} if reasoning_model else None
            responses_patch = (patch("app.agent.loop.open_responses_stream", new=responses_opener) if responses_opener else nullcontext())
            patches = [
                patch("app.tools.loader.load_tools", new=AsyncMock(return_value=tools)),
                patch("app.admin.settings.apply_provider_for_run", new=AsyncMock(side_effect=fake_provider)),
                patch("app.agent.contract_permissions.active_profile", return_value=None),
                patch("app.agent.run_scout.artifact_for_turn", return_value={
                    "id": "turn-1",
                    "source_messages": [{"interaction_id": "turn-1", "content": "Do the work"}],
                }),
                patch("app.agent.loop._open_stream", new=fake_open_stream),
                patch("app.agent.loop._record_billing_usage", new=AsyncMock(return_value={})),
                patch("app.agent.loop._agent_profile_visitor", return_value=None),
                patch("app.agent.session_changes.capture_tool_state_async", new=AsyncMock(return_value={})),
                patch("app.agent.session_changes.record_tool_delta", new=AsyncMock(return_value=[])),
                patch("app.model_catalog.lookup", return_value=lookup_return),
            ]
            with (patches[0], patches[1], patches[2], patches[3], patches[4],
                  patches[5], patches[6], patches[7], patches[8], patches[9], responses_patch):
                events = [
                    event async for event in _stream_agent_events_impl(
                        user_id="admin",
                        session_id="session-1",
                        user_message="Do the work",
                        system_prompt="",
                        agent_id="",
                        history=[],
                        parent_interaction_id="turn-1",
                        db=db,
                        execution_mode="auto",
                        run_turn_id="turn-1",
                        max_turns=max_turns,
                    )
                ]
            return provider_calls, events

    # ── Layer 1: the retry ladder degrades instead of crashing ──────────────

    async def test_forced_tool_choice_rejection_degrades_instead_of_crashing(self):
        provider_calls, events = await self._run([
            {"text": "I will inspect the source now."},
            {"tool": "read_source"},
            {"text": "The inspection is complete and the answer is saved."},
        ])
        choices = [call.get("tool_choice") for call in provider_calls]

        # The guardrail still forces once; the provider rejects it; the loop
        # must retry the same turn with 'auto' rather than fail the run.
        self.assertIn("required", choices)
        first_forced = choices.index("required")
        self.assertEqual(
            choices[first_forced + 1], "auto",
            "a rejected forced tool_choice must be retried as 'auto'",
        )

        # The pre-fix behaviour: a pre-stream failure surfaced as an error event.
        self.assertFalse(
            any(event.get("type") == "error"
                and "LLM call failed" in str(event.get("message"))
                for event in events),
            "the forced-choice 400 must not surface as an LLM call failure",
        )
        self.assertFalse(any(event.get("stop_cause") == "crash" for event in events))
        # The turn still produced a user-visible result.
        self.assertTrue(any(event.get("type") == "response" for event in events))

    async def test_eligible_pre_stream_failure_uses_grouped_alias(self):
        root = "root"
        provider_config = {
            "entry_id": root,
            "alias_group_id": root,
            "provider": "test",
            "base_url": "https://primary.example/v1",
            "api_key": "primary-key",
            "model": "primary",
            "stateful_responses": "disabled",
            "multi_providers": [
                {
                    "entry_id": root,
                    "alias_group_id": root,
                    "provider": "test",
                    "base_url": "https://primary.example/v1",
                    "api_key": "primary-key",
                    "model": "primary",
                    "stateful_responses": "disabled",
                },
                {
                    "entry_id": "backup",
                    "alias_group_id": root,
                    "provider": "test",
                    "base_url": "https://backup.example/v1",
                    "api_key": "backup-key",
                    "model": "backup",
                    "stateful_responses": "disabled",
                },
            ],
        }

        failures = [
            "HTTP 503: upstream unavailable",
            asyncio.TimeoutError(),
        ]
        for failure in failures:
            with self.subTest(failure=type(failure).__name__ if not isinstance(failure, str) else failure):
                provider_calls, events = await self._run(
                    [{"text": "Completed on the backup."}],
                    max_turns=1,
                    provider_config=provider_config,
                    open_errors={"primary": failure},
                )

                self.assertEqual(
                    [call["model"] for call in provider_calls[:2]],
                    ["primary", "backup"],
                )
                failovers = [
                    event for event in events
                    if event.get("step") == "llm_alias_failover"
                ]
                self.assertEqual(len(failovers), 1)
                self.assertEqual(failovers[0]["from_entry_id"], root)
                self.assertEqual(failovers[0]["to_entry_id"], "backup")
                self.assertFalse(any(event.get("type") == "error" for event in events))

    async def test_deterministic_client_error_does_not_use_alias(self):
        root = "root"
        provider_config = {
            "entry_id": root,
            "alias_group_id": root,
            "provider": "test",
            "base_url": "https://primary.example/v1",
            "api_key": "primary-key",
            "model": "primary",
            "stateful_responses": "disabled",
            "multi_providers": [
                {
                    "entry_id": root,
                    "alias_group_id": root,
                    "provider": "test",
                    "base_url": "https://primary.example/v1",
                    "api_key": "primary-key",
                    "model": "primary",
                    "stateful_responses": "disabled",
                },
                {
                    "entry_id": "backup",
                    "alias_group_id": root,
                    "provider": "test",
                    "base_url": "https://backup.example/v1",
                    "api_key": "backup-key",
                    "model": "backup",
                    "stateful_responses": "disabled",
                },
            ],
        }

        provider_calls, events = await self._run(
            [{"text": "must not be reached"}],
            max_turns=1,
            provider_config=provider_config,
            open_errors={"primary": 'HTTP 400: {"error":{"message":"malformed input"}}'},
        )

        self.assertEqual([call["model"] for call in provider_calls], ["primary"])
        self.assertFalse(any(
            event.get("step") == "llm_alias_failover" for event in events
        ))
        self.assertTrue(any(event.get("type") == "error" for event in events))

    # ── Layer 2: a reasoning model is never force-armed ─────────────────────

    async def test_reasoning_model_is_never_armed_with_forced_choice(self):
        provider_calls, events = await self._run([
            {"text": "I will inspect the source now."},
            {"text": "I will now continue with the inspection."},
            {"text": "The inspection is complete."},
            {"text": "The task is finished."},
        ], reasoning_model=True)
        choices = [call.get("tool_choice") for call in provider_calls]
        self.assertTrue(choices)
        self.assertNotIn(
            "required", choices,
            "a reasoning-capable model must never be sent a forced tool_choice",
        )
        self.assertFalse(
            any(isinstance(call.get("tool_choice"), dict) for call in provider_calls),
            "a reasoning-capable model must never be sent a forced named tool_choice",
        )
        self.assertFalse(any(event.get("stop_cause") == "crash" for event in events))

    # ── The capability helper itself ────────────────────────────────────────

    def test_helper_is_permissive_for_unknown_and_non_reasoning_models(self):
        with patch("app.model_catalog.lookup", return_value=None):
            self.assertTrue(
                _model_allows_forced_tool_choice("mystery-model", "some-provider")
            )
        with patch("app.model_catalog.lookup", return_value={"reasoning": False}):
            self.assertTrue(_model_allows_forced_tool_choice("gpt-4o", "openai"))
        with patch("app.model_catalog.lookup", return_value={"reasoning": True}):
            self.assertFalse(
                _model_allows_forced_tool_choice(
                    "deepseek-v4-flash-vision-exp", "deepseek",
                )
            )
            self.assertFalse(
                _model_allows_forced_tool_choice(
                    "deepseek-v4-flash-vision-exp", "deepseek", "responses",
                ),
                "DeepSeek Responses never transmits tool_choice; forcing would "
                "only suppress the reasoning it requires back",
            )
            self.assertFalse(
                _model_allows_forced_tool_choice(
                    "deepseek/deepseek-v4-flash", "openrouter", "responses",
                )
            )

    def test_deepseek_responses_effort_mapping_never_disables_thinking(self):
        # This mapper encodes DeepSeek's own effort vocabulary and nothing else.
        # A forced tool choice used to switch thinking OFF here, which produced an
        # assistant turn carrying no reasoning for the endpoint to reject on the
        # very next call. The force is declined instead (provider_profile), so no
        # input maps a configured effort onto "none".
        self.assertEqual(_effective_reasoning_effort("none", "deepseek", "responses"), "none")
        self.assertEqual(_effective_reasoning_effort("low", "deepseek", "responses"), "low")
        self.assertEqual(_effective_reasoning_effort("medium", "deepseek", "responses"), "high")
        self.assertEqual(_effective_reasoning_effort("high", "deepseek", "responses"), "high")
        self.assertEqual(_effective_reasoning_effort("xhigh", "deepseek", "responses"), "max")
        self.assertEqual(_effective_reasoning_effort("max", "deepseek", "responses"), "max")
        # Other providers are passed through untouched.
        self.assertEqual(_effective_reasoning_effort("high", "openrouter", "responses"), "high")

    def test_helper_fails_open_when_catalog_raises(self):
        with patch("app.model_catalog.lookup", side_effect=RuntimeError("boom")):
            self.assertTrue(
                _model_allows_forced_tool_choice("any-model", "any-provider"),
                "a catalog failure must not disable the guardrails",
            )


    async def test_silent_deepseek_responses_stream_falls_back_in_turn(self):
        responses_calls = []

        async def open_silent_responses(**kwargs):
            responses_calls.append(kwargs["body"])
            return _SilentStream()

        provider_calls, events = await self._run(
            [
                {"tool": "read_source"},
                {"text": "The inspection is complete."},
            ],
            reasoning_model=True,
            provider_config={
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "test-key",
                "model": "deepseek-v4-flash-vision-exp",
                "reasoning_effort": "high",
            },
            responses_opener=open_silent_responses,
        )

        self.assertEqual(len(responses_calls), 1)
        self.assertTrue(provider_calls)
        self.assertEqual(provider_calls[0]["tool_choice"], "auto")
        fallback = [event for event in events if event.get("step") == "llm_transport_fallback"]
        self.assertEqual(len(fallback), 1)
        self.assertEqual(fallback[0]["error_category"], "stream_stall_before_first_event")
        self.assertFalse(any(event.get("type") == "error" for event in events))
        self.assertTrue(any(event.get("type") == "response" for event in events))


    async def test_silent_deepseek_fallback_fails_terminally(self):
        async def open_silent_responses(**kwargs):
            return _SilentStream()

        provider_calls, events = await self._run(
            [{"silent": True}],
            reasoning_model=True,
            provider_config={
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "test-key",
                "model": "deepseek-v4-flash-vision-exp",
                "reasoning_effort": "high",
            },
            responses_opener=open_silent_responses,
        )

        self.assertEqual(len(provider_calls), 1)
        fallback = [
            event
            for event in events
            if event.get("step") == "llm_transport_fallback"
        ]
        self.assertEqual(len(fallback), 1)
        errors = [event for event in events if event.get("type") == "error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["stop_cause"], "provider_unavailable")
        self.assertIn("DeepSeek did not return", errors[0]["message"])
        self.assertFalse(
            any(event.get("type") == "response" for event in events)
        )

    async def test_thinking_replay_rejection_rebuilds_and_retries_in_turn(self):
        """The outage itself, as a regression test.

        DeepSeek rejects a request whose replayed tool calls carry none of the
        reasoning that produced them. The old recovery predicate tested for
        "reasoning_content" — the Chat Completions field name — so the same rule
        under the Responses name ("reasoning_text") never matched. The error
        became a crash, and the crash-recovery loop replayed the identical
        request three times before the session was marked failed.

        Now the classifier recognises the rule rather than the wording, the loop
        rebuilds the outbound history and retries on the same transport, and the
        rebuilt body is re-checked before it is sent.
        """
        attempts = {"n": 0}

        async def open_rejecting_then_ok(**kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError(
                    'Responses API HTTP 400: {"error":{"message":"The '
                    '`reasoning_text` in the thinking mode must be passed back to '
                    'the API.","type":"invalid_request_error",'
                    '"param":null,"code":"invalid_request_error"}}'
                )
            if attempts["n"] == 2:
                return _Stream(_tool_chunk("read_source", {}))
            return _Stream(_text_chunk("Done."))

        with self.assertLogs("app.agent.loop", level="WARNING") as captured:
            _provider_calls, events = await self._run(
                [{"tool": "read_source"}, {"text": "Done."}],
                reasoning_model=True,
                provider_config={
                    "provider": "deepseek",
                    "base_url": "https://api.deepseek.com/v1",
                    "api_key": "test-key",
                    "model": "deepseek-v4-flash-vision-exp",
                    "reasoning_effort": "high",
                },
                responses_opener=open_rejecting_then_ok,
            )

        self.assertGreaterEqual(
            attempts["n"], 2,
            "the loop must rebuild the history and retry rather than fail the run",
        )
        # The rejected replay must be recovered. The run may still stop for an
        # ordinary agent reason (the work policy objects to a prose-only reply in
        # this mock), but never as the provider crash it used to become.
        fatal = [e for e in events if e.get("type") == "error"
                 and e.get("stop_cause") in {"crash", "provider_error"}]
        self.assertFalse(
            fatal, "the rejected replay must not be fatal: %r" % (fatal[:1],),
        )
        log_text = "\n".join(captured.output)
        self.assertIn("thinking-mode replay rejected", log_text)
        self.assertIn("replay check failed before send", log_text,
                      "the rebuilt body must be re-checked before it is sent")
        # ...and the decision is observable, not silent.
        replay = [e for e in events if e.get("step") == "reasoning_replay"]
        self.assertTrue(replay, "the replay check must report what it replayed")
        self.assertEqual(replay[0]["obligation"], "required")
        self.assertEqual(replay[0]["shape"], "reasoning_text")


    async def test_thinking_replay_rejection_is_bounded(self):
        """One rebuild, never a loop: the second rejection is not retried again."""
        attempts = {"n": 0}

        async def open_always_rejecting(**kwargs):
            attempts["n"] += 1
            raise RuntimeError(
                'Responses API HTTP 400: {"error":{"message":"The `reasoning_text` '
                'in the thinking mode must be passed back to the API.",'
                '"code":"invalid_request_error"}}'
            )

        _provider_calls, events = await self._run(
            [{"text": "unused"}],
            reasoning_model=True,
            provider_config={
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "api_key": "test-key",
                "model": "deepseek-v4-flash-vision-exp",
                "reasoning_effort": "high",
            },
            responses_opener=open_always_rejecting,
        )

        # The repair is one-shot, so the second rejection stops the turn instead of
        # rebuilding forever.
        self.assertEqual(attempts["n"], 2)
        self.assertTrue(any(event.get("type") == "error" for event in events))


if __name__ == "__main__":
    unittest.main()

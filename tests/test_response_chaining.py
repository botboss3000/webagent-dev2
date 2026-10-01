import json
import unittest

import httpx

from app.agent.response_chaining import (
    _EventState,
    _headers,
    build_responses_request,
    is_unsupported_deepseek_responses_error,
    response_chain_capability,
    response_chain_history_hash,
    response_event_chunks,
    responses_input,
    open_responses_stream,
)
from app.agent.session_history import _extract_responses_reasoning
from app.db.local import LocalBackend
from app.admin.settings import _assign_slots, _merge_agent_override, _resolve_slot


class ResponseChainingCapabilityTests(unittest.TestCase):
    def test_openresponses_providers_are_enabled(self):
        cases = (
            ("openai", "https://api.openai.com/v1", "gpt-5.6"),
            ("xai", "https://api.x.ai/v1", "grok-4.6"),
            ("azure", "https://example.openai.azure.com/openai/v1", "deployment"),
        )
        for provider, url, model in cases:
            with self.subTest(provider=provider):
                cap = response_chain_capability(
                    provider=provider, base_url=url, model=model,
                )
                self.assertTrue(cap.enabled)
                self.assertEqual(cap.transport, "responses")
                self.assertTrue(cap.stateful)

    def test_official_deepseek_uses_stateless_responses(self):
        cap = response_chain_capability(
            provider="deepseek", base_url="https://api.deepseek.com/v1",
            model="deepseek-v4-flash-vision-exp",
        )
        self.assertTrue(cap.enabled)
        self.assertEqual(cap.transport, "responses")
        self.assertEqual(cap.family, "deepseek")
        self.assertFalse(cap.stateful)

    def test_openrouter_uses_stateless_responses(self):
        # Its Responses schema admits ``previous_response_id`` as null only, so no
        # chain can ever be resumed: the transport stays, the chain does not.
        # Sending one drew HTTP 400 invalid_prompt on nemotron, gemini and nex-agi
        # alike, and each rejected continuation cost the model its own tool result.
        cap = response_chain_capability(
            provider="openrouter", base_url="https://openrouter.ai/api/v1",
            model="nex-agi/nex-n2.5-pro:free",
        )
        self.assertTrue(cap.enabled)
        self.assertEqual(cap.transport, "responses")
        self.assertEqual(cap.family, "openrouter")
        self.assertFalse(cap.stateful)

    def test_openrouter_stateless_body_never_offers_a_chain(self):
        body = build_responses_request(
            model="nex-agi/nex-n2.5-pro:free",
            messages=[{"role": "user", "content": "hi"}],
            tools=None,
            max_output_tokens=64,
            provider_family_name="openrouter",
            previous_response_id="resp_abc123",
            delta_start=4,
            session_key="wa-" + "0" * 40,
            stateful=False,
        )
        self.assertNotIn("previous_response_id", body)
        # No session-scoped continuation either: a stateful session would drift
        # from the history we resend whole.
        self.assertNotIn("session_id", body)
        # store is the one field OpenRouter's schema does constrain, so it is
        # still sent — omitting it is not the same as sending false there.
        self.assertIs(body.get("store"), False)
        # delta_start is ignored without a chain: the whole history travels.
        self.assertEqual(len(body["input"]), 1)

    def test_deepseek_routing_requires_provider_and_official_host(self):
        routed = response_chain_capability(
            provider="openrouter", base_url="https://openrouter.ai/api/v1",
            model="deepseek/deepseek-v4-flash",
        )
        self.assertTrue(routed.enabled)
        self.assertEqual(routed.family, "openrouter")
        # Routed DeepSeek is not first-party DeepSeek: different host, different
        # endpoint behaviour. Reached through OpenRouter it is stateless, exactly
        # as any other OpenRouter model.
        self.assertFalse(routed.stateful)

        proxy = response_chain_capability(
            provider="deepseek", base_url="https://proxy.example/v1",
            model="deepseek-v4-flash",
        )
        self.assertFalse(proxy.enabled)
        self.assertEqual(proxy.transport, "chat_completions")

    def test_explicit_disable_wins(self):
        cap = response_chain_capability(
            provider="openai", base_url="https://api.openai.com/v1", model="gpt-5.6",
            mode="disabled",
        )
        self.assertFalse(cap.enabled)

    def test_provider_setting_survives_slot_and_agent_resolution(self):
        entry = {
            "entry_id": "xai", "provider": "xai", "base_url": "https://api.x.ai/v1",
            "api_key": "key", "model": "grok", "enabled": True,
            "text_capable": True, "stateful_responses": "disabled",
        }
        slots = _assign_slots([entry])
        resolved = _resolve_slot(slots, "role", "standard")
        self.assertEqual(resolved["stateful_responses"], "disabled")
        merged = _merge_agent_override(
            {"stateful_responses": "auto"},
            {"stateful_responses": "disabled"},
        )
        self.assertEqual(merged["stateful_responses"], "disabled")


class ResponseRequestTests(unittest.TestCase):
    def test_chained_request_resends_instructions_and_only_sends_delta(self):
        messages = [
            {"role": "system", "content": "Current agent policy"},
            {"role": "user", "content": "old question"},
            {"role": "assistant", "content": "old answer"},
            {"role": "system", "content": "Fresh wiki context"},
            {"role": "user", "content": "new question"},
        ]
        body = build_responses_request(
            model="gpt-5.6", messages=messages, tools=None,
            max_output_tokens=1000, previous_response_id="resp_old", delta_start=3,
        )
        self.assertEqual(body["previous_response_id"], "resp_old")
        self.assertEqual(body["input"], [{"role": "user", "content": "new question"}])
        self.assertIn("Current agent policy", body["instructions"])
        self.assertIn("Fresh wiki context", body["instructions"])

    def test_openrouter_sends_neither_a_chain_nor_a_session(self):
        # Its Responses schema exposes store=false only, and it cannot retain a
        # response (every non-null previous_response_id is rejected), so no
        # provider-side session is offered either: a retained session would only
        # drift from the history this transport resends whole.
        body = build_responses_request(
            model="openai/gpt-5.4", messages=[{"role": "user", "content": "hi"}],
            tools=None, max_output_tokens=1000, provider_family_name="openrouter",
            session_key="session",
        )
        self.assertIs(body["store"], False)
        self.assertNotIn("session_id", body)

    def test_deepseek_stateless_request_omits_chaining_controls(self):
        messages = [
            {"role": "system", "content": "policy"},
            {"role": "user", "content": "older request"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "inspect it"},
        ]
        body = build_responses_request(
            model="deepseek-v4-flash-vision-exp",
            messages=messages,
            tools=[{
                "type": "function",
                "function": {
                    "name": "search_source", "description": "Search",
                    "parameters": {"type": "object", "properties": {}},
                },
            }],
            max_output_tokens=1000,
            previous_response_id="must-not-leak",
            delta_start=3,
            reasoning_effort="none",
            prompt_cache_key="must-not-leak",
            provider_family_name="deepseek",
            session_key="must-not-leak",
            stateful=False,
        )
        self.assertEqual(body["input"], [
            {"role": "user", "content": "older request"},
            {"role": "assistant", "content": "older answer"},
            {"role": "user", "content": "inspect it"},
        ])
        self.assertEqual(body["reasoning"], {"effort": "none"})
        self.assertEqual(body["tool_choice"], "auto")
        for unsupported in ("store", "previous_response_id", "prompt_cache_key", "session_id"):
            self.assertNotIn(unsupported, body)

    def test_tool_history_converts_to_responses_items(self):
        messages = [
            {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "lookup", "arguments": '{"q":"x"}'},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        items = responses_input(messages)
        self.assertEqual(items[0]["type"], "function_call")
        self.assertEqual(items[1], {
            "type": "function_call_output", "call_id": "call_1", "output": "result",
        })

    def test_encrypted_reasoning_is_replayed_before_assistant_tool_calls(self):
        messages = [{
            "role": "assistant", "content": None,
            "_responses_reasoning": [{
                "type": "reasoning", "encrypted_content": "encrypted",
                "summary": [],
            }],
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }]
        items = responses_input(messages)
        self.assertEqual(items[0]["type"], "reasoning")
        self.assertEqual(items[0]["encrypted_content"], "encrypted")
        self.assertEqual(items[1]["type"], "function_call")

    def test_history_hash_ignores_dynamic_system_layers(self):
        left = [{"role": "system", "content": "old"}, {"role": "user", "content": "same"}]
        right = [{"role": "system", "content": "new"}, {"role": "user", "content": "same"}]
        self.assertEqual(response_chain_history_hash(left), response_chain_history_hash(right))

    def test_stream_events_normalize_text_tools_and_usage(self):
        state = _EventState()
        text = response_event_chunks(
            {"type": "response.output_text.delta", "delta": "hello"}, state,
        )[0]
        self.assertEqual(text.choices[0].delta.content, "hello")

        added = response_event_chunks({
            "type": "response.output_item.added", "output_index": 2,
            "item": {"type": "function_call", "id": "item_1", "call_id": "call_1", "name": "lookup"},
        }, state)[0]
        self.assertEqual(added.choices[0].delta.tool_calls[0].function.name, "lookup")
        args = response_event_chunks({
            "type": "response.function_call_arguments.delta", "item_id": "item_1", "delta": "{}",
        }, state)[0]
        self.assertEqual(args.choices[0].delta.tool_calls[0].index, 2)

        self.assertEqual(response_event_chunks({
            "type": "response.output_item.done",
            "item": {
                "type": "reasoning", "id": "reason_1",
                "encrypted_content": "encrypted", "summary": [],
            },
        }, state), [])
        self.assertEqual(state.reasoning_items[0]["encrypted_content"], "encrypted")

        usage = response_event_chunks({
            "type": "response.completed",
            "response": {"usage": {"input_tokens": 10, "output_tokens": 3,
                                     "input_tokens_details": {"cached_tokens": 7}}},
        }, state)[0]
        self.assertEqual(usage.usage.prompt_tokens, 10)
        self.assertEqual(usage.usage.prompt_tokens_details["cached_tokens"], 7)

    def test_deepseek_plain_reasoning_round_trips_with_tool_call(self):
        state = _EventState(preserve_plain_reasoning=True)
        self.assertEqual(response_event_chunks({
            "type": "response.reasoning_text.delta", "item_id": "rs_1",
            "delta": "I should inspect the source.",
        }, state), [])
        response_event_chunks({
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_1", "content": []},
        }, state)
        self.assertEqual(state.reasoning_items, [{
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "I should inspect the source."}],
        }])

        items = responses_input([{
            "role": "assistant", "content": None,
            "_responses_reasoning": state.reasoning_items,
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "search_source", "arguments": "{}"},
            }],
        }, {
            "role": "tool", "tool_call_id": "call_1", "content": "result",
        }])
        self.assertEqual(items[0], state.reasoning_items[0])
        self.assertEqual(items[1]["type"], "function_call")
        self.assertEqual(items[2]["type"], "function_call_output")

    def test_a_reasoning_item_carrying_both_text_and_a_handle_keeps_the_text(self):
        """The live DeepSeek Responses shape: one item, two forms at once.

        ``response.output_item.done`` for a reasoning item carries the plain
        ``content`` AND an ``encrypted_content`` handle beside it. The handle is
        only meaningful to the endpoint that issued it, so a plain-reasoning
        transport must keep the text; testing the handle first stored it and
        dropped the text, leaving the replay with nothing to pass back — the
        deterministic "reasoning_text ... must be passed back" 400.
        """
        live_item = {
            "type": "reasoning", "id": "rs_1", "status": "completed",
            "content": [{"type": "reasoning_text", "text": "We need answer user."}],
            "summary": [],
            "encrypted_content": "7d026272-3161-4d90-b70b-70a0854246fd-0",
        }
        state = _EventState(preserve_plain_reasoning=True)
        response_event_chunks(
            {"type": "response.output_item.done", "item": live_item}, state)
        self.assertEqual(state.reasoning_items, [{
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "We need answer user."}],
        }])

        # The same preference holds when the item arrives on response.completed.
        state2 = _EventState(preserve_plain_reasoning=True)
        response_event_chunks(
            {"type": "response.completed", "response": {"output": [live_item]}}, state2)
        self.assertEqual(state2.reasoning_items, state.reasoning_items)

        # With no text at all the handle is still the only thing worth keeping.
        state3 = _EventState(preserve_plain_reasoning=True)
        response_event_chunks({
            "type": "response.output_item.done",
            "item": {"type": "reasoning", "id": "rs_2", "content": [],
                     "summary": [], "encrypted_content": "handle-2"},
        }, state3)
        self.assertEqual(state3.reasoning_items[0]["encrypted_content"], "handle-2")

        # Captured text replays as the plain shape the endpoint demands back, so
        # the rebuilt body carries a reasoning item rather than a blob or nothing.
        replayed = responses_input(
            [{"role": "assistant", "content": None,
              "_responses_reasoning": state.reasoning_items,
              "tool_calls": [{"id": "call_1", "type": "function",
                              "function": {"name": "lookup", "arguments": "{}"}}]}],
            plain_reasoning=True,
        )
        self.assertEqual(replayed[0], state.reasoning_items[0])

    def test_legacy_chat_shaped_reasoning_is_replayed_as_a_plain_item(self):
        """A turn whose reasoning survived only under the Chat Completions field
        name is still replayed on a plain-reasoning transport. The message-level
        check credits ``reasoning_content``, so without this the turn passes the
        gate while the built body carries no reasoning item and the endpoint 400s
        "reasoning_text in the thinking mode must be passed back" (session
        d4f7de36-ed3e-483a-aac5-fb7849adacb4)."""
        message = {
            "role": "assistant", "content": None,
            "reasoning_content": "Open the entrypoint first.",
            "tool_calls": [{
                "id": "call_1", "type": "function",
                "function": {"name": "lookup", "arguments": "{}"},
            }],
        }
        items = responses_input([message], plain_reasoning=True)
        self.assertEqual(items[0], {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "Open the entrypoint first."}],
        })
        self.assertEqual(items[1]["type"], "function_call")

        # Off by default, so an encrypted-reasoning family never gets a
        # synthesized plain item...
        self.assertEqual(responses_input([message])[0]["type"], "function_call")
        # ...and a turn that already captured items is never duplicated.
        captured = dict(message, _responses_reasoning=[{
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "captured"}],
        }])
        self.assertEqual(
            [i for i in responses_input([captured], plain_reasoning=True)
             if i.get("type") == "reasoning"],
            [{"type": "reasoning",
              "content": [{"type": "reasoning_text", "text": "captured"}]}],
        )

        # The deepseek transport is wired to it end to end.
        body = build_responses_request(
            model="deepseek-flash", messages=[message], tools=None,
            max_output_tokens=100, provider_family_name="deepseek", stateful=False,
        )
        self.assertEqual(body["input"][0]["type"], "reasoning")

    def test_deepseek_plain_reasoning_reloads_from_persisted_output(self):
        item = {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "Use search_source."}],
        }
        output = json.dumps({"responses_reasoning": [item]})
        self.assertEqual(_extract_responses_reasoning(output), [item])

        malformed = json.dumps({
            "responses_reasoning": [{"type": "reasoning", "content": []}],
        })
        self.assertEqual(_extract_responses_reasoning(malformed), [])

    def test_response_failed_event_raises(self):
        with self.assertRaisesRegex(RuntimeError, "model unavailable"):
            response_event_chunks({
                "type": "response.failed",
                "response": {"error": {"code": "provider_error", "message": "model unavailable"}},
            }, _EventState())


    def test_deepseek_model_scoped_responses_rejection_is_fallback_eligible(self):
        self.assertTrue(is_unsupported_deepseek_responses_error(
            RuntimeError("Responses API HTTP 400: Model Not Exist"),
        ))
        self.assertFalse(is_unsupported_deepseek_responses_error(
            RuntimeError("Responses API HTTP 400: invalid tool schema"),
        ))

class ResponseChainPersistenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_api_key_raises_before_sending(self):
        sent = []

        async def handler(request):
            sent.append(request)
            return httpx.Response(200, text="data: [DONE]\n\n")

        with self.assertRaises(ValueError):
            await open_responses_stream(
                base_url="https://api.deepseek.com/v1", api_key="",
                family="deepseek", body={"model": "deepseek-v4-flash"},
                transport=httpx.MockTransport(handler),
            )
        self.assertEqual(sent, [])

    def test_headers_never_emit_empty_bearer(self):
        headers = _headers(
            api_key="", base_url="https://api.deepseek.com/v1", family="deepseek",
        )
        self.assertNotIn("Authorization", headers)
        self.assertEqual(
            _headers(
                api_key="secret", base_url="https://api.deepseek.com/v1",
                family="deepseek",
            )["Authorization"],
            "Bearer secret",
        )

    async def test_http_sse_adapter_streams_and_captures_response_id(self):
        async def handler(request):
            self.assertEqual(request.url.path, "/v1/responses")
            self.assertEqual(request.headers["authorization"], "Bearer secret")
            body = json.loads(request.content)
            self.assertTrue(body["stream"])
            sse = (
                'data: {"type":"response.created","response":{"id":"resp_1"}}\n\n'
                'data: {"type":"response.output_text.delta","delta":"hello"}\n\n'
                'data: {"type":"response.completed","response":{"id":"resp_1",'
                '"usage":{"input_tokens":4,"output_tokens":1}}}\n\n'
                'data: [DONE]\n\n'
            )
            return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

        stream = await open_responses_stream(
            base_url="https://example.test/v1", api_key="secret", family="xai",
            body={"model": "grok", "input": "hi", "stream": True},
            transport=httpx.MockTransport(handler),
        )
        chunks = [chunk async for chunk in stream]
        await stream.close()
        self.assertEqual(stream.response_id, "resp_1")
        self.assertEqual(chunks[0].choices[0].delta.content, "hello")
        self.assertEqual(chunks[1].usage.prompt_tokens, 4)

    async def test_chain_state_round_trips_without_clobbering_metadata(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            db = LocalBackend(f"{tmp}/test.db", seed=False)
            conn = db._get_conn()
            conn.execute(
                "INSERT INTO sessions (id,user_id,metadata) VALUES (?,?,?)",
                ("session", "user", json.dumps({"keep": True})),
            )
            conn.commit()
            conn.close()

            state = {
                "transport": "responses", "family": "xai", "identity": "identity",
                "model": "grok", "previous_response_id": "resp_1",
                "history_hash": "history", "updated_at": 1,
            }
            await db.set_session_response_chain("session", state)
            self.assertEqual(await db.get_session_response_chain("session"), state)

            await db.set_session_response_chain("session", None)
            self.assertIsNone(await db.get_session_response_chain("session"))
            conn = db._get_conn()
            metadata = json.loads(conn.execute(
                "SELECT metadata FROM sessions WHERE id='session'"
            ).fetchone()[0])
            conn.close()
            self.assertTrue(metadata["keep"])


if __name__ == "__main__":
    unittest.main()

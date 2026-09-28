"""Contract tests for the per-endpoint behaviour profile.

The profile exists because the loop used to take three decisions from a vendor
*name*: whether reasoning has to be replayed, whether a forced tool choice can
coexist with thinking, and therefore what the reasoning setting may be. Encoding
those as facts about the endpoint is what makes the combination that caused the
outage unrepresentable.
"""
import unittest

from app.agent.provider_profile import (
    REPLAY_OPTIONAL,
    REPLAY_REQUIRED,
    SHAPE_CHAT,
    SHAPE_ENCRYPTED,
    SHAPE_NONE,
    SHAPE_PLAIN,
    count_replay_items,
    forced_tool_choice_allowed,
    history_replay_violations,
    message_reasoning_count,
    reasoning_item_shape,
    request_uses_thinking,
    resolve_profile,
    responses_replay_violations,
)


def _body(*items, reasoning=None):
    body = {"input": list(items)}
    if reasoning is not None:
        body["reasoning"] = {"effort": reasoning}
    return body


_FC = {"type": "function_call", "name": "read_source", "call_id": "c1", "arguments": "{}"}
_REASONING = {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "why"}]}


class ProfileResolution(unittest.TestCase):
    def test_deepseek_responses_requires_replay_of_plain_text(self):
        p = resolve_profile(family="deepseek", transport="responses")
        self.assertEqual(p.reasoning_shape, SHAPE_PLAIN)
        self.assertEqual(p.replay_obligation, REPLAY_REQUIRED)
        self.assertTrue(p.requires_replay)
        self.assertFalse(p.forced_tool_choice_with_reasoning)

    def test_openai_responses_requires_encrypted_replay(self):
        p = resolve_profile(family="openai", transport="responses")
        self.assertEqual(p.reasoning_shape, SHAPE_ENCRYPTED)
        self.assertTrue(p.requires_replay)

    def test_chat_completions_never_demands_replay(self):
        for family in ("deepseek", "openai", "openrouter", "compatible"):
            p = resolve_profile(family=family, transport="chat_completions")
            self.assertEqual(p.replay_obligation, REPLAY_OPTIONAL)
            self.assertFalse(p.requires_replay)
            self.assertTrue(p.forced_tool_choice_with_reasoning)

    def test_only_deepseek_keeps_reasoning_on_the_chat_transport(self):
        # The field is DeepSeek's own; every other OpenAI-compatible endpoint
        # rejects it. The outgoing hygiene in provider_completion reads this.
        self.assertEqual(
            resolve_profile(family="deepseek", transport="chat_completions").reasoning_shape,
            SHAPE_CHAT,
        )
        for family in ("openai", "openrouter", "compatible"):
            self.assertEqual(
                resolve_profile(family=family, transport="chat_completions").reasoning_shape,
                SHAPE_NONE,
            )

    def test_unknown_endpoint_is_permissive(self):
        p = resolve_profile(family="", provider="mystery", transport="responses")
        self.assertEqual(p.replay_obligation, REPLAY_OPTIONAL)
        self.assertTrue(forced_tool_choice_allowed(p, model_supports=True))


class ForcedToolChoice(unittest.TestCase):
    def test_deepseek_responses_declines_forcing_even_when_the_model_supports_it(self):
        # The regression: claiming support here while a different function
        # disabled thinking to compensate is what poisoned the replay.
        p = resolve_profile(family="deepseek", transport="responses")
        self.assertFalse(forced_tool_choice_allowed(p, model_supports=True))

    def test_chat_completions_still_allows_forcing(self):
        p = resolve_profile(family="deepseek", transport="chat_completions")
        self.assertTrue(forced_tool_choice_allowed(p, model_supports=True))

    def test_reasoning_capable_models_still_decline_on_other_transports(self):
        p = resolve_profile(family="openrouter", transport="chat_completions")
        self.assertFalse(forced_tool_choice_allowed(p, model_supports=False))


class ReplayValidation(unittest.TestCase):
    def setUp(self):
        self.deepseek = resolve_profile(family="deepseek", transport="responses")

    def test_tool_calls_without_reasoning_are_a_violation(self):
        problems = responses_replay_violations(_body(_FC), self.deepseek)
        self.assertEqual(len(problems), 1)
        self.assertIn("function_call", problems[0])

    def test_tool_calls_with_reasoning_are_clean(self):
        self.assertEqual(
            responses_replay_violations(_body(_REASONING, _FC), self.deepseek), [],
        )

    def test_no_tool_calls_is_clean(self):
        self.assertEqual(responses_replay_violations(_body(), self.deepseek), [])

    def test_deliberate_non_thinking_is_not_rewritten(self):
        # A caller who turned thinking off should not have their tool history
        # rewritten by a rule that only applies in thinking mode.
        self.assertEqual(
            responses_replay_violations(_body(_FC, reasoning="none"), self.deepseek), [],
        )

    def test_explicit_thinking_effort_is_checked(self):
        self.assertEqual(
            len(responses_replay_violations(_body(_FC, reasoning="high"), self.deepseek)), 1,
        )

    def test_chat_completions_is_never_checked(self):
        chat = resolve_profile(family="deepseek", transport="chat_completions")
        self.assertEqual(responses_replay_violations(_body(_FC), chat), [])

    def test_counts(self):
        counts = count_replay_items(_body(_REASONING, _FC, _FC))
        self.assertEqual(counts["reasoning_items"], 1)
        self.assertEqual(counts["function_call_items"], 2)

    def test_thinking_default_depends_on_the_profile(self):
        self.assertTrue(request_uses_thinking(_body(), self.deepseek))



class HistoryReplayCheck(unittest.TestCase):
    """The same rule, applied to a message list.

    The migration and the pre-send check must not be able to disagree: if one
    repairs a history the other would reject, the outage is back with extra steps.
    """

    def setUp(self):
        self.profile = resolve_profile(family="deepseek", transport="responses")

    def test_tool_calls_without_reasoning_are_a_violation(self):
        history = [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                         "function": {"name": "t", "arguments": "{}"}}]}]
        self.assertEqual(len(history_replay_violations(history, self.profile)), 1)

    def test_reasoning_satisfies_it(self):
        history = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "t", "arguments": "{}"}}],
             "_responses_reasoning": [{"type": "reasoning", "content": [
                 {"type": "reasoning_text", "text": "why"}]}]},
        ]
        self.assertEqual(history_replay_violations(history, self.profile), [])

    def test_plain_reasoning_content_satisfies_it(self):
        history = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "t", "arguments": "{}"}}],
             "reasoning_content": "why"},
        ]
        self.assertEqual(history_replay_violations(history, self.profile), [])

    def test_a_plain_assistant_turn_without_reasoning_is_flagged(self):
        # DeepSeek checks every assistant turn, not only the ones carrying calls:
        # rewriting an unreplayable tool turn as prose was rejected again, which is
        # only explicable if plain assistant turns are checked too.
        problems = history_replay_violations(
            [{"role": "assistant", "content": "hi"}], self.profile)
        self.assertEqual(len(problems), 1)

    def test_the_same_plain_turn_is_fine_on_a_profile_that_does_not_check_it(self):
        # Scoped by trait, so providers whose rule really is call-scoped (the
        # encrypted-reasoning family) do not have history dropped needlessly.
        encrypted = resolve_profile(family="openai", transport="responses")
        self.assertEqual(
            history_replay_violations([{"role": "assistant", "content": "hi"}], encrypted), [],
        )

    def test_a_conversation_only_history_is_clean(self):
        self.assertEqual(
            history_replay_violations([{"role": "user", "content": "hi"}], self.profile), [],
        )

    def test_chat_transport_is_never_checked(self):
        chat = resolve_profile(family="deepseek", transport="chat_completions")
        history = [{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                         "function": {"name": "t", "arguments": "{}"}}]}]
        self.assertEqual(history_replay_violations(history, chat), [])

    def test_one_unpaired_turn_is_enough_to_fail_the_history(self):
        # This is the case the first, coarser version of this check missed: a
        # request carrying plenty of reasoning still fails if *one* turn replays
        # calls without its own. A live session proved it (250 reasoning items,
        # 63 function calls, still rejected).
        history = [
            {"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                  "function": {"name": "t", "arguments": "{}"}}],
             "_responses_reasoning": [{"type": "reasoning", "content": [
                 {"type": "reasoning_text", "text": "why"}]}]},
            {"role": "assistant", "tool_calls": [{"id": "c2", "type": "function",
                                                  "function": {"name": "u", "arguments": "{}"}}]},
        ]
        problems = history_replay_violations(history, self.profile)
        self.assertEqual(len(problems), 1)
        self.assertIn("1 assistant turn(s)", problems[0])

    def test_the_body_check_never_blocks_a_clean_history(self):
        # One-way consistency: the coarse body check must not be stricter than the
        # per-turn rule, or it would force repairs on requests that are fine.
        call = {"type": "function_call", "name": "t", "call_id": "c1", "arguments": "{}"}
        cases = [
            ([{"role": "assistant", "tool_calls": [{"id": "c1", "type": "function",
                                                    "function": {"name": "t", "arguments": "{}"}}],
               "_responses_reasoning": [{"type": "reasoning", "content": [
                   {"type": "reasoning_text", "text": "why"}]}]}],
             _body(_REASONING, call)),
            ([{"role": "user", "content": "hi"}], _body()),
        ]
        for history, body in cases:
            self.assertEqual(history_replay_violations(history, self.profile), [],
                             "fixture should be clean: %r" % (history,))
            self.assertEqual(responses_replay_violations(body, self.profile), [],
                             "the body check blocked a clean history: %r" % (body,))


class ReasoningShapeTests(unittest.TestCase):
    """Shape, not item count, decides whether a turn is replayable.

    The count-based version credited any item whose ``type`` was ``reasoning``, so
    a turn carrying an encrypted blob on a plain-thinking endpoint passed the
    check, was replayed verbatim, and collected a deterministic 400
    ("reasoning_text ... must be passed back"). These lock the shape rule in.
    """

    plain = resolve_profile(family="deepseek", transport="responses")
    encrypted = resolve_profile(family="openai", transport="responses")

    def test_classifier_names_each_shape(self):
        self.assertEqual(reasoning_item_shape(
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "t"}]}),
            SHAPE_PLAIN)
        self.assertEqual(reasoning_item_shape(
            {"type": "reasoning", "encrypted_content": "blob"}), SHAPE_ENCRYPTED)
        self.assertEqual(reasoning_item_shape({"type": "function_call"}), SHAPE_NONE)
        # An empty reasoning_text part is not replayable content.
        self.assertEqual(reasoning_item_shape(
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "  "}]}),
            SHAPE_NONE)

    def test_encrypted_item_does_not_satisfy_a_plain_endpoint(self):
        msg = {"role": "assistant", "content": "hi",
               "_responses_reasoning": [{"type": "reasoning", "encrypted_content": "blob"}]}
        self.assertEqual(message_reasoning_count(msg, self.plain), 0)
        self.assertEqual(len(history_replay_violations([msg], self.plain)), 1)

    def test_encrypted_item_satisfies_an_encrypted_endpoint(self):
        msg = {"role": "assistant", "content": "hi",
               "_responses_reasoning": [{"type": "reasoning", "encrypted_content": "blob"}]}
        self.assertEqual(message_reasoning_count(msg, self.encrypted), 1)
        self.assertEqual(history_replay_violations([msg], self.encrypted), [])

    def test_plain_item_satisfies_the_plain_endpoint(self):
        msg = {"role": "assistant", "content": "hi",
               "_responses_reasoning": [{"type": "reasoning", "content": [
                   {"type": "reasoning_text", "text": "why"}]}]}
        self.assertEqual(history_replay_violations([msg], self.plain), [])

    def test_chat_shaped_reasoning_still_counts_on_a_plain_endpoint(self):
        # The body builder synthesizes the plain item from it (responses_input),
        # so the turn is genuinely replayable and must not be dropped.
        msg = {"role": "assistant", "content": "hi", "reasoning_content": "why"}
        self.assertEqual(message_reasoning_count(msg, self.plain), 1)
        self.assertEqual(history_replay_violations([msg], self.plain), [])

    def test_without_a_profile_the_legacy_count_is_unchanged(self):
        msg = {"role": "assistant", "content": "hi",
               "_responses_reasoning": [{"type": "reasoning", "encrypted_content": "blob"}]}
        self.assertEqual(message_reasoning_count(msg), 1)

    def test_the_builder_does_not_ship_an_encrypted_blob_to_a_plain_endpoint(self):
        from app.agent.response_chaining import responses_input
        msg = {"role": "assistant", "content": "hi",
               "_responses_reasoning": [{"type": "reasoning", "encrypted_content": "blob"}],
               "reasoning_content": "why"}
        items = responses_input([msg], plain_reasoning=True)
        reasoning = [i for i in items if i.get("type") == "reasoning"]
        self.assertEqual(len(reasoning), 1)
        self.assertNotIn("encrypted_content", reasoning[0])
        self.assertEqual(reasoning[0]["content"],
                         [{"type": "reasoning_text", "text": "why"}])

    def test_the_builder_still_ships_the_blob_to_an_encrypted_endpoint(self):
        from app.agent.response_chaining import responses_input
        msg = {"role": "assistant", "content": "hi",
               "_responses_reasoning": [{"type": "reasoning", "encrypted_content": "blob"}]}
        items = responses_input([msg], plain_reasoning=False)
        reasoning = [i for i in items if i.get("type") == "reasoning"]
        self.assertEqual(reasoning, [{"type": "reasoning", "encrypted_content": "blob"}])

    def test_repair_drops_a_wrong_shape_turn_and_keeps_the_right_shape_one(self):
        from app.agent.session_history import prepare_portable_history
        # Provenance is a precondition for this test: the migration filter strips
        # private reasoning from any turn whose replay scope does not match the
        # target, *before* the shape check runs. A turn carrying no scope at all
        # is therefore stripped (and then dropped as unreplayable) regardless of
        # its shape -- which would test the provenance filter, not the shape
        # repair. Real assistant turns are stamped with the scope they were
        # produced under (loop.py:408, loop.py:7523), so pin it to the target.
        scope = "prov:deepseek"
        good = {"role": "assistant", "content": "ok",
                "_reasoning_replay_scope": scope,
                "_responses_reasoning": [{"type": "reasoning", "content": [
                    {"type": "reasoning_text", "text": "why"}]}]}
        bad = {"role": "assistant", "content": "bad",
               "_reasoning_replay_scope": scope,
               "_responses_reasoning": [{"type": "reasoning", "encrypted_content": "blob"}]}
        out, stats = prepare_portable_history(
            [{"role": "user", "content": "q"}, good, bad],
            current_scope=scope, current_provider="deepseek", current_model="m",
            target_profile=self.plain,
        )
        self.assertEqual(stats.get("unreplayable_turns_dropped"), 1)
        self.assertEqual(history_replay_violations(out, self.plain), [])
        self.assertIn("ok", [m.get("content") for m in out])


if __name__ == "__main__":
    unittest.main()

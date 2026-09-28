"""The migration's post-condition.

``prepare_portable_history`` transforms by *provenance* — did this turn come from
the target endpoint? — which is not the same question as "will the target accept
the result?". A turn can be labelled as the target's own and still carry none of
the reasoning the target requires back; that is precisely the shape that took the
app down, and the transform alone happily returned it.

These tests pin the post-condition: whatever the transform produces is checked
against the target profile and repaired once if it falls short.
"""
import unittest

from app.agent.provider_profile import (
    history_replay_violations,
    resolve_profile,
)
from app.agent.loop import _has_conversation_content
from app.agent.session_history import prepare_portable_history

SCOPE = "scope-deepseek"
REPLAY_REQUIRED = resolve_profile(family="deepseek", transport="responses")
NO_REPLAY = resolve_profile(family="deepseek", transport="chat_completions")

_CALL = {
    "id": "call-1", "type": "function",
    "function": {"name": "read_source", "arguments": '{"path": "app/main.py"}'},
}


def _poisoned():
    """A turn that kept its structured tool call but carries no reasoning."""
    return [
        {"role": "user", "content": "inspect the entrypoint"},
        {
            "role": "assistant", "content": None, "tool_calls": [dict(_CALL)],
            "_reasoning_replay_scope": SCOPE,
        },
        {"role": "tool", "tool_call_id": "call-1", "content": "def main(): ..."},
    ]


def _healthy():
    """The same turn, with the reasoning that produced it."""
    messages = _poisoned()
    messages[1] = dict(messages[1])
    messages[1]["_responses_reasoning"] = [{
        "type": "reasoning",
        "content": [{"type": "reasoning_text", "text": "Open the entrypoint first."}],
    }]
    return messages


def _migrate(messages, profile, scope=SCOPE):
    return prepare_portable_history(
        messages, current_scope=scope, current_provider="deepseek",
        current_model="deepseek-flash", target_profile=profile,
    )


class PostCondition(unittest.TestCase):
    def test_poisoned_history_is_repaired_for_a_replay_required_target(self):
        out, stats = _migrate(_poisoned(), REPLAY_REQUIRED)

        self.assertEqual(stats.get("target_profile_repaired"), 1)
        self.assertEqual(stats.get("unreplayable_turns_dropped"), 1)
        self.assertEqual(history_replay_violations(out, REPLAY_REQUIRED), [])
        # The turn is dropped, not rewritten: a rewritten turn is still an
        # assistant turn without reasoning, which is what the endpoint rejects.
        self.assertEqual([m for m in out if m.get("role") == "assistant"], [])
        self.assertEqual([m for m in out if m.get("role") == "tool"], [],
                         "the results of a dropped turn must go with it")
        self.assertEqual([m for m in out if m.get("role") == "user"],
                         [{"role": "user", "content": "inspect the entrypoint"}])

    def test_healthy_history_is_left_alone(self):
        out, stats = _migrate(_healthy(), REPLAY_REQUIRED)

        self.assertEqual(stats.get("target_profile_checked"), 1)
        self.assertNotIn("target_profile_repaired", stats)
        assistant = [m for m in out if m.get("role") == "assistant"][0]
        self.assertEqual(len(assistant["tool_calls"]), 1)
        self.assertEqual(len(assistant["_responses_reasoning"]), 1)

    def test_optional_replay_target_does_not_rewrite_tool_calls(self):
        out, stats = _migrate(_poisoned(), NO_REPLAY)

        self.assertNotIn("target_profile_repaired", stats)
        assistant = [m for m in out if m.get("role") == "assistant"][0]
        self.assertEqual(len(assistant["tool_calls"]), 1)

    def test_repair_is_idempotent(self):
        once, _ = _migrate(_poisoned(), REPLAY_REQUIRED)
        twice, stats = _migrate(once, REPLAY_REQUIRED)

        self.assertEqual(once, twice)
        # Second pass finds nothing left to repair.
        self.assertEqual(stats.get("target_profile_checked"), 1)
        self.assertNotIn("target_profile_repaired", stats)

    def test_plain_reasoning_content_also_satisfies_the_rule(self):
        messages = _poisoned()
        messages[1] = dict(messages[1])
        messages[1]["reasoning_content"] = "Open the entrypoint first."
        out, stats = _migrate(messages, REPLAY_REQUIRED)

        self.assertNotIn("target_profile_repaired", stats)
        self.assertEqual(history_replay_violations(out, REPLAY_REQUIRED), [])


class MixedHistory(unittest.TestCase):
    """Only the turns that cannot be replayed are rewritten."""

    def test_only_the_unpaired_turn_is_converted(self):
        messages = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": None, "tool_calls": [dict(_CALL)],
             "_responses_reasoning": [{"type": "reasoning", "content": [
                 {"type": "reasoning_text", "text": "why"}]}],
             "_reasoning_replay_scope": SCOPE},
            {"role": "tool", "tool_call_id": "call-1", "content": "first result"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"id": "call-2", "type": "function",
                             "function": {"name": "search_source", "arguments": "{}"}}],
             "_reasoning_replay_scope": SCOPE},
            {"role": "tool", "tool_call_id": "call-2", "content": "second result"},
        ]
        out, stats = _migrate(messages, REPLAY_REQUIRED)

        self.assertEqual(stats.get("target_profile_repaired"), 1)
        self.assertEqual(stats.get("unreplayable_turns_dropped"), 1)
        assistants = [m for m in out if m.get("role") == "assistant"]
        self.assertEqual(len(assistants), 1, "only the unreplayable turn goes")
        # The turn that carried its reasoning keeps its structured call...
        self.assertEqual(len(assistants[0]["tool_calls"]), 1)
        self.assertEqual(assistants[0]["tool_calls"][0]["id"], "call-1")
        # ...and its tool result stays with it.
        self.assertIn({"role": "tool", "tool_call_id": "call-1", "content": "first result"}, out)
        self.assertEqual(history_replay_violations(out, REPLAY_REQUIRED), [])


class ContentFloor(unittest.TestCase):
    """The drop pass must never hand back a content-free history.

    A resume replays the stored history with no user message of its own, so a
    window that compaction left as assistant/tool traffic only would drop to
    nothing and be sent as an empty request. The post-condition has to say so
    rather than return a history that is merely self-consistently labelled.
    """

    def test_assistant_only_history_is_flagged_as_emptied(self):
        messages = [
            {"role": "system", "content": "checkpoint: prior work"},
            {"role": "assistant", "content": "working"},
            {"role": "assistant", "content": "still working"},
        ]
        out, stats = _migrate(messages, REPLAY_REQUIRED)

        self.assertEqual(stats.get("target_profile_emptied"), 1)
        self.assertEqual([m for m in out if m.get("role") != "system"], [])
        self.assertEqual(history_replay_violations(out, REPLAY_REQUIRED), [])

    def test_a_surviving_user_turn_is_not_flagged(self):
        messages = [
            {"role": "system", "content": "checkpoint: prior work"},
            {"role": "user", "content": "carry on"},
            {"role": "assistant", "content": "working"},
        ]
        out, stats = _migrate(messages, REPLAY_REQUIRED)

        self.assertNotIn("target_profile_emptied", stats)
        self.assertEqual([m for m in out if m.get("role") == "user"],
                         [{"role": "user", "content": "carry on"}])

    def test_content_floor_accepts_a_conversational_turn(self):
        # The send-path predicate that gates BOTH transports: a real user or
        # assistant turn clears the floor.
        self.assertTrue(_has_conversation_content([
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
        ]))
        self.assertTrue(_has_conversation_content([
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "ok"},
        ]))

    def test_content_floor_refuses_a_system_only_history(self):
        # DeepSeek /responses answers 400 "Input items array must not be empty"
        # here, and a Chat Completions body with nothing but a system message is
        # rejected the same way — so the guard stops the send for either transport
        # instead of spending the whole resume ladder learning it.
        self.assertFalse(_has_conversation_content(
            [{"role": "system", "content": "checkpoint: prior work"}]))
        self.assertFalse(_has_conversation_content([]))

    def test_send_path_floor_accepts_a_conversational_turn(self):
        # The send-path predicate the loop guards with, on ANY transport: a real
        # user or assistant turn clears the floor.
        self.assertTrue(_has_conversation_content([
            {"role": "system", "content": "s"},
            {"role": "user", "content": "hi"},
        ]))
        self.assertTrue(_has_conversation_content([
            {"role": "system", "content": "s"},
            {"role": "assistant", "content": "ok"},
        ]))

    def test_send_path_floor_refuses_a_system_only_history(self):
        # DeepSeek /responses answers 400 here and Chat Completions rejects it the
        # same way; the guard must refuse the send for BOTH rather than replay the
        # identical request down the whole resume ladder.
        self.assertFalse(_has_conversation_content(
            [{"role": "system", "content": "checkpoint: prior work"}]))
        self.assertFalse(_has_conversation_content([]))


class ProvenanceTransform(unittest.TestCase):
    def test_a_foreign_turn_is_converted_then_dropped_for_a_strict_target(self):
        # Two stages, and the second is the point of the post-condition. The
        # provenance filter rewrites a foreign turn into prose (which is the right
        # answer for a target that does not check assistant turns), and the
        # deepseek profile then rejects that prose for the same reason it rejected
        # the structured call: an assistant turn with no reasoning at all.
        out, stats = _migrate(_poisoned(), REPLAY_REQUIRED, scope="some-other-scope")

        self.assertEqual(stats["tool_groups_converted"], 1)
        self.assertEqual(stats.get("unreplayable_turns_dropped"), 1)
        self.assertEqual(history_replay_violations(out, REPLAY_REQUIRED), [])
        self.assertEqual([m for m in out if m.get("role") == "assistant"], [])

    def test_no_target_profile_keeps_the_previous_behaviour(self):
        # Callers that do not know their target still get the provenance filter.
        out, stats = prepare_portable_history(
            _poisoned(), current_scope=SCOPE, current_provider="deepseek",
            current_model="deepseek-flash",
        )
        assistant = [m for m in out if m.get("role") == "assistant"][0]
        self.assertEqual(len(assistant["tool_calls"]), 1)
        self.assertNotIn("target_profile_checked", stats)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.loop import (
    NO_MODEL_ERROR_CODE,
    _assistant_output,
    _bounded_turn_snapshot,
    _canonical_tool_signature,
    _check_user_confirmed,
    _compact_active_run_messages,
    _finalize_retry_stream,
    _contains_unparsed_tool_call,
    _responses_tool_choice,
    _is_side_effecting_call,
    _loop_guard_blocks,
    _stream_agent_events_impl,
)
from app.agent.run_health import RunHealthTracker


class AgentLoopGuardrailTests(unittest.TestCase):
    def test_read_only_call_on_confirmation_tool_is_not_reserved(self):
        info = SimpleNamespace(destructive=False, requires_confirmation=True)
        self.assertFalse(_is_side_effecting_call(info, {"mutating": False}))

    def test_mutating_call_on_mixed_action_tool_is_reserved(self):
        info = SimpleNamespace(destructive=False, requires_confirmation=True)
        self.assertTrue(_is_side_effecting_call(info, {"mutating": True}))

    def test_reservation_classification_falls_back_conservatively(self):
        info = SimpleNamespace(destructive=False, requires_confirmation=True)
        self.assertTrue(_is_side_effecting_call(info, {}))

    def test_responses_transport_preserves_required_tool_choice(self):
        self.assertEqual(_responses_tool_choice("required"), "required")
        self.assertEqual(
            _responses_tool_choice({"type": "function", "function": {"name": "present_plan"}}),
            {"type": "function", "name": "present_plan"},
        )
        self.assertIsNone(_responses_tool_choice("auto"))

    def test_raw_tool_call_markup_requires_retry(self):
        self.assertTrue(_contains_unparsed_tool_call("<tool_call>{}</tool_call>"))
        self.assertTrue(_contains_unparsed_tool_call("before <TOOL_CALL>after"))
        self.assertFalse(_contains_unparsed_tool_call("Tool calls are recorded separately."))
        # Provider-native dialects the generic form missed. This is the exact
        # shape that leaked into session 8db6c74a's promoted final message:
        # DeepSeek delimits every DSML tag with U+FF5C FULLWIDTH VERTICAL LINE.
        # It must now be caught so the guard retries instead of letting the
        # markup reach the closing gate and partial the run.
        dsml = (
            "<\uff5c\uff5cDSML\uff5c\uff5c calls>\n"
            "<\uff5c\uff5cDSML\uff5c\uff5c invoke name=\"read_source\">\n"
            "<\uff5c\uff5cDSML\uff5c\uff5c parameter name=\"path\">ui/x.js"
            "</\uff5c\uff5cDSML\uff5c\uff5c parameter>\n"
            "</\uff5c\uff5cDSML\uff5c\uff5c invoke>\n"
            "</\uff5c\uff5cDSML\uff5c\uff5c calls>"
        )
        self.assertTrue(_contains_unparsed_tool_call(dsml))
        # Prose that merely names the dialect, with no delimiter, is not a hit.
        self.assertFalse(_contains_unparsed_tool_call("I wrote it in DSML format."))

    def test_contract_retry_finalizes_full_stream_snapshot(self):
        db = SimpleNamespace(update_interaction=AsyncMock())

        async def exercise():
            await _finalize_retry_stream(db, "assistant-1", "Please confirm.")

        import asyncio
        asyncio.run(exercise())
        db.update_interaction.assert_awaited_once_with(
            "assistant-1",
            content="Please confirm.",
            status="complete",
        )

    def test_typed_continue_approves_gated_tool_after_request(self):
        messages = [
            {"role": "user", "content": "make the change"},
            {"role": "assistant", "content": "Please confirm before I proceed."},
            {"role": "user", "content": "Continue"},
        ]
        self.assertTrue(_check_user_confirmed(messages, "write_source"))

    def test_canonical_tool_signature_ignores_json_order_and_applies_defaults(self):
        schema = {
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 10},
            }
        }
        implicit = _canonical_tool_signature(
            "session_search", '{"query":"chat"}', schema
        )
        explicit = _canonical_tool_signature(
            "session_search", '{"limit":10, "query":"chat"}', schema
        )
        self.assertEqual(implicit, explicit)

    def test_tool_name_streak_is_advisory_when_arguments_keep_changing(self):
        self.assertFalse(_loop_guard_blocks(
            identical=False, tool_budget=False, no_progress=False,
            oscillation=False, name_streak=True,
        ))

    def test_true_loop_signals_still_block_the_call(self):
        self.assertTrue(_loop_guard_blocks(
            identical=True, tool_budget=False, no_progress=False,
            oscillation=False, name_streak=False,
        ))
        self.assertTrue(_loop_guard_blocks(
            identical=False, tool_budget=False, no_progress=False,
            oscillation=True, name_streak=False,
        ))

    def test_intermediate_assistant_output_is_minimal(self):
        raw = _assistant_output(
            tool_calls=[{"function": {"name": "search", "arguments": "{}"}}]
        )
        payload = json.loads(raw)
        self.assertEqual(set(payload), {"role", "tool_calls"})
        self.assertNotIn("_sent_messages", payload)
        self.assertNotIn("_sent_tools", payload)
        self.assertNotIn("content", payload)

    def test_final_snapshot_is_bounded_and_keeps_newest_messages(self):
        messages = [
            {"role": "user", "content": f"old-{i}-" + ("x" * 4000)}
            for i in range(100)
        ]
        snapshot = _bounded_turn_snapshot(messages, [], max_bytes=20_000)
        encoded = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        self.assertLessEqual(len(encoded), 20_000)
        self.assertTrue(snapshot["_sent_messages"][-1]["content"].startswith("old-99-"))
        self.assertEqual(snapshot["_snapshot_truncated"]["messages_total"], 100)

    def test_final_snapshot_serialization_is_linear(self):
        messages = [
            {"role": "user", "content": f"message-{i}-" + ("x" * 2000)}
            for i in range(500)
        ]
        with patch("app.agent.loop.json.dumps", wraps=json.dumps) as dumps:
            _bounded_turn_snapshot(messages, [], max_bytes=20_000)
        self.assertLessEqual(dumps.call_count, len(messages) + 1)

    def test_run_health_detects_two_tool_oscillation_once(self):
        tracker = RunHealthTracker()
        event = None
        calls = (("read_source", "read|a"), ("search_source", "search|x")) * 3
        for tool, signature in calls:
            event = tracker.record_request(tool, turn=2, signature=signature) or event
        self.assertIsNotNone(event)
        self.assertEqual(event["reason"], "alternating_tool_loop")
        self.assertEqual(event["evidence"]["pattern"], "read_source<->search_source")
        self.assertIsNone(tracker.record_request(
            "read_source", turn=2, signature="read|a"
        ))

    def test_run_health_allows_alternating_tools_with_new_arguments(self):
        tracker = RunHealthTracker()
        events = [
            tracker.record_request(tool, turn=1, signature=signature)
            for tool, signature in (
                ("read_source", "read|a"), ("search_source", "search|a"),
                ("read_source", "read|b"), ("search_source", "search|b"),
                ("read_source", "read|c"), ("search_source", "search|c"),
            )
        ]
        self.assertTrue(all(event is None for event in events))

    def test_run_health_uses_sliding_error_window_and_cooldown(self):
        tracker = RunHealthTracker(error_window=4)
        self.assertIsNone(tracker.record_outcome(
            False, turn=1, tool="run_command", error_type="timeout", threshold=3
        ))
        tracker.record_outcome(True, turn=1, tool="read_source", threshold=3)
        tracker.record_outcome(False, turn=2, tool="run_command", threshold=3)
        event = tracker.record_outcome(
            False, turn=2, tool="run_command", error_type="timeout", threshold=3
        )
        self.assertEqual(event["evidence"]["failures_in_window"], 3)
        self.assertTrue(tracker.allow_watchdog(turn=2, cooldown_turns=2))
        self.assertFalse(tracker.allow_watchdog(turn=3, cooldown_turns=2))
        self.assertTrue(tracker.allow_watchdog(turn=4, cooldown_turns=2))


class ActiveRunCompactionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _kwargs(strategy, messages):
        return {
            "db": object(),
            "user_id": "user",
            "session_id": "session",
            "agent_id": "agent",
            "strategy": strategy,
            "settings": {
                "enabled": True,
                "compaction_enabled": True,
                "compact_target_tokens": 100,
            },
            "messages": messages,
            "runtime_system_start": 3,
            "available_tools": set(),
            "shared_system": "core",
            "capability_parts": ["capability"],
            "agent_system": "agent",
            "turn_parts": ["turn"],
        }

    async def test_below_threshold_does_not_call_compactor(self):
        compact = AsyncMock()
        strategy = SimpleNamespace(
            CONTEXT_COMPACT=compact,
            CONTEXT_STATUS=lambda _messages, _settings: {
                "tokens": 100, "line": "100 tokens",
            },
        )
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "current"},
            {"role": "assistant", "content": "progress"},
        ]

        rebuilt, boundary, info, status, pending = await _compact_active_run_messages(
            **self._kwargs(strategy, messages)
        )

        self.assertIs(rebuilt, messages)
        self.assertEqual(boundary, 3)
        self.assertIsNone(info)
        self.assertEqual(status["tokens"], 100)
        self.assertFalse(pending)
        compact.assert_not_awaited()

    async def test_successful_fold_rebases_prompt_without_duplicate_user(self):
        compact = AsyncMock(return_value={
            "summarised_rows": 4,
            "tokens_before": 200,
            "tokens_after": 40,
        })

        def context_status(messages, _settings):
            raw = any(m.get("content") == "old raw" for m in messages)
            return {
                "tokens": 200 if raw else 40,
                "line": "fresh gauge",
            }

        strategy = SimpleNamespace(
            CONTEXT_COMPACT=compact,
            CONTEXT_STATUS=context_status,
        )
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "old raw"},
            {"role": "assistant", "content": "old raw"},
            {"role": "system", "content": "runtime control"},
        ]
        compacted_history = [
            {"role": "system", "content": "checkpoint"},
            {"role": "user", "content": "current user"},
            {"role": "assistant", "content": "recent progress"},
        ]
        with patch(
            "app.agent.session_history.build_openai_history_from_session",
            new=AsyncMock(return_value=compacted_history),
        ) as history_builder:
            rebuilt, boundary, info, status, pending = await _compact_active_run_messages(
                **self._kwargs(strategy, messages)
            )

        self.assertFalse(pending)
        self.assertEqual(info["summarised_rows"], 4)
        self.assertEqual(status["tokens"], 40)
        self.assertFalse(any(m.get("content") == "old raw" for m in rebuilt))
        self.assertEqual(
            sum(m.get("content") == "current user" for m in rebuilt), 1,
        )
        self.assertEqual(rebuilt[boundary:], [
            {"role": "system", "content": "runtime control"},
        ])
        self.assertIn({"role": "system", "content": "fresh gauge"}, rebuilt)
        compact.assert_awaited_once()
        self.assertEqual(
            compact.await_args.args[3]["_compaction_trigger"], "automatic",
        )
        self.assertIsNone(history_builder.await_args.kwargs["agent_id"])

    async def test_failed_reload_retries_without_second_summary(self):
        compact = AsyncMock(return_value={
            "summarised_rows": 2,
            "tokens_before": 200,
            "tokens_after": 50,
        })
        strategy = SimpleNamespace(
            CONTEXT_COMPACT=compact,
            CONTEXT_STATUS=lambda _messages, _settings: {
                "tokens": 200, "line": "gauge",
            },
        )
        messages = [
            {"role": "system", "content": "core"},
            {"role": "user", "content": "current"},
            {"role": "assistant", "content": "progress"},
        ]
        history_builder = AsyncMock(side_effect=[
            RuntimeError("temporary read failure"),
            [{"role": "user", "content": "current"}],
        ])
        with patch(
            "app.agent.session_history.build_openai_history_from_session",
            new=history_builder,
        ):
            first = await _compact_active_run_messages(
                **self._kwargs(strategy, messages)
            )
            self.assertTrue(first[4])
            second = await _compact_active_run_messages(
                **self._kwargs(strategy, messages),
                rebase_required=True,
            )

        self.assertFalse(second[4])
        self.assertEqual(compact.await_count, 1)
        self.assertEqual(history_builder.await_count, 2)


class MissingModelMessageTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_model_persists_visible_system_error_before_failing(self):
        class FakeDB:
            authority_mode = ""

            def __init__(self):
                self.inserted = []

            async def get_session_execution_mode_history(self, _session_id):
                return []

            async def next_session_seq(self, session_id):
                self.session_id = session_id
                return 17

            async def insert_interaction(self, **values):
                self.inserted.append(values)
                return "system-error-1"

        db = FakeDB()
        with patch(
            "app.admin.settings.apply_provider_for_run",
            new=AsyncMock(return_value={
                "provider": "", "base_url": "", "api_key": "", "model": "",
            }),
        ):
            events = [
                event async for event in _stream_agent_events_impl(
                    user_id="admin",
                    session_id="session-one",
                    user_message="hello",
                    system_prompt="",
                    agent_id="",
                    db=db,
                    parent_interaction_id="user-message-1",
                )
            ]

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "error")
        self.assertEqual(events[0]["code"], NO_MODEL_ERROR_CODE)
        self.assertEqual(events[0]["stop_cause"], "failed")
        self.assertEqual(len(db.inserted), 1)
        row = db.inserted[0]
        self.assertEqual(row["role"], "system")
        self.assertEqual(row["source"], "system:error")
        self.assertEqual(row["session_seq"], 17)
        self.assertEqual(row["parent_id"], "user-message-1")
        self.assertIn("Standard model role not assigned", row["content"])


class TempDbSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_visualizer_sequence_backfill_uses_active_temp_db(self):
        from app.chat import execution as chat
        from app.db.local import LocalBackend

        with tempfile.TemporaryDirectory() as td:
            db = LocalBackend(db_path=str(Path(td) / "optimizer.db"))
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, title, metadata) VALUES (?, ?, ?, ?)",
                    ("optimizer-test", "admin", "test", "{}"),
                )
                conn.commit()
            finally:
                conn.close()

            interaction_id = await db.insert_interaction(
                "admin", "optimizer-test", role="assistant", content="hello"
            )
            await chat._emit_to_visualizers(
                "optimizer-test",
                {
                    "type": "db",
                    "role": "assistant",
                    "id": interaction_id,
                    "session_seq": 42,
                    "turn_id": "turn-1",
                    "turn_seq": 7,
                },
                db_override=db,
            )
            # A later completion/update event has its own replay sequence but
            # must not renumber the already-created interaction.
            await chat._emit_to_visualizers(
                "optimizer-test",
                {
                    "type": "db",
                    "op": "update_interaction",
                    "role": "assistant",
                    "id": interaction_id,
                    "session_seq": 99,
                    "turn_id": "turn-1",
                    "turn_seq": 64,
                },
                db_override=db,
            )

            conn = db._get_conn()
            try:
                row = conn.execute(
                    "SELECT session_seq, turn_id, turn_seq FROM interactions WHERE id=?",
                    (interaction_id,),
                ).fetchone()
            finally:
                conn.close()
            self.assertEqual(tuple(row), (42, "turn-1", 7))

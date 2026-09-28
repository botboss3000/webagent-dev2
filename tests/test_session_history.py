"""Unit tests for DB → OpenAI message history mapping."""

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

import app.agent.compaction as compaction
from datetime import datetime, timezone

from app.agent.session_history import (
    TOOL_MARKER,
    build_openai_history_from_session,
    interactions_to_openai_messages,
)
from app.agent.compaction import maybe_compact
from app.agent.context_control import estimate_tokens
from app.models.schemas import InteractionRecord


def _ir(
    iid: str,
    role: str,
    content: str,
    *,
    tool_name: str | None = None,
    tool_call_id: str | None = None,
    output: str | None = None,
    status: str | None = None,
) -> InteractionRecord:
    return InteractionRecord(
        id=iid,
        session_id="sess",
        parent_id=None,
        role=role,
        content=content,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        metadata=None,
        output=output,
        created_at=datetime.now(timezone.utc),
        status=status,
    )


class InteractionsToOpenaiMessagesTests(unittest.TestCase):
    def test_user_only(self) -> None:
        rows = [_ir("1", "user", "hello")]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(out, [{"role": "user", "content": "hello"}])

    def test_exclude_current_user(self) -> None:
        rows = [_ir("1", "user", "first"), _ir("2", "user", "second")]
        out = interactions_to_openai_messages(rows, exclude_interaction_ids={"2"})
        self.assertEqual(out, [{"role": "user", "content": "first"}])

    def test_orphan_streaming_assistant_is_not_replayed(self) -> None:
        rows = [
            _ir("1", "user", "fix it"),
            _ir("2", "assistant", "Please", status="streaming"),
            _ir("3", "assistant", "Completed answer", status="complete"),
        ]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(out, [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": "Completed answer"},
        ])

    def test_omits_internal_memory_tools(self) -> None:
        rows = [
            _ir("1", "user", "hi"),
            _ir("2", "tool", '{"query":"hi"}', tool_name="memory_search", tool_call_id=None),
        ]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(out, [{"role": "user", "content": "hi"}])

    def test_assistant_with_tool_calls_and_tool_results(self) -> None:
        spec = [{"name": "web_search", "args": json.dumps({"q": "cats"})}]
        assistant_content = "I'll search." + TOOL_MARKER + json.dumps(spec)
        rows = [
            _ir("u", "user", "find cats"),
            _ir("a", "assistant", assistant_content),
            _ir(
                "t",
                "tool",
                '{"results":[]}',
                tool_name="web_search",
                tool_call_id="call_abc",
            ),
        ]
        out = interactions_to_openai_messages(rows)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[0], {"role": "user", "content": "find cats"})
        self.assertEqual(out[1]["role"], "assistant")
        self.assertEqual(out[1]["content"], "I'll search.")
        self.assertEqual(len(out[1]["tool_calls"]), 1)
        self.assertEqual(out[1]["tool_calls"][0]["id"], "call_abc")
        self.assertEqual(out[1]["tool_calls"][0]["function"]["name"], "web_search")
        self.assertEqual(out[2]["role"], "tool")
        self.assertEqual(out[2]["tool_call_id"], "call_abc")

    def test_evicted_tool_output_remains_a_valid_result_pair(self) -> None:
        tool_calls = [{
            "id": "call_evicted",
            "type": "function",
            "function": {"name": "web_search", "arguments": '{"q":"cats"}'},
        }]
        notice = (
            "Stored tool output removed to manage storage. The original tool request "
            "is retained. Re-run only if the operation is safe and current output is needed."
        )
        rows = [
            _ir("a", "assistant", "", output=json.dumps({"tool_calls": tool_calls})),
            _ir("t", "tool", notice, tool_name="web_search", tool_call_id="call_evicted"),
        ]

        out = interactions_to_openai_messages(rows)

        self.assertEqual(out[0]["tool_calls"], tool_calls)
        self.assertEqual(out[1], {
            "role": "tool", "content": notice, "tool_call_id": "call_evicted",
        })


class OffTaskToolHidingTests(unittest.TestCase):
    """Closed-task tool results degrade to a placeholder in the model payload;
    current-task results stay in full. The DB keeps full output either way."""

    def setUp(self) -> None:
        import app.agent.session_history as sh
        self._sh = sh
        self._prev = sh._HIDE_OFF_TASK_TOOL_OUTPUTS
        sh._HIDE_OFF_TASK_TOOL_OUTPUTS = True

    def tearDown(self) -> None:
        self._sh._HIDE_OFF_TASK_TOOL_OUTPUTS = self._prev

    @staticmethod
    def _asst(iid: str, tool_id: str, arguments: str = '{"q":"x"}') -> InteractionRecord:
        calls = [{
            "id": tool_id, "type": "function",
            "function": {"name": "web_search", "arguments": arguments},
        }]
        return _ir(iid, "assistant", "", output=json.dumps({"tool_calls": calls}))

    def test_old_task_tool_output_replaced_with_placeholder(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "login source: " + "x" * 500,
                tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "now write a poem about the sea"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "poem draft", tool_name="generate",
                tool_call_id="call_new"),
        ]
        out = interactions_to_openai_messages(rows)

        new = next(m for m in out if m.get("tool_call_id") == "call_new")
        old_calls = [tc for m in out for tc in m.get("tool_calls", [])
                     if tc.get("id") == "call_old"]
        old_results = [m for m in out if m.get("tool_call_id") == "call_old"]
        self.assertEqual(old_calls, [])
        self.assertEqual(old_results, [])
        self.assertEqual(new["content"], "poem draft")

    def test_excluded_live_user_still_closes_the_previous_task(self) -> None:
        huge_arguments = json.dumps({"document": "secret old input " + "z" * 5000})
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old", json.dumps({"_context_reduced": True})),
            _ir("t1", "tool", "old source payload",
                tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated poem about the sea"),
        ]

        out = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "all"},
        )

        calls = [tc for m in out for tc in m.get("tool_calls", [])]
        results = [m for m in out if m.get("tool_call_id") == "call_old"]
        self.assertEqual(calls, [])
        self.assertEqual(results, [])

    def test_same_task_tool_outputs_stay_in_full(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_1"),
            _ir("t1", "tool", "first result payload",
                tool_name="read_source", tool_call_id="call_1"),
            _ir("u2", "user", "yes go ahead"),
            self._asst("a2", "call_2"),
            _ir("t2", "tool", "second result payload",
                tool_name="read_source", tool_call_id="call_2"),
        ]
        out = interactions_to_openai_messages(rows)

        contents = [m["content"] for m in out if m.get("role") == "tool"]
        self.assertEqual(contents, ["first result payload", "second result payload"])
        calls = [tc for m in out for tc in m.get("tool_calls", [])]
        self.assertEqual(
            [tc["function"]["arguments"] for tc in calls],
            ['{"q":"x"}', '{"q":"x"}'],
        )

    def test_recent_runs_bounds_evidence_inside_one_long_task(self) -> None:
        rows = []
        prompts = [
            "fix the login bug", "yes go ahead", "yes go ahead",
            "yes go ahead",
        ]
        for index, prompt in enumerate(prompts, 1):
            rows.extend([
                _ir(f"u{index}", "user", prompt),
                self._asst(f"a{index}", f"call_{index}"),
                _ir(f"t{index}", "tool", f"full result {index}",
                    tool_name="read_source", tool_call_id=f"call_{index}"),
            ])

        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "recent_runs",
            "full_evidence_runs": 2,
        })

        contents = {
            m["tool_call_id"]: m["content"] for m in out if m.get("role") == "tool"
        }
        self.assertNotIn("call_1", contents)
        self.assertNotIn("call_2", contents)
        self.assertEqual(contents["call_3"], "full result 3")
        self.assertEqual(contents["call_4"], "full result 4")

    def test_budget_reduces_current_run_when_it_exceeds_budget(self) -> None:
        rows = []
        for index, prompt in enumerate(
                ["fix the login bug", "yes go ahead"], 1):
            rows.extend([
                _ir(f"u{index}", "user", prompt),
                self._asst(f"a{index}", f"call_{index}"),
                _ir(f"t{index}", "tool", str(index) * 5000,
                    tool_name="read_source", tool_call_id=f"call_{index}"),
            ])

        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "token_budget",
            "full_evidence_token_budget": 1000,
        })
        contents = {
            m["tool_call_id"]: m["content"] for m in out if m.get("role") == "tool"
        }
        self.assertEqual(contents, {})

    def test_dependency_evidence_can_survive_closed_task_within_budget(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "dependency finding", tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "write a birthday poem"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "current finding", tool_name="read_source", tool_call_id="call_new"),
        ]
        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "hybrid", "full_evidence_token_budget": 1000,
            "dependency_interaction_ids": ["t1"],
        })
        contents = {m["tool_call_id"]: m["content"] for m in out if m.get("role") == "tool"}
        self.assertEqual(contents["call_old"], "dependency finding")
        self.assertEqual(contents["call_new"], "current finding")

    def test_task_progress_carryover_never_displaces_active_task_evidence(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "old" * 1700, tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated birthday poem"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "new" * 2200, tool_name="read_source", tool_call_id="call_new"),
        ]
        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "token_budget",
            "full_evidence_token_budget": 10000,
            "task_progress_completed_evidence_tokens": 1000,
        })
        contents = {m["tool_call_id"]: m["content"] for m in out if m.get("role") == "tool"}
        self.assertNotIn("call_old", contents)  # 5,100 chars exceeds 4,000-char carryover
        self.assertEqual(contents["call_new"], "new" * 2200)

    def test_task_progress_carryover_reduces_old_tool_pair_to_retrieval_note(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "old" * 1700, tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated birthday poem"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "new" * 2200, tool_name="read_source", tool_call_id="call_new"),
        ]
        base_settings = {
            "tool_evidence_policy": "token_budget",
            "full_evidence_token_budget": 10000,
        }

        reduced = interactions_to_openai_messages(rows, evidence_settings={
            **base_settings,
            "task_progress_completed_evidence_tokens": 0,
        })
        reduced_tool_ids = {m.get("tool_call_id") for m in reduced if m.get("role") == "tool"}
        reduced_assistant_text = "\n".join(
            str(m.get("content") or "") for m in reduced if m.get("role") == "assistant"
        )
        self.assertNotIn("call_old", reduced_tool_ids)
        self.assertIn("Result interaction: t1", reduced_assistant_text)
        self.assertIn('recall_compacted(interaction_id="t1")', reduced_assistant_text)
        self.assertNotIn("old" * 1700, reduced_assistant_text)

        retained = interactions_to_openai_messages(rows, evidence_settings={
            **base_settings,
            "task_progress_completed_evidence_tokens": 2000,
        })
        retained_tool_contents = {
            m["tool_call_id"]: m["content"] for m in retained if m.get("role") == "tool"
        }
        self.assertEqual(retained_tool_contents["call_old"], "old" * 1700)
        self.assertEqual(retained_tool_contents["call_new"], "new" * 2200)

    def test_default_policy_bounds_huge_current_run(self) -> None:
        rows = [_ir("u1", "user", "fix the login bug"), self._asst("a1", "call"),
                _ir("t1", "tool", "x" * 8000, tool_name="read_source", tool_call_id="call")]
        out = interactions_to_openai_messages(rows, evidence_settings={
            "tool_evidence_policy": "current_task", "full_evidence_token_budget": 1000})
        self.assertEqual(out, [{"role": "user", "content": "fix the login bug"}])
    def test_all_policy_keeps_closed_task_evidence(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "login source", tool_name="read_source",
                tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated poem about the sea"),
        ]
        out = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "all"})
        old = next(m for m in out if m.get("tool_call_id") == "call_old")
        self.assertEqual(old["content"], "login source")

    def test_context_estimate_counts_the_reduced_provider_payload(self) -> None:
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old", json.dumps({"source": "x" * 8000})),
            _ir("t1", "tool", "y" * 8000, tool_name="read_source",
                tool_call_id="call_old"),
            _ir("u2", "user", "write an unrelated poem about the sea"),
        ]
        full = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "all"})
        reduced = interactions_to_openai_messages(
            rows, evidence_settings={"tool_evidence_policy": "current_task"})

        # Context Control's composer/pipeline gauge receives this exact message
        # list, so its estimate reflects compacted arguments and results.
        self.assertLess(estimate_tokens(reduced), estimate_tokens(full) // 10)

    def test_anaphoric_followup_keeps_prior_tool_output_in_full(self) -> None:
        rows = [
            _ir("u1", "user", "summarize the project README"),
            self._asst("a1", "call_1"),
            _ir("t1", "tool", "project setup details",
                tool_name="read_source", tool_call_id="call_1"),
            _ir("u2", "user", "What does that imply for local setup?"),
        ]

        out = interactions_to_openai_messages(rows)

        tool_result = next(m for m in out if m.get("tool_call_id") == "call_1")
        self.assertEqual(tool_result["content"], "project setup details")
        self.assertEqual(
            out[1]["tool_calls"][0]["function"]["arguments"],
            '{"q":"x"}',
        )

    def test_synthetic_user_rows_never_open_a_boundary(self) -> None:
        # An orchestration wake-up mid-task must not split the task.
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_1"),
            _ir("t1", "tool", "first result payload",
                tool_name="read_source", tool_call_id="call_1"),
            _ir("u2", "user", "[orchestration event] helper finished",
                tool_call_id=None),
            self._asst("a2", "call_2"),
            _ir("t2", "tool", "second result payload",
                tool_name="read_source", tool_call_id="call_2"),
        ]
        out = interactions_to_openai_messages(rows)

        contents = [m["content"] for m in out if m.get("role") == "tool"]
        self.assertEqual(contents, ["first result payload", "second result payload"])

    def test_disabled_keeps_everything_in_full(self) -> None:
        self._sh._HIDE_OFF_TASK_TOOL_OUTPUTS = False
        rows = [
            _ir("u1", "user", "fix the login bug"),
            self._asst("a1", "call_old"),
            _ir("t1", "tool", "login source " + "y" * 300,
                tool_name="read_source", tool_call_id="call_old"),
            _ir("u2", "user", "now write a poem about the sea"),
            self._asst("a2", "call_new"),
            _ir("t2", "tool", "poem draft", tool_name="generate",
                tool_call_id="call_new"),
        ]
        out = interactions_to_openai_messages(rows)

        old = next(m for m in out if m.get("tool_call_id") == "call_old")
        self.assertIn("login source", old["content"])
        old_call = next(tc for m in out for tc in m.get("tool_calls", [])
                        if tc.get("id") == "call_old")
        self.assertEqual(old_call["function"]["arguments"], '{"q":"x"}')


class CompactionSummariserResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def test_uses_agent_system_assignment_not_user_legacy_roster_flag(self) -> None:
        system_row = {
            "entry_id": "system-worker",
            "provider": "deepseek",
            "model": "deepseek-v4-flash-vision-exp",
            "base_url": "https://api.deepseek.com/v1",
            "api_key": "secret",
        }
        with patch(
            "app.db.get_db",
            return_value=type("DB", (), {
                "get_session_agent_id": AsyncMock(return_value="agent-1"),
                "get_agent_by_id": AsyncMock(return_value={"id": "agent-1"}),
            })(),
        ), patch(
            "app.admin.settings.resolve_agent_role_config",
            AsyncMock(return_value=system_row),
        ):
            resolved = await compaction._resolve_summariser("admin", "sess", {})

        self.assertEqual(resolved["model"], system_row["model"])
        self.assertEqual(resolved["provider"], system_row["provider"])
        self.assertEqual(resolved["base_url"], system_row["base_url"])
        self.assertEqual(resolved["api_key"], system_row["api_key"])

    async def test_missing_agent_system_assignment_does_not_use_legacy_roster_flag(self) -> None:
        with patch(
            "app.db.get_db",
            return_value=type("DB", (), {
                "get_session_agent_id": AsyncMock(return_value="agent-1"),
                "get_agent_by_id": AsyncMock(return_value={"id": "agent-1"}),
            })(),
        ), patch(
            "app.admin.settings.resolve_agent_role_config",
            AsyncMock(return_value={}),
        ):
            resolved = await compaction._resolve_summariser("admin", "sess", {})

        self.assertEqual(resolved["model"], "")


class LargeSessionAssemblyTests(unittest.IsolatedAsyncioTestCase):
    async def test_manual_command_returns_canonical_result_for_http_delivery(self) -> None:
        from app.api.chat import _handle_compact_command

        class FakeDB:
            async def get_session_agent_id(self, _session_id):
                return "agent"
            async def get_agent_by_id(self, _agent_id):
                return {"metadata": {}}

        notice = {
            "status": "success",
            "trigger": "manual",
            "interaction_id": "compact-http",
            "provider_tokens_before": 76_243,
            "history_tokens_before": 44_982,
            "history_tokens_after": 31_142,
            "history_tokens_saved": 13_840,
            "detail": "History: 44,982 to 31,142 tokens.",
        }
        with (
            patch(
                "app.agent.context_control.get_context_settings",
                AsyncMock(return_value={"enabled": True}),
            ),
            patch.object(
                compaction,
                "maybe_compact",
                AsyncMock(return_value={
                    "tokens_before": 44_982,
                    "tokens_after": 31_142,
                }),
            ),
            patch.object(
                compaction,
                "persist_compaction_success",
                AsyncMock(return_value=notice),
            ),
        ):
            result = await _handle_compact_command(
                "user", "session", FakeDB(),
            )

        self.assertEqual(result["message"], notice["detail"])
        self.assertEqual(result["compaction"]["type"], "compaction_completed")
        self.assertEqual(result["compaction"]["session_id"], "session")
        self.assertEqual(result["compaction"]["interaction_id"], "compact-http")
        self.assertEqual(result["compaction"]["provider_tokens_before"], 76_243)
        self.assertNotIn("tokens_after", result["compaction"])

    async def test_successful_compaction_persists_and_emits_canonical_metrics(self) -> None:
        class FakeDB:
            def __init__(self):
                self.inserted = []

            async def next_session_seq(self, session_id):
                return 42

            async def insert_interaction(self, **values):
                self.inserted.append(values)
                return "compaction-notice"

        db = FakeDB()
        emitted = AsyncMock()
        with (
            patch.object(
                compaction, "_latest_provider_context_measurement",
                AsyncMock(return_value=(76_243, "2026-08-31T12:00:00Z")),
            ),
            patch.object(
                compaction, "projected_context_tokens",
                AsyncMock(return_value=76_243),
            ),
            patch.object(compaction, "_emit_compaction_completed", emitted),
        ):
            notice = await compaction.persist_compaction_success(
                db, "user", "session", {
                    "summarised_rows": 114,
                    "new_cars": 2,
                    "segments": 2,
                    "tokens_before": 44_982,
                    "tokens_after": 31_142,
                },
                trigger="manual",
            )

        self.assertEqual(notice["provider_tokens_before"], 76_243)
        self.assertNotIn("tokens_after", notice)
        self.assertEqual(notice["history_tokens_before"], 44_982)
        self.assertEqual(notice["history_tokens_after"], 31_142)
        self.assertEqual(notice["history_tokens_saved"], 13_840)
        self.assertEqual(notice["interaction_id"], "compaction-notice")
        self.assertIn("History: 44,982 -> 31,142 tokens", notice["detail"].replace(chr(0x2192), "->"))
        self.assertEqual(len(db.inserted), 1)
        inserted = db.inserted[0]
        self.assertEqual(inserted["source"], "system:compaction")
        self.assertEqual(inserted["session_seq"], 42)
        metadata = json.loads(inserted["metadata"])["compaction"]
        self.assertEqual(metadata["version"], 2)
        self.assertEqual(metadata["status"], "success")
        self.assertEqual(metadata["trigger"], "manual")
        self.assertEqual(metadata["history_tokens_saved"], 13_840)
        emitted.assert_awaited_once()
        self.assertEqual(emitted.await_args.args[1]["interaction_id"], "compaction-notice")

    async def test_all_success_triggers_emit_exactly_one_canonical_record(self) -> None:
        class FakeDB:
            def __init__(self):
                self.inserted = []
            async def next_session_seq(self, _session_id):
                return len(self.inserted) + 1
            async def insert_interaction(self, **values):
                self.inserted.append(values)
                return "row-" + str(len(self.inserted))

        for trigger in ("manual", "tool", "automatic", "memory_guard", "engine_hook"):
            db = FakeDB()
            emitted = AsyncMock()
            with (
                patch.object(
                    compaction, "_latest_provider_context_measurement",
                    AsyncMock(return_value=(10_000, "2026-08-31T12:00:00Z")),
                ),
                patch.object(
                    compaction, "projected_context_tokens",
                    AsyncMock(return_value=10_000),
                ),
                patch.object(compaction, "_emit_compaction_completed", emitted),
            ):
                notice = await compaction.persist_compaction_success(
                    db, "user", "session", {
                        "tokens_before": 8_000,
                        "tokens_after": 4_000,
                        "summarised_rows": 2,
                    },
                    trigger=trigger,
                )
            self.assertEqual(len(db.inserted), 1, trigger)
            self.assertEqual(
                json.loads(db.inserted[0]["metadata"])["compaction"]["trigger"],
                trigger,
            )
            self.assertEqual(notice["history_tokens_saved"], 4_000)
            emitted.assert_awaited_once()
            self.assertEqual(emitted.await_args.args[1]["trigger"], trigger)

    async def test_successive_compaction_uses_newer_projection_as_before(self) -> None:
        class FakeDB:
            async def next_session_seq(self, _session_id):
                return 43
            async def insert_interaction(self, **_values):
                return "compaction-second"

        projected = AsyncMock(return_value=62_403)
        with (
            patch.object(
                compaction, "_latest_provider_context_measurement",
                AsyncMock(return_value=(76_243, "2026-08-31T12:00:00Z")),
            ),
            patch.object(compaction, "projected_context_tokens", projected),
            patch.object(
                compaction, "_emit_compaction_completed", AsyncMock(),
            ),
        ):
            notice = await compaction.persist_compaction_success(
                FakeDB(), "user", "session", {
                    "tokens_before": 31_142,
                    "tokens_after": 26_142,
                    "summarised_rows": 5,
                },
                trigger="manual",
            )

        projected.assert_not_awaited()
        self.assertEqual(notice["provider_tokens_before"], 76_243)
        self.assertEqual(notice["history_tokens_after"], 26_142)

    async def test_context_projection_never_replaces_provider_usage(self) -> None:
        self.assertEqual(
            await compaction.projected_context_tokens(
                "user", "session", 76_243, "2026-08-31T12:00:00Z",
            ),
            76_243,
        )

    async def test_compaction_metrics_keep_provider_and_history_scopes_separate(self) -> None:
        notice = compaction._compaction_notice({
            "tokens_before": 49_491,
            "tokens_after": 5_225,
        }, 300_206, "automatic")
        self.assertEqual(notice["provider_tokens_before"], 300_206)
        self.assertNotIn("tokens_after", notice)
        self.assertEqual(notice["history_tokens_before"], 49_491)
        self.assertEqual(notice["history_tokens_after"], 5_225)
        self.assertEqual(notice["history_tokens_saved"], 44_266)

    async def test_cross_worker_lock_rejects_overlapping_forced_compaction(self) -> None:
        if os.name != "posix":
            self.skipTest("host file locks are POSIX-only")
        import fcntl
        import hashlib
        import tempfile

        session_id = "locked-session"
        key = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:32]
        path = os.path.join(
            tempfile.gettempdir(), f"webagent-compaction-{key}.lock",
        )
        handle = open(path, "a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            result = await compaction.maybe_compact(
                object(), "user", session_id, {
                    "enabled": True,
                    "compaction_enabled": True,
                    "token_limit": 100_000,
                },
                force=True,
            )
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

        self.assertEqual(result["status"], "in_progress")

    async def test_history_savings_do_not_rewrite_provider_context(self) -> None:
        notice = compaction._compaction_notice({
            "tokens_before": 25_000,
            "tokens_after": 1_000,
        }, 10_000, "manual")
        self.assertEqual(notice["provider_tokens_before"], 10_000)
        self.assertEqual(notice["history_tokens_saved"], 24_000)
        self.assertNotIn("tokens_after", notice)

    async def test_eighty_k_history_target_is_independent_of_provider_overhead(self) -> None:
        notice = compaction._compaction_notice({
            "tokens_before": 90_000, "tokens_after": 12_000,
            "history_target_tokens": 80_000, "protected_tail_tokens": 12_000,
            "target_met": True,
        }, 100_000, "automatic")
        self.assertEqual(notice["history_target_tokens"], 80_000)
        self.assertEqual(notice["provider_tokens_before"], 100_000)
        self.assertTrue(notice["target_met"])
        self.assertNotIn("provider_tokens_after", notice)

    async def test_five_k_verbatim_boundary_and_long_run_milestone_are_preserved(self) -> None:
        class Row:
            def __init__(self, iid, role, tokens):
                self.id = iid
                self.role = role
                self.tokens = tokens
                self.content = "content"
                self.tool_name = None
                self.source = None

        class FakeDB:
            def __init__(self, rows):
                self.rows = rows
                self.saved = None
            async def count_interactions(self, *_args):
                return len(self.rows)
            async def get_session_segments(self, *_args):
                return []
            async def fetch_interactions_from_offset(self, *_args):
                return list(self.rows)
            async def get_session_agent_id(self, *_args):
                return "agent"
            async def replace_session_segments(self, _user, _session, train):
                self.saved = train

        settings = {
            "enabled": True,
            "compaction_enabled": True,
            "token_limit": 20_000,
            "compact_target_tokens": 10_000,
            "verbatim_tail_tokens": 5_000,
            "segment_target_tokens": 2_000,
        }

        def span_tokens(rows):
            return sum(row.tokens for row in rows)


        async def run(rows):
            db = FakeDB(rows)
            with (
                patch.object(compaction, "_span_tokens", side_effect=span_tokens),
                patch.object(compaction, "_render_transcript", return_value="transcript"),
                patch.object(
                    compaction, "_resolve_summariser",
                    AsyncMock(return_value={"model": "system-worker"}),
                ),
                patch.object(compaction, "_summarise_checkpoint", AsyncMock(return_value=("summary", "topic"))),
            ):
                result = await compaction.maybe_compact(
                    db, "user", "boundary-session", settings, force=True,
                )
            return result, db.saved

        below, _ = await run([Row("u1", "user", 2_499), Row("a1", "assistant", 2_500)])
        exact, _ = await run([Row("u1", "user", 2_500), Row("a1", "assistant", 2_500)])
        above, train = await run([
            Row("u1", "user", 100), Row("a1", "assistant", 100),
            Row("u2", "user", 2_500), Row("a2", "assistant", 2_501),
        ])
        oversized_single, _ = await run([
            Row("u1", "user", 3_000), Row("a1", "assistant", 3_000),
        ])

        self.assertIsNone(below)
        self.assertIsNone(exact)
        self.assertIsNotNone(above)
        self.assertEqual(above["summarised_rows"], 3)
        self.assertEqual(train[0]["end_index"], 3)
        self.assertEqual(len(train), 1)
        self.assertIsNotNone(oversized_single)
        self.assertEqual(oversized_single["summarised_rows"], 1)

    async def test_long_run_skips_tool_call_boundaries(self) -> None:
        class Row:
            def __init__(self, iid, role, tokens, content="content", output=None):
                self.id = iid
                self.role = role
                self.tokens = tokens
                self.content = content
                self.output = output
                self.tool_name = None
                self.source = None

        class FakeDB:
            def __init__(self, rows):
                self.rows = rows
                self.saved = None
            async def count_interactions(self, *_args):
                return len(self.rows)
            async def get_session_segments(self, *_args):
                return []
            async def fetch_interactions_from_offset(self, *_args):
                return list(self.rows)
            async def get_session_agent_id(self, *_args):
                return "agent"
            async def replace_session_segments(self, _user, _session, checkpoint):
                self.saved = checkpoint

        rows = [
            Row("u1", "user", 100),
            Row("a-tool", "assistant", 2_000, "working [Tool calls: []"),
            Row("tool", "tool", 2_000),
            Row("a-milestone", "assistant", 100, "completed one phase"),
            Row("a-live-tool", "assistant", 2_000, "next phase [Tool calls: []"),
            Row("live-tool", "tool", 1_000),
        ]
        settings = {
            "enabled": True, "compaction_enabled": True,
            "token_limit": 20_000, "compact_target_tokens": 1_000,
            "verbatim_tail_tokens": 3_000, "segment_target_tokens": 2_000,
        }
        with (
            patch.object(compaction, "_span_tokens",
                         side_effect=lambda span: sum(row.tokens for row in span)),
            patch.object(compaction, "_render_transcript", return_value="transcript"),
            patch.object(
                compaction, "_resolve_summariser",
                AsyncMock(return_value={"model": "system-worker"}),
            ),
            patch.object(compaction, "_summarise_checkpoint", AsyncMock(return_value=("summary", "topic"))),
        ):
            db = FakeDB(rows)
            result = await compaction.maybe_compact(
                db, "user", "long-run", settings, force=True,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["summarised_rows"], 3)
        self.assertEqual(db.saved[0]["end_index"], 3)

    async def test_completed_tool_exchange_can_compact_within_one_user_turn(self) -> None:
        class Row:
            def __init__(self, iid, role, tokens, content="content", output=None):
                self.id, self.role, self.tokens = iid, role, tokens
                self.content, self.output = content, output
                self.tool_name, self.source = None, None

        class FakeDB:
            def __init__(self, rows): self.rows, self.saved = rows, None
            async def count_interactions(self, *_): return len(self.rows)
            async def get_session_segments(self, *_): return []
            async def fetch_interactions_from_offset(self, *_): return list(self.rows)
            async def get_session_agent_id(self, *_): return "agent"
            async def replace_session_segments(self, _u, _s, checkpoint): self.saved = checkpoint

        rows = [
            Row("u", "user", 100),
            Row("a1", "assistant", 2_000, "call [Tool calls: []"),
            Row("t1", "tool", 2_000),
            Row("a2", "assistant", 2_000, "call [Tool calls: []"),
            Row("t2", "tool", 2_000),
        ]
        with (
            patch.object(compaction, "_span_tokens", side_effect=lambda span: sum(r.tokens for r in span)),
            patch.object(compaction, "_render_transcript", return_value="transcript"),
            patch.object(compaction, "_resolve_summariser", AsyncMock(return_value={"model": "system-worker"})),
            patch.object(compaction, "_summarise_checkpoint", AsyncMock(return_value=("summary", "topic"))),
        ):
            db = FakeDB(rows)
            result = await compaction.maybe_compact(db, "user", "tool-tail", {
                "enabled": True, "compaction_enabled": True, "token_limit": 20_000,
                "compact_target_tokens": 1_000, "verbatim_tail_tokens": 3_000,
                "segment_target_tokens": 2_000,
            })

        self.assertEqual(result["summarised_rows"], 3)
        self.assertEqual(db.saved[0]["end_index"], 3)
        self.assertFalse(result["target_met"])
        self.assertEqual(result["history_target_tokens"], 1_000)
        self.assertEqual(result["protected_tail_tokens"], 4_000)

    async def test_repeated_compaction_replaces_the_rolling_checkpoint(self) -> None:
        class Row:
            def __init__(self, iid, role, tokens):
                self.id = iid
                self.role = role
                self.tokens = tokens
                self.content = "content"
                self.tool_name = None
                self.source = None

        rows = [
            Row("u2", "user", 100),
            Row("a2", "assistant", 100),
            Row("u3", "user", 2_500),
            Row("a3", "assistant", 2_501),
        ]

        class FakeDB:
            saved = None

            async def count_interactions(self, *_args):
                return 6

            async def get_session_segments(self, *_args):
                return [{
                    "seq": 0, "start_index": 0, "end_index": 2,
                    "summary": "checkpoint-v1", "token_estimate": 1_000,
                    "topic": "Earlier work", "tier": 1,
                }]

            async def fetch_interactions_from_offset(self, *_args):
                return list(rows)

            async def get_session_agent_id(self, *_args):
                return "agent"

            async def replace_session_segments(self, _user, _session, checkpoint):
                self.saved = checkpoint

        settings = {
            "enabled": True, "compaction_enabled": True,
            "token_limit": 20_000, "compact_target_tokens": 6_000,
            "verbatim_tail_tokens": 5_000, "segment_target_tokens": 2_000,
        }

        summarise = AsyncMock(return_value=("checkpoint-v2", "Current task"))
        with (
            patch.object(compaction, "_span_tokens",
                         side_effect=lambda span: sum(row.tokens for row in span)),
            patch.object(compaction, "_render_transcript", return_value="new aged turns"),
            patch.object(
                compaction, "_resolve_summariser",
                AsyncMock(return_value={"model": "system-worker"}),
            ),
            patch.object(compaction, "_summarise_checkpoint", summarise),
        ):
            db = FakeDB()
            result = await compaction.maybe_compact(
                db, "user", "rolling-checkpoint", settings, force=False,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["summarised_rows"], 3)
        self.assertEqual(result["segments"], 1)
        self.assertEqual(len(db.saved), 1)
        self.assertEqual(db.saved[0]["start_index"], 0)
        self.assertEqual(db.saved[0]["end_index"], 5)
        self.assertEqual(db.saved[0]["summary"], "checkpoint-v2")
        self.assertEqual(db.saved[0]["tier"], 1)
        checkpoint_input = summarise.await_args.args[0]
        self.assertIn("checkpoint-v1", checkpoint_input)
        self.assertIn("new aged turns", checkpoint_input)

    async def test_manual_compaction_collapses_legacy_cars_without_new_aged_rows(self) -> None:
        class Row:
            def __init__(self, iid, role, tokens):
                self.id = iid
                self.role = role
                self.tokens = tokens
                self.content = "content"
                self.tool_name = None
                self.source = None

        tail = [Row("u3", "user", 2_000), Row("a3", "assistant", 2_000)]
        legacy = [
            {"seq": 0, "start_index": 0, "end_index": 2,
             "summary": "first old car", "token_estimate": 900,
             "topic": "First", "tier": 0},
            {"seq": 1, "start_index": 2, "end_index": 4,
             "summary": "second old car", "token_estimate": 900,
             "topic": "Second", "tier": 0},
        ]

        class FakeDB:
            saved = None

            async def count_interactions(self, *_args):
                return 6

            async def get_session_segments(self, *_args):
                return list(legacy)

            async def fetch_interactions_from_offset(self, *_args):
                return list(tail)

            async def get_session_agent_id(self, *_args):
                return "agent"

            async def replace_session_segments(self, _user, _session, checkpoint):
                self.saved = checkpoint

        with (
            patch.object(compaction, "_span_tokens",
                         side_effect=lambda span: sum(row.tokens for row in span)),
            patch.object(
                compaction, "_resolve_summariser",
                AsyncMock(return_value={"model": "system-worker"}),
            ),
            patch.object(
                compaction, "_summarise_checkpoint",
                AsyncMock(return_value=("one checkpoint", "Earlier work")),
            ),
        ):
            db = FakeDB()
            result = await compaction.maybe_compact(
                db, "user", "legacy-collapse", {
                    "enabled": True, "compaction_enabled": True,
                    "token_limit": 20_000, "compact_target_tokens": 10_000,
                    "verbatim_tail_tokens": 5_000,
                    "segment_target_tokens": 2_000,
                },
                force=True,
            )

        self.assertIsNotNone(result)
        self.assertEqual(result["summarised_rows"], 0)
        self.assertEqual(result["segments"], 1)
        self.assertEqual(len(db.saved), 1)
        self.assertEqual(db.saved[0]["start_index"], 0)
        self.assertEqual(db.saved[0]["end_index"], 4)
        self.assertEqual(db.saved[0]["summary"], "one checkpoint")

    async def test_checkpoint_call_enforces_the_configured_output_ceiling(self) -> None:
        summarise_json = AsyncMock(return_value=("checkpoint", "Topic"))
        with patch.object(compaction, "_summarise_json", summarise_json):
            result = await compaction._summarise_checkpoint(
                "prior state and aged conversation",
                {"segment_target_tokens": 2_000},
                {},
                user_id="user",
                session_id="session",
                agent_id="agent",
            )

        self.assertEqual(result, ("checkpoint", "Topic"))
        self.assertEqual(summarise_json.await_args.kwargs["max_tokens"], 2_000)
        self.assertIn("CONTEXT CHECKPOINT", summarise_json.await_args.args[0])
        self.assertIn("prior state and aged conversation", summarise_json.await_args.args[1])

    async def test_forced_compaction_reports_when_a_fold_is_already_running(self) -> None:
        session_id = "compaction-in-flight"
        compaction._FOLD_IN_FLIGHT.add(session_id)
        try:
            forced = await maybe_compact(object(), "user", session_id, {}, force=True)
            automatic = await maybe_compact(object(), "user", session_id, {}, force=False)
        finally:
            compaction._FOLD_IN_FLIGHT.discard(session_id)

        self.assertEqual(forced, {
            "status": "in_progress",
            "reason": "A compaction pass is already running for this session.",
        })
        self.assertIsNone(automatic)

    async def test_compacted_prefix_adds_continuation_anchor_for_assistant_only_tail(self) -> None:
        tail = [_ir("tail-assistant", "assistant", "Completed prior step", status="complete")]

        class FakeDB:
            suffix_offsets = []

            async def count_interactions(self, user_id, session_id):
                return 1_000_000

            async def get_session_segments(self, user_id, session_id):
                return [{
                    "seq": 0,
                    "start_index": 0,
                    "end_index": 999_999,
                    "summary": "bounded earlier context",
                    "token_estimate": 3,
                    "topic": "Earlier work",
                    "tier": 1,
                }]

            async def fetch_interactions_from_offset(self, user_id, session_id, offset):
                self.suffix_offsets.append(offset)
                return tail

        db = FakeDB()
        out = await build_openai_history_from_session(db, "user", "session")

        self.assertEqual(db.suffix_offsets, [999_999])
        self.assertEqual(out[-2], {"role": "assistant", "content": "Completed prior step"})
        self.assertEqual(out[-1], {
            "role": "user",
            "content": "Continue the task using the compacted conversation context above.",
        })

    async def test_compacted_prefix_is_never_materialized(self) -> None:
        tail = [_ir("tail-user", "user", "latest request")]

        class FakeDB:
            full_fetches = 0
            suffix_offsets = []

            async def count_interactions(self, user_id, session_id):
                return 1_000_000

            async def get_session_segments(self, user_id, session_id):
                return [{
                    "seq": 0,
                    "start_index": 0,
                    "end_index": 999_999,
                    "summary": "bounded earlier context",
                    "token_estimate": 3,
                    "topic": "Earlier work",
                    "tier": 1,
                }]

            async def fetch_interactions_from_offset(self, user_id, session_id, offset):
                self.suffix_offsets.append(offset)
                return tail

            async def fetch_interactions(self, user_id, session_id):
                self.full_fetches += 1
                raise AssertionError("raw compacted prefix must not be fetched")

        db = FakeDB()
        out = await build_openai_history_from_session(db, "user", "session")

        self.assertEqual(db.full_fetches, 0)
        self.assertEqual(db.suffix_offsets, [999_999])
        self.assertEqual(out[-1], {"role": "user", "content": "latest request"})
        self.assertIn("bounded earlier context", out[0]["content"])

    async def test_compaction_check_reads_only_uncovered_suffix(self) -> None:
        tail = [_ir("tail-user", "user", "small hot tail")]

        class FakeDB:
            full_fetches = 0
            suffix_offsets = []

            async def count_interactions(self, user_id, session_id):
                return 1_000_000

            async def get_session_segments(self, user_id, session_id):
                return [{
                    "seq": 0,
                    "start_index": 0,
                    "end_index": 999_999,
                    "summary": "earlier context",
                    "token_estimate": 3,
                    "topic": "Earlier work",
                    "tier": 1,
                }]

            async def fetch_interactions_from_offset(self, user_id, session_id, offset):
                self.suffix_offsets.append(offset)
                return tail

            async def fetch_interactions(self, user_id, session_id):
                self.full_fetches += 1
                raise AssertionError("compaction check loaded summarized prefix")

        db = FakeDB()
        changed = await maybe_compact(db, "user", "session", {
            "enabled": True,
            "compaction_enabled": True,
            "token_limit": 1_000_000,
            "compact_target_tokens": 850_000,
            "verbatim_tail_tokens": 300_000,
        })

        self.assertIsNone(changed)
        self.assertEqual(db.full_fetches, 0)
        self.assertEqual(db.suffix_offsets, [999_999])


if __name__ == "__main__":
    unittest.main()

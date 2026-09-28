import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db.local import LocalBackend
from app.tools.loader import ToolInfo
from app.agent import task_state
from app.agent.loop import _stream_agent_events_impl


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
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=delta)],
    )


def _text_chunk(text):
    delta = SimpleNamespace(
        content=text,
        reasoning_content=None,
        tool_calls=None,
    )
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def _checkpoint_args(final=False):
    args = {
        "steps": [{
            "id": "S1",
            "text": "Complete the requested task",
            "requirement_ids": ["R1"],
            "depends_on": [],
            "status": "done" if final else "in_progress",
            "verification": "Verify the task result",
        }],
        "active_step": "S1",
        "next_action": "Verify the task result" if final else "Continue the active task",
        "summary": (
            "The task is complete and the saved result is ready for review."
            if final else
            "The request is planned and the next work action is ready to run."
        ),
    }
    if final:
        args["coverage"] = [{
            "requirement_id": "R1",
            "evidence_ids": [],
            "answer": "The requested work was completed.",
        }]
    return args


class LoopCheckpointCadenceTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, actions, max_turns=12, browser_authority=False, user_message="Do the work"):
        with tempfile.TemporaryDirectory() as td:
            db = LocalBackend(str(Path(td) / "loop.db"), seed=False)
            if browser_authority:
                db.authority_mode = "browser"
            conn = db._get_conn()
            conn.execute(
                "INSERT INTO sessions (id, user_id, metadata) VALUES (?, ?, ?)",
                ("session-1", "admin", json.dumps({"execution_mode": "auto"})),
            )
            conn.commit()
            conn.close()

            work_calls = []
            provider_calls = []
            checkpoint_revisions = []
            checkpoint_mutations = []
            action_queue = list(actions)

            async def read_source(**kwargs):
                work_calls.append(kwargs)
                return "source result"

            async def update_checkpoint(expected_revision, update):
                checkpoint_revisions.append(expected_revision)
                if checkpoint_mutations.pop(0):
                    task_state.record_attempt(db, "session-1", "turn-1")
                task_state.update_checkpoint(
                    db, "session-1", "turn-1", expected_revision, update
                )
                return json.dumps({"saved": True})

            tools = {
                "read_source": ToolInfo(
                    name="read_source",
                    handler=read_source,
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                "update_task_checkpoint": ToolInfo(
                    name="update_task_checkpoint",
                    handler=update_checkpoint,
                    parameters={"type": "object", "properties": {
                        "expected_revision": {"type": "integer"},
                        "update": {"type": "object"},
                    }, "required": ["expected_revision", "update"]},
                ),
                "get_task_checkpoint": ToolInfo(
                    name="get_task_checkpoint",
                    handler=lambda **kwargs: "{}",
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
            }

            async def fake_open_stream(create_kwargs, timeout_s, client=None):
                provider_calls.append(create_kwargs)
                action = action_queue.pop(0) if action_queue else {"text": "done"}
                if action.get("tool"):
                    args = action.get("args", {})
                    if action["tool"] == "update_task_checkpoint":
                        checkpoint_mutations.append(bool(action.get("mutate_before_execute")))
                        update = _checkpoint_args(final=bool(action.get("final")))
                        if action.get("blocker"):
                            update["blockers"] = [action["blocker"]]
                        args = {
                            "expected_revision": 999,
                            "update": update,
                        }
                    chunk = _tool_chunk(
                        action["tool"], args,
                        call_id="call-%d" % len(provider_calls),
                    )
                else:
                    chunk = _text_chunk(action.get("text", "done"))
                return _Stream(chunk)

            async def fake_provider(*args, **kwargs):
                return {
                    "provider": "test",
                    "base_url": "https://provider.example/v1",
                    "api_key": "test-key",
                    "model": "test-model",
                }

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
                patch("app.agent.manager_config.resolve_manager_loop", return_value={"enabled": True, "task_progress": {"enabled": True}}),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8], patches[9]:
                events = [
                    event async for event in _stream_agent_events_impl(
                        user_id="admin",
                        session_id="session-1",
                        user_message=user_message,
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
            state = task_state.current_state(db, "session-1", "turn-1")
            return state, work_calls, provider_calls, checkpoint_revisions, events

    async def test_normal_tools_remain_available_without_initial_checkpoint(self):
        state, work_calls, provider_calls, checkpoint_revisions, events = await self._run([
            {"tool": "read_source"},
            {"tool": "read_source"},
        ])
        self.assertEqual(len(work_calls), 2)
        self.assertGreaterEqual(len(provider_calls), 2)
        self.assertEqual(provider_calls[0]["tool_choice"], "required")
        self.assertTrue(all(call["tool_choice"] == "auto" for call in provider_calls[1:]))
        self.assertFalse(any(event.get("stop_cause") == "checkpoint_failed" for event in events))
        self.assertEqual(state.get("checkpoint_revision", 0), 0)

    async def test_required_tool_rejects_prose_without_progress(self):
        _state, _work_calls, provider_calls, _checkpoint_revisions, events = await self._run([
            {"text": "I'll start searching now."},
            {"text": "The inspection is complete."},
        ])
        self.assertEqual(len(provider_calls), 2)
        self.assertEqual(provider_calls[0]["tool_choice"], "required")
        self.assertEqual(provider_calls[1]["tool_choice"], "required")
        self.assertTrue(any(event.get("step") == "work_policy_protocol_retry" for event in events))
        self.assertTrue(any(event.get("type") == "error"
                            and event.get("stop_cause") == "work_not_started"
                            for event in events))
        self.assertFalse(any(event.get("type") == "response" for event in events))

    async def test_forced_work_tool_restores_normal_final_response(self):
        _state, work_calls, provider_calls, _checkpoint_revisions, events = await self._run([
            {"text": "I will inspect the source now."},
            {"tool": "read_source"},
            {"text": "The inspection is complete."},
        ])
        self.assertEqual(len(work_calls), 1)
        self.assertEqual([call["tool_choice"] for call in provider_calls],
                         ["required", "required", "auto"])
        self.assertTrue(any(event.get("type") == "response"
                            and event.get("content") == "The inspection is complete."
                            for event in events))

    async def test_durable_task_blocker_allows_prose(self):
        _state, _work_calls, provider_calls, _checkpoint_revisions, events = await self._run([
            {"tool": "update_task_checkpoint", "blocker": "Need source inspection"},
            {"text": "The next step is to inspect the source."},
            {"text": "I need the repository path from you before I can continue."},
        ])
        self.assertEqual(len(provider_calls), 2)
        self.assertFalse(any(event.get("type") == "error" for event in events))
        responses = [event.get("content") for event in events if event.get("type") == "response"]
        self.assertEqual(responses, ["The next step is to inspect the source."])

    async def test_checkpoint_without_durable_gap_satisfies_minimum_work(self):
        _state, work_calls, provider_calls, _checkpoint_revisions, events = await self._run([
            {"tool": "update_task_checkpoint"},
            {"text": "The task needs more work."},
            {"tool": "read_source"},
            {"text": "The source was inspected."},
        ])
        self.assertEqual(len(provider_calls), 2)
        self.assertEqual(len(work_calls), 0)
        self.assertFalse(any(event.get("step") == "work_policy_protocol_retry" for event in events))

    async def test_completed_task_reply_finalizes_without_retry(self):
        _state, _work_calls, provider_calls, _checkpoint_revisions, events = await self._run([
            {"tool": "update_task_checkpoint", "final": True},
            {"text": "The task is complete."},
        ])
        self.assertEqual(len(provider_calls), 2)
        self.assertFalse(any(
            event.get("step") == "incomplete_task_response_retry" for event in events
        ))
        self.assertTrue(any(
            event.get("type") == "response" and event.get("content") == "The task is complete."
            for event in events
        ))

    async def test_no_task_conversational_reply_finalizes_immediately(self):
        _state, _work_calls, provider_calls, _checkpoint_revisions, events = await self._run(
            [{"text": "Here is the answer."}], browser_authority=True, user_message="Why does this happen?",
        )
        self.assertEqual(len(provider_calls), 1)
        self.assertFalse(any(
            event.get("step") == "incomplete_task_response_retry" for event in events
        ))
        self.assertTrue(any(
            event.get("type") == "response" and event.get("content") == "Here is the answer."
            for event in events
        ))

    async def test_serialized_tool_call_is_detected_but_never_executed(self):
        _state, work_calls, provider_calls, _checkpoint_revisions, events = await self._run([
            {"text": "<tool_call name=\"read_source\">{}</tool_call>"},
            {"text": "Inspection complete."},
        ])
        self.assertEqual(len(work_calls), 0)
        self.assertEqual(len(provider_calls), 2)
        self.assertTrue(any(event.get("step") == "raw_tool_call_retry" for event in events))
        self.assertTrue(any(event.get("stop_cause") == "work_not_started" for event in events))
        self.assertFalse(any(event.get("type") == "response" for event in events))

    async def test_checkpoint_cadence_never_stops_normal_work(self):
        actions = ([{"tool": "update_task_checkpoint"}]
                   + [{"tool": "read_source"}] * 10)
        state, work_calls, provider_calls, checkpoint_revisions, events = await self._run(actions)
        self.assertEqual(len(work_calls), 10)
        self.assertEqual(provider_calls[0]["tool_choice"], "required")
        self.assertTrue(all(call["tool_choice"] == "auto" for call in provider_calls[1:]))
        self.assertFalse(any(event.get("stop_cause") == "checkpoint_failed" for event in events))
        self.assertEqual(state["checkpoint_revision"], 1)
        self.assertEqual(checkpoint_revisions, [1])

    async def test_optional_checkpoint_resets_projected_cadence(self):
        actions = ([{"tool": "update_task_checkpoint"}]
                   + [{"tool": "read_source"}] * task_state.MAX_UNCHECKPOINTED_OUTCOMES
                   + [{"tool": "update_task_checkpoint", "final": True},
                      {"text": "Finished"}])
        state, work_calls, provider_calls, checkpoint_revisions, events = await self._run(actions)
        self.assertEqual(len(work_calls), task_state.MAX_UNCHECKPOINTED_OUTCOMES)
        self.assertEqual(state["checkpoint_revision"], 2)
        self.assertEqual(state["checkpoint"]["uncheckpointed_events"], 0)
        self.assertEqual(provider_calls[0]["tool_choice"], "required")
        self.assertEqual(provider_calls[-1]["tool_choice"], "auto")
        self.assertEqual(len(checkpoint_revisions), 2)
        self.assertTrue(any(event.get("type") == "stream" for event in events))


    async def test_distinct_single_tool_inspection_streak_remains_executable(self):
        # A browser/source journey can make many distinct calls to one tool.
        # The old same-tool streak guard blocked call 30 and then escalated it
        # into a terminal stop. Only actual repeats should be rejected.
        actions = [
            {"tool": "read_source", "args": {"path": f"file-{i}.txt"}}
            for i in range(31)
        ] + [{"text": "Finished inspection"}]
        state, work_calls, provider_calls, checkpoint_revisions, events = await self._run(
            actions, max_turns=40,
        )
        self.assertEqual(len(work_calls), 31)
        self.assertFalse(any(
            event.get("step") == "stall_guard_stop" for event in events
        ))
        self.assertTrue(any(
            event.get("step") == "tool_streak_advisory" for event in events
        ))

    async def test_checkpoint_execution_still_uses_cas_after_snapshot_rewrite(self):
        state, work_calls, provider_calls, checkpoint_revisions, events = await self._run([
            {"tool": "update_task_checkpoint", "mutate_before_execute": True},
            {"tool": "update_task_checkpoint"},
            {"text": "Finished"},
        ])
        self.assertEqual(work_calls, [])
        self.assertEqual(checkpoint_revisions, [1, 2])
        self.assertEqual(state["checkpoint_revision"], 1)
        failures = [
            event for event in events
            if event.get("type") == "tool_result"
            and event.get("tool") == "update_task_checkpoint"
            and event.get("error")
        ]
        self.assertTrue(failures)

if __name__ == "__main__":
    unittest.main()

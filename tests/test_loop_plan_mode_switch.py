"""End-to-end coverage for MID-TURN execution-mode switches.

Two DIFFERENT code paths can flip a run's mode while it is already in flight:

  Path A — the agent calls the ``set_execution_mode`` tool (the canonical case:
           Plan -> Auto the moment the user approves a plan). ``loop.py``
           re-derives EVERY mode-dependent local here via ``mode_gate_locals``.

  Path B — the user flips the chat pill, which persists the mode through
           ``POST /api/v1/chat/session-mode``. The loop re-reads the persisted
           mode before each tool call, but only into the PERMISSION locals
           (``_current_mode`` / ``_current_policy``) — never into the contract
           locals the completion gate reads (``_requires_plan_document``).

These tests drive REAL turns through ``_stream_agent_events_impl`` with a
scripted provider, so they exercise the actual completion gate rather than a
mock of it. No server restart is needed — the loop source is imported in-process.

They live in their own file on purpose: ``tests/test_loop_checkpoint_cadence.py``
is pre-broken at HEAD (its ``_task_enabled`` resolves False, so its returned
``state`` is None and 8+ cases fail for reasons unrelated to mode switching).
Editing that suite would poison attribution, so nothing here touches it.

Two harness hazards, both handled below:

  1. ``app/agent/turn_reservations.py`` keeps a DURABLE, PROCESS-SHARED sqlite
     store (``data/db/turn_reservations.sqlite``) of side-effecting tool
     receipts, keyed by a stable turn key. Reusing a ``session_id`` /
     ``turn_id`` across tests makes a later identical call REPLAY an earlier
     result instead of running the handler. Every test therefore uses a unique
     session + turn id.
  2. The plan-mode blocker is emitted as a ``response`` event carrying
     ``terminal_outcome="contract_blocked"`` and ``contract_blocker_reason``,
     NOT a ``stop_cause``. Assertions read those fields.
"""

import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.db.local import LocalBackend
from app.tools.loader import ToolInfo
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
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def _text_chunk(text):
    delta = SimpleNamespace(content=text, reasoning_content=None, tool_calls=None)
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=delta)])


def _steps(events):
    return [e.get("step") for e in events if e.get("step")]


def _blocker_reasons(events):
    """Every contract-blocker reason emitted as a terminal response."""
    return [
        e.get("contract_blocker_reason") for e in events
        if e.get("contract_blocker_reason")
    ]


def _final_responses(events):
    """Non-blocker response events — i.e. a normal completed turn."""
    return [
        e for e in events if e.get("type") == "response"
        and e.get("terminal_outcome") != "contract_blocked"
    ]


def _tool_results(events, tool_name):
    return [
        e for e in events
        if e.get("type") == "tool_result" and e.get("tool") == tool_name
    ]


# The approval ask that must PRECEDE the user's go-ahead, or the Plan->Auto
# escalation gate blocks the switch and every Path A assertion fails for the
# wrong reason. `_check_user_confirmed` requires ask -> later user confirm.
_APPROVAL_ASK = [{
    "role": "assistant",
    "content": "Shall I switch to Auto and carry out the plan now?",
}]
_APPROVAL_USER = "Yes, go ahead and switch to auto."


class PlanModeSwitchE2ETests(unittest.IsolatedAsyncioTestCase):
    async def _run(
        self, actions, *, execution_mode="plan", session_mode="plan",
        user_message="Draft a plan.", history=None, max_turns=14,
    ):
        """Drive one real turn through the loop with a scripted provider.

        ``execution_mode`` is the mode the RUN starts in; ``session_mode`` is
        what is persisted on the session row (normally identical — the pill path
        mutates the row mid-turn).
        """
        # Unique per test: a shared turn key would let the durable reservation
        # store replay a previous test's side effect instead of executing.
        sid = "session-" + uuid.uuid4().hex
        tid = "turn-" + uuid.uuid4().hex

        with tempfile.TemporaryDirectory() as td:
            db = LocalBackend(str(Path(td) / "loop.db"), seed=False)
            conn = db._get_conn()
            conn.execute(
                "INSERT INTO sessions (id, user_id, metadata) VALUES (?, ?, ?)",
                (sid, "admin", json.dumps({"execution_mode": session_mode})),
            )
            conn.commit()
            conn.close()

            calls = []            # (tool_name, kwargs) in execution order
            provider_calls = []
            mode_flips = []
            action_queue = list(actions)

            async def read_source(**kwargs):
                calls.append(("read_source", kwargs))
                return "source result"

            async def write_source(**kwargs):
                calls.append(("write_source", kwargs))
                return "written"

            async def present_plan(**kwargs):
                # Mirrors the real core handler's result contract: the loop only
                # clears the Plan obligation when status == "ok".
                calls.append(("present_plan", kwargs))
                return json.dumps({"status": "ok", "task_progress": "unavailable"})

            async def set_execution_mode(mode, reason=""):
                # Mirrors the real core wrapper's result contract, which the
                # loop parses to decide whether to re-derive the mode locals.
                calls.append(("set_execution_mode", {"mode": mode, "reason": reason}))
                await db.set_session_execution_mode(sid, mode, reason)
                return json.dumps({"status": "ok", "execution_mode": mode})

            async def pill_flip_mode(**kwargs):
                # Simulates the UI pill: persists the mode OUT OF BAND, exactly
                # as POST /api/v1/chat/session-mode does. It never touches the
                # run's contract locals — only the permission re-read sees it.
                calls.append(("pill_flip_mode", kwargs))
                await db.set_session_execution_mode(sid, "auto", "pill")
                mode_flips.append("auto")
                return json.dumps({"status": "ok"})

            tools = {
                "read_source": ToolInfo(
                    name="read_source", handler=read_source,
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
                "write_source": ToolInfo(
                    name="write_source", handler=write_source,
                    parameters={"type": "object", "properties": {}, "required": []},
                    destructive=True,
                ),
                # present_plan is Tier-1 core in production, so it is always in
                # the tool dict. Omitting it sends the loop down the
                # `plan_document_tool_unavailable` branch instead of the real
                # completion gate this suite exists to exercise.
                "present_plan": ToolInfo(
                    name="present_plan", handler=present_plan,
                    parameters={"type": "object", "properties": {
                        "overview": {"type": "object"},
                        "checklist": {"type": "array"},
                    }, "required": []},
                ),
                "set_execution_mode": ToolInfo(
                    name="set_execution_mode", handler=set_execution_mode,
                    parameters={"type": "object", "properties": {
                        "mode": {"type": "string"},
                        "reason": {"type": "string"},
                    }, "required": ["mode"]},
                ),
                "pill_flip_mode": ToolInfo(
                    name="pill_flip_mode", handler=pill_flip_mode,
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
            }

            async def fake_open_stream(create_kwargs, timeout_s, client=None):
                provider_calls.append(create_kwargs)
                action = action_queue.pop(0) if action_queue else {"text": "done"}
                if action.get("tool"):
                    chunk = _tool_chunk(
                        action["tool"], action.get("args", {}),
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
                    "id": tid,
                    "source_messages": [{"interaction_id": tid, "content": str(user_message)}],
                }),
                patch("app.agent.loop._open_stream", new=fake_open_stream),
                patch("app.agent.loop._record_billing_usage", new=AsyncMock(return_value={})),
                patch("app.agent.loop._agent_profile_visitor", return_value=None),
                patch("app.agent.session_changes.capture_tool_state_async", new=AsyncMock(return_value={})),
                patch("app.agent.session_changes.record_tool_delta", new=AsyncMock(return_value=[])),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], \
                    patches[5], patches[6], patches[7], patches[8]:
                events = [
                    event async for event in _stream_agent_events_impl(
                        user_id="admin",
                        session_id=sid,
                        user_message=user_message,
                        system_prompt="",
                        agent_id="",
                        history=list(history or []),
                        parent_interaction_id=tid,
                        db=db,
                        execution_mode=execution_mode,
                        run_turn_id=tid,
                        max_turns=max_turns,
                    )
                ]
            return events, calls, provider_calls, mode_flips

    # ── Path A: agent-driven switch (the fixed path) ───────────────────────

    async def test_path_a_agent_switch_to_auto_releases_plan_contract(self):
        """Plan start -> set_execution_mode('auto') mid-turn -> NO plan blocker.

        The switch re-derives `_requires_plan_document` (loop.py ~5699), so the
        completion gate no longer demands a Plan Overview the user has already
        authorised the agent to go implement.
        """
        events, calls, _provider_calls, _flips = await self._run(
            [
                {"tool": "set_execution_mode", "args": {"mode": "auto", "reason": "user approved"}},
                {"tool": "read_source"},
                {"text": "The approved work is complete."},
            ],
            execution_mode="plan",
            session_mode="plan",
            history=_APPROVAL_ASK,
            user_message=_APPROVAL_USER,
        )

        self.assertTrue(
            any(c[0] == "set_execution_mode" and c[1].get("mode") == "auto" for c in calls),
            "the scripted switch never ran — the escalation gate likely blocked it",
        )
        self.assertTrue(
            any(e.get("type") == "execution_mode" and e.get("mode") == "auto" for e in events),
            "the loop did not broadcast the execution_mode switch event",
        )
        self.assertNotIn(
            "mode_contract_blocked", _steps(events),
            "a Plan->Auto switch still forced present_plan",
        )
        self.assertNotIn(
            "plan_document_required", _blocker_reasons(events),
            "REGRESSION: a Plan->Auto switch still owed a Plan Overview",
        )
        self.assertTrue(
            _final_responses(events),
            "the run did not finish with a normal response event",
        )

    # ── The gate is satisfiable: calling present_plan clears it ────────────

    async def test_plan_run_completing_the_workspace_is_not_blocked(self):
        """Plan + a successful present_plan -> normal response.

        Companion to the negative control: it proves the gate is satisfiable and
        that the control's blocker is specifically about the MISSING call, not a
        broken completion path.
        """
        events, calls, _provider_calls, _flips = await self._run(
            [
                {"tool": "present_plan", "args": {"overview": {}, "checklist": []}},
                {"text": "Here is the plan."},
            ],
            execution_mode="plan",
            session_mode="plan",
            user_message="Draft a plan for the change.",
        )

        self.assertEqual([c[0] for c in calls], ["present_plan"])
        self.assertNotIn("plan_document_required", _blocker_reasons(events))
        self.assertTrue(_final_responses(events))

    # ── Negative control: the gate must still fire when nothing changes ────

    async def test_negative_control_plan_without_switch_still_blocks(self):
        """A Plan run that never switches and never calls present_plan STILL blocks.

        This is the assertion that proves the fix RELEASED the obligation rather
        than quietly deleting the gate. If this test ever passes without the
        blocker, the Plan contract has been weakened.
        """
        events, calls, provider_calls, _flips = await self._run(
            [
                {"text": "Here is the plan summary."},
                {"text": "Here is the plan summary again."},
            ],
            execution_mode="plan",
            session_mode="plan",
            user_message="Draft a plan for the change.",
        )

        self.assertEqual(calls, [], "no tool should have run in the control turn")
        self.assertIn(
            "work_policy_protocol_retry", _steps(events),
            "the loop should retry one provider protocol violation",
        )
        self.assertTrue(any(
            event.get("stop_cause") == "work_not_started" for event in events
        ), "the Plan-document obligation was weakened — it no longer blocks")
        self.assertTrue(all(
            isinstance(call.get("tool_choice"), dict)
            and call["tool_choice"]["function"]["name"] == "present_plan"
            for call in provider_calls
        ))
        self.assertFalse(
            _final_responses(events),
            "a Plan run with no plan document must not deliver a normal response",
        )
        self.assertGreaterEqual(
            len(provider_calls), 2,
            "the nudge must actually produce one more model call before the blocker",
        )

    # ── Path B: pill-driven switch (the SAME bug class, second door) ───────

    async def test_path_b_pill_switch_moves_permission_but_not_the_contract(self):
        """A mid-turn pill flip grants write permission but leaves the plan gate armed.

        Measured finding (NOT asserted as desirable):
          * write_source is blocked while the run is still under Plan;
          * after the out-of-band pill flip the SAME tool executes for real —
            so permission follows the live mode;
          * the completion gate STILL fires plan_document_required, because the
            pill write is only re-read into _current_mode/_current_policy and
            never into _requires_plan_document.
        """
        events, calls, _provider_calls, flips = await self._run(
            [
                {"tool": "write_source"},
                {"tool": "pill_flip_mode"},
                {"tool": "write_source"},
                {"text": "The file is updated."},
                {"text": "The file is updated."},
            ],
            execution_mode="plan",
            session_mode="plan",
            user_message="Update the file.",
        )

        self.assertEqual(flips, ["auto"], "the simulated pill flip never persisted")

        results = _tool_results(events, "write_source")
        blocked = [e for e in results if e.get("error")]
        self.assertTrue(
            blocked, "the destructive tool should have been blocked BEFORE the flip",
        )
        self.assertEqual(
            blocked[0].get("error_type"), "guardrail_blocked",
            "expected a read-only permission-policy block before the flip",
        )

        # The SAME tool reaches its handler once the pill moved the permission
        # boundary — proving permission follows the live mode. (The pre-flip
        # call was blocked before execution, so this is a single real run.)
        self.assertEqual(
            [c[0] for c in calls].count("write_source"), 1,
            "write_source should have executed exactly once, after the flip",
        )
        ok = [e for e in results if not e.get("error")]
        self.assertTrue(ok, "write_source never succeeded after the pill flip")

        # ...and yet the contract obligation does NOT follow the flip.
        self.assertTrue(
            any(event.get("stop_cause") == "work_not_started" for event in events),
            "Path B unexpectedly released the Plan contract — re-measure: the "
            "finding is that the pill path never re-derives _requires_plan_document",
        )


if __name__ == "__main__":
    unittest.main()

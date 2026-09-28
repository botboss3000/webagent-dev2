"""Tests for the run-contract layer (app/agent/contracts.py) and the Manager
(per-trigger contracts in app/agent/manager.py).

Covers the pure, synchronous parts: contract parsing, tool classification,
approval detection, the mechanical rules, contract state transitions, and the
Manager's config resolution / verdict parsing. The DB-touching pieces
(manager window collection, self-note injection) are exercised indirectly
through the loop and are out of scope here.
"""

import json
import unittest
from pathlib import Path

from app.agent.contracts import (
    active_rules,
    add_manual_resume_approval,
    begin_ui_journey_recovery_attempt,
    check_pre_tool_progress_message,
    check_tool_cap,
    check_verify_after_edit,
    contract_prompt_block,
    error_strike,
    is_commit_tool,
    is_edit_tool,
    is_verify_tool,
    is_ui_path,
    mark_browser_journey_event,
    mark_edit_executed,
    mark_repository_changes,
    mark_tool_outcome,
    mark_tool_scheduled,
    mark_verify_executed,
    mechanical_check,
    new_contract_state,
    parse_contract,
    service_worker_cache_requirement,
    ui_journey_recovery_instruction,
    ui_journey_requirement,
    user_approved_plan,
)
from app.agent.manager import (
    _format_manager_prompt,
    _FALLBACK_PROMPTS,
    manager_feedback_message,
    _parse_manager_verdict,
    run_manager_check,
    resolve_manager_config,
    trigger_enabled,
)
from app.agent.loop_executor import LoopConfig
from app.agent.loop import _recovered_plan_workspace_satisfies_contract


class RecoveredPlanWorkspaceTests(unittest.TestCase):
    def test_recovered_or_explicit_continuation_with_workspace_satisfies_plan_contract(self):
        workspace = [{"id": "plan-overview", "data": {"markdown": "## Goal\nResume safely"}}]
        state = {"steps": [{"id": "E1", "phase": "execution", "status": "pending"}]}
        self.assertTrue(_recovered_plan_workspace_satisfies_contract(None, workspace, state))
        self.assertTrue(_recovered_plan_workspace_satisfies_contract("continue", workspace, state))
        self.assertTrue(_recovered_plan_workspace_satisfies_contract(
            "Please resume the original task.", workspace, state,
        ))
        self.assertFalse(_recovered_plan_workspace_satisfies_contract("new task", workspace, state))
        self.assertFalse(_recovered_plan_workspace_satisfies_contract(
            None, workspace, {"steps": []},
        ))
        self.assertFalse(_recovered_plan_workspace_satisfies_contract(None, [], state))


class LoopResponseMetadataRegressionTests(unittest.TestCase):
    def test_incomplete_task_uses_unified_work_policy(self):
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("_initial_task_pending", loop_source)
        self.assertIn("task_pending=(_work_task_pending or _work_contract_pending)", loop_source)
        self.assertNotIn("incomplete_task_response_retry", loop_source)

    def test_external_plan_rollout_uses_stable_directive_and_late_pointer(self):
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('external_plan_context', loop_source)
        self.assertIn('Plan workspace access', loop_source)
        self.assertIn('not inlined here for cache stability', loop_source)
        self.assertIn('Plan workspace unavailable', loop_source)

    def test_plan_workspace_keeps_plan_tool_available_without_forcing_it(self):
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )

        self.assertIn('"present_plan"', loop_source)
        self.assertIn('_plan_result.get("status") == "ok"', loop_source)
        self.assertIn('_work_policy_tool_choice = "auto"', loop_source)
        self.assertNotIn('_work_policy_named_tool', loop_source)
        self.assertNotIn('provider_choice(', loop_source)
        self.assertNotIn("incomplete_task_response_retry", loop_source)

    def test_live_mode_switch_rederives_every_mode_gate_local(self):
        """A mid-turn set_execution_mode must re-derive ALL contract locals.

        Regression: the switch reassigned only `execution_mode`, leaving
        `_requires_plan_document` (and the other contract locals) captured from
        run start, so a Plan→Auto flip still failed the run with the generic
        plan-workspace blocker. Both the run-start capture and the switch site
        must derive them through the shared mode_gate_locals helper.
        """
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("mode_gate_locals, normalize_mode_id, resolve_execution_mode,",
                      loop_source)
        # One call at the run-start capture, one at the live switch.
        self.assertGreaterEqual(loop_source.count("mode_gate_locals("), 2)
        switch = loop_source.index('if tool_name == "set_execution_mode" and success:')
        tail = loop_source[switch:]
        rederive = tail.index("mode_gate_locals(")
        for local in (
            '_mode_entry = _mode_locals["mode_entry"]',
            '_permission_policy = _mode_locals["permission_policy"]',
            '_active_mode_contract = _mode_locals["active_contract"]',
            '_accumulated_mode_contract = _mode_locals["accumulated_contract"]',
            '_requires_plan_document = _mode_locals["requires_plan_document"]',
        ):
            self.assertIn(local, tail)
            self.assertLess(rederive, tail.index(local))

    def test_checkpoint_failure_continues_without_idle_terminal_error(self):
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("continuing without durable checkpoint", loop_source)
        self.assertNotIn(
            'yield {"type": "error", "level": "agent", "stop_cause": "checkpoint_unavailable"',
            loop_source,
        )

    def test_ui_journey_exhaustion_is_partial_not_contract_blocked(self):
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )
        self.assertIn('"terminal_outcome"] = "partial"', loop_source)
        self.assertIn('"partial_reason"] = "ui_journey_unverified"', loop_source)

    def test_contract_evidence_is_added_before_metadata_serialization(self):
        loop_source = (Path(__file__).parents[1] / "app" / "agent" / "loop.py").read_text(
            encoding="utf-8"
        )

        self.assertIn("meta_final_data = json.loads(_build_meta(", loop_source)
        self.assertIn('"step": "ui_journey_contract_blocked"', loop_source)
        self.assertIn("async def _emit_contract_blocker", loop_source)
        self.assertIn('"terminal_outcome"] = "contract_blocked"', loop_source)
        self.assertIn('"contract_blocker_reason"] = reason', loop_source)
        self.assertIn('async def _emit_partial_ui_journey', loop_source)
        self.assertIn('"partial_reason"] = "ui_journey_unverified"', loop_source)
        self.assertNotIn('"ui_journey_required",', loop_source)
        self.assertIn('"step": "service_worker_cache_contract_blocked"', loop_source)
        self.assertIn('"service_worker_cache_required",', loop_source)
        self.assertNotIn('"stop_cause": "work_not_started"', loop_source)
        self.assertNotIn('"stop_cause": "ui_journey_required"', loop_source)
        self.assertNotIn('"stop_cause": "service_worker_cache_required"', loop_source)
        self.assertIn('"ui_verification": {', loop_source)
        self.assertIn("mark_browser_journey_event(", loop_source)
        self.assertIn('meta_final_data["contract_evidence"]', loop_source)
        self.assertIn("build_verification_event(", loop_source)
        self.assertIn('_verification_passed = bool(_verification_event.get("success"))',
                      loop_source)
        self.assertIn("meta_final = json.dumps(meta_final_data)", loop_source)
        self.assertNotIn('meta_final["contract_evidence"]', loop_source)


class ContractParseTests(unittest.TestCase):
    def test_missing_agent_is_empty(self):
        # No agent record at all resolves to no contract (nothing to enforce),
        # which is distinct from a present agent whose gates are simply off.
        self.assertEqual(parse_contract(None), {})
        self.assertEqual(parse_contract({}), {})

    def test_present_agent_without_gates_block_projects_all_off(self):
        # A present agent yields the full catalog view — but every key is OFF.
        # Activation is owned solely by metadata['gates'] (gate_registry); the
        # historical metadata['contract'] block is deliberately inert.
        contract = parse_contract({"metadata": "{}"})
        self.assertTrue(contract)
        self.assertTrue(all(value is False or value == 0 for value in contract.values()))

    def test_legacy_contract_metadata_is_inert(self):
        agent = {"metadata": {"contract": {
            "require_pre_tool_progress_message": True,
            "require_verify_after_edit": True,
            "require_final_service_worker_cache_bump": True,
            "require_browser_journey_for_ui_change": True,
            "max_tool_errors": 3,
            "max_tool_calls_per_turn": 12,
        }}}
        contract = parse_contract(agent)
        self.assertFalse(contract["require_pre_tool_progress_message"])
        self.assertFalse(contract["require_verify_after_edit"])
        self.assertFalse(contract["require_final_service_worker_cache_bump"])
        self.assertFalse(contract["require_browser_journey_for_ui_change"])
        self.assertEqual(contract["max_tool_errors"], 0)
        self.assertEqual(contract["max_tool_calls_per_turn"], 0)

    def test_gates_block_activates_only_its_own_keys(self):
        agent = {
            "metadata": {
                "contract": {"require_pre_tool_progress_message": True},
                "gates": {"schema_version": 1, "gates": [
                    {"type": "verify_after_edit", "enabled": True}]},
            },
        }
        contract = parse_contract(agent)
        self.assertTrue(contract["require_verify_after_edit"])
        self.assertFalse(contract["require_pre_tool_progress_message"])

    def test_malformed_config_fails_closed(self):
        for rec in (
            {"metadata": {"contract": "not json"}},
            {"metadata": {"contract": 42}},
            {"metadata": "not json"},
            {"metadata": {"gates": {"gates": "not-a-list"}}},
        ):
            contract = parse_contract(rec)
            self.assertTrue(
                all(value is False or value == 0 for value in contract.values()), rec
            )

    def test_active_rules_and_prompt_block(self):
        contract = {"require_pre_tool_progress_message": True, "require_verify_after_edit": False,
                    "max_tool_errors": 0, "max_tool_calls_per_turn": 5}
        self.assertEqual(active_rules(contract), ["require_pre_tool_progress_message", "max_tool_calls_per_turn"])
        # Instruction text comes from the enabled gate descriptor, not a legacy
        # rule table — and only for a ``prompt``-inject type, rendered for its
        # ACTIVE rule.
        agent = {"metadata": {"gates": {"schema_version": 1, "gates": [
            {"type": "verify_after_edit", "enabled": True, "message": "VERIFY-MARKER"}]}}}
        self.assertIn(
            "VERIFY-MARKER",
            contract_prompt_block(
                {**contract, "require_verify_after_edit": True}, agent),
        )
        # The pre-tool progress-message gate is ``pre_tool``: its wording asks for a REPLY, so it must
        # never reach the standing block even while its rule is active. The block
        # is driven by the descriptor body, so an empty block proves the gate
        # contributed nothing to it.
        plan_agent = {"metadata": {"gates": {"schema_version": 1, "gates": [
            {"type": "pre_tool_progress_message", "enabled": True}]}}}
        self.assertEqual(contract_prompt_block(contract, plan_agent), "")
        self.assertEqual(contract_prompt_block({}), "")


class ToolClassificationTests(unittest.TestCase):
    def test_edit_and_commit_tools(self):
        self.assertTrue(is_edit_tool("write_source"))
        self.assertTrue(is_edit_tool("edit_source"))
        self.assertTrue(is_edit_tool("patch_source"))
        self.assertTrue(is_edit_tool("delete_source"))
        self.assertTrue(is_edit_tool("resolve_conflict"))
        self.assertFalse(is_edit_tool("read_source"))
        self.assertFalse(is_edit_tool("run_command"))
        self.assertTrue(is_commit_tool("commit_and_push"))
        self.assertFalse(is_commit_tool("git_tool"))

    def test_verify_tools(self):
        self.assertTrue(is_verify_tool("read_source"))
        self.assertTrue(is_verify_tool("search_source"))
        self.assertFalse(is_verify_tool("write_source"))

    def test_verify_tools_respect_arguments(self):
        self.assertTrue(is_verify_tool("git_tool", {"operation": "diff"}))
        self.assertFalse(is_verify_tool("git_tool", {"operation": "push"}))
        self.assertTrue(is_verify_tool("run_command", {"command": "git status"}))
        self.assertFalse(is_verify_tool("run_command", {"command": "rm -rf /"}))
        self.assertTrue(is_verify_tool("http_request", {"method": "GET"}))
        self.assertFalse(is_verify_tool("http_request", {"method": "POST"}))
        self.assertTrue(is_verify_tool("browser_action", {"action": "get_text"}))
        self.assertFalse(is_verify_tool("browser_action", {"action": "click"}))
        self.assertTrue(is_verify_tool(
            "run_command", {"command": "python -m pytest -q tests/test_widget.py"}
        ))
        self.assertFalse(is_verify_tool(
            "run_command", {"command": "pytest -q && rm -rf build"}
        ))


class ApprovalDetectionTests(unittest.TestCase):
    def test_original_request_is_never_approval(self):
        messages = [{"role": "user", "content": "ok, build the feature"}]
        self.assertFalse(user_approved_plan(messages))

    def test_new_approval_after_request_counts(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan."},
            {"role": "user", "content": "go ahead"},
        ]
        self.assertTrue(user_approved_plan(messages))

    def test_latest_message_must_be_approval(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan."},
            {"role": "user", "content": "actually change the approach"},
        ]
        self.assertFalse(user_approved_plan(messages))

    def test_word_boundary_matching(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan."},
            {"role": "user", "content": "looking at the code"},
        ]
        self.assertFalse(user_approved_plan(messages))

    def test_typed_continue_is_case_insensitive_approval(self):
        messages = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan. Approve it?"},
            {"role": "user", "content": "Continue"},
        ]
        self.assertTrue(user_approved_plan(messages))

    def test_continue_button_adds_backend_approval_on_manual_resume(self):
        history = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan. Approve it?"},
        ]
        resumed = add_manual_resume_approval(history, "manual")
        self.assertEqual(resumed[-1]["role"], "user")
        self.assertEqual(resumed[-1]["content"], "Continue")
        self.assertTrue(user_approved_plan(resumed))
        self.assertEqual(len(history), 2)

    def test_automatic_resume_does_not_grant_approval(self):
        history = [
            {"role": "user", "content": "build the feature"},
            {"role": "assistant", "content": "Here is my plan. Approve it?"},
        ]
        self.assertIs(add_manual_resume_approval(history, "watchdog"), history)
        self.assertFalse(user_approved_plan(history))


class MechanicalCheckTests(unittest.TestCase):
    def test_pre_edit_progress_message_blocks_until_the_standalone_message_is_published(self):
        """The gate holds every AUTO-mode edit until the deliberate, tool-free
        message is published. It does not ask for approval, but it also has no
        same-turn or later-retry escape hatch."""
        contract = {"require_pre_tool_progress_message": True}
        state = new_contract_state()
        msg = check_pre_tool_progress_message(contract, state, "write_source", "auto",
                                     announcement="ANNOUNCE-MARKER", turn=1)
        self.assertEqual(msg, "ANNOUNCE-MARKER")
        self.assertTrue(state["pre_tool_progress_message_pending"])
        self.assertFalse(state["pre_tool_progress_message_done"])

        # Sibling and later edits remain held until the dedicated publisher marks
        # the gate complete. The returned fallback is still actionable guidance.
        self.assertIsNotNone(check_pre_tool_progress_message(
            contract, state, "write_source", "auto", turn=1))
        self.assertIsNotNone(check_pre_tool_progress_message(
            contract, state, "write_source", "auto", turn=2))
        self.assertTrue(state["pre_tool_progress_message_pending"])
        self.assertFalse(state["pre_tool_progress_message_done"])

        state["pre_tool_progress_message_pending"] = False
        state["pre_tool_progress_message_done"] = True
        self.assertIsNone(check_pre_tool_progress_message(
            contract, state, "write_source", "auto", announcement="ANNOUNCE-MARKER"))

    def test_pre_tool_progress_message_ships_a_usable_fallback_announcement(self):
        """No configured guidance still delivers an announcement: configuration
        is guidance, never a precondition for the run to proceed."""
        fallback = check_pre_tool_progress_message(
            {"require_pre_tool_progress_message": True}, new_contract_state(),
            "write_source", "auto")
        self.assertIsNotNone(fallback)
        self.assertIn("pre-edit progress message", fallback.lower())

    def test_scheduling_a_tool_does_not_release_the_pre_edit_message_boundary(self):
        state = new_contract_state()
        check_pre_tool_progress_message({"require_pre_tool_progress_message": True}, state,
                               "write_source", "auto")
        mark_tool_scheduled(state)
        self.assertTrue(state["pre_tool_progress_message_pending"])
        self.assertFalse(state["pre_tool_progress_message_done"])

    def test_pre_tool_progress_message_scope_and_gate_off_are_untouched(self):
        contract = {"require_pre_tool_progress_message": True}
        # Plan/Ask modes pass — the mode gate owns confirmation there.
        self.assertIsNone(check_pre_tool_progress_message(
            contract, new_contract_state(), "write_source", "plan"))
        self.assertIsNone(check_pre_tool_progress_message(
            contract, new_contract_state(), "write_source", "ask"))
        # Non-edit tools never trip it.
        self.assertIsNone(check_pre_tool_progress_message(
            contract, new_contract_state(), "read_source", "auto"))
        # Gate off → a pure no-op, window and all.
        off = new_contract_state()
        self.assertIsNone(check_pre_tool_progress_message(
            {}, off, "write_source", "auto", announcement="ANNOUNCE-MARKER"))
        self.assertFalse(off["pre_tool_progress_message_pending"])

    def test_pre_tool_progress_message_passes_after_edit_executed(self):
        contract = {"require_pre_tool_progress_message": True}
        state = new_contract_state()
        mark_edit_executed(state)
        self.assertIsNone(check_pre_tool_progress_message(contract, state, "write_source", "auto"))

    def test_pre_tool_progress_message_follows_the_tool_permission_verdict(self):
        """A MUTATING run_command is announced exactly like a file writer, while
        an argument-proven read-only one is not — the rule follows the same
        permission logic the tools page uses instead of a name list."""
        contract = {"require_pre_tool_progress_message": True}

        # Mutating shell call → announced.
        self.assertEqual(
            check_pre_tool_progress_message(contract, new_contract_state(), "run_command",
                                   "auto", announcement="ANNOUNCE-MARKER",
                                   edit_call=True, turn=1),
            "ANNOUNCE-MARKER",
        )
        # Read-only shell call → not an edit, so nothing is announced.
        self.assertIsNone(check_pre_tool_progress_message(
            contract, new_contract_state(), "run_command", "auto",
            announcement="ANNOUNCE-MARKER", edit_call=False, turn=1))
        # No verdict supplied → the hardcoded name list still decides, so
        # run_command stays out (unchanged behaviour for every other caller).
        self.assertIsNone(check_pre_tool_progress_message(
            contract, new_contract_state(), "run_command", "auto",
            announcement="ANNOUNCE-MARKER", turn=1))
        # The name list stays a FLOOR: a file writer announces whatever the
        # caller's verdict says, so a wrong verdict can never lose the plan.
        self.assertEqual(
            check_pre_tool_progress_message(contract, new_contract_state(), "write_source",
                                   "auto", announcement="ANNOUNCE-MARKER",
                                   edit_call=False, turn=1),
            "ANNOUNCE-MARKER",
        )

    def test_mutating_call_classifier_matches_the_guardrail_exemptions(self):
        """The shared classifier must agree with the confirmation guardrail's
        read-only exemptions, so a safe inspection command never demands a plan
        while a redirecting one does."""
        from app.agent.loop import _classify_mutating_call

        class _Info:
            destructive = False

        class _Destructive:
            destructive = True

        ask = frozenset({"run_command"})
        for cmd in ("git status", "ls -la", "cd /app && git status", "cat README.md"):
            self.assertFalse(
                _classify_mutating_call("run_command", {"command": cmd}, _Info(), ask),
                cmd)
        for cmd in ("echo x > f", "rm -rf /tmp/x", "git status; rm -rf /", "ls |& tee out"):
            self.assertTrue(
                _classify_mutating_call("run_command", {"command": cmd}, _Info(), ask),
                cmd)
        # Mode/model controls are never implementation edits.
        self.assertFalse(_classify_mutating_call(
            "set_execution_mode", {"mode": "auto"}, _Info(), ask))
        # A descriptor flagged destructive is mutating even off the ask-set.
        self.assertTrue(_classify_mutating_call(
            "some_tool", {}, _Destructive(), frozenset()))

    def test_verify_after_edit_blocks_until_verify(self):
        contract = {"require_verify_after_edit": True}
        state = new_contract_state()
        mark_edit_executed(state)  # edit ran → awaiting verify
        self.assertIsNotNone(check_verify_after_edit(contract, state, "write_source", {}))
        self.assertIsNone(check_verify_after_edit(contract, state, "read_source", {}))
        mark_verify_executed(state)
        self.assertIsNone(check_verify_after_edit(contract, state, "write_source", {}))

    def test_ui_path_classification_is_scoped_to_user_interfaces(self):
        for path in (
            "ui/chat/chat.js",
            "static/app.css",
            "frontend/src/App.tsx",
            "client/components/Nav.jsx",
            "index.html",
            "sw.js",
        ):
            self.assertTrue(is_ui_path(path), path)
        for path in ("app/agent/loop.py", "tests/test_api.py", "README.md"):
            self.assertFalse(is_ui_path(path), path)

    def test_ui_change_requires_post_change_browser_journey(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        self.assertIsNotNone(ui_journey_requirement(contract, state))
        mark_browser_journey_event(
            state,
            tool_name="browser_action",
            tool_args={"action": "navigate", "url": "/app"},
            success=True,
            evidence={"environment_generation": "g1", "resource_scope": "headless_container"},
        )
        self.assertIsNotNone(ui_journey_requirement(contract, state))
        mark_browser_journey_event(
            state,
            tool_name="browser_action",
            tool_args={"action": "click", "selector": "#open-menu"},
            success=True,
            evidence={"environment_generation": "g1", "resource_scope": "headless_container"},
        )
        self.assertIsNotNone(ui_journey_requirement(contract, state))
        self.assertFalse(state["ui_journey"]["complete"])
        mark_browser_journey_event(
            state,
            tool_name="browser_action",
            tool_args={"action": "screenshot"},
            success=True,
            evidence={"environment_generation": "g1", "resource_scope": "headless_container"},
        )
        self.assertIsNone(ui_journey_requirement(contract, state))
        self.assertTrue(state["ui_journey"]["complete"])
        mark_edit_executed(state, ["ui/chat/chat.css"])
        self.assertIsNotNone(ui_journey_requirement(contract, state))
        self.assertFalse(state["ui_journey"]["complete"])

    def test_ui_edit_invalidates_prior_proof_and_recovery_reverifies_current_revision(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        mark_edit_executed(state, ["ui/chat/chat.js"])
        for action in ("navigate", "click", "get_text"):
            mark_browser_journey_event(
                state, tool_name="browser_action", tool_args={"action": action},
                success=True, evidence=evidence,
            )
        self.assertTrue(state["ui_journey"]["complete"])
        self.assertEqual(state["ui_journey"]["revision"], 1)

        mark_edit_executed(state, ["ui/chat/chat.css"])
        self.assertFalse(state["ui_journey"]["complete"])
        self.assertEqual(state["ui_journey"]["revision"], 2)
        attempt = begin_ui_journey_recovery_attempt(state)
        self.assertEqual(attempt["strategy"], "same_backend")
        self.assertIn("UI revision 2", ui_journey_recovery_instruction(state, attempt))
        for action in ("navigate", "click", "screenshot"):
            mark_browser_journey_event(
                state, tool_name="browser_action", tool_args={"action": action},
                success=True, evidence=evidence,
            )
        self.assertTrue(state["ui_journey"]["complete"])
        self.assertEqual(state["ui_journey"]["outcome"], "verified")
        self.assertEqual(state["ui_journey"]["attempts"][-1]["status"], "verified")

    def test_capability_failure_recovery_switches_from_headless_to_server(self):
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        mark_browser_journey_event(
            state, tool_name="browser_action", tool_args={"action": "navigate"},
            success=False, evidence={
                "negative_capability": True, "resource_scope": "headless_container",
                "environment_generation": "g2",
            },
        )
        attempt = begin_ui_journey_recovery_attempt(state)
        self.assertEqual(attempt["strategy"], "switch_to_server")
        instruction = ui_journey_recovery_instruction(state, attempt)
        self.assertIn("browser_backend(mode=server)", instruction)
        self.assertIn("Do not close the browser", instruction)

    def test_capability_failure_recovery_switches_from_server_to_headless(self):
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        mark_browser_journey_event(
            state, tool_name="browser_action", tool_args={"action": "navigate"},
            success=False, evidence={
                "negative_capability": True, "resource_scope": "server_container",
                "environment_generation": "g3",
            },
        )
        attempt = begin_ui_journey_recovery_attempt(state)
        self.assertEqual(attempt["strategy"], "switch_to_headless")
        self.assertIn(
            "browser_backend(mode=headless)",
            ui_journey_recovery_instruction(state, attempt),
        )

    # NOTE: the two spatial-layout tests that stood here exercised the RETIRED
    # sibling layout gate (``extract_layout_requirements`` scraping +
    # ``require_layout_assertion`` / ``layout_assertion_requirement``). Layout
    # acceptance is no longer a separate gate: it is one KIND of assertion under
    # the ``assertions`` gate, declared like any other and machine-checked via the
    # ``webagent_layout_assertion`` payload (see app/agent/assertions.py).

    def test_ui_assertion_without_interaction_never_completes_journey(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        for action in ("navigate", "get_text"):
            mark_browser_journey_event(
                state,
                tool_name="browser_action",
                tool_args={"action": action, "url": "/app" if action == "navigate" else ""},
                success=True,
                evidence={
                    "environment_generation": "g1",
                    "resource_scope": "headless_container",
                },
            )

        self.assertTrue(state["ui_journey"]["navigated"])
        self.assertFalse(state["ui_journey"]["interacted"])
        self.assertTrue(state["ui_journey"]["asserted"])
        self.assertFalse(state["ui_journey"]["complete"])
        self.assertIsNotNone(ui_journey_requirement(contract, state))

    def test_interaction_invalidates_earlier_assertion_until_post_interaction_assertion(self):
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "get_text", "click"):
            mark_browser_journey_event(
                state, tool_name="browser_action", tool_args={"action": action},
                success=True, evidence=evidence,
            )
        self.assertFalse(state["ui_journey"]["asserted"])
        self.assertFalse(state["ui_journey"]["complete"])

    def test_non_ui_change_does_not_require_browser_journey(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["app/agent/loop.py"])
        self.assertIsNone(ui_journey_requirement(contract, state))

    def test_development_browser_journey_counts_as_ui_verification(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/main-panel/app/instances.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        mark_browser_journey_event(
            state,
            tool_name="dev_browser_action",
            tool_args={"action": "open", "path": "/app?tab=agents"},
            success=True,
            evidence=evidence,
        )
        self.assertTrue(state["ui_journey"]["navigated"])
        self.assertEqual(state["ui_journey"]["events"][-1]["url"], "/app?tab=agents")
        self.assertIsNotNone(ui_journey_requirement(contract, state))
        for action in ("click", "get_text"):
            mark_browser_journey_event(
                state,
                tool_name="dev_browser_action",
                tool_args={"action": action, "selector": "#open-menu"},
                success=True,
                evidence=evidence,
            )
        self.assertTrue(state["ui_journey"]["complete"])
        self.assertEqual(state["ui_journey"]["outcome"], "verified")
        self.assertIsNone(ui_journey_requirement(contract, state))

    def test_development_browser_read_satisfies_verify_after_edit(self):
        self.assertTrue(is_verify_tool("dev_browser_action", {"action": "open"}))
        self.assertTrue(
            is_verify_tool("dev_browser_action", {"action": "get_text"})
        )
        self.assertFalse(
            is_verify_tool("dev_browser_action", {"action": "click"})
        )

    def test_service_worker_cache_bump_neither_arms_nor_erases_ui_journey(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "click", "get_text"):
            mark_browser_journey_event(
                state, tool_name="browser_action", tool_args={"action": action},
                success=True, evidence=evidence,
            )
        self.assertTrue(state["ui_journey"]["complete"])
        revision = state["ui_change_revision"]

        # The mandatory final cache bump must not invalidate completed proof.
        mark_edit_executed(state, ["sw.js"])
        self.assertEqual(state["ui_change_revision"], revision)
        self.assertEqual(state["ui_changed_paths"], ["ui/chat/chat.js"])
        self.assertTrue(state["ui_journey"]["complete"])
        self.assertIsNone(ui_journey_requirement(contract, state))

        # ...and a run whose only UI-ish edit is the cache bump needs no journey.
        bump_only = new_contract_state()
        mark_edit_executed(bump_only, ["sw.js"])
        self.assertEqual(bump_only["ui_changed_paths"], [])
        self.assertIsNone(ui_journey_requirement(contract, bump_only))

    def test_later_ui_edit_accumulates_changed_paths_for_the_journey(self):
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        mark_edit_executed(state, ["ui/chat/chat.css", "sw.js"])
        self.assertEqual(
            state["ui_changed_paths"], ["ui/chat/chat.css", "ui/chat/chat.js"]
        )
        self.assertEqual(state["ui_change_revision"], 2)

    def test_reverification_then_evaluate_completes_the_journey(self):
        """Regression, session 6dd7352c: after re-navigating, the agent asserted
        with evaluate and the journey stayed incomplete — a false negative. An
        evaluate that returns observed UI state must count as the assertion."""
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/elements/session-dropdown/list.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "click", "screenshot"):
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": action}, success=True, evidence=evidence,
            )
        self.assertTrue(state["ui_journey"]["complete"])

        # Re-verifying from a clean page legitimately invalidates the assertion
        # (the page state changed); the recorded interaction still stands, so one
        # fresh evaluate-based assertion completes the journey again.
        for action in ("navigate", "evaluate", "close"):
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": action}, success=True, evidence=evidence,
                observation=(
                    '{"chipClass": "session-row-agent-warning"}'
                    if action == "evaluate" else None
                ),
            )
        self.assertTrue(state["ui_journey"]["interacted"])
        self.assertTrue(state["ui_journey"]["asserted"])
        self.assertTrue(state["ui_journey"]["complete"])
        self.assertIsNone(ui_journey_requirement(contract, state))

    def test_evaluate_with_observed_state_counts_as_an_assertion(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/elements/session-dropdown/list.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "click"):
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": action}, success=True, evidence=evidence,
            )
        mark_browser_journey_event(
            state, tool_name="dev_browser_action",
            tool_args={"action": "evaluate"}, success=True, evidence=evidence,
            observation='{"chipBoxShadow": "rgb(202, 138, 4) 0px 0px 0px 1.25px"}',
        )
        self.assertTrue(state["ui_journey"]["asserted"])
        self.assertTrue(state["ui_journey"]["complete"])
        self.assertIsNone(ui_journey_requirement(contract, state))

    def test_barren_evaluate_observations_do_not_assert(self):
        for observation in ("", "null", "None", "false", "{}", "[]", "undefined"):
            state = new_contract_state()
            mark_edit_executed(state, ["ui/chat/elements/session-dropdown/list.js"])
            evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
            for action in ("navigate", "click"):
                mark_browser_journey_event(
                    state, tool_name="dev_browser_action",
                    tool_args={"action": action}, success=True, evidence=evidence,
                )
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": "evaluate"}, success=True, evidence=evidence,
                observation=observation,
            )
            self.assertFalse(state["ui_journey"]["asserted"], observation)
            self.assertFalse(state["ui_journey"]["complete"], observation)

    def test_assertion_after_a_later_interaction_recompletes_the_journey(self):
        state = new_contract_state()
        mark_edit_executed(state, ["ui/main-panel/app/model-table.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "click", "get_text"):
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": action}, success=True, evidence=evidence,
            )
        self.assertTrue(state["ui_journey"]["complete"])
        # A later interaction invalidates the assertion (deliberate invariant)...
        mark_browser_journey_event(
            state, tool_name="dev_browser_action",
            tool_args={"action": "click"}, success=True, evidence=evidence,
        )
        self.assertFalse(state["ui_journey"]["asserted"])
        self.assertFalse(state["ui_journey"]["complete"])
        # ...and one fresh post-interaction assertion completes it again.
        mark_browser_journey_event(
            state, tool_name="dev_browser_action",
            tool_args={"action": "get_html"}, success=True, evidence=evidence,
        )
        self.assertTrue(state["ui_journey"]["asserted"])
        self.assertTrue(state["ui_journey"]["complete"])

    def test_later_ui_edit_still_invalidates_a_completed_journey(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/elements/session-dropdown/list.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "click", "get_html"):
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": action}, success=True, evidence=evidence,
            )
        self.assertIsNone(ui_journey_requirement(contract, state))
        # A fresh UI edit must still re-arm the requirement (the revision bump
        # is the real invalidation mechanism, not a later browser step).
        mark_edit_executed(state, ["ui/chat/elements/session-dropdown/list.js"])
        self.assertFalse(state["ui_journey"]["complete"])
        self.assertIsNotNone(ui_journey_requirement(contract, state))

    def test_failed_evaluate_never_asserts(self):
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/chat.js"])
        evidence = {"environment_generation": "g1", "resource_scope": "headless_container"}
        for action in ("navigate", "click"):
            mark_browser_journey_event(
                state, tool_name="dev_browser_action",
                tool_args={"action": action}, success=True, evidence=evidence,
            )
        mark_browser_journey_event(
            state, tool_name="dev_browser_action",
            tool_args={"action": "evaluate"}, success=False,
            evidence={**evidence, "negative_capability": True, "detail": "probe failed"},
            observation='{"chipClass": "session-row-agent-warning"}',
        )
        self.assertFalse(state["ui_journey"]["asserted"])
        self.assertFalse(state["ui_journey"]["complete"])

    def test_fresh_browser_capability_failure_is_a_disclosed_blocker_not_proof(self):
        state = new_contract_state()
        mark_edit_executed(state, ["index.html"])
        mark_browser_journey_event(
            state,
            tool_name="browser_action",
            tool_args={"action": "navigate", "url": "/app"},
            success=False,
            evidence={
                "negative_capability": True,
                "environment_generation": "g2",
                "resource_scope": "headless_container",
            },
        )
        requirement = ui_journey_requirement(
            {"require_browser_journey_for_ui_change": True}, state,
        )
        self.assertIn("headless_container", requirement)
        self.assertFalse(state["ui_journey"]["complete"])

    def test_aged_active_backend_failure_cannot_waive_ui_journey(self):
        from app.agent.fresh_evidence import environment_generation

        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/session-menu.js"])
        mark_browser_journey_event(
            state,
            tool_name="browser_action",
            tool_args={"action": "navigate", "url": "/app"},
            success=False,
            evidence={
                "negative_capability": True,
                "state_dependent": True,
                "observed_at": "2000-01-01T00:00:00Z",
                "stale_after_seconds": 1,
                "environment_generation": environment_generation(
                    "headless_container", "browser",
                ),
                "resource_scope": "headless_container",
                "resource_id": "browser",
            },
        )

        requirement = ui_journey_requirement(contract, state)
        self.assertIsNone(state["ui_journey"]["blocker"])
        self.assertIn("Navigate", requirement)
        self.assertNotIn("unverified because", requirement)

    def test_successful_backend_switch_invalidates_previous_scope_blocker(self):
        contract = {"require_browser_journey_for_ui_change": True}
        state = new_contract_state()
        mark_edit_executed(state, ["ui/chat/session-menu.js"])
        mark_browser_journey_event(
            state,
            tool_name="browser_backend",
            tool_args={"mode": "local"},
            success=False,
            evidence={
                "negative_capability": True,
                "state_dependent": True,
                "environment_generation": "local-g1",
                "resource_scope": "local_device",
                "resource_id": "browser",
            },
        )
        self.assertIsNone(state["ui_journey"]["blocker"])
        self.assertEqual(
            state["ui_journey"]["capability_failures"][-1]["resource_scope"],
            "local_device",
        )

        mark_browser_journey_event(
            state,
            tool_name="browser_backend",
            tool_args={"mode": "headless"},
            success=True,
            evidence={
                "negative_capability": False,
                "state_dependent": True,
                "environment_generation": "headless-g2",
                "resource_scope": "headless_container",
                "resource_id": "browser",
            },
        )

        self.assertIsNone(state["ui_journey"]["blocker"])
        self.assertEqual(
            state["ui_journey"]["backend_evidence"]["resource_scope"],
            "headless_container",
        )
        requirement = ui_journey_requirement(contract, state)
        self.assertIn("Navigate", requirement)
        self.assertNotIn("unverified because", requirement)
        self.assertFalse(state["ui_journey"]["complete"])

    def test_tool_cap(self):
        contract = {"max_tool_calls_per_turn": 2}
        state = new_contract_state()
        mark_tool_scheduled(state)
        mark_tool_scheduled(state)
        self.assertIsNotNone(check_tool_cap(contract, state))
        self.assertIsNone(check_tool_cap({"max_tool_calls_per_turn": 0}, state))

    def test_error_strike_fires_once(self):
        contract = {"max_tool_errors": 2}
        state = new_contract_state()
        mark_tool_outcome(state, False)
        mark_tool_outcome(state, False)
        msg1 = error_strike(contract, state)
        self.assertIsNotNone(msg1)
        self.assertTrue(state["error_strike_sent"])
        self.assertIsNone(error_strike(contract, state))  # once only

    def test_mechanical_check_orders_rules(self):
        contract = {"require_pre_tool_progress_message": True, "require_verify_after_edit": True}
        state = new_contract_state()
        # First edit in auto: plan rule fires first.
        msg = mechanical_check(contract, state, tool_name="write_source",
                               tool_args={}, execution_mode="auto")
        self.assertIsNotNone(msg)
        self.assertIn("pre-edit progress message", msg.lower())
        # Once the standalone message is posted, verification gate is next.

    def test_no_contract_never_blocks(self):
        state = new_contract_state()
        self.assertIsNone(mechanical_check({}, state, tool_name="write_source",
                                           tool_args={}, execution_mode="auto"))


    def test_repository_edits_require_sw_bump_as_the_final_edit(self):
        contract = {"require_final_service_worker_cache_bump": True}
        state = new_contract_state()
        self.assertIsNone(service_worker_cache_requirement(contract, state))

        mark_repository_changes(state, ["app/agent/loop.py"])
        requirement = service_worker_cache_requirement(contract, state)
        self.assertIn("sw.js", requirement)
        self.assertIn("final source edit", requirement)

        mark_repository_changes(state, ["sw.js"])
        self.assertIsNone(service_worker_cache_requirement(contract, state))

        mark_repository_changes(state, ["tests/test_loop.py"])
        self.assertIsNotNone(service_worker_cache_requirement(contract, state))

    def test_edit_event_can_include_sw_bump_and_remain_satisfied(self):
        contract = {"require_final_service_worker_cache_bump": True}
        state = new_contract_state()
        mark_repository_changes(
            state, ["app/agent/contracts.py", "sw.js"],
        )

        self.assertFalse(state["service_worker_cache_bump_pending"])
        self.assertEqual(state["service_worker_cache_bump_revision"], 1)
        self.assertIsNone(service_worker_cache_requirement(contract, state))


class ManagerConfigTests(unittest.TestCase):
    def test_defaults_enable_subagent_contract_gates(self):
        cfg = resolve_manager_config(None)
        self.assertEqual(cfg["plan_gate"], "blocking")
        self.assertEqual(cfg["edit_gate"], "blocking")
        self.assertEqual(cfg["commit_gate"], "off")
        self.assertEqual(cfg["watchdog"]["every_n_turns"], 4)
        self.assertEqual(cfg["max_checks"], 9)
        self.assertEqual(cfg["max_checks_by_kind"], {
            "plan_gate": 4, "edit_gate": 4, "watchdog": 3, "commit_gate": 1,
        })
        self.assertEqual(cfg["max_blocks"], 3)
        self.assertTrue(trigger_enabled(cfg, "plan_gate"))
        self.assertTrue(trigger_enabled(cfg, "watchdog"))

    def test_blocking_and_async_gates(self):
        agent = {"metadata": {"manager": {
            "plan_gate": "blocking", "edit_gate": "async",
            "commit_gate": "blocking", "watchdog": {
                "every_n_turns": 4, "on_errors": 3, "on_stall": True,
                "cooldown_turns": 2, "error_window": 8,
            },
            "max_checks": 7,
            "max_checks_by_kind": {
                "plan_gate": 2, "edit_gate": 6, "watchdog": 5, "commit_gate": 2,
            },
            "max_blocks": 4,
        }}}
        cfg = resolve_manager_config(agent)
        self.assertEqual(cfg["plan_gate"], "blocking")
        self.assertEqual(cfg["edit_gate"], "async")
        self.assertEqual(cfg["commit_gate"], "blocking")
        self.assertEqual(cfg["watchdog"], {
            "every_n_turns": 4, "on_errors": 3, "on_stall": True,
            "cooldown_turns": 2, "error_window": 8,
        })
        self.assertEqual(cfg["max_checks"], 7)
        self.assertEqual(cfg["max_checks_by_kind"], {
            "plan_gate": 2, "edit_gate": 6, "watchdog": 5, "commit_gate": 2,
        })
        self.assertEqual(cfg["max_blocks"], 4)
        self.assertTrue(trigger_enabled(cfg, "plan_gate"))
        self.assertTrue(trigger_enabled(cfg, "edit_gate"))
        self.assertTrue(trigger_enabled(cfg, "commit_gate"))
        self.assertTrue(trigger_enabled(cfg, "watchdog"))

    def test_broken_config_fails_open(self):
        cfg = resolve_manager_config({"metadata": {"manager": "not json"}})
        self.assertEqual(cfg["plan_gate"], "off")

    def test_verdict_parse_tolerant(self):
        v = _parse_manager_verdict(
            '```json\n{"verdict": "block", "reason": "off plan", "feedback": "fix it"}\n```',
            "edit_gate")
        self.assertIsNotNone(v)
        self.assertEqual(v["verdict"], "block")
        self.assertEqual(v["feedback"], "fix it")

    def test_verdict_parse_rejects_wrong_kind_and_garbage(self):
        self.assertIsNone(_parse_manager_verdict('{"verdict": "approve"}', "watchdog"))
        self.assertIsNone(_parse_manager_verdict("not json at all", "edit_gate"))
        self.assertIsNone(_parse_manager_verdict("", "edit_gate"))

    def test_verdict_contract_requires_reason_and_actionable_guidance(self):
        self.assertIsNone(_parse_manager_verdict(
            '{"verdict": "approve", "feedback": "unused"}', "edit_gate"))
        self.assertIsNone(_parse_manager_verdict(
            '{"verdict": "block", "reason": "off plan"}', "edit_gate"))
        self.assertIsNone(_parse_manager_verdict(
            '{"verdict": "off_track", "reason": "looping"}', "watchdog"))
        self.assertIsNotNone(_parse_manager_verdict(
            '{"verdict": "approve", "reason": "scope matches"}', "commit_gate"))
        self.assertIsNotNone(_parse_manager_verdict(
            '{"verdict": "stuck", "reason": "looping", '
            '"suggestion": "inspect the caller"}', "watchdog"))

    def test_bundled_manager_fallbacks_use_evidence_boundaries(self):
        self.assertIn(
            "Absence of evidence is not approval",
            _FALLBACK_PROMPTS["plan_gate"],
        )
        self.assertIn("operation scope", _FALLBACK_PROMPTS["edit_gate"])
        self.assertIn("evidence-bearing progress", _FALLBACK_PROMPTS["watchdog"])
        self.assertIn(
            "successful tool transport is not a passing check",
            _FALLBACK_PROMPTS["commit_gate"],
        )

    def test_bad_custom_prompt_falls_back_to_matching_kind(self):
        formatted = _format_manager_prompt(
            "commit_gate", "bad {unknown_placeholder}", "ship it",
            ["Assistant: verified"], '{"changed_paths":["app/a.py"]}',
        )
        self.assertIn("reviewing whether a commit/push", formatted)
        self.assertIn("STRUCTURED COMMIT EVIDENCE", formatted)
        self.assertIn("successful tool transport is not a passing check", formatted)
        self.assertNotIn("reviewing an agent's PLAN", formatted)

    def test_verdict_parse_buried_in_prose(self):
        v = _parse_manager_verdict(
            'Sure, here: {"verdict": "on_track", "reason": "progressing", "suggestion": ""}',
            "watchdog")
        self.assertIsNotNone(v)
        self.assertEqual(v["verdict"], "on_track")

    def test_on_stall_only_enables_watchdog(self):
        cfg = resolve_manager_config({"metadata": {"manager": {
            "watchdog": {"on_stall": True, "every_n_turns": 0, "on_errors": 0}
        }}})
        self.assertTrue(trigger_enabled(cfg, "watchdog"))

    def test_actionable_feedback_formats_for_active_loop(self):
        note = manager_feedback_message({
            "kind": "watchdog", "verdict": "off_track",
            "reason": "Repeated the same search.",
            "suggestion": "Inspect the caller instead.",
        })
        self.assertIn("MANAGER WATCHDOG", note)
        self.assertIn("Inspect the caller", note)
        self.assertEqual(manager_feedback_message({
            "kind": "watchdog", "verdict": "on_track"
        }), "")

    def test_manager_loop_node_is_independent_from_contract_node(self):
        cfg = LoopConfig([
            {"node": "contract_chk", "enabled": False},
            {"node": "manager_chk", "enabled": True},
        ])
        self.assertFalse(cfg.is_enabled("contract_chk"))
        self.assertTrue(cfg.is_enabled("manager_chk"))


class ManagerRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_explicit_check_index_enforces_configured_cap(self):
        from unittest.mock import patch

        with patch("app.abilities.app_function_enabled", return_value=True):
            verdict = await run_manager_check(
                "watchdog",
                user_id="user", session_id="session", final_asst_id="assistant",
                db=object(), max_checks=2, check_index=3,
            )
        self.assertIsNone(verdict)

    async def test_explicit_kind_check_index_enforces_kind_cap(self):
        from unittest.mock import patch

        with patch("app.abilities.app_function_enabled", return_value=True):
            verdict = await run_manager_check(
                "watchdog",
                user_id="user", session_id="session", final_asst_id="assistant",
                db=object(), max_checks=9, check_index=1,
                kind_max_checks=3, kind_check_index=4,
            )
        self.assertIsNone(verdict)

    async def test_retries_share_one_total_timeout_budget(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        calls = 0

        async def slow_completion(**_kwargs):
            nonlocal calls
            calls += 1
            await asyncio.sleep(1)

        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
            create=slow_completion,
        )))
        loop = asyncio.get_running_loop()
        started = loop.time()
        with (
            patch("app.abilities.app_function_enabled", return_value=True),
            patch("app.agent.manager._LLM_TIMEOUT", 0.03),
            patch("app.agent.manager._LLM_RETRY_BACKOFF_S", 0),
            patch("app.agent.manager._collect_manager_span",
                  return_value=(["User: test"], "test")),
            patch("app.agent.manager._resolve_llm",
                  return_value=("model", "provider", client)),
        ):
            verdict = await run_manager_check(
                "watchdog", user_id="user", session_id="session",
                final_asst_id="assistant", db=object(),
            )
        elapsed = loop.time() - started
        self.assertIsNone(verdict)
        # The one-call assertion proves the first timeout consumed the shared
        # deadline. Leave room for slow Windows CI/import bookkeeping outside
        # the patched 30 ms model call.
        self.assertLess(elapsed, 0.5)
        self.assertEqual(calls, 1)


class AssertionsIntroductionTests(unittest.TestCase):
    """The assertions gate is ALSO the introductory gate.

    The declaration beat does double duty: it opens the run's contract AND it is
    the run's introduction. The prose the agent posts alongside the checklist is
    published by the loop as the run's promoted opening bubble (a ``main`` row),
    so the user sees what the run is about to do before any work lands — and,
    exactly like the pre-tool progress message, that opening bubble does NOT consume the
    run's one promoted answer.
    """

    _DECL = {
        "type": "todo_list",
        "data": {"items": [{"label": "the answer must state what X is"}]},
    }

    def test_declaration_capture_opens_the_opening_message_window(self):
        from app.agent.contracts import check_assertions_declared

        state = new_contract_state()
        self.assertFalse(state["assertion_announcement_pending"])
        self.assertFalse(state["assertion_announcement_done"])
        msg = check_assertions_declared(
            {"require_assertions": True}, state, "present_chat_component", self._DECL)
        self.assertIsNone(msg)
        self.assertTrue(state["assertions_declared"])
        self.assertEqual(
            state["assertion_items"],
            [{"id": "", "label": "the answer must state what X is"}],
        )
        # The window is open: the prose that accompanies the checklist is the
        # run's introduction, and the loop publishes it as the promoted bubble.
        self.assertTrue(state["assertion_announcement_pending"])

    def test_flat_item_shape_opens_the_contract_too(self):
        """A model that flattens the call (items at the top level, not under
        ``data``) is declaring the same thing. Refusing it held back EVERY tool
        call in the run — including the declaration itself — so the run could
        never open its contract and dead-ended."""
        from app.agent.contracts import check_assertions_declared

        state = new_contract_state()
        msg = check_assertions_declared(
            {"require_assertions": True}, state, "present_chat_component",
            {"type": "todo_list",
             "items": ["the report must state each gate's id"]})
        self.assertIsNone(msg)
        self.assertTrue(state["assertions_declared"])
        self.assertEqual(
            [i["label"] for i in state["assertion_items"]],
            ["the report must state each gate's id"],
        )
        self.assertTrue(state["assertion_announcement_pending"])

    def test_bare_string_and_object_items_both_capture(self):
        from app.agent.assertions import items_from_tool_args

        items = items_from_tool_args(
            {"type": "todo_list", "data": {"items": ["one", {"label": "two"}]}})
        self.assertEqual([i["label"] for i in items], ["one", "two"])

    def test_a_todo_list_with_no_usable_items_still_holds_the_run_back(self):
        """The tolerance is about SHAPE, not about accepting an empty contract."""
        from app.agent.contracts import check_assertions_declared

        state = new_contract_state()
        msg = check_assertions_declared(
            {"require_assertions": True}, state, "present_chat_component",
            {"type": "todo_list", "data": {"items": []}})
        self.assertIsNotNone(msg)
        self.assertFalse(state["assertions_declared"])

    def test_a_blocked_first_call_opens_nothing(self):
        from app.agent.contracts import check_assertions_declared

        state = new_contract_state()
        msg = check_assertions_declared(
            {"require_assertions": True}, state, "read_source", {})
        self.assertIsNotNone(msg)
        self.assertFalse(state["assertion_announcement_pending"])
        self.assertFalse(state["assertions_declared"])

    def test_the_assembled_guidance_is_what_the_rule_feeds_back(self):
        from app.agent.contracts import check_assertions_declared

        state = new_contract_state()
        msg = check_assertions_declared(
            {"require_assertions": True}, state, "read_source", {}, "GUIDANCE-TEXT")
        self.assertEqual(msg, "GUIDANCE-TEXT")

    def test_loop_hands_the_composed_declaration_to_the_rule(self):
        """Configured declaration guidance is a silent no-op if the loop never
        passes it in: the rule would fall back to the shipped floor text."""
        source = Path("app/agent/loop.py").read_text(encoding="utf-8")
        self.assertIn("assertions_declaration=_assertions_declaration", source)
        self.assertIn("from app.agent.assertions import (", source)
        # Resolved only while the rule can still fire this run.
        self.assertIn("_assertions_already_announced", source)

    def test_presentation_is_tiny_and_non_judgemental(self):
        from app.agent.assertions import is_publishable, presentation

        self.assertFalse(is_publishable("   "))
        self.assertFalse(is_publishable(None))
        self.assertTrue(is_publishable("I'll do X, then verify it."))
        record = presentation("I'll do X.")
        self.assertTrue(record["announced"])
        self.assertEqual(record["chars"], len("I'll do X."))
        self.assertIn("format_configured", record)

    def test_loop_publishes_the_introduction_as_main_without_the_closing_latch(self):
        source = Path("app/agent/loop.py").read_text(encoding="utf-8")
        self.assertIn("_publish_assertion_introduction", source)
        helper = source[source.index("async def _publish_assertion_introduction"):
                        source.index("async def _persist_stream_progress")]
        self.assertIn('"assertions_announced"', helper)
        self.assertIn('message_phase="main"', helper)
        # The mirror of the closing gate's promotion — but the opening bubble is
        # NOT the answer, so the helper must never ASSIGN ``closing_promoted``.
        # Assigning it would starve the run's one terminal answer. (The docstring
        # names the key deliberately, to record that decision.)
        self.assertNotIn('["closing_promoted"] = ', helper)

    def test_both_step_and_tool_free_paths_publish(self):
        source = Path("app/agent/loop.py").read_text(encoding="utf-8")
        self.assertIn("ASSERTION-ANNOUNCE-STEP:", source)
        self.assertIn("ASSERTION-ANNOUNCE-FREE:", source)
        self.assertIn("assertion_announcement_done", source)

    def test_durable_latch_is_probed_on_both_backends(self):
        from app.db.interface import StorageBackend
        from app.db.local import LocalBackend

        self.assertTrue(hasattr(StorageBackend, "has_declared_assertions"))
        self.assertTrue(hasattr(LocalBackend, "has_declared_assertions"))
        src = Path("app/db/local.py").read_text(encoding="utf-8")
        self.assertIn('%"assertions_announced": true%', src)


    def test_assertion_budget_is_agent_limit_clamped_to_the_app_ceiling(self):
        """The effective budget is min(agent limit, app ceiling), and the app value
        is BOTH the default for an agent with no override AND the ceiling that
        override is clamped to.

        This is a REQUIREMENT-COMPLIANCE budget, NOT error recovery: the run has
        not failed, it simply has not yet answered for its contract. It is
        deliberately separate from the runtime page's resume/error counters, which
        have a different trigger, counter and lifecycle.
        """
        import tempfile
        from unittest import mock

        from app.agent import assertions

        with tempfile.TemporaryDirectory() as tmp:
            settings = Path(tmp) / "app-settings.json"
            with mock.patch.object(assertions, "_SETTINGS_FILE", settings):
                # Shipped default when neither layer is configured.
                self.assertEqual(assertions.effective_max_attempts(None),
                                 assertions.DEFAULT_MAX_ATTEMPTS)
                settings.write_text(
                    json.dumps({"run_max_assertions_attempts": 3}), encoding="utf-8")
                # The app ceiling clamps a larger agent limit …
                self.assertEqual(
                    assertions.effective_max_attempts(
                        {"metadata": {"assertions_max_attempts": 9}}), 3)
                # … a stricter agent value still wins …
                self.assertEqual(
                    assertions.effective_max_attempts(
                        {"metadata": {"assertions_max_attempts": 2}}), 2)
                # … and a stale zero/blank means inherit, so an old value can
                # never silently disable the gate entirely.
                self.assertEqual(
                    assertions.effective_max_attempts(
                        {"metadata": {"assertions_max_attempts": 0}}), 3)

    def test_the_budget_is_wired_through_every_layer_it_promises(self):
        """``effective_max_attempts`` documents an agent limit clamped to an app
        ceiling, so both layers must actually exist — the app setting, the
        per-agent field, and the UI surfaces that edit them. A key nothing
        declares is a knob the documented promise cannot keep."""
        settings = Path("app/admin/settings.py").read_text(encoding="utf-8")
        api = Path("app/api/agents.py").read_text(encoding="utf-8")
        self.assertIn("run_max_assertions_attempts: int =", settings)
        self.assertIn("assertions_max_attempts: Optional[int] = None", api)
        self.assertIn('payload.pop("assertions_max_attempts", None)', api)
        self.assertIn('("assertions_max_attempts", assertions_max_in)', api)
        self.assertIn('result["assertions_max_attempts"]', api)

        # App Settings → App Functions carries the app-wide ceiling …
        page = Path("ui/main-panel/app/settings/pages/app-functions/page.html"
                    ).read_text(encoding="utf-8")
        self.assertIn('id="ac-assertions-max-attempts"', page)
        app_settings = Path(
            "ui/main-panel/app/settings/app-settings/app-settings.js"
        ).read_text(encoding="utf-8")
        self.assertIn("_initAssertionsGate", app_settings)
        self.assertIn("run_max_assertions_attempts: attempts", app_settings)

        # … and Agents → Runtime → Limits carries the per-agent override.
        content = Path("ui/main-panel/agents/configs/content.js").read_text(
            encoding="utf-8")
        self.assertIn("run_max_assertions_attempts", content)
        self.assertIn("assertions_max_attempts: val", content)


if __name__ == "__main__":
    unittest.main()

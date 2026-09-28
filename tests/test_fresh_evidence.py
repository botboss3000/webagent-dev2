import ast
import json
import re
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from app.agent import fresh_evidence as fe
from app.agent.execution_modes import execution_modes_for_agent


def test_every_result_has_required_fresh_evidence_fields():
    ev = fe.build_tool_evidence(
        tool_name="browser_backend",
        tool_args={"mode": "headless"},
        content='{"success": false, "status": "unavailable", "message": "Playwright executable missing"}',
        success=True,
    )
    assert {
        "observed_at", "environment_generation", "environment_id",
        "resource_scope", "resource_id", "state_dependent",
        "recommended_revalidation_call",
    } <= set(ev)
    assert ev["resource_scope"] == "headless_container"
    assert ev["negative_capability"] is True


def test_nonzero_shell_missing_executable_is_negative_capability():
    ev = fe.build_tool_evidence(
        tool_name="run_command",
        tool_args={"command": "node --check app.js"},
        content={"exit_code": 127, "stdout": "", "stderr": "node: not found"},
        success=True,
    )

    assert ev["negative_capability"] is True
    assert ev["resource_scope"] == "headless_container"


def test_browser_backend_failures_do_not_cross_scopes():
    headless = fe.build_tool_evidence(
        tool_name="browser_backend",
        tool_args={"mode": "headless"},
        content='{"success": false, "message": "Playwright executable missing"}',
        success=True,
    )
    local = fe.build_tool_evidence(
        tool_name="browser_backend",
        tool_args={"mode": "local"},
        content='{"success": false, "message": "No device Chrome"}',
        success=True,
    )
    assert headless["resource_scope"] == "headless_container"
    assert local["resource_scope"] == "local_device"
    stale, reason, _ = fe.evidence_staleness(
        headless, current_scope="local_device", current_resource_id="browser",
    )
    assert stale is True
    assert reason == "resource_scope_changed"


def test_generation_change_invalidates_negative_result_and_requires_revalidation():
    ev = fe.build_tool_evidence(
        tool_name="browser_backend",
        tool_args={"mode": "headless"},
        content='{"success": false, "status": "unavailable"}',
        success=False,
    )
    fe.increment_environment_generation(ev["resource_scope"], ev["resource_id"])
    stale, reason, current = fe.evidence_staleness(ev)
    assert stale is True
    assert reason == "environment_generation_changed"
    notice = fe.recalled_evidence_notice(ev)
    assert f"Historical result from environment generation {ev['environment_generation']}" in notice
    assert f"current generation is {current}. Revalidation required." in notice


def test_browser_action_uses_backend_embedded_in_python_mapping_result():
    local = fe.build_tool_evidence(
        tool_name="browser_action",
        tool_args={"action": "title"},
        content="{'success': False, 'backend': 'local', 'error': 'No device Chrome'}",
        success=True,
    )
    headless = fe.build_tool_evidence(
        tool_name="browser_action",
        tool_args={"action": "title"},
        content="{'success': False, 'backend': 'headless', 'error': 'Playwright executable missing'}",
        success=True,
    )
    assert (local["resource_scope"], headless["resource_scope"]) == (
        "local_device", "headless_container",
    )


def test_age_threshold_invalidates_state_dependent_result():
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    ev = fe.build_tool_evidence(
        tool_name="browser_action",
        tool_args={"action": "title"},
        content='{"success": false, "message": "browser unavailable"}',
        success=False,
        observed_at=old,
    )
    stale, reason, _ = fe.evidence_staleness(ev)
    assert stale is True
    assert reason == "age_threshold_exceeded"


def test_successful_mutation_advances_generation_and_is_not_auto_replayed():
    before = fe.environment_generation("headless_container", "application")
    ev = fe.build_tool_evidence(
        tool_name="run_command",
        tool_args={"command": "pip install example"},
        content='{"exit_code": 0}',
        success=True,
        mutating=True,
        advance_generation=True,
    )
    assert ev["environment_generation"] != before
    assert ev["automatic_retry"]["allowed_when_stale"] is False
    assert ev["recommended_revalidation_call"]["tool"] is None


def test_safe_read_only_result_recommends_one_retry():
    ev = fe.build_tool_evidence(
        tool_name="browser_backend",
        tool_args={},
        content='{"backend": "headless"}',
        success=True,
        mutating=False,
    )
    assert ev["automatic_retry"] == {"allowed_when_stale": True, "max_attempts": 1}
    assert ev["recommended_revalidation_call"]["tool"] == "browser_backend"


def test_non_state_dependent_result_does_not_become_stale():
    ev = fe.build_tool_evidence(
        tool_name="policy_gate",
        tool_args={},
        content="blocked",
        success=False,
        state_dependent=False,
    )
    fe.increment_environment_generation(ev["resource_scope"], ev["resource_id"])
    assert fe.evidence_staleness(ev)[0] is False


def test_every_loop_tool_result_event_carries_evidence():
    root = Path(__file__).resolve().parents[1]
    tree = ast.parse((root / "app/agent/loop.py").read_text(encoding="utf-8"))
    events = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        pairs = {
            key.value: value
            for key, value in zip(node.keys, node.values)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        type_node = pairs.get("type")
        if isinstance(type_node, ast.Constant) and type_node.value == "tool_result":
            events.append(pairs)
    assert len(events) >= 6
    assert all("evidence" in event for event in events)


def test_compaction_preserves_metadata_and_generation_notice():
    from app.agent.compaction import _render_transcript

    ev = fe.build_tool_evidence(
        tool_name="browser_backend",
        tool_args={"mode": "headless"},
        content='{"success": false, "message": "Playwright executable missing"}',
        success=False,
    )
    fe.increment_environment_generation(ev["resource_scope"], ev["resource_id"])
    row = SimpleNamespace(
        role="tool",
        tool_name="browser_backend",
        content="Playwright executable missing",
        metadata=json.dumps({"fresh_evidence": ev}),
        source=None,
    )
    rendered = _render_transcript([row])
    assert "FRESH_EVIDENCE:" in rendered
    assert ev["observed_at"] in rendered
    assert "Historical result from environment generation" in rendered
    assert "Revalidation required." in rendered


def test_cache_gate_instruction_is_rendered_only_by_the_contract_block():
    import asyncio

    from app.agent.contracts import contract_prompt_block, parse_contract
    from app.agent.manager import _format_manager_prompt
    from app.agent.output_closer import _format_closer_template
    from app.agent.prompts import build_system_prompt

    default_instruction = "After any repository change, increment the numeric CACHE suffix in sw.js."
    enabled = {"metadata": {"contract": {
        "require_final_service_worker_cache_bump": True,
    }}}
    disabled = {"metadata": {"contract": {
        "require_final_service_worker_cache_bump": False,
    }}}
    custom = {"metadata": {"gates": [{
        "type": "cache_bump", "enabled": True,
        "message": "Use cache generation 42 as the final edit.",
    }]}}

    formatter_calls = (
        lambda agent: _format_manager_prompt(
            "watchdog", "Custom {user_request}", "request", [], "", agent,
        ),
        lambda agent: _format_closer_template(
            "Custom {user_request}", [], "request", [], "", agent,
        ),
        lambda agent: contract_prompt_block(parse_contract(agent), agent),
    )
    for render in formatter_calls:
        active = render(enabled)
        inactive = render(disabled)
        overridden = render(custom)
        assert active.count(default_instruction) == 1
        assert default_instruction not in inactive
        assert "Use cache generation 42 as the final edit." in overridden
        assert default_instruction not in overridden
        assert "# WebAgent service-worker cache contract" not in active

    base_prompt = asyncio.run(build_system_prompt([], agent_rec=enabled))
    assert default_instruction not in base_prompt

    loop_source = (Path(__file__).resolve().parents[1] / "app/agent/loop.py").read_text(
        encoding="utf-8"
    )
    assert "_contract, _agent_rec, mode=execution_mode" in loop_source


def test_seed_and_mode_prompt_catalogs_are_aligned():
    root = Path(__file__).resolve().parents[1]
    for path in (
        root / "data/agents/default.json",
        root / "app/defaults/agents/default.json",
    ):
        seed = json.loads(path.read_text(encoding="utf-8"))
        assert seed["version"] >= 37
        assert seed["metadata"]["contract"]["require_browser_journey_for_ui_change"] is True
        assert seed["metadata"]["contract"]["require_final_service_worker_cache_bump"] is True
        assert "current resource generation" in seed["skills_prompt"]
        assert "UI browser-journey verification" in seed["skills_prompt"]
        assert "Service-worker cache discipline" not in seed["skills_prompt"]
        assert "User-named reference implementation" in seed["skills_prompt"]
        assert "negative capability result is stale" in seed["tasks_prompt"]
        assert "browser journey through the affected route" in seed["tasks_prompt"]
        assert "final source edit" not in seed["tasks_prompt"]
        labels = [
            item.get("label", "")
            for mode in seed["metadata"]["execution_modes"]
            if mode.get("id") == "auto"
            for item in mode.get("contract", {}).get("checklist", [])
        ]
        assert any("stale-evidence revalidation" in label for label in labels)
        assert any("fresh post-change browser journey" in label for label in labels)
        assert not any("final sw.js cache-generation bump" in label for label in labels)

    builtin_auto = next(
        mode for mode in execution_modes_for_agent(None) if mode["id"] == "auto"
    )
    builtin_labels = [
        item.get("label", "") for item in builtin_auto["contract"]["checklist"]
    ]
    assert not any("sw.js" in label or "cache-generation bump" in label for label in builtin_labels)

    for path in (
        root / "data/config/app-prompts.json",
        root / "app/defaults/app-prompts.json",
    ):
        catalog = json.loads(path.read_text(encoding="utf-8"))
        assert catalog["version"] >= 5
        templates = [
            item["template"]
            for group in ("execution_modes", "agent_template_prompts")
            for item in catalog[group].values()
            if isinstance(item, dict) and isinstance(item.get("template"), str)
        ]
        templates.extend(
            item["template"]
            for key, item in catalog["app_level_prompts"].items()
            if re.match(r"^(manager_|output_closer$|output_auditor$|planner_prompt$)", key)
            and isinstance(item, dict) and isinstance(item.get("template"), str)
        )
        assert templates
        assert all("UI-journey contract:" in template for template in templates)
        assert all("failed backend switch" in template.lower() for template in templates)


def test_replayed_evidence_is_explicitly_historical_without_retimestamping():
    original = fe.build_tool_evidence(
        tool_name="browser_action",
        tool_args={"action": "wait"},
        content='{"success": false, "message": "browser unavailable"}',
        success=False,
    )

    recalled = fe.mark_recalled_evidence(original)

    assert recalled["historical_observation"] is True
    assert recalled["recalled_at"]
    assert recalled["observed_at"] == original["observed_at"]
    assert (
        recalled["environment_generation"] == original["environment_generation"]
    )

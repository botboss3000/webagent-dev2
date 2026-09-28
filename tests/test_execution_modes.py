"""Execution modes own access and prompt injection; gates own behavior."""
from pathlib import Path

from app.agent.execution_modes import (
    accumulated_contract, execution_modes_for_agent, mode_gate_locals,
    normalize_execution_modes, resolve_execution_mode,
)


def test_default_modes_have_access_and_prompt_but_no_completion_contract():
    modes = execution_modes_for_agent(None)
    assert [mode["id"] for mode in modes[:3]] == ["ask", "plan", "auto"]
    assert [mode["permission_policy"] for mode in modes[:3]] == ["read_only", "read_only", "write"]
    assert all("contract" not in mode for mode in modes)


def test_default_seed_has_no_mode_or_baseline_contract():
    import json
    root = Path(__file__).resolve().parents[1]
    for name in ("app/defaults/agents/default.json", "data/agents/default.json"):
        metadata = json.loads((root / name).read_text())["metadata"]
        assert "contract" not in metadata
        assert all("contract" not in mode for mode in metadata["execution_modes"])


def test_custom_mode_is_added_with_access_and_prompt_only():
    modes = normalize_execution_modes([{
        "id": "research", "label": "Research", "permission_policy": "read_only",
        "prompt": "Investigate and cite evidence.", "contract": {"checklist": ["ignored"]},
    }])
    research = modes[-1]
    assert research["id"] == "research"
    assert research["prompt"] == "Investigate and cite evidence."
    assert "contract" not in research


def test_invalid_policy_fails_safe_and_legacy_aliases_normalize():
    modes = normalize_execution_modes([{"id": "research", "permission_policy": "unsafe"}])
    assert modes[-1]["id"] == "research"
    assert modes[-1]["permission_policy"] == "read_only"


def test_mode_contract_helpers_are_inert_after_retirement():
    modes = normalize_execution_modes([{
        "id": "research", "permission_policy": "write",
        "contract": {"require_plan_document": True, "checklist": ["old"]},
    }])
    assert accumulated_contract({"metadata": {"execution_modes": modes}}, ["research"], "research")["checklist"] == []
    locals_ = mode_gate_locals({"metadata": {"execution_modes": modes}}, "research", ["research"])
    assert locals_["active_contract"] == {}
    assert locals_["requires_plan_document"] is False


def test_mode_resolution_keeps_prompt_and_write_access():
    agent = {"metadata": {"execution_modes": [{
        "id": "deploy", "label": "Deploy", "permission_policy": "write", "prompt": "Deploy safely."
    }]}}
    mode = resolve_execution_mode(agent, "deploy")
    assert mode["permission_policy"] == "write"
    assert mode["prompt"] == "Deploy safely."

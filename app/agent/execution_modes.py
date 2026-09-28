"""Modular, per-agent chat execution modes.

The permission policy is intentionally a two-state capability boundary:
``read_only`` never executes mutating tools and ``write`` may execute them. A
mode then layers its own prompt injection on top of that policy. Completion
behavior and mechanical enforcement are independently configured as gates.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Dict, List, Optional

_MODE_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
_POLICIES = {"read_only", "write"}


def _global_prompts() -> Dict[str, str]:
    try:
        from app.util.paths import app_prompts_path
        data = json.loads(app_prompts_path().read_text(encoding="utf-8"))
        entries = data.get("execution_modes") or {}
        prompts = {
            key: str((value or {}).get("template") or (value or {}).get("text") or "")
            for key, value in entries.items() if isinstance(value, dict)
        }
        # Keep customized catalogs written before Task Progress became the sole
        # plan checklist compatible without requiring a data rewrite.
        prompts["plan"] = prompts.get("plan", "").replace(
            "Use present_plan to maintain the persistent Plan Overview (stable id plan-overview) and Plan Checklist (stable id plan-checklist). Checklist steps must be concrete, dependency-ordered, bounded, and paired with observable outcomes. Implementation items remain unchecked in Plan mode.",
            "Use present_plan to maintain the persistent Plan Overview and merge the concrete execution checklist into canonical Task Progress. Steps must be detailed, bounded, and paired with observable outcomes; declare dependencies only where order is required. Implementation steps remain pending in Plan mode.",
        ).replace(
            "update both persistent plan documents before presenting the final plan",
            "update the Plan Overview and canonical Task Progress before presenting the final plan",
        )
        return prompts
    except Exception:
        return {}


def default_execution_modes() -> List[Dict[str, Any]]:
    """Return fresh default Ask, Plan, and Auto mode definitions."""
    prompts = _global_prompts()
    return [
        {
            "id": "ask", "label": "Ask",
            "description": "Investigate, answer, and recommend without making changes.",
            "permission_policy": "read_only", "prompt": prompts.get("ask", ""),
            "builtin": True,
        },
        {
            "id": "plan", "label": "Plan",
            "description": "Investigate deeply and deliver an execution-ready plan.",
            "permission_policy": "read_only", "prompt": prompts.get("plan", ""),
            "builtin": True,
        },
        {
            "id": "auto", "label": "Auto",
            "description": "Work autonomously with write-capable permissions.",
            "permission_policy": "write", "prompt": prompts.get("auto", ""),
            "builtin": True,
        },
    ]

def normalize_mode_id(value: Any, fallback: str = "ask") -> str:
    mode_id = str(value or "").strip().lower()
    mode_id = {"read": "plan", "write": "ask"}.get(mode_id, mode_id)
    return mode_id if _MODE_ID_RE.fullmatch(mode_id) else fallback


def _normalize_one(raw: Any, *, builtin: bool = False) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    mode_id = normalize_mode_id(raw.get("id"), fallback="")
    if not mode_id:
        return None
    policy = str(raw.get("permission_policy") or "read_only").strip().lower()
    if policy not in _POLICIES:
        policy = "read_only"
    # Completion behavior is configured exclusively by catalog gates. Historical
    # mode contract/checklist fields are not normalized or retained.
    return {
        "id": mode_id,
        "label": (str(raw.get("label") or mode_id).strip() or mode_id)[:40],
        "description": str(raw.get("description") or "").strip()[:240],
        "permission_policy": policy,
        "prompt": str(raw.get("prompt") or "").strip()[:30000],
        "builtin": builtin,
    }


def normalize_execution_modes(raw: Any) -> List[Dict[str, Any]]:
    """Merge stored mode overrides with the non-removable defaults."""
    defaults = default_execution_modes()
    default_map = {item["id"]: item for item in defaults}
    supplied = raw if isinstance(raw, list) else []
    custom: List[Dict[str, Any]] = []
    seen = set()
    for item in supplied:
        mode_id = normalize_mode_id(item.get("id") if isinstance(item, dict) else None, "")
        if not mode_id or mode_id in seen:
            continue
        seen.add(mode_id)
        if mode_id in default_map:
            merged = {**default_map[mode_id], **item, "id": mode_id}
            normalized = _normalize_one(merged, builtin=True)
            if normalized:
                default_map[mode_id] = normalized
        else:
            normalized = _normalize_one(item, builtin=False)
            if normalized:
                custom.append(normalized)
    return [deepcopy(default_map[key]) for key in ("ask", "plan", "auto")] + custom[:17]


def execution_modes_for_agent(agent_rec: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if agent_rec:
        raw = agent_rec.get("metadata")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw) or {}
            except Exception:
                meta = {}
        elif isinstance(raw, dict):
            meta = raw
    return normalize_execution_modes(meta.get("execution_modes"))


def resolve_execution_mode(agent_rec: Optional[Dict[str, Any]], mode_id: Any) -> Dict[str, Any]:
    modes = execution_modes_for_agent(agent_rec)
    wanted = normalize_mode_id(mode_id)
    return next((mode for mode in modes if mode["id"] == wanted), modes[0])


def contract_for_mode(mode: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Mode contracts were retired in favor of independently scoped gates."""
    return {}


def contract_requirements(mode: Optional[Dict[str, Any]]) -> List[str]:
    items = contract_for_mode(mode).get("checklist") or []
    return [str(item.get("label") or "").strip() for item in items
            if isinstance(item, dict) and str(item.get("label") or "").strip()]


def accumulated_contract(
    agent_rec: Optional[Dict[str, Any]], mode_ids: List[str], current_mode: Any,
    *, run_scoped: bool = False, executed_modes: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Combine the active mode with carry-forward contracts used earlier."""
    current_id = normalize_mode_id(current_mode)
    ordered_ids = []
    for mode_id in [*(mode_ids or []), current_id]:
        normalized = normalize_mode_id(mode_id, fallback="")
        if normalized and normalized not in ordered_ids:
            ordered_ids.append(normalized)
    current_mode_entry = resolve_execution_mode(agent_rec, current_id)
    current_is_write = current_mode_entry.get("permission_policy") == "write"
    # Read-only turns have a terminal deliverable of their own (a proposal or
    # plan). Historical write-mode completion requirements must never leak back
    # into them and demand implementation. Planning requirements carry forward
    # only when the session advances into a write-capable mode.
    if not current_is_write:
        if run_scoped:
            executed = {
                normalize_mode_id(mode_id, fallback="")
                for mode_id in (executed_modes or [])
            }
            # A prior write-capable segment participates only when it actually
            # performed mutating work during THIS run. This prevents an unused
            # Auto start from making a later Ask/Plan turn demand execution.
            ordered_ids = [
                mode_id for mode_id in ordered_ids
                if mode_id == current_id or (
                    mode_id in executed
                    and resolve_execution_mode(agent_rec, mode_id).get("permission_policy") == "write"
                )
            ]
        else:
            ordered_ids = [current_id]

    requirements = []
    sources = []
    seen = set()
    max_rounds = 0
    send_back = False
    for mode_id in ordered_ids:
        mode = resolve_execution_mode(agent_rec, mode_id)
        if mode["id"] != mode_id:
            continue
        contract = contract_for_mode(mode)
        if mode_id != current_id and not contract.get("carry_forward", True):
            continue
        sources.append(mode_id)
        max_rounds = max(max_rounds, int(contract.get("max_rounds") or 0))
        send_back = send_back or bool(contract.get("send_back"))
        for item in contract.get("checklist") or []:
            if not isinstance(item, dict):
                continue
            if mode_id != current_id and not item.get("carry_forward", True):
                continue
            item_id = str(item.get("id") or "")
            label = str(item.get("label") or "").strip()
            key = f"{mode_id}:{item_id or label}"
            if label and key not in seen:
                seen.add(key)
                requirements.append({"id": item_id, "label": label, "mode": mode_id})
    return {"mode_ids": sources, "checklist": requirements,
            "max_rounds": max_rounds, "send_back": send_back}


def mode_gate_locals(
    agent_rec: Optional[Dict[str, Any]],
    execution_mode: Any,
    mode_history: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Derive every mode-dependent local the agent loop's gates read.

    Single source of truth for BOTH the run-start capture in ``app/agent/loop.py``
    and a live ``set_execution_mode`` switch mid-turn. Keeping the two sites on one
    helper is deliberate: when the switch re-derived ``execution_mode`` but not
    these locals, a Plan -> Auto flip left ``requires_plan_document`` set, so the
    run still owed a Plan Overview, skipped its one mechanical retry, and ended
    with the misleading "plan workspace remains incomplete" blocker instead of
    finishing the task the user had just authorised.
    """
    entry = resolve_execution_mode(agent_rec, execution_mode)
    active = contract_for_mode(entry)
    return {
        "mode_entry": entry,
        "permission_policy": entry.get("permission_policy", "read_only"),
        "active_contract": active,
        "accumulated_contract": accumulated_contract(
            agent_rec, mode_history or [], execution_mode,
        ),
        "requires_plan_document": bool(active.get("require_plan_document")),
    }

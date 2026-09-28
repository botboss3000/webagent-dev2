"""Standalone Task Progress policy with a read-compatible Manager Loop migration.

Its configuration is exposed through both Config ▸ Modes and the Knowledge ▸
Task Progress ability card. The Knowledge ability toggle is the sole enable
control; both surfaces write the same canonical policy.

Task Progress used to be nested under ``metadata.manager`` and therefore could
never run unless the auditing Manager Loop was on.  Its canonical home is now
``metadata.task_progress``.  The legacy shape remains a read fallback so existing
agents retain their behavior until their next Task Progress save.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Dict, Optional

from app.agent.execution_modes import normalize_mode_id

_MODE_IDS = ("ask", "plan", "auto")


def _metadata(agent_rec: Optional[dict]) -> Dict[str, Any]:
    raw = (agent_rec or {}).get("metadata") or {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            raw = {}
    return dict(raw) if isinstance(raw, dict) else {}


def default_task_progress() -> Dict[str, Any]:
    return {
        "enabled": False,
        "modes": list(_MODE_IDS),
        # Active-task evidence is retained up to Context Control's final history
        # budget. Task Progress only owns the completed-task carryover allowance.
        "completed_task_evidence_tokens": 20000,
        "allow_self_completion": True,
    }


def normalize_task_progress(raw: Any, *, strict: bool = False) -> Dict[str, Any]:
    cfg = default_task_progress()
    if raw is None:
        return cfg
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("task_progress must be an object")
        return cfg
    if "enabled" in raw:
        if not isinstance(raw["enabled"], bool):
            if strict:
                raise ValueError("task_progress.enabled must be boolean")
        else:
            cfg["enabled"] = raw["enabled"]
    if "modes" in raw:
        modes = raw["modes"]
        if not isinstance(modes, list):
            if strict:
                raise ValueError("task_progress.modes must be a list")
        else:
            normalized = [normalize_mode_id(value, fallback="") for value in modes]
            if (not normalized or not all(normalized) or len(set(normalized)) != len(normalized)):
                if strict:
                    raise ValueError("task_progress.modes must contain unique configured mode IDs")
            else:
                cfg["modes"] = normalized
    # ``active_task_evidence_tokens`` was an early Task Progress sub-cap.
    # Accept and deliberately discard it on read/write for compatibility: Context
    # Control's model-aware history budget is now the only current-task limit.
    for key, minimum, maximum in (
        ("completed_task_evidence_tokens", 0, 1050000),
    ):
        if key not in raw:
            continue
        try:
            value = int(raw[key])
        except (TypeError, ValueError):
            if strict:
                raise ValueError(f"task_progress.{key} must be a number")
            continue
        if not minimum <= value <= maximum:
            if strict:
                raise ValueError(f"task_progress.{key} must be between {minimum} and {maximum}")
            value = max(minimum, min(maximum, value))
        cfg[key] = value
    if "allow_self_completion" in raw:
        if not isinstance(raw["allow_self_completion"], bool):
            if strict:
                raise ValueError("task_progress.allow_self_completion must be boolean")
        else:
            cfg["allow_self_completion"] = raw["allow_self_completion"]
    return cfg


def task_progress_for_agent(agent_rec: Optional[dict]) -> Dict[str, Any]:
    """Return canonical configuration, reading the legacy Manager block once."""
    meta = _metadata(agent_rec)
    if "task_progress" in meta:
        return normalize_task_progress(meta.get("task_progress"))
    # Compatibility migration: preserve prior Task Progress behavior without
    # retaining the former Manager Loop master-switch dependency.
    manager = meta.get("manager")
    legacy = manager.get("task_progress") if isinstance(manager, dict) else None
    return normalize_task_progress(legacy)


def resolve_task_progress(agent_rec: Optional[dict], mode_id: Any = None) -> Dict[str, Any]:
    cfg = deepcopy(task_progress_for_agent(agent_rec))
    selected = normalize_mode_id(mode_id, fallback="") if mode_id is not None else ""
    cfg["enabled"] = bool(cfg["enabled"] and (not selected or selected in cfg["modes"]))
    return cfg


def merge_task_progress_update(agent_rec: Optional[dict], incoming: Any) -> Dict[str, Any]:
    if not isinstance(incoming, dict):
        raise ValueError("task_progress must be an object")
    current = task_progress_for_agent(agent_rec)
    current.update(incoming)
    return normalize_task_progress(current, strict=True)

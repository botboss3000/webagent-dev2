"""Normalization for the per-agent, mode-aware Manager Loop configuration.

The canonical object is stored at ``metadata['manager']``.  The normalizer also
understands the original flat Manager keys, so existing agents remain opt-in and
keep their current behaviour.
"""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any, Dict, Optional

from app.agent.execution_modes import normalize_mode_id
from app.util.paths import app_prompts_path


TRIGGER_KINDS = ("plan_gate", "edit_gate", "commit_gate")
POLICIES = {"off", "async", "blocking"}
WATCHDOG_ACTIONS = {"observe", "advise", "replan", "verify", "pause_and_ask"}
REASONING_EFFORTS = {"minimal", "low", "medium", "high"}
WORKING_INTROS = {"off", "brief", "plan"}
INTRO_TIMINGS = {"before_preparation"}
UPDATE_POLICIES = {"off", "material", "phase"}
SCOUT_VISIBILITIES = {"background", "findings"}
TASK_PROGRESS_MODES = {"agent_native", "strict_staged", "off"}
TASK_PROGRESS_TRIGGER_MODES = {"interval", "task_boundary", "manual_only"}
TASK_PROGRESS_FAILURE_ACTIONS = {"pause_to_closer_review", "retry", "continue_with_warning", "abort_task"}
DEFAULT_KIND_BUDGETS = {
    "plan_gate": 1, "edit_gate": 4, "watchdog": 3, "commit_gate": 1,
}

# Concrete fallback text keeps the configured Manager contract visible even
# when an app-level prompt catalog cannot be read.
_PROMPT_FALLBACKS = {
    "starter": "Act as the Run Scout intake companion. Analyze the user request and any prior handoff, then seed a concise actionable plan and checklist. Preserve constraints, identify material unknowns, and do not claim work was performed.",
    "plan_gate": "Review the proposed first edit against the user request and plan. Return STRICT JSON with approve, revise, or block.",
    "edit_gate": "Review this pending edit for request alignment, scope, and safety. Return STRICT JSON with approve, revise, or block.",
    "commit_gate": "Review the proposed commit evidence for scope and verification. Return STRICT JSON with approve, revise, or block.",
    "watchdog": "Review whether the run is making evidence-based progress. Return STRICT JSON with on_track, off_track, or stuck.",
    "closer": "Write the final user-facing message for this run with concise Markdown bullets for framing, process, and optional context. INFORMATION PRESERVATION is highest priority: when the user asks for substantive information, provide the complete requested answer. Never summarize away, omit, truncate, or replace requested facts, values, names, lists, comparisons, code, steps, instructions, quotations, errors, or other answer content — even when the user did not explicitly request detail or a named format. If concise bullet limits would lose requested information, exceed them or use the format needed to preserve it. Audit the supplied checklist results, state completed work and remaining gaps honestly, and include the substance rather than referring to earlier messages.",
}

def _effective_prompt_defaults() -> Dict[str, str]:
    """Return concrete Manager and Closer defaults from the active prompt catalog."""
    defaults = dict(_PROMPT_FALLBACKS)
    try:
        data = json.loads(app_prompts_path().read_text(encoding="utf-8"))
        catalog = data.get("app_level_prompts") or {}
        for key in ("plan_gate", "edit_gate", "commit_gate", "watchdog"):
            entry = catalog.get(f"manager_{key}") or {}
            value = entry.get("template") or entry.get("text")
            if isinstance(value, str) and value.strip():
                defaults[key] = value.strip()
        entry = catalog.get("output_closer") or catalog.get("output_summarizer") or {}
        value = entry.get("template") or entry.get("text")
        if isinstance(value, str) and value.strip():
            defaults["closer"] = value.strip()
    except Exception:
        pass
    return defaults


def default_manager_loop() -> Dict[str, Any]:
    prompts = _effective_prompt_defaults()
    return {
        # Ship the supervisory Manager and its subagent contracts enabled. Per-agent
        # metadata can still explicitly opt out, and runtime App Functions remain
        # administratively overridable.
        "enabled": True,
        "model": None,
        "effort": None,
        "budgets": {
            "max_checks": 9,
            "max_blocks": 3,
            "by_trigger": dict(DEFAULT_KIND_BUDGETS),
        },
        "starter": {
            "enabled": True,
            "parallel": True,
            "wait_before_write": False,
            "inherit_prior_summary": True,
            "seed_plan": True,
            "seed_checklist": True,
            "prompt": prompts["starter"],
        },
        "communication": {
            "working_intro": "off",
            "intro_timing": "before_preparation",
            "intro_timeout_seconds": 12,
            "update_policy": "material",
            "scout_visibility": "background",
        },
        "task_progress": {
            "enabled": False,
            "mode": "agent_native",
            "auto_start": True,
            "trigger_mode": "interval",
            "max_turns_per_checkpoint": 20,
            "require_checkpoint_before_completion": True,
            "require_checkpoint_before_transition": True,
            "require_summary_per_checkpoint": True,
            "summary_min_chars": 120,
            "max_uncheckpointed_outcomes": 3,
            "reject_stale_checkpoint_updates": True,
            "allowed_checkpoint_failures": 2,
            "failure_action": "pause_to_closer_review",
            "append_failure_to_closer": True,
            "emit_checkpoint_trace": True,
            "keep_checkpoint_history": True,
            "max_checkpoints_stored": 25,
        },
        "triggers": {
            "plan_gate": {"policy": "off", "modes": ["plan", "auto"], "prompt": prompts["plan_gate"]},
            "edit_gate": {"policy": "off", "modes": ["auto"], "prompt": prompts["edit_gate"]},
            "commit_gate": {"policy": "off", "modes": ["auto"], "prompt": prompts["commit_gate"]},
        },
        "watchdog": {
            "enabled": True,
            "every_n_turns": 4,
            "on_errors": 1,
            "error_window": 8,
            "on_stall": True,
            "cooldown_turns": 2,
            "action": "replan",
            "modes": ["ask", "plan", "auto"],
            "prompt": prompts["watchdog"],
        },
        "closer": {
            # These settings shape the optional audit and handoff work. The
            # Closing Message gate alone activates Output Closer itself.
            "audit_mode_contract": True,
            "audit_agent_checklist": True,
            "require_plan_document": True,
            "require_manager_clear": False,
            "max_rounds": 1,
            "send_back": True,
            "prompt": prompts["closer"],
        },
        "contracts": {
            "enabled": True,
            "engine": "subagent",
            "failure_policy": "hybrid",
            "scout": {
                "enabled": True,
                "timeout_seconds": 60,
                "model": None,
            },
            "edit_review": {
                "enabled": True,
                "policy": "blocking",
                "timeout_seconds": 60,
                "max_checks": 4,
                "model": None,
            },
            "close_review": {
                "enabled": True,
                "policy": "blocking",
                "timeout_seconds": 120,
                "max_rounds": 1,
                "model": None,
            },
        },
        "mode_overrides": {},
    }


def _metadata(agent_rec: Optional[dict]) -> Dict[str, Any]:
    raw: Any = (agent_rec or {}).get("metadata", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) or {}
        except Exception:
            raw = {}
    return raw if isinstance(raw, dict) else {}


def _deep_merge(base: Dict[str, Any], incoming: Dict[str, Any]) -> Dict[str, Any]:
    out = deepcopy(base)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = deepcopy(value)
    return out


def _integer(value: Any, default: int, low: int, high: int, *, strict: bool) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        if strict:
            raise ValueError("must be an integer")
        return default
    if strict and not low <= number <= high:
        raise ValueError(f"must be between {low} and {high}")
    return max(low, min(high, number))


def _modes(value: Any, default: list[str], *, strict: bool) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list):
        if strict:
            raise ValueError("modes must be a list")
        return list(default)
    out: list[str] = []
    for item in value:
        mode_id = normalize_mode_id(item, fallback="")
        if not mode_id:
            if strict:
                raise ValueError(f"invalid mode id: {item!r}")
            continue
        if mode_id not in out:
            out.append(mode_id)
    return out


def _legacy_to_canonical(raw: Dict[str, Any], meta: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay original flat keys onto the canonical shape."""
    out = deepcopy(raw)
    triggers = out.setdefault("triggers", {}) if isinstance(out.get("triggers"), dict) else {}
    out["triggers"] = triggers
    saw_active = False
    for kind in TRIGGER_KINDS:
        if kind in raw:
            policy = str(raw.get(kind) or "off").lower()
            if policy == "on":
                policy = "async"
            trigger = triggers.get(kind) if isinstance(triggers.get(kind), dict) else {}
            trigger = dict(trigger)
            trigger.setdefault("policy", policy)
            triggers[kind] = trigger
            saw_active = saw_active or policy in ("async", "blocking")
        legacy_prompt = meta.get(f"manager_{kind}_prompt")
        if isinstance(legacy_prompt, str) and legacy_prompt.strip():
            trigger = triggers.get(kind) if isinstance(triggers.get(kind), dict) else {}
            trigger = dict(trigger)
            trigger.setdefault("prompt", legacy_prompt.strip())
            triggers[kind] = trigger
    if "max_checks" in raw or "max_blocks" in raw or "max_checks_by_kind" in raw:
        budgets = out.get("budgets") if isinstance(out.get("budgets"), dict) else {}
        budgets = dict(budgets)
        budgets.setdefault("max_checks", raw.get("max_checks"))
        budgets.setdefault("max_blocks", raw.get("max_blocks"))
        budgets.setdefault("by_trigger", raw.get("max_checks_by_kind"))
        out["budgets"] = budgets
    legacy_closer = meta.get("closer_prompt")
    if isinstance(legacy_closer, str) and legacy_closer.strip():
        closer = out.get("closer") if isinstance(out.get("closer"), dict) else {}
        closer = dict(closer)
        closer.setdefault("prompt", legacy_closer.strip())
        out["closer"] = closer
    legacy_wd = raw.get("watchdog")
    if ("require_stale_checkpoint_updates" not in out) and ("require_stale_revision_guard" in raw):
        legacy_reject = raw.get("require_stale_revision_guard")
        if isinstance(legacy_reject, bool):
            task_progress = out.get("task_progress") if isinstance(out.get("task_progress"), dict) else {}
            task_progress = dict(task_progress)
            task_progress.setdefault("reject_stale_checkpoint_updates", legacy_reject)
            out["task_progress"] = task_progress
    if legacy_wd == "on" or legacy_wd == "async":
        legacy_wd = {"every_n_turns": 6, "on_stall": True}
    if isinstance(legacy_wd, dict) and not (
        isinstance(out.get("watchdog"), dict) and "enabled" in out["watchdog"]
    ):
        wd = dict(legacy_wd)
        active = bool(wd.get("every_n_turns") or wd.get("on_errors") or wd.get("on_stall", True))
        wd["enabled"] = active
        out["watchdog"] = wd
        saw_active = saw_active or active
    legacy_wd_prompt = meta.get("manager_watchdog_prompt")
    if isinstance(legacy_wd_prompt, str) and legacy_wd_prompt.strip():
        wd = out.get("watchdog") if isinstance(out.get("watchdog"), dict) else {}
        out["watchdog"] = {**wd, "prompt": legacy_wd_prompt.strip()}
    # Old configurations had no master switch. Active triggers imply enabled.
    if "enabled" not in raw and saw_active:
        out["enabled"] = True
    return out


def normalize_manager_loop(
    raw: Any,
    *,
    strict: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return a complete canonical configuration.

    ``strict=True`` is intended for API writes and rejects malformed shapes or
    enums. Reads are deliberately forgiving so a bad historical row fails off.
    """
    if raw is None:
        raw = {}
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) or {}
        except Exception as exc:
            if strict:
                raise ValueError("manager_loop must be an object") from exc
            raw = {}
    if not isinstance(raw, dict):
        if strict:
            raise ValueError("manager_loop must be an object")
        raw = {}
    raw = _legacy_to_canonical(raw, metadata or {})
    cfg = _deep_merge(default_manager_loop(), raw)

    cfg["enabled"] = bool(cfg.get("enabled", False))
    model = cfg.get("model")
    effort = cfg.get("effort")
    cfg["model"] = str(model).strip()[:200] if model not in (None, "") else None
    cfg["effort"] = str(effort).strip().lower()[:40] if effort not in (None, "") else None
    if cfg["effort"] == "default":
        cfg["effort"] = None
    if cfg["effort"] is not None and cfg["effort"] not in REASONING_EFFORTS:
        if strict:
            raise ValueError("effort must be minimal, low, medium, high, or null")
        cfg["effort"] = None

    budgets = cfg.get("budgets")
    if not isinstance(budgets, dict):
        if strict:
            raise ValueError("budgets must be an object")
        budgets = {}
    by_kind = budgets.get("by_trigger")
    if not isinstance(by_kind, dict):
        if strict:
            raise ValueError("budgets.by_trigger must be an object")
        by_kind = {}
    normalized_by_kind = {}
    for kind, default in DEFAULT_KIND_BUDGETS.items():
        try:
            normalized_by_kind[kind] = _integer(by_kind.get(kind, default), default, 1, 100, strict=strict)
        except ValueError as exc:
            raise ValueError(f"budgets.by_trigger.{kind} {exc}") from exc
    try:
        max_checks = _integer(budgets.get("max_checks", 9), 9, 1, 500, strict=strict)
        max_blocks = _integer(budgets.get("max_blocks", 3), 3, 1, 100, strict=strict)
    except ValueError as exc:
        raise ValueError(f"budgets {exc}") from exc
    cfg["budgets"] = {"max_checks": max_checks, "max_blocks": max_blocks, "by_trigger": normalized_by_kind}

    starter = cfg.get("starter")
    if not isinstance(starter, dict):
        if strict:
            raise ValueError("starter must be an object")
        starter = {}
    starter_defaults = default_manager_loop()["starter"]
    cfg["starter"] = {
        key: (str(starter.get(key) or "").strip()[:30000] if key == "prompt" else bool(starter.get(key, default)))
        for key, default in starter_defaults.items()
    }

    trigger_defaults = default_manager_loop()["triggers"]
    triggers = cfg.get("triggers")
    if not isinstance(triggers, dict):
        if strict:
            raise ValueError("triggers must be an object")
        triggers = {}
    normalized_triggers: Dict[str, Any] = {}
    for kind in TRIGGER_KINDS:
        item = triggers.get(kind)
        if not isinstance(item, dict):
            if strict:
                raise ValueError(f"triggers.{kind} must be an object")
            item = {}
        policy = str(item.get("policy") or "off").strip().lower()
        if policy == "on":
            policy = "async"
        if policy not in POLICIES:
            if strict:
                raise ValueError(f"triggers.{kind}.policy must be off, async, or blocking")
            policy = "off"
        normalized_triggers[kind] = {
            "policy": policy,
            "modes": _modes(item.get("modes"), trigger_defaults[kind]["modes"], strict=strict),
            "prompt": str(item.get("prompt") or "").strip()[:30000],
        }
    cfg["triggers"] = normalized_triggers

    wd = cfg.get("watchdog")
    if not isinstance(wd, dict):
        if strict:
            raise ValueError("watchdog must be an object")
        wd = {}
    action = str(wd.get("action") or "advise").strip().lower()
    if action not in WATCHDOG_ACTIONS:
        if strict:
            raise ValueError("watchdog.action is invalid")
        action = "advise"
    try:
        cfg["watchdog"] = {
            "enabled": bool(wd.get("enabled", False)),
            "every_n_turns": _integer(wd.get("every_n_turns", 0), 0, 0, 1000, strict=strict),
            "on_errors": _integer(wd.get("on_errors", 0), 0, 0, 100, strict=strict),
            "error_window": _integer(wd.get("error_window", 8), 8, 1, 1000, strict=strict),
            "on_stall": bool(wd.get("on_stall", True)),
            "cooldown_turns": _integer(wd.get("cooldown_turns", 2), 2, 0, 1000, strict=strict),
            "action": action,
            "modes": _modes(wd.get("modes"), ["ask", "plan", "auto"], strict=strict),
            "prompt": str(wd.get("prompt") or "").strip()[:30000],
        }
    except ValueError as exc:
        raise ValueError(f"watchdog {exc}") from exc

    communication = cfg.get("communication")
    if not isinstance(communication, dict):
        if strict:
            raise ValueError("communication must be an object")
        communication = {}
    communication_defaults = default_manager_loop()["communication"]
    working_intro = str(
        communication.get("working_intro") or communication_defaults["working_intro"]
    ).strip().lower()
    intro_timing = str(
        communication.get("intro_timing") or communication_defaults["intro_timing"]
    ).strip().lower()
    update_policy = str(
        communication.get("update_policy") or communication_defaults["update_policy"]
    ).strip().lower()
    scout_visibility = str(
        communication.get("scout_visibility") or communication_defaults["scout_visibility"]
    ).strip().lower()
    invalid_communication = (
        (working_intro not in WORKING_INTROS, "communication.working_intro is invalid", "off"),
        (intro_timing not in INTRO_TIMINGS, "communication.intro_timing is invalid", "before_preparation"),
        (update_policy not in UPDATE_POLICIES, "communication.update_policy is invalid", "material"),
        (scout_visibility not in SCOUT_VISIBILITIES, "communication.scout_visibility is invalid", "background"),
    )
    values = [working_intro, intro_timing, update_policy, scout_visibility]
    for index, (invalid, message, fallback) in enumerate(invalid_communication):
        if not invalid:
            continue
        if strict:
            raise ValueError(message)
        values[index] = fallback
    working_intro, intro_timing, update_policy, scout_visibility = values
    cfg["communication"] = {
        "working_intro": working_intro,
        "intro_timing": intro_timing,
        "intro_timeout_seconds": _integer(
            communication.get("intro_timeout_seconds", communication_defaults["intro_timeout_seconds"]),
            communication_defaults["intro_timeout_seconds"], 1, 60, strict=strict,
        ),
        "update_policy": update_policy,
        "scout_visibility": scout_visibility,
    }

    closer = cfg.get("closer")
    task_progress = cfg.get("task_progress")
    if not isinstance(task_progress, dict):
        if strict:
            raise ValueError("task_progress must be an object")
        task_progress = {}
    tp_defaults = default_manager_loop()["task_progress"]
    mode = str(task_progress.get("mode") or tp_defaults["mode"]).strip().lower()
    trigger_mode = str(task_progress.get("trigger_mode") or tp_defaults["trigger_mode"]).strip().lower()
    failure_action = str(task_progress.get("failure_action") or tp_defaults["failure_action"]).strip().lower()
    if mode not in TASK_PROGRESS_MODES:
        if strict:
            raise ValueError("task_progress.mode is invalid")
        mode = tp_defaults["mode"]
    if trigger_mode not in TASK_PROGRESS_TRIGGER_MODES:
        if strict:
            raise ValueError("task_progress.trigger_mode is invalid")
        trigger_mode = tp_defaults["trigger_mode"]
    if failure_action not in TASK_PROGRESS_FAILURE_ACTIONS:
        if strict:
            raise ValueError("task_progress.failure_action is invalid")
        failure_action = tp_defaults["failure_action"]
    try:
        cfg["task_progress"] = {
            "enabled": bool(task_progress.get("enabled", tp_defaults["enabled"])),
            "mode": mode,
            "auto_start": bool(task_progress.get("auto_start", tp_defaults["auto_start"])),
            "trigger_mode": trigger_mode,
            "max_turns_per_checkpoint": _integer(
                task_progress.get("max_turns_per_checkpoint", tp_defaults["max_turns_per_checkpoint"]),
                tp_defaults["max_turns_per_checkpoint"], 1, 999, strict=strict,
            ),
            "require_checkpoint_before_completion": bool(
                task_progress.get("require_checkpoint_before_completion", tp_defaults["require_checkpoint_before_completion"])
            ),
            "require_checkpoint_before_transition": bool(
                task_progress.get("require_checkpoint_before_transition", tp_defaults["require_checkpoint_before_transition"])
            ),
            "require_summary_per_checkpoint": bool(
                task_progress.get("require_summary_per_checkpoint", tp_defaults["require_summary_per_checkpoint"])
            ),
            "summary_min_chars": _integer(
                task_progress.get("summary_min_chars", tp_defaults["summary_min_chars"]),
                tp_defaults["summary_min_chars"], 20, 2000, strict=strict,
            ),
            "max_uncheckpointed_outcomes": _integer(
                task_progress.get("max_uncheckpointed_outcomes", tp_defaults["max_uncheckpointed_outcomes"]),
                tp_defaults["max_uncheckpointed_outcomes"], 0, 50, strict=strict,
            ),
            "reject_stale_checkpoint_updates": bool(
                task_progress.get("reject_stale_checkpoint_updates", tp_defaults["reject_stale_checkpoint_updates"])
            ),
            "allowed_checkpoint_failures": _integer(
                task_progress.get("allowed_checkpoint_failures", tp_defaults["allowed_checkpoint_failures"]),
                tp_defaults["allowed_checkpoint_failures"], 0, 9, strict=strict,
            ),
            "failure_action": failure_action,
            "append_failure_to_closer": bool(task_progress.get("append_failure_to_closer", tp_defaults["append_failure_to_closer"])),
            "emit_checkpoint_trace": bool(task_progress.get("emit_checkpoint_trace", tp_defaults["emit_checkpoint_trace"])),
            "keep_checkpoint_history": bool(task_progress.get("keep_checkpoint_history", tp_defaults["keep_checkpoint_history"])),
            "max_checkpoints_stored": _integer(
                task_progress.get("max_checkpoints_stored", tp_defaults["max_checkpoints_stored"]),
                tp_defaults["max_checkpoints_stored"], 1, 500, strict=strict,
            ),
        }
    except ValueError as exc:
        raise ValueError("task_progress " + str(exc)) from exc
    if not isinstance(closer, dict):
        if strict:
            raise ValueError("closer must be an object")
        closer = {}
    closer_defaults = default_manager_loop()["closer"]
    try:
        cfg["closer"] = {
            **{key: bool(closer.get(key, default)) for key, default in closer_defaults.items()
               if key not in {"max_rounds", "prompt"}},
            "max_rounds": _integer(closer.get("max_rounds", 1), 1, 0, 5, strict=strict),
            "prompt": str(closer.get("prompt") or closer_defaults["prompt"]).strip()[:30000],
        }
    except ValueError as exc:
        raise ValueError(f"closer.max_rounds {exc}") from exc

    contracts = cfg.get("contracts")
    if not isinstance(contracts, dict):
        if strict:
            raise ValueError("contracts must be an object")
        contracts = {}
    contract_defaults = default_manager_loop()["contracts"]
    failure_policy = str(
        contracts.get("failure_policy") or contract_defaults["failure_policy"]
    ).strip().lower()
    if failure_policy not in {"hybrid", "fail_closed", "advisory"}:
        if strict:
            raise ValueError("contracts.failure_policy is invalid")
        failure_policy = "hybrid"
    engine = str(contracts.get("engine") or "subagent").strip().lower()
    if engine not in {"subagent", "single_call"}:
        if strict:
            raise ValueError("contracts.engine must be subagent or single_call")
        engine = "subagent"

    def _contract_lane(name: str, *, has_policy: bool = False,
                       has_rounds: bool = False, has_checks: bool = False) -> Dict[str, Any]:
        default = contract_defaults[name]
        value = contracts.get(name)
        if not isinstance(value, dict):
            if strict and value is not None:
                raise ValueError(f"contracts.{name} must be an object")
            value = {}
        lane: Dict[str, Any] = {
            "enabled": bool(value.get("enabled", default["enabled"])),
            "timeout_seconds": _integer(
                value.get("timeout_seconds", default["timeout_seconds"]),
                default["timeout_seconds"], 1, 600, strict=strict,
            ),
            "model": (
                str(value.get("model")).strip()[:200]
                if value.get("model") not in (None, "") else None
            ),
        }
        if has_policy:
            policy = str(value.get("policy") or default["policy"]).strip().lower()
            if policy not in POLICIES:
                if strict:
                    raise ValueError(f"contracts.{name}.policy is invalid")
                policy = default["policy"]
            lane["policy"] = policy
        if has_rounds:
            lane["max_rounds"] = _integer(
                value.get("max_rounds", default["max_rounds"]),
                default["max_rounds"], 0, 5, strict=strict,
            )
        if has_checks:
            lane["max_checks"] = _integer(
                value.get("max_checks", default["max_checks"]),
                default["max_checks"], 1, 100, strict=strict,
            )
        return lane

    cfg["contracts"] = {
        "enabled": bool(contracts.get("enabled", False)),
        "engine": engine,
        "failure_policy": failure_policy,
        "scout": _contract_lane("scout"),
        "edit_review": _contract_lane(
            "edit_review", has_policy=True, has_checks=True,
        ),
        "close_review": _contract_lane(
            "close_review", has_policy=True, has_rounds=True,
        ),
    }

    overrides = cfg.get("mode_overrides")
    if not isinstance(overrides, dict):
        if strict:
            raise ValueError("mode_overrides must be an object")
        overrides = {}
    normalized_overrides: Dict[str, Any] = {}
    for raw_mode, value in overrides.items():
        mode_id = normalize_mode_id(raw_mode, fallback="")
        if not mode_id or not isinstance(value, dict):
            if strict:
                raise ValueError(f"invalid mode_overrides entry: {raw_mode!r}")
            continue
        item: Dict[str, Any] = {}
        trigger_overrides = value.get("triggers", {})
        if trigger_overrides is not None and not isinstance(trigger_overrides, dict):
            if strict:
                raise ValueError(f"mode_overrides.{mode_id}.triggers must be an object")
            trigger_overrides = {}
        clean_triggers = {}
        for kind, policy_value in trigger_overrides.items():
            if kind not in TRIGGER_KINDS:
                continue
            policy = str(policy_value or "off").lower()
            if policy == "on":
                policy = "async"
            if policy not in POLICIES:
                if strict:
                    raise ValueError(f"mode_overrides.{mode_id}.triggers.{kind} is invalid")
                continue
            clean_triggers[kind] = policy
        if clean_triggers:
            item["triggers"] = clean_triggers
        if "watchdog" in value:
            watchdog_policy = str(value.get("watchdog") or "off").lower()
            if watchdog_policy == "on":
                watchdog_policy = "async"
            if watchdog_policy not in {"off", "async"}:
                if strict:
                    raise ValueError(f"mode_overrides.{mode_id}.watchdog is invalid")
            else:
                item["watchdog"] = watchdog_policy
        normalized_overrides[mode_id] = item
    cfg["mode_overrides"] = normalized_overrides
    # Do not leak legacy flat aliases or unknown keys into the public API.
    return {key: cfg[key] for key in default_manager_loop()}


def manager_loop_for_agent(agent_rec: Optional[dict]) -> Dict[str, Any]:
    meta = _metadata(agent_rec)
    raw = meta.get("manager")
    # New agents with no manager metadata inherit the enabled Manager + contract
    # defaults. Preserve explicit legacy flat configurations, however: their
    # trigger policies are a deliberate per-agent choice and must not suddenly be
    # overridden by the new default blocking subagent edit contract. A malformed
    # explicit value continues to fail safely off rather than unexpectedly enabling
    # supervisory work.
    if raw is not None and not isinstance(raw, dict):
        safe = default_manager_loop()
        safe["enabled"] = False
        safe["starter"]["enabled"] = False
        safe["contracts"]["enabled"] = False
        return normalize_manager_loop(safe, metadata=meta)
    if isinstance(raw, dict) and "contracts" not in raw:
        legacy_keys = set(TRIGGER_KINDS) | {"max_checks", "max_blocks", "max_checks_by_kind"}
        if any(key in raw for key in legacy_keys):
            raw = _deep_merge(raw, {"contracts": {"enabled": False}})
    return normalize_manager_loop(raw, metadata=meta)


def merge_manager_loop_update(agent_rec: Optional[dict], incoming: Any) -> Dict[str, Any]:
    """Validate and deep-merge a partial API update over effective config."""
    if not isinstance(incoming, dict):
        raise ValueError("manager_loop must be an object")
    current = manager_loop_for_agent(agent_rec)
    return normalize_manager_loop(_deep_merge(current, incoming), strict=True)


def resolve_manager_loop(agent_rec: Optional[dict], mode_id: Any = None) -> Dict[str, Any]:
    """Resolve mode applicability/overrides without changing mode permissions."""
    cfg = manager_loop_for_agent(agent_rec)
    selected = normalize_mode_id(mode_id, fallback="") if mode_id is not None else ""
    resolved = deepcopy(cfg)
    for kind, trigger in resolved["triggers"].items():
        policy = trigger["policy"] if cfg["enabled"] else "off"
        if selected and selected not in trigger["modes"]:
            policy = "off"
        override = cfg["mode_overrides"].get(selected, {}).get("triggers", {}).get(kind)
        if override is not None:
            policy = override if cfg["enabled"] else "off"
        trigger["policy"] = policy
    wd = resolved["watchdog"]
    wd_enabled = cfg["enabled"] and wd["enabled"] and (not selected or selected in wd["modes"])
    wd_override = cfg["mode_overrides"].get(selected, {}).get("watchdog")
    if wd_override is not None:
        wd_enabled = cfg["enabled"] and wd_override == "async"
    wd["enabled"] = wd_enabled
    if not cfg["enabled"]:
        # The master switch governs Manager workers. Output Closer activation
        # belongs exclusively to the Closing Message gate.
        resolved["contracts"]["enabled"] = False
        resolved["starter"]["enabled"] = False
    return resolved


def legacy_manager_view(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Flat keys consumed by the existing loop, plus the canonical config."""
    out = deepcopy(cfg)
    for kind in TRIGGER_KINDS:
        out[kind] = cfg["triggers"][kind]["policy"]
    out["max_checks_by_kind"] = dict(cfg["budgets"]["by_trigger"])
    contracts = cfg.get("contracts") or {}
    edit_contract = contracts.get("edit_review") or {}
    if (cfg.get("enabled") and contracts.get("enabled")
            and contracts.get("engine") == "subagent"
            and edit_contract.get("enabled")
            and edit_contract.get("policy") != "off"):
        # The legacy loop remains the lifecycle hook. Contract policy makes
        # the two edit gates mandatory even when their single-call switches
        # are off; run_manager_check then chooses contract or fallback.
        out["plan_gate"] = edit_contract["policy"]
        out["edit_gate"] = edit_contract["policy"]
        out["max_checks_by_kind"]["plan_gate"] = max(
            1, int(edit_contract.get("max_checks") or 1),
        )
        out["max_checks_by_kind"]["edit_gate"] = max(
            1, int(edit_contract.get("max_checks") or 1),
        )
    wd = cfg["watchdog"]
    out["watchdog"] = ({
        "every_n_turns": wd["every_n_turns"],
        "on_errors": wd["on_errors"],
        "error_window": wd["error_window"],
        "on_stall": wd["on_stall"],
        "cooldown_turns": wd["cooldown_turns"],
    } if wd["enabled"] else "off")
    out["max_checks"] = cfg["budgets"]["max_checks"]
    out["max_blocks"] = cfg["budgets"]["max_blocks"]
    return out

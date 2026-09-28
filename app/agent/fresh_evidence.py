"""Fresh-evidence metadata for state-dependent tool observations.

Tool output is an observation of one resource at one point in time, not a
timeless capability fact. This module provides a small, secret-safe envelope
that can be persisted with every result and compared with current runtime state.
"""

from __future__ import annotations
import ast

import hashlib
import json
import os
import re
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping


SCHEMA_VERSION = 1
DEFAULT_STALE_AFTER_SECONDS = 15 * 60

FRESH_EVIDENCE_PROMPT = """# [FRESH EVIDENCE CONTRACT]
Treat tool results as observations scoped to a particular resource and environment generation. Every result's evidence metadata states when and where it was observed, whether it depends on mutable state, and the smallest recommended revalidation call. Before relying on an earlier negative capability result, verify that its resource scope, resource identity, generation, and age still match current state. A container or process restart, image/dependency/configuration change, backend/host/device switch, relevant mutation, or expired age makes that negative result stale. Stale negatives may guide planning but must never block execution.

For a safe read-only or reversible action, automatically retry the smallest focused probe once when prior evidence is stale. Do not merely repeat the historical error. Never generalize a failure from one browser backend, host, container, or device to another: local_device, headless_container, connector, and other scopes are independent. Before saying a capability is unavailable, obtain a fresh failed probe against the currently selected resource generation after the latest relevant mutation. If recalled evidence says that its historical and current generations differ, revalidation is required. Never repeat a mutation merely to refresh evidence."""

_PROCESS_EPOCH = f"{int(time.time())}-{uuid.uuid4().hex[:10]}"
_LOCK = threading.Lock()
_REVISIONS: dict[str, int] = {}

_NEGATIVE_CAPABILITY_RE = re.compile(
    r"\b(?:unavailable|not available|not installed|missing executable|"
    r"executable (?:is )?missing|not found|no (?:device|browser|chrome|backend)|"
    r"cannot (?:connect|attach|launch|start)|can't (?:connect|attach|launch|start)|"
    r"unsupported|dependency missing|module not found)\b",
    re.I,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _host_identity() -> str:
    raw = os.environ.get("HOSTNAME") or socket.gethostname() or "unknown-host"
    return re.sub(r"[^A-Za-z0-9_.-]", "-", raw)[:80]


def _resource_key(scope: str, resource_id: str) -> str:
    return f"{scope}:{resource_id}"


def environment_generation(scope: str, resource_id: str = "default") -> str:
    key = _resource_key(scope, resource_id)
    with _LOCK:
        revision = _REVISIONS.get(key, 0)
    raw = f"{_host_identity()}:{_PROCESS_EPOCH}:{key}:{revision}"
    return "gen-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:18]


def increment_environment_generation(scope: str, resource_id: str = "default") -> str:
    """Invalidate cached state for one resource and return its new generation."""
    key = _resource_key(scope, resource_id)
    with _LOCK:
        _REVISIONS[key] = _REVISIONS.get(key, 0) + 1
    return environment_generation(scope, resource_id)


def _as_mapping(content: Any) -> dict[str, Any]:
    if isinstance(content, Mapping):
        return dict(content)
    text = str(content or "").strip()
    if not text or len(text) > 100_000:
        return {}
    for loader in (json.loads, ast.literal_eval):
        try:
            value = loader(text)
        except Exception:
            continue
        if isinstance(value, Mapping):
            return dict(value)
    return {}


def _backend_from_result(content: Any) -> str:
    data = _as_mapping(content)
    backend = str(data.get("backend") or data.get("mode") or "").lower()
    return backend if backend in {"local", "headless", "connector"} else ""


def resource_for_tool(
    tool_name: str,
    tool_args: Mapping[str, Any] | None = None,
    content: Any = None,
) -> tuple[str, str]:
    """Resolve a resource scope without generalising browser backends."""
    args = tool_args or {}
    if tool_name in {"browser_backend", "browser_action"}:
        backend = str(args.get("mode") or "").lower() or _backend_from_result(content)
        if backend == "local":
            return "local_device", str(args.get("browser_session_id") or "browser")
        if backend == "connector":
            return "browser_connector", str(args.get("browser_session_id") or "browser")
        if backend == "headless":
            return "headless_container", str(args.get("browser_session_id") or "browser")
        return "browser_session", str(args.get("browser_session_id") or "browser")
    if tool_name in {"computer_control", "computer_action"}:
        return "local_device", str(args.get("device_id") or "default")
    if tool_name in {"spawn_agent", "message_agent", "check_agents"}:
        return "agent_runtime", str(args.get("agent_id") or "default")
    return "headless_container", "application"


def _safe_revalidation_call(
    tool_name: str,
    tool_args: Mapping[str, Any],
    *,
    mutating: bool,
) -> dict[str, Any]:
    if mutating:
        if tool_name == "browser_backend":
            return {"tool": "browser_backend", "arguments": {}, "reason": "read current backend status"}
        return {
            "tool": None,
            "arguments": {},
            "reason": "use the smallest read-only status or inspection probe for this resource",
        }
    safe_args = {
        str(k): v for k, v in tool_args.items()
        if str(k).lower() not in {
            "api_key", "token", "password", "secret", "authorization", "headers",
            "body", "content", "text", "script", "code",
        }
    }
    return {"tool": tool_name, "arguments": safe_args, "reason": "repeat once if prior negative evidence is stale"}


def is_negative_capability_result(*, success: bool, content: Any) -> bool:
    data = _as_mapping(content)
    if data.get("capability_available") is False:
        return True
    status = str(data.get("status") or "").lower()
    structured_failure = (
        data.get("success") is False
        or status in {"error", "unavailable", "unsupported", "missing"}
        or bool(data.get("timed_out", False))
    )
    try:
        exit_failed = (
            data.get("exit_code") is not None
            and int(data.get("exit_code")) != 0
        )
    except (TypeError, ValueError):
        exit_failed = True
    text = str(content or "")[:20_000]
    return bool(
        _NEGATIVE_CAPABILITY_RE.search(text)
        and (structured_failure or exit_failed or not success)
    )


def build_tool_evidence(
    *,
    tool_name: str,
    tool_args: Mapping[str, Any] | None,
    content: Any,
    success: bool,
    state_dependent: bool = True,
    mutating: bool = False,
    observed_at: str | None = None,
    advance_generation: bool = False,
) -> dict[str, Any]:
    args = dict(tool_args or {})
    scope, resource_id = resource_for_tool(tool_name, args, content)
    if advance_generation and mutating and success:
        generation = increment_environment_generation(scope, resource_id)
    else:
        generation = environment_generation(scope, resource_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "observed_at": observed_at or _utc_now(),
        "environment_generation": generation,
        "environment_id": _host_identity(),
        "resource_scope": scope,
        "resource_id": resource_id,
        "state_dependent": bool(state_dependent),
        "negative_capability": (
            is_negative_capability_result(success=success, content=content)
            if state_dependent else False
        ),
        "stale_after_seconds": DEFAULT_STALE_AFTER_SECONDS,
        "recommended_revalidation_call": _safe_revalidation_call(
            tool_name, args, mutating=mutating,
        ),
        "automatic_retry": {
            "allowed_when_stale": bool(state_dependent and not mutating),
            "max_attempts": 1,
        },
    }


def evidence_staleness(
    evidence: Mapping[str, Any] | None,
    *,
    current_scope: str | None = None,
    current_resource_id: str | None = None,
    now: datetime | None = None,
) -> tuple[bool, str, str]:
    """Return (stale, reason, current_generation)."""
    ev = dict(evidence or {})
    if not ev.get("state_dependent"):
        return False, "not_state_dependent", str(ev.get("environment_generation") or "")
    scope = current_scope or str(ev.get("resource_scope") or "")
    resource_id = current_resource_id or str(ev.get("resource_id") or "default")
    current = environment_generation(scope, resource_id)
    if current_scope and current_scope != ev.get("resource_scope"):
        return True, "resource_scope_changed", current
    if current_resource_id and current_resource_id != ev.get("resource_id"):
        return True, "resource_changed", current
    if str(ev.get("environment_generation") or "") != current:
        return True, "environment_generation_changed", current
    raw_time = str(ev.get("observed_at") or "")
    try:
        observed = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
        age = ((now or datetime.now(timezone.utc)) - observed).total_seconds()
        if age > int(ev.get("stale_after_seconds") or DEFAULT_STALE_AFTER_SECONDS):
            return True, "age_threshold_exceeded", current
    except Exception:
        return True, "invalid_observation_time", current
    return False, "fresh", current


def extract_fresh_evidence(metadata: Any) -> dict[str, Any]:
    """Read a persisted evidence envelope from interaction metadata."""
    if isinstance(metadata, Mapping):
        data = dict(metadata)
    else:
        try:
            data = json.loads(str(metadata or ""))
        except Exception:
            return {}
    evidence = data.get("fresh_evidence") if isinstance(data, Mapping) else None
    return dict(evidence) if isinstance(evidence, Mapping) else {}


def recalled_evidence_notice(evidence: Mapping[str, Any] | None) -> str:
    ev = dict(evidence or {})
    stale, reason, current = evidence_staleness(ev)
    historical = str(ev.get("environment_generation") or "unknown")
    if stale and ev.get("negative_capability"):
        return (
            f"Historical result from environment generation {historical}; current "
            f"generation is {current}. Revalidation required. Reason: {reason}."
        )
    return ""


def mark_recalled_evidence(
    evidence: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Label a replay without pretending it was freshly observed.

    The original observed_at and generation remain authoritative. recalled_at
    records when the durable result re-entered the current run.
    """
    recalled = dict(evidence or {})
    recalled["historical_observation"] = True
    recalled["recalled_at"] = _utc_now()
    return recalled


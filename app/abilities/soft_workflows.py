"""Bounded interpreter for per-agent database-backed soft abilities."""
from __future__ import annotations

import asyncio
import inspect
import json
import re
import time
from typing import Any, Callable, Dict, Optional

MAX_STEPS = 25
MAX_SECONDS = 30.0
MAX_RESULT_CHARS = 20000
_TOKEN = re.compile(r"^\$\{(inputs|steps)\.([A-Za-z0-9_.-]+)\}$")


class WorkflowError(ValueError):
    pass


def _resolve(value: Any, inputs: dict, results: dict) -> Any:
    if isinstance(value, str):
        match = _TOKEN.fullmatch(value)
        if match:
            source = inputs if match.group(1) == "inputs" else results
            current: Any = source
            for part in match.group(2).split("."):
                if not isinstance(current, dict):
                    return None
                current = current.get(part)
            return current
        return value
    if isinstance(value, list):
        return [_resolve(v, inputs, results) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v, inputs, results) for k, v in value.items()}
    return value


def _safe_result(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_RESULT_CHARS:
        return value[:MAX_RESULT_CHARS] + "…[truncated]"
    try:
        encoded = json.dumps(value, default=str)
    except Exception:
        return str(value)[:MAX_RESULT_CHARS]
    return value if len(encoded) <= MAX_RESULT_CHARS else encoded[:MAX_RESULT_CHARS] + "…[truncated]"


async def _credential_step(row: dict, step: dict, user_id: str) -> dict:
    from app.abilities import vault_store
    cred_id = step.get("credential")
    spec = next((s for s in (row.get("credential_schema") or [])
                 if isinstance(s, dict) and s.get("id") == cred_id), None)
    if not spec:
        raise WorkflowError(f"Unknown credential '{cred_id}'.")
    if spec.get("type") == "oauth":
        return {"status": "needs_oauth", "provider": spec.get("provider") or cred_id,
                "message": "Connect this provider from the agent's Abilities tab, then run the ability again."}
    key_id = spec.get("key_id") or f"soft_{row['id']}_{cred_id}"
    meta = await vault_store.reserve_key(
        user_id, name=spec.get("name") or spec.get("label") or cred_id,
        binding={"base_url": spec.get("service_url") or "", "attach": spec.get("attach") or "bearer"},
        fields=spec.get("fields"), key_id=key_id,
    )
    if not meta:
        raise WorkflowError("Could not reserve the requested vault credential.")
    if not meta.get("filled"):
        return {"status": "needs_credential", "ui": "vault_credential_form",
                "key_id": meta["key_id"], "name": meta.get("name"),
                "fields": meta.get("fields") or [], "service": meta.get("service") or "",
                "filled": False,
                "message": "The user must fill the secure vault card, then run the ability again."}
    return {"status": "ready", "key_id": meta["key_id"], "filled": True}


async def execute_soft_ability(*, ability_id: str, inputs: Dict[str, Any] | None,
                               user_id: str, agent_id: str, session_id: str = "",
                               _db: Any = None,
                               _load_tools: Optional[Callable[..., Any]] = None) -> str:
    if _db is None:
        from app.db import get_db
        _db = get_db()
    if _load_tools is None:
        from app.tools.loader import load_tools
        _load_tools = load_tools
    started = time.monotonic()
    db = _db
    rows = await db.get_agent_soft_abilities(agent_id, enabled_only=True)
    row = next((r for r in rows if r.get("id") == ability_id or r.get("slug") == ability_id), None)
    if not row:
        return json.dumps({"status": "error", "message": "Enabled custom ability not found."})
    steps = (row.get("workflow") or {}).get("steps") or []
    if not isinstance(steps, list) or len(steps) > MAX_STEPS:
        return json.dumps({"status": "error", "message": f"Workflow exceeds the {MAX_STEPS}-step limit."})

    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return json.dumps({"status": "error", "message": "Agent not found."})
    disabled = agent.get("allowed_tools") or []
    if isinstance(disabled, str):
        try:
            disabled = json.loads(disabled)
        except Exception:
            disabled = []
    tools = await _load_tools(user_id, agent_id=agent_id, agent_template_id=agent.get("template_id"),
                              allowed_tools=disabled, session_id=session_id, gate_caller_access=True)
    tools.pop("run_soft_ability", None)
    allowed = set(row.get("allowed_tools") or [])
    runnable = {name: info for name, info in tools.items() if name in allowed}
    results: Dict[str, Any] = {}
    supplied = inputs if isinstance(inputs, dict) else {}
    executed, final, status = [], None, "ok"

    try:
        for index, step in enumerate(steps):
            if time.monotonic() - started > MAX_SECONDS:
                raise WorkflowError(f"Workflow exceeded {MAX_SECONDS:g} seconds.")
            if not isinstance(step, dict):
                raise WorkflowError(f"Step {index + 1} is not an object.")
            action = step.get("action")
            step_id = str(step.get("id") or f"step_{index + 1}")
            if action == "approval.require":
                value = {"approved": True, "reason": step.get("reason") or "Workflow checkpoint"}
            elif action == "credential.ensure":
                value = await _credential_step(row, step, user_id)
                if value.get("status") in {"needs_credential", "needs_oauth"}:
                    results[step_id], status, final = value, value["status"], value
                    break
            elif action == "tool.call":
                name = step.get("tool")
                if name not in allowed:
                    raise WorkflowError(f"Tool '{name}' is outside this ability's allowlist.")
                info = runnable.get(name)
                if not info:
                    raise WorkflowError(f"Tool '{name}' is not enabled for this agent and caller.")
                args = _resolve(step.get("args") or {}, supplied, results)
                if not isinstance(args, dict):
                    raise WorkflowError(f"Arguments for '{name}' must be an object.")
                value = info.handler(**args)
                if inspect.isawaitable(value):
                    remaining = max(0.1, MAX_SECONDS - (time.monotonic() - started))
                    value = await asyncio.wait_for(value, timeout=remaining)
                value = _safe_result(value)
                executed.append(name)
            elif action == "return":
                value = final = _resolve(step.get("value"), supplied, results)
                results[step_id] = value
                break
            else:
                raise WorkflowError(f"Unsupported workflow action '{action}'.")
            results[step_id] = value
            final = value
    except (WorkflowError, asyncio.TimeoutError) as exc:
        status, final = "error", {"message": str(exc) or "Workflow timed out."}
    except Exception as exc:
        status, final = "error", {"message": f"Workflow tool failed: {type(exc).__name__}: {exc}"}

    elapsed_ms = int((time.monotonic() - started) * 1000)
    try:
        await db.record_soft_ability_run(ability_id=row["id"], agent_id=agent_id,
            user_id=user_id, session_id=session_id, ability_version=row.get("version", 1),
            status=status, tools=executed, elapsed_ms=elapsed_ms)
    except Exception:
        pass
    payload = {"status": status, "ability_id": row["id"],
        "ability": row.get("display_name"), "version": row.get("version", 1),
        "result": _safe_result(final), "steps": results,
        "tools_executed": executed, "elapsed_ms": elapsed_ms}
    # Chat's secure-card renderer consumes these fields at the top level.
    if status in {"needs_credential", "needs_oauth"} and isinstance(final, dict):
        payload.update(final)
        payload["result"] = final
    return json.dumps(payload, default=str)

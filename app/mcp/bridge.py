"""Tool resolution and execution glue for the MCP bridge.

resolve_tools_for_engine() loads every tool the app's registry resolves for this
agent — the exact same set a native agent gets — and returns {name: (handler, ToolInfo)}.
execute_mcp_tool() runs a single call through the handler and returns an MCP
content block — with the same ToolExecutionContext the native loop wraps.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Callable, Dict, Optional, Tuple

from app.tools.loader import load_tools
from app.tools.execution_context import ToolExecutionContext, tool_execution_scope

logger = logging.getLogger(__name__)

# Re-export for server.py
ToolPer = Tuple[Callable, Dict[str, Any]]  # (handler, ToolInfo-as-dict)
ToolMap = Dict[str, ToolPer]


async def resolve_tools_for_engine(
    user_id: str,
    agent_id: str,
    session_id: str,
    execution_mode: str,
    allowed_tools: Optional[list] = None,
    agent_template_id: Optional[str] = None,
) -> ToolMap:
    """Load every tool available to this agent, returning name → (handler, info).

    Calls the same ``load_tools()`` the native agent loop uses, so ability-
    gated tools, integration tools, ability tools, and custom DB tools
    are all included — with no per-engine wiring.

    Returns an empty dict when loading fails (the MCP server will report zero
    tools rather than crashing).
    """
    try:
        raw = await load_tools(
            user_id=user_id,
            agent_id=agent_id,
            agent_template_id=agent_template_id,
            allowed_tools=allowed_tools,
            session_id=session_id,
            gate_caller_access=True,
        )
    except Exception as exc:
        logger.warning("MCP tool load failed for agent %s: %s", agent_id, exc)
        return {}

    try:
        from app.db import get_db
        from app.agent.contracts import parse_contract
        db = get_db()
        agent = await db.get_agent_by_id(agent_id) if agent_id else None
        if parse_contract(agent, mode=execution_mode).get("require_file_checkout") and await db.get_session_file_checkout_enabled(session_id):
            from plugins.admin.source_tools import inject_source_tools
            required = {}
            inject_source_tools(required, user_id)
            for required_name in (
                "checkout_files", "checkin_files",
                "abandon_file_checkouts", "file_checkout_status",
            ):
                if required_name in required:
                    raw[required_name] = required[required_name]
    except Exception as exc:
        logger.warning("MCP required checkout tool injection failed: %s", exc)

    out: ToolMap = {}
    for name, ti in raw.items():
        out[name] = (
            ti.handler,
            {
                "name": name,
                "parameters": ti.parameters,
                "destructive": bool(ti.destructive),
                "requires_confirmation": bool(ti.requires_confirmation),
                "tool_id": ti.tool_id,
            },
        )
    return out


async def execute_mcp_tool(
    name: str,
    arguments: dict,
    tools: ToolMap,
    user_id: str,
    session_id: str,
    execution_mode: str = "auto",
    expected_turn_id: Optional[str] = None,
    tool_call_id: Optional[str] = None,
    db: Any = None,
) -> dict:
    """Execute a single tool call and return an MCP content block.

    Returns:
        ``{"content": [{"type": "text", "text": "..."}], "isError": false}``
    """
    entry = tools.get(name)
    if entry is None:
        return _error(f"Tool '{name}' is not available to this agent.")

    checkout_tools = {
        "checkout_files", "checkin_files",
        "abandon_file_checkouts", "file_checkout_status",
    }
    # Legacy bridge callers intentionally execute without opening the database.
    # Checkout operations and run-fenced calls always need an authoritative
    # session record, while a supplied backend opts every call into ownership
    # enforcement.
    if db is None and (expected_turn_id or name in checkout_tools):
        from app.db import get_db
        db = get_db()
    assert_owned = getattr(db, "assert_session_owned", None) if db is not None else None
    if assert_owned is not None:
        try:
            await assert_owned(user_id, session_id)
        except PermissionError as exc:
            return _error(str(exc))

    handler, info = entry

    # ── Permission gate (mirrors the native loop's guardrail) ──
    if execution_mode == "plan" and info.get("destructive"):
        return _error(
            f"Tool '{name}' is blocked in Plan mode (it writes or changes state). "
            "Switch to a write-capable mode such as Auto to use it."
        )
    if execution_mode == "ask" and info.get("destructive"):
        return _error(
            f"Tool '{name}' is blocked in Ask mode (it writes or changes state). "
            "Ask mode ends with a proposal; switch to a write-capable mode such as Auto to execute it."
        )

    start = time.time()
    call_id = tool_call_id or "mcp:" + uuid.uuid4().hex
    tracked = False
    mutation = bool(info.get("destructive") or info.get("requires_confirmation"))
    turn_key = ""
    if expected_turn_id:
        from app.agent import task_state
        from app.agent.run_fence import side_effects_allowed
        from app.agent.turn_reservations import stable_key
        if db is None:
            from app.db import get_db
            db = get_db()
        try:
            state = task_state.current_state(db, session_id, expected_turn_id)
            if not state or not await side_effects_allowed(
                db, session_id, expected_turn_id=expected_turn_id,
            ):
                return _error("MCP task belongs to a stopped or replaced run.")
            turn_key = stable_key("chat-turn", user_id, session_id, expected_turn_id)
            tracked = name not in {"update_task_checkpoint", "get_task_checkpoint"}
            if tracked and mutation:
                # Task Progress is projected asynchronously.  The run fence and
                # normal permission checks remain the authority for this call.
                task_state.record_operation(db, session_id, expected_turn_id, call_id,
                                            "mutation", finished=False)
        except Exception as exc:
            logger.warning("MCP task fence unavailable: %s", exc)
            return _error("MCP task checkpoint is unavailable; tool was not executed.")
    try:
        from app.auth.workspace import current_workspace
        workspace = current_workspace()
        require_checkout = False
        write_capable = execution_mode not in {"ask", "plan", "read"}
        try:
            if db is None:
                raise LookupError("legacy MCP call has no session backend")
            agent_id = await db.get_session_agent_id(session_id)
            agent = await db.get_agent_by_id(agent_id) if agent_id else None
            from app.agent.contracts import parse_contract
            require_checkout = bool(
                parse_contract(agent, mode=execution_mode).get("require_file_checkout")
            )
            if require_checkout and not await db.get_session_file_checkout_enabled(session_id):
                require_checkout = False
        except Exception:
            agent_id, require_checkout = None, False
        ctx = ToolExecutionContext(
            user_id=user_id,
            session_id=session_id,
            turn_key=turn_key,
            tool_name=name,
            tool_call_id=call_id,
            authority_mode="server",
            idempotency_key=None,
            side_effecting=mutation,
            actor_user_id=(workspace.actor_user_id if workspace else user_id),
            workspace_kind=(workspace.subject_kind if workspace else "user"),
            workspace_id=(workspace.subject_id if workspace else user_id),
            agent_id=agent_id,
            execution_mode=execution_mode,
            require_file_checkout=require_checkout,
            write_capable_mode=write_capable,
        )
        from app.agent.session_changes import capture_tool_state_async, record_tool_delta
        before_changes = await capture_tool_state_async(name, arguments)
        with tool_execution_scope(ctx):
            result = await handler(**arguments)
        after_changes = await capture_tool_state_async(name, arguments)
        changed_paths = await record_tool_delta(
            session_id, before_changes, after_changes
        )
        duration_ms = int((time.time() - start) * 1000)
        text = _serialise(result)
        if tracked:
            from app.agent.verification_evidence import (
                build_verification_event, result_observation_success,
            )
            observed = {"success": True, "content": text, "duration_ms": duration_ms}
            if mutation:
                task_state.record_operation(
                    db, session_id, expected_turn_id, call_id, "mutation",
                    finished=True, success=result_observation_success(observed),
                )
            task_state.record_evidence(
                db, session_id, expected_turn_id,
                build_verification_event(tool_name=name, tool_args=arguments,
                                         result=observed, turn=0),
                tool_call_id=call_id,
            )
        return {
            "content": [{"type": "text", "text": text}],
            "isError": False,
            "duration_ms": duration_ms,
            "changed_paths": changed_paths,
        }
    except Exception as exc:
        duration_ms = int((time.time() - start) * 1000)
        if tracked:
            try:
                if mutation:
                    task_state.record_operation(
                        db, session_id, expected_turn_id, call_id, "mutation",
                        finished=True, success=False,
                    )
                from app.agent.verification_evidence import build_verification_event
                task_state.record_evidence(
                    db, session_id, expected_turn_id,
                    build_verification_event(
                        tool_name=name, tool_args=arguments,
                        result={"success": False, "content": "", "duration_ms": duration_ms},
                        turn=0, error_type=type(exc).__name__,
                    ), tool_call_id=call_id,
                )
            except Exception:
                # A replaced run must never write evidence into its successor.
                logger.warning("MCP task outcome could not be checkpointed")
        logger.warning("MCP tool %s failed: %s", name, exc)
        return _error(f"Tool '{name}' failed: {exc}", duration_ms=duration_ms)


# ── helpers ────────────────────────────────────────────────────────────────────

def _serialise(value: Any) -> str:
    """Return a string the CLI's model can read — JSON for dicts/lists, str()
    for primitives."""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(value)
    return str(value)


def _error(message: str, duration_ms: int = 0) -> dict:
    return {
        "content": [{"type": "text", "text": message}],
        "isError": True,
        "duration_ms": duration_ms,
    }

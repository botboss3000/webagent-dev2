"""
Tool execution tracker — structured per-call metrics (duration, success,
interaction_id) written to the dedicated, always-local logs database
(``tool_executions`` table in ``logs.db``; see app/db/logs_store.py).

Two ways tool executions reach that table:

  1. **Automatically** — the diagnostics loop tap
     (``app.agent.diagnostics.tap_loop_event``) pairs every ``tool_call`` with
     its ``tool_result``, times it, and queues a row. No call site needed.
  2. **Explicitly** — call :func:`track_execution` from anywhere you run a tool
     outside the visualizer event stream and want a metric row.

This module used to point at a non-existent ``tool_executions`` table via the
the raw client and crashed on import; it now delegates to the log store and
never raises.
"""

import json
import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


async def track_execution(
    tool_name: str,
    user_id: Optional[str] = None,
    session_id: Optional[str] = None,
    success: bool = True,
    duration_ms: Optional[int] = None,
    interaction_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    agent_id: Optional[str] = None,
    error_message: Optional[str] = None,
    error_type: Optional[str] = None,
    input_params: Optional[Dict[str, Any]] = None,
    output_result: Optional[str] = None,
) -> None:
    """Record one tool execution to logs.db. Inputs/outputs are truncated. Never
    raises — a failed metric write must not break the tool call it describes."""
    try:
        params_str: Optional[str] = None
        if input_params is not None:
            params_str = json.dumps(input_params, default=str)
            if len(params_str) > 1000:
                params_str = params_str[:1000] + " …[truncated]"
        out_str = output_result
        if out_str and len(out_str) > 1000:
            out_str = out_str[:1000] + " …[truncated]"

        from app.db.logs_store import get_log_store
        await get_log_store().insert_tool_executions([{
            "tool_name": tool_name,
            "user_id": user_id,
            "session_id": session_id,
            "turn_id": turn_id,
            "interaction_id": interaction_id,
            "agent_id": agent_id,
            "success": bool(success),
            "duration_ms": duration_ms,
            "error_type": error_type,
            "error_message": error_message,
            "input_params": params_str,
            "output_preview": out_str,
        }])
    except Exception as e:
        logger.debug("track_execution failed for %s: %s", tool_name, e)

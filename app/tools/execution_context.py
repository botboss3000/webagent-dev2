"""Request-local authority/idempotency context available to every tool."""

from __future__ import annotations

import contextvars
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator, Optional


@dataclass(frozen=True)
class ToolExecutionContext:
    user_id: str
    session_id: str
    turn_key: str
    tool_name: str
    tool_call_id: str
    authority_mode: str
    idempotency_key: Optional[str]
    side_effecting: bool
    actor_user_id: Optional[str] = None
    workspace_kind: str = "user"
    workspace_id: Optional[str] = None
    agent_id: Optional[str] = None
    execution_mode: str = ""
    require_file_checkout: bool = False
    write_capable_mode: bool = False


_current: contextvars.ContextVar[Optional[ToolExecutionContext]] = contextvars.ContextVar(
    "webagent_tool_execution_context", default=None
)


def current_tool_context() -> Optional[ToolExecutionContext]:
    return _current.get()


@contextmanager
def tool_execution_scope(context: ToolExecutionContext) -> Iterator[ToolExecutionContext]:
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)

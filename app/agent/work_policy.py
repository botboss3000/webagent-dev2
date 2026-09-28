"""Durable loop state used for closing and progress decisions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Optional


class WorkState(str, Enum):
    CONVERSATION = "conversation"
    WORK_PENDING = "work_pending"
    WORK_SATISFIED = "work_satisfied"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class WorkTransition:
    previous: WorkState
    current: WorkState
    reason: str


BOOKKEEPING_TOOLS = frozenset(
    {
        "update_working_memory",
        "get_working_memory",
        "update_checkpoint",
        "get_checkpoint",
        "update_task_checkpoint",
        "get_task_checkpoint",
        "present_plan",
        "set_agent_mode",
        "set_execution_mode",
        "set_reasoning_effort",
        "load_skill",
    }
)
BLOCKING_ERROR_TYPES = frozenset(
    {
        "permission_denied",
        "authorization_required",
        "credential_required",
        "tool_unavailable",
        "capability_unavailable",
    }
)


def is_meaningful_tool(tool_name: str) -> bool:
    return bool(tool_name) and tool_name not in BOOKKEEPING_TOOLS


@dataclass
class WorkPolicy:
    state: WorkState
    transition_reason: str
    protocol_retry_used: bool = False
    transitions: list[WorkTransition] = field(default_factory=list)

    @classmethod
    def initial(
        cls,
        messages: Iterable[Mapping[str, Any]],
        *,
        requires_plan: bool = False,
        task_pending: bool = False,
        resume_reason: Optional[str] = None,
        has_tools: bool = True,
    ) -> "WorkPolicy":
        reason = "conversation"
        state = WorkState.CONVERSATION
        normalized_resume = str(resume_reason or "").strip().lower()
        if requires_plan:
            state, reason = WorkState.WORK_PENDING, "plan_required"
        elif task_pending:
            state, reason = WorkState.WORK_PENDING, "task_progress_gap"
        elif normalized_resume in {"work_not_started", "incomplete_work", "incomplete"}:
            state, reason = WorkState.WORK_PENDING, f"resume:{normalized_resume}"
        if state is WorkState.WORK_PENDING and not has_tools:
            state, reason = WorkState.BLOCKED, "no_tools_available"
        return cls(state=state, transition_reason=reason)

    @property
    def requires_tool(self) -> bool:
        return self.state is WorkState.WORK_PENDING

    def _transition(self, state: WorkState, reason: str) -> None:
        previous = self.state
        self.state = state
        self.transition_reason = reason
        self.transitions.append(WorkTransition(previous, state, reason))

    def record_tool(
        self,
        tool_name: str,
        *,
        succeeded: bool,
        task_pending: bool = False,
        error_type: Optional[str] = None,
        pending_reason: Optional[str] = None,
    ) -> None:
        if succeeded and is_meaningful_tool(tool_name):
            if task_pending:
                self._transition(WorkState.WORK_PENDING, pending_reason or "task_progress_gap")
            else:
                self._transition(WorkState.WORK_SATISFIED, f"meaningful_tool:{tool_name}")
            return
        if not succeeded and str(error_type or "").lower() in BLOCKING_ERROR_TYPES:
            self._transition(WorkState.BLOCKED, f"tool_blocked:{error_type}")

    def record_checkpoint(self, *, task_pending: bool, blocked: bool = False) -> None:
        if blocked:
            self._transition(WorkState.BLOCKED, "durable_task_blocker")
        elif task_pending:
            self._transition(WorkState.WORK_PENDING, "task_progress_gap")
        elif self.state is WorkState.WORK_PENDING:
            self._transition(WorkState.WORK_SATISFIED, "durable_contract_satisfied")

    def record_plan_requirement(self, *, required: bool) -> None:
        """Track a live mode switch without satisfying unrelated user work."""

        if required:
            if self.state is not WorkState.BLOCKED:
                self._transition(WorkState.WORK_PENDING, "plan_required")
        elif self.state is WorkState.WORK_PENDING and self.transition_reason == "plan_required":
            self._transition(WorkState.WORK_SATISFIED, "plan_obligation_released")

    def record_protocol_violation(self) -> bool:
        if self.protocol_retry_used:
            return False
        self.protocol_retry_used = True
        return True

    def diagnostic(
        self,
        *,
        transport: str,
        requested_tool_choice: object,
        outcome: str,
    ) -> dict[str, object]:
        return {
            "state": self.state.value,
            "transition_reason": self.transition_reason,
            "transport": transport,
            "requested_tool_choice": requested_tool_choice,
            "outcome": outcome,
        }

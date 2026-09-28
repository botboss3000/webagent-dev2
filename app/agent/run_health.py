"""Deterministic run-health signals shared by the loop and Manager watchdog.

This module does not decide whether an agent's approach is semantically sound.
It converts cheap, observable facts (tool patterns and outcomes) into compact
events that the supervisory Manager can judge without receiving raw tool output.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Deque, Dict, Optional


def health_event(
    reason: str,
    *,
    turn: int,
    tool: str = "",
    severity: int = 1,
    **evidence: Any,
) -> Dict[str, Any]:
    return {
        "trigger": "run_health",
        "reason": reason,
        "severity": max(1, min(3, int(severity))),
        "turn": max(0, int(turn)),
        "tool": tool or None,
        "evidence": evidence,
    }


@dataclass
class RunHealthTracker:
    """Short-lived health state for one active agent run."""

    error_window: int = 8
    recent_tools: Deque[str] = field(default_factory=lambda: deque(maxlen=6))
    recent_signatures: Deque[str] = field(default_factory=lambda: deque(maxlen=6))
    recent_outcomes: Deque[bool] = field(init=False)
    consecutive_failures: int = 0
    total_failures: int = 0
    last_error_alert_total: int = 0
    last_pattern: str = ""
    last_watchdog_turn: int = -1_000_000

    def __post_init__(self) -> None:
        self.error_window = max(1, int(self.error_window or 8))
        self.recent_outcomes = deque(maxlen=self.error_window)

    def record_request(
        self, tool: str, *, turn: int, signature: str = "",
    ) -> Optional[Dict[str, Any]]:
        """Detect an exact two-call oscillation such as A/B/A/B/A/B.

        Tool names alone are intentionally insufficient: alternating different
        files through read/search is legitimate exploration. The canonical
        signatures must repeat, which identifies a true two-step loop.
        """
        self.recent_tools.append(str(tool or ""))
        self.recent_signatures.append(str(signature or tool or ""))
        if len(self.recent_signatures) < 6:
            return None
        names = list(self.recent_tools)
        signatures = list(self.recent_signatures)
        alternating = (
            signatures[0] == signatures[2] == signatures[4]
            and signatures[1] == signatures[3] == signatures[5]
            and signatures[0] != signatures[1]
        )
        pattern = "<->".join(sorted((names[0], names[1]))) if alternating else ""
        if not alternating:
            self.last_pattern = ""
            return None
        if pattern == self.last_pattern:
            return None
        self.last_pattern = pattern
        return health_event(
            "alternating_tool_loop", turn=turn, tool=tool, severity=2,
            pattern=pattern, recent_tools=names,
            repeated_signatures=2,
        )

    def record_outcome(
        self,
        success: bool,
        *,
        turn: int,
        tool: str,
        error_type: str = "",
        threshold: int = 0,
    ) -> Optional[Dict[str, Any]]:
        success = bool(success)
        self.recent_outcomes.append(success)
        if success:
            self.consecutive_failures = 0
        else:
            self.consecutive_failures += 1
            self.total_failures += 1
        threshold = max(0, int(threshold or 0))
        failures_in_window = sum(1 for outcome in self.recent_outcomes if not outcome)
        if (
            threshold <= 0
            or failures_in_window < threshold
            or self.total_failures <= self.last_error_alert_total
        ):
            return None
        self.last_error_alert_total = self.total_failures
        return health_event(
            "tool_error_cluster", turn=turn, tool=tool, severity=2,
            error_type=error_type or "unknown",
            consecutive_failures=self.consecutive_failures,
            failures_in_window=failures_in_window,
            window_size=self.error_window,
        )

    def allow_watchdog(self, *, turn: int, cooldown_turns: int) -> bool:
        cooldown = max(0, int(cooldown_turns or 0))
        if int(turn) - self.last_watchdog_turn < cooldown:
            return False
        self.last_watchdog_turn = int(turn)
        return True

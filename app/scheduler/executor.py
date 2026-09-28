"""Execute one scheduled automation row.

Thin entry point for the scheduler poll loop — delegates to the shared run
engine (``app.automation.runner.run_one``), which creates the session, runs
the agent loop, dispatches the result via the configured channel (or records
silently), and updates the automation row's run bookkeeping
(``last_run_at`` / ``last_status`` / ``next_run_at``).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

logger = logging.getLogger(__name__)


async def execute_automation(automation: Dict[str, Any]) -> Dict[str, Any]:
    """Run one scheduled automation row end-to-end.

    Thin wrapper over the shared run engine (``app.automation.runner.run_one``),
    which owns run-mode (inline / clone), unified delivery, guardrails, per-run
    history, retries/auto-disable, and one-shot (``schedule_kind='once'``) timers.
    Kept as the scheduler's entry point so the poll loop is unchanged.
    """
    from app.db import get_db
    from app.automation.runner import run_one

    db = get_db()
    kind = "timer" if (automation.get("schedule_kind") == "once") else "schedule"
    return await run_one(db, automation, kind=kind)

"""
Opt-in auto-investigation of involuntary run failures.

When a supervised run ends in an *involuntary* failure (status ``error``, or a
``crash`` / ``zombie`` / ``frozen`` stop-cause) AND the operator has turned on
``diagnostics_auto_investigate`` in app-settings.json, hand the failure to the
**Diagnostic Agent**: spin a fresh background session owned by the failing
(admin) user, bound to their ``diagnostic-agent`` instance, seeded with a
failure brief. The agent reads the flight-recorder (``read_diagnostics``) +
source and reports a likely cause into that session — visible later in the
session list.

OFF by default. Guarded against:
  • recursion — a diagnostic run (origin ``diagnostics`` / ``diag-`` session /
    diagnostic-agent) is never itself investigated;
  • storms — at most N investigations per rolling hour (in-memory counter);
  • voluntary stops — user_stop / replaced / clean complete / interrupt;
  • non-admins — only admin-owned failures are investigated (the diagnostic
    agent is admin-only and can read source).
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import time
import uuid
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DIAGNOSTIC_TEMPLATE_ID = "diagnostic-agent"
_INVOLUNTARY_CAUSES = {"crash", "zombie", "frozen"}
_VOLUNTARY_CAUSES = {"user_stop", "replaced", "needs_manual_resume", "complete"}
SETTINGS_FILE = "app-settings.json"

_recent: "collections.deque[float]" = collections.deque(maxlen=128)
_lock = asyncio.Lock()


def _settings() -> Dict[str, Any]:
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception:
        pass
    return {}


async def maybe_auto_investigate(db, session_id: str, outcome: Any, run_row: Optional[Dict[str, Any]]) -> None:
    """Decide whether to launch a Diagnostic Agent run for a failed turn.

    Cheap + fully guarded; never raises into the finishing run.
    """
    try:
        s = _settings()
        if not bool(s.get("diagnostics_auto_investigate", False)):
            return

        status = getattr(outcome, "status", None)
        cause = getattr(outcome, "stop_cause", None)
        involuntary = (status == "error") or (cause in _INVOLUNTARY_CAUSES)
        if not involuntary or cause in _VOLUNTARY_CAUSES:
            return

        row = run_row or {}
        user_id = row.get("user_id")
        origin = row.get("origin")
        agent_id = row.get("agent_id")
        if not user_id:
            return

        # ── Recursion guards ──
        if origin == "diagnostics" or str(session_id).startswith("diag-"):
            return
        if agent_id:
            try:
                a = await db.get_agent_by_id(agent_id)
                if a and a.get("template_id") == DIAGNOSTIC_TEMPLATE_ID:
                    return
            except Exception:
                pass

        # Only investigate admin-owned failures (diagnostic agent is admin-only).
        try:
            if not await db.is_user_admin(user_id):
                return
        except Exception:
            return

        # ── Storm guard (rolling hour) ──
        max_per_hour = int(s.get("diagnostics_auto_investigate_max_per_hour", 3) or 3)
        async with _lock:
            now = time.time()
            while _recent and now - _recent[0] > 3600:
                _recent.popleft()
            if len(_recent) >= max_per_hour:
                _note("warning", "auto-investigate skipped: hourly rate limit reached",
                      session_id=session_id, user_id=user_id)
                return
            _recent.append(now)

        # Launch in the background — never block the run that just finished.
        asyncio.create_task(_run_investigation(
            db, session_id, user_id, status, cause, getattr(outcome, "error", None)))
    except Exception as e:
        logger.debug("maybe_auto_investigate error: %s", e)


async def _run_investigation(db, failed_session_id, user_id, status, cause, error) -> None:
    try:
        from app.agent.loop import run_agent_loop_buffered
        from app.agent.prompts import build_system_prompt
        from app.agent.runner import run_supervised_turn, RunOutcome

        agent = await db.resolve_agent(user_id, DIAGNOSTIC_TEMPLATE_ID)
        if not agent:
            _note("warning", "auto-investigate skipped: no diagnostic agent available",
                  session_id=failed_session_id, user_id=user_id)
            return
        agent_id = agent["id"]

        # Fresh session bound to the diagnostic agent.
        session_id = f"diag-{uuid.uuid4().hex[:12]}"
        title = (f"Auto-diagnosis: {str(failed_session_id)[:12]}")[:60]
        try:
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                    (session_id, user_id, title, agent_id, json.dumps({"source": "diagnostics"})),
                )
                conn.commit()
            finally:
                conn.close()
        except Exception as e:
            logger.warning("auto-investigate: session insert failed: %s", e)
            return

        # Load investigator brief from app-prompts.json, with inline fallback
        import json as _jmod
        from app.util.paths import app_prompts_path
        _prompt_path = app_prompts_path()
        _brief_tpl = ""
        try:
            _data = _jmod.loads(_prompt_path.read_text(encoding="utf-8"))
            _entry = _data.get("app_level_prompts", {}).get("diagnostic_investigator_brief", {})
            _brief_tpl = _entry.get("template") or _entry.get("text", "")
        except Exception:
            pass

        if _brief_tpl:
            try:
                brief = _brief_tpl.format(
                    failed_session_id=failed_session_id,
                    status=status,
                    cause=cause,
                    error=error or "(none recorded)",
                )
            except (KeyError, ValueError):
                brief = _brief_tpl
        else:
            brief = (
                "A run just failed and needs investigation.\n\n"
                f"- Failed session: {failed_session_id}\n"
                f"- Status: {status}\n"
                f"- Stop cause: {cause}\n"
                f"- Error: {error or '(none recorded)'}\n\n"
                "Use read_diagnostics to pull the recent errors / loop events for this session "
                "and the server, identify the most likely root cause, and report it concisely "
                "with evidence (file:line where a traceback points). Stay read-only — do not "
                "change any files unless explicitly asked."
            )

        resolved_slots = await db.resolve_prompts(agent_id, user_id=user_id)
        context_docs = [
            {"id": s_["slot_name"], "context_type": s_["slot_name"], "title": s_["slot_name"],
             "content": s_["content"], "tags": []}
            for s_ in resolved_slots
            if (s_.get("content") or "").strip() and s_.get("slot_name") != "automation"
        ]
        system_prompt = await build_system_prompt(
            context_docs, brain_context=None, user_id=user_id, agent_id=agent_id,
            session_id=session_id)

        turn_uid = None
        try:
            turn_uid = await db.insert_interaction(
                user_id, session_id, role="user", content=brief, channel="diagnostics",
                metadata=json.dumps({"source": "diagnostics", "failed_session_id": failed_session_id}),
                sender_id=user_id, receiver_id=agent_id, source="diagnostics",
            )
        except Exception as e:
            logger.debug("auto-investigate: brief insert failed: %s", e)

        raw_allowed = agent.get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        relaunch_ctx = {
            "origin": "diagnostics", "session_id": session_id, "user_id": user_id,
            "agent_id": agent_id, "channel": "diagnostics", "timeout_seconds": 300,
        }

        async def _build(replaced: bool) -> RunOutcome:
            reply = await run_agent_loop_buffered(
                user_id=user_id, session_id=session_id, user_message=brief,
                system_prompt=system_prompt, agent_id=agent_id, history=None,
                channel="diagnostics", timeout_seconds=300, db=db,
                agent_template_id=agent.get("template_id"),
                allowed_tools=raw_allowed or None, max_turns=agent.get("max_turn_count", 0),
            )
            if not (reply or "").strip():
                return RunOutcome(status="error", stop_cause="empty_response",
                                  error="agent loop produced no final reply")
            return RunOutcome(status="complete", stop_cause="complete", reply=reply)

        _note("info", "auto-investigation launched", session_id=session_id,
              agent_id=agent_id, user_id=user_id, detail={"failed_session_id": failed_session_id})

        await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=agent_id,
            origin="diagnostics", channel="diagnostics", turn_id=turn_uid,
            relaunch_ctx=relaunch_ctx, build_turn=_build, await_result=False,
        )
    except Exception as e:
        logger.warning("auto-investigation failed: %s", e)


def _note(level: str, message: str, **kw) -> None:
    try:
        from app.agent.diagnostics import record as diag_record
        diag_record(level, "run", message, source="auto_investigate", persist=True, **kw)
    except Exception:
        pass

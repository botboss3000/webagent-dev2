"""Internal ``session`` event source — one event when an agent turn SETTLES.

A *turn* is one user message and the agent run it starts. While that run is in
flight ``session_runs.status`` is ``'running'``; when it stops it becomes
``'complete'`` / ``'interrupted'`` / ``'error'``. This source watches that one
column, so a long run that executes a hundred tool calls emits exactly ONE
event when it settles — never one per step, and never while it is working.

Payload (every key is usable as a subscription filter, because the default
``matches_filter`` compares payload keys directly):

    session_id, session_title, agent_id, status, stop_cause, error, outcome,
    is_incomplete, resume_attempts, started_at, ended_at, last_user_message,
    last_agent_message

``outcome`` is the friendly classification:
    completed  — finished cleanly
    incomplete — stopped involuntarily (crash/frozen/zombie/restart/…) or errored
    stopped    — a voluntary stop (user_stop / replaced)

Runs belonging to the *subscribing* agent are skipped, so a watcher that runs on
these events can never trigger itself.

DROP-IN — drop this one file in ``app/events/sources/`` and the manager discovers
``source_cls`` + ``FEATURE`` automatically. Delete the file to remove it.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration

# Stop causes meaning "stopped involuntarily and is probably not finished".
# Mirrors app/agent/runner.py::_RESUMABLE_CAUSES (kept as a literal so this
# drop-in doesn't import the runner just to read a tuple), plus ``failed``.
_INCOMPLETE_CAUSES = frozenset({
    "server_restart", "zombie", "frozen", "crash", "empty_response",
    "contract_blocked", "work_not_started", "audit_incomplete", "failed",
    "needs_manual_resume",
})

_MAX_EVENTS_PER_POLL = 20   # burst guard; any extra settles arrive next poll
_PEEK_CHARS = 400


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean(text: Any, limit: int = _PEEK_CHARS) -> str:
    """Collapse whitespace and truncate — keeps payloads prompt-sized."""
    return " ".join(str(text or "").split())[:limit]


class SessionSource(EventSource):
    """Fires when a session's agent turn settles (leaves ``running``)."""

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return "session"

    @property
    def event_types(self) -> List[str]:
        return ["session_settled"]

    @property
    def supports_push(self) -> bool:
        return False

    @property
    def required_provider(self) -> Optional[str]:
        # Internal source — no OAuth connection needed.
        return None

    @property
    def default_poll_interval_seconds(self) -> int:
        return 60

    @property
    def description(self) -> str:
        return ("Session events: fires once when one of your sessions settles "
                "(complete, interrupted or error) — never while it is running.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["outcome", "is_incomplete", "agent_id", "status"]
        base["examples"] = [
            {"event_type": "session_settled"},
            {"event_type": "session_settled", "filter": {"outcome": "incomplete"}},
        ]
        return base

    # ── Subscription lifecycle ────────────────────────────────────────────

    async def register_subscription(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        event_type: str,
        filter_dict: Dict[str, Any],
    ) -> SubscriptionRegistration:
        # Poll source: nothing to register provider-side. Returning no cursor is
        # deliberate — the first poll adopts "now" as its watermark (see poll),
        # so subscribing never replays historical sessions.
        return SubscriptionRegistration(poll_cursor=None)

    # ── Polling ───────────────────────────────────────────────────────────

    async def poll(
        self,
        subscription_row: Dict[str, Any],
    ) -> Tuple[List[NormalizedEvent], Optional[str]]:
        from app.db import get_db

        owner_user_id = subscription_row.get("owner_user_id") or ""
        subscriber_agent_id = subscription_row.get("agent_id") or ""
        cursor = subscription_row.get("poll_cursor") or ""

        # First poll for a fresh subscription: adopt "now" and emit nothing.
        # Without this, arming the watch would fire on every session in history.
        if not cursor:
            return [], _now_iso()

        # Keyset pagination, not a bare timestamp: several sessions can settle in
        # the same instant, so the cursor carries (updated_at, session_id) and the
        # query walks strictly past that position. A timestamp-only cursor would
        # either skip rows on the LIMIT boundary or spin on the ties forever.
        cur_ts, cur_sid = self._parse_cursor(cursor)
        store = get_db()._user(owner_user_id)
        conn = store._get_conn()
        try:
            rows = conn.execute(
                """SELECT session_id, user_id, agent_id, turn_id, status,
                          stop_cause, error, resume_attempts,
                          started_at, updated_at
                     FROM session_runs
                    WHERE status <> 'running'
                      AND user_id = ?
                      AND (updated_at > ? OR (updated_at = ? AND session_id > ?))
                    ORDER BY updated_at ASC, session_id ASC
                    LIMIT ?""",
                (owner_user_id, cur_ts, cur_ts, cur_sid, _MAX_EVENTS_PER_POLL),
            ).fetchall()
        finally:
            conn.close()

        events: List[NormalizedEvent] = []
        newest_ts, newest_sid = cur_ts, cur_sid
        for row in rows:
            session_id = row["session_id"] or ""
            ended_at = row["updated_at"] or ""
            # Advance the keyset position past every row we consumed — including
            # ones we skip below — so the next poll never re-walks them.
            if (ended_at, session_id) > (newest_ts, newest_sid):
                newest_ts, newest_sid = ended_at, session_id
            if not session_id:
                continue

            run_agent_id = row["agent_id"] or ""
            # Never trigger on the subscribing agent's own turns — otherwise a
            # watcher that runs on session events would re-trigger itself.
            if subscriber_agent_id and run_agent_id == subscriber_agent_id:
                continue

            status = (row["status"] or "").strip() or "complete"
            stop_cause = (row["stop_cause"] or "").strip()
            if status == "complete" and not stop_cause:
                outcome = "completed"
            elif status == "error" or stop_cause in _INCOMPLETE_CAUSES:
                outcome = "incomplete"
            else:
                outcome = "stopped"

            title, last_user, last_agent = self._peek(store, session_id)

            events.append(NormalizedEvent(
                source=self.name,
                event_type="session_settled",
                owner_user_id=owner_user_id,
                # Stable per (session, turn, terminal status): the router's
                # event_deliveries dedup makes a re-emission harmless.
                external_id=f"{session_id}:{row['turn_id'] or ended_at}:{status}",
                occurred_at=ended_at or _now_iso(),
                payload={
                    "session_id": session_id,
                    "session_title": title,
                    "agent_id": run_agent_id,
                    "status": status,
                    "stop_cause": stop_cause,
                    "outcome": outcome,
                    "is_incomplete": outcome == "incomplete",
                    "error": _clean(row["error"], 300),
                    "resume_attempts": int(row["resume_attempts"] or 0),
                    "started_at": row["started_at"] or "",
                    "ended_at": ended_at,
                    "last_user_message": last_user,
                    "last_agent_message": last_agent,
                },
                raw_ref={"session_id": session_id},
            ))

        return events, f"{newest_ts}|{newest_sid}"

    # ── Enrichment ────────────────────────────────────────────────────────

    @staticmethod
    def _parse_cursor(cursor: str) -> Tuple[str, str]:
        """Split ``"<updated_at>|<session_id>"`` into its two parts.

        A cursor written before this pair existed — or the initial "now"
        watermark — carries no ``|``; the session part is then empty, which keeps
        the keyset comparison well-defined.
        """
        ts, _, sid = (cursor or "").partition("|")
        return ts, sid

    @staticmethod
    def _peek(store, session_id: str) -> Tuple[str, str, str]:
        """Best-effort title + last user / last agent message for one session.

        ``store`` is the user-scoped DB handle the poll already resolved.

        Pure enrichment: any failure degrades to empty strings rather than
        dropping the event.
        """
        title = last_user = last_agent = ""
        conn = store._get_conn()
        try:
            try:
                row = conn.execute(
                    "SELECT title FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row:
                    title = _clean(row["title"], 120)
            except Exception:
                pass
            try:
                rows = conn.execute(
                    """SELECT role, content, tool_name FROM interactions
                        WHERE session_id = ? AND role IN ('user', 'assistant')
                        ORDER BY session_seq DESC LIMIT 6""",
                    (session_id,),
                ).fetchall()
                for r in rows:
                    if r["role"] == "user" and not last_user:
                        last_user = _clean(r["content"])
                    elif r["role"] == "assistant" and not last_agent and not r["tool_name"]:
                        last_agent = _clean(r["content"])
            except Exception:
                pass
        finally:
            conn.close()
        return title, last_user, last_agent


FEATURE = {
    "id": "session_events",
    "display_name": "Sessions (settled turns)",
    "category": "event_source",
    "status": "beta",
    "summary": ("Fire the agent when one of your sessions settles — complete, "
                "interrupted or error. Never fires mid-run."),
    "requires": [],
}

source_cls = SessionSource

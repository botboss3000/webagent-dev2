"""
Automations Dashboard — read-only aggregation endpoint.

Unifies four disconnected sources into one view per agent:
  • agent_automations        — scheduled cron tasks
  • agent_event_subscriptions — push/poll event triggers
  • agent_spawns             — active worker spawns (orchestrator helpers)
  • webhook_registrations    — user-level generic inbound webhooks (separate section)

No destructive actions. Follows the same auth pattern as list_event_subscriptions.
Drop-in file — not wired into any core registry.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from app.auth.identity import assert_caller_is, caller_may_access_page
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/automations", tags=["automations"])


async def _require_automations_page_access(request: Request) -> None:
    """Enforce the Agents page's VISIBILITY on the server, not just by hiding its
    header tab. Automations is no longer its own main-panel tab: it is embedded
    inside the Agents page (the All Agents card's Automations tab, see
    ui/main-panel/agents/agents.html #automations-section), so access follows the
    Agents page's gate. Registered users, admins, and 'open' single-user mode
    pass; because every main page defaults to 'auth' (registration required), an
    anonymous guest is refused unless an admin opens the Agents page to 'all'.
    This stops a hand-crafted request from reaching the dashboard or any
    management action when the admin has gated the page off — the navigation
    layer hides the tab, this closes the API behind it. Ownership
    (assert_caller_is / _owns) is still enforced separately per row."""
    if not await caller_may_access_page(request, "main", "agents"):
        raise HTTPException(status_code=403, detail="The Automations page isn't enabled for your account.")


def _safe_str(v: Any, fallback: str = "") -> str:
    return str(v) if v is not None else fallback


def _fmt_time(iso: Optional[str]) -> Optional[str]:
    """Normalise an ISO timestamp to a timezone-aware string the browser can
    parse as UTC.

    We trim sub-seconds for tidiness but MUST keep the offset. The old version
    stripped it (``iso.replace("T"," ")[:19]``), so a stored UTC time like
    ``2026-06-20T11:21:35+00:00`` came back as ``2026-06-20 11:21:35`` with no
    zone — which the dashboard's relative-time formatter (``new Date(...)``) then
    read as *local* time. For any viewer behind UTC that pushed recent runs into
    the future, so an hour-old run wrongly displayed as "Just now". Keeping the
    offset makes "Last" accurate again.
    """
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
        if dt.tzinfo is None:        # legacy naive timestamps were written as UTC
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.replace(microsecond=0).isoformat()
    except Exception:
        return iso


# Display names for known comms channels (fallback = title-cased id).
_CHANNEL_LABELS = {
    "telegram": "Telegram",
    "slack": "Slack",
    "discord": "Discord",
    "twilio_sms": "SMS",
    "sms": "SMS",
    "twilio_whatsapp": "WhatsApp",
    "whatsapp": "WhatsApp",
    "webchat": "This chat",
}


def _summarize_delivery(row: dict) -> List[dict]:
    """Describe, in display terms, WHERE this automation's result is sent.

    Reads the unified delivery spec (``delivery_json``, parsed to ``delivery``),
    falling back to the legacy channel/recipient/silent columns, and maps each
    target to a small ``{kind, label, available}`` descriptor the dashboard
    renders as a badge in its Output column. Email is flagged ``available:False``
    — no outbound mail channel exists yet — so the page shows it honestly rather
    than implying it works. A row that delivers nothing (a worker/clone/inbound
    webhook never passes through here) yields an empty list → the page shows a dash.
    """
    try:
        from app.automation.delivery import parse_spec
        spec = parse_spec(
            row.get("delivery"),
            legacy_channel=row.get("channel"),
            legacy_recipient=row.get("channel_recipient"),
            legacy_silent=bool(row.get("silent")),
        )
    except Exception:
        spec = {}

    out: List[dict] = []
    for t in (spec.get("targets") or []):
        mode = (t.get("mode") or "").strip()
        if mode == "here":
            out.append({"kind": "chat", "label": "This chat"})
        elif mode == "new_session":
            out.append({"kind": "chat", "label": "New chat"})
        elif mode == "headless":
            out.append({"kind": "silent", "label": "Silent"})
        elif mode == "channel":
            ch = (t.get("channel") or "").strip()
            label = _CHANNEL_LABELS.get(ch.lower(), ch.replace("_", " ").title() or "Channel")
            out.append({"kind": "chat" if ch.lower() == "webchat" else "channel",
                        "channel": ch, "label": label})
        elif mode == "webhook":
            out.append({"kind": "webhook", "label": "Webhook"})
        elif mode == "file":
            out.append({"kind": "file", "label": "File"})
        elif mode == "email":
            out.append({"kind": "email", "label": "Email", "available": False})
        elif mode:
            out.append({"kind": "other", "label": mode})
    # A spec marked silent with no explicit headless target still means "do it
    # quietly" — surface that so the row never reads as having no destination.
    if not out and spec.get("silent"):
        out.append({"kind": "silent", "label": "Silent"})
    return out


def _decorate_automation(row: dict) -> dict:
    """Normalise an agent_automations row for the dashboard."""
    return {
        "id": row.get("id"),
        "type": "scheduled",
        "agent_id": row.get("agent_id"),
        "label": row.get("task_label") or "",
        "prompt": row.get("prompt") or "",
        "trigger": row.get("schedule_cron") or "",
        "timezone": row.get("timezone") or "UTC",
        "channel": row.get("channel"),
        "silent": bool(row.get("silent", False)),
        "delivery": _summarize_delivery(row),
        "enabled": bool(row.get("enabled", True)),
        "next_run_at": _fmt_time(row.get("next_run_at")),
        "last_run_at": _fmt_time(row.get("last_run_at")),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "last_session_id": row.get("last_session_id"),
        # run count isn't stored directly on automations; estimate from last_status presence
        "run_count": 1 if row.get("last_run_at") else 0,
        # Cross-device targeting (see app/devices/): which device runs it + the
        # offline policy. The label is resolved client-side from /api/v1/devices.
        "target_device": row.get("target_device") or "",
        "target_offline": row.get("target_offline") or "wait",
        "created_at": _fmt_time(row.get("created_at")),
    }


def _decorate_subscription(row: dict) -> dict:
    """Normalise an agent_event_subscriptions row for the dashboard."""
    filter_raw = row.get("filter_json") or row.get("filter") or {}
    if isinstance(filter_raw, str):
        try:
            filter_raw = json.loads(filter_raw)
        except Exception:
            filter_raw = {}
    return {
        "id": row.get("id"),
        "type": "event",
        "agent_id": row.get("agent_id"),
        "label": row.get("task_label") or "",
        "prompt": row.get("prompt") or "",
        "trigger": f"{row.get('source')}/{row.get('event_type')}",
        "source": row.get("source"),
        "event_type": row.get("event_type"),
        "filter": filter_raw,
        "channel": row.get("channel"),
        "silent": bool(row.get("silent", False)),
        "delivery": _summarize_delivery(row),
        "enabled": bool(row.get("enabled", True)),
        "last_event_at": _fmt_time(row.get("last_event_at")),
        "last_status": row.get("last_status"),
        "last_error": row.get("last_error"),
        "fire_count": row.get("fire_count", 0),
        "external_expiration_at": _fmt_time(row.get("external_expiration_at")),
        "last_polled_at": _fmt_time(row.get("last_polled_at")),
        "created_at": _fmt_time(row.get("created_at")),
    }


def _decorate_spawn(row: dict) -> dict:
    """Normalise an agent_spawns row for the dashboard."""
    return {
        "id": row.get("id"),
        "type": "worker",
        "agent_id": row.get("orchestrator_agent_id"),
        "spawn_agent_id": row.get("spawn_agent_id"),
        "spawn_session_id": row.get("spawn_session_id"),
        "name": row.get("name") or "",
        "kind": row.get("kind") or "spawn",
        "task": (row.get("task") or "")[:120],
        "status": row.get("status"),
        "result_summary": (row.get("result_summary") or "")[:200],
        "next_check_at": _fmt_time(row.get("next_check_at")),
        "heartbeat_at": _fmt_time(row.get("heartbeat_at")),
        "created_at": _fmt_time(row.get("created_at")),
    }


# Spawn statuses that mean the worker has finished for good (terminal). Anything
# else (pending / queued / running / session_created / active / …) is still in
# flight and belongs in the main dashboard.
_TERMINAL_SPAWN_STATUS = {
    "done", "completed", "complete", "error", "failed", "ok",
    "stopped", "skipped", "cancelled", "canceled",
}


def _is_completed(dec: dict) -> bool:
    """Has this automation finished for good — i.e. it will never run again on its
    own — versus still in-process or scheduled for a future run?

      • scheduled — a one-off that already fired, or a recurring task that expired
        / exhausted its retries: it has run at least once and has no next run. A
        *paused* recurring task keeps its next-run time, so it stays active.
      • worker    — the spawn reached a terminal status.
      • event / webhook / clone — perpetual listeners/agents, never "completed".
    """
    t = dec.get("type")
    if t == "scheduled":
        return bool(dec.get("last_run_at")) and not dec.get("next_run_at")
    if t == "worker":
        return (dec.get("status") or "").strip().lower() in _TERMINAL_SPAWN_STATUS
    return False


@router.get("/dashboard")
async def get_automations_dashboard(
    request: Request,
    user_id: str = Query(...),
    agent_id: Optional[str] = Query(default=None),
    view: str = Query(default="active"),
):
    """
    Aggregate all automation-like activity for the caller's agents.

    ``view`` selects one of three partitions of the caller's automations:
      • "active" (default) — only in-process or future automations (the clean
        main list): scheduled tasks with a next run, live worker spawns, event
        triggers, webhooks, clones.
      • "completed" — automations that have finished for good (see
        ``_is_completed``): one-off tasks that fired, expired/exhausted recurring
        tasks, and worker spawns in a terminal status. A history view that keeps
        the main list uncluttered without deleting anything.
      • "bin" — the soft-deleted rows of every automation type (deleted_at set,
        or status='clone_trashed' for clones), mirroring the Agents and Sessions
        recycling bins.

    Both "active" and "completed" read the same non-deleted rows and differ only
    in which partition they keep; the response always carries ``completed_count``
    (for the toolbar's Completed badge) except in the bin.

    Returns:
      - agents: list of { agent_id, agent_name, agent_description,
                  automations, event_subscriptions, spawns }
      - webhooks: user-level generic webhook registrations
      - clones: orphaned/standalone clone agents (not linked to a live spawn)
      - completed_count: number of finished automations (omitted in the bin)
    """
    await _require_automations_page_access(request)
    bin_view = (view == "bin")
    completed_view = (view == "completed")
    # Verify caller identity. Open/local mode is single-user full-trust, so the
    # claimed user_id is honoured there; otherwise the JWT (Authorization header
    # or ?token=) MUST match the requested user_id (admins may view any user). A
    # failure now propagates as 401/403 — previously it was swallowed and fell
    # back to the unverified user_id param, letting anyone read another user's
    # automations, schedules, delivery recipients and webhooks.
    from app.admin.settings import get_access_mode
    if get_access_mode() == "open":
        caller_id = user_id
    else:
        caller_id = await assert_caller_is(request, user_id)

    db = get_db()

    # 1. Resolve which agents to query
    if agent_id:
        # Single-agent scoped view
        agent = await db.get_agent_by_id(agent_id)
        if not agent:
            raise HTTPException(status_code=404, detail="Agent not found.")
        # Auth: caller must own or be admin of this agent
        from app.api.agents import _is_agent_admin
        if not await _is_agent_admin(db, agent_id, caller_id):
            raise HTTPException(status_code=403, detail="Not authorized to view this agent.")
        agent_ids = [agent_id]
        agent_map = {agent_id: agent}
    else:
        # All agents the caller can see
        all_agents = await db.list_agents_for_user(caller_id, include_admin=await db.is_user_admin(caller_id))
        agent_ids = [a["id"] for a in all_agents if a.get("status") in ("active", None, "")]
        agent_map = {a["id"]: a for a in all_agents}

    # 2. Fetch automations for all agents
    all_automations: List[dict] = []
    for aid in agent_ids:
        rows = await db.list_automations(agent_id=aid, owner_user_id=caller_id, bin_view=bin_view)
        all_automations.extend(rows)

    # 3. Fetch event subscriptions for all agents
    all_subs: List[dict] = []
    for aid in agent_ids:
        rows = await db.list_event_subscriptions(agent_id=aid, owner_user_id=caller_id, bin_view=bin_view)
        all_subs.extend(rows)

    # 4. Fetch spawns for all agents
    all_spawns: List[dict] = []
    for aid in agent_ids:
        try:
            rows = await _list_spawns_by_agent(aid, bin_view=bin_view)
            all_spawns.extend(rows)
        except Exception as e:
            logger.warning("Failed to list spawns for agent %s: %s", aid, e)

    # 5. Fetch webhooks (user-level, not agent-level). Webhooks are perpetual —
    # they never "complete" — so the completed view skips them entirely.
    webhooks: List[dict] = []
    if not completed_view:
        try:
            wh_rows = await db.list_webhooks(caller_id, bin_view=bin_view)
            for w in wh_rows:
                webhooks.append({
                    "id": w.get("id"),
                    "type": "webhook",
                    "name": w.get("name") or "",
                    "instructions": w.get("instructions") or "",
                    "active": bool(w.get("active", True)),
                    "created_at": _fmt_time(w.get("created_at")),
                })
        except Exception as e:
            logger.warning("Failed to list webhooks: %s", e)

    # 6. Find clone agents (status='clone') that aren't linked to a live spawn.
    # Clones are standalone agents, not finishing tasks — skipped in completed view.
    clones: List[dict] = []
    if not completed_view:
        try:
            spawn_agent_ids = {s.get("spawn_agent_id") for s in all_spawns if s.get("spawn_agent_id")}
            clone_rows = await _list_clone_agents(db, caller_id, bin_view=bin_view)
            for c in clone_rows:
                meta = c.get("metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                clone_of = meta.get("clone_of") or ""
                cid = c.get("id", "")
                clones.append({
                    "id": cid,
                    "type": "clone",
                    "name": c.get("name") or "",
                    "clone_of": clone_of,
                    "status": c.get("status"),
                    "has_live_spawn": cid in spawn_agent_ids,
                    "created_at": _fmt_time(c.get("created_at")),
                })
        except Exception as e:
            logger.warning("Failed to list clone agents: %s", e)

    # 7. Build per-agent output. For the active/completed views we read the same
    # non-deleted rows and split them by _is_completed: "active" keeps the in-
    # process/future ones, "completed" keeps the finished ones. The bin shows
    # whatever the bin_view fetch returned (no further split). We also tally
    # completed_count over every non-deleted row so the toolbar can badge it.
    def _keep(dec: dict) -> bool:
        if bin_view:
            return True
        return _is_completed(dec) == completed_view

    completed_count = 0

    automations_by_agent: Dict[str, list] = {a: [] for a in agent_ids}
    for row in all_automations:
        dec = _decorate_automation(row)
        if not bin_view and _is_completed(dec):
            completed_count += 1
        aid = row.get("agent_id")
        if aid in automations_by_agent and _keep(dec):
            automations_by_agent[aid].append(dec)

    subs_by_agent: Dict[str, list] = {a: [] for a in agent_ids}
    for row in all_subs:
        dec = _decorate_subscription(row)
        if not bin_view and _is_completed(dec):
            completed_count += 1
        aid = row.get("agent_id")
        if aid in subs_by_agent and _keep(dec):
            subs_by_agent[aid].append(dec)

    spawns_by_agent: Dict[str, list] = {a: [] for a in agent_ids}
    for row in all_spawns:
        dec = _decorate_spawn(row)
        if not bin_view and _is_completed(dec):
            completed_count += 1
        aid = row.get("orchestrator_agent_id")
        if aid in spawns_by_agent and _keep(dec):
            spawns_by_agent[aid].append(dec)

    agents_out = []
    for aid in agent_ids:
        agent = agent_map.get(aid, {})
        agents_out.append({
            "agent_id": aid,
            "agent_name": agent.get("name") or "Unknown",
            "agent_description": (agent.get("description") or "")[:100],
            "agent_icon": "",
            "agent_engine": "",
            "automations": automations_by_agent.get(aid, []),
            "event_subscriptions": subs_by_agent.get(aid, []),
            "spawns": spawns_by_agent.get(aid, []),
        })
        # Resolve icon and engine from metadata
        meta_raw = agent.get("metadata") or "{}"
        if isinstance(meta_raw, str):
            try:
                import json as _json
                meta = _json.loads(meta_raw)
                if isinstance(meta, dict):
                    if meta.get("icon"):
                        agents_out[-1]["agent_icon"] = meta["icon"]
                    if meta.get("engine"):
                        agents_out[-1]["agent_engine"] = meta["engine"]
            except Exception:
                pass

    out: Dict[str, Any] = {
        "agents": agents_out,
        "webhooks": webhooks,
        "clones": clones,
    }
    # The bin's fetch returns deleted rows, so a completed tally there would be
    # meaningless; the badge only ever reads it from the active view.
    if not bin_view:
        out["completed_count"] = completed_count
    return out


# ── Manage endpoints (run history + pause/resume/edit/delete/run-now) ──────
# The /dashboard route above is read-only aggregation. These add the
# per-run history view and the mutations the dashboard's row controls call.
# Routes use distinct paths from /dashboard, so both coexist on the same prefix.

async def _find_row(db, row_id: str):
    """Return (row, table) for an id across both trigger tables, else (None, None)."""
    row = await db.get_automation(row_id)
    if row:
        return row, "automation"
    row = await db.get_event_subscription(row_id)
    if row:
        return row, "subscription"
    return None, None


async def _owns(row: dict, caller_id: str) -> bool:
    return bool(row) and row.get("owner_user_id") == caller_id


async def _owns_clone(db, clone_id: str, caller_id: str) -> bool:
    """True if the clone agent belongs to caller_id, or the caller is an admin.

    Clones are agents (status 'clone'/'clone_trashed') whose owner is stored in
    the agent's metadata (``owner_user_id``/``user_id``) — they're not in the
    automation tables, so _find_row/_owns don't cover them. Ownerless clones are
    orphans shown to everyone by _list_clone_agents, so anyone may bin those; a
    clone OWNED by another user is refused (was: any caller could destroy any
    clone).
    """
    try:
        agent = await db.get_agent_by_id(clone_id)
    except Exception:
        return False
    if not agent or agent.get("status") not in ("clone", "clone_trashed"):
        return False
    meta = agent.get("metadata") or {}
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    owner = meta.get("owner_user_id") or meta.get("user_id")
    if not owner or owner == caller_id:
        return True
    try:
        return bool(await db.is_user_admin(caller_id))
    except Exception:
        return False


@router.get("/{automation_id}/runs")
async def automation_runs(request: Request, automation_id: str,
                          user_id: str = Query(...), limit: int = Query(default=50)):
    """Per-run history for one automation or event subscription the caller owns."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
        raise HTTPException(status_code=404, detail="automation not found")
    if table == "subscription":
        runs = await db.list_automation_runs(subscription_id=automation_id, limit=limit)
    else:
        runs = await db.list_automation_runs(automation_id=automation_id, limit=limit)
    return {"runs": runs, "count": len(runs)}


@router.patch("/{automation_id}")
async def patch_automation(request: Request, automation_id: str, user_id: str = Query(...)):
    """Edit / pause / resume. Body is a JSON object of fields to set."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    try:
        body = await request.json()
    except Exception:
        body = {}
    from app.automation.runner import _compute_next_run
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
        raise HTTPException(status_code=404, detail="automation not found")

    common = {"task_label", "prompt", "enabled", "delivery_json", "run_mode",
              "clone_abilities", "max_per_day", "disable_after_failures",
              "expires_at", "retry_max", "retry_backoff_seconds"}
    fields: Dict[str, Any] = {k: v for k, v in body.items() if k in common}
    if table == "automation":
        if "schedule_cron" in body:
            fields["schedule_cron"] = body["schedule_cron"]
            fields["next_run_at"] = _compute_next_run(
                body["schedule_cron"], body.get("timezone") or row.get("timezone") or "UTC")
        if "timezone" in body:
            fields["timezone"] = body["timezone"]
        if body.get("enabled") and (row.get("schedule_kind") or "cron") != "once" and not row.get("next_run_at"):
            fields.setdefault("next_run_at", _compute_next_run(
                fields.get("schedule_cron") or row.get("schedule_cron") or "",
                fields.get("timezone") or row.get("timezone") or "UTC"))
        updated = await db.update_automation(automation_id, **fields)
    else:
        if "filter" in body:
            fields["filter_json"] = body["filter"]
        updated = await db.update_event_subscription(automation_id, **fields)
    return {"status": "ok", "automation": updated}


@router.delete("/{automation_id}")
async def delete_automation(request: Request, automation_id: str, user_id: str = Query(...),
                            permanent: bool = Query(default=False)):
    """Recycle (default) or permanently delete an automation/subscription.

    Default (``permanent=false``): SOFT delete — the row moves to the Automations
    recycling bin (deleted_at set, enabled=0) and stops firing, but keeps its
    external provider subscription registered so a restore is lossless.

    ``permanent=true``: HARD delete — erase the row, unregister any external event
    subscription, and reap a dedicated clone runner if one exists. Irreversible.
    """
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    # The row may already be in the bin (find_row ignores deleted_at), so this
    # also resolves rows for the permanent-delete-from-bin case.
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
        raise HTTPException(status_code=404, detail="automation not found")

    if not permanent:
        # Soft delete → recycle. Leave external subscriptions + clone runner in
        # place; they're only torn down on permanent delete.
        if table == "automation":
            ok = await db.trash_automation(automation_id)
        else:
            ok = await db.trash_event_subscription(automation_id)
        return {"status": "ok" if ok else "error", "recycled": bool(ok)}

    runner_id = (row.get("runner_agent_id") or "").strip()
    if table == "automation":
        ok = await db.delete_automation(automation_id)
    else:
        try:
            from app.automation.sync import _unregister_event_sub
            await _unregister_event_sub(row)
        except Exception:
            pass
        ok = await db.delete_event_subscription(automation_id)
    if runner_id and row.get("run_mode") == "dedicated_clone":
        try:
            await db.delete_clone_agent(runner_id)
        except Exception:
            pass
    return {"status": "ok" if ok else "error"}


@router.post("/{automation_id}/restore")
async def restore_automation(request: Request, automation_id: str, user_id: str = Query(...)):
    """Restore a recycled automation/subscription back to the active dashboard."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
        raise HTTPException(status_code=404, detail="automation not found")
    from app.automation.entitlements import (
        AutomationEntitlementError,
        enforce_automation_entitlement,
    )
    try:
        await enforce_automation_entitlement(db, caller_id, additional=1)
    except AutomationEntitlementError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if table == "automation":
        ok = await db.restore_automation(automation_id)
    else:
        ok = await db.restore_event_subscription(automation_id)
    return {"status": "ok" if ok else "error", "restored": bool(ok)}


def _spawn_owned_by(conn, spawn_id: str, caller_id: str, db):
    """Return the spawn row dict if the caller owns it, raising 403 if not, or
    None if the row doesn't exist. Shared by the spawn delete/restore routes."""
    row = conn.execute(
        "SELECT user_id, orchestrator_agent_id FROM agent_spawns WHERE id = ?",
        (spawn_id,),
    ).fetchone()
    return dict(row) if row else None


@router.delete("/spawn/{spawn_id}")
async def delete_spawn(request: Request, spawn_id: str, user_id: str = Query(...),
                       permanent: bool = Query(default=False)):
    """Recycle (default) or permanently remove a worker-spawn ledger row
    (agent_spawns). Scoped to the caller.

    Workers are entries the orchestration plugin writes to its own
    ``agent_spawns`` table; recycling sets deleted_at so the row drops off the
    dashboard into the bin (permanent=true deletes it). We never touch core
    schema — just the plugin-owned table via the raw connection.
    """
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    conn = raw() if raw else None
    if conn is None:
        raise HTTPException(status_code=500, detail="db unavailable")
    try:
        row = _spawn_owned_by(conn, spawn_id, caller_id, db)
        if not row:
            return {"status": "ok"}  # already gone — idempotent
        owner = (row.get("user_id") or "").strip()
        if owner and owner != caller_id:
            from app.api.agents import _is_agent_admin
            orch = row.get("orchestrator_agent_id")
            if not (orch and await _is_agent_admin(db, orch, caller_id)):
                raise HTTPException(status_code=403, detail="Not authorized.")
        if permanent:
            conn.execute("DELETE FROM agent_spawns WHERE id = ?", (spawn_id,))
        else:
            from app.db.local import _now_iso
            conn.execute("UPDATE agent_spawns SET deleted_at = ? WHERE id = ?",
                         (_now_iso(), spawn_id))
        conn.commit()
        return {"status": "ok", "recycled": (not permanent)}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to delete spawn %s: %s", spawn_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.post("/spawn/{spawn_id}/restore")
async def restore_spawn(request: Request, spawn_id: str, user_id: str = Query(...)):
    """Restore a recycled worker-spawn row back to the active dashboard."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    conn = raw() if raw else None
    if conn is None:
        raise HTTPException(status_code=500, detail="db unavailable")
    try:
        row = _spawn_owned_by(conn, spawn_id, caller_id, db)
        if not row:
            return {"status": "ok"}
        owner = (row.get("user_id") or "").strip()
        if owner and owner != caller_id:
            from app.api.agents import _is_agent_admin
            orch = row.get("orchestrator_agent_id")
            if not (orch and await _is_agent_admin(db, orch, caller_id)):
                raise HTTPException(status_code=403, detail="Not authorized.")
        conn.execute("UPDATE agent_spawns SET deleted_at = NULL WHERE id = ?", (spawn_id,))
        conn.commit()
        return {"status": "ok", "restored": True}
    except HTTPException:
        raise
    except Exception as e:
        logger.warning("Failed to restore spawn %s: %s", spawn_id, e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        conn.close()


@router.delete("/clone/{clone_id}")
async def delete_clone(request: Request, clone_id: str, user_id: str = Query(...),
                       permanent: bool = Query(default=False)):
    """Recycle (default) or permanently delete a standalone clone agent."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    if not await _owns_clone(db, clone_id, caller_id):
        raise HTTPException(status_code=404, detail="Clone not found.")
    try:
        if permanent:
            ok = await db.delete_clone_agent(clone_id)
        else:
            ok = await db.trash_clone_agent(clone_id)
        return {"status": "ok", "recycled": (not permanent) and bool(ok)}  # idempotent
    except Exception as e:
        logger.warning("Failed to delete clone %s: %s", clone_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clone/{clone_id}/restore")
async def restore_clone(request: Request, clone_id: str, user_id: str = Query(...)):
    """Restore a recycled clone agent back to the active dashboard."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    if not await _owns_clone(db, clone_id, caller_id):
        raise HTTPException(status_code=404, detail="Clone not found.")
    try:
        ok = await db.restore_clone_agent(clone_id)
        return {"status": "ok", "restored": bool(ok)}
    except Exception as e:
        logger.warning("Failed to restore clone %s: %s", clone_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/webhook/{webhook_id}")
async def delete_webhook_row(request: Request, webhook_id: str, user_id: str = Query(...),
                             permanent: bool = Query(default=False)):
    """Recycle (default) or permanently delete a user-level generic webhook."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    try:
        if permanent:
            ok = await db.delete_webhook(webhook_id=webhook_id, user_id=caller_id)
        else:
            ok = await db.trash_webhook(webhook_id=webhook_id, user_id=caller_id)
        return {"status": "ok", "recycled": (not permanent) and bool(ok)}  # idempotent
    except Exception as e:
        logger.warning("Failed to delete webhook %s: %s", webhook_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/webhook/{webhook_id}/restore")
async def restore_webhook_row(request: Request, webhook_id: str, user_id: str = Query(...)):
    """Restore a recycled webhook back to the active dashboard."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    try:
        ok = await db.restore_webhook(webhook_id=webhook_id, user_id=caller_id)
        return {"status": "ok", "restored": bool(ok)}
    except Exception as e:
        logger.warning("Failed to restore webhook %s: %s", webhook_id, e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/{automation_id}/run-now")
async def run_now(request: Request, automation_id: str, user_id: str = Query(...)):
    """Fire a scheduled automation immediately (not applicable to event subscriptions)."""
    await _require_automations_page_access(request)
    caller_id = await assert_caller_is(request, user_id)
    db = get_db()
    row, table = await _find_row(db, automation_id)
    if not await _owns(row, caller_id):
        raise HTTPException(status_code=404, detail="automation not found")
    if table != "automation":
        raise HTTPException(status_code=400, detail="run-now applies to scheduled tasks only")
    try:
        from app.scheduler import get_scheduler
        res = await get_scheduler().run_now(automation_id)
        return {"status": "ok", "result": res}
    except Exception as e:
        logger.exception("run-now failed for %s", automation_id)
        raise HTTPException(status_code=500, detail=str(e))


# ── Helper: list spawns by orchestrator agent id ──────────────────────────
# The agent_spawns table lives in the agent_orchestration plugin, not in the
# core schema. We query it via the raw connection (same pattern as the
# plugin's own _spawns_list). No-op gracefully if the table doesn't exist.

async def _list_spawns_by_agent(orchestrator_agent_id: str, bin_view: bool = False) -> List[dict]:
    """Return spawn rows for a given orchestrator agent id. By default only
    active (non-recycled) rows; bin_view=True returns only recycled ones."""
    from app.db import get_db
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    if raw is None:
        return []
    conn = raw()
    if conn is None:
        return []
    try:
        # Ensure the table exists (created by the plugin, but safe to re-run).
        # deleted_at is the recycling-bin marker (NULL = active).
        try:
            conn.execute(
                """CREATE TABLE IF NOT EXISTS agent_spawns (
                    id TEXT PRIMARY KEY,
                    user_id TEXT,
                    orchestrator_session_id TEXT,
                    orchestrator_agent_id TEXT,
                    spawn_session_id TEXT,
                    spawn_agent_id TEXT,
                    name TEXT,
                    task TEXT,
                    status TEXT,
                    result_summary TEXT,
                    next_check_at TEXT,
                    check_note TEXT,
                    resume_attempts INTEGER,
                    heartbeat_at TEXT,
                    claim_token TEXT,
                    kind TEXT,
                    deleted_at TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )"""
            )
            # Add later columns to pre-existing spawn tables (guarded; SQLite has
            # no ADD COLUMN IF NOT EXISTS). `kind` marks a delegation to a real
            # saved agent vs. an ordinary clone spawn (owned by the orchestration
            # plugin, added here too so the column exists whichever subsystem
            # created the table first).
            try:
                have = {r[1] for r in conn.execute("PRAGMA table_info(agent_spawns)").fetchall()}
                if "deleted_at" not in have:
                    conn.execute("ALTER TABLE agent_spawns ADD COLUMN deleted_at TEXT")
                if "kind" not in have:
                    conn.execute("ALTER TABLE agent_spawns ADD COLUMN kind TEXT")
            except Exception:
                pass
            for idx_sql in [
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_orch ON agent_spawns(orchestrator_session_id)",
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_session ON agent_spawns(spawn_session_id)",
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_check ON agent_spawns(next_check_at)",
                "CREATE INDEX IF NOT EXISTS idx_agent_spawns_hb ON agent_spawns(status, heartbeat_at)",
            ]:
                try:
                    conn.execute(idx_sql)
                except Exception:
                    pass
        except Exception:
            pass
        cond = "deleted_at IS NOT NULL" if bin_view else "deleted_at IS NULL"
        rows = conn.execute(
            f"""SELECT * FROM agent_spawns
               WHERE orchestrator_agent_id = ? AND {cond}
               ORDER BY created_at DESC
               LIMIT 200""",
            (orchestrator_agent_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        logger.debug("agent_spawns table not available: %s", e)
        return []
    finally:
        conn.close()


async def _list_clone_agents(db, user_id: str, bin_view: bool = False) -> List[dict]:
    """List clone agents that belong to this user. By default the active clones
    (status='clone'); bin_view=True returns recycled ones (status='clone_trashed').

    Clones are hidden from the normal agent roster and recycling bin.
    This is the dashboard's job: surface them so they're visible somewhere.
    """
    raw = getattr(db, "_get_conn", None)
    if raw is None:
        return []
    conn = raw()
    if conn is None:
        return []
    try:
        clone_status = "clone_trashed" if bin_view else "clone"
        rows = conn.execute(
            """SELECT id, name, description, status, metadata, template_id, created_at, updated_at
               FROM agents
               WHERE status = ?
               ORDER BY created_at DESC
               LIMIT 200""",
            (clone_status,),
        ).fetchall()
        # Filter by owner in Python (portable across SQLite/Postgres)
        result = []
        for r in rows:
            d = dict(r)
            meta = d.get("metadata") or "{}"
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            owner = meta.get("owner_user_id") or meta.get("user_id")
            if not owner or owner == user_id:
                result.append(d)
        return result
    except Exception as e:
        logger.debug("Failed to list clone agents: %s", e)
        return []
    finally:
        conn.close()

"""Diff parsed automation rows against DB and notify scheduler / event runtime.

Handles both:
  * cron tasks → ``agent_automations``
  * event subscriptions → ``agent_event_subscriptions`` (and provider-side
    register / unregister via the source plugin)
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.automation.entitlements import enforce_slot_automation_projection
from app.automation.parser import (
    ParsedEventSubscription,
    ParsedTask,
    parse_automation_file,
)

logger = logging.getLogger(__name__)


def _task_hash(t: ParsedTask) -> str:
    payload = json.dumps({
        "kind": "cron",
        "label": t.task_label,
        "prompt": t.prompt,
        "cron": t.schedule_cron,
        "tz": t.timezone,
        "channel": t.channel,
        "recipient": t.channel_recipient,
        "silent": t.silent,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _event_sub_hash(s: ParsedEventSubscription) -> str:
    payload = json.dumps({
        "kind": "event",
        "label": s.task_label,
        "prompt": s.prompt,
        "source": s.source,
        "event_type": s.event_type,
        "filter": s.filter_dict,
        "channel": s.channel,
        "recipient": s.channel_recipient,
        "silent": s.silent,
    }, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _next_run_from_cron(cron_expr: str, tz: str = "UTC") -> Optional[str]:
    try:
        from croniter import croniter
    except ImportError:
        return None
    try:
        from zoneinfo import ZoneInfo
        base = datetime.now(ZoneInfo(tz))
    except Exception:
        base = datetime.now(timezone.utc)
    try:
        nxt = croniter(cron_expr, base).get_next(datetime)
        return nxt.astimezone(timezone.utc).isoformat()
    except Exception as e:
        logger.warning("Could not compute next_run for cron %r tz %r: %s", cron_expr, tz, e)
        return None


async def _register_event_sub(db, sub_row: dict, parsed: ParsedEventSubscription) -> None:
    """Call source.register_subscription and persist the returned IDs/cursor."""
    try:
        from app.events import get_manager
        mgr = get_manager()
        source = mgr.get(parsed.source)
        if source is None:
            logger.warning("Source %s not registered; saving sub but no provider-side subscription",
                           parsed.source)
            return
        reg = await source.register_subscription(
            owner_user_id=sub_row["owner_user_id"],
            agent_id=sub_row.get("agent_id", ""),
            event_type=parsed.event_type,
            filter_dict=parsed.filter_dict,
        )
        await db.update_event_subscription(
            sub_row["id"],
            external_subscription_id=reg.external_subscription_id,
            external_resource_id=reg.external_resource_id,
            external_expiration_at=reg.external_expiration_at,
            external_metadata=reg.external_metadata or {},
            poll_cursor=reg.poll_cursor,
            last_status="ok",
            last_error=None,
        )
    except Exception as e:
        logger.exception("register_subscription failed for sub %s: %s", sub_row["id"], e)
        try:
            await db.update_event_subscription(
                sub_row["id"],
                last_status="error",
                last_error=f"register failed: {str(e)[:400]}",
            )
        except Exception:
            pass


async def _unregister_event_sub(removed_row: dict) -> None:
    try:
        from app.events import get_manager
        mgr = get_manager()
        source = mgr.get(removed_row.get("source") or "")
        if source is None:
            return
        await source.unregister_subscription(removed_row)
    except Exception as e:
        logger.warning("unregister_subscription failed for sub %s: %s",
                       removed_row.get("id"), e)


async def sync_automations(
    db,
    agent_id: str,
    owner_user_id: str,
    slot_content: str,
    agent_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Re-parse the slot, upsert/delete cron + event rows, notify the runtimes."""
    result = await parse_automation_file(slot_content, agent_context or {})

    # Validate the complete post-sync shape before the first write. Slot rows
    # are replacements, so edits at the tier cap remain possible.
    desired_task_hashes = {_task_hash(task) for task in result.tasks}
    desired_event_hashes = {
        _event_sub_hash(subscription) for subscription in result.event_subscriptions
    }
    if desired_task_hashes or desired_event_hashes:
        await enforce_slot_automation_projection(
            db,
            owner_user_id,
            agent_id,
            task_hashes=desired_task_hashes,
            event_hashes=desired_event_hashes,
        )

    # ── Cron tasks ────────────────────────────────────────────────────
    keep_task_hashes: List[str] = []
    upserted_tasks: List[dict] = []
    for t in result.tasks:
        h = _task_hash(t)
        keep_task_hashes.append(h)
        next_run = _next_run_from_cron(t.schedule_cron, t.timezone or "UTC")
        row = await db.upsert_automation(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            source_hash=h,
            task_label=t.task_label,
            prompt=t.prompt,
            schedule_cron=t.schedule_cron,
            schedule_natural=t.schedule_natural,
            timezone=t.timezone,
            channel=t.channel,
            channel_recipient=t.channel_recipient,
            silent=t.silent,
            enabled=True,
            next_run_at=next_run,
        )
        upserted_tasks.append(row)
    removed_tasks = await db.delete_automations_not_in(
        agent_id, owner_user_id, keep_task_hashes,
    )
    try:
        from app.scheduler import get_scheduler
        sched = get_scheduler()
        await sched.sync_tasks(agent_id)
    except Exception as e:
        logger.debug("Scheduler sync_tasks failed (non-fatal): %s", e)

    # ── Event subscriptions ───────────────────────────────────────────
    keep_evt_hashes: List[str] = []
    upserted_evts: List[dict] = []
    to_register: List[tuple] = []  # (row, parsed)

    # Snapshot pre-state so we know which rows are NEW vs unchanged
    pre_rows = await db.list_event_subscriptions(
        agent_id=agent_id, owner_user_id=owner_user_id,
    )
    pre_hash_set = {r.get("source_hash") for r in pre_rows}

    for s in result.event_subscriptions:
        h = _event_sub_hash(s)
        keep_evt_hashes.append(h)
        row = await db.upsert_event_subscription(
            agent_id=agent_id,
            owner_user_id=owner_user_id,
            source_hash=h,
            source=s.source,
            event_type=s.event_type,
            filter_dict=s.filter_dict,
            task_label=s.task_label,
            prompt=s.prompt,
            trigger_natural=s.trigger_natural,
            channel=s.channel,
            channel_recipient=s.channel_recipient,
            silent=s.silent,
            enabled=True,
        )
        upserted_evts.append(row)
        if h not in pre_hash_set:
            to_register.append((row, s))

    removed_evts = await db.delete_event_subscriptions_not_in(
        agent_id, owner_user_id, keep_evt_hashes,
    )

    # Provider-side: unregister removed rows, register newly-added rows.
    for r in removed_evts:
        await _unregister_event_sub(r)
    for row, parsed in to_register:
        await _register_event_sub(db, row, parsed)

    # Reload event subs so the caller sees the post-register state.
    event_subs = await db.list_event_subscriptions(
        agent_id=agent_id, owner_user_id=owner_user_id,
    )
    tasks = await db.list_automations(agent_id=agent_id, owner_user_id=owner_user_id)

    return {
        "tasks": tasks,
        "removed": removed_tasks,
        "event_subscriptions": event_subs,
        "removed_event_subscriptions": removed_evts,
        "error": result.error,
    }

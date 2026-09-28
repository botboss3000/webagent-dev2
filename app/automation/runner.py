"""Shared run engine for automations, one-shot timers, and event subscriptions.

Both the cron scheduler (`app.scheduler`) and the event runtime (`app.events`)
funnel through here so that run-mode, guardrails, delivery, per-run history,
retries/auto-disable, and per-automation memory behave identically everywhere.

A "row" is an `agent_automations` row (cron / once / timer / manual) or an
`agent_event_subscriptions` row (event). The only difference downstream is which
update method persists state — captured by ``table`` ('automation' | 'subscription').

Run modes (column ``run_mode``):
  • inline           → run on the master agent (default).
  • fresh_clone      → spin a new ephemeral clone per run; reaped afterwards.
  • dedicated_clone  → one long-lived clone owns the row (persisted in runner_agent_id).
  • headless         → master runner, output suppressed (delivery handles silence).
Clone modes require the ``agent_orchestration`` ability on the master; otherwise
they fall back to inline with a logged warning.
"""

from __future__ import annotations

import contextvars
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.automation import delivery

logger = logging.getLogger(__name__)

ORCH_ABILITY = "agent_orchestration"

# Custom exception for failing fast when a target session is recycled/dead
class _RecycledSessionError(Exception):
    """Raised when an automation tries to write into a recycled/deleted session."""
    def __init__(self, session_id: str):
        self.session_id = session_id
        super().__init__(f"Session {session_id[:12]} is recycled or deleted")

# Set during a turn so the `remember_automation_state` tool can write back to the
# right row. Propagates to tool handlers because the agent loop is awaited inline.
_current: "contextvars.ContextVar[Optional[tuple]]" = contextvars.ContextVar(
    "current_automation", default=None
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _compute_next_run(cron_expr: str, tz: str = "UTC") -> Optional[str]:
    if not cron_expr:
        return None
    try:
        from croniter import croniter
    except ImportError:
        # Never fail silently: a missing croniter nulls next_run_at and kills
        # every recurring schedule this process touches (seen when a stray
        # server ran under an interpreter without project deps).
        logger.error(
            "croniter is not installed in this interpreter — cannot recompute "
            "next_run_at for cron %r; the recurring schedule will stop.", cron_expr)
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
        logger.warning("Cron recompute failed for %r tz %r: %s", cron_expr, tz, e)
        return None


def _seconds_from_now_iso(seconds: int) -> str:
    from datetime import timedelta
    return (datetime.now(timezone.utc) + timedelta(seconds=max(1, seconds))).isoformat()


# ── current-automation context (for the memory write tool) ───────────────────

def current_automation_id() -> Optional[str]:
    cur = _current.get()
    return cur[1] if cur else None


def begin_run_context(db, row_id: str, table: str):
    """Set the current-automation context so `remember_automation_state` can write
    back during a turn the caller runs itself (e.g. the event runtime). Returns a
    token for `end_run_context`."""
    return _current.set((db, row_id, table))


def end_run_context(token) -> None:
    try:
        _current.reset(token)
    except Exception:
        pass


async def set_automation_memory(data: Any) -> bool:
    """Persist per-automation memory for the row whose run is in progress.

    Called by the `remember_automation_state` tool during an automation run.
    """
    cur = _current.get()
    if not cur:
        return False
    db, row_id, table = cur
    try:
        if table == "subscription":
            await db.update_event_subscription(row_id, memory_json=data)
        else:
            await db.update_automation(row_id, memory_json=data)
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("set_automation_memory failed: %s", e)
        return False


async def _update_row(db, row_id: str, table: str, **fields) -> None:
    if table == "subscription":
        await db.update_event_subscription(row_id, **fields)
    else:
        await db.update_automation(row_id, **fields)


# ── guardrails ───────────────────────────────────────────────────────────────

def enforce_guardrails(row: Dict[str, Any]) -> tuple:
    """Return (allowed, reason). Cheap pre-fire checks: enabled / expiry / daily cap."""
    if not row.get("enabled"):
        return False, "disabled"
    exp = (row.get("expires_at") or "").strip()
    if exp and exp <= _now_iso():
        return False, "expired"
    mpd = row.get("max_per_day")
    if mpd:
        if row.get("runs_today_date") == _today() and int(row.get("runs_today") or 0) >= int(mpd):
            return False, "daily cap reached"
    return True, ""


async def _orchestration_enabled(db, agent_id: str) -> bool:
    try:
        from app.admin.integrations import is_ability_enabled_for_agent
        return bool(await is_ability_enabled_for_agent(agent_id, ORCH_ABILITY))
    except Exception:
        return False


# ── runner resolution (who/where runs it) ────────────────────────────────────

async def resolve_runner(db, row: Dict[str, Any], *, table: str, label: str) -> Dict[str, Any]:
    """Decide which agent id runs this row. Returns
    {runner_agent_id, effective_mode, is_ephemeral}."""
    master_id = row["agent_id"]
    mode = (row.get("run_mode") or "inline").strip()

    if mode not in ("fresh_clone", "dedicated_clone"):
        # inline / headless / unknown → run on the master agent.
        return {"runner_agent_id": master_id, "effective_mode": mode or "inline",
                "is_ephemeral": False}

    if not await _orchestration_enabled(db, master_id):
        logger.warning(
            "Automation %s wants run_mode=%s but agent %s lacks the orchestration "
            "ability — falling back to inline.", row.get("id"), mode, master_id)
        return {"runner_agent_id": master_id, "effective_mode": "inline",
                "is_ephemeral": False}

    if mode == "dedicated_clone":
        existing = (row.get("runner_agent_id") or "").strip()
        if existing:
            ag = await db.get_agent_by_id(existing)
            if ag and ag.get("status") == "clone":
                return {"runner_agent_id": existing, "effective_mode": mode,
                        "is_ephemeral": False}
        clone_id = await _make_clone(db, row, label)
        await _update_row(db, row["id"], table, runner_agent_id=clone_id)
        return {"runner_agent_id": clone_id, "effective_mode": mode, "is_ephemeral": False}

    # fresh_clone
    clone_id = await _make_clone(db, row, label)
    return {"runner_agent_id": clone_id, "effective_mode": mode, "is_ephemeral": True}


async def _make_clone(db, row: Dict[str, Any], label: str) -> str:
    master_id = row["agent_id"]
    user_id = row["owner_user_id"]
    requested = row.get("clone_abilities_list")
    if requested is None:
        try:
            requested = json.loads(row.get("clone_abilities") or "[]")
        except Exception:
            requested = []
    # Clamp to the master's enabled abilities (the ceiling).
    try:
        conns = await db.get_agent_connections(master_id)
        master_abilities = {c["connection_type"] for c in conns
                            if c.get("section") == "ability" and c.get("enabled")}
    except Exception:
        master_abilities = set()
    granted = sorted(set(requested) & master_abilities)
    clone = await db.create_clone_agent(
        user_id=user_id, master_agent_id=master_id,
        name=f"{(label or 'Automation')[:36]} (auto)", abilities=granted,
    )
    return clone["id"]


# ── session creation ─────────────────────────────────────────────────────────

async def _make_session(db, *, runner_agent_id: str, user_id: str, label: str,
                        plan: Dict[str, Any], source: str) -> str:
    """Pick/create the session the turn runs in, per the resolved delivery plan."""
    mode = plan.get("session_mode") or "new_session"
    if mode == "here":
        target = (plan.get("session_id") or "").strip()
        if target:
            try:
                # Safety: refuse to run in a recycled session
                if hasattr(db, "is_session_dead"):
                    try:
                        if await db.is_session_dead(target):
                            logger.warning("Automation refused target %s: session is recycled/deleted", target[:12])
                            raise _RecycledSessionError(target)
                    except Exception as _rse:
                        if isinstance(_rse, _RecycledSessionError):
                            raise
                existing = await db.get_session(target) if hasattr(db, "get_session") else None
                if existing is None:
                    raw = db.get_raw_client()
                    rows = raw.table("sessions").select("id,user_id").eq("id", target).execute()
                    existing = (rows.data or [None])[0]
                if existing and (existing.get("user_id") == user_id):
                    return target
            except _RecycledSessionError:
                raise
            except Exception as e:
                logger.debug("here-session reuse failed for %s: %s", target, e)
        # fall through to a fresh session if the target is gone / not owned.

    session_id = f"auto-{uuid.uuid4().hex[:12]}"
    title = (label or "Automation")[:60]
    meta = {"source": source}
    try:
        from app.devices import identity as _dev_identity
        meta["device"] = {"id": _dev_identity.device_id(), "label": _dev_identity.device_label()}
    except Exception:
        pass
    if mode == "headless":
        meta["hidden"] = True
    try:
        raw = db.get_raw_client()
        raw.table("sessions").insert({
            "id": session_id, "user_id": user_id, "title": title,
            "agent_id": runner_agent_id, "metadata": json.dumps(meta),
        }).execute()
    except Exception:
        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT INTO sessions (id, user_id, title, agent_id, metadata) VALUES (?, ?, ?, ?, ?)",
                (session_id, user_id, title, runner_agent_id, json.dumps(meta)),
            )
            conn.commit()
        finally:
            conn.close()
    return session_id


# ── the agent turn ───────────────────────────────────────────────────────────

async def run_turn(
    db, *, row_id: str, table: str, runner_agent_id: str, user_id: str,
    session_id: str, prompt: str, memory: Any = None,
    extra_overlays: Optional[List[str]] = None,
) -> str:
    """Run one supervised agent turn in ``session_id`` and return its reply."""
    from app.agent.loop import run_agent_loop_buffered
    from app.agent.prompts import build_system_prompt
    from app.agent.runner import run_supervised_turn, RunOutcome

    agent = await db.get_agent_by_id(runner_agent_id) or {}
    resolved_slots = await db.resolve_prompts(runner_agent_id, user_id=user_id)
    context_docs = [
        {"id": s["slot_name"], "context_type": s["slot_name"],
         "title": s["slot_name"], "content": s["content"], "tags": []}
        for s in resolved_slots
        if (s.get("content") or "").strip() and s.get("slot_name") != "automation"
    ]
    system_prompt = await build_system_prompt(
        context_docs, brain_context=None, user_id=user_id, agent_id=runner_agent_id,
        session_id=session_id,
    )

    overlays: List[str] = []
    if memory:
        try:
            mem_str = memory if isinstance(memory, str) else json.dumps(memory)
        except Exception:
            mem_str = str(memory)
        if mem_str and mem_str not in ("{}", "null", ""):
            overlays.append(
                "[AUTOMATION MEMORY] State remembered from earlier runs of this "
                "automation:\n" + mem_str[:4000] +
                "\nWhen this changes (e.g. the last item you processed), call "
                "remember_automation_state to persist the new state.")
    if extra_overlays:
        overlays.extend(extra_overlays)
    if overlays:
        system_prompt = system_prompt + "\n\n" + "\n\n".join(overlays)

    # Live broadcast: route this run's events to the user's WebSocket subscribers
    # so a session they are VIEWING updates in real time, exactly like a typed
    # turn. The scheduler + event runtime run in-process, so this reaches the
    # user's socket. Without it an automation writing into the open session was
    # invisible until a manual refresh. Falls back silently if the chat module
    # can't be imported (e.g. a stripped edition).
    try:
        from app.api.chat import _emit_to_visualizers as _emit_live
    except Exception:
        _emit_live = None

    async def _broadcast(ev: Dict[str, Any]) -> None:
        if not _emit_live:
            return
        try:
            await _emit_live(session_id, ev, user_id=user_id)
        except Exception:
            pass

    turn_uid = None
    try:
        # Assign a session_seq so the synthetic prompt also rides the incremental
        # DB-tail the chat UI polls (the durable "always-updated" path), not just
        # the WebSocket — covers cross-worker topologies where the socket can't.
        try:
            _user_seq = await db.next_session_seq(session_id, 1)
        except Exception:
            _user_seq = None
        turn_uid = await db.insert_interaction(
            user_id, session_id, role="user", content=prompt,
            channel="automation",
            metadata=json.dumps({"source": "automation", "row_id": row_id}),
            sender_id=user_id, receiver_id=runner_agent_id, source="automation",
            session_seq=_user_seq,
        )
    except Exception as e:
        logger.debug("Could not insert synthetic interaction: %s", e)

    # Surface the injected prompt to any device viewing this session right away.
    if turn_uid:
        await _broadcast({
            "type": "user_message", "level": "user",
            "content": prompt, "id": turn_uid, "source": "automation",
        })

    raw_allowed = agent.get("allowed_tools", [])
    if isinstance(raw_allowed, str):
        try:
            raw_allowed = json.loads(raw_allowed)
        except Exception:
            raw_allowed = []

    token = _current.set((db, row_id, table))
    try:
        async def _build(replaced: bool) -> "RunOutcome":
            reply = await run_agent_loop_buffered(
                user_id=user_id, session_id=session_id, user_message=prompt,
                system_prompt=system_prompt, agent_id=runner_agent_id, history=None,
                channel="automation", timeout_seconds=600, db=db,
                agent_template_id=agent.get("template_id"),
                allowed_tools=raw_allowed or None,
                max_turns=agent.get("max_turn_count", 0),
                event_callback=_broadcast,
            )
            if not (reply or "").strip():
                return RunOutcome(status="error", stop_cause="empty_response",
                                  error="agent loop produced no final reply")
            return RunOutcome(status="complete", stop_cause="complete", reply=reply)

        outcome = await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=runner_agent_id,
            origin="automation", channel="automation", turn_id=turn_uid,
            relaunch_ctx={"origin": "automation", "session_id": session_id,
                          "user_id": user_id, "agent_id": runner_agent_id,
                          "channel": "automation", "timeout_seconds": 600},
            build_turn=_build, await_result=True, result_timeout=620,
        )
        return (outcome.reply if outcome else "") or ""
    finally:
        _current.reset(token)


# ── post-run bookkeeping (counters / schedule / retry / auto-disable) ─────────

async def _on_success(db, row: Dict[str, Any], table: str, session_id: str,
                      delivery_error: Optional[str]) -> None:
    row_id = row["id"]
    fields: Dict[str, Any] = {
        "last_run_at": _now_iso(),
        "last_session_id": session_id,
        "last_status": "ok" if not delivery_error else "error",
        "last_error": delivery_error,
        "fail_count": 0,
        "next_retry_at": None,
    }
    # daily counter (reset when the date rolls over)
    today = _today()
    if row.get("runs_today_date") == today:
        fields["runs_today"] = int(row.get("runs_today") or 0) + 1
    else:
        fields["runs_today"] = 1
        fields["runs_today_date"] = today
    # subscriptions track a lifetime fire_count + last_event timestamp
    if table == "subscription":
        fields["fire_count"] = int(row.get("fire_count") or 0) + 1
        fields["last_event_at"] = _now_iso()
    # scheduling
    if table == "automation":
        if (row.get("schedule_kind") or "cron") == "once":
            fields["enabled"] = False
            fields["next_run_at"] = None
        else:
            fields["next_run_at"] = _compute_next_run(
                row.get("schedule_cron") or "", row.get("timezone") or "UTC")
    await _update_row(db, row_id, table, **fields)


async def _on_failure(db, row: Dict[str, Any], table: str, error: str) -> None:
    row_id = row["id"]
    fail_count = int(row.get("fail_count") or 0) + 1
    retry_max = int(row.get("retry_max") or 0)
    disable_after = row.get("disable_after_failures")
    backoff = int(row.get("retry_backoff_seconds") or 0) or 60
    fields: Dict[str, Any] = {
        "last_run_at": _now_iso(),
        "last_status": "error",
        "last_error": (error or "")[:500],
        "fail_count": fail_count,
        "next_retry_at": None,
    }
    if disable_after and fail_count >= int(disable_after):
        fields["enabled"] = False
        if table == "automation":
            fields["next_run_at"] = None
    elif retry_max and fail_count <= retry_max:
        fields["next_retry_at"] = _seconds_from_now_iso(backoff * fail_count)
        # keep the normal cron cadence too for recurring tasks
        if table == "automation" and (row.get("schedule_kind") or "cron") != "once":
            fields["next_run_at"] = _compute_next_run(
                row.get("schedule_cron") or "", row.get("timezone") or "UTC")
    else:
        # give up retrying this occurrence
        if table == "automation":
            if (row.get("schedule_kind") or "cron") == "once":
                fields["enabled"] = False
                fields["next_run_at"] = None
            else:
                fields["next_run_at"] = _compute_next_run(
                    row.get("schedule_cron") or "", row.get("timezone") or "UTC")
    await _update_row(db, row_id, table, **fields)


async def _on_skip(db, row: Dict[str, Any], table: str, reason: str) -> None:
    """Skipped by guardrails — advance the schedule so we don't hot-loop."""
    row_id = row["id"]
    fields: Dict[str, Any] = {"last_status": "skipped", "last_error": reason,
                              "next_retry_at": None}
    if reason == "expired":
        fields["enabled"] = False
        if table == "automation":
            fields["next_run_at"] = None
    elif table == "automation" and (row.get("schedule_kind") or "cron") != "once":
        fields["next_run_at"] = _compute_next_run(
            row.get("schedule_cron") or "", row.get("timezone") or "UTC")
    elif table == "automation":
        # a 'once' that can't fire now → disable so it stops being re-claimed.
        fields["enabled"] = False
        fields["next_run_at"] = None
    await _update_row(db, row_id, table, **fields)


async def _dispatch_to_device(db, row: Dict[str, Any], *, table: str, run_id: str,
                              kind: str, agent_id: str, user_id: str, label: str,
                              target: str) -> Dict[str, Any]:
    """Hand a run to another device instead of running it here.

    Honors the per-row offline policy: ``target_offline='skip'`` drops this
    occurrence when the target is offline at fire time; 'wait' (the default)
    queues the job until the target wakes and claims it (capped by the worker's
    staleness limit so it never fires absurdly late). The schedule is advanced
    either way — a skipped occurrence via the normal skip bookkeeping, a handed
    -off one via the success bookkeeping. Returns a run_one-style result dict.

    Note: clone run-modes and delivery specs apply to LOCAL runs only — a
    dispatched run uses the base agent and runs in the target's own session.
    """
    from app.devices import dispatch as _dispatch

    dev = await _dispatch.resolve_target(target)
    online = bool(dev and dev.get("online"))
    policy = (row.get("target_offline") or "wait").strip().lower()
    target_label = (dev or {}).get("label") or target

    if not online and policy == "skip":
        await db.finish_automation_run(
            run_id, status="skipped",
            reply_excerpt=f"Target device '{target_label}' offline; run skipped.",
            session_id=None)
        await _on_skip(db, row, table, "target device offline")
        logger.info("Automation %s skipped: target device %s offline",
                    row["id"], target_label)
        return {"ok": False, "skipped": "target offline",
                "device": target_label, "dispatched": False}

    target_instance = (dev or {}).get("instance_id") or target
    job_id = await _dispatch.enqueue(
        owner_user_id=user_id,
        prompt=row.get("prompt") or "",
        agent_id=agent_id,
        target_instance=target_instance,
        target_label=target_label,
        payload={"automation_id": row["id"], "kind": kind,
                 "source": "automation", "execution_mode": "auto"},
    )
    await db.finish_automation_run(
        run_id, status="dispatched",
        reply_excerpt=f"Handed to device '{target_label}' (job {job_id[:8]}).",
        session_id=None)
    await _on_success(db, row, table, None, None)
    logger.info("Automation %s dispatched to device %s (job %s)",
                row["id"], target_label, job_id[:8])
    return {"ok": True, "dispatched": True, "device": target_label,
            "job_id": job_id, "session_id": None}


# ── the single execution entry point ─────────────────────────────────────────

async def run_one(
    db, row: Dict[str, Any], *, kind: str = "schedule",
    current_session_id: Optional[str] = None,
    extra_overlays: Optional[List[str]] = None,
    deliver: bool = True,
) -> Dict[str, Any]:
    """Run one automation/subscription row end-to-end.

    Used directly by the scheduler. The event runtime reuses the building blocks
    (resolve_runner / run_turn / delivery) plus its own dedup, but may also call
    this for the non-event-specific parts.
    """
    table = "subscription" if kind == "event" else "automation"
    row_id = row["id"]
    agent_id = row["agent_id"]
    user_id = row["owner_user_id"]
    label = row.get("task_label") or ("Event trigger" if kind == "event" else "Scheduled task")

    # Re-evaluate the live tier at consumption time. A published assignment can
    # change after this row was created; stale scheduled work must not keep
    # running merely because it already exists in storage.
    try:
        from app.automation.entitlements import enforce_automation_entitlement
        await enforce_automation_entitlement(db, user_id)
    except Exception as exc:
        code = getattr(exc, "code", "automations_not_allowed")
        await _update_row(
            db, row_id, table, last_status="skipped",
            last_error=code,
        )
        return {"ok": False, "skipped": code}

    # Ability gate (mirror legacy execute_automation behavior).
    try:
        from app.admin.integrations import is_ability_enabled_for_agent
        if not await is_ability_enabled_for_agent(agent_id, "automation"):
            await _update_row(db, row_id, table, last_status="skipped",
                              last_error="automation ability disabled")
            return {"ok": False, "error": "automation ability disabled"}
    except Exception:
        pass

    allowed, reason = enforce_guardrails(row)
    if not allowed:
        await _on_skip(db, row, table, reason)
        return {"ok": False, "skipped": reason}

    await _update_row(db, row_id, table, last_status="running", last_error=None)
    run_id = await db.create_automation_run(
        kind=kind, agent_id=agent_id, owner_user_id=user_id,
        automation_id=row_id if table == "automation" else None,
        subscription_id=row_id if table == "subscription" else None,
        run_mode=row.get("run_mode") or "inline",
    )

    # ── Cross-device dispatch ──
    # If this row targets another device, hand the whole run to it rather than
    # running here. (The scheduler is leader-gated so this fires once, and the
    # device claim is exactly-once, so the work lands on exactly one machine.)
    target = (row.get("target_device") or "").strip()
    if target:
        try:
            return await _dispatch_to_device(
                db, row, table=table, run_id=run_id, kind=kind,
                agent_id=agent_id, user_id=user_id, label=label, target=target)
        except Exception as e:
            logger.exception("Cross-device dispatch for %s failed: %s", row_id, e)
            try:
                await db.finish_automation_run(run_id, status="error", error=str(e))
            except Exception:
                pass
            await _on_failure(db, row, table, str(e))
            return {"ok": False, "error": str(e)}

    try:
        spec = delivery.parse_spec(
            row.get("delivery_json"),
            legacy_channel=row.get("channel"),
            legacy_recipient=row.get("channel_recipient"),
            legacy_silent=row.get("silent"),
        )
        resolved = await delivery.resolve_delivery(
            db, user_id=user_id, agent_id=agent_id, spec=spec,
            current_session_id=current_session_id,
        )
        runner = await resolve_runner(db, row, table=table, label=label)
        runner_id = runner["runner_agent_id"]
        session_id = await _make_session(
            db, runner_agent_id=runner_id, user_id=user_id, label=label,
            plan=resolved, source="event" if kind == "event" else "automation",
        )

        reply = await run_turn(
            db, row_id=row_id, table=table, runner_agent_id=runner_id,
            user_id=user_id, session_id=session_id,
            prompt=row.get("prompt") or "",
            memory=row.get("memory") or row.get("memory_json"),
            extra_overlays=extra_overlays,
        )

        outcomes: List[Dict[str, Any]] = []
        if deliver and not resolved["silent"] and resolved["external"]:
            outcomes = await delivery.deliver_result(
                db, user_id=user_id, external=resolved["external"], text=reply or "",
                context={"automation_id": row_id, "agent_id": agent_id, "kind": kind},
            )
        deliv_err = next((o.get("error") for o in outcomes if o.get("status") == "error"), None)

        final_status = "delivered" if outcomes and not deliv_err else (
            "delivery_failed" if deliv_err else "ok")
        await db.finish_automation_run(
            run_id, status=final_status, reply_excerpt=reply or "",
            delivery_json={"external": outcomes}, session_id=session_id,
            runner_agent_id=runner_id if runner_id != agent_id else None,
            error=deliv_err,
        )
        await _on_success(db, row, table, session_id, deliv_err)

        if runner.get("is_ephemeral"):
            try:
                await db.delete_clone_agent(runner_id, session_ids=[session_id])
            except Exception as e:
                logger.warning("Could not reap ephemeral clone %s: %s", runner_id, e)

        # Include what the run actually produced: callers that surface this to
        # an LLM (run_automation_now) need the outcome visible, otherwise the
        # model can't tell the run "worked" and re-fires it in a loop.
        return {"ok": True, "session_id": session_id, "delivery": outcomes,
                "reply_excerpt": (reply or "")[:300]}

    except Exception as e:
        logger.exception("Automation run %s failed: %s", row_id, e)
        try:
            await db.finish_automation_run(run_id, status="error", error=str(e))
        except Exception:
            pass
        await _on_failure(db, row, table, str(e))
        return {"ok": False, "error": str(e)}


# Public aliases so the event runtime can share the same bookkeeping.
record_success = _on_success
record_failure = _on_failure
record_skip = _on_skip

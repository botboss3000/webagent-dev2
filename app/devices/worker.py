"""Per-instance device worker — the loop that makes a shared DB act like a fleet.

Runs on EVERY instance (NOT leader-gated — unlike the scheduler / event runtime,
which run in exactly one elected worker). Each tick it:

  1. Heartbeats this device's presence row (so other devices see it online).
  2. Reclaims any expired-lease jobs (a crashed claimer's work returns to pending).
  3. Atomically claims pending jobs addressed to THIS device (or broadcast), and
     runs each — building a session and running the agent loop locally, so the job
     executes with this machine's own tools / browser / files.

A short poll gives a responsive floor; ``kick()`` wakes the loop immediately when
a job is enqueued locally (or a future remote nudge arrives), so same-box
dispatch is instant rather than poll-latency.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Optional, Tuple

from app.devices import identity

logger = logging.getLogger(__name__)

POLL_INTERVAL = 2.5      # seconds between ticks (responsiveness floor)
HEARTBEAT_EVERY = 15     # seconds between presence heartbeats
LEASE_SECONDS = 600      # a claimed job's lease before it's reclaimable
CLAIM_LIMIT = 4          # max jobs claimed per tick

# On a REMOTE shared DB (Postgres) every tick is several network round-
# trips (reclaim + claim + periodic heartbeat). At the local 2.5s floor that's a
# constant drain on the slow connection that competes with the chat hot path, so
# we poll much less often when remote. Local-DB dispatch stays snappy; remote
# cross-device dispatch still gets an instant `kick()` for locally-enqueued jobs,
# so only jobs that ORIGINATE on another instance wait up to the longer poll.
REMOTE_POLL_INTERVAL = 15.0     # seconds between ticks when DB is remote
REMOTE_HEARTBEAT_EVERY = 45     # seconds between presence heartbeats when remote


def _poll_interval() -> float:
    try:
        from app.db import is_remote_db
        if is_remote_db():
            return REMOTE_POLL_INTERVAL
    except Exception:
        pass
    return POLL_INTERVAL


def _heartbeat_every() -> float:
    try:
        from app.db import is_remote_db
        if is_remote_db():
            return REMOTE_HEARTBEAT_EVERY
    except Exception:
        pass
    return HEARTBEAT_EVERY


def _self_endpoint() -> str:
    """This instance's own reachable base URL, recorded in each heartbeat so the
    Instances page can (a) SHOW where a device lives and (b) MATCH a cloud VM to
    its registry-device twin by public IP — collapsing the two into one tile.

    Uses the app-wide base-URL resolver (request-derived / last-seen host), which
    by the time the worker is heartbeating has been populated by real traffic.
    Returns '' when only the bare localhost:8000 fallback is available, so we
    never stamp a wrong port onto the presence row."""
    try:
        from app.admin.integrations import _get_base_url
        url = (_get_base_url() or "").strip().rstrip("/")
        if url and url != "http://localhost:8000":
            return url
    except Exception:
        pass
    return ""


MAX_JOB_AGE_SECONDS = 86400   # staleness cap: a job that has waited longer than
                              # this (e.g. a 'wait' target that stayed offline for
                              # a day) is skipped rather than fired absurdly late


def _payload_of(job: dict) -> dict:
    """The job's routing payload as a dict (it's stored as a JSON string)."""
    payload = job.get("payload")
    if isinstance(payload, str):
        try:
            payload = json.loads(payload or "{}")
        except Exception:
            payload = {}
    return payload or {}


class DeviceWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._kick = asyncio.Event()
        self._last_heartbeat = 0.0

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="device_worker")
        logger.info("Device worker started (id=%s, label=%s, poll=%ss)",
                    identity.device_id(), identity.device_label(), POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def kick(self) -> None:
        """Wake the loop now (a job was just enqueued for us)."""
        try:
            self._kick.set()
        except Exception:
            pass

    async def _run(self) -> None:
        await asyncio.sleep(3)  # let startup settle
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Device worker tick failed: %s", e)
            # Wait for the next poll OR an immediate kick, whichever comes first.
            try:
                await asyncio.wait_for(self._kick.wait(), timeout=_poll_interval())
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            finally:
                self._kick.clear()

    async def _tick(self) -> None:
        from app.db import get_db
        from app.db.offload import db_offload
        db = get_db()

        # This loop fires on a timer. Its DB round-trips are offloaded to worker
        # threads (db_offload) so they don't periodically freeze the event loop —
        # which would stall any live chat LLM stream. See app/db/offload.py.

        # 1. Heartbeat presence (throttled).
        now = time.monotonic()
        if self._last_heartbeat == 0.0 or (now - self._last_heartbeat) >= _heartbeat_every():
            try:
                endpoint = _self_endpoint()
                caps = await identity.capabilities_async()
                if endpoint:
                    caps["endpoint"] = endpoint    # also carried in caps for the pre-heartbeat self row
                    # HTTPS is always auto-detected (no pre-configured URL support).
                    if endpoint.startswith("https://"):
                        caps["endpoint_https_auto"] = True
                # Publish this device's Remote Access tunnel state so other devices'
                # Instances page can show its tunnel URL / running state and offer a
                # remote Start. Best-effort + purely synchronous (no network).
                try:
                    from app.remote_access.manager import tunnel_snapshot
                    caps["tunnel"] = tunnel_snapshot()
                except Exception:
                    pass
                await db_offload(lambda: db.device_heartbeat(
                    identity.device_id(),
                    label=identity.device_label(),
                    capabilities=caps,
                    endpoint=endpoint or None,
                ))
                # Track this URL in the instance's persistent URL list so the
                # Instances Overview shows every URL the device has ever reported
                # (IP, domain, etc.) — not just the most recent one.
                if endpoint:
                    try:
                        from app.db.instance_meta import track_endpoint_url
                        await db_offload(lambda: track_endpoint_url(
                            identity.device_id(), endpoint,
                            https_auto=endpoint.startswith("https://")
                        ))
                    except Exception:
                        pass
                self._last_heartbeat = now
            except Exception as e:
                logger.debug("device heartbeat failed: %s", e)

        # 2. Reclaim jobs whose claimer crashed mid-run.
        try:
            n = await db_offload(lambda: db.reclaim_expired_device_jobs())
            if n:
                logger.info("Reclaimed %d expired device job(s)", n)
        except Exception as e:
            logger.debug("reclaim failed: %s", e)

        # 3. Claim + run jobs addressed to this device.
        try:
            jobs = await db_offload(lambda: db.claim_device_jobs(
                identity.device_id(), limit=CLAIM_LIMIT, lease_seconds=LEASE_SECONDS))
        except Exception as e:
            err = str(e)
            if "no such table" in err or "does not exist" in err:
                return  # tables not migrated yet — try again next tick
            raise
        for job in jobs:
            asyncio.create_task(self._run_job(job), name=f"device_job_{job['id'][:8]}")

    async def _run_job(self, job: dict) -> None:
        from app.db import get_db
        db = get_db()
        job_id = job["id"]
        session_id = None
        try:
            # Staleness cap — never fire a job that has waited absurdly long
            # (the per-automation 'wait' offline policy holds jobs for an
            # offline target; this stops a day-old one running out of the blue).
            created = job.get("created_at")
            if created:
                try:
                    age = (datetime.now(timezone.utc)
                           - datetime.fromisoformat(str(created))).total_seconds()
                except Exception:
                    age = 0
                if age > MAX_JOB_AGE_SECONDS:
                    await db.finish_device_job(
                        job_id, status="skipped",
                        error=f"stale: waited {int(age)}s (> {MAX_JOB_AGE_SECONDS}s); not run")
                    logger.info("Device job %s skipped (stale: %ds old)", job_id, int(age))
                    return

            # ACTION jobs (fleet control, e.g. start a tunnel / kill switch /
            # restart) carry a payload discriminator and run a small local
            # handler with NO agent — branch BEFORE the agent-loop path, which
            # requires an agent and would error.
            payload = _payload_of(job)
            action = (payload.get("action") or "").strip() if isinstance(payload, dict) else ""
            if action:
                from app.devices.actions import run_device_action
                result = await run_device_action(action, job=job, db=db, payload=payload)
                await db.finish_device_job(
                    job_id, status="done", result_excerpt=(result or "")[:2000])
                logger.info("Device job %s ran action '%s' on %s",
                            job_id, action, identity.device_id())
                return

            # Kill switch: while engaged, no agent-loop work runs on this
            # device. Control ACTIONS above still run (restart,
            # kill_switch_resume, ...) so the fleet can always reach the device;
            # plain "run a prompt" jobs are skipped so nothing silently executes
            # while the switch is on.
            try:
                from app.kill_switch import is_engaged as _ks_engaged
                if _ks_engaged():
                    await db.finish_device_job(
                        job_id, status="skipped",
                        error="kill switch engaged — device jobs suppressed")
                    logger.info("Device job %s skipped (kill switch engaged)", job_id)
                    return
            except Exception:
                pass

            session_id, reply = await self._execute(db, job)
            await db.finish_device_job(
                job_id, status="done", result_excerpt=reply or "", session_id=session_id)
            logger.info("Device job %s completed on %s", job_id, identity.device_id())
        except Exception as e:
            logger.exception("Device job %s failed: %s", job_id, e)
            try:
                await db.finish_device_job(
                    job_id, status="error", error=str(e)[:500], session_id=session_id)
            except Exception:
                pass

    async def _execute(self, db, job: dict) -> Tuple[Optional[str], str]:
        """Run the job's prompt on the target agent locally. Mirrors the minimal
        event-executor path: create a session, post the prompt, run the agent
        loop. Returns (session_id, reply_text)."""
        from app.agent.loop import run_agent_loop_buffered
        from app.agent.prompts import build_system_prompt

        owner_user_id = job.get("owner_user_id")
        agent_id = job.get("agent_id")
        prompt_text = job.get("prompt") or ""

        # User BYOD: this runs in a background task with no request context, so
        # the data-plane router doesn't yet know whose data this is. Pin it to the
        # job OWNER and re-resolve db, so the session + interactions written below
        # (pre-loop) land in the owner's own database rather than the central one.
        # No-op in single-tenant mode (get_db() returns the same backend).
        try:
            from app.auth.identity import set_verified_caller_uid
            from app.db import get_db as _get_db
            if owner_user_id:
                set_verified_caller_uid(owner_user_id)
                db = _get_db()
        except Exception:  # noqa: BLE001
            pass

        # Job payload carries optional routing hints from the sender.
        payload = _payload_of(job)
        # A chat hand-off names the sender's existing session so the reply lands
        # back in that same conversation; an automation/broadcast job has none.
        run_in_session = (payload.get("run_in_session") or "").strip() or None
        execution_mode = payload.get("execution_mode") or "ask"
        # Pasted images/files handed off with the turn. The default loop ignores
        # these (it inlines images into the prompt itself); an alternate engine
        # (e.g. claude_code) reads them off disk via its own tools. Resolved from
        # the shared DB — server/DB-stored attachments come through; a browser-only
        # attachment returns nothing here and the engine reports it unreadable.
        attachment_docs = []
        for _att_id in (payload.get("attachment_ids") or []):
            try:
                _att = await db.get_attachment(_att_id)
                if _att:
                    attachment_docs.append(_att)
            except Exception:
                pass

        agent = await db.get_agent_by_id(agent_id) if agent_id else None
        if not agent:
            raise RuntimeError(f"agent {agent_id!r} not found on this device")

        if run_in_session:
            # Run inside the sender's session. The user turn was already recorded
            # on the sending device and the reply is appended here over the shared
            # DB — so DON'T create a session or re-insert the user turn below.
            session_id = run_in_session
        else:
            session_id = f"dev-{uuid.uuid4().hex[:12]}"
            title = (job.get("target_label") or "Device job")[:60]
            meta = {"source": "device_job",
                    "from_instance": job.get("created_by_instance"),
                    "device": {"id": identity.device_id(), "label": identity.device_label()}}
            # Create the session (raw insert, like the event executor; SQL fallback
            # only on backends that expose a sqlite-style connection).
            try:
                raw = db.get_raw_client()
                raw.table("sessions").insert({
                    "id": session_id,
                    "user_id": owner_user_id,
                    "title": title,
                    "agent_id": agent_id,
                    "metadata": json.dumps(meta),
                }).execute()
            except Exception:
                if not hasattr(db, "_get_conn"):
                    raise
                conn = db._get_conn()
                try:
                    conn.execute(
                        "INSERT INTO sessions (id, user_id, title, agent_id, metadata) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (session_id, owner_user_id, title, agent_id, json.dumps(meta)),
                    )
                    conn.commit()
                finally:
                    conn.close()

        # System prompt with the agent's own resolved prompt slots (best-effort).
        context_docs = []
        try:
            resolved = await db.resolve_prompts(agent_id, user_id=owner_user_id)
            context_docs = [
                {"id": s["slot_name"], "context_type": s["slot_name"],
                 "title": s["slot_name"], "content": s["content"], "tags": []}
                for s in resolved
                if (s.get("content") or "").strip() and s.get("slot_name") != "automation"
            ]
        except Exception:
            context_docs = []
        overlay = [{"id": "_device_job", "content": (
            "[DEVICE JOB] Another of the user's devices handed this task to you to "
            "run with your own local tools. Created by instance: "
            f"{job.get('created_by_instance')}.")}]
        system_prompt = await build_system_prompt(
            overlay + context_docs, brain_context=None,
            user_id=owner_user_id, agent_id=agent_id, session_id=session_id)

        # Record the synthetic user turn so the run appears in the session — but
        # only for jobs that minted their own session. A chat hand-off's user
        # turn already exists in the sender's session (re-inserting = a dupe).
        if not run_in_session:
            try:
                await db.insert_interaction(
                    owner_user_id, session_id, role="user", content=prompt_text,
                    channel="device", sender_id=owner_user_id, receiver_id=agent_id,
                    source="device")
            except Exception:
                pass

        raw_allowed = agent.get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        # Remote Control continuity: a chat hand-off CONTINUES an existing
        # conversation, so the taking-over device must rebuild that conversation's
        # history and feed it to the loop — otherwise the turn runs context-blind
        # (history=None) and the agent has no memory of the chat it was handed. A
        # job that minted its own session (automation/broadcast) has nothing to
        # rebuild and starts fresh. resync_session first re-pulls the transcript
        # from the shared authority so this device doesn't continue from a stale
        # local copy of a session another device has since advanced. The current
        # user turn is excluded from the rebuilt history because the loop appends
        # it as the live user_message (leaving it in would replay it twice).
        history = None
        if run_in_session:
            try:
                if hasattr(db, "resync_session"):
                    await db.resync_session(owner_user_id, session_id)
                from app.agent.session_history import build_openai_history_from_session
                _uid_turn = payload.get("user_interaction_id")
                _exclude = {_uid_turn} if _uid_turn else None
                history = await build_openai_history_from_session(
                    db, owner_user_id, session_id,
                    exclude_interaction_ids=_exclude, agent_id=agent_id)
            except Exception as e:
                logger.warning("device job: history rebuild failed for session %s: %s",
                               session_id, e)
                history = None

        # Remote Control LIVE continuity: the buffered/device path (unlike the live
        # chat path's RunBuffer) never stamps `session_seq` on the rows it writes, so
        # they stay NULL and are INVISIBLE to a VIEWER device's live-reconcile poll
        # (`/session-tail` filters `session_seq IS NOT NULL AND > cursor`). Stamp a
        # monotonic seq on each persisted row here so a handed-off turn surfaces on
        # the watching device without a manual reload — the executor is the single
        # writer, so its ordering is authoritative. Only for chat hand-offs
        # (run_in_session): a broadcast/automation session has no live viewer, and
        # the live chat path already stamps its own.
        _seq = {"next": None}

        async def _stamp_seq(rid) -> None:
            if _seq["next"] is None:
                try:
                    _seq["next"] = await db.next_session_seq(session_id)
                except Exception:
                    _seq["next"] = 1
            ss = _seq["next"]
            _seq["next"] = ss + 1
            try:
                await db.stamp_interaction_seq(rid, ss)
            except Exception:
                pass

        rc_callback = None
        if run_in_session:
            # Stamp the handed-off user turn first (it was pulled in by resync with a
            # NULL seq) so it sorts before the reply on the viewer's poll.
            _uid_turn = payload.get("user_interaction_id")
            if _uid_turn:
                await _stamp_seq(_uid_turn)

            async def rc_callback(event):  # noqa: F811 — assigned, not redefined
                try:
                    if (event.get("type") == "db" and event.get("id")
                            and event.get("role") in ("assistant", "tool", "user")):
                        await _stamp_seq(event.get("id"))
                except Exception:
                    pass

        reply = await run_agent_loop_buffered(
            user_id=owner_user_id,
            session_id=session_id,
            user_message=prompt_text,
            system_prompt=system_prompt,
            agent_id=agent_id,
            history=history,
            channel="device",
            timeout_seconds=600,
            db=db,
            agent_template_id=agent.get("template_id"),
            allowed_tools=raw_allowed or None,
            max_turns=agent.get("max_turn_count", 0),
            execution_mode=execution_mode,
            attachment_docs=attachment_docs or None,
            event_callback=rc_callback,
        )
        return session_id, reply or ""


_WORKER: Optional[DeviceWorker] = None


def get_worker() -> DeviceWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = DeviceWorker()
    return _WORKER


def kick() -> None:
    """Module-level convenience: wake this instance's worker immediately."""
    try:
        get_worker().kick()
    except Exception:
        pass


def refresh_presence() -> None:
    """Force the next worker tick to publish a fresh capability snapshot."""
    try:
        worker = get_worker()
        worker._last_heartbeat = 0.0
        worker.kick()
    except Exception:
        pass


async def start_device_worker() -> None:
    await get_worker().start()


async def stop_device_worker() -> None:
    if _WORKER is not None:
        await _WORKER.stop()

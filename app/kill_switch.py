"""Kill switch — stop every background activity on demand.

One header button silences the app's whole background layer without stopping the
server or the user's own foreground work:

  • every live agent run is hard-cancelled and tagged ``user_stop`` so it is
    never auto-resumed;
  • the background leader (scheduler, event-runtime poller/renewer, drop-in
    ability hooks, boot orphan-resume, watchdog, session-namer sweep,
    output-closer sweep, remote access) is stopped;
  • the per-instance device worker, P2P mirror worker, hybrid sync engine and
    communications plugin polling are stopped.

While engaged, the three "revival / polling" chokepoints are also gated in code
(``runner.resume_one``, ``watchdog._tick``, ``EventPoller._tick``,
``LocalScheduler._tick``) so nothing can quietly restart even if a stray task
survives. Foreground requests are untouched: opening a page, starting a chat, or
continuing a chat still run their own supervised turn through the Run Manager.

The kill switch is MOMENTARY by design: it kills everything right now, but the
engaged state is NOT persisted — a server restart always brings all background
services back up normally. (Legacy versions persisted ``kill_switch_engaged`` to
app-settings.json and re-engaged at boot; any leftover flag from those versions
is cleared at startup, never applied.)
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# In-memory state (mirrors the persisted flag once init() runs at startup).
_engaged: bool = False


def _clear_legacy_persisted_flag() -> None:
    """Remove any leftover ``kill_switch_engaged`` flag from older versions.

    The kill switch is momentary: engagement must never survive a restart. A
    flag written by a legacy build would otherwise re-engage the switch at
    boot and keep background services dead — so if one is present it is
    cleared (never applied), and startup proceeds fully disengaged.
    """
    try:
        from app.admin.settings import _load_app_settings, _save_app_settings
        data = _load_app_settings()
        if data.pop("kill_switch_engaged", None) is not None:
            _save_app_settings(data)
            logger.info("Kill switch: cleared legacy persisted kill_switch_engaged flag")
    except Exception as e:  # noqa: BLE001 — cleanup must never block boot
        logger.debug("Kill switch: legacy flag cleanup skipped: %s", e)


def init() -> None:
    """Boot state: ALWAYS disengaged.

    The kill switch is a momentary "kill everything now" control — it stops
    every background process the moment it is pressed, but a restart must
    bring all services back up normally. No persisted state is honoured; a
    legacy flag from older versions is cleared rather than applied.
    """
    global _engaged
    _engaged = False
    _clear_legacy_persisted_flag()


def is_engaged() -> bool:
    """True while the kill switch is on. Read by the revival/polling gates."""
    return _engaged


def status() -> dict:
    return {"engaged": _engaged}


async def _cancel_all_runs() -> dict:
    """Cancel every live run, interrupt browser-authority turns, and finalise
    queued messages. Returns per-step counts for the debug log."""
    from app.agent.run_manager import get_run_manager
    from app.db import get_db
    rm = get_run_manager()
    db = get_db()
    cancelled = 0
    try:
        cancelled = await rm.cancel_all(db=db)
    except Exception as e:  # noqa: BLE001
        logger.warning("Kill switch: cancel-all runs failed: %s", e)
    # Scouts are sibling one-shots and may still be alive after their main run
    # has left RunManager. Cancel and durably fence every revision as part of
    # the same app-wide authority.
    try:
        from app.agent.run_scout import stop_all as stop_all_scouts
        await stop_all_scouts(db, "user_stop")
    except Exception as e:  # noqa: BLE001
        logger.warning("Kill switch: stop-all Run Scouts failed: %s", e)
    # Browser-authority turns run INLINE in their SSE request (not via the Run
    # Manager) — interrupt them through their per-session events so the browser
    # receives a final session_run:interrupted and clears its local spinner.
    browser = 0
    try:
        from app.api.browser_storage import interrupt_all_browser_turns
        browser = await interrupt_all_browser_turns()
    except Exception as e:  # noqa: BLE001
        logger.debug("Kill switch: browser turn interrupt skipped: %s", e)
    # Any message parked in the session-gate / compaction queue had its turn
    # cancelled before it began — finalize the durable 'queued' rows so they
    # show as stopped turns instead of dangling behind a dead queue entry
    # (mirrors the boot cleanup's queued-flip).
    queued = 0
    try:
        flip = getattr(db, "flip_queued_interactions_to_interrupted", None)
        if flip:
            queued = await flip()
    except Exception as e:  # noqa: BLE001
        logger.debug("Kill switch: queued-row flip skipped: %s", e)
    return {"cancelled_runs": cancelled, "browser_turns": browser, "queued_flipped": queued}


# ── Background-service stoppers / starters ────────────────────────────────────
# Each mirrors one site in app.main.startup(). They are intentionally lazy-imported
# so this module stays importable with no dependency on the event loop.

async def _stop_leader() -> None:
    from app.coordination.leader import get_leader
    await get_leader().stop()


async def _start_leader() -> None:
    from app.coordination.leader import get_leader
    await get_leader().start()


async def _start_device() -> None:
    from app.devices import start_device_worker
    await start_device_worker()


async def _stop_p2p() -> None:
    from app.p2p.worker import stop_worker
    await stop_worker()


async def _start_p2p() -> None:
    from app.p2p.worker import start_worker
    await start_worker()


async def _stop_comms() -> None:
    from app.communications.manager import get_plugin_manager
    await get_plugin_manager().stop_all_polling()


async def _start_comms() -> None:
    from app.communications.manager import get_plugin_manager
    await get_plugin_manager().start_polling_for_offline_plugins()


async def _stop_hybrid(app) -> None:
    engine = getattr(app.state, "hybrid_sync_engine", None) if app is not None else None
    if engine is not None:
        try:
            await engine.stop()
        finally:
            app.state.hybrid_sync_engine = None


async def _start_hybrid(app) -> None:
    if app is None:
        return
    try:
        from app import abilities
        from app.db import get_db
        from app.db.hybrid import hybrid_enabled, HybridBackend
        if not abilities.app_function_enabled("hybrid_sync"):
            return
        db = get_db()
        inner = db
        if not isinstance(inner, HybridBackend):
            inner = getattr(db, "_inner", None)
        if hybrid_enabled() and isinstance(inner, HybridBackend):
            from app.db.sync import SyncEngine
            engine = SyncEngine(inner, pull_enabled=True)
            engine.start()
            app.state.hybrid_sync_engine = engine
            logger.info("Kill switch: hybrid sync engine restarted")
    except Exception as e:  # noqa: BLE001
        logger.warning("Kill switch: hybrid sync restart failed: %s", e)


async def _stop_all_background(app) -> list:
    """Stop every background service (leader-gated singletons, P2P worker,
    comms polling, hybrid sync). The DEVICE WORKER is deliberately NOT stopped:
    it is the fleet's control channel — while engaged it executes nothing
    (app/devices/worker.py skips agent-loop jobs) but it keeps heartbeating
    presence and can still claim control actions (restart, kill_switch_resume),
    without which a remotely-engaged device could never be told to disengage.
    Returns the list of services successfully stopped."""
    stopped = []
    for name, fn in (
        ("leader", _stop_leader),
        ("p2p", _stop_p2p),
        ("comms", _stop_comms),
        ("hybrid", lambda: _stop_hybrid(app)),
    ):
        try:
            await fn()
            stopped.append(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Kill switch: stop %s failed: %s", name, e)
    return stopped


async def _start_all_background(app) -> list:
    started = []
    for name, fn in (
        ("leader", _start_leader),
        ("device_worker", _start_device),
        ("p2p", _start_p2p),
        ("comms", _start_comms),
        ("hybrid", lambda: _start_hybrid(app)),
    ):
        try:
            await fn()
            started.append(name)
        except Exception as e:  # noqa: BLE001
            logger.warning("Kill switch: start %s failed: %s", name, e)
    return started


async def broadcast_to_fleet(engaged: bool, owner_user_id: str) -> int:
    """Ask every OTHER known device to engage (or disengage) its own kill
    switch, via the shared device-dispatch queue.

    Each job is targeted at one device; that device's worker claims it when it
    is online (or when it next comes back online, within the dispatch staleness
    window) and runs the matching local action — no re-broadcast, so the fleet
    cannot loop. Best-effort: a failure here never affects the local kill.
    Returns the number of devices targeted."""
    n = 0
    try:
        from app.devices.dispatch import list_devices, enqueue
        from app.devices.identity import device_id
        me = device_id()
        action = "kill_switch" if engaged else "kill_switch_resume"
        for d in await list_devices(online_within_seconds=3600):
            did = (d or {}).get("instance_id") or ""
            if not did or did == me:
                continue
            try:
                await enqueue(
                    owner_user_id=owner_user_id or "admin",
                    prompt="",
                    target_instance=did,
                    payload={"action": action},
                )
                n += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("Kill switch: fleet broadcast to %s failed: %s", did, e)
        if n:
            logger.info("Kill switch: broadcast %s to %d other device(s)", action, n)
    except Exception as e:  # noqa: BLE001
        logger.warning("Kill switch: fleet broadcast failed: %s", e)
    return n


async def engage(app=None) -> dict:
    """Kill everything background right now: cancel runs, stop services,
    suppress revivals. MOMENTARY — the engaged state lives only in memory and
    is never persisted, so a restart brings every background service back up
    normally."""
    global _engaged
    _engaged = True
    logger.warning("KILL SWITCH ENGAGED — cancelling runs and stopping background services")
    counts = await _cancel_all_runs()
    stopped = await _stop_all_background(app)
    logger.warning(
        "Kill switch engaged — stopped: %s; runs cancelled=%d, browser-authority "
        "turns interrupted=%d, queued messages finalised=%d",
        ", ".join(stopped) if stopped else "(none)",
        counts["cancelled_runs"],
        counts["browser_turns"],
        counts["queued_flipped"],
    )
    return {
        "engaged": True,
        **counts,
        "services_stopped": stopped,
    }


async def disengage(app=None) -> dict:
    """Re-enable background services (registered at boot) after the switch is off."""
    global _engaged
    _engaged = False
    logger.info("Kill switch disengaged — restarting background services")
    started = await _start_all_background(app)
    logger.info(
        "Kill switch disengaged — restarted: %s",
        ", ".join(started) if started else "(none)",
    )
    return {"engaged": False, "services_started": started}

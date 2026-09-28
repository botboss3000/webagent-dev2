"""
Run Manager — the server-side owner of every agent turn.

Why this exists
---------------
Before this, an agent turn was launched as a detached ``asyncio.create_task``
whose handle was never stored. Python is free to garbage-collect a task with no
live reference, which silently cancels it mid-run. Combined with the buffered
chat endpoint (which ran the loop *inline* in the request and died the instant
the client disconnected), that is why turning off a device, closing the browser,
or switching sessions could interrupt a run.

The Run Manager fixes this by holding a **strong, supervised reference** to each
running turn, keyed by ``session_id``. The run's lifetime is therefore tied to
the *server*, not to any HTTP/WebSocket connection. Nothing a client does
(leaving, refreshing, closing, switching devices) can reach it. The only things
that end a run are: it finishes, an explicit interrupt, or a server restart
(handled by orphan cleanup at boot — see ``StorageBackend.cleanup_orphaned_runs``).

Contract
--------
- ``start_run`` takes a zero-arg *factory* that returns the coroutine doing the
  actual turn (context build → agent loop → emit/persist). The manager wraps it,
  records run-state in the DB, and supervises completion.
- ``interrupt`` sets the DB interrupt flag the loop already polls, so the turn
  stops gracefully and persists its partial answer as 'interrupted'.
- ``is_running`` answers "is a run live right now?" from RAM
  (the DB ``session_runs`` table is the cross-process / cold-device source).
"""

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Dict, Optional, Set, Tuple

logger = logging.getLogger(__name__)


class _RunHandle:
    __slots__ = ("session_id", "user_id", "turn_id", "task", "started_at")

    def __init__(self, session_id: str, user_id: str, turn_id: Optional[str]):
        self.session_id = session_id
        self.user_id = user_id
        self.turn_id = turn_id
        self.task: Optional[asyncio.Task] = None
        self.started_at = time.time()


class RunManager:
    """Global registry of supervised, connection-independent agent runs."""

    # How long to let an interrupted run stop gracefully (so it persists its
    # partial answer at a clean boundary) before hard-cancelling its task.
    REPLACE_GRACE_SECONDS = 1.2

    # How long to wait for a hard-cancelled task to actually unwind before
    # giving up. A task stuck in a non-cancellable await (e.g. a blocking
    # sqlite write offloaded to a worker that is itself wedged) will never
    # finish; waiting on it forever kills the watchdog's single sweep loop and
    # the whole self-healing layer. Abandoning the task after this timeout
    # keeps the watchdog alive; the task's own finally still runs whenever its
    # inner await eventually returns.
    HARD_CANCEL_WAIT_SECONDS = 10.0

    def __init__(self) -> None:
        self._runs: Dict[str, _RunHandle] = {}
        self._auxiliary: Dict[str, Set[asyncio.Task]] = {}
        self._auxiliary_owner: Dict[asyncio.Task, Tuple[str, Optional[str]]] = {}
        self._lock = asyncio.Lock()
        self._session_locks: Dict[str, asyncio.Lock] = {}

    def _session_lock(self, session_id: str) -> asyncio.Lock:
        lk = self._session_locks.get(session_id)
        if lk is None:
            lk = asyncio.Lock()
            self._session_locks[session_id] = lk
        return lk

    # ── Queries ──────────────────────────────────────────────────────────

    def is_running(self, session_id: str) -> bool:
        h = self._runs.get(session_id)
        return bool(h and h.task and not h.task.done())

    def active_turn_id(self, session_id: str) -> Optional[str]:
        """Return the live run's turn id for stale-control fencing."""
        h = self._runs.get(session_id)
        if not h or not h.task or h.task.done():
            return None
        return h.turn_id

    def has_auxiliary(self, session_id: str) -> bool:
        return any(not task.done() for task in self._auxiliary.get(session_id, set()))

    def register_auxiliary(
        self, session_id: str, *, turn_id: Optional[str] = None,
        task: Optional[asyncio.Task] = None,
    ) -> bool:
        """Own a detached Manager/Closer one-shot until it exits.

        Blocking Manager checks execute inside the supervised main task and do
        not need a second registration.
        """
        task = task or asyncio.current_task()
        if task is None:
            return False
        main = self._runs.get(session_id)
        if main and main.task is task:
            return False
        prior = self._auxiliary_owner.get(task)
        if prior:
            return prior == (session_id, turn_id)
        self._auxiliary.setdefault(session_id, set()).add(task)
        self._auxiliary_owner[task] = (session_id, turn_id)
        task.add_done_callback(self._on_auxiliary_done)
        return True

    def _on_auxiliary_done(self, task: asyncio.Task) -> None:
        owner = self._auxiliary_owner.pop(task, None)
        if not owner:
            return
        session_id, _ = owner
        tasks = self._auxiliary.get(session_id)
        if tasks is not None:
            tasks.discard(task)
            if not tasks:
                self._auxiliary.pop(session_id, None)

    async def cancel_auxiliary(
        self, session_id: str, *, turn_id: Optional[str] = None,
    ) -> int:
        current = asyncio.current_task()
        targets = []
        for task in list(self._auxiliary.get(session_id, set())):
            owner = self._auxiliary_owner.get(task)
            if task is current or task.done():
                continue
            if turn_id is not None and owner and owner[1] != turn_id:
                continue
            targets.append(task)
        for task in targets:
            task.cancel()
        if targets:
            await asyncio.gather(*targets, return_exceptions=True)
        return len(targets)

    # ── Lifecycle ────────────────────────────────────────────────────────

    async def start_run(
        self,
        *,
        session_id: str,
        user_id: str,
        turn_id: Optional[str],
        run_factory: Callable[[], Awaitable[None]],
        db: Optional[Any] = None,
    ) -> bool:
        """Start a supervised background run for ``session_id``.

        Returns False (without starting anything) if a run is already live for
        this session — a session has at most one active turn at a time.

        ``run_factory`` is a zero-arg callable returning the coroutine that does
        the whole turn. It owns all emit/persist work; the manager only supervises
        its lifetime so it cannot be killed by a disconnecting client.
        """
        async with self._lock:
            existing = self._runs.get(session_id)
            if existing and existing.task and not existing.task.done():
                logger.info("start_run rejected — run already active for session %s", session_id[:12])
                return False

            # Belt-and-braces against a stale interrupt flag: if an interrupt was
            # requested while no run was live (or a previous flag was never
            # consumed), the fresh run would self-cancel at its first poll and
            # answer nothing. Clear any leftover flag before starting. Only reach
            # this when no live run exists, so we can never wipe a real stop
            # request aimed at an active run.
            if db is not None:
                try:
                    await db.clear_interrupt(session_id)
                except Exception:
                    pass

            handle = _RunHandle(session_id, user_id, turn_id)

            async def _supervised() -> None:
                try:
                    await run_factory()
                except asyncio.CancelledError:
                    logger.info("Run for session %s was cancelled", session_id[:12])
                    raise
                except Exception as e:  # noqa: BLE001 — supervisor must not propagate
                    logger.error("Run for session %s failed: %s", session_id[:12], e, exc_info=True)

            # Strong reference held in self._runs — this is the whole point.
            handle.task = asyncio.create_task(_supervised(), name=f"agent-run:{session_id}")
            handle.task.add_done_callback(lambda t, sid=session_id: self._on_done(sid, t))
            self._runs[session_id] = handle
            logger.info("Started supervised run for session %s (turn %s)", session_id[:12], str(turn_id)[:12])
            return True

    async def start_or_replace(
        self,
        *,
        session_id: str,
        user_id: str,
        turn_id: Optional[str],
        db,
        run_factory: Callable[[bool], Awaitable[None]],
    ) -> str:
        """Start a run, OR — if one is already active for this session — force
        interrupt it and start the new one in its place.

        This is the entry point for a user sending a message: a new message
        always interrupts whatever the agent is doing so the agent reads it
        immediately and redoes its last step with the new context (the agent
        itself then decides whether the message means stop / steer / add info).

        ``run_factory(replaced)`` builds the turn coroutine; ``replaced`` is True
        when this turn is replacing an interrupted one (so the executor can tell
        the agent it was interrupted). Returns 'running' or 'replacing'.

        Serialized per session so rapid-fire messages can't start two runs at
        once. We wait (briefly) for the interrupted run to stop so its partial
        answer is persisted and run-state is finalized BEFORE the new turn
        begins — avoiding a race on the single session_runs row.
        """
        async with self._session_lock(session_id):
            replaced = self.is_running(session_id)
            if replaced:
                await self._interrupt_and_wait(session_id, db)
            elif self.has_auxiliary(session_id):
                # A new user turn supersedes a still-running Closer/Manager
                # even when the main turn has already left the registry.
                await self.cancel_auxiliary(session_id)
            # Never acknowledge a replacement that did not actually start. A
            # cancelled task can briefly remain live while it unwinds; previously
            # start_run then returned False, but this method still reported
            # "running", leaving the newly persisted user message with no agent
            # turn behind it.
            if self.is_running(session_id):
                logger.error("Replacement run for %s is still active after cancellation",
                             session_id[:12])
                raise RuntimeError("The previous agent run did not stop; please retry.")
            started = await self.start_run(
                session_id=session_id, user_id=user_id, turn_id=turn_id,
                run_factory=lambda: run_factory(replaced),
                db=db,
            )
            if not started:
                logger.error("Failed to start replacement run for session %s", session_id[:12])
                raise RuntimeError("The agent run could not be started; please retry.")
            return "replacing" if replaced else "running"

    async def _interrupt_and_wait(self, session_id: str, db) -> None:
        """Interrupt the active run and wait for it to fully stop (its finally
        runs run_state_finish + end_turn). Graceful first, then hard-cancel.

        Tags the run 'replaced' so the self-healing layer never auto-resumes it:
        a newer user message has superseded it, and the replacement turn already
        carries the interrupted context forward."""
        handle = self._runs.get(session_id)
        task = handle.task if handle else None
        # Fence the durable parent before awaiting any sibling cleanup. A
        # passing reviewer verdict that races Stop/replacement must never make
        # a stale tool call executable.
        try:
            await db.run_state_set_cause(session_id, "replaced")
        except Exception as e:
            logger.exception("Could not persist replacement cancellation cause for %s: %s",
                             session_id[:12], e)
        try:
            await db.set_interrupt(session_id)
        except Exception as e:
            logger.warning("interrupt: set_interrupt failed for %s: %s", session_id[:12], e)
        # The Run Scout is a sibling task rather than a child of the main run.
        # Stop the exact revision owned by this turn before a replacement
        # extends its durable starter bundle.  A failure here must never delay
        # or prevent the user's new message.
        if handle and handle.turn_id:
            try:
                from app.agent.run_scout import stop_turn
                await stop_turn(db, session_id, handle.turn_id, "replaced")
            except Exception:
                logger.debug("Could not stop replaced Run Scout for %s",
                             session_id[:12], exc_info=True)
            try:
                from app.agent.subagent_contracts import stop_turn_workers
                await stop_turn_workers(db, session_id, handle.turn_id)
            except Exception:
                logger.debug("Could not stop replaced contract workers for %s",
                             session_id[:12], exc_info=True)
        await self.cancel_auxiliary(session_id, turn_id=(handle.turn_id if handle else None))
        await self._record_stop_request(session_id, db, "replaced",
                                        {"source": "new_user_message"})
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self.REPLACE_GRACE_SECONDS)
            except asyncio.TimeoutError:
                logger.info("Run for %s didn't stop in %.1fs — hard-cancelling",
                            session_id[:12], self.REPLACE_GRACE_SECONDS)
                task.cancel()
                try:
                    await asyncio.wait_for(task, timeout=self.HARD_CANCEL_WAIT_SECONDS)
                except (asyncio.TimeoutError, BaseException):
                    logger.warning("Run for %s did not unwind after hard-cancel; abandoning task",
                                   session_id[:12])
            except BaseException as e:
                # A failed/cooperatively-cancelled wait is not proof that the
                # task is gone. Force it down before permitting the replacement.
                logger.warning("Run for %s ended wait unexpectedly (%s); ensuring cancellation",
                               session_id[:12], type(e).__name__)
                if not task.done():
                    task.cancel()
                    try:
                        await asyncio.wait_for(task, timeout=self.HARD_CANCEL_WAIT_SECONDS)
                    except (asyncio.TimeoutError, BaseException):
                        logger.warning("Run for %s did not unwind after hard-cancel; abandoning task",
                                       session_id[:12])
        # Clear the flag so the replacement run doesn't immediately self-interrupt.
        try:
            await db.clear_interrupt(session_id)
        except Exception:
            pass

    def _on_done(self, session_id: str, task: "asyncio.Task") -> None:
        # Only drop if this is still the current handle for the session (a new
        # turn may have replaced it).
        h = self._runs.get(session_id)
        if h and h.task is task:
            self._runs.pop(session_id, None)
        # Evict the per-session lock too — otherwise the map grows one entry per
        # session id ever seen (spawn-/optimizer-/closer-/slash-command sessions
        # each have a unique id), a slow leak on a long-lived server. Only when the
        # lock is free AND no run remains, so we never yank a lock a concurrent
        # start_or_replace is holding (that would let two starters use different
        # lock objects and race the single session_runs row). If it's busy we keep
        # it — same as today, so this can only reduce growth, never regress.
        lk = self._session_locks.get(session_id)
        if lk is not None and not lk.locked() and session_id not in self._runs:
            self._session_locks.pop(session_id, None)

    async def interrupt(self, session_id: str, db, cause: str = "user_stop") -> bool:
        """Request a graceful stop. Sets the DB interrupt flag the loop polls;
        the turn finalizes its partial answer and run-state as 'interrupted'.
        Returns True if a run was live to interrupt.

        ``cause`` records intent so the self-healing layer treats it correctly.
        The default 'user_stop' (the Stop button) is NEVER auto-resumed."""
        was_running = self.is_running(session_id)
        had_auxiliary = self.has_auxiliary(session_id)
        if not was_running and not had_auxiliary:
            # Nothing live to stop — do NOT plant an interrupt flag. A Stop
            # pressed with no active run used to leave the flag set in the DB,
            # and the *next* run's first interrupt poll would then immediately
            # self-cancel before producing any output (the silent "no response"
            # bug). A stop with nothing running is a no-op.
            return False
        handle = self._runs.get(session_id)
        try:
            await db.run_state_set_cause(session_id, cause)
        except Exception as e:
            logger.exception("Could not persist cancellation cause %s for %s: %s",
                             cause, session_id[:12], e)
        if was_running:
            try:
                await db.set_interrupt(session_id)
            except Exception as e:
                logger.warning("interrupt: set_interrupt failed for %s: %s", session_id[:12], e)
        if handle and handle.turn_id:
            try:
                from app.agent.run_scout import stop_turn
                await stop_turn(db, session_id, handle.turn_id, cause)
            except Exception:
                logger.debug("Could not stop Run Scout for %s",
                             session_id[:12], exc_info=True)
            try:
                from app.agent.subagent_contracts import stop_turn_workers
                await stop_turn_workers(db, session_id, handle.turn_id)
            except Exception:
                logger.debug("Could not stop contract workers for %s",
                             session_id[:12], exc_info=True)
        await self._record_stop_request(session_id, db, cause, {"source": "stop_request"})
        await self.cancel_auxiliary(session_id)
        if not was_running:
            # Do not plant an interrupt after stopping a detached Closer; it
            # would poison the next genuine user turn.
            return True
        return True

    async def pause(self, session_id: str, db) -> bool:
        """Gracefully pause a run until an explicit manual resume.

        ``user_pause`` is deliberately outside the runner's auto-resumable
        cause set.  Resuming is therefore an affirmative user action through
        ``manual_resume`` rather than a watchdog or boot-recovery decision.
        """
        return await self.interrupt(session_id, db, cause="user_pause")

    async def hard_stop_session_tree(self, session_id: str, db) -> bool:
        """Immediately stop a user-requested session and every owned helper."""
        session_ids = [session_id]
        try:
            from plugins.abilities.Core.agent_orchestration import agent_orchestration
            pending = [str(session_id)]
            seen = set(pending)
            while pending:
                parent = pending.pop()
                for row in agent_orchestration._spawns_list(parent):
                    child = str(row.get("spawn_session_id") or "")
                    if child and child not in seen:
                        seen.add(child)
                        pending.append(child)
                        session_ids.append(child)
        except Exception:
            logger.debug("Could not enumerate stop descendants for %s", session_id[:12], exc_info=True)
        session_ids = list(dict.fromkeys(str(s) for s in session_ids if s))
        stopped = False
        for sid in session_ids:
            handle = self._runs.get(sid)
            auxiliary = self.has_auxiliary(sid)
            if not (handle and handle.task and not handle.task.done()) and not auxiliary:
                continue
            stopped = True
            try:
                await db.run_state_set_cause(sid, "user_stop")
                await db.set_interrupt(sid)
            except Exception:
                logger.warning("hard stop: could not fence %s", sid[:12], exc_info=True)
            if handle and handle.turn_id:
                try:
                    from app.agent.run_scout import stop_turn
                    await stop_turn(db, sid, handle.turn_id, "user_stop")
                except Exception:
                    logger.debug("hard stop: Run Scout stop failed for %s", sid[:12], exc_info=True)
                try:
                    from app.agent.subagent_contracts import stop_turn_workers
                    await stop_turn_workers(db, sid, handle.turn_id)
                except Exception:
                    logger.debug("hard stop: contract worker stop failed for %s", sid[:12], exc_info=True)
            await self._record_stop_request(sid, db, "user_stop", {"source": "footer_kill_switch"})
            await self.cancel_auxiliary(sid)
        for sid in session_ids:
            await self.cancel(sid)
        return stopped

    async def _record_stop_request(self, session_id: str, db, cause: str, detail: dict) -> None:
        """Persist a human-readable audit event before a run is interrupted.

        ``session_runs.stop_cause`` is the recovery authority; the diagnostics
        row makes the initiating action visible after the process has gone away.
        Neither must prevent an already-requested cancellation from unwinding.
        """
        try:
            row = await db.run_state_get(session_id)
            from app.agent.diagnostics import record_run_lifecycle
            record_run_lifecycle(
                "interrupt_requested", session_id, status="running", stop_cause=cause,
                agent_id=(row or {}).get("agent_id"), user_id=(row or {}).get("user_id"),
                origin=(row or {}).get("origin"), detail=detail,
            )
        except Exception:
            logger.exception("Could not record cancellation audit event for %s", session_id[:12])

    async def cancel_all(self, db) -> int:
        """Hard-cancel every live run and tag each 'user_stop' first so the
        self-healing layer never auto-resumes it. Also tags every not-running
        resumable run (retry-backoff rows and freshly-eligible ones) as
        user_stop so a mid-recovery run cannot be re-ignited by the watchdog /
        boot orphan-resume after the kill switch disengages. Used by the kill
        switch. Returns the number of live runs cancelled."""
        session_ids = [
            sid for sid, h in list(self._runs.items())
            if h.task and not h.task.done()
        ]
        if session_ids:
            logger.info(
                "Kill switch: cancelling %d live run(s): %s",
                len(session_ids),
                ", ".join(str(s)[:12] for s in session_ids),
            )
        for sid in session_ids:
            try:
                await db.run_state_set_cause(sid, "user_stop")
            except Exception as e:  # noqa: BLE001
                logger.warning("cancel_all: tag user_stop failed for %s: %s",
                               sid[:12], e)
            try:
                await db.set_interrupt(sid)
            except Exception as e:  # noqa: BLE001
                logger.warning("cancel_all: set interrupt failed for %s: %s",
                               sid[:12], e)
            handle = self._runs.get(sid)
            if handle and handle.turn_id:
                try:
                    from app.agent.run_scout import stop_turn
                    await stop_turn(db, sid, handle.turn_id, "user_stop")
                except Exception:
                    logger.debug("cancel_all: Run Scout stop failed for %s",
                                 sid[:12], exc_info=True)
                try:
                    from app.agent.subagent_contracts import stop_turn_workers
                    await stop_turn_workers(db, sid, handle.turn_id)
                except Exception:
                    logger.debug("cancel_all: contract worker stop failed for %s",
                                 sid[:12], exc_info=True)
        auxiliary_sessions = list(self._auxiliary)
        for sid in auxiliary_sessions:
            # An auxiliary may be the only remaining activity for a completed
            # main turn, so fence its durable row before cancellation.
            if sid not in session_ids:
                try:
                    await db.run_state_set_cause(sid, "user_stop")
                except Exception as e:  # noqa: BLE001
                    logger.warning("cancel_all: auxiliary fence failed for %s: %s",
                                   sid[:12], e)
            await self.cancel_auxiliary(sid)
        cancelled = 0
        for sid in session_ids:
            if await self.cancel(sid):
                cancelled += 1
        # Resumable-sweep: a run parked in retry-backoff — or one whose backoff
        # gate just elapsed and is awaiting the next watchdog tick — is not a
        # live task, so it wasn't tagged above; tag it now so no retry ladder
        # can fire after the switch is off. Best-effort, never raises.
        try:
            sweep = getattr(db, "run_state_tag_resumable_as_user_stop", None)
            if sweep:
                n = await sweep()
                if n:
                    logger.info(
                        "Kill switch: tagged %d not-running resumable run(s) as "
                        "user_stop (retry ladders killed)", n)
        except Exception as e:  # noqa: BLE001
            logger.warning("cancel_all: resumable-sweep failed: %s", e)
        return cancelled

    async def cancel(self, session_id: str) -> bool:
        """Hard-cancel the live task for a session and wait for it to unwind.
        Used by the liveness watchdog on a FROZEN run (stuck inside an await the
        cooperative interrupt poll can't reach). Returns True if a task was
        cancelled. The caller is expected to have already tagged stop_cause so the
        run is reclassified for resume."""
        handle = self._runs.get(session_id)
        task = handle.task if handle else None
        if task and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=self.HARD_CANCEL_WAIT_SECONDS)
            except (asyncio.TimeoutError, BaseException):
                logger.warning("Hard-cancel of %s did not unwind in %.0fs; abandoning",
                               session_id[:12], self.HARD_CANCEL_WAIT_SECONDS)
            return True
        return False


# ── Module-level singleton ──

_manager: Optional[RunManager] = None


def get_run_manager() -> RunManager:
    global _manager
    if _manager is None:
        _manager = RunManager()
    return _manager

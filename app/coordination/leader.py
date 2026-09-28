"""Background-leader election — run singleton background loops in ONE worker.

Multi-worker (``gunicorn --workers N``) and multi-instance deploys would
otherwise start the scheduler, event runtime, ability pollers, watchdog, remote
access and the boot orphan-resume in EVERY worker — double-firing automations
and re-igniting the same orphaned runs N times (the stacked-scheduler bug noted
in the project memory). This module elects a single leader via a TTL'd lock row
in the shared DB (``StorageBackend.background_leader_acquire``) and runs the
registered singleton services only while this worker holds the lease. If the
leader dies, its lease expires and another worker takes over and starts the
services there.

Backends without the lock methods (Postgres backends that use the LocalBackend
degrade to "always leader", preserving today's single-instance behaviour.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# A live leader renews every CHECK_INTERVAL_SEC; the lease lasts LEASE_TTL_SEC.
# TTL must be comfortably larger than the interval so a healthy leader always
# renews before expiry (here 3×), while a dead leader's lock frees within ~TTL.
CHECK_INTERVAL_SEC = 10
LEASE_TTL_SEC = 30

# On a REMOTE shared DB every renewal is a network round-trip. Renew far less
# often (still 3× headroom vs TTL) so the heartbeat stops taxing the slow
# connection; the only cost is a dead leader's singletons failing over in ~TTL
# instead of ~30s, which is fine for background work.
REMOTE_CHECK_INTERVAL_SEC = 30
REMOTE_LEASE_TTL_SEC = 90


def _intervals() -> tuple:
    """Return (check_interval, lease_ttl) for the active DB backend."""
    try:
        from app.db import is_remote_db
        if is_remote_db():
            return REMOTE_CHECK_INTERVAL_SEC, REMOTE_LEASE_TTL_SEC
    except Exception:
        pass
    return CHECK_INTERVAL_SEC, LEASE_TTL_SEC

AsyncFn = Callable[[], Awaitable[None]]


class _Service:
    __slots__ = ("name", "start", "stop", "oneshot")

    def __init__(self, name: str, start: AsyncFn, stop: Optional[AsyncFn], oneshot: bool):
        self.name = name
        self.start = start
        self.stop = stop
        self.oneshot = oneshot


class BackgroundLeader:
    """Owns a TTL'd DB lease and starts/stops registered singleton services as
    leadership is gained/lost. Register services BEFORE calling ``start()``;
    they run in registration order when this worker becomes leader."""

    def __init__(self) -> None:
        self._services: List[_Service] = []
        self._task: Optional[asyncio.Task] = None
        self._is_leader = False
        self._stopping = False
        # Unique per worker process across hosts.
        self._holder_id = f"{socket.gethostname()}:{os.getpid()}"

    def register(self, name: str, start: AsyncFn,
                 stop: Optional[AsyncFn] = None, *, oneshot: bool = False) -> None:
        """Register a singleton background service. ``start`` runs when this
        worker becomes leader; ``stop`` (if given) runs when it loses leadership
        or shuts down. ``oneshot=True`` services run once on becoming leader and
        are never stopped (e.g. the boot orphan-resume)."""
        self._services.append(_Service(name, start, stop, oneshot))

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def _acquire(self, lease_ttl: int = LEASE_TTL_SEC) -> bool:
        try:
            from app.db import get_db
            db = get_db()
            fn = getattr(db, "background_leader_acquire", None)
            if fn is None:
                return True  # backend has no lock → behave as the sole leader
            # Offloaded: this poll fires on a timer; running its remote round-trip
            # on the event loop would periodically freeze the loop (and any live
            # LLM stream). See app/db/offload.py.
            from app.db.offload import db_offload
            return bool(await db_offload(lambda: fn(self._holder_id, lease_ttl)))
        except Exception as e:
            logger.warning("background_leader_acquire failed (assuming NOT leader): %s", e)
            return False

    async def _release(self) -> None:
        try:
            from app.db import get_db
            db = get_db()
            fn = getattr(db, "background_leader_release", None)
            if fn is not None:
                from app.db.offload import db_offload
                await db_offload(lambda: fn(self._holder_id))
        except Exception as e:
            logger.debug("background_leader_release failed: %s", e)

    async def _start_services(self) -> None:
        for svc in self._services:
            try:
                await svc.start()
            except Exception as e:
                logger.warning("Leader service '%s' start failed: %s", svc.name, e)

    async def _stop_services(self) -> None:
        for svc in self._services:
            if svc.oneshot or svc.stop is None:
                continue
            try:
                await svc.stop()
            except Exception as e:
                logger.warning("Leader service '%s' stop failed: %s", svc.name, e)

    async def _loop(self) -> None:
        while not self._stopping:
            check_interval, lease_ttl = _intervals()
            leader = await self._acquire(lease_ttl)
            if leader and not self._is_leader:
                self._is_leader = True
                logger.info("Acquired background leadership (%s) — starting singleton services",
                            self._holder_id)
                await self._start_services()
            elif not leader and self._is_leader:
                self._is_leader = False
                logger.warning("Lost background leadership (%s) — stopping singleton services",
                               self._holder_id)
                await self._stop_services()
            try:
                await asyncio.sleep(check_interval)
            except asyncio.CancelledError:
                break

    async def start(self) -> None:
        """Begin the acquire/renew loop. The first iteration runs immediately, so
        services start as soon as leadership is won (no initial delay)."""
        if self._task is not None:
            return
        self._stopping = False
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        """Stop the loop, stop services we run, and release the lease for a clean
        handoff. Safe to call even if ``start()`` failed or was never called."""
        self._stopping = True
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._is_leader:
            await self._stop_services()
            await self._release()
            self._is_leader = False


_leader: Optional[BackgroundLeader] = None


def get_leader() -> BackgroundLeader:
    """Process-wide singleton leader."""
    global _leader
    if _leader is None:
        _leader = BackgroundLeader()
    return _leader

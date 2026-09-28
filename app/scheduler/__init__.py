"""Scheduler factory.

Returns the currently-configured ``SchedulerBackend`` singleton. Provider is
chosen via ``scheduler_config.json`` (see ``app.admin.scheduler_config``).
Default provider is ``local`` (in-process async poll loop).
"""

from __future__ import annotations

import logging
from typing import Optional

from app.scheduler.base import SchedulerBackend

logger = logging.getLogger(__name__)

_scheduler: Optional[SchedulerBackend] = None
_current_provider: Optional[str] = None


def get_scheduler() -> SchedulerBackend:
    global _scheduler, _current_provider
    if _scheduler is None:
        provider = _read_provider()
        _scheduler = _instantiate(provider)
        _current_provider = provider
        logger.info("Scheduler initialised: provider=%s", provider)
    return _scheduler


def reset_scheduler() -> None:
    """Invalidate the cached singleton (used after switching providers)."""
    global _scheduler, _current_provider
    _scheduler = None
    _current_provider = None


def _read_provider() -> str:
    try:
        from app.admin.scheduler_config import get_provider
        return get_provider()
    except Exception as e:
        logger.debug("Falling back to local scheduler (config read failed: %s)", e)
        return "local"


def _instantiate(provider: str) -> SchedulerBackend:
    p = (provider or "local").lower()
    if p == "local":
        from app.scheduler.local import LocalScheduler
        return LocalScheduler()
    # Look up remote provider in the registry.
    from app.scheduler.providers import get_provider_class
    cls = get_provider_class(p)
    if cls is not None:
        try:
            from app.admin.scheduler_config import get_settings_for
            settings = get_settings_for(p)
        except Exception:
            settings = {}
        return cls(settings)  # type: ignore[call-arg]
    logger.warning("Unknown scheduler provider %r — falling back to local", provider)
    from app.scheduler.local import LocalScheduler
    return LocalScheduler()


async def start_scheduler() -> None:
    sched = get_scheduler()
    await sched.start()


async def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        await _scheduler.stop()

"""Event-trigger subsystem.

A general abstraction for "when X happens at a connected service, fire an
agent prompt." Sibling to the cron-based ``app.scheduler``. Each external
service that can emit events is an ``EventSource`` (see ``app.events.base``);
the ``EventSourceManager`` discovers them; the ``router`` turns each
normalized event into one or more agent runs.

Two delivery paths feed the router:
  - **Push**: provider webhooks land at ``POST /api/v1/events/{source}``,
    are verified by the source plugin, normalized, and routed.
  - **Poll**: a background loop calls ``source.poll()`` per enabled
    subscription on a cadence, advances a cursor, and routes any new
    events. A renewal loop re-registers push subscriptions before their
    provider-side TTL expires.
"""

from __future__ import annotations

_MANAGER = None


def get_manager():
    """Return (and lazily build) the singleton ``EventSourceManager``."""
    global _MANAGER
    if _MANAGER is None:
        from app.events.manager import EventSourceManager
        _MANAGER = EventSourceManager()
    return _MANAGER


async def start_event_runtime() -> None:
    """Start poll + renewal loops. Called from ``app.main`` startup."""
    mgr = get_manager()
    await mgr.start_poller()
    await mgr.start_renewer()


async def stop_event_runtime() -> None:
    """Stop poll + renewal loops. Called from ``app.main`` shutdown."""
    if _MANAGER is None:
        return
    await _MANAGER.stop_poller()
    await _MANAGER.stop_renewer()

"""Background liveness heartbeat for alternate engine runs.

The default LLM loop advances session_runs.heartbeat_at via _beat() /
_stream_heartbeat() inside stream_agent_events (app/agent/loop.py).
Alternate-engine dispatch (plugins/engines) hands the whole turn to an
adapter that yields that same event vocabulary, but the adapter's own
child-process-wait loop never reaches _beat() — so runs lasting longer
than the frozen-watchdog threshold (~360 s) are SYSTEMATICALLY declared
frozen and killed. See app/agent/watchdog.py + app/agent/runner.py.

Import this module from an engine's stream() and call ``start_heartbeat()``
just before the main event-drain loop. Cancel the returned Task in your
cleanup path (finally block). The task is a fire-and-forget daemon — it
recovers from any db error and stops cleanly on cancellation.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)

# Same cadence as app/agent/loop.py:_HEARTBEAT_INTERVAL.
_HEARTBEAT_SECS = 5.0


async def _beat_loop(db, session_id: str) -> None:
    """Run forever, advancing session_runs.heartbeat_at every _HEARTBEAT_SECS.
    Cancelled by the caller via asyncio.Task cancellation."""
    while True:
        await asyncio.sleep(_HEARTBEAT_SECS)
        try:
            await db.run_state_heartbeat(session_id)
        except Exception:
            logger.debug(
                "engine heartbeat failed for %s", session_id, exc_info=True,
            )


def start_heartbeat(db, session_id: str) -> "asyncio.Task":
    """Spawn a background asyncio.Task that bumps session_runs.heartbeat_at
    every 5 s for the lifetime of the task.

    The caller MUST cancel this task when the engine subprocess finishes
    (otherwise it runs forever). Returns the Task — cancel it in a finally
    block."""
    return asyncio.ensure_future(_beat_loop(db, session_id))

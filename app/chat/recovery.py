"""Recover durable work without repeating execution with unknown outcomes."""
import asyncio
import contextlib
import logging
from app.db import get_app_db
from .repository import ChatRepository

logger = logging.getLogger(__name__)

async def recover_once(*, startup=False):
    """Recover controlled-chat work.

    On process startup every prior in-memory worker is gone. Fence V2 leases
    and fail consumed work whose external effects cannot be proven safe to replay.
    Periodic scans apply the same V2-only lease policy.
    """
    from .runtime import schedule
    accounts = await get_app_db().list_user_accounts()
    for account in accounts:
        owner = account["user_id"]
        repo = ChatRepository(owner)
        if startup:
            recovered = await repo.recover_orphaned()
            if recovered:
                logger.info("Boot recovery fenced %d controlled runs for owner %s",
                            len(recovered), owner)
        else:
            await repo.recover_expired()
        resumed = await repo.resume_interrupted()
        if resumed:
            logger.info("Automatically resumed %d interrupted controlled chats for owner %s",
                        len(resumed), owner)
        for run_id in repo.pending_runs():
            schedule(owner,run_id)

async def supervise():
    while True:
        try:
            await recover_once()
        except Exception:
            logger.exception("Controlled chat recovery scan failed")
        await asyncio.sleep(10)

async def shutdown(task):
    from .runtime import _tasks
    from . import runtime
    runtime._shutting_down = True
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    running = list(_tasks.values())
    for work in running:
        work.cancel()
    await asyncio.gather(*running,return_exceptions=True)

"""Recover durable work without repeating execution with unknown outcomes."""
import asyncio
import contextlib
import logging
from app.db import get_app_db
from .repository import ChatRepository

logger = logging.getLogger(__name__)

async def recover_once(*, startup=False):
    """Recover controlled-chat work.

    On process startup every prior in-memory worker is gone, so fence both the
    legacy ``session_runs`` lease and the matching V2 lease before scheduling.
    Periodic scans keep the stricter uncertain-outcome behavior for ordinary
    lease expiry inside a live process.
    """
    from .runtime import schedule
    from app.db import get_user_db

    accounts = await get_app_db().list_user_accounts()
    for account in accounts:
        owner = account["user_id"]
        repo = ChatRepository(owner)
        if startup:
            user_db = get_user_db(owner)
            marked = await user_db.mark_orphans_for_resume()
            recovered = await repo.recover_orphaned()
            if marked or recovered:
                logger.info(
                    "Boot recovery prepared owner %s: legacy=%d controlled=%d",
                    owner, marked, len(recovered),
                )
        else:
            await repo.recover_expired()
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
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    running = list(_tasks.values())
    for work in running:
        work.cancel()
    await asyncio.gather(*running,return_exceptions=True)

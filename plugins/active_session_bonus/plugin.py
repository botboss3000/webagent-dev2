"""Application-plugin entry point; deleting this package disables all hooks."""
import logging

from .api import router
from .scanner import get_worker

logger = logging.getLogger(__name__)


async def start_background():
    await get_worker().start()
    logger.info("Active Session Bonus reconciliation worker started")

async def stop_background():
    await get_worker().stop()

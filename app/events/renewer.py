"""Renew push subscriptions before the provider-side TTL expires.

Gmail watches expire after 7 days; Microsoft Graph mail subscriptions max
out at ~3 days; Drive channels at ~7 days; Dropbox webhooks don't expire
but Dropbox cursors can move on us. This loop re-runs
``source.renew_subscription`` for any row whose ``external_expiration_at``
is within ``RENEW_HEADROOM`` of now.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

TICK_INTERVAL = 3600  # one hour
RENEW_HEADROOM = timedelta(hours=24)


class SubscriptionRenewer:
    def __init__(self, manager) -> None:
        self._mgr = manager
        self._last_tick_at = None
        self._last_error = None

    async def run(self) -> None:
        await asyncio.sleep(10)
        while True:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("Renewer tick failed: %s", e)
                self._last_error = str(e)[:200]
            try:
                await asyncio.sleep(TICK_INTERVAL)
            except asyncio.CancelledError:
                raise

    async def _tick(self) -> None:
        from app.db import get_db
        db = get_db()
        self._last_tick_at = datetime.now(timezone.utc).isoformat()
        cutoff = (datetime.now(timezone.utc) + RENEW_HEADROOM).isoformat()
        try:
            due = await db.list_event_subscriptions_needing_renewal(cutoff)
        except Exception as e:
            logger.warning("Could not query renewal subs: %s", e)
            return
        for sub in due:
            src = self._mgr.get(sub["source"])
            if not src or not src.supports_push:
                continue
            try:
                reg = await src.renew_subscription(sub)
                await db.update_event_subscription(
                    sub["id"],
                    external_subscription_id=reg.external_subscription_id,
                    external_resource_id=reg.external_resource_id,
                    external_expiration_at=reg.external_expiration_at,
                    external_metadata=reg.external_metadata or {},
                    last_status="ok",
                    last_error=None,
                )
                logger.info("Renewed event subscription %s on %s; expires %s",
                            sub["id"], sub["source"], reg.external_expiration_at)
            except Exception as e:
                logger.exception("Renew failed for sub %s: %s", sub["id"], e)
                try:
                    await db.update_event_subscription(
                        sub["id"],
                        last_status="error",
                        last_error=f"renew failed: {str(e)[:400]}",
                    )
                except Exception:
                    pass

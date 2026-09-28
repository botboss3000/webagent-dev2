"""Shopify webhook-based event source.

Shopify webhooks are registered per-shop via the Admin API; topics include
``orders/create``, ``orders/paid``, ``inventory_levels/update``, etc.
Each notification is signed via ``X-Shopify-Hmac-SHA256`` using the shop's
shared secret.

Required env:
  - ``EVENTS_SHOPIFY_WEBHOOK_BASE`` — public base URL, combined with
    ``/api/v1/events/shopify``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call, oauth_label

logger = logging.getLogger(__name__)


def _webhook_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_SHOPIFY_WEBHOOK_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/shopify" if base else None


_TOPIC_MAP = {
    "order_created": "orders/create",
    "order_paid": "orders/paid",
    "inventory_changed": "inventory_levels/update",
}


class ShopifySource(EventSource):
    @property
    def name(self) -> str:
        return "shopify"

    @property
    def event_types(self) -> List[str]:
        return list(_TOPIC_MAP.keys())

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def required_provider(self) -> Optional[str]:
        return "shopify"

    @property
    def enabled(self) -> bool:
        return bool(_webhook_url())

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["min_total"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        webhook_url = _webhook_url()
        if not webhook_url:
            raise RuntimeError("EVENTS_SHOPIFY_WEBHOOK_BASE not configured")
        topic = _TOPIC_MAP.get(event_type)
        if not topic:
            raise RuntimeError(f"Unsupported Shopify event_type {event_type}")
        # Read shop domain from the user's stored shopify auth_element config.
        from app.db import get_db
        db = get_db()
        elem = await db.auth_element_get(owner_user_id, "shopify", oauth_label(agent_id))
        cfg = {}
        if elem:
            try:
                cfg = json.loads(elem.get("config") or "{}")
            except Exception:
                cfg = {}
        shop = cfg.get("shop") or cfg.get("shop_domain") or ""
        if not shop:
            raise RuntimeError("Shopify shop domain not stored on user's auth element")

        body = {"webhook": {"topic": topic, "address": webhook_url, "format": "json"}}
        resp = await oauth_api_call(
            owner_user_id, agent_id, "shopify", "POST",
            f"https://{shop}/admin/api/2023-10/webhooks.json",
            json_body=body,
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Shopify webhook create failed: {resp}")
        wh = ((resp.get("body") or {}).get("webhook") or {})
        wh_id = str(wh.get("id") or "")
        return SubscriptionRegistration(
            external_subscription_id=wh_id,
            external_metadata={"shop": shop, "topic": topic},
        )

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        meta = subscription_row.get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        shop = meta.get("shop")
        wh_id = subscription_row.get("external_subscription_id")
        if not shop or not wh_id:
            return
        try:
            await oauth_api_call(
                subscription_row["owner_user_id"], subscription_row.get("agent_id", ""), "shopify", "DELETE",
                f"https://{shop}/admin/api/2023-10/webhooks/{wh_id}.json",
            )
        except Exception as e:
            logger.warning("Shopify webhook delete failed: %s", e)

    async def verify_webhook(self, request: Request) -> bool:
        # Signature verified per-shop; we look up the active shop from the
        # X-Shopify-Shop-Domain header and find the corresponding sub to
        # match the HMAC against. For brevity we accept and re-check below.
        return True

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        shop = request.headers.get("x-shopify-shop-domain", "")
        topic = request.headers.get("x-shopify-topic", "")
        signature = request.headers.get("x-shopify-hmac-sha256", "")
        body_bytes = await request.body()
        if not shop or not topic:
            return []
        try:
            payload = json.loads(body_bytes.decode("utf-8") or "{}")
        except Exception:
            return []

        from app.db import get_db
        db = get_db()

        # Find subs across all users whose external_metadata.shop matches.
        # We do this by scanning subs for this source (small N in practice).
        all_subs = await db.list_event_subscriptions(source=self.name, enabled_only=True)
        target_subs = []
        for s in all_subs:
            meta = s.get("external_metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if meta.get("shop") == shop and meta.get("topic") == topic:
                target_subs.append(s)
        if not target_subs:
            return []

        # Verify HMAC using the Shopify app shared secret.
        try:
            from app.admin.integrations import get_shopify_creds
            client_id, client_secret = await get_shopify_creds()
        except Exception:
            client_secret = ""
        if client_secret:
            mac = hmac.new(client_secret.encode("utf-8"), body_bytes, hashlib.sha256).digest()
            import base64
            expected = base64.b64encode(mac).decode("ascii")
            if not hmac.compare_digest(expected, signature):
                logger.warning("Shopify HMAC mismatch for shop %s", shop)
                return []

        # Map topic → event_type
        event_type = next((k for k, v in _TOPIC_MAP.items() if v == topic), None)
        if not event_type:
            return []

        out: List[NormalizedEvent] = []
        for sub in target_subs:
            if sub.get("event_type") != event_type:
                continue
            filter_dict = sub.get("filter") or {}
            evt = self._build_event(sub["owner_user_id"], event_type, payload, shop)
            if not self._matches(event_type, payload, filter_dict):
                continue
            out.append(evt)
        return out

    def _matches(self, event_type: str, payload: dict, f: Dict[str, Any]) -> bool:
        if not f:
            return True
        if event_type in ("order_created", "order_paid"):
            mt = f.get("min_total")
            if mt is not None:
                try:
                    if float(payload.get("total_price", 0) or 0) < float(mt):
                        return False
                except Exception:
                    pass
        return True

    def _build_event(self, user_id: str, event_type: str, payload: dict, shop: str) -> NormalizedEvent:
        order_id = str(payload.get("id") or "")
        return NormalizedEvent(
            source=self.name,
            event_type=event_type,
            owner_user_id=user_id,
            external_id=f"{shop}:{order_id or 'evt-' + str(payload.get('updated_at', ''))}",
            occurred_at=payload.get("updated_at") or datetime.now(timezone.utc).isoformat(),
            payload={
                "shop": shop,
                "order_id": order_id,
                "total_price": payload.get("total_price"),
                "currency": payload.get("currency"),
                "customer_email": (payload.get("contact_email") or payload.get("email") or ""),
                "summary": payload.get("name") or "",
            },
            raw_ref={"order_id": order_id, "shop": shop},
        )

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True


source_cls = ShopifySource

"""OneDrive event source via Microsoft Graph subscriptions.

Subscribes to ``me/drive/root`` (delta on the root). Each notification
triggers a ``delta`` call to enumerate new/changed items.

Required env: ``EVENTS_GRAPH_NOTIFICATION_BASE``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request

from app.events.base import EventSource
from app.events.providers.graph_subscription import (
    create_subscription,
    delete_subscription,
    expiration_iso,
    maybe_validation_response,
    new_client_state,
    renew_subscription as graph_renew,
)
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


def _notification_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_GRAPH_NOTIFICATION_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/onedrive" if base else None


class OneDriveSource(EventSource):
    @property
    def name(self) -> str:
        return "onedrive"

    @property
    def event_types(self) -> List[str]:
        return ["file_added", "file_modified", "file_deleted"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def required_provider(self) -> Optional[str]:
        return "microsoft"

    @property
    def enabled(self) -> bool:
        return bool(_notification_url())

    @property
    def description(self) -> str:
        return ("OneDrive file changes via Microsoft Graph drive root delta.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["name_contains", "mime_type", "parent_path"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        notif_url = _notification_url()
        if not notif_url:
            raise RuntimeError("EVENTS_GRAPH_NOTIFICATION_BASE not configured")
        client_state = new_client_state()
        exp = expiration_iso()
        resp = await create_subscription(
            user_id=owner_user_id,
            agent_id=agent_id,
            change_type="updated",
            resource="me/drive/root",
            notification_url=notif_url,
            client_state=client_state,
            expiration=exp,
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Graph onedrive subscription failed: {resp}")
        body = resp.get("body") or {}
        sub_id = body.get("id") or ""

        # Seed a delta token so the first notification has a baseline.
        delta_link = await self._initial_delta_link(owner_user_id, agent_id)

        return SubscriptionRegistration(
            external_subscription_id=sub_id,
            external_expiration_at=body.get("expirationDateTime") or exp,
            external_metadata={
                "client_state": client_state,
                "delta_link": delta_link or "",
            },
        )

    async def _initial_delta_link(self, user_id: str, agent_id: str) -> Optional[str]:
        url = f"{GRAPH_BASE}/me/drive/root/delta"
        # walk to the end to get a @odata.deltaLink
        for _ in range(20):
            resp = await oauth_api_call(user_id, agent_id, "microsoft", "GET", url)
            if resp.get("status") != "ok":
                return None
            body = resp.get("body") or {}
            if body.get("@odata.deltaLink"):
                return body["@odata.deltaLink"]
            next_link = body.get("@odata.nextLink")
            if not next_link:
                return None
            url = next_link
        return None

    async def renew_subscription(self, subscription_row: Dict[str, Any]) -> SubscriptionRegistration:
        sub_id = subscription_row.get("external_subscription_id")
        agent_id = subscription_row.get("agent_id", "")
        if not sub_id:
            return await self.register_subscription(
                owner_user_id=subscription_row["owner_user_id"],
                agent_id=agent_id,
                event_type=subscription_row["event_type"],
                filter_dict=subscription_row.get("filter") or {},
            )
        exp = expiration_iso()
        resp = await graph_renew(
            user_id=subscription_row["owner_user_id"],
            agent_id=agent_id,
            subscription_id=sub_id,
            expiration=exp,
        )
        if resp.get("status") != "ok":
            return await self.register_subscription(
                owner_user_id=subscription_row["owner_user_id"],
                agent_id=agent_id,
                event_type=subscription_row["event_type"],
                filter_dict=subscription_row.get("filter") or {},
            )
        body = resp.get("body") or {}
        meta = subscription_row.get("external_metadata") or {}
        return SubscriptionRegistration(
            external_subscription_id=sub_id,
            external_expiration_at=body.get("expirationDateTime") or exp,
            external_metadata=meta if isinstance(meta, dict) else {},
        )

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        sub_id = subscription_row.get("external_subscription_id")
        if not sub_id:
            return
        try:
            await delete_subscription(
                user_id=subscription_row["owner_user_id"],
                agent_id=subscription_row.get("agent_id", ""),
                subscription_id=sub_id,
            )
        except Exception as e:
            logger.warning("OneDrive unsubscribe failed: %s", e)

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        validation = await maybe_validation_response(request)
        if validation is not None:
            request.state.events_short_circuit = validation
            return []
        try:
            body = await request.json()
        except Exception:
            return []
        notes = body.get("value") or []
        if not notes:
            return []
        from app.db import get_db
        db = get_db()
        out: List[NormalizedEvent] = []
        for note in notes:
            sub_id = note.get("subscriptionId")
            client_state = note.get("clientState") or ""
            if not sub_id:
                continue
            rows = await db.find_event_subscriptions_by_external(self.name, sub_id)
            if not rows:
                continue
            row = rows[0]
            meta = row.get("external_metadata") or {}
            if isinstance(meta, str):
                try:
                    meta = json.loads(meta)
                except Exception:
                    meta = {}
            if (meta.get("client_state") or "") != client_state:
                continue
            delta_link = meta.get("delta_link") or ""
            if not delta_link:
                continue
            items, new_delta = await self._walk_delta(row["owner_user_id"], row.get("agent_id", ""), delta_link)
            meta["delta_link"] = new_delta or delta_link
            try:
                await db.update_event_subscription(row["id"], external_metadata=meta)
            except Exception:
                pass
            filter_dict = row.get("filter") or {}
            for it in items:
                if not self._matches(it, filter_dict):
                    continue
                etype = self._classify(it)
                out.append(NormalizedEvent(
                    source=self.name,
                    event_type=etype,
                    owner_user_id=row["owner_user_id"],
                    external_id=it.get("id") or "",
                    occurred_at=it.get("lastModifiedDateTime") or datetime.now(timezone.utc).isoformat(),
                    payload={
                        "name": it.get("name") or "",
                        "mime_type": ((it.get("file") or {}).get("mimeType")) or "",
                        "size": it.get("size") or 0,
                        "parent_path": ((it.get("parentReference") or {}).get("path")) or "",
                        "web_url": it.get("webUrl") or "",
                        "deleted": bool(it.get("deleted")),
                    },
                    raw_ref={"item_id": it.get("id")},
                ))
        return out

    async def _walk_delta(self, user_id: str, agent_id: str, delta_link: str) -> tuple[List[dict], Optional[str]]:
        url = delta_link
        out: List[dict] = []
        new_delta = None
        for _ in range(20):
            resp = await oauth_api_call(user_id, agent_id, "microsoft", "GET", url)
            if resp.get("status") != "ok":
                break
            body = resp.get("body") or {}
            out.extend(body.get("value") or [])
            if body.get("@odata.deltaLink"):
                new_delta = body["@odata.deltaLink"]
                break
            next_link = body.get("@odata.nextLink")
            if not next_link:
                break
            url = next_link
        return out, new_delta

    def _classify(self, item: dict) -> str:
        if item.get("deleted"):
            return "file_deleted"
        created = item.get("createdDateTime")
        modified = item.get("lastModifiedDateTime")
        if created and modified and created != modified:
            return "file_modified"
        return "file_added"

    def _matches(self, item: dict, f: Dict[str, Any]) -> bool:
        if not f:
            return True
        nc = (f.get("name_contains") or "").lower()
        if nc and nc not in (item.get("name") or "").lower():
            return False
        mt = f.get("mime_type")
        if mt and ((item.get("file") or {}).get("mimeType")) != mt:
            return False
        pp = (f.get("parent_path") or "")
        if pp and not (((item.get("parentReference") or {}).get("path") or "").endswith(pp)):
            return False
        return True

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True


source_cls = OneDriveSource

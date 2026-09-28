"""Google Drive change watcher.

Uses ``changes.watch`` to subscribe to all changes on the user's drive.
Notifications come as Calendar-style channel POSTs (header-only). On
notification we call ``changes.list`` with the saved pageToken to
enumerate what changed, emit one event per added/modified file.

Required environment:
  - ``EVENTS_GDRIVE_NOTIFICATION_BASE`` — public base URL Drive will POST
    to, combined with ``/api/v1/events/google_drive``.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import uuid as _uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)

DRIVE_BASE = "https://www.googleapis.com/drive/v3"


def _notification_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_GDRIVE_NOTIFICATION_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/google_drive" if base else None


class GoogleDriveSource(EventSource):
    @property
    def name(self) -> str:
        return "google_drive"

    @property
    def event_types(self) -> List[str]:
        return ["file_added", "file_modified", "file_deleted"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def required_provider(self) -> Optional[str]:
        return "google"

    @property
    def enabled(self) -> bool:
        return bool(_notification_url())

    @property
    def description(self) -> str:
        return ("Google Drive file changes. Filter by parent folder id, "
                "name substring, or mime type.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["parent_id", "name_contains", "mime_type"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        notif_url = _notification_url()
        if not notif_url:
            raise RuntimeError("EVENTS_GDRIVE_NOTIFICATION_BASE not configured")

        # Get a startPageToken to begin watching from.
        resp = await oauth_api_call(
            owner_user_id, agent_id, "google", "GET",
            f"{DRIVE_BASE}/changes/startPageToken",
        )
        if resp.get("status") != "ok":
            raise RuntimeError(f"Drive startPageToken failed: {resp}")
        start_token = (resp.get("body") or {}).get("startPageToken")
        if not start_token:
            raise RuntimeError("Drive returned no startPageToken")

        channel_id = str(_uuid.uuid4())
        token = secrets.token_urlsafe(16)
        ttl_seconds = 7 * 24 * 3600 - 60
        body = {
            "id": channel_id,
            "type": "web_hook",
            "address": notif_url,
            "token": token,
            "params": {"ttl": str(ttl_seconds)},
        }
        watch = await oauth_api_call(
            owner_user_id, agent_id, "google", "POST",
            f"{DRIVE_BASE}/changes/watch",
            params={"pageToken": start_token},
            json_body=body,
        )
        if watch.get("status") != "ok":
            raise RuntimeError(f"Drive changes.watch failed: {watch}")
        wbody = watch.get("body") or {}
        exp_ms = wbody.get("expiration")
        try:
            exp_iso = datetime.fromtimestamp(int(exp_ms) / 1000.0, timezone.utc).isoformat() if exp_ms else None
        except Exception:
            exp_iso = None
        return SubscriptionRegistration(
            external_subscription_id=channel_id,
            external_resource_id=wbody.get("resourceId"),
            external_expiration_at=exp_iso,
            external_metadata={
                "channel_token": token,
                "page_token": start_token,
            },
        )

    async def renew_subscription(self, subscription_row: Dict[str, Any]) -> SubscriptionRegistration:
        try:
            await self.unregister_subscription(subscription_row)
        except Exception:
            pass
        return await self.register_subscription(
            owner_user_id=subscription_row["owner_user_id"],
            agent_id=subscription_row.get("agent_id", ""),
            event_type=subscription_row["event_type"],
            filter_dict=subscription_row.get("filter") or {},
        )

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        channel_id = subscription_row.get("external_subscription_id")
        resource_id = subscription_row.get("external_resource_id")
        if not channel_id or not resource_id:
            return
        try:
            await oauth_api_call(
                subscription_row["owner_user_id"], subscription_row.get("agent_id", ""), "google", "POST",
                f"{DRIVE_BASE}/channels/stop",
                json_body={"id": channel_id, "resourceId": resource_id},
            )
        except Exception as e:
            logger.warning("Drive channel stop failed: %s", e)

    async def verify_webhook(self, request: Request) -> bool:
        channel_id = request.headers.get("x-goog-channel-id") or ""
        token = request.headers.get("x-goog-channel-token") or ""
        if not channel_id:
            return False
        from app.db import get_db
        db = get_db()
        rows = await db.find_event_subscriptions_by_external(self.name, channel_id)
        if not rows:
            return False
        meta = rows[0].get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        return (meta.get("channel_token") or "") == token

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        channel_id = request.headers.get("x-goog-channel-id") or ""
        state = (request.headers.get("x-goog-resource-state") or "").lower()
        if state == "sync":
            return []
        from app.db import get_db
        db = get_db()
        rows = await db.find_event_subscriptions_by_external(self.name, channel_id)
        if not rows:
            return []
        sub = rows[0]
        meta = sub.get("external_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        page_token = meta.get("page_token")
        if not page_token:
            return []
        user_id = sub["owner_user_id"]
        agent_id = sub.get("agent_id", "")

        changes, new_token = await self._list_changes(user_id, agent_id, page_token)
        if new_token and new_token != page_token:
            meta["page_token"] = new_token
            try:
                await db.update_event_subscription(sub["id"], external_metadata=meta)
            except Exception:
                pass

        filter_dict = sub.get("filter") or {}
        out: List[NormalizedEvent] = []
        for ch in changes:
            f = ch.get("file") or {}
            removed = bool(ch.get("removed"))
            if not removed and not self._matches(f, filter_dict):
                continue
            etype = "file_deleted" if removed else ("file_modified" if f.get("modifiedTime") and f.get("modifiedTime") != f.get("createdTime") else "file_added")
            out.append(NormalizedEvent(
                source=self.name,
                event_type=etype,
                owner_user_id=user_id,
                external_id=ch.get("fileId") or f.get("id") or "",
                occurred_at=ch.get("time") or datetime.now(timezone.utc).isoformat(),
                payload={
                    "name": f.get("name") or "",
                    "mime_type": f.get("mimeType") or "",
                    "parents": f.get("parents") or [],
                    "web_view_link": f.get("webViewLink") or "",
                    "size": f.get("size") or "",
                    "removed": removed,
                },
                raw_ref={"file_id": ch.get("fileId") or f.get("id")},
            ))
        return out

    async def _list_changes(self, user_id: str, agent_id: str, page_token: str) -> tuple[List[dict], Optional[str]]:
        params = {
            "pageToken": page_token,
            "fields": "newStartPageToken,nextPageToken,changes(fileId,removed,time,file(id,name,mimeType,parents,webViewLink,size,createdTime,modifiedTime))",
            "includeRemoved": "true",
            "pageSize": 100,
        }
        out: List[dict] = []
        new_token = page_token
        for _ in range(10):
            resp = await oauth_api_call(
                user_id, agent_id, "google", "GET",
                f"{DRIVE_BASE}/changes",
                params=params,
            )
            if resp.get("status") != "ok":
                break
            body = resp.get("body") or {}
            out.extend(body.get("changes") or [])
            next_page = body.get("nextPageToken")
            if next_page:
                params["pageToken"] = next_page
                continue
            new_token = body.get("newStartPageToken") or new_token
            break
        return out, new_token

    def _matches(self, f: dict, filt: Dict[str, Any]) -> bool:
        if not filt:
            return True
        parent = filt.get("parent_id")
        if parent and parent not in (f.get("parents") or []):
            return False
        name_contains = (filt.get("name_contains") or "").lower()
        if name_contains and name_contains not in (f.get("name") or "").lower():
            return False
        mt = filt.get("mime_type")
        if mt and (f.get("mimeType") or "") != mt:
            return False
        return True

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True


source_cls = GoogleDriveSource

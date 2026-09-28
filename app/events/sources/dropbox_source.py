"""Dropbox event source.

Dropbox push:
  - One webhook URL receives notifications for *all* users.
  - GET requests with ``?challenge=...`` are an app-level verification —
    we echo the challenge as plaintext.
  - POST requests contain a JSON body listing the account ids that have
    changes; per-account we call ``/2/files/list_folder/continue`` (or
    ``list_folder`` on first run) with a saved cursor to enumerate
    changes, then emit normalized events.
  - Dropbox webhooks don't expire, but cursors do drift if the user
    revokes access — the renewer will simply re-establish the cursor.

Required env:
  - ``EVENTS_DROPBOX_WEBHOOK_BASE`` — public base URL Dropbox calls,
    combined with ``/api/v1/events/dropbox``.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import Request
from fastapi.responses import PlainTextResponse

from app.events.base import EventSource
from app.events.types import NormalizedEvent, SubscriptionRegistration
from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)


def _webhook_url() -> Optional[str]:
    base = (os.environ.get("EVENTS_DROPBOX_WEBHOOK_BASE", "") or "").rstrip("/")
    return f"{base}/api/v1/events/dropbox" if base else None


class DropboxSource(EventSource):
    @property
    def name(self) -> str:
        return "dropbox"

    @property
    def event_types(self) -> List[str]:
        return ["file_added", "file_modified", "file_deleted"]

    @property
    def supports_push(self) -> bool:
        return True

    @property
    def required_provider(self) -> Optional[str]:
        return "dropbox"

    @property
    def enabled(self) -> bool:
        return bool(_webhook_url())

    @property
    def description(self) -> str:
        return ("Dropbox file changes via the shared Dropbox webhook + per-user cursor.")

    def parser_schema(self) -> Dict[str, Any]:
        base = super().parser_schema()
        base["filter_fields"] = ["path_prefix", "name_contains"]
        return base

    async def register_subscription(
        self, *, owner_user_id: str, agent_id: str, event_type: str, filter_dict: Dict[str, Any]
    ) -> SubscriptionRegistration:
        # Establish baseline cursor for this user's root (or a path_prefix).
        path = filter_dict.get("path_prefix") or ""
        body = {"path": path, "recursive": True, "include_deleted": True}
        resp = await oauth_api_call(
            owner_user_id, agent_id, "dropbox", "POST",
            "https://api.dropboxapi.com/2/files/list_folder/get_latest_cursor",
            json_body=body,
        )
        cursor = ""
        if resp.get("status") == "ok":
            cursor = (resp.get("body") or {}).get("cursor", "")

        # Capture the user's Dropbox account_id so we can map webhook deliveries.
        account_id = ""
        try:
            me = await oauth_api_call(
                owner_user_id, agent_id, "dropbox", "POST",
                "https://api.dropboxapi.com/2/users/get_current_account",
                json_body=None,
            )
            if me.get("status") == "ok":
                account_id = ((me.get("body") or {}).get("account_id") or "")
        except Exception:
            pass

        return SubscriptionRegistration(
            external_subscription_id=account_id or owner_user_id,
            external_metadata={"cursor": cursor, "account_id": account_id, "path": path},
        )

    async def unregister_subscription(self, subscription_row: Dict[str, Any]) -> None:
        # Dropbox has no per-subscription cancel; webhook is app-level.
        return None

    async def verify_webhook(self, request: Request) -> bool:
        if request.method == "GET":
            challenge = request.query_params.get("challenge") or ""
            request.state.events_short_circuit = PlainTextResponse(content=challenge, status_code=200)
            return True
        # POST: Dropbox signs with HMAC-SHA256 using the app secret in the
        # ``X-Dropbox-Signature`` header. If creds are available, verify;
        # otherwise pass (intake still requires a known account_id).
        try:
            from app.admin.integrations import get_dropbox_creds
            client_id, client_secret = await get_dropbox_creds()
        except Exception:
            client_secret = ""
        if not client_secret:
            return True
        signature = request.headers.get("x-dropbox-signature", "")
        body = await request.body()
        try:
            import hashlib, hmac
            mac = hmac.new(client_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
            return hmac.compare_digest(mac, signature)
        except Exception:
            return False

    async def handle_webhook(self, request: Request) -> List[NormalizedEvent]:
        if request.method == "GET":
            # Short-circuit response already set in verify_webhook.
            return []
        try:
            body = await request.json()
        except Exception:
            return []
        account_ids = ((body.get("list_folder") or {}).get("accounts")) or []
        if not account_ids:
            return []
        from app.db import get_db
        db = get_db()
        out: List[NormalizedEvent] = []
        for acct in account_ids:
            rows = await db.find_event_subscriptions_by_external(self.name, acct)
            for row in rows:
                meta = row.get("external_metadata") or {}
                if isinstance(meta, str):
                    try:
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                cursor = meta.get("cursor") or ""
                entries, new_cursor = await self._list_continue(row["owner_user_id"], row.get("agent_id", ""), cursor)
                if new_cursor and new_cursor != cursor:
                    meta["cursor"] = new_cursor
                    try:
                        await db.update_event_subscription(row["id"], external_metadata=meta)
                    except Exception:
                        pass
                filter_dict = row.get("filter") or {}
                for e in entries:
                    if not self._matches(e, filter_dict):
                        continue
                    out.append(self._build_event(row["owner_user_id"], e))
        return out

    async def _list_continue(self, user_id: str, agent_id: str, cursor: str) -> tuple[List[dict], Optional[str]]:
        if not cursor:
            return [], cursor
        entries: List[dict] = []
        new_cursor = cursor
        for _ in range(20):
            resp = await oauth_api_call(
                user_id, agent_id, "dropbox", "POST",
                "https://api.dropboxapi.com/2/files/list_folder/continue",
                json_body={"cursor": new_cursor},
            )
            if resp.get("status") != "ok":
                break
            body = resp.get("body") or {}
            entries.extend(body.get("entries") or [])
            new_cursor = body.get("cursor") or new_cursor
            if not body.get("has_more"):
                break
        return entries, new_cursor

    def _matches(self, e: dict, f: Dict[str, Any]) -> bool:
        if not f:
            return True
        prefix = (f.get("path_prefix") or "").lower()
        if prefix and not (e.get("path_lower") or "").startswith(prefix):
            return False
        nc = (f.get("name_contains") or "").lower()
        if nc and nc not in (e.get("name") or "").lower():
            return False
        return True

    def _build_event(self, user_id: str, entry: dict) -> NormalizedEvent:
        tag = entry.get(".tag", "")
        if tag == "deleted":
            etype = "file_deleted"
        else:
            etype = "file_added" if entry.get("client_modified") == entry.get("server_modified") else "file_modified"
        return NormalizedEvent(
            source=self.name,
            event_type=etype,
            owner_user_id=user_id,
            external_id=entry.get("id") or entry.get("path_lower") or "",
            occurred_at=entry.get("server_modified") or datetime.now(timezone.utc).isoformat(),
            payload={
                "name": entry.get("name") or "",
                "path": entry.get("path_display") or entry.get("path_lower") or "",
                "size": entry.get("size") or 0,
                "rev": entry.get("rev") or "",
                "tag": tag,
            },
            raw_ref={"path": entry.get("path_lower") or entry.get("id")},
        )

    def matches_filter(self, event, filter_dict: Dict[str, Any]) -> bool:
        return True


source_cls = DropboxSource

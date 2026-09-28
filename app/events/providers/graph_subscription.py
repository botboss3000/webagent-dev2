"""Microsoft Graph subscription helpers.

Graph subscriptions:
  - POST   /v1.0/subscriptions          → create
  - PATCH  /v1.0/subscriptions/{id}     → renew (extend expirationDateTime)
  - DELETE /v1.0/subscriptions/{id}     → cancel
  - Notifications POSTed to notificationUrl with a ``validationToken`` query
    param for the initial handshake (must echo within 10 seconds).

TTL caps (subject to Microsoft's documented limits):
  - mail / calendar    : ~4230 minutes (~3 days)
  - drive              : ~4230 minutes
We renew well before the limit via the renewer loop.

Per-user clientState is generated + stored on the subscription row; we
verify each inbound notification's clientState against the row to detect
spoofing.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from fastapi import Request

from app.integrations.oauth_helper import oauth_api_call

logger = logging.getLogger(__name__)

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Default lifetime we ask for; Graph clamps to its per-resource max.
DEFAULT_TTL = timedelta(days=2, hours=12)


def new_client_state() -> str:
    return secrets.token_urlsafe(32)


def expiration_iso(ttl: timedelta = DEFAULT_TTL) -> str:
    return (datetime.now(timezone.utc) + ttl).replace(microsecond=0).isoformat().replace("+00:00", "Z")


async def create_subscription(
    *,
    user_id: str,
    agent_id: str,
    change_type: str,
    resource: str,
    notification_url: str,
    client_state: str,
    expiration: Optional[str] = None,
) -> Dict[str, Any]:
    """POST /subscriptions for one (user, agent). Caller chooses the resource path."""
    body = {
        "changeType": change_type,
        "notificationUrl": notification_url,
        "resource": resource,
        "expirationDateTime": expiration or expiration_iso(),
        "clientState": client_state,
    }
    result = await oauth_api_call(
        user_id, agent_id, "microsoft", "POST",
        f"{GRAPH_BASE}/subscriptions",
        json_body=body,
    )
    return result


async def renew_subscription(
    *,
    user_id: str,
    agent_id: str,
    subscription_id: str,
    expiration: Optional[str] = None,
) -> Dict[str, Any]:
    body = {"expirationDateTime": expiration or expiration_iso()}
    return await oauth_api_call(
        user_id, agent_id, "microsoft", "PATCH",
        f"{GRAPH_BASE}/subscriptions/{subscription_id}",
        json_body=body,
    )


async def delete_subscription(*, user_id: str, agent_id: str, subscription_id: str) -> Dict[str, Any]:
    return await oauth_api_call(
        user_id, agent_id, "microsoft", "DELETE",
        f"{GRAPH_BASE}/subscriptions/{subscription_id}",
    )


async def maybe_validation_response(request: Request) -> Optional[Any]:
    """If this request is a Graph subscription validation handshake, return
    a FastAPI Response that echoes the validationToken. Otherwise return
    None (caller proceeds with normal handling)."""
    token = request.query_params.get("validationToken")
    if token:
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(content=token, status_code=200)
    return None


async def verify_client_state(
    request_payload: Dict[str, Any],
    expected_states: Dict[str, str],
) -> Tuple[bool, list]:
    """Filter Graph notifications to those whose subscriptionId is known
    and whose clientState matches our stored value.

    ``request_payload`` is the JSON body Graph posts: ``{"value": [{...}]}``.
    ``expected_states`` maps subscriptionId → stored clientState.

    Returns (any_valid, list_of_valid_notifications).
    """
    valid = []
    for note in (request_payload.get("value") or []):
        sub_id = note.get("subscriptionId") or ""
        cs = note.get("clientState") or ""
        if sub_id in expected_states and expected_states[sub_id] == cs:
            valid.append(note)
        else:
            logger.warning("Graph notification rejected (sub=%s)", sub_id)
    return (len(valid) > 0, valid)

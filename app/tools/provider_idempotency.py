"""Provider-native idempotency identifiers for side-effecting tool adapters.

Only adapters backed by a documented provider deduplication primitive should
register ``provider_idempotent=True``. The durable reservation layer may then
retry a lost lease with the same identifier; other uncertain calls remain
manual-reconciliation only.
"""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass
from typing import Optional

from app.tools.execution_context import current_tool_context


@dataclass(frozen=True)
class ProviderOperation:
    provider: str
    operation: str
    resource_id: str
    reservation_key: str
    retry_safe: bool


def _active_key() -> str:
    context = current_tool_context()
    if not context or not context.side_effecting:
        return ""
    return str(context.idempotency_key or "")


def _register(
    provider: str,
    operation: str,
    resource_id: str,
    *,
    retry_safe: bool,
) -> Optional[ProviderOperation]:
    key = _active_key()
    if not key:
        return None
    from app.agent.turn_reservations import register_provider_reconciliation

    register_provider_reconciliation(
        key,
        provider=provider,
        operation=operation,
        resource_id=resource_id,
        provider_idempotent=retry_safe,
    )
    return ProviderOperation(provider, operation, resource_id, key, retry_safe)


def google_calendar_create() -> Optional[ProviderOperation]:
    """Create a valid base32hex Google Calendar event ID from the reservation."""
    key = _active_key()
    if not key:
        return None
    digest = hashlib.sha256(f"gcal:{key}".encode("utf-8")).digest()
    event_id = base64.b32hexencode(digest).decode("ascii").lower().rstrip("=")
    return _register(
        "google",
        "calendar.events.insert",
        event_id,
        retry_safe=True,
    )


def microsoft_calendar_create() -> Optional[ProviderOperation]:
    """Create a stable Graph transactionId from the durable reservation."""
    key = _active_key()
    if not key:
        return None
    raw = hashlib.sha256(f"ms-calendar:{key}".encode("utf-8")).digest()[:16]
    transaction_id = str(uuid.UUID(bytes=raw))
    return _register(
        "microsoft",
        "calendar.events.create",
        transaction_id,
        retry_safe=True,
    )


def stripe_headers() -> dict[str, str]:
    """Return Stripe's documented idempotency header for a POST adapter."""
    key = _active_key()
    if not key:
        return {}
    # Stripe accepts up to 255 characters; the reservation key is a 64-char hash.
    _register("stripe", "api.post", key, retry_safe=True)
    return {"Idempotency-Key": key}

"""Normalized event types.

Every ``EventSource`` produces the same ``NormalizedEvent`` shape, regardless
of provider. The router and agent runtime never need to know whether an event
came from Gmail, Outlook, Slack, or a poll loop — only the source plugin
itself deals with provider-specific payloads.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class NormalizedEvent:
    """Shape every event source emits.

    ``payload`` is a source-specific dict (e.g. ``{from, subject, snippet}``
    for emails) — the agent reads it through the prompt-injection step in
    the router. ``raw_ref`` is optional opaque data the agent (or a follow-up
    tool call) can use to fetch full content (e.g. message_id + thread_id).
    """

    source: str                 # e.g. "gmail"
    event_type: str             # e.g. "message_received"
    owner_user_id: str          # WebAgent internal user id
    external_id: str            # provider-side primary key for dedup
    occurred_at: str            # ISO timestamp
    payload: Dict[str, Any] = field(default_factory=dict)
    raw_ref: Dict[str, Any] = field(default_factory=dict)
    received_at: str = field(default_factory=_now_iso)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    def payload_excerpt(self, limit: int = 400) -> str:
        try:
            s = json.dumps(self.payload, ensure_ascii=False)
        except Exception:
            s = str(self.payload)
        return s[:limit]


@dataclass
class SubscriptionRegistration:
    """Returned by ``EventSource.register_subscription``.

    For push sources this carries the provider-side IDs the renewer + the
    webhook intake need to keep talking to the provider. For poll sources
    everything except ``poll_cursor`` is typically None.
    """

    external_subscription_id: Optional[str] = None
    external_resource_id: Optional[str] = None
    external_expiration_at: Optional[str] = None
    external_metadata: Dict[str, Any] = field(default_factory=dict)
    poll_cursor: Optional[str] = None

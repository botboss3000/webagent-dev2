"""Abstract base class for event sources.

An ``EventSource`` is one external service that can emit events the agent
should react to (Gmail, Slack, Google Calendar, etc.). Implementations live
under ``app.events.sources`` and are auto-discovered by the manager.

Each source decides for itself whether it's push (real-time webhook /
provider-side ``watch``) or poll (we ask on a cadence). The router and the
automation file format don't care which it is.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple

from fastapi import Request

from app.events.types import NormalizedEvent, SubscriptionRegistration


class EventSource(ABC):
    """One external service that can emit events.

    Lifecycle:
      1. On WebAgent startup the manager discovers the class.
      2. When a user subscribes (via the automation file), ``register_subscription``
         is called and any provider-side state is persisted to the row.
      3. For push sources, provider webhooks land at ``/api/v1/events/{name}``
         and are handled by ``handle_webhook``.
      4. For poll sources, the poller calls ``poll`` on a cadence.
      5. Before the provider-side TTL expires, the renewer calls ``renew_subscription``.
      6. On unsubscribe, ``unregister_subscription`` is called so providers
         don't keep delivering for revoked rows.
    """

    # ── Identity ──────────────────────────────────────────────────────────

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier, e.g. ``"gmail"``. Matches the URL path
        ``/api/v1/events/{name}`` and the ``source`` column in the DB."""
        ...

    @property
    @abstractmethod
    def event_types(self) -> List[str]:
        """Event types this source can emit, e.g. ``["message_received"]``."""
        ...

    @property
    @abstractmethod
    def supports_push(self) -> bool:
        """True if the provider can push events to us in real time."""
        ...

    @property
    def supports_poll(self) -> bool:
        """True if this source implements ``poll``. Defaults to "not push"."""
        return not self.supports_push

    @property
    def default_poll_interval_seconds(self) -> int:
        """Seconds between poll ticks for poll-only sources."""
        return 60

    @property
    def required_provider(self) -> Optional[str]:
        """OAuth provider name in ``app/integrations/oauth_helper.py``,
        e.g. ``"google"``, ``"microsoft"``, ``"dropbox"``. Used to check
        whether a user has the integration connected before subscribing."""
        return None

    @property
    def enabled(self) -> bool:
        """Source is allowed to run. Default: yes. Override to gate on admin
        config / required env vars / installed dependencies."""
        return True

    @property
    def description(self) -> str:
        """Human-readable description of what this source emits, surfaced
        to the LLM parser and the UI."""
        return f"{self.name} events: {', '.join(self.event_types)}"

    # ── Subscription lifecycle ────────────────────────────────────────────

    @abstractmethod
    async def register_subscription(
        self,
        *,
        owner_user_id: str,
        agent_id: str,
        event_type: str,
        filter_dict: Dict[str, Any],
    ) -> SubscriptionRegistration:
        """Wire up the provider side of one subscription for one (user, agent).

        Push sources register a watch / Graph subscription and return its
        IDs + TTL. Poll sources return an initial cursor (or none). OAuth
        tokens are scoped per (user, agent), so the source MUST pass
        ``agent_id`` to every ``oauth_api_call`` / ``auth_element_get``.
        """
        ...

    async def renew_subscription(
        self,
        subscription_row: Dict[str, Any],
    ) -> SubscriptionRegistration:
        """Refresh a push subscription before it expires.

        Default implementation re-runs ``register_subscription`` with the
        same filter; override for providers that have a cheaper refresh
        endpoint.
        """
        return await self.register_subscription(
            owner_user_id=subscription_row["owner_user_id"],
            agent_id=subscription_row.get("agent_id", ""),
            event_type=subscription_row["event_type"],
            filter_dict=subscription_row.get("filter") or {},
        )

    async def unregister_subscription(
        self,
        subscription_row: Dict[str, Any],
    ) -> None:
        """Tell the provider to stop delivering. Default: no-op.

        Push sources should override this (Gmail ``stop``, Graph DELETE on
        ``/subscriptions/{id}``, Drive channel stop, etc.).
        """
        return None

    # ── Inbound paths ─────────────────────────────────────────────────────

    async def verify_webhook(self, request: Request) -> bool:
        """Verify request authenticity. Default: accept (override in production)."""
        return True

    async def handle_webhook(
        self,
        request: Request,
    ) -> List[NormalizedEvent]:
        """Convert one provider webhook into 0+ normalized events.

        Push sources implement this. The HTTP intake route calls
        ``verify_webhook`` first, then this. Empty list = nothing to route
        (heartbeats, sync messages, etc.).
        """
        return []

    async def poll(
        self,
        subscription_row: Dict[str, Any],
    ) -> Tuple[List[NormalizedEvent], Optional[str]]:
        """Return (new events since last poll, new poll cursor).

        Poll sources implement this. Returning an empty list with the same
        cursor is fine. The poller persists the returned cursor only when
        no exception is raised.
        """
        return [], subscription_row.get("poll_cursor")

    # ── OAuth scopes ─────────────────────────────────────────────────────

    def required_scopes(
        self,
        event_type: str,
        filter_dict: Dict[str, Any],
    ) -> List[List[str]]:
        """OAuth scopes the user's token must hold for this subscription to
        actually work end-to-end.

        Returned as a list of *groups* — within each group ANY one scope
        satisfies it (alternates), but EVERY group must be satisfied. Most
        sources return a single group of alternates (e.g. Gmail watch:
        `gmail.modify` OR `gmail.metadata` OR `mail.google.com`).

        Empty list = no scope gating (default). The chat-level
        `event_subscribe` tool reads this and refuses with `code=scope_missing`
        before inserting a doomed row.
        """
        return []

    # ── Filtering ─────────────────────────────────────────────────────────

    def matches_filter(
        self,
        event: NormalizedEvent,
        filter_dict: Dict[str, Any],
    ) -> bool:
        """Decide whether ``event`` satisfies a subscription's filter.

        Default: empty filter matches everything; otherwise every key in
        ``filter_dict`` must equal the same key under ``event.payload``.
        Override for source-specific semantics (Gmail query, Slack channel
        membership, glob/regex, etc.).
        """
        if not filter_dict:
            return True
        for k, v in filter_dict.items():
            if event.payload.get(k) != v:
                return False
        return True

    # ── LLM-facing schema ─────────────────────────────────────────────────

    def parser_schema(self) -> Dict[str, Any]:
        """Hint shown to the automation parser so the LLM knows what filters
        this source accepts. Returned in the source manifest endpoint."""
        return {
            "source": self.name,
            "event_types": list(self.event_types),
            "supports_push": self.supports_push,
            "supports_poll": self.supports_poll,
            "required_provider": self.required_provider,
            "filter_fields": [],
            "examples": [],
            "description": self.description,
        }

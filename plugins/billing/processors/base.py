"""Abstract payment processor interface.

Concrete processors (Stripe, PayPal, ...) implement this protocol. The rest of
the billing system is processor-agnostic — it calls these methods and treats
the returned URLs, IDs, and events uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Protocol, runtime_checkable


@dataclass
class CheckoutSession:
    """Result of create_checkout — caller redirects user to redirect_url."""
    session_id: str
    redirect_url: str
    processor: str
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Subscription:
    subscription_id: str
    status: str
    processor: str
    current_period_end: Optional[str] = None
    redirect_url: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class OnboardingLink:
    """Result of onboard_payee — redirect agent admin here to onboard."""
    account_id: str
    redirect_url: str
    processor: str
    complete: bool = False


@dataclass
class WebhookEvent:
    """Normalized event after the processor has verified its signature."""
    processor: str
    event_id: str
    event_type: str          # processor-agnostic: 'payment.completed', 'subscription.updated', ...
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    amount_cents: int = 0
    currency: str = "usd"
    external_payment_id: Optional[str] = None
    external_subscription_id: Optional[str] = None
    raw_event_type: Optional[str] = None   # the processor's native event name
    raw: Dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class PaymentProcessor(Protocol):
    name: str
    display_name: str

    def is_configured(self) -> bool:
        """True if env vars/SDK are set up so calls won't crash."""

    def supported_features(self) -> Dict[str, bool]:
        """e.g. {'checkout': True, 'subscriptions': True, 'onboarding': True}."""

    async def create_checkout(
        self,
        *,
        user_id: str,
        amount_cents: int,
        currency: str,
        kind: str,             # 'purchase' for credit packs, 'one_off' otherwise
        success_url: str,
        cancel_url: str,
        agent_id: Optional[str] = None,
        payee_account_id: Optional[str] = None,
        platform_fee_cents: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession: ...

    async def create_subscription(
        self,
        *,
        user_id: str,
        agent_id: str,
        price_cents: int,
        currency: str,
        success_url: str,
        cancel_url: str,
        payee_account_id: Optional[str] = None,
        platform_fee_pct: float = 0.0,
    ) -> Subscription: ...

    async def onboard_payee(
        self,
        *,
        user_id: str,
        return_url: str,
        refresh_url: str,
    ) -> OnboardingLink: ...

    async def verify_webhook(
        self,
        *,
        headers: Dict[str, str],
        body: bytes,
    ) -> WebhookEvent: ...

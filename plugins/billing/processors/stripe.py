"""Stripe Checkout + Connect Express implementation.

End-user payments flow:
    User → /billing/wallet/purchase
    → create_checkout() returns a Stripe Checkout Session URL
    → user pays
    → Stripe POSTs webhook event → verify_webhook() → wallet.credit()

Marketplace split: when payee_account_id is provided, the charge uses
Connect's `transfer_data.destination` so the agent admin gets paid out
automatically with the platform fee retained.

The Stripe SDK is optional — if it's not installed the processor returns
is_configured()=False and registration is skipped.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from plugins.billing.processors.base import (
    CheckoutSession,
    OnboardingLink,
    PaymentProcessor,
    Subscription,
    WebhookEvent,
)

logger = logging.getLogger(__name__)


class StripeProcessor:
    name = "stripe"
    display_name = "Stripe"

    def __init__(self):
        try:
            import stripe  # type: ignore
            self._stripe = stripe
            api_key = os.environ.get("STRIPE_SECRET_KEY", "")
            if api_key:
                stripe.api_key = api_key
            self._webhook_secret = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
        except ImportError:
            self._stripe = None
            self._webhook_secret = ""

    def is_configured(self) -> bool:
        return self._stripe is not None and bool(os.environ.get("STRIPE_SECRET_KEY"))

    def supported_features(self) -> Dict[str, bool]:
        return {"checkout": True, "subscriptions": True, "onboarding": True, "split": True}

    async def create_checkout(
        self, *, user_id, amount_cents, currency, kind, success_url, cancel_url,
        agent_id=None, payee_account_id=None, platform_fee_cents=0, metadata=None,
    ) -> CheckoutSession:
        if not self.is_configured():
            raise RuntimeError("Stripe is not configured")
        stripe = self._stripe
        md = {"user_id": user_id, "kind": kind}
        if agent_id:
            md["agent_id"] = agent_id
        if metadata:
            md.update({k: str(v) for k, v in metadata.items()})
        params: Dict[str, Any] = {
            "mode": "payment",
            "payment_method_types": ["card"],
            "line_items": [{
                "price_data": {
                    "currency": currency.lower(),
                    "product_data": {"name": f"WebAgent {kind}"},
                    "unit_amount": int(amount_cents),
                },
                "quantity": 1,
            }],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": md,
            "client_reference_id": user_id,
        }
        if payee_account_id and platform_fee_cents >= 0:
            params["payment_intent_data"] = {
                "application_fee_amount": int(platform_fee_cents),
                "transfer_data": {"destination": payee_account_id},
            }
        session = stripe.checkout.Session.create(**params)
        return CheckoutSession(
            session_id=session.id,
            redirect_url=session.url,
            processor=self.name,
            metadata=md,
        )

    async def create_subscription(
        self, *, user_id, agent_id, price_cents, currency, success_url, cancel_url,
        payee_account_id=None, platform_fee_pct=0.0,
    ) -> Subscription:
        if not self.is_configured():
            raise RuntimeError("Stripe is not configured")
        stripe = self._stripe
        params: Dict[str, Any] = {
            "mode": "subscription",
            "payment_method_types": ["card"],
            "line_items": [{
                "price_data": {
                    "currency": currency.lower(),
                    "product_data": {"name": f"WebAgent subscription ({agent_id})"},
                    "unit_amount": int(price_cents),
                    "recurring": {"interval": "month"},
                },
                "quantity": 1,
            }],
            "success_url": success_url,
            "cancel_url": cancel_url,
            "metadata": {"user_id": user_id, "agent_id": agent_id, "kind": "subscription"},
            "client_reference_id": user_id,
        }
        if payee_account_id and platform_fee_pct > 0:
            params["subscription_data"] = {
                "application_fee_percent": float(platform_fee_pct),
                "transfer_data": {"destination": payee_account_id},
            }
        session = stripe.checkout.Session.create(**params)
        return Subscription(
            subscription_id=session.id,
            status="pending",
            processor=self.name,
            redirect_url=session.url,
        )

    async def onboard_payee(self, *, user_id, return_url, refresh_url) -> OnboardingLink:
        if not self.is_configured():
            raise RuntimeError("Stripe is not configured")
        stripe = self._stripe
        # One account per user — reuse if one already exists in our DB layer.
        account = stripe.Account.create(
            type="express",
            metadata={"user_id": user_id},
        )
        link = stripe.AccountLink.create(
            account=account.id,
            refresh_url=refresh_url,
            return_url=return_url,
            type="account_onboarding",
        )
        return OnboardingLink(
            account_id=account.id,
            redirect_url=link.url,
            processor=self.name,
            complete=False,
        )

    async def verify_webhook(self, *, headers, body) -> WebhookEvent:
        if not self.is_configured():
            raise RuntimeError("Stripe is not configured")
        stripe = self._stripe
        sig = headers.get("stripe-signature") or headers.get("Stripe-Signature") or ""
        if not self._webhook_secret:
            raise RuntimeError("STRIPE_WEBHOOK_SECRET not set")
        event = stripe.Webhook.construct_event(
            payload=body, sig_header=sig, secret=self._webhook_secret,
        )
        event_type = event["type"]
        data = event["data"]["object"]
        md = (data.get("metadata") or {}) if isinstance(data, dict) else {}

        normalized = WebhookEvent(
            processor=self.name,
            event_id=event["id"],
            event_type=_normalize_stripe_event(event_type),
            raw_event_type=event_type,
            user_id=md.get("user_id") or data.get("client_reference_id"),
            agent_id=md.get("agent_id"),
            amount_cents=int(data.get("amount_total") or data.get("amount") or 0),
            currency=(data.get("currency") or "usd"),
            external_payment_id=data.get("payment_intent") or data.get("id"),
            external_subscription_id=data.get("subscription"),
            raw=dict(event),
        )
        return normalized


def _normalize_stripe_event(t: str) -> str:
    if t in ("checkout.session.completed", "payment_intent.succeeded"):
        return "payment.completed"
    if t == "payment_intent.payment_failed":
        return "payment.failed"
    if t in ("customer.subscription.created", "customer.subscription.updated"):
        return "subscription.updated"
    if t == "customer.subscription.deleted":
        return "subscription.cancelled"
    if t == "charge.refunded":
        return "payment.refunded"
    return "other"


FEATURE = {
    "id": "stripe",
    "display_name": "Stripe",
    "category": "payment",
    "status": "beta",
    "summary": "Stripe Checkout + Connect marketplace payouts.",
    "requires": ["the stripe SDK", "STRIPE_SECRET_KEY"],
}

# Drop-in hook: the processor registry auto-registers this class.
processor_cls = StripeProcessor

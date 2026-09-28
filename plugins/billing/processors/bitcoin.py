"""Bitcoin and Lightning checkout through BTCPay Server's Greenfield API."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
from decimal import Decimal
from typing import Any, Dict, Optional

import httpx

from plugins.billing.processors.base import (
    CheckoutSession,
    OnboardingLink,
    Subscription,
    WebhookEvent,
)


class BitcoinProcessor:
    name = "bitcoin"
    display_name = "Bitcoin"

    def __init__(self) -> None:
        self._url = (os.environ.get("BTCPAY_URL") or "").rstrip("/")
        self._store_id = os.environ.get("BTCPAY_STORE_ID") or ""
        self._api_key = os.environ.get("BTCPAY_API_KEY") or ""
        self._webhook_secret = os.environ.get("BTCPAY_WEBHOOK_SECRET") or ""

    def is_configured(self) -> bool:
        return bool(self._url and self._store_id and self._api_key)

    def supported_features(self) -> Dict[str, bool]:
        return {
            "checkout": True,
            "subscriptions": False,
            "onboarding": False,
            "split": False,
        }

    async def create_checkout(
        self,
        *,
        user_id: str,
        amount_cents: int,
        currency: str,
        kind: str,
        success_url: str,
        cancel_url: str,
        agent_id: Optional[str] = None,
        payee_account_id: Optional[str] = None,
        platform_fee_cents: int = 0,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> CheckoutSession:
        if not self.is_configured():
            raise RuntimeError("BTCPay Server is not configured")

        invoice_metadata: Dict[str, Any] = {
            "user_id": user_id,
            "kind": kind,
            "amount_cents": int(amount_cents),
            "currency": currency.lower(),
        }
        if agent_id:
            invoice_metadata["agent_id"] = agent_id
        if metadata:
            invoice_metadata.update({key: str(value) for key, value in metadata.items()})

        endpoint = f"{self._url}/api/v1/stores/{self._store_id}/invoices"
        payload = {
            "amount": str(Decimal(amount_cents) / Decimal(100)),
            "currency": currency.upper(),
            "metadata": invoice_metadata,
            "checkout": {
                "redirectURL": success_url,
                "redirectAutomatically": True,
            },
        }
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                endpoint,
                headers={"Authorization": f"token {self._api_key}"},
                json=payload,
            )
            response.raise_for_status()
            invoice = response.json()

        invoice_id = str(invoice.get("id") or "")
        checkout_link = str(invoice.get("checkoutLink") or "")
        if not invoice_id or not checkout_link:
            raise RuntimeError("BTCPay Server returned an incomplete invoice")
        return CheckoutSession(
            session_id=invoice_id,
            redirect_url=checkout_link,
            processor=self.name,
            metadata=invoice_metadata,
        )

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
    ) -> Subscription:
        raise RuntimeError("Bitcoin subscriptions are not supported")

    async def onboard_payee(
        self,
        *,
        user_id: str,
        return_url: str,
        refresh_url: str,
    ) -> OnboardingLink:
        raise RuntimeError("BTCPay Server does not use marketplace onboarding")

    async def verify_webhook(
        self,
        *,
        headers: Dict[str, str],
        body: bytes,
    ) -> WebhookEvent:
        if not self._webhook_secret:
            raise RuntimeError("BTCPAY_WEBHOOK_SECRET not set")
        supplied = headers.get("btcpay-sig") or headers.get("BTCPay-Sig") or ""
        expected = "sha256=" + hmac.new(
            self._webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied, expected):
            raise RuntimeError("BTCPay webhook signature verification failed")

        event = json.loads(body)
        event_type = str(event.get("type") or "")
        metadata = event.get("metadata") or {}
        return WebhookEvent(
            processor=self.name,
            event_id=str(event.get("deliveryId") or event.get("invoiceId") or ""),
            event_type=_normalize_event(event_type),
            raw_event_type=event_type,
            user_id=metadata.get("user_id"),
            agent_id=metadata.get("agent_id"),
            amount_cents=int(metadata.get("amount_cents") or 0),
            currency=str(metadata.get("currency") or "usd").lower(),
            external_payment_id=str(event.get("invoiceId") or ""),
            raw=event,
        )


def _normalize_event(event_type: str) -> str:
    if event_type == "InvoiceSettled":
        return "payment.completed"
    if event_type in ("InvoiceExpired", "InvoiceInvalid"):
        return "payment.failed"
    return "other"


FEATURE = {
    "id": "bitcoin",
    "display_name": "Bitcoin",
    "category": "payment",
    "status": "beta",
    "summary": "Bitcoin and Lightning checkout through BTCPay Server.",
    "requires": [
        "BTCPAY_URL",
        "BTCPAY_STORE_ID",
        "BTCPAY_API_KEY",
        "BTCPAY_WEBHOOK_SECRET",
    ],
}

processor_cls = BitcoinProcessor

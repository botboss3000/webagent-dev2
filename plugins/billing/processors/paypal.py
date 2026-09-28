"""PayPal Orders + Subscriptions implementation.

PayPal's SDK is HTTP-based; we use httpx directly so we don't pull in an extra
dependency. Sandbox vs live is selected via PAYPAL_ENV=sandbox|live.

Marketplace split uses PayPal's platform_fees field on the order — the order
is captured into the agent admin's PayPal merchant account, with the platform
fee retained by the platform's account.

If httpx isn't available or the API creds are unset, is_configured() returns
False and registration is skipped.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, Optional

from plugins.billing.processors.base import (
    CheckoutSession,
    OnboardingLink,
    PaymentProcessor,
    Subscription,
    WebhookEvent,
)

logger = logging.getLogger(__name__)


_SANDBOX_HOST = "https://api-m.sandbox.paypal.com"
_LIVE_HOST = "https://api-m.paypal.com"


class PayPalProcessor:
    name = "paypal"
    display_name = "PayPal"

    def __init__(self):
        try:
            import httpx  # type: ignore
            self._httpx = httpx
        except ImportError:
            self._httpx = None
        env = (os.environ.get("PAYPAL_ENV") or "sandbox").lower()
        self._host = _LIVE_HOST if env == "live" else _SANDBOX_HOST
        self._client_id = os.environ.get("PAYPAL_CLIENT_ID", "")
        self._client_secret = os.environ.get("PAYPAL_CLIENT_SECRET", "")
        self._webhook_id = os.environ.get("PAYPAL_WEBHOOK_ID", "")
        self._token_cache: Dict[str, Any] = {"token": None, "expires_at": 0}

    def is_configured(self) -> bool:
        return bool(self._httpx and self._client_id and self._client_secret)

    def supported_features(self) -> Dict[str, bool]:
        return {"checkout": True, "subscriptions": True, "onboarding": True, "split": True}

    # ── Internal ──
    async def _token(self) -> str:
        now = time.time()
        if self._token_cache["token"] and self._token_cache["expires_at"] > now + 30:
            return self._token_cache["token"]
        async with self._httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{self._host}/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
                headers={"Accept": "application/json"},
            )
            r.raise_for_status()
            body = r.json()
        self._token_cache = {
            "token": body["access_token"],
            "expires_at": now + int(body.get("expires_in", 3600)),
        }
        return body["access_token"]

    async def _post(self, path: str, payload: dict, headers: Optional[dict] = None) -> dict:
        tok = await self._token()
        h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}
        if headers:
            h.update(headers)
        async with self._httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{self._host}{path}", json=payload, headers=h)
            r.raise_for_status()
            return r.json()

    async def _get(self, path: str) -> dict:
        tok = await self._token()
        async with self._httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"{self._host}{path}",
                headers={"Authorization": f"Bearer {tok}"},
            )
            r.raise_for_status()
            return r.json()

    # ── Public ──
    async def create_checkout(
        self, *, user_id, amount_cents, currency, kind, success_url, cancel_url,
        agent_id=None, payee_account_id=None, platform_fee_cents=0, metadata=None,
    ) -> CheckoutSession:
        if not self.is_configured():
            raise RuntimeError("PayPal is not configured")
        value = f"{amount_cents / 100:.2f}"
        unit: Dict[str, Any] = {
            "amount": {"currency_code": currency.upper(), "value": value},
            "custom_id": user_id,
        }
        if payee_account_id:
            unit["payee"] = {"merchant_id": payee_account_id}
        if payee_account_id and platform_fee_cents > 0:
            unit["payment_instruction"] = {
                "disbursement_mode": "INSTANT",
                "platform_fees": [{
                    "amount": {"currency_code": currency.upper(),
                                "value": f"{platform_fee_cents / 100:.2f}"},
                }],
            }
        payload = {
            "intent": "CAPTURE",
            "purchase_units": [unit],
            "application_context": {
                "return_url": success_url,
                "cancel_url": cancel_url,
                "user_action": "PAY_NOW",
            },
        }
        order = await self._post("/v2/checkout/orders", payload)
        approve = next(
            (l["href"] for l in order.get("links", []) if l.get("rel") == "approve"),
            "",
        )
        md = {"user_id": user_id, "kind": kind, "agent_id": agent_id or ""}
        if metadata:
            md.update({k: str(v) for k, v in metadata.items()})
        return CheckoutSession(
            session_id=order["id"],
            redirect_url=approve,
            processor=self.name,
            metadata=md,
        )

    async def create_subscription(
        self, *, user_id, agent_id, price_cents, currency, success_url, cancel_url,
        payee_account_id=None, platform_fee_pct=0.0,
    ) -> Subscription:
        if not self.is_configured():
            raise RuntimeError("PayPal is not configured")
        # PayPal subscriptions require a billing plan ID — in production you create
        # this once per agent. Here we assume the agent admin has set up a plan
        # whose ID is stored in their payment_accounts.metadata; if absent, return
        # an unconfigured Subscription so the UI can prompt.
        plan_id = (
            os.environ.get(f"PAYPAL_PLAN_ID_{agent_id}".upper().replace("-", "_"))
            or os.environ.get("PAYPAL_DEFAULT_PLAN_ID")
        )
        if not plan_id:
            raise RuntimeError(
                "PayPal subscription plan not configured for this agent. "
                "Create a plan in the PayPal dashboard and set PAYPAL_DEFAULT_PLAN_ID."
            )
        payload = {
            "plan_id": plan_id,
            "custom_id": f"{user_id}|{agent_id}",
            "application_context": {
                "return_url": success_url,
                "cancel_url": cancel_url,
            },
        }
        sub = await self._post("/v1/billing/subscriptions", payload)
        approve = next(
            (l["href"] for l in sub.get("links", []) if l.get("rel") == "approve"),
            "",
        )
        return Subscription(
            subscription_id=sub["id"],
            status=(sub.get("status") or "pending").lower(),
            processor=self.name,
            redirect_url=approve,
        )

    async def onboard_payee(self, *, user_id, return_url, refresh_url) -> OnboardingLink:
        if not self.is_configured():
            raise RuntimeError("PayPal is not configured")
        # PayPal partner referrals API — generates a sign-up link that turns
        # the agent admin into a merchant on the platform's PayPal account.
        payload = {
            "tracking_id": user_id,
            "operations": [{
                "operation": "API_INTEGRATION",
                "api_integration_preference": {
                    "rest_api_integration": {
                        "integration_method": "PAYPAL",
                        "integration_type": "THIRD_PARTY",
                        "third_party_details": {
                            "features": ["PAYMENT", "REFUND", "PARTNER_FEE"],
                        },
                    },
                },
            }],
            "products": ["EXPRESS_CHECKOUT"],
            "partner_config_override": {
                "return_url": return_url,
            },
        }
        resp = await self._post("/v2/customer/partner-referrals", payload)
        action = next(
            (l["href"] for l in resp.get("links", []) if l.get("rel") == "action_url"),
            "",
        )
        return OnboardingLink(
            account_id=user_id,  # PayPal returns the merchant ID later via webhook
            redirect_url=action,
            processor=self.name,
            complete=False,
        )

    async def verify_webhook(self, *, headers, body) -> WebhookEvent:
        if not self.is_configured():
            raise RuntimeError("PayPal is not configured")
        if not self._webhook_id:
            raise RuntimeError("PAYPAL_WEBHOOK_ID not set")
        # Verify signature via PayPal API
        try:
            event_body = json.loads(body.decode("utf-8") if isinstance(body, bytes) else body)
        except Exception as e:
            raise RuntimeError(f"Invalid webhook body: {e}")
        verify_payload = {
            "auth_algo": headers.get("paypal-auth-algo") or headers.get("PAYPAL-AUTH-ALGO"),
            "cert_url": headers.get("paypal-cert-url") or headers.get("PAYPAL-CERT-URL"),
            "transmission_id": headers.get("paypal-transmission-id") or headers.get("PAYPAL-TRANSMISSION-ID"),
            "transmission_sig": headers.get("paypal-transmission-sig") or headers.get("PAYPAL-TRANSMISSION-SIG"),
            "transmission_time": headers.get("paypal-transmission-time") or headers.get("PAYPAL-TRANSMISSION-TIME"),
            "webhook_id": self._webhook_id,
            "webhook_event": event_body,
        }
        verify = await self._post("/v1/notifications/verify-webhook-signature", verify_payload)
        if (verify.get("verification_status") or "").upper() != "SUCCESS":
            raise RuntimeError("PayPal webhook signature verification failed")

        resource = event_body.get("resource") or {}
        custom = resource.get("custom_id") or ""
        user_id = custom.split("|")[0] if custom else None
        agent_id = custom.split("|")[1] if "|" in custom else None
        amount_str = (
            (resource.get("amount") or {}).get("value")
            or (resource.get("purchase_units") or [{}])[0].get("amount", {}).get("value")
            or "0"
        )
        try:
            amount_cents = int(round(float(amount_str) * 100))
        except Exception:
            amount_cents = 0

        return WebhookEvent(
            processor=self.name,
            event_id=event_body.get("id", ""),
            event_type=_normalize_paypal_event(event_body.get("event_type", "")),
            raw_event_type=event_body.get("event_type"),
            user_id=user_id,
            agent_id=agent_id,
            amount_cents=amount_cents,
            currency=((resource.get("amount") or {}).get("currency_code") or "USD").lower(),
            external_payment_id=resource.get("id"),
            external_subscription_id=(
                resource.get("id") if "SUBSCRIPTION" in (event_body.get("event_type") or "") else None
            ),
            raw=event_body,
        )


def _normalize_paypal_event(t: str) -> str:
    if t in ("PAYMENT.CAPTURE.COMPLETED", "CHECKOUT.ORDER.APPROVED",
              "CHECKOUT.ORDER.COMPLETED"):
        return "payment.completed"
    if t == "PAYMENT.CAPTURE.DENIED":
        return "payment.failed"
    if t == "PAYMENT.CAPTURE.REFUNDED":
        return "payment.refunded"
    if t.startswith("BILLING.SUBSCRIPTION."):
        if t.endswith("CANCELLED") or t.endswith("EXPIRED"):
            return "subscription.cancelled"
        return "subscription.updated"
    return "other"


FEATURE = {
    "id": "paypal",
    "display_name": "PayPal",
    "category": "payment",
    "status": "beta",
    "summary": "PayPal Orders + Subscriptions.",
    "requires": ["PAYPAL_CLIENT_ID + PAYPAL_CLIENT_SECRET"],
}

# Drop-in hook: the processor registry auto-registers this class.
processor_cls = PayPalProcessor

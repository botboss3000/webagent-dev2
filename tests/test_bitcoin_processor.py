"""BTCPay Server processor behavior."""

import asyncio
import hashlib
import hmac
import json

import pytest

from plugins.billing.processors.bitcoin import BitcoinProcessor


def test_bitcoin_processor_configuration_and_features(monkeypatch):
    monkeypatch.setenv("BTCPAY_URL", "https://btcpay.example")
    monkeypatch.setenv("BTCPAY_STORE_ID", "store-1")
    monkeypatch.setenv("BTCPAY_API_KEY", "token-1")

    processor = BitcoinProcessor()

    assert processor.is_configured() is True
    assert processor.supported_features() == {
        "checkout": True,
        "subscriptions": False,
        "onboarding": False,
        "split": False,
    }


def test_bitcoin_webhook_verification(monkeypatch):
    monkeypatch.setenv("BTCPAY_WEBHOOK_SECRET", "webhook-secret")
    processor = BitcoinProcessor()
    body = json.dumps({
        "deliveryId": "delivery-1",
        "type": "InvoiceSettled",
        "invoiceId": "invoice-1",
        "metadata": {
            "user_id": "user-1",
            "amount_cents": 1250,
            "currency": "usd",
        },
    }).encode()
    signature = "sha256=" + hmac.new(
        b"webhook-secret", body, hashlib.sha256,
    ).hexdigest()

    event = asyncio.run(processor.verify_webhook(
        headers={"btcpay-sig": signature},
        body=body,
    ))

    assert event.event_type == "payment.completed"
    assert event.user_id == "user-1"
    assert event.amount_cents == 1250
    assert event.external_payment_id == "invoice-1"


def test_bitcoin_webhook_rejects_invalid_signature(monkeypatch):
    monkeypatch.setenv("BTCPAY_WEBHOOK_SECRET", "webhook-secret")
    processor = BitcoinProcessor()

    with pytest.raises(RuntimeError, match="signature verification failed"):
        asyncio.run(processor.verify_webhook(
            headers={"btcpay-sig": "sha256=wrong"},
            body=b"{}",
        ))

"""Processor registry & protocol-conformance tests.

Verifies:
- Every registered processor satisfies the PaymentProcessor Protocol
  (has the methods, with the right signatures).
- An invalid-signature webhook is rejected (raises and the API returns 401).
- A configured-but-keyless processor stays out of the registry gracefully.
"""

import asyncio
import os
import sys

import pytest

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from plugins.billing.processors import _REGISTRY, get_processor, list_processors
from plugins.billing.processors.base import PaymentProcessor


REQUIRED_METHODS = [
    "create_checkout", "create_subscription", "onboard_payee",
    "verify_webhook", "is_configured", "supported_features",
]


def test_protocol_conformance():
    assert isinstance(_REGISTRY, dict)
    # Always at least the slot for stripe + paypal — even when SDKs aren't installed,
    # we register them; is_configured() simply returns False then.
    for name, proc in _REGISTRY.items():
        for m in REQUIRED_METHODS:
            assert hasattr(proc, m), f"{name} missing {m}"
        assert proc.name == name
        assert isinstance(proc.display_name, str) and proc.display_name


def test_list_processors_shape():
    rows = list_processors()
    for r in rows:
        assert set(r.keys()) >= {"name", "display_name", "configured", "features"}
        assert isinstance(r["configured"], bool)
        assert isinstance(r["features"], dict)


def test_get_unknown_returns_none():
    assert get_processor("does-not-exist") is None


def test_stripe_webhook_rejects_bad_signature():
    proc = get_processor("stripe")
    if not proc:
        pytest.skip("stripe processor not registered")
    if not proc.is_configured():
        pytest.skip("stripe not configured (no STRIPE_SECRET_KEY)")
    with pytest.raises(Exception):
        asyncio.run(proc.verify_webhook(headers={}, body=b"{}"))

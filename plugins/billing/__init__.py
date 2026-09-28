"""Billing for WebAgent — the agent tier (always present).

An agent admin sets a price, the end user pays it, and the agent admin keeps it.

Public surface:
    pricing.resolve_charge(agent, user_id, usage, db) -> ChargeResult
    wallet.preauthorize(user_id, cents, db) / settle(...) / refund(...)
    enforcement.check_access(agent, user_id, db) -> AccessDecision
    processors.get_processor(name) -> PaymentProcessor
"""

from plugins.billing.pricing import (
    Usage,
    ChargeResult,
    Strategy,
    resolve_charge,
    load_effective_config,
)
from plugins.billing.enforcement import (
    AccessDecision,
    check_access,
)

__all__ = [
    "Usage",
    "ChargeResult",
    "Strategy",
    "resolve_charge",
    "load_effective_config",
    "AccessDecision",
    "check_access",
]

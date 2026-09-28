"""Configuration for the removable Active Session Bonus plugin."""
from __future__ import annotations
from dataclasses import dataclass

@dataclass(frozen=True)
class BonusConfig:
    base_bonus: int = 60
    # An offer's value decays linearly over its frozen, per-flag resolution
    # window.  The window and maximum are stored on each offer's policy snapshot
    # rather than taken from a global rate, so later policy changes cannot alter
    # an already-issued offer.
    minimum_offer: int = 5
    task_offer_multiplier: float = 0.5
    offers_per_task_limit: int = 3
    per_task_claim_cap: int = 100
    daily_claim_cap: int = 180
    offer_expiry_seconds: int = 86400
    reconcile_seconds: int = 7

DEFAULTS = BonusConfig()

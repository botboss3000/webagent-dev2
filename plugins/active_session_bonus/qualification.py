"""Pure reward calculations and durable-progress qualification rules."""
from __future__ import annotations
import math
from datetime import datetime, timezone
from typing import Mapping
from .config import BonusConfig

# The account wallet persists whole USD cents while the chat footer presents
# ten house credits per cent. Bonus amounts must therefore stay on this exact
# boundary so a displayed ◇ amount is also the amount users receive.
HOUSE_CREDITS_PER_WALLET_CENT = 10


def whole_house_credits(amount: int | float) -> int:
    return max(0, int(amount) // HOUSE_CREDITS_PER_WALLET_CENT * HOUSE_CREDITS_PER_WALLET_CENT)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)

def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat(timespec="microseconds")

def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)

def raw_offer(offer_ordinal: int, config: BonusConfig) -> int:
    """Ordinal is one-based: 1 => base bonus, 2 => half, 3 => quarter."""
    return max(config.minimum_offer, math.floor(
        config.base_bonus * (config.task_offer_multiplier ** (offer_ordinal - 1))))

def eligible_offer(offer_ordinal: int, *, task_claimed: int, daily_claimed: int,
                   config: BonusConfig) -> int:
    return whole_house_credits(min(raw_offer(offer_ordinal, config),
                                   config.per_task_claim_cap - task_claimed,
                                   config.daily_claim_cap - daily_claimed))


def decay_floor(config: BonusConfig) -> int:
    """Lowest amount an open offer decays to, rounded up to a payable amount.

    `minimum_offer` is the policy's floor in credits, but the wallet only stores
    whole cents and pays ten credits per cent. A floor below one cent would leave
    an offer the user can see but never be paid, so it is raised to the smallest
    amount the wallet can actually credit.
    """
    floor = max(0, int(config.minimum_offer))
    if floor <= 0:
        return 0
    return -(-floor // HOUSE_CREDITS_PER_WALLET_CENT) * HOUSE_CREDITS_PER_WALLET_CENT


def resolution_seconds(offer: Mapping, config: BonusConfig) -> int:
    """Return the frozen window that determines this offer's linear slope."""
    policy = offer.get("policy")
    if isinstance(policy, Mapping):
        try:
            seconds = int(policy.get("resolution_seconds") or 0)
            if seconds > 0:
                return seconds
        except (TypeError, ValueError):
            pass
    try:
        seconds = math.ceil((parse_time(offer["expires_at"]) - parse_time(offer["offered_at"])).total_seconds())
        if seconds > 0:
            return seconds
    except (KeyError, TypeError, ValueError):
        pass
    return max(1, int(config.offer_expiry_seconds))


def live_amount(offer: Mapping, config: BonusConfig, now: datetime | None = None) -> int:
    """Amount shown while an offer is open, linearly interpolated to its floor.

    The maximum and resolution window are frozen in the offer's policy snapshot.
    At any moment the value is the remaining fraction of the range between the
    issued amount and payable floor, so it reaches the floor exactly at expiry.
    """
    if offer.get("paused_amount") is not None:
        return int(offer["paused_amount"])
    initial = int(offer["initial_amount"])
    floor = min(initial, decay_floor(config))
    elapsed_seconds = max(0, ((now or utcnow()) - parse_time(offer["offered_at"])).total_seconds())
    elapsed_fraction = min(1.0, elapsed_seconds / resolution_seconds(offer, config))
    return max(floor, math.ceil(initial - (initial - floor) * elapsed_fraction))


def remaining_amount(offer: Mapping, config: BonusConfig, now: datetime | None = None) -> int:
    """The amount an open offer locks at right now.

    Raised to the wallet's whole-cent boundary, so an offer activated seconds
    after it appears pays the ◇ value the user just saw rather than being
    shaved down to the lower cent. A locked offer's stored amount is already on
    that boundary, so re-applying this is a no-op.
    """
    return -(-live_amount(offer, config, now) // HOUSE_CREDITS_PER_WALLET_CENT) * HOUSE_CREDITS_PER_WALLET_CENT

def task_snapshot(state: Mapping) -> dict:
    steps = state.get("steps") or []
    evidence = state.get("evidence") or []
    return {
        "task_id": str(state.get("task_id") or ""),
        # `contract_revision` is the durable task-contract revision.  Retain the
        # legacy `revision` fallback for task records created before that field.
        "task_revision": int(state.get("contract_revision", state.get("revision", 0)) or 0),
        "work_revision": int(state.get("work_revision") or 0),
        "done_step_count": sum(1 for step in steps if step.get("status") == "done"),
        "evidence_count": len(evidence),
        "task_status": str(state.get("status") or ""),
    }

def qualifies(offer: Mapping, current: Mapping) -> bool:
    """Only meaningful work progress after activation can settle a bonus."""
    return (
        str(current.get("task_id") or "") == str(offer.get("task_id") or "")
        and int(current.get("work_revision") or 0) > int(offer.get("baseline_work_revision") or 0)
        and (
            int(current.get("done_step_count") or 0) > int(offer.get("baseline_done_steps") or 0)
            or int(current.get("evidence_count") or 0) > int(offer.get("baseline_evidence_count") or 0)
            or str(current.get("task_status") or "") == "completed"
        )
    )

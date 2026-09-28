"""Access enforcement — does this (user, agent) pair have permission to chat?

The chat endpoint calls check_access() after the existing role checks. On
denial we raise HTTP 402 (Payment Required) with a machine-readable reason
the frontend uses to surface the right paywall: buy credits / subscribe /
start trial / re-up trial.

Agents with no billing config (or strategy='free') always allow.
"""

from __future__ import annotations

import logging
import datetime as _dt
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional

from plugins.billing.pricing import (
    Strategy,
    load_effective_config,
    is_exempt,
    parse_strategy_selection,
    primary_strategy,
    _fetch_one,
    _has_billing_tables,
    _is_byo_llm,
    _user_has_own_llm,
    _trial_active,
    _METERED_STRATEGIES,
)
from plugins.billing import wallet as wallet_mod

logger = logging.getLogger(__name__)


class AccessReason(str, Enum):
    ALLOW = "allow"
    NEEDS_CREDITS = "needs_credits"
    NEEDS_SUBSCRIPTION = "needs_subscription"
    TRIAL_EXPIRED = "trial_expired"


@dataclass
class AccessDecision:
    allow: bool
    reason: AccessReason
    detail: str = ""
    strategy: str = "free"
    balance_cents: int = 0
    accepted_processors: list = None  # type: ignore[assignment]
    agent_id: str = ""

    def to_dict(self) -> dict:
        return {
            "allow": self.allow,
            "reason": self.reason.value,
            "detail": self.detail,
            "strategy": self.strategy,
            "balance_cents": self.balance_cents,
            "accepted_processors": self.accepted_processors or [],
            "agent_id": self.agent_id,
        }


async def check_access(agent: dict, user_id: str, db: Any) -> AccessDecision:
    """Return ALLOW unless billing is configured AND the user can't pay.

    Order:
      1. Tables missing → ALLOW (system not migrated yet).
      2. Exemption → ALLOW.
      3. Own-key run (agent ships its own key, or the user has their own LLM
         config) → ALLOW — the platform isn't footing the bill, so credits
         never apply to inherited models only.
      4. Effective strategy == 'free' → ALLOW.
      5. Trial credit grant active (unexpired + unspent) → ALLOW.
      6. Active subscription → ALLOW.
      7. Credits with positive balance → ALLOW.
      8. Otherwise → deny with the specific reason.
    """
    agent_id = agent.get("id") or agent.get("agent_id") or ""

    if not _has_billing_tables(db):
        return AccessDecision(True, AccessReason.ALLOW, "billing-not-configured")

    if await is_exempt(db, agent_id, user_id):
        return AccessDecision(True, AccessReason.ALLOW, "exempt")

    cfg = await load_effective_config(db, agent_id)
    selected_strategies = parse_strategy_selection(cfg.get("strategy"))
    strategy = primary_strategy(selected_strategies)
    processors = cfg.get("allowed_processors") or []

    # Only a credential owned by this user can waive billing. Agent metadata is
    # merely a requested override and the final roster clamp may discard it; it
    # therefore is not proof that this execution uses a user-owned key.
    if await _user_has_own_llm(user_id, db):
        return AccessDecision(True, AccessReason.ALLOW, "own-llm",
                               strategy=strategy, agent_id=agent_id)

    if selected_strategies == [Strategy.FREE.value]:
        return AccessDecision(True, AccessReason.ALLOW, "free", strategy=strategy)

    # Trial — grant a credit allotment exactly once on a user's first attempt
    # when the admin configured one. An exhausted row is deliberately retained,
    # which makes the trial non-repeatable without an explicit admin extension.
    trial = await _fetch_one(db, "trials", {"user_id": user_id, "agent_id": agent_id})
    if trial is None:
        trial = await _grant_configured_trial(db, user_id, agent_id, cfg.get("trial_config"))
    trial_exhausted = False
    if trial:
        if _trial_active(trial):
            return AccessDecision(True, AccessReason.ALLOW, "trial-active",
                                   strategy="trial", agent_id=agent_id)
        trial_exhausted = True
        if strategy == Strategy.TRIAL.value:
            return AccessDecision(False, AccessReason.TRIAL_EXPIRED, "Your trial has ended.",
                                   strategy=strategy, accepted_processors=processors, agent_id=agent_id)

    # Subscription
    sub = await _fetch_one(db, "subscriptions", {"user_id": user_id, "agent_id": agent_id})
    if sub and sub.get("status") == "active":
        return AccessDecision(True, AccessReason.ALLOW, "subscription-active", strategy="subscription", agent_id=agent_id)
    if strategy == Strategy.SUBSCRIPTION.value:
        return AccessDecision(False, AccessReason.NEEDS_SUBSCRIPTION,
                               "This agent requires an active subscription.",
                               strategy=strategy, accepted_processors=processors, agent_id=agent_id)

    # All metered strategies draw from the user's credit wallet.
    if strategy in _METERED_STRATEGIES:
        w = await wallet_mod.get_balance(db, user_id)
        balance = w.available_cents if w else 0
        if balance > 0:
            return AccessDecision(True, AccessReason.ALLOW, "credits-positive",
                                   strategy=strategy, balance_cents=balance,
                                   accepted_processors=processors, agent_id=agent_id)
        # The user's trial grant is spent and they have no balance — surface the
        # exact reason so the frontend shows the trial-ended panel (buy credits
        # / continue for free) instead of the generic buy-credits modal.
        if trial_exhausted and Strategy.TRIAL.value in selected_strategies:
            return AccessDecision(False, AccessReason.TRIAL_EXPIRED, "Your trial has ended.",
                                   strategy=strategy, balance_cents=balance,
                                   accepted_processors=processors, agent_id=agent_id)
        return AccessDecision(False, AccessReason.NEEDS_CREDITS,
                               "Buy credits to chat with this agent.",
                               strategy=strategy, balance_cents=balance,
                               accepted_processors=processors, agent_id=agent_id)

    return AccessDecision(True, AccessReason.ALLOW, "fallthrough", strategy=strategy)


async def _grant_configured_trial(db: Any, user_id: str, agent_id: str, config: Any) -> Optional[dict]:
    """Create and return a first-use trial, or ``None`` when trials are off.

    The trial is a credit allotment: ``credit_cents`` worth of free usage,
    expiring after ``days`` (0 = never expires). Charging burns the same
    cost × multiplier credits as purchased ones, so when the grant runs out
    the trial-ended panel appears.
    """
    config = config if isinstance(config, dict) else {}
    try:
        days = max(0, int(config.get("days") or 0))
        credit_cents = max(0, int(config.get("credit_cents") or 0))
    except (TypeError, ValueError):
        return None
    if credit_cents <= 0:
        return None

    expires_at = (_dt.datetime.utcnow() + _dt.timedelta(days=days)).isoformat(timespec="seconds") + "Z" if days else None
    row = {
        "id": str(uuid.uuid4()), "user_id": user_id, "agent_id": agent_id,
        "expires_at": expires_at,
        "credit_cents": credit_cents,
        "remaining_cents": credit_cents,
    }
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO trials "
                    "(id,user_id,agent_id,expires_at,credit_cents,remaining_cents) "
                    "VALUES (?,?,?,?,?,?)",
                    tuple(row.values()),
                )
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            # The unique (user_id, agent_id) constraint makes concurrent first
            # messages safe; a losing insert simply re-reads the winning row.
            db.get_raw_client().table("trials").upsert(row, on_conflict="user_id,agent_id").execute()
        else:
            return None
    except Exception as exc:
        logger.debug("trial grant skipped: %s", exc)
        return None
    return await _fetch_one(db, "trials", {"user_id": user_id, "agent_id": agent_id})

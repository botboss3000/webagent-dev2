"""The platform (marketplace) billing extension.

Registered into the core billing seam (``plugins.billing.extensions``) on import.
It implements the marketplace behaviour the agent tier deliberately does not know
about:

  * a platform cut of every charge (``split_charge`` / ``record_charge``),
  * app-wide config defaults + allowed-strategy/processor ceilings
    (``augment_config`` / ``validate_agent_config``),
  * payout routing for subscriptions (``subscription_params``).

With this package absent (a public / agent-only edition) none of the above runs:
the core seam's no-op defaults apply and the agent admin keeps 100%.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Tuple

from fastapi import HTTPException

from plugins.admin.billing import store
from plugins.billing.pricing import parse_strategy_selection

logger = logging.getLogger(__name__)


def _jlist(v: Any) -> list:
    if isinstance(v, list):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return []
    return []


def _jval(v: Any, default: Any) -> Any:
    if v in (None, ""):
        return default
    if isinstance(v, (dict, list)):
        return v
    if isinstance(v, str):
        try:
            return json.loads(v)
        except Exception:
            return default
    return default


def _merge(platform_row: dict, agent_cfg: dict) -> dict:
    """Platform defaults overlaid by the agent's own non-empty values; the
    allowed-strategy ceiling and the platform fee always come from the platform
    row. Ports the original three-tier effective-config merge."""
    out = {
        "scope": agent_cfg.get("scope"),
        "strategy": platform_row.get("strategy") or "free",
        "allowed_strategies": _jlist(platform_row.get("allowed_strategies")),
        "allowed_processors": _jlist(platform_row.get("allowed_processors")),
        "rate_card_default_llm": _jval(platform_row.get("rate_card_default_llm"), {}),
        "rate_card_byo_llm": _jval(platform_row.get("rate_card_byo_llm"), {}),
        "cost_multiplier": float(platform_row.get("cost_multiplier") or 1.0),
        "min_charge_cents": int(platform_row.get("min_charge_cents") or 1),
        "flat_image_cost_usd": float(platform_row.get("flat_image_cost_usd") or 0.01),
        "trial_config": _jval(platform_row.get("trial_config"), {}),
        "subscription_price_cents": int(platform_row.get("subscription_price_cents") or 0),
        "currency": platform_row.get("currency") or "usd",
        "platform_fee_pct": float(platform_row.get("platform_fee_pct") or 0),
        "platform_fee_flat_cents": int(platform_row.get("platform_fee_flat_cents") or 0),
    }
    for k, v in (agent_cfg or {}).items():
        if v in (None, "", [], {}, 0):
            continue
        if k in ("allowed_strategies", "platform_fee_pct", "platform_fee_flat_cents"):
            continue  # ceilings/fee always from the platform row
        out[k] = v
    # Cost-pricing fields: the agent overrides the platform ONLY when explicitly
    # set (None = inherit). 0 is a valid explicit override (e.g. disabling the
    # min-charge floor), so handle these outside the generic loop above which
    # skips 0. _row_to_config returns raw (None) values for unset fields.
    for k in ("cost_multiplier", "min_charge_cents", "flat_image_cost_usd"):
        if (agent_cfg or {}).get(k) is not None:
            out[k] = agent_cfg[k]
    return out


class PlatformBillingExtension:
    """Duck-typed implementation of ``plugins.billing.extensions.BillingExtension``."""

    async def augment_config(self, db: Any, agent_id: str, agent_cfg: dict) -> dict:
        platform = await store.read_platform_config(db)
        if not platform:
            return agent_cfg
        return _merge(platform, agent_cfg or {})

    async def split_charge(self, db: Any, agent: dict, end_user_charge_cents: int) -> Tuple[int, int]:
        pct, flat = await store.fee_params(db)
        return store.split(end_user_charge_cents, pct, flat)

    async def record_charge(
        self,
        db: Any,
        *,
        event_id: str,
        agent_id: str,
        user_id: str,
        end_user_charge_cents: int,
        deducted_cents: int,
        retained_cents: int,
        interaction_id: Optional[str] = None,
    ) -> None:
        store.record_ledger(
            db, event_id=event_id, agent_id=agent_id, user_id=user_id,
            end_user_charge_cents=end_user_charge_cents,
            platform_fee_cents=deducted_cents, agent_earnings_cents=retained_cents,
            interaction_id=interaction_id,
        )

    async def validate_agent_config(self, db: Any, agent_id: str, fields: dict) -> None:
        platform = await store.read_platform_config(db)
        allowed_strategies = set(_jlist(platform.get("allowed_strategies")))
        allowed_processors = set(_jlist(platform.get("allowed_processors")))
        strat = fields.get("strategy")
        selected = set(parse_strategy_selection(strat)) - {"free"} if strat else set()
        disallowed = sorted(selected - allowed_strategies) if allowed_strategies else []
        if disallowed:
            raise HTTPException(status_code=400,
                                detail=f"Strategies not enabled by platform admin: {disallowed}")
        procs = fields.get("allowed_processors")
        if procs and allowed_processors:
            bad = [p for p in procs if p not in allowed_processors]
            if bad:
                raise HTTPException(status_code=400,
                                    detail=f"Processors not enabled by platform admin: {bad}")

    async def subscription_params(self, db: Any, agent_id: str, processor: str) -> Tuple[Optional[str], float]:
        platform = await store.read_platform_config(db)
        pct = float(platform.get("platform_fee_pct") or 0)
        payee = store.get_payee_account_id(db, agent_id, processor)
        return payee, pct

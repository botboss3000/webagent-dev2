"""Pricing engine — the single source of truth for what a chat costs.

resolve_charge() is the only place pricing logic lives. The chat loop, the
admin UIs, and the test suite all go through it. Strategies dispatch off
billing_configs.strategy; exemptions short-circuit before any strategy runs.

Money is always integer cents.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from plugins.billing.extensions import apply_config_augment

logger = logging.getLogger(__name__)


# ── Types ──────────────────────────────────────────────────────────────────


class Strategy(str, Enum):
    FREE = "free"
    CREDITS = "credits"
    PER_MESSAGE = "per_message"
    PER_TOKEN = "per_token"
    SUBSCRIPTION = "subscription"
    TRIAL = "trial"


def parse_strategy_selection(value: Any, *, strict: bool = False) -> List[str]:
    """Parse the stored comma-separated strategy selection.

    Existing single-value configs remain valid. ``trial`` and ``subscription``
    act as access options/modifiers; when several metered strategies are
    selected, the first one is the charge calculation used after the trial.
    """
    raw_values = value if isinstance(value, (list, tuple, set)) else str(value or "").split(",")
    known = {strategy.value for strategy in Strategy}
    selected: List[str] = []
    unknown: List[str] = []
    for raw in raw_values:
        token = str(raw).strip().lower()
        if not token or token in selected:
            continue
        if token not in known:
            unknown.append(token)
            continue
        selected.append(token)
    if strict and unknown:
        raise ValueError(f"Unknown strategy: {','.join(unknown)}")
    if len(selected) > 1 and Strategy.FREE.value in selected:
        selected.remove(Strategy.FREE.value)
    return selected or [Strategy.FREE.value]


def primary_strategy(value: Any) -> str:
    """Return the strategy used once trial/subscription access does not apply."""
    selected = parse_strategy_selection(value)
    for strategy in selected:
        if strategy in {
            Strategy.CREDITS.value,
            Strategy.PER_MESSAGE.value,
            Strategy.PER_TOKEN.value,
        }:
            return strategy
    if Strategy.SUBSCRIPTION.value in selected:
        return Strategy.SUBSCRIPTION.value
    if Strategy.TRIAL.value in selected:
        return Strategy.TRIAL.value
    return Strategy.FREE.value


@dataclass
class Usage:
    """Output of one LLM call. Tokens come straight from chunk.usage."""
    input_tokens: int = 0
    output_tokens: int = 0
    provider_cost_cents: int = 0  # raw provider cost (markup added later)
    message_count: int = 1


@dataclass
class ChargeResult:
    """What the end user pays for one usage event (the agent admin keeps it).

    Any allocation of that charge is applied downstream by the optional billing
    extension; this calculation does not know about it."""
    end_user_charge_cents: int = 0
    strategy: str = "free"
    is_byo_llm: bool = False
    is_trial: bool = False
    is_exempt: bool = False
    exempt_reason: Optional[str] = None
    # How a charge is covered: the trial grant pays `trial_used_cents`, the
    # credit wallet pays `wallet_charge_cents`. Both default to the full charge
    # for their respective paths (kept explicit so the debit side never guesses).
    trial_used_cents: int = 0
    wallet_charge_cents: int = 0
    notes: Dict[str, Any] = field(default_factory=dict)


# ── Config loading ─────────────────────────────────────────────────────────


# The ONLY pricing knobs: a multiplier over the platform's real provider cost,
# a minimum charge floor, and a per-image estimate for image models that don't
# report usage. Credits are consumed as 1 credit = 1¢ of charged cost.
_DEFAULT_BILLING = {
    "cost_multiplier": 1.0,        # charge = provider_cost × this
    "min_charge_cents": 1,         # floor per usage event
    "flat_image_cost_usd": 0.01,   # per-image estimate when the provider
                                   # reports no usage (OpenAI /images, etc.)
}

# Strategies that draw from the credit wallet (legacy per_message/per_token now
# behave as credits — clean cutover). subscription is deliberately EXCLUDED: a
# subscription agent never draws on the wallet (its access is the subscription
# itself); a cancelled/lapsed subscription simply falls through to free in
# resolve_charge (the access gate is what blocks the user).
_METERED_STRATEGIES = {
    Strategy.CREDITS.value,
    Strategy.PER_MESSAGE.value,
    Strategy.PER_TOKEN.value,
}


def _parse_json(value: Any, default: Any) -> Any:
    if value is None or value == "":
        return default
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return default
    return default


def _row_to_config(row: Optional[dict]) -> dict:
    if not row:
        return {}
    return {
        "scope": row.get("scope"),
        "strategy": row.get("strategy") or "free",
        "allowed_strategies": _parse_json(row.get("allowed_strategies"), []),
        "allowed_processors": _parse_json(row.get("allowed_processors"), []),
        "rate_card_default_llm": _parse_json(row.get("rate_card_default_llm"), {}),
        "rate_card_byo_llm": _parse_json(row.get("rate_card_byo_llm"), {}),
        # Cost-based pricing knobs — returned RAW (None when the agent hasn't
        # overridden them) so the effective-config merge can distinguish "unset"
        # (inherit the platform default) from an explicit value. Consumers
        # default via _cost_setting() at use time, never here.
        "cost_multiplier": row.get("cost_multiplier"),
        "min_charge_cents": row.get("min_charge_cents"),
        "flat_image_cost_usd": row.get("flat_image_cost_usd"),
        "trial_config": _parse_json(row.get("trial_config"), {}),
        "subscription_price_cents": int(row.get("subscription_price_cents") or 0),
        "currency": row.get("currency") or "usd",
    }


def _cost_setting(cfg: dict, key: str) -> float:
    """A cost knob with a sane default when unset (None/empty). 0 is honoured
    (e.g. min_charge_cents=0 disables the floor), only negatives are rejected."""
    try:
        v = cfg.get(key)
        if v is None or v == "":
            return _DEFAULT_BILLING[key]
        f = float(v)
        return f if f >= 0 else _DEFAULT_BILLING[key]
    except (TypeError, ValueError):
        return _DEFAULT_BILLING[key]


def _empty_config() -> dict:
    return _row_to_config({})


async def load_effective_config(db: Any, agent_id: str) -> dict:
    """Return the agent's own billing config (defaults to 'free' when unset, so
    pre-existing agents stay free until their admin configures billing).

    An optional billing extension, if installed, may augment this with extra
    defaults/limits; otherwise the agent config stands alone and the agent admin
    keeps 100% of every charge."""
    if not _has_billing_tables(db):
        return _empty_config()
    agent_row = await _fetch_one(db, "billing_configs", {"scope": f"agent:{agent_id}"})
    cfg = _row_to_config(agent_row) if agent_row else _empty_config()
    return await apply_config_augment(db, agent_id, cfg)


# ── Exemption check ────────────────────────────────────────────────────────


async def is_exempt(db: Any, agent_id: str, user_id: str) -> Optional[str]:
    """Return a string reason if exempt, else None."""
    if not _has_billing_tables(db):
        return None
    # Platform administrators operate and test the app itself. They must never
    # consume trials, credits, or subscriptions, regardless of per-agent billing
    # configuration. Keep this in the shared exemption seam so it applies both
    # to the pre-send access gate and to post-response charge settlement.
    try:
        if hasattr(db, "is_user_admin") and await db.is_user_admin(user_id):
            return "admin"
    except Exception as exc:
        logger.debug("billing admin exemption check failed for %s: %s", user_id, exc)
    # Agent-wide
    row = await _fetch_one(db, "billing_exemptions", {"kind": "agent", "agent_id": agent_id})
    if row:
        return "agent"
    # User-global
    row = await _fetch_one(db, "billing_exemptions", {"kind": "user", "user_id": user_id})
    if row:
        return "user"
    # User-for-agent
    row = await _fetch_one(db, "billing_exemptions", {
        "kind": "user_for_agent",
        "agent_id": agent_id,
        "user_id": user_id,
    })
    if row:
        return "user_for_agent"
    return None


# ── BYO-LLM detection ──────────────────────────────────────────────────────


def _is_byo_llm(agent: dict) -> bool:
    """True when the agent ships its own LLM key. Pricing uses BYO rate card."""
    meta_raw = agent.get("metadata")
    if isinstance(meta_raw, str):
        try:
            meta = json.loads(meta_raw)
        except Exception:
            meta = {}
    elif isinstance(meta_raw, dict):
        meta = meta_raw
    else:
        meta = {}
    llm_config = meta.get("llm_config") or {}
    if llm_config.get("use_default") is True:
        return False
    # Treat presence of a non-empty api_key / secret_ref as BYO
    if llm_config.get("api_key") or llm_config.get("secret_ref"):
        return True
    for row in llm_config.get("multi_providers") or []:
        if isinstance(row, dict) and (
            row.get("api_key") or row.get("secret_ref")
            or row.get("credential_configured") is True
        ):
            return True
    if llm_config.get("byo") is True:
        return True
    return False


# ── Charge computation ────────────────────────────────────────────────────


def _compute_charge(usage: Usage, cfg: dict) -> int:
    """The charge for one usage event: the platform's real provider cost scaled
    by the admin's multiplier, floored at the minimum charge. Applied whenever
    the run uses an inherited (platform-key) model; own-key runs are free."""
    multiplier = _cost_setting(cfg, "cost_multiplier")
    cents = usage.provider_cost_cents * multiplier
    floor = int(_cost_setting(cfg, "min_charge_cents"))
    return max(int(round(cents)), floor)


# Test seam: tests stub this to control the own-LLM probe without a DB.
_own_llm_probe = None  # async (user_id) -> bool


async def _byo_capabilities(user_id: str, db: Any = None) -> dict:
    """Return only the model entitlement fragment, failing closed."""
    try:
        from app.entitlements.service import resolve_capabilities
        capabilities = await resolve_capabilities(user_id, db=db, use_cache=False)
        return capabilities.get("models") or {}
    except Exception as exc:
        logger.warning("BYO entitlement lookup failed for %s: %s", user_id, exc)
        return {}


async def _user_has_own_llm(user_id: str, db: Any = None) -> bool:
    """True when the USER has their own LLM config (their key pays for runs).

    Billing is deliberately symmetric with the runtime resolution
    (app.admin.settings.apply_provider_for_run): when the user's own config
    carries a key, the platform isn't footing the bill, so no credits apply.
    """
    # A configured key only makes a run free when this user's effective tier
    # permits BYO models. Otherwise stale settings could bypass billing even
    # though runtime model selection will clamp them away.
    if not bool((await _byo_capabilities(user_id, db)).get("allow_byo")):
        return False
    if _own_llm_probe is not None:
        return await _own_llm_probe(user_id)
    try:
        from app.admin.settings import user_has_own_llm_config
        return await user_has_own_llm_config(user_id)
    except Exception:
        return False


# ── Public entry point ─────────────────────────────────────────────────────


async def resolve_charge(
    agent: dict,
    user_id: str,
    usage: Usage,
    db: Any,
    *,
    cfg: Optional[dict] = None,
    trial_row: Optional[dict] = None,
    subscription_row: Optional[dict] = None,
    own_llm: Optional[bool] = None,
) -> ChargeResult:
    """Compute the charge for one usage event (LLM call or image).

    Order of precedence:
        1. Exemption (returns zero, flag set)
        2. Own-key run (agent ships its own key, or the user has their own key
           for THIS modality) → zero. The platform isn't footing the bill, so
           credits never apply to inherited models only.
        3. Trial grant (if active) covers up to its remaining credits; anything
           beyond that is charged to the wallet.
        4. Subscription (if active, free for the user; agent admin still gets
           a portion of the subscription revenue tracked elsewhere)
        5. Metered strategy on the effective config → cost × multiplier
        6. Default 'free'

    ``own_llm`` is an explicit ownership override for callers who already know
    whose key pays (e.g. image generation, where the relevant key is the user's
    IMAGE provider, not their text LLM). None = probe the user's text LLM.
    """
    agent_id = agent.get("id") or agent.get("agent_id") or ""

    # 1. Exemption
    reason = await is_exempt(db, agent_id, user_id)
    if reason:
        return ChargeResult(strategy="free", is_exempt=True, exempt_reason=reason)

    # 2. Effective config
    if cfg is None:
        cfg = await load_effective_config(db, agent_id)
    strategy = primary_strategy(cfg.get("strategy"))
    # An agent-stored key is not enough to prove whose credential the final
    # entitlement-clamped run used. Only the explicit modality signal or the
    # caller's entitled own-model configuration can waive billing.
    byo = False

    # 2b. Own-key run → free. The agent shipping its own key, or the user
    # bringing their own key for this modality, both mean the platform's wallet
    # is not touched.
    user_own_key = await _user_has_own_llm(user_id, db) if own_llm is None else (
        bool(own_llm) and bool((await _byo_capabilities(user_id, db)).get("allow_byo"))
    )
    own_key = user_own_key
    if own_key:
        return ChargeResult(strategy=strategy, is_byo_llm=byo,
                            notes={"own_llm": not byo})

    charge = _compute_charge(usage, cfg)

    # 3. Trial grant covers its remaining credits first; the wallet covers the
    # excess. (If the charge is larger than the grant remainder, the wallet
    # pays the difference — no weird "3 credits left but message costs 10" wall.)
    # Only metered strategies (or a trial-only agent) burn a trial — a free or
    # subscription agent must never consume the grant.
    trial_applies = strategy in _METERED_STRATEGIES or strategy == Strategy.TRIAL.value
    if trial_applies and trial_row is None and _has_billing_tables(db):
        trial_row = await _fetch_one(
            db, "trials", {"user_id": user_id, "agent_id": agent_id}
        )
    if trial_applies and _trial_active(trial_row):
        remaining = max(0, int(trial_row.get("remaining_cents") or 0))
        trial_used = min(remaining, charge)
        return ChargeResult(
            end_user_charge_cents=charge,
            strategy=strategy,
            is_byo_llm=byo,
            is_trial=True,
            trial_used_cents=trial_used,
            wallet_charge_cents=charge - trial_used,
        )

    # 4. Active subscription means no incremental charge.
    if subscription_row is None and _has_billing_tables(db):
        subscription_row = await _fetch_one(
            db, "subscriptions", {"user_id": user_id, "agent_id": agent_id}
        )
    if subscription_row and subscription_row.get("status") == "active":
        return ChargeResult(strategy="subscription", is_byo_llm=byo)

    # 5. Metered strategies — the end user pays and the agent admin keeps it.
    # Any allocation is applied downstream by the optional billing extension,
    # never in this calculation.
    if strategy in _METERED_STRATEGIES:
        return ChargeResult(
            end_user_charge_cents=charge,
            strategy=strategy,
            is_byo_llm=byo,
            wallet_charge_cents=charge,
        )

    # Default — free
    return ChargeResult(strategy="free", is_byo_llm=byo)


def _trial_active(trial_row: Optional[dict]) -> bool:
    """A trial is active while its credit grant is unexpired AND unspent."""
    if not trial_row:
        return False
    import datetime as _dt
    expires_at = trial_row.get("expires_at")
    if expires_at:
        try:
            if isinstance(expires_at, str):
                exp = _dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            else:
                exp = expires_at
            now = _dt.datetime.now(tz=exp.tzinfo) if exp.tzinfo else _dt.datetime.utcnow()
            if exp < now:
                return False
        except Exception:
            pass
    remaining = trial_row.get("remaining_cents")
    if remaining is None:
        # Legacy row without a grant (pre-credit-grant trials): treat as
        # exhausted — clean cutover, no fallback.
        return False
    return int(remaining) > 0


# ── DB helpers ─────────────────────────────────────────────────────────────


def _has_billing_tables(db: Any) -> bool:
    """Best-effort check so the system stays functional before migration is run."""
    if db is None:
        return False
    if hasattr(db, "_billing_ready"):
        return db._billing_ready
    ready = False
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                row = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name='billing_configs' LIMIT 1"
                ).fetchone()
                ready = row is not None
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            # Supabase: assume present; if not, the first query will surface it.
            ready = True
    except Exception:
        ready = False
    try:
        setattr(db, "_billing_ready", ready)
    except Exception:
        pass
    return ready


async def _fetch_one(db: Any, table: str, filters: dict) -> Optional[dict]:
    """Cross-backend single-row fetch. SQLite uses _get_conn; Supabase the client."""
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                where = " AND ".join([f"{k}=?" for k in filters.keys()])
                sql = f"SELECT * FROM {table} WHERE {where} LIMIT 1"
                row = conn.execute(sql, tuple(filters.values())).fetchone()
                if row is None:
                    return None
                # sqlite3.Row supports dict-style access
                return {k: row[k] for k in row.keys()}
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            q = db.get_raw_client().table(table).select("*")
            for k, v in filters.items():
                q = q.eq(k, v)
            res = q.limit(1).execute()
            return res.data[0] if res.data else None
    except Exception as e:
        logger.debug("billing._fetch_one(%s) failed: %s", table, e)
    return None

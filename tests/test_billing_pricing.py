"""Unit tests for the cost-based billing pricing engine.

Covers the credit model:
- charge = provider cost × cost_multiplier (floored at min_charge_cents),
  applied ONLY to inherited (platform-key) models;
- own-key runs using the caller's entitled credential are
  free — the platform isn't footing the bill;
- the trial is a credit grant that spends exactly like purchased credits and
  hands off to the wallet when the grant runs out mid-charge;
- access enforcement: trial-active / credits-positive / trial_expired /
  needs_credits, in the right order.

The pricing engine is pure-logic; we mock the DB with a tiny in-memory shim
so we never hit SQLite/Supabase.
"""

import asyncio
import json
import os
import sqlite3
import sys
from typing import Any, Dict, List, Optional

import pytest

# Make sure the repo root is on sys.path when run from any cwd
HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from plugins.billing import pricing as pricing_mod
from plugins.billing.pricing import (
    Usage,
    Strategy,
    parse_strategy_selection,
    primary_strategy,
    resolve_charge,
    _is_byo_llm,
    _compute_charge,
)
from plugins.billing.enforcement import _grant_configured_trial, check_access


class FakeDB:
    """Minimal DB stub. _has_billing_tables sees ._get_conn so we monkey-patch."""
    def __init__(self):
        self.tables: Dict[str, List[dict]] = {
            "billing_configs": [],
            "billing_exemptions": [],
            "trials": [],
            "subscriptions": [],
            "wallets": [],
            "wallet_transactions": [],
        }
        self._billing_ready = True  # bypass the runtime probe
        self.admin_users = set()

    async def is_user_admin(self, user_id: str) -> bool:
        return user_id in self.admin_users

    # Pretend to be a sqlite-backed db so _fetch_one's first branch is taken.
    def _get_conn(self):
        return _FakeConn(self.tables)


class _FakeConn:
    def __init__(self, tables):
        self.tables = tables

    def execute(self, sql: str, params: tuple = ()):
        return _FakeCursor(self.tables, sql, params)

    def commit(self): pass
    def close(self): pass


class _FakeCursor:
    def __init__(self, tables, sql, params):
        self.tables = tables
        self.sql = sql.strip()
        self.params = params

    def _parse_select(self):
        low = self.sql.lower()
        if not low.startswith("select"):
            return None
        from_idx = low.index(" from ") + 6
        rest = self.sql[from_idx:]
        parts = rest.split(None, 1)
        table = parts[0]
        rows = self.tables.get(table, [])
        if " where " not in low:
            return rows
        where = self.sql[low.index(" where ") + 7:]
        where = where.split(" LIMIT")[0].split(" limit")[0].split(" ORDER")[0]
        clauses = [c.strip() for c in where.split(" AND ")]
        keys = []
        for c in clauses:
            if "=?" in c:
                keys.append(c.split("=", 1)[0].strip())
        filt = dict(zip(keys, self.params))
        out = []
        for r in rows:
            ok = True
            for k, v in filt.items():
                if r.get(k) != v:
                    ok = False
                    break
            if ok:
                out.append(r)
        return out

    def fetchone(self):
        r = self._parse_select() or []
        if not r:
            return None
        return _Row(r[0])

    def fetchall(self):
        r = self._parse_select() or []
        return [_Row(d) for d in r]


class _Row:
    def __init__(self, d):
        self._d = d
    def __getitem__(self, k):
        return self._d.get(k)
    def keys(self):
        return list(self._d.keys())
    def get(self, k, default=None):
        return self._d.get(k, default)


def _agent(byo=False, llm_config=None):
    if llm_config is None:
        llm_config = {"byo": True} if byo else {"use_default": True}
    return {"id": "agent-1", "metadata": json.dumps({"llm_config": llm_config})}


def _platform_cfg(strategy="free", **kwargs):
    base = {
        "scope": "platform",
        "strategy": strategy,
        "allowed_strategies": json.dumps(["credits", "trial"]),
        "allowed_processors": json.dumps(["bitcoin"]),
        "cost_multiplier": 2.0,
        "min_charge_cents": 1,
        "flat_image_cost_usd": 0.01,
        "trial_config": json.dumps({"credit_cents": 500, "days": 7}),
        "currency": "usd",
    }
    base.update(kwargs)
    return base


def _agent_cfg(scope, strategy=None, **kwargs):
    out = {"scope": scope, "strategy": strategy or "free"}
    out.update(kwargs)
    return out


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db():
    d = FakeDB()
    d.tables["billing_configs"].append(_platform_cfg(strategy="free"))
    return d


@pytest.fixture(autouse=True)
def _no_own_llm(monkeypatch):
    """Tests never have a user-owned LLM unless they opt in."""
    async def _probe(user_id):
        return False
    async def _allowed_capabilities(_user_id, **_kwargs):
        return {"models": {"allow_byo": True}}
    pricing_mod._own_llm_probe = _probe
    monkeypatch.setattr(
        "app.entitlements.service.resolve_capabilities", _allowed_capabilities,
    )
    yield
    pricing_mod._own_llm_probe = None


# ── BYO detection ──

def test_byo_detection():
    assert _is_byo_llm({"metadata": json.dumps({"llm_config": {"use_default": True}})}) is False
    assert _is_byo_llm({"metadata": json.dumps({"llm_config": {"byo": True}})}) is True
    assert _is_byo_llm({"metadata": json.dumps({"llm_config": {"api_key": "sk-x"}})}) is True
    assert _is_byo_llm({"metadata": {"llm_config": {"multi_providers": [
        {"entry_id": "owned", "api_key": "sk-row", "credential_scope": "agent"},
    ]}}}) is True


# ── Strategy parsing ──

def test_strategy_selection_is_backward_compatible_and_deterministic():
    assert parse_strategy_selection("credits") == ["credits"]
    assert parse_strategy_selection(" trial, credits ") == ["trial", "credits"]
    assert primary_strategy("trial,credits") == "credits"
    assert primary_strategy("free") == "free"
    with pytest.raises(ValueError, match="Unknown strategy"):
        parse_strategy_selection("trial,not_real", strict=True)


# ── Charge computation: cost × multiplier ──

def test_charge_is_cost_times_multiplier(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits", cost_multiplier=2.0))
    usage = Usage(input_tokens=1000, output_tokens=500, provider_cost_cents=100)
    r = _run(resolve_charge(_agent(), "u1", usage, db))
    # platform cost 100¢ × multiplier 2.0 = 200¢
    assert r.end_user_charge_cents == 200
    assert r.strategy == "credits"
    assert r.wallet_charge_cents == 200


def test_charge_respects_min_charge_floor(db):
    db.tables["billing_configs"].append(
        _agent_cfg("agent:agent-1", strategy="credits", min_charge_cents=25)
    )
    usage = Usage(provider_cost_cents=1)
    r = _run(resolve_charge(_agent(), "u1", usage, db))
    assert r.end_user_charge_cents == 25  # 1¢ × 2.0 = 2¢ → floored to 25¢


def test_charge_zero_cost_still_floored(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=0), db))
    assert r.end_user_charge_cents == 1  # min charge


def test_compute_charge_direct():
    assert _compute_charge(Usage(provider_cost_cents=50), {"cost_multiplier": 3.0, "min_charge_cents": 1}) == 150
    assert _compute_charge(Usage(provider_cost_cents=0), {"cost_multiplier": 1.0, "min_charge_cents": 1}) == 1


# ── Own-key runs are free ──

def test_user_own_llm_is_free(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    async def _probe(user_id):
        return True
    pricing_mod._own_llm_probe = _probe
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=10_000), db))
    assert r.end_user_charge_cents == 0
    assert r.notes.get("own_llm") is True


def test_tier_denied_byo_does_not_bypass_billing(db, monkeypatch):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))

    async def _probe(_user_id):
        return True

    async def denied_capabilities(_user_id, **_kwargs):
        return {"models": {"allow_byo": False}}

    pricing_mod._own_llm_probe = _probe
    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", denied_capabilities)
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    assert r.end_user_charge_cents == 50
    assert r.wallet_charge_cents == 50


def test_explicit_own_llm_override_still_obeys_tier(db, monkeypatch):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))

    async def denied_capabilities(_user_id, **_kwargs):
        return {"models": {"allow_byo": False}}

    monkeypatch.setattr("app.entitlements.service.resolve_capabilities", denied_capabilities)
    r = _run(resolve_charge(
        _agent(), "u1", Usage(provider_cost_cents=50), db, own_llm=True
    ))
    assert r.end_user_charge_cents == 50


def test_agent_metadata_key_alone_does_not_bypass_billing(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    r = _run(resolve_charge(_agent(byo=True), "u1", Usage(provider_cost_cents=10_000), db))
    assert r.end_user_charge_cents == 10_000
    assert r.is_byo_llm is False


# ── Free strategy ──

def test_free_returns_zero(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="free"))
    r = _run(resolve_charge(_agent(), "u1", Usage(input_tokens=100, output_tokens=200), db))
    assert r.end_user_charge_cents == 0
    assert r.strategy == "free"
    assert not r.is_exempt and not r.is_trial


# ── Trial credit grant ──

def test_active_trial_covers_charge_from_grant(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits", cost_multiplier=2.0))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 500,
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    # charge = 50¢ × 2.0 = 100¢, all covered by the grant
    assert r.is_trial is True
    assert r.end_user_charge_cents == 100
    assert r.trial_used_cents == 100
    assert r.wallet_charge_cents == 0


def test_trial_handoff_to_wallet_mid_charge(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits", cost_multiplier=2.0))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 30,
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    # charge 100¢: grant covers 30, wallet covers the remaining 70
    assert r.is_trial is True
    assert r.trial_used_cents == 30
    assert r.wallet_charge_cents == 70


def test_exhausted_trial_falls_through_to_credits(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits", cost_multiplier=2.0))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 0,
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    assert r.is_trial is False
    assert r.strategy == "credits"
    assert r.end_user_charge_cents == 100
    assert r.wallet_charge_cents == 100


def test_expired_trial_is_inactive(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits"))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 500,
        "expires_at": "2000-01-01T00:00:00Z",
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    assert r.is_trial is False


def test_legacy_trial_row_without_grant_is_exhausted(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits", cost_multiplier=2.0))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "messages_remaining": 1, "tokens_remaining": 1,  # legacy shape
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    assert r.is_trial is False
    assert r.end_user_charge_cents == 100


# ── Subscription ──

def test_active_subscription_is_free(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    db.tables["subscriptions"].append({
        "user_id": "u1", "agent_id": "agent-1", "status": "active",
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=10_000), db))
    assert r.end_user_charge_cents == 0
    assert r.strategy == "subscription"


def test_cancelled_subscription_falls_through(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits", cost_multiplier=2.0))
    db.tables["subscriptions"].append({
        "user_id": "u1", "agent_id": "agent-1", "status": "cancelled",
    })
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=50), db))
    assert r.end_user_charge_cents == 100


# ── Exemptions ──

def test_admin_exemption_skips_access_gate_and_charges(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    d = FakeDB()
    d.tables["billing_configs"].append(_platform_cfg(strategy="free"))
    d.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    d.admin_users.add("boss")
    decision = _run(check_access(_agent(), "boss", d))
    assert decision.allow is True
    assert decision.detail == "exempt"
    r = _run(resolve_charge(_agent(), "boss", Usage(provider_cost_cents=9999), d))
    assert r.end_user_charge_cents == 0
    assert r.is_exempt is True


# ── Access enforcement ──

def test_free_agent_always_allows(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="free"))
    decision = _run(check_access(_agent(), "u1", db))
    assert decision.allow is True
    assert decision.detail == "free"


def test_own_llm_allows_even_when_billing_configured(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    async def _probe(user_id):
        return True
    pricing_mod._own_llm_probe = _probe
    decision = _run(check_access(_agent(), "u1", db))
    assert decision.allow is True
    assert decision.detail == "own-llm"


def test_active_trial_allows(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits"))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 300,
    })
    decision = _run(check_access(_agent(), "u1", db))
    assert decision.allow is True
    assert decision.detail == "trial-active"


def test_exhausted_trial_with_balance_allows(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits", cost_multiplier=2.0))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 0,
    })
    db.tables["wallets"].append({
        "id": "w1", "owner_type": "user", "owner_id": "u1",
        "balance_cents": 500, "hold_cents": 0, "currency": "usd",
    })
    decision = _run(check_access(_agent(), "u1", db))
    assert decision.allow is True
    assert decision.detail == "credits-positive"


def test_exhausted_trial_no_balance_reports_trial_expired(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="trial,credits", cost_multiplier=2.0))
    db.tables["trials"].append({
        "user_id": "u1", "agent_id": "agent-1",
        "credit_cents": 500, "remaining_cents": 0,
    })
    db.tables["wallets"].append({
        "id": "w1", "owner_type": "user", "owner_id": "u1",
        "balance_cents": 0, "hold_cents": 0, "currency": "usd",
    })
    decision = _run(check_access(_agent(), "u1", db))
    assert decision.allow is False
    assert decision.reason.value == "trial_expired"
    assert decision.strategy == "credits"


def test_credits_only_no_balance_reports_needs_credits(db):
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits"))
    db.tables["wallets"].append({
        "id": "w1", "owner_type": "user", "owner_id": "u1",
        "balance_cents": 0, "hold_cents": 0, "currency": "usd",
    })
    decision = _run(check_access(_agent(), "u1", db))
    assert decision.allow is False
    assert decision.reason.value == "needs_credits"


def test_configured_trial_is_granted_once(tmp_path):
    """A configured trial starts on first use and an exhausted row is never reset."""
    path = tmp_path / "billing.db"

    class SqliteDB:
        _billing_ready = True
        def _get_conn(self):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

    db = SqliteDB()
    conn = db._get_conn()
    try:
        conn.execute("CREATE TABLE trials (id TEXT, user_id TEXT, agent_id TEXT, expires_at TEXT, "
                     "credit_cents INTEGER, remaining_cents INTEGER, "
                     "messages_remaining INTEGER, tokens_remaining INTEGER, "
                     "UNIQUE(user_id, agent_id))")
        conn.commit()
    finally:
        conn.close()

    trial = _run(_grant_configured_trial(db, "u1", "agent-1", {"days": 7, "credit_cents": 500}))
    assert trial["credit_cents"] == 500
    assert trial["remaining_cents"] == 500
    assert trial["expires_at"]

    # Config changes must not re-grant an existing user's trial.
    same_trial = _run(_grant_configured_trial(db, "u1", "agent-1", {"days": 30, "credit_cents": 999}))
    assert same_trial["id"] == trial["id"]
    assert same_trial["credit_cents"] == 500


def test_trial_not_granted_when_no_credit_value(tmp_path):
    """A trial config with 0 credit value grants nothing (no silent free access)."""
    path = tmp_path / "billing2.db"

    class SqliteDB:
        _billing_ready = True
        def _get_conn(self):
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            return conn

    db = SqliteDB()
    conn = db._get_conn()
    try:
        conn.execute("CREATE TABLE trials (id TEXT, user_id TEXT, agent_id TEXT, expires_at TEXT, "
                     "credit_cents INTEGER, remaining_cents INTEGER, "
                     "messages_remaining INTEGER, tokens_remaining INTEGER, "
                     "UNIQUE(user_id, agent_id))")
        conn.commit()
    finally:
        conn.close()

    trial = _run(_grant_configured_trial(db, "u1", "agent-1", {"days": 7, "credit_cents": 0}))
    assert trial is None


# ── Regression tests for the audit fixes ──

def test_subscription_only_without_active_sub_is_free(db):
    """A subscription agent never draws on the wallet: without an active sub the
    charge is free (the access gate is what blocks the user, not settlement)."""
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="subscription", cost_multiplier=5.0))
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=10_000), db))
    assert r.end_user_charge_cents == 0
    assert r.strategy == "free"
    assert r.wallet_charge_cents == 0


def test_own_llm_override_param_forces_free(db):
    """own_llm=True short-circuits the probe (image path: user's own image key)."""
    db.tables["billing_configs"].append(_agent_cfg("agent:agent-1", strategy="credits", cost_multiplier=2.0))
    # probe says False — the override must still win
    async def _probe(user_id):
        return False
    pricing_mod._own_llm_probe = _probe
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=9999), db, own_llm=True))
    assert r.end_user_charge_cents == 0
    assert r.notes.get("own_llm") is True


def test_min_charge_zero_disables_floor(db):
    """min_charge_cents=0 must be honoured (not silently defaulted to 1)."""
    db.tables["billing_configs"].append(
        _agent_cfg("agent:agent-1", strategy="credits", cost_multiplier=1.0, min_charge_cents=0)
    )
    r = _run(resolve_charge(_agent(), "u1", Usage(provider_cost_cents=0), db))
    assert r.end_user_charge_cents == 0


def test_unset_cost_settings_default(db):
    """Raw (None) cost knobs fall back to the engine defaults at charge time."""
    from plugins.billing.pricing import _compute_charge, _DEFAULT_BILLING
    cfg = _agent_cfg("agent:agent-1", strategy="credits")  # no cost fields set
    assert _compute_charge(Usage(provider_cost_cents=50), cfg) == int(
        round(50 * _DEFAULT_BILLING["cost_multiplier"])
    )


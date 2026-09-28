"""Tests for the platform (marketplace) billing tier.

This file lives inside the platform add-on, so it is stripped from a public /
agent-only edition along with the rest of the package. It covers the pieces the
agent tier deliberately does NOT have: the platform fee split, app-wide ceilings,
the config merge, payout params, and the platform ledger.

Run with pytest, or standalone:  python -m plugins.admin.billing.test_platform_billing
"""

import asyncio
import json
import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(__file__)
ROOT = os.path.abspath(os.path.join(HERE, "..", "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from plugins.billing import extensions as ext   # noqa: E402
from plugins.admin.billing import store          # noqa: E402


class _DB:
    def __init__(self, path):
        self._path = path

    def _get_conn(self):
        c = sqlite3.connect(self._path)
        c.row_factory = sqlite3.Row
        return c


def _fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = _DB(path)
    conn = db._get_conn()
    conn.executescript(
        "CREATE TABLE agents (id TEXT PRIMARY KEY, admin_users TEXT);"
        "INSERT INTO agents VALUES ('a1', '[\"admin\"]');"
    )
    conn.commit()
    conn.close()
    # Make sure the platform extension is registered (idempotent).
    ext.load_billing_extensions()
    return db, path


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_split_is_zero_without_config():
    db, path = _fresh_db()
    try:
        store.ensure_schema(db)
        agent = {"id": "a1", "metadata": json.dumps({"llm_config": {"use_default": True}})}
        fee, earn = _run(ext.apply_split(db, agent, 175))
        assert (fee, earn) == (0, 175)  # no platform config yet → agent keeps all
    finally:
        os.remove(path)


def test_fee_split_and_ledger():
    db, path = _fresh_db()
    try:
        store.upsert_platform_config(db, {"platform_fee_pct": 20.0}, "admin")
        agent = {"id": "a1", "metadata": json.dumps({"llm_config": {"use_default": True}})}
        fee, earn = _run(ext.apply_split(db, agent, 175))
        assert (fee, earn) == (35, 140)
        _run(ext.apply_record(db, event_id="e1", agent_id="a1", user_id="u1",
                              end_user_charge_cents=175, deducted_cents=fee,
                              retained_cents=earn, interaction_id="i1"))
        conn = db._get_conn()
        row = conn.execute("SELECT platform_fee_cents, agent_earnings_cents FROM platform_ledger WHERE usage_event_id='e1'").fetchone()
        conn.close()
        assert row and row["platform_fee_cents"] == 35 and row["agent_earnings_cents"] == 140
    finally:
        os.remove(path)


def test_config_merge_applies_ceilings():
    db, path = _fresh_db()
    try:
        store.upsert_platform_config(db, {
            "allowed_strategies": json.dumps(["credits", "per_message"]),
            "platform_fee_pct": 10.0,
        }, "admin")
        merged = _run(ext.apply_config_augment(db, "a1", {"strategy": "credits"}))
        assert merged["allowed_strategies"] == ["credits", "per_message"]
        assert merged["strategy"] == "credits"
    finally:
        os.remove(path)


def test_validate_rejects_disallowed_strategy():
    db, path = _fresh_db()
    try:
        store.upsert_platform_config(db, {"allowed_strategies": json.dumps(["credits"])}, "admin")
        try:
            _run(ext.apply_agent_config_validation(db, "a1", {"strategy": "subscription"}))
            assert False, "should have rejected 'subscription'"
        except AssertionError:
            raise
        except Exception as e:
            assert getattr(e, "status_code", None) == 400 or \
                "not enabled" in str(getattr(e, "detail", "")).lower()
    finally:
        os.remove(path)


def test_validate_accepts_allowed_strategy_combination():
    db, path = _fresh_db()
    try:
        store.upsert_platform_config(
            db,
            {"allowed_strategies": json.dumps(["trial", "credits"])},
            "admin",
        )
        _run(ext.apply_agent_config_validation(
            db, "a1", {"strategy": "trial,credits"}
        ))
    finally:
        os.remove(path)


def test_validate_rejects_one_disallowed_strategy_in_combination():
    db, path = _fresh_db()
    try:
        store.upsert_platform_config(
            db,
            {"allowed_strategies": json.dumps(["trial", "credits"])},
            "admin",
        )
        try:
            _run(ext.apply_agent_config_validation(
                db, "a1", {"strategy": "trial,subscription"}
            ))
            assert False, "should have rejected 'subscription'"
        except AssertionError:
            raise
        except Exception as e:
            assert getattr(e, "status_code", None) == 400
    finally:
        os.remove(path)


def test_subscription_params_returns_fee():
    db, path = _fresh_db()
    try:
        store.upsert_platform_config(db, {"platform_fee_pct": 15.0}, "admin")
        payee, pct = _run(ext.apply_subscription_params(db, "a1", "stripe"))
        assert pct == 15.0  # payee is None here (no payout account onboarded)
    finally:
        os.remove(path)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1; print(f"  ok  {name}")
    print(f"\n{n} platform-billing tests passed")

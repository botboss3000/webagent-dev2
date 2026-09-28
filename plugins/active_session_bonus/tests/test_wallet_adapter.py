"""Active Session Bonus settlement credits the same amount shown in the footer."""

import asyncio
import os
import sqlite3
import tempfile

import pytest

from plugins.active_session_bonus import wallet_adapter
from plugins.billing import wallet


class FileDB:
    """Small LocalBackend-compatible database for the production wallet helper."""

    _billing_ready = True

    def __init__(self, path):
        self.path = path

    def _get_conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE wallets (
            id TEXT PRIMARY KEY, owner_type TEXT NOT NULL, owner_id TEXT NOT NULL,
            balance_cents INTEGER NOT NULL DEFAULT 0,
            hold_cents INTEGER NOT NULL DEFAULT 0,
            currency TEXT NOT NULL DEFAULT 'usd',
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            updated_at TEXT NOT NULL DEFAULT (datetime('now')),
            UNIQUE(owner_type, owner_id, currency)
        );
        CREATE TABLE wallet_transactions (
            id TEXT PRIMARY KEY, wallet_id TEXT NOT NULL, delta_cents INTEGER NOT NULL,
            kind TEXT NOT NULL, ref_id TEXT, note TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()
    yield FileDB(path)
    os.unlink(path)


def run(coro):
    return asyncio.run(coro)


def test_bonus_credits_displayed_house_credits_once(db, monkeypatch):
    monkeypatch.setattr(wallet_adapter, "get_app_db", lambda: db)

    # The offer and its compact label are both ◇40. The wallet persists cents,
    # so it must gain 4 cents and the footer will render those as 40 credits.
    assert run(wallet_adapter.credit_bonus("user-1", "offer-1", 40)) == "active-session-bonus:offer-1"
    balance = run(wallet.get_balance(db, "user-1"))
    assert balance.balance_cents == 4
    assert balance.available_cents * 10 == 40

    # Retry after a worker interruption: the ledger reference prevents a second credit.
    assert run(wallet_adapter.credit_bonus("user-1", "offer-1", 40)) == "active-session-bonus:offer-1"
    balance = run(wallet.get_balance(db, "user-1"))
    assert balance.balance_cents == 4
    conn = db._get_conn()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM wallet_transactions WHERE ref_id=?",
            ("active-session-bonus:offer-1",),
        ).fetchone()[0] == 1
    finally:
        conn.close()

"""Idempotent bridge from displayed house credits to the account wallet."""
from __future__ import annotations

from app.db import get_app_db
from plugins.billing import wallet

# The footer displays ten house credits for every cents-based wallet unit.
HOUSE_CREDITS_PER_WALLET_CENT = 10


async def credit_bonus(user_id: str, offer_id: str, amount: int):
    """Credit a displayed house-credit reward once into the cents wallet."""
    ref = f"active-session-bonus:{offer_id}"
    db = get_app_db()
    # SQLite is the normal install. A prior ledger row makes recovery after a
    # process crash safe even if the offer had not yet been marked claimed.
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT 1 FROM wallet_transactions WHERE ref_id=? LIMIT 1", (ref,)
            ).fetchone()
            if row:
                return ref
        finally:
            conn.close()

    wallet_cents = max(0, int(amount) // HOUSE_CREDITS_PER_WALLET_CENT)
    result = await wallet.credit(db, "user", user_id, wallet_cents, kind="earnings",
                                 ref_id=ref, note="Active session bonus")
    if result is None:
        raise RuntimeError("wallet credit unavailable")
    return ref

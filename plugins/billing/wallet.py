"""Credit wallets — purchase / preauthorize / settle / refund.

End-user wallets hold credits the user has bought. The agent loop preauthorizes
a worst-case estimate before calling the LLM (moves cents from balance to
hold), then settles the real cost when the stream finishes (releases hold,
debits the actual amount). Concurrent settles are safe because hold/balance
are mutated together inside a single transaction per call.

Agent-admin wallets are informational mirrors of what the agent has earned —
the real money lives in the agent's connected payment-processor account.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class WalletState:
    wallet_id: str
    owner_type: str
    owner_id: str
    balance_cents: int
    hold_cents: int
    currency: str = "usd"

    @property
    def available_cents(self) -> int:
        return max(0, self.balance_cents - self.hold_cents)


def _has_tables(db: Any) -> bool:
    from plugins.billing.pricing import _has_billing_tables
    return _has_billing_tables(db)


async def get_or_create_wallet(
    db: Any,
    owner_type: str,
    owner_id: str,
    currency: str = "usd",
) -> Optional[WalletState]:
    if not _has_tables(db):
        return None
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                row = conn.execute(
                    "SELECT * FROM wallets WHERE owner_type=? AND owner_id=? AND currency=?",
                    (owner_type, owner_id, currency),
                ).fetchone()
                if row is None:
                    wid = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO wallets (id, owner_type, owner_id, balance_cents, hold_cents, currency) VALUES (?, ?, ?, 0, 0, ?)",
                        (wid, owner_type, owner_id, currency),
                    )
                    conn.commit()
                    return WalletState(wid, owner_type, owner_id, 0, 0, currency)
                return WalletState(
                    row["id"], row["owner_type"], row["owner_id"],
                    int(row["balance_cents"]), int(row["hold_cents"]),
                    row["currency"],
                )
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            cli = db.get_raw_client()
            res = (cli.table("wallets")
                   .select("*")
                   .eq("owner_type", owner_type)
                   .eq("owner_id", owner_id)
                   .eq("currency", currency)
                   .limit(1).execute())
            if res.data:
                r = res.data[0]
                return WalletState(
                    r["id"], r["owner_type"], r["owner_id"],
                    int(r["balance_cents"] or 0), int(r.get("hold_cents") or 0),
                    r.get("currency") or "usd",
                )
            wid = str(uuid.uuid4())
            cli.table("wallets").insert({
                "id": wid,
                "owner_type": owner_type,
                "owner_id": owner_id,
                "balance_cents": 0,
                "hold_cents": 0,
                "currency": currency,
            }).execute()
            return WalletState(wid, owner_type, owner_id, 0, 0, currency)
    except Exception as e:
        logger.warning("get_or_create_wallet failed: %s", e)
    return None


def _record_tx(conn, wallet_id: str, delta_cents: int, kind: str, ref_id: Optional[str], note: Optional[str]) -> str:
    tx_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO wallet_transactions (id, wallet_id, delta_cents, kind, ref_id, note) VALUES (?, ?, ?, ?, ?, ?)",
        (tx_id, wallet_id, int(delta_cents), kind, ref_id, note),
    )
    return tx_id


async def debit(
    db: Any,
    user_id: str,
    amount_cents: int,
    kind: str = "usage",
    ref_id: Optional[str] = None,
    note: Optional[str] = None,
    currency: str = "usd",
) -> Optional[WalletState]:
    """Debit up to ``amount_cents`` from the user's wallet WITHOUT going
    negative. Returns the wallet after (balance may be drained to 0 but never
    below), or None when there is nothing to debit.

    Used by the post-hoc charge path (text runs + image generation). The
    pre-send access gate keeps most users above zero, but a charge that exceeds
    the remaining balance (e.g. a big model call after a small purchase) is
    clamped rather than pushed into debt."""
    if amount_cents <= 0:
        return None
    wallet = await get_or_create_wallet(db, "user", user_id, currency)
    if not wallet:
        return None
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                cur = conn.execute(
                    "UPDATE wallets SET balance_cents = balance_cents - ?, "
                    "updated_at = datetime('now') WHERE id=? AND balance_cents >= ?",
                    (amount_cents, wallet.wallet_id, amount_cents),
                )
                conn.commit()
                if cur.rowcount == 0:
                    # Partial: drain whatever is left (never negative).
                    row = conn.execute(
                        "SELECT balance_cents FROM wallets WHERE id=?",
                        (wallet.wallet_id,),
                    ).fetchone()
                    bal = int(row["balance_cents"]) if row else 0
                    actual = min(amount_cents, bal)
                    if actual > 0:
                        conn.execute(
                            "UPDATE wallets SET balance_cents = 0, updated_at = datetime('now') WHERE id=?",
                            (wallet.wallet_id,),
                        )
                        _record_tx(conn, wallet.wallet_id, -actual, kind, ref_id, note)
                        conn.commit()
                    wallet.balance_cents = max(0, bal - actual)
                    return wallet
                _record_tx(conn, wallet.wallet_id, -amount_cents, kind, ref_id, note)
                conn.commit()
                wallet.balance_cents -= amount_cents
                return wallet
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            cli = db.get_raw_client()
            actual = min(amount_cents, wallet.balance_cents)
            new_balance = wallet.balance_cents - actual
            cli.table("wallets").update({"balance_cents": new_balance}).eq("id", wallet.wallet_id).execute()
            if actual > 0:
                cli.table("wallet_transactions").insert({
                    "id": str(uuid.uuid4()),
                    "wallet_id": wallet.wallet_id,
                    "delta_cents": -int(actual),
                    "kind": kind,
                    "ref_id": ref_id,
                    "note": note,
                }).execute()
            wallet.balance_cents = new_balance
            return wallet
    except Exception as e:
        logger.error("wallet.debit failed: %s", e)
    return None


async def credit(
    db: Any,
    owner_type: str,
    owner_id: str,
    amount_cents: int,
    kind: str = "purchase",
    ref_id: Optional[str] = None,
    note: Optional[str] = None,
    currency: str = "usd",
) -> Optional[WalletState]:
    """Increase wallet balance. Used by webhook handlers on successful payment."""
    if amount_cents <= 0:
        return None
    wallet = await get_or_create_wallet(db, owner_type, owner_id, currency)
    if not wallet:
        return None
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                conn.execute(
                    "UPDATE wallets SET balance_cents = balance_cents + ?, updated_at = datetime('now') WHERE id=?",
                    (amount_cents, wallet.wallet_id),
                )
                _record_tx(conn, wallet.wallet_id, amount_cents, kind, ref_id, note)
                conn.commit()
                wallet.balance_cents += amount_cents
                return wallet
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            cli = db.get_raw_client()
            new_balance = wallet.balance_cents + amount_cents
            cli.table("wallets").update({"balance_cents": new_balance}).eq("id", wallet.wallet_id).execute()
            cli.table("wallet_transactions").insert({
                "id": str(uuid.uuid4()),
                "wallet_id": wallet.wallet_id,
                "delta_cents": int(amount_cents),
                "kind": kind,
                "ref_id": ref_id,
                "note": note,
            }).execute()
            wallet.balance_cents = new_balance
            return wallet
    except Exception as e:
        logger.error("wallet.credit failed: %s", e)
    return None


async def preauthorize(
    db: Any,
    user_id: str,
    amount_cents: int,
    ref_id: Optional[str] = None,
) -> Tuple[bool, Optional[WalletState]]:
    """Move funds from balance into hold. Returns (ok, wallet_after).

    Caller MUST follow with settle() or release() before another preauthorize."""
    if amount_cents <= 0:
        return True, None
    wallet = await get_or_create_wallet(db, "user", user_id)
    if not wallet:
        return False, None
    if wallet.available_cents < amount_cents:
        return False, wallet
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                # Atomic check + update guards against TOCTOU under SQLite WAL.
                cur = conn.execute(
                    "UPDATE wallets SET hold_cents = hold_cents + ?, updated_at = datetime('now') "
                    "WHERE id=? AND (balance_cents - hold_cents) >= ?",
                    (amount_cents, wallet.wallet_id, amount_cents),
                )
                if cur.rowcount == 0:
                    conn.commit()
                    return False, wallet
                _record_tx(conn, wallet.wallet_id, -amount_cents, "hold", ref_id, "preauthorize")
                conn.commit()
                wallet.hold_cents += amount_cents
                return True, wallet
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            cli = db.get_raw_client()
            new_hold = wallet.hold_cents + amount_cents
            cli.table("wallets").update({"hold_cents": new_hold}).eq("id", wallet.wallet_id).execute()
            cli.table("wallet_transactions").insert({
                "id": str(uuid.uuid4()),
                "wallet_id": wallet.wallet_id,
                "delta_cents": -int(amount_cents),
                "kind": "hold",
                "ref_id": ref_id,
                "note": "preauthorize",
            }).execute()
            wallet.hold_cents = new_hold
            return True, wallet
    except Exception as e:
        logger.error("wallet.preauthorize failed: %s", e)
    return False, wallet


async def settle(
    db: Any,
    user_id: str,
    hold_cents: int,
    actual_cents: int,
    ref_id: Optional[str] = None,
    note: Optional[str] = None,
) -> Optional[WalletState]:
    """Convert a hold into an actual debit. actual_cents may be lower than the
    hold (refund the difference) or equal. If higher (rare), we cap at the
    hold so we don't push balance negative."""
    if hold_cents <= 0 and actual_cents <= 0:
        return None
    wallet = await get_or_create_wallet(db, "user", user_id)
    if not wallet:
        return None
    debit = min(max(actual_cents, 0), max(hold_cents, 0))
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                conn.execute(
                    "UPDATE wallets SET balance_cents = balance_cents - ?, hold_cents = hold_cents - ?, "
                    "updated_at = datetime('now') WHERE id=?",
                    (debit, hold_cents, wallet.wallet_id),
                )
                if debit > 0:
                    _record_tx(conn, wallet.wallet_id, -debit, "usage", ref_id, note)
                released = hold_cents - debit
                if released > 0:
                    _record_tx(conn, wallet.wallet_id, released, "release", ref_id, "settle-release")
                conn.commit()
                wallet.balance_cents -= debit
                wallet.hold_cents -= hold_cents
                return wallet
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            cli = db.get_raw_client()
            new_balance = wallet.balance_cents - debit
            new_hold = max(0, wallet.hold_cents - hold_cents)
            cli.table("wallets").update({
                "balance_cents": new_balance,
                "hold_cents": new_hold,
            }).eq("id", wallet.wallet_id).execute()
            if debit > 0:
                cli.table("wallet_transactions").insert({
                    "id": str(uuid.uuid4()),
                    "wallet_id": wallet.wallet_id,
                    "delta_cents": -int(debit),
                    "kind": "usage",
                    "ref_id": ref_id,
                    "note": note,
                }).execute()
            released = hold_cents - debit
            if released > 0:
                cli.table("wallet_transactions").insert({
                    "id": str(uuid.uuid4()),
                    "wallet_id": wallet.wallet_id,
                    "delta_cents": int(released),
                    "kind": "release",
                    "ref_id": ref_id,
                    "note": "settle-release",
                }).execute()
            wallet.balance_cents = new_balance
            wallet.hold_cents = new_hold
            return wallet
    except Exception as e:
        logger.error("wallet.settle failed: %s", e)
    return None


async def release(
    db: Any,
    user_id: str,
    hold_cents: int,
    ref_id: Optional[str] = None,
) -> Optional[WalletState]:
    """Release a hold without debiting (e.g. LLM call failed)."""
    return await settle(db, user_id, hold_cents, 0, ref_id=ref_id, note="release-on-error")


async def get_balance(db: Any, user_id: str) -> Optional[WalletState]:
    return await get_or_create_wallet(db, "user", user_id)


async def list_transactions(db: Any, wallet_id: str, limit: int = 50) -> list:
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                rows = conn.execute(
                    "SELECT * FROM wallet_transactions WHERE wallet_id=? ORDER BY created_at DESC LIMIT ?",
                    (wallet_id, limit),
                ).fetchall()
                return [{k: r[k] for k in r.keys()} for r in rows]
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            res = (db.get_raw_client().table("wallet_transactions")
                   .select("*").eq("wallet_id", wallet_id)
                   .order("created_at", desc=True).limit(limit).execute())
            return res.data or []
    except Exception as e:
        logger.warning("list_transactions failed: %s", e)
    return []

"""Low-level data access for the platform (marketplace) billing tier.

This whole package is the optional platform add-on and is stripped from a
public / agent-only edition. It owns the platform-tier data, which lives in its
OWN tables (never in the core billing schema):

  * ``platform_billing_config`` — the platform-wide policy + the platform fee,
  * ``payment_accounts``        — payout (Connect) onboarding state,
  * ``platform_ledger``         — the platform's cut, per charge.

On SQLite these tables are self-installed at runtime by ``ensure_schema`` (the
agent tier's core schema knows nothing about them); on Supabase/Postgres they
come from ``migrations/021_billing_platform.sql``. The agent tier
(``plugins/billing``) never imports this package — the dependency only ever
points platform → agent.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

PLATFORM_ID = "platform"

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS platform_billing_config (
    id                         TEXT    PRIMARY KEY DEFAULT 'platform',
    strategy                   TEXT    NOT NULL DEFAULT 'free',
    allowed_strategies         TEXT    NOT NULL DEFAULT '[]',
    allowed_processors         TEXT    NOT NULL DEFAULT '[]',
    rate_card_default_llm      TEXT    NOT NULL DEFAULT '{}',
    rate_card_byo_llm          TEXT    NOT NULL DEFAULT '{}',
    cost_multiplier            REAL    NOT NULL DEFAULT 1,
    min_charge_cents           INTEGER NOT NULL DEFAULT 1,
    flat_image_cost_usd        REAL    NOT NULL DEFAULT 0.01,
    platform_fee_pct           REAL    NOT NULL DEFAULT 0,
    platform_fee_flat_cents    INTEGER NOT NULL DEFAULT 0,
    trial_config               TEXT    NOT NULL DEFAULT '{}',
    subscription_price_cents   INTEGER NOT NULL DEFAULT 0,
    currency                   TEXT    NOT NULL DEFAULT 'usd',
    updated_at                 TEXT,
    updated_by                 TEXT
);
CREATE TABLE IF NOT EXISTS payment_accounts (
    user_id              TEXT NOT NULL,
    processor            TEXT NOT NULL,
    external_account_id  TEXT,
    onboarding_complete  INTEGER NOT NULL DEFAULT 0,
    metadata             TEXT NOT NULL DEFAULT '{}',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, processor)
);
CREATE TABLE IF NOT EXISTS platform_ledger (
    id                     TEXT PRIMARY KEY,
    usage_event_id         TEXT,
    agent_id               TEXT,
    user_id                TEXT,
    end_user_charge_cents  INTEGER NOT NULL DEFAULT 0,
    platform_fee_cents     INTEGER NOT NULL DEFAULT 0,
    agent_earnings_cents   INTEGER NOT NULL DEFAULT 0,
    interaction_id         TEXT,
    created_at             TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_platform_ledger_agent ON platform_ledger (agent_id);
CREATE INDEX IF NOT EXISTS idx_platform_ledger_event ON platform_ledger (usage_event_id);
"""

_CONFIG_FIELDS = (
    "strategy", "allowed_strategies", "allowed_processors",
    "rate_card_default_llm", "rate_card_byo_llm",
    "cost_multiplier", "min_charge_cents", "flat_image_cost_usd",
    "platform_fee_pct", "platform_fee_flat_cents",
    "trial_config", "subscription_price_cents", "currency",
)


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


# ── Schema bootstrap (SQLite self-install + one-time config migration) ──────


def ensure_schema(db: Any) -> None:
    """Create the platform-only tables on SQLite the first time we touch them.
    No-op on Supabase (migrations/021_billing_platform.sql owns that). Guarded so
    it runs at most once per db handle. Never raises."""
    if db is None or getattr(db, "_platform_billing_ready", False):
        return
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                conn.executescript(_SCHEMA_SQL)
                conn.commit()
                _migrate_legacy_platform_row(conn)
                # Guarded ADD COLUMN for DBs created before the cost-based
                # pricing fields existed (SQLite has no ADD COLUMN IF NOT EXISTS).
                try:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(platform_billing_config)").fetchall()}
                    for _col, _sql in (
                        ("cost_multiplier", "ALTER TABLE platform_billing_config ADD COLUMN cost_multiplier REAL NOT NULL DEFAULT 1"),
                        ("min_charge_cents", "ALTER TABLE platform_billing_config ADD COLUMN min_charge_cents INTEGER NOT NULL DEFAULT 1"),
                        ("flat_image_cost_usd", "ALTER TABLE platform_billing_config ADD COLUMN flat_image_cost_usd REAL NOT NULL DEFAULT 0.01"),
                    ):
                        if _col not in cols:
                            conn.execute(_sql)
                    conn.commit()
                except Exception as _e:
                    logger.debug("platform billing column migration skipped: %s", _e)
            finally:
                conn.close()
    except Exception as e:
        logger.info("platform billing ensure_schema skipped: %s", e)
    try:
        setattr(db, "_platform_billing_ready", True)
    except Exception:
        pass


def _migrate_legacy_platform_row(conn) -> None:
    """Older builds stored the platform config in billing_configs (scope='platform')
    with the platform-fee columns. If that legacy row exists and the new table is
    empty, copy it across once so an upgrading deployment keeps its settings."""
    try:
        have = conn.execute("SELECT 1 FROM platform_billing_config WHERE id=? LIMIT 1", (PLATFORM_ID,)).fetchone()
        if have:
            return
        cols = {r[1] for r in conn.execute("PRAGMA table_info(billing_configs)").fetchall()}
        if "platform_fee_pct" not in cols:
            return  # already a clean (agent-only) schema — nothing to migrate
        legacy = conn.execute("SELECT * FROM billing_configs WHERE scope='platform'").fetchone()
        if not legacy:
            return
        d = {k: legacy[k] for k in legacy.keys()}
        fields = {f: d.get(f) for f in _CONFIG_FIELDS if d.get(f) is not None}
        cols2 = ["id"] + list(fields.keys())
        vals = [PLATFORM_ID] + list(fields.values())
        conn.execute(
            f"INSERT INTO platform_billing_config ({','.join(cols2)}) VALUES ({','.join(['?'] * len(cols2))})",
            vals,
        )
        conn.commit()
        logger.info("migrated legacy platform billing config into platform_billing_config")
    except Exception as e:
        logger.debug("legacy platform-config migration skipped: %s", e)


# ── Platform config ─────────────────────────────────────────────────────────


async def read_platform_config(db: Any) -> dict:
    ensure_schema(db)
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                r = conn.execute("SELECT * FROM platform_billing_config WHERE id=?", (PLATFORM_ID,)).fetchone()
                return {k: r[k] for k in r.keys()} if r else {}
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            res = db.get_raw_client().table("platform_billing_config").select("*").eq("id", PLATFORM_ID).limit(1).execute()
            return res.data[0] if res.data else {}
    except Exception as e:
        logger.debug("read_platform_config failed: %s", e)
    return {}


def upsert_platform_config(db: Any, fields: dict, updated_by: str) -> dict:
    ensure_schema(db)
    fields = {**fields, "updated_by": updated_by, "updated_at": _now_iso()}
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            row = conn.execute("SELECT id FROM platform_billing_config WHERE id=?", (PLATFORM_ID,)).fetchone()
            if row is None:
                cols = ["id"] + list(fields.keys())
                vals = [PLATFORM_ID] + list(fields.values())
                conn.execute(
                    f"INSERT INTO platform_billing_config ({','.join(cols)}) VALUES ({','.join(['?'] * len(cols))})",
                    vals,
                )
            else:
                sets = ",".join([f"{k}=?" for k in fields.keys()])
                conn.execute(f"UPDATE platform_billing_config SET {sets} WHERE id=?", list(fields.values()) + [PLATFORM_ID])
            conn.commit()
            r = conn.execute("SELECT * FROM platform_billing_config WHERE id=?", (PLATFORM_ID,)).fetchone()
            return {k: r[k] for k in r.keys()}
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        existing = cli.table("platform_billing_config").select("id").eq("id", PLATFORM_ID).limit(1).execute()
        if existing.data:
            cli.table("platform_billing_config").update(fields).eq("id", PLATFORM_ID).execute()
        else:
            cli.table("platform_billing_config").insert({"id": PLATFORM_ID, **fields}).execute()
        res = cli.table("platform_billing_config").select("*").eq("id", PLATFORM_ID).limit(1).execute()
        return res.data[0] if res.data else {"id": PLATFORM_ID, **fields}
    return {"id": PLATFORM_ID, **fields}


async def fee_params(db: Any) -> Tuple[float, int]:
    row = await read_platform_config(db)
    return float(row.get("platform_fee_pct") or 0), int(row.get("platform_fee_flat_cents") or 0)


def split(end_user_charge_cents: int, pct: float, flat: int) -> Tuple[int, int]:
    """(fee_cents, earnings_cents) — the platform's cut and what the agent keeps."""
    if end_user_charge_cents <= 0:
        return 0, 0
    fee = int(round(end_user_charge_cents * pct / 100.0)) + int(flat or 0)
    fee = max(0, min(fee, end_user_charge_cents))
    return fee, end_user_charge_cents - fee


# ── Platform ledger (per-charge cut) ────────────────────────────────────────


def record_ledger(
    db: Any,
    *,
    event_id: str,
    agent_id: str,
    user_id: str,
    end_user_charge_cents: int,
    platform_fee_cents: int,
    agent_earnings_cents: int,
    interaction_id: Optional[str] = None,
) -> None:
    ensure_schema(db)
    row_id = str(uuid.uuid4())
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO platform_ledger (id, usage_event_id, agent_id, user_id, "
                    "end_user_charge_cents, platform_fee_cents, agent_earnings_cents, interaction_id) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (row_id, event_id, agent_id, user_id, int(end_user_charge_cents),
                     int(platform_fee_cents), int(agent_earnings_cents), interaction_id),
                )
                conn.commit()
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            db.get_raw_client().table("platform_ledger").insert({
                "id": row_id, "usage_event_id": event_id, "agent_id": agent_id, "user_id": user_id,
                "end_user_charge_cents": int(end_user_charge_cents),
                "platform_fee_cents": int(platform_fee_cents),
                "agent_earnings_cents": int(agent_earnings_cents),
                "interaction_id": interaction_id,
            }).execute()
    except Exception as e:
        logger.debug("record_ledger skipped: %s", e)


# ── Payout (Connect) accounts ───────────────────────────────────────────────


def get_payee_account_id(db: Any, agent_id: str, processor: str) -> Optional[str]:
    """The agent admin's external (Connect) account id for a processor."""
    ensure_schema(db)
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            agent = conn.execute("SELECT admin_users FROM agents WHERE id=?", (agent_id,)).fetchone()
            if not agent:
                return None
            try:
                admins = json.loads(agent["admin_users"] or "[]")
            except Exception:
                admins = []
            for uid in admins:
                row = conn.execute(
                    "SELECT external_account_id FROM payment_accounts WHERE user_id=? AND processor=?",
                    (uid, processor),
                ).fetchone()
                if row and row["external_account_id"]:
                    return row["external_account_id"]
        finally:
            conn.close()
    return None


def upsert_payment_account(db: Any, user_id: str, processor: str, account_id: str, complete: bool) -> None:
    ensure_schema(db)
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT * FROM payment_accounts WHERE user_id=? AND processor=?",
                (user_id, processor),
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO payment_accounts (user_id, processor, external_account_id, onboarding_complete) VALUES (?,?,?,?)",
                    (user_id, processor, account_id, 1 if complete else 0),
                )
            else:
                conn.execute(
                    "UPDATE payment_accounts SET external_account_id=?, onboarding_complete=?, updated_at=datetime('now') WHERE user_id=? AND processor=?",
                    (account_id, 1 if complete else 0, user_id, processor),
                )
            conn.commit()
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        existing = (cli.table("payment_accounts").select("*")
                    .eq("user_id", user_id).eq("processor", processor).limit(1).execute())
        fields = {"external_account_id": account_id, "onboarding_complete": 1 if complete else 0}
        if existing.data:
            (cli.table("payment_accounts").update(fields)
             .eq("user_id", user_id).eq("processor", processor).execute())
        else:
            cli.table("payment_accounts").insert({"user_id": user_id, "processor": processor, **fields}).execute()


def list_payment_accounts(db: Any, user_id: str) -> list:
    ensure_schema(db)
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            rows = conn.execute("SELECT * FROM payment_accounts WHERE user_id=?", (user_id,)).fetchall()
            return [{k: r[k] for k in r.keys()} for r in rows]
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        res = (db.get_raw_client().table("payment_accounts").select("*").eq("user_id", user_id).execute())
        return res.data or []
    return []

"""Plugin-owned durable storage for Active Session Bonus offers."""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from typing import Any, Mapping

from app.runtime_mode import data_root
from .config import BonusConfig, DEFAULTS
from .qualification import eligible_offer, iso, parse_time, remaining_amount, utcnow, whole_house_credits

_SCHEMA = """
CREATE TABLE IF NOT EXISTS active_session_bonus_offers (
 id TEXT PRIMARY KEY, user_id TEXT NOT NULL, session_id TEXT NOT NULL,
 task_id TEXT NOT NULL, source_closer_interaction_id TEXT NOT NULL,
 offer_ordinal INTEGER NOT NULL, initial_amount INTEGER NOT NULL,
 offered_at TEXT NOT NULL, activated_at TEXT, paused_amount INTEGER,
 candidate_user_interaction_id TEXT, qualified_at TEXT, claimed_at TEXT,
 claimed_amount INTEGER, wallet_transaction_ref TEXT, status TEXT NOT NULL,
 baseline_task_revision INTEGER, baseline_work_revision INTEGER,
 baseline_done_steps INTEGER NOT NULL DEFAULT 0,
 baseline_evidence_count INTEGER NOT NULL DEFAULT 0, expires_at TEXT NOT NULL,
 failure_reason TEXT, flag TEXT, policy_json TEXT,
 UNIQUE(user_id, source_closer_interaction_id)
);
CREATE INDEX IF NOT EXISTS idx_active_bonus_user_session ON active_session_bonus_offers(user_id, session_id);
CREATE INDEX IF NOT EXISTS idx_active_bonus_status ON active_session_bonus_offers(status);
CREATE TABLE IF NOT EXISTS active_session_bonus_task_stats (
 user_id TEXT NOT NULL, task_id TEXT NOT NULL, offers_issued INTEGER NOT NULL DEFAULT 0,
 claimed_amount INTEGER NOT NULL DEFAULT 0, updated_at TEXT NOT NULL,
 PRIMARY KEY(user_id, task_id)
);
CREATE TABLE IF NOT EXISTS active_session_bonus_daily_stats (
 user_id TEXT NOT NULL, reward_day TEXT NOT NULL, claimed_amount INTEGER NOT NULL DEFAULT 0,
 PRIMARY KEY(user_id, reward_day)
);
"""


class BonusStore:
    """Small SQLite authority, deliberately independent of application tables."""
    def __init__(self, path: str | Path | None = None, config: BonusConfig = DEFAULTS):
        self.path = str(path or (data_root() / "active_session_bonus.db"))
        self.config = config
        self._lock = threading.RLock()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_SCHEMA)
            columns = {row[1] for row in conn.execute("PRAGMA table_info(active_session_bonus_offers)")}
            if "flag" not in columns:
                conn.execute("ALTER TABLE active_session_bonus_offers ADD COLUMN flag TEXT")
            if "policy_json" not in columns:
                conn.execute("ALTER TABLE active_session_bonus_offers ADD COLUMN policy_json TEXT")

    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @staticmethod
    def _legacy_policy(config: BonusConfig) -> dict[str, Any]:
        return {"flag": "needs_input", "max_bonus": config.base_bonus,
                "resolution_seconds": config.offer_expiry_seconds, "resolution_action": "legacy"}

    @classmethod
    def _row(cls, row, config: BonusConfig = DEFAULTS):
        if not row:
            return None
        result = dict(row)
        try:
            policy = json.loads(result.get("policy_json") or "")
        except (TypeError, json.JSONDecodeError):
            policy = None
        result["policy"] = policy if isinstance(policy, dict) else cls._legacy_policy(config)
        return result

    def _daily_claimed(self, conn, user_id: str, day: str) -> int:
        row = conn.execute("SELECT claimed_amount FROM active_session_bonus_daily_stats WHERE user_id=? AND reward_day=?", (user_id, day)).fetchone()
        return int(row[0]) if row else 0

    def create_offer(self, *, user_id: str, session_id: str, closer_id: str, snapshot: Mapping[str, Any], policy: Mapping[str, Any] | None = None, now=None) -> dict | None:
        """Create exactly once per durable closer and freeze the chosen policy."""
        now = now or utcnow(); task_id = str(snapshot.get("task_id") or "")
        if not task_id:
            return None
        policy = dict(policy or self._legacy_policy(self.config))
        offered_at = iso(now)
        expiry = iso(now + timedelta(seconds=int(policy["resolution_seconds"])))
        policy_config = replace(self.config, base_bonus=int(policy["max_bonus"]))
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                old = conn.execute("SELECT * FROM active_session_bonus_offers WHERE user_id=? AND source_closer_interaction_id=?", (user_id, closer_id)).fetchone()
                if old:
                    conn.commit(); return self._row(old, self.config)
                stats = conn.execute("SELECT offers_issued, claimed_amount FROM active_session_bonus_task_stats WHERE user_id=? AND task_id=?", (user_id, task_id)).fetchone()
                issued, task_claimed = (int(stats[0]), int(stats[1])) if stats else (0, 0)
                if issued >= self.config.offers_per_task_limit:
                    conn.commit(); return None
                ordinal = issued + 1
                amount = eligible_offer(ordinal, task_claimed=task_claimed, daily_claimed=self._daily_claimed(conn, user_id, now.date().isoformat()), config=policy_config)
                if amount <= 0:
                    conn.commit(); return None
                offer_id = str(uuid.uuid4())
                conn.execute("""INSERT INTO active_session_bonus_offers
                  (id,user_id,session_id,task_id,source_closer_interaction_id,offer_ordinal,initial_amount,offered_at,status,
                   baseline_task_revision,baseline_work_revision,baseline_done_steps,baseline_evidence_count,expires_at,flag,policy_json)
                  VALUES (?,?,?,?,?,?,?,?, 'offered',?,?,?,?,?,?,?)""", (
                    offer_id, user_id, session_id, task_id, closer_id, ordinal, amount, offered_at,
                    int(snapshot.get("task_revision") or 0), int(snapshot.get("work_revision") or 0),
                    int(snapshot.get("done_step_count") or 0), int(snapshot.get("evidence_count") or 0), expiry,
                    policy["flag"], json.dumps(policy, sort_keys=True),
                ))
                conn.execute("""INSERT INTO active_session_bonus_task_stats(user_id,task_id,offers_issued,claimed_amount,updated_at)
                  VALUES (?,?,1,0,?) ON CONFLICT(user_id,task_id) DO UPDATE SET offers_issued=offers_issued+1,updated_at=excluded.updated_at""", (user_id, task_id, offered_at))
                row = conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=?", (offer_id,)).fetchone()
                conn.commit(); return self._row(row, self.config)
            except Exception:
                conn.rollback(); raise

    def _present(self, offer: dict | None) -> dict | None:
        if not offer:
            return None
        offer["remaining_amount"] = remaining_amount(offer, self.config)
        return offer

    def offer(self, offer_id: str, user_id: str | None = None) -> dict | None:
        with self._connect() as conn:
            args = (offer_id, user_id) if user_id else (offer_id,)
            row = conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=?" + (" AND user_id=?" if user_id else ""), args).fetchone()
            return self._present(self._row(row, self.config))

    def offers_for_session(self, user_id: str, session_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM active_session_bonus_offers WHERE user_id=? AND session_id=? ORDER BY offered_at DESC", (user_id, session_id)).fetchall()
            return [self._present(self._row(r, self.config)) for r in rows]

    def offers_for_user(self, user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM active_session_bonus_offers WHERE user_id=? AND status NOT IN ('expired','superseded','rejected') ORDER BY offered_at DESC", (user_id,)).fetchall()
            return [self._present(self._row(r, self.config)) for r in rows]

    def activate(self, offer_id: str, user_id: str, now=None) -> dict | None:
        now = now or utcnow()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=? AND user_id=?", (offer_id, user_id)).fetchone(), self.config)
                if not row:
                    conn.commit(); return None
                if row["status"] in {"activated", "awaiting_progress", "qualified", "claimed"}:
                    conn.commit(); return self._present(row)
                if row["status"] != "offered" or now >= parse_time(row["expires_at"]):
                    conn.execute("UPDATE active_session_bonus_offers SET status='expired', failure_reason='offer expired' WHERE id=?", (offer_id,)); conn.commit(); return self.offer(offer_id, user_id)
                amount = remaining_amount(row, self.config, now)
                if amount <= 0:
                    conn.execute("UPDATE active_session_bonus_offers SET status='expired', failure_reason='offer decayed to zero' WHERE id=?", (offer_id,)); conn.commit(); return self.offer(offer_id, user_id)
                conn.execute("UPDATE active_session_bonus_offers SET paused_amount=?, activated_at=?, status='activated' WHERE id=?", (amount, iso(now), offer_id))
                row = self._row(conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=?", (offer_id,)).fetchone(), self.config)
                conn.commit(); return self._present(row)
            except Exception:
                conn.rollback(); raise

    def mark_candidate(self, offer_id: str, interaction_id: str) -> bool:
        with self._lock, self._connect() as conn:
            cur = conn.execute("UPDATE active_session_bonus_offers SET candidate_user_interaction_id=?, status='awaiting_progress' WHERE id=? AND status='activated' AND candidate_user_interaction_id IS NULL", (interaction_id, offer_id))
            return cur.rowcount == 1

    def terminalize(self, offer_id: str, status: str, reason: str | None = None) -> None:
        if status not in {"expired", "superseded", "rejected"}:
            raise ValueError("invalid terminal status")
        with self._connect() as conn:
            conn.execute("UPDATE active_session_bonus_offers SET status=?, failure_reason=? WHERE id=? AND status NOT IN ('claimed',?)", (status, reason, offer_id, status))

    def reserve_claim(self, offer_id: str, now=None) -> dict | None:
        """Reserve amount/caps before wallet credit; `qualified` is the durable reservation."""
        now = now or utcnow(); day = now.date().isoformat()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=?", (offer_id,)).fetchone(), self.config)
                if not row or row["status"] not in {"activated", "awaiting_progress", "qualified"}:
                    conn.commit(); return None
                if row["status"] == "qualified":
                    conn.commit(); return self._present(row)
                stats = conn.execute("SELECT claimed_amount FROM active_session_bonus_task_stats WHERE user_id=? AND task_id=?", (row["user_id"], row["task_id"])).fetchone()
                task_claimed = int(stats[0]) if stats else 0; daily = self._daily_claimed(conn, row["user_id"], day)
                amount = whole_house_credits(min(int(row["paused_amount"] or 0), self.config.per_task_claim_cap - task_claimed, self.config.daily_claim_cap - daily))
                if amount <= 0:
                    conn.execute("UPDATE active_session_bonus_offers SET status='rejected', failure_reason='claim cap reached' WHERE id=?", (offer_id,)); conn.commit(); return None
                conn.execute("UPDATE active_session_bonus_offers SET status='qualified', qualified_at=?, claimed_amount=?, wallet_transaction_ref=? WHERE id=?", (iso(now), amount, f"active-session-bonus:{offer_id}", offer_id))
                row = self._row(conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=?", (offer_id,)).fetchone(), self.config); conn.commit(); return self._present(row)
            except Exception:
                conn.rollback(); raise

    def mark_claimed(self, offer_id: str, now=None) -> bool:
        now = now or utcnow()
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(conn.execute("SELECT * FROM active_session_bonus_offers WHERE id=?", (offer_id,)).fetchone(), self.config)
                if not row:
                    conn.commit(); return False
                if row["status"] == "claimed":
                    conn.commit(); return True
                if row["status"] != "qualified":
                    conn.commit(); return False
                amount = int(row["claimed_amount"] or 0); day = now.date().isoformat()
                conn.execute("UPDATE active_session_bonus_offers SET status='claimed', claimed_at=? WHERE id=?", (iso(now), offer_id))
                conn.execute("UPDATE active_session_bonus_task_stats SET claimed_amount=claimed_amount+?, updated_at=? WHERE user_id=? AND task_id=?", (amount, iso(now), row["user_id"], row["task_id"]))
                conn.execute("INSERT INTO active_session_bonus_daily_stats(user_id,reward_day,claimed_amount) VALUES (?,?,?) ON CONFLICT(user_id,reward_day) DO UPDATE SET claimed_amount=claimed_amount+excluded.claimed_amount", (row["user_id"], day, amount))
                conn.commit(); return True
            except Exception:
                conn.rollback(); raise

    def active_offers(self) -> list[dict]:
        with self._connect() as conn:
            return [self._row(r, self.config) for r in conn.execute("SELECT * FROM active_session_bonus_offers WHERE status IN ('offered','activated','awaiting_progress','qualified')").fetchall()]

    def expire_due(self, now=None) -> None:
        current = now or utcnow()
        with self._connect() as conn:
            for raw in conn.execute("SELECT * FROM active_session_bonus_offers WHERE status='offered'").fetchall():
                offer = self._row(raw, self.config)
                if current >= parse_time(offer["expires_at"]) or remaining_amount(offer, self.config, current) <= 0:
                    conn.execute("UPDATE active_session_bonus_offers SET status='expired', failure_reason='offer expired' WHERE id=?", (offer["id"],))


_STORE: BonusStore | None = None

def get_store() -> BonusStore:
    global _STORE
    if _STORE is None:
        _STORE = BonusStore()
    return _STORE

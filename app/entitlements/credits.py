"""Durable app-credit allowance reservation and reconciliation.

Tier allowances are a separate house-credit plane from cash wallets. Anonymous
and Pending share the coarse anonymous source binding; registered tiers remain
account scoped. Reservation ids are assistant interaction ids, so retries are
idempotent and actual usage can reconcile after the immutable usage event exists.
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import uuid
from typing import Any, Mapping

STARTER_TIER = "anonymous-starter-v1"
NETWORK_TIER = "anonymous-network-cap-v1"
STARTER_LIMIT = 250.0
NETWORK_LIMIT = 1250.0


def _week_key(now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now(_dt.timezone.utc)
    year, week, _ = now.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _source_for_subject(user_id: str) -> str:
    from app.api.rate_limit import _source_for_user
    source = str(_source_for_user(user_id) or "")
    return source if source and source != "unknown" else ""


def allowance_entries(user_id: str, capabilities: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Build the allowance rows charged by one model call."""
    tier = dict(capabilities.get("tier") or {})
    limits = dict(capabilities.get("limits") or {})
    slug = str(tier.get("slug") or "anonymous")
    if slug in {"anonymous", "pending"}:
        source = _source_for_subject(str(user_id))
        if not source:
            raise PermissionError("anonymous_source_binding_required")
        return [
            {"scope_kind": "network", "principal_key": source, "tier_id": STARTER_TIER,
             "tier_revision": 1, "period_kind": "one_time", "period_key": "starter",
             "limit_units": STARTER_LIMIT},
            {"scope_kind": "network", "principal_key": source, "tier_id": NETWORK_TIER,
             "tier_revision": 1, "period_kind": "one_time", "period_key": "network",
             "limit_units": NETWORK_LIMIT},
        ]
    weekly = limits.get("weekly_credit_limit")
    if weekly is None:
        return []
    try:
        weekly = float(weekly)
    except (TypeError, ValueError):
        return []
    if weekly <= 0:
        return []
    renews = bool(limits.get("weekly_credit_renews", True))
    return [{"scope_kind": "account", "principal_key": str(user_id),
             "tier_id": str(tier.get("id") or slug), "tier_revision": 1,
             "period_kind": "weekly" if renews else "one_time",
             "period_key": _week_key() if renews else "starter", "limit_units": weekly}]


def _conn_for(db: Any):
    from app.db import get_app_db
    cdb = get_app_db()
    if not hasattr(cdb, "_get_conn"):
        raise RuntimeError("tier credit ledger requires transactional app database")
    return cdb._get_conn()


def _reserve_sync(db: Any, reservation_id: str, entries: list[dict[str, Any]], units: float) -> dict:
    if units <= 0 or not entries:
        return {"allowed": True, "reservation_id": reservation_id, "entries": []}
    conn = _conn_for(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM tier_credit_reservations WHERE reservation_id=? LIMIT 1", (reservation_id,)).fetchone():
            conn.commit(); return {"allowed": True, "reservation_id": reservation_id, "idempotent": True}
        rows: list[str] = []
        for entry in entries:
            row = conn.execute(
                "SELECT id,limit_units,reserved_units,used_units FROM tier_credit_allowances WHERE scope_kind=? AND principal_key=? AND tier_id=? AND tier_revision=? AND period_kind=? AND period_key=? AND reset_generation=0",
                (entry["scope_kind"], entry["principal_key"], entry["tier_id"], entry["tier_revision"], entry["period_kind"], entry["period_key"]),
            ).fetchone()
            if not row:
                allowance_id = str(uuid.uuid4())
                conn.execute("INSERT INTO tier_credit_allowances (id,scope_kind,principal_key,tier_id,tier_revision,period_kind,period_key,limit_units) VALUES (?,?,?,?,?,?,?,?)",
                             (allowance_id, entry["scope_kind"], entry["principal_key"], entry["tier_id"], entry["tier_revision"], entry["period_kind"], entry["period_key"], entry["limit_units"]))
                row = (allowance_id, entry["limit_units"], 0.0, 0.0)
            allowance_id, limit_units, reserved_units, used_units = row
            if limit_units is not None and float(used_units) + float(reserved_units) + units > float(limit_units) + 1e-9:
                conn.rollback()
                return {"allowed": False, "reservation_id": reservation_id,
                        "scope": "anonymous_network_credit" if entry["tier_id"] == NETWORK_TIER else "tier_credit",
                        "limit_units": float(limit_units), "used_units": float(used_units), "reserved_units": float(reserved_units)}
            rows.append(str(allowance_id))
        for allowance_id in rows:
            conn.execute("UPDATE tier_credit_allowances SET reserved_units=reserved_units+?,updated_at=datetime('now') WHERE id=?", (units, allowance_id))
            conn.execute("INSERT INTO tier_credit_reservations (reservation_id,allowance_id,estimated_units,status) VALUES (?,?,?,'reserved')", (reservation_id, allowance_id, units))
        conn.commit(); return {"allowed": True, "reservation_id": reservation_id, "entries": rows}
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


async def reserve_credits(db: Any, reservation_id: str, user_id: str, capabilities: Mapping[str, Any], units: float) -> dict:
    return await asyncio.to_thread(_reserve_sync, db, reservation_id, allowance_entries(user_id, capabilities), max(0.0, float(units)))


def _reconcile_sync(db: Any, reservation_id: str, actual_units: float) -> dict:
    conn = _conn_for(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute("SELECT allowance_id,estimated_units,status FROM tier_credit_reservations WHERE reservation_id=?", (reservation_id,)).fetchall()
        if not rows:
            conn.commit(); return {"reconciled": False, "reason": "reservation_missing"}
        if all(str(row[2]) == "reconciled" for row in rows):
            conn.commit(); return {"reconciled": True, "idempotent": True}
        actual = max(0.0, float(actual_units))
        for allowance_id, estimated, status in rows:
            if str(status) == "reserved":
                conn.execute("UPDATE tier_credit_allowances SET reserved_units=MAX(0,reserved_units-?),used_units=used_units+?,updated_at=datetime('now') WHERE id=?", (float(estimated), actual, allowance_id))
                conn.execute("UPDATE tier_credit_reservations SET actual_units=?,status='reconciled',reconciled_at=datetime('now') WHERE reservation_id=? AND allowance_id=?", (actual, reservation_id, allowance_id))
        conn.commit(); return {"reconciled": True, "actual_units": actual}
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


async def reconcile_credits(db: Any, reservation_id: str, actual_units: float) -> dict:
    return await asyncio.to_thread(_reconcile_sync, db, reservation_id, actual_units)


def _release_sync(db: Any, reservation_id: str) -> None:
    conn = _conn_for(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for allowance_id, estimated, status in conn.execute("SELECT allowance_id,estimated_units,status FROM tier_credit_reservations WHERE reservation_id=?", (reservation_id,)).fetchall():
            if str(status) == "reserved":
                conn.execute("UPDATE tier_credit_allowances SET reserved_units=MAX(0,reserved_units-?),updated_at=datetime('now') WHERE id=?", (float(estimated), allowance_id))
                conn.execute("UPDATE tier_credit_reservations SET status='released',reconciled_at=datetime('now') WHERE reservation_id=? AND allowance_id=?", (reservation_id, allowance_id))
        conn.commit()
    except Exception:
        conn.rollback(); raise
    finally:
        conn.close()


async def release_credits(db: Any, reservation_id: str) -> None:
    await asyncio.to_thread(_release_sync, db, reservation_id)

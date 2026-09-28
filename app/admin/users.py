"""
Admin user management endpoints.

POST   /admin/users/{user_id}/set-admin   — grant or revoke admin access
POST   /admin/users/{user_id}/approve     — approve a pending user
POST   /admin/users/{user_id}/revoke      — revoke approval (lock account)
DELETE /admin/users/{user_id}             — delete a user account
GET    /admin/users/{user_id}/profile     — fetch user profile (admin only)
GET    /admin/users                       — list all user profiles (admin only)
GET    /admin/users/stats                 — list users with full stats (admin only)
POST   /admin/users/{user_id}/credits     — set trial or paid credits (admin only)
"""

import asyncio
import datetime as dt
import logging
from typing import Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.auth.users import (
    list_users,
    set_user_approval,
    delete_user as auth_delete_user,
    get_user_by_id,
)
from app.db import get_app_db, get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/users", tags=["admin-users"])


class SetAdminRequest(BaseModel):
    requesting_user_id: str   # must be an existing admin
    is_admin: bool


class AdjustCreditsRequest(BaseModel):
    requesting_user_id: str
    credit_type: Literal["trial", "paid"]
    balance_credits: int = Field(ge=0)


class AnonymousCooldownRequest(BaseModel):
    requesting_user_id: str
    seconds: int = Field(default=900, ge=1, le=86400)
    reason: str = Field(default="Manual admin cooldown", min_length=1, max_length=200)


class AnonymousControlRequest(BaseModel):
    requesting_user_id: str


async def _require_admin(db, user_id: str) -> None:
    # Routes through the shared chokepoint so 'open' access mode grants the
    # bootstrap admin to a tokenless tunnel caller; non-open modes still require
    # a real DB admin id. See app.auth.identity.resolve_admin_uid. (db arg kept
    # for signature compatibility with existing call sites.)
    from app.auth.identity import resolve_admin_uid
    if not await resolve_admin_uid(user_id):
        raise HTTPException(status_code=403, detail="Admin access required.")


def _trial_is_current(row: dict) -> bool:
    """A grant remains editable until its expiry, including at zero credits."""
    expires_at = row.get("expires_at")
    if not expires_at:
        return True
    try:
        expires = expires_at
        if isinstance(expires, str):
            expires = dt.datetime.fromisoformat(expires.replace("Z", "+00:00"))
        now = dt.datetime.now(tz=expires.tzinfo) if expires.tzinfo else dt.datetime.utcnow()
        return expires >= now
    except (TypeError, ValueError):
        # Match billing enforcement: malformed legacy expiries do not disable a grant.
        return True


async def _billing_summaries(db) -> dict[str, dict]:
    """Return app-plane paid wallets and current trial grants keyed by user."""
    from plugins.billing.pricing import _has_billing_tables

    if not _has_billing_tables(db):
        return {}
    wallets: list[dict] = []
    trials: list[dict] = []
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                wallets = [dict(r) for r in conn.execute(
                    "SELECT owner_id AS user_id, balance_cents, hold_cents, currency "
                    "FROM wallets WHERE owner_type='user' AND currency='usd'"
                ).fetchall()]
                trials = [dict(r) for r in conn.execute(
                    "SELECT id, user_id, agent_id, started_at, expires_at, "
                    "credit_cents, remaining_cents FROM trials"
                ).fetchall()]
            finally:
                conn.close()
        elif hasattr(db, "get_raw_client"):
            cli = db.get_raw_client()
            wallets = (cli.table("wallets")
                       .select("owner_id,balance_cents,hold_cents,currency")
                       .eq("owner_type", "user").eq("currency", "usd")
                       .execute().data or [])
            trials = (cli.table("trials")
                      .select("id,user_id,agent_id,started_at,expires_at,credit_cents,remaining_cents")
                      .execute().data or [])
    except Exception as exc:
        logger.debug("Could not load user billing summaries: %s", exc)
        return {}

    summaries: dict[str, dict] = {}
    for row in wallets:
        uid = row.get("user_id") or row.get("owner_id")
        if not uid:
            continue
        summary = summaries.setdefault(uid, {})
        summary.update({
            "paid_credits": max(0, int(row.get("balance_cents") or 0)),
            "paid_hold_credits": max(0, int(row.get("hold_cents") or 0)),
            "currency": row.get("currency") or "usd",
        })
    for row in trials:
        uid = row.get("user_id")
        if not uid or not _trial_is_current(row):
            continue
        summary = summaries.setdefault(uid, {})
        summary["has_trial_grant"] = True
        summary["trial_credits"] = summary.get("trial_credits", 0) + max(
            0, int(row.get("remaining_cents") or 0)
        )

    for summary in summaries.values():
        summary.setdefault("paid_credits", 0)
        summary.setdefault("paid_hold_credits", 0)
        summary.setdefault("trial_credits", 0)
        summary.setdefault("has_trial_grant", False)
        summary.setdefault("currency", "usd")
        summary["billing_status"] = (
            "trial" if summary["trial_credits"] > 0
            else "paid" if summary["paid_credits"] > 0
            else "none"
        )
    return summaries


def _entitlement_time(value):
    if not value:
        return None
    if isinstance(value, dt.datetime):
        return value
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


async def _entitlement_summaries(db) -> dict[str, dict]:
    """Resolve the active assignment metadata used by the Users table.

    This intentionally reports product tier separately from ``is_admin``.  It
    mirrors resolver precedence without returning the tier policy or roster.
    """
    try:
        assignments = [dict(row) for row in await db.list_user_tier_assignments()]
        tiers = [dict(row) for row in await db.list_experience_tiers()]
    except Exception as exc:
        logger.debug("Could not load entitlement summaries: %s", exc)
        return {}
    tiers_by_id = {str(row.get("id")): row for row in tiers}
    now = dt.datetime.now(dt.timezone.utc)
    source_rank = {"manual": 4, "billing": 3, "import": 2, "default": 1, "system": 0}
    active: dict[str, list[dict]] = {}
    for row in assignments:
        starts, expires = _entitlement_time(row.get("starts_at")), _entitlement_time(row.get("expires_at"))
        starts_cmp = starts if not starts or starts.tzinfo else starts.replace(tzinfo=dt.timezone.utc)
        expires_cmp = expires if not expires or expires.tzinfo else expires.replace(tzinfo=dt.timezone.utc)
        if starts_cmp and starts_cmp > now:
            continue
        if expires_cmp and expires_cmp <= now:
            continue
        if row.get("user_id"):
            active.setdefault(str(row["user_id"]), []).append(row)
    result = {}
    for user_id, rows in active.items():
        selected = max(rows, key=lambda row: (
            source_rank.get(str(row.get("source") or ""), 0),
            str(row.get("updated_at") or row.get("created_at") or ""),
        ))
        tier = tiers_by_id.get(str(selected.get("tier_id")), {})
        result[user_id] = {
            "tier_id": selected.get("tier_id"),
            "tier_slug": tier.get("slug") or selected.get("tier_id"),
            "tier_name": tier.get("name") or tier.get("slug") or selected.get("tier_id"),
            "tier_source": selected.get("source") or "manual",
            "tier_expires_at": selected.get("expires_at"),
            "tier_assignment_id": selected.get("id"),
        }
    return result


async def _set_trial_credit_total(db, user_id: str, target: int) -> None:
    """Set the aggregate remainder across a user's non-expired agent trials."""
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            rows = [dict(r) for r in conn.execute(
                "SELECT id, started_at, expires_at, credit_cents, remaining_cents "
                "FROM trials WHERE user_id=? ORDER BY started_at DESC",
                (user_id,),
            ).fetchall()]
            current = [r for r in rows if _trial_is_current(r)]
            if not current:
                raise HTTPException(status_code=409, detail="This user has no current trial grant to adjust.")
            delta = target - sum(max(0, int(r.get("remaining_cents") or 0)) for r in current)
            if delta > 0:
                row = current[0]
                remaining = max(0, int(row.get("remaining_cents") or 0)) + delta
                original = max(int(row.get("credit_cents") or 0), remaining)
                conn.execute(
                    "UPDATE trials SET remaining_cents=?, credit_cents=? WHERE id=?",
                    (remaining, original, row["id"]),
                )
            elif delta < 0:
                to_remove = -delta
                for row in current:
                    remaining = max(0, int(row.get("remaining_cents") or 0))
                    reduction = min(remaining, to_remove)
                    if reduction:
                        conn.execute(
                            "UPDATE trials SET remaining_cents=? WHERE id=?",
                            (remaining - reduction, row["id"]),
                        )
                        to_remove -= reduction
                    if not to_remove:
                        break
            conn.commit()
        finally:
            conn.close()
        return

    if hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        rows = (cli.table("trials")
                .select("id,started_at,expires_at,credit_cents,remaining_cents")
                .eq("user_id", user_id).execute().data or [])
        current = sorted(
            (dict(r) for r in rows if _trial_is_current(r)),
            key=lambda r: r.get("started_at") or "",
            reverse=True,
        )
        if not current:
            raise HTTPException(status_code=409, detail="This user has no current trial grant to adjust.")
        delta = target - sum(max(0, int(r.get("remaining_cents") or 0)) for r in current)
        if delta > 0:
            row = current[0]
            remaining = max(0, int(row.get("remaining_cents") or 0)) + delta
            cli.table("trials").update({
                "remaining_cents": remaining,
                "credit_cents": max(int(row.get("credit_cents") or 0), remaining),
            }).eq("id", row["id"]).execute()
        elif delta < 0:
            to_remove = -delta
            for row in current:
                remaining = max(0, int(row.get("remaining_cents") or 0))
                reduction = min(remaining, to_remove)
                if reduction:
                    cli.table("trials").update({
                        "remaining_cents": remaining - reduction,
                    }).eq("id", row["id"]).execute()
                    to_remove -= reduction
                if not to_remove:
                    break
        return

    raise HTTPException(status_code=503, detail="Billing storage is unavailable.")


async def _anonymous_user_exists(db, user_id: str) -> bool:
    from app.api.rate_limit import anonymous_control_identity_ids, is_anonymous_control_subject
    if not is_anonymous_control_subject(user_id):
        return False
    if user_id in anonymous_control_identity_ids():
        return True
    try:
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
            try:
                row = conn.execute(
                    "SELECT 1 FROM channel_identities WHERE user_id=? LIMIT 1",
                    (user_id,),
                ).fetchone()
                if row:
                    return True
                return conn.execute(
                    "SELECT 1 FROM sessions WHERE user_id=? LIMIT 1",
                    (user_id,),
                ).fetchone() is not None
            finally:
                conn.close()
        if hasattr(db, "get_raw_client"):
            rows = (db.get_raw_client().table("channel_identities")
                    .select("user_id").eq("user_id", user_id).limit(1)
                    .execute().data or [])
            return bool(rows)
    except Exception as exc:
        logger.debug("Could not verify anonymous user %s: %s", user_id, exc)
    return False


@router.post("/{user_id}/set-admin")
async def set_user_admin(user_id: str, req: SetAdminRequest):
    """
    Grant or revoke admin access for a user.
    The requesting_user_id must already be an admin.
    The very first admin can be created by setting BOOTSTRAP_ADMIN_ID in the
    environment, which bypasses the check for an empty admin set.
    """
    import os
    db = get_db()

    # Allow a bootstrap admin from env (first-time setup)
    bootstrap_id = os.environ.get("BOOTSTRAP_ADMIN_ID", "")
    if req.requesting_user_id != bootstrap_id:
        await _require_admin(db, req.requesting_user_id)

    profile = await db.set_user_admin(user_id, req.is_admin)
    logger.info(
        "Admin flag for user %s set to %s by %s",
        user_id, req.is_admin, req.requesting_user_id,
    )
    return {
        "user_id": user_id,
        "is_admin": bool(profile.get("is_admin")),
    }


@router.get("/{user_id}/profile")
async def get_user_profile(user_id: str, requesting_user_id: str = Query(...)):
    """Get any user's profile. Requires admin access."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    profile = await db.get_user_profile(user_id)
    if not profile:
        return {"user_id": user_id, "is_admin": False, "default_agent_id": None}
    return {
        "user_id": profile["user_id"],
        "is_admin": bool(profile.get("is_admin")),
        "default_agent_id": profile.get("default_agent_id"),
    }


@router.get("")
async def list_user_profiles(requesting_user_id: str = Query(...)):
    """List all user profiles. Requires admin access."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    conn = db._get_conn()
    try:
        rows = conn.execute(
            "SELECT user_id, is_admin, default_agent_id, created_at, updated_at FROM user_profiles ORDER BY created_at DESC"
        ).fetchall()
        return {
            "profiles": [
                {
                    "user_id": r["user_id"],
                    "is_admin": bool(r["is_admin"]),
                    "default_agent_id": r["default_agent_id"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }
    finally:
        conn.close()


@router.get("/stats")
async def list_users_with_stats(requesting_user_id: str = Query(...)):
    """List registered users joined with profile + activity stats.
    Returns username, display_name, user_id, is_admin, is_approved,
    created_at, last_login_at, session_count, interaction_count.
    Admin only.
    """
    db = get_db()
    app_db = get_app_db()
    await _require_admin(db, requesting_user_id)

    # Profiles are installation-wide account data. Raw access through get_db()
    # follows the current caller's data plane under the split-storage layout,
    # where user_profiles intentionally does not exist.
    profile_map: dict[str, dict] = {}
    app_conn = app_db._get_conn()
    try:
        for r in app_conn.execute(
            "SELECT user_id, is_admin, default_agent_id, created_at, updated_at, last_login_at FROM user_profiles"
        ).fetchall():
            profile_map[r["user_id"]] = dict(r)
    finally:
        app_conn.close()

    conn = db._get_conn()
    session_counts: dict[str, int] = {}
    interaction_counts: dict[str, int] = {}
    session_last_seen: dict[str, str | None] = {}
    anonymous_map: dict[str, dict] = {}

    def merge_anonymous_identity(r) -> None:
        uid = r["user_id"]
        anon = anonymous_map.setdefault(uid, {
            "user_id": uid,
            "display_name": r["display_name"] or "",
            "channels": [],
            "external_ids": [],
            "identity_tiers": [],
            "created_at": r["created_at"],
            "identity_updated_at": r["updated_at"],
        })
        if r["channel"] and r["channel"] not in anon["channels"]:
            anon["channels"].append(r["channel"])
        if r["external_id"] and r["external_id"] not in anon["external_ids"]:
            anon["external_ids"].append(r["external_id"])
        if r["user_tier"] and r["user_tier"] not in anon["identity_tiers"]:
            anon["identity_tiers"].append(r["user_tier"])
        if not anon["display_name"] and r["display_name"]:
            anon["display_name"] = r["display_name"]

    try:
        for r in conn.execute(
            "SELECT user_id, COUNT(*) AS n, MAX(COALESCE(updated_at, created_at)) AS last_seen "
            "FROM sessions GROUP BY user_id"
        ).fetchall():
            session_counts[r["user_id"]] = r["n"]
            session_last_seen[r["user_id"]] = r["last_seen"]
        for r in conn.execute(
            "SELECT s.user_id AS user_id, COUNT(i.id) AS n "
            "FROM interactions i JOIN sessions s ON i.session_id = s.id "
            "GROUP BY s.user_id"
        ).fetchall():
            interaction_counts[r["user_id"]] = r["n"]
    finally:
        conn.close()

    # Anonymous identities belong to the app/control plane. In per-user storage
    # layouts the request DB above is the admin's data plane, so merge the
    # authoritative app-plane registry as well.
    try:
        if hasattr(app_db, "_get_conn"):
            app_conn = app_db._get_conn()
            try:
                for r in app_conn.execute(
                    "SELECT user_id, channel, external_id, user_tier, display_name, created_at, updated_at "
                    "FROM channel_identities WHERE substr(user_id, 1, 5)='anon_' "
                    "ORDER BY updated_at DESC"
                ).fetchall():
                    merge_anonymous_identity(r)
            finally:
                app_conn.close()
        elif hasattr(app_db, "get_raw_client"):
            rows = (app_db.get_raw_client().table("channel_identities")
                    .select("user_id,channel,external_id,user_tier,display_name,created_at,updated_at")
                    .like("user_id", "anon\\_%").execute().data or [])
            for row in rows:
                merge_anonymous_identity(row)
    except Exception as exc:
        logger.debug("Could not load app-plane anonymous identities: %s", exc)

    billing = await _billing_summaries(app_db)
    entitlements = await _entitlement_summaries(app_db)

    # Registered users from the central user_accounts table
    out = []
    for u in await list_users():
        prof = profile_map.get(u.user_id, {})
        bill = billing.get(u.user_id, {})
        entitlement = entitlements.get(u.user_id, {
            "tier_id": "free", "tier_slug": "free", "tier_name": "Free",
            "tier_source": "default", "tier_expires_at": None,
            "tier_assignment_id": None,
        })
        # A pending (unapproved) account has no active product tier yet, so
        # report its experience level as PENDING to distinguish awaiting-approval
        # accounts from active Free/Pro users. This is a display-level override;
        # the entitlement resolver and any explicit assignment stay untouched
        # until the account is authorized.
        if not u.is_approved:
            entitlement = {
                "tier_id": "pending",
                "tier_slug": "pending",
                "tier_name": "Pending",
                "tier_source": "pending",
                "tier_expires_at": None,
                "tier_assignment_id": None,
            }
        is_platform_admin = bool(prof.get("is_admin", 0)) or (u.user_id == "admin")
        out.append({
            "username": u.username,
            "display_name": u.display_name,
            "user_id": u.user_id,
            "is_admin": is_platform_admin,
            "is_approved": bool(u.is_approved),
            "created_at": prof.get("created_at"),
            "last_login_at": prof.get("last_login_at"),
            "session_count": session_counts.get(u.user_id, 0),
            "interaction_count": interaction_counts.get(u.user_id, 0),
            "billing_status": "exempt" if is_platform_admin else bill.get("billing_status", "none"),
            "trial_credits": bill.get("trial_credits", 0),
            "has_trial_grant": bill.get("has_trial_grant", False),
            "paid_credits": bill.get("paid_credits", 0),
            "paid_hold_credits": bill.get("paid_hold_credits", 0),
            "currency": bill.get("currency", "usd"),
            **entitlement,
        })

    # Sort: pending approval first, then by last_login_at desc
    out.sort(key=lambda r: (
        1 if r["is_approved"] else 0,
        -(0 if r["last_login_at"] is None else 1),
        r["last_login_at"] or "",
    ), reverse=True)

    from app.api.rate_limit import anonymous_control_identity_ids, anonymous_control_snapshot, is_anonymous_control_subject
    anonymous_ids = set(anonymous_map)
    anonymous_ids.update(uid for uid in session_counts if is_anonymous_control_subject(uid))
    anonymous_ids.update(uid for uid in billing if is_anonymous_control_subject(uid))
    anonymous_ids.update(anonymous_control_identity_ids())
    anonymous_usage = (await asyncio.to_thread(
        anonymous_control_snapshot, list(anonymous_ids)
    )).get("users", {})
    anonymous_users = []
    for uid in anonymous_ids:
        identity = anonymous_map.get(uid, {})
        bill = billing.get(uid, {})
        usage = anonymous_usage.get(uid, {})
        central_last_seen = max(
            (v for v in (session_last_seen.get(uid), identity.get("identity_updated_at")) if v),
            default=None,
        )
        anonymous_users.append({
            "user_id": uid,
            "display_name": identity.get("display_name") or "Anonymous user",
            "channels": identity.get("channels", []),
            "external_ids": identity.get("external_ids", []),
            "identity_tiers": identity.get("identity_tiers", []),
            "created_at": identity.get("created_at") or usage.get("first_activity_at"),
            "last_seen_at": central_last_seen or usage.get("last_activity_at"),
            "central_metrics_available": uid in session_counts or uid in interaction_counts,
            "session_count": session_counts.get(uid, 0),
            "interaction_count": interaction_counts.get(uid, 0),
            "anonymous_first_activity_at": usage.get("first_activity_at"),
            "anonymous_last_activity_at": usage.get("last_activity_at"),
            "anonymous_actual_tokens": int((usage.get("actual_tokens") or {}).get("used", 0)),
            "anonymous_actual_cost_microusd": int((usage.get("actual_cost_microusd") or {}).get("used", 0)),
            "anonymous_cooldown_active": bool((usage.get("cooldown") or {}).get("active")),
            "billing_status": bill.get("billing_status", "none"),
            "trial_credits": bill.get("trial_credits", 0),
            "has_trial_grant": bill.get("has_trial_grant", False),
            "paid_credits": bill.get("paid_credits", 0),
            "paid_hold_credits": bill.get("paid_hold_credits", 0),
            "currency": bill.get("currency", "usd"),
            "tier_id": "anonymous",
            "tier_slug": "anonymous",
            "tier_name": "Anonymous",
            "tier_source": "system",
            "tier_expires_at": None,
            "tier_assignment_id": None,
        })
    anonymous_users.sort(
        key=lambda r: (r["last_seen_at"] or "", r["user_id"]), reverse=True
    )

    return {"users": out, "anonymous_users": anonymous_users}


@router.get("/anonymous-control")
async def get_anonymous_control(requesting_user_id: str = Query(...)):
    db = get_db()
    await _require_admin(db, requesting_user_id)
    from app.api.rate_limit import anonymous_control_snapshot
    return await __import__("asyncio").to_thread(
        anonymous_control_snapshot,
        [row["user_id"] for row in (await list_users_with_stats(requesting_user_id))["anonymous_users"]],
    )


@router.post("/anonymous-control/{user_id}/cooldown")
async def cool_down_anonymous_user(user_id: str, req: AnonymousCooldownRequest):
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    from app.api.rate_limit import is_anonymous_control_subject, set_anonymous_cooldown
    if not is_anonymous_control_subject(user_id):
        raise HTTPException(status_code=400, detail="Only anonymous identities can be cooled down.")
    await __import__("asyncio").to_thread(
        set_anonymous_cooldown, user_id, req.seconds, req.reason,
    )
    return {"status": "ok", "user_id": user_id, "seconds": req.seconds}


@router.post("/anonymous-control/{user_id}/restore")
async def restore_anonymous_user(user_id: str, req: AnonymousControlRequest):
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    from app.api.rate_limit import clear_anonymous_cooldown, is_anonymous_control_subject
    if not is_anonymous_control_subject(user_id):
        raise HTTPException(status_code=400, detail="Only anonymous identities can be restored.")
    await __import__("asyncio").to_thread(clear_anonymous_cooldown, user_id)
    return {"status": "ok", "user_id": user_id}


@router.post("/anonymous-control/auto-close/clear")
async def clear_anonymous_auto_close_endpoint(req: AnonymousControlRequest):
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    from app.api.rate_limit import clear_anonymous_auto_close
    await __import__("asyncio").to_thread(clear_anonymous_auto_close)
    return {"status": "ok"}


@router.post("/{user_id}/credits")
async def adjust_user_credits(user_id: str, req: AdjustCreditsRequest):
    """Set a user's purchased-wallet or aggregate current-trial credit balance."""
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    if (await get_user_by_id(user_id) is None
            and not await _anonymous_user_exists(get_app_db(), user_id)
            and not await _anonymous_user_exists(db, user_id)):
        raise HTTPException(status_code=404, detail="User not found")

    billing_db = get_app_db()
    if req.credit_type == "trial":
        await _set_trial_credit_total(billing_db, user_id, req.balance_credits)
    else:
        from plugins.billing import wallet as wallet_mod

        wallet = await wallet_mod.get_or_create_wallet(billing_db, "user", user_id)
        if wallet is None:
            raise HTTPException(status_code=503, detail="Billing storage is unavailable.")
        if req.balance_credits < wallet.hold_cents:
            raise HTTPException(
                status_code=409,
                detail=f"Balance cannot be below {wallet.hold_cents} credits currently reserved by active runs.",
            )
        delta = req.balance_credits - wallet.balance_cents
        note = f"Admin adjustment by {req.requesting_user_id}"
        if delta > 0:
            await wallet_mod.credit(
                billing_db, "user", user_id, delta, kind="refund", note=note
            )
        elif delta < 0:
            await wallet_mod.debit(
                billing_db, user_id, -delta, kind="refund", note=note
            )

    summary = (await _billing_summaries(billing_db)).get(user_id, {})
    logger.info(
        "%s credits for user %s set to %d by %s",
        req.credit_type, user_id, req.balance_credits, req.requesting_user_id,
    )
    return {
        "user_id": user_id,
        "credit_type": req.credit_type,
        **summary,
    }


class _ApprovalRequest(BaseModel):
    requesting_user_id: str


@router.post("/{user_id}/approve")
async def approve_user(user_id: str, req: _ApprovalRequest):
    """Approve a pending account. Admin only."""
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    u = await get_user_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    await set_user_approval(u.username, True)
    return {"user_id": user_id, "is_approved": True}


@router.post("/{user_id}/revoke")
async def revoke_user(user_id: str, req: _ApprovalRequest):
    """Revoke approval (lock the account). Admin only."""
    db = get_db()
    await _require_admin(db, req.requesting_user_id)
    u = await get_user_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot revoke the built-in admin account")
    from app.auth.revocation import revoke_user as revoke_auth_sessions
    revoke_auth_sessions(user_id)
    await set_user_approval(u.username, False)
    return {"user_id": user_id, "is_approved": False}


@router.delete("/{user_id}")
async def delete_user_endpoint(user_id: str, requesting_user_id: str = Query(...)):
    """Delete a user account. Admin only. Built-in admin cannot be deleted."""
    db = get_db()
    await _require_admin(db, requesting_user_id)
    from app.db.browser_policy import require_delete_enabled
    try:
        require_delete_enabled()
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    u = await get_user_by_id(user_id)
    if u is None:
        raise HTTPException(status_code=404, detail="User not found")
    if u.username == "admin":
        raise HTTPException(status_code=400, detail="Cannot delete the built-in admin account")
    from app.auth.revocation import revoke_user as revoke_auth_sessions
    revoke_auth_sessions(user_id)
    ok = await auth_delete_user(u.username)
    if not ok:
        raise HTTPException(status_code=400, detail="Could not delete user")
    return {"user_id": user_id, "deleted": True}

"""Billing REST API — agent tier (always present).

  * Agent admin: GET/PUT /api/v1/billing/config/agent/{agent_id}
                 GET/POST/DELETE /api/v1/billing/exemptions
  * End user:    GET  /api/v1/billing/wallet
                 POST /api/v1/billing/wallet/purchase
                 POST /api/v1/billing/subscribe/{agent_id}
                 GET  /api/v1/billing/usage
                 GET  /api/v1/billing/processors

Webhook intake (per-processor): POST /api/v1/billing/webhook/{processor}

An optional billing extension may mount its own routes and plug into the billing
extension seam; nothing in this file references it. With no extension installed
the agent admin keeps 100% of every charge.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.identity import assert_caller_is
from app.db import get_app_db, get_db
from plugins.billing import wallet as wallet_mod
from plugins.billing import pricing as pricing_mod
from plugins.billing.enforcement import check_access
from plugins.billing.pricing import (
    Strategy,
    load_effective_config,
    _fetch_one,
    _has_billing_tables,
)
from plugins.billing.processors import get_processor, list_processors
from plugins.billing.extensions import (
    apply_agent_config_validation,
    apply_subscription_params,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


# ── Helpers ────────────────────────────────────────────────────────────────


async def _require_admin(db, user_id: str) -> None:
    if not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Platform admin required.")


async def _require_agent_admin(db, agent_id: str, user_id: str) -> None:
    if await db.is_user_admin(user_id):
        return
    roles = await db.get_agent_roles(agent_id)
    if user_id not in (roles.get("admin_users") or []):
        raise HTTPException(status_code=403, detail="Agent admin required.")


def _now_iso() -> str:
    import datetime as _dt
    return _dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"


def _conn(db):
    if not hasattr(db, "_get_conn"):
        raise HTTPException(status_code=500, detail="Direct SQL backend required for this op.")
    return db._get_conn()


def _upsert_billing_config(db, scope: str, fields: dict, updated_by: str) -> dict:
    fields = {**fields, "updated_by": updated_by, "updated_at": _now_iso()}
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            row = conn.execute("SELECT * FROM billing_configs WHERE scope=?", (scope,)).fetchone()
            if row is None:
                cols = ["scope"] + list(fields.keys())
                vals = [scope] + list(fields.values())
                placeholders = ",".join(["?"] * len(cols))
                conn.execute(
                    f"INSERT INTO billing_configs ({','.join(cols)}) VALUES ({placeholders})",
                    vals,
                )
            else:
                sets = ",".join([f"{k}=?" for k in fields.keys()])
                conn.execute(
                    f"UPDATE billing_configs SET {sets} WHERE scope=?",
                    list(fields.values()) + [scope],
                )
            conn.commit()
            row = conn.execute("SELECT * FROM billing_configs WHERE scope=?", (scope,)).fetchone()
            return {k: row[k] for k in row.keys()}
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        existing = cli.table("billing_configs").select("*").eq("scope", scope).limit(1).execute()
        if existing.data:
            cli.table("billing_configs").update(fields).eq("scope", scope).execute()
        else:
            cli.table("billing_configs").insert({"scope": scope, **fields}).execute()
        res = cli.table("billing_configs").select("*").eq("scope", scope).limit(1).execute()
        return res.data[0] if res.data else {"scope": scope, **fields}
    raise HTTPException(status_code=500, detail="Unsupported db backend")


def _config_to_response(row: dict) -> dict:
    if not row:
        return {}
    def _j(v, d):
        if v is None or v == "":
            return d
        if isinstance(v, (dict, list)):
            return v
        try:
            return json.loads(v)
        except Exception:
            return d
    return {
        "scope": row.get("scope"),
        "strategy": row.get("strategy") or "free",
        "allowed_strategies": _j(row.get("allowed_strategies"), []),
        "allowed_processors": _j(row.get("allowed_processors"), []),
        "rate_card_default_llm": _j(row.get("rate_card_default_llm"), {}),
        "rate_card_byo_llm": _j(row.get("rate_card_byo_llm"), {}),
        # Raw values (None when the agent hasn't overridden) so the UI can fall
        # back to the effective/inherited numbers.
        "cost_multiplier": row.get("cost_multiplier"),
        "min_charge_cents": row.get("min_charge_cents"),
        "flat_image_cost_usd": row.get("flat_image_cost_usd"),
        "trial_config": _j(row.get("trial_config"), {}),
        "subscription_price_cents": int(row.get("subscription_price_cents") or 0),
        "currency": row.get("currency") or "usd",
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


# ── Request models ─────────────────────────────────────────────────────────


class AgentConfigBody(BaseModel):
    user_id: str
    strategy: Optional[str] = None
    allowed_processors: Optional[List[str]] = None
    rate_card_default_llm: Optional[Dict[str, Any]] = None
    rate_card_byo_llm: Optional[Dict[str, Any]] = None
    cost_multiplier: Optional[float] = None
    min_charge_cents: Optional[int] = None
    flat_image_cost_usd: Optional[float] = None
    trial_config: Optional[Dict[str, Any]] = None
    subscription_price_cents: Optional[int] = None


class PurchaseBody(BaseModel):
    user_id: str
    amount_cents: int
    processor: str
    currency: str = "usd"


class SubscribeBody(BaseModel):
    user_id: str
    processor: str


class ExemptionBody(BaseModel):
    user_id: str  # caller
    kind: str = Field(..., pattern="^(agent|user|user_for_agent)$")
    agent_id: Optional[str] = None
    target_user_id: Optional[str] = None
    reason: Optional[str] = None


# ── Routes: config ─────────────────────────────────────────────────────────


@router.get("/config")
async def get_config(
    agent_id: Optional[str] = Query(None),
    user_id: str = Query(...),
):
    db = get_app_db()
    if not _has_billing_tables(db):
        return {"effective": {"strategy": "free"}, "agent": {}}
    agent = (
        await _fetch_one(db, "billing_configs", {"scope": f"agent:{agent_id}"})
        if agent_id else None
    )
    effective = await load_effective_config(db, agent_id) if agent_id else {"strategy": "free"}
    return {
        "agent": _config_to_response(agent or {}),
        "effective": effective,
    }


@router.put("/config/agent/{agent_id}")
async def put_agent_config(agent_id: str, body: AgentConfigBody, fastapi_request: Request):
    body.user_id = await assert_caller_is(fastapi_request, body.user_id)
    db = get_app_db()
    await _require_agent_admin(db, agent_id, body.user_id)

    fields: Dict[str, Any] = {}
    if body.strategy is not None:
        fields["strategy"] = body.strategy
    if body.allowed_processors is not None:
        fields["allowed_processors"] = json.dumps(body.allowed_processors)
    if body.rate_card_default_llm is not None:
        fields["rate_card_default_llm"] = json.dumps(body.rate_card_default_llm)
    if body.rate_card_byo_llm is not None:
        fields["rate_card_byo_llm"] = json.dumps(body.rate_card_byo_llm)
    for n in ("cost_multiplier", "min_charge_cents", "flat_image_cost_usd"):
        v = getattr(body, n)
        if v is not None:
            fields[n] = v
    if body.trial_config is not None:
        fields["trial_config"] = json.dumps(body.trial_config)
    if body.subscription_price_cents is not None:
        fields["subscription_price_cents"] = body.subscription_price_cents

    # An optional billing extension, if installed, may enforce allowed-strategy /
    # processor limits here; otherwise the agent admin picks freely.
    await apply_agent_config_validation(db, agent_id, {
        "strategy": body.strategy,
        "allowed_processors": body.allowed_processors,
    })

    row = _upsert_billing_config(db, f"agent:{agent_id}", fields, body.user_id)
    return {"config": _config_to_response(row)}


# ── Routes: processors ─────────────────────────────────────────────────────


@router.get("/processors")
async def get_processors():
    return {"processors": list_processors()}


# ── Routes: wallet ─────────────────────────────────────────────────────────


@router.get("/wallet")
async def get_wallet(user_id: str = Query(...)):
    db = get_app_db()
    w = await wallet_mod.get_balance(db, user_id)
    if not w:
        return {"balance_cents": 0, "hold_cents": 0, "currency": "usd", "transactions": []}
    tx = await wallet_mod.list_transactions(db, w.wallet_id, limit=50)
    return {
        "balance_cents": w.balance_cents,
        "available_cents": w.available_cents,
        "hold_cents": w.hold_cents,
        "currency": w.currency,
        "transactions": tx,
    }


@router.get("/access")
async def get_access(
    agent_id: str = Query(...),
    user_id: str = Query(...),
    fastapi_request: Request = None,
):
    """Re-evaluate billing access for a (user, agent) WITHOUT raising 402.

    Lets the frontend clear a trial-expired / needs-credits block after the user
    buys credits or connects their own LLM key (the composer is disabled while
    blocked, so the only way to re-check is a read endpoint like this). Returns
    the same AccessDecision shape the 402 denial carries."""
    if fastapi_request is not None:
        user_id = await assert_caller_is(fastapi_request, user_id)
    agent = await get_db().get_agent_by_id(agent_id)
    if not agent:
        return {"allow": True, "reason": "allow", "detail": "agent-not-found",
                "strategy": "free", "balance_cents": 0,
                "accepted_processors": [], "agent_id": agent_id}
    decision = await check_access(agent, user_id, get_app_db())
    return decision.to_dict()


@router.post("/wallet/purchase")
async def wallet_purchase(body: PurchaseBody, fastapi_request: Request):
    body.user_id = await assert_caller_is(fastapi_request, body.user_id)
    if body.amount_cents <= 0:
        raise HTTPException(status_code=400, detail="amount_cents must be positive")
    proc = get_processor(body.processor)
    if not proc or not proc.is_configured():
        raise HTTPException(status_code=400, detail=f"Processor '{body.processor}' not available")
    base = str(fastapi_request.base_url).rstrip("/")
    try:
        session = await proc.create_checkout(
            user_id=body.user_id,
            amount_cents=body.amount_cents,
            currency=body.currency,
            kind="purchase",
            success_url=f"{base}/?billing=success",
            cancel_url=f"{base}/?billing=cancel",
            metadata={"purpose": "credit_purchase"},
        )
    except Exception as e:
        logger.error("purchase checkout failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Checkout creation failed: {e}")
    # Record the pending payment for reconciliation
    _insert_payment(get_app_db(), {
        "id": str(uuid.uuid4()),
        "processor": body.processor,
        "external_payment_id": session.session_id,
        "user_id": body.user_id,
        "amount_cents": body.amount_cents,
        "currency": body.currency,
        "kind": "purchase",
        "status": "pending",
        "metadata": json.dumps(session.metadata or {}),
    })
    return {"redirect_url": session.redirect_url, "session_id": session.session_id}


@router.post("/subscribe/{agent_id}")
async def subscribe(agent_id: str, body: SubscribeBody, fastapi_request: Request):
    body.user_id = await assert_caller_is(fastapi_request, body.user_id)
    db = get_app_db()
    cfg = await load_effective_config(db, agent_id)
    price = int(cfg.get("subscription_price_cents") or 0)
    if price <= 0:
        raise HTTPException(status_code=400, detail="This agent has no subscription price configured.")
    proc = get_processor(body.processor)
    if not proc or not proc.is_configured():
        raise HTTPException(status_code=400, detail=f"Processor '{body.processor}' not available")
    payee, fee_pct = await apply_subscription_params(db, agent_id, body.processor)
    base = str(fastapi_request.base_url).rstrip("/")
    try:
        sub = await proc.create_subscription(
            user_id=body.user_id,
            agent_id=agent_id,
            price_cents=price,
            currency=cfg.get("currency") or "usd",
            success_url=f"{base}/?subscribed=1",
            cancel_url=f"{base}/?subscribed=0",
            payee_account_id=payee,
            platform_fee_pct=fee_pct,
        )
    except Exception as e:
        logger.error("subscribe failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Subscription creation failed: {e}")
    _upsert_subscription(db, {
        "id": str(uuid.uuid4()),
        "user_id": body.user_id,
        "agent_id": agent_id,
        "processor": body.processor,
        "external_subscription_id": sub.subscription_id,
        "status": sub.status or "pending",
    })
    return {"redirect_url": sub.redirect_url, "subscription_id": sub.subscription_id}


# ── Routes: exemptions ─────────────────────────────────────────────────────


@router.get("/exemptions")
async def list_exemptions(
    user_id: str = Query(...),
    agent_id: Optional[str] = Query(None),
):
    db = get_app_db()
    is_platform_admin = await db.is_user_admin(user_id)
    rows: List[dict] = []
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            if is_platform_admin and not agent_id:
                cur = conn.execute("SELECT * FROM billing_exemptions ORDER BY created_at DESC")
            elif agent_id:
                cur = conn.execute(
                    "SELECT * FROM billing_exemptions WHERE agent_id=? ORDER BY created_at DESC",
                    (agent_id,),
                )
            else:
                # Agent admins see exemptions on their own agents
                user_agents = await db.list_agents_for_user(user_id)
                ids = [a["id"] for a in user_agents if a.get("source") == "custom"]
                if not ids:
                    return {"exemptions": []}
                placeholders = ",".join(["?"] * len(ids))
                cur = conn.execute(
                    f"SELECT * FROM billing_exemptions WHERE agent_id IN ({placeholders}) ORDER BY created_at DESC",
                    ids,
                )
            rows = [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
        finally:
            conn.close()
    return {"exemptions": rows}


@router.post("/exemptions")
async def create_exemption(body: ExemptionBody, fastapi_request: Request):
    body.user_id = await assert_caller_is(fastapi_request, body.user_id)
    db = get_app_db()
    if body.kind in ("agent", "user"):
        await _require_admin(db, body.user_id)
    elif body.kind == "user_for_agent":
        if not body.agent_id:
            raise HTTPException(status_code=400, detail="agent_id is required for user_for_agent")
        await _require_agent_admin(db, body.agent_id, body.user_id)

    # Validate field combos
    if body.kind == "agent" and not body.agent_id:
        raise HTTPException(status_code=400, detail="agent_id is required for kind='agent'")
    if body.kind == "user" and not body.target_user_id:
        raise HTTPException(status_code=400, detail="target_user_id is required for kind='user'")
    if body.kind == "user_for_agent" and (not body.agent_id or not body.target_user_id):
        raise HTTPException(status_code=400, detail="agent_id and target_user_id are required")

    eid = _upsert_exemption(
        db,
        kind=body.kind,
        agent_id=body.agent_id,
        target_user_id=body.target_user_id,
        granted_by=body.user_id,
        reason=body.reason,
    )
    return {"id": eid, "ok": True}


@router.delete("/exemptions/{exemption_id}")
async def delete_exemption(exemption_id: str, user_id: str = Query(...), fastapi_request: Request = None):
    if fastapi_request is not None:
        user_id = await assert_caller_is(fastapi_request, user_id)
    db = get_app_db()
    row = None
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            r = conn.execute("SELECT * FROM billing_exemptions WHERE id=?", (exemption_id,)).fetchone()
            row = {k: r[k] for k in r.keys()} if r else None
        finally:
            conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Exemption not found")
    if row["kind"] in ("agent", "user"):
        await _require_admin(db, user_id)
    else:
        await _require_agent_admin(db, row["agent_id"], user_id)
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            conn.execute("DELETE FROM billing_exemptions WHERE id=?", (exemption_id,))
            conn.commit()
        finally:
            conn.close()
    return {"ok": True}


# ── Routes: usage ──────────────────────────────────────────────────────────


@router.get("/usage")
async def get_usage(
    user_id: str = Query(...),
    agent_id: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    db = get_app_db()
    is_admin = await db.is_user_admin(user_id)
    rows: List[dict] = []
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            if agent_id:
                if not is_admin:
                    roles = await db.get_agent_roles(agent_id)
                    if user_id not in (roles.get("admin_users") or []):
                        # End user sees only their own rows for this agent
                        cur = conn.execute(
                            "SELECT * FROM usage_events WHERE agent_id=? AND user_id=? ORDER BY created_at DESC LIMIT ?",
                            (agent_id, user_id, limit),
                        )
                    else:
                        cur = conn.execute(
                            "SELECT * FROM usage_events WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
                            (agent_id, limit),
                        )
                else:
                    cur = conn.execute(
                        "SELECT * FROM usage_events WHERE agent_id=? ORDER BY created_at DESC LIMIT ?",
                        (agent_id, limit),
                    )
            elif is_admin:
                cur = conn.execute(
                    "SELECT * FROM usage_events ORDER BY created_at DESC LIMIT ?",
                    (limit,),
                )
            else:
                cur = conn.execute(
                    "SELECT * FROM usage_events WHERE user_id=? ORDER BY created_at DESC LIMIT ?",
                    (user_id, limit),
                )
            rows = [{k: r[k] for k in r.keys()} for r in cur.fetchall()]
        finally:
            conn.close()
    return {"events": rows}


# ── Webhook intake ─────────────────────────────────────────────────────────


@router.post("/webhook/{processor}")
async def webhook_intake(processor: str, fastapi_request: Request):
    proc = get_processor(processor)
    if not proc:
        raise HTTPException(status_code=404, detail=f"Unknown processor: {processor}")
    body = await fastapi_request.body()
    headers = {k.lower(): v for k, v in fastapi_request.headers.items()}
    try:
        event = await proc.verify_webhook(headers=headers, body=body)
    except Exception as e:
        logger.warning("Webhook verification failed for %s: %s", processor, e)
        raise HTTPException(status_code=401, detail="Webhook signature verification failed")

    db = get_app_db()
    if event.event_type == "payment.completed":
        if event.user_id and event.amount_cents > 0:
            await wallet_mod.credit(
                db,
                owner_type="user",
                owner_id=event.user_id,
                amount_cents=event.amount_cents,
                kind="purchase",
                ref_id=event.external_payment_id,
                note=f"{processor}:{event.raw_event_type}",
                currency=event.currency,
            )
            _mark_payment_completed(db, processor, event.external_payment_id)
    elif event.event_type == "subscription.updated":
        if event.user_id and event.agent_id:
            _set_subscription_status(db, event.user_id, event.agent_id, "active",
                                      external_id=event.external_subscription_id)
    elif event.event_type == "subscription.cancelled":
        if event.user_id and event.agent_id:
            _set_subscription_status(db, event.user_id, event.agent_id, "cancelled",
                                      external_id=event.external_subscription_id)
    elif event.event_type == "payment.refunded":
        _mark_payment_status(db, processor, event.external_payment_id, "refunded")

    return {"ok": True, "event_type": event.event_type}


# ── Tiny DB helpers (kept inline so we don't have to extend StorageBackend) ──


def _insert_payment(db, fields: dict) -> None:
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            cols = ",".join(fields.keys())
            ph = ",".join(["?"] * len(fields))
            conn.execute(f"INSERT INTO payments ({cols}) VALUES ({ph})", list(fields.values()))
            conn.commit()
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        db.get_raw_client().table("payments").insert(fields).execute()


def _mark_payment_completed(db, processor: str, external_id: Optional[str]) -> None:
    if not external_id:
        return
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            conn.execute(
                "UPDATE payments SET status='completed', updated_at=datetime('now') WHERE processor=? AND external_payment_id=?",
                (processor, external_id),
            )
            conn.commit()
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        (db.get_raw_client().table("payments")
         .update({"status": "completed"})
         .eq("processor", processor).eq("external_payment_id", external_id).execute())


def _mark_payment_status(db, processor: str, external_id: Optional[str], status: str) -> None:
    if not external_id:
        return
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            conn.execute(
                "UPDATE payments SET status=?, updated_at=datetime('now') WHERE processor=? AND external_payment_id=?",
                (status, processor, external_id),
            )
            conn.commit()
        finally:
            conn.close()


def _upsert_subscription(db, fields: dict) -> None:
    user_id = fields["user_id"]
    agent_id = fields["agent_id"]
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM subscriptions WHERE user_id=? AND agent_id=?",
                (user_id, agent_id),
            ).fetchone()
            if row is None:
                cols = ",".join(fields.keys())
                ph = ",".join(["?"] * len(fields))
                conn.execute(f"INSERT INTO subscriptions ({cols}) VALUES ({ph})", list(fields.values()))
            else:
                rest = {k: v for k, v in fields.items() if k not in ("id", "user_id", "agent_id")}
                sets = ",".join([f"{k}=?" for k in rest.keys()])
                conn.execute(
                    f"UPDATE subscriptions SET {sets}, updated_at=datetime('now') WHERE id=?",
                    list(rest.values()) + [row["id"]],
                )
            conn.commit()
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        r = (cli.table("subscriptions").select("id")
             .eq("user_id", user_id).eq("agent_id", agent_id).limit(1).execute())
        if r.data:
            cli.table("subscriptions").update(fields).eq("id", r.data[0]["id"]).execute()
        else:
            cli.table("subscriptions").insert(fields).execute()


def _set_subscription_status(db, user_id: str, agent_id: str, status: str, external_id: Optional[str] = None) -> None:
    fields = {"status": status}
    if external_id:
        fields["external_subscription_id"] = external_id
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            existing = conn.execute(
                "SELECT id FROM subscriptions WHERE user_id=? AND agent_id=?",
                (user_id, agent_id),
            ).fetchone()
            if existing:
                sets = ",".join([f"{k}=?" for k in fields.keys()])
                conn.execute(
                    f"UPDATE subscriptions SET {sets}, updated_at=datetime('now') WHERE id=?",
                    list(fields.values()) + [existing["id"]],
                )
            else:
                row = {"id": str(uuid.uuid4()), "user_id": user_id, "agent_id": agent_id,
                       "processor": "unknown", **fields}
                cols = ",".join(row.keys())
                ph = ",".join(["?"] * len(row))
                conn.execute(f"INSERT INTO subscriptions ({cols}) VALUES ({ph})", list(row.values()))
            conn.commit()
        finally:
            conn.close()


def _upsert_exemption(
    db, *,
    kind: str,
    agent_id: Optional[str],
    target_user_id: Optional[str],
    granted_by: str,
    reason: Optional[str],
) -> str:
    if hasattr(db, "_get_conn"):
        conn = db._get_conn()
        try:
            # Dedupe — exact match on (kind, agent_id, user_id)
            where = ["kind=?"]
            params: list = [kind]
            if agent_id is not None:
                where.append("agent_id=?")
                params.append(agent_id)
            else:
                where.append("agent_id IS NULL")
            if target_user_id is not None:
                where.append("user_id=?")
                params.append(target_user_id)
            else:
                where.append("user_id IS NULL")
            existing = conn.execute(
                f"SELECT id FROM billing_exemptions WHERE {' AND '.join(where)} LIMIT 1",
                params,
            ).fetchone()
            if existing:
                return existing["id"]
            eid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO billing_exemptions (id, kind, agent_id, user_id, granted_by_user_id, reason) VALUES (?,?,?,?,?,?)",
                (eid, kind, agent_id, target_user_id, granted_by, reason),
            )
            conn.commit()
            return eid
        finally:
            conn.close()
    elif hasattr(db, "get_raw_client"):
        cli = db.get_raw_client()
        q = cli.table("billing_exemptions").select("id").eq("kind", kind)
        if agent_id is not None:
            q = q.eq("agent_id", agent_id)
        else:
            q = q.is_("agent_id", "null")
        if target_user_id is not None:
            q = q.eq("user_id", target_user_id)
        else:
            q = q.is_("user_id", "null")
        existing = q.limit(1).execute()
        if existing.data:
            return existing.data[0]["id"]
        eid = str(uuid.uuid4())
        cli.table("billing_exemptions").insert({
            "id": eid, "kind": kind, "agent_id": agent_id,
            "user_id": target_user_id, "granted_by_user_id": granted_by, "reason": reason,
        }).execute()
        return eid
    return ""

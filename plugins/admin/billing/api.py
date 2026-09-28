"""Platform (marketplace) billing REST endpoints — optional add-on.

Mounted by the host only when this package is present (discovered via the core
billing extension seam). Routes:

  * Platform admin: GET/PUT /api/v1/billing/config/platform
                    POST    /api/v1/billing/connect/onboard
                    GET     /api/v1/billing/connect/status

The agent tier in ``plugins/billing`` carries the rest of /api/v1/billing and
never references these routes.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.identity import assert_caller_is
from app.db import get_app_db
from plugins.billing.pricing import (
    _has_billing_tables,
    parse_strategy_selection,
)
from plugins.billing.processors import get_processor
from plugins.admin.billing import store

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/billing", tags=["billing-platform"])


async def _require_platform_admin(db, user_id: str) -> None:
    if not await db.is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="Platform admin required.")


def _platform_config_to_response(row: dict) -> dict:
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
        "cost_multiplier": float(row.get("cost_multiplier") or 1.0),
        "min_charge_cents": int(row.get("min_charge_cents") or 1),
        "flat_image_cost_usd": float(row.get("flat_image_cost_usd") or 0.01),
        "platform_fee_pct": float(row.get("platform_fee_pct") or 0),
        "platform_fee_flat_cents": int(row.get("platform_fee_flat_cents") or 0),
        "trial_config": _j(row.get("trial_config"), {}),
        "subscription_price_cents": int(row.get("subscription_price_cents") or 0),
        "currency": row.get("currency") or "usd",
        "updated_at": row.get("updated_at"),
        "updated_by": row.get("updated_by"),
    }


class PlatformConfigBody(BaseModel):
    user_id: str
    strategy: Optional[str] = None
    allowed_strategies: Optional[List[str]] = None
    allowed_processors: Optional[List[str]] = None
    rate_card_default_llm: Optional[Dict[str, Any]] = None
    rate_card_byo_llm: Optional[Dict[str, Any]] = None
    cost_multiplier: Optional[float] = None
    min_charge_cents: Optional[int] = None
    flat_image_cost_usd: Optional[float] = None
    platform_fee_pct: Optional[float] = None
    platform_fee_flat_cents: Optional[int] = None
    trial_config: Optional[Dict[str, Any]] = None
    subscription_price_cents: Optional[int] = None
    currency: Optional[str] = None


class OnboardBody(BaseModel):
    user_id: str
    processor: str


# ── Platform config ────────────────────────────────────────────────────────


@router.get("/config/platform")
async def get_platform_config(user_id: str = Query(...)):
    db = get_app_db()
    row = await store.read_platform_config(db)
    return {"platform": _platform_config_to_response(row or {})}


@router.put("/config/platform")
async def put_platform_config(body: PlatformConfigBody, fastapi_request: Request):
    body.user_id = await assert_caller_is(fastapi_request, body.user_id)
    db = get_app_db()
    await _require_platform_admin(db, body.user_id)
    fields: Dict[str, Any] = {}
    if body.strategy is not None:
        try:
            selected = parse_strategy_selection(body.strategy, strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        fields["strategy"] = ",".join(selected)
    for src_name in ("allowed_strategies", "allowed_processors",
                     "rate_card_default_llm", "rate_card_byo_llm", "trial_config"):
        v = getattr(body, src_name)
        if v is not None:
            fields[src_name] = json.dumps(v)
    for n in ("cost_multiplier", "min_charge_cents", "flat_image_cost_usd",
              "platform_fee_pct", "platform_fee_flat_cents",
              "subscription_price_cents", "currency"):
        v = getattr(body, n)
        if v is not None:
            fields[n] = v
    row = store.upsert_platform_config(db, fields, body.user_id)
    return {"config": _platform_config_to_response(row)}


# ── Connect / payout onboarding ────────────────────────────────────────────


@router.post("/connect/onboard")
async def connect_onboard(body: OnboardBody, fastapi_request: Request):
    body.user_id = await assert_caller_is(fastapi_request, body.user_id)
    proc = get_processor(body.processor)
    if not proc or not proc.is_configured():
        raise HTTPException(status_code=400, detail=f"Processor '{body.processor}' not available")
    base = str(fastapi_request.base_url).rstrip("/")
    try:
        link = await proc.onboard_payee(
            user_id=body.user_id,
            return_url=f"{base}/?onboarded={body.processor}",
            refresh_url=f"{base}/?onboard_refresh={body.processor}",
        )
    except Exception as e:
        logger.error("onboard failed: %s", e)
        raise HTTPException(status_code=502, detail=f"Onboarding failed: {e}")
    store.upsert_payment_account(get_app_db(), body.user_id, body.processor, link.account_id, complete=link.complete)
    return {"redirect_url": link.redirect_url, "account_id": link.account_id}


@router.get("/connect/status")
async def connect_status(user_id: str = Query(...)):
    db = get_app_db()
    return {"accounts": store.list_payment_accounts(db, user_id)}

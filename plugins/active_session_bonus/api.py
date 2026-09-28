"""Authenticated API owned entirely by the Active Session Bonus plugin."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.api.db_viewer import mark_session_read, require_admin
from app.auth.identity import request_user_id
from .policies import PolicyValidationError, get_policy_store
from .qualification import decay_floor
from .store import get_store

router = APIRouter(prefix="/api/v1/active-session-bonus", tags=["active-session-bonus"])


class PoliciesBody(BaseModel):
    policies: list[dict]


def _user(request: Request) -> str:
    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return user_id


@router.get("/policies")
async def policies(_auth: dict = Depends(require_admin)):
    """Live gate taxonomy plus its exact-match bonus policy list."""
    return get_policy_store().current()


@router.put("/policies")
async def save_policies(body: PoliciesBody, _auth: dict = Depends(require_admin)):
    try:
        return get_policy_store().save(body.policies)
    except PolicyValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/offers")
async def offers(request: Request):
    """Active offers for the caller; server-calculated remaining values are authoritative."""
    store = get_store()
    return {"offers": store.offers_for_user(_user(request)),
            # Each offer carries its frozen expiry window; the only shared value
            # is the payable floor that bounds its linear interpolation.
            "decay": {"mode": "linear", "floor": decay_floor(store.config)}}


@router.get("/session/{session_id}")
async def session_offer(session_id: str, request: Request):
    """Offer history for one owned session, including locked and claimed outcomes."""
    return {"offers": get_store().offers_for_session(_user(request), session_id)}


@router.post("/offers/{offer_id}/activate")
async def activate(offer_id: str, request: Request):
    """Lock a decaying offer and acknowledge the owned session when required."""
    user_id = _user(request)
    existing = get_store().offer(offer_id, user_id)
    if not existing:
        raise HTTPException(status_code=404, detail="Offer not found")
    policy = existing.get("policy") or {}
    if policy.get("resolution_action") == "open_reset" and existing["status"] == "offered":
        # Reuse the owned-session acknowledgement endpoint; this deliberately does
        # not change global read-on-visit behavior.
        await mark_session_read(existing["session_id"], request)
    offer = get_store().activate(offer_id, user_id)
    if not offer:
        raise HTTPException(status_code=404, detail="Offer not found")
    amount = int(offer.get("paused_amount") or offer.get("remaining_amount") or 0)
    if offer["status"] in {"expired", "superseded", "rejected"}:
        raise HTTPException(status_code=409, detail=offer.get("failure_reason") or "Offer is no longer available")
    action = (offer.get("policy") or {}).get("resolution_action")
    message = {
        "open_reset": f"{amount} credits locked. This notification has been marked read and the bonus will settle shortly.",
        "continue": f"{amount} credits locked. Continue this chat to claim them.",
        "complete": f"{amount} credits locked. Complete this task to claim them.",
    }.get(action, f"{amount} credits locked. Reply and advance this task to claim them.")
    return {"status": offer["status"], "paused_amount": amount, "message": message}

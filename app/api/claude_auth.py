"""
HTTP routes for in-app Local Claude Code sign-in.

Thin wrapper over plugins/engines/claude_code/claude_code_login.py: an admin starts sign-in
(gets the authorize link), pastes back the code, and the long-lived token is saved
so the Claude agent keeps working. Admin-gated the same way the engine itself is
(a real DB admin, or the open-mode bootstrap admin over a tunnel).

CORE wiring note: this is the one small new subsystem registered in app/main.py for
the claude_code engine's sign-in — it is NOT an ability/integration/vault (those
auto-discover). REMOVE-WHEN: the Local Claude Code engine is dropped.
"""

import logging
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.identity import request_user_id
from plugins.engines.claude_code import claude_code_login as login

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/claude-code")


async def _require_admin(request: Request) -> str:
    """Resolve the caller and require admin (mirrors the engine's runtime gate).
    Raises 403 otherwise. Returns the verified user_id."""
    uid = request_user_id(request)
    try:
        from app.db import get_db
        is_admin = bool(uid) and await get_db().is_user_admin(uid)
    except Exception:
        is_admin = False
    if not is_admin:
        raise HTTPException(status_code=403, detail="Restricted to admin users only.")
    return uid


class SubmitRequest(BaseModel):
    login_id: str = Field(..., min_length=1, max_length=64)
    code: str = Field(..., min_length=1, max_length=4000)


class CancelRequest(BaseModel):
    login_id: str = Field(..., min_length=1, max_length=64)


@router.get("/auth/status")
async def auth_status(request: Request):
    await _require_admin(request)
    return await login.auth_status()


@router.post("/login/start")
async def login_start(request: Request):
    await _require_admin(request)
    return await login.start_login()


@router.post("/login/submit")
async def login_submit(request: Request, body: SubmitRequest):
    await _require_admin(request)
    return await login.submit_code(body.login_id, body.code)


@router.post("/login/cancel")
async def login_cancel(request: Request, body: CancelRequest):
    await _require_admin(request)
    return await login.cancel_login(body.login_id)


@router.post("/auth/signout")
async def auth_signout(request: Request):
    await _require_admin(request)
    return await login.sign_out()


@router.get("/usage")
async def plan_usage(request: Request):
    """Best-effort Claude subscription usage (5-hour + weekly). Always 200 —
    {"available": False} when the (undocumented) endpoint can't be reached, so
    the footer panel hides the section instead of erroring."""
    await _require_admin(request)
    return await login.get_plan_usage()

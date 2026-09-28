"""FastAPI dependency for protecting DB viewer endpoints."""

import logging
from fastapi import Header, HTTPException, Query
from typing import Optional

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)


async def require_db_auth(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """FastAPI dependency: checks JWT for DB viewer endpoints.

    Accepts token via Authorization header (Bearer) or ?token= query param.
    Returns 401 if no valid token.
    """
    raw = ""
    if authorization and authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token:
        raw = token

    if raw:
        payload = decode_token(raw)
        if payload is not None:
            return payload

    raise HTTPException(status_code=401, detail="Authentication required for DB viewer")

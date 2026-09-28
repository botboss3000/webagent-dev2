"""Caller-identity verification helpers.

The auth middleware (`app/auth/middleware.py`) decodes the JWT on every
non-public request and stashes the subject's user_id on
`request.state.user_id`. It does NOT, however, enforce that the user_id
passed in the request payload (query param, body field, etc.) actually
matches that authenticated user — so without an extra check, an
authenticated user could simply submit a request claiming
`user_id=somebody_else` and the route would happily operate on that other
user's data.

This module is the single chokepoint for that check. Every API route that
reads or writes per-user state should call `assert_caller_is(request,
claimed_user_id)` before touching the DB. Platform administrators have the same
user-workspace boundary as every other account; app-wide operations belong only
on explicitly admin-scoped control-plane tools and endpoints.

WebSocket endpoints can't use this directly (no Request), but can call
`verify_token_matches_user(token, claimed_user_id)` on their handshake
payload to get the same guarantee.
"""

from __future__ import annotations

import asyncio
import contextvars
from typing import Optional

from fastapi import HTTPException, Request

from app.auth.jwt import decode_token

_BOOTSTRAP_ADMIN_ID = "admin"

# ── Verified-caller identity (set per-request by CallerIdentityMiddleware) ─────
# The cryptographically-VERIFIED user_id for the in-flight request, or None.
# This is the single source of truth for token-less chokepoints — chiefly
# resolve_admin_uid — that must demand a proven identity but don't receive the
# Request object. It is populated from the request's JWT (Authorization header
# or ?token=) by the ASGI middleware below; it is NEVER set from a
# client-claimed body/query field, which is the whole point.
_verified_caller_uid: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "verified_caller_uid", default=None
)


def set_verified_caller_uid(uid: Optional[str]):
    return _verified_caller_uid.set(uid or None)


def reset_verified_caller_uid(token) -> None:
    _verified_caller_uid.reset(token)


def get_verified_caller_uid() -> Optional[str]:
    return _verified_caller_uid.get()


class CallerIdentityMiddleware:
    """Pure-ASGI middleware that decodes the request's JWT once and stashes the
    verified user_id in `_verified_caller_uid`.

    Implemented as raw ASGI (not Starlette BaseHTTPMiddleware) on purpose:
    BaseHTTPMiddleware runs the endpoint in a separate task whose contextvar
    snapshot can diverge, whereas a pure-ASGI wrapper sets the var in the same
    await chain the endpoint runs in, so resolve_admin_uid sees it reliably.
    Sets the var on EVERY http request (to None when there's no valid token) so
    a value can never leak from a previous request on a reused context."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            uid = None
            payload = None
            token = self._extract_token(scope)
            if token:
                # decode_token validates the revocation epoch/device against a
                # SQLite database configured with a 10-second busy timeout.
                # Device purge/status writes can hold that lock; never let the
                # global ASGI boundary freeze liveness while validation waits.
                payload = await asyncio.to_thread(decode_token, token)
                if payload:
                    uid = payload.get("user_id") or payload.get("sub")
            scope.setdefault("state", {})["auth_payload"] = payload
            caller_token = set_verified_caller_uid(uid)
            from app.auth.workspace import actor_context, set_workspace
            workspace_context = actor_context(uid) if uid else None
            workspace_token = set_workspace(workspace_context)
            # Also set the DB-layer user context so _get_conn() knows which
            # per-user database file to attach.
            try:
                from app.db.local import set_db_user_context
                db_token = set_db_user_context(
                    workspace_context.subject_id if workspace_context else ""
                )
            except Exception:
                db_token = None
            try:
                await self.app(scope, receive, send)
            finally:
                from app.auth.workspace import reset_workspace
                reset_workspace(workspace_token)
                reset_verified_caller_uid(caller_token)
                if db_token is not None:
                    from app.db.local import reset_db_user_context
                    reset_db_user_context(db_token)
            return
        await self.app(scope, receive, send)

    @staticmethod
    def _extract_token(scope) -> str:
        for k, v in scope.get("headers", []):
            if k == b"authorization":
                val = v.decode("latin-1")
                if val.startswith("Bearer "):
                    return val[7:]
                break
        qs = scope.get("query_string", b"")
        if qs:
            from urllib.parse import parse_qs
            tok = parse_qs(qs.decode("latin-1")).get("token", [""])[0]
            if tok:
                return tok
        return ""


# NOTE: the old 'open' access mode (auto-admin, no sign-in) was retired — the
# only access modes now are Private (admin_approval) and Open Registration
# (public_registered). The former `open_mode_admin_id()` tunnel/local auto-admin
# shortcut has been removed; identity comes only from a valid JWT. "Remember me"
# covers the local-convenience case the old mode existed for.


async def _is_admin(user_id: str) -> bool:
    try:
        from app.db import get_db
        return await get_db().is_user_admin(user_id)
    except Exception:
        return False


def request_user_id(request: Request) -> str:
    """Resolve the caller's user_id for an HTTP gate, the standard way.

    The single request-based chokepoint for admin/identity gates that read the
    token off the request themselves (AuthMiddleware is not registered globally,
    so request.state carries no identity here). Resolution order:

    1. JWT from the ``Authorization: Bearer`` header, then the ``?token=`` query
       param.

    Returns "" when neither yields an identity. Per-router helpers should call
    this instead of re-implementing the token decode (mirrors app/api/github.py
    and app/api/files.py).
    """
    cached = getattr(request.state, "auth_payload", None)
    if cached:
        return str(cached.get("user_id") or cached.get("sub") or "")
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token", "")
    if token:
        payload = decode_token(token)
        if payload:
            uid = payload.get("user_id") or payload.get("sub")
            if uid:
                return uid
    return ""


def _is_anonymous(user_id: Optional[str]) -> bool:
    """True for a caller with no identity or an ``anon_*`` guest id — i.e. NOT a
    registered, signed-in account. Mirrors the same check in app/wiki/store.py."""
    if (not user_id) or str(user_id).startswith("anon_"):
        return True
    try:
        from app.agent.member_workspace import is_agent_member_subject
        return is_agent_member_subject(str(user_id))
    except Exception:
        return False


async def user_may_access_page(
    user_id: Optional[str],
    kind: str,
    page_id: str,
    *,
    capabilities: Optional[dict] = None,
    db=None,
) -> bool:
    """Server-side enforcement of a main/admin page's visibility for one caller.

    The single source of truth a page's sensitive endpoints call so the
    visibility the admin set is enforced on the SERVER, not merely hidden in the
    UI (the client can be bypassed). Rules, mirroring the 3-state visibility:
      • "all"  → everyone, including anonymous guests
      • "auth" → any signed-in registered user (or an admin)
      • "off"  → admins only
    Admins always pass. 'open' (single-user / local) access mode is full local
    trust, so everything passes there too — preserving the local convenience the
    old genui gate had. Fails closed (treats as 'auth') if visibility can't be
    read."""
    try:
        from app.ui_pages import effective_visibility
        vis = effective_visibility(kind, page_id)
    except Exception:
        vis = "auth"  # fail closed to registration-required
    is_admin = await _is_admin(user_id or "")
    try:
        from app.ui_pages import page_entry
        required_capability = str(
            (page_entry(kind, page_id) or {}).get("required_backend_capability") or ""
        )
    except Exception:
        required_capability = ""
    if required_capability == "role:platform_admin" and not is_admin:
        return False
    if vis == "all":
        installation_allowed = True
    elif is_admin:
        installation_allowed = True
    elif vis == "auth":
        installation_allowed = not _is_anonymous(user_id)
    else:
        installation_allowed = False  # "off" → admins only
    if not installation_allowed:
        return False

    # Splash is the public front door, not a tier-controlled app destination.
    # Main pages are granted individually; admin sub-pages inherit the single
    # ``admin-tools`` entitlement. Unknown page ids fail closed.
    if kind == "splash":
        return True
    if capabilities is None:
        try:
            from app.entitlements.service import resolve_capabilities
            capabilities = await resolve_capabilities(user_id, db=db)
        except Exception:
            return False
    entitlement_page = "admin-tools" if kind == "admin" else page_id
    return bool((capabilities.get("pages") or {}).get(entitlement_page, False))


async def caller_may_access_page(request: Request, kind: str, page_id: str) -> bool:
    """Request-based wrapper around user_may_access_page: resolves the caller from
    the request's token (open-mode tokenless → bootstrap admin) and applies the
    page-visibility gate. Use this in HTTP routes that must refuse a caller the
    admin's visibility setting excludes (e.g. the Gen UI endpoints, which run
    agent-authored code with the caller's own app trust)."""
    return await user_may_access_page(request_user_id(request), kind, page_id)


async def resolve_admin_uid(claimed_uid: Optional[str]) -> Optional[str]:
    """Authenticate the actor for an explicitly admin-scoped endpoint.

    The single chokepoint for the body/query-param admin gates across the admin
    routers (users, storage, deploy, remote-access, tunnel-link, events,
    instances). It returns only the cryptographically verified actor when that
    actor is a DB admin. It does not create a user-workspace context and cannot
    authorize an ordinary user-data route for another subject.

    SECURITY (changed 2026-06-28): the client-supplied ``claimed_uid`` is no
    longer trusted as proof of identity. Previously this function honored ANY
    claimed id that happened to be a DB admin — so an unauthenticated caller
    could simply send ``requesting_user_id=admin`` (the well-known bootstrap id)
    and be granted full admin. The claimed id is now ignored for the trust
    decision; only a verified token grants access. The parameter is retained so
    the existing admin-scoped callers need no signature change.

    Returns None when the caller is not a verified DB admin, so the routers'
    ``_require_admin`` helpers reject them with 403.
    """
    verified = get_verified_caller_uid()
    if verified and await _is_admin(verified):
        return verified
    return None


async def assert_caller_is(request: Request, claimed_user_id: Optional[str]) -> str:
    """Confirm the JWT-authenticated caller matches `claimed_user_id`.

    Returns the verified user_id (the JWT subject) so callers can use it
    directly instead of the client-supplied value. Raises 401 if no token and
    hides every cross-workspace resource, including from platform admins.
    """
    from app.auth.workspace import require_user_workspace
    context = await require_user_workspace(request, claimed_user_id)
    return context.subject_id


async def require_platform_admin(request: Request) -> str:
    """Require a verified, registered platform administrator.

    Control-plane routes use this dependency instead of trusting request
    payload identifiers or merely checking that some JWT can be decoded.
    """
    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if _is_anonymous(user_id) or not await _is_admin(user_id):
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


def caller_uid_sync(request: Request) -> str:
    """Synchronous caller resolution for SYNC (`def`) path operations — read
    endpoints that FastAPI runs in its worker threadpool (off the event loop) so
    their DB work can't jam the chat loop. Returns the verified caller uid.
    Mirrors ``assert_caller_is(request, None)``. Raises 401 if no identity.
    """
    state_uid = getattr(request.state, "user_id", None)
    if not state_uid:
        auth_header = request.headers.get("Authorization", "")
        token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
        if not token:
            token = request.query_params.get("token", "")
        payload = decode_token(token) if token else None
        if payload:
            state_uid = payload.get("user_id") or payload.get("sub")
    if not state_uid:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return state_uid


def verify_token_matches_user(token: str, claimed_user_id: str) -> Optional[str]:
    """For WebSocket handshakes: decode `token` and verify its subject
    equals `claimed_user_id`. Returns the verified user_id on success,
    `None` on failure (caller should close the socket)."""
    if not claimed_user_id:
        return None
    payload = decode_token(token) if token else None
    if payload is not None:
        sub = payload.get("user_id") or payload.get("sub")
        if sub == claimed_user_id:
            return sub
    return None

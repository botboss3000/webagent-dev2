"""Auth middleware — protects API routes and the main UI."""

import logging
import re
from fastapi import Request, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from app.auth.jwt import decode_token

logger = logging.getLogger(__name__)

# Paths that do NOT require authentication
PUBLIC_PATHS = {
    "/",
    "/login.html",
    "/ui/login.html",
    "/setup.html",
    "/ui/setup.html",
    "/health",
    "/health/ready",
    "/api/v1/status",
    "/favicon.ico",
    "/favicon.svg",
    "/api/v1/auth/login",
    "/api/v1/auth/recall",
    "/api/v1/auth/logout",
    "/api/v1/auth/anonymous",
    "/api/v1/auth/setup-status",
    "/api/v1/auth/setup-admin",
    "/api/v1/auth/access-mode",
    "/api/v1/auth/ui-config",
    "/test",
    "/privacy",
    "/tos",
}

# Static asset prefixes (CSS, JS, images — always public)
PUBLIC_PREFIXES = ("/ui/", "/web-terminal")

# UUID pattern for public agent URLs (/{agent_id})
_UUID_RE = re.compile(
    r"^/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)
# Anon session endpoint pattern
_ANON_SESSION_RE = re.compile(
    r"^/api/v1/agents/[0-9a-f-]+/anon-session$",
    re.IGNORECASE,
)


class AuthMiddleware(BaseHTTPMiddleware):
    """Check JWT token on protected routes. Redirect to login.html if missing."""

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        method = request.method

        # Allow OPTIONS preflight (CORS handles it below)
        if method == "OPTIONS":
            return await call_next(request)

        # Allow public paths
        if path in PUBLIC_PATHS or any(path.startswith(p) for p in PUBLIC_PREFIXES):
            return await call_next(request)

        # Allow public agent URLs (/{uuid}) and anon-session API
        if _UUID_RE.match(path) or _ANON_SESSION_RE.match(path):
            return await call_next(request)

        # Allow docs
        if path.startswith("/docs") or path.startswith("/openapi"):
            return await call_next(request)

        # Allow WebSocket endpoints (auth done via first message / query token)
        if (path.startswith("/api/v1/agent/ws")
                or path.startswith("/api/v1/transcribe/stream")
                or path.startswith("/api/v1/terminal/ws")
                or path.startswith("/api/v1/browser/ws")
                or path.startswith("/api/v1/browser/screencast")):
            return await call_next(request)

        # Check for Authorization header
        auth_header = request.headers.get("Authorization", "")
        token = ""
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]

        # Also check query param (for SSE / stream connections)
        if not token:
            token = request.query_params.get("token", "")

        if token:
            payload = decode_token(token)
            if payload is not None:
                # Attach user info to request state
                request.state.username = payload.get("sub")
                request.state.user_id = payload.get("user_id")
                return await call_next(request)

        # Not authenticated
        # API routes → 401 JSON
        if path.startswith("/api/"):
            return JSONResponse(
                status_code=401,
                content={"detail": "Not authenticated"},
                headers={"WWW-Authenticate": "Bearer"},
            )

        # Web pages → redirect to login
        return RedirectResponse(url="/login.html", status_code=307)

"""
HTTP error capture for the diagnostic flight-recorder.

Records every 4xx / 5xx error response — status, method, path, the **cause**
(the HTTPException detail / validation errors / 500 exception), and the
requesting user — into the recorder's ``http`` category (and ``logs/http.log``).

Why this exists: a deliberate ``raise HTTPException(400, "...")`` is treated by
FastAPI as a *normal* client-error response and is **not logged**, so it is
invisible to the recorder otherwise. This middleware-free approach closes that
gap by registering exception handlers that take the cause straight from the
exception object (clean and reliable — no response-body parsing, no
BaseHTTPMiddleware message-forwarding quirks), then delegate to FastAPI's
default handlers so the actual responses are unchanged.

Coverage:
  • 4xx / any HTTPException  → ``_http_exc_handler``         (cause = ``exc.detail``)
  • 422 request validation   → ``_validation_handler``       (cause = ``exc.errors()``)
  • unhandled 500            → ``record_server_error()``, called from the
                               global ``Exception`` handler in app.main.

(Errors *returned* directly as a JSONResponse without being *raised* are not
captured — the app uses ``raise HTTPException`` for its error paths.)
"""

import json
import logging
from typing import Optional
from urllib.parse import parse_qsl, urlencode

logger = logging.getLogger(__name__)

# 404s for these are page-load noise (missing assets), not app problems.
_STATIC_404_EXT = (
    ".js", ".css", ".map", ".ico", ".svg", ".png", ".jpg", ".jpeg", ".gif",
    ".webp", ".woff", ".woff2", ".ttf", ".txt",
)
_STATIC_404_PREFIX = ("/ui/", "/screenshots/", "/visuals/", "/user_data/", "/web-terminal", "/static", "/assets")
_SENSITIVE_QUERY_MARKERS = (
    "token", "authorization", "password", "passwd", "secret", "api_key",
    "apikey", "credential", "signature", "code",
)


def _safe_query(raw_query: str) -> Optional[str]:
    """Return a useful query summary without persisting bearer credentials."""
    if not raw_query:
        return None
    try:
        pairs = parse_qsl(raw_query, keep_blank_values=True)
        safe = []
        for key, value in pairs:
            normalized = key.lower().replace("-", "_")
            if any(marker in normalized for marker in _SENSITIVE_QUERY_MARKERS):
                value = "[redacted]"
            safe.append((key, value))
        return urlencode(safe)[:300] or None
    except Exception:
        # A malformed query must not bypass redaction. Parameter names retain
        # enough diagnostic value without preserving any untrusted value.
        names = [part.partition("=")[0] for part in raw_query.split("&")]
        return "&".join(f"{name}=[redacted]" for name in names if name)[:300] or None


def _should_record(path: str, status: int) -> bool:
    if status < 400:
        return False
    if status == 404:
        if path.endswith(_STATIC_404_EXT):
            return False
        if path.startswith(_STATIC_404_PREFIX):
            return False
    return True


def _user_from_request(request) -> Optional[str]:
    try:
        cached = getattr(request.state, "auth_payload", None)
        if cached:
            return cached.get("user_id") or cached.get("sub")
        auth = request.headers.get("authorization", "")
        raw = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token")
        if raw:
            from app.auth.jwt import decode_token
            p = decode_token(raw)
            if p:
                return p.get("user_id") or p.get("sub")
    except Exception:
        pass
    return None


def _record_http(request, status: int, cause: Optional[str], traceback_str: Optional[str] = None) -> None:
    try:
        path = request.url.path
        if not _should_record(path, status):
            return
        from app.agent.diagnostics import get_recorder
        rec = get_recorder()
        if not rec.enabled:
            return
        method = request.method
        detail = {
            "status": status,
            "method": method,
            "path": path,
            "query": _safe_query(str(request.url.query)),
            "cause": cause,
            "client": (request.client.host if request.client else None),
        }
        if traceback_str:
            detail["traceback"] = traceback_str[:8000]
        lvl = "error" if status >= 500 else "warning"
        rec.record(lvl, "http", f"{status} {method} {path}", source=f"HTTP {status}",
                   detail=detail, user_id=_user_from_request(request), persist=True)
    except Exception:
        pass


async def _http_exc_handler(request, exc):
    """Record a raised HTTPException, then return FastAPI's normal response."""
    try:
        d = getattr(exc, "detail", None)
        cause = d if isinstance(d, str) else (json.dumps(d)[:1200] if d is not None else None)
        _record_http(request, getattr(exc, "status_code", 500), cause)
    except Exception:
        pass
    from fastapi.exception_handlers import http_exception_handler
    return await http_exception_handler(request, exc)


async def _validation_handler(request, exc):
    """Record a 422 request-validation error, then return FastAPI's normal response."""
    try:
        _record_http(request, 422, json.dumps(exc.errors(), default=str)[:1500])
    except Exception:
        pass
    from fastapi.exception_handlers import request_validation_exception_handler
    return await request_validation_exception_handler(request, exc)


def record_server_error(request, exc, traceback_str: Optional[str] = None) -> None:
    """Record an unhandled 500. Called from app.main's global Exception handler."""
    try:
        _record_http(request, 500, f"{type(exc).__name__}: {exc}", traceback_str)
    except Exception:
        pass


def install_http_diagnostics(app) -> None:
    """Register HTTP-error exception handlers (4xx + 422). 500s are recorded from
    the global Exception handler in app.main via record_server_error()."""
    from starlette.exceptions import HTTPException as StarletteHTTPException
    from fastapi.exceptions import RequestValidationError
    app.add_exception_handler(StarletteHTTPException, _http_exc_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)

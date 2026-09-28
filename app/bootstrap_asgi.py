"""Fast, read-only bootstrap shell for the WebAgent ASGI application.

``app.main`` intentionally performs some essential local SQLite/auth work before
it can accept normal API requests.  On a cold Windows start that import/lifespan
can take tens of seconds.  This wrapper is deliberately tiny and has no database
imports: it serves the app shell, static UI assets, and truthful health state at
once, then hands every request to the fully started application.

Before hand-off the shell is read-only.  The client uses its cached/fallback
catalog and displays "Connecting services"; API and WebSocket work receive an
explicit temporary-unavailable response rather than a blank page or a false
ready signal.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from pathlib import Path
from typing import Any

from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from starlette.staticfiles import StaticFiles

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_SCRIPT_WITHOUT_NONCE_RE = re.compile(r"<script\b(?![^>]*\bnonce=)", re.IGNORECASE)


def _secure_html(content: str) -> HTMLResponse:
    nonce = secrets.token_urlsafe(24)
    secured = _SCRIPT_WITHOUT_NONCE_RE.sub(f'<script nonce="{nonce}"', content)
    csp = (
        "default-src 'self'; base-uri 'self'; object-src 'none'; "
        "frame-ancestors 'self'; form-action 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; script-src-attr 'none'; "
        "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
        "img-src 'self' data: blob:; media-src 'self' blob:; "
        "font-src 'self' data:; frame-src 'self'; worker-src 'self' blob:; "
        "manifest-src 'self'"
    )
    return HTMLResponse(
        secured,
        headers={
            "Content-Security-Policy": csp,
            "Content-Security-Policy-Report-Only": csp,
            "X-Frame-Options": "SAMEORIGIN",
        },
    )


def _scope_headers(scope) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers", ())
    }


def _import_core_app():
    """Import the heavyweight application off the ASGI event loop."""
    from app.main import app as core_app
    return core_app


class BootstrapASGI:
    def __init__(self) -> None:
        self._core_app: Any | None = None
        self._core_lifespan: Any | None = None
        self._loader: asyncio.Task | None = None
        self._phase = "starting"
        self._error = ""
        self._static = StaticFiles(directory=str(_ROOT / "ui"))

    async def _load_core(self) -> None:
        try:
            # Delayed specifically to keep all heavyweight routes, providers and
            # schema work off the first /app response. Python module import is
            # synchronous, so it must be in a worker thread or it would still
            # freeze the bootstrap event loop before it can answer /app.
            core_app = await asyncio.to_thread(_import_core_app)

            # FastAPI/Starlette's router owns the lifespan context (rather than
            # exposing a public ``router.startup()`` coroutine). Keeping that
            # context open makes the inner app's normal shutdown hooks run too.
            self._core_lifespan = core_app.router.lifespan_context(core_app)
            await self._core_lifespan.__aenter__()
            self._core_app = core_app
            self._phase = "ready"
            logger.info("Core application is ready; bootstrap shell handing off")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # preserve a usable shell + an honest status
            self._phase = "failed"
            self._error = f"{type(exc).__name__}: {exc}"[:240]
            logger.exception("Core application failed during bootstrap")

    async def _lifespan(self, receive, send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                self._phase = "starting"
                self._loader = asyncio.create_task(self._load_core(), name="webagent_core_boot")
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                if self._loader and not self._loader.done():
                    self._loader.cancel()
                    try:
                        await self._loader
                    except asyncio.CancelledError:
                        pass
                if self._core_lifespan is not None:
                    await self._core_lifespan.__aexit__(None, None, None)
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _bootstrap_response(self, scope, receive, send) -> None:
        path = scope.get("path", "")
        headers = _scope_headers(scope)
        host = headers.get("host", "").split(":", 1)[0].lower()
        forwarded_proto = headers.get("x-forwarded-proto", "").split(",", 1)[0].strip().lower()
        if path not in {"/health", "/health/ready"} and host.endswith(".webagent.live") and forwarded_proto == "http":
            query = scope.get("query_string", b"").decode("latin-1")
            location = f"https://{headers.get('host', host)}{path}" + (f"?{query}" if query else "")
            response = RedirectResponse(location, status_code=308)
        elif path == "/health":
            response = JSONResponse({
                "status": "healthy",
                "initialization": self._phase,
                "pending": ["core_application"] if self._phase == "starting" else [],
                "error": self._error if self._phase == "failed" else None,
            })
        elif path == "/health/ready":
            response = JSONResponse(
                {"status": "not_ready", "initialization": self._phase, "error": self._error},
                status_code=503,
            )
        elif path in {"/app", "/index.html"}:
            response = _secure_html((_ROOT / "index.html").read_text(encoding="utf-8"))
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        elif path.startswith("/ui/"):
            relative = path[len("/ui/"):]
            response = await self._static.get_response(relative, scope)
            if relative.lower().endswith(".html") and response.status_code == 200:
                try:
                    response = _secure_html(Path(response.path).read_text(encoding="utf-8"))
                except (OSError, UnicodeError):
                    response = PlainTextResponse("Not Found", status_code=404)
            elif response.status_code == 200:
                response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
        elif path == "/sw.js":
            response = FileResponse(_ROOT / "sw.js", media_type="text/javascript",
                                    headers={"Cache-Control": "no-cache, no-store"})
        elif path in {"/favicon.ico", "/favicon.svg"}:
            response = FileResponse(_ROOT / "ui" / "favicon.svg", media_type="image/svg+xml")
        elif path == "/":
            response = PlainTextResponse("WebAgent is starting. Open /app to use cached content.", status_code=503)
        else:
            response = JSONResponse(
                {"status": "starting", "detail": "Core services are connecting; retry shortly."},
                status_code=503,
                headers={"Retry-After": "2"},
            )
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault(
            "Permissions-Policy",
            "camera=(self), microphone=(self), geolocation=()",
        )
        if forwarded_proto == "https":
            response.headers.setdefault("Strict-Transport-Security", "max-age=86400")
        await response(scope, receive, send)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "lifespan":
            await self._lifespan(receive, send)
            return
        if self._core_app is not None:
            await self._core_app(scope, receive, send)
            return
        if scope["type"] == "http":
            await self._bootstrap_response(scope, receive, send)
            return
        # A WebSocket cannot safely become a cached/read-only experience. Ask the
        # browser to reconnect rather than accepting a socket with no endpoint.
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1013, "reason": "Server starting"})


bootstrap_app = BootstrapASGI()

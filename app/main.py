"""Entry point for the WebAgent server.

CORE FILE — do NOT register new integrations / capabilities here. New event
sources, channels, connectors, integrations, vaults, encryption methods,
payment processors, and scheduler providers are DROP-IN FILES in their plugin
folder and are auto-discovered; they need no router/registration here. Only add
an include_router() for a genuinely new core subsystem. See CLAUDE.md
("Core vs. plugins") and docs/claude/production-editions.md.
"""

import asyncio
import logging
import os
import re
import secrets
import sys
from pathlib import Path
from fastapi import APIRouter

# ── Load .env BEFORE any imports that need environment variables ──
# The .env file lives in the project root (parent of app/).
# This must happen before importing app.chat.execution (which imports loop.py,
# which creates an AsyncOpenAI client at module level using OPENROUTER_API_KEY).
_APP_DIR = Path(__file__).resolve().parent
_DOTENV_PATH = _APP_DIR.parent / ".env"

from dotenv import load_dotenv
load_dotenv(dotenv_path=_DOTENV_PATH)
from app.runtime_mode import data_root, performance_test_mode

# Consume a one-shot Danger-Zone reset marker BEFORE the DB / stores open, so the
# selected data groups (database / vault / attachments / genui / logs) can be
# wiped while nothing holds them. A missing marker is a no-op; any error is
# swallowed so a bad marker can never brick boot. See app/util/reset_boot.py.
from app.util.reset_boot import run_pending_reset
if not performance_test_mode():
    run_pending_reset()

import traceback
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from app.security import (
    CORS_ALLOW_HEADERS,
    CORS_ALLOW_METHODS,
    RequestSecurityMiddleware,
    WebSecurityPolicy,
)

from app.auth.identity import require_platform_admin
class NoCacheMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        p = request.url.path
        # Static UI assets are revalidated cheaply by browsers and shared
        # caches. They are not marked immutable because most paths are not
        # fingerprinted; the service-worker generation remains the coherence
        # boundary for installed clients.
        if p.startswith("/ui/") and p != "/ui/manifest.json":
            # Development assets are not fingerprinted. Let the browser keep them
            # but require revalidation so the dev CDN cannot serve old JavaScript
            # for hours after a source change. Production retains its warm cache.
            forwarded_host = (request.headers.get("x-forwarded-host") or request.url.hostname or "").split(",")[0].split(":")[0].lower()
            if forwarded_host == "dev.webagent.live":
                response.headers["Cache-Control"] = "no-cache"
            else:
                response.headers["Cache-Control"] = "public, max-age=300, stale-while-revalidate=86400"
            if "Pragma" in response.headers:
                del response.headers["Pragma"]
            if "Expires" in response.headers:
                del response.headers["Expires"]
        elif p.startswith("/web-terminal") or p == "/index.html" or p == "/":
            response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"
        return response


import gzip as _gzip

# The first load ships ~2.5 MB of JavaScript + ~0.8 MB of CSS as raw, UNCOMPRESSED
# bytes (the UI is many small ES modules + per-page stylesheets, served straight
# off disk by the /ui StaticFiles mount). Over anything slower than localhost —
# the tunnel, mobile — that uncompressed payload is the dominant hard-refresh cost.
#
# This middleware gzips those responses. It is deliberately SCOPED to static UI
# text assets (paths under /ui/ ending .js/.css/.json/.svg/.map/.html) so it can
# NEVER touch a streaming or dynamic endpoint: chat/agent WebSockets, SSE, NDJSON
# progress streams (commit/deploy), file downloads, or API JSON all pass straight
# through untouched. Those static files are small, so buffering one to compress it
# is safe; non-eligible requests are a zero-overhead pass-through. Only 200s with
# no existing Content-Encoding are compressed (a 206 range / 304 replays as-is).
_GZIP_EXT = (".js", ".css", ".json", ".svg", ".map", ".html")
_GZIP_MIN_SIZE = 600  # below this, the gzip header overhead isn't worth it


class StaticGzipMiddleware:
    """Pure-ASGI gzip for static /ui text assets only (see note above)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)
        path = scope.get("path", "")
        eligible = path.startswith("/ui/") and path.lower().endswith(_GZIP_EXT)
        if eligible:
            accepts_gzip = False
            for k, v in scope.get("headers", []):
                if k == b"accept-encoding" and b"gzip" in v.lower():
                    accepts_gzip = True
                    break
            eligible = accepts_gzip
        if not eligible:
            return await self.app(scope, receive, send)

        state = {"start": None, "chunks": [], "compress": False}

        async def _send(message):
            mtype = message["type"]
            if mtype == "http.response.start":
                # Hold the start line until the (small) body is fully buffered so we
                # can rewrite Content-Length/-Encoding in one shot.
                state["start"] = message
                has_enc = any(k.lower() == b"content-encoding" for k, _ in message.get("headers", []))
                state["compress"] = (message["status"] == 200 and not has_enc)
                return
            if mtype != "http.response.body":
                return await send(message)
            state["chunks"].append(message.get("body", b""))
            if message.get("more_body"):
                return  # keep buffering — a static file may arrive in chunks
            raw = b"".join(state["chunks"])
            start = state["start"]
            if state["compress"] and len(raw) >= _GZIP_MIN_SIZE:
                raw = _gzip.compress(raw)
                headers = [(k, v) for (k, v) in start["headers"]
                           if k.lower() not in (b"content-length", b"content-encoding")]
                headers.append((b"content-encoding", b"gzip"))
                headers.append((b"content-length", str(len(raw)).encode("latin-1")))
                headers.append((b"vary", b"Accept-Encoding"))
                start = {**start, "headers": headers}
            await send(start)
            await send({"type": "http.response.body", "body": raw, "more_body": False})

        await self.app(scope, receive, _send)

from app.chat.execution import router as chat_router
from app.api.agent import router as agent_router
from app.api.agents import router as agents_router, agent_pages_router
from app.api.agent_profiles import router as agent_profiles_router
from app.api.db_viewer import router as session_router
from app.auth import router as auth_router
from app.api.content import router as content_router
from app.api.boot import router as boot_router
from app.api.entitlements import router as entitlements_router
from app.api.status import router as status_router
from app.api.wiki import router as wiki_router
from app.optional_routes import OPTIONAL_ROUTES, load_billing_extension_routers, load_optional_router

# Configure logging
# Level precedence: data/config/debug-config.json (the consolidated debug knobs)
# wins, then the LOG_LEVEL env var, then INFO. Read once at boot, so a change here
# needs a server restart.
try:
    from app.admin.debug_config import log_level as _dbg_log_level
    _log_level_name = (_dbg_log_level() or os.environ.get("LOG_LEVEL", "INFO")).upper()
except Exception:
    _log_level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=getattr(logging, _log_level_name, logging.INFO),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

# Diagnostic flight-recorder: mirror WARNING+ log records (with tracebacks) into
# the in-app recorder so an operator / diagnostic agent can read recent failures
# back via the API + read_diagnostics tool, not just the console. The background
# writer is started in the startup() hook below (needs the event loop).
try:
    from app.agent.diagnostics import install_log_handler as _install_diag_handler
    from app.admin.debug_config import diagnostics_capture_level as _dbg_diag_level
    # INFO by default: capture full backend detail into the now-isolated logs.db.
    # The handler applies a per-logger policy (our app.* at INFO+, noisy libs at
    # WARNING+) so this stays signal, not framework chatter. Level precedence:
    # debug-config.json > DIAGNOSTICS_CAPTURE_LEVEL env var > INFO.
    _diag_level_name = (_dbg_diag_level() or os.environ.get("DIAGNOSTICS_CAPTURE_LEVEL", "INFO")).upper()
    _diag_level = getattr(logging, _diag_level_name, logging.INFO)
    _install_diag_handler(level=_diag_level)
except Exception as _diag_err:  # never let diagnostics wiring break boot
    logger.warning("Diagnostic log handler not installed: %s", _diag_err)

app = FastAPI(
    title="WebAgent API",
    description="WebAgent — FastAPI service with tool-calling agent loop and WebSocket streaming",
    version="0.1.0"
)

# The HTTP server must become useful before optional integrations, repair jobs,
# and remote housekeeping finish. Keep a small, explicitly serial queue for
# those tasks: it prevents a restart from stampeding SQLite/vault resources,
# while exposing truthful progress to the client via /health.
app.state.startup_phase = "starting"
app.state.startup_pending = []
app.state.startup_deferred_queue = []
app.state.startup_deferred_task = None
app.state.startup_active_detail = ""


def _queue_deferred_startup(label: str, operation) -> None:
    """Queue one non-critical startup operation for the post-ready worker.

    ``operation`` is a zero-argument async callable. The queue is drained in
    order, not with ``gather()``, because several legacy operations open the
    same SQLite files and vaults. A failure is recorded and the next item still
    runs, so one optional integration cannot keep the app in "connecting".
    """
    if os.environ.get("WEBAGENT_V2_ISOLATED") == "1" and label in {
        "communications", "hybrid_sync", "leader_services", "device_worker",
        "p2p_worker", "orphaned_run_recovery", "activity_summary_backfill",
        "accounts_and_admin_repair", "agent_admin_backfill",
        "jwt_secret_reconcile",
    }:
        logger.info("Isolated V2: suppressed startup service %s", label)
        return
    app.state.startup_deferred_queue.append((label, operation))


async def _drain_deferred_startup() -> None:
    queue = list(app.state.startup_deferred_queue)
    app.state.startup_pending = [label for label, _ in queue]
    for label, operation in queue:
        try:
            await operation()
        except Exception as exc:  # a deferred task must never take down serving
            logger.warning("Deferred startup task %s failed: %s", label, exc)
        finally:
            app.state.startup_pending = [item for item in app.state.startup_pending if item != label]
    app.state.startup_phase = "ready"
    logger.info("Deferred startup complete")


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception):
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    logger.error("Unhandled exception on %s %s\n%s", request.method, request.url.path, tb)
    # Also record the 500 in the flight-recorder's http category (the logger.error
    # above lands in the `server` category via the diagnostics log handler).
    try:
        from app.api.http_diag import record_server_error
        record_server_error(request, exc, tb)
    except Exception:
        pass
    return JSONResponse(status_code=500, content={"detail": "Internal server error", "error": str(exc)})


# ── Access log ──
# Record every (non-static) HTTP request into the diagnostics `access` category
# in the dedicated logs.db, so the flight recorder shows the full HTTP timeline,
# not just errors. Static asset GETs are skipped to keep the log signal-rich.
_ACCESS_SKIP_PREFIXES = ("/ui/", "/static", "/assets", "/screenshots/",
                         "/visuals/", "/user_data/", "/favicon", "/web-terminal")
_ACCESS_SKIP_EXT = (".js", ".css", ".map", ".ico", ".svg", ".png", ".jpg",
                    ".jpeg", ".gif", ".webp", ".woff", ".woff2", ".ttf")


@app.middleware("http")
async def _access_log_middleware(request: Request, call_next):
    import time as _t
    start = _t.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        try:
            path = request.url.path
            if not (path.startswith(_ACCESS_SKIP_PREFIXES) or path.endswith(_ACCESS_SKIP_EXT)):
                from app.api.http_diag import _user_from_request
                from app.agent.diagnostics import record_access
                record_access(
                    request.method, path, status,
                    int((_t.perf_counter() - start) * 1000),
                    user_id=_user_from_request(request),
                    client=(request.client.host if request.client else None),
                )
        except Exception:
            pass


# Settings are inspectable by every caller, including anonymous visitors, but
# every mutation underneath an admin route must be made by a verified app admin.
# This is a server-side boundary: browser visibility is presentation, not auth.
@app.middleware("http")
async def _admin_settings_mutation_guard(request: Request, call_next):
    unsafe = request.method.upper() not in {"GET", "HEAD", "OPTIONS"}
    if unsafe and request.url.path.startswith("/admin/"):
        from app.auth.identity import request_user_id
        caller_id = request_user_id(request)
        if not caller_id:
            return JSONResponse(status_code=401, content={"detail": "Not authenticated"})
        try:
            from app.db import get_db
            allowed = await get_db().is_user_admin(caller_id)
        except Exception:
            allowed = False
        if not allowed:
            return JSONResponse(status_code=403, content={"detail": "App admin access required."})
    return await call_next(request)


# ── Favicon ──
@app.get("/favicon.ico", include_in_schema=False)
@app.get("/favicon.svg", include_in_schema=False)
async def favicon():
    from fastapi.responses import FileResponse
    return FileResponse(str(_APP_DIR.parent / "ui" / "favicon.svg"), media_type="image/svg+xml")


# ── PWA: service worker + manifest (correct MIME types) ──
@app.get("/sw.js", include_in_schema=False)
async def service_worker():
    sw_path = _APP_DIR.parent / "sw.js"
    if not sw_path.is_file():
        return HTMLResponse("", status_code=404)
    # Never let the browser serve a stale service-worker script from HTTP cache —
    # a new sw.js (e.g. a cache-version bump) must be discovered on next load.
    return FileResponse(
        str(sw_path),
        media_type="text/javascript",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


@app.get("/ui/manifest.json", include_in_schema=False)
async def pwa_manifest():
    m_path = _APP_DIR.parent / "ui" / "manifest.json"
    if not m_path.is_file():
        return HTMLResponse("", status_code=404)
    return FileResponse(
        str(m_path),
        media_type="application/manifest+json",
        headers={
            "Cache-Control": "max-age=86400",
            "X-Content-Type-Options": "nosniff",
        },
    )


# Browser API origins are a boot-time deployment policy.  Same-origin requests
# need no CORS grant; cross-origin browser callers must be listed exactly.
_web_security_policy = WebSecurityPolicy.from_env()
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(_web_security_policy.allowed_origins),
    allow_credentials=_web_security_policy.allow_credentials,
    allow_methods=list(CORS_ALLOW_METHODS),
    allow_headers=list(CORS_ALLOW_HEADERS),
)
app.add_middleware(NoCacheMiddleware)
# Gzip static /ui text assets (JS/CSS/HTML/JSON/SVG) — the big uncompressed
# first-load payload. Scoped to static files only, so no streaming/API endpoint
# is affected. See StaticGzipMiddleware above.
app.add_middleware(StaticGzipMiddleware)

# Decode the request's JWT once and stash the VERIFIED caller id in a
# contextvar, so token-less admin chokepoints (app.auth.identity.resolve_admin_uid)
# can demand a cryptographically-proven identity instead of trusting a
# client-claimed ``requesting_user_id``. Pure-ASGI so the contextvar reliably
# reaches the endpoint. See app/auth/identity.py.
from app.auth.identity import CallerIdentityMiddleware
app.add_middleware(CallerIdentityMiddleware)
# Registered last so this pure-ASGI boundary runs before identity decoding,
# route authentication, WebSocket acceptance, or browser/session allocation.
app.add_middleware(RequestSecurityMiddleware, policy=_web_security_policy)


@app.middleware("http")
async def _capture_public_base_url(request, call_next):
    """Cache the base URL of every incoming request so background code paths
    (agent tools, scheduler) that have no Request object can still build
    correct OAuth redirect URIs instead of falling back to a hardcoded port.

    Localhost requests update the cache only when nothing public has been seen
    yet — that way local dev works on whatever port uvicorn is bound to, but
    production internal pings on 127.0.0.1 don't poison the cached domain."""
    try:
        from app.admin import integrations as _integ
        derived = str(request.base_url).rstrip("/")
        forwarded_proto = request.headers.get("x-forwarded-proto", "")
        if forwarded_proto and derived.startswith("http://") and _integ._is_trusted_proxy(request):
            derived = "https://" + derived[len("http://"):]
        if derived:
            is_local = derived.startswith("http://localhost") or derived.startswith("http://127.")
            cached = _integ._LAST_SEEN_BASE_URL
            cache_is_public = bool(cached) and not (
                cached.startswith("http://localhost") or cached.startswith("http://127.")
            )
            if not (is_local and cache_is_public):
                _integ._LAST_SEEN_BASE_URL = derived
    except Exception:
        pass
    return await call_next(request)


@app.middleware("http")
async def _canonical_host_redirect(request, call_next):
    """301-redirect requests that arrive on a non-canonical host.

    When the deployment has a detected public URL, any request reaching the app
    on a different host (apex, preview URL, etc.) is sent a 301 to the same
    path on that host. This keeps OAuth ``redirect_uri`` values aligned with
    what's registered at the provider regardless of which DNS name the user
    typed in.

    No-op when no canonical URL is detected; localhost requests are exempt
    so local development is never redirected. Hosts listed in
    webhook_base_url_exclude.json are also exempt — the admin can un-redirect
    individual hostnames from the Instances page."""
    # Decide whether to redirect *without* invoking the downstream app, so a
    # failure in the redirect logic can never swallow (and retry) call_next.
    # Calling call_next twice on one request deadlocks: the ASGI receive stream
    # is already consumed, so the second await blocks forever — which is how any
    # downstream 500 used to turn into a silent hang instead of an error.
    redirect_target = None
    try:
        from app.admin.integrations import _get_configured_base_url
        canonical = _get_configured_base_url()
        if canonical:
            host = (request.headers.get("x-forwarded-host") or request.url.netloc or "").split(",")[0].strip()
            if host and not host.startswith("localhost") and not host.startswith("127."):
                # Check per-host exclusions
                excluded: bool = False
                try:
                    import json
                    from pathlib import Path as _P
                    _ef = _P(__file__).resolve().parent.parent / "webhook_base_url_exclude.json"
                    if _ef.exists():
                        _excl = json.loads(_ef.read_text() or "[]")
                        if isinstance(_excl, list) and host.lower() in [str(x).lower() for x in _excl]:
                            excluded = True
                except Exception:
                    pass
                if not excluded:
                    scheme = request.url.scheme or "http"
                    xfp = request.headers.get("x-forwarded-proto", "")
                    if xfp and _integ._is_trusted_proxy(request):
                        scheme = xfp.split(",")[0].strip()
                    actual = f"{scheme}://{host}".rstrip("/")
                    if actual.lower() != canonical.rstrip("/").lower():
                        target = canonical.rstrip("/") + request.url.path
                        if request.url.query:
                            target += "?" + request.url.query
                        redirect_target = target
    except Exception:
        redirect_target = None
    if redirect_target:
        return RedirectResponse(redirect_target, status_code=301)
    # Single call_next, outside the try — downstream exceptions propagate to the
    # real exception handlers (→ proper 500) instead of being caught here.
    return await call_next(request)


# HTTP-error capture for the flight-recorder — records every raised 4xx/5xx
# (status, method, path, cause, user) into the recorder's `http` category +
# logs/http.log, via exception handlers that delegate to FastAPI's defaults
# (so responses are unchanged). 500s are recorded from the global Exception
# handler above. Reliable + stream-safe (no middleware in the request path).
try:
    from app.api.http_diag import install_http_diagnostics
    install_http_diagnostics(app)
except Exception as _httpdiag_err:  # never let diagnostics wiring break boot
    logger.warning("HTTP diagnostics handlers not installed: %s", _httpdiag_err)


# Include routers
app.include_router(chat_router)
app.include_router(agent_router)
app.include_router(agents_router)
app.include_router(agent_profiles_router)
app.include_router(agent_pages_router)
app.include_router(session_router)
app.include_router(boot_router)
app.include_router(entitlements_router)
app.include_router(status_router)
app.include_router(auth_router)
app.include_router(content_router)
app.include_router(wiki_router)


async def _mount_optional_routers() -> None:
    """Import and attach non-interactive APIs after core readiness.

    Each implementation import runs in a worker thread so Python's synchronous
    import machinery cannot freeze health, chat, or cached-session requests.
    Routers attach one at a time, which lets capabilities become available as
    soon as their own import completes instead of waiting for the whole bundle.
    """
    if getattr(app.state, "optional_routers_mounted", False):
        return
    mounted: list[str] = []
    failed: list[str] = []
    for spec in OPTIONAL_ROUTES:
        app.state.startup_active_detail = f"route:{spec.label}"
        try:
            router = await asyncio.to_thread(load_optional_router, spec)
            app.include_router(router)
            mounted.append(spec.label)
            if spec.label == "billing":
                for extension_router in await asyncio.to_thread(load_billing_extension_routers):
                    app.include_router(extension_router)
        except Exception as exc:
            failed.append(spec.label)
            logger.warning("Optional API %s did not mount: %s", spec.label, exc)
    app.state.startup_active_detail = ""
    app.state.optional_routers_mounted = True
    # A client may have opened /docs while optional APIs were still loading.
    # Invalidate that core-only snapshot so the next schema request is complete.
    app.openapi_schema = None
    logger.info("Optional APIs mounted=%d failed=%s", len(mounted), failed)

async def _mount_dropin_page_backends() -> None:
    """Mount optional page APIs after the interactive core is available.

    Discovering a page backend executes arbitrary drop-in Python modules.  That
    work used to happen while importing ``app.main``, which meant a visitor
    could wait behind admin-only/optional page code before the real API even
    became ready.  These routes are deliberately additive, so mounting them
    once from the deferred startup queue preserves their behaviour without
    putting them on the cold-path for chat, auth, Agents, or Wiki.
    """
    if getattr(app.state, "dropin_page_backends_mounted", False):
        return
    try:
        from app import ui_pages as _ui_pages
        # Module execution and filesystem discovery can be slow on a cold
        # Windows disk.  Do that work outside the serving event loop; mounting
        # the finished routers below is in-memory and deliberately tiny.
        discovered = await asyncio.to_thread(_ui_pages.discover_routers)
        for _pid, _page_router in discovered:
            try:
                app.include_router(_page_router)
                logger.info("Registered drop-in page backend: %s", _pid)
            except Exception as _e:
                logger.warning("Could not mount page backend %s: %s", _pid, _e)
        app.state.dropin_page_backends_mounted = True
    except Exception as _e:
        logger.warning("Page backend discovery failed: %s", _e)

async def _mount_dropin_ability_backends() -> None:
    """Mount optional ability APIs after core readiness.

    Ability-router discovery imports every installed ability runtime.  It is
    useful work, but not a prerequisite for a cached session, sign-in, chat,
    or the public Wiki.  Keeping it behind readiness avoids making the shell
    wait for optional integrations and makes its progress visible in /health.
    """
    if getattr(app.state, "dropin_ability_backends_mounted", False):
        return
    try:
        from app import abilities as _abilities_mgr
        discovered = await asyncio.to_thread(_abilities_mgr.ability_routers)
        for _spec in discovered:
            try:
                app.include_router(_spec["router"])
                logger.info("Registered drop-in ability backend: %s", _spec["id"])
            except Exception as _e:
                logger.warning("Could not mount ability backend %s: %s", _spec.get("id"), _e)
        app.state.dropin_ability_backends_mounted = True
    except Exception as _e:
        logger.warning("Ability backend discovery failed: %s", _e)

# ── Restart endpoint ──
# POST /api/v1/restart uses the supervisor-cooperative relauncher.  This lets
# WebAgent.bat's restart loop win when it launched the server, while still
# relaunching a standalone ``run.py`` process when no supervisor is present.
restart_router = APIRouter(prefix="/api/v1")

@restart_router.post("/restart")
async def restart_server(
    _admin_user_id: str = Depends(require_platform_admin),
):
    """Restart without stranding a server that was not batch-supervised."""
    from app.relauncher import trigger_restart

    result = trigger_restart()
    if result.get("auto_restart"):
        logger.warning("Restart requested via /api/v1/restart — relaunching...")
    else:
        logger.error("Restart requested via /api/v1/restart, but unavailable: %s",
                     result.get("reason"))
    return result

app.include_router(restart_router)

# ── Static file mounts ──
# Legacy top-level screenshots are not publicly mounted. New screenshots live
# in a user workspace and are served through the authenticated content broker.

# Uploads, generated images (visuals), and Gen UI page bodies now all live in
# the per-user data home (data/user_data/<uid>/{uploads,visuals,genui}) and serve
# from the /user_data mount below — so the old top-level /uploads and /visuals
# mounts are retired. (Gen UI are served via the /api/v1/genui route, not a mount.)

# User workspace bytes are deliberately not mounted as static files. They are
# streamed through /api/v1/content after workspace and grant verification.

_UI_DIR = _APP_DIR.parent / "ui"

_SCRIPT_WITHOUT_NONCE_RE = re.compile(r"<script\b(?![^>]*\bnonce=)", re.IGNORECASE)


def _secure_html_response(
    content: str,
    *,
    status_code: int = 200,
    frame_ancestors: str = "'self'",
) -> HTMLResponse:
    """Nonce executable script while preserving inline-style compatibility."""
    nonce = secrets.token_urlsafe(24)
    secured = _SCRIPT_WITHOUT_NONCE_RE.sub(
        f'<script nonce="{nonce}"', content,
    )
    csp = (
        "default-src 'self'; "
        "base-uri 'self'; "
        "object-src 'none'; "
        f"frame-ancestors {frame_ancestors}; "
        "form-action 'self'; "
        f"script-src 'self' 'nonce-{nonce}'; "
        "script-src-attr 'none'; "
        "style-src 'self' 'unsafe-inline'; "
        "connect-src 'self'; "
        "img-src 'self' data: blob:; "
        "media-src 'self' blob:; "
        "font-src 'self' data:; "
        "frame-src 'self'; "
        "worker-src 'self' blob:; "
        "manifest-src 'self'"
    )
    headers = {
        "Content-Security-Policy": csp,
        "Content-Security-Policy-Report-Only": csp,
        "X-Content-Type-Options": "nosniff",
    }
    if frame_ancestors == "'self'":
        headers["X-Frame-Options"] = "SAMEORIGIN"
    return HTMLResponse(content=secured, status_code=status_code, headers=headers)


class _UIStaticFiles(StaticFiles):
    """Serve drop-in frontend assets but NEVER the Python source alongside them
    (a page/engine's ``server.py`` / adapter, ``*.py.bak`` backups, etc.). Both
    the ``/ui`` mount (page folders may hold a server-side router) and the
    ``/plugins/engines`` mount (each engine folder holds its Python adapter next
    to an optional ``ui/`` asset folder) use this so source stays private. 404 any
    .py / .pyc / .pyo / .bak / __pycache__ path."""

    async def get_response(self, path, scope):
        norm = path.replace("\\", "/").lower()
        if norm.endswith((".py", ".pyc", ".pyo", ".bak")) or "__pycache__/" in norm:
            from starlette.responses import PlainTextResponse
            return PlainTextResponse("Not Found", status_code=404)
        response = await super().get_response(path, scope)
        if norm.endswith(".html") and response.status_code == 200:
            try:
                content = Path(response.path).read_text(encoding="utf-8")
                return _secure_html_response(content)
            except (OSError, UnicodeError):
                return PlainTextResponse("Not Found", status_code=404)
        return response


app.mount("/ui", _UIStaticFiles(directory=str(_UI_DIR)), name="ui")

# RECORDER-HOOK: generic application plugin catalog/assets, consumed by
# plugins/recorder/plugin.py. Remove this include and deferred/lifecycle hooks
# together if dropping application plugins; retain the /ui mount above.
from app import application_plugins as _application_plugins
app.include_router(_application_plugins.router)

@app.on_event("shutdown")
async def _stop_application_plugins():
    # RECORDER-HOOK: flush this worker only. Other workers may still be serving.
    await _application_plugins.stop()



# Engine-owned frontend assets. An alternate-engine folder
# (plugins/engines/<id>/) may ship a ``ui/`` folder with its own JS/CSS — the
# frontend twin of the engine's Python adapter. The core loads it lazily (dynamic
# import) only when that engine's session mounts, so a normal agent never fetches
# it. Served generically here (no per-engine wiring) at /plugins/engines/…; the
# _UIStaticFiles guard keeps every engine's Python adapter private. Shipped
# consumer: the terminal_chat engine's on-screen keys
# (plugins/engines/terminal_chat/ui/terminal-keys.{js,css}).
_ENGINES_DIR = _APP_DIR.parent / "plugins" / "engines"
try:
    if _ENGINES_DIR.is_dir():
        app.mount("/plugins/engines", _UIStaticFiles(directory=str(_ENGINES_DIR)), name="engines")
        logger.info("Engine frontend assets mounted at /plugins/engines")
except Exception as e:
    logger.warning("Could not mount /plugins/engines: %s", e)

_ROOT_INDEX_HTML = _APP_DIR.parent / "index.html"


# ── SEO / social-preview meta injection ─────────────────────────────────────
# Default copy for the app shell. Per-agent public pages override title + desc
# with the agent's name so a shared /{agent_id} link previews meaningfully.
_SEO_DEFAULT_TITLE = "WebAgent — your AI agent harness"
_SEO_DEFAULT_DESC = (
    "WebAgent is a self-hostable AI agent harness: chat with capable agents, "
    "give them tools and automations, and build visual pages — all in one app."
)
# Dedicated 1200×630 landscape social card — the size Open Graph / Twitter
# "summary_large_image" expects. Generated asset (regenerate with
# scripts/make_og_card.py). The square app icon is kept only as the Organisation
# LOGO in structured data, never as the share-preview image (a square icon in a
# large-image card previews badly).
_SEO_PREVIEW_IMAGE = "/ui/icons/og-card.png"
_SEO_LOGO_IMAGE = "/ui/icons/icon-512x512.png"  # square brand mark (schema.org logo)
# Authoritative external profiles for the brand entity (schema.org `sameAs` on the
# Organisation) — strengthens entity recognition and brand sitelinks. Add more as
# they come online (X, LinkedIn, Discord, …); an empty list omits the field.
_SEO_SAMEAS = ["https://github.com/botboss3000/webagent"]

# Marketing copy for the public landing front page (see _render_landing_page).
_LANDING_TITLE = "WebAgent — your own team of AI agents"
_LANDING_DESC = (
    "Chat, automate, browse the web, build live dashboards and share knowledge — "
    "all driven by tool-using agents you shape to fit the way you work."
)
_SPLASH_DIR = _APP_DIR.parent / "ui" / "splash" / "splash-page"


def _seo_origin(request: Request) -> str:
    """Absolute origin (scheme+host, no trailing slash) for Open Graph / canonical
    urls and sitemap entries. Reuses the integrations base-url resolver so it
    is https-correct behind a TLS-terminating proxy, falling back to the
    request-derived host."""
    try:
        from app.admin.integrations import _get_base_url
        return _get_base_url(request).rstrip("/")
    except Exception:
        return str(request.base_url).rstrip("/")


def _iso_ts(ts) -> str:
    """Normalise a stored timestamp to ISO 8601 for schema.org / Open Graph.
    Handles the bare SQLite `datetime('now')` form ("YYYY-MM-DD HH:MM:SS", assumed
    UTC → swap the space for "T" and append "Z") and passes an already-full ISO
    value (with "T" and/or a timezone) straight through. Empty in → empty out."""
    ts = (ts or "").strip()
    if not ts:
        return ""
    ts = ts.replace(" ", "T", 1)
    return ts + "Z" if len(ts) == 19 and ts[10] == "T" else ts


def _seo_head_block(origin: str, path: str, title: str, desc: str, noindex: bool = False,
                    og_type: str = "website", published=None, modified=None,
                    image: str = None) -> str:
    """Build the shared SEO / social-preview <head> tags (description, canonical,
    Open Graph, Twitter card) with ABSOLUTE urls — link scrapers reject relative
    og:image / og:url. Used by both the app shell and the landing page. Pass
    noindex=True for shareable-but-not-indexed pages (e.g. public agent links).
    Pass og_type="article" with published/modified timestamps for a wiki article so
    it previews as a dated article (adds article:published_time/modified_time)
    rather than a generic website. Pass image=<absolute url> to override the default
    1200×630 share card (e.g. a wiki article's own first image); the card's fixed
    width/height tags are emitted only for that default, since a custom image's
    dimensions are unknown."""
    from html import escape as _esc
    page_url = origin + (path or "/")
    image_url = image or (origin + _SEO_PREVIEW_IMAGE)
    et, ed = _esc(title, quote=True), _esc(desc, quote=True)
    eurl, eimg = _esc(page_url, quote=True), _esc(image_url, quote=True)
    robots = '<meta name="robots" content="noindex, follow">\n' if noindex else ""
    dims = "" if image else (
        '<meta property="og:image:width" content="1200">\n'
        '<meta property="og:image:height" content="630">\n'
    )
    article_meta = ""
    if og_type == "article":
        for prop, raw in (("article:published_time", published), ("article:modified_time", modified)):
            iso = _iso_ts(raw)
            if iso:
                article_meta += f'<meta property="{prop}" content="{_esc(iso, quote=True)}">\n'
    return (
        robots
        + f'<meta name="description" content="{ed}">\n'
        f'<link rel="canonical" href="{eurl}">\n'
        f'<meta property="og:type" content="{_esc(og_type, quote=True)}">\n'
        + article_meta
        + f'<meta property="og:site_name" content="WebAgent">\n'
        f'<meta property="og:title" content="{et}">\n'
        f'<meta property="og:description" content="{ed}">\n'
        f'<meta property="og:url" content="{eurl}">\n'
        f'<meta property="og:image" content="{eimg}">\n'
        + dims
        + f'<meta property="og:image:alt" content="{et}">\n'
        f'<meta property="og:locale" content="en_US">\n'
        f'<meta name="twitter:card" content="summary_large_image">\n'
        f'<meta name="twitter:title" content="{et}">\n'
        f'<meta name="twitter:description" content="{ed}">\n'
        f'<meta name="twitter:image" content="{eimg}">\n'
    )


# ── Structured data (schema.org / JSON-LD) ──────────────────────────────────
# Machine-readable summaries crawlers use for rich results: the brand identity
# under the site name, dated article cards, and breadcrumb trails. Emitted as
# <script type="application/ld+json"> blocks appended to the SEO <head>.

def _jsonld_block(*objs) -> str:
    """Serialize one or more schema.org objects into a single JSON-LD <script>.
    Values are JSON-escaped, and any literal "</" is neutralised so an article's
    own text can never close the <script> tag early."""
    import json
    data = objs[0] if len(objs) == 1 else list(objs)
    text = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return f'<script type="application/ld+json">{text}</script>\n'


def _home_jsonld(origin: str) -> str:
    """Organisation + WebSite structured data for the public home page — lets
    search engines treat the site as a named entity (brand name + logo) and is
    the basis for brand sitelinks. Emitted only on indexable home surfaces."""
    org = {
        "@context": "https://schema.org",
        "@type": "Organization",
        "name": "WebAgent",
        "url": origin + "/",
        "logo": origin + _SEO_LOGO_IMAGE,
        "description": _SEO_DEFAULT_DESC,
    }
    if _SEO_SAMEAS:
        org["sameAs"] = list(_SEO_SAMEAS)
    site = {
        "@context": "https://schema.org",
        "@type": "WebSite",
        "name": "WebAgent",
        "url": origin + "/",
    }
    return _jsonld_block(org, site)


def _breadcrumb_obj(origin: str, trail) -> dict:
    """A schema.org BreadcrumbList from a list of (name, path) pairs."""
    return {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": i + 1, "name": name, "item": origin + path}
            for i, (name, path) in enumerate(trail)
        ],
    }


def _wiki_article_jsonld(origin, slug, title, desc, published, modified, image=None) -> str:
    """Article + breadcrumb structured data for one wiki page — makes it eligible
    for dated article rich results (headline, publish/updated dates, publisher).
    image overrides the default share card with the article's own first image."""
    page_url = origin + "/wiki/" + slug
    article = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": title,
        "description": desc,
        "url": page_url,
        "mainEntityOfPage": {"@type": "WebPage", "@id": page_url},
        "image": image or (origin + _SEO_PREVIEW_IMAGE),
        "author": {"@type": "Organization", "name": "WebAgent", "url": origin + "/"},
        "publisher": {
            "@type": "Organization",
            "name": "WebAgent",
            "logo": {"@type": "ImageObject", "url": origin + _SEO_LOGO_IMAGE},
        },
    }
    # Normalise stored timestamps to ISO 8601 (schema.org wants it); see _iso_ts.
    if published:
        article["datePublished"] = _iso_ts(published)
    if modified:
        article["dateModified"] = _iso_ts(modified)
    crumb = _breadcrumb_obj(origin, [("Home", "/"), ("Wiki", "/wiki"), (title, "/wiki/" + slug)])
    return _jsonld_block(article, crumb)


def _resolve_og_image(url: str, origin: str):
    """Turn a wiki image url into an ABSOLUTE share-image url, or None to fall back
    to the default card. External http(s) urls are used as-is; a site-relative
    "/path" url is made absolute against the origin. Anything else (bare-relative,
    protocol-relative "//", data:, fragment, unknown scheme) is skipped — a broken
    image just yields no preview, which crawlers tolerate."""
    u = (url or "").strip()
    if not u:
        return None
    if u.lower().startswith(("http://", "https://")):
        return u
    if u.startswith("/") and not u.startswith("//"):
        return origin + u
    return None


def _first_article_image(body: str, origin: str):
    """The article's first Markdown image (![alt](url)) resolved to an absolute
    share image — or None. Fenced code blocks are stripped first so an image in a
    code sample doesn't count."""
    if not body:
        return None
    stripped = re.sub(r"```.*?```", " ", body, flags=re.S)
    m = re.search(r"!\[[^\]]*\]\(([^)\s]+)\)", stripped)
    if not m:
        return None
    return _resolve_og_image(m.group(1), origin)


# Conservative match for search / social crawler User-Agents. Used so the home
# page serves crawlable content even when the human welcome experience is off.
_CRAWLER_UA = re.compile(
    r"googlebot|bingbot|slurp|duckduckbot|baiduspider|yandex|sogou|exabot|ia_archiver|"
    r"facebookexternalhit|facebot|twitterbot|linkedinbot|slackbot|telegrambot|whatsapp|"
    r"discordbot|applebot|pinterest|redditbot|embedly|quora|bitlybot|skypeuripreview|"
    r"\bbot\b|crawler|spider",
    re.IGNORECASE,
)


def _is_crawler(request: Request) -> bool:
    """True if the request looks like a search/social crawler (by User-Agent)."""
    return bool(_CRAWLER_UA.search(request.headers.get("user-agent", "")))


def _render_app_shell(
    request: Request,
    agent_id=None,
    agent_name=None,
    *,
    chat_portal: bool = False,
    chat_portal_config=None,
    route_error=None,
) -> HTMLResponse:
    """Serve index.html with the SEO / social-preview <head> built server-side.

    The static index.html ships only a bare <title>; this injects the full
    description / canonical / Open Graph / Twitter-card block with ABSOLUTE urls.
    For a public agent link it also swaps the title + description to that agent's
    name, so sharing the link previews as "Chat with <Agent>" rather than a
    generic "WebAgent" (and marks it noindex — shareable-only). This is the single
    source of truth for app-shell head SEO — keep it here, not in the static file
    (see the matching KEEP comment in index.html)."""
    import json
    from html import escape as _esc
    if not _ROOT_INDEX_HTML.is_file():
        return HTMLResponse("<p>Missing index.html</p>", status_code=404)
    html = _ROOT_INDEX_HTML.read_text(encoding="utf-8")
    origin = _seo_origin(request)
    path = request.url.path or "/"
    title = _SEO_DEFAULT_TITLE
    desc = _SEO_DEFAULT_DESC
    if agent_name:
        title = f"Chat with {agent_name} — WebAgent"
        desc = f"Chat with {agent_name}, an AI agent on WebAgent."
    elif route_error:
        # Keep failed public links non-indexable and generic: the response must
        # not reveal whether an agent was deleted, disabled, or never existed.
        title = "Page not found — WebAgent"
        desc = "This WebAgent page is not available."
    # /app and /index.html serve the SAME home shell as / — point their canonical
    # (and og:url) at / so search engines treat the three as one page rather than
    # duplicate content. Public-agent and route-error URLs keep their own path and
    # are noindex, so neither becomes a misleading canonical home page.
    canonical_path = path if (agent_id or route_error) else "/"
    meta = _seo_head_block(origin, canonical_path, title, desc, noindex=bool(agent_id or route_error))
    if route_error:
        meta += f"<script>window.__routeError = {json.dumps(route_error)};</script>\n"
    if agent_id:
        meta += (
            f"<script>window.__agentId = {json.dumps(agent_id)}; "
            f"window.__agentName = {json.dumps(agent_name or 'Agent')};</script>\n"
        )
        # Public agent and embed routes are nested paths. Rootify the static
        # shell assets and set a same-origin base before any markup that can
        # resolve page-relative dynamic URLs (backgrounds/partials). The CSP
        # permits only a same-origin base, never an attacker-controlled origin.
        html = html.replace('"./ui/', '"/ui/').replace("'./ui/", "'/ui/")
        html = html.replace("<head>", '<head>\n<base href="/">', 1)
    else:
        # Brand identity (Organisation + WebSite) for the indexable app home. A
        # public agent link is noindex, so it skips this.
        meta += _home_jsonld(origin)
    if chat_portal:
        meta += (
            '<script>document.documentElement.classList.add("chat-portal"); '
            'window.__CHAT_PORTAL__ = true; '
            f'window.__CHAT_PORTAL_CONFIG__ = {json.dumps(chat_portal_config or {}).replace("<", "\\u003c")};</script>\n'
            '<link rel="stylesheet" href="/ui/embed/chat-panel-portal.css?v=2">\n'
        )
        html = html.replace(
            "</body>",
            '<script type="module" src="/ui/embed/chat-panel-portal.js?v=1"></script>\n</body>',
            1,
        )
    html = html.replace("<title>WebAgent</title>", f"<title>{_esc(title, quote=True)}</title>", 1)
    html = html.replace("</head>", meta + "</head>", 1)
    # ── Safety lock: inject the lock state + blocking script ──
    # The server knows the lock state at render time, so we inline it
    # directly rather than making a second fetch. This runs synchronously
    # before any other JS, blocking render until the admin decides.
    # Gated by the master switch (safety_lock_enabled) AND the persistent
    # lock flag (safety_lock_active) AND the in-memory session unlock.
    try:
        from app.admin.settings import get_safety_lock_enabled, get_safety_lock_active
        _safety_show = (
            get_safety_lock_enabled()
            and get_safety_lock_active()
            and not _safety_session_unlocked
        )
    except Exception:
        _safety_show = False
    if chat_portal:
        _safety_show = False
    _safety_script = '<script>window.__SAFETY_LOCK=' + json.dumps(_safety_show) + ';</script>\n'
    if _safety_show and not chat_portal:
        _safety_script += '<script src="/ui/shared/js/safety-splash.js"></script>\n'
    html = html.replace("</head>", _safety_script + "</head>", 1)
    route_error_status = int(route_error.get("status", 200)) if isinstance(route_error, dict) else 200
    return _secure_html_response(html, status_code=route_error_status)


def _render_landing_page(request: Request):
    """Server-rendered marketing landing page for the front door (/).

    Reuses the splash plugin's markup + stylesheet (ui/splash/splash-page/) so it's
    the same premium welcome — but delivered as a REAL crawlable page, with all the
    copy present in the initial HTML, instead of a JavaScript overlay a search
    crawler can't see. The markup is wrapped in the same #splash-root container the
    splash CSS targets, with `is-ready` set inline so the content is visible even
    before JavaScript runs. Returns None when the splash folder is absent, so the
    caller falls back to the app shell and deleting ui/splash/splash-page/ cleanly
    removes the landing (drop-in)."""
    from html import escape as _esc
    markup_file = _SPLASH_DIR / "splash-page.html"
    if not markup_file.is_file():
        return None
    try:
        markup = markup_file.read_text(encoding="utf-8")
    except Exception:
        return None
    origin = _seo_origin(request)
    head = _seo_head_block(origin, "/", _LANDING_TITLE, _LANDING_DESC, noindex=False)
    head += _home_jsonld(origin)
    et = _esc(_LANDING_TITLE, quote=True)
    doc = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n'
        '<meta charset="UTF-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">\n'
        f"<title>{et}</title>\n"
        + head
        + '<meta name="theme-color" content="#0d0d1a">\n'
        '<link rel="icon" href="/ui/favicon.svg" type="image/svg+xml">\n'
        '<link rel="manifest" href="/ui/manifest.json">\n'
        '<link rel="apple-touch-icon" href="/ui/icons/icon-512x512.png">\n'

        # design-system tokens + the live appearance overrides so the landing is
        # correctly themed (dark/light + admin palette) exactly like the app.
        '<link rel="stylesheet" href="/ui/shared/css/design-system.css">\n'
        '<script src="/ui/shared/js/appearance.js"></script>\n'
        '<link rel="stylesheet" href="/ui/splash/splash-page/splash-page.css">\n'
        "<style>\n"
        "  /* Standalone base — the splash CSS is scoped under #splash-root and\n"
        "     assumes the app's resets around it; supply the minimum here. */\n"
        "  * { margin: 0; padding: 0; box-sizing: border-box; }\n"
        "  html, body { background: var(--bg-0); color: var(--fg-1); min-height: 100vh; }\n"
        "</style>\n"
        "</head>\n<body>\n"
        '<div id="splash-root" class="is-ready">\n'
        + markup
        + "\n</div>\n"
        '<script type="module" src="/ui/splash/splash-page/js/splash-landing.js"></script>\n'
        "</body>\n</html>\n"
    )
    return _secure_html_response(doc)


# ── Cleanup on shutdown ──
@app.on_event("shutdown")
async def shutdown():
    """Close browser instances and persistent terminal session on server shutdown."""
    app.state.startup_phase = "stopping"
    if performance_test_mode():
        app.state.startup_phase = "stopped"
        return
    # A deferred seed/migration may still own a SQLite connection or vault handle.
    # Cancel it before the normal shutdown sequence closes those resources.
    _deferred = getattr(app.state, "startup_deferred_task", None)
    if _deferred is not None and not _deferred.done():
        _deferred.cancel()
        try:
            await _deferred
        except asyncio.CancelledError:
            pass
        except Exception:
            pass
    try:
        from app.tools.browser import close_all
        await close_all()
    except Exception:
        pass
    try:
        from app.api.terminal import close_persistent_session, stop_idle_gc
        stop_idle_gc()
        await close_persistent_session()
    except Exception:
        pass
    # Remove Telegram webhook on shutdown
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        tg = pm.get_plugin("telegram")
        if tg and tg.enabled:
            await tg.delete_webhook_url()
    except Exception:
        pass
    # ── Safety lock: set the flag so next startup shows the confirmation splash ──
    try:
        from app.admin.settings import set_safety_lock_active
        set_safety_lock_active()
        logger.info("Safety lock set — next startup will require admin confirmation")
    except Exception as _sl_err:
        logger.warning("Failed to set safety lock on shutdown: %s", _sl_err)
    # Stop all singleton background services (scheduler, event runtime, ability
    # pollers, watchdog, Remote Access) and release the leader lease for a clean
    # handoff. The leader runs them only in the elected worker; stop() is a no-op
    # in workers that never held leadership.
    try:
        from app.coordination.leader import get_leader
        await get_leader().stop()
    except Exception:
        pass
    # Stop the per-instance device worker (multi-device dispatch). See app/devices/.
    try:
        from app.devices import stop_device_worker
        await stop_device_worker()
    except Exception:
        pass
    # Stop the per-instance P2P mirror worker.
    try:
        from app.p2p.worker import stop_worker as stop_p2p_worker
        await stop_p2p_worker()
    except Exception:
        pass
    # Stop the hybrid sync engine and let it flush any final pending pushes.
    try:
        _engine = getattr(app.state, "hybrid_sync_engine", None)
        if _engine is not None:
            await _engine.stop()
    except Exception:
        pass
    # Close the DB-viewer's shared autocommit connection pool (chat-panel reads).
    try:
        from app.db.pg_portable import close_viewer_pool
        close_viewer_pool()
    except Exception:
        pass


# ── In-memory session unlock for the safety lock ──
# Set when admin confirms "Start Services" via the confirm endpoint.
# Lost on restart, so every restart shows the splash again.
_safety_session_unlocked = False


# ── Safety lock confirmation endpoint ──
@app.post("/api/v1/admin/safety-lock/confirm")
async def safety_lock_confirm(request: Request):
    """Admin confirms or declines to start services after a shutdown restart.
    Body: { "start_services": true } → unlock this session + run recovery
          { "start_services": false } → do nothing (lock stays)
    """
    global _safety_session_unlocked

    try:
        body = await request.json()
    except Exception:
        body = {}
    start_services = body.get("start_services", False) is True

    # Admin-only check
    from app.auth.identity import request_user_id
    from app.db import get_db
    caller_id = request_user_id(request)
    if not caller_id or not await get_db().is_user_admin(caller_id):
        from fastapi import HTTPException
        raise HTTPException(status_code=403, detail="Admin access required")

    if start_services:
        _safety_session_unlocked = True
        logger.info("Safety-lock: admin confirmed start — session unlocked")
        return {"ok": True, "services_started": True, "reload": True}
    else:
        logger.info("Safety-lock: admin kept services shut down — no recovery triggered")
        return {"ok": True, "services_started": False}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    """Front door. Serves the server-rendered marketing landing page (crawlable)
    to anyone who hasn't entered the app yet, plus to ALL search/social crawlers.

    SEO note — the crawlable landing is decoupled from the welcome toggle: a
    crawler always gets real, readable home content even when the human welcome
    experience (App Settings → Startup & Boot, stored as splash_enabled) is off,
    so the home page is never an empty JavaScript shell to a search engine.
    Humans: when the welcome is enabled, a NEW visitor (no `wa_seen_splash`
    cookie — set by the landing's Enter button / the Manage Account preference)
    sees the landing; when it's disabled, humans go straight to the app shell.
    The app also has a stable home at /app and /index.html that always bypass the
    landing."""
    try:
        from app.admin.settings import get_splash_enabled
        landing_on = get_splash_enabled()
    except Exception:
        landing_on = False
    not_entered = request.cookies.get("wa_seen_splash") != "1"
    # A crawler always gets the crawlable landing (independent of the toggle); a
    # human gets it only when the welcome is enabled and they haven't entered yet.
    if (not_entered and landing_on) or _is_crawler(request):
        page = _render_landing_page(request)
        if page is not None:
            return page
    return _render_app_shell(request)


@app.get("/app", response_class=HTMLResponse, include_in_schema=False)
async def app_shell_direct(request: Request):
    """The app's stable home — always the app shell, bypassing the landing front
    door. The installed PWA (manifest start_url) and the landing's "Enter app"
    button point here so they never bounce through the marketing page."""
    return _render_app_shell(request)


@app.get("/index.html", response_class=HTMLResponse, include_in_schema=False)
async def main_ui(request: Request):
    """Serve the main web UI directly (static assets remain under /ui/). Like
    /app, this always bypasses the landing front door."""
    return _render_app_shell(request)


@app.get("/setup.html", response_class=HTMLResponse, include_in_schema=False)
async def setup_ui():
    """Serve the admin setup page (first-run wizard)."""
    setup_html = _APP_DIR.parent / "ui" / "setup.html"
    if not setup_html.is_file():
        return HTMLResponse("<p>Missing setup.html</p>", status_code=404)
    return HTMLResponse(content=setup_html.read_text(encoding="utf-8"))


@app.get("/login.html", response_class=HTMLResponse, include_in_schema=False)
async def login_ui():
    """Serve the login page."""
    login_html = _APP_DIR.parent / "ui" / "login.html"
    if not login_html.is_file():
        return HTMLResponse("<p>Missing login.html</p>", status_code=404)
    return HTMLResponse(content=login_html.read_text(encoding="utf-8"))


def _render_static_seo_page(request: Request, rel_path: str, page_path: str,
                            title: str, desc: str) -> HTMLResponse:
    """Serve a static public HTML page (e.g. the legal pages) with the shared SEO
    head injected server-side. These files ship only a bare <title>, so on their
    own they're 'thin' to a crawler (no description / canonical / Open Graph); this
    adds the full block before </head> with absolute urls. 404s if the file is
    missing."""
    from html import escape as _esc
    f = _APP_DIR.parent / rel_path
    if not f.is_file():
        return HTMLResponse(f"<p>Missing {rel_path}</p>", status_code=404)
    html = f.read_text(encoding="utf-8")
    origin = _seo_origin(request)
    meta = _seo_head_block(origin, page_path, title, desc, noindex=False)
    # Make the route's SEO title the single source of truth: replace the file's
    # bare <title> so the visible title and the injected og:title can't diverge.
    html = re.sub(r"<title>.*?</title>", f"<title>{_esc(title, quote=True)}</title>",
                  html, count=1, flags=re.S | re.I)
    html = html.replace("</head>", meta + "</head>", 1)
    return HTMLResponse(content=html)


@app.get("/privacy", response_class=HTMLResponse, include_in_schema=False)
async def privacy_page(request: Request):
    """Serve the privacy policy page (public, no auth) with SEO head injected."""
    return _render_static_seo_page(
        request, "ui/privacy.html", "/privacy", "Privacy Policy — WebAgent",
        "How WebAgent handles your data — what is collected, how it is stored and "
        "used, and the choices and controls you have.")


@app.get("/tos", response_class=HTMLResponse, include_in_schema=False)
async def tos_page(request: Request):
    """Serve the terms of service page (public, no auth) with SEO head injected."""
    return _render_static_seo_page(
        request, "ui/tos.html", "/tos", "Terms of Service — WebAgent",
        "The terms for using WebAgent — acceptable use, your responsibilities, and "
        "the terms that govern the service.")


@app.get("/termux", include_in_schema=False)
@app.get("/termux.sh", include_in_schema=False)
async def termux_installer():
    """Serve the Termux one-line installer for the standalone WebAgent TUI.

    Enables `curl -fsSL https://webagent.live/termux | bash` to install the
    Server Manager TUI on Android. Served verbatim from
    `TUI/install-termux.sh`, with line endings forced to LF so a
    Windows checkout can never ship a CRLF script that bash refuses to run.
    Registered BEFORE the `/{agent_id}` catch-all below so it isn't shadowed."""
    from fastapi.responses import PlainTextResponse
    script = _APP_DIR.parent / "TUI" / "install-termux.sh"
    if not script.is_file():
        return PlainTextResponse("# WebAgent TUI installer not found\n", status_code=404)
    body = script.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    return PlainTextResponse(
        body,
        media_type="text/x-shellscript",  # Starlette appends "; charset=utf-8"
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/health")
async def health_check():
    # Liveness stays 200 while optional services are connecting: a restart would
    # only throw away the work already in progress. Clients receive the phase
    # and pending work so they can keep cached/read-only UI usable and honestly
    # show "connecting" instead of implying that every integration is ready.
    return {
        "status": "healthy",
        "initialization": getattr(app.state, "startup_phase", "starting"),
        "pending": list(getattr(app.state, "startup_pending", [])),
        "active_detail": getattr(app.state, "startup_active_detail", "") or None,
    }


@app.get("/health/ready", include_in_schema=False)
async def health_ready():
    """Shallow READINESS probe — deliberately deeper than the liveness ``/health``
    above, which returns 200 as long as the process is up and the event loop turns.

    A server can pass liveness while being unable to do real work: its database
    connection has wedged (remote Postgres dropped, pool exhausted, a sync
    round-trip stuck on the loop — see the chat hot-path latency notes). To the
    Server-Manager that server looks healthy forever and is never recovered. This
    endpoint does ONE cheap, indexed single-row read against whatever backend is
    live, bounded by a short timeout and run OFF the event loop, and returns **503**
    when it can't complete. The watchdog polls this and, on a *sustained* failure,
    restarts the wedged server. Based purely on the live round-trip: a degraded
    fallback to the local copy still reads fine → ready (a restart wouldn't fix a
    remote outage anyway, so we must not restart-loop on it)."""
    import asyncio
    from fastapi.responses import JSONResponse

    def _ping() -> None:
        # Cheapest cross-backend round-trip that still proves the live connection
        # answers. Runs in a worker thread (the DB backends are sync under async).
        from app.db.readiness import check_storage_authorities
        check_storage_authorities()

    try:
        await asyncio.wait_for(asyncio.to_thread(_ping), timeout=2.5)
    except Exception as e:  # timeout, connection error, driver error → not ready
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready", "check": "db",
                     "reason": f"{type(e).__name__}: {e}"[:200]},
        )
    return {"status": "ready"}


@app.get("/robots.txt", include_in_schema=False)
async def robots_txt(request: Request):
    """Crawler directives. Allows the public surface (home + legal pages + static
    assets so render-based crawlers can paint the page), keeps crawlers out of the
    API, auth, and dev/test endpoints, and points to the sitemap. Registered
    BEFORE the /{agent_id} catch-all so it isn't shadowed by it."""
    origin = _seo_origin(request)
    body = (
        "User-agent: *\n"
        "Disallow: /api/\n"
        "Disallow: /login.html\n"
        "Disallow: /setup.html\n"
        "Disallow: /test\n"
        "Disallow: /docs\n"
        "Disallow: /redoc\n"
        "Disallow: /web-terminal/\n"
        "Disallow: /user_data/\n"
        "Disallow: /screenshots/\n"
        "Disallow: /go/\n"
        f"\nSitemap: {origin}/sitemap.xml\n"
    )
    return PlainTextResponse(body, media_type="text/plain")


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap_xml(request: Request):
    """Sitemap of the publicly indexable pages: home + legal + the Wiki index +
    one entry per PUBLISHED wiki article (with a <lastmod> from updated_at). The
    article list is read live from data/wiki.db on each request, so newly
    published / unpublished articles appear / disappear with no rebuild. Public
    per-agent pages are intentionally omitted pending the indexing-policy
    decision. Registered BEFORE the /{agent_id} catch-all so it isn't shadowed."""
    from html import escape as _esc
    from datetime import datetime, timezone
    origin = _seo_origin(request)

    def _u(path, day=None):
        loc = _esc(origin + path, quote=True)
        if day:
            return f"  <url><loc>{loc}</loc><lastmod>{_esc(day, quote=True)}</lastmod></url>\n"
        return f"  <url><loc>{loc}</loc></url>\n"

    def _file_day(fp):
        # <lastmod> for a static page = its source file's modification date (UTC).
        try:
            return datetime.fromtimestamp(fp.stat().st_mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        except Exception:
            return None

    urls = (
        _u("/", _file_day(_SPLASH_DIR / "splash-page.html"))
        + _u("/privacy", _file_day(_APP_DIR.parent / "ui" / "privacy.html"))
        + _u("/tos", _file_day(_APP_DIR.parent / "ui" / "tos.html"))
    )
    try:
        from app.wiki.store import list_articles
        articles = await list_articles(include_drafts=False)
        # The Wiki index changes whenever any published article does, so stamp it
        # with the most recent article update.
        latest = max(((a.get("updated_at") or "")[:10] for a in articles), default="")
        urls += _u("/wiki", latest if len(latest) == 10 else None)
        for a in articles:
            slug = a.get("slug")
            if not slug:
                continue
            day = (a.get("updated_at") or "")[:10]
            urls += _u("/wiki/" + slug, day if len(day) == 10 else None)
    except Exception:
        urls += _u("/wiki")
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        f"{urls}"
        "</urlset>\n"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/wiki", response_class=HTMLResponse, include_in_schema=False)
async def wiki_public_index(request: Request):
    """Public, crawlable index of PUBLISHED wiki articles, server-rendered.

    Companion doorway to the in-app Wiki SPA: the SAME data (data/wiki.db) but
    delivered as real HTML so search engines (which don't run the SPA's JS) can
    read it. Drawn live on each request — no build step. Drafts never appear
    (anonymous/public read path). Registered BEFORE the /{agent_id} catch-all so
    the single-segment /wiki isn't swallowed by it; see app/wiki/public_pages.py."""
    from app.wiki.store import list_articles
    from app.wiki import public_pages
    origin = _seo_origin(request)
    try:
        articles = await list_articles(include_drafts=False)
    except Exception:
        articles = []
    title = "Wiki — WebAgent"
    desc = "Browse the WebAgent knowledge base — published guides, policies and reference."
    head = _seo_head_block(origin, "/wiki", title, desc, noindex=False)
    head += _jsonld_block(_breadcrumb_obj(origin, [("Home", "/"), ("Wiki", "/wiki")]))
    return HTMLResponse(public_pages.build_index_html(
        articles=articles, origin=origin, head_html=head, title=title))


@app.get("/wiki/{slug}", response_class=HTMLResponse, include_in_schema=False)
async def wiki_public_article(slug: str, request: Request):
    """Public, crawlable page for ONE published wiki article, server-rendered.

    Two path segments, so the single-segment /{agent_id} catch-all never matches
    it. Returns 404 for a missing slug OR a draft — the store hides a draft's very
    existence from public callers (include_drafts=False). Per-article title +
    description + canonical + Open Graph come from the shared _seo_head_block."""
    from app.wiki.store import get_article, get_backlinks, list_articles
    from app.wiki import public_pages
    origin = _seo_origin(request)
    article = await get_article(slug, include_drafts=False)
    if not article:
        head = _seo_head_block(
            origin, f"/wiki/{slug}", "Not found — WebAgent Wiki",
            "This wiki article doesn't exist or isn't public.", noindex=True)
        return HTMLResponse(
            public_pages.build_notfound_html(origin=origin, head_html=head),
            status_code=404)
    # Build a (title/slug → slug) index from the published set so [[wiki-links]]
    # in the body resolve to other PUBLIC pages (unknown targets render as text).
    try:
        published = await list_articles(include_drafts=False)
    except Exception:
        published = []
    link_index = {}
    for a in published:
        s = a.get("slug")
        if not s:
            continue
        link_index[s.lower()] = s
        t = a.get("title")
        if t:
            link_index[t.lower()] = s
    try:
        related = await get_backlinks(slug, include_drafts=False)
    except Exception:
        related = []
    page_title = f"{article.get('title') or 'Untitled'} — WebAgent Wiki"
    desc = public_pages.make_description(article.get("body") or "")
    # Prefer the article's own first image as the share preview; fall back to the
    # default 1200×630 card when it has none (or none that verifiably exists).
    og_image = _first_article_image(article.get("body") or "", origin)
    head = _seo_head_block(
        origin, f"/wiki/{slug}", page_title, desc, noindex=False, og_type="article",
        published=article.get("created_at"), modified=article.get("updated_at"),
        image=og_image)
    head += _wiki_article_jsonld(
        origin, slug, article.get("title") or "Untitled", desc,
        article.get("created_at"), article.get("updated_at"), image=og_image)
    return HTMLResponse(public_pages.build_article_html(
        article=article, related=related, link_index=link_index,
        origin=origin, head_html=head, title=page_title))


import re as _re
# Embed URLs remain UUID-only, but public links use the persisted agent ID.
# Built-in agents such as `shared_default` are valid public-link targets too.
_AGENT_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    _re.IGNORECASE,
)
_PUBLIC_AGENT_ID_RE = _re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")

_EMBED_DIR = _APP_DIR.parent / "ui" / "embed"


def _render_embed_chat_document(agent_id: str, agent: dict, pub_cfg: dict) -> str:
    """Render the small, profile-aware iframe client for one public agent."""
    import json
    from html import escape as _esc

    template = _EMBED_DIR / "embed-chat.html"
    if not template.is_file():
        return ""
    document = template.read_text(encoding="utf-8")
    title = pub_cfg.get("title") or agent.get("name") or "Chat"
    boot = {
        "agentId": agent_id,
        "agentName": agent.get("name") or "Agent",
        "enabled": bool(pub_cfg.get("enabled")),
        "embeddable": True,
        "config": pub_cfg,
    }
    payload = json.dumps(boot, separators=(",", ":")).replace("<", "\\u003c")
    document = document.replace("__EMBED_TITLE__", _esc(str(title), quote=True), 1)
    return document.replace(
        '<script type="module" src="/ui/embed/embed-chat.js"></script>',
        f"<script>window.__EMBED__={payload};</script>\n"
        '<script type="module" src="/ui/embed/embed-chat.js"></script>',
        1,
    )


@app.get("/embed.js", include_in_schema=False)
async def embed_loader_js():
    """Serve the website-embed loader script.

    The one asset a customer references from their own site
    (`<script src=".../embed.js" data-agent="…">`). It injects a floating
    launcher + an iframe pointing back at /embed/<agent_id>. Kept at this short
    top-level path (not /ui/embed/embed.js) so the copy-paste snippet is tidy.
    Registered BEFORE the /{agent_id} catch-all so "embed.js" isn't swallowed by
    it."""
    f = _EMBED_DIR / "embed.js"
    if not f.is_file():
        return HTMLResponse("// embed loader missing", status_code=404,
                            media_type="application/javascript")
    return FileResponse(
        str(f),
        media_type="application/javascript",
        headers={"Cache-Control": "public, max-age=300",
                 "X-Content-Type-Options": "nosniff"},
    )


@app.get("/embed/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
async def embed_chat_page(agent_id: str, request: Request):
    """Serve the standalone profile-aware chat panel for an agent.

    The customer's iframe boots the small embed client rather than the complete
    application shell. It is guarded per-agent by a
    Content-Security-Policy `frame-ancestors`
    directive built from the owner's allowed-domains list, so an owner who locks
    the widget to their domain can't have it re-hosted elsewhere. When no domains
    are configured the default is open (`*`) — embed anywhere — matching the
    product promise. The public display config is injected for widget-only
    title, icon, accent, and launcher details."""
    if not _AGENT_UUID_RE.match(agent_id):
        return HTMLResponse("<p>Not found</p>", status_code=404)
    from app.db import get_db as _get_db
    from app.api.embed_config import (
        read_embed_config, public_embed_config, embed_frame_ancestors,
    )
    from html import escape as _esc
    db = _get_db()
    agent = await db.get_agent_by_id(agent_id)
    if not agent:
        return HTMLResponse("<p>Agent not found</p>", status_code=404)

    raw_cfg = read_embed_config(agent)
    pub_cfg = public_embed_config(agent)
    anon_ok = (agent.get("user_mode") or "anonymous") == "anonymous"
    embeddable = bool(pub_cfg.get("enabled")) and anon_ok

    # Per-agent framing policy. frame-ancestors is the modern, allowlist-capable
    # replacement for X-Frame-Options — so we set ONLY it (X-Frame-Options can't
    # express a multi-domain allowlist and would just conflict).
    csp = f"frame-ancestors {embed_frame_ancestors(raw_cfg)}"
    if not embeddable:
        reason = (
            "This chat widget has not been enabled by its owner."
            if not pub_cfg.get("enabled")
            else "This agent is not open to anonymous visitors."
        )
        return _secure_html_response(
            f"<p>{_esc(reason)}</p>",
            frame_ancestors=embed_frame_ancestors(raw_cfg),
        )

    document = _render_embed_chat_document(agent_id, agent, pub_cfg)
    if not document:
        return HTMLResponse("<p>Embed client missing</p>", status_code=500)
    return _secure_html_response(
        document,
        frame_ancestors=embed_frame_ancestors(raw_cfg),
    )



@app.get("/{agent_id}", response_class=HTMLResponse, include_in_schema=False)
async def public_agent_chat(agent_id: str, request: Request):
    """Serve the main UI with a specific agent pre-selected (public access).

    Renders through `_render_app_shell` so the shared link also gets a per-agent
    title + Open Graph / Twitter preview (in addition to the __agentId /
    __agentName bootstrap the app reads). Only accessible when the agent's
    `public_link` flag is enabled."""
    if not _PUBLIC_AGENT_ID_RE.fullmatch(agent_id):
        return _render_app_shell(
            request,
            route_error={"status": 404, "code": "public_agent_not_found"},
        )
    from app.db import get_db as _get_db
    db = _get_db()
    agent = await db.get_agent_by_id(agent_id)
    # The shared default is provisioned lazily as the app-wide singleton. Resolve
    # it here too so its public link works before a roster request seeds it.
    if not agent and agent_id == "shared_default":
        from app.api.agents import provision_default_agent
        agent = await provision_default_agent(db, "admin")
    if not agent:
        return _render_app_shell(
            request,
            route_error={"status": 404, "code": "public_agent_not_found"},
        )
    # Check the public-link flag — gate the public route. Falls back to the
    # legacy is_embeddable key so previously-enabled agents keep working.
    meta = agent.get("metadata") or {}
    if isinstance(meta, str):
        import json as _json
        try:
            meta = _json.loads(meta)
        except Exception:
            meta = {}
    if not meta.get("public_link", meta.get("is_embeddable")):
        return _render_app_shell(
            request,
            route_error={"status": 404, "code": "public_agent_not_found"},
        )
    return _render_app_shell(request, agent_id=agent_id, agent_name=agent.get("name", "Agent"))

@app.get("/test", response_class=HTMLResponse)
async def test_interface():
    """Serve the test interface HTML page."""
    test_html = _APP_DIR.parent / "ui" / "test_interface.html"
    return HTMLResponse(content=test_html.read_text(encoding="utf-8"))


class _SafetyLockGate(Exception):
    """Raised during startup to skip background services when the safety lock
    is active. Caught by the leader-startup handler below. The server stays
    up to serve the UI and the safety splash modal."""


# ── Start-up: register Telegram webhook ──
@app.on_event("startup")
async def startup():
    """Register communication webhooks or start polling on server start."""
    # Startup and the tasks it spawns carry an explicit bootstrap authority;
    # user-plane routing no longer obtains admin data from an ambient default.
    from app.auth.workspace import set_workspace, system_user_context
    from app.db.local import set_db_user_context
    set_workspace(system_user_context("admin", "application startup services"))
    set_db_user_context("admin")
    # Lifespan can be re-entered by test clients and reloaders. Never retain
    # deferred closures from a prior boot.
    app.state.startup_phase = "starting"
    app.state.startup_pending = []
    app.state.startup_deferred_queue = []
    app.state.startup_deferred_task = None
    app.state.startup_active_detail = ""
    if performance_test_mode():
        # Performance tests profile the HTTP/bootstrap path in isolation. They
        # must never join leader election, start pollers, touch live vaults, or
        # run migrations against the operator's data.
        app.state.startup_phase = "ready"
        logger.warning(
            "PERFORMANCE TEST MODE: isolated data=%s; background services disabled",
            data_root(),
        )
        return
    # ── Safety lock: set the persistent flag on EVERY boot ──
    # This ensures the lock is always active, even after a crash where the
    # shutdown handler never ran (e.g. taskkill /F). The in-memory session
    # unlock (set by the confirm endpoint) allows normal boot on the same
    # process without clearing the disk flag.
    try:
        from app.admin.settings import set_safety_lock_active
        await asyncio.to_thread(set_safety_lock_active)
    except Exception:
        pass
    # ── Kill switch: read the persisted state so the background-service gates
    # below honour an engaged switch across a restart. Must run before any
    # resume / polling / watchdog code path.
    try:
        from app import kill_switch as _kill_switch
        await asyncio.to_thread(_kill_switch.init)
    except Exception as _ks_init_err:
        logger.warning("Kill switch init failed: %s", _ks_init_err)
    # ── Full-DB encryption reconcile (MUST be first) ──
    # Before ANY store opens its SQLite files, bring each database file into line
    # with the configured at-rest encryption state (encrypt newly-enabled files,
    # decrypt newly-disabled ones) — backup-first, atomic, no plaintext residue.
    # Always runs: it's near-zero cost on plaintext files (reads 16 bytes per DB)
    # and catches the case where encryption was toggled OFF but the file is still
    # encrypted on disk from a previous run. Runs here so the diagnostics recorder
    # + every backend below open already-correct files.
    try:
        from app.db import db_crypto
        _enc_actions = await asyncio.to_thread(db_crypto.reconcile)
        _changed = [a for a in _enc_actions if a.get("action") in ("encrypted", "decrypted")]
        if _changed:
            logger.info("Full-DB encryption reconcile: %s", _changed)
        _failed = [a for a in _enc_actions if not a.get("ok")]
        if _failed:
            logger.warning("Full-DB encryption reconcile had failures: %s", _failed)
    except Exception as _enc_err:
        logger.warning("Full-DB encryption reconcile failed: %s", _enc_err)

    # Start the diagnostic flight-recorder's background batch-writer + pruner.
    try:
        from app.agent.diagnostics import get_recorder, record_run_lifecycle  # noqa: F401
        # Recorder construction reads settings/drop-in metadata and its first
        # durable flush initializes logs.db. Resolve the singleton off-loop so
        # bootstrap /health and /app remain responsive on a cold disk.
        _diag_recorder = await asyncio.to_thread(get_recorder)
        _diag_recorder.start()
        _diag_recorder.record("info", "server", "Server starting up", source="startup")
    except Exception as _diag_err:
        logger.warning("Diagnostic recorder failed to start: %s", _diag_err)

    # RECORDER-HOOK: plugins/recorder owns capture writers, started in each
    # worker by deferred application-plugin discovery below.

    # Warm the memory embedding client in the background so the FIRST chat turn's
    # memory_search doesn't pay the cold-start penalty (~7.5s cold vs ~0.3s warm).
    # Fire-and-forget — never blocks boot, and is a no-op if no provider key is set.
    try:
        import asyncio as _asyncio
        from app.agent.embed import warm_embed_client
        _asyncio.create_task(warm_embed_client())
    except Exception as _embed_warm_err:
        logger.warning("Embed warmup scheduling failed: %s", _embed_warm_err)

    # Terminal support is not part of the first interactive experience. Import
    # and start its idle-session GC after readiness with the terminal API.
    async def _start_terminal_gc():
        from app.api.terminal import start_idle_gc
        start_idle_gc()
    _queue_deferred_startup("terminal_idle_gc", _start_terminal_gc)

    # First-boot: provision from a bootstrap.json dropped next to the clone (an
    # encrypted setup bundle → DB + vault + LLM + admin). Runs BEFORE the security
    # and LLM-from-env seeds below so the bundle's choices win; a strict freshness
    # gate (no LLM configured yet) makes it a no-op on any real install, and the
    # file is renamed once applied so it never re-fires. See
    # app/admin/bootstrap_bundle.py.
    try:
        from app.admin.bootstrap_bundle import apply_boot_file
        _boot_res = await apply_boot_file()
        if _boot_res:
            logger.info("Bootstrap file provisioned fresh install: %s", _boot_res.get("results"))
    except Exception as _boot_err:
        logger.warning("Bootstrap-file provisioning at startup failed: %s", _boot_err)

    # Optional APIs used to be imported while ``app.main`` was loading. Mount
    # them after the interactive core (auth/chat/sessions/Agents/Wiki/catalog)
    # is serving. Progress remains visible through /health.
    _queue_deferred_startup("optional_routes", _mount_optional_routers)
    _queue_deferred_startup("page_backend_discovery", _mount_dropin_page_backends)
    _queue_deferred_startup("ability_backend_discovery", _mount_dropin_ability_backends)
    # RECORDER-HOOK: standalone plugins mount after readiness, independently of
    # agent abilities. Remove this block with app/application_plugins.py.
    async def _mount_application_plugins():
        await _application_plugins.mount(app)
        await _application_plugins.start()  # RECORDER-HOOK: every worker observes its own events.
    _queue_deferred_startup("application_plugin_discovery", _mount_application_plugins)


    # Seed agent templates from JSON after readiness. This is manifest-gated and
    # idempotent; existing installs already have a usable template catalog, and
    # a fresh install retains the safe shell until this completes.
    async def _seed_agent_templates():
        from app.db import get_db as _get_db_seed
        _seed_db = _get_db_seed()
        _seed_summary = await _seed_db.seed_agent_templates(force=False)
        if _seed_summary.get("cached"):
            logger.info("Agent template seed: cached (manifest hash unchanged)")
        else:
            logger.info(
                "Agent template seed: changed=%s skipped_admin=%s templates=%s",
                _seed_summary.get("changed"),
                _seed_summary.get("skipped_admin"),
                _seed_summary.get("templates"),
            )
    _queue_deferred_startup("agent_templates", _seed_agent_templates)

    # Account migration and environment-admin repair are idempotent background
    # maintenance. Normal account endpoints remain the authority; a visitor can
    # use the cached shell while this catches an old install up.
    async def _migrate_accounts_and_bootstrap_admin():
        from app.auth import users as _auth_users
        _migrated = await _auth_users.migrate_legacy_file()
        if _migrated:
            logger.info("Account store: migrated %d account(s) from users.json → DB", _migrated)
        await _auth_users.bootstrap_admin_from_env()
        # Self-heal the admin PRIVILEGE (user_profiles.is_admin), which the
        # SQLite-only seed misses on a shared/remote control DB — so a fresh
        # deploy that inherits an existing shared database still unlocks Admin
        # Tools. Runs AFTER apply_boot_file activated that shared DB above.
        if await _auth_users.ensure_admin_privilege():
            logger.info("Account store: ensured bootstrap admin has is_admin=1 in the active control DB")
    _queue_deferred_startup("accounts_and_admin_repair", _migrate_accounts_and_bootstrap_admin)

    # The process already has its locally cached signing key at import time.
    # Sharing/reconciling it with the vault can happen after the app is serving.
    async def _reconcile_shared_jwt_secret():
        from app.auth import jwt as _auth_jwt
        await _auth_jwt.ensure_shared_jwt_secret()
    _queue_deferred_startup("jwt_secret_reconcile", _reconcile_shared_jwt_secret)

    # First-boot ability/security defaults are idempotent. They are not a
    # prerequisite for serving cached sessions or the public Wiki.
    async def _seed_default_abilities():
        from app.admin.integrations import seed_default_abilities
        _ab_seed = await seed_default_abilities()
        if _ab_seed.get("seeded"):
            logger.info("Default admin abilities seeded ON: %s", _ab_seed.get("enabled"))
    _queue_deferred_startup("default_abilities", _seed_default_abilities)

    # First-boot: on a FRESH, keyring-capable install, turn on the OS-keyring
    # secrets vault + per-tenant field encryption by default. Strictly guarded
    # (no-op if anything's already configured, no keyring, or secrets already
    # exist) so it never disturbs an existing install. See
    # app/encryption/defaults.py for the full guard list.
    async def _seed_security_defaults():
        from app.encryption.defaults import seed_security_defaults
        _sec_seed = await seed_security_defaults()
        if _sec_seed.get("seeded"):
            logger.info("Security defaults seeded ON (os_keyring + field encryption)")
        else:
            logger.info("Security defaults not seeded: %s", _sec_seed.get("reason"))
    _queue_deferred_startup("security_defaults", _seed_security_defaults)

    # Trigger routes are rebuilt after the template seed so the first request
    # never pays an entire schema/index scan. Requests without a trigger remain
    # fully usable during this short connecting period.
    async def _build_trigger_index():
        from app.agent import trigger_index
        await asyncio.to_thread(trigger_index.build)
    _queue_deferred_startup("trigger_index", _build_trigger_index)

    # ── Self-healing recovery (step 1 of 2): mark mid-flight runs as resumable ──
    # A server restart is the one thing that ends an in-flight run. On boot we
    # flip any 'running' session_runs to 'interrupted' WITH stop_cause=
    # 'server_restart' (and streaming assistant rows to 'interrupted'). That tags
    # them as resume candidates; the actual re-ignition happens in step 2 below,
    # after the scheduler/event runtimes are up. See app/agent/runner.py.
    async def _mark_orphans_for_resume():
        from app.db import get_db as _get_db_orphan
        _orphan_db = _get_db_orphan()
        _n_orphan = await _orphan_db.mark_orphans_for_resume()
        if _n_orphan:
            logger.info("Marked %d orphaned agent run(s) for resume (left by previous process)", _n_orphan)
        from app.agent.subagent_contracts import recover_orphaned_contract_checks
        _n_contracts = await recover_orphaned_contract_checks(_orphan_db)
        if _n_contracts:
            logger.info("Finalized %d fenced orphan contract check(s)", _n_contracts)
    _queue_deferred_startup("orphaned_run_recovery", _mark_orphans_for_resume)

    async def _start_communications():
        from app.communications.manager import get_plugin_manager
        from app.communications import registry
        pm = get_plugin_manager()

        # Detected URL from communications registry
        base_url = registry.get("webhook_base_url", "").rstrip("/")
        _local_hints = ("localhost", "127.0.0.1", "0.0.0.0")
        is_public = bool(base_url) and not any(h in base_url for h in _local_hints)

        if is_public:
            for plugin in pm.get_enabled_plugins():
                if hasattr(plugin, "set_webhook_url"):
                    await plugin.set_webhook_url(base_url)
                    logger.info("Registered webhook for %s at %s", plugin.name, base_url)
        else:
            # No reachable public URL — start polling (local dev only).
            await pm.start_polling_for_offline_plugins()
    _queue_deferred_startup("communications", _start_communications)

    # ── Backfill admin_users for existing agents ──
    async def _backfill_agent_admin_users():
        from app.db import get_db as _get_db_backfill
        _db_bf = _get_db_backfill()
        _n = await _db_bf.backfill_agent_admin_users()
        if _n:
            logger.info("Backfilled admin_users for %d agents", _n)
    _queue_deferred_startup("agent_admin_backfill", _backfill_agent_admin_users)

    # ── Legacy collapsed-activity projection repair ───────────────────────
    async def _backfill_activity_summaries():
        from app.db.session_activity import backfill_legacy_activity_summaries
        repaired = await asyncio.to_thread(backfill_legacy_activity_summaries, data_root() / "user_data")
        if repaired:
            logger.info("Backfilled activity summaries for %d legacy session(s)", repaired)
    _queue_deferred_startup("activity_summary_backfill", _backfill_activity_summaries)

    # ── Shared default agent (app-level single row, admin-owned) ──────────────
    # When shared_default_agent_enabled is on, ensure ONE shared agent row exists
    # (id="shared_default") owned by the app admin. Every user sees this agent in
    # their roster instead of getting a private clone. Idempotent — skips if the
    # row already exists.
    async def _provision_shared_default_agent():
        from app.admin.settings import shared_default_agent_enabled as _sd_on
        if _sd_on():
            from app.api.agents import provision_default_agent as _ensure_shared_default
            from app.db import get_db as _get_db_shared_default
            _sd_agent = await _ensure_shared_default(_get_db_shared_default(), "admin")
            logger.info("Shared default agent ready: %s", _sd_agent.get("id"))
    _queue_deferred_startup("shared_default_agent", _provision_shared_default_agent)

    # ── App Functions gate — admin on/off for the wired-in background services ──
    # Each singleton below (sync engine, scheduler, event runtime, watchdog,
    # boot-orphan-resume, remote access, device worker) has a matching drop-in
    # descriptor under plugins/abilities/System/ carrying "app_function": true, so
    # it appears as a toggle in App Settings ▸ App Functions. Its start site is
    # gated on ``app_function_enabled(<id>)`` — the same app-level toggle store
    # (data/config/agent-abilities.json). Failing ON: if the catalog/config can't
    # be read the service still starts (a default-on service is never silently
    # suppressed). The toggle takes effect on the next restart (these all wire up
    # once at boot), matching how the panel's description reads.
    def _appfn_on(_ability_id: str) -> bool:
        try:
            from app import abilities as _abilities_gate
            return _abilities_gate.app_function_enabled(_ability_id)
        except Exception:
            return True

    # ── Hybrid local-first sync engine (Stage 2) ──
    # When the hybrid backend is active (SQLite hot store in front of the Postgres
    # authority), interaction writes land locally first and are queued in a local
    # OUTBOX. Start the background engine that drains that outbox to the remote as
    # a stripped skeleton within a few seconds. Stage 4 also turns the remote→local
    # PULLER on (pull_enabled=True) so shared content/config edits made on another
    # device land in this machine's local replica continuously. This is safe: the
    # Stage-4 security audit confirmed no authorization/identity/billing decision
    # resolves from the local cache — the agent access-mode selector is read from
    # the authority (chat._enforce_agent_access_policy), billing/roles/admin/tier
    # all resolve remote, and the puller only warms Synced-tier rows (it never
    # pulls money/identity tables). No-op unless the active db is a HybridBackend.
    # The engine binds to THIS machine's local outbox, so it runs per-process (not
    # via the cluster leader, which would wrongly elect a single device for everyone).
    async def _start_hybrid_sync_engine():
        from app.db import get_db as _get_db_sync
        from app.db.hybrid import hybrid_enabled as _hybrid_on, HybridBackend
        from app.kill_switch import is_engaged as _ks_engaged
        _sync_db = _get_db_sync()
        # Reach the HybridBackend — it's either the active backend directly, or
        # wrapped by the encryption decorator (composition is Enc(Hybrid(...))).
        _inner = _sync_db
        if not isinstance(_inner, HybridBackend):
            _inner = getattr(_sync_db, "_inner", None)
        if _hybrid_on() and isinstance(_inner, HybridBackend) and _appfn_on("hybrid_sync") and not _ks_engaged():
            from app.db.sync import SyncEngine
            _engine = SyncEngine(_inner, pull_enabled=True)
            _engine.start()
            app.state.hybrid_sync_engine = _engine
            logger.info("Hybrid sync engine started (push + pull, syncing local replica <-> remote)")
            # One-shot back-fill: push agents that exist only in this device's local
            # mirror (created before it joined the shared DB, or during a Postgres
            # outage) up to the authority, so they stop being invisible to other
            # devices. Enqueues into the outbox the engine just started draining.
            async def _reconcile_hybrid_agents():
                _recon = await _inner.reconcile_local_only_agents()
                if _recon.get("pushed") or _recon.get("skipped_dup_default"):
                    logger.info("Hybrid startup agent reconcile: %s", _recon)
            # Keep reconciliation serial with the startup queue instead of
            # competing with the first foreground request.
            await _reconcile_hybrid_agents()
        elif _hybrid_on() and isinstance(_inner, HybridBackend):
            logger.info("Hybrid sync engine disabled via App Functions (hybrid_sync) — skipping")
    _queue_deferred_startup("hybrid_sync", _start_hybrid_sync_engine)

    # ── Singleton background services, gated by a single-worker leader ──
    # The scheduler, event runtime, ability pollers, boot orphan-resume, watchdog
    # and Remote Access must run in EXACTLY ONE worker. Under gunicorn --workers N
    # (or multiple instances) running them in every worker double-fires automations
    # and re-ignites the same orphaned runs N times. The leader elects one worker
    # via a TTL'd DB lock and runs them only there; if it dies another worker takes
    # over. Single-process dev/prod wins leadership instantly, so behaviour is
    # unchanged. Services run in registration order when leadership is acquired.
    #
    # ⚠ DROP-IN POLICY — do NOT add a per-ability startup line here. If a drop-in
    # ability needs a long-lived service, define start_background()/stop_background()
    # in its plugin file (plugins/abilities/<id>.py); the "ability-background"
    # leader service below discovers and runs it. See CLAUDE.md "Core vs. plugins".
    try:
        # ── Safety lock gate ──
        # Gate on ALL three: master switch enabled, persistent lock active,
        # and no in-memory session unlock. The session unlock is set by the
        # confirm endpoint and lost on restart.
        from app.admin.settings import get_safety_lock_enabled, get_safety_lock_active
        if get_safety_lock_enabled() and get_safety_lock_active() and not _safety_session_unlocked:
            logger.info(
                "Safety lock active — background services (scheduler, events, "
                "orphan-resume, watchdog, remote-access) are SUPPRESSED. "
                "Admin must confirm via the safety splash modal."
            )
            raise _SafetyLockGate()

        from app.coordination.leader import get_leader
        _leader = get_leader()

        async def _start_scheduler():
            from app.scheduler import start_scheduler
            await start_scheduler()

        async def _stop_scheduler():
            from app.scheduler import stop_scheduler
            await stop_scheduler()

        if _appfn_on("scheduler"):
            _leader.register("scheduler", _start_scheduler, _stop_scheduler)
        else:
            logger.info("Scheduler disabled via App Functions (scheduler) — not registered")

        async def _start_events():
            from app.events import start_event_runtime
            await start_event_runtime()

        async def _stop_events():
            from app.events import stop_event_runtime
            await stop_event_runtime()

        if _appfn_on("event_runtime"):
            _leader.register("event-runtime", _start_events, _stop_events)
        else:
            logger.info("Event runtime disabled via App Functions (event_runtime) — not registered")

        async def _start_ability_bg():
            from app import abilities as _abilities_mgr
            for _svc in _abilities_mgr.background_service_hooks():
                try:
                    await _svc["start"]()
                except Exception as _svc_err:
                    logger.warning("Ability '%s' background start failed: %s", _svc["id"], _svc_err)

        async def _stop_ability_bg():
            from app import abilities as _abilities_mgr
            for _svc in _abilities_mgr.background_service_hooks():
                if callable(_svc.get("stop")):
                    try:
                        await _svc["stop"]()
                    except Exception:
                        pass

        _leader.register("ability-background", _start_ability_bg, _stop_ability_bg)
        # Application-plugin observers are per-worker and start with deferred
        # discovery above, not under elected-leader-only services.


        # Self-healing (step 2 of 2): re-ignite resumable orphans. One-shot, run
        # AFTER scheduler/events are up (a resumed turn may use their machinery).
        # Backend-driven — recovers background/sub-agent/delegated runs too. Only
        # involuntary stops with a resumable cause + retry budget are relaunched.
        async def _resume_orphans():
            from app.agent.runner import resume_orphans_at_boot
            _n_resumed = await resume_orphans_at_boot()
            if _n_resumed:
                logger.info("Self-healing: re-ignited %d orphaned run(s) at boot", _n_resumed)

        if _appfn_on("boot_orphan_resume"):
            _leader.register("boot-orphan-resume", _resume_orphans, None, oneshot=True)
        else:
            logger.info("Boot orphan-resume disabled via App Functions (boot_orphan_resume) — not registered")

        async def _start_watchdog():
            from app.agent.watchdog import start_watchdog
            await start_watchdog()

        async def _stop_watchdog():
            from app.agent.watchdog import stop_watchdog
            await stop_watchdog()

        if _appfn_on("watchdog"):
            _leader.register("watchdog", _start_watchdog, _stop_watchdog)
        else:
            logger.info("Run watchdog disabled via App Functions (watchdog) — not registered")

        # Run Scout recovery is independent of main-run recovery: the main
        # response may have completed just before a process restart while its
        # parallel intake analysis had not.  This bounded sweep revives only
        # unfinished Scout revisions and never revives a user-stopped starter.
        async def _start_run_scout_sweep():
            from app.agent.run_scout import start_sweep
            await start_sweep()

        async def _stop_run_scout_sweep():
            from app.agent.run_scout import stop_sweep
            await stop_sweep()

        if _appfn_on("run_manager"):
            _leader.register("run-scout-sweep",
                             _start_run_scout_sweep, _stop_run_scout_sweep)
        else:
            logger.info("Run Scout sweep disabled via App Functions (run_manager) — not registered")

        # Session Namer recovery sweep — the watchdog-analog for the auto-titler:
        # periodically re-triggers naming for sessions still stuck on a fallback
        # "New: …" title (their original turn-hook attempt failed or never ran).
        # Bounded and cooldown-aware (see session_titler.py); runs only while the
        # Session Namer app function itself is enabled.
        async def _start_session_namer_sweep():
            from plugins.app_functions.session_titler.session_titler import start_sweep
            await start_sweep()

        async def _stop_session_namer_sweep():
            from plugins.app_functions.session_titler.session_titler import stop_sweep
            await stop_sweep()

        if _appfn_on("session_titler"):
            _leader.register("session-namer-sweep",
                             _start_session_namer_sweep, _stop_session_namer_sweep)
        else:
            logger.info("Session Namer sweep disabled via App Functions (session_titler) — not registered")

        # Output Closer recovery sweep — the watchdog-analog for the
        # close-out loop: periodically re-fires the close-out for final
        # assistant responses that never got one (their live hook failed,
        # crashed, or never ran). Bounded and cooldown-aware (see
        # output_closer.py); runs only while the Output Closer app
        # function itself is enabled.
        async def _start_closer_sweep():
            from app.agent.output_closer import start_sweep
            await start_sweep()

        async def _stop_closer_sweep():
            from app.agent.output_closer import stop_sweep
            await stop_sweep()

        # The Closing Message gate decides eligibility per recovered row, so
        # the sweep is always registered and fails closed when that gate is off.
        _leader.register("closer-sweep",
                         _start_closer_sweep, _stop_closer_sweep)

        async def _start_remote():
            from app.remote_access import start_remote_access
            await start_remote_access()

        async def _stop_remote():
            from app.remote_access import stop_remote_access
            await stop_remote_access()

        if _appfn_on("remote_access"):
            _leader.register("remote-access", _start_remote, _stop_remote)
        else:
            logger.info("Remote access disabled via App Functions (remote_access) — not registered")

        # Kill switch: services are REGISTERED (so the header toggle can re-enable
        # them later without a restart) but NOT started while engaged.
        from app.kill_switch import is_engaged as _ks_engaged
        if _ks_engaged():
            logger.info(
                "Kill switch engaged — leader services registered but NOT started"
            )
        else:
            # Start after orphan marking in the same bounded deferred queue, so
            # boot recovery cannot race a leader's one-shot resume service.
            async def _start_leader_services():
                await _leader.start()
            _queue_deferred_startup("leader_services", _start_leader_services)
    except _SafetyLockGate:
        pass  # Safety lock active — intentional
    except Exception as _leader_err:
        logger.warning("Failed to start background leader: %s", _leader_err)

    # ── Device worker — runs on EVERY instance (NOT leader-gated) ──
    # Multi-device: when several instances share one database, each runs its own
    # worker that heartbeats presence and claims only the dispatch jobs addressed
    # to its own device id. Deliberately outside the leader block above (those are
    # GLOBAL singletons elected to one worker; this one is per-device by design).
    # See app/devices/. Harmless on a single, unshared instance — there are simply
    # no other devices, so it just heartbeats itself.
    async def _start_device_worker_after_ready():
        from app.kill_switch import is_engaged as _ks_engaged
        if _appfn_on("device_worker") and not _ks_engaged():
            from app.devices import start_device_worker
            await start_device_worker()
        elif _ks_engaged():
            logger.info("Kill switch engaged — device worker not started")
        else:
            logger.info("Multi-device worker disabled via App Functions (device_worker) — skipping")
    _queue_deferred_startup("device_worker", _start_device_worker_after_ready)

    # ── P2P Mirror worker — runs on every instance ──
    async def _start_p2p_worker_after_ready():
        from app.kill_switch import is_engaged as _ks_engaged
        if _appfn_on("p2p") and not _ks_engaged():
            from app.p2p.worker import start_worker as start_p2p_worker
            await start_p2p_worker()
        elif _ks_engaged():
            logger.info("Kill switch engaged — P2P worker not started")
        else:
            logger.info("P2P mirror worker disabled via App Functions (p2p) — skipping")
    _queue_deferred_startup("p2p_worker", _start_p2p_worker_after_ready)

    # ── Provision named platform rosters (cloud-first + legacy migration) ──
    async def _provision_platform_rosters():
        api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY", "")
        from app.db import get_db as _get_db_llm_seed
        from app.entitlements.rosters import provision_system_rosters
        from app.entitlements.tiers import provision_system_tiers

        db = _get_db_llm_seed()
        env_config = None
        if api_key:
            env_config = {
                "provider": os.environ.get("LLM_PROVIDER", ""),
                "base_url": os.environ.get("LLM_BASE_URL", ""),
                "api_key": api_key,
                "model": os.environ.get("LLM_MODEL", ""),
                "providers": {},
                "multi_providers": [],
            }
        roster_seed = await provision_system_rosters(db=db, env_config=env_config)
        if roster_seed["created"] or roster_seed["migrated"]:
            logger.info(
                "Platform model rosters provisioned: created=%d migrated=%d",
                roster_seed["created"], roster_seed["migrated"],
            )
        tier_seed = await provision_system_tiers(db=db)
        if tier_seed["created"]:
            logger.info(
                "Experience tiers provisioned: created=%d skipped=%d",
                tier_seed["created"], tier_seed["skipped"],
            )

        # Preserve the old admin row during the compatibility window so older
        # workers and the existing Models UI continue to operate. New runtime
        # resolution prefers the named roster seeded above.
        if env_config:
            existing = await db.auth_element_get("admin", "llm", "default")
            if not (existing and existing.get("secret_ref")):
                from app.admin.settings import _persist_llm_config
                await _persist_llm_config("admin", env_config)
                logger.info("Seeded legacy admin LLM compatibility row from environment")
    _queue_deferred_startup("platform_rosters", _provision_platform_rosters)

    # ── Scrub any plaintext LLM key (old config-blob copies) into
    #    the encrypted vault. Idempotent; no-op once nothing plaintext remains. ──
    async def _migrate_llm_secrets():
        from app.admin.settings import migrate_llm_secrets
        await migrate_llm_secrets()
    _queue_deferred_startup("llm_secret_migration", _migrate_llm_secrets)

    # ── Prime the shared Git-page GitHub token from the encrypted vault (and
    #    migrate a legacy plaintext provider.json token into it once), so every
    #    device signed into the same vault resolves the same key and the agent's
    #    Git Control ability has it before any UI request runs. ──
    async def _prime_github_token():
        from app.api.github import _prime_shared_token_from_vault
        await _prime_shared_token_from_vault()
    _queue_deferred_startup("github_token_prime", _prime_github_token)

    # Core persistence/authentication are now ready. The queue begins only after
    # FastAPI finishes this lifespan hook, letting /app render its cached shell
    # and the client represent the remaining work as "connecting".
    app.state.startup_phase = "connecting"
    app.state.startup_pending = [
        label for label, _operation in app.state.startup_deferred_queue
    ]
    app.state.startup_deferred_task = asyncio.create_task(
        _drain_deferred_startup(), name="deferred_startup"
    )


if __name__ == "__main__":
    import uvicorn

    _port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(
        "app.bootstrap_asgi:bootstrap_app",
        host="0.0.0.0",
        port=_port,
        reload=True,
        access_log=False,
    )

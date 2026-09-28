"""
Persistent browser manager for Playwright-based automation.

Maintains one browser instance per user+session so the agent can
navigate, click, type, scrape, and screenshot across multiple turns.

By default the browser is headless (invisible). If a visible app-mode Chromium
is running with a remote-debugging port open — e.g. the launcher's "App Window"
button (see launcher/webagent_launcher/app_window.py) — the manager ATTACHES to
that window over CDP instead, so the agent drives the same window the user is
watching. Close the window and it transparently reverts to headless.
"""

# Defer annotation evaluation so the playwright type names (Browser /
# BrowserContext / Page) used in the annotations below don't force an import at
# module load. Playwright is a heavy import (lots of files to scan on a cold
# disk) and is only needed when the browser tools actually run — async_playwright
# is imported lazily inside _ensure_page. This keeps server startup fast.
from __future__ import annotations

import asyncio
import logging
from collections import deque
import os
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Optional, TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:  # imported only for type checkers, never at runtime
    from playwright.async_api import Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

# ── Globals ──────────────────────────────────────────────────────────────────

_playwright_instance = None
# Keyed by browser_session_id (bs_id) — the id of a row in the browser_sessions
# table. One persistent tab per id; its own context is its own cookie jar (loaded
# from / saved to that row's storage_state), so logins survive restarts. The Web
# tab and the agent's browser_action both address a tab by this id, so they drive
# the SAME page.
_browsers: dict[str, Browser] = {}        # bs_id -> Browser
_contexts: dict[str, BrowserContext] = {} # bs_id -> BrowserContext
_pages: dict[str, Page] = {}              # bs_id -> Page
# Keys whose browser is an ATTACHED (CDP) connection to a user-visible window,
# not a headless instance we launched. Never tear these down — closing them
# would shut the window the user is looking at; we only disconnect.
_attached: set[str] = set()

# Per-session BACKEND preference: which kind of browser sits behind a bs_id.
#   "headless" (default) — a server-side invisible Chromium we launch.
#   "local"             — the real Chromium running on the user's device,
#                         driven over CDP. The Web tab's "switcher" flips this.
#   "connector"         — the user's REAL browser, possibly on a different
#                         machine across the internet, driven through their
#                         installed browser extension over a WebSocket. No
#                         Playwright page is ever created for these; every action
#                         is forwarded by _connector_action -> browser_connector.
# A session only ATTACHES to a local window when its backend is "local" (or the
# global auto-attach env is set), so headless sessions never get hijacked just
# because a debuggable Chrome happens to be open.
_backend: dict[str, str] = {}  # bs_id -> "headless" | "server" | "local" | "connector"
_server_xvfb: Optional[subprocess.Popen] = None
_SERVER_DISPLAY = os.environ.get("WEBAGENT_SERVER_BROWSER_DISPLAY", ":99")

# Registrable domains each session has actually NAVIGATED to (first-party). Used
# at persist time to keep only first-party + allowlisted cookies and drop the
# ad-tech long tail. In-memory per process; rebuilt as the session navigates.
_visited_domains: dict[str, set] = {}

# Runtime lifecycle bookkeeping for server-owned Playwright sessions. Activity
# uses monotonic time so wall-clock changes cannot accidentally expire every
# browser. ``_active_refs`` protects a session while an agent action or a live
# Browser-page websocket is using it; protected sessions are never idle-reaped
# or displaced to make room under the concurrency cap.
_last_activity: dict[str, float] = {}
_active_refs: dict[str, int] = {}
_active_since: dict[str, float] = {}
_lifecycle_lock = asyncio.Lock()
_idle_gc_task: Optional[asyncio.Task] = None
_navigation_history: dict[str, deque[tuple[float, str]]] = {}
_navigation_loop_closing: set[str] = set()
_NAVIGATION_LOOP_WINDOW_SECONDS = 60.0
_NAVIGATION_LOOP_MAX_NAVIGATIONS = 10
_NAVIGATION_HISTORY_LIMIT = 128


def _session_policy() -> dict:
    """Load the current app-wide browser lifecycle policy."""
    try:
        from app.admin.settings import get_browser_session_policy
        return get_browser_session_policy()
    except Exception:  # noqa: BLE001
        return {
            "max_concurrent_sessions": 3,
            "idle_timeout_seconds": 300,
            "idle_cleanup_enabled": True,
        }


def note_activity(bs_id: str) -> None:
    """Mark a browser session as recently used without opening a browser."""
    if bs_id:
        _last_activity[bs_id] = time.monotonic()


def _set_session_status(bs_id: str, status: str) -> None:
    """Best-effort durable active/idle/closed marker for the session row."""
    try:
        from app.db import browser_sessions_store as _store
        _store.update(bs_id, status=status)
    except Exception:  # noqa: BLE001
        pass


def retain_session(bs_id: str) -> None:
    """Protect a session while an agent action or live viewer is using it."""
    if not bs_id:
        return
    note_activity(bs_id)
    count = _active_refs.get(bs_id, 0)
    if count == 0:
        _active_since[bs_id] = time.monotonic()
    _active_refs[bs_id] = count + 1


def release_session(bs_id: str) -> None:
    """Release one active-use reference and restart its idle clock."""
    if not bs_id:
        return
    count = _active_refs.get(bs_id, 0)
    if count <= 1:
        _active_refs.pop(bs_id, None)
        _active_since.pop(bs_id, None)
    else:
        _active_refs[bs_id] = count - 1
    if bs_id in _pages:
        note_activity(bs_id)


def _headless_session_ids() -> list[str]:
    return [key for key in _pages if key not in _attached]


async def _close_lru_headless_until_below(limit: int, *, reserve: int = 0) -> list[str]:
    """Close inactive LRU headless sessions until ``limit - reserve`` remain.

    Caller owns ``_lifecycle_lock``. Active viewers/actions are never selected.
    """
    closed: list[str] = []
    target = max(0, limit - reserve)
    while len(_headless_session_ids()) > target:
        candidates = [
            key for key in _headless_session_ids()
            if _active_refs.get(key, 0) == 0
        ]
        if not candidates:
            break
        victim = min(candidates, key=lambda key: _last_activity.get(key, 0.0))
        await close(victim, status="idle")
        closed.append(victim)
    return closed


async def reap_idle_sessions(*, now: Optional[float] = None) -> list[str]:
    """Close headless sessions idle beyond policy; return the released ids."""
    policy = _session_policy()
    if not policy.get("idle_cleanup_enabled", True):
        return []
    timeout = max(1, int(policy.get("idle_timeout_seconds", 300)))
    current = time.monotonic() if now is None else now
    async with _lifecycle_lock:
        victims = [
            key for key in _headless_session_ids()
            if _active_refs.get(key, 0) == 0
            and current - _last_activity.get(key, current) >= timeout
        ]
        for key in victims:
            await close(key, status="idle")
        if victims:
            logger.info("Closed %d idle headless browser session(s)", len(victims))
        return victims


async def reap_stale_active_sessions(*, now: Optional[float] = None) -> list[str]:
    current = time.monotonic() if now is None else now
    async with _lifecycle_lock:
        for key in _headless_session_ids():
            if _active_refs.get(key, 0) > 0:
                _active_since.setdefault(key, current)
        victims = [
            key for key in _headless_session_ids()
            if _active_refs.get(key, 0) > 0
            and key in _active_since
            and current - _active_since[key] >= 1800
        ]
        for key in victims:
            await close(key, status="idle")
        if victims:
            logger.warning("Closed %d stale active headless browser session(s)", len(victims))
        return victims


async def enforce_policy() -> dict:
    """Apply idle expiry and the current cap to already-running sessions."""
    idle_closed = await reap_idle_sessions()
    policy = _session_policy()
    limit = max(1, int(policy.get("max_concurrent_sessions", 3)))
    async with _lifecycle_lock:
        cap_closed = await _close_lru_headless_until_below(limit)
    return {"idle_closed": idle_closed, "cap_closed": cap_closed}


async def _idle_gc_loop() -> None:
    global _idle_gc_task
    try:
        while _pages or _browsers:
            policy = _session_policy()
            timeout = max(1, int(policy.get("idle_timeout_seconds", 300)))
            await asyncio.sleep(max(5, min(30, timeout / 2)))
            await reap_stale_active_sessions()
            await enforce_policy()
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001
        logger.exception("Browser idle cleanup loop failed")
    finally:
        _idle_gc_task = None


def _ensure_idle_gc() -> None:
    global _idle_gc_task
    if _idle_gc_task is None or _idle_gc_task.done():
        _idle_gc_task = asyncio.create_task(_idle_gc_loop(), name="browser_idle_gc")


def _navigation_key(url: str) -> str:
    """Return a safe, query-free navigation identity for loop detection."""
    try:
        parsed = urlsplit(url)
        if parsed.hostname:
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme}://{parsed.hostname}{port}{parsed.path or '/'}"
        if parsed.scheme:
            return f"{parsed.scheme}:{parsed.path}"
    except (TypeError, ValueError):
        pass
    return str(url).split("?", 1)[0]


def _record_navigation(bs_id: str, url: str, *, now: Optional[float] = None) -> bool:
    """Record a main-frame navigation and return whether it trips the breaker."""
    if not bs_id or bs_id in _navigation_loop_closing:
        return False
    current = time.monotonic() if now is None else now
    key = _navigation_key(url)
    history = _navigation_history.setdefault(bs_id, deque(maxlen=_NAVIGATION_HISTORY_LIMIT))
    cutoff = current - _NAVIGATION_LOOP_WINDOW_SECONDS
    while history and history[0][0] < cutoff:
        history.popleft()
    history.append((current, key))
    repeats = sum(1 for _, existing_key in history if existing_key == key)
    if repeats <= _NAVIGATION_LOOP_MAX_NAVIGATIONS:
        return False
    _navigation_loop_closing.add(bs_id)
    return True


async def _close_navigation_loop(bs_id: str, url: str) -> None:
    """Close a server-owned browser that repeatedly reloads the same page."""
    try:
        if bs_id in _attached:
            return
        agent_id = chat_session_id = None
        try:
            from app.db import browser_sessions_store as _store
            row = _store.get(bs_id) or {}
            agent_id = row.get("agent_id")
            chat_session_id = row.get("chat_session_id")
        except Exception:  # noqa: BLE001
            pass
        logger.error(
            "Browser navigation loop detected: browser_session_id=%s agent_id=%s "
            "chat_session_id=%s url=%s navigations=%d window_seconds=%d",
            bs_id, agent_id, chat_session_id, _navigation_key(url),
            _NAVIGATION_LOOP_MAX_NAVIGATIONS + 1,
            int(_NAVIGATION_LOOP_WINDOW_SECONDS),
        )
        await close(bs_id, status="navigation_loop")
    except Exception:  # noqa: BLE001
        logger.exception("Failed to stop browser navigation loop: browser_session_id=%s", bs_id)
    finally:
        _navigation_loop_closing.discard(bs_id)


def _watch_page_navigation(bs_id: str, page) -> None:
    """Install the loop breaker for a newly-created Playwright page."""
    def _on_frame_navigated(frame) -> None:
        try:
            if frame != page.main_frame or bs_id in _attached:
                return
            if _record_navigation(bs_id, frame.url):
                asyncio.create_task(
                    _close_navigation_loop(bs_id, frame.url),
                    name=f"browser_navigation_loop:{bs_id}",
                )
        except Exception:  # noqa: BLE001
            logger.debug("Navigation tracking failed for browser session %s", bs_id)
    page.on("framenavigated", _on_frame_navigated)


def note_navigation(bs_id: str, url: str) -> None:
    """Record that ``bs_id`` visited ``url`` as a first-party page. Called from
    every navigation entry point (agent browser_action + the Web panel). Safe and
    cheap; never raises."""
    note_activity(bs_id)
    try:
        from app.tools.cookie_allowlist import registrable_domain
        reg = registrable_domain(url)
        if reg:
            _visited_domains.setdefault(bs_id, set()).add(reg)
    except Exception:  # noqa: BLE001
        pass


def _keep_firstparty_cookies(state, bs_id: str):
    """Drop cookies that are neither first-party (a domain this session visited)
    nor allowlisted (user list + SSO built-ins). Returns (state, dropped). No-op
    when we have no navigation record for the session (so we never nuke a jar we
    know nothing about — the tracker denylist still ran before this)."""
    visited = _visited_domains.get(bs_id)
    if not visited or not isinstance(state, dict):
        return state, 0
    cookies = state.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return state, 0
    try:
        from app.tools import cookie_allowlist as _ca
        from app.db import browser_sessions_store as _store
        owner = (_store.get(bs_id) or {}).get("user_id", "")
    except Exception:  # noqa: BLE001
        return state, 0
    kept = []
    for ck in cookies:
        reg = _ca.registrable_domain(ck.get("domain", ""))
        if reg in visited or _ca.is_allowed(owner, reg):
            kept.append(ck)
    dropped = len(cookies) - len(kept)
    if dropped:
        state = {**state, "cookies": kept}
    return state, dropped


def _cdp_port() -> int:
    """Remote-debugging port to look for a user-visible app window on.

    Matches the launcher's app_window.CDP_PORT (same env var, same default), so
    when the launcher opens the App Window the agent meets it on the same port.
    """
    try:
        return int(os.environ.get("WEBAGENT_BROWSER_CDP_PORT", "9222") or "9222")
    except ValueError:
        return 9222


def _cdp_endpoint_open(port: int) -> bool:
    """Fast, non-blocking check for a CDP endpoint on localhost:port.

    Cheaper than letting connect_over_cdp time out: when nothing is listening
    (the common case — no visible window, or a headless server deployment) we
    skip the attach attempt and go straight to headless.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.25)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    except OSError:
        return False
    finally:
        s.close()


async def _install_tracker_blocker(context) -> None:
    """Register a route on a context that ABORTS requests to ad/tracker domains
    so their cookies are never set. The regex matcher means only tracker URLs hit
    the handler — every other request proceeds natively, no per-request overhead.
    Best-effort: a failure just leaves the context unfiltered."""
    try:
        from app.tools import tracker_blocklist
        rx = tracker_blocklist.tracker_url_regex()
        if not rx:
            return

        async def _abort(route):
            try:
                await route.abort()
            except Exception:
                try:
                    await route.continue_()
                except Exception:
                    pass

        await context.route(rx, _abort)
    except Exception as e:  # noqa: BLE001
        logger.debug("tracker blocker install skipped: %s", e)


async def _ensure_server_display() -> str:
    """Start the server-owned virtual display used by headed Chromium.

    This is an explicit backend, never a silent fallback for headless or the
    user's device browser. It lets an agent choose Chrome's headed rendering
    path on the development server even when no desktop is attached.
    """
    global _server_xvfb
    socket_path = Path("/tmp/.X11-unix") / f"X{_SERVER_DISPLAY.lstrip(':')}"
    if socket_path.exists():
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(0.2)
            probe.connect(str(socket_path))
            return _SERVER_DISPLAY
        except OSError:
            # Xvfb was owned by an earlier app process. Its stale filesystem
            # socket must not be mistaken for a running display after restart.
            try:
                socket_path.unlink()
            except OSError:
                pass
        finally:
            probe.close()
    if _server_xvfb is not None and _server_xvfb.poll() is None:
        return _SERVER_DISPLAY
    xvfb = shutil.which("Xvfb")
    if not xvfb:
        raise RuntimeError("Xvfb is unavailable for the server Chromium backend")
    _server_xvfb = subprocess.Popen(
        [xvfb, _SERVER_DISPLAY, "-screen", "0", "1280x720x24", "-nolisten", "tcp"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(20):
        if socket_path.exists():
            return _SERVER_DISPLAY
        if _server_xvfb.poll() is not None:
            break
        await asyncio.sleep(0.1)
    raise RuntimeError("Could not start the server virtual display for Chromium")


async def _ensure_page_locked(bs_id: str) -> Page:
    """Get or create the live page for a browser session (bs_id).

    Order of preference:
      1. The live page we already hold for this id.
      2. ATTACH to a user-visible app-mode window if one is open (CDP port up)
         — the agent then drives the window the user is watching.
      3. Launch our own headless Chromium (default). The context is seeded with
         the row's saved storage_state (cookies/localStorage) so a login persists
         across restarts.
    """
    global _playwright_instance
    key = bs_id

    # A "connector" session is driven by the user's OWN browser via their
    # extension — there is no server-side page to create. Refuse here so the
    # screencast / proxy paths (get_or_create_page) fail cleanly instead of
    # silently launching a headless Chromium, which would defeat the whole point
    # of the connector backend. browser_action's connector branch never reaches
    # this (it returns before _ensure_page).
    if _backend.get(key) == "connector":
        raise RuntimeError("connector backend has no server-side page")

    if key in _pages:
        page = _pages[key]
        try:
            # Quick health check
            await page.evaluate("1")
            return page
        except Exception:
            logger.info(f"Page for {key} died, re-creating")
            _pages.pop(key, None)
            # A dead attached page means the window was closed — drop the stale
            # connection and revert to headless so the session transparently
            # continues in the in-app browser.
            if key in _attached:
                _attached.discard(key)
                _contexts.pop(key, None)
                _browsers.pop(key, None)
                _backend[key] = "headless"

    if _playwright_instance is None:
        from playwright.async_api import async_playwright  # lazy: heavy import
        _playwright_instance = await async_playwright().start()

    # ── (2) Attach to a visible app window if this session wants the LOCAL
    #        backend (the Web-tab switcher set it, or the global env opted in)
    #        and a debuggable Chrome is actually listening. Headless sessions
    #        deliberately skip this so they're never hijacked by an open window.
    browser = _browsers.get(key)
    want_local = (
        _backend.get(key) == "local"
        or os.environ.get("WEBAGENT_BROWSER_AUTO_ATTACH", "").strip() in ("1", "true", "yes")
    )
    if browser is None and want_local and _cdp_endpoint_open(_cdp_port()):
        try:
            browser = await _playwright_instance.chromium.connect_over_cdp(
                f"http://127.0.0.1:{_cdp_port()}", timeout=3000
            )
            ctx = browser.contexts[0] if browser.contexts else await browser.new_context()
            # Reuse the window's existing page (the app); else open one in it.
            page = ctx.pages[0] if ctx.pages else await ctx.new_page()
            _browsers[key] = browser
            _contexts[key] = ctx
            _pages[key] = page
            _attached.add(key)
            _watch_page_navigation(key, page)
            logger.info(f"browser_action attached to visible window (CDP :{_cdp_port()}) for {key}")
            return page
        except Exception as e:
            logger.info(f"CDP attach failed ({e}); using headless")
            _browsers.pop(key, None)
            _contexts.pop(key, None)
            _attached.discard(key)
            browser = None

    # ── (3) Server-headed or headless launch ─────────────────────────────────
    server_headed = _backend.get(key) == "server"
    server_display = await _ensure_server_display() if server_headed else ""
    policy = _session_policy()
    limit = max(1, int(policy.get("max_concurrent_sessions", 3)))
    if key not in _pages and len(_headless_session_ids()) >= limit:
        await _close_lru_headless_until_below(limit, reserve=1)
        if len(_headless_session_ids()) >= limit:
            raise RuntimeError(
                f"Browser session limit reached ({limit}). Close an active session or "
                "raise the cap in Settings → Browser Session Management."
            )
    if browser is None or not browser.is_connected():
        server_env = None
        if server_headed:
            runtime_root = Path("/tmp/webagent-server-chromium")
            (runtime_root / "config").mkdir(parents=True, exist_ok=True)
            (runtime_root / "cache").mkdir(parents=True, exist_ok=True)
            server_env = {
                **os.environ,
                "DISPLAY": server_display,
                "XDG_CONFIG_HOME": str(runtime_root / "config"),
                "XDG_CACHE_HOME": str(runtime_root / "cache"),
            }
        browser = await _playwright_instance.chromium.launch(
            headless=not server_headed,
            env=server_env,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                # Playwright Chromium's crashpad handler exits under the
                # server virtual display without a configured database.
                *(["--disable-crashpad"] if server_headed else []),
            ],
        )
        _browsers[key] = browser

    context = _contexts.get(key)
    if context is None:
        ctx_kwargs = {
            "viewport": {"width": 1280, "height": 720},
            "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        # Seed cookies/localStorage from the saved row so a login persists across
        # restarts. Best-effort: a missing/invalid blob just starts a clean jar.
        try:
            from app.db import browser_sessions_store as _store
            saved = _store.get_storage_state(bs_id)
            if saved:
                ctx_kwargs["storage_state"] = saved
        except Exception as _se:  # noqa: BLE001
            logger.debug("storage_state load for %s skipped: %s", bs_id, _se)
        context = await browser.new_context(**ctx_kwargs)
        _contexts[key] = context
        # Block ad/tracker requests at the network layer so their cookies are
        # never set (keeps the persisted jar lean + pages load faster). Only on
        # OUR own contexts — never an attached user window. Best-effort.
        await _install_tracker_blocker(context)

    page = await context.new_page()
    _pages[key] = page
    _watch_page_navigation(key, page)

    # ── Resume prior status ──────────────────────────────────────────────────
    # A freshly (re)created page starts on about:blank. If this session has a
    # remembered URL (the row's last navigation, written on every nav), restore
    # it so the tab comes back where it left off — after a server restart, the
    # page dying, or an app reload that finds no live page — instead of blank.
    # Cookies already came back via storage_state above, so a logged-in page
    # resumes logged in AND on the right page. Best-effort: a slow/failed restore
    # just leaves the blank page; it never blocks (or fails) page creation.
    try:
        from app.db import browser_sessions_store as _store
        last = (_store.get(bs_id) or {}).get("url")
        if last and last != "about:blank":
            await page.goto(last, wait_until="domcontentloaded", timeout=15000)
            note_navigation(bs_id, page.url)
    except Exception as _re:  # noqa: BLE001
        logger.debug("resume navigation for %s skipped: %s", bs_id, _re)

    return page


async def _ensure_page(bs_id: str) -> Page:
    """Serialize lifecycle changes, then mark/start idle management."""
    was_live = bs_id in _pages
    async with _lifecycle_lock:
        page = await _ensure_page_locked(bs_id)
    note_activity(bs_id)
    _ensure_idle_gc()
    if not was_live:
        _set_session_status(bs_id, "active")
    return page


async def get_or_create_page(bs_id: str) -> Page:
    """Public accessor used by the live Browser panel (app/api/browser_stream.py).

    Returns the live Playwright page for this browser session, creating the
    browser/context/page on demand. Because it keys on the same bs_id the agent's
    ``browser_action`` resolves to, the human's Web tab and the agent drive the
    **same** page — whatever one does, the other sees live.
    """
    return await _ensure_page(bs_id)


async def persist_state(bs_id: str) -> None:
    """Snapshot the context's cookies/localStorage back to the row so a login
    survives a server restart. No-op for attached (user-window) contexts."""
    if bs_id in _attached:
        return  # the user's own window — not ours to snapshot
    ctx = _contexts.get(bs_id)
    if ctx is None:
        return
    try:
        state = await ctx.storage_state()
        # (1) Drop any KNOWN tracker-domain cookies (belt-and-braces vs the
        #     network blocker; also cleans jars from before blocking).
        from app.tools import tracker_blocklist
        state, d_track = tracker_blocklist.strip_tracker_cookies(state)
        # (2) Keep ONLY first-party (visited) + allowlisted cookies — this is what
        #     catches the ad-tech long tail no denylist can. No-op if we have no
        #     navigation record for this session yet.
        state, d_3p = _keep_firstparty_cookies(state, bs_id)
        if d_track or d_3p:
            logger.info("persist_state(%s): dropped %d tracker + %d non-first-party cookies",
                        bs_id, d_track, d_3p)
        from app.db import browser_sessions_store as _store
        _store.set_storage_state(bs_id, state)
    except Exception as e:  # noqa: BLE001
        logger.debug("persist_state(%s) failed: %s", bs_id, e)


async def cookies_for_session(bs_id: str) -> list:
    """Best cookies we have for a browser session, as Playwright cookie dicts
    ({name, value, domain, path, ...}).

    Prefers the LIVE context (freshest — includes cookies set this session that
    haven't been persisted yet) and falls back to the last-saved storage_state so
    a closed-but-previously-logged-in tab still yields its login. NEVER launches a
    browser. Returns [] when there's nothing. This is how the cookie-replay
    fetch tools "act as the user" using whatever they're logged into on the
    Browser page."""
    ctx = _contexts.get(bs_id)
    if ctx is not None:
        try:
            return await ctx.cookies()
        except Exception as e:  # noqa: BLE001
            logger.debug("live cookies_for_session(%s) failed: %s", bs_id, e)
    try:
        from app.db import browser_sessions_store as _store
        state = _store.get_storage_state(bs_id)
        if isinstance(state, dict):
            ck = state.get("cookies")
            if isinstance(ck, list):
                return ck
    except Exception as e:  # noqa: BLE001
        logger.debug("saved cookies_for_session(%s) failed: %s", bs_id, e)
    return []


_DEFAULT_EMAIL_SELECTOR = "#email, input[name='email'], input[type='email'], input[name='username'], input[autocomplete='username']"
_DEFAULT_PASSWORD_SELECTOR = "#pass, input[name='pass'], input[type='password'], input[name='password'], input[autocomplete='current-password']"
_DEFAULT_SUBMIT_SELECTOR = "button[name='login'], button[type='submit'], input[type='submit'], [data-testid='royal_login_button']"


async def _vault_login_impl(
    bs_id: str,
    *,
    email: str,
    password: str,
    login_url: Optional[str] = None,
    email_selector: Optional[str] = None,
    password_selector: Optional[str] = None,
    submit_selector: Optional[str] = None,
    success_selector: Optional[str] = None,
    wait_ms: int = 6000,
) -> dict:
    """Fill a site's login form with vault-held credentials, SERVER-SIDE.

    ``email`` / ``password`` are read from the encrypted vault by the caller and
    passed in here; they are typed straight into the page and NEVER returned. The
    result reports only the outcome — logged_in / needs_2fa / url — so the secret
    never enters the agent's context. Cookies are persisted on success so the
    headless session can be reused afterward.
    """
    if not email or not password:
        return {"logged_in": False, "needs_2fa": False, "error": "no_credentials",
                "message": "No saved login to use."}
    page = await _ensure_page(bs_id)
    try:
        if login_url:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=max(wait_ms, 15000))
            try:
                note_navigation(bs_id, login_url)
            except Exception:
                pass
        esel = email_selector or _DEFAULT_EMAIL_SELECTOR
        psel = password_selector or _DEFAULT_PASSWORD_SELECTOR
        ssel = submit_selector or _DEFAULT_SUBMIT_SELECTOR
        # Fill — Playwright's .fill targets the first match of a comma selector.
        try:
            await page.fill(esel, email, timeout=wait_ms)
            await page.fill(psel, password, timeout=wait_ms)
        except Exception as fe:
            return {"logged_in": False, "needs_2fa": False, "error": "form_not_found",
                    "url": page.url,
                    "message": f"Could not find the login fields on the page ({fe}). "
                               f"Pass email_selector/password_selector that match this site's form."}
        try:
            await page.click(ssel, timeout=wait_ms)
        except Exception:
            # Fall back to submitting via Enter on the password field.
            try:
                await page.press(psel, "Enter")
            except Exception as ce:
                return {"logged_in": False, "needs_2fa": False, "error": "submit_failed",
                        "url": page.url, "message": f"Filled the form but couldn't submit it ({ce})."}
        # Let the navigation / XHR settle.
        try:
            await page.wait_for_load_state("networkidle", timeout=wait_ms)
        except Exception:
            pass

        cur = (page.url or "").lower()
        needs_2fa = any(k in cur for k in ("checkpoint", "two_factor", "2fa", "twofactor", "challenge", "/auth/"))
        # Still showing a password field (and not a 2FA wall) → login likely failed.
        still_pw = False
        try:
            still_pw = await page.is_visible(psel, timeout=1000)
        except Exception:
            still_pw = False
        logged_in = False
        if success_selector:
            try:
                logged_in = await page.is_visible(success_selector, timeout=2000)
            except Exception:
                logged_in = False
        else:
            logged_in = (not needs_2fa) and (not still_pw) and ("login" not in cur)

        # Persist cookies so the session is reusable headlessly afterward.
        try:
            await persist_state(bs_id)
        except Exception:
            pass

        title = ""
        try:
            title = await page.title()
        except Exception:
            title = ""

        if needs_2fa:
            msg = ("Login submitted but the site is asking for a verification step "
                   "(2FA / checkpoint). Ask the user to complete it on the Browser page; "
                   "the session will then be reused automatically.")
        elif logged_in:
            msg = "Logged in successfully; the session is saved for reuse."
        elif still_pw:
            msg = ("Login did not go through (the password field is still showing). "
                   "The saved credentials may be wrong, or the site needs a captcha — "
                   "ask the user to sign in on the Browser page.")
        else:
            msg = "Login submitted; outcome unclear. Verify by reading the page."
        return {"logged_in": bool(logged_in), "needs_2fa": bool(needs_2fa),
                "url": page.url, "title": title, "message": msg}
    except Exception as e:  # noqa: BLE001
        logger.warning("vault_login(%s) failed: %s", bs_id, e)
        return {"logged_in": False, "needs_2fa": False, "error": "exception",
                "url": getattr(page, "url", ""), "message": f"Login attempt errored: {e}"}


async def vault_login(bs_id: str, **kwargs) -> dict:
    """Run a protected login so lifecycle enforcement cannot reclaim its page."""
    retain_session(bs_id)
    try:
        return await _vault_login_impl(bs_id, **kwargs)
    finally:
        release_session(bs_id)


async def close(bs_id: str, *, status: str = "closed") -> dict:
    """Persist login state, then tear down the browser for this session.

    Attached (CDP) connections to a user-visible window are only DISCONNECTED —
    closing their page/context would shut the window the user is looking at.
    """
    key = bs_id
    await persist_state(bs_id)
    _set_session_status(bs_id, status)
    _last_activity.pop(key, None)
    _navigation_history.pop(key, None)
    _navigation_loop_closing.discard(key)
    _active_refs.pop(key, None)
    _active_since.pop(key, None)
    _visited_domains.pop(key, None)
    _backend.pop(key, None)
    if key in _attached:
        _pages.pop(key, None)
        _contexts.pop(key, None)
        b = _browsers.pop(key, None)
        _attached.discard(key)
        if b is not None:
            try:
                await b.close()  # CDP: disconnects; the window stays open
            except Exception:
                pass
        return {"success": True, "result": "Detached from app window", "url": "", "title": ""}
    if key in _pages:
        try:
            await _pages[key].close()
        except Exception:
            pass
        _pages.pop(key, None)
    if key in _contexts:
        try:
            await _contexts[key].close()
        except Exception:
            pass
        _contexts.pop(key, None)
    if key in _browsers:
        try:
            await _browsers[key].close()
        except Exception:
            pass
        _browsers.pop(key, None)
    return {"success": True, "result": "Browser closed", "url": "", "title": ""}


def resolve_agent_session(user_id: str, agent_id: str,
                          browser_session_id: Optional[str] = None,
                          chat_session_id: Optional[str] = None) -> str:
    """Resolve the browser-session id an agent chat is allowed to drive.

    Rules: a tab is reachable by the agent only if it is owned by ``user_id``,
    linked to THIS ``agent_id``, shared, and (when available) owned by THIS chat
    session. This is the isolation boundary that prevents concurrent chats for
    one agent from racing on the same page.
    """
    from app.db import browser_sessions_store as _store
    if browser_session_id:
        row = _store.get(browser_session_id)
        if not row or row.get("user_id") != user_id:
            raise PermissionError("browser session not found")
        if row.get("agent_id") != agent_id or not row.get("shared"):
            raise PermissionError("browser session is not shared with this agent")
        owner_chat = row.get("chat_session_id")
        if chat_session_id and owner_chat and owner_chat != chat_session_id:
            raise PermissionError("browser session belongs to a different chat session")
        if chat_session_id and not owner_chat:
            _store.update(browser_session_id, chat_session_id=chat_session_id)
        return browser_session_id
    shared = (_store.list_shared_for_agent_session(user_id, agent_id, chat_session_id)
              if chat_session_id else _store.list_shared_for_agent(user_id, agent_id))
    if shared:
        return shared[0]["id"]
    created = _store.create(
        user_id, agent_id=agent_id, chat_session_id=chat_session_id,
        title="Agent browser", shared=True,
    )
    return created["id"]


async def _browser_action_impl(
    bs_id: str,
    action: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    js: Optional[str] = None,
    timeout_ms: int = 5000,
    full_page: bool = True,
) -> dict:
    """
    Execute a browser automation action against a browser session (bs_id).

    A persistent headless Chromium is maintained per browser session. Consecutive
    calls with the same bs_id share the same page, and the user's Web tab streams
    that same page live.

    Args:
        bs_id: The browser-session id to drive (resolved by the caller/loader
            from the agent's sharing-gated session).
        action: One of:
            - "navigate"    → url (required)
            - "click"       → selector (required)
            - "type"        → selector (required), text (required)
            - "get_text"    → selector (optional, defaults to body)
            - "get_html"    → selector (optional, defaults to body)
            - "screenshot"  → full_page (optional, default true)
            - "wait"        → timeout_ms (optional, default 5000)
            - "evaluate"    → js (required)
            - "title"       → returns page title
            - "url"         → returns current URL
            - "close"       → closes browser for this session
        selector: CSS selector for click/type/get_text actions
        text: Text to type (for type action)
        url: URL to navigate to (for navigate action)
        js: JavaScript code to evaluate (for evaluate action)
        timeout_ms: Timeout in ms for wait action (default 5000)
        full_page: Capture full scrollable page for screenshot (default true)

    Returns:
        dict with:
            - success: bool
            - result: str (text content, screenshot as base64, HTML, etc.)
            - error: str (if failed)
            - url: str (current page URL)
            - title: str (current page title)
    """
    # Connector backend: forward the WHOLE action (including "close") to the
    # owner's browser extension — their real, possibly-remote browser. Branches
    # before any Playwright work; the headless/local path below is untouched.
    if _backend.get(bs_id) == "connector":
        return await _connector_action(
            bs_id, action, selector=selector, text=text, url=url, js=js,
            timeout_ms=timeout_ms, full_page=full_page,
        )

    try:
        if action == "close":
            return await close(bs_id)

        page = await _ensure_page(bs_id)

        if action == "navigate":
            if not url:
                return {"success": False, "error": "url required for navigate action", "url": "", "title": ""}
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1000)  # let JS render settle
            title = await page.title()
            note_navigation(bs_id, page.url)  # mark this site first-party
            # Remember where this tab is + snapshot the (now possibly logged-in)
            # cookie jar so the row survives a restart.
            try:
                from app.db import browser_sessions_store as _store
                _store.update(bs_id, url=page.url, title=title)
            except Exception:  # noqa: BLE001
                pass
            await persist_state(bs_id)
            return {
                "success": True,
                "result": f"Navigated to {url}",
                "url": page.url,
                "title": title,
            }

        elif action == "click":
            if not selector:
                return {"success": False, "error": "selector required for click action", "url": page.url, "title": await page.title()}
            await page.wait_for_selector(selector, timeout=10000)
            await page.click(selector)
            await page.wait_for_timeout(500)
            return {
                "success": True,
                "result": f"Clicked '{selector}'",
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "type":
            if not selector or text is None:
                return {"success": False, "error": "selector and text required for type action", "url": page.url, "title": await page.title()}
            await page.wait_for_selector(selector, timeout=10000)
            await page.fill(selector, "")
            await page.type(selector, text, delay=20)
            return {
                "success": True,
                "result": f"Typed '{text[:100]}' into '{selector}'",
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "get_text":
            # Default to the whole page like get_html does, so "read the page text"
            # works without the model having to know it must pass selector="body".
            if not selector:
                selector = "body"
            try:
                await page.wait_for_selector(selector, timeout=5000)
                el = await page.query_selector(selector)
                if el:
                    text = await el.inner_text()
                else:
                    text = ""
            except Exception:
                text = ""
            return {
                "success": True,
                "result": text,
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "get_html":
            if selector:
                try:
                    el = await page.query_selector(selector)
                    html = await el.inner_html() if el else ""
                except Exception:
                    html = ""
            else:
                html = await page.content()
            return {
                "success": True,
                "result": html,
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "screenshot":
            filename = f"{uuid.uuid4().hex}.png"
            # Land the screenshot in the owning workspace. An unresolved owner
            # fails closed: there is deliberately no shared screenshot folder.
            try:
                from app.db import browser_sessions_store as _bstore
                from app import user_workspace as _ws
                from app.auth.workspace import current_workspace
                row = _bstore.get(bs_id) or {}
                owner = row.get("user_id")
                if not owner:
                    workspace = current_workspace()
                    owner = workspace.subject_id if workspace else ""
                if not owner:
                    raise RuntimeError("browser screenshot has no owning workspace")
                shots_dir = _ws.user_dir(owner, "screenshots")
                filepath = shots_dir / filename
                await page.screenshot(path=str(filepath), full_page=full_page)
                from app.encryption.workspace_files import encrypt_file_in_place
                await encrypt_file_in_place(owner, filepath)
                uri = _ws.public_url(owner, "screenshots", filename)
            except Exception as _e:  # noqa: BLE001
                logger.warning("workspace screenshot failed closed: %s", _e)
                return {
                    "success": False,
                    "error": "Screenshot workspace could not be resolved",
                    "url": page.url,
                    "title": await page.title(),
                }
            return {
                "success": True,
                "result": uri,
                "url": page.url,
                "title": await page.title(),
                "mime_type": "image/png",
            }

        elif action == "wait":
            await page.wait_for_timeout(timeout_ms)
            return {
                "success": True,
                "result": f"Waited {timeout_ms}ms",
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "evaluate":
            if not js:
                return {"success": False, "error": "js required for evaluate action", "url": page.url, "title": await page.title()}
            result = await page.evaluate(js)
            return {
                "success": True,
                "result": str(result),
                "url": page.url,
                "title": await page.title(),
            }

        elif action == "title":
            title = await page.title()
            return {
                "success": True,
                "result": title,
                "url": page.url,
                "title": title,
            }

        elif action == "url":
            return {
                "success": True,
                "result": page.url,
                "url": page.url,
                "title": await page.title(),
            }

        else:
            return {"success": False, "error": f"Unknown action '{action}'", "url": "", "title": ""}

    except Exception as e:
        logger.error(f"browser_action ({action}) failed: {e}")
        # Try to get current URL/title even on failure
        try:
            current_url = _pages.get(bs_id)
            url_str = current_url.url if current_url else ""
            title_str = await current_url.title() if current_url else ""
        except Exception:
            url_str = ""
            title_str = ""
        return {
            "success": False,
            "error": str(e),
            "url": url_str,
            "title": title_str,
        }


async def browser_action(
    bs_id: str,
    action: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    js: Optional[str] = None,
    timeout_ms: int = 5000,
    full_page: bool = True,
) -> dict:
    """Protected public wrapper around one browser automation action."""
    retain_session(bs_id)
    try:
        return await _browser_action_impl(
            bs_id, action, selector=selector, text=text, url=url, js=js,
            timeout_ms=timeout_ms, full_page=full_page,
        )
    finally:
        release_session(bs_id)


async def _connector_action(
    bs_id: str,
    action: str,
    *,
    selector: Optional[str] = None,
    text: Optional[str] = None,
    url: Optional[str] = None,
    js: Optional[str] = None,
    timeout_ms: int = 5000,
    full_page: bool = True,
) -> dict:
    """Forward a browser_action to the session owner's browser extension.

    The owner is read from the browser-session row (the connector keys by user, so
    a command only ever reaches that user's own extension). The reply already
    matches the browser_action contract; we just keep the stored row's url/title
    fresh, like the headless navigate path does.
    """
    from app.db import browser_sessions_store as _store
    row = _store.get(bs_id) or {}
    owner = row.get("user_id")
    if not owner:
        return {"success": False, "error": "browser session not found", "url": "", "title": ""}

    params = {
        "selector": selector, "text": text, "url": url, "js": js,
        "timeout_ms": timeout_ms, "full_page": full_page,
    }
    params = {k: v for k, v in params.items() if v is not None}

    from app.tools import browser_connector as _conn
    result = await _conn.connector_execute(owner, bs_id, action, params)

    try:
        if result.get("success") and result.get("url"):
            _store.update(bs_id, url=result.get("url"),
                          title=result.get("title") or row.get("title"))
    except Exception:  # noqa: BLE001
        pass
    return result


# ── Local (on-device) browser: launch, attach, switch ─────────────────────────
# Phase 1 of the "drive the real browser" feature. The user's Web tab (and the
# agent, with confirmation) can flip a session from the headless backend to the
# real Chromium on the user's device. Because everything keys on bs_id, the swap
# is invisible to the agent's tools — the same session id just points at a
# different browser. Same-machine only for now; the remote/companion path
# (server and device on different machines) is a later phase.


def _find_chrome() -> Optional[str]:
    """Best-effort path to a Chrome/Chromium/Edge executable on this machine.

    Checks the platform's usual install locations and PATH. Returns None if none
    is found (the caller then reports that the device has no compatible browser).
    """
    # An explicit override always wins.
    env = os.environ.get("WEBAGENT_LOCAL_BROWSER_PATH", "").strip()
    if env and Path(env).exists():
        return env

    candidates: list[str] = []
    if sys.platform.startswith("win"):
        pf = os.environ.get("PROGRAMFILES", r"C:\Program Files")
        pf86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
        local = os.environ.get("LOCALAPPDATA", "")
        for base in (pf, pf86, local):
            if not base:
                continue
            candidates += [
                str(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe"),
                str(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe"),
            ]
        for exe in ("chrome", "chrome.exe", "msedge", "msedge.exe"):
            found = shutil.which(exe)
            if found:
                candidates.append(found)
    elif sys.platform == "darwin":
        candidates += [
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        ]
    else:  # linux / other
        for exe in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            found = shutil.which(exe)
            if found:
                candidates.append(found)
        # Container images install Chromium through Playwright rather than as a
        # PATH-level system package. Recognize that full browser executable so a
        # local-backend probe reports the real display limitation instead of the
        # false claim that Chromium is not installed.
        playwright_root = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
        if playwright_root:
            root = Path(playwright_root)
            for pattern in (
                "chromium-*/chrome-linux64/chrome",
                "chromium-*/chrome-linux/chrome",
            ):
                candidates.extend(
                    str(path) for path in sorted(root.glob(pattern))
                )

    for c in candidates:
        try:
            if c and Path(c).exists():
                return c
        except OSError:
            continue
    return None


def _default_user_data_dir() -> Optional[str]:
    """The user's everyday Chrome profile directory for this OS, if it exists.

    Driving this profile gives the agent the user's real logins. NOTE: recent
    Chrome builds refuse to enable remote-debugging on the *default* profile dir
    (an anti-cookie-theft measure), so this can fail — the caller falls back to a
    dedicated profile and reports which one it used.
    """
    try:
        if sys.platform.startswith("win"):
            base = os.environ.get("LOCALAPPDATA", "")
            p = Path(base) / "Google" / "Chrome" / "User Data" if base else None
        elif sys.platform == "darwin":
            p = Path.home() / "Library" / "Application Support" / "Google" / "Chrome"
        else:
            p = Path.home() / ".config" / "google-chrome"
        return str(p) if p and p.exists() else None
    except Exception:  # noqa: BLE001
        return None


def _dedicated_user_data_dir(user_id: Optional[str] = None) -> str:
    """A clean, app-owned Chrome profile dir (persists its own logins) for the
    real-browser backend. Per-user so each user's device logins stay separate and
    live under their own data home: ``data/user_data/<user_id>/chrome-profile/``.
    Falls back to the legacy shared ``data/chrome-profile`` only when no user is
    known (should be rare — every chat call carries a user_id)."""
    if user_id:
        try:
            from app.user_workspace import user_dir as _user_dir
            return str(_user_dir(user_id, "chrome-profile"))
        except Exception:  # noqa: BLE001 — fall through to the shared dir
            pass
    d = Path(__file__).resolve().parent.parent.parent / "data" / "chrome-profile"
    d.mkdir(parents=True, exist_ok=True)
    return str(d)


def _clear_singleton_lock(user_data_dir: str) -> None:
    """Remove a stale Chrome singleton lock left by a crashed/half-launched
    instance. Without this, a fresh launch on a dir whose previous owner died
    uncleanly hands off to nothing and never opens the debug port. Safe to call
    when the files don't exist (the normal case)."""
    try:
        base = Path(user_data_dir)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            p = base / name
            try:
                if p.exists() or p.is_symlink():
                    p.unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        pass


async def _spawn_debug_chrome(exe: str, port: int, user_data: str, profile_used: str) -> dict:
    """Launch one Chrome with remote-debugging on ``user_data`` and wait for the
    debug port. Fast-fails when the process exits early (the "handed off to an
    already-running Chrome on this profile, then quit" case) so the caller can move
    on to the next profile without burning the full timeout."""
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data}",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",  # required by modern Chrome for CDP attach
        "about:blank",
    ]
    try:
        creationflags = 0
        if sys.platform.startswith("win"):
            # Detach so closing the server doesn't kill the user's window.
            creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "launch_failed",
                "message": f"Could not start the local browser: {e}"}

    for _ in range(40):  # up to ~10s
        if _cdp_endpoint_open(port):
            return {"ok": True, "reused": False, "profile_used": profile_used, "port": port,
                    "message": f"Launched local Chrome ({profile_used} profile) on debug port {port}."}
        # Fast-fail: the launcher process already exited without opening the port
        # (it handed off to an existing Chrome on this profile). No point waiting.
        if proc.poll() is not None:
            return {"ok": False, "error": "no_debug_port", "profile_used": profile_used,
                    "message": f"Chrome ({profile_used} profile) handed off to an already-running "
                               f"instance and did not open a debug port."}
        await asyncio.sleep(0.25)
    return {"ok": False, "error": "no_debug_port", "profile_used": profile_used,
            "message": f"Chrome ({profile_used} profile) started but did not open its debug port."}


async def _launch_local_chrome(profile: str = "default", user_id: Optional[str] = None) -> dict:
    """Make sure a debuggable local Chrome is running on the CDP port.

    Returns a status dict: ``{ok, reused, profile_used, port, message}``.
      • If a CDP endpoint is already open on the port we REUSE it (the user — or a
        previous launch — already started one).
      • Otherwise we launch Chrome with remote-debugging on. We try the user's
        everyday profile first (their real logins) UNLESS ``profile="dedicated"``,
        then **always fall back to a per-user dedicated profile**. The fallback is
        essential: modern Chrome refuses remote-debugging on the everyday profile
        (anti-cookie-theft) and an already-running everyday Chrome can't be
        re-launched with a debug port — but a SEPARATE dedicated profile launches a
        new instance that coexists with the user's open Chrome and opens the port.
    """
    port = _cdp_port()
    if _cdp_endpoint_open(port):
        return {"ok": True, "reused": True, "profile_used": "existing", "port": port,
                "message": f"Reusing the Chrome already listening on debug port {port}."}

    exe = _find_chrome()
    if not exe:
        return {"ok": False, "reused": False, "port": port,
                "error": "no_browser",
                "message": "No Chrome/Chromium/Edge found on this local device. This does not "
                           "affect the installed headless Playwright backend; switch to headless "
                           "or use a safe development fixture if device-only data is unavailable. "
                           "Install one or set WEBAGENT_LOCAL_BROWSER_PATH to its executable."}
    if (
        sys.platform.startswith("linux")
        and not os.environ.get("DISPLAY", "").strip()
        and not os.environ.get("WAYLAND_DISPLAY", "").strip()
    ):
        return {
            "ok": False,
            "reused": False,
            "port": port,
            "error": "no_display",
            "message": (
                "Chromium is installed for the headless Playwright backend, but this "
                "container has no graphical display for a visible local-browser window. "
                "Use the headless backend or a connected device browser."
            ),
        }

    # Ordered profiles to try. Everyday-first (real logins) with a guaranteed
    # dedicated fallback; or dedicated-only when explicitly asked.
    attempts: list[tuple[str, str]] = []
    if profile != "dedicated":
        _dflt = _default_user_data_dir()
        if _dflt:
            attempts.append(("default", _dflt))
    attempts.append(("dedicated", _dedicated_user_data_dir(user_id)))

    last: dict = {}
    for profile_used, user_data in attempts:
        if profile_used == "dedicated":
            # A crashed prior instance can leave a lock that makes the launch
            # silently hand off to nothing — clear it so the fallback is reliable.
            _clear_singleton_lock(user_data)
        last = await _spawn_debug_chrome(exe, port, user_data, profile_used)
        if last.get("ok"):
            return {**last, "port": port}

    return {"ok": False, "reused": False, "port": port,
            "error": last.get("error", "no_debug_port"),
            "message": (last.get("message", "Could not open a debuggable local browser.")
                        + " Tried the everyday profile then a dedicated one.")}


def current_url(bs_id: str) -> str:
    """Current URL of the live page for a session, or '' if none is open. Cheap;
    never launches a browser."""
    page = _pages.get(bs_id)
    try:
        return page.url if page else ""
    except Exception:  # noqa: BLE001
        return ""


async def _snapshot_attached_cookies(bs_id: str) -> None:
    """Pull cookies from an ATTACHED (real-device) context into the saved row so a
    later headless session re-seeds with the same login. persist_state skips
    attached contexts (not ours to snapshot), so this is the explicit hand-off
    used when switching a session back to headless."""
    ctx = _contexts.get(bs_id)
    if ctx is None:
        return
    try:
        cookies = await ctx.cookies()
        if not cookies:
            return
        from app.db import browser_sessions_store as _store
        prior = _store.get_storage_state(bs_id) or {}
        if not isinstance(prior, dict):
            prior = {}
        prior["cookies"] = cookies
        _store.set_storage_state(bs_id, prior)
    except Exception as e:  # noqa: BLE001
        logger.debug("snapshot_attached_cookies(%s) failed: %s", bs_id, e)


async def open_local_browser(bs_id: str, *, profile: str = "default", user_id: Optional[str] = None) -> dict:
    """Switch a session to the LOCAL (on-device) browser backend.

    Launches/reuses a debuggable Chrome on the device, re-points this bs_id at it,
    and carries the current page forward so the real window opens where the
    headless one was. ``user_id`` scopes the dedicated fallback profile to that
    user's data home. Returns a status dict the Web tab / agent can show.
    """
    prev_url = current_url(bs_id)

    launch = await _launch_local_chrome(profile=profile, user_id=user_id)
    if not launch.get("ok"):
        return {"success": False, "backend": _backend.get(bs_id, "headless"), **launch}

    # Tear down the headless instance for this key (ours → safe to close) so
    # _ensure_page re-binds to the attached window.
    if bs_id not in _attached:
        for d in (_pages, _contexts, _browsers):
            obj = d.pop(bs_id, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:  # noqa: BLE001
                    pass

    _backend[bs_id] = "local"
    try:
        page = await _ensure_page(bs_id)
    except Exception as e:  # noqa: BLE001
        _backend[bs_id] = "headless"
        return {"success": False, "backend": "headless", "error": "attach_failed",
                "message": f"Started the local browser but could not attach to it: {e}"}

    if bs_id not in _attached:
        # The endpoint was up but the attach silently fell through to headless.
        _backend[bs_id] = "headless"
        return {"success": False, "backend": "headless", "error": "attach_failed",
                "message": "Could not attach to the local browser window."}

    # Bring the real window to where the session was.
    if prev_url and prev_url not in ("about:blank", ""):
        try:
            await page.goto(prev_url, wait_until="domcontentloaded", timeout=30000)
            note_navigation(bs_id, page.url)
        except Exception:  # noqa: BLE001
            pass

    title = ""
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        pass
    return {"success": True, "backend": "local", "attached": True,
            "profile_used": launch.get("profile_used"), "url": page.url, "title": title,
            "message": launch.get("message", "Now driving the browser on your device.")}


async def open_server_browser(bs_id: str) -> dict:
    """Switch to a headed Chromium running on the server's virtual display."""
    prev_url = current_url(bs_id)
    if bs_id not in _attached:
        for mapping in (_pages, _contexts, _browsers):
            obj = mapping.pop(bs_id, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:  # noqa: BLE001
                    pass
    _attached.discard(bs_id)
    _backend[bs_id] = "server"
    try:
        page = await _ensure_page(bs_id)
    except Exception as e:  # noqa: BLE001
        _backend[bs_id] = "headless"
        return {"success": False, "backend": "headless", "error": "server_browser_failed",
                "message": f"Could not start headed server Chromium: {e}"}
    if prev_url and prev_url not in ("about:blank", ""):
        try:
            await page.goto(prev_url, wait_until="domcontentloaded", timeout=30000)
            note_navigation(bs_id, page.url)
        except Exception:  # noqa: BLE001
            pass
    return {"success": True, "backend": "server", "attached": False,
            "url": page.url, "title": await page.title(),
            "message": "Now driving headed Chromium on the server virtual display."}


async def use_remote_browser(bs_id: str) -> dict:
    """Switch a session back to the HEADLESS (server-side) backend.

    Snapshots the device browser's login forward, disconnects from the real window
    (leaving it open for the user), then re-binds this bs_id to a fresh headless
    page seeded with that login and navigated to where the session was.
    """
    prev_url = current_url(bs_id)
    await _snapshot_attached_cookies(bs_id)

    # Disconnect (never close) any attached window, then drop the binding.
    if bs_id in _attached:
        _pages.pop(bs_id, None)
        _contexts.pop(bs_id, None)
        b = _browsers.pop(bs_id, None)
        _attached.discard(bs_id)
        if b is not None:
            try:
                await b.close()  # CDP: disconnects; the window stays open
            except Exception:  # noqa: BLE001
                pass

    _backend[bs_id] = "headless"
    try:
        page = await _ensure_page(bs_id)
    except Exception as e:  # noqa: BLE001
        return {"success": False, "backend": "headless", "error": "headless_failed",
                "message": f"Could not start the headless browser: {e}"}

    if prev_url and prev_url not in ("about:blank", ""):
        try:
            await page.goto(prev_url, wait_until="domcontentloaded", timeout=30000)
            note_navigation(bs_id, page.url)
        except Exception:  # noqa: BLE001
            pass

    title = ""
    try:
        title = await page.title()
    except Exception:  # noqa: BLE001
        pass
    return {"success": True, "backend": "headless", "attached": False,
            "url": page.url, "title": title,
            "message": "Switched back to the in-app browser. You can close the device window."}


async def use_connector_browser(bs_id: str, *, user_id: Optional[str] = None) -> dict:
    """Switch a session to the CONNECTOR backend — the user's real browser, driven
    through their installed extension over the connector WebSocket.

    Requires that the owner has a live extension connection; otherwise the backend
    is left unchanged and the reason is reported. No Playwright page is created for
    a connector session, so we just tear down any headless instance we own for this
    key (never an attached window) and flip the flag — _connector_action then routes
    every action over the socket.
    """
    from app.db import browser_sessions_store as _store
    row = _store.get(bs_id) or {}
    owner = user_id or row.get("user_id")

    from app.tools import browser_connector as _conn
    if not owner or not _conn.connector_connected(owner):
        return {"success": False, "backend": _backend.get(bs_id, "headless"),
                "connected": False, "error": "extension_not_connected",
                "message": "No browser extension is connected for this user. Install and "
                           "connect the WebAgent browser extension, then try again."}

    # Tear down any headless instance we launched for this key (ours → safe to
    # close). Never touch an attached on-device window.
    if bs_id not in _attached:
        for d in (_pages, _contexts, _browsers):
            obj = d.pop(bs_id, None)
            if obj is not None:
                try:
                    await obj.close()
                except Exception:  # noqa: BLE001
                    pass

    _backend[bs_id] = "connector"
    info = _conn.connector_info(owner)
    return {"success": True, "backend": "connector", "connected": True,
            "version": info.get("version", ""),
            "message": "Now driving your own browser through the extension."}


def backend_status(bs_id: str) -> dict:
    """Report which backend a session is on plus what the device offers, so the
    Web tab can render the switcher (and the agent can check before switching)."""
    port = _cdp_port()
    status = {
        "backend": _backend.get(bs_id, "headless"),
        "attached": bs_id in _attached,
        "chrome_running": _cdp_endpoint_open(port),
        "chrome_found": bool(_find_chrome()),
        "server_headed_available": bool(shutil.which("Xvfb")) and bool(_find_chrome()),
        "port": port,
        "url": current_url(bs_id),
    }
    # Connector (remote browser-extension) presence for the owning user, so the
    # Web tab can show whether the "My browser" backend is available / live.
    try:
        from app.db import browser_sessions_store as _store
        owner = (_store.get(bs_id) or {}).get("user_id")
        if owner:
            from app.tools import browser_connector as _conn
            status["connector"] = _conn.connector_info(owner)
    except Exception:  # noqa: BLE001
        pass
    return status


async def close_all():
    """Clean up all browser instances. Call on server shutdown.

    Attached (CDP) connections to user-visible windows are only DISCONNECTED —
    we never close their page/context, which would shut the user's window.
    """
    global _playwright_instance, _idle_gc_task
    task = _idle_gc_task
    _idle_gc_task = None
    if task is not None and task is not asyncio.current_task():
        task.cancel()
    for key in list(_pages.keys()):
        if key in _attached:
            continue  # never close a window the user owns
        try:
            await _pages[key].close()
        except Exception:
            pass
    for key in list(_contexts.keys()):
        if key in _attached:
            continue
        try:
            await _contexts[key].close()
        except Exception:
            pass
    for key in list(_browsers.keys()):
        try:
            await _browsers[key].close()  # CDP-connected → just disconnects
        except Exception:
            pass
    _pages.clear()
    _contexts.clear()
    _browsers.clear()
    _attached.clear()
    _backend.clear()
    _last_activity.clear()
    _navigation_history.clear()
    _navigation_loop_closing.clear()
    _active_refs.clear()
    _active_since.clear()
    if _playwright_instance:
        await _playwright_instance.stop()
        _playwright_instance = None
    logger.info("All browser instances closed")


def resource_status() -> dict:
    """Return only the server resources the Control kill switch can release.

    Connector sessions are deliberately excluded: their browser runs in the
    user's extension/browser, not in this server process. Attached local Chrome
    windows are counted as connections; an emergency stop detaches from them but
    never kills a user-owned browser window.
    """
    attached = len(_attached)
    policy = _session_policy()
    return {
        "live_sessions": len(_pages),
        "headless_sessions": max(0, len(_pages) - attached),
        "attached_sessions": attached,
        "playwright_running": bool(_playwright_instance),
        "active_sessions": sum(1 for key in _pages if _active_refs.get(key, 0) > 0),
        "policy": policy,
    }


async def emergency_stop() -> dict:
    """Close every server-managed browser and detach from local browsers."""
    before = resource_status()
    await close_all()
    return before

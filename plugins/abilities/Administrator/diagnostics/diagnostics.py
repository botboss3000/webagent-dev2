"""Diagnostics ability — SELF-CONTAINED drop-in.

This file is the whole capability: descriptor (FEATURE / the .json sibling),
runtime handler, helpers, and schema all live here. There is NO core factory —
``build_tools`` defines ``read_diagnostics`` (plus its helpers and JSON schema)
inline, closing over the injected ``user_id``. All heavy imports
(app.db.logs_store, app.agent.diagnostics) stay LAZY inside the handler so merely
scanning FEATURE / importing this module never pulls in the app.

Discovered generically by core via three optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE       — catalog + UI + which tool names it gates (in diagnostics.json)
  • build_tools() — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read by the loader AFTER build_tools() runs

What it does: lets an agent read the in-app flight recorder — recent server
errors/tracebacks, agent-loop pipeline problems, failed runs, tool metrics — so
it can diagnose the running app. The flight-recorder writes to the dedicated,
always-local logs database (data/db/logs.db + recordings.db — see
app/db/logs_store.py), separate from the main app DB; the recorder itself lives
in app/agent/diagnostics.py.

``view='tools'`` switches to aggregated tool-execution metrics (per-tool call
count, failure rate, avg/max duration, recent failures) from the structured
tool_executions table.
"""

from __future__ import annotations

import json
import logging
from urllib.parse import quote, urlsplit
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() so the loader (which reads TOOL_SCHEMAS AFTER
# the call) sees the live schema. read_diagnostics is a read-only flight-recorder
# query (loader marks it destructive=False). login_as_current_user mints a session
# AS the user and writes it into the browser — it acts as the user, so it must be
# confirm-gated (destructive) in Ask/Plan mode.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = {"login_as_current_user", "dev_browser_action", "dev_browser_record"}
# These remain destructive in Ask/Plan mode, but their permissions may be set
# to Auto so an Auto-mode development journey does not stop for approval.
REQUIRES_CONFIRMATION: set = set()


# Module-level JSON schema for read_diagnostics. Lives here (not in build_tools)
# so it is defined once; build_tools copies it into TOOL_SCHEMAS each call.
_TOOL_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "levels": {
            "type": "string",
            "description": "Comma-separated severities to include: debug, info, warning, error, critical. "
                           "Default: warning,error,critical (the problems).",
        },
        "view": {
            "type": "string",
            "description": "What to read: 'diagnostics' (default — the server log feed) or "
                           "'tools' (aggregated tool-execution metrics: per-tool call count, "
                           "failure count, success rate, avg/max duration, plus recent failures, "
                           "from the tool_executions table). Use 'tools' for 'which tool is slow / "
                           "failing'.",
        },
        "categories": {
            "type": "string",
            "description": "Comma-separated categories: server (log errors/tracebacks), http "
                           "(4xx/5xx HTTP responses + their cause), access (every HTTP request — "
                           "method/path/status/duration), ws (WebSocket connect/subscribe/"
                           "disconnect), loop (agent-loop pipeline problems), run (run outcomes), "
                           "recovery (self-recovery: stream stalls, frozen/zombie detections, "
                           "resume attempts + outcomes), tool (tool errors). Default: all.",
        },
        "tool_name": {
            "type": "string",
            "description": "With view='tools', restrict metrics to one tool (optional).",
        },
        "session_id": {
            "type": "string",
            "description": "Restrict to one chat session id (optional).",
        },
        "search": {
            "type": "string",
            "description": "Substring match against message / source / detail (optional).",
        },
        "since_minutes": {
            "type": "number",
            "description": "Only records newer than this many minutes (optional; default 120).",
        },
        "limit": {
            "type": "integer",
            "description": "Max records to return (default 40, max 200).",
        },
    },
    "required": [],
}


# Module-level JSON schema for login_as_current_user — same placement rationale
# as _TOOL_PARAMETERS: defined once, copied into TOOL_SCHEMAS by build_tools().
_LOGIN_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "app_url": {
            "type": "string",
            "description": "Origin of the WebAgent app to log into, e.g. "
                           "'http://localhost:8080'. Default: this app's own dev "
                           "origin (PORT env / 8080).",
        },
        "browser_session_id": {
            "type": "string",
            "description": "Optional: a specific shared browser tab id to drive. "
                           "Default: the agent's own shared tab (created if none).",
        },
    },
    "required": [],
}



_DEVELOPMENT_ACTION_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": [
            "open", "navigate", "click", "type", "get_text", "get_html", "screenshot",
            "wait", "evaluate", "title", "url", "close",
        ], "description": "Development-browser action. For every user-facing journey test, capture a final screenshot with screenshot and share that image with the user as evidence of the journey result."},
        "path": {"type": "string", "description": "Relative WebAgent route for open or navigate, e.g. /app?tab=app."},
        "chat_session_id": {"type": "string", "description": "Optional current-user chat session to preload when action is open."},
        "selector": {"type": "string"},
        "text": {"type": "string"},
        "js": {"type": "string"},
        "timeout_ms": {"type": "integer", "minimum": 0, "maximum": 30000, "default": 5000},
        "full_page": {"type": "boolean", "default": True},
    },
    "required": ["action"],
}

_DEVELOPMENT_RECORD_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {"action": {"type": "string", "enum": ["start", "status", "stop"]}},
    "required": ["action"],
}

_DEVELOPMENT_URL_PARAMETERS: Dict[str, Any] = {"type": "object", "properties": {"path": {"type": "string", "description": "Relative WebAgent route, default /app."}, "chat_session_id": {"type": "string", "description": "Optional current-user chat session to preload."}}, "required": []}


def _decode_browser_result(result: Any) -> Dict[str, Any]:
    """Decode the JSON string returned by browser evaluate without raising."""
    try:
        raw = result if isinstance(result, str) else (result or {}).get("result")
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {"value": value}
    except (TypeError, ValueError):
        return {}


def _identity_is_verified(verified: Dict[str, Any], user_id: str) -> bool:
    return bool(
        verified.get("http_status") == 200
        and verified.get("user_id") == user_id
        and verified.get("stored_user_id") == user_id
        and verified.get("active_user_id") == user_id
    )


def _development_path(origin: str, path: str) -> Optional[str]:
    """Accept one same-origin, absolute-path route; reject all external URLs."""
    raw = (path or "").strip()
    parsed = urlsplit(raw)
    if not raw or parsed.scheme or parsed.netloc or not raw.startswith("/") or raw.startswith("//"):
        return None
    return origin.rstrip("/") + raw


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for the diagnostics tool.

    Defines the read_diagnostics handler + helpers inline (closing over the
    injected user_id) and refreshes module-level TOOL_SCHEMAS from the schema
    constant. All heavy imports (logs_store, agent.diagnostics) stay lazy inside
    the handler.
    """

    def _shrink_detail(detail: Any, cap: int = 900) -> Any:
        """Keep details useful but bounded — preserve tracebacks, trim the rest."""
        if detail is None:
            return None
        try:
            s = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        except Exception:
            s = str(detail)
        if len(s) <= cap:
            return detail
        # For a traceback we care most about the tail (the actual exception line).
        return s[: cap - 200] + " …[truncated]… " + s[-200:]

    def _iso_minus(minutes: float) -> Optional[str]:
        try:
            from datetime import datetime, timedelta, timezone
            return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        except Exception:
            return None

    async def _tool_metrics(tool_name: Optional[str], session_id: Optional[str],
                            since_minutes: float, limit: int) -> str:
        """Aggregate the tool_executions table into per-tool stats + recent failures."""
        from app.db.logs_store import get_log_store
        store = get_log_store()
        rows = await store.query_tool_executions(
            tool_name=tool_name or None, session_id=session_id or None,
            since=_iso_minus(since_minutes), limit=max(limit, 500),
        )
        by_tool: Dict[str, Dict[str, Any]] = {}
        failures: List[Dict[str, Any]] = []
        for r in rows:
            name = r.get("tool_name") or "tool"
            s = by_tool.setdefault(name, {"tool": name, "calls": 0, "failures": 0,
                                          "_dur_sum": 0, "_dur_n": 0, "max_ms": 0})
            s["calls"] += 1
            ok = bool(r.get("success"))
            if not ok:
                s["failures"] += 1
                if len(failures) < 20:
                    failures.append({"ts": r.get("ts"), "tool": name,
                                     "error": (r.get("error_message") or "")[:200],
                                     "session_id": r.get("session_id"),
                                     "interaction_id": r.get("interaction_id")})
            d = r.get("duration_ms")
            if isinstance(d, (int, float)):
                s["_dur_sum"] += d
                s["_dur_n"] += 1
                s["max_ms"] = max(s["max_ms"], int(d))
        tools = []
        for s in by_tool.values():
            n = s.pop("_dur_n") or 0
            dur_sum = s.pop("_dur_sum") or 0
            s["avg_ms"] = round(dur_sum / n) if n else None
            s["success_rate"] = round((s["calls"] - s["failures"]) / s["calls"], 3) if s["calls"] else None
            tools.append(s)
        tools.sort(key=lambda x: (x["failures"], x.get("max_ms") or 0), reverse=True)
        out = {
            "status": "ok", "view": "tools",
            "window_minutes": since_minutes, "total_calls": len(rows),
            "tools": tools, "recent_failures": failures,
        }
        if not rows:
            out["hint"] = "No tool executions in this window — widen since_minutes, or no tools have run yet."
        return json.dumps(out, default=str)

    async def read_diagnostics(
        view: str = "diagnostics",
        levels: Optional[str] = None,
        categories: Optional[str] = None,
        tool_name: Optional[str] = None,
        session_id: Optional[str] = None,
        search: Optional[str] = None,
        since_minutes: Optional[float] = None,
        limit: int = 40,
        **_legacy,
    ) -> str:
        """Return recent diagnostic records (or tool metrics) as a JSON string."""
        try:
            try:
                lim = max(1, min(int(limit or 40), 200))
            except Exception:
                lim = 40
            since = since_minutes if since_minutes is not None else 120.0

            if (view or "").strip().lower() in ("tools", "tool", "tool_metrics", "metrics"):
                return await _tool_metrics(tool_name, session_id, since, lim)

            from app.agent.diagnostics import get_recorder
            rec = get_recorder()

            def _csv(v):
                if not v:
                    return None
                parts = [p.strip().lower() for p in str(v).split(",") if p.strip()]
                return parts or None

            lvls = _csv(levels) or ["warning", "error", "critical"]
            cats = _csv(categories)

            records: List[Dict[str, Any]] = await rec.query(
                levels=lvls,
                categories=cats,
                session_id=session_id,
                search=search,
                since_minutes=since,
                limit=lim,
            )

            compact = [{
                "ts": r.get("ts"),
                "level": r.get("level"),
                "category": r.get("category"),
                "source": r.get("source"),
                "message": r.get("message"),
                "session_id": r.get("session_id"),
                "turn_id": r.get("turn_id"),
                "interaction_id": r.get("interaction_id"),
                "session_seq": r.get("session_seq"),
                "agent_id": r.get("agent_id"),
                "instance_id": r.get("instance_id"),
                "detail": _shrink_detail(r.get("detail")),
            } for r in records]

            out = {
                "status": "ok",
                "count": len(compact),
                "filters": {
                    "levels": lvls, "categories": cats, "session_id": session_id,
                    "search": search, "since_minutes": since, "limit": lim,
                },
                "stats": rec.stats(),
                "records": compact,
            }
            if not compact:
                out["hint"] = ("No matching diagnostics. Widen the window (since_minutes), include more "
                               "levels (e.g. levels='info,warning,error'), drop the category filter, or "
                               "try view='tools' for tool timing/failure metrics.")
            return json.dumps(out, default=str)
        except Exception as e:
            logger.warning("read_diagnostics failed: %s", e)
            return json.dumps({"status": "error", "message": str(e)})

    async def login_as_current_user(
        app_url: str = "",
        browser_session_id: str = "",
        **_legacy,
    ) -> str:
        """Mint a session for the CURRENT user and inject it into the browser, so
        the agent can browse the WebAgent app as them — no password involved.

        The JWT is created server-side with the same function the /api/v1/auth/login
        endpoint uses (create_access_token), then written straight into the browser
        page's localStorage.auth_token (the key the whole UI reads). Only the
        OUTCOME is returned (username, user_id, url, verified) — the token itself
        never enters the agent's context.
        """
        try:
            # 1) Resolve the signed-in user. The injected user_id is the identity
            #    this agent already runs as — no privilege escalation possible.
            if not user_id:
                return json.dumps({"status": "error",
                                   "message": "No signed-in user for this run."})
            from app.auth.users import get_user_by_id
            user = await get_user_by_id(user_id)
            if user is None:
                return json.dumps({"status": "error",
                                   "message": f"User {user_id} not found."})

            # 2) Mint the token server-side — same function as the login endpoint.
            from app.auth.jwt import create_access_token
            token = create_access_token(user.username, user.user_id)

            # 3) Resolve the agent's shared browser tab (sharing gate enforced).
            from app.tools.browser import resolve_agent_session, browser_action
            try:
                bs_id = resolve_agent_session(user_id, agent_id or "",
                                              browser_session_id or None,
                                              session_id or None)
            except PermissionError as pe:
                return json.dumps({"status": "error", "message": str(pe)})

            # 4) App origin: caller override, else this app's dev origin.
            if not app_url:
                from app.remote_access.netinfo import get_port
                app_url = f"http://localhost:{get_port()}"
            app_url = app_url.rstrip("/")

            # 5) Open the app, inject the token into localStorage, reload.
            nav = await browser_action(bs_id, action="navigate", url=app_url)
            if not nav.get("success"):
                return json.dumps({"status": "error",
                                   "message": f"Could not open {app_url}: {nav.get('error')}"})
            inj = await browser_action(
                bs_id, action="evaluate",
                js=(
                    f"localStorage.setItem('auth_token', {json.dumps(token)});"
                    f"localStorage.setItem('auth_user_id', {json.dumps(user.user_id)});"
                    f"localStorage.setItem('auth_username', {json.dumps(user.username)});"
                    f"localStorage.setItem('webagent_active_user_id', {json.dumps(user.user_id)});"
                    "localStorage.removeItem('anonUserId');"
                    "localStorage.removeItem('terminalUserId'); 'identity_synchronized'"
                ),
            )
            if not inj.get("success"):
                return json.dumps({"status": "error",
                                   "message": f"Inject failed: {inj.get('error')}"})
            await browser_action(bs_id, action="navigate", url=app_url)

            # 6) Verify as the user WITHOUT returning the token: ask the page to
            #    call /auth/me with the Bearer header from its own localStorage.
            verify_js = (
                "(async () => {"
                "  const t = localStorage.getItem('auth_token') || '';"
                "  const r = await fetch('/api/v1/auth/me', {"
                "    headers: {'Authorization': 'Bearer ' + t}"
                "  });"
                "  const d = await r.json().catch(() => ({}));"
                "  return JSON.stringify({http_status: r.status,"
                "    username: d.username || '', user_id: d.user_id || '',"
                "    stored_user_id: localStorage.getItem('auth_user_id') || '',"
                "    active_user_id: localStorage.getItem('webagent_active_user_id') || ''});"
                "})()"
            )
            v = await browser_action(bs_id, action="evaluate", js=verify_js)
            verified: Dict[str, Any] = {}
            try:
                verified = json.loads((v or {}).get("result") or "{}")
            except Exception:
                verified = {"raw": str((v or {}).get("result"))[:200]}
            ok = (
                verified.get("http_status") == 200
                and verified.get("user_id") == user.user_id
                and verified.get("stored_user_id") == user.user_id
                and verified.get("active_user_id") == user.user_id
            )

            return json.dumps({
                "status": "ok" if ok else "warning",
                "message": ("Browser is now logged in as the signed-in user." if ok
                            else "Token injected but /auth/me did not confirm the "
                                 "login — check app_url / server reachability."),
                "username": user.username,
                "user_id": user.user_id,
                "url": app_url,
                "verified": verified,
                "note": "Token minted server-side and injected into the browser "
                        "session; it was never returned to the agent.",
            }, default=str)
        except Exception as e:
            logger.warning("login_as_current_user failed: %s", e)
            return json.dumps({"status": "error", "message": str(e)})

    def _development_origin() -> str:
        from app.remote_access.netinfo import get_port
        return f"http://localhost:{get_port()}"

    async def _development_url(path: str = "", chat_session_id: str = "") -> Dict[str, Any]:
        target = _development_path(_development_origin(), (path or "/app").strip())
        if not target:
            return {"status": "error", "message": "path must be a same-origin relative route beginning with /."}
        sid = (chat_session_id or "").strip()
        if sid:
            try:
                from app.db import get_db
                await get_db().assert_session_owned(user_id, sid)
            except PermissionError:
                return {"status": "error", "message": "Chat session is not owned by the current user."}
            except Exception:
                logger.warning("development browser session ownership check failed", exc_info=True)
                return {"status": "error", "message": "Could not verify chat-session ownership."}
            # The chat route consumes `session`; `chat_session_id` is the tool's
            # ownership-checked argument, not a UI query-key name.
            target = f"{target}{'&' if '?' in target else '?'}session={quote(sid, safe='')}"
        return {"status": "ok", "origin": _development_origin(), "path": target[len(_development_origin()):], "url": target, "chat_session_id": sid or None}

    async def _verify_development_identity(bs_id: str) -> Dict[str, Any]:
        from app.tools.browser import browser_action
        verify_js = (
            "(async () => {"
            "  const t = localStorage.getItem('auth_token') || '';"
            "  const r = await fetch('/api/v1/auth/me', {headers: {'Authorization': 'Bearer ' + t}});"
            "  const d = await r.json().catch(() => ({}));"
            "  return JSON.stringify({http_status:r.status, username:d.username || '',"
            "user_id:d.user_id || '', stored_user_id:localStorage.getItem('auth_user_id') || '',"
            "active_user_id:localStorage.getItem('webagent_active_user_id') || ''});"
            "})()"
        )
        return _decode_browser_result(await browser_action(bs_id, action="evaluate", js=verify_js))

    async def _open_authenticated_development_browser(path: str = "", chat_session_id: str = "", **_legacy) -> str:
        """Open the authenticated, server-backed browser for this WebAgent development run.

        This is the only browser bootstrap for WebAgent UI journeys. It signs in as
        the current run user, verifies that identity before and after the server
        Chromium handoff, and never returns an access token.
        """
        if not user_id:
            return json.dumps({"status": "error", "message": "No signed-in user for this run."})
        target = await _development_url(path, chat_session_id)
        if target.get("status") != "ok":
            return json.dumps(target)
        try:
            initial = _decode_browser_result(await login_as_current_user())
            if initial.get("status") != "ok":
                return json.dumps({"status": "error", "message": initial.get("message", "Development login failed.")})
            from app.tools.browser import (
                backend_status, browser_action, open_server_browser, resolve_agent_session,
            )
            bs_id = resolve_agent_session(user_id, agent_id or "", None, session_id or None)
            before = await _verify_development_identity(bs_id)
            if not _identity_is_verified(before, user_id):
                return json.dumps({"status": "error", "message": "Development browser identity was not verified before server handoff.", "verified": before})
            switched = await open_server_browser(bs_id)
            if not switched.get("success"):
                return json.dumps({"status": "error", "message": switched.get("message", "Server-backed browser could not start.")})
            status = backend_status(bs_id)
            if status.get("backend") != "server":
                return json.dumps({"status": "error", "message": "Browser did not remain on the server backend."})
            after = await _verify_development_identity(bs_id)
            if not _identity_is_verified(after, user_id):
                return json.dumps({"status": "error", "message": "Development browser identity was not verified after server handoff.", "verified": after})
            navigated = await browser_action(bs_id, action="navigate", url=target["url"])
            if not navigated.get("success"):
                return json.dumps({"status": "error", "message": "Authenticated development browser could not open the requested route."})
            return json.dumps({"status": "ok", "origin": _development_origin(), "backend": "server", "browser_session_id": bs_id, "username": after.get("username", ""), "user_id": user_id, "verified": after, "path": target["path"], "url": target["url"], "chat_session_id": target["chat_session_id"], "note": "Authenticated development browser is ready at the requested deep link; no token was returned."})
        except PermissionError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        except Exception as exc:
            logger.warning("dev_browser_action open failed: %s", exc)
            return json.dumps({"status": "error", "message": "Development browser setup failed."})

    async def _require_development_browser() -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        from app.tools.browser import backend_status, resolve_agent_session
        try:
            bs_id = resolve_agent_session(user_id, agent_id or "", None, session_id or None)
        except PermissionError as exc:
            return None, {"status": "error", "message": str(exc)}
        if backend_status(bs_id).get("backend") != "server":
            return None, {"status": "error", "message": "Call dev_browser_action(action=\"open\") before using development browser tools."}
        verified = await _verify_development_identity(bs_id)
        if not _identity_is_verified(verified, user_id):
            return None, {"status": "error", "message": "Development browser is not authenticated as the current user.", "verified": verified}
        return bs_id, None

    async def dev_browser_url(path: str = "", chat_session_id: str = "", **_legacy) -> str:
        if not user_id:
            return json.dumps({"status": "error", "message": "No signed-in user for this run."})
        return json.dumps(await _development_url(path, chat_session_id))

    async def dev_browser_action(
        action: str,
        path: str = "",
        chat_session_id: str = "",
        selector: str = "",
        text: str = "",
        js: str = "",
        timeout_ms: int = 5000,
        full_page: bool = True,
        **_legacy,
    ) -> str:
        """Act only in the authenticated server-backed WebAgent development browser.

        For every user-facing journey test, capture a final screenshot with
        ``action=\"screenshot\"`` and share that image with the user as evidence
        of the journey result.
        """
        name = (action or "").strip().lower()
        if name not in _DEVELOPMENT_ACTION_PARAMETERS["properties"]["action"]["enum"]:
            return json.dumps({"status": "error", "message": "Unsupported development browser action."})
        if name == "open":
            return await _open_authenticated_development_browser(path, chat_session_id)
        bs_id, error = await _require_development_browser()
        if error:
            return json.dumps(error)
        from app.tools.browser import browser_action
        kwargs: Dict[str, Any] = {
            "action": name, "selector": selector or None, "text": text if name == "type" else None,
            "js": js or None, "timeout_ms": min(max(int(timeout_ms or 0), 0), 30000),
            "full_page": bool(full_page),
        }
        if name == "navigate":
            url = _development_path(_development_origin(), path)
            if not url:
                return json.dumps({"status": "error", "message": "navigate requires a same-origin relative path beginning with /."})
            kwargs["url"] = url
        result = await browser_action(bs_id, **kwargs)
        result["status"] = "ok" if result.get("success") else "error"
        return json.dumps(result, default=str)

    async def dev_browser_record(action: str, **_legacy) -> str:
        """Explicitly start, inspect, or stop the Render Recorder in the development tab."""
        name = (action or "").strip().lower()
        if name not in ("start", "status", "stop"):
            return json.dumps({"status": "error", "message": "action must be start, status, or stop."})
        bs_id, error = await _require_development_browser()
        if error:
            return json.dumps(error)
        from app.tools.browser import browser_action
        state_js = "JSON.stringify(window.__renderRecorderState || {})"
        if name != "status":
            click_js = (
                "(() => { const b=document.querySelector('#render-recorder-toggle');"
                "if (!b) return JSON.stringify({error:'Render Recorder toggle unavailable'});"
                "const s=window.__renderRecorderState || {}; if ((s.enabled===true) !== "
                + ("false" if name == "stop" else "true") + ") b.click();"
                "return JSON.stringify(window.__renderRecorderState || {}); })()"
            )
            changed = _decode_browser_result(await browser_action(bs_id, action="evaluate", js=click_js))
            if changed.get("error"):
                return json.dumps({"status": "error", "message": changed["error"]})
            await browser_action(bs_id, action="wait", timeout_ms=500)
        state = _decode_browser_result(await browser_action(bs_id, action="evaluate", js=state_js))
        if not state:
            return json.dumps({"status": "error", "message": "Render Recorder state could not be read."})
        if name == "start" and state.get("enabled") is not True:
            return json.dumps({"status": "error", "message": "Render Recorder did not start.", "recorder": state})
        if name == "stop" and state.get("enabled") is True:
            return json.dumps({"status": "error", "message": "Render Recorder did not stop.", "recorder": state})
        return json.dumps({"status": "ok", "action": name, "recorder": state})

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "read_diagnostics": _TOOL_PARAMETERS,
        "login_as_current_user": _LOGIN_PARAMETERS,
        "dev_browser_url": _DEVELOPMENT_URL_PARAMETERS,
        "dev_browser_action": _DEVELOPMENT_ACTION_PARAMETERS,
        "dev_browser_record": _DEVELOPMENT_RECORD_PARAMETERS,
    })

    return {
        "read_diagnostics": read_diagnostics,
        "login_as_current_user": login_as_current_user,
        "dev_browser_url": dev_browser_url,
        "dev_browser_action": dev_browser_action,
        "dev_browser_record": dev_browser_record,
    }

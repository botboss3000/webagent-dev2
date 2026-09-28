"""Browser Control ability — SELF-CONTAINED drop-in.

Gates these tools:
  • browser_action — drive a headless Chromium (Playwright) browser. Handler
    still lives in core (``app/tools/browser.py`` — handled in a later wave).
    Wrapped to close over the run's user_id, with the browser session keyed to
    user_id (one persistent browser per user). This exact quirk
    (session_id == user_id) is preserved from the old loader wiring.
  • http_request — arbitrary outbound HTTP. Handler now lives IN THIS FILE
    (relocated out of core so the ability is self-contained), defined at module
    level and returned as-is.
  • web_session_status / web_session_fetch / web_session_graphql — lightweight
    "act AS the user" requests: a plain HTTP/GraphQL call carrying the user's
    cookies, for sites with no public API. Cookies come FIRST from the user's
    LIVE in-app browser login (whatever they've logged into on the Browser page),
    and fall back to cookies the user pasted into this ability's optional
    credentials box. Folded in from the former standalone "Browser Cookies"
    ability — the cookie-replay tools are useless without a browser to log in
    with, so they live with Browser Control now.

Discovered generically by core via three optional module hooks:
  • FEATURE       — catalog + UI + which tool names it gates
  • build_tools() — its tool handlers (app/tools/loader.py injects them)
  • TOOL_SCHEMAS / DESTRUCTIVE — read AFTER build_tools by the loader

Heavy imports stay LAZY (inside the handler bodies / build_tools) so scanning
FEATURE stays cheap.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

_SESSION_HTTP_TIMEOUT = 30.0
_DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# ── Live-events sibling loader ────────────────────────────────────────────────
# This ability is loaded via importlib.util.spec_from_file_location under a dotted
# name whose PARENT packages are not real importable packages (app/abilities load
# files by path, not as a package tree). So a plain `from . import live_events` is
# unreliable — it would resolve `.` to the wrong parent. Load the sibling file by
# PATH instead (with the relative import as a best-effort first try), cache it, and
# treat failure as "no live feed" — every caller guards the result so the agent's
# tool action never breaks just because the visual broadcast couldn't load.
_LIVE_EVENTS_MOD: Any = None


def _load_live_events():
    global _LIVE_EVENTS_MOD
    if _LIVE_EVENTS_MOD is not None:
        return _LIVE_EVENTS_MOD
    try:  # works only if ever loaded as a true package — cheap to try first
        from . import live_events as _m  # type: ignore
        _LIVE_EVENTS_MOD = _m
        return _m
    except Exception:  # noqa: BLE001
        pass
    try:
        import importlib.util as _u
        import os as _os
        _p = _os.path.join(_os.path.dirname(__file__), "live_events.py")
        _spec = _u.spec_from_file_location("browser_control_live_events", _p)
        if _spec and _spec.loader:
            _m = _u.module_from_spec(_spec)
            _spec.loader.exec_module(_m)
            _LIVE_EVENTS_MOD = _m
            return _m
    except Exception:  # noqa: BLE001
        logger.debug("could not load live_events sibling (non-fatal)", exc_info=True)
    return None


# ── HTTP Request tool ────────────────────────────────────────────────────────
# Relocated VERBATIM from app/tools/core_tools.py (it was a browser_control-gated
# tool wired through core; now it lives with its owning ability).

async def http_request(
    method: str = "GET",
    url: str = "",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[Any] = None,
    body_type: str = "json",
    timeout: int = 30,
) -> str:
    """
    Make an outbound HTTP request to any endpoint.

    Supports GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS.
    Body can be JSON (auto-serialized), form data, or raw text.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, PATCH, HEAD, OPTIONS)
        url: Full URL including scheme (e.g. https://api.example.com/data)
        headers: Optional dict of HTTP headers
        body: Request body data (dict for JSON/form, string for raw text)
        body_type: "json", "form", "text" — how to encode body
        timeout: Request timeout in seconds (default 30)

    Returns:
        Formatted response with status code, headers, and body.
    """
    import httpx
    import json as _json
    import time

    method = method.upper()
    valid_methods = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}
    if method not in valid_methods:
        return f"Error: invalid method '{method}'. Use one of: {', '.join(sorted(valid_methods))}"

    if not url:
        return "Error: url is required"

    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"

    prepared_headers = {
        "User-Agent": "WebAgent/1.0",
    }
    if headers:
        prepared_headers.update(headers)

    # Prepare body
    content = None
    if body is not None:
        if body_type == "json":
            if isinstance(body, str):
                try:
                    body = _json.loads(body)
                except _json.JSONDecodeError:
                    pass
            content = _json.dumps(body).encode()
            prepared_headers.setdefault("Content-Type", "application/json")
        elif body_type == "form":
            content = body  # httpx handles dicts directly for data= in POST
        elif body_type == "text":
            content = str(body).encode()
            prepared_headers.setdefault("Content-Type", "text/plain")

    start = time.time()
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            if method == "GET":
                resp = await client.get(url, headers=prepared_headers)
            elif method == "POST":
                if body_type == "form" and isinstance(body, dict):
                    resp = await client.post(url, headers=prepared_headers, data=body)
                else:
                    resp = await client.post(url, headers=prepared_headers, content=content)
            elif method == "PUT":
                resp = await client.put(url, headers=prepared_headers, content=content)
            elif method == "DELETE":
                resp = await client.delete(url, headers=prepared_headers, content=content)
            elif method == "PATCH":
                resp = await client.patch(url, headers=prepared_headers, content=content)
            elif method == "HEAD":
                resp = await client.head(url, headers=prepared_headers)
            elif method == "OPTIONS":
                resp = await client.options(url, headers=prepared_headers)
            else:
                return f"Error: unsupported method {method}"
    except httpx.TimeoutException:
        return f"Error: request to {url} timed out after {timeout}s"
    except httpx.ConnectError as e:
        return f"Error: could not connect to {url} — {e}"
    except Exception as e:
        return f"Error: request failed — {e}"

    duration_ms = int((time.time() - start) * 1000)

    # Truncate large response bodies
    body_text = resp.text
    if len(body_text) > 50000:
        body_text = body_text[:50000] + f"\n[... truncated, full size: {len(body_text)} bytes]"

    # Summarize headers (exclude long or binary ones)
    summary_headers = dict(resp.headers)
    for skip in ("set-cookie", "transfer-encoding", "content-encoding"):
        summary_headers.pop(skip, None)

    lines = [
        f"Status: {resp.status_code}",
        f"Duration: {duration_ms}ms",
        f"Content-Type: {resp.headers.get('content-type', '?')}",
        f"Content-Length: {len(resp.content)} bytes",
        "",
        "Body:",
        body_text,
    ]
    return "\n".join(lines)


# ── Cookie-replay "act as the user" tools ────────────────────────────────────
# Lightweight HTTP/GraphQL calls that carry the user's cookies so the agent can
# act on sites the user is logged into but that expose no API. Cookies are
# sourced LIVE from the user's in-app browser session first (the Browser page
# login), with a pasted-cookie fallback (this ability's credentials block).
# Folded in from the former standalone "Browser Cookies" ability; site-specific
# knowledge (URL templates, GraphQL doc ids, CSRF token names) belongs in the
# admin-editable skill prompt, not here.


def _sess_err(msg: str, **extra) -> str:
    return json.dumps({"status": "error", "message": msg, **extra})


def _host_of(url: str) -> str:
    try:
        return (urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _cookie_domain_matches(cookie_domain: str, host: str) -> bool:
    """A cookie scoped to ``.facebook.com`` is sent to ``www.facebook.com``."""
    d = (cookie_domain or "").lstrip(".").lower()
    if not d or not host:
        return False
    return host == d or host.endswith("." + d)


def _parse_pasted_cookies(blob: str) -> dict:
    """Accept JSON {name:value} or Netscape cookie-jar text → {name: value}."""
    blob = (blob or "").strip()
    if not blob:
        return {}
    if blob.startswith("{"):
        try:
            parsed = json.loads(blob)
            if isinstance(parsed, dict):
                return {str(k): str(v) for k, v in parsed.items()}
        except Exception:
            pass
    out: dict = {}
    for line in blob.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t") if "\t" in line else line.split()
        if len(parts) >= 7:
            out[parts[5]] = parts[6]
        elif "=" in line:
            name, _, value = line.partition("=")
            out[name.strip()] = value.strip().rstrip(";")
    return out


def _looks_like_login_redirect(resp) -> bool:
    """Heuristic: did the upstream bounce us to a login page (stale cookies)?"""
    if resp.status_code in (401, 403):
        return True
    if resp.status_code in (301, 302, 303, 307, 308):
        loc = resp.headers.get("location", "").lower()
        if "login" in loc or "checkpoint" in loc or "signin" in loc:
            return True
    return False


async def _live_cookies_for_url(user_id: str, url: str) -> dict:
    """Cookies from ANY of the user's in-app browser tabs that apply to ``url``'s
    host — fresh from an open tab, else that tab's saved login. {name: value}."""
    host = _host_of(url)
    if not host or not user_id:
        return {}
    try:
        from app.db import browser_sessions_store as _store
        from app.tools.browser import cookies_for_session
    except Exception:
        return {}
    try:
        sessions = _store.list_for_user(user_id)
    except Exception:
        sessions = []
    jar: dict = {}
    for s in sessions:
        bs_id = s.get("id")
        if not bs_id:
            continue
        try:
            for ck in await cookies_for_session(bs_id):
                if ck.get("name") and _cookie_domain_matches(ck.get("domain", ""), host):
                    jar[ck["name"]] = ck.get("value", "")
        except Exception:
            continue
    return jar


async def _pasted_session(user_id: str) -> Optional[dict]:
    """The optional pasted-cookie fallback (this ability's credentials block)."""
    try:
        from app.abilities import credentials as _creds
        sess = await _creds.read_credentials("browser_control", user_id=user_id)
    except Exception as e:  # noqa: BLE001
        logger.debug("read browser_control creds failed: %s", e)
        return None
    if not sess or not (sess.get("cookies") or "").strip():
        return None
    return sess


async def _resolve_session_cookies(user_id: str, url: str):
    """(cookies_dict, source, user_agent). LIVE browser login first; pasted
    cookies as the fallback. source ∈ {"live", "pasted", "none"}."""
    live = await _live_cookies_for_url(user_id, url)
    if live:
        return live, "live", _DEFAULT_UA
    sess = await _pasted_session(user_id)
    if sess:
        ck = _parse_pasted_cookies(sess.get("cookies", ""))
        if ck:
            return ck, "pasted", (sess.get("user_agent") or _DEFAULT_UA)
    return {}, "none", _DEFAULT_UA


_NOT_CONFIGURED_MSG = ("No cookies for this site. Log into it on the Browser page "
                       "(the live session is reused automatically), or paste cookies "
                       "under Browser Control → Credentials.")


async def web_session_status(user_id: str) -> str:
    """Report whether the agent can act as the user, and via what source."""
    live_tabs = 0
    live_total = 0
    try:
        from app.db import browser_sessions_store as _store
        from app.tools.browser import cookies_for_session
        for s in _store.list_for_user(user_id):
            cks = await cookies_for_session(s.get("id"))
            if cks:
                live_tabs += 1
                live_total += len(cks)
    except Exception:
        pass
    pasted = await _pasted_session(user_id)
    info = {
        "status": "ok" if (live_tabs or pasted) else "not_configured",
        "live_sessions_with_cookies": live_tabs,
        "live_cookie_count": live_total,
        "pasted_cookies": bool(pasted),
    }
    if info["status"] == "not_configured":
        info["message"] = _NOT_CONFIGURED_MSG
    return json.dumps(info)


async def web_session_fetch(
    user_id: str,
    url: str,
    method: str = "GET",
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    body: Optional[Any] = None,
    form: Optional[dict] = None,
) -> str:
    """Make an HTTP call carrying the user's cookies (live browser login first,
    pasted cookies as fallback), so the agent acts AS the logged-in user."""
    if not url:
        return _sess_err("url is required")
    cookies, source, user_agent = await _resolve_session_cookies(user_id, url)
    if not cookies:
        return json.dumps({"status": "not_configured", "message": _NOT_CONFIGURED_MSG})

    method = (method or "GET").upper()
    request_headers = {"User-Agent": user_agent, "Accept": "*/*"}
    if headers:
        request_headers.update({str(k): str(v) for k, v in headers.items()})

    try:
        import httpx
        async with httpx.AsyncClient(timeout=_SESSION_HTTP_TIMEOUT, cookies=cookies, follow_redirects=False) as client:
            if form is not None:
                resp = await client.request(method, url, params=params, headers=request_headers, data=form)
            elif isinstance(body, (dict, list)):
                resp = await client.request(method, url, params=params, headers=request_headers, json=body)
            else:
                resp = await client.request(method, url, params=params, headers=request_headers, content=body)
    except Exception as e:
        return _sess_err(f"request failed: {e}")

    if _looks_like_login_redirect(resp):
        return json.dumps({
            "status": "session_expired",
            "cookie_source": source,
            "http_status": resp.status_code,
            "message": "Upstream bounced to login — the session is stale. Re-log-in "
                       "on the Browser page (or refresh pasted cookies).",
        })

    try:
        parsed: Any = resp.json()
    except Exception:
        parsed = resp.text

    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "cookie_source": source,
        "http_status": resp.status_code,
        "headers": dict(resp.headers),
        "body": parsed,
    })


_CSRF_REGEX_CACHE: dict = {}


def _compile_csrf_regex(pattern: str):
    if not pattern:
        return None
    if pattern in _CSRF_REGEX_CACHE:
        return _CSRF_REGEX_CACHE[pattern]
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    _CSRF_REGEX_CACHE[pattern] = compiled
    return compiled


async def web_session_graphql(
    user_id: str,
    url: str,
    doc_id: str = "",
    query: str = "",
    variables: Optional[dict] = None,
    extra_form: Optional[dict] = None,
    csrf_token_url: str = "",
    csrf_token_names: Optional[list] = None,
) -> str:
    """POST a GraphQL-style form to a logged-in page (cookies sourced live-first),
    optionally scraping CSRF tokens from a rendered page first."""
    if not url:
        return _sess_err("url is required")
    if not doc_id and not query:
        return _sess_err("either doc_id or query is required")

    cookies, source, user_agent = await _resolve_session_cookies(user_id, url)
    if not cookies:
        return json.dumps({"status": "not_configured", "message": _NOT_CONFIGURED_MSG})

    headers = {"User-Agent": user_agent, "Accept": "*/*"}

    scraped: dict = {}
    if csrf_token_url and csrf_token_names:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=_SESSION_HTTP_TIMEOUT, cookies=cookies, follow_redirects=True) as client:
                page = await client.get(csrf_token_url, headers=headers)
            if _looks_like_login_redirect(page):
                return json.dumps({"status": "session_expired", "cookie_source": source})
            html = page.text or ""
            for name in csrf_token_names:
                m = re.search(rf'"{re.escape(name)}"\s*:\s*"([^"]+)"', html)
                if m:
                    scraped[name] = m.group(1)
        except Exception as e:
            return _sess_err(f"csrf scrape failed: {e}")

    fields: dict = {}
    if doc_id:
        fields["doc_id"] = doc_id
    if query:
        fields["query"] = query
    if variables is not None:
        fields["variables"] = json.dumps(variables)
    fields.update(scraped)
    if extra_form:
        fields.update({str(k): str(v) for k, v in extra_form.items()})

    try:
        import httpx
        async with httpx.AsyncClient(timeout=_SESSION_HTTP_TIMEOUT, cookies=cookies, follow_redirects=False) as client:
            resp = await client.post(url, data=fields, headers={**headers, "X-Requested-With": "XMLHttpRequest"})
    except Exception as e:
        return _sess_err(f"request failed: {e}")

    if _looks_like_login_redirect(resp):
        return json.dumps({"status": "session_expired", "cookie_source": source, "http_status": resp.status_code})

    try:
        parsed: Any = resp.json()
    except Exception:
        parsed = resp.text

    return json.dumps({
        "status": "ok" if resp.is_success else "error",
        "cookie_source": source,
        "http_status": resp.status_code,
        "scraped_tokens": list(scraped.keys()),
        "body": parsed,
    })


async def cookie_allowlist_tool(user_id: str, action: str = "list", domain: str = "") -> str:
    """List/add/remove sites whose cookies the browser KEEPS across restarts.

    The browser only persists cookies for sites the user actually visits plus the
    user's allowlist (everything else — ad/tracking cookies — is discarded). Call
    this when the user wants a login remembered (add) or forgotten (remove)."""
    from app.tools import cookie_allowlist as ca
    action = (action or "list").lower()
    if action == "add":
        reg = ca.add(user_id, domain)
        if not reg:
            return json.dumps({"status": "error", "message": "Give a domain like 'example.com'."})
        return json.dumps({"status": "ok", "action": "add", "domain": reg,
                           "allowlist": ca.list_for_user(user_id),
                           "message": f"Cookies for {reg} will now be kept across restarts."})
    if action == "remove":
        reg = ca.remove(user_id, domain)
        if not reg:
            return json.dumps({"status": "error", "message": "Give a domain like 'example.com'."})
        return json.dumps({"status": "ok", "action": "remove", "domain": reg,
                           "allowlist": ca.list_for_user(user_id),
                           "message": f"{reg} removed; its cookies are kept only while you actively use it."})
    return json.dumps({
        "status": "ok", "action": "list", "allowlist": ca.list_for_user(user_id),
        "note": "These domains' cookies are always kept. Sites you actively visit are "
                "kept automatically too; ad/tracking cookies are always discarded.",
    })


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: Optional[str] = None, enabled_providers=None,
                **_ctx):
    """Return {tool_name: handler} for browser_action + http_request.

    browser_action is wrapped to resolve the browser SESSION (a persistent,
    shareable tab) this agent may drive — the sharing gate. Without an explicit
    browser_session_id the agent uses (or auto-creates) its own shared tab;
    private/unlinked tabs are unreachable. http_request is the module-level
    handler in this file.
    """
    from app.tools.browser import (
        browser_action as _core_browser_action,
        resolve_agent_session as _resolve_browser_session,
        open_local_browser as _open_local_browser,
        open_server_browser as _open_server_browser,
        use_remote_browser as _use_remote_browser,
        backend_status as _backend_status,
    )

    def _scoped_browser_result(result, bs_id: str):
        """Attach the exact backend/resource scope to every browser observation."""
        if not isinstance(result, dict):
            return result
        scoped = dict(result)
        try:
            status = _backend_status(bs_id)
        except Exception:
            status = {}
        scoped.setdefault("backend", str((status or {}).get("backend") or "headless"))
        scoped.setdefault("browser_session_id", bs_id)
        return scoped

    async def _browser_action_wrapper(
        action: str,
        selector: Optional[str] = None,
        text: Optional[str] = None,
        url: Optional[str] = None,
        js: Optional[str] = None,
        timeout_ms: int = 5000,
        full_page: bool = True,
        browser_session_id: Optional[str] = None,
    ):
        try:
            bs_id = _resolve_browser_session(
                user_id, agent_id or "", browser_session_id, session_id or None,
            )
        except PermissionError as _pe:
            return {"success": False, "error": str(_pe), "url": "", "title": ""}

        # Live "interactive shared browser" feedback. Loaded via the robust sibling
        # loader (the ability isn't a real package, so `from . import` is unsafe);
        # `_live` may be None and EVERY call below is individually guarded, so the
        # broadcast/probe can never raise or alter the tool's return value — the
        # happy path is byte-for-byte preserved.
        _live = _load_live_events()

        # click / type with a target → telegraph the move, act, then confirm.
        if action in ("click", "type") and selector:
            box = None
            desc = None
            try:
                box = await _live.compute_box(bs_id, selector)
            except Exception:  # noqa: BLE001
                box = None
            try:
                desc = await _live.describe_element(bs_id, selector)
            except Exception:  # noqa: BLE001
                desc = None
            if action == "click":
                label = f'Clicking “{desc}”' if desc else f"Clicking {selector}"
            else:
                label = f'Typing into “{desc}”' if desc else f"Typing into {selector}"
            # 1) Telegraph the intent BEFORE the action lands.
            try:
                await _live.publish_browser_event(
                    user_id, bs_id, "telegraph", label, box=box, selector=selector
                )
            except Exception:  # noqa: BLE001
                pass
            # 2) Run the real action.
            result = await _core_browser_action(
                bs_id=bs_id, action=action, selector=selector, text=text,
                url=url, js=js, timeout_ms=timeout_ms, full_page=full_page,
            )
            # 3) Confirm what happened.
            try:
                if action == "click":
                    await _live.publish_browser_event(
                        user_id, bs_id, "click", label, box=box, selector=selector
                    )
                else:
                    await _live.publish_browser_event(
                        user_id, bs_id, "type", label, box=box, text=text, selector=selector
                    )
            except Exception:  # noqa: BLE001
                pass
            return _scoped_browser_result(result, bs_id)

        # navigate → act, then announce the destination.
        if action == "navigate":
            result = await _core_browser_action(
                bs_id=bs_id, action=action, selector=selector, text=text,
                url=url, js=js, timeout_ms=timeout_ms, full_page=full_page,
            )
            try:
                await _live.publish_browser_event(
                    user_id, bs_id, "nav", f"Going to {url}", url=url
                )
            except Exception:  # noqa: BLE001
                pass
            return _scoped_browser_result(result, bs_id)

        # Everything else: run as before (a low-key 'busy' hint for the ticker).
        try:
            await _live.publish_browser_event(
                user_id, bs_id, "busy", f"Running {action}", selector=selector
            )
        except Exception:  # noqa: BLE001
            pass
        result = await _core_browser_action(
            bs_id=bs_id,
            action=action,
            selector=selector,
            text=text,
            url=url,
            js=js,
            timeout_ms=timeout_ms,
            full_page=full_page,
        )

        return _scoped_browser_result(result, bs_id)
    async def _browser_backend_wrapper(

        mode: Optional[str] = None,
        profile: Optional[str] = None,
        browser_session_id: Optional[str] = None,
    ):
        """Switch this session between the in-app headless browser and the REAL
        browser on the user's device (or, with no mode, just report status).

        ``mode="local"`` opens/attaches the device browser and points the session
        at it; ``mode="headless"`` switches back to the in-app browser (carrying
        the login forward). Switching to the device browser is confirmed at chat
        time because it acts in the user's own, logged-in browser."""
        try:
            bs_id = _resolve_browser_session(
                user_id, agent_id or "", browser_session_id, session_id or None,
            )
        except PermissionError as _pe:
            return json.dumps({"success": False, "error": str(_pe)})
        _live = _load_live_events()
        m = (mode or "").lower()
        if m == "local":
            out = await _open_local_browser(bs_id, profile=(profile or "default"), user_id=user_id)
            # Announce the backend swap to the live panel (guarded; never fatal).
            try:
                await _live.publish_browser_event(
                    user_id, bs_id, "backend", "Switched to the on-device browser"
                )
            except Exception:  # noqa: BLE001
                pass
            return json.dumps(out)
        if m == "server":
            out = await _open_server_browser(bs_id)
            try:
                await _live.publish_browser_event(
                    user_id, bs_id, "backend", "Switched to headed server Chromium"
                )
            except Exception:  # noqa: BLE001
                pass
            return json.dumps(out)
        if m == "headless":
            out = await _use_remote_browser(bs_id)
            try:
                await _live.publish_browser_event(
                    user_id, bs_id, "backend", "Switched back to the in-app browser"
                )
            except Exception:  # noqa: BLE001
                pass
            return json.dumps(out)
        # No mode → read-only status.
        return json.dumps(_backend_status(bs_id))

    _uid = user_id

    async def _vault_login_wrapper(
        login_url: Optional[str] = None,
        email_selector: Optional[str] = None,
        password_selector: Optional[str] = None,
        submit_selector: Optional[str] = None,
        success_selector: Optional[str] = None,
        ability: str = "browser_control",
        browser_session_id: Optional[str] = None,
    ):
        """Log into a website using credentials saved in the encrypted vault, without ever seeing them. The email + password are read server-side from the vault and typed into the page for you; only the outcome (logged in / needs 2FA) is returned. First check_credential / web_session_status to confirm a login is saved. Provide login_url and CSS selectors for the email, password and submit fields (defaults cover common forms incl. Facebook). If the result says needs_2fa, ask the user to finish signing in on the Browser page."""
        from app.abilities import credentials as _creds
        try:
            sess = await _creds.read_credentials(ability or "browser_control", user_id=_uid)
        except Exception as e:  # noqa: BLE001
            return json.dumps({"status": "error", "message": f"Could not read saved credentials: {e}"})
        email = (sess or {}).get("login_email") or ""
        password = (sess or {}).get("login_password") or ""
        if not email or not password:
            return json.dumps({
                "status": "no_credentials",
                "message": ("No saved login for this site. Ask the user to enter their email and "
                            "password in the dashboard login form (it writes straight to the vault), "
                            "or to sign in on the Browser page."),
            })
        from app.tools.browser import vault_login as _vault_login, resolve_agent_session as _ras
        try:
            bs_id = _ras(
                _uid, agent_id or "", browser_session_id, session_id or None,
            )
        except PermissionError as _pe:
            return json.dumps({"status": "error", "message": str(_pe)})
        result = await _vault_login(
            bs_id,
            email=email,
            password=password,
            login_url=login_url,
            email_selector=email_selector,
            password_selector=password_selector,
            submit_selector=submit_selector,
            success_selector=success_selector,
        )
        # result carries no secret — outcome only.
        result["status"] = "ok"
        return json.dumps(result)

    async def _session_status(**kw):
        return await web_session_status(_uid)

    async def _session_fetch(**kw):
        return await web_session_fetch(_uid, **kw)

    async def _session_graphql(**kw):
        return await web_session_graphql(_uid, **kw)

    async def _cookie_allowlist(**kw):
        return await cookie_allowlist_tool(_uid, **kw)

    async def _browser_popup_wrapper(
        mode: str = "open",
        url: Optional[str] = None,
        title: Optional[str] = None,
        browser_session_id: Optional[str] = None,
    ) -> str:
        """Show the user a web page in a floating in-app browser window (or close it).

        Use this to hand a page to the USER to look at or interact with — "have a
        look at this", "check this site", "confirm this looks right" — rather than
        just reading it yourself. mode='open' pops a small draggable browser window
        on their screen showing `url`; mode='close' dismisses it. The page renders
        through the in-app browser proxy (the same Playwright-backed view as the
        Browser page), so sites that refuse plain <iframe> embedding still display.
        The user can always close the window themselves. Requires a live chat
        session on screen (not available on background / event-triggered runs).
        """
        m = (mode or "open").lower()
        if not session_id:
            return ("Can't show a browser popup: this run has no live UI session "
                    "attached (it may be a background or event-triggered run).")

        if m == "close":
            event = {"type": "ui_command", "action": "browser_popup", "mode": "close"}
            try:
                from app.api.chat import _emit_to_visualizers
                await _emit_to_visualizers(session_id, event, user_id=user_id)
            except Exception as e:  # noqa: BLE001 — reaching the screen is best-effort
                return f"Couldn't reach the user's screen to close the popup: {e}"
            return "Closed the browser popup on the user's screen."

        # mode == "open"
        if not url:
            return "Provide a url to show in the popup (for mode='open')."
        # Light scheme fix so the header's "open in a real tab" link is absolute;
        # the proxy itself normalises whatever it receives.
        target = url.strip()
        if target and not re.match(r"^[a-z][a-z0-9+.-]*://", target, re.IGNORECASE):
            if " " not in target and "." in target.split("/", 1)[0]:
                target = "https://" + target

        try:
            bs_id = _resolve_browser_session(
                user_id, agent_id or "", browser_session_id, session_id or None,
            )
        except PermissionError as _pe:
            return f"Can't open the browser popup: {_pe}"

        event = {
            "type": "ui_command",
            "action": "browser_popup",
            "mode": "open",
            "url": target,
            "title": title or "",
            "bs_id": bs_id,
        }
        try:
            from app.api.chat import _emit_to_visualizers
            await _emit_to_visualizers(session_id, event, user_id=user_id)
        except Exception as e:  # noqa: BLE001
            return f"Couldn't reach the user's screen to open the popup: {e}"
        return (f"Opened a browser popup on the user's screen showing {target}. "
                "They can look at it and close it when done (or ask me to close it).")

    return {
        "browser_action": _browser_action_wrapper,
        "browser_backend": _browser_backend_wrapper,
        "http_request": http_request,
        "web_session_status": _session_status,
        "web_session_fetch": _session_fetch,
        "web_session_graphql": _session_graphql,
        "vault_login": _vault_login_wrapper,
        "cookie_allowlist": _cookie_allowlist,
        "browser_popup": _browser_popup_wrapper,
    }


# Tools that write / act AS the user → confirm in Ask/Plan mode.
#  • web_session_fetch / web_session_graphql — cookie-driven fetches that mutate
#    remote state and act AS the user.
#  • browser_backend — switching to "local" opens the user's own logged-in
#    browser on their device, so the request is confirmed in chat.
#  • browser_action — drives the live tab; can click/type/run-JS AS the user.
#  • http_request — can POST/PUT/DELETE to any URL.
#  • vault_login — logs into a site with the user's stored credentials.
# The DUAL-USE tools (browser_action, http_request) read AND write, so the agent
# loop exempts their read-only invocations at runtime (navigating/reading a page,
# GET/HEAD/OPTIONS requests) — only the acting/writing uses pause. See loop.py
# `_is_safe_browser_action` / `_is_safe_http_request`.
DESTRUCTIVE = {
    "web_session_fetch", "web_session_graphql", "browser_backend",
    "browser_action", "http_request", "vault_login",
}

TOOL_SCHEMAS = {
    "browser_action": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["navigate", "click", "type", "get_text", "get_html", "screenshot", "wait", "evaluate", "title", "url", "close"],
                "description": (
                    "Browser action to perform. Treat the live browser as leased: "
                    "use 'close' as the final browser step once research is done, "
                    "including best-effort cleanup after errors."
                ),
            },
            "selector": {"type": "string", "description": "CSS selector (for click, type, get_text)"},
            "text": {"type": "string", "description": "Text to type (for type action)"},
            "url": {"type": "string", "description": "URL to navigate to (for navigate action)"},
            "js": {"type": "string", "description": "JavaScript code (for evaluate action)"},
            "timeout_ms": {"type": "integer", "description": "Wait timeout in ms (default 5000)", "default": 5000},
            "full_page": {"type": "boolean", "description": "Full page screenshot", "default": True},
            "browser_session_id": {"type": "string", "description": "Optional browser tab to act on. It must be shared with this agent and belong to this chat. Omit to use this chat's isolated browser tab."},
        },
        "required": ["action"],
    },
    "browser_backend": {
        "type": "object",
        "properties": {
            "mode": {
                "type": "string",
                "enum": ["local", "server", "headless"],
                "description": "Where to run this session's browser. 'local' = open/attach the REAL "
                               "browser on the user's device and drive that (it opens where the "
                               "session was; the user can watch and take over). 'server' = headed Chromium on the development server's virtual display. 'headless' = switch "
                               "back to the in-app browser (the login carries forward). Omit to just "
                               "report the current backend + whether the device can run a local browser.",
            },
            "profile": {
                "type": "string",
                "enum": ["default", "dedicated"],
                "description": "For mode='local': 'default' uses the user's everyday Chrome profile "
                               "(their real logins); 'dedicated' uses a clean app-owned profile. "
                               "Default 'default'.",
                "default": "default",
            },
            "browser_session_id": {"type": "string", "description": "Optional tab to switch. Omit to use the agent's own shared tab."},
        },
        "required": [],
    },
    "http_request": {
        "type": "object",
        "properties": {
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"],
                "description": "HTTP method",
                "default": "GET",
            },
            "url": {"type": "string", "description": "Full URL including scheme (e.g. https://api.example.com/data)"},
            "headers": {
                "type": "object",
                "description": "Optional dict of HTTP headers",
                "additionalProperties": {"type": "string"},
                "default": {},
            },
            "body": {
                "type": "object",
                "description": "Request body (dict for JSON/form, string for text)",
                "default": {},
            },
            "body_type": {
                "type": "string",
                "enum": ["json", "form", "text"],
                "description": "How to encode body",
                "default": "json",
            },
            "timeout": {
                "type": "integer",
                "description": "Request timeout in seconds",
                "default": 30,
            },
        },
        "required": ["url"],
    },
    "web_session_status": {"type": "object", "properties": {}, "required": []},
    "vault_login": {
        "type": "object",
        "properties": {
            "login_url":         {"type": "string", "description": "URL of the login page to open first (e.g. https://www.facebook.com/login)."},
            "email_selector":    {"type": "string", "description": "CSS selector for the email/username field. Default covers common forms incl. Facebook (#email)."},
            "password_selector": {"type": "string", "description": "CSS selector for the password field. Default covers common forms incl. Facebook (#pass)."},
            "submit_selector":   {"type": "string", "description": "CSS selector for the submit/login button. Default covers common forms."},
            "success_selector":  {"type": "string", "description": "Optional CSS selector that is only present once logged in (e.g. a profile menu) — used to confirm success."},
            "ability":           {"type": "string", "description": "Which vault credential set to use (default 'browser_control').", "default": "browser_control"},
            "browser_session_id":{"type": "string", "description": "Optional browser tab to log into. Omit to use the agent's own shared tab."},
        },
        "required": [],
    },
    "web_session_fetch": {
        "type": "object",
        "properties": {
            "url":     {"type": "string", "description": "Full URL of the request."},
            "method":  {"type": "string", "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"], "default": "GET"},
            "params":  {"type": "object", "description": "Optional query-string parameters."},
            "headers": {"type": "object", "description": "Optional extra headers."},
            "body":    {"description": "Optional request body (string or dict → JSON)."},
            "form":    {"type": "object", "description": "Optional url-encoded form body. Mutually exclusive with body."},
        },
        "required": ["url"],
    },
    "web_session_graphql": {
        "type": "object",
        "properties": {
            "url":              {"type": "string", "description": "GraphQL endpoint URL (e.g. https://www.facebook.com/api/graphql/)."},
            "doc_id":           {"type": "string", "description": "Persisted query id (preferred when available).", "default": ""},
            "query":            {"type": "string", "description": "GraphQL query string (only when no doc_id).", "default": ""},
            "variables":        {"type": "object", "description": "Query variables (serialized to JSON)."},
            "extra_form":       {"type": "object", "description": "Additional form fields to send (e.g. fb_api_caller_class)."},
            "csrf_token_url":   {"type": "string", "description": "URL to GET first to scrape CSRF tokens from the HTML.", "default": ""},
            "csrf_token_names": {"type": "array", "items": {"type": "string"},
                                  "description": "Names of JSON-embedded tokens to scrape (e.g. ['fb_dtsg', 'lsd'])."},
        },
        "required": ["url"],
    },
    "cookie_allowlist": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "add", "remove"], "default": "list",
                        "description": "list = show kept sites; add/remove = manage them."},
            "domain": {"type": "string", "default": "",
                        "description": "Site to keep/forget cookies for, e.g. 'example.com' (for add/remove)."},
        },
        "required": [],
    },
    "browser_popup": {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["open", "close"], "default": "open",
                      "description": "'open' shows a floating browser window on the user's screen at `url`; "
                                     "'close' dismisses it."},
            "url": {"type": "string",
                     "description": "The page to show the user (required for mode='open'). Rendered through the "
                                    "in-app browser proxy, so sites that block plain embedding still display."},
            "title": {"type": "string",
                       "description": "Optional short header label for the popup (e.g. the site name or what "
                                      "you want them to check)."},
            "browser_session_id": {"type": "string",
                                    "description": "Optional browser tab to render through. Omit to use the "
                                                   "agent's own shared tab."},
        },
        "required": [],
    },
}

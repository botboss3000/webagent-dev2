"""
REST API for the Gen UI workspace.

Endpoints:
  GET    /api/v1/genui                            — list genui for a user
  POST   /api/v1/genui                            — create a new genui
  DELETE /api/v1/genui/{slug}                     — delete a genui
  GET    /api/v1/genui/{user_id}/{slug}/html      — serve a genui's HTML
  POST   /api/v1/genui/{user_id}/{slug}/logs      — record a genui's own console output
  GET    /api/v1/genui/{user_id}/{slug}/logs      — read back a genui's console output
  GET    /api/v1/genui/{slug}/widget              — read a genui's widget config
  PATCH  /api/v1/genui/{slug}/widget              — write a genui's widget config

The HTML is fetched as text by the Gen UI tab (ui/main-panel/genui/js/genui.js)
and grafted into the app inside a shadow root (first-class rendering) — it is no
longer loaded into a sandboxed iframe.

Split-file convention (see _inline_genui_assets): a genui folder may keep its
source in small files — index.html (markup) + styles.css (styling) + app.js
(logic) + data.json (content). For larger scripts the single app.js may instead
be split into ordered parts app.<nn>-<name>.js (e.g. app.01-icons.js, app.02-data.js)
that the serve route concatenates inside one IIFE. The serve route below inlines
styles.css into <head> and the script(s) before </body>, so authors edit small
files while the browser still receives one document. Genui without those files
serve exactly as stored.
"""
import ipaddress
import json
import os
import socket
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

from fastapi import APIRouter, HTTPException, Query, Header, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from typing import Optional

from app.visualizer.genui import (
    list_genui,
    create_genui,
    delete_genui,
    rename_genui,
    get_genui_html,
    get_genui_data,
    save_genui_data,
    get_genui_widget,
    save_genui_widget,
    append_genui_logs,
    read_genui_logs,
)
from app.auth.identity import caller_may_access_page, assert_caller_is
from app.genui_store.common import genui_dir

router = APIRouter(prefix="/api/v1/genui", tags=["genui"])


async def _require_genui_access(request: Request, user_id: str) -> str:
    """Gate EVERY genui endpoint server-side (a genui runs agent-authored code
    with the viewer's own app trust, so the tab being hidden in the UI is not
    enough — a direct API call must be refused too). Two checks:

      1. The Gen UI page's VISIBILITY (set by the admin) must permit this caller.
         Registration is required by default, so an anonymous visitor is refused
         unless the admin opened Gen UI to "all"; "off" is admins-only. Admins and
         'open' single-user/local mode always pass.
      2. A caller may only read/write their OWN genui — admins may act for any
         user (assert_caller_is enforces caller == user_id, or admin).

    Returns the verified caller user_id. Raises 401 (no identity) / 403 (excluded
    or not the owner)."""
    if not await caller_may_access_page(request, "main", "genui"):
        raise HTTPException(status_code=403, detail="Gen UI isn't enabled for your account.")
    return await assert_caller_is(request, user_id)


# Slim themed scrollbar injected into genui pages. Harmless under first-class
# rendering (it becomes one of the <style> nodes lifted into the shadow root, and
# the client also injects its own scrollbar style), and it keeps a genui opened
# standalone in a browser tab from showing the OS default scrollbar. Uses
# prefers-color-scheme so it adapts to the theme (orange in light docs, blue in dark).
_SCROLLBAR_STYLE = """<style id="webagent-scrollbar-inject">
*{scrollbar-width:thin;scrollbar-color:rgba(125,207,255,0.32) transparent;}
*::-webkit-scrollbar{width:6px;height:6px;}
*::-webkit-scrollbar-track{background:transparent;}
*::-webkit-scrollbar-thumb{background:rgba(125,207,255,0.38);border-radius:999px;}
*::-webkit-scrollbar-thumb:hover{background:rgba(125,207,255,0.62);}
*::-webkit-scrollbar-corner{background:transparent;}
@media (prefers-color-scheme: light){
*{scrollbar-color:rgba(255,140,66,0.40) transparent;}
*::-webkit-scrollbar-thumb{background:rgba(255,140,66,0.48);}
*::-webkit-scrollbar-thumb:hover{background:rgba(255,140,66,0.72);}
}
</style>"""


def _inject_scrollbar_style(html: str) -> str:
    """Inject a slim themed scrollbar stylesheet into the genui page so a
    standalone view doesn't render the OS default scrollbar. Inserts right
    after <head> when present, otherwise prepends."""
    if not html:
        return html
    lower = html.lower()
    idx = lower.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        return html[:insert_at] + _SCROLLBAR_STYLE + html[insert_at:]
    return _SCROLLBAR_STYLE + html


def _inject_genui_data(html: str, data: Optional[dict]) -> str:
    """Bake a genui's data bag into the served page as ``window.__GENUI_DATA``.

    This is the trick that makes a separate data file feel instant: the page's
    content lives in data.json (so the agent edits data without touching the
    markup), but it's merged into the HTML right before sending — so the page
    arrives with its data already present (no second fetch, no loading state).
    The genui reads it via the mount toolbox's ``api.getData()``. Inserted as
    the FIRST thing after <head> so it runs before the genui's own scripts.
    A genui with no data file gets ``{}`` (harmless; the page falls back)."""
    if not html:
        return html
    # Escape "<" so a value containing "</script>" can't break out of the tag.
    payload = json.dumps(data if isinstance(data, dict) else {}).replace("<", "\\u003c")
    # Set the legacy `__CANVAS_DATA` alias too (Canvas → Gen UI rename) so a page
    # body authored under the old name still reads its data directly.
    block = ('<script id="webagent-genui-data">window.__GENUI_DATA='
             'window.__CANVAS_DATA=' + payload + ';</script>')
    lower = html.lower()
    idx = lower.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        return html[:insert_at] + block + html[insert_at:]
    return block + html


def _inject_genui_widget(html: str, widget: Optional[dict]) -> str:
    """Bake a genui's widget config into the served page as
    ``window.__GENUI_WIDGET``.

    Mirrors _inject_genui_data for the page's launcher/widget config (the
    per-page widget.json: which agent the page's chat launcher opens, icon,
    corner buttons, widget options). The Gen UI tab's loader reads it after
    mount and mounts the page's chat launcher from it — no second fetch. A page
    with no widget config gets ``null`` (the tab mounts no page launcher)."""
    if not html:
        return html
    payload = json.dumps(widget if isinstance(widget, dict) else None).replace("<", "\\u003c")
    block = '<script id="webagent-genui-widget">window.__GENUI_WIDGET=' + payload + ';</script>'
    lower = html.lower()
    idx = lower.find("<head>")
    if idx != -1:
        insert_at = idx + len("<head>")
        return html[:insert_at] + block + html[insert_at:]
    return block + html


def _inline_genui_assets(user_id: str, slug: str, html: str) -> str:
    """Inline a genui's sibling ``styles.css`` / ``app.js`` into the served page.

    Split-file convention (filesystem store): a genui folder may keep its source
    in small files — ``index.html`` (markup only), ``styles.css`` (all styling),
    ``app.js`` (all logic) — beside the existing ``data.json`` / ``page.json``.
    This injects ``styles.css`` into <head> and ``app.js`` before </body> so the
    browser still receives ONE document (first-class shadow-root rendering has no
    per-file fetch), while authors keep tidy, small files. Optional: a genui
    without them serves index.html exactly as stored (prior behaviour preserved)."""
    if not html:
        return html
    folder = genui_dir(user_id, slug)
    out = html
    css_path = os.path.join(folder, "styles.css")
    if os.path.isfile(css_path):
        with open(css_path, "r", encoding="utf-8") as f:
            css = f.read()
        style = '\n<style id="webagent-genui-styles">\n' + css + "\n</style>"
        # rfind: the REAL closing head tag is the last occurrence — an HTML
        # comment above it may legitimately mention the tag name in prose.
        idx = out.lower().rfind("</head>")
        if idx != -1:
            out = out[:idx] + style + "\n" + out[idx:]
        else:
            out += style
    # JavaScript: a single `app.js` (inlined verbatim — it may be a standalone
    # script, e.g. its own IIFE) OR ordered parts `app.<nn>-<name>.js`
    # (concatenated in filename order inside ONE IIFE so every part shares a
    # single scope without leaking globals onto the host page — genui scripts
    # run in the app's global scope). `app.js` takes precedence when both exist,
    # so dropping a monolith back into place is the escape hatch.
    js_path = os.path.join(folder, "app.js")
    js_parts = []
    if os.path.isfile(js_path):
        js_parts = [js_path]
    else:
        try:
            js_parts = sorted(
                os.path.join(folder, name)
                for name in os.listdir(folder)
                if name.startswith("app.") and name.endswith(".js")
            )
        except OSError:
            js_parts = []
    if js_parts:
        if len(js_parts) == 1 and os.path.basename(js_parts[0]) == "app.js":
            with open(js_parts[0], "r", encoding="utf-8") as f:
                js = f.read()
            script = '\n<script id="webagent-genui-app">\n' + js + "\n</script>"
        else:
            bodies = []
            for p in js_parts:
                with open(p, "r", encoding="utf-8") as f:
                    bodies.append(f.read())
            script = ('\n<script id="webagent-genui-app">\n(function(){\n'
                      "'use strict';\n" + "\n".join(bodies) + "\n})();\n</script>")
        idx = out.lower().rfind("</body>")
        if idx != -1:
            out = out[:idx] + script + "\n" + out[idx:]
        else:
            out += script
    return out


class CreateGenuiRequest(BaseModel):
    user_id: str
    slug: str
    title: str
    agent_context: Optional[str] = ""
    initial_html: Optional[str] = ""
    # ── REQUIRED session contract (see _build_session_config for rules) ──
    # Every genui must declare how its actions/chat target agent sessions:
    # the deployed session's title + the new-session behaviour. This is
    # enforced at create time so the page never dispatches into an unnamed
    # auto-titled session.
    session_target_name: str
    session_mode: str  # "new_reuse" | "new_each" | "existing"
    session_id: Optional[str] = None  # required iff session_mode == "existing"


class RenameGenuiRequest(BaseModel):
    title: str
    # Optional session-contract update (PATCH may change title, config, or both).
    session_target_name: Optional[str] = None
    session_mode: Optional[str] = None
    session_id: Optional[str] = None


_SESSION_MODES = ("new_reuse", "new_each", "existing")


def _build_session_config(target_name: Optional[str], mode: Optional[str],
                          session_id: Optional[str]) -> Optional[dict]:
    """Validate + assemble the genui session contract.

    Rules (mirrored by the create_genui agent tool):
      - target_name is REQUIRED — the deployed session's title. Missing or
        blank raises ValueError (this is the anti-confusing-names gate).
      - mode is REQUIRED and must be one of new_reuse / new_each / existing.
      - session_id is REQUIRED iff mode == "existing".
    Returns the config dict, or None when nothing was supplied (PATCH with no
    session fields).
    """
    if target_name is None and mode is None and session_id is None:
        return None
    name = (target_name or "").strip()
    if not name:
        raise ValueError("session_target_name is required — the deployed session must have a title.")
    if not mode or mode not in _SESSION_MODES:
        raise ValueError("session_mode is required and must be one of: new_reuse, new_each, existing.")
    cfg: dict = {"target_name": name, "mode": mode}
    if mode == "existing":
        sid = (session_id or "").strip()
        if not sid:
            raise ValueError("session_id is required when session_mode is 'existing'.")
        cfg["session_id"] = sid
    return cfg


@router.get("")
async def api_list_genui(request: Request, user_id: str = Query(..., description="User ID")):
    """Return all genui for the given user. Seeds the home genui if missing."""
    await _require_genui_access(request, user_id)
    pages = await list_genui(user_id)
    return {"status": "ok", "genui": pages, "count": len(pages)}


@router.post("")
async def api_create_genui(request: Request, body: CreateGenuiRequest):
    """Create a new genui for the user."""
    await _require_genui_access(request, body.user_id)
    # REQUIRED session contract — a genui cannot be created without declaring
    # its session target name + new-session behaviour (see _build_session_config).
    try:
        session_config = _build_session_config(
            body.session_target_name, body.session_mode, body.session_id
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    try:
        entry = await create_genui(
            user_id=body.user_id,
            slug=body.slug,
            title=body.title,
            agent_context=body.agent_context or "",
            initial_html=body.initial_html or "",
            session_config=session_config,
        )
        return {"status": "ok", "genui": entry}
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@router.delete("/{slug}")
async def api_delete_genui(request: Request, slug: str, user_id: str = Query(..., description="User ID")):
    """Delete a genui. The home genui cannot be deleted."""
    await _require_genui_access(request, user_id)
    if slug == "home":
        raise HTTPException(status_code=403, detail="The home genui cannot be deleted.")
    ok = await delete_genui(user_id=user_id, slug=slug)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Gen UI '{slug}' not found.")
    return {"status": "ok", "message": f"Gen UI '{slug}' deleted."}


@router.patch("/{slug}")
async def api_rename_genui(
    request: Request,
    slug: str,
    body: RenameGenuiRequest,
    user_id: str = Query(..., description="User ID"),
):
    """Rename a page's display title and/or update its session contract.
    The slug (URL) is preserved."""
    await _require_genui_access(request, user_id)
    new_title = (body.title or "").strip()
    if not new_title:
        raise HTTPException(status_code=400, detail="Title cannot be empty.")
    try:
        session_config = _build_session_config(
            body.session_target_name, body.session_mode, body.session_id
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    ok = await rename_genui(user_id=user_id, slug=slug, new_title=new_title,
                            session_config=session_config)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Gen UI '{slug}' not found.")
    return {"status": "ok", "title": new_title, "session_config": session_config}


class SaveGenuiWidgetRequest(BaseModel):
    widget: dict


@router.get("/{slug}/widget")
async def api_get_genui_widget(
    request: Request,
    slug: str,
    user_id: str = Query(..., description="User ID"),
):
    """Read a genui's widget config (the page's launcher/widget.json options:
    which agent the page's chat launcher opens, icon, corner buttons, widget
    options). Returns ``null`` when the page has no widget config. Gated like
    every genui endpoint (visibility + ownership)."""
    await _require_genui_access(request, user_id)
    if slug == "home":
        await list_genui(user_id)
    return {"status": "ok", "slug": slug, "widget": await get_genui_widget(user_id, slug)}


@router.patch("/{slug}/widget")
async def api_save_genui_widget(
    request: Request,
    slug: str,
    body: SaveGenuiWidgetRequest,
    user_id: str = Query(..., description="User ID"),
):
    """Write a genui's widget config (the page's launcher/widget.json options).
    The Gen UI tab reads it on next load (baked into the served HTML as
    window.__GENUI_WIDGET) and mounts the page's chat launcher from it — so the
    design agent edits the page's launcher with no index.html rewrite. To
    REMOVE a page's launcher, PATCH with an empty object ``{}``."""
    await _require_genui_access(request, user_id)
    widget = body.widget if isinstance(body.widget, dict) else {}
    await save_genui_widget(user_id=user_id, slug=slug, widget=widget)
    return {"status": "ok", "slug": slug, "widget": widget}


class SaveGenuiDataRequest(BaseModel):
    """A genui page POSTing its own data bag for persistence."""
    data: dict


@router.post("/{slug}/data")
async def api_save_genui_data(
    request: Request,
    slug: str,
    body: SaveGenuiDataRequest,
    user_id: str = Query(..., description="User ID"),
):
    """Persist a genui page's in-memory state to its data.json.

    The page calls this to keep QA status, plan state, and other interactive
    state durable across refreshes. The agent also writes through the visualizer
    tool set_genui_data — both paths converge on the same backing file."""
    await _require_genui_access(request, user_id)
    data = body.data if isinstance(body.data, dict) else {}
    await save_genui_data(user_id=user_id, slug=slug, data=data)
    return {"status": "ok", "slug": slug}


@router.get("/{slug}/data")
async def api_get_genui_data(
    request: Request,
    slug: str,
    user_id: str = Query(..., description="User ID"),
):
    """Read a genui page's data bag so it can poll for live updates.

    The page calls this to see fresh QA status, plan state, and other
    interactive state written by an agent or by another browser tab.
    Returns {} when the genui has no data file yet."""
    await _require_genui_access(request, user_id)
    data = await get_genui_data(user_id=user_id, slug=slug)
    return {"status": "ok", "slug": slug, "data": data or {}}


@router.get("/{user_id}/{slug}/html", response_class=HTMLResponse)
async def api_get_genui_html(request: Request, user_id: str, slug: str):
    """Serve a genui's HTML body. Works the same across all GenuiStore
    backends — filesystem, database, or hybrid. Gated: the caller must be
    permitted Gen UI by its visibility setting AND own this genui (or be admin),
    so one user can't fetch another's genui by guessing user_id + slug."""
    await _require_genui_access(request, user_id)
    # Seed the home genui on first request through this endpoint, mirroring
    # the behavior of /api/v1/genui so a direct deep-link to /home/html
    # doesn't 404 a new user.
    if slug == "home":
        await list_genui(user_id)
    html = await get_genui_html(user_id, slug)
    if html is None:
        raise HTTPException(status_code=404, detail=f"Gen UI '{slug}' not found.")
    html = _inline_genui_assets(user_id, slug, html)
    data = await get_genui_data(user_id, slug)
    widget = await get_genui_widget(user_id, slug)
    return HTMLResponse(content=_inject_genui_widget(
        _inject_genui_data(_inject_scrollbar_style(html), data), widget))


@router.post("/{user_id}/{slug}/logs")
async def api_append_genui_logs(request: Request, user_id: str, slug: str, body: dict):
    """Record the console output a genui produced in the browser.

    The Gen UI tab captures each page's own console.log/warn/error (and uncaught
    script errors) as it runs and POSTs them here in small batches; we append them
    to that genui's page-scoped log file (beside index.html) — NOT the global
    logs.db. This is what lets the design agent read the errors of the one genui
    it built via get_genui_logs, with no codebase-admin access. Bounded: at most
    a couple hundred entries per call, then the file itself is capped."""
    await _require_genui_access(request, user_id)
    entries = body.get("entries") if isinstance(body, dict) else None
    if not isinstance(entries, list):
        entries = []
    kept = append_genui_logs(user_id, slug, entries[:200])
    return {"status": "ok", "received": len(entries), "kept": kept}


@router.get("/{user_id}/{slug}/logs")
async def api_read_genui_logs(
    request: Request,
    user_id: str,
    slug: str,
    limit: int = Query(100, ge=1, le=500),
    level: Optional[str] = Query(None, description="Filter to one level (error/warn/log/info/debug)"),
):
    """Read back a genui's recorded console output (newest last). Mirrors what the
    get_genui_logs agent tool returns, for the UI / debugging."""
    await _require_genui_access(request, user_id)
    logs = read_genui_logs(user_id, slug, limit=limit, level=level)
    return {"status": "ok", "slug": slug, "count": len(logs), "logs": logs}


# ─────────────────────────────────────────────────────────────────────────────
# Vault Capture — agent-defined credentials for genui (see
# app/abilities/vault_store.py + the Visualizer ability's request_credential /
# list_vault_keys tools). Nested under the genui router so no new core router
# include is needed. THE SECURITY MODEL:
#   • The agent only ever holds a key id (it reserves the key server-side via the
#     tool). It never sees the secret.
#   • The user types the secret into the secure chat card, which POSTs it STRAIGHT
#     here (save) — it never round-trips through the agent or the transcript.
#   • A genui uses the key by id through /vault/proxy: the server reads the
#     secret, attaches it per the key's binding, and makes the outbound call. The
#     plaintext never reaches the page. The key is locked to its bound base_url.
# All routes resolve the caller from the Bearer token (same as the Abilities →
# Credentials endpoints) and operate ONLY on that user's own vault.
# ─────────────────────────────────────────────────────────────────────────────

class VaultSaveRequest(BaseModel):
    values: dict


class VaultProxyRequest(BaseModel):
    key_id: str
    url: Optional[str] = None          # absolute target; must sit under the bound base_url
    path: Optional[str] = None         # or a path appended to the bound base_url
    method: Optional[str] = "GET"
    headers: Optional[dict] = None
    query: Optional[dict] = None
    json_body: Optional[object] = None  # parsed JSON body to send
    body: Optional[str] = None          # or a raw string body


def _vault_caller(authorization: Optional[str], token: Optional[str]) -> str:
    """Resolve the signed-in user; 401 for anonymous (same gate as cred saves)."""
    from app.admin.integrations import resolve_user_id, ANONYMOUS_KEY
    uid = resolve_user_id(authorization or "", token or "")
    if not uid or uid == ANONYMOUS_KEY:
        raise HTTPException(status_code=401, detail="Sign in to use the vault.")
    return uid


@router.get("/vault/keys")
async def api_vault_list_keys(
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """List the caller's vault keys as agent-safe metadata (never any secret)."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    keys = await vault_store.list_keys(uid)
    return {"status": "ok", "keys": keys, "count": len(keys)}


@router.get("/vault/keys/{key_id}")
async def api_vault_get_key(
    key_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Public view of one vault key — metadata + which secrets are set. No values."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    view = await vault_store.public_view(uid, key_id)
    if view is None:
        raise HTTPException(status_code=404, detail=f"Vault key '{key_id}' not found.")
    return {"status": "ok", **view}


@router.post("/vault/keys/{key_id}")
async def api_vault_save_key(
    key_id: str,
    body: VaultSaveRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Save the secret value(s) the user typed into the secure card. Browser→server
    only — this is the ONE write path for the plaintext, and it never goes near the
    agent. A blank secret keeps the stored one."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    ok = await vault_store.save_values(uid, key_id, body.values or {})
    if not ok:
        raise HTTPException(status_code=404, detail=f"Vault key '{key_id}' not found.")
    view = await vault_store.public_view(uid, key_id)
    return {"status": "ok", "filled": bool(view and view.get("filled"))}


@router.delete("/vault/keys/{key_id}")
async def api_vault_delete_key(
    key_id: str,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Remove a vault key (metadata + secret)."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)
    ok = await vault_store.delete_key(uid, key_id)
    return {"status": "ok" if ok else "error"}


def _host_is_blocked(host: str) -> bool:
    """Block loopback / private / link-local destinations so a bound base_url can't
    be pointed at the server's own internal network (basic SSRF guard)."""
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        # Can't resolve — let the outbound call fail naturally rather than here,
        # but DO block obvious local names.
        return host.lower() in {"localhost", "localhost.localdomain"}
    for info in infos:
        addr = info[4][0]
        try:
            ip = ipaddress.ip_address(addr.split("%")[0])
        except ValueError:
            continue
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            return True
    return False


@router.post("/vault/proxy")
async def api_vault_proxy(
    body: VaultProxyRequest,
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Make an outbound request on a genui's behalf, attaching a vault secret by
    id — server-side, so the plaintext never reaches the page or the agent.

    The key's stored *binding* is authoritative: the target must sit under the
    bound base_url, and the secret attaches exactly the way the binding says
    (bearer / basic / a named header / a query param). A key with no base_url
    can't be proxied at all."""
    from app.abilities import vault_store
    uid = _vault_caller(authorization, token)

    secret_info = await vault_store.read_secret(uid, body.key_id)
    if not secret_info:
        raise HTTPException(status_code=404, detail=f"Vault key '{body.key_id}' not found.")
    binding = secret_info.get("binding") or {}
    base_url = (binding.get("base_url") or "").strip()
    if not base_url:
        raise HTTPException(status_code=400, detail="This key has no service URL bound, so it can't be used to make a call.")
    secrets = secret_info.get("secrets") or {}
    secret_field = binding.get("secret_field") or ""
    secret_val = str(secrets.get(secret_field, "") or "")
    if not secret_val:
        raise HTTPException(status_code=409, detail="This key hasn't been filled in by the user yet.")

    # Resolve the target URL: absolute `url` must live under the bound base_url;
    # otherwise append `path` to base_url. Either way the host is pinned by the
    # binding, so the secret can only ever travel to its intended service.
    base = urlsplit(base_url)
    if body.url:
        target = urlsplit(body.url)
        if target.scheme != base.scheme or target.netloc != base.netloc \
                or not (target.path or "/").startswith(base.path or "/"):
            raise HTTPException(status_code=403, detail="That URL is outside the service this key is bound to.")
        target_url = body.url
    else:
        path = body.path or ""
        joined_path = (base.path.rstrip("/") + "/" + path.lstrip("/")) if path else base.path
        target_url = urlunsplit((base.scheme, base.netloc, joined_path, base.query, ""))

    if base.scheme not in ("http", "https") or _host_is_blocked(base.hostname or ""):
        raise HTTPException(status_code=403, detail="This key's service URL is not an allowed destination.")

    # Build headers / query with the secret attached per the binding.
    headers = {str(k): str(v) for k, v in (body.headers or {}).items()}
    parts = urlsplit(target_url)
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    for k, v in (body.query or {}).items():
        query_pairs.append((str(k), str(v)))

    attach = binding.get("attach") or "bearer"
    if attach == "bearer":
        headers["Authorization"] = "Bearer " + secret_val
    elif attach == "basic":
        headers["Authorization"] = "Basic " + secret_val
    elif attach.startswith("header:"):
        headers[attach.split(":", 1)[1]] = secret_val
    elif attach.startswith("query:"):
        query_pairs.append((attach.split(":", 1)[1], secret_val))

    final_url = urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_pairs), parts.fragment))
    method = (body.method or "GET").upper()

    import httpx
    req_kwargs: dict = {"headers": headers}
    if body.json_body is not None:
        req_kwargs["json"] = body.json_body
    elif body.body is not None:
        req_kwargs["content"] = body.body

    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=False) as client:
            resp = await client.request(method, final_url, **req_kwargs)
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"The call to the service failed: {e}")

    # Hand back status + body. Try JSON; fall back to text. We deliberately do NOT
    # forward upstream auth-bearing response headers.
    ctype = resp.headers.get("content-type", "")
    out: dict = {"status": "ok", "http_status": resp.status_code, "content_type": ctype}
    if "application/json" in ctype:
        try:
            out["json"] = resp.json()
        except Exception:
            out["text"] = resp.text
    else:
        out["text"] = resp.text
    return out

"""UI page catalog — the drop-in registry for main-panel pages & admin views.

A **page** is a top-level destination in the app shell. Its KIND is decided
PURELY by which CONTAINER folder it lives in — there is no ``kind`` field in the
descriptor anymore:
  • ``ui/main-panel/<id>/`` → ``main``  — a tab in the main header strip
  • ``ui/admin-tools/<id>/`` → ``admin`` — a view in the Admin Tools sidebar
  • ``ui/splash/<id>/``      → ``splash`` — the welcome landing page (no tab)

╔══════════════════════════════════════════════════════════════════════════╗
║  THIS FILE IS CORE.  Pages themselves are NOT defined here.               ║
║  Each page is a folder inside one of the three CONTAINERS above           ║
║  (ui/main-panel, ui/admin-tools, ui/splash) holding a ``page.json``       ║
║  descriptor. Drop a folder into a container → the page auto-appears LAST  ║
║  in its strip with the icon/name from its descriptor, its role set by the ║
║  container. Anything ELSE under ui/ (shared, css, js, icons, tutorials,   ║
║  chat, background, …) is infra, never a page — so no skip-list            ║
║  is needed. You do NOT edit index.html, partial-loader.js, tabs.js or     ║
║  files.js to add a page — they all render from whatever this manager      ║
║  discovers via GET /api/v1/pages/catalog. Admin reorder/rename/icon       ║
║  overrides persist to data/config/main-panel-pages.json +                ║
║  admin-panel-pages.json. Mirrors app/abilities/__init__.py. See CLAUDE.md ║
║  ("Core vs. plugins").                                                    ║
╚══════════════════════════════════════════════════════════════════════════╝

Page descriptor JSON contract (``ui/<container>/<page>/page.json``)
--------------------------------------------------------------------------
{
  "id": "agents",            // stable page id; decoupled from folder name so a
                             //   page's id may differ from its containing folder.
                             //   Defaults to the folder name when omitted.
                             // NB: there is NO "kind" field — the page's kind is
                             //   the container folder (main-panel/admin-tools/splash).
  "label": "Agents",         // default display name shown on the tab/strip
  "icon": "bot",             // default Lucide icon name
  "order": 1,                // SEED position. On the live table the admin order
                             //   in data/config/*-panel-pages.json wins; this
                             //   only positions a brand-new drop-in. Lower first;
                             //   ties break on label. Default 100.
  "locked": false,          // true = always visible, can't be hidden, sorts first
                             //   (Admin Tools tab; admin "settings" view).
  "memory": true,           // OPTIONAL — automatic view-state memory. Default
                             //   true for every catalog page: the shell saves
                             //   the page's scroll/panel state to localStorage
                             //   when the user leaves and restores it on return
                             //   or refresh, with NO page code. Set false to
                             //   opt out, or {"remember": "fn", "recall": "fn"}
                             //   to ALSO call those exports in the page's entry
                             //   module — remember() returns arbitrary state on
                             //   leave, recall(state) receives it on return —
                             //   for state that lives in JS (e.g. the open
                             //   record id) rather than the DOM. See
                             //   ui/shared/js/page-memory.js.
  "html": "agents.html",     // partial to inject into the page's mount. Optional
                             //   for iframe-only pages.
  "partials": [              // OPTIONAL extra HTML partials, RELATIVE to the page
    "events/events.html"     //   folder, injected at boot AFTER "html". For a
  ],                         //   multi-section view (admin Settings) whose section
                             //   markup lives in sub-folders; most pages omit it.
  "mount": "#tab-agents",    // content container selector. main → defaults to
                             //   "#tab-<id>"; admin → the per-view main element,
                             //   defaulting to "#files-<id>-main".
  "entry": "ui/main-panel/agents/js/agents.js",  // JS module driving the page (relative to
                             //   the document base). Optional.
  "start": "startAgents",    // exported lifecycle fn called on activate
  "stop": "stopAgents",      // exported lifecycle fn called on deactivate
  "css": ["agents.css"],     // stylesheet(s) the shell auto-injects, RELATIVE to
                             //   the page folder. String or list. Optional — when
                             //   omitted, a "<id>.css" / "<folder>.css" in the
                             //   folder is auto-picked. So a page carries its own
                             //   styling: no <link> edit to index.html ever.
  "router": "server.py",     // optional drop-in BACKEND. A Python module in the
                             //   page folder exposing a FastAPI `router`; the app
                             //   imports + mounts it at startup, so the page's API
                             //   comes and goes with its folder — no edit to
                             //   app/main.py. Defaults to "server.py" by
                             //   convention when the file exists. (.py files are
                             //   never served to the browser — see app/main.py.)
  "iframe": "/web-terminal/" // optional — iframe-only page (no entry/html)
}

For an ``admin`` view, entry/start/stop work exactly as for a main page: the
Admin Tools shell dynamically imports ``entry`` and calls the named ``start`` /
``stop`` when the view is shown / hidden (see ui/shared/js/files.js). Drop a
``ui/admin-tools/<id>/`` folder with a page.json (+ an ``entry`` module and a
``<main class="files-main" id="files-<id>-main" data-view="<id>">`` under
``#admin-tools``) and the strip icon, the view switch and its lifecycle all
work with NO edits to files.js or admin-tools.html.

Descriptors are read SERVER-SIDE only; the browser always goes through the
catalog API. The merge rule matches abilities: the descriptor supplies the seed
(order/label/icon); data/config/*-panel-pages.json supplies admin overrides; a
page with no override row sorts after all overridden pages (appended last),
exactly like a freshly-dropped ability.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# app/ui_pages/__init__.py → app/ui_pages → app → repo root → ui/
_REPO_ROOT = Path(__file__).resolve().parents[2]
_UI_DIR = _REPO_ROOT / "ui"

# Container folder under ui/ → the page "kind" every folder inside it gets.
# A page's role is decided PURELY by which container it lives in; there is no
# `kind` field in the descriptor. Anything under ui/ that is NOT one of these
# containers (shared, css, js, icons, tutorials, chat, background, …)
# is infra, never a page — so no skip-list is needed.
_CONTAINERS = {
    "main-panel": "main",
    "admin-tools": "admin",
    "splash": "splash",
}

_DEFAULT_ICON = "square"
_DEFAULT_ORDER = 100


def _default_visibility(kind: str) -> str:
    """Default page visibility when an admin has set NO override.

    Main header tabs and Admin Tools views require a signed-in (registered)
    account by default ("auth"), so a fresh deployment never exposes a page to
    anonymous (not-signed-in) visitors — an admin opens a page to anon
    explicitly by setting it to "all". The public splash/landing page stays
    world-visible ("all"), since it IS the front door an unauthenticated visitor
    must be able to see. See the page_config visibility contract."""
    return "all" if kind == "splash" else "auth"

_CATALOG: Optional[Dict[str, Dict[str, Any]]] = None
# Descriptor revisions must reach the catalog even when a long-lived worker has
# retained its process cache. Stat only the small descriptor set; page source and
# other assets remain outside this hot-path invalidation check.
_CATALOG_FINGERPRINT: Optional[Tuple[Tuple[str, int, int], ...]] = None


def _descriptor_fingerprint() -> Tuple[Tuple[str, int, int], ...]:
    rows: List[Tuple[str, int, int]] = []
    for container in _CONTAINERS:
        base = _UI_DIR / container
        if not base.is_dir():
            continue
        for descriptor in sorted(base.glob("*/page.json")):
            try:
                stat = descriptor.stat()
                rows.append((str(descriptor), stat.st_mtime_ns, stat.st_size))
            except OSError:
                continue
    return tuple(rows)


def _load_descriptor(json_path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception as e:
        logger.warning("Failed to parse page descriptor %s: %s", json_path, e)
        return None


def _css_list(desc: Dict[str, Any], folder: Path, pid: str) -> List[str]:
    """Stylesheet paths (relative to the page folder) the shell auto-injects.
    Honours an explicit ``css`` (string or list); otherwise falls back to a
    conventionally-named ``<id>.css`` / ``<folder>.css`` sitting in the folder so
    a simple page needs no declaration at all."""
    raw = desc.get("css")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(c) for c in raw if c]
    for cand in (f"{pid}.css", f"{folder.name}.css"):
        if (folder / cand).is_file():
            return [cand]
    return []


def _entry_from_dir(folder: Path, rel_dir: str, kind: str) -> Optional[Dict[str, Any]]:
    """Build a catalog entry from a folder that contains a page.json. Returns
    None when there is no descriptor (folder is not a page)."""
    json_path = folder / "page.json"
    if not json_path.is_file():
        return None
    desc = _load_descriptor(json_path)
    if desc is None:
        return None

    pid = str(desc.get("id") or folder.name)
    return {
        "id": pid,
        "kind": kind,  # authoritative: set by the container folder, not the descriptor
        "label": desc.get("label") or pid,
        "icon": desc.get("icon") or _DEFAULT_ICON,
        "order": desc.get("order", _DEFAULT_ORDER),
        "locked": bool(desc.get("locked", False)),
        "dir": rel_dir,
        "html": desc.get("html"),
        "mount": desc.get("mount") or (f"#tab-{pid}" if kind == "main" else None),
        "entry": desc.get("entry"),
        "start": desc.get("start"),
        "stop": desc.get("stop"),
        "css": _css_list(desc, folder, pid),
        "router": desc.get("router"),
        # Stable capability identifier used to tie the visible page to its
        # authoritative backend gate. It is intrinsic plugin metadata; tier
        # policies grant only the stable page id.
        "required_backend_capability": desc.get("required_backend_capability"),
        # Extra sub-folder backend modules (see discover_routers) — relative
        # paths from this page folder, e.g. ["dashboard/server.py"].
        "routers": [str(r) for r in (desc.get("routers") or []) if r],
        "iframe": desc.get("iframe"),
        # Automatic view-state memory: false opts out; {"remember": fn,
        # "recall": fn} adds custom-state hooks (see ui/shared/js/page-memory.js).
        "memory": desc.get("memory"),
        # Optional per-page chat launcher config — same shape as createChatLauncher
        # options in ui/chat-widget/js/chat-launcher.js (icon, agent_id, corner
        # buttons, widget options…). When present, the shell mounts a launcher
        # for this page on activate and destroys it on deactivate (tabs.js).
        "widget": desc.get("widget"),
        # Extra HTML partials this page needs injected at boot, RELATIVE to the
        # page folder. Most pages have none (their single `html` is enough); a
        # multi-section view (admin Settings) lists its section partials here so
        # the shell loads them with no hand-maintained list. See the loader's
        # dropInAdminPartials() in ui/shared/js/partial-loader.js.
        "partials": [str(p) for p in (desc.get("partials") or []) if p],
        # Settings pages use separately-addressable, lazy-loaded partials. Keep
        # only the loader's declarative id/path contract in the runtime catalog
        # so malformed descriptor entries cannot create invalid fetch targets.
        "settings_partials": [
            {"id": item["id"], "path": item["path"]}
            for item in (desc.get("settings_partials") or [])
            if isinstance(item, dict)
            and isinstance(item.get("id"), str) and item["id"]
            and isinstance(item.get("path"), str) and item["path"]
        ],
    }


def _key(kind: str, page_id: str) -> str:
    """Catalog key. Namespaced by kind so a main page and an admin view may share
    an id (e.g. 'terminal' is both the main iframe tab and the admin Terminal
    Launcher view) without colliding."""
    return f"{kind}:{page_id}"


def _load(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Walk each container (ui/main-panel, ui/admin-tools, ui/splash) and build
    the page catalog {"<kind>:<id>": entry}. The kind is the container's kind."""
    global _CATALOG, _CATALOG_FINGERPRINT
    fingerprint = _descriptor_fingerprint()
    if _CATALOG is not None and not force and _CATALOG_FINGERPRINT == fingerprint:
        return _CATALOG

    cat: Dict[str, Dict[str, Any]] = {}

    for container, kind in _CONTAINERS.items():
        base = _UI_DIR / container
        if not base.is_dir():
            continue
        for folder in sorted(base.iterdir()):
            if not folder.is_dir() or folder.name.startswith("_") or folder.name == "__pycache__":
                continue
            entry = _entry_from_dir(folder, f"{container}/{folder.name}", kind)
            if entry:
                cat[_key(entry["kind"], entry["id"])] = entry

    _CATALOG = cat
    _CATALOG_FINGERPRINT = fingerprint
    return cat


def reload() -> None:
    """Drop the cache so a newly-dropped page folder is picked up."""
    global _CATALOG, _CATALOG_FINGERPRINT
    _CATALOG = None
    _CATALOG_FINGERPRINT = None
    _load(force=True)


def _merge_kind(kind: str) -> List[Dict[str, Any]]:
    """Build the sorted, override-merged page list for one kind ('main'|'admin')."""
    cat = _load()

    overrides: Dict[str, Dict[str, Any]] = {}
    try:
        from app.admin import page_config
        overrides = page_config.get_overrides(kind)
    except Exception:
        pass

    pages: List[Dict[str, Any]] = []
    for entry in cat.values():
        if entry.get("kind") != kind:
            continue
        pid = entry["id"]
        ov = overrides.get(pid) or {}
        eff_order = ov.get("order")
        if not isinstance(eff_order, int):
            eff_order = entry.get("order", _DEFAULT_ORDER)
        # 3-state visibility: "all" (always, incl. anonymous) / "auth" (signed-in
        # registered users only) / "off" (hidden from all but admins). Read the
        # canonical override, falling back to the legacy boolean `hidden`, then to
        # the per-kind DEFAULT (main/admin → "auth", splash → "all") when the admin
        # set nothing — so a fresh deployment requires sign-in for every tab. A
        # locked page (Admin Tools / Admin Configuration) can never be turned fully
        # "off" — that would lock the user out of the app's configuration — so we
        # clamp "off" up to "all"; it may still be "auth".
        locked = entry.get("locked", False)
        vis = ov.get("visibility")
        if vis not in ("all", "auth", "off"):
            vis = "off" if ov.get("hidden") else _default_visibility(kind)
        if locked and vis == "off":
            vis = "all"
        pages.append({
            "id": pid,
            "kind": kind,
            "label": ov.get("label") or entry["label"],
            "icon": ov.get("icon") or entry["icon"],
            "order": eff_order,
            "visibility": vis,
            # `hidden` kept for back-compat (only the "off" state hides outright);
            # the "auth" state is gated client-side on sign-in, not here.
            "hidden": vis == "off",
            "locked": locked,
            "dir": entry.get("dir"),
            "html": entry.get("html"),
            "mount": entry.get("mount"),
            "entry": entry.get("entry"),
            "start": entry.get("start"),
            "stop": entry.get("stop"),
            "css": entry.get("css") or [],
            "iframe": entry.get("iframe"),
            "partials": entry.get("partials") or [],
            "settings_partials": entry.get("settings_partials") or [],
            "memory": entry.get("memory"),
            "widget": entry.get("widget"),
            "required_backend_capability": entry.get("required_backend_capability"),
        })

    # Locked pages first, then by effective order, then label.
    pages.sort(key=lambda p: (0 if p["locked"] else 1, p["order"], p["label"].lower()))
    return pages


def ui_catalog() -> Dict[str, Any]:
    """The payload the shell fetches to render the header tabs and admin sidebar.

    {
      main:  [ {id, label, icon, order, visibility, hidden, locked, dir, html,
                mount, entry, start, stop, css, iframe, partials, memory}, … ],   # sorted
      admin: [ … same shape … ],
      splash:[ … same shape … ],   # drop-in welcome-landing plugin (no tab)
    }

    The ``splash`` kind is for the drop-in welcome **landing page** (served by
    app/main.py at the front door ``/`` — see ``_render_landing_page``), which is
    NOT a header tab and NOT an admin view. A folder under ``ui/splash/`` is
    discovered by the same scanner; because its kind isn't ``main`` it never
    becomes a tab. The shell's boot hook (ui/shared/js/partial-loader.js) imports
    its entry module and calls its ``start`` — now only to expose
    ``window.WA_SPLASH`` (the per-device "show welcome" preference); there is no
    overlay to mount. When no such folder exists the list is empty and the hook is
    a no-op — so the whole feature is add/remove-able by dropping the folder in or
    deleting it, with no further edits here.
    """
    # Master on/off for the welcome landing is server-authoritative: when an admin
    # turns it off (splash_enabled in app-settings.json), the splash pages are
    # omitted from the catalog entirely — so the shell never imports the entry
    # module and ``window.WA_SPLASH`` is absent (the account toggle hides itself),
    # and the / front door serves the app shell directly.
    splash: List[Dict[str, Any]] = []
    try:
        from app.admin.settings import get_splash_enabled
        if get_splash_enabled():
            splash = _merge_kind("splash")
    except Exception:
        # If the setting can't be read, fall back to discovery (fail-open to the
        # folder being present) rather than silently hiding a configured splash.
        splash = _merge_kind("splash")

    return {
        "main": _merge_kind("main"),
        "admin": _merge_kind("admin"),
        "splash": splash,
    }


# ── Drop-in backend routers ─────────────────────────────────────────────────


def discover_routers() -> List[Tuple[str, Any]]:
    """Find drop-in page backends and return ``[(page_id, router), …]``.

    A page folder may carry its own server-side API: a Python module (named in
    its page.json ``router`` field, default ``server.py`` by convention) that
    exposes a FastAPI ``router``. We import each such module by file path and
    hand the router back to app/main.py to mount. So a page's endpoints live in
    its folder and come/go with it — adding or removing a page needs NO edit to
    app/main.py. Failures are isolated per-page (a broken backend skips that one
    page, never the whole app). ``.py`` files under ui/ are never served to the
    browser (see the static mount guard in app/main.py).

    A page may ALSO list ``routers`` in its page.json — relative paths to extra
    backend modules in SUBFOLDERS (e.g. ``"routers": ["dashboard/server.py"]``).
    Each is imported the same way and returned as ``("<pid>/<sub>", router)`` so
    main.py mounts it DIRECTLY on the app. This matters for nested routers:
    ``include_router`` inside a prefixed page router would bake the page's own
    prefix into the child's paths (FastAPI keeps an included router's paths
    under the parent prefix), so a sub-feature that owns its own URL space (like
    the embedded Dashboard's ``/admin/dashboard/*``) must be mounted at app
    level — not nested under ``/admin/instances``."""
    found: List[Tuple[str, Any]] = []
    for entry in _load().values():
        rel_dir = entry.get("dir")
        if not rel_dir:
            continue
        folder = _UI_DIR / rel_dir
        pid = entry["id"]
        kind = entry.get("kind", "main")

        def _load_backend(rel_path: Path, mod_name: str, label: str) -> Optional[Tuple[str, Any]]:
            if not rel_path.is_file():
                return None
            try:
                spec = importlib.util.spec_from_file_location(mod_name, rel_path)
                if spec is None or spec.loader is None:
                    return None
                module = importlib.util.module_from_spec(spec)
                # Register in sys.modules BEFORE exec. Page backends use
                # `from __future__ import annotations`, so Pydantic keeps their
                # field types as strings and resolves them lazily by looking the
                # owning class's `__module__` up in sys.modules. An unregistered
                # module leaves any model with a typing generic (List[str],
                # Optional[…]) "not fully defined", which 500s GET /openapi.json
                # for the WHOLE app as soon as one page backend declares one.
                sys.modules[mod_name] = module
                try:
                    spec.loader.exec_module(module)
                except Exception:
                    sys.modules.pop(mod_name, None)
                    raise
                router = getattr(module, "router", None)
                if router is None:
                    logger.warning("Page backend %s has no `router` export", rel_path)
                    return None
                return (label, router)
            except Exception as e:
                logger.warning("Failed to load page backend %s: %s", rel_path, e)
                return None

        fname = entry.get("router") or "server.py"
        mod_path = folder / fname
        if mod_path.is_file():
            mod_name = f"webagent_page_{kind}_{pid}".replace("-", "_")
            loaded = _load_backend(mod_path, mod_name, pid)
            if loaded:
                found.append(loaded)
        # Extra sub-folder backends (page.json ``routers`` list) — mounted at
        # app level so each keeps its OWN prefix (see docstring above).
        for sub in (entry.get("routers") or []):
            sub_path = folder / str(sub)
            stem = str(sub).replace("\\", "/")
            if stem.endswith(".py"):
                stem = stem[:-3]
            sub_mod = f"webagent_page_{kind}_{pid}_{stem}".replace("-", "_").replace("/", "_")
            loaded = _load_backend(sub_path, sub_mod, f"{pid}/{stem}")
            if loaded:
                found.append(loaded)
    return found


# ── Accessors ─────────────────────────────────────────────────────────────────

def all_raw() -> Dict[str, Dict[str, Any]]:
    return dict(_load())


def page_ids(kind: Optional[str] = None) -> List[str]:
    cat = _load()
    if kind is None:
        return [e["id"] for e in cat.values()]
    return [e["id"] for e in cat.values() if e.get("kind") == kind]


def page_entry(kind: str, page_id: str) -> Optional[Dict[str, Any]]:
    return _load().get(_key(kind, page_id))


def effective_visibility(kind: str, page_id: str) -> str:
    """The page's effective visibility ("all" | "auth" | "off") — the admin
    override if set, else the per-kind default — matching exactly what
    ui_catalog() computes for the strip. This is the SERVER-SIDE source of truth
    a page's sensitive endpoints call (via app.auth.identity.user_may_access_page)
    to enforce who may reach it, instead of trusting the client to hide the tab.
    An unknown page falls back to the kind default (fail-closed for main/admin)."""
    entry = page_entry(kind, page_id)
    if entry is None:
        return _default_visibility(kind)
    try:
        from app.admin import page_config
        ov = page_config.get_overrides(kind).get(page_id) or {}
    except Exception:
        ov = {}
    vis = ov.get("visibility")
    if vis not in ("all", "auth", "off"):
        vis = "off" if ov.get("hidden") else _default_visibility(kind)
    if bool(entry.get("locked")) and vis == "off":
        vis = "all"
    return vis


def is_known_page(scope: str, page_id: str) -> bool:
    """True iff a page with this id exists for the given scope ('main'|'admin')."""
    return _key(scope, page_id) in _load()


def page_orders(kind: str) -> Dict[str, int]:
    """{page_id: seed order} for one kind, from the descriptors."""
    return {e["id"]: int(e.get("order", _DEFAULT_ORDER))
            for e in _load().values() if e.get("kind") == kind}

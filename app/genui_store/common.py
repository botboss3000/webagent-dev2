"""Helpers shared by all GenuiStore implementations."""

import json
import logging
import os
import re
import shutil
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app import user_workspace as _ws

logger = logging.getLogger(__name__)

# Per-process guard so the one-time legacy migrations run at most once per user.
_migrated: set = set()
_manifest_migrated: set = set()

# Per-genui descriptor file, mirroring the main-panel page convention
# (ui/<page>/page.json — see app/ui_pages/__init__.py). Optional: a folder with
# an index.html is already a valid genui; the descriptor only adds ordering and
# metadata (title, agent_context, description, timestamps).
GENUI_META_FILE = "page.json"

# Per-genui DATA file — the genui's content (records the page renders: rosters,
# schedules, rows…) kept SEPARATE from index.html so the agent updates data
# without rewriting the page markup, and the page reads it instead of hardcoding
# it. Optional: a genui with no data.json just has no injected data. Sibling to
# index.html / page.json in the genui folder. (Shared cross-genui datastores
# are a later phase; this is the per-genui file.)
GENUI_DATA_FILE = "data.json"

# Per-genui WIDGET file — optional launcher/widget configuration for THIS page:
# which agent the page's chat launcher opens, which icon it shows, which corner
# buttons surround it, and any createChatWidget options (session contract,
# initial message, transformMessage…). Shape mirrors the createChatLauncher
# options in ui/chat-widget/js/chat-launcher.js. Optional: a genui with no
# widget.json simply gets no page launcher (the global launcher, when enabled,
# still works). Sibling to index.html / data.json in the genui folder.
GENUI_WIDGET_FILE = "widget.json"

# A genui with no `order` in its descriptor sorts after all ordered genui.
DEFAULT_ORDER = 100


def safe(name: str) -> str:
    """Sanitize a string for safe use as a directory / file / slug segment."""
    return re.sub(r"[^\w\-]", "_", name)


def user_genui_dir(user_id: str) -> str:
    """On-disk dir holding a user's genui.

    Lives in the user's data home: ``data/user_data/<user_id>/genui/`` (see
    app/user_workspace.py). Created if missing. Any legacy ``pages/`` layout is
    migrated into the new ``genui/`` folder-per-genui layout on first access.
    A second one-time migration converts any old root ``genui.json`` manifest
    into per-genui ``page.json`` descriptors."""
    _migrate_canvas_to_genui(user_id)
    _migrate_legacy_pages(user_id)
    _migrate_manifest_to_meta(user_id)
    return str(_ws.user_dir(user_id, "genui"))


def _migrate_canvas_to_genui(user_id: str) -> None:
    """Move a user's ``canvas/`` folder to ``genui/`` (Canvas → Gen UI rename).

    Idempotent and non-destructive: renames the dir when only the old ``canvas/``
    exists; if both exist, moves across only the entries that don't already have a
    home in ``genui/``. Runs at most once per user per process. Users on the
    database backend (no on-disk folder) are a no-op."""
    if user_id in _migrated:
        return
    try:
        home = _ws.user_home(user_id)
        old = home / "canvas"
        if not old.is_dir():
            return
        new = home / "genui"
        if not new.exists():
            shutil.move(str(old), str(new))
            logger.info("Renamed genui folder for user %s: canvas/ -> genui/", user_id)
            return
        # Both exist: move non-colliding children, then drop the old dir if empty.
        for child in old.iterdir():
            dest = new / child.name
            if not dest.exists():
                shutil.move(str(child), str(dest))
        try:
            old.rmdir()
        except OSError:
            pass
    except Exception as e:
        logger.warning("Gen UI folder rename skipped for user %s: %s", user_id, e)


def genui_dir(user_id: str, slug: str) -> str:
    """Per-genui folder: ``data/user_data/<user_id>/genui/<slug>/``.

    Each genui owns its own directory so it can grow to hold assets/versions
    later without colliding with siblings."""
    return os.path.join(user_genui_dir(user_id), safe(slug))


def genui_body_path(user_id: str, slug: str) -> str:
    """The HTML body file for one genui: ``<genui dir>/<slug>/index.html``."""
    return os.path.join(genui_dir(user_id, slug), "index.html")


def genui_meta_path(user_id: str, slug: str) -> str:
    """The optional descriptor file for one genui: ``<genui dir>/<slug>/page.json``.

    Holds the genui's title, agent_context, description, display ``order`` and
    timestamps. Entirely optional — a folder with just an index.html is a valid
    genui (title defaults to the slug, no description, sorted alphabetically
    after the explicitly-ordered genui)."""
    return os.path.join(genui_dir(user_id, slug), GENUI_META_FILE)


def read_genui_meta(user_id: str, slug: str) -> Dict:
    """Read a genui's page.json descriptor, or ``{}`` when absent/unreadable."""
    path = genui_meta_path(user_id, slug)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def write_genui_meta(user_id: str, slug: str, meta: Dict) -> None:
    """Write (create or overwrite) a genui's page.json descriptor."""
    os.makedirs(genui_dir(user_id, slug), exist_ok=True)
    with open(genui_meta_path(user_id, slug), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def genui_data_path(user_id: str, slug: str) -> str:
    """The optional data file for one genui: ``<genui dir>/<slug>/data.json``.

    Holds the genui's CONTENT (the records the page displays) as a JSON object,
    kept out of index.html so the agent edits data without touching the page."""
    return os.path.join(genui_dir(user_id, slug), GENUI_DATA_FILE)


def read_genui_data(user_id: str, slug: str) -> Optional[Dict]:
    """Read a genui's data.json, or ``None`` when absent/unreadable/not an object."""
    path = genui_data_path(user_id, slug)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_genui_data(user_id: str, slug: str, data: Dict) -> None:
    """Write (create or overwrite) a genui's data.json content file."""
    os.makedirs(genui_dir(user_id, slug), exist_ok=True)
    with open(genui_data_path(user_id, slug), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def genui_widget_path(user_id: str, slug: str) -> str:
    """The optional widget file for one genui: ``<genui dir>/<slug>/widget.json``.

    Holds the page's launcher/widget configuration (which agent the page's chat
    launcher opens, icon, corner buttons, widget options) as a JSON object, kept
    out of index.html the same way data.json is so the agent edits config
    without touching the page markup."""
    return os.path.join(genui_dir(user_id, slug), GENUI_WIDGET_FILE)


def read_genui_widget(user_id: str, slug: str) -> Optional[Dict]:
    """Read a genui's widget.json, or ``None`` when absent/unreadable/not an object."""
    path = genui_widget_path(user_id, slug)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def write_genui_widget(user_id: str, slug: str, widget: Dict) -> None:
    """Write (create or overwrite) a genui's widget.json config file."""
    os.makedirs(genui_dir(user_id, slug), exist_ok=True)
    with open(genui_widget_path(user_id, slug), "w", encoding="utf-8") as f:
        json.dump(widget, f, indent=2)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Per-genui CONSOLE LOG ─────────────────────────────────────────────────────
# A genui's own console output (console.log/warn/error + uncaught script errors),
# captured in the browser as the page runs and POSTed back here, kept in a plain
# file BESIDE index.html — NOT in the global logs.db. This gives the design agent a
# page-scoped debug log it can read with no codebase-admin / logs.db access: just
# the logs of the one genui it built. Stored as JSON-lines (one record per line:
# {ts, level, text, stack?}) so appends are cheap and the file stays human-readable.
# Filesystem-local on purpose (genui is a local-single-user feature; see the
# genui.js local-only gate) — it lives in the same genui folder as data.json.
GENUI_LOG_FILE = "console.log"

# Hard cap on retained lines so a chatty genui (an interval logging every tick)
# can't grow the file without bound — we keep only the most recent N.
GENUI_LOG_MAX_LINES = 500


def genui_log_path(user_id: str, slug: str) -> str:
    """The per-genui console-log file: ``<genui dir>/<slug>/console.log`` (JSONL)."""
    return os.path.join(genui_dir(user_id, slug), GENUI_LOG_FILE)


def append_genui_logs(user_id: str, slug: str, entries: List[Dict],
                       replace_source: Optional[str] = None) -> int:
    """Append console entries to a genui's log file, trimming to the most recent
    ``GENUI_LOG_MAX_LINES``. Each entry is normalised + size-capped so a runaway
    genui can't write a huge file. Returns the number of lines kept.

    Entries may carry a ``source`` tag (e.g. 'headless' for the screenshot_genui
    render vs the default live-browser capture). ``replace_source`` first DROPS any
    existing lines with that source before appending — so a headless re-verify
    refreshes its own entries without wiping the live-session ones (and a clean
    render with no output still clears stale headless errors). Best-effort: bad
    entries are skipped and the whole call is wrapped so logging never bubbles up."""
    if not isinstance(entries, list):
        entries = []
    # Nothing to write and nothing to clear → no-op.
    if not entries and not replace_source:
        return 0
    new_lines: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rec = {
            "ts": e.get("ts") or now_iso(),
            "level": str(e.get("level") or "log")[:12],
            "text": str(e.get("text") or "")[:4000],
        }
        stack = e.get("stack")
        if stack:
            rec["stack"] = str(stack)[:8000]
        src = e.get("source")
        if src:
            rec["source"] = str(src)[:40]
        new_lines.append(json.dumps(rec, ensure_ascii=False))
    try:
        os.makedirs(genui_dir(user_id, slug), exist_ok=True)
        path = genui_log_path(user_id, slug)
        existing: List[str] = []
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    existing = [ln for ln in f.read().splitlines() if ln.strip()]
            except Exception:
                existing = []
        if replace_source:
            kept_existing: List[str] = []
            for ln in existing:
                try:
                    r = json.loads(ln)
                    if isinstance(r, dict) and r.get("source") == replace_source:
                        continue  # drop the prior entries from this source
                except Exception:
                    pass  # keep unparseable lines as-is
                kept_existing.append(ln)
            existing = kept_existing
        kept = (existing + new_lines)[-GENUI_LOG_MAX_LINES:]
        with open(path, "w", encoding="utf-8") as f:
            f.write(("\n".join(kept) + "\n") if kept else "")
        return len(kept)
    except Exception as ex:
        logger.warning("append_genui_logs failed for %s/%s: %s", user_id, slug, ex)
        return 0


def read_genui_logs(user_id: str, slug: str, limit: int = 100,
                     level: Optional[str] = None) -> List[Dict]:
    """Return a genui's most recent console entries (newest last), optionally
    filtered to one level ('error'/'warn'/'log'/'info'/'debug'). Empty list when
    the genui has no log file yet."""
    path = genui_log_path(user_id, slug)
    if not os.path.exists(path):
        return []
    out: List[Dict] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rec = json.loads(ln)
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if level and str(rec.get("level") or "").lower() != level.lower():
                    continue
                out.append(rec)
    except Exception as ex:
        logger.warning("read_genui_logs failed for %s/%s: %s", user_id, slug, ex)
        return []
    if limit and limit > 0:
        out = out[-limit:]
    return out


def clear_genui_logs(user_id: str, slug: str) -> None:
    """Delete a genui's console-log file (best-effort). Called when the page is
    re-rendered so the log only ever reflects the version now running."""
    path = genui_log_path(user_id, slug)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as ex:
        logger.warning("clear_genui_logs failed for %s/%s: %s", user_id, slug, ex)


def discover_genui_slugs(user_id: str) -> List[str]:
    """Folder names under genui/ that contain an index.html — i.e. every genui,
    descriptor or not. This is the source of truth for which genui exist."""
    root = user_genui_dir(user_id)
    slugs: List[str] = []
    try:
        for name in os.listdir(root):
            folder = os.path.join(root, name)
            if not os.path.isdir(folder) or name.startswith((".", "_")):
                continue
            if os.path.exists(os.path.join(folder, "index.html")):
                slugs.append(name)
    except FileNotFoundError:
        pass
    return slugs


def genui_entry(user_id: str, slug: str) -> Dict:
    """Build a catalog entry for one genui from its (optional) descriptor.

    Defaults when no/partial page.json: title → slug, description → "",
    agent_context → generated default, order → DEFAULT_ORDER (sorts last,
    alphabetically), timestamps → None."""
    meta = read_genui_meta(user_id, slug)
    title = meta.get("title") or slug
    # Session config: the REQUIRED session contract for this page's actions/chat.
    # Defaults to {} for legacy pages (they keep current runtime behavior until
    # an agent sets a config via the PATCH endpoint).
    _sc = meta.get("session_config")
    if not isinstance(_sc, dict):
        try:
            _sc = json.loads(_sc) if isinstance(_sc, str) and _sc.strip() else {}
        except Exception:
            _sc = {}
    return {
        "slug": slug,
        "title": title,
        "description": meta.get("description") or "",
        "agent_context": meta.get("agent_context") or default_agent_context(title),
        # Owning agent (the agent that created/manages this genui), or "" when the
        # genui hasn't been rendered by an agent yet — the footer then falls back
        # to the default WebAgent.
        "agent_id": meta.get("agent_id") or "",
        "session_config": _sc,
        "order": meta.get("order", DEFAULT_ORDER),
        "created_at": meta.get("created_at"),
        "updated_at": meta.get("updated_at"),
        "url": genui_url(user_id, slug),
    }


def sort_genui_entries(entries: List[Dict]) -> List[Dict]:
    """Home always first; then explicitly-ordered genui by ``order``; then
    the rest alphabetically by title. Mirrors the main-panel page sort."""
    def key(e: Dict):
        is_home = 0 if e.get("slug") == "home" else 1
        order = e.get("order")
        order = order if isinstance(order, (int, float)) else DEFAULT_ORDER
        return (is_home, order, (e.get("title") or e.get("slug") or "").lower())
    return sorted(entries, key=key)


def _migrate_manifest_to_meta(user_id: str) -> None:
    """One-time, non-destructive migration from the old root ``genui.json``
    manifest to a per-genui ``page.json`` descriptor in each genui folder.

    For each manifest entry whose folder lacks a page.json, write one carrying
    its title/agent_context/timestamps and an ``order`` equal to its position in
    the manifest (so the existing dropdown arrangement is preserved). The old
    manifest is then removed. Runs at most once per user per process; a no-op
    when there is no manifest (already migrated, or a non-filesystem backend)."""
    if user_id in _manifest_migrated:
        return
    _manifest_migrated.add(user_id)
    try:
        root = _ws.user_dir(user_id, "genui")
        manifest = os.path.join(str(root), "genui.json")
        if not os.path.exists(manifest):
            return
        with open(manifest, "r", encoding="utf-8") as f:
            pages = json.load(f)
        if not isinstance(pages, list):
            os.remove(manifest)
            return
        for idx, page in enumerate(pages):
            slug = safe(str(page.get("slug") or "")) if isinstance(page, dict) else ""
            if not slug:
                continue
            folder = os.path.join(str(root), slug)
            meta_path = os.path.join(folder, GENUI_META_FILE)
            # Only seed folders that exist and aren't already described.
            if not os.path.isdir(folder) or os.path.exists(meta_path):
                continue
            meta = {
                "title": page.get("title") or slug,
                "agent_context": page.get("agent_context") or "",
                "order": idx,
                "created_at": page.get("created_at"),
                "updated_at": page.get("updated_at"),
            }
            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, indent=2)
        os.remove(manifest)
        logger.info("Migrated genui manifest for user %s: genui.json -> per-folder page.json", user_id)
    except Exception as e:
        logger.warning("Gen UI manifest migration skipped for user %s: %s", user_id, e)


def _migrate_legacy_pages(user_id: str) -> None:
    """Move a user's old flat ``pages/<slug>.html`` layout into the new
    folder-per-genui ``genui/<slug>/index.html`` layout.

    Idempotent and non-destructive: only moves a body when its new home does
    not already exist; runs at most once per user per process. Database-mode
    users have no ``pages/`` dir, so this is a no-op for them."""
    if user_id in _migrated:
        return
    _migrated.add(user_id)
    try:
        home = _ws.user_home(user_id)
        old = home / "pages"
        if not old.is_dir():
            return
        new = home / "genui"
        new.mkdir(parents=True, exist_ok=True)
        # Carry any legacy manifest across; _migrate_manifest_to_meta (run right
        # after this) converts it into per-folder page.json descriptors.
        old_manifest = old / "genui.json"
        new_manifest = new / "genui.json"
        if old_manifest.exists() and not new_manifest.exists():
            shutil.move(str(old_manifest), str(new_manifest))
        # Each genui body: pages/<slug>.html -> genui/<slug>/index.html
        for html in old.glob("*.html"):
            dest_dir = new / safe(html.stem)
            dest = dest_dir / "index.html"
            if dest.exists():
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            shutil.move(str(html), str(dest))
        logger.info("Migrated genui data for user %s: pages/ -> genui/", user_id)
    except Exception as e:
        # Don't let a migration hiccup break genui reads/writes.
        logger.warning("Gen UI data migration skipped for user %s: %s", user_id, e)


def genui_url(user_id: str, slug: str) -> str:
    """The URL the iframe loads to render a page.

    Backend-agnostic: served by app/api/genui.py:GET /api/v1/genui/{uid}/{slug}/html
    which dispatches through the active GenuiStore."""
    return f"/api/v1/genui/{safe(user_id)}/{safe(slug)}/html"


def default_agent_context(title: str) -> str:
    return (
        f"You are the {title} genui agent. Your role is to build and maintain this "
        f"genui called '{title}'. When asked to create or update content, produce "
        f"clean, well-designed HTML that matches the WebAgent look in both dark and "
        f"light themes and on desktop and mobile (follow the Visualizer ability "
        f"skill). Render functional, interactive genui tailored to the purpose "
        f"of '{title}'."
    )


def home_agent_context() -> str:
    return (
        "You are the WebAgent home genui agent. Your role is to maintain this "
        "informational genui about WebAgent — its features, getting started guide, "
        "and use cases. When asked to update or modify this genui, produce clean, "
        "well-structured HTML that matches the WebAgent look in both dark and light "
        "themes (follow the Visualizer ability skill). The genui serves as the "
        "main welcome and onboarding resource for users of the app."
    )


def blank_genui_html(title: str) -> str:
    """The BASE genui — the minimal, correct skeleton every new/empty genui starts
    from (and the structure an agent extends). It deliberately models the four rules
    of the first-class-genui contract so the common bugs can't be copied from it:
    consume app tokens (no own palette), one uniquely-named ~20px wrapper (never a
    second ``genui-root``), query the shadow ``root`` (never ``document.*``), and do
    all setup INSIDE ``WebagentGenui.register`` (not at script top-level)."""
    escaped = title.replace("<", "&lt;").replace(">", "&gt;")
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<title>{escaped}</title>
<!--
  BASE GENUI — minimal correct skeleton. Follow these and the usual bugs can't happen:
   1. CONSUME the app's design tokens (var(--fg-1), var(--bg-elev), var(--accent),
      var(--border), var(--shadow-rest), var(--font-sans)…). Do NOT define your own
      :host/:root palette — the app's tokens inherit in and flip light/dark for free.
   2. ONE uniquely-named wrapper (#cv-root) with ~20px padding. Never give your own
      wrapper class="genui-root" — the app already wraps you in one, so duplicating it
      DOUBLES padding/margins.
   3. Query your DOM through the shadow `root` the mount hands you — NEVER document.*
      (your markup lives in a shadow root; document.getElementById returns null).
   4. Do ALL setup INSIDE WebagentGenui.register(...) using that root — not at the top
      of the script (the shadow root doesn't exist yet when top-level code runs).
-->
<style>
  *, *::before, *::after {{ box-sizing: border-box; }}
  /* #cv-root is OUR wrapper (uniquely named, not .genui-root). 20px = the app's gutter. */
  #cv-root {{ padding: 20px; min-height: 100vh; font-family: var(--font-sans, system-ui);
    color: var(--fg-1, #c0caf5); background: transparent; }}
  .cv-empty {{ display: flex; flex-direction: column; align-items: center; justify-content: center;
    min-height: calc(100vh - 40px); gap: 10px; text-align: center; padding: 40px;
    border: var(--border-width, 1px) solid var(--border, rgba(255,255,255,.08));
    border-radius: 16px; background: var(--bg-elev, rgba(255,255,255,.03));
    box-shadow: var(--shadow-rest, 0 2px 10px rgba(0,0,0,.2)); }}
  .cv-empty h2 {{ margin: 0; font-size: 20px; color: var(--accent, #7dcfff); }}
  .cv-empty p {{ margin: 0; font-size: 14px; color: var(--fg-3, #565f89); }}
</style>
</head>
<body>
<div id="cv-root">
  <div class="cv-empty">
    <h2>{escaped}</h2>
    <p>This Gen UI is empty — send a prompt to build it.</p>
  </div>
</div>
<script>
(function () {{
  // Rule 4: all wiring goes INSIDE register, using the shadow `root` it hands you.
  function mount(root, api) {{
    // const el = root.querySelector('#cv-root');   // Rule 3: query via root, never document.*
    return function cleanup() {{ /* stop any cameras/timers you started */ }};
  }}
  if (window.WebagentGenui) window.WebagentGenui.register(mount);
}})();
</script>
</body>
</html>"""

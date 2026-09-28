"""Admin-level UI-page configuration store — two repo-local files.

Owns ``data/config/main-panel-pages.json`` (the main header tabs) and
``data/config/admin-panel-pages.json`` (the Admin Tools sidebar views): the
single, human-readable source of truth for the admin's page customizations —
display order, renamed labels, custom icons, and hidden state — for **every**
drop-in page (current and future) with no hardcoded per-page registration.

What lives here (per page id, all optional overrides):
  • ``order``      — admin display order (overrides the descriptor seed ``order``)
  • ``label``      — renamed display name (overrides the descriptor ``label``)
  • ``icon``       — custom Lucide icon (overrides the descriptor ``icon``)
  • ``visibility`` — who sees the page in its strip. One of:
        ``all``  — always visible, including anonymous (not-signed-in) visitors
        ``auth`` — visible only to a signed-in (registered) user
        ``off``  — hidden from everyone but admins
      All three are EXPLICIT overrides and are stored as-is. A page with NO entry
      here falls back to its per-kind DEFAULT (decided in app/ui_pages: main
      header tabs + admin views default to ``auth`` so a fresh deployment never
      exposes a page to anonymous visitors; the public splash landing defaults to
      ``all``). Locked pages can't be ``off`` (clamped up). The legacy boolean
      ``hidden`` (``hidden: true`` == ``visibility: off``) is still read for
      back-compat and migrated to ``visibility`` on the next save.

A page with no entry here falls back entirely to its ``page.json`` descriptor,
so a freshly-dropped folder lands at the position/name/icon it declares until an
admin customizes it. Mirrors app/admin/ability_config.py (load-with-defaults,
log-and-degrade, ``indent=2``, ``threading.RLock``).
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)

# app/admin/page_config.py → app/admin → app → repo root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_CONFIG_DIR = os.path.join(_REPO_ROOT, "data", "config")

_FILES = {
    "main": os.path.join(_CONFIG_DIR, "main-panel-pages.json"),
    "admin": os.path.join(_CONFIG_DIR, "admin-panel-pages.json"),
}

_VERSION = 1
_VALID_FIELDS = ("order", "label", "icon", "hidden", "visibility")
# 3-state page visibility. All three are explicit overrides and are persisted as
# stored; a page with NO entry falls back to its per-kind default (see
# app/ui_pages._default_visibility: "auth" for main/admin, "all" for splash).
_VALID_VIS = ("all", "auth", "off")

_lock = threading.RLock()
_cache: Dict[str, Optional[Dict[str, Any]]] = {"main": None, "admin": None}


def _scope_file(scope: str) -> str:
    path = _FILES.get(scope)
    if not path:
        raise ValueError(f"Unknown page scope: {scope!r} (expected 'main' or 'admin')")
    return path


def _empty() -> Dict[str, Any]:
    return {"version": _VERSION, "pages": {}}


def _coerce(data: Any) -> Dict[str, Any]:
    out = _empty()
    if isinstance(data, dict):
        if isinstance(data.get("version"), int):
            out["version"] = data["version"]
        pages = data.get("pages")
        if isinstance(pages, dict):
            clean: Dict[str, Any] = {}
            for pid, ov in pages.items():
                if not isinstance(ov, dict):
                    continue
                row: Dict[str, Any] = {}
                if isinstance(ov.get("order"), int):
                    row["order"] = ov["order"]
                if isinstance(ov.get("label"), str) and ov["label"].strip():
                    row["label"] = ov["label"].strip()
                if isinstance(ov.get("icon"), str) and ov["icon"].strip():
                    row["icon"] = ov["icon"].strip()
                # Visibility is canonical. Accept a stored "visibility" (all three
                # states are explicit and kept), else migrate the legacy boolean
                # "hidden" (true -> "off"). No visibility + no hidden = no row, so
                # the page falls back to its per-kind default in ui_pages.
                vis = ov.get("visibility")
                if not (isinstance(vis, str) and vis in _VALID_VIS):
                    vis = "off" if ov.get("hidden") is True else None
                if vis in ("all", "auth", "off"):
                    row["visibility"] = vis
                if row:
                    clean[str(pid)] = row
            out["pages"] = clean
    return out


def _load(scope: str) -> Dict[str, Any]:
    with _lock:
        if _cache[scope] is not None:
            return _cache[scope]
        path = _scope_file(scope)
        data: Any = None
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
        except Exception as e:
            logger.warning("%s: could not read (%s) — using empty", os.path.basename(path), e)
        _cache[scope] = _coerce(data)
        return _cache[scope]


def _save(scope: str, data: Dict[str, Any]) -> None:
    with _lock:
        coerced = _coerce(data)
        _cache[scope] = coerced
        path = _scope_file(scope)
        try:
            safe_write_json(path, coerced)
        except Exception as e:
            logger.warning("%s: could not write: %s", os.path.basename(path), e)


# ── Reads ─────────────────────────────────────────────────────────────────────

def get_overrides(scope: str) -> Dict[str, Dict[str, Any]]:
    """Every stored override row: ``{page_id: {order?, label?, icon?, hidden?}}``."""
    return dict(_load(scope)["pages"])


def get_order(scope: str) -> Dict[str, int]:
    return {pid: ov["order"] for pid, ov in _load(scope)["pages"].items()
            if isinstance(ov.get("order"), int)}


# ── Writes (read-modify-write a single page row) ──────────────────────────────

def _set_field(scope: str, page_id: str, field: str, value: Any) -> None:
    if field not in _VALID_FIELDS:
        raise ValueError(f"Unknown page override field: {field!r}")
    data = _load(scope)
    pages = {pid: dict(ov) for pid, ov in data["pages"].items()}
    row = pages.get(page_id, {})
    if value is None:
        row.pop(field, None)
    else:
        row[field] = value
    if row:
        pages[page_id] = row
    else:
        pages.pop(page_id, None)
    _save(scope, dict(data, pages=pages))


def set_label(scope: str, page_id: str, label: Optional[str]) -> None:
    _set_field(scope, page_id, "label", label.strip() if isinstance(label, str) and label.strip() else None)


def set_icon(scope: str, page_id: str, icon: Optional[str]) -> None:
    _set_field(scope, page_id, "icon", icon.strip() if isinstance(icon, str) and icon.strip() else None)


def set_visibility(scope: str, page_id: str, visibility: Optional[str]) -> None:
    # Canonical 3-state visibility. All three states are EXPLICIT overrides and
    # are stored as-is (the per-kind default now lives in ui_pages, so "all" must
    # persist or an admin could never re-open a page to anonymous visitors). An
    # invalid/None value clears the override, reverting the page to its default.
    vis = visibility if isinstance(visibility, str) and visibility in _VALID_VIS else None
    _set_field(scope, page_id, "visibility", vis)


def set_hidden(scope: str, page_id: str, hidden: bool) -> None:
    # Back-compat shim: "hidden" -> "off"; "un-hidden" clears the override so the
    # page reverts to its per-kind DEFAULT (no longer forced to "all", which would
    # expose it to anonymous visitors).
    set_visibility(scope, page_id, "off" if hidden else None)


def set_order(scope: str, order: Dict[str, int]) -> None:
    """Merge a {page_id: order_int} map into the stored rows."""
    data = _load(scope)
    pages = {pid: dict(ov) for pid, ov in data["pages"].items()}
    for pid, val in (order or {}).items():
        try:
            ival = int(val)
        except (TypeError, ValueError):
            continue
        row = pages.get(str(pid), {})
        row["order"] = ival
        pages[str(pid)] = row
    _save(scope, dict(data, pages=pages))


def set_page(scope: str, page_id: str, *, label: Any = ..., icon: Any = ...,
             hidden: Any = ..., visibility: Any = ..., order: Any = ...) -> None:
    """Apply several overrides to one page in a single write. Pass a value to
    set it (``None`` clears that field); omit an arg to leave it untouched.

    ``visibility`` is the canonical 3-state field ("all"/"auth"/"off"); "all"
    clears it. ``hidden`` is still accepted for back-compat and folded onto
    ``visibility`` (true -> "off", false -> "all")."""
    data = _load(scope)
    pages = {pid: dict(ov) for pid, ov in data["pages"].items()}
    row = pages.get(page_id, {})

    def _apply(field: str, value: Any, coerce):
        if value is ...:
            return
        cleaned = coerce(value)
        if cleaned is None:
            row.pop(field, None)
        else:
            row[field] = cleaned

    # Fold a legacy boolean `hidden` onto `visibility` so a single field stays
    # canonical. An explicit `visibility` arg wins over `hidden`. "un-hidden"
    # clears the override (revert to default) rather than forcing "all".
    if visibility is ... and hidden is not ...:
        visibility = "off" if bool(hidden) else None

    _apply("label", label, lambda v: v.strip() if isinstance(v, str) and v.strip() else None)
    _apply("icon", icon, lambda v: v.strip() if isinstance(v, str) and v.strip() else None)
    _apply("visibility", visibility,
           lambda v: v if isinstance(v, str) and v in _VALID_VIS else None)
    _apply("order", order, lambda v: int(v) if isinstance(v, (int, float, str)) and str(v).lstrip("-").isdigit() else None)

    if row:
        pages[page_id] = row
    else:
        pages.pop(page_id, None)
    _save(scope, dict(data, pages=pages))


def reload() -> None:
    """Drop the in-process cache (e.g. after an external edit to the files)."""
    with _lock:
        _cache["main"] = None
        _cache["admin"] = None

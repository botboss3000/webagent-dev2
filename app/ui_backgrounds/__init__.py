"""Animated-background catalog — the drop-in registry for app backgrounds.

╔══════════════════════════════════════════════════════════════════════════╗
║  THIS FILE IS CORE.  Backgrounds themselves are NOT defined here.          ║
║  Each background is a folder under ``ui/background/<id>/`` containing an    ║
║  ``<id>.json`` descriptor (+ ``<id>.js`` renderer, optional ``<id>.css``). ║
║  Drop a folder in → it auto-appears in the Appearance selector (App        ║
║  Settings) via GET /admin/settings/backgrounds. You do NOT register it     ║
║  anywhere. Mirrors app/ui_pages/__init__.py. See CLAUDE.md (Core vs.       ║
║  plugins) and ui/background/README.md.                                     ║
╚══════════════════════════════════════════════════════════════════════════╝

Descriptor contract (``ui/background/<id>/<id>.json``):
    {
      "id": "stargaze",        // stable id = folder name (defaults to it)
      "name": "Stargaze",      // label shown in the selector
      "description": "…",       // one-line blurb under the label
      "icon": "sparkles",      // Lucide icon name
      "status": "stable",      // stable | beta | experimental
      "order": 10,             // sort position (lower first)
      "themes": ["dark","light"] // hint: which themes it suits
    }

Descriptors are read SERVER-SIDE only; the browser always goes through the
catalog API. The built-in "none" option (plain background) is NOT a folder —
the selector UI adds it.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# app/ui_backgrounds/__init__.py → app/ui_backgrounds → app → repo root → ui/background
_REPO_ROOT = Path(__file__).resolve().parents[2]
_BG_DIR = _REPO_ROOT / "ui" / "background"

_DEFAULT_ICON = "image"
_DEFAULT_ORDER = 100

_CATALOG: Optional[List[Dict[str, Any]]] = None


def _load_descriptor(json_path: Path) -> Optional[Dict[str, Any]]:
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else None
    except Exception as e:  # noqa: BLE001
        logger.warning("Failed to parse background descriptor %s: %s", json_path, e)
        return None


def _entry_from_dir(folder: Path) -> Optional[Dict[str, Any]]:
    """Build a catalog entry from a background folder. The descriptor is
    ``<folder>.json`` by convention. Returns None when absent (not a background)."""
    json_path = folder / f"{folder.name}.json"
    if not json_path.is_file():
        return None
    desc = _load_descriptor(json_path)
    if desc is None:
        return None
    bid = str(desc.get("id") or folder.name)
    themes = desc.get("themes")
    if not isinstance(themes, list):
        themes = ["dark", "light"]
    return {
        "id": bid,
        "name": desc.get("name") or bid,
        "description": desc.get("description") or "",
        "icon": desc.get("icon") or _DEFAULT_ICON,
        "status": desc.get("status") or "stable",
        "order": desc.get("order", _DEFAULT_ORDER),
        "themes": [str(t) for t in themes],
    }


def _load(force: bool = False) -> List[Dict[str, Any]]:
    """Walk ui/background/*/<id>.json and build the sorted catalog list."""
    global _CATALOG
    if _CATALOG is not None and not force:
        return _CATALOG

    entries: List[Dict[str, Any]] = []
    if _BG_DIR.is_dir():
        for folder in sorted(_BG_DIR.iterdir()):
            if not folder.is_dir() or folder.name.startswith("_") or folder.name == "__pycache__":
                continue
            entry = _entry_from_dir(folder)
            if entry:
                entries.append(entry)

    entries.sort(key=lambda e: (e.get("order", _DEFAULT_ORDER), e["name"].lower()))
    _CATALOG = entries
    return entries


def reload() -> None:
    """Drop the cache so a newly-dropped background folder is picked up."""
    global _CATALOG
    _CATALOG = None
    _load(force=True)


def catalog() -> List[Dict[str, Any]]:
    """The list the Appearance selector renders (sorted)."""
    return list(_load())


def valid_ids() -> set[str]:
    """Background ids that currently exist on disk (excludes the built-in 'none')."""
    return {e["id"] for e in _load()}

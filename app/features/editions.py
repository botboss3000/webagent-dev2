"""Edition definitions + which features an edition switches on.

An **edition** is a named view over the feature catalog. ``full`` (the default)
includes everything; ``production`` includes only ``stable`` features; forks can
be any mix. See ``docs/claude/production-editions.md``.

Phase 1 is **report-only**: these helpers describe what an edition *would*
include, but nothing in the app actually filters on them yet.

Resolution order for the active edition:
    1. ``WEBAGENT_EDITION`` env var
    2. the ``active`` key in ``editions.json``
    3. ``"full"``
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_MANIFEST_PATH = Path(__file__).resolve().parent / "editions.json"

# Built-in fallback used if editions.json is missing or unreadable.
_DEFAULT_MANIFEST: Dict[str, Any] = {
    "active": None,
    "editions": {
        "full": {"label": "Full / dev", "description": "Every feature.", "rule": "all"},
        "production": {
            "label": "Production",
            "description": "Only stable features.",
            "rule": "min_status",
            "min_status": "stable",
            "include": [],
            "exclude": [],
        },
    },
}

# Maturity ranking — higher means more production-ready.
_STATUS_RANK = {"unknown": 0, "experimental": 1, "beta": 2, "stable": 3}


def _load_manifest() -> Dict[str, Any]:
    try:
        if _MANIFEST_PATH.exists():
            data = json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("editions"), dict):
                return data
    except Exception as e:  # never let a bad manifest break the report
        logger.warning("Could not read editions.json (%s); using defaults", e)
    return _DEFAULT_MANIFEST


def list_editions() -> Dict[str, Any]:
    """All defined editions: ``{name: {label, description, rule, ...}}``."""
    return dict(_load_manifest().get("editions", {}))


def get_active_edition() -> str:
    """The currently active edition name (defaults to ``full``)."""
    env = (os.environ.get("WEBAGENT_EDITION") or "").strip()
    if env:
        return env
    manifest = _load_manifest()
    active = manifest.get("active")
    if isinstance(active, str) and active.strip():
        return active.strip()
    return "full"


def _edition_def(edition: str) -> Dict[str, Any]:
    return _load_manifest().get("editions", {}).get(edition, {})


def edition_includes(status: str, feature_id: str, edition: Optional[str] = None) -> bool:
    """Would the given edition switch this feature on?

    ``status`` is the feature's maturity; ``feature_id`` lets explicit
    include/exclude lists override the status rule.
    """
    edition = edition or get_active_edition()
    spec = _edition_def(edition)
    if not spec:
        # Unknown edition name → behave like 'full' so nothing is hidden by a typo.
        return True

    excludes = set(spec.get("exclude") or [])
    includes = set(spec.get("include") or [])
    if feature_id in excludes:
        return False
    if feature_id in includes:
        return True

    rule = spec.get("rule", "all")
    if rule == "all":
        return True
    if rule == "min_status":
        floor = _STATUS_RANK.get(str(spec.get("min_status", "stable")), 3)
        return _STATUS_RANK.get(status, 0) >= floor
    return True


def edition_reason(status: str, feature_id: str, edition: Optional[str] = None) -> str:
    """Human-readable reason for the include/exclude decision (for the report)."""
    edition = edition or get_active_edition()
    spec = _edition_def(edition)
    if not spec:
        return f"edition '{edition}' is undefined — treated as full"
    if feature_id in set(spec.get("exclude") or []):
        return "explicitly excluded by this edition"
    if feature_id in set(spec.get("include") or []):
        return "explicitly promoted into this edition"
    rule = spec.get("rule", "all")
    if rule == "all":
        return "edition includes everything"
    if rule == "min_status":
        floor = str(spec.get("min_status", "stable"))
        if edition_includes(status, feature_id, edition):
            return f"status '{status}' meets the '{floor}' bar"
        return f"status '{status}' is below the '{floor}' bar"
    return "included"

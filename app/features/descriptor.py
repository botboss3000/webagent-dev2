"""The ``FEATURE`` self-description header and how to read it.

A plugin module may expose a module-level ``FEATURE`` dict describing itself:

    FEATURE = {
        "id": "gmail",                    # stable id; defaults to module stem
        "display_name": "Gmail",
        "category": "integration",        # see CATEGORIES
        "status": "stable",               # stable | beta | experimental
        "summary": "Read and send Gmail; list/search threads.",
        "requires": ["Google OAuth credentials"],
        # --- reserved for the ability-skill wiring (Phase 1/2) ---
        "skill": "Full how-to text…",     # optional skill body
        "skill_mode": "selectable",       # selectable | always_on
        "skill_handle": "gmail_a1b2c3d4", # minted-once stable handle (optional)
    }

All fields are optional. A module with no ``FEATURE`` is still catalogued, but
with ``status='unknown'`` and ``has_header=False`` so it is never silently
assumed production-ready.

This module is pure data — it must stay import-light so the catalog can read a
header without dragging in heavy dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Categories a feature can belong to. Mirrors the plugin folders.
CATEGORIES = (
    "integration",
    "event_source",
    "channel",
    "connector",
    "scheduler",
    "encryption",
    "payment",
    "secrets",
    "storage",
    "tool",
    "ability",
)

# Maturity levels. `unknown` is assigned to anything without a header so an
# un-annotated file is never treated as production-ready by accident.
STATUSES = ("stable", "beta", "experimental", "unknown")

SKILL_MODES = ("selectable", "always_on")


@dataclass
class FeatureDescriptor:
    """One catalogued capability."""

    id: str
    display_name: str
    category: str
    status: str = "unknown"
    summary: str = ""
    requires: List[str] = field(default_factory=list)
    # Origin / mechanics
    module: str = ""           # importable module name, when known
    drop_in: bool = True       # True = folder-scan add/remove; False = needs a registry edit
    has_header: bool = False   # did the module declare a FEATURE dict?
    error: Optional[str] = None  # set when the module failed to import / read
    # Skill bundle (reserved — wiring lands in Phase 1/2)
    has_skill: bool = False
    skill_mode: Optional[str] = None
    skill_handle: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "category": self.category,
            "status": self.status,
            "summary": self.summary,
            "requires": list(self.requires),
            "module": self.module,
            "drop_in": self.drop_in,
            "has_header": self.has_header,
            "error": self.error,
            "has_skill": self.has_skill,
            "skill_mode": self.skill_mode,
            "skill_handle": self.skill_handle,
        }


def _coerce_requires(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw if x]
    return []


def _humanize(stem: str) -> str:
    """Turn a module stem like 'email_tools' / 'google_cloud' into a label."""
    cleaned = stem.replace("_tools", "").replace("_source", "").replace("_secrets", "")
    cleaned = cleaned.strip("_") or stem
    return cleaned.replace("_", " ").title()


def normalize_feature(
    raw: Any,
    *,
    category: str,
    default_id: str,
    module: str = "",
    drop_in: bool = True,
) -> FeatureDescriptor:
    """Build a :class:`FeatureDescriptor` from a module's ``FEATURE`` dict.

    ``raw`` may be ``None`` (no header) — we still return a usable descriptor
    derived from the defaults, marked ``has_header=False`` / ``status='unknown'``.
    """
    raw = raw if isinstance(raw, dict) else {}
    has_header = bool(raw)

    fid = str(raw.get("id") or default_id)
    display = str(raw.get("display_name") or _humanize(fid))

    status = str(raw.get("status") or ("unknown" if not has_header else "stable")).lower()
    if status not in STATUSES:
        status = "unknown"

    cat = str(raw.get("category") or category)
    if cat not in CATEGORIES:
        cat = category

    skill_mode = raw.get("skill_mode")
    if skill_mode is not None:
        skill_mode = str(skill_mode).lower()
        if skill_mode not in SKILL_MODES:
            skill_mode = "selectable"

    return FeatureDescriptor(
        id=fid,
        display_name=display,
        category=cat,
        status=status,
        summary=str(raw.get("summary") or ""),
        requires=_coerce_requires(raw.get("requires")),
        module=module,
        drop_in=drop_in,
        has_header=has_header,
        has_skill=bool(raw.get("skill")),
        skill_mode=skill_mode,
        skill_handle=(str(raw["skill_handle"]) if raw.get("skill_handle") else None),
    )


def read_feature(mod: Any) -> Optional[Dict[str, Any]]:
    """Return a module's ``FEATURE`` dict if present and well-formed, else None."""
    raw = getattr(mod, "FEATURE", None)
    return raw if isinstance(raw, dict) else None

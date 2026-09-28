"""Deterministic capability profiles for provider prefix caching.

Profiles are nested deliberately: every Standard agent begins with the Simple
ability sequence, and every Advanced agent begins with Standard.  Specialized
tool schemas remain discoverable by default, so different profiles can share
the same small first-call schema while their textual ability menus share the
longest possible prefix.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, Iterable, List, Mapping, Sequence

PROFILE_VERSION = 1
PROMPT_LAYOUT_VERSION = 2
DEFAULT_PROFILE = "simple"
DEFAULT_WEBAGENT_PROFILE = "advanced"

SIMPLE_ABILITIES: tuple[str, ...] = (
    "web_access",
)

STANDARD_ADDITIONS: tuple[str, ...] = (
    "visualizer",
    "user_files",
    "image_generation",
)

ADVANCED_ADDITIONS: tuple[str, ...] = (
    "browser_control",
    "agent_orchestration",
    "automation",
    "create_tools",
    "agent_management",
    "codebase_admin",
    "diagnostics",
)

PROFILE_ABILITIES: Dict[str, tuple[str, ...]] = {
    "simple": SIMPLE_ABILITIES,
    "standard": SIMPLE_ABILITIES + STANDARD_ADDITIONS,
    "advanced": SIMPLE_ABILITIES + STANDARD_ADDITIONS + ADVANCED_ADDITIONS,
}

_PROFILE_SEQUENCE = PROFILE_ABILITIES["advanced"]
_PROFILE_POSITION = {ability_id: i for i, ability_id in enumerate(_PROFILE_SEQUENCE)}


def normalize_profile(value: Any, default: str = DEFAULT_PROFILE) -> str:
    profile = str(value or "").strip().lower()
    return profile if profile in PROFILE_ABILITIES else default


def profile_abilities(
    profile: str,
    extensions: Iterable[str] | None = None,
) -> List[str]:
    """Return a de-duplicated profile sequence followed by explicit extensions."""
    out = list(PROFILE_ABILITIES[normalize_profile(profile)])
    seen = set(out)
    for raw in ordered_ability_ids(extensions or ()):
        ability_id = str(raw or "").strip()
        if ability_id and ability_id not in seen:
            seen.add(ability_id)
            out.append(ability_id)
    return out


def profile_layer_blocks(
    profile: str,
    extensions: Iterable[str] | None = None,
    allowed_abilities: Iterable[str] | None = None,
) -> List[str]:
    """Render exact nested system-message blocks for provider prefix caching."""
    normalized = normalize_profile(profile)
    allowed = None if allowed_abilities is None else set(allowed_abilities)
    tier_rows = (
        ("SIMPLE", SIMPLE_ABILITIES),
        ("STANDARD", STANDARD_ADDITIONS),
        ("ADVANCED", ADVANCED_ADDITIONS),
    )
    max_tier = {"simple": 0, "standard": 1, "advanced": 2}[normalized]
    blocks = [
        "# [CAPABILITY LAYER: " + name + "]\n"
        "Canonical abilities:\n"
        + "\n".join(
            f"- `{ability_id}`"
            for ability_id in abilities
            if allowed is None or ability_id in allowed
        )
        for name, abilities in tier_rows[: max_tier + 1]
    ]
    blocks = [block for block in blocks if not block.endswith("Canonical abilities:\n")]
    base = set(PROFILE_ABILITIES[normalized])
    ordered_extensions = [
        ability_id
        for ability_id in ordered_ability_ids(extensions or ())
        if ability_id not in base and (allowed is None or ability_id in allowed)
    ]
    if ordered_extensions:
        blocks.append(
            "# [CAPABILITY LAYER: EXTENSIONS]\nCanonical abilities:\n"
            + "\n".join(f"- `{ability_id}`" for ability_id in ordered_extensions)
        )
    return blocks


def _catalog_position_map() -> Dict[str, int]:
    """Return the stable drop-in catalog order, falling back cleanly on import."""
    try:
        from app.abilities import ui_catalog

        cat = ui_catalog() or {}
        ordered: List[str] = []
        abilities = cat.get("abilities") or {}
        for group in cat.get("groups") or []:
            ordered.extend(a for a in group.get("members") or [] if a in abilities)
        ordered.extend(a for a in abilities if a not in set(ordered))
        return {ability_id: i for i, ability_id in enumerate(ordered)}
    except Exception:
        return {}


def ordered_ability_ids(ability_ids: Iterable[str]) -> List[str]:
    """Canonical cache order: nested profile abilities first, extensions next."""
    ids = {str(a).strip() for a in ability_ids if str(a or "").strip()}
    catalog_pos = _catalog_position_map()
    return sorted(
        ids,
        key=lambda ability_id: (
            0 if ability_id in _PROFILE_POSITION else 1,
            _PROFILE_POSITION.get(ability_id, catalog_pos.get(ability_id, 1_000_000)),
            ability_id,
        ),
    )


def with_cache_profile(
    metadata: Any,
    profile: str,
    extensions: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """Return metadata with cache/profile invariants applied."""
    if isinstance(metadata, str):
        try:
            meta = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            meta = {}
    else:
        meta = dict(metadata or {})
    if not isinstance(meta, dict):
        meta = {}

    normalized = normalize_profile(profile)
    base = set(PROFILE_ABILITIES[normalized])
    ordered_extensions = [
        ability_id
        for ability_id in ordered_ability_ids(extensions or ())
        if ability_id not in base
    ]
    meta.update(
        {
            "cache_family": normalized,
            "capability_profile": normalized,
            "cache_profile_version": PROFILE_VERSION,
            "prompt_layout_version": PROMPT_LAYOUT_VERSION,
            "capability_extensions": ordered_extensions,
            "discovery_default": "discoverable",
            "pre_enabled_connections": profile_abilities(
                normalized, ordered_extensions,
            ),
        }
    )
    return meta


def profile_from_metadata(metadata: Any, default: str = DEFAULT_PROFILE) -> str:
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata or "{}")
        except (TypeError, ValueError):
            metadata = {}
    if not isinstance(metadata, Mapping):
        return default
    return normalize_profile(
        metadata.get("capability_profile") or metadata.get("cache_family"),
        default,
    )


def stable_hash(value: Any) -> str:
    """Short diagnostic hash for prompt layers and canonical schemas."""
    if isinstance(value, str):
        payload = value
    else:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def cache_profile_summary(
    metadata: Any,
    enabled_abilities: Sequence[str],
    tool_definitions: Sequence[Mapping[str, Any]] | None = None,
) -> Dict[str, Any]:
    profile = profile_from_metadata(metadata)
    ordered = ordered_ability_ids(enabled_abilities)
    return {
        "profile": profile,
        "profile_version": PROFILE_VERSION,
        "prompt_layout_version": PROMPT_LAYOUT_VERSION,
        "ordered_abilities": ordered,
        "ability_hash": stable_hash(ordered),
        "tool_schema_hash": stable_hash(list(tool_definitions or ())),
    }

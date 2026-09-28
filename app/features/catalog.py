"""Scan the plugin folders and build the one feature catalog.

This is the runtime **discovery** the design calls for: the app reads every
plugin folder, picks up each feature's ``FEATURE`` header (or a sensible default
when there's none yet), and reports what code is available, how mature it is,
whether it's true drop-in, and whether the active edition switches it on.

Everything here is **lazy and fully guarded** — a broken or un-importable plugin
shows up as an error row; it can never break boot or the endpoint. Nothing in
this module changes how features actually load (Phase 1 is report-only).
"""

from __future__ import annotations

import importlib
import logging
from pathlib import Path
from typing import Any, Dict, List

from app.features.descriptor import FeatureDescriptor, normalize_feature, read_feature
from app.features.editions import (
    get_active_edition,
    list_editions,
    edition_includes,
    edition_reason,
)

logger = logging.getLogger(__name__)

# app/ directory (this file is app/features/catalog.py).
_APP_DIR = Path(__file__).resolve().parents[1]

# Folders scanned file-by-file. Each entry:
#   (category, relative folder, import package, drop_in?, skip-stems)
# `drop_in` records whether the subsystem already supports true folder-scan
# add/remove today (✅ rows in the scorecard) vs. needing a registry edit (⚠️).
_FOLDER_SPECS = [
    ("integration", "integrations", "app.integrations", True,
     {"oauth_helper", "ability_registry"}),
    ("event_source", "events/sources", "app.events.sources", True, set()),
    ("channel", "communications/plugins", "app.communications.plugins", True, set()),
    ("scheduler", "scheduler/providers", "app.scheduler.providers", False, set()),
    ("encryption", "encryption", "app.encryption", False,
     {"interface", "migration", "vault_keys"}),
    ("payment", "../plugins/billing/processors", "plugins.billing.processors", False, {"base"}),
    ("secrets", "secrets", "app.secrets", False, {"interface"}),
]

# Storage backends live in app/db alongside a lot of unrelated modules, so they
# are enumerated explicitly rather than file-scanned. (id, module). MySQL is
# schema-render/bootstrap only (no live backend module), so it isn't listed.
_STORAGE_BACKENDS = [
    ("sqlite", "app.db.local"),
    ("postgres", "app.db.postgres_backend"),
]

# Agent abilities — the per-agent tool bundles. As of the abilities drop-in
# refactor these live as self-describing files in plugins/abilities/ and are
# read via app.abilities.feature_descriptors(). This literal is only a fail-safe
# used if that scan is unavailable. (id, display_name, status, summary).
_ABILITIES = [
    ("web_access", "Web Access", "stable", "web_search, weather, maps geocoding (free public APIs)."),
    ("codebase_admin", "Codebase Admin", "stable", "read/write/edit/delete source, run commands, restart — privileged."),
    ("git_control", "Git Control", "stable", "GitHub source-control operations."),
    ("agent_management", "Agent Management", "stable", "user-scoped agent CRUD + prompt/ability edits."),
    ("diagnostics", "Diagnostics", "stable", "read the in-app flight-recorder."),
    ("agent_orchestration", "Agent Orchestration", "beta", "delegate to another agent + run the optimizer."),
    ("automation", "Automation", "beta", "scheduled tasks + event subscriptions."),
    ("browser_control", "Browser Control", "beta", "headless Playwright browser + arbitrary HTTP."),
    ("image_generation", "Image Generation", "beta", "generate_image via the configured image model."),
    ("visualizer", "Visualizer", "beta", "page-authoring tools for the Gen UI workspace."),
    ("ui_admin", "UI Admin", "beta", "admin UI configuration tools."),
    ("create_tools", "Create Tools", "beta", "define new DB-persisted tools at runtime."),
    ("terminal_control", "Terminal Control", "experimental", "drive interactive terminal programs (effectively shell access)."),
]


def _ability_features() -> List[FeatureDescriptor]:
    # Preferred: the drop-in ability files in plugins/abilities/.
    try:
        from app.abilities import feature_descriptors
        descriptors = feature_descriptors()
        if descriptors:
            return descriptors
    except Exception as e:
        logger.warning("Ability feature scan unavailable (%s); using fallback list", e)
    # Fail-safe: the static list above.
    out: List[FeatureDescriptor] = []
    for aid, display, status, summary in _ABILITIES:
        out.append(FeatureDescriptor(
            id=aid, display_name=display, category="ability", status=status,
            summary=summary, module="app.admin.integrations", drop_in=False,
            has_header=True,
        ))
    return out


def _scan_folder(
    category: str, rel: str, package: str, drop_in: bool, skip: set
) -> List[FeatureDescriptor]:
    out: List[FeatureDescriptor] = []
    folder = _APP_DIR / rel
    if not folder.is_dir():
        return out
    for fpath in sorted(folder.iterdir()):
        if fpath.suffix != ".py" or fpath.name.startswith("_"):
            continue
        stem = fpath.stem
        if stem in skip:
            continue
        module_name = f"{package}.{stem}"
        try:
            mod = importlib.import_module(module_name)
        except Exception as e:
            # The feature file exists but won't import (e.g. a missing optional
            # dependency). Surface it rather than hiding it.
            logger.debug("Feature module %s failed to import: %s", module_name, e)
            out.append(FeatureDescriptor(
                id=stem, display_name=stem, category=category, status="unknown",
                module=module_name, drop_in=drop_in, has_header=False,
                error=f"import failed: {e}",
            ))
            continue
        out.append(normalize_feature(
            read_feature(mod), category=category, default_id=stem,
            module=module_name, drop_in=drop_in,
        ))
    return out


def _storage_features() -> List[FeatureDescriptor]:
    out: List[FeatureDescriptor] = []
    for fid, module_name in _STORAGE_BACKENDS:
        try:
            mod = importlib.import_module(module_name)
            header = read_feature(mod)
        except Exception as e:
            out.append(FeatureDescriptor(
                id=fid, display_name=fid.title(), category="storage", status="unknown",
                module=module_name, drop_in=False, has_header=False,
                error=f"import failed: {e}",
            ))
            continue
        out.append(normalize_feature(
            header, category="storage", default_id=fid,
            module=module_name, drop_in=False,
        ))
    return out


def _all_descriptors() -> List[FeatureDescriptor]:
    features: List[FeatureDescriptor] = []
    for category, rel, package, drop_in, skip in _FOLDER_SPECS:
        try:
            features.extend(_scan_folder(category, rel, package, drop_in, skip))
        except Exception as e:  # one bad folder must not sink the whole catalog
            logger.warning("Feature scan failed for %s: %s", category, e)
    try:
        features.extend(_storage_features())
    except Exception as e:
        logger.warning("Storage feature scan failed: %s", e)
    try:
        features.extend(_ability_features())
    except Exception as e:
        logger.warning("Ability feature list failed: %s", e)
    return features


def build_catalog() -> Dict[str, Any]:
    """Build the full catalog payload for the report / endpoint.

    Returns active edition, the edition definitions, and a flat list of features
    each annotated with whether the active edition would switch it on (+ why).
    Phase 1: the ``included`` flag is informational only — nothing is gated.
    """
    edition = get_active_edition()
    descriptors = _all_descriptors()

    items: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {}
    for d in descriptors:
        row = d.to_dict()
        row["included"] = edition_includes(d.status, d.id, edition)
        row["reason"] = edition_reason(d.status, d.id, edition)
        items.append(row)
        counts[d.status] = counts.get(d.status, 0) + 1

    items.sort(key=lambda r: (r["category"], r["display_name"].lower()))

    return {
        "active_edition": edition,
        "editions": list_editions(),
        "gating_active": False,  # Phase 1 — report only
        "counts": {
            "total": len(items),
            "by_status": counts,
            "included": sum(1 for r in items if r["included"]),
            "with_header": sum(1 for r in items if r["has_header"]),
            "drop_in": sum(1 for r in items if r["drop_in"]),
            "errors": sum(1 for r in items if r["error"]),
        },
        "features": items,
    }

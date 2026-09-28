"""Deterministic seeding of the JSON config *library* from in-code defaults.

The app boots fine with `data/config/` absent — every reader falls back to a
built-in default and the writers create the folder lazily (see
:mod:`app.util.config_io` and :mod:`app.util.paths`). This module is the
**inverse, opt-in** action: an admin who wants a populated, editable
`data/config/` to work from can have the app **write out the whole library** of
config files from the canonical defaults each owning module already defines.

Design (decided with the user):
  * **The app generates them deterministically** from in-code templates — never a
    live LLM (which would need a provider config that may itself be missing).
  * **Admin-triggered + preview-first** — :func:`plan` shows what a run would do;
    :func:`seed_missing` performs it. Wired to an Admin Tools button.
  * **Never overwrites** — an existing file is skipped, never clobbered, so a run
    is always safe to repeat (it only fills gaps).

Where each default lives: in its OWNING module (the "instructions in code"), so
the default and the code that reads it can't drift. This registry only COLLECTS
them. Files that manage their own lifecycle — self-bootstrapping, secret-bearing,
or network-fetched — are listed as *auto-managed*: shown in the plan for a
complete picture, but never machine-written here.
"""

from __future__ import annotations

import os
from typing import Any, Dict, List

from app.util.config_io import safe_write_json

__all__ = ["CONFIG_DIR", "plan", "seed_missing"]

# app/util/config_seed.py → app/util → app → repo root → data/config
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_DIR = os.path.join(_REPO_ROOT, "data", "config")


def _config_path(name: str) -> str:
    return os.path.join(CONFIG_DIR, name)


def _seedable() -> List[Dict[str, Any]]:
    """The files the seeder writes defaults for.

    Each ``default`` is sourced from its owning module so it stays in sync with
    the code that reads it. Imported lazily to avoid import cycles at app boot.
    """
    from app.admin import page_config, debug_config, scheduler_config
    from app.optimizer import config as optimizer_config
    from app.agent import suggestions

    return [
        {
            "name": "main-panel-pages.json",
            "title": "Main header tabs — per-page overrides",
            "default": page_config._empty(),
        },
        {
            "name": "admin-panel-pages.json",
            "title": "Admin Tools views — per-page overrides",
            "default": page_config._empty(),
        },
        {
            "name": "optimizer.json",
            "title": "Optimizer settings",
            "default": dict(optimizer_config.DEFAULTS),
        },
        {
            "name": "scheduler_config.json",
            "title": "Scheduler backend",
            "default": dict(scheduler_config.DEFAULT_CONFIG),
        },
        {
            "name": "debug-config.json",
            "title": "Debug knobs (override-only)",
            "default": dict(debug_config.DEFAULT_DEBUG_CONFIG),
        },
        {
            "name": "suggestions.json",
            "title": "Suggested-replies tunables",
            "default": dict(suggestions._DEFAULTS),
        },
    ]


# Part of the config library, but each manages its own lifecycle — shown in the
# plan so the admin sees the whole picture, but NEVER machine-written here.
_AUTO_MANAGED: List[Dict[str, str]] = [
    {
        "name": "agent-abilities.json",
        "title": "Ability toggles + tool defaults",
        "reason": "self-seeds from the ability catalog on first run",
    },
    {
        "name": "app-settings.json",
        "title": "Global app settings",
        "reason": "managed by the App Settings UI; absent = built-in defaults",
    },
    {
        "name": "provider.json",
        "title": "Shared GitHub token",
        "reason": "holds a shared deploy token — set it via the Git settings UI",
    },
    {
        "name": "model_catalog.json",
        "title": "Model metadata catalog",
        "reason": "fetched from the network on demand",
    },
    {
        "name": "remote_access.json",
        "title": "Remote-access tunnel",
        "reason": "written by the Remote Access panel; absent = off",
    },
]


def plan() -> Dict[str, Any]:
    """Preview a seed run without writing anything.

    Returns the full library split into: ``will_write`` (missing seedable files
    that a run would create), ``already_exist`` (seedable files a run would skip),
    and ``auto_managed`` (files this action never writes, each with a reason).
    """
    will_write: List[Dict[str, Any]] = []
    already: List[Dict[str, Any]] = []
    for e in _seedable():
        exists = os.path.exists(_config_path(e["name"]))
        row = {"name": e["name"], "title": e["title"], "exists": exists}
        (already if exists else will_write).append(row)
    auto = [
        {
            "name": e["name"],
            "title": e["title"],
            "reason": e["reason"],
            "exists": os.path.exists(_config_path(e["name"])),
        }
        for e in _AUTO_MANAGED
    ]
    return {
        "config_dir": CONFIG_DIR,
        "will_write": will_write,
        "already_exist": already,
        "auto_managed": auto,
    }


def seed_missing() -> Dict[str, Any]:
    """Write the default for every seedable file that is missing.

    Never overwrites an existing file (skip-and-report). Each write goes through
    :func:`app.util.config_io.safe_write_json`, so `data/config/` is created on
    demand and writes are atomic. Returns a summary of what happened.
    """
    written: List[str] = []
    skipped: List[str] = []
    failed: List[Dict[str, str]] = []
    for e in _seedable():
        path = _config_path(e["name"])
        if os.path.exists(path):
            skipped.append(e["name"])
            continue
        try:
            safe_write_json(path, e["default"])
            written.append(e["name"])
        except Exception as ex:  # pragma: no cover - defensive
            failed.append({"name": e["name"], "error": str(ex)})
    return {
        "config_dir": CONFIG_DIR,
        "written": written,
        "skipped": skipped,
        "failed": failed,
    }

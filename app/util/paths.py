"""Resolve read-only *seed* data that must ship even with no ``data/`` folder.

The app is meant to ship with **no ``data/`` folder** — runtime state materializes
lazily (see :mod:`app.util.config_io`). But a handful of files are not runtime
state: they are **read-only defaults the app needs at boot** — the app-prompts
catalog and the agent-template seeds. Those are bundled under ``app/defaults/``
(tracked, always present) instead of ``data/`` so a fresh checkout with no
``data/`` folder still boots with working prompts and a default agent.

Resolution rule for every seed below: **prefer an existing ``data/`` copy** (so an
existing install — or a deployment that drops in a customized override — keeps
using it), and **fall back to the bundled ``app/defaults/`` copy** otherwise. Only
one normally exists, so the order just decides which wins if both are present.

Centralizing the paths here means a future relocation is a one-file edit instead
of touching the ~7 modules that read the app-prompts catalog.
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = [
    "DEFAULTS_DIR", "app_prompts_path", "agents_seed_dir", "project_root",
    "dashboard_default_path", "dashboard_default_override_path",
]

# app/util/paths.py → app/util → app → repo root
_APP_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _APP_DIR.parent

# Bundled, always-tracked read-only defaults (ship even when data/ is absent).
DEFAULTS_DIR = _APP_DIR / "defaults"

# Legacy / override locations under the runtime data/ tree.
_DATA_APP_PROMPTS = _REPO_ROOT / "data" / "config" / "app-prompts.json"
_DATA_AGENTS_DIR = _REPO_ROOT / "data" / "agents"
_DATA_DASHBOARD_DEFAULT = _REPO_ROOT / "data" / "config" / "dashboard-default.json"


def project_root() -> str:
    """Absolute path to the app's own project directory (the repo root the server
    runs in). This is the natural default working folder for a Local Claude Code
    agent — point it at *this* project unless the admin chooses another folder.
    Machine-relative (derived from this file's location), so it follows wherever
    the app is installed instead of being a hard-coded path.
    """
    return str(_REPO_ROOT)


def app_prompts_path() -> Path:
    """Path to the app-prompts catalog (the UI-handoff / runtime-fragment /
    app-level-prompt / page-assistant text catalog).

    Returns the ``data/config/app-prompts.json`` copy if it exists (back-compat
    for installs that still carry one), else the bundled
    ``app/defaults/app-prompts.json`` that always ships.
    """
    if _DATA_APP_PROMPTS.is_file():
        return _DATA_APP_PROMPTS
    return DEFAULTS_DIR / "app-prompts.json"


def dashboard_default_path() -> Path:
    """Path to the Admin Dashboard's **default layout** seed (the cards a fresh
    admin — or a "reset to default" — starts from).

    Returns the runtime override ``data/config/dashboard-default.json`` if an admin
    has saved one (via the dashboard's "Save as default" button), else the bundled
    ``app/defaults/dashboard.json`` that always ships. Same override-or-default rule
    as the agent-template seeds.
    """
    if _DATA_DASHBOARD_DEFAULT.is_file():
        return _DATA_DASHBOARD_DEFAULT
    return DEFAULTS_DIR / "dashboard.json"


def dashboard_default_override_path() -> Path:
    """Where "Save as default" WRITES the current layout — always the runtime
    ``data/config/`` override, never the tracked bundled seed (which stays pristine
    so a fresh checkout still has a sane factory default)."""
    return _DATA_DASHBOARD_DEFAULT


def agents_seed_dir() -> str:
    """Directory the DB seeder scans for agent-template ``*.json`` files.

    Returns ``data/agents/`` if it exists and holds at least one ``.json`` file
    (back-compat / deployment override), else the bundled
    ``app/defaults/agents/`` that always ships the default + system agents.
    """
    try:
        if _DATA_AGENTS_DIR.is_dir() and any(
            f.lower().endswith(".json") for f in os.listdir(_DATA_AGENTS_DIR)
        ):
            return str(_DATA_AGENTS_DIR)
    except OSError:
        pass
    return str(DEFAULTS_DIR / "agents")

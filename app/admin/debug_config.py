"""Consolidated debug knobs — ``data/config/debug-config.json``.

The app's debugging controls used to be split across two mechanisms: a couple of
environment variables read at boot in ``app/main.py`` (``LOG_LEVEL``,
``DIAGNOSTICS_CAPTURE_LEVEL``) and a handful of keys scattered through
``app-settings.json`` (run limits + the recorders/watchdog). This module gives
them a single home.

``debug-config.json`` is an **override-only** file: every knob defaults to
``null``, which means "leave it to the normal source" (the env var, the Admin
Tools > App Settings UI, or the built-in default). Setting a value FORCES it from
this file, winning over the normal source at the point each value is consumed.

So an operator who wants to crank up debugging edits one file; nothing else has
to change, and with the shipped all-``null`` file the app behaves exactly as
before. The file is tiny, so it is re-read on each call (the run-limit and
recorder/watchdog overrides therefore take effect live). The two logging levels
are read once at boot in ``main.py``, so changing those needs a server restart.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

DEBUG_CONFIG_FILE = "data/config/debug-config.json"

# The app-settings-style knobs this file can override at runtime. These are
# merged ON TOP of the effective app settings at each consumer (the agent loop,
# the watchdog, the render recorder, the stream buffer). The two logging levels
# are intentionally NOT here — they are read at boot via log_level() /
# diagnostics_capture_level() below.
_OVERRIDE_KEYS = (
    "max_tool_calls",
    "max_wall_seconds",
    "max_identical_tool_calls",
    "stream_buffer_retention_seconds",
    "run_watchdog_enabled",
)

# Canonical default template written by the config-library seeder
# (app/util/config_seed.py) when this file is missing. Every knob is ``null`` =
# "leave it to the normal source" (env var / App Settings UI / built-in default);
# an operator flips one to FORCE it. The all-null template therefore behaves
# exactly like an absent file — it just makes the available knobs discoverable.
# Grouped layout (logging / run_limits / recorders); _flatten() reads both this
# and a flat layout, and ignores the underscore-prefixed notes.
DEFAULT_DEBUG_CONFIG: Dict[str, Any] = {
    "_README": (
        "Override-only debug knobs. null = use the normal source (env var / "
        "App Settings / built-in default); set a value to FORCE it. The two "
        "logging levels apply at boot (restart to change); the rest are re-read live."
    ),
    "logging": {
        "log_level": None,                  # e.g. "DEBUG" / "INFO" / "WARNING"
        "diagnostics_capture_level": None,  # min level mirrored to the flight-recorder
    },
    "run_limits": {
        "max_tool_calls": None,
        "max_wall_seconds": None,
        "max_identical_tool_calls": None,
    },
    "recorders": {
        "stream_buffer_retention_seconds": None,
        "run_watchdog_enabled": None,
    },
}


def load_debug_config() -> Dict[str, Any]:
    """Read debug-config.json. Never raises — returns {} if missing/invalid."""
    try:
        if os.path.exists(DEBUG_CONFIG_FILE):
            with open(DEBUG_CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f) or {}
    except Exception as e:  # boot/runtime must never break on a bad debug file
        logger.debug("Failed to read %s: %s", DEBUG_CONFIG_FILE, e)
    return {}


def _flatten(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """Collapse the grouped file into a flat {key: value} map.

    Keys beginning with ``_`` (``_README``, per-group ``_note``) are ignored, and
    both the grouped layout (``{"run_limits": {"max_tool_calls": 25}}``) and a
    flat layout (``{"max_tool_calls": 25}``) are accepted.
    """
    flat: Dict[str, Any] = {}
    for k, v in cfg.items():
        if isinstance(k, str) and k.startswith("_"):
            continue
        if isinstance(v, dict):
            for ik, iv in v.items():
                if isinstance(ik, str) and ik.startswith("_"):
                    continue
                flat[ik] = iv
        else:
            flat[k] = v
    return flat


def debug_overrides() -> Dict[str, Any]:
    """Return only the run-limit / recorder overrides that are explicitly set.

    The result is a flat dict ready to merge ON TOP of the effective app
    settings, e.g. ``{**app_settings, **debug_overrides()}``. It is empty when the
    file is missing or every knob is left at ``null`` (the shipped default), so
    merging it is a no-op in the normal case.
    """
    flat = _flatten(load_debug_config())
    return {k: flat[k] for k in _OVERRIDE_KEYS if flat.get(k) is not None}


def log_level(default: Optional[str] = None) -> Optional[str]:
    """The forced console log level name from debug-config.json, or ``default``.

    Returns a level NAME (e.g. ``"DEBUG"``) so the caller can fall through to the
    ``LOG_LEVEL`` env var before applying a built-in default.
    """
    v = _flatten(load_debug_config()).get("log_level")
    return v if isinstance(v, str) and v.strip() else default


def diagnostics_capture_level(default: Optional[str] = None) -> Optional[str]:
    """The forced flight-recorder capture level name, or ``default``.

    Mirrors :func:`log_level`; falls through to ``DIAGNOSTICS_CAPTURE_LEVEL``.
    """
    v = _flatten(load_debug_config()).get("diagnostics_capture_level")
    return v if isinstance(v, str) and v.strip() else default

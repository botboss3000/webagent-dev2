"""
Load/save optimizer.json — configuration for the skill optimizer.
Lives in project root, same pattern as app-settings.json.

CONFIGURABLE VALUES:
- mode: "on" (auto-run) or "off" (manual only).
        Legacy "live" reads as "on"; legacy "scheduled" reads as "off".
- schedule.min_interactions: N — when on, only auto-run the optimizer for a
        session once it has MORE than N completed turns (shorter sessions are
        skipped). 0 = no minimum (every session). Default 5.
- user_feedback: "always" / "on_failure" / "never"
- sessions: show_in_list (bool), allow_pause_for_input (bool)
- models: analyzer, proposer, validator model names (all use same provider/key as main agent)
- target_metrics: list of enabled metrics
- intensity: 1-5 (conservative → aggressive) — adjusts thresholds automatically
- app_wide: min_sample, min_skill_age_days, auto_deploy
- per_user: min_sample, min_skill_age_days, auto_deploy
- notifications: notify_user, notify_devs, channel
- state: last_run_at, improvements_deployed, last_run_status
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_PATH = PROJECT_ROOT / "data" / "config" / "optimizer.json"


DEFAULTS: Dict[str, Any] = {
    "mode": "off",
    "user_feedback": "always",
    "intensity": 3,
    "sessions": {
        "show_in_list": True,
        "allow_pause_for_input": True,
    },
    "schedule": {
        "interval": "per-interaction",
        "min_interactions": 5,   # only auto-run once a session has > N turns
    },
    "models": {
        "analyzer": None,   # None = use same as main agent
        "proposer": None,
        "validator": None,
    },
    "trials": {"per_change": 2},
    "max_iterations": 2,
    "target_metrics": ["turns", "tokens", "time", "failures", "rating"],
    "app_wide": {
        "auto_deploy": True,
        "min_sample": 5,         # low for dev/testing
        "min_skill_age_days": 0,  # allow immediate optimization in dev
    },
    "per_user": {
        "auto_deploy": True,
        "min_sample": 3,
        "min_skill_age_days": 0,
    },
    "notifications": {
        "notify_user": True,
        "notify_devs": False,
        "channel": "email",
    },
    "state": {
        "last_run_at": None,
        "improvements_deployed": 0,
        "last_run_status": None,
    },
}


def load_config() -> dict:
    """Load optimizer config, merging with defaults for missing keys."""
    cfg = dict(DEFAULTS)
    if CONFIG_PATH.is_file():
        try:
            raw = CONFIG_PATH.read_text(encoding="utf-8")
            loaded = json.loads(raw)
            _deep_merge(cfg, loaded)
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load optimizer.json: %s", e)
    return cfg


def save_config(data: dict) -> None:
    """Save optimizer config to disk."""
    try:
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except OSError as e:
        logger.error("Failed to save optimizer.json: %s", e)
        raise


def update_state(**kwargs) -> None:
    """Update just the state section of the config."""
    cfg = load_config()
    cfg.setdefault("state", {}).update(kwargs)
    save_config(cfg)


def optimizer_enabled(cfg: Optional[dict] = None) -> bool:
    """True when the optimizer should auto-run. mode 'on' (or legacy 'live').
    Everything else — 'off', legacy 'scheduled', blank — means manual-only."""
    cfg = cfg if cfg is not None else load_config()
    return str(cfg.get("mode") or "").strip().lower() in ("on", "live")


def optimizer_min_turns(cfg: Optional[dict] = None) -> int:
    """Minimum session length, in completed user turns, before the optimizer
    auto-runs. It runs only for sessions with MORE than this many turns; 0 means
    no minimum (every session). Stored as schedule.min_interactions."""
    cfg = cfg if cfg is not None else load_config()
    try:
        n = int((cfg.get("schedule") or {}).get("min_interactions"))
    except (TypeError, ValueError):
        n = 0
    return max(0, n)


def _deep_merge(base: dict, override: dict) -> None:
    """Merge override into base in-place, recursively."""
    for key, val in override.items():
        if key in base and isinstance(base[key], dict) and isinstance(val, dict):
            _deep_merge(base[key], val)
        else:
            base[key] = val

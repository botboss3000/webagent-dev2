"""Versioned per-flag policy authority for Active Session Bonus."""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any, Mapping

from app.agent.closing_message import configured_flags
from app.agent.gate_registry import gate_types
from app.runtime_mode import data_root

POLICY_VERSION = 1
ACTIONS = ("open_reset", "continue", "complete")
DEFAULT_MAX_BONUS = 60
DEFAULT_RESOLUTION_SECONDS = 86400
_MIN_BONUS = 1
_MAX_BONUS = 10000
_MIN_RESOLUTION_SECONDS = 60
_MAX_RESOLUTION_SECONDS = 30 * 24 * 60 * 60


def authoritative_flags() -> list[str]:
    """Return the app-wide closing-message taxonomy, never an agent-local list."""
    descriptor = gate_types().get("closing_message")
    flags = configured_flags({}, descriptor)
    # The gate rejects duplicate flags, but preserve a defensive unique ordering
    # because policy equality depends on one canonical sequence.
    return list(dict.fromkeys(str(flag).strip().lower() for flag in flags if str(flag).strip()))


def default_policy(flag: str) -> dict[str, Any]:
    # Preserve the existing behavior for the shipped needs_input taxonomy while
    # making every other current flag explicitly configurable on first load.
    return {
        "flag": flag,
        "max_bonus": DEFAULT_MAX_BONUS,
        "resolution_seconds": DEFAULT_RESOLUTION_SECONDS,
        "resolution_action": "continue" if flag == "needs_input" else "open_reset",
    }


def default_policies(flags: list[str] | None = None) -> list[dict[str, Any]]:
    return [default_policy(flag) for flag in (flags if flags is not None else authoritative_flags())]


class PolicyValidationError(ValueError):
    pass


class PolicyStore:
    """Small JSON authority deliberately independent of the global gate file."""
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or (data_root() / "config" / "active-session-bonus-policies.json"))
        self._lock = threading.RLock()

    def _read(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def current(self) -> dict[str, Any]:
        flags = authoritative_flags()
        raw = self._read()
        policies = raw.get("policies") if raw else None
        if not isinstance(policies, list):
            return {"version": POLICY_VERSION, "flags": flags, "policies": default_policies(flags), "persisted": False, "matches": True}
        try:
            checked = validate_policies(policies, flags)
        except PolicyValidationError:
            return {"version": POLICY_VERSION, "flags": flags, "policies": default_policies(flags), "persisted": True, "matches": False}
        return {"version": POLICY_VERSION, "flags": flags, "policies": checked, "persisted": True, "matches": True}

    def save(self, policies: object) -> dict[str, Any]:
        flags = authoritative_flags()
        checked = validate_policies(policies, flags)
        payload = {"version": POLICY_VERSION, "policies": checked}
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(self.path.suffix + ".tmp")
            temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            temp.replace(self.path)
        return {"version": POLICY_VERSION, "flags": flags, "policies": checked, "persisted": True, "matches": True}


def validate_policies(value: object, flags: list[str] | None = None) -> list[dict[str, Any]]:
    expected = flags if flags is not None else authoritative_flags()
    if not isinstance(value, list):
        raise PolicyValidationError("policies must be a list")
    seen: set[str] = set()
    normalized: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, Mapping):
            raise PolicyValidationError("every policy must be an object")
        flag = str(item.get("flag") or "").strip().lower()
        if not flag or flag in seen:
            raise PolicyValidationError("policies contain a missing or duplicate flag")
        seen.add(flag)
        try:
            maximum = int(item.get("max_bonus"))
            seconds = int(item.get("resolution_seconds"))
        except (TypeError, ValueError):
            raise PolicyValidationError("maximum bonus and timer must be integers") from None
        action = str(item.get("resolution_action") or "")
        if not (_MIN_BONUS <= maximum <= _MAX_BONUS):
            raise PolicyValidationError(f"maximum bonus must be between {_MIN_BONUS} and {_MAX_BONUS}")
        if not (_MIN_RESOLUTION_SECONDS <= seconds <= _MAX_RESOLUTION_SECONDS):
            raise PolicyValidationError("timer must be between 60 seconds and 30 days")
        if action not in ACTIONS:
            raise PolicyValidationError("resolution action is invalid")
        normalized[flag] = {"flag": flag, "max_bonus": maximum, "resolution_seconds": seconds, "resolution_action": action}
    if set(normalized) != set(expected):
        raise PolicyValidationError("policy flags must exactly match the current closing-message flag list")
    return [normalized[flag] for flag in expected]


_STORE: PolicyStore | None = None

def get_policy_store() -> PolicyStore:
    global _STORE
    if _STORE is None:
        _STORE = PolicyStore()
    return _STORE


def policy_for_flag(flag: str) -> dict[str, Any] | None:
    state = get_policy_store().current()
    if not state["matches"]:
        return None
    return next((policy for policy in state["policies"] if policy["flag"] == flag), None)

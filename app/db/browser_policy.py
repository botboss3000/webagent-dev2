"""Administrator-visible browser storage and retention policy.

The policy is deliberately independent from the two browser feature gates.
Changing a limit never enables browser authority or persistent browser caching.
Environment values win when configuration is env-locked; otherwise an atomic
JSON file supplies runtime-editable values for the admin UI.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


_ROOT = Path(__file__).resolve().parents[2]
_POLICY_PATH = _ROOT / "data" / "config" / "browser_storage_policy.json"


@dataclass(frozen=True)
class BrowserStoragePolicy:
    persistence_mode: str = "persistent_cache"
    policy_epoch: int = 1
    cache_schema_version: int = 4
    metadata_ttl_seconds: int = 0
    transcript_ttl_seconds: int = 0
    transcript_retention_mode: str = "quota_only"
    run_state_ttl_seconds: int = 0
    generated_html_ttl_seconds: int = 0
    max_cache_bytes: int = 512 * 1024 * 1024
    tombstone_retention_days: int = 90
    receipt_retention_days: int = 30
    turn_reservation_retention_hours: int = 24
    telemetry_enabled: bool = True
    telemetry_redact_payloads: bool = True
    export_enabled: bool = True
    delete_enabled: bool = True


_DEFAULT = BrowserStoragePolicy()
_INT_LIMITS = {
    "policy_epoch": (0, 2**31 - 1),
    "cache_schema_version": (1, 100),
    # Deprecated wire-compatible fields. Cache expiry is disabled globally;
    # quota pressure, logout and explicit deletion are separate policies.
    "metadata_ttl_seconds": (0, 0),
    "transcript_ttl_seconds": (0, 0),
    "run_state_ttl_seconds": (0, 0),
    "generated_html_ttl_seconds": (0, 0),
    "max_cache_bytes": (1 * 1024 * 1024, 2 * 1024 * 1024 * 1024),
    "tombstone_retention_days": (1, 3650),
    "receipt_retention_days": (1, 3650),
    "turn_reservation_retention_hours": (1, 24 * 365),
}
_PERSISTENCE_MODES = {"persistent_cache", "memory_only", "disabled"}
_BOOL_FIELDS = {
    "telemetry_enabled",
    "telemetry_redact_payloads",
    "export_enabled",
    "delete_enabled",
}
_ENV_FIELDS = {
    "policy_epoch": "WEBAGENT_BROWSER_POLICY_EPOCH",
    "metadata_ttl_seconds": "WEBAGENT_BROWSER_METADATA_TTL_SECONDS",
    "transcript_ttl_seconds": "WEBAGENT_BROWSER_TRANSCRIPT_TTL_SECONDS",
    "run_state_ttl_seconds": "WEBAGENT_BROWSER_RUN_STATE_TTL_SECONDS",
    "generated_html_ttl_seconds": "WEBAGENT_BROWSER_GENERATED_HTML_TTL_SECONDS",
    "max_cache_bytes": "WEBAGENT_BROWSER_CACHE_MAX_BYTES",
    "tombstone_retention_days": "WEBAGENT_BROWSER_TOMBSTONE_DAYS",
    "receipt_retention_days": "WEBAGENT_BROWSER_SYNC_RECEIPT_DAYS",
    "turn_reservation_retention_hours": "WEBAGENT_TURN_RESERVATION_HOURS",
}
_ENV_MODE_FIELD = "WEBAGENT_BROWSER_STORAGE_MODE"
_ENV_BOOL_FIELDS = {
    "telemetry_enabled": "WEBAGENT_BROWSER_TELEMETRY_ENABLED",
    "telemetry_redact_payloads": "WEBAGENT_BROWSER_TELEMETRY_REDACT_PAYLOADS",
    "export_enabled": "WEBAGENT_BROWSER_EXPORT_ENABLED",
    "delete_enabled": "WEBAGENT_BROWSER_DELETE_ENABLED",
}


def _coerce(raw: dict[str, Any]) -> BrowserStoragePolicy:
    values = asdict(_DEFAULT)
    mode = str(raw.get("persistence_mode", "")).strip().lower()
    if mode in _PERSISTENCE_MODES:
        values["persistence_mode"] = mode
    for key, (minimum, maximum) in _INT_LIMITS.items():
        if key not in raw:
            continue
        try:
            values[key] = max(minimum, min(maximum, int(raw[key])))
        except (TypeError, ValueError):
            pass
    for key in _BOOL_FIELDS:
        if key in raw and isinstance(raw[key], bool):
            values[key] = raw[key]
    return BrowserStoragePolicy(**values)


def load_browser_storage_policy() -> BrowserStoragePolicy:
    raw: dict[str, Any] = {}
    try:
        value = json.loads(_POLICY_PATH.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            raw = value
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    # An env-locked deployment is configured exclusively by its environment.
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        raw = {}
    for key, env_name in _ENV_FIELDS.items():
        if os.environ.get(env_name):
            raw[key] = os.environ[env_name]
    for key, env_name in _ENV_BOOL_FIELDS.items():
        value = os.environ.get(env_name)
        if value is not None:
            raw[key] = value.strip().lower() in {"1", "true", "yes", "on"}
    if os.environ.get(_ENV_MODE_FIELD):
        raw["persistence_mode"] = os.environ[_ENV_MODE_FIELD]
    return _coerce(raw)


def save_browser_storage_policy(raw: dict[str, Any]) -> BrowserStoragePolicy:
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        raise PermissionError("Browser storage policy is locked by environment configuration")
    policy = _coerce(raw)
    _POLICY_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=".browser-storage-policy-",
        suffix=".json",
        dir=str(_POLICY_PATH.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(policy), handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, _POLICY_PATH)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    return policy


def public_browser_cache_policy() -> dict[str, Any]:
    policy = load_browser_storage_policy()
    return {
        "persistence_mode": policy.persistence_mode,
        "policy_epoch": policy.policy_epoch,
        "schema_version": policy.cache_schema_version,
        "metadata_ttl_seconds": policy.metadata_ttl_seconds,
        "transcript_ttl_seconds": policy.transcript_ttl_seconds,
        "transcript_retention_mode": policy.transcript_retention_mode,
        "run_state_ttl_seconds": policy.run_state_ttl_seconds,
        "generated_html_ttl_seconds": policy.generated_html_ttl_seconds,
        "max_bytes": policy.max_cache_bytes,
        "telemetry_enabled": policy.telemetry_enabled,
        "telemetry_redacted": policy.telemetry_redact_payloads,
    }


def browser_storage_policy_dict() -> dict[str, Any]:
    return asdict(load_browser_storage_policy())


def require_export_enabled() -> None:
    if not load_browser_storage_policy().export_enabled:
        raise PermissionError("Data export is disabled by administrator policy")


def require_delete_enabled() -> None:
    if not load_browser_storage_policy().delete_enabled:
        raise PermissionError("Data deletion is disabled by administrator policy")

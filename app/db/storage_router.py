"""
Storage Router — reads ``data/config/storage_routing.json`` and answers
"where does this data function live?" at runtime.

The routing table maps each data function (session_data, session_tools,
agent_data, user_data, vault, genui_pages, attachments) to one of three
backends: ``browser`` (IndexedDB, no server writes), ``server`` (per-user
SQLite), or ``postgres`` (remote PostgreSQL).

Used by:
- ``app/api/chat.py`` → decides which DB backend to pass to the agent loop
- ``app/api/browser_storage.py`` → decides whether browser-authority mode is active
- Client config endpoint → exposes routing so the browser knows where to send
"""

from __future__ import annotations

import json
import logging
import os
from typing import Dict, Optional

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ROUTING_PATH = os.path.join(_PROJECT_ROOT, "data", "config", "storage_routing.json")

_DEFAULTS: Dict[str, str] = {
    "session_data":   "server",
    "session_tools":  "server",
    "session_cache":  "server",
    "agent_data":     "server",
    "user_data":      "server",
    "vault":          "server",
    "genui_pages":    "server",
    "attachments":    "server",
    "local_instance": "server",   # app-plane state pinned to this machine
    "app_shared_data": "server",  # app-plane data eligible for remote sync
}

# Reserved for explicit inheritance rules. Session cache intentionally defaults
# to server rather than inheriting a future browser-authority selection.
_INHERIT: Dict[str, str] = {}

VALID_BACKENDS = {"browser", "server", "postgres"}

_TRUE_VALUES = {"1", "true", "yes", "on"}

# Legacy cache timing overrides may live in older routing files. They are forced
# to zero for wire compatibility; browser cache expiry is disabled globally.
_CACHE_POLICY_LIMITS: Dict[str, tuple] = {
    "metadata_ttl_seconds": (0, 0),
    "transcript_ttl_seconds": (0, 0),
}


def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_VALUES


def browser_authority_enabled() -> bool:
    """Whether the experimental IndexedDB authority path may be selected."""
    from app.db.browser_policy import load_browser_storage_policy
    if load_browser_storage_policy().persistence_mode != "persistent_cache":
        return False
    return _env_enabled("WEBAGENT_ENABLE_BROWSER_AUTHORITY")


def browser_session_cache_enabled() -> bool:
    """Whether server-authority sessions may be mirrored into IndexedDB."""
    from app.db.browser_policy import load_browser_storage_policy
    if load_browser_storage_policy().persistence_mode != "persistent_cache":
        return False
    if not _env_enabled("WEBAGENT_ENABLE_BROWSER_SESSION_CACHE"):
        return False
    from app.db.browser_canary import rollback_active
    return not rollback_active()


def storage_capabilities() -> Dict[str, bool]:
    """Capabilities published to clients and admin configuration surfaces."""
    return {
        "browser_authority": browser_authority_enabled(),
        "browser_session_cache": browser_session_cache_enabled(),
    }


def _apply_feature_gates(routing: Dict[str, str]) -> Dict[str, str]:
    """Fail closed when an on-disk config enables an unavailable browser path."""
    gated = dict(routing)
    if not browser_authority_enabled() and gated.get("session_data") == "browser":
        gated["session_data"] = "server"
    if not browser_session_cache_enabled() and gated.get("session_cache") == "browser":
        gated["session_cache"] = "server"
    return gated

# ── Singleton ───────────────────────────────────────────────────────────────

class StorageRouter:
    """Resolves which backend each data function uses."""

    def __init__(self) -> None:
        self._routing: Dict[str, str] = {}
        self._cache_policy: Dict[str, int] = {}
        self._mtime: float = 0.0
        self._reload()

    def _reload(self) -> None:
        """Read the config file (if it changed since last read)."""
        try:
            mtime = os.path.getmtime(_ROUTING_PATH) if os.path.exists(_ROUTING_PATH) else 0
        except OSError:
            mtime = 0

        if mtime == self._mtime and self._routing:
            return  # unchanged

        self._mtime = mtime

        if not os.path.exists(_ROUTING_PATH):
            self._routing = dict(_DEFAULTS)
            return

        try:
            with open(_ROUTING_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            logger.warning("Failed to read storage_routing.json: %s", e)
            self._routing = dict(_DEFAULTS)
            self._cache_policy = {}
            return

        merged = dict(_DEFAULTS)
        if isinstance(data, dict):
            for k, v in data.items():
                if k in _DEFAULTS and v in VALID_BACKENDS:
                    merged[k] = v
        # Inherit unset keys from their parent — so session_cache defaults
        # to session_data unless explicitly overridden in the config file.
        for child, parent in _INHERIT.items():
            if not isinstance(data, dict) or child not in data:
                merged[child] = merged.get(parent, _DEFAULTS.get(parent, "server"))
        self._routing = _apply_feature_gates(merged)
        self._cache_policy = self._parse_cache_policy(
            data.get("cache_policy") if isinstance(data, dict) else None
        )

    @staticmethod
    def _parse_cache_policy(raw: object) -> Dict[str, int]:
        """Validate the optional ``cache_policy`` block from the routing file."""
        policy: Dict[str, int] = {}
        if not isinstance(raw, dict):
            return policy
        for key, (minimum, maximum) in _CACHE_POLICY_LIMITS.items():
            try:
                value = int(raw.get(key))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            policy[key] = max(minimum, min(maximum, value))
        return policy

    # ── Query API ───────────────────────────────────────────────────────

    def get(self, function: str) -> str:
        """Return the backend for a data function (e.g. 'browser', 'server', 'postgres')."""
        self._reload()
        # Feature and persistence policy changes are live even when the routing
        # JSON mtime did not change.
        current = _apply_feature_gates(self._routing)
        return current.get(function, _DEFAULTS.get(function, "server"))

    def is_browser(self, function: str) -> bool:
        """True if this function is served by browser-authority IndexedDB."""
        return self.get(function) == "browser"

    def is_server(self, function: str) -> bool:
        """True if this function uses server-side SQLite."""
        return self.get(function) == "server"

    def is_postgres(self, function: str) -> bool:
        """True if this function uses remote PostgreSQL."""
        return self.get(function) == "postgres"

    # ── Convenience ─────────────────────────────────────────────────────

    def session_data_uses(self) -> str:
        """Where session transcript data lives."""
        return self.get("session_data")

    def session_tools_uses(self) -> str:
        """Where tool-call data within sessions lives."""
        return self.get("session_tools")

    def agent_data_uses(self) -> str:
        """Where agent config data lives."""
        return self.get("agent_data")

    def user_data_uses(self) -> str:
        """Where user account/profile data lives."""
        return self.get("user_data")

    def vault_uses(self) -> str:
        """Where the credential vault lives."""
        return self.get("vault")

    def genui_uses(self) -> str:
        """Where Gen UI pages live."""
        return self.get("genui_pages")

    def attachments_uses(self) -> str:
        """Where uploaded attachments live."""
        return self.get("attachments")

    def session_cache_uses(self) -> str:
        """Whether browser IndexedDB caching is enabled ('browser'/'server')."""
        return self.get("session_cache")

    def local_instance_uses(self) -> str:
        """Where per-installation app-plane state (locks, heartbeats, bookkeeping) lives."""
        return self.get("local_instance")

    def app_shared_data_uses(self) -> str:
        """Where shared app-plane data (accounts, catalog, billing) lives."""
        return self.get("app_shared_data")

    @property
    def routing(self) -> Dict[str, str]:
        """Full routing dict (for config endpoints)."""
        self._reload()
        return _apply_feature_gates(self._routing)

    @property
    def cache_policy(self) -> Dict[str, int]:
        """Legacy cache timing overrides from the routing file."""
        self._reload()
        return dict(self._cache_policy)


# ── Singleton ───────────────────────────────────────────────────────────────

_router: Optional[StorageRouter] = None


def get_storage_router() -> StorageRouter:
    """Return the singleton StorageRouter, creating it on first call."""
    global _router
    if _router is None:
        _router = StorageRouter()
    return _router


def reload_routing() -> None:
    """Force reload of the routing config (called after admin save)."""
    global _router
    if _router is not None:
        _router._mtime = -1
        _router._reload()

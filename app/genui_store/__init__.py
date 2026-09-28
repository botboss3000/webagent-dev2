"""
Page-store factory: pick a GenuiStore based on persisted config.

Mode is stored in `genui_mode.json` alongside `db_mode.json` and
`secrets_mode.json`. Default is `filesystem` because the app's primary
target is local-hosted use, where files-on-disk is the simplest and most
editable option. Cloud / multi-user deploys should switch to `database`
(or `hybrid`) via the storage panel.

Available stores:
  - "filesystem"  (default)
  - "database"
  - "hybrid"
"""

import json
import logging
import os
from typing import Optional

from app.genui_store.interface import GenuiStore

logger = logging.getLogger(__name__)

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODE_FILE = os.path.join(_AGENT_DIR, "genui_mode.json")

_AVAILABLE = ("filesystem", "database", "hybrid")

_store: Optional[GenuiStore] = None
_mode: Optional[str] = None


def _load_saved_mode() -> Optional[str]:
    try:
        if os.path.exists(_MODE_FILE):
            with open(_MODE_FILE, "r") as f:
                return json.load(f).get("mode")
    except Exception as e:
        logger.warning("Failed to load genui_mode.json: %s", e)
    return None


def _save_mode(mode: str) -> None:
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return
    try:
        with open(_MODE_FILE, "w") as f:
            json.dump({"mode": mode}, f)
    except Exception as e:
        logger.warning("Failed to save genui_mode.json: %s", e)


def get_mode() -> str:
    """Return the active genui-store mode name."""
    global _mode
    if _mode is None:
        env_choice = os.environ.get("WEBAGENT_GENUI_STORE", "").strip().lower()
        if env_choice in _AVAILABLE:
            _mode = env_choice
        else:
            _mode = _load_saved_mode() or "filesystem"
    return _mode


def set_mode(mode: str) -> None:
    """Switch the genui store. Persists to disk and resets the cached instance."""
    global _store, _mode
    if mode not in _AVAILABLE:
        raise ValueError(f"Invalid mode '{mode}'. Must be one of {_AVAILABLE}")
    _mode = mode
    _store = None
    _save_mode(mode)
    logger.info("Gen UI store switched to '%s'", mode)


def list_modes() -> list:
    return list(_AVAILABLE)


def _construct(mode: str) -> GenuiStore:
    if mode == "filesystem":
        from app.genui_store.filesystem import FilesystemGenuiStore
        return FilesystemGenuiStore()
    if mode == "database":
        from app.genui_store.database import DatabaseGenuiStore
        return DatabaseGenuiStore()
    if mode == "hybrid":
        from app.genui_store.hybrid import HybridGenuiStore
        return HybridGenuiStore()
    raise ValueError(f"Unknown genui store mode: {mode}")


get_genui_store = get_pages_store = lambda: _get_store()


def _get_store() -> GenuiStore:
    """Return the active GenuiStore instance (lazy)."""
    global _store
    if _store is None:
        mode = get_mode()
        try:
            _store = _construct(mode)
            logger.info("Initialized genui store: %s", mode)
        except Exception as e:
            logger.warning(
                "Failed to construct genui store '%s' (%s); falling back to filesystem",
                mode, e,
            )
            from app.genui_store.filesystem import FilesystemGenuiStore
            _store = FilesystemGenuiStore()
    return _store


def get_status() -> dict:
    """For UI: current mode + availability info."""
    return {
        "mode": get_mode(),
        "available": list(_AVAILABLE),
        "env_locked": os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env",
    }

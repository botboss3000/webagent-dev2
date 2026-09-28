"""
Attachment-store factory: pick an AttachmentStore based on persisted config.

Mode + provider config are stored in `attachments_store_config.json` alongside
`pages_mode.json` / `db_mode.json`. Default is `local` (filesystem) because
the app's primary target is local-hosted use.

Available backends:
  - "local"      data/user_data/<uid>/uploads/ on disk (default)
  - "browser"    bytes live in the user's browser IndexedDB; server stores
                 only metadata. Multi-device users won't share files.
  - "s3"         AWS S3 (or any S3-compatible endpoint via S3_ENDPOINT_URL)
  - "gcs"        Google Cloud Storage

Setting precedence (highest first):
  1. WEBAGENT_ATTACHMENTS_STORE env var (env-locked)
  2. attachments_store_config.json on disk
  3. "local" (default)
"""

import json
import logging
import os
from typing import Any, Dict, Optional

from app.attachments_store.interface import AttachmentStore

logger = logging.getLogger(__name__)

_AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CONFIG_FILE = os.path.join(_AGENT_DIR, "attachments_store_config.json")

AVAILABLE_BACKENDS = ("local", "browser", "s3", "gcs")

_store: Optional[AttachmentStore] = None
_mode: Optional[str] = None
_provider_config: Optional[Dict[str, Dict[str, Any]]] = None


# ── Config persistence ──────────────────────────────────────────────────────


def _read_config_file() -> Dict[str, Any]:
    if not os.path.exists(_CONFIG_FILE):
        return {}
    try:
        with open(_CONFIG_FILE, "r") as f:
            return json.load(f) or {}
    except Exception as e:
        logger.warning("Failed to read %s: %s", _CONFIG_FILE, e)
        return {}


def _write_config_file(data: Dict[str, Any]) -> None:
    if os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env":
        return
    try:
        with open(_CONFIG_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        logger.warning("Failed to write %s: %s", _CONFIG_FILE, e)


def _load_state() -> None:
    global _mode, _provider_config
    if _mode is not None and _provider_config is not None:
        return
    raw = _read_config_file()
    _provider_config = raw.get("providers") or {}
    env_choice = os.environ.get("WEBAGENT_ATTACHMENTS_STORE", "").strip().lower()
    if env_choice in AVAILABLE_BACKENDS:
        _mode = env_choice
    else:
        saved = raw.get("mode")
        _mode = saved if saved in AVAILABLE_BACKENDS else "local"


def _save_state() -> None:
    _write_config_file({"mode": _mode, "providers": _provider_config or {}})


# ── Public API ──────────────────────────────────────────────────────────────


def get_mode() -> str:
    """Return the active backend name."""
    _load_state()
    return _mode or "local"


def set_mode(mode: str) -> None:
    """Switch the active backend. Persists to disk and resets the cached instance."""
    global _store, _mode
    if mode not in AVAILABLE_BACKENDS:
        raise ValueError(f"Invalid attachment backend '{mode}'. Must be one of {AVAILABLE_BACKENDS}")
    _load_state()
    _mode = mode
    _store = None
    _save_state()
    logger.info("Attachment backend switched to '%s'", mode)


def list_modes() -> list:
    return list(AVAILABLE_BACKENDS)


def get_provider_config(provider: str) -> Dict[str, Any]:
    """Return the persisted config for a provider (e.g. {bucket, region, prefix})."""
    _load_state()
    return dict((_provider_config or {}).get(provider) or {})


def set_provider_config(provider: str, cfg: Dict[str, Any]) -> None:
    """Persist provider config and reset the cached instance if this is the active one."""
    global _store, _provider_config
    if provider not in AVAILABLE_BACKENDS:
        raise ValueError(f"Unknown provider '{provider}'")
    _load_state()
    if _provider_config is None:
        _provider_config = {}
    # Drop None / empty-string fields so env defaults still apply.
    cleaned = {k: v for k, v in (cfg or {}).items() if v not in (None, "")}
    _provider_config[provider] = cleaned
    if provider == _mode:
        _store = None
    _save_state()


def get_status() -> dict:
    """For UI: current mode + available backends + per-provider config."""
    _load_state()
    return {
        "mode": get_mode(),
        "available": list(AVAILABLE_BACKENDS),
        "providers": dict(_provider_config or {}),
        "env_locked": os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"
        or bool(os.environ.get("WEBAGENT_ATTACHMENTS_STORE")),
    }


# ── Factory ─────────────────────────────────────────────────────────────────


def _construct(mode: str) -> AttachmentStore:
    cfg = get_provider_config(mode)
    if mode == "local":
        from app.attachments_store.local_store import LocalAttachmentStore
        return LocalAttachmentStore(upload_dir=cfg.get("upload_dir"))
    if mode == "browser":
        from app.attachments_store.browser_store import BrowserAttachmentStore
        return BrowserAttachmentStore()
    if mode == "s3":
        from app.attachments_store.s3_store import S3AttachmentStore
        return S3AttachmentStore(
            bucket=cfg.get("bucket"),
            region=cfg.get("region"),
            prefix=cfg.get("prefix"),
            endpoint_url=cfg.get("endpoint_url"),
        )
    if mode == "gcs":
        from app.attachments_store.gcs_store import GcsAttachmentStore
        return GcsAttachmentStore(
            bucket=cfg.get("bucket"),
            prefix=cfg.get("prefix"),
        )
    raise ValueError(f"Unknown attachment backend: {mode}")


def get_attachment_store() -> AttachmentStore:
    """Return the active AttachmentStore instance (lazy)."""
    global _store
    if _store is None:
        mode = get_mode()
        try:
            _store = _construct(mode)
            logger.info("Initialized attachment store: %s", mode)
        except Exception as e:
            logger.warning(
                "Failed to construct attachment store '%s' (%s); falling back to local.",
                mode, e,
            )
            from app.attachments_store.local_store import LocalAttachmentStore
            _store = LocalAttachmentStore()
    return _store


def get_store_for_provider(provider: str) -> AttachmentStore:
    """
    Resolve a backend by name *regardless* of the active mode. Used by
    read/delete paths so an attachment row written under a previous backend
    can still be served after the admin switches modes.
    """
    return _construct(provider)


__all__ = [
    "AVAILABLE_BACKENDS",
    "get_mode",
    "set_mode",
    "list_modes",
    "get_provider_config",
    "set_provider_config",
    "get_status",
    "get_attachment_store",
    "get_store_for_provider",
]

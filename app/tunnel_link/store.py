"""Persisted config for the Tunnel Link feature.

A single JSON file under data/config/ (runtime-only, gitignored), mirroring the
shape of app/remote_access/store.py. Holds the relay we talk to and the
credentials that relay handed us when we registered.

Nothing here is shown to the phone — the instance_secret never leaves the
backend; the admin panel only ever sees whether we're registered.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILE = PROJECT_ROOT / "data" / "config" / "tunnel_link.json"

_lock = threading.Lock()

DEFAULT_CONFIG: Dict = {
    # The hosted relay this instance pairs through.
    "relay_base_url": "https://tunnel.webagent.live",
    # Credentials the relay issued at registration (backend-only).
    "instance_id": "",
    "instance_secret": "",
    # Which relay base we last registered against — if relay_base_url changes,
    # we must register afresh rather than PUT to a relay that doesn't know us.
    "registered_relay": "",
    # The user's Cloudflare tunnel address (what the phone ultimately views).
    "tunnel_url": "",
    # A friendly name shown in the viewer toolbar.
    "label": "",
}


def _read_raw() -> Dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def load_config() -> Dict:
    """Return the saved config merged over the defaults (a fresh dict)."""
    cfg = dict(DEFAULT_CONFIG)
    cfg.update(_read_raw())
    return cfg


def save_config(cfg: Dict) -> Dict:
    """Persist the full config dict to disk."""
    with _lock:
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    return cfg


def update_config(patch: Dict) -> Dict:
    """Merge a shallow patch into the saved config and persist it."""
    cfg = load_config()
    cfg.update(patch)
    return save_config(cfg)

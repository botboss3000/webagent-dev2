"""P2P peer store — CRUD for configured peers and per-peer sync state.

Peers live in ``data/config/p2p/peers/<peer_id>.json`` — one JSON file per peer.
Sync state (last sync timestamp, last known manifest hash) is stored in the same
file under a ``sync`` key.
"""

from __future__ import annotations

import json
import logging
import uuid
from pathlib import Path
from typing import Dict, List, Optional

from app.p2p.identity import instance_id as _self_id

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_PEERS_DIR = _PROJECT_ROOT / "data" / "config" / "p2p" / "peers"


def _peer_path(peer_id: str) -> Path:
    return _PEERS_DIR / f"{peer_id}.json"


def _read_peer(peer_id: str) -> Optional[Dict]:
    p = _peer_path(peer_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_peer(peer_id: str, data: Dict) -> None:
    _PEERS_DIR.mkdir(parents=True, exist_ok=True)
    _peer_path(peer_id).write_text(json.dumps(data, indent=2), encoding="utf-8")


def list_peers() -> List[Dict]:
    """All configured peers with their sync state."""
    _PEERS_DIR.mkdir(parents=True, exist_ok=True)
    peers: List[Dict] = []
    for f in sorted(_PEERS_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["id"] = f.stem
            peers.append(data)
        except Exception:
            pass
    return peers


def get_peer(peer_id: str) -> Optional[Dict]:
    """One peer by id, or None."""
    data = _read_peer(peer_id)
    if data:
        data["id"] = peer_id
    return data


def add_peer(
    url: str,
    name: str,
    public_key_hex: str,
    remote_instance_id: str = "",
    x25519_public_key: str = "",
    sync_options: Optional[Dict] = None,
    bootstrap_only: bool = False,
    push_replica: bool = False,
) -> Dict:
    """Store a new peer (or update an existing one by URL).  Returns the saved dict."""
    existing = list_peers()
    for p in existing:
        if p.get("url", "").rstrip("/") == url.rstrip("/"):
            # Update existing
            p["name"] = name
            p["public_key"] = public_key_hex
            if remote_instance_id:
                p["remote_instance_id"] = remote_instance_id
            if x25519_public_key:
                p["x25519_public_key"] = x25519_public_key
            if sync_options is not None:
                p["sync_options"] = dict(sync_options)
            p["bootstrap_only"] = bool(bootstrap_only)
            p["push_replica"] = bool(push_replica)
            p["updated_at"] = _now_iso()
            _write_peer(p["id"], p)
            return p

    peer_id = uuid.uuid4().hex[:10]
    data = {
        "url": url.rstrip("/"),
        "name": name,
        "public_key": public_key_hex,
        "remote_instance_id": remote_instance_id,
        "x25519_public_key": x25519_public_key,
        "sync_options": dict(sync_options or {}),
        "bootstrap_only": bool(bootstrap_only),
        "push_replica": bool(push_replica),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "sync": {
            "last_sync_at": None,
            "last_manifest_hash": None,
            "last_sync_files": 0,
            "status": "pending",
        },
    }
    _write_peer(peer_id, data)
    data["id"] = peer_id
    logger.info("Added p2p peer %s (%s) at %s", peer_id, name, url)
    return data


def remove_peer(peer_id: str) -> bool:
    """Delete a peer.  Returns True if it existed."""
    p = _peer_path(peer_id)
    if p.exists():
        p.unlink()
        logger.info("Removed p2p peer %s", peer_id)
        return True
    return False


def get_sync_state(peer_id: str) -> Dict:
    """Return a peer's sync state dict, or empty defaults."""
    peer = _read_peer(peer_id)
    if peer and "sync" in peer:
        return peer["sync"]
    return {"last_sync_at": None, "last_manifest_hash": None, "last_sync_files": 0, "status": "pending"}


def set_sync_state(peer_id: str, **kwargs) -> None:
    """Update a peer's sync state fields."""
    data = _read_peer(peer_id)
    if not data:
        return
    if "sync" not in data:
        data["sync"] = {}
    data["sync"].update(kwargs)
    _write_peer(peer_id, data)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

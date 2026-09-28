"""P2P peer revocation — key invalidation and peer data isolation.

When a peer is revoked:
  1. Its public key is added to the revocation set (rejected at handshake)
  2. Its peer KEK is cleared (can no longer decrypt synced secrets)
  3. Its peer JSON file is moved to data/config/p2p/revoked/ (kept for audit)
  4. Its cached session key is dropped from active transports
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Set

from app.p2p import store as peer_store

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_DIR = _PROJECT_ROOT / "data" / "config" / "p2p"
_REVOKED_DIR = _CONFIG_DIR / "revoked"
_REVOCATION_PATH = _CONFIG_DIR / "revocations.json"


def is_revoked(public_key_hex: str) -> bool:
    """Check if a public key has been revoked.

    Called during handshake and before accepting any request from a peer.
    """
    revocation_set = _load_revocation_set()
    return public_key_hex in revocation_set


def revoke_peer(peer_id: str) -> bool:
    """Revoke a peer by its id.

    Returns True if the peer was found and revoked, False if not found.
    """
    peer = peer_store.get_peer(peer_id)
    if not peer:
        return False

    public_key = peer.get("public_key", "")

    # 1. Add public key to revocation set (persist to disk)
    if public_key:
        _add_to_revocation_set(public_key)

    # 2. Clear peer KEK from vault_keys
    try:
        from app.encryption.vault_keys import vault_key_manager
        vault_key_manager.unregister_peer_kek(peer_id)
    except Exception as e:
        logger.warning("Failed to clear peer KEK for %s: %s", peer_id, e)

    # 3. Move peer JSON file to revoked/ directory
    _REVOKED_DIR.mkdir(parents=True, exist_ok=True)
    peer_file = _CONFIG_DIR / "peers" / f"{peer_id}.json"
    if peer_file.exists():
        revoked_file = _REVOKED_DIR / f"{peer_id}.json"
        shutil.move(str(peer_file), str(revoked_file))
        logger.info("Revoked peer %s — moved %s → %s", peer_id, peer_file, revoked_file)

    # 4. Remove from active peer store cache
    peer_store.remove_peer(peer_id)

    logger.info("Peer %s revoked (public_key: %s...)", peer_id, public_key[:12] if public_key else "?")
    return True


# ── Internals ────────────────────────────────────────────────────────────────────


def _load_revocation_set() -> Set[str]:
    """Load the revocation set from disk."""
    if not _REVOCATION_PATH.exists():
        return set()
    try:
        with open(_REVOCATION_PATH, "r") as f:
            keys = json.load(f)
            if isinstance(keys, list):
                return set(keys)
    except Exception as e:
        logger.warning("Failed to load revocation set: %s", e)
    return set()


def _add_to_revocation_set(public_key_hex: str) -> None:
    """Persist a public key to the revocation set."""
    keys = _load_revocation_set()
    keys.add(public_key_hex)
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(_REVOCATION_PATH, "w") as f:
            json.dump(sorted(keys), f)
    except Exception as e:
        logger.error("Failed to write revocation set: %s", e)

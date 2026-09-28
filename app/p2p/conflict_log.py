"""P2P conflict log — append-only JSONL audit trail.

Every sync conflict (file overwrite, vault row version rejection, timestamp tie)
is logged here for diagnostics and manual resolution if needed.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFLICT_LOG_PATH = _PROJECT_ROOT / "data" / "config" / "p2p" / "conflicts.jsonl"


def log_conflict(entry: Dict) -> None:
    """Append a conflict record to the JSONL log.
    
    entry must contain at least: type, timestamp, peer_id
    Additional fields depend on the conflict type.
    """
    entry.setdefault("timestamp", datetime.now(timezone.utc).isoformat())
    _CONFLICT_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(_CONFLICT_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, sort_keys=True) + "\n")
    except Exception as e:
        logger.warning("Failed to log P2P conflict: %s", e)


def log_file_overwrite_conflict(
    peer_id: str,
    file_path: str,
    local_sha256: str,
    remote_sha256: str,
    backup_path: str,
) -> None:
    """Log a file overwrite conflict."""
    log_conflict({
        "type": "file_overwrite",
        "peer_id": peer_id,
        "file_path": file_path,
        "local_sha256": local_sha256,
        "remote_sha256": remote_sha256,
        "backup_path": backup_path,
    })


def log_vault_version_conflict(
    peer_id: str,
    vault_schema: str,
    user_id: str,
    service: str,
    label: str,
    local_version: int,
    incoming_version: int,
    conflict_label: str,
) -> None:
    """Log a vault row version conflict (local version > incoming)."""
    log_conflict({
        "type": "vault_version_reject",
        "peer_id": peer_id,
        "vault_schema": vault_schema,
        "user_id": user_id,
        "service": service,
        "label": label,
        "local_version": local_version,
        "incoming_version": incoming_version,
        "conflict_label": conflict_label,
    })


def log_vault_timestamp_tie(
    peer_id: str,
    vault_schema: str,
    user_id: str,
    service: str,
    label: str,
    local_hash: str,
    remote_hash: str,
) -> None:
    """Log a vault row conflict where timestamps are equal but content differs."""
    log_conflict({
        "type": "vault_timestamp_tie",
        "peer_id": peer_id,
        "vault_schema": vault_schema,
        "user_id": user_id,
        "service": service,
        "label": label,
        "local_hash": local_hash,
        "remote_hash": remote_hash,
        "resolution": "local_wins",
    })

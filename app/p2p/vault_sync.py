"""P2P vault row sync — row-level manifest, diff, and apply for auth_elements.

Syncs individual rows between peers with version-counter optimistic locking.
Conflict resolution: newer updated_at wins; if equal, local wins with backup.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Dict, List, Optional

from app.p2p.manifest import VAULT_DB_FILES
from app.p2p.vault_policy import should_sync_row

logger = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _get_vault_path(vault_schema: str) -> str:
    """Map vault schema name to db file path."""
    reverse = {v: k for k, v in VAULT_DB_FILES.items()}
    return reverse.get(vault_schema, "")


def _row_hash(row: Dict) -> str:
    """Stable sha256 hash of a row's syncable content (config + secret_ref)."""
    payload = json.dumps({
        "config": row.get("config", "{}"),
        "secret_ref": row.get("secret_ref", ""),
    }, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def build_vault_row_manifest(vault_schema: str) -> List[Dict]:
    """Return a row-level manifest for one vault schema.
    
    Each entry: {user_id, service, label, config, secret_ref, is_active,
                 updated_at, _version, _schema_version, row_hash}
    
    Local-only rows (per vault_policy) are excluded.
    """
    import sqlite3
    
    db_path = os.path.join(_PROJECT_ROOT, _get_vault_path(vault_schema))
    if not os.path.exists(db_path):
        logger.warning("Vault DB not found: %s", db_path)
        return []

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            f"SELECT user_id, service, label, config, secret_ref, is_active, "
            f"updated_at, _version, _schema_version "
            f"FROM auth_elements ORDER BY user_id, service, label"
        ).fetchall()
    finally:
        con.close()

    manifest = []
    for r in rows:
        row_dict = dict(r)
        if not should_sync_row(row_dict["user_id"], row_dict["service"], row_dict["label"]):
            continue
        row_dict["row_hash"] = _row_hash(row_dict)
        manifest.append(row_dict)

    return manifest


def diff_vault_rows(
    local: List[Dict],
    remote: List[Dict],
) -> Dict:
    """Compare local and remote vault row manifests.
    
    Composite key: (user_id, service, label).
    
    Returns:
        {
            "to_pull": [...],      # rows on remote, not on local, or newer on remote
            "to_push": [...],      # rows on local, not on remote, or newer on local
            "conflicts": [...],    # same key, different hash, same updated_at
        }
    """
    local_map = {(r["user_id"], r["service"], r["label"]): r for r in local}
    remote_map = {(r["user_id"], r["service"], r["label"]): r for r in remote}

    to_pull = []
    to_push = []
    conflicts = []

    all_keys = set(local_map.keys()) | set(remote_map.keys())

    for key in all_keys:
        local_row = local_map.get(key)
        remote_row = remote_map.get(key)

        if remote_row and not local_row:
            # New on remote
            to_pull.append(remote_row)
        elif local_row and not remote_row:
            # New on local
            to_push.append(local_row)
        elif local_row and remote_row:
            # Both have it — check for changes
            if local_row["row_hash"] == remote_row["row_hash"]:
                continue  # Identical

            local_ts = local_row.get("updated_at", "")
            remote_ts = remote_row.get("updated_at", "")

            if remote_ts > local_ts:
                # Remote is newer — pull
                to_pull.append(remote_row)
            elif local_ts > remote_ts:
                # Local is newer — push
                to_push.append(local_row)
            else:
                # Same timestamp, different content — conflict
                conflicts.append({
                    "key": key,
                    "local_hash": local_row["row_hash"],
                    "remote_hash": remote_row["row_hash"],
                    "local_row": local_row,
                    "remote_row": remote_row,
                })

    return {"to_pull": to_pull, "to_push": to_push, "conflicts": conflicts}


def apply_vault_rows(
    vault_schema: str,
    rows: List[Dict],
    direction: str = "pull",
) -> Dict:
    """Apply synced vault rows to the local database.
    
    direction: "pull" = remote rows being applied locally
               "push" = local rows being sent (no-op here — the remote peer calls this)
    
    Uses version-counter optimistic locking: if the local _version is higher
    than the incoming row's _version, the incoming row is rejected (local wins).
    
    Returns: {"applied": int, "rejected": int, "conflicts": [...]}
    """
    import sqlite3
    
    db_path = os.path.join(_PROJECT_ROOT, _get_vault_path(vault_schema))
    if not os.path.exists(db_path):
        return {"applied": 0, "rejected": 0, "conflicts": []}

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        applied = 0
        rejected = 0
        conflict_backups = []

        for row in rows:
            user_id = row["user_id"]
            service = row["service"]
            label = row["label"]
            incoming_version = row.get("_version", 1)

            # Check local state
            existing = con.execute(
                f"SELECT id, _version, config, secret_ref FROM auth_elements "
                f"WHERE user_id = ? AND service = ? AND label = ?",
                (user_id, service, label),
            ).fetchone()

            if existing:
                local_version = existing["_version"] or 0
                if local_version > incoming_version:
                    # Local has been modified since the peer's version — reject
                    # Create conflict backup
                    try:
                        backup_label = f"{label}_conflict_{row.get('_schema_version', '?')}_{local_version}"
                        con.execute(
                            f"INSERT OR REPLACE INTO auth_elements "
                            f"(id, user_id, service, label, config, secret_ref, is_active, created_at, updated_at, _version, _schema_version) "
                            f"VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), ?, ?)",
                            (
                                f"conflict_{existing['id']}",
                                user_id, service, backup_label,
                                row.get("config", "{}"), row.get("secret_ref", ""),
                                row.get("is_active", 1), incoming_version,
                                row.get("_schema_version", 1),
                            ),
                        )
                        conflict_backups.append({
                            "user_id": user_id, "service": service, "label": label,
                            "reason": "version_rejected",
                            "local_version": local_version,
                            "incoming_version": incoming_version,
                        })
                        from app.p2p.conflict_log import log_vault_version_conflict
                        log_vault_version_conflict(
                            peer_id="",
                            vault_schema=vault_schema,
                            user_id=user_id,
                            service=service,
                            label=label,
                            local_version=local_version,
                            incoming_version=incoming_version,
                            conflict_label=backup_label,
                        )
                    except Exception as e:
                        logger.warning("Failed to create conflict backup: %s", e)
                    rejected += 1
                    continue

            # Apply the row (INSERT OR REPLACE)
            import uuid
            row_id = existing["id"] if existing else str(uuid.uuid4())
            config = row.get("config", "{}")
            if isinstance(config, dict):
                config = json.dumps(config)
            secret_ref = row.get("secret_ref", "")
            now = row.get("updated_at") or _now_iso()

            con.execute(
                f"INSERT OR REPLACE INTO auth_elements "
                f"(id, user_id, service, label, config, secret_ref, is_active, created_at, updated_at, _version, _schema_version) "
                f"VALUES (?, ?, ?, ?, ?, ?, ?, COALESCE((SELECT created_at FROM auth_elements WHERE id = ?), ?), ?, ?, ?)",
                (
                    row_id, user_id, service, label, config, secret_ref,
                    row.get("is_active", 1), row_id, now, now,
                    incoming_version, row.get("_schema_version", 1),
                ),
            )
            applied += 1

        con.commit()
        return {"applied": applied, "rejected": rejected, "conflicts": conflict_backups}
    finally:
        con.close()


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()

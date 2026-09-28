"""P2P classified manifest — tiered data sync inventory.

Replaces the raw build_manifest() from sync.py with a three-tier system:
  full  — file-level sync (config, genui, wiki, global DB, defaults)
  row   — row-level sync (all three vault DBs, with policy filtering)
  never — excluded (per-machine keys, logs, sessions, uploads, WAL files)
"""

from __future__ import annotations

import hashlib
import logging
import os
from pathlib import Path
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ── Tier definitions ─────────────────────────────────────────────────────────────

# Files/patterns that are synced as whole files (full tier)
FULL_SYNC_GLOBS = (
    # Config files (shared admin config)
    "data/config/*.json",
    # GenUI pages
    "data/genui/*",
    "data/genui/**/*",
    # Per-user genui
    "data/user_data/*/genui/*",
    "data/user_data/*/genui/**/*",
    # Per-agent authorities.  app.db is intentionally NOT a full-file sync item:
    # it contains live per-instance coordination state and replacing it under a
    # running peer can copy stale leases/jobs or erase newer local rows.  A new
    # deployment receives its explicitly selected portable app tables through
    # app.p2p.bootstrap instead.
    "data/agent_data/*/*.db",
    "data/agent_data/*/subagents/*/*.db",
    # Durable, agent-owned Knowledge files travel with the agent. Chat uploads
    # remain user/session data and are intentionally excluded below.
    "data/agent_data/*/knowledge/*",
    "data/agent_data/*/knowledge/**/*",
    "data/agent_data/*/subagents/*/knowledge/*",
    "data/agent_data/*/subagents/*/knowledge/**/*",
    "data/wiki.db",
    # Defaults (agent templates, dashboards)
    "data/defaults/*",
    "data/defaults/**/*",
)

# Files/patterns that are NEVER synced
# (per-machine identity, runtime state, logs, sessions)
NEVER_SYNC_GLOBS = (
    # Per-machine identity
    "data/config/p2p/*",
    "data/config/jwt_secret.txt",
    "data/config/tunnel_link.json",
    "data/config/auth_revocation.sqlite*",
    # Per-machine DB config
    "data/config/db_connection.json",
    "data/config/provider.json",
    # Runtime databases
    "data/db/logs.db",
    "data/db/recordings.db",
    # Vault files (synced as rows, not files — see VAULT_DB_FILES below)
    # NOTE: the vault .db paths themselves are NOT in this list, otherwise the
    # NEVER check below would shadow the row tier (classify_file checks NEVER first).
    # Optimizer scratch
    "data/db/optimizer.db",
    "data/db/optimizer_*.db",
    # WAL sidecars (all DB files)
    "data/db/*.db-wal",
    "data/db/*.db-shm",
    "data/db/*.db-journal",
    # User session data
    "data/user_data/*/*.db",
    "data/user_data/*/*.db-wal",
    "data/user_data/*/*.db-shm",
    "data/user_data/*/*.db-journal",
    # Per-agent authority WAL sidecars are never synced.
    "data/agent_data/*/*.db-wal",
    "data/agent_data/*/*.db-shm",
    "data/agent_data/*/*.db-journal",
    # Excluded from genui subfolder in user_data: only genui/ is synced
    # (handled by FULL_SYNC_GLOBS above — everything else in user_data/*/ is never)
    # Uploads, screenshots, visuals
    "data/uploads/*",
    "data/screenshots/*",
    "data/visuals/*",
    "data/visuals/users/*",
    # Temporary / scratch
    "data/tmp/*",
    "data/local-instances/*",
    # Retired pre-v2 vault stray
    "data/vault.db",
    # Stale backup dirs
    "data/db/backups/*",
    "data/db.bak-*/*",
    # P2P conflict backups
    "data/db/backups/p2p-conflict-*",
    # Peer revoked keys
    "data/config/p2p/revoked/*",
    # Config files with per-machine secrets
    "data/config/secrets_mode.json",
    "data/config/app_secrets_config.json",
    "data/config/encryption_mode.json",
    "data/config/db_encryption.json",
    "data/config/db_hybrid.json",
    "data/config/pages_mode.json",
    "data/config/attachments_store_config.json",
    # Tunnel / remote access
    "data/config/tunnel_link.json",
    "remote_access.json",
    "remote_access.json.bak",
    "remote_access_pointers.json",
    # Production mirror config
    "data/config/production-mirror.json",
)

# Vault files that are synced as rows (not files)
VAULT_DB_FILES = {
    "data/db/app_secrets.db": "vault_app",
    "data/db/agent_secrets.db": "vault_agent",
    "data/db/user_secrets.db": "vault_user",
}

# Additional per-machine configs to never sync
NEVER_SYNC_EXACT = {
    "data/config/p2p/conflicts.jsonl",
}

# File suffixes that are always excluded
NEVER_SYNC_SUFFIXES = (
    ".db-wal",
    ".db-shm",
    ".db-journal",
    ".tmp",
    ".bak",
    ".p2p-tmp",
)


# ── Classification logic ─────────────────────────────────────────────────────────

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"


def _match_glob(path: str, pattern: str) -> bool:
    """Simple glob matching for data/ paths.
    
    Supports * (any chars within one path segment) and ** (any number of segments).
    """
    import fnmatch
    return fnmatch.fnmatch(path, pattern)


def classify_file(data_relpath: str) -> str:
    """Classify a single data/ file path into 'full', 'row', or 'never'.
    
    Args:
        data_relpath: Path relative to the project root (e.g., 'data/db/global.db')
    
    Returns:
        One of 'full', 'row', or 'never'
    """
    # Normalize path separators
    path = data_relpath.replace("\\", "/")
    
    # 1. Check NEVER patterns first (most specific)
    for pattern in NEVER_SYNC_GLOBS:
        if _match_glob(path, pattern):
            return "never"
    
    if path in NEVER_SYNC_EXACT:
        return "never"
    
    if path.endswith(NEVER_SYNC_SUFFIXES):
        return "never"
    
    # 2. Check vault DBs (row-level sync)
    if path in VAULT_DB_FILES:
        return "row"
    
    # 3. Check FULL sync patterns
    for pattern in FULL_SYNC_GLOBS:
        if _match_glob(path, pattern):
            return "full"
    
    # 4. Default: never sync unknown files
    return "never"


def build_classified_manifest() -> Dict:
    """Walk data/ and return a classified manifest.
    
    Returns:
        {
            "full": [
                {"path": "data/config/app-settings.json", "size": 1234, "sha256": "hex...", "mtime": 1234567890.0},
                ...
            ],
            "row": {
                "vault_app": "data/db/app_secrets.db",
                "vault_agent": "data/db/agent_secrets.db",
                "vault_user": "data/db/user_secrets.db",
            }
        }
    
    The 'row' tier lists vault files that need row-level sync (the actual row
    manifest is built separately by vault_sync.py).
    The 'never' tier is simply excluded.
    """
    full_entries: List[Dict] = []
    row_entries: Dict[str, str] = {}
    
    for dirpath, dirnames, filenames in os.walk(str(_DATA_DIR)):
        for fn in filenames:
            full_path = os.path.join(dirpath, fn)
            # Normalize to forward slashes so rel matches VAULT_DB_FILES keys and
            # manifests are identical across platforms (os.walk yields backslashes on Windows)
            rel = os.path.relpath(full_path, _PROJECT_ROOT).replace("\\", "/")
            
            tier = classify_file(rel)
            
            if tier == "full":
                try:
                    st = os.stat(full_path)
                    sha = _file_sha256(full_path)
                    full_entries.append({
                        "path": rel,
                        "size": st.st_size,
                        "sha256": sha,
                        "mtime": st.st_mtime,
                    })
                except OSError:
                    pass
            elif tier == "row":
                schema = VAULT_DB_FILES[rel]
                row_entries[schema] = rel
    
    full_entries.sort(key=lambda e: e["path"])
    
    return {
        "full": full_entries,
        "row": row_entries,
    }


def diff_classified_manifest(
    local: Dict,
    remote: Dict,
) -> Dict:
    """Compare local and remote classified manifests and return what to sync.
    
    Returns:
        {
            "to_pull_files": [...],   # files on remote but not local, or with different sha256
            "to_push_files": [...],   # files on local but not remote (reverse diff)
            "to_pull_vaults": [...],  # vault schemas that need row-level sync (placeholder)
            "to_push_vaults": [...],  # vault schemas to push rows to remote
        }
    """
    local_files = {e["path"]: e for e in local.get("full", [])}
    remote_files = {e["path"]: e for e in remote.get("full", [])}
    
    to_pull_files = []
    to_push_files = []
    
    # Files on remote that we need
    for path, entry in remote_files.items():
        if path not in local_files:
            to_pull_files.append(entry)
        elif local_files[path]["sha256"] != entry["sha256"]:
            to_pull_files.append(entry)
    
    # Files on local that remote doesn't have (reverse)
    for path, entry in local_files.items():
        if path not in remote_files:
            to_push_files.append(entry)
        # Note: if both have different sha256, pull wins (pull-before-push in worker)
    
    # Vault sync is handled separately by vault_sync.py — here we just note
    # which vault schemas exist on both sides
    local_vaults = set(local.get("row", {}).keys())
    remote_vaults = set(remote.get("row", {}).keys())
    
    to_pull_vaults = list(remote_vaults)  # all remote vaults are candidates for row sync
    to_push_vaults = list(local_vaults & remote_vaults)  # only push to peers that have the same vault
    
    return {
        "to_pull_files": to_pull_files,
        "to_push_files": to_push_files,
        "to_pull_vaults": to_pull_vaults,
        "to_push_vaults": to_push_vaults,
    }


def _file_sha256(path: str) -> str:
    """Return the hex sha256 digest of a file's contents."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()

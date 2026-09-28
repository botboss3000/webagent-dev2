"""P2P sync engine — manifest building, diffing, and file transfer.

``build_manifest`` walks ``data/`` and returns a list of {path, size, sha256, mtime}
for every file, excluding runtime artefacts (WAL files, temp, logs).

``diff_manifest`` compares two manifests and returns only the files that changed
or are new on the remote side.

``pull_files`` fetches changed files from a peer and writes them locally.  DB files
are snapshotted atomically (``sqlite3 .backup``) before transfer.  Received files
are written to temp first then atomically renamed.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from app.p2p import identity
from app.p2p import store as peer_store

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_DATA_DIR = _PROJECT_ROOT / "data"

# Patterns to EXCLUDE from the manifest (runtime artefacts, per-instance identity).
_EXCLUDE_PREFIXES = (
    "data/tmp/",
    "data/local-instances/",
)
_EXCLUDE_FILES = {
    "relauncher.log",
    "instance_id.txt",
}
_EXCLUDE_SUFFIXES = (
    ".db-wal",
    ".db-shm",
    ".db-journal",
)


def _should_include(data_relpath: str) -> bool:
    """True if this file should be included in the manifest."""
    for prefix in _EXCLUDE_PREFIXES:
        if data_relpath.startswith(prefix):
            return False
    basename = os.path.basename(data_relpath)
    if basename in _EXCLUDE_FILES:
        return False
    for suffix in _EXCLUDE_SUFFIXES:
        if data_relpath.endswith(suffix):
            return False
    return True


def build_manifest() -> List[Dict]:
    """Walk ``data/`` and return a manifest list.
    
    Each entry::
    
        { "path": "data/db/app.db", "size": 12345, "sha256": "hex...", "mtime": 1234567890.0 }
    
    Delegates to the classified manifest (manifest.py) — only returns 'full' tier files.
    Use build_classified_manifest() for the full three-tier result.
    """
    from app.p2p.manifest import build_classified_manifest
    classified = build_classified_manifest()
    return classified["full"]


def diff_manifest(local: List[Dict], remote: List[Dict]) -> List[Dict]:
    """Return files present on remote but NOT on local, or with a different sha256.

    Local-only files are ignored (we only pull, never push in basic mode).
    """
    local_map: Dict[str, str] = {e["path"]: e["sha256"] for e in local}
    changed: List[Dict] = []
    for e in remote:
        path = e["path"]
        if path not in local_map:
            changed.append(e)
        elif local_map[path] != e["sha256"]:
            changed.append(e)
    return changed


async def pull_files(peer: Dict, wanted: List[Dict]) -> int:
    """Fetch changed files from a peer and write them locally.

    ``wanted`` is a list of manifest entries (from ``diff_manifest``).  DB files
    (``.db`` suffix) are requested with a special flag so the peer snapshots them
    atomically before sending.

    Returns the number of files successfully written.
    """
    if not wanted:
        return 0

    url = peer["url"].rstrip("/")
    public_key_hex = peer["public_key"]
    peer_id = peer.get("id", "")

    paths = [e["path"] for e in wanted]

    # Split into DB files and regular files
    db_paths = [p for p in paths if p.endswith(".db")]
    regular_paths = [p for p in paths if not p.endswith(".db")]

    total_written = 0

    # Pull regular files
    if regular_paths:
        written = await _pull_batch(url, public_key_hex, regular_paths, snapshot_db=False)
        total_written += written

    # Pull DB files (each snapshot atomically)
    for dbp in db_paths:
        written = await _pull_batch(url, public_key_hex, [dbp], snapshot_db=True)
        total_written += written

    return total_written


async def _pull_batch(url: str, public_key_hex: str, paths: List[str], snapshot_db: bool) -> int:
    """POST /api/v1/p2p/pull for a batch of files.  Returns count written."""
    import httpx

    body = {"paths": paths, "snapshot_db": snapshot_db}
    body_bytes = json.dumps(body).encode()

    sig, ts = identity.sign_request("POST", "/api/v1/p2p/pull", body_bytes)

    headers = {
        "Content-Type": "application/json",
        "X-P2P-Instance-Id": identity.instance_id(),
        "X-P2P-Signature": sig,
        "X-P2P-Timestamp": ts,
    }

    try:
        async with httpx.AsyncClient(timeout=120.0, verify=False) as client:
            resp = await client.post(
                f"{url}/api/v1/p2p/pull",
                content=body_bytes,
                headers=headers,
            )
            if resp.status_code != 200:
                logger.warning("Pull from %s returned %d: %s", url, resp.status_code, resp.text[:200])
                return 0

            data = resp.json()
            files = data.get("files", [])
            written = 0
            for f in files:
                try:
                    _write_received_file(f)
                    written += 1
                except Exception as e:
                    logger.warning("Failed to write pulled file %s: %s", f.get("path"), e)
            return written

    except Exception as e:
        logger.warning("Pull request to %s failed: %s", url, e)
        return 0


def _write_received_file(entry: Dict, peer_id: str = "") -> None:
    """Write a received file entry to disk.  DB files are atomically swapped.

    If the local file already exists and has different content, it is backed up
    to data/db/backups/p2p-conflict-<iso>-<filename> before the overwrite.
    """
    import shutil as _shutil
    from datetime import datetime, timezone as _timezone

    relpath = entry["path"]
    content_b64 = entry.get("content_b64", "")
    content = base64.b64decode(content_b64)
    dest = _PROJECT_ROOT / relpath

    # Verify checksum
    expected_sha = entry.get("sha256", "")
    actual_sha = hashlib.sha256(content).hexdigest()
    if expected_sha and actual_sha != expected_sha:
        raise ValueError(f"Checksum mismatch for {relpath}: expected {expected_sha[:12]}, got {actual_sha[:12]}")

    os.makedirs(dest.parent, exist_ok=True)

    # Conflict backup: if local file exists and differs, save it before overwriting
    if dest.exists():
        try:
            local_sha = _file_sha256(str(dest))
            if local_sha != actual_sha:
                backup_dir = _PROJECT_ROOT / "data" / "db" / "backups"
                backup_dir.mkdir(parents=True, exist_ok=True)
                iso = datetime.now(_timezone.utc).isoformat().replace(":", "-")
                backup_name = f"p2p-conflict-{iso}-{os.path.basename(relpath)}"
                backup_path = backup_dir / backup_name
                _shutil.move(str(dest), str(backup_path))
                if peer_id:
                    from app.p2p.conflict_log import log_file_overwrite_conflict
                    log_file_overwrite_conflict(
                        peer_id=peer_id,
                        file_path=relpath,
                        local_sha256=local_sha,
                        remote_sha256=actual_sha,
                        backup_path=str(backup_path),
                    )
                logger.info("P2P conflict backup: %s -> %s", relpath, backup_name)
        except Exception as e:
            logger.warning("Failed to create conflict backup for %s: %s", relpath, e)

    if relpath.endswith(".db"):
        # Atomic swap for DB files: write to temp, verify it opens, then move
        tmp = dest.with_suffix(dest.suffix + ".p2p-tmp")
        tmp.write_bytes(content)
        # Quick integrity check: does SQLite open it?
        try:
            subprocess.run(
                ["sqlite3", str(tmp), "PRAGMA integrity_check;"],
                capture_output=True, timeout=15,
            )
        except Exception:
            tmp.unlink(missing_ok=True)
            raise
        # Atomic rename
        shutil.move(str(tmp), str(dest))
    else:
        # Regular file: write to temp then rename
        tmp = dest.with_suffix(dest.suffix + ".p2p-tmp")
        tmp.write_bytes(content)
        shutil.move(str(tmp), str(dest))


def snapshot_db_file(db_relpath: str) -> bytes:
    """Create an atomic, consistent snapshot of a SQLite database file using
    ``sqlite3 .backup`` and return its bytes.  The original DB is untouched."""
    src = _PROJECT_ROOT / db_relpath
    if not src.exists():
        raise FileNotFoundError(f"DB file not found: {db_relpath}")

    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        subprocess.run(
            ["sqlite3", str(src), f".backup '{tmp_path}'"],
            capture_output=True, timeout=30, check=True,
        )
        content = Path(tmp_path).read_bytes()
        return content
    finally:
        Path(tmp_path).unlink(missing_ok=True)


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

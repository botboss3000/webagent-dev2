"""Migration and rotation helpers for encrypted user workspace files."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from app import user_workspace as ws
from app.encryption.workspace_files import (
    MAGIC,
    encrypt_file_in_place,
    is_encrypted,
    rewrap_file_key,
    workspace_key_id,
)

# These contain live databases, a browser profile, or server-side GenUI source
# whose current consumers require ordinary seekable files.  They need their own
# storage-specific encryption rather than the workspace artifact codec.
_EXCLUDED_TOP_LEVEL = {"chrome-profile", "genui"}
_EXCLUDED_SUFFIXES = {".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm", ".db-journal"}


def _eligible_files(home: Path) -> Iterable[Path]:
    if not home.exists():
        return
    for path in home.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            relative = path.relative_to(home)
        except ValueError:
            continue
        if relative.parts and relative.parts[0].lower() in _EXCLUDED_TOP_LEVEL:
            continue
        lower_name = path.name.lower()
        if any(lower_name.endswith(suffix) for suffix in _EXCLUDED_SUFFIXES):
            continue
        if lower_name.endswith(".tmp") or lower_name.startswith("."):
            continue
        yield path


async def migrate_workspace(owner_id: str) -> dict:
    """Encrypt legacy plaintext artifacts in one workspace. Idempotent."""
    tenant = workspace_key_id(owner_id)
    home = ws.base_dir() / tenant
    encrypted = already_encrypted = errors = 0
    for path in _eligible_files(home):
        try:
            with path.open("rb") as stream:
                prefix = stream.read(len(MAGIC))
            if is_encrypted(prefix):
                already_encrypted += 1
            elif await encrypt_file_in_place(tenant, path):
                encrypted += 1
        except Exception:
            errors += 1
    return {
        "ok": errors == 0,
        "user_id": tenant,
        "files_encrypted": encrypted,
        "already_encrypted": already_encrypted,
        "errors": errors,
    }


async def migrate_all_workspaces() -> dict:
    """Encrypt legacy artifacts for every directory under the user-data root."""
    root = ws.base_dir()
    results = []
    if root.exists():
        for home in sorted(root.iterdir(), key=lambda item: item.name):
            if home.is_dir() and not home.is_symlink():
                results.append(await migrate_workspace(home.name))
    return {
        "ok": all(item["ok"] for item in results),
        "workspaces": len(results),
        "files_encrypted": sum(item["files_encrypted"] for item in results),
        "already_encrypted": sum(item["already_encrypted"] for item in results),
        "errors": sum(item["errors"] for item in results),
        "results": results,
    }


async def rewrap_workspace(owner_id: str) -> dict:
    """Re-wrap every object key in one workspace with its active DEK."""
    tenant = workspace_key_id(owner_id)
    home = ws.base_dir() / tenant
    rewrapped = unchanged = errors = 0
    for path in _eligible_files(home):
        try:
            if await rewrap_file_key(tenant, path):
                rewrapped += 1
            else:
                unchanged += 1
        except Exception:
            errors += 1
    return {
        "ok": errors == 0,
        "user_id": tenant,
        "files_rewrapped": rewrapped,
        "unchanged": unchanged,
        "errors": errors,
    }

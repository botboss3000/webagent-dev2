"""File base editor API for WebAgent.

Provides admin-only endpoints for browsing, reading, and editing files
inside the project root. Intended to back the VS Code-style file editor
page in the web UI.

Path safety: every path is resolved relative to _PROJECT_ROOT and any
attempt to escape the root (via `..` or absolute paths) is rejected with
a 400. Hidden dotfiles and common build/cache directories are skipped
from listings by default.
"""

import base64
import logging
import os
import shutil
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.auth.jwt import decode_token
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/files", tags=["files"])

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
_BOOTSTRAP_ADMIN_ID = "admin"

# Names to hide from the tree by default. Users can still address them
# directly if they know the path.
_HIDDEN_NAMES = {
    ".git", "__pycache__", "node_modules", ".venv", "venv",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", ".source-backups",
    ".DS_Store", "dist", "build", ".next",
}

# Max bytes returned by read endpoint. Files above this come back with a
# `truncated: true` flag and `binary: true` if non-utf8.
_MAX_READ_BYTES = 2 * 1024 * 1024  # 2 MB
_MAX_WRITE_BYTES = 5 * 1024 * 1024  # 5 MB


# ── Auth ────────────────────────────────────────────────────────────

def _user_id(request: Request) -> str:
    """Resolve the requesting user_id from a VERIFIED token only.

    SECURITY (changed 2026-06-28): this used to fall back to a raw ``?user_id=``
    query param when the JWT couldn't decode — which let anyone request
    ``/api/v1/files/...?user_id=admin`` with no token and get admin-level read
    /write of the host filesystem. That unverified-identity escape hatch is
    removed. Identity now comes only from:

      1. a valid ``Authorization: Bearer`` JWT, or
      2. a valid ``?token=`` JWT (query-param auth used by some FE callers).

    Still gated by ``db.is_user_admin`` downstream via ``_require_admin``.
    """
    auth = request.headers.get("Authorization", "")
    token = auth[7:] if auth.startswith("Bearer ") else request.query_params.get("token", "")
    if token:
        payload = decode_token(token)
        if payload and (payload.get("user_id") or payload.get("sub")):
            return payload.get("user_id") or payload.get("sub")
    return ""


async def _is_admin(user_id: str) -> bool:
    """Return True only when user_profiles.is_admin = 1 for this user.

    No bootstrap shortcut — the admin user is already promoted
    by the DB migration in app/db/local.py, so the table is the single
    source of truth.
    """
    if not user_id:
        return False
    try:
        return bool(await get_db().is_user_admin(user_id))
    except Exception as e:
        logger.warning("is_user_admin lookup failed for %s: %s", user_id, e)
        return False


async def _require_admin(request: Request) -> None:
    if not await _is_admin(_user_id(request)):
        raise HTTPException(status_code=403, detail="Admin access required")


# ── Path resolution ─────────────────────────────────────────────────

def _resolve(path_str: str) -> Path:
    """Resolve a user-supplied path.

    - Empty string defaults to the project root.
    - Relative paths are resolved against the project root.
    - Absolute paths are honoured as-is, so the admin can browse anywhere
      on the host (this matches the intent of an embeddable file base
      editor — see also `_require_admin`, which is the access gate).
    """
    s = (path_str or "").strip()
    if not s:
        return _PROJECT_ROOT.resolve()
    p = Path(s)
    if not p.is_absolute():
        p = _PROJECT_ROOT / s.lstrip("/").lstrip("\\")
    return p.resolve()


def _abs(path: Path) -> str:
    """Stringified absolute path, normalised to forward slashes so the
    frontend can split on `/` regardless of the host OS. Windows-style
    paths still keep their drive letter (`C:/...`)."""
    return str(path.resolve()).replace(os.sep, "/")


# ── Models ──────────────────────────────────────────────────────────

class WriteRequest(BaseModel):
    path: str = Field(..., min_length=1)
    content: str = ""
    encoding: str = "utf-8"  # "utf-8" or "base64"


class CreateRequest(BaseModel):
    path: str = Field(..., min_length=1)
    kind: str = Field("file", pattern="^(file|dir)$")


class RenameRequest(BaseModel):
    path: str = Field(..., min_length=1)
    new_path: str = Field(..., min_length=1)


class DeleteRequest(BaseModel):
    path: str = Field(..., min_length=1)


# ── Endpoints ───────────────────────────────────────────────────────

@router.get("/check-access")
async def check_access(request: Request):
    """Return whether the requesting user can use the file editor.

    Also reports the user_id we resolved from the JWT so the frontend
    can surface a helpful message ("you're signed in as <X>, not admin")
    when access is denied.
    """
    uid = _user_id(request)
    return {
        "is_admin": await _is_admin(uid),
        "user_id": uid,
        "authenticated": bool(uid),
    }


@router.get("/tree")
async def list_tree(request: Request, path: str = "", show_hidden: bool = False):
    """List the immediate children of the given directory.

    Returns directories first (alphabetical), then files. An empty
    `path` defaults to the project root; an absolute path browses
    anywhere on the host filesystem.
    """
    await _require_admin(request)
    target = _resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if not target.is_dir():
        raise HTTPException(status_code=400, detail="Path is not a directory")

    target_str = _abs(target)
    project_root_str = _abs(_PROJECT_ROOT)
    is_under_project_root = (
        target_str == project_root_str
        or target_str.startswith(project_root_str + "/")
    )

    dirs, files = [], []
    try:
        for entry in target.iterdir():
            name = entry.name
            # Only apply the curated hide list inside the project root —
            # outside, we mimic a normal file explorer and show everything
            # except dotfiles (which the user can toggle with show_hidden).
            if not show_hidden:
                if is_under_project_root:
                    if name.startswith(".") or name in _HIDDEN_NAMES:
                        if name not in {".env.example", ".gitignore", ".github"}:
                            continue
                else:
                    if name.startswith("."):
                        continue
            try:
                is_dir = entry.is_dir()
                size = 0 if is_dir else entry.stat().st_size
            except OSError:
                continue
            (dirs if is_dir else files).append({
                "name": name,
                "path": _abs(entry),
                "is_dir": is_dir,
                "size": size,
            })
    except PermissionError:
        raise HTTPException(status_code=403, detail="Permission denied")

    dirs.sort(key=lambda x: x["name"].lower())
    files.sort(key=lambda x: x["name"].lower())

    parent = target.parent
    parent_str = _abs(parent) if parent != target else None

    return {
        "path": target_str,
        "parent": parent_str,
        "project_root": project_root_str,
        "entries": dirs + files,
    }


@router.get("/read")
async def read_file(request: Request, path: str):
    """Read a file's contents. Returns utf-8 text when possible,
    otherwise base64-encoded bytes with `binary: true`."""
    await _require_admin(request)
    target = _resolve(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="File not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="Path is not a file")

    size = target.stat().st_size
    truncated = False
    read_size = size
    if size > _MAX_READ_BYTES:
        truncated = True
        read_size = _MAX_READ_BYTES

    with target.open("rb") as f:
        raw = f.read(read_size)

    try:
        text = raw.decode("utf-8")
        return {
            "path": _abs(target),
            "content": text,
            "encoding": "utf-8",
            "binary": False,
            "size": size,
            "truncated": truncated,
        }
    except UnicodeDecodeError:
        return {
            "path": _abs(target),
            "content": base64.b64encode(raw).decode("ascii"),
            "encoding": "base64",
            "binary": True,
            "size": size,
            "truncated": truncated,
        }


@router.post("/write")
async def write_file(request: Request, body: WriteRequest):
    """Write a file. Creates parent directories as needed."""
    await _require_admin(request)
    target = _resolve(body.path)
    if target.exists() and target.is_dir():
        raise HTTPException(status_code=400, detail="Path is a directory")

    if body.encoding == "base64":
        try:
            data = base64.b64decode(body.content)
        except Exception:
            raise HTTPException(status_code=400, detail="Invalid base64 content")
    else:
        data = body.content.encode("utf-8")

    if len(data) > _MAX_WRITE_BYTES:
        raise HTTPException(status_code=413, detail=f"File exceeds {_MAX_WRITE_BYTES} bytes")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return {"path": _abs(target), "size": len(data), "status": "ok"}


@router.post("/create")
async def create_entry(request: Request, body: CreateRequest):
    """Create an empty file or a directory."""
    await _require_admin(request)
    target = _resolve(body.path)
    if target.exists():
        raise HTTPException(status_code=409, detail="Path already exists")

    if body.kind == "dir":
        target.mkdir(parents=True, exist_ok=False)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.touch(exist_ok=False)
    return {"path": _abs(target), "kind": body.kind, "status": "ok"}


@router.post("/rename")
async def rename_entry(request: Request, body: RenameRequest):
    """Rename or move a file/directory inside the project root."""
    await _require_admin(request)
    src = _resolve(body.path)
    dst = _resolve(body.new_path)
    if not src.exists():
        raise HTTPException(status_code=404, detail="Source not found")
    if dst.exists():
        raise HTTPException(status_code=409, detail="Destination already exists")
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return {"from": _abs(src), "to": _abs(dst), "status": "ok"}


@router.post("/delete")
async def delete_entry(request: Request, body: DeleteRequest):
    """Delete a file or directory (recursive for directories)."""
    await _require_admin(request)
    target = _resolve(body.path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="Path not found")
    if target.resolve() == _PROJECT_ROOT.resolve():
        raise HTTPException(status_code=400, detail="Cannot delete project root")
    if target.is_dir():
        shutil.rmtree(target)
    else:
        target.unlink()
    return {"path": _abs(target), "status": "deleted"}

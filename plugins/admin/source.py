"""
Source file management endpoints — project-tree filesystem access + shell.

Allows reading, writing, deleting files, and running shell commands.
Used by agent tools (read_source, write_source, edit_source, delete_source, run_command).

SECURITY (added 2026-06-28):
  • Every HTTP endpoint here is ADMIN-ONLY. The router carries a single
    `_require_source_admin` dependency that resolves the caller from a
    cryptographically-verified JWT (Authorization header or ?token=) — or, in
    'open' access mode, the local bootstrap admin — and refuses anyone who is
    not a DB admin. Before this gate the whole router was reachable with NO
    auth, which made `/admin/source/exec` an unauthenticated remote-shell.
  • File reads/writes/deletes are CONFINED to the project tree (`_confine`).
    Absolute paths or `..` escapes outside the repo are rejected. (Shell
    commands via /exec are not path-confined by nature — the admin gate is
    their protection.)
  • The agent's own tools (plugins/admin/source_tools.py) call the in-process
    `_do_*` helpers below DIRECTLY, not over HTTP, so they never need to send a
    token to localhost and the HTTP surface can stay locked down.

To disable: delete this file and source_tools.py, remove the import from main.py.
Guardrails can be added by implementing plugins/admin/guardrails.py.
"""

import ast
import asyncio
import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

# Optional guardrails — delete guardrails.py to remove restrictions
try:
    from plugins.admin.guardrails import check_path, check_command
    _HAS_GUARDRAILS = True
except ImportError:
    _HAS_GUARDRAILS = False

    async def check_path(path: str, action: str = "read") -> None:
        pass

    async def check_command(command: str) -> None:
        pass

logger = logging.getLogger(__name__)

# Project root for resolving relative paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

BACKUP_DIR = PROJECT_ROOT / ".source-backups"
BACKUP_DIR.mkdir(exist_ok=True)

# Syntax checkers for safety on write
SYNTAX_CHECKERS = {
    ".py": lambda code: ast.parse(code),
    ".json": lambda code: __import__("json").loads(code),
}


# ── Auth gate ───────────────────────────────────────────────────────────────
# Single chokepoint for the whole router. Resolves the caller from a verified
# JWT (or the open-mode local admin) and requires a DB admin, mirroring
# app/api/files.py._require_admin. Attached as a router-level dependency so
# EVERY endpoint below — read, write, delete, exec, backups — is gated with no
# per-route wiring to forget.

async def _is_admin(user_id: str) -> bool:
    if not user_id:
        return False
    try:
        from app.db import get_db
        return bool(await get_db().is_user_admin(user_id))
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("is_user_admin lookup failed for %s: %s", user_id, e)
        return False


async def _require_source_admin(request: Request) -> None:
    """Admin gate for the source router.

    Caller identity comes from `request_user_id` (verified Bearer/`?token=`
    JWT, or the bootstrap admin in 'open' access mode) — never from a
    client-claimed id. A non-admin (or unauthenticated) caller gets 403.
    """
    from app.auth.identity import request_user_id
    uid = request_user_id(request)
    if not await _is_admin(uid):
        raise HTTPException(status_code=403, detail="Admin access required")


router = APIRouter(
    prefix="/admin/source",
    tags=["admin"],
    dependencies=[Depends(_require_source_admin)],
)


# ── Models ────────────────────────────────────────────────────────────────────

class FileInfo(BaseModel):
    path: str
    size_bytes: int
    is_dir: bool


class FileContent(BaseModel):
    path: str
    content: str
    size_bytes: int
    extension: str


class WriteRequest(BaseModel):
    path: str
    content: str
    create_backup: bool = True


class WriteResponse(BaseModel):
    path: str
    wrote: bool
    backup_path: Optional[str] = None
    message: str


class DeleteRequest(BaseModel):
    path: str
    recursive: bool = False


class DeleteResponse(BaseModel):
    path: str
    deleted: bool
    message: str


class CommandRequest(BaseModel):
    command: str
    timeout: int = 30


class CommandResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


# ── Helpers ───────────────────────────────────────────────────────────────────

def resolve_path(raw_path: str) -> Path:
    """
    Resolve a path. If relative, it's relative to the project root.

    On Windows, paths like ``/tmp`` have ``is_absolute() == False`` but
    ``p.root == '\\'``, and Python's Path division (``PROJECT_ROOT / p``)
    *still* treats them as absolute (it sees the root separator and replaces
    the left side, producing ``C:\\tmp``).  To prevent this we strip the
    leading separator before joining so they land inside the project tree.

    NOTE: this only *resolves* a path; callers that touch the filesystem must
    pass the result through ``_confine`` so an absolute path or ``..`` escape
    outside the repo is rejected.
    """
    p = Path(raw_path)
    if not p.is_absolute() or (os.name == 'nt' and not p.drive and p.root and len(p.root) == 1):
        # Strip leading separator on Windows so Path division doesn't treat
        # it as absolute (e.g. /tmp -> tmp -> PROJECT_ROOT/tmp).
        p = PROJECT_ROOT / str(p).lstrip('\\/')
    return p.resolve()


def _confine(resolved: Path) -> Path:
    """Reject a resolved path that escapes the project tree.

    Uses ``Path.is_relative_to`` (a real prefix test) rather than a string
    ``startswith`` so a sibling directory like ``…/webagent-dev-attacker``
    cannot pass the check. Raises HTTP 400 on escape.
    """
    try:
        if resolved == PROJECT_ROOT or resolved.is_relative_to(PROJECT_ROOT):
            return resolved
    except AttributeError:  # pragma: no cover - Python < 3.9
        if str(resolved).startswith(str(PROJECT_ROOT) + os.sep):
            return resolved
    raise HTTPException(
        status_code=400,
        detail=f"Path escapes the project root: {resolved}",
    )


def validate_syntax(content: str, ext: str) -> Optional[str]:
    """Validate syntax. Returns error message or None."""
    checker = SYNTAX_CHECKERS.get(ext)
    if checker is None:
        return None
    try:
        checker(content)
        return None
    except (SyntaxError, ValueError) as e:
        return str(e)


# ── In-process core logic ─────────────────────────────────────────────────────
# Both the HTTP endpoints (admin-gated above) and the agent's server-side tools
# (source_tools.py) call these directly. The agent path is in-process and
# trusted, so it bypasses the HTTP auth gate without needing a localhost token;
# the HTTP path is reachable only after `_require_source_admin`.

async def _do_read(path: str) -> FileContent:
    resolved = _confine(resolve_path(path))
    try:
        await check_path(str(resolved), "read")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"File not found: {resolved}")
    if not resolved.is_file():
        raise HTTPException(status_code=400, detail=f"Not a file: {resolved}")

    content = resolved.read_text(encoding="utf-8")
    return FileContent(
        path=str(resolved),
        content=content,
        size_bytes=resolved.stat().st_size,
        extension=resolved.suffix.lower(),
    )


async def _do_write(path: str, content: str, create_backup: bool = True) -> WriteResponse:
    resolved = _confine(resolve_path(path))
    ext = resolved.suffix.lower()

    try:
        await check_path(str(resolved), "write")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    is_new = not resolved.exists()

    error = validate_syntax(content, ext)
    if error:
        raise HTTPException(status_code=400, detail=f"Syntax error: {error}")

    backup_path = None
    if not is_new and create_backup:
        backup_filename = f"{resolved.name}.{int(time.time())}.bak"
        backup_file = BACKUP_DIR / backup_filename
        try:
            shutil.copy2(resolved, backup_file)
            backup_path = str(backup_file)
        except Exception as e:
            logger.warning("Backup failed: %s", e)

    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(content, encoding="utf-8")

    action = "Created" if is_new else "Updated"
    logger.info("%s: %s", action, resolved)
    return WriteResponse(
        path=str(resolved),
        wrote=True,
        backup_path=backup_path,
        message=f"{action} {resolved}",
    )


async def _do_delete(path: str, recursive: bool = False) -> DeleteResponse:
    resolved = _confine(resolve_path(path))

    try:
        await check_path(str(resolved), "delete")
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))

    if not resolved.exists():
        raise HTTPException(status_code=404, detail=f"Not found: {resolved}")

    try:
        if resolved.is_file():
            resolved.unlink()
            logger.info("Deleted file: %s", resolved)
            return DeleteResponse(
                path=str(resolved), deleted=True,
                message=f"Deleted file {resolved}",
            )
        elif resolved.is_dir():
            if not recursive:
                raise HTTPException(
                    status_code=400,
                    detail=f"'{resolved}' is a directory. Set recursive=true to delete it.",
                )
            shutil.rmtree(resolved)
            logger.info("Deleted directory: %s", resolved)
            return DeleteResponse(
                path=str(resolved), deleted=True,
                message=f"Deleted directory {resolved}",
            )
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Delete failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

    raise HTTPException(status_code=500, detail="Unexpected error")


async def _do_exec(command: str, timeout: int = 30) -> CommandResponse:
    try:
        await check_command(command)
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=str(e))
    logger.info("Executing command: %s", command)
    try:
        kwargs = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "cwd": str(PROJECT_ROOT),
        }
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_shell(command, **kwargs)
        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=max(1, int(timeout)))
        except asyncio.TimeoutError:
            # Kill the complete tree. On Windows, killing only cmd.exe leaves a
            # PowerShell grandchild holding captured-output pipes forever; that
            # was sufficient to deadlock the whole ASGI event loop.
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/PID", str(proc.pid), "/T", "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                try:
                    await asyncio.wait_for(killer.wait(), timeout=5)
                except asyncio.TimeoutError:
                    killer.kill()
            else:
                try:
                    os.killpg(proc.pid, 9)
                except ProcessLookupError:
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
            return CommandResponse(
                exit_code=-1,
                stdout="",
                stderr=f"Command timed out after {timeout} seconds",
                timed_out=True,
            )
        stdout = stdout_b.decode(errors="replace")
        stderr = stderr_b.decode(errors="replace")
        return CommandResponse(
            exit_code=proc.returncode or 0,
            stdout=stdout[-50000:],
            stderr=stderr[-50000:],
        )
    except Exception as e:
        return CommandResponse(
            exit_code=-1,
            stdout="",
            stderr=str(e),
        )


# ── Endpoints (admin-gated via the router dependency) ──────────────────────────

@router.get("/read", response_model=FileContent)
async def read_file(path: str = Query(..., description="Path to file (relative or absolute)")):
    """Read any file inside the project tree."""
    return await _do_read(path)


@router.post("/write", response_model=WriteResponse)
async def write_file(request: WriteRequest):
    """
    Create a new file or overwrite an existing one.
    - Creates parent directories automatically
    - Validates Python/JSON syntax before writing
    - Backs up existing files to .source-backups/
    """
    return await _do_write(request.path, request.content, request.create_backup)


@router.post("/delete", response_model=DeleteResponse)
async def delete_file(request: DeleteRequest):
    """
    Delete a file or directory.
    - Files are deleted permanently (not sent to trash)
    - Directories require recursive=True
    """
    return await _do_delete(request.path, request.recursive)


@router.post("/exec", response_model=CommandResponse)
async def run_command(request: CommandRequest):
    """
    Execute a shell command on the server (in the project root).
    Returns stdout, stderr, and exit code.
    """
    return await _do_exec(request.command, request.timeout)


@router.get("/backups")
async def list_backups():
    """List available backups from previous writes."""
    if not BACKUP_DIR.exists():
        return []
    backups = []
    for f in sorted(BACKUP_DIR.iterdir(), key=lambda x: x.stat().st_mtime, reverse=True):
        if f.is_file():
            backups.append({
                "filename": f.name,
                "size_bytes": f.stat().st_size,
                "modified": f.stat().st_mtime,
            })
    return backups

"""Selective Update / Sync from the upstream public repo — admin-only.

Drop-in BACKEND for the Update admin view. Discovered + mounted by the page
catalog (app/ui_pages.py discover_routers, via this folder's page.json `router`
field) — so the API comes and goes with the folder, with NO edit to app/main.py.
Frontend: ui/admin-tools/update/update.js.

Pulls changes from a public "upstream" repo for only the folders the operator
selects, with a real 3-way merge so local customizations survive. Endpoints:
  GET  /api/v1/update/status   — deployment model + syncable folders
  GET  /api/v1/update/config   POST same — persisted config
  POST /api/v1/update/init     — git-init + add upstream (non-git deployments)
  POST /api/v1/update/fetch    — fetch upstream branch
  GET  /api/v1/update/diff     — unified diff of one path vs upstream
  POST /api/v1/update/apply    — 3-way merge selected paths
Reuses app.api.github helpers so auth + admin gating match the Git Control
view. Delete this folder to remove the feature.
REMOVE-WHEN: the Update view is dropped from the admin page catalog.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from app.api.github import (
    _PROJECT_ROOT, _cache_token, _get_token, _require_admin, _run_git,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/update", tags=["admin"])

UPSTREAM_REMOTE = "upstream"
CONFIG_FILE = _PROJECT_ROOT / "data" / "config" / "update_sync.json"
BACKUP_ROOT = _PROJECT_ROOT / ".update-backups"
_DEEP_PARENTS = ("app", "plugins")
_DEFAULT_CONFIG = {"upstream_url": "", "upstream_branch": "main", "tracked_paths": [], "last_synced_commit": ""}


# ── Config persistence ──
def _load_config() -> dict:
    cfg = dict(_DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.is_file():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")) or {})
    except Exception as e:
        logger.warning("update_sync: could not read config: %s", e)
    return cfg


def _save_config(cfg: dict) -> None:
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ── Git helpers ──
def _is_git_repo() -> bool:
    out, _, rc = _run_git(["rev-parse", "--is-inside-work-tree"])
    return rc == 0 and out.strip() == "true"


def _upstream_ref(cfg: dict) -> str:
    return f"{UPSTREAM_REMOTE}/{cfg.get('upstream_branch') or 'main'}"


def _ensure_upstream_remote(url: str) -> None:
    out, _, rc = _run_git(["remote", "get-url", UPSTREAM_REMOTE])
    if rc == 0:
        if out.strip() != url:
            _run_git(["remote", "set-url", UPSTREAM_REMOTE, url])
    else:
        _run_git(["remote", "add", UPSTREAM_REMOTE, url])


def _list_tree_dirs(ref: str, prefix: str = "") -> set[str]:
    args = ["ls-tree", "-d", "--name-only", ref]
    if prefix:
        args.append(prefix.rstrip("/") + "/")
    out, _, rc = _run_git(args)
    if rc != 0:
        return set()
    return {line.strip().rstrip("/") for line in out.splitlines() if line.strip()}


def _candidate_paths(cfg: dict) -> list[str]:
    ref = _upstream_ref(cfg)
    paths: set[str] = set()
    for src in ("HEAD", ref):
        paths |= _list_tree_dirs(src)
        for parent in _DEEP_PARENTS:
            paths |= _list_tree_dirs(src, parent)
    return sorted(paths, key=lambda p: (p.count("/"), p))


def _numstat(cfg: dict) -> list[tuple[int, int, str]]:
    ref = _upstream_ref(cfg)
    out, _, rc = _run_git(["diff", "--numstat", "HEAD", ref], timeout=30)
    rows: list[tuple[int, int, str]] = []
    if rc != 0:
        return rows
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        a = -1 if parts[0] == "-" else int(parts[0] or 0)
        d = -1 if parts[1] == "-" else int(parts[1] or 0)
        rows.append((a, d, parts[2]))
    return rows


def _path_matches(file_path: str, tracked: str) -> bool:
    return file_path == tracked or file_path.startswith(tracked.rstrip("/") + "/")


def _show_blob(ref_path: str) -> Optional[bytes]:
    try:
        proc = subprocess.run(["git", "show", ref_path], cwd=str(_PROJECT_ROOT), capture_output=True, timeout=20)
        if proc.returncode != 0:
            return None
        return proc.stdout
    except Exception:
        return None


def _is_binary(data: Optional[bytes]) -> bool:
    if not data:
        return False
    return b"\x00" in data[:8000]


# ── Models ──
class ConfigIn(BaseModel):
    upstream_url: Optional[str] = None
    upstream_branch: Optional[str] = None
    tracked_paths: Optional[list[str]] = None


class InitIn(BaseModel):
    upstream_url: str
    upstream_branch: str = "main"


class ApplyIn(BaseModel):
    paths: list[str]
    create_backup: bool = True


# This module is loaded dynamically (page-catalog drop-in), and it uses
# `from __future__ import annotations`, so the models above carry their field
# types as *string* annotations. FastAPI 0.115+ generates the OpenAPI schema
# lazily and can no longer locate this module's namespace to resolve them,
# raising "model is not fully defined" (PydanticUserError). Resolving them now,
# while the module namespace is live, fixes both schema generation and request
# body validation for the POST endpoints below.
ConfigIn.model_rebuild()
InitIn.model_rebuild()
ApplyIn.model_rebuild()


# ── Endpoints ──
@router.get("/status")
async def status(request: Request):
    _require_admin(request)
    _cache_token(_get_token())
    cfg = _load_config()
    is_git = _is_git_repo()
    if not is_git:
        return {"mode": "non-git", "is_git": False, "upstream_url": cfg.get("upstream_url", ""),
                "upstream_branch": cfg.get("upstream_branch", "main"),
                "message": ("This deployment is not a git repository, so changes cannot be merged "
                            "from upstream yet. Initialize git + add the upstream remote to enable "
                            "selective updates.")}
    upstream_url = cfg.get("upstream_url", "")
    has_upstream = False
    if upstream_url:
        _ensure_upstream_remote(upstream_url)
        out, _, rc = _run_git(["remote", "get-url", UPSTREAM_REMOTE])
        has_upstream = rc == 0 and bool(out.strip())
    candidates = _candidate_paths(cfg) if has_upstream else []
    local_dirs = {p for p in candidates if (_PROJECT_ROOT / p).is_dir()}
    tracked = cfg.get("tracked_paths") or []
    if not tracked and candidates:
        tracked = sorted(local_dirs)
        cfg["tracked_paths"] = tracked
        _save_config(cfg)
    changed_by_path: dict[str, int] = {}
    if has_upstream:
        for _a, _d, fp in _numstat(cfg):
            best = ""
            for c in candidates:
                if _path_matches(fp, c) and len(c) > len(best):
                    best = c
            if best:
                changed_by_path[best] = changed_by_path.get(best, 0) + 1
    folders = [{"path": p, "present_locally": p in local_dirs, "tracked": p in tracked,
                "changed_files": changed_by_path.get(p, 0)} for p in candidates]
    return {"mode": "git", "is_git": True, "upstream_url": upstream_url,
            "upstream_branch": cfg.get("upstream_branch", "main"), "has_upstream": has_upstream,
            "last_synced_commit": cfg.get("last_synced_commit", ""), "folders": folders}


@router.get("/notifier-status")
async def notifier_status(request: Request):
    """Cheap update probe for the header notifier icon.

    Polled by ui/shared/js/update-notifier.js. Uses `git ls-remote` (one
    round-trip, no object download, works even when this deployment is not a
    git repo) and only performs a real fetch + commit-count when the upstream
    tip has MOVED since the last check — so steady state costs a single
    ls-remote. Results are cached in the config so counts survive between polls.
    """
    _require_admin(request)
    _cache_token(_get_token())
    cfg = _load_config()
    url = (cfg.get("upstream_url") or "").strip()
    if not url:
        return {"configured": False}
    branch = cfg.get("upstream_branch") or "main"

    out, err, rc = _run_git(["ls-remote", url, f"refs/heads/{branch}"], timeout=30)
    if rc != 0:
        return {"configured": True, "is_git": _is_git_repo(),
                "error": (err or "upstream unreachable").strip()}
    remote_tip = out.split()[0] if out.strip() else ""

    last = cfg.get("notifier_last") or {}
    if remote_tip and remote_tip == last.get("tip"):
        # Tip unchanged — return the cached counts without a fetch.
        return {"configured": True, "is_git": _is_git_repo(), "upstream_url": url,
                "upstream_branch": branch, "remote_tip": remote_tip[:7],
                "behind": last.get("behind", 0), "ahead": last.get("ahead", 0),
                "local_head": last.get("local_head", ""),
                "latest_version": last.get("latest_version", ""),
                "has_new_release": last.get("has_new_release", False),
                "cached": True}

    behind = ahead = 0
    local_head = ""
    latest_version = ""
    if remote_tip and _is_git_repo():
        _ensure_upstream_remote(url)
        _out, _e, _rc = _run_git(["fetch", UPSTREAM_REMOTE, branch, "--quiet"], timeout=120)
        if _rc == 0:
            ref = _upstream_ref(cfg)
            bo, _, _ = _run_git(["rev-list", "--count", f"HEAD..{ref}"], timeout=30)
            behind = int(bo.strip()) if bo.strip().isdigit() else 0
            ao, _, _ = _run_git(["rev-list", "--count", f"{ref}..HEAD"], timeout=30)
            ahead = int(ao.strip()) if ao.strip().isdigit() else 0
            lho, _, _ = _run_git(["rev-parse", "HEAD"], timeout=15)
            local_head = lho.strip()[:7]
            to, _, _ = _run_git(["describe", "--tags", "--abbrev=0", ref], timeout=30)
            latest_version = to.strip()

    has_new_release = bool(latest_version) and behind > 0
    cfg["notifier_last"] = {"tip": remote_tip, "behind": behind, "ahead": ahead,
                            "local_head": local_head, "latest_version": latest_version,
                            "has_new_release": has_new_release}
    _save_config(cfg)
    return {"configured": True, "is_git": _is_git_repo(), "upstream_url": url,
            "upstream_branch": branch, "remote_tip": remote_tip[:7], "behind": behind,
            "ahead": ahead, "local_head": local_head, "latest_version": latest_version,
            "has_new_release": has_new_release, "cached": False}


@router.get("/config")
async def get_config(request: Request):
    _require_admin(request)
    return _load_config()


@router.post("/config")
async def set_config(body: ConfigIn, request: Request):
    _require_admin(request)
    cfg = _load_config()
    if body.upstream_url is not None:
        cfg["upstream_url"] = body.upstream_url.strip()
    if body.upstream_branch is not None:
        cfg["upstream_branch"] = body.upstream_branch.strip() or "main"
    if body.tracked_paths is not None:
        cfg["tracked_paths"] = sorted(set(body.tracked_paths))
    _save_config(cfg)
    if cfg.get("upstream_url") and _is_git_repo():
        _ensure_upstream_remote(cfg["upstream_url"])
    return cfg


@router.post("/init")
async def init_git(body: InitIn, request: Request):
    _require_admin(request)
    _cache_token(_get_token())
    if not _is_git_repo():
        _out, err, rc = _run_git(["init"], timeout=30)
        if rc != 0:
            raise HTTPException(status_code=500, detail=f"git init failed: {err.strip()}")
        _run_git(["add", "-A"], timeout=120)
        _run_git(["-c", "user.email=admin@webagent.local", "-c", "user.name=webagent",
                  "commit", "-m", "Baseline before upstream sync", "--quiet"], timeout=120)
    _ensure_upstream_remote(body.upstream_url.strip())
    branch = body.upstream_branch.strip() or "main"
    _out, err, rc = _run_git(["fetch", UPSTREAM_REMOTE, branch, "--quiet"], timeout=120)
    if rc != 0:
        detail = err.strip() or "fetch failed"
        if "Authentication failed" in err or "could not read" in err:
            detail += "\n\nSet your GitHub token in the Git Control sidebar if the upstream repo is private."
        raise HTTPException(status_code=500, detail=detail)
    cfg = _load_config()
    cfg["upstream_url"] = body.upstream_url.strip()
    cfg["upstream_branch"] = branch
    _save_config(cfg)
    return {"status": "initialized", "branch": branch}


@router.post("/fetch")
async def fetch_upstream(request: Request):
    _require_admin(request)
    _cache_token(_get_token())
    if not _is_git_repo():
        raise HTTPException(status_code=400, detail="Not a git repository. Initialize git first.")
    cfg = _load_config()
    url = cfg.get("upstream_url", "")
    if not url:
        raise HTTPException(status_code=400, detail="No upstream repo configured.")
    _ensure_upstream_remote(url)
    branch = cfg.get("upstream_branch") or "main"
    _out, err, rc = _run_git(["fetch", UPSTREAM_REMOTE, branch, "--quiet"], timeout=120)
    if rc != 0:
        detail = err.strip() or "fetch failed"
        if "Authentication failed" in err or "could not read" in err:
            detail += "\n\nSet your GitHub token in the Git Control sidebar if the upstream repo is private."
        raise HTTPException(status_code=500, detail=detail)
    rows = _numstat(cfg)
    tip, _, _ = _run_git(["rev-parse", _upstream_ref(cfg)])
    return {"status": "fetched", "upstream_tip": tip.strip()[:7], "total_changed_files": len(rows)}


@router.get("/diff")
async def diff(request: Request, path: str = Query(..., description="Folder/path to diff vs upstream")):
    _require_admin(request)
    if not _is_git_repo():
        raise HTTPException(status_code=400, detail="Not a git repository.")
    cfg = _load_config()
    ref = _upstream_ref(cfg)
    out, err, rc = _run_git(["diff", "HEAD", ref, "--", path], timeout=30)
    if rc != 0 and err.strip():
        raise HTTPException(status_code=500, detail=err.strip())
    return {"path": path, "diff": out[:400_000], "truncated": len(out) > 400_000}


def _merge_one_file(file_rel: str, base_ref: str, theirs_ref: str, backup_dir: Optional[Path]) -> str:
    """3-way merge a single file in place. Returns:
    'clean' | 'conflict' | 'added' | 'deleted-upstream' | 'binary' | 'error'."""
    abs_path = _PROJECT_ROOT / file_rel
    base = _show_blob(f"{base_ref}:{file_rel}") if base_ref else None
    theirs = _show_blob(f"{theirs_ref}:{file_rel}")
    ours_exists = abs_path.exists()
    ours = abs_path.read_bytes() if ours_exists else None
    if theirs is None:
        return "deleted-upstream"
    if backup_dir is not None and ours_exists:
        dest = backup_dir / file_rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(abs_path, dest)
    if not ours_exists:
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(theirs)
        return "added"
    if ours == theirs:
        return "clean"
    if _is_binary(ours) or _is_binary(theirs) or _is_binary(base):
        abs_path.write_bytes(theirs)
        return "binary"
    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        ours_tmp, base_tmp, theirs_tmp = tdp / "ours", tdp / "base", tdp / "theirs"
        ours_tmp.write_bytes(ours)
        base_tmp.write_bytes(base if base is not None else b"")
        theirs_tmp.write_bytes(theirs)
        proc = subprocess.run(
            ["git", "merge-file", "-L", "local", "-L", "base", "-L", "upstream",
             str(ours_tmp), str(base_tmp), str(theirs_tmp)],
            cwd=str(_PROJECT_ROOT), capture_output=True, timeout=30)
        abs_path.write_bytes(ours_tmp.read_bytes())
        if proc.returncode == 0:
            return "clean"
        if proc.returncode > 0:
            return "conflict"
        return "error"


@router.post("/apply")
async def apply(body: ApplyIn, request: Request):
    _require_admin(request)
    _cache_token(_get_token())
    if not _is_git_repo():
        raise HTTPException(status_code=400, detail="Not a git repository.")
    cfg = _load_config()
    ref = _upstream_ref(cfg)
    base_out, _, base_rc = _run_git(["merge-base", "HEAD", ref])
    base_ref = base_out.strip() if base_rc == 0 else ""
    selected = set(body.paths or [])
    if not selected:
        raise HTTPException(status_code=400, detail="No paths selected.")
    files = [fp for _a, _d, fp in _numstat(cfg) if any(_path_matches(fp, p) for p in selected)]
    backup_dir = None
    if body.create_backup and files:
        backup_dir = BACKUP_ROOT / time.strftime("%Y%m%d-%H%M%S")
        backup_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, list[str]] = {"clean": [], "added": [], "conflict": [], "binary": [], "deleted-upstream": [], "error": []}
    for fp in files:
        try:
            status_str = _merge_one_file(fp, base_ref, ref, backup_dir)
        except Exception as e:
            logger.warning("update_sync: merge failed for %s: %s", fp, e)
            status_str = "error"
        result.setdefault(status_str, []).append(fp)
    backend_changed = any(f.endswith(".py") for k in ("clean", "added", "binary") for f in result.get(k, []))
    tip, _, _ = _run_git(["rev-parse", ref])
    if tip.strip():
        cfg["last_synced_commit"] = tip.strip()
        _save_config(cfg)
    return {"status": "applied", "results": result, "conflict_files": result.get("conflict", []),
            "backend_changed": backend_changed, "backup_dir": str(backup_dir) if backup_dir else None}

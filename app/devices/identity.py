"""Device identity — a STABLE, per-machine id for this WebAgent instance.

When several instances share ONE database, each still needs a local identity so
jobs can address one device and each device can own a separate presence row.
This module stores that identity in this WebAgent installation's data/instance
directory, separate from data/db/app.db.

Overrides (per machine): set WEBAGENT_DEVICE_ID / WEBAGENT_DEVICE_LABEL in the
environment to pin a device's id or friendly name.
"""
from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Dict, Optional
from app.git_executor import run_git_job

_DEVICE_ID: Optional[str] = None
_DEVICE_LABEL: Optional[str] = None

# Claude-readiness probe, cached with a short TTL. A Claude-Code agent only runs on
# a device that has the `claude` CLI installed; the picker shows this per device so
# a target that can't run it is flagged BEFORE you send. Re-probed at most once a
# minute so a device that gains or loses `claude` reflects it within ~a minute
# without a restart (the user's "maybe it got deleted" case), yet a heartbeat every
# 15s doesn't scan PATH on every tick.
_CLAUDE_READY: Optional[bool] = None
_CLAUDE_READY_AT: float = 0.0
_CLAUDE_READY_TTL: float = 60.0

# Working-tree diffstat, cached briefly. Computed at most once per TTL so the
# periodic heartbeat never hammers git; on failure the cache is backdated so a
# pathological repo is retried at most ~once a minute, never on every tick.
_REPO_STATS: Optional[Dict[str, int]] = None
_REPO_STATS_AT: float = 0.0
_REPO_STATS_TTL: float = 8.0
_REPO_STATS_FAIL_BACKOFF: float = 45.0
_REPO_STATS_LOCK = threading.Lock()


def _id_file() -> Path:
    """Durable identity owned by this WebAgent installation."""
    from app.runtime_mode import data_root

    return data_root() / "instance" / "device_id"


def _legacy_id_files():
    """Pre-data/instance locations accepted only for one-time migration."""
    paths = [Path(os.path.expanduser("~")) / ".webagent" / "device_id"]
    codex_home = (os.environ.get("CODEX_HOME") or "").strip()
    if codex_home:
        paths.append(Path(os.path.expanduser(codex_home)) / "webagent" / "device_id")
    return paths


def _remove_migrated_file(path: Path) -> None:
    try:
        path.unlink()
        path.parent.rmdir()
    except OSError:
        pass


def device_id() -> str:
    """A stable id unique to THIS installation. Resolution order: explicit env
    override → data/instance file → migrated legacy file → ephemeral fallback."""
    global _DEVICE_ID
    if _DEVICE_ID:
        return _DEVICE_ID
    env = (os.environ.get("WEBAGENT_DEVICE_ID") or "").strip()
    if env:
        _DEVICE_ID = env
        return env
    try:
        target = _id_file()
        for source in (target, *_legacy_id_files()):
            if not source.exists():
                continue
            val = source.read_text(encoding="utf-8").strip()
            if val:
                # Strip legacy "dev_" prefix from old installations
                _DEVICE_ID = val[4:] if val.startswith("dev_") else val
                if source != target:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(_DEVICE_ID, encoding="utf-8")
                    _remove_migrated_file(source)
                return _DEVICE_ID
        target.parent.mkdir(parents=True, exist_ok=True)
        val = uuid.uuid4().hex[:16]
        target.write_text(val, encoding="utf-8")
        _DEVICE_ID = val
        return val
    except Exception:
        # Never crash over identity — fall back to an ephemeral id.
        _DEVICE_ID = _DEVICE_ID or uuid.uuid4().hex[:16]
        return _DEVICE_ID


def device_label() -> str:
    """A human-friendly name for this device (hostname, or an env override)."""
    global _DEVICE_LABEL
    if _DEVICE_LABEL:
        return _DEVICE_LABEL
    env = (os.environ.get("WEBAGENT_DEVICE_LABEL") or "").strip()
    if env:
        _DEVICE_LABEL = env
        return env
    try:
        _DEVICE_LABEL = socket.gethostname() or "device"
    except Exception:
        _DEVICE_LABEL = "device"
    return _DEVICE_LABEL


_REPO: Optional[Dict[str, str]] = None


def _repo_info() -> Dict[str, str]:
    """This instance's code repo — the git remote + branch of the project folder,
    so the Server Manager hub can show which repo each device runs. Read once from
    .git/config / .git/HEAD (no subprocess, no dependency) and cached. Safe when
    git is absent: returns empty strings."""
    global _REPO
    if _REPO is not None:
        return _REPO
    repo, branch = "", ""
    try:
        # app/devices/identity.py → app/devices → app → repo root.
        root = Path(__file__).resolve().parents[2]
        cfg = root / ".git" / "config"
        if cfg.exists():
            in_origin = False
            for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
                s = line.strip()
                if s.startswith("["):
                    in_origin = s.replace(" ", "").lower() == '[remote"origin"]'
                elif in_origin and s.lower().startswith("url"):
                    repo = s.split("=", 1)[1].strip() if "=" in s else ""
                    break
        head = root / ".git" / "HEAD"
        if head.exists():
            h = head.read_text(encoding="utf-8", errors="replace").strip()
            if h.startswith("ref:"):
                branch = h.rsplit("/", 1)[-1]
    except Exception:
        repo, branch = "", ""
    _REPO = {"repo": repo, "branch": branch}
    return _REPO


def repo_stats() -> Optional[Dict[str, int]]:
    """Best-effort working-tree diffstat for THIS device's app repo (the project
    root): how many files are changed and the +/− line totals since HEAD.
    Untracked files count as all-additions — the same convention as the File
    Explorer's +/− badges and Git Control's status.

    Returns None when git is missing, the folder isn't a git checkout, or the
    read fails/times out — the Instances page then simply omits the badge.
    Pinned to the project root (never the admin's Source-Control selection), so
    every device reports the state of the repo it actually runs."""
    global _REPO_STATS, _REPO_STATS_AT
    now = time.monotonic()
    if (now - _REPO_STATS_AT) < _REPO_STATS_TTL:
        return _REPO_STATS
    with _REPO_STATS_LOCK:
        now = time.monotonic()
        if (now - _REPO_STATS_AT) < _REPO_STATS_TTL:
            return _REPO_STATS
        try:
            root = Path(__file__).resolve().parents[2]
            if not (root / ".git").exists():
                _REPO_STATS = None
                _REPO_STATS_AT = time.monotonic()
                return None
            files, added, removed = 0, 0, 0
            # Read-only status/diff must never take .git/index.lock (the
            # heartbeat polls this while the agent loop may be committing) —
            # GIT_OPTIONAL_LOCKS=0 keeps git's optional index refresh from
            # writing, so these can't collide with a commit in progress.
            git_env = os.environ.copy()
            git_env["GIT_OPTIONAL_LOCKS"] = "0"
            # Tracked changes vs HEAD (staged + unstaged together).
            diff = subprocess.run(
                ["git", "-c", "core.quotePath=false", "diff", "--numstat", "--no-renames", "HEAD"],
                cwd=root, capture_output=True, text=True, timeout=3, env=git_env)
            if diff.returncode == 0:
                for line in diff.stdout.splitlines():
                    cols = line.split("\t")
                    if len(cols) < 3:
                        continue
                    a, r = cols[0], cols[1]
                    if a == "-" and r == "-":
                        continue  # binary — no meaningful line count
                    files += 1
                    added += int(a) if a.isdigit() else 0
                    removed += int(r) if r.isdigit() else 0
            # Untracked files — every line is an addition (capped so a huge
            # accidentally-untracked tree can't make this read forever).
            st = subprocess.run(
                ["git", "-c", "core.quotePath=false", "status", "--porcelain", "--untracked-files=all"],
                cwd=root, capture_output=True, text=True, timeout=3, env=git_env)
            processed = 0
            if st.returncode == 0:
                for line in st.stdout.splitlines():
                    if not line.startswith("?? "):
                        continue
                    if processed >= 1000:
                        break
                    processed += 1
                    rel = line[3:].strip().strip('"')
                    if not rel:
                        continue
                    files += 1
                    try:
                        p = root / rel
                        if p.is_file():
                            with open(p, "r", encoding="utf-8", errors="replace") as fh:
                                added += sum(1 for _ in fh)
                    except Exception:
                        pass
            _REPO_STATS = {"files": files, "insertions": added, "deletions": removed}
        except Exception:
            # Git missing / not a repo / timed out — back off before retrying so a
            # slow repo can't stall the heartbeat loop repeatedly.
            _REPO_STATS = None
            _REPO_STATS_AT = time.monotonic() + _REPO_STATS_FAIL_BACKOFF
            return None
        _REPO_STATS_AT = time.monotonic()
        return _REPO_STATS


def claude_ready() -> bool:
    """True if the `claude` CLI is installed on THIS machine (resolvable on PATH).
    Resolved the SAME way the engine launches it (shutil.which), so if this says
    ready the run will find the binary too. Cached with a short TTL — see the
    module constants above."""
    global _CLAUDE_READY, _CLAUDE_READY_AT
    now = time.monotonic()
    if _CLAUDE_READY is None or (now - _CLAUDE_READY_AT) >= _CLAUDE_READY_TTL:
        try:
            _CLAUDE_READY = bool(shutil.which("claude"))
        except Exception:
            _CLAUDE_READY = False
        _CLAUDE_READY_AT = now
    return _CLAUDE_READY


def capabilities() -> Dict[str, object]:
    """What this device is / where it runs. Kept small; enrich as the feature
    grows (e.g. has_browser, has_terminal, on_battery).

    ``claude`` = this device can run Claude-Code agents (the `claude` CLI is
    installed). The target-device picker reads it to flag a device that can't run
    a Claude agent, and it lets other devices see THIS one's readiness over the
    shared presence row (the login itself is shared via the vault, so readiness is
    purely 'is the binary here')."""
    try:
        sysname = platform.system()
    except Exception:
        sysname = ""
    info = _repo_info()
    deploy_provider = os.environ.get("WEBAGENT_DEPLOY_PROVIDER", "").strip()
    deploy_repo = os.environ.get("WEBAGENT_DEPLOY_REPO", "").strip()
    deploy_branch = os.environ.get("WEBAGENT_DEPLOY_BRANCH", "").strip()
    out: Dict[str, object] = {
        "platform": sysname,
        "hostname": device_label(),
        # Source archives do not contain .git metadata. Managed deployments
        # inject their source identity so fleet controls can still show the repo.
        "repo": deploy_repo or info["repo"],
        "branch": deploy_branch or info["branch"],
        # Working-tree diffstat (files changed / +− lines) — best-effort, None
        # when git isn't available. The Instances Overview shows it next to the
        # repo URL so a device's uncommitted work is visible at a glance.
        "repo_stats": repo_stats(),
        "claude": claude_ready(),
    }
    if deploy_provider:
        out["deployment_provider"] = deploy_provider
    if deploy_provider == "google_cloud_run":
        out["cloud_run"] = {
            "project": os.environ.get("WEBAGENT_CLOUD_RUN_PROJECT", "").strip(),
            "region": os.environ.get("WEBAGENT_CLOUD_RUN_REGION", "").strip(),
            "service": os.environ.get("WEBAGENT_CLOUD_RUN_SERVICE", "").strip(),
            "repo": deploy_repo,
            "branch": deploy_branch,
        }
    return out


async def capabilities_async() -> Dict[str, object]:
    """Resolve capabilities without running repository Git on the event loop."""
    return await run_git_job(capabilities)

"""Publish a filtered snapshot to the public repo.

Reads publish-public.json from the repo root. Takes the currently committed HEAD,
filters out excluded files, syncs to the public repo directory, and pushes.

Usage:
    python -m scripts.publish_public           # full sync + push
    python -m scripts.publish_public --dry-run  # show what would happen
"""

import fnmatch
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "publish-public.json"


def _run_text(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """Run a command and return text output (for git status, remote, etc.)."""
    kwargs: dict = {"capture_output": True, "text": True, "timeout": 60}
    if cwd:
        kwargs["cwd"] = cwd
    return subprocess.run(cmd, **kwargs, check=check)


def _run_binary(cmd: list[str], cwd: Path | None = None, check: bool = True) -> bytes:
    """Run a command and return raw binary stdout (for git archive)."""
    kwargs: dict = {"capture_output": True, "text": False, "timeout": 60}
    if cwd:
        kwargs["cwd"] = cwd
    result = subprocess.run(cmd, **kwargs, check=check)
    return result.stdout


def _should_exclude(path: str, exclude: list[str], exclude_patterns: list[str]) -> bool:
    path = path.replace("\\", "/").lstrip("/")
    for e in exclude:
        e_norm = e.replace("\\", "/").rstrip("/")
        if path == e_norm or path.startswith(e_norm + "/"):
            return True
    for pat in exclude_patterns:
        if fnmatch.fnmatch(path, pat):
            return True
        if "/" in path:
            filename = path.rsplit("/", 1)[-1]
            if fnmatch.fnmatch(filename, pat):
                return True
    return False


def _extract_filtered(archive_bytes: bytes, extract_dir: Path, exclude: list[str], exclude_patterns: list[str]) -> tuple[list[str], int]:
    """Extract tar to extract_dir, removing excluded files. Returns (kept_paths, removed_count)."""
    kept: list[str] = []
    removed = 0
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r|") as tf:
        for member in tf:
            rel = member.name.replace("\\", "/")
            if member.isfile() and _should_exclude(rel, exclude, exclude_patterns):
                removed += 1
                continue
            tf.extract(member, path=extract_dir, filter="data")
            if member.isfile() and not member.name.startswith("."):
                kept.append(rel)
    # Remove empty dirs bottom-up
    for root, dirs, files in os.walk(extract_dir, topdown=False):
        if root == str(extract_dir):
            continue
        rel = os.path.relpath(root, extract_dir).replace("\\", "/")
        if _should_exclude(rel + "/", exclude, exclude_patterns):
            shutil.rmtree(root)
            removed += 1
            continue
        if not os.listdir(root):
            os.rmdir(root)
    return kept, removed


def publish(dry_run: bool = False) -> dict:
    if not CONFIG_PATH.exists():
        return {"status": "error", "message": f"Config not found at {CONFIG_PATH}"}

    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    public_path = Path(config["public_local_path"])
    public_remote = config["public_remote_url"]
    exclude = config.get("exclude", [])
    exclude_patterns = config.get("exclude_patterns", [])
    log: list[str] = []

    log.append(f"Dev repo: {REPO_ROOT}")
    log.append(f"Public repo: {public_path}")
    log.append(f"Remote: {public_remote}")

    if dry_run:
        log.append("DRY RUN -- no files copied or pushed")

    # -- Snapshot committed HEAD via git archive --
    try:
        archive_bytes = _run_binary(["git", "archive", "--format=tar", "HEAD"], cwd=REPO_ROOT)
    except subprocess.CalledProcessError as e:
        return {"status": "error", "message": f"git archive HEAD failed: {e.stderr.decode() if isinstance(e.stderr, bytes) else str(e.stderr)}"}

    if dry_run:
        total = 0
        excluded = 0
        kept = 0
        with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r|") as tf:
            for member in tf:
                if member.isfile():
                    total += 1
                    rel = member.name.replace("\\", "/")
                    if _should_exclude(rel, exclude, exclude_patterns):
                        excluded += 1
                    else:
                        kept += 1
        log.append(f"Tracked files: {total}, kept: {kept}, excluded: {excluded}")
        return {"status": "ok", "message": "\n".join(log)}

    with tempfile.TemporaryDirectory(prefix="publish_public_") as tmpdir_str:
        tmpdir = Path(tmpdir_str)

        kept_files, removed_count = _extract_filtered(archive_bytes, tmpdir, exclude, exclude_patterns)
        log.append(f"Extracted HEAD: {len(kept_files)} kept, {removed_count} excluded")

        # -- Sync into public path --
        if public_path.exists():
            git_dir = public_path / ".git"
            for item in public_path.iterdir():
                if item.name != ".git":
                    if item.is_dir():
                        shutil.rmtree(item)
                    else:
                        item.unlink()
            for item in tmpdir.iterdir():
                dest = public_path / item.name
                (shutil.copytree if item.is_dir() else shutil.copy2)(item, dest, symlinks=True)
        else:
            public_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(tmpdir, public_path, symlinks=True)

        log.append(f"Synced to {public_path}")

    # -- Git init/commit/push --
    try:
        git_dir = public_path / ".git"
        if not git_dir.exists():
            _run_text(["git", "init"], cwd=public_path)
            _run_text(["git", "checkout", "-b", "main"], cwd=public_path)
            _run_text(["git", "remote", "add", "origin", public_remote], cwd=public_path)
            log.append(f"Initialized new repo -> {public_remote}")
        else:
            result = _run_text(["git", "remote", "get-url", "origin"], cwd=public_path, check=False)
            existing = result.stdout.strip()
            if existing and existing != public_remote:
                _run_text(["git", "remote", "set-url", "origin", public_remote], cwd=public_path)
                log.append(f"Updated remote origin -> {public_remote}")

        _run_text(["git", "add", "-A"], cwd=public_path)
        status = _run_text(["git", "status", "--porcelain"], cwd=public_path)
        if status.stdout.strip():
            _run_text(["git", "commit", "-m", f"sync: publish {len(kept_files)} files from dev"], cwd=public_path)
            log.append("Committed to public repo")
            _run_text(["git", "push", "origin", "main"], cwd=public_path)
            log.append(f"Pushed to {public_remote}")
        else:
            log.append("No changes -- nothing new to commit")

    except subprocess.CalledProcessError as e:
        err = e.stderr if isinstance(e.stderr, str) else e.stderr.decode()
        return {"status": "error", "message": f"Git failed:\n{err}\n\nLog:\n" + "\n".join(log)}

    return {"status": "ok", "message": "\n".join(log)}


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    result = publish(dry_run=dry)
    print(result["message"])
    if result["status"] == "error":
        sys.exit(1)
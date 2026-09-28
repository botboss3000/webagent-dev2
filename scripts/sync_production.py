"""Filtered synchronization from the complete dev repo to production.

The default operation is a read-only preview. --apply copies Git-tracked
working-tree files, preserves the production .git directory and excluded
paths, and never stages, commits, pushes, rebuilds, or restarts services.
"""

from __future__ import annotations

import argparse
import filecmp
import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = REPO_ROOT / "dev-sync.json"


class SyncError(RuntimeError):
    """A validation or safety failure that prevents synchronization."""


@dataclass(frozen=True)
class SyncConfig:
    manifest_path: Path
    production_path: Path
    dev_only_roots: tuple[str, ...]
    dev_only_paths: tuple[str, ...]
    state_paths: tuple[str, ...]
    tooling_paths: tuple[str, ...]

    @property
    def excluded_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                set(self.dev_only_paths)
                | set(self.state_paths)
                | set(self.tooling_paths)
            )
        )


@dataclass(frozen=True)
class SyncPlan:
    shipping: tuple[str, ...]
    excluded: tuple[str, ...]
    added: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def total_changes(self) -> int:
        return len(self.added) + len(self.updated) + len(self.removed)


def _run_git(
    repo: Path,
    args: list[str],
    *,
    check: bool = True,
    timeout: int = 60,
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            check=check,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise SyncError("git is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise SyncError(f"git {' '.join(args)} timed out") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or str(exc)).strip()
        raise SyncError(f"git {' '.join(args)} failed: {detail}") from exc


def _normalize_rel(raw: object, field: str) -> str:
    value = str(raw or "").replace("\\", "/").strip()
    if not value:
        raise SyncError(f"{field} contains an empty path")
    if (
        value.startswith("/")
        or Path(value).is_absolute()
        or (len(value) >= 2 and value[1] == ":")
    ):
        raise SyncError(f"{field} must contain repo-relative paths: {raw!r}")
    parts = PurePosixPath(value).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SyncError(f"{field} contains an unsafe path: {raw!r}")
    if parts[0] == ".git":
        raise SyncError(f"{field} cannot target .git")
    return "/".join(parts)


def _normalize_list(data: dict, field: str) -> tuple[str, ...]:
    raw = data.get(field, [])
    if not isinstance(raw, list):
        raise SyncError(f"{field} must be a JSON array")
    return tuple(sorted({_normalize_rel(item, field) for item in raw}))


def _under(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent + "/")


def load_config(
    manifest_path: Path = DEFAULT_MANIFEST,
    *,
    production_override: Path | None = None,
) -> SyncConfig:
    manifest_path = manifest_path.resolve()
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SyncError(f"manifest not found: {manifest_path}") from exc
    except json.JSONDecodeError as exc:
        raise SyncError(f"invalid JSON in {manifest_path}: {exc}") from exc

    if data.get("version") != 1:
        raise SyncError("dev-sync.json version must be 1")

    roots = _normalize_list(data, "dev_only_roots")
    dev_only = _normalize_list(data, "dev_only_paths")
    state = _normalize_list(data, "state_paths")
    tooling = _normalize_list(data, "tooling_paths")
    for path in dev_only:
        if not any(_under(path, root) for root in roots):
            raise SyncError(
                f"dev_only_paths entry {path!r} is outside dev_only_roots"
            )

    if production_override is not None:
        production = production_override.resolve()
    else:
        raw_production = data.get("production_path")
        if not isinstance(raw_production, str) or not raw_production.strip():
            raise SyncError("production_path must be a non-empty string")
        candidate = Path(raw_production)
        if not candidate.is_absolute():
            candidate = manifest_path.parent / candidate
        production = candidate.resolve()

    dev_root = REPO_ROOT.resolve()
    if production == dev_root:
        raise SyncError("production path cannot be the dev repository")
    if dev_root in production.parents:
        raise SyncError("production path cannot be inside the dev repository")

    return SyncConfig(
        manifest_path=manifest_path,
        production_path=production,
        dev_only_roots=roots,
        dev_only_paths=dev_only,
        state_paths=state,
        tooling_paths=tooling,
    )


def _require_git_repo(path: Path, label: str) -> None:
    if not path.is_dir():
        raise SyncError(f"{label} directory does not exist: {path}")
    result = _run_git(path, ["rev-parse", "--is-inside-work-tree"])
    if result.stdout.strip() != "true":
        raise SyncError(f"{label} is not a Git working tree: {path}")


def _tracked_files(repo: Path) -> tuple[str, ...]:
    result = _run_git(repo, ["ls-files", "-z"])
    return tuple(
        sorted(
            segment.replace("\\", "/").strip("/")
            for segment in result.stdout.split("\0")
            if segment.strip()
        )
    )


def _present(repo: Path, rel: str) -> bool:
    path = repo / rel
    return path.exists() or path.is_symlink()


def _is_excluded(path: str, exclusions: tuple[str, ...]) -> bool:
    return any(_under(path, excluded) for excluded in exclusions)


def _same_file(left: Path, right: Path) -> bool:
    if left.is_symlink() != right.is_symlink():
        return False
    if left.is_symlink():
        return os.readlink(left) == os.readlink(right)
    if not right.is_file():
        return False
    return filecmp.cmp(left, right, shallow=False)


def build_plan(source: Path, config: SyncConfig) -> SyncPlan:
    source = source.resolve()
    destination = config.production_path
    _require_git_repo(source, "development source")
    _require_git_repo(destination, "production destination")

    source_tracked = tuple(
        path for path in _tracked_files(source) if _present(source, path)
    )
    destination_tracked = tuple(
        path
        for path in _tracked_files(destination)
        if _present(destination, path)
    )
    exclusions = config.excluded_paths
    excluded = tuple(
        path for path in source_tracked if _is_excluded(path, exclusions)
    )
    shipping = tuple(
        path for path in source_tracked if not _is_excluded(path, exclusions)
    )
    shipping_set = set(shipping)

    added: list[str] = []
    updated: list[str] = []
    for rel in shipping:
        source_file = source / rel
        destination_file = destination / rel
        if not _present(destination, rel):
            added.append(rel)
        elif not _same_file(source_file, destination_file):
            updated.append(rel)

    removed = tuple(
        sorted(
            rel
            for rel in destination_tracked
            if rel not in shipping_set and not _is_excluded(rel, exclusions)
        )
    )
    return SyncPlan(
        shipping=shipping,
        excluded=excluded,
        added=tuple(added),
        updated=tuple(updated),
        removed=removed,
    )


def _working_tree_status(repo: Path) -> str:
    return _run_git(
        repo,
        ["status", "--porcelain", "--untracked-files=normal"],
    ).stdout.strip()


def _copy_one(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        if destination.is_dir() and not destination.is_symlink():
            shutil.rmtree(destination)
        else:
            destination.unlink()
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    else:
        shutil.copy2(source, destination)


def _remove_empty_parents(path: Path, stop: Path) -> None:
    current = path
    while current != stop and stop in current.parents:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def apply_plan(
    source: Path,
    config: SyncConfig,
    plan: SyncPlan,
    *,
    allow_dirty_source: bool = False,
) -> None:
    source = source.resolve()
    destination = config.production_path
    source_status = _working_tree_status(source)
    if source_status and not allow_dirty_source:
        raise SyncError(
            "development source is dirty; commit or stash changes, or pass "
            "--allow-dirty-source explicitly"
        )
    destination_status = _working_tree_status(destination)
    if destination_status:
        raise SyncError(
            "production destination is dirty; synchronization refused:\n"
            + destination_status
        )

    for rel in (*plan.added, *plan.updated):
        _copy_one(source / rel, destination / rel)
    for rel in plan.removed:
        target = destination / rel
        if target.exists() or target.is_symlink():
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            else:
                target.unlink()
            _remove_empty_parents(target.parent, destination)

    remaining = build_plan(source, config)
    if remaining.total_changes:
        raise SyncError(
            "post-sync verification failed: "
            f"{len(remaining.added)} added, "
            f"{len(remaining.updated)} updated, "
            f"{len(remaining.removed)} removed"
        )


def _print_paths(label: str, paths: tuple[str, ...], verbose: bool) -> None:
    print(f"{label}: {len(paths)}")
    if verbose:
        for path in paths:
            print(f"  {path}")


def print_plan(plan: SyncPlan, destination: Path, verbose: bool) -> None:
    print(f"Production destination: {destination}")
    print(f"Tracked files selected for production: {len(plan.shipping)}")
    _print_paths("Excluded tracked files", plan.excluded, verbose)
    _print_paths("Add", plan.added, verbose)
    _print_paths("Update", plan.updated, verbose)
    _print_paths("Remove", plan.removed, verbose)
    print(f"Total production changes: {plan.total_changes}")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or apply a Git-tracked, filtered synchronization from "
            "the complete development repo to production."
        )
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=DEFAULT_MANIFEST,
        help="sync manifest (default: dev-sync.json in the repository root)",
    )
    parser.add_argument(
        "--production",
        type=Path,
        help="override the production destination (useful for verification)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="copy and delete files according to the previewed plan",
    )
    parser.add_argument(
        "--allow-dirty-source",
        action="store_true",
        help="allow an explicit release from uncommitted development files",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="list every excluded and changed path",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config(
            args.manifest,
            production_override=args.production,
        )
        plan = build_plan(REPO_ROOT, config)
        print_plan(plan, config.production_path, args.verbose)
        if not args.apply:
            print("Preview only. Re-run with --apply to synchronize.")
            return 0
        if not plan.total_changes:
            print("Production already matches the selected development files.")
            return 0
        apply_plan(
            REPO_ROOT,
            config,
            plan,
            allow_dirty_source=args.allow_dirty_source,
        )
        print("Synchronization applied and verified.")
        print("Review and test production before committing, pushing, or restarting.")
        return 0
    except SyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

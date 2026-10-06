#!/usr/bin/env python3
"""Prune disposable development build artifacts without touching app state.

Run as the development account, which has access to the v3 job directory and
the Docker socket. Failed or uncertain jobs keep their rollback material.
"""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time


ROOT = Path("/home/ar/webagent-dev2")
JOBS = ROOT / "v3/build-jobs"
JOB_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z")
TEMP_DIR = re.compile(
    r"(?:v3|webagent-v3|webagent-context)-[A-Za-z0-9_-]*"
    r"(?:target|verify|verification|phase|cleanup)\Z"
)
ARCHIVE_AGE = 0
ORPHAN_CHECK_AGE = 2 * 60 * 60
TEMP_AGE = 2 * 60 * 60


def command(*args, timeout=30):
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout).stdout


def current_release():
    # Only a completed job whose image IDs match both healthy live containers
    # can supersede development rollback artifacts.
    images = {}
    for role, name in (("api", "webagent-dev-v3"), ("loop", "webagent-dev-v3-loop")):
        health, image = command(
            "docker", "inspect", "-f", "{{.State.Health.Status}} {{.Image}}", name
        ).strip().split()
        if health != "healthy":
            return None
        images[role] = image
    matching = []
    for receipt in JOBS.glob("*.json"):
        try:
            job = json.loads(receipt.read_text())
        except (OSError, ValueError):
            continue
        if (job.get("status") == "completed" and job.get("phase") == "finished"
                and job.get("images") == images):
            matching.append(job)
    return max(matching, key=lambda job: job["created_at"], default=None)


def job_receipt(job_id):
    try:
        return json.loads((JOBS / (job_id + ".json")).read_text())
    except (OSError, ValueError):
        return None


def prune_archives(now, dry_run):
    count = 0
    release = current_release()
    for entry in os.scandir(JOBS):
        if not JOB_ID.fullmatch(entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        receipt = JOBS / (entry.name + ".json")
        archive = Path(entry.path) / "rollback-images.tar"
        try:
            job = json.loads(receipt.read_text())
            info = archive.lstat()
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            continue
        completed = job.get("status") == "completed" and job.get("phase") == "finished"
        superseded = (release and job.get("status") in {"uncertain", "interrupted", "failed"}
                      and job.get("created_at", 0) < release["created_at"])
        if not (completed or superseded):
            continue
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or now - info.st_mtime < ARCHIVE_AGE:
            continue
        if not dry_run:
            archive.unlink()
        count += 1
    print(f"{'would remove' if dry_run else 'removed'} {count} completed v3 rollback archives")
    return count


def prune_dev_images(dry_run):
    release = current_release()
    if not release:
        print("development image cleanup deferred until both v3 containers are healthy")
        return
    current_id = release["job_id"]
    removed_containers = removed_images = 0
    rows = command("docker", "ps", "-a", "--format", "{{.Names}}|{{.Status}}").splitlines()
    for row in rows:
        name, status = row.split("|", 1)
        match = re.fullmatch(r"webagent-dev-v3(?:-loop)?-pre-build-([0-9a-f-]{36})", name)
        if not match or not status.startswith("Exited"):
            continue
        job = job_receipt(match[1])
        if not job or job.get("created_at", 0) > release["created_at"]:
            continue
        if not dry_run:
            command("docker", "rm", name)
        removed_containers += 1
    tags = command("docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}").splitlines()
    for tag in tags:
        match = re.fullmatch(r"webagent-dev-v3(?:-loop)?:job-([0-9a-f-]{36})", tag)
        if not match or match[1] == current_id:
            continue
        job = job_receipt(match[1])
        if not job or job.get("created_at", 0) >= release["created_at"]:
            continue
        if job.get("operation") not in {"build_deploy", "deploy"}:
            continue
        if job.get("status") not in {"completed", "failed", "cancelled", "stale", "uncertain", "interrupted"}:
            continue
        if not dry_run:
            try:
                command("docker", "image", "rm", tag)
            except subprocess.CalledProcessError:
                continue
        removed_images += 1
    verb = "would remove" if dry_run else "removed"
    print(f"{verb} {removed_containers} stopped v3 rollback containers and {removed_images} old v3 images")


def host_compiler_running():
    rows = command("ps", "-eo", "comm=").splitlines()
    return any(name.strip() in {"cargo", "rustc", "rustdoc"} for name in rows)


def prune_checks(now, dry_run):
    # The coordinator owns task status. A closed task's check cache is only
    # disposable after its deployment job reaches a terminal state.
    items = json.loads((JOBS / "coordination/items.json").read_text())
    if not isinstance(items, dict) or host_compiler_running():
        print("v3 check cleanup deferred while a compiler is active")
        return 0
    checks = JOBS / "checks"
    count = 0
    for entry in os.scandir(checks):
        if not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", entry.name) or not entry.is_dir(follow_symlinks=False):
            continue
        item = items.get(entry.name)
        if item:
            if item.get("status") == "abandoned":
                eligible = True
            elif item.get("status") == "closed" and item.get("job_id"):
                receipt = JOBS / (item["job_id"] + ".json")
                try:
                    job = json.loads(receipt.read_text())
                except (OSError, ValueError):
                    continue
                eligible = job.get("status") in {"completed", "failed", "cancelled", "stale", "uncertain", "interrupted"}
            else:
                eligible = False
        else:
            # Old ad hoc check directories have no coordinator receipt.
            eligible = now - entry.stat(follow_symlinks=False).st_mtime >= ORPHAN_CHECK_AGE
        if not eligible:
            continue
        if not dry_run:
            if host_compiler_running():
                print("v3 compiler started; remaining check cleanup deferred")
                break
            shutil.rmtree(entry.path)
        count += 1
    print(f"{'would remove' if dry_run else 'removed'} {count} finished v3 check caches")
    return count


def compiler_running():
    rows = command("docker", "top", "webagent-dev-v2", "-eo", "pid,comm,args").splitlines()
    return any(row.split()[1] in {"cargo", "rustc", "rustdoc"} for row in rows[1:] if len(row.split()) > 1)


def prune_v2_temp(now, dry_run):
    if compiler_running():
        print("v2 compiler active; temporary build cleanup deferred")
        return 0
    rows = command(
        "docker", "exec", "webagent-dev-v2", "find", "/tmp", "-mindepth", "1",
        "-maxdepth", "1", "-type", "d", "-printf", "%T@ %f\n",
    ).splitlines()
    count = 0
    for row in rows:
        modified, name = row.split(" ", 1)
        if not TEMP_DIR.fullmatch(name) or now - float(modified) < TEMP_AGE:
            continue
        if not dry_run:
            # Check again before each deletion so an active compilation blocks it.
            if compiler_running():
                print("v2 compiler started; remaining temporary cleanup deferred")
                break
            command("docker", "exec", "-u", "0", "webagent-dev-v2", "rm", "-rf", "--", "/tmp/" + name)
        count += 1
    print(f"{'would remove' if dry_run else 'removed'} {count} aged v2 build directories")
    return count


def prune_v2_cache(dry_run):
    disk = os.statvfs("/")
    used_pct = 100 * (disk.f_blocks - disk.f_bavail) / disk.f_blocks
    if used_pct < 80 or compiler_running():
        return
    cache = "/home/appuser/.cache/cargo-target"
    if dry_run:
        print(f"would remove v2 Cargo cache at {used_pct:.0f}% root usage")
    else:
        command("docker", "exec", "-u", "0", "webagent-dev-v2", "rm", "-rf", "--", cache)
        print(f"removed v2 Cargo cache at {used_pct:.0f}% root usage")


def root_usage():
    disk = os.statvfs("/")
    return 100 * (disk.f_blocks - disk.f_bavail) / disk.f_blocks


def root_used_gib():
    disk = os.statvfs("/")
    return (disk.f_blocks - disk.f_bfree) * disk.f_frsize / (1024 ** 3)


def prune_dev_volumes(now, dry_run):
    if root_used_gib() <= 50:
        return
    count = 0
    for name in command("docker", "volume", "ls", "--format", "{{.Name}}").splitlines():
        if not re.fullmatch(r"codex-[A-Za-z0-9_-]{3,72}-(?:target|registry)", name):
            continue
        details = json.loads(command("docker", "volume", "inspect", name))[0]
        created = datetime.fromisoformat(details["CreatedAt"].replace("Z", "+00:00")).timestamp()
        if now - created < 2 * 60 * 60:
            continue
        attached = command("docker", "ps", "-a", "--filter", "volume=" + name, "--format", "{{.ID}}")
        if attached.strip():
            continue
        if not dry_run:
            # Docker refuses to remove a volume that gained a container user.
            command("docker", "volume", "rm", name)
        count += 1
    print(f"{'would remove' if dry_run else 'removed'} {count} aged unused development volumes")


def prune_docker_cache(now, dry_run):
    if root_used_gib() <= 50:
        return
    stamp = JOBS / ".docker-cache-prune-stamp"
    if stamp.exists() and now - stamp.stat().st_mtime < 10 * 60:
        return
    if dry_run:
        print("would prune unused Docker build cache older than one hour")
        return
    command("docker", "builder", "prune", "--all", "--force",
            "--min-free-space", "48GB", "--reserved-space", "1GB", timeout=60)
    stamp.touch()
    print(f"pruned unused Docker build cache; root using {root_used_gib():.1f} GiB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    now = time.time()
    failures = []
    for name, action in (("v3 archives", lambda: prune_archives(now, args.dry_run)),
                         ("v3 images", lambda: prune_dev_images(args.dry_run)),
                         ("v3 checks", lambda: prune_checks(now, args.dry_run)),
                         ("v2 temporary builds", lambda: prune_v2_temp(now, args.dry_run)),
                         ("v2 Cargo cache", lambda: prune_v2_cache(args.dry_run)),
                         ("development volumes", lambda: prune_dev_volumes(now, args.dry_run)),
                         ("Docker build cache", lambda: prune_docker_cache(now, args.dry_run))):
        try:
            action()
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError, IndexError) as error:
            failures.append(f"{name}: {error}")
    for failure in failures:
        print(failure, file=sys.stderr)
    return bool(failures)


if __name__ == "__main__":
    raise SystemExit(main())

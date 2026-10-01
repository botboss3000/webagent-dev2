"""Isolated v3 tool worker. No application credentials are loaded here."""
import asyncio
import ctypes
import contextlib
import fnmatch
import hashlib
import fcntl
import socket
import struct
import subprocess
import sys
import time
import json
import os
import select
import signal
import stat
import shutil
import tempfile
import uuid
from pathlib import Path

CONTROL = Path("/run/v3/control")
LIMIT = 1024 * 1024
ROOT = Path(os.environ.get("V3_WORKSPACE_ROOT", "/workspaces"))
SOCKET = os.environ.get("V3_TOOL_SOCKET", "/run/v3/worker/tools.sock")
TEMPLATE = Path(os.environ.get("V3_CODE_TEMPLATE", "/opt/v3/source-template"))


def workspace_path(session):
    if str(uuid.UUID(session)) != session:
        raise ValueError("invalid workspace identifier")
    root = ROOT.resolve()
    path = root / session
    path.mkdir(mode=0o770, exist_ok=True)
    if path.is_symlink() or path.resolve().parent != root:
        raise ValueError("invalid workspace")
    return path


def scoped(base, value, create=False):
    path = (base / value).resolve()
    if path != base and base not in path.parents:
        raise ValueError("path escapes workspace")
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    return path


async def terminate(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def command(argv, base, timeout, lock):
    env = {"PATH": "/opt/rust/bin:/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "HOME": str(base), "TMPDIR": str(base), "CARGO_HOME": str(base / ".cargo"),
           "CARGO_BUILD_JOBS": "1", "CARGO_INCREMENTAL": "0",
           "RUSTFLAGS": "-C debuginfo=0"}
    process = await asyncio.create_subprocess_exec(
        "/usr/bin/python3", "-I", str(Path(__file__).resolve()), "--runner", json.dumps(argv),
        cwd=base, env=env, start_new_session=True,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    fcntl.flock(lock, fcntl.LOCK_UN)
    data = bytearray()
    truncated = False

    async def drain():
        nonlocal truncated
        while chunk := await process.stdout.read(65536):
            remaining = LIMIT - len(data)
            data.extend(chunk[:max(remaining, 0)])
            truncated |= len(chunk) > remaining
        await process.wait()

    status = "success"
    try:
        await asyncio.wait_for(drain(), timeout)
        if process.returncode != 0:
            status = "failed"
    except asyncio.TimeoutError:
        status = "timeout"
        await terminate(process)
    except asyncio.CancelledError:
        await terminate(process)
        raise
    finally:
        # Kill descendants even if the immediate shell exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
    return {"status": status, "output": data.decode(errors="replace"),
            "exit_code": process.returncode, "truncated": truncated}


async def execute_locked(request, lock):
    base = workspace_path(request["workspace"])
    args = request.get("arguments", {})
    name = request["name"]
    if name in ("shell", "python"):
        argv = ["/bin/sh", "-c", args["command"]] if name == "shell" else [
            "/usr/bin/python3", "-I", "-c", args["code"]]
        return await command(argv, base, min(max(int(request.get("timeout_seconds", 120)), 1), 600), lock)
    # The lock covers dispatch only; controls never wait for tool completion.
    fcntl.flock(lock, fcntl.LOCK_UN)
    if name == "workspace_seed":
        return await asyncio.to_thread(seed_workspace, base)
    if name == "source_review":
        return await asyncio.to_thread(source_review, base)
    if name == "source_patch":
        return await asyncio.to_thread(source_patch, base, args)
    return await asyncio.to_thread(file_operation, request, base, args, name)


def git_capture(source, arguments, limit=LIMIT, acceptable=(0,)):
    """Run Git without hooks or external diff helpers and bound its output."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
           "HOME": str(source), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_OPTIONAL_LOCKS": "0",
           "GIT_NO_REPLACE_OBJECTS": "1"}
    argv = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
            "-c", "diff.external=", *arguments]
    process = subprocess.Popen(argv, cwd=source, env=env, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT)
    output = bytearray()
    deadline = time.monotonic() + 30
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise ValueError("source review timed out")
            chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                return bytes(output[:limit]), True
        if process.wait(timeout=1) not in acceptable:
            raise ValueError("source review Git command failed")
        return bytes(output), False
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def source_review(base):
    """Show tracked and new source changes without altering the Git index."""
    source = base / "webagent-v3"
    if source.is_symlink() or not source.is_dir() or source.resolve().parent != base.resolve():
        raise ValueError("source snapshot unavailable")
    git_dir = source / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError("source baseline unavailable")
    status, limited = git_capture(
        source, ["status", "--short", "--untracked-files=all",
                 "--ignore-submodules=all"], 64 * 1024)
    status_output = status.decode(errors="replace")
    baseline, limited_baseline = git_capture(source, ["rev-parse", "--verify", "HEAD"], 128)
    baseline = baseline.decode().strip()
    if (limited_baseline or len(baseline) not in (40, 64)
        or any(char not in "0123456789abcdef" for char in baseline)):
        raise ValueError("source baseline unavailable")

    def report(raw_diff, truncated):
        text = raw_diff.decode(errors="replace")
        return {"status": "success", "status_output": status_output,
                "baseline_commit": baseline, "diff": text, "truncated": truncated,
                "diff_sha256": None if truncated else hashlib.sha256(text.encode()).hexdigest(),
                "review_sha256": None if truncated else hashlib.sha256(
                    baseline.encode() + b"\0" + text.encode()).hexdigest()}

    if limited:
        return report(b"", True)
    tracked, limited = git_capture(
        source, ["diff", "--no-ext-diff", "--no-textconv", "--binary",
                 "--ignore-submodules=all", "HEAD", "--", "."])
    diff = bytearray(tracked)
    if limited:
        return report(diff, True)
    untracked, limited = git_capture(
        source, ["ls-files", "--others", "--exclude-standard", "-z"], 64 * 1024)
    if limited:
        return report(diff, True)
    paths = [os.fsdecode(item) for item in untracked.split(b"\0") if item]
    incomplete = len(paths) > 128
    for raw in paths[:128]:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("invalid source path")
        path = source / relative
        try:
            info = path.lstat()
        except FileNotFoundError:
            incomplete = True
            continue
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)) or info.st_size > LIMIT:
            incomplete = True
            continue
        remaining = LIMIT - len(diff)
        if remaining <= 0:
            incomplete = True
            break
        patch, limited = git_capture(
            source, ["diff", "--no-index", "--no-ext-diff", "--no-textconv",
                     "--binary", "--", "/dev/null", raw], remaining, (1,))
        diff.extend(patch)
        if limited:
            incomplete = True
            break
    return report(diff, incomplete)


def source_patch(base, args):
    """Apply a checked text patch only within this session's seeded source."""
    source = base / "webagent-v3"
    if source.is_symlink() or not source.is_dir() or source.resolve().parent != base.resolve():
        raise ValueError("source snapshot unavailable")
    git_dir = source / ".git"
    if git_dir.is_symlink() or not git_dir.is_dir():
        raise ValueError("source baseline unavailable")
    patch = args.get("patch")
    if not isinstance(patch, str) or not patch.strip():
        raise ValueError("patch is required")
    encoded = patch.encode()
    if len(encoded) > LIMIT:
        raise ValueError("patch exceeds limit")
    expected = args.get("expected_review_sha256")
    if not isinstance(expected, str) or len(expected) != 64 or any(
        char not in "0123456789abcdef" for char in expected
    ):
        raise ValueError("invalid expected review digest")
    current = source_review(base)
    if current["truncated"] or current["review_sha256"] != expected:
        raise ValueError("source changed since review")
    for line in patch.splitlines():
        if line.startswith(("GIT binary patch", "Binary files ", "rename from ",
                            "rename to ", "copy from ", "copy to ")):
            raise ValueError("binary, rename, and copy patches are unsupported")
        if line.startswith(("new file mode ", "deleted file mode ",
                            "old mode ", "new mode ")):
            if line.rsplit(" ", 1)[-1] not in ("100644", "100755"):
                raise ValueError("unsupported file mode")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=".v3-patch-", dir=base, delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        numstat, limited = git_capture(
            source, ["apply", "--numstat", "-z", str(temporary)], 64 * 1024)
        if limited or not numstat.endswith(b"\0"):
            raise ValueError("invalid or oversized patch path list")
        records = [record for record in numstat.split(b"\0") if record]
        if not records or len(records) > 64:
            raise ValueError("patch must change 1 to 64 files")
        paths = []
        forbidden = {".git", "target", "data", "workspaces",
                     ".cache", "deployment-secrets"}
        for record in records:
            fields = record.split(b"\t", 2)
            if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
                raise ValueError("unsupported patch path or binary content")
            raw = fields[2].decode("utf-8")
            parts = raw.split("/")
            if (not raw or raw.startswith("/") or "\\" in raw
                or any(not part or part in (".", "..") or part in forbidden for part in parts)
                or any(ord(char) < 32 or ord(char) == 127 for char in raw)
                or raw in paths):
                raise ValueError("patch path escapes permitted source")
            cursor = source
            for index, part in enumerate(parts):
                cursor = cursor / part
                if cursor.is_symlink():
                    raise ValueError("patch path contains symlink")
                if cursor.exists() and index < len(parts) - 1 and not cursor.is_dir():
                    raise ValueError("patch path has non-directory parent")
                if cursor.exists() and index == len(parts) - 1 and not cursor.is_file():
                    raise ValueError("patch path is not a regular file")
            paths.append(raw)
        _, limited = git_capture(
            source, ["apply", "--check", "--whitespace=error", str(temporary)], 64 * 1024)
        if limited:
            raise ValueError("patch check output exceeds limit")
        with (CONTROL / "dispatch.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (CONTROL / "shutoff").read_text().strip() != "0":
                raise ValueError("execution disabled")
            try:
                _, limited = git_capture(
                    source, ["apply", "--whitespace=error", str(temporary)], 64 * 1024)
                if limited:
                    raise ValueError("patch output exceeds limit")
            except Exception:
                return {"status": "uncertain", "output": "Patch outcome requires review",
                        "paths": paths, "patch_sha256": hashlib.sha256(encoded).hexdigest()}
        try:
            after = source_review(base)
        except Exception:
            return {"status": "uncertain", "output": "Patch applied; review unavailable",
                    "paths": paths, "patch_sha256": hashlib.sha256(encoded).hexdigest()}
        return {"status": "success", "output": "patch applied", "paths": paths,
                "patch_sha256": hashlib.sha256(encoded).hexdigest(),
                "diff_sha256": after["diff_sha256"],
                "review_sha256": after["review_sha256"],
                "truncated": after["truncated"]}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def seed_workspace(base):
    """Atomically place the vetted image source snapshot in one session workspace."""
    if not TEMPLATE.is_dir() or TEMPLATE.is_symlink():
        raise ValueError("source template unavailable")
    target = base / "webagent-v3"
    if target.exists() or target.is_symlink():
        raise ValueError("workspace source already initialized")
    digest = hashlib.sha256()
    count = 0
    size = 0
    for entry in sorted(TEMPLATE.rglob("*")):
        if entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
            raise ValueError("invalid source template entry")
        if entry.is_file():
            data = entry.read_bytes()
            count += 1
            size += len(data)
            if count > 5000 or size > 32 * LIMIT:
                raise ValueError("source template exceeds limit")
            digest.update(str(entry.relative_to(TEMPLATE)).encode())
            digest.update(b"\0")
            digest.update(data)
    if count == 0:
        raise ValueError("source template is empty")
    staging = Path(tempfile.mkdtemp(prefix=".v3-seed-", dir=base))
    try:
        shutil.copytree(TEMPLATE, staging, dirs_exist_ok=True)
        env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8",
               "HOME": str(staging), "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null"}
        def git(*argv):
            return subprocess.run(
                ["git", *argv], cwd=staging, env=env, check=True,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30
            ).stdout.decode().strip()
        git("init", "--quiet", "--initial-branch=main")
        git("add", "-A")
        git("-c", "user.name=WebAgent v3", "-c", "user.email=webagent-v3@localhost",
            "commit", "--quiet", "-m", "Imported v3 source snapshot")
        baseline = git("rev-parse", "HEAD")
        os.chmod(staging, 0o770)
        with (CONTROL / "dispatch.lock").open("rb") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            if (CONTROL / "shutoff").read_text().strip() != "0":
                raise ValueError("execution disabled")
            if target.exists() or target.is_symlink():
                raise ValueError("workspace source already initialized")
            os.rename(staging, target)
        return {"status": "success", "output": "source seeded",
                "path": "webagent-v3", "files": count,
                "source_sha256": digest.hexdigest(), "baseline_commit": baseline}
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def file_operation(request, base, args, name):
    raw = args.get("path", ".")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("path escapes workspace")
    # Walk each component with openat/O_NOFOLLOW. A concurrent script cannot
    # swap a parent directory for a symlink between validation and access.
    rootfd = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    basefd = None
    directory = None
    try:
        basefd = os.open(request["workspace"], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=rootfd)
        parts = [p for p in relative.parts if p != "."]
        directory = os.dup(basefd)
        for part in parts[:-1]:
            if name == "file_write":
                try:
                    os.mkdir(part, mode=0o770, dir_fd=directory)
                except FileExistsError:
                    pass
                else:
                    os.fsync(directory)
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nextfd
        leaf = parts[-1] if parts else "."
        scan_limited = False
        if name == "file_edit":
            if leaf == ".":
                raise ValueError("path must name a file")
            old_text, new_text = args["old_text"], args["new_text"]
            if not isinstance(old_text, str) or not old_text:
                raise ValueError("old_text must be nonempty text")
            if not isinstance(new_text, str):
                raise ValueError("new_text must be text")
            expected_hash = args.get("expected_sha256")
            if expected_hash is not None and (
                not isinstance(expected_hash, str) or len(expected_hash) != 64
                or any(c not in "0123456789abcdef" for c in expected_hash)
            ):
                raise ValueError("invalid expected_sha256")
            fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            try:
                original_stat = os.fstat(fd)
                if not stat.S_ISREG(original_stat.st_mode):
                    raise ValueError("only regular files are supported")
                with os.fdopen(fd, "rb", closefd=False) as source:
                    original = source.read(LIMIT + 1)
            finally:
                os.close(fd)
            if len(original) > LIMIT:
                raise ValueError("file content exceeds limit")
            digest = hashlib.sha256(original).hexdigest()
            if expected_hash is not None and digest != expected_hash:
                raise ValueError("file changed since inspection")
            before = old_text.encode()
            if original.count(before) != 1:
                raise ValueError("old_text must match exactly once")
            updated = original.replace(before, new_text.encode(), 1)
            if len(updated) > LIMIT:
                raise ValueError("file content exceeds limit")
            temporary = f".v3-edit-{uuid.uuid4().hex}"
            tempfd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             stat.S_IMODE(original_stat.st_mode), dir_fd=directory)
            try:
                with os.fdopen(tempfd, "wb", closefd=False) as target:
                    target.write(updated)
                    target.flush()
                    os.fsync(target.fileno())
                os.fchmod(tempfd, stat.S_IMODE(original_stat.st_mode))
                current = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode) or (
                    current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size
                ) != (
                    original_stat.st_dev, original_stat.st_ino,
                    original_stat.st_mtime_ns, original_stat.st_size
                ):
                    raise ValueError("file changed since inspection")
                os.replace(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
                os.fsync(directory)
            finally:
                os.close(tempfd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
            return {"status": "success", "output": "edited",
                    "previous_sha256": digest, "sha256": hashlib.sha256(updated).hexdigest()}
        if name == "file_write":
            if leaf == ".":
                raise ValueError("path must name a file")
            content = args["content"].encode()
            if len(content) > LIMIT:
                raise ValueError("file content exceeds limit")
            try:
                previous = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                previous = None
            if previous is not None and not stat.S_ISREG(previous.st_mode):
                raise ValueError("only regular files are supported")
            mode = stat.S_IMODE(previous.st_mode) if previous else 0o660
            temporary = f".v3-write-{uuid.uuid4().hex}"
            tempfd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=directory)
            try:
                with os.fdopen(tempfd, "wb", closefd=False) as target:
                    target.write(content)
                    target.flush()
                    os.fchmod(tempfd, mode)
                    os.fsync(target.fileno())
                if previous is None:
                    # linkat publishes atomically and refuses a concurrently created path.
                    os.link(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory,
                            follow_symlinks=False)
                    os.unlink(temporary, dir_fd=directory)
                else:
                    current = os.stat(leaf, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(current.st_mode) or (
                        current.st_dev, current.st_ino, current.st_mtime_ns, current.st_size
                    ) != (
                        previous.st_dev, previous.st_ino, previous.st_mtime_ns, previous.st_size
                    ):
                        raise ValueError("file changed during write")
                    os.replace(temporary, leaf, src_dir_fd=directory, dst_dir_fd=directory)
                try:
                    os.fsync(directory)
                except OSError:
                    return {"status": "uncertain",
                            "output": "File published; durability confirmation failed"}
            finally:
                os.close(tempfd)
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=directory)
            output = "written"
        elif name == "file_read":
            offset = args.get("offset", 1)
            limit = args.get("limit", 2000)
            if type(offset) is not int or offset < 1 or offset > 10_000_000:
                raise ValueError("offset must be a positive line number")
            if type(limit) is not int or not 1 <= limit <= 2000:
                raise ValueError("limit must be between 1 and 2000 lines")
            fd = os.open(leaf, os.O_NOFOLLOW | os.O_NONBLOCK | os.O_RDONLY, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise ValueError("only regular files are supported")
            output, output_size, total, next_offset, truncated = [], 0, 0, None, False
            size = 0
            with os.fdopen(fd, "rb") as file:
                if os.fstat(file.fileno()).st_size > 32 * LIMIT:
                    raise ValueError("file exceeds inspection limit")
                while True:
                    line = file.readline(LIMIT + 1)
                    if not line:
                        break
                    size += len(line)
                    if size > 32 * LIMIT or len(line) > LIMIT:
                        raise ValueError("file exceeds inspection limit")
                    total += 1
                    if total < offset or next_offset is not None:
                        continue
                    if total >= offset + limit or output_size + len(line) > LIMIT:
                        next_offset = total
                        truncated = True
                        continue
                    output.append(line)
                    output_size += len(line)
            return {"status": "success",
                    "output": b"".join(output).decode(errors="replace"),
                    "offset": offset, "limit": limit, "total_lines": total,
                    "next_offset": next_offset, "truncated": truncated}
        elif name in ("file_list", "file_search"):
            if name == "file_list":
                fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=directory)
                try:
                    names = []
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if len(names) >= 10000:
                                scan_limited = True
                                break
                            names.append(entry.name)
                    output = "\n".join(sorted(names))
                finally:
                    os.close(fd)
            else:
                query = args.get("query", "")
                regex = args.get("regex", False)
                comments_only = args.get("comments_only", False)
                file_glob = args.get("file_glob", "")
                if (not isinstance(query, str) or len(query) > 200
                    or type(regex) is not bool or type(comments_only) is not bool
                    or not isinstance(file_glob, str) or len(file_glob) > 200):
                    raise ValueError("invalid search options")
                if not query and not comments_only:
                    raise ValueError("query must not be empty")
                if any(char in file_glob for char in ("\\", "\x00")):
                    raise ValueError("invalid file glob")
                skip = {".git", "target", ".cache", "__pycache__", ".venv",
                        "node_modules", "venv", ".pytest_cache", ".mypy_cache"}
                if any(part in skip for part in parts):
                    raise ValueError("search path is excluded")
                fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=directory)
                found, size = [], 0
                remaining = [10000, 32 * LIMIT]
                deadline = time.monotonic() + 2
                markers = {
                    ".py": ("#",), ".sh": ("#",), ".toml": ("#",),
                    ".yaml": ("#",), ".yml": ("#",),
                    ".rs": ("//", "/*", "*", "*/"),
                    ".js": ("//", "/*", "*", "*/"),
                    ".ts": ("//", "/*", "*", "*/"),
                    ".css": ("/*", "*", "*/"),
                    ".c": ("//", "/*", "*", "*/"),
                    ".h": ("//", "/*", "*", "*/"),
                }

                def scan_file(open_fd, label):
                    nonlocal size, scan_limited
                    if file_glob and not (fnmatch.fnmatchcase(label, file_glob)
                                          or fnmatch.fnmatchcase(Path(label).name, file_glob)):
                        return
                    if remaining[1] <= 0:
                        scan_limited = True
                        return
                    raw_data = os.read(open_fd, min(LIMIT + 1, remaining[1]))
                    remaining[1] -= len(raw_data)
                    if len(raw_data) > LIMIT:
                        scan_limited = True
                        raw_data = raw_data[:LIMIT]
                    lines = raw_data.decode(errors="replace").splitlines()
                    if regex and query:
                        try:
                            result = subprocess.run(
                                ["rg", "--line-number", "--no-heading", "--color", "never",
                                 "--max-count", "1001", "--max-columns", "1",
                                 "--max-columns-preview", "-e", query, "-"],
                                input=raw_data, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE,
                                timeout=max(0.01, deadline - time.monotonic()),
                                check=False)
                        except subprocess.TimeoutExpired:
                            scan_limited = True
                            return
                        if result.returncode not in (0, 1):
                            raise ValueError("invalid search regex")
                        hit_lines = set()
                        for row in result.stdout.splitlines():
                            number = row.split(b":", 1)[0]
                            if number.isdigit():
                                hit_lines.add(int(number))
                        if len(hit_lines) > 1000:
                            scan_limited = True
                    else:
                        hit_lines = None
                    comment_prefixes = markers.get(Path(label).suffix.lower(), ())
                    for index, line in enumerate(lines, 1):
                        if time.monotonic() >= deadline:
                            scan_limited = True
                            return
                        if comments_only and not line.lstrip().startswith(comment_prefixes):
                            continue
                        if query and (index not in hit_lines if hit_lines is not None
                                      else query not in line):
                            continue
                        result = f"{label}:{index}:{line}"
                        encoded_size = len(result.encode())
                        if size + encoded_size > LIMIT:
                            scan_limited = True
                            return
                        found.append(result)
                        size += encoded_size

                def scan_dir(parent, prefix, depth=0):
                    nonlocal scan_limited
                    if depth > 32:
                        scan_limited = True
                        return
                    names = []
                    with os.scandir(parent) as entries:
                        for entry in entries:
                            if len(names) >= remaining[0]:
                                scan_limited = True
                                break
                            names.append(entry.name)
                    for entry in sorted(names):
                        if entry in skip:
                            continue
                        if (remaining[0] <= 0 or remaining[1] <= 0
                            or time.monotonic() >= deadline or size >= LIMIT):
                            scan_limited = True
                            return
                        remaining[0] -= 1
                        try:
                            child = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                                            dir_fd=parent)
                        except OSError:
                            continue
                        try:
                            mode = os.fstat(child).st_mode
                            label = str(Path(prefix) / entry)
                            if stat.S_ISDIR(mode):
                                scan_dir(child, label, depth + 1)
                            elif stat.S_ISREG(mode):
                                scan_file(child, label)
                        finally:
                            os.close(child)
                try:
                    mode = os.fstat(fd).st_mode
                    if stat.S_ISREG(mode):
                        if not query and not comments_only:
                            raise ValueError("query must not be empty")
                        scan_file(fd, raw)
                    elif stat.S_ISDIR(mode):
                        if not query:
                            raise ValueError("a query is required for directory search")
                        scan_dir(fd, raw if raw != "." else "")
                    else:
                        raise ValueError("search path must be a regular file or directory")
                    output = "\n".join(found)
                finally:
                    os.close(fd)
        else:
            raise ValueError("unknown tool")
        encoded = output.encode()
        return {"status": "success", "output": encoded[:LIMIT].decode(errors="replace"),
                "truncated": len(encoded) > LIMIT or scan_limited}
    finally:
        if directory is not None:
            os.close(directory)
        if basefd is not None:
            os.close(basefd)
        os.close(rootfd)


async def execute(request):
    with (CONTROL / "dispatch.lock").open("rb") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if (CONTROL / "shutoff").read_text().strip() != "0":
            raise ValueError("execution disabled")
        return await execute_locked(request, lock)


async def client(reader, writer):
    peer = writer.get_extra_info("socket").getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    if struct.unpack("3i", peer)[1] != 1100:
        writer.close()
        await writer.wait_closed()
        return
    task = None
    disconnected = None
    request = {}
    try:
        raw = await asyncio.wait_for(reader.readline(), 10)
        request = json.loads(raw)
        task = asyncio.create_task(execute(request))
        disconnected = asyncio.create_task(reader.read(1))
        done, _ = await asyncio.wait([task, disconnected], return_when=asyncio.FIRST_COMPLETED)
        if disconnected in done:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            writer.close()
            await writer.wait_closed()
            return
        response = task.result()
    except Exception as error:
        response = {"status": "failed", "output": str(error)[:4096]}
    finally:
        if disconnected:
            disconnected.cancel()
    try:
        response["id"] = request.get("id")
        writer.write((json.dumps(response) + "\n").encode())
        await writer.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        writer.close()
        await writer.wait_closed()


async def main():
    with contextlib.suppress(FileNotFoundError):
        os.unlink(SOCKET)
    server = await asyncio.start_unix_server(client, SOCKET, limit=2 * LIMIT)
    os.chmod(SOCKET, 0o660)
    async with server:
        await server.serve_forever()


def descendants():
    parents = {}
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            # Read only public process metadata, never environ or session data.
            text = (entry / "stat").read_text()
            fields = text[text.rfind(")") + 2:].split()
            parents[int(entry.name)] = int(fields[1])
        except (OSError, ValueError, IndexError):
            continue
    found = set()
    changed = True
    while changed:
        changed = False
        for pid, parent in parents.items():
            if pid not in found and (parent == os.getpid() or parent in found):
                found.add(pid)
                changed = True
    return found


def clean_descendants():
    deadline = time.monotonic() + 3
    while True:
        children = descendants()
        if not children:
            break
        signum = signal.SIGTERM if time.monotonic() < deadline else signal.SIGKILL
        for pid in children:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signum)
        while True:
            try:
                pid, _ = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    break
            except ChildProcessError:
                break
        time.sleep(0.05)
        if time.monotonic() > deadline + 1:
            break


def runner(argv):
    # Orphaned/double-forked children reparent here, rather than container init.
    if ctypes.CDLL(None).prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("cannot enable child subreaper")

    def stop(_signum, _frame):
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        clean_descendants()
        raise SystemExit(143)

    signal.signal(signal.SIGTERM, stop)
    child = subprocess.Popen(argv, stdin=subprocess.DEVNULL)
    try:
        result = child.wait()
    finally:
        clean_descendants()
    raise SystemExit(result if result >= 0 else 128 - result)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--runner":
        runner(json.loads(sys.argv[2]))
    else:
        asyncio.run(main())

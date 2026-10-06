"""Narrow v3 source promotion broker; runs as the server checkout owner."""
import fcntl
import hashlib
import json
import os
import select
import socket
import stat
import struct
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

ROOT = Path(os.environ.get("V3_LIVE_SOURCE_ROOT", "/live-v3"))
MAX_PATCH = 1024 * 1024
ALLOWED_DIRS = {"src", "plugins", "tests", "ui", "docs", "config"}
ALLOWED_FILES = {
    "Cargo.toml", "Cargo.lock", "build.rs", "Dockerfile", ".gitignore",
    "README.md", "deploy.sh", "launch.py", "worker.py", "sandbox.py", "promote.py",
    "ui-baseline.json",
}


def run_git(root, arguments, limit=64 * 1024):
    """Run Git without hooks, external diffs, or unbounded output."""
    env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    command = ["git", "-c", "core.fsmonitor=false",
               "-c", "core.hooksPath=/dev/null", "-c", "diff.external=",
               *arguments]
    process = subprocess.Popen(
        command, cwd=root, env=env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    output = bytearray()
    deadline = time.monotonic() + 30
    try:
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
                raise ValueError("Git command timed out")
            chunk = os.read(process.stdout.fileno(), min(65536, limit + 1 - len(output)))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > limit:
                raise ValueError("Git command output exceeds limit")
        if process.wait(timeout=1) != 0:
            raise ValueError("Git command failed")
        return bytes(output)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stdout.close()


def validate_path(root, raw):
    parts = raw.split("/")
    if (not raw or raw.startswith("/") or "\\" in raw
        or any(not part or part in (".", "..") for part in parts)
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)):
        raise ValueError("Invalid source path")
    if not (raw in ALLOWED_FILES or
            (parts[0] in ALLOWED_DIRS and len(parts) > 1)):
        raise ValueError("Source path is outside the promotion allowlist")
    cursor = root
    for index, part in enumerate(parts):
        cursor = cursor / part
        if cursor.is_symlink():
            raise ValueError("Source path contains a symlink")
        if cursor.exists():
            mode = cursor.stat().st_mode
            if index < len(parts) - 1 and not stat.S_ISDIR(mode):
                raise ValueError("Source path has a non-directory parent")
            if index == len(parts) - 1 and not stat.S_ISREG(mode):
                raise ValueError("Source path is not a regular file")
    return raw


def inspect_patch(root, patch_file, patch):
    for line in patch.splitlines():
        if line.startswith(("GIT binary patch", "Binary files ", "rename from ",
                            "rename to ", "copy from ", "copy to ")):
            raise ValueError("Unsupported patch operation")
        if line.startswith(("new file mode ", "deleted file mode ",
                            "old mode ", "new mode ")):
            if line.rsplit(" ", 1)[-1] not in ("100644", "100755"):
                raise ValueError("Unsupported file mode")
    numstat = run_git(root, ["apply", "--numstat", "-z", str(patch_file)])
    if not numstat.endswith(b"\0"):
        raise ValueError("Invalid patch path list")
    records = [record for record in numstat.split(b"\0") if record]
    if not records or len(records) > 64:
        raise ValueError("Patch must change 1 to 64 files")
    paths = []
    for record in records:
        fields = record.split(b"\t", 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError("Unsupported patch path or binary content")
        path = fields[2].decode("utf-8")
        if path in paths:
            raise ValueError("Duplicate patch path")
        paths.append(validate_path(root, path))
    run_git(root, ["apply", "--check", "--whitespace=error", str(patch_file)])
    return paths


def apply_patch(root, patch, lock_fd, shutoff_fd):
    """Check a proposed patch, then apply it while holding the shutoff lock."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir() or root.stat().st_uid != 1000:
        return {"status": "denied", "output": "Live v3 source unavailable"}
    if not isinstance(patch, str) or not patch.strip():
        return {"status": "denied", "output": "Patch is required"}
    encoded = patch.encode()
    if len(encoded) > MAX_PATCH:
        return {"status": "denied", "output": "Patch exceeds limit"}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix="v3-promotion-", delete=False
        ) as file:
            temporary = Path(file.name)
            file.write(encoded)
            file.flush()
            os.fsync(file.fileno())
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        try:
            os.lseek(shutoff_fd, 0, os.SEEK_SET)
            if os.read(shutoff_fd, 16).strip() != b"0":
                return {"status": "denied", "output": "Execution disabled"}
            try:
                paths = inspect_patch(root, temporary, patch)
            except (OSError, ValueError, UnicodeError):
                return {"status": "denied", "output": "Patch preflight failed"}
            try:
                run_git(root, ["apply", "--whitespace=error", str(temporary)])
            except Exception:
                return {"status": "uncertain", "output": "Promotion outcome requires review",
                        "paths": paths,
                        "patch_sha256": hashlib.sha256(encoded).hexdigest()}
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        return {"status": "success", "output": "Patch promoted", "paths": paths,
                "patch_sha256": hashlib.sha256(encoded).hexdigest()}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def serve(listener_fd, lock_fd, shutoff_fd):
    listener = socket.socket(fileno=listener_fd)
    while True:
        connection, _ = listener.accept()
        with connection:
            peer = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            if struct.unpack("3i", peer)[1] != 1100:
                continue
            connection.settimeout(120)
            try:
                with connection.makefile("rb") as incoming:
                    raw = incoming.readline(2 * MAX_PATCH + 8192)
                if len(raw) > 2 * MAX_PATCH or not raw.endswith(b"\n"):
                    raise ValueError("Invalid promotion request")
                request = json.loads(raw)
                request_id = str(uuid.UUID(request["id"]))
                patch = request["patch"]
                digest = request["patch_sha256"]
                if (not isinstance(patch, str) or
                    not isinstance(digest, str) or
                    hashlib.sha256(patch.encode()).hexdigest() != digest):
                    response = {"status": "denied", "output": "Patch digest mismatch"}
                else:
                    response = apply_patch(ROOT, patch, lock_fd, shutoff_fd)
                response["id"] = request_id
            except Exception:
                response = {"status": "failed", "output": "Invalid promotion request"}
            connection.sendall((json.dumps(response) + "\n").encode())


if __name__ == "__main__":
    serve(int(os.environ["V3_PROMOTE_LISTENER_FD"]),
          int(os.environ["V3_PROMOTE_LOCK_FD"]),
          int(os.environ["V3_PROMOTE_SHUTOFF_FD"]))

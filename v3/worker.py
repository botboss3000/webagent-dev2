"""Isolated v3 tool worker. No application credentials are loaded here."""
import asyncio
import ctypes
import contextlib
import fcntl
import socket
import struct
import subprocess
import sys
import time
import json
import os
import signal
import stat
import uuid
from pathlib import Path

CONTROL = Path("/run/v3/control")
LIMIT = 1024 * 1024
ROOT = Path(os.environ.get("V3_WORKSPACE_ROOT", "/workspaces"))
SOCKET = os.environ.get("V3_TOOL_SOCKET", "/run/v3/worker/tools.sock")


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
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8",
           "HOME": str(base), "TMPDIR": str(base)}
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
        return await command(argv, base, min(max(int(request.get("timeout_seconds", 120)), 1), 120), lock)
    # The lock covers dispatch only; controls never wait for tool completion.
    fcntl.flock(lock, fcntl.LOCK_UN)
    return await asyncio.to_thread(file_operation, request, base, args, name)


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
                with contextlib.suppress(FileExistsError):
                    os.mkdir(part, mode=0o770, dir_fd=directory)
            nextfd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = nextfd
        leaf = parts[-1] if parts else "."
        scan_limited = False
        if name in ("file_read", "file_write"):
            content = args["content"].encode() if name == "file_write" else None
            if content is not None and len(content) > LIMIT:
                raise ValueError("file content exceeds limit")
            flags = os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_RDONLY if name == "file_read" else os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            fd = os.open(leaf, flags, 0o660, dir_fd=directory)
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                os.close(fd)
                raise ValueError("only regular files are supported")
            with os.fdopen(fd, "rb" if name == "file_read" else "wb") as file:
                if name == "file_read":
                    data = file.read(LIMIT + 1)
                    return {"status": "success", "output": data[:LIMIT].decode(errors="replace"),
                            "truncated": len(data) > LIMIT}
                file.write(content)
            output = "written"
        elif name in ("file_list", "file_search"):
            fd = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            try:
                if name == "file_list":
                    names = []
                    with os.scandir(fd) as entries:
                        for entry in entries:
                            if len(names) >= 10000:
                                scan_limited = True
                                break
                            names.append(entry.name)
                    output = "\n".join(sorted(names))
                else:
                    needle = args["query"]
                    if not needle:
                        raise ValueError("query must not be empty")
                    found, size = [], 0
                    remaining = [10000, 32 * LIMIT]
                    deadline = time.monotonic() + 2

                    def search(parent, prefix, depth=0):
                        nonlocal size, scan_limited
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
                            if size >= LIMIT or remaining[0] <= 0 or remaining[1] <= 0 or time.monotonic() >= deadline:
                                scan_limited = True
                                return
                            remaining[0] -= 1
                            try:
                                child = os.open(entry, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                            except OSError:
                                continue
                            try:
                                mode = os.fstat(child).st_mode
                                label = str(Path(prefix) / entry)
                                if stat.S_ISDIR(mode):
                                    search(child, label, depth + 1)
                                elif stat.S_ISREG(mode):
                                    raw_data = os.read(child, min(LIMIT, remaining[1]))
                                    remaining[1] -= len(raw_data)
                                    data = raw_data.decode(errors="replace")
                                    for index, line in enumerate(data.splitlines(), 1):
                                        if needle in line:
                                            result = f"{label}:{index}:{line}"
                                            found.append(result)
                                            size += len(result.encode())
                                            if size >= LIMIT:
                                                break
                            finally:
                                os.close(child)
                    search(fd, raw if raw != "." else "")
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

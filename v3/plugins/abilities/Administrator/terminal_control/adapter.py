"""Session-scoped interactive terminals, supervised outside the child sandbox."""
import asyncio
import ctypes
import errno
import fcntl
import json
import inspect
import os
from pathlib import Path
import select
import signal
import struct
import sys
import termios
import time
import uuid
import weakref

LIMIT = 262144
IDLE_SECONDS = 900
MAX_TERMINALS = 4
KEYS = {"enter": b"\r", "esc": b"\x1b", "tab": b"\t", "backspace": b"\x7f",
        "up": b"\x1b[A", "down": b"\x1b[B", "right": b"\x1b[C", "left": b"\x1b[D",
        "ctrl+c": b"\x03", "ctrl+d": b"\x04", "ctrl+z": b"\x1a"}
_TERMINALS = {}
_GENERATIONS = {}
PORT_ALLOCATOR = None


def configure(port_allocator):
    global PORT_ALLOCATOR
    PORT_ALLOCATOR = port_allocator


async def preview_ports(base):
    if PORT_ALLOCATOR is None:
        return []
    value = PORT_ALLOCATOR(base)
    value = await value if inspect.isawaitable(value) else value
    if not isinstance(value, (list, tuple)) or len(value) != 4 or len(set(value)) != 4 or any(type(p) is not int or not 20000 <= p <= 29999 or p in (8080, 8081, 8082) for p in value):
        raise ValueError("trusted workspace port allocation unavailable")
    return list(value)
_LOCKS = weakref.WeakValueDictionary()


def _base(base):
    path = Path(base)
    if path.is_symlink():
        raise ValueError("symlink workspace forbidden")
    path = path.resolve(strict=True)
    if not path.is_dir():
        raise ValueError("workspace missing")
    return path


def _integer(value, name, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(name + " out of range")
    return value


class Terminal:
    def __init__(self, base, process, command, ports=()):
        self.base, self.process, self.command = base, process, command
        self.ports = list(ports)
        self.id = str(uuid.uuid4())
        self.output = bytearray()
        self.start = self.end = self.sequence = 0
        self.observed = None
        self.updated = time.monotonic()
        self.lock = asyncio.Lock()
        self.changed = asyncio.Event()
        self.task = asyncio.create_task(self.pump())
        self.expiry = asyncio.create_task(self.expire())

    async def expire(self):
        while True:
            await asyncio.sleep(5)
            if time.monotonic() - self.updated > IDLE_SECONDS:
                await self.close()
                _TERMINALS.pop(self.id, None)
                return

    async def pump(self):
        while True:
            data = await self.process.stdout.read(16384)
            async with self.lock:
                if data:
                    self.output.extend(data)
                    self.end += len(data)
                    if len(self.output) > LIMIT:
                        excess = len(self.output) - LIMIT
                        del self.output[:excess]
                        self.start += excess
                    self.sequence += 1
                else:
                    self.sequence += 1
                self.changed.set()
            if not data:
                await self.process.wait()
                break

    async def snapshot(self, cursor=None):
        async with self.lock:
            if cursor is None:
                cursor = self.start
            _integer(cursor, "cursor", 0, self.end)
            truncated = cursor < self.start
            cursor = max(cursor, self.start)
            output = bytes(self.output[cursor - self.start:]).decode("utf-8", "replace")
            self.observed = self.sequence
            self.updated = time.monotonic()
            return {"terminal_id": self.id, "output": output, "cursor": self.end,
                    "sequence": self.sequence, "truncated": truncated,
                    "running": self.process.returncode is None,
                    "exit_code": self.process.returncode}

    async def close(self):
        if asyncio.current_task() is not self.expiry:
            self.expiry.cancel()
        async def cleanup():
            if self.process.stdin:
                self.process.stdin.close()
            if self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(asyncio.shield(self.process.wait()), 2)
                except asyncio.TimeoutError:
                    try:
                        self.process.kill()
                    except ProcessLookupError:
                        pass
                    await asyncio.wait_for(asyncio.shield(self.process.wait()), 1)
            if self.process.stdin:
                try:
                    await asyncio.wait_for(self.process.stdin.wait_closed(), 1)
                except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                    pass
            try:
                await asyncio.wait_for(asyncio.shield(self.task), 1)
            except asyncio.TimeoutError:
                self.task.cancel()
        task = asyncio.create_task(cleanup())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await asyncio.shield(task)
            raise


async def execute(name, arguments, base):
    base = _base(base)
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    allowed = {"terminal_open": {"command", "cols", "rows"},
               "terminal_read": {"terminal_id", "cursor"},
               "terminal_send": {"terminal_id", "expected_sequence", "text", "keys"},
               "terminal_wait": {"terminal_id", "cursor", "timeout_seconds"},
               "terminal_list": set(), "terminal_close": {"terminal_id"}}
    if name not in allowed or set(arguments) - allowed[name]:
        raise ValueError("unknown tool or argument")
    generation = _GENERATIONS.get(base, 0)
    lock = _LOCKS.setdefault(str(base), asyncio.Lock())
    async with lock:
        if _GENERATIONS.get(base, 0) != generation:
            raise ValueError("terminal authority changed during dispatch")
        for ident, terminal in list(_TERMINALS.items()):
            if terminal.base == base and time.monotonic() - terminal.updated > IDLE_SECONDS:
                await terminal.close()
                del _TERMINALS[ident]
        if name == "terminal_open":
            if sum(t.base == base for t in _TERMINALS.values()) >= MAX_TERMINALS:
                raise ValueError("workspace terminal limit reached")
            command = arguments.get("command")
            if not isinstance(command, str) or not command.strip() or len(command.encode()) > 8192 or "\0" in command:
                raise ValueError("command must be bounded nonempty text")
            cols = _integer(arguments.get("cols", 100), "cols", 20, 300)
            rows = _integer(arguments.get("rows", 30), "rows", 5, 100)
            ports = await preview_ports(base)
            env = {"PATH": "/opt/rust/bin:/usr/local/bin:/usr/bin:/bin", "HOME": str(base),
                   "TMPDIR": str(base), "TERM": "xterm-256color", "LANG": "C.UTF-8",
                   "CARGO_HOME": str(base / ".cargo"), "CARGO_BUILD_JOBS": "1",
                   "V3_ALLOWED_LISTEN_PORTS": ",".join(str(port) for port in ports),
                   "V3_UNRESTRICTED_DEV_SHELL": "1"}
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(
                sys.executable, "-I", str(Path(__file__).resolve()), "--supervisor",
                json.dumps({"base": str(base), "command": command, "cols": cols, "rows": rows, "worker_pid": os.getpid()}),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=Path("/live-checkout") if Path("/live-checkout").is_dir() else base,
                env=env, start_new_session=True))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                process = await asyncio.shield(spawn)
                await Terminal(base, process, command).close()
                raise
            terminal = Terminal(base, process, command, ports)
            if _GENERATIONS.get(base, 0) != generation:
                await terminal.close()
                raise ValueError("terminal authority changed during launch")
            _TERMINALS[terminal.id] = terminal
            return {"terminal_id": terminal.id, "sequence": 0, "cursor": 0,
                    "running": True, "requires_read": True, "preview_ports": ports}
        if name == "terminal_list":
            return {"terminals": [{"terminal_id": t.id, "command": t.command,
                    "running": t.process.returncode is None, "sequence": t.sequence,
                    "cursor": t.end, "preview_ports": t.ports} for t in _TERMINALS.values() if t.base == base]}
        terminal = _TERMINALS.get(arguments.get("terminal_id"))
        if terminal is None or terminal.base != base:
            raise ValueError("terminal missing in this workspace; restart never replays input")
        terminal.updated = time.monotonic()
        if name == "terminal_close":
            await terminal.close()
            del _TERMINALS[terminal.id]
            return {"terminal_id": terminal.id, "closed": True, "exit_code": terminal.process.returncode}
        if name == "terminal_wait":
            timeout = _integer(arguments.get("timeout_seconds", 5), "timeout_seconds", 1, 30)
            cursor = arguments.get("cursor", terminal.end)
            _integer(cursor, "cursor", 0, terminal.end)
            terminal.changed.clear()
            if cursor == terminal.end and terminal.process.returncode is None:
                try:
                    await asyncio.wait_for(terminal.changed.wait(), timeout)
                except asyncio.TimeoutError:
                    pass
            return await terminal.snapshot(cursor)
        if name == "terminal_read":
            await asyncio.sleep(0)
            return await terminal.snapshot(arguments.get("cursor"))
        text, keys = arguments.get("text"), arguments.get("keys")
        # Schema optional fields may be emitted as empty defaults. Only the
        # absent alternative is normalized; two meaningful payloads still fail.
        if isinstance(text, str) and text and keys == []:
            keys = None
        elif isinstance(keys, list) and keys and text == "":
            text = None
        if (text is None) == (keys is None):
            raise ValueError("provide exactly one of text or keys")
        if text is not None:
            if not isinstance(text, str) or len(text.encode()) > 8192 or "\0" in text:
                raise ValueError("text exceeds input bound")
            payload = text.encode()
        else:
            if not isinstance(keys, list) or not 1 <= len(keys) <= 32 or any(not isinstance(k, str) or k not in KEYS for k in keys):
                raise ValueError("unsupported terminal key")
            payload = b"".join(KEYS[k] for k in keys)
        expected = _integer(arguments.get("expected_sequence"), "expected_sequence", 0, 2**63 - 1)
        await asyncio.sleep(0)
        async with terminal.lock:
            if expected != terminal.sequence or terminal.observed != expected:
                raise ValueError("stale terminal observation; read before every send")
            if terminal.process.returncode is not None:
                raise ValueError("terminal has exited")
            terminal.observed = None
            terminal.process.stdin.write(payload)
            try:
                await terminal.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise ValueError("terminal closed; input outcome uncertain, do not replay")
            return {"terminal_id": terminal.id, "sent_bytes": len(payload),
                    "sequence": terminal.sequence, "requires_read": True}


async def close_all(base):
    """Trusted worker revocation/shutdown hook; never exposed as a model tool."""
    base = _base(base)
    _GENERATIONS[base] = _GENERATIONS.get(base, 0) + 1
    lock = _LOCKS.setdefault(str(base), asyncio.Lock())
    async with lock:
        for ident, terminal in list(_TERMINALS.items()):
            if terminal.base == base:
                await terminal.close()
                _TERMINALS.pop(ident, None)


def _write_all(fd, data):
    view = memoryview(data)
    while view:
        count = os.write(fd, view)
        if count <= 0:
            raise BrokenPipeError("terminal pipe closed")
        view = view[count:]


def _supervise(spec):
    # Each supervisor is a separate subreaper, so adopted children never cross
    # terminal or workspace ownership. It stays outside the child sandbox.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise RuntimeError("terminal subreaper unavailable")
    stopping = False
    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0:
        raise RuntimeError("terminal parent death guard unavailable")
    worker_pid = spec.get("worker_pid")
    if worker_pid is not None and os.getppid() != worker_pid:
        stopping = True
    master, slave = os.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", spec["rows"], spec["cols"], 0, 0))
    child = os.fork()
    if child == 0:
        os.close(master)
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        for fd in (0, 1, 2):
            os.dup2(slave, fd)
        if slave > 2:
            os.close(slave)
        try:
            import runpy
            sandbox = Path(__file__).resolve().parents[4] / "sandbox.py"
            if os.environ.get("V3_UNRESTRICTED_DEV_SHELL") != "1":
                runpy.run_path(str(sandbox))["restrict"](spec["base"])
            os.execve("/bin/bash", ["bash", "--noprofile", "--norc", "-c", spec["command"]], dict(os.environ))
        except BaseException:
            os.write(2, b"terminal sandbox or launch failed\r\n")
            os._exit(126)
    os.close(slave)
    original_child = child
    os.set_blocking(master, False)
    os.set_blocking(1, False)
    started = time.monotonic()
    exit_status = 0
    try:
        pending_input = bytearray()
        pending_output = bytearray()
        eof = False
        while not stopping and time.monotonic() - started < 3600:
            reads = ([] if eof else [master]) if len(pending_output) < 65536 else []
            if len(pending_input) < 65536 and child:
                reads.append(0)
            writes = ([master] if pending_input and child else []) + ([1] if pending_output else [])
            ready, writable, _ = select.select(reads, writes, [], 0.1)
            if 1 in writable:
                try:
                    count = os.write(1, pending_output)
                    del pending_output[:count]
                except BlockingIOError:
                    pass
            if master in writable:
                try:
                    count = os.write(master, pending_input)
                    del pending_input[:count]
                except BlockingIOError:
                    pass
                except OSError:
                    pending_input.clear()
            if master in ready:
                try:
                    data = os.read(master, 16384)
                except BlockingIOError:
                    data = None
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    data = b""
                if data is not None:
                    if not data:
                        eof = True
                    else:
                        pending_output.extend(data)
            if 0 in ready:
                data = os.read(0, 8192)
                if not data:
                    break
                pending_input.extend(data)
            if child:
                pid, status = os.waitpid(child, os.WNOHANG)
                if pid:
                    exit_status = os.waitstatus_to_exitcode(status)
                    child = 0
            if not pending_output and (eof or not child and master not in ready):
                break
    finally:
        try:
            if child:
                os.killpg(child, signal.SIGKILL)
        except ProcessLookupError:
            pass
        # Repeat: killing children reparents double-fork descendants to us.
        for _ in range(100):
            children = []
            for entry in Path("/proc").iterdir():
                if entry.name.isdigit():
                    try:
                        stat = (entry / "stat").read_text()
                        if int(stat[stat.rfind(")") + 2:].split()[1]) == os.getpid():
                            children.append(int(entry.name))
                    except (OSError, ValueError, IndexError):
                        pass
            for pid in children:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            while True:
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG)
                    if pid == original_child:
                        exit_status = os.waitstatus_to_exitcode(status)
                    if not pid:
                        break
                except ChildProcessError:
                    break
            if not children:
                break
            time.sleep(0.01)
        os.close(master)
    return max(0, min(exit_status, 255))


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--supervisor":
        sys.exit(_supervise(json.loads(sys.argv[2])))

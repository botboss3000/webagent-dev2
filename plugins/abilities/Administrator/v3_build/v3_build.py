"""Scoped v3 build tools; discovered as an Administrator drop-in ability."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import tarfile
import tempfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[4]
TASKS = {"check": ["check"], "test": ["test"], "release": ["build", "--release"]}
CONTEXT = (
    "v3/Dockerfile", "v3/Cargo.toml", "v3/Cargo.lock", "v3/.gitignore", "v3/src", "v3/ui",
    "v3/plugins/abilities", "v3/worker.py", "v3/launch.py", "v3/promote.py", "v3/ui-baseline.json",
    "v3/tests", "v3/README.md", "v3/deploy.sh", "ui/shared/css/app1.css", "ui/shared/css/app2.css",
    "ui/shared/css/app3.css", "ui/shared/css/design-system.css", "ui/shared/css/index.css",
    "ui/main-panel/agents/agents.html", "ui/main-panel/agents/agents.css", "ui/vendor/fonts",
    "ui/vendor/lucide", "ui/favicon.svg",
)
TOOL_SCHEMAS = {
    "v3_build_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "v3_cargo_task": {"type": "object", "properties": {
        "task": {"type": "string", "enum": list(TASKS)},
        "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 1800, "default": 600}},
        "required": ["task"], "additionalProperties": False},
    "v3_build_image": {"type": "object", "properties": {
        "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 1800, "default": 1200}},
        "additionalProperties": False},
}
DESTRUCTIVE = {"v3_cargo_task", "v3_build_image"}
_BUSY = False


def _env():
    # Do not pass provider/API credentials to build processes.
    keys = ("PATH", "HOME", "USER", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "COMSPEC",
            "CARGO_HOME", "RUSTUP_HOME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
            "http_proxy", "https_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR",
            "DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH")
    return {k: os.environ[k] for k in keys if k in os.environ}


async def _run(argv, *, timeout=15, env=None, stdin=None):
    # Stream to a temporary file rather than holding an unbounded compiler log in memory.
    with tempfile.TemporaryFile() as log:
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(ROOT), env=env or _env(), stdin=stdin or asyncio.subprocess.DEVNULL,
                stdout=log, stderr=asyncio.subprocess.STDOUT, start_new_session=(os.name == "posix"))
        except OSError as exc:
            return {"exit_code": None, "timed_out": False, "error": str(exc)}
        timed_out = False
        try:
            await asyncio.wait_for(proc.wait(), timeout)
        except (asyncio.TimeoutError, asyncio.CancelledError) as exc:
            if proc.returncode is None:
                try:
                    if os.name == "posix": os.killpg(proc.pid, signal.SIGKILL)
                    else: proc.kill()
                except ProcessLookupError: pass
            await proc.wait()
            if isinstance(exc, asyncio.CancelledError): raise
            timed_out = True
        size = log.seek(0, 2)
        log.seek(max(0, size - 24000))
        return {"exit_code": proc.returncode, "timed_out": timed_out,
                "output_tail": log.read().decode("utf-8", "replace"), "output_truncated": size > 24000}


async def _probe():
    tools = {}
    for name, args in (("rustc", ["--version"]), ("cargo", ["--version"]),
                       ("cc", ["--version"]), ("docker", ["info", "--format", "{{.ServerVersion}}"] )):
        exe = shutil.which(name)
        tools[name] = {"available": bool(exe)}
        if exe: tools[name].update(await _run([exe, *args]))
    rust = tools["rustc"]
    version_ok = rust.get("exit_code") == 0 and rust.get("output_tail", "").startswith("rustc 1.90.")
    native = version_ok and all(tools[n].get("exit_code") == 0 for n in ("cargo", "cc"))
    return {"observed_at": datetime.now(timezone.utc).isoformat(), "scope": "webagent_server",
            "host": socket.gethostname(), "repository": str(ROOT), "tools": tools,
            "rust_1_90": version_ok, "native_ready": native,
            "image_ready": tools["docker"].get("exit_code") == 0,
            "temporary_free_bytes": shutil.disk_usage(tempfile.gettempdir()).free,
            "dependency_access": "not_probed; requires network access or populated cache"}


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 30 <= value <= 1800:
        raise ValueError("timeout_seconds must be an integer between 30 and 1800")
    return value


def _context_archive(file):
    baseline = json.loads((ROOT / "v3/ui-baseline.json").read_text())
    for name, expected in baseline["assets"].items():
        path = (ROOT / name).resolve()
        if not path.is_relative_to(ROOT) or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Shared UI baseline changed; review v3/ui-baseline.json first")
    def safe_member(member):
        # Never dereference symlinks or include runtime data accidentally.
        if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
            raise ValueError("Image context contains a link or special file: " + member.name)
        if "__pycache__" in Path(member.name).parts or member.name.endswith(".pyc"):
            return None
        return member
    with tarfile.open(fileobj=file, mode="w") as archive:
        for name in CONTEXT:
            path = ROOT / name
            if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
                raise ValueError("Image context escapes repository")
            archive.add(path, arcname=name, filter=safe_member)
    file.seek(0)


def build_tools(*, user_id="", session_id="", agent_id="", agent_template_id="", **ctx):
    async def v3_build_status():
        """Check Rust, Cargo, C compiler, Docker daemon and disk prerequisites for v3."""
        return json.dumps(await _probe())

    async def v3_cargo_task(task, timeout_seconds=600):
        """Run a fixed locked v3 Cargo check, test, or release build; does not deploy."""
        global _BUSY
        try:
            timeout = _timeout(timeout_seconds)
            if task not in TASKS: raise ValueError("Unknown Cargo task")
            if _BUSY: return json.dumps({"status": "busy"})
            _BUSY = True
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})
        try:
            probe = await _probe()
            if not probe["native_ready"]:
                return json.dumps({"status": "blocked", "prerequisites": probe})
            with tempfile.TemporaryDirectory(prefix="webagent-v3-target-") as target:
                env = _env(); env.update(CARGO_TARGET_DIR=target, CARGO_BUILD_JOBS="2")
                result = await _run([shutil.which("cargo"), *TASKS[task], "--locked",
                                    "--manifest-path", str(ROOT / "v3/Cargo.toml"), "-j", "2"],
                                    timeout=timeout, env=env)
            return json.dumps({"status": "ok" if result["exit_code"] == 0 else "failed", "task": task,
                               "artifact_retention": "temporary target removed after verification", **result})
        finally: _BUSY = False

    async def v3_build_image(timeout_seconds=1200):
        """Build the allowlisted webagent-dev-v3:local image only; never start containers."""
        global _BUSY
        try:
            timeout = _timeout(timeout_seconds)
            if _BUSY: return json.dumps({"status": "busy"})
            _BUSY = True
        except ValueError as exc:
            return json.dumps({"status": "error", "error": str(exc)})
        try:
            probe = await _probe()
            if not probe["image_ready"]:
                return json.dumps({"status": "blocked", "prerequisites": probe})
            with tempfile.TemporaryFile() as context:
                try: await asyncio.to_thread(_context_archive, context)
                except (OSError, ValueError, KeyError) as exc:
                    return json.dumps({"status": "error", "error": str(exc)})
                result = await _run([shutil.which("docker"), "build", "-f", "v3/Dockerfile",
                                    "-t", "webagent-dev-v3:local", "-"], timeout=timeout, stdin=context)
            return json.dumps({"status": "ok" if result["exit_code"] == 0 else "failed",
                               "image": "webagent-dev-v3:local", "deployed": False, **result})
        finally: _BUSY = False
    return {"v3_build_status": v3_build_status, "v3_cargo_task": v3_cargo_task, "v3_build_image": v3_build_image}

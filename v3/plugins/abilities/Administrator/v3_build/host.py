"""Fixed development-only build broker. No model-controlled commands or paths.

Install a protected copy, not this writable source, as the host service. Root
creates its listener then drops to ar with the Docker group. A root-owned
snapshot helper supplies only the allowlisted context and two gate JSON files.
"""
import argparse
import asyncio
import concurrent.futures
import hashlib
import fcntl
import http.client
import io
import importlib.util
import inspect
import json
import os
from pathlib import Path
import re
import signal
import shutil
import socket
import stat
import struct
import subprocess
import tarfile
import threading
import time
import uuid

SOURCE = Path("/home/ar/webagent-dev2")
STATE = SOURCE / "v3/build-jobs"
SOCKET = SOURCE / "v3/runtime/build/build.sock"
BRIDGE_SOCKET = SOURCE / "v3/runtime/build/python.sock"
BRIDGE_WORKSPACE = "94c353d5-70bd-44b2-9af0-757c4a979fc9"
CONTEXT = (
    "v3/Dockerfile", "v3/Cargo.toml", "v3/Cargo.lock", "v3/build.rs", "v3/.gitignore",
    "v3/src", "v3/ui", "v3/plugins/abilities", "v3/plugins/gates", "v3/worker.py",
    "v3/sandbox.py", "v3/launch.py", "v3/promote.py", "v3/ui-baseline.json", "v3/tests",
    "v3/README.md", "v3/docs", "v3/deploy.sh", "v3/config",
    "v3/data/config/gates/message_visibility_v1.json",
    "v3/data/config/gates/closing_status_v1.json",
    "ui/shared/css/app1.css", "ui/shared/css/app2.css", "ui/shared/css/app3.css",
    "ui/shared/css/design-system.css", "ui/shared/css/index.css",
    "ui/main-panel/agents/agents.html", "ui/main-panel/agents/agents.css",
    "ui/vendor/fonts", "ui/vendor/lucide", "ui/favicon.svg")
TARGETS = ("webagent-dev-v3", "webagent-dev-v3-loop")
LOCAL_WORKSPACE = "71440aad-a6ba-4ba8-adc8-1fcf66250be6"
WORKER_CONTROL = Path("/var/lib/webagent-v3-build-worker/control.sock")
MAX_LOG = 1024 * 1024
PUBLIC_LOG = 16 * 1024
MAX_CONTEXT = 128 * 1024 * 1024
MIN_BUILD_FREE = 12 * 1024 * 1024 * 1024
MIN_DEPLOY_FREE = 8 * 1024 * 1024 * 1024

_release_file = (SOURCE / "v3/host-build-service/release.py"
                 if str(__file__).startswith("/proc/self/fd/") else Path(__file__).with_name("release.py"))
_release_spec = importlib.util.spec_from_file_location(
    "v3_build_release", _release_file)
release = importlib.util.module_from_spec(_release_spec)
_release_spec.loader.exec_module(release)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def scrub(text, secrets=()):
    for value in sorted(set(secrets), key=len, reverse=True):
        if len(value) >= 4:
            text = text.replace(value, "[redacted]")
    text = re.sub(r"(?i)(authorization\s*[:=]\s*|bearer\s+|cookie\s*[:=]\s*)[^\r\n]+", r"\1[redacted]", text)
    text = re.sub(r"(?i)((?:password|secret|token|api[_-]?key)\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", text)
    return text[-MAX_LOG:]


def context_digest(blob):
    if len(blob) > MAX_CONTEXT:
        raise ValueError("build context exceeds bound")
    entries = {}
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
        for member in archive:
            name = member.name.rstrip("/")
            if name.startswith("./"):
                name = name[2:]
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or not any(name == p or name.startswith(p + "/") for p in CONTEXT):
                raise ValueError("snapshot escaped build allowlist")
            if "__pycache__" in parts or name.endswith((".pyc", ".pyo")):
                raise ValueError("generated Python cache is not a build input")
            if member.isdir():
                continue
            if not member.isfile() or name in entries:
                raise ValueError("snapshot symlink or duplicate forbidden")
            if member.size > 16 * 1024 * 1024:
                raise ValueError("snapshot file exceeds bound")
            body = archive.extractfile(member).read()
            entries[name] = (member.mode & 0o777, len(body), hashlib.sha256(body).hexdigest())
    for required in ("v3/Dockerfile", "v3/Cargo.toml", "v3/Cargo.lock",
                     "v3/data/config/gates/message_visibility_v1.json", "v3/data/config/gates/closing_status_v1.json"):
        if required not in entries:
            raise ValueError("snapshot missing required input")
    if "v3/ui-baseline.json" in entries:
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as archive:
            baseline = json.load(archive.extractfile("v3/ui-baseline.json"))
        for name, expected in baseline["assets"].items():
            if name not in entries or entries[name][2] != expected:
                raise ValueError("shared UI baseline changed")
    return hashlib.sha256(canonical(entries)).hexdigest(), len(entries)


def source_git(root, arguments, patch):
    """Use fixed Git arguments and environment; never execute repository hooks."""
    env = {"PATH": "/usr/bin:/bin", "HOME": "/tmp", "LANG": "C.UTF-8",
           "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null",
           "GIT_OPTIONAL_LOCKS": "0", "GIT_NO_REPLACE_OBJECTS": "1"}
    root = Path(root)
    try:
        prefix = root.relative_to(SOURCE).as_posix()
    except ValueError:
        prefix = ""
    if prefix and arguments[0] == "apply":
        # Git invoked below its parent repository silently skips paths outside
        # the current prefix. Anchor the patch at the checkout root instead.
        arguments = ["apply", "--directory=" + prefix, *arguments[1:]]
        cwd = SOURCE
    else:
        cwd = root
    command = ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
               "-c", "diff.external=", *arguments]
    result = subprocess.run(command, cwd=cwd, env=env, input=patch, capture_output=True, timeout=30)
    if result.returncode or len(result.stdout) > 65536:
        raise ValueError("checked source patch rejected")
    return result.stdout


def source_patch_text_counts(patch):
    """Count all text changes, including lines Git would ignore after a short hunk."""
    added = removed = 0
    in_hunk = False
    for line in patch.splitlines():
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return added, removed


def inspect_source_patch(root, patch):
    """Confine a checked text diff to V3 source; proposals stay separate until here."""
    if not root.is_dir() or root.is_symlink() or root.stat().st_uid != 1000:
        raise ValueError("live V3 checkout unavailable")
    if any(line.startswith(("GIT binary patch", "Binary files ", "rename from ",
                            "rename to ", "copy from ", "copy to "))
           for line in patch.splitlines()):
        raise ValueError("binary or move patch denied")
    for line in patch.splitlines():
        if line.startswith(("new file mode ", "deleted file mode ", "old mode ", "new mode ")):
            if line.rsplit(" ", 1)[-1] not in ("100644", "100755"):
                raise ValueError("unsupported source file mode")
    encoded = patch.encode()
    output = source_git(root, ["apply", "--numstat", "-z", "-"], encoded)
    records = [part for part in output.split(b"\0") if part]
    if not output.endswith(b"\0") or not 1 <= len(records) <= 64:
        raise ValueError("invalid source patch path list")
    counted_added = counted_removed = 0
    paths = []
    allowed_files = {"Cargo.toml", "Cargo.lock", "build.rs", "Dockerfile",
                     ".gitignore", "README.md", "deploy.sh", "launch.py",
                     "worker.py", "sandbox.py", "promote.py", "ui-baseline.json"}
    allowed_dirs = {"src", "plugins", "tests", "ui", "docs", "config"}
    for record in records:
        fields = record.split(b"\t", 2)
        if len(fields) != 3 or not fields[0].isdigit() or not fields[1].isdigit():
            raise ValueError("unsupported source patch")
        counted_added += int(fields[0])
        counted_removed += int(fields[1])
        name = fields[2].decode("utf-8")
        try:
            prefix = root.relative_to(SOURCE).as_posix() + "/"
        except ValueError:
            prefix = ""
        if prefix:
            if not name.startswith(prefix):
                raise ValueError("source patch escaped task workspace")
            name = name[len(prefix):]
        parts = name.split("/")
        if (name in paths or name.startswith("/") or "\\" in name
                or any(ord(char) < 32 or ord(char) == 127 for char in name)
                or any(not part or part in (".", "..") for part in parts)
                or not (name in allowed_files or parts[0] in allowed_dirs and len(parts) > 1)):
            raise ValueError("source patch path denied")
        cursor = root
        for index, part in enumerate(parts):
            cursor = cursor / part
            if cursor.is_symlink() or (cursor.exists() and
                    (not cursor.is_dir() if index < len(parts) - 1 else not cursor.is_file())):
                raise ValueError("source patch path is not a regular source file")
        paths.append(name)
    if (counted_added, counted_removed) != source_patch_text_counts(patch):
        raise ValueError("source patch hunk line counts mismatch")
    source_git(root, ["apply", "--check", "--whitespace=error", "-"], encoded)
    return paths


def replacement_config(current, old_defaults, new_defaults):
    """Preserve explicit overrides while adopting the new image's defaults."""
    result = dict(current)
    for key in ("Entrypoint", "Cmd", "User", "WorkingDir", "Healthcheck", "StopSignal", "Shell", "ExposedPorts", "Volumes"):
        if current.get(key) == old_defaults.get(key):
            if key in new_defaults:
                result[key] = new_defaults[key]
            else:
                result.pop(key, None)
    def env_map(values):
        return dict(value.split("=", 1) for value in (values or []))
    current_env, old_env, new_env = (env_map(value.get("Env")) for value in (current, old_defaults, new_defaults))
    for key, value in current_env.items():
        if old_env.get(key) != value:
            new_env[key] = value
    result["Env"] = [key + "=" + value for key, value in sorted(new_env.items())]
    labels = dict(new_defaults.get("Labels") or {})
    for key, value in (current.get("Labels") or {}).items():
        if (old_defaults.get("Labels") or {}).get(key) != value:
            labels[key] = value
    result["Labels"] = labels
    return result


class WorkerControlHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(str(WORKER_CONTROL))


def worker_control(method, path, body=None):
    conn = WorkerControlHTTP("localhost", timeout=5)
    payload = None if body is None else canonical(body)
    try:
        conn.request(method, path, payload, {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read(4096)
        if response.status == 409:
            raise ValueError("Build worker has an active job or is restarting")
        if response.status not in (200, 202):
            raise RuntimeError("Build worker control unavailable")
        return json.loads(raw)
    finally:
        conn.close()


class DockerHTTP(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect("/var/run/docker.sock")


class Runner:
    def __init__(self, source=SOURCE, snapshot_helper=None, snapshot_socket=None):
        self.source, self.snapshot_helper = Path(source), snapshot_helper
        self.secrets = []
        self.snapshot_socket = snapshot_socket
        self.cancel = threading.Event()

    def command(self, argv, stdin=None, timeout=3600, output_file=None, private_raw=False):
        # Explicit arguments only; the model never supplies argv. Environment
        # has no application tokens, cookies or provider credentials.
        env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/home/ar", "LANG": "C.UTF-8"}
        with subprocess.Popen(argv, cwd=self.source, env=env, stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True) as proc:
            output = bytearray()
            def feed():
                try:
                    if stdin is not None:
                        proc.stdin.write(stdin)
                        proc.stdin.close()
                except (BrokenPipeError, OSError):
                    pass
            writer = threading.Thread(target=feed, daemon=True)
            writer.start()
            deadline = time.monotonic() + timeout
            import select
            try:
                while True:
                    if self.cancel.is_set():
                        raise PermissionError("host broker stopping; reconcile interrupted effects")
                    if time.monotonic() > deadline:
                        raise TimeoutError("fixed build task timeout")
                    if select.select([proc.stdout], [], [], .2)[0]:
                        data = os.read(proc.stdout.fileno(), 65536)
                        if not data:
                            break
                        if output_file is not None:
                            output_file.write(data)
                        else:
                            output.extend(data)
                            if len(output) > MAX_LOG:
                                if private_raw:
                                    raise ValueError("private inspect exceeds bound")
                                del output[:-MAX_LOG]
                    elif proc.poll() is not None:
                        continue
                code = proc.wait(timeout=5)
                writer.join(timeout=5)
                text = scrub(bytes(output).decode("utf-8", "replace"), self.secrets)
                if code:
                    raise RuntimeError("fixed command failed: " + text)
                return bytes(output).decode("utf-8", "strict") if private_raw else text
            finally:
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    proc.wait()

    def snapshot(self):
        # Helper path is service configuration, never a tool argument. It must
        # be root-owned and immutable to the development source identity.
        if self.snapshot_socket:
            conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.settimeout(60)
            try:
                conn.connect(self.snapshot_socket)
                conn.sendall(b"{}\n")
                def receive(count):
                    data = bytearray()
                    while len(data) < count:
                        part = conn.recv(min(count-len(data), 1024*1024))
                        if not part:
                            raise ValueError("protected snapshot helper unavailable")
                        data.extend(part)
                    return bytes(data)
                size = struct.unpack("!Q", receive(8))[0]
                if not 0 < size <= MAX_CONTEXT:
                    raise ValueError("protected snapshot size rejected")
                blob = receive(size)
                context_digest(blob)
                return blob
            finally:
                conn.close()
        if self.snapshot_helper:
            helper = Path(self.snapshot_helper)
            info = helper.stat()
            if info.st_uid != 0 or info.st_mode & 0o022:
                raise ValueError("snapshot helper must be protected root-owned code")
            argv = ["sudo", "-n", str(helper)]
        else:
            argv = ["sudo", "-n", "tar", "--exclude=__pycache__", "--exclude=*.pyc", "--exclude=*.pyo", "-cf", "-", *CONTEXT]
        class BoundedContext(io.BytesIO):
            def write(self, data):
                if self.tell() + len(data) > MAX_CONTEXT:
                    raise ValueError("build context exceeds bound")
                return super().write(data)
        result = BoundedContext()
        self.command(argv, timeout=60, output_file=result)
        blob = result.getvalue()
        context_digest(blob)
        return blob

    def promote_checkout(self, before, after, changed):
        return release.promote_checkout(self.source, before, after, changed, context_digest)

    def inspect(self, name):
        if name not in TARGETS and not re.fullmatch(r"webagent-dev-v3(?:-loop)?-pre-build-[0-9a-f-]{36}", name):
            raise ValueError("container outside development target")
        value = json.loads(self.command(["docker", "inspect", name], timeout=30, private_raw=True))[0]
        self.secrets.extend(item.partition("=")[2] for item in value.get("Config", {}).get("Env", []) if "=" in item)
        return value

    def check_space(self, minimum):
        free = shutil.disk_usage(self.source).free
        if free < minimum:
            raise ValueError(f"development build needs {minimum // (1024 ** 3)} GiB free; {free // (1024 ** 3)} GiB available")
        return free

    def build(self, blob, job_id, role):
        tag = "webagent-dev-v3" + ("-loop" if role == "loop" else "") + ":job-" + job_id
        log = self.command(["docker", "buildx", "build", "--builder", "default", "--load",
                            "--progress=plain", "--target", role, "-f", "v3/Dockerfile",
                            "-t", tag, "-"], stdin=blob)
        image = self.command(["docker", "image", "inspect", "--format", "{{.Id}}", tag], timeout=30).strip()
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
            raise ValueError("invalid image identity")
        return image, log

    def export(self, images, destination):
        if any(not re.fullmatch(r"sha256:[0-9a-f]{64}", image) for image in images):
            raise ValueError("invalid rollback image")
        temporary = destination.with_name(".rollback-" + uuid.uuid4().hex + ".tar")
        try:
            self.command(["docker", "save", "-o", str(temporary), *images])
            os.chmod(temporary, 0o600)
            fd = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError("rollback export is not a regular file")
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, destination)
            parent = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def stop(self, name):
        self.command(["docker", "stop", "--time", "30", name], timeout=60)

    def rename(self, name, target):
        self.command(["docker", "rename", name, target], timeout=30)

    def create(self, name, image, previous, release_root=None):
        # Docker engine JSON preserves the inspected container configuration,
        # including private Env values, without shell escaping or printing it.
        old_defaults = json.loads(self.command(["docker", "image", "inspect", previous["Image"]], timeout=30, private_raw=True))[0]["Config"]
        new_defaults = json.loads(self.command(["docker", "image", "inspect", image], timeout=30, private_raw=True))[0]["Config"]
        config = replacement_config(previous["Config"], old_defaults, new_defaults)
        config["Image"] = image
        host = dict(previous["HostConfig"])
        if release_root is not None:
            host = release.release_mounts(host, Path(release_root))
        # Container-id-derived values must not be reused. Preserve configured
        # mounts, network, limits, ports, restart policy and security options.
        host.pop("ContainerIDFile", None)
        config["HostConfig"] = host
        config.pop("Hostname", None)
        config.pop("Domainname", None)
        config.pop("MacAddress", None)
        conn = DockerHTTP("localhost", timeout=30)
        try:
            conn.request("POST", "/containers/create?name=" + name, body=canonical(config), headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            body = response.read(1024 * 1024)
            if response.status != 201:
                raise RuntimeError("development container create failed (" + str(response.status) + ")")
            return json.loads(body)["Id"]
        finally:
            conn.close()

    def start(self, name):
        self.command(["docker", "start", name], timeout=30)

    def healthy(self, name, expected):
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if self.cancel.is_set():
                raise PermissionError("host broker stopping; deployment outcome requires reconciliation")
            current = self.inspect(name)
            if current["Image"] != expected:
                return False
            state = current["State"]
            if state.get("Status") == "running" and state.get("Health", {}).get("Status") == "healthy":
                return True
            time.sleep(1)
        return False


class Broker:
    def __init__(self, state=STATE, runner=None, recover=True):
        self.state = Path(state)
        if self.state.is_symlink():
            raise ValueError("symlink job state forbidden")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.state, 0o700)
        self.lease = os.open(self.state / "broker.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(self.lease)
            raise ValueError("development build broker already running")
        self.runner = runner or Runner()
        self.lock = threading.RLock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        self.jobs = {}
        self.requests = {}
        self.active = None
        self.stopping = False
        self.work_dir = self.state / "coordination"
        if self.work_dir.is_symlink():
            raise ValueError("symlink coordination state forbidden")
        self.work_dir.mkdir(mode=0o700, exist_ok=True)
        self.work_path = self.work_dir / "items.json"
        if self.work_path.is_symlink():
            raise ValueError("symlink coordination receipt forbidden")
        self.work = json.loads(self.work_path.read_text()) if self.work_path.exists() else {}
        if not isinstance(self.work, dict) or len(self.work) > 1000:
            raise ValueError("invalid coordination registry")
        for path in sorted(self.state.glob("*.json")):
            if path.is_symlink():
                raise ValueError("symlink receipt forbidden")
            job = json.loads(path.read_text())
            if not re.fullmatch(r"[0-9a-f-]{36}", job.get("job_id", "")):
                raise ValueError("invalid durable job")
            self.jobs[job["job_id"]] = job
            self.requests[job["request_key"]] = job["job_id"]
        if recover:
            for job in self.jobs.values():
                if job["status"] in ("queued", "running") and not (
                        job.get("isolated") and job["status"] == "queued"):
                    # A restart never repeats a build or external mutation.
                    job["status"] = "uncertain" if job["operation"] == "deploy" or job.get("effects_started") else "interrupted"
                    job["reason"] = "broker restarted; no automatic replay"
                    if job["status"] == "uncertain" and self._reconcile(job):
                        job["status"] = "completed"
                        job["reason"] = "recovered matching healthy deployment"
                    self._save(job)
        if recover:
            self._start_next()

    def _save(self, job):
        path = self.state / (job["job_id"] + ".json")
        temporary = self.state / (job["job_id"] + ".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(job))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(self.state, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _save_work(self):
        temporary = self.work_dir / "items.tmp"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(canonical(self.work))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.work_path)
        directory = os.open(self.work_dir, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def _open_work(self):
        return [item for item in self.work.values() if item["status"] == "open"]

    def _auto_schedule(self, item, trusted, workspace):
        if item.get("job_id") or self._open_work() or self.active is not None:
            return item.get("job_id")
        pending = sorted(key for key, value in self.work.items() if value["status"] == "closed" and not value.get("job_id"))
        request_id = "auto-" + hashlib.sha256(canonical(pending)).hexdigest()
        job = self.handle({"name": "administrator_auto_build_deploy", "arguments": {
            "request_id": request_id, "expected_inputs_sha256": item["inputs_sha256"]},
            "workspace": workspace, "trusted": trusted})
        for key in pending:
            self.work[key]["job_id"] = job["job_id"]
        self._save_work()
        return job["job_id"]

    def _current_release_blob(self):
        completed = sorted((job for job in self.jobs.values()
                            if job.get("isolated") and job["status"] == "completed"),
                           key=lambda job: job["created_at"])
        if completed:
            job = completed[-1]
            blob = (self.state / job["job_id"] / "candidate.tar").read_bytes()
            if context_digest(blob)[0] != job["inputs_sha256"]:
                raise ValueError("durable release input mismatch")
            return blob
        return self.runner.snapshot()

    def _start_next(self):
        if self.active is not None or self.stopping:
            return
        for job in sorted(self.jobs.values(), key=lambda value: value["created_at"]):
            if not job.get("isolated") or job["status"] != "queued":
                continue
            try:
                self._lease(job)
                folder = self.state / job["job_id"]
                base = (folder / "base.tar").read_bytes()
                task = (folder / "task.tar").read_bytes()
                live = self._current_release_blob()
                candidate, changed = release.candidate(base, task, live, context_digest)
                digest, _ = context_digest(candidate)
                release_root = folder / "release"
                (folder / "candidate.tar").write_bytes(candidate)
                if release_root.exists():
                    if context_digest(release.snapshot(release_root, CONTEXT, context_digest))[0] != digest:
                        raise ValueError("prepared release changed across broker restart")
                else:
                    release.extract(release_root, candidate, context_digest)
                job.update(inputs_sha256=digest, base_inputs_sha256=context_digest(live)[0],
                           changed_paths=changed, release_root=str(release_root),
                           phase="admitted")
                self._save(job)
                self.active = job["job_id"]
                self.pool.submit(self._run, job, candidate)
                return
            except Exception as error:
                job.update(status="failed", phase="admission_failed",
                           reason=scrub(str(error), self.runner.secrets))
                self._save(job)

    def _view(self, job):
        raw = scrub(job.get("log", ""), self.runner.secrets).encode("utf-8")
        tail = raw[-PUBLIC_LOG:].decode("utf-8", "replace")
        # Replacement characters from a split UTF-8 sequence must not expand
        # the public byte limit. Storage can retain a larger bounded log.
        while len(tail.encode("utf-8")) > PUBLIC_LOG:
            tail = tail[1:]
        view = {key: job[key] for key in ("job_id", "operation", "status", "phase", "inputs_sha256", "created_at", "images", "rollback", "tool_name", "access_fingerprint", "isolated") if key in job}
        if "reason" in job:
            view["reason"] = scrub(job["reason"], self.runner.secrets).encode("utf-8")[:4096].decode("utf-8", "ignore")
        return view | {"log": tail, "log_truncated": len(raw) > PUBLIC_LOG, "log_bytes": len(raw)}

    def _lease(self, job):
        if job.get("revoked") or job.get("lease_until", 0) <= time.time():
            raise PermissionError("host job authorization expired or revoked; no automatic replay")

    def _incoming_lease(self, trusted):
        deadline, fingerprint = trusted.get("lease_until"), trusted.get("access_fingerprint")
        max_lease = 4 * 3600 if trusted.get("local_caller") is True or trusted.get("bridge_caller") is True else 35
        if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not time.time() < deadline <= time.time() + max_lease:
            raise ValueError("current bounded host authorization lease required")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("host access fingerprint required")
        return deadline, fingerprint

    def _owned(self, job, trusted, workspace):
        return job["user_id"] == trusted["user_id"] and job["workspace"] == workspace

    def _reconcile(self, job):
        try:
            images = job.get("images", {})
            rollback = job.get("rollback", {})
            if set(images) != {"api", "loop"} or set(rollback) != {"api", "loop"}:
                return False
            archive = self.state / job["job_id"] / "rollback-images.tar"
            if not archive.is_file() or not archive.stat().st_size:
                return False
            for role, name in zip(("api", "loop"), TARGETS):
                previous = self.runner.inspect(rollback[role])
                if previous["State"]["Status"] != "exited" or not self.runner.healthy(name, images[role]):
                    return False
            return True
        except Exception:
            return False

    def handle(self, request):
        if not isinstance(request, dict) or set(request) - {"id", "name", "arguments", "workspace", "timeout_seconds", "trusted"}:
            raise ValueError("invalid broker request")
        trusted, workspace, args = request.get("trusted"), request.get("workspace"), request.get("arguments", {})
        if not isinstance(trusted, dict) or not {"user_id", "session_id"} <= set(trusted) or set(trusted) - {"user_id", "session_id", "lease_until", "access_fingerprint", "local_caller", "bridge_caller"}:
            raise ValueError("trusted caller required")
        if not isinstance(trusted["user_id"], str) or not 1 <= len(trusted["user_id"]) <= 128 or "\0" in trusted["user_id"]:
            raise ValueError("invalid caller")
        try:
            if str(uuid.UUID(workspace)) != workspace or trusted["session_id"] != workspace:
                raise ValueError("workspace/session mismatch")
        except (TypeError, AttributeError):
            raise ValueError("invalid workspace")
        if not isinstance(args, dict):
            raise ValueError("arguments must be an object")
        name = request.get("name")
        with self.lock:
            if name in ("administrator_host_lease", "administrator_host_revoke"):
                if args:
                    raise ValueError("internal lease control takes no arguments")
                incoming = self._incoming_lease(trusted) if name == "administrator_host_lease" else None
                updated = 0
                for job in self.jobs.values():
                    if not self._owned(job, trusted, workspace) or job["status"] not in ("queued", "running"):
                        continue
                    if incoming:
                        deadline, fingerprint = incoming
                        if job.get("revoked"):
                            continue
                        if job.get("access_fingerprint") != fingerprint:
                            job["revoked"] = True
                            self._save(job)
                            updated += 1
                            continue
                        # Expired authorization cannot be revived by renewal.
                        if job.get("lease_until", 0) <= time.time():
                            continue
                        # A queued isolated release or deployment keeps its bounded
                        # commit lease while still accepting live revocation.
                        job["lease_until"] = max(job["lease_until"], deadline) if job.get("effects_started") or job.get("isolated") else deadline
                    else:
                        job["revoked"] = True
                    self._save(job)
                    updated += 1
                return {"updated": updated}
            if name == "v3_build_worker_restart":
                if set(args) != {"request_id"} or not isinstance(args["request_id"], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", args["request_id"]):
                    raise ValueError("stable restart request_id required")
                self._incoming_lease(trusted)
                if self.active is not None:
                    raise ValueError("shared V3 build target busy")
                response = worker_control("POST", "/v1/restart", {"request_id": args["request_id"]})
                previous_pid = response.get("previous_pid")
                if response.get("status") != "restarting" or not isinstance(previous_pid, int):
                    raise RuntimeError("Build worker restart response invalid")
                for _ in range(50):
                    time.sleep(0.5)
                    try:
                        status = worker_control("GET", "/v1/status")
                    except (OSError, ValueError, RuntimeError):
                        continue
                    if status.get("pid") and status["pid"] != previous_pid:
                        return {"status": "ok", "service": "v3-build-worker.service",
                                "active": True, "interrupted_job": None}
                raise RuntimeError("Restart requested but fresh worker readiness was not confirmed; inspect host before retry")
            if name == "v3_source_read":
                if set(args) - {"task_id", "path", "offset", "limit"} or not {"task_id", "path"} <= set(args):
                    raise ValueError("source read requires task_id and path")
                task_id, name_path = args["task_id"], args["path"]
                if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id):
                    raise ValueError("bounded task ID required")
                if not isinstance(name_path, str) or not re.fullmatch(r"[A-Za-z0-9_./-]{1,240}", name_path):
                    raise ValueError("bounded source path required")
                parts = name_path.split("/")
                if any(part in ("", ".", "..") for part in parts) or parts[0] in ("data", "workspaces"):
                    raise ValueError("source read path denied")
                offset, limit = args.get("offset", 1), args.get("limit", 200)
                if type(offset) is not int or offset < 1 or type(limit) is not int or not 1 <= limit <= 200:
                    raise ValueError("bounded source line range required")
                key = hashlib.sha256(canonical([trusted["user_id"], workspace, task_id])).hexdigest()
                item = self.work.get(key)
                if not item or item.get("mode") != "isolated" or item["status"] != "open":
                    raise ValueError("open isolated task required")
                root = self.work_dir / "trees" / key
                source = root / "v3" / name_path
                if source.is_symlink() or not source.is_file() or not source.resolve().is_relative_to((root / "v3").resolve()):
                    raise ValueError("source file unavailable")
                data = source.read_bytes()
                if len(data) > 1024 * 1024 or b"\0" in data:
                    raise ValueError("source read exceeds text bound")
                lines = data.decode("utf-8").splitlines()
                selected = lines[offset - 1:offset - 1 + limit]
                if len(canonical(selected)) > 16000:
                    raise ValueError("source read result exceeds bound; request fewer lines")
                return {"task_id": task_id, "path": name_path, "offset": offset,
                        "lines": selected,
                        "total_lines": len(lines),
                        "workspace_inputs_sha256": context_digest(
                            release.snapshot(root, CONTEXT, context_digest))[0]}
            if name == "v3_source_receipt":
                if set(args) != {"task_id", "request_id"}:
                    raise ValueError("bridge source receipt request denied")
                item = self.work.get(hashlib.sha256(canonical([trusted["user_id"], workspace, args["task_id"]])).hexdigest())
                receipt = item.get("applies", {}).get(args["request_id"]) if item else None
                if not receipt:
                    raise ValueError("source receipt unavailable")
                return dict(receipt)
            if name == "v3_source_apply":
                if set(args) != {"task_id", "request_id", "patch", "expected_inputs_sha256"}:
                    raise ValueError("bridge source apply request denied")
                task_id, request_id = args["task_id"], args["request_id"]
                if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id) or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
                    raise ValueError("bounded task and request IDs required")
                item = self.work.get(hashlib.sha256(canonical([trusted["user_id"], workspace, task_id])).hexdigest())
                if not item or item["status"] != "open":
                    raise ValueError("registered open bridge task required")
                patch = args["patch"]
                if not isinstance(patch, str) or not 0 < len(patch.encode()) <= 1024 * 1024:
                    raise ValueError("bounded text patch required")
                digest = hashlib.sha256(canonical(args)).hexdigest()
                receipt = item.setdefault("applies", {}).get(request_id)
                if receipt:
                    if receipt["request_sha256"] != digest:
                        raise ValueError("request_id reused with changed source apply")
                    return dict(receipt)
                isolated = item.get("mode") == "isolated"
                if self.active is not None and not isolated:
                    raise ValueError("build active; source apply denied")
                tree = self.work_dir / "trees" / item["key"] if isolated else self.runner.source
                current, _ = context_digest(
                    release.snapshot(tree, CONTEXT, context_digest) if isolated else self.runner.snapshot())
                if current != args["expected_inputs_sha256"]:
                    raise ValueError("stale source inputs")
                source = tree / "v3"
                paths = inspect_source_patch(source, patch)
                receipt = {"task_id": task_id, "request_id": request_id, "request_sha256": digest,
                           "patch_sha256": hashlib.sha256(patch.encode()).hexdigest(),
                           "status": "dispatching", "paths": paths}
                item["applies"][request_id] = receipt
                self._save_work()
                try:
                    source_git(source, ["apply", "--whitespace=error", "-"], patch.encode())
                except Exception:
                    receipt.update(status="uncertain", reason="source apply outcome requires review")
                else:
                    receipt.update(status="success", reason="checked patch applied")
                self._save_work()
                return dict(receipt)
            if name == "v3_build_work":
                if set(args) - {"action", "task_id", "summary", "expected_inputs_sha256", "mode"}:
                    raise ValueError("unsupported work argument")
                action = args.get("action")
                if action == "status":
                    if set(args) != {"action"}:
                        raise ValueError("status takes no work arguments")
                    own = [dict(task_id=i["task_id"], status=i["status"], summary=i["summary"],
                                inputs_sha256=i.get("inputs_sha256"), job_id=i.get("job_id"),
                                mode=i.get("mode", "shared"), workspace_path=i.get("workspace_path"),
                                workspace_inputs_sha256=(context_digest(release.snapshot(
                                    self.work_dir / "trees" / i["key"], CONTEXT, context_digest))[0]
                                    if i.get("mode") == "isolated" and i["status"] == "open" else None))
                           for i in self.work.values() if self._owned(i, trusted, workspace)]
                    return {"open_count": len(self._open_work()), "own": own[-20:],
                            "busy": self.active is not None}
                if action not in ("begin", "finish", "abandon"):
                    raise ValueError("unsupported work action")
                task_id = args.get("task_id")
                if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id):
                    raise ValueError("bounded stable task_id required")
                key = hashlib.sha256(canonical([trusted["user_id"], workspace, task_id])).hexdigest()
                item = self.work.get(key)
                if action == "begin":
                    if set(args) not in (
                            {"action", "task_id", "summary"},
                            {"action", "task_id", "summary", "mode"}):
                        raise ValueError("begin requires task_id and summary")
                    mode = args.get("mode", "shared")
                    if mode not in ("shared", "isolated"):
                        raise ValueError("unsupported source work mode")
                    if any(work.get("mode", "shared") != mode for work in self._open_work()):
                        raise ValueError("shared and isolated work modes cannot overlap")
                    if mode == "shared" and any(job.get("isolated") and job["status"] in ("queued", "running") for job in self.jobs.values()):
                        raise ValueError("isolated release queue active")
                    summary = args["summary"]
                    if not isinstance(summary, str) or not 1 <= len(summary) <= 200 or any(ord(c) < 32 for c in summary):
                        raise ValueError("bounded summary required")
                    if item:
                        if item["summary"] != summary or item.get("mode", "shared") != mode:
                            raise ValueError("task_id reused with changed summary or mode")
                        return {"task_id": task_id, "status": item["status"],
                                "open_count": len(self._open_work()),
                                "workspace_path": item.get("workspace_path")}
                    if mode == "shared" and self.active is not None:
                        raise ValueError("build or deployment active; wait before beginning new work")
                    if len(self.work) >= 1000:
                        raise ValueError("coordination registry full; administrator maintenance required")
                    item = {"task_id": task_id, "user_id": trusted["user_id"],
                            "workspace": workspace, "summary": summary, "status": "open",
                            "created_at": time.time(), "mode": mode, "key": key, "applies": {}}
                    if mode == "isolated":
                        blob = self._current_release_blob()
                        folder = self.work_dir / "trees" / key
                        release.extract(folder, blob, context_digest)
                        (folder / "baseline.tar").write_bytes(blob)
                        item["workspace_path"] = str(folder / "v3")
                        item["base_inputs_sha256"] = context_digest(blob)[0]
                    self.work[key] = item
                    self._save_work()
                    return {"task_id": task_id, "status": "open", "open_count": len(self._open_work()),
                            "workspace_path": item.get("workspace_path"),
                            "workspace_inputs_sha256": item.get("base_inputs_sha256")}
                if action == "abandon":
                    if set(args) != {"action", "task_id"} or not item or item["status"] != "open":
                        raise ValueError("only an open task in this session may be abandoned")
                    item.update(status="abandoned", finished_at=time.time())
                    self._save_work()
                    return {"task_id": task_id, "status": "abandoned", "open_count": len(self._open_work())}
                if set(args) != {"action", "task_id", "expected_inputs_sha256"}:
                    raise ValueError("finish requires task_id and expected_inputs_sha256")
                expected = args["expected_inputs_sha256"]
                if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                    raise ValueError("expected_inputs_sha256 required")
                if not item:
                    raise ValueError("task_id not registered in this session")
                if item["status"] == "closed":
                    if item["inputs_sha256"] != expected:
                        raise ValueError("closed task digest cannot change")
                    job_id = (item.get("job_id") if item.get("mode") == "isolated"
                              else self._auto_schedule(item, trusted, workspace))
                    return {"task_id": task_id, "status": "closed", "job_id": job_id,
                            "open_count": len(self._open_work())}
                if item.get("mode") == "isolated":
                    folder = self.work_dir / "trees" / key
                    task_blob = release.snapshot(folder, CONTEXT, context_digest)
                    current, _ = context_digest(task_blob)
                    if current != expected:
                        raise ValueError("stale work inputs")
                    base_blob = (folder / "baseline.tar").read_bytes()
                    release.candidate(base_blob, task_blob, base_blob, context_digest)
                    lease_until, fingerprint = self._incoming_lease(trusted)
                    job_id = str(uuid.uuid4())
                    private = self.state / job_id
                    private.mkdir(mode=0o700)
                    (private / "base.tar").write_bytes(base_blob)
                    (private / "task.tar").write_bytes(task_blob)
                    job = {"job_id": job_id, "request_key": "task-" + key,
                           "body_digest": hashlib.sha256(canonical([task_id, current])).hexdigest(),
                           "user_id": trusted["user_id"], "workspace": workspace,
                           "operation": "build_deploy", "isolated": True, "inputs_sha256": current,
                           "created_at": time.time(), "status": "queued", "phase": "waiting",
                           "images": {}, "log": "", "tool_name": "v3_build_work",
                           "lease_until": max(lease_until, time.time() + 4 * 3600),
                           "access_fingerprint": fingerprint, "revoked": False}
                    self._save(job)
                    self.jobs[job_id] = job
                    item.update(status="closed", inputs_sha256=current, job_id=job_id,
                                finished_at=time.time())
                    self._save_work()
                    self._start_next()
                    return {"task_id": task_id, "status": "closed",
                            "open_count": len(self._open_work()), "job_id": job_id,
                            "inputs_sha256": current}
                if self.active is not None:
                    raise ValueError("build or deployment active; close after it finishes")
                current, _ = context_digest(self.runner.snapshot())
                if current != expected:
                    raise ValueError("stale work inputs")
                item.update(status="closed", inputs_sha256=current, finished_at=time.time())
                self._save_work()
                job_id = self._auto_schedule(item, trusted, workspace)
                return {"task_id": task_id, "status": "closed", "open_count": len(self._open_work()),
                        "job_id": job_id, "inputs_sha256": current}
            if name == "v3_build_status":
                if args:
                    raise ValueError("status takes no arguments")
                digest, count = context_digest(self.runner.snapshot())
                containers = {}
                for role, target in zip(("api", "loop"), TARGETS):
                    try:
                        value = self.runner.inspect(target)
                        containers[role] = {"image_id": value["Image"], "state": value["State"]["Status"], "health": value["State"].get("Health", {}).get("Status", "unknown")}
                    except Exception:
                        containers[role] = {"state": "unavailable"}
                jobs = [self._view(j) for j in sorted(self.jobs.values(), key=lambda j: j["created_at"], reverse=True) if self._owned(j, trusted, workspace)][:3]
                return {"inputs_sha256": digest, "input_files": count, "containers": containers, "jobs": jobs, "busy": self.active is not None}
            if name == "v3_worker_job":
                if set(args) != {"job_id"}:
                    raise ValueError("job_id required")
                job = self.jobs.get(args["job_id"])
                participant = any(self._owned(item, trusted, workspace) and item.get("job_id") == args["job_id"]
                                  for item in self.work.values())
                if not job or not (self._owned(job, trusted, workspace) or participant):
                    raise ValueError("job not available to this session")
                return self._view(job)
            operation = ("build_deploy" if name == "administrator_auto_build_deploy"
                         else "build" if name == "v3_build_image" else args.get("operation"))
            allowed = {"request_id", "expected_inputs_sha256"} | ({"operation", "job_id"} if name == "v3_worker_submit" else set())
            if name not in ("v3_build_image", "v3_worker_submit", "administrator_auto_build_deploy") or set(args) - allowed or operation not in ("build", "deploy", "build_deploy"):
                raise ValueError("unsupported fixed build operation")
            if name == "v3_worker_submit" and operation == "build_deploy":
                raise ValueError("unsupported fixed build operation")
            if self.stopping:
                raise PermissionError("host broker is stopping")
            lease_until, access_fingerprint = self._incoming_lease(trusted)
            request_id, expected = args.get("request_id"), args.get("expected_inputs_sha256")
            if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id):
                raise ValueError("bounded stable request_id required")
            if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError("expected_inputs_sha256 required")
            request_key = hashlib.sha256(canonical([trusted["user_id"], workspace, request_id])).hexdigest()
            body_digest = hashlib.sha256(canonical([name, args])).hexdigest()
            if request_key in self.requests:
                job = self.jobs[self.requests[request_key]]
                if job["body_digest"] != body_digest:
                    raise ValueError("request_id reused with changed body")
                return self._view(job)
            if self.active is not None:
                raise ValueError("shared V3 build target busy; coordinate writers before retry")
            if self._open_work():
                raise ValueError("registered V3 source work remains open")
            if len(self.jobs) >= 1000:
                raise ValueError("durable job limit reached; administrator maintenance required")
            blob = self.runner.snapshot()
            current, _ = context_digest(blob)
            if current != expected:
                raise ValueError("stale build inputs")
            images = {}
            source_job = None
            if operation == "deploy":
                source_job = self.jobs.get(args.get("job_id"))
                if not source_job or not self._owned(source_job, trusted, workspace) or source_job["operation"] != "build" or source_job["status"] != "completed" or source_job["inputs_sha256"] != current:
                    raise ValueError("deploy requires this session's successful unchanged build")
                images = dict(source_job["images"])
            elif "job_id" in args:
                raise ValueError("build does not accept job_id")
            job_id = str(uuid.uuid4())
            job = {"job_id": job_id, "request_key": request_key, "body_digest": body_digest,
                   "user_id": trusted["user_id"], "workspace": workspace, "operation": operation,
                   "inputs_sha256": current, "created_at": time.time(), "status": "queued",
                   "phase": "admitted", "images": images, "log": "",
                   "tool_name": name, "lease_until": lease_until, "access_fingerprint": access_fingerprint, "revoked": False}
            if source_job:
                job["source_job_id"] = source_job["job_id"]
            self._save(job)
            self.jobs[job_id] = job
            self.requests[request_key] = job_id
            self.active = job_id
            self.pool.submit(self._run, job, blob)
            return self._view(job)

    def _phase(self, job, phase):
        with self.lock:
            self._lease(job)
            job["phase"] = phase
            self._save(job)

    def _run(self, job, blob):
        mutated = False
        try:
            with self.lock:
                self._lease(job)
                job["status"] = "running"
                self._save(job)
            if job["operation"] in ("build", "build_deploy"):
                self.runner.check_space(MIN_BUILD_FREE)
                # Capture current environment values privately solely to scrub
                # any accidental occurrence in compiler output.
                for target in TARGETS:
                    try:
                        self.runner.inspect(target)
                    except Exception:
                        pass
                for role in ("api", "loop"):
                    self._phase(job, "building_" + role)
                    image, log = self.runner.build(blob, job["job_id"], role)
                    with self.lock:
                        job["images"][role] = image
                        job["log"] = scrub(job["log"] + log, self.runner.secrets)
                        self._save(job)
                self.runner.check_space(MIN_DEPLOY_FREE)
            if job["operation"] in ("deploy", "build_deploy"):
                self._phase(job, "capturing_rollback")
                previous = {role: self.runner.inspect(name) for role, name in zip(("api", "loop"), TARGETS)}
                private = self.state / job["job_id"]
                private.mkdir(mode=0o700, exist_ok=bool(job.get("isolated")))
                # Credentials remain in protected host state, never tool output.
                fd = os.open(private / "container-config.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(canonical(previous))
                    stream.flush()
                    os.fsync(stream.fileno())
                self.runner.export([previous[r]["Image"] for r in ("api", "loop")], private / "rollback-images.tar")
                if job.get("isolated"):
                    prepared = release.snapshot(Path(job["release_root"]), CONTEXT, context_digest)
                    if context_digest(prepared)[0] != job["inputs_sha256"]:
                        raise ValueError("frozen release changed before deployment")
                    if context_digest(self._current_release_blob())[0] != job["base_inputs_sha256"]:
                        raise ValueError("release base changed before deployment")
                elif context_digest(self.runner.snapshot())[0] != job["inputs_sha256"]:
                    raise ValueError("build inputs changed before deployment")
                if self._open_work() and not job.get("isolated"):
                    raise ValueError("registered V3 source work reopened before deployment")
                with self.lock:
                    job["rollback"] = {"api": TARGETS[0] + "-pre-build-" + job["job_id"], "loop": TARGETS[1] + "-pre-build-" + job["job_id"]}
                    self._save(job)
                # Record intent before each externally visible mutation. On a
                # crash the next broker only reconciles, never retries it.
                self._phase(job, "stopping_loop")
                with self.lock:
                    self._lease(job)
                    # The API cannot renew while it replaces itself. A bounded
                    # commit lease covers container health and keeps revocation live.
                    job["lease_until"] = max(job["lease_until"], time.time() + 180)
                    job["effects_started"] = True
                    self._save(job)
                mutated = True
                self.runner.stop(TARGETS[1])
                self._phase(job, "stopping_api")
                self.runner.stop(TARGETS[0])
                for role, name in zip(("api", "loop"), TARGETS):
                    self._phase(job, "renaming_" + role)
                    self.runner.rename(name, job["rollback"][role])
                    self._phase(job, "creating_" + role)
                    if job.get("isolated"):
                        self.runner.create(name, job["images"][role], previous[role],
                                           job["release_root"])
                    else:
                        self.runner.create(name, job["images"][role], previous[role])
                    self._phase(job, "starting_" + role)
                    self.runner.start(name)
                # API readiness depends on the loop heartbeat. Start both
                # replacements before waiting for either, retaining a durable
                # checkpoint and live lease check for every health phase.
                for role, name in zip(("api", "loop"), TARGETS):
                    self._phase(job, "checking_" + role)
                    if not self.runner.healthy(name, job["images"][role]):
                        raise RuntimeError("replacement V3 " + role + " not healthy; rollback material retained")
            if job.get("isolated"):
                self._phase(job, "promoting_checkout")
                self.runner.promote_checkout(self._current_release_blob(), blob, job["changed_paths"])
            after, _ = context_digest(
                release.snapshot(Path(job["release_root"]), CONTEXT, context_digest)
                if job.get("isolated") else self.runner.snapshot())
            with self.lock:
                self._lease(job)
                job["status"] = "completed" if after == job["inputs_sha256"] else ("uncertain" if mutated else "stale")
                job["reason"] = "inputs unchanged" if after == job["inputs_sha256"] else "build inputs changed; artifact cannot be used for deployment"
                job["phase"] = "finished"
                self._save(job)
        except Exception as error:
            with self.lock:
                job["status"] = "uncertain" if mutated else ("cancelled" if isinstance(error, PermissionError) else "failed")
                job["reason"] = scrub(str(error), self.runner.secrets)
                self._save(job)
        finally:
            with self.lock:
                self.active = None
                if job["status"] not in ("uncertain", "interrupted"):
                    self._start_next()

    def request_shutdown(self):
        with self.lock:
            self.stopping = True
            if hasattr(self.runner, "cancel"):
                self.runner.cancel.set()
            for job in self.jobs.values():
                if job["status"] in ("queued", "running"):
                    job["revoked"] = True
                    self._save(job)

    def shutdown(self):
        self.pool.shutdown(wait=True)
        if self.lease is not None:
            os.close(self.lease)
            self.lease = None


async def serve(broker, listener, bridge_listener=None):
    async def client(reader, writer, bridge=False):
        request = None
        try:
            sock = writer.get_extra_info("socket")
            _, uid, _ = struct.unpack("3i", sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid not in (1000, 1100) or (bridge and uid != 1000):
                raise ValueError("broker caller identity denied")
            raw = await asyncio.wait_for(reader.readline(), 5)
            if len(raw) > 2 * 1024 * 1024 or not raw.endswith(b"\n"):
                raise ValueError("bounded request required")
            request = json.loads(raw)
            if bridge:
                if set(request) - {"id", "name", "arguments"} or request.get("name") not in ("v3_build_work", "v3_build_status", "v3_worker_job", "v3_source_apply", "v3_source_receipt", "v3_source_read"):
                    raise ValueError("bridge operation denied")
                request["workspace"] = BRIDGE_WORKSPACE
                request["trusted"] = {"user_id": "python-v2-admin", "session_id": BRIDGE_WORKSPACE,
                    "lease_until": time.time() + 4 * 3600,
                    "access_fingerprint": hashlib.sha256(b"python-v2-build-bridge").hexdigest(),
                    "bridge_caller": True}
            elif uid == 1000:
                if set(request) - {"id", "name", "arguments"} or request.get("name") not in ("v3_build_work", "v3_build_status", "v3_worker_job", "v3_source_apply", "v3_source_receipt", "v3_source_read"):
                    raise ValueError("local caller operation denied")
                request["workspace"] = LOCAL_WORKSPACE
                request["trusted"] = {"user_id": "codex-local", "session_id": LOCAL_WORKSPACE,
                    "lease_until": time.time() + 4 * 3600,
                    "access_fingerprint": hashlib.sha256(b"local-codex-build").hexdigest(),
                    "local_caller": True}
            elif request.get("trusted", {}).get("local_caller") is not None or request.get("trusted", {}).get("bridge_caller") is not None:
                raise ValueError("local or bridge authority cannot be supplied by the app")
            result = await asyncio.to_thread(broker.handle, request)
            response = {"id": request.get("id"), "status": "success", "output": result}
        except Exception as error:
            response = {"id": request.get("id") if isinstance(request, dict) else None,
                        "status": "failed", "output": scrub(str(error), broker.runner.secrets)}
        writer.write(canonical(response) + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    # Root bound this inherited descriptor through its retained directory FD.
    # That /proc/self/fd address is not valid in the unprivileged child, and
    # Python 3.13+ socket cleanup tries to stat it. The supervisor owns cleanup.
    options = {"cleanup_socket": False} if "cleanup_socket" in inspect.signature(asyncio.get_running_loop().create_unix_server).parameters else {}
    server = await asyncio.start_unix_server(
        client, sock=listener, limit=2 * 1024 * 1024 + 8192, **options)
    if bridge_listener is None:
        async with server:
            await server.serve_forever()
    else:
        bridge_server = await asyncio.start_unix_server(
            lambda reader, writer: client(reader, writer, True),
            sock=bridge_listener, limit=2 * 1024 * 1024 + 8192, **options)
        async with server, bridge_server:
            await asyncio.gather(server.serve_forever(), bridge_server.serve_forever())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--listener-fd", type=int, required=True)
    parser.add_argument("--bridge-listener-fd", type=int)
    parser.add_argument("--snapshot-helper")
    parser.add_argument("--snapshot-socket")
    arguments = parser.parse_args()
    if os.geteuid() != 1000:
        raise SystemExit("broker must run as ar UID 1000 with a protected inherited listener")
    listener = socket.socket(fileno=arguments.listener_fd)
    if listener.family != socket.AF_UNIX:
        raise SystemExit("Unix listener required")
    bridge_listener = socket.socket(fileno=arguments.bridge_listener_fd) if arguments.bridge_listener_fd is not None else None
    broker = Broker(runner=Runner(snapshot_helper=arguments.snapshot_helper, snapshot_socket=arguments.snapshot_socket))
    async def run():
        task = asyncio.create_task(serve(broker, listener, bridge_listener))
        def stop():
            broker.request_shutdown()
            task.cancel()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(signum, stop)
        try:
            await task
        except asyncio.CancelledError:
            pass
    try:
        asyncio.run(run())
    finally:
        broker.request_shutdown()
        broker.shutdown()


if __name__ == "__main__":
    main()

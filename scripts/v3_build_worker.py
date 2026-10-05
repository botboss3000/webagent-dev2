#!/usr/bin/env python3
"""Private host-side v3 build service. Fixed targets; no caller-supplied commands."""
from __future__ import annotations
import argparse
import asyncio
import fcntl
import hashlib
import hmac
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import socket
import struct
import tempfile
import tarfile
import threading
import time
import uuid

TARGET = "webagent-dev-v3"
ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")

def load_build(repo):
    spec = importlib.util.spec_from_file_location("v3_build_host", repo / "plugins/abilities/Administrator/v3_build/v3_build.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod

class ThreadingUnixServer(ThreadingHTTPServer):
    address_family = socket.AF_UNIX

CONTROL_SOCKET = Path("/var/lib/webagent-v3-build-worker/control.sock")

def restart_process():
    # The installed unit uses Restart=on-failure; exit after the reply.
    os._exit(1)

class UnixConnection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect("/var/run/docker.sock")

def engine(method, path, body=None):
    conn = UnixConnection("localhost", timeout=30)
    data = None if body is None else json.dumps(body).encode()
    try:
        conn.request(method, path, data, {"Content-Type": "application/json"})
        response = conn.getresponse()
        raw = response.read()
        if response.status >= 400:
            raise RuntimeError("Docker operation failed (HTTP %d)" % response.status)
        return json.loads(raw) if raw else {}
    finally:
        conn.close()

class Worker:
    def __init__(self, repo, state, token):
        self.repo, self.state, self.token = repo.resolve(), state.resolve(), token
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lockfile = (self.state / "worker.lock").open("a")
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.build = load_build(self.repo)
        self.lock = threading.Lock()
        self.jobs = {}
        self.active = None
        self.draining = False
        self.restart_dir = self.state / "restarts"
        self.restart_dir.mkdir(mode=0o700, exist_ok=True)
        for path in self.state.glob("*.json"):
            job = json.loads(path.read_text())
            if job["status"] in ("queued", "running"):
                job.update(status="interrupted", error="Worker restarted; inspect host before resubmitting deployment")
                self.save(job)
            self.jobs[job["id"]] = job

    def save(self, job):
        path = self.state / (job["id"] + ".json")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(job))
        tmp.chmod(0o600)
        tmp.replace(path)

    def restart(self, request_id):
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            raise ValueError("Stable restart request_id required")
        with self.lock:
            receipt = self.restart_dir / (request_id + ".json")
            if receipt.exists():
                return json.loads(receipt.read_text())["previous_pid"], False
            if self.active is not None:
                raise BlockingIOError("A build or deployment job is active")
            if self.draining:
                raise BlockingIOError("Worker is restarting")
            if len(list(self.restart_dir.glob("*.json"))) >= 1000:
                raise ValueError("Restart receipt limit reached")
            previous_pid = os.getpid()
            tmp = receipt.with_suffix(".tmp")
            tmp.write_text(json.dumps({"previous_pid": previous_pid, "request_id": request_id}))
            tmp.chmod(0o600)
            tmp.replace(receipt)
            self.draining = True
            return previous_pid, True

    def submit(self, request):
        if set(request) - {"operation", "request_id", "build_job_id"}:
            raise ValueError("Unknown request field")
        operation, key = request.get("operation"), request.get("request_id")
        if operation not in ("build", "deploy") or not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError("Expected build/deploy and a unique request_id")
        if operation == "build" and "build_job_id" in request:
            raise ValueError("build_job_id is only valid for deployment")
        with self.lock:
            if self.draining: raise BlockingIOError("Worker is restarting")
            for job in self.jobs.values():
                if job["request"] == request: return dict(job)
                if job["request"]["request_id"] == key: raise ValueError("request_id already used with different input")
            if self.active: raise BlockingIOError("Another job is active")
            if operation == "deploy":
                build = self.jobs.get(request.get("build_job_id", ""), {})
                if build.get("operation") != "build" or build.get("status") != "succeeded":
                    raise ValueError("Deployment requires a successful worker build_job_id")
            job = {"id": str(uuid.uuid4()), "operation": operation, "request": request,
                   "status": "queued", "created_at": time.time()}
            self.jobs[job["id"]] = job
            self.active = job["id"]
            self.save(job)
            threading.Thread(target=self.execute, args=(job,), daemon=True).start()
            return dict(job)

    def command(self, args, timeout=60, stdin=None):
        result = asyncio.run(self.build._run(args, timeout=timeout, stdin=stdin))
        if result.get("exit_code") != 0:
            raise RuntimeError("Fixed host command failed or timed out")
        return result

    def execute(self, job):
        try:
            job.update(status="running", started_at=time.time()); self.save(job)
            if job["operation"] == "build":
                tag = "webagent-dev-v3:worker-" + job["id"]
                with tempfile.TemporaryFile() as context:
                    self.build._context_archive(context)
                    digest = hashlib.file_digest(context, "sha256").hexdigest(); context.seek(0)
                    job["context_sha256"] = digest; self.save(job)
                    # Explicit directory context works with legacy Docker and BuildKit.
                    # The archive is generated locally from the validated file allowlist.
                    with tempfile.TemporaryDirectory(prefix="v3-build-context-") as directory:
                        with tarfile.open(fileobj=context, mode="r:") as archive:
                            archive.extractall(directory, filter="data")
                        dockerfile = str(Path(directory) / "v3/Dockerfile")
                        result = asyncio.run(self.build._run(["docker", "--host", "unix:///var/run/docker.sock", "build", "-f", dockerfile, "-t", tag, directory], timeout=1800))
                # Build output only, never container logs or inspect environment.
                job["output_tail"] = result.get("output_tail", "").replace(self.token, "[redacted]")
                job.update(exit_code=result.get("exit_code"), timed_out=result.get("timed_out", False),
                           output_truncated=result.get("output_truncated", False))
                if result.get("exit_code") != 0: raise RuntimeError("Image build failed or timed out; see build output")
                image = engine("GET", "/images/" + tag + "/json")
                job.update(image_id=image["Id"], image_tag=tag, context_sha256=digest)
            else:
                self.deploy(job)
            job.update(status="succeeded")
        except Exception as exc:
            job.update(status="failed", error=str(exc).replace(self.token, "[redacted]"))
        finally:
            job["finished_at"] = time.time(); self.save(job)
            with self.lock: self.active = None

    def deploy(self, job):
        build = self.jobs[job["request"]["build_job_id"]]
        # Check the pinned artifact before any container lifecycle mutation.
        try:
            image = engine("GET", "/images/" + build["image_id"] + "/json")
            if image.get("Id") != build["image_id"]:
                raise RuntimeError("Pinned image identity mismatch")
        except Exception as exc:
            raise RuntimeError("Deployment preflight image check failed: " + str(exc)) from exc
        targets = (TARGET, TARGET + "-loop")
        plans = []
        for target in targets:
            old = engine("GET", "/containers/" + target + "/json")
            if old.get("Name") != "/" + target or not old["State"].get("Running"):
                raise RuntimeError("Expected running v3 development pair; initial provisioning is host-only")
            host = old["HostConfig"]
            if host.get("Privileged") or host.get("AutoRemove") or host.get("NetworkMode") not in ("default", "bridge"):
                raise RuntimeError("Unexpected container security/network configuration; host review required")
            mounts = {m["Destination"]: m.get("Source") for m in old.get("Mounts", []) if m.get("Type") == "bind"}
            if mounts.get("/state") != str(self.repo / "v3/data") or mounts.get("/workspaces") != str(self.repo / "v3/workspaces"):
                raise RuntimeError("V3 state/workspace mounts do not match the configured development checkout")
            if any("docker.sock" in source for source in mounts.values()):
                raise RuntimeError("Refusing container with Docker socket mount")
            payload = dict(old["Config"])
            payload["Image"] = build["image_id"]
            payload["HostConfig"] = host
            if target.endswith("-loop"):
                # Same tested executable, retaining loop identity and isolation.
                payload["Entrypoint"] = ["/usr/bin/tini", "--"]
                payload["Cmd"] = ["/usr/local/bin/webagent-v3"]
                payload["Healthcheck"] = {"Test": ["CMD", "env", "V3_PROCESS_ROLE=health", "/usr/local/bin/webagent-v3"], "Interval": 5000000000, "Timeout": 3000000000, "StartPeriod": 20000000000, "Retries": 3}
                payload["Env"] = [e for e in payload.get("Env", []) if not e.startswith("V3_PROCESS_ROLE=")] + ["V3_PROCESS_ROLE=loop"]
            plans.append({"target": target, "backup": target + "-rollback-" + job["id"], "payload": payload,
                          "stopped": False, "renamed": False, "created": False})
        job["backup_containers"] = {p["target"]: p["backup"] for p in plans}
        self.save(job)
        stage = "stop"
        try:
            # Stop loop first; start API before loop so its sockets are ready.
            for p in reversed(plans):
                engine("POST", "/containers/" + p["target"] + "/stop?t=30"); p["stopped"] = True
            for p in plans:
                stage = "rename:" + p["target"]
                engine("POST", "/containers/" + p["target"] + "/rename?name=" + p["backup"]); p["renamed"] = True
                stage = "create:" + p["target"]
                engine("POST", "/containers/create?name=" + p["target"], p["payload"]); p["created"] = True
                stage = "start:" + p["target"]
                engine("POST", "/containers/" + p["target"] + "/start")
            stage = "readiness"
            for _ in range(60):
                states = [engine("GET", "/containers/" + p["target"] + "/json") for p in plans]
                if all(s["State"].get("Health", {}).get("Status") == "healthy" for s in states):
                    job.update(deployed_image=build["image_id"], readiness="healthy", deployed_targets=list(targets)); return
                if any(not s["State"].get("Running") for s in states): break
                time.sleep(2)
            raise RuntimeError("Replacement pair did not become healthy")
        except Exception as exc:
            job["failed_stage"] = stage
            try:
                for p in reversed(plans):
                    if p["created"]: engine("DELETE", "/containers/" + p["target"] + "?force=true")
                    if p["renamed"]: engine("POST", "/containers/" + p["backup"] + "/rename?name=" + p["target"])
                for p in plans:
                    if p["stopped"]: engine("POST", "/containers/" + p["target"] + "/start")
                job["rollback"] = "restored"
            except Exception:
                job["rollback"] = "failed; host intervention required"
            raise RuntimeError("Deployment failed at " + stage + ": " + str(exc)) from exc

    def status(self):
        version = self.command(["docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{.ServerVersion}}"], timeout=15)
        return {"scope": "v3_development_host", "host": socket.gethostname(), "active_job": self.active,
                "docker_version": version.get("output_tail", "").strip(), "observed_at": time.time(), "pid": os.getpid()}

class Handler(BaseHTTPRequestHandler):
    server_version = "V3BuildWorker/1"
    def log_message(self, *args): pass  # Authorization and URL input never logged.
    def respond(self, code, body):
        data = json.dumps(body).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
    def dispatch(self, post=False):
        self.connection.settimeout(10)
        worker = self.server.worker
        local = getattr(self.server, "local_control", False)
        if local:
            _, uid, _ = struct.unpack("3i", self.connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            if uid != os.getuid():
                return self.respond(401, {"error": "Unauthorized"})
        elif not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + worker.token).encode()):
            return self.respond(401, {"error": "Unauthorized"})
        try:
            if post and self.path == "/v1/restart":
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 256:
                    raise ValueError("Invalid restart body")
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict) or set(body) != {"request_id"}:
                    raise ValueError("Stable restart request_id required")
                previous_pid, scheduled = worker.restart(body["request_id"])
                if scheduled:
                    threading.Timer(0.5, restart_process).start()
                self.respond(202, {"status": "restarting", "previous_pid": previous_pid})
                return
            if post and self.path == "/v1/jobs":
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2048: raise ValueError("Invalid body size")
                request = json.loads(self.rfile.read(size))
                if not isinstance(request, dict): raise ValueError("Expected JSON object")
                return self.respond(202, worker.submit(request))
            if not post and self.path == "/v1/status": return self.respond(200, worker.status())
            if not post and self.path.startswith("/v1/jobs/"):
                key = self.path.removeprefix("/v1/jobs/")
                if not ID.fullmatch(key): raise ValueError("Invalid job ID")
                job = worker.jobs.get(key)
                return self.respond(200 if job else 404, job or {"error": "Unknown job"})
            self.respond(404, {"error": "Unknown route"})
        except BlockingIOError: self.respond(409, {"error": "Worker busy"})
        except (ValueError, TypeError): self.respond(400, {"error": "Invalid request"})
        except Exception: self.respond(503, {"error": "Worker operation unavailable; inspect host service"})
    def do_GET(self): self.dispatch()
    def do_POST(self): self.dispatch(True)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path("/home/ar/webagent-dev2"))
    parser.add_argument("--state", type=Path, default=Path("/var/lib/webagent-v3-build-worker"))
    parser.add_argument("--bind", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092)
    args = parser.parse_args()
    token = os.environ.pop("V3_BUILD_WORKER_TOKEN", "")
    if len(token) < 32 or len(token) > 256 or not token.isascii() or any(c.isspace() for c in token):
        parser.error("Set a random 32-256 character V3_BUILD_WORKER_TOKEN")
    if args.bind in ("0.0.0.0", "::"): parser.error("Bind to loopback or a specific private interface")
    worker = Worker(args.repo, args.state, token)
    control = args.state / "control.sock"
    if control != CONTROL_SOCKET or control.is_symlink():
        parser.error("Unexpected control socket path")
    control.unlink(missing_ok=True)
    local_server = ThreadingUnixServer(str(control), Handler)
    local_server.worker = worker
    local_server.local_control = True
    control.chmod(0o600)
    threading.Thread(target=local_server.serve_forever, daemon=True).start()
    server = ThreadingHTTPServer((args.bind, args.port), Handler); server.worker = worker
    server.serve_forever()

if __name__ == "__main__": main()

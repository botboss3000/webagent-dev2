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

    def submit(self, request):
        if set(request) - {"operation", "request_id", "build_job_id"}:
            raise ValueError("Unknown request field")
        operation, key = request.get("operation"), request.get("request_id")
        if operation not in ("build", "deploy") or not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError("Expected build/deploy and a unique request_id")
        if operation == "build" and "build_job_id" in request:
            raise ValueError("build_job_id is only valid for deployment")
        with self.lock:
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
        old = engine("GET", "/containers/" + TARGET + "/json")
        if old.get("Name") != "/" + TARGET or not old["State"].get("Running"):
            raise RuntimeError("Expected running v3 development container; initial provisioning is host-only")
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
        backup = TARGET + "-rollback-" + job["id"]
        job["backup_container"] = backup; self.save(job)
        stopped = renamed = created = False
        try:
            engine("POST", "/containers/" + TARGET + "/stop?t=15"); stopped = True
            engine("POST", "/containers/" + TARGET + "/rename?name=" + backup); renamed = True
            engine("POST", "/containers/create?name=" + TARGET, payload); created = True
            engine("POST", "/containers/" + TARGET + "/start")
            for _ in range(45):
                inspect = engine("GET", "/containers/" + TARGET + "/json")
                if inspect["State"].get("Health", {}).get("Status") == "healthy":
                    job.update(deployed_image=build["image_id"], readiness="healthy"); return
                if not inspect["State"].get("Running"): break
                time.sleep(2)
            raise RuntimeError("Replacement did not become healthy")
        except Exception:
            try:
                if created: engine("DELETE", "/containers/" + TARGET + "?force=true")
                if renamed: engine("POST", "/containers/" + backup + "/rename?name=" + TARGET)
                if stopped: engine("POST", "/containers/" + TARGET + "/start")
                job["rollback"] = "restored" if stopped else "not_needed"
            except Exception:
                job["rollback"] = "failed; host intervention required"
            raise

    def status(self):
        version = self.command(["docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{.ServerVersion}}"], timeout=15)
        return {"scope": "v3_development_host", "host": socket.gethostname(), "active_job": self.active,
                "docker_version": version.get("output_tail", "").strip(), "observed_at": time.time()}

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
        supplied = self.headers.get("Authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + worker.token).encode()):
            return self.respond(401, {"error": "Unauthorized"})
        try:
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
    server = ThreadingHTTPServer((args.bind, args.port), Handler); server.worker = worker
    server.serve_forever()

if __name__ == "__main__": main()

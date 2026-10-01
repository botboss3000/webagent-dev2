#!/usr/bin/env python3
"""Private trusted-admin workspace service. No live apply or deployment API."""
from __future__ import annotations
import argparse
import fcntl
import hashlib
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import uuid

LIMIT = 1024 * 1024
ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
OPS = {"seed", "read", "review", "patch", "check", "test", "propose"}
FIELDS = {"seed": set(), "read": {"path", "offset", "limit"}, "review": set(),
          "patch": {"patch", "expected_review_sha256"}, "check": {"timeout_seconds", "expected_review_sha256"},
          "test": {"timeout_seconds", "expected_review_sha256"},
          "propose": {"expected_review_sha256", "verification_summary"}}
SOURCE_FILES = {"Cargo.toml", "Cargo.lock", "Dockerfile", ".gitignore", "README.md",
                "worker.py", "promote.py", "launch.py", "deploy.sh", "ui-baseline.json"}
SOURCE_DIRS = {"src", "tests", "plugins", "ui", "docs"}

def load_worker(repo):
    spec = importlib.util.spec_from_file_location("workspace_helpers", repo / "v3/worker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

def durable_json(path, value):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            stream.write(json.dumps(value).encode()); stream.flush(); os.fsync(stream.fileno())
            os.chmod(temporary, 0o600); os.replace(temporary, path)
            fd = os.open(path.parent, os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally: temporary.unlink(missing_ok=True)

class WorkspaceService:
    def __init__(self, repo, state):
        self.repo, self.state = Path(repo).resolve(), Path(state).resolve()
        if self.state.is_relative_to(self.repo):
            raise ValueError("State must be outside the repository")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.lockfile = (self.state / "service.lock").open("a")
        fcntl.flock(self.lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        self.lock = threading.Lock()
        self.receipts = self.state / "receipts"; self.receipts.mkdir(mode=0o700, exist_ok=True)
        self.proposals = self.state / "proposals"; self.proposals.mkdir(mode=0o700, exist_ok=True)
        self.control = self.state / "control"; self.control.mkdir(mode=0o700, exist_ok=True)
        (self.control / "dispatch.lock").touch()
        if not (self.control / "shutoff").exists(): (self.control / "shutoff").write_text("0")
        self.helper = load_worker(self.repo); self.helper.CONTROL = self.control
        for path in self.receipts.glob("*.json"):
            value = json.loads(path.read_text())
            if value["status"] == "running":
                value.update(status="uncertain", result={"status": "uncertain", "error": "Service restarted; inspect workspace before a new mutation"})
                durable_json(path, value)

    def base(self, workspace):
        if not isinstance(workspace, str) or str(uuid.UUID(workspace)) != workspace:
            raise ValueError("workspace_id must be a canonical UUID")
        root = self.state / "workspaces"; root.mkdir(mode=0o700, exist_ok=True)
        path = root / workspace
        if path.is_symlink(): raise ValueError("Invalid workspace")
        path.mkdir(mode=0o700, exist_ok=True)
        return path

    def receipt(self, key):
        if not isinstance(key, str) or not ID.fullmatch(key): raise ValueError("Invalid request_id")
        path = self.receipts / (key + ".json")
        if not path.exists(): raise FileNotFoundError("Receipt not found")
        return json.loads(path.read_text())

    def submit(self, request):
        if not isinstance(request, dict) or set(request) != {"operation", "workspace_id", "request_id", "arguments"}:
            raise ValueError("Expected operation, workspace_id, request_id and arguments")
        op, key, args = request["operation"], request["request_id"], request["arguments"]
        if not isinstance(op, str) or op not in OPS or not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError("Invalid operation or request_id")
        if not isinstance(args, dict) or set(args) - FIELDS[op]: raise ValueError("Unknown arguments")
        digest = hashlib.sha256(json.dumps(request, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        if not self.lock.acquire(blocking=False): raise BlockingIOError("Workspace service busy")
        try:
            path = self.receipts / (key + ".json")
            if path.exists():
                value = self.receipt(key)
                if value["request_sha256"] != digest: raise ValueError("request_id reused with different input")
                return value
            base = self.base(request["workspace_id"])
            if (self.control / "shutoff").read_text().strip() != "0": raise ValueError("Execution disabled")
            value = {"request_id": key, "workspace_id": request["workspace_id"], "operation": op,
                     "request_sha256": digest, "status": "running"}
            durable_json(path, value)
            try:
                result = self.execute(base, op, args, key)
                value.update(status=result.get("status", "success"), result=result)
            except Exception:
                # Fail closed: exceptions after dispatch do not prove absence of effects.
                value.update(status="uncertain", result={"status": "uncertain", "error": "Operation interrupted or rejected; inspect workspace and review before a new mutation"})
            durable_json(path, value)
            return value
        finally: self.lock.release()

    def reviewed(self, base, expected):
        review = self.helper.source_review(base)
        if not expected or review["truncated"] or review["review_sha256"] != expected:
            raise ValueError("Complete matching source review required")
        return review

    def execute(self, base, op, args, key):
        if op == "seed":
            with tempfile.TemporaryDirectory(dir=self.state, prefix="template-") as directory:
                template = Path(directory)
                for name in sorted(SOURCE_FILES | SOURCE_DIRS):
                    source = self.repo / "v3" / name
                    if not source.exists(): continue
                    entries = [source] + (list(source.rglob("*")) if source.is_dir() else [])
                    if any(p.is_symlink() or not (p.is_file() or p.is_dir()) for p in entries):
                        raise ValueError("Template contains links or special files")
                    if source.is_dir(): shutil.copytree(source, template / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
                    else: shutil.copy2(source, template / name)
                self.helper.TEMPLATE = template
                return self.helper.seed_workspace(base)
        if op == "review": return self.helper.source_review(base)
        if op == "patch": return self.helper.source_patch(base, args)
        if op == "read":
            raw = args.get("path", "")
            if not isinstance(raw, str) or not raw or raw.startswith("/") or "\\" in raw or any(p in ("", ".", "..", ".git", "target", "data", "workspaces") for p in raw.split("/")):
                raise ValueError("Invalid source path")
            offset, count = args.get("offset", 1), args.get("limit", 200)
            if type(offset) is not int or offset < 1 or type(count) is not int or not 1 <= count <= 2000:
                raise ValueError("Invalid line range")
            source = base / "webagent-v3"; path = source
            for part in raw.split("/"):
                path = path / part
                if path.is_symlink(): raise ValueError("Symlink rejected")
            if not path.is_file() or path.stat().st_size > LIMIT: raise ValueError("File unavailable or oversized")
            text = path.read_text(); lines = text.splitlines(keepends=True)
            return {"status": "success", "output": "".join(lines[offset-1:offset-1+count]), "total_lines": len(lines), "sha256": hashlib.sha256(text.encode()).hexdigest()}
        review = self.reviewed(base, args.get("expected_review_sha256"))
        if op == "propose":
            summary = args.get("verification_summary")
            if not isinstance(summary, str) or not 1 <= len(summary) <= 4000: raise ValueError("Verification summary required")
            if not review["diff"]: raise ValueError("No changes to propose")
            proposal = {"status": "proposed", "request_id": key, "workspace_id": base.name,
                        "review": review, "verification_summary": summary, "applied": False,
                        "approval_required": True, "origin": "workspace_adapter"}
            durable_json(self.proposals / (key + ".json"), proposal)
            return proposal
        if not shutil.which("cargo"):
            return {"status": "blocked", "error": "Rust 1.90 and Cargo must be installed on this workspace host"}
        timeout = args.get("timeout_seconds", 300)
        if type(timeout) is not int or not 30 <= timeout <= 600: raise ValueError("Invalid timeout")
        # Trusted repository code executes. No service credential is passed to child processes.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "CARGO_HOME", "RUSTUP_HOME", "SSL_CERT_FILE", "SSL_CERT_DIR") if k in os.environ}
        env.update(CARGO_TARGET_DIR=str(base / "target"), CARGO_BUILD_JOBS="1", CARGO_INCREMENTAL="0")
        with tempfile.TemporaryFile() as output:
            proc = subprocess.Popen(["cargo", op, "--locked", "--manifest-path", str(base / "webagent-v3/Cargo.toml")],
                                    cwd=base / "webagent-v3", env=env, stdin=subprocess.DEVNULL,
                                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            timed_out = False
            try: proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired: timed_out = True
            finally:
                try: os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError: pass
                proc.wait()
            size = output.seek(0, 2); output.seek(max(0, size - 16384))
            tail = output.read().decode(errors="replace")
        after = self.helper.source_review(base)
        return {"status": "success" if proc.returncode == 0 and not timed_out else "failed",
                "exit_code": proc.returncode, "timed_out": timed_out, "output_tail": tail,
                "verified_review_sha256": review["review_sha256"], "source_changed": after["review_sha256"] != review["review_sha256"]}

def serve(service, bind, port, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def response(self, status, value):
            data = json.dumps(value).encode(); self.send_response(status)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(data)))
            self.end_headers(); self.wfile.write(data)
        def authenticated(self):
            if not hmac.compare_digest(self.headers.get("Authorization", ""), "Bearer " + token):
                self.response(401, {"status": "denied"}); return False
            return True
        def do_GET(self):
            if not self.authenticated(): return
            try:
                if self.path == "/v1/status":
                    self.response(200, {"status": "ok", "operations": sorted(OPS), "live_apply": False,
                                        "cargo_available": bool(shutil.which("cargo")), "protocol_version": 1})
                elif self.path.startswith("/v1/receipts/"):
                    self.response(200, service.receipt(self.path.removeprefix("/v1/receipts/")))
                else: self.response(404, {"status": "not_found"})
            except (ValueError, FileNotFoundError): self.response(404, {"status": "not_found"})
        def do_POST(self):
            if not self.authenticated(): return
            if self.path != "/v1/actions": self.response(404, {"status": "not_found"}); return
            self.connection.settimeout(10)
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 2 * LIMIT: raise ValueError("Invalid body size")
                value = json.loads(self.rfile.read(length))
                self.response(200, service.submit(value))
            except BlockingIOError: self.response(409, {"status": "busy"})
            except (ValueError, TypeError): self.response(400, {"status": "rejected"})
            except Exception: self.response(500, {"status": "uncertain", "error": "Inspect receipt before retrying"})
    return ThreadingHTTPServer((bind, port), Handler)

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True); parser.add_argument("--state", required=True)
    parser.add_argument("--bind", default="127.0.0.1"); parser.add_argument("--port", type=int, default=8093)
    args = parser.parse_args()
    token = os.environ.pop("V3_WORKSPACE_WORKER_TOKEN", "")
    if not 32 <= len(token) <= 256 or not all(33 <= ord(c) <= 126 for c in token):
        raise SystemExit("Configure a valid workspace-worker credential")
    serve(WorkspaceService(args.repo, args.state), args.bind, args.port, token).serve_forever()

if __name__ == "__main__": main()

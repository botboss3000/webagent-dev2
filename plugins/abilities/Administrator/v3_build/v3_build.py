"""Scoped v3 build tools; discovered as an Administrator drop-in ability."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from typing import Any, Dict
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
TOOL_SCHEMAS.update({
    "v3_worker_submit": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": ["build", "deploy"]},
        "request_id": {"type": "string", "pattern": "^[a-zA-Z0-9_-]{1,80}$"},
        "build_job_id": {"type": "string"}}, "required": ["operation", "request_id"], "additionalProperties": False},
    "v3_worker_job": {"type": "object", "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"], "additionalProperties": False},
})
_V3_LOGIN_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {"type": "string", "description": "V3 relative route, default /app?tab=account. Origin is server-configured, not caller-selectable."},
        "browser_session_id": {"type": "string", "description": "Optional owned/shared browser tab; defaults to this agent's shared tab."},
    },
    "required": [],
    "additionalProperties": False,
}


def _v3_login_config(user_id: str) -> tuple[str, str]:
    """Explicit trusted-admin mapping; no v2 token or identity is imported into v3."""
    import os
    origin = os.environ.get("V3_DIAGNOSTICS_ORIGIN", "https://dev.webagent.live").rstrip("/")
    parsed = urlsplit(origin)
    if (parsed.scheme not in {"https", "http"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
        raise ValueError("V3_DIAGNOSTICS_ORIGIN must be an HTTPS origin (or local loopback HTTP).")
    token = os.environ.get("V3_BROWSER_LOGIN_TOKEN", "")
    caller = os.environ.get("V3_BROWSER_LOGIN_CALLER_ID", "")
    if not token or not caller:
        raise ValueError("V3 browser login is not configured: set V3_BROWSER_LOGIN_TOKEN and V3_BROWSER_LOGIN_CALLER_ID on both servers.")
    if caller != user_id:
        raise ValueError("Current user is not the configured v3 diagnostics administrator.")
    if not 32 <= len(token) <= 256 or not all(33 <= ord(c) <= 126 for c in token):
        raise ValueError("V3 browser login credential configuration is invalid.")
    return origin, token


async def _request_v3_cookie(origin: str, token: str, caller: str) -> dict:
    """Never return this result to a model. No redirects, proxies, or raw errors."""
    import httpx
    from http.cookies import SimpleCookie
    import time
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
            response = await client.post(origin + "/api/v3/diagnostics/browser-login",
                                         headers={"Authorization": "Bearer " + token},
                                         json={"caller_id": caller})
        if response.status_code != 200:
            raise ValueError(f"V3 login bridge returned HTTP {response.status_code}; check its deployment and trusted-admin configuration.")
        identity = response.json()
        jar = SimpleCookie()
        jar.load(response.headers.get("set-cookie", ""))
        cookie = jar.get("v3_session")
        if (identity.get("user_id") != "admin" or not cookie or len(cookie.value) != 36
                or not cookie["httponly"] or cookie["samesite"].lower() != "strict"
                or cookie["path"] != "/" or cookie["max-age"] != "600"
                or (origin.startswith("https:") and not cookie["secure"])):
            raise ValueError("V3 login bridge did not return the expected short-lived administrator session.")
        return {"name": "v3_session", "value": cookie.value, "url": origin + "/",
                "httpOnly": True, "secure": bool(cookie["secure"]), "sameSite": "Strict",
                "expires": time.time() + 600}
    except ValueError:
        raise
    except Exception:
        raise ValueError("V3 login bridge request failed; check reachability and deployment.") from None


TOOL_SCHEMAS["login_as_current_user_v3"] = _V3_LOGIN_PARAMETERS
DESTRUCTIVE = {"v3_cargo_task", "v3_build_image", "v3_worker_submit", "login_as_current_user_v3"}
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


async def _worker_request(method, path, body=None):
    import httpx
    from urllib.parse import urlsplit
    url = os.environ.get("V3_BUILD_WORKER_URL", "").rstrip("/")
    token = os.environ.get("V3_BUILD_WORKER_CLIENT_TOKEN", "")
    if not url or not token:
        return {"status": "blocked", "scope": "dedicated_build_worker", "error": "Configure V3_BUILD_WORKER_URL and V3_BUILD_WORKER_CLIENT_TOKEN on the WebAgent server"}
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        return {"status": "error", "error": "Worker URL must be an origin without credentials or path"}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, url + path, headers={"Authorization": "Bearer " + token}, json=body)
            if response.status_code >= 300:
                return {"status": "error", "http_status": response.status_code, "error": "Worker request rejected; check worker authentication/status"}
            return response.json()
    except (httpx.HTTPError, ValueError):
        return {"status": "error", "scope": "dedicated_build_worker", "error": "Worker unreachable or response invalid; do not repeat an uncertain submit with a new request_id"}


def build_tools(*, user_id="", session_id="", agent_id="", agent_template_id="", **ctx):
    async def login_as_current_user_v3(path: str = "/app?tab=account", browser_session_id: str = "", **_legacy) -> str:
        """Login to independent v3 as the explicitly mapped diagnostics admin.

        The bridge token and HttpOnly session cookie remain server-side. This is
        an administrator-only development capability, not cross-version SSO.
        """
        from plugins.abilities.Administrator.diagnostics.diagnostics import _development_path, _decode_browser_result
        if not user_id:
            return json.dumps({"status": "error", "message": "No signed-in user for this run."})
        try:
            origin, token = _v3_login_config(user_id)
        except ValueError as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        target = _development_path(origin, path or "/app?tab=account")
        if not target or "\\" in (path or "") or any(ord(c) < 32 for c in (path or "")):
            return json.dumps({"status": "error", "message": "path must be a same-origin v3 relative route beginning with /."})
        bs_id = None
        installed = False
        try:
            from app.auth.identity import _is_admin
            if not await _is_admin(user_id):
                return json.dumps({"status": "error", "message": "V3 development login requires a signed-in platform administrator."})
            from app.tools.browser import (backend_status, browser_action, get_or_create_page,
                                           open_server_browser, resolve_agent_session)
            bs_id = resolve_agent_session(user_id, agent_id or "", browser_session_id or None, session_id or None)
            switched = await open_server_browser(bs_id)
            if not switched.get("success") or backend_status(bs_id).get("backend") != "server":
                return json.dumps({"status": "error", "message": "V3 development login requires the server-backed browser."})
            cookie = await _request_v3_cookie(origin, token, user_id)
            page = await get_or_create_page(bs_id)
            await page.context.add_cookies([cookie])
            installed = True
            # Use browser_action so first-party navigation and screenshots retain normal lifecycle handling.
            navigated = await browser_action(bs_id, action="navigate", url=target)
            if not navigated.get("success"):
                raise RuntimeError("navigation failed")
            if urlsplit(page.url).netloc != urlsplit(origin).netloc or urlsplit(page.url).scheme != urlsplit(origin).scheme:
                raise RuntimeError("unexpected browser origin")
            check = await browser_action(bs_id, action="evaluate", js=(
                "(async()=>{const r=await fetch('/api/v3/auth/me',{credentials:'same-origin',cache:'no-store'});"
                "const d=await r.json().catch(()=>({}));return JSON.stringify({http_status:r.status,"
                "user_id:d.user_id||'',username:d.user||'',is_admin:d.is_admin===true});})()"
            ))
            verified = _decode_browser_result(check)
            if not check.get("success") or verified.get("http_status") != 200 or verified.get("user_id") != "admin" or verified.get("is_admin") is not True:
                raise RuntimeError("identity verification failed")
            return json.dumps({"status": "ok", "application": "v3", "origin": origin,
                               "url": target, "backend": "server", "browser_session_id": bs_id,
                               "caller_user_id": user_id, "user_id": "admin", "verified": verified,
                               "expires_in": 600, "note": "Independent v3 administrator session verified; no credentials returned. Use browser_action for v3 checks, not the v2 dev_browser_action facade."})
        except ValueError as exc:
            # _request_v3_cookie reports only sanitized configuration/HTTP failures.
            return json.dumps({"status": "error", "message": str(exc)})
        except Exception:
            if installed and bs_id:
                try:
                    page = await get_or_create_page(bs_id)
                    await page.context.clear_cookies(name="v3_session", domain=urlsplit(origin).hostname)
                except Exception:
                    pass
            return json.dumps({"status": "error", "message": "V3 browser login or identity verification failed; no verified login is available."})

    async def v3_build_status():
        """Check Rust, Cargo, C compiler, Docker daemon and disk prerequisites for v3."""
        return json.dumps(await _worker_request("GET", "/v1/status") if os.environ.get("V3_BUILD_WORKER_URL") else await _probe())

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
    async def v3_worker_submit(operation, request_id, build_job_id=None):
        """Submit a fixed build or deployment to the dedicated v3 development worker."""
        import re
        if operation not in ("build", "deploy") or not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", request_id):
            return json.dumps({"status": "error", "error": "Invalid operation or request_id"})
        body = {"operation": operation, "request_id": request_id}
        if build_job_id is not None: body["build_job_id"] = build_job_id
        return json.dumps(await _worker_request("POST", "/v1/jobs", body))

    async def v3_worker_job(job_id):
        """Poll durable build/deploy job state and bounded build output from the worker."""
        import re
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", job_id):
            return json.dumps({"status": "error", "error": "Invalid job ID"})
        return json.dumps(await _worker_request("GET", "/v1/jobs/" + job_id))

    return {"login_as_current_user_v3": login_as_current_user_v3, "v3_worker_submit": v3_worker_submit, "v3_worker_job": v3_worker_job, "v3_build_status": v3_build_status, "v3_cargo_task": v3_cargo_task, "v3_build_image": v3_build_image}

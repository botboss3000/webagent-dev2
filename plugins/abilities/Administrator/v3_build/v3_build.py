"""Scoped v3 build tools; discovered as an Administrator drop-in ability."""
from __future__ import annotations
import json
import tempfile
import tarfile
import socket
import signal
import shutil
from pathlib import Path
import hashlib
import asyncio
import os
from urllib.parse import urlsplit
from typing import Any, Dict

ROOT = Path(__file__).resolve().parents[4]
CONTEXT = (
    "v3/Dockerfile", "v3/Cargo.toml", "v3/Cargo.lock", "v3/build.rs", "v3/plugins/diagnostics", "v3/.gitignore", "v3/src", "v3/ui",
    "v3/plugins/abilities", "v3/worker.py", "v3/launch.py", "v3/promote.py", "v3/ui-baseline.json",
    "v3/config", "v3/docs", "v3/plugins/gates", "v3/sandbox.py",
    "v3/data/config/gates/message_visibility_v1.json",
    "v3/data/config/gates/closing_status_v1.json",
    "v3/tests", "v3/README.md", "v3/deploy.sh", "ui/shared/css/app1.css", "ui/shared/css/app2.css",
    "ui/shared/css/app3.css", "ui/shared/css/design-system.css", "ui/shared/css/index.css",
    "ui/main-panel/agents/agents.html", "ui/main-panel/agents/agents.css", "ui/vendor/fonts",
    "ui/vendor/lucide", "ui/favicon.svg",
)
TOOL_SCHEMAS = {
    "v3_build_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "v3_build_work_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "v3_worker_job": {"type": "object", "properties": {"job_id": {"type": "string"}},
        "required": ["job_id"], "additionalProperties": False},
}
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


TOOL_SCHEMAS.update({
    "v3_build_work": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["begin", "abandon"]},
        "task_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"},
        "summary": {"type": "string", "minLength": 1, "maxLength": 200}},
        "required": ["action"], "additionalProperties": False},
    "v3_build_finish": {"type": "object", "properties": {
        "task_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"},
        "expected_inputs_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        "required": ["task_id", "expected_inputs_sha256"], "additionalProperties": False},
    "v3_source_read": {"type": "object", "properties": {
        "task_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"},
        "path": {"type": "string", "maxLength": 240},
        "offset": {"type": "integer", "minimum": 1},
        "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
        "required": ["task_id", "path"], "additionalProperties": False},
    "v3_source_apply": {"type": "object", "properties": {
        "task_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"},
        "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"},
        "patch": {"type": "string", "maxLength": 1048576},
        "expected_inputs_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"}},
        "required": ["task_id", "request_id", "patch", "expected_inputs_sha256"],
        "additionalProperties": False},
    "v3_source_receipt": {"type": "object", "properties": {
        "task_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"},
        "request_id": {"type": "string", "pattern": "^[A-Za-z0-9_-]{8,80}$"}},
        "required": ["task_id", "request_id"], "additionalProperties": False},
})
TOOL_SCHEMAS["login_as_current_user_v3"] = _V3_LOGIN_PARAMETERS
DESTRUCTIVE = {"login_as_current_user_v3", "v3_build_work", "v3_build_finish", "v3_source_apply"}


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
        async with httpx.AsyncClient(timeout=75, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, url + path, headers={"Authorization": "Bearer " + token}, json=body)
            if response.status_code == 409:
                return {"status": "busy", "http_status": 409, "error": "Worker has an active job or is restarting"}
            if response.status_code >= 300:
                return {"status": "error", "http_status": response.status_code, "error": "Worker request rejected; check worker authentication/status"}
            return response.json()
    except (httpx.HTTPError, ValueError):
        return {"status": "error", "scope": "dedicated_build_worker", "error": "Worker unreachable or response invalid; do not repeat an uncertain submit with a new request_id"}


async def _bridge_request(user_id, name, arguments):
    """V2 authorizes the mapped administrator; the worker's socket gives it a distinct V3 identity."""
    from app.auth.identity import _is_admin
    mapped = os.environ.get("V3_BROWSER_LOGIN_CALLER_ID", "")
    if not user_id or user_id != mapped or not await _is_admin(user_id):
        return {"status": "denied", "error": "Mapped platform administrator required"}
    return await _worker_request("POST", "/v1/coordinator", {"name": name, "arguments": arguments})


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
        """Read the native broker's source digest and both development container states."""
        return json.dumps(await _bridge_request(user_id, "v3_build_status", {}))

    async def v3_worker_job(job_id):
        """Read a native durable build and deployment receipt."""
        import re
        if not isinstance(job_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,80}", job_id):
            return json.dumps({"status": "error", "error": "Invalid job ID"})
        return json.dumps(await _bridge_request(user_id, "v3_worker_job", {"job_id": job_id}))

    async def v3_build_work_status():
        """Read native task receipts and shared open count without mutating work."""
        return json.dumps(await _bridge_request(user_id, "v3_build_work", {"action": "status"}))

    async def v3_build_work(action, task_id="", summary=""):
        """Register or abandon source work without scheduling a build."""
        import re
        if action not in ("begin", "abandon"):
            return json.dumps({"status": "error", "error": "Use v3_build_finish for deployment-capable finish"})
        arguments = {"action": action}
        if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id):
            return json.dumps({"status": "error", "error": "Stable task_id required"})
        arguments["task_id"] = task_id
        if action == "begin":
            if not isinstance(summary, str) or not 1 <= len(summary) <= 200:
                return json.dumps({"status": "error", "error": "Bounded summary required"})
            arguments["summary"] = summary
            arguments["mode"] = "isolated"
        return json.dumps(await _bridge_request(user_id, "v3_build_work", arguments))

    async def v3_build_finish(task_id, expected_inputs_sha256):
        """Freeze this task and queue its own serialized V3 development release."""
        import re
        if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id)
                or not isinstance(expected_inputs_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_inputs_sha256)):
            return json.dumps({"status": "error", "error": "Stable task ID and source digest required"})
        return json.dumps(await _bridge_request(user_id, "v3_build_work", {
            "action": "finish", "task_id": task_id, "expected_inputs_sha256": expected_inputs_sha256}))

    async def v3_source_read(task_id, path, offset=1, limit=200):
        """Read bounded lines from this open isolated V3 task source snapshot."""
        return json.dumps(await _bridge_request(user_id, "v3_source_read", {
            "task_id": task_id, "path": path, "offset": offset, "limit": limit}))

    async def v3_source_apply(task_id, request_id, patch, expected_inputs_sha256):
        """Apply a checked text patch to this task's isolated V3 source snapshot."""
        import re
        if (not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", task_id)
                or not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}", request_id)
                or not isinstance(patch, str) or not 0 < len(patch.encode()) <= 1024 * 1024
                or not isinstance(expected_inputs_sha256, str)
                or not re.fullmatch(r"[a-f0-9]{64}", expected_inputs_sha256)):
            return json.dumps({"status": "error", "error": "Stable IDs, bounded text patch and source digest required"})
        return json.dumps(await _bridge_request(user_id, "v3_source_apply", {
            "task_id": task_id, "request_id": request_id, "patch": patch,
            "expected_inputs_sha256": expected_inputs_sha256}))

    async def v3_source_receipt(task_id, request_id):
        """Inspect a durable source-apply receipt before any retry."""
        return json.dumps(await _bridge_request(user_id, "v3_source_receipt", {
            "task_id": task_id, "request_id": request_id}))

    return {"login_as_current_user_v3": login_as_current_user_v3,
            "v3_build_status": v3_build_status, "v3_build_work_status": v3_build_work_status,
            "v3_build_work": v3_build_work,
            "v3_build_finish": v3_build_finish, "v3_source_read": v3_source_read,
            "v3_source_apply": v3_source_apply,
            "v3_source_receipt": v3_source_receipt, "v3_worker_job": v3_worker_job}

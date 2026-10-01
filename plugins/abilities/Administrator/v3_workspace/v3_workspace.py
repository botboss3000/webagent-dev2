"""Dedicated private workspace adapter. Never changes live v3 source."""
from __future__ import annotations
import json
import os
import re
import uuid
from urllib.parse import urlsplit

ID = re.compile(r"^[a-zA-Z0-9_-]{1,80}$")
OPS = ("seed", "read", "review", "patch", "check", "test", "propose")
FIELDS = {"seed": set(), "read": {"path", "offset", "limit"}, "review": set(),
          "patch": {"patch", "expected_review_sha256"}, "check": {"timeout_seconds", "expected_review_sha256"},
          "test": {"timeout_seconds", "expected_review_sha256"},
          "propose": {"expected_review_sha256", "verification_summary"}}
TOOL_SCHEMAS = {
    "v3_workspace_status": {"type": "object", "properties": {}, "additionalProperties": False},
    "v3_workspace_receipt": {"type": "object", "properties": {"workspace_id": {"type": "string", "format": "uuid"}, "request_id": {"type": "string", "pattern": ID.pattern}}, "required": ["request_id"], "additionalProperties": False},
    "v3_workspace_action": {"type": "object", "properties": {
        "operation": {"type": "string", "enum": list(OPS)},
        "workspace_id": {"type": "string", "format": "uuid"},
        "request_id": {"type": "string", "pattern": ID.pattern},
        "arguments": {"type": "object", "properties": {
            "path": {"type": "string"}, "offset": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 2000},
            "patch": {"type": "string", "maxLength": 1048576},
            "expected_review_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            "timeout_seconds": {"type": "integer", "minimum": 30, "maximum": 600},
            "verification_summary": {"type": "string", "minLength": 1, "maxLength": 4000}},
            "additionalProperties": False}},
        "required": ["operation", "workspace_id", "request_id", "arguments"], "additionalProperties": False}}
DESTRUCTIVE = {"v3_workspace_action"}

def configuration(user_id):
    origin = os.environ.get("V3_WORKSPACE_WORKER_URL", "").rstrip("/")
    token = os.environ.get("V3_WORKSPACE_WORKER_CLIENT_TOKEN", "")
    caller = os.environ.get("V3_WORKSPACE_WORKER_CALLER_ID", "")
    if not origin or not token or not caller:
        raise ValueError("Configure V3_WORKSPACE_WORKER_URL, V3_WORKSPACE_WORKER_CLIENT_TOKEN and V3_WORKSPACE_WORKER_CALLER_ID")
    parsed = urlsplit(origin)
    if parsed.scheme not in ("https", "http") or not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
        raise ValueError("Workspace worker must be a configured private origin")
    if user_id != caller or not user_id: raise ValueError("Caller is not the mapped workspace administrator")
    if not 32 <= len(token) <= 256 or not all(33 <= ord(c) <= 126 for c in token): raise ValueError("Invalid workspace credential configuration")
    return origin, token

async def request(user_id, method, path, body=None):
    from app.auth.identity import _is_admin
    if not user_id or not await _is_admin(user_id):
        return {"status": "denied", "error": "Signed-in platform administrator required"}
    try: origin, token = configuration(user_id)
    except ValueError as exc: return {"status": "blocked", "error": str(exc)}
    import httpx
    try:
        async with httpx.AsyncClient(timeout=660, follow_redirects=False, trust_env=False) as client:
            response = await client.request(method, origin + path, headers={"Authorization": "Bearer " + token}, json=body)
            if response.status_code != 200:
                return {"status": "rejected", "http_status": response.status_code, "error": "Workspace service rejected request; inspect receipt before retrying"}
            if len(response.content) > 3 * 1024 * 1024: raise ValueError("Oversized response")
            # Never expose configured credential, even if trusted build output echoes it.
            return json.loads(response.text.replace(token, "[redacted]"))
    except (httpx.HTTPError, ValueError):
        return {"status": "uncertain" if method == "POST" else "error", "error": "Workspace request failed; use receipt lookup with the same request_id"}

def browser_read_path(kind, workspace_id="", request_id=""):
    """Only implemented read APIs; workspace_id is a v3 session ID here."""
    if kind == "status":
        return "/api/v3/auth/me"
    if str(uuid.UUID(workspace_id)) != workspace_id:
        raise ValueError("Canonical v3 session UUID required")
    base = "/api/v3/sessions/" + workspace_id
    if kind == "review":
        return base + "/source-review"
    if kind == "receipt" and ID.fullmatch(request_id):
        return base + "/promotions/" + request_id
    raise ValueError("Unsupported browser read operation")


async def browser_read(ctx, kind, workspace_id="", request_id=""):
    from plugins.abilities.Administrator.v3_build.v3_build import build_tools as login_tools
    from app.tools.browser import browser_action
    try:
        path = browser_read_path(kind, workspace_id, request_id)
    except (ValueError, TypeError, AttributeError) as exc:
        return {"status": "error", "error": str(exc)}
    login = json.loads(await login_tools(**ctx)["login_as_current_user_v3"](path="/app?tab=account"))
    if (login.get("status") != "ok" or login.get("application") != "v3"
            or login.get("backend") != "server"
            or login.get("verified", {}).get("http_status") != 200
            or login.get("verified", {}).get("user_id") != "admin"
            or login.get("verified", {}).get("is_admin") is not True):
        return {"status": "blocked", "backend": "v3_browser", "error": "Verified v3 login unavailable", "login": login}
    bs_id = login["browser_session_id"]
    try:
        js = """(async()=>{
            if(location.origin!==ORIGIN) return JSON.stringify({status:'error',error:'Origin mismatch'});
            const r=await fetch(PATH,{credentials:'same-origin',cache:'no-store',redirect:'error'});
            const text=await r.text();
            if(text.length>3145728) return JSON.stringify({status:'error',error:'Oversized response'});
            return JSON.stringify({http_status:r.status,data:JSON.parse(text)});
        })()""".replace("ORIGIN", json.dumps(login["origin"])).replace("PATH", json.dumps(path))
        result = await browser_action(bs_id, action="evaluate", js=js)
        from plugins.abilities.Administrator.diagnostics.diagnostics import _decode_browser_result
        value = _decode_browser_result(result)
        if not result.get("success") or value.get("http_status") != 200:
            return {"status": "error", "backend": "v3_browser", "http_status": value.get("http_status"), "error": "Authenticated v3 read failed"}
        if kind == "status":
            if value["data"].get("user_id") != "admin" or value["data"].get("is_admin") is not True:
                return {"status": "error", "error": "Identity mismatch"}
            return {"status": "ok", "backend": "v3_browser", "operations": ["review", "promotion_receipt"],
                    "mutations_supported": False, "note": "Mutations require v3 approval-controlled chat tools or the dedicated workspace worker"}
        return {"status": "failed" if value["data"].get("status") in ("failed", "error", "denied") else "ok", "backend": "v3_browser", "result": value["data"]}
    except Exception:
        return {"status": "error", "backend": "v3_browser", "error": "Browser read failed; no mutation attempted"}
    finally:
        await browser_action(bs_id, action="close")


def build_tools(*, user_id="", **ctx):
    bound = {**ctx, "user_id": user_id}
    def use_worker():
        # A configured worker never silently switches to another environment.
        return bool(os.environ.get("V3_WORKSPACE_WORKER_URL"))
    async def v3_workspace_status():
        """Check configured worker, otherwise reuse the existing v3 browser login."""
        result = await request(user_id, "GET", "/v1/status") if use_worker() else await browser_read(bound, "status")
        return json.dumps(result)
    async def v3_workspace_receipt(request_id, workspace_id=""):
        """Worker receipt, or existing v3 promotion receipt with a v3 session ID."""
        if not isinstance(request_id, str) or not ID.fullmatch(request_id):
            return json.dumps({"status": "error", "error": "Invalid request_id"})
        result = await request(user_id, "GET", "/v1/receipts/" + request_id) if use_worker() else await browser_read(bound, "receipt", workspace_id, request_id)
        return json.dumps(result)
    async def v3_workspace_action(operation, workspace_id, request_id, arguments):
        """Isolated worker operations, or authenticated v3 source review only."""
        try:
            if not isinstance(operation, str) or operation not in OPS or not isinstance(request_id, str) or not ID.fullmatch(request_id): raise ValueError("Invalid operation or request_id")
            if str(uuid.UUID(workspace_id)) != workspace_id: raise ValueError("Canonical workspace UUID required")
            if not isinstance(arguments, dict) or set(arguments) - FIELDS[operation]: raise ValueError("Arguments not allowed for this operation")
        except (ValueError, TypeError, AttributeError) as exc:
            return json.dumps({"status": "error", "error": str(exc)})
        if not use_worker():
            if operation != "review":
                return json.dumps({"status": "unsupported", "backend": "v3_browser", "error": "V3 has no direct workspace mutation/read-file API. Use its approval-controlled agent tools or configure the workspace worker. No operation performed."})
            return json.dumps(await browser_read(bound, "review", workspace_id))
        return json.dumps(await request(user_id, "POST", "/v1/actions", {
            "operation": operation, "workspace_id": workspace_id, "request_id": request_id, "arguments": arguments}))
    return {"v3_workspace_status": v3_workspace_status, "v3_workspace_receipt": v3_workspace_receipt, "v3_workspace_action": v3_workspace_action}

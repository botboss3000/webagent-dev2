"""Secret-safe, read-only discovery of Source Control repository access."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

SCHEMA = {
    "type": "object",
    "properties": {
        "verify_remote": {"type": "boolean", "default": True,
                          "description": "Verify GitHub repository metadata using the app vault credential; never download repository contents."}
    },
    "required": [],
}


def github_repository(raw: str) -> str:
    """Accept only GitHub owner/repo addresses; discard credentials and queries."""
    raw = (raw or "").strip()
    if raw.startswith("git@github.com:"):
        path = raw[len("git@github.com:"):]
    else:
        try:
            url = urlsplit(raw)
            if url.hostname != "github.com" or url.scheme not in {"https", "ssh"}:
                return ""
            path = url.path.lstrip("/")
        except ValueError:
            return ""
    path = path.removesuffix(".git").rstrip("/")
    return path if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", path) else ""


async def probe_repository(repository: str, token: str) -> dict:
    """Allowlisted metadata GET only. Never return headers, payloads or exceptions."""
    import httpx
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "WebAgent-repository-access"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
            response = await client.get(f"https://api.github.com/repos/{repository}", headers=headers)
        if response.status_code != 200:
            return {"status": "unverified", "http_status": response.status_code,
                    "reason": "GitHub metadata check failed; missing access, invalid credentials, rate limits or repository visibility may be responsible."}
        data = response.json()
        permissions = data.get("permissions") or {}
        return {"status": "verified", "http_status": 200,
                "authenticated": bool(token), "private": bool(data.get("private")),
                "read": True,
                "push": permissions.get("push") if isinstance(permissions.get("push"), bool) else None,
                "admin": permissions.get("admin") if isinstance(permissions.get("admin"), bool) else None,
                "note": "Repository role is not a guarantee of token write scope or branch-policy approval. No write was attempted."}
    except Exception:
        return {"status": "unverified", "reason": "GitHub metadata request failed; no credentials or response payload disclosed."}


async def repository_access(*, verify_remote: bool = True, user_id: str = "", agent_id: str = "") -> dict:
    from app.db import get_app_db
    from app import git_repos
    # Installation-shared credentials must not be probed on behalf of guests.
    db = get_app_db()
    if not user_id or not await db.is_user_admin(user_id):
        return {"status": "blocked", "reason": "Installation administrator access is required for shared repository credentials."}
    row = await db.auth_element_get("admin", "deploy_github_token", "default")
    token = str((row or {}).get("secret_ref") or "").strip()
    builtin = git_repos.BUILTIN_ID
    repos = git_repos.list_repos()
    output = []
    for repo in repos:
        name = github_repository(repo.get("remote_url", ""))
        is_builtin = repo.get("id") == builtin
        item = {"id": repo["id"], "repository": name or None,
                "remote_url": f"https://github.com/{name}.git" if name else None,
                "agent_target": is_builtin, "selected_in_source_control": bool(repo.get("active")),
                "credential": {"source": "app_db_vault" if is_builtin else "source_control_registry",
                               "configured": bool(token) if is_builtin else bool(repo.get("has_token"))},
                "remote_access": {"status": "not_checked"}}
        if verify_remote and is_builtin and name:
            item["remote_access"] = await probe_repository(name, token)
        output.append(item)
    # Describe actual loaded tool availability, not a blanket permission grant.
    tools = {}
    if agent_id:
        from app.tools.loader import load_tools
        from app.tools.tool_defaults import resolve_permission
        from app.admin.integrations import get_global_tool_defaults
        from app.db import get_db
        agent = await get_db().get_agent_by_id(agent_id)
        loaded = await load_tools(user_id, agent_id=agent_id, gate_caller_access=True,
                                  allowed_tools=(agent or {}).get("allowed_tools"))
        defaults = await get_global_tool_defaults()
        for name in ("git_tool", "commit_and_push", "resolve_conflict", "run_command"):
            info = loaded.get(name)
            permission = resolve_permission(name, agent, defaults)
            tools[name] = {"available": info is not None and permission != "deny",
                           "permission": permission,
                           "requires_confirmation": bool(info and info.requires_confirmation)}
    return {"status": "ok", "observed_at": datetime.now(timezone.utc).isoformat(),
            "repositories": output, "tools": tools,
            "boundaries": ["Git Control agent tools target the built-in project, not the UI selection.",
                           "Credentials are resolved server-side; no token or token fragments are returned.",
                           "Configured credentials are not verified access. No clone, download, fetch, commit or push was performed.",
                           "GitHub clone/download support is not provided by this discovery tool; use separately authorized tools.",
                           "Tool permissions and the current execution mode still govern every operation."]}

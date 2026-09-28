"""
Dynamic tool registry for WebAgent agent.

Replaces the hardcoded tools with database-backed dynamic loading.
Provides the real implementation of create_tool (writes Python code to the DB)
and tool rating/scoring utilities.
"""

import json
import logging
import re
from typing import Dict, Any, List, Optional

from app.db import get_app_db, get_db

logger = logging.getLogger(__name__)


# ── Tool code safety scanner ─────────────────────────────────────────────────
# Prevents create_tool from injecting code that touches the agent's own
# codebase (filesystem, shell, DB, code execution). Only external-facing
# tools (HTTP APIs, data processing, etc.) are allowed.

BLOCKED_IMPORTS = [
    "os",
    "subprocess",
    "shutil",
    "pathlib",
    "sqlite3",
    "builtins",
    "importlib",
    "aiofiles",
    "io",
    "tempfile",
    "fileinput",
    "filecmp",
    "zipfile",
    "tarfile",
    "gzip",
    "bz2",
    "lzma",
]

# Regex: matches `import os`, `import   os`, `from os import`, `from   os import`
_IMPORT_RE = re.compile(
    r"""(?:^|[\n;])\s*(?:import|from)\s+([a-zA-Z0-9_.]+)""",
    re.MULTILINE,
)

# Inline/call patterns: os., subprocess., shutil.rmtree, open(, exec(, eval(
_CALL_PATTERNS = [
    r"""\bos\.""",
    r"""\bsubprocess\.""",
    r"""\bshutil\.""",
    r"""\bpathlib\.""",
    r"""\bsqlite3\.""",
    r"""\bbuiltins\.""",
    r"""\bimportlib\.""",
    r"""\baiofiles\.""",
    r"""\bopen\s*\(""",
    r"""\bexec\s*\(""",
    r"""\beval\s*\(""",
    r"""\bcompile\s*\(""",
    r"""__import__\s*\(""",
    r"""\bget_db\s*\(""",
    r"""\bget_raw_client\s*\(""",
    r"""\b_get_conn\s*\(""",
]
_CALL_RE = re.compile("|".join(_CALL_PATTERNS))


def _check_tool_code_safety(code: str) -> Optional[str]:
    """
    Scan tool code for dangerous imports and patterns.
    Returns error message string if unsafe, None if safe.
    """
    # Check imports
    for match in _IMPORT_RE.finditer(code):
        full_mod = match.group(1)
        mod = full_mod.split(".")[0]  # top-level module name
        if mod in BLOCKED_IMPORTS or full_mod == "app.db" or full_mod.startswith("app.db."):
            return (
                f"Unsafe import rejected: '{match.group(0).strip()}' "
                f"(module '{mod}' provides filesystem/shell/DB access). "
                f"create_tool is for external tools only (HTTP APIs, data processing). "
                f"Allowed imports exclude: os, subprocess, shutil, pathlib, sqlite3, "
                f"builtins, importlib, aiofiles, io, tempfile, and archive modules."
            )

    # Check call patterns (e.g. `os.`, `open(`, `exec(`)
    call_match = _CALL_RE.search(code)
    if call_match:
        return (
            f"Unsafe pattern rejected: '{call_match.group().strip()}' "
            f"provides filesystem/shell/DB access. "
            f"create_tool is for external tools only."
        )

    return None


# ── Built-in tool: create_tool ────────────────────────────────────────────────

# ── Valid loop node IDs for stage metadata validation ────────────────────────────────────────────
VALID_NODE_IDS = {
    # Main agent loop
    "user_input", "load_context", "memory_search", "build_prompt",
    "llm_call", "validate_tools", "guardrails", "execute_tools",
    "check_continue", "final_response", "memory_save",
    # Optimizer loop
    "opt_collect", "opt_analyze", "opt_propose", "opt_validate", "opt_apply",
}


async def create_tool(
    name: str,
    description: str,
    parameters: dict,
    code: str,
    stages: list,
    destructive: bool = False,
    agent_types: Optional[List[str]] = None,
    user_id: str = "",
) -> dict:
    """
    [REQUIRES CONFIRMATION] Create or update a tool in the tools table.

    Stores the full executable code directly. If a tool with the same name
    exists for this user, it is replaced (upsert by name + created_by).

    ⚠ You MUST ask the user to review the code and get explicit approval before calling this.

    Args:
        name: Tool identifier (e.g. 'check_email')
        description: What the tool does (shown to model)
        parameters: JSON Schema describing tool inputs
        code: Full Python async function code
        stages: Non-empty list of loop node IDs where this tool operates.
            Valid values: user_input, load_context, memory_search, build_prompt,
            llm_call, validate_tools, guardrails, execute_tools, check_continue,
            final_response, memory_save, opt_collect, opt_analyze, opt_propose,
            opt_validate, opt_apply.
            Most tools should include 'execute_tools'. Memory tools also include
            'memory_search' and/or 'memory_save'.
        destructive: True if this tool writes, deletes, or has irreversible side effects.
            Destructive tools display a warning badge in the loop visualizer.
        agent_types: List of agent type names that can use this tool (e.g. ['default']).
            Empty list or omitted means all agent types.
        user_id: Creator's user ID (injected by loader)

    Returns:
        dict with tool name, id, status
    """
    if not user_id:
        return {"status": "error", "message": "user_id is required"}

    # ── Validate stages (required, non-empty, valid node IDs) ──
    if not stages:
        return {
            "status": "error",
            "message": (
                "stages is required and must be a non-empty list of loop node IDs. "
                f"Valid node IDs: {', '.join(sorted(VALID_NODE_IDS))}. "
                "Most tools should include 'execute_tools'. "
                "Memory tools also include 'memory_search' and/or 'memory_save'."
            ),
        }
    invalid = [s for s in stages if s not in VALID_NODE_IDS]
    if invalid:
        return {
            "status": "error",
            "message": (
                f"Invalid stage IDs: {', '.join(invalid)}. "
                f"Valid node IDs: {', '.join(sorted(VALID_NODE_IDS))}."
            ),
        }

    # Safety scan — reject code that touches the agent's codebase
    safety_error = _check_tool_code_safety(code)
    if safety_error:
        logger.warning("Tool '%s' rejected by safety scanner: %s", name, safety_error[:80])
        return {"status": "blocked", "tool_name": name, "message": safety_error}

    # Dynamic tool definitions are installation-wide app-plane records.
    client = get_app_db().get_raw_client()

    # Serialise metadata
    params_json = json.dumps(parameters) if isinstance(parameters, dict) else parameters
    stages_json = json.dumps(stages)
    agent_types_json = json.dumps(agent_types or [])
    destructive_int = 1 if destructive else 0

    # Check if tool already exists for this user
    existing = (
        client.table("tools")
        .select("id")
        .eq("name", name)
        .eq("created_by", user_id)
        .limit(1)
        .execute()
    )

    if existing.data:
        # Update existing tool
        tool_id = existing.data[0]["id"]
        client.table("tools").update({
            "code": code,
            "description": description,
            "parameters": params_json,
            "stages": stages_json,
            "destructive": destructive_int,
            "agent_types": agent_types_json,
            "updated_at": "now()",
        }).eq("id", tool_id).execute()
        logger.info(f"Updated tool {name} for user {user_id}")
        return {
            "status": "success",
            "tool_name": name,
            "tool_id": tool_id,
            "message": f"Tool '{name}' updated. You can now call it.",
        }
    else:
        # Insert new tool. The `tools` table's id is a TEXT PRIMARY KEY with no
        # DB default, and the local insert proxy only auto-fills `id` when the
        # key is present-but-falsy — so we must supply it, or the row stores a
        # NULL id and reading back `created["id"]` raises KeyError (surfaced to
        # the agent as a bogus validation_error on the very first create).
        import uuid as _uuid
        row = {
            "id": str(_uuid.uuid4()),
            "name": name,
            "code": code,
            "description": description,
            "parameters": params_json,
            "language": "python",
            "status": "active",
            "created_by": user_id,
            "stages": stages_json,
            "destructive": destructive_int,
            "agent_types": agent_types_json,
        }
        resp = client.table("tools").insert(row).execute()

        if resp.data and len(resp.data) > 0:
            created = resp.data[0]
            logger.info(f"Created tool {name} for user {user_id}")
            return {
                "status": "success",
                "tool_name": name,
                "tool_id": created["id"],
                "message": f"Tool '{name}' created. You can now call it.",
            }
        else:
            logger.error(f"Failed to insert tool {name}")
            return {"status": "error", "message": f"Failed to create tool '{name}'"}


async def browser_open(url: str) -> str:
    """
    Open a browser to a given URL, typically for authentication flows.

    Args:
        url: The URL to open
    """
    import webbrowser
    webbrowser.open(url)
    return f"Browser opened to {url}"


# ── Tool rating / scoring ─────────────────────────────────────────────────────


async def get_tool_rating(tool_name: str, user_id: Optional[str] = None) -> dict:
    """
    Get the success rating for a tool based on interactions metadata.

    Rating = positive / (positive + negative) where:
      - positive = successful executions
      - negative = failed executions

    Args:
        tool_name: The tool name to score
        user_id: Optional — if provided, scoped to that user

    Returns:
        dict with tool_name, total, positive, negative, rating (0-100)
    """
    import json
    client = get_db().get_raw_client()
    query = (
        client.table("interactions")
        .select("metadata")
        .eq("role", "tool")
        .eq("tool_name", tool_name)
    )
    if user_id:
        query = query.eq("user_id", user_id)

    try:
        resp = query.execute()
        rows = resp.data or []
        total = 0
        positive = 0
        for r in rows:
            try:
                meta = json.loads(r["metadata"]) if r.get("metadata") else {}
                total += 1
                if meta.get("success"):
                    positive += 1
            except (json.JSONDecodeError, TypeError):
                pass
        negative = total - positive
        rating = round(100.0 * positive / total, 1) if total > 0 else None

        return {
            "tool_name": tool_name,
            "total_executions": total,
            "positive": positive,
            "negative": negative,
            "rating": rating,
        }
    except Exception as e:
        logger.error(f"Error getting tool rating for {tool_name}: {e}")
        return {"tool_name": tool_name, "total_executions": 0, "positive": 0, "negative": 0, "rating": None}


async def get_top_tools(user_id: Optional[str] = None, min_executions: int = 5, limit: int = 10) -> List[dict]:
    """
    Get the highest-rated tools.

    Args:
        user_id: Optional user scope
        min_executions: Minimum number of executions to qualify
        limit: Max results

    Returns:
        List of {tool_name, total_executions, positive, negative, rating}
    """
    import json
    client = get_db().get_raw_client()
    query = client.table("interactions").select("tool_name, metadata").eq("role", "tool")
    if user_id:
        query = query.eq("user_id", user_id)

    try:
        resp = query.execute()
        rows = resp.data or []

        stats: Dict[str, dict] = {}
        for r in rows:
            name = r.get("tool_name", "?")
            if not name:
                continue
            if name not in stats:
                stats[name] = {"tool_name": name, "total": 0, "positive": 0}
            stats[name]["total"] += 1
            try:
                meta = json.loads(r["metadata"]) if r.get("metadata") else {}
                if meta.get("success"):
                    stats[name]["positive"] += 1
            except (json.JSONDecodeError, TypeError):
                pass

        scored = []
        for s in stats.values():
            if s["total"] < min_executions:
                continue
            negative = s["total"] - s["positive"]
            rating = round(100.0 * s["positive"] / s["total"], 1)
            scored.append({
                "tool_name": s["tool_name"],
                "total_executions": s["total"],
                "positive": s["positive"],
                "negative": negative,
                "rating": rating,
            })

        scored.sort(key=lambda x: x["rating"], reverse=True)
        return scored[:limit]
    except Exception as e:
        logger.error(f"Error getting top tools: {e}")
        return []


# ── Tool listing ──────────────────────────────────────────────────────────────


async def list_user_tools(user_id: str) -> List[Dict[str, Any]]:
    """List all active tools for a user."""
    client = get_app_db().get_raw_client()
    try:
        resp = (
            client.table("tools")
            .select("id, name, description, parameters, language, status, created_at, updated_at")
            .eq("created_by", user_id)
            .eq("status", "active")
            .order("created_at", desc=True)
            .execute()
        )
        return resp.data or []
    except Exception as e:
        logger.error(f"Error listing tools for {user_id}: {e}")
        return []

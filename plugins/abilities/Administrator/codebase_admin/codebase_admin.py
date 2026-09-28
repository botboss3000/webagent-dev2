"""Codebase Admin ability — SELF-CONTAINED drop-in.

The full privileged source suite (read/write/edit/delete files, run commands,
restart, search) plus a ``db_query`` context tool. The source suite comes
from the shared admin library (``plugins/admin/source_tools.py`` via
``inject_source_tools``); version control is NOT part of it — the git tools are
the separate Git Control ability, which defines them in its own file. The
``db_query`` handler now lives IN THIS FILE
(relocated out of core so the ability is self-contained) as the module-level
``_db_query_core``. The shared ``plugins/admin/adapter.extract_injected`` runs
the source injector against a throwaway dict and adapts its ToolInfo objects
into the (handlers, schemas, destructive) triple the generic drop-in loader
contract wants, so schemas/flags never drift.

Discovered generically by core (see app/tools/loader.py "Self-contained ability
tools"): build_tools() returns its handlers and the loader reads the
module-level TOOL_SCHEMAS / DESTRUCTIVE populated below AFTER the call.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Populated inside build_tools() from the injected ToolInfo objects + the
# db_query schema (kept here so the loader, which reads these AFTER calling
# build_tools, sees the right schemas/flags without them drifting).
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


# ── db_query (prompt slots on the user's agent) ──────────────────────────────
# Relocated VERBATIM from app/tools/core_tools.py. It reads/edits the admin-base
# prompt slots on the user's assigned agent. The _db_query_wrapper in
# build_tools() forwards the LLM-facing context_type/context_id/title/tags +
# user_id into this handler, which maps the legacy names via **_legacy.

async def _db_query_core(
    action: str,
    slot_name: Optional[str] = None,
    content: Optional[str] = None,
    lock: Optional[bool] = None,
    merge_mode: Optional[str] = None,
    order_index: Optional[int] = None,
    user_id: str = "",
    **_legacy,
) -> str:
    """
    Read and edit the admin-base prompt slots on the user's assigned agent.

    Actions:
      list   → list all admin-base slots
      get    → get one slot by slot_name
      insert → create a new slot (slot_name + content required)
      update → replace a slot's content (slot_name + content required)
      delete → delete a slot by slot_name

    Lock + merge_mode + order_index are optional on insert/update.
    """
    try:
        from app.db import get_db
        db = get_db()
        agent = await db.get_agent_for_user(user_id)
        if not agent:
            return json.dumps({"status": "error", "message": "No agent assigned for this user."})
        agent_id = agent["id"]
        # Tolerate legacy param names from older callers.
        slot_name = slot_name or _legacy.get("context_id") or _legacy.get("context_type") or _legacy.get("title")

        if action == "list":
            slots = await db.list_slots(agent_id)
            return json.dumps({"status": "ok", "count": len(slots), "slots": slots}, default=str)

        elif action == "get":
            if not slot_name:
                return json.dumps({"status": "error", "message": "slot_name required for get action"})
            slots = await db.list_slots(agent_id)
            slot = next((s for s in slots if s["slot_name"] == slot_name), None)
            if not slot:
                return json.dumps({"status": "error", "message": f"Slot '{slot_name}' not found."})
            return json.dumps({"status": "ok", "slot": slot}, default=str)

        elif action in ("insert", "update"):
            if not slot_name or content is None:
                return json.dumps({"status": "error", "message": "slot_name and content required"})
            existing = await db.list_slots(agent_id)
            current = next((s for s in existing if s["slot_name"] == slot_name), None)
            resolved_order = order_index if order_index is not None else (
                current["order_index"] if current
                else (max((s["order_index"] or 0) for s in existing), 0)[0] + 10 if existing else 10
            )
            resolved_lock = lock if lock is not None else (current["lock"] if current else False)
            resolved_mode = merge_mode if merge_mode is not None else (current.get("merge_mode") if current else "replace")
            await db.upsert_slot(
                agent_id=agent_id,
                slot_name=slot_name,
                order_index=int(resolved_order),
                lock=bool(resolved_lock),
                merge_mode=resolved_mode,
                content=content,
                updated_by=f"tool:{user_id}",
            )
            return json.dumps({"status": "ok", "slot_name": slot_name})

        elif action == "delete":
            if not slot_name:
                return json.dumps({"status": "error", "message": "slot_name required for delete"})
            n = await db.delete_slot(agent_id, slot_name)
            return json.dumps({"status": "ok", "slot_name": slot_name, "deleted_rows": n})

        else:
            return json.dumps({"status": "error", "message": f"Unknown action '{action}'. Use: list, get, insert, update, delete."})

    except Exception as e:
        logger.error("db_query failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# db_query's schema, copied VERBATIM from app/tools/loader.py (the codebase_admin
# block that adds db_query). It confirm-gates (see build_tools — its writes edit
# the user's agent prompt slots).
_DB_QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["list", "get", "insert", "update", "delete"],
            "description": "Action to perform: list, get, insert, update, or delete (delete clears content)",
        },
        "context_type": {"type": "string", "description": "Document type (agent, user, skills, tools, tasks, memory, project, jobs). This reads/writes the agent's prompt slot documents — it is NOT a SQL interface; for raw DB queries use run_python with sqlite3."},
        "context_id": {"type": "string", "description": "Document ID — for get/update/delete actions"},
        "title": {"type": "string", "description": "Title — for insert action"},
        "content": {"type": "string", "description": "Content body — for insert/update actions"},
        "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags — for insert action"},
    },
    "required": ["action"],
}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Return {tool_name: handler} for the privileged source suite + db_query.

    The source suite comes from the shared admin library via the adapter (files +
    shell + search — no git; version control is the separate Git Control ability);
    db_query is lazily wrapped from core. Imports stay LAZY so scanning FEATURE
    is cheap.
    """
    from plugins.admin.adapter import extract_injected
    from plugins.admin.source_tools import inject_source_tools

    handlers, schemas, destructive = extract_injected(inject_source_tools, user_id)

    # ── db_query: wraps the module-level _db_query_core (relocated from core).
    # The wrapper closure + schema are preserved VERBATIM from the loader's
    # codebase_admin block (it forwards context_type/context_id/title/tags +
    # user_id into the handler).
    from typing import List, Optional

    async def _db_query_wrapper(
        action: str,
        context_type: "Optional[str]" = None,
        context_id: "Optional[str]" = None,
        title: "Optional[str]" = None,
        content: "Optional[str]" = None,
        tags: "Optional[List[str]]" = None,
    ):
        return await _db_query_core(
            action=action,
            context_type=context_type,
            context_id=context_id,
            title=title,
            content=content,
            tags=tags,
            user_id=user_id,
        )

    handlers["db_query"] = _db_query_wrapper
    schemas["db_query"] = dict(_DB_QUERY_SCHEMA)
    # db_query writes (insert/update/delete) edit the admin-base prompt slots on
    # the user's agent — a real mutation — so it confirm-gates in Ask/Plan mode.
    # (Its read actions list/get also pause; db_query is rare enough that a
    # per-action read exemption isn't worth the loop special-casing.)
    destructive.add("db_query")

    # Publish schemas/flags for the loader to read AFTER this call.
    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(schemas)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(destructive)
    return handlers

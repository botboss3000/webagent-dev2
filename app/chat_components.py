"""Persisted declarative components shown in chat sessions."""
from __future__ import annotations

import json
import logging
import uuid

logger = logging.getLogger(__name__)

ALLOWED_TYPES = {"todo_list", "status", "choice", "approval", "form", "scratchpad"}
ALLOWED_PLACEMENTS = {"inline", "hover", "sticky"}
RETIRED_COMPONENT_IDS = {"plan-checklist"}

# Canonical short display names per type — the fallback when the agent passes
# no title or one longer than the 3-word cap. Keeps the panel head and the
# session-dropdown chips terse ("Checklist", not "Persist agent-panel UI state
# per session Checklist").
TYPE_SHORT_NAMES = {
    "todo_list": "Checklist",
    "status": "Overview",
    "choice": "Choice",
    "approval": "Approval",
    "form": "Form",
    "scratchpad": "Scratchpad",
}


def short_title(title, fallback):
    """Cap an agent-supplied chat-element title at 3 words; empty or longer
    titles fall back to the element type's short name."""
    words = [w for w in str(title or "").split() if w]
    if not words or len(words) > 3:
        return fallback
    return " ".join(words[:3])


def validate_spec(spec: dict, *, allow_retired: bool = False) -> dict:
    if not isinstance(spec, dict):
        raise ValueError("Component must be an object.")
    component_id = str(spec.get("id") or uuid.uuid4())
    if component_id in RETIRED_COMPONENT_IDS and not allow_retired:
        raise ValueError("plan-checklist is retired; use canonical Task Progress steps")
    kind = str(spec.get("type") or "").strip()
    placement = str(spec.get("placement") or "inline").strip()
    if kind not in ALLOWED_TYPES:
        raise ValueError("Unsupported component type.")
    if placement not in ALLOWED_PLACEMENTS:
        raise ValueError("Unsupported component placement.")
    data = spec.get("data") if isinstance(spec.get("data"), dict) else {}
    if kind == "todo_list":
        items = data.get("items") if isinstance(data.get("items"), list) else []
        # An item's text key is `label` (the renderer reads item.label); `text` is
        # accepted as an alias because a model plausibly writes the sibling row
        # key it knows from other components, and a dropped item would silently
        # render an EMPTY checklist instead of the declaration the agent made.
        data["items"] = [{"id": str(x.get("id") or uuid.uuid4()), "label": str(x.get("label") or x.get("text") or "")[:240], "done": bool(x.get("done"))}
                         for x in items[:100] if isinstance(x, dict) and str(x.get("label") or x.get("text") or "").strip()]
    elif kind == "status":
        sections = data.get("sections") if isinstance(data.get("sections"), list) else []
        data["sections"] = [{"label": str(s.get("label") or "")[:80], "value": str(s.get("value") or "")[:120],
                             "status": s.get("status") if s.get("status") in ("good", "warning", "danger") else None}
                            for s in sections[:20] if isinstance(s, dict)]
    elif kind == "choice":
        options = data.get("options") if isinstance(data.get("options"), list) else []
        data["options"] = [{"id": str(o.get("id") or uuid.uuid4()), "label": str(o.get("label") or "")[:240]}
                           for o in options[:20] if isinstance(o, dict)]
        if "selected" in data and not isinstance(data.get("selected"), str):
            data.pop("selected", None)
    elif kind == "form":
        fields = data.get("fields") if isinstance(data.get("fields"), list) else []
        data["fields"] = [{"name": str(f.get("name") or "")[:40], "label": str(f.get("label") or "")[:120],
                           "type": str(f.get("type") or "text")[:20],
                           "required": bool(f.get("required")),
                           "multiline": bool(f.get("multiline")),
                           "options": f.get("options") if isinstance(f.get("options"), list) else None}
                          for f in fields[:20] if isinstance(f, dict) and str(f.get("name") or "").strip()]
        if "submitted" in data and not isinstance(data.get("submitted"), dict):
            data.pop("submitted", None)
    elif kind == "approval":
        if "status" in data and str(data.get("status") or "") not in ("approved", "rejected"):
            data.pop("status", None)
    elif kind == "scratchpad":
        # SCRATCHPAD-ROWS: user-owned notes, one row per idea. Blank text is KEPT
        # (a freshly added row is empty until the user types); `shared` gates
        # whether the row is injected into the agent's context.
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        data["rows"] = [{"id": str(r.get("id") or uuid.uuid4()),
                         "text": str(r.get("text") or "")[:2000],
                         "shared": bool(r.get("shared"))}
                        for r in rows[:50] if isinstance(r, dict)]

    return {"id": component_id, "type": kind, "placement": placement,
            "title": short_title(spec.get("title"), TYPE_SHORT_NAMES.get(kind, kind.replace("_", " ").title())),
            "data": data,
            "lifecycle": spec.get("lifecycle") if isinstance(spec.get("lifecycle"), dict) else {}}


def _is_completed(spec: dict) -> bool:
    """A component counts as completed once its purpose is fulfilled: every
    todo item done, a choice selected, a form submitted, an approval resolved.
    Completed components are excluded from the active set (list_components)
    exactly like deleted ones — the row stays in the DB as the audit trail."""
    kind = spec.get("type")
    data = spec.get("data") or {}
    if kind == "todo_list":
        items = data.get("items") or []
        return bool(items) and all(bool(i.get("done")) for i in items)
    if kind == "choice":
        return bool(data.get("selected"))
    if kind == "form":
        return data.get("submitted") is not None
    if kind == "approval":
        return data.get("status") in ("approved", "rejected")
    return False


async def _load_components(user_id: str, session_id: str) -> list[dict]:
    from app.db import get_db
    latest = {}
    # First-seen timestamp per component id. Component state is append-only
    # (each write inserts an interaction), so the EARLIEST row for an id is the
    # authoritative creation time — when the tab first appeared. It backs the
    # header tab's live age counter; `updated_at` (the latest row) is kept for
    # session-tab ordering and stays separate.
    first_seen: dict[str, str] = {}
    for row in await get_db().fetch_interactions(user_id, session_id):
        if getattr(row, "tool_name", None) != "chat_component":
            continue
        try:
            payload = json.loads(getattr(row, "content", "") or "{}")
            comp = payload.get("component") or {}
            cid = str(comp.get("id", ""))
            if comp.get("deleted"):
                latest.pop(cid, None)
                # A re-presented id after a tombstone is a fresh component, so
                # drop the stale creation time too.
                first_seen.pop(cid, None)
                continue
            spec = validate_spec(comp, allow_retired=True)
            # Session-tab ordering needs the last component write, not the time
            # this list happened to be fetched.  Component state is append-only,
            # so the interaction timestamp is its authoritative update time.
            created_at = getattr(row, "created_at", None)
            if created_at is not None:
                stamp = (
                    created_at.isoformat() if hasattr(created_at, "isoformat")
                    else str(created_at)
                )
                spec["updated_at"] = stamp
                first_seen.setdefault(spec["id"], stamp)
            latest[spec["id"]] = spec
        except (ValueError, TypeError, json.JSONDecodeError):
            continue
    for cid, stamp in first_seen.items():
        if cid in latest:
            latest[cid]["created_at"] = stamp
    return list(latest.values())


async def list_components(user_id: str, session_id: str) -> list[dict]:
    """Load active components and lazily fold a legacy plan checklist into Task Progress."""
    from app.db import get_db
    db = get_db()
    components = await _load_components(user_id, session_id)
    legacy = next((component for component in components
                   if component.get("id") == "plan-checklist"), None)
    if legacy:
        try:
            from app.agent.task_state import (
                current_state, merge_execution_steps, progress_component,
            )
            state = current_state(db, session_id)
            items = ((legacy.get("data") or {}).get("items") or [])
            if state:
                saved = (merge_execution_steps(
                    db, session_id, state["turn_id"], items, replace=False,
                ) if items else state)
                await save_component(user_id, session_id, progress_component(saved))
                await db.insert_interaction(
                    user_id=user_id, session_id=session_id, role="tool",
                    content=json.dumps({"component": {
                        "id": "plan-checklist", "deleted": True,
                    }}),
                    tool_name="chat_component",
                    metadata=json.dumps({
                        "chat_component": True,
                        "component_id": "plan-checklist",
                        "deleted": True,
                    }),
                    source="chat_component",
                )
                components = await _load_components(user_id, session_id)
        except Exception as exc:
            logger.debug("legacy plan checklist migration unavailable: %s", exc)
    return [component for component in components if not _is_completed(component)]


async def save_component(user_id: str, session_id: str, spec: dict) -> dict:
    from app.db import get_db
    clean = validate_spec(spec)
    await get_db().insert_interaction(user_id=user_id, session_id=session_id, role="tool",
        content=json.dumps({"component": clean}), tool_name="chat_component",
        metadata=json.dumps({"chat_component": True, "component_id": clean["id"]}), source="chat_component")
    return clean


async def apply_action(user_id: str, session_id: str, component_id: str, action: str, payload: dict) -> dict:
    current = next((x for x in await list_components(user_id, session_id) if x["id"] == component_id), None)
    if not current:
        raise LookupError("Component not found.")

    kind = current["type"]
    data = current["data"]

    if kind == "todo_list" and action == "toggle_item":
        item_id = str((payload or {}).get("item_id") or "")
        for item in data.get("items", []):
            if item["id"] == item_id:
                item["done"] = bool((payload or {}).get("done"))
                return await save_component(user_id, session_id, current)
        raise ValueError("To-do item not found.")

    if kind == "choice" and action == "select":
        option_id = str((payload or {}).get("option_id") or "")
        valid = any(o["id"] == option_id for o in data.get("options", []))
        if not valid:
            raise ValueError("Option not found.")
        data["selected"] = option_id
        return await save_component(user_id, session_id, current)

    if kind == "form" and action == "submit":
        values = (payload or {}).get("values") if isinstance((payload or {}).get("values"), dict) else {}
        data["submitted"] = values
        return await save_component(user_id, session_id, current)

    if kind == "approval" and action in ("approve", "reject"):
        data["status"] = "approved" if action == "approve" else "rejected"
        return await save_component(user_id, session_id, current)

    if kind == "scratchpad":
        # A scratchpad is user-authored: the client edits one row at a time and
        # every action rewrites the whole row set, so the list can never drift.
        rows = data.get("rows") if isinstance(data.get("rows"), list) else []
        payload = payload or {}
        if action == "add_row":
            rows.append({"id": str(uuid.uuid4()),
                         "text": str(payload.get("text") or "")[:2000],
                         "shared": bool(payload.get("shared"))})
            data["rows"] = rows[:50]
            return await save_component(user_id, session_id, current)
        row_id = str(payload.get("row_id") or "")
        target = next((r for r in rows if str(r.get("id")) == row_id), None)
        if target is None:
            raise ValueError("Scratchpad row not found.")
        if action == "update_row":
            target["text"] = str(payload.get("text") or "")[:2000]
        elif action == "delete_row":
            rows.remove(target)
        elif action == "toggle_share":
            target["shared"] = bool(payload.get("shared"))
        else:
            raise ValueError("Unsupported scratchpad action.")
        data["rows"] = rows
        return await save_component(user_id, session_id, current)

async def delete_component(user_id: str, session_id: str, component_id: str):
    """Soft-delete by writing a tombstone record."""
    from app.db import get_db
    # Verify it exists
    components = await list_components(user_id, session_id)
    if not any(c["id"] == component_id for c in components):
        raise LookupError("Component not found.")
    await get_db().insert_interaction(user_id=user_id, session_id=session_id, role="tool",
        content=json.dumps({"component": {"id": component_id, "deleted": True}}),
        tool_name="chat_component",
        metadata=json.dumps({"chat_component": True, "component_id": component_id, "deleted": True}),
        source="chat_component")


def sanitize_panel_ui(ui: dict) -> dict:
    """Whitelist the agent-panel state the client may persist per session.

    The full view is the panel's ONLY open state (CHAT-COMPONENT-FULL-VIEW), so
    there is no drawer geometry left to keep: only the collapsed flag
    ("minimized") and the active component tab survive. Unknown keys — including
    the retired expanded/height/width — are dropped."""
    if not isinstance(ui, dict):
        return {}
    clean: dict = {}
    if isinstance(ui.get("minimized"), bool):
        clean["minimized"] = ui["minimized"]
    active = ui.get("active")
    if isinstance(active, str) and active:
        clean["active"] = active[:120]
    return clean


async def get_panel_ui(user_id: str, session_id: str) -> dict:
    """Return the session's saved agent-panel layout state (empty dict if none)."""
    from app.db import get_db
    ui = await get_db().get_session_component_ui(session_id)
    return sanitize_panel_ui(ui) if isinstance(ui, dict) else {}


async def save_panel_ui(user_id: str, session_id: str, ui: dict) -> dict:
    """Persist the session's agent-panel layout state. Returns the clean spec."""
    from app.db import get_db
    clean = sanitize_panel_ui(ui)
    await get_db().set_session_component_ui(session_id, clean)
    return clean

"""
Admin endpoints for database status + agent template lifecycle.

- Stats:           /admin/db/mode (GET, returns table row counts)
- Templates panel: /admin/db/templates (GET, POST seed, POST seed-force)
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from app.db import get_mode, get_db_stats, get_db
import logging

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/db", tags=["admin-db"])


class ModeStatusResponse(BaseModel):
    mode: str
    backend: str
    tables: dict
    db_path: str = ""


@router.get("/mode", response_model=ModeStatusResponse)
async def get_current_mode():
    """
    Get the current database mode and storage statistics.

    Returns:
        ModeStatusResponse with mode, backend type, table row counts
    """
    try:
        stats = await get_db_stats()
        return ModeStatusResponse(**stats)
    except Exception as e:
        logger.error(f"Error getting db mode status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ── Agent Prompt Templates panel ────────────────────────────────────────────
#
# Provides admin-side visibility + control over the JSON → agent_templates +
# agent_prompt_templates seed pipeline.
#
# Read:  GET  /admin/db/templates           — list templates + per-slot version/source
# Write: POST /admin/db/templates/seed      — re-seed (manifest-gated, respects admin)
# Write: POST /admin/db/templates/seed-force— re-seed overriding admin edits (destructive)


@router.get("/templates")
async def list_template_state():
    """
    Return per-template metadata: id, slot count, version range, admin-edited slot count.

    Also returns the currently stored manifest hash so the UI can show whether
    the DB is in sync with the on-disk JSON files. Frontend can compare against
    a freshly-computed hash from the JSON manifest endpoint (below) to decide
    whether to nudge the admin to re-seed.
    """
    try:
        from app.context.md_seeder import compute_agent_manifest_hash
        db = get_db()
        raw = db.get_raw_client()

        # Pull template config rows.
        tpl_res = raw.table("agent_templates").select("id, name, description, updated_at").execute()
        templates = tpl_res.data or []

        # Pull slot rows (grouped client-side; small N so it's fine).
        try:
            slot_res = raw.table("agent_prompt_templates").select(
                "template_id, slot_name, version, source, updated_at, updated_by"
            ).execute()
            slot_rows = slot_res.data or []
        except Exception:
            slot_rows = []

        by_tpl: dict = {}
        for r in slot_rows:
            by_tpl.setdefault(r["template_id"], []).append(r)

        out_templates = []
        for tpl in templates:
            tid = tpl["id"]
            slots = by_tpl.get(tid, [])
            versions = [int(s.get("version") or 0) for s in slots]
            admin_n = sum(1 for s in slots if s.get("source") == "admin")
            out_templates.append({
                "id": tid,
                "name": tpl.get("name") or tid,
                "description": tpl.get("description") or "",
                "slot_count": len(slots),
                "min_version": min(versions) if versions else 0,
                "max_version": max(versions) if versions else 0,
                "admin_edited_slots": admin_n,
                "updated_at": tpl.get("updated_at"),
                "slots": [
                    {
                        "slot_name": s["slot_name"],
                        "version": s.get("version"),
                        "source": s.get("source"),
                        "updated_at": s.get("updated_at"),
                        "updated_by": s.get("updated_by"),
                    }
                    for s in sorted(slots, key=lambda r: r["slot_name"])
                ],
            })

        # Stored hash from app_meta (may be missing on fresh DBs).
        stored_hash = ""
        try:
            meta_res = raw.table("app_meta").select("value").eq(
                "key", "last_agent_manifest_hash"
            ).limit(1).execute()
            if meta_res.data:
                stored_hash = meta_res.data[0].get("value") or ""
        except Exception:
            pass

        current_hash = compute_agent_manifest_hash()

        return {
            "templates": sorted(out_templates, key=lambda t: t["id"]),
            "stored_manifest_hash": stored_hash,
            "current_manifest_hash": current_hash,
            "in_sync": (stored_hash == current_hash) and bool(stored_hash),
        }
    except Exception as e:
        logger.error("Error listing template state: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/templates/seed")
async def seed_templates():
    """
    Run the non-destructive JSON → DB seeder.

    Manifest-gated short-circuit fires when the JSON files haven't changed.
    Slot rows whose source = 'admin' are skipped (admin edits sacred).
    Returns the seeder summary dict.
    """
    try:
        db = get_db()
        summary = await db.seed_agent_templates(force=False)
        return summary
    except Exception as e:
        logger.error("Error seeding agent templates: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/templates/seed-force")
async def seed_templates_force():
    """
    Run the seeder with force=True.

    OVERRIDES the manifest short-circuit AND overwrites admin-edited slot rows
    with the JSON content. Use only when JSON should be re-asserted as truth
    (e.g. admin edits were wrong, or DB drifted from intended baseline).
    """
    try:
        db = get_db()
        summary = await db.seed_agent_templates(force=True)
        return summary
    except Exception as e:
        logger.error("Error force-seeding agent templates: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/templates/{template_id}/export-to-file")
async def export_template_to_file(template_id: str):
    """
    Export a single agent template row + its prompt slots to a JSON seed file
    in the bundled seed directory (app/defaults/agents/<template_id>.json).

    Admin-only. The file is git-tracked and synced with the repository.
    Returns the file path and a preview of the exported data.
    """
    import json as _json
    import os
    from app.util.paths import DEFAULTS_DIR

    db = get_db()
    raw = db.get_raw_client()

    # Read the template config row
    try:
        tpl_res = raw.table("agent_templates").select("*").eq("id", template_id).limit(1).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to read template: {e}")

    if not tpl_res.data:
        raise HTTPException(status_code=404, detail=f"Template '{template_id}' not found.")
    tpl = tpl_res.data[0]

    # Read prompt slots
    try:
        slot_res = raw.table("agent_prompt_templates").select(
            "slot_name, order_index, lock, merge_mode, content, version"
        ).eq("template_id", template_id).order("order_index").execute()
    except Exception:
        slot_res = type("obj", (object,), {"data": []})()

    slots = slot_res.data or []

    # Build the JSON structure matching scan_agent_json_files schema.
    # Metadata: parse and strip runtime-only keys.
    _meta_raw = tpl.get("metadata") or "{}"
    try:
        meta_obj = _json.loads(_meta_raw) if isinstance(_meta_raw, str) else dict(_meta_raw or {})
    except (_json.JSONDecodeError, TypeError):
        meta_obj = {}
    # Strip the source marker we added — it's not part of the canonical seed schema.
    meta_obj.pop("source", None)

    # Map slot rows to flat prompt keys
    slot_map = {s["slot_name"]: s for s in slots}
    _prompt_keys = {
        "system": "system_prompt",
        "agent": "agent_prompt",
        "user": "user_prompt",
        "skills": "skills_prompt",
        "tasks": "tasks_prompt",
        "misc": "misc_prompt",
        "automation": "automation_prompt",
        "bootstrap_tools": "bootstrap_tools",
    }

    out = {
        "id": tpl["id"],
        "version": max((int(s.get("version") or 0) for s in slots), default=1),
        "name": tpl.get("name") or tpl["id"],
        "description": tpl.get("description") or "",
        "icon": tpl.get("icon") or "",
        "can_be_default": bool(tpl.get("can_be_default", True)),
        "is_system": bool(tpl.get("is_system", False)),
        "is_pipeline": bool(tpl.get("is_pipeline", False)),
        "is_admin_agent": bool(tpl.get("is_admin_agent", False)),
        "access_level": tpl.get("access_level") or "all",
        "discoverable": bool(tpl.get("discoverable", False)),
        "system_prompt": (slot_map.get("system", {}) or {}).get("content", "") if isinstance(slot_map.get("system"), dict) else "",
        "max_turn_count": tpl.get("max_turn_count") or 0,
        "max_wall_seconds": tpl.get("max_wall_seconds"),
        "max_identical_tool_calls": tpl.get("max_identical_tool_calls", 0),
        "max_stall_strikes": tpl.get("max_stall_strikes", 0),
        "model": tpl.get("model"),
        "provider": tpl.get("provider"),
        "temperature": tpl.get("temperature") or 0.0,
        "max_tokens": tpl.get("max_tokens") or 8096,
        "trigger_type": tpl.get("trigger_type") or "user_input",
        "trigger_key": tpl.get("trigger_key"),
        "loop_logic": _json.loads(tpl.get("loop_logic") or "[]"),
        "tool_modes": meta_obj.get("tool_modes") or {},
        "tool_permissions": meta_obj.get("tool_permissions") or {},
        "metadata": meta_obj,
    }

    # Add flat prompt keys for slots that have them
    for pkey, json_key in _prompt_keys.items():
        if pkey in slot_map:
            out[json_key] = slot_map[pkey].get("content", "")

    # Pre-enable connections
    pre_enabled = meta_obj.get("pre_enabled_connections")
    if pre_enabled:
        out["pre_enabled_connections"] = pre_enabled
        out["abilities"] = pre_enabled

    # Write to the bundled seed directory
    agents_dir = os.path.join(str(DEFAULTS_DIR), "agents")
    os.makedirs(agents_dir, exist_ok=True)
    fpath = os.path.join(agents_dir, f"{template_id}.json")

    with open(fpath, "w", encoding="utf-8") as f:
        _json.dump(out, f, indent=2, ensure_ascii=False)
        f.write("\n")

    return {
        "path": os.path.relpath(fpath),
        "template_id": template_id,
        "version": out["version"],
        "size_bytes": os.path.getsize(fpath),
        "preview": dict(list(out.items())[:6]) | {"slot_count": len(slots)},
    }

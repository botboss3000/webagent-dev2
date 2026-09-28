"""
Ability Delete API — delete an ability file (or a whole group) from plugins/abilities/.

By default abilities are deletable through the UI. An ability is only blocked if
its descriptor carries ``"protected": true`` — those must be removed/edited or
deleted from disk manually. Whole groups can also be deleted (entire folder),
unless any member ability is protected.
"""
import logging
import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

_ABILITIES_DIR = Path(__file__).resolve().parents[2] / "plugins" / "abilities"


class DeleteAbilityRequest(BaseModel):
    """Admin confirmation to delete an ability."""
    confirmed: bool = True


router = APIRouter(prefix="/api/v1/abilities")


async def _require_app_admin(request: Request) -> None:
    """Verify the authenticated caller before any repository-wide mutation."""
    from app.auth.identity import request_user_id
    from app.db import get_db

    user_id = request_user_id(request)
    if not user_id:
        raise HTTPException(status_code=401, detail="Not authenticated")
    if not await get_db().is_user_admin(user_id):
        raise HTTPException(status_code=403, detail="App admin access required.")


# ── Group endpoints ──────────────────────────────────────────────────────────
# NOTE: these literal `/group/...` routes are declared BEFORE the `/{ability_id}`
# routes below so they are matched first and are NOT shadowed by the dynamic
# ability-id parameter routes.

def _group_files(folder: str) -> list:
    """Collect every deletable file in a group folder — recurses into each
    ability subfolder (excluding _group.json and any name starting with '_' or
    __pycache__). Returns list of {path (repo-relative), size_bytes}."""
    out = []
    gdir = _ABILITIES_DIR / folder
    if not gdir.is_dir():
        return out
    for sub in sorted(gdir.iterdir()):
        if not sub.is_dir():
            continue
        if sub.name.startswith("_") or sub.name == "__pycache__":
            continue
        for fpath in sorted(sub.rglob("*")):
            if not fpath.is_file():
                continue
            if fpath.parent.name == "__pycache__":
                continue
            out.append({
                "path": str(fpath.relative_to(_ABILITIES_DIR.parent.parent)),
                "size_bytes": fpath.stat().st_size,
            })
    return out


@router.get("/group/{group_id}/delete-info")
async def get_group_delete_info(group_id: str):
    """Return info about what deleting a whole group would remove — its member
    abilities, all files in the folder, and how many agents use any member.
    Non-destructive.
    """
    from app.abilities import ui_catalog, all_raw, group_folder

    folder = group_folder(group_id)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

    catalog = ui_catalog()
    group_meta = next((g for g in catalog.get("groups", []) if g["id"] == group_id), None)
    if group_meta is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

    group_name = group_meta.get("name", group_id)
    member_ids = list(group_meta.get("members", []))

    raw = all_raw()

    # Build per-ability info
    abilities_info = []
    protected_members = []
    for aid in member_ids:
        feat = raw.get(aid, {})
        is_protected = bool(feat.get("protected", False))
        if is_protected:
            protected_members.append(feat.get("display_name", aid))
        files = []
        ability_dir = _ABILITIES_DIR / folder / aid
        if ability_dir.is_dir():
            for fpath in sorted(ability_dir.iterdir()):
                if not fpath.is_file():
                    continue
                if fpath.name.startswith("."):
                    continue
                files.append({
                    "path": str(fpath.relative_to(_ABILITIES_DIR.parent.parent)),
                    "size_bytes": fpath.stat().st_size,
                })
        abilities_info.append({
            "id": aid,
            "display_name": feat.get("display_name", aid),
            "protected": is_protected,
            "files": files,
        })

    total_files = len(_group_files(folder))

    # Count agent usage across all member ability ids
    member_set = set(member_ids)
    agent_usage_count = 0
    try:
        from app.db import get_db
        db = get_db()
        conn = None
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
        elif hasattr(db, "_conn"):
            conn = db._conn
        if conn and member_set:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE section = 'ability'",
            ).fetchall()
            agent_usage_count = sum(
                1 for r in rows
                if r.get("section") == "ability" and r.get("connection_type") in member_set
            )
    except Exception as e:
        logger.warning("Could not check agent usage for group %s: %s", group_id, e)

    deletable = len(protected_members) == 0
    if deletable:
        warning = (
            f"Deleting this group removes ALL its abilities and the entire "
            f"plugins/abilities/{folder}/ directory. This disables them for every "
            f"agent. The server should restart to fully clear."
        )
    else:
        warning = (
            "This group cannot be deleted through the UI because it contains "
            f"protected abilities: {', '.join(protected_members)}. Remove the "
            "`protected` flag on those, or delete the folder manually from "
            "plugins/abilities/."
        )

    return {
        "status": "ok",
        "group": {
            "id": group_id,
            "name": group_name,
            "folder": folder,
            "abilities": abilities_info,
            "total_files": total_files,
            "agent_usage_count": agent_usage_count,
        },
        "deletable": deletable,
        "warning": warning,
    }


@router.delete("/group/{group_id}")
async def delete_group(group_id: str, request: Request, req: Optional[DeleteAbilityRequest] = None):
    """Delete an entire ability group — removes the whole folder and disables
    every member ability on all agents.

    Blocked (403) if any member ability is marked `protected`.
    """
    await _require_app_admin(request)
    confirmed = req.confirmed if req else True
    if not confirmed:
        raise HTTPException(status_code=400, detail="Deletion not confirmed")

    from app.abilities import ui_catalog, all_raw, group_folder, reload

    folder = group_folder(group_id)
    if folder is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

    catalog = ui_catalog()
    group_meta = next((g for g in catalog.get("groups", []) if g["id"] == group_id), None)
    if group_meta is None:
        raise HTTPException(status_code=404, detail=f"Group '{group_id}' not found")

    group_name = group_meta.get("name", group_id)
    member_ids = list(group_meta.get("members", []))

    raw = all_raw()

    # Refuse if any member is protected
    protected_members = [
        raw.get(aid, {}).get("display_name", aid)
        for aid in member_ids
        if bool(raw.get(aid, {}).get("protected", False))
    ]
    if protected_members:
        raise HTTPException(
            status_code=403,
            detail=(
                f"Group '{group_name}' contains protected abilities and cannot be "
                f"deleted through the UI: {', '.join(protected_members)}."
            ),
        )

    # Track which member abilities actually existed (had a descriptor)
    existing_members = [aid for aid in member_ids if aid in raw]

    # 1. Delete the entire folder directory
    gdir = _ABILITIES_DIR / folder
    try:
        if gdir.is_dir():
            shutil.rmtree(gdir)
            logger.info("Deleted ability group folder: %s", gdir)
        else:
            raise HTTPException(status_code=404, detail=f"Group folder '{folder}' not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error("Failed to delete group folder %s: %s", gdir, e)
        raise HTTPException(status_code=500, detail=f"Failed to delete group folder: {e}")

    # 2. Disable every member ability on all agents
    disabled_count = 0
    try:
        from app.db import get_db
        db = get_db()
        if hasattr(db, "delete_all_ability_connections"):
            for aid in member_ids:
                try:
                    disabled_count += await db.delete_all_ability_connections(aid)
                except Exception as e:
                    logger.warning("Could not disable ability '%s': %s", aid, e)
            logger.info("Disabled group '%s' members on %d agent rows", group_id, disabled_count)
    except Exception as e:
        logger.warning("Could not disable ability connections for group %s: %s", group_id, e)

    # 3. Reload the catalog cache
    try:
        reload()
        logger.info("Ability cache cleared after deleting group '%s'", group_id)
    except Exception as e:
        logger.warning("Could not reload ability cache: %s", e)

    return {
        "status": "ok",
        "message": f"Group '{group_name}' deleted.",
        "deleted_group": group_id,
        "deleted_folder": folder,
        "deleted_abilities": existing_members,
        "disabled_on_agents": disabled_count,
        "note": "The server should be restarted for changes to fully take effect.",
    }


@router.get("/{ability_id}/delete-info")
async def get_delete_info(ability_id: str):
    """Return info about what would be deleted — files, agents using it, status.
    Non-destructive — for showing the admin what they're about to delete.
    """
    from app.abilities import all_raw

    abilities = all_raw()
    feat = abilities.get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")

    status = feat.get("status", "stable")
    protected = bool(feat.get("protected", False))

    # Find all files belonging to this ability (inside its group folder)
    files_to_delete = [
        {
            "path": str(fpath.relative_to(_ABILITIES_DIR.parent.parent)),
            "size_bytes": fpath.stat().st_size,
        }
        for fpath in _ability_files(ability_id, feat.get("group"))
    ]

    # Find which agents have this ability enabled
    agents_using = []
    try:
        from app.db import get_db
        db = get_db()
        conn = None
        if hasattr(db, "_get_conn"):
            conn = db._get_conn()
        elif hasattr(db, "_conn"):
            conn = db._conn
        if conn:
            rows = conn.execute(
                "SELECT * FROM agent_connections WHERE connection_type = ?",
                (ability_id,),
            ).fetchall()
            agents_using = [
                {"agent_id": r["agent_id"], "enabled": bool(r.get("enabled", 0))}
                for r in rows if r.get("section") == "ability"
            ]
        elif hasattr(db, "get_all_connections_by_type"):
            rows = await db.get_all_connections_by_type(ability_id)
            agents_using = [
                {"agent_id": r["agent_id"], "enabled": bool(r.get("enabled", 0))}
                for r in rows if r.get("section") == "ability"
            ]
    except Exception as e:
        logger.warning("Could not check agent usage for %s: %s", ability_id, e)

    return {
        "status": "ok",
        "ability": {
            "id": ability_id,
            "display_name": feat.get("display_name", ability_id),
            "status": status,
            "files": files_to_delete,
            "agents_using": agents_using,
            "agent_count": len(agents_using),
        },
        "deletable": (not protected),
        "warning": (
            "This will delete the ability file and disable it for all agents. "
            "The server should restart to fully clear the change."
            if not protected
            else "This ability is protected and cannot be deleted through the UI. "
                 "Remove the `protected` flag in its descriptor or delete the file "
                 "manually from plugins/abilities/."
        ),
    }


@router.delete("/{ability_id}")
async def delete_ability(ability_id: str, request: Request, req: Optional[DeleteAbilityRequest] = None):
    """Delete an ability file from the filesystem.

    Deletable by default; blocked only if the descriptor is marked `protected`.
    This will:
    1. Find and delete all files belonging to this ability (.py, .skill.md, .json)
    2. Disable the ability on all agents that have it enabled
    3. Clear the server-side ability cache
    """
    await _require_app_admin(request)
    confirmed = req.confirmed if req else True
    if not confirmed:
        raise HTTPException(status_code=400, detail="Deletion not confirmed")

    from app.abilities import all_raw

    abilities = all_raw()
    feat = abilities.get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")

    if bool(feat.get("protected", False)):
        raise HTTPException(
            status_code=403,
            detail=f"Ability '{ability_id}' is protected and cannot be deleted through the UI.",
        )

    # 1. Delete the ability's subfolder (all its files, __pycache__, etc.)
    from app.abilities import group_folder as _gf
    group_folder_name = _gf(feat.get("group", ""))
    if not group_folder_name:
        raise HTTPException(status_code=404, detail=f"Group not found for ability '{ability_id}'")
    ability_dir = _ABILITIES_DIR / group_folder_name / ability_id
    if not ability_dir.is_dir():
        raise HTTPException(status_code=404, detail=f"Ability folder not found for '{ability_id}'")

    # Collect file names for the response
    deleted_files = []
    try:
        for fpath in sorted(ability_dir.rglob("*")):
            if fpath.is_file() and fpath.parent.name != "__pycache__":
                deleted_files.append(str(fpath.name))
        shutil.rmtree(ability_dir)
        logger.info("Deleted ability folder: %s", ability_dir)
    except Exception as e:
        logger.error("Failed to delete ability folder %s: %s", ability_dir, e)
        raise HTTPException(status_code=500, detail=f"Failed to delete ability folder: {e}")

    # 2. Disable the ability on all agents
    disabled_count = 0
    try:
        from app.db import get_db
        db = get_db()
        if hasattr(db, "delete_all_ability_connections"):
            disabled_count = await db.delete_all_ability_connections(ability_id)
            logger.info("Disabled ability '%s' on %d agents", ability_id, disabled_count)
    except Exception as e:
        logger.warning("Could not disable ability connections for %s: %s", ability_id, e)

    # 3. Clear the ability cache so the next catalog read reflects the deletion
    try:
        from app.abilities import reload
        reload()
        logger.info("Ability cache cleared after deleting '%s'", ability_id)
    except Exception as e:
        logger.warning("Could not reload ability cache: %s", e)

    return {
        "status": "ok",
        "message": f"Ability '{ability_id}' deleted.",
        "deleted_files": deleted_files,
        "disabled_on_agents": disabled_count,
        "note": "The server should be restarted for changes to fully take effect.",
    }


class SaveSkillRequest(BaseModel):
    """Admin edit of an ability's shared .skill.md document."""
    content: str = ""


@router.get("/{ability_id}/skills/{skill_id}")
async def get_ability_general_skill(ability_id: str, skill_id: str):
    """Return one directory-discovered general skill for the read-only card viewer."""
    from app.abilities import ability_general_skills
    record = next((s for s in ability_general_skills(ability_id) if s.get("skill_id") == skill_id), None)
    if not record:
        raise HTTPException(status_code=404, detail="Unknown ability skill.")
    path = record.get("path")
    return {
        "status": "ok", "ability_id": ability_id, "skill_id": skill_id,
        "title": record.get("title") or skill_id,
        "summary": record.get("when_to_use") or "",
        "content": record.get("body") or "", "exists": bool(path and path.is_file()),
        "editable": False,
        "path": str(path.relative_to(_ABILITIES_DIR.parent.parent)) if path else None,
    }


@router.get("/{ability_id}/skill")
async def get_ability_skill(ability_id: str):
    """Return an ability's bundled skill document for viewing/editing.

    The body is the resolved skill text (the ability's ``<id>.skill.md`` file, or
    its inline descriptor skill). ``editable`` is False for inline-skill abilities
    (a file edit would be ignored) and for unknown abilities. Non-destructive.
    """
    from app.abilities import (
        all_raw, ability_feature_with_skill, skill_file_path, ability_has_inline_skill,
    )

    feat = all_raw().get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")

    with_skill = ability_feature_with_skill(ability_id)
    body = (with_skill or {}).get("skill", "") if with_skill else ""
    inline = ability_has_inline_skill(ability_id)
    path = skill_file_path(ability_id)
    rel_path = str(path.relative_to(_ABILITIES_DIR.parent.parent)) if path else None

    return {
        "status": "ok",
        "ability_id": ability_id,
        "display_name": feat.get("display_name", ability_id),
        "summary": feat.get("skill_summary") or "",
        "content": body,
        "exists": bool(path and path.is_file()),
        "inline": inline,
        # Editable when we can resolve a writable file path AND the skill is not
        # defined inline in the descriptor (which would shadow the file).
        "editable": bool(path) and not inline,
        "path": rel_path,
    }


@router.put("/{ability_id}/skill")
async def save_ability_skill(ability_id: str, req: SaveSkillRequest, request: Request):
    """Write an ability's shared ``<id>.skill.md`` file. Creates it if absent.

    This edits a repo-tracked file shared by every agent that uses the ability.
    Refused (409) when the ability defines its skill inline in the descriptor,
    since the file would be ignored. After writing, the ability cache is reloaded
    so the next prompt build serves the new body.
    """
    await _require_app_admin(request)
    from app.abilities import all_raw, skill_file_path, ability_has_inline_skill, reload

    feat = all_raw().get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")

    if ability_has_inline_skill(ability_id):
        raise HTTPException(
            status_code=409,
            detail=(
                f"Ability '{ability_id}' defines its skill inline in its descriptor; "
                "a .skill.md file would be ignored. Edit the descriptor instead."
            ),
        )

    path = skill_file_path(ability_id)
    if path is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not resolve a skill file path for ability '{ability_id}'.",
        )

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(req.content or "", encoding="utf-8")
        logger.info("Saved ability skill file: %s", path)
    except Exception as e:
        logger.error("Failed to write skill file %s: %s", path, e)
        raise HTTPException(status_code=500, detail=f"Failed to write skill file: {e}")

    # Refresh the ability cache so the new body is served on the next prompt build.
    try:
        reload()
    except Exception as e:
        logger.warning("Could not reload ability cache after skill save: %s", e)

    return {
        "status": "ok",
        "ability_id": ability_id,
        "path": str(path.relative_to(_ABILITIES_DIR.parent.parent)),
        "bytes": len((req.content or "").encode("utf-8")),
    }


class SaveToolSkillRequest(BaseModel):
    """Admin edit of one ability tool's canonical .skill.md document."""
    content: str = ""


def _tool_skill_path_or_404(ability_id: str, tool_name: str) -> Path:
    """Resolve only a runtime-declared tool skill; never accept an arbitrary path."""
    from app.abilities import all_raw, tool_skill_file_path

    feat = all_raw().get(ability_id)
    if not feat:
        raise HTTPException(status_code=404, detail=f"Ability '{ability_id}' not found")
    if tool_name not in set(feat.get("tools") or []):
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' is not owned by ability '{ability_id}'",
        )
    path = tool_skill_file_path(ability_id, tool_name)
    if path is None:
        raise HTTPException(status_code=404, detail="Could not resolve this tool skill file.")
    return path


@router.get("/{ability_id}/tools/{tool_name}/skill")
async def get_ability_tool_skill(ability_id: str, tool_name: str):
    """Return one tool's canonical Markdown skill document for the shared editor."""
    path = _tool_skill_path_or_404(ability_id, tool_name)
    try:
        content = path.read_text(encoding="utf-8") if path.is_file() else ""
    except Exception as e:
        logger.error("Failed to read tool skill file %s: %s", path, e)
        raise HTTPException(status_code=500, detail=f"Failed to read tool skill file: {e}")
    return {
        "status": "ok",
        "ability_id": ability_id,
        "tool_name": tool_name,
        "content": content,
        "exists": path.is_file(),
        "editable": True,
        "path": str(path.relative_to(_ABILITIES_DIR.parent.parent)),
    }


@router.put("/{ability_id}/tools/{tool_name}/skill")
async def save_ability_tool_skill(
    ability_id: str, tool_name: str, req: SaveToolSkillRequest, request: Request,
):
    """Write one runtime-declared tool skill, then refresh the ability cache."""
    await _require_app_admin(request)
    path = _tool_skill_path_or_404(ability_id, tool_name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(req.content or "", encoding="utf-8")
        logger.info("Saved ability tool skill file: %s", path)
    except Exception as e:
        logger.error("Failed to write tool skill file %s: %s", path, e)
        raise HTTPException(status_code=500, detail=f"Failed to write tool skill file: {e}")

    try:
        from app.abilities import reload
        reload()
    except Exception as e:
        logger.warning("Could not reload ability cache after tool skill save: %s", e)

    return {
        "status": "ok",
        "ability_id": ability_id,
        "tool_name": tool_name,
        "path": str(path.relative_to(_ABILITIES_DIR.parent.parent)),
        "bytes": len((req.content or "").encode("utf-8")),
    }


def _ability_files(ability_id: str, group_id: Optional[str]) -> list:
    """Return the Path objects of every file belonging to an ability,
    collected from its subfolder inside the group."""
    from app.abilities import group_folder

    folder = group_folder(group_id) if group_id else None
    out: list = []
    if not folder:
        return out
    ability_dir = _ABILITIES_DIR / folder / ability_id
    if not ability_dir.is_dir():
        return out
    for fpath in sorted(ability_dir.rglob("*")):
        if not fpath.is_file():
            continue
        if fpath.parent.name == "__pycache__":
            continue
        out.append(fpath)
    return out

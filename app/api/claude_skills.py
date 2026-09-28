"""
HTTP routes for the Local Claude Code Skills tab.

Thin wrapper over plugins/engines/claude_code/claude_code_skills.py: list / save / delete
the SKILL.md files the local `claude` finds (personal + this agent's working
folder). Admin-gated the same way the rest of the engine is — it reuses
`_require_admin` from app/api/claude_auth.py. The agent's working folder is
resolved SERVER-SIDE from the agent record (never trusted from the client), so
the routes can only ever touch the two skills directories for that agent.

CORE wiring note: like claude_auth, this is a small subsystem registered in
app/main.py for the claude_code engine — it is NOT an auto-discovered
ability/integration. Shares the `/api/v1/claude-code` prefix with claude_auth.
Breadcrumbs: ui/main-panel/agents/js/claude-skills.js (the caller).
REMOVE-WHEN: the Local Claude Code engine (claude_code) is dropped.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.api.claude_auth import _require_admin
from plugins.engines.claude_code import claude_code_skills as skills

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/claude-code")


async def _agent_rec(agent_id: str) -> dict:
    from app.db import get_db
    try:
        rec = await get_db().get_agent_by_id(agent_id)
    except Exception:
        rec = None
    if not rec:
        raise HTTPException(status_code=404, detail="Agent not found.")
    return rec


class SkillSave(BaseModel):
    agent_id: str = Field(..., min_length=1, max_length=128)
    source: str = Field(..., min_length=1, max_length=16)
    slug: Optional[str] = Field(None, max_length=80)
    name: str = Field("", max_length=200)
    description: str = Field("", max_length=4000)
    body: str = Field("", max_length=200000)


@router.get("/skills")
async def list_skills(request: Request, agent_id: str = Query(...)):
    await _require_admin(request)
    rec = await _agent_rec(agent_id)
    try:
        items = skills.list_skills(rec)
        dirs = skills.dir_labels(rec)
    except Exception as e:
        logger.warning("claude skills list failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not read skills: {e}")
    return {"skills": items, "editable": True, "dirs": dirs}


@router.put("/skills")
async def save_skill(request: Request, body: SkillSave):
    await _require_admin(request)
    if body.source not in skills.SOURCES:
        raise HTTPException(status_code=400, detail="Unknown skill source.")
    rec = await _agent_rec(body.agent_id)
    try:
        saved = skills.save_skill(rec, body.source, body.slug,
                                  body.name, body.description, body.body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("claude skills save failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not save skill: {e}")
    return {"ok": True, "skill": saved}


@router.delete("/skills")
async def delete_skill(request: Request, agent_id: str = Query(...),
                       source: str = Query(...), slug: str = Query(...)):
    await _require_admin(request)
    if source not in skills.SOURCES:
        raise HTTPException(status_code=400, detail="Unknown skill source.")
    rec = await _agent_rec(agent_id)
    try:
        skills.delete_skill(rec, source, slug)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.warning("claude skills delete failed: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not delete skill: {e}")
    return {"ok": True}

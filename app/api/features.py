"""Feature catalog endpoint (admin-only) + app-prompts endpoint (public).

Exposes the runtime discovery catalog — every capability the app found, its
maturity, whether it's true drop-in, and whether the active edition switches it
on. Backs the **App Config → Features** report.

    GET /api/v1/features   → { active_edition, editions, counts, features[] }

Also serves the app-prompts catalog (public, no auth):

    GET /api/v1/app-prompts          → full JSON file
    GET /api/v1/app-prompts/section   → just {section: ...} (e.g. /ui-handoffs)

And generates LLM example-requests for a page-assistant pill area (admin-only):

    POST /api/v1/app-prompts/page-suggestions  {page, area, count?}
        → { suggestions: [str, ...] }

Phase 1 is report-only; see docs/claude/production-editions.md.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.api.db_viewer import require_admin
from app.util.paths import app_prompts_path

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["features"])


def _load_prompts() -> dict:
    try:
        return json.loads(app_prompts_path().read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("Failed to load app-prompts.json: %s", e)
        return {}


@router.get("/features")
async def get_features(_auth: dict = Depends(require_admin)):
    """Return the full feature catalog + active edition (admin-only)."""
    try:
        from app.features import build_catalog
        return build_catalog()
    except Exception as e:
        logger.exception("Failed to build feature catalog: %s", e)
        raise HTTPException(status_code=500, detail=f"Could not build feature catalog: {e}")


@router.get("/app-prompts")
async def get_app_prompts():
    """Return the full app-prompts.json catalog (public)."""
    data = _load_prompts()
    if not data:
        raise HTTPException(status_code=500, detail="Could not load app-prompts.json")
    return data


class PageSuggestionsRequest(BaseModel):
    page: str
    area: str
    count: int | None = None


@router.post("/app-prompts/page-suggestions")
async def page_assistant_suggestions(
    req: PageSuggestionsRequest,
    _auth: dict = Depends(require_admin),
):
    """Generate short example requests for one page-assistant pill area.

    Best-effort: returns an empty list (not an error) if the engine is unavailable
    so the pill can keep its static placeholder.
    """
    try:
        from app.agent.suggestions import generate_page_suggestions
        uid = _auth.get("user_id") or _auth.get("sub")
        items = await generate_page_suggestions(req.page, req.area, count=req.count or 4, user_id=uid)
    except Exception as e:
        logger.warning("page_assistant_suggestions failed: %s", e)
        items = []
    return {"suggestions": items}


@router.get("/app-prompts/{section:path}")
async def get_app_prompts_section(section: str):
    """Return a subsection of app-prompts.json by key path, e.g. ui-handoffs, runtime-fragments, app-level-prompts."""
    key = section.replace("-", "_")
    data = _load_prompts()
    if not data:
        raise HTTPException(status_code=500, detail="Could not load app-prompts.json")
    if key not in data:
        raise HTTPException(status_code=404, detail=f"Section '{key}' not found in app-prompts.json")
    return {key: data[key]}

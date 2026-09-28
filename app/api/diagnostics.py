"""
Diagnostics API — admin-only read access to the flight-recorder.

The recorder (app/agent/diagnostics.py) captures server warnings/errors,
agent-loop pipeline problems and run outcomes into a RAM ring + a durable,
auto-pruned `diagnostics` table. These endpoints let the Admin Tools
"Diagnostics" sub-page (and any operator) read recent records back.

    GET    /api/v1/diagnostics         — filtered list of recent records
    GET    /api/v1/diagnostics/stats   — counts by level + ring/queue sizes
    DELETE /api/v1/diagnostics         — clear records (all, older-than, or by filter)

All require a global admin (same gate as the DB viewer).
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.db_viewer import require_admin
from app.agent.diagnostics import get_recorder

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/diagnostics", tags=["diagnostics"])


def _split_csv(v: Optional[str]) -> Optional[list]:
    if not v:
        return None
    parts = [p.strip() for p in v.split(",") if p.strip()]
    return parts or None


@router.get("")
async def list_diagnostics(
    levels: Optional[str] = Query(None, description="Comma list: debug,info,warning,error,critical"),
    categories: Optional[str] = Query(None, description="Comma list: server,http,loop,run,recovery,tool"),
    session_id: Optional[str] = Query(None),
    agent_id: Optional[str] = Query(None),
    search: Optional[str] = Query(None, description="Substring match on message/source/detail"),
    since_minutes: Optional[float] = Query(None, description="Only records newer than N minutes"),
    limit: int = Query(200, ge=1, le=2000),
    include_ring: bool = Query(True, description="Merge the live in-memory ring with durable rows"),
    _auth: dict = Depends(require_admin),
):
    """Return recent diagnostic records, newest first (durable + live, merged)."""
    rec = get_recorder()
    records = await rec.query(
        levels=_split_csv(levels),
        categories=_split_csv(categories),
        session_id=session_id,
        agent_id=agent_id,
        search=search,
        since_minutes=since_minutes,
        limit=limit,
        include_ring=include_ring,
    )
    return {"records": records, "count": len(records)}


@router.get("/stats")
async def diagnostics_stats(_auth: dict = Depends(require_admin)):
    """Return recorder health: enabled flag, ring/queue sizes, counts by level."""
    return get_recorder().stats()


@router.delete("")
async def clear_diagnostics(
    older_than_minutes: Optional[float] = Query(None, description="Clear records older than N minutes (omit = all)"),
    levels: Optional[str] = Query(None, description="Comma list — only clear these severities"),
    categories: Optional[str] = Query(None, description="Comma list — only clear these categories"),
    search: Optional[str] = Query(None, description="Only clear records matching this substring"),
    clear_files: bool = Query(False, description="Also truncate the logs/*.log text files (full clear only)"),
    _auth: dict = Depends(require_admin),
):
    """Clear diagnostics. With no scope, clears everything; otherwise clears only
    the records matching older_than_minutes / levels / categories / search.
    Purges the in-memory ring and the durable DB table, and (on a full clear
    with clear_files) the per-source text files."""
    rec = get_recorder()
    result = await rec.clear(
        older_than_minutes=older_than_minutes,
        levels=_split_csv(levels),
        categories=_split_csv(categories),
        search=search,
        clear_files=clear_files,
    )
    return {"status": "ok", **result}

"""Alternate-engine REST API — mounted generically like the billing plugin.

Hosts endpoints that talk to the LOCAL CLI harnesses (Codex, …) on behalf of the
browser, which cannot shell out itself. Currently one route:

  GET /api/v1/engines/model-catalog?engine=codex&force=0
      Live model (+ per-model reasoning-effort) options for an alternate engine,
      discovered from the CLI itself rather than hardcoded. The chat footer model
      selector AND the agent Config tab's "Query CLI for latest model options"
      button both use this, so alternate-engine agents show the harness's REAL
      choices without an admin maintaining them.
      Returns {"engine", "source": "cli"|"fallback", "catalog"} — the frontend
      falls back to its curated list when source != "cli".

Auth is deliberately lenient (mirrors /admin/settings/current-model-info): the
caller's token is decoded if present, but the route is not gated — these are
non-sensitive harness metadata (model names/blurbs), not secrets. Plugins are
allowed to import app core (app.auth.jwt) — see plugins/billing/api.py.
"""

from __future__ import annotations

import asyncio
import logging
import json
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request
from pydantic import BaseModel, Field

from app.auth.jwt import decode_token
from app.auth.identity import assert_caller_is
from app.db import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/engines", tags=["engines"])


class CodexPortalLinksRequest(BaseModel):
    user_id: str
    agent_id: str
    thread_ids: list[str] = Field(default_factory=list, max_length=100)


class CodexPortalTurnRequest(BaseModel):
    user_id: str
    agent_id: str
    message: str = Field(min_length=1)
    execution_mode: str = "ask"
    model: Optional[str] = None
    effort: Optional[str] = None


class CodexPortalCreateRequest(BaseModel):
    user_id: str
    agent_id: str
    execution_mode: str = "ask"


class CodexPortalInterruptRequest(BaseModel):
    user_id: str
    agent_id: str
    turn_id: Optional[str] = None


class CodexPortalLinkPatchRequest(BaseModel):
    user_id: str
    agent_id: str
    pinned: Optional[bool] = None
    hidden: Optional[bool] = None
    sort_order: Optional[int] = None


async def _require_portal_admin(request: Request, user_id: str) -> str:
    uid = await assert_caller_is(request, user_id)
    if not await get_db().is_user_admin(uid):
        raise HTTPException(status_code=403, detail="Codex Portal requires admin access.")
    return uid


def _native_thread_id(session_id: str) -> str:
    if not session_id.startswith("codex:") or not session_id[6:]:
        raise HTTPException(status_code=400, detail="Invalid Codex Portal session id.")
    return session_id[6:]


def _ensure_link(user_id: str, agent_id: str, thread_id: str) -> None:
    from plugins.engines.codex.portal_store import has_link
    if not has_link(user_id, agent_id, thread_id):
        raise HTTPException(status_code=404, detail="This Codex task is not linked to that Portal agent.")


async def _portal_agent_config(user_id: str, agent_id: str) -> dict:
    agents = await get_db().list_agents_for_user(user_id, include_admin=True, view="active")
    agent = next((row for row in agents if str(row.get("id") or "") == agent_id), None)
    if not agent:
        raise HTTPException(status_code=404, detail="Codex Portal agent not found.")
    metadata = agent.get("metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (TypeError, ValueError):
            metadata = {}
    codex = metadata.get("codex_code") if isinstance(metadata, dict) else {}
    engine = metadata.get("engine") if isinstance(metadata, dict) else agent.get("engine")
    if engine != "codex" or not isinstance(codex, dict) or codex.get("context_mode") != "codex_portal":
        raise HTTPException(status_code=400, detail="Selected agent is not configured for Codex Portal.")
    return codex


def _resolve_user_id(authorization: str = "", token_qs: str = "") -> str:
    """Best-effort caller id (ANONYMOUS when no valid token) — for logging only."""
    raw = ""
    if authorization.startswith("Bearer "):
        raw = authorization[7:]
    if not raw and token_qs:
        raw = token_qs
    if raw:
        payload = decode_token(raw)
        if payload:
            return str(payload.get("user_id", ""))
    return ""


def _catalog_meta(engine: str) -> tuple:
    """(lane, label) for the response — the metadata lane the engine's config is
    saved under, and the human label the UI shows."""
    if engine == "codex":
        return "codex_code", "Codex"
    if engine == "claude_code":
        return "claude_code", "Claude"
    return engine, engine


@router.get("/model-catalog")
async def get_engine_model_catalog(
    engine: str = Query("", alias="engine"),
    force: int = Query(0, alias="force"),
    authorization: Optional[str] = Header(None),
    token: Optional[str] = Query(None),
):
    """Live model options for an alternate engine, read from the local CLI.

    ``engine``: the engine id (metadata.engine) — e.g. "codex".
    ``force=1``: bypass the engine's process cache and re-run the CLI query now
        (what the Config tab's "Query CLI for latest model options" button does).
    """
    _resolve_user_id(authorization or "", token or "")
    engine = (engine or "").strip()
    if not engine:
        return {"engine": "", "source": "fallback", "catalog": None}
    try:
        from plugins.engines import get_engine_model_catalog as _get_cat

        fn = _get_cat(engine)
        if fn is None:  # engine has no CLI catalog (e.g. Claude) → curated fallback
            return {"engine": engine, "source": "fallback", "catalog": None}
        try:
            models = fn(force=bool(force))
        except TypeError:  # hook predates the force param — call without it
            models = fn()
        if not models:
            return {"engine": engine, "source": "fallback", "catalog": None}
        lane, label = _catalog_meta(engine)
        return {
            "engine": engine,
            "source": "cli",
            "catalog": {"lane": lane, "label": label, "models": models, "efforts": None},
        }
    except Exception as e:
        logger.warning("engine model-catalog failed for %s: %s", engine, e)
        return {"engine": engine, "source": "fallback", "catalog": None, "error": str(e)}


@router.get("/codex/portal/candidates")
async def codex_portal_candidates(
    request: Request,
    user_id: str = Query(...),
    agent_id: str = Query(...),
    limit: int = Query(200, ge=1, le=200),
    cursor: Optional[str] = Query(None),
):
    uid = await _require_portal_admin(request, user_id)
    await _portal_agent_config(uid, agent_id)
    from plugins.engines.codex.app_server import app_server
    from plugins.engines.codex.portal import thread_rows
    from plugins.engines.codex.portal_stats import task_stats
    from plugins.engines.codex.portal_store import list_links
    result = await app_server.list_threads(limit, cursor=cursor)
    linked = {row["thread_id"] for row in list_links(uid, agent_id)}
    rows = thread_rows(result)
    stats = await asyncio.to_thread(task_stats, [row["thread_id"] for row in rows])
    for row in rows:
        row["linked"] = row["thread_id"] in linked
        row.update(stats.get(row["thread_id"], {}))
    return {"threads": rows, "next_cursor": result.get("nextCursor")}


@router.get("/codex/portal/sessions")
async def codex_portal_sessions(request: Request, user_id: str = Query(...), agent_id: Optional[str] = Query(None)):
    uid = await _require_portal_admin(request, user_id)
    from plugins.engines.codex.app_server import app_server
    from plugins.engines.codex.portal import normalize_thread
    from plugins.engines.codex.portal_store import list_links
    links = list_links(uid, agent_id)
    if not links:
        return {"sessions": []}
    native_results = await asyncio.gather(
        *(app_server.read_thread_summary(link["thread_id"]) for link in links),
        return_exceptions=True,
    )
    by_id = {}
    for link, result in zip(links, native_results):
        if isinstance(result, Exception):
            logger.warning("Could not read linked Codex task %s: %s", link["thread_id"], result)
            continue
        native = normalize_thread((result or {}).get("thread") or result or {})
        by_id[link["thread_id"]] = native
    sessions = []
    for link in links:
        native = by_id.get(link["thread_id"], {"id": f'codex:{link["thread_id"]}', "thread_id": link["thread_id"], "title": "Codex task", "created_at": link["added_at"], "updated_at": link["added_at"]})
        sessions.append({**native, "agent_id": link["agent_id"], "agent_engine": "codex", "pinned": bool(link["pinned"]), "hidden": bool(link["hidden"]), "sort_order": link["sort_order"], "external_authority": "codex"})
    return {"sessions": sessions}


@router.post("/codex/portal/links")
async def codex_portal_add_links(body: CodexPortalLinksRequest, request: Request):
    uid = await _require_portal_admin(request, body.user_id)
    from plugins.engines.codex.portal_store import add_links
    thread_ids = [str(value).strip() for value in body.thread_ids if str(value).strip()]
    return {"added": add_links(uid, body.agent_id, thread_ids)}


@router.post("/codex/portal/threads")
async def codex_portal_create_thread(body: CodexPortalCreateRequest, request: Request):
    """Create and link a native Codex task for a fresh Portal chat."""
    uid = await _require_portal_admin(request, body.user_id)
    cfg = await _portal_agent_config(uid, body.agent_id)
    from app.util.paths import project_root
    from plugins.engines.codex.app_server import app_server
    from plugins.engines.codex.portal import normalize_thread
    from plugins.engines.codex.portal_store import add_links

    result = await app_server.start_thread(
        cwd=str(project_root()),
        model=str(cfg.get("model") or "").strip() or None,
        execution_mode=body.execution_mode,
    )
    thread = (result or {}).get("thread") or {}
    thread_id = str(thread.get("id") or "").strip()
    if not thread_id:
        raise HTTPException(status_code=502, detail="Codex did not return a task id.")
    add_links(uid, body.agent_id, [thread_id])
    session = {
        **normalize_thread(thread),
        "agent_id": body.agent_id,
        "agent_engine": "codex",
        "external_authority": "codex",
    }
    return {"session_id": session["id"], "session": session}


@router.delete("/codex/portal/links/{session_id}")
async def codex_portal_remove_link(session_id: str, request: Request, user_id: str = Query(...), agent_id: str = Query(...)):
    uid = await _require_portal_admin(request, user_id)
    from plugins.engines.codex.portal_store import remove_link
    return {"removed": remove_link(uid, agent_id, _native_thread_id(session_id))}


@router.patch("/codex/portal/links/{session_id}")
async def codex_portal_patch_link(session_id: str, body: CodexPortalLinkPatchRequest, request: Request):
    uid = await _require_portal_admin(request, body.user_id)
    from plugins.engines.codex.portal_store import update_link
    values = body.model_dump(exclude={"user_id", "agent_id"}, exclude_none=True)
    return {"updated": update_link(uid, body.agent_id, _native_thread_id(session_id), values)}


@router.get("/codex/portal/threads/{session_id}/messages")
async def codex_portal_messages(session_id: str, request: Request, user_id: str = Query(...), agent_id: str = Query(...)):
    uid = await _require_portal_admin(request, user_id)
    thread_id = _native_thread_id(session_id)
    # The agent-scoped Sessions page is the complete native Codex catalog.
    # A sidecar link controls promotion into WebAgent's account-wide catalogs;
    # it is not required merely to open or continue a native task here.
    await _portal_agent_config(uid, agent_id)
    from plugins.engines.codex.app_server import app_server
    from plugins.engines.codex.portal import messages_from_thread, thread_metrics
    from plugins.engines.codex.portal_stats import task_stats
    try:
        native_thread = await app_server.read_thread(thread_id)
    except Exception as exc:
        logger.exception("Codex Portal transcript read failed for %s", thread_id)
        raise HTTPException(status_code=502, detail=f"Codex task read failed: {exc}") from exc
    messages = messages_from_thread(native_thread, thread_id)
    metrics = thread_metrics(native_thread)
    metrics.update((await asyncio.to_thread(task_stats, [thread_id])).get(thread_id, {}))
    usage = {
        "input_tokens": metrics.get("total_input_tokens", 0),
        "output_tokens": metrics.get("total_output_tokens", 0),
        "total_cost_usd": 0,
    }
    return {
        "messages": messages, "has_more": False, "has_newer": False,
        "light": False, "max_session_seq": len(messages),
        "context_tokens": metrics.get("context_tokens", 0), "usage": usage,
        "manifest": {"external_authority": "codex", **metrics},
    }


@router.post("/codex/portal/threads/{session_id}/turns")
async def codex_portal_turn(session_id: str, body: CodexPortalTurnRequest, request: Request):
    uid = await _require_portal_admin(request, body.user_id)
    thread_id = _native_thread_id(session_id)
    await _portal_agent_config(uid, body.agent_id)
    from plugins.engines.codex.app_server import app_server
    from plugins.engines.codex.portal import messages_from_thread
    try:
        turn_result = await app_server.run_turn(thread_id, body.message.strip(), cwd=str(Path.cwd()), model=body.model, effort=body.effort, execution_mode=body.execution_mode)
        native_thread = await app_server.read_thread(thread_id)
    except Exception as exc:
        logger.exception("Codex Portal turn failed for %s", thread_id)
        raise HTTPException(status_code=502, detail=f"Codex task failed: {exc}") from exc
    messages = messages_from_thread(native_thread, thread_id)
    status = "queued" if (turn_result or {}).get("queued") else "ok"
    return {"status": status, "messages": messages, "max_session_seq": len(messages)}


@router.post("/codex/portal/threads/{session_id}/interrupt")
async def codex_portal_interrupt(session_id: str, body: CodexPortalInterruptRequest, request: Request):
    uid = await _require_portal_admin(request, body.user_id)
    thread_id = _native_thread_id(session_id)
    await _portal_agent_config(uid, body.agent_id)
    from plugins.engines.codex.app_server import app_server
    return {"status": "ok", "result": await app_server.interrupt(thread_id, body.turn_id)}

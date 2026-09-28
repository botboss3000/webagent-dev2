"""Combined boot aggregator — GET /api/v1/boot.

ONE round-trip that gathers the read-only payloads the web app fetches at page
boot, so the browser drains the boot burst in ~1 request instead of ~15 queued
behind the HTTP/1.1 six-socket-per-origin cap (the measured cold-load
bottleneck — see memory boot-perf-next-steps-plan + hybrid-vs-local-perf-audit).

This is a PURE ACCELERATOR, not a new behaviour:

  * Every section is OPTIONAL. If a section errors it is simply omitted, and the
    frontend falls back to its own per-endpoint fetch (a coalescer cache "miss"),
    so a broken section degrades to exactly today's behaviour — never a failed
    boot.
  * Each section is produced by calling the SAME handler function the individual
    endpoint uses, from within THIS request's context. So caller identity
    (resolved once by CallerIdentityMiddleware into a contextvar) and every
    per-endpoint authorization check run identically — there is zero duplicated
    or bypassed auth logic here.

The client side is ui/shared/js (index.html boot coalescer): it fires /boot once,
early, and primes the fetch-coalescer cache from `sections`, keyed by the URL each
module would otherwise fetch. See the coalescer script in index.html.

NOTE — calling FastAPI route functions directly (not via the router) means their
``= Query(default)`` parameters carry the Query() FieldInfo object as the default,
NOT the resolved value. So every parameter is passed EXPLICITLY below; do not rely
on a handler's declared default when calling it here.

Scope: stable-key boot reads with cooperative deadlines. Session transcripts and
per-agent abilities deliberately flow through their normal endpoints. They are
valuable prewarms, but neither is allowed to extend the aggregate response.
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request
from pydantic import BaseModel

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["boot"])

# /boot is an opportunistic accelerator. Yielding sections become cache misses
# at this deadline. The frontend also launches /boot only after shell readiness,
# because a timeout cannot preempt legacy synchronous work on the event loop.
BOOT_SECTION_DEADLINE_SECONDS = 0.75


def _plain(value):
    """Coerce a handler return (pydantic model or plain dict/list) to JSON-able."""
    if isinstance(value, BaseModel):
        return value.model_dump()
    return value


@router.get("/boot")
async def boot(request: Request):
    """Aggregate the read-only page-boot payloads into one response.

    Returns ``{ "sections": { <name>: <body> | (omitted on error) },
    "default_agent_id": <id|null> }``. Anonymous callers get only the public
    sections; user-scoped sections require a verified caller.
    """
    # Lazy imports keep this module free of import-time cycles (it is included
    # after all of these routers in app/main.py). Matches the in-handler import
    # style the aggregated handlers themselves use.
    from app.auth.identity import request_user_id
    from app.auth import access_mode as _access_mode, ui_config as _ui_config
    from app.api.features import get_app_prompts
    from app.api.chat import get_suggestions_config
    from app.api.agents import (
        get_abilities_catalog,
        get_pages_catalog,
        list_agents,
        get_user_profile,
    )
    from app.api.files import check_access
    from app.api.db_viewer import list_sessions
    from app.entitlements.service import resolve_capabilities

    try:
        uid = request_user_id(request) or ""
    except Exception:
        uid = ""

    # The session the UI is about to open (client sends it). Used to prime BOTH
    # that session's first transcript page and its bound agent's abilities.
    session_id = request.query_params.get("session_id") or ""

    sections: dict = {}

    async def _gather(name: str, awaitable):
        """Await one section; on ANY error drop it so the client re-fetches it."""
        try:
            sections[name] = _plain(await asyncio.wait_for(
                awaitable,
                timeout=BOOT_SECTION_DEADLINE_SECONDS,
            ))
        except TimeoutError:
            logger.debug("boot: section %r missed the %.2fs deadline", name,
                         BOOT_SECTION_DEADLINE_SECONDS)
        except Exception as e:  # noqa: BLE001 - best-effort by design
            logger.debug("boot: section %r skipped (%s)", name, e)

    # ── Public sections (no identity required) ──────────────────────────────
    tasks = [
        _gather("access_mode", _access_mode()),
        _gather("ui_config", _ui_config(request)),
        _gather("app_prompts", get_app_prompts()),
        _gather("suggestions_config", get_suggestions_config(request)),
        _gather("abilities_catalog", get_abilities_catalog(request)),
        _gather("pages_catalog", get_pages_catalog(request)),
        _gather("capabilities", resolve_capabilities(uid or "__anonymous__")),
    ]

    # ── User-scoped sections (only with a verified caller) ──────────────────
    # Every Query() param is passed explicitly — see module docstring.
    if uid:
        tasks += [
            _gather("check_access", check_access(request)),
            _gather("agents", list_agents(request, user_id=uid,
                                          include_system=False, view="active")),
            _gather("profile", get_user_profile(request, user_id=uid)),
            # list_sessions is async.  Running the function itself in a worker
            # thread merely returns an un-awaited coroutine, which later crashes
            # this endpoint when the chained section calls .get() on it.
            _gather("sessions", list_sessions(
                request, user_id=uid, db="user.db",
                agent_id=None, limit=50, include_hidden=False)),
        ]
    await asyncio.gather(*tasks)

    # Resolve default-agent metadata from sections already gathered. Per-agent
    # abilities are intentionally fetched by their normal endpoint. Priority:
    #   1. the agent bound to the session the UI will open (client sends
    #      ?session_id=) — this is exactly the agent whose abilities it will ask
    #      for, read straight off the sessions list we already loaded;
    #   2. the user's declared default agent;
    #   3. the provisioned WebAgent singleton, else the first agent.
    default_agent_id = None
    if uid:
        profile = sections.get("profile") or {}
        agents = (sections.get("agents") or {}).get("agents") or []
        agent_ids = {a.get("id") for a in agents}

        if session_id:
            for s in (sections.get("sessions") or {}).get("sessions") or []:
                if s.get("id") == session_id and s.get("agent_id"):
                    default_agent_id = s.get("agent_id")
                    break

        if not default_agent_id:
            cand = profile.get("default_agent_id")
            if cand and cand in agent_ids:
                default_agent_id = cand

        if not default_agent_id:
            # When shared default agent is enabled, prefer "shared_default" over
            # is_user_default / first-agent fallbacks.
            from app.admin.settings import shared_default_agent_enabled as _sd_boot
            if _sd_boot() and "shared_default" in agent_ids:
                default_agent_id = "shared_default"

        if not default_agent_id:
            for a in agents:
                if a.get("is_user_default"):
                    default_agent_id = a.get("id")
                    break
            if not default_agent_id and agents:
                default_agent_id = agents[0].get("id")

    return {"sections": sections, "default_agent_id": default_agent_id}

"""
Alternate agent engines — a drop-in plugin tree (sibling of plugins/abilities/).

An "engine" is an alternate runtime for an agent's turn. A normal agent runs
WebAgent's own LLM loop (app/agent/loop.py). An agent whose record carries
``metadata.engine = "<id>"`` instead hands its WHOLE turn to the matching engine
adapter in this package. The adapter yields the SAME event vocabulary the loop
yields (stream / tool_call / tool_result / agent_step_end / response / error) and
persists the same ``interactions`` rows, so the chat UI, streaming, and reload
are unchanged — only the brain differs.

Drop-in contract — add a NEW engine without touching the core:
  • Create a folder ``plugins/engines/<id>/`` whose package ``__init__`` exposes:
        ENGINE_ID = "<id>"                     # matches metadata.engine
        async def stream(**ctx): ...           # async generator of events
    (Keep the heavy adapter in its own module inside the folder and re-export
    those two names from the package __init__ — see ``claude_code/``.)
  • It is auto-discovered here. There is ONE generic dispatch in the loop
    (stream_agent_events) that looks an engine up by id — no per-engine `if`,
    no central registry list to edit.

This package __init__ is the only CORE part of the subsystem; the engines
themselves live in their own folders below it. See
docs/claude/production-editions.md for the drop-in philosophy.
"""

import importlib
import logging
import pkgutil
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Lazily-built {engine_id: stream_callable} (+ optional compact-hook / model-catalog
# maps). Populated on first lookup so the import cost is paid once and only when an
# alternate-engine agent actually runs.
_REGISTRY: Optional[Dict[str, Callable]] = None
_COMPACT_HOOKS: Optional[Dict[str, Callable]] = None
_MODEL_CATALOGS: Optional[Dict[str, Callable]] = None


def _discover() -> tuple:
    """Scan this package for engine folders/modules exposing ENGINE_ID + stream().

    Also collects an OPTIONAL ``compact_restart`` callable per engine — the /compact
    override an engine uses when its memory lives outside WebAgent (e.g. the Claude
    CLI's own session), to compact-and-restart instead of folding in place — and an
    OPTIONAL ``model_catalog`` callable per engine that returns the LIVE list of
    models + per-model reasoning levels the harness actually supports (the Codex
    engine reads `codex debug models`), so the chat footer shows real options."""
    reg: Dict[str, Callable] = {}
    hooks: Dict[str, Callable] = {}
    catalogs: Dict[str, Callable] = {}
    for mod in pkgutil.iter_modules(__path__):
        if mod.name.startswith("_"):
            continue
        try:
            m = importlib.import_module(f"{__name__}.{mod.name}")
        except Exception as e:  # a broken engine module must not break the loop
            logger.warning("engine module %s failed to import: %s", mod.name, e)
            continue
        eid = getattr(m, "ENGINE_ID", None)
        fn = getattr(m, "stream", None)
        if isinstance(eid, str) and eid and callable(fn):
            reg[eid] = fn
            hook = getattr(m, "compact_restart", None)
            if callable(hook):
                hooks[eid] = hook
            cat = getattr(m, "model_catalog", None)
            if callable(cat):
                catalogs[eid] = cat
        else:
            logger.debug("engine module %s missing ENGINE_ID/stream — ignored", mod.name)
    return reg, hooks, catalogs


def _ensure_discovered() -> None:
    global _REGISTRY, _COMPACT_HOOKS, _MODEL_CATALOGS
    if _REGISTRY is None:
        _REGISTRY, _COMPACT_HOOKS, _MODEL_CATALOGS = _discover()


def get_engine_stream(engine_id: str) -> Optional[Callable]:
    """Return the async-generator ``stream(**ctx)`` for an engine id, or None
    when no such engine is installed (caller then falls back to the normal loop)."""
    _ensure_discovered()
    return (_REGISTRY or {}).get((engine_id or "").strip())


def get_engine_compact_hook(engine_id: str) -> Optional[Callable]:
    """Return an engine's optional /compact override, or None when it has none.

    Signature: ``async compact_restart(db, user_id, session_id, agent_id, info) -> str``
    — returns the user-facing chat reply. Lets an engine redefine what /compact does
    (the Claude Code engine uses it to compact-and-restart its CLI session). The
    core /compact handler calls this generically; no per-engine ``if`` here."""
    _ensure_discovered()
    return (_COMPACT_HOOKS or {}).get((engine_id or "").strip())


def get_engine_model_catalog(engine_id: str) -> Optional[Callable]:
    """Return an engine's optional model-catalog provider, or None when it has none.

    Signature: ``model_catalog() -> Optional[List[dict]]`` — each entry at least
    ``{"v": model-id, "label": ..., "sub": ...}`` plus optional per-model
    ``efforts`` and ``default_effort``. The chat footer calls this generically so
    alternate-engine agents show the harness's REAL model/effort options without
    an admin maintaining them; None/[] means "use the curated fallback"."""
    _ensure_discovered()
    return (_MODEL_CATALOGS or {}).get((engine_id or "").strip())

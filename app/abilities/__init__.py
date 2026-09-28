"""Host-ability catalog — the drop-in registry for agent abilities.

An **ability** is a host-side capability the agent admin can grant to an agent
(Codebase Admin, Web Access, Terminal Control, Wiki Control, …). Abilities are
the drop-in unit for everything the agent can do — tool-gating abilities, OAuth
credential providers, messaging channels, and placeholder coming-soon entries.

╔══════════════════════════════════════════════════════════════════════════╗
║  THIS FILE IS CORE.  Abilities themselves are NOT defined here.           ║
║  Each ability is a folder under ``plugins/abilities/`` containing a       ║
║  ``<id>.json`` descriptor and (optionally) a ``<id>.py`` runtime module.  ║
║  Groups are derived from folder names; group styling comes from            ║
║  ``_group.json`` files. You do NOT edit this manager, app/api/agents.py,  ║
║  app/tools/loader.py, ui/js/app-config.js, or ui/js/agents.js when adding ║
║  an ability. They all read whatever this manager discovers.               ║
║  See CLAUDE.md ("Core vs. plugins") and docs/claude/production-editions.md.║
╚══════════════════════════════════════════════════════════════════════════╝

Ability descriptor JSON contract (``plugins/abilities/<group>/<id>.json``)
--------------------------------------------------------------------------
{
  "display_name": "Web Access",
  "kind": "ability",          // ability | channel | oauth | credential | placeholder
  "icon": "globe",            // Lucide icon name
  "color": "#7aa2f7",         // accent colour
  "description": "Lets the agent search the web, …",
  "entitlement_group": "web_read", // reviewed product-capability group
  "risk_class": "read",            // informational: low | read | write | admin
  "action_class": "read",          // read | write | external_action | mixed
  "required_integration": null,     // optional backend/integration identifier
  "note": null,               // optional one-line config note
  "simple": true,             // true = toggle directly; false = needs config panel
  "placeholder": false,       // true = grey "Coming Soon" row, no toggle
  "virtual": false,           // optional — true = display-only row that LISTS
                              //   always-on core tools (e.g. Core ▸ Base) for
                              //   permission management. Owns no .py runtime, is
                              //   kept OUT of tools_map()/ABILITY_TOOLS (gates
                              //   nothing), and is NOT coerced to a placeholder
                              //   despite having no runtime. Pair with
                              //   locked_on+protected. See virtual_ability_for_tool.
  "default_enabled": false,   // optional — on-by-default at the app level when an
                              //   admin has made no explicit choice. Behavioural
                              //   always-on abilities set true; credentialed /
                              //   destructive ones omit it (→ off until enabled).
                              //   The stored choice lives in
                              //   data/config/agent-abilities.json.
  "order": 10,                // optional — SEED position in the ability table. On
                              //   first boot it is snapshotted into the order
                              //   section of data/config/agent-abilities.json,
                              //   which is then the live source an admin can edit;
                              //   the descriptor value only positions a brand-new
                              //   drop-in until then. Lower sorts first; ties break
                              //   on display name. Default 100.
  "tools": ["web_search", "get_weather", "maps_geocode"],
  "config": {                 // optional — only when simple=false or ability has extra data
    "settings": [ ... ]
  }
}

Group descriptor JSON contract (``plugins/abilities/<group>/_group.json``)
--------------------------------------------------------------------------
{
  "name": "Web",
  "icon": "globe",
  "color": "#7aa2f7",
  "desc": "Reach the open web — search, browser, scraping, cookies.",
  "order": 3                  // SEED group position — like a per-ability "order",
                              //   snapshotted into data/config/agent-abilities.json
                              //   on first boot and editable there afterwards.
}
If _group.json is missing, the group gets emergent defaults: folder name
as-is for display, neutral icon/colour, no description, sorted alphabetically
after all numbered groups.

Tool handlers can live EITHER in core (the classic ability just declares which
tool *names* it unlocks, and the handlers sit in app/tools/core_tools.py and
friends) OR in the ability's .py file — a SELF-CONTAINED ability ships its own
handlers via ``build_tools()``, ``TOOL_SCHEMAS``, and ``DESTRUCTIVE``.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_ABILITIES_DIR = Path(__file__).resolve().parents[2] / "plugins" / "abilities"

# Background APP FUNCTIONS live OUTSIDE the abilities tree (they are not
# agent-facing abilities): plugins/app_functions/<function>/. Entries there are
# forced kind="app_function" / app_function=True so they render in App Settings
# ▸ App Functions and never in the two ability tables. See _load().
_APP_FUNCTIONS_DIR = Path(__file__).resolve().parents[2] / "plugins" / "app_functions"

# ── Emergent group defaults (when _group.json is absent) ─────────────────────
_EMERGENT_GROUP_ICON = "layers"
_EMERGENT_GROUP_COLOR = "#9aa5ce"
_EMERGENT_GROUP_ORDER = 100

# ── Warning threshold for group id collisions ────────────────────────────────
_COLLIDING_GROUPS: Dict[str, List[str]] = {}  # normalized_id → [folder_names]


def _normalize_group_id(folder_name: str) -> str:
    """'Agent Admin' → 'agent_admin', 'core' → 'core'."""
    import re
    return re.sub(r'[-\s]+', '_', folder_name.strip().lower())


def _load_group_meta(group_dir: Path, folder_name: str) -> Dict[str, Any]:
    """Read _group.json if it exists, otherwise return emergent defaults."""
    gjson = group_dir / "_group.json"
    if gjson.is_file():
        try:
            raw = json.loads(gjson.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                return {
                    "name": raw.get("name") or folder_name,
                    "icon": raw.get("icon") or _EMERGENT_GROUP_ICON,
                    "color": raw.get("color") or _EMERGENT_GROUP_COLOR,
                    "desc": raw.get("desc") or "",
                    "order": raw.get("order", _EMERGENT_GROUP_ORDER),
                    "entitlement_group": raw.get("entitlement_group"),
                    "risk_class": raw.get("risk_class"),
                    "action_class": raw.get("action_class"),
                    "required_integration": raw.get("required_integration"),
                }
        except Exception as e:
            logger.warning("Failed to parse _group.json in %s: %s", group_dir, e)
    # Emergent defaults
    return {
        "name": folder_name,
        "icon": _EMERGENT_GROUP_ICON,
        "color": _EMERGENT_GROUP_COLOR,
        "desc": "",
        "order": _EMERGENT_GROUP_ORDER,
        "entitlement_group": None,
        "risk_class": None,
        "action_class": None,
        "required_integration": None,
    }


def _load_ability_json(json_path: Path) -> Optional[Dict[str, Any]]:
    """Read an ability descriptor JSON. Returns None on failure."""
    try:
        raw = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return raw
    except Exception as e:
        logger.warning("Failed to parse ability descriptor %s: %s", json_path, e)
        return None


# ── Caches ───────────────────────────────────────────────────────────────────
# _CATALOG: {ability_id: {all descriptor fields + group, has_runtime, _py_path}}
# _GROUP_META: {normalized_group_id: {name, icon, color, desc, order}}
# _MODULE_CACHE: {ability_id: loaded Python module}
_CATALOG: Optional[Dict[str, Dict[str, Any]]] = None
_GROUP_META: Dict[str, Dict[str, Any]] = {}
_MODULE_CACHE: Dict[str, Any] = {}


def _build_catalog_entry(
    ability_id: str,
    sub_dir: Path,
    gid: str,
    desc: Dict[str, Any],
    group_meta: Optional[Dict[str, Any]] = None,
    is_app_function_dir: bool = False,
) -> Dict[str, Any]:
    """Build one catalog entry from a descriptor + its directory.

    ``is_app_function_dir`` marks entries discovered under
    ``plugins/app_functions/`` — they are forced ``kind="app_function"`` and
    ``app_function=True`` regardless of what the descriptor says, so a
    background app service can never leak into the ability tables as an
    agent-facing ability.
    """
    py_path = sub_dir / f"{ability_id}.py"
    has_runtime = py_path.is_file()

    # Determine kind and placeholder status
    kind = desc.get("kind", "ability")
    # A "virtual" ability lists always-on CORE tools (load_tool, get_time,
    # memory, …) for display + permission management only. It owns no
    # runtime and gates nothing — its tools are wired into every agent by
    # the loader regardless — so it is NOT a coming-soon placeholder even
    # without a .py, and it is deliberately kept OUT of tools_map() /
    # ABILITY_TOOLS so the runtime never withholds these tools via ability
    # gating. See base (plugins/abilities/Core/base) and virtual_ability_for_tool.
    is_virtual = bool(desc.get("virtual", False))
    is_placeholder = bool(desc.get("placeholder", False))
    # An app_function is a background app SERVICE, not an agent tool. Some
    # of them (the wired-in core singletons — scheduler, watchdog, sync
    # engine, …) own no ability runtime at all: they are just a descriptor
    # that gives an admin a toggle in App Settings ▸ App Functions, and the
    # matching core service reads ``app_function_enabled(id)`` at boot. So —
    # like a virtual row — an app_function is intentionally runtime-less and
    # must NOT be coerced to a "coming soon" placeholder for lacking a .py.
    is_app_function = is_app_function_dir or bool(desc.get("app_function", False))
    # Only ability-kind requires a .py; oauth/credential/channel can work
    # without one, and a virtual / app_function ability is intentionally
    # runtime-less.
    if (not is_placeholder and not is_virtual and not is_app_function
            and kind == "ability" and not has_runtime):
        is_placeholder = True
    if is_placeholder:
        kind = "placeholder"
    # Entries from the app_functions tree are never agent-facing abilities.
    if is_app_function_dir:
        kind = "app_function"

    group_meta = group_meta or {}
    return {
        "id": ability_id,
        "display_name": desc.get("display_name") or ability_id,
        "kind": kind,
        "icon": desc.get("icon") or "plug",
        "color": desc.get("color") or "#7aa2f7",
        "description": desc.get("description") or "",
        "entitlement_group": desc.get("entitlement_group") or group_meta.get("entitlement_group"),
        "risk_class": desc.get("risk_class") or group_meta.get("risk_class"),
        "action_class": desc.get("action_class") or group_meta.get("action_class"),
        "required_integration": desc.get("required_integration") or group_meta.get("required_integration"),
        "note": desc.get("note"),
        "simple": bool(desc.get("simple", True)),
        # Marks a behavioural, pick-one context-management strategy
        # (compaction / window / retrieval). The resolver filters on this
        # before loading any module. See context_strategy_for_agent.
        "context_strategy": bool(desc.get("context_strategy", False)),
        # Safety device: an always-on ability whose toggle is fixed in
        # the ON position and cannot be turned off (e.g. Context Control,
        # which keeps long chats from running away). Forced enabled at
        # both the app and per-agent level; the two ability panels render
        # its toggle locked. See get_agent_connections / disable_ability /
        # context_strategy_for_agent for the enforcement points.
        "locked_on": bool(desc.get("locked_on", False)),
        "placeholder": is_placeholder,
        "status": desc.get("status") or "stable",
        "protected": bool(desc.get("protected", False)),
        # App function: a BACKGROUND app service, not an agent-invoked
        # ability. The agent never chooses to use it — it runs
        # automatically for the app itself (e.g. Session Namer auto-titles
        # chats, Context Control's compaction train, the Render Recorder
        # flight recorder). Purely a UI-classification flag: an app_function
        # is rendered in App Settings ▸ App Functions instead of the two
        # ability tables, and is excluded from ui_catalog()'s abilities +
        # group members. It changes NOTHING at runtime — tool-building,
        # context-strategy resolution, turn hooks and background services
        # all still read this same catalog and key off locked_on /
        # context_strategy / enabled exactly as before. See ui_catalog().
        "app_function": is_app_function,
        # A small class of safety services has two legitimate control surfaces:
        # app-wide limits in App Functions and per-agent preferences in the
        # Abilities tab.  They remain app functions at runtime, but are also
        # included in the agent-facing catalog when this flag is set.
        "agent_configurable": bool(desc.get("agent_configurable", False)),
        # On-by-default at the app level: an ability with no explicit
        # admin toggle stored in data/config/agent-abilities.json falls
        # back to this. SHIP POLICY — abilities are ON by default, so a
        # descriptor that omits the flag bakes True (fresh installs and
        # newly dropped-in abilities are unlocked with no hand-toggling).
        # An ability that must ship OFF opts out with an explicit
        # "default_enabled": false. (Credentialed abilities stay hidden by
        # the separate secret-present gate until their key is supplied.)
        "default_enabled": bool(desc.get("default_enabled", True)),
        "order": desc.get("order", 100),
        # Display-only ability: lists always-on core tools for management
        # but owns no runtime and gates nothing (see is_virtual above).
        "virtual": is_virtual,
        "tools": list(desc.get("tools") or []),
        "tool_metadata": desc.get("tool_metadata") or {},
        "config": desc.get("config"),
        # Optional generic agent-panel extension. Core passes this opaque id to
        # the shared Abilities renderer; the renderer maps known panel ids to a
        # reusable editor. Runtime/API ownership remains in the drop-in ability.
        "ui_panel": desc.get("ui_panel"),
        # Declarative credential needs (the common-credential framework,
        # see app/abilities/credentials.py). When present, the ability's
        # secrets are stored/read/gated generically — no bespoke endpoint.
        "credentials": desc.get("credentials"),
        # Bundled-skill metadata (the body lives inline or in a sibling
        # <id>.skill.md). skill_summary is the always-shown "when to use
        # it" catalog line; without it a JSON ability's skill would have
        # a blank summary. skill_mode: selectable (load-on-demand) or
        # always_on. See app/agent/ability_skills.py.
        "skill_summary": desc.get("skill_summary") or "",
        "skill_mode": desc.get("skill_mode") or "selectable",
        "skill_handle": desc.get("skill_handle"),
        "group": gid,
        "has_runtime": has_runtime,
        "_py_path": py_path if has_runtime else None,
    }


def _load(force: bool = False) -> Dict[str, Dict[str, Any]]:
    """Walk ``plugins/abilities/*/`` and build the full ability catalog.

    Returns {ability_id: {descriptor fields + group, has_runtime, _py_path}}.
    Populates _GROUP_META from _group.json files (or emergent defaults).
    """
    global _CATALOG, _GROUP_META
    if _CATALOG is not None and not force:
        return _CATALOG

    cat: Dict[str, Dict[str, Any]] = {}
    groups: Dict[str, Dict[str, Any]] = {}
    _COLLIDING_GROUPS.clear()

    if not _ABILITIES_DIR.is_dir():
        logger.warning("Abilities dir not found: %s", _ABILITIES_DIR)
        _CATALOG = cat
        _GROUP_META = groups
        return cat

    for group_dir in sorted(_ABILITIES_DIR.iterdir()):
        if not group_dir.is_dir():
            continue
        if group_dir.name.startswith("_") or group_dir.name == "__pycache__":
            continue

        folder_name = group_dir.name
        gid = _normalize_group_id(folder_name)

        # Detect collisions
        if gid in groups and gid not in _COLLIDING_GROUPS:
            existing = _COLLIDING_GROUPS.setdefault(gid, [])
            # Find the original folder name
            for gid2, meta2 in groups.items():
                if gid2 == gid:
                    existing.append(meta2.get("_folder", folder_name))
                    break
        if gid in groups:
            _COLLIDING_GROUPS.setdefault(gid, []).append(folder_name)

        # Load group meta (first _group.json wins in collision)
        if gid not in groups:
            meta = _load_group_meta(group_dir, folder_name)
            meta["_folder"] = folder_name
            groups[gid] = meta

        # Scan ability subdirectories (one per ability)
        for sub_dir in sorted(group_dir.iterdir()):
            if not sub_dir.is_dir():
                continue
            if sub_dir.name.startswith("_") or sub_dir.name == "__pycache__":
                continue

            ability_id = sub_dir.name
            json_path = sub_dir / f"{ability_id}.json"
            if not json_path.is_file():
                continue  # no descriptor inside — not a valid ability

            desc = _load_ability_json(json_path)
            if desc is None:
                continue

            cat[ability_id] = _build_catalog_entry(
                ability_id, sub_dir, gid, desc, groups.get(gid), is_app_function_dir=False
            )

    # ── App-function tree: plugins/app_functions/<function>/ (flat) ──
    # A background APP FUNCTION (Session Namer, …) is not an agent ability — it
    # lives outside the abilities tree and is forced kind="app_function" so the
    # two ability tables never list it. It renders in App Settings ▸ App
    # Functions via the app_function flag, exactly like the descriptor-only
    # System app functions that still live in the abilities tree.
    if _APP_FUNCTIONS_DIR.is_dir():
        for sub_dir in sorted(_APP_FUNCTIONS_DIR.iterdir()):
            if not sub_dir.is_dir():
                continue
            if sub_dir.name.startswith("_") or sub_dir.name == "__pycache__":
                continue

            ability_id = sub_dir.name
            json_path = sub_dir / f"{ability_id}.json"
            if not json_path.is_file():
                continue  # no descriptor inside — not a valid app function

            desc = _load_ability_json(json_path)
            if desc is None:
                continue

            cat[ability_id] = _build_catalog_entry(
                ability_id, sub_dir, "app_functions", desc, groups.get("app_functions"),
                is_app_function_dir=True,
            )
            # Flat dir → give it an emergent group so group bookkeeping (pure
            # app-function group suppression in ui_catalog) stays consistent.
            groups.setdefault("app_functions", {
                "name": "App Functions",
                "icon": _EMERGENT_GROUP_ICON,
                "color": _EMERGENT_GROUP_COLOR,
                "desc": "Background app services (not agent abilities)",
                "order": _EMERGENT_GROUP_ORDER,
                "_folder": "app_functions",
            })

    # Log collisions
    for gid, folders in _COLLIDING_GROUPS.items():
        logger.warning(
            "Group id collision: '%s' from folders %s — merged into one group",
            gid, folders
        )

    _CATALOG = cat
    _GROUP_META = groups
    return cat


def reload() -> None:
    """Drop caches so a newly-dropped ability/group is picked up."""
    global _CATALOG, _GROUP_META, _CONFIRM_TOOLS_CACHE
    _CATALOG = None
    _GROUP_META.clear()
    _MODULE_CACHE.clear()
    _CONFIRM_TOOLS_CACHE = None
    _load(force=True)


# ── Lazy Python module loading ───────────────────────────────────────────────

def ability_module(ability_id: str) -> Optional[Any]:
    """Return the executed plugin module for an ability id (or None). Lazy-loads
    on first call so the catalog scan stays cheap."""
    if ability_id in _MODULE_CACHE:
        return _MODULE_CACHE.get(ability_id)

    cat = _load()
    entry = cat.get(ability_id)
    if not entry or not entry.get("_py_path"):
        return None

    py_path = entry["_py_path"]
    try:
        spec = importlib.util.spec_from_file_location(
            f"plugins.abilities.{entry['group']}.{ability_id}", py_path
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        # Register under the spec name BEFORE executing. A dynamically loaded
        # module that is absent from sys.modules cannot have its own namespace
        # resolved later — which breaks any Pydantic model defined in it (e.g. a
        # request body on the ability's `router`): FastAPI leaves the body
        # TypeAdapter a deferred "mock" and validation fails at request time with
        # "not fully defined". Inserting it first (the documented importlib
        # pattern) lets those models finalize. The synthetic dotted name is
        # unique per ability and never collides with a real import path.
        sys.modules[spec.name] = mod
        try:
            spec.loader.exec_module(mod)
        except Exception:
            sys.modules.pop(spec.name, None)
            raise
        _MODULE_CACHE[ability_id] = mod
        return mod
    except Exception as e:
        logger.error("Failed to load ability module %s: %s", ability_id, e)
        return None


def background_service_hooks() -> List[Dict[str, Any]]:
    """Discover abilities that expose a background service. Returns {id, start, stop}
    for every ability module defining an async ``start_background``."""
    _load()
    hooks: List[Dict[str, Any]] = []
    for aid, entry in _load().items():
        if not entry.get("has_runtime"):
            continue
        mod = ability_module(aid)
        if mod is None:
            continue
        start = getattr(mod, "start_background", None)
        if callable(start):
            hooks.append({
                "id": aid,
                "start": start,
                "stop": getattr(mod, "stop_background", None),
            })
    return hooks


def ability_routers() -> List[Dict[str, Any]]:
    """Discover abilities that ship their own FastAPI endpoints. Returns
    {id, router} for every ability module exposing a module-level ``router``
    (and/or a ``ROUTERS`` list of them).

    The drop-in twin of :func:`background_service_hooks`: an ability that needs
    HTTP routes (e.g. a browser-intake + admin-read API) declares a ``router`` in
    its plugin file and core's generic mount loop (see app/main.py "ability
    router") includes it — with **no** ``include_router`` line per ability. Drop
    the folder in, its routes appear; delete it, they vanish. Router discovery is
    independent of the app-enable toggle (mirrors the always-mounted page/service
    hooks); an excluded edition strips the folder entirely instead."""
    _load()
    out: List[Dict[str, Any]] = []
    for aid, entry in _load().items():
        if not entry.get("has_runtime"):
            continue
        mod = ability_module(aid)
        if mod is None:
            continue
        single = getattr(mod, "router", None)
        if single is not None:
            out.append({"id": aid, "router": single})
        many = getattr(mod, "ROUTERS", None)
        if isinstance(many, (list, tuple)):
            for r in many:
                if r is not None:
                    out.append({"id": aid, "router": r})
    return out


async def turn_hooks_for_agent(agent_id: str) -> list:
    """Discover abilities enabled for this agent that export a TURN_HOOK.

    Returns a list of async callables with the signature:
        async hook(db, user_id, session_id, emit)
    """
    if not agent_id:
        return []
    cat = _load()
    enabled: set = set()
    try:
        from app.db import get_db
        db = get_db()
        if hasattr(db, "get_agent_connections"):
            rows = await db.get_agent_connections(agent_id)
            enabled = {r["connection_type"] for r in rows
                       if r.get("section") == "ability" and r.get("enabled")}
    except Exception as e:
        # Do NOT abort here: a locked-on hook (e.g. the Session Namer) is a safety
        # device that must run on EVERY turn even if the per-agent connection
        # lookup momentarily fails (e.g. transient DB contention). Bailing out with
        # [] used to silently skip the locked-on union below — the root cause of the
        # namer never firing. Log it and fall through with whatever we resolved.
        logger.warning(
            "turn_hooks_for_agent: connection lookup failed for %s (%s); "
            "continuing with locked-on hooks only", agent_id, e)

    # A locked-on hook ability (safety device) always runs, even if no per-agent
    # row enabled it — that's the whole point of "cannot be turned off". Union it
    # into the enabled set before resolving (mirrors context_strategy_for_agent).
    locked_on = {aid for aid, e in cat.items() if e.get("locked_on")}

    hooks = []
    for aid in (enabled | locked_on):
        mod = ability_module(aid)
        if mod is None:
            continue
        hook = getattr(mod, "TURN_HOOK", None)
        if callable(hook):
            hooks.append(hook)

    # ── Session Namer turn hook (an APP FUNCTION, gated at the app level) ──
    # The namer auto-titles a chat after its first few turns. It carries no
    # agent tool and no per-agent connection row, so it can't ride the enabled|
    # locked_on loop above — it's toggled once, app-wide, from App Settings ▸ App
    # Functions (stored in agent-abilities.json). Dispatch it only when that
    # toggle resolves ON (ships ON by default). ``session_titler`` isn't in the
    # ``enabled|locked_on`` set precisely because it's an app_function, not an
    # agent-facing ability. Its runtime lives OUTSIDE the abilities tree:
    # plugins/app_functions/session_titler/.
    try:
        if app_function_enabled("session_titler"):
            from plugins.app_functions.session_titler.session_titler import (
                TURN_HOOK as _session_namer_hook,
            )
            if callable(_session_namer_hook):
                hooks.append(_session_namer_hook)
    except Exception:
        logger.debug("session_titler not available", exc_info=True)

    return hooks


async def prompt_context_for_agent(
    agent_id: str, user_id: str, query: str,
) -> str:
    """Collect bounded turn-context snippets from enabled drop-in abilities.

    An ability opts in by exporting an async ``build_prompt_context`` hook. The
    hook is called only when the same enabled/configured-provider resolution used
    by tool loading grants that ability to this agent. Failures are isolated so
    an optional knowledge source can never block the chat turn.
    """
    if not agent_id or not (query or "").strip():
        return ""
    try:
        from app.integrations import gather_enabled_providers
        enabled = await gather_enabled_providers(agent_id, user_id) or set()
    except Exception:
        return ""

    sections: List[str] = []
    for ability_id in sorted(enabled):
        try:
            mod = ability_module(ability_id)
            hook = getattr(mod, "build_prompt_context", None) if mod is not None else None
            if not callable(hook):
                continue
            value = await hook(
                agent_id=agent_id, user_id=user_id, query=query,
            )
            if isinstance(value, str) and value.strip():
                sections.append(value.strip())
        except Exception:
            logger.debug(
                "prompt context hook failed for ability %s", ability_id,
                exc_info=True,
            )
    return "\n\n".join(sections)


async def context_strategy_for_agent(agent_id: str):
    """Return the single enabled context-management strategy module for an agent.

    A context strategy decides how the stored transcript is shaped into the
    message list sent to the model (compaction, sliding window, retrieval, …)
    and surfaces the context-fill gauge. Its runtime marks itself with the
    module attribute ``CONTEXT_STRATEGY = True`` (mirroring the ``TURN_HOOK``
    attribute pattern). Strategies are **pick-one**: if more than one is enabled
    we deterministically choose the lowest ``order`` (ties broken by id) and warn,
    rather than stacking two compactors. Returns ``None`` when none is enabled —
    callers then fall back to the default (no shaping), exactly as before.
    """
    if not agent_id:
        return None
    cat = _load()
    try:
        from app.db import get_db
        db = get_db()
        if not hasattr(db, "get_agent_connections"):
            return None
        rows = await db.get_agent_connections(agent_id)
        enabled = {r["connection_type"] for r in rows
                   if r.get("section") == "ability" and r.get("enabled")}
    except Exception:
        return None

    # A locked-on context strategy (safety device) is always a candidate, even
    # if no per-agent row enabled it — that's the whole point of "cannot be
    # turned off". Union it into the enabled set before resolving.
    locked_on = {aid for aid, e in cat.items()
                 if e.get("context_strategy") and e.get("locked_on")}

    candidates = []
    for aid in (enabled | locked_on):
        entry = cat.get(aid) or {}
        if not entry.get("context_strategy"):
            continue  # cheap descriptor filter — avoids loading unrelated modules
        mod = ability_module(aid)
        if mod is None or not getattr(mod, "CONTEXT_STRATEGY", False):
            continue  # descriptor claims it but the runtime doesn't honour it
        candidates.append((entry.get("order", 100), aid, mod))

    if not candidates:
        return None
    candidates.sort(key=lambda c: (c[0], c[1]))
    if len(candidates) > 1:
        logger.warning(
            "Multiple context strategies enabled for agent %s (%s); using '%s'.",
            agent_id, [c[1] for c in candidates], candidates[0][1],
        )
    return candidates[0][2]


# ── Skill resolution ─────────────────────────────────────────────────────────

def _resolve_skill_body(entry: Dict[str, Any], ability_id: str) -> str:
    """Read an ability's legacy root ``<ability_id>.skill.md`` lead unchanged."""
    path = skill_file_path(ability_id)
    if not path:
        return ""
    try:
        return path.read_text(encoding="utf-8").strip() if path.is_file() else ""
    except Exception as e:
        logger.warning("Could not read ability skill file %s: %s", path, e)
        return ""


_GENERAL_SKILL_SUFFIX = ".skill.md"
_GENERAL_SKILL_ID = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_GENERAL_SKILL_FM = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def _parse_general_skill_file(path: Path) -> Optional[Dict[str, Any]]:
    """Parse one added ``skills/*.skill.md`` document without a YAML dependency.

    Added skills own their selection metadata so adding a skill never requires a
    central catalog edit. Values are deliberately scalar-only: this is a small,
    auditable frontmatter contract rather than a general YAML interpreter.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception as e:
        logger.warning("Could not read ability skill file %s: %s", path, e)
        return None
    match = _GENERAL_SKILL_FM.match(raw)
    if not match:
        logger.warning("Ignoring %s: added ability skills require YAML frontmatter", path)
        return None
    meta: Dict[str, str] = {}
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        meta[key] = value
    skill_id = meta.get("id", "")
    title = meta.get("title", "")
    when = meta.get("when_to_use", "")
    body = match.group(2).strip()
    if not (_GENERAL_SKILL_ID.fullmatch(skill_id) and title and when and body):
        logger.warning("Ignoring %s: id, title, when_to_use, and a body are required", path)
        return None
    raw_mode = meta.get("default_mode", "discoverable").lower()
    mode = {"always_on": "visible", "always": "visible", "visible": "visible",
            "selectable": "discoverable", "discoverable": "discoverable"}.get(raw_mode)
    if not mode:
        logger.warning("Ignoring %s: default_mode must be visible or discoverable", path)
        return None
    access = meta.get("minimum_access", "everyone").lower()
    if access not in {"everyone", "registered", "admin"}:
        logger.warning("Ignoring %s: minimum_access must be everyone, registered, or admin", path)
        return None
    return {"skill_id": skill_id, "title": title, "when_to_use": when,
            "body": body, "default_mode": mode, "minimum_access": access,
            "path": path}


def ability_general_skills(ability_id: str) -> List[Dict[str, Any]]:
    """Generate an ability's root and directory-discovered general skill records.

    The legacy root is implicit ``core`` and deliberately retains its JSON
    ``skill_summary`` description. Added files are discovered from ``skills/`` and
    are fully self-describing via YAML frontmatter; no manifest is maintained.
    """
    entry = _load().get(ability_id)
    if not entry:
        return []
    root = skill_file_path(ability_id)
    records: List[Dict[str, Any]] = []
    body = _resolve_skill_body(entry, ability_id)
    if body:
        records.append({"skill_id": "core", "skill_key": f"{ability_id}:core",
                        "title": entry.get("display_name") or ability_id,
                        "when_to_use": (entry.get("skill_summary") or entry.get("summary") or "").strip(),
                        "body": body, "default_mode": entry.get("skill_mode") or "selectable",
                        "minimum_access": "everyone", "path": root, "legacy_root": True})
    directory = root.parent / "skills" if root else None
    if directory and directory.is_dir():
        seen = {"core"}
        for path in sorted(directory.glob(f"*{_GENERAL_SKILL_SUFFIX}")):
            record = _parse_general_skill_file(path)
            if not record:
                continue
            if record["skill_id"] in seen:
                logger.warning("Ignoring duplicate general skill id '%s' in %s", record["skill_id"], path)
                continue
            seen.add(record["skill_id"])
            record["skill_key"] = f"{ability_id}:{record['skill_id']}"
            records.append(record)
    return records


# ── Per-tool skill files ─────────────────────────────────────────────────────
# An ability owns one root lead, ``<ability_id>.skill.md``, and one optional
# Markdown document per runtime-gated tool:
#     tools/<tool_name>.skill.md
# The root is always ability-level guidance. There is intentionally no parser for
# legacy ``## tool:...`` headings: a heading is ordinary root Markdown and cannot
# create a deferred mini-skill. Authoring rules live in plugins/abilities/README.md.

_TOOL_SKILL_SUFFIX = ".skill.md"
# (ability_id, unknown filename set) already warned about.
_SECTION_WARNED: set = set()


def tool_skill_file_path(ability_id: str, tool: str) -> Optional[Path]:
    """Canonical ``tools/<tool_name>.skill.md`` path for one ability tool."""
    root = skill_file_path(ability_id)
    if root is None or not tool:
        return None
    return root.parent / "tools" / f"{tool}{_TOOL_SKILL_SUFFIX}"


def _resolve_tool_skill_sections(entry: Dict[str, Any], ability_id: str) -> Dict[str, str]:
    """Read exact-name per-tool skill files beneath an ability's ``tools/`` dir."""
    root = skill_file_path(ability_id)
    if root is None:
        return {}
    directory = root.parent / "tools"
    if not directory.is_dir():
        return {}
    sections: Dict[str, str] = {}
    for path in sorted(directory.glob(f"*{_TOOL_SKILL_SUFFIX}")):
        name = path.name[:-len(_TOOL_SKILL_SUFFIX)]
        if not name:
            continue
        try:
            sections[name] = path.read_text(encoding="utf-8").strip()
        except Exception as e:
            logger.warning("Could not read tool skill file %s: %s", path, e)
    return sections


def _warn_unknown_tool_sections(ability_id: str, entry: Dict[str, Any],
                                sections: Dict[str, str]) -> None:
    """Log once when a tool-skill filename names no descriptor tool.

    Tests enforce the stronger runtime ``ABILITY_TOOLS`` invariant. This local
    warning remains dependency-cycle-safe while catalog discovery is running.
    """
    declared = {t for t in (entry.get("tools") or []) if t}
    if not declared or not sections:
        return
    unknown = sorted(n for n in sections if n not in declared)
    if not unknown:
        return
    key = (ability_id, tuple(unknown))
    if key in _SECTION_WARNED:
        return
    _SECTION_WARNED.add(key)
    logger.warning(
        "Ability '%s': tool skill file(s) %s name no descriptor tool (declared: %s). "
        "Those files will never be delivered — check tools/<tool_name>.skill.md names.",
        ability_id, ", ".join(unknown), ", ".join(sorted(declared)),
    )


def ability_tool_skill_section(ability_id: str, tool: str) -> str:
    """The per-tool mini-skill an ability declares for ``tool`` ("" when none).

    Delivered by the tool loader when ``tool`` activates, so the agent gets the
    specialised how-to exactly when the tool becomes callable."""
    feat = ability_feature_with_skill(ability_id)
    if not feat:
        return ""
    return ((feat.get("skill_tool_sections") or {}).get(tool) or "").strip()


def ability_feature_with_skill(ability_id: str) -> Optional[Dict[str, Any]]:
    """Return an ability entry with root, added general, and per-tool skills.

    The root ``<ability_id>.skill.md`` remains the compatibility lead. Added
    general skills live under ``skills/``; per-tool mini-skills remain isolated
    under ``tools/`` and are delivered only with their exact tool.
    """
    entry = _load().get(ability_id)
    if not entry:
        return None
    general = ability_general_skills(ability_id)
    body = next((s["body"] for s in general if s.get("legacy_root")), "")
    sections = _resolve_tool_skill_sections(entry, ability_id)
    if not general and not sections:
        return None
    if sections:
        _warn_unknown_tool_sections(ability_id, entry, sections)
    return {
        **entry,
        "skill": body,
        "skill_full": body,
        "general_skills": general,
        "skill_tool_sections": sections,
    }


def skill_file_path(ability_id: str) -> Optional[Path]:
    """The canonical root ``<id>.skill.md`` path inside an ability folder.

    Returned whether or not the file exists, so callers can create it on first
    save. The filesystem is the sole skill source under the tool-file convention.
    """
    entry = _load().get(ability_id)
    if not entry:
        return None
    group = entry.get("group")
    if not group:
        return None
    folder_name = None
    for gid, meta in _GROUP_META.items():
        if gid == group:
            folder_name = meta.get("_folder", group)
            break
    if not folder_name:
        return None
    return _ABILITIES_DIR / folder_name / ability_id / f"{ability_id}.skill.md"


def ability_has_inline_skill(ability_id: str) -> bool:
    """Inline descriptor skill bodies are unsupported by the file convention."""
    return False


# ── Accessors ───────────────────────────────────────────────────────────────

def all_raw() -> Dict[str, Dict[str, Any]]:
    """Every discovered ability's catalog entry, keyed by id."""
    return dict(_load())


def group_folder(group_id: str) -> Optional[str]:
    """Resolve a normalized group id (e.g. 'web') to its on-disk folder name
    (e.g. 'Web'). Returns None if unknown."""
    _load()
    meta = _GROUP_META.get(group_id)
    if meta:
        return meta.get("_folder")
    return None


def tools_map() -> Dict[str, List[str]]:
    """{ability_id: [tool names it gates]} — feeds app/tools/loader.ABILITY_TOOLS.
    Only kind=ability entries with has_runtime contribute tools."""
    return {
        aid: list(entry.get("tools") or [])
        for aid, entry in _load().items()
        if entry.get("has_runtime") and entry.get("kind") in (None, "ability")
    }


def virtual_ability_for_tool(name: str) -> Optional[str]:
    """The DISPLAY-ONLY (``virtual``) ability that lists a tool, or None.

    A virtual ability (e.g. Core ▸ Base) groups always-on core tools under one
    row in both ability tables purely for visibility + permission management. It
    is intentionally NOT in ``tools_map()``/``ABILITY_TOOLS`` — so this lookup is
    kept SEPARATE from ``tool_modes.ability_for_tool`` (which drives runtime
    gating). Using this for gating would let ``tool_hidden_by_ability`` withhold
    these always-on tools — exactly what we avoid. Use it ONLY to LABEL a tool's
    owning row in the panels (see app/api/agents.py list_agent_tools)."""
    for aid, entry in _load().items():
        if entry.get("virtual") and name in (entry.get("tools") or []):
            return aid
    return None


def tool_descriptions() -> Dict[str, str]:
    """Return canonical one-line handler descriptions for catalogued tools.

    This deliberately materializes the same drop-in handlers as the runtime
    loader, then reads the first docstring line used in the model tool index.
    Descriptor authors therefore do not maintain a second UI-only description.
    Failures are isolated per catalog refresh: an optional ability that cannot
    initialize never prevents the rest of the ability table from rendering.
    """
    try:
        from app.tools.loader import ToolLoader
        providers = set(tools_map())
        # Virtual abilities own always-on core tool names, so include those in
        # the known-name filter even though they intentionally do not gate tools.
        known_names = {
            name for entry in _load().values() for name in (entry.get("tools") or [])
        }
        loaded: Dict[str, Any] = {}
        ToolLoader()._inject_builtin_tools(
            loaded, user_id="", enabled_providers=providers, session_id="",
        )
        descriptions: Dict[str, str] = {}
        for name in known_names:
            handler = getattr(loaded.get(name), "handler", None)
            text = (getattr(handler, "__doc__", "") or "").strip().split("\n")[0]
            if text:
                descriptions[name] = text
        return descriptions
    except Exception as exc:
        logger.warning("Could not build catalog tool descriptions: %s", exc)
        return {}


def tool_metadata() -> Dict[str, Dict[str, Any]]:
    """Per-tool loop metadata for every ability-gated tool — feeds
    app/tools/loader.BUILTIN_TOOL_METADATA so a dropped-in ability's tools show
    up in /admin/tools and the loop visualizer with NO loader edit.

    An ability's .json may carry an optional top-level ``tool_metadata`` map:
        "tool_metadata": {"my_tool": {"stages": ["guardrails", "execute_tools"],
                                       "destructive": true}}
    Tools without an explicit entry get safe defaults (execute_tools stage,
    non-destructive). Entries with explicit descriptor metadata are flagged
    ``_explicit`` so the loader lets them override its legacy literals."""
    out: Dict[str, Dict[str, Any]] = {}
    for aid, entry in _load().items():
        if not entry.get("has_runtime") or entry.get("kind") not in (None, "ability"):
            continue
        explicit = entry.get("tool_metadata") or {}
        for tname in entry.get("tools") or []:
            meta: Dict[str, Any] = {"stages": ["execute_tools"],
                                    "destructive": False, "agent_types": []}
            em = explicit.get(tname)
            if isinstance(em, dict):
                meta.update(em)
                meta["_explicit"] = True
            out[tname] = meta
    return out


_CONFIRM_TOOLS_CACHE: Optional[set] = None


def confirm_gated_tools(force: bool = False) -> set:
    """Global set of tool names that inherently PAUSE for the user's confirmation
    in Ask/Plan mode — their built-in ``requires_confirmation`` nature, independent
    of any admin/agent permission override.

    This is the single source of truth that lets both ability tables show a
    write/destructive tool at its true permission ("Ask") and lock it from being
    set looser, instead of misleadingly defaulting it to "Auto". It unions:

      1. each runtime ability's ``DESTRUCTIVE`` set (the loader turns membership
         into ``requires_confirmation`` on the tool);
      2. integration tools whose spec requires confirmation (defaults to its
         ``destructive`` flag) + the always-confirmed generic ``oauth_api_call``;
      3. built-in tools flagged ``requires_confirmation`` in the loader metadata,
         plus the hard-coded loop baseline (``run_command``).

    Note this is the ASK-mode floor: a tool that is ``destructive`` but NOT
    ``requires_confirmation`` (e.g. ``db_query``, gated only in Plan mode) is
    deliberately excluded, so the displayed permission matches Ask-mode reality.
    Cached; pass ``force=True`` (or call ``reload()``) after a drop-in change."""
    global _CONFIRM_TOOLS_CACHE
    if _CONFIRM_TOOLS_CACHE is not None and not force:
        return _CONFIRM_TOOLS_CACHE

    names: set = set()
    # 1. Each runtime ability's DESTRUCTIVE set (populated by its build_tools()).
    for aid, entry in _load().items():
        if not entry.get("has_runtime") or entry.get("kind") not in (None, "ability"):
            continue
        mod = ability_module(aid)
        if mod is None:
            continue
        bt = getattr(mod, "build_tools", None)
        if callable(bt):
            try:
                bt(user_id="", session_id="", agent_id="", enabled_providers={aid})
            except Exception:
                pass
        try:
            names |= set(getattr(mod, "DESTRUCTIVE", set()) or set())
        except Exception:
            pass

    # 2. Integration (OAuth) tools — requires_confirmation defaults to destructive.
    try:
        from app.integrations import _discover_tool_specs
        for spec in _discover_tool_specs():
            if spec.get("requires_confirmation", spec.get("destructive", False)):
                names.add(spec["name"])
        names.add("oauth_api_call")
    except Exception:
        pass

    # 3. Built-in metadata flagged requires_confirmation + the loop baseline.
    try:
        from app.tools.loader import BUILTIN_TOOL_METADATA
        names |= {n for n, md in BUILTIN_TOOL_METADATA.items()
                  if isinstance(md, dict) and md.get("requires_confirmation")}
    except Exception:
        pass
    try:
        from app.agent.loop import DESTRUCTIVE_TOOLS
        names |= set(DESTRUCTIVE_TOOLS)
    except Exception:
        names |= {"run_command"}

    _CONFIRM_TOOLS_CACHE = names
    return names


def feature_descriptors() -> List[Any]:
    """FeatureDescriptor objects for the feature catalog / edition gating."""
    from app.features.descriptor import normalize_feature
    out = []
    for aid, entry in _load().items():
        out.append(normalize_feature(
            {
                "id": aid,
                "display_name": entry["display_name"],
                "category": "ability",
                "status": entry.get("status") or "stable",
                "summary": entry.get("description", ""),
            },
            category="ability", default_id=aid,
            module=f"plugins.abilities.{entry.get('group', '')}.{aid}",
            drop_in=True,
        ))
    return out


def connection_rows() -> List[Dict[str, Any]]:
    """Connection rows for app/api/agents.py — all non-placeholder abilities."""
    rows: List[Dict[str, Any]] = []
    for aid, entry in _load().items():
        if entry.get("placeholder"):
            continue
        rows.append({
            "connection_type": aid,
            "section": "ability",
            "display_name": entry["display_name"],
            "status": "available",
            "description": entry.get("description") or "",
            "icon": entry.get("icon") or "plug",
            "color": entry.get("color") or "#7aa2f7",
            "group": entry.get("group") or "",
            "simple": bool(entry.get("simple", True)),
            "kind": entry.get("kind", "ability"),
            "maturity": entry.get("status") or "stable",
            # Safety device — always-on, fixed toggle (cannot be turned off).
            # The per-agent Abilities panel renders its toggle locked and the
            # connections API forces its enabled flag true.
            "locked_on": bool(entry.get("locked_on", False)),
            # True when the ability declares a credentials block — the per-agent
            # Abilities panel renders the shared credentials form for it (the
            # admin panel reads the same flag from the catalog as has_credentials).
            "has_credentials": bool(isinstance(entry.get("credentials"), dict)
                                    and entry["credentials"].get("fields")),
            "ui_panel": entry.get("ui_panel"),
        })
    return rows


def ui_catalog() -> Dict[str, Any]:
    """The payload both ability panels fetch to render generically.

    {
      groups: [{id, name, icon, color, desc, members:[ability_id…]}],
      abilities: {id: {display_name, kind, description, note, icon, color, simple,
                        placeholder, group, tools}},
    }
    Members are sorted alphabetically by display name.
    Groups are sorted by _group.json order, then alphabetically.
    """
    cat = _load()
    groups_meta = dict(_GROUP_META)  # {normalized_gid: {name, icon, ...}}

    # Live order overrides from data/config/agent-abilities.json (admin-editable,
    # repo-specific). Any group/ability not listed there falls back to the
    # descriptor / _group.json ``order`` (the drop-in seed default). Read lazily so
    # the catalog core stays decoupled and still works if admin config is absent.
    order_groups: Dict[str, int] = {}
    order_abilities: Dict[str, int] = {}
    # Live on/off toggles (admin-set, stored in data/config/agent-abilities.json).
    # Only explicitly-stored choices appear here; anything absent falls back to the
    # ability's descriptor ``default_enabled``. Baking the resolved state INTO the
    # catalog lets each panel draw every toggle in the correct position on first
    # render, with no second state fetch / race (mirrors ``_ability_app_enabled``).
    stored_states: Dict[str, bool] = {}
    try:
        from app.admin import ability_config as _abcfg
        _ov = _abcfg.get_order()
        order_groups = _ov.get("groups") or {}
        order_abilities = _ov.get("abilities") or {}
        stored_states = _abcfg.all_ability_states()
    except Exception:
        pass

    abilities: Dict[str, Any] = {}
    for aid, entry in cat.items():
        abilities[aid] = {
            "id": aid,
            "display_name": entry["display_name"],
            "kind": entry["kind"],
            "description": entry.get("description") or "",
            "note": entry.get("note"),
            "icon": entry.get("icon") or "plug",
            "color": entry.get("color") or "#7aa2f7",
            "simple": entry.get("simple", True),
            "placeholder": entry.get("placeholder", False),
            "status": entry.get("status", "stable"),
            "protected": entry.get("protected", False),
            "default_enabled": entry.get("default_enabled", False),
            # Safety device — always-on, fixed toggle (cannot be turned off).
            "locked_on": entry.get("locked_on", False),
            # Background app service. Most are App-Functions-only; a descriptor
            # may opt into the per-agent table with agent_configurable.
            "app_function": entry.get("app_function", False),
            "agent_configurable": entry.get("agent_configurable", False),
            # App Functions are administered only from App Settings.  Include
            # their non-secret schema in the catalog so the expanded row can
            # render its controls immediately; it still fetches saved values
            # through the authenticated config endpoint. Agent-facing abilities
            # keep their schema on the existing on-demand endpoint.
            "config": entry.get("config") if entry.get("app_function") else None,
            # Resolved live on/off: a locked-on safety ability is forced ON
            # regardless of any stored admin choice; otherwise stored admin
            # choice ▸ descriptor default.
            # SHIP POLICY (mirrors ability_default_enabled): no stored admin
            # choice ⇒ ON, so the Agent Tools table renders every fresh/new
            # ability enabled. An ability opts out with "default_enabled": false.
            "enabled": True if entry.get("locked_on")
                       else stored_states.get(aid, entry.get("default_enabled", True)),
            # Bundled-skill metadata so a panel can render the "Skill" row (its
            # "when to use it" summary + an editor) without a per-ability fetch.
            "skill_summary": entry.get("skill_summary") or "",
            "skill_mode": entry.get("skill_mode") or "selectable",
            "order": order_abilities.get(aid, entry.get("order", 100)),
            "group": entry["group"],
            "entitlement_group": entry.get("entitlement_group") or "platform_admin",
            "risk_class": entry.get("risk_class"),
            "action_class": entry.get("action_class"),
            "required_integration": entry.get("required_integration"),
            "tools": entry.get("tools") or [],
            # Existing per-tool Markdown association, keyed by declared tool name.
            # The App abilities table consumes this read-only map to distinguish a
            # missing dedicated mini-skill from every other tool property.
            "skill_tool_sections": _resolve_tool_skill_sections(entry, aid),
            # True when the ability declares a ``credentials`` block — the panel
            # then renders its generic credentials form. Field defs + values are
            # fetched on demand from /abilities/{id}/credentials (never inlined
            # here, so secrets never ride along with the catalog).
            "has_credentials": bool(isinstance(entry.get("credentials"), dict)
                                    and entry["credentials"].get("fields")),
            "ui_panel": entry.get("ui_panel"),
        }

    # ── Split off background APP FUNCTIONS ──────────────────────────────────
    # An app_function is copied into App Settings ▸ App Functions. Ordinarily it
    # is then lifted out of the agent ability map; dual-surface safety services
    # marked agent_configurable remain available for per-agent preferences too.
    # Nothing else changes —
    # runtime still reads these from _load() (locked_on / context_strategy /
    # background-service hooks all resolve exactly as before).
    app_functions: Dict[str, Any] = {}
    for aid in [a for a, m in abilities.items() if m.get("app_function")]:
        app_functions[aid] = dict(abilities[aid])
        if not abilities[aid].get("agent_configurable"):
            abilities.pop(aid)

    # Display names for member sorting
    name_of: Dict[str, str] = {aid: a["display_name"] for aid, a in abilities.items()}

    # Bucket members by group (app functions already removed from `abilities`)
    by_group: Dict[str, List[str]] = {}
    for aid, a in abilities.items():
        by_group.setdefault(a["group"], []).append(aid)

    # A group whose ONLY descriptors are app_functions (e.g. the "System" group
    # that holds the wired-in background services) must NOT surface as an empty
    # category card in either ability table — its members all render in App
    # Settings ▸ App Functions instead. Suppress only these pure-app-function
    # groups; a genuinely empty group (a _group.json with no descriptors at all)
    # still appears, as before.
    _grp_has_real: Dict[str, bool] = {}
    _grp_has_appfn: Dict[str, bool] = {}
    for e in cat.values():
        g = e.get("group")
        if e.get("app_function") and not e.get("agent_configurable"):
            _grp_has_appfn[g] = True
        else:
            _grp_has_real[g] = True
    _appfn_only_groups = {g for g in _grp_has_appfn if not _grp_has_real.get(g)}

    # Ensure groups with only _group.json and no members still appear
    for gid in groups_meta:
        if gid not in by_group:
            if gid in _appfn_only_groups:
                continue  # pure app-function group — rendered under App Functions
            by_group[gid] = []

    # Build group list with sorted members
    groups = []
    for gid, members in by_group.items():
        gm = groups_meta.get(gid, {
            "name": gid.replace("_", " ").title(),
            "icon": _EMERGENT_GROUP_ICON,
            "color": _EMERGENT_GROUP_COLOR,
            "desc": "",
            "order": _EMERGENT_GROUP_ORDER,
        })
        groups.append({
            "id": gid,
            "name": gm["name"],
            "icon": gm["icon"],
            "color": gm["color"],
            "desc": gm.get("desc", ""),
            "members": sorted(members, key=lambda m: (abilities.get(m, {}).get("order", 100), name_of.get(m, m).lower())),
            "_order": order_groups.get(gid, gm.get("order", _EMERGENT_GROUP_ORDER)),
        })

    groups.sort(key=lambda g: (g["_order"], g["name"].lower()))
    for g in groups:
        g.pop("_order", None)

    # App functions carry the SAME shape as abilities (id, display_name, kind,
    # description, icon, color, enabled, locked_on, config-presence, …) so the App
    # Functions panel renders them with the shared row + settings helpers. They
    # are keyed by id (no groups — the panel shows one flat table), sorted by the
    # descriptor `order` then display name.
    app_function_list = sorted(
        app_functions.values(),
        key=lambda a: (a.get("order", 100), (a.get("display_name") or "").lower()),
    )

    return {
        "groups": groups,
        "abilities": abilities,
        "app_functions": app_function_list,
    }


def ability_ids(kind: Optional[str] = "ability") -> List[str]:
    """All ability ids in the catalog, optionally filtered to one ``kind``.

    Default ``kind="ability"`` returns the host tool-gating abilities that the
    admin Agent Tools panel shows an on/off toggle for — i.e. the set whose
    enabled-state lives in data/config/agent-abilities.json. Pass ``kind=None``
    for every catalog entry (abilities, oauth, credential, channel, placeholder).
    """
    cat = _load()
    if kind is None:
        return list(cat.keys())
    return [aid for aid, e in cat.items() if e.get("kind") == kind]


def _seedable_ability_ids() -> List[str]:
    """Toggleable abilities that make sense to pre-enable on an agent — every
    kind="ability" entry EXCEPT display-only ``virtual`` rows (Core ▸ Base),
    which gate nothing so a connection row for them would be inert, and
    ``app_function`` background services (Session Namer, the scheduler, …), which
    are app-level only — a per-agent connection row for one would be meaningless.
    Placeholder (coming-soon) abilities are already excluded (kind="placeholder").
    Catalog order is preserved."""
    return [
        aid for aid, e in _load().items()
        if e.get("kind") == "ability" and not e.get("virtual")
        and not e.get("app_function")
    ]


def expand_ability_selectors(selectors: List[str]) -> List[str]:
    """Expand a template's ability list (``pre_enabled_connections`` /
    ``abilities``) — resolving wildcards to concrete ability ids.

    Supported wildcard tokens (case-insensitive):
      • ``"*"`` / ``"all"``          → every seedable ability (all groups)
      • ``"<Group>/*"``              → every seedable ability in that group,
        ``"group:<Group>"``            e.g. ``"Core/*"`` or ``"group:web"``.
        ``"<Group>:*"``                Group is matched by its normalized id OR
                                       its on-disk folder name, case-insensitive.

    Any other token is treated as a LITERAL id and passes through unchanged, so
    ordinary ability ids and integration connection-types (``gmail``, …) still
    work. Order is preserved and duplicates are dropped. A wildcard naming an
    unknown group expands to nothing (logged) rather than erroring — fail-open so
    a typo can never break agent creation.
    """
    if not selectors:
        return []
    cat = _load()
    seedable = _seedable_ability_ids()

    def _resolve_group(token: str) -> Optional[str]:
        norm = _normalize_group_id(token)
        if norm in _GROUP_META:
            return norm
        # Fall back to matching the folder name (e.g. "Core") case-insensitively.
        for gid, meta in _GROUP_META.items():
            if _normalize_group_id(meta.get("_folder", "")) == norm:
                return gid
        return None

    out: List[str] = []
    seen: set = set()

    def _add(aid: str) -> None:
        if aid and aid not in seen:
            seen.add(aid)
            out.append(aid)

    for raw in selectors:
        if not isinstance(raw, str):
            continue
        tok = raw.strip()
        if not tok:
            continue
        low = tok.lower()

        # ── "everything" ──
        if low in ("*", "all", "__all__"):
            for aid in seedable:
                _add(aid)
            continue

        # ── group wildcard ──
        grp: Optional[str] = None
        if low.startswith("group:"):
            grp = tok.split(":", 1)[1]
        elif tok.endswith("/*"):
            grp = tok[:-2]
        elif tok.endswith(":*"):
            grp = tok[:-2]
        if grp is not None:
            gid = _resolve_group(grp.strip())
            if gid is None:
                logger.warning(
                    "Ability selector %r names an unknown group — expands to "
                    "nothing (known groups: %s)",
                    raw, ", ".join(sorted(_GROUP_META)) or "none",
                )
                continue
            for aid in seedable:
                if cat[aid].get("group") == gid:
                    _add(aid)
            continue

        # ── literal id ──
        _add(tok)

    return out


def ability_entry(ability_id: str) -> Optional[Dict[str, Any]]:
    """Return the raw catalog entry for an ability id (or None)."""
    return _load().get(ability_id)


def is_toggleable_ability(ability_id: str) -> bool:
    """True iff ``ability_id`` is a kind="ability" member of the catalog — the
    ones the admin can enable/disable at the app level. Replaces the old
    hardcoded `_ABILITY_CONFIG_KEY` membership check."""
    e = _load().get(ability_id)
    return bool(e and e.get("kind") == "ability")


def ability_default_enabled(ability_id: str) -> bool:
    """The app-level default for an ability with no stored admin toggle.

    SHIP POLICY — abilities are ON by default: a known ability with no explicit
    ``default_enabled`` in its descriptor defaults to **enabled**, so a fresh
    install (and any ability dropped in later) ships fully unlocked without the
    admin hand-toggling each row. An ability that must ship OFF still opts out by
    setting ``"default_enabled": false`` explicitly in its descriptor. Unknown
    ids stay False."""
    e = _load().get(ability_id)
    return bool(e and e.get("default_enabled", True))


def app_function_enabled(app_function_id: str) -> bool:
    """Resolved app-level on/off for an app function — the SAME resolution
    ui_catalog bakes into each row: a ``locked_on`` safety device is always True;
    otherwise the admin's stored choice (data/config/agent-abilities.json) wins,
    falling back to the descriptor ``default_enabled`` (SHIP POLICY: True when
    unset). Used by background dispatchers that have no per-agent connection row
    to key off — e.g. the Session Namer turn hook, an app_function gated only at
    the app level. Fails ON if the config store can't be read (never silently
    disables a default-on function)."""
    e = _load().get(app_function_id)
    if not e:
        return False
    if e.get("locked_on"):
        return True
    try:
        from app.admin import ability_config as _abcfg
        stored = _abcfg.get_ability_enabled(app_function_id)
    except Exception:
        stored = None
    if stored is None:
        return bool(e.get("default_enabled", True))
    return bool(stored)


def ability_is_locked_on(ability_id: str) -> bool:
    """True for a safety-device ability that is always on and cannot be turned
    off (descriptor ``locked_on``). Its toggle is fixed ON in both panels and it
    is forced enabled at the app and per-agent level. See get_agent_connections,
    upsert_agent_connection, disable_ability, and context_strategy_for_agent."""
    e = _load().get(ability_id)
    return bool(e and e.get("locked_on", False))


def group_orders() -> Dict[str, int]:
    """{normalized_group_id: order} from each group's ``_group.json`` (the seed
    default). The live order an admin can override lives in
    data/config/agent-abilities.json."""
    _load()
    return {gid: int(meta.get("order", _EMERGENT_GROUP_ORDER))
            for gid, meta in _GROUP_META.items()}


def ability_orders() -> Dict[str, int]:
    """{ability_id: order} from each ability's descriptor ``order`` (the seed
    default). The live order an admin can override lives in
    data/config/agent-abilities.json."""
    return {aid: int(e.get("order", 100)) for aid, e in _load().items()}


def ability_config_schema(ability_id: str) -> Optional[Dict[str, Any]]:
    """Return the config schema for an ability from its descriptor's ``config`` key.
    Returns None if the ability has no config or is simple=True."""
    entry = _load().get(ability_id)
    if not entry:
        return None
    if entry.get("simple", True):
        return None
    config = entry.get("config")
    if not isinstance(config, dict):
        return None
    settings = config.get("settings")
    if not isinstance(settings, list):
        return None
    return {
        "ability_id": ability_id,
        "settings": settings,
        "notes": config.get("notes"),
    }


def ability_credentials_spec(ability_id: str) -> Optional[Dict[str, Any]]:
    """Return the ability's declarative ``credentials`` block (or None).

    Shape: {scope: admin|user|agent, requires: [keys], fields: [{key,label,type,
    secret,options,placeholder,hint}, …]}. Consumed by the common-credential
    framework (app/abilities/credentials.py), the GET/POST/DELETE credential
    endpoints, and configured-state gating."""
    entry = _load().get(ability_id)
    if not entry:
        return None
    creds = entry.get("credentials")
    if not isinstance(creds, dict) or not creds.get("fields"):
        return None
    return creds

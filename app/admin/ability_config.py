"""Admin-level ability configuration store — one repo-local file.

This module owns ``data/config/agent-abilities.json``: the single, human-readable
source of truth for **non-secret** admin ability settings. It exists so the admin
Agent Tools panel persists reliably for *every* drop-in ability (current and
future) without any hardcoded per-ability registration in core — which the old
``_ABILITY_CONFIG_KEY`` vault-row scheme failed to do for newer drop-ins.

What lives here (all non-secret):
  • ``abilities`` — the app-level on/off toggle per ability id: ``{id: {enabled}}``
  • ``tools``     — global per-tool defaults: ``{tool: {permission, visibility}}``
  • ``order``     — the display order of the ability table: group order and
                    per-ability order — ``{groups: {gid: int}, abilities: {id: int}}``.
                    Seeded from the drop-in descriptors' ``order`` fields, then this
                    file is the live source of truth (the descriptors keep their
                    ``order`` only as the seed default for a brand-new drop-in).
  • ``ability_config`` — non-secret config knobs for non-simple abilities (phase 2)

What does NOT live here: secrets. API keys, OAuth client secrets, BYO credentials
and tokens stay in the encrypted vault (``data/db/vault.db``). That is why this
file is safe to track in the repo alongside data/config/integrations.json &c.

Persistence pattern mirrors app/agent/suggestions.py and
app/admin/scheduler_config.py: load-with-defaults, log-and-degrade on error,
``indent=2``, create the folder on write.

Migration: on first access when the file is absent, ``ensure_bootstrapped`` reads
the current state out of the vault (the old per-ability rows + the tool_defaults
blob) and writes the file once, so nothing an admin already configured resets.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Dict, Optional

from app.util.config_io import safe_write_json

logger = logging.getLogger(__name__)

# app/admin/ability_config.py → app/admin → app → repo root
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
CONFIG_FILE = os.path.join(_REPO_ROOT, "data", "config", "agent-abilities.json")

# Admin (platform) scope — matches _ADMIN_USER in app/admin/integrations.py and
# the vault rows this file migrates from.
_ADMIN_USER = "admin"

_VERSION = 1

_lock = threading.RLock()
_cache: Optional[Dict[str, Any]] = None
_bootstrapped = False


def _empty() -> Dict[str, Any]:
    return {
        "version": _VERSION,
        "abilities": {},
        "order": {"groups": {}, "abilities": {}},
        "tools": {},
        "ability_config": {},
    }


def _coerce(data: Any) -> Dict[str, Any]:
    """Layer a loaded blob over the empty skeleton so every section exists."""
    out = _empty()
    if isinstance(data, dict):
        if isinstance(data.get("abilities"), dict):
            out["abilities"] = data["abilities"]
        if isinstance(data.get("tools"), dict):
            out["tools"] = data["tools"]
        if isinstance(data.get("ability_config"), dict):
            out["ability_config"] = data["ability_config"]
        order = data.get("order")
        if isinstance(order, dict):
            if isinstance(order.get("groups"), dict):
                out["order"]["groups"] = order["groups"]
            if isinstance(order.get("abilities"), dict):
                out["order"]["abilities"] = order["abilities"]
        if isinstance(data.get("version"), int):
            out["version"] = data["version"]
    return out


def file_exists() -> bool:
    return os.path.exists(CONFIG_FILE)


def _load() -> Dict[str, Any]:
    """Return the in-process config, reading the file on first call."""
    global _cache
    with _lock:
        if _cache is not None:
            return _cache
        data: Any = None
        try:
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
        except Exception as e:
            logger.warning("agent-abilities.json: could not read (%s) — using empty", e)
        _cache = _coerce(data)
        return _cache


def _save(data: Dict[str, Any]) -> None:
    """Persist the config to disk and refresh the cache."""
    global _cache
    with _lock:
        _cache = _coerce(data)
        try:
            safe_write_json(CONFIG_FILE, _cache)
        except Exception as e:
            logger.warning("agent-abilities.json: could not write: %s", e)


# ── Ability on/off toggles ────────────────────────────────────────────────────

def get_ability_enabled(ability_id: str) -> Optional[bool]:
    """Stored app-level toggle for an ability, or ``None`` when not set.

    ``None`` means "no explicit admin choice" — the caller should fall back to the
    ability's descriptor ``default_enabled``."""
    entry = _load()["abilities"].get(ability_id)
    if isinstance(entry, dict) and isinstance(entry.get("enabled"), bool):
        return entry["enabled"]
    if isinstance(entry, bool):  # tolerate a bare bool if hand-edited
        return entry
    return None


def set_ability_enabled(ability_id: str, enabled: bool) -> None:
    data = _load()
    abilities = dict(data["abilities"])
    abilities[ability_id] = {"enabled": bool(enabled)}
    data = dict(data, abilities=abilities)
    _save(data)


def all_ability_states() -> Dict[str, bool]:
    """Every explicitly-stored ability toggle as ``{id: enabled}``."""
    out: Dict[str, bool] = {}
    for aid in _load()["abilities"]:
        val = get_ability_enabled(aid)
        if val is not None:
            out[aid] = val
    return out


# ── Global per-tool defaults ──────────────────────────────────────────────────

def get_tools() -> Dict[str, Any]:
    """The global per-tool default map: ``{tool: {permission?, visibility?}}``."""
    return dict(_load()["tools"])


def set_tools(tools: Dict[str, Any]) -> None:
    data = _load()
    _save(dict(data, tools=dict(tools or {})))


# ── Display order (ability table) ─────────────────────────────────────────────

def get_order() -> Dict[str, Dict[str, int]]:
    """The stored display order: ``{groups: {gid: int}, abilities: {id: int}}``.

    Either map may be empty/partial — any group or ability not listed falls back
    to its drop-in descriptor ``order`` (so a freshly-dropped ability still lands
    at the position its descriptor declares until an admin reorders it)."""
    order = _load()["order"]
    groups = {k: v for k, v in (order.get("groups") or {}).items() if isinstance(v, int)}
    abilities = {k: v for k, v in (order.get("abilities") or {}).items() if isinstance(v, int)}
    return {"groups": groups, "abilities": abilities}


def get_group_order() -> Dict[str, int]:
    return get_order()["groups"]


def get_ability_order() -> Dict[str, int]:
    return get_order()["abilities"]


def set_order(groups: Optional[Dict[str, int]] = None,
              abilities: Optional[Dict[str, int]] = None) -> None:
    """Replace the stored group and/or ability order maps. Passing ``None`` for a
    side leaves that side untouched."""
    data = _load()
    order = {
        "groups": dict(data["order"].get("groups") or {}),
        "abilities": dict(data["order"].get("abilities") or {}),
    }
    if groups is not None:
        order["groups"] = {str(k): int(v) for k, v in groups.items()}
    if abilities is not None:
        order["abilities"] = {str(k): int(v) for k, v in abilities.items()}
    _save(dict(data, order=order))


def set_group_order(group_id: str, order: int) -> None:
    cur = get_group_order()
    cur[group_id] = int(order)
    set_order(groups=cur)


def set_ability_order(ability_id: str, order: int) -> None:
    cur = get_ability_order()
    cur[ability_id] = int(order)
    set_order(abilities=cur)


def order_is_seeded() -> bool:
    """True once the order section holds at least one entry (i.e. it has been
    seeded from the descriptors or set by an admin)."""
    order = _load()["order"]
    return bool(order.get("groups")) or bool(order.get("abilities"))


# ── Non-secret ability config knobs (phase 2) ─────────────────────────────────

def get_ability_config(ability_id: str) -> Dict[str, Any]:
    cfg = _load()["ability_config"].get(ability_id)
    return dict(cfg) if isinstance(cfg, dict) else {}


def set_ability_config(ability_id: str, cfg: Dict[str, Any]) -> None:
    data = _load()
    blob = dict(data["ability_config"])
    blob[ability_id] = dict(cfg or {})
    _save(dict(data, ability_config=blob))


def _as_bool(v: Any) -> bool:
    """Interpret a settings value as a bool. The config panel stores every field
    as a STRING, so ``bool("false")`` would be wrong — treat the common false-ish
    strings as False (mirrors automation.py:_coerce_bool)."""
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(v)


def _as_num(v: Any) -> Optional[float]:
    """Best-effort numeric read; ``None`` when not parseable."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return v
    if isinstance(v, str):
        try:
            return float(v.strip())
        except (TypeError, ValueError):
            return None
    return None


def _clamp_value(ceiling: str, ftype: Optional[str], admin_val: Any, agent_val: Any) -> Any:
    """Clamp one per-agent value to the admin ceiling per the field's declared
    ``ceiling`` rule. The admin value is the GLOBAL MAXIMUM; the agent may match it
    or be stricter, never looser:

      • boolean ``"off"`` — admin OFF binds → effective = admin AND agent.
      • boolean ``"on"``  — admin ON binds  → effective = admin OR agent.
      • number  ``"max"`` — admin value caps the top → effective = min(admin, agent).
      • number  ``"min"`` — admin value sets the floor → effective = max(admin, agent).

    A non-numeric/odd value degrades to the agent's own value (never raises)."""
    if ceiling in ("off", "on") or ftype == "boolean":
        a, g = _as_bool(admin_val), _as_bool(agent_val)
        if ceiling == "on":
            return a or g
        return a and g  # "off" (default for booleans)
    if ceiling in ("max", "min") or ftype == "number":
        a, g = _as_num(admin_val), _as_num(agent_val)
        if a is None:
            return agent_val
        if g is None:
            return admin_val
        clamped = max(a, g) if ceiling == "min" else min(a, g)
        # Keep ints int (the panel/loader expects whole numbers for counts).
        return int(clamped) if float(clamped).is_integer() else clamped
    return agent_val


def _ceiling_rules(ability_id: str) -> Dict[str, tuple]:
    """``{key: (ceiling, type, default, source_key)}`` for config fields
    that declares a ``ceiling`` rule. Empty when the ability has no schema or no
    ceiling-bearing fields. ``ceiling_key`` optionally points at a different
    app-level field that supplies the cap (for example an agent target capped by
    the global maximum-context field)."""
    rules: Dict[str, tuple] = {}
    try:
        from app.abilities import ability_config_schema
        schema = ability_config_schema(ability_id) or {}
        for f in (schema.get("settings") or []):
            c = f.get("ceiling")
            if c and f.get("key"):
                rules[f["key"]] = (
                    c, f.get("type"), f.get("default"),
                    f.get("ceiling_key") or f["key"],
                )
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("_ceiling_rules: schema read failed for %s: %s", ability_id, e)
    return rules


def effective_ability_config(
    ability_id: str, per_agent: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """The EFFECTIVE non-secret settings for an ability on one agent.

    The **admin app-level** config (set in Agent Settings → Agent Tools) is the
    app-wide **ceiling / default**; the agent's **own per-agent** settings layer on
    top, BUT only down to that ceiling. Two regimes per knob:

      • A field that declares a ``ceiling`` rule (off / on / max / min) is CLAMPED:
        the agent may match the admin or be stricter, never looser (see
        ``_clamp_value``). An admin who disables a knob, or lowers a numeric cap,
        binds every agent.
      • A field with NO ceiling rule keeps the old behaviour — the agent's value
        simply wins over the admin default (back-compatible; un-annotated abilities
        are unaffected).

    Either side may be empty/missing. Callers may still coerce types (values may be
    strings from the config panel). Never raises — a bad admin store or schema
    degrades to the plain merge."""
    base: Dict[str, Any] = {}
    try:
        stored = get_ability_config(ability_id)
        if isinstance(stored, dict):
            base = dict(stored)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("effective_ability_config: admin read failed for %s: %s", ability_id, e)
        base = {}

    pa = per_agent if isinstance(per_agent, dict) else {}
    rules = _ceiling_rules(ability_id)

    out = dict(base)
    # Non-ceiling knobs: agent wins (old behaviour).
    for k, v in pa.items():
        if k not in rules:
            out[k] = v
    # Ceiling knobs: clamp the agent value to the admin value (admin value falls
    # back to the field default when the admin never set it explicitly).
    for k, (ceiling, ftype, default, source_key) in rules.items():
        admin_val = base[source_key] if source_key in base else default
        if k in pa:
            out[k] = _clamp_value(ceiling, ftype, admin_val, pa[k])
        elif k not in out:
            out[k] = admin_val
    return out


# ── First-run migration from the vault ────────────────────────────────────────

async def ensure_bootstrapped(db) -> None:
    """One-time: if the file is missing, seed it from current vault state.

    Reproduces the previous per-ability semantics exactly so an admin's existing
    configuration is preserved on upgrade:
      • on-by-default abilities (descriptor ``default_enabled``) were enabled
        unless an explicit ``secret_ref="disabled"`` vault row existed;
      • all other abilities were enabled iff a vault row carried a truthy
        ``secret_ref``.
    On a brand-new install (no vault rows) this yields each ability's descriptor
    default, and the global tool-defaults blob is carried across verbatim.
    """
    global _bootstrapped
    with _lock:
        already = _bootstrapped or file_exists()

    # The file may already exist (toggles seeded in an earlier version) yet predate
    # the ``order`` section — backfill it from the descriptors without disturbing
    # any existing toggles, then we're done.
    if already:
        _ensure_order_seeded()
        with _lock:
            _bootstrapped = True
        return

    try:
        from app import abilities as abilities_catalog
    except Exception as e:
        logger.warning("ability_config bootstrap: catalog unavailable: %s", e)
        return

    abilities_state: Dict[str, Any] = {}
    try:
        for aid in abilities_catalog.ability_ids("ability"):
            default_on = abilities_catalog.ability_default_enabled(aid)
            enabled = default_on
            try:
                elem = await db.auth_element_get(_ADMIN_USER, f"ability_{aid}", "default")
            except Exception:
                elem = None
            if default_on:
                enabled = not (elem and elem.get("secret_ref") == "disabled")
            else:
                enabled = bool(elem and elem.get("secret_ref"))
            abilities_state[aid] = {"enabled": bool(enabled)}
    except Exception as e:
        logger.warning("ability_config bootstrap: reading ability rows failed: %s", e)

    tools: Dict[str, Any] = {}
    try:
        elem = await db.auth_element_get(_ADMIN_USER, "tool_defaults", "default")
        if elem and elem.get("config"):
            stored = json.loads(elem["config"]) if isinstance(elem["config"], str) else elem["config"]
            if isinstance(stored, dict) and isinstance(stored.get("tools"), dict):
                tools = stored["tools"]
    except Exception as e:
        logger.debug("ability_config bootstrap: reading tool defaults failed: %s", e)

    order_blob = _build_order_blob()
    _save({
        "version": _VERSION,
        "abilities": abilities_state,
        "order": order_blob,
        "tools": tools,
        "ability_config": {},
    })
    with _lock:
        _bootstrapped = True
    logger.info(
        "Seeded data/config/agent-abilities.json from vault (%d abilities, %d tool defaults, %d group + %d ability orders)",
        len(abilities_state), len(tools),
        len(order_blob.get("groups", {})), len(order_blob.get("abilities", {})),
    )


def _build_order_blob() -> Dict[str, Dict[str, int]]:
    """Snapshot the current display order from the drop-in descriptors into the
    file's shape: ``{groups: {gid: int}, abilities: {id: int}}``."""
    try:
        from app import abilities as abilities_catalog
        return {
            "groups": dict(abilities_catalog.group_orders()),
            "abilities": dict(abilities_catalog.ability_orders()),
        }
    except Exception as e:
        logger.warning("ability_config: could not snapshot order from catalog: %s", e)
        return {"groups": {}, "abilities": {}}


def _ensure_order_seeded() -> None:
    """If the order section is empty (file predates it), seed it from the
    descriptors once — preserving the table's current order exactly."""
    if order_is_seeded():
        return
    blob = _build_order_blob()
    if not blob.get("groups") and not blob.get("abilities"):
        return
    set_order(groups=blob["groups"], abilities=blob["abilities"])
    logger.info(
        "Backfilled display order into agent-abilities.json (%d groups, %d abilities)",
        len(blob["groups"]), len(blob["abilities"]),
    )


def reload() -> None:
    """Drop the in-process cache (e.g. after an external edit to the file)."""
    global _cache
    with _lock:
        _cache = None

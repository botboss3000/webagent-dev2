"""Gate registry — declarative run-contract gate types from ``plugins/gates/``.

Gate descriptors are Markdown drop-ins named ``<id>.gate.md``. Their JSON front
matter declares a reviewed projection onto observables the loop already tracks;
the Markdown body is the prompt instruction. The registry never executes
Markdown. Python ``FEATURE`` modules are accepted only as a temporary migration
format, with a Markdown descriptor taking precedence for the same id.

An agent's gate instances live in ``metadata['gates']``. This is the sole
activation authority: missing, malformed, omitted, or disabled instances are
OFF. Historical ``metadata['contract']`` gate fields are deliberately inert so
operators can enable and validate catalog gates one at a time.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GATES_PACKAGE = "plugins.gates"
_GATES_DIR = Path(__file__).resolve().parents[2] / "plugins" / "gates"
MARKDOWN_GATE_SUFFIX = ".gate.md"

# ``scope`` values that mean "every execution mode".
MODE_WILDCARD = "*"

# Stored before the progress-message rename; normalize only at the persistence boundary.
_LEGACY_GATE_TYPE_ALIASES = {"plan_before_edit": "pre_tool_progress_message"}

_VALID_PHASES = ("pre_tool", "completion", "prompt")
_VALID_VIEW_KINDS = ("bool", "int")

# ``inject`` — where a type's prompt instruction is allowed to render. The
# default (``prompt``) is the standing ``## Contract`` block. ``completion``
# marks a type whose instruction belongs ONLY to the end-of-run injection, so
# it must never be rendered into the standing block or the context estimate.
# ``pre_tool`` marks a type whose instruction belongs ONLY to the message its
# own rule feeds back the moment it first fires — the plan-announcement gate,
# whose wording asks for a reply and would therefore be actively wrong in the
# standing block. Both non-default values share the property that matters: the
# body never reaches the standing block or the context estimate.
INJECT_PROMPT = "prompt"
INJECT_COMPLETION = "completion"
INJECT_PRE_TOOL = "pre_tool"
_VALID_INJECTS = (INJECT_PROMPT, INJECT_COMPLETION, INJECT_PRE_TOOL)

_registry: Optional[Dict[str, Dict[str, Any]]] = None


# ── Discovery ────────────────────────────────────────────────────────────────

def _module_name(path: Path) -> str:
    return f"{GATES_PACKAGE}.{path.stem}"


def _id_from_markdown_path(path: Path) -> str:
    return path.name[:-len(MARKDOWN_GATE_SUFFIX)]


def _validate(path: Path, feature: Any, *, expected_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """Return a normalized descriptor, or None after an actionable warning."""
    if not isinstance(feature, dict):
        logger.warning("gate descriptor %s has no object front matter — ignored", path.name)
        return None
    gate_id = str(feature.get("id") or "").strip()
    if not gate_id:
        logger.warning("gate descriptor %s is missing id — ignored", path.name)
        return None
    expected_id = expected_id or path.stem
    if gate_id != expected_id:
        logger.warning(
            "gate descriptor %s declares id %r — the id must equal %r",
            path.name, gate_id, expected_id,
        )
        return None
    phase = str(feature.get("phase") or "").strip()
    if phase not in _VALID_PHASES:
        logger.warning(
            "gate descriptor %s has invalid phase %r (expected one of %s) — ignored",
            path.name, phase, ", ".join(_VALID_PHASES),
        )
        return None
    view = feature.get("view")
    if not isinstance(view, dict) or not str(view.get("key") or "").strip():
        logger.warning("gate descriptor %s has no view.key — ignored", path.name)
        return None
    kind = str(view.get("kind") or "bool").strip()
    if kind not in _VALID_VIEW_KINDS:
        logger.warning("gate descriptor %s has invalid view kind %r — ignored", path.name, kind)
        return None
    params = feature.get("params")
    if params is not None and not isinstance(params, dict):
        logger.warning("gate descriptor %s has non-dict params — ignored", path.name)
        return None
    try:
        order = int(feature.get("order", 50))
    except (TypeError, ValueError):
        order = 50
    # ``inject`` declares WHERE this type's instruction may be rendered.
    # ``prompt`` (default) is the standing ``## Contract`` block; ``completion``
    # is the end-of-run injection only, so its instruction can never leak into
    # every turn of the run. Unknown values fail closed to the standing block.
    inject = str(feature.get("inject") or INJECT_PROMPT).strip()
    if inject not in _VALID_INJECTS:
        logger.warning(
            "gate descriptor %s has invalid inject %r (expected one of %s) — using %r",
            path.name, inject, ", ".join(_VALID_INJECTS), INJECT_PROMPT,
        )
        inject = INJECT_PROMPT
    prompt_instruction = str(feature.get("prompt_instruction") or "").strip()
    return {
        "id": gate_id,
        "display_name": str(feature.get("display_name") or gate_id.replace("_", " ").title()),
        "description": str(feature.get("description") or ""),
        "prompt_instruction": prompt_instruction,
        "phase": phase,
        "inject": inject,
        "order": order,
        "status": str(feature.get("status") or "stable"),
        "default_enabled": feature.get("default_enabled") is True,
        "mode_scoped": feature.get("mode_scoped") is not False,
        "append_message": feature.get("append_message") is True,
        "view": {
            "key": str(view["key"]).strip(),
            "kind": kind,
            "param": str(view.get("param") or "value"),
            "default": int(view.get("default") or 0) if kind == "int" else False,
        },
        "params": dict(params or {}),
        "source": "markdown" if path.name.endswith(MARKDOWN_GATE_SUFFIX) else "python",
    }


def _load_markdown(path: Path) -> Optional[Dict[str, Any]]:
    """Parse JSON front matter and a Markdown instruction body without execution."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("gate descriptor %s could not be read: %s", path.name, exc)
        return None
    if not text.startswith("---\n"):
        logger.warning("gate descriptor %s is missing JSON front matter — ignored", path.name)
        return None
    marker = text.find("\n---\n", 4)
    if marker < 0:
        logger.warning("gate descriptor %s has unterminated JSON front matter — ignored", path.name)
        return None
    try:
        feature = json.loads(text[4:marker])
    except json.JSONDecodeError as exc:
        logger.warning("gate descriptor %s has invalid JSON front matter: %s", path.name, exc)
        return None
    if isinstance(feature, dict) and not str(feature.get("prompt_instruction") or "").strip():
        feature["prompt_instruction"] = text[marker + 5:].strip()
    return _validate(path, feature, expected_id=_id_from_markdown_path(path))


def _load_module(path: Path) -> Optional[Any]:
    name = _module_name(path)
    if name in sys.modules:
        return sys.modules[name]
    try:
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot build import spec for {path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    except Exception as exc:  # noqa: BLE001 — one bad plugin must not break the loop
        sys.modules.pop(name, None)
        logger.warning("legacy Python gate %s failed to import: %s", path.name, exc)
        return None


def _discover() -> Dict[str, Dict[str, Any]]:
    found: Dict[str, Dict[str, Any]] = {}
    if not _GATES_DIR.is_dir():
        return found
    # Markdown is canonical and wins any temporary duplicate during migration.
    for path in sorted(_GATES_DIR.glob(f"*{MARKDOWN_GATE_SUFFIX}")):
        descriptor = _load_markdown(path)
        if descriptor is not None:
            found[descriptor["id"]] = descriptor
    for path in sorted(_GATES_DIR.glob("*.py")):
        if path.name.startswith("_") or path.stem in found:
            continue
        module = _load_module(path)
        if module is None:
            continue
        descriptor = _validate(path, getattr(module, "FEATURE", None))
        if descriptor is not None:
            found[descriptor["id"]] = descriptor
    return found


def gate_types() -> Dict[str, Dict[str, Any]]:
    """Registered gate types, keyed by id (discovered once, then cached)."""
    global _registry
    if _registry is None:
        _registry = _discover()
    return _registry


def reload_gate_types() -> Dict[str, Dict[str, Any]]:
    """Re-scan the catalog — useful to a development loop dropping in a gate."""
    global _registry
    _registry = None
    return gate_types()


def gate_type_menu() -> List[Dict[str, Any]]:
    """Registry descriptors as JSON-safe catalog rows for the agent UI."""
    return [{
        "id": descriptor["id"],
        "display_name": descriptor["display_name"],
        "description": descriptor["description"],
        "prompt_instruction": descriptor["prompt_instruction"],
        "phase": descriptor["phase"],
        "inject": descriptor["inject"],
        "order": descriptor["order"],
        "status": descriptor["status"],
        "default_enabled": descriptor["default_enabled"],
        "mode_scoped": descriptor["mode_scoped"],
        "append_message": descriptor["append_message"],
        "params": descriptor["params"],
    } for descriptor in sorted(gate_types().values(), key=lambda d: (d["order"], d["id"]))]


# ── Instance resolution ──────────────────────────────────────────────────────

def _metadata(agent_rec: Optional[dict]) -> Dict[str, Any]:
    meta = (agent_rec or {}).get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta) or {}
        except Exception:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _gates_block(agent_rec: Optional[dict]) -> Optional[Any]:
    meta = _metadata(agent_rec)
    return meta.get("gates") if "gates" in meta else None


def _scope_allows(scope: Any, mode: Optional[str]) -> bool:
    if mode is None:
        return True
    if not isinstance(scope, list) or not scope or MODE_WILDCARD in scope:
        return True
    return str(mode) in [str(s) for s in scope]


def _configured_rows(block: Any) -> List[Dict[str, Any]]:
    """Normalize stored rows without activating anything; malformed means empty."""
    items = block.get("gates") if isinstance(block, dict) else block
    if not isinstance(items, list):
        return []
    known = gate_types()
    out: List[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        gate_type = str(item.get("type") or "").strip()
        gate_type = _LEGACY_GATE_TYPE_ALIASES.get(gate_type, gate_type)
        if gate_type not in known or gate_type in seen:
            continue
        seen.add(gate_type)
        row = {
            "type": gate_type,
            "enabled": item.get("enabled") is True,
            "params": dict(item.get("params")) if isinstance(item.get("params"), dict) else {},
            "message": str(item.get("message") or ""),
            "scope": list(item["scope"]) if isinstance(item.get("scope"), list) and item["scope"] else None,
        }
        # Closing Message no longer has a user-facing master switch.  Preserve a
        # legacy master-off row's intent by projecting it as the always-available
        # coordinator with every independently controlled feature explicitly off.
        # The canonical replacement is persisted on the next editor save.
        if gate_type == "closing_message" and not row["enabled"]:
            row["enabled"] = True
            row["params"].update({
                "require_flag": False,
                "require_notification": False,
                "output_closer_enabled": False,
            })
        out.append(row)
    return out


def _instances(block: Any, mode: Optional[str]) -> List[Dict[str, Any]]:
    """Enabled rows used by prompt rendering and enforcement.

    A descriptor may opt into a durable default-on behavior. An explicit stored
    row still wins, which makes turning that gate off an ordinary saved override.
    """
    known = gate_types()
    configured = {item["type"]: item for item in _configured_rows(block)}
    for gate_id, descriptor in known.items():
        if gate_id not in configured and descriptor.get("default_enabled"):
            configured[gate_id] = {
                "type": gate_id, "enabled": True, "params": {},
                "message": "", "scope": None,
            }
    return [
        item for item in configured.values()
        if item["enabled"]
        and (
            not known[item["type"]].get("mode_scoped", True)
            or _scope_allows(item.get("scope"), mode)
        )
    ]


def _project(instances: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Project enabled instances onto the effective contract view, seeding OFF."""
    known = gate_types()
    view: Dict[str, Any] = {}
    for descriptor in known.values():
        entry = descriptor["view"]
        view[entry["key"]] = 0 if entry["kind"] == "int" else False
    for inst in instances:
        descriptor = known.get(inst["type"])
        if descriptor is None:
            continue
        entry = descriptor["view"]
        if entry["kind"] == "int":
            raw = inst["params"].get(entry["param"], entry.get("default", 0))
            try:
                value = int(raw or 0)
            except (TypeError, ValueError):
                logger.warning("gate %s param %r is not an integer (%r) — treated as 0", inst["type"], entry["param"], raw)
                value = 0
            view[entry["key"]] = max(0, value)
        else:
            view[entry["key"]] = True
    return view


def gate_view_for_agent(agent_rec: Optional[dict], mode: Optional[str] = None) -> Dict[str, Any]:
    """The catalog-only gate view; absent or malformed metadata means all OFF."""
    return _project(_instances(_gates_block(agent_rec), mode))


def instances_for_agent(agent_rec: Optional[dict], mode: Optional[str] = None) -> List[Dict[str, Any]]:
    """One catalog row per known type, with missing instances explicitly OFF."""
    configured = {item["type"]: item for item in _configured_rows(_gates_block(agent_rec))}
    rows: List[Dict[str, Any]] = []
    for descriptor in sorted(gate_types().values(), key=lambda d: (d["order"], d["id"])):
        item = configured.get(descriptor["id"])
        if item is None:
            item = {
                "type": descriptor["id"],
                "enabled": bool(descriptor.get("default_enabled")),
                "params": {}, "message": "", "scope": None,
            }
        rows.append(item)
    return rows


def enabled_instance(
    agent_rec: Optional[dict], gate_type: str, mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """The enabled, in-scope instance of ``gate_type``, or None.

    Completion-path consumers (e.g. ``app/agent/closing_message.py``) read their
    own instance through this accessor instead of ``prompt_instructions_for_agent``
    so a ``completion``-inject gate's instruction is never part of the standing
    ``## Contract`` block.
    """
    if gate_type not in gate_types():
        return None
    for instance in _instances(_gates_block(agent_rec), mode):
        if instance["type"] == gate_type:
            return instance
    return None


def prompt_instructions_for_agent(agent_rec: Optional[dict], mode: Optional[str] = None) -> Dict[str, str]:
    """Enabled ``prompt``-inject instructions keyed by their contract-view key.

    ``completion``-inject types are excluded here: their instruction belongs to
    the end-of-run injection only, so it can never reach the standing block or
    the context estimate even if its key were ever added to ``active_rules()``.
    """
    instructions: Dict[str, str] = {}
    known = gate_types()
    for instance in _instances(_gates_block(agent_rec), mode):
        descriptor = known.get(instance["type"])
        if descriptor is None:
            continue
        if descriptor.get("inject") != INJECT_PROMPT:
            continue
        floor = str(descriptor.get("prompt_instruction") or "").strip()
        custom = str(instance.get("message") or "").strip()
        instruction = (
            f"{floor}\n\nAdditional agent guidance:\n{custom}"
            if floor and custom and descriptor.get("append_message")
            else (custom or floor)
        )
        if instruction:
            instructions[descriptor["view"]["key"]] = instruction
    return instructions

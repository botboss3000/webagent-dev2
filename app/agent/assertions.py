"""Run-contract assertions policy for the ``assertions`` gate.

Every run carries a contract the agent DECLARES and then ANSWERS FOR. Two beats,
both mechanical:

* ``app.agent.contracts.check_assertions_declared`` holds the run's first tool
  call back until it is a ``present_chat_component`` TODO LIST, and captures the
  declared items from that call's own arguments. Nothing else opens the contract:
  the declaration is the agent's statement of what the user actually asked for.
* the loop's completion path calls :func:`missing_resolutions` and, while an
  assertion is still unanswered, re-injects :func:`resolution_instruction` and
  continues the run under a requirement-compliance budget.

An assertion is RESOLVED by either channel:

* an explicit line-anchored answer in the agent's final message —
  ``ASSERTION <n>: MET`` or ``ASSERTION <n>: NOT MET — <reason>`` — where ``n`` is
  the 1-based declaration order. A stated "not met" is a VALID close; only an
  unanswered assertion blocks.
* a MACHINE-CHECKED layout assertion. A spatial assertion (after/before/outside/
  …) is proven by the ``webagent_layout_assertion`` payload the browser tools
  already record, so a declaration whose label names the recorded relation and
  anchor is resolved without an extra prose line.

This is why the old spatial-keyword regex is gone: layout is one KIND of
assertion here, declared like any other, rather than a separate gate whose
requirements were scraped out of the user's prose by pattern.

Guidance layers stack on the descriptor's floor, mirroring
``app/agent/closing_message.py`` and ``app/agent/pre_tool_progress.py``:

1. the descriptor body (``prompt_instruction``) — the floor, always present;
2. the app-level ``gate_settings['assertions']`` params — ``declaration`` and
   ``resolution`` (both ``level: "app"``) plus ``format`` when it is set app-wide
   — applied app-wide, edited on App Settings → Agent tools & abilities → Gates.
   There is deliberately NO generic app-level guidance blob: app-wide guidance IS
   these params;
3. the per-agent instance params (``format``) — appended last, which is where a
   house style belongs.

Every blank layer is skipped, so a gate nobody configured still delivers a usable
floor rather than an error.

The budget is a REQUIREMENT-COMPLIANCE budget, NOT error recovery: nothing has
failed, the run simply has not yet answered for its contract. It is the AGENT's
own limit (Agents → Runtime → Limits) clamped to the APP ceiling (App Settings →
App Functions), mirroring the closing-message gate — and deliberately separate
from the runtime page's resume/error counters, which have a different trigger,
counter and lifecycle.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

GATE_TYPE = "assertions"

#: The completion tool that opens the contract.
DECLARATION_TOOL = "present_chat_component"

#: Requirement-compliance budget. The effective value is the AGENT's own limit
#: clamped to the APP ceiling; this is the fallback when neither layer is set.
DEFAULT_MAX_ATTEMPTS = 4

#: App-level key (``app-settings.json``) holding the ceiling.
CEILING_KEY = "run_max_assertions_attempts"

#: Per-agent metadata key holding the agent's own limit.
AGENT_KEY = "assertions_max_attempts"

#: Durable metadata key written on the published opening-bubble row. The loop's
#: publish site writes it and nothing else does, so the crash-resume latch
#: (``has_declared_assertions``) can tell whether this run already introduced
#: itself without re-publishing a second bubble for the same message.
ANNOUNCED_KEY = "assertions_announced"

#: The two per-run contract-state flags that bound the opening-message window —
#: the exact mirror of the pre-tool progress-message gate's pair. ``PENDING`` is set the
#: moment the declaration is captured and cleared when the window closes;
#: ``DONE`` records that this gate is finished with the run, so the opening
#: message is never published twice.
PENDING_KEY = "assertion_announcement_pending"
DONE_KEY = "assertion_announcement_done"

#: The marker returned by :func:`missing_resolutions` when the run never opened
#: its contract at all. Not a label — the caller renders it as "declaration".
DECLARE_MARKER = "declare"

# Line-anchored so prose that merely mentions the word "assertion" can never
# satisfy the contract. Declaration order is the index the agent was asked for.
_RESOLUTION_RE = re.compile(
    r"^[ \t]*ASSERTION[ \t]+(?P<index>\d+)[ \t]*:[ \t]*(?P<status>MET|NOT[ \t]+MET)\b",
    re.IGNORECASE | re.MULTILINE,
)

_SETTINGS_FILE = (
    Path(__file__).resolve().parents[2] / "data" / "config" / "app-settings.json"
)


# ── Configuration layers ─────────────────────────────────────────────────────

def _settings_block() -> Dict[str, Any]:
    """The raw app-settings.json object; ``{}`` when absent or unreadable.

    Missing file, bad JSON, or a non-object payload all mean "not configured" —
    config must never fail a run.
    """
    try:
        data = json.loads(_SETTINGS_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — config must never fail a run
        return {}
    return data if isinstance(data, dict) else {}


def _app_int(key: str, default: int) -> int:
    """One top-level app-settings integer; ``default`` when unset or invalid."""
    raw = _settings_block().get(key)
    if raw is None or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _agent_positive_int(agent_rec: Optional[dict], key: str) -> Optional[int]:
    """One per-agent budget from agent metadata; None = inherit the app value.

    ``0``/blank/absent all mean "inherit", so a stale zero can never silently
    disable the gate.
    """
    meta = (agent_rec or {}).get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta or "{}")
        except Exception:  # noqa: BLE001 — a bad blob means "not configured"
            meta = {}
    if not isinstance(meta, dict):
        return None
    raw = meta.get(key)
    if raw is None or isinstance(raw, bool):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def effective_max_attempts(agent_rec: Optional[dict] = None) -> int:
    """The corrective-turn budget: ``min(agent limit, app ceiling)``."""
    ceiling = _app_int(CEILING_KEY, DEFAULT_MAX_ATTEMPTS)
    agent_max = _agent_positive_int(agent_rec, AGENT_KEY)
    if agent_max is None:
        return ceiling
    return min(agent_max, ceiling)


def app_gate_values() -> Dict[str, str]:
    """This gate's APP-LEVEL params (App Settings → Gates), blank keys dropped.

    Shape read: ``{"gate_settings": {"assertions": {"declaration": "…"}}}``.
    """
    block = _settings_block().get("gate_settings")
    values = block.get(GATE_TYPE) if isinstance(block, dict) else None
    if not isinstance(values, dict):
        return {}
    out: Dict[str, str] = {}
    for key, value in values.items():
        if isinstance(value, bool) or value is None:
            continue
        if not isinstance(value, (str, int, float)):
            continue
        text = str(value).strip()
        if text:
            out[str(key)] = text
    return out


def _param_spec(descriptor: Optional[dict], name: str) -> Dict[str, Any]:
    params = (descriptor or {}).get("params")
    spec = (params or {}).get(name)
    return spec if isinstance(spec, dict) else {}


# ── Instruction composition ──────────────────────────────────────────────────

def assemble_instruction(instance: dict, descriptor: Optional[dict] = None) -> str:
    """Compose the declaration beat from the floor plus every set guidance layer.

    Order: floor → app-level declaration/resolution guidance → per-agent format.
    Every blank layer is skipped, so an unconfigured gate still delivers a usable
    floor rather than an error.
    """
    parts: List[str] = []
    floor = str((descriptor or {}).get("prompt_instruction") or "").strip()
    if floor:
        parts.append(floor)
    app_params = app_gate_values()
    for key in ("declaration", "resolution"):
        value = app_params.get(key, "")
        if value:
            parts.append(value)
    # The app-wide style is a LAYER like the two above, so it appends BEFORE the
    # per-agent style — exactly how the closing_message gate treats its own
    # app-level format (tests/test_closing_message.py). Reading it only to
    # suppress the descriptor default would make the app-level "Assertion style
    # guidance" field a silent no-op.
    app_format = str(app_params.get("format") or "").strip()
    if app_format:
        parts.append(app_format)
    params = instance.get("params") if isinstance(instance.get("params"), dict) else {}
    value = str(params.get("format") or "").strip()
    if not value and not app_format:
        # The descriptor default IS this layer's shipped guidance. The
        # assertions gate ships none, so an unconfigured agent phrases its own;
        # an app-level value supersedes the default.
        value = str(_param_spec(descriptor, "format").get("default") or "").strip()
    if value:
        parts.append(value)
    return "\n".join(parts).strip()


def _resolution_tail(instance: dict, descriptor: Optional[dict]) -> str:
    """The app-level resolution guidance plus the app-wide and per-agent styles."""
    parts: List[str] = []
    app_params = app_gate_values()
    resolution = app_params.get("resolution", "")
    if resolution:
        parts.append(resolution)
    app_format = str(app_params.get("format") or "").strip()
    if app_format:
        parts.append(app_format)
    params = instance.get("params") if isinstance(instance.get("params"), dict) else {}
    value = str(params.get("format") or "").strip()
    if value:
        parts.append(value)
    return "\n".join(parts).strip()


# ── Policy resolution ────────────────────────────────────────────────────────

def resolve(agent_rec: Optional[dict], mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The active assertions policy, or None when the gate is off/out of scope."""
    try:
        from app.agent.gate_registry import enabled_instance, gate_types

        instance = enabled_instance(agent_rec, GATE_TYPE, mode=mode)
        if instance is None:
            return None
        descriptor = gate_types().get(GATE_TYPE)
        if descriptor is None:
            return None
    except Exception:  # noqa: BLE001 — gate discovery must never fail a run
        return None
    return {
        "instance": instance,
        "max_attempts": effective_max_attempts(agent_rec),
        "instruction": assemble_instruction(instance, descriptor),
        "resolution_tail": _resolution_tail(instance, descriptor),
    }


def instruction(policy: Optional[Dict[str, Any]]) -> str:
    """The declaration text fed back when the first tool call is intercepted."""
    return str((policy or {}).get("instruction") or "").strip()


# ── Declaration capture ──────────────────────────────────────────────────────

def _declared_items(args: Dict[str, Any]) -> Any:
    """The raw item list from whichever shape the call used, or ``None``.

    The canonical shape nests the items under ``data`` — ``{"type": "todo_list",
    "data": {"items": [...]}}`` — which is what the tool's own spec and
    ``app/chat_components.validate_spec`` read. A model that flattens the call
    (``{"type": "todo_list", "items": [...]}``) or hands ``data`` over as a JSON
    string is still declaring the same thing, and holding its run back over the
    nesting alone would dead-end the whole run, so both variants are accepted as
    equivalent declarations.
    """
    data = args.get("data")
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except Exception:  # noqa: BLE001 — a malformed data blob is not a declaration
            data = None
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return data["items"]
    return args.get("items")


def items_from_tool_args(tool_args: Any) -> List[Dict[str, str]]:
    """The checklist items a ``present_chat_component`` call declares, or ``[]``.

    Reads the call's OWN arguments (accepted as a dict or a JSON string), so the
    capture is synchronous and needs no store round-trip. An empty list means the
    call was not a usable declaration and the rule keeps holding the run back.

    An item is normally an object carrying a ``label``; a bare string is accepted
    as its own label, because that is a declaration a model plausibly writes and
    rejecting it would hold every tool call in the run back for a shape
    difference alone.
    """
    args = tool_args
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:  # noqa: BLE001 — a malformed call is simply not a declaration
            return []
    if not isinstance(args, dict):
        return []
    if str(args.get("type") or "").strip() != "todo_list":
        return []
    items = _declared_items(args)
    if not isinstance(items, list):
        return []
    out: List[Dict[str, str]] = []
    for item in items:
        if isinstance(item, str):
            label = item.strip()
            if label:
                out.append({"id": "", "label": label[:240]})
            continue
        if not isinstance(item, dict):
            continue
        # `text` is accepted as an alias for `label` so this capture stays in step
        # with app.chat_components.validate_spec, which accepts it too. Without
        # the alias a model that writes {"text": ...} declares nothing, the rule
        # keeps holding every call back, and the run dies on the stall guard.
        label = str(item.get("label") or item.get("text") or "").strip()
        if label:
            out.append({"id": str(item.get("id") or ""), "label": label[:240]})
    return out


def declares_contract_in_calls(tool_calls: Any) -> bool:
    """Whether THIS step's own tool calls open the assertions contract.

    The declaration beat's prose travels WITH the declaration call, and the
    capture (``check_assertions_declared``) only happens when that call EXECUTES
    — which is after its assistant row has been written. So the loop cannot use
    ``assertion_announcement_pending`` to recognise that row; it probes the
    step's own call arguments instead, which is mechanical and needs no store
    round-trip. A ``True`` answer means the prose on this step IS the run's
    introduction, so it is published as a ``main`` bubble rather than folded
    into the run's activity.

    Accepts the loop's persisted call shape
    (``{"function": {"name", "arguments"}}``) and provider objects alike.
    """
    for call in tool_calls or []:
        if isinstance(call, dict):
            fn = call.get("function")
            if isinstance(fn, dict):
                name, args = fn.get("name"), fn.get("arguments")
            else:
                name = call.get("name")
                args = call.get("arguments") if call.get("arguments") is not None else call.get("args")
        else:
            fn = getattr(call, "function", None)
            name = getattr(fn, "name", None) or getattr(call, "name", None)
            args = getattr(fn, "arguments", None)
        name = str(name or "").strip()
        if name and name != "present_chat_component":
            continue
        if items_from_tool_args(args):
            return True
    return False


# ── Resolution ───────────────────────────────────────────────────────────────

def _resolved_indexes(content: str) -> set:
    """Declaration indexes the agent explicitly answered for in its final message."""
    return {int(match.group("index")) for match in _RESOLUTION_RE.finditer(str(content or ""))}


def _layout_covers(label: str, assertions: List[Dict[str, Any]]) -> bool:
    """Whether a passing machine-checked layout assertion proves this label.

    The declaration names the relation and the anchor ("… after the model
    table"); the browser payload records the same pair. Both must appear in the
    label for the payload to stand in for a prose answer.
    """
    text = str(label or "").lower()
    if not text:
        return False
    for assertion in assertions:
        if not isinstance(assertion, dict) or not assertion.get("passed"):
            continue
        relation = str(assertion.get("relation") or "").lower().strip()
        anchor = str(assertion.get("anchor") or "").lower().strip()
        if relation and anchor and relation in text and anchor in text:
            return True
    return False


def missing_resolutions(
    content: str,
    state: Dict[str, Any],
    policy: Optional[Dict[str, Any]],
) -> List[str]:
    """Assertions the run has not yet answered for, in declaration order.

    Returns the :data:`DECLARE_MARKER` alone when the run never opened its
    contract, ``[]`` when every assertion is resolved, and ``[]`` when the gate
    is off — so a run the gate does not govern is never asked for anything.
    """
    if policy is None:
        return []
    items = list(state.get("assertion_items") or [])
    if not items:
        return [DECLARE_MARKER]
    resolved = _resolved_indexes(content)
    machine = list(state.get("ui_layout_assertions") or [])
    missing: List[str] = []
    for index, item in enumerate(items, start=1):
        label = str((item or {}).get("label") or "") if isinstance(item, dict) else ""
        if index in resolved:
            continue
        if _layout_covers(label, machine):
            continue
        missing.append(label or f"assertion {index}")
    return missing


def resolution_instruction(
    policy: Optional[Dict[str, Any]],
    missing: List[str],
) -> str:
    """The corrective message injected while an assertion is still unanswered."""
    labels = [str(item).strip() for item in (missing or []) if str(item).strip()]
    if not labels or labels == [DECLARE_MARKER]:
        opening = (
            "Contract: this run declared no assertions. Before it may finish, "
            "declare the user's concrete requirements as a checklist with "
            "present_chat_component (type 'todo_list'), one item per requirement."
        )
    else:
        opening = (
            "Contract: this run may not finish until you have answered for every "
            "assertion you declared. Still unanswered: "
            + "; ".join(labels)
            + "."
        )
    body = (
        " In your final message, answer each assertion on its own line, numbered "
        "in declaration order: 'ASSERTION 1: MET — <how you verified it>' or "
        "'ASSERTION 2: NOT MET — <why not>'. A 'NOT MET' with a stated reason is "
        "an acceptable close; an unanswered assertion is not."
    )
    tail = str((policy or {}).get("resolution_tail") or "").strip()
    return opening + body + (("\n" + tail) if tail else "")


# ── Opening message — the run's introduction ─────────────────────────────────
# The declaration beat does double duty. It opens the contract AND it is the
# run's INTRODUCTION: the prose the agent posts alongside the checklist is
# published by the loop as the run's promoted opening bubble (a ``main`` row),
# so the user sees what the run is about to do before any work lands. This makes
# the assertions gate the introductory gate.
#
# The publication mirrors the pre-tool progress-message gate exactly, and for the same
# reason it does NOT set ``closing_promoted``: the opening message is not the
# answer to the request, so the run still promotes its one terminal answer
# later. Promoting an opening bubble therefore cannot starve the closing gate.

def is_publishable(content: str) -> bool:
    """Whether a reply qualifies as the run's opening message.

    The ONLY requirement, by design: the agent produced a message. Nothing here
    judges the wording — the introduction is information for the user, and a run
    must never be held back by the quality of its own greeting.
    """
    return bool(str(content or "").strip())


def presentation(content: str) -> Dict[str, Any]:
    """The compact, durable presentation record stored on the published row.

    Deliberately tiny and non-judgemental: it records THAT the run introduced
    itself, never how well. ``format_configured`` tells a reader whether the
    shipped floor or a configured style shaped the message.
    """
    text = str(content or "")
    return {
        "announced": True,
        "chars": len(text),
        "format_configured": bool(
            app_gate_values().get("format") or app_gate_values().get("declaration")
        ),
    }


async def already_announced(db: Any, user_id: str, session_id: str) -> bool:
    """Whether THIS run already published its opening message.

    The per-run contract state dies with the run, so a crash-resume would start
    with ``assertion_announcement_done`` false and publish a second introduction.
    The published row carries :data:`ANNOUNCED_KEY`, written ONLY by the publish
    site, so the transcript is the second source of truth. Best-effort by design:
    a backend without the probe, or an unreadable transcript, reports "not
    announced" and the run behaves exactly as it would have.
    """
    probe = getattr(db, "has_declared_assertions", None)
    if probe is None:
        return False
    try:
        return bool(await probe(user_id, session_id))
    except Exception:  # noqa: BLE001 — the latch must never fail a run
        logger.debug("assertions announcement: durable latch probe failed", exc_info=True)
        return False

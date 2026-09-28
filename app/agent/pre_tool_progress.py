"""Pre-tool progress-message policy for the ``pre_tool_progress_message`` gate.

The gate INFORMS; it does not gate. In AUTO mode the agent posts the progress update it is
about to execute as a user-visible message before its first file change, and then
carries on in the same run. Nothing here validates that progress update: there is no audit,
no approval round, no reviewer, no second opinion. The only pass/fail is that a
progress update was produced and published — the run proceeds with the work either
way. Auditing and refining progress updates is a separate, later concern.

Two halves cooperate and neither can wedge the run:

* ``app.agent.contracts.check_pre_tool_progress_message`` intercepts the FIRST edit and
  feeds this module's assembled announcement back as its result — the same
  ``contract_blocked`` path every mechanical rule uses;
* the loop's completion path recognises the tool-free message that instruction
  asked for, publishes it as a ``main`` row (a real reply — the mirror of the
  closing gate's promotion) and CONTINUES the run instead of terminating it.

The announcement window is deliberately narrow. It opens when the instruction is
delivered and closes at whichever comes first: the progress update it asked for, or
the next tool that actually executes. A reply arriving after the agent has moved
on is the run's own answer, never a late progress update.

Guidance layers stack on the descriptor's floor, mirroring
``app/agent/closing_message.py``:

1. the descriptor body (``prompt_instruction``) — the floor, always present;
2. the app-level ``gate_settings['pre_tool_progress_message']`` params (App Settings →
   Agent tools & abilities → Gates) — applied app-wide;
3. the per-agent instance params (``format``) — appended last, which is where a
   house style belongs.

``format`` is GUIDANCE: it rides in the announcement and is never checked.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

GATE_TYPE = "pre_tool_progress_message"
_LEGACY_GATE_TYPE = "plan_before_edit"
_LEGACY_META_KEY = "pre_tool_progress_message_sent"

#: Durable metadata key written on the published progress update row. The completion path
#: also probes for it so a crash-resume cannot publish a second progress update.
META_KEY = "pre_tool_progress_message_sent"

#: The two per-run contract-state flags that bound the announcement window.
#: ``PENDING`` is set when the announcement is delivered and cleared when the
#: window closes; ``DONE`` records that the gate is finished with this run
#: (published, or the window expired because the agent moved on) so the
#: announcement is never repeated.
PENDING_KEY = "pre_tool_progress_message_pending"
DONE_KEY = "pre_tool_progress_message_done"

_SETTINGS_FILE = (
    Path(__file__).resolve().parents[2] / "data" / "config" / "app-settings.json"
)


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


def app_gate_values() -> Dict[str, str]:
    """This gate's APP-LEVEL params (App Settings → Gates), blank keys dropped.

    Shape read: ``{"gate_settings": {"pre_tool_progress_message": {"format": "…"}}}``.
    Empty when the layer is unconfigured, which is the normal case.
    """
    block = _settings_block().get("gate_settings")
    values = block.get(GATE_TYPE) if isinstance(block, dict) else None
    if not isinstance(values, dict) and isinstance(block, dict):
        values = block.get(_LEGACY_GATE_TYPE)
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


def assemble_instruction(instance: dict, descriptor: Optional[dict] = None) -> str:
    """Compose the announcement from the floor plus every set guidance layer.

    Order: floor → app-level format → per-agent format. Every blank layer is
    skipped, so a gate nobody configured still delivers a usable announcement
    rather than an error.
    """
    parts: list = []
    floor = str((descriptor or {}).get("prompt_instruction") or "").strip()
    if floor:
        parts.append(floor)
    app_params = app_gate_values()
    app_format = app_params.get("format", "")
    if app_format:
        parts.append(app_format)
    params = instance.get("params") if isinstance(instance.get("params"), dict) else {}
    value = str(params.get("format") or "").strip()
    if not value and not app_format:
        # The descriptor default is the shipped, user-editable agent guidance.
        # An app-level value supersedes that default when no per-agent instruction
        # has been saved.
        value = str(_param_spec(descriptor, "format").get("default") or "").strip()
    if value:
        parts.append(value)
    return "\n".join(parts).strip()


# ── Policy resolution ────────────────────────────────────────────────────────

def resolve(agent_rec: Optional[dict], mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The active pre-tool progress-message policy, or None when the gate is off/out of scope."""
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
        "instruction": assemble_instruction(instance, descriptor),
    }


def instruction(policy: Optional[Dict[str, Any]]) -> str:
    """The announcement text fed back when the first edit is intercepted."""
    return str((policy or {}).get("instruction") or "").strip()


async def already_sent(db: Any, user_id: str, session_id: str) -> bool:
    """Whether THIS run already published its progress update — the durable half of the latch.

    The per-run contract state dies with the run, so a crash-resume would start
    with ``pre_tool_progress_message_done`` false and ask for the progress update a second time. The
    published row carries :data:`META_KEY`, written ONLY by the publish site, so
    the transcript is the second source of truth. Best-effort by design: a
    backend without the probe, or an unreadable transcript, reports "not sent" and the run behaves exactly as it would have.
    """
    probe = getattr(db, "has_pre_tool_progress_message", None)
    if probe is None:
        return False
    try:
        return bool(await probe(user_id, session_id))
    except Exception:  # noqa: BLE001 — the latch must never fail a run
        logger.debug("pre-tool progress message: durable latch probe failed", exc_info=True)
        return False


# ── Publishability and presentation ──────────────────────────────────────────

def is_publishable(content: str) -> bool:
    """Whether a tool-free reply is a progress update worth publishing.

    The ONLY requirement, by design: the agent produced a message. There is no
    completeness check, no format check and no reviewer — the progress update is information
    for the user, and a run must never be held back by the quality of one.
    """
    return bool(str(content or "").strip())


def presentation(content: str) -> Dict[str, Any]:
    """The compact, durable presentation record stored on the published row.

    Deliberately tiny and non-judgemental: it records THAT a progress update was announced,
    never how good it was. ``format_configured`` tells a reader whether the
    shipped guidance or a configured one styled the message.
    """
    text = str(content or "")
    return {
        "announced": True,
        "chars": len(text),
        "format_configured": bool(app_gate_values().get("format")),
    }

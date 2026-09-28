"""Parse the human-readable ``automation`` slot into structured rows.

The user writes free-form English describing both **scheduled** prompts
(cron-style) and **event-triggered** prompts ("when an email arrives..."),
along with how to deliver each result. An LLM call classifies every line
into one of two output types and emits a structured JSON object.

Cron expressions are validated with ``croniter`` before a cron row is
accepted. Event-trigger rows are validated against the registered event
source manifest (source name + event type must exist; filter is left as
an opaque dict that the source plugin interprets).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


VALID_CHANNELS = {
    "webchat", "telegram", "whatsapp", "sms",
    "email", "slack", "discord",
}


@dataclass
class ParsedTask:
    task_label: str
    prompt: str
    schedule_cron: str
    schedule_natural: str
    timezone: str
    channel: Optional[str]
    channel_recipient: Optional[str]
    silent: bool


@dataclass
class ParsedEventSubscription:
    task_label: str
    prompt: str
    source: str                 # e.g. "gmail"
    event_type: str             # e.g. "message_received"
    filter_dict: Dict[str, Any] = field(default_factory=dict)
    trigger_natural: str = ""
    channel: Optional[str] = None
    channel_recipient: Optional[str] = None
    silent: bool = False


@dataclass
class ParseResult:
    tasks: List[ParsedTask] = field(default_factory=list)
    event_subscriptions: List[ParsedEventSubscription] = field(default_factory=list)
    error: Optional[str] = None


_LLM_INSTRUCTION_HEADER = """You convert a free-form Automation file into a JSON object describing
two kinds of agent triggers:
  - "tasks": time/cron-based recurring prompts ("every weekday at 9am ...")
  - "event_subscriptions": triggered by an external event ("when an email arrives ...")

Output JSON shape (ALWAYS this top-level shape):
{
  "tasks": [ ... ],
  "event_subscriptions": [ ... ]
}

Each entry under "tasks" MUST have:
- task_label: short human label (1-6 words)
- prompt: the exact prompt sent to the agent on each fire
- schedule_cron: 5-field cron expression (minute hour day-of-month month day-of-week). Use "*" for any.
- schedule_natural: original human phrase describing when this runs
- timezone: IANA timezone string (e.g. "America/Los_Angeles"); use "UTC" if unspecified
- channel: one of webchat|telegram|whatsapp|sms|email|slack|discord, or null
- channel_recipient: external id (phone, chat id, email) or null
- silent: true if the task should NOT deliver a message anywhere

Each entry under "event_subscriptions" MUST have:
- task_label: short human label (1-6 words)
- prompt: the exact prompt sent to the agent when this trigger fires
- source: an event source name from the AVAILABLE_SOURCES list below
- event_type: one of the event_types listed for that source
- filter: an object whose keys are valid filter_fields for that source (may be empty {})
- trigger_natural: original human phrase describing the trigger
- channel: webchat|telegram|whatsapp|sms|email|slack|discord, or null if unspecified
  (when null the agent itself will ask the user which channel to use; that's preferred over guessing)
- channel_recipient: external id or null
- silent: true if delivery should be entirely suppressed

Rules:
- Output ONLY a JSON object with the two top-level keys. No prose. No markdown fences.
- If the user wrote no triggers, output {"tasks": [], "event_subscriptions": []}.
- If the user says "every minute", schedule_cron = "* * * * *".
- For event triggers, use ONLY a source from AVAILABLE_SOURCES below; if none fit, omit the entry.
- For event triggers, leave channel null UNLESS the user explicitly named a channel.
  The agent prefers to ask the user where to deliver rather than have a guess baked in.
- Do NOT invent triggers the user didn't describe.

"""


def _strip_code_fences(text: str) -> str:
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```\s*$", "", s)
    return s.strip()


def _extract_json_object(text: str) -> Optional[Dict[str, Any]]:
    s = _strip_code_fences(text)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", s)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    return data if isinstance(data, dict) else None


def _validate_cron(expr: str) -> bool:
    expr = (expr or "").strip()
    if not expr:
        return False
    try:
        from croniter import croniter
        return croniter.is_valid(expr)
    except ImportError:
        return len(expr.split()) == 5


def _coerce_task(raw: Dict[str, Any]) -> Optional[ParsedTask]:
    if not isinstance(raw, dict):
        return None
    prompt = (raw.get("prompt") or "").strip()
    cron = (raw.get("schedule_cron") or "").strip()
    if not prompt or not _validate_cron(cron):
        return None
    channel = raw.get("channel")
    if channel is not None:
        channel = str(channel).strip().lower() or None
        if channel == "none":
            channel = None
        if channel and channel not in VALID_CHANNELS:
            channel = None
    label = (raw.get("task_label") or "").strip() or prompt[:48]
    return ParsedTask(
        task_label=label,
        prompt=prompt,
        schedule_cron=cron,
        schedule_natural=(raw.get("schedule_natural") or "").strip(),
        timezone=(raw.get("timezone") or "UTC").strip() or "UTC",
        channel=channel,
        channel_recipient=(raw.get("channel_recipient") or None) or None,
        silent=bool(raw.get("silent")) or (channel is None and not raw.get("channel_recipient")),
    )


def _coerce_event_sub(
    raw: Dict[str, Any],
    valid_sources: Dict[str, List[str]],
) -> Optional[ParsedEventSubscription]:
    if not isinstance(raw, dict):
        return None
    prompt = (raw.get("prompt") or "").strip()
    source = (raw.get("source") or "").strip().lower()
    event_type = (raw.get("event_type") or "").strip().lower()
    if not prompt or not source or not event_type:
        return None
    if source not in valid_sources:
        logger.info("Discarding event sub: source %r not registered", source)
        return None
    if event_type not in valid_sources[source]:
        logger.info("Discarding event sub: %s does not emit event_type %r", source, event_type)
        return None
    filter_obj = raw.get("filter") or {}
    if not isinstance(filter_obj, dict):
        filter_obj = {}
    channel = raw.get("channel")
    if channel is not None:
        channel = str(channel).strip().lower() or None
        if channel == "none":
            channel = None
        if channel and channel not in VALID_CHANNELS:
            channel = None
    label = (raw.get("task_label") or "").strip() or prompt[:48]
    return ParsedEventSubscription(
        task_label=label,
        prompt=prompt,
        source=source,
        event_type=event_type,
        filter_dict=filter_obj,
        trigger_natural=(raw.get("trigger_natural") or "").strip(),
        channel=channel,
        channel_recipient=(raw.get("channel_recipient") or None) or None,
        silent=bool(raw.get("silent")),
    )


def _build_sources_block() -> tuple[str, Dict[str, List[str]]]:
    """Render the AVAILABLE_SOURCES manifest the LLM uses for classification.

    Returns (rendered_text, {source_name: [event_types]}) for downstream validation.
    """
    try:
        from app.events import get_manager
        mgr = get_manager()
        manifest = mgr.manifest()
    except Exception as e:
        logger.warning("Could not load event source manifest: %s", e)
        return ("AVAILABLE_SOURCES: (none registered)\n", {})

    if not manifest:
        return ("AVAILABLE_SOURCES: (none registered)\n", {})

    valid = {m["source"]: list(m.get("event_types") or []) for m in manifest}
    lines = ["AVAILABLE_SOURCES:"]
    for m in manifest:
        et = ", ".join(m.get("event_types") or [])
        fields = ", ".join(m.get("filter_fields") or []) or "(any keys allowed)"
        lines.append(
            f"- source={m['source']}  event_types=[{et}]  filter_fields={fields}"
        )
        if m.get("description"):
            lines.append(f"    {m['description']}")
        for ex in (m.get("examples") or []):
            lines.append(f"    example: {ex}")
    return ("\n".join(lines) + "\n", valid)


async def _call_llm(slot_content: str, sources_block: str, agent_context: Dict[str, Any]) -> str:
    try:
        from openai import AsyncOpenAI
    except ImportError:
        from app.openai_compat import AsyncOpenAI

    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or ""
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""

    if not api_key or not model:
        raise RuntimeError("LLM credentials not configured (LLM_API_KEY / LLM_MODEL).")

    client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=45.0)

    agent_hint = ""
    if agent_context:
        ag_name = agent_context.get("name") or ""
        ag_desc = agent_context.get("description") or ""
        if ag_name or ag_desc:
            agent_hint = f"\nAgent context (for reference only — do not put it in tasks):\nname: {ag_name}\ndescription: {ag_desc}\n"

    system_msg = _LLM_INSTRUCTION_HEADER + sources_block + agent_hint
    messages = [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": f"Automation file content:\n\n{slot_content}\n\nReturn the JSON object."},
    ]
    resp = await client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.0,
        max_tokens=2048,
    )
    return resp.choices[0].message.content or ""


async def parse_automation_file(
    slot_content: str,
    agent_context: Optional[Dict[str, Any]] = None,
) -> ParseResult:
    """Parse the slot into both cron tasks and event subscriptions."""
    text = (slot_content or "").strip()
    if not text:
        return ParseResult()

    if text.lower().startswith("# automation tasks") and "every weekday at 9am" in text.lower():
        return ParseResult()

    sources_block, valid_sources = _build_sources_block()

    try:
        raw_output = await _call_llm(text, sources_block, agent_context or {})
    except Exception as e:
        logger.warning("Automation parse LLM call failed: %s", e)
        return ParseResult(error=f"LLM call failed: {e}")

    data = _extract_json_object(raw_output)
    if data is None:
        # Back-compat: maybe the LLM returned the old bare array of cron tasks.
        try:
            arr = json.loads(_strip_code_fences(raw_output))
            if isinstance(arr, list):
                data = {"tasks": arr, "event_subscriptions": []}
        except Exception:
            data = None

    if data is None:
        logger.warning("Automation parse: could not extract JSON from LLM output")
        return ParseResult(error="Parser could not extract JSON from the LLM response.")

    tasks: List[ParsedTask] = []
    for item in (data.get("tasks") or []):
        t = _coerce_task(item)
        if t is not None:
            tasks.append(t)

    subs: List[ParsedEventSubscription] = []
    for item in (data.get("event_subscriptions") or []):
        s = _coerce_event_sub(item, valid_sources)
        if s is not None:
            subs.append(s)

    return ParseResult(tasks=tasks, event_subscriptions=subs)

"""Completion-path closing-message policy for the ``closing_message`` gate.

The WORKING agent — not the Output Closer — authors the run's promoted final
message. The closer lacks the working context, so it must defer; this module
owns everything that makes the agent's own message usable as the terminal
bubble:

- the instruction assembled at the completion path (the prompt injection),
- the strict, line-anchored ``FLAG: <name>`` envelope parser,
- the taxonomy downgrade rules (unknown/absent → safe default; ``complete`` is
  refused while completion gaps remain),
- the compact presentation metadata the chat UI reads on every rehydration path.

Guidance layers stack on the descriptor's floor. The floor is deliberately BARE
— it only tells the agent to give its final response, exactly as it would
without the gate. Every layer above it is optional and skipped when blank, so an
empty configuration still produces a closing turn rather than an error:

1. the descriptor body (``prompt_instruction``) — the bare floor, always present;
2. the app-level params (``gate_settings.closing_message`` in
   ``app-settings.json``) — the gate's OWN declared ``notification`` / ``format``
   values, applied app-wide — edited on App Settings → Agent tools & abilities →
   Gates. There is deliberately NO generic app-level guidance blob: app-wide
   guidance IS these params, so that surface stays exactly the tailored rows the
   gate declares and the app-level file carries no opaque prose;
3. the flag vocabulary clause — which ALSO carries the machine-readable
   ``FLAG: <name>`` envelope, because the bare floor no longer specifies it and
   this layer is always present;
4. the per-agent instance params (``notification``, ``format``) — the AGENT-level
   append, which is where a house style belongs (the ultra-compact bullet format
   shared_default carries, for example). Nothing ships on this layer for the
   closing message, so app level + agent level both blank means the agent
   answers freeform rather than inheriting someone else's taste.

There is deliberately NO per-instance instruction override. The instruction is a
machine-readable contract, so an agent-local free-text edit could silently break
the envelope and the badge with it. Each gate's configuration is tailored to
that gate: the Modes page renders only the params the gate declares for the
agent level.

The flag taxonomy's shipped default lives on the descriptor's ``flags`` param,
which is marked ``level: "app"`` — it is configured at the APP level only and is
never offered on an agent's Modes page. A freshly enabled gate therefore still
carries the machine-readable envelope with nothing typed in. The output format
ships EMPTY on purpose: format is taste, and taste belongs to the app's global
guidance or to the individual agent, never to the gate type every agent inherits.

FLAGS are the one layer that does not stack: the taxonomy is a single
vocabulary, so the app-level list WINS when set and the descriptor default is
the fallback. The per-agent instance is deliberately NOT read. That is because
the flags are destined to line up with backend/session status values, which only
works if one app-wide list defines them.

WHAT IS VALIDATED (end of run) — the flag names a member of the app-level
taxonomy; the notification / call-to-action is above 0 and at most the configured
word limit (``cta_max_words``, app-level, default 5); and the output message is
longer than 0. When the per-agent ``format_enforcement_min_tokens`` threshold is
enabled, a message strictly above it must also contain a Markdown list item.

Bullets, the 12-word bullet style, tone and prose style otherwise remain GUIDANCE:
short responses are promoted unchanged, and only the threshold-gated list
requirement costs a corrective
turn, and re-injection continues until the requirement is met. The budget is a
REQUIREMENT-COMPLIANCE budget, NOT error recovery — nothing has failed, the run
has simply not yet produced a message that meets the contract. It is the AGENT's
own limit (Agents → Runtime → Limits) clamped to the APP ceiling (App Settings →
App Functions), mirroring the self-healing resume knobs but deliberately NOT
sharing their counter, trigger or lifecycle (and never named "per error type").

Once the budget is spent the run completes with the message the agent produced,
promoted as a labelled PARTIAL — the badge carries the safe flag and the loop
inserts ONE user-visible status row — rather than diverging the completion path's
``turn_count -= 1; continue``. A run must never end with nothing to show.

The flag is ADVISORY for presentation — the durable run status stays
authoritative and the flag only drives the badge + call-to-action. It is
VALIDATED as a requirement, though: an off-taxonomy flag costs a corrective
turn instead of merely downgrading the badge.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.agent.context_control import estimate_tokens

logger = logging.getLogger(__name__)

GATE_TYPE = "closing_message"

#: Durable marker carried on the folded draft row, holding the run's
#: corrective-attempt tally. Deliberately NOT ``META_KEY`` — that name already
#: holds the promoted row's presentation record (the key the chat UI and the DB
#: viewer read). The per-run contract state dies with the run
#: (contracts.new_contract_state), so without a transcript copy a crash-resume
#: restarts this budget at attempt 1 and the gate can never reach
#: ``attempts_exhausted`` — the run loops until its *resume* budget gives out
#: instead of promoting its labelled PARTIAL. Mirrors ``pre_tool_progress_message_sent`` /
#: ``assertions_announced``: the mark is written ONLY by the closing retry site,
#: so a hit is authoritative.
ATTEMPTS_KEY = "closing_attempts"

#: Marker on the folded natural terminal row while a private status-only repair
#: is outstanding. The same durable probe as ``ATTEMPTS_KEY`` restores this row
#: after crash/resume without introducing another completion pipeline.
METADATA_CANDIDATE_KEY = "closing_metadata_candidate"

#: Taxonomy used when neither the instance nor the descriptor supplies one.
DEFAULT_FLAGS: Tuple[str, ...] = (
    "needs_input", "complete", "error", "partial", "unverified",
)

#: Flag written when the agent's flag is absent, unknown, or downgraded.
SAFE_FLAG = "partial"

#: The one flag that asserts the work is finished.
COMPLETE_FLAG = "complete"

#: The literal placeholder from the floor instruction — never a real flag.
PLACEHOLDER_FLAG = "<name>"

#: One-line gloss per shipped flag: the MEANING, not just the name.
#:
#: The clause carried the vocabulary and the envelope but never said what any flag
#: MEANT, so the model had to tell ``partial`` from ``unverified`` and ``complete``
#: from ``needs_input`` by the names alone — and the observed failure was a final
#: message carrying no flag line at all. Both surfaces that state this contract
#: (the pre-completion advisory and the corrective instruction) now carry these
#: definitions, because both compose from the one clause.
#:
#: Keyed by name: a custom app-level taxonomy may use a subset or add its own
#: flags, and a name with no gloss is left undefined rather than defined wrongly.
FLAG_MEANINGS: Dict[str, str] = {
    "needs_input": "the work is done, but you need a decision or an answer from the user",
    "complete": "the work the user asked for is finished",
    "error": "the work failed and you could not complete it",
    "partial": "the work is unfinished, or only partly done",
    "unverified": "the work is done, but you could not confirm that it works",
}

#: The completion path's turn DIRECTIVE: what this turn must DO, as opposed to what
#: the answer must LOOK like (the envelope). It belongs to the INSTRUCTION only —
#: never to the advisory, which is delivered before the run has finished its work
#: and must not read as a stop order.
#:
#: Without it the corrective turn read as permission to go back to work: a live run
#: was rejected three times, and between rejections answered "I'll look at the
#: current launcher and widget code before touching anything" — spending the
#: compliance budget on further tool turns instead of on the envelope.
CLOSE_NOW_DIRECTIVE = (
    "This message is the run's final answer: start no new work. Reply with only "
    "the prose the user should see, then call the required private status tool "
    "with the matching flag and notification. Never write flag or status protocol "
    "text in the reply itself."
)

#: The three validated requirements, in the order they are reported.
REQUIREMENTS: Tuple[str, ...] = ("flag", "notification", "message")

#: Corrective-turn budget. These are REQUIREMENT-COMPLIANCE budgets, not error
#: recovery: nothing has failed, the run simply has not yet produced a closing
#: message that meets the contract. They are deliberately separate from the
#: runtime page's resume/error counters (``run_max_resume_attempts`` …), which
#: have a different trigger, counter and lifecycle.
#:
#: The effective budget is the AGENT's own limit (Agents → Runtime → Limits)
#: clamped to the APP ceiling (App Settings → App Functions), exactly like the
#: self-healing knobs — so a per-agent value can never exceed the app cap. These
#: module values are the fallbacks used when neither layer is configured.
DEFAULT_MAX_ATTEMPTS = 8
DEFAULT_MAX_ATTEMPTS_PER_REQUIREMENT = 3

#: App-level keys (``app-settings.json``) holding the two ceilings.
CEILING_KEY = "run_max_closing_attempts"
CEILING_PER_REQUIREMENT_KEY = "run_max_closing_attempts_per_requirement"

#: Notification / call-to-action word limit used when nothing is configured.
DEFAULT_CTA_MAX_WORDS = 5

#: Metadata key on the final assistant row.
META_KEY = "closing_message"

#: Gate-owned metadata tool. It is injected only while the completion gate is
#: active; it is not a user-discoverable capability or a normal agent tool.
STATUS_TOOL_NAME = "set_closer_status"


def status_tool_definition(policy: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Return the private status schema only for enabled metadata requirements."""
    required: List[str] = []
    properties: Dict[str, Any] = {}
    if (policy or {}).get("require_flag", True):
        flags = [str(flag).strip().lower() for flag in (policy or {}).get("flags", []) if str(flag).strip()]
        flag_schema: Dict[str, Any] = {"type": "string", "minLength": 1}
        if flags:
            flag_schema["enum"] = flags
        properties["flag"] = flag_schema
        required.append("flag")
    if (policy or {}).get("require_notification", True):
        properties["notification"] = {"type": "string", "minLength": 1, "maxLength": 80}
        required.append("notification")
    if not required:
        return None
    return {
        "type": "function",
        "function": {
            "name": STATUS_TOOL_NAME,
            "description": "Record the enabled final response status metadata. This is internal, not user-visible.",
            "parameters": {
                "type": "object", "properties": properties,
                "required": required, "additionalProperties": False,
            },
        },
    }


def validate_status(args: Any, policy: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, str]], List[str]]:
    """Validate one private status-tool payload and return normalized metadata."""
    require_flag = bool((policy or {}).get("require_flag", True))
    require_notification = bool((policy or {}).get("require_notification", True))
    expected = ({"flag"} if require_flag else set()) | ({"notification"} if require_notification else set())
    if not isinstance(args, dict):
        return None, ([name for name in ("flag", "notification") if name in expected])
    unexpected = set(args) - expected
    flag = str(args.get("flag") or "").strip().lower()
    notification = str(args.get("notification") or "").strip()
    failures: List[str] = []
    taxonomy = [str(value).strip().lower() for value in (policy or {}).get("flags", [])]
    if require_flag and (not flag or (taxonomy and flag not in taxonomy) or unexpected):
        failures.append("flag")
    limit = int((policy or {}).get("cta_max_words") or 0)
    words = notification.split()
    if require_notification and (not words or len(notification) > 80 or (limit > 0 and len(words) > limit)):
        failures.append("notification")
    if unexpected and not failures:
        return None, ["flag" if require_flag else "notification"]
    if failures:
        return None, failures
    return {"flag": flag, "cta": notification}, []

_SETTINGS_FILE = (
    Path(__file__).resolve().parents[2] / "data" / "config" / "app-settings.json"
)

# Line-anchored so ordinary prose that merely contains the word "flag:" can
# never be mistaken for the envelope. The LAST match wins.
#
# The payload is captured LOOSELY and the name/status split is done after the
# match, because the model reaches for the INLINE form (``FLAG: complete — <the
# status>``) as readily as the two-line one. That is not hypothetical: a live run
# was rejected three times and promoted as a labelled PARTIAL purely because its
# status rode the SAME line as its flag — the strict one-token form matched
# nothing, so ``parse_envelope`` reported "no envelope at all" and all three
# requirements came due at once. The canonical layout stays flag-line *then*
# status-line; the inline form is the tolerant fallback, accepted only when a
# real separator (an em/en dash, a hyphen, or a colon) divides name from status.
# Dashes must be space-delimited, so a hyphenated flag name is never split.
_FLAG_LINE_RE = re.compile(r"^[ \t]*FLAG:[ \t]*(.*?)[ \t]*$", re.MULTILINE)
_FLAG_INLINE_SEP_RE = re.compile(r"(?:[ \t]+[—–-]+[ \t]+|[ \t]*:[ \t]*)")
_FLAG_SPLIT_RE = re.compile(r"[,\n;]+")


# ── Envelope ─────────────────────────────────────────────────────────────────

def parse_envelope(text: str) -> Dict[str, Any]:
    """Split a closing message into statement / flag / call-to-action.

    Layout the assembled instruction asks for::

        <the final message the user will see>

        FLAG: <name>
        <one short step or status update>

    The INLINE form is accepted as a tolerant fallback::

        <the final message the user will see>

        FLAG: <name> — <one short step or status update>

    Both layouts yield the same ``flag`` / ``cta`` pair, so a message whose status
    rode the same line as its flag is understood rather than read as carrying no
    envelope at all. Returns ``valid=False`` (with the untouched text as
    ``statement``) when no flag line is present, so a run without the gate behaves
    exactly as before.
    """
    raw = str(text or "")
    matches = list(_FLAG_LINE_RE.finditer(raw))
    if not matches:
        return {"valid": False, "statement": raw.strip(),
                "flag": "", "cta": "", "flag_line": ""}
    match = matches[-1]
    payload = match.group(1).strip()
    # Inline form: everything after the first real separator is the status. The
    # separator must carry content after it, so a trailing dash is not mistaken
    # for one and a bare ``FLAG: <name>`` still reads as flag-only.
    flag, cta = payload, ""
    separator = _FLAG_INLINE_SEP_RE.search(payload)
    if separator is not None and payload[separator.end():].strip():
        flag = payload[:separator.start()].strip()
        cta = payload[separator.end():].strip()
    if not cta:
        tail_lines = [line.strip() for line in raw[match.end():].splitlines()]
        cta = next((line for line in tail_lines if line), "")
    return {
        "valid": True,
        "statement": raw[:match.start()].strip(),
        "flag": flag,
        "cta": cta,
        "flag_line": match.group(0).strip(),
    }


# ── Taxonomy ─────────────────────────────────────────────────────────────────

def _param_spec(descriptor: Optional[dict], name: str) -> Dict[str, Any]:
    params = (descriptor or {}).get("params")
    spec = (params or {}).get(name)
    return spec if isinstance(spec, dict) else {}


def _configured_boolean(instance: dict, descriptor: Optional[dict], name: str) -> bool:
    """Resolve a per-agent feature switch, preserving shipped defaults."""
    params = instance.get("params") if isinstance(instance.get("params"), dict) else {}
    raw = params.get(name, _param_spec(descriptor, name).get("default", True))
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off", ""}
    return bool(raw)


def configured_flags(instance: dict, descriptor: Optional[dict] = None) -> List[str]:
    """The effective flag taxonomy — an APP-LEVEL setting.

    Precedence: the app-level list (App Settings → Agent tools & abilities →
    Gates) wins when set, then the descriptor's shipped ``flags`` default (the
    param is marked ``level: "app"``). The per-agent ``instance`` is deliberately
    NOT read: one app-wide vocabulary is what lets these flags line up with
    backend/session status values, so the flags never vary per agent.
    """
    raw = app_gate_values().get("flags", "")
    if not raw:
        raw = str(_param_spec(descriptor, "flags").get("default") or "").strip()
    if not raw:
        # No descriptor at all is the freeform path — never invent a taxonomy.
        raw = ", ".join(DEFAULT_FLAGS) if descriptor is not None else ""
    if not raw:
        return []
    flags = [part.strip().lower() for part in _FLAG_SPLIT_RE.split(raw) if part.strip()]
    return flags


def _safe_flag(allowed: Sequence[str]) -> str:
    """The downgrade target — always a member of the active taxonomy."""
    if not allowed:
        return SAFE_FLAG
    if SAFE_FLAG in allowed:
        return SAFE_FLAG
    return next((flag for flag in allowed if flag != COMPLETE_FLAG), allowed[0])


def resolve_flag(
    raw_flag: str,
    allowed: Optional[Sequence[str]],
    gaps: Optional[Sequence[str]] = None,
) -> Tuple[str, str]:
    """Return ``(flag, downgrade_reason)`` for one agent-authored flag.

    ``allowed=None`` means no taxonomy is configured: any token is accepted (the
    flag is advisory) except the literal placeholder. A configured taxonomy
    rejects unknown values.
    """
    name = str(raw_flag or "").strip().lower()
    taxonomy = list(allowed or [])
    if not name or name == PLACEHOLDER_FLAG.lower():
        return _safe_flag(taxonomy), "absent"
    if taxonomy and name not in taxonomy:
        return _safe_flag(taxonomy), "unknown"
    if name == COMPLETE_FLAG and gaps:
        return _safe_flag(taxonomy), "completion_gaps"
    return name, ""


# ── Guidance assembly ────────────────────────────────────────────────────────

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

    Shape read: ``{"gate_settings": {"closing_message": {"flags": "…", …}}}``.
    Empty when the layer is unconfigured, which is the normal case.

    Numbers are stored as real JSON numbers (the notification word limit) and
    everything else as text; both are normalised to a stripped string here so one
    reader serves every param. Bools are skipped — no gate declares one.
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


def configured_cta_max_words(descriptor: Optional[dict] = None) -> int:
    """The notification word limit — an APP-LEVEL setting, read like the taxonomy.

    Precedence: the app-level value wins when set, then the descriptor's shipped
    ``cta_max_words`` default. A blank or unparseable value falls back to
    ``DEFAULT_CTA_MAX_WORDS`` rather than to 0, so a bad config can never reject
    every closing message by accident.
    """
    raw = app_gate_values().get("cta_max_words", "")
    if not raw:
        raw = str(_param_spec(descriptor, "cta_max_words").get("default") or "")
    try:
        limit = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return DEFAULT_CTA_MAX_WORDS
    return limit if limit > 0 else DEFAULT_CTA_MAX_WORDS


def _app_int(key: str, default: int) -> int:
    """One app-level integer ceiling; blank/invalid falls back to the default."""
    try:
        value = int(float(str(_settings_block().get(key, "")).strip()))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _agent_positive_int(agent_rec: Optional[dict], key: str) -> Optional[int]:
    """One per-agent budget from agent metadata; None = inherit the app value.

    Mirrors how the resume knobs are stored (metadata, possibly a JSON string).
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
    """The overall corrective-turn budget: ``min(agent limit, app ceiling)``."""
    ceiling = _app_int(CEILING_KEY, DEFAULT_MAX_ATTEMPTS)
    agent_max = _agent_positive_int(agent_rec, "closing_max_attempts")
    if agent_max is None:
        return ceiling
    return min(agent_max, ceiling)


def effective_max_attempts_per_requirement(agent_rec: Optional[dict] = None) -> int:
    """The per-requirement corrective cap.

    ``min(agent value, app ceiling, effective overall)`` — clamped to the overall
    as well as its own ceiling so a per-requirement cap can never outlive the
    total budget it sits inside. ``0`` disables the per-requirement cap, leaving
    only the overall budget (same convention as the resume per-cause cap).
    """
    ceiling = _app_int(CEILING_PER_REQUIREMENT_KEY, DEFAULT_MAX_ATTEMPTS_PER_REQUIREMENT)
    agent_max = _agent_positive_int(agent_rec, "closing_max_attempts_per_requirement")
    cap = ceiling if agent_max is None else min(agent_max, ceiling)
    return min(cap, effective_max_attempts(agent_rec))


def assemble_instruction(instance: dict, descriptor: Optional[dict] = None) -> str:
    """Compose the completion-path injection from the floor plus every set layer.

    Order: floor → app-level notification/format → the flag taxonomy → per-agent
    notification/format → the closing turn directive. Every layer is skipped when
    blank, so an unconfigured gate still injects a valid floor. The directive
    (:data:`CLOSE_NOW_DIRECTIVE`) says what the retry must DO with the turn; every
    other layer says what the message must LOOK like. There is no per-instance
    override and no generic app guidance blob: the instruction is a contract, and
    app-wide guidance is expressed through the gate's own app-level params.
    """
    parts: List[str] = []
    floor = str((descriptor or {}).get("prompt_instruction") or "").strip()
    if floor:
        parts.append(floor)
    app_params = app_gate_values()
    for key in ("notification", "format"):
        if key == "notification" and not _configured_boolean(instance, descriptor, "require_notification"):
            continue
        value = app_params.get(key, "")
        if value:
            parts.append(value)
    # The floor is deliberately bare, so the machine-readable envelope travels
    # with the flag vocabulary instead. That layer is always present (the
    # descriptor ships a default taxonomy) and is app-level governed, so the
    # envelope cannot be lost by clearing a format field.
    _flags_clause = _flag_clause(instance, descriptor)
    if _flags_clause:
        parts.append(_flags_clause)
    params = instance.get("params") if isinstance(instance.get("params"), dict) else {}
    for key in ("notification", "format"):
        if key == "notification" and not _configured_boolean(instance, descriptor, "require_notification"):
            continue
        value = str(params.get(key) or "").strip()
        if not value and not app_params.get(key):
            # The descriptor default IS this layer's shipped guidance. The
            # closing message ships none, so an unconfigured agent answers
            # freeform; a gate that does ship one still reaches an agent that
            # configures nothing. An app-level value supersedes it.
            value = str(_param_spec(descriptor, key).get("default") or "").strip()
        if value:
            parts.append(value)
    if parts:
        # The turn directive goes LAST: it says what to DO with this turn, and the
        # rejected draft is replayed immediately before the instruction, so this is
        # the final thing the model reads before it answers. Skipped only when no
        # layer composed anything at all (nothing configured and no descriptor),
        # which keeps "no contract ⇒ no injection" intact.
        parts.append(CLOSE_NOW_DIRECTIVE)
    return "\n".join(parts).strip()


def _flag_clause(instance: dict, descriptor: Optional[dict] = None) -> str:
    """The flag-vocabulary clause, which ALSO carries the ``FLAG: <name>`` envelope.

    Split out so the pre-completion advisory (:func:`envelope_advisory`) can
    deliver exactly these words, without restating the taxonomy in two places
    that could drift apart. The word limit is read from the SAME config the
    end-of-run validator enforces, so the stated limit and the check can never
    disagree.

    The vocabulary carries a one-line MEANING per flag (:data:`FLAG_MEANINGS`): a
    bare word list left the model to infer ``partial`` from ``unverified``, and the
    observed failure was a closing message with no flag line at all. A flag the
    active taxonomy does not name is simply left unglossed, so a custom vocabulary
    degrades to the bare list rather than to a definition that is not ours. Empty
    when no taxonomy resolves.
    """
    if not _configured_boolean(instance, descriptor, "require_flag"):
        return ""
    flags = configured_flags(instance, descriptor)
    if not flags:
        return ""
    limit = configured_cta_max_words(descriptor)
    parts = ["Valid flags: " + ", ".join(flags) + "."]
    glosses = [
        f"{name} = {FLAG_MEANINGS[name]}" for name in flags if name in FLAG_MEANINGS
    ]
    if glosses:
        parts.append("Choose the one that matches: " + "; ".join(glosses) + ".")
    parts.append(
        "End your reply with 'FLAG: <name>' alone on its OWN line — never a dash "
        "and never extra words after the flag — and the status on the NEXT line: "
        f"a short 1-{limit} word status."
    )
    return " ".join(parts)


def envelope_advisory(instance: dict, descriptor: Optional[dict] = None) -> str:
    """The envelope, delivered BEFORE the run's closing turn rather than after it.

    The completion-path instruction is completion-INJECT by design, so it reaches
    the model only once its draft has already been judged and rejected. For a
    weak model that is too late to be useful: it re-derives the same prose from
    the same context and the gate spends its whole budget on one class of miss.
    The loop therefore delivers this ONCE, on the first turn where the next prose
    reply IS the closing message (the loop's closing-envelope advisory), so the
    envelope is in context while the model writes its first closing attempt.

    Deliberately the ENVELOPE ONLY, never the floor. The floor asks for the final
    response, and saying that before the run has finished its work would read as
    an instruction to stop early — exactly the failure the completion-only
    injection exists to avoid.
    """
    clause = _flag_clause(instance, descriptor)
    if not clause:
        return ""
    return (
        "When you write your FINAL message to the user it must carry this "
        "envelope. " + clause
    )


def attempt_marker(
    retries: int, per_requirement: Optional[dict] = None,
) -> Dict[str, Any]:
    """The compact durable tally stamped on the folded draft row.

    Returns a metadata FRAGMENT keyed by :data:`ATTEMPTS_KEY`, ready to merge into
    the folded row's metadata. Deliberately tiny: it records how much of the
    closing budget the run has spent, never why or how well.
    """
    return {
        ATTEMPTS_KEY: {
            "total": int(retries or 0),
            "per_requirement": {
                str(key): int(value or 0)
                for key, value in (per_requirement or {}).items()
            },
        }
    }


async def durable_attempts(
    db: Any, user_id: str, session_id: str,
) -> Optional[Dict[str, Any]]:
    """The closing tally THIS run already spent, or ``None`` — the budget's durable half.

    The per-run contract state dies with the run, so a crash-resume would restart
    the compliance budget at attempt 1 and the gate would never reach
    ``attempts_exhausted``. The transcript is the second source of truth: the
    folded draft rows carry :data:`ATTEMPTS_KEY`, written only by the closing retry
    site. Best-effort by design — a backend without the probe, or an unreadable
    transcript, reports ``None`` and the run behaves exactly as it would have.
    """
    probe = getattr(db, "closing_attempts_for_run", None)
    if probe is None:
        return None
    try:
        marker = await probe(user_id, session_id)
    except Exception:  # noqa: BLE001 — the latch must never fail a run
        logger.debug("closing message: durable tally probe failed", exc_info=True)
        return None
    return marker if isinstance(marker, dict) else None


# ── Policy resolution ────────────────────────────────────────────────────────

def resolve(agent_rec: Optional[dict], mode: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """The active closing-message policy, or None when the gate is off/out of scope."""
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
    require_flag = _configured_boolean(instance, descriptor, "require_flag")
    require_notification = _configured_boolean(instance, descriptor, "require_notification")
    output_closer_enabled = _configured_boolean(instance, descriptor, "output_closer_enabled")
    flags = configured_flags(instance, descriptor) if require_flag else []
    params = instance.get("params") if isinstance(instance.get("params"), dict) else {}
    token_definition = (descriptor.get("params") or {}).get("max_tokens") or {}
    try:
        max_tokens = int(params.get("max_tokens", token_definition.get("default", 0)) or 0)
    except (TypeError, ValueError):
        max_tokens = 0
    max_tokens = max(0, min(int(token_definition.get("max", 100000)), max_tokens))
    format_definition = (
        (descriptor.get("params") or {}).get("format_enforcement_min_tokens") or {})
    format_default = int(format_definition.get("default", 1000) or 1000)
    try:
        format_enforcement_min_tokens = int(params.get(
            "format_enforcement_min_tokens", format_default) or 0)
    except (TypeError, ValueError):
        format_enforcement_min_tokens = format_default
    format_enforcement_min_tokens = max(
        0,
        min(int(format_definition.get("max", 100000)), format_enforcement_min_tokens),
    )
    rejection_message = str(params.get("rejection_message") or "").strip()
    if not rejection_message:
        rejection_message = str(
            ((descriptor.get("params") or {}).get("rejection_message") or {}).get("default") or ""
        ).strip()
    return {
        "instance": instance,
        "flags": flags,
        "taxonomy": bool(flags),
        "require_flag": require_flag,
        "require_notification": require_notification,
        "output_closer_enabled": output_closer_enabled,
        "cta_max_words": configured_cta_max_words(descriptor),
        # Output Closer owns every closer-only influence on the terminal message:
        # its prompt/advisory, provider-capped token finalizer, and format retry.
        # Keep flag/notification metadata independent, but when this toggle is off
        # the working agent's ordinary terminal message must pass through unchanged.
        "max_tokens": max_tokens if output_closer_enabled else 0,
        "format_enforcement_min_tokens": (
            format_enforcement_min_tokens if output_closer_enabled else 0
        ),
        "rejection_message": rejection_message,
        "max_attempts": effective_max_attempts(agent_rec),
        "max_attempts_per_requirement": effective_max_attempts_per_requirement(agent_rec),
        # A single completion injection combines whichever independent controls
        # are enabled.  The bare floor is therefore absent only when all three
        # controls are off; an enabled flag or notification still gets the one
        # instruction it needs without activating Output Message enforcement.
        "instruction": (
            assemble_instruction(instance, descriptor)
            if require_flag or require_notification or output_closer_enabled
            else ""
        ),
        "advisory": (
            envelope_advisory(instance, descriptor) if require_flag else ""
        ),
    }


def token_count(content: str) -> int:
    """Estimate tokens in the complete final user-facing message."""
    return estimate_tokens([{"role": "assistant", "content": str(content or "")}])


_MARKDOWN_LIST_ITEM_RE = re.compile(
    r"^\s*(?:[-*+•]|\d+[.)])\s+\S", re.MULTILINE)

# Metadata is repaired privately through ``set_closer_status``.  Output failures
# are the only category permitted to ask the model for replacement user prose.
METADATA_REQUIREMENTS = frozenset({"flag", "notification"})
OUTPUT_REQUIREMENTS = frozenset({"message", "output_token_limit", "output_format"})


def classify_missing_requirements(missing: Sequence[str]) -> Dict[str, List[str]]:
    """Split unmet closing requirements by their permitted repair lane.

    Keep unknown names in the output lane: only the explicitly named metadata
    requirements may use the no-prose status repair path.
    """
    metadata: List[str] = []
    output: List[str] = []
    for name in missing:
        normalized = str(name or "").strip()
        if not normalized:
            continue
        (metadata if normalized in METADATA_REQUIREMENTS else output).append(normalized)
    return {"metadata": metadata, "output": output}


def metadata_repair_instruction(missing: Sequence[str]) -> str:
    """Return the private-only instruction for repairing closing metadata.

    The natural final response is already fixed.  The only valid response to
    this turn is the gate-owned ``set_closer_status`` tool call, so a missing
    flag or notification can never create replacement user-visible prose.
    """
    needed = ", ".join(str(name) for name in missing if str(name).strip()) or "metadata"
    return (
        "The final response to the user is already complete and must remain unchanged. "
        f"Repair only the missing completion metadata ({needed}) by calling "
        "set_closer_status now. Do not write any user-facing response, do not "
        "rewrite the final response, and do not call any other tool."
    )


def _has_markdown_list_item(content: str) -> bool:
    """Whether content contains a recognizable Markdown list item."""
    return bool(_MARKDOWN_LIST_ITEM_RE.search(str(content or "")))


def missing_requirements(
    content: str,
    policy: Optional[Dict[str, Any]],
    status: Optional[Dict[str, Any]] = None,
) -> List[str]:
    """The end-of-run requirements this closing message does NOT yet satisfy.

    New runs provide ``flag`` and ``notification`` through the gate-owned private
    status tool.  When ``status`` is absent, retain the historic text-envelope
    parse so existing rows and interrupted pre-migration runs remain readable.
    """
    if policy is None:
        return []
    structured_status = isinstance(status, dict)
    source = status if structured_status else parse_envelope(content)
    require_flag = bool(policy.get("require_flag", True))
    require_notification = bool(policy.get("require_notification", True))
    if not structured_status and not source.get("valid"):
        # Historic text-only records without an envelope still owe only the
        # metadata requirements that are currently enabled. New runs pass an
        # empty mapping, never ``None``.
        return [
            *( ["flag"] if require_flag else [] ),
            *( ["notification"] if require_notification else [] ),
            "message",
        ]
    failures: List[str] = []
    # A legacy envelope's flag/status lines are protocol, not user prose; the
    # structured path has no embedded protocol, so its entire content is prose.
    if not (str(content or "").strip() if structured_status else source.get("statement")):
        failures.append("message")
    limit = int(policy.get("cta_max_words") or 0)
    words = [word for word in str(source.get("cta") or "").split() if word]
    if require_notification and (not words or (limit > 0 and len(words) > limit)):
        failures.append("notification")
    taxonomy = [str(flag).strip().lower() for flag in (policy.get("flags") or [])]
    if require_flag and (not str(source.get("flag") or "").strip() or (
        taxonomy and str(source.get("flag") or "").strip().lower() not in taxonomy
    )):
        failures.append("flag")
    max_tokens = int(policy.get("max_tokens") or 0)
    if max_tokens > 0 and token_count(content) > max_tokens:
        failures.append("output_token_limit")
    format_threshold = int(policy.get("format_enforcement_min_tokens") or 0)
    if (
        format_threshold > 0
        and token_count(content) > format_threshold
        and not _has_markdown_list_item(content)
    ):
        failures.append("output_format")
    return failures


def attempts_exhausted(
    missing: Sequence[str],
    retries: int,
    per_requirement: Optional[Dict[str, int]],
    policy: Optional[Dict[str, Any]],
) -> bool:
    """Whether no further corrective turn can help.

    Two independent budgets stop the completion path from diverging: the overall
    budget (``max_attempts``) and, when enabled, the per-requirement cap. The run
    stops as soon as EITHER is reached — but the per-requirement cap only stops
    the run once EVERY still-missing requirement is individually capped, so one
    stubborn requirement cannot starve the others of their own attempts.
    """
    if policy is None:
        return True
    if int(retries or 0) >= int(policy.get("max_attempts") or 0):
        return True
    per_max = int(policy.get("max_attempts_per_requirement") or 0)
    if per_max <= 0 or not missing:
        return False
    counts = per_requirement or {}
    return all(int(counts.get(name) or 0) >= per_max for name in missing)


def requirement(
    content: str,
    policy: Optional[Dict[str, Any]],
    retries: int = 0,
    per_requirement: Optional[Dict[str, int]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    """The instruction to inject while a closing requirement is still unmet.

    Re-injection continues until every requirement is met. The budget is the
    effective ``min(agent limit, app ceiling)`` resolved into ``policy`` — see
    :func:`attempts_exhausted`. Once it is spent the caller completes the run
    with whatever the agent produced, promoted as a labelled PARTIAL, rather than
    blocking it forever. That is what stops an unsatisfiable requirement from
    diverging the completion path's ``turn_count -= 1; continue``.
    """
    if policy is None:
        return None
    missing = missing_requirements(content, policy, status)
    if not missing:
        return None
    if attempts_exhausted(missing, retries, per_requirement, policy):
        return None
    instruction = str(policy.get("instruction") or "").strip()
    if "output_token_limit" in missing:
        limit = int(policy.get("max_tokens") or 0)
        guidance = str(policy.get("rejection_message") or "").strip()
        token_instruction = (
            f"FINAL RESPONSE REJECTED: it is estimated at {token_count(content)} tokens, above "
            f"the configured maximum of {limit} tokens. {guidance}\n"
            "Reply with only the rewritten final response. Do not call tools."
        )
        return "\n\n".join(part for part in (instruction, token_instruction) if part) or None
    if "output_format" in missing:
        threshold = int(policy.get("format_enforcement_min_tokens") or 0)
        format_instruction = (
            f"FINAL RESPONSE REJECTED: it is estimated at {token_count(content)} tokens, above "
            f"the configured bullet-format threshold of {threshold} tokens, but it contains "
            "no Markdown bullet points. Rewrite the final response using concise Markdown "
            "bullet points.\nReply with only the rewritten final response. Do not call tools."
        )
        return "\n\n".join(part for part in (instruction, format_instruction) if part) or None
    return instruction or None


# ── Presentation metadata ────────────────────────────────────────────────────

def completion_gaps(contract: dict, state: dict) -> List[str]:
    """Outstanding completion requirements that forbid a ``complete`` flag."""
    gaps: List[str] = []
    try:
        from app.agent.contracts import (
            service_worker_cache_requirement, ui_journey_requirement,
        )
        if service_worker_cache_requirement(contract, state):
            gaps.append("service_worker_cache_bump")
        if ui_journey_requirement(contract, state):
            gaps.append("ui_journey")
    except Exception:  # noqa: BLE001 — a gap probe must never fail a run
        logger.debug("closing message: completion-gap probe failed", exc_info=True)
    # The work contract, not a mode contract: a run whose work policy is still
    # WORK_PENDING has NOT done what the user asked, so it may not close as
    # ``complete``. The loop stamps the live work state on the contract state at
    # the completion path (``work_policy_pending``), which is also the path the
    # agent's OWN closing message takes when an exhausted work contract is
    # handed to this gate instead of hard-stopping.
    if state.get("work_policy_pending"):
        gaps.append("work_pending")
    return gaps


def presentation(
    content: str,
    policy: Optional[Dict[str, Any]],
    gaps: Optional[Sequence[str]] = None,
    manager_findings: Optional[Sequence[str]] = None,
    partial: bool = False,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """The compact, durable presentation record stored on the final row.

    ``partial=True`` marks a message the gate gave up on: the run's budget was
    spent with requirements still unmet, so the message is promoted anyway (a run
    must never end with nothing to show) but the badge must not read as a clean
    flag. The safe flag is forced and the downgrade reason recorded as
    ``"partial"`` so the durable row explains itself.
    """
    envelope = parse_envelope(content)
    source = status if isinstance(status, dict) else envelope
    allowed = list(policy.get("flags") or []) if policy and policy.get("taxonomy") else None
    all_gaps = [*(gaps or []), *(manager_findings or [])]
    flag, reason = resolve_flag(str(source.get("flag") or ""), allowed, all_gaps)
    if partial and not reason:
        flag, reason = _safe_flag(allowed or []), "partial"
    return {
        "flag": flag,
        "cta": str(source.get("cta") or "")[:80],
        # Retained for historic consumers: false means the new private-status path
        # supplied the record, not that the status itself was invalid.
        "envelope_valid": bool(envelope["valid"]),
        "downgraded": reason,
        # True when the gate exhausted its budget with requirements still unmet:
        # the message is the run's answer but it does NOT meet the closing
        # contract, so the UI and the durable row can say so plainly.
        "partial": bool(partial),
        # Which of the three validated requirements were still unmet — the same
        # list ``requirement()`` acts on, recorded so the durable row stays the
        # single source of truth about why a run took corrective turns.
        "failed": missing_requirements(content, policy, status),
    }

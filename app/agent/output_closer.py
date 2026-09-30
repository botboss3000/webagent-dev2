"""
Output Closer — parallel close-out loop.

When an agent's FINAL response is sent to the user — or the run is interrupted
or errors out — this app function first checks whether that response is already
a suitable close-out. A lone response, lightweight conversation, or an explicit
result/summary is reused verbatim. Otherwise a bounded background LLM call reads
the conversation since the last Closer lane (the user's starting message, the
agent's working messages, and any interruption message; never tool calls, never
raw tool output) and REWORDS it into ONE concise, polished message. It renders
in the chat as its own separate 'Closer' lane bubble right after the agent's
normal response (which stays untouched above it).

The summary is persisted as a ``role='system'`` interaction row
(``source='system:closer'``) and pushed to the chat UI as an ``summary``
event. The UI renders it as a separate bubble labeled 'Closer'; it survives
refresh / session-switch (the reconcile poll picks it up from the DB).

- **Prompt** — ``app/defaults/app-prompts.json`` → ``app_level_prompts.output_closer``
  (or the ``data/config`` override), with a built-in fallback so the loop never
  breaks on a config error.
- **On/off** — solely the active ``closing_message`` gate's
  ``output_closer_enabled`` parameter for that agent and execution mode. A
  disabled or out-of-scope Closing Message gate fails closed.
  ``WEBAGENT_DISABLE_OUTPUT_CLOSER`` remains the operator-wide emergency stop.
- **Model** — the roster's STANDARD role model (the same model ordinary chat
  replies run on: the first enabled text-capable entry, or the pinned default),
  resolved from the DB provider config. Env vars are NOT consulted.

Robust and fully parallel: the loop fires an ``asyncio`` task right after the
final ``response`` event is yielded (see the call sites in ``app/agent/loop.py``),
so it never delays, blocks, or breaks the main response. The summary is
DURABLE, not best-effort: the LLM call retries transient errors and blank
completions (``_LLM_ATTEMPTS``), the DB insert retries SQLite writer-slot
contention (``_WRITE_ATTEMPTS``), and a leader-registered recovery sweep
(``start_sweep`` / ``stop_sweep``) re-fires the close-out for any final response
that never got one — covering a server crash between the ``response`` event and
the insert, an LLM outage, or a hook that never ran. Failures are stamped onto
the final assistant row (``summary_attempt_at`` / ``summary_attempts``) so the
sweep backs off instead of hammering a broken provider.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.agent.provider_completion import SubscriptionResponseClient, complete_text

from app.util.paths import app_prompts_path

logger = logging.getLogger(__name__)

_PERF_ENABLED = (os.environ.get("WEBAGENT_PERF_TRACE", "1") or "1").strip().lower() not in (
    "0", "false", "no", "off",
)


class _CloserPerfTimer:
    """Durable phase timings for the detached manager/Closer task."""

    def __init__(self, *, session_id: str, final_asst_id: Optional[str],
                 user_id: str, agent_id: Optional[str]) -> None:
        self.session_id = session_id
        self.final_asst_id = final_asst_id
        self.user_id = user_id
        self.agent_id = agent_id
        self._started = self._last = time.monotonic()

    def mark(self, name: str, **extra: Any) -> None:
        if not _PERF_ENABLED:
            return
        now = time.monotonic()
        delta_ms = int((now - self._last) * 1000)
        total_ms = int((now - self._started) * 1000)
        self._last = now
        try:
            from app.agent.diagnostics import record
            detail = {
                "phase": "output_closer", "mark": name,
                "delta_ms": delta_ms, "total_ms": total_ms,
            }
            detail.update(extra)
            record(
                "info", "perf",
                f"[perf] output_closer:{name} +{delta_ms}ms (t={total_ms}ms)",
                source="agent.output_closer", detail=detail,
                session_id=self.session_id, turn_id=self.final_asst_id,
                user_id=self.user_id, agent_id=self.agent_id,
            )
        except Exception:
            pass

# app-prompts catalog (bundled default under app/defaults/, or a data/ override).
_APP_PROMPTS_PATH = app_prompts_path()

# The closer always receives the FULL run context — no caps on message count,
# per-message length, total transcript size, or the user's request. A very
# long run passes its entire window through, so the summary (and the checklist
# judgment) never judge a partial picture.
_MAX_SUMMARY_TOKENS = 4096     # headroom: reasoning models spend hidden tokens first
# Single-call closer: ONE background LLM call writes the summary AND judges the
# checklist from the user's messages and the agent's responses (full uncapped
# window since the last closer — no tool call results). A large transcript
# needs a generous per-attempt bound — this is a background task, not a chat
# turn, so 90s is safe and cures the timeouts that plagued the old separate
# auditor call (20s was too tight for 40-80K-char transcripts + hidden reasoning).
_LLM_TIMEOUT = 90.0            # per-attempt bound — full-context single call needs headroom
_CONFIG_CACHE_TTL = 300        # 5 minutes for the resolved (model, client)

# Robustness — the summary must survive the same failures the normal messages
# do (LLM outage, blank completion, SQLite write contention, server crash).
# Mirrors the Session Namer (plugins/app_functions/session_titler/):
_LLM_ATTEMPTS = 3              # retries on BOTH exceptions and empty content
_LLM_RETRY_BACKOFF_S = 0.6     # brief gap between attempts
_WRITE_ATTEMPTS = 5            # insert can lose SQLite's writer slot at turn-end
_WRITE_BACKOFF_S = 0.25

# ── Tier-2 recovery sweep (the watchdog-analog for the closer) ──
# Re-fires the close-out for final assistant responses that never got one.
# Bounded and cooldown-aware so a persistently-down provider cannot burn
# unlimited LLM calls; min-age prevents racing a live in-flight summary
# (worst-case live window = attempts × timeout + backoff ≈ 62s).
_SWEEP_STARTUP_DELAY_S = 30     # let boot settle (and the first turn-hooks run)
_SWEEP_INTERVAL_S = 60          # sweep every minute
_SWEEP_MAX_PER_TICK = 10        # at most this many summaries re-fired per sweep
_SWEEP_MIN_AGE_S = 120          # only final rows idle at least this long
_SWEEP_RETRY_COOLDOWN_S = 300   # don't re-attempt a row this soon after a failure

# Built-in fallback so the loop still works if app-prompts.json is unreadable.
_FALLBACK_CLOSER_PROMPT = (
    "You are the agent's final voice in a chat. The message you write is the ONLY thing the user sees from this run — the working conversation below is internal scratch and is never shown directly. So your message must carry the substance itself: the actual answer, explanation, or content the user asked for, not a reference to it.\n"
    "\n"
    "Write the final message to the user.\n"
    "\n"
    "Rules:\n"
    "- Your message IS the answer. If the user asked for an explanation, write the actual explanation — the real points, reasons, and details — not a claim that you explained something.\n"
    "- Never say that you explained, showed, described, provided, demonstrated, or mentioned something ('I explained…', 'I showed you…', 'as I said above…'). Those phrases only point at content the user never sees; put the content itself in your message instead.\n"
    "- FORMAT: use concise Markdown bullets for framing, process, and optional context.\n"
    "- INFORMATION PRESERVATION (highest priority): when the user asks for substantive information, provide the complete requested answer. Never summarize away, omit, truncate, or replace requested facts, values, names, lists, comparisons, code, steps, instructions, quotations, errors, or other answer content — even when the user did not explicitly request 'detail' or a named format.\n"
    "- If the concise bullet limits would lose requested information, exceed them or use the format needed to preserve that information. Concision applies only to unrequested framing, process, and optional context.\n"
    "- No preamble, no 'Summary' label, no sign-off, and no meta-commentary about the response itself.\n"
    "- Be compact only after preserving the answer. State the outcome, not the process: what is now true, what the user can do, and any caveat that changes their decision. Omit only unrequested context.\n"
    "- Lead with the actual result or answer.\n"
    "- Speak as the agent who did the work: first person, e.g. 'I created…', 'The page now…', 'You can…'.\n"
    "- Never invent facts, tools, or results that are not in the working conversation.\n"
    "- When the user requests a format, follow it. Otherwise, choose the clearest format that preserves all requested information and omit only unrequested context.\n"
    "- The conversation may end with an interruption: the work may be incomplete because it was cut short by an error or a newer user message. If so, say so plainly in one bullet — what was completed, and what was cut short (and why, if the interruption message says so).\n"
    "- When an AUDIT RESULTS block is present below, the run was checked against a checklist. Report the audit within the same limits: what the auditor flagged, what was fixed, what was not, and why. If the verdict is PASS, reflect the checked items naturally. If items are still MISSING, say so plainly in one bullet and do not claim the work is complete. If the block says no checklist was configured, no audit ran: ignore it and summarize normally.\n"
    "\n"
    "AUDIT RESULTS: {audit_results}\n"
    "\n"
    "WORKING CONVERSATION (oldest first; 'User' is the human, 'Assistant' is the agent):\n"
    "{run_transcript}\n"
    "\n"
    "FINAL MESSAGE:"
)

# ── Checklist judgment / final closer (the upgrade) ──
# When a checklist is configured (per-agent prop ``audit_checklist`` in the
# agent's metadata, or the app-level ``output_auditor.checklist`` in
# app-prompts.json), the closer's SINGLE call both writes the close-out summary
# and judges the run against the checklist from the user's messages and the
# agent's responses since the last closer (no separate auditor call, no tool
# call results). PASS → the summary reflects
# the checklist as satisfied. FAIL → the verdict's feedback is injected back
# into the main loop as an ASSISTANT-side SELF-NOTE — a row in the agent's own
# first-person voice ("I'm not done yet — I still need to…"), so the transcript
# reads as the agent telling itself to keep working (no faux user message). The
# agent re-runs (supervised, inheriting the session's execution mode) to finish
# the missing items; the next final response re-triggers this loop, bounded by
# max_rounds. When rounds run out, the summary flags what is still missing
# instead of claiming the work is done. The judgment only fires for COMPLETED
# runs (see audit_eligible).
_AUDIT_SOURCE = "system:audit"        # source stamped on injected feedback rows
_AUDIT_MAX_ROUNDS = 2                 # default send-back rounds (prop-overridable)
_AUDIT_SEND_BACK = True               # default: a failed audit re-runs the agent

# When the audit verdict is FAIL and no send-back round is left, the run must
# NOT be left as a bare ``complete`` — that is a false green: the summary says
# the work is unfinished while the durable row claims success, so neither the
# user nor the recovery ladder can tell. Close-out instead tags WHY it stopped:
#   * a recoverable shortfall  → ``audit_incomplete`` (resumable; the bounded
#     retry ladder re-ignites it, then terminates with a stated reason);
#   * a genuine prohibition    → ``needs_manual_resume`` (a retry cannot satisfy
#     it — the active read-only mode forbids the missing action — so hold it for
#     a human instead of burning the resume budget).
_AUDIT_INCOMPLETE_CAUSE = "audit_incomplete"
_AUDIT_BLOCKED_CAUSE = "needs_manual_resume"
# The single closer call returns a JSON envelope (verdict + missing + feedback +
# summary); the summary itself can run long and JSON escaping inflates tokens,
# so give it generous headroom — a tight cap truncates mid-thought and comes
# back EMPTY (observed: 2048 failed ~5/6 runs on the old separate auditor call).
_COMBINED_MAX_TOKENS = 8192

# ── Checklist contract appended to the closer's single call ──
# When a checklist is configured, the closer's ONE LLM call both writes the
# final summary and judges the checklist (no separate auditor call). This block
# is appended to the closer prompt and demands a STRICT-JSON envelope; the
# transcript fed to that call is the user's messages and the agent's responses
# since the last closer (no tool call results) — the closer summarizes the
# outputs and checks the agent's responses for gaps against the criteria.
_CHECKLIST_AUDIT_BLOCK = (
    "\n\nCHECKLIST AUDIT (required — judge the run against the checklist below "
    "using the user's messages and the agent's responses as evidence; tool "
    "call internals are NOT shown to you):\n{checklist}\n\n"
    "Rules:\n"
    "- A checklist item counts as DONE only when the conversation shows it was "
    "actually performed (tests run and passed, file written, goal met), not "
    "merely mentioned or promised.\n"
    "- verdict = \"pass\" ONLY when every item shows evidence of completion.\n"
    "- List each item with insufficient evidence verbatim in \"missing\".\n"
    "- \"feedback\" is a concise instruction to the agent (2-4 sentences) "
    "telling it exactly what to do next to satisfy the missing items; empty "
    "when verdict is \"pass\".\n"
    "- \"summary\" is your final message to the user written per the closer "
    "instructions above — if verdict is \"fail\", say plainly in one bullet "
    "what is still missing and do not claim the work is complete.\n\n"
    "Reply with STRICT JSON only — no prose, no markdown fences:\n"
    "{{\"verdict\": \"pass\"|\"fail\", \"missing\": [\"item\", ...], "
    "\"feedback\": \"...\", \"summary\": \"...\"}}"
)

_CONFIG_CACHE: Optional[tuple] = None  # (model, base_url, api_key, provider, expiry_ts)


_LIGHTWEIGHT_REQUEST_RE = re.compile(
    r"^(?:"
    r"(?:hi|hello|hey|hiya|howdy|yo)(?:\s+(?:there|again|assistant|agent))?"
    r"|good\s+(?:morning|afternoon|evening)"
    r"|(?:thanks|thank\s+you)(?:\s+(?:so\s+much|again))?"
    r"|(?:ok|okay|got\s+it|sounds\s+good|cool|great|perfect|nice)"
    r")$",
    re.IGNORECASE,
)

_SUMMARY_HEADING_RE = re.compile(
    r"(?im)^\s{0,3}(?:#{1,6}\s*)?"
    r"(?:summary|result|outcome|completed|done|what\s+changed|changes|"
    r"implementation|final\s+answer|answer|findings|recommendation|"
    r"next\s+steps?)\s*:?[ \t]*$"
)

_SUMMARY_LEAD_RE = re.compile(
    r"^(?:(?:summary|result|outcome|what\s+changed)\s*:|"
    r"(?:done|completed|implemented|fixed|updated|created|finished|resolved|"
    r"shipped)\b|the\s+.+?\s+now\b|i(?:'ve|\s+have)?\s+"
    r"(?:completed|implemented|fixed|updated|created|finished|resolved)\b|"
    r"here(?:'s|\s+is)\b)",
    re.IGNORECASE,
)

_USER_HANDOFF_RE = re.compile(
    r"^(?:could\s+you|can\s+you|would\s+you|which\b|what\b|please\s+"
    r"(?:provide|choose|confirm|attach|send|share)|i\s+need\s+your\b|"
    r"before\s+i\s+(?:continue|proceed)\b|i(?:'m|\s+am)\s+unable\b|"
    r"i\s+(?:can't|cannot)\b)",
    re.IGNORECASE,
)

_FENCED_BLOCK_RE = re.compile(
    r"(?ms)^[ \t]*(?P<fence>`{3,}|~{3,})[^\n]*\n.*?"
    r"^[ \t]*(?P=fence)[ \t]*(?:\n|$)"
)
_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]\n]*\]\([^\n]+?\)")
_MARKDOWN_LINK_RE = re.compile(r"(?<!!)\[[^\]\n]+\]\([^\n]+?\)")
_RICH_DIRECTIVE_RE = re.compile(r"(?m)^\s*::[a-zA-Z][\w-]*\{.*?\}\s*$")
_RICH_HTML_RE = re.compile(
    r"(?is)<(?P<tag>table|svg|canvas|iframe|video|audio|picture|object)\b.*?"
    r"</(?P=tag)\s*>"
)

_VERBATIM_PROMPT_HEADER = (
    "\n\nVERBATIM CONTENT (mandatory):\n"
    "The transcript contains placeholders for render-critical or exact content. "
    "Write a concise summary around them, but emit every placeholder token exactly "
    "once at the point where its original content belongs. Never rewrite, summarize, "
    "escape, place inside a code fence, or alter a placeholder. The application will "
    "replace each token with its original bytes after your response. The protected "
    "source blocks are supplied below so you can understand and introduce them.\n"
)


def _is_lightweight_conversation(request: str) -> bool:
    """True for greetings/thanks/acknowledgements that must not be audited."""
    normalized = re.sub(r"[^\w\s']+", " ", request or "")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return bool(normalized and _LIGHTWEIGHT_REQUEST_RE.fullmatch(normalized))


def _reusable_final_response(request: str, assistant_msgs: List[str]) -> Optional[str]:
    """Return an already-suitable final answer, avoiding a redundant LLM call.

    A lone assistant response is already the run's only answer. For longer
    runs, reuse the last response when the user only made lightweight
    conversation or when that response explicitly presents itself as the
    result/summary. The deliberately narrow markers keep ordinary progress
    commentary eligible for the normal Closer rewrite.
    """
    messages = [
        str(msg).strip()
        for msg in assistant_msgs
        if msg is not None and str(msg).strip()
    ]
    if not messages:
        return None
    final = messages[-1]
    # A closing-message run already ends with the WORKING agent's own promoted
    # message, authored with full context and a validated flag. Never paraphrase
    # it — the closer only lacks that context, so any rewrite is strictly worse.
    try:
        from app.chat.closing import parse_envelope
        if parse_envelope(final)["valid"]:
            return final
    except Exception:  # noqa: BLE001 — reuse detection must never break the closer
        pass
    if len(messages) == 1 or _is_lightweight_conversation(request):
        return final
    if _SUMMARY_HEADING_RE.search(final):
        return final
    lead = re.sub(r"^[\s>*#_-]+", "", final).strip().splitlines()[0]
    if _SUMMARY_LEAD_RE.match(lead) or _USER_HANDOFF_RE.match(lead):
        return final
    return None

def _fallback_working_response(assistant_msgs: List[str]) -> Optional[str]:
    """Return the latest usable working-agent message for a failed closer."""
    for message in reversed(assistant_msgs or []):
        text = str(message or "").strip()
        if text:
            return text
    return None








def _markdown_table_ranges(text: str) -> List[Tuple[int, int]]:
    """Find GitHub-flavored pipe tables, including their complete data rows."""
    lines = text.splitlines(keepends=True)
    offsets: List[int] = []
    cursor = 0
    for line in lines:
        offsets.append(cursor)
        cursor += len(line)
    ranges: List[Tuple[int, int]] = []
    i = 0
    separator = re.compile(
        r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
    )
    while i + 1 < len(lines):
        header = lines[i].rstrip("\r\n")
        divider = lines[i + 1].rstrip("\r\n")
        if "|" not in header or not separator.match(divider):
            i += 1
            continue
        end_line = i + 2
        while end_line < len(lines) and "|" in lines[end_line]:
            if not lines[end_line].strip():
                break
            end_line += 1
        start = offsets[i]
        end = offsets[end_line] if end_line < len(offsets) else len(text)
        ranges.append((start, end))
        i = end_line
    return ranges


def _verbatim_ranges(text: str) -> List[Tuple[int, int, str]]:
    """Return non-overlapping exact-content spans and their diagnostic kind."""
    candidates: List[Tuple[int, int, str]] = []
    for kind, pattern in (
        ("fenced block", _FENCED_BLOCK_RE),
        ("generated image", _MARKDOWN_IMAGE_RE),
        ("rich directive", _RICH_DIRECTIVE_RE),
        ("rich HTML/UI", _RICH_HTML_RE),
        ("link", _MARKDOWN_LINK_RE),
    ):
        candidates.extend((m.start(), m.end(), kind) for m in pattern.finditer(text))
    candidates.extend((start, end, "data table") for start, end in _markdown_table_ranges(text))

    # Prefer the largest span when constructs overlap (an image contains a
    # Markdown link; a table may contain links). This keeps one stable token for
    # the complete renderable object instead of nesting placeholders.
    selected: List[Tuple[int, int, str]] = []
    for start, end, kind in sorted(candidates, key=lambda item: (item[0], -(item[1] - item[0]))):
        if any(start < prior_end and end > prior_start for prior_start, prior_end, _ in selected):
            continue
        selected.append((start, end, kind))
    return sorted(selected)


def _protect_verbatim_content(
    transcript: List[str], assistant_msgs: List[str],
) -> Tuple[List[str], List[str], List[Dict[str, str]]]:
    """Replace render-critical assistant spans with stable placeholder tokens."""
    blocks: List[Dict[str, str]] = []
    content_to_block: Dict[str, Dict[str, str]] = {}
    for message in assistant_msgs:
        text = str(message or "")
        for start, end, kind in _verbatim_ranges(text):
            content = text[start:end]
            if not content or content in content_to_block:
                continue
            block = {
                "token": f"[[CLOSER_VERBATIM_{len(blocks) + 1:03d}]]",
                "kind": kind,
                "content": content,
            }
            blocks.append(block)
            content_to_block[content] = block

    def _masked(text: str) -> str:
        masked = str(text)
        for block in sorted(blocks, key=lambda b: len(b["content"]), reverse=True):
            masked = masked.replace(block["content"], block["token"])
        return masked

    return ([_masked(line) for line in transcript],
            [_masked(message) for message in assistant_msgs], blocks)


def _verbatim_prompt(blocks: List[Dict[str, str]]) -> str:
    if not blocks:
        return ""
    parts = [_VERBATIM_PROMPT_HEADER]
    for block in blocks:
        parts.append(
            f"\n{block['token']} ({block['kind']}):\n"
            f"<protected-content>\n{block['content']}\n</protected-content>\n"
        )
    return "".join(parts)


def _restore_verbatim_content(summary: str, blocks: List[Dict[str, str]]) -> str:
    """Restore every protected span exactly once; append any token the LLM lost."""
    restored = str(summary or "")
    omitted: List[str] = []
    for block in blocks:
        token = block["token"]
        if token not in restored:
            omitted.append(block["content"])
            continue
        before, _, after = restored.partition(token)
        # A model occasionally repeats a token. Preserve the rich object once.
        restored = before + block["content"] + after.replace(token, "")
    if omitted:
        restored = restored.rstrip() + "\n\n" + "\n\n".join(omitted)
    return restored.strip()


# Operator escape hatch. The Closing Message gate is the sole per-agent
# activation authority for Output Closer; this env var is the only app-wide stop.
_OPERATOR_DISABLE_ENV = "WEBAGENT_DISABLE_OUTPUT_CLOSER"
_OPERATOR_DISABLE_VALUES = {"1", "true", "yes", "on"}


def _operator_disabled() -> bool:
    """True when an operator has stopped the Closer app-wide via the env var."""
    return (os.environ.get(_OPERATOR_DISABLE_ENV) or "").strip().lower() in _OPERATOR_DISABLE_VALUES


def _closing_output_enabled(agent_rec: Optional[dict], execution_mode: Optional[str]) -> bool:
    """True only when an active Closing Message policy permits Output Closer.

    A missing policy means the gate is disabled or out of scope.  That must
    fail closed: Manager/Codex configuration is not an activation authority.
    """
    try:
        from app.chat.closing import resolve as resolve_closing_message
        policy = resolve_closing_message(agent_rec, execution_mode)
        return bool(policy and policy.get("output_closer_enabled", True))
    except Exception as exc:
        logger.warning("output closer: could not resolve Closing Message policy: %s", exc)
        return False


def _codex_checkpoint_capable(agent_rec: Optional[dict]) -> bool:
    """True only for agents carrying the Codex engine configuration block."""
    if not agent_rec:
        return False
    raw = agent_rec.get("metadata")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) or {}
        except (TypeError, ValueError):
            return False
    if not isinstance(raw, dict):
        return False
    return isinstance(raw.get("codex_code"), dict)


def _prepare_codex_checkpoint_target(
    db: Any, agent_rec: Optional[dict], session_id: str, final_asst_id: str,
) -> Optional[Dict[str, Any]]:
    """Capture the durable task and its CAS revision before the Closer call."""
    if not _codex_checkpoint_capable(agent_rec):
        return None
    try:
        from plugins.engines.codex.context_store import task_state_for_interaction
        return task_state_for_interaction(db, session_id, final_asst_id)
    except Exception as exc:  # checkpointing must never suppress the visible closer
        logger.debug("output closer: checkpoint target unavailable: %s", exc)
        return None


async def _save_codex_closer_checkpoint(
    *, db: Any, agent_id: Optional[str], session_id: str,
    target: Optional[Dict[str, Any]], request: str, summary: str,
    audit_eligible: bool, audit_verdict: Optional[str],
    audit_missing: List[str], final_asst_id: str, closer_row_id: str,
) -> bool:
    """Persist the machine checkpoint without changing the visible summary."""
    if not target or not await _closing_output_enabled_live(
        db, agent_id, session_id=session_id,
    ):
        return False
    try:
        from plugins.engines.codex.context_store import (
            bind_interaction_to_task,
            bounded_tool_evidence,
            materialize_session_tasks,
            save_checkpoint,
        )
        task_id = str(target["task_id"])
        bind_interaction_to_task(
            db, session_id=session_id, interaction_id=closer_row_id,
            task_id=task_id,
        )
        # The Closer row now exists and is explicitly pinned to the run's task;
        # materialize any other rows that landed while the LLM was in flight.
        materialize_session_tasks(db, session_id)
        missing = [str(x)[:500] for x in audit_missing[:50] if str(x).strip()]
        if audit_verdict == "fail" or (audit_eligible and (audit_verdict != "pass" or missing)):
            status = "needs_work"
        elif audit_eligible and audit_verdict == "pass":
            status = "complete"
        else:
            status = "running"
        user_summary = str(summary)[:12000]
        checkpoint = {
            "version": 1,
            "objective": (str(target.get("objective") or request))[:4000],
            "request": str(request)[:4000],
            "status": status,
            "user_summary": user_summary,
            "completed": [user_summary[:3000]] if status == "complete" else [],
            "remaining": missing,
            "audit_verdict": audit_verdict or "not_run",
            "audit_missing": missing,
            "audit": {
                "verdict": audit_verdict or "not_run",
                "missing": missing,
            },
            "references": {
                "final_interaction_id": final_asst_id,
                "closer_interaction_id": closer_row_id,
                "final_session_seq": target.get("final_session_seq"),
            },
            "tool_evidence": bounded_tool_evidence(db, task_id, limit=20),
        }
        saved = save_checkpoint(
            db, task_id, checkpoint,
            expected_revision=int(target.get("revision") or 0),
        )
        if not saved:
            logger.info("output closer: stale checkpoint rejected for task %s", task_id)
        return saved
    except Exception as exc:  # visible Closer persistence remains authoritative
        logger.debug("output closer: checkpoint persist failed: %s", exc)
        return False


async def _closing_output_enabled_live(
    db: Any,
    agent_id: Optional[str],
    *,
    session_id: Optional[str] = None,
    execution_mode: Optional[str] = None,
) -> bool:
    """Re-read the sole Closing Message activation policy before side effects."""
    if not agent_id:
        return False
    try:
        mode = execution_mode
        if mode is None and session_id:
            mode = await db.get_session_execution_mode(session_id)
        agent_rec = await db.get_agent_by_id(agent_id)
        return _closing_output_enabled(agent_rec, mode)
    except Exception as exc:  # pragma: no cover - defensive DB failure
        # A failed live read must not authorize an unexpected background write.
        logger.warning("output closer: could not read Closing Message policy: %s", exc)
        return False


_CLOSER_RESPONSE_FORMAT_CONTRACT = (
    "\n\nOUTPUT FORMAT: use concise Markdown bullets for framing, process, and optional "
    "context. INFORMATION PRESERVATION (highest priority): when the user asks for "
    "substantive information, provide the complete requested answer. Never summarize away, "
    "omit, truncate, or replace requested facts, values, names, lists, comparisons, code, "
    "steps, instructions, quotations, errors, or other answer content — even when the user "
    "did not explicitly request 'detail' or a named format. If concise bullet limits would "
    "lose requested information, exceed them or use the format needed to preserve it. "
    "Concision applies only to unrequested framing, process, and optional context; no "
    "preamble, 'Summary' label, or sign-off."
)

def _enforce_closer_response_format(prompt: str) -> str:
    """Keep the user-facing closer ultra-compact even with a custom agent prompt."""
    text = prompt.strip()
    # A custom prompt is compliant only when it contains the complete
    # information-preservation rule. Legacy word/bullet limits alone can still
    # cause requested information to be dropped, so they must receive the
    # mandatory override below.
    lowered = text.lower()
    if (
        "information preservation (highest priority)" in lowered
        and "concision applies only to unrequested framing" in lowered
    ):
        return text
    return text + _CLOSER_RESPONSE_FORMAT_CONTRACT


def _load_closer_prompt(agent_rec: Optional[dict] = None) -> str:
    """Read the closer template for a run.

    Per-agent first: a non-empty ``agent_rec['metadata']['closer_prompt']``
    string wins (agents API field ``closer_prompt``, stored in metadata — the
    per-agent closer voice/contract). Falls back to the global app-prompts.json
    template (``output_closer``, legacy ``output_summarizer`` / ``output_overviewer``
    keys), then to the built-in fallback so the closer never breaks on a config
    error.
    """
    if agent_rec:
        meta = agent_rec.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta) or {}
            except Exception:
                meta = {}
        if isinstance(meta, dict):
            # Manager Loop is the canonical closer owner. Its normalizer also
            # migrates legacy metadata["closer_prompt"] for existing agents.
            try:
                from app.agent.manager_config import manager_loop_for_agent
                prompt = manager_loop_for_agent({**agent_rec, "metadata": meta})["closer"].get("prompt")
                if isinstance(prompt, str) and prompt.strip():
                    return _enforce_closer_response_format(prompt)
            except Exception:
                pass
            tpl = meta.get("closer_prompt")
            if isinstance(tpl, str) and tpl.strip():
                return _enforce_closer_response_format(tpl)
    try:
        data = json.loads(_APP_PROMPTS_PATH.read_text(encoding="utf-8"))
        entry = (data.get("app_level_prompts") or {}).get("output_closer")
        # Legacy key fallback: pre-rename customized prompt files may still
        # carry the entry under 'output_summarizer' (or the original
        # 'output_overviewer').
        if entry is None:
            entry = (data.get("app_level_prompts") or {}).get("output_summarizer")
        if entry is None:
            entry = (data.get("app_level_prompts") or {}).get("output_overviewer")
        entry = entry or {}
        tpl = entry.get("template") or entry.get("text")
        if isinstance(tpl, str) and tpl.strip():
            return _enforce_closer_response_format(tpl)
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("output closer: could not read prompt: %s", e)
    return _enforce_closer_response_format(_FALLBACK_CLOSER_PROMPT)


def _resolve_audit_config(
    agent_rec: Optional[dict], execution_mode: Optional[str] = None,
    mode_history: Optional[List[str]] = None,
    *, run_scoped: bool = False,
    executed_modes: Optional[List[str]] = None,
) -> Tuple[Optional[List[str]], int, bool]:
    """Resolve the close-out audit configuration for a run.

    Returns ``(checklist, max_rounds, send_back)``, or ``(None, 0, False)``
    when NO checklist is configured — in which case the closer keeps its
    original pure-summary behavior.

    Precedence (highest first):
      1. The selected execution mode's completion checklist (layered onto the
         agent-wide items, with the mode's fix-round settings).
      2. The per-agent prop ``metadata['audit_checklist']`` — a plain string
         (one item per line), a JSON array of strings, or a JSON object
         ``{"checklist": [...], "max_rounds": N, "send_back": bool}``. This is
         the user-changeable prop (agents API → ``audit_checklist``).
      3. The app-level default ``app_level_prompts.output_auditor.checklist``
         (plus optional ``max_rounds`` / ``send_back`` keys) in app-prompts.json.
    """
    checklist: List[str] = []
    max_rounds = _AUDIT_MAX_ROUNDS
    send_back = _AUDIT_SEND_BACK

    def _absorb(items: Any, rounds: Any, sb: Any) -> None:
        nonlocal checklist, max_rounds, send_back
        if isinstance(items, list):
            checklist = [str(x).strip() for x in items if str(x).strip()]
        elif isinstance(items, str) and items.strip():
            checklist = [ln.strip() for ln in items.splitlines() if ln.strip()]
        if rounds is not None:
            try:
                max_rounds = int(rounds)
            except (TypeError, ValueError):
                pass
        if sb is not None:
            # A JSON config gives a real bool, but the agents API accepts the
            # prop as `Any` — a user can send `"false"`/`"true"`/`"0"`/`"1"`.
            # bool("false") is True, which would silently flip the send-back ON.
            if isinstance(sb, str):
                send_back = sb.strip().lower() in ("1", "true", "yes", "on")
            else:
                send_back = bool(sb)

    # App-level default first (weakest).
    try:
        data = json.loads(_APP_PROMPTS_PATH.read_text(encoding="utf-8"))
        entry = (data.get("app_level_prompts") or {}).get("output_auditor") or {}
        _absorb(entry.get("checklist"), entry.get("max_rounds"), entry.get("send_back"))
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("output auditor: app-level checklist unreadable: %s", e)

    # Per-agent prop overrides.
    meta: Dict[str, Any] = {}
    if agent_rec:
        raw = agent_rec.get("metadata")
        if isinstance(raw, str):
            try:
                meta = json.loads(raw) or {}
            except Exception:
                meta = {}
        elif isinstance(raw, dict):
            meta = dict(raw)
    prop = meta.get("audit_checklist") if isinstance(meta, dict) else None
    # The Manager Loop may narrow which completion sources the Closer audits.
    # Only an explicitly stored canonical `closer` block participates here, so
    # legacy agents retain the original app/agent/mode precedence unchanged.
    manager_closer: Optional[Dict[str, Any]] = None
    raw_manager = meta.get("manager") if isinstance(meta, dict) else None
    if isinstance(raw_manager, dict) and isinstance(raw_manager.get("closer"), dict):
        try:
            from app.agent.manager_config import manager_loop_for_agent
            manager_closer = manager_loop_for_agent({**(agent_rec or {}), "metadata": meta})["closer"]
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("output auditor: manager closer config unreadable: %s", e)
    audit_agent_checklist = not manager_closer or manager_closer.get("audit_agent_checklist", True)
    audit_mode_contract = not manager_closer or manager_closer.get("audit_mode_contract", True)
    if not audit_agent_checklist:
        checklist = []  # also disables the app-level generic checklist
    if prop is not None and audit_agent_checklist:
        if isinstance(prop, str):
            s = prop.strip()
            if s:
                try:
                    obj = json.loads(s)
                except Exception:
                    obj = None
                if isinstance(obj, dict):
                    _absorb(obj.get("checklist"), obj.get("max_rounds"), obj.get("send_back"))
                elif isinstance(obj, list):
                    _absorb(obj, None, None)
                else:
                    _absorb(prop, None, None)
        elif isinstance(prop, list):
            _absorb(prop, None, None)
        elif isinstance(prop, dict):
            _absorb(prop.get("checklist"), prop.get("max_rounds"), prop.get("send_back"))

    # The selected mode contributes its own completion contract. It is layered
    # on top of the agent-wide checklist so administrators can keep universal
    # quality requirements while giving Research/Planner/etc. distinct goals.
    if execution_mode and audit_mode_contract:
        try:
            from app.agent.execution_modes import (
                accumulated_contract, contract_for_mode, resolve_execution_mode,
            )
            mode_contract = accumulated_contract(
                agent_rec, mode_history or [], execution_mode,
                run_scoped=run_scoped, executed_modes=executed_modes,
            )
            active_mode = resolve_execution_mode(agent_rec, execution_mode)
            # A read-only mode has a mode-local terminal deliverable. Generic
            # app/agent checklists commonly describe implementation and
            # verification; layering those onto Ask/Plan would invert the
            # contract and make a correct proposal or plan fail for not
            # executing. In read-only modes the selected mode contract is the
            # audit authority. Those earlier planning requirements still carry
            # forward normally once the active mode is write-capable.
            executed_write_mode = any(
                resolve_execution_mode(agent_rec, mode_id).get("permission_policy") == "write"
                for mode_id in (executed_modes or [])
            )
            if (active_mode.get("permission_policy") == "read_only"
                    and not executed_write_mode):
                checklist = []
            mode_items = [
                item.get("label") for item in mode_contract.get("checklist", [])
                if isinstance(item, dict) and item.get("label")
            ]
            if mode_items:
                checklist = list(dict.fromkeys(checklist + [str(x).strip() for x in mode_items if str(x).strip()]))
                max_rounds = int(mode_contract.get("max_rounds") or 0)
                send_back = bool(mode_contract.get("send_back", bool(max_rounds)))
            if manager_closer and manager_closer.get("require_plan_document"):
                requires_plan = any(
                    contract_for_mode(resolve_execution_mode(agent_rec, mode_id)).get("require_plan_document")
                    for mode_id in mode_contract.get("mode_ids", [])
                )
                if requires_plan:
                    checklist = list(dict.fromkeys(checklist + [
                        "The required persistent plan document was presented and maintained.",
                    ]))
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("output auditor: mode checklist unreadable: %s", e)

    if manager_closer:
        max_rounds = int(manager_closer.get("max_rounds", max_rounds))
        send_back = bool(manager_closer.get("send_back", send_back))
        if manager_closer.get("require_manager_clear"):
            # Manager self-notes are assistant-side rows in the clean transcript,
            # so the existing auditor can judge whether the agent subsequently
            # resolved or explicitly dispositioned each actionable objection.
            checklist = list(dict.fromkeys(checklist + [
                "All actionable Manager feedback in this run was resolved or explicitly dispositioned before close-out.",
            ]))

    if not checklist:
        return None, 0, False
    if max_rounds < 0:
        max_rounds = 0
    return checklist, max_rounds, send_back


def _audit_rounds_used(db: Any, session_id: str) -> int:
    """How many audit send-backs the CURRENT task has already had.

    Scoped to the span AFTER the most recent close-out lane (``system:closer`` /
    ``system:summary`` / ``system:overview``) row (a completed task's close-out), so the round bound
    resets for each new task instead of accumulating across the session's
    lifetime. A task with no prior summary counts from the start of the session.
    """
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM interactions "
            "WHERE session_id = ? AND source = ? "
            "AND session_seq > COALESCE(("
            "  SELECT MAX(session_seq) FROM interactions "
            "  WHERE session_id = ? "
            "  AND source IN ('system:overview', 'system:summary', 'system:closer')"
            "), 0)",
            (session_id, _AUDIT_SOURCE, session_id),
        ).fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


def _read_only_execution_violation(missing: List[str]) -> bool:
    """A prohibited execution cannot be repaired by another read-only round."""
    return any(
        "no requested implementation or other mutating action was executed" in str(item).lower()
        for item in (missing or [])
    )


async def _record_audit_blockage(
    db: Any,
    session_id: str,
    missing: List[str],
    *,
    round_no: int,
    max_rounds: int,
    blocked: bool,
) -> None:
    """Tag WHY an audit-failed run is stopping, so it never reads as success.

    Reaching this means the verdict was FAIL and no send-back round remained —
    the summary is about to tell the user the work is unfinished. The durable
    run row must say the same thing: ``complete`` is reserved for a PASS,
    otherwise the run shows a false green and neither the user nor the recovery
    ladder can see that anything is outstanding.

    A recoverable shortfall is tagged ``audit_incomplete`` so the bounded retry
    ladder re-ignites it, then terminates with a specific reason once that budget
    is spent. A genuine read-only prohibition is tagged ``needs_manual_resume``
    instead: no retry can satisfy it, so it is held for a human rather than
    spending the resume budget on a turn that must fail the same way again.

    Best-effort by design — bookkeeping must never break close-out.
    """
    cause = _AUDIT_BLOCKED_CAUSE if blocked else _AUDIT_INCOMPLETE_CAUSE
    items = [str(x).strip() for x in (missing or []) if str(x).strip()]
    head = "; ".join(items[:3])
    reason = (
        f"Audit FAIL: {len(items)} checklist item(s) still outstanding after "
        f"round {round_no}/{max_rounds}"
        + (f" — {head}" if head else "")
        + (" (…)" if len(items) > 3 else "")
    )
    if blocked:
        reason += (
            " — blocked: the active read-only mode prohibits the missing action, "
            "so a retry cannot satisfy it; resume with write access."
        )
    try:
        await db.run_state_finish(
            session_id, status="interrupted", stop_cause=cause, error=reason,
        )
    except Exception as e:  # noqa: BLE001 — close-out must never break
        logger.debug("output closer: audit blockage record failed: %s", e)
        return
    logger.info(
        "output closer: recorded %s for %s (%d item(s) outstanding)",
        cause, session_id[:8], len(items),
    )


def _resolve_original_parent(db: Any, parent_interaction_id: Optional[str]) -> Optional[str]:
    """Unwind an audit-feedback row back to the ORIGINAL task-start user message.

    A send-back re-run's response is parented to the ``system:audit`` row (so
    the task tree stays intact and the sweep's audit-child exclusion works), but
    the summarizer/auditor window and the 'user request' context must start from
    the ORIGINAL user message — not the injected assistant-side self-note. Each
    audit row records its original parent in metadata, so one lookup unwinds a
    round; returns ``parent_interaction_id`` unchanged when it is not an audit row.
    """
    if not parent_interaction_id:
        return None
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT source, metadata FROM interactions WHERE id = ?",
            (parent_interaction_id,),
        ).fetchone()
        if row and row["source"] == "system:audit":
            try:
                meta = json.loads(row["metadata"] or "{}")
                orig = meta.get("original_parent_id")
                if orig:
                    return str(orig)
            except Exception:
                pass
        return parent_interaction_id
    finally:
        conn.close()


def _parse_closer_verdict(text: str) -> Optional[Dict[str, Any]]:
    """Tolerantly parse the single-call closer's STRICT-JSON envelope.

    Accepts the exact JSON, markdown-fenced JSON, or JSON buried in prose.
    Returns ``{"verdict", "missing", "feedback", "summary"}`` or None when
    unparseable — None means 'inconclusive' and the caller falls back to no
    close-out (never sends back on garbage, which would risk a fix-loop).
    """
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1] if "\n" in t else t
        t = t.rsplit("```", 1)[0].strip()
    try:
        obj = json.loads(t)
    except Exception:
        start = t.find("{")
        end = t.rfind("}")
        if start == -1 or end <= start:
            return None
        try:
            obj = json.loads(t[start:end + 1])
        except Exception:
            return None
    if not isinstance(obj, dict):
        return None
    verdict = str(obj.get("verdict") or "").strip().lower()
    if verdict not in ("pass", "fail"):
        return None
    missing = obj.get("missing")
    if not isinstance(missing, list):
        missing = []
    missing = [str(x).strip() for x in missing if str(x).strip()]
    feedback = str(obj.get("feedback") or "").strip()
    summary = str(obj.get("summary") or "").strip()
    if not summary:
        return None
    return {"verdict": verdict, "missing": missing, "feedback": feedback, "summary": summary}


def _build_client(model: str, base_url: str, api_key: str,
                  provider: str = "") -> tuple:
    """Build the (model, provider, client_or_None) tuple from resolved config."""
    if not api_key:
        return (model, provider, None)
    try:
        from openai import AsyncOpenAI
        return (model, provider, AsyncOpenAI(
            base_url=base_url, api_key=api_key,
            timeout=_LLM_TIMEOUT, max_retries=0,
        ))
    except ImportError:  # pragma: no cover
        try:
            from app.openai_compat import AsyncOpenAI
            return (model, provider, AsyncOpenAI(
                base_url=base_url, api_key=api_key, timeout=_LLM_TIMEOUT,
            ))
        except Exception:
            return (model, provider, None)
    except Exception:  # pragma: no cover
        return (model, provider, None)


async def _resolve_fast_llm(
    user_id: str = "admin", agent_rec: Optional[dict] = None,
) -> tuple:
    """Resolve (model, provider, client) for the summarizer call.

    Uses the roster's STANDARD role model — the same model ordinary chat
    replies run on (the first enabled text-capable entry, or the pinned
    default) — resolved from the DB provider config for ``user_id`` (falls
    back to the admin's config when that user has none, which is
    ``_resolve_user_config``'s own behavior). This is authoritative: env vars
    are deliberately NOT consulted, so a stale env override (e.g. ``LLM_MODEL``)
    can never steer the summarizer onto a different (possibly broken) model.

    Returns (model_str, provider_str, AsyncOpenAI | None). If the standard
    role can't be resolved (no roster, no key) the client is None and the
    caller silently skips.
    """
    global _CONFIG_CACHE

    if agent_rec:
        try:
            from app.admin.settings import resolve_agent_role_config
            selected = await resolve_agent_role_config(user_id, agent_rec, "standard")
            model = str(selected.get("model") or "")
            provider = str(selected.get("provider") or "")
            if provider == "openai_subscription" and model:
                return (model, provider, SubscriptionResponseClient(
                    scope=str(selected.get("credential_scope") or "platform"),
                    entry_id=str(selected.get("entry_id") or ""),
                    user_id=str(selected.get("_credential_user_id") or user_id),
                    agent_id=str(selected.get("_credential_agent_id") or ""),
                ))
            if model and selected.get("base_url") and selected.get("api_key"):
                return _build_client(model, str(selected["base_url"]), str(selected["api_key"]), provider)
        except Exception as exc:
            logger.debug("output closer: agent standard-role lookup failed: %s", exc)
        return ("", "", None)

    now = time.time()
    if _CONFIG_CACHE and _CONFIG_CACHE[0] == user_id and _CONFIG_CACHE[5] > now:
        _, cached_model, cached_url, cached_key, cached_provider, _ = _CONFIG_CACHE
        return _build_client(cached_model, cached_url, cached_key, cached_provider)

    try:
        from app.admin.settings import _resolve_user_config as _resolve_llm_config
        from app.admin.settings import _assign_slots
        cfg = await _resolve_llm_config(user_id)
        union = cfg.get("multi_providers") or []
        slots = _assign_slots(union)
        std = (slots.get("roles") or {}).get("standard")
        if isinstance(std, dict):
            m = str(std.get("model") or "")
            u = str(std.get("base_url") or "")
            k = str(std.get("api_key") or "")
            provider = str(std.get("provider") or "")
            if provider == "openai_subscription" and m:
                # OAuth-backed ChatGPT entries intentionally have no raw API key.
                # Do not cache their access token: transport_auth refreshes it
                # against the configured credential tenant for every call.
                return (
                    m, provider,
                    SubscriptionResponseClient(
                        scope=str(std.get("credential_scope") or "platform"),
                        entry_id=str(std.get("entry_id") or ""),
                        user_id=str(std.get("_credential_user_id") or user_id),
                        agent_id=str(std.get("_credential_agent_id") or ""),
                    ),
                )
            if m and u and k:
                _CONFIG_CACHE = (user_id, m, u, k, provider,
                                 now + _CONFIG_CACHE_TTL)
                return _build_client(m, u, k, provider)
    except Exception as exc:
        logger.debug("output closer: standard-role lookup failed: %s", exc)

    _CONFIG_CACHE = (user_id, "", "", "", "", 0)  # negative cache — don't retry immediately
    return ("", "", None)


async def _closer_display_name(user_id: str, agent_rec: Any, model_str: str) -> str:
    """The user-facing name for the model the closer ran on — presentation only.

    The chat bubble labels a closer exactly as it labels the agent's own replies:
    by the caller's own display name for that model, which the roster resolves down
    to the catalog model name and only then to the raw id. The raw id is therefore
    never what the user reads whenever a name exists.

    Returns ``""`` when no name is available, and the caller then records only the
    raw model id exactly as before. Mirrors `_resolve_fast_llm`'s Standard-role
    lookup so the label and the model that actually ran can never disagree.
    """
    if not model_str:
        return ""
    try:
        from app.admin.settings import resolve_agent_role_config
        selected = await resolve_agent_role_config(user_id, agent_rec, "standard")
        name = str((selected or {}).get("display_name") or "").strip()
        # The roster stores the real model id in display_name when the row carries
        # no alias of its own, so a value equal to the id is not a display name.
        return name if name and name != model_str else ""
    except Exception as exc:  # noqa: BLE001
        logger.debug("output closer: display-name lookup failed: %s", exc)
        return ""


def _collect_span_messages(
    db: Any, session_id: str,
    parent_interaction_id: Optional[str],
    final_asst_id: Optional[str],
    include_tools: bool = False,
) -> Tuple[List[str], str, List[str]]:
    """Return (transcript lines, starting user request, assistant texts).

    The window is EVERYTHING since the last close-out lane (``system:closer`` /
    ``system:summary`` / ``system:overview``): it starts after the previous
    close-out row whose parent PREDATES the final row
    (falling back to this run's own start when no such summary exists) and
    ends at the run's final assistant row — extended forward to include a
    trailing user message only when it directly follows the final row (a new
    user message that interrupted the run; the API inserts it BEFORE
    cancelling the old run, so it is already in the DB at fire time). User
    and assistant messages are interleaved oldest-first so the summarizer
    sees the full arc from the starting request through the interruption —
    including the partial answer of an interrupted/errored run (kept in the
    DB with status 'interrupted'/'error' and selected because it is not
    'deleted').

    ``include_tools=True`` additionally folds tool results into the transcript
    as ``Assistant tool [name]: …`` lines (never added to the returned
    assistant-text list). The CLOSER (summary AND checklist judgment) always
    uses the default clean transcript — the user's messages and the agent's
    responses since the last close-out lane. Tool calls/output are never shown
    to the closer: its job is to summarize the outputs and check the agent's
    responses for gaps against the criteria, not to re-verify tool internals.

    Newest rows are loaded first, then reversed so the FULL window is passed
    through in chronological order — no truncation of any kind. The closer
    (and auditor) always see the complete run context, including the
    interruption message and the partial answer of an interrupted/errored run
    (kept in the DB with status 'interrupted'/'error' and selected because it
    is not 'deleted').
    """
    if not session_id or not final_asst_id:
        return [], "", []
    conn = db._get_conn()
    try:
        # End boundary: the final assistant row's session_seq. (The start
        # boundary below must be computed against THIS raw value — not the
        # interruption-extended one — so a newer summary can never hijack an
        # older row's window.) Background re-runs (automation, audit send-back)
        # skip the chat-API reconcile pass and leave their rows seq-less, so
        # fall back to a created_at window instead of bailing — a seq-less run
        # must still be summarizable and auditable.
        end_row = conn.execute(
            "SELECT session_seq, created_at FROM interactions WHERE id = ? LIMIT 1",
            (final_asst_id,),
        ).fetchone()
        if not end_row:
            return [], "", []
        use_ts = end_row["session_seq"] is None
        final_seq = int(end_row["session_seq"]) if not use_ts else None
        final_ts = end_row["created_at"] or ""
        end_seq = final_seq

        # Start boundary: after the last summary whose parent PREDATES the
        # final row (the previous completed run). Only older summaries count —
        # a re-fired summary for an old row must not inherit a newer summary's
        # boundary, which would empty its window. Falls back to this run's own
        # start message when no prior summary exists (start_seq = parent_seq-1
        # so the `>` window query still INCLUDES the starting message itself).
        start_seq = 0
        start_ts = ""
        if parent_interaction_id:
            _p = conn.execute(
                "SELECT session_seq, created_at FROM interactions WHERE id = ? LIMIT 1",
                (parent_interaction_id,),
            ).fetchone()
            if _p:
                if _p["session_seq"] is not None:
                    start_seq = int(_p["session_seq"]) - 1
                elif _p["created_at"]:
                    start_ts = _p["created_at"]
        if use_ts:
            _ps = conn.execute(
                "SELECT s.parent_id FROM interactions s "
                "JOIN interactions p ON p.id = s.parent_id "
                "WHERE s.session_id = ? AND s.source IN ('system:overview', 'system:summary', 'system:closer') "
                "AND p.created_at < ? "
                "ORDER BY s.created_at DESC LIMIT 1",
                (session_id, final_ts),
            ).fetchone()
            if _ps and _ps["parent_id"]:
                _q = conn.execute(
                    "SELECT created_at FROM interactions WHERE id = ? LIMIT 1",
                    (_ps["parent_id"],),
                ).fetchone()
                if _q and _q["created_at"]:
                    start_ts = _q["created_at"]
        else:
            _ps = conn.execute(
                "SELECT s.parent_id FROM interactions s "
                "JOIN interactions p ON p.id = s.parent_id "
                "WHERE s.session_id = ? AND s.source IN ('system:overview', 'system:summary', 'system:closer') "
                "AND p.session_seq < ? "
                "ORDER BY s.session_seq DESC LIMIT 1",
                (session_id, final_seq),
            ).fetchone()
            if _ps and _ps["parent_id"]:
                _q = conn.execute(
                    "SELECT session_seq FROM interactions WHERE id = ? LIMIT 1",
                    (_ps["parent_id"],),
                ).fetchone()
                if _q and _q["session_seq"] is not None:
                    start_seq = int(_q["session_seq"])

        # Interruption extension (seq mode only): a trailing user message is
        # pulled into the window ONLY when it directly follows the final row
        # (a new user message that interrupted the run; the API inserts it
        # before cancelling the old run). If any assistant row sits between
        # the final row and that user message, the user message belongs to a
        # LATER run and must not be pulled into this summary.
        if not use_ts:
            _lu = conn.execute(
                "SELECT MAX(session_seq) AS s FROM interactions "
                "WHERE session_id = ? AND role = 'user' "
                "AND (status IS NULL OR status NOT IN ('deleted', 'queued'))",
                (session_id,),
            ).fetchone()
            if _lu and _lu["s"] is not None and int(_lu["s"]) > final_seq:
                _between = conn.execute(
                    "SELECT COUNT(*) AS c FROM interactions "
                    "WHERE session_id = ? AND role = 'assistant' "
                    "AND session_seq > ? AND session_seq <= ? "
                    "AND (status IS NULL OR status != 'deleted')",
                    (session_id, final_seq, int(_lu["s"])),
                ).fetchone()
                if _between is None or int(_between["c"]) == 0:
                    end_seq = int(_lu["s"])

        # Request = this run's starting user message (parent_interaction_id).
        request = ""
        if parent_interaction_id:
            _r = conn.execute(
                "SELECT content FROM interactions WHERE id = ? AND role = 'user' LIMIT 1",
                (parent_interaction_id,),
            ).fetchone()
            if _r:
                request = _r["content"] or ""

        # Newest rows first, then reversed in the caller — the full window is
        # kept; nothing is dropped or truncated.
        _roles = ("user", "assistant") if not include_tools else ("user", "assistant", "tool")
        _status_sql = (
            "AND ("
            "  (role = 'user' AND (status IS NULL OR status NOT IN ('deleted', 'queued')))"
            "  OR"
            "  (role IN ('assistant', 'tool') AND (status IS NULL OR status != 'deleted'))"
            ") "
            "AND TRIM(COALESCE(content, '')) != '' "
        )
        if use_ts:
            rows = conn.execute(
                "SELECT role, content, tool_name FROM interactions "
                "WHERE session_id = ? AND role IN "
                f"({','.join('?' for _ in _roles)}) "
                "AND (? = '' OR created_at > ?) AND created_at <= ? "
                + _status_sql +
                "ORDER BY created_at DESC",
                (session_id, *_roles, start_ts, start_ts, final_ts),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT role, content, tool_name FROM interactions "
                "WHERE session_id = ? AND role IN "
                f"({','.join('?' for _ in _roles)}) "
                "AND session_seq > ? AND session_seq <= ? "
                + _status_sql +
                "ORDER BY session_seq DESC",
                (session_id, *_roles, start_seq, end_seq),
            ).fetchall()
    finally:
        conn.close()

    lines: List[str] = []
    asst_texts: List[str] = []
    for r in reversed(rows):
        text = (r["content"] or "").strip()
        if not text:
            continue
        role = r["role"]
        if role == "user":
            lines.append(f"User: {text}")
        elif role == "assistant":
            lines.append(f"Assistant: {text}")
            asst_texts.append(text)
        else:  # tool — only present when include_tools=True (raw-results mode)
            tname = (r["tool_name"] or "tool").strip()
            lines.append(f"Assistant tool [{tname}] output: {text}")
    return lines, request, asst_texts


def _next_session_seq(db: Any, session_id: str) -> int:
    """Next available session_seq for the session (so the reconcile poll sees
    the persisted row immediately even though the run buffer has ended)."""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT COALESCE(MAX(session_seq), 0) + 1 FROM interactions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else 1
    finally:
        conn.close()


def _final_row_meta(db: Any, final_asst_id: str) -> Tuple[Optional[str], Optional[int], Optional[int]]:
    """(turn_id, turn_seq, session_seq) of the final assistant row, so the
    summary row lands in the same turn for ordering on reload."""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT turn_id, turn_seq, session_seq FROM interactions WHERE id = ? LIMIT 1",
            (final_asst_id,),
        ).fetchone()
        if not row:
            return None, None, None
        return (row["turn_id"], row["turn_seq"], row["session_seq"])
    finally:
        conn.close()


def _parse_ts(s: Optional[str]) -> Optional[datetime]:
    """Tolerant ISO/naive timestamp parse (same as the watchdog's)."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except Exception:
        try:
            dt = datetime.fromisoformat(str(s).replace(" ", "T"))
        except Exception:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _stamp_closer_attempt(db: Any, final_asst_id: str) -> None:
    """Record a failed summarization attempt on the final assistant row
    (metadata: ``summary_attempt_at`` / ``summary_attempts``). The recovery
    sweep reads these for its failure cooldown so a persistently-broken
    provider is not hammered. Best-effort — never raises."""
    try:
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM interactions WHERE id = ?",
                (final_asst_id,),
            ).fetchone()
            if not row:
                return
            meta = {}
            try:
                meta = json.loads(row["metadata"]) if row["metadata"] else {}
            except Exception:
                meta = {}
            if not isinstance(meta, dict):
                meta = {}
            meta["summary_attempt_at"] = datetime.now(timezone.utc).isoformat()
            meta["summary_attempts"] = int(meta.get("summary_attempts") or 0) + 1
            last_err: Optional[Exception] = None
            for attempt in range(1, _WRITE_ATTEMPTS + 1):
                try:
                    conn.execute(
                        "UPDATE interactions SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), final_asst_id),
                    )
                    conn.commit()
                    return
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    if "lock" not in str(e).lower():
                        raise
                    if attempt < _WRITE_ATTEMPTS:
                        await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
            logger.debug("output closer: attempt stamp failed: %s", last_err)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a failed stamp must never break a turn
        pass


async def _stamp_closer_skipped_disabled(db: Any, final_asst_id: str) -> None:
    """Durably make a no-Closer turn ineligible for later recovery.

    Without this marker, turning the feature back on would cause the recovery
    sweep to backfill every response intentionally produced while it was off.
    The metadata merge is serialized so it does not discard a concurrent run
    or diagnostics update.  Best-effort, like the failure-attempt stamp.
    """
    try:
        getter = getattr(db, "_get_conn", None)
        if getter is None:
            local = getattr(db, "_local", None)
            getter = getattr(local, "_get_conn", None) if local is not None else None
        if getter is None:
            return
        last_err: Optional[Exception] = None
        for attempt in range(1, _WRITE_ATTEMPTS + 1):
            conn = getter()
            try:
                conn.execute("BEGIN IMMEDIATE")
                row = conn.execute(
                    "SELECT metadata FROM interactions WHERE id = ?",
                    (final_asst_id,),
                ).fetchone()
                if not row:
                    conn.rollback()
                    return
                raw = row["metadata"] if hasattr(row, "keys") else row[0]
                try:
                    meta = json.loads(raw) if raw else {}
                except (TypeError, ValueError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta["closer_skipped_disabled"] = True
                meta["closer_skipped_disabled_at"] = datetime.now(
                    timezone.utc).isoformat()
                conn.execute(
                    "UPDATE interactions SET metadata = ? WHERE id = ?",
                    (json.dumps(meta), final_asst_id),
                )
                conn.commit()
                return
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                try:
                    conn.rollback()
                except Exception:
                    pass
                if "lock" not in str(exc).lower():
                    raise
            finally:
                conn.close()
            if attempt < _WRITE_ATTEMPTS:
                await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
        logger.debug("output closer: disabled-skip stamp failed: %s", last_err)
    except Exception:  # noqa: BLE001 — skip persistence must not break a turn
        pass


def _format_closer_template(
    template: str, transcript: List[str], request: str,
    assistant_msgs: List[str], audit_results: str,
    agent_rec: Optional[dict] = None,
) -> str:
    """Format a closer template against the run's content.

    Supplies the full placeholder set (``user_request``, ``assistant_messages``,
    ``run_transcript``, ``audit_results``) so old and new templates both work.
    A per-agent template referencing any OTHER placeholder would raise
    ``KeyError`` (``str.format`` is strict) and kill the closer — so we catch
    it and fall back to the global template instead. A bad per-agent prompt
    must degrade gracefully, never crash the close-out.
    """
    try:
        rendered = template.format(
            user_request=request or "(not available)",
            assistant_messages="\n\n---\n\n".join(
                f"[{i}] {m}" for i, m in enumerate(assistant_msgs, 1)),
            run_transcript="\n".join(transcript) if transcript else "(no messages)",
            audit_results=audit_results or "No checklist was configured for this run (no audit ran).",
        )
    except (KeyError, IndexError, ValueError) as e:
        logger.warning(
            "output closer: template has unsupported placeholder(s) (%s) — "
            "falling back to the global template", e)
        rendered = _load_closer_prompt(None).format(
            user_request=request or "(not available)",
            assistant_messages="\n\n---\n\n".join(
                f"[{i}] {m}" for i, m in enumerate(assistant_msgs, 1)),
            run_transcript="\n".join(transcript) if transcript else "(no messages)",
            audit_results=audit_results or "No checklist was configured for this run (no audit ran).",
        )

    # The closer summarizes a completed run; it must not independently inject
    # mode-scoped gate guidance. The working-loop prompt is the sole prompt
    # surface for active-gate instructions.
    return rendered

async def _attempt_closer_call(
    transcript: List[str], request: str, assistant_msgs: List[str],
    model_str: str, client: Any, audit_results: str = "",
    agent_rec: Optional[dict] = None,
    verbatim_blocks: Optional[List[Dict[str, str]]] = None,
) -> Tuple[Optional[str], Optional[str], Any]:
    """One bounded summarizer LLM call against a PRE-RESOLVED client.

    Returns ``(summary_text, model_str, resp)`` on success, or
    ``(None, model_str, resp_or_None)`` when the completion came back blank or
    was cut off by the token budget (finish_reason 'length'). The caller
    retries both exceptions and blank/truncated completions; the client is
    resolved once up front so a MISSING model is a config gap (not a retryable
    blank).

    The prompt template may use either the transcript-based placeholder set
    (``run_transcript``) or the legacy pair (``user_request`` /
    ``assistant_messages``); all are supplied — ``str.format`` ignores extras,
    so old and new templates both work. ``audit_results`` is the checklist
    status block (empty when no audit ran) — templates that don't reference it
    are unaffected.
    """
    prompt = _format_closer_template(
        _load_closer_prompt(agent_rec), transcript, request, assistant_msgs,
        audit_results or "No checklist was configured for this run (no audit ran).",
        agent_rec,
    ) + _verbatim_prompt(verbatim_blocks or [])
    if verbatim_blocks:
        # The configured template commonly ends in FINAL MESSAGE:. Repeat the
        # response cue after the protected-source appendix so generation starts
        # after, rather than before, the exact blocks it must place.
        prompt += "\nFINAL MESSAGE:"
    # API-key providers use safe_chat_completion's compatibility fallbacks;
    # ChatGPT subscriptions use the main loop's OAuth-backed Responses transport.
    resp = await complete_text(
        client, model=model_str, messages=[{"role": "user", "content": prompt}],
        max_tokens=_MAX_SUMMARY_TOKENS, temperature=0.3, timeout=_LLM_TIMEOUT,
    )
    ch = resp.choices[0] if resp and resp.choices else None
    finish = getattr(ch, "finish_reason", None) if ch else None
    summary = (getattr(ch.message, "content", None) or "").strip() if ch else ""
    # Provider quirk (deepseek-v4-flash proxy): the whole completion may land
    # in ``reasoning_content`` with ``content`` empty — read it as a fallback.
    if not summary and ch:
        summary = (getattr(ch.message, "reasoning_content", None) or "").strip()
    # A 'length' finish means the model was cut off mid-sentence — return None
    # so the caller's retry loop treats truncation as a failure instead of
    # persisting a half-written summary.
    if finish == "length":
        return None, model_str, resp
    if summary:
        summary = _restore_verbatim_content(summary, verbatim_blocks or [])
    return (summary or None), model_str, resp


async def _attempt_combined_call(
    transcript: List[str], request: str, assistant_msgs: List[str],
    checklist: List[str], model_str: str, client: Any,
    agent_rec: Optional[dict] = None,
    verbatim_blocks: Optional[List[Dict[str, str]]] = None,
) -> Optional[Dict[str, Any]]:
    """ONE closer call that writes the summary AND judges the checklist.

    The checklist judgment is folded into the closer's single LLM call — there
    is NO separate auditor call. The prompt is the closer template (final
    message formatting) plus the checklist + STRICT-JSON contract, and the
    transcript is the user's messages and the agent's responses since the last
    closer (clean transcript, NO tool call results) — the closer summarizes the
    outputs and checks the agent's responses for gaps against the criteria.

    Returns the parsed ``{"verdict", "missing", "feedback", "summary"}``
    dict, or None when the completion was blank, truncated, or unparseable.
    None means 'inconclusive' — the caller falls back to no close-out and the
    recovery sweep re-fires it later (a garbage verdict must not start a
    fix-loop).
    """
    prompt = _format_closer_template(
        _load_closer_prompt(agent_rec), transcript, request, assistant_msgs,
        "Checklist audit requested (see the CHECKLIST AUDIT block below) — "
        "judge it yourself from the transcript.",
        agent_rec,
    ) + _verbatim_prompt(verbatim_blocks or []) + _CHECKLIST_AUDIT_BLOCK.format(
        checklist="\n".join(f"- {c}" for c in checklist))
    resp = await complete_text(
        client, model=model_str, messages=[{"role": "user", "content": prompt}],
        max_tokens=_COMBINED_MAX_TOKENS, temperature=0.2, timeout=_LLM_TIMEOUT,
    )
    ch = resp.choices[0] if resp and resp.choices else None
    text = (getattr(ch.message, "content", None) or "").strip() if ch else ""
    # Provider quirk (observed on the deepseek-v4-flash proxy): the model
    # sometimes writes the ENTIRE completion into ``reasoning_content`` and
    # leaves ``content`` empty (finish_reason 'stop', all tokens marked as
    # reasoning). Fall back to that field — the verdict/summary is real, it's
    # just in the wrong slot. This was the root cause of the old auditor's
    # "no verdict after 3 attempts": the answer WAS there, unread.
    if not text and ch:
        text = (getattr(ch.message, "reasoning_content", None) or "").strip()
    # A 'length' finish usually means the JSON was cut off mid-envelope (which
    # fails to parse), but the envelope can occasionally be complete at the
    # exact cap — parse first and only reject when parsing actually fails, so
    # a good verdict is never discarded on a finish_reason technicality.
    parsed = _parse_closer_verdict(text)
    if parsed:
        parsed["summary"] = _restore_verbatim_content(
            parsed["summary"], verbatim_blocks or [])
    return parsed


async def _send_audit_back(
    *,
    db: Any,
    user_id: str,
    session_id: str,
    agent_id: Optional[str],
    channel: Optional[str],
    final_asst_id: str,
    feedback: str,
    missing: List[str],
    round_no: int,
    max_rounds: int,
    original_parent_id: Optional[str] = None,
    execution_mode: Optional[str] = None,
) -> None:
    """Send the auditor's verdict back into the MAIN LOOP.

    The feedback is injected as an ASSISTANT-side SELF-NOTE — a row in the
    agent's own first-person voice ("I'm not done yet — I still need to…"),
    persisted with role='assistant', source='system:audit', parented to the
    audited final response. There is deliberately NO faux user message: the
    transcript reads as the agent telling itself to keep working. The row is
    written BEFORE the re-run's history is built so the re-run's context ends
    with that unfinished self-note, and the loop is kicked with
    ``user_message=None`` (the same resume-without-nudge path crash-resume
    uses) — the agent continues working from where it left off.

    The re-run goes through the same supervised-turn machinery automations use
    (fresh turn, durable run-state). When it yields its final response,
    ``_schedule_output_closer`` fires again from the loop — round N+1 — and
    the durable ``system:audit`` row count caps the recursion (see
    ``_audit_rounds_used``).

    The re-run inherits the session's execution mode (the loop's live mode
    re-read), NOT a forced auto: destructive tools still respect the user's
    confirmation posture — in ask/plan they are blocked and the agent reports
    back needing approval, so the auditor can never silently escalate
    privileges.

    Best-effort: any failure here just means the run closes out with the
    checklist gaps flagged in the summary instead of a re-run — never raises.
    """
    try:
        from app.agent.run_fence import interaction_turn_id, side_effects_allowed
        expected_turn_id = interaction_turn_id(db, final_asst_id)
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return
        # The closer runs in the background, so the user can disable Closing
        # Message while the audit LLM is still working. Re-read immediately
        # before the durable self-note and supervised re-run are created.
        if not await _closing_output_enabled_live(
            db, agent_id, session_id=session_id, execution_mode=execution_mode,
        ):
            logger.info("output closer: disabled before audit send-back")
            await _stamp_closer_skipped_disabled(db, final_asst_id)
            return

        # First-person self-note in the agent's own voice — the re-run's context
        # ends with this unfinished thought, so the agent continues working.
        # The round number stays in metadata; the visible transcript stays natural.
        _items = "; ".join(missing) or "unspecified items"
        _feedback = (feedback or "").strip()
        if _feedback:
            content = (
                "I'm not done yet — my last response left "
                f"{len(missing)} checklist item(s) incomplete: {_items}. "
                f"{_feedback} I'll continue working on these now and complete them."
            )
        else:
            content = (
                "I'm not done yet — my last response left "
                f"{len(missing)} checklist item(s) incomplete: {_items}. "
                "I'll continue working on these now and complete them."
            )

        # Durable assistant-side self-note row — rides the DB-tail the chat UI
        # polls, and the round counter counts these rows. Inserted BEFORE the
        # history build so the re-run's replay context ends with this note.
        seq = await db.next_session_seq(session_id, 1)
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return
        if not await _closing_output_enabled_live(
            db, agent_id, session_id=session_id, execution_mode=execution_mode,
        ):
            logger.info("output closer: disabled before audit feedback write")
            await _stamp_closer_skipped_disabled(db, final_asst_id)
            return
        turn_uid = await db.insert_interaction(
            user_id, session_id, role="assistant", content=content,
            parent_id=final_asst_id, channel=channel,
            metadata=json.dumps({
                "kind": "audit_feedback",
                "asst_id": final_asst_id,
                "round": round_no,
                "missing": missing,
                # Original task-start user message id, so a later round's
                # summarizer/auditor unwinds to the real request (not this row).
                "original_parent_id": original_parent_id,
            }),
            sender_id=agent_id, receiver_id=user_id,
            source=_AUDIT_SOURCE, session_seq=seq,
        )

        # Live UI push — same 'response' event shape the loop emits for an
        # assistant message, so the self-note renders as an agent bubble.
        try:
            from app.chat.execution import _emit_to_visualizers
            await _emit_to_visualizers(session_id, {
                "type": "response", "level": "agent",
                "content": content, "asst_id": turn_uid,
                "source": _AUDIT_SOURCE, "session_seq": seq,
            }, user_id=user_id, db_override=db)
        except Exception as _emit_err:
            logger.debug("output auditor: emit failed: %s", _emit_err)

        # Rebuild the run the way the background executors do (prompts + full
        # history) AFTER persisting the self-note, so the history reflects
        # everything up to and including the audited final response AND the
        # self-note. The loop is kicked with user_message=None (resume-without-
        # nudge) so it continues from the note instead of appending a user turn.
        from app.agent.prompts import build_system_prompt
        from app.agent.session_history import build_openai_history_from_session
        from app.agent.runner import run_supervised_turn, RunOutcome
        from app.agent.loop import run_agent_loop_buffered

        agent = await db.get_agent_by_id(agent_id) if agent_id else None
        system_prompt = ""
        try:
            resolved = (await db.resolve_prompts(agent_id, user_id=user_id)) if agent_id else []
            context_docs = [
                {"id": s["slot_name"], "context_type": s["slot_name"],
                 "title": s["slot_name"], "content": s["content"], "tags": []}
                for s in resolved
                if (s.get("content") or "").strip() and s.get("slot_name") not in ("automation",)
            ]
            system_prompt = await build_system_prompt(
                context_docs, brain_context=None, user_id=user_id,
                agent_id=agent_id, session_id=session_id)
        except Exception as _sp_err:
            logger.debug("output auditor: system prompt rebuild failed: %s", _sp_err)
        try:
            history = await build_openai_history_from_session(
                db, user_id, session_id, agent_id=agent_id)
        except Exception:
            history = []

        raw_allowed = (agent or {}).get("allowed_tools", [])
        if isinstance(raw_allowed, str):
            try:
                raw_allowed = json.loads(raw_allowed)
            except Exception:
                raw_allowed = []

        async def _broadcast(ev: Dict[str, Any]) -> None:
            try:
                from app.chat.execution import _emit_to_visualizers
                await _emit_to_visualizers(session_id, ev, user_id=user_id, db_override=db)
            except Exception:
                pass

        async def _build_turn(replaced: bool) -> RunOutcome:
            reply = await run_agent_loop_buffered(
                user_id=user_id, session_id=session_id, user_message=None,
                system_prompt=system_prompt, agent_id=agent_id, history=history,
                parent_interaction_id=turn_uid, channel=channel, db=db,
                agent_template_id=(agent or {}).get("template_id"),
                allowed_tools=raw_allowed or None,
                max_turns=(agent or {}).get("max_turn_count", 0),
                event_callback=_broadcast, timeout_seconds=600,
                # Inherit the mode the ORIGINAL run used (threaded from the
                # loop) — the fix-loop must never silently escalate, and must
                # not be demoted to ask when the user dispatched the run auto.
                execution_mode=execution_mode or "ask",
            )
            # run_agent_loop_buffered swallows the loop's error/interrupted
            # events and returns only the collected final string — an empty
            # reply means the re-run errored or produced nothing, and a reply
            # starting with "I encountered an error:" / "I was interrupted:"
            # is the loop's fabricated stand-in for a loop failure. All four
            # must be reported as HONEST failures, never a fake 'complete'
            # that hides the crash. Empty/crash get RESUMABLE stop-causes so
            # the watchdog can auto-revive a fix-loop that died mid-run; an
            # interrupted re-run is a deliberate stop (user_stop, not resumable).
            if not (reply or "").strip():
                return RunOutcome(
                    status="error", stop_cause="empty_response",
                    error="audit re-run produced no final reply",
                )
            # Legacy/no-terminal-event fallback: older builds of
            # run_agent_loop_buffered fabricated a success-looking placeholder
            # when the loop ended with NO response/error/interrupted event at
            # all. That string is the wrapper's admission that nothing was
            # produced — map it to the same resumable failure as an empty
            # reply, never to 'complete' (this exact case hid an LLM stream
            # failure behind a fake-complete audit re-run).
            if reply.strip() == "I completed the analysis but produced no output.":
                return RunOutcome(
                    status="error", stop_cause="empty_response",
                    error="audit re-run produced no final reply",
                )
            if reply.startswith("I encountered an error:"):
                return RunOutcome(
                    status="error", stop_cause="crash", error=reply,
                )
            if reply.startswith("I was interrupted:"):
                return RunOutcome(
                    status="error", stop_cause="user_stop",
                    error="audit re-run was interrupted",
                )
            return RunOutcome(status="complete", stop_cause="complete", reply=reply)

        outcome = await run_supervised_turn(
            session_id=session_id, user_id=user_id, agent_id=agent_id,
            origin="audit", channel=channel, turn_id=turn_uid,
            relaunch_ctx={
                "origin": "audit", "session_id": session_id, "user_id": user_id,
                "agent_id": agent_id, "channel": channel, "timeout_seconds": 600,
                # The injected self-note row — a crash-resume re-parents to it so the
                # resumed run's assistant rows stay in the task tree.
                "parent_interaction_id": turn_uid,
            },
            build_turn=_build_turn, await_result=True, result_timeout=640,
        )
        logger.info(
            "output auditor: round %d/%d sent back to the main loop; "
            "re-run finished (%s)",
            round_no, max_rounds,
            (outcome.status if outcome else "?"),
        )

        # Backfill session_seq onto the re-run's rows. Background turns skip
        # the chat-API reconcile pass, leaving their rows seq-less — invisible
        # to the live UI tail and (without the collector's created_at fallback)
        # to the round-N+1 summarizer. Reserve ONE atomic block up front (the
        # manifest-backed reserving allocator, not per-row MAX+1 reads which
        # race any concurrent user message) and assign in created_at order.
        try:
            _conn = db._get_conn()
            try:
                _seqless = _conn.execute(
                    "SELECT id FROM interactions "
                    "WHERE session_id = ? AND session_seq IS NULL "
                    "AND created_at >= COALESCE(("
                    "  SELECT created_at FROM interactions WHERE id = ?"
                    "), '') ORDER BY created_at, id",
                    (session_id, turn_uid),
                ).fetchall()
            finally:
                _conn.close()
            if _seqless:
                _base = await db.next_session_seq(session_id, len(_seqless))
                for _i, _r in enumerate(_seqless):
                    await db.stamp_interaction_seq(_r["id"], _base + _i)
                logger.debug("output auditor: backfilled %d seq-less row(s)", len(_seqless))
        except Exception as _bf_err:  # noqa: BLE001 — cosmetic; never break the close-out
            logger.debug("output auditor: seq backfill failed: %s", _bf_err)
    except Exception as e:  # noqa: BLE001 — never let the send-back break a turn
        logger.warning("output auditor: send-back failed (%s)", e)


async def _emit_progress_note(
    session_id: str, step: str, *, user_id: str = "", db: Any = None
) -> None:
    """Best-effort live pipeline event so the chat activity indicator can show
    closer progress notes ('Sending to Closer…', 'Closer auditing…', 'Closer
    writing…'). Mirrors the summary push path; never raises — a failed emit
    must not break the close-out."""
    try:
        from app.chat.execution import _emit_to_visualizers
        await _emit_to_visualizers(session_id, {
            "type": "pipeline", "level": "pipeline", "step": step,
        }, user_id=user_id, db_override=db)
    except Exception:
        pass


def _run_stopped_by_user(db: Any, session_id: str, final_asst_id: str) -> bool:
    """True when the run being closed was stopped by the USER (Stop button).

    The user-Stop path (``app/agent/run_manager.py`` → ``interrupt``) records
    ``stop_cause='user_stop'`` on the run whose ``assistant_interaction_id``
    is the final (partial) assistant row. A user stop must silence the closer
    entirely — no summary, no checklist judgment, no closer lane — because the
    user explicitly killed the run and a machine close-out would be noise.

    Note: the pending-interrupt row in ``session_interrupts`` is CLEARED by the
    main loop the moment it consumes the stop, so it is NOT a reliable signal
    at close-out time; the run's persisted ``stop_cause`` is.
    """
    if not session_id or not final_asst_id:
        return False
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT stop_cause FROM session_runs "
            "WHERE assistant_interaction_id = ? LIMIT 1",
            (final_asst_id,),
        ).fetchone()
    finally:
        conn.close()
    return bool(row and (row["stop_cause"] or "") == "user_stop")


_FALLBACK_MUTATING_TOOLS = {
    "write_source", "edit_source", "patch_source", "delete_source",
    "resolve_conflict", "commit_and_push", "restart_server", "run_python",
    "create_tool", "create_agent", "update_agent", "set_agent_tool",
    "edit_agent_prompt", "set_agent_ability", "manage_agent_skills",
    "wiki_create", "wiki_update", "wiki_set_status", "wiki_delete",
    "schedule_task", "update_automation", "cancel_automation",
}


def _collect_run_mode_context(
    db: Any,
    session_id: str,
    parent_interaction_id: Optional[str],
    final_asst_id: str,
    starting_mode: Optional[str],
    final_mode: Optional[str],
) -> Dict[str, Any]:
    """Reconstruct the ordered, task-local mode path and mutation posture.

    User footer switches are durable ``system:mode`` rows. Tool rows now carry
    the exact mode/policy used at validation time; legacy rows fall back to the
    nearest preceding mode notice and conservative changed-path/tool-name
    classification. The next user message bounds the task when a recovery
    Closer runs later.
    """
    from app.agent.execution_modes import normalize_mode_id

    start_mode = normalize_mode_id(starting_mode, fallback="")
    end_mode = normalize_mode_id(final_mode, fallback="")
    modes: List[str] = [start_mode] if start_mode else []
    mutations: Dict[str, int] = {}
    conn = db._get_conn()
    try:
        start_row = conn.execute(
            "SELECT session_seq FROM interactions WHERE id=? LIMIT 1",
            (parent_interaction_id,),
        ).fetchone() if parent_interaction_id else None
        start_seq = int(start_row["session_seq"] or 0) if start_row else 0
        next_user = conn.execute(
            "SELECT MIN(session_seq) AS s FROM interactions "
            "WHERE session_id=? AND role='user' AND session_seq>?",
            (session_id, start_seq),
        ).fetchone()
        bounded_by_next_user = bool(next_user and next_user["s"] is not None)
        if bounded_by_next_user:
            end_seq = int(next_user["s"]) - 1
        else:
            max_row = conn.execute(
                "SELECT COALESCE(MAX(session_seq), 0) AS s FROM interactions WHERE session_id=?",
                (session_id,),
            ).fetchone()
            end_seq = int(max_row["s"] or 0) if max_row else 0
        rows = conn.execute(
            "SELECT role, source, tool_name, metadata FROM interactions "
            "WHERE session_id=? AND session_seq>? AND session_seq<=? "
            "AND (source='system:mode' OR role='tool') ORDER BY session_seq",
            (session_id, start_seq, end_seq),
        ).fetchall()
    finally:
        conn.close()


    current = start_mode
    for row in rows:
        try:
            meta = json.loads(row["metadata"] or "{}")
            if not isinstance(meta, dict):
                meta = {}
        except Exception:
            meta = {}
        if row["source"] == "system:mode":
            mode = normalize_mode_id(meta.get("mode"), fallback="")
            if mode:
                current = mode
                if not modes or modes[-1] != mode:
                    modes.append(mode)
            continue
        if row["role"] != "tool" or not bool(meta.get("success")):
            continue
        mode = normalize_mode_id(meta.get("execution_mode"), fallback="") or current
        if mode and mode != current:
            current = mode
            if not modes or modes[-1] != mode:
                modes.append(mode)
        elif mode and not modes:
            current = mode
            modes.append(mode)
        changed_paths = meta.get("changed_paths")
        mutating = bool(meta.get("mutating"))
        if "mutating" not in meta:
            mutating = bool(changed_paths) or str(row["tool_name"] or "") in _FALLBACK_MUTATING_TOOLS
        if mutating and mode:
            mutations[mode] = mutations.get(mode, 0) + 1

    if end_mode and not bounded_by_next_user:
        current = end_mode
        if not modes or modes[-1] != end_mode:
            modes.append(end_mode)
    if not modes and current:
        modes.append(current)
    segments = [
        f"{mode} ({mutations.get(mode, 0)} mutating action(s))"
        for mode in modes
    ]
    return {
        "starting_mode": modes[0] if modes else start_mode or end_mode,
        "final_mode": current or (modes[-1] if modes else ""),
        "timeline": modes,
        "executed_modes": [mode for mode, count in mutations.items() if count > 0],
        "mutations": mutations,
        "summary": " → ".join(segments),
    }


def _contract_completion_evidence(db: Any, final_asst_id: str,
                                  protected_lines: List[str],
                                  mode_context: Dict[str, Any]) -> Dict[str, Any]:
    """Read the bounded evidence snapshot persisted by the completed main loop."""
    evidence: Dict[str, Any] = {
        "changed_paths": [], "edit_events": [], "verification_events": [],
        "manager_checks_used": 0, "manager_blocks": 0,
        "run_mode": mode_context, "assistant_transcript": protected_lines[-80:],
    }
    try:
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT metadata FROM interactions WHERE id=? LIMIT 1",
                (final_asst_id,),
            ).fetchone()
        finally:
            conn.close()
        raw = row["metadata"] if row else None
        meta = json.loads(raw or "{}") if isinstance(raw, str) else (raw or {})
        stored = meta.get("contract_evidence") if isinstance(meta, dict) else None
        if isinstance(stored, dict):
            evidence.update(stored)
    except Exception as exc:
        logger.debug("output closer: contract evidence unavailable: %s", exc)
    return evidence


async def run_output_closer(
    user_id: str,
    session_id: str,
    agent_id: Optional[str] = None,
    final_asst_id: Optional[str] = None,
    parent_interaction_id: Optional[str] = None,
    db: Optional[Any] = None,
    channel: Optional[str] = None,
    audit_eligible: bool = False,
    execution_mode: Optional[str] = None,
    starting_execution_mode: Optional[str] = None,
) -> None:
    """Durable, fully parallel close-out of one agent run.

    Fired as a background task after the final ``response`` event is yielded;
    never raises. Retries transient LLM errors and blank completions
    (``_LLM_ATTEMPTS``), retries the DB insert on SQLite contention
    (``_WRITE_ATTEMPTS``), and stamps failures onto the final assistant row so
    the recovery sweep (``start_sweep``) can re-fire summarization later.
    Skips silently when the feature is off, no model is configured, or the
    run produced no assistant messages.

    ``audit_eligible`` gates the checklist-audit stage: it is True ONLY for a
    COMPLETED run (the loop's clean final-response site). Interrupted, errored,
    stall-guard, max-turns and pre-cleanup endings pass False, so a checklist
    never audits — and never sends back — against a partial answer. The summary
    still runs for those (unchanged behavior), just without the audit block.
    """
    _perf = _CloserPerfTimer(
        session_id=session_id, final_asst_id=final_asst_id,
        user_id=user_id, agent_id=agent_id,
    )
    _contract_cleanup = False
    _contract_turn_id = ""
    _contract_db = db
    _perf.mark("start", audit_eligible=bool(audit_eligible))
    try:
        # The active Closing Message gate is the sole per-agent activation
        # authority. WEBAGENT_DISABLE_OUTPUT_CLOSER is the operator-wide stop.
        if _operator_disabled():
            logger.info("output closer: disabled by %s", _OPERATOR_DISABLE_ENV)
            return

        if not user_id or not session_id or not final_asst_id:
            return
        _perf.mark("feature_gate_done")

        from app.db import get_db
        if db is None:
            db = get_db()
        _contract_db = db

        from app.agent.run_fence import (
            interaction_turn_id, register_current_one_shot,
            side_effects_allowed,
        )
        expected_turn_id = interaction_turn_id(db, final_asst_id)
        _contract_turn_id = expected_turn_id or final_asst_id
        # Live hooks are detached tasks.  The recovery sweep is itself owned by
        # the background leader, so do not register that long-lived sweep task
        # as though it belonged to one chat session.
        current_task = asyncio.current_task()
        if current_task is not None and current_task.get_name() != "closer_sweep":
            register_current_one_shot(session_id, expected_turn_id)
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return
        _perf.mark("generation_fence_checked", expected_turn_id=expected_turn_id)

        # Fetch the record before transcript/model work and retain it for prompt
        # and checklist resolution below. Closing Message is the sole per-agent
        # activation authority; absent or out-of-scope policy fails closed.
        agent_rec = (await db.get_agent_by_id(agent_id)) if agent_id else None
        try:
            from app.agent.subagent_contracts import resolved_contract_config
            _contract_cleanup = bool(
                resolved_contract_config(agent_rec, execution_mode).get("enabled")
            )
        except Exception:
            _contract_cleanup = False
        _perf.mark("agent_loaded")
        closer_mode = execution_mode
        if closer_mode is None:
            try:
                closer_mode = await db.get_session_execution_mode(session_id)
            except Exception:
                closer_mode = None
        if not _closing_output_enabled(agent_rec, closer_mode):
            logger.info("output closer: disabled by Closing Message output toggle for agent %s", agent_id)
            await _stamp_closer_skipped_disabled(db, final_asst_id)
            return

        # A USER-STOPPED run gets NO close-out at all. The Stop button kills
        # the main loop AND must kill the closer: skip entirely (no summary,
        # no checklist judgment, no progress note, no closer lane) so a machine
        # close-out never speaks for a run the user explicitly killed.
        # Checked before the progress note and before any LLM call so the
        # closer never even initiates work for a stopped run.
        if _run_stopped_by_user(db, session_id, final_asst_id):
            logger.info("output closer: run %s stopped by user — skipping close-out",
                        session_id[:8])
            return

        # A send-back re-run's response is parented to the self-note row; unwind
        # to the ORIGINAL task-start user message so the summary/audit window and
        # 'user request' context stay correct across rounds (round 2+ otherwise
        # sees the audit feedback as the "request" and — on a fresh session with
        # no prior summary — a window that excludes the original work).
        orig_parent_id = _resolve_original_parent(db, parent_interaction_id)

        try:
            live_mode = await db.get_session_execution_mode(session_id)
        except Exception:
            live_mode = execution_mode
        try:
            mode_context = _collect_run_mode_context(
                db, session_id, orig_parent_id, final_asst_id,
                starting_execution_mode or execution_mode,
                live_mode or execution_mode,
            )
        except Exception as exc:
            logger.debug("output closer: run mode timeline unavailable: %s", exc)
            _fallback_mode = live_mode or execution_mode or starting_execution_mode or ""
            mode_context = {
                "starting_mode": starting_execution_mode or execution_mode or _fallback_mode,
                "final_mode": _fallback_mode,
                "timeline": [_fallback_mode] if _fallback_mode else [],
                "executed_modes": [], "mutations": {}, "summary": "",
            }
        execution_mode = mode_context.get("final_mode") or execution_mode

        lines, request, msgs = _collect_span_messages(
            db, session_id, orig_parent_id, final_asst_id)
        _perf.mark(
            "span_collected", message_count=len(msgs or []),
            line_count=len(lines or []),
        )

        from app.agent.task_state import current_state, completion_gaps, accept_review
        canonical_target = current_state(db, session_id)
        canonical_review_applied = False

        def apply_canonical_review():
            nonlocal audit_verdict, audit_missing, canonical_review_applied
            if not canonical_target or canonical_review_applied:
                return True
            gaps = completion_gaps({**canonical_target, "manager_findings": []})
            audit_missing = list(dict.fromkeys([*audit_missing, *gaps]))
            if audit_verdict != "pass" or audit_missing:
                audit_verdict = "fail" if audit_missing else "inconclusive"
            accepted = accept_review(
                db, session_id, canonical_target["turn_id"],
                canonical_target["revision"], audit_verdict or "inconclusive",
                audit_missing,
            )
            canonical_review_applied = accepted
            return accepted

        # Reuse an answer that is already an adequate close-out. This gate is
        # intentionally ahead of model resolution and checklist auditing: a
        # lone final answer does not need to be paraphrased, and lightweight
        # conversation ("hi", "thanks", acknowledgements) must never trigger
        # the agent's task checklist or an auditor send-back.
        reused_summary = None if canonical_target else _reusable_final_response(request, msgs)
        protected_lines, protected_msgs, verbatim_blocks = _protect_verbatim_content(
            lines, msgs)
        if mode_context.get("summary"):
            protected_lines.insert(
                0,
                "Run mode timeline (oldest first; switches apply prospectively): "
                + mode_context["summary"],
            )

        # Fence the durable task before the slow LLM call.  A later Closer can
        # advance this revision while this call is in flight; the final CAS
        # then rejects this stale result instead of moving task state backward.
        checkpoint_target = _prepare_codex_checkpoint_target(
            db, agent_rec, session_id, final_asst_id)

        model_str = ""
        provider_str = ""
        client = None
        fallback_reason = ""
        if not reused_summary:
            # Resolve the model/client ONCE up front. A missing model is a config
            # gap, not a transient blank completion — don't run retries or stamp
            # the row (which would pollute metadata and make the sweep churn).
            model_str, provider_str, client = await _resolve_fast_llm(user_id, agent_rec)
            _perf.mark("model_resolved", model=model_str or "", configured=bool(client))
            if client is None:
                reused_summary = _fallback_working_response(msgs)
                fallback_reason = "closer_model_unavailable"
                if not reused_summary:
                    logger.debug("output closer: no LLM or working response available")
                    return
            else:
                # ── Live progress note: the closer has taken over the run ──
                await _emit_progress_note(
                    session_id, "closer_start", user_id=user_id, db=db)

        # ── Checklist judgment folded into the closer's single call ──
        # Resolve the per-agent prop / app-level checklist. No checklist ⇒ the
        # original pure-summary behavior, byte-for-byte unchanged. With a
        # checklist, the closer's ONE LLM call both writes the summary AND
        # judges the run against it (no separate auditor call), reading the
        # user's messages and the agent's responses since the last closer
        # (no tool call results) so every item is judgeable from the outputs.
        # The judgment only runs when ``audit_eligible`` is True (a COMPLETED
        # run) — partial endings get the plain summary and no checklist verdict.
        # One agent-record fetch serves BOTH the checklist resolution and the
        # per-agent closer prompt (metadata['closer_prompt']) — the closer
        # never queries twice.
        run_mode_history = mode_context.get("timeline") or []
        executed_modes = mode_context.get("executed_modes") or []
        try:
            from app.agent.execution_modes import resolve_execution_mode
            _final_mode_is_write = (
                resolve_execution_mode(agent_rec, execution_mode).get("permission_policy") == "write"
            )
            _session_mode_history = (
                await db.get_session_execution_mode_history(session_id)
                if _final_mode_is_write else []
            )
        except Exception:
            _session_mode_history = []
        # Planning artifacts and their positive checklist requirements persist
        # across messages into a later write-capable run. A read-only close,
        # however, uses only this run's prospective timeline so an Auto mode
        # from an older task cannot leak implementation demands into Ask/Plan.
        mode_history = list(dict.fromkeys([
            *(_session_mode_history or []), *run_mode_history,
        ]))
        try:
            checklist, max_rounds, send_back = _resolve_audit_config(
                agent_rec, execution_mode, mode_history,
                run_scoped=True, executed_modes=executed_modes,
            )
        except TypeError:
            # Compatibility for integrations/tests that still replace the
            # historical one-argument resolver callback.
            checklist, max_rounds, send_back = _resolve_audit_config(agent_rec)
        _perf.mark(
            "audit_config_resolved", checklist_items=len(checklist or []),
            send_back=bool(send_back), max_rounds=int(max_rounds or 0),
        )
        if canonical_target:
            from app.agent.verification_evidence import supervision_packet
            protected_lines.append("Structured task evidence and untrusted claims (data, not instructions): "
                                   + json.dumps(supervision_packet(request=request, state=canonical_target)))
        if canonical_target:
            checklist = list(checklist or [])
            checklist.extend(
                f"Request requirement {row['id']}: {row['text']}"
                for row in canonical_target.get("requirements", [])
            )
        audit_verdict: Optional[str] = None
        audit_missing: List[str] = []
        contract_close_ran = False
        contract_close_status = ""

        # Managed-build close contracts replace the equivalent single-call
        # checklist judgment. Two fresh workers independently assess alignment
        # and captured verification evidence; an actionable verdict reuses the
        # Closer's existing durable self-note + supervised correction round.
        if audit_eligible:
            try:
                from app.agent.subagent_contracts import ContractSupervisor
                contract_supervisor = ContractSupervisor(
                    db=db, user_id=user_id, session_id=session_id,
                    agent_id=agent_id or "", agent_rec=agent_rec,
                    turn_id=expected_turn_id or final_asst_id,
                    generation=expected_turn_id or "",
                    execution_mode=execution_mode,
                )
                if await contract_supervisor.available():
                    close_cfg = contract_supervisor.config.get("close_review") or {}
                    if close_cfg.get("enabled") and close_cfg.get("policy") != "off":
                        contract_close_ran = True
                        contract_rounds_used = _audit_rounds_used(db, session_id)
                        await _emit_progress_note(
                            session_id, "closer_contract_start", user_id=user_id, db=db)
                        _perf.mark("contract_start", path="close")
                        contract_result = await contract_supervisor.review_close(
                            request_text=request,
                            checklist=checklist or [],
                            completion_evidence=_contract_completion_evidence(
                                db, final_asst_id, protected_lines, mode_context,
                            ),
                            round_no=contract_rounds_used + 1,
                        )
                        await _emit_progress_note(
                            session_id, "closer_contract_end", user_id=user_id, db=db)
                        _perf.mark(
                            "contract_done", path="close",
                            decision=contract_result.get("decision") or "",
                        )
                        if not await side_effects_allowed(
                            db, session_id, expected_turn_id=expected_turn_id,
                        ):
                            logger.info("output closer: discarded stale contract result for %s",
                                        session_id[:8])
                            await _emit_progress_note(
                                session_id, "contract_stale_discard",
                                user_id=user_id, db=db,
                            )
                            return
                        contract_decision = contract_result.get("decision")
                        if contract_decision == "pass":
                            gaps = completion_gaps({**canonical_target, "manager_findings": []}) if canonical_target else []
                            if gaps:
                                contract_decision = "revise"
                                contract_result = {**contract_result, "findings": gaps}
                        if contract_decision == "pass":
                            audit_verdict = "pass"
                            contract_close_status = "verified"
                        elif contract_decision in {"revise", "block"}:
                            audit_verdict = "fail"
                            contract_close_status = "needs_attention"
                            def _finding_text(item: Any) -> str:
                                if isinstance(item, dict):
                                    return str(item.get("message") or item.get("text")
                                               or item.get("reason") or json.dumps(item))
                                return str(item)
                            audit_missing = [
                                _finding_text(item)[:1000]
                                for item in (contract_result.get("findings") or
                                             contract_result.get("corrective_actions") or [])
                                if _finding_text(item).strip()
                            ][:50] or [str(contract_result.get("reason") or
                                          "Independent close review failed.")[:1000]]
                            if not apply_canonical_review():
                                return
                            contract_max_rounds = int(close_cfg.get("max_rounds") or 1)
                            if (send_back and agent_id
                                    and contract_rounds_used < contract_max_rounds
                                    and not _read_only_execution_violation(audit_missing)):
                                await _emit_progress_note(
                                    session_id, "contract_correction",
                                    user_id=user_id, db=db,
                                )
                                await _send_audit_back(
                                    db=db, user_id=user_id, session_id=session_id,
                                    agent_id=agent_id, channel=channel,
                                    final_asst_id=final_asst_id,
                                    feedback=str(contract_result.get("reason") or ""),
                                    missing=audit_missing,
                                    round_no=contract_rounds_used + 1,
                                    max_rounds=contract_max_rounds,
                                    original_parent_id=orig_parent_id,
                                    execution_mode=execution_mode,
                                )
                                return
                        elif contract_decision == "inconclusive":
                            # The review is best-effort infrastructure. It must
                            # never leave a completed user run without a visible
                            # close-out when capacity or the reviewer is unavailable.
                            logger.info("output closer: subagent review skipped: %s",
                                        contract_result.get("reason"))
                            contract_close_status = "review_skipped"
                            # review_close ran, which intentionally suppresses
                            # the legacy audit branch below. Supply the normal
                            # working-response fallback explicitly so this path
                            # persists one terminal closer row and recovery will
                            # not retry the unavailable review forever.
                            reused_summary = _fallback_working_response(msgs)
                            if not reused_summary:
                                logger.warning(
                                    "output closer: skipped review had no working response"
                                )
                                return
            except Exception as contract_error:  # noqa: BLE001
                logger.info("output closer: contract layer unavailable; using legacy audit (%s)",
                            contract_error)
                contract_close_ran = False

        summary = None
        resp = None
        last_err: Optional[Exception] = None
        if reused_summary:
            summary = reused_summary
            _perf.mark("summary_reused")
            logger.debug(
                "output closer: reusing final response for run %s (no LLM call)",
                session_id[:8],
            )
        elif checklist and audit_eligible and not contract_close_ran:
            # One call: summary + checklist verdict from the SAME clean
            # transcript the summary uses — the user's messages and the
            # agent's responses since the last closer. Tool call results are
            # deliberately NOT included: the closer's job is to summarize the
            # outputs and check the agent's responses for gaps against the
            # criteria, not to re-verify tool internals. (``lines``, ``request``
            # and ``msgs`` were collected above with include_tools=False.)
            rounds_used = _audit_rounds_used(db, session_id)
            combined = None
            await _emit_progress_note(
                session_id, "closer_audit_start", user_id=user_id, db=db)
            _perf.mark("llm_start", path="audit")
            for attempt in range(1, _LLM_ATTEMPTS + 1):
                try:
                    combined = await _attempt_combined_call(
                        protected_lines, request, protected_msgs, checklist,
                        model_str, client, agent_rec=agent_rec,
                        verbatim_blocks=verbatim_blocks)
                    if combined:
                        break
                except Exception as e:  # noqa: BLE001
                    logger.debug("output closer: combined attempt %d/%d failed: %s",
                                 attempt, _LLM_ATTEMPTS, e)
                if attempt < _LLM_ATTEMPTS:
                    await asyncio.sleep(_LLM_RETRY_BACKOFF_S * attempt)
            await _emit_progress_note(
                session_id, "closer_audit_end", user_id=user_id, db=db)
            _perf.mark(
                "llm_done", path="audit", attempts=attempt,
                produced_summary=bool(combined),
            )
            # The setting may have changed while the LLM call was in flight.
            # Stop before audit feedback, attempt stamps, or summary writes.
            if not await _closing_output_enabled_live(
                db, agent_id, session_id=session_id, execution_mode=execution_mode,
            ):
                logger.info("output closer: disabled during audit call")
                await _stamp_closer_skipped_disabled(db, final_asst_id)
                return
            if not await side_effects_allowed(
                db, session_id, expected_turn_id=expected_turn_id,
            ):
                logger.info("output closer: discarded stale audit for %s", session_id[:8])
                return
            if combined:
                summary = combined["summary"]
                audit_verdict = combined["verdict"]
                audit_missing = combined["missing"]
                if not apply_canonical_review():
                    return
                if (audit_verdict == "fail" and send_back and agent_id
                        and rounds_used < max_rounds
                        and not _read_only_execution_violation(audit_missing)):
                    # Send the verdict back INTO the main loop: the agent
                    # re-runs on the assistant-side self-note; its next final
                    # response re-triggers this summarizer for round N+1. No
                    # summary is written this round — the work is not done yet.
                    await _send_audit_back(
                        db=db, user_id=user_id, session_id=session_id,
                        agent_id=agent_id, channel=channel,
                        final_asst_id=final_asst_id,
                        feedback=combined["feedback"] or "",
                        missing=audit_missing,
                        round_no=rounds_used + 1, max_rounds=max_rounds,
                        original_parent_id=orig_parent_id,
                        execution_mode=execution_mode,
                    )
                    return
                if audit_verdict == "fail":
                    logger.info(
                        "output closer: checklist FAIL round %d/%d; closing "
                        "with %d item(s) still missing",
                        rounds_used + 1, max_rounds, len(audit_missing))
                    # Never let a FAILED audit close as a plain success. The
                    # summary written below tells the user the work is
                    # unfinished, so the durable run row must carry the same
                    # verdict plus a specific reason — and stay recoverable when
                    # a retry could still help. Without this, the run reads as
                    # ``complete``: a false green that no watchdog will revisit.
                    await _record_audit_blockage(
                        db, session_id, audit_missing,
                        round_no=rounds_used + 1, max_rounds=max_rounds,
                        blocked=_read_only_execution_violation(audit_missing),
                    )
            else:
                logger.warning(
                    "output closer: no summary after %d attempt(s) — %s",
                    _LLM_ATTEMPTS,
                    f"{last_err}" if last_err else "empty completions",
                )
                await _stamp_closer_attempt(db, final_asst_id)
                summary = _fallback_working_response(msgs)
                fallback_reason = "closer_audit_failure"
                if not summary:
                    return
        else:
            # No checklist (or a partial run): the original pure-summary path
            # with the clean transcript — byte-for-byte behavior preserved.
            await _emit_progress_note(
                session_id, "closer_llm_start", user_id=user_id, db=db)
            _perf.mark("llm_start", path="summary")
            for attempt in range(1, _LLM_ATTEMPTS + 1):
                try:
                    summary, _, resp = await _attempt_closer_call(
                        protected_lines, request, protected_msgs, model_str,
                        client, agent_rec=agent_rec,
                        verbatim_blocks=verbatim_blocks)
                    if summary:
                        break
                    last_err = None  # blank completion — retry it
                except Exception as e:  # noqa: BLE001
                    last_err = e
                    logger.debug("output closer: attempt %d/%d failed: %s",
                                 attempt, _LLM_ATTEMPTS, e)
                if attempt < _LLM_ATTEMPTS:
                    await asyncio.sleep(_LLM_RETRY_BACKOFF_S * attempt)
            await _emit_progress_note(
                session_id, "closer_llm_end", user_id=user_id, db=db)
            _perf.mark(
                "llm_done", path="summary", attempts=attempt,
                produced_summary=bool(summary),
            )
            # The setting may have changed while the LLM call was in flight.
            # Stop before attempt stamps or summary/checkpoint persistence.
            if not await _closing_output_enabled_live(
                db, agent_id, session_id=session_id, execution_mode=execution_mode,
            ):
                logger.info("output closer: disabled during summary call")
                await _stamp_closer_skipped_disabled(db, final_asst_id)
                return
            if not await side_effects_allowed(
                db, session_id, expected_turn_id=expected_turn_id,
            ):
                logger.info("output closer: discarded stale summary for %s", session_id[:8])
                return
            if not summary:
                # Stamp the failure so the recovery sweep backs off instead of
                # re-firing immediately, and records the attempt for diagnostics.
                await _stamp_closer_attempt(db, final_asst_id)
                logger.warning(
                    "output closer: no summary after %d attempt(s) — %s",
                    _LLM_ATTEMPTS,
                    f"{last_err}" if last_err else "empty completions",
                )
                summary = _fallback_working_response(msgs)
                fallback_reason = "closer_llm_failure"
                if not summary:
                    return

        if not apply_canonical_review():
            logger.info("output closer: discarded stale canonical review")
            return

        # Fallback metadata remains durable, but the user receives the useful
        # working response as the Closer message without an alarming wrapper.
        # ── Persist as a role='system' row so the summary survives ──
        # refresh. source='system:closer' tells the UI to render it as its
        # own separate 'Closer' lane bubble after the agent's response
        # (session-load / reconcile map that source). session_history skips
        # unknown roles, so it is never fed back into the agent's context.
        # The insert retries SQLite writer-slot contention (turn-end writers
        # race for the single writer lock) — a lost write would strand the
        # summary until the recovery sweep notices and re-fires.
        if not await _closing_output_enabled_live(
            db, agent_id, session_id=session_id, execution_mode=execution_mode,
        ):
            logger.info("output closer: disabled before summary persist")
            await _stamp_closer_skipped_disabled(db, final_asst_id)
            return
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return
        # Presentation only: the Closer bubble is labelled by the model's
        # user-facing name (the caller's own display name, then the catalog model
        # name, then the raw id) — never the raw id alone. See _closer_display_name.
        closer_display_name = await _closer_display_name(user_id, agent_rec, model_str)
        turn_id, turn_seq, _final_seq = _final_row_meta(db, final_asst_id)
        # Allocate the Closer's order through the reserved-counter path instead of
        # a bare MAX(session_seq)+1 scan, and never let the Closer sort BEFORE the
        # row it closes. That row is the run's terminal answer — for a max-turns or
        # stall stop it is written at the very end of the run, so a late stamp could
        # still be NULL when this task allocates: the Closer would then take the
        # message's number and the message would be stamped one higher afterwards,
        # rendering the terminal message as the newest node above its own closer.
        try:
            seq = await db.next_session_seq(session_id, 1)
        except Exception:  # noqa: BLE001 — keep the local scan as a fallback
            seq = _next_session_seq(db, session_id)
        if _final_seq and seq <= int(_final_seq):
            seq = int(_final_seq) + 1
        row_id = None
        last_wr: Optional[Exception] = None
        for attempt in range(1, _WRITE_ATTEMPTS + 1):
            # Re-read the Closing Message gate for every retry: it alone may
            # revoke a pending closer write while the worker is in flight.
            if not await _closing_output_enabled_live(
                db, agent_id, session_id=session_id, execution_mode=execution_mode,
            ):
                logger.info("output closer: disabled before summary write attempt")
                await _stamp_closer_skipped_disabled(db, final_asst_id)
                return
            if not await side_effects_allowed(
                db, session_id, expected_turn_id=expected_turn_id,
            ):
                return
            try:
                row_id = await db.insert_interaction(
                    user_id, session_id, role="system", content=summary,
                    parent_id=final_asst_id, channel=channel,
                    metadata=json.dumps({
                        "kind": "summary",
                        "model": model_str or "",
                        "display_name": closer_display_name,
                        "asst_id": final_asst_id,
                        # Checklist-audit outcome (absent when no checklist ran).
                        "audit": audit_verdict or "",
                        "audit_missing": audit_missing,
                        "contract_status": contract_close_status,
                        "reused_final_response": bool(reused_summary),
                        "verbatim_blocks": len(verbatim_blocks),
                        "fallback_working_response": bool(fallback_reason),
                        "fallback_reason": fallback_reason,
                        "verbatim_kinds": sorted({
                            block["kind"] for block in verbatim_blocks
                        }),
                    }),
                    output_data=None,
                    sender_id=agent_id,
                    receiver_id=user_id,
                    source="system:closer",
                    session_seq=seq,
                    turn_id=turn_id,
                    turn_seq=turn_seq,
                )
                break
            except Exception as e:  # noqa: BLE001
                last_wr = e
                if "lock" not in str(e).lower():
                    break
                if attempt < _WRITE_ATTEMPTS:
                    await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
        if not row_id:
            await _stamp_closer_attempt(db, final_asst_id)
            logger.warning("output closer: summary persist failed: %s", last_wr)
            return
        _perf.mark("summary_persisted", row_id=row_id, write_attempts=attempt)

        # V2 projection is idempotent across the live hook and recovery sweep.
        try:
            from app.chat.repository import ChatRepository
            await ChatRepository(user_id).publish_closer_summary(session_id,row_id,summary)
        except Exception as exc:
            logger.debug("output closer: public projection failed: %s", exc)

        # The visible summary is already durable.  Materialize its structured
        # continuation contract independently; failures are intentionally
        # best-effort and never alter or remove the Closer lane.
        await _save_codex_closer_checkpoint(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            target=checkpoint_target,
            request=request,
            summary=summary,
            audit_eligible=audit_eligible,
            audit_verdict=audit_verdict,
            audit_missing=audit_missing,
            final_asst_id=final_asst_id,
            closer_row_id=row_id,
        )
        _perf.mark("checkpoint_saved")

        # Enrich the deterministic run handoff using this same Closer result.
        # This is machine-facing state only: no additional model call, and the
        # shared generation fence rejects late writes after Stop/replacement.
        try:
            from app.agent.run_handoff import capsule_for_turn, persist_capsule
            _handoff_turn = expected_turn_id or turn_id
            if _handoff_turn:
                _existing_handoff = capsule_for_turn(db, session_id, _handoff_turn) or {}
                _completed = list(_existing_handoff.get("completed") or [])
                if audit_verdict == "pass":
                    _completed.extend(
                        item for item in (checklist or []) if item not in _completed
                    )
                _open = (audit_missing if audit_missing
                         else list(_existing_handoff.get("open_requirements") or []))
                await persist_capsule(
                    db,
                    session_id=session_id,
                    turn_id=_handoff_turn,
                    run_id=final_asst_id,
                    status=("complete" if audit_eligible else
                            str(_existing_handoff.get("status") or "interrupted")),
                    stop_cause=_existing_handoff.get("stop_cause"),
                    user_id=user_id,
                    agent_id=agent_id,
                    execution_mode=execution_mode,
                    objective=request,
                    completed=_completed,
                    open_requirements=_open,
                    blockers=(_open if audit_verdict == "fail" else []),
                    next_action=("Complete the remaining contract items."
                                 if _open else ""),
                    summary=summary,
                    source="closer",
                )
        except Exception as exc:
            logger.debug("output closer: handoff capsule write failed: %s", exc)

        # Book the tokens against background usage (best-effort), linked to the
        # summary row just persisted so the UI can show the size of the
        # CLOSER's own prompt next to this Closer-lane bubble (not the
        # session's full context).
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model_str,
                    provider=provider_str,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="closer",
                    session_id=session_id,
                    user_id=user_id,
                    agent_id=agent_id,
                    interaction_id=row_id,
                )
        except Exception:
            pass

        # ── Push live to the chat UI (parallel event, same path as tool/response) ──
        try:
            from app.chat.execution import _emit_to_visualizers
            await _emit_to_visualizers(session_id, {
                "type": "summary",
                "level": "agent",
                "content": summary,
                "id": row_id,
                "asst_id": final_asst_id,
                "session_seq": seq,
                "turn_id": turn_id,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "contract_status": contract_close_status,
            }, user_id=user_id, db_override=db)
            _perf.mark("summary_emitted")
        except Exception as _emit_err:
            logger.debug("output closer: emit failed: %s", _emit_err)
    except Exception as e:  # noqa: BLE001 — never let the summarizer break a turn
        _perf.mark("failed", error_type=type(e).__name__)
        logger.warning("output closer: skipped (%s)", e)
    finally:
        if _contract_cleanup and _contract_db is not None and _contract_turn_id:
            try:
                from app.agent.subagent_contracts import stop_turn_workers
                await stop_turn_workers(
                    _contract_db, session_id, _contract_turn_id,
                )
            except Exception:
                logger.debug("output closer: contract worker cleanup failed", exc_info=True)


# ────────────────────────────────────────────────────────────────────────────
# Tier-2 recovery sweep — the watchdog-analog for the summarizer.
#
# The live hook above is an in-memory task: if the process dies between the
# final ``response`` event and the summary insert, or the LLM is down, or the
# hook never ran, nothing would ever produce that summary. This leader-registered
# sweep (started from app/main.py, gated on the output_closer app function)
# periodically finds final assistant responses that have NO close-out row (``system:closer``/``system:summary``/``system:overview``)
# child and re-fires the closer — exactly how the Session Namer heals
# sessions stuck on fallback titles. Bounded per tick, cooldown-aware (the
# failure stamp above), and min-age gated so it never races a live in-flight
# summary.
# ────────────────────────────────────────────────────────────────────────────


def _find_final_rows_without_summary(db: Any, limit: int) -> List[dict]:
    """Candidate rows needing a summary: completed final assistant responses
    (message_phase main/final) PLUS interrupted/errored partial answers, with
    NO close-out row (``system:closer``/``system:summary``/``system:overview``) child yet. Interrupted/errored rows carry their
    partial text (status set in the loop's cancel/error handlers) and get a
    summary the same way a completed run does — the window collector spans
    from the last summary through the interruption."""
    conn = db._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT i.id, i.session_id, s.user_id, s.agent_id, i.channel,
                   i.parent_id, i.metadata, i.created_at
            FROM interactions i
            JOIN sessions s ON s.id = i.session_id
            WHERE i.role = 'assistant'
              AND i.parent_id IS NOT NULL
              AND COALESCE(i.metadata, '') NOT LIKE '%"closer_skipped_disabled"%'
              AND NOT EXISTS (
                  SELECT 1 FROM interactions ov
                  WHERE ov.source IN ('system:overview', 'system:summary', 'system:closer') AND ov.parent_id = i.id
              )
              AND NOT EXISTS (
                  -- A row the auditor already sent back for fixes: its close-out
                  -- summary will come from the re-run's final response. Re-firing
                  -- here would race the in-flight fix-loop with a duplicate.
                  SELECT 1 FROM interactions af
                  WHERE af.source = 'system:audit' AND af.parent_id = i.id
              )
              AND NOT EXISTS (
                  -- A run the USER stopped (Stop button): it gets no close-out
                  -- at all — the user killed it, so no summary/audit lane.
                  SELECT 1 FROM session_runs r
                  WHERE r.assistant_interaction_id = i.id
                    AND r.stop_cause = 'user_stop'
              )
              AND (
                  (i.status IS NULL OR i.status NOT IN ('interrupted', 'error', 'deleted'))
                  AND (i.metadata LIKE '%"message_phase": "main"%'
                       OR i.metadata LIKE '%"message_phase": "final"%')
                  OR i.status IN ('interrupted', 'error')
              )
            ORDER BY i.created_at ASC
            """
        )
        # Apply the retry cooldown before the effective candidate limit.  Doing
        # it in _sweep_once after SQL LIMIT made cooled-down rows at the head of
        # the queue consume every slot indefinitely, starving later eligible
        # rows from recovery.
        now = datetime.now(timezone.utc)
        candidates: List[dict] = []
        for row in rows:
            candidate = dict(row)
            try:
                metadata = json.loads(candidate.get("metadata")) if candidate.get("metadata") else {}
                if not isinstance(metadata, dict):
                    metadata = {}
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            last_attempt = _parse_ts(
                metadata.get("summary_attempt_at") or metadata.get("overview_attempt_at")
            )
            if last_attempt and (now - last_attempt).total_seconds() < _SWEEP_RETRY_COOLDOWN_S:
                continue
            candidates.append(candidate)
            if len(candidates) >= limit:
                break
        return candidates
    finally:
        conn.close()


async def _sweep_once() -> int:
    """One recovery-sweep tick: re-fire summarization for final responses that
    never got one. Returns how many were attempted. Bounded (max per tick) and
    cooldown-aware (a row failed recently is skipped until its cooldown
    lapses), so a persistently-down provider cannot burn unlimited LLM calls.
    The re-fired worker resolves its own model+client (env-independent) and
    stamps its failures for the next tick."""
    from app.db import get_db
    db = get_db()
    # Do not preflight a raw API-key client here. Some rows can be closed by
    # reusing an adequate final answer without any model call, and the per-row
    # closer resolves the provider-aware transport (including ChatGPT OAuth)
    # using the candidate's owning user.
    try:
        candidates = _find_final_rows_without_summary(db, _SWEEP_MAX_PER_TICK)
    except Exception as e:  # noqa: BLE001
        logger.warning("output closer: sweep candidate query failed: %s",
                       e, exc_info=True)
        return 0

    now = datetime.now(timezone.utc)
    attempted = 0
    for row in candidates:
        asst_id = row.get("id")
        sid = row.get("session_id")
        owner = row.get("user_id")
        if not asst_id or not sid or not owner:
            continue
        # Recovery honors the same Closing Message gate as the live hook and
        # permanently records this intentional no-Closer outcome. Re-enabling
        # later must not backfill responses produced while the feature was off.
        if not await _closing_output_enabled_live(
            db, row.get("agent_id"), session_id=sid,
        ):
            await _stamp_closer_skipped_disabled(db, asst_id)
            continue
        try:
            meta = json.loads(row.get("metadata")) if row.get("metadata") else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        # Cooldown: the failure stamp sets summary_attempt_at (legacy rows used
        # overview_attempt_at) — don't hammer a row we tried recently.
        last_attempt = _parse_ts(meta.get("summary_attempt_at") or meta.get("overview_attempt_at"))
        if last_attempt and (now - last_attempt).total_seconds() < _SWEEP_RETRY_COOLDOWN_S:
            continue
        # Age gate: skip very fresh rows — their live hook may still be
        # running; don't double-summarize by racing it (worst-case live window
        # ≈ _LLM_ATTEMPTS × _LLM_TIMEOUT + backoff ≈ 62s < min-age).
        created = _parse_ts(row.get("created_at"))
        if created and (now - created).total_seconds() < _SWEEP_MIN_AGE_S:
            continue
        try:
            # Recovered COMPLETED rows are audit-eligible (they just missed their
            # live hook); interrupted/errored partial answers get summary-only.
            _status = row.get("status")
            _eligible = _status is None or _status not in ("interrupted", "error", "deleted")
            await run_output_closer(
                user_id=owner,
                session_id=sid,
                agent_id=row.get("agent_id"),
                final_asst_id=asst_id,
                parent_interaction_id=row.get("parent_id"),
                db=db,
                channel=row.get("channel"),
                audit_eligible=_eligible,
            )
            attempted += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("output closer: sweep summarization %s failed: %s",
                           str(asst_id)[:12], e, exc_info=True)
    return attempted


async def _sweep_loop() -> None:
    await asyncio.sleep(_SWEEP_STARTUP_DELAY_S)
    while True:
        try:
            _n = await _sweep_once()
            if _n:
                logger.info("output closer: recovery sweep re-triggered "
                            "%d close-out(s)", _n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("output closer: recovery sweep tick failed: %s",
                           e, exc_info=True)
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
        except asyncio.CancelledError:
            raise


_sweep_task: Optional[asyncio.Task] = None


async def start_sweep() -> None:
    """Start the recovery sweep (idempotent). Registered on the background
    leader from app/main.py, gated on the output_closer app function."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="closer_sweep")
    logger.info("Output Closer recovery sweep started (every %ss, max %d/tick)",
                _SWEEP_INTERVAL_S, _SWEEP_MAX_PER_TICK)
    # Visible sweep liveness: plugin INFO lines are suppressed by the
    # diagnostics handler, so record the start through the recorder directly
    # (persists at INFO) — otherwise a silently-dead sweep is indistinguishable
    # from a healthy one in the diagnostics page.
    try:
        from app.agent.diagnostics import record as _diag
        _diag("info", "recovery",
              "Output Closer recovery sweep started "
              f"(every {_SWEEP_INTERVAL_S}s, max {_SWEEP_MAX_PER_TICK}/tick)",
              source="closer_sweep")
    except Exception:  # noqa: BLE001
        pass


async def stop_sweep() -> None:
    global _sweep_task
    _t, _sweep_task = _sweep_task, None
    if _t is not None:
        _t.cancel()
        try:
            await _t
        except asyncio.CancelledError:
            pass

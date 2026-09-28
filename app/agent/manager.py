"""
Run Manager — per-trigger supervisory contracts for agent runs.

The Manager is the JUDGMENT layer above the mechanical contract
(``app/agent/contracts.py``) and the mode gates (ask/plan/auto — which already
live in ``app/agent/loop.py``). It fires bounded, mostly-stateless LLM calls at
specific TRIGGERS, each with its own prompt and STRICT-JSON verdict envelope,
reusing the Output Closer's machinery (window collection, standard-role model
resolution, tolerant JSON parsing, durable rows, usage booking). Every trigger
is OPT-IN and gated on the ``run_manager`` app function (App Settings ▸ App
Functions; descriptor at ``plugins/abilities/System/run_manager/``).

Triggers (kind → verdict envelope):
- ``plan_gate``    — before the FIRST write/edit tool: does the run have a
                     plan consistent with the request?     {approve|block|revise}
- ``edit_gate``    — before a write/edit tool: does THIS edit match the
                     approved plan?                        {approve|block|revise}
- ``watchdog``     — periodic (every N turns) or on tool-error clusters:
                     is the run on track?                  {on_track|off_track|stuck}
- ``commit_gate``  — before ``commit_and_push``: does the change set match
                     the contract?                         {approve|block|revise}

The Manager receives allowlisted durable task state, labeled agent claims, and
operation categories. Raw calls, arguments, outputs and the working transcript
are withheld. Exact operation scope can therefore remain unverified.
The Manager judges only that supplied evidence, not hidden tool internals (see
``_collect_manager_span``).

Blocking vs advisory: ``plan_gate`` / ``edit_gate`` / ``commit_gate`` may run
BLOCKING (the loop awaits the verdict before executing the guarded action —
fail-open on timeout/error so a dead Manager never freezes the agent) or ASYNC
(a background verdict lands as a self-note the next turn sees). ``watchdog`` is
always advisory (async) and never sits on the critical path.

Actionable feedback is persisted as an assistant-side SELF-NOTE
(role='assistant', source='system:manager') that rides the DB tail into the
agent's next turn — the same pattern the audit send-back uses
(``output_closer._send_audit_back``). Non-actionable verdicts (approve /
on_track) are stamped onto the anchor assistant row (``manager_checked_at`` /
``manager_verdicts``) instead of writing a visible row, so the transcript stays
clean. Per-run checks have an overall cap (``manager.max_checks``, default 9)
plus per-trigger caps (``manager.max_checks_by_kind``), so a noisy gate cannot
starve the other supervisory lanes or rack up unbounded Manager calls.

Config — per-agent ``metadata['manager']`` (JSON):
    {"plan_gate": "off"|"async"|"blocking",
     "edit_gate": "off"|"async"|"blocking",
     "watchdog":  {"every_n_turns": 6, "on_errors": 3,
                    "on_stall": true, "cooldown_turns": 2} | "off",
     "commit_gate": "off"|"async"|"blocking",
     "max_checks": 9,
     "max_checks_by_kind": {"plan_gate": 1, "edit_gate": 4,
                              "watchdog": 3, "commit_gate": 1},
     "max_blocks": 3}
Prompts — per-agent ``metadata['manager_<kind>_prompt']`` first, then
``app_level_prompts.manager_<kind>.template`` in app-prompts.json, then the
built-in fallback — the same precedence chain as the closer's prompt.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.util.paths import DEFAULTS_DIR, app_prompts_path

logger = logging.getLogger(__name__)

_APP_PROMPTS_PATH = app_prompts_path()

# The Manager is a background/advisory layer (or a bounded blocking gate).
# Windows are lean (text + tool names + placeholders), so a 60s total bound
# across all attempts is generous; retries mirror the closer's robustness
# pattern without multiplying worst-case latency.
_LLM_TIMEOUT = 60.0
_LLM_ATTEMPTS = 2
_LLM_RETRY_BACKOFF_S = 0.5
_WRITE_ATTEMPTS = 5
_WRITE_BACKOFF_S = 0.25
_MAX_MANAGER_TOKENS = 2048
_CONFIG_CACHE_TTL = 300

_MANAGER_SOURCE = "system:manager"   # source on persisted self-note rows
_DEFAULT_MAX_CHECKS = 9              # overall per-run cap on Manager LLM calls
_DEFAULT_MAX_CHECKS_BY_KIND: Dict[str, int] = {
    "plan_gate": 1,
    "edit_gate": 4,
    "watchdog": 3,
    "commit_gate": 1,
}
_DEFAULT_MAX_BLOCKS = 3

# Verdict sets per trigger kind (tolerant parse validates against these).
_VERDICTS: Dict[str, tuple] = {
    "plan_gate": ("approve", "block", "revise"),
    "edit_gate": ("approve", "block", "revise"),
    "commit_gate": ("approve", "block", "revise"),
    "watchdog": ("on_track", "off_track", "stuck"),
}

# Built-in fallback prompts (per-kind) so the Manager never breaks on a config
# error. Each demands STRICT JSON with a kind-specific envelope.
_FALLBACK_PROMPTS: Dict[str, str] = {
    "plan_gate": (
        "You are the run Manager reviewing an agent's PLAN before its first file edit.\n"
        "The working conversation below shows the user's request and the agent's work so far "
        "(tool results are withheld — judge intent and sequence, not tool internals).\n\n"
        "Decide whether the agent has a plan that genuinely addresses the request before it edits files.\n"
        "- approve: the work clearly follows a plan that matches the request.\n"
        "- revise: the approach is plausibly right but the plan is missing something important "
        "(tell the agent exactly what to add in feedback).\n"
        "- block: the agent is about to edit files with no coherent plan, or is going the wrong way.\n\n"
        "USER REQUEST:\n{user_request}\n\nWORKING CONVERSATION:\n{manager_transcript}\n\n"
        "EDIT BEING VERIFIED:\n{edit_context}\n\n"
        "Reply with STRICT JSON only — no prose, no markdown fences. "
        "reason must be non-empty; feedback must be non-empty for block or revise:\n"
        "{{\"verdict\": \"approve\"|\"block\"|\"revise\", \"reason\": \"...\", \"feedback\": \"...\"}}"
    ),
    "edit_gate": (
        "You are the run Manager verifying ONE edit before it executes.\n"
        "The working conversation below shows the user's request and the agent's work so far "
        "(tool results are withheld — judge intent and sequence, not tool internals).\n\n"
        "Decide whether THIS edit matches the approved plan and the user's request.\n"
        "- approve: the edit is clearly consistent with the plan/request.\n"
        "- revise: the edit is in the right area but wrong in some way (tell the agent exactly what).\n"
        "- block: the edit is off-plan, unrelated to the request, or destructive without justification.\n\n"
        "USER REQUEST:\n{user_request}\n\nWORKING CONVERSATION:\n{manager_transcript}\n\n"
        "EDIT BEING VERIFIED:\n{edit_context}\n\n"
        "Reply with STRICT JSON only — no prose, no markdown fences. "
        "reason must be non-empty; feedback must be non-empty for block or revise:\n"
        "{{\"verdict\": \"approve\"|\"block\"|\"revise\", \"reason\": \"...\", \"feedback\": \"...\"}}"
    ),
    "watchdog": (
        "You are the run Manager acting as a watchdog for an agent run.\n"
        "The working conversation below shows the user's request and the agent's recent work "
        "(tool results are withheld — judge intent and sequence from the tool-call NAMES).\n\n"
        "Trigger reason: {trigger}\n\n"
        "Decide whether the run is making real progress toward the request.\n"
        "- on_track: the agent is progressing sensibly.\n"
        "- off_track: the agent is drifting, thrashing, or repeating itself — suggest a correction.\n"
        "- stuck: the agent is clearly stuck (repeated failures, loops, no progress) — "
        "recommend changing approach or stopping to ask the user.\n\n"
        "USER REQUEST:\n{user_request}\n\nWORKING CONVERSATION (recent):\n{manager_transcript}\n\n"
        "Reply with STRICT JSON only — no prose, no markdown fences. "
        "reason must be non-empty; suggestion must be non-empty for off_track or stuck:\n"
        "{{\"verdict\": \"on_track\"|\"off_track\"|\"stuck\", \"reason\": \"...\", \"suggestion\": \"...\"}}"
    ),
    "commit_gate": (
        "You are the run Manager reviewing a commit before it is pushed.\n"
        "The working conversation below shows the user's request and the agent's work; "
        "raw tool results are withheld. The commit context is structured JSON containing "
        "the proposed commit arguments, changed-path/change inventory, and a verification "
        "summary. Judge only that supplied evidence: do not claim that you independently "
        "inspected the diff or proved there are no stray changes.\n\n"
        "Decide whether committing/pushing now is supported by the evidence and matches "
        "the request.\n"
        "- approve: the supplied change inventory matches the request and the verification "
        "summary is adequate.\n"
        "- revise: committing is premature because the evidence, scope, or verification is "
        "incomplete (say exactly what is needed).\n"
        "- block: the supplied inventory shows work that is wrong, unrelated, destructive, "
        "or unsafe to push.\n\n"
        "USER REQUEST:\n{user_request}\n\nWORKING CONVERSATION:\n{manager_transcript}\n\n"
        "STRUCTURED COMMIT EVIDENCE:\n{commit_context}\n\n"
        "Reply with STRICT JSON only — no prose, no markdown fences. "
        "reason must be non-empty; feedback must be non-empty for block or revise:\n"
        "{{\"verdict\": \"approve\"|\"block\"|\"revise\", \"reason\": \"...\", \"feedback\": \"...\"}}"
    ),
}


def _bundled_manager_prompts() -> Dict[str, str]:
    """Load the shipped prompt catalog as the resilient manager fallback."""
    try:
        data = json.loads(
            (DEFAULTS_DIR / "app-prompts.json").read_text(encoding="utf-8")
        )
        prompts = data.get("app_level_prompts") or {}
        resolved = {}
        for kind in _FALLBACK_PROMPTS:
            entry = prompts.get(f"manager_{kind}") or {}
            template = entry.get("template") or entry.get("text")
            if isinstance(template, str) and template.strip():
                resolved[kind] = template.strip()
        return resolved
    except Exception as exc:  # pragma: no cover - hardcoded prompts remain
        logger.debug("run manager: bundled prompt read failed: %s", exc)
        return {}


# Keep the inline strings as the last-ditch fallback, while making the shipped
# prompt catalog authoritative for both normal and fallback resolution.
_FALLBACK_PROMPTS.update(_bundled_manager_prompts())

_CONFIG_CACHE: Optional[tuple] = None  # (model, base_url, api_key, provider, expiry_ts)


# ── Config ───────────────────────────────────────────────────────────────────

def resolve_manager_config(
    agent_rec: Optional[dict], execution_mode: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve the per-agent Manager configuration from ``metadata['manager']``.

    Returns a normalized dict with every trigger key present, an overall
    per-run check cap, per-trigger caps, and the maximum blocking verdicts.
    All triggers default OFF — the Manager adds LLM calls, so it is strictly
    opt-in. Any parse error yields all-off with safe default caps.
    """
    # The canonical schema lives in a separate module so the API and runtime
    # share exactly one compatibility/validation path.  It returns the legacy
    # flat keys below as a view for the existing loop.
    from app.agent.manager_config import legacy_manager_view, resolve_manager_loop

    return legacy_manager_view(resolve_manager_loop(agent_rec, execution_mode))


def trigger_enabled(cfg: Dict[str, Any], kind: str) -> bool:
    """True when a trigger is configured on for this agent."""
    val = cfg.get(kind)
    if kind in ("plan_gate", "edit_gate"):
        return val in ("async", "blocking")
    if kind == "commit_gate":
        return val in ("async", "blocking", "on")
    if kind == "watchdog":
        return isinstance(val, dict) and bool(
            val.get("every_n_turns") or val.get("on_errors") or val.get("on_stall")
        )
    return False


def _load_manager_prompt(kind: str, agent_rec: Optional[dict]) -> str:
    """Resolve the prompt for one trigger: per-agent prop → app-prompts →
    built-in fallback (never breaks on a config error)."""
    if agent_rec:
        meta = agent_rec.get("metadata")
        if isinstance(meta, str):
            try:
                meta = json.loads(meta) or {}
            except Exception:
                meta = {}
        if isinstance(meta, dict):
            try:
                from app.agent.manager_config import manager_loop_for_agent
                manager_cfg = manager_loop_for_agent({**agent_rec, "metadata": meta})
                if kind == "watchdog":
                    canonical = manager_cfg.get("watchdog", {}).get("prompt")
                else:
                    canonical = manager_cfg.get("triggers", {}).get(kind, {}).get("prompt")
                if isinstance(canonical, str) and canonical.strip():
                    return canonical.strip()
            except Exception:
                pass
            tpl = meta.get(f"manager_{kind}_prompt")
            if isinstance(tpl, str) and tpl.strip():
                return tpl.strip()
    try:
        data = json.loads(_APP_PROMPTS_PATH.read_text(encoding="utf-8"))
        entry = (data.get("app_level_prompts") or {}).get(f"manager_{kind}") or {}
        tpl = entry.get("template") or entry.get("text")
        if isinstance(tpl, str) and tpl.strip():
            return tpl
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("run manager: prompt read failed: %s", e)
    return _FALLBACK_PROMPTS.get(kind, "")


# ── Window (lean: text + tool NAMES + placeholder outputs) ───────────────────

def _collect_manager_span(
    db: Any, session_id: str,
    parent_interaction_id: Optional[str],
    final_asst_id: Optional[str],
    recent_only: bool = False,
) -> Tuple[List[str], str]:
    """Return a structured supervision packet, never a working transcript."""
    from app.agent.output_closer import _collect_span_messages
    from app.agent.verification_evidence import load_supervision_packet

    _lines, request, _msgs = _collect_span_messages(
        db, session_id, parent_interaction_id, final_asst_id,
        include_tools=False,
    )
    packet = load_supervision_packet(db, session_id, parent_interaction_id, request)
    return [json.dumps(packet, ensure_ascii=False)], request


_MANAGER_FENCE_FIELDS = (
    "turn_id", "contract_revision", "work_revision", "checkpoint_revision",
)


def _manager_packet_is_stale(captured: Dict[str, Any], latest: Dict[str, Any],
                             *, negative_feedback: bool = False) -> bool:
    """Return whether a verdict crossed a task boundary that changes its meaning.

    The durable state ``revision`` advances for every evidence observation. A
    new observation does not itself invalidate negative advisory feedback.
    Positive verdicts retain exact revision checks: a new failing observation
    must not be approved by an older review. Generation, contract, work, and a
    valid checkpoint update do change the supervised claim and must invalidate
    the verdict.
    """
    if not captured.get("state_available") or not latest.get("state_available"):
        return True
    return (any(captured.get(field) != latest.get(field)
                for field in _MANAGER_FENCE_FIELDS)
            or (not negative_feedback
                and captured.get("state_revision") != latest.get("state_revision")))


# ── Verdict parsing ──────────────────────────────────────────────────────────

def _parse_manager_verdict(text: Optional[str], kind: str) -> Optional[Dict[str, Any]]:
    """Tolerantly parse a trigger's STRICT-JSON envelope.

    Accepts exact JSON, markdown-fenced JSON, or JSON buried in prose. Validates
    the verdict against the kind's allowed set and enforces the documented
    fields: every verdict needs a reason, while actionable gate verdicts need
    feedback and actionable watchdog verdicts need a suggestion. None =
    inconclusive — callers fail OPEN (never block on garbage, which could wedge
    the run).
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
    allowed = _VERDICTS.get(kind, ())
    if verdict not in allowed:
        return None
    reason = obj.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        return None
    out: Dict[str, Any] = {
        "verdict": verdict,
        "kind": kind,
        "reason": reason.strip()[:1000],
    }
    for key in ("reason", "feedback", "suggestion"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            out[key] = val.strip()[:1000]
    if verdict in ("block", "revise") and not out.get("feedback"):
        return None
    if verdict in ("off_track", "stuck") and not out.get("suggestion"):
        return None
    return out


def _manager_checks_used(db: Any, session_id: str) -> int:
    """How many Manager self-notes the CURRENT task has already produced.

    Scoped to the span after the most recent close-out lane (system:closer /
    system:summary / system:overview), mirroring ``output_closer._audit_rounds_used``
    so the cap resets per task instead of accumulating across the session.
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
            (session_id, _MANAGER_SOURCE, session_id),
        ).fetchone()
        return int(row["c"]) if row else 0
    finally:
        conn.close()


async def _stamp_manager_check(
    db: Any, session_id: str, final_asst_id: str, kind: str, verdict: str,
    expected_turn_id: Optional[str] = None,
) -> None:
    """Record a completed (non-actionable) Manager check on the anchor row.

    Best-effort; the stamp is the diagnostics trail for approve/on_track
    verdicts (actionable ones write a visible self-note instead). Never raises.
    """
    try:
        from app.agent.run_fence import side_effects_allowed
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return
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
            meta["manager_checked_at"] = datetime.now(timezone.utc).isoformat()
            verdicts = meta.get("manager_verdicts") or []
            if not isinstance(verdicts, list):
                verdicts = []
            verdicts = [v for v in verdicts if isinstance(v, str)][-8:]
            verdicts.append(f"{kind}:{verdict}")
            meta["manager_verdicts"] = verdicts
            for attempt in range(1, _WRITE_ATTEMPTS + 1):
                try:
                    conn.execute(
                        "UPDATE interactions SET metadata = ? WHERE id = ?",
                        (json.dumps(meta), final_asst_id),
                    )
                    conn.commit()
                    return
                except Exception as e:  # noqa: BLE001
                    if "lock" not in str(e).lower():
                        raise
                    if attempt < _WRITE_ATTEMPTS:
                        time.sleep(_WRITE_BACKOFF_S * attempt)
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 — a failed stamp must never break anything
        pass


async def _inject_manager_note(
    db: Any, user_id: str, session_id: str, agent_id: Optional[str],
    channel: Optional[str], final_asst_id: Optional[str],
    kind: str, verdict: str, reason: str, feedback: str,
    expected_turn_id: Optional[str] = None,
) -> None:
    """Persist an assistant-side SELF-NOTE with the Manager's actionable
    feedback, and emit it live as a response event (the audit pattern).

    The note rides the DB tail into the agent's NEXT turn's context, so a
    blocking gate that landed just before the edit executes still corrects the
    agent's next action. Best-effort — never raises.
    """
    try:
        from app.agent.run_fence import side_effects_allowed
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return
        lines = [f"Manager {kind} review ({verdict}):"]
        if reason:
            lines.append(reason.strip()[:600])
        if feedback:
            lines.append(feedback.strip()[:1000])
        content = "\n".join(lines)
        seq = await db.next_session_seq(session_id, 1)
        turn_uid = await db.insert_interaction(
            user_id, session_id, role="assistant", content=content,
            parent_id=final_asst_id, channel=channel,
            metadata=json.dumps({
                "kind": f"manager_{kind}",
                "verdict": verdict,
                "asst_id": final_asst_id,
                "manager_reason": (reason or "")[:600],
            }),
            sender_id=agent_id, receiver_id=user_id,
            source=_MANAGER_SOURCE, session_seq=seq,
        )
        try:
            from app.api.chat import _emit_to_visualizers
            await _emit_to_visualizers(session_id, {
                "type": "response", "level": "agent",
                "content": content, "asst_id": turn_uid,
                "source": _MANAGER_SOURCE, "session_seq": seq,
            }, user_id=user_id, db_override=db)
        except Exception as _emit_err:
            logger.debug("run manager: emit failed: %s", _emit_err)
    except Exception as _note_err:  # noqa: BLE001
        logger.debug("run manager: self-note failed: %s", _note_err)


def manager_feedback_message(verdict: Optional[Dict[str, Any]]) -> str:
    """Render an actionable verdict for injection into the active loop.

    The durable ``system:manager`` row remains the cross-run source of truth;
    this compact system message lets a background verdict affect the current
    in-memory run at its next inference boundary.
    """
    if not verdict:
        return ""
    value = str(verdict.get("verdict") or "").strip().lower()
    if value not in ("block", "revise", "off_track", "stuck"):
        return ""
    kind = str(verdict.get("kind") or "watchdog").strip()
    reason = str(verdict.get("reason") or "").strip()[:600]
    feedback = str(verdict.get("feedback") or verdict.get("suggestion") or "").strip()[:1000]
    parts = [f"[MANAGER {kind.upper()} — {value.upper()}]"]
    if reason:
        parts.append(reason)
    if feedback:
        parts.append(feedback)
    parts.append("Do not repeat the blocked approach. Re-plan, change tools, or ask the user if progress requires missing information.")
    return "\n".join(parts)


def _format_manager_prompt(
    kind: str, template: str, request: str, transcript: List[str], context: str,
    agent_rec: Optional[dict] = None,
) -> str:
    """Format a manager template with the standard placeholder set, falling
    back to the built-in template on an unknown placeholder (a bad per-agent
    prompt must degrade, never crash the check)."""
    try:
        rendered = template.format(
            user_request=request or "(not available)",
            manager_transcript="\n".join(transcript) if transcript else "(no messages yet)",
            edit_context=context or "(none)",
            commit_context=context or "(none)",
            trigger=context or "periodic",
        )
    except (KeyError, IndexError, ValueError):
        rendered = _FALLBACK_PROMPTS.get(kind, _FALLBACK_PROMPTS["watchdog"]).format(
            user_request=request or "(not available)",
            manager_transcript="\n".join(transcript) if transcript else "(no messages yet)",
            edit_context=context or "(none)",
            commit_context=context or "(none)",
            trigger=context or "periodic",
        )
    # Managers review a completed/ongoing run and must not independently inject
    # mode-scoped gate guidance. The working-loop prompt owns active gates.
    return rendered


# ── LLM resolution (reuse the closer's standard-role resolver) ───────────────

async def _resolve_llm(
    user_id: str, model_override: Optional[str] = None,
    agent_rec: Optional[dict] = None,
) -> tuple:
    """(model_str, provider_str, client_or_None) on the roster's STANDARD role —
    or an explicit text-capable model from the user's entitlement-clamped roster.

    A stale/unauthorized override safely falls back to STANDARD. Credentials and
    provider are always taken from the matching roster entry, never inferred
    from the model name or environment variables.
    """
    from app.agent.output_closer import _build_client, _resolve_fast_llm
    wanted = str(model_override or "").strip()
    if wanted:
        try:
            from app.admin.settings import load_llm_capabilities_for_user
            caps = await load_llm_capabilities_for_user(user_id, agent_rec=agent_rec)
            entries = [caps.get("default") or {}, *(caps.get("racers") or [])]
            match = next((entry for entry in entries
                          if isinstance(entry, dict)
                          and entry.get("model") == wanted
                          and entry.get("enabled", True) is not False
                          and entry.get("text_capable", True) is not False
                          and entry.get("base_url")
                          and (entry.get("api_key")
                               or entry.get("provider") == "openai_subscription")), None)
            if match:
                provider = str(match.get("provider") or "")
                if provider == "openai_subscription":
                    from app.agent.provider_completion import SubscriptionResponseClient
                    return (
                        wanted, provider,
                        SubscriptionResponseClient(
                            scope=str(match.get("credential_scope") or "platform"),
                            entry_id=str(match.get("entry_id") or ""),
                            user_id=str(match.get("_credential_user_id") or user_id),
                            agent_id=str(match.get("_credential_agent_id") or ""),
                        ),
                    )
                return _build_client(
                    wanted, str(match.get("base_url") or ""),
                    str(match.get("api_key") or ""), provider,
                )
            logger.info("run manager: configured model %r is unavailable; using standard", wanted)
        except Exception as exc:
            logger.debug("run manager: configured model lookup failed: %s", exc)
    return await _resolve_fast_llm(user_id, agent_rec)


async def _resolve_effort(
    user_id: str, configured: Optional[str], model: str, provider: str,
) -> Optional[str]:
    """Entitlement/catalog-clamp a Manager reasoning hint."""
    effort = str(configured or "").strip().lower() or None
    if not effort:
        return None
    try:
        from app.entitlements.service import resolve_capabilities
        from app.admin.settings import _clamp_reasoning_effort
        effort = _clamp_reasoning_effort(effort, await resolve_capabilities(user_id))
    except Exception:
        pass
    if not effort:
        return None
    try:
        from app import model_catalog
        entry = model_catalog.lookup(model, provider or "") or {}
        if entry.get("reasoning") is False:
            return None
    except Exception:
        pass
    return effort


# ── The check ────────────────────────────────────────────────────────────────

async def run_manager_check(
    kind: str,
    *,
    user_id: str,
    session_id: str,
    agent_id: Optional[str] = None,
    agent_rec: Optional[dict] = None,
    final_asst_id: Optional[str] = None,
    parent_interaction_id: Optional[str] = None,
    db: Optional[Any] = None,
    channel: Optional[str] = None,
    execution_mode: Optional[str] = None,
    extra: str = "",
    max_checks: Optional[int] = None,
    check_index: Optional[int] = None,
    kind_max_checks: Optional[int] = None,
    kind_check_index: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    """Run ONE Manager trigger check and return its verdict (or None).

    Never raises. Steps:
      1. App-function gate (``run_manager``) — fails OFF.
      2. Overall and per-kind run caps — skips silently when either is
         exhausted.
      3. Collect the lean window; resolve the standard-role model/client.
      4. One bounded LLM call with the kind's prompt + STRICT-JSON contract.
      5. Actionable verdicts (block/revise/off_track/stuck) → persist an
         assistant-side self-note; non-actionable → stamp the anchor row.
      6. Return the parsed verdict dict (the loop uses it for blocking gates;
         async callers ignore it). Fail-open: any error → None.

    ``extra`` carries trigger-specific context (e.g. the edit being verified or
    the watchdog trigger reason) and lands in the prompt's context placeholder.
    """
    try:
        # 1. App-level on/off — checked live so the toggle takes effect
        #    immediately. Fails OFF on a read error.
        try:
            from app.abilities import app_function_enabled
            if not app_function_enabled("run_manager"):
                return None
        except Exception:
            return None

        if not user_id or not session_id or not final_asst_id:
            return None
        from app.db import get_db
        if db is None:
            db = get_db()

        from app.agent.manager_config import resolve_manager_loop
        manager_loop_cfg = resolve_manager_loop(agent_rec, execution_mode)

        # Capture the durable run generation before the slow subjective call.
        # A user Stop, replacement message, or app-wide kill invalidates this
        # one-shot before it can stamp or inject late feedback.
        from app.agent.run_fence import (
            interaction_turn_id, register_current_one_shot,
            side_effects_allowed,
        )
        expected_turn_id = interaction_turn_id(db, final_asst_id)
        register_current_one_shot(session_id, expected_turn_id)
        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            return None

        # 2. Per-run cap — count visible manager self-notes since the last
        #    close-out lane (blocking-gate verdicts that produce notes count;
        #    approve/on_track stamps are cheap and capped by turn cadence).
        resolved_cap = max(1, int(
            _DEFAULT_MAX_CHECKS if max_checks is None else max_checks
        ))
        if check_index is not None:
            if int(check_index) > resolved_cap:
                return None
        else:
            try:
                if _manager_checks_used(db, session_id) >= resolved_cap:
                    return None
            except Exception as cap_err:  # noqa: BLE001
                # Reservation indexes are authoritative when supplied. The DB
                # count is only a compatibility fallback and must not turn a
                # diagnostics failure into a disabled Manager.
                logger.debug("run manager: cap count unavailable: %s", cap_err)

        default_kind_cap = _DEFAULT_MAX_CHECKS_BY_KIND.get(kind, 1)
        resolved_kind_cap = max(1, int(
            default_kind_cap if kind_max_checks is None else kind_max_checks
        ))
        if kind_check_index is not None and int(kind_check_index) > resolved_kind_cap:
            return None

        # Project raw pending operation before any model sees it.
        from app.agent.verification_evidence import load_supervision_packet, SUPERVISION_RULES
        try:
            proposed = json.loads(extra or "{}")
        except (TypeError, ValueError):
            proposed = {}
        safe_operation = load_supervision_packet(
            db, session_id, expected_turn_id or parent_interaction_id, "", proposed,
        )["pending_operation"]
        safe_extra = json.dumps(safe_operation)
        # 3. Structured state + model resolution (once).
        transcript, request = _collect_manager_span(
            db, session_id, parent_interaction_id, final_asst_id,
            recent_only=(kind == "watchdog"),
        )
        if not transcript:
            return None
        packet = load_supervision_packet(
            db, session_id, expected_turn_id or parent_interaction_id, request, proposed,
        )
        transcript = [json.dumps(packet, ensure_ascii=False)]
        last_err: Optional[Exception] = None
        resp: Any = None
        raw = ""
        verdict: Optional[Dict[str, Any]] = None
        model_str = ""
        provider_str = ""

        # A configured subagent edit contract replaces the equivalent one-shot
        # Manager call.  It still returns the legacy verdict envelope so the
        # existing blocking/escalation/self-note machinery remains authoritative.
        if kind in {"plan_gate", "edit_gate"}:
            try:
                from app.agent.subagent_contracts import ContractSupervisor
                supervisor = ContractSupervisor(
                    db=db, user_id=user_id, session_id=session_id,
                    agent_id=agent_id or "", agent_rec=agent_rec,
                    turn_id=expected_turn_id or parent_interaction_id or final_asst_id,
                    generation=expected_turn_id or "",
                    execution_mode=execution_mode,
                )
                if await supervisor.available():
                    starter = {}
                    try:
                        from app.agent.run_scout import seeded_context_for_turn
                        starter = seeded_context_for_turn(
                            db, expected_turn_id or parent_interaction_id,
                        )
                    except Exception:
                        starter = {}
                    contract_result = await supervisor.review_edit(
                        review_kind=kind, request_text=request,
                        working_context=transcript, edit=extra or "",
                        plan=starter.get("plan") or [],
                        plan_revision=int(starter.get("revision") or 0),
                        invocation=int(kind_check_index or check_index or 1),
                    )
                    decision = contract_result.get("decision")
                    if decision == "pass":
                        verdict = {
                            "verdict": "approve",
                            "reason": contract_result.get("reason") or "Contract passed.",
                            "feedback": "",
                            "contract": contract_result,
                        }
                    elif decision in {"revise", "block"}:
                        actions = contract_result.get("corrective_actions") or []
                        verdict = {
                            "verdict": decision,
                            "reason": contract_result.get("reason") or "Contract objection.",
                            "feedback": "; ".join(str(item) for item in actions)[:4000]
                                        or "Address the contract findings before retrying.",
                            "contract": contract_result,
                        }
                    elif decision == "inconclusive":
                        # Hybrid policy: the durable check row records why the
                        # review was skipped, but infrastructure cannot deadlock
                        # the primary agent.
                        return None
            except Exception as contract_error:  # noqa: BLE001
                logger.info("run manager: subagent %s unavailable; using single call (%s)",
                            kind, contract_error)

        # 4. Default/fallback: one bounded Manager LLM call.
        if verdict is None:
            model_str, provider_str, client = await _resolve_llm(
                user_id, manager_loop_cfg.get("model"), agent_rec,
            )
            if client is None:
                return None
            manager_effort = await _resolve_effort(
                user_id, manager_loop_cfg.get("effort"), model_str, provider_str,
            )
            prompt = _load_manager_prompt(kind, agent_rec)
            formatted = _format_manager_prompt(
                kind, prompt, request, transcript, safe_extra, agent_rec,
            )
            deadline = asyncio.get_running_loop().time() + _LLM_TIMEOUT
            for attempt in range(1, _LLM_ATTEMPTS + 1):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    last_err = asyncio.TimeoutError(
                        "Manager operation exceeded its total timeout"
                    )
                    break
                try:
                    create_kwargs = {
                        "model": model_str,
                        "messages": [
                            {"role": "system", "content": SUPERVISION_RULES},
                            {"role": "user", "content": formatted},
                        ],
                        "temperature": 0.2,
                        "max_tokens": _MAX_MANAGER_TOKENS,
                    }
                    if manager_effort:
                        create_kwargs["extra_body"] = {"reasoning": {"effort": manager_effort}}
                    from app.agent.provider_completion import complete_text
                    try:
                        resp = await asyncio.wait_for(
                            complete_text(
                                client, timeout=remaining, **create_kwargs,
                            ), timeout=remaining,
                        )
                    except Exception as effort_error:
                        if "extra_body" not in create_kwargs:
                            raise
                        logger.info("run manager: effort %r rejected (%s); retrying without it",
                                    manager_effort, effort_error)
                        create_kwargs.pop("extra_body", None)
                        remaining = deadline - asyncio.get_running_loop().time()
                        if remaining <= 0:
                            raise
                        resp = await asyncio.wait_for(
                            complete_text(
                                client, timeout=remaining, **create_kwargs,
                            ), timeout=remaining,
                        )
                    raw = (resp.choices[0].message.content or "") if resp.choices else ""
                    verdict = _parse_manager_verdict(raw, kind)
                    if verdict is not None:
                        break
                except Exception as e:  # noqa: BLE001
                    last_err = e
                if attempt < _LLM_ATTEMPTS:
                    remaining = deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        break
                    await asyncio.sleep(min(
                        _LLM_RETRY_BACKOFF_S * attempt,
                        remaining,
                    ))
        if verdict is None:
            logger.debug("run manager: %s inconclusive (%s)", kind,
                         last_err or "empty/unparseable completion")
            return None

        if not await side_effects_allowed(
            db, session_id, expected_turn_id=expected_turn_id,
        ):
            logger.info("run manager: discarded stale %s verdict for %s",
                        kind, session_id[:12])
            return None

        if packet.get("state_available"):
            latest = load_supervision_packet(
                db, session_id, expected_turn_id or parent_interaction_id, request,
            )
            if _manager_packet_is_stale(
                    packet, latest, negative_feedback=verdict.get("verdict") in
                    {"block", "revise", "off_track", "stuck"}):
                logger.info("run manager: discarded stale task-state verdict")
                return None

        # 5. Actionable → self-note; otherwise → stamp.
        v = verdict.get("verdict")
        actionable = (v in ("block", "revise")) or (v in ("off_track", "stuck"))
        feedback = verdict.get("feedback") or verdict.get("suggestion", "")
        if kind == "watchdog" and actionable:
            watchdog_action = manager_loop_cfg.get("watchdog", {}).get("action", "advise")
            if watchdog_action == "observe":
                actionable = False
            elif watchdog_action == "replan":
                feedback = "Update the active plan and checklist before continuing. " + feedback
            elif watchdog_action == "verify":
                feedback = "Verify the latest work before making further changes. " + feedback
            elif watchdog_action == "pause_and_ask":
                feedback = "Pause at the next safe boundary and ask the user for direction. " + feedback
        if actionable and packet.get("state_available"):
            from app.agent.task_state import record_manager_finding, _label_key
            note = feedback or verdict.get("reason") or "Supervisor requests correction."
            existing_notes = {_label_key(item.get("message")) for item in
                              packet.get("untrusted_manager_findings", [])
                              if item.get("status") == "open"}
            if _label_key(note) in existing_notes:
                actionable = False
            elif not record_manager_finding(
                db, session_id, packet["turn_id"], packet["state_revision"],
                "revise" if v in {"off_track", "stuck"} else v, [note],
                expected_review_signature=(
                    packet.get("contract_revision", 0), packet.get("work_revision", 0),
                    packet.get("checkpoint_revision", 0),
                )):
                return None
        if actionable:
            await _inject_manager_note(
                db=db, user_id=user_id, session_id=session_id,
                agent_id=agent_id, channel=channel,
                final_asst_id=final_asst_id, kind=kind,
                verdict=v,
                reason=verdict.get("reason", ""),
                feedback=feedback,
                expected_turn_id=expected_turn_id,
            )
        else:
            await _stamp_manager_check(
                db, session_id, final_asst_id, kind, v, expected_turn_id,
            )

        # Book background usage (best-effort, like the closer).
        try:
            _u = getattr(resp, "usage", None) if resp is not None else None
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model_str, provider=provider_str,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label=f"manager:{kind}", session_id=session_id,
                    user_id=user_id, agent_id=agent_id,
                )
        except Exception:
            pass

        return verdict
    except Exception as e:  # noqa: BLE001 — the Manager must never break a run
        logger.warning("run manager: %s skipped (%s)", kind, e)
        return None

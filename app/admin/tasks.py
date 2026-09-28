"""
Admin endpoints for TASK grouping inside a chat session.

A "task" is a run of consecutive turns that belong together: one request, the
agent's plan + execution for it, and any short approval/feedback turns the user
adds along the way. The chat DB only knows two levels — the session and the turn
(a `role='user'` row plus every assistant/tool row that hangs off it). There is
NO `task_id` anywhere. This module adds the missing middle level WITHOUT a
migration, exactly like the Optimizer Runs dashboard derives runs at read time
(see app/admin/optimizer.py): we reconstruct the turns of a session and then run
an INFERENCE layer over them to decide where one task ends and the next begins.

The first pass of boundary inference is intentionally **text-only and stateless**:
a new user turn CONTINUES the current task only when it reads as a short reaction
/ approval / feedback ("yes go ahead", "thanks", "no, redo it") or an explicit
continuation phrase ("also…", "and…", "now…"). Everything else would, on the
keyword rule alone, open a new task. Timing is NOT used — the same task is very
often resumed after a long gap, so an idle gap must never split a task.

**LLM tie-breaker (the fast second pass).** The keyword rule is deliberately
high-precision, so it over-splits: a follow-up like "make that bigger" or "why
did it do that" reads as a brand-new request and gets its own task even though it
clearly belongs to the previous one. To glue those back together, whenever the
keyword rule would OPEN A NEW TASK we run one cheap, single-shot LLM call that
looks at the last few turns — the **user messages AND the agent's replies to
them** (never tool output) — plus the new message and decides SAME vs NEW. The
classifier's purpose is context-pool economy: a SAME verdict keeps the whole
task's tool results in the agent's working context, while a NEW verdict closes
the task and its tool results degrade to a placeholder in the model payload (see
``app/agent/session_history.py`` off-task hiding). SAME folds the turn into the
current task; NEW (or any failure / disabled / no-credentials) keeps the keyword
verdict. The prompt lives in ``app/defaults/app-prompts.json`` under
``app_level_prompts.task_grouping_classifier`` and the call is bounded (short
timeout, no retries) so grouping never hangs and degrades cleanly to the text
rule. Results are memoised by (recent user messages + recent agent replies + new
message) so re-opening a session is free, and the runtime off-task hiding reuses
the same memo synchronously (``refresh_task_grouping_verdicts`` /
``cached_llm_verdict``) so the agent's context pool is refined by the same
verdicts with zero LLM calls on the hot per-turn history build.

The per-session detail endpoint uses the LLM tie-breaker; the multi-session
overview stays text-only (one read fans out over many sessions — an LLM call per
boundary there would be a storm).

`_decide_boundary` (text rule) and `_llm_decide_related` (LLM tie-breaker) are
kept isolated from the turn reconstruction and the endpoints so either can evolve
on its own. NOTE: the chat-side task-frame overlay
(``ui/chat/js/chat-task-frames.js``) still mirrors the text-only rule
client-side for an instant draw, so its frames can differ from this endpoint's
LLM-refined grouping; wiring it to this endpoint is a possible follow-up.
RUNTIME consumer: the same boundary inference also runs inside
``app/agent/session_history.py`` (off-task tool-output hiding) to decide which
tool results of the session belong to closed tasks and can be replaced by a
placeholder in the model payload. It is fed the text rule
(`_decide_boundary`, kept text-only and dependency-free for the hot per-turn
history build) PLUS any memoised LLM verdicts (``cached_llm_verdict``), which
``refresh_task_grouping_verdicts`` computes asynchronously once per history
build so the sync path never awaits.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Query

from app.util.paths import app_prompts_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/admin/settings", tags=["admin"])

# app-prompts catalog (bundled default under app/defaults/, or a data/ override).
_APP_PROMPTS_PATH = app_prompts_path()


# Short reactions / approvals / feedback — these ATTACH to the prior task, even
# the negative ones ("no, redo it" is still feedback ON that task, not a new one).
_REACTION_PHRASES = {
    "yes", "yep", "yeah", "y", "ok", "okay", "k", "sure", "go", "go ahead",
    "proceed", "continue", "do it", "do that", "please do", "yes please",
    "sounds good", "looks good", "lgtm", "perfect", "great", "nice", "cool",
    "thanks", "thank you", "ty", "approved", "approve", "confirm", "confirmed",
    "make it so", "ship it", "no", "nope", "stop", "wait", "cancel", "redo",
    "try again", "fix it", "not quite", "that's wrong", "thats wrong",
}
_REACTION_PREFIXES = (
    "yes", "yep", "yeah", "ok", "okay", "sure", "go ahead", "proceed",
    "do it", "do that", "please do", "thanks", "thank you", "perfect",
    "great", "no ", "nope", "redo", "try again", "fix ", "looks good",
    "sounds good", "approved", "confirm",
)

# Connectives that signal the user is adding to the SAME task ("also do X").
# High-precision only: bare "and / then / next / now" are deliberately excluded —
# "Now write a backup script" is a NEW task, not a continuation of the last one.
_CONTINUATION_PREFIXES = (
    "also", "and also", "plus", "additionally", "what about", "how about",
    "one more", "another", "btw", "by the way", "oh and", "can you also",
    "could you also",
)

# Short references whose meaning depends on the immediately preceding turn.
# These are safer to attach deterministically than to send through the
# ambiguous NEW fallback: splitting first hides the very tool evidence needed
# to answer them. Keep this list syntactic/high-precision; genuinely standalone
# prompts such as "what does Python imply..." do not match.
_ANAPHORIC_CONTINUATION_PREFIXES = (
    "what does that", "what does this", "what did that", "what did this",
    "why did that", "why did this", "why does that", "why does this",
    "how did that", "how did this", "how does that", "how does this",
    "where did that", "where did this", "when did that", "when did this",
    "can you explain that", "can you explain this", "explain that", "explain this",
    "make that", "make this", "change that", "change this",
    "update that", "update this", "fix that", "fix this",
    "do the same", "use that", "use this",
)


# Some ``role='user'`` rows are NOT genuine user requests — they are synthetic
# messages the runtime injects to drive the agent (an orchestration spawn finishing,
# a follow-up timer, an event trigger, an optimizer kick). They must NEVER open a
# task boundary, or every background wake-up would shatter a session into one task
# per event. Detected by content marker or by source. Kept in sync with the chat
# frontend's mirror in ui/chat/js/chat-task-frames.js.
_SYNTHETIC_USER_PREFIXES = (
    "[orchestration event]",
    "[event trigger]",
    "[automation event]",
    "[scheduled event]",
)
_SYNTHETIC_USER_SOURCES = ("optimizer:trigger", "optimizer:init", "automation", "event", "scheduler", "system:audit")


def _is_synthetic_user(content: str, source: Optional[str]) -> bool:
    low = (content or "").lstrip().lower()
    if any(low.startswith(p) for p in _SYNTHETIC_USER_PREFIXES):
        return True
    src = (source or "").strip().lower()
    return bool(src) and src in _SYNTHETIC_USER_SOURCES


def _tokens_from_meta(meta_raw: Optional[str]) -> int:
    if not meta_raw:
        return 0
    try:
        md = json.loads(meta_raw)
    except Exception:
        return 0
    if not isinstance(md, dict):
        return 0
    return int(md.get("input_tokens") or 0) + int(md.get("output_tokens") or 0)


def _boundary_from_meta(meta_raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not meta_raw:
        return None
    try:
        md = json.loads(meta_raw)
    except Exception:
        return None
    value = md.get("task_boundary") if isinstance(md, dict) else None
    return value if isinstance(value, dict) else None


# ── Turn reconstruction ────────────────────────────────────────────────────────
def _load_turns(conn, session_id: str) -> List[Dict[str, Any]]:
    """Rebuild the ordered turns of a session.

    A new turn opens at every GENUINE ``role='user'`` row; assistant/tool/system
    rows attach to the open turn. System rows before the first user message (e.g.
    an optimizer:init seed) are ignored — they don't start a user turn. Synthetic
    user rows (orchestration/event/optimizer injections — see _is_synthetic_user)
    do NOT open a turn either: they attach to the open turn like an assistant row,
    so a background wake-up never splits a session into a new task.
    """
    rows = conn.execute(
        "SELECT id, role, content, tool_name, metadata, created_at, session_seq, source "
        "FROM interactions WHERE session_id = ? "
        "ORDER BY created_at ASC, COALESCE(session_seq, 999999999) ASC",
        (session_id,),
    ).fetchall()

    turns: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None
    for r in rows:
        row = dict(r)
        role = (row.get("role") or "").lower()
        content = row.get("content") or ""
        created = row.get("created_at")

        if role == "user" and not _is_synthetic_user(content, row.get("source")):
            cur = {
                "index": len(turns),
                "root_id": row.get("id"),
                "prompt": content,
                "started_at": created,
                "ended_at": created,
                "tools": [],
                "tokens": _tokens_from_meta(row.get("metadata")),
                "msg_count": 1,
                "last_assistant": "",
                "boundary_diagnostic": _boundary_from_meta(row.get("metadata")),
            }
            turns.append(cur)
            continue

        if cur is None:
            continue  # stray assistant/system before any user turn

        cur["ended_at"] = created or cur["ended_at"]
        cur["msg_count"] += 1
        cur["tokens"] += _tokens_from_meta(row.get("metadata"))
        tn = row.get("tool_name")
        if tn and tn not in cur["tools"]:
            cur["tools"].append(tn)
        if role == "assistant" and content:
            cur["last_assistant"] = content
    return turns


# ── Boundary inference (text-only; the part an LLM could later sharpen) ─────────
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _classify_user_turn(text: str) -> Dict[str, Any]:
    low = _norm(text)
    stripped = low.strip(" .!?,")
    words = len(low.split())

    is_reaction = (
        words <= 2
        or stripped in _REACTION_PHRASES
        or any(stripped == p or stripped.startswith(p) for p in _REACTION_PREFIXES)
    ) and words <= 6

    is_continuation = any(low.startswith(p) for p in _CONTINUATION_PREFIXES)
    is_anaphoric_continuation = (
        words <= 18
        and any(low.startswith(p) for p in _ANAPHORIC_CONTINUATION_PREFIXES)
    )

    return {
        "words": words,
        "is_reaction": bool(is_reaction),
        "is_continuation": bool(is_continuation),
        "is_anaphoric_continuation": bool(is_anaphoric_continuation),
    }


def _decide_boundary(curr_turn: Dict[str, Any]) -> Tuple[bool, str, Dict[str, Any]]:
    """Decide if ``curr_turn`` STARTS A NEW TASK (True) or continues (False).

    Text-only and stateless — depends solely on the wording of this user turn,
    never on timing. Returns (is_new, reason, signals); the signals dict is kept
    so the frontend can show "why" and so a future LLM classifier can be slotted
    in on the ambiguous fall-through branch without changing anything else.
    """
    c = _classify_user_turn(curr_turn.get("prompt", ""))

    # Short reaction / approval / feedback always attaches to the prior task.
    if c["is_reaction"]:
        return False, "short reaction or approval", c

    # An explicit continuation phrase ("also…", "and…") continues the task.
    if c["is_continuation"]:
        return False, "continuation phrase", c

    # A short anaphoric question/edit ("what does that imply?", "make this
    # bigger") cannot stand alone: it refers to the preceding work. Preserve
    # that task's tool evidence without waiting for an LLM tie-breaker.
    if c["is_anaphoric_continuation"]:
        return False, "anaphoric continuation", c

    # Keyword rule says NEW — but this is the ambiguous case the LLM tie-breaker
    # (`_llm_decide_related`) gets to override. Flag it so the orchestrator knows
    # an LLM second opinion is worth taking here.
    c["ambiguous"] = True
    return True, "new request", c


def boundary_diagnostic(prompt: str, *, first_turn: bool = False) -> Dict[str, Any]:
    """Return the small, JSON-safe boundary decision persisted on user rows.

    This records the deterministic decision available at send time. Ambiguous
    rows explicitly say that an async LLM verdict may later refine the effective
    grouping, which makes over-splits traceable without storing conversation
    text in metadata.
    """
    if first_turn:
        return {
            "schema_version": 1,
            "classifier": "task-text-v1",
            "is_new": True,
            "reason": "session start",
            "signals": {"first_turn": True},
            "provisional": False,
        }
    is_new, reason, signals = _decide_boundary({"prompt": prompt or ""})
    return {
        "schema_version": 1,
        "classifier": "task-text-v1",
        "is_new": bool(is_new),
        "reason": reason,
        "signals": signals,
        "provisional": bool(signals.get("ambiguous")),
    }


# ── LLM tie-breaker (the fast second pass over the ambiguous boundaries) ────────
# On by default; set TASK_GROUPING_LLM=0 to force the text-only rule everywhere.
_LLM_DEFAULT_ENABLED = os.environ.get("TASK_GROUPING_LLM", "1").strip().lower() not in ("0", "false", "no", "off")
# How many prior TURNS the classifier sees as context — each turn contributes the
# user's message AND the agent's reply to it (both are shown to the model).
_LLM_PRIOR_USER_MSGS = max(1, int(os.environ.get("TASK_GROUPING_LLM_CONTEXT", "5") or 5))
_LLM_TIMEOUT = 12.0

# Memoise verdicts by (recent user msgs, recent agent replies, new msg) so
# re-reading a session is free, we never pay twice for the same boundary, and the
# runtime off-task hiding can reuse the verdicts synchronously. Text-keyed ⇒ safe.
_llm_cache: Dict[Tuple[str, ...], bool] = {}
_LLM_CACHE_MAX = 4000

# How many ambiguous boundaries the async runtime refresh
# (`refresh_task_grouping_verdicts`) may consult the LLM for in a single history
# build. In steady state the newest boundary is the only uncached one, so this is
# purely a belt-and-braces cap for pathological sessions / provider outages.
_LLM_REFRESH_BOUND = 5

_FALLBACK_CLASSIFIER_PROMPT = (
    "You are a fast classifier that decides whether a new user message continues "
    "the SAME task as the recent conversation, or starts a NEW task.\n\n"
    "YOUR PURPOSE — context-pool optimization. Your verdict decides what the "
    "agent keeps in its working context. Everything a task's agent did — all of "
    "its tool results — stays fully available while that task is the current "
    "one. As soon as a task is closed, its tool results are removed from context "
    "and replaced by a small placeholder (the full output stays in storage and "
    "can be re-fetched by re-running the tool or searching the session "
    "transcript, but the agent no longer sees it). So: SAME keeps the recent "
    "task's tool results available — right when the new message genuinely builds "
    "on, refines, corrects, or asks about that same work; NEW drops them — right "
    "when the new message is a fresh, unrelated piece of work.\n\n"
    "You see the user's recent messages AND the agent's replies to them (the "
    "replies show what was actually done — use them to judge whether the new "
    "message refers to that work). SAME = it refers to, builds on, refines, or "
    "asks about the same thing; NEW = it switches to an unrelated subject, even "
    "right after the previous task finished. When genuinely unsure, prefer SAME: "
    "keeping a possibly-related task's results in context costs tokens but "
    "preserves correctness; splitting hides them and forces re-runs.\n\n"
    "Recent user messages (oldest first):\n"
    "{recent_user_messages}\n\n"
    "Agent replies to them (oldest first):\n"
    "{recent_assistant_messages}\n\n"
    "New user message:\n{new_message}\n\n"
    "Reply with ONE word only: SAME or NEW."
)


def _llm_prior_user_msgs() -> int:
    """How many prior TURNS the LLM tie-breaker sees as context — each turn
    contributes the user's message plus the agent's reply to it (both are shown
    to the model). LIVE read (no restart): the `llm_context` knob on the Task
    Grouping app function (App Settings ▸ App Functions) wins; the
    TASK_GROUPING_LLM_CONTEXT env var overrides it; default 5."""
    env = os.environ.get("TASK_GROUPING_LLM_CONTEXT")
    if env and env.strip():
        try:
            return max(1, int(env.strip()))
        except (TypeError, ValueError):
            pass
    try:
        from app.admin.ability_config import get_ability_config
        v = get_ability_config("task_grouping").get("llm_context")
        if v is not None:
            try:
                return max(1, int(str(v).strip() or "5"))
            except (TypeError, ValueError):
                pass
    except Exception:  # pragma: no cover - best-effort; falls back to the default
        pass
    return _LLM_PRIOR_USER_MSGS


def _load_classifier_prompt() -> str:
    """Read the task-grouping classifier template. Priority: the editable
    `classifier_prompt` knob on the Task Grouping app function (App Settings ▸
    App Functions) → the `task_grouping_classifier` template in app-prompts.json
    → a built-in fallback. Never raises — grouping survives config errors.
    """
    try:
        from app.admin.ability_config import get_ability_config
        tpl = (get_ability_config("task_grouping").get("classifier_prompt") or "").strip()
        if tpl:
            return tpl
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("task grouping: config classifier prompt unreadable: %s", e)
    try:
        data = json.loads(_APP_PROMPTS_PATH.read_text(encoding="utf-8"))
        entry = (data.get("app_level_prompts") or {}).get("task_grouping_classifier") or {}
        tpl = entry.get("template") or entry.get("text")
        if isinstance(tpl, str) and tpl.strip():
            return tpl
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("task grouping: could not read classifier prompt: %s", e)
    return _FALLBACK_CLASSIFIER_PROMPT


_CLASSIFIER_CONFIG_CACHE: Optional[tuple] = None  # (model, base_url, api_key, expiry_ts)
_CLASSIFIER_CACHE_TTL = 300  # 5 minutes


def _infer_classifier_provider(base_url: str) -> str:
    """Best-effort provider name for the classifier from its base URL — used only
    for the alert message so the user knows which account to top up."""
    b = (base_url or "").lower()
    if "openrouter" in b:
        return "openrouter"
    if "routellm" in b or "abacus" in b:
        return "abacus"
    if "deepseek" in b:
        return "deepseek"
    if "generativelanguage" in b or "googleapis" in b:
        return "google"
    if "openai" in b:
        return "openai"
    return ""


async def _classifier_config() -> tuple:
    """Resolve (model, provider, client) for the LLM tie-breaker.

    Priority chain (first-win):
      1. Dedicated CLASSIFIER_MODEL / CLASSIFIER_BASE_URL / CLASSIFIER_API_KEY env vars
      2. Multi-provider roster from DB — picks the first enabled text-capable row,
         guaranteeing the model always reaches the right endpoint.
      3. Plain env vars (LLM_MODEL / LLM_BASE_URL / LLM_API_KEY) with the
         DeepSeek-base-url + non-DeepSeek-model auto-correction.

    Returns (model_str, provider_str, AsyncOpenAI | None).  If no API key is
    available anywhere the client is None and the caller falls back to the
    keyword verdict.
    """
    global _CLASSIFIER_CONFIG_CACHE

    now = time.time()

    # --- 1. Dedicated classifier env vars ---
    model = os.environ.get("CLASSIFIER_MODEL")
    base_url = os.environ.get("CLASSIFIER_BASE_URL") or os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL")
    api_key = os.environ.get("CLASSIFIER_API_KEY") or os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY")
    if model:
        return _build_classifier(model, base_url or "", api_key or "",
                                 _infer_classifier_provider(base_url or ""))

    # --- 2. Multi-provider roster from DB ---
    if _CLASSIFIER_CONFIG_CACHE and _CLASSIFIER_CONFIG_CACHE[4] > now:
        cached_model, cached_url, cached_key, cached_provider, _ = _CLASSIFIER_CONFIG_CACHE
        return _build_classifier(cached_model, cached_url, cached_key, cached_provider)

    try:
        from app.admin.settings import _resolve_user_config as _resolve_llm_config
        cfg = await _resolve_llm_config("admin")
        roster = cfg.get("multi_providers") or []
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            if entry.get("enabled") is not False and entry.get("text_capable") is not False:
                m = entry.get("model") or ""
                u = entry.get("base_url") or ""
                k = entry.get("api_key") or ""
                if m and u and k:
                    _CLASSIFIER_CONFIG_CACHE = (m, u, k, entry.get("provider", ""),
                                                now + _CLASSIFIER_CACHE_TTL)
                    return _build_classifier(m, u, k, entry.get("provider", ""))
    except Exception as exc:
        logger.debug("classifier: multi-provider lookup failed: %s", exc)

    _CLASSIFIER_CONFIG_CACHE = ("", "", "", "", 0)  # negative cache — don't retry immediately

    # --- 3. Plain env vars (current fallback) ---
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""
    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or ""
    api_key = os.environ.get("LLM_API_KEY") or os.environ.get("OPENROUTER_API_KEY") or ""
    # DeepSeek auto-correction: if the endpoint is DeepSeek but the model name
    # has a non-DeepSeek prefix, swap to a safe default.
    if "deepseek" in base_url.lower() and model and not model.lower().startswith("deepseek"):
        model = "deepseek-v4-flash"
    if not model:
        model = ""
    return _build_classifier(model, base_url or "", api_key,
                             _infer_classifier_provider(base_url or ""))


def _build_classifier(model: str, base_url: str, api_key: str,
                      provider: str = "") -> tuple:
    """Build the (model, provider, client_or_None) tuple from resolved config.

    Extracted so the priority chain always constructs the client the same way
    regardless of which tier won. ``provider`` is carried for the user-facing
    alert message (which account is out of credits).
    """
    if not api_key:
        return (model, provider, None)
    try:
        from openai import AsyncOpenAI
        return (model, provider, AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=_LLM_TIMEOUT, max_retries=0))
    except ImportError:  # pragma: no cover
        try:
            from app.openai_compat import AsyncOpenAI
            return (model, provider, AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=_LLM_TIMEOUT))
        except Exception:
            return (model, provider, None)
    except Exception:
        return (model, provider, None)


async def _maybe_alert_402(err: Exception, user_id: str) -> None:
    """Detect provider credit/billing errors (402 Insufficient Balance or gateway
    "no remaining credits" 400s) from the task-grouping classifier and surface
    them as a visible system:error message in the user's most recent active
    session. Delegates to the shared alert utility."""
    text = str(err)
    from app.util.alerts import is_provider_credit_error
    if not is_provider_credit_error(text):
        return

    # Resolve the provider model name from the classifier config.
    model_str = ""
    provider_str = ""
    try:
        model_str, provider_str, _ = await _classifier_config()
        model_str = model_str or ""
        provider_str = provider_str or ""
    except Exception:
        pass

    # Find the most recent active session for this user.
    sid = ""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, "_get_conn", None)
        if raw:
            conn = raw()
            try:
                row = conn.execute(
                    "SELECT id FROM sessions WHERE user_id=? AND (status IS NULL OR status='active') ORDER BY updated_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if row:
                    sid = row[0]
            finally:
                conn.close()
    except Exception:
        return

    if not sid:
        return

    from app.util.alerts import persist_402_alert
    await persist_402_alert(text, user_id, sid, model_str, provider_str)


def _llm_cache_key(prior_user_msgs: List[str], prior_assistant_msgs: Optional[List[str]],
                   new_msg: str) -> Optional[Tuple[str, ...]]:
    """Build the memo key for an ambiguous boundary — (recent user messages,
    recent agent replies, new message). The exact same key is built by the
    async classifier call (`_llm_decide_related`) and the sync runtime lookup
    (`cached_llm_verdict`), so both paths always agree. Returns None when the
    boundary is not LLM-eligible (no prior context or empty new message)."""
    recent = [m.strip() for m in prior_user_msgs if m and m.strip()][-_llm_prior_user_msgs():]
    asst = [m.strip() for m in (prior_assistant_msgs or []) if m and m.strip()][-_llm_prior_user_msgs():]
    new_msg = (new_msg or "").strip()
    if not recent or not new_msg:
        return None
    return tuple(recent) + tuple(asst) + (new_msg,)


def cached_llm_verdict(prior_user_msgs: List[str], prior_assistant_msgs: Optional[List[str]],
                       new_msg: str) -> Optional[bool]:
    """Sync lookup of a previously computed LLM tie-breaker verdict — used by the
    runtime off-task hiding in ``app/agent/session_history.py`` so it can reuse
    the same refined boundaries as the admin dashboard with NO awaits on the hot
    per-turn history build. Returns True (SAME), False (NEW), or None when no
    verdict is memoised yet (caller falls back to the text rule)."""
    key = _llm_cache_key(prior_user_msgs, prior_assistant_msgs, new_msg)
    if key is None:
        return None
    return _llm_cache.get(key)


async def refresh_task_grouping_verdicts(rows) -> int:
    """Best-effort, async: run the LLM tie-breaker over a session's interaction
    rows so its verdicts land in the shared memo (`_llm_cache`) BEFORE the sync
    history build reads them back via ``cached_llm_verdict``. Only ambiguous
    boundaries (text rule says NEW) consult the LLM; verdicts are memoised by
    message text, so steady-state turns add ~0 calls (just the newest boundary).
    Bounded per build by `_LLM_REFRESH_BOUND`; any failure / disabled function /
    no credentials simply leaves the text rule in charge. Returns how many
    boundaries were (re)considered."""
    if not _LLM_DEFAULT_ENABLED:
        return 0
    try:
        from app.abilities import app_function_enabled
        if not app_function_enabled("task_grouping"):
            return 0
    except Exception:
        pass
    # Reconstruct the turn stream the same way the admin grouping and the runtime
    # hiding do: each genuine user row opens a turn; assistant text becomes the
    # turn's reply; synthetic user rows (orchestration/event wake-ups) are skipped.
    turns: List[List[str]] = []
    for r in rows:
        role = (r.role or "").lower()
        content = r.content or ""
        if role == "user":
            if not _is_synthetic_user(content, getattr(r, "source", None)):
                turns.append([content, ""])
            continue
        if role == "assistant" and content.strip() and turns:
            turns[-1][1] = content
    considered = 0
    # Newest boundaries first: the current task's boundary is the one that most
    # directly shapes the agent's context pool. Older uncached boundaries get
    # refined on later builds (or by the admin detail endpoint, which is unbounded).
    for i in range(len(turns) - 1, 0, -1):
        prompt = turns[i][0]
        is_new, _, sig = _decide_boundary({"prompt": prompt})
        if not (is_new and sig.get("ambiguous")):
            continue
        if considered >= _LLM_REFRESH_BOUND:
            break
        prior_users = [t[0] for t in turns[:i]]
        prior_asst = [t[1] for t in turns[:i]]
        if cached_llm_verdict(prior_users, prior_asst, prompt) is not None:
            continue  # already refined on an earlier build
        considered += 1
        await _llm_decide_related(prior_users, prompt, prior_assistant_msgs=prior_asst)
    return considered


async def _llm_decide_related(prior_user_msgs: List[str], new_msg: str, *,
                              prior_assistant_msgs: Optional[List[str]] = None,
                              session_id: Optional[str] = None,
                              user_id: Optional[str] = None) -> Optional[bool]:
    """Ask the fast model: does ``new_msg`` continue the same task as the recent
    conversation — the prior user messages AND the agent's replies to them (never
    tool output)? Returns True (SAME / keep in current task), False (NEW), or
    None when the call can't be made (disabled, no creds, error) so the caller
    falls back to the keyword verdict. Best-effort: never raises.
    """
    if not _LLM_DEFAULT_ENABLED:
        return None
    # App-level on/off: Task Grouping is an app_function (App Settings ▸ App
    # Functions). When an admin turns it off, skip the LLM tie-breaker entirely
    # and fall back to the keyword verdict — no model calls. Checked live so the
    # toggle takes effect immediately (no restart). Fails ON (never silently
    # disables a default-on function if the catalog/config can't be read).
    try:
        from app.abilities import app_function_enabled
        if not app_function_enabled("task_grouping"):
            return None
    except Exception:
        pass
    new_msg = (new_msg or "").strip()
    cache_key = _llm_cache_key(prior_user_msgs, prior_assistant_msgs, new_msg)
    if cache_key is None:
        return None
    if cache_key in _llm_cache:
        return _llm_cache[cache_key]

    model_str, _, client = await _classifier_config()
    if client is None:
        return None

    # Rebuild the two display blocks from the raw inputs (same slicing as the key).
    recent_users = [m.strip() for m in prior_user_msgs if m and m.strip()][-_llm_prior_user_msgs():]
    recent_asst = [m.strip() for m in (prior_assistant_msgs or []) if m and m.strip()][-_llm_prior_user_msgs():]
    user_block = "\n".join(f"- {m}" for m in recent_users)
    asst_block = "\n".join(f"- {m}" for m in recent_asst) or "(no agent replies yet)"
    prompt = _load_classifier_prompt().format(
        recent_user_messages=user_block,
        recent_assistant_messages=asst_block,
        new_message=new_msg,
    )
    try:
        resp = await client.chat.completions.create(
            model=model_str,
            messages=[{"role": "user", "content": prompt}],
            # Generous cap on purpose: the answer is one word, but the app's
            # configured model may be a REASONING model that spends hidden tokens
            # thinking first — too small a cap returns empty content (finish
            # reason "length") and the verdict would silently fall back. Only the
            # tokens actually used are billed, so this just removes a ceiling.
            max_tokens=512,
            temperature=0.0,
        )
        raw = (resp.choices[0].message.content or "").strip().upper()
        try:
            _u = getattr(resp, "usage", None)
            if _u:
                from plugins.billing.usage import record_background_usage
                await record_background_usage(
                    model=model_str,
                    input_tokens=getattr(_u, "prompt_tokens", 0) or 0,
                    output_tokens=getattr(_u, "completion_tokens", 0) or 0,
                    label="task-group",
                    session_id=session_id,
                    user_id=user_id,
                )
        except Exception:
            pass
    except Exception as e:
        logger.warning("task grouping: classifier call failed: %s", e)
        await _maybe_alert_402(e, "admin")
        return None

    if "SAME" in raw:
        verdict = True
    elif "NEW" in raw:
        verdict = False
    else:
        return None  # unparseable → fall back to keyword verdict

    if len(_llm_cache) < _LLM_CACHE_MAX:
        _llm_cache[cache_key] = verdict
    return verdict


# ── Title / excerpt helpers ────────────────────────────────────────────────────
def _title_from(text: str, limit: int = 60) -> str:
    line = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    line = re.sub(r"\s+", " ", line).strip()
    # Drop a leading politeness wrapper so the title leads with the verb/topic.
    line = re.sub(r"^(please\s+|can you\s+|could you\s+|can u\s+|i (?:want|need|would like) (?:you )?to\s+)",
                  "", line, flags=re.IGNORECASE).strip()
    if not line:
        return "(empty prompt)"
    if len(line) <= limit:
        return line
    cut = line[:limit].rsplit(" ", 1)[0]
    return (cut or line[:limit]).rstrip() + "…"


def _excerpt(text: str, limit: int = 160) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[:limit].rstrip() + "…"


# ── Grouping orchestration ─────────────────────────────────────────────────────
async def group_session_tasks(conn, session_id: str,
                        include_turns: bool = False,
                        use_llm: bool = True) -> Dict[str, Any]:
    turns = _load_turns(conn, session_id)
    tasks: List[Dict[str, Any]] = []

    # Session linkage for the task-grouping LLM tie-breaker's usage rows — the
    # classifier's tokens are real LLM calls made FOR this session, so they
    # should count toward its totals/cost when recorded.
    _sid_user: Optional[str] = None
    try:
        _srow = conn.execute(
            "SELECT user_id FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        if _srow:
            _sid_user = _srow[0]
    except Exception:
        pass

    def _new_task(turn: Dict[str, Any], reason: str) -> Dict[str, Any]:
        return {
            "index": len(tasks),
            "title": _title_from(turn.get("prompt", "")),
            "prompt": _excerpt(turn.get("prompt", ""), 280),
            "started_at": turn.get("started_at"),
            "ended_at": turn.get("ended_at"),
            "turn_count": 0,
            "turn_indices": [],
            "root_ids": [],
            "tools": [],
            "tokens": 0,
            "boundary_reason": reason,
            "last_assistant": "",
            "_turns": [],
        }

    cur_task: Optional[Dict[str, Any]] = None

    def _absorb(task: Dict[str, Any], turn: Dict[str, Any], cont_reason: str) -> None:
        task["turn_count"] += 1
        task["turn_indices"].append(turn["index"])
        if turn.get("root_id"):
            task["root_ids"].append(turn["root_id"])
        task["ended_at"] = turn.get("ended_at") or task["ended_at"]
        task["tokens"] += int(turn.get("tokens") or 0)
        for t in turn.get("tools", []):
            if t not in task["tools"]:
                task["tools"].append(t)
        if turn.get("last_assistant"):
            task["last_assistant"] = _excerpt(turn["last_assistant"])
        task["_turns"].append({
            "index": turn["index"],
            "root_id": turn.get("root_id"),
            "prompt": _excerpt(turn.get("prompt", ""), 200),
            "started_at": turn.get("started_at"),
            "tools": turn.get("tools", []),
            "tokens": int(turn.get("tokens") or 0),
            "msg_count": turn.get("msg_count", 1),
            "continued_reason": cont_reason,
            "boundary_diagnostic": turn.get("boundary_diagnostic"),
        })

    for i, turn in enumerate(turns):
        if cur_task is None:
            cur_task = _new_task(turn, "session start")
            tasks.append(cur_task)
            _absorb(cur_task, turn, "first turn")
            continue
        is_new, reason, sig = _decide_boundary(turn)

        # The keyword rule says NEW, but this is the ambiguous fall-through — give
        # the fast LLM a vote, showing it the prior user messages AND the agent's
        # replies to them (never tool output). SAME folds the turn into the
        # current task; NEW (or no verdict) keeps the keyword decision. The
        # overview view passes use_llm=False to stay text-only. Verdicts land in
        # the shared memo the runtime off-task hiding reuses (see
        # refresh_task_grouping_verdicts / cached_llm_verdict).
        if is_new and use_llm and sig.get("ambiguous"):
            prior_user_msgs = [t.get("prompt", "") for t in turns[:i]]
            prior_asst_msgs = [t.get("last_assistant", "") for t in turns[:i]]
            related = await _llm_decide_related(
                prior_user_msgs, turn.get("prompt", ""),
                prior_assistant_msgs=prior_asst_msgs,
                session_id=session_id, user_id=_sid_user)
            if related is True:
                is_new, reason = False, "llm: same task"
            elif related is False:
                reason = "llm: new task"

        if is_new:
            cur_task = _new_task(turn, reason)
            tasks.append(cur_task)
            _absorb(cur_task, turn, "first turn")
        else:
            _absorb(cur_task, turn, reason)

    if not include_turns:
        for t in tasks:
            t.pop("_turns", None)
    else:
        for t in tasks:
            t["turns"] = t.pop("_turns", [])

    return {
        "session_id": session_id,
        "task_count": len(tasks),
        "turn_count": len(turns),
        "tasks": tasks,
    }


# ── Endpoints ──────────────────────────────────────────────────────────────────
@router.get("/tasks/session/{session_id}")
async def get_session_tasks(
    session_id: str,
    include_turns: bool = Query(True, description="Include the per-turn breakdown inside each task"),
):
    """Group one session's turns into inferred tasks (derived at read time)."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, "_get_conn", None)
        if not raw:
            return {"error": "no db", "session_id": session_id, "tasks": []}
        conn = raw()
        try:
            return await group_session_tasks(conn, session_id, include_turns=include_turns, use_llm=True)
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed to group tasks for session %s: %s", session_id, e)
        return {"error": str(e), "session_id": session_id, "tasks": []}


@router.get("/tasks/overview")
async def get_tasks_overview(
    user_id: str = Query(..., description="Owner user id"),
    limit: int = Query(50, ge=1, le=200),
):
    """One row per recent session with its inferred task count — for a list view."""
    try:
        from app.db import get_db
        db = get_db()
        raw = getattr(db, "_get_conn", None)
        if not raw:
            return []
        conn = raw()
        try:
            rows = conn.execute(
                "SELECT id, title, created_at, updated_at FROM sessions "
                "WHERE user_id = ? AND (status IS NULL OR status = 'active') "
                "ORDER BY updated_at DESC NULLS LAST LIMIT ?",
                (user_id, limit),
            ).fetchall()
            out = []
            for r in rows:
                row = dict(r)
                # Text-only here: one read fans out over many sessions, so an LLM
                # call per boundary would be a storm. The detail endpoint refines.
                grouped = await group_session_tasks(conn, row["id"], include_turns=False, use_llm=False)
                out.append({
                    "session_id": row["id"],
                    "title": row.get("title") or (row["id"][:12]),
                    "created_at": row.get("created_at"),
                    "updated_at": row.get("updated_at"),
                    "task_count": grouped["task_count"],
                    "turn_count": grouped["turn_count"],
                    "tasks": grouped["tasks"],  # already turn-free
                })
            return out
        finally:
            conn.close()
    except Exception as e:
        logger.error("Failed tasks overview for %s: %s", user_id, e)
        return []

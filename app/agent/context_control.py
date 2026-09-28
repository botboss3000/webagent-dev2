"""
Context Control — per-agent compaction targets under an app-wide hard ceiling.

This is Part 1 of the Context Control ability: a live gauge of how much of the
agent's context window is in use, surfaced to the agent each user turn so it can
feel itself filling up. Later parts use the same settings/limit to decide when to
compact older turns in the background.

Storage: the app-level Context Control config owns the maximum token ceiling.
Each agent's ability connection owns its compaction target, verbatim-tail amount,
and self-compaction posture. The runtime layers those scopes and always clamps an
auto-detected model window to the app maximum.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# The ability id this feature is gated behind (must match the agent_abilities row).
ABILITY_ID = "context_control"

# The token limit (context window) the gauge + compaction measure against. By
# default this is auto-detected from the ACTIVE model's real context window (via
# the model catalog); DEFAULT_TOKEN_LIMIT is only the fallback for a model the
# catalog doesn't know, and the value an admin uses when auto-detect is turned off.
DEFAULT_TOKEN_LIMIT = 1_050_000
DEFAULT_AUTO_LIMIT = True           # auto-track the active model's window

# ── Compaction defaults (Part 2 of Context Control) ──────────────────────────
# When conversation history exceeds the absolute target, turns older than the
# verbatim hot-tail budget are combined with the previous compacted state into
# one rolling checkpoint. Its output ceiling is proportional to the target.
DEFAULT_COMPACT_TARGET_TOKENS = 100_000
DEFAULT_VERBATIM_TAIL_TOKENS = 30_000
DEFAULT_SEGMENT_TARGET_FRACTION = 0.05  # rolling checkpoint output ceiling

# Historical tool-evidence policy. ``current_task`` preserves the behaviour
# shipped before this became agent-configurable. A "run" is one genuine user
# turn plus the assistant/tool work it triggered (internal wake-ups do not count).
TOOL_EVIDENCE_POLICIES = frozenset({
    "current_task", "recent_runs", "token_budget", "hybrid", "all",
})
DEFAULT_TOOL_EVIDENCE_POLICY = "current_task"
DEFAULT_FULL_EVIDENCE_RUNS = 2
DEFAULT_FULL_EVIDENCE_TOKEN_BUDGET = 40_000

# Rough chars-per-token ratio for the pre-call estimate. The exact prompt-token
# count is only known *after* a provider call returns usage; this heuristic gives
# the agent a usable gauge at the top of each turn. A later part folds the
# provider's exact count back in for the compaction trigger.
_CHARS_PER_TOKEN = 4


def _defaults() -> Dict[str, Any]:
    return {
        "enabled": False,
        "token_limit": DEFAULT_TOKEN_LIMIT,
        "compaction_enabled": True,
        "compact_target_tokens": DEFAULT_COMPACT_TARGET_TOKENS,
        "verbatim_tail_tokens": DEFAULT_VERBATIM_TAIL_TOKENS,
        "segment_target_tokens": int(
            DEFAULT_SEGMENT_TARGET_FRACTION * DEFAULT_COMPACT_TARGET_TOKENS),
        "summary_model": "",
        "tool_evidence_policy": DEFAULT_TOOL_EVIDENCE_POLICY,
        "full_evidence_runs": DEFAULT_FULL_EVIDENCE_RUNS,
        "full_evidence_token_budget": DEFAULT_FULL_EVIDENCE_TOKEN_BUDGET,
    }


def _as_float(val: Any, fallback: float, lo: float, hi: float) -> float:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return fallback
    if f <= lo or f >= hi:
        return fallback
    return f


def _coerce_bool(val: Any, fallback: bool) -> bool:
    """Tolerant bool parse. Config panel settings arrive as strings, so a bare
    ``bool("false")`` would wrongly read as True — coerce the string forms."""
    if val is None:
        return fallback
    if isinstance(val, bool):
        return val
    if isinstance(val, (int, float)):
        return bool(val)
    if isinstance(val, str):
        return val.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return fallback


def normalize_tool_evidence_settings(values: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Return a safe, complete tool-evidence policy.

    This deliberately accepts a plain override bag so a future member-profile
    resolver can layer ``member > agent > app`` without teaching history
    assembly about membership or entitlement storage.
    """
    values = values if isinstance(values, dict) else {}
    policy = str(values.get("tool_evidence_policy") or "").strip().lower()
    if policy not in TOOL_EVIDENCE_POLICIES:
        policy = DEFAULT_TOOL_EVIDENCE_POLICY

    def _bounded_int(key: str, fallback: int, lo: int, hi: int) -> int:
        try:
            value = int(float(values.get(key, fallback)))
        except (TypeError, ValueError):
            value = fallback
        return min(hi, max(lo, value))

    return {
        "tool_evidence_policy": policy,
        "full_evidence_runs": _bounded_int(
            "full_evidence_runs", DEFAULT_FULL_EVIDENCE_RUNS, 1, 50),
        "full_evidence_token_budget": _bounded_int(
            "full_evidence_token_budget",
            DEFAULT_FULL_EVIDENCE_TOKEN_BUDGET, 1_000, DEFAULT_TOKEN_LIMIT),
    }


def apply_tool_evidence_override(
    settings: Dict[str, Any], override: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Layer a member/profile override onto resolved Context Control settings.

    No member-profile store is assumed here. When that store is introduced its
    resolver only needs to pass the permitted override bag through this seam.
    """
    out = dict(settings or {})
    if isinstance(override, dict):
        merged = {**out, **{
            key: override[key] for key in (
                "tool_evidence_policy", "full_evidence_runs",
                "full_evidence_token_budget",
            ) if key in override
        }}
    else:
        merged = out
    out.update(normalize_tool_evidence_settings(merged))
    return out


async def _resolve_model_limit(
    db: Any, agent_id: str, session_id: Optional[str], user_id: Optional[str]
) -> Optional[int]:
    """The ACTIVE model's real context window for this run, or None if unknown.

    Resolves the effective model exactly as a run does (app-default → agent →
    session) and reads its context size from the model catalog. None on any
    failure or an unknown model, so the caller falls back to the manual limit.
    """
    if not user_id:
        return None
    try:
        from app.admin.settings import resolve_active_model
        agent_rec = await db.get_agent_by_id(agent_id) if agent_id else None
        active = await resolve_active_model(user_id, agent_rec, session_id)
        model = (active or {}).get("model") or ""
        provider = (active or {}).get("provider") or ""
        if not model:
            return None
        from app import model_catalog
        entry = model_catalog.lookup(model, provider) or {}
        ctx = entry.get("context")
        return int(ctx) if ctx and int(ctx) > 0 else None
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: model-limit resolution failed: %s", e)
        return None


async def get_context_settings(
    db: Any, agent_id: str,
    session_id: Optional[str] = None, user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve Context Control settings for an agent.

    The result contains the model-aware hard limit, the conversation-history
    trigger, the verbatim-tail budget, the rolling-checkpoint output budget, the
    summary model, and tool-evidence policy. Missing configuration safely leaves
    the feature disabled.

    Enablement and stored values come from the agent's Context Control ability
    connection. Per-session target and verbatim overrides are layered afterward.
    """
    out = _defaults()
    if not agent_id:
        return out
    # Safety device: when the ability is locked-on it is always active, even if
    # no per-agent row exists yet or a row says disabled — it cannot be turned
    # off. We still read whatever settings the row carries (or fall back to
    # defaults) so the admin's tuning of the knobs is honoured.
    try:
        from app.abilities import ability_is_locked_on
        locked = ability_is_locked_on(ABILITY_ID)
    except Exception:
        locked = False
    try:
        rows = await db.get_agent_connections(agent_id)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: could not read connections for %s: %s", agent_id, e)
        rows = []
    matched = None
    for r in rows or []:
        if r.get("connection_type") != ABILITY_ID or r.get("section") != "ability":
            continue
        matched = r
        break
    if matched is None:
        # No row: on only when the ability is a locked-on safety device.
        if not locked:
            return out
        cfg = {}
    else:
        if not matched.get("enabled") and not locked:
            return out
        cfg = matched.get("config") or {}
    if isinstance(cfg, str):
        try:
            cfg = json.loads(cfg or "{}")
        except Exception:
            cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    # Settings the config panel saves live under ability_settings; fall back
    # to the bare config bag for any manually-written row.
    s = cfg.get("ability_settings")
    if not isinstance(s, dict):
        s = cfg
    agent_values = dict(s) if isinstance(s, dict) else {}
    # Layer the ADMIN app-level defaults (App Settings → App Functions) UNDER the
    # per-agent settings: an agent inherits the admin-set default for any knob it
    # hasn't explicitly chosen, and its own choices win. Mirrors how the global
    # tool-permission defaults layer beneath per-agent overrides.
    try:
        from app.admin import ability_config as _abcfg
        s = _abcfg.effective_ability_config(ABILITY_ID, s if isinstance(s, dict) else {})
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("context_control: admin-default merge skipped: %s", e)
    # App-level maximum = the stored number, also the fallback when the model is
    # unknown.  The descriptor marks this field with ceiling=max, so legacy
    # per-agent values can only make the effective limit smaller.
    manual_limit = s.get("token_limit", DEFAULT_TOKEN_LIMIT)
    try:
        manual_limit = int(manual_limit)
    except (TypeError, ValueError):
        manual_limit = DEFAULT_TOKEN_LIMIT
    if manual_limit <= 0:
        manual_limit = DEFAULT_TOKEN_LIMIT

    # Auto-detect the active model's real window unless turned off, but never let
    # it exceed the app-wide ceiling. Large provider windows therefore cannot
    # silently defeat the administrator's RAM/request-size guardrail.
    auto = _coerce_bool(s.get("auto_context_limit"), DEFAULT_AUTO_LIMIT)
    limit = manual_limit
    if auto:
        model_limit = await _resolve_model_limit(db, agent_id, session_id, user_id)
        if model_limit:
            limit = min(model_limit, manual_limit)

    def _as_int(val: Any, fallback: int, lo: int) -> int:
        try:
            i = int(float(val))
        except (TypeError, ValueError):
            return fallback
        return i if i >= lo else fallback

    # Absolute per-agent budgets. Legacy percentage/fraction keys remain readable
    # so existing agent/session rows migrate without a database rewrite; every new
    # save uses the token keys.
    target_raw = s.get("compact_target_tokens")
    legacy_target = s.get("compact_threshold")
    if ("compact_target_tokens" not in agent_values
            and agent_values.get("compact_threshold") is not None):
        legacy_target = agent_values["compact_threshold"]
        target_raw = None
    if target_raw is None and legacy_target is not None:
        target_raw = _as_float(
            legacy_target, 0.85, 0.0, 1.0) * limit
    target_tokens = min(limit, _as_int(
        target_raw, min(DEFAULT_COMPACT_TARGET_TOKENS, limit), 1_000))

    verbatim_raw = s.get("verbatim_tail_tokens")
    legacy_verbatim = s.get("tail_fraction")
    if ("verbatim_tail_tokens" not in agent_values
            and agent_values.get("tail_fraction") is not None):
        legacy_verbatim = agent_values["tail_fraction"]
        verbatim_raw = None
    if verbatim_raw is None and legacy_verbatim is not None:
        verbatim_raw = _as_float(
            legacy_verbatim, 0.30, 0.0, 1.0) * limit
    verbatim_tokens = _as_int(
        verbatim_raw, min(DEFAULT_VERBATIM_TAIL_TOKENS, target_tokens), 1_000)
    verbatim_tokens = min(verbatim_tokens, max(1_000, target_tokens - 1_000))

    # The one checkpoint is proportional to the conversation-history trigger.
    seg_target_frac = _as_float(
        s.get("segment_target_fraction"), DEFAULT_SEGMENT_TARGET_FRACTION, 0.0, 1.0)

    resolved = _defaults()
    resolved.update({
        "enabled": True,
        "token_limit": limit,
        "auto_context_limit": auto,
        # Auto-compaction is forced ON for a locked-on safety device: the whole
        # point of the lock is that runaway context can never be left ungoverned,
        # so any stored "off" choice is overridden (the config panel no longer
        # offers the toggle). Only a non-locked install can still opt out.
        "compaction_enabled": True if locked else _coerce_bool(s.get("compaction_enabled"), True),
        "compact_target_tokens": target_tokens,
        "verbatim_tail_tokens": verbatim_tokens,
        "segment_target_tokens": max(200, int(seg_target_frac * target_tokens)),
        "summary_model": str(s.get("summary_model") or ""),
        **normalize_tool_evidence_settings(s),
    })

    # Per-session override (saved from the chat footer's compaction panel) wins over
    # the agent's stored knobs for THIS one conversation — mirroring how the
    # per-session model override layers over the agent/app default. Only the two
    # user-facing token budgets can be tuned per-chat; everything else stays agent-wide.
    if session_id:
        try:
            ov = await db.get_session_context_override(session_id)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("context_control: session override read skipped: %s", e)
            ov = None
        if isinstance(ov, dict):
            if ov.get("compact_target_tokens") is not None:
                resolved["compact_target_tokens"] = min(limit, _as_int(
                    ov.get("compact_target_tokens"),
                    resolved["compact_target_tokens"], 1_000))
            elif ov.get("compact_threshold") is not None:  # legacy fraction
                resolved["compact_target_tokens"] = min(limit, int(
                    _as_float(ov.get("compact_threshold"), 0.85, 0.0, 1.0) * limit))
            if ov.get("verbatim_tail_tokens") is not None:
                resolved["verbatim_tail_tokens"] = _as_int(
                    ov.get("verbatim_tail_tokens"),
                    resolved["verbatim_tail_tokens"], 1_000)
            elif ov.get("tail_fraction") is not None:  # legacy fraction
                resolved["verbatim_tail_tokens"] = int(
                    _as_float(ov.get("tail_fraction"), 0.30, 0.0, 1.0) * limit)
            resolved["verbatim_tail_tokens"] = min(
                resolved["verbatim_tail_tokens"],
                max(1_000, resolved["compact_target_tokens"] - 1_000),
            )

    return resolved


def _json_chars(value: Any) -> int:
    """Best-effort serialized size for provider payload fields."""
    if value in (None, "", [], {}):
        return 0
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str))
    except Exception:
        return len(str(value))


def _reasoning_chars(message: Dict[str, Any]) -> int:
    """Return replayed thinking carried outside ordinary assistant content."""
    return _json_chars(message.get("reasoning_content")) + _json_chars(
        message.get("_responses_reasoning"))


def estimate_tokens(messages: List[Dict[str, Any]]) -> int:
    """Best-effort token count of an assembled OpenAI-style message list.

    Counts message content, tool calls, and provider-required replayed reasoning.
    DeepSeek stores thinking in ``reasoning_content`` rather than ``content``;
    omitting it made context control materially undercount long reasoning chats.
    """
    total_chars = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total_chars += len(c)
        elif c is not None:
            total_chars += len(str(c))
        total_chars += _json_chars(m.get("tool_calls"))
        total_chars += _reasoning_chars(m)
    return max(0, total_chars // _CHARS_PER_TOKEN)


def _message_token_parts(message: Dict[str, Any]) -> tuple[int, int]:
    """Return ordinary and replayed-reasoning token estimates for one message."""
    reasoning = _reasoning_chars(message) // _CHARS_PER_TOKEN
    total = estimate_tokens([message])
    return max(0, total - reasoning), reasoning


def context_composition(
    messages: List[Dict[str, Any]],
    *,
    tool_schema_tokens: int = 0,
    verbatim_tail_tokens: int = 0,
) -> Dict[str, Any]:
    """Split an assembled context into cache- and compaction-relevant segments.

    ``prompt_tokens`` is the stable base prompt/tool surface. ``runtime_state``
    isolates injected task state. ``reasoning_tokens`` makes replayed provider
    thinking visible rather than silently disappearing from the budget. The
    remaining history is split between compactable text and the verbatim tail.
    Legacy ``prompt_tokens`` remains for callers predating the richer panel.
    """
    base_prompt, runtime_state, history = [], [], []
    for message in messages or []:
        if message.get("role") == "system" and message.get("name") != "webagent_history_summary":
            (runtime_state if message.get("name") == "webagent_task_state" else base_prompt).append(message)
        else:
            history.append(message)

    base_prompt_tokens = estimate_tokens(base_prompt) + 4 * len(base_prompt) + max(0, int(tool_schema_tokens or 0))
    runtime_state_tokens = estimate_tokens(runtime_state) + 4 * len(runtime_state)
    tail_budget = max(0, int(verbatim_tail_tokens or 0))
    tail_indices: set[int] = set()
    used = 0
    for index in range(len(history) - 1, -1, -1):
        ordinary, reasoning = _message_token_parts(history[index])
        cost = ordinary + reasoning + 4
        if used and used + cost > tail_budget:
            break
        tail_indices.add(index)
        used += cost

    reasoning_tokens = 0
    verbatim_tokens = 0
    compactable_tokens = 0
    for index, message in enumerate(history):
        ordinary, reasoning = _message_token_parts(message)
        reasoning_tokens += reasoning
        if index in tail_indices:
            verbatim_tokens += ordinary + 4
        else:
            compactable_tokens += ordinary + 4
    history_tokens = reasoning_tokens + verbatim_tokens + compactable_tokens
    return {
        "prompt_tokens": base_prompt_tokens,
        "base_prompt_tokens": base_prompt_tokens,
        "runtime_state_tokens": runtime_state_tokens,
        "reasoning_tokens": reasoning_tokens,
        "compactable_tokens": compactable_tokens,
        "verbatim_tokens": verbatim_tokens,
        "history_tokens": history_tokens,
        "estimate_total": base_prompt_tokens + runtime_state_tokens + history_tokens,
        "message_count": len(messages or []),
    }


def context_pct(tokens: int, limit: int) -> int:
    """Percentage of the limit in use, clamped to 0..100+ (can exceed 100)."""
    if limit <= 0:
        return 0
    return int(round(100 * tokens / limit))


def status_line(
    tokens: int, limit: int,
    compact_target_tokens: int = DEFAULT_COMPACT_TARGET_TOKENS,
) -> str:
    """The block injected into the system prompt so the agent sees its own fill."""
    p = context_pct(tokens, limit)
    return (
        "# [CONTEXT]\n"
        f"Approximate context usage: {tokens:,} / {limit:,} tokens (~{p}% full). "
        f"At the configured {compact_target_tokens:,}-token compaction target, older parts of this "
        "conversation are automatically "
        "summarized in the background to free space. Nothing is ever deleted - if "
        "you need a detail from earlier that is no longer visible above, search "
        "your past messages to recall it."
    )


class ContextBudgetExceeded(ValueError):
    """Essential task state cannot fit; replan/split instead of truncating it."""


def history_budget(
    settings: Dict[str, Any], *, mandatory_tokens: int = 0,
    instruction_tokens: int = 0, tool_schema_tokens: int = 0,
    response_tokens: Optional[int] = None,
) -> int:
    """Allocate one estimated request budget, reserving non-history inputs first.

    Callers with provider token counts should pass them. Character estimates are
    conservative bookkeeping, not a guarantee of a provider's tokenization.
    """
    limit = max(1, int(settings.get("token_limit") or DEFAULT_TOKEN_LIMIT))
    response = max(0, int(response_tokens if response_tokens is not None else min(4096, max(256, limit // 10))))
    reserved = max(0, mandatory_tokens) + max(0, instruction_tokens) + max(0, tool_schema_tokens) + response
    if reserved >= limit:
        raise ContextBudgetExceeded("Required task state and request overhead exceed the context budget; split the active step or increase its budget.")
    return min(max(1, int(settings.get("compact_target_tokens") or limit)), limit - reserved)


def bound_history(messages: List[Dict[str, Any]], budget_tokens: int) -> List[Dict[str, Any]]:
    """Select whole tool exchanges, reserving durable state and latest request.

    This is a final pre-call bound for history after instructions, schemas, and
    response allowance have been accounted for by ``history_budget``. It never
    mutates persisted messages or truncates mandatory fields.
    """
    budget = max(0, int(budget_tokens))
    groups: List[List[Dict[str, Any]]] = []
    for message in messages:
        if message.get("role") == "tool" and groups and (
            groups[-1][0].get("tool_calls")):
            groups[-1].append(message)
        else:
            groups.append([message])
    newest_user = next((i for i in range(len(groups) - 1, -1, -1)
                        if groups[i][0].get("role") == "user"), None)
    selected = {i for i, group in enumerate(groups)
                if group[0].get("name") == "webagent_task_state" or i == newest_user}
    cost = sum(estimate_tokens(groups[i]) + 4 * len(groups[i]) for i in selected)
    if cost > budget:
        raise ContextBudgetExceeded("Required task state and latest user request exceed the history budget; split the active step or increase its budget.")
    for i in range(len(groups) - 1, -1, -1):
        if i in selected:
            continue
        group_cost = estimate_tokens(groups[i]) + 4 * len(groups[i])
        if cost + group_cost <= budget:
            selected.add(i)
            cost += group_cost
    return [message for i in sorted(selected) for message in groups[i]]

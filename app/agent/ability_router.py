"""
Semantic router — rank abilities / agents by relevance to the current message.

Now that embeddings are in-process (local, free, no network hop), we can cheaply
match what the user just asked against short "fingerprint" texts (name +
description) of things the agent *could* reach but hasn't yet, and surface the
right ones proactively. This powers three consumers:

  - **Hint** — the ``# [ABILITIES]`` menu is ordered by relevance and the top
    matches are starred, so the agent sees the ability it needs first instead of
    scanning an alphabetical list.
  - **Auto-reveal** — the single strongest match (above a high bar) is loaded
    automatically, so the agent skips the notice → ``load_ability`` → act
    round-trip entirely.
  - **Agent suggestions** — which of the user's saved specialist agents best fits
    this request, surfaced to an orchestrator without it having to go looking.

Everything here is best-effort. If the local embedder is unavailable, or a fact
fails to embed, ranking returns empty / unranked and every caller falls back to
its existing behaviour (alphabetical menu, no auto-reveal, pull-based agent
discovery). Routing is an optimisation, never a gate on anything working.
"""

from __future__ import annotations

import logging
import math
import os
from typing import Dict, List, Optional

logger = logging.getLogger("webagent.ability_router")

# Auto-reveal needs BOTH a high absolute score AND a clear margin over the
# runner-up — because short ability descriptions embed into a narrow cosine band,
# an absolute threshold alone happily picks noise (e.g. "salesforce" for a deploy
# query). Requiring a decisive winner is what stops a wrong auto-load. Calibrated
# on bge-small with the query-prefix recipe below: real matches land ≥0.66 with a
# ≥0.08 gap; ambiguous ones don't. Both tunable without a code change.
AUTO_REVEAL_THRESHOLD = float(os.environ.get("ABILITY_AUTOREVEAL_THRESHOLD") or 0.65)
AUTO_REVEAL_MARGIN = float(os.environ.get("ABILITY_AUTOREVEAL_MARGIN") or 0.08)

# Cosine bar below which a candidate isn't worth surfacing as "relevant" for this
# message. Entries under this are still listed in the menu, just not starred.
HINT_FLOOR = float(os.environ.get("ABILITY_HINT_FLOOR") or 0.50)

# How many top matches to star as "likely relevant right now" in the menu.
HINT_TOP_N = int(os.environ.get("ABILITY_HINT_TOP_N") or 4)

# bge-family models are trained for ASYMMETRIC retrieval: the query (the user's
# message) must carry this instruction prefix, while passages (the ability/agent
# fingerprints) are embedded bare. Adding it measurably widens the gap between the
# right match and the noise. Only applied for bge models — other models (MiniLM,
# cloud) are embedded without it.
_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "


def _use_query_prefix() -> bool:
    try:
        from app.agent.embed import embed_model_name
        return "bge" in (embed_model_name() or "").lower()
    except Exception:
        return False

# Fingerprint-vector cache: {fingerprint_text: embedding}. Abilities/agents are
# few and their descriptions are static, so we embed each once and reuse it every
# turn. Bounded by the number of distinct fingerprints (dozens), so no eviction.
_fp_cache: Dict[str, List[float]] = {}


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb) + 1e-12)


def _fingerprint(item: dict) -> str:
    """The text we embed for a candidate: name + one-line description + a few of
    its tool names. The name is the label the user might echo, the description is
    what it does, and the tool names add concrete verbs ("send_message",
    "deploy_server") that sharpen the match — measurably widening the gap between
    the right ability and the noise."""
    name = (item.get("name") or item.get("id") or "").strip()
    desc = (item.get("desc") or item.get("description") or "").strip().split("\n")[0]
    fp = f"{name}. {desc}" if (name and desc) else (name or desc)
    tools = item.get("tools") or []
    if isinstance(tools, list) and tools:
        fp = f"{fp} Tools: " + ", ".join(str(t) for t in tools[:8])
    return fp.strip()


async def _embed_cached(text: str, *, is_query: bool = False) -> Optional[List[float]]:
    text = (text or "").strip()
    if not text:
        return None
    # The user's message is a QUERY; ability/agent fingerprints are PASSAGES. For
    # bge models the query carries an instruction prefix (see _QUERY_PREFIX).
    key = (_QUERY_PREFIX + text) if (is_query and _use_query_prefix()) else text
    hit = _fp_cache.get(key)
    if hit is not None:
        return hit
    try:
        from app.agent.embed import embed_text
        vec = await embed_text(key)
    except Exception as e:
        logger.debug("ability_router: embed failed (%s) — ranking disabled this call", e)
        return None
    if vec:
        _fp_cache[key] = vec
    return vec


async def rank(message: str, items: List[dict]) -> List[dict]:
    """Return ``items`` copied with a ``score`` (cosine to the message), sorted
    most-relevant first. Empty list if there's nothing to rank or the embedder is
    unavailable — callers then keep their existing order.

    Each item is a ``{"id", "name", "desc"}`` dict (abilities or agents share the
    shape). Items that can't be fingerprinted or embedded get dropped from the
    ranking (the caller still has them in its own list).
    """
    message = (message or "").strip()
    if not message or not items:
        return []
    qv = await _embed_cached(message, is_query=True)
    if not qv:
        return []
    scored: List[dict] = []
    for it in items:
        fp = _fingerprint(it)
        if not fp:
            continue
        cv = await _embed_cached(fp)
        if not cv:
            continue
        out = dict(it)
        out["score"] = _cosine(qv, cv)
        scored.append(out)
    scored.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return scored


async def route_abilities(message: str, candidates: List[dict]) -> dict:
    """One call that produces everything the [ABILITIES] menu + auto-reveal need.

    ``candidates`` are the discoverable-and-unloaded abilities
    (``{"id","name","desc"}``). Returns::

        {
          "ranked":   [candidate, ... most-relevant-first, each with "score"],
          "starred":  [ability_id, ...]   # top HINT_TOP_N above HINT_FLOOR
          "reveal":   ability_id | None    # single top match >= AUTO_REVEAL_THRESHOLD
          "reveal_score": float | None,
        }

    All fields degrade safely: empty ``ranked`` (embedder down) → no starring, no
    reveal, and the caller renders its plain alphabetical menu.
    """
    ranked = await rank(message, candidates)
    if not ranked:
        return {"ranked": [], "starred": [], "reveal": None, "reveal_score": None}

    starred = [r["id"] for r in ranked[:HINT_TOP_N]
               if r.get("id") and r.get("score", 0.0) >= HINT_FLOOR]

    # Auto-reveal only a DECISIVE winner: high score AND a clear gap to the
    # runner-up. The margin is what rejects the "everything scores ~0.6" case
    # where the top match is really just noise.
    top = ranked[0]
    second_score = ranked[1].get("score", 0.0) if len(ranked) > 1 else 0.0
    reveal = None
    reveal_score = None
    if (top.get("id")
            and top.get("score", 0.0) >= AUTO_REVEAL_THRESHOLD
            and (top.get("score", 0.0) - second_score) >= AUTO_REVEAL_MARGIN):
        reveal = top["id"]
        reveal_score = top["score"]

    return {
        "ranked": ranked,
        "starred": starred,
        "reveal": reveal,
        "reveal_score": reveal_score,
    }


# Bar for surfacing a delegatable agent as a strong match in the suggestion hint,
# and how many to list. A specialist whose trigger description matches the request
# should clear this comfortably; unrelated agents shouldn't appear at all.
AGENT_SUGGEST_FLOOR = float(os.environ.get("AGENT_SUGGEST_FLOOR") or 0.55)
AGENT_SUGGEST_TOP_N = int(os.environ.get("AGENT_SUGGEST_TOP_N") or 3)


async def suggest_agents(message: str, templates: List[dict], *,
                         exclude_id: Optional[str] = None) -> str:
    """Rank the user's delegatable agent templates against the message and return
    a rendered ``# [SUGGESTED AGENTS]`` hint block — or ``""`` when none clear the
    bar (or the embedder is off). This is the PUSH counterpart to the pull-only
    ``list_delegatable_agents`` tool: instead of the orchestrator having to go
    looking, the specialist that fits *this* request is surfaced up front.

    ``templates`` are ``{"id","name","desc"}`` dicts (desc = trigger description).
    ``exclude_id`` drops the current agent so it never suggests itself.
    """
    cands = [t for t in (templates or []) if t.get("id") and t.get("id") != exclude_id]
    if not cands:
        return ""
    ranked = await rank(message, cands)
    picks = [r for r in ranked[:AGENT_SUGGEST_TOP_N]
             if r.get("score", 0.0) >= AGENT_SUGGEST_FLOOR]
    if not picks:
        return ""
    lines = [
        "# [SUGGESTED AGENTS]",
        "For this request, these saved specialist agents look like strong matches. "
        "Consider handing the work off — `list_delegatable_agents` for the full "
        "roster, `delegate_task_to_agent` to give one a discrete task, or "
        "`delegate_to_agent` to hand over the whole chat.",
    ]
    for r in picks:
        nm = r.get("name") or r["id"]
        d = (r.get("desc") or "").strip().split("\n")[0]
        lines.append(f"- `{r['id']}` — {nm}: {d}" if d else f"- `{r['id']}` — {nm}")
    return "\n".join(lines)


def message_text(user_message) -> str:
    """Coerce a turn's ``user_message`` (a plain string, or a multimodal content
    list of ``{type, text|...}`` parts) down to plain text for ranking. Non-text
    parts (images) are ignored. Never raises."""
    try:
        if isinstance(user_message, str):
            return user_message
        if isinstance(user_message, list):
            parts = []
            for p in user_message:
                if isinstance(p, dict):
                    t = p.get("text") or p.get("content")
                    if isinstance(t, str) and t.strip():
                        parts.append(t)
                elif isinstance(p, str):
                    parts.append(p)
            return " ".join(parts)
        return str(user_message or "")
    except Exception:
        return ""

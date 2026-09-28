"""Context Control ability — SELF-CONTAINED drop-in (default context strategy).

Context Control is a *context-management strategy*: it decides how the stored
transcript is shaped into the message list sent to the model, and surfaces a
"how full is my context" gauge each turn. At the configured target it folds the
oldest turns into a rolling summary (compaction). Nothing is ever deleted — the raw
turns stay in the ``interactions`` table and remain searchable.

This file is the **plugin shell**: it exposes the *context-strategy contract*
below (the gauge + automatic background compaction) and adds ONE agent-facing
tool, ``compact_context``, for *deliberate* self-compaction. The actual engine
still lives in core agent infrastructure (``app/agent/context_control.py`` and
``app/agent/compaction.py``) because the loop and history assembly call it on
every turn — this module simply delegates to it, making Context Control the
default implementation behind a generic seam.

``compact_context`` lets the agent fold its own older turns into the rolling
summary on demand — meant for the rare checkpoint in a big, multi-step project
where it has just finished a self-contained phase and wants to free room before
the next one. It forces a compaction *now* (past the automatic threshold, and
even if background auto-compaction is switched off), but keeps the same safe
verbatim "hot tail" and boundary rules, so nothing is lost — cuts may use a user
turn or a completed plain-assistant milestone, but never a streaming message or
tool-call transaction. The raw turns stay in the ``interactions`` table and remain
searchable. The companion
``context_control.skill.md`` carries the "when is this worth it" judgement and
how to search back through memory for any detail that scrolled out of view.

Why a contract instead of a hard-wired import: context strategies are swappable
and **pick-one**. Drop in a different ability that also sets ``CONTEXT_STRATEGY``
(e.g. a sliding-window or retrieval-recall strategy), enable it *instead* of this
one, and the loop/history use it with no core edits. The resolver lives in
``app/abilities/__init__.py`` (``context_strategy_for_agent``), mirroring how
``turn_hooks_for_agent`` dispatches ``TURN_HOOK`` abilities.

Contract a context-strategy runtime may expose (all optional except the marker):
  * ``CONTEXT_STRATEGY = True`` — marks this module as a context strategy.
  * ``async CONTEXT_SETTINGS(db, agent_id, session_id=None, user_id=None) -> dict``
    — resolve settings for the agent, including an ``enabled`` flag. Returns
    ``{"enabled": False, ...}`` when the ability is off. ``session_id``/``user_id``
    let it size ``token_limit`` to the run's active model (else it falls back to
    the stored limit).
  * ``async CONTEXT_COMPACT(db, user_id, session_id, settings) -> dict | None`` —
    the write side: persist any reshaping of the stored history (here: fold old
    turns into the rolling summary). No-op / ``None`` when nothing changed.
  * ``CONTEXT_STATUS(messages, settings) -> dict | None`` — the gauge: returns
    ``{"line", "tokens", "limit", "pct"}`` to inject into the system prompt and
    emit to the UI, or ``None`` to inject nothing.
  * ``async CONTEXT_ASSEMBLE(db, user_id, session_id, rows, settings) -> list | None``
    — OPTIONAL full override of history assembly. Return the message list to send,
    or ``None`` to fall through to the default assembly (summary marker + tail).
    This default strategy does NOT implement it: its compaction persists a
    ``session_summaries`` marker that the default assembly already understands, so
    no override is needed. A window/retrieval strategy would implement this.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Marker the resolver checks (mirrors the TURN_HOOK attribute pattern).
CONTEXT_STRATEGY = True


async def CONTEXT_SETTINGS(
    db: Any, agent_id: str,
    session_id: Optional[str] = None, user_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Resolve Context Control settings (fill gauge + compaction) for an agent.

    ``session_id``/``user_id`` (when provided) let the limit auto-track the run's
    active model; omitted, it falls back to the stored token limit."""
    from app.agent.context_control import get_context_settings
    return await get_context_settings(db, agent_id, session_id, user_id)


async def CONTEXT_COMPACT(
    db: Any, user_id: str, session_id: str, settings: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Write side: fold older turns into the rolling summary when over threshold."""
    from app.agent.compaction import maybe_compact, persist_compaction_success
    info = await maybe_compact(db, user_id, session_id, settings)
    if info and not info.get("status"):
        await persist_compaction_success(
            db, user_id, session_id, info,
            trigger=str(settings.get("_compaction_trigger") or "automatic"),
        )
    return info


def CONTEXT_STATUS(
    messages: List[Dict[str, Any]], settings: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Gauge: estimate fill and return the status line + numbers, or None if off."""
    if not settings.get("enabled"):
        return None
    from app.agent.context_control import estimate_tokens, status_line, context_pct
    limit = int(settings.get("token_limit") or 0)
    if limit <= 0:
        return None
    tokens = estimate_tokens(messages)
    return {
        "line": status_line(
            tokens, limit,
            int(settings.get("compact_target_tokens") or 100_000),
        ),
        "tokens": tokens,
        "limit": limit,
        "pct": context_pct(tokens, limit),
    }


# ── Agent-facing tool: deliberate self-compaction ────────────────────────────
# The loader reads these AFTER build_tools is called, so build_tools fills them.
TOOL_SCHEMAS: Dict[str, Any] = {}
DESTRUCTIVE: set = set()  # compaction deletes nothing (raw turns stay) → no gate


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **_ctx):
    """Expose ``compact_context`` — the agent's deliberate self-compaction tool.

    A thin, self-contained wrapper over the same engine the background gauge and
    auto-compaction use: it resolves this agent's Context Control settings and
    runs a *forced* compaction for the current session. Heavy imports stay lazy.
    """

    async def compact_context() -> str:
        """Fold the older part of THIS conversation into a short rolling summary
        now, freeing context space, while keeping the most recent turns word-for-
        word. Nothing is deleted — every earlier turn stays stored and you can
        still find it later with session_search.

        Use this RARELY and deliberately: only in a long, multi-step project, at a
        natural checkpoint where you have just finished a self-contained phase and
        the early back-and-forth (now settled) is taking up room you need for the
        next phase. Do NOT call it mid-task, on a short chat, or just because the
        context gauge ticked up — automatic compaction already handles routine
        fill near the limit. The fold takes effect on your next turn.
        """
        if not (user_id and session_id):
            return json.dumps({
                "status": "error",
                "error": "compact_context is only available inside an active chat session.",
            })
        try:
            from app.db import get_db
            from app.agent.context_control import get_context_settings
            from app.agent.compaction import maybe_compact, persist_compaction_success

            db = get_db()
            settings = await get_context_settings(db, agent_id, session_id, user_id)
            if not settings.get("enabled"):
                return json.dumps({
                    "status": "skipped",
                    "message": "Context Control is not active for this agent, so there is nothing to compact.",
                })
            info = await maybe_compact(db, user_id, session_id, settings, force=True)
        except Exception as e:  # failure-safe: never break the turn
            logger.warning("compact_context failed: %s", e)
            return json.dumps({"status": "error", "error": f"compaction failed: {e}"})

        if not info:
            return json.dumps({
                "status": "noop",
                "message": (
                    "Nothing to compact right now — the conversation is already "
                    "short enough that everything fits in the verbatim recent tail. "
                    "No summary was created."
                ),
            })
        if info.get("status") == "failed":
            return json.dumps({
                "status": "error",
                "error": info.get("reason") or (
                    "The configured System roster model could not create a summary."
                ),
                "tokens_before": info.get("tokens_before"),
                "limit": info.get("limit"),
            })
        if info.get("status") == "in_progress":
            return json.dumps({
                "status": "in_progress",
                "message": info.get("reason") or (
                    "A compaction pass is already running for this session."
                ),
            })
        # Announce a tool-triggered fold just like a manual /compact. The system
        # row is visible in chat but excluded from the model's reconstructed history.
        await persist_compaction_success(
            db, user_id, session_id, info, trigger="tool",
        )

        # The rolling checkpoint changed — drop the prewarmed prep bundle so
        # the next turn rebuilds history compaction-aware. A stale pre-compaction
        # bundle would otherwise be consumed within its TTL, negating the fold.
        try:
            from app.agent.turn_prewarm import invalidate as _invalidate_prewarm
            _invalidate_prewarm(session_id)
        except Exception:
            pass
        return json.dumps({
            "status": "ok",
            "message": (
                "Folded the older turns of this conversation into the rolling "
                "summary. The most recent turns are kept word-for-word; everything "
                "earlier is now a summary and stays searchable via session_search. "
                "This takes effect on your next turn."
            ),
            "summarised_turns": info.get("summarised_rows"),
            "tokens_before": info.get("tokens_before"),
            "limit": info.get("limit"),
        }, default=str)

    # ── Retrieval: pull compacted detail back from the raw transcript ─────────
    # When a fact the agent needs has aged into the checkpoint, these let it
    # recover the original wording instead of guessing. The raw turns were never
    # deleted — recall_compacted reads an exact span verbatim; search_this_session
    # keyword-finds where a detail lives so it can then be recalled.
    _RECALL_MAX_MSGS = 40
    _RECALL_MSG_CHARS = 2000
    _DIRECT_RECALL_DEFAULT_CHARS = 8000
    _DIRECT_RECALL_MAX_CHARS = 20000
    _SNIPPET_PAD = 240

    def _render_rows(span, *, start_n: int):
        out = []
        for off, r in enumerate(span):
            content = (getattr(r, "content", "") or "")
            if len(content) > _RECALL_MSG_CHARS:
                content = content[:_RECALL_MSG_CHARS] + " …[truncated — narrow the range to see more]"
            out.append({
                "n": start_n + off,
                "interaction_id": getattr(r, "id", None),
                "role": getattr(r, "role", "?"),
                "tool": getattr(r, "tool_name", None),
                "tool_call_id": getattr(r, "tool_call_id", None),
                "content": content,
            })
        return out

    async def recall_compacted(
        segment: int = 0, start: int = 0, end: int = 0,
        interaction_id: str = "", tool_call_id: str = "",
        offset: int = 0, max_chars: int = _DIRECT_RECALL_DEFAULT_CHARS,
    ) -> str:
        """Read the ORIGINAL messages behind a compacted summary, verbatim.

        Use this when a detail you need has aged into one of the "EARLIER
        CONVERSATION — COMPACTED CHECKPOINT" and the summary omits it. Pass
        segment=1 for the rolling checkpoint (or the displayed part number on a
        legacy session) to get the exact turns it stands in for, or pass start/end
        precise window. Nothing was ever deleted — this reads the stored transcript.
        Large spans are returned a page at a time. A reduced historical tool
        message carries an ``interaction_id``; pass it here for an indexed,
        character-paginated read. For a reduced assistant tool input, also pass
        its ``tool_call_id`` to recover that call's original arguments.
        """
        if not (user_id and session_id):
            return json.dumps({"status": "error",
                               "error": "recall_compacted only works inside an active chat session."})
        try:
            from app.db import get_db
            db = get_db()
        except Exception as e:
            return json.dumps({"status": "error", "error": f"could not open transcript: {e}"})

        requested_id = str(interaction_id or "").strip()
        requested_call_id = str(tool_call_id or "").strip()
        if requested_id:
            try:
                fetch_one = getattr(db, "fetch_session_interaction", None)
                if callable(fetch_one):
                    row = await fetch_one(user_id, session_id, requested_id)
                else:  # compatibility for third-party/lightweight backends
                    all_rows = await db.fetch_interactions(user_id, session_id)
                    row = next((r for r in all_rows if str(getattr(r, "id", "")) == requested_id), None)
            except Exception as e:
                return json.dumps({"status": "error", "error": f"could not read interaction: {e}"})
            if row is None:
                return json.dumps({
                    "status": "error",
                    "error": "No visible interaction with that id exists in this session.",
                    "interaction_id": requested_id,
                })

            payload = str(getattr(row, "content", "") or "")
            payload_kind = "content"
            if requested_call_id:
                if str(getattr(row, "role", "") or "") != "assistant":
                    return json.dumps({
                        "status": "error",
                        "error": "tool_call_id can only select arguments from an assistant interaction.",
                        "interaction_id": requested_id,
                    })
                try:
                    stored = json.loads(getattr(row, "output", "") or "{}")
                except (TypeError, ValueError):
                    stored = {}
                calls = stored.get("tool_calls") if isinstance(stored, dict) else None
                selected = next((c for c in (calls or [])
                                 if isinstance(c, dict) and str(c.get("id") or "") == requested_call_id), None)
                if selected is None:
                    return json.dumps({
                        "status": "error",
                        "error": "That tool call is not stored on the requested assistant interaction.",
                        "interaction_id": requested_id,
                        "tool_call_id": requested_call_id,
                    })
                fn = selected.get("function") if isinstance(selected.get("function"), dict) else {}
                raw_arguments = fn.get("arguments", selected.get("arguments", ""))
                payload = (raw_arguments if isinstance(raw_arguments, str)
                           else json.dumps(raw_arguments, ensure_ascii=False, default=str))
                payload_kind = "tool_arguments"

            char_offset = max(0, int(offset or 0))
            page_chars = max(1, min(
                int(max_chars or _DIRECT_RECALL_DEFAULT_CHARS),
                _DIRECT_RECALL_MAX_CHARS,
            ))
            content_chars = len(payload)
            chunk = payload[char_offset:char_offset + page_chars]
            next_offset = char_offset + len(chunk)
            if next_offset >= content_chars:
                next_offset = None
            return json.dumps({
                "status": "ok",
                "interaction_id": requested_id,
                "role": getattr(row, "role", "?"),
                "tool": getattr(row, "tool_name", None),
                "tool_call_id": requested_call_id or getattr(row, "tool_call_id", None),
                "created_at": str(getattr(row, "created_at", "") or ""),
                "payload_kind": payload_kind,
                "content_chars": content_chars,
                "offset": char_offset,
                "returned_chars": len(chunk),
                "next_offset": next_offset,
                "content": chunk,
            }, default=str)

        try:
            rows = await db.fetch_interactions(user_id, session_id)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"could not read transcript: {e}"})
        n = len(rows)
        if segment and int(segment) > 0:
            try:
                from app.agent.compaction import load_segments
                segs = await load_segments(db, user_id, session_id, rows)
            except Exception as e:
                return json.dumps({"status": "error", "error": f"could not read summaries: {e}"})
            k = int(segment) - 1
            if k < 0 or k >= len(segs):
                return json.dumps({"status": "error",
                                   "error": f"There is no summary PART {segment}; this conversation has {len(segs)} summary part(s)."})
            s_idx = int(segs[k].get("start_index") or 0)
            e_idx = int(segs[k].get("end_index") or 0)
        elif start or end:
            s_idx = max(0, int(start) - 1) if start else 0
            e_idx = int(end) if end else n
        else:
            return json.dumps({"status": "error",
                                "error": "Pass interaction_id, segment=<PART number>, or start/end message numbers."})
        s_idx = max(0, min(s_idx, n))
        e_idx = max(s_idx, min(e_idx, n))
        span = rows[s_idx:e_idx]
        total_in_range = len(span)
        page = span[:_RECALL_MAX_MSGS]
        result = {
            "status": "ok",
            "covers_messages": [s_idx + 1, e_idx],
            "returned_messages": [s_idx + 1, s_idx + len(page)],
            "total_in_range": total_in_range,
            "messages": _render_rows(page, start_n=s_idx + 1),
        }
        if total_in_range > len(page):
            result["note"] = (
                f"Showing the first {len(page)} of {total_in_range} messages in this range. "
                f"Call recall_compacted(start={s_idx + len(page) + 1}, end={e_idx}) for the next page."
            )
        return json.dumps(result, default=str)

    async def search_this_session(query: str, limit: int = 10) -> str:
        """Keyword-search the FULL transcript of the CURRENT conversation only.

        Finds where a word or phrase appears across every turn of this session —
        including turns that have been folded into summaries — and returns each
        hit's message number so you can read it in full with recall_compacted. Use
        this when you know a detail was discussed but don't know which summary part
        it landed in.
        """
        if not (user_id and session_id):
            return json.dumps({"status": "error",
                               "error": "search_this_session only works inside an active chat session."})
        q = (query or "").strip().lower()
        if not q:
            return json.dumps({"status": "error", "error": "Provide a non-empty query."})
        try:
            from app.db import get_db
            db = get_db()
            rows = await db.fetch_interactions(user_id, session_id)
        except Exception as e:
            return json.dumps({"status": "error", "error": f"could not read transcript: {e}"})
        matches = []
        for idx, r in enumerate(rows):
            content = (getattr(r, "content", "") or "")
            tool = (getattr(r, "tool_name", "") or "")
            low = content.lower()
            hit = low.find(q)
            if hit == -1 and q not in tool.lower():
                continue
            if hit == -1:
                snippet = content[:_SNIPPET_PAD]
            else:
                a = max(0, hit - _SNIPPET_PAD)
                b = min(len(content), hit + len(q) + _SNIPPET_PAD)
                snippet = ("…" if a > 0 else "") + content[a:b] + ("…" if b < len(content) else "")
            matches.append({
                "n": idx + 1,
                "interaction_id": getattr(r, "id", None),
                "role": getattr(r, "role", "?"),
                "tool": tool or None,
                "tool_call_id": getattr(r, "tool_call_id", None),
                "snippet": snippet,
            })
            if len(matches) >= max(1, int(limit or 10)):
                break
        return json.dumps({
            "status": "ok", "query": query, "count": len(matches),
            "results": matches,
            "hint": "Use recall_compacted(start=n, end=n) to read any hit in full." if matches else None,
        }, default=str)

    # update_task_checkpoint / get_task_checkpoint were RELOCATED to the always-on
    # core (app/tools/core_tools.py) and are wired by app/tools/loader.py under the
    # virtual Core ▸ Base row. They are the read/write half of the plan and task
    # machinery, not context-management tools, and Plan mode must never depend on
    # an optional ability — see the relocation note in core_tools.py.

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update({
        "compact_context": {"type": "object", "properties": {}, "required": []},
        "recall_compacted": {
            "type": "object",
            "properties": {
                "segment": {"type": "integer",
                            "description": "Checkpoint number to recall (normally 1; legacy sessions may expose several parts). Reads the exact original turns it replaces."},
                "start": {"type": "integer", "description": "1-based message number to start from (use instead of/with end for a precise window)."},
                "end": {"type": "integer", "description": "1-based message number to end at (inclusive)."},
                "interaction_id": {"type": "string", "description": "Exact stored interaction id from a reduced historical tool message or search result."},
                "tool_call_id": {"type": "string", "description": "With an assistant interaction_id, select and recover this tool call's original arguments."},
                "offset": {"type": "integer", "description": "0-based character offset for direct interaction retrieval."},
                "max_chars": {"type": "integer", "description": "Characters to return for direct retrieval (default 8000, maximum 20000)."},
            },
            "required": [],
        },
        "search_this_session": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Word or phrase to find across this conversation's full transcript."},
                "limit": {"type": "integer", "description": "Max hits to return (default 10)."},
            },
            "required": ["query"],
        },
    })
    DESTRUCTIVE.clear()  # explicitly non-destructive: raw transcript is preserved
    return {
        "compact_context": compact_context,
        "recall_compacted": recall_compacted,
        "search_this_session": search_this_session,
    }

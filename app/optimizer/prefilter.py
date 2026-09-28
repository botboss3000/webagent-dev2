"""
Prefilter — extracts raw session stats. No skills. No decisions. Just data.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from typing import Optional

from app.db import get_db

logger = logging.getLogger(__name__)


async def resolve_session_agent_id(user_id: str, session_id: str) -> Optional[str]:
    """Best-effort: the agent whose *base prompts* represent this session.

    The optimizer reuses the same starting point the orchestrator ability uses
    when it spawns a helper — an agent's resolved base prompt slots (via
    ``db.resolve_prompts``). This finds the right agent id to resolve:
      1. the agent bound to the target session (sessions.agent_id), if any;
      2. else the user's default agent (is_user_default);
      3. else the 'default' template resolved for the user.
    Returns ``None`` if nothing resolves (caller then skips agent context).
    """
    db = get_db()
    raw = getattr(db, "_get_conn", None)
    if raw:
        try:
            conn = raw()
            try:
                row = conn.execute(
                    "SELECT agent_id FROM sessions WHERE id=?", (session_id,)
                ).fetchone()
            finally:
                conn.close()
            if row and row[0]:
                return row[0]
        except Exception:
            pass
    try:
        agents = await db.list_agents_for_user(user_id, include_admin=True)
        for a in agents:
            if a.get("is_user_default") and a.get("id"):
                return a.get("id")
    except Exception:
        pass
    try:
        agent = await db.resolve_agent(user_id, "default")
        if agent and agent.get("id"):
            return agent.get("id")
    except Exception:
        pass
    return None


async def prefilter(user_id: str, session_id: str) -> Dict[str, Any]:
    """Pull raw session data: transcript, turns, tokens. No skill scanning."""
    try:
        raw = getattr(get_db(), "_get_conn", None)
        if not raw:
            return {"transcript": [], "turns": 0, "tokens": 0, "time_ms": 5000}
        conn = raw()

        interactions = conn.execute(
            """SELECT role, tool_name, substr(content, 1, 300), created_at
               FROM interactions WHERE session_id = ? ORDER BY created_at LIMIT 50""",
            (session_id,),
        ).fetchall()

        transcript = []
        assistant_turns = 0
        token_estimate = 0

        for role, tool, content, ts in interactions:
            prefix = f"[{role}]"
            if tool:
                prefix += f" (tool:{tool})"
            transcript.append(f"{prefix} {content[:250]}")
            if role == "assistant" and "(tool:" not in transcript[-1]:
                assistant_turns += 1
            token_estimate += len(content) // 3

        # Full transcript: no content truncation
        full_interactions = conn.execute(
            """SELECT role, tool_name, content, created_at
               FROM interactions WHERE session_id = ? ORDER BY created_at""",
            (session_id,),
        ).fetchall()
        full_transcript = []
        original_message = ""
        tool_names_used = set()
        for role, tool, content, ts in full_interactions:
            prefix = f"[{role}]"
            if tool:
                prefix += f" (tool:{tool})"
                tool_names_used.add(tool)
            full_transcript.append(f"{prefix} {content}")
            if role == "user" and not original_message and tool is None:
                original_message = content[:500]

        # Look up tool definitions for tools used in this session
        tool_definitions = []
        if tool_names_used:
            placeholders = ",".join("?" for _ in tool_names_used)
            try:
                rows = conn.execute(
                    f"SELECT name, description FROM tools WHERE name IN ({placeholders})",
                    list(tool_names_used),
                ).fetchall()
                for name, desc in rows:
                    tool_definitions.append({"name": name, "description": desc[:200] if desc else "", "source": ""})
            except Exception:
                # tools table may have different schema in some DBs
                pass

        # Get context from the agent's resolved BASE PROMPTS. The legacy
        # per-column agents.*_prompt schema was dropped (migration 021); prompts
        # now live as slot rows, resolved by db.resolve_prompts — the same base
        # prompts the orchestrator uses as the starting point when it spawns a
        # helper. Isolated in its own try so a context-lookup miss can never zero
        # out the transcript / turn / token stats (the bug this replaces, where a
        # stale column query crashed the whole prefilter back to {turns:0}).
        context_docs = []
        try:
            agent_id = await resolve_session_agent_id(user_id, session_id)
            if agent_id:
                slot_titles = {
                    "agent": "Agent Identity", "user": "User",
                    "skills": "Core Skills", "tasks": "Common Tasks", "misc": "Misc",
                }
                for s in await get_db().resolve_prompts(agent_id, user_id=user_id):
                    title = slot_titles.get(s.get("slot_name"))
                    content = s.get("content") or ""
                    if title and content.strip():
                        context_docs.append({"type": s["slot_name"], "title": title,
                                             "excerpt": content[:300]})
        except Exception as e:
            logger.debug("prefilter: agent base-prompt context unavailable: %s", e)

        conn.close()
        return {
            "transcript": transcript,
            "full_transcript": full_transcript,
            "original_message": original_message,
            "tool_definitions": tool_definitions,
            "context_docs": context_docs,
            "turns": max(assistant_turns, 1),
            "tokens": max(token_estimate, 50),
            "time_ms": 5000,
        }

    except Exception as e:
        logger.warning("Prefilter: error: %s", e)
        return {"transcript": [], "turns": 0, "tokens": 0, "time_ms": 5000}

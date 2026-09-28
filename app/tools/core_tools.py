"""
Core bootstrap tools — always available from turn 1.

These are the agent's essential utilities. Tools the agent has but that aren't
sent in full every turn are surfaced by name in the generated # [TOOLS] index
and pulled into context on demand via the `load_tool` core tool (see
app/tools/loader.py and app/tools/tool_modes.py).

What lives here (always-on host core, wired directly by app/tools/loader.py):
list_skills, load_skill (+ the _ability_skills helper), memory, session_search,
get_time, get_date, calculate, the self-prompt tools read_own_prompt /
edit_own_prompt (an agent reading + improving its OWN prompt), and the self-skill
tools save_own_skill / remove_own_skill (an agent teaching itself reusable
how-to "skill-type memories"). All four self-improvement tools are surfaced under
Core ▸ Base and gateable per-tool via the standard Deny/Ask/Auto permission, NOT
a separate ability toggle; their methodology guide lives in
plugins/abilities/Core/base/base.skill.md. The ability-gated handlers that used to live here —
web_search, get_weather, db_query, http_request — have been relocated into their
owning ability plugins (web_access, codebase_admin, browser_control); see the
in-file breadcrumbs.

⚠ DROP-IN POLICY — this file is ONLY for the always-on core bootstrap tools.
Do NOT add a new agent capability's tool handler here. New capabilities are
DROP-IN PLUGINS, never core edits:
  • an ability      → plugins/abilities/<id>.py  (ship handlers via build_tools())
  • an integration  → app/integrations/<id>_tools.py  (ship handlers via TOOLS)
Both are auto-discovered; you wire nothing. See CLAUDE.md "Core vs. plugins"
and plugins/abilities/_TEMPLATE.py.
"""

import json
import logging
from typing import List, Optional

logger = logging.getLogger(__name__)


# ── On-demand skills ──────────────────────────────────────────────────────────

async def _ability_skills(agent_id: str) -> List[dict]:
    """Skills contributed by the agent's enabled abilities (guarded, never raises)."""
    if not agent_id:
        return []
    try:
        from app.agent.ability_skills import collect_ability_skills
        return await collect_ability_skills(agent_id)
    except Exception:
        return []


async def list_skills(agent_id: str = "", session_id: str = "", db=None) -> str:
    """
    List this agent's skills — named knowledge packs you can pull in on demand.

    Returns each skill's name, when-to-use description, mode (always_on or
    selectable), and whether it is already loaded in this conversation. Call
    `load_skill` with a skill's name to load its full step-by-step instructions.
    """
    try:
        from app.db import get_db
        if db is None:
            db = get_db()
        skills = await db.get_agent_skills(agent_id) if agent_id else []
        skills = skills + await _ability_skills(agent_id)
        active = set(await db.get_session_active_skills(session_id)) if session_id else set()
        out = []
        for s in skills:
            if not s.get("enabled", True):
                continue
            always = s.get("mode") == "always_on"
            token = s.get("handle") or s["name"]
            out.append({
                "name": token,
                "display_name": s.get("display_name") or s["name"],
                "description": s.get("description", ""),
                "mode": s.get("mode", "selectable"),
                "loaded": always or token in active,
            })
        return json.dumps({"status": "ok", "count": len(out), "skills": out})
    except Exception as e:
        logger.error("list_skills failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


async def load_skill(name: str, agent_id: str = "", session_id: str = "", db=None) -> str:
    """
    Load a skill's full instructions into this conversation.

    Use this when the user's request matches a skill listed in the `[SKILLS]`
    section of your system prompt. Pass the skill's exact `name`. The returned
    instructions stay active for the rest of the conversation — load each skill
    only once.
    """
    try:
        from app.db import get_db
        from app.agent.skills import loaded_result_text
        if db is None:
            db = get_db()
        skills = await db.get_agent_skills(agent_id) if agent_id else []
        skills = skills + await _ability_skills(agent_id)
        target = (name or "").strip().lower()
        match = next(
            (s for s in skills
             if s["name"].lower() == target or (s.get("handle") or "").lower() == target),
            None,
        )
        if not match:
            avail = [(s.get("handle") or s["name"]) for s in skills if s.get("enabled", True)]
            return json.dumps({
                "status": "error",
                "message": f"No skill named '{name}'. Available: "
                           f"{', '.join(avail) if avail else '(none)'}",
            })
        if not match.get("enabled", True):
            return json.dumps({"status": "error",
                               "message": f"Skill '{match['name']}' is disabled."})
        # Record it active for this session (drives the catalog + UI chips).
        if session_id:
            try:
                await db.set_session_active_skill(session_id, match["name"], True)
            except Exception as e:
                logger.debug("set_session_active_skill failed: %s", e)
        # The returned text IS the persisted tool result, so it replays into
        # context for the rest of the conversation. Its header line lets the
        # deactivate endpoint neutralize this row later.
        return loaded_result_text(match)
    except Exception as e:
        logger.error("load_skill failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# web_search → relocated to plugins/abilities/Web/web_access.py (web_access ability).
# db_query   → relocated to plugins/abilities/Administrator/codebase_admin.py
#              (codebase_admin ability).


# ── Memory (persistent knowledge pages) ──────────────────────────────────────

async def memory(
    action: str,
    slug: Optional[str] = None,
    query: Optional[str] = None,
    page_type: Optional[str] = None,
    title: Optional[str] = None,
    compiled_truth: Optional[str] = None,
    timeline: Optional[str] = None,
    limit: int = 10,
    user_id: str = "",
) -> str:
    """
    Manage persistent memory pages across sessions.

    Memory pages use a compiled-truth + timeline format. Each page has a
    unique slug. Pages persist across sessions and are searchable.

    Actions:
      search     → Search memory by keyword query
      list       → List all memory pages, optionally filtered by page_type
      get        → Get one memory page by slug
      upsert     → Create or update a memory page (requires slug, compiled_truth)
      delete     → Delete a memory page by slug

    Use this to remember important information about the user, project,
    preferences, and past decisions.
    """
    try:
        from app.db import get_db
        db = get_db()

        if action == "search":
            if not query:
                return json.dumps({"status": "error", "message": "query required for search action"})
            results = await db.memory_search(user_id, query, limit=limit)
            return json.dumps({
                "status": "ok",
                "query": query,
                "count": len(results or []),
                "results": results or [],
            })

        elif action == "list":
            results = await db.memory_list(user_id, page_type=page_type)
            return json.dumps({"status": "ok", "count": len(results or []), "pages": results or []})

        elif action == "get":
            if not slug:
                return json.dumps({"status": "error", "message": "slug required for get action"})
            result = await db.memory_get(user_id, slug)
            if not result:
                return json.dumps({"status": "error", "message": f"Memory page '{slug}' not found."})
            return json.dumps({"status": "ok", "page": result})

        elif action == "upsert":
            if not slug:
                return json.dumps({"status": "error", "message": "slug required for upsert action"})
            if not compiled_truth:
                return json.dumps({"status": "error", "message": "compiled_truth required for upsert action"})
            # Near-duplicate guard: if a semantically near-identical fact already
            # exists under a DIFFERENT slug, update THAT row instead of adding a
            # restatement — one row per fact, newest wording wins. Runs on the
            # in-process embedder (free); degrades to a plain insert if the
            # embedder is unavailable or nothing similar is found. See memory_dedup.
            target_slug = slug
            merged_from = None
            try:
                from app.agent.memory_dedup import find_duplicate_memory
                dup = await find_duplicate_memory(db, user_id, slug, compiled_truth)
            except Exception as _dedup_err:
                logger.debug("memory dedup skipped: %s", _dedup_err)
                dup = None
            if dup:
                target_slug = dup["slug"]
                merged_from = slug
            try:
                from app.tools.execution_context import current_tool_context
                _memory_ctx = current_tool_context()
            except Exception:
                _memory_ctx = None
            result = await db.memory_upsert(
                user_id, target_slug,
                page_type=page_type or "note",
                title=title or target_slug,
                compiled_truth=compiled_truth,
                timeline=timeline or "",
                origin="deliberate",
                source_session_id=getattr(_memory_ctx, "session_id", None),
                source_interaction_id=getattr(_memory_ctx, "tool_call_id", None),
            )
            resp = {"status": "ok", "slug": target_slug, "action": "upserted"}
            if merged_from:
                resp["action"] = "merged"
                resp["merged_from"] = merged_from
                resp["note"] = (
                    f"Recognised as a near-duplicate of existing memory "
                    f"'{target_slug}' (similarity {dup['similarity']:.2f}); updated "
                    f"that entry in place instead of creating a new one."
                )
            return json.dumps(resp)

        elif action == "delete":
            if not slug:
                return json.dumps({"status": "error", "message": "slug required for delete action"})
            ok = await db.memory_delete(user_id, slug)
            return json.dumps({"status": "ok" if ok else "error", "deleted": ok, "slug": slug})

        else:
            return json.dumps({
                "status": "error",
                "message": f"Unknown action '{action}'. Use: search, list, get, upsert, delete.",
            })

    except Exception as e:
        logger.error("memory tool failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Session search ───────────────────────────────────────────────────────────

async def session_search(
    query: str = "",
    limit: int = 10,
    user_id: str = "",
    session_id: str = "",
    after_session_seq: int = 0,
) -> str:
    """
    Search across past conversations, or inspect one exact owned session.

    Finds user messages, assistant replies, and tool calls that match
    ``query``. When ``session_id`` is supplied, returns that session's newest
    durable rows in chronological order; ``after_session_seq`` supports safe
    incremental inspection of a live source session.
    """
    try:
        from app.db import get_db
        db = get_db()
        q = (query or "").strip().lower()
        bounded_limit = max(1, min(int(limit or 10), 50))
        try:
            after_seq = max(0, int(after_session_seq or 0))
        except (TypeError, ValueError):
            after_seq = 0

        if session_id:
            conn = db._get_conn()
            try:
                session = conn.execute(
                    "SELECT title, status FROM sessions WHERE id = ? AND user_id = ?",
                    (session_id, user_id),
                ).fetchone()
                if not session:
                    return json.dumps({
                        "status": "error",
                        "message": "Session not found or not owned by this user",
                    })
                latest = conn.execute(
                    "SELECT MAX(session_seq) FROM interactions WHERE session_id = ? "
                    "AND (status IS NULL OR status != 'deleted')",
                    (session_id,),
                ).fetchone()
                latest_seq = int(latest[0] or 0) if latest else 0
                params = [session_id, after_seq]
                where = (
                    "session_id = ? AND COALESCE(session_seq, 0) > ? "
                    "AND (status IS NULL OR status != 'deleted')"
                )
                if q:
                    where += " AND (LOWER(COALESCE(content, '')) LIKE ? OR LOWER(COALESCE(tool_name, '')) LIKE ?)"
                    params.extend([f"%{q}%", f"%{q}%"])
                params.append(bounded_limit)
                rows = conn.execute(
                    "SELECT id, session_id, role, content, tool_name, created_at, "
                    "session_seq, status FROM interactions WHERE " + where
                    + " ORDER BY session_seq DESC, created_at DESC LIMIT ?",
                    tuple(params),
                ).fetchall()
            finally:
                conn.close()

            results = []
            for row in reversed(rows):
                results.append({
                    "id": row[0],
                    "session_id": row[1],
                    "role": row[2],
                    "content": (row[3] or "")[:2000],
                    "tool_name": row[4],
                    "created_at": row[5],
                    "session_seq": row[6],
                    "status": row[7],
                })
            return json.dumps({
                "status": "ok",
                "session_id": session_id,
                "title": session[0],
                "session_status": session[1],
                "query": query or "",
                "after_session_seq": after_seq,
                "latest_session_seq": latest_seq,
                "count": len(results),
                "results": results,
            })

        if not q:
            return json.dumps({
                "status": "error",
                "message": "Provide query or session_id",
            })
        
        # Try the raw query-builder client first
        raw = getattr(db, 'get_raw_client', None)
        if raw:
            try:
                client = raw()
                rows = (
                    client.table("interactions")
                    .select("id, session_id, role, content, tool_name, created_at")
                    .eq("user_id", user_id)
                    .limit(100)
                    .order("created_at", desc=True)
                    .execute()
                )
                data = rows.data or []
                matches = []
                for r in data:
                    content = r.get("content", "") or ""
                    tool_name = r.get("tool_name", "") or ""
                    if q in content.lower() or q in tool_name.lower():
                        matches.append({
                            "id": r.get("id"),
                            "session_id": r.get("session_id"),
                            "role": r.get("role"),
                            "tool_name": r.get("tool_name"),
                            "content": content[:500],
                            "created_at": r.get("created_at"),
                        })
                    if len(matches) >= bounded_limit:
                        break
                return json.dumps({"status": "ok", "query": query, "count": len(matches), "results": matches})
            except Exception as e:
                logger.debug("Raw client session_search failed, falling back to local: %s", e)
        
        # Fallback: the active local backend (SQLite or Postgres)
        from app.db import get_db
        conn = get_db()._get_conn()
        rows = conn.execute(
            """SELECT i.id, i.session_id, i.role, i.content, i.tool_name, i.created_at
               FROM interactions i
               JOIN sessions s ON i.session_id = s.id
               WHERE s.user_id = ? AND (LOWER(i.content) LIKE ? OR LOWER(COALESCE(i.tool_name, '')) LIKE ?)
               ORDER BY i.created_at DESC LIMIT ?""",
            (user_id, f"%{q}%", f"%{q}%", bounded_limit)
        ).fetchall()
        conn.close()
        matches = []
        for r in rows:
            matches.append({
                "id": r[0],
                "session_id": r[1],
                "role": r[2],
                "content": (r[3] or "")[:500],
                "tool_name": r[4],
                "created_at": r[5],
            })
        return json.dumps({"status": "ok", "query": query, "count": len(matches), "results": matches})
    except Exception as e:
        logger.error("session_search failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Similar sessions (semantic, whole-conversation) ───────────────────────────
# Sibling of session_search. Where session_search matches KEYWORDS inside
# individual messages, this ranks whole past CONVERSATIONS by meaning — the
# "which past chat is like this one?" question. Built on the in-process embedder
# (local, free): it embeds each recent session's title + opening message on the
# fly and cosine-ranks them, so it needs NO new storage or migration. Degrades to
# a title keyword match if the embedder is unavailable. Candidate set is capped
# at the most recent N sessions to bound cost (stated in the result).

_SIMILAR_SESSIONS_SCAN = 80  # most-recent sessions embedded per call (cost bound)


def _sim_cosine(a, b) -> float:
    import math
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb) + 1e-12)


async def similar_sessions(
    query: str,
    limit: int = 8,
    user_id: str = "",
    exclude_session_id: str = "",
) -> str:
    """
    Find PAST chat sessions whose TOPIC is semantically similar to `query`.

    Unlike session_search (which keyword-matches individual messages), this ranks
    whole past *conversations* by meaning — use it to resurface "the chat where we
    set up X" or to find prior sessions on the same topic so you can continue
    rather than start over. Describe the topic in `query` (e.g. "setting up the
    deploy pipeline"). Only the most recent sessions are scanned.
    """
    try:
        from app.db import get_db
        db = get_db()
        q = (query or "").strip()
        if not q:
            return json.dumps({"status": "error", "message": "query required"})

        # Candidate whole-conversation texts: title + first user message, newest
        # first. One correlated query — works on SQLite and the Postgres-portable
        # connection (same `?` placeholder + LIKE the rest of this module uses).
        conn = db._get_conn()
        try:
            rows = conn.execute(
                """SELECT s.id, s.title, s.created_at,
                          (SELECT i.content FROM interactions i
                             WHERE i.session_id = s.id AND i.role = 'user'
                             ORDER BY i.created_at ASC LIMIT 1) AS first_msg
                     FROM sessions s
                    WHERE s.user_id = ?
                    ORDER BY s.created_at DESC
                    LIMIT ?""",
                (user_id, _SIMILAR_SESSIONS_SCAN),
            ).fetchall()
        finally:
            conn.close()

        # Normalise rows (sqlite3.Row or tuple) into candidate dicts with text.
        cands = []
        for r in rows or []:
            sid = r[0] if not isinstance(r, dict) else r.get("id")
            title = (r[1] if not isinstance(r, dict) else r.get("title")) or ""
            created = r[2] if not isinstance(r, dict) else r.get("created_at")
            first_msg = (r[3] if not isinstance(r, dict) else r.get("first_msg")) or ""
            if sid and sid == exclude_session_id:
                continue
            text = f"{title}. {first_msg[:300]}".strip(". ").strip()
            if not text:
                continue
            cands.append({"session_id": sid, "title": title,
                          "created_at": created, "text": text})
        if not cands:
            return json.dumps({"status": "ok", "query": query, "count": 0,
                               "results": [], "scanned": len(rows or [])})

        # Semantic rank (local embedder). Any failure → keyword fallback on title.
        results = []
        mode = "semantic"
        try:
            from app.agent.embed import embed_text
            qv = await embed_text(q)
            scored = []
            for c in cands:
                try:
                    cv = await embed_text(c["text"])
                except Exception:
                    continue
                scored.append((_sim_cosine(qv, cv), c))
            scored.sort(key=lambda x: -x[0])
            for score, c in scored[:max(1, int(limit or 8))]:
                results.append({
                    "session_id": c["session_id"],
                    "title": c["title"] or "(untitled)",
                    "similarity": round(float(score), 3),
                    "created_at": c["created_at"],
                })
        except Exception as embed_err:
            logger.debug("similar_sessions: embed unavailable, keyword fallback: %s", embed_err)
            mode = "keyword_fallback"
            ql = q.lower()
            for c in cands:
                if ql in (c["title"] or "").lower() or ql in c["text"].lower():
                    results.append({
                        "session_id": c["session_id"],
                        "title": c["title"] or "(untitled)",
                        "created_at": c["created_at"],
                    })
                if len(results) >= max(1, int(limit or 8)):
                    break

        return json.dumps({
            "status": "ok", "query": query, "mode": mode,
            "scanned": len(cands), "count": len(results), "results": results,
        })
    except Exception as e:
        logger.error("similar_sessions failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Self-prompt (read + improve your OWN prompt) ──────────────────────────────
# The two halves of agent self-improvement. An agent uses these to study and then
# refine the instruction sections ("slots") that define it. They act ONLY on the
# calling agent's own prompt — there is no way to target another agent (that lives
# behind the heavier Agent Management ability, which deliberately FORBIDS
# self-targeting). Admin-LOCKED slots are read-only here, so safety/identity
# sections an admin protects can never be self-rewritten. Writes go to the
# admin-base slot layer (user_id IS NULL), exactly like edit_agent_prompt.

async def read_own_prompt(agent_id: str = "", user_id: str = "") -> str:
    """
    Read your OWN prompt — the instruction sections that define you.

    This is the self-knowledge half of self-improvement: review how you are
    currently instructed before deciding what to refine. Returns every section
    ("slot") of your own system prompt, each with:
      - slot_name : the section's name (e.g. system, agent, user, skills)
      - order     : where it sits in the assembled prompt
      - locked    : true = protected by your admin — you may READ it, but
                    edit_own_prompt will refuse to change it
      - editable  : true = you may rewrite this section with edit_own_prompt
      - content   : the section's current text

    You can only ever see your OWN prompt — never another agent's. Pair this with
    edit_own_prompt to study, then improve, your own instructions.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return json.dumps({
                "status": "error",
                "message": "No agent is bound to this conversation, so there is no prompt to read.",
            })
        slots = await db.list_slots(agent_id)
        out = []
        for s in slots:
            name = s.get("slot_name") or ""
            # Internal slots (e.g. __skills__) are machine-managed by their own
            # tools (save_own_skill / load_skill) — hide them from the prompt view.
            if name.startswith("__"):
                continue
            locked = bool(s.get("lock"))
            out.append({
                "slot_name": name,
                "order": s.get("order_index"),
                "locked": locked,
                "editable": not locked,
                "content": s.get("content", "") or "",
            })
        return json.dumps({"status": "ok", "count": len(out), "slots": out})
    except Exception as e:
        logger.error("read_own_prompt failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


async def edit_own_prompt(
    slot_name: str,
    content: str,
    mode: str = "replace",
    agent_id: str = "",
    user_id: str = "",
) -> str:
    """
    Improve your OWN prompt by editing one of its sections.

    This is how you update and improve yourself: rewrite or extend a section
    ("slot") of your own system prompt so future runs behave better. Read your
    current prompt first with read_own_prompt.

      slot_name : the section to change (from read_own_prompt). A NEW name
                  creates a new section.
      content   : the new text.
      mode      : "replace" overwrites the section with content (default), or
                  "append" adds content to the end of the existing section — use
                  append to record a lesson learned without losing what's there.

    Rules:
      - You can only edit your OWN prompt; there is no way to target another
        agent (use the Agent Management ability for that).
      - Sections your admin has LOCKED are read-only — this refuses to change
        them. That is how safety/identity instructions are protected from
        self-modification. New sections you create start unlocked.
    Changes take effect on your next run.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return json.dumps({
                "status": "error",
                "message": "No agent is bound to this conversation, so there is no prompt to edit.",
            })
        if not slot_name or not slot_name.strip():
            return json.dumps({"status": "error", "message": "slot_name is required."})
        if content is None:
            return json.dumps({"status": "error", "message": "content is required."})
        mode = (mode or "replace").lower()
        if mode not in ("replace", "append"):
            return json.dumps({"status": "error", "message": "mode must be 'replace' or 'append'."})

        slot_name = slot_name.strip()
        existing = await db.list_slots(agent_id)
        current = next((s for s in existing if s.get("slot_name") == slot_name), None)

        # Respect locks — a locked section is admin-protected and read-only here.
        if current and bool(current.get("lock")):
            return json.dumps({
                "status": "error",
                "message": (f"Section '{slot_name}' is locked by your admin and cannot be "
                            f"self-edited. You can still read it with read_own_prompt."),
            })

        # Resolve the new content (append vs replace).
        if mode == "append" and current:
            base = current.get("content", "") or ""
            new_content = base + ("\n\n" if (base and content) else "") + (content or "")
        else:
            new_content = content

        resolved_order = (
            current.get("order_index") if current
            else (max((s.get("order_index") or 0) for s in existing) + 10 if existing else 10)
        )
        merge_mode = (current.get("merge_mode")
                      if current and current.get("merge_mode") in ("replace", "append")
                      else "replace")

        await db.upsert_slot(
            agent_id=agent_id,
            slot_name=slot_name,
            order_index=int(resolved_order),
            lock=False,  # self-edited/created sections stay unlocked (admin can lock later)
            merge_mode=merge_mode,
            content=new_content,
            updated_by=f"self:{agent_id}",
        )
        return json.dumps({
            "status": "ok",
            "slot_name": slot_name,
            "mode": mode,
            "created": current is None,
            "message": (f"Your '{slot_name}' prompt section was "
                        f"{'created' if current is None else 'updated'}. "
                        f"It takes effect on your next run."),
        })
    except Exception as e:
        logger.error("edit_own_prompt failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Self-skills (teach yourself reusable how-to "skill-type memories") ─────────
# An agent's skills are named, loadable knowledge packs (each like a little
# skill.md: a "when to use it" line + a body of instructions). These let the
# agent WRITE its own — the heart of self-improvement: when it builds something
# reusable, works out a non-obvious procedure, or is given a durable task-specific
# instruction, it records a skill so a future run can load it with load_skill.
# This is for MEANINGFUL, reusable know-how only — ordinary facts belong in the
# `memory` tool, and ordinary replies need saving nowhere. Skills are stored in
# the agent's own (admin-base) skill list, exactly like admin/agent-authored ones,
# so they appear in list_skills and load on demand. Self-targeted: an agent can
# only ever write its OWN skills.

async def save_own_skill(
    name: str,
    description: Optional[str] = None,
    instructions: Optional[str] = None,
    mode: str = "selectable",
    agent_id: str = "",
    user_id: str = "",
) -> str:
    """
    Teach yourself a reusable SKILL — a named how-to you can load on a later task.

    This is how you improve yourself with durable, reusable know-how. Save a skill
    when something is worth keeping for next time, for example:
      - you built or set up something and want to remember HOW to use it,
      - you worked out a non-obvious procedure or fix worth repeating,
      - the user gave a SPECIFIC, lasting instruction for how a task should be done.
    Do NOT use this for ordinary facts (use the `memory` tool) or for normal
    replies (save nothing).

      name         : short identifier you'll load it by later.
      description  : ONE line saying WHEN to use this skill — always shown to you
                     in your [SKILLS] catalog, so make the trigger obvious.
      instructions : the full step-by-step body (the actual know-how).
      mode         : "selectable" (default) keeps the body out of context until
                     you load it with load_skill on a matching task — best for
                     most playbooks; "always_on" keeps it in context every turn —
                     use only for short, essential guidance.

    Saving an existing name updates it (omitted fields keep their current value).
    You can only ever write your OWN skills. See load_skill / list_skills to use
    them, and remove_own_skill to delete one.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return json.dumps({
                "status": "error",
                "message": "No agent is bound to this conversation, so there is no skill set to write.",
            })
        if not name or not name.strip():
            return json.dumps({"status": "error", "message": "name is required."})
        nm = name.strip()
        if mode is not None and mode not in ("selectable", "always_on"):
            return json.dumps({"status": "error", "message": "mode must be 'selectable' or 'always_on'."})

        skills = await db.get_agent_skills(agent_id)
        existing = next((s for s in skills if (s.get("name") or "").lower() == nm.lower()), None)
        if existing:
            if description is not None:
                existing["description"] = description
            if instructions is not None:
                existing["body"] = instructions
            if mode is not None:
                existing["mode"] = mode
            existing["enabled"] = True
        else:
            if instructions is None or not instructions.strip():
                return json.dumps({"status": "error",
                                   "message": "instructions are required when creating a new skill."})
            skills.append({
                "name": nm,
                "description": description or "",
                "body": instructions,
                "mode": mode or "selectable",
                "enabled": True,
            })
        saved = await db.set_agent_skills(agent_id, skills, updated_by=f"self:{agent_id}")
        return json.dumps({
            "status": "ok",
            "skill": nm,
            "created": existing is None,
            "count": len(saved),
            "message": (f"Skill '{nm}' was {'created' if existing is None else 'updated'}. "
                        f"You can load it with load_skill('{nm}') on a future matching task."),
        })
    except Exception as e:
        logger.error("save_own_skill failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


async def remove_own_skill(name: str, agent_id: str = "", user_id: str = "") -> str:
    """
    Delete one of your OWN skills by name.

    Use this to retire a skill that is wrong, stale, or no longer useful. Only
    your own skills can be removed (skills contributed by your abilities are not
    yours to delete). See list_skills for the names.
    """
    try:
        from app.db import get_db
        db = get_db()
        if not agent_id:
            return json.dumps({
                "status": "error",
                "message": "No agent is bound to this conversation, so there is no skill set to edit.",
            })
        if not name or not name.strip():
            return json.dumps({"status": "error", "message": "name is required."})
        target = name.strip().lower()
        skills = await db.get_agent_skills(agent_id)
        kept = [s for s in skills if (s.get("name") or "").lower() != target]
        if len(kept) == len(skills):
            return json.dumps({"status": "error", "message": f"You have no skill named '{name}'."})
        saved = await db.set_agent_skills(agent_id, kept, updated_by=f"self:{agent_id}")
        return json.dumps({"status": "ok", "removed": name, "count": len(saved)})
    except Exception as e:
        logger.error("remove_own_skill failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Time & Date utilities ─────────────────────────────────────────────────────

async def get_time(timezone: str = "UTC") -> str:
    """
    Get the current time in a specified timezone.

    Args:
        timezone: IANA timezone name (e.g. "America/New_York", "Europe/London",
                  "Asia/Tokyo"). Defaults to "UTC".

    Returns:
        Current time as a human-readable string.
    """
    from datetime import datetime
    if not timezone:
        timezone = "UTC"
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.now(tz)
        use_12h = now.strftime("%p") != ""
        return now.strftime("%I:%M:%S %p") if use_12h else now.strftime("%H:%M:%S")
    except Exception as e:
        return f"Error: could not resolve timezone '{timezone}'. Use IANA format e.g. 'America/New_York'. ({e})"


async def get_date(timezone: str = "UTC", format: str = "full") -> str:
    """
    Get the current date in a specified timezone.

    Args:
        timezone: IANA timezone name (e.g. "America/New_York"). Defaults to "UTC".
        format: "full" (Monday, May 8, 2026), "short" (2026-05-08), or "iso" (2026-05-08T...)

    Returns:
        Current date as a human-readable string.
    """
    from datetime import datetime
    if not timezone:
        timezone = "UTC"
    try:
        import zoneinfo
        tz = zoneinfo.ZoneInfo(timezone)
        now = datetime.now(tz)
        if format == "short":
            return now.strftime("%Y-%m-%d")
        elif format == "iso":
            return now.isoformat()
        else:
            return now.strftime("%A, %B %d, %Y")
    except Exception as e:
        return f"Error: could not resolve timezone '{timezone}'. Use IANA format e.g. 'America/New_York'. ({e})"


# get_weather (+ its _resolve_location / _weather_code_to_text / _encode_location
# helpers and the _WEATHER_CODES table) → relocated to
# plugins/abilities/Web/web_access.py (web_access ability).


# ── Calculator ────────────────────────────────────────────────────────────────

import math as _math

_CALC_SAFE_GLOBALS = {
    "__builtins__": {},
    "abs": abs,
    "round": round,
    "min": min,
    "max": max,
    "sum": sum,
    "pow": pow,
    "int": int,
    "float": float,
    "str": str,
    "len": len,
    "range": range,
    "True": True,
    "False": False,
    "None": None,
}

_CALC_SAFE_LOCALS = {
    "pi": _math.pi,
    "e": _math.e,
    "inf": _math.inf,
    "nan": _math.nan,
    "sin": _math.sin,
    "cos": _math.cos,
    "tan": _math.tan,
    "asin": _math.asin,
    "acos": _math.acos,
    "atan": _math.atan,
    "atan2": _math.atan2,
    "sqrt": _math.sqrt,
    "log": _math.log,
    "log10": _math.log10,
    "exp": _math.exp,
    "floor": _math.floor,
    "ceil": _math.ceil,
    "degrees": _math.degrees,
    "radians": _math.radians,
    "factorial": _math.factorial,
}


# http_request → relocated to plugins/abilities/Web/browser_control.py
#                (browser_control ability).


async def calculate(expression: str) -> str:
    """
    Evaluate a mathematical expression.

    Supports: +, -, *, /, **, %, (), and functions like sin(), cos(), sqrt(),
    log(), abs(), round(), pi, e, factorial().

    Args:
        expression: A mathematical expression as a string (e.g. "2 + 2",
                   "sin(pi/4) * 100", "sqrt(144) + 3!")

    Returns:
        The result of the expression.
    """
    import traceback

    # Replace common notation: 3! → factorial(3)
    import re
    expr = re.sub(r"(\d+)!", r"factorial(\1)", expression)

    try:
        result = eval(expr, _CALC_SAFE_GLOBALS, _CALC_SAFE_LOCALS)
        # Format: round floats, avoid scientific notation for common sizes
        if isinstance(result, float):
            if abs(result) < 0.0001 or abs(result) > 1e12:
                formatted = f"{result:.6e}"
            else:
                formatted = f"{result:.6f}".rstrip("0").rstrip(".")
        else:
            formatted = str(result)
        return f"{expression} = {formatted}"
    except Exception as e:
        return f"Error evaluating '{expression}': {e}. Only basic math expressions are supported."
    except Exception as e:
        logger.error("resume_session failed: %s", e)
        return json.dumps({"status": "error", "message": str(e)})


# ── Plan workspace + chat-component presenters (core) ─────────────────────────
# RELOCATED here from the Visualizer ability (app/visualizer/register_tools):
# neither is a Gen UI tool. Both are plain chat-component / harness presenters
# built on app/chat_components + app/agent/task_state, and both are wired by
# app/tools/loader.py with the rest of the always-on core (listed under the
# virtual Core ▸ Base row).
#
# present_plan is REQUIRED by Plan mode's mode contract (app/agent/execution_modes.py
# "plan-document" + the loop's require_plan_document completion gate). While it
# shipped inside the OPTIONAL Visualizer ability, an agent whose Visualizer was
# switched off could still be put into Plan mode and then hard-fail the turn with
# "plan_document_tool_unavailable" — a mode the platform offers every agent must
# never depend on an ability an admin can turn off.

async def present_chat_component(type: str, title: str = "", placement: str = "inline",
                                 data: dict = None, lifecycle: dict = None,
                                 component_id: str = None, **_extra) -> str:
    """Present a declarative chat component (todo_list, status, choice, approval, form).

    Use this for an in-chat status, checklist, choice, approval, or form card. The
    component is saved against this chat session server-side, so it survives a
    reload and re-renders for clients that connect later. Titles stay terse (1–2
    words). No agent-authored HTML or JS ever runs inside the chat shell.
    """
    from app.chat_components import validate_spec, save_component
    from app.tools.execution_context import current_tool_context
    try:
        component = validate_spec({"id": component_id, "type": type, "title": title,
                                   "placement": placement, "data": data or {},
                                   "lifecycle": lifecycle or {}})
    except ValueError as exc:
        return json.dumps({"status": "error", "message": str(exc)})
    # Durable server-side save (same row shape the browser used to POST).
    # Best-effort: a save failure must not fail the tool — the live event
    # would still render the component for connected clients.
    try:
        ctx = current_tool_context()
        if ctx is not None:
            await save_component(ctx.user_id, ctx.session_id, component)
    except Exception:
        pass
    return json.dumps({"status": "ok", "ui": "chat_component", "component": component})


async def present_plan(overview: dict = None, checklist: list = None,
                       goal: str = "", criteria: str = "", notes: str = "",
                       title: str = "Plan", **_extra) -> str:
    """Save the durable Plan Overview and merge the execution checklist into Task Progress.

    Required to complete a turn in Plan mode. Writes the stable `plan-overview`
    chat component and, when a durable Task Progress checkpoint exists, replaces
    the execution-phase slice of canonical Task Progress with `checklist`
    (preserving statuses for matching step ids). The task system is OPTIONAL: with
    checkpoints disabled the Plan Overview is still saved and this call still
    succeeds, so Plan mode never depends on whether an agent has task progress
    switched on. Needs a non-empty checklist. Titles stay terse (1–2 words); a
    longer one is dropped.
    """
    from app.chat_components import validate_spec, save_component, short_title
    from app.agent.task_state import current_state, merge_execution_steps, progress_component
    from app.db import get_db
    from app.tools.execution_context import current_tool_context
    ctx = current_tool_context()
    if ctx is None:
        return json.dumps({"status": "error", "message": "no active session context"})
    # Component titles stay terse (1-2 words, max 3). A short plan title
    # prefixes the element name ("Plan Overview"); a long one is dropped.
    words = [w for w in str(title or "").split() if w]
    if len(words) <= 2:
        base = " ".join(words) or "Plan"
        overview_title = short_title(base + " Overview", "Overview")
    else:
        overview_title = "Overview"
    sections = []
    if goal:
        sections.append({"label": "Goal", "status": None, "value": goal})
    if criteria:
        sections.append({"label": "Criteria", "status": None, "value": criteria})
    for s in (overview or {}).get("sections", []):
        if s not in sections:
            sections.append(s)
    if notes:
        sections.append({"label": "Notes", "status": None, "value": notes})
    # The overview renders as a MARKDOWN document (the status renderer uses
    # data.markdown when present; sections are the structured fallback).
    md_parts = []
    for s in sections:
        md_parts.append(f"## {s['label']}")
        if s.get("status"):
            md_parts.append(f"*{s['status']}*")
        if s.get("value"):
            md_parts.append(s["value"])
    overview_comp = validate_spec({
        "id": "plan-overview", "type": "status", "title": overview_title,
        "placement": "sticky",
        "data": {"sections": sections, "markdown": "\n\n".join(md_parts)},
        "lifecycle": {}})
    if not checklist:
        return json.dumps({"status": "error",
                           "message": "present_plan requires a detailed execution checklist"})
    # The durable Task Progress checkpoint is OPTIONAL — it exists only when the
    # agent's manager.task_progress.enabled is switched on. Plan mode is offered to
    # EVERY agent, so the Plan Overview must not depend on it: with a checkpoint we
    # also merge the execution checklist into canonical Task Progress, without one
    # we still save the standalone overview and report success. Returning status
    # "ok" is what satisfies the loop's require_plan_document completion gate.
    state = current_state(get_db(), ctx.session_id)
    if state:
        try:
            saved_state = merge_execution_steps(
                get_db(), ctx.session_id, state["turn_id"], checklist,
            )
            progress_comp = validate_spec(progress_component(saved_state))
            await save_component(ctx.user_id, ctx.session_id, overview_comp)
            await save_component(ctx.user_id, ctx.session_id, progress_comp)
        except Exception as exc:
            return json.dumps({"status": "error", "message": str(exc)})
        return json.dumps({"status": "ok", "ui": "chat_component",
                           "components": [overview_comp, progress_comp]})
    try:
        await save_component(ctx.user_id, ctx.session_id, overview_comp)
    except Exception as exc:
        return json.dumps({"status": "error", "message": str(exc)})
    return json.dumps({"status": "ok", "ui": "chat_component",
                       "components": [overview_comp],
                       "task_progress": "unavailable",
                       "note": "Task Progress checkpoints are disabled for this agent, so the "
                               "Plan Overview was saved on its own and the execution checklist "
                               "was not merged into canonical Task Progress."})


# ── Task Progress checkpoint tools (core) ─────────────────────────────────────
# These are plan/task-machine harness tools, not context-management tools. They
# are wired by app/tools/loader.py as Core ▸ Base utilities, but unlike irreducible
# mode machinery they remain subject to explicit per-agent and global deny
# permissions. A denied checkpoint tool is never provided to the model.
#
# Unlike the ability versions they replace, identity is read from the ACTIVE TOOL
# CONTEXT rather than captured as closure arguments — the same shape present_plan
# and present_chat_component use above.

async def get_task_checkpoint() -> str:
    """Read the current task contract, checkpoint and revision after tool work."""
    from app.db import get_db
    from app.agent.task_state import current_state
    from app.tools.execution_context import current_tool_context
    ctx = current_tool_context()
    if ctx is None:
        raise ValueError("Checkpoint read requires the active session context")
    state = current_state(get_db(), ctx.session_id)
    if not state:
        raise ValueError("No active task checkpoint")
    from app.agent.turn_reservations import stable_key
    from app.agent.run_fence import side_effects_allowed
    if ctx.turn_key != stable_key("chat-turn", ctx.user_id, ctx.session_id, state["turn_id"]):
        raise ValueError("Checkpoint belongs to a replaced run")
    if not await side_effects_allowed(get_db(), ctx.session_id, expected_turn_id=state["turn_id"]):
        raise ValueError("Checkpoint is fenced by Stop or replacement")
    cp = state.get("checkpoint") or {}
    return json.dumps({
        "status": "ok",
        "revision": state.get("revision"),
        "contract_revision": state.get("contract_revision"),
        "task_status": state.get("status"),
        "mode": state.get("mode"),
        "task": state.get("title", "Task"),
        "requested_outcomes": [r.get("text") for r in state.get("requirements", [])
                               if not r.get("parent_id")],
        "steps": [{
            "label": step.get("label") or step.get("text"),
            "phase": step.get("phase", "planning"), "status": step.get("status"),
        } for step in state.get("steps", [])],
        "active_step": next((step.get("label") or step.get("text") for step in state.get("steps", [])
                             if step.get("id") == cp.get("active_step")), ""),
        "next_action": cp.get("next_action"),
        "blockers": cp.get("blockers", []),
        "pending_operation_ids": [op.get("id") for op in cp.get("pending_operations", [])
                                  if op.get("status") == "uncertain"],
        "hint": "The full authoritative request and detailed plan are already injected in the current system context.",
    }, ensure_ascii=False)


async def update_task_checkpoint(expected_revision: int, update: dict) -> str:
    """Save request coverage, plan progress, findings and the exact next action.
    Internal bookkeeping only; cannot change user scope or declare verified success.
    Read the current revision from the durable task state in context.
    """
    from app.db import get_db
    from app.agent.task_state import current_state, update_checkpoint
    from app.agent.run_fence import side_effects_allowed
    from app.tools.execution_context import current_tool_context
    from app.agent.turn_reservations import stable_key
    db = get_db()
    ctx = current_tool_context()
    state = current_state(db, ctx.session_id) if ctx is not None else None
    if not state or ctx is None:
        raise ValueError("Checkpoint requires an active, scoped task")
    if ctx.turn_key != stable_key("chat-turn", ctx.user_id, ctx.session_id, state["turn_id"]):
        raise ValueError("Checkpoint belongs to a replaced run")
    if not await side_effects_allowed(db, ctx.session_id, expected_turn_id=state["turn_id"]):
        raise ValueError("Checkpoint is fenced by Stop or replacement")
    if not isinstance(update, dict) or not isinstance(update.get("summary"), str) or not 20 <= len(update["summary"].strip()) <= 1200:
        raise ValueError("Include update.summary: a concise user-facing result, implication for the active step, and next action (20-1200 characters).")
    # A manager can record advisory findings between the model's inference
    # and this bookkeeping call. That must not make the working agent stop
    # or spend a turn reloading state: preserve the same scoped update and
    # retry once against the latest revision. Ownership/fence failures and
    # invalid update content remain real errors and are never retried.
    try:
        saved = update_checkpoint(db, ctx.session_id, state["turn_id"], expected_revision, update)
    except ValueError as exc:
        if "Task revision changed; reload the checkpoint" not in str(exc):
            raise
        latest = current_state(db, ctx.session_id)
        if not latest or latest.get("turn_id") != state["turn_id"]:
            raise
        saved = update_checkpoint(db, ctx.session_id, state["turn_id"], latest["revision"], update)
    from app.agent.task_state import progress_component
    from app.chat_components import validate_spec, save_component
    component = validate_spec(progress_component(saved))
    await save_component(ctx.user_id, ctx.session_id, component)
    counts = {}
    for step in saved.get("steps", []):
        status = step.get("status", "pending")
        counts[status] = counts.get(status, 0) + 1
    checkpoint = saved.get("checkpoint") or {}
    return json.dumps({"status": "ok", "revision": saved["revision"],
                       "task_status": saved["status"],
                       "active_step": checkpoint.get("active_step"),
                       "next_action": checkpoint.get("next_action"),
                       "step_counts": counts,
                       "ui": "task-progress-saved"})

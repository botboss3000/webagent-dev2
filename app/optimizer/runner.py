"""
Optimizer Runner — creates sessions, hands off to chat.py routing.
"""

from __future__ import annotations

import asyncio, json, logging, os, sqlite3, uuid
from datetime import datetime
from typing import Any, Dict

from app.optimizer.config import load_config
from app.optimizer.prefilter import prefilter

logger = logging.getLogger(__name__)
_recently_seen: Dict[str, float] = {}
_RECENT_WINDOW_SEC = 60


def _optimizer_conn():
    from app.db.local import DB_DIR
    return sqlite3.connect(os.path.join(DB_DIR, "optimizer.db"))


async def run_optimizer_async(user_id, session_id, channel="ui", criteria="", feedback="", skill_name="", force=False):
    """
    Lightweight entry point for /optimize and live mode.

    1. Dedup + mode check.
    2. Create optimizer session (opt_role='optimizer' → chat.py routes to optimizer agent).
    3. Run prefilter on target session.
    4. Insert prefilter stats + user feedback as init interactions.
    5. Return session_id for the user to open.

    No direct LLM calls. No iteration loop. No auto-deploy.
    The Planner / Closer agents handle those via tools in chat flow.
    """
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")

    # Skip Worker test sessions to prevent recursive optimizer cascades
    if session_id.startswith('worker-') or session_id.startswith('optimizer-') or session_id.startswith('closer-'):
        return None

    cfg = load_config()
    # Bypass mode + threshold + dedup when force=True (/optimize command).
    if not force:
        from app.optimizer.config import optimizer_enabled, optimizer_min_turns
        if not optimizer_enabled(cfg):
            return None
        # Minimum session length: only auto-run once this session has MORE than
        # N completed turns (short chats are skipped). This MUST run before the
        # dedup timestamp update below — otherwise the sub-threshold turns would
        # still refresh the dedup clock and suppress the first eligible run.
        min_turns = optimizer_min_turns(cfg)
        if min_turns > 0:
            turns = _count_user_turns(user_id, session_id)
            if turns <= min_turns:
                logger.debug("Optimizer: session %s has %s turns, not > threshold %s — skip",
                             session_id, turns, min_turns)
                return None
        # Past the threshold. The dedup window throttles how often a long, active
        # session re-runs the optimizer (at most once per window).
        last = _recently_seen.get(session_id)
        if last is not None and (asyncio.get_running_loop().time() - last) < _RECENT_WINDOW_SEC and not criteria:
            return None
    _recently_seen[session_id] = asyncio.get_running_loop().time()

    run_id = str(uuid.uuid4())
    opt_sid = f"optimizer-{str(uuid.uuid4())[:8]}"

    # ── Create optimizer temp DB (under data/db/, with the other runtime DBs) ──
    from app.db.local import DB_DIR as _db_dir
    os.makedirs(_db_dir, exist_ok=True)
    temp_db_name = "optimizer.db"
    temp_db_path = os.path.join(_db_dir, temp_db_name)
    # Initialize temp DB schema (creates all tables via LocalBackend.__init__)
    from app.db.local import LocalBackend as _OptBackend
    _OptBackend(db_path=temp_db_path)

    # Look up original session title
    _orig_title = _lookup_session_title(user_id, session_id)
    # Insert session row into temp DB so chat.py can find it
    _opt_conn = sqlite3.connect(temp_db_path)
    _opt_conn.execute(
        "INSERT OR IGNORE INTO sessions (id, user_id, title, metadata, created_at, updated_at) "
        "VALUES (?, ?, ?, '{}', ?, ?)",
        (opt_sid, user_id, f"Opt: {_orig_title}", now, now)
    )
    _opt_conn.commit()
    _opt_conn.close()

    # ── Inject target session data + WebAgent context into temp DB ──
    # This gives the Planner the full picture without needing tool calls
    await _inject_session_context(user_id, session_id, temp_db_path, opt_sid)

    # ── Create optimizer session in local.db ──
    _ensure_session(user_id, opt_sid, session_id)
    _store_optimizer_metadata(user_id, opt_sid, session_id, temp_db_path=temp_db_path)  # sets opt_role='optimizer'

    # ── Prefilter target session ──
    pf = await prefilter(user_id, session_id)
    turns = pf.get("turns", 1)
    tokens_est = pf.get("tokens", 100)

    if feedback:
        _insert_opt_msg(user_id, opt_sid, "user", "optimizer:user_feedback",
                        f"User feedback about target session ({session_id[:12]}...): {feedback}",
                        temp_db_path=temp_db_path,
                        from_id=user_id)

    # ── Kickstart: welcome the user to the interactive Optimizer session ──
    # Sends a welcome message as a user message so the optimizer agent sees
    # it naturally when the user opens the session. The optimizer now waits
    # for the user's direction rather than auto-running a pipeline.
    asyncio.create_task(_kickstart_optimizer(user_id, opt_sid, feedback=feedback, welcome=True))

    # ── Log and return ──
    _log_start(run_id, cfg, opt_sid)
    _log_complete(run_id, "running", cfg, opt_sid,
                  summary=f"Session created. {turns} turns, ~{tokens_est} tokens in target session.")
    return opt_sid


def mark_optimizer_run_terminal(
    session_id: str,
    status: str,
    error: str = "",
) -> None:
    """Close a failed/interrupted interactive optimizer run in control storage."""
    if not session_id.startswith("optimizer-") or status == "complete":
        return
    terminal = "interrupted" if status == "interrupted" else "error"
    conn = _optimizer_conn()
    try:
        conn.execute(
            "UPDATE optimizer_runs SET status=?, completed_at=?, "
            "errors=CASE WHEN ? != '' THEN ? ELSE errors END "
            "WHERE session_id=? AND status='running'",
            (
                terminal,
                datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                error or "",
                error or "",
                session_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def _inject_session_context(user_id: str, session_id: str, temp_db_path: str, opt_sid: str) -> None:
    """Inject target session interactions + WebAgent base-prompt context + diagnostics
    + tool usage stats into the optimizer temp DB. Limits to last 50 interactions so
    long sessions don't bloat the prompt."""
    import json
    from app.db.local import DB_DIR

    ctx_data = []
    # ── Target session identity (IDs so the optimizer knows exactly which session it owns) ──
    ctx_data.append("## Target Session")
    ctx_data.append(f"- **Session ID**: `{session_id}`")

    # ── Target session transcript (read from the real runtime local.db) ──
    try:
        from app.db import get_user_db
        local = get_user_db(user_id)._get_conn()
        ics = local.execute(
            "SELECT role, content, tool_name FROM interactions WHERE session_id=? ORDER BY created_at DESC LIMIT 50",
            (session_id,)
        ).fetchall()
        local.close()
        ics.reverse()
        if ics:
            ctx_data.append("## Target Session Interactions")
            for ic in ics:
                role = ic['role']
                content = str(ic['content'] or '')[:500]
                if role == 'user':
                    ctx_data.append(f"[user]: {content}")
                elif role == 'assistant':
                    ctx_data.append(f"[assistant]: {content}")
                elif role == 'tool':
                    tn = ic['tool_name'] or ''
                    ctx_data.append(f"[tool] ({tn}): {content[:200]}")
    except Exception as e:
        logging.warning("Failed to inject session interactions: %s", e)

    # ── WebAgent context = the agent's resolved BASE PROMPTS ──
    try:
        from app.optimizer.prefilter import resolve_session_agent_id
        from app.db import get_db
        agent_id = await resolve_session_agent_id(user_id, session_id)
        if agent_id:
            # Embellish the target session block with the agent ID now that we have it
            ctx_data.append(f"- **Agent ID**: `{agent_id}`")

            slot_titles = {
                "agent": "Agent Identity", "user": "User",
                "skills": "Core Skills", "tasks": "Common Tasks", "misc": "Misc",
            }
            ctx_sections = []
            for s in await get_db().resolve_prompts(agent_id, user_id=user_id):
                title = slot_titles.get(s.get("slot_name"))
                content = s.get("content") or ""
                if title and content.strip():
                    ctx_sections.append(f"### {title} ({s['slot_name']})")
                    ctx_sections.append(content[:3000])
            if ctx_sections:
                ctx_data.append("")
                ctx_data.append("## WebAgent Context")
                ctx_data.extend(ctx_sections)
            
            # ── Inject target agent's abilities and tools ──
            try:
                agent = await get_db().get_agent_by_id(agent_id)
                if agent:
                    ctx_data.append("")
                    ctx_data.append("## Target Agent Config")
                    ctx_data.append(f"- **Name**: {agent.get('name', 'unknown')}")
                    ctx_data.append(f"- **Model**: {agent.get('model', 'default')}")
                    ctx_data.append(f"- **Temperature**: {agent.get('temperature', 'default')}")
                    ctx_data.append(f"- **Max turn count**: {agent.get('max_turn_count', 'default')}")
                    # Abilities
                    abilities = agent.get('abilities', []) or []
                    if abilities:
                        ctx_data.append(f"- **Abilities**: {', '.join(abilities)}")
                    else:
                        ctx_data.append("- **Abilities**: (none)")
                    # Tool permissions (from agent_connections or tool_overrides)
                    try:
                        tool_rows = get_db()._get_conn().execute(
                            "SELECT tool_name, permission FROM agent_tool_overrides WHERE agent_id=?",
                            (agent_id,)
                        ).fetchall()
                        if tool_rows:
                            overrides = []
                            for tr in tool_rows:
                                overrides.append(f"{tr['tool_name']}={tr['permission']}")
                            ctx_data.append(f"- **Tool overrides**: {', '.join(overrides)}")
                    except Exception:
                        pass
            except Exception as e:
                logging.debug("Failed to inject agent config: %s", e)

    except Exception as e:
        logging.warning("Failed to inject agent base-prompt context: %s", e)

    # ── Inject recent diagnostics (errors/warnings from flight recorder) ──
    try:
        from app.db import get_user_db
        raw = getattr(get_user_db(user_id), '_get_conn', None)
        if raw:
            diag_conn = raw()
            try:
                diag_rows = diag_conn.execute(
                    "SELECT level, message, created_at FROM diagnostics "
                    "WHERE level IN ('error','warning') AND user_id=? "
                    "ORDER BY created_at DESC LIMIT 20",
                    (user_id,)
                ).fetchall()
            finally:
                diag_conn.close()
            if diag_rows:
                ctx_data.append("")
                ctx_data.append("## Recent Diagnostics (Errors & Warnings)")
                for dr in diag_rows:
                    ctx_data.append(f"[{dr['level']}] {dr['message'][:200]} (at {dr['created_at']})")
    except Exception as e:
        logging.debug("Failed to inject diagnostics: %s", e)

    # ── Inject tool usage stats from the target session ──
    try:
        from app.db import get_user_db
        local = get_user_db(user_id)._get_conn()
        tool_stats = local.execute(
            "SELECT tool_name, COUNT(*) as count FROM interactions "
            "WHERE session_id=? AND role='tool' AND tool_name IS NOT NULL "
            "GROUP BY tool_name ORDER BY count DESC",
            (session_id,)
        ).fetchall()
        local.close()
        if tool_stats:
            ctx_data.append("")
            ctx_data.append("## Tool Usage in Target Session")
            for ts in tool_stats:
                ctx_data.append(f"- **{ts['tool_name']}**: called {ts['count']} times")
    except Exception as e:
        logging.debug("Failed to inject tool stats: %s", e)

    if ctx_data:
        content = "\n".join(ctx_data)
        try:
            _now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            c = sqlite3.connect(temp_db_path)
            c.execute(
                "INSERT INTO interactions (id,session_id,role,content,source,channel,from_id,created_at) "
                "VALUES (?,?,'assistant',?,'context','optimizer',?,?)",
                (str(uuid.uuid4()), opt_sid, content, user_id, _now),
            )
            c.commit()
            c.close()
        except Exception as e:
            logging.warning("Failed to write context injection: %s", e)


async def _kickstart_optimizer(user_id: str, opt_sid: str, feedback: str = "", welcome: bool = False) -> None:
    """Welcome the user to the interactive Optimizer session (or auto-trigger for legacy mode).
    chat.py routes messages to the optimizer agent via resolve_agent()."""
    import asyncio, httpx, os, logging as _log
    try:
        port = os.environ.get('PORT', '8080')
        _log.warning(f"Kickstarting Optimizer for session {opt_sid} (welcome={welcome})")

        if welcome:
            msg = (
                "👋 **Optimizer ready.** I've loaded the target session context.\n\n"
                "What would you like me to do?\n"
                "• **Monitor** this session for issues\n"
                "• **Find bugs** in the codebase or logs\n"
                "• **Optimize** the agent's prompts\n"
                "• **Audit** tool usage and permissions\n"
                "• **Analyze** what happened and suggest improvements\n"
                "• Check the **database** for error patterns\n"
                "• **Compare** the target agent's approach with a reference\n"
                "• Or anything else — just tell me what you need!"
            )
            if feedback:
                msg += f"\n\nI also see your note: _{feedback}_"
        else:
            msg = "Please analyze the main session and propose improvements."
            if feedback:
                msg += f" The user's feedback: {feedback}"

        # Mint a short-lived JWT so this loopback POST passes the chat endpoint's
        # caller-identity check (assert_caller_is). Without it the request carries
        # no token and is rejected 401 → surfaced as 500, silently killing the
        # Planner before it ever starts. Same technique the orchestration ability
        # uses for its spawn loopbacks (_internal_auth_headers). We act on behalf
        # of the session's own user, so minting that user's own token is legit.
        headers = {}
        try:
            from app.auth.jwt import create_access_token
            headers["Authorization"] = (
                f"Bearer {create_access_token(username=user_id, user_id=user_id)}"
            )
        except Exception as _auth_err:
            _log.warning(f"Could not mint kickstart auth token for {user_id}: {_auth_err}")

        # The kickstart fires from the tail of the target turn, so the very first
        # Planner request races the turn-finalization + optimizer-setup writes and
        # the one-time materialization of the optimizer agent — a transient
        # SQLite "database is locked" that surfaces as a 500. Settle briefly, then
        # retry on 5xx / connection errors with backoff. The first message is
        # idempotent ("analyze…") and the 500 happens at agent-resolve time, before
        # any Planner work, so a retry can't duplicate work.
        await asyncio.sleep(0.6)
        delays = [1.0, 2.5, 5.0]
        async with httpx.AsyncClient(timeout=120.0) as client:
            for attempt in range(len(delays) + 1):
                try:
                    resp = await client.post(
                        f"http://127.0.0.1:{port}/api/v1/chat",
                        json={"message": msg, "user_id": user_id, "session_id": opt_sid,
                               "execution_mode": "auto"},
                        headers=headers,
                    )
                    if resp.status_code < 500 or attempt >= len(delays):
                        _log.warning(f"Kickstart Planner responded: {resp.status_code}"
                                     + (f" (attempt {attempt + 1})" if attempt else ""))
                        break
                    _log.warning(f"Kickstart Planner got {resp.status_code}; "
                                 f"retrying in {delays[attempt]}s (attempt {attempt + 1})")
                except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as ce:
                    if attempt >= len(delays):
                        _log.warning(f"Kickstart Planner connection failed: {ce}")
                        break
                    _log.warning(f"Kickstart Planner connect error; retrying in {delays[attempt]}s")
                await asyncio.sleep(delays[attempt])
    except Exception as e:
        _log.warning(f"Kickstart Planner failed (non-fatal): {e}")


# ── Helpers ──

def _retry_db_write(fn, max_attempts=5, delay=0.3):
    import time
    for a in range(max_attempts):
        try:
            return fn()
        except Exception as e:
            if "locked" in str(e).lower() and a < max_attempts - 1:
                time.sleep(delay * (a + 1))
            else:
                raise


def _count_user_turns(user_id, session_id) -> int:
    """Number of completed user turns in a target session (one user message =
    one turn). Used to enforce the 'run every N turns' cadence. Best-effort:
    any DB error returns 0 so the caller simply skips this cycle."""
    try:
        from app.db import get_user_db
        raw = getattr(get_user_db(user_id), '_get_conn', None)
        if not raw:
            return 0
        c = raw()
        try:
            row = c.execute(
                "SELECT COUNT(*) FROM interactions WHERE session_id=? AND role='user'",
                (session_id,),
            ).fetchone()
            return int(row[0]) if row else 0
        finally:
            c.close()
    except Exception:
        return 0


def _store_optimizer_metadata(uid, sid, target_sid, temp_db_path=None):
    """Store the original target session + temp_db_path in optimizer session metadata."""
    from app.db import get_user_db
    db = get_user_db(uid)
    raw = getattr(db, '_get_conn', None)
    if raw:
        meta = json.dumps({
            "opt_role": "optimizer",
            "target_session": target_sid,
            "temp_db_path": temp_db_path,
            "execution_mode": "auto",
        })

        def _do():
            c = raw()
            c.execute(
                "UPDATE sessions SET metadata=? WHERE id=?",
                (meta, sid),
            )
            c.commit()
            c.close()

        _retry_db_write(_do)


def _ensure_session(uid, sid, orig):
    from app.db import get_user_db
    db = get_user_db(uid)
    raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            _now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            _title = _lookup_session_title(uid, orig)
            c = raw()
            c.execute(
                "INSERT OR IGNORE INTO sessions (id,user_id,title,created_at,updated_at) "
                "VALUES (?,?,?,?,?)",
                (sid, uid, f"Opt: {_title}", _now, _now),
            )
            c.commit()
            c.close()

        _retry_db_write(_do)


def _insert_opt_msg(uid, sid, role, source, content, *, from_id=None, to_id=None, output_data=None, temp_db_path=None):
    """Insert an interaction into the optimizer's temp DB (or local.db fallback)."""
    import json
    
    if temp_db_path:
        def _do_temp():
            _now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            c = sqlite3.connect(temp_db_path)
            c.execute(
                "INSERT INTO interactions (id,session_id,role,content,source,channel,from_id,to_id,output,created_at) "
                "VALUES (?,?,?,?,?,'optimizer',?,?,?,?)",
                (str(uuid.uuid4()), sid, role, content, source, from_id, to_id, output_data, _now),
            )
            c.commit()
            c.close()
        _retry_db_write(_do_temp)
        return

    from app.db import get_user_db
    db = get_user_db(uid)
    raw = getattr(db, '_get_conn', None)
    if raw:
        def _do():
            _now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            c = raw()
            c.execute(
                "INSERT INTO interactions (id,session_id,role,content,source,channel,from_id,to_id,output,created_at) "
                "VALUES (?,?,?,?,?,'optimizer',?,?,?,?)",
                (str(uuid.uuid4()), sid, role, content, source, from_id, to_id, output_data, _now),
            )
            c.commit()
            c.close()
        _retry_db_write(_do)


def _log_start(rid, cfg, sid):
    _log_run(rid, "running", cfg, sid)


def _log_complete(rid, st, cfg, sid, **kw):
    _log_run(rid, st, cfg, sid, **kw)


def _log_run(rid, status, config, sid, skills_analyzed=0, proposals_generated=0, proposals_deployed=0, summary="", errors=None):
    try:
        raw = _optimizer_conn
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        completed = now if status in ("success", "failed") else None
        errs = json.dumps(errors) if errors else None
        cfg_json = json.dumps(config)

        def _do():
            c = raw()
            c.execute(
                "CREATE TABLE IF NOT EXISTS optimizer_runs "
                "(id TEXT PRIMARY KEY, status TEXT DEFAULT 'running', started_at TEXT, "
                "completed_at TEXT, skills_analyzed INTEGER DEFAULT 0, "
                "proposals_generated INTEGER DEFAULT 0, proposals_deployed INTEGER DEFAULT 0, "
                "errors TEXT, summary TEXT, config_snapshot TEXT, session_id TEXT)"
            )
            ex = c.execute("SELECT id FROM optimizer_runs WHERE id=?", (rid,)).fetchone()
            if ex:
                c.execute(
                    "UPDATE optimizer_runs SET status=?,completed_at=?,skills_analyzed=?,"
                    "proposals_generated=?,proposals_deployed=?,errors=?,summary=? WHERE id=?",
                    (status, completed, skills_analyzed, proposals_generated, proposals_deployed, errs, summary, rid),
                )
            else:
                c.execute(
                    "INSERT INTO optimizer_runs (id,status,started_at,completed_at,skills_analyzed,"
                    "proposals_generated,proposals_deployed,errors,summary,config_snapshot,session_id) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, status, now, completed, skills_analyzed, proposals_generated, proposals_deployed, errs, summary, cfg_json, sid),
                )
            c.commit()
            c.close()

        _retry_db_write(_do)
    except Exception as e:
        logger.warning("_log_run: %s", e)


def _lookup_session_title(user_id: str, session_id: str) -> str:
    """Get the title of a target session, falling back to the session id on error."""
    try:
        from app.db import get_user_db
        local = get_user_db(user_id)._get_conn()
        row = local.execute(
            "SELECT title FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        local.close()
        if row and row[0]:
            return str(row[0])
    except Exception as e:
        logger.debug("Failed to lookup session title: %s", e)
    return session_id[:12]

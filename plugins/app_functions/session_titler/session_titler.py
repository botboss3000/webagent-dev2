"""Session Namer — a background APP FUNCTION (not an agent ability).

Auto-names a chat session ONCE from its first few user messages using a tiny
LLM call, then locks the name so it is never silently re-titled.
Pushes a live ``session_title`` event over the per-user WebSocket so the
chat-panel header shows a spinner while naming and swaps in the new name.

Failures are verified, not swallowed: a naming attempt only counts as a
success once the title is PERSISTED. A blank LLM result (after retries), a
write error, or a vanished session row is stamped onto the session's metadata
(``auto_title_failed`` / ``auto_title_last_error`` / ``auto_title_last_attempt_at``)
and recorded as a ``recovery``-category diagnostic, and the WebSocket always
gets a final event so the header never hangs on "generating". A bounded
recovery sweep (``start_sweep`` / ``stop_sweep``, leader-registered) re-triggers
naming for sessions still stuck on a fallback "New: …" title.

The model/client are RESOLVED, not read from env: the chat loop stopped
exporting the provider to ``os.environ`` (07-30, to stop concurrent chats
cross-wiring credentials), so env-based resolution silently no-ops on a fresh
boot. The namer resolves the session's provider config itself
(``apply_env=False``), exactly like the Auto-rename endpoint does.

As a TURN_HOOK app function, it exports ``TURN_HOOK`` — an async callable that
is dispatched from ``app/api/chat.py`` after every chat turn. This makes the
naming strategy swappable: toggle this app function off in App Settings ▸ App
Functions and enable a different one (e.g. "first-message only", "rule-based
naming", "no naming") to change behaviour without editing core code.

It also backs the on-demand "Auto rename" action (POST /sessions/{id}/auto-title):
called with ``force=True`` it re-names ANY session — even a locked,
manually-renamed, or optimizer-/closer-/slash- session — from a larger sample
of its user messages.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, List, Optional

logger = logging.getLogger(__name__)

# How many opening user messages to sample when deriving the title. The
# background namer generates ONE name and then locks — it no longer refines
# across turns (that re-titling churn cost an LLM call per turn and re-named
# sessions the user had already seen).
_TITLE_TURN_LIMIT = 3

# Keep each message excerpt small so the prompt stays cheap.
_MSG_CLIP = 600

# Output budget for the title call. A title is only a handful of tokens, but
# REASONING models spend their budget *thinking* before emitting any visible
# text — a tight cap (the old value was 24) truncates mid-thought and returns
# EMPTY content. Give every model enough room to finish and still print the
# title; _clean_title trims whatever comes back to <=10 words, so a generous cap
# is harmless and barely costs more (the model stops right after the short title
# on a normal completion). Must stay model-agnostic — the admin picks the model.
_MAX_TITLE_TOKENS = 512

# Some models/providers intermittently return null/empty content with NO error
# (observed ~1-in-4 on a cheap flash model: finish_reason "stop", content None).
# Retry on both an empty result AND an exception so one unlucky blank doesn't
# strand the session on its fallback name. Robust for any model.
_TITLE_ATTEMPTS = 3

# Brief gap between retries — lets a momentarily-degraded upstream recover
# instead of being hammered with identical back-to-back calls. The namer runs
# in the background after a turn, so this delay is never user-visible.
_RETRY_BACKOFF_S = 0.4

# The title UPDATE competes for SQLite's single writer slot with the other
# turn-end writers (memory save, run-state finalize). If it loses the race it
# raises "database is locked"; retry a few times with a short, growing backoff so
# a transient contention spike doesn't strand the session on its fallback name.
# Background task → these awaits are never user-visible.
_WRITE_ATTEMPTS = 5
_WRITE_BACKOFF_S = 0.25

# ── Tier-2 recovery sweep — re-trigger naming for sessions stuck on a fallback
# "New: …" title (their original turn-hook attempt failed or never ran). Bounded
# so a persistently-down model cannot burn unlimited LLM calls: at most
# _SWEEP_MAX_PER_TICK sessions per sweep, each at most once per
# _SWEEP_RETRY_COOLDOWN_S, and only once a session has been idle at least
# _SWEEP_MIN_AGE_S (so a live turn-hook still in flight isn't double-fired).
_SWEEP_STARTUP_DELAY_S = 30     # let boot settle (and the first turn-hooks run)
_SWEEP_INTERVAL_S = 60          # sweep every minute
_SWEEP_MAX_PER_TICK = 10        # at most this many sessions named per sweep
_SWEEP_MIN_AGE_S = 60           # only sessions idle at least this long
_SWEEP_RETRY_COOLDOWN_S = 60    # don't re-attempt a session this soon after a failure

_SYSTEM_PROMPT = (
    "You write a very short title for a chat session, capturing what the user "
    "wants. Rules: 1 to 10 words, strongly prefer 2 to 5 words. Use Title Case. "
    "No surrounding quotes and no trailing punctuation. Ignore greetings, "
    "pleasantries and filler \u2014 focus on the actual task or topic. If the intent "
    "is still vague, summarize the topic as best you can. Reply with the title "
    "only, nothing else."
)


async def _system_prompt(agent_id: str = "") -> str:
    """The titler's system prompt, extended by THIS agent's System-role
    directive (grep ROLE-DIRECTIVE-INJECT) when one is set."""
    base = _SYSTEM_PROMPT
    try:
        from app.admin.settings import get_agent_model_directives
        extra = (await get_agent_model_directives(agent_id or "")).get("system") or ""
    except Exception:  # noqa: BLE001
        extra = ""
    return f"{extra}\n\n{base}" if extra else base

EmitFn = Callable[[dict], Awaitable[None]]


def _model() -> str:
    """Return the best available model for the titler, preferring the dedicated
    CLASSIFIER_MODEL env var (which the classifier also respects), then falling
    back to the normal chain.  When the base URL is a DeepSeek endpoint and the
    model carries a non-DeepSeek prefix, auto-correct to a safe default so the
    titler doesn't 400 on every call."""
    custom = os.environ.get("CLASSIFIER_MODEL")
    if custom:
        return custom
    model = os.environ.get("LLM_MODEL") or os.environ.get("OPENROUTER_MODEL") or ""
    base_url = os.environ.get("LLM_BASE_URL") or os.environ.get("OPENROUTER_BASE_URL") or ""
    if "deepseek" in base_url.lower() and model and not model.lower().startswith("deepseek"):
        return "deepseek-v4-flash"
    return model


def _clean_title(raw: str) -> str:
    """Normalize the model's reply into a tidy 1\u201310 word title."""
    t = (raw or "").strip()
    if not t:
        return ""
    # First non-empty line only.
    t = next((ln.strip() for ln in t.splitlines() if ln.strip()), "")
    # Strip wrapping quotes the model sometimes adds, then trailing punctuation.
    t = t.strip('"').strip("'").strip().rstrip(".,!?;:").strip()
    # Hard clamp to 10 words.
    words = t.split()
    if len(words) > 10:
        t = " ".join(words[:10])
    return t[:80]


async def _subscription_title(messages: List[dict], *, model: str,
                              provider_config: dict, user_id: Optional[str],
                              session_id: Optional[str], agent_id: Optional[str]) -> str:
    """Title through the ChatGPT subscription Responses transport.

    ``openai_subscription`` deliberately uses ``codex://chatgpt`` as a routing
    marker, not an HTTP base URL.  It therefore cannot use ``AsyncOpenAI``;
    mirror the normal loop's OAuth-backed Responses request instead.
    """
    from app import openai_subscription as subscription
    from app.agent.response_chaining import build_responses_request, open_responses_stream

    auth = await subscription.transport_auth(
        scope=str(provider_config.get("credential_scope") or "platform"),
        entry_id=str(provider_config.get("entry_id") or ""),
        user_id=str(provider_config.get("_credential_user_id") or user_id or ""),
        agent_id=str(provider_config.get("_credential_agent_id") or agent_id or ""),
    )
    request_key = hashlib.sha256(
        f"{user_id or ''}:{session_id or ''}:title".encode()
    ).hexdigest()[:40]
    headers = {
        "originator": "webagent",
        "User-Agent": "webagent/1.0",
        "session-id": f"wa-{request_key}",
    }
    if auth.get("account_id"):
        headers["ChatGPT-Account-Id"] = auth["account_id"]
    if auth.get("residency"):
        headers["x-openai-internal-codex-residency"] = auth["residency"]

    body = build_responses_request(
        model=model, messages=messages, tools=None,
        max_output_tokens=_MAX_TITLE_TOKENS, provider_family_name="openai",
    )
    # Match the subscription transport in the main agent loop.
    body["store"] = False
    body.pop("max_output_tokens", None)
    body["include"] = ["reasoning.encrypted_content"]
    stream = await open_responses_stream(
        base_url=str(provider_config.get("base_url") or ""),
        api_key=auth["access_token"], family="openai", body=body,
        request_url=subscription.RESPONSES_ENDPOINT, extra_headers=headers,
    )
    chunks: List[str] = []
    try:
        async for chunk in stream:
            for choice in getattr(chunk, "choices", []) or []:
                content = getattr(getattr(choice, "delta", None), "content", None)
                if content:
                    chunks.append(str(content))
    finally:
        await stream.close()
    return _clean_title("".join(chunks))


async def _llm_title(user_messages: List[str], *, model: Optional[str] = None,
                     client: Any = None, user_id: Optional[str] = None,
                     session_id: Optional[str] = None,
                     agent_id: Optional[str] = None,
                     provider_config: Optional[dict] = None) -> str:
    """One lightweight LLM call → a short title. Returns "" only when every
    attempt fails or comes back blank.

    ``model``/``client`` are the resolved provider. ChatGPT subscription runs
    use their dedicated OAuth Responses transport because ``codex://chatgpt``
    is not a valid OpenAI-compatible HTTP base URL.
    """
    if not model:
        model = _model()
    if not model:
        logger.info("session_namer: no model configured; skipping")
        return ""
    is_subscription = (provider_config or {}).get("provider") == "openai_subscription"
    if client is None and not is_subscription:
        from app.agent.loop import _get_client
        client = _get_client()

    numbered = "\n".join(
        f"{i + 1}. {(m or '').strip()[:_MSG_CLIP]}"
        for i, m in enumerate(user_messages)
    )
    user_msg = (
        "Here are the first user message(s) of a chat session:\n\n"
        f"{numbered}\n\n"
        "Give a short title for this session."
    )
    messages = [
        {"role": "system", "content": await _system_prompt(agent_id)},
        {"role": "user", "content": user_msg},
    ]

    for attempt in range(1, _TITLE_ATTEMPTS + 1):
        try:
            if is_subscription:
                title = await _subscription_title(
                    messages, model=model, provider_config=provider_config or {},
                    user_id=user_id, session_id=session_id, agent_id=agent_id,
                )
                usage = None
                finish_reason = "responses"
            else:
                resp = await client.chat.completions.create(
                    model=model, messages=messages, temperature=0.3,
                    max_tokens=_MAX_TITLE_TOKENS,
                )
                choice = resp.choices[0]
                title = _clean_title(choice.message.content or "")
                usage = getattr(resp, "usage", None)
                finish_reason = getattr(choice, "finish_reason", "?")
            if title:
                try:
                    if usage:
                        from plugins.billing.usage import record_background_usage
                        await record_background_usage(
                            model=model,
                            input_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                            output_tokens=getattr(usage, "completion_tokens", 0) or 0,
                            label="title", session_id=session_id,
                            user_id=user_id, agent_id=agent_id,
                        )
                except Exception:
                    pass
                return title
            logger.warning(
                "session_namer: empty title from %s (attempt %d/%d, finish=%s)",
                model, attempt, _TITLE_ATTEMPTS, finish_reason,
            )
        except Exception as e:
            logger.warning(
                "session_namer: LLM call failed (attempt %d/%d): %s",
                attempt, _TITLE_ATTEMPTS, e,
            )
        if attempt < _TITLE_ATTEMPTS:
            await asyncio.sleep(_RETRY_BACKOFF_S)

    logger.warning("session_namer: no title after %d attempts (model=%s)",
                   _TITLE_ATTEMPTS, model)
    return ""


async def _maybe_title_session(
    db: Any,
    user_id: str,
    session_id: str,
    emit: Optional[EmitFn] = None,
    *,
    force: bool = False,
    sample_limit: int = _TITLE_TURN_LIMIT,
) -> None:
    """Generate the session name ONCE from the opening user messages.

    Background (turn-hook) behaviour: it produces a single title and then locks
    it, so the user never sees the name change out from under them. It no-ops
    once the name is locked (after a successful auto-name or a manual rename)
    and skips the special optimizer-/closer-/slash- sessions. ``force=True``
    (the on-demand "Auto rename" action) overrides BOTH gates — any session can
    be re-named — and re-locks the new name. ``sample_limit`` bounds how many
    opening user messages the title is derived from. Never raises — best-effort.
    """
    try:
        # Skip special sessions that are named by their own flows — unless this
        # is an explicit on-demand rename (force).
        if not force and session_id.startswith(("optimizer-", "closer-", "slash-")):
            return

        # Don't auto-title if the user (or a prior lock) already settled the
        # name — unless force re-names it deliberately.
        _title, meta = _read_session(db, session_id)
        if not force and meta.get("auto_title_locked"):
            return
        # "Already named" gate: a session with a real (non-fallback) title is
        # left alone even if it was never locked (e.g. a title written before
        # the lock-always change). Only blank / "New …" fallback titles are
        # auto-named. Mirrors the recovery sweep's candidate filter.
        if not force and _has_real_title(_title):
            return

        # Pull ONLY the opening user messages (oldest first). The background
        # namer titles a chat from its first few turns and then locks — it never
        # needs the rest of the transcript, so we ask the DB for just these
        # instead of loading (and immediately discarding) the entire session
        # into memory. That full load was one of the heaviest allocations at
        # every turn's end and a candidate to tip a memory-stressed process
        # over. An on-demand rename samples more (up to ``sample_limit``) so an
        # established session is titled from its whole conversation.
        first_msgs = await db.fetch_first_user_messages(
            user_id, session_id, sample_limit
        )
        if not first_msgs:
            return

        # Name once, then lock: the session keeps its first auto-title so the
        # user never sees it change underneath them. (Historically the namer
        # refined the title across the first few turns — that re-titling is now
        # off.) The lock is only persisted when a title is actually produced
        # (see _write_title), so a blank/failed attempt still retries next turn.
        lock = True

        async def _push(ev: dict) -> None:
            if emit is None:
                return
            try:
                await emit(ev)
            except Exception:
                pass

        # Spinner on.
        await _push({"type": "session_title", "status": "generating",
                     "session_id": session_id})

        # Resolve the session's model + client WITHOUT touching process env (the
        # run loop stopped exporting env on 07-30, so env-based resolution is
        # empty on a fresh boot — the original reason naming silently failed).
        title = ""
        _fail: Optional[str] = None
        _model_resolved, _client, _provider_config = await _resolve_llm_config(
            db, user_id, _session_agent_id(db, session_id), session_id)
        if not _model_resolved:
            _fail = "no LLM model/provider configured for this session"
        else:
            title = await _llm_title(
                first_msgs, model=_model_resolved, client=_client,
                user_id=user_id, session_id=session_id,
                agent_id=_session_agent_id(db, session_id),
                provider_config=_provider_config)

        # Tier-1 verification: a naming attempt only counts as a success once
        # the title is actually PERSISTED. Any failure — blank LLM result after
        # retries, a write error, a vanished session row — is stamped onto the
        # session's metadata and recorded as a 'recovery'-category diagnostic so
        # it is visible and inspectable, instead of silently stranding the
        # session on its fallback name.
        if title:
            try:
                await _write_title(db, session_id, title, lock, force=force)
                logger.info("session_namer: named %s -> %r (lock=%s)",
                            session_id[:12], title, lock)
            except Exception as _we:  # noqa: BLE001
                _fail = f"title write failed: {_we}"
                logger.warning("session_namer: %s (session=%s)", _fail,
                               str(session_id)[:12], exc_info=True)
        elif _fail is None:
            _fail = f"LLM returned no title after {_TITLE_ATTEMPTS} attempts"

        if _fail is not None:
            await _stamp_failure(db, session_id, _fail)
            _record_recovery_diag(
                "warning", f"session naming failed: {_fail}", session_id,
                user_id, {"error": _fail, "model": _model() or ""})

        # Spinner off — ALWAYS fires, even on failure, so the header never hangs
        # on 'generating'. On failure we send the CURRENT title (never a name
        # that didn't persist).
        await _push({"type": "session_title", "status": "done",
                     "session_id": session_id,
                     "title": (title if _fail is None else _title) or ""})
    except Exception as e:
        # WARNING, not DEBUG: this used to swallow the failure completely, so the
        # namer's only observable symptom was a session stuck on its fallback name
        # with metadata=None. Surface it (with traceback) so a real failure is
        # diagnosable instead of invisible — and stamp it on the session so the
        # failure state is inspectable and the recovery sweep backs off.
        logger.warning("session_namer: maybe_title_session failed for %s: %s",
                       str(session_id)[:12], e, exc_info=True)
        try:
            await _stamp_failure(db, session_id, f"hook failed: {e}")
        except Exception:
            pass


def _read_session(db: Any, session_id: str):
    """Return (title, metadata_dict) for the session, or (None, {}) if missing."""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT title, metadata FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return None, {}
    title = row[0]
    try:
        meta = json.loads(row[1]) if row[1] else {}
    except (json.JSONDecodeError, TypeError):
        meta = {}
    return title, (meta if isinstance(meta, dict) else {})


def _session_agent_id(db: Any, session_id: str) -> Optional[str]:
    """The session's agent_id (None when missing/unknown)."""
    conn = db._get_conn()
    try:
        row = conn.execute(
            "SELECT agent_id FROM sessions WHERE id=?", (session_id,)
        ).fetchone()
        return row["agent_id"] if row else None
    finally:
        conn.close()


def _has_real_title(title: Optional[str]) -> bool:
    """True when the session already carries a real (non-fallback) name.

    Fallback titles are the blank / 'New Session' / 'New: …' placeholders the
    namer is supposed to replace; anything else is a name the user has seen, so
    the namer leaves it alone (unless force=True re-names deliberately).
    """
    t = (title or "").strip()
    if not t:
        return False
    if t == "New Session":
        return False
    if t.startswith("New:"):
        return False
    return True


def _runtime_model_name(config: dict) -> str:
    """Strip a 'provider/model' prefix for native (non-OpenRouter) APIs — the
    same normalization _apply_config_to_env applies to env vars."""
    model = str(config.get("model") or "")
    base_url = str(config.get("base_url") or "")
    if base_url and "openrouter.ai" not in base_url and "/" in model:
        return model.split("/", 1)[-1]
    return model


async def _resolve_llm_config(db: Any, user_id: str, agent_id: Optional[str],
                              session_id: str):
    """Resolve (model, client) for this session's agent WITHOUT touching env.

    The chat loop consumes the provider config directly (apply_env=False since
    07-30) and never exports it to os.environ, so the namer must resolve the
    config itself — the same resolution the Auto-rename endpoint performs.
    Returns ``(model, client, effective_config)``. For the ChatGPT
    subscription transport the client is ``None`` because ``codex://chatgpt``
    is a routing marker, not an HTTP endpoint. Returns ``(None, None, {})``
    when no model/provider is configured or resolution fails.
    """
    try:
        from app.admin.settings import apply_provider_for_run
        from app.agent.loop import _get_client
        agent_rec = None
        if agent_id:
            try:
                agent_rec = await db.get_agent_by_id(agent_id)
            except Exception:  # noqa: BLE001
                agent_rec = None
        effective = await apply_provider_for_run(
            user_id, agent_rec, session_id, apply_env=False
        )
        model = _runtime_model_name(effective)
        base_url = effective.get("base_url")
        if not model or not base_url:
            return None, None, {}
        if effective.get("provider") == "openai_subscription":
            return model, None, effective
        client = _get_client(base_url, effective.get("api_key"))
        return model, client, effective
    except Exception as e:  # noqa: BLE001
        logger.warning("session_namer: provider resolution failed: %s", e)
        return None, None, {}


async def _write_meta(db: Any, session_id: str, meta: dict,
                      title: Optional[str] = None) -> None:
    """Persist the session's metadata (and optionally a new title).

    Shares the lock-contention retry loop with _write_title, and VERIFIES the
    row actually updated: ``rowcount`` must be 1, so a vanished session row
    raises instead of silently "succeeding" while the caller reports a title
    that never landed in the database.
    """
    last_err: Optional[Exception] = None
    for attempt in range(1, _WRITE_ATTEMPTS + 1):
        conn = db._get_conn()
        try:
            if title is None:
                cur = conn.execute(
                    "UPDATE sessions SET metadata=? WHERE id=?",
                    (json.dumps(meta), session_id),
                )
            else:
                cur = conn.execute(
                    "UPDATE sessions SET title=?, metadata=? WHERE id=?",
                    (title, json.dumps(meta), session_id),
                )
            conn.commit()
            if cur.rowcount == 0:
                raise RuntimeError(
                    f"session row not found (rowcount=0): {session_id}")
            return
        except Exception as e:  # noqa: BLE001
            last_err = e
            # Only a lock contention is worth retrying; anything else (including
            # the rowcount check above) is a real error and surfaces immediately.
            if "lock" not in str(e).lower():
                raise
        finally:
            conn.close()
        if attempt < _WRITE_ATTEMPTS:
            await asyncio.sleep(_WRITE_BACKOFF_S * attempt)
    if last_err is not None:
        raise last_err


async def _write_title(db: Any, session_id: str, title: str, lock: bool,
                       *, force: bool = False) -> None:
    """Persist the new title and, when locking, mark auto_title_locked in metadata.

    Re-reads the lock flag immediately before writing: the hooks fire
    fire-and-forget per turn, so two turns' titler tasks can overlap. Once a
    name is locked, only a forced (on-demand "Auto rename") write may proceed —
    a background write must never clobber a name the user has already seen.

    A successful write also clears any prior failure state, so the session reads
    healthy again. Retries on a transient "database is locked" (see
    _WRITE_ATTEMPTS): the write contends with other turn-end writers for
    SQLite's single writer slot.
    """
    _title, meta = _read_session(db, session_id)
    if meta.get("auto_title_locked") and not force:
        return
    if lock:
        meta["auto_title_locked"] = True
    for _k in ("auto_title_failed", "auto_title_last_error",
               "auto_title_last_attempt_at"):
        meta.pop(_k, None)
    await _write_meta(db, session_id, meta, title=title)


async def _stamp_failure(db: Any, session_id: str, err: str) -> None:
    """Record a naming failure ON the session — count, last error, last attempt
    timestamp — so the failure is inspectable in the DB and the recovery sweep
    can back off. Best-effort; never raises."""
    try:
        _title, meta = _read_session(db, session_id)
        meta["auto_title_failed"] = int(meta.get("auto_title_failed") or 0) + 1
        meta["auto_title_last_error"] = str(err)[:300]
        from app.db.local import _iso_now
        meta["auto_title_last_attempt_at"] = _iso_now()
        await _write_meta(db, session_id, meta)
    except Exception as _se:  # noqa: BLE001
        logger.warning("session_namer: could not stamp failure for %s: %s",
                       str(session_id)[:12], _se)


def _record_recovery_diag(level: str, message: str, session_id: str,
                          user_id: str, detail: dict) -> None:
    """Structured 'recovery'-category diagnostic — the same bucket the run
    watchdog writes to — so a naming failure is visible on the diagnostics page,
    not just buried in the logs. Never raises."""
    try:
        from app.agent.diagnostics import record as _diag
        _diag(level, "recovery", message, source="session_namer",
              detail=detail, session_id=session_id, user_id=user_id)
    except Exception:  # noqa: BLE001
        pass


# ── TURN_HOOK: dispatched by app/api/chat.py after every chat turn ──
# Signature: async hook(db, user_id, session_id, emit)
TURN_HOOK = _maybe_title_session


# ── Tier-2 recovery sweep (leader-registered singleton, see app/main.py) ──
# The turn-hook only fires when a turn completes, so a session whose very first
# naming attempt failed (or whose turn completed through a path that skips
# hooks) can stay stuck on its "New: …" fallback forever. This sweep periodically
# finds those sessions and re-triggers naming — the watchdog-analog: bounded,
# cooldown-aware, and it writes the same failure-state metadata + diagnostics.


def _parse_ts(value: Any) -> Optional[datetime]:
    """Parse a stored ISO timestamp (UTC, microseconds) or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except Exception:  # noqa: BLE001
        return None


async def _find_unnamed_sessions(db: Any, limit: int = _SWEEP_MAX_PER_TICK) -> List[dict]:
    """Sessions that look auto-namable but never got a name: still on a fallback
    "New: …" / blank title, active, not a special session, and with real user
    messages to name from. Same ``sessions`` table the turn-hook namer reads and
    writes, so the sweep sees exactly what the hook sees."""
    conn = db._get_conn()
    try:
        rows = conn.execute(
            """
            SELECT s.id, s.user_id, s.agent_id, s.title, s.metadata, s.updated_at
            FROM sessions s
            WHERE s.status = 'active'
              AND (s.title IS NULL OR s.title = '' OR s.title = 'New Session'
                   OR s.title LIKE 'New:%')
              AND s.id NOT LIKE 'optimizer-%'
              AND s.id NOT LIKE 'closer-%'
              AND s.id NOT LIKE 'slash-%'
              AND EXISTS (
                  SELECT 1 FROM interactions i
                  WHERE i.session_id = s.id
                    AND i.role = 'user'
                    AND TRIM(COALESCE(i.content, '')) != ''
                    AND COALESCE(i.source, '') != 'terminal_tunnel'
              )
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


async def _sweep_once() -> int:
    """One recovery-sweep tick: find stuck sessions and re-trigger naming.
    Returns how many sessions were attempted. Bounded (max per tick) and
    cooldown-aware (a session failed recently is skipped until its cooldown
    lapses), so a persistently-down model cannot burn unlimited LLM calls.
    The naming call resolves its own model+client (env-independent), so a
    fresh-boot process with empty env is handled correctly."""
    from app.db import get_db
    db = get_db()
    try:
        candidates = await _find_unnamed_sessions(db, limit=_SWEEP_MAX_PER_TICK)
    except Exception as e:  # noqa: BLE001
        logger.warning("session_namer: sweep candidate query failed: %s",
                       e, exc_info=True)
        return 0

    now = datetime.now(timezone.utc)
    attempted = 0
    for row in candidates:
        sid = row.get("id")
        owner = row.get("user_id")
        if not sid or not owner:
            continue
        try:
            meta = json.loads(row.get("metadata")) if row.get("metadata") else {}
            if not isinstance(meta, dict):
                meta = {}
        except (json.JSONDecodeError, TypeError):
            meta = {}
        if meta.get("auto_title_locked"):
            continue  # user or a prior lock already settled the name
        # Cooldown: the failure stamp sets auto_title_last_attempt_at — don't
        # hammer a session we tried recently (or whose hook is still in flight).
        last_attempt = _parse_ts(meta.get("auto_title_last_attempt_at"))
        if last_attempt and (now - last_attempt).total_seconds() < _SWEEP_RETRY_COOLDOWN_S:
            continue
        # Age gate: skip very fresh sessions — their own turn-hook may still be
        # running; don't double-name by racing it.
        upd = _parse_ts(row.get("updated_at"))
        if upd and (now - upd).total_seconds() < _SWEEP_MIN_AGE_S:
            continue

        async def _emit(ev: dict, _owner: str = owner) -> None:
            # Push the live rename to every tab the owner has open, so a swept
            # session's header updates in place. Never raises.
            try:
                from app.api.chat import notify_user
                await notify_user(_owner, ev)
            except Exception:  # noqa: BLE001
                pass

        try:
            await _maybe_title_session(db, owner, sid, emit=_emit, force=False)
            attempted += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("session_namer: sweep naming %s failed: %s",
                           str(sid)[:12], e, exc_info=True)
    return attempted


async def _sweep_loop() -> None:
    await asyncio.sleep(_SWEEP_STARTUP_DELAY_S)
    while True:
        try:
            _n = await _sweep_once()
            if _n:
                logger.info(
                    "session_namer: recovery sweep re-triggered naming for %d "
                    "stuck session(s)", _n)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("session_namer: recovery sweep tick failed: %s",
                           e, exc_info=True)
        try:
            await asyncio.sleep(_SWEEP_INTERVAL_S)
        except asyncio.CancelledError:
            raise


_sweep_task: Optional[asyncio.Task] = None


async def start_sweep() -> None:
    """Start the recovery sweep (idempotent). Registered on the background
    leader from app/main.py, gated on the session_titler app function."""
    global _sweep_task
    if _sweep_task is not None and not _sweep_task.done():
        return
    _sweep_task = asyncio.create_task(_sweep_loop(), name="session_namer_sweep")
    logger.info("Session Namer recovery sweep started (every %ss, max %d/tick)",
                _SWEEP_INTERVAL_S, _SWEEP_MAX_PER_TICK)
    # Visible sweep liveness: plugin INFO lines are suppressed by the
    # diagnostics handler, so record the start through the recorder directly
    # (persists at INFO) — otherwise a silently-dead sweep is indistinguishable
    # from a healthy one in the diagnostics page.
    try:
        from app.agent.diagnostics import record as _diag
        _diag("info", "recovery",
              "Session Namer recovery sweep started "
              f"(every {_SWEEP_INTERVAL_S}s, max {_SWEEP_MAX_PER_TICK}/tick)",
              source="session_namer_sweep")
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
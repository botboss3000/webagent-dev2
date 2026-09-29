"""
Unified supervised agent-run entry point + self-healing resume engine.

Why this exists
---------------
Only the interactive web-chat path used to record durable run-state
(``session_runs``) and run under the supervised ``RunManager``. Every background
path — scheduled automations, event triggers, inbound messages, webhooks — called
the agent loop directly, with no run-state row and no strong task reference. So a
server restart or a frozen await silently lost that work, and nobody re-ignited it.

This module is the single funnel that makes EVERY origin:
  1. **recorded** — a ``session_runs`` row with ``origin`` + a ``relaunch_ctx``
     recipe to rebuild the turn headlessly, and a ``stop_cause`` describing why it
     ended;
  2. **supervised** — owned by ``RunManager`` so a disconnecting client (or a
     dropped task reference) cannot kill it;
  3. **recoverable** — boot recovery and the liveness watchdog call
     :func:`resume_one` to re-ignite an *involuntary* stop (server_restart /
     zombie / frozen / crash) from durable conversation history, while NEVER
     resuming a voluntary one (user_stop / replaced) or a throwaway/opted-out run.

Resume happens at the **turn boundary**: the full conversation is replayed exactly
as it ended — the partial interrupted answer kept, any dangling mid-tool-call
stripped by ``session_history`` — with NO continue-nudge, so the agent continues
unaware of the interruption. Mid-tool Python state is not snapshotted, so a
side-effecting tool whose result was never persisted MAY run again — the per-tool /
per-agent opt-out is the mitigation for that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


async def _reserve_recovery_notice_seq(session_id: str, db: Any) -> int:
    """Allocate a recovery-notice sequence without colliding with a RunBuffer."""
    from app.agent.run_buffer import get_registry
    return await get_registry().reserve_external_seq(session_id, db)


# ── Tunables (Phase 3 wires these to app-settings.json; defaults until then) ──
DEFAULT_MAX_RESUME_ATTEMPTS = 6
# Per-error-type budget default: how many times ONE involuntary stop cause may be
# retried before the run is retired, even while total attempts remain. 0 = off.
DEFAULT_MAX_RESUME_ATTEMPTS_PER_CAUSE = 3
RESUME_BACKOFF_BASE_SECONDS = 30.0
RESUME_BACKOFF_CAP_SECONDS = 1800.0
LEASE_SECONDS = 120.0
BOOT_RESUME_CONCURRENCY = 4

# Origins / sessions that are never auto-resumed (configurable per-agent via auto_resume).
_EXCLUDED_ORIGINS: tuple = ()
_THROWAWAY_PREFIXES: tuple = ()
# Auto-resume is reserved for involuntary infrastructure failures. Provider
# contract outcomes remain visible and wait for a fresh user turn; replaying the
# old turn made the footer Continue action consume its retry budget without new
# direction or meaningful tool work.
_RESUMABLE_CAUSES = (
    "server_restart", "zombie", "frozen", "crash", "empty_response",
)
_VOLUNTARY_STOP_CAUSES = ("user_stop", "user_pause", "replaced")

# Resumes replay the RAW transcript with NO nudge. The resumed agent must see the
# conversation exactly as it was when the run stopped — ending at the last real
# message (or, if the server died mid-tool-call, with the dangling call stripped
# by the history builder) — and continue with no awareness that it was
# interrupted or resumed. A "[SYSTEM] resume" instruction is deliberately NOT
# injected anywhere: callers pass user_message=None.


@dataclass
class RunOutcome:
    """Terminal result of a supervised turn. ``stop_cause`` is the machine
    taxonomy used for resume decisions; ``reply`` is the final assistant text
    (needed by Mode-A callers that deliver it)."""
    status: str = "complete"            # complete | interrupted | error
    stop_cause: Optional[str] = None    # complete | crash | ... (None → leave existing)
    reply: Optional[str] = None
    error: Optional[str] = None


# ── Per-origin resume builders ────────────────────────────────────────────────
# Each entry knows how to REBUILD and run a turn for its origin from a stored
# relaunch_ctx. Registered at import time by the owning module (web by chat.py;
# the background origins default to the generic builder below).
ResumeBuilder = Callable[[Dict[str, Any], bool], Awaitable[RunOutcome]]
_RESUME_BUILDERS: Dict[str, ResumeBuilder] = {}


def register_resume_builder(origin: str, fn: ResumeBuilder) -> None:
    _RESUME_BUILDERS[origin] = fn


def get_resume_builder(origin: str) -> Optional[ResumeBuilder]:
    return _RESUME_BUILDERS.get(origin)


def _norm_outcome(x: Any) -> RunOutcome:
    if isinstance(x, RunOutcome):
        return x
    if x is None:
        return RunOutcome()
    if isinstance(x, str):
        return RunOutcome(reply=x)
    return RunOutcome()


def _cfg() -> Dict[str, Any]:
    """Self-healing tunables from app-settings.json (falls back to defaults)."""
    try:
        from app.admin.settings import get_self_heal_config
        return get_self_heal_config()
    except Exception:
        return {
            "max_resume_attempts": DEFAULT_MAX_RESUME_ATTEMPTS,
            "max_resume_attempts_per_cause": DEFAULT_MAX_RESUME_ATTEMPTS_PER_CAUSE,
            "backoff_seconds": RESUME_BACKOFF_BASE_SECONDS,
        }


def _backoff_seconds(attempts: int, base: Optional[float] = None) -> float:
    """Seconds to wait before the next resume: ``base * 2**attempts``, capped.

    ``base`` is the per-agent backoff override when the run's agent set one;
    otherwise the app-global ``run_resume_backoff_seconds`` (default 30s)."""
    if base is None:
        base = float(_cfg().get("backoff_seconds", RESUME_BACKOFF_BASE_SECONDS))
    return min(float(base) * (2 ** max(0, attempts)), RESUME_BACKOFF_CAP_SECONDS)


def _effective_max(row: Dict[str, Any], agent_max: Optional[int] = None) -> int:
    """The resume budget for one run.

    Precedence: the run's own stamped cap → the per-agent override → the global
    default. The global ``max_resume_attempts`` is BOTH the fallback and the
    CEILING, so neither a per-agent value nor a stale/oversized stamped value
    can ever exceed it (defense in depth)."""
    ceiling = int(_cfg().get("max_resume_attempts", DEFAULT_MAX_RESUME_ATTEMPTS))
    if ceiling < 0:
        ceiling = 0
    cap = row.get("max_resume_attempts") or agent_max
    if not cap:
        return ceiling
    try:
        cap_i = int(cap)
    except (TypeError, ValueError):
        return ceiling
    if cap_i <= 0:
        return ceiling
    return min(cap_i, ceiling)


def _effective_max_per_cause(row: Dict[str, Any], agent_max: Optional[int] = None) -> int:
    """The per-ERROR-TYPE resume cap for one run.

    Precedence mirrors ``_effective_max``: the per-agent override clamped to the
    app-global ceiling (``run_max_resume_attempts_per_cause``). Unlike the total
    budget there is no per-run stamped value. ``0`` disables the per-type cap, so
    only the total budget applies."""
    ceiling = int(_cfg().get("max_resume_attempts_per_cause",
                             DEFAULT_MAX_RESUME_ATTEMPTS_PER_CAUSE))
    if ceiling < 0:
        ceiling = 0
    if not agent_max:
        return ceiling
    try:
        cap_i = int(agent_max)
    except (TypeError, ValueError):
        return ceiling
    if cap_i <= 0:
        return ceiling
    return min(cap_i, ceiling)


def recovery_pending(row: Dict[str, Any]) -> bool:
    """True while a run that stopped involuntarily has NOT been marked finished.

    An involuntary stop (crash / server_restart / frozen / zombie) marks the
    ``session_runs`` row terminal *before* the watchdog or boot sweep re-ignites
    it, so without this the UI renders that gap as "finished": the session goes
    idle for a few seconds and then starts working again. Read-model callers use
    this to keep presenting the session as in-progress — a ``resuming`` status
    in the session list, ``active``/``recovering`` on the transcript run payload
    — for the WHOLE ladder, not just the lap that is about to run.

    The one durable "the revival gave up" signal is ``stop_cause``, not the
    attempt count: ``run_state_mark_failed`` writes ``stop_cause='failed'`` (the
    opt-out path writes ``needs_manual_resume``; a real turn ends ``complete``).
    No resumable cause survives those, so this returns False from the moment the
    run is genuinely retired. Deliberately NOT also gating on
    ``resume_attempts < max``: after the final attempt crashes the row stays
    parked with the resumable cause until the next watchdog tick retires it, and
    clearing the indicator inside that window is exactly the
    idle → working flicker this predicate exists to remove. A run cancelled by
    the global stop is tagged ``user_stop``, so it still reads as stopped.
    """
    if not row:
        return False
    if str(row.get("status") or "") in ("running", "complete"):
        return False
    return row.get("stop_cause") in _RESUMABLE_CAUSES


def _nonresumable_tools() -> set:
    """Tools whose use holds a run for manual resume instead of auto-resuming.
    Empty by default (user choice: auto-resume with opt-out) — populated by an
    explicit ``no_auto_resume`` metadata flag and/or an app-settings list."""
    names: set = set()
    try:
        from app.tools.loader import BUILTIN_TOOL_METADATA
        names |= {n for n, m in BUILTIN_TOOL_METADATA.items() if m.get("no_auto_resume")}
    except Exception:
        pass
    return names


def _agent_opted_out(agent: Optional[Dict[str, Any]]) -> bool:
    """An agent explicitly flagged auto_resume=False is never auto-resumed."""
    if not agent:
        return False
    val = agent.get("auto_resume")
    return val is False or val == 0


def _parse_ctx(row: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(row.get("relaunch_ctx") or "{}") or {}
    except Exception:
        return {}


def _parse_cause_counts(row: Dict[str, Any]) -> Dict[str, int]:
    """The run's per-error-type resume tally: ``{cause: count}``.

    Tolerant of the column being absent (older DB before the migration) or of
    malformed JSON → empty dict, so resume never fails on a bad tally."""
    try:
        raw = json.loads(row.get("resume_cause_counts") or "{}") or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: Dict[str, int] = {}
    for key, val in raw.items():
        try:
            out[str(key)] = int(val)
        except (TypeError, ValueError):
            continue
    return out


async def _current_agent_id(db, row: Dict[str, Any]) -> Optional[str]:
    """The session's CURRENT agent — which a mid-session delegation may have
    rebound (loop.py bind_session_to_agent). Resume must use this, not the
    agent captured when the run first began."""
    sid = row.get("session_id")
    try:
        aid = await db.get_session_agent_id(sid)
        if aid:
            return aid
    except Exception:
        pass
    return row.get("agent_id")


def _agent_meta(agent: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """An agent record's metadata as a dict (SQLite stores it as a JSON string)."""
    meta = (agent or {}).get("metadata")
    if isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = {}
    return meta if isinstance(meta, dict) else {}


def _positive_num(val: Any) -> Optional[float]:
    """A positive number, else None — so 0 / blank / absent means 'inherit global'."""
    if val is None or isinstance(val, bool):
        return None
    try:
        num = float(val)
    except (TypeError, ValueError):
        return None
    return num if num > 0 else None


async def _agent_resume_overrides(
    db, agent_id: Optional[str],
) -> Tuple[Optional[int], Optional[float], Optional[int]]:
    """The agent's per-agent resume knobs: (total attempts, backoff base seconds,
    per-error-type attempts).

    ``(None, None, None)`` when the agent sets none, so the run inherits the
    global default/ceiling. Read from agent metadata (``resume_max_attempts`` /
    ``resume_backoff_seconds`` / ``resume_max_attempts_per_cause``), mirroring how
    ``resume_tail_messages`` is stored. Resolved HERE from the run's agent id
    rather than threaded through the many ``run_supervised_turn`` call sites."""
    if not agent_id:
        return None, None, None
    try:
        agent = await db.get_agent_by_id(agent_id)
    except Exception:
        return None, None, None
    meta = _agent_meta(agent)
    attempts = _positive_num(meta.get("resume_max_attempts"))
    backoff = _positive_num(meta.get("resume_backoff_seconds"))
    per_cause = _positive_num(meta.get("resume_max_attempts_per_cause"))
    return (
        int(attempts) if attempts is not None else None,
        backoff,
        int(per_cause) if per_cause is not None else None,
    )


# ── Supervised launch ─────────────────────────────────────────────────────────

async def run_supervised_turn(
    *,
    session_id: str,
    user_id: str,
    agent_id: Optional[str],
    origin: str,
    build_turn: Callable[[bool], Awaitable[Any]],
    channel: Optional[str] = None,
    turn_id: Optional[str] = None,
    relaunch_ctx: Optional[Any] = None,
    max_resume_attempts: Optional[int] = None,
    owner_token: Optional[str] = None,
    fresh: bool = True,
    replace: bool = True,
    await_result: bool = False,
    result_timeout: Optional[float] = None,
) -> Optional[RunOutcome]:
    """Run one agent turn, recorded in ``session_runs`` and supervised by
    ``RunManager`` so no disconnecting client can kill it.

    ``build_turn(replaced)`` is the per-origin coroutine that actually executes
    the turn (build prompt → run loop → persist). It must NOT touch run-state;
    this function owns begin/finish.

    - ``fresh``  True for a brand-new turn (writes ``run_state_begin`` and resets
      the retry budget). False for a resume (the claim already flipped the row to
      running and bumped the attempt — begin would wrongly zero the budget).
    - ``replace`` True uses ``start_or_replace`` (a new message supersedes a live
      run — the web/inbound semantic). False uses ``start_run`` (a resume must
      yield to any live run, never clobber it).
    - ``await_result`` (Mode A) waits for and returns the ``RunOutcome`` so the
      caller can deliver the reply, while the work itself stays supervised and
      survives the caller leaving.
    """
    from app.db import get_db
    from app.agent.run_manager import get_run_manager
    from app.agent import session_gate

    db = get_db()
    rm = get_run_manager()
    rc_json = json.dumps(relaunch_ctx) if isinstance(relaunch_ctx, (dict, list)) else relaunch_ctx

    loop = asyncio.get_running_loop()
    fut: "asyncio.Future[RunOutcome]" = loop.create_future()

    def _resolve(outcome: RunOutcome) -> None:
        if not fut.done():
            fut.set_result(outcome)

    async def _do(replaced: bool) -> RunOutcome:
        # App-wide session cap (app/agent/session_gate.py): wait for a free slot
        # before starting when max_active_sessions is reached. Fail-open: a gate
        # error must never block a run (release() then no-ops).
        try:
            await session_gate.acquire(session_id, user_id=user_id)
        except asyncio.CancelledError:
            raise
        except Exception as _ge:  # noqa: BLE001
            logger.debug("session gate acquire failed for %s: %s", session_id[:12], _ge)
        try:
            if fresh:
                _stamped_max = max_resume_attempts
                if _stamped_max is None:
                    # Per-agent budget: stamp the agent's own limit onto the run so
                    # every later resume (boot, watchdog, zombie) honors it. None
                    # leaves the column NULL → the run inherits the global default.
                    try:
                        _agent_max, _ = await _agent_resume_overrides(db, agent_id)
                        _stamped_max = _agent_max
                    except Exception as _oe:  # noqa: BLE001
                        logger.debug("agent resume override lookup failed for %s: %s",
                                     session_id[:12], _oe)
                        _stamped_max = None
                try:
                    await db.run_state_begin(
                        session_id, user_id, agent_id, turn_id,
                        origin=origin, relaunch_ctx=rc_json,
                        max_resume_attempts=_stamped_max,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.debug("run_state_begin failed for %s: %s", session_id[:12], e)
            finish_resume_attempts: Optional[int] = None
            try:
                _version_row = await db.run_state_get(session_id)
                if _version_row is not None:
                    finish_resume_attempts = int(
                        _version_row.get("resume_attempts") or 0
                    )
            except Exception:
                pass

            outcome = RunOutcome()
            try:
                outcome = _norm_outcome(await build_turn(replaced))
            except asyncio.CancelledError:
                # A cancellation raised by the supervised task without a
                # durable voluntary cause is an infrastructure failure, not a
                # user choice. Give it a resumable cause so the watchdog
                # sweep can continue the same turn from its persisted history.
                # _finish_run preserves a previously recorded user_stop or
                # replaced cause, so Stop and a newer user message remain
                # terminal as intended.
                outcome = RunOutcome(status="interrupted", stop_cause="crash")
                await _finish_run(
                    db, session_id, outcome, turn_id,
                    expected_owner_token=owner_token,
                    expected_resume_attempts=finish_resume_attempts,
                )
                _resolve(outcome)
                raise
            except Exception as e:  # noqa: BLE001 — supervisor records, never propagates a crash silently
                logger.error("supervised turn %s (origin=%s) failed: %s",
                             session_id[:12], origin, e, exc_info=True)
                # A deterministic client error is not resumable: the resume
                # replays the same request. One HTTP 400 cost three identical
                # attempts before the session was marked failed, so ask the
                # classifier whether this cause deserves a retry at all.
                from app.agent.provider_errors import provider_stop_cause
                outcome = RunOutcome(
                    status="error",
                    stop_cause=provider_stop_cause(e),
                    error=str(e)[:500],
                )
                await _finish_run(
                    db, session_id, outcome, turn_id,
                    expected_owner_token=owner_token,
                    expected_resume_attempts=finish_resume_attempts,
                )
                _resolve(outcome)
                return outcome
            await _finish_run(
                db, session_id, outcome, turn_id,
                expected_owner_token=owner_token,
                expected_resume_attempts=finish_resume_attempts,
            )
            _resolve(outcome)
            return outcome
        finally:
            try:
                await session_gate.release(session_id)
            except Exception as _gr:  # noqa: BLE001
                logger.debug("session gate release failed for %s: %s", session_id[:12], _gr)

    if replace:
        await rm.start_or_replace(
            session_id=session_id, user_id=user_id, turn_id=turn_id, db=db,
            run_factory=lambda replaced: _do(replaced),
        )
    else:
        started = await rm.start_run(
            session_id=session_id, user_id=user_id, turn_id=turn_id,
            run_factory=lambda: _do(False),
            db=db,
        )
        if not started:
            # A live run already owns this session (a user message, or another
            # resume that won the claim). Do not clobber it.
            logger.info("supervised turn %s not started — already running", session_id[:12])
            _resolve(RunOutcome(status="running"))

    if await_result:
        try:
            if result_timeout:
                return await asyncio.wait_for(asyncio.shield(fut), timeout=result_timeout)
            return await fut
        except asyncio.TimeoutError:
            logger.info("await_result timed out for %s; run continues supervised", session_id[:12])
            return RunOutcome(status="running")
        except Exception:  # noqa: BLE001
            return RunOutcome(status="error", stop_cause="crash")
    return None


async def _finish_run(
    db, session_id: str, outcome: RunOutcome, turn_id: Optional[str] = None,
    expected_owner_token: Optional[str] = None,
    expected_resume_attempts: Optional[int] = None,
) -> None:
    """Flip run-state to its terminal status + derived cause.

    A concurrent voluntary cause always wins.  A watchdog-originated
    involuntary cause (for example ``frozen``) also remains authoritative when
    cancellation produces the generic ``interrupted`` outcome with no cause:
    otherwise lifecycle diagnostics lose the recovery classification and a
    later terminal sweep cannot safely explain or continue the retry ladder.
    """
    cause = outcome.stop_cause
    if cause is None and outcome.status == "complete":
        cause = "complete"
    try:
        _before_finish = await db.run_state_get(session_id)
        _recorded_cause = (_before_finish or {}).get("stop_cause")
        if _recorded_cause in _VOLUNTARY_STOP_CAUSES:
            cause = _recorded_cause
        elif cause is None and _recorded_cause in _RESUMABLE_CAUSES:
            # Watchdog cancellation tags the durable row first, then task
            # cancellation unwinds as an unclassified "interrupted" outcome.
            # Carry the known involuntary cause into both finish persistence and
            # lifecycle evidence so frozen/zombie/crash rows remain resumable.
            cause = _recorded_cause
    except Exception:
        pass
    try:
        _finished = await db.run_state_finish(
            session_id, status=outcome.status,
            error=outcome.error, stop_cause=cause,
            expected_owner_token=expected_owner_token,
            expected_resume_attempts=expected_resume_attempts,
        )
        if _finished is False:
            logger.info("Ignored stale finisher for recovered run %s", session_id[:12])
            return
    except Exception as e:  # noqa: BLE001
        logger.debug("run_state_finish failed for %s: %s", session_id[:12], e)

    # Flight-recorder: keep every run outcome (durable) for post-hoc diagnosis,
    # and (opt-in) hand involuntary failures to the diagnostic agent.
    _row: Optional[Dict[str, Any]] = None
    try:
        _row = await db.run_state_get(session_id)
    except Exception:
        _row = None
    try:
        from app.agent.diagnostics import record_run_lifecycle
        _attempts = (_row or {}).get("resume_attempts") or 0
        record_run_lifecycle(
            "finish", session_id,
            status=outcome.status, stop_cause=cause, error=outcome.error,
            agent_id=(_row or {}).get("agent_id"),
            user_id=(_row or {}).get("user_id"),
            origin=(_row or {}).get("origin"),
            # Ties re-trigger success together: a 'finish' with status=complete and
            # resume_attempts>0 is a recovered run; the attempt count is the depth.
            detail={"resume_attempts": _attempts, "was_resume": _attempts > 0},
        )
    except Exception:
        pass
    # Auto-investigate involuntary failures (default OFF — see maybe_auto_investigate).
    try:
        from app.agent.diag_investigate import maybe_auto_investigate
        await maybe_auto_investigate(db, session_id, outcome, _row)
    except Exception:
        pass

    # ── Model Switcher auto-cleanup (background, invisible) ──
    # Whenever a run reaches a terminal state, any AGENT-driven model/effort
    # override (use_premium_model / set_model / set_effort) is dropped in the
    # background so the NEXT user turn starts on the agent's DEFAULT model at
    # default effort. This is what lets the skill stop mandating
    # reset_to_default() as a deliberate closing step — the cleanup happens on
    # its own, after the final answer is already out. The user's own
    # footer-picker slot selection survives (clear_session_agent_model_override
    # strips only the agent-driven parts, by design). Guarded: if a NEWER run
    # already owns the session (a replacement started before this teardown ran),
    # skip — that run's own prepare-path resets anyway, and racing it could wipe
    # a fresh mid-run switch.
    try:
        async def _reset_model_override() -> None:
            try:
                _cur = await db.run_state_get(session_id)
                if turn_id and _cur and _cur.get("turn_id") and _cur.get("turn_id") != turn_id:
                    return
                _clear = getattr(db, "clear_session_agent_model_override", None)
                if _clear:
                    await _clear(session_id)
                # Live push: the footer model changer drops its warning color
                # now that the run is over (see model_switcher.py
                # _emit_override_state). Best-effort — never break teardown.
                try:
                    from app.chat.execution import _emit_to_visualizers
                    await _emit_to_visualizers(session_id, {
                        "type": "model_override", "active": False, "model": "",
                    }, user_id=(_row or {}).get("user_id") or "")
                except Exception:  # noqa: BLE001
                    pass
            except Exception:  # noqa: BLE001 — cleanup must never break teardown
                pass
        asyncio.create_task(_reset_model_override())
    except Exception:  # noqa: BLE001
        pass


# ── Resume orchestration ──────────────────────────────────────────────────────

async def _turn_already_promoted(db, row: Dict[str, Any]) -> bool:
    """True when this run's turn already delivered its promoted closing terminal.

    Recovery exists to FINISH a turn that never produced its answer. When the
    previous attempt DID promote — the process died between persisting the final
    row and recording a terminal run status — re-running the turn would promote a
    SECOND answer for a single turn. The closing gate promotes exactly one
    terminal per run, so a turn that already has its promoted closer is finished,
    not resumable.

    ``chat_render_kind='promoted_answer'`` is the sole durable marker of
    "this run reached its promoted answer". The run-state row points
    ``assistant_interaction_id`` at the in-progress answer, which the terminal
    block finalizes in place.

    Read-only and best-effort: a backend without the indexed lookup, or a
    malformed row, simply reports "not promoted" and today's resume behavior is
    unchanged."""
    iid = str(row.get("assistant_interaction_id") or "").strip()
    sid = str(row.get("session_id") or "").strip()
    uid = str(row.get("user_id") or "").strip()
    fetch = getattr(db, "fetch_session_interaction", None)
    if not (iid and sid and uid) or fetch is None:
        return False
    try:
        record = await fetch(uid, sid, iid)
    except Exception:
        return False
    if not record:
        return False
    kind = (record.get("chat_render_kind") if isinstance(record, dict)
            else getattr(record, "chat_render_kind", None))
    return kind == "promoted_answer"


async def _classify_resume(db, row: Dict[str, Any]) -> Tuple[str, str]:
    """Decide what to do with a stopped run: 'ok' (resume), 'manual' (hold for a
    human one-click), 'complete' (already answered — close it out), 'failed'
    (terminal), or 'skip' (not ours / not resumable)."""
    sid = row.get("session_id") or ""
    origin = row.get("origin") or "web"
    if origin in _EXCLUDED_ORIGINS:
        return ("skip", "excluded origin")
    if sid.startswith(_THROWAWAY_PREFIXES):
        return ("skip", "throwaway session")
    cause = row.get("stop_cause")
    if cause in _VOLUNTARY_STOP_CAUSES:
        return ("skip", f"voluntary stop cause '{cause}'")
    if cause not in _RESUMABLE_CAUSES:
        return ("skip", f"cause '{cause}' not resumable")

    # Per-agent opt-out (uses the session's CURRENT agent after any delegation).
    agent_id = await _current_agent_id(db, row)
    if agent_id:
        try:
            agent = await db.get_agent_by_id(agent_id)
        except Exception:
            agent = None
        if _agent_opted_out(agent):
            return ("manual", "agent opted out of auto-resume")

    # Per-tool opt-out — a turn that touched a non-resumable tool waits for a human.
    blockset = _nonresumable_tools()
    if blockset:
        try:
            used = set(await db.run_state_session_tool_names(sid))
        except Exception:
            used = set()
        hit = used & blockset
        if hit:
            return ("manual", f"used non-resumable tool(s): {', '.join(sorted(hit))}")

    # One promoted terminal per run: a turn that already delivered its promoted
    # closing answer is finished, not resumable — even when the process died
    # before the run recorded its terminal status. Re-running it would promote a
    # second answer into the same activity group.
    if await _turn_already_promoted(db, row):
        return ("complete", "turn already delivered its promoted terminal")

    return ("ok", "")


# ── Cheap provider probe (resume gate) ────────────────────────────────────────

_PROBE_PROMPT = "ping"
_PROBE_TIMEOUT_SECONDS = 12.0


async def _probe_provider_for_run(db: Any, row: Dict[str, Any]) -> bool:
    """Cheap 1-token reachability probe against the SAME provider config the
    resume turn would use (app-default → agent → session resolution, exactly
    like the loop's ``apply_provider_for_run`` call).

    Returns True when the provider answers within the bound; False on any
    resolution error, connection failure, DNS failure, or timeout. Never raises.
    Each failed probe is charged to the bounded recovery budget. That lets a
    transient outage retry, but guarantees a persistent outage reaches a
    terminal state instead of leaving the browser permanently active.
    """
    try:
        from app.admin.settings import apply_provider_for_run
        from app.agent.loop import _get_client

        user_id = row.get("user_id")
        if not user_id:
            return False
        agent_id = await _current_agent_id(db, row) or row.get("agent_id")
        agent = None
        if agent_id:
            try:
                agent = await db.get_agent_by_id(agent_id)
            except Exception:
                agent = None
        cfg = await apply_provider_for_run(
            user_id, agent, row.get("session_id"), apply_env=False)
        model = str(cfg.get("model") or "")
        base_url = str(cfg.get("base_url") or "")
        if not model or not base_url:
            return False
        # ChatGPT subscriptions authenticate from their encrypted OAuth token
        # store at stream-open time. They intentionally have no API key and use a
        # synthetic ``codex://`` base URL, so the standard OpenAI-client probe
        # cannot represent their health. Verify the same scoped authorization the
        # runtime will use, then let the normal stream-open timeout handle remote
        # reachability without incorrectly deferring every recovery attempt.
        if str(cfg.get("provider") or "") == "openai_subscription":
            from app import openai_subscription
            status = await openai_subscription.status(
                scope=str(cfg.get("credential_scope") or "platform"),
                entry_id=str(cfg.get("entry_id") or ""),
                user_id=str(cfg.get("_credential_user_id") or user_id),
                agent_id=str(cfg.get("_credential_agent_id") or agent_id or ""),
            )
            return bool(status.get("chatgpt_authorized"))
        # Same runtime-model normalization the loop applies (_runtime_model).
        if base_url and "openrouter.ai" not in base_url and "/" in model:
            model = model.split("/", 1)[-1]
        client = _get_client(base_url, cfg.get("api_key"))
        await asyncio.wait_for(
            client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": _PROBE_PROMPT}],
                max_tokens=1,
                stream=False,
            ),
            timeout=_PROBE_TIMEOUT_SECONDS,
        )
        return True
    except Exception:
        return False


async def resume_one(session_id: str, *, reason: Optional[str] = None) -> bool:
    """Attempt to re-ignite a single stopped run. Returns True if a resume turn
    was launched. Enforces eligibility, the retry budget + backoff, the opt-out,
    and an atomic lease so a live run is never double-started."""
    from app.db import get_db
    from app.agent.run_manager import get_run_manager

    # Kill switch: while engaged, auto-resume is suppressed. This is the single
    # funnel for every involuntary revival (watchdog, boot orphan-resume, runtime
    # sweep, instant network-crash resume) — gating here silences all of them.
    # A user's explicit "continue a chat" uses manual_resume(), which is NOT
    # gated, so foreground use still works.
    try:
        from app.kill_switch import is_engaged as _ks_engaged
        if _ks_engaged():
            return False
    except Exception:
        pass

    db = get_db()
    rm = get_run_manager()

    row = await db.run_state_get(session_id)
    if not row:
        return False
    if rm.is_running(session_id):
        return False  # already alive in this process — nothing to heal

    decision, why = await _classify_resume(db, row)
    if decision == "skip":
        return False
    if decision == "complete":
        # The turn already has its promoted terminal. Close the orphaned run
        # rather than re-running it — a resume would promote a second answer.
        await db.run_state_finish(session_id, status="complete")
        logger.info("Run %s already delivered its promoted terminal — closed, not resumed",
                    session_id[:12])
        return False
    if decision == "manual":
        await db.run_state_mark_manual(session_id, why)
        logger.info("Holding run %s for manual resume: %s", session_id[:12], why)
        return False
    if decision == "failed":
        await db.run_state_mark_failed(session_id, why)
        return False

    # Per-agent knobs for THIS run's current agent (a mid-session delegation may
    # have rebound it). The attempt ceiling stays the app-global limit.
    _run_agent_id = await _current_agent_id(db, row)
    _agent_max, _agent_backoff, _agent_per_cause = await _agent_resume_overrides(
        db, _run_agent_id)
    effective_max = _effective_max(row, _agent_max)
    attempts = int(row.get("resume_attempts") or 0)
    if attempts >= effective_max:
        # Keep the last concrete failure.  Replacing it with only "exhausted N
        # attempts" is what made the terminal chat notice useless.
        _root_error = (row.get("error") or "").strip()
        await db.run_state_mark_failed(session_id, None)
        logger.warning("Run %s exhausted %d resume attempts — marked failed",
                       session_id[:12], effective_max)
        try:
            from app.agent.diagnostics import record as _diag
            _diag("warning", "run",
                  f"Session {session_id[:12]} exhausted {effective_max} resume attempts — marked failed",
                  source="resume_exhausted",
                  detail={"session_id": session_id, "attempts": attempts, "max": effective_max},
                  session_id=session_id)
        except Exception:
            pass
        # Persist a concise user-facing system message so the user can resume in chat.
        _stop_cause = row.get("stop_cause") or "crash"
        _sys_msg = (
            f"Run failed after {effective_max} recovery attempt"
            f"{'s' if effective_max != 1 else ''}\n"
            f"Cause: {_stop_cause}\n"
            "Send a message to resume this session"
        )
        try:
            await db.insert_interaction(
                user_id=row.get("user_id"),
                session_id=session_id,
                role="system",
                content=_sys_msg,
                source="system:error",
                # Allocate a session_seq so the message actually reaches the chat
                # UI: transcript fetches order by session_seq and the live
                # reconcile poll filters "session_seq IS NOT NULL" — a seq-less
                # system row stays invisible.
                session_seq=await _reserve_recovery_notice_seq(session_id, db),
            )
        except Exception:
            pass
        return False

    # Per-ERROR-TYPE budget: one repeating cause must not consume the whole total
    # budget. Charge this resume against the stop cause it is reviving, and retire
    # the run once a single cause reaches its own cap — even while total attempts
    # remain. Both tallies reset when a fresh run begins (run_state_begin).
    _cause = str(row.get("stop_cause") or "crash")
    per_cause_max = _effective_max_per_cause(row, _agent_per_cause)
    _cause_counts = _parse_cause_counts(row)
    _next_cause_count = int(_cause_counts.get(_cause) or 0) + 1
    if per_cause_max > 0 and _next_cause_count > per_cause_max:
        await db.run_state_mark_failed(session_id, None)
        logger.warning("Run %s exhausted %d resume attempts for cause '%s' — marked failed",
                       session_id[:12], per_cause_max, _cause)
        try:
            from app.agent.diagnostics import record as _diag
            _diag("warning", "run",
                  f"Session {session_id[:12]} stopped after {per_cause_max} "
                  f"'{_cause}' resume attempts — marked failed",
                  source="resume_cause_exhausted",
                  detail={"session_id": session_id, "cause": _cause,
                          "cause_attempts": _next_cause_count - 1,
                          "per_cause_max": per_cause_max},
                  session_id=session_id)
        except Exception:
            pass
        try:
            await db.insert_interaction(
                user_id=row.get("user_id"),
                session_id=session_id,
                role="system",
                content=(
                    f"Run failed after {per_cause_max} recovery attempt"
                    f"{'s' if per_cause_max != 1 else ''} for the same cause\n"
                    f"Cause: {_cause}\n"
                    "Send a message to resume this session"
                ),
                source="system:error",
                session_seq=await _reserve_recovery_notice_seq(session_id, db),
            )
        except Exception:
            pass
        return False
    _cause_counts[_cause] = _next_cause_count
    _cause_counts_json = json.dumps(_cause_counts)

    origin = row.get("origin") or "web"
    builder = get_resume_builder(origin)
    if builder is None:
        await db.run_state_mark_manual(session_id, f"no resume builder for origin '{origin}'")
        logger.warning("No resume builder for origin '%s' (session %s)", origin, session_id[:12])
        return False

    # Probe the provider before a full resume. A failed probe still consumes one
    # bounded recovery attempt: otherwise an unavailable provider re-arms this
    # run forever and keeps the browser's active-state polling alive.
    if not await _probe_provider_for_run(db, row):
        _probe_backoff = _backoff_seconds(attempts, _agent_backoff)
        try:
            await db.run_state_defer_resume(
                session_id, _probe_backoff,
                note="LLM provider probe failed — recovery attempt consumed")
        except Exception as _pde:
            logger.debug("run_state_defer_resume failed for %s: %s", session_id[:12], _pde)
        logger.info("Run %s provider probe failed — deferred %ss (attempt %d/%d)",
                    session_id[:12], _probe_backoff, attempts, effective_max)
        try:
            from app.agent.diagnostics import record as _diag
            _diag("warning", "recovery",
                  f"LLM provider probe failed — resume deferred (session {session_id[:12]})",
                  source="resume_probe_failed",
                  detail={"session_id": session_id, "attempts": attempts + 1,
                          "max": effective_max, "defer_seconds": _probe_backoff},
                  session_id=session_id)
        except Exception:
            pass
        return False

    # Atomic claim: flips to running, bumps the attempt, takes the lease, and sets
    # the NEXT backoff gate. Fails (returns False) if it raced another worker or
    # the run came alive.
    token = uuid.uuid4().hex
    claimed = await db.run_state_claim_for_resume(
        session_id, token, LEASE_SECONDS, _backoff_seconds(attempts, _agent_backoff),
        effective_max, cause_counts_json=_cause_counts_json,
    )
    if not claimed:
        return False

    rc = _parse_ctx(row)
    rc.setdefault("session_id", session_id)
    rc.setdefault("user_id", row.get("user_id"))
    rc.setdefault("agent_id", row.get("agent_id"))
    # This is UI-only operational context. It is emitted to the chat as a
    # recoverable notice and is never added to the agent's model messages.
    rc["recovery"] = {
        "stop_cause": row.get("stop_cause") or "unknown",
        "issue": row.get("error") or "",
        "stopped_at": row.get("updated_at") or row.get("heartbeat_at") or "",
        "recovered_at": datetime.now(timezone.utc).isoformat(),
        "attempt": attempts + 1,
        "max_attempts": effective_max,
        "trigger": reason or "watchdog",
    }
    # A retry is a visible synthetic tool result, not a conversational system
    # message. It stays out of the model history while showing the recovery
    # cause and bounded attempt number in the transcript's normal tool UI.
    recovery_tool = {
        "attempt": attempts + 1,
        "max_attempts": effective_max,
        "cause": rc["recovery"]["stop_cause"],
        "trigger": rc["recovery"]["trigger"],
    }
    rc["recovery_tool"] = recovery_tool
    recovery_result = (
        f"Retrying after {recovery_tool['cause']} "
        f"(attempt {recovery_tool['attempt']}/{recovery_tool['max_attempts']})."
    )
    try:
        await db.insert_interaction(
            user_id=row.get("user_id"), session_id=session_id, role="tool",
            content=recovery_result, parent_id=row.get("turn_id"),
            tool_name="resume_session", channel=rc.get("channel"),
            metadata=json.dumps({"_synth_tool": True, "args": recovery_tool,
                                 "duration_ms": 0, "error": False}),
            output_data=json.dumps({"role": "tool", "content": recovery_result,
                                    "name": "resume_session", "success": True}),
            sender_id=row.get("agent_id"), receiver_id=row.get("agent_id"),
            source="system:recovery", session_seq=await _reserve_recovery_notice_seq(session_id, db),
            turn_id=row.get("turn_id"),
        )
    except Exception:
        logger.debug("Failed to persist recovery tool row for %s", session_id[:12], exc_info=True)

    logger.info("Resuming run %s (origin=%s cause=%s attempt=%d/%d reason=%s)",
                session_id[:12], origin, row.get("stop_cause"),
                attempts + 1, effective_max, reason or "watchdog")
    # Durable record of the re-trigger itself (the matching 'finish' record then
    # tells you whether this attempt succeeded). logger.info above is below the
    # diagnostics WARNING bridge, so without this a resume never reaches the page.
    try:
        from app.agent.diagnostics import record_run_lifecycle
        record_run_lifecycle(
            "resume", session_id, status="running",
            stop_cause=row.get("stop_cause"), origin=origin,
            agent_id=row.get("agent_id"), user_id=row.get("user_id"),
            detail={"attempt": attempts + 1, "max": effective_max,
                    "reason": reason or "watchdog"},
        )
    except Exception:
        pass

    await run_supervised_turn(
        session_id=session_id, user_id=row.get("user_id"), agent_id=row.get("agent_id"),
        origin=origin, channel=rc.get("channel"), turn_id=row.get("turn_id"),
        relaunch_ctx=rc, fresh=False, replace=False,
        build_turn=lambda replaced: builder(rc, True),
        owner_token=token,
    )
    return True


async def manual_resume(session_id: str) -> bool:
    """Resume a run the user explicitly asked to continue (the one-click path for
    a run held as ``needs_manual_resume`` by the opt-out, or any stopped run).
    Bypasses the cause/opt-out gates (the user overrode them) but keeps the
    is-running and lease guards so a live run is never double-started. The retry
    budget is relaxed by one so a manual click always gets an attempt."""
    from app.db import get_db
    from app.agent.run_manager import get_run_manager

    db = get_db()
    rm = get_run_manager()
    row = await db.run_state_get(session_id)
    if not row:
        return False
    # A completed run has nothing to resume.  The chat footer may probe this
    # endpoint before falling back to a new "continue" user turn, so claiming a
    # complete row here would incorrectly replay an already-finished run.
    if row.get("status") == "complete":
        return False
    if rm.is_running(session_id):
        return False
    # One promoted terminal per run — a manual click must not re-run (and
    # re-answer) a turn whose closing message was already promoted.
    if await _turn_already_promoted(db, row):
        await db.run_state_finish(session_id, status="complete")
        logger.info("Manual resume skipped for %s — promoted terminal already delivered",
                    session_id[:12])
        return False
    origin = row.get("origin") or "web"
    if origin in _EXCLUDED_ORIGINS or session_id.startswith(_THROWAWAY_PREFIXES):
        return False
    builder = get_resume_builder(origin)
    if builder is None:
        return False

    # A still-'running' row (e.g. flagged manual without finishing) must be flipped
    # terminal so the atomic claim can take it.
    if row.get("status") == "running":
        await db.run_state_finish(session_id, status="interrupted", stop_cause="crash")

    attempts = int(row.get("resume_attempts") or 0)
    # Honor the per-agent backoff base here too (budget stays attempts+1: a manual
    # click always gets its one shot regardless of the auto-resume ceiling).
    try:
        _, _agent_backoff, _agent_per_cause = await _agent_resume_overrides(
            db, await _current_agent_id(db, row))
    except Exception:
        _agent_backoff, _agent_per_cause = None, None
    # A manual click bypasses BOTH budgets (the user overrode them) but still
    # records the cause against the per-type tally, so the next automatic resume
    # sees an accurate count.
    _cause = str(row.get("stop_cause") or "crash")
    _cause_counts = _parse_cause_counts(row)
    _cause_counts[_cause] = int(_cause_counts.get(_cause) or 0) + 1
    token = uuid.uuid4().hex
    claimed = await db.run_state_claim_for_resume(
        session_id, token, LEASE_SECONDS, _backoff_seconds(attempts, _agent_backoff),
        attempts + 1, cause_counts_json=json.dumps(_cause_counts),
    )
    if not claimed:
        return False

    rc = _parse_ctx(row)
    rc.setdefault("session_id", session_id)
    rc.setdefault("user_id", row.get("user_id"))
    rc.setdefault("agent_id", row.get("agent_id"))
    rc["resume_reason"] = "manual"
    logger.info("Manual resume of run %s (origin=%s attempt=%d)",
                session_id[:12], origin, attempts + 1)
    await run_supervised_turn(
        session_id=session_id, user_id=row.get("user_id"), agent_id=row.get("agent_id"),
        origin=origin, channel=rc.get("channel"), turn_id=row.get("turn_id"),
        relaunch_ctx=rc, fresh=False, replace=False,
        build_turn=lambda replaced: builder(rc, True),
        owner_token=token,
    )
    return True


async def resume_orphans_at_boot(max_concurrent: int = BOOT_RESUME_CONCURRENCY) -> int:
    """Re-ignite every eligible run the previous process left behind. Called at
    startup AFTER scheduler/events are up. Throttled so boot does not stampede."""
    from app.db import get_db
    db = get_db()
    try:
        rows = await db.run_state_list_resumable()
    except Exception as e:  # noqa: BLE001
        logger.warning("resume_orphans_at_boot: list_resumable failed: %s", e)
        return 0
    if not rows:
        return 0

    sem = asyncio.Semaphore(max(1, max_concurrent))
    launched = 0

    async def _one(sid: str) -> None:
        nonlocal launched
        async with sem:
            try:
                if await resume_one(sid, reason="boot"):
                    launched += 1
            except Exception as e:  # noqa: BLE001
                logger.warning("boot resume failed for %s: %s", sid, e)

    await asyncio.gather(*[_one(r["session_id"]) for r in rows])
    logger.info("Boot resume: re-ignited %d/%d resumable run(s)", launched, len(rows))

    # Compaction-queued messages: a server restart may have killed a /compact
    # mid-fold, leaving user rows durably persisted as status='queued' with
    # metadata queued_for='compact'. Drain them so those messages still get
    # answered — they run as normal turns, and the client's pending bubble
    # clears on the drain's 'running' broadcast. (If a tab is open, the user's
    # /compact itself is also retried from the browser outbox, which re-runs
    # the fold properly.) Best-effort: a failure here must not break boot.
    try:
        from app.chat.execution import (
            _list_compaction_queued_sessions, _drain_compaction_queue,
        )
        _cqs = _list_compaction_queued_sessions(db)
        for _sid, _uid in _cqs:
            try:
                await _drain_compaction_queue(db, _sid, _uid or "", "web_portal", "ask")
            except Exception as _ce:
                logger.warning("boot compaction-drain failed for %s: %s",
                               str(_sid)[:12], _ce)
        if _cqs:
            logger.info("Boot compaction-drain: ran queued message(s) for %d session(s)", len(_cqs))
    except Exception as _cd:
        logger.warning("boot compaction-queue sweep failed: %s", _cd)
    return launched


# ── Generic background resume builder (automation / event / inbound / webhook) ──

async def _deliver_reply(channel: str, recipient: str, text: str) -> None:
    """Re-send a resumed background reply through its messaging channel (Mode A
    re-delivery, e.g. Telegram). Mirrors app/scheduler/executor.py _deliver."""
    try:
        from app.communications.manager import get_plugin_manager
        pm = get_plugin_manager()
        plugin = pm.get_plugin(channel)
        if plugin and getattr(plugin, "enabled", False) and recipient:
            await plugin.send_message(recipient, text)
    except Exception as e:  # noqa: BLE001
        logger.warning("resume re-delivery failed (%s → %s): %s", channel, recipient, e)


async def _generic_background_resume(rc: Dict[str, Any], replaced: bool) -> RunOutcome:
    """Rebuild and run a headless background turn from durable history. Used for
    automation / event / inbound / webhook origins (no UI attached)."""
    from app.db import get_db
    from app.agent.prompts import build_system_prompt
    from app.agent.session_history import build_openai_history_from_session
    from app.agent.loop import run_agent_loop_buffered

    db = get_db()
    sid = rc.get("session_id")
    user_id = rc.get("user_id")
    agent_id = await db.get_session_agent_id(sid) or rc.get("agent_id")
    agent = await db.get_agent_by_id(agent_id) if agent_id else None
    if not agent:
        return RunOutcome(status="error", stop_cause="failed",
                          error="agent not found for resume")

    # Re-check ability gating for automation/event (it may have been disabled).
    origin = rc.get("origin")
    if origin in ("automation", "event"):
        try:
            from app.admin.integrations import is_ability_enabled_for_agent
            ability = "automation" if origin == "automation" else "event"
            if not await is_ability_enabled_for_agent(agent_id, ability):
                return RunOutcome(status="error", stop_cause="failed",
                                  error=f"{ability} ability disabled")
        except Exception:
            pass

    # Rebuild the system prompt the way the background executors do.
    try:
        resolved = await db.resolve_prompts(agent_id, user_id=user_id)
        context_docs = [
            {"id": s["slot_name"], "context_type": s["slot_name"],
             "title": s["slot_name"], "content": s["content"], "tags": []}
            for s in resolved
            if (s.get("content") or "").strip() and s.get("slot_name") not in ("automation",)
        ]
    except Exception:
        context_docs = []
    system_prompt = await build_system_prompt(
        context_docs, brain_context=None, user_id=user_id, agent_id=agent_id,
        session_id=sid)

    # Pre-load full compacted history so the agent sees the conversation exactly
    # as it was before the crash — no bootstrap tool call, no nudge. user_message
    # is None so the raw transcript (ending at the last real message) is the
    # entire context and the agent continues unaware of the interruption.
    try:
        from app.agent.session_history import build_openai_history_from_session
        history = await build_openai_history_from_session(
            db, user_id, sid, agent_id=agent_id)
    except Exception:
        history = []

    raw_allowed = agent.get("allowed_tools", [])
    if isinstance(raw_allowed, str):
        try:
            raw_allowed = json.loads(raw_allowed)
        except Exception:
            raw_allowed = []

    reply = await run_agent_loop_buffered(
        user_id=user_id, session_id=sid, user_message=None,
        system_prompt=system_prompt, agent_id=agent_id, history=history,
        parent_interaction_id=rc.get("parent_interaction_id"),
        channel=rc.get("channel"), db=db, agent_template_id=agent.get("template_id"),
        allowed_tools=raw_allowed or None, max_turns=agent.get("max_turn_count", 0),
        timeout_seconds=rc.get("timeout_seconds") or 600,
    )

    # Mode-A re-delivery for inbound/webhook (the original caller is long gone).
    delivery = rc.get("delivery") or {}
    ch = delivery.get("channel")
    rcpt = delivery.get("recipient")
    if (ch and ch not in ("webchat", "silent") and not delivery.get("silent")
            and rcpt and reply):
        await _deliver_reply(ch, rcpt, reply)

    if not (reply or "").strip():
        # The resumed run's loop errored ("" is the honest signal from
        # run_agent_loop_buffered). Record a resumable failure instead of a
        # fake 'complete' so the watchdog can try again within the budget.
        return RunOutcome(status="error", stop_cause="empty_response",
                          error="resumed agent run produced no final reply")

    return RunOutcome(status="complete", stop_cause="complete", reply=reply)


# Default registrations for the headless background origins. The web origin
# registers its own (UI-emitting) builder from app/api/chat.py at import time.
# 'audit' is the Output Summarizer's checklist-auditor send-back: it rebuilds
# the same way the other headless origins do (durable history + the original
# parent carried in relaunch_ctx), so a crash mid-fix-loop resumes instead of
# stranding the task.
for _bg_origin in ("automation", "event", "inbound", "webhook", "audit"):
    register_resume_builder(_bg_origin, _generic_background_resume)

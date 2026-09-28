"""Run a blocking (synchronous-under-async) DB call OFF the main event loop.

WHY THIS EXISTS
---------------
The DB backends (SQLite / psycopg-Postgres) are *synchronous*. Their methods are
declared ``async def`` for a uniform interface, but the body runs the network
round-trip inline — so ``await db.method()`` executes the round-trip ON the event
loop and FREEZES every other coroutine for its full duration. On a *local* DB a
round-trip is sub-millisecond (invisible). On a *remote* DB each round-trip is
~150ms+, and because the chat LLM stream is an ``AsyncOpenAI`` HTTP call driven by
the SAME loop, every concurrent DB call (background pollers, per-turn writes,
streaming bookkeeping, memory saves) freezes the loop and stalls the model's
token stream. Measured: a no-tool reply spent 55s in send-prep and 37s in memory
recall — almost entirely loop STARVATION by serialized 150ms round-trips, not the
model.

WHAT THIS DOES
--------------
``db_offload(factory)`` hands the DB coroutine to a small, PERSISTENT pool of
worker threads (the "DB workers"). ``await``-ing it SUSPENDS the calling coroutine
and lets the main loop keep running other tasks — including the LLM stream — while
a worker thread waits on the network (psycopg/sqlite release the GIL on I/O).
Control flow and ordering are unchanged: a drop-in for ``await db.method()`` that
simply stops freezing the loop.

    # before:  await db.run_state_set_op(session_id, op)
    # after:   await db_offload(lambda: db.run_state_set_op(session_id, op))

WHY A PERSISTENT POOL (and not ``asyncio.to_thread`` + ``asyncio.run``)
----------------------------------------------------------------------
The first version did ``asyncio.to_thread(lambda: asyncio.run(factory()))``. That
worked but paid two avoidable costs on the chat hot path, where ``db_offload`` is
called many times per turn:

  * ``asyncio.run`` builds and tears down a BRAND-NEW event loop on EVERY call —
    pure overhead repeated dozens of times a turn.
  * ``asyncio.to_thread`` uses the default executor, a pool shared with every
    other ``to_thread`` caller in the process; under a burst of concurrent
    offloads (the per-turn read fan-out) calls queue behind unrelated work.

This module instead owns a DEDICATED, fixed-size pool of worker threads, each
holding ONE long-lived event loop that is reused for every call it runs. So a
``db_offload`` is just "submit the coroutine to an already-running worker" — no
loop spin-up, no contention with unrelated ``to_thread`` work. The pool is
fixed-size (bounded parallelism) and sized to sit UNDER the Postgres connection
pool cap, so workers never out-number available DB connections.

A fixed pool of N threads still gives REAL parallelism for the per-turn read
fan-out (N independent round-trips in flight at once), exactly as before — it just
stops re-creating the machinery every call.

WHY DRIVING WRITES ON A WORKER LOOP IS SAFE
-------------------------------------------
An earlier offload attempt broke chat: the backends guarded writes with an
``asyncio.Lock`` bound to the MAIN loop, so a write coroutine driven by a worker
thread's loop threw "is bound to a different event loop" and the turn died
(message stuck "pending"). That lock is now ``_DbWriteLock`` (a thread-safe
``threading.Semaphore`` — see app/db/local.py), which is loop- and thread-agnostic,
so the only remaining rule is the one below.

HARD RULE
---------
Only offload **pure-DB** coroutines. NEVER offload a method that awaits a
main-loop-bound async client — the embedding client (app/agent/embed.py), or the
WebSocket broadcaster ``notify_user`` reached via ``_emit_agent_run_status``
(so ``run_state_begin`` / ``run_state_finish`` and the memory embed/vector methods
stay on the main loop). Driving those on a worker loop corrupts the shared client.

NOTES
-----
* ``factory`` is a zero-arg callable returning a FRESH coroutine (use a lambda).
* The per-turn read cache is a ``ContextVar``. ``asyncio.to_thread`` copied the
  context for free; ``run_in_executor`` does NOT, so we copy it explicitly
  (``contextvars.copy_context()``) and run the coroutine inside that copy. The
  copy shares the SAME cache dict object, so fills made on a worker thread are
  still visible to the main turn — caching keeps working exactly as before.
"""
from __future__ import annotations

import asyncio
import contextvars
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Awaitable, Callable


def _worker_count() -> int:
    """Size of the DB-worker pool. Kept UNDER the Postgres connection-pool cap so
    workers never wait on a connection that doesn't exist. Tunable via env. The
    default (16) is the concurrency ceiling for the page-boot API burst: every
    remote read funnels through this pool, so at boot ~15-20 dynamic-data calls
    fire at once and this bounds how many run in parallel (was 8 → the burst
    queued in waves; measured ~12s cold-load contention). 16 stays under the PG
    pool cap (default 24) so a worker always has a connection to check out."""
    try:
        n = int(os.environ.get("WEBAGENT_DB_WORKERS") or 16)
    except ValueError:
        n = 16
    return max(1, n)


# A dedicated, fixed-size pool of worker threads. Built lazily on first use so
# importing this module is free, and so the pool is created under the running
# server (not at import time). Each thread gets its OWN persistent event loop via
# the thread-local below, created once and reused for every call that lands on it.
_EXECUTOR: ThreadPoolExecutor | None = None
_EXECUTOR_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()


def _get_executor() -> ThreadPoolExecutor:
    global _EXECUTOR
    if _EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _EXECUTOR is None:
                _EXECUTOR = ThreadPoolExecutor(
                    max_workers=_worker_count(),
                    thread_name_prefix="db-worker",
                )
    return _EXECUTOR


def _thread_loop() -> asyncio.AbstractEventLoop:
    """The calling worker thread's persistent event loop (created once, reused).

    The executor runs at most one job per thread at a time, so this loop is never
    driven re-entrantly — ``run_until_complete`` on it is always safe."""
    loop = getattr(_THREAD_LOCAL, "loop", None)
    if loop is None:
        loop = asyncio.new_event_loop()
        _THREAD_LOCAL.loop = loop
    return loop


def _run_in_worker(ctx: contextvars.Context, factory: Callable[[], Awaitable[Any]]) -> Any:
    """Worker-thread entry point: run ``factory()`` to completion on this thread's
    persistent loop, inside the captured context so the per-turn read cache (a
    ContextVar) is visible — and so cache fills here are seen by the main turn."""
    loop = _thread_loop()

    def _go():
        return loop.run_until_complete(factory())

    # ctx.run sets this thread's context to the captured copy for the duration of
    # _go; the coroutine (and the task run_until_complete wraps it in) is created
    # under that context, so it sees the same turn-cache dict.
    return ctx.run(_go)


async def db_offload(factory: Callable[[], Awaitable[Any]]) -> Any:
    """Run ``factory()`` (a PURE-DB async call) on a persistent DB-worker thread so
    the round-trip does not block the main event loop. Returns the call's result;
    exceptions propagate to the caller exactly as a direct ``await`` would. See the
    module docstring for the hard rule on what may NOT be offloaded."""
    ctx = contextvars.copy_context()
    main_loop = asyncio.get_running_loop()
    import time as _time
    _t0 = _time.perf_counter()
    try:
        return await main_loop.run_in_executor(_get_executor(), _run_in_worker, ctx, factory)
    finally:
        # Record the round-trip duration for the admin Dashboard's live DB-latency
        # cards (in-memory, no DB write — see app/metrics.py). Never let a metrics
        # hiccup affect the actual DB call.
        try:
            from app import metrics as _metrics
            _metrics.record_db_latency((_time.perf_counter() - _t0) * 1000.0)
        except Exception:
            pass

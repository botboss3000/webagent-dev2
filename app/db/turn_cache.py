"""Per-turn (request-scoped) read cache for the chat hot path.

On a remote Postgres database every DB read is a network round-trip (~150ms+).
A single chat turn re-reads the SAME invariant rows many times — the caller's
admin flag, vault secrets, the agent's connections / tool-modes / ability-modes,
its attached data sources. None of those change mid-turn, so reading them once
and reusing the answer removes dozens of round-trips per message.

How it works:
  * ``turn_cache_scope()`` (or the ``@with_turn_cache`` decorator) installs a
    fresh dict in a ``ContextVar`` for the duration of one turn.
  * ``@turn_cached`` on a backend read method consults that dict, keyed by
    (method, args). On a cache miss it runs the real read and stores the result;
    on a hit it returns the stored value with no DB round-trip.
  * OUTSIDE a scope the decorator is a transparent pass-through, so every other
    caller (config/preview endpoints, background jobs) is completely unaffected.

Thread propagation: ``asyncio.to_thread`` copies the current context into the
worker thread, so reads issued by ``_gather_db_concurrent`` (which run in worker
threads) share the SAME cache dict as the main turn. Concurrent fills are safe —
dict writes are atomic under the GIL and the reads are idempotent.

SAFETY: only decorate reads that are INVARIANT for the whole turn. Notably the
per-session "active tools/abilities/skills" reads are deliberately NOT cached —
the agent loop re-reads them mid-turn to pick up changes a tool just made.
"""
from __future__ import annotations

import asyncio
import functools
from concurrent.futures import Future
from contextvars import ContextVar
from typing import Any, Dict, Optional

# None = no active turn scope (pass-through). A dict = the live per-turn cache.
_TURN_CACHE: ContextVar[Optional[Dict[Any, Any]]] = ContextVar("webagent_turn_db_cache", default=None)


class _Scope:
    """Context manager that installs a fresh per-turn cache and restores the
    previous one on exit (supports nesting)."""

    __slots__ = ("_token",)

    def __enter__(self):
        self._token = _TURN_CACHE.set({})
        return self

    def __exit__(self, *exc):
        try:
            _TURN_CACHE.reset(self._token)
        except Exception:
            pass
        return False


def turn_cache_scope() -> _Scope:
    """Open a per-turn read-cache scope: ``with turn_cache_scope(): ...``."""
    return _Scope()


def turn_cache_invalidate(*qualname_parts: str) -> None:
    """Drop cached entries whose method qualname contains any of the given strings,
    from the active turn cache (no-op outside a scope). Call right after a WRITE that
    changes rows a turn-cached read may already have memoised this turn, so a later
    read in the same turn sees the new value (e.g. an OAuth token refreshed and then
    re-read inside one chat turn)."""
    cache = _TURN_CACHE.get()
    if not cache or not qualname_parts:
        return
    for key in [
        k for k in list(cache)
        if isinstance(k, tuple) and k and isinstance(k[0], str)
        and any(part in k[0] for part in qualname_parts)
    ]:
        cache.pop(key, None)


def turn_scope_active() -> bool:
    """True when a per-turn cache scope is currently installed. Lets a read method
    choose a turn-optimised path (e.g. a single bulk load it can dedup) only when
    it's actually inside a chat turn, and fall back to a plain point read for every
    other caller."""
    return _TURN_CACHE.get() is not None


def with_turn_cache(fn):
    """Decorator: run an async function inside a fresh per-turn cache scope."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        with turn_cache_scope():
            return await fn(*args, **kwargs)
    return wrapper


def turn_cached(fn):
    """Decorator for an async backend READ method. Memoizes its result for the
    duration of the current turn-cache scope, keyed by (method, args, kwargs).
    Pass-through (no caching) when no scope is active, or when the args aren't
    hashable."""
    @functools.wraps(fn)
    async def wrapper(self, *args, **kwargs):
        cache = _TURN_CACHE.get()
        if cache is None:
            return await fn(self, *args, **kwargs)
        try:
            key = (fn.__qualname__, args, tuple(sorted(kwargs.items())))
            _ = hash(key)
        except TypeError:
            return await fn(self, *args, **kwargs)
        # The context is copied into persistent DB-worker threads. A plain
        # get/await/set lets multiple workers observe the same miss and each
        # issue an identical remote query. Store a cross-loop Future first, so
        # concurrent callers share one in-flight read.
        existing = cache.get(key)
        if isinstance(existing, Future):
            return await asyncio.wrap_future(existing)
        if key in cache:
            return existing

        pending: Future = Future()
        # setdefault is atomic under the GIL, so only the winning worker runs
        # the query. concurrent.futures.Future is intentionally used rather
        # than asyncio.Future because waiters may belong to different loops.
        current = cache.setdefault(key, pending)
        if current is not pending:
            if isinstance(current, Future):
                return await asyncio.wrap_future(current)
            return current
        try:
            val = await fn(self, *args, **kwargs)
        except BaseException as exc:
            pending.set_exception(exc)
            # Failures are not cached; later reads can retry a transient issue.
            if cache.get(key) is pending:
                cache.pop(key, None)
            raise
        pending.set_result(val)
        if cache.get(key) is pending:
            cache[key] = val
        return val
    return wrapper

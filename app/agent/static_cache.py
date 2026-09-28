"""Cross-message in-process cache for per-agent STATIC data.

Some hot-path work is re-derived from the database on EVERY message even though
its inputs don't change between messages in a conversation — most expensively the
agent's tool set (``load_tools`` makes dozens of DB round-trips). On a remote
Postgres that re-derivation costs many seconds per message.

This is a tiny TTL + version-keyed memo. Entries are keyed by a caller-supplied
string and validated against a ``version`` token (the agent's ``updated_at``):

  * a hit returns instantly only if BOTH the version matches AND the entry hasn't
    passed its TTL;
  * any agent edit bumps ``agents.updated_at`` → the version changes → miss →
    fresh recompute. This is how invalidation works across MULTIPLE instances
    with no cross-process messaging: every instance reads the same ``updated_at``.
  * the TTL is a backstop for the rare edit that doesn't bump ``updated_at``
    (e.g. editing a shared tool's code), bounding staleness to ``ttl`` seconds.

It is deliberately dumb: a plain dict guarded by the GIL, no eviction beyond TTL
overwrite. The cached value sets are small (one per agent/user combo).
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

# key -> (value, version, expires_at_monotonic)
_CACHE: Dict[str, Tuple[Any, Any, float]] = {}

DEFAULT_TTL_SECONDS = 120.0


async def get_or_compute(
    key: str,
    version: Any,
    factory: Callable[[], Awaitable[Any]],
    ttl: float = DEFAULT_TTL_SECONDS,
) -> Any:
    """Return the cached value for ``key`` when it matches ``version`` and is
    within TTL; otherwise run ``factory()`` (an async thunk), store, and return.

    ``version`` of ``None`` disables caching (always recompute) — callers pass
    None when they can't determine a reliable version token."""
    if version is None:
        return await factory()
    now = time.monotonic()
    hit = _CACHE.get(key)
    if hit is not None:
        value, ver, expires_at = hit
        if ver == version and now < expires_at:
            return value
    value = await factory()
    _CACHE[key] = (value, version, now + ttl)
    return value


def invalidate(key_prefix: str = "") -> int:
    """Drop cached entries whose key starts with ``key_prefix`` ("" = all).
    Returns the number removed."""
    doomed = [k for k in _CACHE if k.startswith(key_prefix)]
    for k in doomed:
        _CACHE.pop(k, None)
    return len(doomed)

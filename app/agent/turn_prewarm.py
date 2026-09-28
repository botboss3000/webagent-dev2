"""Per-session PREWARM of the read-only chat prep.

The session/agent-scoped prep a turn needs — the agent's TOOL set and chat
HISTORY — does NOT depend on the message the
user is about to type. On a remote DB those reads cost several seconds. This
module lets the front-end build them WHILE THE USER IS STILL TYPING (via the
``/chat/prewarm`` endpoint) and stash them per session, so when the message is
actually sent the turn reads the bundle instead of repeating those round-trips.

CORRECTNESS OF THE CACHED HISTORY
  A bundle is only CONSUMED if it was built AFTER the session's last completed
  turn. ``mark_turn_done`` is called when a turn finishes; it records that
  timestamp and DROPS the session's bundle. So any bundle that survives to be
  consumed was necessarily built after the previous reply — it already includes
  it. A short TTL bounds staleness from any other writer (e.g. a message that
  arrives from another device between prewarm and send). On any doubt → miss,
  and the turn falls back to building the prep live (correct, just slower).

This is a deliberately dumb in-process dict (one entry per active session),
guarded by the GIL. It is an optimization only — a miss is always safe.
"""
from __future__ import annotations

import time
from typing import Any, Dict, Optional, Tuple

# session_id -> bundle dict
_BUNDLES: Dict[str, Dict[str, Any]] = {}
# session_id -> monotonic ts of the last completed turn. A bundle built at or
# before this is stale (it predates the latest reply) and must not be consumed.
_LAST_TURN_DONE: Dict[str, float] = {}

DEFAULT_TTL_SECONDS = 90.0

# Signature = the inputs that must match between prewarm time and send time for
# the bundle to be valid: (user_id, agent_id, agent_version/updated_at).
Sig = Tuple[Any, Any, Any]


def store(session_id: str, *, sig: Sig, tools: Any, history: Any,
          ttl: float = DEFAULT_TTL_SECONDS) -> None:
    """Stash a freshly-built prep bundle for ``session_id``."""
    now = time.monotonic()
    _BUNDLES[session_id] = {
        "sig": sig,
        "tools": tools,
        "history": history,
        "built_at": now,
        "expires_at": now + ttl,
    }


def consume(
    session_id: str,
    *,
    sig: Sig,
    pending_starts_new_task: bool = False,
) -> Optional[Dict[str, Any]]:
    """Return a valid bundle for ``session_id`` (matching sig, within TTL, built
    after the last completed turn), or None. Non-destructive: a superseding /
    retried turn for the same session may reuse it until ``mark_turn_done``.

    A prewarm is built before the pending user message exists, so its history
    cannot know that the message opens a new task. In that case the expensive
    tool reads remain reusable, but ``history`` is deliberately
    cleared so the send rebuilds it with the newly persisted user row and can
    reduce closed-task tool exchanges before the first provider call.
    """
    b = _BUNDLES.get(session_id)
    if not b:
        return None
    now = time.monotonic()
    if now >= b["expires_at"]:
        _BUNDLES.pop(session_id, None)
        return None
    if b["sig"] != sig:
        return None
    if b["built_at"] <= _LAST_TURN_DONE.get(session_id, 0.0):
        return None
    if not pending_starts_new_task:
        return b
    without_stale_history = dict(b)
    without_stale_history["history"] = None
    without_stale_history["history_reusable"] = False
    return without_stale_history


def mark_turn_done(session_id: str) -> None:
    """Record that a turn just finished and drop the session's bundle, so the
    NEXT turn re-warms with history that includes the reply just produced."""
    _LAST_TURN_DONE[session_id] = time.monotonic()
    _BUNDLES.pop(session_id, None)


def invalidate(session_id: str) -> None:
    """Forget any stashed bundle for a session (e.g. on agent switch)."""
    _BUNDLES.pop(session_id, None)

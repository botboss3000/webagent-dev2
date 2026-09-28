"""Deletion tombstones — the sync channel that carries a HARD delete to every
device's local mirror.

The pusher/puller in ``engine.py`` move rows local↔remote by UPSERT on a monotonic
``updated_at`` watermark. That mechanism can carry a *soft* delete (a status flip is
just another column value with a bumped watermark) but it can NOT carry a *hard*
delete: once a row is gone from the remote authority it simply stops appearing in
the puller's "rows newer than my watermark" query, so a second device that already
mirrored it never learns it was erased and shows a ghost (a deleted session in the
list, a deleted agent in the dropdown) until the app cold-starts.

A tombstone is the missing positive signal: when device A permanently deletes a
session or agent it writes a small row to the remote ``hybrid_tombstones`` table
(table_name + row_id + deleted_at). Every other device's puller reads new
tombstones on the ``deleted_at`` watermark and applies the matching local delete —
so a permanent delete converges everywhere within a sync tick, the same few seconds
as any other change.

Only the VISIBLE, user-facing rows are tombstoned (``sessions`` and ``agents``);
the puller cascades each one to its attached transcript locally (see
``SyncEngine._apply_tombstone_local``), so we never mint a tombstone per interaction
row. Writing is idempotent (upsert on a deterministic id) and best-effort: a failure
to record a tombstone only means a ghost lingers on another device until its next
cold start — it never blocks or corrupts the delete that already happened.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

TOMBSTONE_TABLE = "hybrid_tombstones"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_tombstone_table(conn) -> None:
    """Create the tombstone table if absent. Portable DDL (TEXT columns only), so
    it runs identically on the local SQLite mirror and the remote Postgres
    authority. Cheap to call repeatedly."""
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {TOMBSTONE_TABLE} ("
        "id TEXT PRIMARY KEY, "        # deterministic: "<table_name>:<row_id>"
        "table_name TEXT NOT NULL, "
        "row_id TEXT NOT NULL, "
        "user_id TEXT, "
        "deleted_at TEXT NOT NULL)"
    )


def _remote_of(db):
    """Return the raw remote authority backend behind whatever ``get_db()`` handed
    us (unwrapping an encryption decorator), or None when there is no distinct
    remote — a pure single-backend install, where every device reads the same DB
    directly so a delete is already visible everywhere and no tombstone is needed."""
    try:
        from app.db.hybrid import HybridBackend
        inner = db if isinstance(db, HybridBackend) else getattr(db, "_inner", None)
        return inner.remote if isinstance(inner, HybridBackend) else None
    except Exception:  # noqa: BLE001
        return None


def record_tombstones(db, pairs: Iterable[Tuple[str, str]],
                      user_id: Optional[str] = None) -> int:
    """Record hard-delete tombstones on the REMOTE authority so other devices can
    prune their local mirrors. ``pairs`` is ``[(table_name, row_id), ...]``.

    Best-effort and synchronous (hard deletes are rare — emptying the bin / an
    explicit permanent delete). Returns the number of tombstones written; 0 (and no
    error) when there is no distinct remote or the write fails."""
    rows = [(t, str(r)) for (t, r) in (pairs or []) if t and r]
    if not rows:
        return 0
    remote = _remote_of(db)
    if remote is None:
        return 0
    try:
        conn = remote._get_conn()
        try:
            ensure_tombstone_table(conn)
            conn.commit()
        finally:
            conn.close()
        now = _now_iso()
        client = remote.get_raw_client()
        written = 0
        for table_name, row_id in rows:
            payload = {
                "id": f"{table_name}:{row_id}",
                "table_name": table_name,
                "row_id": row_id,
                "user_id": user_id,
                "deleted_at": now,
            }
            client.table(TOMBSTONE_TABLE).upsert(payload, on_conflict="id").execute()
            written += 1
        return written
    except Exception as e:  # noqa: BLE001
        logger.warning("tombstones: failed to record %d hard-delete(s) (%s); "
                       "a ghost may linger on another device until cold start", len(rows), e)
        return 0

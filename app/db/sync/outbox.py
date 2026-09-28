"""The local outbox — a durable queue of Synced-tier rows waiting to be pushed
to the remote authority.

An outbox row is written in the SAME local transaction context as the data write
that produced it, so a crash never loses a pending push: either both the data row
and its outbox marker are committed, or neither is. Each entry names a table + a
row id + an op; the pusher re-reads the CURRENT row from the local store at flush
time, so several quick edits to the same row coalesce into one push (batching that
keeps rows while cutting round-trips — handoff §5/§8).

The outbox lives in the LOCAL SQLite database (via the local backend's own
connection), keyed by an autoincrement ``seq`` that also gives the pusher a stable
drain order.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

_TABLE = "hybrid_outbox"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Outbox:
    """Local pending-push queue backed by the local backend's SQLite connection."""

    def __init__(self, local) -> None:
        self._local = local
        self._ready = False

    def _ensure(self, conn) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
            "table_name TEXT NOT NULL, "
            "row_id TEXT NOT NULL, "
            "op TEXT NOT NULL DEFAULT 'upsert', "
            "created_at TEXT NOT NULL)"
        )
        self._ready = True

    async def enqueue(self, table: str, row_id: str, op: str = "upsert") -> None:
        """Record that ``table``/``row_id`` needs pushing to the remote authority."""
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                self._ensure(conn)
                conn.execute(
                    f"INSERT INTO {_TABLE} (table_name, row_id, op, created_at) VALUES (?, ?, ?, ?)",
                    (table, row_id, op, _now_iso()),
                )
                conn.commit()
            finally:
                conn.close()

    async def enqueue_many(self, items: List[tuple]) -> None:
        """Bulk enqueue ``[(table, row_id, op), ...]`` in one transaction."""
        if not items:
            return
        now = _now_iso()
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                self._ensure(conn)
                conn.executemany(
                    f"INSERT INTO {_TABLE} (table_name, row_id, op, created_at) VALUES (?, ?, ?, ?)",
                    [(t, r, (o or "upsert"), now) for (t, r, o) in items],
                )
                conn.commit()
            finally:
                conn.close()

    async def pending(self, limit: int = 500) -> List[Dict[str, Any]]:
        """Return up to ``limit`` oldest pending entries (seq asc)."""
        conn = self._local._get_conn()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
            ).fetchone()
            if not exists:
                return []
            rows = conn.execute(
                f"SELECT seq, table_name, row_id, op FROM {_TABLE} ORDER BY seq ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def clear(self, seqs: List[int]) -> None:
        """Delete flushed entries by ``seq`` (called after a successful push)."""
        if not seqs:
            return
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                self._ensure(conn)
                conn.executemany(f"DELETE FROM {_TABLE} WHERE seq = ?", [(s,) for s in seqs])
                conn.commit()
            finally:
                conn.close()

    async def count(self) -> int:
        conn = self._local._get_conn()
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (_TABLE,)
            ).fetchone()
            if not exists:
                return 0
            return int(conn.execute(f"SELECT COUNT(*) FROM {_TABLE}").fetchone()[0])
        finally:
            conn.close()

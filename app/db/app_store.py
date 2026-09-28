"""Explicit SQLite store for WebAgent's installation/control plane."""

from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Sequence

from app.db import db_crypto
from app.db.schema import ensure_sqlite_plane_columns, render_plane


class AppStore:
    """Small explicit handle for ``app.db``.

    This is intentionally not a ``StorageBackend``. Control-plane code states
    its intent by asking for this handle explicitly.
    """

    def __init__(self, path: str | os.PathLike[str], *, initialize: bool = True):
        self.path = Path(path)
        try:
            from app.db.storage_layout import APP_DB_PATH
            self._db_id = "app" if self.path.resolve() == APP_DB_PATH.resolve() else "_app_custom"
        except Exception:
            self._db_id = "_app_custom"
        self._initialize = initialize
        self._initialized = False
        self._init_lock = threading.Lock()

    def _connect(self):
        if self._initialize:
            self.ensure_schema()
        conn = db_crypto.connect(str(self.path), self._db_id, check_same_thread=False)
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def ensure_schema(self) -> None:
        if self._initialized:
            return
        with self._init_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = db_crypto.connect(str(self.path), self._db_id, check_same_thread=False)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA foreign_keys=ON")
                conn.executescript(render_plane("app", "sqlite"))
                ensure_sqlite_plane_columns(conn, "app")
                conn.commit()
            finally:
                conn.close()
            self._initialized = True

    @contextmanager
    def connection(self) -> Iterator[Any]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        with self.connection() as conn:
            cur = conn.execute(sql, tuple(params))
            conn.commit()
            return max(0, int(cur.rowcount or 0))

    def fetchone(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        with self.connection() as conn:
            row = conn.execute(sql, tuple(params)).fetchone()
            return dict(row) if row is not None else None

    def fetchall(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self.connection() as conn:
            return [dict(row) for row in conn.execute(sql, tuple(params)).fetchall()]

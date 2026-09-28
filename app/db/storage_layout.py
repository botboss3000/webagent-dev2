"""Versioned activation and explicit handles for the split storage layout."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DB_DIR = data_root() / "db"
APP_DB_PATH = DB_DIR / "app.db"
LEGACY_GLOBAL_DB_PATH = DB_DIR / "global.db"
LAYOUT_VERSION = 2
_LAYOUT_ROW_ID = 1
_ACTIVE_STATES = {"active"}

_app_store = None


def get_app_store(*, path: str | Path | None = None, initialize: bool = True):
    """Return the explicit app/control-plane store.

    A custom path always returns an uncached handle, which keeps tests and
    migration staging isolated from the process-global runtime handle.
    """
    from app.db.app_store import AppStore

    global _app_store
    if path is not None:
        return AppStore(path, initialize=initialize)
    if _app_store is None:
        _app_store = AppStore(APP_DB_PATH, initialize=initialize)
    return _app_store


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_layout(*, path: str | Path | None = None) -> dict | None:
    target = Path(path) if path is not None else APP_DB_PATH
    if not target.exists() or target.stat().st_size == 0:
        return None
    store = get_app_store(path=target, initialize=False)
    try:
        row = store.fetchone("SELECT * FROM storage_layout WHERE id=?", (_LAYOUT_ROW_ID,))
    except Exception:
        return None
    if row is not None:
        try:
            row["manifest"] = json.loads(row.pop("manifest_json") or "{}")
        except Exception:
            row["manifest"] = {}
    return row


def is_layout_active(*, path: str | Path | None = None) -> bool:
    """True only after a verified manifest has been explicitly activated."""
    row = read_layout(path=path)
    if not row:
        return False
    manifest = row.get("manifest") or {}
    return (
        int(row.get("layout_version") or 0) == LAYOUT_VERSION
        and row.get("state") in _ACTIVE_STATES
        and manifest.get("verified") is True
    )


def begin_layout(*, path: str | Path | None = None, manifest: dict | None = None) -> dict:
    store = get_app_store(path=path, initialize=True)
    now = _utcnow()
    payload = dict(manifest or {})
    payload.update({"verified": False, "layout_version": LAYOUT_VERSION})
    with store.connection() as conn:
        conn.execute(
            """INSERT INTO storage_layout
               (id,layout_version,state,manifest_json,started_at,activated_at,updated_at)
               VALUES (?,?,?,?,?,NULL,?)
               ON CONFLICT(id) DO UPDATE SET
                 layout_version=excluded.layout_version,
                 state='preparing', manifest_json=excluded.manifest_json,
                 activated_at=NULL, updated_at=excluded.updated_at""",
            (_LAYOUT_ROW_ID, LAYOUT_VERSION, "preparing", json.dumps(payload, sort_keys=True), now, now),
        )
        conn.commit()
    return read_layout(path=store.path) or {}


def activate_layout(*, path: str | Path | None = None, manifest: dict | None = None) -> dict:
    """Activate only when every recorded table migration is verified."""
    store = get_app_store(path=path, initialize=True)
    with store.connection() as conn:
        current = conn.execute(
            "SELECT manifest_json FROM storage_layout WHERE id=? AND layout_version=?",
            (_LAYOUT_ROW_ID, LAYOUT_VERSION),
        ).fetchone()
        if current is None:
            raise RuntimeError("Storage layout has not been prepared")
        try:
            payload = json.loads(current[0] or "{}")
        except Exception:
            payload = {}
        plane_status = payload.get("plane_status") or {}
        required = {"app", "user", "agent"}
        incomplete = sorted(plane for plane in required if plane_status.get(plane) != "verified")
        if incomplete:
            raise RuntimeError(f"Storage layout cannot activate: unverified planes {incomplete}")
        pending = conn.execute(
            "SELECT COUNT(*) FROM storage_migrations WHERE state != 'verified'"
        ).fetchone()[0]
        total = conn.execute("SELECT COUNT(*) FROM storage_migrations").fetchone()[0]
        if total == 0 or pending:
            raise RuntimeError(
                f"Storage layout cannot activate: {pending} unverified migrations, {total} total"
            )
        now = _utcnow()
        payload.update(manifest or {})
        payload.update({"verified": True, "layout_version": LAYOUT_VERSION})
        conn.execute(
            """UPDATE storage_layout
               SET state='active', manifest_json=?, activated_at=?, updated_at=?
               WHERE id=? AND layout_version=?""",
            (json.dumps(payload, sort_keys=True), now, now, _LAYOUT_ROW_ID, LAYOUT_VERSION),
        )
        if conn.total_changes == 0:
            raise RuntimeError("Storage layout has not been prepared")
        conn.commit()
    return read_layout(path=store.path) or {}


def mark_plane_status(
    plane: str,
    state: str,
    *,
    path: str | Path | None = None,
    summary: dict | None = None,
) -> dict:
    if plane not in {"app", "user", "agent"}:
        raise ValueError(f"Unknown authority plane: {plane}")
    if state not in {"pending", "preparing", "verified", "failed"}:
        raise ValueError(f"Invalid plane migration state: {state}")
    store = get_app_store(path=path, initialize=True)
    with store.connection() as conn:
        row = conn.execute(
            "SELECT manifest_json FROM storage_layout WHERE id=?", (_LAYOUT_ROW_ID,)
        ).fetchone()
        if row is None:
            raise RuntimeError("Storage layout has not been prepared")
        try:
            payload = json.loads(row[0] or "{}")
        except Exception:
            payload = {}
        statuses = dict(payload.get("plane_status") or {})
        statuses[plane] = state
        payload["plane_status"] = statuses
        if summary is not None:
            summaries = dict(payload.get("plane_summaries") or {})
            summaries[plane] = summary
            payload["plane_summaries"] = summaries
        conn.execute(
            "UPDATE storage_layout SET manifest_json=?, updated_at=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), _utcnow(), _LAYOUT_ROW_ID),
        )
        conn.commit()
    return read_layout(path=store.path) or {}


def reset_cached_handles() -> None:
    global _app_store
    _app_store = None

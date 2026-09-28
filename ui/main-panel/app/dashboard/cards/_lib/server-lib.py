"""server-lib.py — shared helpers for dashboard CARD PLUGIN backends.

Every card that ships a ``server.py`` (contributing a ``build_section(ctx)``
snapshot section) imports the time/query helpers from here instead of
redefining them, so the parsing + SQL conventions live in ONE place. The
dashboard SHELL server (dashboard/server.py) imports the same helpers.

REMOVE-WHEN: the Dashboard tab is dropped from the Instances page.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("webagent.dashboard.cards")


# ── time helpers (shared conventions: stored timestamps are space-separated UTC) ─
def to_epoch(val: Any) -> float:
    """Parse a stored timestamp (ISO string or epoch number) to epoch seconds.
    Returns 0 on anything unparseable so it falls outside every window."""
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    s = str(val).strip()
    if not s:
        return 0.0
    try:
        return float(s)
    except ValueError:
        pass
    try:
        s2 = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


def iso_since(window_s: float) -> str:
    return datetime.fromtimestamp(time.time() - window_s, tz=timezone.utc).isoformat()


def iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def sql_ts(epoch: float) -> str:
    """A lower-bound string that matches how ``created_at`` is STORED — space
    separated UTC, no ``T`` and no offset. Comparisons are lexical on SQLite
    (text) and string-cast on Postgres; an ISO bound with a ``T`` sorts AFTER a
    space and silently drops same-day rows. The caller re-filters by epoch, so
    this bound only needs to sort correctly."""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def raw_rows(table: str, cols: str, limit: int, order: Optional[str] = None) -> List[Dict[str, Any]]:
    """One raw-client table read (worker-thread caller wraps this).

    Reads the CONTROL (central) database: this is the admin-global dashboard,
    and billing/usage_events + the account plane live centrally. In user-BYOD
    mode individual users' private data lives in their own databases and is not
    part of this global view by design. No-op difference in single-tenant mode."""
    from app.db import get_control_db
    q = get_control_db().get_raw_client().table(table).select(cols)
    if order:
        q = q.order(order, desc=True)
    res = q.limit(limit).execute()
    return getattr(res, "data", None) or []


def parse_meta(raw: Any) -> Dict[str, Any]:
    """Parse a metadata column that may be a JSON string or already a dict."""
    if isinstance(raw, str):
        try:
            return json.loads(raw) or {}
        except Exception:
            return {}
    return raw or {}

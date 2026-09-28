"""CRUD for the ``browser_sessions`` table.

A browser session is a first-class, persistent browser TAB that lives beside
chat. Agent-created rows are scoped to their owning chat session, while manually
created user tabs may remain unscoped until explicitly handed to an agent.

Backend-agnostic: everything routes through ``get_db().get_raw_client()`` (the
local query-builder idiom, emulated for SQLite/Postgres in
``app/db/local.py``), so the same code runs on every backend with no per-dialect
SQL. Used by the Playwright engine (``app/tools/browser.py`` keys live pages by
these rows' ids) and the REST + WebSocket layer (``app/api/browser_stream.py``).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.db import get_db


def _now() -> str:
    # Match SQLite's datetime('now') format (UTC, no tz suffix) so values sort and
    # compare identically across SQLite and the Postgres TEXT-timestamp parity.
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _client():
    return get_db().get_raw_client()


def _decode(row: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Normalize a raw row: parse storage_state JSON, coerce ``shared`` to bool."""
    if not row:
        return None
    out = dict(row)
    out["shared"] = bool(out.get("shared"))
    ss = out.get("storage_state")
    if isinstance(ss, str) and ss:
        try:
            out["storage_state"] = json.loads(ss)
        except Exception:  # noqa: BLE001
            out["storage_state"] = None
    return out


def create(user_id: str, *, agent_id: Optional[str] = None,
           chat_session_id: Optional[str] = None, title: Optional[str] = None,
           url: Optional[str] = None, shared: bool = False, position: int = 0) -> Dict[str, Any]:
    """Create a browser session (a tab). Private by default (shared=False)."""
    row = {
        "id": str(uuid.uuid4()),
        "user_id": user_id,
        "agent_id": agent_id,
        "chat_session_id": chat_session_id,
        "title": title,
        "url": url,
        "shared": 1 if shared else 0,
        "status": "active",
        "storage_state": None,
        "position": position,
        "created_at": _now(),
        "updated_at": _now(),
    }
    _client().table("browser_sessions").insert(row).execute()
    return _decode(row)  # type: ignore[return-value]


def get(bs_id: str) -> Optional[Dict[str, Any]]:
    res = _client().table("browser_sessions").select("*").eq("id", bs_id).limit(1).execute()
    rows = res.data or []
    return _decode(rows[0]) if rows else None


def list_for_user(user_id: str) -> List[Dict[str, Any]]:
    res = (_client().table("browser_sessions").select("*")
           .eq("user_id", user_id).order("position").order("created_at").execute())
    return [_decode(r) for r in (res.data or [])]  # type: ignore[misc]


def list_shared_for_agent(user_id: str, agent_id: str) -> List[Dict[str, Any]]:
    """Tabs the given agent may see: owned by the user, linked to the agent, shared."""
    res = (_client().table("browser_sessions").select("*")
           .eq("user_id", user_id).eq("agent_id", agent_id).eq("shared", 1)
           .order("position").execute())
    return [_decode(r) for r in (res.data or [])]  # type: ignore[misc]


def list_shared_for_agent_session(user_id: str, agent_id: str,
                                  chat_session_id: str) -> List[Dict[str, Any]]:
    """Shared tabs owned by exactly one agent chat session."""
    res = (_client().table("browser_sessions").select("*")
           .eq("user_id", user_id).eq("agent_id", agent_id)
           .eq("chat_session_id", chat_session_id).eq("shared", 1)
           .order("position").order("created_at").execute())
    return [_decode(r) for r in (res.data or [])]  # type: ignore[misc]


def update(bs_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    """Patch a row. Coerces ``shared`` to int and serializes ``storage_state``.
    Always bumps ``updated_at``. Unknown keys are passed through verbatim."""
    data: Dict[str, Any] = {}
    for k, v in fields.items():
        if k == "shared":
            data[k] = 1 if v else 0
        elif k == "storage_state":
            data[k] = json.dumps(v) if v is not None else None
        else:
            data[k] = v
    if not data:
        return get(bs_id)
    data["updated_at"] = _now()
    _client().table("browser_sessions").update(data).eq("id", bs_id).execute()
    return get(bs_id)


def delete(bs_id: str) -> None:
    _client().table("browser_sessions").delete().eq("id", bs_id).execute()


def get_storage_state(bs_id: str) -> Optional[Dict[str, Any]]:
    row = get(bs_id)
    return row.get("storage_state") if row else None


def set_storage_state(bs_id: str, state: Optional[Dict[str, Any]]) -> None:
    update(bs_id, storage_state=state)

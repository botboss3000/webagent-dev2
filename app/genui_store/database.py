"""Database-backed GenuiStore.

Stores page metadata + HTML body in the `pages` table via the active
StorageBackend (SQLite or Postgres). Survives container restarts, gets
backed up with the DB, and stays consistent with the rest of the app's
storage architecture.
"""

import json
from typing import Dict, List, Optional

from app.db import get_db
from app.genui_store.interface import GenuiStore
from app.genui_store.common import (
    default_agent_context,
    home_agent_context,
    genui_url,
    safe,
)


def _entry(row: Dict, user_id: str) -> Dict:
    """Project a DB row into the manifest-entry shape the UI expects."""
    # session_config column → dict (JSON). Legacy rows default to {}.
    _sc = row.get("session_config") or {}
    if isinstance(_sc, str):
        try:
            _sc = json.loads(_sc) if _sc.strip() else {}
        except Exception:
            _sc = {}
    if not isinstance(_sc, dict):
        _sc = {}
    return {
        "slug": row["slug"],
        "title": row["title"],
        "agent_context": row.get("agent_context") or "",
        "agent_id": row.get("agent_id") or "",
        "session_config": _sc,
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "url": genui_url(user_id, row["slug"]),
    }


class DatabaseGenuiStore(GenuiStore):
    name = "database"

    async def list_genui(self, user_id: str) -> List[Dict]:
        rows = await get_db().genui_list(user_id)
        return [_entry(r, user_id) for r in rows]

    async def get_genui_html(self, user_id: str, slug: str) -> Optional[str]:
        row = await get_db().genui_get(user_id, slug)
        return row.get("html") if row else None

    async def save_genui_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        existing = await get_db().genui_get(user_id, slug)
        title = (existing or {}).get("title") or slug.capitalize()
        ctx = (existing or {}).get("agent_context") or ""
        await get_db().genui_upsert(
            user_id=user_id,
            slug=slug,
            title=title,
            agent_context=ctx,
            html=html,
            agent_id=agent_id,
        )
        return genui_url(user_id, slug)

    async def create_genui(
        self,
        user_id: str,
        slug: str,
        title: str,
        agent_context: str = "",
        initial_html: str = "",
        agent_id: str = "",
        session_config: Optional[dict] = None,
    ) -> Dict:
        from app.genui_store.common import blank_genui_html
        safe_slug = safe(slug)
        if await get_db().genui_get(user_id, safe_slug):
            raise ValueError(f"Gen UI '{safe_slug}' already exists")
        row = await get_db().genui_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=title,
            agent_context=agent_context or default_agent_context(title),
            html=initial_html or blank_genui_html(title),
            agent_id=agent_id,
            session_config=json.dumps(session_config or {}),
        )
        return _entry(row, user_id)

    async def delete_genui(self, user_id: str, slug: str) -> bool:
        if safe(slug) == "home":
            return False
        return await get_db().genui_delete(user_id, safe(slug))

    async def rename_genui(self, user_id: str, slug: str, new_title: str,
                           session_config: Optional[dict] = None) -> bool:
        safe_slug = safe(slug)
        existing = await get_db().genui_get(user_id, safe_slug)
        if not existing:
            return False
        # html=None on upsert preserves the existing body and only updates
        # the metadata columns (title + agent_context + session_config + updated_at).
        await get_db().genui_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=new_title,
            agent_context=existing.get("agent_context") or "",
            html=None,
            session_config=json.dumps(session_config) if session_config is not None else None,
        )
        return True

    async def get_genui_data(self, user_id: str, slug: str) -> Optional[Dict]:
        raw = await get_db().genui_get_data(user_id, safe(slug))
        if not raw:
            return None
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
            return data if isinstance(data, dict) else None
        except Exception:
            return None

    async def save_genui_data(self, user_id: str, slug: str, data: Dict) -> None:
        await get_db().genui_set_data(user_id, safe(slug), json.dumps(data))

    async def get_genui_widget(self, user_id: str, slug: str) -> Optional[Dict]:
        raw = await get_db().genui_get_widget(user_id, safe(slug))
        if not raw:
            return None
        try:
            widget = json.loads(raw) if isinstance(raw, str) else raw
            return widget if isinstance(widget, dict) else None
        except Exception:
            return None

    async def save_genui_widget(self, user_id: str, slug: str, widget: Dict) -> None:
        await get_db().genui_set_widget(user_id, safe(slug), json.dumps(widget))

    async def ensure_home_genui(self, user_id: str, seed_html: str) -> None:
        existing = await get_db().genui_get(user_id, "home")
        if existing:
            return
        await get_db().genui_upsert(
            user_id=user_id,
            slug="home",
            title="Home",
            agent_context=home_agent_context(),
            html=seed_html,
        )
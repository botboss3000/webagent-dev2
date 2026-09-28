"""Hybrid GenuiStore.

Metadata (slug, title, agent_context, timestamps) lives in the `pages`
table via the active StorageBackend; the HTML body lives on disk at
data/user_data/{uid}/genui/{slug}/index.html (one folder per genui).

Trade-off: genui index survives container restarts and is queryable across
backends, but HTML bodies remain hand-editable on disk and serve from a
single fast read. Best of both for setups that want DB-backed catalog +
direct file access. Container restarts will still wipe the bodies unless
the disk is persistent.
"""

import json
import os
import shutil
from typing import Dict, List, Optional

from app.db import get_db
from app.genui_store.interface import GenuiStore
from app.genui_store.common import (
    genui_dir,
    genui_body_path,
    read_genui_data,
    write_genui_data,
    read_genui_widget,
    write_genui_widget,
    blank_genui_html,
    default_agent_context,
    home_agent_context,
    genui_url,
    safe,
)


def _entry(row: Dict, user_id: str) -> Dict:
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


class HybridGenuiStore(GenuiStore):
    name = "hybrid"

    def _genui_path(self, user_id: str, slug: str) -> str:
        return genui_body_path(user_id, slug)

    def _ensure_genui_dir(self, user_id: str, slug: str) -> str:
        d = genui_dir(user_id, slug)
        os.makedirs(d, exist_ok=True)
        return d

    def _write_body(self, user_id: str, slug: str, html: str) -> None:
        self._ensure_genui_dir(user_id, slug)
        with open(self._genui_path(user_id, slug), "w", encoding="utf-8") as f:
            f.write(html)

    def _read_body(self, user_id: str, slug: str) -> Optional[str]:
        path = self._genui_path(user_id, slug)
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def list_genui(self, user_id: str) -> List[Dict]:
        rows = await get_db().genui_list(user_id)
        return [_entry(r, user_id) for r in rows]

    async def get_genui_html(self, user_id: str, slug: str) -> Optional[str]:
        return self._read_body(user_id, slug)

    async def save_genui_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        existing = await get_db().genui_get(user_id, slug)
        title = (existing or {}).get("title") or slug.capitalize()
        ctx = (existing or {}).get("agent_context") or ""
        # html=None on upsert leaves the column NULL (body lives on disk)
        await get_db().genui_upsert(
            user_id=user_id,
            slug=slug,
            title=title,
            agent_context=ctx,
            html=None,
            agent_id=agent_id,
        )
        self._write_body(user_id, slug, html)
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
        safe_slug = safe(slug)
        if await get_db().genui_get(user_id, safe_slug):
            raise ValueError(f"Gen UI '{safe_slug}' already exists")
        row = await get_db().genui_upsert(
            user_id=user_id,
            slug=safe_slug,
            title=title,
            agent_context=agent_context or default_agent_context(title),
            html=None,
            agent_id=agent_id,
            session_config=json.dumps(session_config or {}),
        )
        self._write_body(user_id, safe_slug, initial_html or blank_genui_html(title))
        return _entry(row, user_id)

    async def delete_genui(self, user_id: str, slug: str) -> bool:
        safe_slug = safe(slug)
        if safe_slug == "home":
            return False
        deleted = await get_db().genui_delete(user_id, safe_slug)
        folder = genui_dir(user_id, safe_slug)
        if os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        return deleted

    async def rename_genui(self, user_id: str, slug: str, new_title: str,
                           session_config: Optional[dict] = None) -> bool:
        safe_slug = safe(slug)
        existing = await get_db().genui_get(user_id, safe_slug)
        if not existing:
            return False
        # Body lives on disk — only the DB metadata row needs updating.
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
        # Like the HTML body, genui data lives on disk in hybrid mode.
        return read_genui_data(user_id, safe(slug))

    async def save_genui_data(self, user_id: str, slug: str, data: Dict) -> None:
        safe_slug = safe(slug)
        self._ensure_genui_dir(user_id, safe_slug)
        write_genui_data(user_id, safe_slug, data)

    async def get_genui_widget(self, user_id: str, slug: str) -> Optional[Dict]:
        # Like data.json, the widget config lives on disk in hybrid mode.
        return read_genui_widget(user_id, safe(slug))

    async def save_genui_widget(self, user_id: str, slug: str, widget: Dict) -> None:
        safe_slug = safe(slug)
        self._ensure_genui_dir(user_id, safe_slug)
        write_genui_widget(user_id, safe_slug, widget)

    async def ensure_home_genui(self, user_id: str, seed_html: str) -> None:
        existing = await get_db().genui_get(user_id, "home")
        if not existing:
            await get_db().genui_upsert(
                user_id=user_id,
                slug="home",
                title="Home",
                agent_context=home_agent_context(),
                html=None,
            )
        if not os.path.exists(self._genui_path(user_id, "home")):
            self._write_body(user_id, "home", seed_html)
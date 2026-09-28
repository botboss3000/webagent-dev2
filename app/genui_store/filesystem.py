"""Filesystem-backed GenuiStore — default.

Stores each genui per-user at data/user_data/{user_id}/genui/{slug}/index.html
— one folder per genui. Following the same drop-in philosophy as abilities and
main-panel pages, a folder containing an ``index.html`` IS a genui: no central
manifest. Each folder MAY carry an optional ``page.json`` descriptor holding the
genui's title, agent_context, description and display ``order``; when it's
absent the title defaults to the slug (the folder name) and the genui sorts
alphabetically after the explicitly-ordered ones. Drop a folder in → it appears;
delete it → it's gone.

Simple, fast, hand-editable on disk. Loses genui on container restart in
stateless deploys (e.g. Cloud Run) — for those use the database or hybrid
backends.
"""

import os
import shutil
from typing import Dict, List, Optional

from app.genui_store.interface import GenuiStore
from app.genui_store.common import (
    genui_dir,
    genui_body_path,
    genui_entry,
    discover_genui_slugs,
    read_genui_meta,
    write_genui_meta,
    read_genui_data,
    write_genui_data,
    read_genui_widget,
    write_genui_widget,
    sort_genui_entries,
    blank_genui_html,
    default_agent_context,
    home_agent_context,
    genui_url,
    now_iso,
    safe,
)


class FilesystemGenuiStore(GenuiStore):
    name = "filesystem"

    # ── Path helpers ─────────────────────────────────────────────────────

    def _genui_path(self, user_id: str, slug: str) -> str:
        return genui_body_path(user_id, slug)

    def _ensure_genui_dir(self, user_id: str, slug: str) -> str:
        d = genui_dir(user_id, slug)
        os.makedirs(d, exist_ok=True)
        return d

    # ── GenuiStore impl ───────────────────────────────────────────────────

    async def list_genui(self, user_id: str) -> List[Dict]:
        entries = [genui_entry(user_id, slug) for slug in discover_genui_slugs(user_id)]
        return sort_genui_entries(entries)

    async def get_genui_html(self, user_id: str, slug: str) -> Optional[str]:
        path = self._genui_path(user_id, safe(slug))
        if not os.path.exists(path):
            return None
        with open(path, "r", encoding="utf-8") as f:
            return f.read()

    async def save_genui_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        safe_slug = safe(slug)
        self._ensure_genui_dir(user_id, safe_slug)
        with open(self._genui_path(user_id, safe_slug), "w", encoding="utf-8") as f:
            f.write(html)
        # Bump updated_at when a descriptor already exists; ALSO write one when an
        # agent rendered this genui (agent_id given) so its owning-agent link
        # persists — a supplied agent_id wins, otherwise the existing owner is
        # kept. A descriptor-less, agent-less render stays descriptor-free.
        meta = read_genui_meta(user_id, safe_slug)
        if meta or agent_id:
            if agent_id:
                meta["agent_id"] = agent_id
            meta["updated_at"] = now_iso()
            write_genui_meta(user_id, safe_slug, meta)
        return genui_url(user_id, safe_slug)

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
        if os.path.exists(self._genui_path(user_id, safe_slug)):
            raise ValueError(f"Gen UI '{safe_slug}' already exists")
        now = now_iso()
        meta = {
            "title": title,
            "agent_context": agent_context or default_agent_context(title),
            "agent_id": agent_id or "",
            "session_config": session_config or {},
            "description": "",
            "created_at": now,
            "updated_at": now,
        }
        self._ensure_genui_dir(user_id, safe_slug)
        with open(self._genui_path(user_id, safe_slug), "w", encoding="utf-8") as f:
            f.write(initial_html or blank_genui_html(title))
        write_genui_meta(user_id, safe_slug, meta)
        return genui_entry(user_id, safe_slug)

    async def delete_genui(self, user_id: str, slug: str) -> bool:
        safe_slug = safe(slug)
        if safe_slug == "home":
            return False
        folder = genui_dir(user_id, safe_slug)
        if not os.path.isdir(folder):
            return False
        shutil.rmtree(folder, ignore_errors=True)
        return True

    async def rename_genui(self, user_id: str, slug: str, new_title: str,
                           session_config: Optional[dict] = None) -> bool:
        safe_slug = safe(slug)
        if not os.path.exists(self._genui_path(user_id, safe_slug)):
            return False
        # Persisting a title requires a descriptor — create one if the genui
        # didn't have a page.json yet.
        meta = read_genui_meta(user_id, safe_slug)
        meta["title"] = new_title
        if session_config is not None:
            meta["session_config"] = session_config
        meta["updated_at"] = now_iso()
        if not meta.get("agent_context"):
            meta["agent_context"] = default_agent_context(new_title)
        write_genui_meta(user_id, safe_slug, meta)
        return True

    async def get_genui_data(self, user_id: str, slug: str) -> Optional[Dict]:
        return read_genui_data(user_id, safe(slug))

    async def save_genui_data(self, user_id: str, slug: str, data: Dict) -> None:
        safe_slug = safe(slug)
        self._ensure_genui_dir(user_id, safe_slug)
        write_genui_data(user_id, safe_slug, data)
        # Mirror save_genui_html: bump updated_at only when a descriptor exists.
        meta = read_genui_meta(user_id, safe_slug)
        if meta:
            meta["updated_at"] = now_iso()
            write_genui_meta(user_id, safe_slug, meta)

    async def get_genui_widget(self, user_id: str, slug: str) -> Optional[Dict]:
        return read_genui_widget(user_id, safe(slug))

    async def save_genui_widget(self, user_id: str, slug: str, widget: Dict) -> None:
        safe_slug = safe(slug)
        self._ensure_genui_dir(user_id, safe_slug)
        write_genui_widget(user_id, safe_slug, widget)
        # Mirror save_genui_data: bump updated_at only when a descriptor exists.
        meta = read_genui_meta(user_id, safe_slug)
        if meta:
            meta["updated_at"] = now_iso()
            write_genui_meta(user_id, safe_slug, meta)

    async def ensure_home_genui(self, user_id: str, seed_html: str) -> None:
        if not os.path.exists(self._genui_path(user_id, "home")):
            self._ensure_genui_dir(user_id, "home")
            with open(self._genui_path(user_id, "home"), "w", encoding="utf-8") as f:
                f.write(seed_html)
        if not read_genui_meta(user_id, "home"):
            now = now_iso()
            write_genui_meta(user_id, "home", {
                "title": "Home",
                "agent_context": home_agent_context(),
                "description": "",
                "order": 0,  # Home always sorts first.
                "created_at": now,
                "updated_at": now,
            })

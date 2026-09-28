"""
Abstract page-store backend.

A GenuiStore owns the persistence of genui — the per-user HTML
documents rendered into the Gen UI tab iframe and edited via the page-builder.

Implementations:
  - FilesystemGenuiStore   one folder per genui on disk at
                          data/user_data/{uid}/genui/{slug}/index.html, with an
                          optional per-folder page.json descriptor (title,
                          agent_context, order, …). No central manifest — a
                          folder with an index.html IS a genui. (default)
  - DatabaseGenuiStore     full HTML + metadata in the `pages` table
  - HybridGenuiStore       metadata in the `pages` table, body on disk
"""

from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class GenuiStore(ABC):
    """Async per-user store for genui."""

    name: str = "unknown"

    @abstractmethod
    async def list_genui(self, user_id: str) -> List[Dict]:
        """Return catalog entries for a user, sorted for display. Each entry has
        keys: slug, title, description, agent_context, order, created_at,
        updated_at, url. (The home genui is seeded by the facade, not here.)"""
        ...

    @abstractmethod
    async def get_genui_html(self, user_id: str, slug: str) -> Optional[str]:
        """Return the HTML body for a page, or None if not found."""
        ...

    @abstractmethod
    async def save_genui_html(self, user_id: str, slug: str, html: str, agent_id: str = "") -> str:
        """Write HTML for a page and bump its updated_at. Returns the page URL.
        `agent_id` records the agent that rendered it (the genui's owner); a
        supplied value wins, otherwise the existing owner is preserved."""
        ...

    @abstractmethod
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
        """Create a new page. Raises ValueError if slug already exists.
        Returns the manifest entry including its URL. `agent_id` is the creating
        agent (the genui's owner). `session_config` is the REQUIRED session
        contract for the page's actions/chat — {target_name, mode, session_id?}
        — see the API request model for the shape and validation rules."""
        ...

    @abstractmethod
    async def delete_genui(self, user_id: str, slug: str) -> bool:
        """Delete a page. The 'home' page cannot be deleted; returns False."""
        ...

    @abstractmethod
    async def rename_genui(self, user_id: str, slug: str, new_title: str,
                           session_config: Optional[dict] = None) -> bool:
        """Update a page's display title and optionally its session config.
        Slug and body are preserved. Returns False if no page with that slug
        exists. When `session_config` is None the existing config is kept."""
        ...

    @abstractmethod
    async def ensure_home_genui(self, user_id: str, seed_html: str) -> None:
        """Seed the home page for user_id if it doesn't already exist."""
        ...

    @abstractmethod
    async def get_genui_data(self, user_id: str, slug: str) -> Optional[Dict]:
        """Return a genui's DATA object (the content it renders), or None when it
        has no data file. Kept separate from the HTML body so the agent can update
        a genui's content without rewriting its page markup."""
        ...

    @abstractmethod
    async def save_genui_data(self, user_id: str, slug: str, data: Dict) -> None:
        """Write (replace) a genui's DATA object. Bumps updated_at where the
        backend tracks it."""
        ...

    @abstractmethod
    async def get_genui_widget(self, user_id: str, slug: str) -> Optional[Dict]:
        """Return a genui's WIDGET config (widget.json — the page's launcher /
        widget options), or None when it has no widget file."""
        ...

    @abstractmethod
    async def save_genui_widget(self, user_id: str, slug: str, widget: Dict) -> None:
        """Write (replace) a genui's WIDGET config. Bumps updated_at where the
        backend tracks it."""
        ...

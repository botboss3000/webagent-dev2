"""
Page management for the Gen UI multi-page workspace.

Thin async facade over the configurable PageStore (filesystem / database /
hybrid). Pick the backend from the App Config → Storage panel or via the
WEBAGENT_GENUI_STORE env var. See app/genui_store/__init__.py for details.
"""
from typing import Dict, List, Optional

from app.genui_store import get_genui_store
from app.genui_store.seed import home_genui_html
from app.genui_store import common as _genui_files


async def list_genui(user_id: str) -> List[Dict]:
    """Return all pages for a user, seeding home if missing."""
    store = get_genui_store()
    await store.ensure_home_genui(user_id, home_genui_html())
    pages = await store.list_genui(user_id)
    # RECORDER-HOOK: optional plugin pages (plugins/recorder/genui). Remove this
    # merge and the HTML fallback below together; keep normal stored pages.
    from app.application_plugins import genui_pages
    installed = await genui_pages(user_id)
    known = {page["slug"] for page in pages}
    return pages + [page for page in installed if page["slug"] not in known]


async def get_genui_html(user_id: str, slug: str) -> Optional[str]:
    html = await get_genui_store().get_genui_html(user_id, slug)
    if html is not None:
        return html
    # RECORDER-HOOK: no persistent source copy for plugin-owned GenUI. Remove
    # this fallback with the catalog merge; preserve stored-page return above.
    from app.application_plugins import genui_html
    return await genui_html(user_id, slug)


async def get_genui_data(user_id: str, slug: str) -> Optional[Dict]:
    """Return a genui's data bag (the content it renders), or None when unset."""
    return await get_genui_store().get_genui_data(user_id, slug)


async def save_genui_data(user_id: str, slug: str, data: Dict) -> None:
    """Replace a genui's data bag. The genui reads this instead of hardcoding
    its content into the page, so updating data never touches index.html."""
    await get_genui_store().save_genui_data(user_id, slug, data)


async def get_genui_widget(user_id: str, slug: str) -> Optional[Dict]:
    """Return a genui's widget config (the page's launcher/widget.json options:
    which agent the page's chat launcher opens, icon, corner buttons, widget
    options), or None when unset."""
    return await get_genui_store().get_genui_widget(user_id, slug)


async def save_genui_widget(user_id: str, slug: str, widget: Dict) -> None:
    """Replace a genui's widget config. The Gen UI tab reads it (baked into the
    served HTML as window.__GENUI_WIDGET) and mounts the page's chat launcher
    from it, so the agent edits the launcher without touching the page markup."""
    await get_genui_store().save_genui_widget(user_id, slug, widget)


async def save_genui_html(user_id: str, slug: str, html: str, title: str = "",
                          agent_id: str = "", session_config: Optional[dict] = None) -> str:
    """Write HTML to a genui, auto-registering it in the manifest if it's new.

    The store's own ``save_genui_html`` only updates the timestamp of an
    EXISTING manifest entry — a brand-new slug written straight through
    ``render_visual`` (without a prior ``create_genui``) would save its HTML but
    never appear in the page selector. So when the slug isn't listed yet we
    create it (which registers it + writes the HTML in one step); the agent can
    therefore add a new genui simply by rendering to it. ``session_config`` is
    the session contract ({target_name, mode, session_id?}) carried into the
    auto-create — required by render_visual when the page is brand new.

    ``agent_id`` records the agent doing the render as the genui's owner — set on
    the auto-create and carried into the plain save (last writer wins; an existing
    owner is preserved when no agent_id is supplied).
    """
    store = get_genui_store()
    try:
        existing = await store.list_genui(user_id)
        if not any(p.get("slug") == slug for p in (existing or [])):
            nice = title or slug.replace("-", " ").replace("_", " ").strip().title() or slug
            try:
                created = await store.create_genui(
                    user_id=user_id, slug=slug, title=nice, initial_html=html,
                    agent_id=agent_id, session_config=session_config,
                )
                if isinstance(created, dict) and created.get("url"):
                    return created["url"]
            except ValueError:
                pass  # already exists (race) — fall through to a plain save
    except Exception:
        pass  # never let registration failure block the actual save
    saved = await store.save_genui_html(user_id, slug, html, agent_id=agent_id)
    # A re-render is a NEW version of the page — wipe its console log so the agent
    # only ever reads errors from the build that's now running (the browser will
    # re-capture as the new page mounts). Best-effort; never block the save.
    try:
        _genui_files.clear_genui_logs(user_id, slug)
    except Exception:
        pass
    return saved


# ── Per-genui console log (filesystem-local; see common.py) ───────────────────
# The page's own console output, captured in the browser and POSTed back, kept in
# a file beside index.html — a page-scoped debug log the design agent reads WITHOUT
# any logs.db / codebase-admin access. These thin wrappers keep the genui API
# routes + the get_genui_logs tool importing from one place.

def append_genui_logs(user_id: str, slug: str, entries, replace_source: str = None) -> int:
    """Append console entries for a genui (returns lines kept). `replace_source`
    first drops existing lines from that source (e.g. 'headless') before appending."""
    return _genui_files.append_genui_logs(user_id, slug, entries, replace_source=replace_source)


def read_genui_logs(user_id: str, slug: str, limit: int = 100, level: str = None):
    """Return a genui's most recent console entries (newest last), optional level filter."""
    return _genui_files.read_genui_logs(user_id, slug, limit=limit, level=level)


def clear_genui_logs(user_id: str, slug: str) -> None:
    """Wipe a genui's console log (also done automatically on each re-render)."""
    _genui_files.clear_genui_logs(user_id, slug)


async def create_genui(
    user_id: str,
    slug: str,
    title: str,
    agent_context: str = "",
    initial_html: str = "",
    agent_id: str = "",
    session_config: Optional[dict] = None,
) -> Dict:
    return await get_genui_store().create_genui(
        user_id=user_id,
        slug=slug,
        title=title,
        agent_context=agent_context,
        initial_html=initial_html,
        agent_id=agent_id,
        session_config=session_config,
    )


async def delete_genui(user_id: str, slug: str) -> bool:
    return await get_genui_store().delete_genui(user_id, slug)


async def rename_genui(user_id: str, slug: str, new_title: str,
                       session_config: Optional[dict] = None) -> bool:
    return await get_genui_store().rename_genui(user_id, slug, new_title,
                                                session_config=session_config)


async def ensure_home_genui(user_id: str) -> None:
    await get_genui_store().ensure_home_genui(user_id, home_genui_html())

"""Wiki Control ability — SELF-CONTAINED drop-in.

Gates the company-wide Wiki tools and OWNS their handlers + schemas outright —
nothing here is wired into core. The factory ``build_wiki_tools`` (formerly the
core module ``app.tools.wiki_tools``) now lives in this file, alongside the
per-tool JSON schemas (``WIKI_TOOL_SCHEMAS``) and the destructive-tool set
(``WIKI_DESTRUCTIVE``). Drop this file in → the Wiki tools appear; delete it →
they're gone. That is the entire contract.

The Wiki is one shared, searchable knowledge base (company info, policies,
contacts, any reference data). These tools wrap the same store the HTTP API and
the UI use (``app/wiki/store.py``), so an article an agent writes shows up
immediately in the Wiki tab and in everyone's search.

  wiki_search       — semantic + keyword search across visible articles
  wiki_list         — list visible articles (titles/tags/snippets)
  wiki_get          — read one full article by slug
  wiki_create       — add a new article (defaults to draft/internal)
  wiki_update       — edit an existing article
  wiki_set_status   — publish (make public) or unpublish (make internal)
  wiki_delete       — remove an article
  wiki_history      — list an article's prior versions (revision history)
  wiki_get_revision — read the full text of one past revision
  wiki_restore      — roll an article back to a past revision
  wiki_backlinks    — find which articles link to a given one

Article bodies are **Markdown** — write headings, lists, bold, tables, links.
Link to another wiki article with double brackets: ``[[Article Title]]`` (or
``[[slug]]``). Those become live links in the UI and feed the backlinks graph.

**Visibility (draft vs published).** Every article is either ``draft`` (internal —
only signed-in members can see it) or ``published`` (public — visible to everyone,
including anonymous visitors). New articles start as **draft**; call
``wiki_set_status`` to publish when it's ready to share. The tools honour who you
are acting for: when serving a **signed-in member** you can read & search drafts
AND published; when serving an **anonymous visitor** you can only see published
articles, and editing is refused. So never put confidential content in a
published article, and don't publish something unless it's meant for the public.

It is discovered generically by core via two optional module hooks (see
app/abilities/__init__.py "Self-contained abilities"):
  • FEATURE / .json descriptor — catalog + UI + which tool names it gates
  • build_tools(...)           — its tool handlers (app/tools/loader.py injects them)

build_tools builds the handler dict and populates module-level TOOL_SCHEMAS /
DESTRUCTIVE from the factory constants below (the loader reads them AFTER the
call). Heavy imports (the wiki store) stay LAZY inside the factory.
"""

from __future__ import annotations

import json

# Populated by build_tools() from the factory constants (see below). The loader
# reads these AFTER calling build_tools, so filling them there avoids drift.
TOOL_SCHEMAS: dict = {}
DESTRUCTIVE: set = set()


async def build_prompt_context(*, user_id: str, query: str, limit: int = 3, **_ctx) -> str:
    """Recall small, relevant Wiki excerpts for this turn's system context.

    This hook is discovered generically by ``app.abilities`` and runs only when
    Wiki Control is enabled for the active agent. It deliberately injects
    snippets rather than whole articles so automatic recall stays bounded; the
    agent can call ``wiki_get`` when it needs the complete source.
    """
    from app.wiki import store as _wiki

    include_drafts = not _wiki.is_public_actor(user_id)
    try:
        results = await _wiki.search_articles(
            query, limit=max(1, min(int(limit or 3), 5)), include_drafts=include_drafts,
        )
    except Exception:
        return ""
    if not results:
        return ""

    lines = [
        "## Relevant company Wiki excerpts",
        "These excerpts were recalled automatically by Wiki Control. Use wiki_get for a full article before relying on details beyond an excerpt.",
    ]
    for article in results:
        slug = str(article.get("slug") or "?")
        title = str(article.get("title") or slug)
        snippet = str(article.get("snippet") or article.get("body") or "").strip()[:500]
        lines.append(f"### {title} (`{slug}`)")
        if snippet:
            lines.append(snippet)
    return "\n\n".join(lines)


def _ok(**data) -> str:
    out = {"status": "ok"}
    out.update(data)
    return json.dumps(out, default=str)


def _err(msg: str, **extra) -> str:
    out = {"status": "error", "error": msg}
    out.update(extra)
    return json.dumps(out)


def build_wiki_tools(user_id: str):
    """Return {tool_name: handler} bound to this user (for visibility + write
    attribution). When ``user_id`` is anonymous/public, reads are limited to
    published articles and writes are refused."""

    # Heavy import kept LAZY: only the wiki store, pulled in when an agent
    # actually has this ability and we build its handlers.
    from app.wiki import store as _wiki
    from app.wiki.store import is_public_actor

    # Visibility gate for this acting user: members see drafts + published;
    # anonymous / public callers see only published.
    _public = is_public_actor(user_id)
    _include_drafts = not _public

    def _editing_blocked() -> str:
        return _err("Editing the wiki requires a signed-in member; this request "
                    "is from an anonymous visitor.")

    async def wiki_search(query: str = "", limit: int = 10) -> str:
        """Search the company Wiki by meaning AND keywords. Use this first to
        find existing knowledge before answering or before creating a new
        article. Returns ranked matches with titles, slugs, tags, and snippets.
        (When serving an anonymous visitor, only published articles are matched.)"""
        try:
            results = await _wiki.search_articles(query, limit=int(limit or 10), include_drafts=_include_drafts)
            return _ok(results=results, count=len(results), query=query)
        except Exception as e:
            return _err(f"wiki search failed: {e}")

    async def wiki_list() -> str:
        """List every visible Wiki article (metadata + a short snippet), newest
        first. Use wiki_search when you have a topic; use this to browse what
        exists. (Anonymous visitors only see published articles.)"""
        try:
            articles = await _wiki.list_articles(include_drafts=_include_drafts)
            return _ok(articles=articles, count=len(articles))
        except Exception as e:
            return _err(f"wiki list failed: {e}")

    async def wiki_get(slug: str) -> str:
        """Read one full Wiki article by its slug (from search/list results).
        A draft article is invisible (not found) when serving an anonymous user."""
        try:
            article = await _wiki.get_article(slug, include_drafts=_include_drafts)
            if article is None:
                return _err(f"no article with slug '{slug}'")
            return _ok(article=article)
        except Exception as e:
            return _err(f"wiki get failed: {e}")

    async def wiki_create(
        title: str, body: str = "", tags=None, category: str = "", status: str = "draft"
    ) -> str:
        """Create a new Wiki article (ADMINS only — non-admin members are
        refused, matching the REST API). The slug is derived from the title.
        Search first to avoid duplicating an existing entry.

        New articles default to status 'draft' (internal — members only). Pass
        status='published' only if it should be visible to the public/anonymous
        visitors right away; otherwise publish later with wiki_set_status.

        The body is Markdown — use headings, lists, tables, bold, links. To link
        to another wiki article, write [[Article Title]] (or [[slug]]); it renders
        as a live link and shows up in that article's backlinks."""
        if _public:
            return _editing_blocked()
        # Creating articles is admin-only (mirrors the REST API's 403 for
        # non-admin members); editing existing articles stays member-level.
        try:
            from app.db import get_db
            if not await get_db().is_user_admin(user_id):
                return _err("Creating wiki articles is restricted to admins.")
        except Exception:
            return _err("Creating wiki articles is restricted to admins.")
        try:
            article = await _wiki.create_article(
                title=title, body=body or "", tags=tags,
                category=category or "", user_id=user_id, status=status or "draft",
            )
            return _ok(article=article, created=True)
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"wiki create failed: {e}")

    async def wiki_update(
        slug: str, title: str = None, body: str = None,
        tags=None, category: str = None, status: str = None,
    ) -> str:
        """Edit an existing Wiki article. Only the fields you pass change; the
        slug (URL) is preserved. Read it first with wiki_get if you need the
        current body before rewriting it. The previous version is automatically
        saved to history (see wiki_history / wiki_restore).

        Pass status='published' or 'draft' to change visibility while editing
        (or omit it to keep the current state; you can also use wiki_set_status).

        The body is Markdown; link to other articles with [[Article Title]]."""
        if _public:
            return _editing_blocked()
        try:
            article = await _wiki.update_article(
                slug=slug, title=title, body=body, tags=tags,
                category=category, user_id=user_id, status=status,
            )
            if article is None:
                return _err(f"no article with slug '{slug}'")
            return _ok(article=article, updated=True)
        except ValueError as e:
            return _err(str(e))
        except Exception as e:
            return _err(f"wiki update failed: {e}")

    async def wiki_set_status(slug: str, status: str) -> str:
        """Publish or unpublish an article without changing its content.
        status='published' makes it public (visible to anonymous visitors);
        status='draft' makes it internal again (members only). Use this to share
        an article with the public, or to pull something back to internal."""
        if _public:
            return _editing_blocked()
        try:
            article = await _wiki.set_article_status(slug, status, user_id=user_id)
            if article is None:
                return _err(f"no article with slug '{slug}'")
            return _ok(article=article, status=article.get("status"))
        except Exception as e:
            return _err(f"wiki set_status failed: {e}")

    async def wiki_delete(slug: str) -> str:
        """Delete a Wiki article permanently. This removes it for everyone."""
        if _public:
            return _editing_blocked()
        try:
            ok = await _wiki.delete_article(slug)
            if not ok:
                return _err(f"no article with slug '{slug}'")
            return _ok(deleted=True, slug=slug)
        except Exception as e:
            return _err(f"wiki delete failed: {e}")

    async def wiki_history(slug: str) -> str:
        """List an article's prior versions (revision history), newest first.
        Each entry has a revision id (for wiki_get_revision / wiki_restore), who
        edited it, when, and a snippet. (Members only — not for anonymous users.)"""
        if _public:
            return _editing_blocked()
        try:
            revs = await _wiki.list_revisions(slug)
            return _ok(revisions=revs, count=len(revs), slug=slug)
        except Exception as e:
            return _err(f"wiki history failed: {e}")

    async def wiki_get_revision(revision_id: str) -> str:
        """Read the full text of one past revision (from a wiki_history entry's
        id) — without changing the live article. (Members only.)"""
        if _public:
            return _editing_blocked()
        try:
            rev = await _wiki.get_revision(revision_id)
            if rev is None:
                return _err(f"no revision with id '{revision_id}'")
            return _ok(revision=rev)
        except Exception as e:
            return _err(f"wiki get_revision failed: {e}")

    async def wiki_restore(slug: str, revision_id: str) -> str:
        """Roll an article back to a past revision (from wiki_history). The
        current version is snapshotted to history first, so this is reversible."""
        if _public:
            return _editing_blocked()
        try:
            article = await _wiki.restore_revision(slug, revision_id, user_id=user_id)
            if article is None:
                return _err(f"no revision with id '{revision_id}'")
            return _ok(article=article, restored=True)
        except Exception as e:
            return _err(f"wiki restore failed: {e}")

    async def wiki_backlinks(slug: str) -> str:
        """Find which articles link TO this one (via [[slug]] or [[Title]]).
        Useful for understanding how a topic is referenced before editing it."""
        try:
            links = await _wiki.get_backlinks(slug)
            return _ok(backlinks=links, count=len(links), slug=slug)
        except Exception as e:
            return _err(f"wiki backlinks failed: {e}")

    return {
        "wiki_search": wiki_search,
        "wiki_list": wiki_list,
        "wiki_get": wiki_get,
        "wiki_create": wiki_create,
        "wiki_update": wiki_update,
        "wiki_set_status": wiki_set_status,
        "wiki_delete": wiki_delete,
        "wiki_history": wiki_history,
        "wiki_get_revision": wiki_get_revision,
        "wiki_restore": wiki_restore,
        "wiki_backlinks": wiki_backlinks,
    }


# JSON-Schema parameter definitions, keyed by tool name. The loader pairs these
# with the handlers above when building ToolInfo entries.
WIKI_TOOL_SCHEMAS = {
    "wiki_search": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "What to look for. Matched by meaning and keywords across titles, bodies, and tags."},
            "limit": {"type": "integer", "description": "Max results to return (default 10)."},
        },
        "required": ["query"],
    },
    "wiki_list": {"type": "object", "properties": {}, "required": []},
    "wiki_get": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article's slug (from a search/list result)."},
        },
        "required": ["slug"],
    },
    "wiki_create": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Article title. The URL slug is derived from this."},
            "body": {"type": "string", "description": "Article content in Markdown (headings, lists, tables, bold, links). Link to another wiki article with [[Article Title]] or [[slug]]."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "Optional tags for filtering, e.g. ['finance','2026']."},
            "category": {"type": "string", "description": "Optional grouping label, e.g. 'HR' or 'Product'."},
            "status": {"type": "string", "enum": ["draft", "published"], "description": "Visibility. 'draft' (default) = internal, members only. 'published' = public, visible to anonymous visitors. Leave as draft unless it should be public immediately."},
        },
        "required": ["title"],
    },
    "wiki_update": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to edit (its slug)."},
            "title": {"type": "string", "description": "New title (optional; omit to keep current)."},
            "body": {"type": "string", "description": "New full body in Markdown (optional; replaces the current body). Link to other articles with [[Article Title]]."},
            "tags": {"type": "array", "items": {"type": "string"}, "description": "New tag list (optional; replaces current tags)."},
            "category": {"type": "string", "description": "New category (optional)."},
            "status": {"type": "string", "enum": ["draft", "published"], "description": "Optional new visibility ('draft' = internal, 'published' = public). Omit to keep the current state."},
        },
        "required": ["slug"],
    },
    "wiki_set_status": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to publish or unpublish."},
            "status": {"type": "string", "enum": ["draft", "published"], "description": "'published' makes it public (visible to anonymous visitors); 'draft' makes it internal (members only)."},
        },
        "required": ["slug", "status"],
    },
    "wiki_delete": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to delete (its slug)."},
        },
        "required": ["slug"],
    },
    "wiki_history": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article whose revision history to list."},
        },
        "required": ["slug"],
    },
    "wiki_get_revision": {
        "type": "object",
        "properties": {
            "revision_id": {"type": "string", "description": "A revision id from a wiki_history entry."},
        },
        "required": ["revision_id"],
    },
    "wiki_restore": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to roll back."},
            "revision_id": {"type": "string", "description": "The revision id (from wiki_history) to restore."},
        },
        "required": ["slug", "revision_id"],
    },
    "wiki_backlinks": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "The article to find inbound links for."},
        },
        "required": ["slug"],
    },
}

# Writes to the shared wiki require the user's confirmation in Ask/Plan mode —
# create, update, status changes, restores and deletes all mutate a knowledge base
# others rely on. Reads (search / list / get / history / revision / backlinks) run
# free. (Auto execution mode still runs everything without a pause.)
WIKI_DESTRUCTIVE = {
    "wiki_create", "wiki_update", "wiki_set_status", "wiki_restore", "wiki_delete",
}


def build_tools(*, user_id: str = "", session_id: str = "", agent_id: str = "",
                agent_template_id: str = "", enabled_providers=None, **ctx):
    """Return {tool_name: handler} for the Wiki tools.

    Builds the handler dict from the in-module factory and mirrors its
    schema/destructive constants into this module's TOOL_SCHEMAS / DESTRUCTIVE
    (the loader reads those AFTER this call, so filling them here avoids drift).
    """
    handlers = build_wiki_tools(user_id)

    TOOL_SCHEMAS.clear()
    TOOL_SCHEMAS.update(WIKI_TOOL_SCHEMAS)
    DESTRUCTIVE.clear()
    DESTRUCTIVE.update(WIKI_DESTRUCTIVE)

    return handlers

"""Async facade over the wiki storage layer.

Thin wrapper around the ``wiki_*`` methods on the active StorageBackend
(``app/db/local.py``), plus slug derivation and a couple of validation rules.
Used by both the HTTP API (``app/api/wiki.py``) and the agent tools
(the Wiki Control ability, ``plugins/abilities/Memory/wiki_context.py``) so the
two stay in lock-step.
"""
from __future__ import annotations

import re
from typing import List, Optional

from app.wiki.db import get_wiki_store

MAX_TITLE = 120

# Article visibility statuses.
STATUS_DRAFT = "draft"          # internal — signed-in members (and their agents) only
STATUS_PUBLISHED = "published"  # public — visible to everyone, incl. anonymous visitors


def normalize_status(status) -> str:
    """Coerce any input to a valid status; anything that isn't 'published' is a draft."""
    return STATUS_PUBLISHED if str(status or "").strip().lower() == STATUS_PUBLISHED else STATUS_DRAFT


def is_public_actor(user_id) -> bool:
    """True when the caller should be treated as anonymous / public.

    Registered members have a real user_id (their email, or the bootstrap
    'admin'). Anonymous visitors always get an 'anon_…' user_id (see
    app/communications/auth.create_anonymous_identity). No user_id (no token)
    is also treated as public. Public actors see only PUBLISHED articles.
    """
    uid = str(user_id or "").strip()
    return (not uid) or uid.startswith("anon_")


def slugify(text: str) -> str:
    """Derive a url-safe, globally-unique-ish handle from a title.

    Lowercase, spaces/punctuation collapse to single hyphens, trimmed. Empty
    input falls back to ``"untitled"``.
    """
    s = (text or "").strip().lower()
    s = re.sub(r"[^\w\s-]", "", s)        # drop punctuation
    s = re.sub(r"[\s_-]+", "-", s)         # whitespace/underscores → hyphen
    s = s.strip("-")
    return s or "untitled"


def _norm_tags(tags) -> List[str]:
    """Coerce tags into a clean list of non-empty strings."""
    if tags is None:
        return []
    if isinstance(tags, str):
        tags = re.split(r"[,\n]", tags)
    out = []
    for t in tags:
        t = str(t).strip()
        if t and t not in out:
            out.append(t)
    return out


async def list_articles(include_drafts: bool = True) -> List[dict]:
    """All visible articles (metadata + short snippet), newest first.
    ``include_drafts`` False → only published (for public/anonymous callers)."""
    return await get_wiki_store().list(include_drafts=include_drafts)


async def get_article(slug: str, include_drafts: bool = True) -> Optional[dict]:
    """Full article by slug, or None. Public callers can't see drafts."""
    return await get_wiki_store().get(slug, include_drafts=include_drafts)


async def search_articles(query: str, limit: int = 10, include_drafts: bool = True) -> List[dict]:
    """Hybrid (semantic + keyword) search. Returns ranked results with snippets.
    Public callers only match published articles."""
    query = (query or "").strip()
    if not query:
        return []
    return await get_wiki_store().search(query, limit=limit, include_drafts=include_drafts)


async def create_article(
    title: str,
    body: str = "",
    tags=None,
    category: str = "",
    user_id: str = "",
    slug: Optional[str] = None,
    status: str = STATUS_DRAFT,
) -> dict:
    """Create a new article. Slug is derived from the title unless given; a
    numeric suffix is appended if the slug is already taken. New articles default
    to ``draft`` (internal) — publish explicitly to make them public."""
    title = (title or "").strip()
    if not title:
        raise ValueError("Title is required.")
    store = get_wiki_store()
    base = slugify(slug or title)
    final = base
    n = 2
    while await store.get(final) is not None:
        final = f"{base}-{n}"
        n += 1
    return await store.upsert(
        slug=final,
        title=title[:MAX_TITLE],
        body=body or "",
        tags=_norm_tags(tags),
        category=(category or "").strip(),
        user_id=user_id,
        status=normalize_status(status),
    )


async def update_article(
    slug: str,
    title: Optional[str] = None,
    body: Optional[str] = None,
    tags=None,
    category: Optional[str] = None,
    user_id: str = "",
    status: Optional[str] = None,
) -> Optional[dict]:
    """Update fields on an existing article. Only provided fields change; the
    slug (URL) is preserved. ``status`` None keeps the current draft/published
    state. Returns the updated article, or None if missing."""
    store = get_wiki_store()
    existing = await store.get(slug)
    if existing is None:
        return None
    new_title = (title if title is not None else existing["title"]).strip()
    if not new_title:
        raise ValueError("Title cannot be empty.")
    new_tags = _norm_tags(tags) if tags is not None else existing.get("tags", [])
    return await store.upsert(
        slug=slug,
        title=new_title[:MAX_TITLE],
        body=body if body is not None else existing.get("body", ""),
        tags=new_tags,
        category=(category if category is not None else existing.get("category", "")).strip(),
        user_id=user_id,
        status=normalize_status(status) if status is not None else None,
    )


async def set_article_status(slug: str, status: str, user_id: str = "") -> Optional[dict]:
    """Publish or unpublish an article (no body change, no new revision).
    Returns the updated article, or None if the slug doesn't exist."""
    return await get_wiki_store().set_status(slug, normalize_status(status), user_id=user_id)


async def delete_article(slug: str) -> bool:
    """Delete an article. Returns False if it didn't exist."""
    return await get_wiki_store().delete(slug)


async def list_revisions(slug: str) -> List[dict]:
    """Prior versions of an article, newest first."""
    return await get_wiki_store().list_revisions(slug)


async def get_revision(rev_id: str) -> Optional[dict]:
    """One full historical revision by id."""
    return await get_wiki_store().get_revision(rev_id)


async def restore_revision(slug: str, rev_id: str, user_id: str = "") -> Optional[dict]:
    """Restore an article to a past revision (reversible). Returns the article."""
    return await get_wiki_store().restore_revision(slug, rev_id, user_id=user_id)


async def get_backlinks(slug: str, include_drafts: bool = True) -> List[dict]:
    """Articles that link to this one via [[slug]] or [[Title]].
    Public callers only see published articles among the backlinks."""
    return await get_wiki_store().backlinks(slug, include_drafts=include_drafts)

"""
REST API for the company-wide Wiki — a shared, searchable knowledge base.

The Wiki is NOT per-user: every article is visible to everyone within its
**visibility**, and searched together. ``user_id`` is recorded for attribution.

Visibility (draft vs published):
  • Each article is ``draft`` (internal) or ``published`` (public).
  • **Signed-in members** (and agents acting for them) see BOTH — drafts and
    published. **Anonymous / public callers** see ONLY published articles.
  • New articles default to ``draft`` so nothing is exposed publicly until it is
    deliberately published.
  • Writes (create / edit / delete / publish / restore) require a signed-in
    member — anonymous callers get 403.
  • CREATE is additionally admin-only: signed-in members can read + edit
    existing articles, but only global admins can add new ones (403 otherwise).

The caller is identified from the optional ``Authorization: Bearer <jwt>`` header
(or ``?token=``); no/invalid token, or an ``anon_…`` user, is treated as public.

Endpoints:
  GET    /api/v1/wiki                       — list visible articles (metadata + snippet)
  GET    /api/v1/wiki/search?q=…            — hybrid (semantic + keyword) search
  GET    /api/v1/wiki/{slug}                — one full article
  GET    /api/v1/wiki/{slug}/backlinks      — inbound [[links]]
  GET    /api/v1/wiki/{slug}/revisions      — revision history (members)
  GET    /api/v1/wiki/revisions/{rev_id}    — one full revision (members)
  POST   /api/v1/wiki                       — create (ADMINS only; default draft)
  PATCH  /api/v1/wiki/{slug}                — update fields incl. status (members)
  POST   /api/v1/wiki/{slug}/publish        — make public (members)
  POST   /api/v1/wiki/{slug}/unpublish      — make internal/draft (members)
  POST   /api/v1/wiki/{slug}/restore/{rev_id} — restore a revision (members)
  DELETE /api/v1/wiki/{slug}                — delete (members)
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel

from app.auth.identity import caller_may_access_page, request_user_id, resolve_admin_uid
from app.wiki.store import (
    list_articles,
    get_article,
    search_articles,
    create_article,
    update_article,
    set_article_status,
    delete_article,
    list_revisions,
    get_revision,
    restore_revision,
    get_backlinks,
    is_public_actor,
)

async def _require_wiki_page(request: Request) -> None:
    if not await caller_may_access_page(request, "main", "wiki"):
        raise HTTPException(
            status_code=403,
            detail={"code": "page_access_denied", "page_id": "wiki"},
        )


router = APIRouter(
    prefix="/api/v1/wiki", tags=["wiki"],
    dependencies=[Depends(_require_wiki_page)],
)


# ── Caller identification ──────────────────────────────────────────────────────

def _actor_user_id(request: Request) -> str:
    """Resolve the caller's user_id from the optional Bearer token (or ?token=).
    Returns "" when there is no/invalid token.

    Delegates to the shared chokepoint, so in 'open' access mode a tokenless
    caller resolves to the bootstrap admin — letting wiki editing work over a
    Cloudflare Tunnel, where the browser can't mint a JWT (see
    app.auth.identity.request_user_id)."""
    return request_user_id(request)


def _can_see_drafts(request: Request) -> bool:
    """True for signed-in members; False for anonymous / public callers."""
    return not is_public_actor(_actor_user_id(request))


def _require_member(request: Request) -> str:
    """Return the member's user_id, or 403 if the caller is anonymous/public."""
    actor = _actor_user_id(request)
    if is_public_actor(actor):
        raise HTTPException(status_code=403, detail="Sign in to edit the wiki.")
    return actor


async def _require_admin(request: Request) -> str:
    """Return the admin's user_id, or 403 if the caller isn't a global admin.

    Gates article CREATION (the "New article" feature): signed-in members may
    still read and edit existing articles, but only admins create new ones."""
    actor = _require_member(request)
    if not await resolve_admin_uid(actor):
        raise HTTPException(status_code=403, detail="Only admins can create wiki articles.")
    return actor


class CreateArticleRequest(BaseModel):
    title: str
    body: Optional[str] = ""
    tags: Optional[List[str]] = None
    category: Optional[str] = ""
    status: Optional[str] = "draft"      # 'draft' (default) | 'published'
    user_id: Optional[str] = ""          # fallback attribution; token wins


class UpdateArticleRequest(BaseModel):
    title: Optional[str] = None
    body: Optional[str] = None
    tags: Optional[List[str]] = None
    category: Optional[str] = None
    status: Optional[str] = None         # None = keep current draft/published state
    user_id: Optional[str] = ""


class StatusRequest(BaseModel):
    user_id: Optional[str] = ""


class RestoreRequest(BaseModel):
    user_id: Optional[str] = ""


@router.get("")
async def api_list_wiki(request: Request):
    """List every visible article (metadata + a short body snippet), newest first."""
    articles = await list_articles(include_drafts=_can_see_drafts(request))
    return {"status": "ok", "articles": articles, "count": len(articles)}


@router.get("/search")
async def api_search_wiki(
    request: Request,
    q: str = Query("", description="Search query"),
    limit: int = Query(10, ge=1, le=50),
):
    """Hybrid semantic + keyword search. Returns ranked results with snippets."""
    results = await search_articles(q, limit=limit, include_drafts=_can_see_drafts(request))
    return {"status": "ok", "results": results, "count": len(results), "query": q}


@router.get("/revisions/{rev_id}")
async def api_get_wiki_revision(rev_id: str, request: Request):
    """Return one full historical revision (for viewing/diffing). Members only."""
    _require_member(request)
    rev = await get_revision(rev_id)
    if rev is None:
        raise HTTPException(status_code=404, detail=f"Revision '{rev_id}' not found.")
    return {"status": "ok", "revision": rev}


@router.get("/{slug}/revisions")
async def api_list_wiki_revisions(slug: str, request: Request):
    """List prior versions of an article, newest first. Members only."""
    _require_member(request)
    revisions = await list_revisions(slug)
    return {"status": "ok", "revisions": revisions, "count": len(revisions)}


@router.get("/{slug}/backlinks")
async def api_wiki_backlinks(slug: str, request: Request):
    """List articles that link to this one via [[slug]] or [[Title]]."""
    links = await get_backlinks(slug, include_drafts=_can_see_drafts(request))
    return {"status": "ok", "backlinks": links, "count": len(links)}


@router.post("/{slug}/publish")
async def api_publish_wiki(slug: str, request: Request, body: StatusRequest = StatusRequest()):
    """Make an article public (visible to anonymous visitors). Members only."""
    actor = _require_member(request)
    article = await set_article_status(slug, "published", user_id=actor)
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "article": article}


@router.post("/{slug}/unpublish")
async def api_unpublish_wiki(slug: str, request: Request, body: StatusRequest = StatusRequest()):
    """Make an article internal again (draft — members only). Members only."""
    actor = _require_member(request)
    article = await set_article_status(slug, "draft", user_id=actor)
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "article": article}


@router.post("/{slug}/restore/{rev_id}")
async def api_restore_wiki_revision(slug: str, rev_id: str, request: Request, body: RestoreRequest = RestoreRequest()):
    """Restore an article to a past revision (the current version is snapshotted
    into history first, so a restore is itself reversible). Members only."""
    actor = _require_member(request)
    article = await restore_revision(slug, rev_id, user_id=actor)
    if article is None:
        raise HTTPException(status_code=404, detail=f"Revision '{rev_id}' not found.")
    return {"status": "ok", "article": article}


@router.get("/{slug}")
async def api_get_wiki(slug: str, request: Request):
    """Return one full article by slug (draft articles are hidden from public)."""
    article = await get_article(slug, include_drafts=_can_see_drafts(request))
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "article": article}


@router.post("")
async def api_create_wiki(request: Request, body: CreateArticleRequest):
    """Create a new article (ADMINS only — non-admin members get 403). The slug
    is derived from the title; new articles default to draft (internal) unless
    status='published' is given."""
    actor = await _require_admin(request)
    try:
        article = await create_article(
            title=body.title,
            body=body.body or "",
            tags=body.tags,
            category=body.category or "",
            user_id=actor or (body.user_id or ""),
            status=body.status or "draft",
        )
        return {"status": "ok", "article": article}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@router.patch("/{slug}")
async def api_update_wiki(slug: str, request: Request, body: UpdateArticleRequest):
    """Update an existing article's fields, incl. draft/published status (members
    only). The slug (URL) is preserved."""
    actor = _require_member(request)
    try:
        article = await update_article(
            slug=slug,
            title=body.title,
            body=body.body,
            tags=body.tags,
            category=body.category,
            user_id=actor or (body.user_id or ""),
            status=body.status,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if article is None:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "article": article}


@router.delete("/{slug}")
async def api_delete_wiki(slug: str, request: Request):
    """Delete an article (members only)."""
    _require_member(request)
    ok = await delete_article(slug)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Article '{slug}' not found.")
    return {"status": "ok", "message": f"Article '{slug}' deleted."}

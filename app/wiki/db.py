"""
Dedicated, **always-local** SQLite store for the company-wide Wiki.

Deliberately separate from the main application database (``get_db()`` — SQLite
``local.db`` or a remote Postgres). The Wiki is a single shared knowledge base
that lives in its **own file**, ``data/wiki.db``, with its own WAL and its own
write lock — so it can be backed up, copied, or wiped independently of chat /
agent / memory data, and it stays a portable SQLite file even when the main
backend is a remote Postgres.

Schema (mirrors the memories + memory_chunks + FTS pattern):
  • ``wiki_articles``      — one row per article (title, body, tags, category,
                             created_by / updated_by, timestamps). slug is unique.
  • ``wiki_chunks``        — the body chunked + embedded (float32 BLOB) for
                             semantic search.
  • ``wiki_articles_fts``  — FTS5 keyword index, kept in sync by triggers.

Search is hybrid: FTS5 keyword + embedding cosine similarity, merged with
Reciprocal-Rank Fusion. Embeddings use the shared ``embed_text`` helper; when the
embedding API is unavailable, writes still succeed (no vector) and search
degrades to keyword-only.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import struct
import uuid
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.agent.embed import embed_text, embed_dim
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

# Runtime DB file. The user-requested location is the project-root data/ dir.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
WIKI_DB_PATH = str(data_root() / "wiki.db")

_FTS5_QUERY_OPS = frozenset({"AND", "OR", "NOT", "NEAR"})

_np = None


def _ensure_np():
    global _np
    if _np is None:
        import numpy as np  # lazy: numpy import is heavy
        _np = np
    return _np


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _uuid() -> str:
    return str(uuid.uuid4())


def _fts5_safe_match_query(raw: str, max_tokens: int = 12, max_token_len: int = 64) -> Optional[str]:
    """Turn free text into a conservative prefix OR-query safe for FTS5 MATCH."""
    if not raw or not raw.strip():
        return None
    tokens = re.findall(r"\w+", raw, flags=re.UNICODE)
    parts: List[str] = []
    for t in tokens:
        if len(t) < 2 or t.upper() in _FTS5_QUERY_OPS:
            continue
        if len(t) > max_token_len:
            t = t[:max_token_len]
        parts.append(f"{t}*")
        if len(parts) >= max_tokens:
            break
    if not parts:
        return None
    return " OR ".join(parts)


_SCHEMA = """
CREATE TABLE IF NOT EXISTS wiki_articles (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',   -- JSON array of strings
    category    TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT 'draft',  -- 'draft' = internal (members only) | 'published' = public
    created_by  TEXT NOT NULL DEFAULT '',      -- user_id (attribution only)
    updated_by  TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(slug)
);

CREATE INDEX IF NOT EXISTS idx_wiki_articles_updated ON wiki_articles(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_wiki_articles_category ON wiki_articles(category);
-- NOTE: the index on `status` is created in _migrate(), AFTER the column is
-- ensured — older wiki.db files predate the column, so indexing it here (before
-- the ALTER) would fail with "no such column: status".

CREATE VIRTUAL TABLE IF NOT EXISTS wiki_articles_fts USING fts5(
    slug UNINDEXED,
    title,
    body,
    tags,
    content='wiki_articles',
    content_rowid='rowid',
    tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS trg_wiki_articles_fts_insert
    AFTER INSERT ON wiki_articles BEGIN
    INSERT INTO wiki_articles_fts(rowid, slug, title, body, tags)
    VALUES (new.rowid, new.slug, new.title, new.body, new.tags);
END;

CREATE TRIGGER IF NOT EXISTS trg_wiki_articles_fts_delete
    AFTER DELETE ON wiki_articles BEGIN
    INSERT INTO wiki_articles_fts(wiki_articles_fts, rowid, slug, title, body, tags)
    VALUES ('delete', old.rowid, old.slug, old.title, old.body, old.tags);
END;

CREATE TRIGGER IF NOT EXISTS trg_wiki_articles_fts_update
    AFTER UPDATE ON wiki_articles BEGIN
    INSERT INTO wiki_articles_fts(wiki_articles_fts, rowid, slug, title, body, tags)
    VALUES ('delete', old.rowid, old.slug, old.title, old.body, old.tags);
    INSERT INTO wiki_articles_fts(rowid, slug, title, body, tags)
    VALUES (new.rowid, new.slug, new.title, new.body, new.tags);
END;

CREATE TABLE IF NOT EXISTS wiki_chunks (
    id            TEXT PRIMARY KEY,
    article_id    TEXT NOT NULL REFERENCES wiki_articles(id) ON DELETE CASCADE,
    chunk_index   INTEGER NOT NULL,
    chunk_text    TEXT NOT NULL,
    embedding     BLOB,       -- numpy float32 array for local vector search
    token_count   INTEGER,
    created_at    TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE(article_id, chunk_index)
);

CREATE INDEX IF NOT EXISTS idx_wiki_chunks_article ON wiki_chunks(article_id);

-- Point-in-time snapshots of an article. One row is written for the PRIOR
-- version every time an article is updated (and one for the initial create), so
-- the full edit history is preserved and any version can be restored.
CREATE TABLE IF NOT EXISTS wiki_revisions (
    id          TEXT PRIMARY KEY,
    article_id  TEXT NOT NULL,         -- not a FK: history outlives deletes
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    category    TEXT NOT NULL DEFAULT '',
    edited_by   TEXT NOT NULL DEFAULT '',
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_wiki_revisions_article ON wiki_revisions(article_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_wiki_revisions_slug ON wiki_revisions(slug);
"""


class WikiStore:
    """Manages the dedicated local ``data/wiki.db`` SQLite file (own WAL + lock)."""

    def __init__(self, path: Optional[str] = None) -> None:
        self._path = path or WIKI_DB_PATH
        self._write_lock = asyncio.Lock()
        self._init_db()

    # ── Connection ───────────────────────────────────────────────────────────
    # journal_mode=DELETE (rollback journal), NOT WAL: this DB file is tracked in
    # the repo, so it must stay a single self-contained file. WAL keeps recent
    # writes in a -wal sidecar until checkpoint, which would mean the committed
    # .db could miss data and leave transient -wal/-shm files lying around. With
    # DELETE mode the only sidecar is a short-lived -journal that exists solely
    # during a write transaction; between writes there is just wiki.db.
    def _connect(self):
        # Routed through db_crypto so wiki.db honours the full-DB-encryption config
        # (off by default — this file is git-tracked and serves public pages). With
        # encryption OFF this is plain stdlib sqlite3. Row factory set by db_crypto.
        from app.db import db_crypto
        db_id = "wiki" if self._path == WIKI_DB_PATH else "_wiki_custom"
        conn = db_crypto.connect(self._path, db_id)
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_db(self) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
        except Exception:
            pass
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
            self._migrate(conn)
            conn.commit()
        finally:
            conn.close()

    def _migrate(self, conn) -> None:
        """Add columns that older wiki.db files predate. Safe to run every boot."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(wiki_articles)").fetchall()}
        if "status" not in cols:
            # Backfill pre-existing articles as 'published' so they stay visible
            # exactly as before this feature; new articles default to 'draft'.
            conn.execute(
                "ALTER TABLE wiki_articles ADD COLUMN status TEXT NOT NULL DEFAULT 'published'"
            )
        # Index on status — created here (not in _SCHEMA) so it runs after the
        # column is guaranteed to exist on older databases.
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_wiki_articles_status ON wiki_articles(status)"
        )

    # ── Row helpers ──────────────────────────────────────────────────────────
    @staticmethod
    def _row(r) -> dict:
        d = dict(r)
        try:
            d["tags"] = json.loads(d.get("tags") or "[]")
        except (json.JSONDecodeError, TypeError):
            d["tags"] = []
        return d

    @staticmethod
    def _chunk_text(text: str, max_chars: int = 500) -> List[str]:
        """Split text into ~max_chars chunks, breaking at sentence boundaries."""
        if len(text) <= max_chars:
            return [text]
        chunks: List[str] = []
        pos = 0
        while pos < len(text):
            end = min(pos + max_chars, len(text))
            if end < len(text):
                best_break = max(
                    text.rfind(". ", pos, end),
                    text.rfind(".\n", pos, end),
                    text.rfind("\n", pos, end),
                    text.rfind(" ", pos + max_chars // 2, end),
                )
                if best_break > pos + max_chars // 2:
                    end = best_break + 1
            chunk = text[pos:end].strip()
            if chunk:
                chunks.append(chunk)
            pos = end
        return chunks

    # ── Reads ────────────────────────────────────────────────────────────────
    # ``include_drafts`` is the visibility gate: True for signed-in members (and
    # agents acting for them) — they see drafts + published; False for anonymous
    # / public callers — they see only published articles.
    async def list(self, include_drafts: bool = True) -> List[dict]:
        where = "" if include_drafts else "WHERE status = 'published'"
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, slug, title, tags, category, status, created_by, updated_by, "
                "created_at, updated_at, substr(body, 1, 240) AS snippet "
                f"FROM wiki_articles {where} ORDER BY updated_at DESC",
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    async def get(self, slug: str, include_drafts: bool = True) -> Optional[dict]:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM wiki_articles WHERE slug = ?", (slug,),
            ).fetchone()
            if not row:
                return None
            # Hide a draft's very existence from public callers.
            if not include_drafts and (row["status"] or "draft") != "published":
                return None
            return self._row(row)
        finally:
            conn.close()

    # ── Writes ───────────────────────────────────────────────────────────────
    async def upsert(
        self,
        slug: str,
        title: str,
        body: str = "",
        tags: Optional[list] = None,
        category: str = "",
        user_id: str = "",
        status: Optional[str] = None,
    ) -> dict:
        """Create or update an article (keyed by slug), then re-chunk + re-embed
        its body. The FTS row is kept in sync by triggers.

        ``status`` ('draft' | 'published'): on create, defaults to 'draft'
        (internal). On update, ``None`` keeps the current status unchanged."""
        tags_json = json.dumps(tags or [])
        async with self._write_lock:
            conn = self._connect()
            try:
                now = _now_iso()
                existing = conn.execute(
                    "SELECT * FROM wiki_articles WHERE slug = ?", (slug,),
                ).fetchone()
                if existing:
                    article_id = existing["id"]
                    new_status = status if status is not None else (existing["status"] or "draft")
                    # Snapshot the PRIOR version into history before overwriting.
                    conn.execute(
                        "INSERT INTO wiki_revisions (id, article_id, slug, title, "
                        "body, tags, category, edited_by, created_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (_uuid(), article_id, existing["slug"], existing["title"],
                         existing["body"], existing["tags"], existing["category"],
                         existing["updated_by"] or existing["created_by"],
                         existing["updated_at"]),
                    )
                    conn.execute(
                        "UPDATE wiki_articles SET title = ?, body = ?, tags = ?, "
                        "category = ?, status = ?, updated_by = ?, updated_at = ? WHERE id = ?",
                        (title, body, tags_json, category, new_status, user_id, now, article_id),
                    )
                else:
                    article_id = _uuid()
                    new_status = status if status is not None else "draft"
                    conn.execute(
                        "INSERT INTO wiki_articles (id, slug, title, body, tags, "
                        "category, status, created_by, updated_by, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (article_id, slug, title, body, tags_json, category,
                         new_status, user_id, user_id, now, now),
                    )
                conn.commit()
                await self._embed_and_store_chunks(conn, article_id, body)
                conn.commit()
                return self._row(conn.execute(
                    "SELECT * FROM wiki_articles WHERE id = ?", (article_id,),
                ).fetchone())
            finally:
                conn.close()

    async def set_status(self, slug: str, status: str, user_id: str = "") -> Optional[dict]:
        """Flip an article between 'draft' and 'published' without touching its
        body/history (no revision snapshot, no re-embed). Returns the updated
        article, or None if the slug doesn't exist."""
        status = "published" if status == "published" else "draft"
        async with self._write_lock:
            conn = self._connect()
            try:
                cur = conn.execute(
                    "UPDATE wiki_articles SET status = ?, updated_by = ?, updated_at = ? "
                    "WHERE slug = ?",
                    (status, user_id, _now_iso(), slug),
                )
                conn.commit()
                if cur.rowcount == 0:
                    return None
                return self._row(conn.execute(
                    "SELECT * FROM wiki_articles WHERE slug = ?", (slug,),
                ).fetchone())
            finally:
                conn.close()

    async def delete(self, slug: str) -> bool:
        async with self._write_lock:
            conn = self._connect()
            try:
                # Resolve the article id first so we can also purge its child rows.
                # Deleting only wiki_articles left orphaned wiki_revisions (and
                # wiki_chunks) behind — which both wastes space and let the agent
                # wrongly tell the user "all revision history is gone" while the
                # rows persisted. Purge children in the same transaction.
                idrow = conn.execute(
                    "SELECT id FROM wiki_articles WHERE slug = ?", (slug,)
                ).fetchone()
                article_id = idrow[0] if idrow else None
                cur = conn.execute("DELETE FROM wiki_articles WHERE slug = ?", (slug,))
                if article_id is not None:
                    for _tbl in ("wiki_revisions", "wiki_chunks"):
                        try:
                            conn.execute(f"DELETE FROM {_tbl} WHERE article_id = ?", (article_id,))
                        except Exception:
                            pass  # table may not exist in older schemas
                conn.commit()
                return cur.rowcount > 0
            finally:
                conn.close()

    async def _embed_and_store_chunks(self, conn, article_id: str, text: str) -> None:
        conn.execute("DELETE FROM wiki_chunks WHERE article_id = ?", (article_id,))
        if not text or not text.strip():
            return
        for i, chunk in enumerate(self._chunk_text(text, max_chars=500)):
            if not chunk.strip():
                continue
            embedding_blob = None
            try:
                emb_list = await embed_text(chunk)
                embedding_blob = struct.pack(f"{len(emb_list)}f", *emb_list)
            except Exception as e:
                logger.warning("Wiki chunk embed failed (idx=%d art=%s): %s", i, article_id, e)
            conn.execute(
                "INSERT OR REPLACE INTO wiki_chunks "
                "(id, article_id, chunk_index, chunk_text, embedding, token_count) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (_uuid(), article_id, i, chunk, embedding_blob, len(chunk.split())),
            )

    # ── Search (hybrid FTS + vector, RRF-merged) ─────────────────────────────
    async def search(self, query: str, limit: int = 10, include_drafts: bool = True) -> List[dict]:
        query = (query or "").strip()
        if not query:
            return []
        fts_task = asyncio.create_task(self._fts5_search(query, limit * 3, include_drafts))
        vec_task = asyncio.create_task(self._vector_search(query, limit * 3, include_drafts))
        fts_results, vec_results = await asyncio.gather(
            fts_task, vec_task, return_exceptions=True
        )
        if isinstance(fts_results, BaseException):
            logger.warning("Wiki FTS5 search failed: %s", fts_results)
            fts_results = []
        if isinstance(vec_results, BaseException):
            logger.warning("Wiki vector search failed: %s", vec_results)
            vec_results = []

        if not vec_results:
            return fts_results[:limit] if fts_results else []
        if not fts_results:
            return vec_results[:limit]

        k = 60
        rrf: Dict[str, float] = {}
        for rank, a in enumerate(fts_results, start=1):
            rrf[a.get("slug", "")] = rrf.get(a.get("slug", ""), 0.0) + 1.0 / (k + rank)
        for rank, a in enumerate(vec_results, start=1):
            rrf[a.get("slug", "")] = rrf.get(a.get("slug", ""), 0.0) + 1.0 / (k + rank)

        all_arts: Dict[str, dict] = {}
        for a in fts_results + vec_results:
            s = a.get("slug", "")
            if s and s not in all_arts:
                all_arts[s] = a

        merged = []
        for slug, score in sorted(rrf.items(), key=lambda x: -x[1]):
            if slug in all_arts:
                entry = dict(all_arts[slug])
                entry["rank"] = round(score, 4)
                merged.append(entry)
        return merged[:limit]

    async def _fts5_search(self, query: str, limit: int = 10, include_drafts: bool = True) -> List[dict]:
        match_expr = _fts5_safe_match_query(query)
        if not match_expr:
            return []
        status_clause = "" if include_drafts else "AND w.status = 'published' "
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT w.id, w.slug, w.title, w.tags, w.category, w.status, w.updated_at, "
                "substr(w.body, 1, 240) AS snippet, rank "
                "FROM wiki_articles_fts fts "
                "JOIN wiki_articles w ON w.rowid = fts.rowid "
                f"WHERE wiki_articles_fts MATCH ? {status_clause}ORDER BY rank LIMIT ?",
                (match_expr, limit),
            ).fetchall()
            return [self._row(r) for r in rows]
        finally:
            conn.close()

    async def _vector_search(self, query_text: str, limit: int = 10, include_drafts: bool = True) -> List[dict]:
        np = _ensure_np()
        status_clause = "" if include_drafts else "AND w.status = 'published' "
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT wc.article_id, wc.embedding, w.slug, w.title, w.tags, "
                "w.category, w.status, w.updated_at, substr(w.body, 1, 240) AS snippet "
                "FROM wiki_chunks wc JOIN wiki_articles w ON w.id = wc.article_id "
                f"WHERE wc.embedding IS NOT NULL {status_clause}",
            ).fetchall()
            if not rows:
                return []
        finally:
            conn.close()

        try:
            query_vec = np.array(await embed_text(query_text), dtype=np.float32)
        except Exception as e:
            logger.warning("Wiki query embed failed, skipping vector search: %s", e)
            return []

        article_ids, vecs = [], []
        for r in rows:
            if r["embedding"]:
                vec = np.frombuffer(r["embedding"], dtype=np.float32)
                if vec.shape[0] == embed_dim():
                    article_ids.append(r["article_id"])
                    vecs.append(vec)
        if not vecs:
            return []

        matrix = np.stack(vecs)
        norms = np.linalg.norm(matrix, axis=1)
        q_norm = np.linalg.norm(query_vec)
        scores = np.dot(matrix, query_vec) / (norms * q_norm + 1e-12)

        art_best: Dict[str, float] = {}
        for i, aid in enumerate(article_ids):
            s = float(scores[i])
            if aid not in art_best or s > art_best[aid]:
                art_best[aid] = s

        art_rows: Dict[str, dict] = {}
        for r in rows:
            aid = r["article_id"]
            if aid not in art_rows:
                art_rows[aid] = self._row(r)

        result = []
        for aid, score in sorted(art_best.items(), key=lambda x: -x[1]):
            if aid in art_rows:
                entry = dict(art_rows[aid])
                entry["rank"] = round(float(score), 4)
                entry.pop("embedding", None)
                entry.pop("article_id", None)
                result.append(entry)
                if len(result) >= limit:
                    break
        return result

    # ── Revision history ─────────────────────────────────────────────────────
    async def list_revisions(self, slug: str) -> List[dict]:
        """Prior versions of an article, newest first (metadata + snippet)."""
        conn = self._connect()
        try:
            art = conn.execute(
                "SELECT id FROM wiki_articles WHERE slug = ?", (slug,),
            ).fetchone()
            if not art:
                return []
            rows = conn.execute(
                "SELECT id, slug, title, category, edited_by, created_at, "
                "substr(body, 1, 240) AS snippet "
                "FROM wiki_revisions WHERE article_id = ? ORDER BY created_at DESC",
                (art["id"],),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    async def get_revision(self, rev_id: str) -> Optional[dict]:
        """One full historical revision by its id."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM wiki_revisions WHERE id = ?", (rev_id,),
            ).fetchone()
            return self._row(row) if row else None
        finally:
            conn.close()

    async def restore_revision(self, slug: str, rev_id: str, user_id: str = "") -> Optional[dict]:
        """Restore an article to a past revision. The current version is first
        snapshotted into history (via the normal upsert path), so a restore is
        itself reversible. Returns the restored article, or None if not found."""
        rev = await self.get_revision(rev_id)
        if rev is None:
            return None
        # The revision belongs to the current article (slug is immutable).
        target = rev.get("slug") or slug
        return await self.upsert(
            slug=target,
            title=rev["title"],
            body=rev.get("body", ""),
            tags=rev.get("tags", []),
            category=rev.get("category", ""),
            user_id=user_id,
        )

    # ── Backlinks ────────────────────────────────────────────────────────────
    async def backlinks(self, slug: str, include_drafts: bool = True) -> List[dict]:
        """Articles whose body links to this one via [[slug]] or [[Title]].
        Public callers only see published articles among the backlinks."""
        status_clause = "" if include_drafts else "AND status = 'published' "
        conn = self._connect()
        try:
            art = conn.execute(
                "SELECT title FROM wiki_articles WHERE slug = ?", (slug,),
            ).fetchone()
            if not art:
                return []
            title = art["title"]
            rows = conn.execute(
                "SELECT slug, title FROM wiki_articles "
                "WHERE slug != ? "
                f"{status_clause}AND ("
                "  instr(lower(body), lower(?)) > 0 OR instr(lower(body), lower(?)) > 0"
                ") ORDER BY title COLLATE NOCASE",
                (slug, f"[[{slug}]]", f"[[{title}]]"),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    def path(self) -> str:
        return self._path


# ── Module singleton ──────────────────────────────────────────────────────────
_store: Optional[WikiStore] = None


def get_wiki_store() -> WikiStore:
    global _store
    if _store is None:
        _store = WikiStore()
    return _store

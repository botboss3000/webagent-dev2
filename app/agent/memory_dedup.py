"""
Near-duplicate memory detection.

When an agent saves a *fact* to memory (the ``upsert`` action of the memory
tool), it may be re-stating something it — or a past session — already saved
under a different slug ("The user runs a bakery" vs "User owns a bakery
business"). Left unchecked, the memory store fills with many near-identical rows
for the same fact, which bloats recall and dilutes ranking.

This module answers one question, cheaply and backend-agnostically: *does a
semantically near-identical memory already exist under a DIFFERENT slug?* If so,
the caller updates THAT row in place instead of inserting a restatement — one row
per fact, newest wording wins (the behaviour an admin picked).

Design notes:
- **Backend-agnostic.** It uses only the public ``db.memory_search`` (already
  implemented for SQLite / Postgres / hybrid) plus the in-process
  embedder. No per-backend vector SQL here.
- **Absolute similarity, not rank.** ``memory_search`` returns an RRF *rank*,
  which is a fusion score, not a cosine distance — useless as a duplicate
  threshold. So we take its top few candidates, then re-embed the incoming text
  and each candidate locally and compute a real cosine. Embeddings are in-process
  by default (no network, no API cost), so re-embedding a handful of short texts
  is effectively free.
- **Degrades to a normal insert.** If the local embedder isn't installed, or
  search/embed fails, this returns ``None`` and the caller just saves normally —
  dedup is an optimisation, never a gate on saving.
- **Conservative threshold.** The bar is deliberately HIGH (near-identical), so a
  genuinely different fact about the same subject is NOT merged away. Tunable via
  ``MEMORY_DEDUP_THRESHOLD``.
"""

from __future__ import annotations

import math
import os
import logging
from typing import Optional

logger = logging.getLogger("webagent.memory_dedup")

# Cosine-similarity bar for treating two memories as the same fact. bge-small
# scores near-identical restatements ~0.90–0.97 and merely related facts lower,
# so 0.90 catches restatements without merging distinct facts. Override with
# MEMORY_DEDUP_THRESHOLD (e.g. lower to 0.86 for more aggressive merging).
_DEDUP_THRESHOLD = float(os.environ.get("MEMORY_DEDUP_THRESHOLD") or 0.90)

# Below this many characters a "fact" is too terse to judge duplication safely
# (short strings collide easily), so we skip the check and just save.
_MIN_LEN = 40

# How many top candidates from keyword+vector search to re-score with a real
# cosine. The true duplicate, if any, is almost always in the top couple.
_TOP_K = 3


def _cosine(a, b) -> float:
    """Plain cosine similarity of two equal-length float lists. Pure Python — the
    vectors are short (384) and we compare only a few, so numpy isn't worth it."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    return dot / (math.sqrt(na) * math.sqrt(nb) + 1e-12)


async def find_duplicate_memory(
    db,
    user_id: str,
    slug: str,
    text: str,
    *,
    threshold: Optional[float] = None,
    top_k: int = _TOP_K,
) -> Optional[dict]:
    """Return an existing near-duplicate memory (different slug) or ``None``.

    ``text`` is the incoming memory's ``compiled_truth``. On a hit, returns
    ``{"slug", "title", "similarity"}`` for the existing row the caller should
    update instead of inserting a new one. Returns ``None`` (save normally) when
    the text is too short, nothing similar exists, or the embedder is unavailable.
    """
    text = (text or "").strip()
    if not user_id or len(text) < _MIN_LEN:
        return None
    thr = threshold if threshold is not None else _DEDUP_THRESHOLD

    # 1) Cheap candidate shortlist via the existing hybrid search (keyword+vector).
    try:
        cands = await db.memory_search(user_id, text, limit=top_k, vector=True)
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("dedup: candidate search failed, saving normally: %s", e)
        return None
    cands = [
        c for c in (cands or [])
        if c.get("slug") and c.get("slug") != slug
    ]
    if not cands:
        return None

    # 2) Real cosine on locally-embedded texts (free with the in-process engine).
    try:
        from app.agent.embed import embed_text
        qv = await embed_text(text)
    except Exception as e:
        # No local embedder (or it failed) → don't block the save, just skip dedup.
        logger.debug("dedup: incoming embed failed, saving normally: %s", e)
        return None

    best: Optional[dict] = None
    for c in cands:
        ctext = (c.get("compiled_truth") or c.get("title") or "").strip()
        if not ctext:
            continue
        try:
            cv = await embed_text(ctext)
        except Exception:
            continue
        sim = _cosine(qv, cv)
        if best is None or sim > best["similarity"]:
            best = {
                "slug": c["slug"],
                "title": c.get("title") or c["slug"],
                "similarity": sim,
            }

    if best and best["similarity"] >= thr:
        logger.debug(
            "dedup: '%s' is a near-duplicate of '%s' (%.3f ≥ %.2f) → update in place",
            slug, best["slug"], best["similarity"], thr,
        )
        return best
    return None

"""
Backfill embeddings for existing memory pages.

Iterates all memory pages, chunks their compiled_truth + timeline,
embeds via OpenRouter text-embedding-3-small, stores in memory_chunks.

Run:  python scripts/backfill_embeddings.py [--dry-run] [--user-id USER_ID]
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import struct
import sys
import time
from pathlib import Path
from typing import List

# Add project root to path so we can import app modules
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Must set OPENROUTER_API_KEY before importing app modules
from app.agent.embed import embed_text, EMBED_DIM
from app.db.local import DEFAULT_DB_PATH

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("backfill")


def chunk_text(text: str, max_chars: int = 500) -> List[str]:
    """Split text into ~max_chars chunks at sentence boundaries."""
    if len(text) <= max_chars:
        return [text]
    chunks = []
    pos = 0
    while pos < len(text):
        end = min(pos + max_chars, len(text))
        if end < len(text):
            best = max(
                text.rfind(". ", pos, end),
                text.rfind(".\n", pos, end),
                text.rfind("\n", pos, end),
                text.rfind(" ", pos + max_chars // 2, end),
            )
            if best > pos + max_chars // 2:
                end = best + 1
        chunk = text[pos:end].strip()
        if chunk:
            chunks.append(chunk)
        pos = end
    return chunks


async def backfill_page(
    db_path: str, memory_id: str, slug: str, compiled_truth: str, timeline: str,
    user_id: str, dry_run: bool,
) -> int:
    """Embed and store chunks for one memory page. Returns chunk count."""
    chunks_data = []
    if compiled_truth and compiled_truth.strip():
        for chunk in chunk_text(compiled_truth, max_chars=500):
            if chunk.strip():
                chunks_data.append((chunk, "compiled_truth"))
    if timeline and timeline.strip():
        for chunk in chunk_text(timeline, max_chars=500):
            if chunk.strip():
                chunks_data.append((chunk, "timeline"))

    if not chunks_data:
        return 0

    if dry_run:
        logger.info("  [dry-run] %s: %d chunks would be embedded", slug, len(chunks_data))
        return len(chunks_data)

    conn = sqlite3.connect(db_path)
    try:
        # Delete old chunks for this memory_id
        conn.execute("DELETE FROM memory_chunks WHERE memory_id = ?", (memory_id,))
        stored = 0
        for i, (text, source) in enumerate(chunks_data):
            chunk_id = f"bf-{memory_id[:8]}-{i:04d}"
            embedding_blob = None
            try:
                emb = await embed_text(text)
                embedding_blob = struct.pack(f"{len(emb)}f", *emb)
            except Exception as e:
                logger.warning("  Embed failed for %s chunk %d: %s", slug, i, e)
            conn.execute(
                """INSERT OR REPLACE INTO memory_chunks
                   (id, memory_id, chunk_index, chunk_text, chunk_source, embedding, token_count)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (chunk_id, memory_id, i, text, source, embedding_blob, len(text.split())),
            )
            stored += 1
        conn.commit()
        return stored
    finally:
        conn.close()


async def main():
    parser = argparse.ArgumentParser(description="Backfill memory page embeddings")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--user-id", help="Only backfill pages for this user")
    parser.add_argument("--db-path", default=DEFAULT_DB_PATH, help="SQLite database path")
    args = parser.parse_args()

    db_path = args.db_path
    if not os.path.exists(db_path):
        logger.error("Database not found: %s", db_path)
        sys.exit(1)

    if not os.environ.get("OPENROUTER_API_KEY"):
        logger.error("OPENROUTER_API_KEY not set — embed API calls will fail")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if args.user_id:
            rows = conn.execute(
                "SELECT * FROM memories WHERE user_id = ? ORDER BY updated_at DESC",
                (args.user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM memories ORDER BY updated_at DESC",
            ).fetchall()
    finally:
        conn.close()

    total_pages = len(rows)
    total_chunks = 0
    failed = 0
    skipped = 0

    logger.info("Backfilling %d pages (dry_run=%s)…", total_pages, args.dry_run)

    for idx, row in enumerate(rows, 1):
        memory_id = row["id"]
        slug = row["slug"]
        ct = row["compiled_truth"] or ""
        tl = row["timeline"] or ""
        uid = row["user_id"]

        if not ct.strip() and not tl.strip():
            skipped += 1
            continue

        logger.info("[%d/%d] %s (%s)", idx, total_pages, slug, uid)
        try:
            n = await backfill_page(db_path, memory_id, slug, ct, tl, uid, args.dry_run)
            if n > 0:
                total_chunks += n
                logger.info("  → %d chunks stored", n)
            # Rate-limit: small delay between pages to avoid API throttle
            await asyncio.sleep(0.1)
        except Exception as e:
            logger.error("  FAILED: %s", e)
            failed += 1

    logger.info(
        "Done. %d pages, %d chunks, %d failed, %d skipped (dry_run=%s)",
        total_pages, total_chunks, failed, skipped, args.dry_run,
    )


if __name__ == "__main__":
    asyncio.run(main())

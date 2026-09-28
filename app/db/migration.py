"""
Data migration: export current backend to JSON, import JSON into current backend.

Export iterates all canonical tables via the active backend's raw client and
streams a single JSON document of the form:

{
  "version": 1,
  "exported_at": "<iso8601>",
  "source_backend": "LocalBackend",
  "tables": {
      "sessions":     [ {...row...}, ... ],
      "interactions": [ ... ],
      ...
  }
}

Import reverses the operation: validates the payload, then INSERTs (best-effort
upsert) into each table on the active backend.

Both directions go through `db.get_raw_client().table(name)` which both
LocalBackend implements with the same query-builder shape.
"""

import json
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Iterator

from app.db.schema.tables import TABLE_ORDER

logger = logging.getLogger(__name__)


# ── Export ──────────────────────────────────────────────────────────────────


def iter_export_json() -> Iterator[bytes]:
    """
    Synchronous generator (FastAPI StreamingResponse accepts sync or async).
    Yields JSON byte chunks of the export document.

    The raw client's table().select().execute() is sync in both backends.
    """
    from app.db import get_db
    db = get_db()
    raw = db.get_raw_client()

    now = datetime.now(timezone.utc).isoformat()
    header = (
        '{"version": 1, "exported_at": '
        + json.dumps(now)
        + ', "source_backend": '
        + json.dumps(type(db).__name__)
        + ', "tables": {'
    )
    yield header.encode("utf-8")

    first = True
    for tbl in TABLE_ORDER:
        try:
            res = raw.table(tbl).select("*").execute()
            rows = res.data or []
        except Exception as e:
            logger.warning("Export skip table %s: %s", tbl, e)
            rows = []

        prefix = ("" if first else ",") + json.dumps(tbl) + ":"
        first = False
        yield prefix.encode("utf-8")

        # Stream rows as a JSON array; chunked per row to avoid building
        # the whole array in memory for big tables.
        yield b"["
        for i, row in enumerate(rows):
            if i:
                yield b","
            yield json.dumps(row, default=str).encode("utf-8")
        yield b"]"

    yield b"}}"


# ── Import ──────────────────────────────────────────────────────────────────


async def import_payload(payload: dict) -> dict:
    """
    Insert rows from a previously-exported document into the current backend.

    Strategy:
      - Iterate TABLE_ORDER (parents-before-children).
      - For each table, INSERT rows in batches of 500. On conflict, skip
        (best-effort: assumes idempotent re-import).
    """
    if not isinstance(payload, dict) or "tables" not in payload:
        return {"ok": False, "error": "Payload missing 'tables' key"}

    from app.db import get_db
    db = get_db()
    raw = db.get_raw_client()

    inserted: dict = {}
    errors: list = []
    tables = payload["tables"] or {}

    for tbl in TABLE_ORDER:
        rows = tables.get(tbl) or []
        if not rows:
            inserted[tbl] = 0
            continue
        ok = 0
        # Insert in batches of 500
        for chunk_start in range(0, len(rows), 500):
            chunk = rows[chunk_start:chunk_start + 500]
            try:
                raw.table(tbl).insert(chunk).execute()
                ok += len(chunk)
            except Exception as e:
                # Try one-by-one on batch failure (duplicates / FK)
                for row in chunk:
                    try:
                        raw.table(tbl).insert(row).execute()
                        ok += 1
                    except Exception as e2:
                        errors.append({"table": tbl, "error": str(e2)[:200]})
                        if len(errors) > 50:
                            errors.append({"table": tbl, "error": "(further errors truncated)"})
                            break
        inserted[tbl] = ok

    return {
        "ok": True,
        "target_backend": type(db).__name__,
        "inserted": inserted,
        "errors": errors[:51],
    }

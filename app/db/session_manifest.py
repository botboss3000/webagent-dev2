"""Canonical transcript manifests for revision-validated browser caching."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Iterable


MANIFEST_SCHEMA_VERSION = 2
_FIELDS = (
    "id",
    "session_id",
    "parent_id",
    "role",
    "content",
    "tool_name",
    "tool_call_id",
    "channel",
    "metadata",
    "output",
    "source",
    "from_id",
    "to_id",
    "session_seq",
    "turn_id",
    "turn_seq",
    "status",
    "created_at",
)

_SQLITE_MANIFEST_SCHEMA = """
CREATE TABLE IF NOT EXISTS {manifest_table} (
    session_id TEXT PRIMARY KEY,
    authority_revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    max_session_seq INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TRIGGER IF NOT EXISTS {trigger_prefix}trg_session_manifest_interaction_insert
AFTER INSERT ON interactions
BEGIN
    INSERT INTO session_manifests
        (session_id,authority_revision,content_hash,interaction_count,
         max_session_seq,dirty,updated_at)
    VALUES
        (NEW.session_id,1,'',1,COALESCE(NEW.session_seq,0),1,datetime('now'))
    ON CONFLICT(session_id) DO UPDATE SET
        authority_revision=session_manifests.authority_revision+1,
        content_hash='',
        interaction_count=session_manifests.interaction_count+1,
        max_session_seq=MAX(
            session_manifests.max_session_seq, COALESCE(NEW.session_seq,0)
        ),
        dirty=1,
        updated_at=datetime('now');
END;
CREATE TRIGGER IF NOT EXISTS {trigger_prefix}trg_session_manifest_interaction_update
AFTER UPDATE ON interactions
WHEN OLD.session_seq IS NOT NEW.session_seq
  OR OLD.status IS NOT NEW.status
  OR OLD.turn_id IS NOT NEW.turn_id
  OR OLD.turn_seq IS NOT NEW.turn_seq
BEGIN
    INSERT INTO session_manifests
        (session_id,authority_revision,content_hash,interaction_count,
         max_session_seq,dirty,updated_at)
    VALUES
        (NEW.session_id,1,'',1,COALESCE(NEW.session_seq,0),1,datetime('now'))
    ON CONFLICT(session_id) DO UPDATE SET
        authority_revision=session_manifests.authority_revision+1,
        content_hash='',
        max_session_seq=MAX(
            session_manifests.max_session_seq, COALESCE(NEW.session_seq,0)
        ),
        dirty=1,
        updated_at=datetime('now');
END;
CREATE TRIGGER IF NOT EXISTS {trigger_prefix}trg_session_manifest_interaction_move_old
AFTER UPDATE OF session_id ON interactions
WHEN OLD.session_id IS NOT NEW.session_id
BEGIN
    INSERT INTO session_manifests
        (session_id,authority_revision,content_hash,interaction_count,
         max_session_seq,dirty,updated_at)
    VALUES
        (OLD.session_id,1,'',0,0,1,datetime('now'))
    ON CONFLICT(session_id) DO UPDATE SET
        authority_revision=session_manifests.authority_revision+1,
        content_hash='',
        dirty=1,
        updated_at=datetime('now');
END;
CREATE TRIGGER IF NOT EXISTS {trigger_prefix}trg_session_manifest_interaction_delete
AFTER DELETE ON interactions
BEGIN
    INSERT INTO session_manifests
        (session_id,authority_revision,content_hash,interaction_count,
         max_session_seq,dirty,updated_at)
    VALUES (OLD.session_id,1,'',0,0,1,datetime('now'))
    ON CONFLICT(session_id) DO UPDATE SET
        authority_revision=session_manifests.authority_revision+1,
        content_hash='',
        interaction_count=MAX(session_manifests.interaction_count-1,0),
        dirty=1,
        updated_at=datetime('now');
END;
"""

POSTGRES_MANIFEST_DDL = """
CREATE TABLE IF NOT EXISTS session_manifests (
    session_id TEXT PRIMARY KEY,
    authority_revision INTEGER NOT NULL DEFAULT 0,
    content_hash TEXT NOT NULL DEFAULT '',
    interaction_count INTEGER NOT NULL DEFAULT 0,
    max_session_seq INTEGER NOT NULL DEFAULT 0,
    dirty INTEGER NOT NULL DEFAULT 1,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE FUNCTION webagent_mark_session_manifest_dirty()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    target_session_id TEXT;
BEGIN
    target_session_id := CASE WHEN TG_OP = 'DELETE' THEN OLD.session_id ELSE NEW.session_id END;
    INSERT INTO session_manifests (
        session_id, authority_revision, content_hash, interaction_count,
        max_session_seq, dirty, updated_at
    )
    VALUES (target_session_id, 1, '', 0, 0, 1, CURRENT_TIMESTAMP)
    ON CONFLICT (session_id) DO UPDATE SET
        authority_revision = session_manifests.authority_revision + 1,
        content_hash = '',
        dirty = 1,
        updated_at = CURRENT_TIMESTAMP;

    IF TG_OP = 'UPDATE' AND OLD.session_id IS DISTINCT FROM NEW.session_id THEN
        INSERT INTO session_manifests (
            session_id, authority_revision, content_hash, interaction_count,
            max_session_seq, dirty, updated_at
        )
        VALUES (OLD.session_id, 1, '', 0, 0, 1, CURRENT_TIMESTAMP)
        ON CONFLICT (session_id) DO UPDATE SET
            authority_revision = session_manifests.authority_revision + 1,
            content_hash = '',
            dirty = 1,
            updated_at = CURRENT_TIMESTAMP;
    END IF;
    IF TG_OP = 'DELETE' THEN
        RETURN OLD;
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_session_manifest_interaction_change ON interactions;
CREATE TRIGGER trg_session_manifest_interaction_change
AFTER INSERT OR UPDATE OR DELETE ON interactions
FOR EACH ROW EXECUTE FUNCTION webagent_mark_session_manifest_dirty();
"""


def install_postgres_manifest_maintenance(connection: Any) -> None:
    """Install the provider-native dirty/revision trigger on a psycopg connection."""
    connection.execute(POSTGRES_MANIFEST_DDL)


def _value(row: Any, index: int, name: str) -> Any:
    try:
        return row[name]
    except (KeyError, TypeError, IndexError):
        return row[index]


def manifest_from_rows(rows: Iterable[Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    count = 0
    max_seq = 0
    for row in rows:
        canonical = [_value(row, index, name) for index, name in enumerate(_FIELDS)]
        try:
            max_seq = max(max_seq, int(canonical[13] or 0))
        except (TypeError, ValueError):
            pass
        digest.update(
            json.dumps(
                canonical,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
        digest.update(b"\n")
        count += 1
    return {
        "authority_revision": max(max_seq, count),
        "content_hash": digest.hexdigest(),
        "cache_schema_version": MANIFEST_SCHEMA_VERSION,
        "interaction_count": count,
        "max_session_seq": max_seq,
    }


def compute_session_manifest(connection: Any, session_id: str) -> dict[str, Any]:
    has_durable_manifest = False
    manifest_table = "session_manifests"
    is_sqlite = isinstance(connection, sqlite3.Connection)
    if is_sqlite:
        try:
            connection.execute("SELECT sqlite_version()").fetchone()
        except Exception:
            is_sqlite = False

    if is_sqlite:
        try:
            trigger_prefix = ""
            schema_name = "main"
            required_triggers = {
                "trg_session_manifest_interaction_insert",
                "trg_session_manifest_interaction_move_old",
            }
            installed_triggers = {
                str(row[0])
                for row in connection.execute(
                    f"SELECT name FROM {schema_name}.sqlite_master "
                    "WHERE type='trigger' AND name IN (?,?)",
                    tuple(required_triggers),
                ).fetchall()
            }
            if installed_triggers != required_triggers:
                connection.executescript(
                    _SQLITE_MANIFEST_SCHEMA.format(
                        manifest_table=manifest_table,
                        trigger_prefix=trigger_prefix,
                    )
                )
            has_durable_manifest = True
        except Exception:
            has_durable_manifest = False
    else:
        # Postgres create the table + trigger during provider bootstrap.
        # Detect the provider from the connection object rather than deliberately
        # issuing SQLite-only SQL: a failed probe aborts a Postgres transaction.
        try:
            connection.execute(
                "SELECT authority_revision FROM session_manifests LIMIT 1"
            ).fetchone()
            has_durable_manifest = True
        except Exception:
            # A failed statement aborts a normal psycopg transaction. Recover so
            # the caller can still fetch the transcript without durable caching.
            try:
                connection.rollback()
            except Exception:
                pass
            has_durable_manifest = False

    # A trigger can advance the revision between reading the transcript and
    # publishing its hash. Publish only if the revision we read is still current;
    # otherwise rebuild from the new snapshot. This makes a clean manifest a
    # trustworthy cache-validation record rather than a best-effort hint.
    for _attempt in range(3):
        durable_row = None
        if has_durable_manifest:
            try:
                durable_row = connection.execute(
                    "SELECT authority_revision,content_hash,interaction_count,"
                    f"max_session_seq,dirty FROM {manifest_table} WHERE session_id=?",
                    (session_id,),
                ).fetchone()
                if durable_row and str(durable_row[1] or "") and not bool(durable_row[4]):
                    return {
                        "authority_revision": int(durable_row[0] or 0),
                        "content_hash": str(durable_row[1]),
                        "cache_schema_version": MANIFEST_SCHEMA_VERSION,
                        "interaction_count": int(durable_row[2] or 0),
                        "max_session_seq": int(durable_row[3] or 0),
                    }
            except Exception:
                if not is_sqlite:
                    try:
                        connection.rollback()
                    except Exception:
                        pass
                durable_row = None

        cursor = connection.cursor()
        columns = ", ".join(_FIELDS)
        try:
            cursor.execute(
                f"SELECT {columns} FROM interactions WHERE session_id = ? "
                "ORDER BY COALESCE(session_seq, 0), created_at, id",
                (session_id,),
            )
        except Exception:
            # Legacy databases may lack newer interaction columns. They are not safe
            # persistent-cache sources, so advertise an empty/non-cacheable manifest.
            if not is_sqlite:
                try:
                    connection.rollback()
                except Exception:
                    pass
            return {
                "authority_revision": 0,
                "content_hash": "",
                "cache_schema_version": MANIFEST_SCHEMA_VERSION,
                "interaction_count": 0,
                "max_session_seq": 0,
            }
        manifest = manifest_from_rows(cursor.fetchall())
        expected_revision = int(durable_row[0] or 0) if durable_row else None
        if expected_revision is not None:
            manifest["authority_revision"] = max(
                expected_revision, int(manifest["authority_revision"])
            )
        if not has_durable_manifest:
            return manifest

        try:
            # End the read transaction before the compare-and-swap. A mutation
            # committed after the SELECT is then visible through its new revision.
            connection.commit()
            now = datetime.now(timezone.utc).isoformat()
            values = (
                manifest["authority_revision"],
                manifest["content_hash"],
                manifest["interaction_count"],
                manifest["max_session_seq"],
                now,
                session_id,
            )
            if expected_revision is None:
                published = connection.execute(
                    f"""INSERT INTO {manifest_table}
                           (authority_revision,content_hash,interaction_count,
                            max_session_seq,dirty,updated_at,session_id)
                       VALUES (?,?,?,?,0,?,?)
                       ON CONFLICT(session_id) DO NOTHING""",
                    values,
                )
            else:
                published = connection.execute(
                    f"""UPDATE {manifest_table}
                           SET authority_revision=?,content_hash=?,
                               interaction_count=?,max_session_seq=?,
                               dirty=0,updated_at=?
                         WHERE session_id=? AND authority_revision=? AND dirty=1""",
                    values + (expected_revision,),
                )
            if int(published.rowcount or 0) == 1:
                connection.commit()
                return manifest
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            break

    # Repeated concurrent changes should cost a full transcript fetch, never a
    # false cache hit. The next validation can rebuild once writes settle.
    manifest["content_hash"] = ""
    return manifest

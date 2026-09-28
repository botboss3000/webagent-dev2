"""Hybrid local-first storage backend (SQLite hot store + Postgres authority).

WHAT THIS IS
------------
A decorator over TWO ``StorageBackend``-shaped objects — a LOCAL SQLite backend
and a REMOTE Postgres backend — that presents the single ``StorageBackend``
surface the rest of the app already calls. It is inserted in ``get_db()`` exactly
the way :class:`EncryptedStorageBackend` wraps a single backend today. Nothing
above the storage layer (agent loop, API, UI) changes.

Like ``EncryptedStorageBackend`` it deliberately does NOT inherit from the
``StorageBackend`` ABC (that would force us to re-declare ~100 abstract methods).
It duck-types: any method NOT explicitly overridden here is delegated by
``__getattr__`` to a default backend, so callers typed as ``StorageBackend`` are
unaffected.

DISPOSITIONS (see temp/hybrid-local-first-db-handoff.md §3)
----------------------------------------------------------
Every method resolves to one of four behaviours:

  * SERVER-AUTHORITATIVE (money / identity / coordination) → remote, synchronous.
    A local copy must NEVER decide authorization/billing. This is the default for
    anything not explicitly localized, so the app stays correct-by-default.
  * SYNCED (shared content/config)  → local read (fast) + local write-through, with
    the write enqueued to an outbox and pushed to remote in the background.
  * PRE-CACHED LOCAL (chat-start config) → same as Synced, warm local replica.
  * LOCAL-ONLY / LOCAL-HEAVY (disposable detail) → local only, never remote.

PHASING
-------
This file is built up phase by phase; each phase is independently shippable and
guarded so the live app is untouched until the hybrid is explicitly enabled.

  * Phase 1 (this commit): the seam. Everything delegates to REMOTE (the
    authority) — i.e. exactly today's behaviour when Postgres is the active DB —
    while a live LOCAL backend is constructed and held, ready for later phases.
    Zero behaviour change; proves the wrapper composes with encryption + the
    factory.
  * Phase 2: local-only tier (diagnostics / render recordings never hit remote).
  * Phase 3: the ``interactions`` split (fat tool payloads local, skeleton synced).
  * Phase 4: write-through + outbox sync for the rest of the Synced tier.

ENABLEMENT
----------
On by default. Turned off via ``app/db_hybrid.json`` (``{"enabled": false}``) or the
``WEBAGENT_DB_HYBRID`` env var when config is env-locked. Only ever built when a
reachable Postgres-family remote exists — on a local-only install it is a no-op
and ``get_db()`` returns the plain local backend as before.
"""

from __future__ import annotations

import asyncio
import json
import logging

from app.db.session_activity import project_interaction_activity
import os
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Interactions split tuning ─────────────────────────────────────────────────
# A fat column value LARGER than this (chars) would be pulled OFF the remote
# authority and kept only in the local hot store; anything at or under it stays
# inline on the remote skeleton. The interactions transcript currently declares
# no fat columns (see the sync engine's SYNCED_SPECS), so nothing is stripped —
# the constant is kept as the single definition point should a fat column be
# reintroduced.
_FAT_THRESHOLD_CHARS = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_local_payloads_sync(ids) -> Dict[str, dict]:
    """Read the full ``output`` for interaction ids from the LOCAL store
    directly (no HybridBackend instance needed), returning
    ``{id: {"output":..}}``.

    For read paths that query the remote skeleton on their own connection (the
    admin DB Viewer) and want to restore the fat payloads the local-first hot
    store holds. Synchronous — opens the local (possibly SQLCipher) file via
    db_crypto. Returns ``{}`` on any error / when a row isn't local, so callers
    can merge unconditionally."""
    ids = [i for i in (ids or []) if i]
    if not ids:
        return {}
    try:
        from app.db import db_crypto
        from app.db.local import get_db_user_context
        from app.db.user_store import _user_db_path
        user_id = get_db_user_context()
        conn = db_crypto.connect(_user_db_path(user_id), "_local_custom")
    except Exception:
        return {}
    out: Dict[str, dict] = {}
    try:
        for i in range(0, len(ids), 400):
            chunk = ids[i:i + 400]
            q = ("SELECT id, output FROM interactions "
                 f"WHERE id IN ({','.join('?' * len(chunk))})")
            for row in conn.execute(q, chunk).fetchall():
                out[row["id"]] = {"output": row["output"]}
    except Exception:
        pass
    finally:
        try:
            conn.close()
        except Exception:
            pass
    return out

# ── Enablement flag ──────────────────────────────────────────────────────────
_APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_FLAG_FILE = os.path.join(_APP_DIR, "db_hybrid.json")
_enabled_cache: Optional[bool] = None


def _env_locked() -> bool:
    return os.environ.get("WEBAGENT_CONFIG_SOURCE", "").lower() == "env"


def hybrid_enabled() -> bool:
    """True if the hybrid local-first layer is switched on for this install.

    Env-locked deployments read ``WEBAGENT_DB_HYBRID`` (1/true/yes/on/absent=on);
    otherwise the persisted ``db_hybrid.json`` flag. Absent/unreadable = ON (the
    default), so a fresh install ships with local-first storage active whenever
    a Postgres-family remote is configured (a no-op otherwise)."""
    global _enabled_cache
    if _enabled_cache is not None:
        return _enabled_cache
    val = True
    if _env_locked():
        raw = os.environ.get("WEBAGENT_DB_HYBRID", "").strip().lower()
        val = raw not in ("0", "false", "no", "off") if raw else True
    else:
        try:
            if os.path.exists(_FLAG_FILE):
                with open(_FLAG_FILE, "r") as f:
                    val = bool((json.load(f) or {}).get("enabled", True))
        except Exception as e:
            logger.warning("Failed to read db_hybrid.json: %s", e)
            val = True
    _enabled_cache = val
    return val


def set_hybrid_enabled(on: bool) -> None:
    """Persist the hybrid on/off flag. Takes effect on the next ``get_db()`` build
    (callers should ``reset_db_instance()`` after flipping it)."""
    global _enabled_cache
    _enabled_cache = bool(on)
    if _env_locked():
        return
    try:
        tmp = _FLAG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"enabled": bool(on)}, f, indent=2)
        os.replace(tmp, _FLAG_FILE)
    except Exception as e:
        logger.warning("Failed to write db_hybrid.json: %s", e)


# ── The backend ──────────────────────────────────────────────────────────────


class HybridBackend:
    """Local-first decorator over a (local, remote) backend pair.

    ``local`` is a LocalBackend (SQLite hot store); ``remote`` is a Postgres-family
    backend (the shared authority). Both share LocalBackend's method surface —
    PostgresBackend subclasses LocalBackend — which is what makes per-method
    routing tractable.

    Phase 1: every StorageBackend method delegates to ``remote`` via ``__getattr__``
    (unchanged behaviour). ``local`` is live and reachable via ``self.local`` for
    the phases that follow.
    """

    def __init__(self, local, remote) -> None:
        self._local = local
        self._remote = remote

    # Expose the two halves for the sync engine + later-phase overrides + tests.
    @property
    def local(self):
        return self._local

    @property
    def remote(self):
        return self._remote

    # ── Pass-through ─────────────────────────────────────────────────────────
    def __getattr__(self, name):
        # Only invoked for attributes not found on the instance/class, so any
        # method explicitly overridden below shadows this. Default target is the
        # REMOTE authority — Phase 1 behaviour and the safe default for every
        # server-authoritative method thereafter.
        return getattr(self._remote, name)

    def get_raw_client(self):
        """Direct query client for admin/tools. Points at the remote authority so
        admin reads/writes see the shared source of truth. (Local-only tables are
        queried through the typed methods, which route to ``local``.)"""
        return self._remote.get_raw_client()

    async def refresh_session_metadata(self, user_id: str) -> List[str]:
        """Bring newer shared session metadata into this device's SQLite mirror.

        This is intentionally narrower than a transcript resync: opening the
        chat-panel session picker needs titles, pins, visibility and ordering to
        converge promptly, but must not download interaction payloads.  A remote
        row is applied only when its ``updated_at`` is newer than the local copy,
        so an edit that is still waiting in this device's outbox is never
        overwritten by an older authority snapshot.
        """
        return await asyncio.to_thread(self._refresh_session_metadata_sync, user_id)

    def _refresh_session_metadata_sync(self, user_id: str) -> List[str]:
        remote_rows = (self._remote.get_raw_client().table("sessions").select("*")
                       .eq("user_id", user_id).execute().data or [])
        if not remote_rows:
            return []
        local_rows = (self._local.get_raw_client().table("sessions").select("*")
                      .eq("user_id", user_id).execute().data or [])
        local_by_id = {str(row.get("id")): row for row in local_rows if row.get("id")}

        def _stamp(value) -> float:
            if not value:
                return 0.0
            try:
                parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
                # SQLite's CURRENT_TIMESTAMP is UTC but has no offset.
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (TypeError, ValueError):
                return 0.0

        changed: List[str] = []
        for remote_row in remote_rows:
            sid = str(remote_row.get("id") or "")
            if not sid:
                continue
            local_row = local_by_id.get(sid)
            remote_updated = _stamp(remote_row.get("updated_at"))
            local_updated = _stamp((local_row or {}).get("updated_at"))
            # Last-writer-wins: do not replace this device's newer unsynced edit.
            if local_row is not None and remote_updated <= local_updated:
                continue
            clean = {key: value for key, value in remote_row.items() if value is not None}
            self._local.get_raw_client().table("sessions").upsert(
                clean, on_conflict="id").execute()
            changed.append(sid)
        return changed

    # ══════════════════════════════════════════════════════════════════════════
    # Stage 2 — local-first transcript, background push
    #
    # The FULL interaction row (content + `output`) is written to
    # the LOCAL SQLite hot store FIRST — the id is minted locally and the write
    # completes without touching the network, so a turn never waits on the remote.
    # The row is then recorded in the local OUTBOX; the background SyncEngine
    # (app/db/sync/) drains it to the remote authority within a few seconds as a
    # SKELETON (fat columns over the size gate stripped to NULL), keeping a second
    # device / the shared record current. Reads are served from SQLite; on first
    # access to a session its row + existing transcript are pulled from the remote
    # into SQLite ("warm on chat open"), so history is present locally. Fat detail
    # for rows another device wrote is the skeleton only (handoff §7).
    #
    # (Stage 1 wrote the remote skeleton synchronously inline; Stage 2 replaced
    # that inline round-trip with the outbox enqueue below.)
    # ══════════════════════════════════════════════════════════════════════════

    # Full interactions column set (matches app/db/schema/tables.py), minus
    # created_at which the local row defaults to now on a fresh write and is
    # preserved verbatim when mirroring existing remote rows.
    _ICOLS = ("id", "session_id", "parent_id", "role", "content", "tool_name",
              "tool_call_id", "channel", "metadata", "output", "source",
              "from_id", "to_id", "session_seq", "turn_id", "turn_seq", "chat_render_kind", "status")

    # (The remote skeleton's fat-column stripping now lives in the sync engine's
    # push, gated by the shared _FAT_THRESHOLD_CHARS above.)

    # ── Warm SQLite: pull a session + its transcript from remote on first touch ──

    # How long a session's local mirror is trusted before a read re-pulls any rows
    # another device has since written + pushed. Bounds cross-device staleness on
    # the typed read paths (history rebuild / Remote Control) without paying a
    # remote round-trip on every read. The chat panel's live tail makes its own
    # local-vs-remote decision (see get_session_tail), so this need not be tiny.
    _MIRROR_TTL = 8.0

    async def _ensure_local_session(self, user_id: str, session_id: str,
                                    refresh: bool = False) -> None:
        """Warm a session's local mirror.

        First touch does a full pull. Thereafter WRITE callers (``refresh=False``)
        treat it as a cheap once-per-process no-op — the owning device holds every
        row it wrote local-first, so re-pulling would only add a pointless remote
        round-trip on the hot write path. READ callers (``refresh=True``) re-pull
        at most every ``_MIRROR_TTL`` seconds (INSERT OR IGNORE, so it only ADDS
        rows another device has since written + pushed — the local copy's own
        full-fidelity rows are untouched), bounding cross-device staleness on the
        history-rebuild / Remote-Control read paths without a per-read round-trip."""
        seen = getattr(self, "_mirror_at", None)
        if seen is None:
            seen = {}
            self._mirror_at = seen
        now = time.monotonic()
        last = seen.get(session_id)
        if last is not None and (not refresh or (now - last) < self._MIRROR_TTL):
            return
        seen[session_id] = now
        try:
            rraw = self._remote.get_raw_client()
            srows = rraw.table("sessions").select("*").eq("id", session_id).execute().data or []
            if not srows:
                return
            # Drop NULL-valued keys so LOCAL defaults fill NOT-NULL columns the
            # remote left null (e.g. sessions.hidden / sort_order) — an explicit
            # NULL would violate the local NOT NULL constraint.
            srow = {k: v for k, v in srows[0].items() if v is not None}
            self._local.get_raw_client().table("sessions").upsert(srow, on_conflict="id").execute()
            # After the initial hydrate, fetch only rows newer than the local
            # per-session sequence watermark. This avoids re-downloading a whole
            # conversation on every periodic freshness check.
            local_conn = self._local._get_conn()
            try:
                local_max = local_conn.execute(
                    "SELECT MAX(session_seq) FROM interactions WHERE session_id = ?",
                    (session_id,),
                ).fetchone()
                local_seq = local_max[0] if local_max and local_max[0] is not None else None
            finally:
                local_conn.close()
            interaction_query = rraw.table("interactions").select("*").eq("session_id", session_id)
            if local_seq is not None:
                interaction_query = interaction_query.gt("session_seq", local_seq)
            irows = interaction_query.order("session_seq", desc=False).execute().data or []
            if irows:
                # The remote authority can outlive a local app upgrade (or carry
                # a retired column from a previous schema).  Never let such an
                # extra field make the whole local transcript mirror fail.  In
                # particular, old Postgres installs had ``interactions.input``;
                # SQLite has never used that column.
                cols = [c for c in (*self._ICOLS, "created_at") if c in irows[0]]
                if not cols:
                    return
                col_sql = ",".join(cols)
                placeholders = ",".join("?" * len(cols))
                values = [tuple(r.get(c) for c in cols) for r in irows]
                async with self._local._write_lock:
                    conn = self._local._get_conn()
                    try:
                        conn.executemany(
                            f"INSERT OR IGNORE INTO interactions ({col_sql}) VALUES ({placeholders})",
                            values,
                        )
                        conn.commit()
                    finally:
                        conn.close()
        except Exception as e:
            logger.warning("hybrid: mirroring session %s to local failed: %s", session_id, e)
            # Don't wedge writes if the mirror hiccups — allow the local row to be
            # created; the session upsert above usually succeeded first.

    async def resync_session(self, user_id: str, session_id: str) -> None:
        """Force a re-pull of a session's transcript from the remote authority,
        overriding the once-per-process warm cache.

        The transcript is push-only (the SyncEngine never pulls interactions back),
        and ``_ensure_local_session`` warms a given session's local copy exactly
        once. That's fine for a session that only ever runs on one device — but
        Remote Control hands a session BACK AND FORTH between devices. When a device
        takes over a turn it may already hold a STALE local copy (warmed earlier,
        missing the turns another device has since written + pushed to remote).
        Discarding the warm marker and re-warming pulls the missing turns in
        (INSERT OR IGNORE, so the device's own full-fidelity rows are untouched and
        only the genuinely-missing rows are added)."""
        seen = getattr(self, "_mirror_at", None)
        if seen is not None:
            seen.pop(session_id, None)
        await self._ensure_local_session(user_id, session_id)

    async def _local_write_full(self, row: Dict[str, Any]) -> None:
        """Durably write one interaction and its remote-push marker.

        An interaction id becomes a parent key as soon as this method returns.
        Never acknowledge it unless both the SQLite row and its durable outbox
        marker committed together.
        """
        cols = [c for c in self._ICOLS if c in row]
        col_sql = ",".join(cols)
        placeholders = ",".join("?" * len(cols))
        set_sql = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
        vals = tuple(row.get(c) for c in cols)
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                conn.execute(
                    f"INSERT INTO interactions ({col_sql}) VALUES ({placeholders}) "
                    f"ON CONFLICT(id) DO UPDATE SET {set_sql}",
                    vals,
                )
                project_interaction_activity(conn, str(row["id"]))
                # The outbox is part of the write's durability contract. A crash
                # between two independent commits otherwise leaves a local-only
                # transcript row that can never reach the shared authority.
                from app.db.sync.outbox import _TABLE, _now_iso
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "table_name TEXT NOT NULL, row_id TEXT NOT NULL, "
                    "op TEXT NOT NULL DEFAULT 'upsert', created_at TEXT NOT NULL)"
                )
                conn.execute(
                    f"INSERT INTO {_TABLE} (table_name, row_id, op, created_at) VALUES (?, ?, ?, ?)",
                    ("interactions", row["id"], "upsert", _now_iso()),
                )
                conn.commit()
            except Exception as e:
                conn.rollback()
                logger.warning("hybrid: local full-row write failed for %s: %s", row.get("id"), e)
                raise
            finally:
                conn.close()

    async def _local_update(self, interaction_id: str, fields: Dict[str, Any]) -> bool:
        """Patch an interaction and enqueue its update atomically.

        A missing row is an integrity failure, not a successful stream checkpoint.
        """
        fields = {k: v for k, v in fields.items() if v is not None}
        if not fields:
            return False
        sets = ",".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [interaction_id]
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                cur = conn.execute(f"UPDATE interactions SET {sets} WHERE id=?", vals)
                if cur.rowcount != 1:
                    raise RuntimeError(f"interaction {interaction_id} was not found during local update")
                project_interaction_activity(conn, interaction_id)
                from app.db.sync.outbox import _TABLE, _now_iso
                conn.execute(
                    f"CREATE TABLE IF NOT EXISTS {_TABLE} ("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT, "
                    "table_name TEXT NOT NULL, row_id TEXT NOT NULL, "
                    "op TEXT NOT NULL DEFAULT 'upsert', created_at TEXT NOT NULL)"
                )
                conn.execute(
                    f"INSERT INTO {_TABLE} (table_name, row_id, op, created_at) VALUES (?, ?, ?, ?)",
                    ("interactions", interaction_id, "upsert", _now_iso()),
                )
                conn.commit()
                return True
            except Exception as e:
                conn.rollback()
                logger.warning("hybrid: local update failed for %s: %s", interaction_id, e)
                raise
            finally:
                conn.close()

    async def stamp_interaction_seq(
        self,
        interaction_id: str,
        session_seq: int,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
    ) -> Optional[int]:
        """Backfill ordering columns on a LOCAL-first row where it actually lives.

        The transcript is written local-first; the row does NOT exist on the remote
        authority until the background push lands. A backfill aimed at the remote
        (the default ``_get_conn`` target) would match zero rows and the row would
        stay NULL-``session_seq`` forever — invisible to the reconcile tail's
        ``session_seq IS NOT NULL`` filter. So write the LOCAL copy and re-enqueue
        it, so the push carries the stamped number to remote too.

        Returns the row's authoritative durable ``session_seq`` (see
        ``LocalBackend.stamp_interaction_seq``)."""
        fields = {"session_seq": session_seq}
        if turn_id is not None:
            fields["turn_id"] = turn_id
        if turn_seq is not None:
            fields["turn_seq"] = turn_seq
        # Reuse LocalBackend's late-sequence detector, then enqueue the completed
        # projection so the anomaly metadata travels with the interaction.
        stamped = await self._local.stamp_interaction_seq(interaction_id, session_seq, turn_id, turn_seq)
        # Re-publish the LOCAL row's authoritative order, never the caller-supplied
        # candidate: a durable session_seq already committed at INSERT must not be
        # overwritten by a later backfill candidate (the live-event backfill passes
        # this event's replay cursor, which drifts far above the durable ledger).
        if stamped is not None and stamped != session_seq:
            fields["session_seq"] = stamped
        await self._local_update(interaction_id, fields)
        return stamped

    async def publish_interaction_now(self, interaction_id: str) -> None:
        """Push one local interaction to the shared authority immediately.

        Remote-control dispatch has a strict ordering requirement: the target's
        shared ``device_jobs`` row must not become claimable before its user turn
        is visible on the shared transcript.  The normal outbox is deliberately
        asynchronous, so it cannot provide that guarantee.  This publishes the
        exact current row through the same skeletonizer the sync engine uses;
        the outbox marker remains in place as harmless retry protection.
        """
        from app.db.sync.engine import SYNCED_SPECS, SyncEngine

        rows = await asyncio.to_thread(
            lambda: self._local.get_raw_client().table("interactions").select("*")
            .eq("id", interaction_id).execute().data or []
        )
        if not rows:
            raise RuntimeError(f"local interaction {interaction_id} is missing")
        engine = SyncEngine(self)
        await asyncio.to_thread(
            engine._push_row, "interactions", SYNCED_SPECS["interactions"], rows[0]
        )

    async def next_session_seq(self, session_id: str, count: int = 1) -> int:
        """Allocate the next ordering number from the LOCAL store.

        The local hot store is the source of truth for the transcript (rows are
        written local-first and only some devices' rows ever reach a given local
        copy), so the highest ``session_seq`` seen locally is the correct basis for
        the next value. Reading the remote authority here (the ``__getattr__``
        default) would under-count whenever local rows haven't pushed yet and hand
        back a colliding number."""
        return await self._local.next_session_seq(session_id, count)

    async def persist_session_manifest_seq(self, session_id: str, high_water: int) -> None:
        """Persist the in-memory RunBuffer counter into the LOCAL manifest
        (the remote manifest is a downstream mirror and shouldn't be driven
        directly from the in-memory state)."""
        return await self._local.persist_session_manifest_seq(session_id, high_water)

    def _outbox(self):
        """Lazily-built local outbox — the durable queue the background SyncEngine
        drains to the remote authority. Kept here so a write can enqueue even when
        no engine is running yet (it just accumulates until one starts)."""
        ob = getattr(self, "_outbox_inst", None)
        if ob is None:
            from app.db.sync import Outbox
            ob = Outbox(self._local)
            self._outbox_inst = ob
        return ob

    async def _enqueue(self, table: str, row_id: str) -> None:
        """Record a local row for background push; never let a queue hiccup break
        the write (the row is already durable locally — worst case it syncs late)."""
        try:
            await self._outbox().enqueue(table, row_id)
        except Exception as e:
            logger.warning("hybrid: outbox enqueue for %s/%s failed: %s", table, row_id, e)

    async def reconcile_local_only_agents(self) -> dict:
        """Back-fill agents that live ONLY in this device's local mirror up to the
        shared authority.

        The outbox only carries changes made *through* the hybrid write path, so an
        agent created while this device was a standalone LocalBackend (single-device
        mode, or a Postgres outage that fell back to local) never gets pushed once
        the device joins the shared DB — it stays invisible to every other device
        forever (the exact bug that stranded the user's Claude agent). This one-shot
        reconciliation, run at startup, finds owned/active/non-clone local agents the
        authority is missing and enqueues them for the normal background push.

        Guard for the WebAgent singleton: a local-only ``default`` agent is pushed
        only when its owner has NO active ``default`` on the authority yet — so we
        recover a genuinely-unsynced WebAgent but never propagate a race duplicate.

        Returns a small summary dict; never raises (best-effort)."""
        import json as _json
        summary = {"scanned": 0, "pushed": 0, "skipped_present": 0, "skipped_dup_default": 0}
        try:
            # 1. Remote authority: which agent ids already exist, and which owners
            #    already have an active default (the singleton guard).
            remote_ids: set = set()
            remote_default_owners: set = set()
            rc = self._remote.get_raw_client()
            for r in (rc.table("agents").select("id,template_id,status,admin_users").execute().data or []):
                remote_ids.add(str(r.get("id")))
                if r.get("template_id") == "default" and (r.get("status") or "") in ("", "active"):
                    admins = r.get("admin_users")
                    if isinstance(admins, str):
                        try:
                            admins = _json.loads(admins)
                        except Exception:
                            admins = []
                    for u in (admins or []):
                        remote_default_owners.add(u)

            # 2. Local pipeline template ids — internal machinery, never user agents.
            pipeline_ids: set = set()
            conn = self._local._get_conn()
            try:
                for row in conn.execute("SELECT id FROM agent_templates WHERE is_pipeline = 1").fetchall():
                    pipeline_ids.add(row["id"])
                local_rows = conn.execute(
                    """SELECT id, template_id, status, admin_users
                       FROM agents
                       WHERE (status IS NULL OR status = '' OR status = 'active')"""
                ).fetchall()
            finally:
                conn.close()

            # 3. Decide + enqueue.
            to_push: list = []
            for row in local_rows:
                d = dict(row)
                summary["scanned"] += 1
                aid = str(d.get("id"))
                if aid in remote_ids:
                    summary["skipped_present"] += 1
                    continue
                if d.get("template_id") in pipeline_ids:
                    continue
                admins = d.get("admin_users")
                if isinstance(admins, str):
                    try:
                        admins = _json.loads(admins)
                    except Exception:
                        admins = []
                if not admins:  # unowned — not a user-facing agent
                    continue
                if d.get("template_id") == "default":
                    # Singleton guard: don't propagate a duplicate WebAgent.
                    if any(u in remote_default_owners for u in admins):
                        summary["skipped_dup_default"] += 1
                        continue
                to_push.append(aid)

            for aid in to_push:
                await self._enqueue("agents", aid)
                summary["pushed"] += 1

            if summary["pushed"] or summary["skipped_dup_default"]:
                logger.info("hybrid: reconciled local-only agents → %s", summary)
        except Exception as e:
            logger.warning("hybrid: reconcile_local_only_agents failed: %s", e)
        return summary

    async def insert_interaction_with_sequence(self, user_id: str, session_id: str, role: str, content: str, **kwargs) -> tuple[str, int]:
        await self._ensure_local_session(user_id, session_id)
        rid, seq = await self._local.insert_interaction_with_sequence(user_id, session_id, role, content, **kwargs)
        await self._enqueue("interactions", rid)
        return rid, seq

    async def insert_interaction(
        self,
        user_id: str,
        session_id: str,
        role: str,
        content: str,
        parent_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        channel: Optional[str] = None,
        metadata: Optional[str] = None,
        output_data: Optional[str] = None,
        sender_id: Optional[str] = None,
        receiver_id: Optional[str] = None,
        source: Optional[str] = None,
        session_seq: Optional[int] = None,
        turn_id: Optional[str] = None,
        turn_seq: Optional[int] = None,
        status: str = "complete",
        chat_render_kind: Optional[str] = None,
    ) -> str:
        if session_seq is None:
            rid, _ = await self.insert_interaction_with_sequence(user_id, session_id, role, content, parent_id=parent_id, tool_name=tool_name, tool_call_id=tool_call_id, channel=channel, metadata=metadata, output_data=output_data, sender_id=sender_id, receiver_id=receiver_id, source=source, turn_id=turn_id, turn_seq=turn_seq, status=status, chat_render_kind=chat_render_kind)
            return rid
        # Local-first: mint the id here, write the FULL row to SQLite (source of
        # truth for reads), then enqueue it for the background push to remote. No
        # network on the write path.
        rid = str(uuid.uuid4())
        await self._ensure_local_session(user_id, session_id)
        row = {
            "id": rid, "session_id": session_id, "parent_id": parent_id, "role": role,
            "content": content, "tool_name": tool_name, "tool_call_id": tool_call_id,
            "channel": channel, "metadata": metadata,
            "output": output_data, "source": source or "user", "from_id": sender_id,
            "to_id": receiver_id, "session_seq": session_seq, "turn_id": turn_id,
            "turn_seq": turn_seq, "status": status, "chat_render_kind": chat_render_kind,
        }
        try:
            await self._local_write_full(row)
        except Exception:
            logger.warning(
                "hybrid: insert_interaction failed for %s (session %s) — "
                "re-pulling session from remote and retrying once",
                rid, session_id,
            )
            seen = getattr(self, "_mirror_at", None)
            if seen is not None:
                seen.pop(session_id, None)
            await self._ensure_local_session(user_id, session_id, refresh=True)
            await self._local_write_full(row)
        return rid

    async def insert_interactions_batch(self, rows: List[Dict[str, Any]]) -> List[str]:
        if not rows:
            return []
        # Local-first: mint ids, write the FULL rows to SQLite, enqueue for push.
        ids: List[str] = []
        seen_sessions = set()
        for r in rows:
            sid = r.get("session_id")
            if sid and sid not in seen_sessions:
                seen_sessions.add(sid)
                await self._ensure_local_session(r.get("user_id", ""), sid)
            rid = r.get("id") or str(uuid.uuid4())
            ids.append(rid)
            await self._local_write_full({
                "id": rid, "session_id": sid, "parent_id": r.get("parent_id"),
                "role": r.get("role", "tool"), "content": r.get("content", ""),
                "tool_name": r.get("tool_name"), "tool_call_id": r.get("tool_call_id"),
                "channel": r.get("channel"), "metadata": r.get("metadata"),
                "output": r.get("output"),
                "source": r.get("source") or "user", "from_id": r.get("from_id"),
                "to_id": r.get("to_id"), "session_seq": r.get("session_seq"),
                "turn_id": r.get("turn_id"), "turn_seq": r.get("turn_seq"),
                "status": r.get("status", "complete"), "chat_render_kind": r.get("chat_render_kind"),
            })
        return ids

    async def update_interaction(
        self,
        interaction_id: str,
        *,
        content: Optional[str] = None,
        status: Optional[str] = None,
        output_data: Optional[str] = None,
        metadata: Optional[str] = None,
        chat_render_kind: Optional[str] = None,
    ) -> bool:
        # Local-first: patch the full row in SQLite (keeps the fat output), then
        # enqueue so the background push refreshes the remote skeleton.
        return await self._local_update(interaction_id, {
            "content": content, "status": status, "output": output_data, "metadata": metadata, "chat_render_kind": chat_render_kind,
        })

    async def get_activity_summary_for_interaction(self, user_id: str, session_id: str, interaction_id: str):
        await self._ensure_local_session(user_id, session_id)
        return await self._local.get_activity_summary_for_interaction(user_id, session_id, interaction_id)

    async def fetch_interactions(self, user_id: str, session_id: str):
        # Warm the local copy on first touch, then read the transcript LOCALLY.
        # Safety net: if the local mirror is missing (a mirror hiccup left the
        # session out of the local store), fall back to the remote authority so a
        # read NEVER hard-fails a chat — degrade, don't break.
        await self._ensure_local_session(user_id, session_id, refresh=True)
        try:
            return await self._local.fetch_interactions(user_id, session_id)
        except PermissionError:
            logger.warning("hybrid: local session %s absent — reading from remote", session_id)
            return await self._remote.fetch_interactions(user_id, session_id)

    async def count_interactions(self, user_id: str, session_id: str) -> int:
        await self._ensure_local_session(user_id, session_id, refresh=True)
        try:
            return await self._local.count_interactions(user_id, session_id)
        except PermissionError:
            return await self._remote.count_interactions(user_id, session_id)

    async def fetch_session_interaction(
        self, user_id: str, session_id: str, interaction_id: str,
    ):
        await self._ensure_local_session(user_id, session_id, refresh=True)
        try:
            return await self._local.fetch_session_interaction(
                user_id, session_id, interaction_id)
        except PermissionError:
            return await self._remote.fetch_session_interaction(
                user_id, session_id, interaction_id)

    async def fetch_interactions_from_offset(
        self, user_id: str, session_id: str, offset: int
    ):
        await self._ensure_local_session(user_id, session_id, refresh=True)
        try:
            return await self._local.fetch_interactions_from_offset(
                user_id, session_id, offset)
        except PermissionError:
            return await self._remote.fetch_interactions_from_offset(
                user_id, session_id, offset)

    async def fetch_first_user_messages(self, user_id: str, session_id: str, limit: int = 3):
        await self._ensure_local_session(user_id, session_id, refresh=True)
        try:
            return await self._local.fetch_first_user_messages(user_id, session_id, limit)
        except PermissionError:
            return await self._remote.fetch_first_user_messages(user_id, session_id, limit)

    # ══════════════════════════════════════════════════════════════════════════
    # Stage 3 — pre-cache agent config on chat open ("instant but double-check")
    #
    # An agent's config (its `agents` row + `agent_prompts` slots) is needed at
    # chat-open and on every turn, but unlike the append-only transcript it is
    # CENTRALLY AUTHORED and can change. So we keep a warm LOCAL replica and, on a
    # config read, do ONE cheap round-trip to the remote authority for a version
    # STAMP (the newest `updated_at` across the agent's config rows). If the stamp
    # is unchanged since we last mirrored, the heavy config is served from local
    # with no further network; if it moved (an edit here or on another device) we
    # pull the bundle fresh first. The stamp check is itself cached for a few
    # seconds so the several config reads in one turn collapse to one verification.
    #
    # Config is Synced-tier (not money/identity), so a bounded (~stamp-TTL) window
    # of staleness is acceptable (handoff §8/§9). Authority still lives on the
    # remote — writes delegate there and bump the stamp, which is what refreshes
    # us. Neither `agents` nor `agent_prompts` has a foreign key, so the local
    # mirror writes can't trip an FK.
    # ══════════════════════════════════════════════════════════════════════════

    _AGENT_STAMP_TTL = 5.0  # seconds a freshness check is trusted before re-verifying

    async def _agent_config_stamp(self, agent_id: str) -> Optional[str]:
        """One round-trip: the newest `updated_at` across the agent's config rows
        on the REMOTE authority, as a string. None on any error / missing agent →
        the caller then reads remote (correct, just not accelerated)."""
        try:
            conn = self._remote._get_conn()
            try:
                row = conn.execute(
                    "SELECT MAX(u) FROM ("
                    "SELECT updated_at AS u FROM agents WHERE id = ? "
                    "UNION ALL "
                    "SELECT updated_at AS u FROM agent_prompts WHERE agent_id = ?"
                    ") x",
                    (agent_id, agent_id),
                ).fetchone()
                return str(row[0]) if row and row[0] is not None else None
            finally:
                conn.close()
        except Exception as e:
            logger.debug("hybrid: agent config stamp for %s failed: %s", agent_id, e)
            return None

    async def _mirror_agent_config(self, agent_id: str) -> bool:
        """Pull the agent's config bundle (agents row + agent_prompts slots) from
        the remote authority into local SQLite. The prompt slots are REPLACED
        wholesale so a slot deleted on the remote also disappears locally."""
        try:
            rraw = self._remote.get_raw_client()
            arows = rraw.table("agents").select("*").eq("id", agent_id).execute().data or []
            if not arows:
                return False
            arow = {k: v for k, v in arows[0].items() if v is not None}
            prows = rraw.table("agent_prompts").select("*").eq("agent_id", agent_id).execute().data or []
            ads_rows = (rraw.table("agent_data_sources").select("*")
                        .eq("agent_id", agent_id).execute().data or [])
            source_ids = [r.get("data_source_id") for r in ads_rows if r.get("data_source_id")]
            source_rows = []
            if source_ids:
                source_rows = (rraw.table("data_sources").select("*")
                               .in_("id", source_ids).execute().data or [])
            acols = list(arow.keys())
            a_set = ",".join(f"{c}=excluded.{c}" for c in acols if c != "id")
            async with self._local._write_lock:
                conn = self._local._get_conn()
                try:
                    def _upsert_rows(table: str, rows: List[dict]) -> None:
                        if not rows:
                            return
                        valid = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
                        for raw in rows:
                            row = {k: v for k, v in raw.items() if k in valid and v is not None}
                            if not row or "id" not in row:
                                continue
                            cols = list(row)
                            updates = ",".join(f"{c}=excluded.{c}" for c in cols if c != "id")
                            conflict = f" ON CONFLICT(id) DO UPDATE SET {updates}" if updates else " ON CONFLICT(id) DO NOTHING"
                            conn.execute(
                                f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' * len(cols))}){conflict}",
                                tuple(row[c] for c in cols),
                            )
                    conn.execute(
                        f"INSERT INTO agents ({','.join(acols)}) "
                        f"VALUES ({','.join('?' * len(acols))}) "
                        f"ON CONFLICT(id) DO UPDATE SET {a_set}",
                        tuple(arow.get(c) for c in acols),
                    )
                    conn.execute("DELETE FROM agent_prompts WHERE agent_id = ?", (agent_id,))
                    for pr in prows:
                        prow = {k: v for k, v in pr.items() if v is not None}
                        pcols = list(prow.keys())
                        conn.execute(
                            f"INSERT INTO agent_prompts ({','.join(pcols)}) "
                            f"VALUES ({','.join('?' * len(pcols))})",
                            tuple(prow.get(c) for c in pcols),
                        )
                    # These rows drive the attached-data-source tool bundle at
                    # chat start. Keep their compact configuration local; the
                    # actual remote source remains authoritative if a cold or
                    # stale mirror cannot be used.
                    conn.execute("DELETE FROM agent_data_sources WHERE agent_id = ?", (agent_id,))
                    _upsert_rows("data_sources", source_rows)
                    _upsert_rows("agent_data_sources", ads_rows)
                    conn.commit()
                finally:
                    conn.close()
            return True
        except Exception as e:
            logger.warning("hybrid: mirroring agent config %s failed: %s", agent_id, e)
            return False

    async def _ensure_local_agent(self, agent_id: str) -> bool:
        """Make the local config replica for `agent_id` usable + fresh; return True
        if a local config read is safe. Cheap remote stamp check, TTL-cached so
        repeated reads within one turn don't each pay a round-trip."""
        if not agent_id:
            return False
        stamps = getattr(self, "_agent_stamps", None)
        if stamps is None:
            stamps = {}
            self._agent_stamps = stamps
            self._agent_checked: Dict[str, float] = {}
        now = time.monotonic()
        # Recently verified? Trust the local replica without another round-trip.
        if agent_id in stamps and (now - self._agent_checked.get(agent_id, 0.0)) < self._AGENT_STAMP_TTL:
            return True
        remote_stamp = await self._agent_config_stamp(agent_id)
        if remote_stamp is None:
            return False  # can't verify freshness → read remote
        self._agent_checked[agent_id] = now
        if stamps.get(agent_id) == remote_stamp:
            return True  # local already fresh
        if await self._mirror_agent_config(agent_id):
            stamps[agent_id] = remote_stamp
            return True
        return False

    async def get_agent_by_id(self, agent_id: str):
        if await self._ensure_local_agent(agent_id):
            try:
                res = await self._local.get_agent_by_id(agent_id)
                if res is not None:
                    return res
            except Exception as e:
                logger.debug("hybrid: local get_agent_by_id fell back: %s", e)
        return await self._remote.get_agent_by_id(agent_id)

    async def fetch_agent_by_id_with_context(self, agent_id, context_types=None, user_id=None):
        if await self._ensure_local_agent(agent_id):
            try:
                res = await self._local.fetch_agent_by_id_with_context(agent_id, context_types, user_id)
                if res is not None:
                    return res
            except Exception as e:
                logger.debug("hybrid: local fetch_agent_by_id_with_context fell back: %s", e)
        return await self._remote.fetch_agent_by_id_with_context(agent_id, context_types, user_id)

    async def resolve_prompts(self, agent_id, user_id=None):
        if await self._ensure_local_agent(agent_id):
            try:
                return await self._local.resolve_prompts(agent_id, user_id=user_id)
            except Exception as e:
                logger.debug("hybrid: local resolve_prompts fell back: %s", e)
        return await self._remote.resolve_prompts(agent_id, user_id=user_id)

    async def assemble_prompt(self, agent_id, user_id=None):
        if await self._ensure_local_agent(agent_id):
            try:
                return await self._local.assemble_prompt(agent_id, user_id=user_id)
            except Exception as e:
                logger.debug("hybrid: local assemble_prompt fell back: %s", e)
        return await self._remote.assemble_prompt(agent_id, user_id=user_id)

    async def agent_data_source_list(self, agent_id: str, enabled_only: bool = False):
        """Serve attached source configuration from the local chat-start bundle
        after the normal remote freshness stamp validates it."""
        if await self._ensure_local_agent(agent_id):
            try:
                return await self._local.agent_data_source_list(agent_id, enabled_only=enabled_only)
            except Exception as e:
                logger.debug("hybrid: local agent data sources fell back: %s", e)
        return await self._remote.agent_data_source_list(agent_id, enabled_only=enabled_only)

    async def list_agents_for_user(self, user_id, include_admin=False, view="active"):
        """Serve the agents ROSTER (dropdown + Agents page) from the local mirror.

        The sync puller keeps the whole `agents` table mirrored locally, so this
        display list — NOT an authz decision — reads with no network round-trip.
        The same ownership/membership filtering runs on the identical local schema,
        so the per-user scoping is unchanged. Access ENFORCEMENT stays remote
        (is_user_admin / get_agent_roles / the Stage-3 authoritative user_mode read
        in chat._enforce_agent_access_policy), never this list.

        Two fallbacks to the remote authority: the 'clones' view needs the
        remote-only `agent_spawns` ledger, and an EMPTY local result (a device
        whose first puller tick hasn't landed yet) reads through to remote so a
        cold start still shows the roster."""
        if view != "clones":
            try:
                rows = await self._local.list_agents_for_user(
                    user_id, include_admin, view=view)
                if rows:
                    return rows
            except Exception as e:
                logger.debug("hybrid: local list_agents_for_user fell back: %s", e)
        return await self._remote.list_agents_for_user(
            user_id, include_admin, view=view)

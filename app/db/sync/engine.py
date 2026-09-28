"""The sync engine — pusher (local outbox → remote) + puller (remote → local).

Scope: the SYNCED tier only (shared content/config). Money and coordination are
written straight to Postgres by the hybrid backend and never appear here. Conflict
policy is last-writer-wins on the table's ``updated_at`` watermark, which is
acceptable for config-like rows that are edited rarely and from one active device
at a time (handoff §8).

This engine is deliberately backend-agnostic: it moves rows through the portable
raw-client query builder (``select/in_/gt/order/upsert``), so it works whether the
remote is real Postgres or (in tests) a second SQLite file.

STATUS: the mechanics are exercised by temp/test_hybrid.py against a two-SQLite
harness. Wiring ``start()`` into the app lifespan and flipping the hybrid's Synced
writes to local-first+enqueue is the ACTIVATION step — it needs a real Postgres
remote to validate end-to-end and is intentionally left off by default.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.db.sync.outbox import Outbox

logger = logging.getLogger(__name__)


@dataclass
class SyncedSpec:
    """How one Synced-tier table is moved between local and remote.

    ``key`` is the conflict/identity column; ``watermark`` is the monotonic
    column the puller compares against (usually ``updated_at``); ``fat_cols`` are
    columns whose value is stripped to NULL on the remote skeleton when it exceeds
    ``strip_over`` chars (the full copy stays in the local hot store); a small
    value at/under the gate rides along inline. ``push_only`` marks a table the
    engine only pushes local→remote and never pulls back (used for the
    ``interactions`` transcript, which has no ``updated_at`` watermark and whose
    reads are served by the hybrid's warm-on-open mirror, not the puller)."""
    table: str
    key: str = "id"
    watermark: str = "updated_at"
    fat_cols: Tuple[str, ...] = ()
    strip_over: int = 0
    push_only: bool = False


# The Synced tier (handoff §6). Extend as more tables are moved local-first; each
# addition is data, not new control flow.
#
# ``interactions`` is the transcript (Stage 2): written local-first + pushed as a
# SKELETON — oversized *tool-result content* is stripped to NULL on the remote
# (the full copy stays local). User and assistant text stays present for a useful
# cross-device handoff. It is push-only: the remote has no
# per-row ``updated_at`` to watermark on, and a second device gets the transcript
# through the hybrid's warm-on-open mirror instead of the puller.
#
# REMOTE-CONTROL CONTINUITY: ``output`` is now slimmed to a SKELETON (tool names
# + IDs only, flagged ``_remote_placeholder: true``) instead of full fat-strip.
# The skeleton preserves enough for the remote UI to show "N tool calls" headings,
# and the takeover history rebuild (session_history.py) detects the flag and
# skips the tool_calls + orphaned tool result rows gracefully. The full output
# (args, results, LLM output) stays in the local hot store. Tool results carry
# nearly all transcript bulk and are not needed to continue a handed-off chat;
# retaining their tool name/id while replacing a large body with a clear marker
# preserves both monitoring and history pairing. Pushing them in full also stalls
# the sync worker and, when a row carries a stray NUL, wedges the queue in a
# permanent retry.
from app.db.hybrid import _FAT_THRESHOLD_CHARS  # noqa: E402  (source of the gate)

SYNCED_SPECS: Dict[str, SyncedSpec] = {
    "interactions": SyncedSpec(
        "interactions", push_only=True),
    "sessions": SyncedSpec("sessions"),
    "session_summaries": SyncedSpec("session_summaries"),
    "session_summary_segments": SyncedSpec("session_summary_segments"),
    "genui": SyncedSpec("genui"),
    "webhook_registrations": SyncedSpec("webhook_registrations"),
    "agent_automations": SyncedSpec("agent_automations"),
    "agent_event_subscriptions": SyncedSpec("agent_event_subscriptions"),
    # ── Chat-panel read tables (mirrored so the panel reads them locally) ──
    # `agents` powers the agents dropdown + Agents page roster (served local-first
    # by HybridBackend.list_agents_for_user). Config AUTHORITY still lives on the
    # remote — the Stage-3 version-stamp governs the OPEN agent's prompt freshness;
    # this pull just keeps the display ROSTER local. `agent_prompts` is deliberately
    # NOT pulled: Stage-3 replaces its slots wholesale to propagate deletions, which
    # the upsert-only puller can't express.
    "agents": SyncedSpec("agents"),
    # `session_runs` gives the session-list its live run badges. Keyed by
    # session_id (no `id` column). Pulled at the fast 1s cadence while a turn is
    # actively pushing, so a running session's badge stays ~1s fresh on the mirror.
    "session_runs": SyncedSpec("session_runs", key="session_id"),
    # `session_notifications` carries the chat panel's undismissed "Session
    # complete" toasts across devices. Keyed by session_id; dismissal is a soft
    # `dismissed` flag (upsert-only puller can't express hard deletes), and old
    # dismissed rows are pruned locally on read. Remote table must mirror the
    # local columns (see the schema bootstrap DDL).
    "session_notifications": SyncedSpec("session_notifications", key="session_id"),
}


class SyncEngine:
    """Drains the local outbox to the remote authority and pulls remote changes
    back into the local replica. Operates on a :class:`HybridBackend`'s two halves.
    """

    def __init__(self, hybrid, specs: Optional[Dict[str, SyncedSpec]] = None,
                 interval_seconds: float = 5.0, pull_enabled: bool = True) -> None:
        self._hb = hybrid
        self._local = hybrid.local
        self._remote = hybrid.remote
        self._specs = specs if specs is not None else SYNCED_SPECS
        self._outbox = Outbox(self._local)
        self._interval = interval_seconds
        # Remote Control live-stream cadence: while rows are actively flowing (a
        # live turn — most importantly one running on THIS device that a viewer on
        # ANOTHER device has open and is polling), the pusher runs at this faster
        # interval so the streaming assistant row reaches the shared DB in ~1s and
        # the viewer's reconcile poll renders it near-live instead of in 5s steps.
        # It relaxes back to _interval once the flow stops (see _run).
        self._fast_interval = 1.0
        self._active_grace = 3.0  # stay fast this long after the last non-empty push
        # When False the loop only PUSHES the local outbox to the remote and never
        # pulls remote→local. Stage 2 runs push-only; the puller is turned on in
        # Stage 4 after the "no authz/identity/billing read resolves from local"
        # security audit (handoff §6). Direct pull_table() calls still work (tests).
        self._pull_enabled = pull_enabled
        self._watermarks: Dict[str, Optional[str]] = {}
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        # ── Adaptive per-table pull backoff (idle round-trip suppression) ──
        # Each pull is a remote round-trip even when nothing has changed. A table
        # that returns nothing for several consecutive ticks is "quiet" — usually
        # permanently empty (genui/webhooks/automations here had NO local watermark
        # so they did an unfiltered `SELECT * … LIMIT 500` full scan EVERY tick
        # forever) or simply unchanged. Once quiet AND the engine is idle (no rows
        # flowing), we re-check that table only every _quiet_interval seconds
        # instead of every tick, cutting the steady-state background round-trip
        # tax from ~10/tick to ~1-2. The backoff is driven by "did this pull return
        # rows", so ANY table with real cross-device activity resets to eager on
        # its own (e.g. a viewer's session_runs stays live while a remote turn
        # streams, because each pull keeps returning the advancing run row). While
        # ACTIVE (a live local turn is pushing) the backoff is bypassed entirely.
        self._quiet: Dict[str, int] = {}          # consecutive empty pulls / table
        self._next_pull: Dict[str, float] = {}    # monotonic time a quiet table is due
        try:
            self._quiet_after = max(1, int(os.environ.get("WEBAGENT_SYNC_QUIET_AFTER") or 3))
        except ValueError:
            self._quiet_after = 3
        try:
            self._quiet_interval = max(
                1.0, float(os.environ.get("WEBAGENT_SYNC_QUIET_SECONDS") or 15.0))
        except ValueError:
            self._quiet_interval = 15.0
        # Hold the first full sweep briefly after start so it doesn't compete with
        # the very first page-load API burst right after a (re)start. 0 disables.
        try:
            self._startup_delay = max(0.0, float(
                os.environ.get("WEBAGENT_SYNC_STARTUP_DELAY") or 5.0))
        except ValueError:
            self._startup_delay = 5.0

    @property
    def outbox(self) -> Outbox:
        return self._outbox

    # ── Pusher: local outbox → remote ────────────────────────────────────────

    async def push_pending(self, batch: int = 500) -> int:
        """Flush the outbox to the remote authority. Returns rows pushed.

        Entries are grouped by table, the CURRENT row is re-read from local (so
        repeated edits coalesce), fat columns are stripped, and each row is
        upserted on its key. An entry whose LATEST op for its key is ``delete``
        is instead propagated as a remote DELETE (the local row is already gone).
        Only entries that push cleanly are cleared, so a remote outage simply
        leaves them queued for the next pass."""
        pending = await self._outbox.pending(batch)
        if not pending:
            return 0

        # Preserve arrival order but de-dup (table,row_id) to the LATEST op. A
        # delete enqueued after upserts (rename-then-delete) wins → the row is
        # removed remotely, not resurrected.
        upserts_by_table: Dict[str, List[str]] = {}
        deletes_by_table: Dict[str, List[str]] = {}
        latest_op: Dict[Tuple[str, str], str] = {}
        seqs_by_key: Dict[Tuple[str, str], List[int]] = {}
        for e in pending:
            t, rid, seq = e["table_name"], e["row_id"], e["seq"]
            seqs_by_key.setdefault((t, rid), []).append(seq)
            latest_op[(t, rid)] = (e.get("op") or "upsert")
        for (t, rid), op in latest_op.items():
            bucket = deletes_by_table if op == "delete" else upserts_by_table
            ids = bucket.setdefault(t, [])
            if rid not in ids:
                ids.append(rid)

        pushed = 0
        cleared: List[int] = []

        # ── Resurrection guard ──
        # Never re-push an upsert for a row that has been HARD-deleted (tombstoned)
        # on the authority. A queued sessions/agents upsert on THIS device — e.g. a
        # mark-read or rename that raced a permanent delete on ANOTHER device — would
        # otherwise re-create the row remotely, and the delete would bounce back to
        # every device (the classic delete-vs-update resurrection). We check the
        # visible tables only: interactions self-heal because the tombstone's local
        # cascade removes their rows, leaving nothing to push.
        for _t in ("sessions", "agents"):
            _ids = upserts_by_table.get(_t)
            if not _ids:
                continue
            try:
                dead = await asyncio.to_thread(self._remote_tombstoned, _t, _ids)
            except Exception:  # noqa: BLE001
                dead = set()
            if dead:
                upserts_by_table[_t] = [i for i in _ids if i not in dead]
                for rid in dead:
                    cleared.extend(seqs_by_key.get((_t, rid), []))
                logger.info("sync: suppressed %d resurrecting upsert(s) to %r "
                            "(row was hard-deleted on the authority)", len(dead), _t)

        # ── Deletes: issue a remote DELETE per id (no local row to re-read) ──
        for table, ids in deletes_by_table.items():
            spec = self._specs.get(table)
            if not spec:
                for rid in ids:
                    cleared.extend(seqs_by_key.get((table, rid), []))
                continue
            for rid in ids:
                try:
                    await asyncio.to_thread(self._delete_row, table, spec, rid)
                    pushed += 1
                    cleared.extend(seqs_by_key.get((table, rid), []))
                except Exception as e:
                    logger.warning("sync: delete of %r row %r failed (%s); leaving queued",
                                   table, rid, e)

        # ── Upserts: re-read the current local row(s) and push ──
        for table, ids in upserts_by_table.items():
            spec = self._specs.get(table)
            if not spec:
                # Unknown table — drop the marker so it doesn't wedge the queue.
                for rid in ids:
                    cleared.extend(seqs_by_key.get((table, rid), []))
                logger.warning("sync: no spec for table %r; dropping %d outbox entries",
                               table, len(ids))
                continue
            try:
                rows = self._local_rows(table, spec.key, ids)
            except Exception as e:
                # Reading the local batch failed as a whole — transient; retry next tick.
                logger.warning("sync: read of table %r failed (%s); leaving queued", table, e)
                continue
            # Push each row on its own so one unpushable row can't wedge the batch.
            present = set()
            for row in rows:
                rid = str(row.get(spec.key))
                present.add(rid)
                try:
                    # The remote upsert is a synchronous network round-trip; run it
                    # on a worker thread so a slow remote can't freeze the main event
                    # loop (which also serves every HTTP request + the LLM stream).
                    await asyncio.to_thread(self._push_row, table, spec, row)
                    pushed += 1
                    cleared.extend(seqs_by_key.get((table, rid), []))
                except Exception as e:
                    # Leave this one row queued (a transient remote error will
                    # succeed on a later tick); the rest of the batch still clears,
                    # so a single stuck row no longer starves the whole queue.
                    logger.warning("sync: push of %r row %r failed (%s); leaving row queued",
                                   table, rid, e)
            # Ids enqueued but already gone from local — clear their stale markers.
            for rid in ids:
                if rid not in present:
                    cleared.extend(seqs_by_key.get((table, rid), []))

        await self._outbox.clear(cleared)
        return pushed

    def _local_rows(self, table: str, key: str, ids: List[str]) -> List[dict]:
        res = self._local.get_raw_client().table(table).select("*").in_(key, ids).execute()
        return list(res.data or [])

    def _delete_row(self, table: str, spec: SyncedSpec, rid: str) -> None:
        """Propagate a local hard-delete to the remote authority (portable
        DELETE … WHERE key = rid). Runs on a worker thread via push_pending."""
        client = self._remote.get_raw_client()
        if table == "sessions":
            # A session owns these records even when optional plugin tables are
            # present only on some devices. Missing optional tables cannot block
            # the core remote session erase.
            for dependent in ("interactions", "session_summaries", "session_summary_segments",
                              "pipeline_events", "session_runs", "messages", "browser_sessions",
                              "session_notifications"):
                try:
                    client.table(dependent).delete().eq("session_id", rid).execute()
                except Exception:
                    pass
            for column in ("orchestrator_session_id", "spawn_session_id"):
                try:
                    client.table("agent_spawns").delete().eq(column, rid).execute()
                except Exception:
                    pass
        client.table(table).delete().eq(spec.key, rid).execute()
        if table == "sessions":
            # This retry reached the authority. Publish the tombstone now (not
            # before), so peers can never hide a session the remote still has.
            from app.db.sync.tombstones import TOMBSTONE_TABLE, ensure_tombstone_table
            conn = self._remote._get_conn()
            try:
                ensure_tombstone_table(conn)
                conn.commit()
            finally:
                conn.close()
            from datetime import datetime, timezone
            client.table(TOMBSTONE_TABLE).upsert({
                "id": f"sessions:{rid}", "table_name": "sessions", "row_id": rid,
                "user_id": None, "deleted_at": datetime.now(timezone.utc).isoformat(),
            }, on_conflict="id").execute()

    def _remote_tombstoned(self, table: str, ids: List[str]) -> set:
        """Return the subset of ``ids`` that carry a hard-delete tombstone on the
        authority (so an upsert of them would resurrect a deleted row). One bounded
        ``id IN (...)`` lookup; empty set on any error / missing table."""
        from app.db.sync.tombstones import TOMBSTONE_TABLE
        want = [f"{table}:{i}" for i in ids]
        try:
            res = (self._remote.get_raw_client().table(TOMBSTONE_TABLE)
                   .select("id").in_("id", want).execute())
            got = {r.get("id") for r in (res.data or [])}
        except Exception:  # noqa: BLE001
            return set()
        return {i for i in ids if f"{table}:{i}" in got}

    def _push_row(self, table: str, spec: SyncedSpec, row: dict) -> None:
        payload = dict(row)
        # ``input`` was a legacy local-only interactions column.  Some existing
        # SQLite replicas still carry it, but the shared Postgres schema removed
        # it; never let an old local row make the remote transcript upsert fail.
        if table == "interactions":
            payload.pop("input", None)
        for col in spec.fat_cols:
            val = payload.get(col)
            if val is None:
                continue
            # Over the split gate → strip to NULL on the remote skeleton (the full
            # value stays in the local hot store). A small value rides along inline
            # so cross-device readers and JSON consumers still see it.
            if spec.strip_over <= 0 or len(str(val)) > spec.strip_over:
                payload[col] = None
        # PostgreSQL text columns reject NUL (0x00) bytes outright, so a single
        # transcript row carrying a stray null (garbled tool output, a bad paste)
        # would otherwise fail forever and wedge this table's push queue. Strip
        # NUL from every text value — the readable content is preserved and the
        # full copy still lives in the local hot store.
        for _col, _v in payload.items():
            if isinstance(_v, str) and "\x00" in _v:
                payload[_col] = _v.replace("\x00", "")
        # Tool result bodies are local-heavy data. Keep a small, explicit marker
        # remotely: a monitoring device can explain the gap and a new owner will
        # not try to replay a missing tool result as if it were complete context.
        if table == "interactions" and payload.get("role") == "tool":
            # Tool execution detail never needs to leave the originating device.
            # Retain the row's structural fields (tool_name, call id, ordering and
            # status) but replace every body with one explicit handoff marker.
            payload["content"] = "[tool call processed on local device; recall unavailable remotely]"
            payload["output"] = None
            payload["metadata"] = json.dumps({
                "remote_placeholder": True,
                "local_payload": "tool_execution",
            }, separators=(",", ":"))
        # ── Slim interactions output to skeleton (tool names + IDs only) ──────────
        # The `output` column on assistant rows carries full tool arguments, results
        # and LLM output — potentially megabytes per turn. Push a minimal skeleton
        # with just tool names + IDs + a `_remote_placeholder` flag so the remote UI
        # can still show "N tool calls" headings and the takeover history rebuild
        # can gracefully skip the missing data instead of rebuilding broken tool
        # pairs. The full output stays in the local hot store.
        if table == "interactions" and payload.get("role") == "assistant":
            _output_raw = payload.get("output")
            if _output_raw and isinstance(_output_raw, str):
                try:
                    _out_parsed = json.loads(_output_raw)
                    _tc_list = _out_parsed.get("tool_calls")
                    if _tc_list and isinstance(_tc_list, list) and len(_tc_list) > 0:
                        # Tool arguments are device-local execution detail, even
                        # when short.  The remote transcript must never receive
                        # them: it only carries names/ids to preserve UI grouping
                        # and hand-off history shape.
                        payload["output"] = json.dumps({
                            "_remote_placeholder": True,
                            "local_payload": "tool_calls",
                            "tool_calls_processed_locally": len(_tc_list),
                        }, separators=(",", ":"))
                except (json.JSONDecodeError, TypeError, AttributeError):
                    pass  # leave output as-is if unparseable
        self._remote.get_raw_client().table(table).upsert(payload, on_conflict=spec.key).execute()

    # ── Puller: remote → local ───────────────────────────────────────────────

    def _local_max_watermark(self, spec: SyncedSpec) -> Optional[str]:
        conn = self._local._get_conn()
        try:
            row = conn.execute(
                f"SELECT MAX({spec.watermark}) FROM {spec.table}"
            ).fetchone()
            return row[0] if row and row[0] is not None else None
        except Exception:
            return None
        finally:
            conn.close()

    async def pull_table(self, spec: SyncedSpec, limit: int = 500) -> int:
        """Pull rows the remote has that are newer than our watermark into local.

        Returns the number of rows applied. Idempotent: re-applying a row we
        already have (or just pushed) is a harmless last-writer-wins upsert."""
        wm = self._watermarks.get(spec.table)
        if wm is None:
            wm = self._local_max_watermark(spec)

        q = self._remote.get_raw_client().table(spec.table).select("*")
        if wm is not None:
            q = q.gt(spec.watermark, wm)
        q = q.order(spec.watermark, desc=False).limit(limit)
        try:
            # Remote fetch is a synchronous round-trip — run it off the main event
            # loop so the per-tick pull (one per synced table) can't stall requests.
            res = await asyncio.to_thread(q.execute)
            remote_rows = list(res.data or [])
        except Exception as e:
            logger.warning("sync: pull of table %r failed: %s", spec.table, e)
            return 0

        applied = 0
        newest = wm
        for row in remote_rows:
            try:
                # Drop NULL-valued keys so LOCAL NOT-NULL defaults fill columns the
                # remote left null (e.g. sessions.hidden / sort_order) — an explicit
                # NULL would trip the local constraint and silently fail the row.
                # Same policy the hybrid's warm-on-open mirror uses.
                clean = {k: v for k, v in row.items() if v is not None}
                self._local.get_raw_client().table(spec.table).upsert(
                    clean, on_conflict=spec.key
                ).execute()
                applied += 1
                w = row.get(spec.watermark)
                if w is not None and (newest is None or str(w) > str(newest)):
                    newest = w
            except Exception as e:
                logger.debug("sync: applying pulled row to %r failed: %s", spec.table, e)
        if newest is not None:
            self._watermarks[spec.table] = newest
        return applied

    # ── Tombstone puller: carry HARD deletes remote → local ──────────────────

    def _apply_tombstone_local(self, conn, table_name: str, row_id: str) -> List[str]:
        """Apply one hard-delete tombstone to the LOCAL mirror. A ``sessions`` or
        ``agents`` tombstone cascades to its attached transcript locally (so we
        never need a tombstone per interaction row); anything else is a plain
        single-row delete by ``id``. Best-effort per statement — a missing table
        (feature never used on this device) is skipped, not fatal.

        Returns the list of SESSION ids this erased, so the caller can tell any
        open chat on those sessions to drop its transcript in real time (an agent
        delete cascades to several)."""
        def _try(sql, params):
            try:
                conn.execute(sql, params)
            except Exception:  # noqa: BLE001
                pass
        if table_name == "sessions":
            _try("DELETE FROM interactions WHERE session_id = ?", (row_id,))
            for t in ("session_summaries", "session_summary_segments", "pipeline_events",
                      "session_runs", "messages", "browser_sessions", "session_notifications",
                      "run_contract_checks", "run_contract_state"):
                _try(f"DELETE FROM {t} WHERE session_id = ?", (row_id,))
            _try("DELETE FROM agent_spawns WHERE orchestrator_session_id = ?", (row_id,))
            _try("DELETE FROM agent_spawns WHERE spawn_session_id = ?", (row_id,))
            _try("DELETE FROM sessions WHERE id = ?", (row_id,))
            return [row_id]
        elif table_name == "agents":
            # Remove the agent AND everything hanging off it — its sessions, their
            # transcripts, and its agent-scoped config — mirroring the authority's
            # delete_custom_agent so no phantom session is left pointing at a
            # now-deleted agent.
            try:
                sids = [r[0] for r in conn.execute(
                    "SELECT id FROM sessions WHERE agent_id = ?", (row_id,)).fetchall()]
            except Exception:  # noqa: BLE001
                sids = []
            for sid in sids:
                _try("DELETE FROM interactions WHERE session_id = ?", (sid,))
                for t in ("session_summaries", "pipeline_events", "session_runs",
                          "session_notifications", "run_contract_checks",
                          "run_contract_state"):
                    _try(f"DELETE FROM {t} WHERE session_id = ?", (sid,))
            _try("DELETE FROM sessions WHERE agent_id = ?", (row_id,))
            for t in ("agent_prompts", "agent_connections", "agent_abilities", "agent_soft_abilities", "soft_ability_runs",
                      "agent_automations", "agent_event_subscriptions"):
                _try(f"DELETE FROM {t} WHERE agent_id = ?", (row_id,))
            _try("DELETE FROM agents WHERE id = ?", (row_id,))
            return sids
        else:
            _try(f"DELETE FROM {table_name} WHERE id = ?", (row_id,))
            return []

    async def pull_tombstones(self, limit: int = 500) -> int:
        """Pull hard-delete tombstones the remote has that are newer than our
        watermark and apply each as a local delete. Mirrors ``pull_table`` (same
        ``deleted_at`` high-water mark), and keeps a LOCAL copy of every applied
        tombstone so the watermark survives a restart and we don't re-scan the
        whole ledger each boot. Idempotent: re-applying a delete for a row that is
        already gone locally is a harmless no-op."""
        from app.db.sync.tombstones import TOMBSTONE_TABLE, ensure_tombstone_table

        # Local high-water mark (max deleted_at we've already applied).
        wm = self._watermarks.get(TOMBSTONE_TABLE)
        if wm is None:
            lconn = self._local._get_conn()
            try:
                ensure_tombstone_table(lconn)
                lconn.commit()
                row = lconn.execute(
                    f"SELECT MAX(deleted_at) FROM {TOMBSTONE_TABLE}").fetchone()
                wm = row[0] if row and row[0] is not None else None
            except Exception:  # noqa: BLE001
                wm = None
            finally:
                lconn.close()

        q = self._remote.get_raw_client().table(TOMBSTONE_TABLE).select("*")
        if wm is not None:
            q = q.gt("deleted_at", wm)
        q = q.order("deleted_at", desc=False).limit(limit)
        try:
            res = await asyncio.to_thread(q.execute)
            tombstones = list(res.data or [])
        except Exception as e:
            # Table absent (no hard delete ever recorded) or a transient remote
            # error — nothing to apply this tick.
            logger.debug("sync: tombstone pull skipped (%s)", e)
            return 0
        if not tombstones:
            return 0

        applied = 0
        newest = wm
        # (user_id, table_name, row_id, [affected_session_ids])
        notify: List[Tuple[str, str, str, List[str]]] = []
        async with self._local._write_lock:
            conn = self._local._get_conn()
            try:
                ensure_tombstone_table(conn)
                for ts in tombstones:
                    table_name = ts.get("table_name")
                    row_id = ts.get("row_id")
                    if not table_name or row_id is None:
                        continue
                    rid = str(row_id)
                    affected_sids = self._apply_tombstone_local(conn, table_name, rid)
                    # Drop any queued push for this id so we never re-send an upsert
                    # that would resurrect it on the authority (belt-and-suspenders
                    # with the push-time resurrection guard).
                    try:
                        conn.execute(
                            "DELETE FROM hybrid_outbox WHERE table_name = ? AND row_id = ?",
                            (table_name, rid))
                    except Exception:  # noqa: BLE001
                        pass
                    # Record locally so this tombstone advances our watermark and
                    # is never re-applied after a restart.
                    try:
                        conn.execute(
                            f"INSERT OR REPLACE INTO {TOMBSTONE_TABLE} "
                            "(id, table_name, row_id, user_id, deleted_at) "
                            "VALUES (?, ?, ?, ?, ?)",
                            (ts.get("id") or f"{table_name}:{rid}", table_name,
                             rid, ts.get("user_id"), ts.get("deleted_at")),
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    if ts.get("user_id"):
                        notify.append((ts["user_id"], table_name, rid, affected_sids))
                    applied += 1
                    d = ts.get("deleted_at")
                    if d is not None and (newest is None or str(d) > str(newest)):
                        newest = d
                conn.commit()
            finally:
                conn.close()
        if newest is not None:
            self._watermarks[TOMBSTONE_TABLE] = newest
        # Tell any open chat on THIS device that its session/agent was erased
        # elsewhere, so it can drop the transcript and show "Session not found"
        # in real time instead of waiting for a failed reconcile. Best-effort.
        for user_id, table_name, rid, affected_sids in notify:
            try:
                from app.api.chat import notify_user
                # An open chat drops its transcript on session_deleted. Emit one per
                # erased session — for a session tombstone that's the row itself; for
                # an agent tombstone it's each of the agent's now-deleted sessions.
                for sid in (affected_sids or ([rid] if table_name == "sessions" else [])):
                    await notify_user(user_id, {
                        "type": "session_deleted", "user_id": user_id,
                        "session_id": sid, "permanent": True, "reason": "remote_delete"})
                if table_name == "agents":
                    await notify_user(user_id, {
                        "type": "agent_deleted", "user_id": user_id,
                        "agent_id": rid, "permanent": True, "reason": "remote_delete"})
            except Exception:  # noqa: BLE001
                pass
        if applied:
            logger.info("sync: applied %d hard-delete tombstone(s) to local mirror", applied)
        return applied

    # ── Combined tick + background loop ──────────────────────────────────────

    async def sync_once(self, active: bool = True) -> dict:
        """One push+pull tick. ``active`` = rows are actively flowing (a live turn
        is pushing); when False, quiet tables are pulled on the backoff cadence
        instead of every tick. Defaults True so direct/test calls always pull
        everything."""
        pushed = await self.push_pending()
        pulled = 0
        tombstoned = 0
        if self._pull_enabled:
            now = time.monotonic()
            for spec in self._specs.values():
                if spec.push_only:
                    continue  # transcript etc. — pushed, never pulled
                # Skip a quiet table this tick unless it's due or we're active.
                if (not active
                        and self._quiet.get(spec.table, 0) >= self._quiet_after
                        and now < self._next_pull.get(spec.table, 0.0)):
                    continue
                n = await self.pull_table(spec)
                pulled += n
                if n > 0:
                    # Real change → back to eager (this table matters right now).
                    self._quiet[spec.table] = 0
                    self._next_pull[spec.table] = 0.0
                else:
                    self._quiet[spec.table] = self._quiet.get(spec.table, 0) + 1
                    if self._quiet[spec.table] >= self._quiet_after:
                        self._next_pull[spec.table] = now + self._quiet_interval
            # Apply hard-delete tombstones AFTER the upsert pulls, so a delete that
            # lands in the same tick as a stale upsert wins (the row is removed, not
            # re-added). Pulled EVERY tick (one cheap watermarked round-trip) — a
            # permanent delete on another device should clear the ghost row here
            # promptly, not wait out the backoff.
            tombstoned = await self.pull_tombstones()
        return {"pushed": pushed, "pulled": pulled, "tombstoned": tombstoned}

    async def _run(self) -> None:
        logger.info("hybrid sync engine started (interval=%.1fs, fast=%.1fs, "
                    "startup_delay=%.1fs)",
                    self._interval, self._fast_interval, self._startup_delay)
        # Startup grace: let the first client's page-load API burst clear before
        # the first full pull sweep competes for the connection pool.
        if self._startup_delay > 0:
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._startup_delay)
            except asyncio.TimeoutError:
                pass
        active_until = 0.0
        while not self._stop.is_set():
            # Whether THIS tick is "active" is decided by recent pushes (set below
            # from prior ticks). On the first tick active_until=0 → not active, but
            # quiet counters are 0 so every table still pulls (full first sweep).
            now = time.monotonic()
            active = now < active_until
            result = None
            try:
                result = await self.sync_once(active=active)
            except Exception as e:  # never let one bad tick kill the loop
                logger.warning("sync tick failed: %s", e)
            # Adaptive cadence: a non-empty push means rows are actively flowing —
            # stay on the fast interval for a short grace window so a live turn
            # (e.g. a Remote Control run streaming here while a viewer polls from
            # another device) syncs near-live. Idle ticks relax to the base rate.
            now = time.monotonic()
            if result and result.get("pushed"):
                active_until = now + self._active_grace
            interval = self._fast_interval if now < active_until else self._interval
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
        logger.info("hybrid sync engine stopped")

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await self._task
            except Exception:
                pass
            self._task = None

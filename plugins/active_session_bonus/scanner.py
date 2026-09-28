"""Plugin-owned reconciliation worker for offer detection and settlement."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from app.runtime_mode import data_root
from .config import DEFAULTS
from .policies import policy_for_flag
from .qualification import qualifies, task_snapshot
from .store import BonusStore, get_store
from .wallet_adapter import credit_bonus

logger = logging.getLogger(__name__)


def _user_db_paths():
    root = data_root() / "user_data"
    return root.glob("*/*.db") if root.exists() else ()


def _snapshot(conn, session_id: str) -> dict | None:
    row = conn.execute("SELECT state_json FROM agent_task_state WHERE session_id=?", (session_id,)).fetchone()
    if not row:
        return None
    try:
        state = json.loads(row[0])
    except (TypeError, json.JSONDecodeError):
        return None
    return task_snapshot(state) if isinstance(state, dict) else None


def _closers(conn):
    """Latest closers whose flag has a valid live policy."""
    rows = conn.execute("""SELECT i.id, i.session_id, s.user_id, i.metadata
        FROM interactions i JOIN sessions s ON s.id=i.session_id
        WHERE i.role='assistant' AND i.metadata LIKE '%\"closing_message\"%'
          AND (i.status IS NULL OR i.status != 'deleted')
          AND julianday(i.created_at) >= julianday('now', '-1 day')
          AND NOT EXISTS (
              SELECT 1 FROM interactions newer
              WHERE newer.session_id=i.session_id AND newer.role='assistant'
                AND newer.metadata LIKE '%\"closing_message\"%'
                AND (newer.status IS NULL OR newer.status != 'deleted')
                AND newer.session_seq > i.session_seq
          )
        ORDER BY i.session_seq""").fetchall()
    for row in rows:
        try:
            meta = json.loads(row[3] or "{}")
            closing = meta.get("closing_message") if isinstance(meta, dict) else None
        except (TypeError, json.JSONDecodeError):
            closing = None
        flag = str((closing or {}).get("flag") or "").strip().lower()
        policy = policy_for_flag(flag)
        if policy:
            yield {"id": row[0], "session_id": row[1], "user_id": row[2], "policy": policy}


def _first_reply(conn, offer, *, from_source_closer: bool = False):
    """Return the first qualifying reply after activation or the source closer.

    A continue policy promises a reward for resolving the closing message.  The
    scanner can discover that closer just after the user replies, so that policy
    must use the closer itself—not delayed browser activation—as its boundary.
    """
    boundary = offer["activated_at"]
    if from_source_closer:
        row = conn.execute("SELECT created_at FROM interactions WHERE id=?", (offer["source_closer_interaction_id"],)).fetchone()
        if row and row[0]:
            boundary = row[0]
    return conn.execute("""SELECT id FROM interactions
        WHERE session_id=? AND role='user' AND trim(content) != ''
          AND (source IS NULL OR source IN ('user','web','chat','telegram','whatsapp'))
          AND julianday(created_at) > julianday(?)
        ORDER BY session_seq ASC LIMIT 1""", (offer["session_id"], boundary)).fetchone()


def _run_complete(conn, session_id: str) -> bool:
    row = conn.execute("SELECT status FROM session_runs WHERE session_id=?", (session_id,)).fetchone()
    return bool(row and str(row[0]).lower() not in {"running", "queued", "resuming"})


class BonusWorker:
    def __init__(self, store: BonusStore | None = None):
        self.store = store or get_store()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self._leader_lock = None

    def _acquire_leader_lock(self) -> bool:
        """Elect one process to scan the shared development data directory.

        Uvicorn uses multiple worker processes.  Starting a scanner in every
        worker multiplied a full authority-database sweep by the worker count
        every few seconds.  An advisory lock is released automatically if its
        owner exits, so another worker takes over after a restart.
        """
        try:
            import fcntl
            lock_path = data_root() / ".active_session_bonus.lock"
            handle = lock_path.open("a+")
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            try:
                handle.close()
            except UnboundLocalError:
                pass
            return False
        self._leader_lock = handle
        return True

    def _release_leader_lock(self) -> None:
        handle, self._leader_lock = self._leader_lock, None
        if handle is None:
            return
        try:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except (OSError, ImportError):
            pass
        finally:
            handle.close()

    async def start(self):
        if self._task and not self._task.done():
            return
        if not self._acquire_leader_lock():
            logger.info("Active Session Bonus worker standby; another worker is leader")
            return
        self._stopping.clear()
        self._task = asyncio.create_task(self._run(), name="active-session-bonus")

    async def stop(self):
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        self._release_leader_lock()

    async def _run(self):
        while not self._stopping.is_set():
            try:
                await self.reconcile_once()
            except Exception:
                logger.exception("Active Session Bonus reconciliation failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=DEFAULTS.reconcile_seconds)
            except asyncio.TimeoutError:
                pass

    async def reconcile_once(self):
        self.store.expire_due()
        for path in _user_db_paths():
            await self._scan_user_db(path)
        await self._settle_candidates()

    async def _scan_user_db(self, path: Path):
        import sqlite3
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                for closer in _closers(conn):
                    snapshot = _snapshot(conn, closer["session_id"])
                    if snapshot:
                        self.store.create_offer(user_id=closer["user_id"], session_id=closer["session_id"], closer_id=closer["id"], snapshot=snapshot, policy=closer["policy"])
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            logger.debug("Active Session Bonus skipped unreadable user authority %s", path)

    async def _settle_candidates(self):
        import sqlite3
        for offer in self.store.active_offers():
            path = data_root() / "user_data" / offer["user_id"] / f"{offer['user_id']}.db"
            if not path.exists():
                continue
            try:
                conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                try:
                    current = _snapshot(conn, offer["session_id"])
                    if not current or current.get("task_id") != offer["task_id"]:
                        self.store.terminalize(offer["id"], "superseded", "task changed")
                        continue
                    action = (offer.get("policy") or {}).get("resolution_action", "legacy")
                    if offer["status"] == "activated" and action == "open_reset":
                        offer = self.store.reserve_claim(offer["id"])
                    elif offer["status"] == "activated":
                        reply = _first_reply(conn, offer, from_source_closer=action == "continue")
                        if reply:
                            self.store.mark_candidate(offer["id"], reply[0])
                            offer = self.store.offer(offer["id"], offer["user_id"]) or offer
                    if offer and offer["status"] == "awaiting_progress":
                        if action == "continue":
                            offer = self.store.reserve_claim(offer["id"])
                        elif action == "complete":
                            if current.get("task_status") == "completed" and int(current.get("work_revision") or 0) > int(offer.get("baseline_work_revision") or 0):
                                offer = self.store.reserve_claim(offer["id"])
                        else:  # Historical offers retain the original eligibility contract.
                            if not _run_complete(conn, offer["session_id"]):
                                continue
                            if not qualifies(offer, current):
                                self.store.terminalize(offer["id"], "rejected", "reply did not produce verified task progress")
                                continue
                            offer = self.store.reserve_claim(offer["id"])
                    if offer and offer["status"] == "qualified":
                        await credit_bonus(offer["user_id"], offer["id"], int(offer["claimed_amount"]))
                        self.store.mark_claimed(offer["id"])
                finally:
                    conn.close()
            except (sqlite3.Error, OSError):
                logger.debug("Active Session Bonus skipped unavailable authority for offer %s", offer["id"])


_WORKER: BonusWorker | None = None

def get_worker() -> BonusWorker:
    global _WORKER
    if _WORKER is None:
        _WORKER = BonusWorker()
    return _WORKER

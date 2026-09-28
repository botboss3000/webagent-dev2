"""P2P worker — background loop that keeps ``data/`` in sync with peers.

Runs alongside the device worker (started/stopped in ``app/main.py``).  Each tick:

  1. Heartbeats every peer (GET /status) to update online/offline status.
  2. On a configurable interval (default 60s), fetches the peer's manifest and
     pulls any changed files.
  3. A ``kick()`` event wakes the loop immediately for on-demand sync.

No leader election — every instance runs its own worker independently.  If two
instances are peered, each pulls from the other and converges.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Dict, List, Optional

from app.p2p import identity
from app.p2p import store as peer_store
from app.p2p.manifest import build_classified_manifest, diff_classified_manifest
from app.p2p.sync import _write_received_file, pull_files
from app.p2p.transport.http_transport import HttpTransport
from app.p2p.vault_sync import build_vault_row_manifest, diff_vault_rows, apply_vault_rows

logger = logging.getLogger(__name__)

HEARTBEAT_EVERY = 30   # seconds between peer health checks
SYNC_EVERY = 60        # seconds between full sync cycles
HTTP_TIMEOUT = 30.0    # per-request timeout


class P2PWorker:
    def __init__(self) -> None:
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._kick = asyncio.Event()
        self._last_heartbeat = 0.0
        self._last_sync = 0.0
        # Track per-peer last sync so fresh peers sync immediately
        self._peer_last_sync: Dict[str, float] = {}
        self._transport = HttpTransport()

    async def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="p2p_worker")
        logger.info("P2P worker started (id=%s)", identity.instance_id())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._task = None

    def kick(self) -> None:
        """Wake the loop immediately (for on-demand sync)."""
        try:
            self._kick.set()
        except Exception:
            pass

    async def _run(self) -> None:
        await asyncio.sleep(3)  # let startup settle
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception("P2P worker tick failed: %s", e)

            try:
                await asyncio.wait_for(self._kick.wait(), timeout=self._next_wake())
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                raise
            finally:
                self._kick.clear()

    def _next_wake(self) -> float:
        """Figure out how long to sleep: the soonest of heartbeat / sync deadlines."""
        now = time.monotonic()
        to_heartbeat = max(0, HEARTBEAT_EVERY - (now - self._last_heartbeat))
        to_sync = max(0, SYNC_EVERY - (now - self._last_sync))
        return min(to_heartbeat, to_sync) + 0.5

    async def _tick(self) -> None:
        now = time.monotonic()
        # Path.mkdir/glob/read_text can pause for seconds on Windows when the
        # workspace is busy or under antivirus inspection. Never run that file
        # walk on the server event loop.
        peers = await asyncio.to_thread(peer_store.list_peers)

        if not peers:
            # With both timestamps left at zero, _next_wake() returned 0.5s and
            # rescanned the directory forever despite P2P being unconfigured.
            self._last_heartbeat = now
            self._last_sync = now
            return

        # 1. Heartbeat (throttled)
        if self._last_heartbeat == 0.0 or (now - self._last_heartbeat) >= HEARTBEAT_EVERY:
            await self._heartbeat_peers(peers)
            self._last_heartbeat = now

        # 2. Sync (throttled)
        if self._last_sync == 0.0 or (now - self._last_sync) >= SYNC_EVERY:
            await self._sync_peers(peers)
            self._last_sync = time.monotonic()

    # ── Heartbeat ───────────────────────────────────────────────────────────

    async def _heartbeat_peers(self, peers: List[Dict]) -> None:
        for peer in peers:
            try:
                await self._heartbeat_one(peer)
            except Exception as e:
                logger.debug("Heartbeat to %s failed: %s", peer.get("name"), e)

    async def _heartbeat_one(self, peer: Dict) -> None:
        peer_id = peer["id"]
        try:
            response_bytes = await self._transport.send(peer_id, "GET", "/api/v1/p2p/status", b"")
            data = json.loads(response_bytes.decode())
            online = True
            remote_id = data.get("instance_id", "")
            if remote_id and remote_id != peer.get("remote_instance_id", ""):
                peer_store.set_sync_state(peer_id, remote_instance_id=remote_id)
        except Exception:
            online = False

        peer_store.set_sync_state(peer_id, status="online" if online else "offline")

    # ── Sync ────────────────────────────────────────────────────────────────

    async def _sync_peers(self, peers: List[Dict]) -> None:
        # build_classified_manifest walks ALL of data/, fnmatches every file
        # against ~65 globs, and SHA256-reads every full-tier file (wiki.db,
        # agent_data DBs, genui). On Windows that can block the event loop for
        # seconds to tens of seconds — freezing sends, the run loop, and the
        # watchdog heartbeat. Offload it exactly like list_peers above.
        local_classified = await asyncio.to_thread(build_classified_manifest)
        for peer in peers:
            # A New Instance created from this node is a source-authoritative
            # replica. Push only its selected portable app rows, re-encrypted
            # secrets and safe JSON configs; never swap a live SQLite file.
            if peer.get("push_replica"):
                try:
                    await self._push_replica(peer)
                except Exception as e:
                    logger.debug("Replica push to %s failed: %s", peer.get("name"), e)
                continue
            # Replica targets accept source pushes but do not run broad manifest
            # pulls back toward the source. This preserves one clear authority.
            if peer.get("bootstrap_only"):
                continue
            try:
                await self._sync_one(peer, local_classified)
            except Exception as e:
                logger.debug("Sync with %s failed: %s", peer.get("name"), e)

    async def _push_replica(self, peer: Dict) -> None:
        """Continuously mirror the peer's selected app plane from this source."""
        from app.p2p.bootstrap import build_payload

        peer_id = peer.get("id", "")
        if not await self._transport.is_connected(peer_id):
            if not await self._transport.connect(peer):
                raise RuntimeError("Could not establish the encrypted P2P channel")

        payload = await build_payload(peer.get("sync_options") or {})
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(body).hexdigest()
        if digest == ((peer.get("sync") or {}).get("last_replica_hash") or ""):
            peer_store.set_sync_state(peer_id, status="insync")
            return

        response_bytes = await self._transport.send(
            peer_id, "POST", "/api/v1/p2p/bootstrap/apply", body
        )
        result = json.loads(response_bytes.decode("utf-8") or "{}")
        if not result.get("ok"):
            raise RuntimeError(result.get("message") or "Replica apply failed")

        from datetime import datetime, timezone
        written = (
            int(result.get("app_rows") or 0)
            + int(result.get("secret_rows") or 0)
            + int(result.get("config_files") or 0)
        )
        peer_store.set_sync_state(
            peer_id,
            status="insync",
            last_sync_at=datetime.now(timezone.utc).isoformat(),
            last_sync_files=written,
            last_replica_hash=digest,
        )

    async def _sync_one(self, peer: Dict, local_classified: Dict) -> None:
        """Sync with one peer using the classified manifest system."""
        url = peer["url"].rstrip("/")
        peer_id = peer.get("id", "")

        # Ensure HTTP transport has a session key for this peer
        if not await self._transport.is_connected(peer_id):
            await self._transport.connect(peer)

        try:
            # Fetch remote classified manifest
            response_bytes = await self._transport.send(
                peer_id, "GET", "/api/v1/p2p/manifest", b""
            )
            data = json.loads(response_bytes.decode())
            remote_classified = data.get("classified", data)  # backward compat with old manifest
        except Exception as e:
            logger.debug("Manifest fetch from %s failed: %s", url, e)
            return

        # If remote sent old-style flat manifest, wrap it
        if "full" not in remote_classified:
            remote_classified = {"full": remote_classified.get("manifest", []), "row": {}}

        # Diff using classified manifest
        diff = diff_classified_manifest(local_classified, remote_classified)
        
        to_pull_files = diff.get("to_pull_files", [])
        to_push_files = diff.get("to_push_files", [])
        to_pull_vaults = diff.get("to_pull_vaults", [])
        to_push_vaults = diff.get("to_push_vaults", [])
        
        if not to_pull_files and not to_push_files and not to_pull_vaults and not to_push_vaults:
            peer_store.set_sync_state(peer_id, status="insync")
            return

        total_written = 0
        
        # ── Sync vault rows (always before file sync — pull-before-push) ──────
        if to_pull_vaults or to_push_vaults:
            try:
                vault_synced = await self._sync_vaults(peer, to_pull_vaults, to_push_vaults)
                total_written += vault_synced
            except Exception as e:
                logger.debug("Vault sync with %s failed: %s", peer.get("name"), e)
        
        # ── Pull changed files ────────────────────────────────────────────────
        if to_pull_files:
            logger.info("P2P sync: %d changed files from %s", len(to_pull_files), peer.get("name"))
            try:
                written = await self._pull_files_via_transport(peer, to_pull_files)
                total_written += written
            except Exception as e:
                logger.warning("Pull from %s failed: %s", peer.get("name"), e)

        # Notify peer of local changes (they pull on their next tick)
        if to_push_files:
            logger.info("P2P sync: notifying %s of %d local changes", peer.get("name"), len(to_push_files))
            try:
                pushed = await self._notify_push(peer, to_push_files)
                total_written += pushed
            except Exception as e:
                logger.debug("Push notification to %s failed: %s", peer.get("name"), e)

        from datetime import datetime, timezone
        peer_store.set_sync_state(
            peer_id,
            status="insync",
            last_sync_at=datetime.now(timezone.utc).isoformat(),
            last_sync_files=total_written,
        )

    async def _pull_files_via_transport(self, peer: Dict, wanted: List[Dict]) -> int:
        """Fetch changed files from a peer using the transport layer.
        
        DB files are requested with snapshot_db=True for atomic backup.
        Returns number of files successfully written.
        """
        if not wanted:
            return 0

        paths = [e["path"] for e in wanted]
        db_paths = [p for p in paths if p.endswith(".db")]
        regular_paths = [p for p in paths if not p.endswith(".db")]

        total_written = 0
        peer_id = peer["id"]

        # Pull regular files
        if regular_paths:
            body = json.dumps({"paths": regular_paths, "snapshot_db": False}).encode()
            try:
                response_bytes = await self._transport.send(peer_id, "POST", "/api/v1/p2p/pull", body)
                data = json.loads(response_bytes.decode())
                for f in data.get("files", []):
                    try:
                        _write_received_file(f, peer_id)
                        total_written += 1
                    except Exception as e:
                        logger.warning("Failed to write pulled file %s: %s", f.get("path"), e)
            except Exception as e:
                logger.warning("Pull batch from %s failed: %s", peer.get("name"), e)

        # Pull DB files individually (each snapshotted atomically)
        for dbp in db_paths:
            body = json.dumps({"paths": [dbp], "snapshot_db": True}).encode()
            try:
                response_bytes = await self._transport.send(peer_id, "POST", "/api/v1/p2p/pull", body)
                data = json.loads(response_bytes.decode())
                for f in data.get("files", []):
                    try:
                        _write_received_file(f, peer_id)
                        total_written += 1
                    except Exception as e:
                        logger.warning("Failed to write pulled DB %s: %s", f.get("path"), e)
            except Exception as e:
                logger.warning("Pull DB %s from %s failed: %s", dbp, peer.get("name"), e)

        return total_written

    async def _notify_push(self, peer: Dict, wanted: List[Dict]) -> int:
        """Notify a peer that we have local changes, triggering their next pull cycle.
        
        The peer's pull-based sync picks up our changes naturally on the next tick.
        This just sends a lightweight nudge so the peer doesn't wait the full interval.
        Returns 0 (notification only, no files transferred here).
        """
        if not wanted:
            return 0
        peer_id = peer["id"]
        try:
            # Lightweight ping to trigger the peer's next sync cycle
            await self._transport.send(peer_id, "GET", "/api/v1/p2p/status", b"")
            return 0
        except Exception:
            return 0


    async def _sync_vaults(self, peer: Dict, to_pull_vaults: list, to_push_vaults: list) -> int:
        """Sync vault rows with a peer using row-level manifest diffing.

        Pull: fetches remote vault manifest, diffs, pulls changed rows, applies locally.
        Push: builds local manifest, diffs against remote, pushes local-newer rows.
        Returns number of rows synced.
        """
        peer_id = peer.get("id", "")
        total_synced = 0

        # ── Pull vault rows from remote ────────────────────────────────────
        for vault_schema in to_pull_vaults:
            try:
                # Fetch remote vault manifest
                body = json.dumps({"vault_schema": vault_schema}).encode()
                response_bytes = await self._transport.send(
                    peer_id, "POST", "/api/v1/p2p/vault/manifest", body
                )
                data = json.loads(response_bytes.decode())
                remote_rows = data.get("manifests", {}).get(vault_schema, [])

                if not remote_rows:
                    continue

                # Build local manifest
                local_rows = build_vault_row_manifest(vault_schema)

                # Diff
                diff = diff_vault_rows(local_rows, remote_rows)
                to_pull_rows = diff.get("to_pull", [])

                if to_pull_rows:
                    # Fetch full rows from remote
                    pull_keys = [{"user_id": r["user_id"], "service": r["service"], "label": r["label"]} for r in to_pull_rows]
                    pull_body = json.dumps({"vault_schema": vault_schema, "rows": pull_keys}).encode()
                    resp_bytes = await self._transport.send(
                        peer_id, "POST", "/api/v1/p2p/vault/pull", pull_body
                    )
                    pull_data = json.loads(resp_bytes.decode())
                    full_rows = pull_data.get("rows", [])

                    if full_rows:
                        result = apply_vault_rows(vault_schema, full_rows, direction="pull")
                        total_synced += result.get("applied", 0)
                        logger.info("P2P vault: pulled %d rows from %s/%s", result.get("applied", 0), peer.get("name"), vault_schema)
            except Exception as e:
                logger.debug("Vault pull %s from %s failed: %s", vault_schema, peer.get("name"), e)

        # ── Push local vault rows to remote ────────────────────────────────
        for vault_schema in to_push_vaults:
            try:
                local_rows = build_vault_row_manifest(vault_schema)
                if not local_rows:
                    continue

                # Fetch remote manifest for diffing
                push_body = json.dumps({"vault_schema": vault_schema}).encode()
                response_bytes = await self._transport.send(
                    peer_id, "POST", "/api/v1/p2p/vault/manifest", push_body
                )
                data = json.loads(response_bytes.decode())
                remote_rows = data.get("manifests", {}).get(vault_schema, [])

                # Diff (reverse: local vs remote)
                diff = diff_vault_rows(remote_rows, local_rows)
                to_push_rows = diff.get("to_pull", [])  # "to_pull" from remote's perspective = local-newer rows

                if to_push_rows:
                    push_payload = json.dumps({"vault_schema": vault_schema, "rows": to_push_rows}).encode()
                    await self._transport.send(
                        peer_id, "POST", "/api/v1/p2p/vault/push", push_payload
                    )
                    total_synced += len(to_push_rows)
                    logger.info("P2P vault: pushed %d rows to %s/%s", len(to_push_rows), peer.get("name"), vault_schema)
            except Exception as e:
                logger.debug("Vault push %s to %s failed: %s", vault_schema, peer.get("name"), e)

        return total_synced


# ── Module-level singleton ─────────────────────────────────────────────────────

_worker: Optional[P2PWorker] = None


def get_worker() -> P2PWorker:
    global _worker
    if _worker is None:
        _worker = P2PWorker()
    return _worker


async def start_worker() -> None:
    await get_worker().start()


async def stop_worker() -> None:
    if _worker:
        await _worker.stop()


def kick() -> None:
    if _worker:
        _worker.kick()

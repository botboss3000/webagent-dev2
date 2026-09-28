"""
Tiered, append-only message cache keyed by authenticated user and session.

Why it exists
-------------
Every turn of an agent session needs the full ``messages[]`` array (system
prompts + conversation history) sent to the LLM.  Without a cache the server
rebuilds this array from scratch each turn — re-reading the DB (``chat.py``),
re-deserializing the browser's transcript (``browser_storage.py``), and
re-concatenating every layer in ``_build_layered_messages``.

With this cache:
  1. Turn 1 cache-miss → the full prompt is split into immutable,
     content-addressed blocks.
  2. Turn 2+ cache-hit → the block objects are structurally shared and only
     the newly appended suffix is copied, token-counted, and persisted.
     The LLM sees a byte-identical prefix, allowing provider prompt caching.
  3. The loop avoids rebuilding and deep-copying the stable prompt layers; the
     authoritative history hash still validates that cached content is current.

Decoded hot blocks are bounded by bytes and evicted LRU-style.  Their canonical
JSON representation is stored under the runtime data root so a cold block can
be rehydrated without keeping it in process RAM.  Disk use is also bounded and
old files are pruned; it is an application cache, never OS swap.

Thread safety
-------------
``asyncio.Lock`` guards the internal dict so concurrent event-loop tasks
(rare but possible during background automations) do not race.

Telemetry
---------
Every public ``get()`` / ``set()`` call records a hit or miss to the in-memory
metrics recorder (``app.metrics.record_cache_hit`` / ``record_cache_miss``).
The admin Dashboard's ``/metrics`` endpoint surfaces these live — no DB read.
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

from app.metrics import record_cache_hit, record_cache_miss

from app.agent.cache_profiles import stable_hash
from app.runtime_mode import data_root

logger = logging.getLogger(__name__)

_TTL_SECONDS = 1800   # 30 min — after that, rebuild from source
_MAX_ENTRIES = 1000   # ~50 MB at 50 KB/session — safe for most deployments
_BLOCK_TARGET_BYTES = 256_000
_HOT_BLOCK_BYTES = 64 * 1024 * 1024
_DISK_BLOCK_BYTES = 512 * 1024 * 1024
_DISK_BLOCK_TTL_SECONDS = 24 * 60 * 60


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _canonical_message(message: Dict[str, Any]) -> bytes:
    return json.dumps(
        message, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str,
    ).encode("utf-8")


def _block_payload(messages: Iterable[Dict[str, Any]]) -> bytes:
    return json.dumps(
        list(messages), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False, default=str,
    ).encode("utf-8")


def _token_estimate_from_bytes(payload: bytes) -> int:
    # Keep this deterministic and dependency-free.  Context Control uses the
    # same deliberately conservative chars/token class of estimate.
    return max(1, (len(payload) + 3) // 4)


class CachedMessageList(list):
    """Mutable request list carrying the length of its structurally shared prefix.

    The agent loop appends to this list.  Mutations inside the shared prefix
    reduce ``stable_prefix`` so ``set()`` never reuses a block that changed.
    """

    def __init__(self, values=(), *, cache_key=None, block_ids=()):
        super().__init__(values)
        self.cache_key = cache_key
        self.block_ids = tuple(block_ids)
        self.stable_prefix = len(self)

    def _dirty(self, index: int = 0) -> None:
        if index < 0:
            index = max(0, len(self) + index)
        self.stable_prefix = min(self.stable_prefix, max(0, index))

    def __setitem__(self, key, value):
        self._dirty(key.start or 0 if isinstance(key, slice) else key)
        return super().__setitem__(key, value)

    def __delitem__(self, key):
        self._dirty(key.start or 0 if isinstance(key, slice) else key)
        return super().__delitem__(key)

    def insert(self, index, value):
        self._dirty(index)
        return super().insert(index, value)

    def pop(self, index=-1):
        self._dirty(index)
        return super().pop(index)

    def remove(self, value):
        self._dirty(self.index(value))
        return super().remove(value)

    def clear(self):
        self._dirty(0)
        return super().clear()

    def reverse(self):
        self._dirty(0)
        return super().reverse()

    def sort(self, *args, **kwargs):
        self._dirty(0)
        return super().sort(*args, **kwargs)


@dataclass(frozen=True)
class _BlockMeta:
    block_id: str
    message_count: int
    byte_size: int
    token_estimate: int


@dataclass
class _Manifest:
    timestamp: float
    blocks: Tuple[_BlockMeta, ...]
    system_hash: str
    history_hash: str

    @property
    def message_count(self) -> int:
        return sum(block.message_count for block in self.blocks)

    @property
    def token_estimate(self) -> int:
        return sum(block.token_estimate for block in self.blocks)


class SessionMessageCache:
    """Content-addressed message blocks with bounded RAM and disk tiers."""

    def __init__(
        self,
        *,
        cache_dir: Optional[Path] = None,
        hot_max_bytes: Optional[int] = None,
        disk_max_bytes: Optional[int] = None,
        disk_enabled: Optional[bool] = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._data: Dict[Tuple[str, str], _Manifest] = {}
        self._hot: "OrderedDict[str, Tuple[Tuple[Dict[str, Any], ...], int]]" = OrderedDict()
        self._hot_bytes = 0
        self._hot_max_bytes = hot_max_bytes if hot_max_bytes is not None else _env_int(
            "WEBAGENT_CONTEXT_HOT_CACHE_BYTES", _HOT_BLOCK_BYTES,
        )
        self._disk_max_bytes = disk_max_bytes if disk_max_bytes is not None else _env_int(
            "WEBAGENT_CONTEXT_DISK_CACHE_BYTES", _DISK_BLOCK_BYTES,
        )
        if disk_enabled is None:
            disk_enabled = os.environ.get(
                "WEBAGENT_CONTEXT_DISK_CACHE", "1"
            ).strip().lower() not in {"0", "false", "no", "off"}
        self._disk_enabled = bool(disk_enabled and self._disk_max_bytes > 0)
        self._cache_dir = Path(cache_dir or (data_root() / "cache" / "context-blocks"))
        self._blocks_dir = self._cache_dir / "blocks"
        self._manifests_dir = self._cache_dir / "manifests"
        self._last_prune = 0.0

    def _manifest_path(self, key: Tuple[str, str]) -> Path:
        digest = hashlib.sha256((key[0] + "\0" + key[1]).encode("utf-8")).hexdigest()
        return self._manifests_dir / f"{digest}.json"

    def _block_path(self, block_id: str) -> Path:
        return self._blocks_dir / f"{block_id}.json"

    def _put_hot(self, block_id: str, messages: Tuple[Dict[str, Any], ...], size: int) -> None:
        old = self._hot.pop(block_id, None)
        if old:
            self._hot_bytes -= old[1]
        self._hot[block_id] = (messages, size)
        self._hot_bytes += size
        while self._hot and self._hot_bytes > self._hot_max_bytes:
            _, (_, evicted_size) = self._hot.popitem(last=False)
            self._hot_bytes -= evicted_size

    def _write_atomic(
        self, path: Path, payload: bytes, *, skip_if_exists: bool = False,
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if skip_if_exists and path.exists():
            try:
                os.utime(path, None)
            except OSError:
                pass
            return
        fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def _manifest_payload(self, manifest: _Manifest) -> bytes:
        return json.dumps({
            "timestamp": manifest.timestamp,
            "system_hash": manifest.system_hash,
            "history_hash": manifest.history_hash,
            "blocks": [block.__dict__ for block in manifest.blocks],
        }, separators=(",", ":"), sort_keys=True).encode("utf-8")

    def _load_manifest_disk(self, key: Tuple[str, str]) -> Optional[_Manifest]:
        if not self._disk_enabled:
            return None
        try:
            raw = json.loads(self._manifest_path(key).read_text(encoding="utf-8"))
            return _Manifest(
                timestamp=float(raw["timestamp"]),
                system_hash=str(raw["system_hash"]),
                history_hash=str(raw["history_hash"]),
                blocks=tuple(_BlockMeta(**item) for item in raw["blocks"]),
            )
        except (OSError, ValueError, TypeError, KeyError):
            return None

    async def _load_block(self, meta: _BlockMeta) -> Optional[Tuple[Dict[str, Any], ...]]:
        hot = self._hot.pop(meta.block_id, None)
        if hot is not None:
            self._hot[meta.block_id] = hot
            record_cache_hit("context_blocks_hot")
            return hot[0]
        if not self._disk_enabled:
            record_cache_miss("context_blocks_disk")
            return None
        try:
            def read_block() -> Tuple[Dict[str, Any], ...]:
                return tuple(json.loads(
                    self._block_path(meta.block_id).read_text(encoding="utf-8")
                ))

            messages = await asyncio.to_thread(read_block)
            if len(messages) != meta.message_count:
                record_cache_miss("context_blocks_disk")
                return None
            self._put_hot(meta.block_id, messages, meta.byte_size)
            record_cache_hit("context_blocks_disk")
            return messages
        except (OSError, ValueError, TypeError):
            record_cache_miss("context_blocks_disk")
            return None

    def _drop_manifest(self, key: Tuple[str, str]) -> None:
        self._data.pop(key, None)
        if self._disk_enabled:
            try:
                self._manifest_path(key).unlink()
            except FileNotFoundError:
                pass

    def _prune_disk(self) -> None:
        if not self._disk_enabled:
            return
        now = time.time()
        if now - self._last_prune < 300:
            return
        self._last_prune = now
        files = []
        total = 0
        for folder in (self._blocks_dir, self._manifests_dir):
            if not folder.exists():
                continue
            for path in folder.glob("*.json"):
                try:
                    stat = path.stat()
                except OSError:
                    continue
                if now - stat.st_mtime > _DISK_BLOCK_TTL_SECONDS:
                    try:
                        path.unlink()
                    except OSError:
                        pass
                    continue
                files.append((stat.st_mtime, stat.st_size, path))
                total += stat.st_size
        for _, size, path in sorted(files):
            if total <= self._disk_max_bytes:
                break
            try:
                path.unlink()
                total -= size
            except OSError:
                pass

    def _persist_files(
        self,
        block_writes: List[Tuple[Path, bytes]],
        manifest_path: Path,
        manifest_payload: bytes,
    ) -> None:
        for path, payload in block_writes:
            self._write_atomic(path, payload, skip_if_exists=True)
        self._write_atomic(manifest_path, manifest_payload)
        self._prune_disk()

    # ── public API ──────────────────────────────────────────────────────────

    async def has(self, user_id: str, session_id: str) -> bool:
        """Return True if a non-expired cache entry exists for this session."""
        key = (user_id, session_id)
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                entry = self._load_manifest_disk(key)
                if entry is not None:
                    self._data[key] = entry
            if entry is None or time.time() - entry.timestamp > _TTL_SECONDS:
                self._drop_manifest(key)
                return False
            return True

    async def get(
        self,
        user_id: str,
        session_id: str,
        system_hash: Optional[str] = None,
        history_hash: Optional[str] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """Return a shallow-copied messages array if cache is warm, else None.

        When *system_hash* is provided the entry is also validated against it;
        a mismatch triggers eviction (system prompt has changed).  When omitted
        (e.g. a caller that can't compute the same fingerprint) only TTL is
        checked — loop.py will validate the full hash on its own get().
        """
        key = (user_id, session_id)
        async with self._lock:
            entry = self._data.get(key) or self._load_manifest_disk(key)
            if entry is None:
                record_cache_miss("session_messages")
                return None
            self._data[key] = entry
            # TTL check
            if time.time() - entry.timestamp > _TTL_SECONDS:
                self._drop_manifest(key)
                record_cache_miss("session_messages")
                return None
            # System prompt changed (agent config updated) → rebuild
            if system_hash is not None and entry.system_hash != system_hash:
                self._drop_manifest(key)
                record_cache_miss("session_messages")
                return None
            if history_hash is None or entry.history_hash != history_hash:
                self._drop_manifest(key)
                record_cache_miss("session_messages")
                return None
            values: List[Dict[str, Any]] = []
            for block in entry.blocks:
                decoded = await self._load_block(block)
                if decoded is None:
                    self._drop_manifest(key)
                    record_cache_miss("session_messages")
                    return None
                values.extend(decoded)
        record_cache_hit("session_messages")
        return CachedMessageList(
            values,
            cache_key=key,
            block_ids=tuple(block.block_id for block in entry.blocks),
        )

    async def set(
        self,
        user_id: str,
        session_id: str,
        messages: List[Dict[str, Any]],
        system_hash: str,
        history_hash: str,
    ) -> None:
        """Store a snapshot of the messages array for this session.

        The caller passes the *final* ``messages[]`` array after a turn completes,
        with the last assistant response appended.  The FULL array is stored so
        the next turn's ``get()`` returns the complete conversation context.

        Existing complete blocks from a ``get()`` result are retained by ID.
        Only the changed suffix is snapshotted into new blocks.
        """
        if not history_hash:
            return
        key = (user_id, session_id)
        async with self._lock:
            previous = self._data.get(key)
            reusable: List[_BlockMeta] = []
            stable_prefix = 0
            if (
                previous is not None
                and isinstance(messages, CachedMessageList)
                and messages.cache_key == key
            ):
                allowed = min(messages.stable_prefix, len(messages))
                covered = 0
                for block in previous.blocks:
                    if covered + block.message_count > allowed:
                        break
                    reusable.append(block)
                    covered += block.message_count
                stable_prefix = covered

            import copy as _copy
            new_blocks: List[_BlockMeta] = []
            disk_writes: List[Tuple[Path, bytes]] = []
            chunk: List[Dict[str, Any]] = []
            chunk_bytes = 2

            def flush_chunk() -> None:
                nonlocal chunk, chunk_bytes
                if not chunk:
                    return
                snapshot = tuple(_copy.deepcopy(chunk))
                payload = _block_payload(snapshot)
                block_id = hashlib.sha256(payload).hexdigest()
                meta = _BlockMeta(
                    block_id=block_id,
                    message_count=len(snapshot),
                    byte_size=len(payload),
                    token_estimate=_token_estimate_from_bytes(payload),
                )
                self._put_hot(block_id, snapshot, len(payload))
                if self._disk_enabled:
                    disk_writes.append((self._block_path(block_id), payload))
                new_blocks.append(meta)
                chunk = []
                chunk_bytes = 2

            for message in messages[stable_prefix:]:
                message_size = len(_canonical_message(message)) + 1
                if chunk and chunk_bytes + message_size > _BLOCK_TARGET_BYTES:
                    flush_chunk()
                chunk.append(message)
                chunk_bytes += message_size
            flush_chunk()

            manifest = _Manifest(
                timestamp=time.time(),
                blocks=tuple(reusable + new_blocks),
                system_hash=system_hash,
                history_hash=history_hash,
            )
            # Capacity evict: drop oldest entry when full
            if len(self._data) >= _MAX_ENTRIES:
                oldest_key = min(self._data.keys(), key=lambda item: self._data[item].timestamp)
                self._drop_manifest(oldest_key)
            if self._disk_enabled:
                await asyncio.to_thread(
                    self._persist_files,
                    disk_writes,
                    self._manifest_path(key),
                    self._manifest_payload(manifest),
                )
            self._data[key] = manifest

    async def stats(self) -> Dict[str, int]:
        """Return bounded-cache diagnostics without exposing prompt contents."""
        async with self._lock:
            return {
                "sessions": len(self._data),
                "hot_blocks": len(self._hot),
                "hot_bytes": self._hot_bytes,
                "hot_max_bytes": self._hot_max_bytes,
                "disk_max_bytes": self._disk_max_bytes if self._disk_enabled else 0,
                "estimated_tokens": sum(m.token_estimate for m in self._data.values()),
            }


class ToolDefsCache:
    """In-memory cache of serialized ``tool_definitions`` arrays.

    Tool schemas are the same across every turn of a session (and often across
    many sessions using the same agent).  Rebuilding them each turn requires
    iterating over every loaded tool, checking ability modes and visibility
    rules, and constructing full JSON-Schema objects — pure CPU work that
    produces the same result.

    Key: stable hash of ``(agent_id, active_tools, active_abilities,
    suppressed_abilities)``.  Invalidates automatically when the user loads a
    new tool, toggles an ability, or the agent config changes.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: Dict[str, Tuple[float, List[Dict[str, Any]]]] = {}

    async def get(self, cache_key: str) -> Optional[List[Dict[str, Any]]]:
        """Return the cached tool_definitions list, or None."""
        async with self._lock:
            entry = self._data.get(cache_key)
            if entry is None:
                record_cache_miss("tool_defs")
                return None
            ts, defs = entry
            if time.time() - ts > _TTL_SECONDS:
                del self._data[cache_key]
                record_cache_miss("tool_defs")
                return None
        record_cache_hit("tool_defs")
        return list(defs)  # shallow copy

    async def set(self, cache_key: str, tool_definitions: List[Dict[str, Any]]) -> None:
        async with self._lock:
            if len(self._data) >= _MAX_ENTRIES:
                oldest = min(self._data.keys(), key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[cache_key] = (time.time(), list(tool_definitions))


# ── helpers ───────────────────────────────────────────────────────────────────

def compute_tool_defs_cache_key(
    agent_id: str,
    active_tools: set[str],
    active_abilities: set[str],
    suppressed_abilities: set[str],
) -> str:
    """Deterministic key for the tool_definitions array."""
    return stable_hash([
        agent_id,
        sorted(active_tools),
        sorted(active_abilities),
        sorted(suppressed_abilities),
    ])


# ── singleton accessors ───────────────────────────────────────────────────────

_cache: Optional[SessionMessageCache] = None
_tool_cache: Optional[ToolDefsCache] = None


def get_session_cache() -> SessionMessageCache:
    global _cache
    if _cache is None:
        _cache = SessionMessageCache()
    return _cache


def get_tool_defs_cache() -> ToolDefsCache:
    global _tool_cache
    if _tool_cache is None:
        _tool_cache = ToolDefsCache()
    return _tool_cache

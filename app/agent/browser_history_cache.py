"""Bounded, tenant-scoped browser history and idempotency caches."""

from __future__ import annotations

import asyncio
import copy
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.agent.cache_profiles import stable_hash

_TTL_SECONDS = 900
_MAX_ENTRIES = 500
_MAX_TOTAL_BYTES = 32 * 1024 * 1024
_MAX_ENTRY_BYTES = 1024 * 1024


@dataclass
class BrowserHistoryEntry:
    created_at: float
    token: str
    revision: int
    content_hash: str
    history: List[Dict[str, Any]]
    size_bytes: int


class BrowserHistoryCache:
    """One-time revision tokens for validated warm-history requests."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: Dict[Tuple[str, str], BrowserHistoryEntry] = {}
        self._total_bytes = 0

    @staticmethod
    def content_hash(history: List[Dict[str, Any]]) -> str:
        return stable_hash(history)

    async def consume(
        self,
        user_id: str,
        session_id: str,
        *,
        token: str,
        revision: int,
    ) -> Optional[List[Dict[str, Any]]]:
        key = (user_id, session_id)
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if time.time() - entry.created_at > _TTL_SECONDS:
                self._remove_locked(key)
                return None
            if not secrets.compare_digest(entry.token, token):
                return None
            if entry.revision != revision:
                return None
            self._remove_locked(key)
            return copy.deepcopy(entry.history)

    async def put(
        self,
        user_id: str,
        session_id: str,
        *,
        revision: int,
        history: List[Dict[str, Any]],
    ) -> Optional[dict]:
        raw = json.dumps(history, sort_keys=True, separators=(",", ":"), default=str)
        size_bytes = len(raw.encode("utf-8"))
        if size_bytes > _MAX_ENTRY_BYTES:
            return None
        entry = BrowserHistoryEntry(
            created_at=time.time(),
            token=secrets.token_urlsafe(32),
            revision=revision,
            content_hash=self.content_hash(history),
            history=copy.deepcopy(history),
            size_bytes=size_bytes,
        )
        key = (user_id, session_id)
        async with self._lock:
            self._remove_locked(key)
            self._evict_expired_locked()
            while self._data and (
                len(self._data) >= _MAX_ENTRIES
                or self._total_bytes + size_bytes > _MAX_TOTAL_BYTES
            ):
                oldest = min(
                    self._data, key=lambda item: self._data[item].created_at
                )
                self._remove_locked(oldest)
            self._data[key] = entry
            self._total_bytes += size_bytes
        return {
            "token": entry.token,
            "revision": entry.revision,
            "content_hash": entry.content_hash,
            "expires_in": _TTL_SECONDS,
        }

    async def invalidate(self, user_id: str, session_id: str) -> None:
        async with self._lock:
            self._remove_locked((user_id, session_id))

    async def purge_user(self, user_id: str) -> int:
        """Remove every warm-history entry owned by one deleted account."""
        async with self._lock:
            keys = [key for key in self._data if key[0] == user_id]
            for key in keys:
                self._remove_locked(key)
            return len(keys)

    async def accept_cold_revision(
        self, user_id: str, session_id: str, revision: int
    ) -> bool:
        """Replace a warm entry only when the cold snapshot is not stale.

        A missing entry means the cache cannot arbitrate (restart/eviction), so
        the complete browser-authoritative snapshot is accepted for recovery.
        """
        key = (user_id, session_id)
        async with self._lock:
            entry = self._data.get(key)
            if entry and time.time() - entry.created_at > _TTL_SECONDS:
                self._remove_locked(key)
                entry = None
            if entry is not None and revision < entry.revision:
                return False
            self._remove_locked(key)
            return True

    def _remove_locked(self, key: Tuple[str, str]) -> None:
        entry = self._data.pop(key, None)
        if entry:
            self._total_bytes = max(0, self._total_bytes - entry.size_bytes)

    def _evict_expired_locked(self) -> None:
        now = time.time()
        for key, entry in list(self._data.items()):
            if now - entry.created_at > _TTL_SECONDS:
                self._remove_locked(key)


@dataclass
class BrowserTurnReplayEntry:
    created_at: float
    request_hash: str
    events: List[Dict[str, Any]]


class BrowserTurnReplayCache:
    """Short-lived idempotency receipts for completed browser turns."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._data: Dict[Tuple[str, str, str], BrowserTurnReplayEntry] = {}

    async def get(
        self, user_id: str, session_id: str, idempotency_key: str, request_hash: str
    ) -> Optional[List[Dict[str, Any]]]:
        key = (user_id, session_id, idempotency_key)
        async with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            if time.time() - entry.created_at > _TTL_SECONDS:
                self._data.pop(key, None)
                return None
            if not secrets.compare_digest(entry.request_hash, request_hash):
                raise ValueError("idempotency key reused with different payload")
            return copy.deepcopy(entry.events)

    async def put(
        self,
        user_id: str,
        session_id: str,
        idempotency_key: str,
        request_hash: str,
        events: List[Dict[str, Any]],
    ) -> None:
        key = (user_id, session_id, idempotency_key)
        async with self._lock:
            now = time.time()
            for old_key, entry in list(self._data.items()):
                if now - entry.created_at > _TTL_SECONDS:
                    self._data.pop(old_key, None)
            while len(self._data) >= _MAX_ENTRIES:
                oldest = min(
                    self._data, key=lambda item: self._data[item].created_at
                )
                self._data.pop(oldest, None)
            self._data[key] = BrowserTurnReplayEntry(
                created_at=now,
                request_hash=request_hash,
                events=copy.deepcopy(events),
            )

    async def purge_user(self, user_id: str) -> int:
        """Remove every in-memory idempotency receipt for one deleted account."""
        async with self._lock:
            keys = [key for key in self._data if key[0] == user_id]
            for key in keys:
                self._data.pop(key, None)
            return len(keys)


_cache: Optional[BrowserHistoryCache] = None
_turn_cache: Optional[BrowserTurnReplayCache] = None


def get_browser_history_cache() -> BrowserHistoryCache:
    global _cache
    if _cache is None:
        _cache = BrowserHistoryCache()
    return _cache


def get_browser_turn_replay_cache() -> BrowserTurnReplayCache:
    global _turn_cache
    if _turn_cache is None:
        _turn_cache = BrowserTurnReplayCache()
    return _turn_cache

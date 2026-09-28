"""Production-contract regressions for Phase 1 storage authority and sync."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import task_state
from app.agent.browser_history_cache import (
    BrowserHistoryCache,
    BrowserTurnReplayCache,
)
from app.agent.session_cache import SessionMessageCache
from app.db import user_store
from app.db.browser_authority import BrowserAuthorityDB


class _ReadPlane:
    def __init__(self) -> None:
        self.writes = []

    async def get_agent_by_id(self, agent_id):
        return {"id": agent_id}

    async def insert_session(self, *args, **kwargs):
        self.writes.append((args, kwargs))


class BrowserAuthorityContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_adapter_enumerates_reads_and_never_delegates_unknown_writes(self):
        shared = _ReadPlane()
        db = BrowserAuthorityDB(shared, user_id="user-a", session_id="session-a")
        self.addCleanup(db.close)
        self.assertEqual((await db.get_agent_by_id("agent"))["id"], "agent")
        self.assertFalse(hasattr(db, "insert_session"))
        with self.assertRaises(AttributeError):
            await db.insert_session("session-a")
        self.assertEqual(shared.writes, [])

    async def test_transcript_writes_are_ephemeral_and_owner_scoped(self):
        db = BrowserAuthorityDB(_ReadPlane(), user_id="user-a", session_id="session-a")
        self.addCleanup(db.close)
        interaction_id = await db.insert_interaction(
            "user-a", "session-a", role="assistant", content="hello"
        )
        self.assertEqual(db.export_interactions()[0]["id"], interaction_id)
        with self.assertRaises(Exception):
            await db.insert_interaction(
                "user-b", "session-a", role="assistant", content="leak"
            )

    async def test_task_progress_uses_facade_scoped_memory_not_shared_storage(self):
        shared = _ReadPlane()
        db = BrowserAuthorityDB(shared, user_id="user-a", session_id="session-a")
        self.addCleanup(db.close)
        started = task_state.begin_task(
            db, session_id="session-a", turn_id="turn-a", task_id="task-a",
            source_messages=[{"interaction_id": "turn-a", "content": "Fix it"}],
            mode="auto",
        )
        updated = task_state.update_checkpoint(
            db, "session-a", "turn-a", started["revision"],
            {"summary": "Started browser-authority task", "steps": [{
                "label": "Record browser-authority state",
                "status": "in_progress",
                "depends_on": [],
            }]},
        )
        self.assertEqual(
            updated["checkpoint"]["summary"], "Started browser-authority task"
        )
        self.assertEqual(
            task_state.current_state(db, "session-a", "turn-a")["revision"],
            updated["revision"],
        )
        self.assertFalse(hasattr(db, "_get_conn"))
        self.assertEqual(shared.writes, [])


class BrowserCacheContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_history_tokens_are_tenant_scoped_revisioned_and_one_time(self):
        cache = BrowserHistoryCache()
        ack = await cache.put(
            "user-a", "session-a", revision=2, history=[{"role": "user", "content": "a"}]
        )
        self.assertIsNone(
            await cache.consume(
                "user-b", "session-a", token=ack["token"], revision=2
            )
        )
        self.assertIsNone(
            await cache.consume(
                "user-a", "session-a", token=ack["token"], revision=1
            )
        )
        self.assertIsNotNone(
            await cache.consume(
                "user-a", "session-a", token=ack["token"], revision=2
            )
        )
        self.assertIsNone(
            await cache.consume(
                "user-a", "session-a", token=ack["token"], revision=2
            )
        )

    async def test_cold_snapshot_cannot_replace_a_newer_cached_revision(self):
        cache = BrowserHistoryCache()
        await cache.put("user-a", "session-a", revision=5, history=[])
        self.assertFalse(await cache.accept_cold_revision("user-a", "session-a", 4))
        self.assertTrue(await cache.accept_cold_revision("user-a", "session-a", 5))

    async def test_completed_turn_replay_rejects_key_reuse(self):
        cache = BrowserTurnReplayCache()
        events = [{"type": "response", "content": "ok"}]
        await cache.put("u", "s", "key", "hash-a", events)
        self.assertEqual(await cache.get("u", "s", "key", "hash-a"), events)
        with self.assertRaises(ValueError):
            await cache.get("u", "s", "key", "hash-b")

    async def test_server_message_cache_is_tenant_and_history_hash_scoped(self):
        cache = SessionMessageCache()
        messages = [{"role": "user", "content": "secret"}]
        await cache.set("user-a", "same-session", messages, "system", "history-a")
        self.assertIsNone(
            await cache.get("user-b", "same-session", "system", "history-a")
        )
        self.assertIsNone(
            await cache.get("user-a", "same-session", "system", "history-b")
        )


def _session(session_id: str) -> dict:
    return {
        "id": session_id,
        "title": "Title",
        "agent_id": "default",
        "metadata": {},
        "participants": [],
    }


def _interaction(session_id: str, interaction_id: str, content: str) -> dict:
    return {
        "id": interaction_id,
        "session_id": session_id,
        "role": "user",
        "content": content,
        "session_seq": 0,
    }


class RevisionedSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.patch = patch.object(user_store, "USER_DATA_DIR", self.temp.name)
        self.patch.start()
        self.store = user_store.UserStore("user-a")

    async def asyncTearDown(self):
        self.store.close()
        self.patch.stop()
        self.temp.cleanup()

    async def test_per_session_results_allow_partial_success_without_cleaning_conflict(self):
        first = {
            "mutation_id": "mutation-0001",
            "session_id": "session-a",
            "operation": "upsert",
            "base_server_revision": 0,
            "client_revision": 1,
            "content_hash": "hash-a",
            "session": _session("session-a"),
            "interactions": [_interaction("session-a", "ix-a", "first")],
        }
        self.assertEqual((await self.store.apply_sync_mutations("user-a", [first]))[0]["status"], "applied")

        stale = {
            **first,
            "mutation_id": "mutation-0002",
            "client_revision": 2,
            "content_hash": "hash-stale",
        }
        sibling = {
            "mutation_id": "mutation-0003",
            "session_id": "session-b",
            "operation": "upsert",
            "base_server_revision": 0,
            "client_revision": 1,
            "content_hash": "hash-b",
            "session": _session("session-b"),
            "interactions": [_interaction("session-b", "ix-b", "sibling")],
        }
        results = await self.store.apply_sync_mutations("user-a", [stale, sibling])
        self.assertEqual([row["status"] for row in results], ["conflict", "applied"])
        self.assertIsNotNone(await self.store.get_session("session-b"))

    async def test_retry_is_idempotent_and_changed_payload_is_rejected(self):
        mutation = {
            "mutation_id": "mutation-1000",
            "session_id": "session-a",
            "operation": "upsert",
            "base_server_revision": 0,
            "client_revision": 1,
            "content_hash": "hash-a",
            "session": _session("session-a"),
            "interactions": [_interaction("session-a", "ix-a", "first")],
        }
        first = await self.store.apply_sync_mutations("user-a", [mutation])
        retry = await self.store.apply_sync_mutations("user-a", [mutation])
        self.assertEqual(retry, first)
        changed = {
            **mutation,
            "interactions": [_interaction("session-a", "ix-a", "changed")],
        }
        result = (await self.store.apply_sync_mutations("user-a", [changed]))[0]
        self.assertEqual(result["status"], "rejected")

    async def test_tombstone_prevents_stale_resurrection(self):
        upsert = {
            "mutation_id": "mutation-2000",
            "session_id": "session-a",
            "operation": "upsert",
            "base_server_revision": 0,
            "client_revision": 1,
            "content_hash": "hash-a",
            "session": _session("session-a"),
            "interactions": [_interaction("session-a", "ix-a", "first")],
        }
        await self.store.apply_sync_mutations("user-a", [upsert])
        delete = {
            "mutation_id": "mutation-2001",
            "session_id": "session-a",
            "operation": "delete",
            "base_server_revision": 1,
            "client_revision": 2,
        }
        deleted = (await self.store.apply_sync_mutations("user-a", [delete]))[0]
        self.assertEqual(deleted["status"], "applied")
        stale = {
            **upsert,
            "mutation_id": "mutation-2002",
            "base_server_revision": deleted["server_revision"],
            "client_revision": 3,
        }
        result = (await self.store.apply_sync_mutations("user-a", [stale]))[0]
        self.assertEqual(result["status"], "conflict")
        self.assertIn("tombstone", result["error"])
        self.assertEqual(await self.store.get_interactions("session-a"), [])

    async def test_same_session_id_is_isolated_between_users(self):
        other = user_store.UserStore("user-b")
        try:
            base = {
                "session_id": "shared-session",
                "operation": "upsert",
                "base_server_revision": 0,
                "client_revision": 1,
                "session": _session("shared-session"),
            }
            await self.store.apply_sync_mutations("user-a", [{
                **base,
                "mutation_id": "mutation-user-a",
                "content_hash": "hash-a",
                "interactions": [_interaction("shared-session", "ix-a", "alpha")],
            }])
            await other.apply_sync_mutations("user-b", [{
                **base,
                "mutation_id": "mutation-user-b",
                "content_hash": "hash-b",
                "interactions": [_interaction("shared-session", "ix-b", "beta")],
            }])
            self.assertEqual((await self.store.get_interactions("shared-session"))[0]["content"], "alpha")
            self.assertEqual((await other.get_interactions("shared-session"))[0]["content"], "beta")
        finally:
            other.close()


if __name__ == "__main__":
    unittest.main()

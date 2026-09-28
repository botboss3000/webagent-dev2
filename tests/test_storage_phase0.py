"""Regression tests for the Phase 0 storage containment policy."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.db import storage_router
from app.db import user_store


class StoragePolicyTests(unittest.TestCase):
    def _router_for(self, routing: dict, env: dict[str, str] | None = None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        path = Path(temp.name) / "storage_routing.json"
        path.write_text(json.dumps(routing), encoding="utf-8")
        env_patch = patch.dict(os.environ, env or {}, clear=True)
        path_patch = patch.object(storage_router, "_ROUTING_PATH", str(path))
        env_patch.start()
        path_patch.start()
        self.addCleanup(env_patch.stop)
        self.addCleanup(path_patch.stop)
        return storage_router.StorageRouter()

    def test_browser_routes_are_clamped_when_features_are_disabled(self):
        router = self._router_for({
            "session_data": "browser",
            "session_cache": "browser",
        })
        self.assertEqual(router.get("session_data"), "server")
        self.assertEqual(router.get("session_cache"), "server")

    def test_browser_routes_survive_when_explicitly_enabled(self):
        router = self._router_for(
            {"session_data": "browser", "session_cache": "browser"},
            {
                "WEBAGENT_ENABLE_BROWSER_AUTHORITY": "true",
                "WEBAGENT_ENABLE_BROWSER_SESSION_CACHE": "1",
            },
        )
        self.assertEqual(router.get("session_data"), "browser")
        self.assertEqual(router.get("session_cache"), "browser")

    def test_malformed_config_fails_closed(self):
        router = self._router_for(["not", "a", "mapping"])
        self.assertEqual(router.get("session_data"), "server")
        self.assertEqual(router.get("session_cache"), "server")


class UserStorePathTests(unittest.TestCase):
    def test_safe_user_path_stays_below_storage_root(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(user_store, "USER_DATA_DIR", temp):
                result = Path(user_store._user_db_path("person@example.com"))
                result.relative_to(Path(temp).resolve())

    def test_path_traversal_and_windows_device_names_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(user_store, "USER_DATA_DIR", temp):
                for value in ("../outside", r"..\outside", "C:", "CON", "", "a/b"):
                    with self.subTest(value=value):
                        with self.assertRaises(ValueError):
                            user_store._user_db_path(value)


class UserStoreImportTests(unittest.IsolatedAsyncioTestCase):
    async def test_import_serializes_structured_fields(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch.object(user_store, "USER_DATA_DIR", temp):
                store = user_store.UserStore("test-user")
                try:
                    sessions = [{
                            "id": "session-1",
                            "user_id": "test-user",
                            "metadata": {"source": "browser"},
                            "participants": ["test-user"],
                        }]
                    interactions_to_import = [{
                            "id": "interaction-1",
                            "session_id": "session-1",
                            "role": "user",
                            "content": "hello",
                            "metadata": {"private": True},
                        }]
                    count = await store.import_sessions(sessions, interactions_to_import)
                    # A retry must update in place rather than REPLACE the parent
                    # session and trip its interaction foreign key.
                    await store.import_sessions(sessions, interactions_to_import)
                    self.assertEqual(count, 1)
                    session = await store.get_session("session-1")
                    interactions = await store.get_interactions("session-1")
                    self.assertEqual(json.loads(session["metadata"])["source"], "browser")
                    self.assertEqual(json.loads(interactions[0]["metadata"])["private"], True)
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()

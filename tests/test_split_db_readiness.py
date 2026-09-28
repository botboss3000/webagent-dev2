from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.db.readiness import check_storage_authorities


class _RawQuery:
    def __init__(self, responses):
        self.responses = responses
        self.tables = []
        self.current = ""

    def table(self, name):
        self.current = name
        self.tables.append(name)
        return self

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def upsert(self, *_args, **_kwargs):
        return self

    def execute(self):
        return SimpleNamespace(data=self.responses.get(self.current, []))


class SplitStorageReadinessTests(unittest.TestCase):
    def test_active_layout_checks_app_and_existing_user_authorities(self):
        app_raw = _RawQuery({"user_accounts": [{"user_id": "admin"}]})
        user_raw = _RawQuery({"sessions": []})
        app_db = Mock()
        app_db.get_raw_client.return_value = app_raw
        user_db = Mock()
        user_db.get_raw_client.return_value = user_raw

        with tempfile.TemporaryDirectory() as tmp:
            user_path = Path(tmp) / "admin.db"
            user_path.touch()
            with patch("app.db.get_app_db", return_value=app_db), \
                    patch("app.db.get_user_db", return_value=user_db) as get_user, \
                    patch("app.db.storage_layout.is_layout_active", return_value=True), \
                    patch("app.db.user_store._user_db_path", return_value=str(user_path)):
                check_storage_authorities()

        self.assertEqual(app_raw.tables, ["user_accounts"])
        self.assertEqual(user_raw.tables, ["sessions"])
        get_user.assert_called_once_with("admin")

    def test_missing_user_authority_fails_without_opening_backend(self):
        app_raw = _RawQuery({"user_accounts": [{"user_id": "admin"}]})
        app_db = Mock()
        app_db.get_raw_client.return_value = app_raw

        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "admin.db"
            with patch("app.db.get_app_db", return_value=app_db), \
                    patch("app.db.get_user_db") as get_user, \
                    patch("app.db.storage_layout.is_layout_active", return_value=True), \
                    patch("app.db.user_store._user_db_path", return_value=str(missing)):
                with self.assertRaisesRegex(RuntimeError, "User authority is missing"):
                    check_storage_authorities()

        get_user.assert_not_called()


class InstanceMetadataRoutingTests(unittest.TestCase):
    def test_instance_metadata_uses_explicit_app_authority(self):
        from app.db.instance_meta import (
            get_instance,
            list_instances_by_kind,
            upsert_instance,
        )

        raw = _RawQuery(
            {
                "instances": [
                    {
                        "ref": "dev",
                        "kind": "server",
                        "metadata": "{}",
                    }
                ]
            }
        )
        app_db = Mock()
        app_db.get_raw_client.return_value = raw

        with patch("app.db.get_app_db", return_value=app_db) as get_app:
            row = asyncio.run(get_instance("dev"))
            listed = asyncio.run(list_instances_by_kind("server"))
            saved = asyncio.run(upsert_instance("dev", status="online"))

        self.assertEqual(row["ref"], "dev")
        self.assertEqual(listed[0]["kind"], "server")
        self.assertTrue(saved)
        self.assertGreaterEqual(get_app.call_count, 4)
        self.assertTrue(all(name == "instances" for name in raw.tables))


if __name__ == "__main__":
    unittest.main()

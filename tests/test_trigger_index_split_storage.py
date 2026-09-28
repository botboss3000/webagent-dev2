from __future__ import annotations

import sqlite3
import unittest
from unittest.mock import Mock, patch

from app.agent import trigger_index


class TriggerIndexSplitStorageTests(unittest.TestCase):
    def test_build_reads_templates_and_agents_from_their_own_planes(self):
        app_conn = sqlite3.connect(":memory:")
        app_conn.execute(
            "CREATE TABLE agent_templates "
            "(id TEXT PRIMARY KEY, trigger_type TEXT, trigger_key TEXT)"
        )
        app_conn.execute(
            "INSERT INTO agent_templates VALUES ('template-1', 'slash_command', '/old')"
        )
        app_conn.execute(
            "CREATE TABLE agent_catalog (agent_id TEXT PRIMARY KEY, deleted_at TEXT)"
        )
        app_conn.execute("INSERT INTO agent_catalog VALUES ('agent-1', NULL)")

        agent_conn = sqlite3.connect(":memory:")
        agent_conn.execute(
            "CREATE TABLE agents "
            "(template_id TEXT, trigger_type TEXT, trigger_key TEXT)"
        )
        agent_conn.execute(
            "INSERT INTO agents VALUES ('template-1', 'slash_command', '/new')"
        )

        app_backend = Mock()
        app_backend._get_conn.return_value = app_conn
        agent_backend = Mock()
        agent_backend._get_conn.return_value = agent_conn
        trigger_index._index = {}

        with (
            patch("app.db.get_db", side_effect=AssertionError("ambient DB is forbidden")),
            patch("app.db.get_app_db", return_value=app_backend),
            patch("app.db.get_data_db", side_effect=AssertionError("user DB is forbidden")),
            patch("app.db.get_agent_db", return_value=agent_backend) as get_agent_db,
        ):
            trigger_index.build()

        get_agent_db.assert_called_once_with("agent-1")
        self.assertEqual(trigger_index.all_entries(), {("slash_command", "/new"): "template-1"})


if __name__ == "__main__":
    unittest.main()

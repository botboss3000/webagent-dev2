"""Focused tests for exact native-session transcript retrieval."""

import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

from app.models.schemas import InteractionRecord
from plugins.abilities.Memory.context_control.context_control import (
    TOOL_SCHEMAS,
    build_tools,
)


def _row(
    iid: str, role: str, content: str, *, output: str | None = None,
    tool_name: str | None = None, tool_call_id: str | None = None,
) -> InteractionRecord:
    return InteractionRecord(
        id=iid,
        session_id="session",
        parent_id=None,
        role=role,
        content=content,
        output=output,
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        created_at=datetime.now(timezone.utc),
    )


class _DirectDb:
    def __init__(self, rows):
        self.rows = {row.id: row for row in rows}
        self.full_fetches = 0
        self.direct_fetches = []

    async def fetch_session_interaction(self, user_id, session_id, interaction_id):
        self.direct_fetches.append((user_id, session_id, interaction_id))
        return self.rows.get(interaction_id)

    async def fetch_interactions(self, user_id, session_id):
        self.full_fetches += 1
        return list(self.rows.values())


class DirectRecallTests(unittest.IsolatedAsyncioTestCase):
    async def test_tool_result_is_indexed_and_character_paginated(self):
        db = _DirectDb([_row(
            "t1", "tool", "abcdefghij", tool_name="read_source",
            tool_call_id="call-1",
        )])
        with patch("app.db.get_db", return_value=db):
            recall = build_tools(
                user_id="user", session_id="session")["recall_compacted"]

            first = json.loads(await recall(interaction_id="t1", max_chars=4))
            second = json.loads(await recall(
                interaction_id="t1", offset=first["next_offset"], max_chars=20,
            ))

        self.assertEqual(first["content"], "abcd")
        self.assertEqual(first["next_offset"], 4)
        self.assertEqual(second["content"], "efghij")
        self.assertIsNone(second["next_offset"])
        self.assertEqual(first["payload_kind"], "content")
        self.assertEqual(db.full_fetches, 0)
        self.assertEqual(db.direct_fetches, [
            ("user", "session", "t1"),
            ("user", "session", "t1"),
        ])

    async def test_assistant_selects_original_tool_arguments(self):
        arguments = json.dumps({"path": "app/auth.py", "content": "x" * 200})
        output = json.dumps({"tool_calls": [{
            "id": "call-write",
            "type": "function",
            "function": {"name": "write_file", "arguments": arguments},
        }]})
        db = _DirectDb([_row("a1", "assistant", "", output=output)])
        with patch("app.db.get_db", return_value=db):
            recall = build_tools(
                user_id="user", session_id="session")["recall_compacted"]
            result = json.loads(await recall(
                interaction_id="a1", tool_call_id="call-write", max_chars=1000,
            ))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["payload_kind"], "tool_arguments")
        self.assertEqual(result["content"], arguments)
        self.assertEqual(result["tool_call_id"], "call-write")
        self.assertEqual(db.full_fetches, 0)


def test_recall_schema_advertises_direct_retrieval():
    build_tools(user_id="user", session_id="session")
    properties = TOOL_SCHEMAS["recall_compacted"]["properties"]

    assert {"interaction_id", "tool_call_id", "offset", "max_chars"} <= set(properties)

"""SSE contract checks for manual compaction completion."""

import json
import unittest
from unittest.mock import AsyncMock, patch

from app.chat.execution import chat_stream
from app.models.schemas import ChatRequest


class CompactionSseTests(unittest.IsolatedAsyncioTestCase):
    async def test_detail_is_only_carried_by_structured_compaction_event(self):
        detail = (
            "Context: 123,747 → 93,647 tokens (30,100 saved, 24%).\n"
            "Folded 319 older message(s) into 2 new summary part(s)."
        )
        event = {
            "type": "compaction_completed",
            "status": "success",
            "session_id": "session",
            "interaction_id": "compact-1",
            "tokens_before": 123_747,
            "tokens_after": 93_647,
            "detail": detail,
        }
        request = ChatRequest(
            user_id="user", session_id="session", message="/compact",
        )
        prepared = {
            "slash_result": detail,
            "compaction": event,
        }

        with patch(
            "app.chat.execution._prepare_send", AsyncMock(return_value=prepared),
        ):
            response = await chat_stream(request, object())

        chunks = []
        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8")
            chunks.append(json.loads(chunk.removeprefix("data: ").strip()))

        self.assertEqual(chunks[0], event)
        self.assertEqual(
            chunks[1], {"type": "response", "level": "agent", "content": ""},
        )
        self.assertEqual(len(chunks), 2)


if __name__ == "__main__":
    unittest.main()

"""End-to-end regression coverage for the local main-message publisher."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.loop import _stream_agent_events_impl
from app.db.local import LocalBackend
from app.tools.loader import ToolInfo


class _Stream:
    def __init__(self, chunk):
        self._chunk = chunk
        self.reasoning_items = []
        self.response_id = None

    def __aiter__(self):
        async def _items():
            yield self._chunk
        return _items()

    async def close(self):
        return None


def _tool_chunk(calls):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None,
            reasoning_content=None,
            tool_calls=[SimpleNamespace(
                index=index,
                id=call_id,
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
            ) for index, (call_id, name, arguments) in enumerate(calls)],
        ))],
    )


def _text_chunk(text):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(delta=SimpleNamespace(
            content=text, reasoning_content=None, tool_calls=None,
        ))],
    )


class MainMessageVisibilityLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_control_is_clean_notice_before_real_tool_activity(self):
        await self._run_tool_case(include_main=True)

    async def test_plain_tool_stream_finalizes_with_originating_turn(self):
        await self._run_tool_case(include_main=False)

    async def _run_tool_case(self, *, include_main):
        sid = "session-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as td:
            db = LocalBackend(str(Path(td) / "loop.db"), seed=False)
            conn = db._get_conn()
            conn.execute(
                "INSERT INTO sessions (id, user_id, metadata) VALUES (?, ?, ?)",
                (sid, "admin", json.dumps({"execution_mode": "auto"})),
            )
            conn.commit()
            conn.close()

            provider_calls = []
            calls = [("read-1", "read_source", {})]
            if include_main:
                calls.insert(0, (
                    "main-1", "publish_main_message",
                    {"text": "I found the issue and am fixing it now."},
                ))
            actions = [
                _tool_chunk(calls),
                _text_chunk("The repair is complete."),
            ]

            async def read_source(**_kwargs):
                return "source result"

            tools = {
                "read_source": ToolInfo(
                    name="read_source", handler=read_source,
                    parameters={"type": "object", "properties": {}, "required": []},
                ),
            }

            async def fake_open_stream(create_kwargs, timeout_s, client=None):
                provider_calls.append(create_kwargs)
                return _Stream(actions.pop(0))

            async def fake_provider(*_args, **_kwargs):
                return {
                    "provider": "test", "base_url": "https://provider.example/v1",
                    "api_key": "test-key", "model": "test-model",
                }

            patches = [
                patch("app.tools.loader.load_tools", new=AsyncMock(return_value=tools)),
                patch("app.admin.settings.apply_provider_for_run", new=AsyncMock(side_effect=fake_provider)),
                patch("app.agent.contracts.parse_contract", return_value={"require_message_visibility": True}),
                patch("app.agent.contract_permissions.active_profile", return_value=None),
                patch("app.agent.loop._open_stream", new=fake_open_stream),
                patch("app.agent.loop._record_billing_usage", new=AsyncMock(return_value={})),
                patch("app.agent.loop._agent_profile_visitor", return_value=None),
                patch("app.agent.session_changes.capture_tool_state_async", new=AsyncMock(return_value={})),
                patch("app.agent.session_changes.record_tool_delta", new=AsyncMock(return_value=[])),
            ]
            with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5], patches[6], patches[7], patches[8]:
                events = [event async for event in _stream_agent_events_impl(
                    user_id="admin", session_id=sid, user_message="Fix the issue",
                    system_prompt="", agent_id="", history=[], parent_interaction_id="turn-1",
                    db=db, execution_mode="auto", max_turns=8, run_turn_id="turn-1",
                )]

            self.assertFalse([event for event in events if event.get("type") == "error"])
            schema_names = [item["function"]["name"] for item in provider_calls[0]["tools"]]
            self.assertIn("publish_main_message", schema_names)
            real_tool = next(event for event in events if event.get("type") == "tool_call")
            self.assertEqual(real_tool["tool"], "read_source")
            if include_main:
                visible_notice = next(event for event in events if event.get("message_visibility_notice"))
                self.assertLess(events.index(visible_notice), events.index(real_tool))
            self.assertTrue(any(event.get("chat_render_kind") == "promoted_answer"
                                and event.get("content") == "The repair is complete."
                                for event in events))

            rows = await db.fetch_interactions("admin", sid)
            if include_main:
                self.assertTrue(any(row.chat_render_kind == "notice"
                                    and row.content == "I found the issue and am fixing it now."
                                    for row in rows))
            else:
                assistant_rows = [row for row in rows if row.role == "assistant"]
                self.assertGreaterEqual(len(assistant_rows), 2)
                for row in assistant_rows:
                    self.assertEqual(row.status, "complete")
                    self.assertEqual(row.turn_id, "turn-1")
                self.assertTrue(any(row.chat_render_kind == "promoted_answer"
                                    and row.content == "The repair is complete."
                                    for row in assistant_rows))
            self.assertFalse(any(row.tool_name == "publish_main_message" for row in rows))
            self.assertTrue(any(row.tool_name == "read_source" for row in rows))

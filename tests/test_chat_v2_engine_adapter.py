"""Controlled preparation uses the accepted V2 message exactly once."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from app.chat.contracts import Conflict
from app.chat.engine_adapter import prepare_controlled_turn, execute_controlled_turn


class EngineAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_preparation_projects_one_interaction_without_v1_send(self):
        db=SimpleNamespace(
            is_session_dead=AsyncMock(return_value=False),
            fetch_agent_by_id_with_context=AsyncMock(return_value={"id":"agent"}),
            get_session_agent_id=AsyncMock(return_value=None),
            bind_session_to_agent=AsyncMock(),
            is_session_participant=AsyncMock(return_value=False),
            add_session_participant=AsyncMock(),
            insert_interaction=AsyncMock(return_value="interaction"),
        )
        request=SimpleNamespace(user_id="alice",session_id="session",agent_id="agent",
                                message="Hello",attachment_ids=["file"],execution_mode="ask")
        rows=[{"id":"message","client_message_id":"client"}]
        with patch("app.chat.execution._ensure_session",AsyncMock(return_value=True)), \
             patch("app.chat.execution._enforce_agent_access_policy",AsyncMock()), \
             patch("app.chat.execution._reset_agent_model_switcher",AsyncMock()), \
             patch("app.chat.execution._record_session_execution_mode",AsyncMock()):
            agent,interaction,first=await prepare_controlled_turn(db,request,rows)
        self.assertEqual((agent["id"],interaction,first),("agent","interaction",True))
        db.insert_interaction.assert_awaited_once()
        metadata=json.loads(db.insert_interaction.await_args.kwargs["metadata"])
        self.assertEqual((metadata["cmid"],metadata["chat_message_id"],metadata["attachment_ids"]),
                         ("client","message",["file"]))
        db.bind_session_to_agent.assert_awaited_once_with("session","agent")

    async def test_mismatched_agent_cannot_change_existing_binding(self):
        db=SimpleNamespace(
            is_session_dead=AsyncMock(return_value=False),
            fetch_agent_by_id_with_context=AsyncMock(return_value={"id":"other"}),
            get_session_agent_id=AsyncMock(return_value="agent"),
        )
        request=SimpleNamespace(user_id="alice",session_id="session",agent_id="other",
                                message="Hello",attachment_ids=[],execution_mode="ask")
        with patch("app.chat.execution._ensure_session",AsyncMock(return_value=False)), \
             patch("app.chat.execution._enforce_agent_access_policy",AsyncMock()):
            with self.assertRaises(Conflict):
                await prepare_controlled_turn(db,request,[{"id":"m","client_message_id":"c"}])

    async def test_worker_uses_shared_engine_with_v2_context(self):
        db=object()
        request=SimpleNamespace()
        with patch("app.chat.execution.run_shared_agent_turn",AsyncMock()) as work:
            await execute_controlled_turn(db,request,{"id":"agent"},"interaction",first=True)
        work.assert_awaited_once_with(
            db,request,{"id":"agent"},"interaction","web_portal",is_first_turn=True)

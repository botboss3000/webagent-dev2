from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch


class RecoveryErrorDetailTests(unittest.TestCase):
    def test_exhausted_notice_uses_concise_resume_copy(self):
        from app.agent import runner

        session_id = "session-1"
        db = Mock()
        db.run_state_get = AsyncMock(return_value={
            "session_id": session_id,
            "user_id": "admin",
            "agent_id": "agent-1",
            "origin": "web",
            "stop_cause": "crash",
            "resume_attempts": 3,
            "error": "LLM call failed: Connection error.",
        })
        db.run_state_mark_failed = AsyncMock()
        db.next_session_seq = AsyncMock(return_value=42)
        db.insert_interaction = AsyncMock()
        manager = Mock()
        manager.is_running.return_value = False

        with patch("app.db.get_db", return_value=db), \
             patch("app.agent.run_manager.get_run_manager", return_value=manager), \
             patch.object(runner, "_classify_resume", AsyncMock(return_value=("ok", ""))), \
             patch.object(runner, "_effective_max", return_value=3):
            launched = asyncio.run(runner.resume_one(session_id))

        self.assertFalse(launched)
        db.run_state_mark_failed.assert_awaited_once_with(session_id, None)
        content = db.insert_interaction.await_args.kwargs["content"]
        self.assertEqual(
            content,
            "Run failed after 3 recovery attempts\n"
            "Cause: crash\n"
            "Send a message to resume this session",
        )


    def test_successful_resume_persists_visible_recovery_tool(self):
        from app.agent import runner

        session_id = "session-2"
        db = Mock()
        db.run_state_get = AsyncMock(return_value={
            "session_id": session_id,
            "user_id": "admin",
            "agent_id": "agent-1",
            "turn_id": "turn-1",
            "origin": "web",
            "stop_cause": "server_restart",
            "resume_attempts": 0,
            "error": "Server restarted while this run was in progress.",
        })
        db.run_state_claim_for_resume = AsyncMock(return_value=True)
        db.next_session_seq = AsyncMock(return_value=43)
        db.insert_interaction = AsyncMock()
        manager = Mock()
        manager.is_running.return_value = False

        with patch("app.db.get_db", return_value=db), \
             patch("app.agent.run_manager.get_run_manager", return_value=manager), \
             patch.object(runner, "_classify_resume", AsyncMock(return_value=("ok", ""))), \
             patch.object(runner, "_effective_max", return_value=3), \
             patch.object(runner, "_probe_provider_for_run", AsyncMock(return_value=True)), \
             patch.object(runner, "get_resume_builder", return_value=Mock()), \
             patch.object(runner, "run_supervised_turn", AsyncMock()), \
             patch("app.agent.diagnostics.record_run_lifecycle"):
            launched = asyncio.run(runner.resume_one(session_id, reason="boot"))

        self.assertTrue(launched)
        tool_call = db.insert_interaction.await_args.kwargs
        self.assertEqual(tool_call["role"], "tool")
        self.assertEqual(tool_call["tool_name"], "resume_session")
        self.assertEqual(tool_call["source"], "system:recovery")
        self.assertEqual(tool_call["parent_id"], "turn-1")
        self.assertEqual(tool_call["metadata"],
                         '{"_synth_tool": true, "args": {"attempt": 1, "max_attempts": 3, "cause": "server_restart", "trigger": "boot"}, "duration_ms": 0, "error": false}')

    def test_recovery_notice_uses_active_buffer_sequence(self):
        """A recovery row cannot reuse a sequence reserved by a live stream."""
        from app.agent.run_buffer import RunBufferRegistry

        async def scenario():
            db = Mock()
            db.next_session_seq = AsyncMock(return_value=10)
            db.persist_session_manifest_seq = AsyncMock()
            registry = RunBufferRegistry()
            buffer = await registry.start_turn("session-3", "admin", "turn-3", db)
            first_seq, _ = buffer.next_seq()
            recovery_seq = await registry.reserve_external_seq("session-3", db)
            return first_seq, recovery_seq, db

        first_seq, recovery_seq, db = asyncio.run(scenario())
        self.assertEqual(first_seq, 10)
        self.assertEqual(recovery_seq, 11)
        db.next_session_seq.assert_awaited_once_with("session-3")
        db.persist_session_manifest_seq.assert_awaited_once_with("session-3", 12)


if __name__ == "__main__":
    unittest.main()

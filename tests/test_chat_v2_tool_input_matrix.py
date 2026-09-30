"""V2 publication and input-boundary checks for tools and attachments."""
import base64
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from types import SimpleNamespace
from app.agent.loop import _stream_agent_events_impl
from app.chat.context import current
from app.db.local import LocalBackend
from app.tools.loader import ToolInfo

from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.chat.api import router
from app.chat.contracts import Send
from app.chat.repository import ChatRepository
from app.chat.runtime import Execution, _settle_execution
from app.db.schema import render_plane


class Cipher:
    level = "field"

    def __init__(self):
        self.key = Fernet(Fernet.generate_key())

    async def encrypt(self, owner, value):
        return self.key.encrypt((owner + "\0" + value).encode()).decode()

    async def decrypt(self, owner, value):
        decoded = self.key.decrypt(value.encode()).decode()
        prefix = owner + "\0"
        if not decoded.startswith(prefix):
            raise PermissionError("Wrong owner")
        return decoded[len(prefix):]


class ToolAndInputMatrixTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "user.db"

        def connect():
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=5000")
            return conn

        self.connect = connect
        conn = connect()
        conn.executescript(render_plane("user", "sqlite"))
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,agent_id,status,metadata) "
            "VALUES ('session','admin','Test','agent','active','{}')"
        )
        conn.commit()
        conn.close()
        self.repo = ChatRepository("admin", connect=connect, cipher=Cipher())
        self.policy = {"require_flag": False, "require_notification": False}

    def tearDown(self):
        self.tmp.cleanup()

    async def accepted_run(self, text="Run a tool", cmid="input-test"):
        receipt = await self.repo.accept(
            "session", "admin", Send(client_message_id=cmid, text=text), self.policy,
        )
        generation = self.repo.claim(receipt["run_id"], "matrix-worker")
        await self.repo.consume(receipt["run_id"], generation)
        return receipt["run_id"], generation

    async def test_approval_style_interactive_tool_is_private_and_explicitly_unavailable(self):
        run_id, generation = await self.accepted_run()
        execution = Execution(self.repo, run_id, generation)
        await execution.observe({
            "type": "tool_call", "tool": "request_approval",
            "args": {"operation": "publish", "secret": "private-approval-data"},
        })
        await execution.observe({
            "type": "tool_result", "tool": "request_approval",
            "result": {"ui": "approval_prompt", "approval_id": "private-id",
                       "summary": "Publish changes?"},
        })

        snapshot = await self.repo.snapshot("session")
        self.assertEqual([item["kind"] for item in snapshot["items"]], ["user", "notice"])
        self.assertIn("unavailable", snapshot["items"][-1]["payload"]["text"])
        activity = await self.repo.activity("session")
        self.assertTrue(any(event["kind"] == "tool_call" for event in activity))
        self.assertTrue(any(event["kind"] == "tool_result" for event in activity))
        self.assertNotIn("private-approval-data", json.dumps(snapshot))
        self.assertNotIn("private-id", json.dumps(snapshot))

    async def test_failed_tool_result_stays_in_private_activity_and_run_can_finish(self):
        run_id, generation = await self.accepted_run(cmid="tool-failure")
        execution = Execution(self.repo, run_id, generation)
        await execution.observe({
            "type": "tool_call", "tool": "safe_read", "tool_call_id": "call-1",
            "args": {"path": "README.md"},
        })
        await execution.observe({
            "type": "tool_result", "tool": "safe_read", "tool_call_id": "call-1",
            "result": "Permission denied: internal path /private/key",
            "error": True, "error_type": "permission_error", "recoverable": True,
        })
        await execution.observe({
            "type": "response", "content": "I could not read that file.",
            "terminal_candidate": True,
        })
        await execution.flush()

        snapshot = await self.repo.snapshot("session")
        self.assertEqual([item["kind"] for item in snapshot["items"]], ["user", "answer"])
        self.assertEqual(snapshot["items"][-1]["payload"]["text"], "I could not read that file.")
        self.assertNotIn("internal path", json.dumps(snapshot))
        activity = await self.repo.activity("session")
        result = next(event for event in activity if event["kind"] == "tool_result")
        self.assertTrue(result["payload"]["error"])
        self.assertIn("internal path", json.dumps(result["payload"]))
        self.assertEqual((await self.repo.run(run_id))["state"], "completed")

    async def test_interrupted_tool_effect_fails_run_without_replaying_or_answering(self):
        run_id, generation = await self.accepted_run(cmid="tool-interrupted")
        execution = Execution(self.repo, run_id, generation)
        await execution.observe({
            "type": "tool_call", "tool": "write_file", "tool_call_id": "call-write",
            "args": {"path": "notes.txt"},
        })
        # A crash after dispatch leaves no confirmed result. V2 must fail closed.
        await _settle_execution(execution)

        self.assertEqual((await self.repo.run(run_id))["state"], "failed")
        snapshot = await self.repo.snapshot("session")
        self.assertEqual([item["kind"] for item in snapshot["items"]], ["user", "failure"])
        self.assertIn("unconfirmed tool result", snapshot["items"][-1]["payload"]["text"])
        activity = await self.repo.activity("session")
        self.assertFalse(any(event["kind"] == "tool_result" for event in activity))

    async def test_v2_send_accepts_owned_attachment_ids_and_rejects_other_users(self):
        api = FastAPI()
        api.include_router(router)
        db = type("AttachmentDB", (), {})()
        db.get_attachment = AsyncMock(side_effect=lambda attachment_id: {
            "id": attachment_id, "user_id": "admin", "mime_type": "image/png",
            "storage_provider": "browser", "storage_path": None,
        } if attachment_id == "owned-image" else {
            "id": attachment_id, "user_id": "someone-else", "mime_type": "image/png",
        })
        common_patches = (
            patch("app.chat.api.scope", new=AsyncMock(return_value=(
                "admin", self.repo, {"id": "session", "agent_id": "agent"},
            ))),
            patch("app.db.get_db", return_value=db),
            patch("app.chat.execution._require_chat_agent_access",
                  new=AsyncMock(return_value={"id": "agent"})),
            patch("app.chat.execution._enforce_billing_access", new=AsyncMock()),
            patch("app.api.rate_limit.enforce_tier_chat", new=AsyncMock()),
            patch("app.chat.runtime.resolve_policy", return_value=self.policy),
            patch("app.chat.runtime.schedule"),
        )
        with common_patches[0], common_patches[1], common_patches[2], common_patches[3], \
                common_patches[4], common_patches[5], common_patches[6]:
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
                good = await client.post("/api/v2/chat/sessions/session/messages", json={
                    "client_message_id": "attachment-good", "text": "Describe this image",
                    "attachment_ids": ["owned-image"],
                })
                bad = await client.post("/api/v2/chat/sessions/session/messages", json={
                    "client_message_id": "attachment-bad", "text": "Read this image",
                    "attachment_ids": ["other-users-image"],
                })
        self.assertEqual(good.status_code, 202, good.text)
        self.assertEqual(bad.status_code, 409, bad.text)
        accepted = await self.repo.snapshot("session")
        user_item = next(item for item in accepted["items"] if item["kind"] == "user")
        self.assertEqual(user_item["payload"]["attachment_ids"], ["owned-image"])
        self.assertNotIn("other-users-image", json.dumps(accepted))

    async def test_image_attachment_is_inlined_as_provider_image_content(self):
        from app.agent.prompts import build_user_message_content

        image_bytes = b"\x89PNG\r\n\x1a\ncontrolled-test-image"
        attachment = {
            "id": "image-1", "mime_type": "image/png", "size_bytes": len(image_bytes),
            "storage_provider": "local", "storage_path": "controlled-test-path",
        }
        with patch("app.db.attachments.read_file", new=AsyncMock(return_value=image_bytes)):
            content = await build_user_message_content("What is shown?", [attachment])

        self.assertEqual(content[0], {"type": "text", "text": "What is shown?"})
        self.assertEqual(content[1]["type"], "image_url")
        expected = "data:image/png;base64," + base64.b64encode(image_bytes).decode("ascii")
        self.assertEqual(content[1]["image_url"]["url"], expected)
    async def test_real_shared_loop_tool_failure_is_journaled_before_final_answer(self):
        session_id = "loop-session"
        db = LocalBackend(str(Path(self.tmp.name) / "loop.db"), seed=False)
        conn = db._get_conn()
        conn.executescript(render_plane("user", "sqlite"))
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,agent_id,status,metadata) "
            "VALUES (?,?,?,?,'active',?)",
            (session_id, "admin", "Loop failure", "agent", json.dumps({"execution_mode": "auto"})),
        )
        conn.commit()
        conn.close()
        repo = ChatRepository("admin", connect=db._get_conn, cipher=Cipher())
        api = FastAPI()
        api.include_router(router)
        with (
            patch("app.chat.api.scope", new=AsyncMock(return_value=(
                "admin", repo, {"id": session_id, "agent_id": "agent"},
            ))),
            patch("app.db.get_db", return_value=db),
            patch("app.chat.execution._require_chat_agent_access",
                  new=AsyncMock(return_value={"id": "agent"})),
            patch("app.chat.execution._enforce_billing_access", new=AsyncMock()),
            patch("app.api.rate_limit.enforce_tier_chat", new=AsyncMock()),
            patch("app.chat.runtime.resolve_policy", return_value=self.policy),
            patch("app.chat.runtime.schedule"),
        ):
            async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
                response = await client.post(
                    f"/api/v2/chat/sessions/{session_id}/messages",
                    json={"client_message_id": "loop-tool-error", "text": "Read safely",
                          "execution_mode": "auto"},
                )
        self.assertEqual(response.status_code, 202, response.text)
        run_id = response.json()["run_id"]
        generation = repo.claim(run_id, "loop-worker")
        rows = await repo.consume(run_id, generation)
        await db.insert_interaction(
            "admin", session_id, role="user", content=rows[0]["text"],
            sender_id="admin", receiver_id="agent",
        )
        execution = Execution(repo, run_id, generation)
        calls = []

        async def fail_read(**kwargs):
            calls.append(kwargs)
            raise PermissionError("controlled read denied")

        class ScriptedStream:
            def __init__(self, chunk):
                self.chunk = chunk
                self.reasoning_items = []
                self.response_id = None

            def __aiter__(self):
                async def items():
                    yield self.chunk
                return items()

            async def close(self):
                return None

        call = SimpleNamespace(index=0, id="failed-read-1", function=SimpleNamespace(
            name="read_source", arguments="{}",
        ))
        tool_chunk = SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
            content=None, reasoning_content=None, tool_calls=[call],
        ))])
        text_chunk = SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
            content="The read was denied safely.", reasoning_content=None, tool_calls=None,
        ))])
        chunks = [tool_chunk, text_chunk]

        async def open_stream(create_kwargs, timeout_s, client=None):
            return ScriptedStream(chunks.pop(0))

        tools = {"read_source": ToolInfo(
            name="read_source", handler=fail_read,
            parameters={"type": "object", "properties": {}, "required": []},
        )}
        token = current.set(execution)
        try:
            with (
                patch("app.tools.loader.load_tools", new=AsyncMock(return_value=tools)),
                patch("app.admin.settings.apply_provider_for_run", new=AsyncMock(return_value={
                    "provider": "test", "base_url": "https://provider.example/v1",
                    "api_key": "test-key", "model": "test-model",
                })),
                patch("app.agent.contracts.parse_contract", return_value={"require_message_visibility": True}),
                patch("app.agent.contract_permissions.active_profile", return_value=None),
                patch("app.agent.loop._open_stream", new=open_stream),
                patch("app.agent.loop._record_billing_usage", new=AsyncMock(return_value={})),
                patch("app.agent.loop._agent_profile_visitor", return_value=None),
                patch("app.agent.loop._schedule_output_closer"),
                patch("app.agent.session_changes.capture_tool_state_async", new=AsyncMock(return_value={})),
                patch("app.agent.session_changes.record_tool_delta", new=AsyncMock(return_value=[])),
            ):
                async for event in _stream_agent_events_impl(
                    user_id="admin", session_id=session_id, user_message=rows[0]["text"],
                    system_prompt="", agent_id="", history=[], parent_interaction_id="turn-error",
                    db=db, execution_mode="auto", max_turns=8, run_turn_id="turn-error",
                ):
                    await execution.observe(event)
            await execution.flush()
        finally:
            current.reset(token)

        self.assertEqual(calls, [{}])
        self.assertFalse(chunks)
        self.assertEqual((await repo.run(run_id))["state"], "completed")
        snapshot = await repo.snapshot(session_id)
        self.assertEqual([item["kind"] for item in snapshot["items"]], ["user", "answer"])
        self.assertEqual(snapshot["items"][-1]["payload"]["text"], "The read was denied safely.")
        activity = await repo.activity(session_id)
        result = next(event for event in activity if event["kind"] == "tool_result")
        self.assertTrue(result["payload"]["error"])
        self.assertIn("controlled read denied", json.dumps(result["payload"]))
        self.assertNotIn("controlled read denied", json.dumps(snapshot))



if __name__ == "__main__":
    unittest.main()

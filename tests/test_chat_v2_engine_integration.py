"""Exercise a real agent-loop turn under the controlled chat publication boundary."""
import json
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from cryptography.fernet import Fernet
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.agent.loop import _stream_agent_events_impl
from app.chat.api import router
from app.chat.context import current
from app.chat.repository import ChatRepository
from app.chat.runtime import Execution
from app.db.local import LocalBackend
from app.db.schema import render_plane
from app.tools.loader import ToolInfo


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


class Stream:
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


def tool_chunk():
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
        content=None, reasoning_content=None, tool_calls=[SimpleNamespace(
            index=0, id="read-1", function=SimpleNamespace(name="read_source", arguments="{}"),
        )],
    ))])


def text_chunk(text):
    return SimpleNamespace(usage=None, choices=[SimpleNamespace(delta=SimpleNamespace(
        content=text, reasoning_content=None, tool_calls=None,
    ))])


class ControlledEngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_loop_tool_and_answer_use_v2_publication(self):
        session_id = "session-" + uuid.uuid4().hex
        turn_id = "turn-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            db = LocalBackend(str(Path(directory) / "user.db"), seed=False)
            conn = db._get_conn()
            conn.executescript(render_plane("user", "sqlite"))
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,agent_id,status,metadata) "
                "VALUES (?,?,?,?,'active',?)",
                (session_id, "admin", "Test", "agent", json.dumps({"execution_mode": "auto"})),
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
                patch("app.chat.runtime.resolve_policy", return_value={
                    "require_flag": False, "require_notification": False,
                }),
                patch("app.chat.runtime.schedule"),
            ):
                async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
                    response = await client.post(f"/api/v2/chat/sessions/{session_id}/messages", json={
                        "client_message_id": "client-1", "text": "Read the source",
                        "execution_mode": "auto",
                    })
            self.assertEqual(response.status_code, 202)
            receipt = response.json()
            generation = repo.claim(receipt["run_id"], "worker")
            rows = await repo.consume(receipt["run_id"], generation)
            self.assertEqual([row["text"] for row in rows], ["Read the source"])
            await db.insert_interaction(
                "admin", session_id, role="user", content=rows[0]["text"],
                sender_id="admin", receiver_id="agent",
            )
            execution = Execution(repo, receipt["run_id"], generation)
            actions = [tool_chunk(), text_chunk("The source is healthy.")]
            tool_calls = []

            async def read_source(**kwargs):
                tool_calls.append(kwargs)
                return "source result"

            async def open_stream(create_kwargs, timeout_s, client=None):
                return Stream(actions.pop(0))

            tools = {"read_source": ToolInfo(
                name="read_source", handler=read_source,
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
                        system_prompt="", agent_id="", history=[], parent_interaction_id=turn_id,
                        db=db, execution_mode="auto", max_turns=8, run_turn_id=turn_id,
                    ):
                        await execution.observe(event)
                await execution.flush()
            finally:
                current.reset(token)
            self.assertEqual(tool_calls, [{}])
            snapshot = await repo.snapshot(session_id)
            self.assertEqual([item["kind"] for item in snapshot["items"]], ["user", "answer"])
            self.assertEqual(snapshot["items"][-1]["payload"]["text"], "The source is healthy.")
            self.assertEqual(snapshot["items"], [event["item"] for event in await repo.changes(session_id)])
            with patch("app.chat.api.scope", new=AsyncMock(return_value=(
                "admin", repo, {"id": session_id, "agent_id": "agent"},
            ))):
                async with AsyncClient(transport=ASGITransport(app=api), base_url="http://test") as client:
                    public_snapshot = await client.get(f"/api/v2/chat/sessions/{session_id}")
                    public_events = await client.get(f"/api/v2/chat/sessions/{session_id}/events?after=0")
            self.assertEqual(public_snapshot.status_code, 200)
            self.assertEqual(public_snapshot.json()["items"], snapshot["items"])
            self.assertEqual(public_events.status_code, 200)
            self.assertEqual(public_events.text.count("event: publication"), 2)
            self.assertNotIn("source result", public_events.text)
            self.assertEqual((await repo.run(receipt["run_id"]))["state"], "completed")
            activity = await repo.activity(session_id)
            self.assertTrue(any(event["kind"] == "tool_result" for event in activity))
            self.assertNotIn("source result", json.dumps(snapshot))
            conn = db._get_conn()
            try:
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM session_runs WHERE session_id=?", (session_id,),
                ).fetchone()[0], 0)
            finally:
                conn.close()

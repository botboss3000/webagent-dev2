"""V2 agent and execution-mode selection through acceptance and publication."""
import tempfile
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.chat.api import router
from app.chat.engine_adapter import prepare_controlled_turn, execute_controlled_turn
from app.chat.repository import ChatRepository
from app.chat.runtime import Execution
from app.db.local import LocalBackend
from app.db.schema import render_plane


class Cipher:
    level = "field"

    async def encrypt(self, owner, value):
        return value

    async def decrypt(self, owner, value):
        return value


class AgentModeMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def make_store(self, agent_id):
        directory = tempfile.TemporaryDirectory()
        session_id = "session-" + uuid.uuid4().hex
        db = LocalBackend(str(Path(directory.name) / "user.db"), seed=False)
        conn = db._get_conn()
        conn.executescript(render_plane("user", "sqlite"))
        conn.execute(
            "INSERT INTO sessions(id,user_id,title,agent_id,status) VALUES (?,?,?,?,'active')",
            (session_id, "admin", "Matrix", agent_id),
        )
        conn.commit()
        conn.close()
        repo = ChatRepository("admin", connect=db._get_conn, cipher=Cipher())
        return directory, db, session_id, repo

    async def accepted_turn(self, agent_id, mode):
        directory, db, session_id, repo = await self.make_store(agent_id)
        agent = {"id": agent_id, "name": agent_id}
        app = FastAPI()
        app.include_router(router)
        with (
            patch("app.chat.api.scope", new=AsyncMock(return_value=(
                "admin", repo, {"id": session_id, "agent_id": agent_id},
            ))),
            patch("app.db.get_db", return_value=db),
            patch("app.chat.execution._require_chat_agent_access", new=AsyncMock(return_value=agent)),
            patch("app.chat.execution._enforce_billing_access", new=AsyncMock()),
            patch("app.api.rate_limit.enforce_tier_chat", new=AsyncMock()),
            patch("app.chat.runtime.resolve_policy", return_value={
                "require_flag": False, "require_notification": False,
            }),
            patch("app.chat.runtime.schedule"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    f"/api/v2/chat/sessions/{session_id}/messages",
                    json={"client_message_id": "matrix", "text": "Run selected configuration",
                          "execution_mode": mode},
                )
        self.assertEqual(response.status_code, 202)
        receipt = response.json()
        generation = repo.claim(receipt["run_id"], "matrix-worker")
        rows = await repo.consume(receipt["run_id"], generation)
        request = SimpleNamespace(
            user_id="admin", session_id=session_id, agent_id=agent_id,
            message=rows[0]["text"], attachment_ids=[], execution_mode=rows[0]["execution_mode"],
        )
        db_adapter = SimpleNamespace(
            is_session_dead=AsyncMock(return_value=False),
            fetch_agent_by_id_with_context=AsyncMock(return_value=agent),
            get_session_agent_id=AsyncMock(return_value=agent_id),
            is_session_participant=AsyncMock(return_value=True),
            insert_interaction=AsyncMock(return_value="interaction-" + agent_id),
        )
        mode_record = AsyncMock()
        with (
            patch("app.chat.execution._ensure_session", new=AsyncMock(return_value=False)),
            patch("app.chat.execution._enforce_agent_access_policy", new=AsyncMock()),
            patch("app.chat.execution._reset_agent_model_switcher", new=AsyncMock()),
            patch("app.chat.execution._record_session_execution_mode", new=mode_record),
        ):
            prepared_agent, interaction_id, first = await prepare_controlled_turn(db_adapter, request, rows)
        self.assertEqual(prepared_agent["id"], agent_id)
        self.assertEqual(interaction_id, "interaction-" + agent_id)
        self.assertFalse(first)
        self.assertEqual(mode_record.await_args.args[2], mode)
        self.assertEqual(db_adapter.insert_interaction.await_args.kwargs["receiver_id"], agent_id)
        with patch("app.chat.execution.run_shared_agent_turn", new=AsyncMock()) as shared:
            await execute_controlled_turn(db_adapter, request, prepared_agent, interaction_id, first=first)
        self.assertEqual(shared.await_args.args[2]["id"], agent_id)
        self.assertEqual(shared.await_args.args[1].execution_mode, mode)
        execution = Execution(repo, receipt["run_id"], generation)
        await execution.observe({"type": "response", "content": f"{agent_id}:{mode}",
                                 "terminal_candidate": True})
        await execution.flush()
        snapshot = await repo.snapshot(session_id)
        self.assertEqual([item["kind"] for item in snapshot["items"]], ["user", "answer"])
        self.assertEqual(snapshot["items"][-1]["payload"]["text"], f"{agent_id}:{mode}")
        self.assertEqual((await repo.run(receipt["run_id"]))["state"], "completed")
        directory.cleanup()

    async def test_agents_and_execution_modes_reach_shared_adapter_and_publication(self):
        for agent_id in ("shared_default", "research_helper"):
            for mode in ("ask", "plan", "auto", "wkspc"):
                with self.subTest(agent=agent_id, mode=mode):
                    await self.accepted_turn(agent_id, mode)

    async def test_agent_access_denial_prevents_acceptance(self):
        directory, db, session_id, repo = await self.make_store("restricted_agent")
        denied_agent = {"id": "restricted_agent", "user_mode": "register"}
        app = FastAPI()
        app.include_router(router)
        access_db = SimpleNamespace(fetch_agent_by_id_with_context=AsyncMock(return_value=denied_agent))
        with (
            patch("app.chat.api.scope", new=AsyncMock(return_value=(
                "admin", repo, {"id": session_id, "agent_id": "restricted_agent"},
            ))),
            patch("app.db.get_db", return_value=access_db),
            patch("app.agent.profiles.resolve_member", new=AsyncMock(return_value=None)),
            patch("app.chat.execution._enforce_billing_access", new=AsyncMock()) as billing,
            patch("app.api.rate_limit.enforce_tier_chat", new=AsyncMock()),
            patch("app.chat.runtime.schedule"),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    f"/api/v2/chat/sessions/{session_id}/messages",
                    json={"client_message_id": "denied", "text": "Should be rejected",
                          "execution_mode": "ask"},
                )
        self.assertEqual(response.status_code, 403)
        self.assertEqual((await repo.snapshot(session_id))["items"], [])
        billing.assert_not_awaited()
        directory.cleanup()
    async def test_model_role_selection_uses_entitled_roster_and_publishes_notice(self):
        directory, db, session_id, repo = await self.make_store("shared_default")
        app = FastAPI()
        app.include_router(router)
        access_db = SimpleNamespace(
            assert_session_owned=AsyncMock(),
            get_session_llm_override=AsyncMock(return_value=None),
            set_session_llm_override=AsyncMock(return_value={"type": "role", "role": "standard"}),
            add_model_switch_notice=AsyncMock(),
        )
        slots = {"slots": [{"type": "custom", "entry_id": "std-entry",
                            "roles": ["standard", "premium"], "position": 1}]}
        with (
            patch("app.chat.api.scope", new=AsyncMock(return_value=(
                "admin", repo, {"id": session_id, "agent_id": "shared_default"},
            ))),
            patch("app.auth.identity.assert_caller_is", new=AsyncMock(return_value="admin")),
            patch("app.chat.execution.get_db", return_value=access_db),
            patch("app.admin.settings.resolve_agent_models", new=AsyncMock(return_value=slots)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    f"/api/v2/chat/sessions/{session_id}/model",
                    json={"selection_type": "role", "role": "standard"},
                )
                rejected = await client.post(
                    f"/api/v2/chat/sessions/{session_id}/model",
                    json={"selection_type": "role", "role": "unentitled"},
                )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(rejected.status_code, 403, rejected.text)
        access_db.set_session_llm_override.assert_awaited_once()
        self.assertEqual(access_db.set_session_llm_override.await_args.args[1],
                         {"type": "role", "role": "standard"})
        access_db.add_model_switch_notice.assert_awaited_once()
        snapshot = await repo.snapshot(session_id)
        self.assertEqual([item["kind"] for item in snapshot["items"]], ["notice"])
        self.assertEqual(snapshot["items"][0]["payload"]["control"]["kind"], "model")

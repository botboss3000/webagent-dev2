"""Provider-path integration through V2 acceptance, journal, and publication.

All provider responses are scripted; this suite makes no network calls and uses
no configured provider credentials.
"""
import json
import tempfile
import unittest
import uuid
from contextlib import ExitStack
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
from app.chat.runtime import Execution, _settle_execution
from app.db.local import LocalBackend
from app.db.schema import render_plane


class _Cipher:
    level = "field"

    def __init__(self):
        self._key = Fernet(Fernet.generate_key())

    async def encrypt(self, owner, value):
        return self._key.encrypt((owner + "\0" + value).encode()).decode()

    async def decrypt(self, owner, value):
        value = self._key.decrypt(value.encode()).decode()
        prefix = owner + "\0"
        if not value.startswith(prefix):
            raise PermissionError("Wrong owner")
        return value[len(prefix):]


class _Stream:
    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.reasoning_items = []
        self.response_id = None

    def __aiter__(self):
        async def items():
            for chunk in self._chunks:
                yield chunk
        return items()

    async def close(self):
        return None


class _FailingStream:
    def __init__(self, chunk, error):
        self._chunk = chunk
        self._error = error
        self.reasoning_items = []
        self.response_id = None

    def __aiter__(self):
        async def items():
            yield self._chunk
            raise self._error
        return items()

    async def close(self):
        return None


def _chunk(*, text=None, reasoning=None):
    return SimpleNamespace(
        usage=None,
        choices=[SimpleNamespace(
            finish_reason=None,
            delta=SimpleNamespace(
                content=text, reasoning_content=reasoning, tool_calls=None,
            ),
        )],
    )


class V2ProviderMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def _run(self, provider_config, *, stream_factory=None, responses_factory=None,
                   auth_factory=None):
        session_id = "provider-session-" + uuid.uuid4().hex
        turn_id = "provider-turn-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory() as directory:
            db = LocalBackend(str(Path(directory) / "user.db"), seed=False)
            conn = db._get_conn()
            conn.executescript(render_plane("user", "sqlite"))
            conn.execute(
                "INSERT INTO sessions(id,user_id,title,agent_id,status,metadata) "
                "VALUES (?,?,?,?,'active',?)",
                (session_id, "admin", "Provider matrix", "agent",
                 json.dumps({"execution_mode": "auto"})),
            )
            conn.commit()
            conn.close()
            repo = ChatRepository("admin", connect=db._get_conn, cipher=_Cipher())
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
                async with AsyncClient(transport=ASGITransport(app=api),
                                       base_url="http://test") as client:
                    response = await client.post(
                        f"/api/v2/chat/sessions/{session_id}/messages",
                        json={"client_message_id": "provider-client",
                              "text": "Answer using the selected provider path.",
                              "execution_mode": "auto"},
                    )
            self.assertEqual(response.status_code, 202)
            receipt = response.json()
            generation = repo.claim(receipt["run_id"], "provider-test-worker")
            rows = await repo.consume(receipt["run_id"], generation)
            await db.insert_interaction(
                "admin", session_id, role="user", content=rows[0]["text"],
                sender_id="admin", receiver_id="agent",
            )
            execution = Execution(repo, receipt["run_id"], generation)
            open_calls = []
            responses_calls = []

            async def open_stream(kwargs, timeout_s, client=None):
                open_calls.append(dict(kwargs))
                if stream_factory:
                    return await stream_factory(kwargs, timeout_s, client)
                return _Stream([_chunk(text="V2 provider answer.")])

            async def open_responses(**kwargs):
                responses_calls.append(kwargs)
                if responses_factory:
                    return await responses_factory(**kwargs)
                return _Stream([_chunk(text="V2 provider answer.")])

            async def transport_auth(**kwargs):
                if auth_factory:
                    return await auth_factory(**kwargs)
                return {"access_token": "scripted-access-token"}

            token = current.set(execution)
            try:
                with ExitStack() as patches:
                    patches.enter_context(patch(
                        "app.tools.loader.load_tools", new=AsyncMock(return_value={}),
                    ))
                    patches.enter_context(patch(
                        "app.admin.settings.apply_provider_for_run",
                        new=AsyncMock(return_value=provider_config),
                    ))
                    patches.enter_context(patch(
                        "app.agent.contracts.parse_contract",
                        return_value={"require_message_visibility": True},
                    ))
                    patches.enter_context(patch(
                        "app.agent.contract_permissions.active_profile", return_value=None,
                    ))
                    patches.enter_context(patch(
                        "app.agent.loop._open_stream", new=open_stream,
                    ))
                    patches.enter_context(patch(
                        "app.agent.loop.open_responses_stream", new=open_responses,
                    ))
                    patches.enter_context(patch(
                        "app.openai_subscription.transport_auth", new=transport_auth,
                    ))
                    patches.enter_context(patch(
                        "app.agent.loop._record_billing_usage",
                        new=AsyncMock(return_value={}),
                    ))
                    patches.enter_context(patch(
                        "app.agent.loop._agent_profile_visitor", return_value=None,
                    ))
                    patches.enter_context(patch(
                        "app.agent.loop._schedule_output_closer",
                    ))
                    patches.enter_context(patch(
                        "app.agent.session_changes.capture_tool_state_async",
                        new=AsyncMock(return_value={}),
                    ))
                    patches.enter_context(patch(
                        "app.agent.session_changes.record_tool_delta",
                        new=AsyncMock(return_value=[]),
                    ))
                    events = []
                    async for event in _stream_agent_events_impl(
                        user_id="admin", session_id=session_id,
                        user_message=rows[0]["text"], system_prompt="",
                        agent_id="agent", history=[], parent_interaction_id=turn_id,
                        db=db, execution_mode="auto", max_turns=8, run_turn_id=turn_id,
                    ):
                        events.append(dict(event))
                        await execution.observe(event)
                    await execution.flush()
                    await _settle_execution(execution)
            finally:
                current.reset(token)

            snapshot = await repo.snapshot(session_id)
            run_state = await repo.run(receipt["run_id"])
            return {
                "repo": repo, "run_id": receipt["run_id"],
                "session_id": session_id, "events": events,
                "open_calls": open_calls, "responses_calls": responses_calls,
                "snapshot": snapshot, "run_state": run_state,
            }

    async def test_streamed_reasoning_stays_private_and_answer_is_published(self):
        async def stream_factory(*_args):
            return _Stream([
                _chunk(reasoning="private chain-of-thought marker"),
                _chunk(text="A user-facing answer."),
            ])

        result = await self._run({
            "provider": "test", "base_url": "https://provider.invalid/v1",
            "api_key": "scripted", "model": "test-reasoning",
        }, stream_factory=stream_factory)
        snapshot = result["snapshot"]
        serialized = json.dumps(snapshot)
        self.assertIn("A user-facing answer.", serialized)
        self.assertNotIn("private chain-of-thought marker", serialized)
        self.assertEqual(snapshot["items"][-1]["kind"], "answer")
        self.assertEqual(result["run_state"]["state"], "completed")
        self.assertTrue(any(event.get("type") == "stream" for event in result["events"]))

    async def test_provider_open_timeout_fails_v2_run_without_public_answer(self):
        async def stream_factory(*_args):
            raise TimeoutError("scripted provider connect timeout")

        result = await self._run({
            "provider": "test", "base_url": "https://provider.invalid/v1",
            "api_key": "scripted", "model": "test-timeout",
        }, stream_factory=stream_factory)
        snapshot = result["snapshot"]
        self.assertFalse(any(item["kind"] == "answer" for item in snapshot["items"]))
        self.assertEqual(result["run_state"]["state"], "failed")
        self.assertTrue(any(event.get("type") == "error" for event in result["events"]))

    async def test_midstream_provider_disconnect_does_not_publish_partial_answer(self):
        async def stream_factory(*_args):
            return _FailingStream(
                _chunk(text="partial answer that must stay private"),
                ConnectionError("scripted midstream disconnect"),
            )

        result = await self._run({
            "provider": "test", "base_url": "https://provider.invalid/v1",
            "api_key": "scripted", "model": "test-midstream-error",
        }, stream_factory=stream_factory)
        snapshot = result["snapshot"]
        serialized = json.dumps(snapshot)
        self.assertNotIn("partial answer that must stay private", serialized)
        self.assertFalse(any(item["kind"] == "answer" for item in snapshot["items"]))
        self.assertEqual(result["run_state"]["state"], "failed")
        self.assertTrue(any(event.get("type") == "error" for event in result["events"]))

    async def test_deepseek_responses_rejection_falls_back_through_v2(self):
        async def responses_factory(**_kwargs):
            raise RuntimeError("HTTP 400 model does not support Responses API")

        async def stream_factory(*_args):
            return _Stream([_chunk(text="DeepSeek fallback answer.")])

        result = await self._run({
            "provider": "deepseek", "base_url": "https://api.deepseek.com/v1",
            "api_key": "scripted", "model": "deepseek-v4-flash-vision-exp",
            "reasoning_effort": "high",
        }, stream_factory=stream_factory, responses_factory=responses_factory)
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["items"][-1]["payload"]["text"],
                         "DeepSeek fallback answer.")
        self.assertEqual(result["run_state"]["state"], "completed")
        fallbacks = [event for event in result["events"]
                     if event.get("step") == "llm_transport_fallback"]
        self.assertEqual(len(fallbacks), 1)
        self.assertEqual(fallbacks[0]["to_transport"], "chat_completions")
        self.assertEqual(len(result["responses_calls"]), 1)
        self.assertEqual(len(result["open_calls"]), 1)

    async def test_subscription_responses_transport_uses_v2_publication(self):
        result = await self._run({
            "provider": "openai_subscription", "base_url": "codex://chatgpt",
            "model": "gpt-5.6-sol", "credential_scope": "platform",
            "entry_id": "scripted-entry",
        })
        snapshot = result["snapshot"]
        self.assertEqual(snapshot["items"][-1]["payload"]["text"],
                         "V2 provider answer.")
        self.assertEqual(result["run_state"]["state"], "completed")
        self.assertEqual(result["open_calls"], [])
        self.assertEqual(len(result["responses_calls"]), 1)
        request = result["responses_calls"][0]
        self.assertEqual(request["api_key"], "scripted-access-token")
        self.assertEqual(request["request_url"], "https://chatgpt.com/backend-api/codex/responses")
        self.assertFalse(request["body"]["store"])
        self.assertEqual(request["body"]["include"], ["reasoning.encrypted_content"])


if __name__ == "__main__":
    unittest.main()

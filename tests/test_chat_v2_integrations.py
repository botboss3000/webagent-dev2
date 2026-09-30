"""V2 integration boundaries use controlled chat authority."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.chat.api import router
from app.chat.contracts import Conflict, Send
from app.chat.runtime import submit


class ControlledIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_terminal_input_requires_an_active_owned_tunnel(self):
        app = FastAPI()
        app.include_router(router)
        db = object()
        with (
            patch("app.chat.api.scope", AsyncMock(return_value=("alice", None, {}))),
            patch("app.db.get_user_db", return_value=db),
            patch("app.agent.terminal_tunnel.route_user_line",
                  AsyncMock(side_effect=[True, False])) as route,
        ):
            async with AsyncClient(transport=ASGITransport(app=app),
                                   base_url="http://test") as client:
                accepted = await client.post(
                    "/api/v2/chat/sessions/s/terminal/input", json={"text": "help"})
                inactive = await client.post(
                    "/api/v2/chat/sessions/s/terminal/input", json={"text": "help"})
                oversized = await client.post(
                    "/api/v2/chat/sessions/s/terminal/input",
                    json={"text": "x" * 32001})
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json(), {"status": "sent"})
        self.assertEqual(inactive.status_code, 409)
        self.assertEqual(oversized.status_code, 422)
        self.assertEqual(route.await_count, 2)
        route.assert_any_await(db, "alice", "s", "help")

    async def test_oversized_guest_message_is_rejected_before_metering(self):
        repo = SimpleNamespace(receipt_for=lambda *_: None)
        with (
            patch("app.db.get_db", return_value=object()),
            patch("app.chat.execution._require_chat_agent_access",
                  AsyncMock(return_value={"id": "agent"})),
            patch("app.chat.execution._enforce_billing_access",
                  AsyncMock()) as billing,
        ):
            with self.assertRaises(Conflict):
                await submit(object(), "anon_guest", repo,
                             {"id": "s", "agent_id": "agent"},
                             Send(client_message_id="one", text="x" * 32001))
        billing.assert_not_awaited()

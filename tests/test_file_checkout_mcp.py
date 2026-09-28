import asyncio
from unittest.mock import AsyncMock, patch

from app.mcp.bridge import execute_mcp_tool, resolve_tools_for_engine


class _AgentDb:
    async def get_agent_by_id(self, agent_id):
        return {"id": agent_id, "metadata": {"contract": {"require_file_checkout": True}}}


def test_checkout_tools_are_forced_into_mcp_when_broad_exposure_is_empty():
    async def scenario():
        with (
            patch("app.mcp.bridge.load_tools", new=AsyncMock(return_value={})),
            patch("app.db.get_db", return_value=_AgentDb()),
        ):
            return await resolve_tools_for_engine(
                user_id="u1", agent_id="a1", session_id="s1",
                execution_mode="auto", allowed_tools=[],
            )

    tools = asyncio.run(scenario())
    assert {
        "checkout_files", "checkin_files",
        "abandon_file_checkouts", "file_checkout_status",
    } <= set(tools)


def test_mcp_rejects_a_session_not_owned_by_the_calling_user():
    handler = AsyncMock(return_value="must not run")

    class _Db:
        async def assert_session_owned(self, user_id, session_id):
            raise PermissionError("not owned")

    result = asyncio.run(execute_mcp_tool(
        "checkout_files", {"paths": ["README.md"]},
        {"checkout_files": (handler, {})},
        "other-user", "session-1", db=_Db(),
    ))
    assert result["isError"] is True
    assert "not owned" in result["content"][0]["text"]
    handler.assert_not_awaited()

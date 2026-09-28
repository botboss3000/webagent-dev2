import asyncio

from app.tools import loader


async def _noop(**_kwargs):
    return None


def _tool(name: str) -> loader.ToolInfo:
    return loader.ToolInfo(
        name=name,
        handler=_noop,
        parameters={"type": "object", "properties": {}, "required": []},
    )


def test_anonymous_runtime_has_only_plain_chat_utilities(monkeypatch):
    async def assembled(*_args, **_kwargs):
        return {
            name: _tool(name)
            for name in (
                "get_time", "get_date", "calculate", "load_ability",
                "load_skill", "read_own_prompt", "create_tool",
                "run_command", "list_my_agents",
            )
        }

    monkeypatch.setattr(loader._tool_loader, "load_tools", assembled)
    tools = asyncio.run(loader.load_tools(
        "anon_test", agent_id="shared_default", gate_caller_access=True,
    ))
    assert set(tools) == {"get_time", "get_date", "calculate"}


def test_registered_runtime_is_not_subject_to_anonymous_pruning(monkeypatch):
    async def assembled(*_args, **_kwargs):
        return {"load_ability": _tool("load_ability"), "get_time": _tool("get_time")}

    monkeypatch.setattr(loader._tool_loader, "load_tools", assembled)
    tools = asyncio.run(loader.load_tools(
        "registered-user", agent_id="shared_default", gate_caller_access=True,
    ))
    assert set(tools) == {"load_ability", "get_time"}


def test_funded_public_agent_gets_only_explicit_public_tools(monkeypatch):
    async def assembled(*_args, **_kwargs):
        return {
            "get_time": _tool("get_time"),
            "web_search": _tool("web_search"),
            "run_command": _tool("run_command"),
        }

    class DB:
        async def get_agent_by_id(self, _agent_id):
            return {"id": "agent-1", "metadata": {"public_access": {
                "funding": {"mode": "dedicated_key"},
                "capabilities": {"tools": ["web_search"]},
            }}}

    monkeypatch.setattr(loader._tool_loader, "load_tools", assembled)
    monkeypatch.setattr(loader, "get_db", lambda: DB())
    tools = asyncio.run(loader.load_tools(
        "anon_test", agent_id="agent-1", gate_caller_access=True,
    ))
    assert set(tools) == {"get_time", "web_search"}

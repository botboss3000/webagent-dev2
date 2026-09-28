import asyncio
from types import SimpleNamespace

import pytest

from app.agent import file_checkout
from app.db.local import LocalBackend
from app.tools.execution_context import ToolExecutionContext, tool_execution_scope
from plugins.admin import source_tools


def _context():
    return ToolExecutionContext(
        user_id="u1", session_id="s1", turn_key="t1", tool_name="write_source",
        tool_call_id="tc1", authority_mode="server", idempotency_key=None,
        side_effecting=True, agent_id="a1", execution_mode="auto",
        require_file_checkout=True, write_capable_mode=True,
    )


def test_source_tools_require_checkout_and_complete_lifecycle(tmp_path, monkeypatch):
    backend = LocalBackend(str(tmp_path / "state.db"), seed=False)
    monkeypatch.setattr(file_checkout, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(file_checkout, "get_db", lambda: backend)
    monkeypatch.setattr(source_tools, "_PROJECT_ROOT", tmp_path)

    async def write(path, content, backup):
        target = tmp_path / path
        target.write_text(content)
        return SimpleNamespace(message="written", backup_path=None)

    async def read(path):
        target = tmp_path / path
        return SimpleNamespace(content=target.read_text())

    monkeypatch.setattr(source_tools, "_do_write", write)
    monkeypatch.setattr(source_tools, "_do_read", read)

    tools = {}
    source_tools.inject_source_tools(tools, "u1")

    async def scenario():
        with tool_execution_scope(_context()):
            with pytest.raises(file_checkout.CheckoutError):
                await tools["write_source"].handler(path="fixture.txt", content="first")
            acquired = await tools["checkout_files"].handler(paths=["fixture.txt"])
            assert acquired["files"][0]["fingerprint"]["exists"] is False
            assert await tools["write_source"].handler(path="fixture.txt", content="first") == "written"
            output = await tools["read_source"].handler(path="fixture.txt")
            assert "first" in output
            status = await tools["file_checkout_status"].handler(paths=["fixture.txt"])
            assert status["files"][0]["status"] in {"changed", "validated"}
            checked_in = await tools["checkin_files"].handler(
                paths=["fixture.txt"], validation_note="read back"
            )
            assert checked_in["files"][0]["status"] == "checked_in"

    asyncio.run(scenario())


def test_broad_tool_schema_declares_checkout_paths():
    tools = {}
    source_tools.inject_source_tools(tools, "u1")
    for name in ("run_command", "run_python"):
        props = tools[name].parameters["properties"]
        assert props["checkout_paths"]["items"]["type"] == "string"


def test_broad_tool_flags_undeclared_mutation(tmp_path, monkeypatch):
    backend = LocalBackend(str(tmp_path / "state.db"), seed=False)
    monkeypatch.setattr(file_checkout, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(file_checkout, "get_db", lambda: backend)
    monkeypatch.setattr(source_tools, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "declared.py").write_text("before")
    (tmp_path / "surprise.py").write_text("before")

    snapshots = iter([
        {"declared.py": "old"},
        {"declared.py": "new", "surprise.py": "new"},
    ])

    async def capture(name, args):
        return next(snapshots)

    monkeypatch.setattr(
        "app.agent.session_changes.capture_tool_state_async", capture
    )
    monkeypatch.setattr(
        source_tools, "_run_subprocess",
        lambda *args, **kwargs: {
            "stdout": "", "stderr": "", "exit_code": 0, "timed_out": False,
        },
    )
    tools = {}
    source_tools.inject_source_tools(tools, "u1")

    async def scenario():
        with tool_execution_scope(_context()):
            await tools["checkout_files"].handler(paths=["declared.py"])
            output = await tools["run_python"].handler(
                code="pass", checkout_paths=["declared.py"]
            )
            assert "checkout_audit" in output
            status = {
                row["path"]: row
                for row in (await tools["file_checkout_status"].handler())["files"]
            }
            assert status["declared.py"]["status"] == "changed"
            assert status["surprise.py"]["status"] == "violation"

    asyncio.run(scenario())


def test_research_searches_record_returned_file_observations(tmp_path, monkeypatch):
    backend = LocalBackend(str(tmp_path / "state.db"), seed=False)
    monkeypatch.setattr(file_checkout, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(file_checkout, "get_db", lambda: backend)
    monkeypatch.setattr(source_tools, "_PROJECT_ROOT", tmp_path)
    (tmp_path / "research.py").write_text("# checkout-observation-marker\n")

    tools = {}
    source_tools.inject_source_tools(tools, "u1")

    async def scenario():
        with tool_execution_scope(_context()):
            output = await tools["search_source"].handler(
                pattern="checkout-observation-marker", path="."
            )
            assert "research.py" in output
            status = await tools["file_checkout_status"].handler(paths=["research.py"])
            assert status["files"][0]["status"] == "observed"

    asyncio.run(scenario())

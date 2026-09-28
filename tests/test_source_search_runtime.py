import asyncio
import threading
import time

import pytest


def test_patch_source_write_failure_propagates(tmp_path, monkeypatch):
    from plugins.admin import source_tools

    target = tmp_path / "locked.py"
    target.write_text("before\n", encoding="utf-8")
    monkeypatch.setattr(source_tools, "_PROJECT_ROOT", tmp_path)

    def deny_backup(*_args, **_kwargs):
        raise PermissionError("write denied")

    monkeypatch.setattr("shutil.copy2", deny_backup)
    tools = {}
    source_tools.inject_source_tools(tools, "admin")

    with pytest.raises(OSError, match="Error writing.*write denied"):
        asyncio.run(tools["patch_source"].handler(
            path="locked.py", old_string="before", new_string="after",
        ))

    assert target.read_text(encoding="utf-8") == "before\n"

def test_root_source_search_excludes_runtime_trees_and_yields_event_loop(
    tmp_path, monkeypatch,
):
    from plugins.admin import source_tools

    (tmp_path / "app.py").write_text("needle = True\n", encoding="utf-8")
    captured = {}
    main_thread = threading.get_ident()

    def slow_rg(cmd, timeout):
        captured["cmd"] = cmd
        captured["timeout"] = timeout
        captured["thread"] = threading.get_ident()
        time.sleep(0.08)
        return {"exit_code": 0, "stdout": "app.py:1:needle = True\n", "stderr": ""}

    monkeypatch.setattr(source_tools, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(source_tools, "_run_subprocess", slow_rg)
    monkeypatch.setattr("shutil.which", lambda _name: "rg")
    tools = {}
    source_tools.inject_source_tools(tools, "admin")

    async def scenario():
        search = asyncio.create_task(tools["search_source"].handler(pattern="needle"))
        await asyncio.sleep(0.01)
        assert not search.done()
        return await search

    assert "needle = True" in asyncio.run(scenario())
    command = captured["cmd"]
    assert "!.ast-index/**" in command
    assert "!data/**" in command
    assert "!logs/**" in command
    assert "!*.db" in command
    assert captured["timeout"] == 30
    assert captured["thread"] != main_thread


def test_explicit_runtime_path_remains_searchable(tmp_path, monkeypatch):
    from plugins.admin import source_tools

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "agent.log").write_text("explicit needle\n", encoding="utf-8")
    monkeypatch.setattr(source_tools, "_PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("shutil.which", lambda _name: None)
    tools = {}
    source_tools.inject_source_tools(tools, "admin")
    result = asyncio.run(tools["search_source"].handler(
        pattern="needle", path="logs",
    ))
    assert "logs" in result
    assert "explicit needle" in result

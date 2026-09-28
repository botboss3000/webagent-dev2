"""Regression contracts for compact, discoverable ability skills."""

import asyncio
import json
import runpy
from pathlib import Path

import tiktoken


ROOT = Path(__file__).resolve().parents[1]
ENCODING = tiktoken.get_encoding("o200k_base")


def _skill_tokens(relative_path: str) -> int:
    return len(ENCODING.encode((ROOT / relative_path).read_text()))


def test_large_ability_skills_stay_within_prompt_budgets():
    # These are loaded results that persist for the conversation, so budgets
    # protect usable task context rather than merely repository file size.
    budgets = {
        "plugins/abilities/Core/visualizer/visualizer.skill.md": 6_000,
        "plugins/abilities/Core/agent_management/agent_management.skill.md": 3_000,
        "plugins/abilities/Web/browser_control/browser_control.skill.md": 1_800,
        "plugins/abilities/Administrator/code_index/code_index.skill.md": 1_500,
    }
    observed = {path: _skill_tokens(path) for path in budgets}
    assert observed == {
        path: tokens for path, tokens in observed.items()
        if tokens <= budgets[path]
    }, observed


def test_visualizer_descriptor_matches_its_runtime_tools():
    module = runpy.run_path(str(ROOT / "plugins/abilities/Core/visualizer/visualizer.py"))
    runtime_tools = module["build_tools"](user_id="contract-test", agent_id="contract-test")
    descriptor = json.loads(
        (ROOT / "plugins/abilities/Core/visualizer/visualizer.json").read_text()
    )

    assert set(descriptor["tools"]) == set(runtime_tools)
    assert set(module["TOOL_SCHEMAS"]) == set(runtime_tools)


def test_visualizer_references_are_narrow_and_discoverable():
    module = runpy.run_path(str(ROOT / "plugins/abilities/Core/visualizer/visualizer.py"))
    runtime_tools = module["build_tools"](user_id="contract-test", agent_id="contract-test")
    get_reference = runtime_tools["get_genui_reference"]

    listed = asyncio.run(get_reference())
    assert listed["topics"] == [
        "chat", "contract", "credentials", "data", "embedding", "style", "verification"
    ]

    style = asyncio.run(get_reference("style"))
    assert style["topic"] == "style"
    assert "inherited app tokens" in style["reference"]
    assert "local palette" in style["reference"]

    unknown = asyncio.run(get_reference("not-a-topic"))
    assert "error" in unknown
    assert unknown["topics"] == listed["topics"]


def test_browser_skill_preserves_the_no_secret_manual_login_boundary():
    browser_skill = (ROOT / "plugins/abilities/Web/browser_control/browser_control.skill.md").read_text()
    assert "Do not attempt to type vaulted credentials manually" in browser_skill
    assert "the agent never sees the email or password" in browser_skill


def test_browser_skill_routes_webagent_journeys_to_authenticated_server_chromium():
    browser_skill = (ROOT / "plugins/abilities/Web/browser_control/browser_control.skill.md").read_text()
    default_agent = json.loads(
        (ROOT / "app/defaults/agents/default.json").read_text(encoding="utf-8")
    )
    # Browser Control retains its generic authenticated-server guidance for
    # agents that still have that broad ability enabled.
    assert "login_as_current_user()" in browser_skill
    assert 'browser_backend(mode="server")' in browser_skill
    assert 'mode="local"' in browser_skill

    # The default development agent instead uses Diagnostics' restricted facade.
    instructions = default_agent["tasks_prompt"]
    assert 'dev_browser_action(action="open")' in instructions
    assert "dev_browser_action" in instructions
    assert "open_development_browser" not in instructions
    assert "development_browser_action" not in instructions
    assert "dev_browser_record" in instructions
    assert "Do not use generic Browser Control" in instructions
    assert "run_python, shell commands, Playwright APIs, or Chromium/Chrome executables" in instructions
    assert 'dev_browser_action(action=\"close\")' in instructions

    diagnostics_skill = (ROOT / "plugins/abilities/Administrator/diagnostics/diagnostics.skill.md").read_text()
    dev_browser_skill = (ROOT / "plugins/abilities/Administrator/diagnostics/tools/dev_browser_action.skill.md").read_text()
    assert 'dev_browser_action(action="open"' in diagnostics_skill
    assert "mandatory development-browser tool" in dev_browser_skill
    assert "Do not use `run_python`, shell commands, Playwright APIs" in dev_browser_skill
    assert 'dev_browser_action(action="open"' in dev_browser_skill
    assert 'dev_browser_action(action="close")' in dev_browser_skill

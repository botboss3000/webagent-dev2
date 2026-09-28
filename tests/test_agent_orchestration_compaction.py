import ast
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from plugins.abilities.Core.agent_orchestration import agent_orchestration as orchestration


ROOT = Path(__file__).resolve().parents[1]
ABILITY = ROOT / "plugins" / "abilities" / "Core" / "agent_orchestration"


def test_orchestration_prompt_and_schema_budgets_stay_compact():
    skill = (ABILITY / "agent_orchestration.skill.md").read_text(encoding="utf-8")
    source = (ABILITY / "agent_orchestration.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    schemas = next(
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "TOOL_SCHEMAS"
                for target in node.targets)
    )

    assert len(skill) <= 6000
    assert len(skill.split()) <= 900
    assert "unlimited" not in skill.lower()
    assert "vision model for a helper" not in json.dumps(schemas).lower()
    assert len(json.dumps(schemas, separators=(",", ":"))) <= 6000


def _spawn(delivery=""):
    return {
        "id": "spawn-1",
        "user_id": "user-1",
        "orchestrator_session_id": "parent-1",
        "orchestrator_agent_id": "agent-1",
        "completion_delivery": delivery,
    }


def test_completion_delivery_defaults_to_resume():
    assert orchestration._ORCH_CONFIG_DEFAULTS["completion_delivery"] == "resume"


def test_terminal_completion_resumes_once_when_configured():
    async def run():
        with (
            patch.object(orchestration, "_spawns_claim_delivery", return_value="claim"),
            patch.object(orchestration, "_load_orch_config",
                         new=AsyncMock(return_value={"completion_delivery": "resume"})),
            patch.object(orchestration, "_post_chat", new=AsyncMock(return_value="ok")) as post,
            patch.object(orchestration, "_spawns_finish_delivery") as finish,
            patch.object(orchestration, "_notify_orchestrator", new=AsyncMock()) as notify,
        ):
            orchestration._PARENT_RESUME_LOCKS.clear()
            await orchestration._deliver_terminal_event(_spawn(), "completed")
        post.assert_awaited_once_with("user-1", "parent-1", "completed", timeout=300.0)
        finish.assert_called_once_with("spawn-1", "claim", "resumed")
        notify.assert_not_awaited()

    asyncio.run(run())


def test_terminal_completion_can_notify_without_resuming():
    async def run():
        with (
            patch.object(orchestration, "_spawns_claim_delivery", return_value="claim"),
            patch.object(orchestration, "_post_chat", new=AsyncMock()) as post,
            patch.object(orchestration, "_spawns_finish_delivery") as finish,
            patch.object(orchestration, "_notify_orchestrator", new=AsyncMock()) as notify,
        ):
            await orchestration._deliver_terminal_event(_spawn("notify"), "completed")
        post.assert_not_awaited()
        notify.assert_awaited_once_with("user-1", "parent-1", "completed")
        finish.assert_called_once_with("spawn-1", "claim", "notified")

    asyncio.run(run())


def test_terminal_completion_claim_prevents_a_duplicate_parent_turn():
    async def run():
        with (
            patch.object(orchestration, "_spawns_claim_delivery", return_value=None),
            patch.object(orchestration, "_post_chat", new=AsyncMock()) as post,
            patch.object(orchestration, "_notify_orchestrator", new=AsyncMock()) as notify,
        ):
            await orchestration._deliver_terminal_event(_spawn(), "completed")
        post.assert_not_awaited()
        notify.assert_not_awaited()

    asyncio.run(run())



def test_failed_parent_resume_falls_back_to_a_notification():
    async def run():
        with (
            patch.object(orchestration, "_spawns_claim_delivery", return_value="claim"),
            patch.object(orchestration, "_load_orch_config",
                         new=AsyncMock(return_value={"completion_delivery": "resume"})),
            patch.object(orchestration, "_post_chat", new=AsyncMock(side_effect=RuntimeError("offline"))),
            patch.object(orchestration, "_spawns_finish_delivery") as finish,
            patch.object(orchestration, "_notify_orchestrator", new=AsyncMock()) as notify,
        ):
            orchestration._PARENT_RESUME_LOCKS.clear()
            await orchestration._deliver_terminal_event(_spawn(), "completed")
        notify.assert_awaited_once()
        assert "Automatic parent resume failed" in notify.await_args.args[2]
        finish.assert_called_once_with("spawn-1", "claim", "resume_failed_notified")

    asyncio.run(run())

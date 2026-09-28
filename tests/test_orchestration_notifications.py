from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE = (ROOT / "plugins" / "abilities" / "Core" / "agent_orchestration" / "agent_orchestration.py").read_text(encoding="utf-8")

def test_spawn_followups_use_configurable_terminal_delivery():
    assert "async def _notify_orchestrator" in SOURCE
    assert "async def _deliver_terminal_event" in SOURCE
    assert '"type": "spawn_status"' in SOURCE
    assert "async def _rewake_orchestrator" not in SOURCE
    start = SOURCE.index("async def _deliver_terminal_event")
    end = SOURCE.index("async def _heartbeat_loop", start)
    assert "_post_chat(" in SOURCE[start:end]
    assert 'completion_delivery") or "resume"' in SOURCE

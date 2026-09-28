from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_design_launcher_uses_one_time_session_handoff() -> None:
    source = _source("ui/main-panel/agents/js/identity-settings.js")
    block = source[source.index("designForm.addEventListener"):source.index("design.appendChild(designCopy)")]

    assert "sessionStorage.setItem(`agent-design-launch:${launchId}`" in block
    assert "target.searchParams.set('design_launch', launchId)" in block
    assert "target.searchParams.set('prompt'" not in block
    assert "target.searchParams.set('send'" not in block
    assert "target.searchParams.set('mode'" not in block


def test_url_launch_cannot_enable_send_or_auto_mode() -> None:
    source = _source("ui/shared/js/tabs.js")
    block = source[source.index("// ── If this tab was opened"):source.index("// (Main-panel swipe")]

    assert "var sendPrompt = false;" in block
    assert "var launchMode = null;" in block
    assert "p.get('send') === '1'" not in block
    assert "p.get('mode')" not in block
    assert "sessionStorage.removeItem(launchKey)" in block
    assert "launch.agentId === agentId" in block
    assert "Date.now() - Number(launch.createdAt || 0) <= 60000" in block
    assert "p.delete('design_launch')" in block


from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_native_codex_catalog_uses_agent_scoped_stale_while_revalidate_cache():
    source = (ROOT / "ui/main-panel/agents/sessions/js/sessions-page.js").read_text(encoding="utf-8")
    assert "sessions:codex:${app.currentUserId || 'anonymous'}:${agentId}" in source
    assert "Verifying Codex tasks…" in source
    assert "Codex tasks verified" in source
    assert "loadNativeContext ? 7 * 24 * 60 * 60 * 1000" in source


def test_native_codex_tool_rows_use_foldable_tool_renderer():
    source = (ROOT / "ui/chat/js/session-load.js").read_text(encoding="utf-8")
    assert "if (msg.source === 'codex:portal') return true;" in source
    assert "args: (meta.args && typeof meta.args === 'object') ? meta.args : {}" in source

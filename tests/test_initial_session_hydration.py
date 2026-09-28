from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_boot_loads_the_session_painted_by_the_dropdown():
    source = (ROOT / "ui/chat/js/session-init.js").read_text(encoding="utf-8")
    boot = source[source.index("populateUserSelect().then") : source.index(
        "// ── Session completion notification"
    )]

    assert "populateUserSelect().then(async function" in boot
    assert "await populateSessionSelect(" in boot
    assert boot.index("await populateSessionSelect(") < boot.index(
        "const initialSessionId = app.currentSessionId"
    )
    assert boot.index("const initialSessionId = app.currentSessionId") < boot.index(
        "loadSessionChat(initialSessionId)"
    )


def test_user_boot_waits_for_the_selected_agent_before_sessions():
    source = (ROOT / "ui/shared/js/user-panel.js").read_text(encoding="utf-8")

    assert "await app.populateAgentSelect(app.currentUserId)" in source

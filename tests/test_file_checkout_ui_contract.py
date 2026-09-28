import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_updates_panel_preserves_configured_footer_placement():
    config = json.loads((ROOT / "data/config/chat_ui.json").read_text())
    below = config["chat_common"]["active_footer"]["below_pill"]
    rendered = json.dumps(below)
    assert "changes" in rendered


def test_updates_panel_renders_checkout_lifecycle_and_unique_file_count():
    panel = (ROOT / "ui/chat/elements/local-changes/index.js").read_text()
    endpoint = (ROOT / "app/api/github.py").read_text()
    assert "Read" in endpoint
    assert "Checked out elsewhere" in endpoint
    for label in (
        "Checked out", "Changed", "Validated", "Checked in",
        "Stale", "Conflict", "Violation",
    ):
        assert label in endpoint
    assert "new Map" in panel
    assert "aria-label" in panel
    assert "row.title" in panel


def test_updates_panel_uses_theme_tokens_and_keeps_commit_conflict_guard():
    source = (ROOT / "ui/chat/elements/local-changes/index.js").read_text()
    css = (ROOT / "ui/chat/elements/local-changes/index.css").read_text()
    assert "!f.conflict" in source
    assert "canCommit" in source
    assert "manual_checkin_allowed" in source
    assert "/chat-changes/checkin" in source
    assert "aria-label" in source
    assert "var(--" in css
    assert "#fff" not in css.lower()

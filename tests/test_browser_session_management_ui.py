import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "ui" / "main-panel" / "instances" / "settings"


def test_browser_session_management_is_registered_and_mounted():
    index = json.loads((SETTINGS / "settings-index.json").read_text(encoding="utf-8"))
    sections = [section for group in index["groups"] for section in group["sections"]]
    section = next(item for item in sections if item["id"] == "browser-session-management")
    shell = (SETTINGS / "settings.html").read_text(encoding="utf-8")
    descriptor = json.loads(
        (ROOT / "ui" / "main-panel" / "instances" / "page.json").read_text(encoding="utf-8")
    )

    assert section["title"] == "Browser Session Management"
    assert "settings-page-slot-browser-session-management" in shell
    assert "settings/pages/browser-session-management.html" in descriptor["partials"]


def test_browser_session_management_controls_requested_defaults_and_actions():
    page = (SETTINGS / "pages" / "browser-session-management.html").read_text(encoding="utf-8")
    controller = (SETTINGS / "browser-session-management.js").read_text(encoding="utf-8")

    assert 'id="ac-browser-max-sessions"' in page
    assert 'value="3"' in page
    assert 'value="300">5 minutes (default)' in page
    assert "/api/v1/control/browser/reap-idle" in controller
    assert "browser_max_concurrent_sessions" in controller
    assert "browser_idle_timeout_seconds" in controller

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_browser_page_is_presented_as_control_with_three_stoppable_surfaces():
    descriptor = json.loads(
        (ROOT / "ui" / "main-panel" / "browser" / "page.json").read_text(
            encoding="utf-8"
        )
    )
    markup = (
        ROOT / "ui" / "main-panel" / "browser" / "browser.html"
    ).read_text(encoding="utf-8")

    assert descriptor["id"] == "browser"  # stable capability/session identity
    assert descriptor["label"] == "Control"
    assert 'data-surface="browser"' in markup
    assert 'data-surface="extension"' in markup
    assert 'data-surface="desktop"' in markup
    assert 'data-stop-surface="browser"' in markup
    assert 'data-stop-surface="extension"' in markup
    assert 'data-stop-surface="desktop"' in markup
    assert 'aria-label="Control surface"' in markup


def test_control_opens_idle_without_starting_playwright():
    markup = (
        ROOT / "ui" / "main-panel" / "browser" / "browser.html"
    ).read_text(encoding="utf-8")
    client = (
        ROOT / "ui" / "main-panel" / "browser" / "js" / "browser.js"
    ).read_text(encoding="utf-8")

    assert "let surfaceMode = 'idle'" in client
    assert "if (surfaceMode === 'idle')" in client
    assert "surfaceMode !== 'browser'" in client
    assert 'data-surface="browser" aria-pressed="false"' in markup
    assert 'data-surface="browser" aria-pressed="true"' not in markup


def test_control_client_wires_the_server_desktop_socket():
    config = (ROOT / "ui" / "shared" / "js" / "config.js").read_text(
        encoding="utf-8"
    )
    client = (
        ROOT / "ui" / "main-panel" / "browser" / "js" / "browser.js"
    ).read_text(encoding="utf-8")
    server = (ROOT / "app" / "api" / "browser_stream.py").read_text(
        encoding="utf-8"
    )

    assert "/api/v1/control/desktop" in config
    assert "desktopControlWsUrl()" in client
    assert '@router.websocket("/api/v1/control/desktop")' in server
    assert "is_user_admin" in server


def test_control_emergency_stops_are_admin_gated_and_present_in_instances():
    server = (ROOT / "app" / "api" / "browser_stream.py").read_text(
        encoding="utf-8"
    )
    instances = (ROOT / "ui" / "main-panel" / "instances" / "instances.js").read_text(
        encoding="utf-8"
    )

    assert '@router.post("/api/v1/control/browser/kill")' in server
    assert '@router.post("/api/v1/control/browser/reap-idle")' in server
    assert '@router.post("/api/v1/control/extension/kill")' in server
    assert '@router.post("/api/v1/control/desktop/kill")' in server
    assert "_require_control_admin(request)" in server
    assert "data-act=\"control-kill\"" in instances
    assert "Stop Browser" in instances
    assert "Stop Desktop" in instances
    assert "function _killControl(kind)" in instances


def test_extension_surface_exposes_status_and_remote_settings():
    markup = (ROOT / "ui" / "main-panel" / "browser" / "browser.html").read_text(
        encoding="utf-8"
    )
    client = (ROOT / "ui" / "main-panel" / "browser" / "js" / "browser.js").read_text(
        encoding="utf-8"
    )
    server = (ROOT / "app" / "api" / "browser_stream.py").read_text(
        encoding="utf-8"
    )

    assert "extension-control-panel" in markup
    assert 'data-extension-setting="paused"' in markup
    assert 'data-extension-setting="allow_screenshots"' in markup
    assert "refreshExtensionStatus" in client
    assert "stopControlSurface" in client
    assert '@router.patch("/api/v1/control/extension/settings")' in server

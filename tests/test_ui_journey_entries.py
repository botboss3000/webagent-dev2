from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "claude" / "ui-journeys.md"
APP_CATALOG = ROOT / "data" / "config" / "app-pages.json"
APP_OWNERS = (
    ROOT / "ui" / "main-panel" / "app" / "instances.html",
    ROOT / "ui" / "main-panel" / "app" / "instances.css",
    ROOT / "ui" / "main-panel" / "app" / "instances.js",
)


def test_registry_covers_every_catalog_entry_once():
    registry = REGISTRY.read_text(encoding="utf-8")
    catalog = json.loads(APP_CATALOG.read_text(encoding="utf-8"))
    for page_id in catalog["pages"]:
        assert registry.count(f"`app.{page_id}`") == 1
    for main_page in ("agents", "app", "browser", "genui", "wiki", "admin-tools"):
        assert registry.count(f"route `/app?tab={main_page}`") == 1


def test_app_journey_owner_headers_and_route_contract_are_present():
    for owner in APP_OWNERS:
        assert "JOURNEY ENTRY: app.*" in owner.read_text(encoding="utf-8")
    tabs = (ROOT / "ui" / "shared" / "js" / "tabs.js").read_text(encoding="utf-8")
    instances = (ROOT / "ui" / "main-panel" / "app" / "instances.js").read_text(encoding="utf-8")
    settings = (ROOT / "ui" / "main-panel" / "app" / "settings" / "settings.js").read_text(encoding="utf-8")
    assert "app: ['view']" in tabs
    assert "_requestedAppView" in instances
    assert "_applyRequestedAppView" in instances
    assert "WA_SYNC_APP_VIEW" in instances
    assert "WA_SYNC_APP_VIEW?.(id)" in settings
    assert "WA_SYNC_APP_VIEW?.('settings')" in settings

def test_registry_and_diagnostics_skill_prescribe_dynamic_server_chromium_routes():
    registry = REGISTRY.read_text(encoding="utf-8")
    skill = (ROOT / "plugins" / "abilities" / "Administrator" / "diagnostics" / "diagnostics.skill.md").read_text(encoding="utf-8")
    assert "/app?tab=app&view=<id>" in registry
    assert "login_as_current_user()" in registry
    assert "WEBAGENT_PORT" in registry
    assert "dev.webagent.live" not in registry
    assert "localhost:8080" not in registry
    assert "browser_backend(mode=\"server\")" in skill
    assert "login_as_current_user()" in skill
    assert "WEBAGENT_PORT" in skill
    assert "Do **not** search for Chrome/Chromium executables" in skill
    assert "a backend-status call is" in skill

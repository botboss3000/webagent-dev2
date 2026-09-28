import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / "ui" / "main-panel" / "instances" / "settings"
MODULE = SETTINGS / "entitlements" / "entitlements.js"


def test_entitlement_settings_page_is_registered_and_lifecycle_loaded():
    descriptor = json.loads((ROOT / "ui" / "main-panel" / "instances" / "page.json").read_text(encoding="utf-8"))
    index = json.loads((SETTINGS / "settings-index.json").read_text(encoding="utf-8"))
    settings_js = (SETTINGS / "settings.js").read_text(encoding="utf-8")
    shell = (SETTINGS / "settings.html").read_text(encoding="utf-8")

    assert "settings/pages/entitlements.html" in descriptor["partials"]
    assert "settings/entitlements/entitlements.css" in descriptor["css"]
    assert any(section["id"] == "entitlements" for group in index["groups"] for section in group["sections"])
    assert "settings-page-slot-entitlements" in shell
    assert "initEntitlements()" in settings_js
    assert "loadEntitlements()" in settings_js


def test_entitlement_editor_covers_roster_and_tier_control_plane_routes():
    source = MODULE.read_text(encoding="utf-8")
    for fragment in (
        "/admin/entitlements/rosters",
        "/admin/entitlements/schema",
        "/credentials/",
        "/validate",
        "/preview",
        "/history",
        "/publish",
        "/retire",
        "/rollback",
        "/admin/entitlements/tiers",
        "/admin/entitlements/assignments",
        "/admin/entitlements/audit",
    ):
        assert fragment in source


def test_credentials_are_write_only_and_cleared_before_network_completion():
    source = MODULE.read_text(encoding="utf-8")
    partial = (SETTINGS / "pages" / "entitlements.html").read_text(encoding="utf-8")

    assert 'type="password"' in source
    assert 'autocomplete="new-password"' in source
    assert "if (input) input.value = '';" in source
    assert "credential_configured" in source
    assert "api_key" not in source
    assert "credential" not in partial.lower()


def test_tier_editor_exposes_every_policy_dimension_and_clear_limit_units():
    source = MODULE.read_text(encoding="utf-8")
    for dimension in ("pages", "features", "ability_groups", "agent_templates", "allowed_entry_ids"):
        assert dimension in source
    for limit in (
        "max_agents", "max_automations", "max_connections",
        "concurrent_sessions_per_user", "messages_per_window", "window_seconds",
        "max_attachment_bytes", "max_storage_bytes",
    ):
        assert limit in source
    assert "blank = unlimited" in source
    assert "Maximum BYO entries" in source
    assert "Maximum reasoning effort" in source
    assert "_applySchema(schema)" in source


def test_editor_uses_auth_aware_fetch_and_safe_result_rendering():
    source = MODULE.read_text(encoding="utf-8")
    assert "_fetch(url, options)" in source
    assert "requesting_user_id" in source
    assert "pre.textContent = JSON.stringify(payload, null, 2)" in source
    assert "role=\"status\"" in (SETTINGS / "pages" / "entitlements.html").read_text(encoding="utf-8")

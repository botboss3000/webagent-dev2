from pathlib import Path
import json


ROOT = Path(__file__).resolve().parents[1]
INSTANCES = ROOT / "ui" / "main-panel" / "instances"
USERS = INSTANCES / "users"
SETTINGS = INSTANCES / "settings"


def _section(section_id):
    index = json.loads((SETTINGS / "settings-index.json").read_text(encoding="utf-8"))
    return next(
        section
        for group in index["groups"]
        for section in group["sections"]
        if section["id"] == section_id
    )


def test_user_policies_are_hosted_in_standalone_settings_page():
    users_source = (USERS / "users.js").read_text(encoding="utf-8")
    partial = (SETTINGS / "pages" / "user-policies.html").read_text(encoding="utf-8")
    module = (SETTINGS / "user-policies.js").read_text(encoding="utf-8")
    descriptor = json.loads((INSTANCES / "page.json").read_text(encoding="utf-8"))

    assert "attachExperienceTierEditor" not in users_source
    assert "mountAnonymousLimits" not in users_source
    assert "fetch('/admin/settings/app'" not in users_source
    assert 'fetch("/admin/settings/app"' not in users_source
    assert 'id="settings-page-user-policies"' in partial
    assert 'id="user-policies-access"' in partial
    assert 'id="user-policies-anonymous"' not in partial
    assert 'mountAnonymousPolicy' not in module
    assert 'id="user-policies-tiers"' in partial
    assert 'id="user-policies-manage-usage"' in partial
    assert "export function init()" in module
    assert "export async function load()" in module
    assert "[data-tab=\"users\"]" in module
    assert "settings/pages/user-policies.html" in descriptor["partials"]


def test_settings_catalog_separates_policies_from_usage_data():
    user_policies = _section("user-policies")
    usage_data = _section("usage-data")
    agent_tools = _section("agent-tools")
    index = json.loads((SETTINGS / "settings-index.json").read_text(encoding="utf-8"))
    agent_sections = next(group["sections"] for group in index["groups"] if group["id"] == "agent")
    ids = [section["id"] for section in agent_sections]

    assert user_policies["title"] == "User Policies"
    assert usage_data["title"] == "Usage Data"
    assert usage_data["externalTab"] == "users"
    assert agent_tools["title"] == "Agent tools & abilities"
    assert ids.index("models") < ids.index("user-policies") < ids.index("usage-data") < ids.index("agent-tools")


def test_tier_editor_exposes_app_credit_allowance_controls():
    editor = (SETTINGS / "entitlements" / "entitlements.js").read_text(encoding="utf-8")
    policy = (ROOT / "app" / "entitlements" / "policy.py").read_text(encoding="utf-8")

    assert 'weekly_credit_limit' in editor
    assert 'monthly_credit_limit' in editor
    assert 'weekly_credit_renews' in editor
    assert 'weekly_credit_limit' in policy
    assert 'monthly_credit_limit' in policy
    assert 'weekly_credit_renews' in policy
    assert "key !== 'weekly_credit_renews'" in editor
    generic_limits = "LIMITS.filter(([key]) => key !== 'weekly_credit_limit')"
    assert editor.index('data-limit="weekly_credit_limit"') < editor.index(generic_limits)
    assert editor.index('data-limit-renewal="weekly_credit_renews"') < editor.index(generic_limits)
    for key in ('identity_messages', 'daily_messages', 'network_messages'):
        assert key in editor
        assert key in policy
    assert 'Anonymous tier allowances' in editor
    assert 'Global safety ceiling' in editor


def test_usage_data_assets_are_registered_with_instances_page():
    descriptor = json.loads((INSTANCES / "page.json").read_text(encoding="utf-8"))
    assert "users/policies/policies.css" in descriptor["css"]
    assert (USERS / "policies" / "page-access.js").is_file()
    assert not (USERS / "policies" / "anonymous-policy.js").exists()
    assert not (USERS / "policies" / "anonymous-limits.js").exists()

"""Stable mapping from installed ability slugs to entitlement groups.

Unknown abilities are deliberately classified as ``platform_admin``.  New
abilities therefore stay unavailable to ordinary users until they have been
reviewed and assigned to an explicit group.
"""

from __future__ import annotations


ABILITY_GROUP_MEMBERS: dict[str, frozenset[str]] = {
    "chat_core": frozenset({"base", "app_control", "agent_management", "visualizer"}),
    "memory": frozenset({"context_control", "wiki_context", "wiki_control"}),
    "user_files": frozenset({"user_files"}),
    "web_read": frozenset({"web_access", "web_scraper"}),
    "browser_control": frozenset({"browser_control"}),
    "ssh_control": frozenset({"ssh_control"}),
    "host_management": frozenset({"host_management"}),
    "image_vision": frozenset({"image_vision"}),
    "image_generation": frozenset({"image_generation"}),
    "model_switching": frozenset({"model_switcher"}),
    "automation": frozenset({"automation"}),
    "agent_orchestration": frozenset({"agent_orchestration"}),
    "personal_integrations": frozenset({
        "airtable", "discord", "dropbox", "email", "google", "google_sheets",
        "facebook", "hubspot", "instagram", "linkedin", "mailchimp", "meta", "microsoft", "notion",
        "pinterest", "reddit", "salesforce", "slack", "snapchat", "telegram",
        "tiktok", "twilio", "twitch", "twitter", "whatsapp", "yahoo",
    }),
    "financial_actions": frozenset({
        "amazon", "bank", "bank_accounts", "ebay", "etsy", "paypal", "shopify",
        "square", "stripe",
    }),
    "developer_write": frozenset({"github", "gitlab", "jira", "jira_linear"}),
    "tool_creation": frozenset({"create_tools"}),
    "platform_infra": frozenset({"p2p", "run_on_device"}),
    "platform_admin": frozenset({
        "code_index", "code_index", "codebase_admin", "computer_control", "diagnostics",
        "git_control", "program_screenshot", "remote_tunnel", "render_recorder",
        "terminal_control", "ui_admin",
    }),
}

ABILITY_TO_GROUP = {
    ability: group
    for group, abilities in ABILITY_GROUP_MEMBERS.items()
    for ability in abilities
}


def ability_group(ability_slug: str) -> str:
    """Return an ability's reviewed group, restricting unknown/invalid values.

    New drop-ins declare the group in their descriptor (or group descriptor).
    The central map remains only as a compatibility reader during migration.
    """
    slug = str(ability_slug or "").strip().lower()
    try:
        from app.abilities import ability_entry
        from app.entitlements.policy import KNOWN_ABILITY_GROUPS

        declared = str((ability_entry(slug) or {}).get("entitlement_group") or "").strip()
        if declared:
            return declared if declared in KNOWN_ABILITY_GROUPS else "platform_admin"
    except Exception:
        return "platform_admin"
    return ABILITY_TO_GROUP.get(slug, "platform_admin")


def filter_abilities_by_groups(enabled: set[str], allowed_groups: set[str]) -> set[str]:
    return {ability for ability in enabled if ability_group(ability) in allowed_groups}

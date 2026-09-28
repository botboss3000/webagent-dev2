import asyncio
import json
import time

from app import abilities
from app.api import agents
from app.entitlements.abilities import ability_group
from app.entitlements.policy import KNOWN_ABILITY_GROUPS
from app import ui_pages


def test_installed_abilities_declare_or_inherit_valid_entitlement_groups():
    abilities.reload()
    for ability_id in abilities.ability_ids(kind="ability"):
        entry = abilities.ability_entry(ability_id) or {}
        if entry.get("app_function"):
            continue
        assert entry.get("entitlement_group"), ability_id
        assert ability_group(ability_id) in KNOWN_ABILITY_GROUPS


def test_main_page_descriptors_declare_backend_capability_contracts():
    ui_pages.reload()
    for page_id in ui_pages.page_ids("main"):
        entry = ui_pages.page_entry("main", page_id) or {}
        assert entry.get("required_backend_capability"), page_id


def test_catalog_preserves_backend_capability_metadata():
    ui_pages.reload()
    catalog = ui_pages.ui_catalog()
    agents = next(page for page in catalog["main"] if page["id"] == "agents")
    assert agents["required_backend_capability"] == "page:agents"

    instances = next(page for page in catalog["main"] if page["id"] == "instances")
    assert instances["required_backend_capability"] == "role:platform_admin"


def test_app_catalog_exposes_declared_settings_partials():
    ui_pages.reload()
    catalog = ui_pages.ui_catalog()
    app = next(page for page in catalog["main"] if page["id"] == "app")

    assert {"id": "design", "path": "settings/pages/design/page.html"} in app["settings_partials"]


def test_catalog_omits_malformed_settings_partial_entries(tmp_path):
    descriptor = {
        "id": "test-page",
        "settings_partials": [
            {"id": "valid", "path": "settings/pages/valid/page.html"},
            {"id": "missing-path"},
            {"path": "settings/pages/missing-id/page.html"},
            {"id": 3, "path": "settings/pages/non-string-id/page.html"},
            "not-a-mapping",
        ],
    }
    (tmp_path / "page.json").write_text(json.dumps(descriptor), encoding="utf-8")

    entry = ui_pages._entry_from_dir(tmp_path, "main-panel/test-page", "main")

    assert entry is not None
    assert entry["settings_partials"] == [
        {"id": "valid", "path": "settings/pages/valid/page.html"}
    ]


def test_ability_catalog_returns_caller_entitlement_decisions(monkeypatch):
    agents._CATALOG_CACHE.update({
        "at": time.monotonic(),
        "data": {"groups": [], "abilities": {
            "base": {"entitlement_group": "chat_core"},
            "diagnostics": {"entitlement_group": "platform_admin"},
        }},
    })
    monkeypatch.setattr(agents, "request_user_id", lambda _request: "user")

    async def capabilities(*_args, **_kwargs):
        return {"ability_groups": ["chat_core"]}

    monkeypatch.setattr(agents, "resolve_capabilities", capabilities)
    result = asyncio.run(agents.get_abilities_catalog(object()))
    assert result["abilities"]["base"]["entitlement_allowed"] is True
    assert result["abilities"]["diagnostics"]["entitlement_reason"] == "tier_denied"

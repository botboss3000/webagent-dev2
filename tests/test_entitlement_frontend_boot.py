from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_page_catalog_fetch_carries_auth_and_identity_scopes_cache():
    source = (ROOT / "ui/shared/js/header-build.js").read_text(encoding="utf-8")
    assert "options.headers.Authorization = 'Bearer ' + token" in source
    assert "saved.identity === currentIdentity()" in source
    assert "tierRevision:" in source
    assert "evaluationRevision:" in source
    assert "savedAt: Date.now()" in source
    assert "CACHE_FAILURE_MAX_AGE_MS" not in source
    assert "window.__readPagesCache = function ()" in source
    assert "closed unavailable state" in source
    assert "window.__pagesFallback" not in source


def test_boot_primes_capabilities_endpoint():
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    assert "capabilities:       '/api/v1/entitlements/me'" in source


def test_unknown_catalog_page_fails_closed_after_catalog_loads():
    source = (ROOT / "ui/shared/js/header-build.js").read_text(encoding="utf-8")
    assert "if (cat) return true;" in source


def test_agent_ability_lazy_cards_use_defined_display_name_for_toggle_labels():
    table = (ROOT / "ui/shared/js/agent-ability-table.js").read_text(encoding="utf-8")

    assert "const displayName = conn.display_name || abilityId;" in table
    assert "${displayName} availability:" in table
    assert "abilityName" not in table


def test_agent_ability_toggle_proxy_mounts_its_menu_anchor_before_clicking():
    table = (ROOT / "ui/shared/js/agent-ability-table.js").read_text(encoding="utf-8")

    mount = "header.appendChild(moreBtn);"
    proxy_click = "abilityMoreBtn?.click();"
    assert mount in table
    assert proxy_click in table
    assert table.index(mount) < table.index(proxy_click)


def test_ability_catalog_is_identity_aware_and_denied_rows_render_locked():
    dom = (ROOT / "ui/shared/js/dom-utils.js").read_text(encoding="utf-8")
    table = (ROOT / "ui/shared/js/agent-ability-table.js").read_text(encoding="utf-8")
    assert "abilities/catalog', { headers: { ...authHeaders() }" in dom
    assert "entitlement_allowed" in table
    assert "Upgrade required" in table

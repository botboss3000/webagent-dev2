from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _early_navigation_script() -> str:
    source = (ROOT / "index.html").read_text(encoding="utf-8")
    start = source.index("window.__queueEarlyMainTab = function")
    end = source.index("\n  // Pick a tab", start)
    return source[start:end]


def _select_option_guard_script() -> str:
    source = (ROOT / "ui/shared/js/tabs.js").read_text(encoding="utf-8")
    start = source.index("function _selectOptionBlocked")
    end = source.index("\n\nfunction setChatPanelVisible", start)
    return source[start:end]


def _mobile_menu_script() -> str:
    source = (ROOT / "ui/shared/js/mobile-navigation.js").read_text(encoding="utf-8")
    start = source.index("function _isSourceHidden")
    end = source.index("\nfunction _enableMobileNavigation", start)
    return source[start:end]


def test_mobile_menu_click_refreshes_back_label_after_routing():
    source = _mobile_menu_script()
    start = source.index("button.addEventListener('click'")
    end = source.index("return button;", start)
    click_handler = source[start:end]

    assert click_handler.index("source?.click()") < click_handler.index("_updateBackLabel()")
    assert click_handler.index("_updateBackLabel()") < click_handler.index("_closeMenu()")

def test_pre_router_instances_click_clears_stale_wiki_and_paints_guest_gate():
    playwright = pytest.importorskip("playwright.sync_api")
    runtime = playwright.sync_playwright().start()
    try:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as error:
            pytest.skip(f"Chromium is unavailable: {error}")
        try:
            page = browser.new_page()
            page.set_content(
                '<main id="main-panel">'
                '<div id="tab-wiki" class="tab-content active">'
                '<p>This article has no content yet.</p></div>'
                '<div id="tab-app" class="tab-content"></div>'
                '<div id="page-access-gate" hidden></div>'
                '</main>'
            )
            page.evaluate("window.app = { currentUserId: 'anon_behavior_test' }")
            page.add_script_tag(content=_early_navigation_script())

            page.evaluate("window.__queueEarlyMainTab('instances')")

            assert page.locator("#tab-wiki").evaluate("el => !el.classList.contains('active')")
            assert page.locator("#tab-app").evaluate("el => el.classList.contains('active')")
            assert page.locator("#tab-app").get_attribute("data-shell-access-gate") == "anonymous"
            assert "Instances are unavailable for guest chats" in page.locator("#tab-app").inner_text()
            assert page.evaluate("window.__pendingMainTab") == "instances"
        finally:
            browser.close()
    finally:
        runtime.stop()


def test_post_router_guest_click_routes_instances_without_select_option():
    playwright = pytest.importorskip("playwright.sync_api")
    runtime = playwright.sync_playwright().start()
    try:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as error:
            pytest.skip(f"Chromium is unavailable: {error}")
        try:
            page = browser.new_page()
            page.set_content(
                '<select id="main-tab-select"><option value="agents">Agents</option></select>'
                '<main id="main-panel">'
                '<div id="tab-wiki" class="tab-content active">'
                '<p>This article has no content yet.</p></div>'
                '<div id="tab-app" class="tab-content"></div>'
                '<div id="page-access-gate" hidden></div>'
                '</main>'
            )
            page.add_script_tag(content=_early_navigation_script())
            page.evaluate(
                """
                window.app = { currentUserId: '' };
                window.isAnonGuest = () => false;
                window.getAccessMode = () => 'private';
                window.isAuthenticated = () => true;
                """
            )
            page.add_script_tag(content=_select_option_guard_script())
            page.evaluate(
                """
                window.__mainTabsRouterReady = true;
                window.__routedTabs = [];
                window.__startCalls = 0;
                const select = document.getElementById('main-tab-select');
                window.__routeMainTab = (id, userInitiated) => {
                  window.__routedTabs.push({ id, userInitiated });
                  document.querySelectorAll('#main-panel > .tab-content')
                    .forEach(el => el.classList.remove('active'));
                  if (_selectOptionBlocked(id, select)) {
                    const gate = document.getElementById('page-access-gate');
                    gate.hidden = false;
                    gate.textContent = 'Instances are unavailable for guest chats';
                    return;
                  }
                  window.__startCalls += 1;
                  throw new Error('initializer must not run for a gated page');
                };
                void 0;
                """
            )

            routed = page.evaluate(
                "window.__routeHeaderMainTab('instances', document.getElementById('main-tab-select'))"
            )

            assert routed is True
            assert page.evaluate("window.__routedTabs") == [
                {"id": "instances", "userInitiated": True}
            ]
            assert page.locator("#main-tab-select").input_value() == "agents"
            assert page.locator("#tab-wiki").evaluate("el => !el.classList.contains('active')")
            assert page.evaluate("window.__startCalls") == 0
            assert page.locator("#page-access-gate").is_visible()
            assert "Instances are unavailable for guest chats" in page.locator("#page-access-gate").inner_text()
        finally:
            browser.close()
    finally:
        runtime.stop()


def test_mobile_menu_click_updates_back_label_immediately():
    playwright = pytest.importorskip("playwright.sync_api")
    runtime = playwright.sync_playwright().start()
    try:
        try:
            browser = runtime.chromium.launch(headless=True)
        except Exception as error:
            pytest.skip(f"Chromium is unavailable: {error}")
        try:
            page = browser.new_page()
            page.set_content(
                '<button id="mobile-nav-toggle"></button>'
                '<aside id="mobile-nav-menu"></aside>'
                '<span id="mobile-back-main-btn"></span>'
                '<div id="main-tabs">'
                '<button class="main-tab active" data-value="agents">Agents</button>'
                '<button class="main-tab" data-value="wiki">Wiki</button>'
                '</div>'
            )
            page.add_script_tag(content=_mobile_menu_script())
            page.evaluate(
                """
                localStorage.setItem('lastActiveTab', 'agents');
                _updateBackLabel();
                const source = document.querySelector('[data-value="wiki"]');
                source.addEventListener('click', () => {
                  localStorage.setItem('lastActiveTab', 'wiki');
                });
                document.body.appendChild(_menuButton(source, 'Wiki', 'tab'));
                void 0;
                """
            )

            assert page.locator("#mobile-back-main-btn").inner_text() == "Agents"

            page.locator('.mobile-nav-item[data-value="wiki"]').click()

            assert page.locator("#mobile-back-main-btn").inner_text() == "Wiki"
        finally:
            browser.close()
    finally:
        runtime.stop()

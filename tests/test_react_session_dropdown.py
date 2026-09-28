from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_session_dropdown_exposes_a_react_external_store_boundary():
    source = (ROOT / "ui/chat/elements/session-dropdown/list.js").read_text()

    assert "const sessionDropdownStore" in source
    assert "subscribe(listener)" in source
    assert "getSnapshot:" in source
    assert "_publishSessionDropdownSnapshot()" in source
    assert "selectedSessionIds" in source
    assert "_disposeLegacy" in (ROOT / "ui/chat/elements/session-dropdown/react-island.js").read_text()
    assert "presentation: _sessionDropdownPresentation" in source
    assert "activateReact()" in source


def test_react_selector_is_a_local_lazy_bundle():
    bridge = (ROOT / "ui/chat/elements/session-dropdown/react-island.js").read_text()
    config = (ROOT / "frontend/vite.config.mjs").read_text()
    package = (ROOT / "package.json").read_text()

    assert 'import("/ui/vendor/react-session-dropdown/react-session-dropdown.js?v=6")' in bridge
    assert "inlineDynamicImports: true" in config
    assert '"react"' in package and '"react-dom"' in package
    assert "cdn" not in bridge.lower()
    island = (ROOT / 'frontend/session-dropdown/index.jsx').read_text()
    assert 'id: "session-dropdown-trigger"' in island
    assert '"data-react-managed": "true"' in island

def test_session_selector_title_is_an_explicit_open_close_toggle():
    controller = (ROOT / "ui/chat/elements/session-dropdown/controller.js").read_text()

    assert "const menuIsOpen = !menu.hidden" in controller
    assert "menu.dataset.state === 'opening' || menu.dataset.state === 'open'" in controller
    assert "if (menuIsOpen) {\n      _lastToggleTime = Date.now();\n      closeMenu();" in controller
    assert "const cornerRadius = getComputedStyle(menu).borderTopLeftRadius" in controller

def test_session_menu_opens_before_session_data_work_begins():
    controller = (ROOT / "ui/chat/elements/session-dropdown/controller.js").read_text()

    assert "Render only phantom rows here: cache reads and row" in controller
    assert "_renderSessionMenuSkeleton();" in controller
    assert "if (requestId === _openRequestId && menu.dataset.state === 'open') _loadSessionList();" in controller
    assert "Start the request only after the skeleton has occupied" not in controller

def test_loading_shell_grows_to_the_final_viewport_cap():
    controller = (ROOT / "ui/chat/elements/session-dropdown/controller.js").read_text()
    selector_css = (ROOT / "ui/chat/elements/session-dropdown/index.css").read_text()

    assert "const targetHeight = availableHeight;" in controller
    assert "must not derive its opening height from the randomized skeleton" in controller
    assert '[data-loading="true"][data-state="opening"]' in selector_css
    assert 'height: var(--session-menu-open-height, 0px);' in selector_css


def test_react_handoff_disposes_persistent_title_row_listeners():
    controller = (ROOT / "ui/chat/elements/session-dropdown/controller.js").read_text()

    assert "const _dropdownEventAbort = new AbortController();" in controller
    assert "{ signal: _dropdownEventAbort.signal }" in controller
    assert "_dropdownEventAbort.abort();" in controller






def test_session_selector_menu_uses_the_title_row_corner_radius():
    selector_css = (ROOT / "ui/chat/elements/session-dropdown/index.css").read_text()

    shell_css = (ROOT / "ui/shared/css/app1.css").read_text()

    assert "--session-selector-radius: 14px" in selector_css
    assert "border-radius: var(--session-selector-radius, 14px)" in selector_css
    assert (
        "border-radius: 0 0 var(--session-selector-radius, 14px) "
        "var(--session-selector-radius, 14px)" in shell_css
    )
    assert "border-bottom-color: transparent !important;" in shell_css


def test_mobile_menu_uses_the_title_row_geometry_and_connected_panel():
    controller = (ROOT / "ui/chat/elements/session-dropdown/controller.js").read_text()
    shell_css = (ROOT / "ui/shared/css/app1.css").read_text()

    assert "const viewportHeight = viewport?.height || window.innerHeight;" in controller
    assert "const viewportCap = Math.max(100, Math.floor(viewportHeight * 0.66));" in controller
    assert "menu.style.left = rect.left + 'px';" in controller
    assert "menu.style.width = rect.width + 'px';" in controller
    assert "const isMobile" not in controller
    assert ".session-dropdown-menu:not(#session-dropdown-menu)" in shell_css
    assert "All layouts\n   use the same connected panel" in shell_css


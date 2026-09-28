from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_only_debug_and_chat_stay_visible_while_header_is_loading():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "ui/shared/css/design-system.css").read_text(encoding="utf-8")
    mobile_navigation = (
        ROOT / "ui/shared/js/mobile-navigation.js"
    ).read_text(encoding="utf-8")

    assert 'id="debug-console-toggle"' in html
    assert 'id="chat-toggle-btn"' in html
    assert "body.header-pending #main-tabs-wrap" not in css
    assert "body.header-pending #status-right" not in css
    assert "body.header-pending #main-tabs > :not(#debug-console-toggle)" in css
    assert "body.is-booting #main-tabs > :not(#debug-console-toggle)" in css
    assert "body.header-pending #main-tabs-chev-left" in css
    assert "body.is-booting .mobile-nav-toggle" in css

    mobile_success_start = mobile_navigation.index("const config = await response.json();")
    mobile_success = mobile_navigation[
        mobile_success_start : mobile_navigation.index("} catch (_)", mobile_success_start)
    ]
    assert mobile_success.index("_enableMobileNavigation()") < mobile_success.index("headerReady()")


def test_app_admin_header_controls_are_ordered_gated_and_mirrored_on_mobile():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    main = (ROOT / "ui/shared/js/main.js").read_text(encoding="utf-8")
    debug_console = (ROOT / "ui/shared/js/debugConsole.js").read_text(encoding="utf-8")
    mobile_navigation = (ROOT / "ui/shared/js/mobile-navigation.js").read_text(encoding="utf-8")

    debug = html.index('id="debug-console-toggle"')
    restart = html.index('id="header-restart-server"')
    reset = html.index('id="header-refresh-sw"')
    assert debug < restart < reset
    for control_id in ("debug-console-toggle", "header-restart-server", "header-refresh-sw"):
        tag_start = html.index(f'id="{control_id}"')
        tag_end = html.index(">", tag_start)
        assert "hidden" in html[tag_start:tag_end]
        assert f"'{control_id}'" in main
    assert "if (control) control.hidden = !admin;" in main

    assert "#debug-console-restart, #header-restart-server" in debug_console
    assert "restartServerAndReload" in debug_console
    assert "setTimeout(() => _restartResetBtn(btn), 3000)" in debug_console

    controls = mobile_navigation[
        mobile_navigation.index("const debug = document.getElementById"):
        mobile_navigation.index("if (controls.length", mobile_navigation.index("const debug = document.getElementById"))
    ]
    assert controls.index("debug-console-toggle") < controls.index("header-restart-server") < controls.index("header-refresh-sw")
    assert "kill-switch-toggle" not in controls
    assert "'Restart server', 'power', 'restart'" in mobile_navigation
    assert "if (kind === 'restart') _openMenu();" in mobile_navigation
    assert "if (document.body.classList.contains('mobile-mode')) _syncMenu();" in mobile_navigation


def test_minimum_shell_precedes_partial_loader_and_never_fetches():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    start = html.index("function paintMinimumShell()")
    end = html.index("import { partialsReady }", start)
    shell = html[start:end]

    assert "window.__pagesFallback()" not in shell
    assert "window.__pagesUnavailable()" in shell
    assert "window.__buildHeader(shellCatalog.main)" in shell
    assert "window.__boot.shellReady()" in shell
    assert "fetch(" not in shell


def test_shell_ready_does_not_publish_full_app_readiness():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    coordinator = html[html.index("function liftCurtain()") : html.index("<!-- Pull-to-refresh indicator")]

    assert "shellReady:     liftCurtain" in coordinator
    assert "function announceAppReady()" in coordinator
    assert "webagent-app-ready" in coordinator


def test_boot_shell_orbits_background_and_hides_blank_main_panel():
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")
    phantom = html[html.index("// ── Boot phantom cursor") : html.index("// Pinch-zoom guard")]

    assert "Math.min(W, H) * 0.42" in phantom
    assert "Math.cos(angle) * radius" in phantom
    assert "Math.sin(angle) * radius" in phantom
    assert "pickTarget" not in phantom
    assert "new CustomEvent('wa-boot-pointermove'" in phantom
    assert "new PointerEvent('pointermove'" not in phantom
    assert "body.is-booting #main-panel" in css
    assert "body.is-booting #chat-resize-handle" in css
    assert "visibility: hidden" in css


def test_boot_orbit_and_real_mouse_use_independent_background_inputs():
    cursor = (ROOT / "ui/shared/js/cursor-effects.js").read_text(encoding="utf-8")
    glow_css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")

    assert "--boot-gx" in cursor
    assert "--boot-gy" in cursor
    assert "#cursor-glow.boot-is-on" in glow_css
    assert "var(--boot-gx" in glow_css

    for relative in (
        "ui/background/stargaze/stargaze.js",
        "ui/background/bullet-grid/bullet-grid.js",
        "ui/background/particles/particles.js",
    ):
        source = (ROOT / relative).read_text(encoding="utf-8")
        assert "realPointer" in source
        assert "bootPointer" in source
        assert "wa-boot-pointermove" in source
        assert "wa-boot-pointerend" in source


def test_late_catalog_hydrates_pages_independently_and_preserves_user_navigation():
    loader = (ROOT / "ui/shared/js/partial-loader.js").read_text(encoding="utf-8")
    tabs = (ROOT / "ui/shared/js/tabs.js").read_text(encoding="utf-8")

    late = loader[loader.index("async function hydrateLateMainPages") : loader.index("function retryCatalogAfterCoreBoot")]
    assert "Promise.allSettled(requestedPages.map" in late
    assert "mount.dataset.pagePartial" in late
    assert "for (const extra of (page.partials || []))" in late
    assert "late page hydration failed" in late
    assert "Promise.all(missing.map" not in late

    assert "_userNavigated = true" in tabs
    assert "_lastRequestedTab = tabValue" in tabs
    assert "_userNavigated || !_lateCatalogReady" in tabs
    assert "_reconcileDefaultLanding" in tabs
    assert "pages-catalog-ready" in tabs
    assert "(_deepLinkHandled || _userNavigated) && requested" in tabs
    assert "activateTab(requested, false)" in tabs


def test_catalog_authority_and_inactive_partials_do_not_block_startup():
    loader = (ROOT / "ui/shared/js/partial-loader.js").read_text(encoding="utf-8")

    boot = loader[loader.index("export const partialsReady") :]
    assert "const cachedCatalog = window.__readPagesCache" in boot
    assert "catalog = cachedCatalog ||" in boot
    assert "Promise.race" not in boot
    assert "CATALOG_BOOT_TIMEOUT_MS" not in loader
    assert "setTimeout(resolve, 0)" not in loader
    assert "window.__ensurePagePartial" in loader
    assert "ensurePagePartial(requestedPageId, catalog)" in boot


def test_tab_page_start_waits_for_on_demand_partial_but_paints_immediately():
    tabs = (ROOT / "ui/shared/js/tabs.js").read_text(encoding="utf-8")
    activate = tabs[tabs.index("function activateTab") : tabs.index("// On load:")]

    active_paint = activate.index("targetContent.classList.add('active')")
    ensure = activate.index("window.__ensurePagePartial(tabValue)")
    start = activate.index("_startPage(tabValue)", ensure)
    assert active_paint < ensure < start
    assert "const activationSequence = ++_activationSequence" in activate
    assert "activationSequence !== _activationSequence" in activate
    assert "targetContent.setAttribute('aria-busy', 'true')" in activate


def test_late_header_buttons_use_the_atomic_tab_activation_path():
    tabs = (ROOT / "ui/shared/js/tabs.js").read_text(encoding="utf-8")

    # Catalog reconciliation replaces generated buttons. Delegation on the
    # stable strip ensures replacements cannot update only the visual button;
    # they must use the same activation path as the hidden select.
    assert "const tabBar = document.getElementById('main-tabs')" in tabs
    assert "tabBar.addEventListener('click'" in tabs
    assert "event.target.closest('.main-tab[data-value]')" in tabs
    assert "activateTab(value, true)" in tabs


def test_public_guest_boot_does_not_wait_for_private_setup_probe():
    main = (ROOT / "ui/shared/js/main.js").read_text(encoding="utf-8")
    auth = main[main.index("async function _initBootAuth()") : main.index("// ── App-wide access gate")]

    assert "if (!window.__agentId && mode !== 'public_registered')" in auth
    assert auth.index("mode !== 'public_registered'") < auth.index("fetch('/api/v1/auth/setup-status')")


def test_chat_does_not_render_anonymous_browser_limit_notice():
    partial = (ROOT / "ui/chat/chat-side-panel.html").read_text(encoding="utf-8")
    main = (ROOT / "ui/shared/js/main.js").read_text(encoding="utf-8")
    tier_editor = (
        ROOT / "ui/main-panel/app/settings/entitlements/entitlements.js"
    ).read_text(encoding="utf-8")
    user_panel = (ROOT / "ui/shared/js/user-panel.js").read_text(encoding="utf-8")

    assert 'id="chat-anon-registration-notice"' not in partial
    assert "_syncAnonRegistrationNotice" not in main
    assert "'anonymous_identity_source', 'anonymous_global_session'" in main
    assert "Global safety ceiling" in tier_editor
    assert "anon_identity_max" in tier_editor
    assert "anon_global_session_max" in tier_editor
    assert "anon_daily_chat_max" in tier_editor
    assert "anon_global_chat_max" in tier_editor
    assert "anon_budget_max" in tier_editor
    assert "anon_budget_window" in tier_editor
    assert "public_registration_ip_max" in tier_editor
    assert "public_registration_global_max" in tier_editor
    assert 'id="dd-register-toggle"' in user_panel
    assert "fetch('/api/v1/auth/register'" in user_panel
    assert "Authorization: `Bearer ${anonToken}`" in user_panel


def test_tier_editor_consolidates_anonymous_safeguards():
    tier_editor = (
        ROOT / "ui/main-panel/app/settings/entitlements/entitlements.js"
    ).read_text(encoding="utf-8")
    retired = ROOT / "ui/main-panel/app/users/policies/anonymous-limits.js"

    assert "Anonymous tier allowances" in tier_editor
    assert "Global safety ceiling" in tier_editor
    assert not retired.exists()
    for field_name in (
        "anon_session_max",
        "anon_identity_max",
        "anon_global_session_max",
        "public_registration_ip_max",
        "anon_chat_max",
        "anon_daily_chat_max",
        "anon_global_chat_max",
        "anon_budget_max",
        "anon_chat_ip_max",
    ):
        assert field_name in tier_editor


def test_instances_overview_has_anonymous_chat_kill_switch():
    instances = (ROOT / "ui/main-panel/app/instances.js").read_text(encoding="utf-8")

    assert "function _anonymousAccessSectionHtml()" in instances
    assert "data-act=\"anonymous-chat-toggle\"" in instances
    assert "'/admin/settings/anonymous-access'" in instances
    assert "anonymous_chat_enabled" in instances
    assert "registered accounts remain available" in instances


def test_users_page_has_native_anonymous_defense_dashboard():
    users = (ROOT / "ui/main-panel/app/users/users.js").read_text(encoding="utf-8")
    css = (ROOT / "ui/main-panel/app/users/users.css").read_text(encoding="utf-8")
    assert "/admin/users/anonymous-control" in users
    assert "Anonymous access controls" in users
    assert "anon_max_concurrent_runs" in users
    assert "anon_token_user_max" in users
    assert "anon_cost_global_microusd_max" in users
    assert "anon_risk_cooldown_score" in users
    assert "anon_error_max" in users
    assert "Resume anonymous access" in users
    assert "data-anon-user-action" in users
    assert ".anon-control-metrics" in css
    assert ".anon-user-guard" in css
    assert "Network actual:" in users
    assert '<th class="members-num">Tokens</th><th>Cost</th><th>Activity</th><th>Status</th><th>Limits</th>' in users
    assert "const tokensHtml" in users
    assert "const costHtml" in users
    assert "const activityHtml" in users
    assert "const statusHtml" in users
    assert "Native activity & limits" not in users
    assert "Guest actual:" not in users
    assert "Guest actual cost:" not in users
    assert "Central sessions" in users
    assert "Central messages" in users
    assert "hasCentralMetrics ?" in users
    assert "_anonAllowancePercent" in users
    assert "network_estimated_cost_microusd" in users
    assert "Network cost budget (lifetime)" in users
    assert "it does not renew" in users


def test_anonymous_budget_denial_becomes_registration_notice_not_retry():
    chat_send = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")

    assert "detail.code === 'registration_required'" in chat_send
    assert "_showAnonymousRegistrationRequired(registrationDetail.message)" in chat_send
    assert "showLeftOverlay();" in chat_send


def test_shell_lands_on_agents_not_instances_or_wiki():
    tabs = (ROOT / "ui/shared/js/tabs.js").read_text(encoding="utf-8")
    header = (ROOT / "ui/shared/js/header-build.js").read_text(encoding="utf-8")
    assert "const preferred = 'agents';" in tabs
    assert "A bare /app always starts in Agents" in tabs
    assert "FALLBACK_MAIN" not in header
    assert "window.__pagesFallback" not in header
    assert "_unavailable: true" in header
    assert "var CACHE_VERSION = 3" in header
    assert "saved.cacheVersion === CACHE_VERSION" in header


def test_anonymous_agent_create_card_is_a_visible_registration_preview():
    view = (ROOT / "ui/main-panel/agents/js/view.js").read_text(encoding="utf-8")

    assert "Register an account to name and create an agent" in view
    assert "Preview the agent configuration below" in view
    assert 'aria-disabled="true" aria-haspopup="dialog"' in view
    assert "Anonymous visitors can explore the configuration form" in view
    assert "if (_isAnonymousCreatePreview())" in view
    assert "showLeftOverlay();" in view


def test_new_agent_draft_reuses_the_persisted_config_renderer_and_payload():
    config = (ROOT / "ui/main-panel/agents/js/tab-config.js").read_text(encoding="utf-8")
    identity = (ROOT / "ui/main-panel/agents/js/identity-settings.js").read_text(encoding="utf-8")
    state = (ROOT / "ui/main-panel/agents/js/state.js").read_text(encoding="utf-8")
    utils = (ROOT / "ui/main-panel/agents/js/utils.js").read_text(encoding="utf-8")
    mock = (ROOT / "ui/main-panel/agents/js/mock-agent.js").read_text(encoding="utf-8")

    assert "renderAgentIdentitySettings(body, agent);" in config
    assert "if (!isMock) renderAgentIdentitySettings" not in config
    assert "_mockAgentDirtyFields" in state
    assert "_mockAgentConfigPayload" in state
    assert "if (_isMockAgent(agent))" in utils
    assert "_patchMockAgentDraft(updates)" in utils
    assert "...config," in mock
    assert "Register an account to name and create an agent" in identity


def test_legacy_quick_message_panel_is_removed():
    main = (ROOT / "ui/shared/js/main.js").read_text(encoding="utf-8")
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")
    settings = (ROOT / "app/admin/settings.py").read_text(encoding="utf-8")
    auth = (ROOT / "app/auth/__init__.py").read_text(encoding="utf-8")
    settings_js = (
        ROOT / "ui/main-panel/app/settings/app-settings/app-settings.js"
    ).read_text(encoding="utf-8")
    functions_page = (
        ROOT / "ui/main-panel/app/settings/pages/app-functions.html"
    ).read_text(encoding="utf-8")

    assert not (ROOT / "ui/shared/js/app-control-point.js").exists()
    assert not (ROOT / "ui/shared/css/app-control-point.css").exists()
    assert "app-control-point" not in main
    assert "app-control-point" not in html
    assert "app-control-point" not in worker
    assert "app_control_quick_message" not in settings
    assert "app_control_quick_message" not in auth
    assert "app_control_quick_message" not in settings_js
    assert "ac-app-control-point" not in functions_page
    assert 'const CACHE = "webagent-v' in worker


def test_private_access_page_is_a_segregated_shell_surface():
    private_root = ROOT / "ui/private-access/private-access-page"
    html = (private_root / "private-access-page.html").read_text(encoding="utf-8")
    css = (private_root / "private-access-page.css").read_text(encoding="utf-8")
    js = (private_root / "js/private-access-page.js").read_text(encoding="utf-8")
    readme = (private_root / "README.md").read_text(encoding="utf-8")
    shell = (ROOT / "index.html").read_text(encoding="utf-8")
    shared_css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")
    main = (ROOT / "ui/shared/js/main.js").read_text(encoding="utf-8")
    partials = (ROOT / "ui/shared/js/partial-loader.js").read_text(encoding="utf-8")

    assert "This app is private" in html
    assert "pending administrator approval" in html
    assert "data-private-access-sign-in" in html
    assert "private-access-card" in css
    assert "wirePrivateAccessPage" in js
    assert "Private Access" in readme
    assert "private-access-page.css" in shell
    assert "private-access-page.html" in partials
    assert "wirePrivateAccessPage" in main
    assert "#app-restricted-overlay::after" not in shared_css
    assert 'id="app-restricted-overlay"' in shell

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def test_device_picker_short_circuits_anonymous_users_before_fetch():
    source = _source("ui/shared/js/device-picker.js")
    load = source[source.index("async function load(force)") : source.index("function selfId()")]

    assert "if (_isAnonymousGuest(uid))" in load
    assert load.index("if (_isAnonymousGuest(uid))") < load.index("await fetch(url")
    assert "_cacheUid" in load
    assert "res.status === 401 || res.status === 403" in load
    assert "_accessDenied = true" in load


def test_sessions_page_allows_guests_and_stops_polling_on_api_denial():
    source = _source("ui/main-panel/agents/sessions/js/sessions-page.js")
    start = source[source.index("export function startSessions()") : source.index("export function stopSessions()")]
    load = source[source.index("async function _loadAndRender()") : source.index("// ── Exports")]

    assert "_isAnonymousGuest" not in source
    assert "isAnonGuest" not in source
    assert "if (_sourceAccessDenied)" in start
    assert start.index("if (_sourceAccessDenied)") < start.index("setInterval(_loadAndRender")
    assert "_renderAccessDeniedState();" in start
    assert "if (_sourceAccessDenied)" in load
    assert "_sourceAccessDenied = true" in source
    assert "clearInterval(_refreshTimer)" in source


def test_instances_page_gates_guests_before_admin_requests_and_polling():
    source = _source("ui/main-panel/app/instances.js")
    start = source[source.index("export function startView()") : source.index("export function stopView()")]

    assert "if (_isAnonymousGuest() || !isAdmin())" in start
    assert start.index("if (_isAnonymousGuest() || !isAdmin())") < start.index("_post('/admin/instances/canonical-url')")
    assert start.index("if (_isAnonymousGuest() || !isAdmin())") < start.index("S.poll = setInterval")
    assert "_renderAnonymousAccessGate(el);" in start
    assert "getAccessMode() === 'public_registered' && !isAuthenticated()" in source
    guest_check = source[source.index("function _isAnonymousGuest()") : source.index("function _renderAnonymousAccessGate")]
    assert "const uid = (app && app.currentUserId)" not in guest_check
    assert "if (!adminStatusReady())" in start
    assert "_isAnonymousGuest() || !isAdmin()" in start


def test_instances_first_open_uses_this_device_and_later_returns_keep_selection():
    source = _source("ui/main-panel/app/instances.js")
    start = source[source.index("export function startView()") : source.index("export function stopView()")]

    assert "_readActive()" not in start
    assert "if (!S.opened || S.active == null)" in start
    assert "S.active = S.self || _selfId();" in start
    assert "S.tab = 'overview';" in start
    assert "S.opened = true;" in start


def test_instances_live_self_replaces_cached_self_even_when_duplicate_still_exists():
    source = _source("ui/main-panel/app/instances.js")
    load = source[source.index("async function _load(foreground)") : source.index("// ── Local-network URL discovery")]

    assert "const previousSelf = S.self || _selfId();" in load
    assert "S.active === previousSelf || S.active === _STUB_ID" in load
    assert "_setActive(S.self || null);" in load


def test_instances_requests_carry_the_admin_bearer_token():
    source = _source("ui/main-panel/app/instances.js")

    get_request = source[source.index("async function _get(path)") : source.index("async function _post(path")]
    post_request = source[source.index("async function _post(path") : source.index("// ── Instant-paint helpers")]
    stream_request = source[source.index("async function _stream(path") : source.index("async function _httpsStream")]
    https_request = source[source.index("async function _httpsStream(path") : source.index("function _onClick(e)")]
    assert "authHeaders()" in get_request
    assert "authHeaders()" in post_request
    assert "authHeaders()" in stream_request
    assert "authHeaders()" in https_request

    for relative_path in (
        "ui/main-panel/app/dashboard/dashboard.js",
        "ui/main-panel/app/new-instance/new-instance.js",
        "ui/main-panel/app/settings/data-settings/deploy.js",
        "ui/main-panel/app/settings/data-settings/dns.js",
    ):
        module = _source(relative_path)
        assert "authHeaders" in module, relative_path
        assert "...authHeaders()" in module, relative_path


def test_instances_is_an_admin_only_shell_destination():
    descriptor = _source("ui/main-panel/app/page.json")
    tabs = _source("ui/shared/js/tabs.js")

    assert '"required_backend_capability": "role:platform_admin"' in descriptor
    admin_only = tabs[tabs.index("function _isAdminOnlyTab") : tabs.index("function _pageVisibilityBlocked")]
    assert "tabValue === 'instances'" in admin_only
    assert "activateTab(_deepLinkTab" in tabs


def test_shell_visibility_gate_explains_anonymous_instances_access():
    source = _source("ui/shared/js/page-access-gate.js")

    assert "showPageAccessGate(pageId = '')" in source
    assert "mountSignInForm(el, pageId)" in source
    assert "Instances" in source
    assert "unavailable for guest chats" in source
    assert 'data-page-access-state="blocked"' in source
    assert 'data-page-gate-action="account"' in source
    assert "gate-spinner" not in source


def test_v256_shell_delivers_the_current_instances_access_modules():
    index = _source("index.html")
    main = _source("ui/shared/js/main.js")
    tabs = _source("ui/shared/js/tabs.js")

    assert "import('./ui/shared/js/main.js?v=269')" in index
    assert "from './tabs.js?v=266'" in main
    assert "from './left-login.js?v=253'" in main
    assert "from './left-login.js?v=253'" in tabs
    assert "from './page-access-gate.js?v=253'" in tabs
    descriptor = _source("ui/main-panel/app/page.json")
    assert '"entry": "ui/main-panel/app/instances.js?v=256"' in descriptor
    assert "if (id === 'instances') moduleUrl.searchParams.set('v', '255');" in tabs


def test_instances_uses_the_shell_admin_status_singleton():
    instances_root = ROOT / "ui/main-panel/app"
    offenders = []
    for path in instances_root.rglob("*.js"):
        for line in path.read_text(encoding="utf-8").splitlines():
            if "left-login.js" in line and "left-login.js?v=253" not in line:
                offenders.append(f"{path.relative_to(ROOT)}: {line.strip()}")

    assert not offenders, "Instances loaded a separate admin-state module:\n" + "\n".join(offenders)


def test_visible_gated_header_button_routes_by_id_without_select_option():
    index = _source("index.html")
    tabs = _source("ui/shared/js/tabs.js")

    assert "window.__routeHeaderMainTab(val, nativeSelect)" in index
    assert "window.__routeMainTab(tabId, true)" in index
    assert "window.__routeMainTab = function (view" in tabs
    assert "_selectOptionBlocked(tabValue, tabSelect)" in tabs
    assert "_anonymousIdentityBlocked(tabValue)" in tabs
    assert "getActive().user_id" in tabs
    assert "document.getElementById('top-user-id')?.title" in tabs
    assert "#top-user-avatar-slot .user-avatar" in tabs
    assert "_deepLinkTab || _lastRequestedTab || tabSelect.value" in tabs
    assert "localStorage.getItem('auth_user_id') || uid" in index
    guard = tabs[tabs.index("function _selectOptionBlocked") : tabs.index("function setChatPanelVisible")]
    assert "isAnonGuest" not in guard
    assert "isAuthenticated" not in guard
    assert "getAccessMode" not in guard
    assert "stopImmediatePropagation" in index
    route = tabs[tabs.index("window.__routeMainTab = function") : tabs.index("// On load:")]
    assert "activateTab(view, userInitiated)" in route


def test_guest_admission_overlaps_access_mode_and_optional_ui_is_bounded():
    main = _source("ui/shared/js/main.js")
    auth = main[main.index("async function _initBootAuth()") : main.index("// ── App-wide access gate")]
    boot = main[main.index("const _bootReady") : main.index("// ── Visibility change")]

    assert auth.index("const guestAdmission =") < auth.index("await fetchAccessMode()")
    assert "await (guestAdmission || _requestGuestAdmission())" in auth
    assert "const _bootReady = _anonReady;" in main
    assert "setTimeout(res, 250)" in boot
    assert "setTimeout(resolve, 750)" in boot
    assert "controlsReady.then(() => _safeInit('initChatActivity'" in boot


def test_service_worker_maintenance_has_a_finite_boot_budget():
    index = _source("index.html")
    sw_boot = index[index.index("const registration = navigator.serviceWorker.register") : index.index("await Promise.all([")]

    assert "Promise.race([" in sw_boot
    assert "resolve(null), 1000" in sw_boot
    assert "setTimeout(resolve, 750)" in sw_boot
    assert "setTimeout(resolve, 2500)" in sw_boot
    assert "await navigator.serviceWorker.register" not in sw_boot
    assert "await reg.update()" not in sw_boot


def test_public_agent_guests_do_not_poll_account_session_endpoints():
    session_init = _source("ui/chat/js/session-init.js")
    session_list = _source("ui/chat/elements/session-dropdown/list.js")

    assert "if (isPublicAgentGuest()) return false;" in session_init
    assert "String(app.currentUserId || '').startsWith('agentmember--')" in session_init
    assert "window.__agentId && String(userId).startsWith('agentmember--')" in session_list
    assert "_sessionListAccessDenied = true;" in session_list

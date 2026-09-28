import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_service_worker_uses_version_coherent_module_cache():
    source = (ROOT / "sw.js").read_text(encoding="utf-8")

    code_branch = source.index("CODE_PATTERN.test(url.pathname)")
    passive_branch = source.index("STATIC_PATTERN.test(url.pathname)")
    assert code_branch < passive_branch
    assert "e.respondWith(networkFirstCode(request))" in source
    assert re.search(r'const CACHE = "webagent-v\d+";', source)
    network_first = source[source.index("async function networkFirstCode") : source.index("async function staleWhileRevalidate")]
    assert "return await fetchAndCache(request)" in network_first
    assert "const cache = await caches.open(CACHE)" in network_first
    assert "const cached = await cache.match(request)" in network_first
    assert "await caches.match(request)" not in network_first
    assert "new Request(request, { cache: 'reload' })" in source
    assert 'fetchAndCache(new Request(new URL(u, self.location.href), { cache: "reload" }))' in source


def test_service_worker_keeps_static_html_available_offline():
    source = (ROOT / "sw.js").read_text(encoding="utf-8")

    html_branch = source.index("HTML_PATTERN.test(url.pathname)")
    navigation_branch = source.index('request.mode === "navigate"')
    assert html_branch < navigation_branch
    assert '"/ui/chat/chat-side-panel.html"' in source
    assert '"/ui/shared/js/partial-loader.js"' in source


def test_public_agent_routes_use_root_api_base_and_never_get_generic_offline_shell():
    config = (ROOT / "ui/shared/js/config.js").read_text(encoding="utf-8")
    worker = (ROOT / "sw.js").read_text(encoding="utf-8")

    # Public IDs are not necessarily UUIDs: the shared default is a real route.
    assert "window.__agentId" in config
    assert "publicAgentPath" in config
    assert "path === publicAgentPath" in config
    assert "shared_default" in config

    # An unavailable public route must not impersonate a live agent page by
    # falling back to a cached contextless index shell.
    assert "function isPublicAgentNavigation(request)" in worker
    guard = worker[worker.index("async function networkFirstNavigation") :]
    assert "if (isPublicAgentNavigation(request))" in guard
    assert "Public link unavailable while offline" in guard
    assert guard.index("if (isPublicAgentNavigation(request))") < guard.index('cache.match("/index.html")')


def test_public_link_404_uses_the_normal_shell_without_a_failed_agent_identity():
    server = (ROOT / "app/main.py").read_text(encoding="utf-8")
    ui = (ROOT / "ui/shared/js/route-error.js").read_text(encoding="utf-8")

    assert "route_error=None" in server
    assert 'window.__routeError' in server
    assert 'route_error={"status": 404, "code": "public_agent_not_found"}' in server
    assert "status_code=route_error_status" in server
    assert "public_agent_not_found" in ui
    assert "window.__agentId" not in ui
    assert "Go to WebAgent" in ui


def test_offline_reader_queues_chat_but_blocks_session_mutations():
    reader = (ROOT / "ui/shared/js/offline-reader.js").read_text(encoding="utf-8")
    session_core = (ROOT / "ui/chat/js/session-core.js").read_text(encoding="utf-8")
    storage = (ROOT / "ui/chat/js/storage/storage-adapter.js").read_text(encoding="utf-8")

    assert "window.__webagentOfflineReadOnly = next" in reader
    assert "inputArea.inert = false" in reader
    assert "window.__webagentOfflineReadOnly === true" in session_core
    assert "Offline · cached data is read-only" in storage
    auth = (ROOT / "ui/shared/js/auth-refresh.js").read_text(encoding="utf-8")
    assert "window.__webagentOfflineReadOnly === true" in auth
    assert "method !== 'GET' && method !== 'HEAD'" in auth
    send = (ROOT / "ui/chat/js/chat-send.js").read_text(encoding="utf-8")
    assert "_queueMessageOffline(text, textOverride)" in send
    assert "Queued offline; waiting for confirmed server reconnection." in send
    assert "if (window.__webagentOfflineReadOnly === true) return false" in send
    css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")
    assert 'body[data-offline-readonly="true"] #chat-input-area' not in css


def test_cache_has_no_time_based_expiry_and_reconnect_reconciles_before_online():
    indexeddb = (ROOT / "ui/chat/js/storage/indexeddb.js").read_text(encoding="utf-8")
    adapter = (ROOT / "ui/chat/js/storage/storage-adapter.js").read_text(encoding="utf-8")
    reconnect = (ROOT / "ui/shared/js/reconnect.js").read_text(encoding="utf-8")
    lifecycle = (ROOT / "ui/shared/js/browser-lifecycle.js").read_text(encoding="utf-8")

    assert "for (const key of Object.keys(_lifecyclePolicy)) _lifecyclePolicy[key] = 0" in indexeddb
    assert "cache_expires_at: null" in adapter
    reconcile = reconnect.index("await window.__reconcilePagesCatalog()")
    online = reconnect.index("setConnectionState('ready')", reconcile)
    assert reconcile < online
    assert "clearCurrentTenantCache" in lifecycle
    clear_only = lifecycle[lifecycle.index("export async function clearCurrentTenantCache") :]
    assert "auth_token" not in clear_only


def test_chat_agent_selector_uses_the_tenant_cache_when_offline():
    source = (ROOT / "ui/chat/js/session-agent.js").read_text(encoding="utf-8")

    assert "await ensureAgentCacheHydrated()" in source
    assert "readAgentCache('list:main')" in source
    assert "writeAgentCache('list:main'" in source
    assert "if (!agentsData && saved)" in source


def test_cached_session_dropdown_remains_browsable_offline():
    css = (ROOT / "ui/shared/css/index.css").read_text(encoding="utf-8")

    assert '.session-dropdown[data-offline="true"][data-loaded="true"] .session-dropdown-trigger' in css
    assert "pointer-events: auto" in css


def test_signout_obscures_privileged_ui_before_network_revocation():
    source = (ROOT / "ui" / "shared" / "js" / "user-panel.js").read_text(encoding="utf-8")
    transition = source.index("webagent-auth-transition-start")
    overlay = source.index("webagent-auth-transition")
    revoke = source.index("/api/v1/auth/logout", transition)
    assert transition < revoke
    assert overlay < revoke


def test_optional_codex_portal_404_is_an_empty_capability():
    source = (
        ROOT / "ui" / "chat" / "elements" / "session-dropdown" / "list.js"
    ).read_text(encoding="utf-8")
    assert "if (portalRes.status === 404)" in source
    assert "_portalSessionsCache = { userId, sessions: [], loadedAt: Date.now() };" in source


def test_unpaired_connector_does_not_auto_connect():
    background = (
        ROOT / "clients" / "browser-extension" / "background.js"
    ).read_text(encoding="utf-8")
    options = (
        ROOT / "clients" / "browser-extension" / "options.js"
    ).read_text(encoding="utf-8")
    manifest = (
        ROOT / "clients" / "browser-extension" / "manifest.json"
    ).read_text(encoding="utf-8")

    assert "autoConnect: true" in background
    assert "autoConnect: true" in options
    assert "if (!cfg.token)" in background
    assert "if (!cfg.autoConnect || !cfg.token) return;" in background
    assert 'const VERSION = "0.2.0"' in background
    assert '"version": "0.2.0"' in manifest
    assert 'msg.action === "shutdown"' in background
    assert "installation_id: installationId" in background


def test_mobile_navigation_does_not_rerender_its_observed_tab_strip():
    source = (
        ROOT / "ui" / "shared" / "js" / "mobile-navigation.js"
    ).read_text(encoding="utf-8")

    assert "lucide.createIcons" not in source
    assert "new MutationObserver" not in source

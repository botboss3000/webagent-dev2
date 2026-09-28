"""Real-browser coverage for the cached shell, queued composer, and safe UI."""

from __future__ import annotations

import functools
import http.server
import threading
import unittest
from pathlib import Path


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class OfflineReaderBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")
        root = Path(__file__).resolve().parents[1]
        handler = functools.partial(_QuietHandler, directory=str(root))
        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.playwright = sync_playwright().start()
        try:
            cls.browser = cls.playwright.chromium.launch(headless=True)
        except Exception as exc:
            cls.playwright.stop()
            cls.server.shutdown()
            raise unittest.SkipTest(f"Chromium unavailable: {exc}")
        cls.context = cls.browser.new_context(service_workers="allow")
        cls.page = cls.context.new_page()
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"
        cls.page.goto(f"{cls.base}/ui/diagnostics.html")

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "context"):
            cls.context.close()
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    def test_one_install_serves_shell_and_chat_partial_offline(self):
        self.page.evaluate(
            """async () => {
              await navigator.serviceWorker.register('/sw.js');
              await navigator.serviceWorker.ready;
              if (!navigator.serviceWorker.controller) {
                await new Promise(resolve => {
                  navigator.serviceWorker.addEventListener('controllerchange', resolve, {once: true});
                });
              }
            }"""
        )
        # Complete one real online app visit so the worker sees the executable
        # module graph exactly as a user's browser would.
        self.page.goto(f"{self.base}/index.html", wait_until="domcontentloaded")
        self.page.wait_for_selector("#chat-panel #chat-input", timeout=15000)
        self.context.set_offline(True)
        try:
            # /app is not a static file on this test server. Its success proves
            # the navigation strategy fell back to the cached /index.html shell.
            response = self.page.goto(f"{self.base}/app", wait_until="domcontentloaded")
            self.page.wait_for_selector("#chat-panel #chat-input", timeout=15000)
            result = self.page.evaluate(
                """async () => {
                  const [shell, chat] = await Promise.all([
                    fetch('/index.html'),
                    fetch('/ui/chat/chat-side-panel.html'),
                  ]);
                  return {
                    navigationShell: document.querySelector('#chat-panel #chat-input') !== null,
                    shellOk: shell.ok,
                    shellText: (await shell.text()).includes('id="chat-panel"'),
                    chatOk: chat.ok,
                    chatText: (await chat.text()).includes('id="chat-input"'),
                  };
                }"""
            )
        finally:
            self.context.set_offline(False)
        self.assertEqual(
            result,
            {
                "navigationShell": True,
                "shellOk": True,
                "shellText": True,
                "chatOk": True,
                "chatText": True,
            },
        )

    def test_reader_state_makes_mutation_surfaces_inert(self):
        result = self.page.evaluate(
            """async ({base}) => {
              document.body.innerHTML = `
                <div id="status-right"></div>
                <button id="chat-toggle-btn"><i class="lucide-icon"></i></button>
                <div id="chat-input-area"><textarea id="chat-input"></textarea></div>
                <button id="session-new-header-btn">New</button>`;
              const reader = await import(base + '/ui/shared/js/offline-reader.js');
              reader.initOfflineReader();
              reader.setOfflineReadOnly(true, {source: 'test'});
              const active = {
                flag: window.__webagentOfflineReadOnly,
                body: document.body.dataset.offlineReadonly,
                inputInert: document.getElementById('chat-input-area').inert,
                newInert: document.getElementById('session-new-header-btn').inert,
                badgeAbsent: !document.getElementById('offline-reader-badge'),
                chatAura: document.getElementById('chat-toggle-btn').classList.contains('chat-aura-red'),
                chatTitle: document.getElementById('chat-toggle-btn').title,
              };
              reader.setOfflineReadOnly(false, {source: 'test'});
              active.restored = !document.getElementById('chat-input-area').inert;
              return active;
            }""",
            {"base": self.base},
        )
        self.assertEqual(
            result,
            {
                "flag": True,
                "body": "true",
                "inputInert": False,
                "newInert": True,
                "badgeAbsent": True,
                "chatAura": True,
                "chatTitle": "Offline · cached views unavailable",
                "restored": True,
            },
        )

    def test_offline_composer_queues_without_a_network_write(self):
        page = self.browser.new_page()
        try:
            page.goto(f"{self.base}/ui/diagnostics.html")
            result = page.evaluate(
                """async ({base}) => {
                  document.body.innerHTML = `<button id="chat-toggle-btn"></button>
                    <div id="chat-messages"></div><div id="chat-input-area">
                    <div id="chat-input-row" class="chat-pill"><textarea id="chat-input">Queue me safely</textarea>
                    <button id="chat-send">Send</button></div></div>`;
                  localStorage.setItem('auth_token', 'offline-token');
                  localStorage.setItem('auth_user_id', 'offline-user');
                  const policy = await import(base + '/ui/shared/js/browser-storage-policy.js');
                  const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
                  policy.configureBrowserStoragePolicy({mode: 'persistent_cache', ownerScope: 'offline_queue_test'});
                  db.setOwnerScope('offline_queue_test');
                  const {default: kvCache} = await import(base + '/ui/chat/js/storage/kv-cache.js');
                  await kvCache.hydrate();
                  const {app} = await import(base + '/ui/shared/js/state.js');
                  Object.assign(app, {
                    currentUserId: 'offline-user', currentAgentId: 'offline-agent',
                    currentSessionId: 'offline-session',
                    chatInput: document.getElementById('chat-input'),
                    chatSend: document.getElementById('chat-send'),
                    chatMessages: document.getElementById('chat-messages'),
                  });
                  const reader = await import(base + '/ui/shared/js/offline-reader.js');
                  reader.initOfflineReader();
                  reader.setOfflineReadOnly(true, {source: 'test'});
                  const chatSend = await import(base + '/ui/chat/js/chat-send.js');
                  let networkWrites = 0;
                  let reconnected = false;
                  const originalFetch = window.fetch;
                  window.fetch = async (...args) => {
                    if (!['GET', 'HEAD'].includes(String(args[1]?.method || 'GET').toUpperCase())) networkWrites++;
                    return reconnected
                      ? new Response(JSON.stringify({status: 'ok', turn_id: 'saved-turn'}), {
                          status: 200, headers: {'Content-Type': 'application/json'},
                        })
                      : new Response('{}', {status: 503});
                  };
                  await chatSend.sendMessage();
                  const writesWhileOffline = networkWrites;
                  const queued = JSON.parse(localStorage.getItem('webagent.pendingMessages.v1') || '[]');
                  const queuedBubble = document.querySelector('[data-offline-queued="1"]')?.textContent;
                  const queuedNotice = document.getElementById('offline-reader-composer-notice')?.textContent;
                  reconnected = true;
                  reader.setOfflineReadOnly(false, {source: 'test-reconnect'});
                  const deadline = Date.now() + 2000;
                  while (Date.now() < deadline
                      && JSON.parse(localStorage.getItem('webagent.pendingMessages.v1') || '[]').length) {
                    await new Promise(resolve => setTimeout(resolve, 20));
                  }
                  window.fetch = originalFetch;
                  return {
                    input: app.chatInput.value, writesWhileOffline, networkWrites, queued,
                    remaining: JSON.parse(localStorage.getItem('webagent.pendingMessages.v1') || '[]').length,
                    bubble: queuedBubble, notice: queuedNotice,
                  };
                }""",
                {"base": self.base},
            )
            self.assertEqual(result["input"], "")
            self.assertEqual(result["writesWhileOffline"], 0)
            self.assertEqual(len(result["queued"]), 1)
            self.assertEqual(result["queued"][0]["text"], "Queue me safely")
            self.assertEqual(result["queued"][0]["session_id"], "offline-session")
            self.assertEqual(result["queued"][0]["transport"], "server")
            self.assertIn("queued offline", result["bubble"])
            self.assertIn("message queued", result["notice"])
            self.assertEqual(result["networkWrites"], 1)
            self.assertEqual(result["remaining"], 0)
        finally:
            page.close()

    def test_catalog_uses_old_tenant_snapshot_and_never_generic_fallback(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const originalFetch = window.fetch.bind(window);
              const source = await (await originalFetch(base + '/ui/shared/js/header-build.js')).text();
              document.body.innerHTML = `
                <div id="main-tabs"></div>
                <select id="main-tab-select"><option value="account">Manage Account</option></select>
                <div id="main-panel"><div id="tab-account" class="tab-content"></div></div>`;
              localStorage.setItem('auth_user_id', 'tenant-member');
              localStorage.setItem('auth_token', 'x.eyJleHAiOjF9.x');
              localStorage.setItem('pagesCatalogCache', JSON.stringify({
                cacheVersion: 3,
                identity: 'tenant-member',
                savedAt: 1,
                catalog: {main: [{
                  id: 'private-view', label: 'Private view', icon: 'lock',
                  order: 1, mount: '#tab-private-view', visibility: 'auth'
                }], admin: []}
              }));
              (0, eval)(source);
              window.fetch = async () => { throw new TypeError('offline'); };
              const cached = await window.__loadPagesCatalog();
              window.__buildHeader(cached.main);
              const tenantView = {
                id: cached.main[0]?.id,
                tabText: document.getElementById('main-tabs').textContent,
                hidden: window.__pageHiddenByVisibility('private-view'),
              };

              localStorage.setItem('auth_user_id', 'different-tenant');
              window.__pagesCatalog = null;
              window.__pagesCatalogAuthoritative = false;
              const unavailable = await window.__loadPagesCatalog();
              window.__buildHeader(unavailable.main);
              window.fetch = originalFetch;
              return {
                tenantView,
                unavailable: unavailable._unavailable === true,
                pageCount: unavailable.main.length,
                tabs: document.getElementById('main-tabs').textContent,
                notice: document.querySelector('[data-catalog-unavailable="1"]')?.textContent,
                fallbackDefined: typeof window.__pagesFallback !== 'undefined',
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["tenantView"]["id"], "private-view")
        self.assertIn("Private view", result["tenantView"]["tabText"])
        self.assertFalse(result["tenantView"]["hidden"])
        self.assertTrue(result["unavailable"])
        self.assertEqual(result["pageCount"], 0)
        self.assertEqual(result["tabs"], "")
        self.assertIn("Cached views unavailable", result["notice"])
        self.assertFalse(result["fallbackDefined"])

    def test_authoritative_empty_catalog_does_not_block_public_guest_chat(self):
        result = self.page.evaluate(
            """async ({base}) => {
              const source = await (await fetch(base + '/ui/shared/js/header-build.js')).text();
              document.body.innerHTML = `
                <div id="main-tabs"></div>
                <select id="main-tab-select"><option value="account">Manage Account</option></select>
                <div id="main-panel"><div id="tab-chat" class="tab-content active">Chat is available</div><div id="tab-account" class="tab-content"></div></div>`;
              (0, eval)(source);
              window.__buildHeader([]);
              return {
                notice: document.querySelector('[data-catalog-unavailable="1"]')?.textContent || '',
                chatVisible: !!document.getElementById('tab-chat')?.offsetParent,
                tabs: document.getElementById('main-tabs').textContent,
              };
            }""",
            {"base": self.base},
        )
        self.assertEqual(result["notice"], "")
        self.assertTrue(result["chatVisible"])
        self.assertEqual(result["tabs"], "")

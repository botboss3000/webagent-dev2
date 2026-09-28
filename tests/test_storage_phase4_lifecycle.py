"""Phase 4 browser inventory, lifecycle, and memory-only contracts."""

from __future__ import annotations

import functools
import http.server
import json
import threading
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from app.db import browser_policy, storage_router


ROOT = Path(__file__).resolve().parents[1]


class InventoryContractTests(unittest.TestCase):
    def test_every_store_has_release_blocking_lifecycle_fields(self):
        inventory = json.loads(
            (ROOT / "docs" / "browser-storage-inventory.json").read_text("utf-8")
        )
        stores = {
            store["name"]: store
            for database in inventory["databases"]
            for store in database["stores"]
        }
        self.assertEqual(
            set(stores),
            {
                "sessions",
                "interactions",
                "agent_config",
                "session_runs",
                "memories",
                "genui_pages",
                "genui_html",
                "user_files",
                "sync_outbox",
                "tool_details",
                "app_cache",
                "attachments",
            },
        )
        required = {
            "purpose",
            "schema_version",
            "classification",
            "data_categories",
            "identifiers",
            "tenant_key",
            "sensitivity",
            "secrets_forbidden",
            "writers",
            "readers",
            "sync_destination",
            "retention",
            "quota",
            "export",
            "erasure",
            "logout_purge",
            "remote_invalidation",
            "migration",
        }
        for name, entry in stores.items():
            self.assertFalse(required - set(entry), name)
            self.assertTrue(entry["logout_purge"], name)
            self.assertTrue(entry["secrets_forbidden"], name)

    def test_memory_only_and_disabled_fail_closed_server_capabilities(self):
        for mode in ("memory_only", "disabled"):
            with patch.object(
                browser_policy,
                "load_browser_storage_policy",
                return_value=browser_policy.BrowserStoragePolicy(
                    persistence_mode=mode
                ),
            ), patch.dict(
                "os.environ",
                {
                    "WEBAGENT_ENABLE_BROWSER_AUTHORITY": "true",
                    "WEBAGENT_ENABLE_BROWSER_SESSION_CACHE": "true",
                },
                clear=False,
            ):
                self.assertFalse(storage_router.browser_authority_enabled())
                self.assertFalse(storage_router.browser_session_cache_enabled())


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format, *args):
        return


class ChromiumLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise unittest.SkipTest(f"Playwright unavailable: {exc}")
        handler = functools.partial(_QuietHandler, directory=str(ROOT))
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
        cls.base = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, "browser"):
            cls.browser.close()
        if hasattr(cls, "playwright"):
            cls.playwright.stop()
        if hasattr(cls, "server"):
            cls.server.shutdown()
            cls.server.server_close()

    def _page(self):
        page = self.browser.new_page()
        page.goto(f"{self.base}/ui/chat/index.html")
        return page

    def _scope(self):
        return f"phase4_{uuid.uuid4().hex}"

    def test_real_schema_matches_inventory_and_scoped_blob_export(self):
        page = self._page()
        try:
            result = page.evaluate(
                """async ({base, first, second}) => {
                  const policy = await import(base + '/ui/shared/js/browser-storage-policy.js');
                  const {default: db, SESSION_DB_STORES} = await import(
                    base + '/ui/chat/js/storage/indexeddb.js'
                  );
                  const attachments = await import(base + '/ui/shared/js/attachments-idb.js');
                  const lifecycle = await import(base + '/ui/shared/js/browser-lifecycle.js');
                  policy.configureBrowserStoragePolicy({mode: 'persistent_cache'});
                  db.setOwnerScope(first);
                  attachments.setAttachmentOwnerScope(first);
                  await db.ready();
                  await attachments.putAttachment({
                    id: 'blob', blob: new Blob(['tenant-one'], {type: 'text/plain'}),
                    name: 'one.txt', size: 10,
                  });
                  const exported = await lifecycle.exportBrowserData(first);
                  const attachment = exported.databases[
                    `webagent_attachments_${first}`
                  ].stores.attachments[0].blob;

                  attachments.closeAttachmentStorage();
                  attachments.setAttachmentOwnerScope(second);
                  await attachments.putAttachment({
                    id: 'blob', blob: new Blob(['tenant-two'], {type: 'text/plain'}),
                    name: 'two.txt', size: 10,
                  });
                  const purged = await lifecycle.purgeBrowserData(first);
                  const names = (await indexedDB.databases()).map(row => row.name);
                  return {
                    sessionStores: SESSION_DB_STORES,
                    actualStores: Object.keys(exported.databases[
                      `webagent_session_db_${first}`
                    ].stores),
                    attachment,
                    complete: exported.complete,
                    purged: purged.complete,
                    firstPresent: names.includes(`webagent_attachments_${first}`),
                    secondPresent: names.includes(`webagent_attachments_${second}`),
                  };
                }""",
                {"base": self.base, "first": self._scope(), "second": self._scope()},
            )
            self.assertTrue(result["complete"])
            self.assertTrue(result["purged"])
            self.assertEqual(
                set(result["sessionStores"]), set(result["actualStores"])
            )
            self.assertEqual(result["attachment"]["size"], 10)
            self.assertEqual(result["attachment"]["base64"], "dGVuYW50LW9uZQ==")
            self.assertFalse(result["firstPresent"])
            self.assertTrue(result["secondPresent"])
        finally:
            page.close()

    def test_memory_only_never_opens_indexeddb_or_persists_account(self):
        page = self._page()
        try:
            result = page.evaluate(
                """async ({base, scope}) => {
                  let opens = 0;
                  const realOpen = indexedDB.open.bind(indexedDB);
                  indexedDB.open = (...args) => { opens += 1; return realOpen(...args); };
                  const policy = await import(base + '/ui/shared/js/browser-storage-policy.js');
                  const accounts = await import(base + '/ui/shared/js/accounts.js');
                  const attachments = await import(base + '/ui/shared/js/attachments-idb.js');
                  const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
                  policy.configureBrowserStoragePolicy({
                    mode: 'memory_only', ownerScope: scope,
                  });
                  attachments.setAttachmentOwnerScope(scope);
                  accounts.upsertAccount({
                    user_id: 'u', username: 'u', access_token: 'sentinel-token',
                    remember_token: 'sentinel-remember',
                  });
                  await attachments.putAttachment({
                    id: 'a', blob: new Blob(['ram']), name: 'ram.txt', size: 3,
                  });
                  let readyFailed = false;
                  db.setOwnerScope(scope);
                  try { await db.ready(); } catch (_) { readyFailed = true; }
                  const stats = await attachments.getStats();
                  return {
                    opens, readyFailed, stats,
                    active: accounts.getActive()?.user_id,
                    durableAccounts: localStorage.getItem('webagent_accounts'),
                    durableToken: localStorage.getItem('auth_token'),
                    durableRemember: localStorage.getItem('remember_token'),
                    names: (await indexedDB.databases()).map(row => row.name),
                  };
                }""",
                {"base": self.base, "scope": self._scope()},
            )
            self.assertEqual(result["opens"], 0)
            self.assertTrue(result["readyFailed"])
            self.assertEqual(result["stats"], {"count": 1, "bytes": 3})
            self.assertEqual(result["active"], "u")
            self.assertIsNone(result["durableAccounts"])
            self.assertIsNone(result["durableToken"])
            self.assertIsNone(result["durableRemember"])
            self.assertFalse(any("webagent_" in name for name in result["names"]))
        finally:
            page.close()

    def test_cache_summary_and_clear_preserve_current_authentication(self):
        page = self._page()
        try:
            result = page.evaluate(
                """async ({base, scope}) => {
                  const policy = await import(base + '/ui/shared/js/browser-storage-policy.js');
                  const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
                  const attachments = await import(base + '/ui/shared/js/attachments-idb.js');
                  const lifecycle = await import(base + '/ui/shared/js/browser-lifecycle.js');
                  policy.configureBrowserStoragePolicy({
                    mode: 'persistent_cache', ownerScope: scope, schemaVersion: 11,
                  });
                  db.setOwnerScope(scope);
                  attachments.setAttachmentOwnerScope(scope);
                  localStorage.setItem('auth_user_id', 'cache-owner');
                  localStorage.setItem('auth_token', 'sentinel-auth-token');
                  localStorage.setItem('remember_token', 'sentinel-remember-token');
                  localStorage.setItem('webagent_accounts', '[{"user_id":"cache-owner"}]');
                  localStorage.setItem('webagent.browserCacheCtx.v1', JSON.stringify({
                    mode: 'persistent_cache', owner_scope: scope, schema_version: 11,
                  }));
                  localStorage.setItem('pagesCatalogCache', JSON.stringify({
                    cacheVersion: 3, identity: 'cache-owner', savedAt: 1700000000000,
                    catalog: {main: [{id: 'one'}, {id: 'two'}], admin: []},
                  }));
                  await db.createSession({
                    id: 'cached-session', agent_id: 'a',
                    last_validated_at: '2025-01-02T03:04:05.000Z',
                  });
                  await db.addInteractions('cached-session', [
                    {id: 'i1', role: 'user', content: 'cached'},
                    {id: 'i2', role: 'assistant', content: 'reply'},
                  ]);
                  await attachments.putAttachment({
                    id: 'a1', blob: new Blob(['attachment']), name: 'a.txt', size: 10,
                  });
                  const summary = await lifecycle.getBrowserCacheSummary(scope);
                  const cleared = await lifecycle.clearCurrentTenantCache(scope);
                  const names = (await indexedDB.databases()).map(row => row.name);
                  return {
                    summary: {
                      schema: summary.context.schema_version,
                      pages: summary.counts.pages,
                      sessions: summary.counts.sessions,
                      transcripts: summary.counts.transcripts,
                      attachments: summary.counts.attachments,
                      bytesPositive: summary.approximate_cache_bytes > 0,
                    },
                    complete: cleared.complete,
                    tenantDatabasesRemain: names.some(name => name.endsWith(scope)),
                    auth: localStorage.getItem('auth_token'),
                    remember: localStorage.getItem('remember_token'),
                    accounts: localStorage.getItem('webagent_accounts'),
                    context: localStorage.getItem('webagent.browserCacheCtx.v1'),
                    catalog: localStorage.getItem('pagesCatalogCache'),
                  };
                }""",
                {"base": self.base, "scope": self._scope()},
            )
            self.assertEqual(
                result["summary"],
                {
                    "schema": 11,
                    "pages": 2,
                    "sessions": 1,
                    "transcripts": 2,
                    "attachments": 1,
                    "bytesPositive": True,
                },
            )
            self.assertTrue(result["complete"])
            self.assertFalse(result["tenantDatabasesRemain"])
            self.assertEqual(result["auth"], "sentinel-auth-token")
            self.assertEqual(result["remember"], "sentinel-remember-token")
            self.assertEqual(result["accounts"], '[{"user_id":"cache-owner"}]')
            self.assertIn('"owner_scope"', result["context"])
            self.assertIsNone(result["catalog"])
        finally:
            page.close()

    def test_storage_pressure_is_reported_without_deleting_existing_data(self):
        page = self._page()
        try:
            result = page.evaluate(
                """async ({base}) => {
                  const policy = await import(base + '/ui/shared/js/browser-storage-policy.js');
                  let pressure = null;
                  window.addEventListener('webagent-browser-storage-pressure', event => {
                    pressure = event.detail;
                  }, {once: true});
                  policy.configureBrowserStoragePolicy({
                    mode: 'persistent_cache', ownerScope: 'pressure', maxBytes: 1,
                  });
                  let errorName = '';
                  try { await policy.assertBrowserCapacity(2); }
                  catch (error) { errorName = error.name; }
                  return {errorName, pressure};
                }""",
                {"base": self.base},
            )
            self.assertEqual(result["errorName"], "QuotaExceededError")
            self.assertEqual(result["pressure"]["requested_bytes"], 2)
            self.assertEqual(result["pressure"]["quota_bytes"], 1)
        finally:
            page.close()

    def test_user_management_keeps_local_cache_panel_when_server_is_down(self):
        page = self._page()
        try:
            result = page.evaluate(
                """async ({base, scope}) => {
                  const policy = await import(base + '/ui/shared/js/browser-storage-policy.js');
                  const {default: db} = await import(base + '/ui/chat/js/storage/indexeddb.js');
                  policy.configureBrowserStoragePolicy({
                    mode: 'persistent_cache', ownerScope: scope, schemaVersion: 9,
                  });
                  db.setOwnerScope(scope);
                  localStorage.setItem('auth_user_id', 'local-admin');
                  localStorage.setItem('auth_display_name', 'Local Admin');
                  localStorage.setItem('pagesCatalogCache', JSON.stringify({
                    cacheVersion: 3, identity: 'local-admin', savedAt: Date.now(),
                    catalog: {main: [{id: 'cached'}], admin: []},
                  }));
                  await db.createSession({id: 'local-session', agent_id: 'a'});
                  const realFetch = window.fetch.bind(window);
                  window.fetch = async input => String(input).includes('/admin/')
                    ? new Response('{"detail":"offline"}', {status: 503})
                    : realFetch(input);
                  document.body.innerHTML = '<main id="host"></main>';
                  const users = await import(base + '/ui/main-panel/app/users/users.js');
                  users.mountUsers(document.getElementById('host'));
                  await new Promise(resolve => setTimeout(resolve, 300));
                  const panel = document.querySelector('.browser-cache-panel');
                  const result = {
                    panel: Boolean(panel),
                    policy: document.querySelector('.members-policy-title')?.textContent,
                    account: panel?.querySelector('[data-cache-account]')?.textContent,
                    pages: panel?.querySelector('[data-cache-pages]')?.textContent,
                    rosterError: document.querySelector('.members-roster-error')?.textContent,
                    clearLabel: panel?.querySelector('[data-cache-clear]')?.textContent,
                  };
                  users.unmountUsers();
                  return result;
                }""",
                {"base": self.base, "scope": self._scope()},
            )
            self.assertTrue(result["panel"])
            self.assertEqual(result["policy"], "Access policy")
            self.assertEqual(result["account"], "Local Admin")
            self.assertEqual(result["pages"], "1")
            self.assertEqual(result["clearLabel"], "Clear cached data")
            self.assertIn("Local cache controls remain available", result["rosterError"])
        finally:
            page.close()

    def test_partial_delete_never_acknowledges_and_retry_succeeds(self):
        page = self._page()
        try:
            result = page.evaluate(
                """async ({base, userId, revision}) => {
                  const payload = btoa(JSON.stringify({
                    user_id: userId, sub: userId, rev: revision,
                  })).replace(/=/g, '').replace(/\\+/g, '-').replace(/\\//g, '_');
                  const token = `header.${payload}.signature`;
                  const input = new TextEncoder().encode(
                    `webagent-browser-cache:${userId}:${revision}`,
                  );
                  const bytes = new Uint8Array(await crypto.subtle.digest('SHA-256', input));
                  const scope = Array.from(
                    bytes, byte => byte.toString(16).padStart(2, '0'),
                  ).join('').slice(0, 24);
                  localStorage.setItem('webagent_accounts', JSON.stringify([{
                    user_id: userId, username: userId, access_token: token,
                  }]));
                  localStorage.setItem('webagent_active_user_id', userId);
                  localStorage.setItem('auth_user_id', userId);
                  const {default: db} = await import(
                    base + '/ui/chat/js/storage/indexeddb.js'
                  );
                  db.setOwnerScope(scope);
                  await db.createSession({id: 'must-purge', agent_id: 'a'});
                  db.close();

                  let acknowledgements = 0;
                  window.fetch = async url => {
                    if (String(url).includes('/device/purge-ack')) acknowledgements += 1;
                    return new Response('{}', {status: 200});
                  };
                  const realDelete = indexedDB.deleteDatabase.bind(indexedDB);
                  let injectFailure = true;
                  indexedDB.deleteDatabase = name => {
                    if (injectFailure && name === `webagent_session_db_${scope}`) {
                      const request = {};
                      queueMicrotask(() => request.onblocked?.());
                      return request;
                    }
                    return realDelete(name);
                  };
                  const {purgeAndAcknowledge} = await import(
                    base + '/ui/shared/js/device-purge.js'
                  );
                  const first = await purgeAndAcknowledge(
                    token, {forgetAccount: false},
                  );
                  const firstAcks = acknowledgements;
                  injectFailure = false;
                  const second = await purgeAndAcknowledge(
                    token, {forgetAccount: false},
                  );
                  return {first, firstAcks, second, acknowledgements};
                }""",
                {
                    "base": self.base,
                    "userId": f"user_{uuid.uuid4().hex}",
                    "revision": 4,
                },
            )
            self.assertEqual(
                result,
                {
                    "first": False,
                    "firstAcks": 0,
                    "second": True,
                    "acknowledgements": 1,
                },
            )
        finally:
            page.close()


if __name__ == "__main__":
    unittest.main()

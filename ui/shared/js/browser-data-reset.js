'use strict';

/**
 * browser-data-reset.js — complete local browser wipe for the header refresh
 * button (#header-refresh-sw, mirrored into the compact mobile sidebar).
 *
 * That button used to drop only the service worker and its Cache Storage
 * buckets. It now performs a full local reset so the next load behaves like a
 * brand-new incognito window: every service worker registration, every Cache
 * Storage bucket, localStorage, sessionStorage, every IndexedDB database, and
 * every script-readable cookie on this origin is removed before the reload.
 *
 * Deliberately dependency-free: debugConsole.js is loaded directly by
 * index.html on every page boot (so the console still works when the main
 * module graph cannot finish loading), so importing the chat-storage or
 * attachment graph merely to delete it would tax every cold start on mobile.
 *
 * Limits of the browser sandbox — not oversights:
 *  - HttpOnly cookies are invisible to page script and cannot be removed here.
 *    The app keeps auth in localStorage, so the reload still lands anonymous.
 *  - Other origins' data and the HTTP cache are out of reach; those need the
 *    browser's own site-data settings.
 *  - The app's own IndexedDB connections are closed before deletion, because an
 *    open connection turns deleteDatabase into an onblocked rejection; a
 *    deletion still blocked by ANOTHER tab is bounded by a timeout so the reset
 *    always reaches its reload.
 */

const COOKIE_EXPIRY = 'Thu, 01 Jan 1970 00:00:00 GMT';
const DB_DELETE_TIMEOUT_MS = 3000;

/** Delete one IndexedDB database, bounded so a blocked request cannot hang the
 *  reset (mirrors the pattern in attachments-idb.js). */
function _deleteDatabase(name, timeoutMs = DB_DELETE_TIMEOUT_MS) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      reject(new Error(`Timed out deleting ${name}; another tab may be holding it open`));
    }, timeoutMs);
    const request = indexedDB.deleteDatabase(name);
    request.onsuccess = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve();
    };
    request.onerror = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(request.error || new Error(`Could not delete ${name}`));
    };
    request.onblocked = () => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      reject(new Error(`Deletion blocked for ${name}`));
    };
  });
}

/** Every path a cookie on this page could have been set for: the current path
 *  and each of its ancestors, plus root. */
function _cookiePaths() {
  const paths = new Set(['/']);
  let path = window.location.pathname || '/';
  while (path && path !== '/') {
    paths.add(path);
    const parent = path.replace(/\/[^/]*$/, '');
    path = parent === path ? '/' : (parent || '/');
  }
  return [...paths];
}

/** Domain variants a cookie may carry. The no-domain form is tried first so
 *  host-locked cookies (__Host-*) are still expired rather than rejected. */
function _cookieDomains() {
  const host = window.location.hostname;
  if (!host || host === 'localhost' || /^[\d.]+$/.test(host)) return [null];
  const domains = [null, host, `.${host}`];
  const parts = host.split('.');
  if (parts.length > 2) domains.push(`.${parts.slice(-2).join('.')}`);
  return [...new Set(domains)];
}

function _clearCookies() {
  const completed = [];
  const failures = [];
  let names = [];
  try {
    names = document.cookie
      .split(';')
      .map((entry) => entry.split('=')[0].trim())
      .filter(Boolean);
  } catch (error) {
    failures.push({ target: 'cookies', error: String(error?.message || error) });
    return { completed, failures };
  }
  if (!names.length) return { completed, failures };

  const paths = _cookiePaths();
  const domains = _cookieDomains();
  for (const name of new Set(names)) {
    for (const path of paths) {
      for (const domain of domains) {
        const suffix = `; path=${path}; expires=${COOKIE_EXPIRY}; max-age=0`
          + (domain ? `; domain=${domain}` : '');
        try {
          document.cookie = `${name}=${suffix}`;
        } catch (error) {
          failures.push({ target: `cookie:${name}`, error: String(error?.message || error) });
        }
      }
    }
    completed.push(`cookie:${name}`);
  }
  return { completed, failures };
}

/**
 * Close the app's own IndexedDB connections before deleting their databases.
 * Only two modules open a connection (ui/chat/js/storage/indexeddb.js and
 * ui/shared/js/attachments-idb.js) and both expose a close; ES module identity
 * means these calls hit the live singletons. Dynamic imports keep this module
 * dependency-free at boot — nothing is fetched until the button is pressed,
 * and both closes are safe no-ops when no connection was ever opened.
 */
async function _closeAppStorageConnections() {
  await Promise.all([
    import('../../chat/js/storage/indexeddb.js')
      .then((mod) => { mod.default?.close?.(); })
      .catch(() => {}),
    import('./attachments-idb.js')
      .then((mod) => { mod.closeAttachmentStorage?.(); })
      .catch(() => {}),
  ]);
}

/**
 * Wipe every local store this origin exposes and report what was removed.
 * Never throws for a single failed target — the caller reloads regardless, and
 * the returned `failures` list is the honest record of anything left behind.
 */
export async function purgeAllBrowserData() {
  const completed = [];
  const failures = [];

  // 1. Service workers — every registration for this origin, not just the one
  //    currently controlling this page.
  if (navigator.serviceWorker?.getRegistrations) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      for (const registration of registrations) {
        const target = `service-worker:${registration.scope || 'unknown'}`;
        try {
          await registration.unregister();
          completed.push(target);
        } catch (error) {
          failures.push({ target, error: String(error?.message || error) });
        }
      }
    } catch (error) {
      failures.push({ target: 'service-worker', error: String(error?.message || error) });
    }
  }

  // 2. Cache Storage — every bucket, not only the webagent-* ones.
  if (globalThis.caches?.keys) {
    try {
      for (const name of await caches.keys()) {
        try {
          await caches.delete(name);
          completed.push(`cache:${name}`);
        } catch (error) {
          failures.push({ target: `cache:${name}`, error: String(error?.message || error) });
        }
      }
    } catch (error) {
      failures.push({ target: 'cache-storage', error: String(error?.message || error) });
    }
  }

  // 3. IndexedDB — release our own connections first, then enumerate and
  //    delete every database the origin exposes.
  await _closeAppStorageConnections();
  if (typeof indexedDB.databases === 'function') {
    try {
      const names = (await indexedDB.databases()).map((row) => row?.name).filter(Boolean);
      for (const name of names) {
        try {
          await _deleteDatabase(name);
          completed.push(`indexeddb:${name}`);
        } catch (error) {
          failures.push({ target: `indexeddb:${name}`, error: String(error?.message || error) });
        }
      }
    } catch (error) {
      failures.push({ target: 'indexeddb-catalog', error: String(error?.message || error) });
    }
  }

  // 4. Web storage — a full clear with no device-level allowlist. The narrower
  //    logout purge preserves preferences; this button's whole point is to
  //    leave nothing behind.
  for (const [label, storage] of [['localStorage', localStorage], ['sessionStorage', sessionStorage]]) {
    try {
      storage.clear();
      completed.push(label);
    } catch (error) {
      failures.push({ target: label, error: String(error?.message || error) });
    }
  }

  // 5. Cookies readable by page script.
  const cookies = _clearCookies();
  completed.push(...cookies.completed);
  failures.push(...cookies.failures);

  const result = { complete: failures.length === 0, completed_targets: completed, failures };

  // In-memory caches (kvCache, the chat message/focus maps) subscribe to this
  // event and drop their rows, and the chat unload-mirrors check the flag it
  // sets — so the pagehide handlers that fire during the reload below cannot
  // write purged state back into the storage we just cleared.
  try {
    window.dispatchEvent(new CustomEvent('webagent-browser-storage-purge', {
      detail: { reason: 'full-browser-reset', ...result },
    }));
  } catch (_) {}

  return result;
}

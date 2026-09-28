'use strict';

import sessionDB, { SESSION_DB_STORES } from '../../chat/js/storage/indexeddb.js';
import {
  clearAttachmentStorage,
  exportAttachmentStorage,
  getStats as getAttachmentStats,
  revokeAllObjectUrls,
  setAttachmentOwnerScope,
} from './attachments-idb.js';
import { getBrowserStorageContext } from './browser-storage-policy.js';

const SESSION_PREFIX = 'webagent_session_db_';
const ATTACHMENT_PREFIX = 'webagent_attachments_';
const LEGACY_ATTACHMENT_DB = 'webagent-attachments';
const STORAGE_PRESSURE_KEY = 'webagent.browserStoragePressure.v1';

function _expected(scope, { includeLegacy = true } = {}) {
  const databases = [
    `${SESSION_PREFIX}${scope}`,
    `${ATTACHMENT_PREFIX}${scope}`,
  ];
  if (includeLegacy) databases.push(LEGACY_ATTACHMENT_DB);
  return databases;
}

async function _databaseNames() {
  if (typeof indexedDB.databases !== 'function') return [];
  return (await indexedDB.databases()).map(row => row?.name).filter(Boolean);
}

// Keys that are genuinely device-level (not tenant data) and safe to keep
// across a purge. EVERYTHING else in localStorage is tenant/UI state and is
// swept on logout/policy-transition so no key can leak into the next tenant —
// including keys added later (the old explicit list had exactly this
// forget-a-key bug for the Phase-C UI-state keys: files.*, chat_*, genui:* …).
const _PURGE_ALLOWLIST = new Set([
  'wa_appearance',        // device-level theme choice
  'wa_bg_choice',         // device-level background-engine choice
  'tutorialPrefs',        // device-level tutorial completion
  'pagesCatalogCache',    // device-level page-catalog cache
  'webagent_embed_bid',   // device-level embed widget browser id
]);

async function _clearWebStorage() {
  // localStorage: FULL sweep minus the device-level allowlist — structurally
  // leak-proof against keys that forget to register here.
  for (const storage of [localStorage]) {
    const keys = [];
    try {
      for (let i = 0; i < storage.length; i++) {
        const k = storage.key(i);
        if (k && !_PURGE_ALLOWLIST.has(k)) keys.push(k);
      }
    } catch (_) {}
    for (const key of keys) {
      try { storage.removeItem(key); } catch (_) {}
    }
  }
  // sessionStorage is per-tab and transient; sweep the explicit tenant-ish keys
  // (device-level entries like debugConsole / safety-splash / onboarding flags
  // survive).
  for (const key of [
    'webagent_accounts', 'webagent_active_user_id',
    'webagent_device_cache_scopes', 'auth_token', 'auth_username',
    'auth_user_id', 'auth_display_name', 'remember_token',
    'anonUserId', 'terminalUserId',
    // Chat cache keys — migrated to app_cache rows; these legacy blobs and the
    // still-localStorage outbox/draft must never survive into another tenant.
    'sessionFocus.v1', 'sessionManifest.v1',
    'webagent.lastSessionSeq.v1', 'webagent.messagePersistenceReceipts.v1',
    'webagent.chatDraft.v1', 'webagent.pendingMessages.v1',
    'lastSessionPerAgent',
    // Per-tab caches that are tenant-scoped.
    'inst.devices.v1', 'ws:pendingModelSel',
  ]) {
    try { sessionStorage.removeItem(key); } catch (_) {}
  }
}

async function _clearUserCaches() {
  if (!globalThis.caches?.keys) return;
  const names = await caches.keys();
  for (const name of names) {
    if (/webagent.*(user|session|attachment|chat)/i.test(name)) {
      if (!(await caches.delete(name))) {
        throw new Error(`Cache Storage deletion failed: ${name}`);
      }
    }
  }
}

function _catalogEnvelope() {
  try {
    const saved = JSON.parse(localStorage.getItem('pagesCatalogCache') || 'null');
    const identity = localStorage.getItem('auth_user_id') || '__anonymous__';
    return saved && saved.identity === identity ? saved : null;
  } catch (_) {
    return null;
  }
}

function _catalogPageCount(saved) {
  const catalog = saved?.catalog || {};
  const ids = new Set();
  for (const area of ['main', 'admin', 'splash']) {
    for (const page of (Array.isArray(catalog[area]) ? catalog[area] : [])) {
      if (page?.id) ids.add(page.id);
    }
  }
  return ids.size;
}

/** Return current-tenant cache diagnostics for the local browser only. */
export async function getBrowserCacheSummary(scope = sessionDB.ownerScope) {
  const context = getBrowserStorageContext();
  const resolvedScope = scope || context.owner_scope || '';
  const catalog = _catalogEnvelope();
  let database = null;
  let attachments = { count: 0, bytes: 0 };
  let sessions = [];
  const failures = [];
  if (resolvedScope && context.mode === 'persistent_cache') {
    try {
      sessionDB.setOwnerScope(resolvedScope);
      database = {
        counts: await sessionDB.stats(),
        bytes: await sessionDB.estimateSizeBytes(),
      };
      sessions = await sessionDB.listSessions();
    } catch (error) {
      failures.push({ target: 'session-cache', error: String(error?.message || error) });
    }
    try {
      setAttachmentOwnerScope(resolvedScope);
      attachments = await getAttachmentStats();
    } catch (error) {
      failures.push({ target: 'attachment-cache', error: String(error?.message || error) });
    }
  }
  let browser = { usage: 0, quota: 0 };
  try {
    if (navigator.storage?.estimate) browser = await navigator.storage.estimate();
  } catch (error) {
    failures.push({ target: 'browser-estimate', error: String(error?.message || error) });
  }
  const validated = [
    catalog?.savedAt ? new Date(catalog.savedAt).toISOString() : null,
    ...sessions.map(row => row?.last_validated_at).filter(Boolean),
  ].filter(Boolean).sort().at(-1) || null;
  let catalogBytes = 0;
  try { catalogBytes = new Blob([JSON.stringify(catalog || {})]).size; } catch (_) {}
  let storagePressure = null;
  try {
    const savedPressure = JSON.parse(localStorage.getItem(STORAGE_PRESSURE_KEY) || 'null');
    if (savedPressure?.owner_scope === resolvedScope) storagePressure = savedPressure;
  } catch (_) {}
  return {
    context: { ...context, owner_scope: resolvedScope },
    identity: catalog?.identity || (() => {
      try { return localStorage.getItem('auth_user_id') || ''; } catch (_) { return ''; }
    })(),
    last_validated_at: validated,
    counts: {
      pages: _catalogPageCount(catalog),
      sessions: Number(database?.counts?.sessions || 0),
      transcripts: Number(database?.counts?.interactions || 0),
      attachments: Number(attachments.count || 0),
    },
    approximate_cache_bytes: Number(database?.bytes || 0) + Number(attachments.bytes || 0) + catalogBytes,
    browser_usage_bytes: Math.max(0, Number(browser.usage) || 0),
    browser_quota_bytes: Math.max(0, Number(browser.quota) || 0),
    storage_pressure: storagePressure,
    failures,
  };
}

function _clearCurrentCatalog() {
  const saved = _catalogEnvelope();
  if (!saved) return false;
  try { localStorage.removeItem('pagesCatalogCache'); } catch (_) {}
  window.__pagesCatalog = null;
  window.__pagesCatalogAuthoritative = false;
  return true;
}

/**
 * Clear cached read-only content for the active tenant without removing auth,
 * remembered accounts, browser policy context, or device preferences.
 */
export async function clearCurrentTenantCache(scope = sessionDB.ownerScope) {
  const context = getBrowserStorageContext();
  const resolvedScope = scope || context.owner_scope || '';
  const expected = _expected(resolvedScope, { includeLegacy: false });
  const completed = [];
  const failures = [];
  if (!resolvedScope) {
    failures.push({ target: 'owner_scope', error: 'owner scope is not initialized' });
  } else {
    try {
      sessionDB.setOwnerScope(resolvedScope);
      sessionDB.close();
      await sessionDB.deleteDatabase();
      completed.push(`${SESSION_PREFIX}${resolvedScope}`);
    } catch (error) {
      failures.push({ target: `${SESSION_PREFIX}${resolvedScope}`, error: String(error?.message || error) });
    }
    try {
      setAttachmentOwnerScope(resolvedScope);
      const result = await clearAttachmentStorage({ deleteDatabase: true, includeLegacy: false });
      completed.push(...result.completed);
      failures.push(...result.failures);
    } catch (error) {
      failures.push({ target: `${ATTACHMENT_PREFIX}${resolvedScope}`, error: String(error?.message || error) });
    }
  }
  revokeAllObjectUrls();
  if (_clearCurrentCatalog()) completed.push('pagesCatalogCache');
  try {
    const pressure = JSON.parse(localStorage.getItem(STORAGE_PRESSURE_KEY) || 'null');
    if (pressure?.owner_scope === resolvedScope) localStorage.removeItem(STORAGE_PRESSURE_KEY);
  } catch (_) {}
  for (const key of [
    'sessionFocus.v1', 'sessionManifest.v1', 'webagent.lastSessionSeq.v1',
    'webagent.messagePersistenceReceipts.v1', 'webagent.chatDraft.v1',
    'webagent.pendingMessages.v1', 'lastSessionPerAgent',
  ]) {
    try { sessionStorage.removeItem(key); } catch (_) {}
  }
  try { await _clearUserCaches(); } catch (error) {
    failures.push({ target: 'cache-storage', error: String(error?.message || error) });
  }
  try {
    const remaining = new Set(await _databaseNames());
    for (const name of expected) {
      if (remaining.has(name)) failures.push({ target: name, error: 'database still exists after deletion' });
    }
  } catch (error) {
    failures.push({ target: 'indexeddb-verification', error: String(error?.message || error) });
  }
  const result = {
    complete: failures.length === 0,
    context: { ...context, owner_scope: resolvedScope },
    completed_targets: [...new Set(completed)],
    failures,
  };
  window.dispatchEvent(new CustomEvent('webagent-browser-storage-purge', {
    detail: { reason: 'cache-clear', ...result },
  }));
  return result;
}

/**
 * Export all inventoried browser stores. Any failed required read makes the
 * envelope explicitly incomplete and the caller must not describe it as a
 * complete data export.
 */
export async function exportBrowserData(scope = sessionDB.ownerScope) {
  const context = getBrowserStorageContext();
  const failures = [];
  const databases = {};
  if (!scope) {
    return {
      format: 'webagent-browser-lifecycle-export',
      version: 2,
      complete: false,
      failures: [{ database: null, error: 'owner scope is not initialized' }],
      context,
      databases,
    };
  }
  sessionDB.setOwnerScope(scope);
  setAttachmentOwnerScope(scope);
  if (context.mode === 'persistent_cache') {
    try {
      const exported = await sessionDB.exportAll();
      databases[exported.database.name] = exported.database;
    } catch (error) {
      failures.push({ database: `${SESSION_PREFIX}${scope}`, error: String(error?.message || error) });
    }
  } else {
    databases[`${SESSION_PREFIX}${scope}`] = {
      name: `${SESSION_PREFIX}${scope}`,
      version: 7,
      persistence: context.mode,
      stores: Object.fromEntries(SESSION_DB_STORES.map(name => [name, []])),
    };
  }
  try {
    const exported = await exportAttachmentStorage();
    databases[exported.database.name] = exported.database;
  } catch (error) {
    failures.push({ database: `${ATTACHMENT_PREFIX}${scope}`, error: String(error?.message || error) });
  }
  return {
    format: 'webagent-browser-lifecycle-export',
    version: 2,
    complete: failures.length === 0,
    created_at: new Date().toISOString(),
    context: { ...context, owner_scope: scope },
    databases,
    failures,
  };
}

/**
 * Purge and verify every tenant browser data path. A partial result is
 * deliberately retryable and must never be used for purge acknowledgement.
 */
export async function purgeBrowserData(scope, { includeLegacy = true } = {}) {
  const context = getBrowserStorageContext();
  const expected = _expected(scope, { includeLegacy });
  const completed = [];
  const failures = [];
  if (!scope) {
    failures.push({ target: 'owner_scope', error: 'owner scope is not initialized' });
  } else {
    try {
      sessionDB.setOwnerScope(scope);
      sessionDB.close();
      await sessionDB.deleteDatabase();
      completed.push(`${SESSION_PREFIX}${scope}`);
    } catch (error) {
      failures.push({ target: `${SESSION_PREFIX}${scope}`, error: String(error?.message || error) });
    }
    try {
      setAttachmentOwnerScope(scope);
      const result = await clearAttachmentStorage({ deleteDatabase: true, includeLegacy });
      completed.push(...result.completed);
      failures.push(...result.failures);
    } catch (error) {
      failures.push({ target: `${ATTACHMENT_PREFIX}${scope}`, error: String(error?.message || error) });
    }
  }

  revokeAllObjectUrls();
  try { await _clearWebStorage(); } catch (error) {
    failures.push({ target: 'web-storage', error: String(error?.message || error) });
  }
  try { await _clearUserCaches(); } catch (error) {
    failures.push({ target: 'cache-storage', error: String(error?.message || error) });
  }

  // Deletion success events are insufficient: verify the database catalog.
  try {
    const remaining = new Set(await _databaseNames());
    for (const name of expected) {
      if (remaining.has(name)) {
        failures.push({ target: name, error: 'database still exists after deletion' });
      }
    }
  } catch (error) {
    failures.push({ target: 'indexeddb-verification', error: String(error?.message || error) });
  }
  const uniqueCompleted = [...new Set(completed)].filter(
    name => !failures.some(failure => failure.target === name),
  );
  const result = {
    complete: failures.length === 0 && expected.every(name => uniqueCompleted.includes(name)),
    context: { ...context, owner_scope: scope },
    expected_databases: expected,
    completed_databases: uniqueCompleted,
    failures,
  };
  // Single source of truth for the purge event: every tenant-purge path
  // (policy transition in storage-adapter, device revocation in device-purge)
  // funnels through here, so in-memory caches (kvCache) can reset on a real
  // purge without each caller dispatching its own event — and — critically —
  // without missing the SUCCESS path (device-purge previously fired only on
  // failure, leaving stale rows in memory after a successful logout).
  if (typeof window !== 'undefined') {
    window.dispatchEvent(new CustomEvent('webagent-browser-storage-purge', {
      detail: { reason: 'lifecycle', ...result },
    }));
  }
  return result;
}

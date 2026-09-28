'use strict';

// ── FEATURE: UI-state persistence through kvCache (with sync localStorage mirror) ──
// Single-blob UI state (explorer tabs, terminal layout, chat per-session prefs …)
// lives in the tenant-scoped IndexedDB app_cache via kvCache (schema 7). Because
// module-load reads run synchronously while kvCache hydrates asynchronously from
// IndexedDB, every key keeps a synchronous localStorage MIRROR:
//   • reads prefer kvCache (authoritative once hydrated) and fall back to the
//     mirror while it survives (boot-time reads + first boot after migration);
//   • writes go to kvCache (async IDB) AND the mirror (sync — a pagehide /
//     visibilitychange write is guaranteed to land);
//   • purgeBrowserData() sweeps the whole localStorage bucket on logout, so a
//     stale mirror never leaks into the next tenant.
// In memory_only / disabled mode the mirror is skipped (the storage policy
// forbids browser writes) and kvCache degrades to pure in-memory — state is
// intentionally ephemeral there, exactly as before.

import { kvCache } from '../../chat/js/storage/kv-cache.js';
import { browserPersistenceAllowed } from './browser-storage-policy.js';

function _readLegacy(key) {
  try { return localStorage.getItem(key); } catch (_) { return null; }
}

function _parse(raw) {
  if (raw === null || raw === undefined || raw === '') return undefined;
  try { return JSON.parse(raw); } catch (_) { return raw; } // plain-string keys
}

/** Read UI state: kvCache row first, then the legacy localStorage mirror. */
export function kvRead(kcKey, legacyKey) {
  const v = kvCache.get(kcKey);
  if (v !== undefined && v !== null) return v;
  return _parse(_readLegacy(legacyKey));
}

/** Write UI state: kvCache row (async IDB) + sync localStorage mirror. */
export function kvWrite(kcKey, legacyKey, value) {
  kvCache.set(kcKey, value);
  if (!browserPersistenceAllowed()) return; // memory_only/disabled: never touch localStorage
  const raw = typeof value === 'string' ? value : JSON.stringify(value);
  try { localStorage.setItem(legacyKey, raw); } catch (_) {}
}

/** Delete UI state from both layers. */
export function kvDelete(kcKey, legacyKey) {
  kvCache.del(kcKey);
  try { localStorage.removeItem(legacyKey); } catch (_) {}
}

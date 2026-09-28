'use strict';

/**
 * kvCache — generic key-value cache on top of the tenant IndexedDB database.
 *
 * Migration target for the localStorage blobs that used to hold chat
 * bookkeeping (per-session focus, manifests, seq cursors, persistence
 * receipts) and UI state. Writes go to the `app_cache` object store
 * (DB_VERSION 7+); reads are served synchronously from an in-memory Map so
 * existing callers keep their sync call sites.
 *
 * Behaviour contract:
 *  - get()/getAll() are SYNCHRONOUS and never throw. Until hydration
 *    completes (or if it never runs — e.g. normal/server mode), they fall
 *    back to the registered legacy localStorage blob, so a boot-time read
 *    (session-init's _loadSessionFocus, chat-send's module-scope seq load)
 *    sees exactly what it saw before the migration. Once hydrated, reads
 *    come from the Map and legacy keys are migrated then removed.
 *  - set()/del() update the in-memory Map immediately and flush to IndexedDB
 *    through a serialized promise chain. Writes are fire-and-forget: like the
 *    old localStorage path this is a cache, never a send blocker.
 *  - memory_only / disabled policy modes degrade to the in-memory Map (no
 *    IndexedDB writes, legacy keys are left untouched), exactly like the rest
 *    of the storage layer.
 *  - Rows are durable. The legacy expires_at field remains at 0 for schema
 *    compatibility, but cache reads and lifecycle cleanup never age rows out.
 *
 * Purge/logout is structural: purgeBrowserData deletes the whole tenant
 * database, and this module resets its Map on the purge/policy events so a
 * later page can never serve stale rows for a different tenant.
 */

import defaultSessionDB, { valueByteSize } from './indexeddb.js';
import {
  assertBrowserCapacity,
  browserPersistenceAllowed,
} from '../../../shared/js/browser-storage-policy.js';

const STORE = 'app_cache';

const _mem = new Map();      // key -> { key, value, expires_at, size, updated_at }
const _legacy = new Map();   // prefix -> legacy localStorage key (map-shaped fallback seam)
const _legacyValues = new Map(); // cacheKey -> { legacyKey, parseJson } (whole-blob fallback seam)
let _hydrated = false;
let _hydratePromise = null;
let _flushChain = Promise.resolve();
let _lastPolicyScope = '';
// Purge/reset epoch. reset() bumps it so flushes queued BEFORE the purge are
// dropped even when the tenant scope value itself did not change (purgeBrowserData
// re-sets ownerScope to the outgoing tenant's scope while deleting, so a scope
// comparison alone would let a queued write re-create the deleted database).
let _epoch = 0;
// Hard-off flag: set by reset() (purge/logout/policy transition) and cleared by
// the next hydrate(). Between purge and the next boot the page may still be
// alive (reconcile ticks, focus captures) with ownerScope still pointing at the
// outgoing tenant — without this flag those post-purge writes would re-create
// the just-deleted database. The epoch guard only covers flushes queued before
// the purge; this covers writes made after it.
let _purged = false;

function _now() { return Date.now(); }

function _rowIsLive(row) {
  return !!row;
}

function _putRow(row) {
  // The tenant scope is captured at ENQUEUE time: this write is only legal for
  // the tenant that queued it. If the user logs out and into a different tenant
  // before the flush runs (reset() swaps the chain pointer but already-queued
  // callbacks survive), the write must never land in the new tenant's database.
  const scope = defaultSessionDB.ownerScope;
  // The epoch is also captured at enqueue: a purge/reset invalidates every
  // queued flush even when ownerScope is re-set to the same value during the
  // purge (purgeBrowserData does exactly that), which would otherwise re-create
  // the just-deleted database with a stale row.
  const epoch = _epoch;
  // Serialize writes so put/delete order matches call order (last-write-wins).
  _flushChain = _flushChain.then(async () => {
    if (!browserPersistenceAllowed() || !defaultSessionDB.ownerScope) return;
    if (_purged) return;              // purge since enqueue — the tenant DB is gone
    if (epoch !== _epoch) return; // purged/reset since enqueue — drop the write
    if (defaultSessionDB.ownerScope !== scope) return; // tenant changed — drop
    try {
      await assertBrowserCapacity(row.size);
      const db = await defaultSessionDB.ready();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite');
        const req = tx.objectStore(STORE).put(row);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
      });
    } catch (_) { /* quota / blocked storage — cache writes are never fatal */ }
  });
  return _flushChain;
}

function _delRow(key) {
  const scope = defaultSessionDB.ownerScope; // captured at enqueue (see _putRow)
  const epoch = _epoch;                       // captured at enqueue (see _putRow)
  _flushChain = _flushChain.then(async () => {
    if (!browserPersistenceAllowed() || !defaultSessionDB.ownerScope) return;
    if (epoch !== _epoch) return; // purged/reset since enqueue — drop the write
    if (defaultSessionDB.ownerScope !== scope) return; // tenant changed — drop
    try {
      const db = await defaultSessionDB.ready();
      await new Promise((resolve, reject) => {
        const tx = db.transaction(STORE, 'readwrite');
        const req = tx.objectStore(STORE).delete(key);
        req.onsuccess = () => resolve();
        req.onerror = () => reject(req.error);
      });
    } catch (_) { /* non-fatal */ }
  });
  return _flushChain;
}

async function _getAllRows(db) {
  return new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
}

/** Parse a legacy localStorage map blob (best-effort, never throws). */
function _readLegacy(legacyKey) {
  try {
    const raw = localStorage.getItem(legacyKey);
    if (!raw) return null;
    const obj = JSON.parse(raw);
    return obj && typeof obj === 'object' ? obj : null;
  } catch (_) { return null; }
}

/** Which registered legacy blob (if any) backs a given key? */
function _legacyFor(key) {
  for (const [prefix, legacyKey] of _legacy) {
    if (key.startsWith(prefix)) return { legacyKey, suffix: key.slice(prefix.length) };
  }
  return null;
}

export const kvCache = {

  /** @returns {boolean} true once rows are in memory (or the backend is memory-only). */
  isHydrated() { return _hydrated; },

  /**
   * Register the legacy localStorage map blob that backs `prefix` until
   * hydration migrates it. Safe to call at module scope in any mode. If
   * hydration already completed (persistent), migrates immediately.
   */
  registerLegacyMap(legacyKey, prefix) {
    _legacy.set(prefix, legacyKey);
    if (_hydrated && browserPersistenceAllowed() && defaultSessionDB.ownerScope) {
      this.migrateLegacyMap(legacyKey, prefix);
    }
    return this;
  },

  /**
   * Register a legacy localStorage blob that backs ONE whole app_cache row
   * (`cacheKey`) — e.g. the outbox array or the raw-string chat draft, which
   * are not map-shaped. Until hydration migrates it, get(cacheKey) falls back
   * to the blob. `parseJson` (default true) controls whether the blob is
   * parsed or returned as a raw string (drafts are stored raw).
   */
  registerLegacyValue(legacyKey, cacheKey, { parseJson = true } = {}) {
    _legacyValues.set(cacheKey, { legacyKey, parseJson });
    if (_hydrated && browserPersistenceAllowed() && defaultSessionDB.ownerScope) {
      this.migrateLegacyValue(legacyKey, cacheKey, parseJson);
    }
    return this;
  },

  /**
   * Load every app_cache row for the current tenant into memory. Idempotent.
   * No-op (memory-only) when persistence is unavailable or no tenant scope
   * has been issued yet. Writes made before hydration are flushed afterwards
   * without ever clobbering a newer row another tab wrote.
   */
  async hydrate() {
    if (_hydrated) return _hydratePromise;
    _purged = false; // a new tenant context is being established — writes may resume
    if (!browserPersistenceAllowed() || !defaultSessionDB.ownerScope) {
      _hydrated = true;
      return Promise.resolve();
    }
    _hydratePromise = (async () => {
      const db = await defaultSessionDB.ready();
      const rows = await _getAllRows(db);
      const seen = new Map(); // key -> db row
      for (const row of rows) {
        seen.set(row.key, row);
        if (!_mem.has(row.key)) _mem.set(row.key, row);
      }
      _hydrated = true;
      // Reconcile anything written to the Map before hydration completed with
      // what the DB actually holds — the newest row wins in both directions.
      for (const [key, row] of _mem) {
        const dbRow = seen.get(key);
        if (!dbRow) {
          _putRow(row);
        } else if (Number(dbRow.updated_at) > Number(row.updated_at)) {
          _mem.set(key, dbRow);
        } else {
          _putRow(row);
        }
      }
      // Copy registered legacy blobs into rows, then drop the legacy keys.
      for (const [prefix, legacyKey] of _legacy) {
        await this.migrateLegacyMap(legacyKey, prefix);
      }
      for (const [cacheKey, lv] of _legacyValues) {
        await this.migrateLegacyValue(lv.legacyKey, cacheKey, lv.parseJson);
      }
      // Tell boot-time sync readers (focus/manifests/seq) they can now re-read
      // real IndexedDB rows — their first read ran before hydration resolved.
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('webagent-kv-cache-hydrated', {
          detail: { owner_scope: defaultSessionDB.ownerScope },
        }));
      }
    })();
    return _hydratePromise;
  },

  /** @returns {Promise<void>} resolved once hydration is done (or skipped). */
  async ensureHydrated() {
    if (_hydrated) return;
    return this.hydrate();
  },

  /**
   * Synchronous read. Returns undefined for a missing or
   * not-yet-hydrated (and legacy-unbacked) key.
   * @param {string} key
   */
  get(key) {
    const row = _mem.get(key);
    if (_rowIsLive(row)) return row.value;
    if (!_hydrated) {
      const legacy = _legacyFor(key);
      if (legacy) {
        const obj = _readLegacy(legacy.legacyKey);
        if (obj && Object.prototype.hasOwnProperty.call(obj, legacy.suffix)) {
          return obj[legacy.suffix];
        }
      }
      const lv = _legacyValues.get(key);
      if (lv) {
        try {
          const raw = localStorage.getItem(lv.legacyKey);
          if (raw != null) {
            if (lv.parseJson) {
              try { return JSON.parse(raw); } catch (_) { return raw; }
            }
            return raw;
          }
        } catch (_) { /* inaccessible storage — fall through */ }
      }
    }
    return undefined;
  },

  /**
   * Write one entity row. Updates memory synchronously, flushes async.
   * @param {string} key
   * @param {*} value — structured-clone-able
   * @param {Object} [opts]
   * @param {number} [opts.ttlMs] — ignored legacy option; rows do not expire
   * @returns {Promise<void>} resolves when the queued flush is done
   */
  set(key, value, _opts = {}) {
    const now = _now();
    const row = {
      key,
      value,
      expires_at: 0,
      size: valueByteSize(value),
      updated_at: now,
    };
    _mem.set(key, row);
    return _putRow(row);
  },

  /**
   * Delete one entity row (no-op if absent).
   * @returns {Promise<void>}
   */
  del(key) {
    _mem.delete(key);
    return _delRow(key);
  },

  /**
   * Persist a map-shaped value as one row per entry under `prefix`.
   * e.g. setAll('chat:lastSeq:', { [sessionId]: 42 }) →
   *      row 'chat:lastSeq:<sessionId>' = 42.
   * @param {string} prefix
   * @param {Object} obj
   * @param {Object} [opts]
   */
  setAll(prefix, obj, opts = {}) {
    const entries = obj && typeof obj === 'object' ? Object.entries(obj) : [];
    for (const [key, value] of entries) this.set(prefix + key, value, opts);
    return _flushChain;
  },

  /**
   * Synchronously read every live row under `prefix` as { suffixKey: value }.
   * Before hydration, reads the registered legacy blob for the prefix.
   * @param {string} prefix
   * @returns {Object}
   */
  getAll(prefix) {
    const out = {};
    // In-memory rows always win — they are the freshest (including writes made
    // while persistence was unavailable, e.g. memory_only mode).
    for (const [key, row] of _mem) {
      if (key.startsWith(prefix) && _rowIsLive(row)) {
        out[key.slice(prefix.length)] = row.value;
      }
    }
    if (_hydrated) return out;
    // Not hydrated yet: fold in the registered legacy blob for boot-time reads.
    const legacyKey = _legacy.get(prefix);
    const obj = legacyKey ? _readLegacy(legacyKey) : null;
    if (obj) {
      for (const [k, v] of Object.entries(obj)) {
        if (!(k in out)) out[k] = v;
      }
    }
    return out;
  },

  /** @param {string} prefix — deletes every row under it. */
  delAll(prefix) {
    for (const key of Array.from(_mem.keys())) {
      if (key.startsWith(prefix)) this.del(key);
    }
    return _flushChain;
  },

  /**
   * One-time seed: copy a legacy localStorage map blob (e.g. 'sessionFocus.v1'
   * storing { sessionId: focus }) into per-entity rows under `prefix`, then
   * remove the legacy key. Only runs when persistence is actually available —
   * in memory_only/disabled mode the legacy key is left in place untouched.
   *
   * A surviving legacy blob is AUTHORITATIVE when present: chat-message-cache
   * writes a synchronous unload-mirror into it (pagehide/visibilitychange can
   * kill the page before an async IndexedDB flush completes), so any blob that
   * survives to the next boot is by construction newer than the rows in
   * IndexedDB and must overwrite them. When absent, nothing is copied.
   * @param {string} legacyKey
   * @param {string} prefix
   * @returns {Promise<void>}
   */
  async migrateLegacyMap(legacyKey, prefix) {
    if (!browserPersistenceAllowed() || !defaultSessionDB.ownerScope) return;
    try {
      const obj = _readLegacy(legacyKey);
      if (obj) this.setAll(prefix, obj);
      localStorage.removeItem(legacyKey);
    } catch (_) { /* blocked/corrupt storage — keep legacy key, retry later */ }
  },

  /**
   * One-time seed: copy a legacy scalar/whole-blob localStorage value (e.g.
   * 'webagent.pendingMessages.v1' holding the outbox array, or the raw-string
   * chat draft) into the single app_cache row `cacheKey`, then remove the
   * legacy key. Same contract as migrateLegacyMap: a surviving blob is
   * AUTHORITATIVE (it is the synchronous crash-durability mirror written after
   * the last async IndexedDB flush) and this only runs when persistence is
   * actually available.
   * @param {string} legacyKey
   * @param {string} cacheKey
   * @param {boolean} [parseJson=true] — false returns the raw string (drafts)
   * @returns {Promise<void>}
   */
  async migrateLegacyValue(legacyKey, cacheKey, parseJson = true) {
    if (!browserPersistenceAllowed() || !defaultSessionDB.ownerScope) return;
    try {
      const raw = localStorage.getItem(legacyKey);
      if (raw != null) {
        let value = raw;
        if (parseJson) {
          try { value = JSON.parse(raw); } catch (_) { value = raw; }
        }
        this.set(cacheKey, value);
      }
      localStorage.removeItem(legacyKey);
    } catch (_) { /* blocked/corrupt storage — keep legacy key, retry later */ }
  },

  /**
   * Drop all in-memory rows and close the hydrated state. Called on tenant
   * purge/logout so a different account can never read the previous tenant's
   * cache. Next hydrate() re-reads the (now empty) tenant database.
   */
  reset() {
    _epoch += 1; // invalidate every flush queued before the purge/reset
    _purged = true; // hard-off until the next hydrate() — see declaration note
    _mem.clear();
    _hydrated = false;
    _hydratePromise = null;
    _flushChain = Promise.resolve();
  },
};

// Reset on tenant lifecycle events: purge (logout / policy-transition) and
// policy mode changes to non-persistent or to a different owner scope.
if (typeof window !== 'undefined') {
  window.addEventListener('webagent-browser-storage-purge', () => kvCache.reset());
  window.addEventListener('webagent-browser-storage-policy', (ev) => {
    const detail = ev.detail || {};
    const scope = String(detail.owner_scope || '');
    if (detail.mode !== 'persistent_cache' || (scope && scope !== _lastPolicyScope)) {
      kvCache.reset();
    }
    if (scope) _lastPolicyScope = scope;
  });
}

export default kvCache;

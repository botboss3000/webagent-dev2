'use strict';

import {
  browserPersistenceAllowed,
  assertBrowserCapacity,
  getBrowserStorageMode,
  MEMORY_ONLY,
} from './browser-storage-policy.js';

const DB_NAME_PREFIX = 'webagent_attachments';
const LEGACY_DB_NAME = 'webagent-attachments';
const DB_VERSION = 2;
const STORE = 'attachments';

let _ownerScope = '';
let _dbPromise = null;
const _memory = new Map();
const _urlCache = new Map();

function _dbName() {
  if (!_ownerScope) throw new Error('Attachment owner scope is not initialized');
  return `${DB_NAME_PREFIX}_${_ownerScope}`;
}

export function setAttachmentOwnerScope(scope) {
  const normalized = String(scope || '').replace(/[^A-Za-z0-9_-]/g, '');
  if (!normalized) throw new Error('Missing attachment owner scope');
  if (_ownerScope && _ownerScope !== normalized) closeAttachmentStorage();
  _ownerScope = normalized;
}

export function closeAttachmentStorage() {
  if (_dbPromise) {
    _dbPromise.then(db => db.close()).catch(() => {});
    _dbPromise = null;
  }
}

function _openDb() {
  if (!browserPersistenceAllowed()) {
    throw new Error(`Attachment persistence is unavailable in ${getBrowserStorageMode()} mode`);
  }
  if (_dbPromise) return _dbPromise;
  _dbPromise = new Promise((resolve, reject) => {
    if (!('indexedDB' in window)) {
      reject(new Error('IndexedDB is not available in this browser.'));
      return;
    }
    const req = indexedDB.open(_dbName(), DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(STORE)) {
        const store = db.createObjectStore(STORE, { keyPath: 'id' });
        store.createIndex('by_created', 'created', { unique: false });
      }
    };
    req.onsuccess = () => {
      req.result.onversionchange = () => {
        req.result.close();
        _dbPromise = null;
      };
      resolve(req.result);
    };
    req.onerror = () => reject(req.error || new Error('IndexedDB open failed'));
    req.onblocked = () => reject(new Error('Attachment database open was blocked'));
  });
  return _dbPromise;
}

function _tx(mode) {
  return _openDb().then(db => {
    const tx = db.transaction(STORE, mode);
    return { tx, store: tx.objectStore(STORE) };
  });
}

function _normalizeAttachment({ id, blob, mime_type, name, size }) {
  if (!id || !(blob instanceof Blob)) throw new Error('Attachment id and Blob are required');
  return {
    id: String(id),
    blob,
    mime_type: String(mime_type || blob.type || 'application/octet-stream'),
    name: String(name || ''),
    size: Number.isFinite(Number(size)) ? Number(size) : blob.size,
    created: Date.now(),
    owner_scope: _ownerScope,
  };
}

export async function putAttachment(value) {
  const record = _normalizeAttachment(value);
  if (getBrowserStorageMode() === MEMORY_ONLY) {
    _memory.set(record.id, record);
    return true;
  }
  await assertBrowserCapacity(record.blob.size);
  const { tx, store } = await _tx('readwrite');
  await new Promise((resolve, reject) => {
    const req = store.put(record);
    req.onsuccess = resolve;
    req.onerror = () => reject(req.error || new Error('IndexedDB put failed'));
    tx.onabort = () => reject(tx.error || new Error('Attachment transaction aborted'));
  });
  return true;
}

async function getAttachment(id) {
  if (getBrowserStorageMode() === MEMORY_ONLY) return _memory.get(id) || null;
  const { store } = await _tx('readonly');
  return new Promise((resolve, reject) => {
    const req = store.get(id);
    req.onsuccess = () => resolve(req.result || null);
    req.onerror = () => reject(req.error || new Error('IndexedDB get failed'));
  });
}

export async function deleteAttachment(id) {
  const cached = _urlCache.get(id);
  if (cached) {
    try { URL.revokeObjectURL(cached); } catch (_) {}
    _urlCache.delete(id);
  }
  if (getBrowserStorageMode() === MEMORY_ONLY) return _memory.delete(id);
  const { store } = await _tx('readwrite');
  return new Promise((resolve, reject) => {
    const req = store.delete(id);
    req.onsuccess = () => resolve(true);
    req.onerror = () => reject(req.error || new Error('IndexedDB delete failed'));
  });
}

export async function getObjectUrl(id) {
  const cached = _urlCache.get(id);
  if (cached) return cached;
  const rec = await getAttachment(id);
  if (!rec?.blob) return null;
  const url = URL.createObjectURL(rec.blob);
  _urlCache.set(id, url);
  return url;
}

export function revokeAllObjectUrls() {
  for (const url of _urlCache.values()) {
    try { URL.revokeObjectURL(url); } catch (_) {}
  }
  _urlCache.clear();
}

function _deleteDatabase(name, timeoutMs = 3000) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) {
        settled = true;
        reject(new Error(`Timed out deleting ${name}; another tab may be blocking it`));
      }
    }, timeoutMs);
    const req = indexedDB.deleteDatabase(name);
    req.onsuccess = () => {
      if (!settled) { settled = true; clearTimeout(timer); resolve(); }
    };
    req.onerror = () => {
      if (!settled) { settled = true; clearTimeout(timer); reject(req.error || new Error(`Could not delete ${name}`)); }
    };
    req.onblocked = () => {
      if (!settled) { settled = true; clearTimeout(timer); reject(new Error(`Deletion blocked for ${name}`)); }
    };
  });
}

export async function clearAttachmentStorage({ deleteDatabase = false, includeLegacy = false } = {}) {
  revokeAllObjectUrls();
  _memory.clear();
  const completed = [];
  const failures = [];
  const names = [_dbName()];
  if (includeLegacy) names.push(LEGACY_DB_NAME);
  closeAttachmentStorage();
  for (const name of names) {
    try {
      if (deleteDatabase) {
        await _deleteDatabase(name);
      } else if (name === _dbName()) {
        const { store } = await _tx('readwrite');
        await new Promise((resolve, reject) => {
          const req = store.clear();
          req.onsuccess = resolve;
          req.onerror = () => reject(req.error);
        });
      }
      completed.push(name);
    } catch (error) {
      failures.push({ target: name, error: String(error?.message || error) });
    }
  }
  return { complete: failures.length === 0, completed, failures };
}

async function _blobEnvelope(blob) {
  const bytes = new Uint8Array(await blob.arrayBuffer());
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  }
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return {
    __webagent_blob__: true,
    type: blob.type || 'application/octet-stream',
    size: blob.size,
    sha256: Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join(''),
    base64: btoa(binary),
  };
}

export async function exportAttachmentStorage() {
  if (!browserPersistenceAllowed()) {
    const rows = [];
    for (const row of _memory.values()) {
      rows.push({ ...row, blob: await _blobEnvelope(row.blob) });
    }
    return {
      database: {
        name: _dbName(),
        version: DB_VERSION,
        stores: { [STORE]: rows },
        persistence: 'memory_only',
      },
    };
  }
  const db = await _openDb();
  const rows = await new Promise((resolve, reject) => {
    const tx = db.transaction(STORE, 'readonly');
    const req = tx.objectStore(STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
    tx.onabort = () => reject(tx.error || new Error('Attachment export transaction aborted'));
  });
  const encoded = [];
  for (const row of rows) encoded.push({ ...row, blob: await _blobEnvelope(row.blob) });
  return {
    database: {
      name: db.name,
      version: db.version,
      stores: { [STORE]: encoded },
    },
  };
}

export async function getStats() {
  if (getBrowserStorageMode() === MEMORY_ONLY) {
    return {
      count: _memory.size,
      bytes: [..._memory.values()].reduce((n, row) => n + Number(row.blob?.size || 0), 0),
    };
  }
  const { store } = await _tx('readonly');
  const rows = await new Promise((resolve, reject) => {
    const req = store.getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
  });
  return {
    count: rows.length,
    bytes: rows.reduce((n, row) => n + Number(row.blob?.size || row.size || 0), 0),
  };
}

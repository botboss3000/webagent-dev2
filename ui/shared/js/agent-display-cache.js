'use strict';

// Agent DISPLAY-fields cache (name/icon/engine) with batched hydration.
//
// Session lists (chat-header dropdown, Sessions page, launcher widget) render
// instantly from this cache — session payloads always carry agent_id, and any
// agent whose display fields are missing is a "phantom cell" until this module
// fills it in with ONE batched request to GET /api/v1/agents/display (which is
// itself served from a server-side TTL cache, so the agent plane is touched at
// most once per agent per minute regardless of how many consumers ask).
//
// Seeding: the full-roster /agents response already carries name/icon/engine,
// so callers that load it seed this cache for free (see session-agent.js).
// Persistence: entries are kept in localStorage so a cold app start still
// paints last-known values; stale entries are re-fetched on the next hydrate.

import { apiPath } from './config.js';

const _cache = new Map();        // agent_id -> {name, icon, engine, updated_at, ts}
const _pending = new Map();      // batch-key -> Promise (dedupe concurrent hydrates)
const _FRESH_MS = 5 * 60 * 1000; // how long a client entry is trusted before re-fetch
const _STORAGE_KEY = 'agentDisplayCache';
let _loaded = false;
let _accessDenied = false;

function _loadPersisted() {
  if (_loaded) return;
  _loaded = true;
  try {
    const raw = localStorage.getItem(_STORAGE_KEY);
    if (!raw) return;
    const obj = JSON.parse(raw);
    for (const [id, rec] of Object.entries(obj)) {
      if (id && rec && typeof rec === 'object') {
        // Persisted entries are usable but never "fresh" — hydrate re-fetches
        // them on the next batch so they can't go stale forever.
        _cache.set(id, {
          name: rec.name || '', icon: rec.icon || '', engine: rec.engine || '',
          updated_at: rec.updated_at || '', ts: 0,
        });
      }
    }
  } catch (_) { /* corrupt storage — ignore */ }
}

function _persist() {
  try {
    const obj = {};
    for (const [id, rec] of _cache.entries()) obj[id] = {
      name: rec.name, icon: rec.icon, engine: rec.engine, updated_at: rec.updated_at,
    };
    localStorage.setItem(_STORAGE_KEY, JSON.stringify(obj));
  } catch (_) { /* storage may be full/blocked */ }
}

function _isFresh(agentId) {
  const e = _cache.get(agentId);
  return !!e && (Date.now() - e.ts) < _FRESH_MS;
}

/**
 * Record raw roster entries ({id, name, icon, engine, updated_at}) into the
 * cache — typically the /agents list the app already loads elsewhere.
 */
export function seedAgentDisplay(agents) {
  if (!Array.isArray(agents) || !agents.length) return;
  let changed = false;
  for (const a of agents) {
    if (!a || !a.id) continue;
    _cache.set(a.id, {
      name: a.name || '', icon: a.icon || '', engine: a.engine || '',
      updated_at: a.updated_at || '', ts: Date.now(),
    });
    changed = true;
  }
  if (changed) _persist();
}

/**
 * Record /agents/display response records ({agent_id, name, icon, engine}).
 * Internal-ish, exported for tests / other consumers with the same shape.
 */
export function seedAgentDisplayMap(records) {
  if (!Array.isArray(records) || !records.length) return;
  let changed = false;
  for (const rec of records) {
    if (!rec || !rec.agent_id) continue;
    _cache.set(rec.agent_id, {
      name: rec.name || '', icon: rec.icon || '', engine: rec.engine || '',
      updated_at: rec.updated_at || '', ts: Date.now(),
    });
    changed = true;
  }
  if (changed) _persist();
}

/**
 * Best-known display record for an agent, or null. Returns stale-but-usable
 * entries too — better to paint a slightly-old name than a blank cell.
 */
export function getAgentDisplay(agentId) {
  if (!agentId) return null;
  _loadPersisted();
  const e = _cache.get(agentId);
  if (!e) return null;
  return { name: e.name, icon: e.icon, engine: e.engine, updated_at: e.updated_at };
}

export function hasAgentDisplay(agentId) {
  _loadPersisted();
  return _cache.has(agentId);
}

/**
 * Ensure display fields exist for the given agent ids, fetching only the
 * missing/stale ones in a single batched request. Resolves to the array of
 * records that landed (possibly empty). Concurrent callers share one in-flight
 * request per batch; failures resolve to [] so callers can repaint on success
 * and leave phantom cells as-is otherwise.
 */
export async function hydrateAgentDisplay(agentIds, userId) {
  _loadPersisted();
  if (_accessDenied || !userId || !Array.isArray(agentIds) || !agentIds.length) return [];
  const missing = [];
  const seen = new Set();
  for (const id of agentIds) {
    if (!id || seen.has(id)) continue;
    seen.add(id);
    if (!_isFresh(id)) missing.push(id);
  }
  if (!missing.length) return [];
  const batchKey = [...missing].sort().join('|');
  if (_pending.has(batchKey)) return _pending.get(batchKey);
  const p = (async () => {
    try {
      const token = localStorage.getItem('auth_token');
      let url = apiPath(`/api/v1/agents/display?user_id=${encodeURIComponent(userId)}&ids=${encodeURIComponent(missing.join(','))}`);
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(url);
      if (res.status === 401 || res.status === 403) {
        // Auth transitions reload the document. Until then, every caller has
        // the same denied identity, so repeated hydration cannot succeed.
        _accessDenied = true;
        return [];
      }
      if (!res.ok) return [];
      const data = await res.json();
      const records = Array.isArray(data.agents) ? data.agents : [];
      seedAgentDisplayMap(records);
      return records;
    } catch (_) {
      return []; // non-fatal: callers keep phantom cells
    } finally {
      _pending.delete(batchKey);
    }
  })();
  _pending.set(batchKey, p);
  return p;
}

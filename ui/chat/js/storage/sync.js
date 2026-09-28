'use strict';

/**
 * Revisioned browser sync.
 *
 * Every session is an independent compare-and-swap mutation. The server
 * derives the destination user from the JWT, records idempotency receipts,
 * and returns one result per session. Only the exact local revision that was
 * acknowledged is marked clean.
 */

import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { randomUUID } from '../../../shared/js/uuid.js';
import defaultSessionDB from './indexeddb.js';

let _syncTimer = null;
let _syncInFlight = false;
const _SYNC_INTERVAL_MS = 15000;
const _SYNC_BATCH_SIZE = 10;

function _sessionPayload(session) {
  return {
    id: session.id,
    agent_id: session.agent_id || '',
    title: session.title || '',
    metadata: session.metadata || {},
    participants: session.participants || [],
    sort_order: session.sort_order ?? null,
    status: session.status || 'active',
    created_at: session.created_at || null,
    updated_at: session.updated_at || null,
  };
}

async function _upsertMutation(session) {
  const mutationId = session._mutation_id || randomUUID();
  if (!session._mutation_id) {
    await defaultSessionDB.updateSession(session.id, { _mutation_id: mutationId });
  }
  return {
    mutation_id: mutationId,
    session_id: session.id,
    operation: 'upsert',
    base_server_revision: Number(session.server_revision || 0),
    client_revision: Number(session.local_revision || 0),
    session: _sessionPayload(session),
    interactions: await defaultSessionDB.getInteractions(session.id),
  };
}

async function _pendingMutations() {
  const sessions = await defaultSessionDB.listSessions();
  const mutations = [];
  for (const session of sessions.filter(row => row._dirty)) {
    mutations.push(await _upsertMutation(session));
  }
  mutations.push(...await defaultSessionDB.listSyncOutbox());
  return mutations;
}

async function _ackResult(result, submitted) {
  if (!['applied', 'noop'].includes(result.status)) {
    const current = await defaultSessionDB.getSession(result.session_id);
    if (current) {
      await defaultSessionDB.updateSession(result.session_id, {
        _dirty: true,
        _sync_error: result.error || result.status,
      });
    }
    return false;
  }

  if (submitted.operation === 'delete') {
    await defaultSessionDB.removeSyncOutbox(
      submitted.session_id, submitted.mutation_id,
    );
    return true;
  }

  const current = await defaultSessionDB.getSession(submitted.session_id);
  if (!current) return false;
  const unchanged =
    Number(current.local_revision || 0) === Number(submitted.client_revision || 0) &&
    current._mutation_id === submitted.mutation_id;
  const nextMutationId = unchanged
    ? null
    : (current._mutation_id === submitted.mutation_id
      ? randomUUID()
      : current._mutation_id);
  await defaultSessionDB.updateSession(submitted.session_id, {
    server_revision: Number(result.server_revision || 0),
    content_hash: result.content_hash || '',
    synced_local_revision: Number(submitted.client_revision || 0),
    _dirty: unchanged ? false : true,
    _mutation_id: nextMutationId,
    _sync_error: null,
  });
  return unchanged;
}

async function _postBatch(path, batch) {
  const resp = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ mutations: batch }),
  });
  if (!resp.ok) {
    const body = await resp.text().catch(() => '');
    throw new Error(`Server error (${resp.status}): ${body.substring(0, 200)}`);
  }
  return resp.json();
}

async function _flushTo(path, userId) {
  if (_syncInFlight) {
    return { ok: false, count: 0, errors: ['Sync already in flight'], results: [] };
  }
  if (!userId) return { ok: false, count: 0, errors: ['No authenticated user'], results: [] };
  _syncInFlight = true;
  let count = 0;
  const errors = [];
  const allResults = [];
  try {
    const mutations = await _pendingMutations();
    for (let i = 0; i < mutations.length; i += _SYNC_BATCH_SIZE) {
      const batch = mutations.slice(i, i + _SYNC_BATCH_SIZE);
      try {
        const data = await _postBatch(path, batch);
        const byMutation = new Map(batch.map(row => [row.mutation_id, row]));
        const received = new Set();
        for (const result of data.results || []) {
          received.add(result.mutation_id);
          allResults.push(result);
          const submitted = byMutation.get(result.mutation_id);
          if (submitted && await _ackResult(result, submitted)) count += 1;
          if (!['applied', 'noop'].includes(result.status)) {
            errors.push(`${result.session_id}: ${result.error || result.status}`);
          }
        }
        for (const submitted of batch) {
          if (!received.has(submitted.mutation_id)) {
            errors.push(`${submitted.session_id}: missing sync result`);
          }
        }
      } catch (error) {
        errors.push(error.message);
      }
    }
  } finally {
    _syncInFlight = false;
  }
  return { ok: errors.length === 0, count, errors, results: allResults };
}

export const syncEngine = {
  async markDirty(sessionId) {
    const session = await defaultSessionDB.getSession(sessionId);
    if (!session) return false;
    await defaultSessionDB.updateSession(sessionId, {
      _dirty: true,
      _mutation_id: randomUUID(),
    });
    return true;
  },

  async markClean(sessionId) {
    return defaultSessionDB.updateSession(sessionId, {
      _dirty: false,
      _mutation_id: null,
      _sync_error: null,
    });
  },

  async getDirtySessions() {
    const all = await defaultSessionDB.listSessions();
    return all.filter(row => row._dirty).map(row => row.id);
  },

  async flush(userId) {
    return _flushTo('/api/v1/browser/sync', userId);
  },

  async promoteAll(userId) {
    const sessions = await defaultSessionDB.listSessions();
    for (const session of sessions) await this.markDirty(session.id);
    return _flushTo('/api/v1/browser/promote', userId);
  },

  startAutoSync(userId) {
    this.stopAutoSync();
    if (!userId) return;
    _syncTimer = setInterval(() => {
      this.flush(userId).catch(error => {
        console.warn('[SyncEngine] Auto-sync error:', error);
      });
    }, _SYNC_INTERVAL_MS);
  },

  stopAutoSync() {
    if (_syncTimer) clearInterval(_syncTimer);
    _syncTimer = null;
  },

  async flushNow(userId) {
    return this.flush(userId);
  },
};

export default syncEngine;

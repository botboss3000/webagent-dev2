'use strict';

/**
 * Personal mobile Agents-list preferences.
 *
 * The browser cache renders synchronously; the server snapshot is authoritative
 * across devices. Favorites never alter a shared agent record, its desktop
 * presentation, or another user's view.
 */

import { app } from '../../../../shared/js/state.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { kvRead, kvWrite } from '../../../../shared/js/kv-ui-state.js';

const CACHE_PREFIX_V2 = 'agents:mobile-favorites:v2:';
const MIRROR_PREFIX_V2 = 'agents_mobile_favorites_v2:';
const LEGACY_CACHE_PREFIX = 'agents:mobile-favorites:';
const LEGACY_MIRROR_PREFIX = 'agents_mobile_favorites_v1:';
const MAX_FAVORITES = 200;
const SYNC_DEBOUNCE_MS = 180;

let _syncTimer = null;
let _syncing = false;
let _pendingSync = null;
let _localGeneration = 0;

function currentUserId() {
  if (app.currentUserId) return String(app.currentUserId);
  try { return localStorage.getItem('auth_user_id') || 'anonymous'; } catch (_) { return 'anonymous'; }
}

function keys() {
  const userId = currentUserId();
  return {
    cache: CACHE_PREFIX_V2 + userId,
    mirror: MIRROR_PREFIX_V2 + userId,
    legacyCache: LEGACY_CACHE_PREFIX + userId,
    legacyMirror: LEGACY_MIRROR_PREFIX + userId,
  };
}

function normalizeFavoriteAgentIds(value) {
  const seen = new Set();
  return (Array.isArray(value) ? value : []).filter((id) => {
    if (typeof id !== 'string' || !id || id.length > 200 || seen.has(id)) return false;
    seen.add(id);
    return true;
  }).slice(0, MAX_FAVORITES);
}

function normalizeSnapshot(value, fallback = null) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return fallback;
  const revision = Number.isInteger(value.revision) && value.revision >= 0 ? value.revision : 0;
  return {
    agent_ids: normalizeFavoriteAgentIds(value.agent_ids),
    revision,
    configured: value.configured === true,
  };
}

function readSnapshot() {
  const { cache, mirror, legacyCache, legacyMirror } = keys();
  const cached = normalizeSnapshot(kvRead(cache, mirror));
  if (cached) return cached;
  return {
    agent_ids: normalizeFavoriteAgentIds(kvRead(legacyCache, legacyMirror)),
    revision: 0,
    configured: false,
  };
}

function writeSnapshot(snapshot) {
  const { cache, mirror } = keys();
  kvWrite(cache, mirror, normalizeSnapshot(snapshot, {
    agent_ids: [], revision: 0, configured: false,
  }));
}

function scheduleSync(userId, generation) {
  if (userId === 'anonymous') return;
  _pendingSync = { userId, generation };
  if (_syncTimer || _syncing) return;
  _syncTimer = setTimeout(() => {
    _syncTimer = null;
    flushSync();
  }, SYNC_DEBOUNCE_MS);
}

function rescheduleIfNeeded() {
  if (!_pendingSync || _syncTimer || _syncing) return;
  _syncTimer = setTimeout(() => {
    _syncTimer = null;
    flushSync();
  }, SYNC_DEBOUNCE_MS);
}

async function flushSync() {
  if (_syncing || !_pendingSync) return;
  const pending = _pendingSync;
  _pendingSync = null;
  if (pending.userId !== currentUserId()) return;

  _syncing = true;
  const snapshot = readSnapshot();
  try {
    const res = await fetch(
      `/api/v1/user/mobile-agent-favorites?user_id=${encodeURIComponent(pending.userId)}`,
      {
        method: 'PUT',
        headers: { ...authHeaders(), 'Content-Type': 'application/json' },
        body: JSON.stringify({
          agent_ids: snapshot.agent_ids,
          expected_revision: snapshot.revision,
        }),
      },
    );
    const body = await res.json().catch(() => null);
    if (res.ok && body) {
      if (pending.generation === _localGeneration) {
        applyServerMobileAgentFavorites(body);
      } else {
        // Preserve newer local clicks, but advance their expected revision before
        // the queued write so they are not silently overwritten by this response.
        const current = readSnapshot();
        writeSnapshot({ ...current, revision: body.revision, configured: true });
        scheduleSync(pending.userId, _localGeneration);
      }
    } else if (res.status === 409 && body?.current) {
      if (pending.generation === _localGeneration) {
        // This preference is intentionally low-risk: adopt the authoritative
        // cross-device value rather than forcing an offline mutation queue.
        applyServerMobileAgentFavorites(body.current);
      } else {
        const current = readSnapshot();
        writeSnapshot({ ...current, revision: body.current.revision, configured: true });
        scheduleSync(pending.userId, _localGeneration);
      }
    }
  } catch (_) {
    // Keep the IndexedDB value for instant local use. The next profile load or
    // favorite toggle will reconcile it; a durable offline mutation queue is
    // deliberately unnecessary for this low-risk preference.
  } finally {
    _syncing = false;
    rescheduleIfNeeded();
  }
}

/** Return this user's saved favorite agent IDs in stable preference order. */
export function readMobileAgentFavorites() {
  return readSnapshot().agent_ids;
}

/** Apply an authoritative profile or PUT-response snapshot to the local cache. */
export function applyServerMobileAgentFavorites(snapshot) {
  const normalized = normalizeSnapshot(snapshot);
  if (!normalized || !normalized.configured) return readMobileAgentFavorites();
  writeSnapshot(normalized);
  return normalized.agent_ids;
}

/** Reconcile a profile response; migrates a non-empty legacy browser preference once. */
export function reconcileMobileAgentFavorites(snapshot) {
  const normalized = normalizeSnapshot(snapshot);
  if (normalized?.configured) return applyServerMobileAgentFavorites(normalized);

  const current = readSnapshot();
  // A v2 configured snapshot includes an intentional empty list. Preserve it
  // too, otherwise clearing the last favorite before its first write could let
  // another device's legacy list reappear later.
  if (current.configured || current.agent_ids.length) {
    _localGeneration += 1;
    writeSnapshot({ ...current, configured: true, revision: 0 });
    scheduleSync(currentUserId(), _localGeneration);
  }
  return current.agent_ids;
}

/** Toggle a personal mobile-sidebar favorite without changing shared agent data. */
export function toggleMobileAgentFavorite(agentId) {
  if (typeof agentId !== 'string' || !agentId) return readMobileAgentFavorites();
  const current = readSnapshot();
  const next = current.agent_ids.includes(agentId)
    ? current.agent_ids.filter((id) => id !== agentId)
    : [...current.agent_ids, agentId];
  _localGeneration += 1;
  writeSnapshot({ ...current, agent_ids: next, configured: true });
  scheduleSync(currentUserId(), _localGeneration);
  return next;
}

export function isMobileAgentFavorite(agentId, ids = readMobileAgentFavorites()) {
  return ids.includes(agentId);
}

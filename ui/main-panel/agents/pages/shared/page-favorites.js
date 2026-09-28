'use strict';

/**
 * Personal, global Agent-page header preferences.
 *
 * The server-side user preference is authoritative across devices. kv-ui-state is
 * retained only for fast startup, offline fallback, and one-time migration of
 * pre-existing browser-only favorites.
 */

import { app } from '../../../../shared/js/state.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { kvRead, kvWrite } from '../../../../shared/js/kv-ui-state.js';
import agentPagesCatalog from '../catalog.json' with { type: 'json' };

const CACHE_PREFIX = 'agents:page-favorites:';
const MIRROR_PREFIX = 'agents_page_favorites_v1:';
const REQUIRED_PAGE_ID = 'config';
const VALID_PAGE_IDS = new Set((agentPagesCatalog.pages || []).map((page) => page.id));

let _favorites = null;
let _revision = 0;
let _hydration = null;

function currentUserId() {
  if (app.currentUserId) return String(app.currentUserId);
  try { return localStorage.getItem('auth_user_id') || ''; } catch (_) { return ''; }
}

function keys() {
  const userId = currentUserId() || 'anonymous';
  return {
    cache: CACHE_PREFIX + userId,
    mirror: MIRROR_PREFIX + userId,
  };
}

function normalizeFavoritePageIds(value) {
  const seen = new Set();
  const ids = Array.isArray(value) ? value : [];
  const valid = ids.filter((id) => {
    if (typeof id !== 'string' || !VALID_PAGE_IDS.has(id) || seen.has(id)) return false;
    seen.add(id);
    return true;
  });
  // Config is required, but never pinned: preserve its selected position and
  // append it only when a legacy/malformed preference omitted it.
  return valid.includes(REQUIRED_PAGE_ID) ? valid : [...valid, REQUIRED_PAGE_ID];
}

function localFavorites() {
  const { cache, mirror } = keys();
  return normalizeFavoritePageIds(kvRead(cache, mirror));
}

function cacheFavorites(ids) {
  const normalized = normalizeFavoritePageIds(ids);
  const { cache, mirror } = keys();
  kvWrite(cache, mirror, normalized);
  return normalized;
}

function sameIds(left, right) {
  return left.length === right.length && left.every((id, index) => id === right[index]);
}

function applySnapshot(snapshot, { notify = false } = {}) {
  const next = normalizeFavoritePageIds(snapshot?.page_ids);
  const changed = !_favorites || !sameIds(_favorites, next);
  _favorites = cacheFavorites(next);
  _revision = Math.max(0, Number(snapshot?.revision) || 0);
  if (notify && changed) {
    window.dispatchEvent(new CustomEvent('agent-page-favorites-hydrated', {
      detail: { page_ids: _favorites, revision: _revision },
    }));
  }
  return _favorites;
}

function endpoint() {
  const userId = currentUserId();
  return userId ? `/api/v1/user/agent-page-favorites?user_id=${encodeURIComponent(userId)}` : '';
}

async function persist(ids) {
  const url = endpoint();
  if (!url) return;
  const expectedRevision = _revision;
  try {
    const response = await fetch(url, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ page_ids: ids, expected_revision: expectedRevision }),
    });
    if (response.status === 409) {
      const conflict = await response.json();
      applySnapshot(conflict.current, { notify: true });
      return;
    }
    if (!response.ok) return;
    const snapshot = await response.json();
    // Do not dispatch for the caller's own optimistic write: it would remount
    // Config just after a star click. Only update the revision/cache.
    applySnapshot(snapshot, { notify: false });
  } catch (_) {
    // The local mirror remains an offline fallback; it will seed or be replaced
    // by the server snapshot during the next successful hydration.
  }
}

/** Hydrate this user's global page order from the authoritative server record. */
export function hydrateAgentPageFavorites() {
  if (_hydration) return _hydration;
  const url = endpoint();
  if (!url) return Promise.resolve(readAgentPageFavorites());
  _hydration = (async () => {
    const local = readAgentPageFavorites();
    try {
      const response = await fetch(url, { headers: authHeaders() });
      if (!response.ok) return local;
      const snapshot = await response.json();
      if (snapshot?.configured) return applySnapshot(snapshot, { notify: true });

      // One-time migration: only seed a missing server preference. Once a server
      // record exists, it always wins over stale browser mirrors on every device.
      _revision = Math.max(0, Number(snapshot?.revision) || 0);
      await persist(local);
      return _favorites || local;
    } catch (_) {
      return local;
    }
  })().finally(() => { _hydration = null; });
  return _hydration;
}

/** Return the best immediately available page order; server hydration follows asynchronously. */
export function readAgentPageFavorites() {
  if (_favorites) return _favorites.slice();
  _favorites = localFavorites();
  return _favorites.slice();
}

/** Optimistically persist a caller-provided ordered list to the per-user server profile. */
export function writeAgentPageFavorites(ids) {
  const normalized = cacheFavorites(ids);
  _favorites = normalized;
  void persist(normalized);
  return normalized.slice();
}

/** Toggle one page. New favorites prepend left; Config is required but not pinned. */
export function toggleAgentPageFavorite(pageId) {
  const current = readAgentPageFavorites();
  if (pageId === REQUIRED_PAGE_ID || !VALID_PAGE_IDS.has(pageId)) return current;
  const next = current.includes(pageId)
    ? current.filter((id) => id !== pageId)
    : [pageId, ...current];
  return writeAgentPageFavorites(next);
}

export function isAgentPageFavorite(pageId, ids = readAgentPageFavorites()) {
  return ids.includes(pageId);
}

'use strict';

/**
 * Personal, global My Agents-page header preferences.
 *
 * Mirrors ui/main-panel/agents/pages/shared/page-favorites.js, but scoped to the
 * My Agents (All Agents) card. Its page ids differ from the per-agent catalog
 * (notably `models`), so it keeps its own server record, cache keys and change
 * event instead of sharing the per-agent preference.
 *
 * The server-side user preference is authoritative across devices. kv-ui-state is
 * retained only for fast startup, offline fallback, and one-time migration of
 * pre-existing browser-only favorites.
 */

import { app } from '../../../../shared/js/state.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { kvRead, kvWrite } from '../../../../shared/js/kv-ui-state.js';
import myPagesCatalog from '../catalog.json' with { type: 'json' };

const CACHE_PREFIX = 'agents:my-page-favorites:';
const MIRROR_PREFIX = 'agents_my_page_favorites_v1:';
const REQUIRED_PAGE_ID = 'config';
const HYDRATED_EVENT = 'my-agents-page-favorites-hydrated';
const VALID_PAGE_IDS = new Set((myPagesCatalog.pages || []).map((page) => page.id));

// Unlike the per-agent catalog (which defaults to Config only), every My Agents
// page starts favorited: existing users keep Sessions, Automations, Available
// models and Abilities exactly as before and simply gain the Config tab.
const DEFAULT_PAGE_IDS = [...(myPagesCatalog.pages || [])]
  .sort((left, right) => (left.order || 0) - (right.order || 0))
  .map((page) => page.id);

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
  const stored = kvRead(cache, mirror);
  // Nothing persisted yet -> every My Agents page is visible by default. An empty
  // array is a real (normalized) preference, so only a missing value uses defaults.
  if (!Array.isArray(stored)) return DEFAULT_PAGE_IDS.slice();
  return normalizeFavoritePageIds(stored);
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
    window.dispatchEvent(new CustomEvent(HYDRATED_EVENT, {
      detail: { page_ids: _favorites, revision: _revision },
    }));
  }
  return _favorites;
}

function endpoint() {
  const userId = currentUserId();
  return userId ? `/api/v1/user/my-agents-page-favorites?user_id=${encodeURIComponent(userId)}` : '';
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

/** Hydrate this user's My Agents page order from the authoritative server record. */
export function hydrateMyAgentsPageFavorites() {
  if (_hydration) return _hydration;
  const url = endpoint();
  if (!url) return Promise.resolve(readMyAgentsPageFavorites());
  _hydration = (async () => {
    const local = readMyAgentsPageFavorites();
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

/** Return the best immediately available order; server hydration follows asynchronously. */
export function readMyAgentsPageFavorites() {
  if (_favorites) return _favorites.slice();
  _favorites = localFavorites();
  return _favorites.slice();
}

/** Optimistically persist a caller-provided ordered list to the per-user server profile. */
export function writeMyAgentsPageFavorites(ids) {
  const normalized = cacheFavorites(ids);
  _favorites = normalized;
  void persist(normalized);
  return normalized.slice();
}

/** Toggle one page. New favorites prepend left; Config is required but not pinned. */
export function toggleMyAgentsPageFavorite(pageId) {
  const current = readMyAgentsPageFavorites();
  if (pageId === REQUIRED_PAGE_ID || !VALID_PAGE_IDS.has(pageId)) return current;
  const next = current.includes(pageId)
    ? current.filter((id) => id !== pageId)
    : [pageId, ...current];
  return writeMyAgentsPageFavorites(next);
}

export function isMyAgentsPageFavorite(pageId, ids = readMyAgentsPageFavorites()) {
  return ids.includes(pageId);
}

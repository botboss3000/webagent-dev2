'use strict';

// Single source of truth for server reachability and cached-reader mode.
// `online` means authority has been reconciled, not merely that navigator.onLine
// is true or /health answered once.

export const CONNECTIVITY = Object.freeze({
  ONLINE: 'online',
  CONNECTING: 'connecting',
  OFFLINE_CACHED: 'offline-cached',
  OFFLINE_NO_CACHE: 'offline-no-cache',
});

const VALID_STATES = new Set(Object.values(CONNECTIVITY));
let _state = CONNECTIVITY.CONNECTING;
let _detail = { source: 'boot', pending: [] };

export function hasCachedViewsForCurrentIdentity() {
  try {
    if (typeof window.__readPagesCache === 'function') {
      return Boolean(window.__readPagesCache());
    }
    const saved = JSON.parse(localStorage.getItem('pagesCatalogCache') || 'null');
    const identity = localStorage.getItem('auth_user_id') || '__anonymous__';
    return Boolean(saved && saved.identity === identity && saved.catalog
      && Array.isArray(saved.catalog.main));
  } catch (_) {
    return false;
  }
}

export function offlineConnectivityState() {
  return hasCachedViewsForCurrentIdentity()
    ? CONNECTIVITY.OFFLINE_CACHED
    : CONNECTIVITY.OFFLINE_NO_CACHE;
}

export function getConnectivityState() {
  return { state: _state, ..._detail };
}

export function setConnectivityState(state, detail = {}) {
  if (!VALID_STATES.has(state)) throw new Error(`Unknown connectivity state: ${state}`);
  const previous = _state;
  _state = state;
  _detail = { ...detail, pending: Array.isArray(detail.pending) ? detail.pending : [] };
  window.__webagentConnectivityState = state;
  if (document.body) document.body.dataset.connectivityState = state;
  window.dispatchEvent(new CustomEvent('webagent-connectivity-changed', {
    detail: { state, previous, ..._detail },
  }));
  return getConnectivityState();
}


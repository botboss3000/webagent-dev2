'use strict';

// App-wide cached-reader state. The health poll owns server reachability;
// navigator.onLine is only an immediate hint for a cold boot. In this mode the
// already-cached shell, sessions and transcripts remain navigable. Session and
// configuration mutations are inert; the composer stays live and writes to the
// tenant outbox for delivery after authoritative reconnection.

import {
  CONNECTIVITY,
  getConnectivityState,
  offlineConnectivityState,
  setConnectivityState,
} from './connectivity-state.js';

let _active = false;
let _observer = null;

const MUTATION_SELECTORS = [
  '#session-new-header-btn',
  '#session-dropdown-kebab',
  '#session-dropdown-delete',
  '.session-row-kebab',
  '.session-row-delete',
  '.session-row-action',
  '.session-row-visibility',
  '.session-manage-delete-selected',
];

function _ensureComposerNotice() {
  let notice = document.getElementById('offline-reader-composer-notice');
  const inputArea = document.getElementById('chat-input-area');
  if (!inputArea || notice) return notice;
  notice = document.createElement('div');
  notice.id = 'offline-reader-composer-notice';
  notice.setAttribute('role', 'status');
  notice.textContent = 'Offline · messages will be queued and sent after reconnect';
  inputArea.parentNode?.insertBefore(notice, inputArea);
  return notice;
}

function _applyDomState() {
  if (!document.body) return;
  document.body.dataset.offlineReadonly = _active ? 'true' : 'false';
  _ensureComposerNotice();

  // The chat aura is the app's single connection indicator: red for either
  // offline state, orange while connecting. Do not add a second header flag.
  const chatButton = document.getElementById('chat-notification-toggle-btn');
  if (chatButton) {
    const connection = getConnectivityState().state;
    const offline = connection === CONNECTIVITY.OFFLINE_CACHED
      || connection === CONNECTIVITY.OFFLINE_NO_CACHE;
    chatButton.classList.toggle('chat-aura-red', offline);
    if (connection === CONNECTIVITY.OFFLINE_CACHED) chatButton.title = 'Offline · cached data only';
    else if (connection === CONNECTIVITY.OFFLINE_NO_CACHE) chatButton.title = 'Offline · cached views unavailable';
    else if (chatButton.title === 'Offline · cached data only'
        || chatButton.title === 'Offline · cached views unavailable') {
      chatButton.title = 'Toggle chat panel (long-press for a new session)';
    }
  }

  const inputArea = document.getElementById('chat-input-area');
  // Cached mode still blocks session/configuration mutations, but the composer
  // remains interactive: chat-send persists messages to the tenant outbox and
  // flushes them only after authority has reconnected. Undo the old inert flag
  // as a migration guard for a live page upgraded from the previous module.
  if (inputArea?.dataset.offlineReaderInert === 'true' || inputArea?.inert) {
    delete inputArea.dataset.offlineReaderInert;
    inputArea.inert = false;
  }

  for (const el of document.querySelectorAll(MUTATION_SELECTORS.join(','))) {
    if (_active) {
      el.dataset.offlineReaderInert = 'true';
      el.inert = true;
      el.setAttribute('aria-disabled', 'true');
    } else if (el.dataset.offlineReaderInert === 'true') {
      delete el.dataset.offlineReaderInert;
      el.inert = false;
      el.removeAttribute('aria-disabled');
    }
  }
}

export function isOfflineReadOnly() {
  return _active;
}

export function setOfflineReadOnly(active, detail = {}) {
  const wanted = active === true;
  const current = getConnectivityState().state;
  const alreadyOffline = current === CONNECTIVITY.OFFLINE_CACHED
    || current === CONNECTIVITY.OFFLINE_NO_CACHE;
  if (wanted && !alreadyOffline) {
    setConnectivityState(offlineConnectivityState(), detail);
    return;
  }
  if (!wanted && current !== CONNECTIVITY.ONLINE) {
    setConnectivityState(CONNECTIVITY.ONLINE, detail);
    return;
  }
  _setOfflineReadOnly(wanted, detail);
}

function _setOfflineReadOnly(active, detail = {}) {
  const next = active === true;
  const changed = next !== _active;
  _active = next;
  window.__webagentOfflineReadOnly = next;
  _applyDomState();
  if (changed) {
    window.dispatchEvent(new CustomEvent('webagent-offline-readonly-changed', {
      detail: { active: next, ...detail },
    }));
  }
}

export function initOfflineReader() {
  window.addEventListener('webagent-connectivity-changed', (event) => {
    const state = event.detail?.state;
    _setOfflineReadOnly(state !== CONNECTIVITY.ONLINE, {
      source: event.detail?.source || 'connectivity', state,
    });
  });
  if (navigator.onLine === false) {
    setConnectivityState(offlineConnectivityState(), { source: 'navigator' });
  } else {
    _setOfflineReadOnly(true, { source: 'boot', state: CONNECTIVITY.CONNECTING });
  }
  window.addEventListener('offline', () => {
    setConnectivityState(offlineConnectivityState(), { source: 'navigator' });
  });
  // Do not clear the state on navigator "online": internet access does not
  // prove that this WebAgent server is reachable. The health poll clears it.
  if (!_observer && document.documentElement) {
    _observer = new MutationObserver(() => _applyDomState());
    _observer.observe(document.documentElement, { childList: true, subtree: true });
  }
}

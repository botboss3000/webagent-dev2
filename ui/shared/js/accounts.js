'use strict';

import { browserPersistenceAllowed } from './browser-storage-policy.js';

/**
 * Multi-account localStorage manager.
 *
 * Single source of truth for the set of signed-in accounts in this browser.
 * The legacy single-account keys (`auth_token`, `auth_username`, `auth_user_id`,
 * `auth_display_name`, `remember_token`) are kept in sync as derived mirrors
 * of the currently-active account so existing code that reads them keeps
 * working unchanged.
 */

const ACCOUNTS_KEY = 'webagent_accounts';
const ACTIVE_KEY = 'webagent_active_user_id';

const LEGACY_KEYS = {
  token: 'auth_token',
  username: 'auth_username',
  user_id: 'auth_user_id',
  display_name: 'auth_display_name',
  remember_token: 'remember_token',
};

let _memoryAccounts = [];
let _memoryActiveId = '';

function _readPersistent() {
  try {
    const raw = localStorage.getItem(ACCOUNTS_KEY);
    if (!raw) return [];
    const arr = JSON.parse(raw);
    return Array.isArray(arr) ? arr : [];
  } catch (_) {
    return [];
  }
}

function _read() {
  return browserPersistenceAllowed() ? _readPersistent() : [..._memoryAccounts];
}

function _write(list) {
  if (!browserPersistenceAllowed()) {
    _memoryAccounts = [...list];
    return;
  }
  try {
    localStorage.setItem(ACCOUNTS_KEY, JSON.stringify(list));
  } catch (_) {}
}

/** Migrate legacy single-account storage into the multi-account list. */
function _migrateLegacy() {
  const existing = _read();
  if (existing.length > 0) return;

  const token = localStorage.getItem(LEGACY_KEYS.token);
  const username = localStorage.getItem(LEGACY_KEYS.username);
  const user_id = localStorage.getItem(LEGACY_KEYS.user_id);
  if (!token || !user_id) return;

  const acct = {
    user_id,
    username: username || user_id,
    display_name: localStorage.getItem(LEGACY_KEYS.display_name) || username || user_id,
    access_token: token,
    remember_token: localStorage.getItem(LEGACY_KEYS.remember_token) || '',
  };
  _write([acct]);
  try { localStorage.setItem(ACTIVE_KEY, user_id); } catch (_) {}
}

_migrateLegacy();

/** Mirror an account into the legacy single-account localStorage keys. */
function _writeLegacy(acct) {
  if (!browserPersistenceAllowed()) return;
  try {
    if (!acct) {
      Object.values(LEGACY_KEYS).forEach((k) => localStorage.removeItem(k));
      return;
    }
    localStorage.setItem(LEGACY_KEYS.token, acct.access_token || '');
    localStorage.setItem(LEGACY_KEYS.username, acct.username || '');
    localStorage.setItem(LEGACY_KEYS.user_id, acct.user_id || '');
    localStorage.setItem(LEGACY_KEYS.display_name, acct.display_name || acct.username || '');
    if (acct.remember_token) {
      localStorage.setItem(LEGACY_KEYS.remember_token, acct.remember_token);
    } else {
      localStorage.removeItem(LEGACY_KEYS.remember_token);
    }
  } catch (_) {}
}

/** Return the list of signed-in accounts. */
export function listAccounts() {
  return _read();
}

/** Return the currently-active account, or null if none. */
export function getActive() {
  const list = _read();
  if (list.length === 0) return null;
  const activeId = browserPersistenceAllowed()
    ? localStorage.getItem(ACTIVE_KEY)
    : _memoryActiveId;
  return list.find((a) => a.user_id === activeId) || list[0];
}

/** Insert-or-update an account by user_id and make it active. */
export function upsertAccount(payload) {
  if (!payload || !payload.user_id) return null;
  const list = _read();
  const idx = list.findIndex((a) => a.user_id === payload.user_id);
  const merged = {
    user_id: payload.user_id,
    username: payload.username || (idx >= 0 ? list[idx].username : payload.user_id),
    display_name: payload.display_name || (idx >= 0 ? list[idx].display_name : payload.username || payload.user_id),
    access_token: payload.access_token || (idx >= 0 ? list[idx].access_token : ''),
    remember_token: payload.remember_token || (idx >= 0 ? list[idx].remember_token : ''),
  };
  if (idx >= 0) list[idx] = merged;
  else list.push(merged);
  _write(list);
  if (browserPersistenceAllowed()) {
    try { localStorage.setItem(ACTIVE_KEY, merged.user_id); } catch (_) {}
  } else {
    _memoryActiveId = merged.user_id;
  }
  _writeLegacy(merged);
  _notify();
  return merged;
}

/** Patch fields on the active account (e.g. after PATCH /auth/me).
 * @expected-unused — kept as utility for future use. */
export function updateActive(patch) {
  const active = getActive();
  if (!active) return null;
  return upsertAccount({ ...active, ...patch });
}

/** Remove an account by user_id. If it was active, pick another (or none). */
export function removeAccount(user_id) {
  const list = _read().filter((a) => a.user_id !== user_id);
  _write(list);
  const activeId = browserPersistenceAllowed()
    ? localStorage.getItem(ACTIVE_KEY)
    : _memoryActiveId;
  if (activeId === user_id) {
    if (list.length > 0) {
      if (browserPersistenceAllowed()) {
        try { localStorage.setItem(ACTIVE_KEY, list[0].user_id); } catch (_) {}
      } else {
        _memoryActiveId = list[0].user_id;
      }
      _writeLegacy(list[0]);
    } else {
      if (browserPersistenceAllowed()) {
        try { localStorage.removeItem(ACTIVE_KEY); } catch (_) {}
      } else {
        _memoryActiveId = '';
      }
      _writeLegacy(null);
    }
  }
  _notify();
  return list;
}

/** Switch to a different account by user_id. */
export async function switchTo(user_id) {
  const list = _read();
  const acct = list.find((a) => a.user_id === user_id);
  if (!acct) return null;

  // Try to use the cached JWT first. If it's expired/invalid we fall back
  // to /auth/recall using the stored remember_token (Google-style: no
  // password prompt unless the persistent credential is gone).
  let working = acct;
  const tokenOk = await _probeToken(acct.access_token);
  if (!tokenOk) {
    if (!acct.remember_token) {
      removeAccount(user_id);
      _notify();
      return null;
    }
    try {
      const res = await fetch('/api/v1/auth/recall', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ remember_token: acct.remember_token }),
      });
      if (!res.ok) {
        removeAccount(user_id);
        _notify();
        return null;
      }
      const data = await res.json();
      working = {
        ...acct,
        access_token: data.access_token,
        username: data.username || acct.username,
        display_name: data.display_name || acct.display_name,
      };
    } catch (_) {
      return null;
    }
  }

  upsertAccount(working);
  return working;
}

async function _probeToken(token) {
  if (!token) return false;
  try {
    const res = await fetch('/api/v1/auth/me', {
      headers: { 'Authorization': `Bearer ${token}` },
    });
    return res.ok;
  } catch (_) {
    return false;
  }
}

/** Subscribe to account list/active changes. Returns an unsubscribe fn. */
const _listeners = new Set();
export function onChange(fn) {
  _listeners.add(fn);
  return () => _listeners.delete(fn);
}
function _notify() {
  for (const fn of _listeners) {
    try { fn(); } catch (e) { console.warn('accounts onChange listener threw:', e); }
  }
  try { window.dispatchEvent(new CustomEvent('webagent-accounts-changed')); } catch (_) {}
}

// The policy event is synchronous and fires before durable account mirrors are
// erased. Preserve the active tab's identity in memory, never in another
// browser persistence API.
window.addEventListener('webagent-browser-storage-policy', (event) => {
  if (event.detail?.mode === 'persistent_cache') return;
  const durable = _readPersistent();
  _memoryAccounts = durable;
  try {
    _memoryActiveId = localStorage.getItem(ACTIVE_KEY) || durable[0]?.user_id || '';
  } catch (_) {
    _memoryActiveId = durable[0]?.user_id || '';
  }
});

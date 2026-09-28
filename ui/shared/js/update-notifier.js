'use strict';

// Update-available notifier — the account-panel action above Manage Account.
//
// Polls GET /api/v1/update/notifier-status (backed by ui/admin-tools/update/
// server.py) on a timer and shows the icon for ADMIN users at all times:
//   • badge + "N commits behind" / release tag when the configured upstream is
//     ahead of local HEAD or carries a new release,
//   • no badge + "Up to date" tooltip otherwise,
//   • "Update check unavailable" tooltip if the probe fails (e.g. server not
//     restarted with the endpoint yet).
// Non-admins never see the icon. Clicking it jumps straight to the Admin
// Tools → Update panel so the operator can review and apply changes.
//
// The poll is cheap on purpose: the endpoint does a single `git ls-remote`
// (no object download) and only fetches when the upstream tip actually moved.
//
// UI validation mode: append ?updicon=1 to the URL (or ?updicon=0 to clear)
// to force the account-panel action visible with a demo badge, bypassing admin + API checks.
// The choice persists in localStorage until cleared, so the header can be
// inspected across reloads without a configured upstream.

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

const POLL_MS = 10 * 60 * 1000; // 10 minutes
const FORCE_KEY = 'wa.forceUpdateIcon';

let _btn = null;
let _badge = null;
let _timer = null;
let _inFlight = false;
let _lastSignature = '';
// Authoritative admin flag. We can't rely on the `is-admin` body class alone:
// main.js sets that class in ITS 'admin-status-loaded' listener, which is
// registered AFTER this module's listener — so by the time our handler runs,
// the class isn't set yet. We take the event's `detail.is_admin` directly.
let _isAdminUser = false;

function _isAdmin() {
  return _isAdminUser || document.body.classList.contains('is-admin');
}

// Read + persist the validation override from the URL query string.
function _updateForceFlag() {
  try {
    const p = new URLSearchParams(location.search);
    if (p.has('updicon')) {
      const on = p.get('updicon') !== '0';
      if (on) localStorage.setItem(FORCE_KEY, '1');
      else localStorage.removeItem(FORCE_KEY);
    }
    return localStorage.getItem(FORCE_KEY) === '1';
  } catch (_) { return false; }
}

function _setVisible(show) {
  if (_btn) _btn.hidden = !show;
}

function _setBadge(text) {
  if (!_badge) return;
  if (text) {
    _badge.textContent = text;
    _badge.hidden = false;
  } else {
    _badge.hidden = true;
  }
}

function _render(status) {
  // Visibility is decided in _check (always-visible for admins). Here we only
  // set the badge + tooltip, so an up-to-date/not-configured state shows the
  // icon without a badge rather than hiding it.
  if (!status || !status.configured || !status.is_git) {
    _setBadge('');
    if (_btn) _btn.title = status && status.error
      ? 'Update check unavailable — ' + status.error
      : 'Update check unavailable — open the Update panel';
    return;
  }
  const behind = status.behind || 0;
  const hasRelease = !!status.has_new_release;
  if (!behind && !hasRelease) {
    _setBadge('');
    _btn.title = 'Up to date — open the Update panel';
    return;
  }
  if (hasRelease) {
    _setBadge(status.latest_version || 'new');
    _btn.title = `Update available: ${status.latest_version || 'new release'} — open the Update panel`;
  } else {
    _setBadge(String(behind));
    _btn.title = `${behind} commit${behind === 1 ? '' : 's'} behind upstream — open the Update panel`;
  }
}

async function _check() {
  // Validation override wins over everything — show a demo state.
  if (_updateForceFlag()) {
    _setVisible(true);
    _setBadge('3');
    if (_btn) _btn.title = 'Update available (forced for UI validation)';
    return;
  }
  // Always-visible for admins (validation-friendly); hidden for everyone else.
  if (!_isAdmin()) { _setVisible(false); return; }
  _setVisible(true);
  if (_inFlight) return;
  _inFlight = true;
  try {
    const res = await fetch(apiPath('/api/v1/update/notifier-status'), {
      headers: authHeaders(),
    });
    if (!res.ok) {
      _setBadge('');
      if (_btn) _btn.title = 'Update check unavailable — open the Update panel';
      return;
    }
    const status = await res.json().catch(() => null);
    if (!status) {
      _setBadge('');
      if (_btn) _btn.title = 'Update check unavailable — open the Update panel';
      return;
    }
    // Signature lets us skip re-rendering when nothing actually changed.
    const sig = JSON.stringify([status.configured, status.is_git, status.behind,
                                status.has_new_release, status.latest_version]);
    if (sig !== _lastSignature) {
      _lastSignature = sig;
      _render(status);
    }
  } catch (_) {
    // Offline or transient failure — keep the icon, mark the check unavailable.
    _setBadge('');
    if (_btn) _btn.title = 'Update check unavailable — open the Update panel';
  } finally {
    _inFlight = false;
  }
}

// Click → open Admin Tools at the Update view. Two coordinated steps:
//   1. Seed the saved sidebar view so a first-time Admin Tools mount restores it.
//   2. Switch the sidebar directly (works when Admin Tools is ALREADY mounted —
//      applySidebarView's stored-view read only happens once at init), then
//      activate the Admin Tools tab (mounts it if needed, re-applies the view).
function _onClick() {
  try { localStorage.setItem('files.sidebarView', 'update'); } catch (_) {}
  if (typeof window.__applySidebarView === 'function') {
    window.__applySidebarView('update');
  }
  if (typeof window.__setMainTab === 'function') window.__setMainTab('admin-tools');
}

export function init() {
  _btn = document.getElementById('update-available-btn');
  _badge = document.getElementById('update-available-badge');
  if (!_btn) return; // button not in this page (widget/embed/splash)

  _btn.addEventListener('click', _onClick);

  _check();
  _timer = setInterval(_check, POLL_MS);

  // Refresh immediately when the tab becomes visible again (don't wait out a
  // full poll interval after the user returns to the app).
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) _check();
  });

  // Re-evaluate the moment admin status resolves. Consume the event's own
  // is_admin flag (NOT the body class — see _isAdmin above) so we don't race
  // main.js's class-setting listener.
  window.addEventListener('admin-status-loaded', (e) => {
    if (e && e.detail) _isAdminUser = !!e.detail.is_admin;
    _check();
  });

  // Belt-and-braces: re-evaluate shortly after boot regardless of event
  // ordering. By then the body class is set for a confirmed admin, so even if
  // the event was missed entirely the icon still appears.
  setTimeout(_check, 1000);
  setTimeout(_check, 4000);
}

export function destroy() {
  if (_timer) { clearInterval(_timer); _timer = null; }
}

init();

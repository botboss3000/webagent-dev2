'use strict';

// Kill-switch Debug-panel button — sits directly right of Commit & Push.
//
// Admin-only. When clicked it arms (two-tap confirm, matching the debug
// console's restart button), then POSTs to /api/v1/kill-switch with
// { engaged: !current }. Engaging cancels every live agent run and stops every
// background service (revivals, watchdog, polling, workers); the app stays
// usable for foreground work (open a page, start/continue a chat). The button
// turns red while engaged and reads "stop all background activity" as its
// tooltip when idle.
//
// State is polled from GET /api/v1/kill-switch so a second device or a server
// restart keeps the icon truthful. Non-admins never see the icon.

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

let _btn = null;
let _engaged = false;
let _armed = false;
let _busy = false;
let _armTimer = null;
let _isAdminUser = false;

function _isAdmin() {
  return _isAdminUser || document.body.classList.contains('is-admin');
}

function _render() {
  if (!_btn) return;
  const admin = _isAdmin();
  _btn.hidden = !admin;
  if (!admin) return;
  _btn.classList.toggle('active', _engaged);
  _btn.classList.toggle('is-armed', _armed);
  _btn.classList.toggle('is-busy', _busy);
  _btn.disabled = _busy;
  _btn.setAttribute('aria-busy', String(_busy));
  _btn.setAttribute('aria-pressed', String(_engaged));
  const live = _btn.querySelector('.kill-switch-live');
  const idleStop = 'Global stop: stop active run families and background services. Stopped runs stay stopped; watchdog recovery is suppressed while engaged. The switch itself is momentary, so a server restart clears it without reviving stopped runs.';
  const idleResume = 'Global stop is engaged. Active run families are stopped and automatic recovery is suppressed. Click to resume background services; stopped runs remain stopped. A server restart clears only this momentary switch.';
  if (_armed) {
    _btn.title = _engaged ? 'Confirm resume of background services' : 'Confirm global stop of all active run families';
    _btn.setAttribute('aria-label', _btn.title);
    if (live) live.textContent = _btn.title;
  } else if (_busy) {
    _btn.title = _engaged ? 'Resuming background services…' : 'Stopping active run families and suppressing recovery…';
    _btn.setAttribute('aria-label', _btn.title);
    if (live) live.textContent = _btn.title;
  } else {
    _btn.title = _engaged ? idleResume : idleStop;
    _btn.setAttribute('aria-label', _btn.title);
    if (live) live.textContent = _engaged ? 'Global stop engaged; automatic recovery suppressed' : 'Global stop is off';
  }
}

function _resetArm() {
  _armed = false;
  if (_armTimer) { clearTimeout(_armTimer); _armTimer = null; }
  _render();
}

async function _fetchStatus() {
  try {
    const res = await fetch(apiPath('/api/v1/kill-switch'), { headers: authHeaders() });
    if (res.ok) {
      const data = await res.json().catch(() => null);
      if (data && typeof data.engaged === 'boolean') _engaged = data.engaged;
    }
  } catch (_) { /* keep last known state */ }
  _render();
}

async function _apply(engaged) {
  let ok = false;
  _busy = true;
  _render();
  try {
    const res = await fetch(apiPath('/api/v1/kill-switch'), {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify({ engaged }),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
      console.error('kill switch failed: ' + (detail || ('HTTP ' + res.status)));
      _busy = false;
      _resetArm();
      await _fetchStatus();
      return;
    }
    const data = await res.json().catch(() => null);
    if (data && typeof data.engaged === 'boolean') _engaged = data.engaged;
    ok = true;
    // Debug visibility: log exactly what the server stopped/started.
    if (data) {
      if (engaged) {
        const stopped = (data.services_stopped || []).join(', ')
          || '(none — only the device-worker control channel stays up)';
        console.log('› kill switch ENGAGED — ' + (data.cancelled_runs || 0) + ' run(s) cancelled, '
          + (data.browser_turns || 0) + ' browser turn(s) interrupted, '
          + (data.queued_flipped || 0) + ' queued message(s) finalised; services stopped: '
          + stopped + ((data.fleet_targets || 0) ? '; fleet notified: ' + data.fleet_targets + ' device(s)' : ''));
      } else {
        console.log('› kill switch DISENGAGED — services restarted: '
          + ((data.services_started || []).join(', ') || '(none)'));
      }
    }
  } catch (e) {
    console.error('kill switch failed: ' + (e.message || e));
  }
  _busy = false;
  _resetArm();
  if (ok) _broadcastChange();
}

// Let the session dropdown + Sessions page refresh immediately (so running
// spinners clear / reappear) and re-tint stopped-session times.
function _broadcastChange() {
  try { window.dispatchEvent(new CustomEvent('kill-switch-changed', { detail: { engaged: _engaged } })); } catch (_) {}
  // Dropdown refresh regardless of which tab is active (it also updates the
  // chat header trigger label + badge).
  try {
    if (typeof app !== 'undefined' && app && app.currentUserId
        && typeof app.populateSessionSelect === 'function') {
      app.populateSessionSelect(app.currentUserId);
    }
  } catch (_) {}
}

function _onClick() {
  if (!_isAdmin() || _busy) return;
  if (!_armed) {
    _armed = true;
    if (_armTimer) clearTimeout(_armTimer);
    _armTimer = setTimeout(_resetArm, 4000);
    console.log('› global stop ARMED — click again within 4s to confirm. It stops active run families permanently, suppresses recovery while engaged, and a server restart clears only the momentary switch—not the stopped-run state.');
    _render();
    return;
  }
  _resetArm();
  _apply(!_engaged);
}

function initKillSwitch() {
  _btn = document.getElementById('kill-switch-toggle');
  if (!_btn) return;

  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons({ nodes: [_btn.querySelector('[data-lucide]')].filter(Boolean) }); } catch (_) {}
  }

  // Delegated listener on document (capture-safe): the header tab strip can
  // rebuild/re-render and the index.html desktop click-completion fallback may
  // re-dispatch a click against a re-resolved element — a direct listener on
  // _btn would be silently orphaned if the button node were ever replaced.
  // Delegation means any real or completed click on #kill-switch-toggle still
  // reaches us. The 4s arm window makes double-fires harmless (2nd tap = confirm).
  document.addEventListener('click', (e) => {
    if (!e.target || typeof e.target.closest !== 'function') return;
    if (e.target.closest('#kill-switch-toggle')) _onClick();
  });

  // Authoritative admin flag from the same event update-notifier uses; the
  // body class is a fallback for the race where main.js hasn't set it yet.
  window.addEventListener('admin-status-loaded', (e) => {
    _isAdminUser = !!(e.detail && e.detail.is_admin);
    _render();
  });

  // The switch was toggled from another tab/device of this user (agentWs
  // re-dispatches the server's kill_switch WS event here). Sync the button
  // without re-fetching.
  window.addEventListener('kill-switch-changed', (e) => {
    if (e.detail && typeof e.detail.engaged === 'boolean') {
      _engaged = e.detail.engaged;
      _render();
    }
  });

  _fetchStatus();
  // Keep the icon truthful if the switch is toggled from another device.
  setInterval(() => { if (_isAdmin()) _fetchStatus(); }, 30000);
}

initKillSwitch();

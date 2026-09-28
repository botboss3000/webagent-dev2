'use strict';

/**
 * Manage Account tab — profile/password/delete self-service.
 *
 * The tab renders three sections: profile (email + display name), password
 * (current + new + confirm), and danger zone (delete account with password
 * confirmation). All calls go to /api/v1/auth/* endpoints.
 */

import { renderAvatar } from './user-avatar.js';
import { _esc } from './dom-utils.js';
import { getActive, upsertAccount } from './accounts.js';
// "My appearance" per-user theme editor. Self-fills its slot only when the admin
// has enabled per-user themes (App Settings → Appearance); otherwise removes it.
import { mountMyAppearance } from './appearance-me.js';
// "Your database" per-user connect panel. Self-fills its slot only when the
// admin has turned on User BYOD (App Settings); otherwise removes itself.
import { mountMyDatabase } from './tenant-db.js';
import { mountMyModels } from './my-models.js';
import { checkDevicePurge, purgeAndAcknowledge } from './device-purge.js';

let _wired = false;

function _authHeaders() {
  const a = getActive();
  return a && a.access_token ? { 'Authorization': `Bearer ${a.access_token}` } : {};
}

function _setStatus(el, msg, kind) {
  if (!el) return;
  el.textContent = msg || '';
  el.style.color = kind === 'error' ? 'var(--danger)' : (kind === 'ok' ? 'var(--success)' : 'var(--fg-2)');
  el.style.display = msg ? 'block' : 'none';
}

export function initAccount() {
  // The tab is re-rendered on startAccount() (whenever the user opens it).
  // Don't re-render in-place on account changes — that would wipe inline
  // status messages right after a save. The header avatar/dropdown handles
  // its own refresh via sessions.js's listener.
}

export function startAccount() {
  renderTab();
}

function stopAccount() {
  // nothing to tear down
}

function renderTab() {
  const tab = document.getElementById('tab-account');
  if (!tab) return;
  const active = getActive();

  tab.innerHTML = '';

  const wrap = document.createElement('div');
  wrap.className = 'account-tab-wrap';

  // Header with avatar + identity
  const header = document.createElement('div');
  header.className = 'account-header';
  if (active) header.appendChild(renderAvatar(active, 'xl'));
  const idBlock = document.createElement('div');
  idBlock.className = 'account-header-id';
  idBlock.innerHTML = `
    <div class="account-header-name">${_esc(active?.display_name || 'Signed out')}</div>
    <div class="account-header-email">${_esc(active?.username || '')}</div>
  `;
  header.appendChild(idBlock);
  wrap.appendChild(header);

  if (!active) {
    const msg = document.createElement('div');
    msg.className = 'account-empty';
    msg.textContent = 'You are not signed in. Sign in to manage your account.';
    wrap.appendChild(msg);
    tab.appendChild(wrap);
    return;
  }

  // ── Profile section ──
  const profile = document.createElement('section');
  profile.className = 'account-section';
  profile.innerHTML = `
    <h3 class="account-section-title">Profile</h3>
    <div class="account-field">
      <label for="account-email">Email / username</label>
      <input id="account-email" type="email" name="username" autocomplete="username" value="${escapeAttr(active.username || '')}">
    </div>
    <div class="account-field">
      <label for="account-display">Display name</label>
      <input id="account-display" type="text" name="name" autocomplete="name" value="${escapeAttr(active.display_name || '')}">
    </div>
    <div class="account-row">
      <button id="account-save-profile" class="account-btn account-btn-primary">Save changes</button>
      <span id="account-profile-status" class="account-status" style="display:none;"></span>
    </div>
  `;
  wrap.appendChild(profile);

  // ── Password section ──
  const pw = document.createElement('section');
  pw.className = 'account-section';
  pw.innerHTML = `
    <h3 class="account-section-title">Password</h3>
    <div class="account-field">
      <label for="account-curpass">Current password</label>
      <input id="account-curpass" type="password" autocomplete="current-password">
    </div>
    <div class="account-field">
      <label for="account-newpass">New password</label>
      <input id="account-newpass" type="password" autocomplete="new-password">
    </div>
    <div class="account-field">
      <label for="account-newpass2">Confirm new password</label>
      <input id="account-newpass2" type="password" autocomplete="new-password">
    </div>
    <div class="account-row">
      <button id="account-change-password" class="account-btn account-btn-primary">Change password</button>
      <span id="account-password-status" class="account-status" style="display:none;"></span>
    </div>
  `;
  wrap.appendChild(pw);

  // ── Sign-in & sessions (per-user session policy) ──
  // How long this user's login pass lasts before it must renew, plus whether it
  // silently auto-renews so they're never signed out mid-use. Backed by
  // GET/PUT /api/v1/auth/me/session-policy. The section is appended optimistically
  // and removed in loadSessionPolicy() if the caller has no real account record
  // (e.g. an anonymous guest), so guests never see a control that can't save.
  const sess = document.createElement('section');
  sess.className = 'account-section';
  sess.id = 'account-session-section';
  sess.innerHTML = `
    <h3 class="account-section-title">Sign-in &amp; sessions</h3>
    <div class="account-field">
      <label for="account-session-lifetime">Keep me signed in for</label>
      <select id="account-session-lifetime">
        <option value="1440">1 day</option>
        <option value="10080">7 days</option>
        <option value="43200">30 days</option>
        <option value="129600">90 days</option>
      </select>
    </div>
    <label class="account-pref-row">
      <input type="checkbox" id="account-session-autorenew">
      <span class="account-pref-text">
        <span class="account-pref-title">Stay signed in automatically</span>
        <span class="account-pref-desc">Renew my session in the background so I stay signed in while I keep using the app — I won't be logged out the moment the period above runs out. Turn off to be signed out cleanly when that period ends.</span>
      </span>
    </label>
    <div class="account-row">
      <button id="account-save-session" class="account-btn account-btn-primary">Save changes</button>
      <span id="account-session-status" class="account-status" style="display:none;"></span>
    </div>
    <div class="account-field" style="margin-top:18px;">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px;">
        <label style="margin:0;">Signed-in devices</label>
        <button id="account-devices-refresh" type="button" class="account-device-refresh-btn" title="Refresh device sessions" aria-label="Refresh device sessions"><i data-lucide="refresh-cw" style="width:14px;height:14px;"></i></button>
      </div>
      <div id="account-device-list" class="account-device-list">
        <span class="account-status" style="display:block;">Loading devices…</span>
      </div>
      <span id="account-device-status" class="account-status" style="display:none;"></span>
    </div>
  `;
  wrap.appendChild(sess);

  // ── Preferences ──
  const prefs = document.createElement('section');
  prefs.className = 'account-section';
  // The welcome-screen row appears only when the splash plugin is installed AND
  // the admin hasn't disabled it app-wide (window.WA_SPLASH, set by
  // ui/splash/splash-page). It and the landing's own "Don't show this again"
  // checkbox drive the SAME per-device flag (the wa_seen_splash skip cookie), so
  // they stay linked. Drop-in safe: delete the splash folder → no WA_SPLASH →
  // this row simply doesn't render.
  const _splash = window.WA_SPLASH;
  const splashRow = (_splash && _splash.enabled()) ? `
    <label class="account-pref-row">
      <input type="checkbox" id="account-splash-show">
      <span class="account-pref-text">
        <span class="account-pref-title">Show welcome screen</span>
        <span class="account-pref-desc">The welcome landing page shown at the front door before the app. Turn off to go straight to the app on this device; turn back on to see the welcome page again.</span>
      </span>
    </label>
  ` : '';
  prefs.innerHTML = `
    <h3 class="account-section-title">Preferences</h3>
    ${splashRow}
  `;
  // Only show the Preferences section when a control is available.
  if (splashRow) wrap.appendChild(prefs);

  // ── Appearance (per-user theme editor) ──
  // A positioned slot that mountMyAppearance() fills when the admin has enabled
  // per-user themes, or removes when off. Kept here (before Danger zone) so the
  // section lands in the right place even though it loads asynchronously.
  const appearanceSlot = document.createElement('section');
  appearanceSlot.className = 'account-section';
  appearanceSlot.id = 'account-appearance-slot';
  wrap.appendChild(appearanceSlot);

  // ── My models (per-user BYO model account) ──
  const modelsSlot = document.createElement('section');
  modelsSlot.className = 'account-section';
  modelsSlot.id = 'account-models-slot';
  wrap.appendChild(modelsSlot);

  // ── Your database (per-user bring-your-own-database) ──
  // A slot that mountMyDatabase() fills only when the admin has turned on
  // User BYOD, letting this user point their interaction data at their
  // own Postgres. Removes itself when the feature is off.
  const databaseSlot = document.createElement('section');
  databaseSlot.className = 'account-section';
  databaseSlot.id = 'account-database-slot';
  wrap.appendChild(databaseSlot);

  // ── Danger zone ──
  const danger = document.createElement('section');
  danger.className = 'account-section account-danger-zone';
  danger.innerHTML = `
    <h3 class="account-section-title">Danger zone</h3>
    <p class="account-danger-desc">Deleting your account is permanent. Your sessions and data cannot be recovered.</p>
    <button id="account-delete-trigger" class="account-btn account-btn-danger">Delete account…</button>
    <div id="account-delete-confirm" class="account-delete-confirm" style="display:none;">
      <div class="account-field">
        <label for="account-delete-pass">Type your password to confirm</label>
        <input id="account-delete-pass" type="password" autocomplete="current-password">
      </div>
      <label class="account-delete-ack">
        <input type="checkbox" id="account-delete-ack">
        I understand this action cannot be undone.
      </label>
      <div class="account-row">
        <button id="account-delete-confirm-btn" class="account-btn account-btn-danger" disabled>Permanently delete</button>
        <button id="account-delete-cancel" class="account-btn">Cancel</button>
        <span id="account-delete-status" class="account-status" style="display:none;"></span>
      </div>
    </div>
  `;
  wrap.appendChild(danger);

  const footer = document.createElement('footer');
  footer.className = 'account-footer';
  footer.innerHTML = '<a href="/privacy">Privacy Policy</a>';
  wrap.appendChild(footer);

  tab.appendChild(wrap);

  wireHandlers();

  // Fill (or remove) the per-user theme editor now that the slot is in the DOM.
  mountMyAppearance(document.getElementById('account-appearance-slot'));
  mountMyModels(document.getElementById('account-models-slot'));
  // Fill (or remove) the per-user "Your database" panel.
  mountMyDatabase(document.getElementById('account-database-slot'));
}

function wireHandlers() {
  const profileBtn = document.getElementById('account-save-profile');
  if (profileBtn) profileBtn.addEventListener('click', onSaveProfile);

  const pwBtn = document.getElementById('account-change-password');
  if (pwBtn) pwBtn.addEventListener('click', onChangePassword);

  const sessBtn = document.getElementById('account-save-session');
  if (sessBtn) sessBtn.addEventListener('click', onSaveSessionPolicy);
  loadSessionPolicy();
  loadDevices();

  const refreshDevicesBtn = document.getElementById('account-devices-refresh');
  if (refreshDevicesBtn) refreshDevicesBtn.addEventListener('click', loadDevices);

  const delTrigger = document.getElementById('account-delete-trigger');
  if (delTrigger) delTrigger.addEventListener('click', () => {
    document.getElementById('account-delete-confirm').style.display = 'block';
    delTrigger.style.display = 'none';
  });

  const delCancel = document.getElementById('account-delete-cancel');
  if (delCancel) delCancel.addEventListener('click', () => {
    document.getElementById('account-delete-confirm').style.display = 'none';
    document.getElementById('account-delete-trigger').style.display = '';
    document.getElementById('account-delete-pass').value = '';
    document.getElementById('account-delete-ack').checked = false;
    document.getElementById('account-delete-confirm-btn').disabled = true;
    _setStatus(document.getElementById('account-delete-status'), '', '');
  });

  const ack = document.getElementById('account-delete-ack');
  const pass = document.getElementById('account-delete-pass');
  function evalDeleteEnable() {
    document.getElementById('account-delete-confirm-btn').disabled =
      !(ack.checked && pass.value.length > 0);
  }
  if (ack) ack.addEventListener('change', evalDeleteEnable);
  if (pass) pass.addEventListener('input', evalDeleteEnable);

  const delBtn = document.getElementById('account-delete-confirm-btn');
  if (delBtn) delBtn.addEventListener('click', onDeleteAccount);

  // Welcome-screen toggle — bound to the splash plugin's per-device flag via
  // window.WA_SPLASH (same flag as the splash's own "Don't show this again").
  const splashShow = document.getElementById('account-splash-show');
  if (splashShow && window.WA_SPLASH) {
    splashShow.checked = window.WA_SPLASH.isShown();
    splashShow.addEventListener('change', () => {
      const show = splashShow.checked;
      window.WA_SPLASH.setShown(show);
      // The welcome screen is served only by the root route now; the app shell
      // lives at /app. Return to the front door immediately when re-enabling it
      // so the cleared "seen" cookie takes effect instead of appearing to do
      // nothing until a later visit.
      if (show) window.location.assign('/');
    });
    // Stay in sync if the splash's own checkbox flips the same flag.
    window.addEventListener('wa-splash-pref-changed', (e) => {
      if (!document.body.contains(splashShow)) return;
      splashShow.checked = !!(e.detail && e.detail.shown);
    });
  }
}

async function onSaveProfile() {
  const status = document.getElementById('account-profile-status');
  const emailEl = document.getElementById('account-email');
  const dispEl = document.getElementById('account-display');
  const email = emailEl.value.trim();
  const display = dispEl.value.trim();
  const active = getActive();
  if (!active) return;

  const body = {};
  if (email !== active.username) body.email = email;
  if (display !== active.display_name) body.display_name = display;
  if (Object.keys(body).length === 0) {
    _setStatus(status, 'No changes to save', '');
    return;
  }

  _setStatus(status, 'Saving…', '');
  try {
    const res = await fetch('/api/v1/auth/me', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to update profile', 'error');
      return;
    }
    const data = await res.json();
    upsertAccount({
      ...active,
      username: data.username,
      display_name: data.display_name,
      access_token: data.access_token,
    });
    _setStatus(status, 'Saved', 'ok');
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

async function onChangePassword() {
  const status = document.getElementById('account-password-status');
  const cur = document.getElementById('account-curpass').value;
  const np = document.getElementById('account-newpass').value;
  const np2 = document.getElementById('account-newpass2').value;
  if (!cur || !np) {
    _setStatus(status, 'Fill in both password fields', 'error');
    return;
  }
  if (np !== np2) {
    _setStatus(status, 'New passwords do not match', 'error');
    return;
  }
  if (np.length < 4) {
    _setStatus(status, 'New password must be at least 4 characters', 'error');
    return;
  }
  _setStatus(status, 'Changing password…', '');
  try {
    const res = await fetch('/api/v1/auth/change-password', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ current_password: cur, new_password: np }),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to change password', 'error');
      return;
    }
    const data = await res.json();
    const active = getActive();
    if (active && data.remember_token) {
      upsertAccount({ ...active, remember_token: data.remember_token });
    }
    document.getElementById('account-curpass').value = '';
    document.getElementById('account-newpass').value = '';
    document.getElementById('account-newpass2').value = '';
    _setStatus(status, 'Password changed', 'ok');
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

// Load the caller's session policy into the controls. If the caller has no real
// account record (anonymous guest → 404/401), drop the whole section so a guest
// never sees a control that can't be saved.
async function loadSessionPolicy() {
  const section = document.getElementById('account-session-section');
  if (!section) return;
  try {
    const res = await fetch('/api/v1/auth/me/session-policy', { headers: _authHeaders() });
    if (!res.ok) { section.remove(); return; }
    const data = await res.json();
    const sel = document.getElementById('account-session-lifetime');
    const renew = document.getElementById('account-session-autorenew');
    if (sel) {
      const mins = String(data.lifetime_minutes);
      // If the stored value isn't one of the presets, add it as a selected option
      // so the dropdown faithfully shows the current setting.
      if (!Array.from(sel.options).some((o) => o.value === mins)) {
        const days = Math.round(data.lifetime_minutes / 1440);
        const opt = document.createElement('option');
        opt.value = mins;
        opt.textContent = days >= 1 ? `${days} day${days === 1 ? '' : 's'}` : `${Math.round(data.lifetime_minutes / 60)} hours`;
        sel.appendChild(opt);
      }
      sel.value = mins;
    }
    if (renew) renew.checked = !!data.auto_renew;
  } catch (_) {
    section.remove();
  }
}

async function onSaveSessionPolicy() {
  const status = document.getElementById('account-session-status');
  const sel = document.getElementById('account-session-lifetime');
  const renew = document.getElementById('account-session-autorenew');
  if (!sel || !renew) return;
  _setStatus(status, 'Saving…', '');
  try {
    const res = await fetch('/api/v1/auth/me/session-policy', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({
        lifetime_minutes: parseInt(sel.value, 10),
        auto_renew: renew.checked,
      }),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to save', 'error');
      return;
    }
    const data = await res.json();
    // Apply the fresh pass + renewal ticket immediately so the new policy takes
    // effect without a re-login (a cleared ticket turns silent renewal off).
    const active = getActive();
    if (active) {
      upsertAccount({
        ...active,
        access_token: data.access_token || active.access_token,
        remember_token: data.remember_token || '',
      });
    }
    _setStatus(status, 'Saved', 'ok');
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

function _deviceTime(value) {
  if (!value) return 'Unknown';
  try { return new Date(Number(value) * 1000).toLocaleString(); }
  catch (_) { return 'Unknown'; }
}

function _deviceLabel(userAgent) {
  const ua = String(userAgent || '');
  const browser = /Edg\//.test(ua) ? 'Edge'
    : (/Firefox\//.test(ua) ? 'Firefox'
      : (/Chrome\//.test(ua) ? 'Chrome'
        : (/Safari\//.test(ua) ? 'Safari' : 'Browser')));
  const platform = /Android/.test(ua) ? 'Android'
    : (/iPhone|iPad/.test(ua) ? 'iOS'
      : (/Windows/.test(ua) ? 'Windows'
        : (/Mac OS/.test(ua) ? 'macOS' : (/Linux/.test(ua) ? 'Linux' : 'device'))));
  return `${browser} on ${platform}`;
}

// Human label for the origin a sign-in came through — [localhost] / [tunnel] /
// plain URL for anything else. Only filled for logins captured after this
// feature shipped (the origin is read from the login request).
function _originLabel(origin) {
  if (!origin) return '';
  let host = origin;
  let kind = '';
  try {
    const u = new URL(origin);
    host = u.host;
    const hostname = u.hostname.toLowerCase();
    if (hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '::1') {
      kind = 'localhost';
    } else if (/\.(trycloudflare\.com|ngrok\.(io|app|free\.app)|loca\.lt|serveo\.net|localhost\.run|tunnel\.dev)$/.test(hostname)) {
      kind = 'tunnel';
    }
  } catch (_) { /* fall through to the raw origin */ }
  return kind ? `[${kind}] ${host}` : host;
}

async function loadDevices() {
  const list = document.getElementById('account-device-list');
  if (!list) return;
  const refreshBtn = document.getElementById('account-devices-refresh');
  if (refreshBtn) { refreshBtn.disabled = true; refreshBtn.classList.add('spinning'); }
  try {
    const res = await fetch('/api/v1/auth/me/devices', {
      headers: _authHeaders(),
      cache: 'no-store',
    });
    if (!res.ok) { list.closest('.account-field')?.remove(); return; }
    const data = await res.json();
    list.innerHTML = '';
    const devices = Array.isArray(data.devices) ? data.devices : [];
    if (!devices.length) {
      list.textContent = 'No device sessions found.';
      return;
    }
    const selected = new Set();
    const frame = document.createElement('div');
    frame.className = 'account-device-table-frame';
    const table = document.createElement('table');
    table.className = 'account-device-table';
    const head = document.createElement('thead');
    const headerRow = document.createElement('tr');
    const selectHeader = document.createElement('th');
    selectHeader.className = 'account-device-select';
    const selectAll = document.createElement('input');
    selectAll.type = 'checkbox';
    selectAll.setAttribute('aria-label', 'Select all device sessions');
    selectHeader.appendChild(selectAll);
    headerRow.appendChild(selectHeader);
    headerRow.insertAdjacentHTML(
      'beforeend',
      '<th>Device</th><th>Last login</th><th>Location</th>',
    );
    const actionHeader = document.createElement('th');
    actionHeader.className = 'account-device-action';
    const bulkButton = document.createElement('button');
    bulkButton.type = 'button';
    bulkButton.className = 'account-btn account-btn-danger account-device-purge-btn';
    bulkButton.textContent = 'Sign out & purge';
    bulkButton.disabled = true;
    actionHeader.appendChild(bulkButton);
    headerRow.appendChild(actionHeader);
    head.appendChild(headerRow);
    table.appendChild(head);
    const body = document.createElement('tbody');
    table.appendChild(body);
    frame.appendChild(table);
    list.appendChild(frame);

    const rowCheckboxes = [];
    const updateBulkButton = () => {
      const count = selected.size;
      bulkButton.disabled = count === 0;
      bulkButton.textContent = count
        ? `Sign out & purge (${count})`
        : 'Sign out & purge';
      selectAll.checked = count > 0 && count === devices.length;
      selectAll.indeterminate = count > 0 && count < devices.length;
    };
    for (const device of devices) {
      const row = document.createElement('tr');
      const current = device.device_id === data.current_device_id;
      const location = device.location || 'Unknown location';
      const ip = device.ip_address || '';
      const origin = device.origin ? _originLabel(device.origin) : '';
      row.innerHTML = `
        <td class="account-device-select"></td>
        <td><span class="account-device-primary">${current ? 'This device' : _esc(_deviceLabel(device.user_agent))}</span><span class="account-device-secondary">${_esc(String(device.device_id || '').slice(0, 10))}</span></td>
        <td>${_esc(_deviceTime(device.last_login_at))}</td>
        <td><span class="account-device-primary">${_esc(location)}</span><span class="account-device-secondary">${_esc(ip)}</span>${origin ? `<span class="account-device-secondary account-device-origin">${_esc(origin)}</span>` : ''}</td>
        <td></td>
      `;
      const checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.setAttribute(
        'aria-label',
        current ? 'Select this device' : `Select ${_deviceLabel(device.user_agent)}`,
      );
      checkbox.addEventListener('change', () => {
        if (checkbox.checked) selected.add(device.device_id);
        else selected.delete(device.device_id);
        updateBulkButton();
      });
      row.querySelector('.account-device-select').appendChild(checkbox);
      row.addEventListener('click', (event) => {
        if (event.target === checkbox) return;
        checkbox.checked = !checkbox.checked;
        checkbox.dispatchEvent(new Event('change'));
      });
      rowCheckboxes.push({ checkbox, deviceId: device.device_id });
      body.appendChild(row);
    }
    selectAll.addEventListener('change', () => {
      selected.clear();
      for (const entry of rowCheckboxes) {
        entry.checkbox.checked = selectAll.checked;
        if (selectAll.checked) selected.add(entry.deviceId);
      }
      updateBulkButton();
    });
    bulkButton.addEventListener('click', () => {
      const chosen = devices
        .filter(device => selected.has(device.device_id))
        .map(device => ({
          deviceId: device.device_id,
          current: device.device_id === data.current_device_id,
        }));
      revokeSelectedDevices(chosen, bulkButton);
    });
  } catch (_) {
    list.textContent = 'Could not load device sessions.';
  } finally {
    if (refreshBtn) { refreshBtn.disabled = false; refreshBtn.classList.remove('spinning'); }
  }
}

async function revokeSelectedDevices(devices, button) {
  if (!devices.length) return;
  const status = document.getElementById('account-device-status');
  button.disabled = true;
  _setStatus(
    status,
    `Signing out ${devices.length} selected device${devices.length === 1 ? '' : 's'}…`,
    '',
  );

  try {
    const res = await fetch('/api/v1/auth/me/devices/revoke', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ device_ids: devices.map(device => device.deviceId) }),
    });
    if (!res.ok) {
      _setStatus(status, (await _detail(res)) || 'Could not purge devices', 'error');
      button.disabled = false;
      return;
    }
    const result = await res.json();
    const completed = Number(result.revoked_count || 0);
    if (devices.some(device => device.current)) {
      const purged = await checkDevicePurge({ reload: true });
      if (!purged) _setStatus(status, 'Signed out; device data purge could not complete.', 'error');
      return;
    }
    _setStatus(
      status,
      `${completed} device${completed === 1 ? '' : 's'} forcibly signed out and purge enforced.`,
      'ok',
    );
    await loadDevices();
  } catch (error) {
    _setStatus(status, String(error?.message || 'Connection error'), 'error');
    button.disabled = false;
  }
}

async function onDeleteAccount() {
  const status = document.getElementById('account-delete-status');
  const pass = document.getElementById('account-delete-pass').value;
  const active = getActive();
  if (!active || !pass) return;
  const token = active.access_token || '';

  _setStatus(status, 'Deleting…', '');
  try {
    const res = await fetch('/api/v1/auth/me', {
      method: 'DELETE',
      headers: { 'Content-Type': 'application/json', ..._authHeaders() },
      body: JSON.stringify({ password: pass }),
    });
    if (!res.ok) {
      const detail = await _detail(res);
      _setStatus(status, detail || 'Failed to delete account', 'error');
      return;
    }
    const purged = await purgeAndAcknowledge(token, { reload: false });
    if (!purged) {
      _setStatus(
        status,
        'The server account was deleted, but this device purge is incomplete. Keep this page open to retry.',
        'error',
      );
      return;
    }
    // Reload — another account becomes active, or the login overlay appears.
    window.location.reload();
  } catch (e) {
    _setStatus(status, 'Connection error', 'error');
  }
}

async function _detail(res) {
  try {
    const d = await res.json();
    return d && d.detail ? String(d.detail) : '';
  } catch (_) { return ''; }
}

// Attribute-safe escaping: canonical _esc handles & < >, then escape " for use
// inside a double-quoted attribute value.
function escapeAttr(s) {
  return _esc(s).replace(/"/g, '&quot;');
}

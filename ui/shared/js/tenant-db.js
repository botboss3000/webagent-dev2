/*
 * tenant-db.js — the account page's "Your database" per-user panel.
 *
 * Part of the User BYOD (bring-your-own-database) feature. Lets a signed-in
 * user point WebAgent at THEIR OWN Postgres so their interaction data (chats,
 * memories, agents, secrets) lives in a database they control, instead of the
 * shared central one. mountMyDatabase(slot) is called by account.js; it removes
 * its slot entirely unless the admin has turned User BYOD ON (App
 * Settings), so single-tenant installs never show it.
 *
 * Talks to the backend at /api/v1/tenant-db (app/api/tenant_db.py):
 *   GET → status · POST /test → try connection · POST → save+bootstrap · DELETE.
 * The password is write-only: it is sent on save/test and stored server-side in
 * the credential store, never returned here.
 *
 * KEEP: self-contained ES module; removes itself when the feature is off.
 */

import { getActive } from './accounts.js';
import { _esc } from './dom-utils.js';

const API = '/api/v1/tenant-db';

function _authHeaders(extra) {
  const a = getActive();
  const h = a && a.access_token ? { 'Authorization': `Bearer ${a.access_token}` } : {};
  return Object.assign(h, extra || {});
}

export async function mountMyDatabase(slotEl) {
  if (!slotEl) return;
  let status;
  try {
    const r = await fetch(API, { headers: _authHeaders() });
    if (!r.ok) { slotEl.remove(); return; }
    status = await r.json();
  } catch (_) { slotEl.remove(); return; }
  // Feature off → no panel at all (single-tenant behaviour).
  if (!status || !status.user_byod_enabled) { slotEl.remove(); return; }
  _render(slotEl, status);
}

function _stateBanner(status) {
  const h = status.health || {};
  const conn = status.connection || {};
  if (status.configured) {
    if (h.state === 'unreachable') {
      return `<p class="account-status" style="color:var(--danger);display:block;">⚠ Your database is currently unreachable${h.message ? ' — ' + _esc(h.message) : ''}.</p>`;
    }
    return `<p class="account-danger-desc">Connected to your own database <strong>${_esc(conn.database || '')}</strong>${conn.host ? ' at <strong>' + _esc(conn.host) + '</strong>' : ''}. Your interaction data is stored there.</p>`;
  }
  return `<p class="account-danger-desc">You're using the <strong>shared</strong> database. Connect your own Postgres below to keep your chats, memories and keys in a database you own.</p>`;
}

function _render(slotEl, status) {
  const c = status.connection || {};
  const configured = !!status.configured;
  slotEl.innerHTML = `
    <h3 class="account-section-title">Your database</h3>
    ${_stateBanner(status)}
    <div class="account-field">
      <label for="tdb-provider">Provider</label>
      <select id="tdb-provider" class="ac-input">
        <option value="postgres">PostgreSQL</option>
        <option value="neon">Neon</option>
        <option value="aws_rds">AWS RDS / Aurora</option>
        <option value="gcp_cloud_sql">Google Cloud SQL</option>
        <option value="azure_postgres">Azure Postgres</option>
      </select>
    </div>
    <div class="account-row">
      <div class="account-field" style="flex:2;"><label for="tdb-host">Host</label>
        <input id="tdb-host" type="text" placeholder="db.example.com" value="${_esc(c.host || '')}"></div>
      <div class="account-field" style="flex:1;"><label for="tdb-port">Port</label>
        <input id="tdb-port" type="number" placeholder="5432" value="${_esc(c.port != null ? c.port : 5432)}"></div>
    </div>
    <div class="account-row">
      <div class="account-field" style="flex:1;"><label for="tdb-database">Database</label>
        <input id="tdb-database" type="text" placeholder="mydb" value="${_esc(c.database || '')}"></div>
      <div class="account-field" style="flex:1;"><label for="tdb-username">Username</label>
        <input id="tdb-username" type="text" placeholder="user" value="${_esc(c.username || '')}"></div>
    </div>
    <div class="account-row">
      <div class="account-field" style="flex:1;"><label for="tdb-password">Password</label>
        <input id="tdb-password" type="password" autocomplete="new-password" placeholder="${configured ? '•••••• (unchanged)' : ''}"></div>
      <div class="account-field" style="flex:1;"><label for="tdb-ssl">SSL</label>
        <select id="tdb-ssl" class="ac-input">
          <option value="require">Require</option>
          <option value="verify-full">Verify full</option>
          <option value="disable">Disable</option>
        </select></div>
    </div>
    <div class="account-row">
      <button id="tdb-test" class="account-btn">Test connection</button>
      <button id="tdb-save" class="account-btn account-btn-primary">${configured ? 'Update' : 'Connect'}</button>
      ${configured ? '<button id="tdb-remove" class="account-btn account-btn-danger">Disconnect</button>' : ''}
      <span id="tdb-status" class="account-status" style="display:none;"></span>
    </div>
  `;
  // Prefill selects from current config.
  const q = (id) => slotEl.querySelector('#' + id);
  if (c.provider) q('tdb-provider').value = c.provider;
  if (c.ssl_mode) q('tdb-ssl').value = c.ssl_mode;

  const statusEl = q('tdb-status');
  const say = (msg, kind) => {
    statusEl.style.display = 'inline';
    statusEl.textContent = msg;
    statusEl.style.color = kind === 'err' ? 'var(--danger)' : (kind === 'ok' ? 'var(--success)' : 'var(--text-dim)');
  };
  const body = () => ({
    provider: q('tdb-provider').value,
    host: q('tdb-host').value.trim() || null,
    port: parseInt(q('tdb-port').value, 10) || 5432,
    database: q('tdb-database').value.trim(),
    username: q('tdb-username').value.trim() || null,
    password: q('tdb-password').value,
    ssl_mode: q('tdb-ssl').value,
    schema_name: 'public',
  });

  q('tdb-test').addEventListener('click', async () => {
    say('Testing…');
    try {
      const r = await fetch(API + '/test', {
        method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body()),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) say('✓ Connected — ' + (d.database || 'ok'), 'ok');
      else say('✗ ' + (d.error || d.detail || ('HTTP ' + r.status)), 'err');
    } catch (e) { say('✗ ' + e.message, 'err'); }
  });

  q('tdb-save').addEventListener('click', async () => {
    say('Connecting & preparing your database…');
    try {
      const r = await fetch(API, {
        method: 'POST', headers: _authHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify(body()),
      });
      const d = await r.json().catch(() => ({}));
      if (r.ok && d.ok) {
        say('✓ Saved — your data now uses this database.', 'ok');
        setTimeout(() => mountMyDatabase(slotEl), 800);  // re-render with new status
      } else {
        say('✗ ' + (d.detail || d.error || ('HTTP ' + r.status)), 'err');
      }
    } catch (e) { say('✗ ' + e.message, 'err'); }
  });

  const rm = q('tdb-remove');
  if (rm) rm.addEventListener('click', async () => {
    if (!window.confirm('Disconnect your database? Your data stays in it (nothing is deleted), but WebAgent will use the shared database again.')) return;
    say('Disconnecting…');
    try {
      const r = await fetch(API, { method: 'DELETE', headers: _authHeaders() });
      if (r.ok) { say('Disconnected.', 'ok'); setTimeout(() => mountMyDatabase(slotEl), 600); }
      else say('✗ HTTP ' + r.status, 'err');
    } catch (e) { say('✗ ' + e.message, 'err'); }
  });
}

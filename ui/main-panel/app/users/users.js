'use strict';

/**
 * Instances — Usage Data tab (stable internal `users` route, "This device").
 *
 * App-wide roster and operational usage management, mirroring the Agents page
 * Members tab: account, activity, credit, tier-assignment and anonymous-subject
 * controls. Policy configuration lives only in Settings → User Policies.
 *
 * Data:
 *   GET  /admin/users/stats                 roster + stats (admin only)
 *   GET+POST /admin/settings/app            access mode (auto-save on pick)
 *   POST /admin/users/{uid}/approve|revoke  authorize / lock an account
 *   POST /admin/users/{uid}/credits         set trial / paid credit balance
 *   GET  /admin/entitlements/tiers          published product tiers
 *   POST /admin/entitlements/users/{uid}/tier  assign a product tier
 *   POST /admin/users/{uid}/set-admin       grant / revoke admin
 *   DELETE /admin/users/{uid}               reject & delete a pending account
 * All mutations are admin-only, guarded by isAdmin()/showRestrictedModal().
 *
 * Styling: users.css — mirrors the agents members-* classes, self-contained
 * in this folder. Mounted by instances.js (_onUsersTabRendered) into
 * #inst-users-host, exactly like the dashboard tab.
 */

import { apiPath } from '../../../shared/js/config.js';
import {
  clearCurrentTenantCache,
  getBrowserCacheSummary,
} from '../../../shared/js/browser-lifecycle.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js?v=253';
import { _fetch, _esc, _fmtDate } from '../settings/utils.js';

let _host = null;
let _rosterHost = null;
let _cachePanel = null;
let _creditPopover = null;
let _creditPopoverCleanup = null;
let _tierPopover = null;
let _tierPopoverCleanup = null;
let _publishedTiers = [];
let _anonymousControl = null;

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }

// ── Entry ────────────────────────────────────────────────────────────────
export function mountUsers(host) {
  _host = host;
  host.innerHTML = '';
  _cachePanel = _buildBrowserCachePanel();
  _rosterHost = document.createElement('div');
  _rosterHost.className = 'members-roster-host';
  _rosterHost.innerHTML = _usersSkeleton();
  host.appendChild(_cachePanel);
  host.appendChild(_rosterHost);
  _refreshBrowserCachePanel();
  _load();
}

export function unmountUsers() {
  _closeCreditPopover();
  _closeTierPopover();
  window.removeEventListener('webagent-browser-storage-pressure', _onStoragePressure);
  _host = null;
  _rosterHost = null;
  _cachePanel = null;
}

async function _load() {
  if (!_host) return;
  try {
    const [statsRes, tiersRes, anonControlRes] = await Promise.all([
      _fetch(apiPath('/admin/users/stats?requesting_user_id=' + encodeURIComponent(_uid()))),
      _fetch(apiPath('/admin/entitlements/tiers?status=published&requesting_user_id=' + encodeURIComponent(_uid())), { cache: 'no-store' }),
      _fetch(apiPath('/admin/users/anonymous-control?requesting_user_id=' + encodeURIComponent(_uid())), { cache: 'no-store' }),
    ]);
    if (tiersRes.ok) {
      try { _publishedTiers = (await tiersRes.json()).tiers || []; } catch { _publishedTiers = []; }
    }
    if (anonControlRes.ok) {
      try { _anonymousControl = await anonControlRes.json(); } catch { _anonymousControl = null; }
    }
    if (!statsRes.ok) throw new Error('HTTP ' + statsRes.status);
    const data = await statsRes.json();
    if (!_rosterHost) return;
    _render(data.users || [], data.anonymous_users || [], _anonymousControl);
  } catch (e) {
    if (!_rosterHost) return;
    _rosterHost.innerHTML = '';
    const error = document.createElement('div');
    error.className = 'members-loading members-roster-error';
    error.textContent = 'Usage data is unavailable: ' + e.message
      + '. Local cache controls remain available.';
    _rosterHost.appendChild(error);
  }
}

function _render(users, anonymousUsers, anonymousControl) {
  _closeCreditPopover();
  _closeTierPopover();
  if (!_rosterHost) return;
  _rosterHost.innerHTML = '';
  const notice = document.createElement('div'); notice.className = 'members-notice';
  notice.textContent = 'Activity counts reflect this instance only. Billing credits are app-wide; 1 credit equals 1 cent of usage.';
  _rosterHost.appendChild(notice);
  const admins = users.filter(u => u.is_admin);
  const members = users.filter(u => !u.is_admin);
  _rosterHost.appendChild(_buildUsersSection('Admins', admins, 'admin'));
  _rosterHost.appendChild(_buildUsersSection('Users', members, 'member'));
  _rosterHost.appendChild(_buildAnonymousSection(anonymousUsers, anonymousControl));
}

function _formatBytes(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return bytes + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let size = bytes / 1024;
  let unit = units[0];
  for (let i = 1; i < units.length && size >= 1024; i++) {
    size /= 1024;
    unit = units[i];
  }
  return size.toFixed(size >= 10 ? 1 : 2) + ' ' + unit;
}

function _accountLabel() {
  try {
    return localStorage.getItem('auth_display_name')
      || localStorage.getItem('auth_username')
      || localStorage.getItem('auth_user_id')
      || 'Current account';
  } catch (_) {
    return 'Current account';
  }
}

function _buildBrowserCachePanel() {
  window.removeEventListener('webagent-browser-storage-pressure', _onStoragePressure);
  window.addEventListener('webagent-browser-storage-pressure', _onStoragePressure);
  const panel = document.createElement('section');
  panel.className = 'browser-cache-panel';
  panel.setAttribute('aria-labelledby', 'browser-cache-title');
  panel.innerHTML = `
    <div class="browser-cache-head">
      <div>
        <div id="browser-cache-title" class="members-policy-title">This browser</div>
        <div class="browser-cache-subtitle">Read-only cache for <strong data-cache-account></strong> on this device. Cached data never expires automatically.</div>
      </div>
      <span class="browser-cache-mode" data-cache-mode>Checking…</span>
    </div>
    <div class="browser-cache-metrics" aria-live="polite">
      <div><span>Cached views</span><strong data-cache-pages>—</strong></div>
      <div><span>Sessions</span><strong data-cache-sessions>—</strong></div>
      <div><span>Transcript entries</span><strong data-cache-transcripts>—</strong></div>
      <div><span>Cached data</span><strong data-cache-bytes>—</strong></div>
      <div><span>Browser storage</span><strong data-browser-usage>—</strong></div>
      <div><span>Last validated</span><strong data-cache-validated>—</strong></div>
      <div><span>Schema</span><strong data-cache-schema>—</strong></div>
    </div>
    <div class="browser-cache-warning" data-cache-warning role="status" hidden></div>
    <div class="browser-cache-actions">
      <button type="button" class="members-btn make-admin" data-cache-refresh>Refresh usage</button>
      <button type="button" class="members-btn reject" data-cache-clear>Clear cached data</button>
      <span data-cache-status role="status"></span>
    </div>`;
  panel.querySelector('[data-cache-account]').textContent = _accountLabel();
  panel.querySelector('[data-cache-refresh]').addEventListener('click', _refreshBrowserCachePanel);
  panel.querySelector('[data-cache-clear]').addEventListener('click', _clearBrowserCache);
  return panel;
}

async function _refreshBrowserCachePanel() {
  const panel = _cachePanel;
  if (!panel) return;
  const refresh = panel.querySelector('[data-cache-refresh]');
  const status = panel.querySelector('[data-cache-status]');
  refresh.disabled = true;
  status.textContent = 'Calculating…';
  try {
    const summary = await getBrowserCacheSummary();
    if (panel !== _cachePanel) return;
    panel.querySelector('[data-cache-mode]').textContent = summary.context.mode.replaceAll('_', ' ');
    panel.querySelector('[data-cache-pages]').textContent = summary.counts.pages.toLocaleString();
    panel.querySelector('[data-cache-sessions]').textContent = summary.counts.sessions.toLocaleString();
    panel.querySelector('[data-cache-transcripts]').textContent = summary.counts.transcripts.toLocaleString();
    panel.querySelector('[data-cache-bytes]').textContent = 'About ' + _formatBytes(summary.approximate_cache_bytes);
    panel.querySelector('[data-browser-usage]').textContent = summary.browser_quota_bytes
      ? _formatBytes(summary.browser_usage_bytes) + ' of ' + _formatBytes(summary.browser_quota_bytes)
      : _formatBytes(summary.browser_usage_bytes);
    panel.querySelector('[data-cache-validated]').textContent = summary.last_validated_at
      ? _fmtDate(summary.last_validated_at) : 'Not yet validated';
    panel.querySelector('[data-cache-schema]').textContent = String(summary.context.schema_version || 0);
    const warning = panel.querySelector('[data-cache-warning]');
    if (summary.storage_pressure) {
      warning.hidden = false;
      warning.textContent = 'A cache write failed because browser storage was full or constrained. Existing cached views remain readable, but newer views may not have been saved.';
    } else if (summary.failures.length) {
      warning.hidden = false;
      warning.textContent = 'Some storage details could not be read. Existing cached views were not changed.';
    } else {
      warning.hidden = true;
      warning.textContent = '';
    }
    status.textContent = '';
  } catch (error) {
    status.textContent = 'Could not inspect local cache: ' + (error?.message || error);
  } finally {
    if (panel === _cachePanel) refresh.disabled = false;
  }
}

async function _clearBrowserCache() {
  const panel = _cachePanel;
  if (!panel) return;
  if (!window.confirm('Clear cached views, sessions, transcripts, and attachments for this account on this browser? You will remain signed in.')) return;
  const button = panel.querySelector('[data-cache-clear]');
  const status = panel.querySelector('[data-cache-status]');
  button.disabled = true;
  status.textContent = 'Clearing cached data…';
  try {
    const result = await clearCurrentTenantCache();
    if (!result.complete) {
      status.textContent = 'Cache clearing was incomplete: '
        + result.failures.map(row => row.target).join(', ') + '. Close other app tabs and try again.';
      await _refreshBrowserCachePanel();
      return;
    }
    status.textContent = 'Cached data cleared. Reloading…';
    setTimeout(() => window.location.reload(), 250);
  } catch (error) {
    status.textContent = 'Could not clear cached data: ' + (error?.message || error);
  } finally {
    if (panel === _cachePanel) button.disabled = false;
  }
}

function _onStoragePressure(event) {
  if (!_cachePanel) return;
  const warning = _cachePanel.querySelector('[data-cache-warning]');
  warning.hidden = false;
  warning.textContent = 'Browser storage is full or constrained. Existing cached views remain readable, but newer views may not be saved.';
  if (event?.detail?.occurred_at) warning.dataset.occurredAt = event.detail.occurred_at;
}

function _pctMetric(label, metric, suffix = '') {
  const used = Number(metric?.used || 0);
  const limit = Number(metric?.limit || 0);
  const pct = limit > 0 ? Math.min(100, Math.round(100 * used / limit)) : 0;
  return '<div class="anon-control-metric"><div><span>' + _esc(label) + '</span><strong>'
    + used.toLocaleString() + suffix + (limit > 0 ? ' / ' + limit.toLocaleString() + suffix : '')
    + '</strong></div><div class="anon-control-meter"><i style="width:' + pct + '%"></i></div></div>';
}

function _moneyMicros(value) {
  return '$' + (Math.max(0, Number(value) || 0) / 1000000).toFixed(4);
}

function _moneyMetric(label, metric) {
  const used = Math.max(0, Number(metric?.used) || 0);
  const limit = Math.max(0, Number(metric?.limit) || 0);
  const pct = limit > 0 ? Math.min(100, Math.round(100 * used / limit)) : 0;
  return '<div class="anon-control-metric"><div><span>' + _esc(label) + '</span><strong>'
    + _moneyMicros(used) + (limit > 0 ? ' / ' + _moneyMicros(limit) : '')
    + '</strong></div><div class="anon-control-meter"><i style="width:' + pct + '%"></i></div></div>';
}

function _metricPercent(metric) {
  const limit = Number(metric?.limit || 0);
  return limit > 0 ? Math.min(100, 100 * Number(metric?.used || 0) / limit) : 0;
}

function _anonAllowancePercent(row, control) {
  const guard = control?.users?.[row.user_id] || {};
  return Math.max(0, ...[
    guard.estimated_tokens, guard.estimated_cost_microusd,
    guard.actual_tokens, guard.actual_cost_microusd,
    guard.network_estimated_tokens, guard.network_estimated_cost_microusd,
    guard.network_actual_tokens, guard.network_actual_cost_microusd,
  ].map(_metricPercent));
}

function _buildAnonymousControl(data) {
  const sec = document.createElement('section');
  sec.className = 'members-policy anon-control';
  if (!data) {
    sec.innerHTML = '<div class="members-policy-title">Anonymous access controls</div>'
      + '<div class="members-empty">Control telemetry is unavailable.</div>';
    return sec;
  }
  const s = data.settings || {};
  const policy = data.policy || {};
  const autoClose = data.auto_close || {};
  const usage = data.global_usage || {};
  const stateClass = !policy.enabled || autoClose.active ? ' danger' : '';
  const stateText = !policy.enabled ? 'Manually disabled' : (autoClose.active ? 'Automatically paused' : 'Accepting anonymous chat');
  sec.innerHTML = '<div class="anon-control-head"><div><div class="members-policy-title">Anonymous access controls</div>'
    + '<div class="anon-control-state' + stateClass + '">' + _esc(stateText) + '</div></div>'
    + '<div class="anon-control-actions"><button type="button" data-anon-action="refresh">Refresh</button>'
    + (autoClose.active ? '<button type="button" class="danger" data-anon-action="resume">Resume anonymous access</button>' : '')
    + '</div></div>'
    + (autoClose.active ? '<div class="anon-control-alert">Paused for ' + _esc(autoClose.reason || 'safety budget')
      + ' · retries in ' + Number(autoClose.remaining_seconds || 0).toLocaleString() + 's</div>' : '')
    + '<div class="anon-control-metrics">'
    + _pctMetric('Estimated tokens this window', usage.estimated_tokens)
    + _moneyMetric('Estimated cost this window', usage.estimated_cost_microusd)
    + _pctMetric('Actual tokens observed', usage.actual_tokens)
    + _moneyMetric('Actual provider cost', usage.actual_cost_microusd)
    + '</div>'
    + '<form class="anon-control-form"><label class="anon-switch"><input type="checkbox" name="anonymous_chat_enabled" '
    + (policy.enabled ? 'checked' : '') + '><span>Anonymous chat enabled</span></label>'
    + '<label class="anon-switch"><input type="checkbox" name="anon_auto_close_enabled" '
    + (s.auto_close_enabled ? 'checked' : '') + '><span>Automatic temporary closure</span></label>'
    + '<div class="anon-control-grid">'
    + _anonNumber('Concurrent model runs', 'anon_max_concurrent_runs', s.max_concurrent_runs, 0)
    + _anonNumber('Guest/global budget window (seconds)', 'anon_spend_window', s.spend_window, 60)
    + _anonNumber('Guest token budget', 'anon_token_user_max', s.token_user_max, 0)
    + _anonNumber('Network token budget', 'anon_token_source_max', s.token_source_max, 0)
    + _anonNumber('Global token budget', 'anon_token_global_max', s.token_global_max, 0)
    + _anonMoney('Guest cost budget', 'anon_cost_user_microusd_max', s.cost_user_microusd_max)
    + _anonMoney('Network cost budget (lifetime)', 'anon_cost_source_microusd_max', s.cost_source_microusd_max)
    + _anonMoney('Global cost budget', 'anon_cost_global_microusd_max', s.cost_global_microusd_max)
    + _anonNumber('Reserved output tokens', 'anon_estimated_output_tokens', s.estimated_output_tokens, 0)
    + _anonNumber('Estimate μ$ / 1K tokens', 'anon_estimated_cost_per_1k_microusd', s.estimated_cost_per_1k_microusd, 0)
    + _anonNumber('Delay risk score', 'anon_risk_delay_score', s.risk_delay_score, 0)
    + _anonNumber('Cooldown risk score', 'anon_risk_cooldown_score', s.risk_cooldown_score, 0)
    + _anonNumber('Progressive delay (ms)', 'anon_risk_delay_ms', s.risk_delay_ms, 0)
    + _anonNumber('Cooldown (seconds)', 'anon_risk_cooldown_seconds', s.risk_cooldown_seconds, 1)
    + _anonNumber('Auto-close (seconds)', 'anon_auto_close_seconds', s.auto_close_seconds, 60)
    + _anonNumber('Model errors before pause', 'anon_error_max', s.error_max, 0)
    + _anonNumber('Error window (seconds)', 'anon_error_window', s.error_window, 1)
    + '</div><div class="anon-control-foot"><span>' + Number(data.active_runs || 0) + ' active anonymous model run(s)</span>'
    + '<button type="submit">Save anonymous controls</button></div>'
    + '<div class="members-notice">Anonymous identities may be separate across browsers, but every guest on one coarse network shares one lifetime cost allowance. At the default $0.25 network credit limit, anonymous model work becomes permanently unavailable for that network after the allowance is used; it does not renew. Registered accounts continue on their own credits and tier. Network, browser and behavior signals indicate risk—not identity, so shared offices, schools, CGNAT and public Wi-Fi can create false positives.</div></form>'
    + _anonymousEvents(data.recent_events || []);

  sec.querySelector('[data-anon-action="refresh"]')?.addEventListener('click', _load);
  sec.querySelector('[data-anon-action="resume"]')?.addEventListener('click', async (ev) => {
    if (!isAdmin()) { showRestrictedModal(); return; }
    ev.currentTarget.disabled = true;
    const res = await _fetch(apiPath('/admin/users/anonymous-control/auto-close/clear'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: _uid() }),
    });
    if (!res.ok) alert('Could not resume anonymous access.');
    _load();
  });
  sec.querySelector('form')?.addEventListener('submit', _saveAnonymousControls);
  return sec;
}

function _anonNumber(label, name, value, min) {
  return '<label>' + _esc(label) + '<input type="number" name="' + name + '" min="' + min
    + '" step="1" value="' + Math.max(min, Number(value) || 0) + '"></label>';
}

function _anonMoney(label, name, micros) {
  return '<label>' + _esc(label) + '<span class="anon-money"><span>$</span><input type="number" data-micros name="'
    + name + '" min="0" step="0.0001" value="' + (Math.max(0, Number(micros) || 0) / 1000000).toFixed(4) + '"></span></label>';
}

function _anonymousEvents(events) {
  if (!events.length) return '<div class="anon-events"><strong>Recent decisions</strong><span>No blocks or delays recorded.</span></div>';
  return '<details class="anon-events"><summary>Recent decisions (' + events.length + ')</summary><div>'
    + events.slice(0, 12).map(e => '<span><b>' + _esc(e.event_type || 'event') + '</b> '
      + _esc(e.detail || '') + (e.score ? ' · score ' + Number(e.score) : '') + '</span>').join('') + '</div></details>';
}

async function _saveAnonymousControls(ev) {
  ev.preventDefault();
  if (!isAdmin()) { showRestrictedModal(); return; }
  const form = ev.currentTarget;
  const body = {};
  for (const input of form.querySelectorAll('input[name]')) {
    if (input.type === 'checkbox') body[input.name] = input.checked;
    else body[input.name] = input.hasAttribute('data-micros')
      ? Math.round(Math.max(0, Number(input.value) || 0) * 1000000)
      : Math.max(Number(input.min || 0), Math.round(Number(input.value) || 0));
  }
  const save = form.querySelector('button[type="submit"]'); save.disabled = true;
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST', cache: 'no-store', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) throw new Error(await res.text() || ('HTTP ' + res.status));
    await _load();
  } catch (e) {
    alert('Failed to save anonymous controls: ' + e.message); save.disabled = false;
  }
}

// ── Access policy card (the app's access mode) ────────────────────────────
// Mirrors the agents Members tab's policy control: radio cards that auto-save.
// Private = admin approval (pending users appear in the Users table below),
// Open Registration = anyone can join + sign in. Same backend the old
// Data Settings → App Access form used (GET/POST /admin/settings/app).
// ── Admins / Users section tables ─────────────────────────────────────────
// Mirror _buildMembersSection in tab-members.js, app-wide: User | Sessions |
// Messages | Last login | Billing, plus Status + actions for members (Authorize /
// Restrict / Make admin / Reject) and Remove admin for admins.
function _buildUsersSection(title, rows, kind) {
  const sec = document.createElement('div'); sec.className = 'members-section';
  const header = document.createElement('div'); header.className = 'members-section-header';
  const pending = rows.filter(r => !r.is_approved).length;
  header.innerHTML = '<span class="members-section-title">' + _esc(title) + '</span>'
    + '<span class="members-section-count">' + rows.length + '</span>'
    + (pending ? '<span class="members-status pending">' + pending + ' pending</span>' : '');
  sec.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'members-empty';
    empty.textContent = kind === 'admin' ? 'No admins on this instance yet.' : 'No registered users yet.';
    sec.appendChild(empty); return sec;
  }
  const showStatus = kind === 'member';
  const wrap = document.createElement('div'); wrap.className = 'members-table-wrap';
  const table = document.createElement('table'); table.className = 'members-table';
  table.innerHTML = '<thead><tr><th>User</th><th>Experience</th><th class="members-num">Sessions</th><th class="members-num">Messages</th><th>Last login</th><th>Credits</th>'
    + (showStatus ? '<th>Status</th>' : '') + '<th></th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector('tbody');
  const me = _uid();
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || r.username || r.user_id;
    const subId = r.username && r.username !== name ? r.username : r.user_id;
    const last = r.last_login_at ? _fmtDate(r.last_login_at) : '—';
    const isSelf = r.user_id === me;
    let statusHtml = '', actionHtml = '';
    if (showStatus) {
      const isAuth = !!r.is_approved;
      statusHtml = '<td><span class="members-status ' + (isAuth ? 'ok' : 'pending') + '">' + (isAuth ? 'Authorized' : 'Pending') + '</span></td>';
      actionHtml = '<td class="members-actions">'
        + (isAuth
          ? '<button type="button" class="members-btn restrict" data-act="restrict" data-uid="' + _esc(r.user_id) + '"' + (isSelf ? ' disabled title="You cannot restrict your own account"' : '') + '>Restrict</button>'
            + '<button type="button" class="members-btn make-admin" data-act="make-admin" data-uid="' + _esc(r.user_id) + '">Make admin</button>'
          : '<button type="button" class="members-btn authorize" data-act="authorize" data-uid="' + _esc(r.user_id) + '">Authorize</button>'
            + '<button type="button" class="members-btn reject" data-act="reject" data-uid="' + _esc(r.user_id) + '" data-name="' + _esc(name) + '">Reject</button>')
        + '</td>';
    } else if (!isSelf && r.username !== 'admin') {
      actionHtml = '<td class="members-actions"><button type="button" class="members-btn restrict" data-act="remove-admin" data-uid="' + _esc(r.user_id) + '">Remove admin</button></td>';
    } else {
      actionHtml = '<td class="members-actions"></td>';
    }
    const billingHtml = _billingCell(r, name);
    tr.innerHTML = '<td><div class="members-user-name">' + _esc(name) + '</div><div class="members-user-sub">' + _esc(subId) + '</div></td>'
      + _tierCell(r, name)
      + '<td class="members-num">' + (r.session_count ?? 0) + '</td>'
      + '<td class="members-num">' + (r.interaction_count ?? 0) + '</td>'
      + '<td>' + _esc(last) + '</td>' + billingHtml + statusHtml + actionHtml;
    tbody.appendChild(tr);
  }
  tbody.addEventListener('click', async (ev) => {
    const creditBtn = ev.target.closest('button.members-credit');
    if (creditBtn && !creditBtn.disabled) {
      if (!isAdmin()) { showRestrictedModal(); return; }
      _openCreditPopover(creditBtn);
      return;
    }
    const tierBtn = ev.target.closest('button.members-tier-edit');
    if (tierBtn && !tierBtn.disabled) {
      if (!isAdmin()) { showRestrictedModal(); return; }
      _openTierPopover(tierBtn);
      return;
    }
    const btn = ev.target.closest('button.members-btn');
    if (!btn || btn.disabled) return;
    const act = btn.dataset.act;
    const uid = btn.dataset.uid;
    if (!isAdmin()) { showRestrictedModal(); return; }
    if (act === 'reject' && !confirm('Reject and permanently delete "' + (btn.dataset.name || uid) + '"? This cannot be undone.')) return;
    btn.disabled = true;
    try {
      let res;
      if (act === 'authorize' || act === 'restrict') {
        const route = act === 'authorize' ? 'approve' : 'revoke';
        res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(uid) + '/' + route), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ requesting_user_id: _uid() }),
        });
      } else if (act === 'make-admin' || act === 'remove-admin') {
        res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(uid) + '/set-admin'), {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ requesting_user_id: _uid(), is_admin: act === 'make-admin' }),
        });
      } else if (act === 'reject') {
        res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(uid) + '?requesting_user_id=' + encodeURIComponent(_uid())), { method: 'DELETE' });
      }
      if (!res || !res.ok) throw new Error(((res && await res.text()) || 'request failed') || 'HTTP error');
      _load();
    } catch (e) { alert('Action failed: ' + e.message); btn.disabled = false; }
  });
  wrap.appendChild(table); sec.appendChild(wrap); return sec;
}

function _tierCell(r, name, editable = true) {
  const slug = r.tier_slug || r.tier_id || 'free';
  const tierName = r.tier_name || slug;
  const source = r.tier_source || 'default';
  const expiry = r.tier_expires_at ? _fmtDate(r.tier_expires_at) : '';
  const detail = source + (expiry ? ' · until ' + expiry : ' · no expiry');
  const disabled = !_publishedTiers.length ? ' disabled title="No published tiers are available"' : '';
  return '<td><div class="members-tier">'
    + '<span class="members-tier-badge tier-' + _esc(slug) + '">' + _esc(tierName) + '</span>'
    + '<span class="members-tier-detail">' + _esc(detail) + '</span>'
    + (editable ? '<button type="button" class="members-tier-edit" data-uid="' + _esc(r.user_id) + '"'
    + ' data-name="' + _esc(name) + '" data-tier-id="' + _esc(r.tier_id || '') + '"'
    + disabled + '>Assign</button>' : '') + '</div></td>';
}

function _closeTierPopover() {
  if (_tierPopoverCleanup) _tierPopoverCleanup();
  _tierPopoverCleanup = null;
  if (_tierPopover) _tierPopover.remove();
  _tierPopover = null;
  document.querySelectorAll('.members-tier-edit[aria-expanded="true"]').forEach(btn => btn.setAttribute('aria-expanded', 'false'));
}

function _openTierPopover(btn) {
  _closeCreditPopover();
  _closeTierPopover();
  const popover = document.createElement('div');
  popover.className = 'members-credit-popover members-tier-popover';
  popover.setAttribute('role', 'dialog');
  popover.innerHTML = '<form class="members-credit-form">'
    + '<div class="members-credit-popover-head"><div><div class="members-credit-popover-title">Assign experience tier</div>'
    + '<div class="members-credit-popover-user"></div></div>'
    + '<button type="button" class="members-credit-close" aria-label="Close">×</button></div>'
    + '<label class="members-credit-label">Tier<select class="members-tier-select" required></select></label>'
    + '<label class="members-credit-label">Reason<input class="members-credit-input members-tier-reason" maxlength="1000" required placeholder="Why is this tier changing?"></label>'
    + '<label class="members-credit-label">Expires (optional)<input class="members-credit-input members-tier-expiry" type="datetime-local"></label>'
    + '<div class="members-credit-help">Pending keeps the account awaiting approval; any other tier authorizes it. Admin access remains a separate role.</div>'
    + '<div class="members-tier-history" aria-live="polite"><div class="members-tier-history-title">Assignment history</div>'
    + '<div class="members-tier-history-list">Loading…</div></div>'
    + '<div class="members-credit-error" role="alert" hidden></div>'
    + '<div class="members-credit-popover-actions"><button type="button" class="members-credit-cancel">Cancel</button>'
    + '<button type="submit" class="members-credit-save">Assign tier</button></div></form>';
  popover.querySelector('.members-credit-popover-user').textContent = btn.dataset.name || btn.dataset.uid;
  const select = popover.querySelector('.members-tier-select');
  for (const tier of _publishedTiers) {
    const option = document.createElement('option');
    option.value = tier.id;
    option.textContent = tier.name || tier.slug || tier.id;
    option.selected = tier.id === btn.dataset.tierId;
    select.appendChild(option);
  }
  document.body.appendChild(popover);
  _loadTierHistory(popover, btn.dataset.uid);
  _tierPopover = popover;
  btn.setAttribute('aria-expanded', 'true');
  const rect = btn.getBoundingClientRect();
  const margin = 10;
  popover.style.left = Math.min(window.innerWidth - popover.offsetWidth - margin, Math.max(margin, rect.left)) + 'px';
  const below = rect.bottom + 6;
  popover.style.top = (below + popover.offsetHeight <= window.innerHeight - margin
    ? below : Math.max(margin, rect.top - popover.offsetHeight - 6)) + 'px';
  const onOutside = (ev) => { if (!popover.contains(ev.target) && ev.target !== btn) _closeTierPopover(); };
  const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); _closeTierPopover(); btn.focus(); } };
  const onViewport = () => _closeTierPopover();
  document.addEventListener('pointerdown', onOutside);
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', onViewport);
  window.addEventListener('scroll', onViewport, true);
  _tierPopoverCleanup = () => {
    document.removeEventListener('pointerdown', onOutside);
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', onViewport);
    window.removeEventListener('scroll', onViewport, true);
  };
  popover.querySelector('.members-credit-close').addEventListener('click', _closeTierPopover);
  popover.querySelector('.members-credit-cancel').addEventListener('click', _closeTierPopover);
  popover.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const reason = popover.querySelector('.members-tier-reason').value.trim();
    const expiry = popover.querySelector('.members-tier-expiry').value;
    const error = popover.querySelector('.members-credit-error');
    const save = popover.querySelector('.members-credit-save');
    if (!reason) { error.textContent = 'A reason is required.'; error.hidden = false; return; }
    save.disabled = true; error.hidden = true;
    try {
      const res = await _fetch(apiPath('/admin/entitlements/users/' + encodeURIComponent(btn.dataset.uid) + '/tier'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: _uid(), tier_id: select.value, reason,
          expires_at: expiry ? new Date(expiry).toISOString() : null }),
      });
      if (!res.ok) {
        let detail = await res.text();
        try { detail = JSON.parse(detail).detail || detail; } catch {}
        throw new Error(detail || ('HTTP ' + res.status));
      }
      _closeTierPopover();
      await _load();
    } catch (e) {
      error.textContent = e.message || 'Tier assignment failed.';
      error.hidden = false; save.disabled = false;
    }
  });
  popover.querySelector('.members-tier-reason').focus();
}

async function _loadTierHistory(popover, userId) {
  const host = popover.querySelector('.members-tier-history-list');
  if (!host) return;
  try {
    const url = apiPath('/admin/entitlements/assignments?requesting_user_id='
      + encodeURIComponent(_uid()) + '&user_id=' + encodeURIComponent(userId));
    const res = await _fetch(url, { cache: 'no-store' });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    const rows = (await res.json()).assignments || [];
    if (!rows.length) { host.textContent = 'No explicit assignments. Free/default policy applies.'; return; }
    host.innerHTML = rows.map(row => {
      const dates = (row.starts_at ? _fmtDate(row.starts_at) : 'immediate')
        + ' → ' + (row.expires_at ? _fmtDate(row.expires_at) : 'no expiry');
      return '<div class="members-tier-history-row"><strong>' + _esc(row.tier_id || 'unknown') + '</strong>'
        + '<span>' + _esc(row.source || 'unknown') + ' · ' + _esc(dates) + '</span>'
        + (row.reason ? '<span>' + _esc(row.reason) + '</span>' : '')
        + (row.assigned_by ? '<span>by ' + _esc(row.assigned_by) + '</span>' : '') + '</div>';
    }).join('');
  } catch (e) {
    host.textContent = 'History unavailable.';
  }
}

function _billingCell(r, name) {
  const status = r.billing_status || 'none';
  const labels = { trial: 'Trial', paid: 'Paid', exempt: 'Exempt', none: 'No credits' };
  const paid = Math.max(0, Number(r.paid_credits) || 0);
  const trial = Math.max(0, Number(r.trial_credits) || 0);
  const uid = _esc(r.user_id);
  const safeName = _esc(name);
  const creditButton = (type, amount, label, title) => '<button type="button" class="members-credit"'
    + ' data-credit-type="' + type + '" data-current="' + amount + '" data-uid="' + uid + '" data-name="' + safeName + '"'
    + ' aria-haspopup="dialog" title="' + _esc(title) + '">' + amount.toLocaleString() + ' ' + label + '</button>';
  let credits = '';
  if (r.has_trial_grant) {
    credits += creditButton('trial', trial, 'trial credits', 'Click to set the total remaining credits across current trial grants');
  }
  credits += creditButton('paid', paid, 'paid credits', 'Click to set the purchased-credit wallet balance');
  const hold = Math.max(0, Number(r.paid_hold_credits) || 0);
  if (hold) credits += '<span class="members-credit-hold" title="Reserved by active runs">' + hold.toLocaleString() + ' reserved</span>';
  return '<td><div class="members-billing">'
    + '<span class="members-billing-status ' + _esc(status) + '">' + _esc(labels[status] || labels.none) + '</span>'
    + '<div class="members-credit-list">' + credits + '</div>'
    + '</div></td>';
}

function _buildAnonymousSection(rows, control) {
  const sec = document.createElement('div'); sec.className = 'members-section';
  const header = document.createElement('div'); header.className = 'members-section-header';
  header.innerHTML = '<span class="members-section-title">Anonymous users</span>'
    + '<span class="members-section-count">' + rows.length + '</span>';
  sec.appendChild(header);
  if (!rows.length) {
    const empty = document.createElement('div'); empty.className = 'members-empty';
    empty.textContent = 'No anonymous user accounts have been created yet.';
    sec.appendChild(empty); return sec;
  }
  rows = [...rows].sort((a, b) => {
    const proximity = _anonAllowancePercent(b, control) - _anonAllowancePercent(a, control);
    if (Math.abs(proximity) > 0.001) return proximity;
    return String(b.last_seen_at || '').localeCompare(String(a.last_seen_at || ''));
  });
  const wrap = document.createElement('div'); wrap.className = 'members-table-wrap';
  const table = document.createElement('table'); table.className = 'members-table members-anonymous-table';
  table.innerHTML = '<thead><tr><th>Anonymous user</th><th>Channel</th><th>Experience</th><th class="members-num">Central sessions</th>'
    + '<th class="members-num">Central messages</th><th class="members-num">Tokens</th><th>Cost</th><th>Activity</th><th>Status</th><th>Limits</th><th>Billing</th><th></th></tr></thead><tbody></tbody>';
  const tbody = table.querySelector('tbody');
  for (const r of rows) {
    const tr = document.createElement('tr');
    const name = r.display_name || 'Anonymous user';
    const channels = (r.channels || []).join(', ') || 'Unknown';
    const tiers = (r.identity_tiers || []).map(t => t.replaceAll('_', ' ')).join(', ');
    const externalIds = (r.external_ids || []).join(', ');
    const lastActivity = r.anonymous_last_activity_at || r.last_seen_at;
    const lastSeen = lastActivity ? _fmtDate(lastActivity) : '—';
    const hasCentralMetrics = !!r.central_metrics_available;
    const guard = control?.users?.[r.user_id] || {};
    const cooldown = guard.cooldown || {};
    const tokenMetric = guard.estimated_tokens || {};
    const costMetric = guard.estimated_cost_microusd || {};
    const actualTokens = guard.actual_tokens || { used: r.anonymous_actual_tokens || 0 };
    const actualCost = guard.actual_cost_microusd || { used: r.anonymous_actual_cost_microusd || 0 };
    const allowancePct = Math.round(_anonAllowancePercent(r, control));
    const networkCost = guard.network_estimated_cost_microusd || {};
    const networkActualCost = guard.network_actual_cost_microusd || {};
    const tokensHtml = '<td class="members-num">' + Number(actualTokens.used || 0).toLocaleString() + '</td>';
    const costHtml = '<td>' + _moneyMicros(actualCost.used) + '</td>';
    const activityHtml = '<td>' + _esc(lastSeen) + '</td>';
    const statusHtml = '<td><div class="anon-user-guard">'
      + '<span class="members-status ' + (cooldown.active ? 'pending' : 'ok') + '">'
      + (cooldown.active ? 'Cooldown' : 'Allowed') + '</span>'
      + (cooldown.active ? '<small>' + Number(cooldown.remaining_seconds || 0).toLocaleString() + 's remaining</small>' : '')
      + '</div></td>';
    const limitsHtml = '<td><div class="anon-user-guard">'
      + '<strong>' + allowancePct + '% near limit</strong>'
      + '<small>Guest est: ' + Number(tokenMetric.used || 0).toLocaleString() + ' / '
      + Number(tokenMetric.limit || 0).toLocaleString() + ' tokens</small>'
      + '<small>Guest est cost: ' + _moneyMicros(costMetric.used) + ' / ' + _moneyMicros(costMetric.limit) + '</small>'
      + '<small>Network est: ' + _moneyMicros(networkCost.used) + ' / ' + _moneyMicros(networkCost.limit) + '</small>'
      + '<small>Network actual: ' + _moneyMicros(networkActualCost.used) + ' / ' + _moneyMicros(networkActualCost.limit) + '</small>'
      + '</div></td>';
    const actionHtml = '<td class="members-actions">'
      + (cooldown.active
        ? '<button type="button" class="members-btn authorize" data-anon-user-action="restore" data-uid="' + _esc(r.user_id) + '">Restore</button>'
        : '<button type="button" class="members-btn restrict" data-anon-user-action="cooldown" data-uid="' + _esc(r.user_id) + '">Cooldown</button>')
      + '</td>';
    tr.innerHTML = '<td><div class="members-user-name">' + _esc(name) + '</div>'
      + '<div class="members-user-sub">' + _esc(r.user_id) + '</div></td>'
      + '<td><div class="members-channel">' + _esc(channels) + '</div>'
      + (tiers ? '<div class="members-anon-tier">' + _esc(tiers) + '</div>' : '')
      + (externalIds ? '<div class="members-user-sub" title="' + _esc(externalIds) + '">' + _esc(externalIds) + '</div>' : '') + '</td>'
      + _tierCell(r, name, false)
      + '<td class="members-num">' + (hasCentralMetrics ? Number(r.session_count || 0).toLocaleString() : '—') + '</td>'
      + '<td class="members-num">' + (hasCentralMetrics ? Number(r.interaction_count || 0).toLocaleString() : '—') + '</td>'
      + tokensHtml + costHtml + activityHtml + statusHtml + limitsHtml + _billingCell(r, name) + actionHtml;
    tbody.appendChild(tr);
  }
  tbody.addEventListener('click', async (ev) => {
    const anonAction = ev.target.closest('button[data-anon-user-action]');
    if (anonAction) {
      if (!isAdmin()) { showRestrictedModal(); return; }
      const action = anonAction.dataset.anonUserAction;
      const uid = anonAction.dataset.uid;
      let seconds = 900; let reason = 'Manual admin cooldown';
      if (action === 'cooldown') {
        const raw = prompt('Cooldown seconds for this anonymous identity and its network:', '900');
        if (raw === null) return;
        seconds = Math.max(1, Math.min(86400, Math.round(Number(raw) || 900)));
        reason = prompt('Reason:', reason) || reason;
      }
      anonAction.disabled = true;
      const res = await _fetch(apiPath('/admin/users/anonymous-control/' + encodeURIComponent(uid) + '/'
        + (action === 'restore' ? 'restore' : 'cooldown')), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: _uid(), ...(action === 'cooldown' ? { seconds, reason } : {}) }),
      });
      if (!res.ok) alert('Anonymous user action failed: ' + await res.text());
      _load(); return;
    }
    const creditBtn = ev.target.closest('button.members-credit');
    if (!creditBtn || creditBtn.disabled) return;
    if (!isAdmin()) { showRestrictedModal(); return; }
    _openCreditPopover(creditBtn);
  });
  wrap.appendChild(table); sec.appendChild(wrap); return sec;
}

function _closeCreditPopover() {
  if (_creditPopoverCleanup) _creditPopoverCleanup();
  _creditPopoverCleanup = null;
  if (_creditPopover) _creditPopover.remove();
  _creditPopover = null;
  document.querySelectorAll('.members-credit[aria-expanded="true"]').forEach(btn => btn.setAttribute('aria-expanded', 'false'));
}

function _openCreditPopover(btn) {
  if (_creditPopover && _creditPopover.dataset.uid === btn.dataset.uid
      && _creditPopover.dataset.creditType === btn.dataset.creditType) {
    _closeCreditPopover();
    return;
  }
  _closeCreditPopover();
  const type = btn.dataset.creditType;
  const current = Number(btn.dataset.current) || 0;
  const name = btn.dataset.name || btn.dataset.uid;
  const scope = type === 'trial' ? 'total remaining trial credits' : 'paid credit balance';
  const popover = document.createElement('div');
  popover.className = 'members-credit-popover';
  popover.dataset.uid = btn.dataset.uid;
  popover.dataset.creditType = type;
  popover.setAttribute('role', 'dialog');
  popover.setAttribute('aria-modal', 'false');
  popover.innerHTML = '<form class="members-credit-form">'
    + '<div class="members-credit-popover-head"><div><div class="members-credit-popover-title"></div>'
    + '<div class="members-credit-popover-user"></div></div>'
    + '<button type="button" class="members-credit-close" aria-label="Close">×</button></div>'
    + '<label class="members-credit-label">Credits<input class="members-credit-input" type="number" min="0" step="1" required></label>'
    + '<div class="members-credit-help">1 credit equals 1 cent of usage.</div>'
    + '<div class="members-credit-error" role="alert" hidden></div>'
    + '<div class="members-credit-popover-actions"><button type="button" class="members-credit-cancel">Cancel</button>'
    + '<button type="submit" class="members-credit-save">Save balance</button></div></form>';
  popover.querySelector('.members-credit-popover-title').textContent = type === 'trial' ? 'Adjust trial credits' : 'Adjust paid credits';
  popover.querySelector('.members-credit-popover-user').textContent = name + ' · ' + scope;
  const input = popover.querySelector('.members-credit-input');
  input.value = String(current);
  document.body.appendChild(popover);
  _creditPopover = popover;
  btn.setAttribute('aria-expanded', 'true');

  const rect = btn.getBoundingClientRect();
  const margin = 10;
  const left = Math.min(window.innerWidth - popover.offsetWidth - margin, Math.max(margin, rect.left));
  const below = rect.bottom + 6;
  const top = below + popover.offsetHeight <= window.innerHeight - margin
    ? below : Math.max(margin, rect.top - popover.offsetHeight - 6);
  popover.style.left = left + 'px';
  popover.style.top = top + 'px';

  const onOutside = (ev) => { if (!popover.contains(ev.target) && ev.target !== btn) _closeCreditPopover(); };
  const onKey = (ev) => { if (ev.key === 'Escape') { ev.preventDefault(); _closeCreditPopover(); btn.focus(); } };
  const onViewport = () => _closeCreditPopover();
  document.addEventListener('pointerdown', onOutside);
  document.addEventListener('keydown', onKey);
  window.addEventListener('resize', onViewport);
  window.addEventListener('scroll', onViewport, true);
  _creditPopoverCleanup = () => {
    document.removeEventListener('pointerdown', onOutside);
    document.removeEventListener('keydown', onKey);
    window.removeEventListener('resize', onViewport);
    window.removeEventListener('scroll', onViewport, true);
  };
  popover.querySelector('.members-credit-close').addEventListener('click', _closeCreditPopover);
  popover.querySelector('.members-credit-cancel').addEventListener('click', _closeCreditPopover);
  popover.querySelector('form').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    await _saveCreditAdjustment(btn, popover, input.value.trim());
  });
  input.focus(); input.select();
}

async function _saveCreditAdjustment(btn, popover, value) {
  const type = btn.dataset.creditType;
  const error = popover.querySelector('.members-credit-error');
  if (!/^\d+$/.test(value) || !Number.isSafeInteger(Number(value))) {
    error.textContent = 'Enter a non-negative whole number of credits.';
    error.hidden = false;
    return;
  }
  const save = popover.querySelector('.members-credit-save');
  const input = popover.querySelector('.members-credit-input');
  save.disabled = true; input.disabled = true; error.hidden = true;
  try {
    const res = await _fetch(apiPath('/admin/users/' + encodeURIComponent(btn.dataset.uid) + '/credits'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        requesting_user_id: _uid(),
        credit_type: type,
        balance_credits: Number(value),
      }),
    });
    if (!res.ok) {
      let detail = await res.text();
      try { detail = JSON.parse(detail).detail || detail; } catch {}
      throw new Error(detail || ('HTTP ' + res.status));
    }
    _closeCreditPopover();
    await _load();
  } catch (e) {
    error.textContent = e.message || 'Credit adjustment failed.';
    error.hidden = false;
    save.disabled = false; input.disabled = false; input.focus();
  }
}

// Loading skeleton — echoes the access-policy card + two member sections so the
// tab has structure while the stats fetch resolves (shared .sk-shimmer in app3.css).
function _usersSkeleton() {
  const dot = '<span class="mem-sk sk-shimmer" style="width:14px;height:14px;border-radius:50%;flex:none;"></span>';
  const opt = (w) => '<div class="mem-sk-opt">' + dot + '<span class="mem-sk sk-shimmer" style="width:' + w + ';"></span></div>';
  const row = (w) => '<div class="mem-sk-row"><span class="mem-sk sk-shimmer" style="width:' + w + ';"></span></div>';
  const section = (titleW, rows) => '<div class="mem-sk-section">'
      + '<span class="mem-sk sk-shimmer mem-sk-head" style="width:' + titleW + ';"></span>'
      + rows.map(row).join('')
    + '</div>';
  return '<div class="members-skeleton" aria-hidden="true">'
    + '<div class="mem-sk-policy">'
    +   '<span class="mem-sk sk-shimmer mem-sk-head" style="width:90px;"></span>'
    +   opt('62%') + opt('54%')
    + '</div>'
    + section('64px', ['78%', '66%'])
    + section('82px', ['84%', '70%', '58%'])
    + section('118px', ['88%', '74%', '62%'])
    + '</div>';
}

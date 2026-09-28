'use strict';

/**
 * Automations Dashboard — table view.
 *
 * Aggregates scheduled tasks, event triggers, active worker spawns, clone
 * agents, and webhooks into ONE flat table that mirrors the Sessions table
 * (ui/main-panel/agents/sessions/js/sessions-page.js): check · agent · automation ·
 * status on the left, then automation-specific columns (type · trigger · last ·
 * next · count · enabled). Markup: ../automations.html · styles: ../automations.css.
 *
 * The Automations main-panel tab was folded into the Agents page (the All Agents
 * card's Automations tab, see ui/main-panel/agents/agents.html #automations-section);
 * this engine is started by the card's tab handler (js/view.js _renderAllAgentsCard)
 * instead of tabs.js.
 *
 * Lazy init: first call to startAutomations() does the work; subsequent calls
 * re-render with fresh data.
 */

import { app } from '../../../../shared/js/state.js';
import { apiPath } from '../../../../shared/js/config.js';
import { _esc, _fmtTime, _statusBadge, _typeIcon, _enabledToggle } from '../../../../shared/js/dom-utils.js';
import { icon, claudeMark, codexMark } from '../../../../shared/js/icons.js';
import { ICON_PICKER_ICONS } from '../../../../shared/js/icon-picker.js';
// SHARED-DELETE-CONTROL — two-click trash→hazard→spinner delete, shared with the
// Agents page and chat session menu. State machine lives in delete-control.js.
import { advanceDeleteBtn, resetDeleteBtn } from "../../../../shared/js/delete-control.js";

// ── Helpers ────────────────────────────────────────────────────────

function _qs(id) { return document.getElementById(id); }

/**
 * Render an agent icon from inline fields (Lucide name, engine, emoji).
 * Matches _agentIconHtml in session-agent.js.
 */
function _agentIconHtml(iconName, engine, size) {
  const n = parseFloat(size) || 14;
  const unit = String(size).replace(/[\d.]/g, '') || 'px';
  const big = `${n * 1.5}${unit}`;
  if (engine === 'claude_code' && (!iconName || iconName === 'sparkles')) {
    return claudeMark({ size: big });
  }
  if (engine === 'codex' && (!iconName || iconName === 'code-2')) {
    return codexMark({ size: big });
  }
  if (!iconName) return icon('bot', { size: big });
  if (ICON_PICKER_ICONS.includes(iconName)) return icon(iconName, { size: big });
  return `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${iconName.replace(/</g, '&lt;')}</span>`;
}

/**
 * Find a row in _agentsData by id + type so a delete/toggle can resolve its
 * owning agent_id. Returns { row, agent_id } or null.
 */
function _findRow(id, type) {
  for (const agent of _agentsData) {
    for (const col of ['automations', 'event_subscriptions']) {
      for (const row of agent[col]) {
        if (row.id === id && row.type === type) return { row, agent_id: agent.agent_id };
      }
    }
  }
  for (const w of _webhooksData) {
    if (w.id === id && (w.type === type || type === 'webhook')) return { row: w, agent_id: null };
  }
  return null;
}

/** Show a brief toast that auto-hides (mirrors the Sessions delete toast). */
function _showToast(msg) {
  const wrap = _qs('automations-table-wrap');
  if (!wrap) return;
  const existing = wrap.querySelector('.auto-toast');
  if (existing) existing.remove();
  const toast = document.createElement('div');
  toast.className = 'auto-toast';
  toast.textContent = msg;
  wrap.appendChild(toast);
  setTimeout(() => { toast.classList.add('auto-toast-fade'); }, 1500);
  setTimeout(() => { if (toast.parentNode) toast.parentNode.removeChild(toast); }, 2200);
}

// ── State & wiring guard ───────────────────────────────────────────

let _initialized = false;
let _refreshTimer = null;
const _REFRESH_INTERVAL_MS = 30000;

let _agentsData = [];
let _webhooksData = [];
let _clonesData = [];
let _filterAgentId = '';
// Which partition of the dashboard is showing. Three sibling views:
//   'active'    — in-process / future automations (the clean main list)
//   'completed' — automations that have finished for good (a history view)
//   'bin'       — soft-deleted rows (the recycling bin)
// The Completed view mirrors the bin's enter/back/back-to-active navigation.
let _view = 'active';
let _completedCount = 0;   // finished-automation tally for the Completed button badge

// ── Fetch ──────────────────────────────────────────────────────────

async function _fetchDashboard() {
  const userId = app.currentUserId;
  if (!userId) return null;

  const token = localStorage.getItem('auth_token');
  let url = `/api/v1/automations/dashboard?user_id=${encodeURIComponent(userId)}&view=${_view}`;
  if (_filterAgentId) url += `&agent_id=${encodeURIComponent(_filterAgentId)}`;
  if (token) url += `&token=${encodeURIComponent(token)}`;

  try {
    const res = await fetch(apiPath(url));
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return await res.json();
  } catch (e) {
    console.warn('[Automations] Failed to fetch dashboard:', e);
    return null;
  }
}

// ── Flatten into one list of rows ──────────────────────────────────

/**
 * Collapse the per-agent dashboard structure into one flat array of
 * { row, agentName, agentId } entries — the table renders these as <tr>s.
 */
function _flatRows() {
  const out = [];
  for (const agent of _agentsData) {
    const icon = agent.agent_icon || '';
    const engine = agent.agent_engine || '';
    for (const r of agent.automations) out.push({ row: r, agentName: agent.agent_name, agentId: agent.agent_id, agentIcon: icon, agentEngine: engine });
    for (const r of agent.event_subscriptions) out.push({ row: r, agentName: agent.agent_name, agentId: agent.agent_id, agentIcon: icon, agentEngine: engine });
    for (const r of agent.spawns) out.push({ row: r, agentName: agent.agent_name, agentId: agent.agent_id, agentIcon: icon, agentEngine: engine });
  }
  for (const c of _clonesData) out.push({ row: c, agentName: c.name || 'Clone', agentId: c.agent_id || c.id || '', agentIcon: c.icon || '', agentEngine: '' });
  for (const w of _webhooksData) out.push({ row: w, agentName: '', agentId: '', agentIcon: '', agentEngine: '' });
  return out;
}

// ── Render one row ─────────────────────────────────────────────────

// Map a delivery descriptor's kind → a small lucide icon for its badge.
const _OUT_ICON = {
  chat: 'message-circle',
  channel: 'send',
  silent: 'eye-off',
  webhook: 'webhook',
  file: 'file-text',
  email: 'mail',
  other: 'arrow-right',
};

/**
 * Render the Output cell: one badge per place this automation's result is sent
 * (this chat, a new chat, Telegram, a file, silent, …) — the destination the
 * agent set when it created the automation. Email is flagged "(not set up)"
 * because no outbound mail channel exists yet. Rows that don't deliver a
 * message (workers, clones, inbound webhooks) pass no delivery → a dash.
 */
function _deliveryBadges(delivery) {
  if (!Array.isArray(delivery) || delivery.length === 0) return '<span class="auto-tz">—</span>';
  const inner = delivery.map((d) => {
    const kind = d.kind || 'other';
    const ic = _OUT_ICON[kind] || 'arrow-right';
    const unavail = d.available === false;
    const label = unavail ? `${d.label} (not set up)` : (d.label || kind);
    const cls = `auto-out-badge auto-out-${kind}${unavail ? ' auto-out-unavail' : ''}`;
    const title = unavail ? "Email delivery isn't set up yet" : label;
    return `<span class="${cls}" title="${_esc(title)}"><i data-lucide="${ic}" style="width:11px;height:11px;"></i>${_esc(label)}</span>`;
  }).join('');
  return `<span class="auto-out-list">${inner}</span>`;
}

/** Build one <tr> for a row, given its owning agent name/id. */
function _renderRowCells(row, agentName, agentId, agentIcon, agentEngine) {
  let agentLabel = _esc(agentName) || '—';
  let trigger = '—';
  let last = '—';
  let next = '—';
  let count = '—';
  let status = '';
  let error = '';
  let supportsToggle = false;
  let enabled = row.enabled !== false;

  switch (row.type) {
    case 'scheduled':
      trigger = `<code class="auto-cron">${_esc(row.trigger || '—')}</code>`;
      if (row.timezone && row.timezone !== 'UTC') trigger += ` <span class="auto-tz">${_esc(row.timezone)}</span>`;
      last = _fmtTime(row.last_run_at);
      next = _fmtTime(row.next_run_at);
      count = row.run_count || '—';
      status = _statusBadge(row.last_status);
      error = row.last_error ? `<span class="auto-error" title="${_esc(row.last_error)}">Error</span>` : '';
      supportsToggle = true;
      break;

    case 'event':
      trigger = `<span class="auto-event-src">${_esc(row.trigger || '—')}</span>`;
      if (row.filter && Object.keys(row.filter).length > 0) trigger += ` <span class="auto-filter-badge">filter</span>`;
      last = _fmtTime(row.last_event_at);
      count = row.fire_count != null ? String(row.fire_count) : '—';
      status = _statusBadge(row.last_status);
      error = row.last_error ? `<span class="auto-error" title="${_esc(row.last_error)}">Error</span>` : '';
      supportsToggle = true;
      break;

    case 'worker':
      agentLabel = `${_esc(agentName)} <span class="auto-clone-badge">clone</span>`;
      trigger = `<span class="auto-worker-name">${_esc(row.name || row.task || '—')}</span>`;
      last = _fmtTime(row.heartbeat_at);
      status = _statusBadge(row.status);
      error = row.result_summary ? `<span class="auto-error" title="${_esc(row.result_summary)}">summary</span>` : '';
      break;

    case 'webhook':
      agentLabel = '— <span class="auto-tz">user-level</span>';
      trigger = `<span class="auto-webhook-name">${_esc(row.name || '—')}</span>`;
      status = row.active
        ? '<span class="auto-status ok">Active</span>'
        : '<span class="auto-status error">Disabled</span>';
      last = _fmtTime(row.last_triggered_at);
      break;

    case 'clone':
      trigger = `<span class="auto-clone-of">clone of ${_esc(row.clone_of || '?')}</span>`;
      status = _statusBadge(row.status);
      break;
  }

  // In the Completed view every row has finished for good, so surface that
  // directly instead of echoing the last run's status — a one-off reminder that
  // fired should read "Completed", not "OK". A run that ended in failure keeps
  // its error status (and the error badge beside it) so we never mask a failure.
  if (_view === 'completed') {
    const raw = String(row.last_status || row.status || '').toLowerCase();
    const failed = raw === 'error' || raw === 'failed' || raw === 'failure';
    if (!failed) status = '<span class="auto-status completed">Completed</span>';
  }

  const name = row.label || row.name || '—';
  const typeCell = `<span class="auto-type">${_typeIcon(row.type)}${_esc(row.type || '')}</span>`;
  // Only the active dashboard offers a live enable toggle. In the bin every row
  // is dormant, and in the completed view every row has already finished — both
  // show a static dash so toggling can never silently re-arm a row.
  const enabledCell = (supportsToggle && _view === 'active')
    ? _enabledToggle(enabled, row.id, row.type)
    : '<span class="auto-tz">—</span>';

  return `<tr data-id="${_esc(row.id)}" data-type="${_esc(row.type)}" data-agent-id="${_esc(agentId || '')}">
    <td class="col-check"><span class="auto-check-cell" data-id="${_esc(row.id)}"> </span></td>
    <td class="col-agent"><span class="auto-agent-badge">${_agentIconHtml(agentIcon, agentEngine, '12px')}${agentLabel}</span></td>
    <td class="col-name" title="${_esc(name)}">${_esc(name)}</td>
    <td class="col-status">${status}${error}</td>
    <td class="col-type">${typeCell}</td>
    <td class="col-trigger">${trigger}</td>
    <td class="col-output">${_deliveryBadges(row.delivery)}</td>
    <td class="col-last">${last}</td>
    <td class="col-next">${next}</td>
    <td class="col-count">${count}</td>
    <td class="col-device">${_deviceCell(row, agentId)}</td>
    <td class="col-enabled">${enabledCell}</td>
  </tr>`;
}

// ── "Run on" device chip ───────────────────────────────────────────
// Cross-device targeting (app/devices/) is wired for SCHEDULED automations
// only (the runner's firing seam), so other row types show a dash. The chip
// opens the shared DevicePicker; the label is resolved from the device list.
function _deviceCell(row, agentId) {
  if (row.type !== 'scheduled') return '<span class="auto-tz">—</span>';
  const target = row.target_device || '';
  const offline = (row.target_offline === 'skip') ? 'skip' : 'wait';
  const label = target
    ? ((window.DevicePicker && window.DevicePicker.labelFor) ? window.DevicePicker.labelFor(target) : target)
    : 'This device';
  const cls = 'auto-device-chip' + (target ? ' targeting' : '');
  const title = target
    ? `Runs on ${_esc(label)} — click to change`
    : 'Runs on this device — click to send it to another device';
  return `<span class="${cls}" data-id="${_esc(row.id)}" data-agent-id="${_esc(agentId || '')}" data-target="${_esc(target)}" data-offline="${offline}" title="${title}">
    <i data-lucide="monitor" style="width:11px;height:11px;"></i><span class="auto-device-text">${_esc(label)}</span></span>`;
}

// ── Render the table ───────────────────────────────────────────────

function _renderTable() {
  const tbody = _qs('automations-table-body');
  const empty = _qs('automations-empty');
  const loading = _qs('automations-loading');
  if (!tbody) return;

  if (loading) loading.style.display = 'none';

  const rows = _flatRows();
  if (rows.length === 0) {
    tbody.innerHTML = '';
    if (empty) {
      const txt = empty.querySelector('.auto-empty-text');
      const hint = empty.querySelector('.auto-empty-hint');
      if (txt) txt.textContent =
        _view === 'bin' ? 'Recycling bin is empty'
        : _view === 'completed' ? 'No completed automations'
        : 'No automations yet';
      if (hint) hint.textContent =
        _view === 'bin' ? 'Recycled automations appear here until permanently deleted'
        : _view === 'completed' ? 'Finished one-off tasks and worker spawns appear here'
        : 'Create scheduled tasks or event triggers in any agent to see them here';
      empty.style.display = 'flex';
    }
    return;
  }
  if (empty) empty.style.display = 'none';

  tbody.innerHTML = rows.map(r => _renderRowCells(r.row, r.agentName, r.agentId, r.agentIcon, r.agentEngine)).join('');

  // Re-render lucide icons in the new rows
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    window.lucide.createIcons({ nodes: [tbody] });
  }
}

/** Fill the agent filter <select> from the loaded agents. */
function _populateFilter() {
  const sel = _qs('automations-filter-agent');
  if (!sel) return;
  let html = '<option value="">All agents</option>';
  for (const a of _agentsData) {
    const count = a.automations.length + a.event_subscriptions.length + a.spawns.length;
    const isSel = _filterAgentId === a.agent_id ? ' selected' : '';
    html += `<option value="${_esc(a.agent_id)}"${isSel}>${_agentIconHtml(a.agent_icon, a.agent_engine, '12px')} ${_esc(a.agent_name)} (${count})</option>`;
  }
  sel.innerHTML = html;
}

// Fill the table body with phantom "shimmer" rows matching the column layout,
// so the first paint has the table's shape instead of a lone centered spinner.
// Uses the shared skeleton primitives (sk-shimmer in app3.css).
function _renderSkeletonRows(count = 8) {
  const tbody = _qs('automations-table-body');
  const empty = _qs('automations-empty');
  const loading = _qs('automations-loading');
  if (!tbody) return;
  if (empty) empty.style.display = 'none';
  if (loading) loading.style.display = 'none';   // skeleton rows replace the spinner
  const cell = (w) => `<td><span class="auto-sk sk-shimmer" style="width:${w};"></span></td>`;
  let html = '';
  for (let i = 0; i < count; i++) {
    html += `<tr class="auto-skeleton-row" aria-hidden="true">
      <td class="col-check"></td>
      ${cell('70%')}${cell('85%')}${cell('46px')}${cell('50%')}${cell('60%')}${cell('55%')}${cell('55%')}${cell('55%')}${cell('30px')}${cell('60%')}${cell('34px')}
    </tr>`;
  }
  tbody.innerHTML = html;
}

// ── Data loading ───────────────────────────────────────────────────

async function _loadAndRender() {
  const loading = _qs('automations-loading');
  // First load (no data yet) shows skeleton rows; a background auto-refresh
  // (data already present) refreshes in place so it doesn't flash to skeletons.
  const firstLoad = !_agentsData.length && !_webhooksData.length && !_clonesData.length;
  if (firstLoad) _renderSkeletonRows();
  else if (loading) loading.style.display = 'flex';

  const data = await _fetchDashboard();
  if (!data) {
    if (loading) loading.style.display = 'none';
    return;
  }

  _agentsData = data.agents || [];
  _webhooksData = data.webhooks || [];
  _clonesData = data.clones || [];
  // completed_count rides on the active/completed responses (omitted in the bin).
  // Keep the last known value when it's absent so the badge doesn't flicker.
  if (typeof data.completed_count === 'number') _completedCount = data.completed_count;
  _populateFilter();
  _renderTable();
  // Resolve device names for any "Run on" chips that rendered before the
  // device list was cached (best-effort; non-fatal if the fleet API is absent).
  if (window.DevicePicker && window.DevicePicker.load) {
    window.DevicePicker.load().then(() => _refreshDeviceChipLabels()).catch(() => {});
  }
  // Re-render rebuilt every row, so any selection is gone — clear the header
  // toggle and repaint the toolbar (resting recycle icon, back/restore states).
  const checkAll = _qs('automations-check-all');
  if (checkAll) checkAll.classList.remove('checked');
  _updateViewToolbar();
}

// ── Selection helpers ──────────────────────────────────────────────

function _toggleCheck(cell) {
  cell.classList.toggle('checked');
  const tr = cell.closest('tr');
  if (tr) tr.classList.toggle('selected', cell.classList.contains('checked'));
  _resetTrash();          // selection changed → disarm any pending hazard
  _updateViewToolbar();   // repaint the recycle/trash icon + restore state
}

function _setAllChecked(on) {
  document.querySelectorAll('#automations-table .auto-check-cell[data-id]').forEach(c => {
    c.classList.toggle('checked', on);
    const tr = c.closest('tr');
    if (tr) tr.classList.toggle('selected', on);
  });
  const checkAll = _qs('automations-check-all');
  if (checkAll) checkAll.classList.toggle('checked', on);
  _resetTrash();
  _updateViewToolbar();
}

function _selectedRows() {
  return Array.from(document.querySelectorAll('#automations-table tbody tr.selected'));
}

// ── Enable / disable toggle ────────────────────────────────────────

async function _toggleEnabled(input) {
  const id = input.dataset.id;
  const type = input.dataset.type;
  const enabled = input.checked;
  const found = _findRow(id, type);
  if (!found || !found.agent_id) return;

  const token = localStorage.getItem('auth_token');
  let url;
  if (type === 'scheduled') {
    url = `/api/v1/agents/${encodeURIComponent(found.agent_id)}/automations/${encodeURIComponent(id)}`;
  } else if (type === 'event') {
    url = `/api/v1/agents/${encodeURIComponent(found.agent_id)}/event-subscriptions/${encodeURIComponent(id)}`;
  } else {
    return;
  }
  if (token) url += `?token=${encodeURIComponent(token)}`;

  try {
    const res = await fetch(apiPath(url), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId, enabled }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    // Keep local model in sync so a refresh doesn't flip it back.
    found.row.enabled = enabled;
    _showToast(enabled ? 'Enabled' : 'Disabled');
  } catch (e) {
    console.warn('[Automations] Toggle failed:', e);
    input.checked = !enabled;  // revert
    _showToast('Update failed');
  }
}

// ── Run-on device targeting ────────────────────────────────────────

function _openDeviceChip(chip) {
  if (!window.DevicePicker || !window.DevicePicker.open) return;
  window.DevicePicker.open(chip, {
    title: 'Run this automation on',
    currentInstance: chip.dataset.target || '',
    currentOffline: chip.dataset.offline || 'wait',
    showOffline: true,
    onSelect: (sel) => _setAutomationTarget(
      { id: chip.dataset.id, agentId: chip.dataset.agentId }, chip, sel),
  });
}

async function _setAutomationTarget(ref, chip, sel) {
  const target = sel.is_self ? '' : (sel.instance_id || '');
  const offline = (sel.offline === 'skip') ? 'skip' : 'wait';
  const token = localStorage.getItem('auth_token');
  let url = `/api/v1/agents/${encodeURIComponent(ref.agentId)}/automations/${encodeURIComponent(ref.id)}`;
  if (token) url += `?token=${encodeURIComponent(token)}`;
  try {
    const res = await fetch(apiPath(url), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId, target_device: target, target_offline: offline }),
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    // Keep the local model in sync so a re-render doesn't flip it back.
    const found = _findRow(ref.id, 'scheduled');
    if (found && found.row) { found.row.target_device = target; found.row.target_offline = offline; }
    // Repaint this chip in place.
    const label = target
      ? ((window.DevicePicker && window.DevicePicker.labelFor) ? window.DevicePicker.labelFor(target) : target)
      : 'This device';
    chip.dataset.target = target;
    chip.dataset.offline = offline;
    chip.classList.toggle('targeting', !!target);
    const txt = chip.querySelector('.auto-device-text');
    if (txt) txt.textContent = label;
    chip.title = target ? `Runs on ${label} — click to change`
      : 'Runs on this device — click to send it to another device';
    _showToast(target ? `Runs on ${label}` + (offline === 'skip' ? ' (skip if offline)' : '') : 'Runs on this device');
  } catch (e) {
    console.warn('[Automations] Set target failed:', e);
    _showToast('Update failed');
  }
}

// After the device list loads, fill chips that rendered before names resolved.
function _refreshDeviceChipLabels() {
  document.querySelectorAll('#automations-table .auto-device-chip[data-target]').forEach(chip => {
    const t = chip.dataset.target || '';
    if (!t) return;
    const lbl = (window.DevicePicker && window.DevicePicker.labelFor) ? window.DevicePicker.labelFor(t) : t;
    const txt = chip.querySelector('.auto-device-text');
    if (txt) txt.textContent = lbl;
    chip.title = `Runs on ${lbl} — click to change`;
  });
}

// ── Delete selected ────────────────────────────────────────────────

// Per-row-type base path on the automations router (shared by delete/restore).
function _rowBasePath(type, id) {
  if (type === 'scheduled' || type === 'event') return `/api/v1/automations/${encodeURIComponent(id)}`;
  if (type === 'worker') return `/api/v1/automations/spawn/${encodeURIComponent(id)}`;
  if (type === 'clone') return `/api/v1/automations/clone/${encodeURIComponent(id)}`;
  if (type === 'webhook') return `/api/v1/automations/webhook/${encodeURIComponent(id)}`;
  return null;
}

// Delete the selected rows. In the active view this soft-deletes (moves to the
// bin); in the bin view it permanently deletes (with ?permanent=true). Every row
// type goes through the automations page's own router so the dashboard owns the
// whole lifecycle and nothing depends on per-agent core routes.
async function _deleteSelected(btn) {
  const selected = _selectedRows();
  if (selected.length === 0) {
    if (btn) resetDeleteBtn(btn, { size: '16px', title: _trashTitle() });
    return;
  }

  const token = localStorage.getItem('auth_token');
  const userId = app.currentUserId;
  let ok = 0, fail = 0;
  for (const tr of selected) {
    let url = _rowBasePath(tr.dataset.type, tr.dataset.id);
    if (!url) { fail++; continue; }
    url += `?user_id=${encodeURIComponent(userId)}`;
    if (_view === 'bin') url += '&permanent=true';
    if (token) url += `&token=${encodeURIComponent(token)}`;
    try {
      const res = await fetch(apiPath(url), { method: 'DELETE' });
      if (res.ok || res.status === 404) ok++; else fail++;
    } catch (_) { fail++; }
  }
  // Deleting from the completed/active view soft-deletes (→ bin); the bin itself
  // permanently erases.
  if (ok > 0) _showToast(_view === 'bin'
    ? `Deleted ${ok} automation${ok > 1 ? 's' : ''}`
    : `Recycled ${ok} automation${ok > 1 ? 's' : ''}`);
  if (fail > 0) console.warn('[Automations] Failed to delete', fail, 'rows');
  await _loadAndRender();
  if (btn) resetDeleteBtn(btn, { size: '16px', title: _trashTitle() });
}

// Restore the selected rows from the bin back to the active dashboard.
async function _restoreSelected() {
  const selected = _selectedRows();
  if (selected.length === 0) return;
  const token = localStorage.getItem('auth_token');
  const userId = app.currentUserId;
  let ok = 0;
  for (const tr of selected) {
    const base = _rowBasePath(tr.dataset.type, tr.dataset.id);
    if (!base) continue;
    let url = `${base}/restore?user_id=${encodeURIComponent(userId)}`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    try {
      const res = await fetch(apiPath(url), { method: 'POST' });
      if (res.ok) ok++;
    } catch (_) {}
  }
  if (ok > 0) _showToast(`Restored ${ok} automation${ok > 1 ? 's' : ''}`);
  await _loadAndRender();
}

function _trashTitle() {
  if (_view === 'bin') return 'Select automations to delete';
  if (_view === 'completed') return 'Select automations to remove';
  return 'Open recycling bin';
}

async function _enterCompleted() {
  if (_view === 'completed') return;
  _view = 'completed';
  await _loadAndRender();
}

async function _enterBin() {
  if (_view === 'bin') return;
  _view = 'bin';
  await _loadAndRender();
}

// Back arrow / title returns to the main list from either the completed or bin view.
async function _exitToActive() {
  if (_view === 'active') return;
  _view = 'active';
  await _loadAndRender();
}

// Selection / view toolbar — mirrors agents' _updateBinToolbar and the Sessions
// page, extended to three sibling views (active · completed · bin). Paints the
// morphing recycle/trash icon, the Completed button + badge, and the
// back/restore/filter state.
function _updateViewToolbar() {
  const trash        = _qs('automations-trash-btn');
  const restoreBtn   = _qs('automations-restore-btn');
  const backBtn      = _qs('automations-back-btn');
  const completedBtn = _qs('automations-completed-btn');
  const completedNum = _qs('automations-completed-count');
  const title        = _qs('automations-toolbar-title');
  const sub          = _qs('automations-toolbar-sub');
  const filterSel    = _qs('automations-filter-agent');
  const refreshBtn   = _qs('automations-refresh-btn');
  const n = _selectedRows().length;

  const inActive    = _view === 'active';
  const inCompleted = _view === 'completed';
  const inBin       = _view === 'bin';

  // Back arrow + clickable title appear in any non-active view.
  if (backBtn) backBtn.style.display = inActive ? 'none' : 'inline-flex';
  if (title) {
    title.textContent = inActive ? 'Automations' : 'Back to Automations';
    title.style.cursor = inActive ? '' : 'pointer';
  }
  if (sub) sub.textContent =
    inBin ? ' · Recycling bin'
    : inCompleted ? ' · Completed'
    : ' · scheduled tasks · event triggers · worker spawns · webhooks';

  // The agent filter only makes sense on the active dashboard; refresh works on
  // both active and completed (live data), not in the bin.
  if (filterSel) filterSel.style.display = inActive ? '' : 'none';
  if (refreshBtn) refreshBtn.style.display = inBin ? 'none' : '';

  // Completed entry button: active view only, with a count badge.
  if (completedBtn) completedBtn.style.display = inActive ? 'inline-flex' : 'none';
  if (completedNum) {
    if (_completedCount > 0) { completedNum.textContent = String(_completedCount); completedNum.style.display = ''; }
    else completedNum.style.display = 'none';
  }

  // Restore button: bin view only.
  if (restoreBtn) {
    restoreBtn.style.display = inBin ? 'inline-flex' : 'none';
    restoreBtn.disabled = (n === 0);
  }

  if (trash) {
    if (inBin) {
      trash.title = n ? 'Permanently delete selected' : 'Select automations to delete';
      trash.disabled = (n === 0);
    } else if (inCompleted) {
      trash.title = n ? 'Move selected to the recycling bin' : 'Select automations to remove';
      trash.disabled = (n === 0);
    } else {
      trash.title = n ? 'Move selected to the recycling bin' : 'Open recycling bin';
      trash.disabled = false;
    }
    // Only the active view's resting state opens the bin (green recycle). Any
    // selection — or the resting state of completed/bin — shows the red trash.
    const showRecycleEntry = inActive && n === 0;
    trash.innerHTML = icon(showRecycleEntry ? 'recycle' : 'trash-2',
      { size: '16px', style: showRecycleEntry ? 'color:var(--success)' : '' });
  }
}

// ── Wire DOM (run once) ────────────────────────────────────────────

function _wireDom() {
  if (_initialized) return;
  _initialized = true;

  // Row-level interactions (delegated on the tbody)
  const tbody = _qs('automations-table-body');
  if (tbody) {
    tbody.addEventListener('click', (e) => {
      const deviceChip = e.target.closest('.auto-device-chip');
      if (deviceChip) { _openDeviceChip(deviceChip); return; }
      const checkCell = e.target.closest('.auto-check-cell');
      if (checkCell) { _toggleCheck(checkCell); return; }
    });
    tbody.addEventListener('change', (e) => {
      const toggle = e.target.closest('.auto-toggle-input');
      if (toggle) _toggleEnabled(toggle);
    });
  }

  // Header check-all — toggle all on/off (replaces the old toolbar Select-all /
  // Deselect-all buttons).
  const checkAll = _qs('automations-check-all');
  if (checkAll) {
    checkAll.addEventListener('click', () => {
      const all = document.querySelectorAll('#automations-table .auto-check-cell[data-id]');
      const checked = document.querySelectorAll('#automations-table .auto-check-cell[data-id].checked');
      _setAllChecked(checked.length < all.length);
    });
  }

  // Agent filter
  const filterSel = _qs('automations-filter-agent');
  if (filterSel) {
    filterSel.addEventListener('change', (e) => {
      _filterAgentId = e.target.value;
      _loadAndRender();
    });
  }

  // Refresh
  const refreshBtn = _qs('automations-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', () => _loadAndRender());

  // Completed button — swap into the history of finished automations (active view only).
  const completedBtn = _qs('automations-completed-btn');
  if (completedBtn) completedBtn.addEventListener('click', () => _enterCompleted());

  // Trash / delete button — green recycle (open bin) when nothing is selected on
  // the active view; red trash (two-click confirm) to move the selection to the
  // bin, or — when already in the bin — to permanently delete it. In the
  // completed view it soft-deletes the selection too. (HARMONY with agents + sessions)
  const trash = _qs('automations-trash-btn');
  if (trash) {
    trash.addEventListener('click', () => {
      if (_view === 'active' && _selectedRows().length === 0) { _enterBin(); return; }
      if (_selectedRows().length === 0) return;  // completed/bin, nothing selected → no-op
      advanceDeleteBtn(trash, {
        size: '16px', spinSize: '16px',
        armTitle: _view === 'bin' ? 'Click again to permanently delete' : 'Click again to move to the bin',
        onConfirm: () => _deleteSelected(trash),
      });
    });
  }

  // Back button + title click — return to the main list from completed or bin.
  const backBtn = _qs('automations-back-btn');
  if (backBtn) backBtn.addEventListener('click', () => { if (_view !== 'active') _exitToActive(); });
  const title = _qs('automations-toolbar-title');
  if (title) title.addEventListener('click', () => { if (_view !== 'active') _exitToActive(); });

  // Restore button — bring the selected recycled automations back.
  const restoreBtn = _qs('automations-restore-btn');
  if (restoreBtn) restoreBtn.addEventListener('click', () => {
    if (_selectedRows().length > 0) _restoreSelected();
  });
}

/** Return the trash button to its resting state (called when selection changes). */
function _resetTrash() {
  const trash = _qs('automations-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: _trashTitle() });
}

// ── Exports ────────────────────────────────────────────────────────

export function startAutomations() {
  if (!app.currentUserId) return;
  _wireDom();
  _loadAndRender();

  // Auto-refresh
  if (!_refreshTimer) {
    _refreshTimer = setInterval(_loadAndRender, _REFRESH_INTERVAL_MS);
  }
}

export function stopAutomations() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
}

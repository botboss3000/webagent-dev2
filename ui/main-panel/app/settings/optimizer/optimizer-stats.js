'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Optimizer Runs tab.
 *
 * A sortable / filterable table of optimizer runs (one Planner → Worker →
 * Closer pipeline per row), mirroring the Sessions / Automations dashboards.
 * Each row expands to its per-stage breakdown; Planner and Closer sessions open
 * straight into the chat so the admin can talk to them. Performance columns show
 * the current value plus the delta the run achieved vs the baseline session.
 *
 * Backend: GET /admin/settings/optimizer/runs (list, enriched) and
 *          GET /admin/settings/optimizer/runs/{id} (per-stage detail).
 * Below the table: a simple On/Off switch plus, when on, a "run only if the
 * session is longer than N turns" threshold (stored as schedule.min_interactions).
 * Off = manual only.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { app } from '../../../../shared/js/state.js';
import { isAdmin } from '../../../../shared/js/left-login.js?v=253';
import { loadSessionChat } from '../../../../chat/js/session-load.js?v=509';
import { populateSessionSelect } from '../../../../chat/js/session-list.js';
import { _fetch, _qs, _esc } from '../utils.js';

// ── Module state ───────────────────────────────────────────────────
let _runs = [];
let _stageCache = {};                 // run_id → { stages, html }
const _expanded = new Set();          // run_ids whose detail row is open
let _sortKey = 'started_at';
let _sortDir = 'desc';                // 'asc' | 'desc'
let _filterStatus = '';
let _searchText = '';

// ── Init / load ────────────────────────────────────────────────────
export function init() {
  _qs('ac-opt-mode-on')?.addEventListener('click', () => _setOptMode('on'));
  _qs('ac-opt-mode-off')?.addEventListener('click', () => _setOptMode('off'));
  _qs('ac-opt-save')?.addEventListener('click', _saveOptimizer);
  _qs('ac-opt-run-now')?.addEventListener('click', _runOptimizerNow);

  _qs('ac-opt-refresh')?.addEventListener('click', _loadRuns);
  _qs('ac-opt-filter-status')?.addEventListener('change', (e) => { _filterStatus = e.target.value; _render(); });
  _qs('ac-opt-search')?.addEventListener('input', (e) => { _searchText = (e.target.value || '').toLowerCase(); _render(); });

  // Sortable headers (delegated on the thead).
  const table = _qs('ac-opt-runs-table');
  table?.querySelector('thead')?.addEventListener('click', (e) => {
    const th = e.target.closest('.ac-opt-sort');
    if (!th) return;
    const key = th.dataset.sort;
    if (_sortKey === key) { _sortDir = _sortDir === 'asc' ? 'desc' : 'asc'; }
    else { _sortKey = key; _sortDir = key === 'started_at' ? 'desc' : 'asc'; }
    _render();
  });

  // Row clicks: toggle expand, or open a stage session.
  _qs('ac-opt-runs-tbody')?.addEventListener('click', _onTbodyClick);
}

export async function load() {
  await _loadRuns();
  try {
    const res = await _fetch(apiPath('/admin/settings/optimizer'));
    if (!res.ok) return;
    const cfg = await res.json();
    // Normalize legacy modes: "live" → on, "scheduled"/blank → off.
    const mode = (cfg.mode === 'on' || cfg.mode === 'live') ? 'on' : 'off';
    _setOptMode(mode);
    const minEl = _qs('ac-opt-schedule-min');
    if (minEl && cfg.schedule?.min_interactions) minEl.value = cfg.schedule.min_interactions;
  } catch (e) { /* use defaults */ }
}

// ── Fetch + render the runs table ──────────────────────────────────
async function _loadRuns() {
  const tbody = _qs('ac-opt-runs-tbody');
  const countEl = _qs('ac-opt-runs-count');
  if (!tbody) return;
  try {
    const res = await _fetch(apiPath('/admin/settings/optimizer/runs?limit=100'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _runs = await res.json();
    if (!Array.isArray(_runs)) _runs = [];
    _stageCache = {};
    _render();
  } catch (e) {
    if (countEl) countEl.textContent = '— error';
    tbody.innerHTML = `<tr><td colspan="9" class="ac-table-empty" class="ac-fg-danger">Error loading runs</td></tr>`;
  }
}

function _filteredSorted() {
  let rows = _runs.slice();
  if (_filterStatus) rows = rows.filter(r => (r.status || '') === _filterStatus);
  if (_searchText) {
    rows = rows.filter(r => {
      const hay = `${r.target_agent || ''} ${r.target_title || ''} ${r.target_session || ''} ${r.summary || ''}`.toLowerCase();
      return hay.includes(_searchText);
    });
  }
  const dir = _sortDir === 'asc' ? 1 : -1;
  rows.sort((a, b) => {
    const av = _sortVal(a, _sortKey), bv = _sortVal(b, _sortKey);
    if (av < bv) return -1 * dir;
    if (av > bv) return 1 * dir;
    return 0;
  });
  return rows;
}

function _sortVal(r, key) {
  switch (key) {
    case 'target':   return `${r.target_agent || ''} ${r.target_title || ''}`.toLowerCase();
    case 'status':   return (r.status || '').toLowerCase();
    case 'stage':    return (r.stage || '').toLowerCase();
    case 'proposals': return (r.proposals_generated || 0) * 1000 + (r.proposals_deployed || 0);
    case 'trials_count': return r.trials_count || 0;
    case 'tokens':   return r.tokens || 0;
    case 'cost':     return r.cost || 0;
    case 'duration_ms': return r.duration_ms || 0;
    case 'started_at': return r.started_at || '';
    default: return '';
  }
}

function _render() {
  const tbody = _qs('ac-opt-runs-tbody');
  const countEl = _qs('ac-opt-runs-count');
  if (!tbody) return;

  _paintSortIndicators();

  const rows = _filteredSorted();
  if (countEl) {
    const total = _runs.length;
    const shown = rows.length;
    countEl.textContent = shown === total
      ? `— ${total} run${total !== 1 ? 's' : ''}`
      : `— ${shown} of ${total}`;
  }

  if (!rows.length) {
    tbody.innerHTML = `<tr><td colspan="9" class="ac-table-empty">${_runs.length ? 'No runs match the filter' : 'No optimizer runs yet'}</td></tr>`;
    return;
  }

  let html = '';
  for (const r of rows) {
    const isOpen = _expanded.has(r.id);
    html += _renderRow(r, isOpen);
    if (isOpen) html += `<tr class="ac-opt-detail-row" data-detail-for="${_esc(r.id)}"><td colspan="9">${_stageCache[r.id]?.html || _loadingDetail()}</td></tr>`;
  }
  tbody.innerHTML = html;
  if (window.lucide?.createIcons) window.lucide.createIcons({ nodes: [tbody] });

  // Lazily fetch detail for any open row not yet cached.
  for (const r of rows) {
    if (_expanded.has(r.id) && !_stageCache[r.id]) _loadDetail(r.id);
  }
}

function _renderRow(r, isOpen) {
  const target = r.target_agent || r.target_title || (r.target_session ? r.target_session.slice(0, 12) : '—');
  const caret = `<i data-lucide="${isOpen ? 'chevron-down' : 'chevron-right'}" class="ac-opt-caret"></i>`;
  const proposals = `${r.proposals_generated || 0} <span class="ac-opt-arrow">→</span> ${r.proposals_deployed || 0}`;
  return `<tr class="ac-opt-run-row${isOpen ? ' open' : ''}" data-run-id="${_esc(r.id)}">
    <td class="ac-opt-target" title="${_esc(r.target_session || '')}">${caret}<span>${_esc(target)}</span></td>
    <td>${_statusBadge(r.status)}</td>
    <td>${_stageBadge(r.stage)}</td>
    <td class="ac-opt-num">${proposals}</td>
    <td class="ac-opt-num">${r.trials_count || '—'}</td>
    <td class="ac-opt-num">${_metric(r.tokens, r.tokens_delta, _fmtTokens)}</td>
    <td class="ac-opt-num">${_metric(r.cost, r.cost_delta, _fmtCost)}</td>
    <td class="ac-opt-num">${_fmtDuration(r.duration_ms)}</td>
    <td class="ac-opt-date">${_fmtDate(r.started_at)}</td>
  </tr>`;
}

// Metric cell: current value + signed delta (negative = improvement = green).
function _metric(value, delta, fmt) {
  if (value == null) return '—';
  let html = `<span class="ac-opt-metric-val">${fmt(value)}</span>`;
  if (delta != null && delta !== 0) {
    const better = delta < 0;                       // fewer tokens / less cost is better
    const sign = delta > 0 ? '+' : '−';
    html += ` <span class="ac-opt-delta ${better ? 'good' : 'bad'}">${sign}${fmt(Math.abs(delta))}</span>`;
  }
  return html;
}

function _loadingDetail() {
  return `<div class="ac-opt-detail-loading"><i data-lucide="loader-2" class="lucide-spin" class="ac-ico-14"></i> Loading stages…</div>`;
}

// ── Per-stage detail (expandable row) ──────────────────────────────
async function _loadDetail(runId) {
  try {
    const res = await _fetch(apiPath(`/admin/settings/optimizer/runs/${encodeURIComponent(runId)}`));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _stageCache[runId] = { stages: data.stages || [], html: _renderStages(data.stages || []) };
  } catch (e) {
    _stageCache[runId] = { stages: [], html: `<div class="ac-opt-detail-loading" class="ac-fg-danger">Failed to load stages</div>` };
  }
  // Re-paint just this detail cell if still open.
  const cell = document.querySelector(`tr.ac-opt-detail-row[data-detail-for="${CSS.escape(runId)}"] td`);
  if (cell) {
    cell.innerHTML = _stageCache[runId].html;
    if (window.lucide?.createIcons) window.lucide.createIcons({ nodes: [cell] });
  }
}

const _STAGE_ICON = { planner: 'clipboard-list', worker: 'flask-conical', closer: 'shield-check' };

function _renderStages(stages) {
  if (!stages.length) return `<div class="ac-opt-detail-loading">No stage detail</div>`;
  let html = '<div class="ac-opt-stages">';
  for (const s of stages) {
    const ic = _STAGE_ICON[s.key] || 'circle';
    const bits = [];
    if (s.trials_count != null) bits.push(`${s.trials_count} trial${s.trials_count !== 1 ? 's' : ''}`);
    if (s.tokens != null) bits.push(`${_fmtTokens(s.tokens)} tok`);
    if (s.cost != null && s.cost > 0) bits.push(_fmtCost(s.cost));
    if (s.duration_ms != null) bits.push(_fmtDuration(s.duration_ms));
    const meta = bits.length ? `<span class="ac-opt-stage-meta">${bits.join(' · ')}</span>` : '';
    const action = (s.openable && s.session_id)
      ? `<button type="button" class="ac-btn ac-btn-ghost ac-opt-open-btn" data-open-session="${_esc(s.session_id)}"><i data-lucide="message-square" class="ac-ico-13"></i> Open</button>`
      : `<span class="ac-opt-stage-noopen">${s.session_id ? '' : 'no session'}</span>`;
    html += `<div class="ac-opt-stage">
      <div class="ac-opt-stage-head"><i data-lucide="${ic}" class="ac-opt-stage-icon"></i><span class="ac-opt-stage-label">${_esc(s.label)}</span>${meta}</div>
      <div class="ac-opt-stage-detail">${_esc(s.detail || '')}</div>
      <div class="ac-opt-stage-action">${action}</div>
    </div>`;
  }
  html += '</div>';
  return html;
}

// ── Row interactions ───────────────────────────────────────────────
function _onTbodyClick(e) {
  const openBtn = e.target.closest('[data-open-session]');
  if (openBtn) { e.stopPropagation(); _openSession(openBtn.dataset.openSession); return; }

  const row = e.target.closest('.ac-opt-run-row');
  if (!row) return;
  const id = row.dataset.runId;
  if (_expanded.has(id)) _expanded.delete(id); else _expanded.add(id);
  _render();
}

// Load a pipeline session into the chat and reveal the chat panel so the admin
// can talk to that agent (Planner or Closer).
function _openSession(sid) {
  if (!sid) return;
  app.currentSessionId = sid;
  try { loadSessionChat(sid); } catch (_) {}
  try { populateSessionSelect(app.currentUserId); } catch (_) {}
  window.dispatchEvent(new CustomEvent("webagent-loop-session-changed"));
  // Reveal the chat panel (it may be hidden behind the admin view).
  try { if (typeof window.__applyChatVisible === 'function') window.__applyChatVisible(true); } catch (_) {}
}

function _paintSortIndicators() {
  document.querySelectorAll('#ac-opt-runs-table .ac-opt-sort').forEach(th => {
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.sort === _sortKey) th.classList.add(_sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
  });
}

// ── Run config (on/off · cadence · save · run-now) ──
function _setOptMode(mode) {
  const schedSec = _qs('ac-opt-schedule-section');
  const onBtn    = _qs('ac-opt-mode-on');
  const offBtn   = _qs('ac-opt-mode-off');
  if (onBtn)  onBtn.classList.toggle('active', mode === 'on');
  if (offBtn) offBtn.classList.toggle('active', mode === 'off');
  // The cadence card only matters when the optimizer is on.
  if (schedSec) schedSec.style.display = mode === 'on' ? '' : 'none';
}

async function _saveOptimizer() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const mode    = _qs('ac-opt-mode-on')?.classList.contains('active') ? 'on' : 'off';
  let   minInt  = parseInt(_qs('ac-opt-schedule-min')?.value || '5', 10);
  if (!Number.isFinite(minInt) || minInt < 1) minInt = 5;
  const statusEl = _qs('ac-opt-status');
  try {
    const res = await _fetch(apiPath('/admin/settings/optimizer'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, schedule: { min_interactions: minInt } }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) {
      statusEl.textContent = 'Config saved';
      statusEl.style.color = 'var(--success)';
      statusEl.style.display = 'inline';
      setTimeout(() => { statusEl.style.display = 'none'; }, 3000);
    }
  } catch (e) {
    if (statusEl) {
      statusEl.textContent = `Error: ${e.message}`;
      statusEl.style.color = 'var(--danger)';
      statusEl.style.display = 'inline';
    }
  }
}

async function _runOptimizerNow() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const statusEl = _qs('ac-opt-status');
  if (statusEl) { statusEl.textContent = 'Running optimizer…'; statusEl.style.color = 'var(--fg-3)'; statusEl.style.display = 'inline'; }
  try {
    const res = await _fetch(apiPath('/admin/optimizer/run'), { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (statusEl) { statusEl.textContent = 'Optimizer started'; statusEl.style.color = 'var(--success)'; }
    setTimeout(() => { _loadRuns(); if (statusEl) statusEl.style.display = 'none'; }, 3000);
  } catch (e) {
    if (statusEl) { statusEl.textContent = `Error: ${e.message}`; statusEl.style.color = 'var(--danger)'; }
  }
}

// ── Badges + formatters ────────────────────────────────────────────
function _statusBadge(status) {
  const s = (status || 'running').toLowerCase();
  const label = s.charAt(0).toUpperCase() + s.slice(1);
  return `<span class="ac-opt-badge status-${_esc(s)}">${_esc(label)}</span>`;
}
function _stageBadge(stage) {
  const s = (stage || 'planner').toLowerCase();
  const label = s.charAt(0).toUpperCase() + s.slice(1);
  return `<span class="ac-opt-badge stage-${_esc(s)}">${_esc(label)}</span>`;
}
function _fmtTokens(n) { if (n == null) return '—'; n = Math.round(n); return Math.abs(n) >= 1000 ? (n / 1000).toFixed(1) + 'k' : String(n); }
function _fmtDuration(ms) { if (ms == null) return '—'; if (ms < 1000) return ms + 'ms'; if (ms < 60000) return (ms/1000).toFixed(1) + 's'; return Math.floor(ms/60000) + 'm ' + Math.floor((ms%60000)/1000) + 's'; }
function _fmtCost(c) { if (c == null) return '—'; if (c === 0) return '$0'; return '$' + (c < 0.001 ? c.toFixed(6) : c < 0.01 ? c.toFixed(4) : c.toFixed(3)); }
function _fmtDate(ts) { if (!ts) return '—'; try { const d = new Date(ts.replace(' ', 'T') + (ts.includes('Z') || ts.includes('+') ? '' : 'Z')); if (isNaN(d)) return ts; return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' }) + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' }); } catch { return ts; } }

// Diagnostics sub-page (Admin Tools → Diagnostics).
//
// Reads the in-app flight-recorder via GET /api/v1/diagnostics (admin-only)
// and renders a filterable, optionally-live feed of recent records: server
// errors + tracebacks, agent-loop pipeline problems, run outcomes and tool
// errors. Mirrors the Interactions / Runtime-Loop sub-page lifecycle
// (start / stop / renderSidebar) wired in files.js.

import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';

const LEVELS = [
  { key: 'error', label: 'Errors' },
  { key: 'warning', label: 'Warnings' },
  { key: 'info', label: 'Info' },
];
const CATEGORIES = [
  { key: 'server', label: 'Server' },
  { key: 'http', label: 'HTTP' },
  { key: 'loop', label: 'Loop' },
  { key: 'run', label: 'Runs' },
  { key: 'recovery', label: 'Self Recovery' },
  { key: 'tool', label: 'Tools' },
];

const state = {
  levels: new Set(['error', 'warning']),  // default: the problems
  categories: new Set(),                   // empty = all categories
  search: '',
  sinceMinutes: 60,
  live: false,
  timer: null,
  wiredSidebar: false,
  wiredMain: false,
  flashUntil: 0,
};

const POLL_MS = 5000;

function _qs(id) { return document.getElementById(id); }

// ── Lifecycle ────────────────────────────────────────────────────────────

export function startDiagnostics() {
  renderDiagnosticsSidebar();
  _wireMain();
  fetchAndRender();
  _applyLiveTimer();
}

export function stopDiagnostics() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
}

export function renderDiagnosticsSidebar() {
  _renderChips('diag-levels', LEVELS, state.levels);
  _renderChips('diag-categories', CATEGORIES, state.categories);
  if (state.wiredSidebar) return;
  const search = _qs('diag-search');
  if (search) {
    search.value = state.search;
    search.addEventListener('input', _debounce(() => {
      state.search = search.value.trim();
      fetchAndRender();
    }, 300));
  }
  const win = _qs('diag-window');
  if (win) {
    win.value = String(state.sinceMinutes);
    win.addEventListener('change', () => {
      state.sinceMinutes = win.value === '' ? null : Number(win.value);
      fetchAndRender();
    });
  }
  const live = _qs('diag-live');
  if (live) {
    live.checked = state.live;
    live.addEventListener('change', () => {
      state.live = live.checked;
      _applyLiveTimer();
      if (state.live) fetchAndRender();
    });
  }
  const refresh = _qs('diag-refresh');
  if (refresh && !refresh.dataset.wired) {
    refresh.dataset.wired = '1';
    refresh.addEventListener('click', () => fetchAndRender());
  }
  // Clear-logs controls (each is a two-click confirm).
  const clearAll = _qs('diag-clear-all');
  if (clearAll && !clearAll.dataset.wired) {
    clearAll.dataset.wired = '1';
    clearAll.addEventListener('click', () => _armClear(clearAll, () => {
      _clearLogs({ clear_files: !!(_qs('diag-clear-files') || {}).checked });
    }));
  }
  const clearAge = _qs('diag-clear-age-btn');
  if (clearAge && !clearAge.dataset.wired) {
    clearAge.dataset.wired = '1';
    clearAge.addEventListener('click', () => _armClear(clearAge, () => {
      const sel = _qs('diag-clear-age');
      _clearLogs({ older_than_minutes: sel ? Number(sel.value) : 60 });
    }));
  }
  const clearFilter = _qs('diag-clear-filter');
  if (clearFilter && !clearFilter.dataset.wired) {
    clearFilter.dataset.wired = '1';
    clearFilter.addEventListener('click', () => _armClear(clearFilter, () => {
      const levels = new Set(state.levels);
      if (levels.has('error')) levels.add('critical');
      _clearLogs({
        levels: levels.size ? [...levels].join(',') : null,
        categories: state.categories.size ? [...state.categories].join(',') : null,
        search: state.search || null,
      });
    }));
  }
  state.wiredSidebar = true;
}

// Two-click confirm: first click arms the button (turns it red, relabels);
// second click within 3.5s runs the action. Any other armed clear button disarms.
function _armClear(btn, run) {
  if (btn.dataset.armed === '1') {
    btn.dataset.armed = '0';
    if (btn._disarm) { clearTimeout(btn._disarm); btn._disarm = null; }
    btn.classList.remove('armed');
    btn.textContent = btn.dataset.label || btn.textContent;
    run();
    return;
  }
  document.querySelectorAll('.diag-clear-btn.armed').forEach((b) => {
    b.dataset.armed = '0'; b.classList.remove('armed');
    if (b._disarm) { clearTimeout(b._disarm); b._disarm = null; }
    b.textContent = b.dataset.label || b.textContent;
  });
  if (!btn.dataset.label) btn.dataset.label = btn.textContent;
  btn.dataset.armed = '1';
  btn.classList.add('armed');
  btn.textContent = 'Click again to confirm';
  btn._disarm = setTimeout(() => {
    btn.dataset.armed = '0'; btn.classList.remove('armed');
    btn.textContent = btn.dataset.label || btn.textContent;
  }, 3500);
}

async function _clearLogs(params) {
  const p = new URLSearchParams();
  if (params.older_than_minutes != null) p.set('older_than_minutes', String(params.older_than_minutes));
  if (params.levels) p.set('levels', params.levels);
  if (params.categories) p.set('categories', params.categories);
  if (params.search) p.set('search', params.search);
  if (params.clear_files) p.set('clear_files', 'true');
  try {
    const qs = p.toString();
    const res = await fetch(apiPath('/api/v1/diagnostics' + (qs ? '?' + qs : '')), {
      method: 'DELETE', headers: authHeaders(),
    });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
      _flashStatus('Clear failed: ' + msg);
      return;
    }
    const j = await res.json();
    const n = Math.max(j.db_deleted || 0, j.ring_removed || 0);
    const filePart = (j.files_cleared && j.files_cleared.length) ? `, ${j.files_cleared.length} file(s) wiped` : '';
    _flashStatus(`Cleared ${n} record(s)${filePart}`);
    fetchAndRender();                                  // empty the feed now (stats held by the flash)
    setTimeout(() => { state.flashUntil = 0; fetchAndRender(); }, 2600);  // then restore the live count
  } catch (e) {
    _flashStatus('Clear failed: ' + (e && e.message ? e.message : e));
  }
}

function _flashStatus(text) {
  state.flashUntil = Date.now() + 2500;
  const el = _qs('diag-stats');
  if (el) el.textContent = text;
}

function _wireMain() {
  if (state.wiredMain) return;
  const r = _qs('diag-refresh-main');
  if (r) r.addEventListener('click', () => fetchAndRender());
  state.wiredMain = true;
}

function _applyLiveTimer() {
  if (state.timer) { clearInterval(state.timer); state.timer = null; }
  const dot = _qs('diag-live-dot');
  if (dot) dot.hidden = !state.live;
  if (state.live) state.timer = setInterval(fetchAndRender, POLL_MS);
}

// ── Filter chips ──────────────────────────────────────────────────────────

function _renderChips(containerId, defs, selectedSet) {
  const box = _qs(containerId);
  if (!box) return;
  box.innerHTML = '';
  for (const d of defs) {
    const b = document.createElement('button');
    b.className = 'diag-chip' + (selectedSet.has(d.key) ? ' active' : '');
    b.dataset.key = d.key;
    b.textContent = d.label;
    b.addEventListener('click', () => {
      if (selectedSet.has(d.key)) selectedSet.delete(d.key);
      else selectedSet.add(d.key);
      b.classList.toggle('active', selectedSet.has(d.key));
      fetchAndRender();
    });
    box.appendChild(b);
  }
}

// ── Fetch + render ─────────────────────────────────────────────────────────

function _buildQuery() {
  const p = new URLSearchParams();
  // "Errors" implies critical too.
  const levels = new Set(state.levels);
  if (levels.has('error')) levels.add('critical');
  if (levels.size) p.set('levels', [...levels].join(','));
  if (state.categories.size) p.set('categories', [...state.categories].join(','));
  if (state.search) p.set('search', state.search);
  if (state.sinceMinutes != null) p.set('since_minutes', String(state.sinceMinutes));
  p.set('limit', '300');
  return p.toString();
}

async function fetchAndRender() {
  const feed = _qs('diag-feed');
  if (!feed) return;
  try {
    const url = apiPath('/api/v1/diagnostics?' + _buildQuery());
    const res = await fetch(url, { headers: authHeaders() });
    if (!res.ok) {
      let msg = 'HTTP ' + res.status;
      try { const j = await res.json(); msg = j.detail || msg; } catch (_) {}
      feed.innerHTML = '';
      feed.appendChild(_hint(res.status === 403
        ? 'Admin access required to view diagnostics.'
        : ('Could not load diagnostics: ' + msg)));
      _setStats(null);
      return;
    }
    const data = await res.json();
    _renderRecords(data.records || []);
  } catch (e) {
    feed.innerHTML = '';
    feed.appendChild(_hint('Could not reach the diagnostics endpoint: ' + (e && e.message ? e.message : e)));
    _setStats(null);
  }
}

function _renderRecords(records) {
  const feed = _qs('diag-feed');
  if (!feed) return;
  feed.innerHTML = '';
  if (!records.length) {
    feed.appendChild(_hint('No diagnostics match these filters. Widen the window, add severities, or clear the category filter.'));
    _setStats({ shown: 0, error: 0, warning: 0 });
    return;
  }
  let nErr = 0, nWarn = 0;
  for (const r of records) {
    const lvl = (r.level || 'info').toLowerCase();
    if (lvl === 'error' || lvl === 'critical') nErr++;
    else if (lvl === 'warning') nWarn++;
    feed.appendChild(_recordRow(r, lvl));
  }
  _setStats({ shown: records.length, error: nErr, warning: nWarn });
}

function _recordRow(r, lvl) {
  const row = document.createElement('div');
  row.className = 'diag-row lvl-' + lvl;

  const head = document.createElement('div');
  head.className = 'diag-row-head';

  const badge = document.createElement('span');
  badge.className = 'diag-badge lvl-' + lvl;
  badge.textContent = lvl;
  head.appendChild(badge);

  const cat = document.createElement('span');
  cat.className = 'diag-cat';
  cat.textContent = r.category || '';
  head.appendChild(cat);

  const msg = document.createElement('span');
  msg.className = 'diag-msg';
  msg.textContent = r.message || '';
  head.appendChild(msg);

  const ts = document.createElement('span');
  ts.className = 'diag-ts';
  ts.textContent = _fmtTime(r.ts);
  head.appendChild(ts);

  row.appendChild(head);

  const meta = [];
  if (r.source) meta.push('source: ' + r.source);
  if (r.session_id) meta.push('session: ' + String(r.session_id).slice(0, 12));
  if (r.agent_id) meta.push('agent: ' + String(r.agent_id).slice(0, 12));
  const hasDetail = r.detail != null && (typeof r.detail !== 'object' || Object.keys(r.detail).length);

  if (meta.length || hasDetail) {
    const sub = document.createElement('div');
    sub.className = 'diag-row-sub';
    if (meta.length) {
      const m = document.createElement('span');
      m.className = 'diag-meta';
      m.textContent = meta.join('  ·  ');
      sub.appendChild(m);
    }
    if (hasDetail) {
      const toggle = document.createElement('button');
      toggle.className = 'diag-detail-toggle';
      toggle.textContent = 'detail';
      const pre = document.createElement('pre');
      pre.className = 'diag-detail';
      pre.hidden = true;
      pre.textContent = typeof r.detail === 'string' ? r.detail : JSON.stringify(r.detail, null, 2);
      toggle.addEventListener('click', () => {
        pre.hidden = !pre.hidden;
        toggle.classList.toggle('active', !pre.hidden);
      });
      sub.appendChild(toggle);
      row.appendChild(sub);
      row.appendChild(pre);
    } else {
      row.appendChild(sub);
    }
  }
  return row;
}

function _setStats(s) {
  const el = _qs('diag-stats');
  if (!el) return;
  // Don't clobber a recent "Cleared N records" flash with the live count.
  if (state.flashUntil && Date.now() < state.flashUntil) return;
  if (!s) { el.textContent = ''; return; }
  const bits = [s.shown + ' shown'];
  if (s.error) bits.push(s.error + ' error' + (s.error === 1 ? '' : 's'));
  if (s.warning) bits.push(s.warning + ' warning' + (s.warning === 1 ? '' : 's'));
  el.textContent = bits.join('  ·  ');
}

// ── Small helpers ───────────────────────────────────────────────────────────

function _hint(text) {
  const d = document.createElement('div');
  d.className = 'diag-hint';
  d.textContent = text;
  return d;
}

function _fmtTime(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    const now = new Date();
    const sameDay = d.toDateString() === now.toDateString();
    const t = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
    return sameDay ? t : (d.toLocaleDateString([], { month: 'short', day: 'numeric' }) + ' ' + t);
  } catch (_) {
    return iso;
  }
}

function _debounce(fn, ms) {
  let h = null;
  return (...a) => { if (h) clearTimeout(h); h = setTimeout(() => fn(...a), ms); };
}

'use strict';

// DeepSeek Costs — drop-in Admin Tools view module.
// Discovered via ui/admin-tools/deepseek-costs/page.json and driven generically
// by the admin shell (ui/shared/js/files.js applySidebarView), which dynamically
// imports this module and calls startView / stopView when the view is shown /
// hidden. Talks to this folder's server.py (/api/v1/deepseek-costs/*).
// Reporting-only: the adjusted figures live ONLY here — nothing else in the app
// is touched (no catalog writes, no wallet writes).
// COLOR SCHEME → ui/shared/css/design-system.css. Use CSS variables in inline
// styles; never write hex/rgb colour literals here.

import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';

const state = { config: null, report: null };

function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function fmtUsd(v) {
  if (v === null || v === undefined) return '—';
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  if (n === 0) return '$0.00';
  return n >= 0.01 ? '$' + n.toFixed(2) : '$' + n.toFixed(5);
}
function fmtTok(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (n >= 1000) return (n / 1000).toFixed(1) + 'k';
  return String(n);
}
function _el(tag, cls, html) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (html !== undefined) el.innerHTML = html;
  return el;
}
const _qs = (sel) => document.querySelector(sel);

async function _api(path, opts) {
  const res = await fetch(apiPath('/api/v1/deepseek-costs' + path),
    opts || { headers: authHeaders() });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) {
    const d = body && body.detail;
    const msg = d ? (typeof d === 'string' ? d : JSON.stringify(d)) : ('HTTP ' + res.status);
    throw new Error(msg);
  }
  return body;
}

// ── Shared style bits (design-system variables only) ──
const CARD = 'background:var(--bg-elev);border:1px solid var(--border);border-radius:8px;';
const BTN = 'background:var(--bg-elev);border:1px solid var(--border);color:var(--fg-1);' +
  'padding:5px 10px;border-radius:5px;cursor:pointer;font-size:12px;font-family:inherit;';
const BTN_PRIMARY = 'background:var(--accent);border:1px solid var(--accent);color:#fff;' +
  'padding:5px 12px;border-radius:5px;cursor:pointer;font-size:12px;font-family:inherit;';
const INPUT = 'background:var(--bg);border:1px solid var(--border);color:var(--fg-1);' +
  'padding:5px 8px;border-radius:5px;font-size:12px;font-family:inherit;width:100%;box-sizing:border-box;';
const MUTED = 'color:var(--fg-3);font-size:12px;';

// ── Lifecycle ──
export function startView() {
  _buildShell();
  _loadConfig();
  _loadReport();
}
export function stopView() { /* no polling — nothing to tear down */ }

// ── Shell ──
function _buildShell() {
  const root = _qs('#dcost-root');
  if (!root) return;
  root.innerHTML = `
    <div style="margin-bottom:14px;">
      <h2 style="margin:0 0 4px;font-size:16px;">DeepSeek Costs</h2>
      <div style="${MUTED}">Recompute per-user cost from raw token usage using an
        operator-tweakable cache + peak-hour rate table. These figures appear only
        on this page — the model catalog, wallet, and every other surface are left
        untouched.</div>
    </div>

    <section style="${CARD}padding:14px;margin-bottom:16px;">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;">
        <h3 style="margin:0;font-size:13px;">Rate table</h3>
        <div style="display:flex;align-items:center;gap:8px;">
          <span id="dcost-save-status" style="${MUTED}"></span>
          <button id="dcost-save" style="${BTN_PRIMARY}">Save</button>
        </div>
      </div>
      <div id="dcost-models"></div>
      <div style="margin-top:8px;">
        <button id="dcost-add-model" style="${BTN}">+ Add model</button>
      </div>

      <div style="border-top:1px solid var(--border);margin:14px 0 10px;"></div>
      <h3 style="margin:0 0 10px;font-size:13px;">Peak-hour billing</h3>
      <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;margin-bottom:10px;">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;color:var(--fg-1);">
          <input type="checkbox" id="dcost-peak-enabled"> Enabled
        </label>
        <label style="font-size:12px;color:var(--fg-1);">Multiplier
          <input type="number" id="dcost-peak-mult" step="0.1" min="0"
                 style="${INPUT}width:80px;display:inline-block;margin-left:6px;">
        </label>
      </div>
      <div id="dcost-windows" style="margin-bottom:8px;"></div>
      <button id="dcost-add-window" style="${BTN}">+ Add window</button>
      <div style="${MUTED}margin-top:8px;">Windows are UTC half-open [start, end) hours.
        Peak cost = base rate × multiplier.</div>
    </section>

    <section style="${CARD}padding:14px;">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:12px;">
        <h3 style="margin:0;font-size:13px;">Usage &amp; cost per user</h3>
        <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">
          <label style="font-size:12px;color:var(--fg-1);">From
            <input type="date" id="dcost-from" style="${INPUT}width:140px;display:inline-block;margin-left:4px;">
          </label>
          <label style="font-size:12px;color:var(--fg-1);">To
            <input type="date" id="dcost-to" style="${INPUT}width:140px;display:inline-block;margin-left:4px;">
          </label>
          <button id="dcost-refresh" style="${BTN}">Refresh</button>
        </div>
      </div>
      <div id="dcost-totals" style="margin-bottom:12px;"></div>
      <div id="dcost-table"></div>
    </section>
  `;

  _qs('#dcost-save').addEventListener('click', _onSave);
  _qs('#dcost-add-model').addEventListener('click', () => _addModelRow());
  _qs('#dcost-add-window').addEventListener('click', () => _addWindowRow());
  _qs('#dcost-refresh').addEventListener('click', _loadReport);

  // Default date range: last 30 days.
  const to = new Date();
  const from = new Date(Date.now() - 30 * 86400000);
  const fmt = (d) => d.toISOString().slice(0, 10);
  _qs('#dcost-from').value = fmt(from);
  _qs('#dcost-to').value = fmt(to);
}

// ── Config ──
async function _loadConfig() {
  try {
    const c = await _api('/config');
    state.config = c;
    _renderConfig(c);
  } catch (e) {
    _setSaveStatus('! ' + e.message, true);
  }
}

function _renderConfig(cfg) {
  const host = _qs('#dcost-models');
  host.innerHTML = '';
  for (const [key, rates] of Object.entries(cfg.models || {})) {
    _addModelRow(key, rates);
  }

  _qs('#dcost-peak-enabled').checked = !!cfg.peak.enabled;
  _qs('#dcost-peak-mult').value = cfg.peak.multiplier ?? 1;

  const whost = _qs('#dcost-windows');
  whost.innerHTML = '';
  for (const w of (cfg.peak.windows || [])) {
    _addWindowRow(w[0], w[1]);
  }
}

function _addModelRow(key = '', rates = {}) {
  const host = _qs('#dcost-models');
  if (!host) return;
  const row = _el('div', 'dcost-model-row',
    `<div style="display:grid;grid-template-columns:160px 1fr 1fr 1fr 1fr 28px;gap:8px;align-items:center;margin-bottom:8px;">
      <input class="dcost-model-key" placeholder="model id" value="${esc(key)}" style="${INPUT}">
      <label style="font-size:11px;color:var(--fg-3);">cache hit $/1M<input class="dcost-rate-hit" type="number" step="0.000001" min="0" value="${rates.cache_hit ?? ''}" style="${INPUT}"></label>
      <label style="font-size:11px;color:var(--fg-3);">cache miss $/1M<input class="dcost-rate-miss" type="number" step="0.000001" min="0" value="${rates.cache_miss ?? ''}" style="${INPUT}"></label>
      <label style="font-size:11px;color:var(--fg-3);">cache write $/1M<input class="dcost-rate-write" type="number" step="0.000001" min="0" value="${rates.cache_write ?? ''}" style="${INPUT}"></label>
      <label style="font-size:11px;color:var(--fg-3);">output $/1M<input class="dcost-rate-out" type="number" step="0.000001" min="0" value="${rates.output ?? ''}" style="${INPUT}"></label>
      <button class="dcost-rm-model" title="Remove model" style="${BTN}padding:4px 6px;">✕</button>
    </div>`);
  row.querySelector('.dcost-rm-model').addEventListener('click', () => row.remove());
  host.appendChild(row);
}

function _addWindowRow(s = '', e = '') {
  const host = _qs('#dcost-windows');
  if (!host) return;
  const row = _el('div', 'dcost-window-row',
    `<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px;">
      <input class="dcost-win-start" type="number" min="0" max="23" placeholder="start" value="${esc(s)}" style="${INPUT}width:70px;">
      <span style="color:var(--fg-3);">→</span>
      <input class="dcost-win-end" type="number" min="0" max="23" placeholder="end" value="${esc(e)}" style="${INPUT}width:70px;">
      <span style="${MUTED}">UTC</span>
      <button class="dcost-rm-window" title="Remove window" style="${BTN}padding:4px 6px;">✕</button>
    </div>`);
  row.querySelector('.dcost-rm-window').addEventListener('click', () => row.remove());
  host.appendChild(row);
}

async function _onSave() {
  const models = {};
  for (const row of document.querySelectorAll('.dcost-model-row')) {
    const key = row.querySelector('.dcost-model-key').value.trim();
    if (!key) continue;
    models[key] = {
      cache_hit: Number(row.querySelector('.dcost-rate-hit').value) || 0,
      cache_miss: Number(row.querySelector('.dcost-rate-miss').value) || 0,
      cache_write: Number(row.querySelector('.dcost-rate-write').value) || 0,
      output: Number(row.querySelector('.dcost-rate-out').value) || 0,
    };
  }
  const windows = [];
  for (const row of document.querySelectorAll('.dcost-window-row')) {
    const s = Number(row.querySelector('.dcost-win-start').value);
    const e = Number(row.querySelector('.dcost-win-end').value);
    if (Number.isInteger(s) && Number.isInteger(e)) windows.push([s, e]);
  }
  const payload = {
    models,
    peak: {
      enabled: _qs('#dcost-peak-enabled').checked,
      multiplier: Number(_qs('#dcost-peak-mult').value) || 1,
      windows,
    },
  };

  _setSaveStatus('Saving…');
  try {
    const c = await _api('/config', {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
      body: JSON.stringify(payload),
    });
    state.config = c;
    _renderConfig(c);
    _setSaveStatus('✓ Saved');
    _loadReport();
  } catch (e) {
    _setSaveStatus('! ' + e.message, true);
  }
}

function _setSaveStatus(text, isError) {
  const el = _qs('#dcost-save-status');
  if (!el) return;
  el.textContent = text;
  el.style.color = isError ? 'var(--danger)' : 'var(--fg-3)';
  clearTimeout(el._t);
  if (!isError) el._t = setTimeout(() => { el.textContent = ''; }, 2500);
}

// ── Report ──
async function _loadReport() {
  const from = _qs('#dcost-from').value || '';
  const to = _qs('#dcost-to').value || '';
  const q = new URLSearchParams();
  if (from) q.set('from', from);
  if (to) q.set('to', to);
  try {
    const r = await _api('/report?' + q.toString());
    state.report = r;
    _renderReport(r);
  } catch (e) {
    _qs('#dcost-table').innerHTML = `<div style="color:var(--danger);">${esc(e.message)}</div>`;
    _qs('#dcost-totals').innerHTML = '';
  }
}

function _renderReport(r) {
  const t = r.totals || {};
  const unconf = r.unconfigured_models || {};

  _qs('#dcost-totals').innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:10px;">
      ${_statCard('Total calls', String(t.calls || 0))}
      ${_statCard('Input tokens', fmtTok(t.input_tokens))}
      ${_statCard('Output tokens', fmtTok(t.output_tokens))}
      ${_statCard('Cache write', fmtTok(t.cache_write_tokens))}
      ${_statCard('Recomputed cost', fmtUsd(t.cost_usd))}
      ${_statCard('Unconfigured calls', String(t.unconfigured_calls || 0))}
    </div>
    ${Object.keys(unconf).length ? `<div style="${MUTED}margin-top:8px;">Models without a matching rate:
      ${Object.entries(unconf).map(([m, n]) => `<b>${esc(m)}</b> (${n})`).join(', ')}</div>` : ''}
  `;

  const host = _qs('#dcost-table');
  const users = r.users || [];
  if (!users.length) {
    host.innerHTML = `<div style="${MUTED}padding:12px;">No usage rows in range.</div>`;
    return;
  }

  host.innerHTML = `
    <div style="display:grid;grid-template-columns:1.6fr 0.7fr 0.9fr 0.9fr 0.9fr 0.9fr;gap:8px;
                padding:6px 10px;border-bottom:1px solid var(--border);font-size:11px;color:var(--fg-3);font-weight:600;">
      <div>User</div><div>Calls</div><div>Input</div><div>Cached / Miss</div><div>Output</div><div style="text-align:right;">Cost</div>
    </div>
  `;

  for (const u of users) {
    const details = _el('details', null, '');
    details.style.borderBottom = '1px solid var(--border)';
    details.innerHTML = `
      <summary style="cursor:pointer;list-style:none;display:grid;
        grid-template-columns:1.6fr 0.7fr 0.9fr 0.9fr 0.9fr 0.9fr;gap:8px;
        padding:8px 10px;font-size:12px;color:var(--fg-1);align-items:center;">
        <div><span style="color:var(--fg-3);margin-right:6px;">▸</span>${esc(u.display_name)}</div>
        <div>${u.calls}</div>
        <div>${fmtTok(u.input_tokens)}</div>
        <div>${fmtTok(u.cached_input_tokens)} / ${fmtTok(u.uncached_input_tokens)}</div>
        <div>${fmtTok(u.output_tokens)}</div>
        <div style="text-align:right;font-weight:600;">${fmtUsd(u.cost_usd)}</div>
      </summary>
      <div style="padding:4px 10px 12px 26px;">
        ${u.unconfigured_calls ? `<div style="${MUTED}margin-bottom:6px;">${u.unconfigured_calls} call(s) had no matching rate and are excluded from cost.</div>` : ''}
        ${u.models.length ? `
          <div style="display:grid;grid-template-columns:1.2fr 0.6fr 0.8fr 0.8fr 0.8fr 0.8fr;gap:8px;
                      font-size:11px;color:var(--fg-3);padding:4px 0;">
            <div>Model</div><div>Calls</div><div>Input</div><div>Cached / Miss</div><div>Output</div><div style="text-align:right;">Cost</div>
          </div>
          ${u.models.map(m => `
            <div style="display:grid;grid-template-columns:1.2fr 0.6fr 0.8fr 0.8fr 0.8fr 0.8fr;gap:8px;
                        font-size:12px;color:var(--fg-2);padding:3px 0;">
              <div>${esc(m.model)}</div>
              <div>${m.calls}</div>
              <div>${fmtTok(m.input_tokens)}</div>
              <div>${fmtTok(m.cached_input_tokens)} / ${fmtTok(m.uncached_input_tokens)}</div>
              <div>${fmtTok(m.output_tokens)}</div>
              <div style="text-align:right;">${fmtUsd(m.cost_usd)}</div>
            </div>`).join('')}` : ''}
      </div>`;
    host.appendChild(details);
  }
}

function _statCard(label, value) {
  return `<div style="${CARD}padding:10px 12px;">
    <div style="font-size:11px;color:var(--fg-3);margin-bottom:2px;">${esc(label)}</div>
    <div style="font-size:15px;color:var(--fg-1);font-weight:600;">${esc(value)}</div>
  </div>`;
}

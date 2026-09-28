'use strict';

/* card-lib.js — shared helpers for dashboard CARD PLUGINS (cards/<id>/card.js).
   The dashboard shell (dashboard.js) and every card module import from here, so
   the stat/gauge/bars/list/sparkline vocabulary and number formatting stay in ONE
   place. Cards should NOT redefine these — import what they need.

   Usage:
     import { stat, fmtNum, getPath, windowLabel } from '../_lib/card-lib.js';

   COLOR SCHEME stays in ui/shared/css/design-system.css — no raw hex here.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { _esc, _escAttr } from '../../../../../shared/js/dom-utils.js';
export { _esc, _escAttr };

// ── number / value formatting ───────────────────────────────────────────────
export function fmtInt(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
export function fmtNum(n) {
  if (n == null) return '—';
  n = Number(n);
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
export function fmtCost(n) { return (n == null) ? '—' : '$' + Number(n).toFixed(Number(n) < 1 ? 4 : 2); }
export function fmtMs(n) { return (n == null) ? '—' : (Number(n) >= 1000 ? (n / 1000).toFixed(2) + 's' : Math.round(n) + 'ms'); }
export function fmtDur(s) {
  if (s == null) return '—';
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
export function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
export function agoLabel(s) {
  if (s == null) return '—';
  return s < 60 ? 'just now' : fmtDur(s) + ' ago';
}
export function agoIso(ts) {
  const t = Date.parse(ts || '');
  return t ? agoLabel(Math.floor((Date.now() - t) / 1000)) : '';
}

// ── tone helpers ────────────────────────────────────────────────────────────
export function cpuTone(v) { v = Number(v) || 0; return v > 85 ? 'danger' : (v > 60 ? 'warning' : 'success'); }
export function latTone(v) { v = Number(v) || 0; return v > 250 ? 'danger' : (v > 120 ? 'warning' : 'success'); }

// ── time-window label (kept in sync with the shell's toolbar select) ────────
let _window = 3600;
export function setWindow(secs) { _window = Number(secs) || 3600; }
export function getWindow() { return _window; }
export function windowLabel() {
  return ({ 300: 'last 5m', 3600: 'last hour', 86400: 'last 24h', 604800: 'last 7d' })[_window] || 'window';
}

// ── metric info popover ────────────────────────────────────────────────────
// Dashboard cards clip their bodies, so the panel itself is portaled to document.body.
let _infoPopover = null;
let _infoTrigger = null;
let _infoInstalled = false;

function _closeMetricInfoPopover() {
  if (_infoTrigger) _infoTrigger.setAttribute('aria-expanded', 'false');
  _infoPopover?.remove();
  _infoPopover = null;
  _infoTrigger = null;
}

function _positionMetricInfoPopover() {
  if (!_infoPopover || !_infoTrigger?.isConnected) return _closeMetricInfoPopover();
  const r = _infoTrigger.getBoundingClientRect();
  const margin = 12;
  const width = _infoPopover.offsetWidth;
  const height = _infoPopover.offsetHeight;
  const left = Math.max(margin, Math.min(window.innerWidth - width - margin, r.left + r.width / 2 - width / 2));
  const above = r.top - height - 9 >= margin;
  _infoPopover.style.left = `${Math.round(left)}px`;
  _infoPopover.style.top = `${Math.round(above ? r.top - height - 9 : r.bottom + 9)}px`;
  _infoPopover.dataset.side = above ? 'top' : 'bottom';
}

function _openMetricInfoPopover(trigger) {
  if (_infoTrigger === trigger) return _closeMetricInfoPopover();
  _closeMetricInfoPopover();
  const pop = document.createElement('section');
  pop.className = 'dash-metric-popover';
  pop.setAttribute('role', 'dialog');
  pop.setAttribute('aria-label', trigger.dataset.dashInfoTitle || 'Metric information');
  const title = document.createElement('strong');
  title.className = 'dash-metric-popover-title';
  title.textContent = trigger.dataset.dashInfoTitle || 'Metric information';
  const body = document.createElement('p');
  body.className = 'dash-metric-popover-body';
  body.textContent = trigger.dataset.dashInfoBody || '';
  pop.append(title, body);
  document.body.appendChild(pop);
  _infoPopover = pop;
  _infoTrigger = trigger;
  trigger.setAttribute('aria-expanded', 'true');
  _positionMetricInfoPopover();
}

export function installMetricInfoPopovers() {
  if (_infoInstalled) return;
  _infoInstalled = true;
  document.addEventListener('click', (event) => {
    const trigger = event.target.closest?.('[data-dash-info-trigger]');
    if (trigger) { event.preventDefault(); _openMetricInfoPopover(trigger); return; }
    if (_infoPopover && !event.target.closest?.('.dash-metric-popover')) _closeMetricInfoPopover();
  });
  document.addEventListener('keydown', (event) => { if (event.key === 'Escape') _closeMetricInfoPopover(); });
  window.addEventListener('resize', _positionMetricInfoPopover);
  document.addEventListener('scroll', _positionMetricInfoPopover, true);
}

export function closeMetricInfoPopover() { _closeMetricInfoPopover(); }

function metricInfoButton(label, help) {
  if (!help) return '';
  return `<button type="button" class="dash-metric-info" data-dash-info-trigger data-dash-info-title="${_escAttr(label)}" data-dash-info-body="${_escAttr(help)}" aria-label="More information about ${_escAttr(label)}" aria-expanded="false"><span aria-hidden="true">i</span></button>`;
}

function metricInfoValue(label, details, value) {
  if (!details?.body) return value;
  const title = details.title || label;
  return `<button type="button" class="dash-metric-value-button" data-dash-info-trigger data-dash-info-title="${_escAttr(title)}" data-dash-info-body="${_escAttr(details.body)}" aria-label="Show calculation details for ${_escAttr(label)}" aria-expanded="false">${value}</button>`;
}

// ── small render helpers (return HTML strings) ─────────────────────────────
export function stat(big, sub, tone) {
  return `<div class="dash-stat ${tone ? 'tone-' + tone : ''}">`
    + `<div class="dash-stat-big">${big}</div>`
    + (sub ? `<div class="dash-stat-sub">${sub}</div>` : '') + `</div>`;
}
export function gauge(pct, label, tone, help) {
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="dash-gauge">`
    + `<div class="dash-gauge-big">${pct.toFixed(0)}<span>%</span></div>`
    + `<div class="dash-gauge-bar"><span class="tone-${tone || 'brand'} dash-bar-fill" data-bar-pct="${pct}"></span></div>`
    + (label ? `<div class="dash-stat-sub dash-gauge-label">${label}${metricInfoButton(label, help)}</div>` : '') + `</div>`;
}
export function bars(rows) {
  const max = Math.max(1, ...rows.map(r => r.value || 0));
  return `<div class="dash-bars">` + rows.map(r =>
    `<div class="dash-bar-row"><span class="dash-bar-label">${r.label}</span>`
    + `<span class="dash-bar-track"><span class="tone-${r.tone || 'brand'} dash-bar-fill" data-bar-pct="${Math.round((r.value || 0) / max * 100)}"></span></span>`
    + `<span class="dash-bar-val">${r.display != null ? r.display : fmtNum(r.value)}</span></div>`
  ).join('') + `</div>`;
}
export function list(rows) {
  if (!rows.length) return `<div class="dash-muted">No data yet.</div>`;
  return `<div class="dash-list">` + rows.map(r =>
    `<div class="dash-list-row"><span class="dash-list-k">${r.k}${metricInfoButton(r.k, r.help)}</span><span class="dash-list-v ${r.tone ? 'tone-' + r.tone : ''}">${metricInfoValue(r.k, r.details, r.v)}</span></div>`
  ).join('') + `</div>`;
}
export function sparkline(points, tone) {
  const vals = points.map(p => p.avg_ms || 0);
  const max = Math.max(1, ...vals);
  const W = 100, H = 34;
  const step = vals.length > 1 ? W / (vals.length - 1) : W;
  const pts = vals.map((v, i) => `${(i * step).toFixed(1)},${(H - (v / max) * H).toFixed(1)}`).join(' ');
  const last = vals.length ? vals[vals.length - 1] : 0;
  return `<div class="dash-spark">`
    + `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" class="tone-${tone || 'brand'}">`
    + `<polyline points="${pts}" fill="none" vector-effect="non-scaling-stroke"/></svg>`
    + `<div class="dash-stat-sub">latest ${fmtMs(last)} · peak ${fmtMs(max)}</div></div>`;
}

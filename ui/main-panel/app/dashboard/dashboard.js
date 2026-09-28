'use strict';

/* Admin Dashboard — movable/resizable grid of live metric cards.
   MOVED from ui/admin-tools/dashboard/ into ui/main-panel/app/dashboard/.
   Now mounted as a tab inside the Instances page's "This device" tile detail
   (alongside Overview and Configuration). The standalone Dashboard admin page
   no longer exists; this module is imported by instances.js which calls
   mountDashboard() to render the toolbar + grid inside the dashboard tab.

   The standalone startView/stopView lifecycle is preserved for backwards
   compatibility but the page.json is set to hidden.

   Breadcrumb: ui/main-panel/app/dashboard/dashboard.html,
   ui/main-panel/app/dashboard/dashboard.css,
   ui/main-panel/app/dashboard/server.py (metrics + layout + AI),
   ui/main-panel/app/dashboard/cards/README.md (CARD PLUGIN contract),
   ui/shared/js/files.js (drop-in view lifecycle),
   ui/shared/css/design-system.css.

   CARDS ARE PLUGINS: every data card is a folder under cards/<id>/ (card.json +
   card.js + optional server.py/card.css), discovered via GET /admin/dashboard/
   cards. DO NOT add data cards to CARD_TYPES below — that registry is only the
   shell framework cards. To add/remove a card, drop/delete its folder.

   COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
   No raw hex/rgb here: colours come from CSS classes/vars in dashboard.css.

   Layout is per-admin (server-side); the LIVE metrics poll a single DB-free
   endpoint so an open dashboard adds no database load. */

import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js?v=253';
import { _esc, _escAttr, _refreshLucideIcons } from '../../../shared/js/dom-utils.js';
import { closeMetricInfoPopover, installMetricInfoPopovers, setWindow } from './cards/_lib/card-lib.js';
import { app } from '../../../shared/js/state.js';
import { createChatWidget } from '../../../chat-widget/js/chat-widget.js';
import { switchToSession } from '../../../chat/js/session-core.js?v=515';
import { readInstanceCache, writeInstanceCache } from '../instance-cache.js';
// The old instance-switcher header (instances-header.js) was removed along with
// the standalone Dashboard page — the Instances page already shows the current
// device, so the embedded dashboard renders no header.

// Host hook: the embedding page (Instances) can set this to receive every
// metrics snapshot (e.g. to keep its own DB/health pills live). No-op by
// default so the 5s poll always proceeds to refreshBodies() — if this were
// undefined, the ReferenceError inside pollLive/poll would swallow the whole
// turn and every data card would spin forever while the hero chart still draws.
function onDashboardSnapshot() {}

const API = '/admin/dashboard';
const COLS = 12;
const ROW_H = 88;   // px per grid row
const GAP = 12;     // px gutter between cards
const POLL_MS = 5000;
const DASH_CACHE_TTL = {
  catalog: 30 * 60 * 1000,
  layout: 24 * 60 * 60 * 1000,
  snapshot: 2 * 60 * 1000,
};
// ── responsive flow (mobile) ──────────────────────────────────────────────────
// The 12-column grid keeps cards legible on a wide panel, but on a narrow one
// each of the 12 columns becomes tiny and cards shrink past readability. Below
// FLOW_BREAKPOINT the grid switches to a wrapping-tile layout: cards keep a
// minimum legible width (FLOW_MIN_CARD) and flow left→right, top→bottom in
// reading order — a card that doesn't fit the remaining row width wraps to the
// next line instead of shrinking. See flowActive / computeFlowRects / positionAll.
const FLOW_BREAKPOINT = 720;   // px of grid content width — below this, linearise
const FLOW_MIN_CARD = 240;     // px — a card is never rendered narrower than this

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch (e) { return ''; } }

// ── state ───────────────────────────────────────────────────────────────────
let _cards = [];
let _snapshot = null;
let _pollTimer = null;
let _editMode = false;
let _window = 3600;
let _started = false;
let _saveTimer = null;
let _ro = null;
const _tsCache = {};   // card id -> { at, points } for chart cards

// ── fetch helpers ────────────────────────────────────────────────────────────
async function _getJSON(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(API + path + sep + 'requesting_user_id=' + encodeURIComponent(_uid())), {
    headers: { ...authHeaders() },
  });
  if (!res.ok) throw new Error('GET ' + path + ' → ' + res.status);
  return res.json();
}
async function _sendJSON(path, method, body) {
  const res = await fetch(apiPath(API + path), {
    method,
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ requesting_user_id: _uid(), ...(body || {}) }),
  });
  if (!res.ok) {
    let detail = res.status;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(String(detail));
  }
  return res.json();
}

// ── number formatting ────────────────────────────────────────────────────────
function fmtInt(n) { return (n == null) ? '—' : Number(n).toLocaleString(); }
function fmtNum(n) {
  if (n == null) return '—';
  n = Number(n);
  if (Math.abs(n) >= 1e9) return (n / 1e9).toFixed(1) + 'B';
  if (Math.abs(n) >= 1e6) return (n / 1e6).toFixed(1) + 'M';
  if (Math.abs(n) >= 1e3) return (n / 1e3).toFixed(1) + 'k';
  return String(Math.round(n));
}
function fmtCost(n) { return (n == null) ? '—' : '$' + Number(n).toFixed(Number(n) < 1 ? 4 : 2); }
function fmtMs(n) { return (n == null) ? '—' : (Number(n) >= 1000 ? (n / 1000).toFixed(2) + 's' : Math.round(n) + 'ms'); }
function fmtDur(s) {
  if (s == null) return '—';
  s = Math.floor(s);
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  if (d) return d + 'd ' + h + 'h';
  if (h) return h + 'h ' + m + 'm';
  return m + 'm';
}
function getPath(obj, path) {
  return path.split('.').reduce((o, k) => (o == null ? undefined : o[k]), obj);
}
function agoLabel(s) {
  if (s == null) return '—';
  return s < 60 ? 'just now' : fmtDur(s) + ' ago';
}
function agoIso(ts) {
  const t = Date.parse(ts || '');
  return t ? agoLabel(Math.floor((Date.now() - t) / 1000)) : '';
}

// ── small render helpers (return HTML strings) ───────────────────────────────
function stat(big, sub, tone) {
  return `<div class="dash-stat ${tone ? 'tone-' + tone : ''}">`
    + `<div class="dash-stat-big">${big}</div>`
    + (sub ? `<div class="dash-stat-sub">${sub}</div>` : '') + `</div>`;
}
function gauge(pct, label, tone) {
  pct = Math.max(0, Math.min(100, Number(pct) || 0));
  return `<div class="dash-gauge">`
    + `<div class="dash-gauge-big">${pct.toFixed(0)}<span>%</span></div>`
    + `<div class="dash-gauge-bar"><span class="tone-${tone || 'brand'} dash-bar-fill" data-bar-pct="${pct}"></span></div>`
    + (label ? `<div class="dash-stat-sub">${label}</div>` : '') + `</div>`;
}
function bars(rows) {
  const max = Math.max(1, ...rows.map(r => r.value || 0));
  return `<div class="dash-bars">` + rows.map(r =>
    `<div class="dash-bar-row"><span class="dash-bar-label">${r.label}</span>`
    + `<span class="dash-bar-track"><span class="tone-${r.tone || 'brand'} dash-bar-fill" data-bar-pct="${Math.round((r.value || 0) / max * 100)}"></span></span>`
    + `<span class="dash-bar-val">${r.display != null ? r.display : fmtNum(r.value)}</span></div>`
  ).join('') + `</div>`;
}
function list(rows) {
  if (!rows.length) return `<div class="dash-muted">No data yet.</div>`;
  return `<div class="dash-list">` + rows.map(r =>
    `<div class="dash-list-row"><span class="dash-list-k">${r.k}</span><span class="dash-list-v ${r.tone ? 'tone-' + r.tone : ''}">${r.v}</span></div>`
  ).join('') + `</div>`;
}
function sparkline(points, tone) {
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

// ── card registry ────────────────────────────────────────────────────────────
// FRAMEWORK cards live here (special shell-built bodies). Every DATA card is a
// PLUGIN folder under cards/<id>/ (card.json + card.js + optional server.py) —
// the same drop-in philosophy as the ui/admin-tools page folders. cardDef()
// resolves a card type: the plugin descriptor (from GET /admin/dashboard/cards)
// wins, then this registry, then the generic `custom` fallback.
// ⛔ DO NOT add data cards here — that is hardcoding. Drop a folder under
// cards/<id>/ instead (see cards/README.md); the catalog + lazy import pick it
// up automatically. This registry holds ONLY the shell framework cards above.
const CARD_TYPES = {
  // The full-width "Metrics over time" hero chart. Its body is built ONCE
  // (controls + SVG) by buildMetricChart and refreshed in place by
  // refreshMetricChart, so the 5s metrics poll never wipes its controls.
  metric_chart: {
    title: 'Metrics over time', icon: 'line-chart', w: 12, h: 5, live: true, hero: true, render: () => '',
  },
  custom: {
    title: 'Custom', icon: 'sparkles', w: 3, h: 2,
    render: (s, card) => renderCustom(s, card),
  },
  // The "Add a card" tile — a permanent card whose + button opens the plugin
  // picker and whose chat pill generates an AI card. Its body is built once by
  // buildAdder() (see renderCardBody) so polling never wipes it.
  add_card: {
    title: 'Add a card', icon: 'plus', w: 4, h: 3, adder: true, render: () => '',
  },
};

// ── plugin registry (cards/<id>/) ────────────────────────────────────────────
// Descriptors come from GET /admin/dashboard/cards (scanned from card.json by
// dashboard/server.py). Render modules are imported lazily from
// ./cards/<id>/card.js on first paint of that card.
const _pluginDefs = {};        // type -> descriptor
const _pluginRenders = {};     // type -> promise of the imported card module
let _catalogLoaded = false;

// A card's optional card.css is auto-injected here (idempotent, keyed by href)
// so dropping a folder needs NO page.json css edit — true drop-in styling.
function _injectCardCss(id) {
  const href = apiPath('/ui/main-panel/app/dashboard/cards/' + id + '/card.css');
  if (document.querySelector('link[data-card-css="' + id + '"]')) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  link.dataset.cardCss = id;
  document.head.appendChild(link);
}

async function loadCardCatalog() {
  const cached = readInstanceCache('dashboard:catalog');
  if (!_catalogLoaded && Array.isArray(cached)) _applyCardCatalog(cached);
  try {
    const j = await _getJSON('/cards');
    const cards = Array.isArray(j.cards) ? j.cards : [];
    _applyCardCatalog(cards);
    writeInstanceCache('dashboard:catalog', cards, DASH_CACHE_TTL.catalog);
  } catch (e) { /* non-fatal: framework cards still work */ }
}
function _applyCardCatalog(cards) {
  for (const c of cards) {
    _pluginDefs[c.id] = c;
    if (c.hasCss) _injectCardCss(c.id);
  }
  _catalogLoaded = true;
  refreshBodies();   // re-evaluate readiness with the real section needs
  refreshCharts();   // pick up chart-cards (def.chart arrives with the catalog)
  _refreshLucideIcons();
}
function cardDef(type) {
  return _pluginDefs[type] || CARD_TYPES[type] || null;
}

// Picker order: the hero chart first, then plugins sorted by their card.json
// `order`. Built lazily when the + button first opens — by then the catalog has
// usually landed; the fallback list keeps the picker usable offline.
const _PICKER_FALLBACK = ['metric_chart', 'cost', 'tokens', 'token_rate', 'active_runs', 'db_mode', 'cpu', 'memory',
  'db_latency', 'db_latency_chart', 'loop_split', 'context', 'tokens_by_agent', 'agents_panel',
  'sessions_monitor', 'user_mgmt', 'security', 'tool_usage', 'devices', 'failures', 'health_board', 'uptime', 'storage'];
function pickerTypes() {
  if (_catalogLoaded && Object.keys(_pluginDefs).length) {
    return ['metric_chart'].concat(Object.values(_pluginDefs)
      .sort((a, b) => (a.order - b.order) || a.id.localeCompare(b.id)).map(d => d.id));
  }
  return _PICKER_FALLBACK;
}





// ── the "Metrics over time" hero chart — series + palette ────────────────────
// Selectable series for the full-width chart. Series are grouped by UNIT into
// stacked LANES that share one time axis (small multiples — never two scales on
// one plot): `cost` is a stacked-bar lane (one segment per model), every other
// unit group is a line lane with its own real, zero-based Y axis.
// `tone` is the series' FIXED identity colour — a design-system --chart-* token
// (validated categorical set; see design-system.css). It follows the series id,
// never the current selection, so toggling series never repaints survivors.
// `errors` deliberately wears the status danger hue: that colour MEANS "bad".
const CHART_SERIES = [
  { id: 'cost',        label: 'Cost per model',   kind: 'bars', unit: '$' },
  { id: 'tokens_in',   label: 'Tokens in',        kind: 'line', unit: 'tok',   tone: '--chart-1' },
  { id: 'tokens_out',  label: 'Tokens out',       kind: 'line', unit: 'tok',   tone: '--chart-2' },
  { id: 'llm_calls',   label: 'LLM calls',        kind: 'line', unit: 'calls', tone: '--chart-3' },
  { id: 'errors',      label: 'Errors',           kind: 'line', unit: 'calls', tone: '--danger' },
  { id: 'active_runs', label: 'Active runs',      kind: 'line', unit: 'runs',  tone: '--chart-6' },
  { id: 'db_avg_ms',   label: 'DB latency (avg)', kind: 'line', unit: 'ms',    tone: '--chart-1' },
  { id: 'db_p95_ms',   label: 'DB latency (p95)', kind: 'line', unit: 'ms',    tone: '--chart-5' },
  { id: 'cpu',         label: 'CPU',              kind: 'line', unit: '%',     tone: '--chart-4' },
  { id: 'ram',         label: 'RAM',              kind: 'line', unit: 'MB',    tone: '--chart-5' },
];
const CHART_DEFAULT_SERIES = ['tokens_in', 'tokens_out', 'cost'];
// Colour slots for the per-model COST bar segments — the validated categorical
// set from design-system.css, assigned in cost order (largest first); the
// folded "Other" bucket wears the muted de-emphasis ink instead.
const CHART_TONES = ['--chart-1', '--chart-2', '--chart-3', '--chart-4', '--chart-5', '--chart-6'];
// Lane order (top → bottom) when several unit groups are visible.
const CHART_LANE_ORDER = ['$', 'tok', 'calls', 'runs', 'ms', '%', 'MB'];
const CHART_LANE_LABEL = {
  '$': 'Cost', tok: 'Tokens', calls: 'Count', runs: 'Active runs',
  ms: 'Latency ms', '%': 'CPU %', MB: 'RAM MB',
};
const CHART_WINDOWS = [
  { s: 300, label: '5m' }, { s: 3600, label: '1h' },
  { s: 86400, label: '24h' }, { s: 604800, label: '7d' },
];
const _chartCache = {};   // card id -> last /chart response (for redraw without refetch)
const _chartUi = {};      // card id -> transient UI state { hidden:Set, zoomStack:[] }

function chartUi(card) {
  if (!_chartUi[card.id]) _chartUi[card.id] = { hidden: new Set(), zoomStack: [] };
  return _chartUi[card.id];
}

function cpuTone(v) { v = Number(v) || 0; return v > 85 ? 'danger' : (v > 60 ? 'warning' : 'success'); }
function latTone(v) { v = Number(v) || 0; return v > 250 ? 'danger' : (v > 120 ? 'warning' : 'success'); }
function windowLabel() {
  return ({ 300: 'last 5m', 3600: 'last hour', 86400: 'last 24h', 604800: 'last 7d' })[_window] || 'window';
}

function renderCustom(s, card) {
  const fields = card.fields || [];
  if (card.viz === 'stat' && fields.length) {
    const f = fields[0];
    return stat(fmtNum(getPath(s, f.path)) + (f.unit ? ' <span class="dash-unit">' + _esc(f.unit) + '</span>' : ''), _esc(f.label), 'brand');
  }
  return list(fields.map(f => {
    let v = getPath(s, f.path);
    if (typeof v === 'boolean') v = v ? 'yes' : 'no';
    else if (typeof v === 'number') v = fmtNum(v);
    else if (Array.isArray(v)) v = v.length;
    else v = (v == null ? '—' : _esc(String(v)));
    return { k: _esc(f.label), v: v + (f.unit ? ' ' + _esc(f.unit) : '') };
  }));
}

// ── DOM refs ─────────────────────────────────────────────────────────────────
function $(id) { return document.getElementById(id); }
let elGrid, elWrap, elEmpty;

// ── grid geometry + rendering ────────────────────────────────────────────────
function gridContentW() {
  // Measure the GRID's own content box, not the scroll wrap: elWrap.clientWidth
  // still includes the wrap's 14px L/R padding (28px), whereas the cards live
  // inside .dash-grid (width:100% of that content box). elGrid.clientWidth also
  // already subtracts the vertical scrollbar, so the layout can't spill past the
  // right edge. Fall back to the wrap minus its padding if the grid isn't
  // measurable yet (0-width during the Admin Tools view switch-in).
  const gw = elGrid && elGrid.clientWidth;
  return (gw > 6 ? gw : (((elWrap && elWrap.clientWidth) || 1000) - 28)) - 2;
}
function cellW() {
  const cw = (gridContentW() - GAP * (COLS - 1)) / COLS;
  return cw > 6 ? cw : 6;   // never negative/tiny — a hidden/0-width panel would
                            // otherwise write negative card widths (rejected by
                            // the browser) and stack every card at the origin.
}
// True when the panel is too narrow for the 12-col grid to stay legible, so the
// grid linearises into wrapping tiles (see computeFlowRects). Measured once the
// panel has a real width — a 0-width switch-in reports the fallback (1000px),
// which is wide, so we don't flash the flow layout during the view transition.
function flowActive() {
  const w = gridContentW();
  return w > 6 && w < FLOW_BREAKPOINT;
}
// Lay every card out as a wrapping tile: cards flow left→right, top→bottom in
// reading order (their saved y, then x), each keeping a minimum legible width.
// The panel is split into as many equal columns as fit at FLOW_MIN_CARD; an
// authored 12-col width maps to a proportional column span (never below 1, never
// past the row), and a card that doesn't fit the remaining columns wraps to the
// next line. Returns a card.id → {left, top, width, height} map + the total
// height. Heights are preserved (card.h rows) so card internals stay readable.
function computeFlowRects() {
  const availW = Math.max(FLOW_MIN_CARD, gridContentW());
  const fcols = Math.max(1, Math.floor((availW + GAP) / (FLOW_MIN_CARD + GAP)));
  const colW = (availW - GAP * (fcols - 1)) / fcols;
  const order = _cards.slice().sort((a, b) => (a.y - b.y) || (a.x - b.x));
  const rects = new Map();
  let rowTop = 0, col = 0, rowMaxH = 0;
  for (const card of order) {
    let span = Math.max(1, Math.min(fcols, Math.round(card.w / COLS * fcols)));
    if (col > 0 && col + span > fcols) { rowTop += rowMaxH + GAP; col = 0; rowMaxH = 0; }
    const width = Math.round(span * colW + (span - 1) * GAP);
    const height = Math.round(card.h * ROW_H + (card.h - 1) * GAP);
    rects.set(card.id, { left: Math.round(col * (colW + GAP)), top: rowTop, width, height });
    col += span;
    if (height > rowMaxH) rowMaxH = height;
    if (col >= fcols) { rowTop += rowMaxH + GAP; col = 0; rowMaxH = 0; }
  }
  return { rects, totalH: rowTop + rowMaxH };
}
// The panel is often still 0-width while the Admin Tools view is switching in;
// positioning then is wrong. Defer the first render until it has a real width.
function sized() { return !!(elWrap && elWrap.clientWidth > 120); }
function whenSized(cb, tries) {
  tries = tries || 0;
  if (sized() || tries > 60) { cb(); return; }
  requestAnimationFrame(() => whenSized(cb, tries + 1));
}
function place(el, card) {
  const cw = cellW();
  el.style.left = Math.round(card.x * (cw + GAP)) + 'px';
  el.style.top = Math.round(card.y * (ROW_H + GAP)) + 'px';
  el.style.width = Math.round(card.w * cw + (card.w - 1) * GAP) + 'px';
  el.style.height = Math.round(card.h * ROW_H + (card.h - 1) * GAP) + 'px';
}
function gridHeight() {
  const maxRow = _cards.reduce((m, c) => Math.max(m, c.y + c.h), 6);
  return maxRow * (ROW_H + GAP);
}

function cardShell(card) {
  const def = cardDef(card.type) || CARD_TYPES.custom;
  const el = document.createElement('div');
  el.className = 'dash-card';
  el.dataset.id = card.id;
  el.dataset.type = card.type;
  // The adder tile is permanent — draggable/resizable, but no remove button so
  // an admin can't accidentally delete their only way to add cards.
  const removeBtn = def.adder ? ''
    : `<button class="dash-card-btn dash-card-remove" title="Remove card"><i data-lucide="x" class="dash-ico-14"></i></button>`;
  // AI star — ALWAYS visible (it lives OUTSIDE .dash-card-tools, which only shows
  // in edit mode). Opens a WebAgent chat pre-primed with this exact card's identity
  // + where its front/back-end live, so the admin can just say what to change
  // (openCardAssistant). The adder tile has its own AI pill → no star on it.
  const aiBtn = def.adder ? ''
    : `<button class="dash-card-btn dash-card-ai" title="Ask WebAgent to update this card"><i data-lucide="sparkles" class="dash-ico-14"></i></button>`;
  el.innerHTML =
    `<div class="dash-card-head">`
    + `<span class="dash-card-title"><i data-lucide="${_escAttr((card.icon || def.icon))}" class="dash-ico-14"></i>${_esc(card.title || def.title || def.label)}</span>`
    + `<span class="dash-card-tools">`
    + removeBtn
    + `</span>`
    + aiBtn
    + `</div>`
    + `<div class="dash-card-body"></div>`
    + `<span class="dash-card-resize" data-edge="n" title="Drag to resize vertically"></span>`
    + `<span class="dash-card-resize" data-edge="e" title="Drag to resize horizontally"></span>`
    + `<span class="dash-card-resize" data-edge="s" title="Drag to resize vertically"></span>`
    + `<span class="dash-card-resize" data-edge="w" title="Drag to resize horizontally"></span>`;
  place(el, card);
  return el;
}

// ── per-card loading gate ─────────────────────────────────────────────────────
// A card shows a spinner until the snapshot section(s) it reads have arrived, so
// cards fill in AS their data lands instead of all at once. The fast first poll
// (/metrics?live=1) returns only the in-memory / filesystem sections — live,
// memory_mb, db_health, storage — so those cards populate near-instantly while
// the DB-backed cards keep spinning until the full poll() returns. Keys below are
// top-level snapshot sections (server.py _build_snapshot); keep in sync when a
// card starts reading a new section. Cards not listed (hero chart, adder, the
// timeseries spark) build their own body and are never gated here.
function cardNeeds(card) {
  if (card.type === 'custom') {
    // A custom card is ready once the section(s) behind its fields have arrived.
    return [...new Set((card.fields || [])
      .map(f => String(f.path || '').split('.')[0]).filter(Boolean))];
  }
  const def = cardDef(card.type);
  if (def && Array.isArray(def.sections)) return def.sections;
  // Framework card (hero/adder): no data gate. Unknown type (catalog not loaded
  // yet): wait for the catalog so we never render a card "ready" against an
  // incomplete snapshot.
  return CARD_TYPES[card.type] ? [] : ['__catalog_pending__'];
}
// True once every snapshot section this card reads is present. Until then the
// per-card spinner shows. Cards with no declared needs (e.g. db_latency_chart)
// become ready as soon as ANY snapshot lands — their render() then shows its own
// "collecting…" placeholder.
function cardReady(card) {
  if (!_snapshot) return false;
  return cardNeeds(card).every(k => _snapshot[k] != null);
}
const _CARD_SPINNER = '<div class="dash-loading-card"><span class="dash-spinner"></span></div>';

// CSP: convert data-bar-pct/data-leg-bg/data-tip-bg to CSS custom properties
function _postRender(body) {
  body.querySelectorAll('[data-bar-pct]').forEach(el => {
    el.style.setProperty('--bar-w', el.dataset.barPct + '%');
  });
  body.querySelectorAll('[data-leg-bg]').forEach(el => {
    el.style.setProperty('--leg-bg', 'var(' + el.dataset.legBg + ')');
  });
  body.querySelectorAll('[data-tip-bg]').forEach(el => {
    el.style.setProperty('--tip-bg', el.dataset.tipBg);
  });
  _refreshLucideIcons();
}

// Per-card render context handed to plugin render(snap, ctx): transient data
// the shell owns (timeseries points for chart cards, the hero chart cache).
function cardCtx(card) {
  return { ts: _tsCache[card.id], chart: _chartCache[card.id], window: _window };
}

// Render a PLUGIN card body. The module (./cards/<id>/card.js) is imported on
// first paint; until it (and the catalog) arrive the card shows the spinner.
function _renderPluginBody(el, card, mod) {
  closeMetricInfoPopover();
  const body = el.querySelector('.dash-card-body');
  if (!body) return;
  if (!mod || typeof mod.render !== 'function') {
    body.innerHTML = '<div class="dash-muted">Card failed to load.</div>';
    return;
  }
  body.classList.remove('is-loading');
  try {
    body.innerHTML = mod.render(_snapshot, cardCtx(card));
  } catch (e) {
    body.innerHTML = `<div class="dash-muted">Card error.</div>`;
  }
  _postRender(body);
}

function renderCardBody(el, card) {
  const def = cardDef(card.type) || CARD_TYPES.custom;
  const body = el.querySelector('.dash-card-body');
  if (!body) return;
  if (def.hero) {
    // Build the chart shell + controls once; the metrics poll must NOT re-render
    // it (that would tear down the SVG / close the metrics menu). refreshMetricChart
    // re-fetches and redraws the SVG in place on its own cadence.
    if (!body.dataset.heroBuilt) { buildMetricChart(body, card); body.dataset.heroBuilt = '1'; refreshMetricChart(card); }
    return;
  }
  if (def.adder) {
    // Build the pill + inline picker once; the 5s metrics poll must NOT re-render
    // it (that would clear a half-typed prompt or close the open picker).
    if (!body.dataset.adderBuilt) { buildAdder(body); body.dataset.adderBuilt = '1'; }
    return;
  }
  if (!cardReady(card)) {
    // Data for this card hasn't arrived yet — show the spinner. Inject it only
    // ONCE (re-writing it every 5s poll would restart the CSS animation); it
    // stays until this card's snapshot section lands.
    if (!body.classList.contains('is-loading')) {
      body.classList.add('is-loading');
      body.innerHTML = _CARD_SPINNER;
    }
    return;
  }
  body.classList.remove('is-loading');
  // Plugin card → lazy-import its render module from its own folder.
  if (_pluginDefs[card.type]) {
    const mod = _pluginRenders[card.type];
    if (mod && typeof mod.then === 'function') {
      mod.then(m => { if (elGrid && elGrid.contains(el)) _renderPluginBody(el, card, m); });
      if (!body.classList.contains('is-loading')) {
        body.classList.add('is-loading');
        body.innerHTML = _CARD_SPINNER;
      }
      return;
    }
    if (!mod) {
      _pluginRenders[card.type] = import('./cards/' + card.type + '/card.js')
        .then(m => m.default || {})
        .catch(e => { console.error('dashboard card ' + card.type + ' failed to load', e); return {}; });
      if (!body.classList.contains('is-loading')) {
        body.classList.add('is-loading');
        body.innerHTML = _CARD_SPINNER;
      }
      _pluginRenders[card.type].then(m => { if (elGrid && elGrid.contains(el)) _renderPluginBody(el, card, m); });
      return;
    }
    _renderPluginBody(el, card, mod);
    return;
  }
  try {
    body.innerHTML = def.render(_snapshot, card);
  } catch (e) {
    body.innerHTML = `<div class="dash-muted">Card error.</div>`;
  }
  _postRender(body);
}

function dashboardCards() {
  return _cards.filter(card => !cardDef(card.type)?.adder);
}

function syncDashboardList() {
  const cards = dashboardCards();
  const count = $('dash-dashboard-count');
  const items = $('dash-dashboard-list-items');
  if (count) count.textContent = `${cards.length} Dashboard${cards.length === 1 ? '' : 's'}`;
  if (!items) return;
  items.innerHTML = cards.length
    ? cards.map(card => {
      const def = cardDef(card.type) || CARD_TYPES.custom;
      const title = card.title || def.title || def.label || card.type;
      return `<button type="button" class="dash-dashboard-list-item" data-card-id="${_escAttr(card.id)}"><i data-lucide="${_escAttr(card.icon || def.icon || 'layout-dashboard')}" class="dash-ico-14"></i><span>${_esc(title)}</span></button>`;
    }).join('')
    : '<div class="dash-dashboard-list-empty">No dashboards yet.</div>';
  _refreshLucideIcons(items);
}

function renderAll() {
  if (!elGrid) return;
  // The former inline Add card mock tile is retired. Remove legacy entries from
  // saved layouts and persist the cleaned layout without requiring user action.
  const hadLegacyAdder = _cards.some(card => card.type === 'add_card');
  if (hadLegacyAdder) {
    _cards = _cards.filter(card => card.type !== 'add_card');
    scheduleSave();
  }
  elGrid.innerHTML = '';
  for (const card of _cards) {
    const el = cardShell(card);
    elGrid.appendChild(el);
    renderCardBody(el, card);
  }
  // cardShell placed each card for the wide 12-col grid; repositionAll re-lays
  // them (and sets the grid height) honouring flow mode when the panel is narrow.
  repositionAll();
  elGrid.classList.toggle('edit', _editMode);
  if (elEmpty) elEmpty.hidden = _cards.length > 0;
  syncDashboardList();
  _refreshLucideIcons();
}

function refreshBodies() {
  if (!elGrid) return;
  for (const card of _cards) {
    const el = elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
    if (el) renderCardBody(el, card);
  }
}

function repositionAll() {
  if (!elGrid) return;
  const flow = flowActive();
  elGrid.classList.toggle('flow', flow);
  if (flow) {
    // Narrow panel: linearise into wrapping tiles that keep a legible min width.
    const { rects, totalH } = computeFlowRects();
    for (const card of _cards) {
      const el = elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
      const r = el && rects.get(card.id);
      if (r) {
        el.style.left = r.left + 'px'; el.style.top = r.top + 'px';
        el.style.width = r.width + 'px'; el.style.height = r.height + 'px';
      }
    }
    elGrid.style.height = totalH + 'px';
    return;
  }
  for (const card of _cards) {
    const el = elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
    if (el) place(el, card);
  }
  elGrid.style.height = gridHeight() + 'px';
}

// ── collision-aware reflow ───────────────────────────────────────────────────
// When a card is moved or resized it must not sit on top of others. `reflow`
// re-flows the whole grid so cards make way and settle upward, like a magnetic
// layout. The active card is "pinned" — it holds exactly where the user put it,
// and every other card drops from the top into the first slot that clears both
// the pinned card and the cards already settled this pass. Dropping-until-clear
// (rather than only checking the row above) is what guarantees zero overlaps
// even with mixed-width cards that can only rise partway.
function overlaps(a, b) {
  return a.x < b.x + b.w && a.x + a.w > b.x && a.y < b.y + b.h && a.y + a.h > b.y;
}
function reflow(activeId) {
  const placed = [];
  const pin = _cards.find(c => c.id === activeId);
  if (pin) placed.push(pin);            // pinned card holds its slot, always avoided
  const rest = _cards.filter(c => c.id !== activeId)
    .sort((a, b) => (a.y - b.y) || (a.x - b.x));
  for (const c of rest) {
    let y = 0;
    while (placed.some(p => overlaps({ x: c.x, y, w: c.w, h: c.h }, p))) y++;
    c.y = y;
    placed.push(c);
  }
}

// ── drag + resize (edit mode) ────────────────────────────────────────────────
let _drag = null;
function onPointerDown(e) {
  if (!_editMode) return;
  // In flow mode cards are auto-arranged (wrapping tiles), so drag-to-move and
  // corner-resize don't map to the 12-col grid — leave them inert there.
  if (flowActive()) return;
  const resizeH = e.target.closest('.dash-card-resize');
  const control = e.target.closest('button, a, input, select, textarea, [contenteditable="true"]');
  // In edit mode, the card surface itself is the move handle. Preserve normal
  // behavior for per-card controls; the resize edge zones take precedence.
  if (!resizeH && control) return;
  const el = e.target.closest('.dash-card');
  if (!el) return;
  const card = _cards.find(c => c.id === el.dataset.id);
  if (!card) return;
  e.preventDefault();
  el.classList.add('dragging');
  const cw = cellW();
  _drag = {
    el, card, mode: resizeH ? 'resize' : 'move', edge: resizeH?.dataset.edge || '', cw,
    startX: e.clientX, startY: e.clientY,
    ox: card.x, oy: card.y, ow: card.w, oh: card.h,
  };
  document.addEventListener('pointermove', onPointerMove);
  document.addEventListener('pointerup', onPointerUp, { once: true });
}
function onPointerMove(e) {
  if (!_drag) return;
  const { card, cw, mode } = _drag;
  const dx = Math.round((e.clientX - _drag.startX) / (cw + GAP));
  const dy = Math.round((e.clientY - _drag.startY) / (ROW_H + GAP));
  if (mode === 'move') {
    card.x = Math.max(0, Math.min(COLS - card.w, _drag.ox + dx));
    card.y = Math.max(0, _drag.oy + dy);
  } else {
    // Each edge keeps its opposite edge anchored, so a left/top resize grows
    // toward the pointer without shifting the far side of the card.
    if (_drag.edge === 'e') {
      card.w = Math.max(2, Math.min(COLS - _drag.ox, _drag.ow + dx));
    } else if (_drag.edge === 'w') {
      const x = Math.max(0, Math.min(_drag.ox + _drag.ow - 2, _drag.ox + dx));
      card.x = x;
      card.w = _drag.ow + _drag.ox - x;
    } else if (_drag.edge === 's') {
      card.h = Math.max(1, Math.min(12, _drag.oh + dy));
    } else if (_drag.edge === 'n') {
      const y = Math.max(0, Math.min(_drag.oy + _drag.oh - 1, _drag.oy + dy));
      card.y = y;
      card.h = _drag.oh + _drag.oy - y;
    }
  }
  reflow(card.id);        // flow other cards out of the way + settle upward
  repositionAll();        // redraw every card at its (possibly shuffled) slot
}
function onPointerUp() {
  if (!_drag) return;
  _drag.el.classList.remove('dragging');
  const wasResize = _drag.mode === 'resize';
  _drag = null;
  document.removeEventListener('pointermove', onPointerMove);
  if (wasResize) { refreshBodies(); redrawMetricChartsFromCache(); }   // re-fit at new size
  scheduleSave();
}

// ── persistence ──────────────────────────────────────────────────────────────
function scheduleSave() {
  if (_saveTimer) clearTimeout(_saveTimer);
  _saveTimer = setTimeout(() => {
    _saveTimer = null;
    const cards = slimCards();
    writeInstanceCache('dashboard:layout', cards, DASH_CACHE_TTL.layout);
    _sendJSON('/layout', 'PUT', { cards }).catch(e => console.error('dashboard save failed', e));
  }, 600);
}

// ── add / remove cards ───────────────────────────────────────────────────────
function nextFreeSpot(w, h) {
  // Simple placement: first row-major slot that doesn't overlap an existing card.
  for (let y = 0; y < 200; y++) {
    for (let x = 0; x <= COLS - w; x++) {
      if (!_cards.some(c => x < c.x + c.w && x + w > c.x && y < c.y + c.h && y + h > c.y)) return { x, y };
    }
  }
  return { x: 0, y: 0 };
}
function addCard(spec) {
  const def = cardDef(spec.type) || CARD_TYPES.custom;
  const w = spec.w || def.w, h = spec.h || def.h;
  const spot = nextFreeSpot(w, h);
  const card = { id: 'c-' + Math.random().toString(36).slice(2, 9), type: spec.type, x: spot.x, y: spot.y, w, h };
  if (spec.type === 'custom') { card.title = spec.title; card.icon = spec.icon; card.viz = spec.viz; card.fields = spec.fields; }
  if (spec.type === 'metric_chart') {
    card.series = (Array.isArray(spec.series) && spec.series.length) ? spec.series.slice() : CHART_DEFAULT_SERIES.slice();
    card.range = spec.range || { window: 3600 };
  }
  _cards.push(card);
  renderAll();
  scheduleSave();
  if (def.chart) refreshChart(card);
}
function removeCard(id) {
  _cards = _cards.filter(c => c.id !== id);
  renderAll();
  scheduleSave();
}

// ── per-card AI assistant (the star button) ──────────────────────────────────
// Every card's star opens a floating WebAgent (manager) chat that is PRE-PRIMED
// with which card this is and where its front-end renderer + back-end metrics
// live — so the admin can just say “change the title”, “swap the icon”, “point
// this at a different number” and the agent knows exactly what to edit. The
// priming text rides the admin's FIRST message invisibly (transformMessage): the
// chat bubble shows only what they typed, not the wall of file pointers.
function cardAssistantMessage(card) {
  const def = cardDef(card.type) || CARD_TYPES.custom;
  const title = card.title || def.title || def.label;
  const L = [];
  L.push('[WebAgent Request | Source: Admin Tools → Dashboard]');
  L.push('The admin clicked the AI star on ONE dashboard card and wants to change that specific card. You are editing the WebAgent codebase.');
  L.push('');
  L.push('CARD BEING UPDATED');
  L.push('• Title on the card: "' + title + '"');
  L.push('• Card type id: ' + card.type);
  if (card.type === 'custom' && Array.isArray(card.fields) && card.fields.length) {
    L.push('• Custom-card fields (label ← data path):');
    card.fields.forEach(f => L.push('    - "' + (f.label || '') + '" ← ' + (f.path || '') + (f.unit ? ' [' + f.unit + ']' : '')));
    if (card.viz) L.push('• Visualisation: ' + card.viz);
  }
  L.push('');
  L.push('WHERE THIS CARD IS DEFINED');
  const pdef = _pluginDefs[card.type];
  if (pdef) {
    L.push('• This card is a PLUGIN: ui/main-panel/app/dashboard/cards/' + card.type + '/');
    L.push('  - card.json — descriptor (label, icon, default w/h, live, sections, chart, section).');
    L.push('  - card.js — render(snapshot, ctx) builds the card body; imports helpers from ../_lib/card-lib.js.');
    if (pdef.hasServer) L.push('  - server.py — build_section(ctx) contributes the \'' + (pdef.section || '') + '\' snapshot section; helpers from ../_lib/server-lib.py (via dashboard_server_lib).');
    if (pdef.hasCss) L.push('  - card.css — card-specific styles (design-system tokens only).');
    if (pdef.aiHint) L.push('• Card author hint: ' + pdef.aiHint);
    L.push('• The shell: ui/main-panel/app/dashboard/dashboard.js (grid, layout, polling) + dashboard/server.py (snapshot + layout + /cards catalog).');
  } else {
    L.push("• Front-end renderer + registry: ui/main-panel/app/dashboard/dashboard.js → CARD_TYPES['" + card.type + "'] — its render() builds the card body; the default title, Lucide icon and grid size are in the same entry.");
    L.push('• Back-end data: ui/main-panel/app/dashboard/server.py → _build_snapshot() and its section builders (tokens, db_health, active_runs, devices, failures, context, storage, agents, users, sessions, health, tool_usage, security). The always-fresh live gauges come from app/metrics.py; the metric paths a card may read are listed in server.py as _AI_FIELDS.');
    L.push('• Styling: ui/main-panel/app/dashboard/dashboard.css.');
  }
  L.push('• Saved layout: per-admin in data/config/dashboard-layouts.json (card id → x,y,w,h = order + sizing); the default seed is app/defaults/dashboard.json.');
  L.push('');
  L.push('Briefly confirm which card you are working on, then ask what they would like to change (or just make the change if they have already said).');
  return L.join('\n');
}

function openCardAssistant(card) {
  const def = cardDef(card.type) || CARD_TYPES.custom;
  const title = card.title || def.title || def.label;
  const context = cardAssistantMessage(card);
  let primed = false;
  const w = createChatWidget({
    title: 'Dashboard · ' + title,
    iconName: 'sparkles',
    ensureAgent: app.startWebagentSession,
    // Wrap the admin's FIRST message only: their words go to the agent under the
    // card context so it knows the card; later replies pass through untouched.
    transformMessage: async (text) => {
      if (primed) return text;
      primed = true;
      return context + '\n\nThe admin asked:\n' + text;
    },
  });
  w.open();
}

// ── operational card actions (rows + buttons carrying data-act) ─────────────
// Reveal the chat side-panel if hidden (same best-effort helpers the Agents
// page uses — they live in index.html).
function revealChatPanel() {
  try {
    if (typeof window.__getChatVisible === 'function' && !window.__getChatVisible()
        && typeof window.__applyChatVisible === 'function') {
      window.__applyChatVisible(true);
    }
  } catch (e) { /* best-effort */ }
}

async function _adminUsersPost(path, method) {
  const res = await fetch(apiPath('/admin/users/' + path
    + (method === 'DELETE' ? '?requesting_user_id=' + encodeURIComponent(_uid()) : '')), {
    method: method || 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: method === 'DELETE' ? undefined : JSON.stringify({ requesting_user_id: _uid() }),
  });
  if (!res.ok) {
    let detail = res.status;
    try { detail = (await res.json()).detail || detail; } catch (e) { /* keep status */ }
    throw new Error(String(detail));
  }
}

function handleCardAction(btn) {
  const act = btn.dataset.act;
  const id = btn.dataset.id || '';
  const busy = (on) => { btn.classList.toggle('busy', on); if (btn.tagName === 'BUTTON') btn.disabled = on; };
  const refreshSoon = () => { setTimeout(poll, 400); };
  try {
    if (act === 'open-agent') {
      revealChatPanel();
      if (typeof app.switchToAgent === 'function') app.switchToAgent(id);
      return;
    }
    if (act === 'open-session') {
      revealChatPanel();
      switchToSession(id);
      return;
    }
    if (act === 'stop-run') {
      if (!window.confirm('Stop this agent run?')) return;
      busy(true);
      _sendJSON('/runs/stop', 'POST', { session_id: id })
        .then(refreshSoon)
        .catch(e => console.error('dashboard stop-run failed', e))
        .finally(() => busy(false));
      return;
    }
    if (act === 'approve-user' || act === 'reject-user') {
      if (act === 'reject-user'
          && !window.confirm('Reject and delete this pending account?')) return;
      busy(true);
      const p = act === 'approve-user'
        ? _adminUsersPost(encodeURIComponent(id) + '/approve', 'POST')
        : _adminUsersPost(encodeURIComponent(id), 'DELETE');
      p.then(refreshSoon)
        .catch(e => console.error('dashboard user action failed', e))
        .finally(() => busy(false));
    }
  } catch (e) {
    console.error('dashboard card action failed', e);
  }
}

// Delegated so it survives every renderAll() rebuild and works whether or not the
// grid is in edit mode (the star is not a drag handle, so onPointerDown ignores it).
function onGridClick(e) {
  const rmBtn = e.target.closest('.dash-card-remove');
  if (rmBtn) {
    const el = rmBtn.closest('.dash-card');
    const card = el && _cards.find(c => c.id === el.dataset.id);
    if (card) removeCard(card.id);
    return;
  }
  // Operational rows/buttons (data-act). Button clicks must not ALSO fire the
  // row's own action, so the closest data-act element wins.
  const actEl = e.target.closest('[data-act]');
  if (actEl && elGrid.contains(actEl)) {
    e.stopPropagation();
    handleCardAction(actEl);
    return;
  }
  const aiBtn = e.target.closest('.dash-card-ai');
  if (!aiBtn) return;
  const el = aiBtn.closest('.dash-card');
  const card = el && _cards.find(c => c.id === el.dataset.id);
  if (card) openCardAssistant(card);
}

// Serialize the current grid to the slim shape the layout endpoints store (same
// fields scheduleSave persists). Shared by the per-admin save and save-as-default.
function slimCards() {
  return _cards.map(c => {
    const o = { id: c.id, type: c.type, x: c.x, y: c.y, w: c.w, h: c.h };
    if (c.type === 'custom') { o.title = c.title; o.icon = c.icon; o.viz = c.viz; o.fields = c.fields; }
    if (c.type === 'metric_chart') { o.series = c.series; o.range = c.range; }
    return o;
  });
}

// Save the CURRENT arrangement as the default everyone gets (fresh admins + anyone
// who hits "reset to default"). Writes the server-side default override. A confirm()
// guards it since it changes the default for all admins, not just this one.
const _SAVE_ICON = '<i data-lucide="save" class="dash-ico-15"></i><span>Save as default</span>';
async function saveAsDefault() {
  if (!window.confirm('Make the current layout the default dashboard? New admins — and anyone who resets — will get this arrangement.')) return;
  const btn = $('dash-savedefault-btn');
  try {
    await _sendJSON('/layout/default', 'POST', { cards: slimCards() });
    // Brief inline confirmation: swap the whole icon to a check, then revert.
    // (Rebuild via innerHTML — Lucide has already replaced the original <i> with
    // an <svg>, so mutating an <i> in place wouldn't take.)
    if (btn) {
      btn.innerHTML = '<i data-lucide="check" class="dash-ico-15"></i><span>Saved</span>';
      btn.classList.add('dash-saved-ok');
      _refreshLucideIcons();
      setTimeout(() => { btn.innerHTML = _SAVE_ICON; btn.classList.remove('dash-saved-ok'); _refreshLucideIcons(); }, 1400);
    }
  } catch (e) {
    console.error('dashboard save-as-default failed', e);
    if (btn) { const t = btn.title; btn.title = 'Could not save default: ' + e.message; setTimeout(() => { btn.title = t; }, 2500); }
  }
}

// Reset to the default layout: drop the saved arrangement server-side and render
// the seed cards it returns. A confirm() guards against wiping a custom layout by
// accident. We do NOT scheduleSave afterwards — leaving the save cleared keeps the
// grid on "default" until the admin next moves something.
async function resetLayout() {
  if (!window.confirm('Reset the dashboard to its default layout? This discards your current arrangement.')) return;
  try {
    const j = await _sendJSON('/layout/reset', 'POST', {});
    _cards = Array.isArray(j.cards) ? j.cards.map(c => ({ ...c })) : [];
    writeInstanceCache('dashboard:layout', slimCards(), DASH_CACHE_TTL.layout);
    renderAll();
    refreshCharts();
  } catch (e) {
    console.error('dashboard reset failed', e);
  }
}

// ── chart cards (timeseries) ─────────────────────────────────────────────────
async function refreshChart(card) {
  const def = cardDef(card.type);
  if (!def || !def.chart) return;
  try {
    const j = await _getJSON('/metrics/timeseries?kind=' + def.chart + '&window=1800&buckets=30');
    _tsCache[card.id] = { at: Date.now(), points: j.points || [] };
    const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
    if (el) renderCardBody(el, card);
  } catch (e) { /* best-effort */ }
}
function refreshCharts() {
  for (const c of _cards) {
    const def = cardDef(c.type) || {};
    if (def.chart) refreshChart(c);
    else if (def.hero) refreshMetricChart(c);
  }
}

// ── Metrics-over-time chart (the full-width hero card) ────────────────────────
// A single plot with a shared time X axis: per-model COST as stacked bars (stack
// height = total), and any of tokens-in/out/CPU/RAM as overlaid LINES — each line
// normalised to its own min/max, real numbers shown in the legend. Data comes from
// GET /admin/dashboard/chart (durable tokens/cost from usage_events; live CPU/RAM
// from the in-memory ring). Controls (metrics menu, presets + date range) are built
// once; only the SVG + legend redraw.
function chartSeriesIds(card) {
  return (Array.isArray(card.series) && card.series.length) ? card.series : CHART_DEFAULT_SERIES.slice();
}
function chartQuery(card) {
  const p = new URLSearchParams();
  p.set('series', chartSeriesIds(card).join(','));
  p.set('buckets', '60');
  const r = card.range || { window: 3600 };
  if (r.from && r.to) { p.set('from', r.from); p.set('to', r.to); }
  else { p.set('window', String(r.window || 3600)); }
  return p.toString();
}
function visibleSeries(card) {
  const ids = chartSeriesIds(card);
  const hidden = chartUi(card).hidden;
  return CHART_SERIES.filter(s => ids.includes(s.id) && !hidden.has(s.id));
}

function _lastNonZero(vals) {
  for (let i = vals.length - 1; i >= 0; i--) if (vals[i]) return vals[i];
  return vals.length ? vals[vals.length - 1] : 0;
}
function fmtUnitVal(unit, v) {
  if (v == null) return '—';
  if (unit === '$') return fmtCost(v);
  if (unit === '%') return Number(v).toFixed(0) + '%';
  if (unit === 'ms') return fmtMs(v);
  if (unit === 'MB') return fmtNum(v) + ' MB';
  return fmtNum(v);
}
// A series' one-number legend summary: totals for volume series (tokens, calls),
// the latest reading for level series (%, MB, runs, latency).
function _seriesSummary(s, vals) {
  if (['%', 'MB', 'runs', 'ms'].includes(s.unit)) return fmtUnitVal(s.unit, _lastNonZero(vals));
  return fmtUnitVal(s.unit, vals.reduce((a, b) => a + b, 0));
}

// "Nice" axis ceiling: 1 / 2 / 2.5 / 5 × 10^k at or above the data max.
function niceCeil(v) {
  if (!(v > 0)) return 1;
  const p = Math.pow(10, Math.floor(Math.log10(v)));
  const m = v / p;
  const f = m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10;
  return f * p;
}

// Adaptive time ticks: pick a natural step (minute/hour/day multiples) that
// yields ~4-7 ticks, aligned to round times; label format follows the step.
const _TICK_STEPS = [60, 300, 900, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 86400, 2 * 86400, 7 * 86400];
function timeTicks(start, end) {
  const span = Math.max(1, end - start);
  let step = _TICK_STEPS[_TICK_STEPS.length - 1];
  for (const s of _TICK_STEPS) { if (span / s <= 7) { step = s; break; } }
  const ticks = [];
  for (let t = Math.ceil(start / step) * step; t <= end; t += step) ticks.push(t);
  return { ticks, step, span };
}
function fmtTick(epoch, span, step) {
  const d = new Date(epoch * 1000);
  const p = (x) => String(x).padStart(2, '0');
  if (step >= 86400 || span > 2 * 86400) return (d.getMonth() + 1) + '/' + d.getDate();
  return p(d.getHours()) + ':' + p(d.getMinutes());
}
function fmtTipTime(epoch, span) {
  const d = new Date(epoch * 1000);
  const p = (x) => String(x).padStart(2, '0');
  const hm = p(d.getHours()) + ':' + p(d.getMinutes());
  if (span > 20 * 3600) return (d.getMonth() + 1) + '/' + d.getDate() + ' ' + hm;
  return hm;
}

function _legChip(tone, label, val, seriesId, off) {
  const sw = tone ? `background:var(${tone})` : 'background:var(--fg-4)';
  const attrs = seriesId
    ? ` data-series="${_escAttr(seriesId)}" role="button" tabindex="0" title="Show / hide this series"`
    : '';
  return `<span class="dash-leg${seriesId ? ' dash-leg-btn' : ''}${off ? ' off' : ''}"${attrs}>`
    + `<span class="dash-leg-sw" data-leg-bg="${tone || '--fg-4'}"></span>${_esc(label)} <b>${_esc(String(val))}</b></span>`;
}

// Legend: one chip per SELECTED series (hidden ones render dimmed — click any
// chip to show/hide), plus per-model cost chips when the cost lane is on.
function chartLegend(data, card) {
  const ids = chartSeriesIds(card);
  const hidden = chartUi(card).hidden;
  const chips = [];
  for (const s of CHART_SERIES) {
    if (!ids.includes(s.id)) continue;
    const off = hidden.has(s.id);
    if (s.kind === 'bars') {
      const models = data.cost_models || [];
      let total = 0;
      models.forEach(m => { total += (m.values || []).reduce((a, b) => a + b, 0); });
      chips.push(_legChip('--chart-1', 'Cost', fmtCost(total), 'cost', off));
      if (!off) {
        models.forEach((m, i) => {
          const t = (m.values || []).reduce((a, b) => a + b, 0);
          chips.push(_legChip(m.model === 'Other' ? null : CHART_TONES[i % CHART_TONES.length],
            m.model, fmtCost(t)));
        });
      }
      continue;
    }
    const ln = (data.lines || []).find(l => l.id === s.id);
    const vals = (ln && ln.values) || [];
    chips.push(_legChip(s.tone, s.label, _seriesSummary(s, vals), s.id, off));
  }
  return chips.join('') || `<span class="dash-muted">No metrics selected — use “Metrics”.</span>`;
}

// Group the visible series into unit LANES (small multiples over one X axis).
function chartLanes(data, card) {
  const vis = visibleSeries(card);
  const lanes = [];
  for (const unit of CHART_LANE_ORDER) {
    const ss = vis.filter(s => s.unit === unit);
    if (!ss.length) continue;
    if (unit === '$') {
      lanes.push({ unit, kind: 'bars', models: data.cost_models || [] });
    } else {
      const lines = ss
        .map(s => ({ s, ln: (data.lines || []).find(l => l.id === s.id) }))
        .filter(x => x.ln);
      if (lines.length) lanes.push({ unit, kind: 'lines', lines });
    }
  }
  return lanes;
}

// Rect with only the TOP corners rounded (the bar's data-end); baseline stays square.
function _roundTopRect(x, y, w, h, r) {
  r = Math.max(0, Math.min(r, w / 2, h));
  return `M${(x).toFixed(1)},${(y + h).toFixed(1)} L${x.toFixed(1)},${(y + r).toFixed(1)} `
    + `Q${x.toFixed(1)},${y.toFixed(1)} ${(x + r).toFixed(1)},${y.toFixed(1)} `
    + `L${(x + w - r).toFixed(1)},${y.toFixed(1)} Q${(x + w).toFixed(1)},${y.toFixed(1)} ${(x + w).toFixed(1)},${(y + r).toFixed(1)} `
    + `L${(x + w).toFixed(1)},${(y + h).toFixed(1)} Z`;
}

// The full SVG: stacked unit lanes sharing one time axis. Each lane gets a real,
// zero-based Y scale with nice ticks + hairline gridlines (never two scales on
// one lane). Geometry is stashed on chartUi(card).geom for the hover layer.
function chartSvg(data, card, W, H) {
  const buckets = data.buckets || [];
  const n = buckets.length;
  const lanes = chartLanes(data, card);
  if (!n || !lanes.length) {
    chartUi(card).geom = null;   // no plot → no crosshair mapping
    return `<div class="dash-muted">${n ? 'No metrics visible — pick some under “Metrics”, or click a dimmed legend chip.' : 'No data in this range yet.'}</div>`;
  }

  const L = 8, R = 58, T = 6, B = 20, GAP_L = 16;
  const plotW = Math.max(10, W - L - R);
  const plotH = Math.max(10, H - T - B);
  const laneH = Math.max(24, (plotH - GAP_L * (lanes.length - 1)) / lanes.length);
  const slot = plotW / n;
  const parts = [];
  const geomLanes = [];
  const span = (data.end || 0) - (data.start || 0);

  // Shared vertical time gridlines + labels (adaptive natural steps).
  const tt = timeTicks(data.start || 0, data.end || 1);
  for (const t of tt.ticks) {
    const x = L + ((t - data.start) / Math.max(1, span)) * plotW;
    parts.push(`<line x1="${x.toFixed(1)}" y1="${T}" x2="${x.toFixed(1)}" y2="${(T + plotH).toFixed(1)}" class="dash-chart-grid-v"/>`);
    parts.push(`<text x="${x.toFixed(1)}" y="${(H - 6).toFixed(1)}" class="dash-chart-ax" text-anchor="middle">${_esc(fmtTick(t, tt.span, tt.step))}</text>`);
  }

  lanes.forEach((lane, li) => {
    const y0 = T + li * (laneH + GAP_L);
    const yB = y0 + laneH;

    // Lane max → nice zero-based scale (CPU is pinned to 0-100).
    let max = 0;
    if (lane.kind === 'bars') {
      const totals = new Array(n).fill(0);
      lane.models.forEach(m => (m.values || []).forEach((v, i) => { totals[i] += v; }));
      max = Math.max(...totals, 0);
    } else {
      lane.lines.forEach(x => (x.ln.values || []).forEach(v => { if (v > max) max = v; }));
    }
    const niceMax = lane.unit === '%' ? 100 : niceCeil(max);

    // Horizontal gridlines + right-gutter tick labels (0 · half · max).
    [[yB, 0], [y0 + laneH / 2, niceMax / 2], [y0, niceMax]].forEach(([yy, val], ti) => {
      parts.push(`<line x1="${L}" y1="${yy.toFixed(1)}" x2="${(L + plotW).toFixed(1)}" y2="${yy.toFixed(1)}" class="${ti === 0 ? 'dash-chart-base' : 'dash-chart-grid'}"/>`);
      parts.push(`<text x="${(W - R + 6).toFixed(1)}" y="${(yy + 3).toFixed(1)}" class="dash-chart-ax">${_esc(fmtUnitVal(lane.unit, val))}</text>`);
    });
    // Lane caption (unit group), tucked inside the lane's top-left corner —
    // drawn inside (not above) so the first lane's caption can't clip at T.
    parts.push(`<text x="${(L + 2).toFixed(1)}" y="${(y0 + 9).toFixed(1)}" class="dash-chart-lane">${_esc(CHART_LANE_LABEL[lane.unit] || lane.unit)}</text>`);

    if (lane.kind === 'bars') {
      // Stacked per-model cost bars: ≤24px wide, 2px surface gaps between
      // segments, rounded top on the stack's data-end, square baseline.
      const barW = Math.min(slot * 0.62, 24);
      for (let i = 0; i < n; i++) {
        const bx = L + i * slot + (slot - barW) / 2;
        let yCur = yB;
        const segs = [];
        lane.models.forEach((m, mi) => {
          const v = (m.values || [])[i] || 0;
          if (v <= 0) return;
          const hh = (v / niceMax) * laneH;
          yCur -= hh;
          segs.push({ y: yCur, h: hh, mi, other: m.model === 'Other' });
        });
        segs.forEach((sg, si) => {
          const isTop = si === segs.length - 1;
          const gap = si > 0 ? 2 : 0;               // 2px surface gap between segments
          const h = Math.max(1, sg.h - gap);
          const fill = sg.other ? 'var(--fg-4)' : `var(${CHART_TONES[sg.mi % CHART_TONES.length]})`;
          if (isTop && h > 3) {
            parts.push(`<path d="${_roundTopRect(bx, sg.y, barW, h, 3)}" fill="${fill}" opacity="0.9"/>`);
          } else {
            parts.push(`<rect x="${bx.toFixed(1)}" y="${sg.y.toFixed(1)}" width="${barW.toFixed(1)}" height="${h.toFixed(1)}" fill="${fill}" opacity="0.9"/>`);
          }
        });
      }
    } else {
      // 2px lines at bucket centres + an end marker with a surface ring.
      for (const { s, ln } of lane.lines) {
        const vals = ln.values || [];
        const pts = vals.map((v, i) => {
          const x = L + (i + 0.5) * slot;
          const y = yB - (Math.max(0, v) / niceMax) * laneH;
          return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
        parts.push(`<polyline points="${pts}" fill="none" stroke="var(${s.tone})" stroke-width="2" vector-effect="non-scaling-stroke" stroke-linejoin="round" stroke-linecap="round"/>`);
        if (vals.length) {
          const lastX = L + (vals.length - 0.5) * slot;
          const lastY = yB - (Math.max(0, vals[vals.length - 1]) / niceMax) * laneH;
          parts.push(`<circle cx="${lastX.toFixed(1)}" cy="${lastY.toFixed(1)}" r="3.5" fill="var(${s.tone})" stroke="var(--bg-elev)" stroke-width="2"/>`);
        }
      }
    }
    geomLanes.push({ unit: lane.unit, kind: lane.kind, y0, laneH, niceMax });
  });

  // Hover geometry for the crosshair/tooltip + drag-zoom layer.
  chartUi(card).geom = {
    L, R, T, B, W, H, plotW, plotH, slot, n,
    start: data.start || 0, end: data.end || 1, lanes: geomLanes,
  };
  return `<svg width="${W}" height="${H}" viewBox="0 0 ${W} ${H}" class="dash-chart-svg">${parts.join('')}</svg>`;
}

function drawMetricChart(card) {
  const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
  if (!el) return;
  const plot = el.querySelector('.dash-chart-plot');
  const legend = el.querySelector('.dash-chart-legend');
  const data = _chartCache[card.id];
  if (!plot || !data) return;
  const W = plot.clientWidth, H = plot.clientHeight;
  if (W < 40 || H < 30) return;   // panel not laid out yet — a later tick redraws
  // Rebuild the SVG but keep the persistent hover layer (cross + tip + zoombox).
  const svgHtml = chartSvg(data, card, W, H);
  const old = plot.querySelector('.dash-chart-svg, .dash-muted');
  if (old) old.remove();
  plot.insertAdjacentHTML('afterbegin', svgHtml);
  plot.classList.remove('refetching');
  if (legend) legend.innerHTML = chartLegend(data, card);
  // CSP: apply leg-bg from data attributes
  if (legend) legend.querySelectorAll('[data-leg-bg]').forEach(el => {
    el.style.setProperty('--leg-bg', 'var(' + el.dataset.legBg + ')');
  });
  const zo = el.querySelector('.dash-chart-zoomout');
  if (zo) zo.hidden = !chartUi(card).zoomStack.length;
}

async function refreshMetricChart(card) {
  // Hold the previous render at reduced opacity while refetching — no flash.
  const el = elGrid && elGrid.querySelector(`.dash-card[data-id="${CSS.escape(card.id)}"]`);
  const plot = el && el.querySelector('.dash-chart-plot');
  if (plot && plot.querySelector('.dash-chart-svg')) plot.classList.add('refetching');
  try {
    const j = await _getJSON('/chart?' + chartQuery(card));
    _chartCache[card.id] = j;
    drawMetricChart(card);
  } catch (e) { if (plot) plot.classList.remove('refetching'); }
}
function redrawMetricChartsFromCache() {
  for (const c of _cards) if ((cardDef(c.type) || {}).hero && _chartCache[c.id]) drawMetricChart(c);
}

function updateChartControls(body, card) {
  const r = card.range || { window: 3600 };
  const isRange = !!(r.from && r.to);
  body.querySelectorAll('.dash-chart-win').forEach(b =>
    b.classList.toggle('active', !isRange && Number(b.dataset.window) === Number(r.window)));
  const fromI = body.querySelector('.dash-chart-from'), toI = body.querySelector('.dash-chart-to');
  const dateOnly = (v) => /^\d{4}-\d{2}-\d{2}$/.test(String(v || ''));
  if (isRange && dateOnly(r.from) && dateOnly(r.to)) {
    if (fromI) fromI.value = r.from;
    if (toI) toI.value = r.to;
  }
  body.querySelectorAll('input[data-series]').forEach(cb =>
    cb.checked = chartSeriesIds(card).includes(cb.dataset.series));
  const zo = body.querySelector('.dash-chart-zoomout');
  if (zo) zo.hidden = !chartUi(card).zoomStack.length;
}

// ── hover layer: crosshair + all-series tooltip + drag-to-zoom ───────────────
function _tipRow(toneCss, name, val) {
  return `<div class="dash-tip-row"><span class="dash-tip-key" data-tip-bg="${toneCss}"></span>`
    + `<span class="dash-tip-name">${_esc(name)}</span><b>${_esc(val)}</b></div>`;
}

function _chartTipHtml(card, bi) {
  const data = _chartCache[card.id];
  if (!data) return '';
  const span = (data.end || 0) - (data.start || 0);
  const rows = [];
  for (const s of visibleSeries(card)) {
    if (s.kind === 'bars') {
      let total = 0;
      (data.cost_models || []).forEach((m, mi) => {
        const v = (m.values || [])[bi] || 0;
        total += v;
        if (v > 0) {
          const tone = m.model === 'Other' ? 'var(--fg-4)' : `var(${CHART_TONES[mi % CHART_TONES.length]})`;
          rows.push(_tipRow(tone, m.model, fmtCost(v)));
        }
      });
      rows.push(_tipRow('var(--fg-4)', 'Total cost', fmtCost(total)));
    } else {
      const ln = (data.lines || []).find(l => l.id === s.id);
      const v = ((ln && ln.values) || [])[bi];
      rows.push(_tipRow(`var(${s.tone})`, s.label, fmtUnitVal(s.unit, v || 0)));
    }
  }
  const ts = (data.buckets || [])[bi] || 0;
  return `<div class="dash-tip-head">${_esc(fmtTipTime(ts, span))}</div>` + rows.join('');
}

function wireChartHover(plot, card) {
  const cross = document.createElement('div');
  cross.className = 'dash-chart-cross';
  cross.hidden = true;
  const tip = document.createElement('div');
  tip.className = 'dash-chart-tip';
  tip.hidden = true;
  const zbox = document.createElement('div');
  zbox.className = 'dash-chart-zoombox';
  zbox.hidden = true;
  plot.append(cross, tip, zbox);

  let dragX0 = null;

  const hide = () => { cross.hidden = true; tip.hidden = true; };
  const geom = () => (chartUi(card).geom || null);

  plot.addEventListener('pointerleave', () => { hide(); zbox.hidden = true; dragX0 = null; });

  plot.addEventListener('pointerdown', (e) => {
    const g = geom();
    if (!g || e.button !== 0) return;
    const rect = plot.getBoundingClientRect();
    const px = e.clientX - rect.left;
    if (px < g.L || px > g.L + g.plotW) return;
    dragX0 = px;
    try { plot.setPointerCapture(e.pointerId); } catch (err) { /* best-effort */ }
  });

  plot.addEventListener('pointermove', (e) => {
    const g = geom();
    if (!g) return;
    const rect = plot.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const py = e.clientY - rect.top;

    if (dragX0 != null && Math.abs(px - dragX0) > 6) {
      // Drag-to-zoom selection band.
      const x0 = Math.max(g.L, Math.min(dragX0, px));
      const x1 = Math.min(g.L + g.plotW, Math.max(dragX0, px));
      zbox.style.left = x0 + 'px';
      zbox.style.width = (x1 - x0) + 'px';
      zbox.style.top = g.T + 'px';
      zbox.style.height = g.plotH + 'px';
      zbox.hidden = false;
      tip.hidden = true;
      cross.hidden = true;
      return;
    }

    if (px < g.L || px > g.L + g.plotW) { hide(); return; }
    const bi = Math.max(0, Math.min(g.n - 1, Math.floor((px - g.L) / g.slot)));
    const cx = g.L + (bi + 0.5) * g.slot;
    cross.style.left = cx + 'px';
    cross.style.top = g.T + 'px';
    cross.style.height = g.plotH + 'px';
    cross.hidden = false;

    tip.innerHTML = _chartTipHtml(card, bi);
    // CSP: apply tip-bg from data attributes
    tip.querySelectorAll('[data-tip-bg]').forEach(el => {
      el.style.setProperty('--tip-bg', el.dataset.tipBg);
    });
    tip.hidden = false;
    // Flip the tooltip to whichever side of the crosshair has room.
    const tw = tip.offsetWidth || 160;
    const left = (cx + 14 + tw > plot.clientWidth) ? (cx - 14 - tw) : (cx + 14);
    tip.style.left = Math.max(2, left) + 'px';
    tip.style.top = Math.max(2, Math.min(py + 12, plot.clientHeight - tip.offsetHeight - 4)) + 'px';
  });

  plot.addEventListener('pointerup', (e) => {
    const g = geom();
    const wasDrag = dragX0 != null;
    const rect = plot.getBoundingClientRect();
    const px = e.clientX - rect.left;
    const x0 = dragX0;
    dragX0 = null;
    zbox.hidden = true;
    if (!g || !wasDrag || Math.abs(px - x0) <= 6) return;
    // Zoom into the dragged time span (transient — not saved to the layout;
    // the Zoom-out button unwinds, presets reset).
    const t = (x) => g.start + (Math.max(g.L, Math.min(x, g.L + g.plotW)) - g.L) / g.plotW * (g.end - g.start);
    const t0 = Math.floor(Math.min(t(x0), t(px)));
    const t1 = Math.ceil(Math.max(t(x0), t(px)));
    if (t1 - t0 < 30) return;   // too narrow to be meaningful
    chartUi(card).zoomStack.push(card.range || { window: 3600 });
    card.range = {
      from: new Date(t0 * 1000).toISOString(),
      to: new Date(t1 * 1000).toISOString(),
    };
    const body = plot.closest('.dash-card-body');
    if (body) updateChartControls(body, card);
    refreshMetricChart(card);
  });
}

function buildMetricChart(body, card) {
  body.innerHTML =
    `<div class="dash-chart">`
    + `<div class="dash-chart-ctl">`
    + `<div class="dash-chart-times">`
    + CHART_WINDOWS.map(w => `<button type="button" class="dash-chart-win" data-window="${w.s}">${w.label}</button>`).join('')
    + `<span class="dash-chart-range">`
    + `<input type="date" class="dash-chart-from dash-select" title="Range start">`
    + `<span class="dash-chart-dash">–</span>`
    + `<input type="date" class="dash-chart-to dash-select" title="Range end">`
    + `<button type="button" class="dash-chart-apply dash-btn">Apply</button>`
    + `</span>`
    + `<button type="button" class="dash-chart-zoomout dash-btn" hidden title="Undo the last drag-zoom"><i data-lucide="undo-2" class="dash-ico-13"></i><span>Zoom out</span></button>`
    + `</div>`
    + `<div class="dash-chart-metrics">`
    + `<button type="button" class="dash-chart-metrics-btn dash-btn"><i data-lucide="sliders-horizontal" class="dash-ico-14"></i><span>Metrics</span></button>`
    + `<div class="dash-chart-menu" hidden>`
    + CHART_SERIES.map(s => `<label class="dash-chart-opt"><input type="checkbox" data-series="${s.id}"><span>${_esc(s.label)}</span></label>`).join('')
    + `</div></div>`
    + `</div>`
    + `<div class="dash-chart-legend"></div>`
    + `<div class="dash-chart-plot"></div>`
    + `</div>`;

  const q = (sel) => body.querySelector(sel);
  const menu = q('.dash-chart-menu');
  const plot = q('.dash-chart-plot');

  body.querySelectorAll('.dash-chart-win').forEach(b => b.addEventListener('click', () => {
    card.range = { window: Number(b.dataset.window) || 3600 };
    chartUi(card).zoomStack = [];
    updateChartControls(body, card); scheduleSave(); refreshMetricChart(card);
  }));
  q('.dash-chart-apply').addEventListener('click', () => {
    const from = q('.dash-chart-from').value, to = q('.dash-chart-to').value;
    if (!from || !to) return;
    card.range = { from, to };
    chartUi(card).zoomStack = [];
    updateChartControls(body, card); scheduleSave(); refreshMetricChart(card);
  });
  q('.dash-chart-zoomout').addEventListener('click', () => {
    const stack = chartUi(card).zoomStack;
    if (stack.length) card.range = stack.pop();
    updateChartControls(body, card); refreshMetricChart(card);
  });
  q('.dash-chart-metrics-btn').addEventListener('click', () => { menu.hidden = !menu.hidden; });
  menu.querySelectorAll('input[data-series]').forEach(cb => cb.addEventListener('change', () => {
    const ids = new Set(chartSeriesIds(card));
    if (cb.checked) ids.add(cb.dataset.series); else ids.delete(cb.dataset.series);
    card.series = CHART_SERIES.map(s => s.id).filter(id => ids.has(id));
    chartUi(card).hidden.delete(cb.dataset.series);   // re-adding always shows it
    scheduleSave(); refreshMetricChart(card);
  }));
  // Legend chips toggle series visibility in place (delegated — the legend is
  // re-rendered on every draw).
  q('.dash-chart-legend').addEventListener('click', (e) => {
    const chip = e.target.closest('.dash-leg-btn');
    if (!chip) return;
    const sid = chip.dataset.series;
    const hidden = chartUi(card).hidden;
    if (hidden.has(sid)) hidden.delete(sid); else hidden.add(sid);
    drawMetricChart(card);
  });
  wireChartHover(plot, card);

  updateChartControls(body, card);
  _refreshLucideIcons();
}

// ── polling ──────────────────────────────────────────────────────────────────
// The FAST first fill: /metrics?live=1 is DB-free, so the live / db-health /
// storage cards populate almost immediately (even on a slow remote DB) while the
// heavier DB-backed cards keep their spinner until the full poll() lands. Merged
// (not replaced) so the sections it carries appear the instant they're back.
async function pollLive() {
  try {
    const live = await _getJSON('/metrics?live=1&window=' + _window);
    _snapshot = Object.assign(_snapshot || {}, live);
    writeInstanceCache('dashboard:snapshot', _snapshot, DASH_CACHE_TTL.snapshot);
    const dot = $('dash-live-dot');
    if (dot) dot.classList.add('on');
    onDashboardSnapshot(_snapshot);
    refreshBodies();
  } catch (e) { /* the full poll below fills everything anyway */ }
}
async function poll() {
  try {
    const full = await _getJSON('/metrics?window=' + _window);
    // Merge so a section the full snapshot happens to omit doesn't blank a card
    // the fast poll already filled; the full poll carries every section normally.
    _snapshot = Object.assign(_snapshot || {}, full);
    writeInstanceCache('dashboard:snapshot', _snapshot, DASH_CACHE_TTL.snapshot);
    const dot = $('dash-live-dot');
    if (dot) dot.classList.add('on');
    onDashboardSnapshot(_snapshot);   // keep the instance header's DB/health pills live
    refreshBodies();
  } catch (e) {
    const dot = $('dash-live-dot');
    if (dot) dot.classList.remove('on');
  }
}
function startPolling() {
  stopPolling();
  pollLive();   // instant partial fill (live / db-health / storage cards)
  poll();       // full snapshot (DB-backed cards fill when it returns)
  refreshCharts();
  _pollTimer = setInterval(() => {
    if (document.hidden) return;
    poll();
  }, POLL_MS);
  _chartTimer = setInterval(() => { if (!document.hidden) refreshCharts(); }, 30000);
}
let _chartTimer = null;
function stopPolling() {
  if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  if (_chartTimer) { clearInterval(_chartTimer); _chartTimer = null; }
}

// ── the "Add a card" tile (inline — no modal) ────────────────────────────────
// Builds the card body: a chat pill (shared .chat-pill design) whose + button
// reveals an inline grid of built-in card types, and whose send button turns a
// plain-English prompt into an AI-designed card. Wired once per card element.
function buildAdder(body) {
  body.innerHTML =
    `<div class="dash-adder">`
    + `<div class="dash-adder-pill chat-pill chat-pill-1line no-voice">`
    + `<button type="button" class="chat-pill-attach dash-adder-plus" title="Browse card types"><i data-lucide="plus" class="dash-ico-18"></i></button>`
    + `<textarea class="chat-pill-input dash-adder-input" rows="1" placeholder="Describe a card and AI will build it — e.g. “tokens used per agent today”" autocomplete="off"></textarea>`
    + `<button type="button" class="chat-pill-send dash-adder-send" title="Generate with AI"><i data-lucide="sparkles" class="dash-ico-16"></i></button>`
    + `</div>`
    + `<div class="dash-adder-status" hidden></div>`
    + `<div class="dash-adder-picker" hidden><div class="dash-adder-grid"></div></div>`
    + `</div>`;
  const pill = body.querySelector('.dash-adder-pill');
  const plus = body.querySelector('.dash-adder-plus');
  const input = body.querySelector('.dash-adder-input');
  const send = body.querySelector('.dash-adder-send');
  const picker = body.querySelector('.dash-adder-picker');
  const grid = body.querySelector('.dash-adder-grid');

  function buildPickerGrid() {
    grid.innerHTML = pickerTypes().map(t => {
      const d = cardDef(t) || CARD_TYPES.custom;
      return `<button class="dash-picker-item" data-type="${t}">`
        + `<i data-lucide="${_escAttr(d.icon)}" class="dash-ico-18"></i>`
        + `<span>${_esc(d.title || d.label)}</span></button>`;
    }).join('');
    grid.dataset.built = '1';
    grid.querySelectorAll('.dash-picker-item').forEach(b =>
      b.addEventListener('click', () => addCard({ type: b.dataset.type })));
    _refreshLucideIcons();
  }

  plus.addEventListener('click', () => {
    if (!grid.dataset.built) buildPickerGrid();   // built lazily so the catalog lands first
    const show = picker.hidden;
    picker.hidden = !show;
    plus.classList.toggle('active', show);
    if (show) _refreshLucideIcons();
  });
  input.addEventListener('input', () => pill.classList.toggle('has-text', !!input.value.trim()));
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); adderGenerate(body); }
  });
  send.addEventListener('click', () => adderGenerate(body));
  _refreshLucideIcons();
}

async function adderGenerate(body) {
  const input = body.querySelector('.dash-adder-input');
  const status = body.querySelector('.dash-adder-status');
  const send = body.querySelector('.dash-adder-send');
  if (!input) return;
  const prompt = input.value.trim();
  if (!prompt) { input.focus(); return; }
  if (status) { status.hidden = false; status.className = 'dash-adder-status'; status.textContent = 'Designing your card…'; }
  if (send) send.disabled = true;
  try {
    const spec = await _sendJSON('/ai-card', 'POST', { prompt });
    input.value = '';
    addCard(spec);   // addCard → renderAll rebuilds a fresh (empty) adder; the new
                     // card appearing is the confirmation.
  } catch (e) {
    if (status) { status.hidden = false; status.className = 'dash-adder-status err'; status.textContent = 'Could not generate: ' + e.message; }
    if (send) send.disabled = false;
  }
}

// Footer / empty-state entry point: Add card now opens directly from the
// footer dock rather than relying on an inline mock card in the dashboard grid.
function closeCardPicker() {
  $('dash-add-card-menu')?.remove();
}
function openCardPicker() {
  closeMoreMenu();
  closeDashboardList();
  closeCardPicker();
  const dock = document.getElementById('inst-dashboard-footer-dock');
  if (!dock) return;
  const menu = document.createElement('div');
  menu.id = 'dash-add-card-menu';
  menu.className = 'dash-add-card-menu';
  menu.setAttribute('role', 'dialog');
  menu.setAttribute('aria-label', 'Add card');
  menu.innerHTML = '<div class="dash-add-card-label">Add card</div><div class="dash-add-card-grid">'
    + pickerTypes().map(type => {
      const def = cardDef(type) || CARD_TYPES.custom;
      return `<button type="button" class="dash-picker-item" data-type="${_escAttr(type)}"><i data-lucide="${_escAttr(def.icon || 'layout-dashboard')}" class="dash-ico-18"></i><span>${_esc(def.title || def.label || type)}</span></button>`;
    }).join('') + '</div>';
  menu.addEventListener('click', e => {
    const item = e.target.closest('.dash-picker-item');
    if (!item) return;
    addCard({ type: item.dataset.type });
    closeCardPicker();
  });
  dock.appendChild(menu);
  _refreshLucideIcons(menu);
}

// ── edit mode toggle ─────────────────────────────────────────────────────────
function toggleEdit() {
  _editMode = !_editMode;
  const btn = $('dash-edit-btn');
  if (btn) {
    btn.classList.toggle('active', _editMode);
    btn.setAttribute('aria-pressed', String(_editMode));
    btn.title = _editMode ? 'Exit layout edit' : 'Edit layout';
    btn.setAttribute('aria-label', btn.title);
  }
  if (elGrid) elGrid.classList.toggle('edit', _editMode);
}

// ── "More" actions dropdown ─────────────────────────────────────────────────
// Add card / Save as default / Reset live here. Edit layout is a dedicated
// footer icon so its active state remains visible while cards are rearranged.
// Anchored to the ⋮ button; closes on item click or an outside click.
function toggleMoreMenu() {
  const m = $('dash-more-menu'), b = $('dash-more-btn');
  if (!m) return;
  const open = m.hidden;   // was hidden → we're opening it
  if (open) closeDashboardList();
  m.hidden = !open;
  if (b) { b.setAttribute('aria-expanded', String(open)); b.classList.toggle('active', open); }
}
function closeMoreMenu() {
  const m = $('dash-more-menu'), b = $('dash-more-btn');
  if (m) m.hidden = true;
  if (b) { b.setAttribute('aria-expanded', 'false'); b.classList.remove('active'); }
}

function toggleDashboardList() {
  const m = $('dash-dashboard-list-menu'), b = $('dash-dashboard-list-btn');
  if (!m) return;
  const open = m.hidden;
  if (open) closeMoreMenu();
  m.hidden = !open;
  if (b) { b.setAttribute('aria-expanded', String(open)); b.classList.toggle('active', open); }
}
function closeDashboardList() {
  const m = $('dash-dashboard-list-menu'), b = $('dash-dashboard-list-btn');
  if (m) m.hidden = true;
  if (b) { b.setAttribute('aria-expanded', 'false'); b.classList.remove('active'); }
}

// ── lifecycle ────────────────────────────────────────────────────────────────
let _wired = false;
function wire() {
  if (_wired) return;
  _wired = true;
  elGrid = $('dash-grid'); elWrap = $('dash-grid-wrap'); elEmpty = $('dash-empty');
  const on = (id, ev, fn) => { const el = $(id); if (el) el.addEventListener(ev, fn); };
  on('dash-add-btn', 'click', (e) => { e.stopPropagation(); openCardPicker(); });
  on('dash-empty-add', 'click', (e) => { e.stopPropagation(); openCardPicker(); });
  on('dash-edit-btn', 'click', toggleEdit);
  on('dash-savedefault-btn', 'click', saveAsDefault);
  on('dash-reset-btn', 'click', resetLayout);
  on('dash-refresh-btn', 'click', () => { poll(); refreshCharts(); });
  on('dash-dashboard-list-btn', 'click', (e) => { e.stopPropagation(); toggleDashboardList(); });
  on('dash-dashboard-list-menu', 'click', (e) => {
    const item = e.target.closest('.dash-dashboard-list-item');
    if (!item) return;
    elGrid?.querySelector(`.dash-card[data-id="${CSS.escape(item.dataset.cardId)}"]`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    closeDashboardList();
  });
  // "More" dropdown: toggle on the ⋮ button, close after any item click.
  on('dash-more-btn', 'click', (e) => { e.stopPropagation(); toggleMoreMenu(); });
  on('dash-more-menu', 'click', (e) => { if (e.target.closest('button')) closeMoreMenu(); });
  const sel = $('dash-window');
  if (sel) sel.addEventListener('change', () => { _window = Number(sel.value) || 3600; setWindow(_window); poll(); });
  if (elGrid) elGrid.addEventListener('pointerdown', onPointerDown);
  if (elGrid) elGrid.addEventListener('click', onGridClick);
  // Close any open chart Metrics menu — and the toolbar "More" menu — when
  // clicking outside its button/menu.
  document.addEventListener('click', (e) => {
    document.querySelectorAll('.dash-chart-menu').forEach(m => {
      if (m.hidden) return;
      const wrap = m.closest('.dash-chart-metrics');
      if (wrap && !wrap.contains(e.target)) m.hidden = true;
    });
    const more = $('dash-more');
    if (more && !more.contains(e.target)) closeMoreMenu();
    const dashboardList = $('dash-dashboard-list');
    if (dashboardList && !dashboardList.contains(e.target)) closeDashboardList();
    const cardPicker = $('dash-add-card-menu');
    if (cardPicker && !cardPicker.contains(e.target)) closeCardPicker();
  });
  if (elWrap && 'ResizeObserver' in window) {
    _ro = new ResizeObserver(() => { repositionAll(); redrawMetricChartsFromCache(); });
    _ro.observe(elWrap);
  }
}

// Full-panel spinner shown while the layout is loading — i.e. BEFORE the card
// sizes are even known. Once renderAll() has laid the cards out, this hides and
// the per-card spinners (renderCardBody) take over until each card's data lands.
function showGridLoading(on) {
  const el = $('dash-loading');
  if (el) el.hidden = !on;
}

export async function startView() {
  wire();
  loadCardCatalog();   // plugin descriptors (sizes, sections, chart, picker order)
  if (_started) {
    // Returning to the view: the panel width may have changed while away.
    whenSized(() => { repositionAll(); refreshBodies(); redrawMetricChartsFromCache(); });
    startPolling();
    return;
  }
  // First open: show the overall spinner until the layout is fetched and the grid
  // is laid out (the point where per-card spinners can take over).
  showGridLoading(true);
  const cachedLayout = readInstanceCache('dashboard:layout');
  const cachedSnapshot = readInstanceCache('dashboard:snapshot');
  if (Array.isArray(cachedLayout)) {
    _cards = cachedLayout.map(c => ({ ...c }));
    _started = true;
  }
  if (cachedSnapshot && typeof cachedSnapshot === 'object') _snapshot = cachedSnapshot;
  // Load the saved layout. Only mark the view "started" on SUCCESS, so a
  // transient failure (e.g. the auth wrapper not yet installed right at boot)
  // retries on the next activation instead of sticking on an empty grid.
  try {
    const j = await _getJSON('/layout');
    _cards = Array.isArray(j.cards) ? j.cards : [];
    writeInstanceCache('dashboard:layout', slimCards(), DASH_CACHE_TTL.layout);
    _started = true;
  } catch (e) {
    console.error('dashboard layout load failed (will retry on next open)', e);
  }
  // Render the card shells (each with a per-card spinner) as soon as the panel
  // has a real width — do NOT wait on the first metrics poll, which can take
  // seconds on a slow remote DB. Hide the overall spinner now that the sizes are
  // known, then startPolling() fires the fast + full polls that fill the bodies.
  // The ResizeObserver keeps cards positioned afterwards (rail toggle / window
  // resize / a panel that widens after the tab activates).
  whenSized(() => { renderAll(); showGridLoading(false); startPolling(); });
}

export function stopView() {
  stopPolling();
}

// ── Embedded mounting (called by instances.js dashboard tab) ────────────────
// The dashboard can be mounted inside a host element (the Instances page's
// "This device" detail tab) instead of as a standalone admin page.
let _mountedHost = null;

export function mountDashboard(hostEl) {
  if (!hostEl) return;
  _mountedHost = hostEl;

  // Reset module state for clean re-mount
  _started = false;
  _wired = false;
  _cards = [];
  _snapshot = null;
  _editMode = false;
  stopPolling();
  if (_ro) { try { _ro.disconnect(); } catch {} _ro = null; }

  // Build the toolbar + grid DOM inside the host (NO instance-switcher header)
  installMetricInfoPopovers();
  hostEl.innerHTML = _embeddedHtml();

  // The Dashboard footer is a page-level Instances dock (a sibling of the
  // scrolling grid, like Agents Sessions). Keep the controls outside this host so
  // they remain visible during the Instances carousel-to-detail transition.
  const footerDock = document.getElementById('inst-dashboard-footer-dock');
  if (footerDock) {
    footerDock.innerHTML = _footerHtml();
    _refreshLucideIcons(footerDock);
  }

  // Refresh Lucide icons in the new grid.
  _refreshLucideIcons(hostEl);

  // Re-bind DOM refs to the new elements inside the host
  elGrid = hostEl.querySelector('#dash-grid');
  elWrap = hostEl.querySelector('#dash-grid-wrap');
  elEmpty = hostEl.querySelector('#dash-empty');

  // Paint the last layout and metrics immediately. The live layout/metrics
  // requests below revalidate these tenant-scoped IDB rows in the background.
  const cachedLayout = readInstanceCache('dashboard:layout');
  const cachedSnapshot = readInstanceCache('dashboard:snapshot');
  const hasCachedLayout = Array.isArray(cachedLayout);
  if (hasCachedLayout) {
    _cards = cachedLayout.map(c => ({ ...c }));
    _started = true;
  }
  if (cachedSnapshot && typeof cachedSnapshot === 'object') _snapshot = cachedSnapshot;

  // Wire events
  wire();
  loadCardCatalog();   // plugin descriptors (sizes, sections, chart, picker order)
  // NOTE: the ResizeObserver on elWrap is created inside wire() above — do NOT
  // create a second one here. A duplicate would be orphaned on every mount
  // (mountDashboard → wire → observer A, then this block → observer B, with A
  // never disconnected), and with the dashboard being re-mounted several times
  // during the Instances page's initial load, the orphans pile up and every
  // resize fires repositionAll/redrawMetricChartsFromCache once per observer.

  // Load the saved layout and start polling
  showGridLoading(!hasCachedLayout);
  if (hasCachedLayout) {
    whenSized(() => { renderAll(); showGridLoading(false); startPolling(); });
  }
  _loadEmbeddedLayout().then(() => {
    whenSized(() => {
      renderAll();
      showGridLoading(false);
      if (!hasCachedLayout) startPolling();
    });
  });
}

function _embeddedHtml() {
  return '<div class="dash-grid-wrap" id="dash-grid-wrap">'
    +   '<div class="dash-grid" id="dash-grid"></div>'
    +   '<div class="dash-loading" id="dash-loading" hidden>'
    +     '<span class="dash-spinner dash-spinner-lg"></span>'
    +     '<span class="dash-loading-text">Loading your dashboard…</span>'
    +   '</div>'
    +   '<div class="dash-empty" id="dash-empty" hidden>'
    +     '<i data-lucide="layout-dashboard" class="dash-ico-28"></i>'
    +     '<div class="dash-empty-title">Your dashboard is empty</div>'
    +     '<div class="dash-empty-desc">Add cards to track tokens, cost, database latency, devices and more.</div>'
    +     '<button class="dash-btn dash-btn-primary" id="dash-empty-add"><i data-lucide="plus" class="dash-ico-14"></i><span>Add your first card</span></button>'
    +   '</div>'
    + '</div>';
}

function _footerHtml() {
  return '<div class="dash-dashboard-list" id="dash-dashboard-list">'
    +   '<button id="dash-dashboard-list-btn" class="dash-dashboard-list-btn" type="button" aria-haspopup="true" aria-expanded="false" aria-controls="dash-dashboard-list-menu"><span id="dash-dashboard-count">0 Dashboards</span><i data-lucide="chevron-up" class="dash-ico-14"></i></button>'
    +   '<div id="dash-dashboard-list-menu" class="dash-dashboard-list-menu" hidden>'
    +     '<div class="dash-dashboard-list-label">Dashboards</div>'
    +     '<div id="dash-dashboard-list-items"></div>'
    +   '</div>'
    + '</div>'
    + '<span class="dash-spacer"></span>'
    + '<select id="dash-window" class="dash-select" title="Time window for aggregate cards">'
    +   '<option value="300">Last 5 min</option>'
    +   '<option value="3600" selected>Last hour</option>'
    +   '<option value="86400">Last 24 hours</option>'
    +   '<option value="604800">Last 7 days</option>'
    + '</select>'
    + '<div class="dash-footer-actions">'
    +   '<button id="dash-edit-btn" type="button" class="dash-icon-btn" title="Edit layout" aria-label="Edit layout" aria-pressed="false"><i data-lucide="move" class="dash-ico-14"></i></button>'
    +   '<button id="dash-refresh-btn" type="button" class="dash-icon-btn" title="Refresh now"><i data-lucide="refresh-cw" class="dash-ico-14"></i></button>'
    +   '<div class="dash-more" id="dash-more">'
    +     '<button id="dash-more-btn" type="button" class="dash-icon-btn" title="More actions" aria-haspopup="true" aria-expanded="false"><i data-lucide="more-vertical" class="dash-ico-16"></i></button>'
    +     '<div class="dash-more-menu" id="dash-more-menu" hidden>'
    +       '<button id="dash-add-btn" class="dash-more-item" title="Add a card"><i data-lucide="plus" class="dash-ico-15"></i><span>Add card</span></button>'
    +       '<div class="dash-more-sep"></div>'
    +       '<button id="dash-savedefault-btn" class="dash-more-item" title="Save this layout as the default for everyone"><i data-lucide="save" class="dash-ico-15"></i><span>Save as default</span></button>'
    +       '<button id="dash-reset-btn" class="dash-more-item" title="Reset to the default layout"><i data-lucide="rotate-ccw" class="dash-ico-15"></i><span>Reset to default</span></button>'
    +     '</div>'
    +   '</div>'
    + '</div>';
}

// Separate layout load for embedded use (doesn't touch the standalone
// _started flag path in a conflicting way with startView).
async function _loadEmbeddedLayout() {
  try {
    const j = await _getJSON('/layout');
    _cards = Array.isArray(j.cards) ? j.cards : [];
    writeInstanceCache('dashboard:layout', slimCards(), DASH_CACHE_TTL.layout);
    _started = true;
  } catch (e) {
    console.error('dashboard layout load failed (will retry on next open)', e);
  }
}

export function unmountDashboard() {
  document.getElementById('inst-dashboard-footer-dock')?.replaceChildren();
  closeMetricInfoPopover();
  stopPolling();
  if (_ro) { try { _ro.disconnect(); } catch {} _ro = null; }
  if (_mountedHost) {
    _mountedHost.innerHTML = '';
    _mountedHost = null;
  }
  _wired = false;
  _started = false;
  _cards = [];
  _snapshot = null;
  elGrid = null;
  elWrap = null;
  elEmpty = null;
  _editMode = false;
}

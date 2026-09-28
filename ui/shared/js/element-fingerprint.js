'use strict';

// ── Shared element fingerprint helpers ──────────────────────────────────
// Pure functions that describe a DOM element: its label, role, CSS path,
// computed style, markup snippet, app region, and active page. These are used
// by the launcher element-pickup toggle (webagent-launcher.js).
//
// Each function is a pure computation — no state, no side effects.
// Import: import { describeTarget, cssPath, ... } from './element-fingerprint.js';

// ── Current page ──────────────────────────────────────────────────────────
// Friendly name of the active main-panel tab, for the grounding message.
export function activePage() {
  let val = '';
  try {
    const active = document.querySelector('.tab-content.active');
    if (active && active.id && active.id.indexOf('tab-') === 0) val = active.id.slice(4);
  } catch (_) { /* ignore */ }
  if (!val) { try { val = localStorage.getItem('lastActiveTab') || ''; } catch (_) { /* ignore */ } }
  let label = '';
  try {
    const btn = document.querySelector('#main-tabs .main-tab[data-value="' + val + '"]');
    if (btn) label = (btn.textContent || '').replace(/\s+/g, ' ').trim();
  } catch (_) { /* ignore */ }
  const MAP = { browser: 'Control', genui: 'Gen UI', agents: 'Agents', sessions: 'Sessions', automations: 'Automations', wiki: 'Wiki', account: 'Account', 'admin-tools': 'Admin Tools' };
  return label || MAP[val] || (val || 'app');
}

// ── Describe what was clicked ──────────────────────────────────────────────
// Walk up to the nearest meaningful element and produce a human label + a short
// descriptor (its role / kind), so the agent knows exactly what "this" is.
// Returns { label, descriptor, el }.
export function describeTarget(el) {
  const SEL = 'button, a, [role], input, select, textarea, label, summary, li, ' +
    'h1, h2, h3, h4, h5, h6, .main-tab, .agent-card, [aria-label], [title], [data-act]';
  let node = el;
  let hit = null;
  for (let i = 0; node && node !== document.body && i < 6; i++) {
    if (node.nodeType === 1 && node.matches && node.matches(SEL)) { hit = node; break; }
    node = node.parentElement;
  }
  const target = hit || (el && el.nodeType === 1 ? el : null);
  if (!target) return { label: 'the page', descriptor: 'area', el: null };

  const clean = (s) => (s || '').replace(/\s+/g, ' ').trim().slice(0, 60);
  const attr = (n) => (target.getAttribute ? target.getAttribute(n) : '') || '';
  const label =
    clean(attr('aria-label') || attr('title')) ||
    (target.tagName === 'IMG' ? clean(attr('alt')) : '') ||
    clean(attr('placeholder')) ||
    clean(target.textContent) ||
    clean(target.id) ||
    clean(attr('data-act')) ||
    target.tagName.toLowerCase();

  const tag = target.tagName.toLowerCase();
  const TAGNAME = { a: 'link', button: 'button', select: 'dropdown', textarea: 'text field', img: 'image', li: 'list item' };
  let descriptor = attr('role') || TAGNAME[tag] || tag;
  if (tag === 'input') descriptor = (attr('type') || 'text').toLowerCase() + ' input';

  return { label: label || tag, descriptor, el: target };
}

// ── Short CSS-style locator ────────────────────────────────────────────────
// Stops at the first ancestor with an id (ids are unique), keeps 1–2 classes
// per level, disambiguates repeats with :nth-of-type, caps at 4 levels.
export function cssPath(el) {
  if (!el || el.nodeType !== 1) return '';
  const parts = [];
  let node = el;
  for (let i = 0; node && node.nodeType === 1 && node !== document.body && i < 4; i++) {
    if (node.id) { parts.unshift('#' + node.id); break; }
    let seg = node.tagName.toLowerCase();
    const cls = (typeof node.className === 'string')
      ? node.className.trim().split(/\s+/).filter(Boolean).slice(0, 2) : [];
    if (cls.length) seg += '.' + cls.join('.');
    const parent = node.parentElement;
    if (parent) {
      const sameTag = Array.prototype.filter.call(parent.children, (c) => c.tagName === node.tagName);
      if (sameTag.length > 1) seg += ':nth-of-type(' + (sameTag.indexOf(node) + 1) + ')';
    }
    parts.unshift(seg);
    node = node.parentElement;
  }
  return parts.join(' > ');
}

// ── Computed style summary ────────────────────────────────────────────────
// Human-readable: "text #333, background #fff, font 14px/600 Inter, box 120×40px"
export function styleSummary(el) {
  try {
    const cs = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const family = (cs.fontFamily || '').split(',')[0].replace(/["']/g, '').trim();
    return 'text ' + cs.color +
      ', background ' + cs.backgroundColor +
      ', font ' + cs.fontSize + '/' + cs.fontWeight + (family ? ' ' + family : '') +
      ', box ' + Math.round(r.width) + '×' + Math.round(r.height) + 'px';
  } catch (_) { return ''; }
}

// ── HTML markup slice ──────────────────────────────────────────────────────
// Whitespace-collapsed, capped at ~240 chars.
export function htmlSlice(el) {
  try {
    let h = (el.outerHTML || '').replace(/\s+/g, ' ').trim();
    if (h.length > 240) h = h.slice(0, 240) + '…';
    return h;
  } catch (_) { return ''; }
}

// ── App region ────────────────────────────────────────────────────────────
// Which broad region the element sits in: "main content area", "dialog / popup",
// "header / top navigation bar", "footer", "chat side panel".
export function regionOf(el) {
  try {
    if (!el || typeof el.closest !== 'function') return 'main area';
    const MAP = [
      ['[role="dialog"], .modal, .dialog, .popup-menu', 'dialog / popup'],
      ['#main-header', 'header / top navigation bar'],
      ['footer, [role="contentinfo"], .app-footer', 'footer'],
      ['#chat-panel', 'chat side panel'],
      ['#main-panel, main, [role="main"], .tab-content', 'main content area'],
    ];
    for (let i = 0; i < MAP.length; i++) {
      if (el.closest(MAP[i][0])) return MAP[i][1];
    }
  } catch (_) { /* ignore */ }
  return 'main area';
}

// ── Build a complete fingerprint object ──────────────────────────────────
// Combines all helpers into a single object ready to send as `app_control`.
// Provide the element and optionally the cursor x,y.
export function buildFingerprint(el, x, y) {
  const d = describeTarget(el);
  const target = d.el;
  return {
    intent: 'Element pickup',
    label: d.label,
    descriptor: d.descriptor,
    page: activePage(),
    region: regionOf(target),
    selector: cssPath(target) || '(unknown)',
    styles: styleSummary(target) || '(unavailable)',
    html: htmlSlice(target) || '(unavailable)',
    x: Math.round(x || 0),
    y: Math.round(y || 0),
  };
}

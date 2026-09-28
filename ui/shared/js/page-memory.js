'use strict';

// ── Page memory — automatic save/recall of main-panel page state ────────────
// A framework-level "remember where I was" for every main-panel page — the
// built-in ones (Gen UI, Sessions, Agents, …) and any drop-in plugin page the
// admin drops into ui/main-panel/<id>/. tabs.js drives it automatically around
// every page's start/stop lifecycle, so a page needs NO code to participate:
//
//   • Automatic DOM snapshot (default ON for every catalog page): when the user
//     leaves the page (tab switch, page flip, reload) the shell captures the
//     page mount's scroll positions, open <details>, aria-expanded toggles,
//     <select> values, checkbox/radio state, and every element the page marks
//     with data-memory="…". On return (tab switch or refresh) it is re-applied.
//   • data-memory: a page marks a stateful element <button data-memory="view">
//     and its checked / value / active-class / aria-selected state is remembered
//     under that key (best-effort appearance/state; wire behaviour via the
//     remember/recall hooks below).
//   • remember/recall hooks: the page.json descriptor declares
//     "memory": {"remember": "fnName", "recall": "fnName"} and the entry module
//     exports those functions — remember() returns arbitrary state when the
//     user leaves, recall(state) receives it when the page returns. For state
//     that lives in JS (the open genui slug, the selected session id) rather
//     than the DOM.
//   • Opt out: "memory": false in page.json disables everything for that page.
//
// Everything is keyed per page + per user and lives ONLY in the browser's
// localStorage. SECRETS NEVER PERSIST: free-text inputs (text/password/email/
// url/tel/textarea) are deliberately skipped — credential fields go straight to
// the vault.
//
// Storage shape (one localStorage key per page, one row per user):
//   webagent.page.<pageId>.v1 = { [userId]: { dom: {scroll, struct, els}, custom: … } }
//
// Convenience accessors:
//   pageMemory.setItem(pageId, key, value) / getItem(pageId, key) — read/write
//   one custom field without touching the DOM snapshot (e.g. "last open slug").

const _PREFIX = 'webagent.page.';
// Cap the descendant scan so big pages (Agents, Admin Tools) stay cheap on tab
// switch — we stop after N nodes or M milliseconds, whichever comes first.
const _SCAN_BUDGET = { nodes: 2000, ms: 6 };
// Scroll re-apply retries: the page's own start may still be building content,
// so we retry on a short schedule (mirrors genui.js' restore schedule).
const _SCROLL_RETRIES = [0, 60, 150, 320, 650, 1100];
// Inputs whose value is free-form text and is therefore never persisted.
const _NO_VALUE_TYPES = ['text', 'password', 'email', 'url', 'tel'];

function _uid() {
  try { return (window.app && window.app.currentUserId) || 'anon'; } catch (_) { return 'anon'; }
}

function _key(pageId) { return _PREFIX + pageId + '.v1'; }

function _read(pageId) {
  try {
    const m = JSON.parse(localStorage.getItem(_key(pageId)) || '{}') || {};
    return m[_uid()] || null;
  } catch (_) { return null; }
}

function _write(pageId, state) {
  try {
    const m = JSON.parse(localStorage.getItem(_key(pageId)) || '{}') || {};
    m[_uid()] = state;
    localStorage.setItem(_key(pageId), JSON.stringify(m));
  } catch (_) {}
}

// ── Stable element addressing ────────────────────────────────────────────────
// A path from the mount container to an element using element-child indexes.
// The page's partial is rebuilt identically on each boot, so the same path
// resolves to the same node after a refresh or a re-render.

function _elPath(el, root) {
  const parts = [];
  let node = el;
  while (node && node !== root) {
    const parent = node.parentNode;
    if (!parent || !parent.children) break;
    parts.unshift(Array.prototype.indexOf.call(parent.children, node));
    node = parent;
  }
  return parts.join('/');
}

function _resolvePath(path, root) {
  let node = root;
  for (const seg of String(path == null ? '' : path).split('/')) {
    if (!node || !node.children) return null;
    node = node.children[+seg];
  }
  return node || null;
}

// ── Snapshot helpers ─────────────────────────────────────────────────────────

function _pushScroll(list, el, root) {
  try {
    list.push({ p: _elPath(el, root), st: el.scrollTop || 0, sl: el.scrollLeft || 0 });
  } catch (_) {}
}

// Merge one structural fact (open / aria-expanded / select value / checked)
// into the path-keyed struct map — a single element may contribute several.
function _putStruct(struct, path, d) {
  if (!path) return;
  struct[path] = Object.assign(struct[path] || {}, d);
}

function _captureMemEl(out, el) {
  const key = el.getAttribute('data-memory');
  if (!key) return;
  const tag = el.tagName;
  if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
    const t = (el.type || '').toLowerCase();
    if (tag === 'TEXTAREA' || _NO_VALUE_TYPES.indexOf(t) !== -1) return; // may hold secrets/personal data
    if (t === 'checkbox' || t === 'radio') out[key] = { ck: !!el.checked };
    else out[key] = { val: el.value };
    return;
  }
  const d = {};
  if (el.classList && el.classList.contains('active')) d.active = true;
  if (el.hasAttribute && el.hasAttribute('aria-selected')) d.ax = el.getAttribute('aria-selected');
  if (Object.keys(d).length) out[key] = d;
}

// Structural state of one element, if it has any worth remembering. Mirrors the
// genui page's rule set: open <details>, aria-expanded, <select> value and
// checkbox/radio — free-text inputs are never persisted (may hold secrets).
function _captureStructEl(el) {
  const tag = el.tagName;
  const d = {};
  if (tag === 'DETAILS') d.open = !!el.open;
  if (el.hasAttribute && el.hasAttribute('aria-expanded')) d.ax = el.getAttribute('aria-expanded');
  if (tag === 'SELECT') d.val = el.value;
  if (tag === 'INPUT') {
    const t = (el.type || '').toLowerCase();
    if (t === 'checkbox' || t === 'radio') d.ck = !!el.checked;
  }
  return Object.keys(d).length ? d : null;
}

export const pageMemory = {

  // Descriptor-driven: on for every catalog page unless "memory": false.
  enabled(page) {
    return !!(page && page.memory !== false);
  },

  save(pageId, state) { _write(pageId, state); },
  load(pageId) { return _read(pageId); },

  clear(pageId) {
    try {
      const m = JSON.parse(localStorage.getItem(_key(pageId)) || '{}') || {};
      delete m[_uid()];
      localStorage.setItem(_key(pageId), JSON.stringify(m));
    } catch (_) {}
  },

  // Read/write ONE custom field (e.g. "last open slug") without touching the
  // DOM snapshot. The shell's remember/recall hooks are the richer path; these
  // are for pages that want a single scalar remembered at any time.
  setItem(pageId, key, value) {
    const s = _read(pageId) || { dom: null, custom: {} };
    if (!s.custom || typeof s.custom !== 'object') s.custom = {};
    s.custom[key] = value;
    _write(pageId, s);
  },

  getItem(pageId, key) {
    const s = _read(pageId);
    return (s && s.custom && typeof s.custom === 'object') ? s.custom[key] : undefined;
  },

  // Snapshot the page mount: its own scroll + scrollable descendants and
  // structural state (open <details>, aria-expanded, <select> values,
  // checkbox/radio — free-text inputs are never persisted), plus every element
  // carrying data-memory="…". The descendant scan is budget-capped so big pages
  // stay cheap on tab switch. Pure data — never throws.
  captureDom(container) {
    const snap = { scroll: [], struct: {}, els: {} };
    if (!container) return snap;
    _pushScroll(snap.scroll, container, container);
    try {
      for (const el of container.querySelectorAll('[data-memory]')) _captureMemEl(snap.els, el);
    } catch (_) {}
    try {
      const t0 = performance.now();
      let n = 0;
      for (const el of container.querySelectorAll('*')) {
        if (++n > _SCAN_BUDGET.nodes || performance.now() - t0 > _SCAN_BUDGET.ms) break;
        if (el.scrollTop || el.scrollLeft) _pushScroll(snap.scroll, el, container);
        const d = _captureStructEl(el);
        if (d) _putStruct(snap.struct, _elPath(el, container), d);
      }
    } catch (_) {}
    return snap;
  },

  // Re-apply a snapshot. data-memory + structural state applies immediately; the
  // scroll positions retry on a short schedule because the page's own start may
  // still be building content. The mount's own scroll is only re-applied while
  // it hasn't reached the saved position yet (so a late retry can't yank the
  // user back after they've scrolled away) — mirroring genui.js' restore logic.
  restoreDom(container, snapshot) {
    if (!container || !snapshot) return;
    try {
      for (const el of container.querySelectorAll('[data-memory]')) {
        const d = snapshot.els[el.getAttribute('data-memory')];
        if (!d) continue;
        const tag = el.tagName;
        if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') {
          const t = (el.type || '').toLowerCase();
          if (t === 'checkbox' || t === 'radio') { if ('ck' in d) el.checked = !!d.ck; }
          else if ('val' in d && tag !== 'TEXTAREA' && _NO_VALUE_TYPES.indexOf(t) === -1) el.value = d.val;
        } else {
          if (d.active && el.classList) el.classList.add('active');
          if (d.ax !== undefined && el.hasAttribute('aria-selected')) el.setAttribute('aria-selected', d.ax);
        }
      }
      for (const p of Object.keys(snapshot.struct || {})) {
        const d = snapshot.struct[p];
        const el = p === '' ? container : _resolvePath(p, container);
        if (!el) continue;
        const tag = el.tagName;
        if ('open' in d && tag === 'DETAILS') el.open = !!d.open;
        if (d.ax !== undefined && el.hasAttribute('aria-expanded')) el.setAttribute('aria-expanded', d.ax);
        if ('val' in d && tag === 'SELECT') el.value = d.val;
        if ('ck' in d && tag === 'INPUT') {
          const t = (el.type || '').toLowerCase();
          if (t === 'checkbox' || t === 'radio') el.checked = !!d.ck;
        }
      }
    } catch (_) {}
    const scroll = (snapshot.scroll) || [];
    if (!scroll.length) return;
    for (const delay of _SCROLL_RETRIES) {
      setTimeout(() => {
        if (container.isConnected === false) return;
        for (const s of scroll) {
          const el = s.p === '' ? container : _resolvePath(s.p, container);
          if (!el) continue;
          if (s.p === '') {
            const want = s.st || 0;
            if (!want || Math.abs((el.scrollTop || 0) - want) <= 2) continue;
          }
          if (s.sl) el.scrollLeft = s.sl;
          if (s.st) el.scrollTop = s.st;
        }
      }, delay);
    }
  },
};

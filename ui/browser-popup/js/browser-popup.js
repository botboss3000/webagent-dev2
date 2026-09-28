'use strict';

// Browser Popup — a floating, agent-openable in-app browser window.
//
// The agent's browser_control ability (plugins/abilities/Web/browser_control/)
// can surface a web page to the user in a small floating card ("check this site
// for me") and close it again, via its `browser_popup` tool. That tool emits a
// `ui_command` WebSocket event (action: 'browser_popup', mode: 'open'|'close')
// on the always-on per-user socket; the shared handler in
// ui/shared/js/agentWs.js routes it here through window.__browserPopup(event).
// This mirrors the App Control ability's set_view → window.__setMainTab seam.
//
// The page itself is rendered through the SAME server proxy the Browser page
// uses (app/api/browser_stream.py GET /api/v1/browser/proxy) — Playwright fetches
// the real page server-side and re-serves it, so sites that refuse cross-origin
// framing still render (unlike a raw <iframe src>). Link clicks inside the proxy
// post 'webagent-navigate' to the parent; we follow them by reloading the frame.
//
// The user can always close the popup themselves (× button / Escape); the agent
// can also close it (mode: 'close'). Wired once at boot from
// ui/shared/js/main.js (initBrowserPopup). Styles: ui/browser-popup/browser-popup.css.
//
// REMOVE-WHEN: the Browser Control ability is dropped from the ability catalog.

import { apiPath } from '../../shared/js/config.js';
import { getAuthToken } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';

// Single reused popup instance — one floating window at a time. A second `open`
// re-points the existing card at the new URL rather than stacking windows.
let _root = null;        // outermost fixed overlay element
let _frame = null;       // the proxy <iframe>
let _titleEl = null;
let _urlEl = null;
let _loadingEl = null;
let _bsId = null;        // browser session backing the current page
let _msgBound = false;   // window 'message' listener installed once

// Build the proxy URL exactly like the Browser page (ui/main-panel/browser/js/
// browser.js _proxyUrl): the JWT rides in the query string because an iframe
// request can't set an Authorization header.
function _proxyUrl(bsId, pageUrl) {
  const token = encodeURIComponent(getAuthToken() || '');
  return apiPath('/api/v1/browser/proxy') +
    '?token=' + token +
    '&bs_id=' + encodeURIComponent(bsId) +
    '&url=' + encodeURIComponent(pageUrl);
}

// Pretty host for the sub-title (falls back to the raw string).
function _hostOf(url) {
  try { return new URL(url).host; } catch (_) { return url || ''; }
}

// Load (or reload) the frame at a target URL through the proxy.
function _loadFrame(url) {
  if (!_frame || !_bsId || !url) return;
  if (_loadingEl) _loadingEl.classList.remove('hidden');
  _frame.src = _proxyUrl(_bsId, url);
  if (_urlEl) _urlEl.textContent = _hostOf(url);
}

// Follow link clicks reported by the proxy bridge script — but ONLY for our own
// frame, so we never react to the Browser page's iframe (or it to ours).
function _onMessage(e) {
  if (!_frame || !e || !e.data || typeof e.data !== 'object') return;
  if (e.source !== _frame.contentWindow) return;
  const d = e.data;
  if (d.type === 'webagent-navigate' && d.url) {
    _loadFrame(d.url);                 // proxy re-fetches → Playwright follows too
  } else if (d.type === 'webagent-ready') {
    if (_loadingEl) _loadingEl.classList.add('hidden');
  } else if (d.type === 'webagent-urlchange' && d.url && _urlEl) {
    _urlEl.textContent = _hostOf(d.url);
  }
}

// Make the card draggable by its header. Uses document-level listeners only for
// the duration of a drag (this is app chrome, not a genui, so that's fine).
function _makeDraggable(card, handle) {
  let sx = 0, sy = 0, ox = 0, oy = 0, dragging = false;
  function down(ev) {
    if (ev.button != null && ev.button !== 0) return;
    if (ev.target.closest('button')) return;   // header buttons aren't drag starts
    dragging = true;
    const r = card.getBoundingClientRect();
    // Switch from transform-centring to explicit left/top on first grab.
    card.style.left = r.left + 'px';
    card.style.top = r.top + 'px';
    card.style.transform = 'none';
    card.style.margin = '0';
    sx = ev.clientX; sy = ev.clientY; ox = r.left; oy = r.top;
    document.addEventListener('pointermove', move);
    document.addEventListener('pointerup', up, { once: true });
    ev.preventDefault();
  }
  function move(ev) {
    if (!dragging) return;
    const nx = Math.max(0, Math.min(window.innerWidth - 80, ox + (ev.clientX - sx)));
    const ny = Math.max(0, Math.min(window.innerHeight - 40, oy + (ev.clientY - sy)));
    card.style.left = nx + 'px';
    card.style.top = ny + 'px';
  }
  function up() {
    dragging = false;
    document.removeEventListener('pointermove', move);
  }
  handle.addEventListener('pointerdown', down);
}

function _build() {
  if (_root) return;

  _root = document.createElement('div');
  _root.className = 'wa-bpop';
  _root.setAttribute('role', 'dialog');
  _root.setAttribute('aria-label', 'Browser');

  const card = document.createElement('div');
  card.className = 'wa-bpop-card';

  const head = document.createElement('div');
  head.className = 'wa-bpop-head';

  const titleWrap = document.createElement('div');
  titleWrap.className = 'wa-bpop-titlewrap';
  _titleEl = document.createElement('div');
  _titleEl.className = 'wa-bpop-title';
  _urlEl = document.createElement('div');
  _urlEl.className = 'wa-bpop-url';
  titleWrap.appendChild(_titleEl);
  titleWrap.appendChild(_urlEl);

  const actions = document.createElement('div');
  actions.className = 'wa-bpop-actions';

  const reloadBtn = document.createElement('button');
  reloadBtn.type = 'button';
  reloadBtn.className = 'wa-bpop-btn';
  reloadBtn.title = 'Reload';
  reloadBtn.innerHTML = icon('rotate-cw');
  reloadBtn.addEventListener('click', () => {
    if (_frame && _frame.src) { if (_loadingEl) _loadingEl.classList.remove('hidden'); _frame.src = _frame.src; }
  });

  const extBtn = document.createElement('button');
  extBtn.type = 'button';
  extBtn.className = 'wa-bpop-btn';
  extBtn.title = 'Open in a real browser tab';
  extBtn.innerHTML = icon('external-link');
  extBtn.addEventListener('click', () => {
    const u = _root && _root.dataset.url;
    if (u) window.open(u, '_blank', 'noopener');
  });

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'wa-bpop-btn wa-bpop-close';
  closeBtn.title = 'Close';
  closeBtn.innerHTML = icon('x');
  closeBtn.addEventListener('click', () => close());

  actions.appendChild(reloadBtn);
  actions.appendChild(extBtn);
  actions.appendChild(closeBtn);

  head.appendChild(titleWrap);
  head.appendChild(actions);

  const body = document.createElement('div');
  body.className = 'wa-bpop-body';
  _frame = document.createElement('iframe');
  _frame.className = 'wa-bpop-frame';
  _frame.setAttribute('title', 'Web page');
  _loadingEl = document.createElement('div');
  _loadingEl.className = 'wa-bpop-loading';
  _loadingEl.textContent = 'Loading…';
  body.appendChild(_frame);
  body.appendChild(_loadingEl);

  card.appendChild(head);
  card.appendChild(body);
  _root.appendChild(card);
  document.body.appendChild(_root);

  _makeDraggable(card, head);

  if (!_msgBound) {
    window.addEventListener('message', _onMessage);
    _msgBound = true;
  }
}

// Public open — driven by the agent event (or callable directly for testing).
export function open({ url, title, bs_id } = {}) {
  if (!url) return;
  // Without a browser session we can't proxy-render (framing-blocked sites would
  // fail); fall back to a real browser tab rather than showing a broken frame.
  if (!bs_id) { window.open(url, '_blank', 'noopener'); return; }

  _build();
  _bsId = bs_id;
  _root.dataset.url = url;
  _titleEl.textContent = title || 'Browser';
  _root.classList.add('open');
  _loadFrame(url);
}

export function close() {
  if (!_root) return;
  _root.classList.remove('open');
  // Drop the frame so the proxied page stops running while hidden.
  if (_frame) _frame.src = 'about:blank';
  _bsId = null;
}

// Wired once at boot. Registers the window global the shared WS handler calls.
export function initBrowserPopup() {
  window.__browserPopup = function (event) {
    try {
      const mode = event && event.mode;
      if (mode === 'close') { close(); return; }
      open({ url: event.url, title: event.title, bs_id: event.bs_id });
    } catch (_) { /* never let a bad event break the socket handler */ }
  };

  // Escape closes the popup when it's open (scoped: only acts while open).
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && _root && _root.classList.contains('open')) {
      close();
    }
  });
}

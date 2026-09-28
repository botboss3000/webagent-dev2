'use strict';

// ── Web page: an in-app, AI-augmented browser ──────────────────────────────
// REMOVE-WHEN: the Browser page is dropped from the page catalog — this whole
// folder (ui/main-panel/browser/) is a drop-in page and dies with it.
// Markup lives in ui/main-panel/browser/browser.html (injected into #tab-browser by
// ui/shared/js/partial-loader.js); styles in ui/main-panel/browser/browser.css. This
// module loads the shared Playwright page into an
// <iframe> through the proxy endpoint (app/api/browser_stream.py) and keeps a
// WebSocket for navigation commands.
//
// The browser session (bs_id) is a first-class, persistent tab that lives beside
// chat. The per-tab "Share with agent" toggle flips the row's `shared` flag:
// when on, the linked agent sees and can act on this same page.
//
// Link clicks inside the iframe are intercepted by an injected bridge script,
// sent to this parent frame via postMessage, then forwarded to the server so
// Playwright navigates and the proxy re-fetches the page — keeping user and
// agent in sync.

import { app } from '../../../shared/js/state.js';
import { browserWsUrl, browserScreencastWsUrl, desktopControlWsUrl, apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { connectionTicket } from '../../../shared/js/auth-refresh.js';
import { icon } from '../../../shared/js/icons.js';
import { copyText } from '../../../shared/js/clipboard.js';
// ── Interactive-shared-browser layer (all drop-in, this folder only) ──
//   overlay.js     — paints agent/user cursors, click ripples, telegraph halos.
//   ticker.js      — the action ticker caption strip + "who's driving" badge.
//   live-events.js — a dedicated subscriber for the agent's `browser_live` feed.
import * as overlay from './overlay.js';
import * as ticker from './ticker.js';
import { startLiveEvents, stopLiveEvents } from './live-events.js';

let ws = null;
let mounted = false;
let active = false;
let connectedBs = null;     // bs_id the live socket is connected to
let currentBs = null;       // the full browser-session row {id, shared, agent_id, …}
let reconnectTimer = null;
let els = null;
// Control deliberately opens idle. Neither Playwright nor desktop capture is
// started until the user explicitly selects a surface.
let surfaceMode = 'idle'; // 'idle' | 'browser' | 'extension' | 'desktop'
let desktopWs = null;
let desktopReconnectTimer = null;
let extensionStatusTimer = null;
let lastBrowserBackend = 'headless';

// ── UNIFIED-PILL state ──────────────────────────────────────────────────────
// The single bottom pill is BOTH the address bar and the agent chat box. It
// rests showing the current page URL; while the user is editing we never
// overwrite it (so an agent navigation can't clobber a half-typed message).
let _currentUrl = '';   // the page's real URL, tracked independently of the input
let _editing = false;   // is the user focused/typing in the pill right now?

// ── Device backend + live pixel mirror state ───────────────────────────────
// A session can be driven by the in-app headless browser (view = proxy iframe)
// or the REAL browser on the user's device (view = screencast genui mirror).
let scWs = null;            // screencast WebSocket
let scReconnectTimer = null;
let viewMode = 'proxy';     // 'proxy' (iframe) | 'mirror' (genui screencast)
let deviceOn = false;       // is this session on the on-device (local) backend?
// Which backend drives this session: 'headless' (in-app), 'local' (the real
// browser on this machine), or 'connector' (the user's OWN browser, driven
// through the WebAgent extension). deviceOn stays a derived alias of
// backendMode==='local' so the existing screencast/mirror logic is untouched.
let backendMode = 'headless';
let connectorConnected = false;  // is the user's browser extension live right now?
let mirrorSpace = { width: 1280, height: 720 };  // CSS-pixel coord space of the page
let mirrorImg = null;       // reusable <img> for decoding JPEG frames
let _lastMoveSent = 0;      // throttle mousemove forwarding
// Last JPEG frame we painted, kept so we can instantly restore the page when the
// user flips back to the Browser tab — see suspend()/resumeRetained() below.
let _lastFrameB64 = null;
// Per-session view snapshot, so flipping BETWEEN browser sessions (the sessions
// popover) restores each one instantly — like real browser tabs — instead of
// blanking to a reload while the new session reconnects. The pages themselves
// stay alive on the server; this only remembers what each looked like.
// bs_id → { frame, url, hasRealPage, deviceOn }
const _sessionViews = {};

function mount() {
  const root = document.getElementById('tab-browser');
  if (!root) return false;
  els = {
    root,
    frame: root.querySelector('.browser-frame'),
    mirror: root.querySelector('.browser-mirror'),
    empty: root.querySelector('.browser-empty'),
    loading: root.querySelector('.browser-loading'),
    stage: root.querySelector('.browser-stage'),
    dot: root.querySelector('.browser-dot'),
    statusText: root.querySelector('.browser-status-text'),
    device: root.querySelector('.web-device'),
    deviceLabel: root.querySelector('.browser-device-label'),
    connectorNotice: root.querySelector('.browser-connector-notice'),
    share: root.querySelector('.web-share'),
    sessionsToggle: root.querySelector('.web-sessions-toggle'),
    sessionsTriggerLabel: root.querySelector('.browser-session-trigger-label'),
    newSessionBtn: root.querySelector('.web-new-session'),
    sessionsPopover: root.querySelector('.browser-sessions-popover'),
    sessionsList: root.querySelector('.browser-sessions-list'),
    promptRow: root.querySelector('.browser-prompt-row'),
    promptInput: root.querySelector('.web-prompt-input'),
    send: root.querySelector('.web-send'),
    voice: root.querySelector('.web-voice'),
    attach: root.querySelector('.web-attach'),
    point: root.querySelector('.web-point'),
    page: root.querySelector('.browser-page'),
    surfaceButtons: Array.from(root.querySelectorAll('.control-surface-btn')),
    surfaceStops: Array.from(root.querySelectorAll('.control-surface-stop')),
    extensionPanel: root.querySelector('.extension-control-panel'),
    extensionState: root.querySelector('.extension-connection-state'),
    extensionStateLabel: root.querySelector('.extension-state-label'),
    extensionDetails: Array.from(root.querySelectorAll('[data-extension-detail]')),
    extensionSettings: Array.from(root.querySelectorAll('[data-extension-setting]')),
    extensionPair: root.querySelector('.extension-pair-action'),
    extensionRefresh: root.querySelector('.extension-refresh-action'),
  };
  if (!els.stage || !els.frame) { els = null; return false; }

  // ── Interactive overlay layer ──
  // A transparent, click-through layer over the mirror genui that the overlay
  // module paints into (cursors / ripples / telegraphs). Created here so the
  // page markup stays minimal; the module owns everything inside it.
  els.overlay = document.createElement('div');
  els.overlay.className = 'browser-overlay-layer';
  els.stage.appendChild(els.overlay);
  try { overlay.initOverlay(els.overlay, els.mirror); } catch (_) {}
  try { ticker.initTicker(els.stage); } catch (_) {}

  wireInput();
  els.surfaceButtons.forEach((button) => {
    button.addEventListener('click', () => switchSurface(button.dataset.surface));
  });
  els.surfaceStops.forEach((button) => {
    button.addEventListener('click', (event) => stopControlSurface(button.dataset.stopSurface, button, event));
  });
  if (els.extensionPair) els.extensionPair.addEventListener('click', openPairModal);
  if (els.extensionRefresh) els.extensionRefresh.addEventListener('click', refreshExtensionStatus);
  els.extensionSettings.forEach((input) => {
    input.addEventListener('change', () => updateExtensionSetting(input));
  });
  return true;
}

// ── "Who's driving" state ───────────────────────────────────────────────────
// Each agent/user action flips the driver badge to that actor and arms a short
// relax timer; after a quiet beat the badge fades to idle. Last actor wins.
let _driverTimer = null;
function markDriver(actor) {
  try { ticker.setDriver(actor); } catch (_) {}
  clearTimeout(_driverTimer);
  _driverTimer = setTimeout(() => { try { ticker.setDriver(null); } catch (_) {} }, 2600);
}

function setStatus(state, text) {
  if (!els) return;
  els.dot.className = 'browser-dot' + (state === 'live' ? ' live' : state === 'down' ? ' down' : '');
  els.statusText.textContent = text;
}

function send(obj) {
  if (ws && ws.readyState === WebSocket.OPEN) {
    try { ws.send(JSON.stringify(obj)); } catch (_) {}
  }
}

// ── Iframe navigation ──────────────────────────────────────────────────────
// The iframe loads pages through the server proxy, which fetches the real page
// via Playwright and injects a bridge script.  The proxy URL carries auth so
// the iframe's request is authenticated.

let _lastNavUrl = null;  // last URL we told the server to navigate to (deduplicates echoed navs)
let _frameLoadGeneration = 0;

function _proxyUrl(bsId, pageUrl, ticket) {
  return apiPath('/api/v1/browser/proxy') +
    '?ticket=' + encodeURIComponent(ticket) +
    '&bs_id=' + encodeURIComponent(bsId) +
    '&url=' + encodeURIComponent(pageUrl);
}

async function _loadFrame(url) {
  if (!els || !els.frame || !currentBs) return;
  const generation = ++_frameLoadGeneration;
  const bsId = currentBs.id;
  _lastNavUrl = url;
  const ticket = await connectionTicket('/api/v1/browser/proxy');
  if (
    !ticket || generation !== _frameLoadGeneration ||
    !els || !els.frame || !currentBs || currentBs.id !== bsId
  ) return;
  els.frame.src = _proxyUrl(bsId, url, ticket);
}

function _showFrame() {
  if (!els) return;
  if (!els.frame.classList.contains('has-frame')) {
    els.frame.classList.add('has-frame');
    _setStage('page');
  }
}

// ── Bridge script messages ─────────────────────────────────────────────────
// The injected bridge script posts these messages from inside the iframe:
//   { type: 'webagent-navigate', url }   — user clicked a link
//   { type: 'webagent-ready', url, title } — page finished loading
//   { type: 'webagent-urlchange', url, title } — SPA pushState/replaceState/popstate

window.addEventListener('message', function(e) {
  if (!e.data || typeof e.data !== 'object') return;
  var d = e.data;
  if (viewMode === 'mirror') return;  // the iframe bridge is irrelevant in mirror mode
  if (d.type === 'webagent-navigate' && d.url) {
    // User clicked a link inside the iframe.  Optimistically load the new page
    // in the iframe while we tell the server to navigate Playwright.
    _lastNavUrl = d.url;
    _loadFrame(d.url);
    _showFrame();
    send({ action: 'navigate', url: d.url });
  } else if (d.type === 'webagent-ready' && d.url) {
    // The proxy page finished loading in the iframe.
    _showFrame();
    setStatus('live', 'live');
  } else if (d.type === 'webagent-urlchange' && d.url) {
    // SPA navigation happened inside the iframe — the resting pill should reflect it.
    _setUrlDisplay(d.url);
    // Tell the server about the SPA navigation so the agent stays in sync.
    send({ action: 'navigate', url: d.url });
  }
});

// ── UNIFIED-PILL: address bar ⊕ agent chat ─────────────────────────────────
// One pill does both jobs. As the user types we classify the text: a URL flips
// the action icon to a globe and Enter/Send navigates the page; anything else
// keeps the send icon and Enter/Send messages the agent in the active session.

// Heuristic: a single whitespace-free token that has a scheme, is localhost/an
// IP, or is a dotted domain (example.com, a.b.co/path) reads as a URL. Anything
// with a space — or a pending point-and-ask — is treated as a message.
function looksLikeUrl(raw) {
  const s = (raw || '').trim();
  if (!s || /\s/.test(s)) return false;
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) return true;        // scheme://…
  const host = s.split(/[/?#]/)[0];                            // strip path/query
  if (/^localhost(:\d+)?$/i.test(host)) return true;
  if (/^(\d{1,3}\.){3}\d{1,3}(:\d+)?$/.test(host)) return true; // bare IP[:port]
  return /^([a-z0-9-]+\.)+[a-z]{2,}$/i.test(host);             // dotted domain.tld
}

// Show a URL as the pill's resting value — but only when the user isn't editing,
// so a live agent navigation never overwrites a half-typed message.
function _setUrlDisplay(url) {
  if (!url || url === 'about:blank') return;
  _currentUrl = url;
  if (!els || !els.promptInput || _editing) return;
  els.promptInput.value = url;
  els.promptInput.style.height = 'auto';
  _refreshPillMode();
}

// Swap the action button between "send to agent" and "go to page".
function _setSendMode(mode) {
  if (!els || !els.send || els.send._mode === mode) return;
  els.send._mode = mode;
  els.send.innerHTML = icon(mode === 'url' ? 'globe' : 'send');
  els.send.title = mode === 'url' ? 'Go to this page' : 'Send to the agent';
  if (els.promptRow) els.promptRow.classList.toggle('url-mode', mode === 'url');
}

// Reflect the current input into the pill chrome: has-text (mic↔send swap) and
// the URL-vs-message action mode.
function _refreshPillMode() {
  if (!els || !els.promptInput) return;
  const raw = els.promptInput.value;
  const has = raw.trim().length > 0;
  if (els.promptRow) els.promptRow.classList.toggle('has-text', has);
  const isUrl = surfaceMode === 'browser' && has && !_pendingPoint && looksLikeUrl(raw);
  _setSendMode(isUrl ? 'url' : 'message');
}

// Submit handler for the unified pill — route by what was typed.
function submitPill() {
  if (!els || !els.promptInput) return;
  const raw = els.promptInput.value;
  if (surfaceMode === 'browser' && !_pendingPoint && looksLikeUrl(raw)) navigateTo(raw.trim());
  else submitPrompt();
}

function navigateTo(v) {
  if (!v) return;
  _lastNavUrl = v;
  send({ action: 'navigate', url: v });
  // Drop edit mode and rest the pill on the URL we're heading to; the server's
  // `nav`/`ready` echo will refine it (e.g. add the scheme) when it lands.
  _editing = false;
  if (els && els.promptInput) els.promptInput.blur();
  _setUrlDisplay(v);
}

// Bottom chat pill submit → chat with the agent about the page.
function submitPrompt() {
  if (!els || !els.promptInput) return;
  let text = els.promptInput.value.trim();
  if (!text && !_pendingPoint) return;
  // Point-and-ask: if the user marked a spot on the page, prepend a grounding
  // line so the agent knows WHERE on the shared page they're pointing. The agent
  // can resolve the element with browser_action evaluate + document.elementFromPoint.
  if (_pendingPoint) {
    const { x, y } = _pendingPoint;
    const w = mirrorSpace.width, h = mirrorSpace.height;
    const lead = surfaceMode === 'desktop'
      ? `[Pointing at the server desktop near x=${Math.round(x)}, y=${Math.round(y)} ` +
        `(desktop screenshot is ${w}×${h} pixels; use computer_screenshot to inspect it).] `
      : `[Pointing at the shared browser page near x=${Math.round(x)}, y=${Math.round(y)} ` +
        `(viewport is ${w}×${h} CSS px; use document.elementFromPoint(${Math.round(x)}, ${Math.round(y)}) ` +
        `to identify the element).] `;
    text = lead + (text || 'What is this, and what can you do with it?');
    _clearPoint();
  }
  if (app.chatInput && app.chatSend) {
    app.chatInput.value = text;
    try { app.chatInput.dispatchEvent(new Event('input', { bubbles: true })); } catch (_) {}
    app.chatSend.click();
  }
  // Message sent — drop edit mode and let the pill rest back on the page URL.
  _editing = false;
  els.promptInput.value = '';
  els.promptInput.style.height = 'auto';
  if (els.promptRow) els.promptRow.classList.remove('has-text');
  if (surfaceMode === 'browser') _setUrlDisplay(_currentUrl);
}

// ── Point-and-ask ───────────────────────────────────────────────────────────
// Turn the live page into a pointing surface: toggle the mode, click a spot, and
// the next chat prompt carries that page-space coordinate so a question is
// grounded ("what is THIS?"). The click is captured (not forwarded to the page).
let _pointMode = false;
let _pendingPoint = null;   // { x, y } in page CSS pixels, awaiting the next prompt

function togglePointMode() {
  _pointMode = !_pointMode;
  if (!_pointMode) _clearPoint();
  _updatePointUi();
  if (_pointMode && els && els.promptInput) {
    // Nudge the user toward the next step.
    els.promptInput.placeholder = 'Click a spot on the page, then ask about it…';
  } else if (els && els.promptInput) {
    els.promptInput.placeholder = surfaceMode === 'desktop'
      ? 'Message the agent about this desktop…'
      : 'Enter a URL, or message the agent…';
  }
}

function _capturePoint(p) {
  _pendingPoint = { x: p.x, y: p.y };
  try { overlay.showClick({ point: p }, 'user'); } catch (_) {}
  try { ticker.pushTicker('Pointing here — ask the agent about it', 'user'); } catch (_) {}
  if (els && els.promptInput) els.promptInput.focus();
  _updatePointUi();
}

function _clearPoint() {
  _pendingPoint = null;
  _updatePointUi();
}

function _updatePointUi() {
  if (!els || !els.point) return;
  const armed = _pointMode || !!_pendingPoint;
  els.point.classList.toggle('on', armed);
  els.point.setAttribute('aria-pressed', armed ? 'true' : 'false');
  els.point.title = _pendingPoint
    ? 'A spot is marked — your next message asks the agent about it'
    : _pointMode
      ? 'Point mode on — click a spot on the page'
      : 'Point at something on the page to ask the agent about it';
}

function wireInput() {
  if (!els) return;

  // Navigation buttons
  els.root.querySelectorAll('.browser-btn').forEach((b) => {
    b.addEventListener('click', (e) => {
      const act = b.dataset.act;
      if (act === 'back') send({ action: 'back' });
      else if (act === 'forward') send({ action: 'forward' });
      else if (act === 'reload') send({ action: 'reload' });
      else if (act === 'sessions') toggleSessionsPopover(e);
      else if (act === 'new-session') openNewSession();
      else if (act === 'share') toggleShare();
      else if (act === 'device') cycleBackend();
      else if (act === 'pair') openPairModal();
      else if (act === 'point') togglePointMode();
    });
  });

  wireMirrorInput();

  // ── Unified pill (address bar ⊕ agent chat) ──
  if (els.promptInput) {
    // Did the user actually type during this focus session? Drives the blur
    // revert: an untouched pill always re-rests on the (possibly newly-navigated)
    // page URL, but a real draft the user typed is preserved across blur.
    let dirty = false;
    const onInput = () => {
      dirty = true;
      els.promptInput.style.height = 'auto';
      els.promptInput.style.height = els.promptInput.scrollHeight + 'px';
      _refreshPillMode();
    };
    els.promptInput.addEventListener('input', onInput);
    els.promptInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitPill(); }
    });
    // Address-bar focus behaviour: entering the pill arms edit mode and, if it's
    // resting on the URL, selects it so typing replaces it.
    els.promptInput.addEventListener('focus', () => {
      _editing = true;
      dirty = false;
      if (els.promptInput.value && els.promptInput.value === _currentUrl) {
        try { els.promptInput.select(); } catch (_) {}
      }
    });
    // On blur: if the user never typed (or cleared the field), re-rest on the
    // current page URL — this also picks up any navigation that happened while
    // the pill was focused. A non-empty draft they typed is left in place.
    els.promptInput.addEventListener('blur', () => {
      _editing = false;
      if (!dirty || !els.promptInput.value.trim()) _setUrlDisplay(_currentUrl);
    });
  }
  if (els.send) els.send.addEventListener('click', submitPill);
}

// ── Browser-session REST ───────────────────────────────────────────────────

async function apiJson(path, opts = {}) {
  const headers = {
    'Content-Type': 'application/json',
    ...authHeaders(),
    ...(opts.headers || {}),
  };
  const r = await fetch(apiPath(path), { ...opts, headers });
  if (!r.ok) throw new Error('http ' + r.status);
  return r.status === 204 ? null : r.json();
}

async function ensureBrowserSession() {
  const agentId = app.currentAgentId;
  if (agentId) {
    try {
      const row = await apiJson('/api/v1/browser/sessions/resolve', {
        method: 'POST',
        body: JSON.stringify({ agent_id: agentId, session_id: app.currentSessionId || null }),
      });
      if (row && row.id) return row;
    } catch (_) { /* fall through */ }
  }
  let list = [];
  try {
    const d = await apiJson('/api/v1/browser/sessions');
    list = (d && d.sessions) || [];
  } catch (_) {}
  if (list.length) return list[0];
  return apiJson('/api/v1/browser/sessions', {
    method: 'POST',
    body: JSON.stringify({ title: 'My browser' }),
  });
}

function updateShareUi() {
  _updateSessionTrigger();
  if (!els || !els.share) return;
  const shared = !!(currentBs && currentBs.shared);
  els.share.classList.toggle('on', shared);
  els.share.setAttribute('aria-pressed', shared ? 'true' : 'false');
  els.share.textContent = shared ? 'Shared' : 'Private';
  els.share.title = shared
    ? 'This tab is visible to the linked agent — click to make it private'
    : 'This tab is private — click to share it with the agent';
}

// ── Session trigger label ──────────────────────────────────────────────────
// Mirrors the chat session dropdown: the trigger shows the active session's
// title (truncated). Updated whenever the active session changes.

function _updateSessionTrigger() {
  if (!els || !els.sessionsTriggerLabel) return;
  let title = 'Session';
  if (currentBs) title = currentBs.title || (currentBs.agent_id ? 'Agent tab' : 'My browser');
  if (title.length > 20) title = title.slice(0, 20) + '…';
  els.sessionsTriggerLabel.textContent = title;
}

// ── Sessions popover ───────────────────────────────────────────────────────
// Styled like the chat-panel session menu: each browser session renders as a
// `.session-row` (the shared chat-menu row look) with a leading globe/lock
// icon, the title, a private/shared meta tag and a hold-to-confirm delete.

function _escape(s) {
  return String(s == null ? '' : s).replace(/[&<>"]/g, c =>
    ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

async function refreshSessionsPopover() {
  if (!els || !els.sessionsList) return;
  let sessions = [];
  try {
    const d = await apiJson('/api/v1/browser/sessions');
    sessions = (d && d.sessions) || [];
  } catch (_) {}
  const sortByDate = (a, b) => (b.updated_at || '').localeCompare(a.updated_at || '');
  const noAgent = sessions.filter(s => !s.agent_id).sort(sortByDate);
  const withAgent = sessions.filter(s => s.agent_id).sort(sortByDate);
  const sorted = [...noAgent, ...withAgent].slice(0, 12);

  els.sessionsList.innerHTML = '';

  if (!sorted.length) {
    const empty = document.createElement('div');
    empty.className = 'browser-sessions-empty';
    empty.textContent = 'No browser sessions yet';
    els.sessionsList.appendChild(empty);
  }

  for (const s of sorted) {
    const isActive = currentBs && currentBs.id === s.id;
    const row = document.createElement('div');
    row.className = 'session-row' + (isActive ? ' selected' : '');
    row.dataset.id = s.id;
    const glyph = s.shared ? 'users' : 'lock';
    const meta = s.agent_id ? 'agent' : (s.shared ? 'shared' : 'private');
    row.innerHTML =
      '<span class="browser-session-row-icon">' + icon(glyph, { size: '14px' }) + '</span>' +
      '<span class="session-row-title">' + _escape(s.title || 'Untitled') + '</span>' +
      '<span class="browser-session-row-meta">' + meta + '</span>' +
      '<button class="session-row-delete" data-id="' + _escape(s.id) + '" data-state="trash" title="Delete session">' +
        icon('trash-2', { size: '14px' }) + '</button>';
    row.addEventListener('click', (e) => {
      if (e.target.closest('.session-row-delete')) return;
      switchSession(s.id);
    });
    const del = row.querySelector('.session-row-delete');
    if (del) del.addEventListener('click', (e) => { e.stopPropagation(); _onDeleteClick(del, s.id); });
    els.sessionsList.appendChild(row);
  }

  // Footer action: create a new browser session (mirrors the chat "+" new).
  const foot = document.createElement('div');
  foot.className = 'browser-sessions-foot';
  foot.innerHTML = '<button class="browser-sessions-new" type="button">' +
    icon('plus', { size: '14px' }) + 'New browser session</button>';
  foot.querySelector('.browser-sessions-new').addEventListener('click', openNewSession);
  els.sessionsList.appendChild(foot);
}

// Two-click hazard delete: first click arms (warning), second within 3s deletes.
function _onDeleteClick(btn, bsId) {
  if (btn.dataset.state !== 'armed') {
    btn.dataset.state = 'armed';
    btn.classList.add('warning');
    btn.title = 'Click again to delete';
    clearTimeout(btn._disarmTimer);
    btn._disarmTimer = setTimeout(() => {
      btn.dataset.state = 'trash';
      btn.classList.remove('warning');
      btn.title = 'Delete session';
    }, 3000);
    return;
  }
  clearTimeout(btn._disarmTimer);
  _deleteSession(bsId);
}

async function _deleteSession(bsId) {
  try {
    await apiJson('/api/v1/browser/sessions/' + encodeURIComponent(bsId), { method: 'DELETE' });
  } catch (_) { return; }
  delete _sessionViews[bsId];   // forget its cached view — the page is gone
  // If we deleted the active session, fall back to another (or none).
  if (currentBs && currentBs.id === bsId) {
    await switchSession(null);
  }
  refreshSessionsPopover();
}

// ── Per-session view snapshot / restore ──────────────────────────────────────
// Remember the outgoing session's view before we switch away, then paint the
// incoming session's remembered view immediately so the swap feels instant.

function _snapshotCurrentView() {
  if (!currentBs || !currentBs.id) return;
  _sessionViews[currentBs.id] = {
    frame: _lastFrameB64,
    url: _currentUrl,
    hasRealPage: _hasRealPage,
    deviceOn: deviceOn,
  };
}

// Paint the remembered view for `bsId` (using the session row's stored url as a
// fallback). doConnect() then reconnects the live stream on top of it; the empty
// "type a URL to start" hint only shows when there's genuinely nothing yet.
function _restoreView(bsId, fallbackUrl) {
  const v = (bsId && _sessionViews[bsId]) || null;
  _lastFrameB64 = (v && v.frame) || null;
  _hasRealPage = !!(v && v.hasRealPage);
  deviceOn = !!(v && v.deviceOn);   // refreshBackend() confirms this once connected
  if (_lastFrameB64) { setViewMode('mirror'); _repaintLast(); }
  else { _clearMirror(); setViewMode('proxy'); }
  // Reset the unified pill, then rest it on this session's url (if any).
  _editing = false;
  _currentUrl = '';
  if (els && els.promptInput) { els.promptInput.value = ''; els.promptInput.style.height = 'auto'; }
  if (els && els.promptRow) els.promptRow.classList.remove('has-text', 'url-mode');
  if (els && els.send) _setSendMode('message');
  const url = (v && v.url) || fallbackUrl || '';
  if (url && url !== 'about:blank') _setUrlDisplay(url);
  // Spinner if this session has a page to come back to, the hint only if not.
  _showConnectingStage();
  updateDeviceUi();
}

// Make `row` the active session: snapshot the one we're leaving, pause its live
// feed (the page persists server-side), restore the new one's view, reconnect.
function _activateSession(row) {
  _snapshotCurrentView();
  _teardownConnections();
  try { overlay.clearOverlay(); } catch (_) {}
  try { ticker.clearTicker(); } catch (_) {}
  currentBs = row && row.id ? row : null;
  connectedBs = null;
  if (currentBs) {
    _restoreView(currentBs.id, currentBs.url);
    doConnect();
  } else {
    _restoreView(null, '');
    setStatus('down', 'no session');
  }
}

async function switchSession(bsId) {
  _setPopover(false);
  if (bsId && currentBs && currentBs.id === bsId) return;
  let row = null;
  if (bsId) {
    row = await ensureSessionById(bsId) || { id: bsId };
  } else {
    try {
      const d = await apiJson('/api/v1/browser/sessions');
      const list = (d && d.sessions) || [];
      row = list.find(s => !s.agent_id) || null;
    } catch (_) { row = null; }
  }
  _activateSession(row);
}
window.__switchWebSession = switchSession;
window.__closeWebSession = _deleteSession;

async function ensureSessionById(bsId) {
  try {
    const d = await apiJson('/api/v1/browser/sessions');
    const list = (d && d.sessions) || [];
    return list.find(s => s.id === bsId) || null;
  } catch (_) { return null; }
}

async function openNewSession() {
  try {
    const row = await apiJson('/api/v1/browser/sessions', {
      method: 'POST',
      body: JSON.stringify({ title: 'New tab' }),
    });
    if (row && row.id) {
      _setPopover(false);
      _activateSession(row);   // a brand-new session has no cached view → shows the empty hint
    }
  } catch (_) {}
}

function _setPopover(open) {
  if (!els || !els.sessionsPopover) return;
  els.sessionsPopover.style.display = open ? 'block' : 'none';
  if (els.sessionsToggle) els.sessionsToggle.setAttribute('aria-expanded', open ? 'true' : 'false');
}

function toggleSessionsPopover(e) {
  e.stopPropagation();
  if (!els || !els.sessionsPopover) return;
  const showing = els.sessionsPopover.style.display !== 'none';
  if (showing) {
    _setPopover(false);
  } else {
    refreshSessionsPopover();
    _setPopover(true);
  }
}

document.addEventListener('click', (e) => {
  if (els && els.sessionsPopover && els.sessionsPopover.style.display !== 'none') {
    if (!els.sessionsPopover.contains(e.target) &&
        els.sessionsToggle && !els.sessionsToggle.contains(e.target)) {
      _setPopover(false);
    }
  }
});

async function toggleShare() {
  if (!currentBs || !currentBs.id) return;
  const next = !currentBs.shared;
  const body = { shared: next, agent_id: next ? (app.currentAgentId || null) : null };
  try {
    const updated = await apiJson(
      '/api/v1/browser/sessions/' + encodeURIComponent(currentBs.id),
      { method: 'PATCH', body: JSON.stringify(body) },
    );
    currentBs = { ...currentBs, ...(updated || {}) };
  } catch (_) {
    return;
  }
  updateShareUi();
}

// ── Device backend switcher ─────────────────────────────────────────────────
// Flip this session between the in-app headless browser (proxy iframe view) and
// the real browser on the user's device (live screencast mirror view). The agent
// can do the same via the browser_backend tool; this is the human's control.

function updateDeviceUi() {
  if (!els || !els.device) return;
  const onAnything = backendMode !== 'headless';
  els.device.classList.toggle('on', onAnything);
  els.device.setAttribute('aria-pressed', onAnything ? 'true' : 'false');
  let label, title;
  if (backendMode === 'local') {
    label = 'On device';
    title = 'Driving the real browser on this machine — click to cycle to “My browser”, then back to in-app';
  } else if (backendMode === 'connector') {
    label = connectorConnected ? 'My browser' : 'My browser (offline)';
    title = connectorConnected
      ? 'Driving your own browser through the extension — click to switch back to the in-app browser'
      : 'Your browser extension isn’t connected — click to switch back to the in-app browser';
  } else {
    label = 'In-app';
    title = 'Switch how this session is driven: in-app → on device → my browser (extension)';
  }
  if (els.deviceLabel) els.deviceLabel.textContent = label;
  els.device.title = title;
}

// Cycle this session's backend: in-app (headless) → on device (local) → my
// browser (connector — the user's own browser via the extension) → in-app. The
// backend only commits on a SUCCESSFUL switch, so a failed step (e.g. "My
// browser" with no extension connected) leaves the session where it was and just
// surfaces why.
const _BACKEND_CYCLE = { headless: 'local', local: 'connector', connector: 'headless' };
const _BACKEND_WORD = { headless: 'in-app', local: 'device', connector: 'my browser' };

async function cycleBackend() {
  if (!currentBs || !currentBs.id) return;
  const prevMode = backendMode;
  const next = _BACKEND_CYCLE[backendMode] || 'headless';
  if (els && els.device) els.device.classList.add('busy');
  setStatus('idle', 'switching to ' + _BACKEND_WORD[next] + '…');
  let res = null;
  try {
    res = await apiJson('/api/v1/browser/sessions/' + encodeURIComponent(currentBs.id) + '/backend', {
      method: 'POST',
      body: JSON.stringify({ mode: next, profile: 'default' }),
    });
  } catch (_) {
    res = { success: false, message: 'Could not switch backend.' };
  }
  if (els && els.device) els.device.classList.remove('busy');

  if (res && res.success) {
    const leavingConnector = (prevMode === 'connector');
    backendMode = res.backend || next;
    deviceOn = (backendMode === 'local');
    if (backendMode === 'connector') connectorConnected = true;
    updateDeviceUi();
    if (backendMode === 'connector') {
      // The page lives in the user's OWN browser — there's no server-side page to
      // mirror here. Drop the live pipes and show a notice instead.
      _teardownConnections();
      _showConnectorStage();
      setStatus('live', 'your browser');
    } else if (leavingConnector) {
      // Coming back FROM connector: the live pipes were torn down on the way in,
      // so fully re-establish them (nav WS + screencast + agent feed).
      doConnect();
    } else {
      // local <-> headless: the nav WS stayed open; just refresh the live mirror
      // to stream the (possibly new) page and update the URL bar.
      setViewMode('mirror');
      connectScreencast();
      const u = res.url || _currentUrl || '';
      if (u && u !== 'about:blank') _setUrlDisplay(u);
      _markRealPage(u);
      setStatus('live', deviceOn ? 'device' : 'live');
    }
  } else {
    // Switch failed — keep the current backend, surface why (e.g. extension not
    // connected when trying to use "My browser").
    setStatus('down', (res && res.message) ? res.message : 'switch failed');
    updateDeviceUi();
  }
}

// Read the session's current backend on (re)connect so the switcher label matches
// reality, and learn whether the user's browser extension is live. Returns the
// backend mode so doConnect can choose the right stage (a connector session has
// no server-side page to mirror).
async function refreshBackend() {
  if (!currentBs || !currentBs.id) return backendMode;
  let st = null;
  try {
    st = await apiJson('/api/v1/browser/sessions/' + encodeURIComponent(currentBs.id) + '/backend');
  } catch (_) { return backendMode; }
  backendMode = (st && st.backend) || 'headless';
  deviceOn = (backendMode === 'local');
  connectorConnected = !!(st && st.connector && st.connector.connected);
  updateDeviceUi();
  return backendMode;
}

// ── Live pixel mirror (CDP screencast) ──────────────────────────────────────

function setViewMode(mode) {
  viewMode = mode;
  if (!els) return;
  if (mode === 'mirror') {
    els.frame.classList.remove('has-frame');
    els.mirror.classList.add('has-frame');
  } else {
    els.mirror.classList.remove('has-frame');
    // The iframe's own .has-frame (set by _showFrame) governs its visibility.
  }
  // The overlay only makes sense over the live mirror — disable it in proxy
  // mode (the iframe scrolls independently, so page-space boxes wouldn't map).
  try { overlay.setEnabled(mode === 'mirror'); } catch (_) {}
}

// The "type a URL to start" hint stays until a real page loads; after that the
// live mirror (or iframe) owns the stage. Tracked so an about:blank session
// keeps the hint visible even though the mirror is already streaming.
let _hasRealPage = false;

// ── Stage placeholder state ──────────────────────────────────────────────────
// Behind the page the stage shows exactly ONE of three things: a spinner while a
// session that has a saved page reconnects/restores, the "type a URL to start"
// hint only when there's genuinely nothing saved, or neither once a real page is
// painting. _setStage is the single switch; everything else routes through it.
function _setStage(state) {  // 'loading' | 'empty' | 'page'
  if (!els) return;
  if (els.loading) els.loading.style.display = state === 'loading' ? '' : 'none';
  if (els.empty) els.empty.style.display = state === 'empty' ? '' : 'none';
  // The connector notice is an independent overlay; any normal stage change clears it.
  if (els.connectorNotice) els.connectorNotice.style.display = 'none';
  if (els.extensionPanel) els.extensionPanel.style.display = 'none';
}

// Connector backend: the page is in the user's OWN browser, so there is nothing
// to mirror here. Hide the frame + mirror and show the notice instead.
function _showConnectorStage() {
  if (!els) return;
  if (els.frame) els.frame.classList.remove('has-frame');
  if (els.mirror) els.mirror.classList.remove('has-frame');
  _hasRealPage = false;
  if (els.loading) els.loading.style.display = 'none';
  if (els.empty) els.empty.style.display = 'none';
  if (els.connectorNotice) els.connectorNotice.style.display = '';
}

function _showExtensionStage() {
  if (!els) return;
  if (els.frame) els.frame.classList.remove('has-frame');
  if (els.mirror) els.mirror.classList.remove('has-frame');
  if (els.loading) els.loading.style.display = 'none';
  if (els.empty) els.empty.style.display = 'none';
  if (els.connectorNotice) els.connectorNotice.style.display = 'none';
  if (els.extensionPanel) els.extensionPanel.style.display = '';
  try { overlay.setEnabled(false); } catch (_) {}
}

// ── "Pair extension" setup card ──────────────────────────────────────────────
// One-time setup for the "My browser" (connector) backend. Outside 'open' access
// mode the browser extension MUST present a token — the connector WebSocket has
// no tokenless shortcut — and a non-technical user has no other way to obtain
// one. This card mints a long-lived token from the user's signed-in session
// (GET /api/v1/connector/pair) and shows it, with the server URL, for one-click
// copy into the extension's Settings. Built lazily and appended to <body> as a
// fixed overlay so it covers the page regardless of the browser stage layout.
let _pairModal = null;

function _closePairModal() {
  if (_pairModal) { _pairModal.remove(); _pairModal = null; }
}

// A Copy button that flips to "Copied" briefly. getValue is read at click time so
// the field's current value is copied even if it was filled in asynchronously.
function _pairCopyBtn(getValue) {
  const b = document.createElement('button');
  b.className = 'pair-copy';
  b.type = 'button';
  b.textContent = 'Copy';
  b.addEventListener('click', () => {
    const v = getValue();
    if (!v) return;
    copyText(v).then(() => {
      b.textContent = 'Copied';
      b.classList.add('done');
      setTimeout(() => { b.textContent = 'Copy'; b.classList.remove('done'); }, 1400);
    }).catch(() => {});
  });
  return b;
}

// A labelled, read-only field with a Copy button (server URL / token).
function _pairField(labelText, value, mono) {
  const wrap = document.createElement('div');
  wrap.className = 'pair-field';
  const lab = document.createElement('label');
  lab.className = 'pair-label';
  lab.textContent = labelText;
  const row = document.createElement('div');
  row.className = 'pair-input-row';
  const inp = document.createElement('input');
  inp.className = 'pair-input' + (mono ? ' mono' : '');
  inp.type = 'text';
  inp.readOnly = true;
  inp.value = value;
  inp.addEventListener('focus', () => { try { inp.select(); } catch (_) {} });
  row.appendChild(inp);
  row.appendChild(_pairCopyBtn(() => inp.value));
  wrap.appendChild(lab);
  wrap.appendChild(row);
  return wrap;
}

async function openPairModal() {
  _closePairModal();

  const overlayEl = document.createElement('div');
  overlayEl.className = 'pair-modal-overlay';
  overlayEl.addEventListener('click', (e) => { if (e.target === overlayEl) _closePairModal(); });

  const card = document.createElement('div');
  card.className = 'pair-modal';
  card.setAttribute('role', 'dialog');
  card.setAttribute('aria-modal', 'true');

  const head = document.createElement('div');
  head.className = 'pair-head';
  const h = document.createElement('div');
  h.className = 'pair-title';
  h.textContent = 'Connect your browser';
  const x = document.createElement('button');
  x.className = 'pair-close';
  x.type = 'button';
  x.setAttribute('aria-label', 'Close');
  x.innerHTML = icon('x');
  x.addEventListener('click', _closePairModal);
  head.appendChild(h);
  head.appendChild(x);

  const intro = document.createElement('p');
  intro.className = 'pair-intro';
  intro.textContent = 'Install the WebAgent browser extension, then paste these two values into its Settings so the agent can drive this browser.';

  const body = document.createElement('div');
  body.className = 'pair-body';
  body.textContent = 'Generating a setup token…';

  card.appendChild(head);
  card.appendChild(intro);
  card.appendChild(body);
  overlayEl.appendChild(card);
  document.body.appendChild(overlayEl);
  _pairModal = overlayEl;

  const onKey = (e) => {
    if (e.key === 'Escape') { _closePairModal(); document.removeEventListener('keydown', onKey); }
  };
  document.addEventListener('keydown', onKey);

  // Mint the token from the user's signed-in session.
  let data = null, err = null;
  try {
    data = await apiJson('/api/v1/connector/pair');
  } catch (e) {
    err = e;
  }
  if (_pairModal !== overlayEl) return;  // closed (or reopened) while loading

  if (!data || !data.token) {
    body.textContent = '';
    const msg = document.createElement('p');
    msg.className = 'pair-error';
    msg.textContent = (err && String(err.message || err).indexOf('401') >= 0)
      ? 'Sign in to the app first, then reopen this to pair your browser.'
      : 'Could not generate a setup token. Make sure you are signed in and the server is reachable, then try again.';
    body.appendChild(msg);
    return;
  }

  body.textContent = '';
  body.appendChild(_pairField('Server URL', window.location.origin, false));
  body.appendChild(_pairField('Token', data.token, true));

  const steps = document.createElement('ol');
  steps.className = 'pair-steps';
  [
    'Click the WebAgent extension icon, then “Settings…”.',
    'Paste the Server URL and Token above, then Save.',
    'The extension badge turns green (ON) once it connects.',
    'Come back here and switch the control to “My browser”.',
  ].forEach((t) => { const li = document.createElement('li'); li.textContent = t; steps.appendChild(li); });
  body.appendChild(steps);
}

// Does this session have somewhere to return to? True once we know a non-blank
// URL for it (a live page already, or the session row's last-saved url), which
// means a reconnect should SPIN rather than show the empty hint.
function _expectsPage() {
  if (_hasRealPage) return true;
  if (_currentUrl && _currentUrl !== 'about:blank') return true;
  return !!(currentBs && currentBs.url && currentBs.url !== 'about:blank');
}

// Choose loading-vs-empty for a session that isn't painting a real page yet.
function _showConnectingStage() {
  if (_hasRealPage) { _setStage('page'); return; }
  _setStage(_expectsPage() ? 'loading' : 'empty');
}

function _markRealPage(url) {
  if (!url || url === 'about:blank') return;
  _hasRealPage = true;
  _setStage('page');
}

function _sizeGenui() {
  if (!els || !els.mirror) return;
  els.mirror.width = mirrorSpace.width;
  els.mirror.height = mirrorSpace.height;
}

function drawFrame(b64) {
  if (!els || !els.mirror || !b64) return;
  _lastFrameB64 = b64;   // remember it so we can restore the page on tab return
  if (!mirrorImg) mirrorImg = new Image();
  const cv = els.mirror;
  mirrorImg.onload = function () {
    try {
      const ctx = cv.getContext('2d');
      ctx.drawImage(mirrorImg, 0, 0, cv.width, cv.height);
    } catch (_) {}
  };
  mirrorImg.src = 'data:image/jpeg;base64,' + b64;
}

// Repaint the last frame we saw (used to restore the page instantly on tab
// return, and to cover the brief blank after a genui resize on reconnect).
function _repaintLast() {
  if (_lastFrameB64) drawFrame(_lastFrameB64);
}

// Wipe the mirror bitmap — only on a real session change, so a stale page never
// flashes under the next session before its first frame arrives.
function _clearMirror() {
  if (!els || !els.mirror) return;
  try { els.mirror.getContext('2d').clearRect(0, 0, els.mirror.width, els.mirror.height); } catch (_) {}
}

async function connectScreencast() {
  disconnectScreencast();
  if (!currentBs || !currentBs.id) return;
  const ticket = await connectionTicket('/api/v1/browser/screencast');
  if (!ticket || !currentBs || !currentBs.id) return;
  const qs = '?ticket=' + encodeURIComponent(ticket) +
             '&bs_id=' + encodeURIComponent(currentBs.id);
  try {
    scWs = new WebSocket(browserScreencastWsUrl() + qs);
  } catch (_) {
    setStatus('down', 'mirror failed');
    return;
  }
  scWs.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.type === 'ready') {
      if (m.space && m.space.width && m.space.height) mirrorSpace = m.space;
      _sizeGenui();
      _repaintLast();   // _sizeGenui clears the bitmap; cover it with the saved frame until the live stream resumes
      try { overlay.setSpace(mirrorSpace); } catch (_) {}
      if (m.url && m.url !== 'about:blank') _setUrlDisplay(m.url);
      _markRealPage(m.url);
      // Real page → _markRealPage already cleared the placeholder. If we got here
      // and still have no page, the session is genuinely empty — show the hint.
      if (!_hasRealPage) _setStage('empty');
      setStatus('live', deviceOn ? 'device' : 'live');
    } else if (m.type === 'frame') {
      drawFrame(m.data);
    } else if (m.type === 'error') {
      // The live mirror failed — fall back to the HTML proxy iframe so the page
      // is still usable (just without the interactive overlay).
      setStatus('down', m.message || 'mirror error');
      setViewMode('proxy');
      const u = _currentUrl || '';
      if (u && u !== 'about:blank') { _loadFrame(u); _showFrame(); }
    }
  };
  scWs.onclose = () => {
    if (active && deviceOn) {
      scReconnectTimer = setTimeout(() => { if (active && deviceOn) connectScreencast(); }, 1500);
    }
  };
  scWs.onerror = () => {};
}

function disconnectScreencast() {
  clearTimeout(scReconnectTimer);
  if (scWs) {
    try { scWs.onclose = null; scWs.onerror = null; scWs.close(); } catch (_) {}
    scWs = null;
  }
}

function _scSend(obj) {
  const target = surfaceMode === 'desktop' ? desktopWs : scWs;
  if (target && target.readyState === WebSocket.OPEN) {
    try { target.send(JSON.stringify({ type: 'input', ...obj })); } catch (_) {}
  }
}

// Map a pointer event on the (object-fit:contain) genui to page CSS pixels.
function _mapPt(e) {
  const cv = els.mirror;
  const rect = cv.getBoundingClientRect();
  const cw = cv.width || mirrorSpace.width;
  const ch = cv.height || mirrorSpace.height;
  const scale = Math.min(rect.width / cw, rect.height / ch) || 1;
  const dispW = cw * scale, dispH = ch * scale;
  const offX = rect.left + (rect.width - dispW) / 2;
  const offY = rect.top + (rect.height - dispH) / 2;
  const x = Math.max(0, Math.min(cw, (e.clientX - offX) / scale));
  const y = Math.max(0, Math.min(ch, (e.clientY - offY) / scale));
  return { x, y };
}

const _BTN = { 0: 'left', 1: 'middle', 2: 'right' };
const _KEYMAP = {
  Enter: 'Enter', Backspace: 'Backspace', Tab: 'Tab', Delete: 'Delete',
  ArrowUp: 'ArrowUp', ArrowDown: 'ArrowDown', ArrowLeft: 'ArrowLeft', ArrowRight: 'ArrowRight',
  Home: 'Home', End: 'End', PageUp: 'PageUp', PageDown: 'PageDown', Escape: 'Escape',
};

// Wired ONCE (in mount). Each handler no-ops unless the mirror view is active.
function wireMirrorInput() {
  if (!els || !els.mirror) return;
  const cv = els.mirror;

  cv.addEventListener('mousemove', (e) => {
    if (viewMode !== 'mirror' || _pointMode) return;
    const now = Date.now();
    if (now - _lastMoveSent < 33) return;  // ~30fps
    _lastMoveSent = now;
    const p = _mapPt(e);
    _scSend({ kind: 'mousemove', x: p.x, y: p.y });
  });
  cv.addEventListener('mousedown', (e) => {
    if (viewMode !== 'mirror') return;
    e.preventDefault();
    cv.focus();
    const p = _mapPt(e);
    // Point-and-ask: a left click in "point" mode marks a spot to ask the agent
    // about instead of clicking the page through to the real browser.
    if (_pointMode && (e.button === 0 || e.button == null)) {
      _capturePoint(p);
      return;
    }
    _scSend({ kind: 'mousedown', x: p.x, y: p.y, button: _BTN[e.button] || 'left' });
    // Paint the user's own click + flag them as the one driving right now.
    try { overlay.showClick({ point: p }, 'user'); } catch (_) {}
    markDriver('user');
  });
  cv.addEventListener('mouseup', (e) => {
    if (viewMode !== 'mirror' || _pointMode) return;
    e.preventDefault();
    const p = _mapPt(e);
    _scSend({ kind: 'mouseup', x: p.x, y: p.y, button: _BTN[e.button] || 'left' });
  });
  cv.addEventListener('contextmenu', (e) => { if (viewMode === 'mirror') e.preventDefault(); });
  cv.addEventListener('wheel', (e) => {
    if (viewMode !== 'mirror' || _pointMode) return;
    e.preventDefault();
    const p = _mapPt(e);
    _scSend({ kind: 'wheel', x: p.x, y: p.y, deltaX: e.deltaX, deltaY: e.deltaY });
  }, { passive: false });
  cv.addEventListener('keydown', (e) => {
    if (viewMode !== 'mirror' || _pointMode) return;
    if (surfaceMode === 'desktop' && (e.ctrlKey || e.metaKey || e.altKey)) {
      e.preventDefault();
      const keys = [];
      if (e.ctrlKey) keys.push('CTRL');
      if (e.altKey) keys.push('ALT');
      if (e.shiftKey) keys.push('SHIFT');
      if (e.metaKey) keys.push('WIN');
      keys.push(_KEYMAP[e.key] || e.key);
      _scSend({ kind: 'key', keys });
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;  // keep app shortcuts in Browser mode
    if (_KEYMAP[e.key]) {
      e.preventDefault();
      _scSend({ kind: 'key', key: _KEYMAP[e.key] });
    } else if (e.key && e.key.length === 1) {
      e.preventDefault();
      _scSend({ kind: 'text', text: e.key });
    }
  });
}

// ── Server desktop surface ───────────────────────────────────────────────────

function _updateSurfaceUi() {
  if (!els) return;
  if (els.page) els.page.classList.toggle('desktop-mode', surfaceMode === 'desktop');
  if (els.page) els.page.classList.toggle('extension-mode', surfaceMode === 'extension');
  if (els.page) els.page.classList.toggle('idle-mode', surfaceMode === 'idle');
  els.surfaceButtons.forEach((button) => {
    const selected = button.dataset.surface === surfaceMode;
    button.classList.toggle('on', selected);
    button.setAttribute('aria-pressed', selected ? 'true' : 'false');
  });
  if (els.promptInput) {
    els.promptInput.placeholder = surfaceMode === 'desktop'
      ? 'Message the agent about this desktop…'
      : 'Enter a URL, or message the agent…';
  }
  if (els.empty && surfaceMode === 'idle') {
    els.empty.textContent = 'Choose Browser for the shared Playwright session, Extension for your Chromium browser, or Desktop for this server.';
  } else if (els.empty) {
    els.empty.innerHTML = 'This is a shared browser. Type a URL in the bar below to start &mdash; the agent can see and act on this same page, and you can take over any time.';
  }
}

function _showIdleStage() {
  if (!els) return;
  if (els.frame) els.frame.classList.remove('has-frame');
  if (els.mirror) els.mirror.classList.remove('has-frame');
  _hasRealPage = false;
  _clearMirror();
  try { overlay.setEnabled(false); } catch (_) {}
  _setStage('empty');
  setStatus('idle', 'choose Browser, Extension, or Desktop');
}

async function _setSessionBackend(mode) {
  if (!currentBs || !currentBs.id) return false;
  try {
    const res = await apiJson('/api/v1/browser/sessions/' + encodeURIComponent(currentBs.id) + '/backend', {
      method: 'POST', body: JSON.stringify({ mode, profile: 'default' }),
    });
    if (!res || !res.success) return false;
    backendMode = res.backend || mode;
    deviceOn = backendMode === 'local';
    updateDeviceUi();
    return true;
  } catch (_) {
    return false;
  }
}

async function switchSurface(next) {
  if (next !== 'browser' && next !== 'extension' && next !== 'desktop') return;
  if (surfaceMode === next) return;
  const previous = surfaceMode;
  if (previous === 'browser' && backendMode !== 'connector') lastBrowserBackend = backendMode;
  _teardownConnections();
  surfaceMode = next;
  _pendingPoint = null;
  _pointMode = false;
  _lastFrameB64 = null;
  _clearMirror();
  _updatePointUi();
  _updateSurfaceUi();
  if (!active) return;
  if (surfaceMode === 'desktop') {
    connectDesktop();
  } else if (surfaceMode === 'extension') {
    _showExtensionStage();
    setStatus('idle', 'checking extension…');
    const info = await refreshExtensionStatus();
    if (info && info.connected) {
      try { currentBs = currentBs || await ensureBrowserSession(); } catch (_) { /* status still works */ }
      if (currentBs && await _setSessionBackend('connector')) {
        connectorConnected = true;
        setStatus('live', 'extension connected');
      }
    }
    _startExtensionStatusPolling();
  } else {
    clearTimeout(extensionStatusTimer);
    if (backendMode === 'connector' && currentBs) await _setSessionBackend(lastBrowserBackend);
    _hasRealPage = !!(currentBs && currentBs.url && currentBs.url !== 'about:blank');
    doConnect();
  }
}

function _extensionDetail(name, value) {
  const item = els && els.extensionDetails.find((node) => node.dataset.extensionDetail === name);
  if (item) item.textContent = value || '—';
}

async function refreshExtensionStatus() {
  if (!els) return null;
  let info = null;
  try {
    const status = await apiJson('/api/v1/control/status');
    info = status && status.extension;
  } catch (_) {
    info = { connected: false };
  }
  const connected = !!(info && info.connected);
  connectorConnected = connected;
  if (els.extensionState) els.extensionState.classList.toggle('connected', connected);
  if (els.extensionStateLabel) els.extensionStateLabel.textContent = connected ? 'Connected' : 'Offline';
  _extensionDetail('browser', connected ? [info.browser, info.browser_version].filter(Boolean).join(' ') : 'Not connected');
  _extensionDetail('platform', connected ? info.platform : '—');
  _extensionDetail('version', connected ? ('v' + (info.version || 'unknown')) : '—');
  _extensionDetail('last-seen', connected && info.last_seen ? new Date(info.last_seen).toLocaleString() : '—');
  const settings = (info && info.settings) || {};
  els.extensionSettings.forEach((input) => {
    input.disabled = !connected;
    if (Object.prototype.hasOwnProperty.call(settings, input.dataset.extensionSetting)) {
      input.checked = !!settings[input.dataset.extensionSetting];
    }
  });
  if (surfaceMode === 'extension') setStatus(connected ? 'live' : 'down', connected ? 'extension connected' : 'extension offline');
  return info;
}

function _startExtensionStatusPolling() {
  clearTimeout(extensionStatusTimer);
  if (!active || surfaceMode !== 'extension') return;
  extensionStatusTimer = setTimeout(async () => {
    await refreshExtensionStatus();
    _startExtensionStatusPolling();
  }, 3000);
}

async function updateExtensionSetting(input) {
  const setting = input.dataset.extensionSetting;
  input.disabled = true;
  try {
    await apiJson('/api/v1/control/extension/settings', {
      method: 'PATCH', body: JSON.stringify({ [setting]: input.checked }),
    });
    setTimeout(refreshExtensionStatus, 250);
  } catch (_) {
    input.checked = !input.checked;
    setStatus('down', 'could not update extension');
  } finally {
    input.disabled = false;
  }
}

async function stopControlSurface(kind, button, event) {
  if (event) event.stopPropagation();
  if (!['browser', 'extension', 'desktop'].includes(kind)) return;
  button.disabled = true;
  // Disarm reconnect loops before asking the server to release the resource.
  // Otherwise a socket close caused by the kill could race us and relaunch it.
  if (surfaceMode === kind) {
    _teardownConnections();
    surfaceMode = 'idle';
    _updateSurfaceUi();
    _showIdleStage();
  }
  try {
    await apiJson('/api/v1/control/' + kind + '/kill', { method: 'POST', body: '{}' });
  } catch (_) {
    setStatus('down', 'could not stop ' + kind);
  } finally {
    button.disabled = false;
  }
}

async function connectDesktop() {
  disconnectDesktop();
  setViewMode('mirror');
  _setStage('loading');
  setStatus('idle', 'connecting to desktop…');
  const ticket = await connectionTicket('/api/v1/control/desktop');
  if (!ticket || !active || surfaceMode !== 'desktop') return;
  const qs = '?ticket=' + encodeURIComponent(ticket);
  try {
    desktopWs = new WebSocket(desktopControlWsUrl() + qs);
  } catch (_) {
    setStatus('down', 'desktop connection failed');
    return;
  }
  desktopWs.onmessage = (ev) => {
    let message; try { message = JSON.parse(ev.data); } catch (_) { return; }
    if (message.type === 'ready') {
      if (message.space && message.space.width && message.space.height) mirrorSpace = message.space;
      _sizeGenui();
      _setStage('page');
      setStatus('live', 'server desktop');
    } else if (message.type === 'frame') {
      drawFrame(message.data);
      _setStage('page');
    } else if (message.type === 'error' || message.type === 'input_error') {
      setStatus('down', message.message || 'desktop error');
    }
  };
  desktopWs.onclose = (event) => {
    desktopWs = null;
    setStatus('down', event && event.code === 4403 ? 'desktop requires admin' : 'desktop disconnected');
    if (active && surfaceMode === 'desktop') {
      desktopReconnectTimer = setTimeout(() => {
        if (active && surfaceMode === 'desktop') connectDesktop();
      }, 1500);
    }
  };
  desktopWs.onerror = () => {};
}

function disconnectDesktop() {
  clearTimeout(desktopReconnectTimer);
  if (desktopWs) {
    try { desktopWs.onclose = null; desktopWs.onerror = null; desktopWs.close(); } catch (_) {}
    desktopWs = null;
  }
}

// ── WebSocket connection ───────────────────────────────────────────────────

async function doConnect() {
  if (!active || surfaceMode !== 'browser') return;
  _updateSessionTrigger();
  if (!currentBs || !currentBs.id) {
    setStatus('down', 'no session');
    return;
  }
  connectedBs = currentBs.id;
  // Show a spinner (not the empty hint) while we reconnect a session that has a
  // page to restore; the `ready` echo below resolves it to the page or the hint.
  _showConnectingStage();
  updateShareUi();
  // Learn this session's backend FIRST. A "connector" session is driven by the
  // user's OWN browser via the extension — there is no server-side page to mirror,
  // so we drop the live pipes and show a notice instead of opening the command WS
  // + screencast (which would fail server-side and reconnect-loop).
  const _mode = await refreshBackend();
  if (connectedBs !== (currentBs && currentBs.id)) return;  // session changed mid-await
  if (_mode === 'connector') {
    _teardownConnections();
    _showConnectorStage();
    setStatus('live', connectorConnected ? 'your browser' : 'extension offline');
    return;
  }
  const ticket = await connectionTicket('/api/v1/browser/ws');
  if (!ticket || connectedBs !== (currentBs && currentBs.id)) return;
  const qs = '?ticket=' + encodeURIComponent(ticket) +
             '&bs_id=' + encodeURIComponent(currentBs.id);
  try {
    ws = new WebSocket(browserWsUrl() + qs);
  } catch (_) {
    setStatus('down', 'connection failed');
    return;
  }
  ws.onopen = () => setStatus('idle', 'connecting…');
  ws.onmessage = (ev) => {
    let m; try { m = JSON.parse(ev.data); } catch (_) { return; }
    if (m.type === 'nav') {
      // The server says the page URL changed — update the address bar and (in
      // proxy mode) load the new page into the iframe.  Skip if this is an echo
      // of a navigation we already initiated (link click or URL-bar Go). In
      // mirror mode the genui already shows the change live, so only the
      // address bar needs updating.
      if (m.url && m.url !== 'about:blank') {
        _setUrlDisplay(m.url);
        _markRealPage(m.url);
        if (viewMode === 'proxy' && m.url !== _lastNavUrl) {
          _loadFrame(m.url);
          _showFrame();
        }
      }
      setStatus('live', deviceOn ? 'device' : 'live');
    } else if (m.type === 'ready') {
      if (m.url && m.url !== 'about:blank') {
        _setUrlDisplay(m.url);
        _markRealPage(m.url);
        if (viewMode === 'proxy') _loadFrame(m.url);
      } else if (!_hasRealPage) {
        _setStage('empty');  // nothing saved for this session — show the hint
      }
      setStatus('idle', 'ready');
    } else if (m.type === 'error') {
      setStatus('down', m.message || 'error');
    }
  };
  ws.onclose = () => {
    setStatus('down', 'disconnected');
    if (active) reconnectTimer = setTimeout(() => { if (active) connect(); }, 1500);
  };
  ws.onerror = () => setStatus('down', 'error');

  // ── Always-on live mirror + agent action feed ──
  // The live pixel mirror is now the primary shared view (it's the only one that
  // shows the agent acting in real time), so connect it for every session — not
  // just the on-device backend. The live-events subscriber paints the agent's
  // clicks/typing/telegraphs onto the same surface the user clicks on.
  setViewMode('mirror');
  connectScreencast();
  startLiveEvents(() => (currentBs && currentBs.id) || null, _liveHandlers);
}

// ── Agent action feed → overlay + ticker ────────────────────────────────────
// Routes each `browser_live` event from live-events.js to the visual layer:
// telegraph halos before the agent acts, click ripples + a ghost cursor where
// it acts, typing highlights, and a plain-English line in the action ticker.
const _liveHandlers = {
  onTelegraph(box) {
    try { overlay.showTelegraph(box); } catch (_) {}
    if (box) { try { overlay.setAgentCursor({ x: box.x + (box.width || 0) / 2, y: box.y + (box.height || 0) / 2 }); } catch (_) {} }
    markDriver('agent');
  },
  onClick({ box, point, label }) {
    const pt = point || (box ? { x: box.x + (box.width || 0) / 2, y: box.y + (box.height || 0) / 2 } : null);
    try { overlay.showClick({ box, point }, 'agent'); } catch (_) {}
    if (pt) { try { overlay.setAgentCursor(pt); } catch (_) {} }
    if (label) { try { ticker.pushTicker(label, 'agent'); } catch (_) {} }
    markDriver('agent');
  },
  onType({ box, text, label }) {
    try { overlay.showType(box, text, 'agent'); } catch (_) {}
    if (label) { try { ticker.pushTicker(label, 'agent'); } catch (_) {} }
    markDriver('agent');
  },
  onNav({ label }) {
    if (label) { try { ticker.pushTicker(label, 'agent'); } catch (_) {} }
    markDriver('agent');
  },
  onBackend({ label }) {
    if (label) { try { ticker.pushTicker(label, 'agent'); } catch (_) {} }
    markDriver('agent');
  },
  onScroll() { markDriver('agent'); },
  onBusy() { markDriver('agent'); },
};

async function connect() {
  // A tab restore or background route must never implicitly launch Playwright.
  if (!active || surfaceMode !== 'browser') return;
  if (!app.currentUserId) {
    setStatus('down', 'sign in to browse');
    return;
  }
  clearTimeout(reconnectTimer);
  setStatus('idle', 'connecting…');
  // Spin while we resolve the session — the empty hint only shows once we know
  // (via the `ready` echo) that the session truly has nothing saved.
  _setStage('loading');
  try {
    currentBs = await ensureBrowserSession();
  } catch (_) {
    setStatus('down', 'connection failed');
    _setStage('empty');
    return;
  }
  if (!currentBs || !currentBs.id) {
    setStatus('down', 'connection failed');
    _setStage('empty');
    return;
  }
  doConnect();
}

// ── Bringing the live connection down ────────────────────────────────────────
// _teardownConnections() just closes the pipes (both WebSockets + the agent
// feed). It NEVER wipes on-screen state — the page persists on the server, so
// every consumer either restores a remembered view (tab return, session switch)
// or paints a fresh one. suspend() is the tab-navigation-away wrapper.

function _teardownConnections() {
  clearTimeout(reconnectTimer);
  clearTimeout(_driverTimer);
  clearTimeout(extensionStatusTimer);
  if (ws) {
    try { ws.onclose = null; ws.onerror = null; ws.close(); } catch (_) {}
    ws = null;
  }
  disconnectScreencast();
  disconnectDesktop();
  try { stopLiveEvents(); } catch (_) {}
}

// Tab navigation away — keep everything visible/stateful for a seamless return.
function suspend() {
  _teardownConnections();
}

// Returning to the Browser tab after a suspend: the page, URL and session are
// still in module state, so repaint the saved frame immediately and reconnect
// the live stream on top of it — no blank "type a URL to start" flash, no reset.
function resumeRetained() {
  if (surfaceMode === 'desktop') {
    connectDesktop();
    return;
  }
  if (surfaceMode === 'extension') {
    _showExtensionStage();
    refreshExtensionStatus().then(_startExtensionStatusPolling);
    return;
  }
  _showConnectingStage();
  setViewMode('mirror');
  _repaintLast();
  connectedBs = null;
  doConnect();
}

export function startBrowser() {
  active = true;
  if (!mounted) {
    if (!mount()) { active = false; return; }
    mounted = true;
  }
  _updateSurfaceUi();
  if (surfaceMode === 'idle') {
    _showIdleStage();
    return;
  }
  if (surfaceMode === 'desktop') {
    if (!desktopWs || desktopWs.readyState > WebSocket.OPEN) connectDesktop();
    return;
  }
  if (surfaceMode === 'extension') {
    _showExtensionStage();
    refreshExtensionStatus().then(_startExtensionStatusPolling);
    return;
  }
  if (ws && ws.readyState <= WebSocket.OPEN) return;  // already connected
  if (currentBs && currentBs.id) resumeRetained();     // flip back to a session we already had
  else connect();                                      // first open / no session yet
}

export function stopBrowser() {
  active = false;
  // Suspend (not disconnect) so the rendered page + session survive the tab
  // switch and can be restored instantly when the user comes back.
  suspend();
}

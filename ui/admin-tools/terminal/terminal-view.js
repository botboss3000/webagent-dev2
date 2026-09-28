'use strict';

// ── FEATURE: Terminal Launcher (admin sub-view) ───────────────────────────
// FILE: ui/admin-tools/terminal/terminal-view.js
//
// Self-contained Terminal drop-in. Owns everything terminal: its own tab
// array (`termTabs`), tab strip + carousel, in-page xterm panes, the keyboard
// bar, gestures (pinch/long-press/drag-scroll), the tmux + PTY session
// launcher panel, and the Ctrl+` global shortcut. It shares NOTHING with the
// File Manager (Explorer) — the two were split out of the old monolithic
// files.js so each is independent. The only things it leans on are app-wide
// infrastructure (the low-level xterm factory in shared/js/terminal.js, the
// generic floating-menu + lucide helpers in shared/js/dom-utils.js, clipboard,
// uuid) and the Admin-Tools FRAME, reached through window.__applySidebarView /
// app.setSidebarState — never through Explorer.
//
// Lifecycle: the frame discovers this file from terminal/page.json and calls
// startView()/stopView() when the Terminal sidebar view is (de)activated,
// exactly like every other drop-in admin view (Database, Git Control…).

import { createTerminalInstance } from '../../shared/js/terminal.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { copyText } from '../../shared/js/clipboard.js';
import { isMobileLayout } from '../../shared/js/layout.js';
import { app } from '../../shared/js/state.js';
import { _refreshLucideIcons, openFloatingMenu, closeFloatingMenu } from '../../shared/js/dom-utils.js';
import { authHeaders } from '../../shared/js/left-login.js';
// Shared infra (not Explorer): the terminal sidebar's restart button reuses the
// server-restart helper that lives with the Git Control drop-in.
import { restartServerAndReload } from '../../shared/js/files-git.js';
import { kvRead, kvWrite } from '../../shared/js/kv-ui-state.js';

// ── Module state ──────────────────────────────────────────────────
const API_BASE = '/api/v1/files';
const LS_SIDEBAR_VIEW  = 'files.sidebarView';
const LS_TERM_FONT_SIZE = 'files.terminalFontSize';
const TERM_FONT_DEFAULT = 14;
const LS_ACTIVE_TERMINAL = 'files.activeTerminal';
const LS_TERM_OPEN_TABS  = 'files.openTerminals';   // terminal-only persisted tab list
const LS_OPEN_TABS_LEGACY = 'files.openTabs';        // pre-split mixed list (read once, for migration)
// kvCache rows (tenant-scoped app_cache); the LS_* keys above are the sync
// localStorage mirrors written alongside (see shared/js/kv-ui-state.js).
const KC_SIDEBAR_VIEW   = 'terminal:sidebarView';
const KC_TERM_FONT_SIZE = 'terminal:fontSize';
const KC_ACTIVE_TERMINAL = 'terminal:activeTerminal';
const KC_TERM_OPEN_TABS  = 'terminal:openTabs';
const KC_TERM_SESSION_NAMES = 'terminal:sessionNames';

let termTabs = [];             // { path(session_id), name, kind:'terminal', instance, wrap, tmuxSession, sshServerId, initialCommand, claudeSessionId, closing }
let activeTerminalId = null;   // session_id of the active terminal tab
let dragSrcTermPath = null;    // path of the terminal tab being dragged

// ── Shared fetch helper (auth headers + user_id), duplicated verbatim from the
// frame so the module has no dependency on Explorer. Pure infrastructure. The
// /api/-prefix branch matters here: the launcher panel hits absolute
// /api/v1/terminal/... endpoints, everything else is a files-relative subpath. ──
function withUserIdParam(path) {
  // Append the active user_id as a query param. The backend prefers the JWT
  // when valid, but falls back to this so the page still works if the cached
  // token is stale.
  const uid = localStorage.getItem('auth_user_id') || '';
  if (!uid) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'user_id=' + encodeURIComponent(uid);
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, authHeaders(), opts.headers || {});
  if (opts.body && !('Content-Type' in headers)) {
    headers['Content-Type'] = 'application/json';
  }
  const url = path.startsWith('/api/') ? withUserIdParam(path) : (API_BASE + withUserIdParam(path));
  const res = await fetch(url, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  return res.json();
}

// ── Terminal tab strip ─────────────────────────────────────────────

function renderTermTabs() {
  const termBar = document.getElementById('files-term-tabs');
  if (!termBar) return;
  termBar.innerHTML = '';
  for (const tab of termTabs) {
    const isActive = tab.path === activeTerminalId;
    const el = document.createElement('div');
    el.className = 'files-tab' + (isActive ? ' active' : '') + (tab.closing ? ' closing' : '');
    el.dataset.path = tab.path;
    el.draggable = true;
    el.title = tab.path;

    const iconWrap = document.createElement('span');
    iconWrap.className = 'files-tab-icon files-tab-icon-dotonly';
    const dot = document.createElement('span');
    dot.className = 'files-tab-conn-dot';
    const initialState = (tab.instance && tab.instance.getState && tab.instance.getState()) || 'connecting';
    dot.dataset.state = initialState;
    dot.title = _connStateTitle(initialState);
    iconWrap.appendChild(dot);
    el.appendChild(iconWrap);

    const label = document.createElement('span');
    label.className = 'files-tab-label';
    label.textContent = tab.name;
    label.title = 'Double-click to rename';
    label.addEventListener('dblclick', (e) => { e.stopPropagation(); e.preventDefault(); startInlineRename(tab, label); });
    el.appendChild(label);

    const more = document.createElement('button');
    more.className = 'files-tab-more';
    more.type = 'button';
    more.title = 'More actions';
    more.draggable = false;
    const moreI = document.createElement('i');
    moreI.setAttribute('data-lucide', 'more-vertical');
    moreI.className = 'lucide-icon';
    more.appendChild(moreI);
    more.addEventListener('mousedown', (e) => { e.stopPropagation(); if (e.button === 0) { e.preventDefault(); showTerminalTabMenu(tab, more); } });
    more.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });
    more.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(more);

    const close = document.createElement('button');
    close.className = 'files-tab-close';
    close.type = 'button';
    close.title = tab.closing ? 'Closing…' : 'Close (middle-click also works)';
    close.draggable = false;
    close.disabled = !!tab.closing;
    const xI = document.createElement('i');
    xI.setAttribute('data-lucide', tab.closing ? 'loader-2' : 'x');
    xI.className = 'lucide-icon' + (tab.closing ? ' files-tab-spin' : '');
    xI.style.pointerEvents = 'none';
    close.appendChild(xI);
    close.addEventListener('mousedown', (e) => { e.stopPropagation(); if (e.button === 0 || e.button === 1) { e.preventDefault(); closeTermTab(tab.path); } });
    close.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });
    close.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(close);

    el.addEventListener('click', () => activateTermTab(tab.path));
    el.addEventListener('mousedown', (e) => { if (e.button === 1) { e.preventDefault(); closeTermTab(tab.path); } });

    // Drag-and-drop reordering (within the terminal bar only).
    el.addEventListener('dragstart', (e) => {
      dragSrcTermPath = tab.path;
      el.classList.add('dragging');
      try { e.dataTransfer.setData('text/plain', tab.path); } catch (_) {}
      e.dataTransfer.effectAllowed = 'move';
    });
    el.addEventListener('dragend', () => {
      dragSrcTermPath = null;
      el.classList.remove('dragging');
      document.querySelectorAll('.files-tab.drop-before, .files-tab.drop-after')
        .forEach((t) => t.classList.remove('drop-before', 'drop-after'));
    });
    el.addEventListener('dragover', (e) => {
      if (!dragSrcTermPath || dragSrcTermPath === tab.path) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      el.classList.toggle('drop-before', before);
      el.classList.toggle('drop-after', !before);
    });
    el.addEventListener('dragleave', () => { el.classList.remove('drop-before', 'drop-after'); });
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragSrcTermPath || dragSrcTermPath === tab.path) return;
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      reorderTermTab(dragSrcTermPath, tab.path, before);
    });

    termBar.appendChild(el);
  }
  _refreshLucideIcons(termBar);
  updateTermCarousel();
}

function reorderTermTab(srcPath, destPath, before) {
  const srcIdx = termTabs.findIndex((t) => t.path === srcPath);
  const destIdx = termTabs.findIndex((t) => t.path === destPath);
  if (srcIdx < 0 || destIdx < 0) return;
  const [src] = termTabs.splice(srcIdx, 1);
  let insertAt = termTabs.findIndex((t) => t.path === destPath);
  if (!before) insertAt += 1;
  termTabs.splice(insertAt, 0, src);
  renderTermTabs();
  persistTermTabs();
}

// ── Terminal tab carousel (its own bar + pinned "+" add button) ────
function updateTermCarousel() {
  const bar = document.getElementById('files-term-tabs');
  const prev = document.getElementById('files-term-tabs-prev');
  const next = document.getElementById('files-term-tabs-next');
  if (!bar || !prev || !next) return;
  const overflow = bar.scrollWidth > bar.clientWidth + 1;
  if (!overflow) { prev.style.display = 'none'; next.style.display = 'none'; return; }
  prev.style.display = 'inline-flex';
  next.style.display = 'inline-flex';
  prev.disabled = bar.scrollLeft <= 0;
  next.disabled = bar.scrollLeft + bar.clientWidth >= bar.scrollWidth - 1;
}

let _termCarouselWired = false;
function initTermCarousel() {
  if (_termCarouselWired) return;
  const SCROLL_STEP = 160;
  const bar = document.getElementById('files-term-tabs');
  const prev = document.getElementById('files-term-tabs-prev');
  const next = document.getElementById('files-term-tabs-next');
  if (bar && prev && next) {
    _termCarouselWired = true;
    prev.addEventListener('click', () => { bar.scrollBy({ left: -SCROLL_STEP, behavior: 'smooth' }); });
    next.addEventListener('click', () => { bar.scrollBy({ left:  SCROLL_STEP, behavior: 'smooth' }); });
    bar.addEventListener('scroll', updateTermCarousel, { passive: true });
    [prev, next].forEach((btn, i) => {
      btn.addEventListener('dragover', (e) => { e.preventDefault(); bar.scrollBy({ left: (i === 0 ? -1 : 1) * 40, behavior: 'auto' }); });
    });
    if (typeof ResizeObserver !== 'undefined') new ResizeObserver(updateTermCarousel).observe(bar);
    window.addEventListener('resize', updateTermCarousel);
  }

  // Always-visible "new terminal tab" button, pinned outside the scrolling
  // carousel. TAP = blank terminal tab; LONG-PRESS (≥500ms) = new tmux session
  // with a default name + a white confirmation popup.
  const addBtn = document.getElementById('files-term-new-tab');
  if (addBtn && !addBtn.dataset.wired) {
    addBtn.dataset.wired = '1';
    let lpTimer = null;
    let lpFired = false;
    const LP_MS = 500;
    const clearLp = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
    addBtn.addEventListener('pointerdown', (e) => {
      if (e.button !== 0 && e.pointerType === 'mouse') return;
      lpFired = false;
      clearLp();
      lpTimer = setTimeout(() => {
        lpTimer = null;
        lpFired = true;
        const name = openNewTmuxSessionDefault();
        _flashTermPop(addBtn, 'New tmux · ' + name);
      }, LP_MS);
    });
    addBtn.addEventListener('pointerup', clearLp);
    addBtn.addEventListener('pointerleave', clearLp);
    addBtn.addEventListener('pointercancel', clearLp);
    addBtn.addEventListener('contextmenu', (e) => e.preventDefault());
    addBtn.addEventListener('click', (e) => {
      if (lpFired) { lpFired = false; e.preventDefault(); e.stopPropagation(); return; }
      openNewTerminalTab();
    });
  }
}

// ── Terminal panes (xterm hosts) ──────────────────────────────────

function renderTermPanes() {
  const termContent = document.getElementById('files-term-content');
  if (!termContent) return;

  function showWelcome(host, html) {
    if (!host) return;
    if (host.querySelector('.files-welcome')) return;
    host.innerHTML = html;
    _refreshLucideIcons(host);
  }
  if (!termTabs.length) {
    showWelcome(termContent, `
      <div class="files-welcome">
        <i data-lucide="terminal" class="lucide-icon files-welcome-icon"></i>
        <div class="files-welcome-title">Terminal</div>
        <div class="files-welcome-text">Open a session from the launcher on the left, or press <kbd>Ctrl</kbd>+<kbd>\`</kbd>.</div>
      </div>`);
    // No tabs — drop any stale panes.
    termContent.querySelectorAll('.files-editor-pane').forEach((p) => p.remove());
    return;
  }

  const existing = new Map();
  termContent.querySelectorAll('.files-editor-pane').forEach((p) => existing.set(p.dataset.path, p));
  for (const [path, pane] of existing) {
    if (!termTabs.find((t) => t.path === path)) { pane.remove(); existing.delete(path); }
  }
  const welcome = termContent.querySelector('.files-welcome');
  if (welcome) welcome.remove();
  for (const tab of termTabs) {
    let pane = existing.get(tab.path);
    if (!pane) {
      pane = buildTermPane(tab);
      termContent.appendChild(pane);
    }
    pane.classList.toggle('active', tab.path === activeTerminalId);
  }
}

// ── Tab actions ───────────────────────────────────────────────────

function activateTermTab(path) {
  const tab = termTabs.find((t) => t.path === path);
  if (!tab) return;
  activeTerminalId = path;
  renderTermTabs();
  const host = document.getElementById('files-term-content');
  if (host) {
    host.querySelectorAll('.files-editor-pane').forEach((p) => {
      p.classList.toggle('active', p.dataset.path === path);
    });
  }
  // Terminal tabs need a refit each time they regain focus — xterm can't
  // measure while its pane is display:none.
  if (tab.instance) {
    setTimeout(() => { tab.instance.fit(); tab.instance.focus(); }, 30);
  }
  const tabEl = document.querySelector('#files-term-tabs .files-tab[data-path="' + cssEscape(path) + '"]');
  if (tabEl && typeof tabEl.scrollIntoView === 'function') {
    tabEl.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }
  kvWrite(KC_ACTIVE_TERMINAL, LS_ACTIVE_TERMINAL, path);
}

async function closeTermTab(path) {
  const tab = termTabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.closing) return;
  // Terminal tabs: only remove from the UI after the backend confirms the PTY
  // is gone. If the DELETE fails we keep the tab so the user can retry instead
  // of silently leaking the still-running shell.
  if (tab.instance) {
    tab.closing = true;
    renderTermTabs();
    try {
      await tab.instance.closeBackendSession();
    } catch (e) {
      tab.closing = false;
      renderTermTabs();
      alert('Could not close terminal "' + tab.name + '":\n\n' + (e.message || e) +
        '\n\nThe shell may still be running on the server. Try again.');
      return;
    }
    try { tab.instance.dispose(); } catch (_) {}
    tab.instance = null;
  }
  const idx = termTabs.findIndex((t) => t.path === path);
  if (idx < 0) return;
  termTabs.splice(idx, 1);
  const host = document.getElementById('files-term-content');
  if (host) {
    const pane = host.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.remove();
  }
  if (activeTerminalId === path) {
    activeTerminalId = termTabs.length ? termTabs[Math.min(idx, termTabs.length - 1)].path : null;
  }
  renderTermTabs();
  renderTermPanes();
  persistTermTabs();
}

function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return s.replace(/(["\\])/g, '\\$1');
}

// ── Persistence (terminal tabs only) ──────────────────────────────

function persistTermTabs() {
  const minimal = termTabs.map((t) => ({
    path: t.path, name: t.name, kind: 'terminal',
    wrap: t.wrap !== false, tmuxSession: t.tmuxSession || '', sshServerId: t.sshServerId || '',
  }));
  kvWrite(KC_TERM_OPEN_TABS, LS_TERM_OPEN_TABS, minimal);
  kvWrite(KC_ACTIVE_TERMINAL, LS_ACTIVE_TERMINAL, activeTerminalId || '');
}

async function restoreTermTabs() {
  try {
    let saved = kvRead(KC_TERM_OPEN_TABS, LS_TERM_OPEN_TABS) || null;
    // Migration: first run after the explorer/terminal split — pull the terminal
    // entries out of the old mixed list.
    if (!Array.isArray(saved)) {
      let legacy = [];
      try { legacy = JSON.parse(localStorage.getItem(LS_OPEN_TABS_LEGACY) || '[]'); } catch (_) { legacy = []; }
      saved = Array.isArray(legacy) ? legacy.filter((t) => t && t.kind === 'terminal') : [];
    }
    let wantTerm = kvRead(KC_ACTIVE_TERMINAL, LS_ACTIVE_TERMINAL) || '';
    if (!Array.isArray(saved) || !saved.length) { renderTermTabs(); renderTermPanes(); return; }
    for (const t of saved) {
      try {
        pushTerminalTab(t.path, t.name, { tmuxSession: t.tmuxSession || '', sshServerId: t.sshServerId || '' });
        const restored = termTabs[termTabs.length - 1];
        if (restored && t.wrap === false) restored.wrap = false;
      } catch (_) {}
    }
    renderTermTabs();
    renderTermPanes();
    if (wantTerm && termTabs.find((t) => t.path === wantTerm)) activateTermTab(wantTerm);
  } catch (_) { renderTermTabs(); renderTermPanes(); }
}

// ── Global keyboard shortcuts (Ctrl+` from anywhere, terminal zoom) ─
// Wired once at module load so Ctrl+` works app-wide even before the Terminal
// view has been opened (the module loads at boot via reconnect.js's import).
let _globalKeysWired = false;
function initTermGlobalKeys() {
  if (_globalKeysWired) return;
  _globalKeysWired = true;

  // Ctrl+` (Backquote) opens a new terminal tab from anywhere in the app.
  document.addEventListener('keydown', (e) => {
    if (e.code !== 'Backquote') return;
    if (!e.ctrlKey || e.altKey || e.metaKey || e.shiftKey) return;
    e.preventDefault();
    e.stopPropagation();
    const tabSelect = document.getElementById('main-tab-select');
    if (tabSelect && tabSelect.value !== 'files') {
      tabSelect.value = 'files';
      tabSelect.dispatchEvent(new Event('change', { bubbles: true }));
    }
    openNewTerminalTab();
  }, true);

  // Zoom shortcuts — only when a terminal tab is active, so they don't hijack
  // browser zoom elsewhere.
  document.addEventListener('keydown', (e) => {
    if (!(e.ctrlKey || e.metaKey) || e.altKey || e.shiftKey) return;
    const active = getActiveTerminalTab();
    if (!active) return;
    const t = e.target;
    if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return;
    const isPlus  = e.code === 'Equal' || e.key === '+' || e.key === '=';
    const isMinus = e.code === 'Minus' || e.key === '-' || e.key === '_';
    const isZero  = e.code === 'Digit0' || e.key === '0';
    if (!isPlus && !isMinus && !isZero) return;
    e.preventDefault();
    e.stopPropagation();
    if (isPlus)  terminalZoom(+1);
    if (isMinus) terminalZoom(-1);
    if (isZero)  terminalResetZoom();
  }, true);

  // Refit the active terminal on viewport resize. Background tabs refit when
  // they next become active (see activateTermTab).
  window.addEventListener('resize', () => {
    const tab = getActiveTerminalTab();
    if (tab && tab.instance) tab.instance.fit();
  });
}

// Keep visible terminal tabs in sync with sidebar resize drags (the handle is
// created by the frame, so this wires lazily on first Terminal-view open).
let _resizeHandleWired = false;
function initTermResizeHandle() {
  if (_resizeHandleWired) return;
  const handle = document.getElementById('files-resize-handle');
  if (!handle) return;
  _resizeHandleWired = true;
  handle.addEventListener('mouseup', () => {
    const tab = getActiveTerminalTab();
    if (tab && tab.instance) setTimeout(() => tab.instance.fit(), 30);
  });
}


// ══════════════════════════════════════════════════════════════════

// ── Build a terminal pane (xterm host) — the terminal branch of the old
// buildPaneForTab, now standalone. ──
function buildTermPane(tab) {
  const pane = document.createElement('div');
  pane.className = 'files-editor-pane';
  pane.dataset.path = tab.path;
  pane.dataset.mode = 'terminal';
    pane.classList.add('files-terminal-pane');
    // Find bar above the host — hidden until Ctrl+F. Placed before the
    // host so it stacks on top in flex-column layout.
    const findBar = buildTerminalFindBar();
    pane.appendChild(findBar);
    // Scroll wrapper owns the horizontal scrollbar in no-wrap mode. The
    // host inside is grown wider than the wrapper via CSS so the user can
    // swipe / drag to see off-screen content.
    const scrollWrap = document.createElement('div');
    scrollWrap.className = 'files-terminal-scroll';
    if (tab.wrap === false) scrollWrap.classList.add('files-terminal-scroll-nowrap');
    const host = document.createElement('div');
    host.className = 'files-terminal-host';
    if (tab.wrap === false) host.classList.add('files-terminal-host-nowrap');
    scrollWrap.appendChild(host);
    pane.appendChild(scrollWrap);

    // "Disconnected" overlay — shown whenever the WS isn't connected so the
    // user knows their keystrokes aren't reaching the shell (the xterm genui
    // still looks live even when the socket is dead). pointer-events:none in
    // CSS keeps clicks going through to xterm for focus restore.
    const overlay = document.createElement('div');
    overlay.className = 'files-terminal-overlay';
    const overlayText = document.createElement('div');
    overlayText.className = 'files-terminal-overlay-text';
    overlay.appendChild(overlayText);
    pane.appendChild(overlay);

    // Scroll-to-bottom FAB — floats bottom-right; visible only when scrolled up.
    const scrollBot = document.createElement('button');
    scrollBot.className = 'files-term-scroll-bot';
    scrollBot.type = 'button';
    scrollBot.title = 'Scroll to bottom';
    scrollBot.setAttribute('aria-label', 'Scroll to bottom');
    scrollBot.innerHTML = '<i data-lucide="chevrons-down" class="lucide-icon"></i>';
    pane.appendChild(scrollBot);

    // Clicking anywhere on the pane should restore xterm focus. xterm's input
    // is in a hidden helper textarea that only auto-focuses when the click
    // lands on a row glyph; clicks on padding/margins otherwise look focused
    // but actually drop input on the floor.
    host.addEventListener('mousedown', () => {
      if (tab.instance) {
        try { tab.instance.focus(); } catch (_) {}
      }
    });

    // Drag-and-drop. Two cases:
    //  • A file from the file TREE → its absolute path (shell-quoted) is pasted
    //    at the prompt. The tree marshals the path as text/plain in dragstart.
    //  • An external IMAGE file dropped from the OS → uploaded to the server via
    //    the same relay as the Paste chip, then its saved path typed in (quoted)
    //    so Claude Code reads it as an image. Detected via dataTransfer "Files".
    host.addEventListener('dragover', (e) => {
      if (!e.dataTransfer) return;
      const types = Array.from(e.dataTransfer.types || []);
      if (types.indexOf('text/plain') !== -1 || types.indexOf('Files') !== -1) {
        e.preventDefault();
        e.dataTransfer.dropEffect = 'copy';
        host.classList.add('files-terminal-drop-target');
      }
    });
    host.addEventListener('dragleave', () => host.classList.remove('files-terminal-drop-target'));
    host.addEventListener('drop', (e) => {
      host.classList.remove('files-terminal-drop-target');
      if (!tab.instance) return;
      // An external image file takes priority over the text/plain path.
      const dt = e.dataTransfer;
      const imgFile = dt && dt.files && Array.from(dt.files).find(
        (f) => f.type && f.type.startsWith('image/'),
      );
      if (imgFile) {
        e.preventDefault();
        uploadTerminalPasteImage(imgFile, imgFile.type)
          .then((path) => {
            if (path && tab.instance) {
              tab.instance.paste('"' + path + '" ');
              tab.instance.focus();
            }
          })
          .catch((err) => alert('Could not add image: ' + ((err && err.message) ? err.message : err)));
        return;
      }
      const raw = dt && dt.getData('text/plain');
      if (!raw) return;
      e.preventDefault();
      tab.instance.paste(shellQuote(raw) + ' ');
      tab.instance.focus();
    });

    // Long-press on mobile → context menu with Copy / Paste / Select all.
    // Fires after a 500ms hold that didn't move; cancelled on move / lift.
    wireTerminalLongPress(host, () => tab.instance);
    // Two-finger pinch → adjust the global terminal font size.
    wireTerminalPinchZoom(host);
    // One-finger drag → scroll the scrollback (touch screens have no wheel
    // and xterm doesn't pan on a single-finger drag by itself).
    wireTerminalDragScroll(host, () => tab.instance);

    // xterm.open() measures its host immediately. The pane has just been
    // created (not yet appended to the document) — defer xterm creation
    // until after the pane is attached AND the browser has reflowed, so
    // fit() sees the final host width. Without the rAF, on slow mobile
    // browsers the host can still report a stale width and xterm picks
    // too many cols, making the shell think it has more room than it
    // does and wrap behaviour appears broken.
    requestAnimationFrame(() => requestAnimationFrame(async () => {
      if (!document.body.contains(pane)) return;
      try {
        // tab.path is the backend session_id (see pushTerminalTab). Passing
        // the existing id on restore reattaches to the running shell.
        tab.instance = await createTerminalInstance(host, tab.path, {
          initialCommand: tab.initialCommand || '',
          claudeSessionId: tab.claudeSessionId || '',
          sshServerId: tab.sshServerId || '',
          wrap: tab.wrap !== false,           // default true unless persisted false
          fontSize: getTerminalFontSize(),    // global setting, shared across tabs
          // Re-evaluated on every (re)connect so inline-rename and the cached
          // localStorage name are reflected on the server without reconnecting.
          nameProvider: () => (tab.name || getTerminalSessionName(tab.path) || ''),
        });
        // Consume the command — buildPaneForTab can be called again later
        // (e.g. pane mode swap), but the shell already has it.
        tab.initialCommand = '';
        // Wire scroll-to-bottom FAB: show when viewport is not at the buffer end.
        const _term = tab.instance && tab.instance.term;
        if (_term && scrollBot) {
          const _updateFab = () => {
            const buf = _term.buffer.active;
            scrollBot.classList.toggle('visible', buf.viewportY < buf.length - _term.rows);
          };
          _term.onScroll(_updateFab);
          scrollBot.addEventListener('click', () => {
            try { _term.scrollToBottom(); _term.focus(); } catch (_) {}
          });
          _refreshLucideIcons(scrollBot);
        }
        // Drive the per-tab status dot AND the pane overlay from the WS
        // state machine. The dot is a small at-a-glance hint on the tab; the
        // overlay is the loud "your keystrokes aren't reaching the shell"
        // signal on the pane itself.
        tab.instance.onStateChange((s) => {
          _updateTabConnDot(tab.path, s);
          pane.classList.toggle('files-terminal-disconnected', s !== 'connected');
          overlayText.textContent = _connStateTitle(s);
        });
        tab.instance.fit();
        // Belt-and-braces second fit after another paint — covers any
        // late layout shift from the address bar settling on iOS Safari.
        setTimeout(() => { if (tab.instance) tab.instance.fit(); }, 150);
        // Wire Ctrl+F → find bar. Capture-phase on the host so we preempt
        // xterm's keydown handler (which otherwise forwards Ctrl+F bytes
        // to the shell).
        host.addEventListener('keydown', (e) => {
          if ((e.ctrlKey || e.metaKey) && (e.key === 'f' || e.key === 'F')) {
            e.preventDefault();
            e.stopPropagation();
            openTerminalFindBar(findBar, tab.instance);
          }
        }, true);
        if (tab.path === activeTerminalId) tab.instance.focus();
      } catch (e) {
        host.textContent = 'Failed to start terminal: ' + (e.message || e);
      }
    }));
  return pane;
}


// ══════════════════════════════════════════════════════════════════


function getTerminalFontSize() {
  const raw = parseInt(kvRead(KC_TERM_FONT_SIZE, LS_TERM_FONT_SIZE), 10);
  return Number.isFinite(raw) && raw >= 8 && raw <= 32 ? raw : TERM_FONT_DEFAULT;
}
function setTerminalFontSize(n) {
  kvWrite(KC_TERM_FONT_SIZE, LS_TERM_FONT_SIZE, n);
  // Propagate to every open terminal — font size is global, not per-tab.
  for (const t of termTabs) {
    if (t.kind === 'terminal' && t.instance && t.instance.setFontSize) {
      try { t.instance.setFontSize(n); } catch (_) {}
    }
  }
}

// Look up the active terminal tab (used by the terminal main and the
// global keyboard shortcuts).
function getActiveTerminalTab() {
  if (!activeTerminalId) return null;
  return termTabs.find((t) => t.kind === 'terminal' && t.path === activeTerminalId) || null;
}


// Upload a pasted/dropped image blob to the server and return the saved file's
// absolute path. The caller types that path into the terminal so Claude Code
// (or any program that reads an image path) can pick the image up — the web
// terminal is a text-only pipe, so an image can't be sent through it directly.
// Used by both the keybar Paste chip and the drag-an-image-onto-the-terminal
// drop handler. Throws on a non-2xx response so the caller can surface why.
async function uploadTerminalPasteImage(blob, mimeType) {
  const sub = (mimeType && mimeType.split('/')[1]) || 'png';
  const ext = '.' + sub.split('+')[0];   // image/svg+xml → .svg
  const form = new FormData();
  form.append('file', blob, 'clipboard' + ext);
  // Raw fetch (not apiFetch): FormData must set its own multipart boundary, so
  // we can't let apiFetch force a JSON Content-Type. Auth + user_id match the
  // rest of the terminal API.
  const res = await fetch(
    withUserIdParam('/api/v1/terminal/paste-image'),
    { method: 'POST', headers: authHeaders(), body: form },
  );
  if (!res.ok) {
    let detail = 'HTTP ' + res.status;
    try { const j = await res.json(); if (j && j.detail) detail = j.detail; } catch (_) {}
    throw new Error(detail);
  }
  const data = await res.json();
  return (data && data.path) ? data.path : '';
}


function showTerminalTabMenu(tab, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const wrapOn = tab.wrap !== false;
  const items = [
    { icon: 'pencil',    label: 'Rename…',        action: () => startInlineRename(tab, document.querySelector('#files-term-tabs .files-tab[data-path="' + cssEscape(tab.path) + '"] .files-tab-label')) },
    { icon: 'wrap-text', label: 'Wrap lines',     checked: wrapOn, action: () => toggleTerminalWrap(tab.path) },
    { separator: true },
    { icon: 'zoom-in',   label: 'Zoom in',        action: () => terminalZoom(+1) },
    { icon: 'zoom-out',  label: 'Zoom out',       action: () => terminalZoom(-1) },
    { icon: 'refresh-cw', label: 'Reset zoom',    action: () => terminalResetZoom() },
    { separator: true },
    { icon: 'search',    label: 'Find…',          action: () => openTerminalFindFromMenu(tab.path) },
  ];
  openFloatingMenu(items, rect.bottom + 2, rect.right - 180);
}

function toggleTerminalWrap(tabPath) {
  const tab = termTabs.find((t) => t.path === tabPath);
  if (!tab || tab.kind !== 'terminal') return;
  tab.wrap = !(tab.wrap !== false);   // flip; treat undefined as true
  // Sync the CSS classes that control the scroll wrapper's overflow-x and
  // the host's width. The terminal pane is a sibling of the find bar; the
  // host lives inside .files-terminal-scroll.
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(tabPath) + '"]');
  if (pane) {
    const scrollWrap = pane.querySelector('.files-terminal-scroll');
    const host = pane.querySelector('.files-terminal-host');
    if (scrollWrap) scrollWrap.classList.toggle('files-terminal-scroll-nowrap', !tab.wrap);
    if (host)       host.classList.toggle('files-terminal-host-nowrap', !tab.wrap);
  }
  if (tab.instance && tab.instance.setWrap) tab.instance.setWrap(tab.wrap);
  // Refit after the layout change. Two rAF ticks guarantee the browser has
  // applied the new width to the host BEFORE fitAddon measures — on mobile
  // a 30ms setTimeout sometimes fires before layout has reflowed, so xterm
  // keeps its old cols and wrap appears not to work.
  requestAnimationFrame(() => requestAnimationFrame(() => {
    if (!tab.instance) return;
    tab.instance.fit();
    // Print a one-line confirmation so the user can see the toggle took
    // effect and what cols xterm is now using. Future shell output will
    // wrap (or not) at this column count.
    try {
      const cols = (tab.instance.term && tab.instance.term.cols) || '?';
      const msg = tab.wrap
        ? '\r\n\x1b[2;33m[wrap ON — ' + cols + ' cols, lines wrap to next row]\x1b[0m\r\n'
        : '\r\n\x1b[2;33m[wrap OFF — ' + cols + ' cols, swipe horizontally to see overflow]\x1b[0m\r\n';
      tab.instance.term.write(msg);
    } catch (_) {}
  }));
  persistTermTabs();
}

function terminalZoom(delta) {
  const next = getTerminalFontSize() + delta;
  setTerminalFontSize(next);
}
function terminalResetZoom() {
  setTerminalFontSize(TERM_FONT_DEFAULT);
}

function openTerminalFindFromMenu(tabPath) {
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(tabPath) + '"]');
  const bar = pane && pane.querySelector('.files-terminal-findbar');
  const tab = termTabs.find((t) => t.path === tabPath);
  if (bar && tab && tab.instance) openTerminalFindBar(bar, tab.instance);
}


// Deliver a named key to a tmux session via the backend's `tmux send-keys`.
// Used for keys that a raw escape sequence can't reliably trigger inside tmux:
// modern TUIs (Claude Code) negotiate extended keys with tmux 3.x and then stop
// recognising the legacy form of MODIFIED keys (Shift+Tab → \e[Z). Letting tmux
// itself originate the key means it's encoded to match the pane's current
// keyboard mode, whatever that is. Rejects (so callers can fall back to raw)
// if tmux isn't installed, the session is gone, or the request fails.
async function _sendTmuxKey(session, key) {
  await apiFetch('/api/v1/terminal/tmux/send-keys', {
    method: 'POST',
    body: JSON.stringify({ session, key }),
  });
}

// Host hook for terminal.js's Shift+Tab interceptor. terminal.js catches the
// key at keydown (so the browser can't steal it for focus traversal) and asks
// us to deliver it. For a tmux tab we re-originate it through `tmux send-keys`
// (BTab) so it reaches a TUI (Claude Code) that ignores the raw back-tab under
// tmux's extended-keys; we return true to tell terminal.js we took over. For a
// non-tmux tab we return false and terminal.js injects the raw \e[Z itself.
// Defined at module scope (not inside the keybar) so it works on desktop too.
window.__termShiftTab = (sessionId) => {
  let tab = null;
  if (sessionId) {
    tab = termTabs.find((t) => t.kind === 'terminal' && t.path === sessionId) || null;
  }
  if (!tab) tab = getActiveTerminalTab();
  if (!tab || !tab.tmuxSession) return false;  // plain shell → let terminal.js send raw \e[Z
  _sendTmuxKey(tab.tmuxSession, 'shift-tab').catch(() => {
    // tmux call failed (session gone / not installed) — fall back to raw byte.
    try { if (tab.instance && tab.instance.paste) tab.instance.paste('\x1b[Z'); } catch (_) {}
  });
  return true;
};

// Shortcut-key panel in the terminal tab bar. On mobile keyboards there
// are no arrow keys, so the bottom row + middle cell of this 3x3 pad
// pipes ANSI cursor escapes into the active terminal. The remaining
// cells are placeholders for future shortcuts.
// Always-visible sticky bottom bar of chips: Ctrl (one-shot/lock), ^C copy,
// arrows, new-line, copy, paste, mic. Ctrl chip arms on tap, locks on
// long-press so subsequent arrow taps become word-jump sequences.
function initTerminalKeybar() {
  const bar = document.getElementById('files-term-keybar');
  if (!bar || bar.dataset.wired) return;
  bar.dataset.wired = '1';

  // ── Modifier state ───────────────────────────────────────────────
  // 'off' | 'armed' (one-shot) | 'locked' (until tapped again).
  // tmux locked sends the Ctrl+B prefix once at lock time; subsequent keys
  // pass through unmodified — tmux interprets them server-side.
  const mod = { ctrl: 'off', tmux: 'off' };
  function setMod(name, state) {
    mod[name] = state;
    const chip = bar.querySelector('[data-mod="' + name + '"]');
    if (!chip) return;
    chip.dataset.armed = state === 'armed' ? '1' : '';
    chip.dataset.locked = state === 'locked' ? '1' : '';
  }
  function consumeArm() {
    if (mod.ctrl === 'armed') setMod('ctrl', 'off');
    if (mod.tmux === 'armed') setMod('tmux', 'off');
  }
  function sendToActive(bytes) {
    const tab = getActiveTerminalTab();
    if (!tab || !tab.instance || !tab.instance.paste) return false;
    tab.instance.paste(bytes);
    try { tab.instance.focus(); } catch (_) {}
    return true;
  }

  // Translate a chip key (data-key) into raw bytes the PTY expects. When
  // Ctrl is armed/locked, arrows escalate to word-jump sequences.
  function chipBytes(key) {
    const PLAIN = {
      esc:           '\x1b',
      tab:           '\t',
      'shift-tab':   '\x1b[Z',   // CSI Z — back-tab (e.g. cycles modes in Claude Code)
      enter:         '\r',
      sigint:        '\x03',     // Ctrl+C interrupt (the row-1 "^C" chip is copy, not this)
      up:            '\x1b[A',
      down:          '\x1b[B',
      right:         '\x1b[C',
      left:          '\x1b[D',
      'shift-enter': '\x1b\r',
      'ctrl-d':      '\x04',
      'ctrl-l':      '\x0c',
      'ctrl-r':      '\x12',
      'ctrl-z':      '\x1a',
      'lit-pipe':    '|',
      'lit-tilde':   '~',
      'lit-fwd':     '/',
      'lit-bk':      '\\',
      'lit-tick':    '`',
    };
    if (mod.ctrl !== 'off' && /^(up|down|left|right)$/.test(key)) {
      const map = { up: 'A', down: 'B', right: 'C', left: 'D' };
      return '\x1b[1;5' + map[key];
    }
    return Object.prototype.hasOwnProperty.call(PLAIN, key) ? PLAIN[key] : null;
  }

  // ── Long-press detection on modifier chips ───────────────────────
  // 500 ms hold = lock; quick tap = arm. The synthetic click that fires
  // after pointerup is suppressed when a long-press already ran.
  function attachLongPress(chip, modName) {
    const HOLD_MS = 500;
    let timer = null;
    let didLongPress = false;
    let pressed = false;
    function start(e) {
      didLongPress = false;
      pressed = true;
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (!pressed) return;
        didLongPress = true;
        const nextState = mod[modName] === 'locked' ? 'off' : 'locked';
        setMod(modName, nextState);
        if (modName === 'tmux' && nextState === 'locked') sendToActive('\x02');
      }, HOLD_MS);
    }
    function cancel() {
      pressed = false;
      clearTimeout(timer);
    }
    chip.addEventListener('pointerdown',  start);
    chip.addEventListener('pointerup',    cancel);
    chip.addEventListener('pointercancel', cancel);
    chip.addEventListener('pointerleave', cancel);
    chip.addEventListener('click', (e) => {
      if (didLongPress) {
        e.preventDefault();
        e.stopPropagation();
        didLongPress = false;
        return;
      }
      const cur = mod[modName];
      if (cur === 'off') {
        setMod(modName, 'armed');
        // tmux: arming means "send prefix now, next key is the tmux command".
        if (modName === 'tmux') sendToActive('\x02');
      } else {
        setMod(modName, 'off');
      }
    });
  }
  bar.querySelectorAll('.ftk-chip-mod[data-mod]').forEach((chip) => {
    attachLongPress(chip, chip.dataset.mod);
  });

  // ── Non-modifier chip clicks ─────────────────────────────────────
  bar.addEventListener('click', async (e) => {
    const chip = e.target.closest('.ftk-chip');
    if (!chip) return;
    const key = chip.dataset.key;
    if (!key) return;
    // Modifier chips own their own click via attachLongPress.
    if (chip.classList.contains('ftk-chip-mod')) return;
    if (key === 'copy')   { await chipCopy();  consumeArm(); return; }
    if (key === 'ctrl-c') { await chipCopy();  consumeArm(); return; }
    if (key === 'paste')  { await chipPaste(); consumeArm(); return; }
    if (key === 'mic')   { toggleMic(chip);   consumeArm(); return; }
    // Shift+Tab inside a tmux session: route through `tmux send-keys` so it
    // reaches a TUI (Claude Code) that remaps modified keys under tmux's
    // extended-keys. The raw \e[Z we'd otherwise inject is silently ignored by
    // such apps. Falls back to the raw byte path if the tmux call fails or the
    // active tab isn't a tmux session.
    if (key === 'shift-tab' && mod.ctrl === 'off' && mod.tmux === 'off') {
      const tab = getActiveTerminalTab();
      if (tab && tab.tmuxSession) {
        consumeArm();
        _sendTmuxKey(tab.tmuxSession, 'shift-tab').catch(() => sendToActive('\x1b[Z'));
        try { if (tab.instance && tab.instance.focus) tab.instance.focus(); } catch (_) {}
        return;
      }
    }
    const bytes = chipBytes(key);
    if (bytes != null) {
      sendToActive(bytes);
      consumeArm();
    }
  });

  // ── Copy / Paste chips ───────────────────────────────────────────
  async function chipCopy() {
    const tab = getActiveTerminalTab();
    if (!tab || !tab.instance || !tab.instance.term) return;
    let text = '';
    try { text = tab.instance.term.getSelection() || ''; } catch (_) {}
    if (!text) {
      // No selection — grab the visible viewport so the user gets *something*.
      try {
        const t = tab.instance.term;
        const buf = t.buffer.active;
        const lines = [];
        const end = buf.viewportY + t.rows;
        for (let i = buf.viewportY; i < end; i++) {
          const line = buf.getLine(i);
          if (line) lines.push(line.translateToString(true));
        }
        text = lines.join('\n').replace(/\s+$/, '');
      } catch (_) {}
    }
    if (!text) return;
    try { await copyText(text); } catch (_) {}   // copyText: works in insecure contexts (phones)
  }
  async function chipPaste() {
    // Prefer the rich clipboard API so a pasted IMAGE (a screenshot, a copied
    // picture) is caught too — not just text. An image can't be "typed" into
    // the PTY text stream, so we hand it to _pasteImageBlob, which uploads it
    // to the server and types back the saved file's path; Claude Code (and any
    // program that reads an image path) then picks it up. We fall through to a
    // plain-text paste when there's no image, and to readText() when the rich
    // API is unavailable (older browser, or an insecure http:// context where
    // image-clipboard reads are blocked — see deployment.md "Secure-context").
    if (navigator.clipboard && typeof navigator.clipboard.read === 'function') {
      let items = null;
      try { items = await navigator.clipboard.read(); } catch (_) { items = null; }
      if (items) {
        for (const item of items) {
          const imgType = (item.types || []).find((t) => t.startsWith('image/'));
          if (imgType) {
            try {
              const blob = await item.getType(imgType);
              await _pasteImageBlob(blob, imgType);
            } catch (e) {
              alert('Could not paste image: ' + ((e && e.message) ? e.message : e));
            }
            return;
          }
        }
        // No image among the items — take text from them if present.
        for (const item of items) {
          if ((item.types || []).includes('text/plain')) {
            try {
              const blob = await item.getType('text/plain');
              const text = await blob.text();
              if (text) sendToActive(text);
            } catch (_) {}
            return;
          }
        }
      }
    }
    // Fallback: plain-text clipboard (older browsers, or read() blocked).
    let text = '';
    try { text = await navigator.clipboard.readText(); } catch (_) { return; }
    if (text) sendToActive(text);
  }

  // Upload a pasted image blob, then type the saved file's absolute path into
  // the active terminal. The path is double-quoted (it usually contains spaces,
  // e.g. "C:\Users\Alex R\...") and trailed with a space so the user can keep
  // typing their request after it. No Enter is sent.
  async function _pasteImageBlob(blob, mimeType) {
    if (!blob) return;
    const path = await uploadTerminalPasteImage(blob, mimeType);
    if (path) sendToActive('"' + path + '" ');
  }

  // ── Microphone dictation ────────────────────────────────────────
  // Web Speech API. One result is appended to the PTY input. Tap again to
  // stop. We don't auto-submit (no trailing \n) so the user can review
  // the recognised text before hitting Enter.
  let recognition = null;
  let listeningChip = null;
  function toggleMic(chip) {
    if (listeningChip === chip && recognition) {
      try { recognition.stop(); } catch (_) {}
      return;
    }
    const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SR) {
      alert('Speech recognition is not supported in this browser.');
      return;
    }
    recognition = new SR();
    recognition.continuous = false;
    recognition.interimResults = false;
    recognition.lang = navigator.language || 'en-US';
    recognition.onresult = (ev) => {
      const text = Array.from(ev.results).map((r) => r[0].transcript).join(' ');
      if (text) sendToActive(text);
    };
    const stopUi = () => {
      chip.dataset.listening = '';
      listeningChip = null;
      recognition = null;
    };
    recognition.onend = stopUi;
    recognition.onerror = stopUi;
    try {
      recognition.start();
      chip.dataset.listening = '1';
      listeningChip = chip;
    } catch (_) { /* InvalidStateError — already running */ }
  }

  // ── Soft-keyboard transform ──────────────────────────────────────
  // Lets the Ctrl chord work with characters the user types on the soft
  // keyboard (not just other chips). Wired into terminal.js via
  // window.__termInputTransform so we don't need a cross-module import.
  window.__termInputTransform = (data) => {
    if (mod.ctrl === 'off' && mod.tmux !== 'armed') return data;
    let out = '';
    if (mod.ctrl !== 'off') {
      for (let i = 0; i < data.length; i++) {
        const code = data.charCodeAt(i);
        if (code >= 0x61 && code <= 0x7a) out += String.fromCharCode(code - 0x60);       // a-z
        else if (code >= 0x41 && code <= 0x5a) out += String.fromCharCode(code - 0x40);  // A-Z
        else out += data[i];
      }
    } else {
      out = data;
    }
    consumeArm();
    return out;
  };
}

// ── Pinch-zoom on the terminal pane ───────────────────────────────
// Two fingers on the terminal content scale the font size. Uses the
// existing setTerminalFontSize (which propagates to every open terminal).
function initTerminalPinchZoom() {
  const host = document.getElementById('files-term-content');
  if (!host || host.dataset.pinchWired) return;
  host.dataset.pinchWired = '1';
  let initialDist = 0;
  let initialFontSize = 0;
  function dist(touches) {
    const dx = touches[0].clientX - touches[1].clientX;
    const dy = touches[0].clientY - touches[1].clientY;
    return Math.hypot(dx, dy);
  }
  host.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 2) return;
    initialDist = dist(e.touches);
    initialFontSize = getTerminalFontSize();
  }, { passive: true });
  host.addEventListener('touchmove', (e) => {
    if (e.touches.length !== 2 || initialDist <= 0) return;
    e.preventDefault(); // stop the page from pinch-zooming
    const ratio = dist(e.touches) / initialDist;
    setTerminalFontSize(Math.round(initialFontSize * ratio));
  }, { passive: false });
  function release(e) {
    if (e.touches.length < 2) initialDist = 0;
  }
  host.addEventListener('touchend',    release, { passive: true });
  host.addEventListener('touchcancel', release, { passive: true });
}

// ── Visual viewport refit ─────────────────────────────────────────
// When the mobile soft keyboard opens/closes, the visual viewport shrinks
// or grows. xterm sees its host height change only on a window resize,
// not on visualViewport resize, so the prompt ends up under the keyboard.
// Refit the active terminal each time the viewport changes.
function initTerminalViewportRefit() {
  const vv = window.visualViewport;
  if (!vv || vv.dataset && vv.dataset.refitWired) return;
  const handler = () => {
    const tab = getActiveTerminalTab();
    if (tab && tab.instance) setTimeout(() => tab.instance.fit(), 60);
  };
  vv.addEventListener('resize', handler);
  vv.addEventListener('scroll', handler);
}

// ── Swipe between terminal tabs ───────────────────────────────────
// Horizontal swipe on the tab strip switches to the previous / next
// terminal tab. Threshold + axis check keep accidental vertical
// scrolls from triggering a switch.
function initTerminalTabSwipe() {
  const strip = document.getElementById('files-term-tabs');
  if (!strip || strip.dataset.swipeWired) return;
  strip.dataset.swipeWired = '1';
  let startX = 0, startY = 0, t0 = 0;
  strip.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    startX = e.touches[0].clientX;
    startY = e.touches[0].clientY;
    t0 = Date.now();
  }, { passive: true });
  strip.addEventListener('touchend', (e) => {
    if (e.changedTouches.length !== 1) return;
    const dx = e.changedTouches[0].clientX - startX;
    const dy = e.changedTouches[0].clientY - startY;
    if (Date.now() - t0 > 500) return;
    if (Math.abs(dx) < 60) return;
    if (Math.abs(dy) > Math.abs(dx)) return;
    const termTabs = termTabs.filter((t) => t.kind === 'terminal');
    if (termTabs.length < 2) return;
    const cur = termTabs.findIndex((t) => t.path === activeTerminalId);
    if (cur < 0) return;
    const next = dx < 0
      ? (cur + 1) % termTabs.length
      : (cur - 1 + termTabs.length) % termTabs.length;
    activateTermTab(termTabs[next].path);
  }, { passive: true });
}


// (The global header "restart & reload" button was removed — restart is still
// available via #btn-restart in the chat header and #ft-refresh in the terminal
// sidebar. Removing it lets initFiles() be deferred to first Admin Tools open.)

// ── In-page terminal tabs ──────────────────────────────────────────
//
// Each click of the "new terminal" button in the sidebar pushes a fresh
// tab with kind === 'terminal' and spawns its own xterm + PTY WebSocket.
// Terminal tabs sit alongside file tabs in the same tab bar and share the
// same activate / close / drag-reorder machinery.

function _connStateTitle(s) {
  return s === 'connected'    ? 'Connected'
       : s === 'reconnecting' ? 'Reconnecting…'
       : s === 'error'        ? 'Disconnected — refresh to retry'
       :                        'Connecting…';
}

function _updateTabConnDot(tabPath, state) {
  // Connection dots live on terminal tabs, which render into the terminal
  // tab bar; fall back to the file bar for safety.
  const tabEl =
    document.querySelector('#files-term-tabs .files-tab[data-path="' + cssEscape(tabPath) + '"]') ||
    document.querySelector('#files-tabs .files-tab[data-path="' + cssEscape(tabPath) + '"]');
  if (!tabEl) return;
  const dot = tabEl.querySelector('.files-tab-conn-dot');
  if (!dot) return;
  dot.dataset.state = state;
  dot.title = _connStateTitle(state);
}

function newTerminalSessionId() {
  return 'terminal:' + randomUUID();
}

// ── Mobile long-press → Copy / Paste menu ─────────────────────────
//
// xterm.js doesn't natively expose a touch-friendly copy/paste UI.
// We watch for a still touch held >500ms on the terminal host and pop a
// small floating menu near the touch point. Cancelled on touchmove /
// touchcancel so it doesn't fight with the user's scrolling or selection.

const LONG_PRESS_MS = 500;
const LONG_PRESS_MOVE_TOLERANCE = 8;  // px — touchmove farther than this aborts

function wireTerminalLongPress(host, getInstance) {
  let timer = null;
  let startX = 0;
  let startY = 0;

  function cancel() {
    if (timer !== null) {
      clearTimeout(timer);
      timer = null;
    }
  }

  host.addEventListener('touchstart', (e) => {
    // Multi-touch (e.g. pinch-to-zoom) cancels the long-press intent.
    if (!e.touches || e.touches.length !== 1) { cancel(); return; }
    const t = e.touches[0];
    startX = t.clientX;
    startY = t.clientY;
    cancel();
    timer = setTimeout(() => {
      timer = null;
      const inst = getInstance && getInstance();
      if (inst) showTerminalContextMenu(startX, startY, inst);
    }, LONG_PRESS_MS);
  }, { passive: true });

  host.addEventListener('touchmove', (e) => {
    if (!e.touches || e.touches.length !== 1) { cancel(); return; }
    const t = e.touches[0];
    if (Math.abs(t.clientX - startX) > LONG_PRESS_MOVE_TOLERANCE ||
        Math.abs(t.clientY - startY) > LONG_PRESS_MOVE_TOLERANCE) {
      cancel();
    }
  }, { passive: true });

  host.addEventListener('touchend',    cancel, { passive: true });
  host.addEventListener('touchcancel', cancel, { passive: true });
}

// ── Mobile pinch-to-zoom ──────────────────────────────────────────
//
// Two-finger pinch on the terminal host adjusts the global font size.
// Uses the same setter as the menu's Zoom in / out items so the new size
// propagates to every open terminal and persists in localStorage.

function wireTerminalPinchZoom(host) {
  let startDist = 0;
  let startFontSize = 0;
  let lastApplied = 0;

  function dist(t0, t1) {
    const dx = t0.clientX - t1.clientX;
    const dy = t0.clientY - t1.clientY;
    return Math.hypot(dx, dy);
  }

  host.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length < 2) return;
    // Prevent the OS-level pinch zoom from kicking in on top of ours.
    if (e.cancelable) e.preventDefault();
    startDist = dist(e.touches[0], e.touches[1]);
    startFontSize = getTerminalFontSize();
    lastApplied = startFontSize;
  }, { passive: false });

  host.addEventListener('touchmove', (e) => {
    if (!e.touches || e.touches.length < 2 || startDist === 0) return;
    if (e.cancelable) e.preventDefault();
    const d = dist(e.touches[0], e.touches[1]);
    if (d === 0) return;
    const ratio = d / startDist;
    let next = Math.round(startFontSize * ratio);
    if (next < 8) next = 8;
    if (next > 32) next = 32;
    if (next !== lastApplied) {
      setTerminalFontSize(next);
      lastApplied = next;
    }
  }, { passive: false });

  function end() { startDist = 0; }
  host.addEventListener('touchend',    end, { passive: true });
  host.addEventListener('touchcancel', end, { passive: true });
}

// ── Mobile drag-to-scroll ─────────────────────────────────────────
//
// One-finger vertical drag pans the terminal scrollback. xterm has no
// built-in touch panning and there's no scroll wheel on touch devices, so
// without this you can't reach earlier output on a phone/tablet. We convert
// the finger's pixel travel into whole rows and feed them to xterm's
// scrollLines (negative = toward the top). Single-finger only — two-finger
// gestures belong to pinch-zoom, and a stationary hold belongs to the
// long-press menu (both wired separately on the same host).
function wireTerminalDragScroll(host, getInstance) {
  let active = false;
  let lastY = 0;
  let accumRows = 0;   // fractional rows carried between moves

  function rowHeightPx(term) {
    const rows = term.rows || 24;
    const h = (term.element && term.element.clientHeight) || host.clientHeight || (rows * 17);
    return rows > 0 ? h / rows : 17;
  }

  host.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length !== 1) { active = false; return; }
    active = true;
    lastY = e.touches[0].clientY;
    accumRows = 0;
  }, { passive: true });

  host.addEventListener('touchmove', (e) => {
    if (!active || !e.touches || e.touches.length !== 1) { active = false; return; }
    const inst = getInstance && getInstance();
    const term = inst && inst.term;
    if (!term) return;
    const y = e.touches[0].clientY;
    const dy = y - lastY;
    lastY = y;
    const px = rowHeightPx(term);
    if (px <= 0) return;
    accumRows += dy / px;
    const rows = Math.trunc(accumRows);
    if (rows !== 0) {
      accumRows -= rows;
      // Finger moving down reveals earlier content → scroll toward the top
      // (negative). scrollLines is positive-down, hence the sign flip.
      try { term.scrollLines(-rows); } catch (_) {}
      // Once we're actually scrolling, swallow the gesture so xterm doesn't
      // start a text selection and the page doesn't rubber-band underneath.
      if (e.cancelable) e.preventDefault();
    }
  }, { passive: false });

  const end = () => { active = false; };
  host.addEventListener('touchend',    end, { passive: true });
  host.addEventListener('touchcancel', end, { passive: true });
}

function showTerminalContextMenu(x, y, instance) {
  closeTerminalContextMenu();
  const menu = document.createElement('div');
  menu.className = 'files-terminal-ctxmenu';
  menu.id = 'files-terminal-ctxmenu';

  function btn(label, icon, action, disabled) {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'files-terminal-ctxmenu-item' + (disabled ? ' disabled' : '');
    b.disabled = !!disabled;
    b.innerHTML = '<i data-lucide="' + icon + '" class="lucide-icon"></i><span>' + label + '</span>';
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      closeTerminalContextMenu();
      if (!disabled) action();
    });
    return b;
  }

  const hasSel = !!(instance && instance.term && instance.term.hasSelection && instance.term.hasSelection());

  menu.appendChild(btn('Copy',       'copy',      async () => {
    try {
      const text = (instance.term.getSelection && instance.term.getSelection()) || '';
      if (text) await copyText(text);   // copyText: works in insecure contexts (phones)
    } catch (_) {}
    try { instance.focus(); } catch (_) {}
  }, !hasSel));

  menu.appendChild(btn('Paste',      'clipboard', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text && instance.paste) instance.paste(text);
    } catch (_) {}
    try { instance.focus(); } catch (_) {}
  }));

  menu.appendChild(btn('Select all', 'list',      () => {
    try { instance.term.selectAll && instance.term.selectAll(); } catch (_) {}
  }));

  menu.appendChild(btn('Clear',      'trash-2',   () => {
    try { instance.term.clear && instance.term.clear(); } catch (_) {}
    try { instance.focus(); } catch (_) {}
  }));

  document.body.appendChild(menu);

  // Clamp into viewport.
  const rect = menu.getBoundingClientRect();
  const left = Math.max(8, Math.min(window.innerWidth  - rect.width  - 8, x - rect.width / 2));
  const top  = Math.max(8, Math.min(window.innerHeight - rect.height - 8, y - rect.height - 8));
  menu.style.left = left + 'px';
  menu.style.top  = top  + 'px';

  _refreshLucideIcons(menu);

  // Close on any tap / click outside, or on scroll. Touchstart capture so
  // we catch the gesture before xterm processes it as a new selection.
  const outside = (ev) => {
    if (!menu.contains(ev.target)) closeTerminalContextMenu();
  };
  setTimeout(() => {
    document.addEventListener('mousedown',  outside, true);
    document.addEventListener('touchstart', outside, true);
    document.addEventListener('scroll',     closeTerminalContextMenu, true);
  }, 0);
  menu._outsideHandler = outside;
}

function closeTerminalContextMenu() {
  const menu = document.getElementById('files-terminal-ctxmenu');
  if (!menu) return;
  if (menu._outsideHandler) {
    document.removeEventListener('mousedown',  menu._outsideHandler, true);
    document.removeEventListener('touchstart', menu._outsideHandler, true);
  }
  document.removeEventListener('scroll', closeTerminalContextMenu, true);
  menu.remove();
}

// ── Drag a file from the tree onto a terminal pane ────────────────
// Quote a path so it can be safely pasted at a POSIX shell prompt: bare
// when only safe chars, single-quoted otherwise (with embedded ' escaped).
function shellQuote(s) {
  if (s == null) return '';
  s = String(s);
  if (/^[a-zA-Z0-9._\/\-+=:@,]+$/.test(s)) return s;
  return "'" + s.replace(/'/g, "'\\''") + "'";
}

// ── In-terminal find bar (xterm search addon) ─────────────────────

function buildTerminalFindBar() {
  const bar = document.createElement('div');
  bar.className = 'files-terminal-findbar';
  bar.hidden = true;
  bar.innerHTML =
    '<input type="text" class="files-terminal-findbar-input" placeholder="Find in terminal" spellcheck="false" autocomplete="off" data-lpignore="true" data-1p-ignore="true">' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="case" title="Match case">Aa</button>' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="prev" title="Previous (Shift+Enter)"><i data-lucide="chevron-up" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="next" title="Next (Enter)"><i data-lucide="chevron-down" class="lucide-icon"></i></button>' +
    '<span class="files-terminal-findbar-status"></span>' +
    '<button type="button" class="files-terminal-findbar-btn" data-act="close" title="Close (Esc)"><i data-lucide="x" class="lucide-icon"></i></button>';
  return bar;
}

function openTerminalFindBar(bar, instance) {
  if (!bar || !instance) return;
  bar.hidden = false;
  _refreshLucideIcons(bar);
  const input = bar.querySelector('.files-terminal-findbar-input');
  const status = bar.querySelector('.files-terminal-findbar-status');
  const caseBtn = bar.querySelector('[data-act="case"]');

  // Listeners are wired once per bar. Subsequent opens just refocus.
  if (!bar.dataset.wired) {
    bar.dataset.wired = '1';
    bar._caseSensitive = false;

    function find(dir) {
      const q = input.value;
      if (!q) { status.textContent = ''; return; }
      const opts = { caseSensitive: bar._caseSensitive };
      const ok = dir === 'prev'
        ? instance.findPrevious(q, opts)
        : instance.findNext(q, opts);
      status.textContent = ok ? '' : 'No match';
    }
    function close() {
      bar.hidden = true;
      try { instance.clearSearch(); } catch (_) {}
      instance.focus();
    }
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); find(e.shiftKey ? 'prev' : 'next'); }
      else if (e.key === 'Escape') { e.preventDefault(); close(); }
    });
    input.addEventListener('input', () => find('next'));
    bar.querySelector('[data-act="next"]').addEventListener('click', () => find('next'));
    bar.querySelector('[data-act="prev"]').addEventListener('click', () => find('prev'));
    bar.querySelector('[data-act="close"]').addEventListener('click', close);
    caseBtn.addEventListener('click', () => {
      bar._caseSensitive = !bar._caseSensitive;
      caseBtn.classList.toggle('active', bar._caseSensitive);
      find('next');
    });
  }

  input.focus();
  input.select();
}

function startInlineRename(tab, labelEl) {
  // Swap the label for a text input; commit on Enter/blur, cancel on Esc.
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'files-tab-rename-input';
  input.value = tab.name;
  input.spellcheck = false;
  input.maxLength = 60;

  let committed = false;
  function finish(save) {
    if (committed) return;
    committed = true;
    const v = input.value.trim();
    if (save && v && v !== tab.name) {
      tab.name = v;
      persistTermTabs();
      // For terminal tabs, cache the name locally AND push it to the server
      // so the "Your sessions" list on other devices shows the new label.
      if (tab.kind === 'terminal') {
        try { setTerminalSessionName(tab.path, v); } catch (_) {}
        if (tab.instance && typeof tab.instance.setName === 'function') {
          try { tab.instance.setName(v); } catch (_) {}
        }
      }
    }
    renderTermTabs();   // rebuild — swaps the input back to a span
  }
  // ╔═╗ RENAME-FIELD PATTERN  ════════════════════════════════════════════════════╗
  // ║ Inline rename: create <input>, replace label, Enter/Escape/blur commit.    ║
  // ║ Duplicated in sessions.js (startRename & _headerRenameSession) and           ║
  // ║ genui.js (_startRenamePage). Mirror fixes across all copies.            ║
  // ╚══════════════════════════════════════════════════════════════════════════════╝
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  // Prevent the parent tab's click/drag handlers from firing while editing.
  input.addEventListener('mousedown', (e) => e.stopPropagation());
  input.addEventListener('click', (e) => e.stopPropagation());

  labelEl.replaceWith(input);
  input.focus();
  input.select();
}

// Extract the tmux SESSION NAME a terminal tab is driving, parsed from its
// launch command (`tmux new -As <name>`, `tmux attach -t <name>`, etc.). Empty
// for a plain shell or a non-session tmux command (`tmux ls`). The keybar uses
// this so it can deliver Shift+Tab through `tmux send-keys` — see _sendTmuxKey.
function _parseTmuxSession(cmd) {
  if (!cmd) return '';
  // Lazily skip from `tmux` to the first session selector (-s / -As / -t),
  // then grab the session name. The name may carry an EMBEDDED single quote in
  // the resume launcher (`tmux new -As claude-'<sid>' …`), so we include the
  // quote in the captured token and strip every quote afterwards — otherwise the
  // match stops at the inner quote and yields a truncated name (`claude-`) that
  // no live session matches, breaking `tmux send-keys` (e.g. Shift+Tab).
  const m = /\btmux\b[\s\S]*?\s-(?:A?s|t)\s+([A-Za-z0-9_.'-]+)/.exec(cmd);
  return m ? m[1].replace(/'/g, '') : '';
}

function pushTerminalTab(sessionId, name, opts) {
  opts = opts || {};
  termTabs.push({
    // The tab path doubles as the backend session_id — terminal tabs use a
    // 'terminal:<uuid>' prefix that can't collide with real file paths.
    path: sessionId,
    name: name || ('Terminal ' + (termTabs.filter((t) => t.kind === 'terminal').length + 1)),
    kind: 'terminal',
    instance: null,           // set by buildPaneForTab once xterm is opened
    dirty: false,
    binary: false,
    // If set, typed into the shell once on first WS open. Not persisted —
    // persistTermTabs() strips it so reloads don't re-run the command.
    initialCommand: opts.initialCommand || '',
    // For "Resume Claude" tabs: the Claude conversation id this tab is resuming.
    // Passed up on the WS so the backend marks that conversation "already open"
    // (a second click on the quick-launch then skips to the next-newest). Not
    // persisted — but the backend session keeps its own copy across reloads.
    claudeSessionId: opts.claudeSessionId || '',
    // For a "Saved servers" SSH launcher: the deploy server id this tab connects
    // to. Passed up on the WS (?ssh_server_id=) so the backend opens an SSH shell
    // to that server. Persisted so a reload can respawn it if the backend session
    // died (a live one just reattaches). Empty for ordinary terminal tabs.
    sshServerId: opts.sshServerId || '',
    // tmux session this tab is attached to (if any). Lets the keybar route
    // Shift+Tab via `tmux send-keys` so it reaches modern TUIs (Claude Code)
    // that remap modified keys under tmux's extended-keys. Persisted so it
    // survives a reload even though initialCommand isn't.
    tmuxSession: opts.tmuxSession || _parseTmuxSession(opts.initialCommand),
  });
}

function openNewTerminalTab() {
  const id = newTerminalSessionId();
  pushTerminalTab(id);
  activeTerminalId = id;
  renderTermTabs();
  renderTermPanes();
  persistTermTabs();
  // Land the user on the terminal main so the freshly-opened session is
  // actually visible. Same call as a click on the zap strip icon.
  window.__applySidebarView('terminal');
  kvWrite(KC_SIDEBAR_VIEW, LS_SIDEBAR_VIEW, 'terminal');
}

// Like openNewTerminalTab but pre-types `command` into the shell. Used by
// the sidebar's quick-launch and tmux-session lists. `opts.claudeSessionId`
// tags the tab with a Claude conversation id (see "Resume Claude").
function openNewTerminalTabWithCommand(command, name, opts) {
  opts = opts || {};
  const id = newTerminalSessionId();
  pushTerminalTab(id, name, {
    initialCommand: command || '',
    claudeSessionId: opts.claudeSessionId || '',
  });
  activeTerminalId = id;
  renderTermTabs();
  renderTermPanes();
  persistTermTabs();
  _switchToTerminalStrip();
}

// Open a terminal tab that SSHes into a saved deploy server. The backend
// connects over the login stored for that server in the Deploy panel (see
// /api/v1/terminal/ssh-servers → TerminalSession.spawn_ssh). Confirms first,
// since this drops you into a live — and often root — shell on a real server.
function openSshTerminalTab(server) {
  if (!server || !server.id) return;
  const who = (server.ssh_user || 'root') + '@' + (server.host || '?');
  const label = server.label || who;
  const ok = window.confirm(
    'Open an SSH terminal to ' + who + '?\n\n' +
    'This connects using the login saved for this server in the Deploy panel.');
  if (!ok) return;
  const id = newTerminalSessionId();
  pushTerminalTab(id, label, { sshServerId: server.id });
  activeTerminalId = id;
  renderTermTabs();
  renderTermPanes();
  persistTermTabs();
  _switchToTerminalStrip();
}

function _switchToTerminalStrip() {
  window.__applySidebarView('terminal');
  kvWrite(KC_SIDEBAR_VIEW, LS_SIDEBAR_VIEW, 'terminal');
  const sidebar = document.getElementById('files-sidebar');
  if (sidebar && isMobileLayout() && sidebar.dataset.state === 'max') {
    app.setSidebarState('strip');
  }
}


export function reconnectAllTerminals() {
  for (const t of termTabs) {
    if (t.kind === 'terminal' && t.instance && !t.closing) {
      try { t.instance.reconnect(); } catch (_) {}
    }
  }
}


// ── Terminal launchers sidebar panel ──────────────────────────────
//
// Three sections (quick launches, unified session list, static hints)
// in the terminal launcher sidebar. Quick launches come from
// /api/v1/terminal/quick-launches and are essentially static for the
// lifetime of the page; the session list (PTY + tmux) is polled every
// 5s while the panel is visible.

let _ftQuickLaunchesLoaded = false;
let _ftTmuxPollTimer = null;

// Friendly names for terminal sessions, scoped to the current user. Sent on
// WS open via the `name` query param so other devices see a useful label in
// the "Your sessions" list instead of the raw UUID.
const LS_TERM_SESSION_NAMES = 'files.terminalSessionNames';
function _loadTermNames() {
  return kvRead(KC_TERM_SESSION_NAMES, LS_TERM_SESSION_NAMES) || {};
}
function _saveTermNames(map) {
  kvWrite(KC_TERM_SESSION_NAMES, LS_TERM_SESSION_NAMES, map);
}
function getTerminalSessionName(sessionId) {
  if (!sessionId) return '';
  return _loadTermNames()[sessionId] || '';
}
function setTerminalSessionName(sessionId, name) {
  if (!sessionId) return;
  const map = _loadTermNames();
  if (name) map[sessionId] = name;
  else delete map[sessionId];
  _saveTermNames(map);
}

function ftEscapeHtml(s) {
  return String(s || '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function ftRenderLaunches(items) {
  const host = document.getElementById('ft-list-launches');
  if (!host) return;
  if (!items || !items.length) {
    host.innerHTML = '<div class="ft-empty">No quick launches configured</div>';
    return;
  }
  host.innerHTML = '';
  for (const it of items) {
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ft-row';
    const ic = it.icon || 'terminal';
    row.innerHTML = '<i data-lucide="' + ftEscapeHtml(ic) + '" class="lucide-icon ft-row-icon"></i>' +
                    '<span class="ft-row-label">' + ftEscapeHtml(it.name) + '</span>';
    if (it.action === 'claude-resume') {
      // Dynamic launcher: the exact command (which conversation to resume) is
      // resolved server-side at click time, so this row carries an `action`
      // instead of a static `command`. Tap = resume in a plain terminal;
      // long-press = resume inside tmux. See ftLaunchClaudeResume.
      row.title = 'Tap: resume your most recent Claude conversation in a terminal. Long-press: resume in tmux.';
      _ftWireLaunchRow(row,
        () => ftLaunchClaudeResume(row, false),
        () => ftLaunchClaudeResume(row, true),
        it.name);
    } else {
      // A short tap opens the plain `command` in a normal terminal window; a
      // long-press opens the tmux variant (`tmux_command`) when one exists, so
      // the session survives tab close/refresh. Rows without a distinct tmux
      // variant (attach/ls/plain shell) do the same thing either way.
      const plain = it.command || '';
      const tmuxCmd = it.tmux_command || '';
      const hasTmux = !!tmuxCmd && tmuxCmd !== plain;
      row.title = plain ? ('Run: ' + plain) : 'Open a plain shell';
      if (hasTmux) row.title += '  ·  Long-press to run in tmux';
      _ftWireLaunchRow(row,
        () => openNewTerminalTabWithCommand(plain, it.name || undefined),
        hasTmux ? () => openNewTerminalTabWithCommand(tmuxCmd, it.name || undefined) : null,
        it.name);
    }
    host.appendChild(row);
  }
  _refreshLucideIcons(host);
}

// Render the "Saved servers (SSH)" launcher from /api/v1/terminal/ssh-servers.
// Each row is a saved deploy server; a click opens an SSH terminal to it via
// openSshTerminalTab. Servers with no stored login are shown dimmed + disabled
// with a hint (a click would just fail server-side). The whole section hides
// when there are no saved servers, so a fresh install shows nothing extra.
function ftRenderSshServers(items) {
  const host = document.getElementById('ft-list-ssh');
  const section = document.getElementById('ft-section-ssh');
  if (!host) return;
  const servers = Array.isArray(items) ? items : [];
  if (!servers.length) {
    if (section) section.hidden = true;
    host.innerHTML = '';
    return;
  }
  if (section) section.hidden = false;
  host.innerHTML = '';
  for (const s of servers) {
    const who = (s.ssh_user || 'root') + '@' + (s.host || '?');
    const label = s.label || who;
    const configured = s.configured !== false;
    const row = document.createElement('button');
    row.type = 'button';
    row.className = 'ft-row';
    // Only the label carries the (host-derived) name; the user@host goes in the
    // faint meta slot so the row reads like "Prod  ubuntu@203.0.113.10".
    let inner = '<i data-lucide="server" class="lucide-icon ft-row-icon"></i>' +
                '<span class="ft-row-label">' + ftEscapeHtml(label) + '</span>';
    if (who !== label) inner += '<span class="ft-row-meta">' + ftEscapeHtml(who) + '</span>';
    row.innerHTML = inner;
    if (configured) {
      row.title = 'SSH into ' + who + ' — connects with the saved login';
      row.addEventListener('click', () => openSshTerminalTab(s));
    } else {
      row.disabled = true;
      row.title = who + ' — no SSH login saved. Add a key or password in ' +
                  'App Settings → Deploy, then it becomes clickable.';
    }
    host.appendChild(row);
  }
  _refreshLucideIcons(host);
}

async function ftLoadSshServers() {
  await _ftFetchAndRender('/api/v1/terminal/ssh-servers', ftRenderSshServers, 'ft-list-ssh');
}

// Wire a quick-launch row for tap vs long-press. `onShort` fires on a normal
// tap; `onLong` (optional) fires after a ≥500ms hold and flashes a small "in
// tmux" confirmation. When there's no `onLong`, the row is a plain click.
// Mirrors the "+" new-tab button's long-press pattern (see initTermCarousel):
// the long-press sets `lpFired` so the trailing synthetic click is swallowed.
function _ftWireLaunchRow(row, onShort, onLong, name) {
  if (!onLong) { row.addEventListener('click', onShort); return; }
  let lpTimer = null;
  let lpFired = false;
  const LP_MS = 500;
  const clearLp = () => { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } };
  row.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.pointerType === 'mouse') return;  // primary button / touch / pen only
    lpFired = false;
    clearLp();
    lpTimer = setTimeout(() => {
      lpTimer = null;
      lpFired = true;
      onLong();
      _flashTermPop(row, (name ? name + ' · ' : '') + 'in tmux');
    }, LP_MS);
  });
  row.addEventListener('pointerup', clearLp);
  row.addEventListener('pointerleave', clearLp);
  row.addEventListener('pointercancel', clearLp);
  // Suppress the OS context menu a touch/pen long-press would otherwise pop.
  row.addEventListener('contextmenu', (e) => e.preventDefault());
  row.addEventListener('click', (e) => {
    if (lpFired) { lpFired = false; e.preventDefault(); e.stopPropagation(); return; }
    onShort();
  });
}

// "Resume Claude" quick-launch. Unlike the static launches, the command is
// resolved on the server at click time (/api/v1/terminal/claude-resume-target):
// it returns the newest Claude conversation that isn't already open in a
// running `claude`, as a ready-to-run shell command — or a fresh `claude` when
// there's nothing to resume. We then open it like any other quick-launch.
async function ftLaunchClaudeResume(row, useTmux) {
  if (row && row.dataset.busy === '1') return;   // ignore double-taps mid-lookup
  if (row) row.dataset.busy = '1';
  try {
    const target = await apiFetch('/api/v1/terminal/claude-resume-target');
    // Tap resumes in a plain terminal (`command`); long-press resumes inside a
    // named tmux session (`tmux_command`) so it survives tab close/refresh.
    const cmd = (useTmux && target && target.tmux_command)
      ? target.tmux_command
      : ((target && target.command) || 'claude');
    const name = (target && target.name) || 'Claude';
    // Tag the tab with the resumed conversation id so the backend counts it as
    // open — clicking the button again then skips it and resumes the next-newest
    // (the whole point of this fix; works on Windows where /proc detection can't).
    openNewTerminalTabWithCommand(cmd, name, {
      claudeSessionId: (target && target.session_id) || '',
    });
  } catch (e) {
    // Endpoint unreachable (e.g. server not yet restarted after this change) —
    // still do the useful thing and launch a fresh Claude so the button never
    // dead-ends.
    console.warn('Resume Claude lookup failed, launching fresh:', (e && e.message) || e);
    openNewTerminalTabWithCommand('claude', 'Claude');
  } finally {
    if (row) row.dataset.busy = '';
  }
}

// ── Unified session list renderer (PTY + tmux) ────────────────────
//
// Renders both PTY terminal sessions and tmux sessions as one unified
// list. PTY rows use a `square-terminal` icon; tmux rows use a `layers`
// icon (same as the "New tmux session" button), replacing the old
// "tmux: " text prefix.

// Append tmux's mouse-mode toggle to a launch/attach command so the
// browser's scroll wheel is forwarded to a mouse-aware TUI (Claude) in
// the pane — which scrolls its OWN message history — instead of tmux's
// default (mouse off), where the wheel becomes cursor-up/down keys that
// scroll Claude's prompt-history. `-g` is server-global and idempotent,
// so it's safe on every command; `';'` is a tmux command separator wrapped
// in single quotes so it survives both bash/WSL and PowerShell (PowerShell
// treats `;` as a statement separator, so `\;` breaks there).
// Mirrors `_TMUX_MOUSE_ON` in app/api/terminal.py.
function _ftTmuxMouseOn(cmd) {
  return cmd + " ';' set -g mouse on";
}

function ftRenderAllSessions(sessions, tmuxItems) {
  const host = document.getElementById('ft-list-sessions');
  if (!host) return;

  // Combine: tag tmux items for the render loop
  const combined = [];

  // PTY sessions (from /api/v1/terminal/sessions)
  const live = (sessions || []).filter((s) => s && s.alive !== false);
  // Sort: attached first, then by age desc
  live.sort((a, b) => {
    const aAtt = (a.attached_clients || 0) > 0 ? 1 : 0;
    const bAtt = (b.attached_clients || 0) > 0 ? 1 : 0;
    if (aAtt !== bAtt) return bAtt - aAtt;
    return (b.age_secs || 0) - (a.age_secs || 0);
  });
  for (const s of live) combined.push({ kind: 'pty', data: s });

  // Tmux sessions (from /api/v1/terminal/tmux-sessions)
  // Filter out any that are already represented as PTY sessions
  // (named sessions that also show up as alive PTYs)
  for (const t of (tmuxItems || [])) {
    if (!t || !t.name) continue;
    // If there's already a PTY entry with a matching name/label, skip it
    const hasPty = live.some((s) => {
      const label = _sessionLabel(s);
      return label === t.name || s.name === t.name;
    });
    if (!hasPty) combined.push({ kind: 'tmux', data: t });
  }

  if (!combined.length) {
    host.innerHTML = '<div class="ft-empty">No running sessions</div>';
    return;
  }

  host.innerHTML = '';
  for (const item of combined) {
    if (item.kind === 'pty') {
      _renderPtyRow(item.data, host);
    } else {
      _renderTmuxRow(item.data, host);
    }
  }
  _refreshLucideIcons(host);
}

function _renderPtyRow(s, host) {
  const label = _sessionLabel(s);
  const attached = (s.attached_clients || 0) > 0;
  const isAgent = !!s.agent_driven;
  const isSsh = !!s.ssh;
  const row = document.createElement('div');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.className = 'ft-row';
  row.title = attached
    ? 'Open: ' + label + ' (attached on ' + s.attached_clients + ' client' + (s.attached_clients === 1 ? '' : 's') + ')'
    : 'Reattach: ' + label + (s.idle_secs != null ? ' (idle ' + _fmtIdle(s.idle_secs) + ')' : '');
  const dotCls = attached ? 'ft-row-dot ft-row-dot-on' : 'ft-row-dot';
  const meta = isAgent
    ? (s.launch_command || '')
    : (attached ? '' : (s.idle_secs != null ? _fmtIdle(s.idle_secs) + ' idle' : ''));
  const icon = isAgent ? 'bot' : (isSsh ? 'server' : 'square-terminal');
  const chip = isAgent
    ? '<span class="ft-agent-chip" title="Opened and driven by an agent">AGENT</span>'
    : (isSsh ? '<span class="ft-agent-chip" title="Interactive SSH shell to a saved server">SSH</span>' : '');
  row.innerHTML =
    '<i data-lucide="' + icon + '" class="lucide-icon ft-row-icon"></i>' +
    '<span class="ft-row-label">' + ftEscapeHtml(label) + '</span>' +
    chip +
    (meta ? '<span class="ft-row-meta">' + ftEscapeHtml(meta) + '</span>' : '') +
    '<span class="' + dotCls + '" title="' + (attached ? 'attached' : 'detached') + '"></span>';
  const open = () => openOrAttachTerminalSession(s.session_id, label, s.ssh_server_id || '');
  row.addEventListener('click', open);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); }
  });
  if (isAgent) {
    const pause = document.createElement('button');
    pause.type = 'button';
    pause.className = 'ft-pause-btn' + (s.paused ? ' ft-pause-btn-on' : '');
    pause.textContent = s.paused ? 'Resume' : 'Pause';
    pause.title = s.paused
      ? 'Resume: let the agent drive this terminal again'
      : 'Pause the agent so you can take over typing';
    pause.addEventListener('click', async (e) => {
      e.stopPropagation();
      pause.disabled = true;
      try {
        await ftSetSessionPaused(s.session_id, !s.paused);
        ftLoadAllSessions();
      } catch (err) {
        pause.disabled = false;
      }
    });
    row.appendChild(pause);
  }
  // 3-dot "more" menu — rename or delete this session
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'ft-row-more';
  more.title = 'More — rename or delete';
  more.setAttribute('aria-label', 'Session actions');
  more.innerHTML = '<i data-lucide="more-vertical" class="lucide-icon"></i>';
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    _ftShowSessionMenu(s, label, more);
  });
  more.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
  });
  row.appendChild(more);
  host.appendChild(row);
}

function _renderTmuxRow(t, host) {
  // Use a div (role=button) so it can host child buttons (3-dot menu).
  const row = document.createElement('div');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.className = 'ft-row';
  row.title = "Attach: tmux attach -t '" + (t.name || '') + "'";
  const dot = t.attached ? 'ft-row-dot ft-row-dot-on' : 'ft-row-dot';
  row.innerHTML =
    '<i data-lucide="layers" class="lucide-icon ft-row-icon"></i>' +
    '<span class="ft-row-label">' + ftEscapeHtml(t.name) + '</span>' +
    '<span class="ft-row-meta">' + ftEscapeHtml(t.windows + (t.windows === 1 ? ' win' : ' wins')) + '</span>' +
    '<span class="' + dot + '" title="' + (t.attached ? 'attached' : 'detached') + '"></span>';
  const attach = () => {
    const cmd = _ftTmuxMouseOn("tmux attach -t '" + String(t.name).replace(/'/g, "'\\''") + "'");
    openNewTerminalTabWithCommand(cmd, t.name);
  };
  row.addEventListener('click', attach);
  row.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); attach(); }
  });

  // 3-dot "more" menu — rename or kill this tmux session
  const more = document.createElement('button');
  more.type = 'button';
  more.className = 'ft-row-more';
  more.title = 'More — rename or kill';
  more.setAttribute('aria-label', 'Tmux session actions');
  more.innerHTML = '<i data-lucide="more-vertical" class="lucide-icon"></i>';
  more.addEventListener('click', (e) => {
    e.stopPropagation();
    _ftShowTmuxSessionMenu(t, more);
  });
  more.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') e.stopPropagation();
  });
  row.appendChild(more);
  host.appendChild(row);
}

// 3-dot row menu for a tmux session entry: rename + kill (two-click hazard in-menu).
function _ftShowTmuxSessionMenu(t, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  openFloatingMenu([
    { icon: 'pencil', label: 'Rename…', action: () => _ftRenameTmuxSession(t) },
    // Kill starts as a normal danger item; first click arms it in-place
    { icon: 'trash-2', label: 'Kill session…', danger: true, action: null },
  ], rect.bottom + 2, rect.right - 180);

  // Wire the kill item (last danger button) with two-click hazard in the menu
  const menu = document.getElementById('files-floating-menu');
  if (!menu) return;
  const items = menu.querySelectorAll('.files-tab-menu-item.danger');
  const killBtn = items[items.length - 1];
  if (!killBtn) return;

  const killAction = async () => {
    try {
      await apiFetch('/api/v1/terminal/tmux-sessions/' + encodeURIComponent(t.name),
        { method: 'DELETE' });
      ftLoadAllSessions();
    } catch (e) {
      alert('Could not kill tmux session: ' + ((e && e.message) || e));
    }
  };

  // Replace the default click handler on the kill item (which auto-closes)
  // with one that stays open on first click, then confirms on second.
  // CloneNode removes the original listeners.
  const newKillBtn = killBtn.cloneNode(true);
  killBtn.parentNode.replaceChild(newKillBtn, killBtn);
  newKillBtn.className = 'files-tab-menu-item danger';
  newKillBtn.innerHTML =
    '<i data-lucide="trash-2" class="lucide-icon"></i><span>Kill session…</span><span class="files-tab-menu-check">✓</span>';
  _refreshLucideIcons(newKillBtn);
  newKillBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const state = newKillBtn.dataset.state || '';
    if (state === 'armed') {
      // Second click — confirm
      closeFloatingMenu();
      killAction();
    } else {
      // First click — arm in-place, stay open
      newKillBtn.dataset.state = 'armed';
      newKillBtn.classList.add('armed');
      newKillBtn.innerHTML =
        '<i data-lucide="alert-triangle" class="lucide-icon"></i><span>Confirm Kill</span>';
      _refreshLucideIcons(newKillBtn);
    }
  });
}

// Rename a tmux session via the backend tmux rename endpoint.
async function _ftRenameTmuxSession(t) {
  const raw = window.prompt('Rename tmux session "' + (t.name || '') + '":', t.name || '');
  if (raw == null) return;
  const name = raw.trim();
  if (!name || name === t.name) return;
  try {
    await apiFetch('/api/v1/terminal/tmux-sessions/' + encodeURIComponent(t.name) + '/rename',
      { method: 'POST', body: JSON.stringify({ name }) });
    ftLoadAllSessions();
  } catch (e) {
    alert('Could not rename tmux session: ' + ((e && e.message) || e));
  }
}

// Kill (delete) a tmux session via the backend tmux delete endpoint.
async function _ftDeleteTmuxSession(t) {
  if (!confirm('Kill tmux session "' + (t.name || '') + '"?\n\nThis kills the session and all its panes.')) return;
  try {
    await apiFetch('/api/v1/terminal/tmux-sessions/' + encodeURIComponent(t.name),
      { method: 'DELETE' });
    ftLoadAllSessions();
  } catch (e) {
    alert('Could not kill tmux session: ' + ((e && e.message) || e));
  }
}

async function _ftFetchAndRender(url, renderFn, errorElId) {
  try {
    const data = await apiFetch(url);
    renderFn(Array.isArray(data) ? data : []);
  } catch (e) {
    const host = document.getElementById(errorElId);
    if (host) host.innerHTML = '<div class="ft-empty ft-error">Error: ' + ftEscapeHtml(e.message || e) + '</div>';
  }
}

async function ftLoadQuickLaunches() {
  await _ftFetchAndRender('/api/v1/terminal/quick-launches', ftRenderLaunches, 'ft-list-launches');
  _ftQuickLaunchesLoaded = true;
}



function _fmtIdle(secs) {
  if (secs == null) return '';
  if (secs < 60) return secs + 's';
  if (secs < 3600) return Math.floor(secs / 60) + 'm';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h';
  return Math.floor(secs / 86400) + 'd';
}

function _sessionLabel(sess) {
  // Server-supplied name wins; then a local name this browser remembered; then
  // an open tab's name in this browser; then a shortened id.
  if (sess.name) return sess.name;
  const localName = getTerminalSessionName(sess.session_id);
  if (localName) return localName;
  const openTab = termTabs.find((t) => t.kind === 'terminal' && t.path === sess.session_id);
  if (openTab && openTab.name) return openTab.name;
  // session_id is 'terminal:<uuid>' — show the first 8 chars of the uuid.
  const raw = String(sess.session_id || '');
  const tail = raw.startsWith('terminal:') ? raw.slice(9) : raw;
  return tail.slice(0, 8) || 'session';
}

// ftRenderSessions removed — replaced by ftRenderAllSessions above

// 3-dot row menu for a "Your sessions" entry: rename + delete. Reuses the
// shared floating-menu component (same look as the terminal tab menu).
function _ftShowSessionMenu(s, label, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  openFloatingMenu([
    { icon: 'pencil', label: 'Rename…', action: () => _ftRenameSession(s.session_id, label) },
    {
      icon: 'trash-2',
      label: (s.agent_driven ? 'Kill session…' : 'Delete…'),
      danger: true,
      action: () => _ftDeleteSession(s.session_id, label),
    },
  ], rect.bottom + 2, rect.right - 180);
}

// Rename a session from the sidebar. Names are per-user: we remember the new
// label locally (sidebar fallback + sent on future WS opens), update any tab
// open for it in THIS browser, and push it to the server so the user's other
// devices pick it up too. The server name is authoritative in _sessionLabel,
// so a session that already had a server/agent name still gets renamed.
async function _ftRenameSession(sessionId, currentLabel) {
  const next = prompt('Rename session:', currentLabel || '');
  if (next == null) return;                       // cancelled
  const name = next.trim();
  try { setTerminalSessionName(sessionId, name); } catch (_) {}
  const tab = termTabs.find((t) => t.kind === 'terminal' && t.path === sessionId);
  if (tab) {
    if (name) tab.name = name;
    if (tab.instance && typeof tab.instance.setName === 'function') {
      try { tab.instance.setName(name); } catch (_) {}
    }
    renderTermTabs();
    persistTermTabs();
  }
  try {
    await apiFetch('/api/v1/terminal/sessions/' + encodeURIComponent(sessionId) + '/rename',
      { method: 'POST', body: JSON.stringify({ name }) });
  } catch (e) {
    // Older server without the rename route — the local rename above still
    // relabels this browser; degrade quietly.
    console.warn('Session rename (server) failed:', (e && e.message) || e);
  }
  ftLoadSessions();
}

// Delete (kill) a session from the sidebar. If a tab for it is open in this
// browser, closeTermTab kills the PTY and tears down the tab; otherwise we DELETE
// the session directly. Either way we drop the remembered name and refresh.
async function _ftDeleteSession(sessionId, label) {
  if (!confirm('Delete terminal session "' + label + '"?\n\n' +
               'This kills the running shell — anything unsaved in it is lost.')) return;
  const tab = termTabs.find((t) => t.kind === 'terminal' && t.path === sessionId);
  try {
    if (tab && tab.instance) {
      await closeTermTab(sessionId);                  // kills the PTY + removes the tab
    } else {
      await apiFetch('/api/v1/terminal/sessions/' + encodeURIComponent(sessionId),
        { method: 'DELETE' });
    }
  } catch (e) {
    alert('Could not delete session: ' + ((e && e.message) || e));
  }
  try { setTerminalSessionName(sessionId, ''); } catch (_) {}
  ftLoadSessions();
}

// Take-over lock: pause/resume an agent's control of a terminal session.
async function ftSetSessionPaused(sessionId, paused) {
  return apiFetch(
    '/api/v1/terminal/sessions/' + encodeURIComponent(sessionId) + '/pause',
    { method: 'POST', body: JSON.stringify({ paused: !!paused }) },
  );
}

// Combined loader: fetches PTY sessions AND tmux sessions then renders them
// as one unified list with different icons (PTY = square-terminal, tmux = layers).
async function ftLoadAllSessions() {
  try {
    const [sessions, tmux] = await Promise.all([
      apiFetch('/api/v1/terminal/sessions'),
      apiFetch('/api/v1/terminal/tmux-sessions'),
    ]);
    ftRenderAllSessions(Array.isArray(sessions) ? sessions : [], Array.isArray(tmux) ? tmux : []);
  } catch (e) {
    const host = document.getElementById('ft-list-sessions');
    if (host) host.innerHTML = '<div class="ft-empty ft-error">Error: ' + ftEscapeHtml(e.message || e) + '</div>';
  }
}
// Backward-compat so existing callers (pause button, delete, etc.) still work
async function ftLoadSessions() { return ftLoadAllSessions(); }

// Click handler for a "Your sessions" row. If we already have a tab open in
// this browser for that session_id, activate it; otherwise add a tab with the
// same id — the WebSocket layer reattaches to the live PTY automatically and
// replays scrollback.
function openOrAttachTerminalSession(sessionId, name, sshServerId) {
  if (!sessionId) return;
  const existing = termTabs.find((t) => t.kind === 'terminal' && t.path === sessionId);
  if (existing) {
    activeTerminalId = sessionId;
    activateTermTab(sessionId);
  } else {
    // Carry the SSH server id (if this is a remote session) so that, should the
    // backend session have died, reconnecting respawns the SSH shell rather than
    // a local one. A still-live session just reattaches regardless.
    pushTerminalTab(sessionId, name || undefined, { sshServerId: sshServerId || '' });
    activeTerminalId = sessionId;
    renderTermTabs();
    renderTermPanes();
    persistTermTabs();
  }
  _switchToTerminalStrip();
}

function openTerminalLaunchersPanel() {
  // Quick launches are effectively static; load once per page lifetime.
  if (!_ftQuickLaunchesLoaded) ftLoadQuickLaunches();
  // Saved SSH servers: refresh on each panel open (cheap GET) so a server just
  // added in the Deploy panel appears without a page reload. Self-hides when
  // none are saved.
  ftLoadSshServers();
  // Sessions + tmux refresh on every panel show, then poll every 5s while
  // the panel stays open. Sessions surface PTYs created on any device the
  // user is signed into, so opening this panel on a new device shows the
  // running shells from elsewhere and lets you reattach.
  ftLoadAllSessions();
  stopTerminalLaunchersPolling();
  _ftTmuxPollTimer = setInterval(() => {
    ftLoadAllSessions();
  }, 5000);
  // Wire the refresh button once. Repeat-safe: removeEventListener-then-add
  // would be wordy, so we use a sentinel attribute.
  const btn = document.getElementById('ft-refresh');
  if (btn && !btn.dataset.wired) {
    btn.dataset.wired = '1';
    btn.title = 'Restart server & reload page';
    btn.addEventListener('click', async () => {
      if (btn.dataset.busy === '1') return;
      btn.dataset.busy = '1';
      const origTitle = btn.title;
      btn.title = 'Restarting server…';
      btn.classList.add('is-spinning');
      const ok = await restartServerAndReload();
      if (!ok) {
        btn.dataset.busy = '';
        btn.title = origTitle;
        btn.classList.remove('is-spinning');
        alert('Server did not come back within 60s. Check `journalctl -u webagent -f`.');
      }
    });
  }
  // Wire the "New terminal" primary action in the launcher panel. Replaces
  // the old standalone Terminal Tab strip icon.
  const newBtn = document.getElementById('ft-new-terminal');
  if (newBtn && !newBtn.dataset.wired) {
    newBtn.dataset.wired = '1';
    newBtn.addEventListener('click', () => openNewTerminalTab());
  }
  // "New tmux session" — prompts for a name, then opens a tab that runs
  // `tmux new -As <name>`. -A reattaches if a session by that name already
  // exists, so clicking the same button twice reattaches instead of
  // erroring. Sanitise the name to what tmux actually accepts.
  const newTmuxBtn = document.getElementById('ft-new-tmux');
  if (newTmuxBtn && !newTmuxBtn.dataset.wired) {
    newTmuxBtn.dataset.wired = '1';
    newTmuxBtn.addEventListener('click', () => openNewTmuxSessionDefault());
  }
}

function _sanitiseTmuxName(raw) {
  // tmux disallows '.', ':' and whitespace in session names. Collapse to
  // [A-Za-z0-9_-], replacing runs of unsupported chars with a single dash.
  return String(raw || '')
    .trim()
    .replace(/[^A-Za-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40);
}

function promptNewTmuxSession() {
  // Suggest a fresh name like work-3 based on how many tmux tabs already exist.
  const existing = termTabs.filter((t) => t.kind === 'terminal' && !!t.tmuxSession);
  const suggested = 'work-' + (existing.length + 1);
  const raw = window.prompt(
    "New tmux session name (letters, numbers, '-' or '_'):",
    suggested,
  );
  if (raw == null) return;
  const name = _sanitiseTmuxName(raw);
  if (!name) {
    alert('Invalid name. Use letters, numbers, dashes, or underscores.');
    return;
  }
  // -A = attach if it already exists, otherwise create. Single-quote the
  // name; tmux session names can't contain a single quote, so no need to
  // escape further.
  openNewTerminalTabWithCommand(_ftTmuxMouseOn("tmux new -As '" + name + "'"), name);
}

// Open a new tmux session with an auto-generated default name — no naming
// dialog. Used by the "+" button's long-press (see initTermCarousel). Mirrors
// promptNewTmuxSession's create path but skips the prompt; returns the name so
// the caller can show it in the confirmation popup.
function openNewTmuxSessionDefault() {
  const existing = termTabs.filter((t) => t.kind === 'terminal' && !!t.tmuxSession);
  const name = _sanitiseTmuxName('work-' + (existing.length + 1)) || 'work';
  openNewTerminalTabWithCommand(_ftTmuxMouseOn("tmux new -As '" + name + "'"), name);
  return name;
}

// Small white confirmation popup. Mirrors the ability-tree ⚠ `.ac-save-pop`
// hazard callout (same little plate + pointing nub + lift-in) but WHITE — a
// confirmation, not a hazard — anchored just under `anchorEl`. Appended to
// <body> and FIXED-positioned so the tab bar's overflow:hidden can't clip it.
// Auto-removes after ~1.9s. Styled `.files-term-pop` in files.css.
let _termPopEl = null;
let _termPopTimer = null;
function _flashTermPop(anchorEl, text) {
  if (!anchorEl) return;
  try { if (_termPopEl) _termPopEl.remove(); } catch (_) {}
  if (_termPopTimer) { clearTimeout(_termPopTimer); _termPopTimer = null; }
  const pop = document.createElement('div');
  pop.className = 'files-term-pop';
  pop.textContent = text;
  document.body.appendChild(pop);
  const r = anchorEl.getBoundingClientRect();
  pop.style.left = Math.round(r.left + r.width / 2) + 'px';
  pop.style.top = Math.round(r.bottom + 8) + 'px';
  _termPopEl = pop;
  // Force a synchronous reflow so the opacity/transform transition actually
  // runs when we add `.show` — don't use requestAnimationFrame here, it's
  // paused in backgrounded/throttled tabs (ui_toggle_list skill gotcha).
  void pop.offsetWidth;
  pop.classList.add('show');
  _termPopTimer = setTimeout(() => {
    pop.classList.remove('show');
    setTimeout(() => {
      try { pop.remove(); } catch (_) {}
      if (_termPopEl === pop) _termPopEl = null;
    }, 220);
    _termPopTimer = null;
  }, 1900);
}

function stopTerminalLaunchersPolling() {
  if (_ftTmuxPollTimer) {
    clearInterval(_ftTmuxPollTimer);
    _ftTmuxPollTimer = null;
  }
}


// ── Lifecycle (drop-in view hooks called by the Admin-Tools frame) ─
// The frame's applySidebarView dispatches these via terminal/page.json's
// start/stop, exactly like every other drop-in admin view.
let _termWired = false;
let _termRestored = false;

export function startView() {
  if (!_termWired) {
    _termWired = true;
    initTermCarousel();
    initTerminalKeybar();
    initTerminalPinchZoom();
    initTerminalViewportRefit();
    initTerminalTabSwipe();
  }
  initTermResizeHandle();
  if (!_termRestored) {
    _termRestored = true;
    restoreTermTabs();          // async — reopens persisted terminals, then renders
  } else {
    renderTermTabs();
    renderTermPanes();
  }
  openTerminalLaunchersPanel();
  const tab = getActiveTerminalTab();
  if (tab && tab.instance) setTimeout(() => tab.instance.fit(), 30);
}

export function stopView() {
  stopTerminalLaunchersPolling();
}

// Wire global shortcuts (Ctrl+`, terminal zoom, resize refit) as soon as the
// module loads — reconnect.js statically imports reconnectAllTerminals at boot,
// so this runs app-wide before the Terminal view is ever opened.
initTermGlobalKeys();

import { copyText } from './clipboard.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';
import { purgeAllBrowserData } from './browser-data-reset.js';

'use strict';

/**
 * debugConsole.js — Floating debug console log panel
 *
 * Intercepts console.log / console.warn / console.error / console.debug and
 * shows the output in a fixed-bottom panel. The panel is hidden by default
 * and toggled via the bug icon button in the header (#debug-console-toggle).
 *
 * Captures all calls (even ones made before this module initialises, if
 * the earlyCapture buffer is used). Filter buttons let you show/hide log
 * levels. Entries can be preserved across page refreshes via sessionStorage.
 */

// ── Early capture buffer ─────────────────────────────────────────────────
// Stores console calls made before the panel DOM is ready, so nothing is lost.
const _earlyBuffer = [];
const _origConsole = {};
let _initialized = false;

// Set while a full browser-data reset (#header-refresh-sw) is running, so the
// pagehide/visibilitychange session mirror below cannot write the log buffer
// back into the sessionStorage that reset just cleared.
let _wipeInProgress = false;

// ── Capture console methods ──────────────────────────────────────────────
function _intercept(logLevel, origFn) {
  return function (...args) {
    // Still forward to the real console
    origFn.apply(console, args);

    // Capture the output
    try {
      const text = args.map(a => {
        if (typeof a === 'string') return a;
        if (a === null) return 'null';
        if (a === undefined) return 'undefined';
        try { return JSON.stringify(a, null, 2); } catch (_) { return String(a); }
      }).join(' ');

      // Build a stack trace snippet (omit this wrapper frame)
      let stack = '';
      try { throw new Error(); } catch (e) {
        if (e.stack) {
          const lines = e.stack.split('\n');
          // Skip frames: Error, _intercept, the console wrapper itself
          let skip = 3;
          for (const line of lines) {
            if (skip > 0) { skip--; continue; }
            const trimmed = line.trim();
            if (!trimmed || trimmed.startsWith('debugConsole')) continue;
            stack = trimmed;
            break;
          }
        }
      }

      // Forward genuine errors to the server diagnostic log (logs.db) via the
      // shared funnel (window.__reportJsError, installed pre-module in index.html),
      // so app/library console.error calls — e.g. genui's "[genui] mount
      // error:" — are recorded, not just shown in this panel. window.onerror /
      // unhandledrejection already cover UNCAUGHT errors; this adds the ones code
      // swallows with try/catch + console.error. Skip our own banner marker so an
      // error isn't logged twice, and let the funnel dedupe the rest by message.
      // (Browser-internal render warnings — bad SVG path d=, CSS parse — are NOT
      // routed through console.error and remain uncapturable from JS.)
      if (logLevel === 'error' && text && text.indexOf('[WebAgent JS error]') !== 0) {
        try {
          if (typeof window.__reportJsError === 'function') {
            window.__reportJsError({ message: text, source: 'console.error', stack: stack });
          }
        } catch (_) {}
      }

      const entry = { level: logLevel, text, stack, ts: Date.now() };

      if (_initialized) {
        _addEntry(entry);
      } else {
        _earlyBuffer.push(entry);
        if (_earlyBuffer.length > 500) _earlyBuffer.shift();
      }
    } catch (_) {}
  };
}

// ── Panel state ──────────────────────────────────────────────────────────
let _filterState = { log: true, warn: true, error: true, debug: false };
let _entries = [];

// ── DOM refs (set during init) ──
let _panel, _content, _toggle, _close;
let _clearBtn, _countEl;
let _filterBtns = {};

// ── Add a single entry to the list ──
function _addEntry(entry) {
  _entries.push(entry);
  if (entry.level === 'error') _updateErrorIndicator();

  // Apply current filter
  if (!_filterState[entry.level]) return;

  const el = document.createElement('div');
  el.className = 'dc-entry ' + entry.level;

  const tag = document.createElement('span');
  tag.className = 'dc-tag ' + entry.level;
  tag.textContent = entry.level;
  el.appendChild(tag);

  const msg = document.createElement('span');
  msg.className = 'dc-msg';
  msg.textContent = entry.text;
  el.appendChild(msg);

  if (entry.stack) {
    const stackEl = document.createElement('span');
    stackEl.className = 'dc-stack';
    stackEl.textContent = entry.stack;
    // Click to expand/collapse stack trace
    stackEl.addEventListener('click', (e) => {
      e.stopPropagation();
      stackEl.classList.toggle('expanded');
    });
    el.appendChild(stackEl);
  }

  // Click the row to copy
  _makeEntryClickable(el, entry);

  // Remove empty hint
  const empty = _content.querySelector('.debug-console-empty');
  if (empty) empty.remove();

  _content.appendChild(el);
  _content.scrollTop = _content.scrollHeight;
  _updateCount();
}

// ── Re-filter all entries ──
function _reFilter() {
  // Remove all entry elements
  const entries = _content.querySelectorAll('.dc-entry');
  entries.forEach(e => e.remove());

  // Re-add empty hint if needed
  const hasVisible = _entries.some(e => _filterState[e.level]);
  const empty = _content.querySelector('.debug-console-empty');
  if (!hasVisible) {
    if (!empty) {
      const hint = document.createElement('div');
      hint.className = 'debug-console-empty';
      hint.textContent = 'No entries match the current filter.';
      _content.appendChild(hint);
    }
  } else {
    if (empty) empty.remove();
  }

  // Re-add matching entries
  let visibleCount = 0;
  for (const entry of _entries) {
    if (!_filterState[entry.level]) continue;
    visibleCount++;

    const el = document.createElement('div');
    el.className = 'dc-entry ' + entry.level;

    const tag = document.createElement('span');
    tag.className = 'dc-tag ' + entry.level;
    tag.textContent = entry.level;
    el.appendChild(tag);

    const msg = document.createElement('span');
    msg.className = 'dc-msg';
    msg.textContent = entry.text;
    el.appendChild(msg);

    if (entry.stack) {
      const stackEl = document.createElement('span');
      stackEl.className = 'dc-stack';
      stackEl.textContent = entry.stack;
      stackEl.addEventListener('click', (e) => {
        e.stopPropagation();
        stackEl.classList.toggle('expanded');
      });
      el.appendChild(stackEl);
    }

    // Click the row to copy
    _makeEntryClickable(el, entry);

    _content.appendChild(el);
  }

  if (visibleCount > 0) {
    _content.scrollTop = _content.scrollHeight;
  }
  _updateCount();
}

function _updateCount() {
  if (_countEl) {
    const visible = _entries.filter(e => _filterState[e.level]).length;
    _countEl.textContent = visible + ' / ' + _entries.length + ' entries';
  }
}

// ── Error indicator on the header bug-toggle ──────────────────────────────
// While the log holds ≥1 error entry, the toggle button gets `.has-errors`
// (red icon — see app3.css). This is the app's always-visible error signal,
// replacing the old fixed top-of-page JS error banner: every banner error
// also emitted console.error('[WebAgent JS error]', …) from main.js, which
// this panel captures, so the banner was a strict subset of the log.
function _updateErrorIndicator() {
  if (!_toggle) return;
  const hasErrors = _entries.some(e => e.level === 'error');
  _toggle.classList.toggle('has-errors', hasErrors);
}

function _clearLog() {
  _entries = [];
  const entryEls = _content.querySelectorAll('.dc-entry');
  entryEls.forEach(e => e.remove());
  const empty = _content.querySelector('.debug-console-empty');
  if (!empty) {
    const hint = document.createElement('div');
    hint.className = 'debug-console-empty';
    hint.textContent = 'Console log output will appear here.';
    _content.appendChild(hint);
  }
  _updateCount();

  // Also clear preserved storage
  try {
    sessionStorage.removeItem('debugConsoleEntries');
  } catch (_) {}
  _updateErrorIndicator();
}

// ── Make a row element clickable to copy its text ──
function _makeEntryClickable(el, entry) {
  el.addEventListener('click', () => _copyEntry(entry, el));

  // Stack trace expand/collapse shouldn't also copy the row
  const stackEl = el.querySelector('.dc-stack');
  if (stackEl) {
    stackEl.addEventListener('click', (e) => e.stopPropagation());
  }
}

// ── Copy all visible entries as formatted text ──
function _copyAllLogs() {
  const lines = _entries
    .filter(e => _filterState[e.level])
    .map(e => '[' + e.level.toUpperCase() + '] ' + e.text + (e.stack ? '\n  ' + e.stack : ''));
  const text = lines.join('\n');
  if (!text) return;
  copyText(text);
  const btn = document.getElementById('debug-console-copy-all');
  if (btn) {
    btn.classList.add('copied');
    setTimeout(() => btn.classList.remove('copied'), 1200);
  }
}

// ── Copy a single entry ──
function _copyEntry(entry, el) {
  const text = '[' + entry.level.toUpperCase() + '] ' + entry.text + (entry.stack ? '\n  ' + entry.stack : '');
  copyText(text);
  // Brief highlight flash
  el.classList.add('copied-row');
  setTimeout(() => el.classList.remove('copied-row'), 400);
}

function _saveToSession() {
  // A full browser-data reset has already emptied sessionStorage and is about
  // to reload; mirroring the buffer back would undo part of that sweep.
  if (_wipeInProgress) return;
  try {
    if (_entries.length > 0) {
      // Trim to last 200 entries to keep storage sane
      const toSave = _entries.slice(-200);
      sessionStorage.setItem('debugConsoleEntries', JSON.stringify(toSave));
    }
  } catch (_) {}
}

function _loadFromSession() {
  try {
    const saved = sessionStorage.getItem('debugConsoleEntries');
    if (saved) {
      const parsed = JSON.parse(saved);
      if (Array.isArray(parsed)) {
        // Merge with any early-buffer entries already drained, session ones first
        // (chronological order: session = older page, early buffer = this boot).
        if (_entries.length > 0) {
          _entries = parsed.concat(_entries);
        } else {
          _entries = parsed;
        }
      }
    }
  } catch (_) {}
  // Always re-render: covers early-buffer-only and session-only and merged cases
  _reFilter();
  _updateErrorIndicator();
}

// ── Init ─────────────────────────────────────────────────────────────────
export function initDebugConsole() {
  if (_initialized) return;

  // ── Drain early capture buffer (console calls made before this module loaded) ──
  // The early inline script in index.html's <head> intercepts console.* from the
  // very first script tag onward, buffering everything into a window-level array.
  // We drain it here so no boot-time log is lost.
  if (window.__earlyConsoleBuffer && window.__earlyConsoleBuffer.length > 0) {
    _entries = window.__earlyConsoleBuffer.slice();
    window.__earlyConsoleBuffer = null;  // tells the early wrapper to stop buffering
    // Don't re-render yet — _loadFromSession runs later and may merge more entries
  }

  _panel = document.getElementById('debug-console-panel');
  _content = document.getElementById('debug-console-content');
  _toggle = document.getElementById('debug-console-toggle');
  _close = document.getElementById('debug-console-close');
  _clearBtn = document.getElementById('debug-console-clear');
  _countEl = document.getElementById('debug-console-count');

  if (!_panel || !_content || !_toggle) return;

  // Restore visibility from session
  try {
    const wasOpen = sessionStorage.getItem('debugConsoleOpen') === 'true';
    if (wasOpen) _panel.classList.add('open');
  } catch (_) {}

  // Toggle button
  _toggle.addEventListener('click', () => {
    _panel.classList.toggle('open');
    try {
      sessionStorage.setItem('debugConsoleOpen', String(_panel.classList.contains('open')));
    } catch (_) {}
    if (_panel.classList.contains('open')) {
      _toggle.classList.add('active');
      _content.scrollTop = _content.scrollHeight;
    } else {
      _toggle.classList.remove('active');
    }
  });

  // If it started open, mark the toggle active
  if (_panel.classList.contains('open')) {
    _toggle.classList.add('active');
  }

  // Close button (minimise)
  if (_close) {
    _close.addEventListener('click', () => {
      _panel.classList.remove('open');
      _toggle.classList.remove('active');
      try { sessionStorage.setItem('debugConsoleOpen', 'false'); } catch (_) {}
    });
  }

  // Clear button
  if (_clearBtn) {
    _clearBtn.addEventListener('click', _clearLog);
  }

  // Copy-all button
  const _copyAllBtn = document.getElementById('debug-console-copy-all');
  if (_copyAllBtn) {
    _copyAllBtn.addEventListener('click', _copyAllLogs);
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      try { window.lucide.createIcons({ nodes: [_copyAllBtn.querySelector('[data-lucide]')].filter(Boolean) }); } catch (_) {}
    }
  }

  // ── Commit & Push button ──
  const _commitPushBtn = document.getElementById('debug-console-commit-push');
  let _commitAbortController = null;

  if (_commitPushBtn) {
    // Render the lucide icon
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      try { window.lucide.createIcons({ nodes: [_commitPushBtn.querySelector('[data-lucide]')].filter(Boolean) }); } catch (_) {}
    }

    _commitPushBtn.addEventListener('click', async () => {
      if (_commitAbortController) return; // already running

      const controller = new AbortController();
      _commitAbortController = controller;
      _commitPushBtn.disabled = true;
      _commitPushBtn.classList.add('is-working');
      console.log('› commit & push — starting...');

      let final = null;
      try {
        const res = await fetch(apiPath('/api/v1/github/commit-and-push'), {
          method: 'POST',
          headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
          body: JSON.stringify({ stream: true }),
          signal: controller.signal,
        });

        if (!res.ok) {
          let detail = res.statusText;
          try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
          throw new Error(detail || ('HTTP ' + res.status));
        }

        const ctype = res.headers.get('content-type') || '';
        if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
          final = await res.json();
        } else {
          const reader = res.body.getReader();
          const dec = new TextDecoder();
          let buf = '';
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += dec.decode(value, { stream: true });
            let nl;
            while ((nl = buf.indexOf('\n')) >= 0) {
              const line = buf.slice(0, nl).trim();
              buf = buf.slice(nl + 1);
              if (!line) continue;
              try {
                const ev = JSON.parse(line);
                if (ev.phase === 'done') final = ev.result || null;
                else if (ev.phase && ev.phase !== 'done') {
                  const labels = { analyzing: 'Analyzing...', scanning: 'Safety check...', committing: 'Committing...', pushing: 'Pushing...' };
                  console.log('› ' + (labels[ev.phase] || ev.phase));
                }
              } catch (_) {}
            }
          }
          // Drain remainder
          const tail = dec.decode();
          if (tail) {
            const tailLine = tail.trim();
            if (tailLine) {
              try {
                const ev = JSON.parse(tailLine);
                if (ev.phase === 'done') final = ev.result || null;
              } catch (_) {}
            }
          }
        }
      } catch (e) {
        console.error('commit & push failed: ' + (e.message || e));
      }

      _commitPushBtn.classList.remove('is-working');
      _commitPushBtn.disabled = false;

      if (final) {
        const push = final.push || {};
        const pushed = push.attempted && push.ok;
        const msg = (pushed ? '✓ Pushed: ' : '✓ Committed: ') + (final.title || final.hash || 'ok');
        console.log(msg);
        _commitPushBtn.classList.add('is-done');
        setTimeout(() => { _commitPushBtn.classList.remove('is-done'); }, 1600);
      }

      _commitPushBtn.disabled = false;
      _commitAbortController = null;
    });
  }

  // ── Restart server controls (two-tap arm/confirm) ──
  // The debug-panel and app-admin header controls intentionally share the same
  // authenticated restart/recovery path while retaining independent confirmation
  // timers, so a first tap in one location cannot confirm the other.
  const _restartBtns = Array.from(document.querySelectorAll('#debug-console-restart, #header-restart-server'));
  const _restartTimers = new Map();

  function _restartIconMarkup(btn, icon) {
    const size = btn.id === 'debug-console-restart' ? 12 : 14;
    return '<i data-lucide="' + icon + '" class="lucide-icon" style="width:' + size + 'px;height:' + size + 'px;display:block;"></i>';
  }

  function _renderRestartIcon(btn, icon) {
    btn.innerHTML = _restartIconMarkup(btn, icon);
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      try { window.lucide.createIcons({ nodes: [btn.querySelector('[data-lucide]')].filter(Boolean) }); } catch (_) {}
    }
  }

  function _restartResetBtn(btn) {
    if (!btn) return;
    clearTimeout(_restartTimers.get(btn));
    _restartTimers.delete(btn);
    btn.dataset.state = 'idle';
    btn.classList.remove('is-armed');
    btn.disabled = false;
    btn.title = 'Restart server';
    _renderRestartIcon(btn, btn.id === 'header-restart-server' ? 'power' : 'rotate-ccw');
  }

  _restartBtns.forEach((btn) => {
    // Render the initial Lucide icon after the static header has parsed.
    _renderRestartIcon(btn, btn.id === 'header-restart-server' ? 'power' : 'rotate-ccw');

    btn.addEventListener('click', async () => {
      if (btn.dataset.state === 'armed') {
        // Second click — confirmed, fire the existing authenticated restart flow.
        clearTimeout(_restartTimers.get(btn));
        _restartTimers.delete(btn);
        btn.dataset.state = 'idle';
        btn.classList.remove('is-armed');
        btn.disabled = true;
        btn.title = 'Restarting…';
        btn.innerHTML = '<span style="font-size:9px;">Restarting…</span>';
        console.log('› restarting server...');
        // Keep the diagnostic console's module graph small so its header toggle
        // is wired as soon as the shell parses. The file-manager graph is only
        // needed when the user actually confirms a server restart.
        const { restartServerAndReload } = await import('./files-git.js');
        const ok = await restartServerAndReload();
        if (!ok) {
          console.error('› server restart timed out — reload manually');
          _restartResetBtn(btn);
        }
        // On success restartServerAndReload calls window.location.reload().
        return;
      }

      // First click — arm this control for three seconds.
      clearTimeout(_restartTimers.get(btn));
      btn.dataset.state = 'armed';
      btn.classList.add('is-armed');
      btn.title = 'Click again to restart server';
      _renderRestartIcon(btn, 'alert-triangle');
      _restartTimers.set(btn, setTimeout(() => _restartResetBtn(btn), 3000));
    });
  });

  // ── Carousel chevron toggle (same pattern as main-tab strip) ──
  const _actionsWrap = document.querySelector('.debug-console-header-actions-wrap');
  const _actionsStrip = document.querySelector('.debug-console-header-actions');
  const _chevLeft = _actionsWrap && _actionsWrap.querySelector('.debug-console-chev.left');
  const _chevRight = _actionsWrap && _actionsWrap.querySelector('.debug-console-chev.right');

  function _updateDebugChevrons() {
    if (!_actionsStrip || !_chevLeft || !_chevRight) return;
    const overflow = _actionsStrip.scrollWidth - _actionsStrip.clientWidth > 1;
    _chevLeft.classList.toggle('visible', overflow && _actionsStrip.scrollLeft > 1);
    _chevRight.classList.toggle('visible', overflow && _actionsStrip.scrollLeft < _actionsStrip.scrollWidth - _actionsStrip.clientWidth - 1);
  }

  if (_actionsStrip && _chevLeft && _chevRight) {
    const _chevStep = function () { return Math.max(60, Math.floor(_actionsStrip.clientWidth * 0.5)); };
    _chevLeft.addEventListener('click', function () { _actionsStrip.scrollBy({ left: -_chevStep(), behavior: 'smooth' }); });
    _chevRight.addEventListener('click', function () { _actionsStrip.scrollBy({ left: _chevStep(), behavior: 'smooth' }); });
    _actionsStrip.addEventListener('scroll', _updateDebugChevrons, { passive: true });
    requestAnimationFrame(_updateDebugChevrons);
    if (typeof ResizeObserver !== 'undefined') {
      let roPending = false;
      const ro = new ResizeObserver(function () {
        if (roPending) return;
        roPending = true;
        requestAnimationFrame(function () { roPending = false; _updateDebugChevrons(); });
      });
      ro.observe(_actionsStrip);
    }
    window.addEventListener('resize', _updateDebugChevrons);
  }

  // ── Drag-to-resize (Pointer Events API — works for both mouse and touch) ──
  const _dragHandle = document.getElementById('debug-console-drag-handle');
  const STORAGE_HEIGHT_KEY = 'debugConsoleHeight';
  const MIN_H = 120;
  const MAX_H = window.innerHeight - 100;

  // Restore saved height
  try {
    const saved = sessionStorage.getItem(STORAGE_HEIGHT_KEY);
    if (saved) {
      const h = parseInt(saved, 10);
      if (!isNaN(h) && h >= MIN_H && h <= MAX_H) _panel.style.height = h + 'px';
    }
  } catch (_) {}

  let _dragging = false;
  let _dragStartY = 0;
  let _dragStartHeight = 0;

  if (_dragHandle) {
    _dragHandle.addEventListener('pointerdown', (e) => {
      _dragging = true;
      _dragStartY = e.clientY;
      _dragStartHeight = _panel.getBoundingClientRect().height;
      _dragHandle.classList.add('resizing');
      document.body.style.cursor = 'row-resize';
      document.body.style.userSelect = 'none';
      _dragHandle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });
  }

  document.addEventListener('pointermove', (e) => {
    if (!_dragging) return;
    const delta = _dragStartY - e.clientY;
    const maxH = window.innerHeight - 100;
    const newH = Math.min(maxH, Math.max(MIN_H, _dragStartHeight + delta));
    _panel.style.height = newH + 'px';
  });

  document.addEventListener('pointerup', () => {
    if (!_dragging) return;
    _dragging = false;
    if (_dragHandle) _dragHandle.classList.remove('resizing');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
    try {
      sessionStorage.setItem(STORAGE_HEIGHT_KEY, parseInt(_panel.style.height, 10));
    } catch (_) {}
  });

  // ── SW version display + clear ──
  const _swVerEl = document.getElementById('debug-console-sw-version');
  const _clearSwEl = document.getElementById('debug-console-clear-sw');
  const _clearSwBtn = _clearSwEl ? _clearSwEl.querySelector('.dc-clear-sw-btn') : null;
  const _headerRefreshBtn = document.getElementById('header-refresh-sw');

  function _setSwVersion(label) {
    if (!_swVerEl) return;
    _swVerEl.textContent = 'SW: ' + label;
    _swVerEl.classList.add('has-sw');
  }

  // Narrow reset: unregister the active service worker, delete every cache,
  // then reload. Still bound to the debug panel's "Clear cache & SW" button,
  // whose label promises exactly that and nothing more.
  async function _resetSw() {
    try {
      if (navigator.serviceWorker) {
        const reg = await navigator.serviceWorker.getRegistration();
        if (reg) await reg.unregister();
      }
      if (caches && caches.keys) {
        const keys = await caches.keys();
        await Promise.all(keys.map((k) => caches.delete(k)));
      }
    } catch (_) {}
    window.location.reload();
  }

  // Full local wipe for the header refresh button: everything _resetSw does,
  // plus localStorage, sessionStorage, every IndexedDB database and every
  // script-readable cookie — i.e. the next load behaves like a fresh incognito
  // window. Fires immediately, with no confirmation prompt; the compact mobile
  // sidebar mirrors #header-refresh-sw and proxies its click, so this one
  // handler covers both surfaces (and 'is-busy' is mirrored onto its button).
  async function _resetBrowserCompletely() {
    if (_wipeInProgress) return;   // one-shot: repeat clicks during the wipe are ignored
    _wipeInProgress = true;
    if (_headerRefreshBtn) _headerRefreshBtn.classList.add('is-busy');
    try {
      const result = await purgeAllBrowserData();
      if (result.failures.length) {
        console.warn('[WebAgent] Full browser reset left data behind:', result.failures);
      }
    } catch (error) {
      console.warn('[WebAgent] Full browser reset failed:', error);
    }
    window.location.reload();
  }

  // Listen for the SW to push its version
  if (navigator.serviceWorker) {
    navigator.serviceWorker.addEventListener('message', (e) => {
      if (e.data && e.data.type === 'sw-version') {
        const ver = (e.data.cache || '').replace(/^webagent-v/, 'v');
        _setSwVersion(ver);
      }
    });
    // Also try reading the active cache name directly as fallback
    if (caches && caches.keys) {
      caches.keys().then((keys) => {
        const swCache = keys.find((k) => k.startsWith('webagent-'));
        if (swCache) {
          const ver = swCache.replace(/^webagent-v/, 'v');
          _setSwVersion(ver);
        }
      }).catch(() => {});
    }
  }

  // Click SW version → show/hide the clear prompt
  if (_swVerEl) {
    _swVerEl.addEventListener('click', () => {
      if (!_clearSwEl) return;
      const shown = _clearSwEl.style.display !== 'none';
      _clearSwEl.style.display = shown ? 'none' : 'inline-flex';
    });
  }

  // Clear button → unregister SW, delete all caches, reload
  if (_clearSwBtn) {
    _clearSwBtn.addEventListener('click', _resetSw);
  }

  // Header refresh button → FULL local wipe (see _resetBrowserCompletely).
  if (_headerRefreshBtn) {
    _headerRefreshBtn.addEventListener('click', _resetBrowserCompletely);
  }

  // ── Filter popover toggle ──
  const _filterToggle = document.getElementById('debug-console-filter-toggle');
  const _filterPopover = document.getElementById('debug-console-filter-popover');

  if (_filterToggle && _filterPopover) {
    _filterToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      const wasOpen = _filterPopover.classList.toggle('is-open');
      _filterToggle.classList.toggle('is-open', wasOpen);
    });

    // Close popover when clicking outside
    document.addEventListener('click', (e) => {
      if (!_filterPopover.contains(e.target) && e.target !== _filterToggle) {
        _filterPopover.classList.remove('is-open');
        _filterToggle.classList.remove('is-open');
      }
    });
  }

  // Filter buttons (inside popover)
  ['log', 'warn', 'error', 'debug'].forEach(level => {
    const btn = document.getElementById('debug-console-filter-' + level);
    if (!btn) return;
    _filterBtns[level] = btn;
    btn.addEventListener('click', () => {
      const isActive = btn.classList.toggle('active');
      _filterState[level] = isActive;
      _reFilter();
    });
  });

  // ── Command input (REPL) ──
  const _input = document.getElementById('debug-console-input');
  const _runBtn = document.getElementById('debug-console-run-btn');

  function _runCommand() {
    if (!_input) return;
    const command = _input.value.trim().toLowerCase();
    if (!command) return;

    // Show the expression in the console (use the intercepted console so it
    // appears in the panel as well as the real browser console)
    console.log('› ' + command);

    if (command === 'clear') {
      _clearLog();
    } else if (command === 'help') {
      console.log('Available commands: clear, help');
    } else {
      console.warn('Arbitrary JavaScript execution is disabled. Available commands: clear, help');
    }

    _input.value = '';
    _input.focus();
  }

  if (_input) {
    _input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        _runCommand();
      }
    });
  }
  if (_runBtn) {
    _runBtn.addEventListener('click', _runCommand);
  }

  // Save entries to sessionStorage before unload
  window.addEventListener('pagehide', _saveToSession);
  window.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') _saveToSession();
  });

  // ── Intercept console methods ──
  ['log', 'warn', 'error', 'debug'].forEach(level => {
    _origConsole[level] = console[level];
    console[level] = _intercept(level, _origConsole[level]);
  });

  _initialized = true;
  window.__debugConsoleReady = true;

  // Flush early buffer
  for (const entry of _earlyBuffer) {
    _addEntry(entry);
  }
  _earlyBuffer.length = 0;

  // Load any preserved entries from session
  _loadFromSession();
}

// This module is loaded directly by index.html instead of through main.js.
// Keep the console usable when the rest of the application module graph cannot
// finish booting (for example while the server is down and the PWA falls back
// to its cached shell).
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initDebugConsole, { once: true });
} else {
  initDebugConsole();
}

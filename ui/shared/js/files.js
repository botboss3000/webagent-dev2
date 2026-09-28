'use strict';

// Admin-Tools FRAME (shared shell only).
//
// This file is now purely the Admin-Tools shell that hosts EVERY admin view:
// the left sidebar + resize, the view-switcher strip, sidebar state
// (split/strip/maximize), the drop-in view dispatch (entry/start/stop), and the
// boot/teardown (startAdminTools / stopAdminTools / relocateAdminToolsContainers).
// It owns NO view content. The File Manager (Explorer) and Terminal are both
// folder drop-ins — ui/admin-tools/explorer/explorer-view.js and
// ui/admin-tools/terminal/terminal-view.js — discovered from their page.json and
// driven through this frame's generic dispatch, exactly like Database, Source
// Control and Settings. The two views share no code with each other.

import { isMobileLayout } from './layout.js';
import { app } from './state.js';
import { showRestrictedModal, authHeaders } from './left-login.js';

const API_BASE = '/api/v1/files';
const LS_SIDEBAR_VIEW = 'files.sidebarView';   // 'explorer' | 'git' | 'database'

let initialised = false;
let isAdmin = false;

// Persisted state (across tab switches and reloads)
const LS_SIDEBAR_WIDTH    = 'files.sidebarWidth';
const LS_SIDEBAR_COLLAPSED = 'files.sidebarCollapsed';

function withUserIdParam(path) {
  // Append the active user_id as a query param. The backend prefers the
  // JWT when valid, but falls back to this — same pattern as the other
  // admin pages — so the page still works if the cached token is stale.
  const uid = localStorage.getItem('auth_user_id') || '';
  if (!uid) return path;
  const sep = path.includes('?') ? '&' : '?';
  return path + sep + 'user_id=' + encodeURIComponent(uid);
}

async function apiFetch(path, opts = {}) {
  const headers = Object.assign({}, authHeaders(), opts.headers || {});
  // Avoid a CORS preflight on GETs by only setting Content-Type when
  // we're actually sending a body.
  if (opts.body && !('Content-Type' in headers)) {
    headers['Content-Type'] = 'application/json';
  }
  // Paths that already start with /api/ are absolute — used by the terminal
  // launcher panel to hit /api/v1/terminal/... endpoints. Everything else is
  // a files-relative subpath ('/tree?...', '/write', etc.) and gets the
  // /api/v1/files prefix.
  const url = path.startsWith('/api/') ? withUserIdParam(path) : (API_BASE + withUserIdParam(path));
  const res = await fetch(url, Object.assign({}, opts, { headers }));
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; } catch (_) {}
    throw new Error(detail || ('HTTP ' + res.status));
  }
  return res.json();
}

// ── Sidebar resize / toggle ───────────────────────────────────────

function initSidebarResize() {
  const sidebar = document.getElementById('files-sidebar');
  const handle = document.getElementById('files-resize-handle');
  if (!sidebar || !handle) return;

  // Restore width
  const savedWidth = parseInt(localStorage.getItem(LS_SIDEBAR_WIDTH), 10);
  if (!isNaN(savedWidth) && savedWidth >= 160) sidebar.style.width = savedWidth + 'px';

  if (localStorage.getItem(LS_SIDEBAR_COLLAPSED) === 'true') {
    sidebar.classList.add('collapsed');
  }

  let dragging = false;
  handle.addEventListener('mousedown', (e) => {
    dragging = true;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    e.preventDefault();
  });
  document.addEventListener('mousemove', (e) => {
    if (!dragging) return;
    const editorRect = document.getElementById('admin-tools').getBoundingClientRect();
    let w = e.clientX - editorRect.left;
    w = Math.max(160, Math.min(600, w));
    sidebar.style.width = w + 'px';
  });
  document.addEventListener('mouseup', () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    try { localStorage.setItem(LS_SIDEBAR_WIDTH, parseInt(sidebar.style.width, 10) || ''); } catch (_) {}
  });

  const toggle = document.getElementById('files-sidebar-toggle');
  if (toggle) {
    toggle.addEventListener('click', () => {
      const collapsed = sidebar.classList.toggle('collapsed');
      try { localStorage.setItem(LS_SIDEBAR_COLLAPSED, String(collapsed)); } catch (_) {}
    });
  }
}

// ── Public API ────────────────────────────────────────────────────

function initFiles() {
  if (initialised) return;
  initialised = true;

  initSidebarResize();
  // Build the sidebar strip's view-switch icons from the admin page catalog
  // BEFORE the view switcher / maximize init apply their active highlighting —
  // the buttons must exist first. __applyAdminPanelOrder → __buildAdminStrip
  // (header-build.js) renders them from window.__pagesCatalog.admin (the same
  // drop-in catalog the main header uses). startAdminTools rebuilds once the
  // catalog has loaded, in case this ran before the boot fetch resolved.
  if (window.__applyAdminPanelOrder) window.__applyAdminPanelOrder();
  initSidebarViewSwitcher();
  injectPanelCollapseButtons();
  initSidebarMaximize();
}

// ── Sidebar state cycle ───────────────────────────────────────────
//
// Desktop toggles split ↔ strip (no "max" — the editor is always visible
// alongside the sidebar). Mobile cycles strip ↔ max (no usable split view
// on small screens). The strip column itself is always rendered; in strip
// state it's the only thing visible, in split/max it's the left rail next
// to the active panel.

const LS_SIDEBAR_STATE = 'files.sidebarState';   // 'split' | 'max' | 'strip'

// isMobileLayout now lives in ./layout.js (imported at the top of this file)
// so leaf modules like db/tables.js can use it without importing this large
// module — that import created a db/* <-> files.js cycle.

// Give every sidebar panel its own collapse / switch-display control. This
// replaces the single shared strip button: each view owns the control in its
// panel header, so a dropped-in view's panel gets one for FREE (no markup in
// the view's own HTML). Idempotent. Panels that ship a .files-sidebar-header
// get the button prepended (far-left, strip-adjacent — it collapses toward the
// icon strip); panels without one get a minimal header created to hold it.
function injectPanelCollapseButtons() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  sidebar.querySelectorAll('.files-sidebar-panel').forEach((panel) => {
    if (panel.querySelector('.files-panel-collapse-btn')) return;
    let header = panel.querySelector(':scope > .files-sidebar-header');
    if (!header) {
      header = document.createElement('div');
      header.className = 'files-sidebar-header files-panel-collapse-header';
      panel.insertBefore(header, panel.firstChild);
    }
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'files-icon-btn files-panel-collapse-btn';
    btn.title = 'Collapse sidebar';
    btn.setAttribute('aria-label', 'Collapse sidebar');
    btn.innerHTML = '<i data-lucide="chevrons-left" class="lucide-icon"></i>';
    header.insertBefore(btn, header.firstChild);
  });
  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

function initSidebarMaximize() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Restore prior state. On mobile, fold 'split' into 'strip'.
  let saved = localStorage.getItem(LS_SIDEBAR_STATE) || 'split';
  if (saved !== 'split' && saved !== 'max' && saved !== 'strip') saved = 'split';
  if (isMobileLayout() && saved === 'split') saved = 'strip';
  setSidebarState(saved);

  // Delegate clicks on the per-view collapse buttons and the strip's
  // view-switch buttons. Each view's panel header carries its own
  // .files-panel-collapse-btn (injected by injectPanelCollapseButtons); they
  // all drive the same cycleSidebarState — collapse to the icon strip on
  // desktop, switch panel↔main on mobile.
  sidebar.addEventListener('click', (e) => {
    const cycle = e.target.closest('.files-panel-collapse-btn');
    if (cycle && sidebar.contains(cycle)) {
      e.stopPropagation();
      cycleSidebarState();
      return;
    }
    const stripView = e.target.closest('.files-strip-view');
    if (stripView && sidebar.contains(stripView)) {
      e.stopPropagation();
      const v = stripView.dataset.view;
      if (!v) return;
      // Belt-and-braces: non-admin clicking a strip view should re-show the
      // restricted overlay rather than activate the sub-page. The parent
      // gate in startAdminTools normally hides this strip already; this
      // covers DevTools-style bypasses where someone unhides #admin-tools
      // without re-running /check-access.
      if (!isAdmin) {
        showRestrictedModal();
        return;
      }
      applySidebarView(v);
      try { localStorage.setItem(LS_SIDEBAR_VIEW, v); } catch (_) {}
      // Other views need the panel column visible — expand from strip
      // mode. Settings has no panel and renders full-bleed via CSS, so
      // leave the data-state alone (and skip the mobile 'max' that
      // would otherwise hide the settings main).
      if (v !== 'settings') {
        setSidebarState(isMobileLayout() ? 'max' : 'split');
      }
    }
  });
}

function cycleSidebarState() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  const cur = sidebar.dataset.state || 'split';
  const mobile = isMobileLayout();
  let next;
  if (mobile) {
    // 2-stage: strip ↔ max
    next = (cur === 'max') ? 'strip' : 'max';
  } else {
    // 2-stage: split ↔ strip (max removed on desktop)
    next = (cur === 'strip') ? 'split' : 'strip';
  }
  setSidebarState(next);
}

// Not exported: db/tables.js reaches this via app.setSidebarState (registered
// below) to avoid a db/* <-> files.js import cycle.
function setSidebarState(state) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  if (state !== 'split' && state !== 'max' && state !== 'strip') state = 'split';
  if (isMobileLayout() && state === 'split') state = 'strip';
  // 'max' only exists on mobile — coerce stale localStorage to 'split' on desktop.
  if (!isMobileLayout() && state === 'max') state = 'split';
  sidebar.dataset.state = state;
  sidebar.classList.toggle('maximized', state === 'max');
  sidebar.classList.toggle('strip',     state === 'strip');

  // Keep each panel's collapse button's icon + title in sync. They only show
  // while a panel is expanded (panels are hidden in strip mode), so in practice
  // they always read "Collapse"; the strip-mode branch is harmless bookkeeping.
  const iconName = state === 'strip' ? 'chevrons-right' : 'chevrons-left';
  const title    = state === 'strip' ? 'Expand sidebar' : 'Collapse sidebar';
  sidebar.querySelectorAll('.files-panel-collapse-btn').forEach((b) => {
    b.title = title;
    b.innerHTML = '<i data-lucide="' + iconName + '" class="lucide-icon"></i>';
  });

  // Strip is always rendered now; panels follow the current view (via
  // applySidebarView) but stay hidden when in strip mode.
  applySidebarView(sidebar.dataset.view || 'explorer');

  // icons.js auto-renders via MutationObserver — no manual refresh needed
  try { localStorage.setItem(LS_SIDEBAR_STATE, state); } catch (_) {}
}

// Exposed on `app` so db/tables.js can collapse the sidebar after a table is
// selected WITHOUT importing files.js (that import was the db/* <-> files.js
// cycle edge). files.js is always loaded before the DB viewer it launches, so
// app.setSidebarState is set well before any table click.
app.setSidebarState = setSidebarState;

// ── Sidebar view switcher (Explorer ↔ Git Control) ─────────────

function initSidebarViewSwitcher() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Restore last view (default: explorer)
  const stored = localStorage.getItem(LS_SIDEBAR_VIEW);
  const want = (stored && (stored in VIEW_MAIN_ID || _adminCatalogPage(stored)))
    ? stored : 'explorer';
  applySidebarView(want);
  // The toggle icons live in BOTH panel headers (so each header has its
  // own copy). Delegate click handling at the sidebar level so we catch
  // whichever pair is currently rendered.
  sidebar.addEventListener('click', (e) => {
    const btn = e.target.closest('.files-view-toggle-btn');
    if (!btn || !sidebar.contains(btn)) return;
    const v = btn.dataset.view;
    if (!v) return;
    // Click on the currently-active (greyed-out) icon = no-op.
    if (btn.classList.contains('active')) return;
    applySidebarView(v);
    try { localStorage.setItem(LS_SIDEBAR_VIEW, v); } catch (_) {}
  });
}

const VIEW_TITLE = {
  explorer: 'File Manager',
  terminal: 'Terminal launchers',
};
const VIEW_SWITCH = {
  explorer: 'file manager',
  terminal: 'terminal launchers',
};

// Sidebar views used to be "built-ins" wired inline in this file; every one —
// Explorer and Terminal included — is now a folder drop-in driven from its
// descriptor's entry/start/stop. This map is therefore empty: `isBuiltin` is
// always false, so applySidebarView dispatches EVERY view dynamically. (Kept as
// the escape hatch should a view ever need inline wiring again.)
const VIEW_MAIN_ID = {};

// ── Drop-in admin views ────────────────────────────────────────────────────
// The eight views above are the BUILT-INS — their lifecycles (panel + main
// renders, poll loops) are wired inline in applySidebarView, exactly as
// tabs.js keeps startAgents/stopAgents etc. as static hooks. A brand-new admin
// view dropped in as ui/admin-tools/<id>/page.json is NOT listed here; it is
// driven generically from its descriptor's entry/start/stop via a dynamic
// import (mirrors tabs.js _startPage/_stopDynamic). Its main pane must be a
// <main class="files-main" id="files-<id>-main" data-view="<id>"> under
// #admin-tools; the strip button + #files-<id>-main swap come for free.
function _adminCatalogPage(id) {
  try {
    const c = window.__pagesCatalog;
    const list = (c && Array.isArray(c.admin)) ? c.admin : [];
    return list.find((p) => p.id === id) || null;
  } catch (_) { return null; }
}
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  DYN-IMPORT-CACHE — evict on failure, never cache a rejection.         ║
// ║  A failed dynamic import (network hiccup, mid-save edit, 404) must     ║
// ║  NOT poison the module cache for the rest of the session — the next    ║
// ║  activation MUST retry with a fresh import.  This applies to BOTH      ║
// ║  _dynAdminMods here and _dynMods in tabs.js (same contract).           ║
// ║  DO NOT simplify back to `try { … } catch { Promise.reject(e) }` —    ║
// ║  that caches the rejection forever, leaving the page white-boxed       ║
// ║  (toolbar visible, no data) with no recovery short of a full reload.   ║
// ║  The `.catch()` in callers only logs — it does not retry the import.   ║
// ║  (grep `DYN-IMPORT-CACHE` to find both sites).                          ║
// ╚══════════════════════════════════════════════════════════════════════════╝
const _dynAdminMods = {};      // view id → Promise<module> (evicted on failure)
const _dynAdminPending = {};   // view id → true while startView promise is in-flight
let _activeDynAdminView = null;
function _dynAdminModule(id, entry) {
  if (!_dynAdminMods[id]) {
    // DYN-IMPORT-CACHE: evict the rejection so the next activation retries.
    _dynAdminMods[id] = Promise.resolve()
      .then(() => import(new URL(entry, document.baseURI).href))
      .catch((e) => { delete _dynAdminMods[id]; throw e; });
  }
  return _dynAdminMods[id];
}
function _startDynAdminView(id) {
  // Guard: if startView for this id is already in-flight, skip — avoid
  // double-starting the same view (e.g. initSidebarViewSwitcher then
  // startAdminTools racing on the same stored view).
  if (_dynAdminPending[id]) return;
  const p = _adminCatalogPage(id);
  if (p && p.entry && p.start) {
    _dynAdminPending[id] = true;
    _dynAdminModule(id, p.entry)
      .then((m) => { const fn = m && m[p.start]; if (typeof fn === 'function') fn(); })
      .catch((e) => {
        // Include the actual error message so debugConsole's JSON.stringify
        // doesn't reduce Error objects to the useless "{}".
        const detail = (e && e.message) ? e.message : String(e);
        console.error('admin view start ' + id + ' failed: ' + detail, e);
      })
      .finally(() => { _dynAdminPending[id] = false; });
  }
}
function _stopDynAdminView(id) {
  _dynAdminPending[id] = false;
  const p = _adminCatalogPage(id);
  if (p && p.entry && p.stop && _dynAdminMods[id]) {
    _dynAdminMods[id]
      .then((m) => { const fn = m && m[p.stop]; if (typeof fn === 'function') fn(); })
      .catch(() => {});
  }
}

function applySidebarView(view) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  // Accept any built-in view OR a catalog-known drop-in admin view; fall back
  // to explorer for anything unrecognised.
  const isBuiltin = (view in VIEW_MAIN_ID);
  if (!isBuiltin && !_adminCatalogPage(view)) view = 'explorer';
  sidebar.dataset.view = view;
  // Mirror the active sub-view into the address bar (?tab=admin-tools&view=<id>)
  // so a sub-view is shareable and survives a refresh. tabs.js (window.__setMainSubView)
  // guards this to the live Admin Tools tab; Explorer (the default view) collapses
  // to the bare ?tab=admin-tools. Best-effort — never blocks the view swap.
  try {
    if (typeof window.__setMainSubView === 'function') {
      window.__setMainSubView('admin-tools', view, 'explorer');
    }
  } catch (_) { /* address-bar sync is best-effort */ }
  // Update aria-selected on every view-toggle button (in panel headers
  // and in the strip). The Settings strip button now lives in
  // .files-strip-view, so it's covered by the same selector.
  sidebar.querySelectorAll(
    '.files-view-toggle-btn, .files-strip-view'
  ).forEach((b) => {
    const active = b.dataset.view === view;
    b.classList.toggle('active', active);
    b.setAttribute('aria-selected', active ? 'true' : 'false');
    if (b.classList.contains('files-view-toggle-btn')) {
      if (active) {
        b.setAttribute('aria-disabled', 'true');
        b.title = VIEW_TITLE[view] + ' (current view)';
      } else {
        b.removeAttribute('aria-disabled');
        b.title = 'Switch to ' + (VIEW_SWITCH[b.dataset.view] || 'explorer');
      }
    }
  });
  // In strip mode all sidebar panels stay hidden; otherwise the matching
  // panel shows. No panel has data-view="settings", so all panels hide
  // naturally for that view — the CSS rule on data-view="settings"
  // additionally collapses the panel column to strip width.
  const state = sidebar.dataset.state || 'split';
  sidebar.querySelectorAll('.files-sidebar-panel').forEach((p) => {
    const explorerMode = sidebar.dataset.explorerMode || 'files';
    const wrongExplorerMode = view === 'explorer'
      && p.dataset.explorerMode
      && p.dataset.explorerMode !== explorerMode;
    p.hidden = (state === 'strip') || (p.dataset.view !== view) || wrongExplorerMode;
  });
  // Right-pane swap: hide every per-view main except the one matching
  // `view`. Built-ins map via VIEW_MAIN_ID; a drop-in view follows the
  // #files-<id>-main convention (overridable by its descriptor `mount`).
  const dynPage = isBuiltin ? null : _adminCatalogPage(view);
  const wantId = VIEW_MAIN_ID[view]
    || (dynPage && dynPage.mount && dynPage.mount.replace(/^#/, ''))
    || ('files-' + view + '-main');
  document.querySelectorAll('#admin-tools .files-main[data-view]').forEach((el) => {
    el.hidden = (el.id !== wantId);
  });
  // Non-admin (or pre-check-access): stop here. Skip per-view background
  // work (polls, fetches, lazy panel renders) so a non-admin who somehow
  // reaches this code path — or the brief window before startAdminTools'
  // /check-access has set the local isAdmin flag — doesn't kick off
  // database/loop/git polling. startAdminTools re-calls applySidebarView
  // after the access check so the side effects fire for real admins.
  if (!isAdmin) return;
  // Every admin view — Explorer and Terminal included — is a drop-in now: its
  // per-view work (tree + production load, git +/- badges, terminal panels,
  // database/loop polling) runs from its own start/stop hook via the dynamic
  // dispatch below.
  // Stop the one we navigated away from, then start the active one.
  // then start the active one via its descriptor entry/start/stop. Mirrors the
  // tabs.js page runtime; built-in views are already handled above.
  if (_activeDynAdminView && _activeDynAdminView !== view) {
    _stopDynAdminView(_activeDynAdminView);
    _activeDynAdminView = null;
  }
  if (!isBuiltin) {
    _startDynAdminView(view);
    _activeDynAdminView = view;
  }
}

// Exposed so drop-in sidebar views (e.g. the Terminal module) can jump the
// sidebar to themselves without importing the frame. Mirrors app.setSidebarState.
window.__applySidebarView = applySidebarView;

// Relocate detached markup (App Config and the Database viewer) into the
// Admin Tools layout. The originals are parked at the bottom of #stage
// in index.html so this module owns their final mount point. Idempotent.
export function relocateAdminToolsContainers() {
  // Database viewer — sidebar host receives #db-sidebar; the main host
  // receives #db-toolbar then #db-table-view. The empty #db-panel and
  // #db-viewer wrappers are dropped once their children have been moved.
  const dbSbHost = document.getElementById('db-sidebar-host');
  const dbMainHost = document.getElementById('files-database-main');
  const dbSidebar = document.getElementById('db-sidebar');
  const dbToolbar = document.getElementById('db-toolbar');
  const dbTableView = document.getElementById('db-table-view');
  if (dbSbHost && dbSidebar && dbSidebar.parentElement !== dbSbHost) {
    dbSbHost.appendChild(dbSidebar);
  }
  if (dbMainHost && dbToolbar && dbToolbar.parentElement !== dbMainHost) {
    dbMainHost.appendChild(dbToolbar);
  }
  if (dbMainHost && dbTableView && dbTableView.parentElement !== dbMainHost) {
    dbMainHost.appendChild(dbTableView);
  }
  const dbPanel = document.getElementById('db-panel');
  if (dbPanel && !dbPanel.children.length) dbPanel.remove();
  const dbViewer = document.getElementById('db-viewer');
  if (dbViewer && !dbViewer.children.length) dbViewer.remove();
  const dbPark = document.getElementById('db-viewer-park');
  if (dbPark && !dbPark.children.length) dbPark.remove();
}

export async function startAdminTools() {
  initFiles();
  // Check admin access; show overlay if not
  let accessInfo = { is_admin: false, user_id: '', authenticated: false };
  try {
    accessInfo = await apiFetch('/check-access');
  } catch (e) {
    accessInfo = { is_admin: false, user_id: '', authenticated: false, error: e.message };
  }
  isAdmin = !!accessInfo.is_admin;

  const overlay = document.getElementById('files-restricted-overlay');
  const editor = document.getElementById('admin-tools');

  // Non-admin: show the Restricted Access overlay and stop. The backend rejects
  // every file/admin call on its own (app/api/files.py → _require_admin), so
  // this is the UX layer of the gate — and the header tab is normally hidden
  // for non-admins anyway (a deep link or stale tab can still land here).
  if (!isAdmin) {
    if (editor) editor.style.display = 'none';
    if (overlay) overlay.style.display = 'flex';
    const diag = document.getElementById('files-restricted-diag');
    if (diag) {
      diag.textContent = accessInfo.authenticated
        ? `Signed in as ${accessInfo.user_id || 'a non-admin account'}, which is not an admin.`
        : 'You are not signed in.';
    }
    // Full-screen the restricted view: hide the chat side panel (no agent
    // context to chat in here) so the main panel fills the window. Reverted on
    // leave (stopAdminTools) or once admin access is confirmed below.
    document.body.classList.add('admin-restricted');
    return;
  }

  // Admin confirmed — make sure the restricted full-screen layout is cleared
  // (e.g. an earlier non-admin check on this tab, now superseded).
  _clearRestrictedLayout();

  if (overlay) overlay.style.display = 'none';
  if (editor) editor.style.display = 'flex';

  // Build the strip from the authoritative page catalog. initFiles() built it
  // from whatever was cached/memoized synchronously; this re-runs once the boot
  // fetch has resolved so a freshly-dropped admin view is present before we
  // activate a view. Also injects each panel's collapse control.
  try {
    if (window.__loadPagesCatalog) {
      const cat = await window.__loadPagesCatalog();
      if (window.__buildAdminStrip) window.__buildAdminStrip(cat && cat.admin);
    }
  } catch (_) {}
  injectPanelCollapseButtons();

  // Re-apply the current sidebar view now that admin status is confirmed.
  // initFiles() called applySidebarView() before /check-access resolved, at
  // which point the cached isAdmin() may still have been false — so the
  // per-view side effects (git/terminal/database panels) were skipped by
  // the guard. Re-running here lets them fire for the real admin.
  // Activating the view fires its drop-in start hook (Explorer's startView loads
  // the tree + production state + open tabs; Terminal's opens its launcher, etc.).
  const sb = document.getElementById('files-sidebar');
  const view = sb?.dataset.view || 'explorer';
  applySidebarView(view);
}

// Exit the restricted full-screen layout and restore the user's saved chat
// visibility. Safe to call when not restricted (the class toggle is a no-op).
function _clearRestrictedLayout() {
  if (!document.body.classList.contains('admin-restricted')) return;
  document.body.classList.remove('admin-restricted');
  // Reassert the user's chat panel preference (the restricted CSS only forced
  // it hidden; this brings it back exactly as the user last left it).
  try { window.__applyChatVisible(window.__getChatVisible()); } catch (_) {}
}

export function stopAdminTools() {
  // Leaving Admin Tools: drop the restricted full-screen layout so the chat
  // panel returns on whatever tab the user switches to.
  _clearRestrictedLayout();
  // Quiet background loops; the view stays selected so polling resumes when the
  // user returns. Every admin view is a drop-in now, so the active view's
  // descriptor stop (e.g. Settings' stopView → stopAppConfig/stopBilling)
  // quiets its pollers.
  if (_activeDynAdminView) { try { _stopDynAdminView(_activeDynAdminView); } catch (_) {} }
}

// Re-arm the one-time init guard so a RE-MOUNT into a fresh container re-wires
// its (fresh) sidebar resize/toggle/maximize and builds a new strip. Admin Tools
// is now embedded as a tab inside the Instances page (see
// ui/main-panel/app/instances.js _onAdminToolsTabRendered), where the host
// is created/destroyed on each tile open — the standalone main-panel page usage
// never calls this: its #tab-admin-tools mount persists, so `initialised` stays
// true and listeners are never double-wired.
export function resetAdminToolsForRemount() {
  initialised = false;
}

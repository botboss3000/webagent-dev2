'use strict';

/**
 * App Settings tab — global feature toggles, page layout, and boot/startup
 * settings. Page audience and app-user policy live on Instances → Users.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { app } from '../../../../shared/js/state.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { _fetch, _qs, _esc } from '../utils.js';
// Unified save-status indicator (spinner → ✓ / ⚠-with-revert, centred ON TOP of
// the control). One save language for every toggle / dropdown / input across the
// app config panels — see the ui_toggle_list skill "Unified save-status
// indicator". Replaces the older inline `.agents-autosave-check` tick here.
import { _markSaving, _flashSaveCheck } from '../../../../shared/js/dom-utils.js';
// Palette token metadata, preset themes and colour math — the SHARED single
// source (ui/shared/js/appearance-editor.js), also imported by the per-user theme
// editor (ui/shared/js/appearance-me.js) so the two editors can never drift.
// Aliased to the historical underscore names this file already uses.
import {
  TOKEN_META as _TOKEN_META,
  APP_DEFAULTS as _APP_DEFAULTS,
  PRESETS as _PRESETS,
  FOLLOW_ALPHA as _FOLLOW_ALPHA,
  asHex as _asHex,
  splitColor as _splitColor,
  combineColor as _combineColor,
} from '../../../../shared/js/appearance-editor.js';
import { openIconPicker } from '../../../../shared/js/icon-picker.js';
// App Functions table — the background app services (Session Namer, Context
// Control compaction, Render Recorder) split out of the ability tables. Rendered
// from the ability catalog's `app_functions` list. See ./app-functions.js.
import { initAppFunctions } from './app-functions.js';
// LIVE theme switch (Light · Dark · System) in the Design header reuses the
// user-dropdown's theme logic so the two controls never diverge. setTheme owns
// apply + persist; highlightThemeOption syncs every `.theme-option` on the page.
import { setTheme, getSavedTheme, highlightThemeOption } from '../../../../shared/js/user-panel.js';
// Always-on display toggle uses the Screen Wake Lock API (screen-wake.js).
import { enableWakeLock, disableWakeLock } from '../../../../shared/js/screen-wake.js';
// The Deploy card moved to its own "Data Settings" tab (renamed "Deployment") —
// its wiring now lives in ../data-settings/data-settings.js, not here.

// ───────────────────────────────────────────────────────────────────────────

function _initAppSettings() {
  const saveBtn = _qs('ac-app-settings-save');
  if (saveBtn) saveBtn.addEventListener('click', _saveAppSettings);
  const gpBtn = _qs('ac-global-prompt-save');
  if (gpBtn) gpBtn.addEventListener('click', _saveGlobalPrompt);
  _initBootAnimation();
  // Startup & Boot rows are now STANDARD config rows — the toggle / dropdown
  // sits inline on the right, no expandable body — so they need no row wiring
  // here (the selects/checkboxes are wired by their _initBoot* handlers).
  // Remote Access moved to the Data Settings tab (below the Database card) — its
  // two expandable rows are now wired in ../data-settings/data-settings.js.
  // Stream Buffer now lives in the App Functions group (moved out of the retired
  // Advanced group). Same head-click expand wiring; its body's control ids are
  // owned by ui/shared/js/storage.js. Config Files + User BYOD moved to the Data
  // Settings tab (wired there) — the Advanced group is gone.
  _wireBootRow('ac-row-streambuffer', null);
  _wireBootRow('ac-hide-header-kb-row', null);
  _wireBootRow('ac-voice-dictation-row', null);
  _wireBootRow('ac-max-active-sessions-row', null);
  _wireBootRow('ac-default-max-tokens-row', null);
  _wireBootRow('ac-resume-recovery-row', null);
  _wireBootRow('ac-closing-gate-row', null);
  _wireBootRow('ac-pre-tool-progress-message-gate-row', null);
  _wireBootRow('ac-assertions-gate-row', null);
  _wireBootRow('ac-widget-launcher-row', null);
  // The Deploy card's rows + wiring moved to the Data Settings tab
  // (../data-settings/data-settings.js).
  _initBootSplash();
  _initAllowUserAppearance();
  _initSafetySwitch();
  _initSessionNotifications();
  _initAlwaysOnDisplay();
  _initSiteName();
  _initVoiceDictationPolicy();
  _initMaxActiveSessions();
  _initDefaultMaxTokens();
  _initResumeRecovery();
  _initClosingMessageGate();
  // Pre-tool progress-message gate — same gate family as the closing gate above, so its
  // row wiring and init sit directly beside it (see _initPreToolProgressMessageGate).
  _initPreToolProgressMessageGate();
  // Assertions gate — same run-contract gate family as the closing gate above,
  // and the same requirement-compliance budget shape (see _initAssertionsGate).
  _initAssertionsGate();
  _initTranscriptOrderRecovery();
  _initWidgetLauncher();
  _initHideHeaderKeyboard();
  // Advanced row inside Model Settings — expandable compact row with checkboxes
  _wireAdvancedRow();
  _initMainPanelPages();
  _initAdminPanelPages();
  _initAppFunctions();
  _initAppearance();
}

// App Functions table — background app services (not agent abilities). Rendered
// from the ability catalog's `app_functions` list into #ac-app-functions-list.
function _initAppFunctions() {
  initAppFunctions().catch(() => { /* non-fatal — the rest of the page still loads */ });
}

// -- Page-assistant chat pill --------------------------------------------------
// The floating "advanced chat pill" at the bottom of App Settings. Typing hints,
// the typewriter suggestions, Tab-to-fill and the context-wrapped message are all
// owned by the shared page-assistant engine (../page-assistant.js), which here
// runs in COMPOSER mode so it also owns this page's static #ac-pa-* pill's
// send / Enter / voice / file-uploads. The prompt it hands WebAgent changes with
// the section the mouse is over (the data-pa-area groups in app-settings.html);
// on send it opens a floating WebAgent (the manager) chat via spawnWebagentPageChat.
// Text catalog (placeholders + idea hints + prompts) lives in
// app/defaults/app-prompts.json -> page_assistants.app_settings (edit + reload).
function _wireBootRow(rowId, selectId) {
  const row = _qs(rowId);
  if (!row) return;
  const head = row.querySelector(':scope > .ac-ability-row');
  head?.addEventListener('click', (e) => {
    // Don't toggle when interacting with the control itself.
    if (e.target.closest('select, input, button, a, label')) return;
    row.classList.toggle('expanded');
  });
}

// Wire the model action row. Two independent icon buttons; the row itself is
// NOT clickable:
//  • the Advanced (sliders) button toggles the advanced-options body below it;
//  • the `+` button toggles the add-new-model configurator form above the row
//    (the table's `ac-cfg-collapsed` class hides the 4 configurator rows).
function _wireAdvancedRow() {
  const row = _qs('ac-model-advanced-row');
  if (!row) return;
  const advToggle = _qs('ac-model-advanced-toggle');
  advToggle?.addEventListener('click', () => { row.classList.toggle('expanded'); });
  const addToggle = _qs('ac-model-add-toggle');
  const table = _qs('ac-model-config');
  addToggle?.addEventListener('click', () => {
    table?.classList.toggle('ac-cfg-collapsed');
  });
}

// ── Main Panel Pages & Admin Panel Pages (catalog-driven) ──────────────────
// SISTER-PANEL: AGENT-ABILITY-TABLE — these two lists are sister panels and must
// stay mirrored in design. Both render from the SAME page catalog
// (GET /api/v1/pages/catalog, built from drop-in ui/<page>/page.json + admin
// overrides) and persist admin changes SERVER-SIDE (data/config/
// main-panel-pages.json + admin-panel-pages.json) so every user sees the admin's
// chosen order, names and icons. Visibility is managed on Users → Page access.
// There is no hardcoded page list; adding ui/<page>/page.json adds it to catalog.

let _pageCatalog = { main: [], admin: [] };

async function _fetchPageCatalog(force) {
  try {
    if (force && typeof window.__reloadPagesCatalog === 'function') {
      _pageCatalog = await window.__reloadPagesCatalog();
    } else if (typeof window.__loadPagesCatalog === 'function') {
      _pageCatalog = await window.__loadPagesCatalog();
    } else {
      const r = await fetch('/api/v1/pages/catalog', { cache: 'no-store' });
      _pageCatalog = await r.json();
    }
  } catch (e) {
    console.warn('app-settings: page catalog fetch failed', e);
  }
  if (!_pageCatalog || !Array.isArray(_pageCatalog.main)) _pageCatalog = { main: [], admin: [] };
  return _pageCatalog;
}

async function _savePagesOrder(scope, orderedIds) {
  const pages = {};
  orderedIds.forEach((id, idx) => { pages[id] = idx; });
  try {
    await fetch(`/admin/integrations/pages/${scope}/order`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ pages }),
    });
  } catch (e) { console.warn('save page order failed', e); }
}

async function _savePageOverride(scope, id, body) {
  try {
    const res = await fetch(`/admin/integrations/pages/${scope}/${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    return res.ok;
  } catch (e) { console.warn('save page override failed', e); return false; }
}

// Re-apply the live UI for a scope after a change: main rebuilds the header tab
// strip from the fresh catalog; admin re-applies the sidebar strip order.
function _applyLive(scope) {
  if (scope === 'main') {
    if (typeof window.__buildHeader === 'function') {
      try { window.__buildHeader(_pageCatalog.main); } catch (_) {}
    }
  } else {
    if (typeof window.__applyAdminPanelOrder === 'function') {
      try { window.__applyAdminPanelOrder(); } catch (_) {}
    }
  }
}

const _PANEL_CFG = {
  main:  { listId: 'ac-main-panel-list',  where: 'header' },
  admin: { listId: 'ac-admin-panel-list', where: 'sidebar' },
};

function _renderPanelList(scope) {
  const cfg = _PANEL_CFG[scope];
  const list = _qs(cfg.listId);
  if (!list) return;

  const pages = (_pageCatalog[scope] || []);
  list.innerHTML = '';

  pages.forEach((page) => {
    const id = page.id;
    const isLocked = !!page.locked;

    const item = document.createElement('div');
    item.className = 'ac-main-panel-item ac-ability-row' + (isLocked ? ' ac-mp-locked' : '');
    item.draggable = !isLocked;
    item.dataset.pageId = id;

    // Drag handle
    const handle = document.createElement('span');
    handle.className = 'ac-mp-drag-handle';
    handle.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/></svg>';

    // Icon button — opens the shared searchable icon picker
    const iconBtn = document.createElement('button');
    iconBtn.type = 'button';
    iconBtn.className = 'ac-ability-icon ac-mp-icon-btn';
    iconBtn.title = 'Change icon';
    iconBtn.innerHTML = `<i data-lucide="${_esc(page.icon || 'square')}" class="lucide-icon dash-ico-18"></i>`;
    iconBtn.addEventListener('click', async () => {
      const chosen = await openIconPicker({ current: page.icon || '', title: 'Choose a page icon' });
      if (!chosen || chosen === page.icon) return;
      // Optimistic + in-place (no full re-render) so the save overlay survives on
      // this button — spinner → ✓ over the icon, or ⚠ on failure (icon unchanged).
      _markSaving(iconBtn);
      const ok = await _savePageOverride(scope, id, { icon: chosen });
      if (ok === false) { _flashSaveCheck(iconBtn, false); return; }
      page.icon = chosen;
      iconBtn.innerHTML = `<i data-lucide="${_esc(chosen)}" class="lucide-icon dash-ico-18"></i>`;
      if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
      await _fetchPageCatalog(true);
      _applyLive(scope);
      _flashSaveCheck(iconBtn, true);
    });

    // Editable name
    const label = document.createElement('div');
    label.className = 'ac-ability-label';
    const nameInput = document.createElement('input');
    nameInput.type = 'text';
    nameInput.className = 'ac-mp-name-input';
    nameInput.value = page.label || id;
    nameInput.setAttribute('aria-label', 'Page name');
    const _commitName = async () => {
      const v = nameInput.value.trim();
      if (v === (page.label || '')) return;
      // Optimistic + in-place (no full re-render) so the overlay survives on the
      // name field — spinner → ✓, or ⚠ + revert to the prior name on failure.
      _markSaving(label);
      const ok = await _savePageOverride(scope, id, { label: v });
      if (ok === false) {
        nameInput.value = page.label || id;   // revert
        _flashSaveCheck(label, false);
        return;
      }
      page.label = v;
      await _fetchPageCatalog(true);
      _applyLive(scope);
      _flashSaveCheck(label, true);
    };
    nameInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') { e.preventDefault(); nameInput.blur(); }
    });
    nameInput.addEventListener('blur', _commitName);
    // Don't let typing/selecting in the field start a drag.
    nameInput.addEventListener('mousedown', (e) => e.stopPropagation());
    label.appendChild(nameInput);

    item.appendChild(handle);
    item.appendChild(iconBtn);
    item.appendChild(label);

    // Drag-and-drop reorder
    if (!isLocked) {
      item.addEventListener('dragstart', (e) => {
        item.classList.add('dragging');
        e.dataTransfer.effectAllowed = 'move';
        e.dataTransfer.setData('text/plain', id);
      });
      item.addEventListener('dragend', () => {
        item.classList.remove('dragging');
        list.querySelectorAll('.ac-main-panel-item').forEach(el => el.classList.remove('drag-over'));
      });
    }
    item.addEventListener('dragover', (e) => {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      list.querySelectorAll('.ac-main-panel-item').forEach(el => el.classList.remove('drag-over'));
      item.classList.add('drag-over');
    });
    item.addEventListener('dragleave', () => item.classList.remove('drag-over'));
    item.addEventListener('drop', async (e) => {
      e.preventDefault();
      item.classList.remove('drag-over');
      const fromId = e.dataTransfer.getData('text/plain');
      const toId = id;
      if (!fromId || fromId === toId) return;

      // Build the new id order from the current rendered order.
      const ids = pages.map(p => p.id);
      const fromIdx = ids.indexOf(fromId);
      if (fromIdx < 0 || ids.indexOf(toId) < 0) return;
      ids.splice(fromIdx, 1);
      const toIdx = ids.indexOf(toId);
      ids.splice(toIdx, 0, fromId);

      await _savePagesOrder(scope, ids);
      await _fetchPageCatalog(true);
      _renderPanelList(scope);
      _applyLive(scope);
    });

    list.appendChild(item);
  });

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

async function _initMainPanelPages() {
  await _fetchPageCatalog(false);
  _renderPanelList('main');
}

async function _initAdminPanelPages() {
  await _fetchPageCatalog(false);
  _renderPanelList('admin');
}

// Re-apply the admin sidebar strip from the catalog. The strip's view-switch
// buttons are now BUILT from the `admin` catalog by __buildAdminStrip (in
// header-build.js) — the same drop-in source the main header uses — so order,
// label, icon and hidden all flow from one place and a freshly-dropped admin
// view appears with no edits here. Exposed so files.js can (re)build the strip
// on Admin Tools init and so an App Settings edit can refresh it live.
function _applyAdminPanelOrder() {
  // Prefer the global boot catalog (set by header-build.js) so this also works
  // when called from files.js before App Settings has been opened; fall back to
  // the panel's own copy.
  const adminPages = (window.__pagesCatalog && Array.isArray(window.__pagesCatalog.admin))
    ? window.__pagesCatalog.admin
    : (_pageCatalog.admin || []);
  if (typeof window.__buildAdminStrip === 'function') {
    try { window.__buildAdminStrip(adminPages); } catch (_) {}
  }
}
window.__applyAdminPanelOrder = _applyAdminPanelOrder;

// Welcome landing page master switch — an app-WIDE setting saved server-side
// (splash_enabled in app-settings.json), unlike the per-browser boot rows above.
// When on, app/main.py serves the crawlable welcome landing at the front door (/)
// to new visitors and routes the app to /app; when off, the app loads directly at
// /. The checkbox state is loaded in _loadAppSettings; flipping it autosaves via
// the merge-friendly POST /admin/settings/app (server keeps every other key). The
// per-device "show for me" preference is separate (Manage Account + the landing's
// own checkbox, both via window.WA_SPLASH → the wa_seen_splash skip cookie).
// Whether the welcome-page folder actually has content to render. The front door
// (app/main.py → _render_landing_page) checks the SAME file before serving the
// landing, so a build that stripped ui/splash/splash-page/ has nothing to enable.
// We probe that file (HEAD) so the toggle can sit OFF and explain why, instead of
// being a switch with nothing behind it.
let _splashContentAvailable = true;

async function _checkSplashContent() {
  try {
    const r = await fetch('/ui/splash/splash-page/splash-page.html', { method: 'HEAD', cache: 'no-store' });
    _splashContentAvailable = r.ok;
  } catch (_) {
    _splashContentAvailable = true;   // fail-open: a probe glitch shouldn't lock the toggle
  }
  return _splashContentAvailable;
}

// Force the toggle off + show an inline note when there's no welcome page to show.
function _reflectSplashContent(chk) {
  const row = chk.closest('.ac-ability-row');
  if (!row) return;
  let note = row.querySelector('.ac-splash-missing-note');
  if (_splashContentAvailable) { if (note) note.remove(); return; }
  chk.checked = false;
  if (!note) {
    note = document.createElement('div');
    note.className = 'ac-ability-desc ac-splash-missing-note';
    note.style.marginTop = '4px';
    note.textContent = '⚠ No welcome page found — add content to ui/splash/splash-page to enable this.';
    (row.querySelector('.ac-ability-label') || row).appendChild(note);
  }
}

function _initBootSplash() {
  const chk = _qs('ac-boot-splash-enabled');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    // Nothing to turn on: the welcome-page folder was stripped from this build.
    // Revert and tell the admin exactly what to add.
    if (chk.checked && !_splashContentAvailable) {
      chk.checked = false;
      _flashSaveCheck(wrap, false, 'Add a welcome page to ui/splash/splash-page first');
      return;
    }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ splash_enabled: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save splash_enabled failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Allow per-user themes master switch — an app-WIDE setting saved server-side
// (allow_user_appearance in app-settings.json), mirroring the Welcome screen
// switch. The checkbox state is loaded in _loadAppSettings;
// flipping it autosaves via the merge-friendly POST /admin/settings/app (server
// keeps every other key). When on, /api/v1/auth/ui-config layers each signed-in
// user's saved overrides on top of the global theme and the account page
// (ui/shared/js/appearance-me.js) shows its "My appearance" editor.
function _initAllowUserAppearance() {
  const chk = _qs('ac-allow-user-appearance');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ allow_user_appearance: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save allow_user_appearance failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

function _initSafetySwitch() {
  const chk = _qs('ac-safety-switch');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ safety_lock_enabled: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save safety_lock_enabled failed', e);
      chk.checked = !chk.checked;
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Session completion notifications master switch — an app-WIDE setting saved
// server-side (session_completion_notifications in app-settings.json), sitting
// in the App Functions group. It gates the sliding notification panel shown when
// a background session finishes (ui/chat/js/session-notification.js), which reads
// the flag from the public /api/v1/auth/ui-config at boot. Flipping it autosaves
// via the merge-friendly POST /admin/settings/app (server keeps every other key).
// The checkbox state is loaded in _loadAppSettings. Defaults ON (the panel has
// always been available).
function _initSessionNotifications() {
  const chk = _qs('ac-session-notifications');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ session_completion_notifications: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save session_completion_notifications failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// Always-on display master switch — an app-WIDE setting saved server-side
// (always_on_display in app-settings.json), sitting in the App Functions
// group. When ON, the device screen stays awake while the app tab is visible,
// using the Screen Wake Lock API (ui/shared/js/screen-wake.js).
// Flipping it autosaves via the merge-friendly POST /admin/settings/app and
// activates the wake lock immediately in the admin's own browser; every other
// visitor's browser applies it at boot from the public /api/v1/auth/ui-config
// (ui/shared/js/main.js). The checkbox state is loaded in _loadAppSettings.
// Defaults OFF (screen sleeps normally).
function _initAlwaysOnDisplay() {
  const chk = _qs('ac-always-on-display');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ always_on_display: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      // Activate the wake lock right away — reverts with the checkbox if
      // the save failed.
      if (chk.checked) {
        await enableWakeLock('app-setting');
      } else {
        await disableWakeLock('app-setting');
      }
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save always_on_display failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _flashSaveCheck(wrap, false, e.message);
    }
  });
}

// App-wide site name — saved in app-settings.json and reflected in the
// document title / public UI config on the next load.
function _initSiteName() {
  const input = _qs('ac-site-name');
  if (!input) return;
  const control = input.closest('.ac-config-control') || input;
  const save = async () => {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const value = input.value.trim().slice(0, 120);
    input.value = value;
    _markSaving(control);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ site_name: value || 'WebAgent' }),
      });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      document.title = value || 'WebAgent';
      _flashSaveCheck(control, true);
    } catch (e) {
      _flashSaveCheck(control, false, e.message);
    }
  };
  input.addEventListener('change', save);
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Enter') { event.preventDefault(); save(); }
  });
}

// Voice dictation policy — OFF means browser-native only. When ON, the
// expandable selector chooses browser-first fallback or LLM transcription for
// every browser. The server independently enforces the LLM permission.
function _initVoiceDictationPolicy() {
  const chk = _qs('ac-voice-dictation-llm');
  const mode = _qs('ac-voice-dictation-mode');
  if (!chk || !mode) return;
  const toggleWrap = chk.closest('.ac-ability-toggle-wrap');
  const modeCtrl = mode.closest('.ac-config-control');
  let savedEnabled = chk.checked;
  let savedMode = mode.value;

  const reflect = () => {
    mode.disabled = !chk.checked;
    window.__waVoiceDictationPolicy = {
      llm_enabled: chk.checked,
      mode: mode.value === 'llm_only' ? 'llm_only' : 'browser_then_llm',
    };
  };

  const save = async (sourceCtrl) => {
    if (!isAdmin()) {
      showRestrictedModal();
      chk.checked = savedEnabled;
      mode.value = savedMode;
      reflect();
      return;
    }
    const nextMode = mode.value === 'llm_only' ? 'llm_only' : 'browser_then_llm';
    chk.disabled = true;
    mode.disabled = true;
    _markSaving(sourceCtrl);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          voice_dictation_llm_enabled: chk.checked,
          voice_dictation_mode: nextMode,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      savedEnabled = chk.checked;
      savedMode = nextMode;
      reflect();
      _flashSaveCheck(sourceCtrl, true);
    } catch (e) {
      chk.checked = savedEnabled;
      mode.value = savedMode;
      reflect();
      _flashSaveCheck(sourceCtrl, false, e.message);
    } finally {
      chk.disabled = false;
      mode.disabled = !chk.checked;
    }
  };

  chk.addEventListener('change', () => {
    reflect();
    save(toggleWrap);
  });
  mode.addEventListener('change', () => save(modeCtrl));
  chk.addEventListener('voice-policy-loaded', () => {
    savedEnabled = chk.checked;
    savedMode = mode.value;
    reflect();
  });
  reflect();
}

// Widget launcher button — master on/off + expandable config (corners, agent,
// prompt, detection). The master toggle is the app-WIDE show/hide gate saved
// server-side (widget_launcher_visible in app-settings.json). The expanded body
// fields are saved as a `chat_widget` dict alongside it. The init code in
// webagent-launcher.js reads both flags from ui-config.
function _initWidgetLauncher() {
  const chk = _qs('ac-widget-launcher');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');

  // ── Body field refs ──
  const fields = {
    cornersEnabled: _qs('ac-chatwidget-corners-enabled'),
    cornersDelay: _qs('ac-chatwidget-corners-delay'),
    corner_tl: _qs('ac-chatwidget-corner-tl'),
    corner_tl_type: _qs('ac-chatwidget-corner-tl-type'),
    corner_tl_icon: _qs('ac-chatwidget-corner-tl-icon'),
    corner_tl_tooltip: _qs('ac-chatwidget-corner-tl-tooltip'),
    corner_tr: _qs('ac-chatwidget-corner-tr'),
    corner_tr_type: _qs('ac-chatwidget-corner-tr-type'),
    corner_tr_icon: _qs('ac-chatwidget-corner-tr-icon'),
    corner_tr_tooltip: _qs('ac-chatwidget-corner-tr-tooltip'),
    corner_bl: _qs('ac-chatwidget-corner-bl'),
    corner_bl_type: _qs('ac-chatwidget-corner-bl-type'),
    corner_bl_icon: _qs('ac-chatwidget-corner-bl-icon'),
    corner_bl_tooltip: _qs('ac-chatwidget-corner-bl-tooltip'),
    corner_br: _qs('ac-chatwidget-corner-br'),
    corner_br_type: _qs('ac-chatwidget-corner-br-type'),
    corner_br_icon: _qs('ac-chatwidget-corner-br-icon'),
    corner_br_tooltip: _qs('ac-chatwidget-corner-br-tooltip'),
    agentId: _qs('ac-chatwidget-agent-id'),
    prompt: _qs('ac-chatwidget-prompt'),
    pickup12: _qs('ac-chatwidget-pickup-12'),
    pickupSurroundings: _qs('ac-chatwidget-pickup-surroundings'),
  };

  function _cornerObj(enabledEl, typeEl, iconEl, tooltipEl) {
    return {
      enabled: enabledEl ? enabledEl.checked : true,
      type: typeEl ? typeEl.value : 'sessions',
      icon: iconEl ? iconEl.value.trim() : 'list',
      tooltip: tooltipEl ? tooltipEl.value.trim() : '',
    };
  }

  function _collect() {
    return {
      chat_widget: {
        corner_buttons: {
          enabled: fields.cornersEnabled ? fields.cornersEnabled.checked : true,
          hover_delay_ms: fields.cornersDelay ? parseInt(fields.cornersDelay.value, 10) || 200 : 200,
          top_left: _cornerObj(fields.corner_tl, fields.corner_tl_type, fields.corner_tl_icon, fields.corner_tl_tooltip),
          top_right: _cornerObj(fields.corner_tr, fields.corner_tr_type, fields.corner_tr_icon, fields.corner_tr_tooltip),
          bottom_left: _cornerObj(fields.corner_bl, fields.corner_bl_type, fields.corner_bl_icon, fields.corner_bl_tooltip),
          bottom_right: _cornerObj(fields.corner_br, fields.corner_br_type, fields.corner_br_icon, fields.corner_br_tooltip),
        },
        agent_id: fields.agentId ? fields.agentId.value.trim() : '',
        prompt: fields.prompt ? fields.prompt.value : '',
        pickup_12oclock: fields.pickup12 ? fields.pickup12.checked : true,
        pickup_surroundings: fields.pickupSurroundings ? fields.pickupSurroundings.checked : false,
      },
    };
  }

  let _saving = false;
  async function _save() {
    if (_saving) return;
    if (!isAdmin()) { showRestrictedModal(); return; }
    _saving = true;
    _markSaving(wrap);
    try {
      const payload = { widget_launcher_visible: chk.checked, ..._collect() };
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save widget launcher config failed', e);
      chk.checked = !chk.checked;
      _flashSaveCheck(wrap, false, e.message);
    } finally {
      _saving = false;
    }
  }

  chk.addEventListener('change', _save);

  // Auto-save on any body-field change (debounced: 600ms quiet window)
  let _bodyTimer = null;
  function _bodyChanged() {
    if (_bodyTimer) clearTimeout(_bodyTimer);
    _bodyTimer = setTimeout(_save, 600);
  }
  const allBodyEls = [
    fields.cornersEnabled, fields.cornersDelay,
    fields.corner_tl, fields.corner_tl_type, fields.corner_tl_icon, fields.corner_tl_tooltip,
    fields.corner_tr, fields.corner_tr_type, fields.corner_tr_icon, fields.corner_tr_tooltip,
    fields.corner_bl, fields.corner_bl_type, fields.corner_bl_icon, fields.corner_bl_tooltip,
    fields.corner_br, fields.corner_br_type, fields.corner_br_icon, fields.corner_br_tooltip,
    fields.agentId, fields.prompt,
    fields.pickup12, fields.pickupSurroundings,
  ];
  allBodyEls.forEach((el) => {
    if (!el) return;
    el.addEventListener('change', _bodyChanged);
    if (el.tagName === 'INPUT' && (el.type === 'text' || el.type === 'number')) {
      el.addEventListener('input', _bodyChanged);
    }
    if (el.tagName === 'TEXTAREA') {
      el.addEventListener('input', _bodyChanged);
    }
  });
}

// Hide header during keyboard master switch — an app-WIDE setting saved
// server-side (hide_header_on_keyboard in app-settings.json), sitting in the
// App Functions group. When ON and the chat input is focused on mobile
// (≤800px), the main app header and chat panel header collapse so the
// keyboard doesn't crowd the input. Default OFF. The boot code in main.js
// reads this flag from ui-config and sets body.hide-header-kb accordingly.
function _initHideHeaderKeyboard() {
  const chk = _qs('ac-hide-header-kb');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');
  // Threshold input lives in the expandable body
  const threshInput = _qs('ac-hide-header-kb-threshold');

  function _saveHideHeaderKb() {
    const payload = { hide_header_on_keyboard: chk.checked };
    if (threshInput) {
      const v = parseInt(threshInput.value, 10);
      payload.hide_header_kb_threshold = (!isNaN(v) && v >= 0) ? v : 200;
    }
    return payload;
  }

  function _applyLive() {
    document.body.classList.toggle('hide-header-kb', chk.checked);
    // Update threshold as a data attribute so the viewport watcher can read it
    if (threshInput) {
      const v = parseInt(threshInput.value, 10);
      const t = (!isNaN(v) && v >= 0) ? v : 200;
      document.body.dataset.hhkThreshold = String(t);
    }
    // Immediately check viewport height so admin sees the effect live
    var vv = window.visualViewport;
    var vvh = vv ? vv.height : window.innerHeight;
    var raw = document.body.dataset.hhkThreshold;
    var th = parseInt(raw, 10);
    if (isNaN(th) || th < 0) th = 200;
    var isMobile = window.innerWidth <= 800;
    var ae = document.activeElement;
    var editable = !!ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA' || ae.isContentEditable);
    var open = isMobile && editable && (window.innerHeight - vvh) > 120;
    var below = th === 0 ? open : (open && vvh < th);
    document.body.classList.toggle('hhk-active', chk.checked && below);
  }

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_saveHideHeaderKb()),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _applyLive();
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('app-settings: save hide_header_on_keyboard failed', e);
      chk.checked = !chk.checked;
      _flashSaveCheck(wrap, false, e.message);
    }
  }

  chk.addEventListener('change', _save);
  if (threshInput) {
    threshInput.addEventListener('change', _save);
  }
}

// Max active sessions — app-WIDE cap on how many sessions may be actively
// running a turn at the same time (max_active_sessions in app-settings.json,
// enforced by app/agent/session_gate.py). 0 = unlimited (default). When the cap
// is reached, new sessions wait in a FIFO queue and start only after an active
// session completes. The value is loaded in _loadAppSettings; Save (or Enter in
// the input) posts the merge-friendly /admin/settings/app. The row's expand/
// collapse head-click is wired via _wireBootRow in _initAppSettings.
function _initMaxActiveSessions() {
  const input = _qs('ac-max-active-sessions');
  const saveBtn = _qs('ac-max-active-sessions-save');
  if (!input || !saveBtn) return;

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const v = parseInt(input.value, 10);
    const value = (!isNaN(v) && v >= 0) ? v : 0;
    input.value = String(value);
    _markSaving(saveBtn);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ max_active_sessions: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(saveBtn, true);
    } catch (e) {
      console.warn('app-settings: save max_active_sessions failed', e);
      _flashSaveCheck(saveBtn, false, e.message);
    }
  }

  saveBtn.addEventListener('click', _save);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _save(); }
  });
}

// Default Max Output Tokens — the output-token ceiling used ONLY for an agent
// that has never set its own Max Tokens (agent Runtime → Limits). An agent's own
// value is authoritative and is sent to the provider unchanged, with no clamp
// against the context window; this app-wide default also seeds a newly created
// agent. Saved as default_max_tokens in app-settings.json (read by
// app/agent/loop.py via app/admin/settings.py get_default_max_tokens). The row's
// expand/collapse head-click is wired via _wireBootRow in _initAppSettings.
function _initDefaultMaxTokens() {
  const input = _qs('ac-default-max-tokens');
  const saveBtn = _qs('ac-default-max-tokens-save');
  if (!input || !saveBtn) return;

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const v = parseInt(input.value, 10);
    const value = (!isNaN(v) && v > 0) ? v : 8096;
    input.value = String(value);
    _markSaving(saveBtn);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ default_max_tokens: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(saveBtn, true);
    } catch (e) {
      console.warn('app-settings: save default_max_tokens failed', e);
      _flashSaveCheck(saveBtn, false, e.message);
    }
  }

  saveBtn.addEventListener('click', _save);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _save(); }
  });
}

// Auto-resume recovery — the app-wide resume attempt budget and the exponential
// backoff base (run_max_resume_attempts / run_resume_backoff_seconds in
// app-settings.json; read by app/agent/runner.py via get_self_heal_config). The
// attempt limit is BOTH the default for an agent with no override AND the
// ceiling the per-agent value is clamped to. Save posts the merge-friendly
// /admin/settings/app; values are loaded in _loadAppSettings.
function _initResumeRecovery() {
  const attemptsEl = _qs('ac-resume-max-attempts');
  const perCauseEl = _qs('ac-resume-per-cause-attempts');
  const backoffEl = _qs('ac-resume-backoff-seconds');
  const saveBtn = _qs('ac-resume-recovery-save');
  if (!attemptsEl || !perCauseEl || !backoffEl || !saveBtn) return;

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const a = parseInt(attemptsEl.value, 10);
    const attempts = (!isNaN(a) && a >= 0) ? a : 6;
    const p = parseInt(perCauseEl.value, 10);
    const perCause = (!isNaN(p) && p >= 0) ? p : 3;
    const b = parseInt(backoffEl.value, 10);
    const backoff = (!isNaN(b) && b >= 1) ? b : 30;
    attemptsEl.value = String(attempts);
    perCauseEl.value = String(perCause);
    backoffEl.value = String(backoff);
    _markSaving(saveBtn);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          run_max_resume_attempts: attempts,
          run_max_resume_attempts_per_cause: perCause,
          run_resume_backoff_seconds: backoff,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(saveBtn, true);
    } catch (e) {
      console.warn('app-settings: save resume recovery failed', e);
      _flashSaveCheck(saveBtn, false, e.message);
    }
  }

  saveBtn.addEventListener('click', _save);
  [attemptsEl, perCauseEl, backoffEl].forEach((el) => el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _save(); }
  }));
}

// Closing-message gate — the app-wide corrective-turn budget for the end-of-run
// closing contract (run_max_closing_attempts /
// run_max_closing_attempts_per_requirement in app-settings.json; read by
// app/agent/closing_message.py via effective_max_attempts / attempts_exhausted).
// These are REQUIREMENT-COMPLIANCE budgets, NOT error recovery: the closing gate
// re-injects its instruction while the run's final message does not yet meet the
// contract, and nothing has failed — deliberately separate from the resume/error
// counters in _initResumeRecovery above (different trigger, counter, lifecycle;
// never "per error type"). Both values are BOTH the default for an agent with no
// override AND the ceiling the per-agent value is clamped to. Save posts the
// merge-friendly /admin/settings/app; values are loaded in _loadAppSettings.
function _initClosingMessageGate() {
  const maxEl = _qs('ac-closing-max-attempts');
  const perReqEl = _qs('ac-closing-per-requirement-attempts');
  const saveBtn = _qs('ac-closing-gate-save');
  if (!maxEl || !perReqEl || !saveBtn) return;

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const a = parseInt(maxEl.value, 10);
    const attempts = (!isNaN(a) && a >= 0) ? a : 8;
    const p = parseInt(perReqEl.value, 10);
    const perReq = (!isNaN(p) && p >= 0) ? p : 3;
    maxEl.value = String(attempts);
    perReqEl.value = String(perReq);
    _markSaving(saveBtn);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          run_max_closing_attempts: attempts,
          run_max_closing_attempts_per_requirement: perReq,
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(saveBtn, true);
    } catch (e) {
      console.warn('app-settings: save closing-message gate failed', e);
      _flashSaveCheck(saveBtn, false, e.message);
    }
  }

  saveBtn.addEventListener('click', _save);
  [maxEl, perReqEl].forEach((el) => el.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _save(); }
  }));
}

// Assertions gate — the app-wide corrective-turn budget for the run's DECLARED
// assertions (run_max_assertions_attempts in app-settings.json; read by
// app/agent/assertions.py via effective_max_attempts). The gate opens every run
// by declaring the user's requirements as a checklist and holds the run open
// until each one is answered for; while one is unanswered nothing has failed, so
// this is REQUIREMENT COMPLIANCE, not error recovery — separate from the
// closing-message budget above (different requirement, counter and lifecycle)
// and from the resume/error counters. The value is BOTH the default for an agent
// with no override AND the ceiling the per-agent value is clamped to
// (metadata['assertions_max_attempts'], Agents → Runtime → Limits). Save posts
// the merge-friendly /admin/settings/app; loaded in _loadAppSettings.
function _initAssertionsGate() {
  const maxEl = _qs('ac-assertions-max-attempts');
  const saveBtn = _qs('ac-assertions-gate-save');
  if (!maxEl || !saveBtn) return;

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const a = parseInt(maxEl.value, 10);
    const attempts = (!isNaN(a) && a >= 0) ? a : 4;
    maxEl.value = String(attempts);
    _markSaving(saveBtn);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ run_max_assertions_attempts: attempts }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(saveBtn, true);
    } catch (e) {
      console.warn('app-settings: save assertions gate failed', e);
      _flashSaveCheck(saveBtn, false, e.message);
    }
  }

  saveBtn.addEventListener('click', _save);
  maxEl.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); _save(); }
  });
}

// Pre-tool progress-message gate — the APP-WIDE style for the progress update the gate asks
// for before an Auto-mode run's first edit. The gate INFORMS rather than gates:
// the progress update is published exactly as written and never validated (see
// app/agent/pre_tool_progress.py). This row IS that app-wide guidance — written to
// data/config/app-settings.json → gate_settings['pre_tool_progress_message'] via
// PUT /admin/settings/gates/{id} (the same app-level surface the Gates table on
// Agent tools & abilities writes), which the gate's resolver stacks on top of its
// own wording and BELOW any per-agent value. Unlike the closing-message budgets
// above these are guidance, not limits: there is nothing to clamp and a blank
// value genuinely unsets the layer.
function _initPreToolProgressMessageGate() {
  const formatEl = _qs('ac-pre-tool-progress-message-gate-format');
  const saveBtn = _qs('ac-pre-tool-progress-message-gate-save');
  if (!formatEl || !saveBtn) return;
  const GATE_ID = 'pre_tool_progress_message';

  // Load the saved app-wide value so a refresh shows what is actually in force.
  // A failed load leaves the field blank, which reads as "unset" — the default
  // and the same state the resolver sees.
  (async () => {
    try {
      const res = await _fetch(apiPath('/admin/settings/gates'));
      if (!res.ok) return;
      const data = await res.json().catch(() => null);
      const gate = (data && Array.isArray(data.gates) ? data.gates : [])
        .find((row) => row && row.id === GATE_ID);
      const saved = (gate && gate.values) || {};
      formatEl.value = saved.format || '';
    } catch (e) {
      console.warn('app-settings: load pre-tool progress-message gate failed', e);
    }
  })();

  async function _save() {
    if (!isAdmin()) { showRestrictedModal(); return; }
    const value = formatEl.value.trim();
    formatEl.value = value;
    _markSaving(saveBtn);
    try {
      const res = await _fetch(
        apiPath('/admin/settings/gates/' + encodeURIComponent(GATE_ID)), {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ params: { format: value } }),
        });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _flashSaveCheck(saveBtn, true);
    } catch (e) {
      console.warn('app-settings: save pre-tool progress-message gate failed', e);
      _flashSaveCheck(saveBtn, false, e.message);
    }
  }

  saveBtn.addEventListener('click', _save);
}

// Allowed values for the boot dropdowns. Declared BEFORE the init functions
// that reference them so the symbols are defined regardless of load/eval order
// (strict-mode declaration-before-use discipline — see CLAUDE.md / Codebase
// Admin skill). Declaring these after _initBootAnimation previously caused a
// temporal-dead-zone ReferenceError when this code was concatenated into a
// single bundle that ran initAppConfig before reaching the line.
const _BOOT_ANIM_ALLOWED = ['chat-slide-right', 'chat-slide-left', 'no-animation'];

function _initBootAnimation() {
  const sel = _qs('ac-boot-animation');
  if (!sel) return;
  const ctl = sel.closest('.ac-config-control');
  let saved = localStorage.getItem('bootAnimation');
  if (!_BOOT_ANIM_ALLOWED.includes(saved)) saved = 'chat-slide-right';
  sel.value = saved;
  sel.addEventListener('change', () => {
    const val = _BOOT_ANIM_ALLOWED.includes(sel.value) ? sel.value : 'chat-slide-right';
    // Per-browser save (localStorage, synchronous) — confirm with the same
    // overlay so every dropdown reads the same way; the held spinner makes the
    // instant write perceptible before the ✓.
    _markSaving(ctl);
    localStorage.setItem('bootAnimation', val);
    _flashSaveCheck(ctl, true);
  });
}

function _initTranscriptOrderRecovery() {
  const enabled = _qs('ac-transcript-order-recovery-enabled');
  const threshold = _qs('ac-transcript-order-recovery-threshold-seconds');
  if (!enabled || !threshold) return;
  const control = _qs('ac-transcript-order-recovery-row') || enabled.closest('.ac-config-control');
  const save = async () => {
    let seconds = parseInt(threshold.value, 10);
    if (isNaN(seconds) || seconds < 0) seconds = 60;
    threshold.value = String(seconds);
    _markSaving(control);
    try {
      const response = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          transcript_timestamp_order_recovery_enabled: enabled.checked,
          transcript_timestamp_order_recovery_threshold_seconds: seconds,
        }),
      });
      if (!response.ok) throw new Error('HTTP ' + response.status);
      _flashSaveCheck(control, true);
    } catch (error) {
      _flashSaveCheck(control, false, error.message);
    }
  };
  enabled.addEventListener('change', save);
  threshold.addEventListener('change', save);
  threshold.addEventListener('keydown', event => {
    if (event.key === 'Enter') save();
  });
}

async function _loadAppSettings(data) {
  try {
    // Reuse the caller's pre-fetched payload when provided (load() fetches it
    // once for both consumers); only fetch here when called standalone.
    if (!data) {
      const res = await _fetch(apiPath('/admin/settings/app'));
      if (!res.ok) return;
      data = await res.json();
    }
    const cb = _qs('ac-extend-llm-to-agents');
    if (cb) cb.checked = data.extend_llm_to_agents !== false;
    const sp = _qs('ac-boot-splash-enabled');
    if (sp) {
      sp.checked = data.splash_enabled !== false;
      await _checkSplashContent();
      _reflectSplashContent(sp);
    }
    const au = _qs('ac-allow-user-appearance');
    if (au) au.checked = data.allow_user_appearance === true;
    const sn = _qs('ac-session-notifications');
    if (sn) sn.checked = data.session_completion_notifications !== false;  // default ON
    const aod = _qs('ac-always-on-display');
    if (aod) aod.checked = data.always_on_display === true;  // default OFF
    const voiceLlm = _qs('ac-voice-dictation-llm');
    const siteName = _qs('ac-site-name');
    if (siteName) siteName.value = typeof data.site_name === 'string' && data.site_name.trim()
      ? data.site_name.trim()
      : 'WebAgent';
    const voiceMode = _qs('ac-voice-dictation-mode');
    if (voiceLlm) voiceLlm.checked = data.voice_dictation_llm_enabled !== false;
    if (voiceMode) {
      voiceMode.value = data.voice_dictation_mode === 'llm_only'
        ? 'llm_only'
        : 'browser_then_llm';
      voiceMode.disabled = voiceLlm ? !voiceLlm.checked : false;
    }
    window.__waVoiceDictationPolicy = {
      llm_enabled: voiceLlm ? voiceLlm.checked : data.voice_dictation_llm_enabled !== false,
      mode: voiceMode ? voiceMode.value : (
        data.voice_dictation_mode === 'llm_only' ? 'llm_only' : 'browser_then_llm'
      ),
    };
    voiceLlm?.dispatchEvent(new Event('voice-policy-loaded'));
    const wl = _qs('ac-widget-launcher');
    if (wl) wl.checked = data.widget_launcher_visible === true;  // default OFF (hidden)
    // Populate the expandable chat-widget config body fields from the stored
    // chat_widget dict (saved alongside widget_launcher_visible).
    const cw = data.chat_widget;
    if (cw && typeof cw === 'object') {
      const cb = cw.corner_buttons;
      if (cb && typeof cb === 'object') {
        const ce = _qs('ac-chatwidget-corners-enabled');
        if (ce) ce.checked = cb.enabled !== false;
        const cd = _qs('ac-chatwidget-corners-delay');
        if (cd) cd.value = String(cb.hover_delay_ms != null ? cb.hover_delay_ms : 200);
        _loadCorner('tl', cb.top_left);
        _loadCorner('tr', cb.top_right);
        _loadCorner('bl', cb.bottom_left);
        _loadCorner('br', cb.bottom_right);
      }
      const aid = _qs('ac-chatwidget-agent-id');
      if (aid) aid.value = cw.agent_id || '';
      const pr = _qs('ac-chatwidget-prompt');
      if (pr) pr.value = cw.prompt || '';
      const pk12 = _qs('ac-chatwidget-pickup-12');
      if (pk12) pk12.checked = cw.pickup_12oclock !== false;
      const pks = _qs('ac-chatwidget-pickup-surroundings');
      if (pks) pks.checked = cw.pickup_surroundings === true;
    }
    function _loadCorner(pos, obj) {
      if (!obj || typeof obj !== 'object') return;
      const en = _qs('ac-chatwidget-corner-' + pos);
      if (en) en.checked = obj.enabled !== false;
      const ty = _qs('ac-chatwidget-corner-' + pos + '-type');
      if (ty) ty.value = obj.type || 'sessions';
      const ic = _qs('ac-chatwidget-corner-' + pos + '-icon');
      if (ic) ic.value = obj.icon || '';
      const tt = _qs('ac-chatwidget-corner-' + pos + '-tooltip');
      if (tt) tt.value = obj.tooltip || '';
    }
    const ssw = _qs('ac-safety-switch');
    if (ssw) ssw.checked = data.safety_lock_enabled === true;  // default OFF
    const hhk = _qs('ac-hide-header-kb');
    if (hhk) hhk.checked = data.hide_header_on_keyboard === true;  // default OFF
    const hhkThresh = _qs('ac-hide-header-kb-threshold');
    if (hhkThresh) {
      const v = parseInt(data.hide_header_kb_threshold, 10);
      hhkThresh.value = (!isNaN(v) && v >= 0) ? v : 200;
    }
    const mas = _qs('ac-max-active-sessions');
    if (mas) {
      const v = parseInt(data.max_active_sessions, 10);
      mas.value = (!isNaN(v) && v >= 0) ? v : 0;
    }
    const dmt = _qs('ac-default-max-tokens');
    if (dmt) {
      const v = parseInt(data.default_max_tokens, 10);
      dmt.value = (!isNaN(v) && v > 0) ? v : 8096;
    }
    const rma = _qs('ac-resume-max-attempts');
    if (rma) {
      const v = parseInt(data.run_max_resume_attempts, 10);
      rma.value = (!isNaN(v) && v >= 0) ? v : 6;
    }
    const rpc = _qs('ac-resume-per-cause-attempts');
    if (rpc) {
      const v = parseInt(data.run_max_resume_attempts_per_cause, 10);
      rpc.value = (!isNaN(v) && v >= 0) ? v : 3;
    }
    const rbs = _qs('ac-resume-backoff-seconds');
    if (rbs) {
      const v = parseInt(data.run_resume_backoff_seconds, 10);
      rbs.value = (!isNaN(v) && v >= 1) ? v : 30;
    }
    const cma = _qs('ac-closing-max-attempts');
    if (cma) {
      const v = parseInt(data.run_max_closing_attempts, 10);
      cma.value = (!isNaN(v) && v >= 0) ? v : 8;
    }
    const cpr = _qs('ac-closing-per-requirement-attempts');
    if (cpr) {
      const v = parseInt(data.run_max_closing_attempts_per_requirement, 10);
      cpr.value = (!isNaN(v) && v >= 0) ? v : 3;
    }
    const ama = _qs('ac-assertions-max-attempts');
    if (ama) {
      const v = parseInt(data.run_max_assertions_attempts, 10);
      ama.value = (!isNaN(v) && v >= 0) ? v : 4;
    }
    const recoveryEnabled = _qs('ac-transcript-order-recovery-enabled');
    if (recoveryEnabled) recoveryEnabled.checked = data.transcript_timestamp_order_recovery_enabled !== false;
    const recoveryThreshold = _qs('ac-transcript-order-recovery-threshold-seconds');
    if (recoveryThreshold) {
      const v = parseInt(data.transcript_timestamp_order_recovery_threshold_seconds, 10);
      recoveryThreshold.value = (!isNaN(v) && v >= 0) ? v : 60;
    }
    const gp = _qs('ac-global-system-prompt');
    if (gp) gp.value = data.global_system_prompt || '';
  } catch (e) {
    console.warn('settings: could not load app settings', e);
  }
}

// ── Appearance: combined theme palette · background · fonts ────────────────
// One panel, two mirrored theme cards (Light + Dark). Each card holds: preset
// chips (one-click full palettes), the animated-background select, and the full
// palette of colour swatches. A shared card holds the theme-independent fonts +
// border thickness. Every control auto-saves per theme to /admin/settings/app
// (merged), previews live in the admin's own window, and applies app-wide for
// every visitor via the public /api/v1/auth/ui-config:
//   • palette + border + fonts → ui/shared/js/appearance.js (WA_APPEARANCE)
//   • background                → ui/background/_engine/manager.js (WA_BG)
// All keys can also be hand-edited in data/config/app-settings.json.
//
// The swatch order + which token each maps to is the SINGLE SOURCE OF TRUTH in
// appearance.js (window.WA_APPEARANCE.tokens); here we only attach a friendly
// label + icon. Saved-value key for a token+theme is `<base>_<theme>` (border's
// is the historical `border_color_<theme>` — same shape since base = border_color).

// _TOKEN_META (label + icon per token), _APP_DEFAULTS (per-theme palette
// defaults) and _FOLLOW_ALPHA (the blank-bubble panel tint) now live in the
// SHARED ui/shared/js/appearance-editor.js and are imported at the top of this
// file (aliased to these names). Edit them there so the admin + per-user editors
// stay identical.

// The colour a `follow` token (user/agent bubble, chat pill) actually shows when
// left blank: the LIVE Panel swatch at the theme's bubble alpha. Reads the built
// Panel swatch input when present (so it tracks a drag in progress), else the
// saved value, else the design default — mirroring the index.css color-mix on
// --bg-elev so the table never shows a frozen stock colour.
function _followDefault(theme) {
  const sw = _qs(`ac-sw-${theme}-surface_panel`);
  const panelHex = sw
    ? sw.value
    : _asHex(_appValues[_keyFor('surface_panel', theme)], _APP_DEFAULTS[theme].surface_panel);
  return _combineColor(panelHex, _FOLLOW_ALPHA[theme]);
}

// The effective default for any token under a theme — what the swatch shows when
// the saved value is blank. Follow tokens derive from the live Panel colour;
// every other token uses its design-system default.
function _effectiveDefault(base, theme) {
  const meta = _TOKEN_META[base];
  if (meta && meta.follow === 'panel') return _followDefault(theme);
  if (meta && meta.follow === 'border') return _APP_DEFAULTS[theme].border_color;
  return _APP_DEFAULTS[theme][base] || '#000000';
}

// Keep the blank (theme-following) bubble/pill swatches in sync with the Panel
// swatch — called when Panel changes so the table stays truthful. A pinned
// bubble (non-blank saved value) is left untouched.
function _syncFollowSwatches(theme) {
  _tokenOrder().forEach((base) => {
    const meta = _TOKEN_META[base];
    if (!meta || !meta.follow) return;
    if (_appValues[_keyFor(base, theme)]) return;   // pinned → leave alone
    const split = _splitColor(_effectiveDefault(base, theme), '#000000');
    const sw = _qs(`ac-sw-${theme}-${base}`);
    if (sw) sw.value = split.hex;
    const al = _qs(`ac-al-${theme}-${base}`);
    if (al) al.value = split.alpha;
  });
}

// _PRESETS (the one-click preset themes) now lives in the SHARED
// ui/shared/js/appearance-editor.js and is imported at the top of this file. Add
// or edit a preset there so the admin + per-user editors offer the same chips.
// (The admin can still snapshot a custom theme via "Add theme" — see
// _addCustomTheme below; custom themes are admin-only and stored in app settings,
// NOT in the shared preset list.)

// Module state: the live appearance values (loaded from the server), the
// background catalog, and the per-theme background choice.
let _appValues = {};                          // every appearance key → value
let _bgCatalog = [];
let _bgChoice = { dark: 'stargaze', light: 'bullet-grid' };
let _customThemes = [];                        // user-saved theme chips (both modes)

// Saved-value key for a token base under a theme.
function _keyFor(base, theme) { return `${base}_${theme}`; }

// The colour helpers _asHex / _splitColor / _combineColor now live in the SHARED
// ui/shared/js/appearance-editor.js and are imported at the top of this file
// (aliased to these names), so the per-user editor uses the identical maths.

// Live-preview a patch in the admin's own window (no save).
function _previewAppearance(patch) {
  try {
    if (window.WA_APPEARANCE && typeof window.WA_APPEARANCE.setOverrides === 'function') {
      window.WA_APPEARANCE.setOverrides(patch);
    }
  } catch (_) { /* preview is best-effort */ }
}

// Persist a patch (merged server-side) and confirm the save with the unified
// on-top overlay over `ctrl` (the control's container — a swatch row's
// `.ac-theme-row-ctl`, a select's `.ac-config-control`, or the presets host).
// Crucially, state and the green ✓ change ONLY after parsing the server's
// post-write response; callers that need rollback can await the result.
async function _saveAppearance(patch, ctrl) {
  if (!isAdmin()) {
    showRestrictedModal();
    return { ok: false, error: 'Admin access required' };
  }
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!res.ok) {
      let detail = '';
      try {
        const payload = await res.json();
        detail = typeof payload?.detail === 'string' ? payload.detail : '';
      } catch (_) { /* retain the HTTP fallback if the error response is not JSON */ }
      throw new Error(detail || `HTTP ${res.status}`);
    }
    const settings = await res.json();
    if (!settings || typeof settings !== 'object') throw new Error('Invalid save response');
    Object.assign(_appValues, settings);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return { ok: true, settings };
  } catch (e) {
    const error = e && e.message ? e.message : 'Save failed';
    if (ctrl) _flashSaveCheck(ctrl, false, error);
    return { ok: false, error };
  }
}

// Build (or rebuild) one theme row's expanded body: preset chips + background
// row + a colour swatch per palette token, all inside its .ac-ability-body
// (the shared ability-table expandable region).
function _buildThemeCard(theme) {
  const body = _qs('ac-theme-body-' + theme);
  if (!body) return;
  body.innerHTML = '';

  // ── Preset chips (top of the expanded body) ──
  const presetsHost = document.createElement('div');
  presetsHost.className = 'ac-theme-presets';
  presetsHost.id = 'ac-presets-' + theme;
  body.appendChild(presetsHost);
  _PRESETS.concat(_customThemes).forEach((p) => {
    const isCustom = typeof p.id === 'string' && p.id.indexOf('custom-') === 0;
    const merged = { ..._APP_DEFAULTS[theme], ...(p[theme] || {}) };
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = 'ac-preset-chip' + (isCustom ? ' ac-preset-custom' : '');
    chip.dataset.preset = p.id;
    chip.innerHTML =
      `<span class="ac-preset-dot"></span>${_esc(p.name)}` +
      (isCustom ? '<span class="ac-preset-del" title="Delete theme" aria-label="Delete theme">&times;</span>' : '');
    const dot = chip.querySelector('.ac-preset-dot');
    if (dot) dot.style.setProperty('--preset-bg', _asHex(merged.accent, '#888'));
    chip.addEventListener('click', (e) => {
      if (e.target && e.target.classList.contains('ac-preset-del')) {
        e.stopPropagation();
        _deleteCustomTheme(p.id);
        return;
      }
      _applyPreset(theme, p);
    });
    presetsHost.appendChild(chip);
  });
  // "Add theme" — snapshot the current colours (both modes) as a new chip.
  const addBtn = document.createElement('button');
  addBtn.type = 'button';
  addBtn.className = 'ac-preset-add';
  addBtn.title = 'Save the current colours as a new theme';
  addBtn.innerHTML = '<i data-lucide="plus" class="lucide-icon dash-ico-13"></i>Add theme';
  addBtn.addEventListener('click', () => _addCustomTheme());
  presetsHost.appendChild(addBtn);
  // Preset / custom-theme saves confirm with the unified overlay over the whole
  // presets strip (`ac-presets-<theme>`) — no separate tick span.

  // ── Background select row + a swatch row per palette token ──

  // Background select row.
  const bgRow = document.createElement('div');
  bgRow.className = 'ac-theme-row';
  bgRow.innerHTML =
    '<span class="ac-theme-row-label"><i data-lucide="image" class="lucide-icon" class="ac-ico-15 ac-fg3"></i>Background</span>' +
    '<span class="ac-theme-row-ctl">' +
      `<select id="ac-bg-${theme}" class="ac-input"></select>` +
    '</span>';
  body.appendChild(bgRow);

  // Colour swatch rows — order from WA_APPEARANCE.tokens (single source of truth).
  const order = (window.WA_APPEARANCE && Array.isArray(window.WA_APPEARANCE.tokens))
    ? window.WA_APPEARANCE.tokens
    : Object.keys(_TOKEN_META);
  order.forEach((base) => {
    const meta = _TOKEN_META[base];
    if (!meta) return;
    const key = _keyFor(base, theme);
    const fallback = _effectiveDefault(base, theme);
    // A blank saved value seeds the swatch from `fallback` (which carries the
    // follow-token's panel tint + alpha), so bubbles/pill show the live theme.
    const split = _splitColor(_appValues[key] || fallback, _asHex(fallback, '#000000'));
    const row = document.createElement('div');
    row.className = 'ac-theme-row';
    // Alpha-capable tokens (surfaces + bubbles) get an opacity slider after the
    // swatch; the two combine into a #rrggbb[aa] value.
    const alphaCtl = meta.alpha
      ? `<input type="range" class="ac-swatch-alpha" id="ac-al-${theme}-${base}" min="0" max="100" step="1" value="${split.alpha}" title="Opacity" aria-label="${_esc(meta.label)} opacity">`
      : '';
    row.innerHTML =
      `<span class="ac-theme-row-label"><i data-lucide="${_esc(meta.icon)}" class="lucide-icon" class="ac-ico-15 ac-fg3"></i>${_esc(meta.label)}</span>` +
      '<span class="ac-theme-row-ctl">' +
        alphaCtl +
        `<input type="color" class="ac-swatch" id="ac-sw-${theme}-${base}" value="${_esc(split.hex)}">` +
      '</span>';
    body.appendChild(row);

    const input = row.querySelector('input[type="color"]');
    const alpha = row.querySelector('input[type="range"]');
    // The unified save overlay sits ON TOP of the swatch+slider cluster.
    const ctl = row.querySelector('.ac-theme-row-ctl');
    // Current stored value = swatch colour combined with the opacity slider
    // (no slider → plain hex).
    const curVal = () => (alpha ? _combineColor(input.value, alpha.value) : input.value);
    // Drag = live preview only; release/commit = save + green tick.
    const preview = () => {
      _previewAppearance({ [key]: curVal() });
      if (base === 'accent') _refreshPresetActive(theme, input.value);
      if (base === 'surface_panel' || base === 'border_color') _syncFollowSwatches(theme);
    };
    const commit = () => {
      const v = curVal();
      _previewAppearance({ [key]: v });
      _saveAppearance({ [key]: v }, ctl);
      if (base === 'accent') _refreshPresetActive(theme, input.value);
      if (base === 'surface_panel') _syncFollowSwatches(theme);
    };
    input.addEventListener('input', preview);
    input.addEventListener('change', commit);
    if (alpha) {
      alpha.addEventListener('input', preview);
      alpha.addEventListener('change', commit);
    }
  });

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

// The palette token order (single source of truth = appearance.js).
function _tokenOrder() {
  return (window.WA_APPEARANCE && Array.isArray(window.WA_APPEARANCE.tokens))
    ? window.WA_APPEARANCE.tokens
    : Object.keys(_TOKEN_META);
}

// Apply a theme (built-in preset OR custom) to one mode — a COMPLETE RESET of
// every palette token for that mode, wiping any per-swatch / opacity / bubble
// customisation. For each token: use the theme's explicit value if it has one;
// otherwise a `follow` token (bubbles) resets to BLANK (= track the theme) and
// every other token resets to its design-system default. Swatch + opacity
// slider are synced to match.
function _applyPreset(theme, preset) {
  const pv = (preset && preset[theme]) || {};
  const patch = {};
  _tokenOrder().forEach((base) => {
    const meta = _TOKEN_META[base];
    if (!meta) return;
    const key = _keyFor(base, theme);
    let v;
    if (Object.prototype.hasOwnProperty.call(pv, base)) v = pv[base];
    else if (meta.follow) v = '';
    else v = _APP_DEFAULTS[theme][base] || '';
    patch[key] = v;
    const disp = v || _effectiveDefault(base, theme);
    const split = _splitColor(disp, _asHex(disp, '#000000'));
    const sw = _qs(`ac-sw-${theme}-${base}`);
    if (sw) sw.value = split.hex;
    const al = _qs(`ac-al-${theme}-${base}`);
    if (al) al.value = split.alpha;
  });
  _previewAppearance(patch);
  _saveAppearance(patch, _qs('ac-presets-' + theme));
  _refreshPresetActive(theme, pv.accent || _APP_DEFAULTS[theme].accent);
}

// ── Custom themes (the "Add theme" button) ────────────────────────────────
// Snapshot the CURRENT palette of one mode into a { <token>: value } object —
// exactly what's applied now, including opacity (#rrggbbaa) and any bubble
// override (or '' when a bubble is following the theme).
function _snapshotTheme(theme) {
  const obj = {};
  _tokenOrder().forEach((base) => {
    if (!_TOKEN_META[base]) return;
    const cur = _appValues[_keyFor(base, theme)];
    obj[base] = (typeof cur === 'string')
      ? cur
      : (_TOKEN_META[base].follow ? '' : (_APP_DEFAULTS[theme][base] || ''));
  });
  return obj;
}

function _saveCustomThemes() {
  const v = JSON.stringify(_customThemes);
  _appValues.custom_themes = v;
  _saveAppearance({ custom_themes: v }, _qs('ac-presets-dark') || _qs('ac-presets-light'));
}

// Save the current colours (BOTH modes) as a new named theme chip.
function _addCustomTheme() {
  const name = (window.prompt('Name this theme:', 'My theme') || '').trim();
  if (!name) return;
  _customThemes.push({
    id: 'custom-' + Date.now(),
    name: name.slice(0, 40),
    dark: _snapshotTheme('dark'),
    light: _snapshotTheme('light'),
  });
  _saveCustomThemes();
  _buildThemeCard('light');
  _buildThemeCard('dark');
}

function _deleteCustomTheme(id) {
  _customThemes = _customThemes.filter((t) => t.id !== id);
  _saveCustomThemes();
  _buildThemeCard('light');
  _buildThemeCard('dark');
}

// Highlight the preset whose accent matches the current accent for this theme.
function _refreshPresetActive(theme, accentHex) {
  const host = _qs('ac-presets-' + theme);
  if (!host) return;
  const cur = String(accentHex || '').toLowerCase();
  host.querySelectorAll('.ac-preset-chip').forEach((chip) => {
    const p = _PRESETS.concat(_customThemes).find((x) => x.id === chip.dataset.preset);
    if (!p) return;
    const presetAccent = _asHex((p[theme] && p[theme].accent) || _APP_DEFAULTS[theme].accent, '');
    chip.classList.toggle('active', presetAccent.toLowerCase() === cur);
  });
}

// Populate a background <select> from the catalog + the built-in "None".
function _populateBgSelect(sel, cat, current) {
  if (!sel) return;
  sel.innerHTML = '';
  const none = document.createElement('option');
  none.value = 'none';
  none.textContent = 'None — plain background';
  sel.appendChild(none);
  cat.forEach((bg) => {
    const o = document.createElement('option');
    o.value = bg.id;
    o.textContent = bg.name + (bg.status && bg.status !== 'stable' ? ` (${bg.status})` : '');
    sel.appendChild(o);
  });
  const known = current === 'none' || cat.some((b) => b.id === current);
  sel.value = known ? current : 'none';
}

// Save the per-theme background choice (sends BOTH so WA_BG.setChoice is whole).
function _saveBackgroundChoice(theme, ctrl) {
  const sel = _qs('ac-bg-' + theme);
  if (!sel) return;
  _bgChoice[theme] = sel.value;
  try {
    if (window.WA_BG && typeof window.WA_BG.setChoice === 'function') {
      window.WA_BG.setChoice({ dark: _bgChoice.dark, light: _bgChoice.light });
    }
  } catch (_) {}
  _saveAppearance({ [`background_${theme}`]: sel.value }, ctrl);
}

// Wire the two background selects (built into the theme cards by _buildThemeCard).
function _wireBackgroundSelects() {
  ['dark', 'light'].forEach((theme) => {
    const sel = _qs('ac-bg-' + theme);
    if (!sel) return;
    _populateBgSelect(sel, _bgCatalog, _bgChoice[theme]);
    // Overlay sits on the select's control cluster (`.ac-theme-row-ctl`).
    sel.addEventListener('change', () => _saveBackgroundChoice(theme, sel.closest('.ac-theme-row-ctl')));
  });
}

// Wire a single appearance <select>/<input>: live-preview + save (with the
// green-tick indicator) on change. Shared by the shared-controls and chat-layout
// groups below, which previously inlined byte-identical wire(id, key) closures.
function _wireAppearanceSelect(id, key) {
  const el = _qs(id);
  if (!el) return;
  el.addEventListener('change', () => {
    _previewAppearance({ [key]: el.value });
    _saveAppearance({ [key]: el.value }, el.closest('.ac-config-control'));
  });
}

function _isValidChatPanelWidth(value) {
  const v = String(value || '').trim();
  return /^(?:\d+(?:\.\d+)?|\.\d+)(?:px|%)$/.test(v) && Number.parseFloat(v) > 0;
}

function _wireChatPanelWidth() {
  const el = _qs('ac-chat-panel-width');
  if (!el) return;
  el.addEventListener('input', () => el.setCustomValidity(''));
  el.addEventListener('change', async () => {
    if (!isAdmin()) {
      showRestrictedModal();
      return;
    }
    const value = el.value.trim();
    if (!_isValidChatPanelWidth(value)) {
      el.setCustomValidity('Use a positive width in px or %, for example 420px or 35%.');
      el.reportValidity();
      return;
    }
    if (el.dataset.saving === '1') return;

    const ctrl = el.closest('.ac-config-control');
    const prior = _isValidChatPanelWidth(_appValues.chat_panel_width)
      ? _appValues.chat_panel_width.trim()
      : '380px';
    el.setCustomValidity('');
    el.value = value;
    // Preview is intentionally optimistic, but no success indicator is shown
    // until the response confirms the exact value that was persisted.
    _previewAppearance({ chat_panel_width: value });
    el.dataset.saving = '1';
    el.disabled = true;
    if (ctrl) _markSaving(ctrl);
    try {
      const result = await _saveAppearance({ chat_panel_width: value });
      const confirmed = result?.settings?.chat_panel_width;
      if (!result?.ok || confirmed !== value) {
        throw new Error(result?.error || 'Save was not confirmed by the server');
      }
      el.value = confirmed;
      _previewAppearance({ chat_panel_width: confirmed });
      if (ctrl) _flashSaveCheck(ctrl, true);
    } catch (e) {
      // Keep the server-confirmed value authoritative after a rejected or lost save.
      el.value = prior;
      _previewAppearance({ chat_panel_width: prior });
      if (ctrl) _flashSaveCheck(ctrl, false, e?.message || 'Save failed');
    } finally {
      delete el.dataset.saving;
      el.disabled = false;
    }
  });
}

// Wire the shared (theme-independent) controls: border thickness + fonts.
function _wireSharedControls() {
  _wireAppearanceSelect('ac-border-width', 'border_width');
  _wireAppearanceSelect('ac-font-sans', 'font_sans');
  _wireAppearanceSelect('ac-font-mono', 'font_mono');
}

// Wire the Chat panel layout selects (the separate "Chat panel" group). Both are
// app-wide appearance keys saved via _saveAppearance: `chat_position` (right/left)
// injects a #stage flex rule LIVE via _previewAppearance; `chat_default_visible`
// (visible/hidden) only seeds the first-visit desktop default (read at boot by
// index.html __chatVisibilityDefault), so it has no live effect on the admin's own
// already-toggled chat — just save + the green tick.
function _wireChatLayout() {
  _wireAppearanceSelect('ac-chat-position', 'chat_position');
  _wireChatPanelWidth();
  _wireAppearanceSelect('ac-chat-default-visible', 'chat_default_visible');
}

// Add a "Custom" option if a saved select value isn't one of the presets, so a
// hand-edited JSON value round-trips intact.
function _selectOrCustom(sel, value) {
  if (!sel || !value) return;
  const has = Array.from(sel.options).some((o) => o.value === value);
  if (!has) {
    const o = document.createElement('option');
    o.value = value;
    o.textContent = 'Custom (from settings file)';
    sel.appendChild(o);
  }
  sel.value = value;
}

function _initAppearance() {
  // The row bodies are built lazily in _loadAppearance (after values + catalog
  // load), so the swatches reflect saved colours. Shared controls are static.
  _wireSharedControls();
  _wireChatLayout();
  // Make the two theme rows expand/collapse like the ability-table rows
  // (clicking a control inside the body never toggles — see _wireBootRow).
  _wireBootRow('ac-theme-row-light', null);
  _wireBootRow('ac-theme-row-dark', null);
  _initThemeSwitch();
}

// Wire the LIVE theme switch in the Design header (Light · Dark · System). It is
// the SAME per-browser toggle as the user-menu dropdown — clicking calls the
// shared setTheme (apply to <body> + persist + sync every `.theme-option`
// highlight). Wired here (not by user-panel.js's boot-time pass) because this
// admin page mounts after app boot, so those buttons don't exist yet at boot.
function _initThemeSwitch() {
  const btns = document.querySelectorAll('.ac-theme-switch .theme-option');
  if (!btns.length) return;
  btns.forEach((btn) => {
    if (btn.dataset.wired) return;        // guard against re-init double-binding
    btn.dataset.wired = '1';
    // pointerdown matches the dropdown's handler — more reliable on small targets.
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      e.preventDefault();
      setTheme(btn.dataset.theme);
    });
  });
  // Reflect the visitor's saved choice on these freshly-mounted buttons.
  highlightThemeOption(getSavedTheme());
}

// Load saved values + background catalog, then build the two theme cards and
// fill the shared controls.
async function _loadAppearance(data, bgData) {
  // Saved appearance values. Reuse the caller's pre-fetched payload when given
  // (load() shares one /admin/settings/app fetch across both consumers);
  // `bgData === undefined` means "not passed" → fetch the catalog ourselves.
  const _bgPassed = arguments.length >= 2;
  try {
    if (data) {
      _appValues = data;
    } else {
      const r = await _fetch(apiPath('/admin/settings/app'));
      if (r.ok) _appValues = await r.json();
    }
  } catch (e) {
    console.warn('settings: appearance values load failed', e);
  }
  if (_appValues.background_dark) _bgChoice.dark = _appValues.background_dark;
  if (_appValues.background_light) _bgChoice.light = _appValues.background_light;

  // User-saved custom themes (JSON array string).
  _customThemes = [];
  try {
    const arr = JSON.parse(_appValues.custom_themes || '[]');
    if (Array.isArray(arr)) _customThemes = arr.filter((t) => t && t.id && t.name);
  } catch (e) { /* keep none */ }

  // Background catalog (for the per-theme selects). Reuse the pre-fetched list
  // when load() passed it; otherwise fetch standalone.
  try {
    if (_bgPassed) {
      _bgCatalog = Array.isArray(bgData?.backgrounds) ? bgData.backgrounds : [];
    } else {
      const cr = await _fetch(apiPath('/admin/settings/backgrounds'));
      if (cr.ok) { const d = await cr.json(); _bgCatalog = Array.isArray(d.backgrounds) ? d.backgrounds : []; }
    }
  } catch (e) {
    console.warn('settings: background catalog load failed', e);
  }

  // Build the mirrored theme cards + wire their background selects.
  _buildThemeCard('light');
  _buildThemeCard('dark');
  _wireBackgroundSelects();
  _refreshPresetActive('light', _asHex(_appValues.accent_light, _APP_DEFAULTS.light.accent));
  _refreshPresetActive('dark', _asHex(_appValues.accent_dark, _APP_DEFAULTS.dark.accent));

  // Shared controls (fonts + border thickness).
  _selectOrCustom(_qs('ac-border-width'), _appValues.border_width || '1px');
  _selectOrCustom(_qs('ac-font-sans'), _appValues.font_sans || "'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif");
  _selectOrCustom(_qs('ac-font-mono'), _appValues.font_mono || "'JetBrains Mono', 'Fira Code', Menlo, ui-monospace, monospace");

  // Chat panel layout (separate "Chat panel" group).
  const cp = _qs('ac-chat-position');
  if (cp) cp.value = (_appValues.chat_position === 'left') ? 'left' : 'right';
  const cpw = _qs('ac-chat-panel-width');
  if (cpw) cpw.value = _isValidChatPanelWidth(_appValues.chat_panel_width) ? _appValues.chat_panel_width.trim() : '380px';
  const cdv = _qs('ac-chat-default-visible');
  if (cdv) cdv.value = (_appValues.chat_default_visible === 'hidden') ? 'hidden' : 'visible';
}

// ── Public API ───────────────────────────────────────────────────────────

export function init() {
  _initAppSettings();
}

export async function load() {
  // Fetch the shared /admin/settings/app payload ONCE and the background catalog
  // in parallel, then hand both to the two consumers. Previously _loadAppSettings
  // and _loadAppearance each did their own /admin/settings/app fetch (the endpoint
  // was hit 2-3× per open) and ran strictly serially.
  const appP = _fetch(apiPath('/admin/settings/app'))
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const bgP = _fetch(apiPath('/admin/settings/backgrounds'))
    .then((r) => (r.ok ? r.json() : null)).catch(() => null);
  const [appData, bgData] = await Promise.all([appP, bgP]);
  await _loadAppSettings(appData);
  await _loadAppearance(appData, bgData);
}

async function _saveGlobalPrompt() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const ta = _qs('ac-global-system-prompt');
  const btn = _qs('ac-global-prompt-save');
  _markSaving(btn);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ global_system_prompt: ta ? ta.value : '' }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(btn, true);
  } catch (e) {
    _flashSaveCheck(btn, false, e.message);
  }
}

async function _saveAppSettings() {
  const cb = _qs('ac-extend-llm-to-agents');
  const btn = _qs('ac-app-settings-save');
  _markSaving(btn);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        extend_llm_to_agents: cb ? cb.checked : true,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(btn, true);
  } catch (e) {
    _flashSaveCheck(btn, false, e.message);
  }
}

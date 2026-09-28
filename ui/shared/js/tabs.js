'use strict';

// tabs.js imports NOTHING from a drop-in page folder (ui/<page>/) — every page's
// start/stop is driven dynamically from its catalog descriptor (see below), so
// deleting a page folder can never break this module's load. The only static
// imports are app-shell INFRA that is never a removable page: the pinned Account
// view and the Admin Tools sub-panel inits (Admin Tools is the locked shell tab).
import { startAccount } from './account.js';
import { pageMemory } from './page-memory.js';
import { isAdmin } from './left-login.js?v=253';
import { showPageAccessGate, hidePageAccessGate } from './page-access-gate.js?v=253';
import { app } from './state.js';
import { getActive } from './accounts.js';
import { createChatLauncher } from '../../chat-widget/js/chat-launcher.js';

// ── Lazy Admin Tools init ───────────────────────────────────────────────────
// The Admin Tools sub-panels (App Config, Database viewer, Data Management,
// Remote Access) are NOT built at boot anymore — they're wired the first time
// the user actually opens the Admin Tools tab. This keeps startup light,
// especially on mobile where only one panel is ever on screen. All four inits
// are null-safe + idempotent; initFiles() is run by startAdminTools() itself.
let _adminInited = false;
function _ensureAdminInit() {
  if (_adminInited) return;
  _adminInited = true;
  Promise.all([
    import('./index.js'),
    import('./data-management.js'),
    import('./remote-access.js'),
    import('./tunnel-link.js'),
    import('../../main-panel/app/settings/settings.js?v=7'),
  ]).then(([dbViewer, dataManagement, remoteAccess, tunnelLink, settings]) => {
    try { dbViewer.initDbViewer(); }             catch (e) { console.error('initDbViewer failed', e); }
    try { dataManagement.initDataManagement(); } catch (e) { console.error('initDataManagement failed', e); }
    try { remoteAccess.initRemoteAccess(); }     catch (e) { console.error('initRemoteAccess failed', e); }
    try { tunnelLink.initTunnelLink(); }         catch (e) { console.error('initTunnelLink failed', e); }
    try { settings.initSettings(); }             catch (e) { console.error('initSettings failed', e); }
  }).catch((e) => console.error('Admin Tools initialization failed', e));
}

// Instances is an admin-only destination. A non-admin must never be ACTIVATED
// onto it — not an anonymous guest, and not a real admin in the brief boot
// window before the server confirms admin status — because activating it flashes
// the admin panels (App Settings etc.) and then full-screens the "Restricted
// Access" overlay. (File Manager and Admin Tools are now Instances tabs, gated
// by the Instances page itself.)
function _isAdminOnlyTab(tabValue) {
  return tabValue === 'instances';
}

// A main page hidden from this visitor by its 3-state visibility setting — a
// signed-out / anonymous guest on a 'registered-only' (auth) page, or anyone on
// an 'off' page. This is the SAME decision header-build uses to hide the tab
// button, so a hidden tab can no longer be silently reached through a ?tab= deep
// link, a middle-click, a restored lastActiveTab, the mobile dropdown, or an
// agent ui_command. When true, activateTab shows the inline sign-in gate IN the
// page area (page-access-gate.js) instead of mounting the page — it is never
// started, so its gated endpoints are never hit. (The server still enforces
// per-row data ownership independently; this is the UI-side access boundary.)
function _pageVisibilityBlocked(tabValue) {
  try {
    return typeof window.__pageHiddenByVisibility === 'function'
      && !!window.__pageHiddenByVisibility(tabValue);
  } catch (_) { return false; }
}

function _selectOptionBlocked(tabValue, tabSelect) {
  if (!tabValue || !tabSelect) return false;
  return !Array.from(tabSelect.options || []).some((option) => option.value === tabValue);
}

function _anonymousIdentityBlocked(tabValue) {
  if (tabValue !== 'instances') return false;
  let uid = String((app && app.currentUserId) || '');
  try { uid = String((getActive() && getActive().user_id) || uid); } catch (_) {}
  try { uid = String(localStorage.getItem('auth_user_id') || uid); } catch (_) {}
  try { uid = String(document.getElementById('top-user-id')?.title || uid); } catch (_) {}
  try { uid = String(document.querySelector('#top-user-avatar-slot .user-avatar')?.title || uid); } catch (_) {}
  return uid.indexOf('anon_') === 0;
}

function setChatPanelVisible(visible) {
  const chatPanel = document.getElementById('chat-panel');
  const resizeHandle = document.getElementById('chat-resize-handle');
  if (chatPanel) chatPanel.style.display = visible ? '' : 'none';
  if (resizeHandle) resizeHandle.style.display = visible ? '' : 'none';
}

// ── Page lifecycle dispatch ──────────────────────────────────────────────────
// EVERY page — core or dropped-in — is driven the same way: a DYNAMIC import of
// the module + the export names declared in its page.json descriptor (served on
// window.__pagesCatalog via /api/v1/pages/catalog). So adding OR removing a page
// needs no edit here. The only non-catalog destinations are app-shell views that
// are not pages (the pinned "Manage Account" entry); they live in _SHELL_HOOKS.
const _SHELL_HOOKS = {
  'account': { start: startAccount },
};

let _activePageId = null;
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  DYN-IMPORT-CACHE — evict on failure, never cache a rejection.         ║
// ║  A failed dynamic import must NOT poison the module cache.  The next    ║
// ║  tab activation retries with a fresh import.  Sister site:              ║
// ║  _dynAdminMods in files.js (same contract).  DO NOT simplify back to    ║
// ║  `try/catch Promise.reject(e)` — that caches the rejection forever.     ║
// ║  (grep `DYN-IMPORT-CACHE` to find both sites).                          ║
// ╚══════════════════════════════════════════════════════════════════════════╝
const _dynMods = {};   // page id → Promise<module> (evicted on failure)

function _catalogPage(id) {
  try {
    const c = window.__pagesCatalog;
    const list = (c && Array.isArray(c.main)) ? c.main : [];
    return list.find((p) => p.id === id) || null;
  } catch (_) { return null; }
}

function _dynModule(id, entry) {
  if (!_dynMods[id]) {
    // DYN-IMPORT-CACHE: evict the rejection so the next activation retries.
    const moduleUrl = new URL(entry, document.baseURI);
    // The running backend memoizes page.json descriptors until restart. Keep the
    // security-sensitive Instances module independently cache-bustable so a shell
    // refresh can deliver an auth-gate fix even while an older catalog is live.
    if (id === 'instances') moduleUrl.searchParams.set('v', '255');
    if (id === 'agents') moduleUrl.searchParams.set('v', '515');
    // Same trick for the Wiki page: its sidebar-popover layout ships in this
    // module + wiki.css, so bump this whenever those assets change.
    if (id === 'wiki') moduleUrl.searchParams.set('v', '6');
    _dynMods[id] = Promise.resolve()
      .then(() => import(moduleUrl.href))
      .catch((e) => { delete _dynMods[id]; throw e; });
  }
  return _dynMods[id];
}

function _startPage(id) {
  _activePageId = id;
  const h = _SHELL_HOOKS[id];
  if (h) {
    try { if (h.start) h.start(); } catch (e) { console.error('start ' + id + ' failed', e); }
    _mountPageLauncher(id, null);
    return;
  }
  const p = _catalogPage(id);
  // Mount the page's chat launcher (page.json `widget` block) as soon as the
  // tab activates — it floats over the app like the global launcher.
  _mountPageLauncher(id, p);
  if (p && p.entry && p.start) {
    _dynModule(id, p.entry)
      .then((m) => {
        const fn = m && m[p.start];
        if (typeof fn === 'function') fn();
        // Restore the page's saved view state after it has started rendering.
        _recallPage(id, p, m);
      })
      .catch((e) => {
        console.error('start ' + id + ' failed', e);
        const target = document.getElementById('tab-' + id);
        if (target && target.classList.contains('active')) {
          target.removeAttribute('aria-busy');
          target.innerHTML = '<section role="status" data-page-start-state="failed" '
            + 'style="max-width:460px;margin:auto;padding:36px 28px;text-align:center">'
            + '<h2 style="font-size:18px;margin:0 0 10px">This page could not be opened</h2>'
            + '<p style="margin:0;color:var(--fg-3)">Try again in a moment.</p></section>';
        }
      });
  } else {
    // Static / iframe-only pages still get their DOM snapshot restored.
    _recallPage(id, p, null);
  }
}

async function _stopPage(id) {
  const h = _SHELL_HOOKS[id];
  const p = _catalogPage(id);
  // The page's chat launcher (page.json `widget` block) only exists while its
  // tab is active — tear it down on leave so each page gets its own.
  _destroyPageLauncher(id);
  // Capture the page's view state BEFORE its stop hook tears anything down —
  // the DOM snapshot is synchronous, then (best-effort) the page's declared
  // `remember` export contributes custom state (see page-memory.js).
  await _rememberPage(id, p);
  if (h) {
    if (h.stop) { try { h.stop(); } catch (_) {} }
    return;
  }
  if (p && p.entry && p.stop && _dynMods[id]) {
    _dynMods[id]
      .then((m) => { const fn = m && m[p.stop]; if (typeof fn === 'function') fn(); })
      .catch(() => {});
  }
}

// ── Per-page chat launcher (page.json `widget` block) ───────────────────────
// A catalog page may declare a `widget` block in its page.json — the SAME shape
// as createChatLauncher options in ui/chat-widget/js/chat-launcher.js (icon,
// agent_id, corner buttons, widget options…). The shell mounts the page's own
// launcher when the tab activates and destroys it on deactivate, so every page
// can have a distinct chat launcher without any per-page code. Lives in the
// main document (the widget layer attaches to <body>) so it floats like the
// global WebAgent launcher.
const _pageLaunchers = {};

function _mountPageLauncher(id, p) {
  if (!p || !p.widget || typeof p.widget !== 'object' || _pageLaunchers[id]) return;
  const cfg = p.widget;
  const agentId = cfg.agent_id || cfg.agentId || null;
  let ensureAgent = null;
  if (!agentId && app && typeof app.ensureWebagentAgent === 'function') {
    ensureAgent = () => app.ensureWebagentAgent(app.currentUserId);
  }
  try {
    _pageLaunchers[id] = createChatLauncher({
      mountEl: document.body,
      position: cfg.position || 'fixed',
      icon: cfg.icon || 'bot',
      iconSize: cfg.iconSize,
      ariaLabel: cfg.ariaLabel || `Ask about ${p.label || id}`,
      label: cfg.label,
      showLabel: !!cfg.showLabel,
      agentId: agentId || null,
      ensureAgent,
      cornerButtons: cfg.cornerButtons,
      cornerActions: cfg.cornerActions,
      widget: (cfg.widget && typeof cfg.widget === 'object') ? cfg.widget : {},
      draggable: cfg.draggable,
      storageKey: cfg.storageKey || ('page-launcher:' + (app.currentUserId || 'anon') + ':' + id),
      elementPickup: !!cfg.elementPickup,
      hoverDelayMs: cfg.hoverDelayMs,
      onOpen: (typeof cfg.onOpen === 'function') ? cfg.onOpen : null,
      onClose: (typeof cfg.onClose === 'function') ? cfg.onClose : null,
    });
  } catch (e) {
    console.error('page launcher mount failed for ' + id, e);
  }
}

function _destroyPageLauncher(id) {
  const l = _pageLaunchers[id];
  if (l) {
    try { l.destroy(); } catch (_) {}
    delete _pageLaunchers[id];
  }
}

// Only the previously-active page can be "running", so stopping it (when it
// isn't the new target) is sufficient and idempotent. Admin Tools' stop hook
// (stopAdminTools) owns its sidebar sub-views' lifecycle, so leaving Admin Tools
// tears them down here too.
async function _stopAllExcept(target) {
  if (_activePageId && _activePageId !== target) {
    await _stopPage(_activePageId);
  }
}

// ── Page memory — automatic save/recall of every page's view state ─────────
// Every catalog page (built-in or drop-in) gets its view state remembered in
// localStorage — scroll positions, open panels/toggles, selects/checkboxes,
// [data-memory] elements — and restored when the page starts again (tab switch
// or refresh). Zero per-page code: enabled by default for every page; a page
// opts out with "memory": false in page.json, or adds richer state with
// "memory": {"remember": "fn", "recall": "fn"} exports in its entry module
// (remember() returns state on leave, recall(state) receives it on return).
// See ui/shared/js/page-memory.js for the storage contract.
function _memoryEnabled(p) {
  return pageMemory.enabled(p);
}

function _pageMount(id, p) {
  if (p && p.mount) {
    try { const el = document.querySelector(p.mount); if (el) return el; } catch (_) {}
  }
  return document.getElementById('tab-' + id);
}

// Called by _stopPage BEFORE the page's stop hook: snapshot the page's DOM
// synchronously, then ask the page's declared `remember` export for custom
// state. The stop hook runs after, so remember() sees the page as the user
// left it (module-scope state survives; don't depend on the DOM here).
async function _rememberPage(id, p) {
  if (!_memoryEnabled(p)) return;
  const state = { dom: pageMemory.captureDom(_pageMount(id, p)), custom: null };
  if (p && p.entry && p.memory && p.memory.remember && _dynMods[id]) {
    try {
      const m = await _dynMods[id];
      const fn = m && m[p.memory.remember];
      if (typeof fn === 'function') state.custom = fn() || null;
    } catch (_) {}
  }
  pageMemory.save(id, state);
}

// Called by _startPage AFTER the page's start hook: hand custom state to the
// page's declared `recall` export, then re-apply the DOM snapshot (scroll is
// retried on a schedule because the page may still be rendering).
function _recallPage(id, p, mod) {
  if (!_memoryEnabled(p)) return;
  const state = pageMemory.load(id);
  if (!state) return;
  if (mod && p && p.memory && p.memory.recall) {
    try {
      const fn = mod[p.memory.recall];
      if (typeof fn === 'function') fn(state.custom || null);
    } catch (_) {}
  }
  pageMemory.restoreDom(_pageMount(id, p), state.dom);
}

// ── Address-bar sync ─────────────────────────────────────────────────────────
// Each tab "owns" any deep-link sub-params it puts in the URL. When the user
// navigates to a DIFFERENT tab, those params must be dropped so a hotlink like
// ?tab=wiki&article=foo doesn't leave a stale &article= behind. (While a page is
// active it keeps its own param fresh — e.g. wiki.js mirrors the open article and
// files.js mirrors the Admin Tools sidebar view via history.replaceState.) Add a
// row here when a new page gains a hotlink sub-param.
const _PAGE_PARAMS = { app: ['view'], wiki: ['article'], 'admin-tools': ['view'], agents: ['agent', 'agentView'] };

// The landing page for every no-param /app load is Agents, regardless of role.
// If Agents is not present (for example, removed by an entitlement/catalog
// change), fall back to the first visible non-admin page.
function _computeDefaultTab() {
  const preferred = 'agents';
  const preferredButton = document.querySelector(
    `#main-tabs .main-tab[data-value="${preferred}"]`
  );
  if (preferredButton && preferredButton.style.display !== 'none'
      && !_pageVisibilityBlocked(preferred)) {
    return preferred;
  }
  const btns = Array.from(document.querySelectorAll('#main-tabs .main-tab[data-value]'));
  for (const b of btns) {
    const v = b.dataset.value;
    if (!v || v === 'admin-tools' || v === 'account') continue;
    if (b.style.display === 'none') continue;  // hidden by per-page visibility
    return v;
  }
  const tabSelect = document.getElementById('main-tab-select');
  const opts = tabSelect ? Array.from(tabSelect.options).map((o) => o.value) : [];
  return opts.find((v) => v && v !== 'admin-tools' && v !== 'account'
    && !(typeof window.__pageHiddenByVisibility === 'function'
         && window.__pageHiddenByVisibility(v))) || '';
}

// Mirror the active main tab into the address bar (?tab=<id>) without reloading,
// and strip every OTHER page's deep-link params. This is what makes a refresh
// re-open the page you're actually on instead of snapping back to whatever
// ?tab=/&article= you first arrived through. Best-effort — never throws.
//
// Two refinements keep the bar honest:
//  • CLEAN DEFAULT — when the target IS the canonical landing page, ?tab= adds
//    nothing (a bare /app opens there anyway), so we DROP the param (and that
//    page's own sub-params) rather than let a stale "?tab=agents" linger.
//  • OWN SUB-PARAMS PRESERVED — a non-default page keeps its own hotlink params
//    (wiki's &article=, Admin Tools' &view=); only sibling pages' params are
//    stripped. The page itself refreshes its sub-param via its own start hook /
//    window.__setMainSubView.
function _syncTabUrl(tabValue) {
  try {
    const url = new URL(location.href);
    const hasOwnedSubParam = (_PAGE_PARAMS[tabValue] || []).some((param) => url.searchParams.has(param));
    const isDefault = tabValue === _computeDefaultTab() && !hasOwnedSubParam;
    if (isDefault) url.searchParams.delete('tab');
    else url.searchParams.set('tab', tabValue);
    for (const owner in _PAGE_PARAMS) {
      // Keep the active non-default page's OWN params; strip everyone else's
      // (and, on the clean default URL, strip them all).
      if (owner === tabValue && !isDefault) continue;
      for (const p of _PAGE_PARAMS[owner]) url.searchParams.delete(p);
    }
    history.replaceState(history.state, '', url);
  } catch (_) { /* address-bar sync is best-effort */ }
}

export function initTabs() {
  const tabSelect = document.getElementById('main-tab-select');
  if (!tabSelect) return;
  const pendingEarlyTab = window.__pendingMainTab || '';
  window.__pendingMainTab = '';
  window.__mainTabsRouterReady = true;

  // Safe landing when a non-admin requests an admin-only view. This uses the
  // same Agents-first target as a plain /app navigation.
  function _safeDefaultTab() {
    return _computeDefaultTab();
  }

  let _userNavigated = false;
  let _lastRequestedTab = null;
  let _activationSequence = 0;
  function activateTab(tabValue, userInitiated) {
    const activationSequence = ++_activationSequence;
    if (userInitiated) {
      _userNavigated = true;
      _lastRequestedTab = tabValue;
    }
    // Back-compat: 'files' was the legacy id for what is now 'admin-tools'.
    // Saved state in older browsers will still hold 'files'.
    if (tabValue === 'files') tabValue = 'admin-tools';
    // Back-compat: 'database' was its own top-level tab; it now lives as a
    // sidebar view inside Admin Tools. Redirect and request that sub-view.
    if (tabValue === 'database') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'database'); } catch (_) {}
    }
    // Back-compat: 'flow' and 'loop-visual' top-level tabs moved into Admin
    // Tools as the Interactions and Runtime Loop strip views.
    if (tabValue === 'stream' || tabValue === 'flow') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'interactions'); } catch (_) {}
    }
    if (tabValue === 'loop-visual') {
      tabValue = 'admin-tools';
      try { localStorage.setItem('files.sidebarView', 'runtime-loop'); } catch (_) {}
    }

    // No tenant catalog means there is no page we can safely activate. Keep the
    // explicit cached-view-unavailable panel visible instead of converting a
    // missing default page into the generic account-access gate. Account remains
    // reachable so the user can sign in/out or change identity.
    const catalogUnavailable = window.__pagesCatalog?._unavailable === true;
    if (catalogUnavailable && tabValue !== 'account') {
      document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
      const notice = document.querySelector('[data-catalog-unavailable="1"]');
      if (notice) notice.classList.add('active');
      hidePageAccessGate();
      _stopAllExcept(null);
      _activePageId = null;
      return;
    }

    // Admin Tools boot-race guard. Admin status isn't known yet at boot (the
    // /check-access probe is async), so a not-yet-confirmed admin is redirected
    // to a safe, visible page rather than flashing the admin panels. A real admin
    // is bounced back to Admin Tools by the admin-status-loaded handler once the
    // server confirms — see the deferred restore after the initial activate. (Its
    // own panel shows a Restricted treatment for confirmed non-admins, so Admin
    // Tools keeps the redirect rather than the inline sign-in gate below.)
    if (_isAdminOnlyTab(tabValue) && !isAdmin()) {
      tabValue = _safeDefaultTab();
      if (tabSelect.querySelector(`option[value="${tabValue}"]`)) tabSelect.value = tabValue;
    }

    // Visibility gate. A main page hidden from this visitor (anonymous guest on a
    // registered-only page, anyone on an 'off' page) is NOT redirected — instead
    // we leave the page unmounted and show the inline sign-in gate over the main
    // panel, so the visitor signs in right where they tried to go. Covers every
    // entry point (?tab= deep link, dropdown, restored lastActiveTab, agent
    // ui_command). Signing in reloads and re-runs this check.
    // Authoritative select membership is an access boundary. The catalog omits
    // disallowed/unavailable pages from the hidden select, but a stale/locked
    // visible header button or deep link can still carry their page id. Fail
    // closed here, before partial hydration or _startPage; this does not depend
    // on identity state being synchronized yet.
    // Pinned shell hooks (currently Manage Account) intentionally have no
    // catalog descriptor. They remain available independent of catalog loading
    // and must not be fail-closed as unknown catalog IDs.
    const isShellHook = !!_SHELL_HOOKS[tabValue];
    const gated = !!tabValue
      && !isShellHook
      && (_pageVisibilityBlocked(tabValue)
        || _selectOptionBlocked(tabValue, tabSelect)
        || _anonymousIdentityBlocked(tabValue));

    document.querySelectorAll('.tab-content').forEach((c) => c.classList.remove('active'));
    if (!gated) {
      const targetContent = document.getElementById('tab-' + tabValue);
      if (targetContent) targetContent.classList.add('active');
    }

    if (tabValue) localStorage.setItem('lastActiveTab', tabValue);

    // Keep the address bar in step with the active tab so a refresh re-opens the
    // page you're actually on — not whatever ?tab=/&article= you arrived via.
    // Only on real navigation (dropdown change, tab click, agent ui_command);
    // the initial localStorage restore passes userInitiated=false so a plain
    // reload of "/" stays clean. For a gated page this keeps ?tab=<id> set so the
    // post-sign-in reload returns the visitor to the page they tried to open.
    if (userInitiated) _syncTabUrl(tabValue);

    // Mobile: explicit page pick from dropdown hides chat (reveals page / gate).
    // Initial load on mobile must respect saved chat-visibility — don't override.
    // Desktop: always reflect saved chat toggle preference.
    const isMobile = typeof window.__isMobileChatLayout === 'function'
      ? window.__isMobileChatLayout() : (window.innerWidth <= 514);
    if (isMobile) {
      if (userInitiated) {
        if (typeof window.__applyChatVisible === 'function') {
          window.__applyChatVisible(false);
        } else {
          setChatPanelVisible(false);
        }
      }
    } else {
      const stored = localStorage.getItem('chatPanelVisible');
      const visible = stored === null ? true : stored !== 'false';
      if (typeof window.__applyChatVisible === 'function') {
        window.__applyChatVisible(visible);
      } else {
        setChatPanelVisible(visible);
      }
    }

    if (gated) {
      // Stop whatever page was running and show the sign-in gate instead of
      // starting the gated one — no gated endpoint is ever called for a visitor
      // who can't see the page.
      _stopAllExcept(null);
      _activePageId = null;
      const gatedContent = document.getElementById('tab-' + tabValue);
      if (gatedContent) gatedContent.removeAttribute('aria-busy');
      showPageAccessGate(tabValue);
      return;
    }

    // Normal page: ensure any prior gate is torn down, then dispatch. Flow +
    // Runtime Loop are sidebar views inside Admin Tools, so stopAdminTools() (the
    // admin-tools page's stop hook) owns their lifecycle when leaving admin-tools.
    // Inactive page markup is loaded on demand. A page initializer must not run
    // before its own partial exists, but the active-tab paint above remains
    // synchronous so the click is acknowledged immediately.
    hidePageAccessGate();
    _stopAllExcept(tabValue);
    const targetContent = document.getElementById('tab-' + tabValue);
    if (targetContent) targetContent.setAttribute('aria-busy', 'true');
    const ensurePartial = typeof window.__ensurePagePartial === 'function'
      ? window.__ensurePagePartial(tabValue)
      : Promise.resolve();
    Promise.resolve(ensurePartial).then(() => {
      if (activationSequence !== _activationSequence) return;
      if (targetContent) targetContent.removeAttribute('aria-busy');
      if (tabValue === 'admin-tools') _ensureAdminInit();
      _startPage(tabValue);
    }).catch((error) => {
      if (targetContent) targetContent.removeAttribute('aria-busy');
      console.error('hydrate ' + tabValue + ' failed', error);
    });
  }

  // The visible header may contain a gated page that is intentionally omitted
  // from the guest <select>. Route its button id directly so the select cannot
  // collapse the requested value to an empty string before activateTab sees it.
  window.__routeMainTab = function (view, userInitiated = true) {
    if (!view) return;
    if (tabSelect.querySelector(`option[value="${view}"]`)) tabSelect.value = view;
    activateTab(view, userInitiated);
  };

  // On load: if ?tab=<tabValue> is in the URL, activate that tab. Used when
  // middle-clicking a header button opens a new window — and now also the path a
  // direct link to a GATED page travels: validate against the page's content
  // mount (created for every catalog page, gated or not) rather than the <select>
  // option, which is deliberately absent for gated pages. That way a deep link to
  // a registered-only page lands on the inline sign-in gate instead of being
  // silently ignored. `_deepLinkHandled` then suppresses the default initial
  // activation below so it can't overwrite the gate (or the deep-linked page).
  let _deepLinkHandled = false;
  let _deepLinkTab = null;
  (function () {
    try {
      var p = new URLSearchParams(location.search);
      var t = p.get('tab');
      if (t && document.getElementById('tab-' + t)) {
        _deepLinkTab = t;
        // Sub-view deep link: ?tab=admin-tools&view=<id> should open that sidebar
        // view. Seed the saved-view preference BEFORE activating so startAdminTools
        // restores it (applySidebarView coerces an unknown id back to Explorer).
        // This only writes a UI preference — admin access is still gated by
        // activateTab + the server, so a non-admin's link can never reach the view.
        if (t === 'admin-tools') {
          var v = p.get('view');
          if (v) { try { localStorage.setItem('files.sidebarView', v); } catch (_) {} }
        }
        if (tabSelect.querySelector('option[value="' + t + '"]')) tabSelect.value = t;
        // The incoming URL is already canonical. Do not run generic tab sync before
        // an asynchronously loaded page can consume its owned sub-view parameter.
        // Later user navigation still invokes activateTab(..., true).
        activateTab(t, false);
        _deepLinkHandled = true;
      }
    } catch (_) {}
  })();

  // Programmatic view switch for the App-control ability (agent-driven, via a
  // `ui_command` WebSocket event). Mirrors a user picking from the dropdown:
  // sync the select (when the option exists) then activate the tab.
  window.__setMainTab = function (view) {
    if (!view) return;
    try {
      if (tabSelect.querySelector(`option[value="${view}"]`)) {
        tabSelect.value = view;
      }
      activateTab(view, true);
    } catch (_) { /* ignore */ }
  };

  // Reflect a page's active SUB-VIEW in the address bar (e.g. the Admin Tools
  // sidebar view → ?tab=admin-tools&view=database) so a sub-view is shareable and
  // survives a refresh. The page that owns the sub-view (files.js' applySidebarView)
  // calls this whenever the view changes. Guards keep the bar honest:
  //  • only the LIVE page may write (a background re-render can't hijack the bar),
  //  • the param must be declared in _PAGE_PARAMS for that page,
  //  • &view= is never written unless ?tab=<page> is already present, and
  //  • the page's DEFAULT sub-view is the bare ?tab=<page> (param omitted).
  // SECURITY: this only MIRRORS UI state into the URL — it grants nothing. The
  // Admin Tools sub-views stay gated by activateTab (a non-admin is redirected to
  // the default page, never activated onto admin-tools) and by the server, where
  // every admin endpoint is independently _require_admin. Reading the param back
  // (the deep-link handler below) likewise only seeds a UI preference.
  window.__setMainSubView = function (pageId, viewId, defaultView) {
    try {
      if (_activePageId !== pageId) return;
      const owned = _PAGE_PARAMS[pageId];
      if (!owned || owned.indexOf('view') === -1) return;
      const url = new URL(location.href);
      if (url.searchParams.get('tab') !== pageId) return;
      const cur = url.searchParams.get('view') || '';
      const next = (!viewId || viewId === (defaultView || 'explorer')) ? '' : viewId;
      if (cur === next) return;
      if (next) url.searchParams.set('view', next);
      else url.searchParams.delete('view');
      history.replaceState(history.state, '', url);
    } catch (_) { /* address-bar sync is best-effort */ }
  };

  tabSelect.addEventListener('change', (e) => {
    activateTab(e.target.value, true);
  });

  // Header buttons are rebuilt when a fallback catalog is replaced by the
  // authoritative catalog. Bind navigation to the stable strip rather than to
  // the individual buttons so every replacement still travels through the one
  // activation path that changes button/content/lifecycle state together.
  const tabBar = document.getElementById('main-tabs');
  if (tabBar) {
    tabBar.addEventListener('click', (event) => {
      if (event.__mainTabRouted) return;
      const button = event.target.closest('.main-tab[data-value]');
      if (!button || !tabBar.contains(button) || button.disabled) return;
      const value = button.dataset.value;
      if (!value) return;
      if (tabSelect.querySelector(`option[value="${value}"]`)) {
        tabSelect.value = value;
      }
      activateTab(value, true);
    });
  }

  // Skip the default initial activation when a ?tab= deep link already activated
  // a page (or its gate) above — re-activating tabSelect.value here would drop the
  // gate and snap to the default tab.
  // On mobile, if the chat panel was visible on the last visit, the user was on
  // the chat view — not a main page tab. Skip activating any main page so the
  // refresh lands on the chat view with the same session (restored from
  // terminalSessionId) instead of starting a stale main page in the background.
  // Also apply the saved chat visibility here (not just in _initChatVisibility)
  // because this module script runs before DOMContentLoaded, so the body class
  // hasn't been set yet — without it the user would see the main panel.
  // A public-agent guest can have a successfully authorized, chat-only catalog.
  // There is no shell page to select in that case: stay on the existing chat
  // surface instead of activating the default Agents tab and showing its gate.
  const publicChatOnly = !!window.__agentId
    && window.__pagesCatalog?._unavailable !== true
    && Array.isArray(window.__pagesCatalog?.main)
    && window.__pagesCatalog.main.length === 0;
  if (publicChatOnly) {
    hidePageAccessGate();
    _stopAllExcept(null);
    _activePageId = null;
    if (typeof window.__applyChatVisible === 'function') window.__applyChatVisible(true);
    else setChatPanelVisible(true);
  } else if (pendingEarlyTab && document.getElementById('tab-' + pendingEarlyTab)) {
    if (tabSelect.querySelector(`option[value="${pendingEarlyTab}"]`)) {
      tabSelect.value = pendingEarlyTab;
    }
    activateTab(pendingEarlyTab, true);
  } else if (typeof window.__isMobileChatLayout === 'function' && window.__isMobileChatLayout()
      && typeof window.__getChatVisible === 'function' && window.__getChatVisible()) {
    if (typeof window.__applyChatVisible === 'function') window.__applyChatVisible(true);
  } else if (!_deepLinkHandled) {
    // A bare /app always starts in Agents rather than restoring an arbitrary
    // browser-local last tab.
    const initialTab = _computeDefaultTab() || 'agents';
    tabSelect.value = initialTab;
    activateTab(initialTab);
  }

  // When a slow catalog forced the shell to boot from the minimal Agents fallback,
  // reconcile its tab strip and choose the now-authoritative Agents landing once
  // its partials have been mounted.  Explicit deep links retain precedence.
  let _lateCatalogReady = !window.__catalogBootedFromFallback;
  const _reconcileDefaultLanding = () => {
    if (_deepLinkHandled || _userNavigated || !_lateCatalogReady) return;
    const target = _computeDefaultTab();
    if (target && tabSelect.querySelector(`option[value="${target}"]`)) {
      tabSelect.value = target;
      activateTab(target);
    }
  };
  window.addEventListener('pages-catalog-ready', () => {
    if (!window.__catalogBootedFromFallback) return;
    window.__catalogBootedFromFallback = false;
    _lateCatalogReady = true;
    // A deep link or click can target a page that was absent from the minimal
    // fallback catalog. Its tab paints immediately, but _startPage had no
    // descriptor to dispatch. Late hydration has now mounted its markup and
    // published the authoritative catalog, so reactivate the user's chosen page
    // once without changing their navigation intent.
    const requested = _deepLinkTab || _lastRequestedTab || tabSelect.value;
    if ((_deepLinkHandled || _userNavigated) && requested) {
      if (tabSelect.querySelector(`option[value="${requested}"]`)) tabSelect.value = requested;
      activateTab(requested, false);
      return;
    }
    _reconcileDefaultLanding();
  }, { once: true });

  // A direct Admin Tools deep link must still work once the server verifies the
  // admin role. A saved browser-local tab is deliberately NOT restored: a bare
  // /app always uses the tier landing rule above.
  if (_isAdminOnlyTab(_deepLinkTab) && !isAdmin()) {
    const _restoreAdminTab = (e) => {
      window.removeEventListener('admin-status-loaded', _restoreAdminTab);
      if (e && e.detail && e.detail.is_admin) {
        const isMobile = typeof window.__isMobileChatLayout === 'function'
          && window.__isMobileChatLayout();
        activateTab(_deepLinkTab, !isMobile);
      }
    };
    window.addEventListener('admin-status-loaded', _restoreAdminTab);
  }

  // Reconcile once admin status resolves in case the provisional catalog did
  // not yet contain Agents. Explicit ?tab= links retain precedence.
  if (!_deepLinkHandled) {
    window.addEventListener('admin-status-loaded', _reconcileDefaultLanding, { once: true });
  }

  // ── Middle-click on a header tab opens that page in a new browser tab ──
  // (The new-session + button's own middle-click handler moved with the button
  // into ui/chat/js/session-init.js when it relocated to the chat
  // header — that partial mounts after this shell-init runs.)
  (function () {
    var base = location.pathname.replace(/\/+$/, '') || '/';
    document.querySelectorAll('#main-tabs .main-tab').forEach(function (btn) {
      btn.addEventListener('auxclick', function (e) {
        if (e.button !== 1) return;  // middle-click only
        e.preventDefault();
        var val = btn.dataset.value;
        if (val) {
          window.open(base + '?tab=' + encodeURIComponent(val), '_blank');
        }
      });
    });
  })();

  // ── If this tab was opened with ?agent=X&new=1, start a fresh session ──
  (function () {
    try {
      var p = new URLSearchParams(location.search);
      var agentId = p.get('agent');
      // URL prompts are prefill-only. Sending and auto mode require a fresh,
      // one-use same-tab handoff created by the authenticated design form.
      var prompt = (p.get('prompt') || '').slice(0, 200000);
      var sendPrompt = false;
      var launchMode = null;
      var designLaunch = p.get('design_launch');
      if (designLaunch && agentId) {
        var launchKey = 'agent-design-launch:' + designLaunch;
        var rawLaunch = null;
        try {
          rawLaunch = sessionStorage.getItem(launchKey);
          sessionStorage.removeItem(launchKey);
          var launch = rawLaunch ? JSON.parse(rawLaunch) : null;
          if (launch
              && launch.version === 1
              && launch.agentId === agentId
              && typeof launch.prompt === 'string'
              && launch.prompt.length <= 200000
              && launch.mode === 'auto'
              && Date.now() - Number(launch.createdAt || 0) >= 0
              && Date.now() - Number(launch.createdAt || 0) <= 60000) {
            prompt = launch.prompt;
            sendPrompt = true;
            launchMode = 'auto';
          }
        } catch (_) {
          try { sessionStorage.removeItem(launchKey); } catch (_) {}
        }
      }
      if (prompt || p.has('send') || p.has('mode') || designLaunch) {
        p.delete('prompt');
        p.delete('send');
        p.delete('mode');
        p.delete('design_launch');
        var cleanUrl = location.pathname + (p.toString() ? '?' + p.toString() : '') + location.hash;
        history.replaceState(null, '', cleanUrl);
      }
      if (agentId && p.get('new') === '1') {
        // Clear localStorage saved session so the new tab doesn't load an old one
        try { localStorage.removeItem('lastActiveTab'); } catch (_) {}
        // Wait for the app state to be ready, then switch
        var _trySwitch = function () {
          if (typeof app !== 'undefined' && typeof app.switchToAgent === 'function' && app.currentUserId) {
            app.switchToAgent(agentId, { forceNewSession: true, silent: true });
            if (prompt) {
              var _prefillPrompt = function (attempt) {
                var input = document.getElementById('chat-input');
                if (input && app.currentAgentId === agentId) {
                  if (sendPrompt) {
                    if (app.currentSessionId && typeof app.sendChatMessage === 'function') {
                      if (launchMode === 'auto' && typeof app.setExecutionMode === 'function') {
                        app.setExecutionMode('auto');
                      }
                      app.sendChatMessage(prompt);
                      return;
                    }
                    if (attempt < 50) setTimeout(function () { _prefillPrompt(attempt + 1); }, 100);
                    return;
                  }
                  if (!input.value) {
                    input.value = prompt;
                    input.dispatchEvent(new Event('input', { bubbles: true }));
                  }
                  input.focus();
                  return;
                }
                if (attempt < 50) setTimeout(function () { _prefillPrompt(attempt + 1); }, 100);
              };
              _prefillPrompt(0);
            }
          } else {
            setTimeout(_trySwitch, 100);
          }
        };
        setTimeout(_trySwitch, 0);
      }
    } catch (_) {}
  })();

  // (Main-panel swipe-to-flip gesture removed — was causing accidental page switches.)

  // The pre-paint script set body.is-booting during init. Now that the
  // tab content is mounted, clear it — this stops the boot phantom
  // cursor (index.html) that drives the animated background while booting.
  document.body.classList.remove('is-booting');
  // Refresh starter prompts after the chat shell is mounted. Empty sessions use
  // the same bottom dock as active sessions; refresh must not alter its position.
  if (typeof app.refreshSuggestions === 'function') {
    try { app.refreshSuggestions(); } catch (_) { /* best-effort boot refresh */ }
  }
}

'use strict';

/**
 * Settings — orchestrator for the JSON-driven config area (Instances →
 * "This device" → Settings).
 *
 * Everything is driven by app-pages.json:
 *   • the landing LIST is rendered from the index's groups/sections,
 *   • each section's PAGE is opened/closed by id (page partials are injected
 *     into #settings-page-slot-* by the partial loader),
 *   • the unified SEARCH opens every matching section page directly,
 *   • the page-assistant pill maps hovered data-pa-area groups to their
 *     app-prompts page key via each page's data-pa-page.
 *
 * Lifecycle mirrors the old app-config/index.js (initSettings /
 * startSettings / stopSettings); the view boots them via settings-view.js
 * (window.initSettings etc., called by instances.js and tabs.js).
 */

import { _qs, _esc } from './utils.js';
import { mountSettingsPageChatWidget } from '../../../shared/js/settings-page-chat-widget.js';
import { createSettingsSearch } from './settings-search.js';
import { readInstanceCache, writeInstanceCache } from '../instance-cache.js';

// The App catalog is loaded by instances.js and is the single source of Settings rows and favorites.
const RECENT_STORAGE_PREFIX = 'webagent.settings.recent.v1:';
const RECENT_LIMIT = 3;

let _initialized = false;
let _active = false;
let _search = null;
let _index = null;   // app-pages.json payload
let _openId = null;  // section id of the currently-open page (null = list view)
let _initPromise = null;

function _allSections() {
  const catalog = window.WA_APP_PAGES;
  return (catalog?.groups || []).flatMap(group => (group.pageIds || []).map(id => {
    const page = catalog.pages?.[id];
    return page ? { ...page, id, title: page.label } : null;
  }).filter(Boolean));
}

function _sectionById(id) {
  return _allSections().find(section => section.id === id) || null;
}

function _pageElement(id) {
  return document.getElementById('settings-page-' + id);
}

function _recentStorageKey() {
  try {
    return RECENT_STORAGE_PREFIX + (localStorage.getItem('auth_user_id') || 'anonymous');
  } catch (_) {
    return null;
  }
}

function _recentIds() {
  const key = _recentStorageKey();
  if (!key) return [];
  try {
    const ids = JSON.parse(localStorage.getItem(key) || '[]');
    return Array.isArray(ids) ? ids.filter(id => typeof id === 'string') : [];
  } catch (_) {
    return [];
  }
}

function _rememberPage(id) {
  const key = _recentStorageKey();
  if (!key) return;
  const ids = [id, ..._recentIds().filter(savedId => savedId !== id)].slice(0, RECENT_LIMIT);
  try { localStorage.setItem(key, JSON.stringify(ids)); } catch (_) {}
}

function _settingsItemHtml(section) {
  const pinned = (window.WA_APP_PAGES?.headerPageIds || []).includes(section.id);
  const action = pinned ? 'Remove from header' : 'Add to header';
  return '<div class="settings-item-row"><button type="button" class="settings-item" data-settings-page-open="' + _esc(section.id) + '">'
    + '<span class="settings-item-icon"><i data-lucide="' + _esc(section.icon || 'circle') + '" class="lucide-icon ac-ico-16"></i></span>'
    + '<span class="settings-item-body"><span class="settings-item-title">' + _esc(section.title) + '</span><span class="settings-item-desc">' + _esc(section.description || '') + '</span></span></button>'
    + '<button type="button" class="settings-item-favorite" data-app-page-toggle="' + _esc(section.id) + '" aria-label="' + action + '" title="' + action + '" aria-pressed="' + String(pinned) + '">'
    + '<i data-lucide="' + (pinned ? 'star' : 'star') + '" class="lucide-icon ac-ico-16 settings-favorite-icon' + (pinned ? ' is-favorited' : '') + '"></i></button></div>';
}

// ── View modes: list ↔ page ↔ search ──────────────────────────────────────
function _showList() {
  const list = _qs('settings-list');
  const topbar = _qs('settings-topbar');
  if (list) list.hidden = false;
  if (topbar) topbar.hidden = true;
  for (const section of _allSections()) {
    const page = _pageElement(section.id);
    if (page) page.hidden = true;
  }
  _openId = null;
}

function _showPage(id) {
  const section = _sectionById(id);
  if (!section) return;
  const list = _qs('settings-list');
  const topbar = _qs('settings-topbar');
  const title = _qs('settings-topbar-title');
  if (list) list.hidden = true;
  if (topbar) topbar.hidden = false;
  if (title) title.textContent = section.title;
  for (const s of _allSections()) {
    const page = _pageElement(s.id);
    if (page) page.hidden = (s.id !== id);
  }
  _openId = id;
}

/** Search deactivated — restore whichever surface was showing before. */
function _restoreSurface() {
  if (_openId && _pageElement(_openId)) _showPage(_openId);
  else _showList();
}

/** Put an opened page at its own top.
 *
 *  `#app-config-content` carries no overflow of its own — the settings content is
 *  fully expanded inside the Instances "This device" card, so the element that
 *  actually scrolls is the outer `.inst-grid.carousel` (the same scroller
 *  sticky-nav.js and page-assistant.js resolve). Writing `content.scrollTop = 0`
 *  was therefore a no-op, and an opened page inherited whatever offset the landing
 *  list was left at. A page in the catalog's LAST group (Model Catalog) is only
 *  reachable after scrolling the list down, so it opened part-way through and the
 *  trailing "Free Model Ranking" block filled the viewport instead of the page's
 *  own top (heading, roster bar, chart).
 *
 *  The content block never sticks, so its FLOW offset is readable at any scroll
 *  position; subtracting the pinned tab strip's height lands the page's first row
 *  directly UNDER that strip instead of behind it. */
function _scrollPageToTop(page) {
  const content = page?.closest('#app-config-content');
  if (!content) return;
  const scroller = content.closest('.inst-grid.carousel') || content;
  if (scroller !== content) {
    const bar = scroller.querySelector('.inst-tabs-wrap') || scroller.querySelector('.inst-tabs');
    const flowTop = scroller.scrollTop + content.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
    const barHeight = bar ? Math.round(bar.getBoundingClientRect().height) : 0;
    const max = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
    const target = Math.max(0, Math.min(Math.round(flowTop - barHeight), max));
    if (Math.abs(scroller.scrollTop - target) > 2) scroller.scrollTop = target;
  }
  content.scrollTop = 0;   // defensive: keep the content's own box at its top when it does scroll
}

/** Open one section page (optionally scrolled/anchored to a group inside it).
 *  A section may live on another section's page (the integration categories
 *  render inside Agent Tools) — the index's `page` / `anchor` fields resolve
 *  where it actually is. */
export async function openPage(id, anchor = null) {
  const section = _sectionById(id);
  if (!section) return;
  // Usage Data intentionally remains the stable internal `users` route.  The
  // catalog handoff prevents a duplicate settings partial while keeping a
  // discoverable Settings entry.
  if (section.externalTab) {
    document.querySelector(`[data-tab="${section.externalTab}"]`)?.click();
    return;
  }
  const pageId = section.page || section.id;
  const targetAnchor = anchor || section.anchor || null;
  _rememberPage(id);
  try {
    await window.__ensureSettingsPagePartial?.(pageId);
    await _loadPageOwner(pageId);
  } catch (error) {
    console.error('Settings page load failed:', pageId, error);
    return;
  }
  _renderList();
  _search?.clear();          // leaves search mode; _restoreSurface shows the old surface
  _showPage(pageId);
  window.WA_SYNC_APP_VIEW?.(id);
  requestAnimationFrame(() => {
    let target = null;
    if (targetAnchor) {
      // Integration categories live inside the Agent Tools page as group rows
      // (#ac-group-<id>, built by admin-ability-table.js) — or, for legacy
      // markup, as collapsible category sections (#ac-cat-<id>).
      target = document.getElementById('ac-group-' + targetAnchor)
        || document.getElementById('ac-cat-' + targetAnchor)
        || document.getElementById('ac-group-body-' + targetAnchor);
    }
    if (target) {
      target.scrollIntoView({ block: 'start', behavior: 'smooth' });
    } else {
      _scrollPageToTop(_pageElement(pageId));
    }
  });
}

/** Back to the landing list. */
export function closePage() {
  _search?.clear();
  window.WA_SYNC_APP_VIEW?.('settings');
  _showList();
}

// ── Landing list (rendered from app-pages.json) ──────────────────────
function _renderList() {
  const list = _qs('settings-list');
  const catalog = window.WA_APP_PAGES;
  if (!list || !catalog) return;
  const recent = _recentIds().map(_sectionById).filter(Boolean).slice(0, RECENT_LIMIT);
  const recentGroup = recent.length ? '<section class="settings-group settings-recent-group" data-settings-group="recent"><div class="settings-group-label"><i data-lucide="history" class="lucide-icon ac-ico-14"></i><span>Recent</span></div><div class="settings-group-card">' + recent.map(_settingsItemHtml).join('') + '</div></section>' : '';
  const groups = (catalog.groups || []).map(group => {
    const rows = (group.pageIds || []).map(_sectionById).filter(Boolean).map(_settingsItemHtml).join('');
    return rows ? '<section class="settings-group" data-settings-group="' + _esc(group.id) + '"><div class="settings-group-label"><i data-lucide="' + _esc(group.icon || 'circle') + '" class="lucide-icon ac-ico-14"></i><span>' + _esc(group.label) + '</span></div><div class="settings-group-card">' + rows + '</div></section>' : '';
  }).join('');
  list.innerHTML = recentGroup + groups;
  list.onclick = (e) => {
    const toggle = e.target.closest('[data-app-page-toggle]');
    if (toggle) { e.preventDefault(); e.stopPropagation(); window.WA_UPDATE_APP_PAGES?.(toggle.dataset.appPageToggle, 'toggle').catch(console.error); return; }
    const item = e.target.closest('[data-settings-page-open]');
    if (!item) return;
    const section = _sectionById(item.dataset.settingsPageOpen);
    if (!section) return;
    if (section.runtime && section.runtime !== 'settings') window.WA_OPEN_APP_PAGE?.(section.id);
    else openPage(section.id, section.anchor || null);
  };
}

// ── Settings search engine + floating launcher ────────────────────────────
// SETTINGS-SEARCH-CONTROLLER: the Settings area no longer owns a visible search
// field of its own. The floating "Ask or Search" launcher
// (ui/shared/js/settings-page-chat-widget.js) supplies the input and calls the
// function published below, so every matching rule stays in settings-search.js.
function _initSettingsSearch() {
  const content = _qs('app-config-content');
  if (!content) return;

  _search = createSettingsSearch({
    root: content,
    empty: _qs('settings-search-empty'),
    list: _qs('settings-list'),
    topbar: _qs('settings-topbar'),
    getPages: () => [...content.querySelectorAll('.settings-page')],
    pageMeta: new Map(_allSections().map(section => [section.id, section])),
    onDeactivate: _restoreSurface,
  });

  // The launcher resolves this by name and forwards its live text here.
  content._settingsSearchController = (query) => _search?.filter(query);

  mountSettingsPageChatWidget();
}

// ── Public lifecycle (mirrors the old app-config exports) ─────────────────
/** Called once at boot — loads the index, wires the section modules, renders
 *  the list and creates the unified search/chat pill. Idempotent: concurrent
 *  callers (tabs.js at boot + instances.js on tab open) share one in-flight
 *  promise so the section modules are never initialised twice. */
export function initSettings() {
  if (_initialized) return _initPromise;
  if (_initPromise) return _initPromise;
  _initPromise = (async () => {
    // The index is static settings chrome, but it previously blocked every cold
    // Settings-tab open on a network fetch. Use the tenant IDB row immediately
    // and revalidate it in the background; first-ever loads still await fetch.
    _index = window.WA_APP_PAGES || readInstanceCache('app:pages:v2') || null;
    if (!_index) {
      console.error('DEBUG-TAG: Settings catalog was unavailable during initialization');
      return;
    }
    writeInstanceCache('app:pages:v2', _index, 24 * 60 * 60 * 1000);
    _renderList();
    window.addEventListener('wa-app-pages-changed', () => _renderList());
    // Page chrome: the topbar's back button returns to the landing list.
    _qs('settings-back-btn')?.addEventListener('click', closePage);
    _initSettingsSearch();
    _initialized = true;
  })();
  return _initPromise;
}

/** Called when the Settings view becomes active — loads data for all pages. */
export async function startSettings({ preserveScroll = false } = {}) {
  // Instances periodically rebuilds its outer detail card as fleet state changes.
  // The settings container itself is persistent and is re-parented into the new
  // card, so a remount must not reload every section and overwrite in-progress
  // form edits. Mark the lifecycle active before awaiting initialization so two
  // overlapping remounts cannot both pass the guard.
  if (_active) return;
  _active = true;
  try {
    if (!_initialized) await initSettings();
  } catch (error) {
    _active = false;
    throw error;
  }
  // The view may have been stopped while its first initialization was pending.
  if (!_active) return;
}

/** Called when leaving the Settings view. */
export function stopSettings() {
  _active = false;
  _ownerModules.get('agent')?.stop?.();
}
let _openGeneration = 0;
const _ownerModules = new Map();
const _ownerInitialized = new Set();
const _ownerLoads = new Map();
const PAGE_OWNERS = {
  'app-access': 'data', 'storage-routing': 'data',
  'main-panel-pages': 'app', 'admin-panel-pages': 'app', 'app-functions': 'app',
  'design': 'app', 'templates-prompts': 'app', 'browser-session-management': 'browser',
  'agent-tools': 'agent', 'automation-engine': 'agent', 'models': 'agent',
  'entitlements': 'entitlements', 'user-policies': 'user-policies', 'bonus-policies': 'bonus-policies',
};
const OWNER_LOADERS = {
  data: () => import('./data-settings/data-settings.js'),
  app: () => import('./app-settings/app-settings.js'),
  browser: () => import('./browser-session-management.js'),
  agent: () => import('./agent-settings/agent-settings.js?v=10'),
  models: () => import('./entitlements/entitlements.js'),
  entitlements: () => import('./entitlements/entitlements.js'),
  'user-policies': () => import('./user-policies.js'),
  'bonus-policies': () => import('./bonus-policies.js?v=2'),
};
async function _loadPageOwner(pageId) {
  const owner = PAGE_OWNERS[pageId];
  if (!owner) return;
  if (_ownerLoads.has(owner)) return _ownerLoads.get(owner);
  const task = (async () => {
    let module = _ownerModules.get(owner);
    if (!module) { module = await OWNER_LOADERS[owner](); _ownerModules.set(owner, module); }
    if (!_ownerInitialized.has(owner)) {
      if (owner === 'bonus-policies') module.initBonusPolicies(); else module.init?.();
      _ownerInitialized.add(owner);
    }
    if (owner === 'models') { module.showModelRosters?.(); return module.load?.(); }
    if (owner === 'bonus-policies') return module.loadBonusPolicies();
    return module.load?.();
  })().finally(() => _ownerLoads.delete(owner));
  _ownerLoads.set(owner, task);
  return task;
}

'use strict';

// Wiki tab — the company-wide, searchable knowledge base.
// REMOVE-WHEN: the Wiki page is dropped from the page catalog — this whole folder
// (ui/main-panel/wiki/) is a drop-in page and dies with it.
// Markup: ui/main-panel/wiki/wiki.html · styles: ui/main-panel/wiki/wiki.css · API: app/api/wiki.py.
// One shared collection of articles (NOT per-user). People search/browse/edit
// here; agents manage the same data through the wiki_control ability. Lifecycle
// (startWiki/stopWiki) is driven by ui/shared/js/tabs.js.
//
// Layout is a persistent encyclopedia shell: a banner on top, a TREE SIDEBAR on
// the left (articles grouped by category, collapsible), and the ARTICLE on the
// right. Searching or applying a tag/category filter re-shapes the sidebar; the
// article surface on the right stays put.
//
// Deep-linking: the tab opens at ?tab=wiki (handled by tabs.js); this module
// extends it to a specific article with ?tab=wiki&article=<slug>. On open we
// read that param and jump straight to the article; while reading we mirror the
// open article's slug back into the address bar (history.replaceState) so any
// article URL is copy-pasteable. README + other docs link articles this way.
//
// Public pages: every PUBLISHED article also has a separate server-rendered,
// crawlable page at /wiki/<slug> (app/wiki/public_pages.py) — real HTML for
// search engines, the same data as here. The reader's "Copy link" button
// (_copyPublicLink) copies that public URL; it's hidden for drafts.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { authHeaders, isAdmin } from '../../../shared/js/left-login.js?v=253';
import { _esc } from '../../../shared/js/dom-utils.js';
import { copyText } from '../../../shared/js/clipboard.js';

// A signed-in member (not an anonymous visitor) — gates the write controls and
// whether drafts are even returned by the API. Anonymous users get an 'anon_…'
// id; members get a real one (email / 'admin').
function _isMember() {
  const uid = String(app.currentUserId || '');
  return !!uid && !uid.startsWith('anon_');
}

// Admin status for the wiki's create gate. `admin-status-loaded` carries the
// authoritative value in its event detail (the shared left-login module can be
// duplicated across versioned/unversioned imports, so we don't trust its
// in-module cache alone — see _applyRoleVisibility). Falls back to isAdmin()
// until the event is seen.
let _adminOverride = null;
function _canCreate() {
  return _isMember() && (_adminOverride !== null ? _adminOverride : isAdmin());
}

let _active = false;
let _inited = false;

// View state for the article surface.
let _current = null;   // the article being viewed, or null when nothing is open
let _editing = false;

let _searchTimer = null;
let _toastTimer = null;

// Browse + linking state.
let _allArticles = [];            // full (unfiltered) browse list
let _index = new Map();           // lowercased title/slug -> {slug, title}
let _activeFilter = null;         // { type:'category'|'tag', value } or null
let _viewingRevisionId = null;    // set while reading an old revision (read-only)
let _searchResults = null;        // ranked results while a search is active, else null
let _collapsed = new Set();       // category names currently collapsed in the tree
let _pendingDeepLink = null;      // slug from ?article= to open once articles load

const UNCATEGORIZED = 'General';  // tree group label for articles with no category

// ── Small DOM helpers ────────────────────────────────────────────────────────

function _root() { return document.getElementById('tab-wiki'); }
function _q(sel) { const r = _root(); return r ? r.querySelector(sel) : null; }

function _toast(msg, isError) {
  const el = _q('.wiki-toast');
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('error', !!isError);
  el.hidden = false;
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.hidden = true; }, 2600);
}

async function _api(path, opts) {
  // Always send the auth token so the backend knows whether the caller is a
  // member (sees drafts, may edit) or an anonymous visitor (published only).
  opts = opts || {};
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
  const res = await fetch(apiPath(path), opts);
  let data = null;
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || res.statusText;
    throw new Error(detail || `HTTP ${res.status}`);
  }
  return data;
}

// ── Lifecycle ────────────────────────────────────────────────────────────────

export function startWiki() {
  _active = true;
  _init();
  _applyRoleVisibility();
  _setSidebarOpen(false);   // always start collapsed; the popover opens on demand
  _updateNarrow();          // apply the popover/split layout immediately (no flash)
  // ?tab=wiki&article=<slug> → remember which article to jump to once the list
  // loads (so the sidebar can highlight it). _refresh() honours this.
  _pendingDeepLink = _readArticleParam();
  _refresh();
}

// Read the requested article slug from the URL (?article=<slug>), or '' if none.
function _readArticleParam() {
  try {
    const v = new URLSearchParams(location.search).get('article');
    return v ? v.trim() : '';
  } catch (_) { return ''; }
}

// Mirror the open article (or none) into the address bar without reloading, so
// the current article has a shareable ?tab=wiki&article=<slug> URL.
function _syncUrl(slug) {
  try {
    const url = new URL(location.href);
    url.searchParams.set('tab', 'wiki');
    if (slug) url.searchParams.set('article', slug);
    else url.searchParams.delete('article');
    history.replaceState(history.state, '', url);
  } catch (_) { /* address-bar sync is best-effort */ }
}

// Hide the editing controls from anonymous visitors, and hide article CREATION
// (the "New article" buttons) from everyone but admins. The backend enforces
// both; this just keeps the UI honest — anon users get a read-only, published-
// only wiki, and non-admin members can read + edit existing articles but not
// create new ones.
function _applyRoleVisibility() {
  const canCreate = _canCreate();
  const root = _root();
  if (!root) return;
  const fab = root.querySelector('.wiki-fab');
  if (fab) fab.hidden = !canCreate;
  const welcomeNew = root.querySelector('.wiki-welcome [data-act="new"]');
  if (welcomeNew) welcomeNew.style.display = canCreate ? '' : 'none';
}

// ── Sidebar popover (narrow wiki only) ──────────────────────────────────────
// The sidebar becomes a hidden-by-default POPOVER panel — like the main app's
// mobile nav — whenever the WIKI ITSELF is narrow: either the whole app is in
// mobile mode (body.mobile-mode) or the wiki panel is narrower than 750px even
// in desktop mode. The banner hamburger opens it floating OVER the content;
// outside click / Escape / picking an article closes it. When the wiki is
// wide, the always-visible split view stays. State lives in classes on the tab
// root: `wiki-narrow` (layout trigger, set by _updateNarrow) and
// `wiki-sidebar-open` (popover open/closed).
const _NARROW_PX = 750;

function _isNarrow() {
  const mount = _root();
  const w = mount ? mount.getBoundingClientRect().width : 0;
  return document.body.classList.contains('mobile-mode') || w < _NARROW_PX;
}

// Recompute the `wiki-narrow` class on the tab root. Returns the current
// narrow state; closes an open popover when the wiki widens back out.
function _updateNarrow() {
  const r = _root();
  if (!r) return false;
  const narrow = _isNarrow();
  const changed = r.classList.contains('wiki-narrow') !== narrow;
  r.classList.toggle('wiki-narrow', narrow);
  if (changed && !narrow) _setSidebarOpen(false);
  return narrow;
}

function _isSidebarOpen() {
  const r = _root();
  return !!(r && r.classList.contains('wiki-sidebar-open'));
}

function _setSidebarOpen(open) {
  const r = _root();
  if (!r) return;
  r.classList.toggle('wiki-sidebar-open', !!open);
  const t = r.querySelector('.wiki-sidebar-toggle');
  if (t) t.setAttribute('aria-expanded', open ? 'true' : 'false');
}

export function stopWiki() {
  _active = false;
  clearTimeout(_searchTimer);
}

function _init() {
  if (_inited) return;
  const root = _root();
  if (!root) return;
  _inited = true;

  // Search (debounced) + clear button.
  const search = _q('.wiki-search-input');
  const clear = _q('.wiki-search-clear');
  if (search) {
    search.addEventListener('input', () => {
      if (clear) clear.hidden = !search.value;
      if (search.value.trim()) _activeFilter = null;   // searching overrides a chip filter
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => _runSearch(), 220);
    });
  }
  if (clear && search) {
    clear.addEventListener('click', () => {
      search.value = '';
      clear.hidden = true;
      _searchResults = null;
      _renderSidebar();
      search.focus();
    });
  }

  // Ctrl/Cmd+K focuses the search box (matches the ⌘K hint in the banner).
  // Scoped to when the wiki tab is the active surface so it doesn't hijack the
  // shortcut from other pages. Escape collapses the sidebar popover.
  document.addEventListener('keydown', (e) => {
    if (!_active) return;
    if (e.key === 'Escape' && _isSidebarOpen()) { _setSidebarOpen(false); return; }
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      const box = _q('.wiki-search-input');
      if (box) { e.preventDefault(); box.focus(); box.select(); }
    }
  });

  // Close the sidebar popover on outside press/click. Registered in the
  // CAPTURE phase, like the main app's mobile nav: many app elements call
  // e.stopPropagation() in their own bubble-phase click handlers, which would
  // swallow the click before it reached a bubble-phase document listener and
  // leave the panel stuck open. pointerdown also dismisses on touch before the
  // tap resolves.
  const popover = _q('.wiki-sidebar');
  const toggle = _q('.wiki-sidebar-toggle');
  const _outsidePopover = (t) => !!(popover && toggle && !popover.contains(t) && !toggle.contains(t));
  document.addEventListener('pointerdown', (e) => {
    if (_active && _isSidebarOpen() && _outsidePopover(e.target)) _setSidebarOpen(false);
  }, true);
  document.addEventListener('click', (e) => {
    if (_active && _isSidebarOpen() && _outsidePopover(e.target)) _setSidebarOpen(false);
  }, true);

  // Track the narrow condition that switches the sidebar to a popover:
  // body.mobile-mode flips (MutationObserver on the body class) or the wiki
  // panel's own width changes (ResizeObserver — catches a narrow wiki column
  // in desktop mode, and the panel resizing while the app stays desktop).
  // _updateNarrow() reflects it as `wiki-narrow` on the tab root; leaving
  // narrow mode also closes any open popover.
  //
  // The observer watches the MOUNT (#tab-wiki), not .wiki-page: the page
  // partial can be re-injected on tab switches, which would orphan an observer
  // attached to the old .wiki-page and silently stop updating the class.
  const mount = _root();
  if (mount) {
    const ro = new ResizeObserver(() => _updateNarrow());
    ro.observe(mount);
  }
  const bo = new MutationObserver(() => _updateNarrow());
  bo.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  _updateNarrow();

  // Admin status resolves asynchronously after boot (fetchAdminStatus) — when it
  // lands, re-apply the New-article visibility gate. The event detail is the
  // authoritative admin flag (see _canCreate above).
  window.addEventListener('admin-status-loaded', (e) => {
    if (e && e.detail && typeof e.detail.is_admin === 'boolean') _adminOverride = e.detail.is_admin;
    _applyRoleVisibility();
  });

  // Scroll-spy: keep the "On this page" rail in sync with the article scroll.
  const scroll = root.querySelector('.wiki-article-scroll');
  if (scroll) scroll.addEventListener('scroll', () => _spyToc(), { passive: true });

  // Delegated clicks for the whole tab.
  root.addEventListener('click', (e) => {
    // 0) An "On this page" rail link → smooth-scroll to that heading.
    const tocLink = e.target.closest('.wiki-toc-link');
    if (tocLink && root.contains(tocLink)) {
      e.preventDefault();
      _gotoHeading(tocLink.dataset.toc);
      return;
    }
    // 1) A clickable [[wiki-link]] or backlink inside the reader.
    const link = e.target.closest('.wiki-link, .wiki-backlink');
    if (link && root.contains(link)) {
      e.preventDefault();
      if (link.dataset.slug) _openArticle(link.dataset.slug);
      else if (link.dataset.newtitle) _openNew(link.dataset.newtitle);
      return;
    }
    // 2) A tree category header → collapse / expand.
    const cat = e.target.closest('.wiki-tree-cat');
    if (cat && root.contains(cat)) {
      e.preventDefault();
      _toggleCategory(cat.dataset.cat);
      return;
    }
    // 3) A tree article link (sidebar) or a welcome "Start here" card → open it.
    const item = e.target.closest('.wiki-tree-item, .wiki-welcome-card');
    if (item && item.dataset.slug) {
      e.preventDefault();
      _openArticle(item.dataset.slug);
      _setSidebarOpen(false);   // picking an article collapses the drawer
      return;
    }
    // 4) A category/tag filter chip (on a row/reader) → filter the sidebar.
    const fchip = e.target.closest('[data-filter-type]');
    if (fchip && root.contains(fchip)) {
      e.preventDefault();
      e.stopPropagation();
      _applyFilter(fchip.dataset.filterType, fchip.dataset.filterValue);
      return;
    }
    // 5) A revision row's actions.
    const revAct = e.target.closest('[data-rev-act]');
    if (revAct && root.contains(revAct)) {
      e.preventDefault();
      const id = revAct.dataset.revId;
      if (revAct.dataset.revAct === 'view') _viewRevision(id);
      else if (revAct.dataset.revAct === 'restore') _restoreRevision(id);
      return;
    }
    // 6) Toolbar / banner / welcome actions.
    const btn = e.target.closest('[data-act]');
    if (btn && root.contains(btn)) {
      const act = btn.dataset.act;
      if (act === 'toggle-sidebar') _setSidebarOpen(!_isSidebarOpen());
      else if (act === 'new') _openNew();
      else if (act === 'edit') _enterEdit();
      else if (act === 'cancel') _cancelEdit();
      else if (act === 'save') _save();
      else if (act === 'delete') _delete();
      else if (act === 'toggle-status') _toggleStatus();
      else if (act === 'copy-link') _copyPublicLink();
      else if (act === 'history') _toggleHistory();
      else if (act === 'clear-filter') { _activeFilter = null; _renderSidebar(); }
      else if (act === 'restore-this' && _viewingRevisionId) _restoreRevision(_viewingRevisionId);
      else if (act === 'exit-revision') _exitRevisionView();
      return;
    }
  });
}

// ── Link index (for resolving [[Title]] → slug) ───────────────────────────────

function _buildIndex(articles) {
  _index = new Map();
  for (const a of articles) {
    if (!a || !a.slug) continue;
    const ref = { slug: a.slug, title: a.title };
    _index.set(a.slug.toLowerCase(), ref);
    if (a.title) _index.set(a.title.toLowerCase(), ref);
  }
}

// ── Derived display helpers (byline, reading time, relative dates) ────────────

// A friendly relative date: "today" / "yesterday" / "N days ago" / "Mon D, YYYY".
function _relDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return _esc(iso);
    const days = Math.round((Date.now() - d.getTime()) / 86400000);
    if (days <= 0) return 'today';
    if (days === 1) return 'yesterday';
    if (days < 7) return `${days} days ago`;
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  } catch (_) { return _esc(iso); }
}

// Turn a user id (email / 'admin' / 'agent:…') into a {name, initials} for the
// byline avatar. We only have an id from the API, so this is a best-effort label.
function _authorFrom(id) {
  let raw = String(id || '').trim();
  if (!raw) return null;
  raw = raw.replace(/^agent:/i, '');
  const local = raw.split('@')[0];                          // strip an email domain
  const words = local.replace(/[._-]+/g, ' ').trim().split(/\s+/).filter(Boolean);
  const name = words.map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ') || raw;
  const initials = (words.length >= 2
    ? words[0][0] + words[words.length - 1][0]
    : (local.slice(0, 2))).toUpperCase();
  return { name, initials };
}

// Rough reading time from a body's word count (~200 wpm), min 1.
function _readingTime(body) {
  const words = String(body || '').trim().split(/\s+/).filter(Boolean).length;
  return `${Math.max(1, Math.round(words / 200))} min read`;
}

// Footer line: when the wiki as a whole was last touched (newest updated_at).
function _renderSidebarFoot() {
  const foot = _q('.wiki-sidebar-foot');
  const txt = _q('.wiki-sidebar-foot-text');
  if (!foot || !txt) return;
  const latest = _allArticles.reduce((m, a) => (a.updated_at && a.updated_at > m ? a.updated_at : m), '');
  if (!latest) { foot.hidden = true; return; }
  txt.textContent = `Last updated ${_relDate(latest)}`;
  foot.hidden = false;
}

// ── Load articles + render the sidebar ────────────────────────────────────────

// Phantom shimmer rows for the sidebar tree while articles load — a set of
// category-header + article-row placeholders, so the sidebar has structure
// instead of a bare "Loading…" line. `.sk-shimmer` is shared (app3.css).
function _treeSkeleton(groups = 3, rowsPer = 3) {
  let html = '';
  for (let g = 0; g < groups; g++) {
    html += '<div class="wiki-tree-sk-group">';
    html += '<span class="wiki-sk sk-shimmer wiki-tree-sk-head"></span>';
    for (let r = 0; r < rowsPer; r++) {
      html += '<span class="wiki-sk sk-shimmer wiki-tree-sk-row"></span>';
    }
    html += '</div>';
  }
  return html;
}

async function _refresh() {
  const tree = _q('.wiki-tree');
  if (tree) tree.innerHTML = _treeSkeleton();
  try {
    const data = await _api('/api/v1/wiki');
    if (!_active) return;
    _allArticles = (data && data.articles) || [];
    _buildIndex(_allArticles);
    _renderSidebar();
    _renderWelcome();   // keep the landing's cards/chips fresh (shown when nothing is open)
    // Honour a ?article=<slug> deep link now that the tree + index are built.
    if (_pendingDeepLink) {
      const slug = _pendingDeepLink;
      _pendingDeepLink = null;
      _openArticle(slug);
    }
  } catch (e) {
    if (!_active) return;
    if (tree) tree.innerHTML = `<div class="wiki-loading">Couldn't load the wiki: ${_esc(e.message)}</div>`;
  }
}

async function _runSearch() {
  const search = _q('.wiki-search-input');
  const query = search ? search.value.trim() : '';
  if (!query) { _searchResults = null; _renderSidebar(); return; }
  const tree = _q('.wiki-tree');
  if (tree) tree.innerHTML = '<div class="wiki-loading">Searching&hellip;</div>';
  try {
    const data = await _api(`/api/v1/wiki/search?q=${encodeURIComponent(query)}`);
    if (!_active) return;
    _searchResults = (data && data.results) || [];
    _renderSidebar();
  } catch (e) {
    if (!_active) return;
    if (tree) tree.innerHTML = `<div class="wiki-loading">Search failed: ${_esc(e.message)}</div>`;
  }
}

// Decide what the sidebar shows: search results > active filter > full tree.
function _renderSidebar() {
  const tree = _q('.wiki-tree');
  const countEl = _q('.wiki-sidebar-count');
  const filterBar = _q('.wiki-filter-active');
  if (!tree) return;

  _renderSidebarFoot();

  // Active tag/category filter banner.
  if (_activeFilter && !_searchResults) {
    if (filterBar) {
      filterBar.hidden = false;
      const txt = filterBar.querySelector('.wiki-filter-active-text');
      if (txt) txt.innerHTML = `Filtered by ${_activeFilter.type === 'tag' ? '#' : ''}${_esc(_activeFilter.value)}`;
    }
  } else if (filterBar) {
    filterBar.hidden = true;
  }

  // Search results: a flat, ranked list.
  if (_searchResults) {
    const results = _searchResults;
    if (countEl) { countEl.hidden = false; countEl.textContent = `${results.length} result${results.length === 1 ? '' : 's'}`; }
    if (!results.length) {
      tree.innerHTML = '<div class="wiki-tree-empty">No matching articles.</div>';
      return;
    }
    tree.innerHTML = '<ul class="wiki-tree-items wiki-tree-flat">'
      + results.map(a => _itemHtml(a, true)).join('') + '</ul>';
    _markActiveInTree();
    return;
  }

  // Tag/category filter: a flat list of matches.
  if (_activeFilter) {
    const matches = _allArticles.filter(a => _matchesFilter(a, _activeFilter));
    if (countEl) { countEl.hidden = false; countEl.textContent = `${matches.length} article${matches.length === 1 ? '' : 's'}`; }
    if (!matches.length) {
      tree.innerHTML = '<div class="wiki-tree-empty">Nothing matches this filter.</div>';
      return;
    }
    tree.innerHTML = '<ul class="wiki-tree-items wiki-tree-flat">'
      + matches.map(a => _itemHtml(a)).join('') + '</ul>';
    _markActiveInTree();
    return;
  }

  // Default: the full category tree.
  if (countEl) {
    countEl.hidden = false;
    countEl.textContent = `${_allArticles.length} article${_allArticles.length === 1 ? '' : 's'}`;
  }
  if (!_allArticles.length) {
    tree.innerHTML = '<div class="wiki-tree-empty">No articles yet.<br>Use <b>New</b> above to start one.</div>';
    return;
  }
  tree.innerHTML = _treeHtml(_allArticles);
  _markActiveInTree();
}

// Group articles by category and render collapsible groups.
function _treeHtml(articles) {
  const groups = new Map();
  for (const a of articles) {
    const key = a.category && a.category.trim() ? a.category.trim() : UNCATEGORIZED;
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(a);
  }
  // Sort categories alphabetically, but keep "General" (uncategorized) last.
  const cats = [...groups.keys()].sort((x, y) => {
    if (x === UNCATEGORIZED) return 1;
    if (y === UNCATEGORIZED) return -1;
    return x.localeCompare(y);
  });
  return cats.map((cat) => {
    const items = groups.get(cat).sort((x, y) => (x.title || '').localeCompare(y.title || ''));
    const collapsed = _collapsed.has(cat);
    return `<div class="wiki-tree-group${collapsed ? ' collapsed' : ''}">
      <button class="wiki-tree-cat" data-cat="${_esc(cat)}" aria-expanded="${collapsed ? 'false' : 'true'}">
        <span class="wiki-tree-caret">&#9656;</span>
        <span class="wiki-tree-cat-name">${_esc(cat)}</span>
        <span class="wiki-tree-cat-count">${items.length}</span>
      </button>
      <ul class="wiki-tree-items">${items.map(a => _itemHtml(a)).join('')}</ul>
    </div>`;
  }).join('');
}

function _itemHtml(a, withSnippet) {
  const snip = withSnippet && a.snippet
    ? `<span class="wiki-tree-snippet">${_esc(a.snippet)}</span>` : '';
  // Drafts (internal) get a small dot so members can tell them apart at a glance.
  const isDraft = (a.status || 'draft') !== 'published';
  const draftDot = isDraft ? '<span class="wiki-tree-draft-dot" title="Draft (internal)"></span>' : '';
  const cls = 'wiki-tree-item' + (isDraft ? ' is-draft' : '');
  return `<li><a class="${cls}" data-slug="${_esc(a.slug)}" title="${_esc(a.title)}">`
    + `<span class="wiki-tree-item-title">${draftDot}${_esc(a.title)}</span>${snip}</a></li>`;
}

function _toggleCategory(cat) {
  if (!cat) return;
  if (_collapsed.has(cat)) _collapsed.delete(cat);
  else _collapsed.add(cat);
  _renderSidebar();
}

// Highlight the open article's row in the sidebar.
function _markActiveInTree() {
  const tree = _q('.wiki-tree');
  if (!tree) return;
  tree.querySelectorAll('.wiki-tree-item.active').forEach(el => el.classList.remove('active'));
  if (!_current || !_current.slug) return;
  const el = tree.querySelector(`.wiki-tree-item[data-slug="${CSS.escape(_current.slug)}"]`);
  if (el) el.classList.add('active');
}

function _matchesFilter(a, f) {
  if (!f) return true;
  if (f.type === 'category') return (a.category || '') === f.value;
  if (f.type === 'tag') return Array.isArray(a.tags) && a.tags.includes(f.value);
  return true;
}

function _applyFilter(type, value) {
  // Clicking the active chip again clears the filter.
  if (_activeFilter && _activeFilter.type === type && _activeFilter.value === value) {
    _activeFilter = null;
  } else {
    _activeFilter = { type, value };
  }
  // Clear any search so the filter is what drives the sidebar.
  const search = _q('.wiki-search-input');
  if (search && search.value) { search.value = ''; const c = _q('.wiki-search-clear'); if (c) c.hidden = true; }
  _searchResults = null;
  _renderSidebar();
}

// ── Markdown rendering + [[wiki-links]] ───────────────────────────────────────

function _mdReady() {
  return !!(window.marked && typeof window.marked.parse === 'function'
         && window.DOMPurify && typeof window.DOMPurify.sanitize === 'function');
}

// Convert an article body (Markdown + [[links]]) into safe HTML, or null when
// the libs are missing (caller falls back to plain text). [[Title]] / [[Title|label]]
// become links to a #wiki/<target> sentinel that _wireReaderLinks() resolves.
function _mdToHtml(body) {
  if (!body || !_mdReady()) return null;
  const pre = body.replace(/\[\[([^\]]+)\]\]/g, (m, inner) => {
    const bar = inner.indexOf('|');
    const target = (bar >= 0 ? inner.slice(0, bar) : inner).trim();
    const label = (bar >= 0 ? inner.slice(bar + 1) : inner).trim();
    if (!target) return m;
    return `[${label}](#wiki/${encodeURIComponent(target)})`;
  });
  let html;
  try { html = window.marked.parse(pre, { gfm: true, breaks: true }); }
  catch (_) { return null; }
  return window.DOMPurify.sanitize(html, { FORBID_ATTR: ['style'] });
}

// Turn #wiki/<target> sentinel links into resolved/missing wiki-links, and make
// every other link open safely in a new tab.
function _wireReaderLinks(rootEl) {
  rootEl.querySelectorAll('a[href]').forEach((a) => {
    const href = a.getAttribute('href') || '';
    if (href.startsWith('#wiki/')) {
      const target = decodeURIComponent(href.slice('#wiki/'.length));
      const hit = _index.get(target.toLowerCase());
      a.classList.add('wiki-link');
      a.removeAttribute('href');
      if (hit) { a.dataset.slug = hit.slug; a.title = hit.title; }
      else { a.classList.add('wiki-link-missing'); a.dataset.newtitle = target; a.title = `Create "${target}"`; }
    } else {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    }
  });
}

// ── Surface switching (welcome ↔ article) ─────────────────────────────────────

function _showWelcome() {
  _current = null;
  _editing = false;
  _viewingRevisionId = null;
  const w = _q('.wiki-welcome');
  const art = _q('.wiki-article');
  if (w) w.hidden = false;
  if (art) art.hidden = true;
  _renderWelcome();
  _markActiveInTree();
  _syncUrl(null);   // nothing open → drop ?article= from the address bar
}

// Build the welcome landing's "Start here" cards (newest articles) and
// "Browse by category" chips from the loaded article list. Both sections hide
// themselves when there's nothing to show (empty wiki / no categories).
function _renderWelcome() {
  const featSec = _q('[data-welcome="featured"]');
  const cardsEl = _q('.wiki-welcome-cards');
  if (featSec && cardsEl) {
    const top = _allArticles.slice(0, 3);   // list is newest-first
    if (top.length) {
      cardsEl.innerHTML = top.map(a => {
        const cat = a.category && a.category.trim() ? a.category.trim() : 'General';
        const excerpt = (a.snippet || '').replace(/\s+/g, ' ').trim();
        return `<button class="wiki-welcome-card" data-slug="${_esc(a.slug)}">
          <span class="wiki-welcome-card-cat">${_esc(cat)}</span>
          <span class="wiki-welcome-card-title">${_esc(a.title || 'Untitled')}</span>
          <span class="wiki-welcome-card-excerpt">${_esc(excerpt)}</span>
        </button>`;
      }).join('');
      featSec.hidden = false;
    } else {
      featSec.hidden = true;
    }
  }

  const catSec = _q('[data-welcome="categories"]');
  const chipsEl = _q('.wiki-welcome-chips');
  if (catSec && chipsEl) {
    const counts = new Map();
    for (const a of _allArticles) {
      const cat = a.category && a.category.trim() ? a.category.trim() : UNCATEGORIZED;
      counts.set(cat, (counts.get(cat) || 0) + 1);
    }
    const chips = [...counts.entries()].sort((x, y) => {
      if (x[0] === UNCATEGORIZED) return 1;
      if (y[0] === UNCATEGORIZED) return -1;
      return x[0].localeCompare(y[0]);
    });
    if (chips.length) {
      chipsEl.innerHTML = chips.map(([name, count]) =>
        `<button class="wiki-welcome-chip" data-filter-type="category" data-filter-value="${_esc(name)}">
          <span>${_esc(name)}</span><span class="wiki-welcome-chip-count">${count}</span>
        </button>`).join('');
      catSec.hidden = false;
    } else {
      catSec.hidden = true;
    }
  }
}

function _showArticleSurface() {
  const w = _q('.wiki-welcome');
  const art = _q('.wiki-article');
  if (w) w.hidden = true;
  if (art) art.hidden = false;
}

// Show the reader with phantom shimmer placeholders (title bar + body lines)
// while the article fetch is in flight, so opening an article doesn't blank
// the surface. _renderReader() overwrites these with the real content.
function _showReaderSkeleton() {
  _showArticleSurface();
  const layout = _q('.wiki-reader-layout');
  if (layout) layout.hidden = false;
  const ed = _q('.wiki-editor'); if (ed) ed.hidden = true;
  const rev = _q('.wiki-revisions'); if (rev) rev.hidden = true;
  // Hide the TOC while loading (_buildToc re-evaluates it on render). We
  // deliberately DON'T touch the badge/meta/tag/backlink containers — those are
  // shown/hidden per-child by _renderReader, so hiding the wrappers here would
  // leave them hidden after render.
  const toc = _q('.wiki-toc'); if (toc) toc.hidden = true;
  const title = _q('.wiki-reader-title');
  if (title) title.innerHTML = '<span class="wiki-sk sk-shimmer wiki-reader-sk-title"></span>';
  const body = _q('.wiki-reader-body');
  if (body) {
    const widths = ['92%', '84%', '96%', '70%', '', '88%', '90%', '60%'];
    body.innerHTML = widths.map(w =>
      w ? `<span class="wiki-sk sk-shimmer wiki-reader-sk-line" style="width:${w};"></span>`
        : '<span class="wiki-reader-sk-gap"></span>'
    ).join('');
  }
}

// ── Open / read an article ───────────────────────────────────────────────────

async function _openArticle(slug) {
  _showReaderSkeleton();   // paint placeholders immediately, before the fetch
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(slug)}`);
    _current = data.article;
    _editing = false;
    _viewingRevisionId = null;
    _showArticleSurface();
    _renderReader();
    _loadBacklinks(slug);
    _markActiveInTree();
    _syncUrl(_current && _current.slug);
  } catch (e) {
    _toast(`Couldn't open article: ${e.message}`, true);
  }
}

// Render the reader from an article-like object {title, body, tags, category, …}.
function _renderReader(source) {
  const a = source || _current || {};
  const layout = _q('.wiki-reader-layout');
  if (layout) layout.hidden = false;
  _q('.wiki-editor').hidden = true;
  _q('.wiki-revisions').hidden = true;
  _q('.wiki-reader-title').textContent = a.title || 'Untitled';

  // Breadcrumb (toolbar): category › title.
  const bcCat = _q('.wiki-breadcrumb-cat');
  const bcSep = _q('.wiki-breadcrumb-sep');
  const bcTitle = _q('.wiki-breadcrumb-title');
  if (bcTitle) bcTitle.textContent = a.title || 'Untitled';
  if (bcCat && bcSep) {
    if (a.category) {
      bcCat.hidden = false; bcSep.hidden = false;
      bcCat.textContent = a.category;
      bcCat.dataset.filterValue = a.category;
    } else { bcCat.hidden = true; bcSep.hidden = true; }
  }

  // Category badge (above the title), doubles as a category filter.
  const cat = _q('.wiki-reader-category');
  if (cat) {
    if (a.category) {
      cat.hidden = false; cat.textContent = a.category;
      cat.dataset.filterValue = a.category;
    } else { cat.hidden = true; }
  }

  // Draft/published badge — only the "Draft" state is called out (published is
  // the unremarkable default). Hidden while viewing an old revision.
  const statusEl = _q('.wiki-reader-status');
  if (statusEl) {
    const isDraft = (a.status || 'draft') !== 'published';
    if (isDraft && !_viewingRevisionId) {
      statusEl.hidden = false;
      statusEl.textContent = 'Draft · internal';
      statusEl.className = 'wiki-reader-status wiki-status-draft';
    } else {
      statusEl.hidden = true;
    }
  }

  // Byline (author avatar + name + updated date) and reading time.
  const author = _authorFrom(a.updated_by || a.edited_by || a.created_by);
  const when = a.updated_at || a.created_at || '';
  const byline = _q('.wiki-reader-byline');
  if (byline) {
    if (author) {
      byline.hidden = false;
      _q('.wiki-reader-avatar').textContent = author.initials;
      _q('.wiki-reader-author').textContent = author.name;
      _q('.wiki-reader-updated').textContent = when ? `Updated ${_relDate(when)}` : '';
    } else { byline.hidden = true; }
  }
  const reading = _q('.wiki-reader-reading');
  const sep = _q('.wiki-reader-meta-sep');
  if (reading) {
    if (a.body && a.body.trim()) {
      reading.hidden = false;
      _q('.wiki-reader-reading-text').textContent = _readingTime(a.body);
    } else { reading.hidden = true; }
  }
  if (sep) sep.hidden = !(author && reading && !reading.hidden);

  // Tags (beneath the body), each a tag filter.
  const tagsEl = _q('.wiki-reader-tags');
  if (tagsEl) {
    const tags = Array.isArray(a.tags) ? a.tags : [];
    if (tags.length) {
      tagsEl.hidden = false;
      tagsEl.innerHTML = tags.map(t =>
        `<span class="wiki-chip" data-filter-type="tag" data-filter-value="${_esc(t)}">`
        + `<i data-lucide="hash" class="lucide-icon"></i>${_esc(t)}</span>`).join('');
    } else { tagsEl.hidden = true; tagsEl.innerHTML = ''; }
  }

  const body = _q('.wiki-reader-body');
  if (body) {
    const html = _mdToHtml(a.body || '');
    if (html != null) {
      body.classList.add('md-body');
      body.innerHTML = html;
      _wireReaderLinks(body);
      try { if (window.Prism) window.Prism.highlightAllUnder(body); } catch (_) {}
    } else {
      body.classList.remove('md-body');
      body.textContent = a.body || '';
    }
  }
  _buildToc();
  _setToolbarMode('reading');
}

// ── "On this page" rail (TOC) + scroll-spy ────────────────────────────────────

let _tocHeadings = [];   // [{ id, el }] for the currently rendered article

// Collect the h2/h3 headings the markdown produced, give them stable ids, and
// render the rail. Hidden unless there are at least 2 headings to navigate.
function _buildToc() {
  const aside = _q('.wiki-toc');
  const list = _q('.wiki-toc-list');
  const body = _q('.wiki-reader-body');
  _tocHeadings = [];
  if (!aside || !list || !body) return;
  const heads = [...body.querySelectorAll('h1, h2, h3')];
  if (heads.length < 2) { aside.hidden = true; list.innerHTML = ''; return; }
  list.innerHTML = heads.map((h, i) => {
    const id = `wiki-h-${i}`;
    h.id = id;
    h.style.scrollMarginTop = '20px';
    _tocHeadings.push({ id, el: h });
    const sub = h.tagName === 'H3' ? ' sub' : '';
    return `<a class="wiki-toc-link${sub}" data-toc="${id}" title="${_esc(h.textContent || '')}">${_esc(h.textContent || '')}</a>`;
  }).join('');
  aside.hidden = false;
  _spyToc();
}

// Highlight the heading nearest the top of the scroll surface.
function _spyToc() {
  const scroll = _q('.wiki-article-scroll');
  const list = _q('.wiki-toc-list');
  if (!scroll || !list || !_tocHeadings.length) return;
  const cTop = scroll.getBoundingClientRect().top;
  let cur = _tocHeadings[0].id;
  for (const h of _tocHeadings) {
    if (h.el.getBoundingClientRect().top - cTop <= 110) cur = h.id;
  }
  list.querySelectorAll('.wiki-toc-link').forEach(a =>
    a.classList.toggle('active', a.dataset.toc === cur));
}

// Smooth-scroll the article surface to a heading picked from the rail.
function _gotoHeading(id) {
  const scroll = _q('.wiki-article-scroll');
  const h = _tocHeadings.find(x => x.id === id);
  if (!scroll || !h) return;
  const y = h.el.getBoundingClientRect().top - scroll.getBoundingClientRect().top + scroll.scrollTop - 20;
  scroll.scrollTo({ top: y, behavior: 'smooth' });
}

async function _loadBacklinks(slug) {
  const box = _q('.wiki-backlinks');
  const listEl = _q('.wiki-backlinks-list');
  if (!box || !listEl) return;
  box.hidden = true; listEl.innerHTML = '';
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(slug)}/backlinks`);
    const links = (data && data.backlinks) || [];
    if (!links.length || _viewingRevisionId) return;          // hide when empty / viewing old rev
    listEl.innerHTML = links.map(l =>
      `<a class="wiki-backlink" data-slug="${_esc(l.slug)}">${_esc(l.title)}</a>`).join('');
    box.hidden = false;
  } catch (_) { /* backlinks are best-effort */ }
}

// ── Revision history ──────────────────────────────────────────────────────────

async function _toggleHistory() {
  const panel = _q('.wiki-revisions');
  const layout = _q('.wiki-reader-layout');
  if (!panel || !_current) return;
  // History is a panel shown in place of the reader; toggling it off restores
  // the article.
  if (!panel.hidden) { panel.hidden = true; if (layout) layout.hidden = false; return; }
  if (layout) layout.hidden = true;
  const listEl = _q('.wiki-revisions-list');
  listEl.innerHTML = '<div class="wiki-loading">Loading history&hellip;</div>';
  panel.hidden = false;
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}/revisions`);
    const revs = (data && data.revisions) || [];
    if (!revs.length) {
      listEl.innerHTML = '<div class="wiki-revisions-empty">No earlier versions yet — history starts the first time this article is edited.</div>';
      return;
    }
    listEl.innerHTML = revs.map(r => {
      const when = _fmtDate(r.created_at);
      const who = r.edited_by ? ` · ${_esc(r.edited_by)}` : '';
      return `<div class="wiki-revision-row">
        <div class="wiki-revision-meta"><span class="wiki-revision-when">${when}</span><span class="wiki-revision-who">${who}</span></div>
        <div class="wiki-revision-snippet">${_esc(r.snippet || '')}</div>
        <div class="wiki-revision-actions">
          <button class="wiki-btn wiki-btn-sm" data-rev-act="view" data-rev-id="${_esc(r.id)}">View</button>
          <button class="wiki-btn wiki-btn-sm" data-rev-act="restore" data-rev-id="${_esc(r.id)}">Restore</button>
        </div>
      </div>`;
    }).join('');
  } catch (e) {
    listEl.innerHTML = `<div class="wiki-loading">Couldn't load history: ${_esc(e.message)}</div>`;
  }
}

async function _viewRevision(revId) {
  try {
    const data = await _api(`/api/v1/wiki/revisions/${encodeURIComponent(revId)}`);
    const rev = data && data.revision;
    if (!rev) return;
    _viewingRevisionId = revId;
    _renderReader(rev);                       // render the OLD content read-only
    const banner = _q('.wiki-revision-banner');
    const txt = _q('.wiki-revision-banner-text');
    if (txt) txt.textContent = `Viewing an old version from ${_fmtDate(rev.created_at)} — this is read-only.`;
    if (banner) banner.hidden = false;
    _q('.wiki-backlinks').hidden = true;
    _setToolbarMode('revision');
  } catch (e) {
    _toast(`Couldn't load revision: ${e.message}`, true);
  }
}

function _exitRevisionView() {
  _viewingRevisionId = null;
  const banner = _q('.wiki-revision-banner');
  if (banner) banner.hidden = true;
  if (_current) { _renderReader(); _loadBacklinks(_current.slug); }
}

async function _restoreRevision(revId) {
  if (!_current) return;
  if (!window.confirm('Restore this version? The current version is saved to history first, so you can undo this.')) return;
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}/restore/${encodeURIComponent(revId)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId || '' }),
    });
    _current = data.article;
    _viewingRevisionId = null;
    const banner = _q('.wiki-revision-banner');
    if (banner) banner.hidden = true;
    _renderReader();
    _loadBacklinks(_current.slug);
    _toast('Restored.');
  } catch (e) {
    _toast(`Couldn't restore: ${e.message}`, true);
  }
}

function _fmtDate(iso) {
  if (!iso) return '';
  try {
    const d = new Date(iso);
    if (isNaN(d.getTime())) return _esc(iso);
    return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
  } catch (_) { return _esc(iso); }
}

// ── Edit / new ───────────────────────────────────────────────────────────────

function _openNew(prefillTitle) {
  // Creating articles is admin-only (mirrors the API's 403 for non-admins).
  if (!_canCreate()) { _toast('Only admins can create articles.', true); return; }
  _current = null;            // null slug = create on save
  _editing = true;
  _viewingRevisionId = null;
  const banner = _q('.wiki-revision-banner');
  if (banner) banner.hidden = true;
  _showArticleSurface();
  _fillEditor({ title: typeof prefillTitle === 'string' ? prefillTitle : '', body: '', tags: [], category: '' });
  _q('.wiki-reader-layout').hidden = true;
  _q('.wiki-revisions').hidden = true;
  _q('.wiki-editor').hidden = false;
  // Breadcrumb shows the editor state while there's no live article yet.
  const bcCat = _q('.wiki-breadcrumb-cat'), bcSep = _q('.wiki-breadcrumb-sep'), bcTitle = _q('.wiki-breadcrumb-title');
  if (bcCat) bcCat.hidden = true;
  if (bcSep) bcSep.hidden = true;
  if (bcTitle) bcTitle.textContent = 'New article';
  _setToolbarMode('editing');
  _markActiveInTree();
  const t = _q('.wiki-editor-title');
  if (t) t.focus();
}

function _enterEdit() {
  if (!_current) return;
  _editing = true;
  _fillEditor(_current);
  _q('.wiki-reader-layout').hidden = true;
  _q('.wiki-revisions').hidden = true;
  _q('.wiki-editor').hidden = false;
  _setToolbarMode('editing');
}

function _cancelEdit() {
  _editing = false;
  if (_current) {
    _renderReader();
    _loadBacklinks(_current.slug);
  } else {
    _showWelcome();
  }
}

function _fillEditor(a) {
  const tags = Array.isArray(a.tags) ? a.tags : [];
  _q('.wiki-editor-title').value = a.title || '';
  _q('.wiki-editor-category').value = a.category || '';
  _q('.wiki-editor-tags').value = tags.join(', ');
  _q('.wiki-editor-body').value = a.body || '';
  const statusSel = _q('.wiki-editor-status');
  if (statusSel) statusSel.value = (a.status === 'published') ? 'published' : 'draft';
}

async function _save() {
  const title = _q('.wiki-editor-title').value.trim();
  if (!title) { _toast('Give the article a title first.', true); return; }
  const body = _q('.wiki-editor-body').value;
  const category = _q('.wiki-editor-category').value.trim();
  const tags = _q('.wiki-editor-tags').value
    .split(',').map(s => s.trim()).filter(Boolean);
  const statusSel = _q('.wiki-editor-status');
  const status = statusSel && statusSel.value === 'published' ? 'published' : 'draft';
  const userId = app.currentUserId || '';

  const saveBtn = _q('[data-act="save"]');
  if (saveBtn) saveBtn.disabled = true;
  try {
    let data;
    if (_current && _current.slug) {
      data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tags, category, status, user_id: userId }),
      });
    } else {
      data = await _api('/api/v1/wiki', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title, body, tags, category, status, user_id: userId }),
      });
    }
    _current = data.article;
    _editing = false;
    _renderReader();
    _loadBacklinks(_current.slug);
    _toast('Saved.');
    await _refresh();          // article set changed → rebuild tree + link index
    _markActiveInTree();
  } catch (e) {
    _toast(`Couldn't save: ${e.message}`, true);
  } finally {
    if (saveBtn) saveBtn.disabled = false;
  }
}

async function _delete() {
  if (!_current || !_current.slug) { _showWelcome(); return; }
  if (!window.confirm(`Delete "${_current.title}"? This removes it for everyone and can't be undone.`)) return;
  try {
    await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}`, { method: 'DELETE' });
    _toast('Deleted.');
    _showWelcome();
    await _refresh();
  } catch (e) {
    _toast(`Couldn't delete: ${e.message}`, true);
  }
}

// Toggle which toolbar buttons + panels show per mode: reading | editing | revision.
// Editing controls are members-only (anonymous visitors get a read-only view).
function _setToolbarMode(mode) {
  const show = (sel, on) => { const el = _q(sel); if (el) el.hidden = !on; };
  const member = _isMember();
  const reading = mode === 'reading';
  const editing = mode === 'editing';
  const revision = mode === 'revision';
  show('[data-act="history"]', reading && member);
  show('[data-act="edit"]', reading && member);
  show('[data-act="delete"]', reading && member);
  show('[data-act="save"]', editing && member);
  show('[data-act="cancel"]', editing);

  // Publish / unpublish button: only while reading the live article, member only.
  const pub = _q('.wiki-publish-btn');
  if (pub) {
    const showPub = reading && member && _current && _current.slug;
    pub.hidden = !showPub;
    if (showPub) {
      const isDraft = (_current.status || 'draft') !== 'published';
      const label = pub.querySelector('.wiki-publish-label');
      if (label) label.textContent = isDraft ? 'Publish' : 'Unpublish';
      pub.title = isDraft ? 'Make this article public' : 'Make this article internal (draft)';
      pub.classList.toggle('wiki-btn-primary', isDraft);
    }
  }

  // Copy-public-link button: only while reading a PUBLISHED live article (a draft
  // has no public /wiki/<slug> page). Available to everyone — a public link is
  // public — not just members.
  const copy = _q('.wiki-copylink-btn');
  if (copy) {
    const isPublished = _current && _current.slug && (_current.status || 'draft') === 'published';
    copy.hidden = !(reading && isPublished);
  }
}

// Copy the open article's PUBLIC, crawlable page URL (/wiki/<slug>) — the
// server-rendered page (app/wiki/public_pages.py), not the in-app SPA deep link —
// to the clipboard, so people share the search-engine-friendly address.
async function _copyPublicLink() {
  if (!_current || !_current.slug) return;
  const url = `${location.origin}/wiki/${encodeURIComponent(_current.slug)}`;
  try {
    // copyText handles non-secure http://<ip> contexts (e.g. a phone on the LAN),
    // where the async Clipboard API is absent, via an execCommand fallback.
    await copyText(url);
    _toast('Public link copied.');
  } catch (_) {
    _toast(url, false);   // last resort: show it so the user can copy manually
  }
}

// Publish (draft → published) or unpublish (published → draft) the open article.
async function _toggleStatus() {
  if (!_current || !_current.slug) return;
  const isDraft = (_current.status || 'draft') !== 'published';
  const action = isDraft ? 'publish' : 'unpublish';
  if (isDraft && !window.confirm(`Publish "${_current.title}"? It will become visible to everyone, including anonymous visitors.`)) return;
  try {
    const data = await _api(`/api/v1/wiki/${encodeURIComponent(_current.slug)}/${action}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: app.currentUserId || '' }),
    });
    _current = data.article;
    _renderReader();
    _toast(isDraft ? 'Published — now public.' : 'Unpublished — now internal.');
    await _refresh();
    _markActiveInTree();
  } catch (e) {
    _toast(`Couldn't change visibility: ${e.message}`, true);
  }
}

'use strict';

// Session dropdown list — session data + rendering (cache, validated fetch with
// bounded retry, row rendering, manage/search/bin footer, trigger label, related
// chips, group tree). Centralized home: ui/chat/elements/session-dropdown/.
// ui/chat/js/session-list.js is a compatibility re-export shim for this module,
// so existing importers (session-core.js, chat-send.js, chat-stream.js, ...)
// keep working unchanged. Module map: ui/chat/js/README.md.

import { app } from '../../../shared/js/state.js';
import { apiPath } from '../../../shared/js/config.js';
import { authHeaders, isAuthenticated, isAnonGuest } from '../../../shared/js/left-login.js';
import { icon, componentTypeIcon, claudeMark, codexMark } from '../../../shared/js/icons.js';
import { storageAdapter } from '../../js/storage/storage-adapter.js';
import { ICON_PICKER_ICONS } from '../../../shared/js/icon-picker.js';
import { _esc, _escAttr } from '../../../shared/js/dom-utils.js';
import { _agentIconFor, _agentIconHtml, _agentNameFor } from '../../js/session-agent.js';
import { hydrateAgentDisplay } from '../../../shared/js/agent-display-cache.js';
// session-core imports from this module too — an ES-module cycle. Safe because
// switchToSession and deleteSession are only CALLED at runtime (in the tab click
// handlers / chip close handlers), by which point the live binding is resolved.
import { switchToSession, deleteSession } from '../../js/session-core.js?v=515';
import { applyRubberBand } from '../../../shared/js/rubber-band.js';
import { kvCache } from '../../js/storage/kv-cache.js';
import { compareSessionsByPinnedOrder } from '../../../shared/js/session-ordering.js';
import { sessionAttentionFlag, sessionFlagIsRead, flagLabel, flagTone, closingCta } from '../../../shared/js/session-flag.js';

let _sessionsCache = [];

// React-island boundary. Existing vanilla consumers retain the mutable cache;
// React reads immutable snapshots and is notified only after a visible render.
const _sessionDropdownListeners = new Set();
let _sessionDropdownRevision = 0;
let _sessionDropdownPresentation = Object.freeze([]);
let _sessionDropdownLoadState = 'idle';
let _reactSessionDropdownActive = false;
let _sessionDropdownSearchFocus = null;
let _sessionDropdownSnapshot = Object.freeze({ revision: 0, sessions: [], presentation: _sessionDropdownPresentation });

// The React island retains the legacy action authority but owns the menu DOM.
function _captureSessionDropdownPresentation(renderedMenu, searchFocus = null) {
  _sessionDropdownSearchFocus = searchFocus;
  _sessionDropdownPresentation = Object.freeze(Array.from(renderedMenu.children).map((node, index) => Object.freeze({
    key: node.dataset.id || node.id || (node.className || 'item') + ':' + index,
    className: node.className, id: node.id || '', role: node.getAttribute('role') || '',
    dataset: Object.freeze({ ...node.dataset }), html: node.innerHTML,
  })));
}

function _publishSessionDropdownSnapshot() {
  _sessionDropdownRevision += 1;
  _sessionDropdownSnapshot = Object.freeze({
    revision: _sessionDropdownRevision,
    sessions: Object.freeze(_sessionsCache.map(row => Object.freeze({ ...row }))),
    selectedSessionId: app.currentSessionId || null,
    searchQuery: _searchQuery,
    showHidden: _showHidden,
    binMode: _binMode,
    checkboxMode: _checkboxMode,
    selectedSessionIds: Object.freeze([..._selectedSessions]),
    config: _sessionDropdownConfig,
    loadState: _sessionDropdownLoadState,
    presentation: _sessionDropdownPresentation,
    searchFocus: _sessionDropdownSearchFocus,
  });
  for (const listener of _sessionDropdownListeners) {
    try { listener(); } catch (_) { /* external-store subscribers are isolated */ }
  }
}

const sessionDropdownStore = {
  subscribe(listener) {
    _sessionDropdownListeners.add(listener);
    return () => _sessionDropdownListeners.delete(listener);
  },
  getSnapshot: () => _sessionDropdownSnapshot,
  refresh: (userId = app.currentUserId) => populateSessionSelect(userId),
  render: () => _renderSessionRows(),
  activateReact() { _reactSessionDropdownActive = true; _renderSessionRows(); },
  deactivateReact() { _reactSessionDropdownActive = false; },
  setLoadState(state) { if (state === 'idle' && _sessionDropdownLoadState === 'error') return; _sessionDropdownLoadState = state || 'idle'; _publishSessionDropdownSnapshot(); },
};
// Client-confirmed read actions can arrive ahead of the local session mirror.
// Keep them separately from list snapshots so an asynchronous refresh cannot
// discard them before its own data catches up.
const _sessionReadStateOverrides = new Map();
let _portalSessionsCache = { userId: '', sessions: [], loadedAt: 0 };
let _portalSessionsRequest = null;
const _PORTAL_SESSION_CACHE_TTL_MS = 15_000;
let _sessionFetchSeq = 0;
let _metadataRefreshFlight = null;
let _metadataRefreshUserId = '';
let _sessionListAccessDenied = false;

const _SESSION_DROPDOWN_DEFAULTS = {
  // Trigger (the chat header title bar): its standalone trash is OFF by default.
  // Deleting the current session lives in the bar's ⋯ panel (menu.delete), behind
  // the shared two-click confirm, so the bar carries one trailing control instead
  // of two. Set delete: true to restore the standalone trash button.
  trigger: { agent_icon: true, status: true, row_actions: true, delete: false, rename_on_hold: true },
  menu: {
    agent_icons: true, status: true, grouping: true, pinning: true,
    rename: true, auto_rename: true, row_actions: true, delete: true,
    open_in_widget: true,
    // "Start a scratchpad" — the ⋯ menu line that creates/opens this session's
    // Scratchpad document (a user-owned set of notes, each row shared with the
    // agent or not). Enable/disable the affordance independently of the others.
    start_scratchpad: true,
    // "Always-on display" — the ⋯ menu's keep-screen-awake switch. A PER-DEVICE
    // override of the app-wide App Functions setting (the server default every
    // visitor inherits at boot): it drives the owner-scoped Screen Wake Lock
    // under the shared 'app-setting' owner, so it can never fight Gen UI's own
    // owner. Off = the device sleeps normally. Mirrored in
    // data/config/chat_ui.json (controls.session_dropdown.menu).
    always_on_display: true,
    // Per-row trash button — OFF by default. Deleting lives in the row's ⋯
    // "more" panel (menu.delete), behind the shared two-click confirm, so there
    // is one delete affordance per row instead of two. Set true to restore it.
    row_delete: false,
    agent_id: true, session_id: true, engine_thread_id: true,
    search: true, content_search: true, recycle_bin: true,
    hidden_sessions: true, batch_delete: true,
  },
};
let _sessionDropdownConfig = _mergeDropdownConfig({}, _SESSION_DROPDOWN_DEFAULTS);

function _mergeDropdownConfig(base, override) {
  const out = { ...(base || {}) };
  for (const [key, value] of Object.entries(override || {})) {
    out[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? _mergeDropdownConfig(base?.[key], value)
      : value;
  }
  return out;
}

export function configureSessionDropdown(cfg = {}) {
  _sessionDropdownConfig = _mergeDropdownConfig(_SESSION_DROPDOWN_DEFAULTS, cfg);
  const menu = _sessionDropdownConfig.menu;
  if (!menu.search) { _searchQuery = ''; _flagFilter = false; }
  if (!menu.recycle_bin) _binMode = false;
  if (!menu.hidden_sessions) _showHidden = false;
  if (!menu.batch_delete) { _checkboxMode = false; _selectedSessions.clear(); }
  _renderSessionRows();
  return _sessionDropdownConfig;
}

export function getSessionDropdownConfig() { return _sessionDropdownConfig; }
// The Sessions page fetches this same active-session metadata alongside its
// table-only usage stats. Share the common subset so opening the picker after
// the page does not start another list request.
const _ACTIVE_SESSION_CACHE_TTL_MS = 30000;
const _PERSISTED_SESSION_CACHE_TTL_MS = 24 * 60 * 60 * 1000;
let _activeSessionCache = { userId: '', sessions: [], loadedAt: 0 };

function _persistedSessionCacheKey(userId) {
  return `session-list:v2:${String(userId || '')}`;
}

function _readPersistedSessionCache(userId) {
  const cached = kvCache.get(_persistedSessionCacheKey(userId));
  return cached && Array.isArray(cached.sessions) ? cached : null;
}

function _persistSessionMetadataCache(userId, sessions) {
  if (!userId || !Array.isArray(sessions)) return;
  const snapshot = {
    sessions: sessions.map(_normaliseSessionMetadata).filter(Boolean),
    loadedAt: Date.now(),
  };
  kvCache.set(_persistedSessionCacheKey(userId), snapshot, {
    ttlMs: _PERSISTED_SESSION_CACHE_TTL_MS,
  });
}

// A child helper session (orchestration spawn) is NESTED under its parent via
// the group tree (/related), never a top-level row. The authoritative /sessions
// list already excludes them (`id NOT LIKE 'spawn-%'`), but the dropdown's
// cache is primed by the Sessions page's /session-stats, which does NOT filter
// them — so without this guard a cache-first open would briefly paint every
// subagent as its own row before the server list lands.
function _isChildSpawnSession(session) {
  const id = session && (session.id || session.session_id);
  return typeof id === 'string' && id.startsWith('spawn-');
}

function _normaliseSessionMetadata(session) {
  const id = session && (session.id || session.session_id);
  if (!id) return null;
  // Subagent helper sessions are children of an orchestrator parent; they must
  // never surface as standalone rows in the picker (they nest on expand).
  if (id.startsWith('spawn-')) return null;
  return {
    id,
    title: session.title || 'New Session',
    agent_id: session.agent_id || null,
    agent_name: session.agent_name || '',
    agent_icon: session.agent_icon || '',
    agent_engine: session.agent_engine || '',
    created_at: session.created_at || null,
    updated_at: session.updated_at || session.last_active || session.created_at || null,
    activity_at: session.activity_at || session.last_active || null,
    activity_count: Number(session.activity_count ?? session.turn_count ?? session.interaction_count ?? 0),
    pinned: !!session.pinned,
    sort_order: Number.isFinite(session.sort_order) ? session.sort_order : null,
    hidden: !!session.hidden,
    recycled: !!session.recycled,
    run_status: session.run_status || null,
    run_updated_at: session.run_updated_at || null,
    execution_mode: session.execution_mode || null,
    // The closer's own verdict + note for this session's latest run
    // (app/api/db_viewer.py, _fetch_closing_flags). Preferred over run_status by
    // ui/shared/js/session-flag.js; absent when the gate is off.
    closing_flag: session.closing_flag || null,
    closing_cta: session.closing_cta || null,
    queue_position: typeof session.queue_position === 'number' ? session.queue_position : null,
    queue_total: typeof session.queue_total === 'number' ? session.queue_total : null,
    has_unread: !!session.has_unread,
    read_at: session.read_at || null,
    unread_at: session.unread_at || null,
    child_count: Number(session.child_count) || 0,
  };
}

function _canUseActiveSessionCache(userId) {
  return _activeSessionCache.userId === userId
    && _activeSessionCache.sessions.length > 0
    && Date.now() - _activeSessionCache.loadedAt < _ACTIVE_SESSION_CACHE_TTL_MS;
}

// Called by the Sessions page after its active /session-stats response.
// Hidden, searched, and recycled scopes still use their own server queries.
function primeSessionMetadataCache(userId, sessions) {
  if (!userId || !Array.isArray(sessions)) return;
  const normalized = sessions.map(_normaliseSessionMetadata).filter(Boolean);
  _activeSessionCache = {
    userId,
    sessions: normalized,
    loadedAt: Date.now(),
  };
  _persistSessionMetadataCache(userId, normalized);
}

function hasFreshSessionMetadataCache(userId) {
  return _canUseActiveSessionCache(userId);
}
// Session-list tree: which parent rows (orchestrator) are expanded, and a
// cache of their fetched children so re-renders don't refetch. Children are
// loaded lazily from the same /related endpoint that feeds the sub-header tab
// bar, so the tree and the tabs always agree. Optimizer Planner/Closer
// sessions are top-level rows of their own — they are not nested here.
const _expandedGroups = new Set();
const _childrenCache = new Map();   // parent sid -> [{session_id,label,role,name,status}]
// Footer eye-toggle state: when true the dropdown reveals hidden sessions and
// each row shows an eye toggle instead of a trash button (manage mode).
let _showHidden = false;
function _setShowHidden(v) { _showHidden = !!v; }
function _getShowHidden() { return _showHidden; }
// Checkbox delete mode: when active, each row shows a checkbox and a "delete selected"
// button appears in the manage row. Independent of show-hidden manage mode.
let _checkboxMode = false;
const _selectedSessions = new Set();
function _setCheckboxMode(v) { _checkboxMode = !!v; }
function _getCheckboxMode() { return _checkboxMode; }
function _clearSelected() { _selectedSessions.clear(); }

// ── Dropdown search + recycle-bin (footer bar) ─────────────────────────────
// The footer search bar filters by session TITLE and, when the content_search
// capability is on, by message CONTENT too — both at once, with no mode chip to
// choose between them. Content matching mirrors the Sessions page scan
// (/session-messages?light=1, chunked + cached) and only kicks in for queries
// at least _MSG_MIN_QUERY_LEN characters long. State lives here so the 15s poll
// and every re-render preserve the active search/bin scope.
let _searchQuery = '';
let _binMode = false;
// FLAG chip beside the footer search field: when on, only sessions that carry a
// status flag survive the list — idle sessions drop out. It reuses the very
// predicate the row chips render from (`sessionAttentionFlag`), so the filter
// and the chips can never disagree about what "has a flag" means.
let _flagFilter = false;
const _msgIndex = new Map();     // sid -> [{role, content}] (user/assistant only)
const _msgHitCounts = new Map(); // sid -> matching-message count for current query
let _msgScanBusy = null;         // in-flight content-scan query token
const _MSG_MIN_QUERY_LEN = 2;    // below this, content mode falls back to title
const _MSG_SCAN_MAX = 300;       // most-recently-active sessions scanned
const _MSG_FETCH_LIMIT = 300;    // newest messages fetched per session

// Debounce timer for the search input. Centralised here so that ANY query
// state change (typing, clearing via ×/Escape, closing the menu) cancels a
// pending commit — otherwise a stale timer could resurrect an old query AFTER
// the user cleared the box, silently filtering the list while the input looks
// empty (the "dropdown sometimes doesn't show the sessions" bug).
let _searchCommitTimer = 0;

function _cancelPendingSearchCommit() {
  if (_searchCommitTimer) { clearTimeout(_searchCommitTimer); _searchCommitTimer = 0; }
}

function _setSearchQuery(v) {
  // A new value (including clearing to '') supersedes any pending debounce
  // commit — the timer must never overwrite a newer, deliberate state.
  _cancelPendingSearchCommit();
  _searchQuery = (v == null ? '' : String(v));
}
function _getSearchQuery() { return _searchQuery; }
function _setBinMode(v) { _binMode = !!v; }
function _getBinMode() { return _binMode; }
function _setFlagFilter(v) { _flagFilter = !!v; }
function _getFlagFilter() { return _flagFilter; }
function _clearMsgSearch() { _msgHitCounts.clear(); }

// Debounced commit of the search input's live value: waits 200ms after the
// last keystroke, then applies the query and refetches (plus a message-content
// scan when that capability is on). A newer input call replaces the pending
// timer.
function _scheduleSearchCommit(q) {
  _cancelPendingSearchCommit();
  _searchCommitTimer = setTimeout(() => {
    _searchCommitTimer = 0;
    _setSearchQuery(q);
    _clearMsgSearch();
    if (app.currentUserId) {
      populateSessionSelect(app.currentUserId);
      const tq = q.trim();
      if (_sessionDropdownConfig.menu.content_search && tq.length >= 2) _scanMessagesForQuery(tq);
    }
  }, 200);
}

// Fetch ONE session's newest messages (light mode) and cache ONLY the
// user/assistant text rows — tool calls (role 'tool') and system messages are
// excluded here, at the source, so they can never match a query. Mirrors
// _fetchSessionMessages on the Sessions page.
async function _fetchSessionMessages(sessionId) {
  if (_msgIndex.has(sessionId)) return _msgIndex.get(sessionId);
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${_MSG_FETCH_LIMIT}&light=1`);
  if (token) url += `&token=${encodeURIComponent(token)}`;
  let store = [];
  try {
    let messages = null;
    if (storageAdapter.isBrowser || storageAdapter.isHybrid) {
      const data = await storageAdapter.getInteractions(sessionId, _MSG_FETCH_LIMIT);
      messages = data && data.messages;
    } else {
      const res = await fetch(url);
      if (res.ok) messages = (await res.json()).messages;
    }
    store = (messages || [])
      .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content && String(m.content).trim())
      .map(m => ({ role: m.role, content: String(m.content) }));
  } catch (e) {
    console.warn('Session message fetch failed for', sessionId, e);
  }
  _msgIndex.set(sessionId, store);   // cache even on failure (empty) → no retry loop
  return store;
}

// How many user/assistant messages in this session contain `q`. Null when
// there are no matches (or the session isn't indexed yet).
function _msgMatchInfo(sid, q) {
  const idx = _msgIndex.get(sid);
  if (!idx || !q) return null;
  const ql = q.toLowerCase();
  let count = 0;
  for (const m of idx) {
    if (m.content.toLowerCase().includes(ql)) count++;
  }
  return count > 0 ? { count } : null;
}

// Scan cached sessions' messages for `q` in concurrent batches, re-rendering
// after each batch so matches appear progressively. Results are cached per
// session, so re-typing the same query is instant; a newer query supersedes
// the in-flight one. Ported from the Sessions page _scanMessagesForQuery.
async function _scanMessagesForQuery(q) {
  if (!q || q.length < _MSG_MIN_QUERY_LEN) return;
  if (!_sessionDropdownConfig.menu.content_search) return;
  if (_msgScanBusy === q) return;      // already scanning this query
  _msgScanBusy = q;                    // newer query supersedes an in-flight one
  try {
    const seen = new Set();
    const sessionRows = [];
    for (const s of _sessionsCache) {
      if (!s.id || seen.has(s.id)) continue;
      seen.add(s.id);
      sessionRows.push(s);
    }
    sessionRows.sort((a, b) => {
      const ta = Date.parse(a.activity_at || a.updated_at || a.created_at || '') || 0;
      const tb = Date.parse(b.activity_at || b.updated_at || b.created_at || '') || 0;
      return tb - ta;
    });
    const todo = sessionRows.slice(0, _MSG_SCAN_MAX).filter(s => !_msgIndex.has(s.id));
    if (todo.length === 0) { _renderSessionRows(); return; } // everything indexed
    const CHUNK = 8;
    for (let i = 0; i < todo.length; i += CHUNK) {
      // Stop early if the query changed or was cleared while fetching.
      if (_msgScanBusy !== q || _getSearchQuery().trim() !== q) return;
      await Promise.all(todo.slice(i, i + CHUNK).map(s => _fetchSessionMessages(s.id)));
      if (_msgScanBusy === q && _getSearchQuery().trim() === q) _renderSessionRows();
    }
  } catch (e) {
    console.warn('Session message search failed:', e);
  } finally {
    if (_msgScanBusy === q) {
      _msgScanBusy = null;
      if (_getSearchQuery().trim() === q) _renderSessionRows();
    }
  }
}

function _sortSessionsByPinnedOrder(sessions) {
  return sessions.sort(compareSessionsByPinnedOrder);
}

function _ensureCurrentSessionStub() {
  if (!app.currentSessionId || _sessionsCache.some(s => s.id === app.currentSessionId)) return;
  const now = new Date().toISOString();
  _sessionsCache.push({
    id: app.currentSessionId,
    title: 'New Session',
    agent_id: app.currentAgentId || null,
    agent_name: '', agent_icon: '', agent_engine: '',
    // A fresh client-side UUID has no server row yet. Keep its ordering timestamp,
    // but leave created_at null as the durable-row marker for session resources.
    created_at: null, updated_at: now, activity_at: now,
    pinned: false, sort_order: null, hidden: false, recycled: false,
    run_status: null, has_unread: false,
  });
}

/**
 * Optimistically record durable local session activity. Server refreshes later
 * reconcile this timestamp with the authoritative latest interaction, but the
 * picker must not keep a stale IndexedDB snapshot ahead of that refresh.
 */
export function markSessionRecentlyActive(sessionId, { agentId, persist = false } = {}) {
  if (!sessionId) return;
  const now = new Date().toISOString();
  let row = _sessionsCache.find(s => s.id === sessionId);
  if (!row) {
    row = _normaliseSessionMetadata({
      id: sessionId,
      title: 'New Session',
      agent_id: agentId || app.currentAgentId || null,
      created_at: now,
      updated_at: now,
      activity_at: now,
    });
    if (!row) return;
    _sessionsCache.push(row);
  }
  row.created_at = row.created_at || now;
  row.updated_at = now;
  row.activity_at = now;
  if (agentId) row.agent_id = agentId;
  _sortSessionsByPinnedOrder(_sessionsCache);

  // A successfully accepted message has a durable server row, so carry the
  // optimistic timestamp into the metadata snapshot used on the next open.
  if (persist) {
    // Do not turn an absent metadata snapshot into a one-row snapshot: that
    // would momentarily hide every older session on the next cache-first open.
    if (_activeSessionCache.userId === app.currentUserId && _activeSessionCache.sessions.length) {
      const normalized = _normaliseSessionMetadata(row);
      const rows = _activeSessionCache.sessions.slice();
      const index = rows.findIndex(s => s.id === sessionId);
      if (index >= 0) rows[index] = normalized;
      else rows.push(normalized);
      _activeSessionCache = { userId: app.currentUserId, sessions: rows, loadedAt: Date.now() };
      _persistSessionMetadataCache(app.currentUserId, rows);
    }
    // The IndexedDB row may exist even without an in-memory snapshot.
    storageAdapter.updateSessionCache(sessionId, { activity_at: now }).catch(() => {});
  }

  _renderSessionRows();
  _setTriggerLabel();
}

function _truncate(s, n) {
  return (s && s.length > n) ? s.slice(0, n) + '\u2026' : (s || '');
}

function _parseDate(dateStr) {
  if (!dateStr) return new Date(NaN);
  // ISO 8601 with offset (+00:00, -05:00, or Z) — parse as-is
  if (/[+-]\d{2}:\d{2}$/.test(dateStr) || /Z$/i.test(dateStr)) return new Date(dateStr);
  // No timezone info — treat as UTC
  return new Date(dateStr + 'Z');
}

function _isSessionRead(session) {
  if (!session?.read_at || session.unread_at) return false;
  const latestActivity = session.activity_at || session.updated_at || session.created_at;
  if (!latestActivity) return true;
  const readMs = _parseDate(session.read_at).getTime();
  const activityMs = _parseDate(latestActivity).getTime();
  // Preserve the stored read state if a legacy timestamp cannot be parsed.
  return !Number.isFinite(readMs) || !Number.isFinite(activityMs) || readMs >= activityMs;
}

// A menu-open list read can arrive from the local mirror just after a visit or
// "Mark unread" write succeeded. Overlay the client-confirmed state while the
// mirror catches up; otherwise reopening the dropdown replaces the cache row
// and makes the read-only More actions disappear until a page refresh.
function _reconcileSessionReadState(sessions) {
  return sessions.map((session) => {
    const override = _sessionReadStateOverrides.get(session.id);
    return override ? { ...session, ...override } : session;
  });
}

function _formatRelativeTime(dateStr) {
  if (!dateStr) return '';
  const now = Date.now();
  const d = _parseDate(dateStr);
  const diffMs = now - d.getTime();
  if (diffMs < 0) return 'just now';
  const sec = Math.floor(diffMs / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m`;
  const hrs = Math.floor(min / 60);
  if (hrs < 24) return `${hrs}h`;
  const days = Math.floor(hrs / 24);
  if (days < 7) return `${days}d`;
  const weeks = Math.floor(days / 7);
  if (weeks < 4) return `${weeks}w`;
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ── Session tabs (spawns, browsers, pages, and agent components) ───────────

const _SESSION_TAB_RECENCY_KEY = 'webagent.session-tabs.recency.v1';

function _getSessionTabRecency() {
  try {
    const value = JSON.parse(localStorage.getItem(_SESSION_TAB_RECENCY_KEY) || '{}');
    return value && typeof value === 'object' ? value : {};
  } catch (_e) { return {}; }
}

function _tabAttrs(key, updatedAt) {
  return ` data-session-tab-key="${_escAttr(key)}" data-updated-at="${_escAttr(updatedAt || '')}"`;
}

function _sessionTabScore(chip, recency) {
  const viewed = Number(recency[chip.dataset.sessionTabKey] || 0);
  const rawUpdated = chip.dataset.updatedAt || '';
  // SQLite stores UTC timestamps without a zone suffix. Date.parse treats
  // those as local time, which can put a backend update hours in the future and
  // prevent a real user click from becoming most-recent. Normalize naive DB
  // timestamps to UTC; ISO values that already include a zone are unchanged.
  const normalizedUpdated = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(rawUpdated)
    ? rawUpdated.replace(' ', 'T') + 'Z'
    : rawUpdated;
  const updated = Date.parse(normalizedUpdated) || 0;
  return Math.max(viewed, updated);
}

function _sortSessionTabs(wrap) {
  if (!wrap) return;
  const recency = _getSessionTabRecency();
  const chips = [...wrap.querySelectorAll(':scope > .sub-chip')];
  chips
    .map((chip, index) => ({ chip, index, score: _sessionTabScore(chip, recency) }))
    .sort((a, b) => (b.score - a.score) || (a.index - b.index))
    .forEach(({ chip }) => wrap.appendChild(chip));
}

function _touchSessionTab(chip) {
  const key = chip && chip.dataset.sessionTabKey;
  if (!key) return;
  const recency = _getSessionTabRecency();
  recency[key] = Date.now();
  // Bound browser storage while retaining the most useful recent reads.
  const bounded = Object.fromEntries(
    Object.entries(recency)
      .sort((a, b) => Number(b[1] || 0) - Number(a[1] || 0))
      .slice(0, 300)
  );
  try { localStorage.setItem(_SESSION_TAB_RECENCY_KEY, JSON.stringify(bounded)); } catch (_e) {}
  const wrap = chip.parentElement;
  _sortSessionTabs(wrap);
  requestAnimationFrame(() => wrap?.scrollTo({ left: 0, behavior: 'smooth' }));
}

// Collapse concurrent same-session refreshes into a single fetch. On load the
// init call, the first list render, and the 15s poll can all fire
// _fetchRelatedSessions() for the same session at once — firing 3 identical
// /related round-trips (each opens a fresh Postgres connection + a heavy query
// server-side, observed back-to-back in the diagnostics feed). While one is in
// flight for the CURRENT session, later callers reuse the same promise. Keyed by
// session id so a session switch still triggers a fresh fetch.
let _relatedInFlight = null; // { sid, promise }
let _relatedAccessDenied = false;
async function _fetchRelatedSessions() {
  if (_relatedAccessDenied) return;
  const _sid = app.currentSessionId;
  if (_relatedInFlight && _relatedInFlight.sid === _sid) return _relatedInFlight.promise;
  const promise = _fetchRelatedSessionsInner();
  _relatedInFlight = { sid: _sid, promise };
  promise.finally(() => {
    if (_relatedInFlight && _relatedInFlight.promise === promise) _relatedInFlight = null;
  });
  return promise;
}

async function _fetchRelatedSessionsInner() {
  const subHeader = document.getElementById('chat-sub-header');
  const wrap = document.getElementById('chat-sub-scroll-wrap');
  if (!subHeader || !wrap) return;

  const sid = app.currentSessionId;
  if (_relatedAccessDenied || !sid || !app.currentUserId) {
    subHeader.style.display = 'none';
    return;
  }

  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/related?db=user.db`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const resp = await fetch(apiPath(url));
    if (resp.status === 401 || resp.status === 403) {
      _relatedAccessDenied = true;
      subHeader.style.display = 'none';
      return;
    }
    if (!resp.ok) { subHeader.style.display = 'none'; return; }
    const data = await resp.json();

    // Unified family-member list. Newer backends return `children` (covers
    // spawned helpers AND optimizer worker/closer sessions, each with a label +
    // role); fall back to the legacy `spawns` array so an un-restarted backend
    // still renders spawn tabs.
    const children = (data.children && data.children.length)
      ? data.children
      : (data.spawns || []).map(s => ({
          session_id: s.spawn_session_id || s.id, label: null,
          role: 'spawn', name: s.name, status: s.status,
        }));
    const browsers = data.browser_sessions || [];
    const curSid = app.currentSessionId;
    const parent = data.parent || null;   // set when THIS session is itself a child
    const rootLabel = data.root_label || 'Main';
    const groupKind = data.group_kind
      || (children.length || parent ? 'orchestrator' : null);
    // The root tab target (orchestrator / Planner). Fallbacks keep an
    // un-restarted backend working: a child → its parent is the root; a root
    // (has members, no parent) → it IS the root.
    const orchestrator = data.orchestrator
      || (parent && parent.session_id ? { session_id: parent.session_id, title: parent.title } : null)
      || (children.length ? { session_id: curSid, title: null } : null);

    // A family renders as one compact Spawns (n) header trigger. Its dropdown
    // always puts the root chat first, then every member, so users can hop
    // freely without one header tab per helper. Shown whenever there is a family
    // relationship: this session has members, or it is a member (so it still
    // offers a root-chat route on an un-restarted backend without siblings).
    const hasFamily = !!(orchestrator && orchestrator.session_id
                         && (children.length || (parent && parent.session_id)));

    // ── Build all chips into a single carousel strip ──
    let chipsHtml = '';
    if (hasFamily) {
      // Keep the header compact: one trigger opens a menu containing the root
      // chat first, followed by every spawned helper. The full family data is
      // attached to the trigger after render rather than serialized into HTML.
      const runningCount = children.filter(sp => sp.status === 'running' || sp.status === 'queued').length;
      const triggerIcon = runningCount ? icon('loader-2', { size: '10px' }) : icon('git-branch', { size: '10px' });
      const triggerTitle = `${children.length} spawned helper${children.length === 1 ? '' : 's'}`;
      // Tab age = the most recent helper's appearance (when the family last
      // gained a member). Each helper keeps its own time inside the menu.
      const newestChildAt = children.reduce(
        (acc, sp) => ((sp.created_at || sp.updated_at || '') > acc ? (sp.created_at || sp.updated_at || '') : acc),
        '',
      );
      chipsHtml += `<button type="button" class="sub-chip chip-spawn-menu${runningCount ? ' chip-running' : ''}" title="${triggerTitle}" aria-haspopup="menu" aria-expanded="false" data-family-menu><span class="chip-status">${triggerIcon}</span>Spawns (${children.length})${_chipAge(newestChildAt)}</button>`;
    }

    // Browser tabs
    if (browsers.length) {
      for (const bs of browsers) {
        const isPrivate = !bs.shared;
        const state = String(bs.status || 'active').toLowerCase();
        const stateClass = state === 'idle' ? ' chip-browser-idle'
          : state === 'closed' ? ' chip-browser-closed' : '';
        const chipClass = 'sub-chip chip-browser' + stateClass + (isPrivate ? ' chip-private' : '');
        const title = (bs.url || bs.title || 'Browser') + ` — ${state}` + (isPrivate ? ' (private)' : '');
        chipsHtml += `<button type="button" class="${chipClass}" title="${_escAttr(title)}" data-bs-id="${_escAttr(bs.id)}"${_tabAttrs('browser:' + bs.id, bs.updated_at || bs.created_at)}><span class="chip-status">${icon('globe', { size: '10px' })}</span><span class="chip-label">Browser</span>${_chipAge(bs.created_at || bs.updated_at)}<span class="chip-close" role="button" tabindex="0" title="Close browser" aria-label="Close browser" data-close-bs-id="${_escAttr(bs.id)}">${icon('x', { size: '10px' })}</span></button>`;
      }
    }

    // GenUI tabs — genui pages this session built/edited. Closing one only
    // UNLINKS it (a local dismissal, keyed per session): the page itself is
    // never touched, and the tab returns if the agent builds the page again.
    const genuiChips = (data.genui_sessions || [])
      .filter(g => g && g.slug && !_isGenuiUnlinked(sid, g.slug));
    if (genuiChips.length) {
      for (const g of genuiChips) {
        const label = _esc(g.title || g.slug);
        const title = `${label} \u2014 GenUI page`;
        chipsHtml += `<button type="button" class="sub-chip chip-genui" title="${_escAttr(title)}" data-genui-slug="${_escAttr(g.slug)}"${_tabAttrs('genui:' + g.slug, g.updated_at)}><span class="chip-status">${icon('layout-dashboard', { size: '10px' })}</span>${label}${_chipAge(g.created_at || g.updated_at)}<span class="chip-close" role="button" tabindex="0" title="Unlink genui (page stays saved)" aria-label="Unlink genui" data-close-genui-slug="${_escAttr(g.slug)}">${icon('x', { size: '10px' })}</span></button>`;
      }
    }

    // Agent component tabs — active chat components (present_chat_component)
    // for this session. Like the spawned-helper family above, they collapse
    // into ONE compact "Elements (n)" header trigger instead of one chip per
    // component; its dropdown (_openComponentMenu) lists every plan / checklist
    // / component and selecting a row opens it in the agent panel. Each row
    // keeps a ⋯ that reuses the component Show / Hide popup. The GET only
    // returns active components — completed/deleted are filtered server-side —
    // so a row clears the moment its component completes or is deleted. Live
    // refresh comes from the chat-components-changed event (chat-components.js).
    const compItems = await _fetchComponentChips(sid);
    if (compItems.length) {
      // Trigger age = the newest element's time (when the group last gained or
      // changed an entry). Every element keeps its own age inside the menu, the
      // same way the Spawns trigger ages by its newest helper.
      const newestCompAt = compItems.reduce(
        (acc, c) => (((c.created_at || c.updated_at || '') > acc) ? (c.created_at || c.updated_at || '') : acc),
        '',
      );
      const compTitle = `${compItems.length} chat element${compItems.length === 1 ? '' : 's'}`;
      // The group is a tab in its own right: it carries the shared recency key
      // ("component:" + session) and dates from its NEWEST element, so it sorts
      // among the session / browser / genui tabs by when the group last changed —
      // exactly how the Spawns trigger ages by its newest helper.
      chipsHtml += `<button type="button" class="sub-chip chip-component-menu" title="${_escAttr(compTitle)}" aria-haspopup="menu" aria-expanded="false" data-component-menu${_tabAttrs('component:' + curSid, newestCompAt)}><span class="chip-status">${icon('layers', { size: '10px' })}</span>Elements (${compItems.length})${_chipAge(newestCompAt)}</button>`;
    }

    const wrap = document.getElementById('chat-sub-scroll-wrap');
    if (wrap) {
      _closeFamilyMenu();
      _closeComponentMenu();
      wrap.innerHTML = chipsHtml;
      const familyMenuTrigger = wrap.querySelector('[data-family-menu]');
      if (familyMenuTrigger) {
        familyMenuTrigger._familyMenu = {
          root: {
            sessionId: orchestrator.session_id,
            label: rootLabel,
            title: orchestrator.title || `${rootLabel} session`,
            updatedAt: orchestrator.updated_at,
            icon: (groupKind === 'optimizer' && rootLabel !== 'Main') ? 'wand-2' : 'layout-dashboard',
          },
          children,
          groupKind,
          currentSessionId: curSid,
        };
      }
      const compMenuTrigger = wrap.querySelector('[data-component-menu]');
      if (compMenuTrigger) {
        // Same deferred-attachment trick as the family trigger: keep the full
        // element list off the DOM, attach it to the trigger for the menu.
        compMenuTrigger._componentMenu = { items: compItems, currentSessionId: curSid };
      }
      _sortSessionTabs(wrap);
      // Single delegated listener on the STATIC wrap — see _wireChipStrip.
      _wireChipStrip(wrap);
    }

    subHeader.style.display = (hasFamily || browsers.length || genuiChips.length || compItems.length) ? 'flex' : 'none';
    _wireSubCarousel();
  } catch (e) {
    console.warn('Failed to fetch related sessions:', e);
    subHeader.style.display = 'none';
  }
}

// ── Chip × close (bin a session / kill a browser / unlink a genui) ────────
// The sub-header chip close buttons let the user bin a family member (same
// DELETE the trash button uses — the /related endpoint then filters the
// recycled session out so the tab actually disappears), kill a browser
// session, or unlink a genui page (local dismissal only — nothing deleted).
// The root "Main" chip has no close button. After a successful close the tab
// bar is re-fetched so the chip disappears (and, if the deleted session was
// the open one, the chat switches to a remaining session — deleteSession
// already handles that).
async function _handleChipClose(btn) {
  const spawnId = btn.dataset.closeSpawnId;
  const bsId = btn.dataset.closeBsId;
  const genuiSlug = btn.dataset.closeGenuiSlug;
  try {
    if (spawnId) {
      await deleteSession(spawnId, { retries: 1 });
    } else if (bsId) {
      // A session-tab close tears down Playwright but retains the durable row,
      // allowing the strip to show its closed/struck state and reopen it later.
      const res = await fetch(apiPath('/api/v1/browser/sessions/' + encodeURIComponent(bsId) + '/close'), {
        method: 'POST', headers: { ...authHeaders() },
      });
      if (!res.ok) return;
    } else if (genuiSlug) {
      // Unlink: local dismissal only — the genui page itself is untouched.
      _unlinkGenui(app.currentSessionId, genuiSlug);
    }
  } catch (_e) {
    // Never let a failed close break the tab bar; it re-renders on next poll.
  }
  _fetchRelatedSessions();
}

// ── Spawn-family menu ──────────────────────────────────────────────────────
// A family used to consume one header chip per helper. Keep one compact trigger
// instead, then expose the root chat and helpers as selectable menu rows.
let _familyMenuEl = null;
let _familyMenuTrigger = null;
function _closeFamilyMenu() {
  if (_familyMenuEl) { _familyMenuEl.remove(); _familyMenuEl = null; }
  if (_familyMenuTrigger) _familyMenuTrigger.setAttribute('aria-expanded', 'false');
  _familyMenuTrigger = null;
  document.removeEventListener('pointerdown', _onFamilyMenuOutside, true);
  document.removeEventListener('keydown', _onFamilyMenuEscape, true);
  window.removeEventListener('resize', _closeFamilyMenu);
}
function _onFamilyMenuOutside(e) {
  if (_familyMenuEl && !_familyMenuEl.contains(e.target) && !_familyMenuTrigger?.contains(e.target)) _closeFamilyMenu();
}
function _onFamilyMenuEscape(e) {
  if (e.key === 'Escape') _closeFamilyMenu();
}
function _openFamilyMenu(trigger) {
  const family = trigger && trigger._familyMenu;
  if (!family) return;
  if (_familyMenuTrigger === trigger) { _closeFamilyMenu(); return; }
  _closeFamilyMenu();
  _closeComponentMenu();

  const memberRows = family.children.map((sp, i) => {
    const spSid = sp.session_id;
    const isActive = spSid === family.currentSessionId;
    const statusClass = sp.status === 'running' || sp.status === 'queued' ? 'chip-running'
      : sp.status === 'done' ? 'chip-done'
        : sp.status === 'error' ? 'chip-error' : '';
    const label = sp.label || (family.groupKind === 'optimizer'
      ? `Optimizer: ${family.root.title || family.root.label}`
      : `Spawn ${i + 1}`);
    const name = sp.name && sp.name !== label ? sp.name : '';
    const roleIcon = sp.role === 'closer' ? 'flag'
      : sp.role === 'planner' ? 'wand-2'
        : sp.role === 'delegate' ? 'share-2' : 'git-branch';
    const statusIcon = sp.status === 'running' || sp.status === 'queued'
      ? icon('loader-2', { size: '12px' })
      : sp.status === 'done' ? icon('check-circle', { size: '12px' })
        : sp.status === 'error' ? icon('alert-circle', { size: '12px' })
          : icon(roleIcon, { size: '12px' });
    const completed = sp.run_status && ['complete', 'interrupted', 'error'].includes(sp.run_status) && sp.completed_at;
    const time = completed ? `<span class="chip-time" data-completed-at="${_escAttr(sp.completed_at)}">${_formatRelativeTime(sp.completed_at)}</span>` : '';
    const more = `<span class="chip-more" role="button" tabindex="0" title="More options for ${_escAttr(name || label)}" aria-label="More options for ${_escAttr(name || label)}" data-more-spawn-id="${_escAttr(spSid)}">${icon('more-vertical', { size: '14px' })}</span>`;
    return `<button type="button" class="family-menu-item ${statusClass}${isActive ? ' selected' : ''}" role="menuitem" data-family-session-id="${_escAttr(spSid)}"${_tabAttrs('session:' + spSid, sp.updated_at || sp.completed_at)}><span class="chip-status">${statusIcon}</span><span class="family-menu-text"><span>${_esc(label)}</span>${name ? `<small>${_esc(name)}</small>` : ''}</span>${time}${more}</button>`;
  }).join('');

  const rootActive = family.root.sessionId === family.currentSessionId;
  const menu = document.createElement('div');
  menu.className = 'family-spawn-menu sub-chip-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Spawn sessions');
  menu.innerHTML = `<button type="button" class="family-menu-item family-menu-root${rootActive ? ' selected' : ''}" role="menuitem" data-family-session-id="${_escAttr(family.root.sessionId)}"${_tabAttrs('session:' + family.root.sessionId, family.root.updatedAt)}><span class="chip-status">${icon(family.root.icon, { size: '12px' })}</span><span class="family-menu-text"><span>${_esc(family.root.label)}</span><small>Main chat</small></span></button>${memberRows}`;
  document.body.appendChild(menu);
  const r = trigger.getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + 'px';
  menu.style.left = Math.max(4, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.addEventListener('click', (e) => {
    const moreBtn = e.target.closest('.chip-more');
    if (moreBtn) {
      e.stopPropagation();
      _openChipMenu(moreBtn);
      _closeFamilyMenu();
      return;
    }
    const item = e.target.closest('[data-family-session-id]');
    if (!item) return;
    _touchSessionTab(item);
    const targetSessionId = item.dataset.familySessionId;
    _closeFamilyMenu();
    if (targetSessionId && targetSessionId !== app.currentSessionId) switchToSession(targetSessionId);
  });
  menu.addEventListener('keydown', (e) => {
    const moreBtn = e.target.closest('.chip-more');
    if (moreBtn && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      _openChipMenu(moreBtn);
      _closeFamilyMenu();
    }
  });
  _familyMenuEl = menu;
  _familyMenuTrigger = trigger;
  trigger.setAttribute('aria-expanded', 'true');
  document.addEventListener('pointerdown', _onFamilyMenuOutside, true);
  document.addEventListener('keydown', _onFamilyMenuEscape, true);
  window.addEventListener('resize', _closeFamilyMenu);
}

// ── Chip strip interaction — ONE delegated listener on the static wrap ─────
// The wrap element itself is never replaced (only its chip children are), so
// the listener is attached exactly once. The old code re-added the listener on
// every 15s-poll render, so after a long session a single click fired N close
// handlers at once (N DELETE round-trips + N refetches — and, combined with the
// /related endpoint listing every agent_spawns row forever, made the spawn ×
// look dead: the tab simply came straight back).
function _wireChipStrip(wrap) {
  if (!wrap || wrap.dataset.chipWired === '1') return;
  wrap.dataset.chipWired = '1';

  const onClick = (e) => {
    // × close: bin a family session / kill a browser / unlink a genui.
    const closeBtn = e.target.closest('.chip-close');
    if (closeBtn) { e.stopPropagation(); _handleChipClose(closeBtn); return; }
    // ⋯ more: hide / bin a sub-agent session.
    const moreBtn = e.target.closest('.chip-more');
    if (moreBtn) { e.stopPropagation(); _openChipMenu(moreBtn); return; }
    const familyMenu = e.target.closest('[data-family-menu]');
    if (familyMenu) { e.stopPropagation(); _openFamilyMenu(familyMenu); return; }
    // "Elements (n)" group trigger → dropdown of every active chat element.
    const componentMenu = e.target.closest('[data-component-menu]');
    if (componentMenu) { e.stopPropagation(); _openComponentMenu(componentMenu); return; }
    const chip = e.target.closest('[data-parent-id], [data-spawn-id], [data-bs-id], [data-genui-slug], [data-component-id]');
    if (!chip) return;
    _touchSessionTab(chip);
    const curSid = app.currentSessionId;
    if (chip.dataset.parentId && chip.dataset.parentId !== curSid) {
      switchToSession(chip.dataset.parentId);
    } else if (chip.dataset.spawnId && chip.dataset.spawnId !== curSid) {
      switchToSession(chip.dataset.spawnId);
    } else if (chip.dataset.bsId) {
      const webTab = document.querySelector('.main-tab[data-value="browser"]');
      if (webTab) webTab.click();
      if (window.__switchWebSession) {
        window.__switchWebSession(chip.dataset.bsId);
      }
    } else if (chip.dataset.genuiSlug) {
      const genuiTab = document.querySelector('.main-tab[data-value="genui"]');
      if (genuiTab) genuiTab.click();
      if (window.__switchGenuiPage) {
        window.__switchGenuiPage(chip.dataset.genuiSlug);
      }
    } else if (chip.dataset.componentId) {
      // Component chip → show THAT component in the agent panel (or collapse
      // if it's the one already showing).
      _toggleComponentPanel(chip.dataset.componentId);
    }
  };
  const onKeydown = (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    const closeBtn = e.target.closest('.chip-close');
    if (closeBtn) { e.preventDefault(); _handleChipClose(closeBtn); return; }
    const moreBtn = e.target.closest('.chip-more');
    if (moreBtn) { e.preventDefault(); _openChipMenu(moreBtn); return; }
    const familyMenu = e.target.closest('[data-family-menu]');
    if (familyMenu) { e.preventDefault(); _openFamilyMenu(familyMenu); return; }
    const componentMenu = e.target.closest('[data-component-menu]');
    if (componentMenu) { e.preventDefault(); _openComponentMenu(componentMenu); }
  };
  wrap.addEventListener('click', onClick);
  wrap.addEventListener('keydown', onKeydown);
}

// ── Sub-agent chip "more" menu (hide / bin) ────────────────────────────────
// Body-mounted floating menu anchored under the ⋯ button, mirroring the
// session-dropdown row-actions popup (.session-row-actions). Hide = PATCH
// hidden:true — the session stays intact but drops out of the list + tab bar
// (reversible via the dropdown's show-hidden manage mode). Bin = recycle
// (DELETE; transcript kept in the recycle bin). Either way the tab bar
// re-fetches so the chip disappears.
let _chipMenuEl = null;
function _closeChipMenu() {
  if (_chipMenuEl) { _chipMenuEl.remove(); _chipMenuEl = null; }
  document.removeEventListener('pointerdown', _onChipMenuOutside, true);
  document.removeEventListener('keydown', _onChipMenuEsc, true);
  window.removeEventListener('resize', _closeChipMenu);
}
function _onChipMenuOutside(e) {
  if (_chipMenuEl && !_chipMenuEl.contains(e.target) && !e.target.closest('.chip-more')) {
    _closeChipMenu();
  }
}
function _onChipMenuEsc(e) { if (e.key === 'Escape') _closeChipMenu(); }
function _openChipMenu(moreBtn) {
  _closeChipMenu();
  _closeFamilyMenu();
  _closeComponentMenu();
  const spawnId = moreBtn.dataset.moreSpawnId;
  const componentId = moreBtn.dataset.moreComponentId;
  if (!spawnId && !componentId) return;
  const menu = document.createElement('div');
  menu.className = 'sub-chip-menu';
  if (spawnId) {
    menu.innerHTML =
      `<button type="button" class="sub-chip-menu-item" data-action="hide">${icon('eye-off', { size: '14px' })} Hide session</button>` +
      `<button type="button" class="sub-chip-menu-item danger" data-action="bin">${icon('trash-2', { size: '14px' })} Bin session</button>`;
  } else {
    // Component chip ⋯ — Show / Hide the agent panel for now;
    // per-component actions (rename, pin, re-present) can slot in later.
    menu.innerHTML =
      `<button type="button" class="sub-chip-menu-item" data-action="show">${icon('eye', { size: '14px' })} Show panel</button>` +
      `<button type="button" class="sub-chip-menu-item" data-action="hide">${icon('eye-off', { size: '14px' })} Hide panel</button>`;
  }
  document.body.appendChild(menu);
  const r = moreBtn.getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + 'px';
  menu.style.left = Math.max(4, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.addEventListener('click', async (e) => {
    const item = e.target.closest('.sub-chip-menu-item');
    if (!item) return;
    const action = item.dataset.action;
    _closeChipMenu();
    try {
      if (spawnId) {
        if (action === 'hide') {
          await _setSessionHidden(spawnId, true);
        } else if (action === 'bin') {
          await deleteSession(spawnId, { retries: 1 });
        }
      } else if (componentId) {
        if (action === 'show' && typeof app.componentPanelShow === 'function') app.componentPanelShow(componentId);
        else if (action === 'hide' && typeof app.componentPanelHide === 'function') app.componentPanelHide();
      }
    } catch (_e) {
      // Best-effort; the tab bar re-renders on next poll either way.
    }
    _fetchRelatedSessions();
  });
  document.addEventListener('pointerdown', _onChipMenuOutside, true);
  document.addEventListener('keydown', _onChipMenuEsc, true);
  window.addEventListener('resize', _closeChipMenu);
  _chipMenuEl = menu;
}

// ── Agent component group: fetch + icon + panel toggle ─────────────────────
// The single "Elements (n)" header trigger and its dropdown rows are built from
// the same GET the agent panel hydrates from, so the header group and the panel
// always agree on the active set.
function _componentChipLabel(component) {
  // The persistent plan document uses a stable, concise header-tab label.
  if (component?.id === 'plan-overview') return 'Plan';
  // The user's Scratchpad document is likewise pinned to one stable id per session.
  if (component?.id === 'scratchpad') return 'Scratchpad';
  return component?.title || component?.type?.replace('_', ' ') || 'Component';
}

// Chat-header tab age counter — a compact "when this tab appeared" label
// (e.g. "10h"), mirroring the session-title-bar timer (see #session-dropdown-status)
// but smaller and tighter (styling in app1.css: .chip-age). The value counts up
// from the resource's creation time; the 1s _startCompletedTimeTick refreshes
// every [data-created-at] chip-age live. Empty when the backend exposes no time.
function _chipAge(iso) {
  if (!iso) return '';
  return `<span class="chip-age" data-created-at="${_escAttr(iso)}">${_formatRelativeTime(iso)}</span>`;
}

// Element recency in ms for the "Elements" dropdown ordering. The element
// touched most recently wins: `updated_at` (last write) is authoritative, with
// `created_at` (first appearance) as the fallback, so a freshly appeared
// component — whose first write IS its update — still sorts to the top. Naive
// DB stamps are UTC; normalize before parsing so Date.parse doesn't read them
// as local time (same guard as _sessionTabScore).
function _componentRecencyMs(component) {
  const raw = String(component?.updated_at || component?.created_at || '');
  if (!raw) return 0;
  const normalized = /^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?$/.test(raw)
    ? raw.replace(' ', 'T') + 'Z'
    : raw;
  return Date.parse(normalized) || 0;
}

const COMPONENT_CHIP_CACHE_MS = 15_000;
let _compChipsInFlight = null;
let _componentChipsCache = { sid: "", fetchedAt: 0, items: [] };
let _componentChipsGeneration = 0;

function _invalidateComponentChips(sid) {
  if (!sid || _componentChipsCache.sid === sid) {
    _componentChipsCache = { sid: "", fetchedAt: 0, items: [] };
  }
}

// Active chat components for one session. Returns the RAW rows (not chip HTML):
// the caller renders the single "Elements (n)" trigger and _openComponentMenu
// renders its dropdown rows, so the header group and the panel are always built
// from one source of truth.
async function _fetchComponentChips(sid) {
  if (!sid || !app.currentUserId) return [];
  const cached = _componentChipsCache;
  if (cached.sid === sid && Date.now() - cached.fetchedAt < COMPONENT_CHIP_CACHE_MS) {
    return cached.items;
  }
  if (_compChipsInFlight && _compChipsInFlight.sid === sid) return _compChipsInFlight.promise;
  const generation=_componentChipsGeneration;
  const promise = (async () => {
    try {
      const resp = await fetch(apiPath("/api/v1/chat/components?session_id=" + encodeURIComponent(sid)), { headers: authHeaders() });
      if (!resp.ok) return [];
      const items = ((await resp.json()).components || []).filter(c => c && c.id);
      if(generation!==_componentChipsGeneration)
        return _componentChipsCache.sid===sid ? _componentChipsCache.items : [];
      _componentChipsCache = { sid, fetchedAt: Date.now(), items };
      return items;
    } catch (_e) {
      return [];
    }
  })();
  _compChipsInFlight = { sid, promise };
  promise.finally(() => {
    if (_compChipsInFlight && _compChipsInFlight.promise === promise) _compChipsInFlight = null;
  });
  return promise;
}

function _componentTypeIcon(type) {
  return componentTypeIcon(type);
}

// Show a specific component in the agent panel (or collapse it if it's the
// one already showing). Falls back to a plain toggle if the per-component
// API isn't available.
function _toggleComponentPanel(componentId) {
  const panel = document.getElementById('chat-component-panel');
  if (!panel || !panel.classList.contains('has-components')) return;
  if (typeof app.componentPanelToggle === 'function') {
    app.componentPanelToggle(componentId);
    return;
  }
  if (typeof app.componentPanelShow !== 'function' || typeof app.componentPanelHide !== 'function') return;
  if (panel.classList.contains('open')) app.componentPanelHide();
  else app.componentPanelShow(componentId);
}

// ── Chat-element ("Elements") group menu ───────────────────────────────────
// The chat header used to spend one chip per active chat component (plan,
// checklist, …). It now mirrors the spawned-helper family above: one compact
// "Elements (n)" trigger opens this menu, whose rows open each element in the
// agent panel. A row's ⋯ reuses the component Show / Hide popup (_openChipMenu).
// Only ONE chip menu is ever open — this, the family menu, and the ⋯ popup
// close each other.
let _componentMenuEl = null;
let _componentMenuTrigger = null;
function _closeComponentMenu() {
  if (_componentMenuEl) { _componentMenuEl.remove(); _componentMenuEl = null; }
  if (_componentMenuTrigger) _componentMenuTrigger.setAttribute('aria-expanded', 'false');
  _componentMenuTrigger = null;
  document.removeEventListener('pointerdown', _onComponentMenuOutside, true);
  document.removeEventListener('keydown', _onComponentMenuEscape, true);
  window.removeEventListener('resize', _closeComponentMenu);
}
function _onComponentMenuOutside(e) {
  if (_componentMenuEl && !_componentMenuEl.contains(e.target) && !_componentMenuTrigger?.contains(e.target)) _closeComponentMenu();
}
function _onComponentMenuEscape(e) { if (e.key === 'Escape') _closeComponentMenu(); }

// Which element the panel currently shows ('' when collapsed), read from the
// panel DOM — the render loop leaves exactly one card un-.cc-hidden.
function _activeComponentIdFromPanel() {
  const panel = document.getElementById('chat-component-panel');
  if (!panel || !panel.classList.contains('open')) return '';
  const card = panel.querySelector('[data-chat-component-id]:not(.cc-hidden)');
  return card ? card.dataset.chatComponentId : '';
}

function _openComponentMenu(trigger) {
  const group = trigger && trigger._componentMenu;
  if (!group || !group.items.length) return;
  if (_componentMenuTrigger === trigger) { _closeComponentMenu(); return; }
  _closeComponentMenu();
  _closeFamilyMenu();

  const activeId = _activeComponentIdFromPanel();
  // Order the dropdown most-recent-first: the element touched/written last is
  // the top row, so a freshly appeared (or just-updated) plan / checklist is
  // always within reach. Ties keep the server's order (Array.sort is stable).
  const ordered = [...group.items].sort((a, b) => _componentRecencyMs(b) - _componentRecencyMs(a));
  const rows = ordered.map(c => {
    const label = _esc(_componentChipLabel(c));
    const isActive = c.id === activeId;
    const more = `<span class="chip-more" role="button" tabindex="0" title="More options for ${label}" aria-label="More options for ${label}" data-more-component-id="${_escAttr(c.id)}">${icon('more-vertical', { size: '14px' })}</span>`;
    return `<button type="button" class="family-menu-item${isActive ? ' selected' : ''}" role="menuitem" data-component-id="${_escAttr(c.id)}"><span class="chip-status">${icon(_componentTypeIcon(c.type), { size: '12px' })}</span><span class="family-menu-text"><span>${label}</span></span>${_chipAge(c.created_at || c.updated_at)}${more}</button>`;
  }).join('');

  const menu = document.createElement('div');
  menu.className = 'family-spawn-menu family-component-menu sub-chip-menu';
  menu.setAttribute('role', 'menu');
  menu.setAttribute('aria-label', 'Chat elements');
  menu.innerHTML = rows;
  document.body.appendChild(menu);
  const r = trigger.getBoundingClientRect();
  menu.style.top = (r.bottom + 4) + 'px';
  menu.style.left = Math.max(4, Math.min(r.left, window.innerWidth - menu.offsetWidth - 8)) + 'px';
  menu.addEventListener('click', (e) => {
    const moreBtn = e.target.closest('.chip-more');
    if (moreBtn) {
      e.stopPropagation();
      _openChipMenu(moreBtn);
      _closeComponentMenu();
      return;
    }
    const item = e.target.closest('[data-component-id]');
    if (!item) return;
    _closeComponentMenu();
    // Selecting a row OPENS that element (a menu row selects its target, like
    // the family menu) — the ⋯ and the panel's own minimize still hide it.
    if (typeof app.componentPanelShow === 'function') app.componentPanelShow(item.dataset.componentId);
    else _toggleComponentPanel(item.dataset.componentId);
  });
  menu.addEventListener('keydown', (e) => {
    const moreBtn = e.target.closest('.chip-more');
    if (moreBtn && (e.key === 'Enter' || e.key === ' ')) {
      e.preventDefault();
      _openChipMenu(moreBtn);
      _closeComponentMenu();
    }
  });
  _componentMenuEl = menu;
  _componentMenuTrigger = trigger;
  trigger.setAttribute('aria-expanded', 'true');
  document.addEventListener('pointerdown', _onComponentMenuOutside, true);
  document.addEventListener('keydown', _onComponentMenuEscape, true);
  window.addEventListener('resize', _closeComponentMenu);
}

// PATCH a session's hidden flag (same endpoint the dropdown's eye toggle uses).
async function _setSessionHidden(sid, hidden) {
  try {
    await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), {
      method: 'PATCH',
      headers: { ...authHeaders(), 'Content-Type': 'application/json' },
      body: JSON.stringify({ hidden: !!hidden }),
    });
  } catch (_e) {
    // Best-effort.
  }
}

// ── GenUI unlink state (local dismissal, nothing deleted) ──────────────────
// "Unlinking" a genui tab just remembers the dismissal per chat session in
// localStorage. The genui page itself is never touched; a fresh build of the
// same slug re-shows the tab.
const _GENUI_UNLINK_KEY = 'genui-unlinked-tabs';
function _getGenuiUnlinked() {
  try { return JSON.parse(localStorage.getItem(_GENUI_UNLINK_KEY) || '{}') || {}; } catch (_e) { return {}; }
}
function _isGenuiUnlinked(sid, slug) {
  const m = _getGenuiUnlinked();
  return !!(sid && slug && Array.isArray(m[sid]) && m[sid].includes(slug));
}
function _unlinkGenui(sid, slug) {
  if (!sid || !slug) return;
  const m = _getGenuiUnlinked();
  const arr = Array.isArray(m[sid]) ? m[sid] : [];
  if (!arr.includes(slug)) { arr.push(slug); m[sid] = arr; }
  try { localStorage.setItem(_GENUI_UNLINK_KEY, JSON.stringify(m)); } catch (_e) {}
}

// ── Sub-header carousel wiring (scroll-out chevrons + edge fades) ────────
// Mirrors the Agents / Instances page carousel pattern. Hooked after every
// chip render in _fetchRelatedSessionsInner.
function _wireSubCarousel() {
  const wrap = document.getElementById('chat-sub-scroll-wrap');
  if (!wrap) return;
  // Prevent duplicate wiring — the wrap element is static (only chips inside it
  // are replaced), so skip if already wired.
  if (wrap.dataset.carouselWired === '1') return;
  wrap.dataset.carouselWired = '1';

  const chevLeft  = wrap.parentElement?.querySelector('.sub-carousel-chev.left');
  const chevRight = wrap.parentElement?.querySelector('.sub-carousel-chev.right');

  const update = () => {
    const maxScroll = wrap.scrollWidth - wrap.clientWidth;
    const atStart = wrap.scrollLeft <= 1;
    const atEnd   = wrap.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left',  overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
    // Toggle chevrons directly (left chevron is before wrap in DOM, so ~ selector can't reach it)
    if (chevLeft)  chevLeft.style.display  = (overflowing && !atStart) ? 'flex' : 'none';
    if (chevRight) chevRight.style.display = (overflowing && !atEnd)   ? 'flex' : 'none';
  };
  wrap.addEventListener('scroll', update, { passive: true });
  applyRubberBand(wrap);
  requestAnimationFrame(update);
  setTimeout(update, 120);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(update).observe(wrap);

  const page = () => Math.max(wrap.clientWidth * 0.65, 120);
  if (chevLeft)  chevLeft.addEventListener('click',  e => { e.stopPropagation(); wrap.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); wrap.scrollBy({ left:  page(), behavior: 'smooth' }); });
}

// ── Session title (applied from WS titler) ─────────────────────────────────

function applySessionTitle(event) {
  const sid = event && (event.session_id || event.sessionId);
  if (!sid) return;
  const status = event.status || 'done';
  const dropdown = document.getElementById('session-dropdown');
  const isCurrent = sid === app.currentSessionId;

  if (status === 'generating') {
    if (isCurrent && dropdown) dropdown.dataset.titling = 'true';
    return;
  }

  if (event.title) {
    const found = _sessionsCache.find(s => s.id === sid);
    if (found) found.title = event.title;
  }
  if (isCurrent && dropdown) delete dropdown.dataset.titling;
  if (isCurrent) _setTriggerLabel();
}

// ── Session trigger label ──────────────────────────────────────────────────

function _setTriggerLabel() {
  let labelEl = document.getElementById('session-dropdown-label');
  const trigger = document.getElementById('session-dropdown-trigger');
  if (!labelEl && trigger) {
    // A live header rename input (⋯ → Rename / long-press) replaces the
    // label. Swapping it back mid-typing — e.g. on a session_title WS event
    // or any label refresh — would blur it and commit a half-typed name.
    // Leave it in place; the rename's own finish() restores the label first.
    if (trigger.querySelector('.session-row-title-input')) return;
    // No label AND no live rename input: the trigger lost its name field
    // (the rename commit detached the input without restoring the label).
    // Recreate the span in its slot (before the status) so the session name
    // never blanks out until a refresh.
    labelEl = document.createElement('span');
    labelEl.id = 'session-dropdown-label';
    labelEl.className = 'session-row-title';
    const statusEl = trigger.querySelector('#session-dropdown-status');
    trigger.insertBefore(labelEl, statusEl || null);
  }
  if (!labelEl) return;
  const sid = app.currentSessionId;
  const found = _sessionsCache.find(s => s.id === sid);
  const title = (found && found.title) || 'New Session';
  const agentId = (found && found.agent_id) || app.currentAgentId;
  const agentIcon = found?.agent_icon || _agentIconFor(agentId);
  const agentIconHtml = _agentIconHtml(agentId, '16px');
  labelEl.textContent = title || 'New Session';
  // Populate the trigger's agent icon (mirrors dropdown row icon).
  // DESIGN SYNC CONTRACT: a pinned current session wears the SAME amber warning
  // halo around this header avatar as its expanded-menu row and the
  // Sessions-page title column (`.session-row-agent-warning`). One pin therefore
  // reads identically in the collapsed chip and the open menu.
  const iconEl = document.getElementById('session-dropdown-icon');
  if (iconEl) {
    iconEl.classList.toggle('session-row-agent-warning', !!(found && found.pinned));
    let triggerIconHtml;
    if (found?.agent_icon || found?.agent_engine) {
      const name = found.agent_icon || '';
      const engine = found.agent_engine || '';
      if (engine === 'claude_code' && (!name || name === 'sparkles')) {
        triggerIconHtml = claudeMark({ size: '14px' });
      } else if (engine === 'codex' && (!name || name === 'code-2')) {
        triggerIconHtml = codexMark({ size: '14px' });
      } else if (!name) {
        triggerIconHtml = icon('bot', { size: '14px' });
      } else if (ICON_PICKER_ICONS.includes(name)) {
        triggerIconHtml = icon(name, { size: '14px' });
      } else {
        triggerIconHtml = `<span style="font-size:14px;line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
      }
    } else {
      triggerIconHtml = agentIconHtml || _esc(agentIcon);
    }
    iconEl.innerHTML = triggerIconHtml;
  }
  // Agent-name row above the session dropdown.
  const agentNameEl = document.getElementById('chat-header-agent-name');
  if (agentNameEl) {
    const agentName = found?.agent_name || _agentNameFor(agentId);
    if (agentName) {
      // Build icon HTML from session's own icon if available
      let headerIconHtml;
      if (found?.agent_icon || found?.agent_engine) {
        const name = found.agent_icon || '';
        const engine = found.agent_engine || '';
        if (engine === 'claude_code' && (!name || name === 'sparkles')) {
          headerIconHtml = claudeMark({ size: '20px' });
        } else if (engine === 'codex' && (!name || name === 'code-2')) {
          headerIconHtml = codexMark({ size: '20px' });
        } else if (!name) {
          headerIconHtml = icon('bot', { size: '20px' });
        } else if (ICON_PICKER_ICONS.includes(name)) {
          headerIconHtml = icon(name, { size: '20px' });
        } else {
          headerIconHtml = `<span style="font-size:20px;line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
        }
      } else {
        headerIconHtml = agentIconHtml || _esc(agentIcon);
      }
      agentNameEl.innerHTML = `<span class="header-agent-icon">${headerIconHtml}</span> <span class="header-agent-label">${_esc(agentName)}</span>`;
      agentNameEl.title = agentName;
      agentNameEl.style.display = '';
      // Move interactive affordances to the glass-chip wrapper so the
      // entire padded chip area is clickable (not just the inner text).
      const nameRow = agentNameEl.parentElement;
      if (nameRow) {
        nameRow.tabIndex = 0;
        nameRow.role = 'button';
        nameRow.setAttribute('aria-label', 'Switch agent');
      }
    } else {
      agentNameEl.textContent = '';
      agentNameEl.style.display = 'none';
    }
  }
  labelEl.title = ((found && found.id) || sid || '');
  // Set data-id on the trigger's kebab and delete buttons.
  const triggerKebab = document.getElementById('session-dropdown-kebab');
  const triggerDelete = document.getElementById('session-dropdown-delete');
  if (triggerKebab) triggerKebab.dataset.id = sid || '';
  if (triggerDelete) triggerDelete.dataset.id = sid || '';
  const sessionDropdown = document.getElementById('session-dropdown');
  if (sessionDropdown) sessionDropdown.dataset.loaded = 'true';
  const statusEl = document.getElementById('session-dropdown-status');
  if (statusEl) {
    // TITLE BAR == the current session's own ROW, collapsed. Same predicate
    // (sessionAttentionFlag), same tone/verb/note inputs and the same chip
    // markup as the row builder below, so a flag that reads in the open menu
    // reads identically here — and the chip's three-stage cycle
    // (ui/shared/js/needs-input-chip.js) runs in both places. The status span
    // stays the passive container and the chip is its child: that child is the
    // one click the trigger handler routes to the chip instead of the menu.
    const ackFlag = sessionAttentionFlag(found);
    if (ackFlag) {
      const ackLabel = flagLabel(ackFlag);
      const ackRead = sessionFlagIsRead(found);
      const ackTone = ackRead ? 'neutral' : flagTone(ackFlag);
      const ackVerb = ackRead ? 'unread' : 'read';
      const ackNote = closingCta(found);
      const ackHint = `${ackLabel} \u2014 click to mark ${ackVerb}`;
      // The row's own pairing, collapsed: the flag chip keeps its live "time ago"
      // counter beside it, exactly as the open menu renders it. The counter is a
      // plain [data-ts] span, which the 1s _startCompletedTimeTick already
      // refreshes under this element, so the collapsed bar reads like the row it
      // collapses — same chip, same tone, same recency value next to it.
      const needsInputAt = found.activity_at || found.updated_at;
      const needsInputCounter = needsInputAt
        ? `<span class="session-row-status session-status-idle session-needs-input-counter flag-tone-${ackTone}" title="Last active ${_parseDate(needsInputAt).toLocaleString()}" data-ts="${_esc(needsInputAt)}">${_formatRelativeTime(needsInputAt)}</span>`
        : '';
      statusEl.innerHTML = `<span class="session-row-status-group"><button type="button" class="session-row-status session-status-needs-input session-needs-input-chip flag-tone-${ackTone}" data-session-id="${_esc(found.id)}" data-state="needs-input" data-flag="${_esc(ackFlag)}" data-needs-input-label="${_escAttr(ackLabel)}" data-needs-input-verb="${ackVerb}" data-needs-input-note="${_escAttr(ackNote)}" title="${_escAttr(ackHint)}" aria-label="${_escAttr(ackHint)}">${_esc(ackLabel)}</button>${needsInputCounter}</span>`;
      statusEl.className = 'session-row-status session-dropdown-status session-dropdown-status-chip';
      statusEl.title = ackHint;
    } else if (found && (found.run_status === 'running' || found.run_status === 'resuming')) {
      statusEl.innerHTML = '<span class="session-radial-loader sm"></span>';
      statusEl.className = 'session-row-status session-dropdown-status session-status-running';
      // 'resuming' = stopped involuntarily and still inside the auto-resume
      // ladder — in progress, so the same spinning loader, different wording.
      statusEl.title = found.run_status === 'resuming'
        ? 'Re-assessing after an interruption\u2026'
        : 'Agent is thinking\u2026';
    } else if (found && found.run_status === 'queued') {
      const qPos = Number.isFinite(found.queue_position) ? found.queue_position : null;
      const qTot = Number.isFinite(found.queue_total) ? found.queue_total : null;
      const qLabel = qPos != null
        ? (qTot != null ? `#${qPos}/${qTot}` : `#${qPos}`)
        : '\u23f3';
      const qTitle = qPos != null
        ? (qTot != null ? `Waiting in the session queue: position ${qPos} of ${qTot}`
          : `Waiting in the session queue: position ${qPos}`)
        : 'Waiting in the session queue\u2026';
      statusEl.textContent = qLabel;
      statusEl.className = 'session-row-status session-dropdown-status session-status-queued';
      statusEl.title = qTitle;
    } else if (found && ['complete','interrupted','error'].includes(found.run_status) && (found.run_updated_at || found.activity_at || found.updated_at)) {
      const completedAt = found.run_updated_at || found.activity_at || found.updated_at;
      const _wasComplete = found.run_status === 'complete';
      // Complete = green timestamp; stopped (kill switch / user stop / crash)
      // = neutral grey "time since stopped".
      const isRead = _isSessionRead(found);
      const readClass = isRead ? ' session-status-read' : '';
      const readLabel = isRead ? 'Read · ' : '';
      statusEl.innerHTML = `<span data-ts="${_esc(completedAt)}">${_formatRelativeTime(completedAt)}</span>`;
      const statusClass = _wasComplete
        ? 'session-status-completed'
        : (found.run_status === 'error' ? 'session-status-error' : 'session-status-stopped');
      const resultLabel = _wasComplete ? 'Completed' : (found.run_status === 'error' ? 'Errored' : 'Stopped');
      statusEl.className = 'session-row-status session-dropdown-status ' + statusClass + readClass;
      statusEl.title = readLabel + resultLabel + ' ' + _parseDate(completedAt).toLocaleString();
    } else if (found && found.has_unread) {
      statusEl.innerHTML = icon('check-circle', { size: '12px' });
      statusEl.className = 'session-row-status session-dropdown-status session-status-unread';
      statusEl.title = 'New response ready';
    } else {
      // Idle current session: same muted live "time ago" as the Sessions page.
      const at = found && (found.activity_at || found.updated_at);
      if (at) {
        statusEl.innerHTML = `<span data-ts="${_esc(at)}">${_formatRelativeTime(at)}</span>`;
        statusEl.className = 'session-row-status session-dropdown-status session-status-idle';
        statusEl.title = 'Last active ' + _parseDate(at).toLocaleString();
      } else {
        statusEl.innerHTML = '';
        statusEl.className = 'session-row-status session-dropdown-status';
        statusEl.title = '';
      }
    }
  }
  _fetchRelatedSessions();
}

// The dropdown's status chips — the open menu's ROW chip and the collapsed
// TITLE BAR's chip — and the Agents → Sessions page share ONE predicate:
// `sessionAttentionFlag` (ui/shared/js/session-flag.js) shows the session's
// RESOLVED flag — the closer's own verdict when the run carries one, else the
// runtime run_status — for as long as the session wants attention: a clean
// `complete` drops its chip once read, every other settled flag keeps a NEUTRAL
// one. The notification panel deliberately keeps the stricter unread-only
// `sessionAckFlag`, so reading a session clears its notification even while the
// chip lingers on the two list surfaces.

// ── Clear header for a session switch ────────────────────────────────────
// Runs SYNCHRONOUSLY the instant a switch begins so the header never carries
// the OLD session's identity while the new one loads (the chat area switches
// over immediately; without this the label/agent-name/status only re-render
// after loadSessionChat's fetch resolves). Reuses the dropdown's existing
// "not loaded" state (data-loaded removed → CSS shimmers the label, dims the
// trigger, spins the chevron — the same affordance as boot). Every control is
// then re-populated in PARALLEL by its own loader once the new session's data
// arrives: _setTriggerLabel (label/icon/status/agent name), reloadExecutionMode
// + setExecutionMode (mode pill), reloadTargetDevice + setTargetDevice (target
// pill), _fetchRelatedSessions (sub-header family chips), refreshSessionChanges
// (changes badge). No fetches, no awaits — must never block the switch.
function _clearSessionHeader() {
  const dropdown = document.getElementById('session-dropdown');
  if (dropdown) {
    delete dropdown.dataset.loaded;
    delete dropdown.dataset.titling;
  }
  const label = document.getElementById('session-dropdown-label');
  if (label) label.textContent = '\u2014';
  const iconEl = document.getElementById('session-dropdown-icon');
  if (iconEl) {
    iconEl.innerHTML = icon('bot', { size: '14px' });
    // Drop the pinned halo with the old session's identity — the incoming
    // session's own pin state is re-applied by _setTriggerLabel when it lands.
    iconEl.classList.remove('session-row-agent-warning');
  }
  const statusEl = document.getElementById('session-dropdown-status');
  if (statusEl) {
    statusEl.innerHTML = '';
    statusEl.className = 'session-row-status session-dropdown-status';
    statusEl.title = '';
  }
  const kebab = document.getElementById('session-dropdown-kebab');
  const del = document.getElementById('session-dropdown-delete');
  if (kebab) kebab.dataset.id = '';
  if (del) del.dataset.id = '';
  const agentNameEl = document.getElementById('chat-header-agent-name');
  if (agentNameEl) {
    agentNameEl.textContent = '';
    agentNameEl.style.display = 'none';
  }
  // Sub-header family chips belong to the old session — hide until the new
  // session's /related fetch lands (mirrors the no-family state).
  const subHeader = document.getElementById('chat-sub-header');
  if (subHeader) subHeader.style.display = 'none';
  // Mode / target pills: blank now; reloadExecutionMode / reloadTargetDevice
  // fill them from the new session's localStorage keys synchronously right
  // after, and the server values (setExecutionMode / setTargetDevice) follow
  // when the fetch lands. Never shows a stale value in between.
  const modeBtn = document.getElementById('chat-mode-btn');
  if (modeBtn) modeBtn.textContent = '\u2014';
  const targetBtn = document.getElementById('chat-target-btn');
  if (targetBtn) {
    targetBtn.classList.remove('targeting');
    const labelEl = targetBtn.querySelector('.chat-target-label');
    if (labelEl) labelEl.textContent = '';
    targetBtn.title = 'Remote Control \u2014 choose which device runs your next message';
  }
  // Changes badge belongs to the old session's working tree (the 1s poll in
  // chat-session-changes.js refills it for the new id).
  const changesCount = document.getElementById('chat-changes-count');
  if (changesCount) { changesCount.textContent = '0'; changesCount.hidden = true; }
}

// ── Live tick: update the completed-time status every second ───────────
// Avoids a full poll just for ticking numbers. Only touches the trigger label.
let _completedTimeTick = null;
function _startCompletedTimeTick() {
  if (_completedTimeTick) return;
  _completedTimeTick = setInterval(() => {
    // Relative times tick live: trigger status + open-menu rows. Both the
    // completed-run timestamps and the idle last-activity times render a
    // [data-ts] span (mirrors the Sessions page's 1s time-ago refresh).
    const tsEls = document.querySelectorAll(
      '#session-dropdown-status [data-ts], #session-dropdown-menu [data-ts]'
    );
    for (const el of tsEls) {
      const at = el.dataset.ts;
      if (at) el.textContent = `${el.dataset.timePrefix || ''}${_formatRelativeTime(at)}`;
    }
    // Sub-header chip completion times
    const chipTimes = document.querySelectorAll('#chat-sub-scroll-wrap .chip-time, .family-spawn-menu .chip-time');
    for (const ct of chipTimes) {
      const at = ct.dataset.completedAt;
      if (at) ct.textContent = _formatRelativeTime(at);
    }
    // Chat-header tab age counters (the "Elements (n)" group trigger, browser /
    // genui / spawn chips) and the ages inside an open chip menu: each counts
    // up live from when its tab appeared.
    const chipAges = document.querySelectorAll('#chat-sub-scroll-wrap .chip-age[data-created-at], .sub-chip-menu .chip-age[data-created-at]');
    for (const ca of chipAges) {
      const at = ca.dataset.createdAt;
      if (at) ca.textContent = _formatRelativeTime(at);
    }
  }, 1000);
}

// ── Session-list tree: lazy child fetch + expand/collapse ──────────────────

// Fetch a parent's family members (spawns) via /related and cache them.
// Re-renders the list once they arrive so the nested child rows appear.
// Stores [] on failure so we don't spin.
async function _ensureGroupChildren(sid) {
  if (_childrenCache.has(sid)) return;
  if (_relatedAccessDenied) {
    _childrenCache.set(sid, []);
    return;
  }
  _childrenCache.set(sid, null);   // in-flight marker (renders a "Loading…" row)
  let kids = [];
  try {
    const token = localStorage.getItem('auth_token');
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/related?db=user.db`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    // Show-hidden manage mode also reveals hidden family members in the tree
    // (the tab bar itself always hides them).
    if (_getShowHidden()) url += '&include_hidden=1';
    const resp = await fetch(apiPath(url));
    if (resp.status === 401 || resp.status === 403) {
      _relatedAccessDenied = true;
    }
    if (resp.ok) {
      const data = await resp.json();
      kids = (data.children && data.children.length)
        ? data.children
        : (data.spawns || []).map(s => ({
            session_id: s.spawn_session_id || s.id, label: null,
            role: 'spawn', name: s.name, status: s.status,
          }));
    }
  } catch (e) {
    console.warn('Failed to fetch group children:', e);
  }
  _childrenCache.set(sid, kids);
  _renderSessionRows();
}

// Toggle a parent group open/closed. Exported so the dropdown's delegated click
// handler (controller.js) can call it from the caret button.
function toggleSessionGroup(sid) {
  if (!sid) return;
  if (_expandedGroups.has(sid)) {
    _expandedGroups.delete(sid);
  } else {
    _expandedGroups.add(sid);
    _ensureGroupChildren(sid);   // async; re-renders when ready
  }
  _renderSessionRows();
}

// Build the indented child rows for an expanded parent.
function _childRowsHtml(sid) {
  const kids = _childrenCache.get(sid);
  if (kids === null || kids === undefined) {
    return `<div class="session-child-row session-child-loading"><span class="session-radial-loader sm"></span><span class="session-row-sep"> </span>Loading…</div>`;
  }
  if (!kids.length) return '';
  let html = '';
  kids.forEach((k, i) => {
    const kSid = k.session_id;
    const label = k.label || `Spawn ${i + 1}`;
    const name = k.name && k.name !== label ? k.name : '';
    const roleIcon = k.role === 'closer' ? 'flag' : k.role === 'planner' ? 'wand-2' : 'git-branch';
    const isSel = kSid === app.currentSessionId ? ' selected' : '';
    let statusHtml = '';
    if (k.status === 'running' || k.status === 'queued' || k.status === 'resuming') {
      const _kTitle = k.status === 'resuming' ? 'Re-assessing after an interruption…' : 'Working…';
      statusHtml = `<span class="session-row-status session-status-running" title="${_kTitle}"><span class="session-radial-loader sm"></span></span>`;
    }
    const text = name ? `${label} · ${name}` : label;
    const childAgentIcon = _agentIconHtml(k.agent_id, '14px') || icon('bot', { size: '14px' });
    html += `
      <div class="session-row session-child-row${isSel}" data-id="${kSid}" title="${(name || label).replace(/"/g, '&quot;')}">
        <span class="session-row-child-icon">${icon(roleIcon, { size: '11px' })}</span>
        <span class="session-row-agent-icon">${childAgentIcon}</span>
        <span class="session-row-title">${text.replace(/</g, '&lt;')}</span>
        ${statusHtml}
      </div>`;
  });
  return html;
}

// ── Open-menu height refresh ─────────────────────────────────────────────
// When the dropdown menu is open (or mid-open) and its content size changes —
// e.g. the async session fetch resolves AFTER the open animation measured an
// empty 0-height menu — re-lock --session-menu-open-height to the real list
// height (clamped to the space the open positioned for). Without this the
// menu stays an invisible 0-height box until the next close/open cycle.
function _refreshOpenMenuHeight(menu) {
  if (!menu || menu.hidden) return;
  const state = menu.dataset.state;
  if (state !== 'open' && state !== 'opening') return;
  const viewportCap = Math.max(100, Math.floor(window.innerHeight * 0.66));
  const avail = Math.min(menu._availableHeight || menu.scrollHeight, viewportCap);
  const target = Math.min(menu.scrollHeight, avail);
  if (!(target > 0)) return;
  const current = parseFloat(menu.style.getPropertyValue('--session-menu-open-height')) || 0;
  if (Math.abs(current - target) < 1) return;
  menu.style.setProperty('--session-menu-open-height', `${target}px`);
}

function _sessionRowMatches(s, q, qActive, contentMode) {
  if (s.recycled && !_sessionDropdownConfig.menu.recycle_bin) return false;
  // Recycle-bin toggle: with no query it narrows to binned rows only; with a
  // query it keeps the bin view scoped to matches within it.
  if (_binMode && !s.recycled) return false;
  // FLAG chip: narrowed to sessions carrying a status flag, so idle sessions
  // are hidden until the chip is clicked again.
  if (_flagFilter && !sessionAttentionFlag(s)) return false;
  if (!qActive) return true;
  const titleHit = (s.title || '').toLowerCase().includes(q)
    || (s.agent_name || '').toLowerCase().includes(q);
  // Content mode: a row counts when its title matches OR its messages contain
  // the query (count chip). Content matches appear progressively as the scan
  // batches land; before that, title hits carry the row.
  if (contentMode && q.length >= _MSG_MIN_QUERY_LEN) {
    const info = _msgMatchInfo(s.id, q);
    if (info) { _msgHitCounts.set(s.id, info.count); return true; }
    _msgHitCounts.delete(s.id);
  }
  return titleHit;
}

// Restore focus + live value + caret to the footer search input after a
// wholesale menu re-render (debounced refetch, poll, gate events). Without
// this, the rebuild would drop in-progress keystrokes and yank focus away
// mid-typing.
function _restoreSearchInput(menu, state) {
  if (!state) return;
  const inp = menu.querySelector('.session-manage-search-input');
  if (!inp) return;
  inp.value = state.value;
  inp.focus();
  try {
    inp.setSelectionRange(
      Math.min(state.s ?? state.value.length, state.value.length),
      Math.min(state.e ?? state.value.length, state.value.length),
    );
  } catch (_) { /* caret restore is best-effort */ }
}

function _renderSessionRows() {
  const liveMenu = document.getElementById('session-dropdown-menu');
  if (!liveMenu) {
    _publishSessionDropdownSnapshot();
    return;
  }
  const reactManaged = _reactSessionDropdownActive && liveMenu.dataset.reactManaged === 'true';
  const menu = reactManaged ? document.createElement('div') : liveMenu;
  // An inline rename input (long-press or kebab → Rename) is live in the
  // menu. A wholesale rebuild here would detach the focused input, fire its
  // blur handler, and commit a half-typed name mid-typing (the "rename gets
  // cancelled by a background refresh" bug — WS agent_status gate events,
  // post-open metadata refresh and agent-icon hydration all re-render while
  // the menu is open). Skip the rebuild; the rename's own finish() removes
  // the input and triggers the next render, which then paints any updated
  // status/badges/order.
  if (liveMenu.querySelector('.session-row-title-input')) {
    _publishSessionDropdownSnapshot();
    return;
  }
  // Preserve the search input's live value/caret when it is focused so the
  // re-render below never drops mid-typing keystrokes.
  const _prevSearch = liveMenu.querySelector('.session-manage-search-input');
  const _searchState = _prevSearch && document.activeElement === _prevSearch
    ? { value: _prevSearch.value, s: _prevSearch.selectionStart, e: _prevSearch.selectionEnd }
    : null;
  delete menu.dataset.loading;
  if (reactManaged) { delete liveMenu.dataset.loading; liveMenu.removeAttribute('aria-busy'); }
  menu.removeAttribute('aria-busy');
  menu.innerHTML = '';

  const q = _searchQuery.trim().toLowerCase();
  const qActive = q.length > 0;
  // Title AND message content are searched together by default (the mode chip
  // is gone); content only contributes when the capability is on.
  const contentMode = qActive && _sessionDropdownConfig.menu.content_search;
  if (!qActive) _msgHitCounts.clear();

  const dropdownCfg = _sessionDropdownConfig;
  const menuCfg = dropdownCfg.menu;
  const sorted = _sortSessionsByPinnedOrder(
    [..._sessionsCache].filter(s => !_isChildSpawnSession(s)
      && (_showHidden || !s.hidden) && _sessionRowMatches(s, q, qActive, contentMode)),
  );
  if (!sorted.length) {
    const empty = document.createElement('div');
    empty.className = 'session-dropdown-empty';
    empty.textContent = qActive ? 'No matching sessions'
      : (_binMode ? 'Recycle bin is empty' : 'No sessions yet');
    menu.appendChild(empty);
    // Still show the manage footer below the empty message so hidden sessions
    // remain reachable when the visible list is empty.
    _appendManageRow(menu);
    _refreshOpenMenuHeight(menu);
    _restoreSearchInput(menu, _searchState);
    if (reactManaged) _captureSessionDropdownPresentation(menu, _searchState);
    _publishSessionDropdownSnapshot();
    return;
  }
  for (const s of sorted) {
    const hasChildren = menuCfg.grouping && (s.child_count || 0) > 0;
    const isExpanded = hasChildren && _expandedGroups.has(s.id);
    const row = document.createElement('div');
    row.className = 'session-row'
      + (s.pinned ? ' pinned' : '')
      + (s.recycled ? ' recycled' : '')
      + (hasChildren ? ' group-parent' : '')
      + (isExpanded ? ' expanded' : '')
      + (s.id === app.currentSessionId ? ' selected' : '');
    row.dataset.id = s.id;
    if (s.agent_id) row.dataset.agent = s.agent_id;
    const label = s.title || 'New Session';
    const sAgentIcon = s.agent_icon || '';
    const sAgentEngine = s.agent_engine || '';
    // Build icon HTML from session's own data (API) or fall back to agent-cache lookup
    let rowIconHtml = '';
    if (menuCfg.agent_icons && (sAgentIcon || sAgentEngine)) {
      // Keep this avatar aligned with Agents → Sessions at a visible 18px.
      const big = '18px';
      const name = sAgentIcon;
      const engine = sAgentEngine;
      if (engine === 'claude_code' && (!name || name === 'sparkles')) {
        rowIconHtml = claudeMark({ size: big });
      } else if (engine === 'codex' && (!name || name === 'code-2')) {
        rowIconHtml = codexMark({ size: big });
      } else if (!name) {
        rowIconHtml = icon('bot', { size: big });
      } else if (ICON_PICKER_ICONS.includes(name)) {
        rowIconHtml = icon(name, { size: big });
      } else {
        rowIconHtml = `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${name.replace(/</g, '&lt;')}</span>`;
      }
    } else if (menuCfg.agent_icons) {
      // _agentIconHtml renders its supplied base at 1.5×; 12px keeps cached
      // agent icons aligned with the 18px API-backed avatar path above.
      const agentIconHtml = _agentIconHtml(s.agent_id, '12px');
      rowIconHtml = agentIconHtml || _esc(_agentIconFor(s.agent_id));
    }
    // DESIGN SYNC CONTRACT: Keep this dropdown title treatment visually and
    // behaviorally aligned with Agents → Sessions' session title column.
    // Its label-owned ellipsis and compact title-to-Recency spacing are shared
    // acceptance criteria: update them in tandem with sessions-page.js/CSS.
    // The agent avatar is the family disclosure control. Pinned sessions reuse
    // the Sessions-page warning halo rather than adding a separate pin glyph.
    const avatarClasses = 'session-row-agent-icon' + (s.pinned ? ' session-row-agent-warning' : '');
    const avatarHtml = menuCfg.agent_icons
      ? (hasChildren && !s.recycled
        ? `<button type="button" class="session-row-agent-toggle" data-id="${s.id}" title="${isExpanded ? 'Collapse' : 'Expand'} ${s.child_count} subagent${s.child_count === 1 ? '' : 's'}" aria-label="${isExpanded ? 'Collapse' : 'Expand'} ${s.child_count} subagent${s.child_count === 1 ? '' : 's'}" aria-expanded="${isExpanded ? 'true' : 'false'}"><span class="${avatarClasses}">${rowIconHtml}</span></button>`
        : `<span class="session-row-agent-pad"><span class="${avatarClasses}">${rowIconHtml}</span></span>`)
      : '';
    const isRead = _isSessionRead(s);
    let statusHtml = '';
    // Lifetime is sessionAttentionFlag's, not the unread-only sessionAckFlag —
    // the SAME predicate Agents → Sessions uses: a CLEAN completion (complete)
    // drops its chip the moment the user reads it, while any OTHER settled flag
    // (needs_input / partial / unverified / error / interrupted) keeps a NEUTRAL
    // grey chip after being read, so a read-but-still-unfinished session stays
    // visibly unfinished. A grey chip's click puts the session back to unread
    // (restoring the coloured flag), which is why it carries
    // data-needs-input-verb="unread" and the coloured one "read".
    const ackFlag = sessionAttentionFlag(s);
    if (menuCfg.status && ackFlag) {
      // Keep the action-required flag and its activity counter together so the
      // flag does not displace the recency value shown by the other row states.
      const ackLabel = flagLabel(ackFlag);
      const ackRead = sessionFlagIsRead(s);
      const ackTone = ackRead ? 'neutral' : flagTone(ackFlag);
      const ackVerb = ackRead ? 'unread' : 'read';
      // The closer's own short account of the run, shown on the chip's FIRST
      // click (ui/shared/js/needs-input-chip.js) — the same line the pop-up
      // notification carries, so the flag explains itself before it is cleared.
      const ackNote = closingCta(s);
      const ackHint = `${ackLabel} — click to mark ${ackVerb}`;
      const needsInputAt = s.activity_at || s.updated_at;
      const needsInputCounter = needsInputAt
        ? `<span class="session-row-status session-status-idle session-needs-input-counter flag-tone-${ackTone}" title="Last active ${_parseDate(needsInputAt).toLocaleString()}" data-ts="${_esc(needsInputAt)}">${_formatRelativeTime(needsInputAt)}</span>`
        : '';
      statusHtml = `<span class="session-row-status-group"><button type="button" class="session-row-status session-status-needs-input session-needs-input-chip flag-tone-${ackTone}" data-session-id="${_esc(s.id)}" data-state="needs-input" data-flag="${_esc(ackFlag)}" data-needs-input-label="${_escAttr(ackLabel)}" data-needs-input-verb="${ackVerb}" data-needs-input-note="${_escAttr(ackNote)}" title="${_escAttr(ackHint)}" aria-label="${_escAttr(ackHint)}">${_esc(ackLabel)}</button>${needsInputCounter}</span>`;
    } else if (menuCfg.status && (s.run_status === 'running' || s.run_status === 'resuming')) {
      // 'resuming' keeps the spinning loader: the run stopped involuntarily and
      // is waiting for the watchdog/boot sweep to re-ignite it (not finished).
      const _spinTitle = s.run_status === 'resuming'
        ? 'Re-assessing after an interruption\u2026'
        : 'Agent is thinking\u2026';
      statusHtml = `<span class="session-row-status session-status-running" title="${_spinTitle}"><span class="session-radial-loader sm"></span></span>`;
    } else if (menuCfg.status && s.run_status === 'queued') {
      const qPos = Number.isFinite(s.queue_position) ? s.queue_position : null;
      const qTot = Number.isFinite(s.queue_total) ? s.queue_total : null;
      const qLabel = qPos != null
        ? (qTot != null ? `#${qPos} of ${qTot}` : `#${qPos}`)
        : '\u23F3';
      const qTitle = qPos != null
        ? (qTot != null ? `Position ${qPos} of ${qTot} in the session queue` : `Position ${qPos} in the session queue`)
        : 'Waiting in the session queue\u2026';
      statusHtml = `<span class="session-row-status session-status-queued" title="${qTitle}">${qLabel}</span>`;
    } else if (menuCfg.status && ['complete','interrupted','error'].includes(s.run_status) && (s.run_updated_at || s.activity_at || s.updated_at)) {
      // Fall back to last activity when run_updated_at is absent (rows cached
      // before the storage layer preserved it) so the time-ago still renders
      // instead of the unread check hiding it.
      const completedAt = s.run_updated_at || s.activity_at || s.updated_at;
      const _wasComplete = s.run_status === 'complete';
      const statusClass = _wasComplete
        ? 'session-status-completed'
        : (s.run_status === 'error' ? 'session-status-error' : 'session-status-stopped');
      const readClass = isRead ? ' session-status-read' : '';
      const readLabel = isRead ? 'Read · ' : '';
      const resultLabel = _wasComplete ? 'Completed' : (s.run_status === 'error' ? 'Errored' : 'Stopped');
      statusHtml = `<span class="session-row-status ${statusClass}${readClass}" title="${readLabel}${resultLabel} ${_parseDate(completedAt).toLocaleString()}" data-ts="${_esc(completedAt)}">${_formatRelativeTime(completedAt)}</span>`;
    } else if (menuCfg.status && s.has_unread) {
      statusHtml = `<span class="session-row-status session-status-unread" title="New response ready">${icon('check-circle', { size: '12px' })}</span>`;
    } else if (menuCfg.status) {
      // Idle session (no active run): mirror the Sessions page — a muted,
      // live-ticking "time ago" of last activity instead of an empty cell.
      const at = s.activity_at || s.updated_at;
      if (at) {
        statusHtml = `<span class="session-row-status session-status-idle" title="Last active ${_parseDate(at).toLocaleString()}" data-ts="${_esc(at)}">${_formatRelativeTime(at)}</span>`;
      }
    }
    const _isSpawn = typeof s.id === 'string' && s.id.startsWith('spawn-');
    const spawnBadge = _isSpawn
      ? `<span class="session-row-spawn-badge" title="Spawned helper session">${icon('git-branch', { size: '11px' })}</span>`
      : '';
    // Checkbox mode: each row gets a selectable checkbox; kebab/visibility hidden.
    // Recycled rows skip the checkbox (nothing to batch-delete — they're already
    // in the bin) and always show the restore-only bin badge instead.
    const checkboxHtml = menuCfg.batch_delete && _checkboxMode && !s.recycled
      ? `<button class="session-row-checkbox" data-id="${s.id}" data-checked="${_selectedSessions.has(s.id) ? '1' : '0'}">${icon(_selectedSessions.has(s.id) ? 'check-square' : 'square', { size: '14px' })}</button>`
      : '';
    const trailingBtn = s.recycled && menuCfg.recycle_bin
      ? `<span class="session-row-bin" role="button" tabindex="0" data-id="${s.id}" title="In recycling bin — click to restore">bin</span>`
      : (_checkboxMode ? '' : (_showHidden
      ? `<button class="session-row-visibility" title="${s.hidden ? 'Un-hide session' : 'Hide session'}" data-id="${s.id}" data-hidden="${s.hidden ? '1' : '0'}">${icon(s.hidden ? 'eye-off' : 'eye', { size: '14px' })}</button>`
      : (menuCfg.row_actions ? `<button class="session-row-kebab" title="More…" data-id="${s.id}">${icon('more-vertical', { size: '14px' })}</button>` : '')
        // Per-row trash is opt-in (menu.row_delete, default off): delete lives in
        // the row's ⋯ panel so each row has a single, two-click delete affordance.
        + (menuCfg.row_delete ? `<button class="session-row-delete" title="Delete session" data-id="${s.id}" data-state="trash">${icon('trash-2', { size: '14px' })}</button>` : '')));
    // Content-mode hit chip: number of matching messages (see _scanMessagesForQuery).
    const msgHit = _msgHitCounts.has(s.id)
      ? `<span class="session-row-msg-hit" title="${_msgHitCounts.get(s.id)} matching message${_msgHitCounts.get(s.id) === 1 ? '' : 's'}">${_msgHitCounts.get(s.id)}</span>`
      : '';
    row.innerHTML = `
      ${checkboxHtml}
      <span class="session-row-title"${menuCfg.rename ? ' title="Hold to rename"' : ''}>${avatarHtml}${spawnBadge}<span class="session-row-title-label">${label.replace(/</g, '&lt;')}</span></span>
      ${msgHit}
      ${statusHtml}
      ${trailingBtn}
    `;
    menu.appendChild(row);
    // Nested child rows (lazily fetched) directly under an expanded parent.
    // Never for recycled rows — their family is binned too and restore-only.
    if (menuCfg.grouping && isExpanded && !s.recycled) menu.insertAdjacentHTML('beforeend', _childRowsHtml(s.id));
  }
  _appendManageRow(menu);
  _restoreSearchInput(menu, _searchState);

  // The async session list can resolve AFTER the open animation measured the
  // menu (a 0-height empty menu at measure time = invisible "open" state).
  // Re-measure whenever the open menu's content changes so the real list
  // expands the menu instead of staying clipped at 0 (the dropdown artifact).
  _sessionDropdownLoadState = 'idle';
  _refreshOpenMenuHeight(menu);
  _publishSessionDropdownSnapshot();
}

function _mergePortalSessions(sessions, portalSessions = _portalSessionsCache.sessions) {
  const nativeIds = new Set((sessions || []).map(row => row.id));
  return [
    ...(sessions || []),
    ...(portalSessions || []).filter(row => !nativeIds.has(row.id)),
  ];
}

async function _refreshPortalSessions(userId) {
  if (!isAuthenticated() || isAnonGuest()) return [];
  const warm = _portalSessionsCache.userId === userId
    && Date.now() - _portalSessionsCache.loadedAt < _PORTAL_SESSION_CACHE_TTL_MS;
  if (warm) return _portalSessionsCache.sessions;
  if (_portalSessionsRequest?.userId === userId) return _portalSessionsRequest.promise;

  const promise = (async () => {
    const portalUrl = `/api/v1/engines/codex/portal/sessions?user_id=${encodeURIComponent(userId)}`;
    const portalRes = await fetch(apiPath(portalUrl), { headers: authHeaders() });
    // Codex Portal is optional. Servers without that engine expose no route;
    // treat 404 as an empty capability instead of a startup/session-load error.
    if (portalRes.status === 404) {
      _portalSessionsCache = { userId, sessions: [], loadedAt: Date.now() };
      return [];
    }
    if (!portalRes.ok) throw new Error('HTTP ' + portalRes.status);
    const portalData = await portalRes.json();
    const portalSessions = (portalData.sessions || []).map(_normaliseSessionMetadata).filter(Boolean);
    _portalSessionsCache = { userId, sessions: portalSessions, loadedAt: Date.now() };
    return portalSessions;
  })();
  _portalSessionsRequest = { userId, promise };
  try {
    return await promise;
  } finally {
    if (_portalSessionsRequest?.promise === promise) _portalSessionsRequest = null;
  }
}

function _refreshAndRenderPortalSessions(userId) {
  _refreshPortalSessions(userId).then(portalSessions => {
    if (userId !== app.currentUserId || _binMode) return;
    _sessionsCache = _mergePortalSessions(_sessionsCache, portalSessions);
    _sortSessionsByPinnedOrder(_sessionsCache);
    _renderSessionRows();
    _setTriggerLabel();
  }).catch(error => {
    console.warn('Failed to load Codex Portal links:', error);
  });
}

// ── Management footer row ──────────────────────────────────────────────
// A row below the session list with list-wide actions: a search bar (title +
// message content) with a clear button, a recycle-bin toggle, the
// show/hide-hidden eye toggle and a checkbox-batch-delete toggle. Only shown
// when there is at least one session.
function _appendManageRow(menu) {
  if (!_sessionsCache.length) return;
  const cfg = _sessionDropdownConfig.menu;
  const showSearch = cfg.search;
  const showActions = cfg.recycle_bin || cfg.hidden_sessions || cfg.batch_delete;
  if (!showSearch && !showActions) return;
  const bar = document.createElement('div');
  bar.className = 'session-manage-row';
  const eyeTitle = _showHidden ? 'Hide hidden sessions' : 'Show hidden sessions';
  const checkboxTitle = _checkboxMode ? 'Disable checkbox mode' : 'Enable checkbox mode';
  const binTitle = _binMode ? 'Show all sessions' : 'Show recycled sessions (bin)';
  const q = _searchQuery.trim();
  const deleteBtn = cfg.batch_delete && _checkboxMode && _selectedSessions.size > 0
    ? `<button class="session-manage-delete-selected" title="Delete selected (${_selectedSessions.size})" data-state="trash">${icon('trash-2', { size: '15px' })}</button>`
    : '';
  bar.innerHTML = `
    ${showSearch ? `<span class="session-manage-search">
      ${icon('search', { size: '13px' })}
      <input class="session-manage-search-input" type="text" placeholder="Search sessions…" value="${_escAttr(_searchQuery)}" aria-label="Search sessions" spellcheck="false">
      ${q
        ? `<button class="session-manage-search-clear" title="Clear search">${icon('x', { size: '13px' })}</button>`
        : ''}
      <button class="session-manage-flag-filter" title="${_flagFilter ? 'Show all sessions (including idle)' : 'Show only sessions with a flag'}" data-state="${_flagFilter ? 'on' : 'off'}" aria-pressed="${_flagFilter ? 'true' : 'false'}">FLAG</button>
    </span>` : ''}
    ${showActions ? `<span class="session-manage-actions">
      ${deleteBtn}
      ${cfg.recycle_bin ? `<button class="session-manage-bin" title="${binTitle}" data-state="${_binMode ? 'on' : 'off'}">${icon('recycle', { size: '15px' })}</button>` : ''}
      ${cfg.hidden_sessions ? `<button class="session-manage-eye" title="${eyeTitle}" data-state="${_showHidden ? 'on' : 'off'}">${icon(_showHidden ? 'eye' : 'eye-off', { size: '15px' })}</button>` : ''}
      ${cfg.batch_delete ? `<button class="session-manage-checkbox-toggle" title="${checkboxTitle}" data-state="${_checkboxMode ? 'on' : 'off'}">${icon('square', { size: '15px' })}</button>` : ''}
    </span>` : ''}
  `;
  menu.appendChild(bar);
}

// ── Session-list fetch: reliability policy ─────────────────────────────────
// A transient server/replica blip must NEVER be presented as an empty account.
// Treat an empty body cautiously and distinguish exhausted request failures
// from a confirmed successful empty response. Policy:
//   • the response must be res.ok with a real `sessions` array, else it is a failure
//   • an empty body is always double-checked (one quick re-fetch); only two
//     consecutive empty responses are accepted as the truth, so a genuinely
//     empty account converges while a swallowed server error doesn't wipe the list
//   • a COLD cache keeps the skeleton phantom rows visible while retrying
//     (bounded backoff); only after the retry budget is spent do we render an
//     explicit load-error state rather than claiming the account is empty
let _sessionFetchRetryTimer = 0;
let _sessionFetchRetries = 0;
let _sessionEmptyResponses = 0;   // consecutive VALID empty responses
let _sessionIsRetryPopulate = false; // true only for fetches fired by the retry timer
const _SESSION_FETCH_MAX_RETRIES = 3;

function _scheduleSessionFetchRetry() {
  clearTimeout(_sessionFetchRetryTimer);
  if (_sessionFetchRetries >= _SESSION_FETCH_MAX_RETRIES) return;
  const delay = 700 * Math.pow(2, _sessionFetchRetries); // 700ms → 1.4s → 2.8s
  _sessionFetchRetries++;
  _sessionIsRetryPopulate = true;
  _sessionFetchRetryTimer = setTimeout(() => {
    _sessionFetchRetryTimer = 0;
    if (app.currentUserId) populateSessionSelect(app.currentUserId);
  }, delay);
}

function _cancelSessionFetchRetry() {
  clearTimeout(_sessionFetchRetryTimer);
  _sessionFetchRetryTimer = 0;
}

function _renderSessionLoadError(menu) {
  if (!menu) return;
  if (_reactSessionDropdownActive && menu.dataset.reactManaged === 'true') {
    const rendered = document.createElement('div');
    const error = document.createElement('div');
    error.className = 'session-dropdown-empty';
    error.textContent = 'Unable to load sessions';
    rendered.appendChild(error);
    _captureSessionDropdownPresentation(rendered);
    _sessionDropdownLoadState = 'error';
    _publishSessionDropdownSnapshot();
    return;
  }
  delete menu.dataset.loading;
  menu.removeAttribute('aria-busy');
  menu.replaceChildren();
  const error = document.createElement('div');
  error.className = 'session-dropdown-empty';
  error.textContent = 'Unable to load sessions';
  menu.appendChild(error);
}

async function populateSessionSelect(userId, { preferCache = false } = {}) {
  const menu = document.getElementById('session-dropdown-menu');
  if (!userId) {
    _cancelSessionFetchRetry();
    _sessionsCache = [];
    if (menu) menu.innerHTML = '';
    _setTriggerLabel();
    return;
  }
  // Public agent-member guests have a live conversation channel but no
  // account-level session catalog. Never turn that stable capability boundary
  // into an endless /db/sessions retry loop.
  if (window.__agentId && String(userId).startsWith('agentmember--')) {
    _sessionListAccessDenied = true;
    _cancelSessionFetchRetry();
    _ensureCurrentSessionStub();
    _renderSessionRows();
    _setTriggerLabel();
    return;
  }
  const isDefaultScope = !_showHidden && !_binMode && !_searchQuery.trim();
  if (preferCache && isDefaultScope) {
    let snapshot = _canUseActiveSessionCache(userId) ? _activeSessionCache : null;
    if (!snapshot) {
      // Chat boot can reach this module before the generic app_cache has
      // hydrated. Wait only for IndexedDB; the live server read still runs as
      // stale-while-revalidate immediately after this first paint.
      await kvCache.ensureHydrated().catch(() => {});
      snapshot = _readPersistedSessionCache(userId);
    }
    if (snapshot && Array.isArray(snapshot.sessions)) {
      _activeSessionCache = {
        userId,
        sessions: snapshot.sessions.map(s => ({ ...s })),
        // The persisted snapshot may be old enough to require revalidation,
        // but it has just been promoted into the short-lived memory cache.
        // Mark this local read as warm so another opener does not hit IDB again.
        loadedAt: Date.now(),
      };
      _sessionsCache = _mergePortalSessions(
        _activeSessionCache.sessions.map(s => ({ ...s })),
        _portalSessionsCache.userId === userId ? _portalSessionsCache.sessions : [],
      );
      _ensureCurrentSessionStub();
      _sortSessionsByPinnedOrder(_sessionsCache);
      _renderSessionRows();
      _setTriggerLabel();
      _refreshAndRenderPortalSessions(userId);
      return true;
    }
  }
  // An authority denial is stable for this document's identity. Sign-in and
  // sign-out reload the app, so retrying protected session reads on every poll
  // only creates a 401/403 storm and cannot heal in-place.
  if (_sessionListAccessDenied) {
    _ensureCurrentSessionStub();
    _renderSessionRows();
    _setTriggerLabel();
    return;
  }
  const mySeq = ++_sessionFetchSeq;
  // A fetch triggered by the retry timer keeps the current retry budget; any
  // user/poll-initiated fetch starts a fresh one, so opening the dropdown
  // always gets a full skeleton + retry window even after a background burst.
  if (!_sessionIsRetryPopulate) _sessionFetchRetries = 0;
  _sessionIsRetryPopulate = false;
  let sessions = null;
  let failed = false;
  try {
    const useHybridCache = storageAdapter.isBrowser
      || (storageAdapter.isHybrid && isDefaultScope);
    if (useHybridCache) {
      // Hybrid mode: the adapter serves the cached list INSTANTLY from
      // IndexedDB and fires a background refresh — no network wait for the
      // first paint. (Search/bin/hidden fall through to the server fetch
      // below because the cache is default-scope only.)
      const raw = await storageAdapter.listSessions(userId);
      if (!Array.isArray(raw)) throw new Error('Malformed session list');
      sessions = raw.map(s => ({ ...s, recycled: !!s.recycled }));
    } else {
      const token = localStorage.getItem('auth_token');
      const q = _searchQuery.trim();
      const searchActive = q.length > 0;
      // limit=0 asks the server for the FULL session list (no cap), so the
      // dropdown shows every session — matching the Sessions page, which reads
      // the same data from /session-stats with no limit. The server also
      // ignores the row cap while q is set, so title search stays exact.
      let url = `/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(userId)}&limit=0`;
      // include_manifest=0: this list only renders title/agent/status — it
      // never reads manifest fields (authority_revision/content_hash/…), so
      // skip the per-session manifest computation entirely (it was the list's
      // real N+1). Storage/sync consumers keep the default include_manifest=1.
      url += `&include_manifest=0`;
      if (_showHidden) url += `&include_hidden=1`;
      // Bin rows come back when the toggle is on OR a search is active (a name
      // search must be able to surface recycled sessions, like the Sessions
      // page cross-catalog search).
      if (_binMode || searchActive) url += `&include_recycled=1`;
      // Deliberately NOT filtering server-side while content search is on: the
      // server's q= is TITLE-only (db_viewer.list_sessions: s.title LIKE ?), so
      // sending it would shrink _sessionsCache down to the title hits and leave
      // the message-content matches below with no candidate rows to match
      // against — a content-only query would come back empty. The client filter
      // (_sessionRowMatches) matches title + agent name itself, so the server
      // filter is redundant here; limit=0 already returns the full catalog.
      // When the content_search capability is off, keep the server-side narrow
      // (cheaper list, identical title semantics).
      if (searchActive && !_sessionDropdownConfig.menu.content_search) url += `&q=${encodeURIComponent(q)}`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url));
      if (res.status === 401 || res.status === 403) {
        _sessionListAccessDenied = true;
        throw new Error('session access denied');
      }
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const data = await res.json();
      if (!data || !Array.isArray(data.sessions)) throw new Error('Malformed session list');
      sessions = data.sessions.map(_normaliseSessionMetadata).filter(Boolean);
    }
  } catch (e) {
    failed = true;
    console.warn('Failed to load sessions:', e);
  }
  // Portal links are a second authority and intentionally never enter the
  // WebAgent/IndexedDB session caches. Merge the last native snapshot now and
  // refresh it independently so slow Codex discovery cannot lose a race with
  // the much faster WebAgent session poll.
  if (!failed && !_binMode && _portalSessionsCache.userId === userId) {
    sessions = _mergePortalSessions(sessions, _portalSessionsCache.sessions);
  }
  if (!failed && !_binMode) _refreshAndRenderPortalSessions(userId);
  if (mySeq !== _sessionFetchSeq) return;

  // ── Failure path: never clobber; keep what the user can see; retry ──
  if (failed) {
    if (!_sessionListAccessDenied) _scheduleSessionFetchRetry();
    if (_sessionsCache.length) {
      // Warm cache: keep showing the real list (never the stub-only state).
      _renderSessionRows();
    } else if (_sessionListAccessDenied || _sessionFetchRetries >= _SESSION_FETCH_MAX_RETRIES) {
      // A failed request is not evidence of an empty account. Stop the
      // skeleton after the retry budget, but report the load failure honestly.
      _renderSessionLoadError(menu);
    }
    // Cold cache + budget remaining: leave the skeleton phantom rows in place.
    _setTriggerLabel();
    return;
  }

  // ── Empty-but-valid response: always double-check before adopting it ──
  // A search/bin "no matches" result is legitimate and adopted immediately.
  const searchOrBin = !!(_binMode || _searchQuery.trim());
  if (sessions.length === 0) {
    if (searchOrBin) {
      _sessionEmptyResponses = 0;
    } else {
      _sessionEmptyResponses++;
      if (_sessionEmptyResponses < 2) {
        // The first empty could be a swallowed server error. Keep warm rows on
        // screen, or leave the cold-load skeleton in place, and verify once.
        _scheduleSessionFetchRetry();
        if (_sessionsCache.length) _renderSessionRows();
        _setTriggerLabel();
        return;
      }
      // This empty result was confirmed. Reset the counter so a later,
      // unrelated empty response must earn confirmation again.
      _sessionEmptyResponses = 0;
    }
  } else {
    _sessionEmptyResponses = 0;
  }

  _cancelSessionFetchRetry();
  _sessionFetchRetries = 0;
  if (isDefaultScope) {
    primeSessionMetadataCache(
      userId,
      sessions.filter(row => row.external_authority !== 'codex'),
    );
  }
  _sessionsCache = _reconcileSessionReadState(sessions);
  _ensureCurrentSessionStub();
  _sortSessionsByPinnedOrder(_sessionsCache);
  _renderSessionRows();
  _setTriggerLabel();

  // ── Phantom-cell hydration ────────────────────────────────────────────
  // Paint the list immediately (rows already carry server-enriched agent
  // fields when the agent plane is healthy). Then fill any agent display
  // fields this payload didn't carry — cold cache, orphaned, or deleted
  // agents — with ONE batched lean request, and repaint only if something
  // landed. The display cache also warms the header fallbacks (_agentNameFor
  // / _agentIconFor) for the current agent on a cold boot.
  const sessionAgentIds = [...new Set(_sessionsCache.map(s => s.agent_id).filter(Boolean))];
  if (sessionAgentIds.length) {
    hydrateAgentDisplay(sessionAgentIds, userId).then(recs => {
      if (recs && recs.length && mySeq === _sessionFetchSeq) {
        _renderSessionRows();
        _setTriggerLabel();
      }
    });
  }
  // False means this invocation already consulted the selected authority.
  // Callers only need a follow-up revalidation when the earlier fast path
  // returned true after painting memory/IndexedDB directly.
  return false;
}

// The picker always paints the local SQLite mirror first.  Once it is visible,
// check the shared authority for newer *session metadata* only.  The API applies
// changes to SQLite before replying; reload the local list only when something
// actually changed, avoiding flicker and redundant renders on an unchanged open.
async function refreshSessionMetadata(userId) {
  if (!userId) return false;
  if (storageAdapter.isBrowser) return false; // no remote authority
  // Page activation, menu open, and the background poll can converge on this
  // handshake. They share one request per user instead of multiplying the
  // same SQLite/sync work while the server is already under pressure.
  if (_metadataRefreshFlight && _metadataRefreshUserId === userId) {
    return _metadataRefreshFlight;
  }
  _metadataRefreshUserId = userId;
  const flight = (async () => {
    try {
      const token = localStorage.getItem('auth_token');
      let url = `/api/v1/db/sessions/refresh?db=user.db&user_id=${encodeURIComponent(userId)}`;
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'POST', headers: authHeaders() });
      if (!res.ok) return false;
      const data = await res.json();
      if (!data.changed) return false;
      await populateSessionSelect(userId);
      return true;
    } catch (e) {
      console.warn('Failed to refresh session metadata:', e);
      return false;
    }
  })();
  _metadataRefreshFlight = flight;
  try {
    return await flight;
  } finally {
    if (_metadataRefreshFlight === flight) {
      _metadataRefreshFlight = null;
      _metadataRefreshUserId = '';
    }
  }
}

// ── Gate-queue live refresh ────────────────────────────────────────────
// Called from agentWs.js when agent_status: queued fires. Triggers an
// immediate session-list refresh so the dropdown shows the queue position
// without waiting for the next 15s poll.

async function onSessionGateQueue(event) {
  if (!event || !event.session_id) return;
  // If the session is already in our cache, don't wait for a full reload —
  // just re-render with the queued status patched in.
  const found = _sessionsCache.find(s => s.id === event.session_id);
  if (found) {
    found.run_status = 'queued';
    found.run_updated_at = null;
    found.queue_position = Number.isFinite(event.queue_position) ? event.queue_position : null;
    found.queue_total = Number.isFinite(event.queue_total) ? event.queue_total : null;
    _renderSessionRows();
    _setTriggerLabel();
  } else {
    // Session not yet in cache — do a full refresh.
    try { await populateSessionSelect(app.currentUserId); } catch (_) { /* ignore */ }
  }
}

app.onSessionGateQueue = onSessionGateQueue;

function onSessionGateRunning(event) {
  if (!event || !event.session_id) return;
  const found = _sessionsCache.find(s => s.id === event.session_id);
  if (!found) return;
  found.run_status = 'running';
  found.run_updated_at = null;
  found.queue_position = null;
  found.queue_total = null;
  _renderSessionRows();
  _setTriggerLabel();
}

app.onSessionGateRunning = onSessionGateRunning;

// WebSocket subscription supplies this snapshot after reconnect, when the
// original `agent_status: running` broadcast may have occurred while this
// browser was offline. Reuse the live running handler so the header and menu
// retain their standard spinner, then restore the open session's normal
// Working… activity bar without inventing a synthetic tool event.
function onActiveServerSessions(sessionIds) {
  if (!Array.isArray(sessionIds)) return;
  const activeIds = new Set(sessionIds.filter(id => typeof id === 'string' && id));
  if (!activeIds.size) return;

  let missingFromCache = false;
  for (const sessionId of activeIds) {
    if (_sessionsCache.some(s => s.id === sessionId)) {
      onSessionGateRunning({ session_id: sessionId });
    } else {
      missingFromCache = true;
    }
  }

  if (activeIds.has(app.currentSessionId)
      && typeof app.chatActivityRestore === 'function') {
    try { app.chatActivityRestore(null); } catch (_) { /* non-fatal */ }
  }

  // The eager session-list load normally makes this unnecessary, but a cold
  // reconnect can arrive first. One refresh lets the standard row renderer add
  // the spinner as soon as the missing session becomes available.
  if (missingFromCache && app.currentUserId) {
    try { void populateSessionSelect(app.currentUserId); } catch (_) { /* ignore */ }
  }
}

app.onActiveServerSessions = onActiveServerSessions;
// If the WebSocket subscribed before this module mounted, consume its retained
// snapshot now instead of waiting for another reconnect.
try { onActiveServerSessions(app._activeServerSessions); } catch (_) { /* ignore */ }

// Called after a successful force-run: patch the cached session row from
// queued → running so the dropdown reflects it immediately (the 15s poll
// would otherwise be the next update). Clear the queue fields even if the
// cached row lost its run_status, and refresh the trigger label so the
// header's queue badge (e.g. "#1/2") disappears at once, not just the row.

async function onSessionGateForce() {
  const sid = app.currentSessionId;
  if (!sid) return;
  const found = _sessionsCache.find(s => s.id === sid);
  if (found && (found.run_status === 'queued' || found.queue_position != null)) {
    found.run_status = 'running';
    found.run_updated_at = null;
    found.queue_position = null;
    found.queue_total = null;
    _renderSessionRows();
    _setTriggerLabel();
  }
}

app.onSessionGateForce = onSessionGateForce;

// Kill switch: when it toggles, refresh the dropdown immediately so running
// spinners clear (or reappear) without waiting for the 15s poll.
window.addEventListener('kill-switch-changed', () => {
  try { if (app.currentUserId) populateSessionSelect(app.currentUserId); } catch (_) { /* ignore */ }
});

function _applySessionReadState(sessionId, readAt, unreadAt = null) {
  if (!sessionId) return;
  const isUnread = Boolean(unreadAt) || !readAt;
  _sessionReadStateOverrides.set(sessionId, {
    read_at: readAt || null,
    unread_at: unreadAt || null,
    has_unread: isUnread,
  });
  const update = (sessions) => {
    if (!Array.isArray(sessions)) return;
    const session = sessions.find((item) => item.id === sessionId);
    if (!session) return;
    session.read_at = readAt || null;
    session.unread_at = unreadAt || null;
    session.has_unread = isUnread;
  };
  update(_sessionsCache);
  update(_activeSessionCache.sessions);
  if (app.currentUserId) _persistSessionMetadataCache(app.currentUserId, _sessionsCache);
  _renderSessionRows();
  _setTriggerLabel();
}

window.addEventListener('session:visited', (event) => {
  const sessionId = event?.detail?.sessionId;
  if (sessionId) _applySessionReadState(sessionId, event.detail.visitedAt || new Date().toISOString());
});

window.addEventListener('session:unread', (event) => {
  const sessionId = event?.detail?.sessionId;
  if (sessionId) _applySessionReadState(
    sessionId,
    event.detail.readAt || null,
    event.detail.unreadAt || new Date().toISOString(),
  );
});

// Hybrid cache refresh landed (the background re-sync fired by every hybrid
// list read). Repaint from the fresh list so run-status changes — e.g. the
// kill switch stopping every run — clear the spinners the moment the server
// data arrives, instead of waiting for the next 15s poll. Default scope only:
// search/bin/hidden use their own server fetches and must not be clobbered.
// Re-render is safe while the menu is open (rename input is guarded inside
// _renderSessionRows).
window.addEventListener('sessions-delta', (e) => {
  const fresh = e.detail && e.detail.sessions;
  if (!Array.isArray(fresh) || !fresh.length) return;
  if (_searchQuery.trim() || _binMode || _showHidden) return;
  try {
    _sessionsCache = _reconcileSessionReadState(_mergePortalSessions(
      fresh.map(s => ({ ...s, recycled: !!s.recycled })),
      _portalSessionsCache.userId === app.currentUserId ? _portalSessionsCache.sessions : [],
    ));
    _sortSessionsByPinnedOrder(_sessionsCache);
    primeSessionMetadataCache(app.currentUserId, fresh);
    _renderSessionRows();
    _setTriggerLabel();
  } catch (_) { /* ignore */ }
});

// Agent component set changed (presented / completed / deleted) — refresh the
// chip strip immediately so component tabs appear and clear live, instead of
// waiting for the 15s poll. Fired by chat-components.js.
window.addEventListener('chat-components-changed', event => {
  const detail=event.detail || {};
  if(detail.sessionId && detail.sessionId!==app.currentSessionId) return;
  _componentChipsGeneration++;
  if(detail.sessionId && Array.isArray(detail.components)) {
    _componentChipsCache={sid:detail.sessionId,fetchedAt:Date.now(),items:detail.components};
  } else {
    _invalidateComponentChips(app.currentSessionId);
  }
  try { _fetchRelatedSessions(); } catch (_) { /* ignore */ }
});

export {
  _sessionsCache,
  primeSessionMetadataCache,
  hasFreshSessionMetadataCache,
  _sortSessionsByPinnedOrder,
  populateSessionSelect,
  refreshSessionMetadata,
  _renderSessionRows,
  _setTriggerLabel,
  _clearSessionHeader,
  _fetchRelatedSessions,
  applySessionTitle,
  _formatRelativeTime,
  _isSessionRead,
  _truncate,
  _setShowHidden,
  _getShowHidden,
  _setCheckboxMode,
  _getCheckboxMode,
  _selectedSessions,
  _clearSelected,
  toggleSessionGroup,
  _startCompletedTimeTick,
  _setSearchQuery,
  _getSearchQuery,
  _setBinMode,
  _getBinMode,
  _setFlagFilter,
  _getFlagFilter,
  _scanMessagesForQuery,
  _clearMsgSearch,
  sessionDropdownStore,
  _scheduleSearchCommit,
  _cancelPendingSearchCommit,
};

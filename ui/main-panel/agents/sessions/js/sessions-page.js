'use strict';

/**
 * 🤖 AI CODING AGENT — for column layout changes, edit column-config.js, NOT this file.
 *    This file is a GENERIC RENDERER — every column, breakpoint, sort key, and
 *    data source is read from the config objects in column-config.js.
 *
 * Sessions Table panel.
 *
 * Renders a table of all the user's sessions with per-session stats:
 * agent, title, status, message count, input/output tokens, cost, duration.
 * Clicking a row switches the main chat to that session.
 *
 * Lazy init: first call to startSessions() lazily initializes; subsequent
 * calls re-render the data.
 */

import { app } from '../../../../shared/js/state.js';
import { apiPath } from '../../../../shared/js/config.js';
import { authHeaders } from '../../../../shared/js/left-login.js';
import { autoRenameSession, startRename, switchToSession, togglePin } from '../../../../chat/js/session-core.js?v=515';
import { populateSessionSelect, primeSessionMetadataCache, _formatRelativeTime, _isSessionRead } from '../../../../chat/js/session-list.js';
import { _esc, _escAttr, _fmtTime, positionKebabPopup } from '../../../../shared/js/dom-utils.js';
import { icon, claudeMark, codexMark } from '../../../../shared/js/icons.js';
import { ICON_PICKER_ICONS } from '../../../../shared/js/icon-picker.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../../../shared/js/delete-control.js';
import { activateNeedsInputChip } from '../../../../shared/js/needs-input-chip.js';
import { sessionAttentionFlag, sessionFlagIsRead, flagLabel, flagTone, closingCta } from '../../../../shared/js/session-flag.js';
import { onSessionsChanged, onActiveSessionChanged } from '../../../../shared/js/session-events.js';
import { loadViewerConfig, getColumns, getGutterConfig, getDataSources, getBreakpointRules, getHiddenColumns, getStickyTotalWidth, respHideClass } from './column-config.js';
import { hydrateAgentDisplay } from '../../../../shared/js/agent-display-cache.js';
import { kvRead, kvWrite } from '../../../../shared/js/kv-ui-state.js';
import { compareSessionsByPinnedOrder } from '../../../../shared/js/session-ordering.js';
import {
  ensureAgentCacheHydrated,
  readAgentCache,
  writeAgentCache,
} from '../../js/agent-cache.js';
import { initSessionsActivityChart } from './sessions-activity-chart.js';

// ── Helpers ────────────────────────────────────────────────────────

function _qs(id) { return document.getElementById(id); }

function _renderAccessDeniedState() {
  const section = _qs('sessions-section');
  if (section) {
    section.classList.remove('agents-phantom');
    section.dataset.accessBlocked = 'denied';
  }
  const tbody = _qs('sessions-table-body');
  if (tbody) tbody.innerHTML = '';
  const loading = _qs('sessions-loading');
  if (loading) loading.style.display = 'none';
  const spinner = _qs('sessions-loading-spinner');
  if (spinner) spinner.style.display = 'none';
  const empty = _qs('sessions-empty');
  if (empty) {
    const text = empty.querySelector('.sessions-empty-text');
    const hint = empty.querySelector('.sessions-empty-hint');
    if (text) text.textContent = 'Session history is unavailable';
    if (hint) hint.textContent = 'Refresh the page or sign in again to view your sessions';
    empty.style.display = 'flex';
  }
}

// A failed catalog request is not evidence of an empty agent. Keep this distinct
// from the successful-empty state in _renderTable so a weak connection never
// tells someone that their agent has no sessions.
function _renderConnectionFaultState() {
  const tbody = _qs('sessions-table-body');
  if (tbody) tbody.innerHTML = '';
  const loading = _qs('sessions-loading');
  if (loading) loading.style.display = 'none';
  const empty = _qs('sessions-empty');
  if (empty) {
    const text = empty.querySelector('.sessions-empty-text');
    const hint = empty.querySelector('.sessions-empty-hint');
    if (text) text.textContent = 'Couldn’t load sessions';
    if (hint) hint.textContent = 'Check your connection — we’ll keep retrying automatically';
    empty.style.display = 'flex';
  }
  _setLoadStatus('Connection issue — retrying…', false);
}

/**
 * Render an agent icon from inline fields (Lucide name, engine, emoji).
 * Matches _agentIconHtml in session-agent.js.
 */
function _agentIconHtml(iconName, engine, size) {
  const n = parseFloat(size) || 14;
  const unit = String(size).replace(/[\d.]/g, '') || 'px';
  const big = `${n * 1.5}${unit}`;
  if (engine === 'claude_code' && (!iconName || iconName === 'sparkles')) {
    return claudeMark({ size: big });
  }
  if (engine === 'codex' && (!iconName || iconName === 'code-2')) {
    return codexMark({ size: big });
  }
  if (!iconName) return icon('bot', { size: big });
  if (ICON_PICKER_ICONS.includes(iconName)) return icon(iconName, { size: big });
  return `<span style="font-size:${big};line-height:1;display:inline-flex;align-items:center;justify-content:center">${iconName.replace(/</g, '&lt;')}</span>`;
}

function _fmtDuration(ms) {
  if (ms == null || ms === 0) return '—';
  const secs = Math.floor(ms / 1000);
  if (secs < 60) return secs + 's';
  const m = Math.floor(secs / 60);
  const s = secs % 60;
  if (m < 60) return m + 'm ' + s + 's';
  const h = Math.floor(m / 60);
  const mr = m % 60;
  return h + 'h ' + mr + 'm';
}

function _fmtCost(cost) {
  if (cost == null || cost === 0) return '—';
  if (cost < 0.001) return '$' + cost.toFixed(6);
  return '$' + cost.toFixed(4);
}

function _fmtTokens(n) {
  if (n == null || n === 0) return '—';
  if (n < 1000) return String(n);
  return (n / 1000).toFixed(1) + 'K';
}

/** Compact byte size for the footer storage meter (1024-based, 1 decimal). */
function _fmtBytes(n) {
  const v = Number(n);
  if (!isFinite(v) || v < 0) return '—';
  if (v < 1024) return v + ' B';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let val = v / 1024;
  let i = 0;
  while (val >= 1024 && i < units.length - 1) { val /= 1024; i++; }
  return (val >= 100 ? Math.round(val) : val.toFixed(1)) + ' ' + units[i];
}

function _statusBadge(status) {
  if (!status) return '<span class="sess-status unknown">Unknown</span>';
  const cls = status.replace(/\s+/g, '_');
  return `<span class="sess-status ${_esc(cls)}">${_esc(status)}</span>`;
}

// Time-ago tint mirrors the session's run-status chip (same semantic hues):
// complete=green, running=blue, interrupted/error=red, stopped=amber; a session
// with no run status keeps the neutral base grey. Same normalization as
// _statusBadge so the class always matches a chip class when one exists.
function _timeStatusClass(runStatus) {
  if (!runStatus) return '';
  return String(runStatus).replace(/\s+/g, '_');
}

// The page's status chip and the chat session dropdown share ONE predicate:
// `sessionAckFlag` (ui/shared/js/session-flag.js) shows the session's RESOLVED
// flag — the closer's own verdict when the run carries one, else the runtime
// run_status — while the session is unread. Same resolver the notification pill
// uses, so all three surfaces agree.

// Read awareness, mirroring the chat session dropdown. The dropdown normalizes
// activity_at from the row's last_active before calling the shared predicate
// (see session-dropdown/list.js: `activity_at: session.activity_at || session.last_active`);
// the page's rows carry last_active, so supply the same normalization here so
// the read/activity comparison uses the true last-activity timestamp rather
// than falling through to created_at.
function _rowIsRead(row) {
  if (!row) return false;
  return _isSessionRead({ ...row, activity_at: row.activity_at || row.last_active });
}

// ── Live time-ago ticking + click-to-flash-status ──────────────────
// The relative times next to session names are LIVE: a 1s interval re-runs
// _formatRelativeTime on every [data-ts] span so "8m ago" counts up to 9m, 10m,
// … and hours/days roll over while the page stays open. Clicking a time
// flashes the session's run status to its left (e.g. "Completed 8m ago") for
// exactly 1s, then it clears.
const _STATUS_LABELS = {
  complete: 'Completed',
  running: 'Running',
  queued: 'Queued',
  interrupted: 'Interrupted',
  error: 'Error',
  stopped: 'Stopped',
  needs_manual_resume: 'Needs resume',
};

function _statusFlashLabel(status) {
  if (!status) return '';
  if (_STATUS_LABELS[status]) return _STATUS_LABELS[status];
  return status.charAt(0).toUpperCase() + status.slice(1).replace(/_/g, ' ');
}

// One 1s pass: recompute every visible relative time from its data-ts.
function _tickRelativeTimes() {
  document.querySelectorAll('#sessions-table .sess-title-time[data-ts]').forEach(el => {
    el.textContent = _formatRelativeTime(el.dataset.ts);
  });
}

// Show the run-status label left of a clicked time for 1s. Re-clicking while
// visible restarts the timer; a re-render that replaces the node cancels it.
function _flashStatus(timeEl) {
  const wrap = timeEl.closest('.sess-title-time-wrap');
  if (!wrap) return;
  const statusEl = wrap.querySelector('.sess-title-status');
  const label = _statusFlashLabel(timeEl.dataset.status || '');
  if (!statusEl || !label) return;
  if (wrap._flashTimer) clearTimeout(wrap._flashTimer);
  statusEl.classList.remove('flash');
  void statusEl.offsetWidth;   // restart the CSS animation
  statusEl.textContent = label;
  statusEl.classList.add('flash');
  wrap._flashTimer = setTimeout(() => {
    statusEl.classList.remove('flash');
    statusEl.textContent = '';
  }, 1000);
}

// Small icon badges marking sessions that built/edited a genui (has_genui)
// and/or drove the live web browser (has_browser). Both are derived server-side
// from the tool_executions log. Renders an em-dash when neither applies.
// The badges are clickable: a click switches the chat to that session and opens
// the Gen UI or Browser tab so you can jump straight to the artifact
// the session produced. (Handled by the delegated tbody click in _wireDom.)
function _linkBadges(s) {
  const sid = _esc(s.session_id);
  const aid = _esc(s.agent_id || '');
  let html = '';
  if (s.has_genui) {
    html += `<span class="sess-link-badge genui" role="button" tabindex="0"
      data-link-kind="genui" data-session-id="${sid}" data-agent-id="${aid}"
      title="Open this session's genui">
      <i data-lucide="layout-dashboard" style="width:12px;height:12px;"></i></span>`;
  }
  if (s.has_browser) {
    html += `<span class="sess-link-badge browser" role="button" tabindex="0"
      data-link-kind="browser" data-session-id="${sid}" data-agent-id="${aid}"
      title="Open this session in the browser page">
      <i data-lucide="globe" style="width:12px;height:12px;"></i></span>`;
  }
  return html || '<span class="sess-link-none">—</span>';
}

// Which device ran this session (stamped at creation; see app/devices/). Shown
// as a compact badge beside the agent so a multi-device fleet is legible at a
// glance. Rendered only when the session carries a device label.
function _deviceBadge(s) {
  if (!s.device_label) return '';
  return `<span class="sess-device-badge" title="Ran on device: ${_esc(s.device_label)}">
    <i data-lucide="monitor" style="width:11px;height:11px;"></i>${_esc(s.device_label)}</span>`;
}

// ── State & wiring guard ───────────────────────────────────────────

let _initialized = false;
let _sessionsData = [];
let _sessionsDataViewId = '';
let _sessionsDataAgentId = null; // which agent the _sessionsData buffer belongs to (null = account-wide / All Agents)
let _agentFilterId = null; // null = the All Agents catalog
let _nativeCodexContext = null; // agent metadata when its card owns the native Codex catalog
let _refreshTimer = null;
let _sourceAccessDenied = false;
let _tickTimer = null;   // 1s interval that re-renders the live time-ago labels
let _onKsChanged = null; // kill-switch-changed listener while the tab is active
let _agentContextName = 'All Agents';
let _viewId = 'sessions';  // 'sessions' | 'recycle-bin' | 'both' (chip switcher)
let _selectionPanelMode = 'selected';
// Successful recycling removes active rows from the current projection. Keep this
// short-lived action context so Undo/Permanent delete stays available even when
// those binned rows no longer have DOM checkboxes in the active-only view.
let _postBinSessionIds = [];
let _activeConfig = null;
let _searchQuery = '';
let _searchMode = 'content';  // 'title' | 'content' — persisted in IDB-backed UI state
// FLAG chip in the footer (beside the search field): when on, the table keeps
// only rows carrying a status flag, so idle sessions drop out. Shares the
// row-chip predicate `sessionAttentionFlag`, so filter and chips always agree.
let _flagFilterOn = false;
let _lastTheadConfigId = null; // only rebuild thead when config/view actually changes
let _contextLoadToken = 0;   // supersedes a queued context load on a newer click
let _ctxSeq = 0;             // supersedes a paint deferred by a transient null context
let _loadStatusTimer = null;
let _activityChart = null;     // Sessions activity chart instance (mounted above the table)
let _activityChartScope = '';  // scope key the chart is currently showing

function _cacheKey(viewId, agentId, nativeContext) {
  if (nativeContext && agentId) {
    return `sessions:codex:${app.currentUserId || 'anonymous'}:${agentId}`;
  }
  return agentId ? null : `sessions:${viewId}`;
}

function _setLoadStatus(text = '', spinning = false, clearAfter = 0) {
  if (_loadStatusTimer) clearTimeout(_loadStatusTimer);
  _loadStatusTimer = null;
  const spinner = _qs('sessions-loading-spinner');
  const status = _qs('sessions-loading-status');
  if (spinner) spinner.style.display = spinning ? 'inline-block' : 'none';
  if (status) {
    status.textContent = text;
    status.classList.toggle('active', !!text);
  }
  if (clearAfter && text) {
    _loadStatusTimer = setTimeout(() => _setLoadStatus(), clearAfter);
  }
}

function _markSessionsPaint(source) {
  const section = _qs('sessions-section');
  if (!section || typeof performance === 'undefined') return;
  const at = performance.now().toFixed(1);
  section.dataset.lastPaintAt = at;
  section.dataset.lastPaintSource = source;
  if (!section.dataset.firstPaintAt) {
    section.dataset.firstPaintAt = at;
    section.dataset.firstPaintSource = source;
  }
}

// ── Canonical account-wide session snapshot ─────────────────────────
// SESSIONS-SHARED-SNAPSHOT (breadcrumb): All Agents and every agent card read
// the SAME account-wide session list. A selected-agent view is derived from it
// by a local agent_id filter in _renderTable — never by a separate server-scoped
// request — which is what makes an agent click instant. The snapshot is
// refreshed by the 30s background pass and by explicit session mutations; it is
// NEVER narrowed to a single agent's rows.
let _sharedSnapshotViewId = '';
let _sharedSnapshotRows = null;
let _sharedSnapshotAt = 0;
const _SHARED_SNAPSHOT_TTL_MS = 30000;
// Signature of the row set last painted into tbody. A background refresh that
// returns the same signature leaves the resolved table completely untouched.
let _lastRenderedSignature = '';

function _rowsSignature(rows) {
  if (!Array.isArray(rows) || !rows.length) return '';
  let out = '';
  for (const r of rows) {
    if (!r) continue;
    out += (r.session_id || r.id || '') + '\u0001'
      + (r.run_status || '') + '\u0001'
      + (r.message_count == null ? '' : r.message_count) + '\u0001'
      + (r.total_cost == null ? '' : r.total_cost) + '\u0001'
      + (r.title || '') + '\u0002';
  }
  return out;
}

function _writeSharedSnapshot(viewId, rows) {
  if (!viewId || !Array.isArray(rows)) return;
  _sharedSnapshotViewId = viewId;
  _sharedSnapshotRows = rows;
  _sharedSnapshotAt = Date.now();
}

function _readSharedSnapshot(viewId) {
  if (_sharedSnapshotViewId !== viewId || !Array.isArray(_sharedSnapshotRows)) return null;
  return _sharedSnapshotRows;
}

function _sharedSnapshotIsStale(viewId) {
  if (_sharedSnapshotViewId !== viewId || !Array.isArray(_sharedSnapshotRows)) return true;
  return (Date.now() - _sharedSnapshotAt) > _SHARED_SNAPSHOT_TTL_MS;
}

// The account-wide All Agents catalog. The shared _sessionsData buffer can be
// narrowed to the last-loaded context, so the authoritative account-wide set
// lives in (in order) the in-memory snapshot, the buffer when it already holds
// All Agents rows, or the sessions:{view} IDB cache. Read-only — never narrows.
function _accountWideRows() {
  const snapshot = _readSharedSnapshot(_viewId);
  if (snapshot && snapshot.length) return snapshot;
  if (_sessionsDataAgentId === null && _sessionsDataViewId === _viewId) {
    if (Array.isArray(_sessionsData)) return _sessionsData;
  }
  const cached = readAgentCache('sessions:' + _viewId);
  return Array.isArray(cached) ? cached : null;
}

// The agent context to scope rendering to. Set when the shared table is shown
// for an agent card: prefer the module _agentFilterId, but fall back to the
// section's own context stamps so the table is ALWAYS scoped to the displayed
// agent's sessions even if the module state and DOM get out of sync.
function _activeAgentFilter() {
  if (_agentFilterId) return _agentFilterId;
  const section = _qs('sessions-section');
  if (section && section.classList.contains('sessions-agent-filtered')) {
    return section.dataset.agentId || null;
  }
  return null;
}

// Paint the clicked agent's rows from the shared account-wide snapshot on the
// next render frame — strictly AFTER the skeleton has been committed, so the
// switch reads as "skeleton, then this agent's list" without any server wait.
// Falls back silently to the skeleton (and the scoped request in _loadAndRender)
// when no shared data is available yet, e.g. a cold first load.
function _paintAgentFromSharedSnapshot(agentId) {
  let done = false;
  const paint = () => {
    if (done) return;
    done = true;
    if (_activeAgentFilter() !== agentId) return;   // user moved on — never repaint
    if (_nativeCodexContext) return;                // separate dataset
    if (!_activeConfig) return;                     // no columns yet — keep skeleton
    const shared = _accountWideRows();
    if (!Array.isArray(shared)) return;             // cold start — let the loader resolve
    _sessionsData = shared;
    _sessionsDataViewId = _viewId;
    _sessionsDataAgentId = null;                    // the buffer holds the account-wide set
    _renderTable(shared);
    _markSessionsPaint('shared-snapshot');
  };
  // The skeleton is already committed synchronously by the caller, so this only
  // has to swap in the agent's ALREADY-AVAILABLE rows. A double rAF lets the
  // skeleton land on screen for one frame first; the timeout is the fallback for
  // a throttled or background tab, where rAF may never fire at all — without it
  // the switch would stall on the network whenever the tab is not animating.
  if (typeof requestAnimationFrame === 'function') {
    requestAnimationFrame(() => requestAnimationFrame(paint));
  }
  setTimeout(paint, 60);
}

// The Agents page owns one sessions table. The All Agents Sessions tab and an
// opened agent's Sessions tab move that same DOM node into their card so search,
// sorting, selection, and refresh state are preserved.
export function setSessionsAgentContext(agentId = null, container = null, options = {}) {
  // Every context assignment claims a new sequence number; a paint deferred by a
  // transient null context is dropped when a newer assignment arrives.
  const seq = ++_ctxSeq;
  const section = _qs('sessions-section');
  const page = _qs('tab-agents');
  const nextId = agentId || null;
  const nextNativeCodex = nextId && options.nativeCodex ? {
    agentId: nextId,
    agentName: options.agentName || 'Codex',
    agentIcon: options.agentIcon || '',
  } : null;
  if (section) {
    const target = container || page;
    if (target && section.parentElement !== target) target.appendChild(section);
    section.classList.toggle('sessions-agent-filtered', !!nextId);
    section.classList.toggle('sessions-native-codex', !!nextNativeCodex);
    if (nextId) {
      section.dataset.agentId = nextId;
      section.dataset.agentName = options.agentName || 'Agent';
    } else {
      delete section.dataset.agentId;
      delete section.dataset.agentName;
    }
  }
  const changed = _agentFilterId !== nextId || !!_nativeCodexContext !== !!nextNativeCodex;
  _agentFilterId = nextId;
  _agentContextName = nextId ? (options.agentName || 'Agent') : 'All Agents';
  _nativeCodexContext = nextNativeCodex;
  _updateSessionsHeading();
  if (!nextNativeCodex) _setLoadStatus();
  if (changed && nextId) {
    // An agent click is an atomic context change: the title AND the rows switch
    // to the loading skeleton in this same event turn, so the previous view's
    // label and rows can never be read while the new agent's list resolves.
    _renderSkeletonRows();
    // Native Codex catalogs are a separate dataset and must wait for their own
    // request. Every normal agent shares the account-wide SESSIONS-SHARED-SNAPSHOT
    // instead, so its filtered list is painted on the next frame with no network
    // round-trip — that is what makes agent switching instant.
    if (!nextNativeCodex) _paintAgentFromSharedSnapshot(nextId);
  } else if ((changed || container) && _activeConfig) {
    if (!nextId) {
      // A card render clears the context to null and re-assigns the real agent in
      // the SAME turn, so the All Agents paint is deferred by one microtask and
      // dropped if a newer context lands first. Painting it inline would flash the
      // account-wide table (and its "All Agent's Sessions" title) over the agent
      // the user just clicked, destroying the loading skeleton.
      queueMicrotask(() => {
        if (seq !== _ctxSeq) return;      // superseded by a newer context
        if (_agentFilterId) return;       // a real agent context arrived
        const accountWide = _accountWideRows();
        if (Array.isArray(accountWide)) _renderTable(accountWide);
        else _renderSkeletonRows();
      });
    } else if (nextNativeCodex) {
      // Native Codex catalogs are a separate dataset with no shared snapshot.
      if (_sessionsDataAgentId === nextId) _renderTable(_sessionsData);
      else _renderSkeletonRows();
    } else {
      // Re-mounting the shared table for a normal agent re-filters the already
      // available account-wide catalog instead of skeletonizing.
      const accountWide = _accountWideRows();
      if (Array.isArray(accountWide)) _renderTable(accountWide);
      else _renderSkeletonRows();
    }
  }
  // Re-fetch after a context change so an agent card is backed only by its
  // selected agent's catalog. Queue this because card rendering clears the
  // shared context before assigning the selected agent in the same call stack.
  // The token coalesces those two same-tick changes into one load while making
  // sure a LATER user click supersedes an already-queued load instead of being
  // swallowed by it — a swallowed click would strand the view on its skeleton
  // until the next auto-refresh.
  if (changed && _activeConfig) {
    const token = ++_contextLoadToken;
    queueMicrotask(() => {
      if (token !== _contextLoadToken) return;   // superseded by a newer context
      _loadAndRender();
    });
  }
}
// Cross-catalog search cache: while a query is active we lazily fetch the OTHER
// catalog (sessions ↔ recycle bin) so results surface from both views at once.
// Rows carry `_origin` ('active' | 'bin') and `_cross` (true = from the other
// catalog — rendered dimmed with an origin badge, excluded from selection).
let _searchExtra = null;
let _searchExtraPending = null;
// ── Advanced search state (message content) ─────────────────────────
// Message search is a client-side scan: for sessions in the current + other
// catalog we lazily fetch their newest messages (light=1) once per session
// and cache role+content for USER and ASSISTANT rows only — tool calls and
// system messages are excluded by design. Matches render as a badge + snippet.
const _MSG_MIN_QUERY_LEN = 2;   // below this, search is title/agent only
const _MSG_SCAN_MAX = 300;      // most-recently-active sessions scanned
const _MSG_FETCH_LIMIT = 300;   // newest messages fetched per session
const _msgIndex = new Map();    // sessionId -> [{role, content}] (user|assistant)
let _msgScanBusy = null;        // query currently being scanned (latest wins)
// Restore view state from the tenant-scoped IDB cache (with legacy mirror).
let _stored = {};
try {
  const saved = kvRead('agents:sessions-view-state', 'data_viewer_view');
  _stored = typeof saved === 'string' ? JSON.parse(saved || '{}') : (saved || {});
  if (_stored.viewId) _viewId = _stored.viewId;
} catch (_) {}
// The old dropdown also offered an Automations view — that is its own main-panel
// tab now, so migrate any stored value back into the sessions scope.
if (!['sessions', 'recycle-bin', 'both'].includes(_viewId)) _viewId = 'sessions';
_searchMode = _stored.searchMode || 'content';
const _REFRESH_INTERVAL_MS = 30000;
// Which parent rows are currently expanded in the tree. Persists across the
// 30s auto-refresh so an open group stays open. Mirrors the chat session list's
// _expandedGroups (ui/chat/js/session-list.js).
const _expandedGroups = new Set();

// Spawned helper sessions belong beneath their orchestrator's avatar, never in
// the root list. This matches the chat session dropdown's authoritative
// `spawn-*` rule and remains correct even when an old parent was deleted.
function _isChildSpawnSession(session) {
  const id = session && (session.session_id || session.id);
  return typeof id === 'string' && id.startsWith('spawn-');
}

// Narrow title-only viewer: set by _wireSelectionColumnToggle; re-measured by startSessions
// on every tab activation (the tab is display:none until active, so resize
// events alone can't keep the mode correct).
let _narrowUpdater = null;

// ── Sort state ─────────────────────────────────────────────────────
// Default: most recent first (last_active descending).
let _sortColumn = _stored.sortColumn || 'last_active';
let _sortAsc = _stored.sortAsc === true;

// Persist view state to the tenant-scoped IDB cache.
function _saveViewState() {
  kvWrite('agents:sessions-view-state', 'data_viewer_view', {
    viewId: _viewId,
    sortColumn: _sortColumn,
    sortAsc: _sortAsc,
    searchMode: _searchMode,
  });
}

// ── Search-mode button state ─────────────────────────────────────────
// Single toggle: "T+C" (content) ↔ "Title" (title). Title mode gets an
// accent-tinted glass-chip appearance via the .title-mode CSS class.
function _updateSearchModeBtn() {
  const btn = _qs('sessions-search-mode-btn');
  const isTitle = _searchMode === 'title';
  const label = isTitle ? 'Title' : 'T+C';
  const hint = 'Search mode: ' + (isTitle ? 'Title only' : 'Title + Content');
  if (btn) {
    btn.textContent = label;
    btn.dataset.mode = _searchMode;
    btn.title = hint;
    btn.classList.toggle('title-mode', isTitle);
  }
  // The docked footer renders its OWN copy of the mode chip (the shared Sessions
  // fragment can mount inside more than one card), so mirror the label onto every
  // [data-sessions-footer-mode] chip here — the single source of truth for the
  // mode. Without this a freshly rendered footer keeps its static "Title"
  // default while the table is actually filtering in T+C (content) mode.
  document.querySelectorAll('[data-sessions-footer-mode]').forEach((chip) => {
    chip.textContent = label;
    chip.title = hint;
  });
}

function _sessionsHeadingText() {
  const section = _qs('sessions-section');
  const agentId = section?.dataset.agentId || _agentFilterId;
  const agentName = section?.dataset.agentName || _agentContextName;
  return agentId ? `${agentName}'s Sessions` : 'All my sessions';
}

// A loading state outranks the label. While the table is showing skeleton rows
// the heading must render as a shimmer bar whichever caller writes last —
// otherwise a late "All Agent's Sessions" update lands on top of the skeletons
// (card render, container move, and view-state restore all touch the heading).
// _renderTable() clears the flag, so the real label is restored by the FIRST
// resolved render, never by an out-of-order context update.
let _headingLoadPending = false;

function _clearHeadingSkeleton(heading) {
  heading.classList.remove('sessions-heading-skeleton', 'sk-shimmer');
  heading.removeAttribute('aria-busy');
  heading.removeAttribute('aria-label');
}

function _applyHeadingSkeleton(heading) {
  heading.classList.add('sessions-heading-skeleton', 'sk-shimmer');
  heading.setAttribute('aria-busy', 'true');
  heading.setAttribute('aria-label', 'Loading sessions');
  heading.textContent = '';
}

function _updateSessionsHeading() {
  const heading = _qs('sessions-heading');
  if (!heading) return;
  if (_headingLoadPending) { _applyHeadingSkeleton(heading); return; }
  _clearHeadingSkeleton(heading);
  heading.textContent = _sessionsHeadingText();
}

// Selected-agent loads paint the TITLE as a skeleton too. The previous view's
// label ("All Agent's Sessions") must never sit above skeleton rows, and the
// selected agent's real name is only restored once its rows are ready to render.
// The heading element itself becomes the shimmer bar (see sessions.css), so no
// stale text node survives the transition.
function _setHeadingSkeleton(on) {
  _headingLoadPending = !!on;
  const heading = _qs('sessions-heading');
  if (!heading) return;
  if (on) _applyHeadingSkeleton(heading);
  else _updateSessionsHeading();
}

// ── View menu state ────────────────────────────────────────────────
function _updateViewButton() {
  document.querySelectorAll('.sessions-view-option').forEach(btn => {
    const active = btn.dataset.view === _viewId;
    btn.classList.toggle('active', active);
    btn.setAttribute('aria-checked', active ? 'true' : 'false');
  });
}

// ── Sort helper ────────────────────────────────────────────────────
// The sort-key map maps data-sort-key values to the property on the session object.
// Some columns need a type-aware comparator (numbers, strings, dates).
const _SORT_KEY_MAP = {
  agent_name:        { key: 'agent_name',        type: 'string' },
  title:             { key: 'title',             type: 'string' },
  run_status:        { key: 'run_status',        type: 'string' },
  message_count:     { key: 'message_count',     type: 'number' },
  total_input_tokens:  { key: 'total_input_tokens',  type: 'number' },
  total_output_tokens: { key: 'total_output_tokens', type: 'number' },
  total_cost:        { key: 'total_cost',        type: 'number' },
  total_duration_ms: { key: 'total_duration_ms', type: 'number' },
  last_active:       { key: 'last_active',       type: 'date' },
};

function _applySort(sessions) {
  const spec = _SORT_KEY_MAP[_sortColumn] || _SORT_KEY_MAP.last_active;
  const dir = _sortAsc ? 1 : -1;
  // The default "recent" view IS the shared session-list order — pinned rows first,
  // then newest activity — and it is applied through the SAME comparator the chat
  // session picker and the session activity chart above this table rank by (see
  // ui/shared/js/session-ordering.js), so the table and the chart cannot drift.
  // That comparator intentionally blends coarse recency with repeated use, so the
  // first few rows do not reshuffle on every message.
  const defaultRecent = _sortColumn === 'last_active' && !_sortAsc;
  return sessions.slice().sort((a, b) => {
    if (defaultRecent) return compareSessionsByPinnedOrder(a, b);
    // An explicit column sort still floats pinned sessions first — but a binned row
    // loses that pinned ordering status (the pin flag stays set so restore brings it
    // back): bin rows sort purely by the chosen column.
    const ap = !!(a.pinned && a._origin !== 'bin');
    const bp = !!(b.pinned && b._origin !== 'bin');
    if (ap !== bp) return ap ? -1 : 1;
    if (ap && bp) {
      const ao = Number.isFinite(a.sort_order) ? a.sort_order : Number.MAX_SAFE_INTEGER;
      const bo = Number.isFinite(b.sort_order) ? b.sort_order : Number.MAX_SAFE_INTEGER;
      if (ao !== bo) return ao - bo;
    }
    let va = a[spec.key];
    let vb = b[spec.key];
    if (spec.type === 'number') {
      va = (va == null) ? -Infinity : Number(va);
      vb = (vb == null) ? -Infinity : Number(vb);
      return (va - vb) * dir;
    }
    if (spec.type === 'date') {
      va = Date.parse(va || a.created_at || '') || 0;
      vb = Date.parse(vb || b.created_at || '') || 0;
      return (va - vb) * dir;
    }
    // string type
    va = (va || '').toLowerCase();
    vb = (vb || '').toLowerCase();
    return va.localeCompare(vb) * dir;
  });
}

function _updateSortArrows() {
  document.querySelectorAll('#sessions-table thead th.col-sortable').forEach(th => {
    const key = th.dataset.sortKey;
    th.classList.remove('sorted-asc', 'sorted-desc');
    let arrow = th.querySelector('.sort-arrow');
    const arrowHost = th.querySelector('#sessions-heading') || th;
    if (!arrow) {
      arrow = document.createElement('span');
      arrow.className = 'sort-arrow';
      arrowHost.appendChild(arrow);
    }
    if (key === _sortColumn) {
      th.classList.add(_sortAsc ? 'sorted-asc' : 'sorted-desc');
      arrow.textContent = _sortAsc ? '▲' : '▼';
    } else {
      arrow.textContent = '▽';
    }
  });
}

function _setSort(columnKey) {
  if (_sortColumn === columnKey) {
    if (!_sortAsc) {
      _sortAsc = true;     // second click: ascending
    } else {
      _sortColumn = 'last_active'; // third click: remove the explicit sort
      _sortAsc = false;            // and restore default recent activity
    }
  } else {
    _sortColumn = columnKey;
    _sortAsc = false;      // first click: descending
  }
  _saveViewState();
  _updateSortArrows();
  _renderTable(_sessionsData);
}

// ── Session activity chart ─────────────────────────────────────────
// ACTIVITY-CHART (breadcrumb): a per-session token-activity overview sits above
// the sessions table (see sessions/js/sessions-activity-chart.js). The host div
// is created here rather than in agents.html/sessions.html so BOTH copies of the
// sessions markup — the inline one in agents.html and the partial — stay in sync
// automatically. It is a child of #sessions-section, so it moves into an agent
// card with the rest of the page.

/** Return the chart host, creating it directly above the table wrap if needed. */
function _ensureActivityChartHost() {
  const wrap = _qs('sessions-table-wrap');
  if (!wrap || !wrap.parentNode) return null;
  let host = _qs('sessions-activity-chart-host');
  if (!host) {
    host = document.createElement('div');
    host.id = 'sessions-activity-chart-host';
    wrap.parentNode.insertBefore(host, wrap);
  }
  return host;
}

function _wireActivityChart() {
  const host = _ensureActivityChartHost();
  if (!host || _activityChart) return;
  _activityChart = initSessionsActivityChart(host, {
    apiPath,
    headers: () => authHeaders(),
    // Picking a lane is chart-local (it only aims the chart's own stats panel), so the
    // panel's "Open session" link is the one route back out: it hands the lane here and
    // this opens that session in the chat panel, exactly as a row click does.
    onOpen: (sessionId, agentId, title) => { _switchToSession(sessionId, agentId || undefined, { title: title || null }); },
  });
}

/**
 * Point the chart at the context the table is currently showing.
 *
 * The chart reads the account-wide usage ledger, so it follows the same agent
 * scope as the rows around it. It is hidden where it has nothing honest to draw:
 * a recycling-bin view (those sessions are deliberately excluded from the live
 * catalog) and a native Codex catalog (its runs carry no usage_events rows).
 */
function _reloadActivityChart(force = false) {
  const host = _ensureActivityChartHost();
  if (!host) return;
  const hiddenContext = !!_nativeCodexContext || _viewId === 'recycle-bin';
  host.hidden = hiddenContext;
  if (hiddenContext) return;
  if (!_activityChart) return;
  const agentId = _activeAgentFilter() || '';
  const key = agentId + '|' + _viewId;
  if (!force && key === _activityChartScope) return;
  _activityChartScope = key;
  _activityChart.reload({ agentId }, { force });
}

// ── Fetch & render ─────────────────────────────────────────────────

async function _fetchSource(src, agentFilterId) {
  if (_sourceAccessDenied) return [];
  const userId = app.currentUserId;
  if (!userId) return [];
  const token = localStorage.getItem('auth_token');
  let url = apiPath(src.endpoint);
  if (!url.includes('user_id=')) url += (url.includes('?') ? '&' : '?') + 'user_id=' + encodeURIComponent(userId);
  // Agent cards must never request or display another agent's sessions. The
  // server supports this filter; the render-time filter remains as defence in
  // depth for cached data and for a context change while a request is in flight.
  // Use the load-captured filter so the request and the cache key stay in step
  // (never overwrite the account-wide cache with a single agent's rows). When
  // not threaded (e.g. cross-catalog search), fall back to the effective filter.
  const fetchAgentFilter = agentFilterId !== undefined ? agentFilterId : _activeAgentFilter();
  if (fetchAgentFilter && src.row_type === 'session' && !url.includes('agent_id=')) {
    url += '&agent_id=' + encodeURIComponent(fetchAgentFilter);
  }
  if (token && !url.includes('token=')) url += '&token=' + encodeURIComponent(token);
  const res = await fetch(url);
  if (res.status === 401 || res.status === 403) {
    _sourceAccessDenied = true;
    if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
    _renderAccessDeniedState();
    return [];
  }
  if (!res.ok) throw new Error(`${src.row_type} catalog HTTP ${res.status}`);
  const data = await res.json();
  let rows = [];
  if (src.result_shape === 'nested') {
    for (const agent of (data.agents || [])) {
      for (const r of (agent.automations || [])) rows.push({ ...r, _rowType: src.row_type, _agentName: agent.agent_name, _agentId: agent.agent_id, _agentIcon: agent.agent_icon || '', _agentEngine: agent.agent_engine || '' });
      for (const r of (agent.event_subscriptions || [])) rows.push({ ...r, _rowType: src.row_type, _agentName: agent.agent_name, _agentId: agent.agent_id, _agentIcon: agent.agent_icon || '', _agentEngine: agent.agent_engine || '' });
      for (const r of (agent.spawns || [])) rows.push({ ...r, _rowType: src.row_type, _agentName: agent.agent_name, _agentId: agent.agent_id, _agentIcon: agent.agent_icon || '', _agentEngine: agent.agent_engine || '' });
    }
    for (const w of (data.webhooks || [])) rows.push({ ...w, _rowType: src.row_type, _agentName: '', _agentId: '', _agentIcon: '', _agentEngine: '' });
    for (const c of (data.clones || [])) rows.push({ ...c, _rowType: src.row_type, _agentName: c.name || 'Clone', _agentId: c.agent_id || c.id || '', _agentIcon: c.icon || '', _agentEngine: '' });
  } else {
    rows = (data.sessions || []).map(s => ({ ...s, _rowType: src.row_type }));
    // session-stats supplies table-only counters but deliberately omits the
    // dropdown's acknowledgement metadata. Enrich active table rows from that
    // existing endpoint so both session surfaces use the same Needs input flag.
    if (src.row_type === 'session' && src.origin === 'active') {
      try {
        let metadataUrl = apiPath(`/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(userId)}`);
        if (fetchAgentFilter) metadataUrl += `&agent_id=${encodeURIComponent(fetchAgentFilter)}`;
        if (token) metadataUrl += `&token=${encodeURIComponent(token)}`;
        const metadataResponse = await fetch(metadataUrl);
        if (metadataResponse.ok) {
          const metadata = await metadataResponse.json();
          const byId = new Map((metadata.sessions || []).map(session => [session.id, session]));
          rows = rows.map(row => {
            const session = byId.get(row.session_id || row.id);
            return session ? {
              ...row,
              execution_mode: session.execution_mode,
              has_unread: session.has_unread,
              read_at: session.read_at,
              unread_at: session.unread_at,
              // The closer's own verdict + note for the latest run — the same
              // fields the chat dropdown maps, so the page's chip resolves to
              // the identical flag.
              closing_flag: session.closing_flag,
              closing_cta: session.closing_cta,
            } : row;
          });
        }
      } catch (error) {
        // Counters remain usable if the optional presentation metadata is slow.
        console.warn('[Sessions] Failed to load needs-input metadata', error);
      }
      // Publish the now-enriched subset for a fast picker open.
      primeSessionMetadataCache(userId, rows);
    }
  }
  return rows;
}

async function _fetchNativeCodexSessions() {
  const context = _nativeCodexContext;
  const userId = app.currentUserId;
  if (!context || !userId) return [];
  const rows = [];
  let cursor = '';
  const seenCursors = new Set();
  do {
    const qs = new URLSearchParams({
      user_id: userId,
      agent_id: context.agentId,
      limit: '200',
    });
    if (cursor) qs.set('cursor', cursor);
    const res = await fetch(apiPath(`/api/v1/engines/codex/portal/candidates?${qs}`), {
      headers: authHeaders(),
    });
    if (res.status === 401 || res.status === 403) {
      _sourceAccessDenied = true;
      _renderAccessDeniedState();
      return [];
    }
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `Codex task catalog HTTP ${res.status}`);
    // Card renders briefly clear and restore the shared sessions context. A
    // slow catalog response belongs only to the context that launched it.
    if (_nativeCodexContext !== context || app.currentUserId !== userId) return [];
    for (const thread of (data.threads || [])) {
      const threadId = String(thread.thread_id || '').trim();
      if (!threadId) continue;
      const status = typeof thread.status === 'string'
        ? thread.status : (thread.status?.type || thread.status?.status || '');
      rows.push({
        ...thread,
        session_id: thread.id || `codex:${threadId}`,
        agent_id: context.agentId,
        agent_name: context.agentName,
        agent_icon: context.agentIcon,
        agent_engine: 'codex',
        last_active: thread.updated_at || thread.created_at,
        run_status: status,
        external_authority: 'codex',
        _rowType: 'session',
        _origin: 'active',
        _nativeCodexCatalog: true,
      });
    }
    const next = String(data.next_cursor || '').trim();
    if (!next || seenCursors.has(next)) break;
    seenCursors.add(next);
    cursor = next;
  } while (cursor);
  return rows;
}

// session-stats intentionally stays lean for the table's aggregate fields, but
// the Needs input flag also needs the picker-owned acknowledgement metadata.
// Enrich active native rows only; binned and external-catalog rows retain their
// existing semantics and never make this extra request.
async function _enrichActiveSessionMetadata(rows) {
  const activeRows = (rows || []).filter(row => row?._rowType === 'session'
    && row._origin === 'active' && !row._nativeCodexCatalog && row.session_id);
  if (!activeRows.length || !app.currentUserId) return rows;
  try {
    const response = await fetch(apiPath(`/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(app.currentUserId)}`), {
      headers: authHeaders(),
    });
    if (!response.ok) return rows;
    const payload = await response.json();
    const metadataById = new Map((payload.sessions || []).map(session => [session.id, session]));
    return rows.map(row => {
      if (row?._rowType !== 'session' || row._origin !== 'active' || row._nativeCodexCatalog) return row;
      const metadata = metadataById.get(row.session_id);
      return metadata ? {
        ...row,
        execution_mode: metadata.execution_mode,
        has_unread: metadata.has_unread,
        read_at: metadata.read_at,
        unread_at: metadata.unread_at,
        closing_flag: metadata.closing_flag,
        closing_cta: metadata.closing_cta,
      } : row;
    });
  } catch (error) {
    // The stats row is still usable if acknowledgement metadata is temporarily
    // unavailable; the next normal refresh retries the enrichment.
    console.warn('[Sessions] Needs input metadata unavailable', error);
    return rows;
  }
}

async function _fetchForConfig(cfg, agentFilterId) {
  if (!cfg) return [];
  if (_nativeCodexContext) return _fetchNativeCodexSessions();
  const allRows = [];
  for (const src of getDataSources(cfg)) {
    const rows = await _fetchSource(src, agentFilterId);
    // Stamp which catalog each row belongs to ('active' | 'bin') so the
    // merged 'both' view can badge binned rows and keep them non-loadable.
    const origin = src.origin || (src.endpoint.includes('status=recycled') ? 'bin' : 'active');
    for (const r of rows) r._origin = origin;
    allRows.push(...rows);
  }
  return _enrichActiveSessionMetadata(allRows);
}

// ── Cross-catalog search (sessions ↔ recycle bin) ────────────────────
// The search field matches against the CURRENT catalog by default. When a
// query is active we also fetch the other catalog (lazily, once, cached) so
// results surface recycled sessions while in the Sessions view and live
// sessions while in the Recycling bin view. Only session rows cross over —
// automations stay scoped to their own view. The cache is invalidated on
// every load/refresh so it tracks restore/delete changes.
function _invalidateSearchExtra() { _searchExtra = null; _searchExtraPending = null; }

async function _ensureSearchExtra() {
  if (_nativeCodexContext) return [];
  if (_viewId !== 'sessions' && _viewId !== 'recycle-bin') return [];
  if (_searchExtra !== null) return _searchExtra;
  if (_searchExtraPending) return _searchExtraPending;
  const otherId = _viewId === 'recycle-bin' ? 'sessions' : 'recycle-bin';
  const origin = _viewId === 'recycle-bin' ? 'active' : 'bin';
  _searchExtraPending = (async () => {
    try {
      const cfg = await loadViewerConfig(otherId);
      const src = (getDataSources(cfg) || []).find(s => s.row_type === 'session');
      const rows = src ? await _fetchSource(src) : [];
      _searchExtra = rows.map(r => ({ ...r, _origin: origin }));
    } catch (e) {
      console.warn('[Sessions] Failed to load cross-catalog search data:', e);
      _searchExtra = [];
    } finally {
      _searchExtraPending = null;
    }
    return _searchExtra;
  })();
  return _searchExtraPending;
}

// ── Advanced search — message-content matching ──────────────────────
// Toggle the little "searching messages…" spinner inside the search field.
function _setMsgScanning(on) {
  const el = _qs('sessions-msg-scanning');
  if (el) el.classList.toggle('on', !!on);
}

// Fetch ONE session's newest messages (light mode) and cache ONLY the
// user/assistant text rows — tool calls (role 'tool') and system messages
// are excluded here, at the source, so they can never match a query.
async function _fetchSessionMessages(sessionId) {
  if (_msgIndex.has(sessionId)) return _msgIndex.get(sessionId);
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${_MSG_FETCH_LIMIT}&light=1`);
  if (token) url += `&token=${encodeURIComponent(token)}`;
  let store = [];
  try {
    const res = await fetch(url);
    if (res.ok) {
      const data = await res.json();
      store = (data.messages || [])
        .filter(m => (m.role === 'user' || m.role === 'assistant') && m.content && String(m.content).trim())
        .map(m => ({ role: m.role, content: String(m.content) }));
    }
  } catch (e) {
    console.warn('[Sessions] Message fetch failed for', sessionId, e);
  }
  _msgIndex.set(sessionId, store);   // cache even on failure (empty) → no retry loop
  return store;
}

// How many user/assistant messages in this session contain `q` (plus up to 3
// snippets for display). Null when there are no matches.
function _msgMatchInfo(sessionId, q) {
  const idx = _msgIndex.get(sessionId);
  if (!idx || !q) return null;
  const ql = q.toLowerCase();
  let count = 0;
  const snippets = [];
  for (const m of idx) {
    if (m.content.toLowerCase().includes(ql)) {
      count++;
      if (snippets.length < 3) snippets.push(m.content);
    }
  }
  return count > 0 ? { count, snippets } : null;
}

// Build a compact highlighted snippet around the first match of `q` in a
// message's text — escaped, with the hit wrapped in <mark>.
function _msgSnippetHtml(content, q) {
  const ql = q.toLowerCase();
  const low = content.toLowerCase();
  const i = low.indexOf(ql);
  if (i < 0) {
    const cut = content.slice(0, 80);
    return _esc(cut) + (content.length > 80 ? '…' : '');
  }
  const start = Math.max(0, i - 30);
  const end = Math.min(content.length, i + q.length + 70);
  let html = start > 0 ? '…' : '';
  html += _esc(content.slice(start, i));
  html += '<mark>' + _esc(content.slice(i, i + q.length)) + '</mark>';
  html += _esc(content.slice(i + q.length, end));
  if (end < content.length) html += '…';
  return html;
}

// Stamp a row with its message-match info (count + snippet) for the current
// query. Only user/assistant content was indexed, so tool & system rows never
// register here.
function _attachMsgMatches(row, q) {
  if (!q || q.length < _MSG_MIN_QUERY_LEN || row._rowType !== 'session' || !row.session_id) return;
  const info = _msgMatchInfo(row.session_id, q);
  if (info) {
    row._msgMatchCount = info.count;
  }
}

// Prune the message index to sessions that still exist (current catalog +
// cached cross catalog). Called on every load/refresh — keeps memory bounded
// and drops deleted sessions WITHOUT forcing a full re-fetch of untouched
// sessions on the 30s auto-refresh.
function _invalidateMsgIndex() {
  const keep = new Set();
  for (const r of _sessionsData) {
    if (r._rowType === 'session' && r.session_id) keep.add(r.session_id);
  }
  for (const r of (_searchExtra || [])) {
    if (r.session_id) keep.add(r.session_id);
  }
  for (const sid of _msgIndex.keys()) {
    if (!keep.has(sid)) _msgIndex.delete(sid);
  }
}

// Scan both catalogs' session messages for `q` in concurrent batches,
// re-rendering after each batch so matches appear progressively. Results are
// cached per session, so re-typing the same query is instant. A newer query
// supersedes the in-flight one (checked at batch boundaries).
async function _scanMessagesForQuery(q) {
  if (!q || q.length < _MSG_MIN_QUERY_LEN) return;
  if (_searchMode === 'title') return; // title-only mode — no message scan
  if (_viewId !== 'sessions' && _viewId !== 'recycle-bin' && _viewId !== 'both') return;
  if (_msgScanBusy === q) return;      // already scanning this query
  _msgScanBusy = q;                    // newer query supersedes an in-flight one
  try {
    const extra = await _ensureSearchExtra();
    const sessionRows = [];
    const seen = new Set();
    for (const r of [..._sessionsData, ...(extra || [])]) {
      if (r._rowType !== 'session' || !r.session_id) continue;
      if (seen.has(r.session_id)) continue;
      seen.add(r.session_id);
      if ((r.message_count || 0) > 0) sessionRows.push(r);
    }
    sessionRows.sort((a, b) => {
      const ta = Date.parse(a.last_active || a.updated_at || a.created_at || '') || 0;
      const tb = Date.parse(b.last_active || b.updated_at || b.created_at || '') || 0;
      return tb - ta;
    });
    const todo = sessionRows.slice(0, _MSG_SCAN_MAX).filter(r => !_msgIndex.has(r.session_id));
    if (todo.length === 0) return;     // everything already indexed — nothing to fetch
    _setMsgScanning(true);
    const CHUNK = 8;
    for (let i = 0; i < todo.length; i += CHUNK) {
      // Stop early if the query changed or was cleared while fetching.
      if (_msgScanBusy !== q || _searchQuery.trim() !== q) return;
      await Promise.all(todo.slice(i, i + CHUNK).map(r => _fetchSessionMessages(r.session_id)));
      if (_msgScanBusy === q && _searchQuery.trim() === q) _renderTable(_sessionsData);
    }
  } catch (e) {
    console.warn('[Sessions] Message search failed:', e);
  } finally {
    if (_msgScanBusy === q) {
      _msgScanBusy = null;
      _setMsgScanning(false);
    }
    if (_searchQuery.trim() === q) _renderTable(_sessionsData);
  }
}

function _buildThead(thead, cfg) {
  const hidden = getHiddenColumns(cfg, _sessionsViewWidth());
  const viewKey = _viewId + '|' + cfg.id;
  if (_lastTheadConfigId === viewKey) {
    // Same config — preserve the stateful header DOM, but update its context
    // label so a selected agent never retains the All Agents heading.
    _updateSessionsHeading();
    _updateResponsiveColumns();
    return;
  }
  const headerActions = _qs('sessions-header-actions');
  const staging = _qs('sessions-toolbar');
  // A view change rebuilds <thead>; keep the stateful controls staged so their
  // listeners and current search text survive without appearing in the title cell.
  if (headerActions && staging) staging.appendChild(headerActions);
  _lastTheadConfigId = viewKey;
  let html = '<tr>';
  for (const col of getColumns(cfg)) {
    const align = col.align ? 'text-align:' + col.align + ';' : '';
    const width = 'width:' + col.width + 'px;min-width:48px;';
    const sortable = col.sort_key ? ' col-sortable' : '';
    const resizable = ' col-resizable';
    const sortKey = col.sort_key ? ' data-sort-key="' + col.sort_key + '"' : '';
    const respHide = hidden.has(col.id) ? ' ' + respHideClass(_breakpointForCol(col, cfg)) : '';
    const label = col.id === 'check' ? '' : col.id.charAt(0).toUpperCase() + col.id.slice(1).replace('_', ' ');
    const content = col.id === 'title'
      ? '<div class="sessions-title-heading"><span id="sessions-heading"></span></div>'
      : label;
    html += '<th class="col-' + col.id + sortable + resizable + respHide + '"' + sortKey + ' style="' + width + align + '">' + content + '</th>';
  }
  html += '</tr>';
  thead.innerHTML = html;
  // Keep stateful search and view controls in their hidden staging area. The
  // sessions footer exposes those actions; the title cell intentionally shows
  // only the context heading.
  _updateSessionsHeading();
  _updateViewButton();
  _applyColWidths(); // re-apply saved widths after rebuild
}

function _breakpointForCol(col, cfg) {
  for (const rule of getBreakpointRules(cfg)) {
    if ((rule.hide || []).includes(col.id)) return rule.max_width;
  }
  return 9999;
}

function _sessionsViewWidth() {
  const wrap = document.getElementById('sessions-table-wrap');
  return wrap && wrap.clientWidth > 0 ? wrap.clientWidth : window.innerWidth;
}

function _updateResponsiveColumns() {
  if (!_activeConfig) return;
  // Sessions can be narrowed independently of the browser by the split-panel
  // drag handle. Use the table's own visible width so column breakpoints track
  // the view the user actually sees, not window.innerWidth.
  const hidden = getHiddenColumns(_activeConfig, _sessionsViewWidth());
  const allBreakpoints = getBreakpointRules(_activeConfig).map(function(r) { return r.max_width; });
  document.querySelectorAll('#sessions-table th, #sessions-table td').forEach(function(el) {
    for (const bp of allBreakpoints) el.classList.remove(respHideClass(bp));
    for (const colId of hidden) {
      if (el.classList.contains('col-' + colId)) {
        const bp = _breakpointForCol({ id: colId }, _activeConfig);
        el.classList.add(respHideClass(bp));
      }
    }
  });
}

function _renderTable(rows) {
  const tbody = _qs('sessions-table-body');
  const empty = _qs('sessions-empty');
  const loading = _qs('sessions-loading');
  const thead = _qs('sessions-table-head');
  if (!tbody) return;
  // A resolved render ends the loading state, so the heading label may be
  // written again (see _updateSessionsHeading / _headingLoadPending).
  _headingLoadPending = false;
  if (loading) loading.style.display = 'none';
  // Only rebuild thead on view change — _buildThead tracks _lastTheadConfigId
  // internally. After the call we always update responsive + sort arrows.
  if (thead && _activeConfig) {
    _buildThead(thead, _activeConfig);
    _updateSessionsHeading();
    _updateResponsiveColumns();
    _updateSortArrows();
  }
  const table = _qs('sessions-table');
  if (table) table.classList.toggle('bin-view', !_nativeCodexContext && _viewId === 'recycle-bin');
  const q = _searchQuery.trim().toLowerCase();
  let candidates = rows;
  if (q) {
    // Cross-catalog search: merge the OTHER catalog (sessions ↔ recycle bin)
    // into the pool so one query matches both. Rows keep an `_origin` tag
    // ('active' | 'bin'); `_cross` marks rows from the other catalog, which
    // render dimmed with an origin badge and are excluded from selection.
    const viewOrigin = _viewId === 'recycle-bin' ? 'bin' : 'active';
    const pool = [];
    const seen = new Set();
    const keyOf = (r) => (r._rowType === 'session' ? 's:' + (r.session_id || '') : 'a:' + (r.id || ''));
    for (const r of rows) {
      const k = keyOf(r);
      if (k && seen.has(k)) continue;
      if (k) seen.add(k);
      const row = Object.assign({}, r, { _origin: r._origin || viewOrigin, _cross: false });
      if (_searchMode !== 'title') _attachMsgMatches(row, q);
      pool.push(row);
    }
    for (const r of (_searchExtra || [])) {
      if (r._origin === viewOrigin) continue;
      const k = keyOf(r);
      if (k && seen.has(k)) continue;
      if (k) seen.add(k);
      const row = Object.assign({}, r, { _cross: true });
      if (_searchMode !== 'title') _attachMsgMatches(row, q);
      pool.push(row);
    }
    candidates = pool.filter(r => {
      const title = (r.title || r.label || r.name || '').toLowerCase();
      const agent = (r.agent_name || r._agentName || '').toLowerCase();
      return title.includes(q) || agent.includes(q) || (_searchMode !== 'title' && (r._msgMatchCount || 0) > 0);
    });
  }
  const filterAgentId = _activeAgentFilter();
  if (filterAgentId) {
    // A selected-agent view shows ONLY that agent's rows. The account-wide
    // snapshot backs every context, so this local filter is the whole of the
    // agent scoping — rows without that agent stamp are excluded rather than
    // leaking the account-wide list into an agent card.
    candidates = candidates.filter(r => r.agent_id === filterAgentId);
  }
  if (_flagFilterOn) {
    // FLAG chip: keep only session rows that carry a chip — the very predicate
    // the row chip renders from (`_renderRow`'s ackFlag), so an idle session
    // always leaves the list and a flagged one never does.
    candidates = candidates.filter(r => r._rowType === 'session' && !!sessionAttentionFlag(r));
  }
  if (!candidates || candidates.length === 0) {
    tbody.innerHTML = '';
    if (empty) {
      const txt = empty.querySelector('.sessions-empty-text');
      const hint = empty.querySelector('.sessions-empty-hint');
      const label = _activeConfig ? _activeConfig.label : 'items';
      if (q) {
        if (txt) txt.textContent = 'No matching rows';
        if (hint) hint.textContent = 'Try a different search term';
      } else if (_flagFilterOn) {
        if (txt) txt.textContent = 'No flagged sessions';
        if (hint) hint.textContent = 'Show all sessions to see the idle ones';
      } else {
        if (txt) txt.textContent = _nativeCodexContext ? 'No native Codex sessions' : (_activeAgentFilter() ? 'No sessions for this agent' : (_viewId === 'recycle-bin' ? 'Recycling bin is empty' : (_viewId === 'both' ? 'No sessions yet' : 'No ' + label.toLowerCase() + ' yet')));
        if (hint) hint.textContent = _nativeCodexContext ? 'New Codex tasks will appear here automatically' : (_activeAgentFilter() ? 'Start a conversation with this agent to see it here' : (_viewId === 'recycle-bin' ? 'Recycled items appear here until permanently deleted' : (_viewId === 'both' ? 'Active and recycled sessions will appear here' : 'Nothing to show in ' + label)));
      }
      empty.style.display = 'flex';
    }
    return;
  }
  if (empty) empty.style.display = 'none';
  // Remember exactly what was painted so the 30s background refresh can detect a
  // no-op and leave the resolved table (and its scroll position) untouched.
  _lastRenderedSignature = _rowsSignature(rows);
  // All views show session rows only (Automations is its own tab). In the
  // merged 'both' view the config fetches BOTH catalogs, so every row already
  // carries its `_origin` ('active' | 'bin') — sorting mixes them freely.
  let filtered = _applySort(candidates.filter(r => r._rowType === 'session'));
  const byId = new Map(filtered.filter(r => r._rowType === 'session' && !r._cross).map(s => [s.session_id, s]));
  const childrenOf = new Map();
  const topLevel = [];
  for (const s of filtered) {
    // Cross-catalog rows are always shown flat — they don't participate in the
    // current view's parent/child tree (and are never expandable).
    if (s._rowType !== 'session' || s._cross) { topLevel.push(s); continue; }
    const pid = s.parent_session_id;
    if (pid && byId.has(pid)) {
      if (!childrenOf.has(pid)) childrenOf.set(pid, []);
      childrenOf.get(pid).push(s);
    } else if (!_isChildSpawnSession(s)) {
      // An orphaned spawn has no live parent row to expand from. Keep it out of
      // the root projection instead of incorrectly presenting it as a main agent.
      topLevel.push(s);
    }
  }
  let html = '';
  for (const row of topLevel) html += _renderRow(row, { childrenOf, depth: 0 });
  tbody.innerHTML = html;
  if (window.lucide && typeof window.lucide.createIcons === 'function') window.lucide.createIcons({ nodes: [tbody] });
  _updateSortArrows();
  _updateResponsiveColumns();
}

// Build one table row. `childrenOf` (the tree map) drives recursive rendering
// for parents — each parent with children that are expanded renders its child
// rows inline beneath itself. `depth` tracks the nesting level (0 = top-level,
// 1 = child, 2 = grandchild) for visual indentation.
function _renderRow(row, ctx) {
  if (!ctx) ctx = {};
  var childrenOf = ctx.childrenOf || null;
  var depth = ctx.depth || 0;
  if (!_activeConfig) return '';
  var columns = getColumns(_activeConfig);
  var isSession = row._rowType === 'session';
  var isAuto = row._rowType === 'automation';
  var cells = '';
  for (var i = 0; i < columns.length; i++) {
    var col = columns[i];
    if (col.for && !col.for.includes(row._rowType)) {
      cells += '<td class="col-' + col.id + '"><span style="color:var(--fg-4)">—</span></td>';
      continue;
    }
    cells += _renderCell(row, col, { childrenOf: childrenOf, depth: depth, isSession: isSession, isAuto: isAuto });
  }
  var sid = isSession ? _esc(row.session_id) : '';
  var rowId = isSession ? sid : (row.id || '');
  var rowCls = isSession ? _sessionRowClass(row, childrenOf, depth) : '';
  // data-origin stamps which catalog a row lives in ('active' | 'bin') so the
  // click handler can refuse to load recycled rows even when a cross-catalog
  // search surfaced them in the Sessions view. Cross rows get a dimmed class.
  var origin = row._origin || (_viewId === 'recycle-bin' ? 'bin' : 'active');
  var cls = (rowCls || '') + (row._cross ? ' sess-cross-row' : '');
  var html = '<tr data-session-id="' + sid + '" data-id="' + _esc(rowId) + '" data-agent-id="' + _esc(row.agent_id || row._agentId || '') + '" data-row-type="' + row._rowType + '" data-origin="' + _esc(origin) + '"' + (cls ? ' class="' + cls.trim() + '"' : '') + '>' + cells + '</tr>';
  // An expanded family renders its members immediately below the parent. The
  // tree map is built in _renderTable; without this recursion the toggle state
  // changed but no child rows ever reached the DOM.
  if (isSession && childrenOf && _expandedGroups.has(row.session_id)) {
    var children = childrenOf.get(row.session_id) || [];
    for (var j = 0; j < children.length; j++) {
      html += _renderRow(children[j], { childrenOf: childrenOf, depth: depth + 1 });
    }
  }
  return html;
}

function _sessionRowClass(s, childrenOf, depth) {
  var kids = childrenOf ? (childrenOf.get(s.session_id) || []) : [];
  var childCount = kids.length;
  var activeChat = s.session_id === app.currentSessionId ? ' active-chat' : '';
  if (depth > 0) {
    return 'sessions-child-row' + (childCount > 0 ? ' group-parent' + (_expandedGroups.has(s.session_id) ? ' expanded' : '') : '') + activeChat;
  }
  return (childCount > 0 ? 'group-parent' + (_expandedGroups.has(s.session_id) ? ' expanded' : '') : '') + activeChat;
}

function _renderCell(row, col, ctx) {
  const childrenOf = ctx.childrenOf;
  const depth = ctx.depth;
  const isSession = ctx.isSession;
  const isAuto = ctx.isAuto;
  switch (col.id) {
    case 'check': return _renderCheckCell(row, isSession);
    case 'agent': return _renderAgentCell(row, isSession, isAuto);
    case 'title': case 'name': return _renderTitleCell(row, col.id, depth, isSession, isAuto, childrenOf);
    case 'status': return _renderStatusCell(row, isSession, isAuto);
    case 'type': return _renderTypeCell(row, isAuto);
    case 'trigger': return _renderTriggerCell(row, isAuto);
    case 'output': return _renderOutputCell(row, isAuto);
    case 'links': return _renderLinksCell(row, isSession);
    case 'msgs': return '<td class="col-msgs" style="text-align:right;">' + (row.message_count || '—') + '</td>';
    case 'tokens_in': return '<td class="col-tokens_in" style="text-align:right;">' + _fmtTokens(row.total_input_tokens) + '</td>';
    case 'tokens_out': return '<td class="col-tokens_out" style="text-align:right;">' + _fmtTokens(row.total_output_tokens) + '</td>';
    case 'cost': return '<td class="col-cost" style="text-align:right;">' + _fmtCost(row.total_cost) + '</td>';
    case 'duration': return '<td class="col-duration" style="text-align:right;">' + _fmtDuration(row.total_duration_ms) + '</td>';
    case 'last': return '<td class="col-last">' + _fmtTime(row.last_run_at || row.last_event_at || row.heartbeat_at || row.last_triggered_at) + '</td>';
    case 'next': return '<td class="col-next">' + _fmtTime(row.next_run_at) + '</td>';
    case 'count': return '<td class="col-count" style="text-align:right;">' + (row.run_count || row.fire_count || '—') + '</td>';
    case 'device': return _renderDeviceCell(row, isAuto);
    case 'enabled': return _renderEnabledCell(row, isAuto);
    case 'updated': return '<td class="col-updated" style="text-align:right;">' + _fmtTime(row.last_active) + '</td>';
    default: return '<td class="col-' + col.id + '">—</td>';
  }
}

function _renderCheckCell(row, isSession) {
  // Cross-catalog rows can't be selected — bulk actions (delete/restore) must
  // stay scoped to the currently viewed catalog.
  if (row._cross || row._nativeCodexCatalog) return '<td class="col-check"></td>';
  const id = isSession ? row.session_id : row.id;
  return '<td class="col-check"><span class="sessions-check-cell" data-session-id="' + _esc(id || '') + '"> </span></td>';
}

function _renderAgentCell(row, isSession, isAuto) {
  const iconName = isSession ? (row.agent_icon || '') : (row._agentIcon || '');
  const engine = isSession ? (row.agent_engine || '') : (row._agentEngine || '');
  const agentLabel = isSession ? (row.agent_name || row.agent_id || '—') : (row._agentName || row._agentId || '—');
  let deviceBadge = '';
  if (isSession && row.device_label) deviceBadge = '<span class="sess-device-badge" title="Ran on device: ' + _esc(row.device_label) + '"><i data-lucide="monitor" style="width:11px;height:11px;"></i>' + _esc(row.device_label) + '</span>';
  return '<td class="col-agent"><span class="sess-agent-badge">' + _agentIconHtml(iconName, engine, '12px') + _esc(agentLabel) + '</span>' + deviceBadge + '</td>';
}

function _renderTitleCell(row, colId, depth, isSession, isAuto, childrenOf) {
  const titleText = isAuto ? _esc(row.label || row.name || '—') : _esc(row.title);
  // DESIGN SYNC CONTRACT: Keep this Sessions title column visually and
  // behaviorally aligned with the chat session dropdown's title treatment.
  // Reuse its title-label primitive so ellipsis truncation, compact title-to-
  // recency spacing, icon alignment, and typography stay identical across both
  // session surfaces. Change these details in tandem with session-dropdown/list.js.
  const rowClasses = 'session-row sess-title-row';
  let inner = '<span class="session-row-title sess-title-text"><span class="session-row-title-label">' + titleText + '</span></span>';
  if (isSession) {
    // The avatar is the family control: parent sessions expand inline beneath
    // their title, while ordinary session avatars remain passive identity marks.
    const kids = childrenOf ? (childrenOf.get(row.session_id) || []) : [];
    const childCount = kids.length;
    const avatarClasses = 'session-row-agent-icon sess-title-agent-slot' + (row.pinned ? ' sess-title-agent-warning' : '');
    if (childCount > 0 && !row._cross) {
      const open = _expandedGroups.has(row.session_id);
      inner = '<button type="button" class="' + avatarClasses + ' sess-title-agent-toggle" data-session-id="' + _esc(row.session_id) + '" title="' + (open ? 'Collapse' : 'Expand') + ' ' + childCount + ' subagent' + (childCount === 1 ? '' : 's') + '" aria-label="' + (open ? 'Collapse' : 'Expand') + ' ' + childCount + ' subagent' + (childCount === 1 ? '' : 's') + '" aria-expanded="' + (open ? 'true' : 'false') + '">' + _agentIconHtml(row.agent_icon, row.agent_engine, '12px') + '</button>' + inner;
    } else {
      inner = '<span class="' + avatarClasses + '">' + _agentIconHtml(row.agent_icon, row.agent_engine, '12px') + '</span>' + inner;
    }
  }
  if (depth > 0 && isSession) {
    const roleIcon = row.child_role === 'closer' ? 'flag' : row.child_role === 'planner' ? 'wand-2' : 'git-branch';
    // Child rows are identified by their role icon and agent avatar only; no
    // connector rail is rendered ahead of the fork icon.
    inner = '<i data-lucide="' + roleIcon + '" class="sess-child-role-icon" style="width:12px;height:12px;"></i>' + inner;
  }
  // Shared with the chat dropdown: the acknowledgement chip shows the session's
  // RESOLVED flag (the closer's own verdict, else the runtime status) and sits
  // before the relative-time counter so the flag and counter stay adjacent.
  //
  // Lifetime is sessionAttentionFlag's, not the unread-only sessionAckFlag: a
  // CLEAN completion (complete) drops its chip the moment the user reads it, but
  // any OTHER settled flag — needs_input / partial / unverified / error /
  // interrupted — keeps a NEUTRAL grey chip after being read, so a read-but-still-
  // unfinished session stays visibly unfinished. Clicking a grey chip marks the
  // session unread again (restoring the coloured flag), which is why the read chip
  // carries data-needs-input-verb="unread" and the coloured one "read".
  const ackFlag = isSession ? sessionAttentionFlag(row) : '';
  const ackRead = ackFlag ? sessionFlagIsRead(row) : false;
  if (ackFlag) {
    const ackLabel = _escAttr(flagLabel(ackFlag));
    const ackTone = ackRead ? 'neutral' : flagTone(ackFlag);
    const ackVerb = ackRead ? 'unread' : 'read';
    // The closer's own short account of the run, shown on the chip's FIRST click
    // (ui/shared/js/needs-input-chip.js) — the same line the pop-up carries.
    const ackNote = _escAttr(closingCta(row));
    const ackHint = ackLabel + ' — click to mark ' + ackVerb;
    inner += '<button type="button" class="session-row-status session-status-needs-input session-needs-input-chip flag-tone-' + _esc(ackTone) + '" data-session-id="' + _esc(row.session_id) + '" data-state="needs-input" data-flag="' + _esc(ackFlag) + '" data-needs-input-verb="' + ackVerb + '" data-needs-input-label="' + ackLabel + '" data-needs-input-note="' + ackNote + '" title="' + ackHint + '" aria-label="' + ackHint + '">' + ackLabel + '</button>';
  }
  // Relative "time ago" to the right of the title — same format as the chat
  // session dropdown (s/m/h/d/w ago, then a short date once old enough). It
  // TICKS live (a 1s interval re-renders every [data-ts] span in place) and is
  // clickable: clicking flashes the run-status label to its left for 1s
  // (e.g. "Completed 8m ago"). Running sessions swap the timer for the same
  // radial loader the chat session dropdown shows.
  if (isSession && row.last_active) {
    const timeCls = _timeStatusClass(row.run_status);
    // 'resuming' = involuntarily stopped but still inside the auto-resume ladder,
    // so the session is in progress, not finished — same radial loader.
    const running = row.run_status === 'running' || row.run_status === 'resuming';
    // Mirror the chat session dropdown's read awareness: a finished run
    // (complete/interrupted/error) the user has already read keeps its
    // timestamp but drops the green/red attention hue for a muted grey. A row
    // showing the acknowledgement flag keeps its counter and is never muted.
    const finished = ['complete', 'interrupted', 'error'].includes(row.run_status);
    const readCls = finished && !ackFlag && _rowIsRead(row) ? ' read' : '';
    // The flag cluster is ONE colour: the counter beside the acknowledgement chip
    // takes the chip's own tone (its resolved flag, or neutral once read) instead
    // of always reading amber — a complete flag counts down green, an error flag
    // red. Rules: `.sess-title-time-wrap.needs-input-counter.flag-tone-*`.
    const counterTone = ackFlag ? ' flag-tone-' + _esc(ackRead ? 'neutral' : flagTone(ackFlag)) : '';
    const timeHtml = running
      ? '<span class="session-row-time sess-title-time" title="' + (row.run_status === 'resuming' ? 'Re-assessing after an interruption…' : 'Agent is thinking…') + '"><span class="session-radial-loader sm session-status-running"></span></span>'
      : '<span class="session-row-time sess-title-time" role="button" tabindex="0" data-ts="' + _esc(row.last_active) + '" data-status="' + _esc(row.run_status || '') + '" title="' + _esc(row.last_active) + ' — click for status">' + _formatRelativeTime(row.last_active) + '</span>';
    inner += '<span class="sess-title-time-wrap' + (timeCls ? ' ' + timeCls : '') + readCls + (ackFlag ? ' needs-input-counter' : '') + counterTone + '">' +
      '<span class="sess-title-status" aria-hidden="true"></span>' + timeHtml + '</span>';
  }
  // Origin badge — which catalog a row came from. Cross-catalog search results
  // always carry one, so a match from the other view is unmistakable. In the
  // merged 'both' view binned rows get the same badge so the combined table
  // stays legible. The "bin" badge doubles as a two-click restore control
  // (see _onBinChipClick).
  if (row._cross || (row._origin === 'bin' && _viewId === 'both')) {
    if (row._origin === 'bin') {
      inner += '<span class="sess-origin-badge bin" role="button" tabindex="0" data-session-id="' + _esc(row.session_id) + '" title="In recycling bin — click to restore">bin</span>';
    } else {
      inner += '<span class="sess-origin-badge" title="This session is active">Active</span>';
    }
  }
  // Advanced search: show a small round chip with the number of matching messages.
  if (row._msgMatchCount > 0) {
    inner += '<span class="sess-msg-count-chip" title="' + row._msgMatchCount + ' matching message' + (row._msgMatchCount > 1 ? 's' : '') + '">' + row._msgMatchCount + '</span>';
  }
  // Same per-row control as the chat session dropdown: the ⋯ kebab opens the
  // session-actions panel, and Delete session lives THERE (with its two-click
  // confirm) rather than as a second trash button on the row. The kebab remains
  // inside the title cell so the table keeps its existing column model.
  if (isSession && !row._cross) {
    const sid = _esc(row.session_id);
    inner += '<span class="sess-title-actions">' +
      '<button type="button" class="session-row-kebab" data-session-row-more="' + sid + '" title="Session actions" aria-label="Session actions"><i data-lucide="more-vertical" style="width:14px;height:14px;"></i></button>' +
      '</span>';
  }
  return '<td class="col-' + colId + '" title="' + titleText + '">' + '<div class="' + rowClasses + '">' + inner + '</div>' + '</td>';
}

function _renderStatusCell(row, isSession, isAuto) {
  if (isAuto) {
    let statusHtml = '';
    const rawStatus = row.last_status || row.status || '';
    if (row.type === 'webhook') {
      statusHtml = row.active ? '<span class="sess-status complete">Active</span>' : '<span class="sess-status error">Disabled</span>';
    } else {
      statusHtml = _statusBadge(rawStatus || '—');
    }
    if (row.last_error) statusHtml += ' <span class="auto-error" title="' + _esc(row.last_error) + '">Error</span>';
    return '<td class="col-status">' + statusHtml + '</td>';
  }
  return '<td class="col-status">' + _statusBadge(row.run_status) + '</td>';
}

function _renderTypeCell(row, isAuto) {
  if (!isAuto) return '<td class="col-type"></td>';
  const icons = { scheduled: 'clock', event: 'zap', worker: 'cpu', webhook: 'webhook' };
  const ic = icons[row.type] || 'settings';
  return '<td class="col-type"><span class="auto-type"><i data-lucide="' + ic + '" style="width:12px;height:12px;"></i>' + _esc(row.type || '') + '</span></td>';
}

function _renderTriggerCell(row, isAuto) {
  if (!isAuto) return '<td class="col-trigger"></td>';
  let trigger = '—';
  if (row.type === 'scheduled') {
    trigger = '<code class="auto-cron">' + _esc(row.trigger || '—') + '</code>';
    if (row.timezone && row.timezone !== 'UTC') trigger += ' <span class="auto-tz">' + _esc(row.timezone) + '</span>';
  } else if (row.type === 'event') {
    trigger = '<span class="auto-event-src">' + _esc(row.trigger || '—') + '</span>';
    if (row.filter && Object.keys(row.filter).length > 0) trigger += ' <span class="auto-filter-badge">filter</span>';
  } else if (row.type === 'worker') {
    trigger = '<span class="auto-worker-name">' + _esc(row.name || row.task || '—') + '</span>';
  } else if (row.type === 'webhook') {
    trigger = '<span class="auto-webhook-name">' + _esc(row.name || '—') + '</span>';
  } else if (row.type === 'clone') {
    trigger = '<span class="auto-clone-of">clone of ' + _esc(row.clone_of || '?') + '</span>';
  }
  return '<td class="col-trigger">' + trigger + '</td>';
}

function _renderOutputCell(row, isAuto) {
  if (!isAuto || !Array.isArray(row.delivery) || row.delivery.length === 0) return '<td class="col-output"><span class="auto-tz">—</span></td>';
  const outIcons = { chat: 'message-circle', channel: 'send', webhook: 'webhook', file: 'file-text', email: 'mail' };
  const inner = row.delivery.map(function(d) {
    const kind = d.kind || 'other';
    const ic = outIcons[kind] || 'arrow-right';
    return '<span class="auto-out-badge auto-out-' + kind + '" title="' + _esc(d.label || kind) + '"><i data-lucide="' + ic + '" style="width:11px;height:11px;"></i>' + _esc(d.label || kind) + '</span>';
  }).join('');
  return '<td class="col-output"><span class="auto-out-list">' + inner + '</span></td>';
}

function _renderLinksCell(row, isSession) {
  if (!isSession || row._cross) return '<td class="col-links"></td>';
  return '<td class="col-links">' + _linkBadges(row) + '</td>';
}

function _renderDeviceCell(row, isAuto) {
  if (!isAuto || row.type !== 'scheduled') return '<td class="col-device"><span class="auto-tz">—</span></td>';
  const label = row.target_device || 'This device';
  return '<td class="col-device"><span class="auto-device-chip" title="Runs on ' + _esc(label) + '"><i data-lucide="monitor" style="width:11px;height:11px;"></i><span class="auto-device-text">' + _esc(label) + '</span></span></td>';
}

function _renderEnabledCell(row, isAuto) {
  if (!isAuto) return '<td class="col-enabled"></td>';
  const enabled = row.enabled !== false;
  return '<td class="col-enabled" style="text-align:center;"><span class="auto-status ' + (enabled ? 'ok' : 'error') + '">' + (enabled ? 'On' : 'Off') + '</span></td>';
}

// Expand / collapse a parent group. Re-renders the table but preserves the
// user's current checkbox selection across the rebuild.
function _toggleGroup(sid) {
  if (_expandedGroups.has(sid)) _expandedGroups.delete(sid);
  else _expandedGroups.add(sid);

  const checked = new Set(_getSelectedSessionIds());
  _renderTable(_sessionsData);
  if (checked.size) {
    document.querySelectorAll('.sessions-check-cell').forEach(c => {
      if (c.dataset.sessionId && checked.has(c.dataset.sessionId)) {
        c.classList.add('checked');
        const tr = c.closest('tr');
        if (tr) tr.classList.add('selected');
      }
    });
  }
  _updateTrashButton();
}

// Fill the table body with phantom "shimmer" rows that mirror the real column
// layout, so the first paint has the table's shape instead of a lone centered
// spinner. Uses the shared skeleton primitives (sk-shimmer in app3.css).
// SESSIONS-SKELETON (breadcrumb) — the sessions table paints a DETERMINISTIC
// loading state: exactly SIX rows, every content cell one 60%-wide shimmer bar,
// so loading reads as the same shape on every load and on every agent switch.
// This deliberately DIVERGES from the ragged 6–15 row / 30–100% draw used by the
// session-dropdown menu skeleton
// (ui/chat/elements/session-dropdown/controller.js → _renderSessionMenuSkeleton)
// and from the Agents-page static phantom rows — keep the sessions table itself
// at six uniform rows and do not "re-sync" it to the dropdown ranges.
// The title heading is part of this same loading state (title skeleton).
const _SKELETON_ROW_COUNT = 6;
const _SKELETON_BAR_WIDTH = '60%';
function _renderSkeletonRows(count) {
  const tbody = _qs('sessions-table-body');
  const empty = _qs('sessions-empty');
  const loading = _qs('sessions-loading');
  if (!tbody) return;
  if (empty) empty.style.display = 'none';
  if (loading) loading.style.display = 'none';   // skeleton rows replace the spinner
  const n = (typeof count === 'number') ? count : _SKELETON_ROW_COUNT;
  // Mirror the live table's columns when the config is already loaded so the
  // phantom keeps the real column shape; fall back to the session column set.
  const cols = _activeConfig ? getColumns(_activeConfig) : null;
  const colIds = (cols && cols.length)
    ? cols.map(c => c.id)
    : ['check', 'title', 'links', 'msgs', 'tokens_in', 'tokens_out', 'cost', 'duration'];
  const bar = '<span class="sess-sk sk-shimmer" style="width:' + _SKELETON_BAR_WIDTH + '"></span>';
  let html = '';
  for (let i = 0; i < n; i++) {
    let cells = '';
    for (const id of colIds) {
      if (id === 'check') { cells += '<td class="col-check"></td>'; continue; }
      cells += '<td class="col-' + id + '">' + bar + '</td>';
    }
    html += '<tr class="sessions-skeleton-row" aria-hidden="true">' + cells + '</tr>';
  }
  tbody.innerHTML = html;
  // The heading belongs to the same loading state — a stale "All Agent's
  // Sessions" label must never sit above the skeleton rows.
  _setHeadingSkeleton(true);
}

// ── Footer storage meter ───────────────────────────────────────────
// SESSION-STORAGE-METER (breadcrumb): "how much session storage is used vs
// available" for the Sessions footer, which lives inside the Agents page
// (ui/main-panel/agents/agents.html). The backend reports the same SQLite
// file + live-WAL footprint that the user_database_size_limit quota enforces,
// so what a user reads here and the pressure the cleanup worker reacts to are
// the same number — see GET /api/v1/db/storage-usage in app/api/db_viewer.py.
const _STORAGE_WARN_PERCENT = 80;   // amber from here — cleanup is getting close
const _STORAGE_FULL_PERCENT = 95;   // red from here — session eviction imminent
let _storageUsage = null;           // last payload that rendered successfully
let _storagePending = null;         // in-flight fetch, shared by every caller

async function _fetchStorageUsage() {
  const userId = app.currentUserId;
  if (!userId) return null;
  const url = apiPath(`/api/v1/db/storage-usage?db=user.db&user_id=${encodeURIComponent(userId)}`);
  const res = await fetch(url, { headers: authHeaders() });
  if (!res.ok) throw new Error(`storage-usage HTTP ${res.status}`);
  return await res.json();
}

function _storageState(data) {
  if (!data || data.available !== true) return 'unknown';
  if (!data.limit_enabled) return 'off';
  const pct = Number(data.used_percent);
  if (!isFinite(pct)) return 'ok';
  if (pct >= _STORAGE_FULL_PERCENT) return 'full';
  if (pct >= _STORAGE_WARN_PERCENT) return 'warn';
  return 'ok';
}

/** Full sentence for the meter's tooltip / accessible name. */
function _storageSummary(data) {
  if (!data || data.available !== true) {
    return (data && data.reason === 'remote_backend')
      ? 'Session storage is held by the remote database, so there is no per-user file to measure'
      : 'Session storage usage is unavailable';
  }
  if (!data.limit_enabled) return `${_fmtBytes(data.used_bytes)} used · no size limit configured`;
  return `${_fmtBytes(data.used_bytes)} used of ${_fmtBytes(data.limit_bytes)} · `
    + `${_fmtBytes(data.remaining_bytes)} available · ${Number(data.used_percent) || 0}%`;
}

/** Paint every footer copy (the shared section + the page-level dock). The
    meter is bar-only, so the exact figures ride on the group's title and
    aria-label instead of a visible readout. */
function _renderStorageUsage(data) {
  const measured = !!(data && data.available === true);
  const summary = _storageSummary(data);
  document.querySelectorAll('[data-sessions-footer-storage]').forEach(el => {
    el.dataset.storageState = _storageState(data);
    el.title = summary;
    el.setAttribute('aria-label', `Session storage: ${summary}`);
    const fill = el.querySelector('[data-storage-fill]');
    if (fill) {
      const pct = (measured && data.limit_enabled)
        ? Math.max(0, Math.min(100, Number(data.used_percent) || 0))
        : 0;
      fill.style.width = pct + '%';
    }
  });
}

function _refreshStorageUsage() {
  if (_sourceAccessDenied) { _renderStorageUsage(null); return Promise.resolve(); }
  if (_storagePending) return _storagePending;
  _storagePending = (async () => {
    try {
      const data = await _fetchStorageUsage();
      if (data) { _storageUsage = data; _renderStorageUsage(data); }
    } catch (e) {
      // A transient failure must not blank a good reading — keep the last one.
      console.warn('[Sessions] storage usage unavailable', e);
      _renderStorageUsage(_storageUsage);
    } finally {
      _storagePending = null;
    }
  })();
  return _storagePending;
}

async function _loadAndRender(reason = 'foreground') {
  if (_sourceAccessDenied) {
    _renderAccessDeniedState();
    return;
  }
  // SESSIONS-LOAD-INTENT (breadcrumb): only a FOREGROUND load (cold start, view
  // chip flip) may paint skeletons. The passive 30s auto-refresh ('background')
  // and session mutations ('mutation') must keep the resolved title and rows on
  // screen and repaint only when the returned data actually changed — a regular
  // update must never reset the view to a skeleton.
  const isBackground = reason === 'background';
  const isPassive = isBackground || reason === 'mutation';
  const loadViewId = _viewId;
  const loadAgentFilterId = _activeAgentFilter();
  const loadNativeContext = _nativeCodexContext;
  const isNativeCtx = !!loadNativeContext;
  const tbody = _qs('sessions-table-body');
  // Phantom takeover: the static shell ships real .sessions-skeleton-row rows in
  // the partial's tbody — replace them with a fresh draw just like an empty
  // tbody would get (same classes, so no visual flash).
  const hasPhantom = !!(tbody && tbody.querySelector('tr.sessions-skeleton-row'));
  // Normal contexts keep the ACCOUNT-WIDE catalog in the buffer, so the stamp is
  // null there; only a native Codex catalog is stamped with its own agent id.
  const bufferStamp = isNativeCtx ? loadAgentFilterId : null;
  const contextMismatch = _sessionsDataAgentId !== bufferStamp;
  if (!isPassive) {
    if (hasPhantom || (tbody && tbody.children.length === 0) || contextMismatch) _renderSkeletonRows();
  }
  let loadConfig;
  try {
    loadConfig = await loadViewerConfig(loadViewId);
    if (_viewId !== loadViewId || _activeAgentFilter() !== loadAgentFilterId || _nativeCodexContext !== loadNativeContext) return;
    _activeConfig = loadConfig;
  } catch (e) {
    console.warn('[DataViewer] Failed to load config for', loadViewId, e);
    return;
  }
  // SESSION-STORAGE-METER: the session store is account-wide, not view- or
  // agent-scoped, so the meter refreshes on every load — cold start, view
  // switch, the passive 30s pass, and after a delete/restore mutation.
  _refreshStorageUsage();
  // Native Codex catalogs are their own dataset. Every other context reads the
  // single account-wide session catalog and filters it locally, so a selected
  // agent never waits on (or is narrowed by) a per-agent server request.
  const fetchAgentFilter = isNativeCtx ? loadAgentFilterId : null;
  const cacheKey = _cacheKey(loadViewId, fetchAgentFilter, loadNativeContext);
  const cachedRows = cacheKey ? readAgentCache(cacheKey) : null;
  const bufferMismatch = _sessionsDataViewId !== loadViewId
    || _sessionsDataAgentId !== bufferStamp;
  // The account-wide cache is first paint for EVERY normal context — All Agents
  // and each agent card alike — so a selected agent renders its filtered subset
  // immediately instead of showing a scoped-request skeleton. Native Codex
  // catalogs keep their own scoped flow.
  if (!isNativeCtx && bufferMismatch && Array.isArray(cachedRows)) {
    _sessionsData = cachedRows;
    _sessionsDataViewId = loadViewId;
    _sessionsDataAgentId = null;
    _writeSharedSnapshot(loadViewId, cachedRows);
    _renderTable(cachedRows);
    _markSessionsPaint('memory');
  }
  if (loadNativeContext) {
    _setLoadStatus(Array.isArray(cachedRows) ? 'Verifying Codex tasks…' : 'Loading Codex tasks…', true);
  }
  // The other catalog may have changed (restore/delete/refresh) — drop the
  // cross-catalog search cache and prune the message index so the next query
  // re-fetches what it needs (without nuking the whole cache on auto-refresh).
  _invalidateSearchExtra();
  _invalidateMsgIndex();
  let rows;
  try {
    rows = await _fetchForConfig(loadConfig, fetchAgentFilter);
  } catch (error) {
    console.warn('[Sessions] Catalog load failed', error);
    if (loadNativeContext) _setLoadStatus('Could not verify Codex tasks', false, 3000);
    else _renderConnectionFaultState();
    return;
  }
  if (_viewId !== loadViewId || _activeAgentFilter() !== loadAgentFilterId || _nativeCodexContext !== loadNativeContext) return;
  if (!isNativeCtx) _setLoadStatus();
  if (isNativeCtx) {
    _sessionsData = rows;
    _sessionsDataViewId = loadViewId;
    _sessionsDataAgentId = loadAgentFilterId;
  } else {
    // The account-wide catalog is the canonical SESSIONS-SHARED-SNAPSHOT for
    // every normal context — keep it intact so agent switching stays a local
    // filter instead of a fresh per-agent request.
    _writeSharedSnapshot(loadViewId, rows);
    _sessionsData = rows;
    _sessionsDataViewId = loadViewId;
    _sessionsDataAgentId = null;
  }
  // Keep a useful returning-visit snapshot well beyond the 30s live refresh.
  // It is only first paint: the authoritative request above still revalidates
  // immediately, so a 30-minute IDB horizon removes blank/skeleton waits without
  // making stale rows authoritative.
  // The cache key represents the account-wide viewer. Do not replace it with
  // a selected agent's narrower result set.
  if (cacheKey) writeAgentCache(
    cacheKey,
    rows,
    loadNativeContext ? 7 * 24 * 60 * 60 * 1000 : 30 * 60 * 1000,
  );
  // A passive refresh that returned the SAME rows leaves the resolved table
  // completely untouched: no rebuilt tbody, no skeleton, no scroll jump.
  const signature = _rowsSignature(rows);
  if (isBackground && signature && signature === _lastRenderedSignature) {
    if (loadNativeContext) _setLoadStatus('Codex tasks verified', false, 1400);
    _updateViewButton();
    _updateTrashButton();
    return;
  }
  _renderTable(rows);
  _markSessionsPaint('network');
  // The activity chart follows the same context as the rows above it. A passive
  // 30s pass leaves an unchanged scope alone (the chart guards on scope itself);
  // a foreground load or a mutation refreshes it.
  _reloadActivityChart(reason !== 'background');
  if (loadNativeContext) _setLoadStatus('Codex tasks verified', false, 1400);

  // ── Phantom-cell hydration ────────────────────────────────────────────
  // Session rows always carry agent_id; when the stats payload lacks display
  // fields (orphaned/deleted agents, cold agent plane), fill them from the
  // lean bulk endpoint and repaint in place — no full refetch, no empty
  // "—" cells that stick forever. Missing ids resolve to nothing and simply
  // keep their fallback label.
  const missingAgentIds = [...new Set(rows
    .filter(r => r._rowType === 'session' && r.agent_id && !r.agent_name)
    .map(r => r.agent_id))];
  if (missingAgentIds.length) {
    hydrateAgentDisplay(missingAgentIds, app.currentUserId).then(recs => {
      if (!recs || !recs.length) return;
      const byId = new Map(recs.map(r => [r.agent_id, r]));
      let changed = false;
      for (const r of _sessionsData) {
        if (r._rowType !== 'session') continue;
        const rec = byId.get(r.agent_id);
        if (!rec) continue;
        if (!r.agent_name) { r.agent_name = rec.name; changed = true; }
        if (!r.agent_icon) { r.agent_icon = rec.icon; changed = true; }
        if (!r.agent_engine) { r.agent_engine = rec.engine; changed = true; }
      }
      if (changed) _renderTable(_sessionsData);
    });
  }
  // If a search is active, re-pull the other catalog and re-render so
  // cross-catalog results survive refreshes and view switches.
  if (_searchQuery.trim()) {
    _ensureSearchExtra().then(() => {
      if (_searchQuery.trim()) _renderTable(_sessionsData);
    }).then(() => {
      _scanMessagesForQuery(_searchQuery.trim());
    });
  }
  _updateViewButton();
  _updateTrashButton();
}

// ── Row click: switch to that session ──────────────────────────────

// Toggle the "opening…" state on a clicked row. Keep the session title and
// actions visible: only the existing agent-avatar slot temporarily becomes the
// spinner, then its exact icon markup is restored after loadSessionChat settles.
// The chat panel is the sole owner of app.currentSessionId. Keep this existing
// table DOM synchronized on its active-session event; do not wait for an async
// session-list reload, which can otherwise paint a stale prior row.
function _syncActiveSessionRow(sessionId = app.currentSessionId) {
  const activeId = String(sessionId || '');
  document.querySelectorAll('#sessions-table-body tr[data-session-id]').forEach(tr => {
    tr.classList.toggle('active-chat', !!activeId && tr.dataset.sessionId === activeId);
  });
}

function _setRowLoading(tr, on) {
  if (!tr) return;
  tr.classList.toggle('sessions-row-loading', on);
  const avatar = tr.querySelector('td.col-title .sess-title-agent-slot');
  if (!avatar) return;
  if (on) {
    if (!avatar.classList.contains('sess-title-agent-loading')) {
      avatar.dataset.sessAvatarMarkup = avatar.innerHTML;
      avatar.classList.add('sess-title-agent-loading');
      avatar.setAttribute('aria-busy', 'true');
      avatar.innerHTML = '<span class="sess-row-spinner" aria-label="Opening session"></span>';
    }
  } else if (avatar.classList.contains('sess-title-agent-loading')) {
    avatar.innerHTML = avatar.dataset.sessAvatarMarkup || '';
    delete avatar.dataset.sessAvatarMarkup;
    avatar.classList.remove('sess-title-agent-loading');
    avatar.removeAttribute('aria-busy');
  }
}

// Make `sessionId` the active chat session (switching agent if needed) and
// optionally jump to a main-panel tab afterwards ('genui' = Gen UI,
// 'browser' = Browser page). Shared by row clicks and the Links badges.
// `row` (when given) shows a per-row loading animation until the chat resolves.
async function _switchToSession(sessionId, agentId, { tab = null, title = null, row = null } = {}) {
  if (!sessionId) return;
  // Recycled sessions aren't loadable — block bin rows, whether the whole bin
  // view is open or a recycled row surfaced via cross-catalog search. Live
  // rows found through search while in the bin view ARE loadable.
  const origin = row && row.dataset ? row.dataset.origin : (_viewId === 'recycle-bin' ? 'bin' : 'active');
  if (origin === 'bin') return;
  app.sessionTitle = title || sessionId.slice(0, 12);

  _setRowLoading(row, true);
  try {
    await switchToSession(sessionId, { agentId });
  } finally {
    _setRowLoading(row, false);
  }
  populateSessionSelect(app.currentUserId);

  // On mobile the tab-switch hides chat — reveal it so the user sees the session
  // they just clicked, mirroring how the Agents page shows chat on tap.
  if (typeof window.__getChatVisible === 'function' && !window.__getChatVisible()
      && typeof window.__applyChatVisible === 'function') {
    window.__applyChatVisible(true);
  }

  if (tab && typeof window.__setMainTab === 'function') {
    window.__setMainTab(tab);
  }
}

function _onRowClick(e) {
  const tr = e.target.closest('tr[data-session-id]');
  if (!tr || !tr.dataset.sessionId) return;
  _switchToSession(tr.dataset.sessionId, tr.dataset.agentId, {
    title: tr.querySelector('.col-title')?.textContent,
    row: tr,
  });
}

// ── Selection helpers ──────────────────────────────────────────────

// Which catalog(s) the currently checked rows belong to ('active' | 'bin').
// Drives restore/delete behaviour in the merged 'both' view, where both
// catalogs can be selected at once.
function _selectedOrigins() {
  const origins = new Set();
  document.querySelectorAll('.sessions-check-cell.checked').forEach(cell => {
    const tr = cell.closest('tr');
    if (tr && tr.dataset.origin) origins.add(tr.dataset.origin);
  });
  // A recycle can remove the selected rows from the active projection before
  // the follow-up panel is rendered. Its durable action context is bin-only.
  if (origins.size === 0 && _postBinSessionIds.length) origins.add('bin');
  return origins;
}

function _selectedSessionCount() {
  const checked = document.querySelectorAll('.sessions-check-cell.checked').length;
  return checked || _postBinSessionIds.length;
}

function _clearPostBinContext() {
  _postBinSessionIds = [];
}

// Selection / bin toolbar — mirrors agents' _updateBinToolbar (HARMONY-SESSIONS-BIN).
function _syncFooterActions() {
  const trash = _qs('sessions-trash-btn');
  const n = document.querySelectorAll('.sessions-check-cell.checked').length;
  const state = trash?.dataset.state || 'trash';
  const armed = state === 'warning';
  document.querySelectorAll('[data-sessions-footer-action="delete"]').forEach(btn => {
    btn.disabled = n === 0;
    btn.dataset.state = state;
    btn.classList.toggle('warning', armed);
    btn.title = n === 0 ? 'Select sessions to delete' : (trash?.title || trashTitle());
    btn.setAttribute('aria-label', armed ? 'Confirm deleting selected sessions' : 'Delete selected sessions');
    const label = btn.querySelector('.sessions-footer-delete-label');
    if (label) label.textContent = state === 'deleting' ? 'Deleting...' : (armed ? 'Delete?' : 'Delete');
  });
}

// SESSION-GUTTER-CHECK-ALL — the footer's select-all/clear-all twin of the
// gutter toggle. Its pressed state mirrors the rows: on only when EVERY
// selectable row is checked. Painted for all three footer copies at once.
function _syncFooterCheckAll() {
  const cells = document.querySelectorAll('.sessions-check-cell');
  const n = document.querySelectorAll('.sessions-check-cell.checked').length;
  const allChecked = cells.length > 0 && n === cells.length;
  document.querySelectorAll('[data-sessions-footer-checkall]').forEach(btn => {
    btn.dataset.state = allChecked ? 'on' : 'off';
    btn.setAttribute('aria-pressed', allChecked ? 'true' : 'false');
    const label = allChecked ? 'Clear selection' : 'Select all sessions';
    btn.title = label;
    btn.setAttribute('aria-label', label);
  });
}

// Toggle every selectable row on/off — the one path shared by the header
// check-all and the footer SESSION-GUTTER-CHECK-ALL button. Selects all unless
// everything is already checked, so repeated clicks alternate
// checked → unchecked → checked (never a no-op on a partly-selected list).
function _toggleAllSelection() {
  _clearPostBinContext();
  const cells = document.querySelectorAll('.sessions-check-cell');
  const checkTo = document.querySelectorAll('.sessions-check-cell.checked').length < cells.length;
  cells.forEach(c => {
    c.classList.toggle('checked', checkTo);
    c.closest('tr')?.classList.toggle('selected', checkTo);
  });
  const checkAll = _qs('sessions-check-all');
  if (checkAll) checkAll.classList.toggle('checked', checkTo);
  const trash = _qs('sessions-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle() });
  _resetUndoButton();
  _updateTrashButton();
}

function _updateTrashButton() {
  const trash = _qs('sessions-trash-btn');
  const undo = _qs('sessions-undo-btn');
  const clear = _qs('sessions-clear-selection-btn');
  const panel = _qs('sessions-selection-panel');
  const count = _qs('sessions-selection-count');
  _updateViewButton();
  const n = _selectedSessionCount();
  const origins = _selectedOrigins();
  const binned = n > 0 && origins.size === 1 && origins.has('bin');
  const mode = binned ? 'binned' : 'selected';
  if (panel) {
    panel.hidden = n === 0;
    panel.dataset.mode = mode;
  }
  if (count) count.textContent = binned
    ? n + ' Session' + (n === 1 ? '' : 's') + ' binned'
    : n + ' session' + (n === 1 ? '' : 's') + ' selected';
  if (_selectionPanelMode !== mode) {
    _selectionPanelMode = mode;
    if (trash) resetDeleteBtn(trash, { size: '16px', title: binned ? 'Permanently delete selected sessions' : trashTitle() });
    if (undo) resetDeleteBtn(undo, { size: '16px', title: 'Undo recycling selected sessions', label: 'Undo recycling selected sessions', iconName: 'undo-2' });
  }
  if (undo) undo.hidden = !binned;
  if (clear) clear.hidden = binned;
  if (trash) {
    trash.title = binned ? 'Permanently delete selected sessions' : trashTitle();
    trash.disabled = n === 0;
    trash.setAttribute('aria-label', binned
      ? 'Permanently delete selected sessions'
      : (origins.has('bin')
        ? 'Delete selected sessions; recycled sessions will be permanently deleted'
        : 'Move selected sessions to the recycling bin'));
  }
  _syncFooterActions();
  _syncFooterCheckAll();
}

function _resetUndoButton() {
  const undo = _qs('sessions-undo-btn');
  if (undo) resetDeleteBtn(undo, { size: '16px', title: 'Undo recycling selected sessions', label: 'Undo recycling selected sessions', iconName: 'undo-2' });
}

function _clearSelection() {
  _clearPostBinContext();
  document.querySelectorAll('.sessions-check-cell.checked').forEach(cell => {
    cell.classList.remove('checked');
    cell.closest('tr')?.classList.remove('selected');
  });
  _qs('sessions-check-all')?.classList.remove('checked');
  const trash = _qs('sessions-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle(), label: 'Delete selected sessions' });
  _resetUndoButton();
  _updateTrashButton();
}

function _getSelectedSessionIds() {
  const ids = [];
  document.querySelectorAll('.sessions-check-cell.checked').forEach(cell => {
    const sid = cell.dataset.sessionId;
    if (sid) ids.push(sid);
  });
  return ids.length ? ids : [..._postBinSessionIds];
}

function _selectSessionIds(ids) {
  const wanted = new Set(ids || []);
  document.querySelectorAll('.sessions-check-cell').forEach(cell => {
    const selected = wanted.has(cell.dataset.sessionId);
    cell.classList.toggle('checked', selected);
    cell.closest('tr')?.classList.toggle('selected', selected);
  });
  _qs('sessions-check-all')?.classList.remove('checked');
  _updateTrashButton();
}

function _toast(msg) {
  if (!msg) return;
  const toast = document.createElement('div');
  toast.textContent = msg;
  toast.style.cssText = 'position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:var(--accent);color:#fff;padding:8px 18px;border-radius:8px;font-size:13px;font-weight:600;z-index:9999;';
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 2500);
}

// ── Per-row session actions — same panel contract as chat dropdown ─────────
// DESIGN SYNC CONTRACT: Keep this body-mounted .session-row-actions popup in
// lockstep with ui/chat/elements/session-dropdown/controller.js.  The title
// surfaces intentionally share both their action-panel design and lifecycle.
let _sessionRowActionsEl = null;

function _closeSessionRowActions() {
  if (_sessionRowActionsEl) {
    _sessionRowActionsEl.remove();
    _sessionRowActionsEl = null;
  }
  document.removeEventListener('pointerdown', _onSessionRowActionsOutside, true);
  document.removeEventListener('keydown', _onSessionRowActionsEsc, true);
  window.removeEventListener('resize', _closeSessionRowActions);
}

function _onSessionRowActionsOutside(e) {
  if (_sessionRowActionsEl && !_sessionRowActionsEl.contains(e.target)
      && !e.target.closest('[data-session-row-more]')) _closeSessionRowActions();
}

function _onSessionRowActionsEsc(e) {
  if (e.key === 'Escape') _closeSessionRowActions();
}

function _sessionRowById(sid) {
  return _sessionsData.find(row => row && row._rowType === 'session' && row.session_id === sid) || null;
}

async function _setSessionReadState(sid, read) {
  const endpoint = read ? 'read' : 'unread';
  const response = await fetch(apiPath(`/api/v1/db/sessions/${encodeURIComponent(sid)}/${endpoint}?db=user.db&user_id=${encodeURIComponent(app.currentUserId || '')}`), {
    method: 'POST',
    headers: authHeaders(),
  });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  await _loadAndRender('mutation');
  try { populateSessionSelect(app.currentUserId); } catch (_) {}
}

function _copySessionRowId(value, label, row) {
  if (!value || !navigator.clipboard?.writeText) return;
  navigator.clipboard.writeText(value).then(() => {
    row.classList.add('copied');
    const val = row.querySelector('.session-row-id-val');
    const original = val?.textContent || '';
    if (val) val.textContent = `${label} ID copied ✓`;
    setTimeout(() => {
      row.classList.remove('copied');
      if (val) val.textContent = original;
    }, 1200);
  }).catch(() => {});
}

function _openSessionRowActions(sid, kebabBtn) {
  const sess = _sessionRowById(sid);
  if (!sess || !kebabBtn) return;
  _closeSessionRowActions();
  const isRead = !!sess.read_at && !sess.unread_at;
  const lastSeen = sess.read_at ? _formatRelativeTime(sess.read_at) : '';
  const tableRow = kebabBtn.closest('tr');
  const popup = document.createElement('div');
  popup.className = 'session-row-actions';
  popup.dataset.id = sid;
  popup.dataset.source = 'sessions-page';
  popup.innerHTML =
    `<button type="button" class="session-row-action" data-action="pin">${icon(sess.pinned ? 'pin-off' : 'pin', { size: '14px' })} ${sess.pinned ? 'Unpin session' : 'Pin session'}</button>` +
    `<button type="button" class="session-row-action" data-action="auto-rename">${icon('wand-2', { size: '14px' })} Auto rename</button>` +
    `<button type="button" class="session-row-action" data-action="rename">${icon('pencil', { size: '14px' })} Rename session</button>` +
    `<button type="button" class="session-row-action" data-action="refresh">${icon('refresh-cw', { size: '14px' })} Refresh session</button>` +
    `<div class="session-row-action session-row-read-info" role="status">${icon('clock-3', { size: '14px' })} Last seen${lastSeen ? ` ${lastSeen}` : ' never'}</div>` +
    `<button type="button" class="session-row-action" data-action="${isRead ? 'mark-unread' : 'mark-read'}">${icon(isRead ? 'mail-open' : 'check-circle', { size: '14px' })} ${isRead ? 'Mark unread' : 'Mark read'}</button>` +
    `<button type="button" class="session-row-action danger" data-action="delete">${icon('trash-2', { size: '14px' })} Delete session</button>`;

  const info = document.createElement('div');
  info.className = 'session-row-actions-info';
  const addIdRow = (key, label, value) => {
    const item = document.createElement('div');
    item.className = 'session-row-action session-row-id';
    item.title = `Copy ${label} id`;
    item.tabIndex = 0;
    item.innerHTML = `<span class="session-row-id-key">${key}</span><span class="session-row-id-val">${value ? _esc(value.slice(0, 8)) : '—'}</span>`;
    item.addEventListener('click', (e) => { e.stopPropagation(); _copySessionRowId(value, label, item); });
    item.addEventListener('keydown', (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); _copySessionRowId(value, label, item); } });
    info.appendChild(item);
  };
  addIdRow('A:', 'Agent', sess.agent_id || '');
  addIdRow('S:', 'Session', sid);
  if (sess.engine_thread_id) addIdRow(sess.agent_engine === 'codex' ? 'Codex:' : 'Claude:', sess.agent_engine === 'codex' ? 'Codex' : 'Claude', sess.engine_thread_id);
  popup.appendChild(info);

  document.body.appendChild(popup);
  // Position under the kebab, right-aligned, clamped to the visual viewport.
  // Shared helper keeps this DESIGN SYNC CONTRACT copy identical to the chat
  // session dropdown's row-actions panel (controller.js).
  positionKebabPopup(popup, kebabBtn);
  popup.addEventListener('click', async (e) => {
    e.stopPropagation();
    const action = e.target.closest('.session-row-action[data-action]');
    if (!action) return;
    try {
      switch (action.dataset.action) {
        case 'pin':
          await togglePin(sid);
          _closeSessionRowActions();
          await _loadAndRender('mutation');
          break;
        case 'auto-rename':
          _closeSessionRowActions();
          await autoRenameSession(sid);
          await _loadAndRender('mutation');
          break;
        case 'rename':
          _closeSessionRowActions();
          if (tableRow) startRename(sid, tableRow);
          break;
        case 'refresh':
          _closeSessionRowActions();
          await _loadAndRender('mutation');
          try { populateSessionSelect(app.currentUserId); } catch (_) {}
          break;
        case 'mark-read':
          await _setSessionReadState(sid, true);
          _closeSessionRowActions();
          break;
        case 'mark-unread':
          await _setSessionReadState(sid, false);
          _closeSessionRowActions();
          break;
        case 'delete':
          // Retain the page's established two-click safety confirmation.
          _deleteOneSession(action, sid, tableRow?.dataset.origin || 'active');
          break;
      }
    } catch (error) {
      console.warn('[Sessions] Row action failed', error);
      _toast('Session action failed');
    }
  });
  _sessionRowActionsEl = popup;
  setTimeout(() => {
    document.addEventListener('pointerdown', _onSessionRowActionsOutside, true);
    document.addEventListener('keydown', _onSessionRowActionsEsc, true);
    window.addEventListener('resize', _closeSessionRowActions);
  }, 0);
}

// Delete the checked sessions. Active-origin rows soft-delete (move to the
// bin); bin-origin rows permanently delete (with ?permanent=true) — so the
// merged 'both' view can act on both catalogs in one go.
async function _deleteOneSession(btn, sid, origin) {
  if (!sid) return;
  const permanent = origin === 'bin';
  advanceDeleteBtn(btn, {
    size: '14px',
    spinSize: '14px',
    armTitle: permanent ? 'Click again to permanently delete' : 'Click again to move to the bin',
    onConfirm: async () => {
      const token = localStorage.getItem('auth_token');
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}?db=user.db`;
      if (permanent) url += '&permanent=true';
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'DELETE' });
      if (!res.ok && res.status !== 404) throw new Error(`Delete failed (${res.status})`);
      _toast(permanent ? 'Deleted session' : 'Recycled session');
      // The session is gone, so its actions panel is stale the moment the DELETE
      // resolves. Close it BEFORE the table reload: the reload can be slow (an
      // active search filter re-indexes on repaint), and waiting on it left the
      // panel lingering — with a label-less delete row — for the whole refresh.
      _closeSessionRowActions();
      await _loadAndRender('mutation');
      try { populateSessionSelect(app.currentUserId); } catch (_) {}
      resetDeleteBtn(btn, { size: '14px', title: 'Delete session' });
    },
  });
}

async function _deleteSelectedSessions(btn) {
  const cells = Array.from(document.querySelectorAll('.sessions-check-cell.checked'));
  const selections = cells.length
    ? cells.map(cell => ({
      sid: cell.dataset.sessionId,
      origin: cell.closest('tr')?.dataset.origin || (_viewId === 'recycle-bin' ? 'bin' : 'active'),
    }))
    : _postBinSessionIds.map(sid => ({ sid, origin: 'bin' }));
  if (selections.length === 0) return;

  const token = localStorage.getItem('auth_token');
  let fail = 0, recycled = 0, deleted = 0;
  const recycledIds = [];
  for (const { sid, origin } of selections) {
    if (!sid) continue;
    const permanent = origin === 'bin';
    try {
      let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}?db=user.db`;
      if (permanent) url += '&permanent=true';
      if (token) url += `&token=${encodeURIComponent(token)}`;
      const res = await fetch(apiPath(url), { method: 'DELETE' });
      if (res.ok || res.status === 404) {
        if (permanent) deleted++;
        else { recycled++; recycledIds.push(sid); }
      } else fail++;
    } catch (_) { fail++; }
  }
  if (fail > 0) console.warn('[Sessions] Failed to delete', fail, 'sessions');
  if (deleted > 0) _toast(`Deleted ${deleted} session${deleted > 1 ? 's' : ''}`);
  // Stay in the user's current view after recycling. In an active-only view
  // these rows naturally disappear, so retain their IDs as the binned panel's
  // action context. When a current projection includes them, the normal DOM
  // selection is reapplied as well.
  if (recycledIds.length) {
    _postBinSessionIds = recycledIds;
    // Paint the binned follow-up panel before the mutation refresh starts. The
    // active-only projection removes these rows, and a slow/cold refresh must
    // not leave the user without Undo/Permanent delete while it is loading.
    _updateTrashButton();
  } else if (deleted > 0) {
    _clearPostBinContext();
  }
  await _loadAndRender('mutation');
  if (recycledIds.length) {
    _selectSessionIds(recycledIds);
  } else {
    _updateTrashButton();
  }
  // Keep the chat side-panel session list in sync with what we just removed.
  try { populateSessionSelect(app.currentUserId); } catch (_) {}
  if (btn && !recycledIds.length) resetDeleteBtn(btn, { size: '16px', title: trashTitle() });
}

// Restore a single session from the bin. Returns true on success. Shared by
// the bulk Restore button and the "In bin" chip's two-click confirm.
async function _restoreOne(sid) {
  if (!sid) return false;
  const token = localStorage.getItem('auth_token');
  try {
    let url = `/api/v1/db/sessions/${encodeURIComponent(sid)}/restore?db=user.db`;
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(apiPath(url), { method: 'POST' });
    return res.ok;
  } catch (e) {
    console.warn('[Sessions] Failed to restore', sid, e);
    return false;
  }
}

// Restore the checked binned sessions back to the active list. Only bin-origin
// rows are touched — in the merged 'both' view an active row can never be
// "restored" (and the Restore button only appears once a binned row is checked).
async function _restoreSelected() {
  const cells = Array.from(document.querySelectorAll('.sessions-check-cell.checked'));
  const ids = cells.length
    ? cells
      .filter(cell => cell.closest('tr')?.dataset.origin === 'bin')
      .map(cell => cell.dataset.sessionId)
    : [..._postBinSessionIds];
  let ok = 0;
  for (const sid of ids) {
    if (sid && await _restoreOne(sid)) ok++;
  }
  if (ok > 0) _toast(`Restored ${ok} session${ok > 1 ? 's' : ''}`);
  _clearPostBinContext();
  await _loadAndRender('mutation');
  _updateTrashButton();
  if (typeof populateSessionSelect === 'function') {
    try { populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

// "bin" chip — two-click restore. First click arms the chip ("Restore?"),
// second click restores the session. Disarms after 3s if the user doesn't
// confirm, mirroring the delete button's confirm pattern (HARMONY-SESSIONS-BIN).
async function _onBinChipClick(chip) {
  const sid = chip && chip.dataset.sessionId;
  if (!sid) return;
  if (!chip.classList.contains('armed')) {
    chip.classList.add('armed');
    chip.textContent = 'Restore?';
    chip.title = 'Click again to restore this session';
    clearTimeout(chip._disarmTimer);
    chip._disarmTimer = setTimeout(() => {
      chip.classList.remove('armed');
      chip.textContent = 'bin';
      chip.title = 'In recycling bin — click to restore';
    }, 3000);
    return;
  }
  // Confirmed — restore now.
  clearTimeout(chip._disarmTimer);
  chip.classList.remove('armed');
  chip.textContent = 'bin';
  chip.title = 'In recycling bin — click to restore';
  if (await _restoreOne(sid)) _toast('Session restored');
  await _loadAndRender('mutation');
  _updateTrashButton();
  if (typeof populateSessionSelect === 'function') {
    try { populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

// Shared flag chip: the first click arms its visible confirmation for two
// seconds; the second runs the verb the chip itself carries (read from the chip,
// never from local state, so a re-render between the two clicks cannot flip the
// meaning of the second one — same contract as ui/shared/js/needs-input-chip.js).
//
//   'read'   — the COLOURED unread flag. Marking read LEAVES the flag in place
//              and greys it for every flag except `complete` (a read but
//              unfinished session still wants attention); a clean `complete`
//              drops its chip, because the work is acknowledged.
//   'unread' — the GREY read-but-unfinished chip. Puts the session back to
//              unread so the coloured flag (and the notification row) return.
//
// SYNC: ui/chat/elements/session-dropdown/controller.js honours the same verb.
async function _onNeedsInputChipClick(chip) {
  const sid = chip?.dataset.sessionId;
  if (!sid) return;
  const toUnread = chip.dataset.needsInputVerb === 'unread';
  await activateNeedsInputChip(chip, async () => {
    await _setSessionReadState(sid, !toUnread);
    return true;
  });
}

function trashTitle() {
  const n = document.querySelectorAll('.sessions-check-cell.checked').length;
  const origins = _selectedOrigins();
  if (_viewId === 'recycle-bin') return n ? 'Permanently delete selected' : 'Select items to delete';
  if (_viewId === 'both') {
    if (n === 0) return 'Select sessions to recycle or permanently delete';
    if (origins.has('bin') && !origins.has('active')) return 'Permanently delete selected';
    if (origins.has('bin')) return 'Recycle active · permanently delete binned';
    return 'Move selected to the recycling bin';
  }
  return n ? 'Move selected to the recycling bin' : 'Select sessions to delete';
}

async function _switchView(viewId) {
  if (_viewId === viewId) return;
  _clearPostBinContext();
  const spinner = _qs('sessions-loading-spinner');
  if (spinner) spinner.style.display = 'inline-block';
  _viewId = viewId;
  _lastTheadConfigId = null; // force full thead rebuild on view change
  _sortColumn = 'last_active';
  _sortAsc = false;
  _saveViewState();
  await _loadAndRender();
  if (spinner) spinner.style.display = 'none';
}

async function _enterBin() { return _switchView('recycle-bin'); }
async function _exitBin() { return _switchView('sessions'); }

function _toggleCheck(cell) {
  _clearPostBinContext();
  cell.classList.toggle('checked');
  const tr = cell.closest('tr');
  if (tr) tr.classList.toggle('selected', cell.classList.contains('checked'));
  const trash = _qs('sessions-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle() });
  _resetUndoButton();
  _updateTrashButton();
}

// ── Bulk subset selection ──────────────────────────────────────────
// Identify which "family" a session belongs to. Optimizer runs are the
// Planner (optimizer-*) / Closer (closer-*) sessions, which are top-level
// rows (matched by id prefix, since the backend no longer tags them as
// children); spawns are orchestrator helpers (child_role spawn / spawn-* ids).
function _matchKind(s, kind) {
  if (kind === 'all') return true;
  const sid = s.session_id || '';
  if (kind === 'optimizer') {
    return s.child_role === 'planner' || s.child_role === 'closer' ||
      /^(optimizer|closer)-/.test(sid);
  }
  if (kind === 'spawn') {
    return s.child_role === 'spawn' || /^spawn-/.test(sid);
  }
  return false;
}

// Check every session matching `kind` (optimizer | spawn | all | none). Expands
// all parent groups first so collapsed children are in the DOM and can be
// selected, then deletion works on the whole family in one click.
function _selectSubset(kind) {
  _clearPostBinContext();
  // Clear any current selection (data rows + the header toggle).
  document.querySelectorAll('.sessions-check-cell.checked').forEach(c => {
    c.classList.remove('checked');
    c.closest('tr')?.classList.remove('selected');
  });
  const checkAll = _qs('sessions-check-all');
  if (checkAll) checkAll.classList.remove('checked');

  if (kind === 'none') { _updateTrashButton(); return; }

  // Reveal collapsed children so their checkboxes exist, then re-render.
  for (const s of _sessionsData) {
    if ((s.child_count || 0) > 0) _expandedGroups.add(s.session_id);
  }
  _renderTable(_sessionsData);

  const want = new Set(_sessionsData.filter(s => _matchKind(s, kind)).map(s => s.session_id));
  document.querySelectorAll('.sessions-check-cell').forEach(c => {
    const sid = c.dataset.sessionId;
    if (sid && want.has(sid)) {
      c.classList.add('checked');
      c.closest('tr')?.classList.add('selected');
    }
  });
  const trash = _qs('sessions-trash-btn');
  if (trash) resetDeleteBtn(trash, { size: '16px', title: trashTitle() });
  _resetUndoButton();
  _updateTrashButton();
}

// ── Column resize ──────────────────────────────────────────────────
// Drag the right edge of any .col-resizable <th>. With table-layout:fixed,
// setting the header's width drives the whole column. Widths persist per
// column-class in tenant-scoped IDB UI state so they survive reloads and re-renders (only
// the <tbody> is rebuilt; the <thead> widths stay put).
const _COLW_KEY = 'sessions.colWidths';
let _resizing = null;

function _loadColWidths() {
  try {
    const saved = kvRead('agents:sessions-column-widths', _COLW_KEY);
    return (typeof saved === 'string' ? JSON.parse(saved || '{}') : saved) || {};
  } catch (_) { return {}; }
}
function _saveColWidths(m) {
  kvWrite('agents:sessions-column-widths', _COLW_KEY, m);
}
// A column's stable identity = its semantic col-* class (not col-resizable).
function _colKey(th) {
  return Array.from(th.classList).find(c => c.startsWith('col-') && c !== 'col-resizable') || '';
}
function _applyColWidths() {
  const m = _loadColWidths();
  document.querySelectorAll('#sessions-table thead th').forEach(th => {
    const k = _colKey(th);
    if (k && m[k]) { th.style.width = m[k] + 'px'; th.style.minWidth = m[k] + 'px'; }
  });
}
function _onResizeMove(e) {
  if (!_resizing) return;
  const cx = e.clientX ?? e.touches?.[0]?.clientX ?? 0;
  const w = Math.max(48, _resizing.startW + (cx - _resizing.startX));
  _resizing.th.style.width = w + 'px';
  _resizing.th.style.minWidth = w + 'px';
}
function _onResizeUp() {
  if (!_resizing) return;
  if (_resizing.key) {
    const m = _loadColWidths();
    m[_resizing.key] = _resizing.th.offsetWidth;
    _saveColWidths(m);
  }
  _resizing.th.classList.remove('resizing');
  _resizing = null;
  document.body.style.cursor = '';
  document.body.style.userSelect = '';
  window.removeEventListener('mousemove', _onResizeMove);
  window.removeEventListener('mouseup', _onResizeUp);
  window.removeEventListener('touchmove', _onResizeMove);
  window.removeEventListener('touchend', _onResizeUp);
}
function _wireColumnResize() {
  const thead = document.querySelector('#sessions-table thead');
  if (!thead || thead._resizeWired) return;
  thead._resizeWired = true;

  function _startResize(e, clientX) {
    if (e.target.closest('.sessions-header-actions')) return;
    const th = e.target.closest('th.col-resizable');
    if (!th) return;
    const rect = th.getBoundingClientRect();
    // Only start when grabbing the ~8px right-edge handle (the col-resize zone).
    if (clientX < rect.right - 8) return;
    e.preventDefault();
    th.classList.add('resizing');
    _resizing = { th, startX: clientX, startW: th.offsetWidth, key: _colKey(th) };
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    window.addEventListener('mousemove', _onResizeMove);
    window.addEventListener('mouseup', _onResizeUp);
    window.addEventListener('touchmove', _onResizeMove, { passive: false });
    window.addEventListener('touchend', _onResizeUp);
  }

  thead.addEventListener('mousedown', (e) => {
    _startResize(e, e.clientX);
  });

  thead.addEventListener('touchstart', (e) => {
    const touch = e.touches?.[0];
    if (!touch) return;
    _startResize(e, touch.clientX);
  }, { passive: false });
}

// The bulk panel can be repositioned by dragging only its header. Pointer
// capture keeps the drag stable if the pointer leaves the panel.
function _wireSelectionPanelDrag() {
  const panel = _qs('sessions-selection-panel');
  const header = _qs('sessions-selection-panel-header');
  if (!panel || !header || panel._dragWired) return;
  panel._dragWired = true;
  let drag = null;
  const margin = 6;

  const clamp = () => {
    if (!panel.classList.contains('moved') || panel.hidden) return;
    const rect = panel.getBoundingClientRect();
    const left = Math.max(margin, Math.min(rect.left, window.innerWidth - rect.width - margin));
    const top = Math.max(margin, Math.min(rect.top, window.innerHeight - rect.height - margin));
    panel.style.left = Math.round(left) + 'px';
    panel.style.top = Math.round(top) + 'px';
  };
  const finish = (e) => {
    if (!drag) return;
    drag = null;
    panel.classList.remove('dragging');
    try { if (panel.hasPointerCapture(e.pointerId)) panel.releasePointerCapture(e.pointerId); } catch (_) {}
  };

  header.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    const rect = panel.getBoundingClientRect();
    panel.classList.add('moved', 'dragging');
    panel.style.left = Math.round(rect.left) + 'px';
    panel.style.top = Math.round(rect.top) + 'px';
    panel.style.bottom = 'auto';
    drag = { x: e.clientX, y: e.clientY, left: rect.left, top: rect.top };
    panel.setPointerCapture(e.pointerId);
    e.preventDefault();
  });
  panel.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const left = Math.max(margin, Math.min(
      drag.left + e.clientX - drag.x,
      window.innerWidth - panel.offsetWidth - margin,
    ));
    const top = Math.max(margin, Math.min(
      drag.top + e.clientY - drag.y,
      window.innerHeight - panel.offsetHeight - margin,
    ));
    panel.style.left = Math.round(left) + 'px';
    panel.style.top = Math.round(top) + 'px';
  });
  panel.addEventListener('pointerup', finish);
  panel.addEventListener('pointercancel', finish);
  panel.addEventListener('lostpointercapture', finish);
  window.addEventListener('pointerup', finish);
  window.addEventListener('resize', clamp);
}

// ── Wire DOM events (run once) ─────────────────────────────────────

function _wireDom() {
  if (_initialized) return;
  _initialized = true;

  // Refresh button — full reload of the sessions page state, as if the page
  // were refreshed in the browser: clears the search query, selection and
  // expanded groups, resets the sort to the persisted default, then re-fetches
  // fresh data. Deliberately NOT window.location.reload() — that would reload
  // the whole app (chat, agents, …), not just this page.
  const refreshBtn = _qs('sessions-refresh-btn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', async () => {
      // Clear the search field + query + any in-flight message scan.
      const searchInput = _qs('sessions-search-input');
      const searchClear = _qs('sessions-search-clear');
      if (searchInput) searchInput.value = '';
      if (searchClear) searchClear.style.display = 'none';
      _searchQuery = '';
      _msgScanBusy = null;
      _setMsgScanning(false);
      // Clear selection (data rows + the header toggle).
      document.querySelectorAll('.sessions-check-cell.checked').forEach(c => {
        c.classList.remove('checked');
        c.closest('tr')?.classList.remove('selected');
      });
      const checkAll = _qs('sessions-check-all');
      if (checkAll) checkAll.classList.remove('checked');
      _expandedGroups.clear();
      // Reset the sort back to the persisted default (what a browser refresh
      // would re-read from the persisted UI-state row).
      _sortColumn = _stored.sortColumn || 'last_active';
      _sortAsc = _stored.sortAsc === true;
      // Spin the icon while the fresh data loads.
      const ic = refreshBtn.querySelector('i');
      if (ic) ic.classList.add('lucide-spin');
      refreshBtn.disabled = true;
      try {
        await _loadAndRender();
        _updateTrashButton();
      } finally {
        refreshBtn.disabled = false;
        if (ic) ic.classList.remove('lucide-spin');
      }
    });
  }

  // Wire row clicks (delegated) — skip clicks on check cells
  const tbody = _qs('sessions-table-body');
  if (tbody) {
    tbody.addEventListener('click', (e) => {
      const needsInputChip = e.target.closest('.session-needs-input-chip');
      if (needsInputChip) {
        // Stop here so a stale/re-mounted delegated listener cannot interpret
        // the same physical first click as the confirmation click.
        e.stopImmediatePropagation();
        void _onNeedsInputChipClick(needsInputChip);
        return;
      }
      const rowMore = e.target.closest('[data-session-row-more]');
      if (rowMore) {
        e.stopPropagation();
        const sid = rowMore.dataset.sessionRowMore;
        if (_sessionRowActionsEl?.dataset.id === sid) _closeSessionRowActions();
        else _openSessionRowActions(sid, rowMore);
        return;
      }
      // The parent avatar expands/collapses its inline family before the row
      // click can switch the main chat session.
      const familyToggle = e.target.closest('.sess-title-agent-toggle');
      if (familyToggle) {
        _toggleGroup(familyToggle.dataset.sessionId);
        return;
      }
      const checkCell = e.target.closest('.sessions-check-cell');
      if (checkCell) {
        _toggleCheck(checkCell);
        return;
      }
      // Links column: open the genui / browser for that session.
      const linkBadge = e.target.closest('.sess-link-badge');
      if (linkBadge) {
        e.stopPropagation();
        const tab = linkBadge.dataset.linkKind === 'genui' ? 'genui' : 'browser';
        _switchToSession(linkBadge.dataset.sessionId, linkBadge.dataset.agentId, { tab });
        return;
      }
      // "In bin" chip on cross-catalog search rows — two-click restore.
      const binChip = e.target.closest('.sess-origin-badge.bin');
      if (binChip) {
        e.stopPropagation();
        _onBinChipClick(binChip);
        return;
      }
      // Live time-ago — click to flash the run status to its left for 1s
      // (e.g. "Completed 8m ago") instead of switching to the session.
      const timeEl = e.target.closest('.sess-title-time[data-ts]');
      if (timeEl) {
        e.stopPropagation();
        _flashStatus(timeEl);
        return;
      }
      _onRowClick(e);
    });
  }

  // Column resize (drag the right edge of any .col-resizable header).
  _wireColumnResize();
  _applyColWidths();

  // Session activity chart — mounted above the table wrap by _wireActivityChart.
  _wireActivityChart();

  // Column header sort — click any .col-sortable <th> to sort by that column.
  const thead = document.querySelector('#sessions-table thead');
  if (thead) {
    thead.addEventListener('click', (e) => {
      // Header controls live inside the sortable title cell.
      if (e.target.closest('.sessions-header-actions')) return;
      const th = e.target.closest('th.col-sortable');
      if (!th || !th.dataset.sortKey) return;
      // Don't intercept column-resize drags on the right edge.
      const rect = th.getBoundingClientRect();
      if (th.classList.contains('col-resizable') && e.clientX >= rect.right - 8) return;
      _setSort(th.dataset.sortKey);
    });
  }

  // Refresh when a session is mutated anywhere else (e.g. deleted from the chat
  // header / a session-row menu). Only reload while this tab is on screen;
  // startSessions() reloads on activation, so background fetches are wasteful.
  onSessionsChanged(() => {
    if (!_initialized) return;
    const tab = document.getElementById('tab-agents');
    if (tab && tab.classList.contains('active')) _loadAndRender('mutation');
  });

  // Chat switches are not database mutations. Reconcile the currently-rendered
  // rows immediately so rapid clicks can never leave the prior chat highlighted.
  onActiveSessionChanged((event) => {
    if (!_initialized) return;
    _syncActiveSessionRow(event?.detail?.sessionId);
  });

  // Header checkbox — toggle all on/off (this replaces the old toolbar
  // Select-all / Deselect-all buttons). Shares the one path with the footer's
  // SESSION-GUTTER-CHECK-ALL button (_toggleAllSelection).
  const checkAll = _qs('sessions-check-all');
  if (checkAll) {
    checkAll.addEventListener('click', () => { _toggleAllSelection(); });
  }

  // Trash / delete button — green recycle (open bin) when nothing is selected;
  // red trash (two-click confirm) to move selected to the bin, or — when
  // already in the bin — to permanently delete them. (HARMONY-SESSIONS-BIN)
  const trash = _qs('sessions-trash-btn');
  if (trash) {
    trash.addEventListener('click', () => {
      const n = _getSelectedSessionIds().length;
      if (n === 0) {
        // Nothing selected — in the plain Sessions view this opens the bin;
        // in the merged 'both' view the bin is already visible, so no-op.
        if (_viewId === 'sessions') { _switchView('recycle-bin'); return; }
        return;
      }
      const origins = _selectedOrigins();
      const binned = origins.size === 1 && origins.has('bin');
      let armTitle = binned ? 'Click again to permanently delete' : 'Click again to move to the bin';
      if (!binned && _viewId === 'both' && origins.has('bin')) {
        armTitle = 'Click again: recycle active, delete binned';
      }
      advanceDeleteBtn(trash, {
        size: '16px', spinSize: '16px',
        armTitle,
        armLabel: binned ? 'Confirm permanently deleting selected sessions' : 'Confirm moving selected sessions to the recycling bin',
        busyLabel: binned ? 'Permanently deleting selected sessions' : 'Moving selected sessions to the recycling bin',
        busyText: 'Deleting...',
        onConfirm: () => _deleteSelectedSessions(trash),
      });
      _syncFooterActions();
      setTimeout(_syncFooterActions, 450);
    });
  }

  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    document.querySelectorAll('.sessions-footer').forEach(footer => {
      window.lucide.createIcons({ nodes: [footer] });
    });
  }

  // FLAG filter chip — the footer is rendered in up to three places (the
  // Sessions page plus the two Agents copies; see the SESSION-STORAGE-METER
  // sync note above), so one helper paints every copy's pressed state from the
  // one piece of module state the table filter reads.
  const syncFooterFlagChips = () => {
    document.querySelectorAll('[data-sessions-footer-flag]').forEach(btn => {
      btn.dataset.state = _flagFilterOn ? 'on' : 'off';
      btn.setAttribute('aria-pressed', _flagFilterOn ? 'true' : 'false');
      btn.title = _flagFilterOn ? 'Show all sessions (including idle)' : 'Show only sessions with a flag';
    });
  };
  syncFooterFlagChips();

  // The footer no longer carries a search field of its own: the shared page-chat
  // launcher drives #sessions-search-input directly (see hiddenSearchSelector in
  // ui/shared/js/agents-page-chat-widget.js), so the engine listener wired below
  // stays the single entry point for the table's search.
  document.addEventListener('click', (e) => {
    const mode = e.target.closest('[data-sessions-footer-mode]');
    if (mode) {
      e.preventDefault();
      _qs('sessions-search-mode-btn')?.click();
      requestAnimationFrame(() => {
        const label = _qs('sessions-search-mode-btn')?.textContent || 'Title';
        document.querySelectorAll('[data-sessions-footer-mode]').forEach(btn => { btn.textContent = label; });
      });
      return;
    }
    const view = e.target.closest('[data-sessions-footer-view]');
    if (view) {
      e.preventDefault();
      _switchView(view.dataset.sessionsFooterView);
      return;
    }
    // Footer: FLAG filter chip — narrow the list to sessions carrying a flag.
    const flagChip = e.target.closest('[data-sessions-footer-flag]');
    if (flagChip) {
      e.preventDefault();
      _flagFilterOn = !_flagFilterOn;
      syncFooterFlagChips();
      _renderTable(_sessionsData);
      return;
    }

    // Footer: SESSION-GUTTER-CHECK-ALL — alternate select-all / clear-all. The
    // button is delegated (not bound per-element) because the shared Sessions
    // fragment moves between cards; see the footer sync note above.
    const checkAllBtn = e.target.closest('[data-sessions-footer-checkall]');
    if (checkAllBtn) {
      e.preventDefault();
      _toggleAllSelection();
      return;
    }

  });

  // More popover — refresh plus explicit Active / Recycled / All views.
  const moreTrigger = _qs('sessions-more-trigger');
  const morePanel = _qs('sessions-more-panel');
  const searchPanel = _qs('sessions-search-panel');
  const closePopovers = (except = null) => {
    [[moreTrigger, morePanel], [_qs('sessions-search-trigger'), searchPanel]].forEach(([trigger, panel]) => {
      if (!panel || panel === except) return;
      panel.hidden = true;
      if (trigger) {
        trigger.setAttribute('aria-expanded', 'false');
        trigger.classList.remove('active');
        if (trigger.id === 'sessions-search-trigger') trigger.title = 'Search sessions';
      }
    });
  };
  if (moreTrigger && morePanel) {
    moreTrigger.addEventListener('click', () => {
      const open = morePanel.hidden;
      closePopovers(open ? morePanel : null);
      morePanel.hidden = !open;
      moreTrigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    });
    morePanel.querySelectorAll('.sessions-view-option').forEach(btn => {
      btn.addEventListener('click', async () => {
        await _switchView(btn.dataset.view);
        closePopovers();
      });
    });
    document.addEventListener('pointerdown', (e) => {
      if (!e.target.closest('.sessions-header-actions')) closePopovers();
    });
  }

  // Search input — filter sessions by title as the user types. When a query is
  // active, results also surface from the OTHER catalog (sessions ↔ recycle
  // bin), fetched lazily so one search covers both views at once. A clear (×)
  // button on the right empties the field.
  const searchInput = _qs('sessions-search-input');
  const searchClear = _qs('sessions-search-clear');
  if (searchInput) {
    const syncSearchClear = () => {
      if (searchClear) searchClear.style.display = searchInput.value ? 'inline-flex' : 'none';
    };
    // Debounced: re-render 150ms after the user stops typing.
    let _searchTimer = null;
    searchInput.addEventListener('input', () => {
      clearTimeout(_searchTimer);
      _searchTimer = setTimeout(() => {
        _searchQuery = searchInput.value;
        _renderTable(_sessionsData);
        const q = _searchQuery.trim();
        if (q) {
          _ensureSearchExtra().then(() => {
            if (_searchQuery.trim() === q) {
              _renderTable(_sessionsData);
              _scanMessagesForQuery(q);
            }
          });
        }
      }, 150);
      syncSearchClear();
    });
    // Clear (×) — empty the field, drop the query, cancel any message scan,
    // and show the full view again.
    const clearSearch = () => {
      searchInput.value = '';
      _searchQuery = '';
      _msgScanBusy = null;          // stop the in-flight message scan
      _setMsgScanning(false);
      _renderTable(_sessionsData);
      syncSearchClear();
    };
    if (searchClear) {
      searchClear.addEventListener('click', () => {
        clearSearch();
        searchInput.focus();
      });
    }
    syncSearchClear();

    // Search trigger — open an anchored popover from the title heading.
    const searchTrigger = _qs('sessions-search-trigger');
    if (searchTrigger && searchPanel) {
      const setSearchOpen = (open) => {
        closePopovers(open ? searchPanel : null);
        searchPanel.hidden = !open;
        searchTrigger.classList.toggle('active', open);
        searchTrigger.setAttribute('aria-expanded', open ? 'true' : 'false');
        searchTrigger.title = open ? 'Close search' : 'Search sessions';
        if (open) searchInput.focus();
      };
      searchTrigger.addEventListener('click', () => {
        const open = searchPanel.hidden;
        if (!open) clearSearch();
        setSearchOpen(open);
      });
      searchInput.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && !searchPanel.hidden) {
          clearSearch();
          setSearchOpen(false);
        }
      });
    }
  }

  // Search-mode toggle — single button that switches between "Title" and "T+C".
  const searchModeBtn = _qs('sessions-search-mode-btn');
  if (searchModeBtn) {
    _updateSearchModeBtn();
    searchModeBtn.addEventListener('click', function() {
      _searchMode = _searchMode === 'title' ? 'content' : 'title';
      _saveViewState();
      _updateSearchModeBtn();
      if (_searchMode === 'title') {
        _msgScanBusy = null;
        _setMsgScanning(false);
      }
      _renderTable(_sessionsData);
      const q = _searchQuery.trim();
      if (q && _searchMode !== 'title') {
        _scanMessagesForQuery(q);
      }
    });
  }

  const undoBtn = _qs('sessions-undo-btn');
  if (undoBtn) {
    resetDeleteBtn(undoBtn, { size: '16px', title: 'Undo recycling selected sessions', label: 'Undo recycling selected sessions', iconName: 'undo-2' });
    undoBtn.addEventListener('click', () => {
      advanceDeleteBtn(undoBtn, {
        size: '16px', spinSize: '16px', iconName: 'undo-2',
        armTitle: 'Click again to undo recycling',
        busyTitle: 'Restoring…',
        armLabel: 'Confirm undoing recycling for selected sessions',
        busyLabel: 'Restoring selected sessions',
        onConfirm: () => _restoreSelected(),
      });
    });
  }
  const clearSelectionBtn = _qs('sessions-clear-selection-btn');
  if (clearSelectionBtn) clearSelectionBtn.addEventListener('click', _clearSelection);
  _wireSelectionPanelDrag();

  _wireSelectionColumnToggle();
  window.addEventListener('resize', function() { if (_activeConfig) _updateResponsiveColumns(); });
}

function _wireSelectionColumnToggle() {
  const _NARROW_WIDTH = 500;   // below this the view becomes title-only (see .narrow CSS)
  const cfg = _activeConfig ? getGutterConfig(_activeConfig) : { default_open: true, auto_collapse_at: 800 };
  const stickyTotal = _activeConfig ? getStickyTotalWidth(_activeConfig) : 86;
  const minOffset = -stickyTotal;
  const _tab = document.getElementById('sessions-section');
  const _inset = _tab ? (parseFloat(getComputedStyle(_tab).getPropertyValue('--sessions-content-inset')) || 20) : 20;
  const _tableWrap = document.getElementById('sessions-table-wrap');
  if (!_tableWrap) return;
  let _gutterOpen = cfg.default_open;
  let _narrowActive = false;
  let _preNarrowOpen = _gutterOpen;

  function _syncToggleButtons() {
    document.querySelectorAll('[data-sessions-footer-gutter]').forEach(btn => {
      const action = _gutterOpen ? 'Hide' : 'Show';
      btn.setAttribute('aria-pressed', String(_gutterOpen));
      btn.setAttribute('aria-label', `${action} selection column`);
      btn.title = `${action} selection column`;
      btn.dataset.state = _gutterOpen ? 'on' : 'off';
    });
    // SESSION-GUTTER-CHECK-ALL — the select-all twin lives next to the gutter
    // toggle and only exists while the selection column is open (there is
    // nothing to select when it is tucked away).
    document.querySelectorAll('[data-sessions-footer-checkall]').forEach(btn => {
      btn.hidden = !_gutterOpen;
    });
  }
  // Single open/close path — every call site (init, footer button,
  // auto-collapse, narrow mode) funnels through here so the state cannot drift.
  function _setGutterOpen(open) {
    _gutterOpen = open;
    document.documentElement.style.setProperty('--sticky-offset', (open ? 0 : minOffset) + 'px');
    _tableWrap.classList.toggle('gutter-closed', !open);
    _syncToggleButtons();
    _syncFooterCheckAll();
    // Opening/closing the checkbox changes how much of the view the title can
    // occupy — recompute the title width immediately while in narrow mode.
    if (_narrowActive) _updateNarrowMode();
  }
  _setGutterOpen(_gutterOpen);
  function _autoCollapse() {
    if (window.innerWidth <= cfg.auto_collapse_at && _gutterOpen) _setGutterOpen(false);
  }
  // ── Narrow title-only viewer ──────────────────────────────────────
  // Below the narrow threshold the sessions view becomes a pure session-title
  // viewer: the checkbox is auto-tucked away on entry (its pre-narrow state is
  // remembered and restored on widening). Showing the checkbox again makes the
  // title column narrower (its right edge stays
  // put at the view edge) instead of pushing it right. The title width
  // re-tracks the view on every resize while narrow; the scroll position is
  // only snapped to the left edge when ENTERING the mode, so a user who
  // scrolled right to inspect other columns isn't yanked back.
  function _viewWidth() {
    const w = _tableWrap ? _tableWrap.clientWidth : 0;
    return w > 0 ? w : window.innerWidth;   // clientWidth is 0 while the tab is hidden
  }
  // The checkbox side's ACTUAL rendered width (inset + sticky check column).
  // Measured, not config-derived: the check <th> carries an inline
  // min-width:48px from _buildThead that beats the stylesheet's 32px, and
  // stickyTotal is captured before _activeConfig loads (stale fallback 86).
  // The check cell is sticky at the left edge, so its width is stable at any
  // scroll position.
  function _measureCheckWidth() {
    if (_tableWrap) {
      const el = _tableWrap.querySelector('th.col-check, td.col-check');
      if (el) {
        const w = el.getBoundingClientRect().width;
        if (w > 0) return w;
      }
    }
    return stickyTotal;
  }
  function _updateNarrowMode() {
    const viewW = _viewWidth();
    // Mobile navigation deliberately treats this as a title-first view even
    // when the viewport is wider than the desktop narrow threshold.
    const mobileTitleOnly = document.body.classList.contains('mobile-mode');
    if (viewW <= _NARROW_WIDTH || mobileTitleOnly) {
      if (!_narrowActive) {
        _narrowActive = true;
        _preNarrowOpen = _gutterOpen;
        _setGutterOpen(false);
        if (_tableWrap) { _tableWrap.classList.add('narrow'); _tableWrap.scrollLeft = 0; }
      }
      // The title column must end exactly at the view's right edge. Its left
      // edge sits at (inset + checkbox column) when the gutter is open, or 0
      // when the checkbox is tucked away (gutter-closed collapses the column
      // width and the inner's left padding). The checkbox width is measured
      // from the live DOM (see _measureCheckWidth) — config totals and the
      // pre-config fallback are both wrong for the real rendered column.
      // Showing the gutter makes the title narrower instead of pushing it right.
      const titleLeft = _gutterOpen ? (_inset + _measureCheckWidth()) : 0;
      const maxTitleW = mobileTitleOnly ? viewW : _NARROW_WIDTH;
      const titleW = Math.max(48, Math.min(viewW - titleLeft, maxTitleW));
      document.documentElement.style.setProperty('--sessions-title-width', titleW + 'px');
    } else if (_narrowActive) {
      _narrowActive = false;
      _setGutterOpen(_preNarrowOpen && window.innerWidth > cfg.auto_collapse_at);
      document.documentElement.style.setProperty('--sessions-title-width', '');
      if (_tableWrap) _tableWrap.classList.remove('narrow');
    }
  }
  _narrowUpdater = _updateNarrowMode;
  // Watch the table wrap's box size directly — NOT just window resize. The
  // #chat-resize-handle drag (ui/shared/js/chatResize.js) resizes #main-panel
  // by setting chat-panel width inline and never fires a window resize event,
  // so only an element-level observer catches the split-layout drag path.
  if (typeof ResizeObserver !== 'undefined' && _tableWrap) {
    new ResizeObserver(function() {
      _updateResponsiveColumns();
      _updateNarrowMode();
    }).observe(_tableWrap);
  }
  window.addEventListener('resize', function() { _autoCollapse(); _updateNarrowMode(); });
  _autoCollapse();
  _updateNarrowMode();
  document.querySelectorAll('[data-sessions-footer-gutter]').forEach(btn => {
    btn.addEventListener('click', function(e) {
      e.preventDefault();
      _setGutterOpen(!_gutterOpen);
    });
  });
}

// ── Exports (matches the start/stop pattern used by other tabs) ────

export function startSessions() {
  if (!app.currentUserId) return;
  if (_sourceAccessDenied) {
    stopSessions();
    _renderAccessDeniedState();
    return;
  }
  const section = _qs('sessions-section');
  if (section) delete section.dataset.accessBlocked;
  _wireDom();
  // Paint the footer storage meter immediately — it must not wait for the
  // (possibly slow) session catalog request.
  _refreshStorageUsage();
  // On a cold boot the All Agents cache may still be hydrating. Never let an
  // asynchronous cache completion replace a selected-agent skeleton or a newer
  // context with account-wide rows.
  const cacheViewId = _viewId;
  const cacheAgentFilterId = _activeAgentFilter();
  const cacheNativeContext = _nativeCodexContext;
  ensureAgentCacheHydrated().then(async () => {
    if (cacheAgentFilterId || _viewId !== cacheViewId || _activeAgentFilter() !== cacheAgentFilterId || _nativeCodexContext !== cacheNativeContext) return;
    if (_sessionsDataViewId === cacheViewId && _sessionsDataAgentId === cacheAgentFilterId) return;
    const key = _cacheKey(cacheViewId, cacheAgentFilterId, cacheNativeContext);
    const rows = key ? readAgentCache(key) : null;
    if (!Array.isArray(rows)) return;
    const config = await loadViewerConfig(cacheViewId);
    if (_viewId !== cacheViewId || _activeAgentFilter() !== cacheAgentFilterId || _nativeCodexContext !== cacheNativeContext) return;
    if (_sessionsDataViewId === cacheViewId && _sessionsDataAgentId === cacheAgentFilterId) return;
    _activeConfig = config;
    _sessionsData = rows;
    _sessionsDataViewId = cacheViewId;
    _sessionsDataAgentId = cacheAgentFilterId;
    // Seed the canonical account-wide snapshot so the first agent click can
    // filter locally even before the live request returns.
    _writeSharedSnapshot(cacheViewId, rows);
    _renderTable(rows);
    _markSessionsPaint('indexeddb');
  }).catch(() => {});
  _loadAndRender();
  // Kill switch: refresh immediately when it toggles so running spinners
  // clear (or return) without waiting for the 30s auto-refresh.
  if (!_onKsChanged) {
    _onKsChanged = () => { try { _loadAndRender(); } catch (_) { /* ignore */ } };
    window.addEventListener('kill-switch-changed', _onKsChanged);
  }
  // Re-measure the narrow title-only viewer now that the tab is visible
  // (it is display:none while inactive, so clientWidth is only meaningful
  // at activation time).
  if (_narrowUpdater) _narrowUpdater();

  // Start auto-refresh. SESSIONS-LOAD-INTENT: this passes 'background' so the
  // passive 30s pass never resets the view — it keeps the resolved title and
  // rows on screen and repaints only when the data actually changed.
  if (!_refreshTimer) {
    _refreshTimer = setInterval(() => _loadAndRender('background'), _REFRESH_INTERVAL_MS);
  }
  // Start the 1s live ticker for the relative time-ago labels. Runs only while
  // this tab is initialized — stopSessions() clears it.
  if (!_tickTimer) {
    _tickTimer = setInterval(_tickRelativeTimes, 1000);
  }
}

export function stopSessions() {
  if (_refreshTimer) {
    clearInterval(_refreshTimer);
    _refreshTimer = null;
  }
  if (_tickTimer) {
    clearInterval(_tickTimer);
    _tickTimer = null;
  }
  if (_onKsChanged) {
    window.removeEventListener('kill-switch-changed', _onKsChanged);
    _onKsChanged = null;
  }
}

// Aliases for backward compat
const activateSessionsPage = startSessions;
const initSessionsPage = startSessions;

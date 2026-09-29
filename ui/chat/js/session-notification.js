'use strict';

// Session notifications — one state model, two surfaces.
//
// A notification IS an unread, settled session. The row set is derived from
// _sessionsCache with the SAME predicate the Agents → Sessions page chip uses
// (sessionAckFlag: has_unread AND a settled flag), so the panel and that page
// list the same sessions by construction. They used to disagree: the panel was
// an EVENT log (only the running → done transitions observed while the tab was
// open, so a small, recent, mostly-green subset) while the page is a STATE view
// (every unread session).
//
// Lifetime is the DB's own read state, never client-only bookkeeping:
//   • mark read — an EXPLICIT acknowledgement only: the row's ✕, "Clear all",
//     the session chip, or the row menu — writes sessions.read_at, and the row
//     goes. Merely OPENING a session does not acknowledge it (see
//     ui/chat/js/session-load.js), so flipping through sessions is safe;
//   • mark unread writes sessions.unread_at, and the same row comes back.
// One acknowledgement therefore serves both surfaces, and it follows the user
// across devices (read_at lives in the per-user DB that hybrid sync replicates).
//
//   • Desktop — a strip of inline pills in the header's right cluster
//     (#session-notification-inline in index.html, immediately left of the
//     Recorder control): one clickable element per notification, each carrying a
//     flag glyph + label, the truncated session name, the closer's note and a
//     live timer, and opening that session exactly like a panel row does.
//   • Both layouts — the header counter beside the "Chat" label
//     (#chat-notification-toggle-btn, authored in index.html immediately left of
//     #chat-toggle-btn) shows the unread count and toggles the draggable panel
//     (#session-notification-container). Desktop is running the panel AND the
//     pills at once (dual notifications), so the counter is not a mobile-only
//     control; at zero it falls back to its plain chat-opener role.
// Module map: ui/chat/js/README.md.

import { _sessionsCache, _formatRelativeTime, sessionDropdownStore } from './session-list.js';
import { switchToSession } from './session-core.js?v=515';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import {setNotificationUnread} from '../core/notifications.js';
import {
  flagLabel,
  resolveSessionFlag,
  sessionAckFlag,
  closingCta,
} from '../../shared/js/session-flag.js';

// ── State ──────────────────────────────────────────────────────────────────

/** Map<sid, { title, timerAt }> of active notification rows */
const _activeRows = new Map();

/** True once the session-list store subscription is installed */
let _listSubscribed = false;

/** Panel DOM refs set after _ensurePanel() */
let _panel = null;
let _panelFloating = false;
let _outsideCloseListening = false;
let _sessionVisitListening = false;
let _resizeListening = false;
let _timeAgoTicker = null;

// ── App-wide on/off gate ─────────────────────────────────────────────────────
// The notification panel is an APP-LEVEL helper, so it is NOT gated on the
// current chat agent's abilities. It IS gated on one app-wide admin switch:
// App Settings → App Functions → "Session completion notifications"
// (session_completion_notifications in app-settings.json), served to every
// visitor via the public /api/v1/auth/ui-config and read once at boot (see
// initSessionNotification). Defaults ON — the panel has always been available,
// so a missing/unreachable config keeps it on (fail-open).
let _enabledFlag = true;

function _enabled() {
  return _enabledFlag;
}

// Fetch the app-wide on/off flag once from the public ui-config. Sends the auth
// token when present so it behaves identically on authenticated pages (mirrors
// appearance.js). Best-effort: any failure leaves the
// flag at its ON default.
function _loadEnabledFlag() {
  let headers = {};
  try {
    const t = localStorage.getItem('auth_token');
    if (t) headers = { Authorization: 'Bearer ' + t };
  } catch (_) { /* ignore */ }
  window.__fetchUiConfig({ headers })
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      if (cfg && typeof cfg.session_completion_notifications === 'boolean') {
        _enabledFlag = cfg.session_completion_notifications;
      }
    })
    .catch(() => { /* keep the ON default */ });
}

// ── Read state — the row lifetime ──────────────────────────────────────────
// A notification lives exactly as long as its session is UNREAD, and that state
// is the DB's own (sessions.read_at / unread_at, written by the /read and
// /unread endpoints in app/api/db_viewer.py). Dismissing a notification is
// therefore the same write the Sessions-page chip performs — one acknowledgement
// shared by both surfaces.
//
// This retired the per-user session_notifications table as the panel's source of
// truth: /api/v1/db/session-notifications is no longer called from the UI. The
// table and its endpoints stay in place for existing deployments and for the
// hybrid-sync payloads, but no panel behaviour depends on them.

function _currentUserId() {
  return (window.app && window.app.currentUserId) || '';
}

/** Mark one session read — the acknowledgement that clears its notification.
 *  Dispatches session:visited with the DB-confirmed timestamp so the session
 *  list repaints from the same source of truth, exactly as the dropdown's own
 *  markSessionRead does. */
async function _markSessionRead(sid) {
  if (await setNotificationUnread(sid,false)) {
    const payload={ok:true,session_id:sid,read_at:new Date().toISOString()};
    window.dispatchEvent(new CustomEvent('session:visited', {
      detail: { sessionId:sid, visitedAt:payload.read_at },
    }));
    return payload;
  }
  const res = await fetch(apiPath(
    `/api/v1/db/sessions/${encodeURIComponent(sid)}/read`
    + `?db=user.db&user_id=${encodeURIComponent(_currentUserId())}`,
  ), {
    method: 'POST',
    headers: authHeaders(),
  });
  if (!res.ok) throw new Error(`mark read failed: ${res.status}`);
  const payload = await res.json().catch(() => ({}));
  window.dispatchEvent(new CustomEvent('session:visited', {
    detail: { sessionId: sid, visitedAt: payload.read_at || new Date().toISOString() },
  }));
  return payload;
}

// ── Panel DOM ──────────────────────────────────────────────────────────────

const CHECK_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
const X_SVG = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/></svg>';
const BELL_SVG = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8A6 6 0 0 0 6 8c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.73 21a2 2 0 0 1-3.46 0"/></svg>';

// ── Flag glyphs ────────────────────────────────────────────────────────────
// ONE glyph per flag, shared by the panel rows and the desktop pills, so a
// shape names a flag on every notification surface. On a row the glyph is what
// carries the flag's identity: its SHAPE names the flag and its TONE colour
// (the .sn-row[data-flag] rules) says how loud it is.
// SYNC: FLAG_LABELS / FLAG_TONES in ui/shared/js/session-flag.js — a new flag
// needs a glyph here, a label+tone there, and a [data-flag] tone rule per surface.
const _flagSvg = (body) => '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">' + body + '</svg>';
const FLAG_SVGS = {
  // Finished cleanly.
  complete: _flagSvg('<polyline points="20 6 9 17 4 12"/>'),
  // Finished, but the agent is asking the user something.
  needs_input: _flagSvg('<circle cx="12" cy="12" r="9"/><path d="M9.6 9.4a2.5 2.5 0 1 1 3.5 2.3c-.7.4-1.1 1-1.1 1.8"/><line x1="12" y1="17" x2="12" y2="17.05"/>'),
  // Finished, but only part of the work.
  partial: _flagSvg('<circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 1 0 18z" fill="currentColor" stroke="none"/>'),
  // Finished, but the agent could not verify it.
  unverified: _flagSvg('<path d="M12 3l7 3v6c0 4.1-2.9 7.5-7 9-4.1-1.5-7-4.9-7-9V6z"/><path d="M9.9 10.4a2.2 2.2 0 1 1 3.1 2c-.6.35-.95.9-.95 1.6"/><line x1="12" y1="16.6" x2="12" y2="16.65"/>'),
  // Failed.
  error: _flagSvg('<path d="M12 4l9 16H3z"/><line x1="12" y1="10" x2="12" y2="14.5"/><line x1="12" y1="17.4" x2="12" y2="17.45"/>'),
  // Stopped on purpose — not a failure.
  interrupted: _flagSvg('<rect x="6" y="6" width="12" height="12" rx="2.5"/>'),
  // Still working.
  running: _flagSvg('<path d="M12 3a9 9 0 1 0 9 9"/>'),
  // Waiting for a slot.
  queued: _flagSvg('<circle cx="12" cy="12" r="9"/><polyline points="12 7 12 12 15.5 14"/>'),
};

/** The glyph for a flag; an off-taxonomy flag falls back to the check. */
function _flagIcon(flag) {
  const key = String(flag || '').trim().toLowerCase();
  return FLAG_SVGS[key] || CHECK_SVG;
}

// Match the dropdown's "Last active" timer. The backend derives activity_at
// from MAX(interactions.created_at), i.e. the latest message in this session.
function _sessionTimerAt(session, fallback = '') {
  return session?.activity_at
    || session?.updated_at
    || session?.created_at
    || fallback
    || new Date().toISOString();
}

// Live relative-time labels: the mobile panel's rows and the desktop pills share
// one 1s tick (see _syncTicker).
function _refreshTimeAgoLabels() {
  for (const scope of [_panel, document.getElementById(INLINE_HOST_ID)]) {
    if (!scope) continue;
    for (const timeEl of scope.querySelectorAll('[data-ts]')) {
      const label = _formatRelativeTime(timeEl.dataset.ts);
      if (timeEl.textContent !== label) timeEl.textContent = label;
    }
  }
}

function _setTimeAgoTicker(active) {
  if (_timeAgoTicker) {
    clearInterval(_timeAgoTicker);
    _timeAgoTicker = null;
  }
  if (!active || _activeRows.size === 0) return;
  _refreshTimeAgoLabels();
  _timeAgoTicker = window.setInterval(_refreshTimeAgoLabels, 1000);
}

function _ensurePanel() {
  if (_panel && document.body.contains(_panel)) return _panel;

  let container = document.getElementById('session-notification-container');
  if (!container) {
    container = document.createElement('div');
    container.id = 'session-notification-container';
  }
  // Move to <body> so it escapes #chat-panel stacking context
  if (container.parentNode !== document.body) {
    document.body.appendChild(container);
  }
  // Rows may be restored or added in the background, but the panel only opens
  // when the user explicitly toggles it from the header notification counter.
  container.hidden = true;

  // Only build the inner structure once
  if (container.querySelector('.sn-header')) {
    _panel = container;
    return _panel;
  }

  container.innerHTML = '';

  // ── Header ──
  const header = document.createElement('div');
  header.className = 'sn-header';

  const icon = document.createElement('span');
  icon.className = 'sn-head-icon';
  icon.innerHTML = BELL_SVG;
  header.appendChild(icon);

  const title = document.createElement('span');
  title.className = 'sn-title';
  title.textContent = 'Notifications';
  header.appendChild(title);

  const count = document.createElement('span');
  count.className = 'sn-count';
  count.textContent = '0';
  header.appendChild(count);

  // "Clear all" sits immediately left of the close X, so the header's top-right
  // action group reads [count] [Clear all] [X]. It dismisses every visible row
  // (see _clearAll) and is hidden at zero rows, mirroring the count badge.
  const clearAllBtn = document.createElement('button');
  clearAllBtn.type = 'button';
  clearAllBtn.className = 'sn-clear-all';
  clearAllBtn.textContent = 'Clear all';
  clearAllBtn.title = 'Clear all notifications';
  clearAllBtn.setAttribute('aria-label', 'Clear all notifications');
  clearAllBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _clearAll();
  });
  header.appendChild(clearAllBtn);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'sn-icon-btn';
  closeBtn.title = 'Close notifications';
  closeBtn.setAttribute('aria-label', 'Close notifications');
  closeBtn.innerHTML = X_SVG;
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _hidePanel();
  });
  header.appendChild(closeBtn);

  // Drag to move
  header.addEventListener('pointerdown', _onHeaderDown);

  container.appendChild(header);

  // ── Body ──
  const body = document.createElement('div');
  body.className = 'sn-body';
  container.appendChild(body);

  _panel = container;
  _updateCount();
  return _panel;
}

function _buildRow(sid, title, timerAt) {
  const row = document.createElement('div');
  row.className = 'sn-row';
  row.dataset.sid = sid;

  // The flag glyph — the sole carrier of the flag on a row (see _applyRowFlag).
  const icon = document.createElement('span');
  icon.className = 'sn-row-icon';
  row.appendChild(icon);

  const textWrap = document.createElement('span');
  textWrap.className = 'sn-row-text';

  // Line 1 — the session NAME.
  const titleEl = document.createElement('span');
  titleEl.className = 'sn-row-title';
  titleEl.textContent = title || 'New Session';

  // Line 2 — the closer's own note for this run (the call-to-action it wrote),
  // hidden when the session carries none.
  const noteEl = document.createElement('span');
  noteEl.className = 'sn-row-note';
  noteEl.hidden = true;

  // Line 3 — the live relative-time counter.
  const timeEl = document.createElement('time');
  timeEl.className = 'sn-row-time';
  timeEl.dataset.ts = timerAt || new Date().toISOString();
  timeEl.dateTime = timeEl.dataset.ts;
  timeEl.textContent = _formatRelativeTime(timeEl.dataset.ts);
  timeEl.title = timeEl.dataset.ts;

  textWrap.appendChild(titleEl);
  textWrap.appendChild(noteEl);
  textWrap.appendChild(timeEl);
  row.appendChild(textWrap);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'sn-row-close';
  closeBtn.setAttribute('aria-label', 'Dismiss');
  closeBtn.innerHTML = X_SVG;
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _dismissRow(sid);
  });
  row.appendChild(closeBtn);

  // Click the row → switch to session WITHOUT acknowledging it. Opening a
  // session never clears its notification: only the row's ✕, "Clear all" or the
  // session's own flag chip writes read_at, so the user can glance through
  // sessions and still find the unread ones waiting for them.
  row.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    switchToSession(sid);
  });

  // Stamp the flag LAST, so the glyph, its tone and the note all come from the
  // one resolver.
  _applyRowFlag(row, sid);
  return row;
}

/** Stamp a row with its session's CURRENT resolved flag — the glyph, the tone
 *  hook (data-flag) and the closer's note. Called at build time AND again on
 *  every poll (see checkSessionCompletions): a run's closer record lands a beat
 *  after its status flips, so a row built on the transition would otherwise
 *  freeze at the runtime fallback and never repaint its true flag. */
function _applyRowFlag(row, sid) {
  const session = _sessionsCache.find((s) => s.id === sid);
  const flag = resolveSessionFlag(session) || 'complete';
  const label = flagLabel(flag);
  const note = closingCta(session);
  const nameEl = row.querySelector('.sn-row-title');
  const name = nameEl ? nameEl.textContent : '';

  row.dataset.flag = flag;

  const icon = row.querySelector('.sn-row-icon');
  if (icon) icon.innerHTML = _flagIcon(flag);

  const noteEl = row.querySelector('.sn-row-note');
  if (noteEl) {
    noteEl.textContent = note;
    noteEl.hidden = !note;
  }

  // The flag still reads to assistive tech, even though the visible row leads
  // with the session name rather than the flag label.
  row.title = [label, name, note].filter(Boolean).join(' · ');
  row.setAttribute('aria-label', `${label}: ${name}. Open session.`);
}

/** Re-stamp every live panel row against the current cache. The desktop pills
 *  are rebuilt on each render; this keeps the panel rows in step with them so
 *  the two surfaces can never disagree about a session's flag. */
function _refreshActiveFlags() {
  if (!_panel) return;
  for (const sid of _activeRows.keys()) {
    const row = _panel.querySelector(`.sn-row[data-sid="${CSS.escape(sid)}"]`);
    if (row) _applyRowFlag(row, sid);
  }
}

// ── Desktop inline pills (header strip) ────────────────────────────────────
// The desktop header has no room for a floating panel beside it, so the same
// notification set renders as a strip of clickable pills inside #status-right.
// index.html authors #session-notification-inline immediately left of the
// Recorder control; this module only fills it in.

const INLINE_HOST_ID = 'session-notification-inline';

/** True on the desktop layout — the 800px breakpoint __isMobileChatLayout and
 *  the chat header/footer configs already share. */
function _isDesktopLayout() {
  if (typeof window === 'undefined') return false;
  if (typeof window.__isMobileChatLayout === 'function') return !window.__isMobileChatLayout();
  return window.innerWidth > 800;
}

// Status flag vocabulary for the pills. The flag is the SHARED session flag
// (ui/shared/js/session-flag.js): the closer's own verdict when the session
// carries one, with the runtime `run_status` as the fallback. The pill shows the
// resolved flag WITHOUT the unread gate the list chips apply (`sessionAckFlag`):
// every notification is unread by definition, so the plain resolved flag is the
// right label here.
// DESIGN SYNC: the flag colours and labels track the .sess-status badges in
// ui/main-panel/agents/sessions/sessions.css.
function _pillFlag(sid) {
  const session = _sessionsCache.find((s) => s.id === sid);
  return resolveSessionFlag(session) || 'complete';
}

/** The header pill strip. Authored in index.html; rebuilt defensively so a hot
 *  reload or a rebuilt header cannot leave the notifications homeless. */
function _inlineHost() {
  let host = document.getElementById(INLINE_HOST_ID);
  if (host) return host;
  const right = document.getElementById('status-right');
  if (!right) return null;
  host = document.createElement('span');
  host.id = INLINE_HOST_ID;
  host.className = 'session-notification-inline';
  host.hidden = true;
  right.insertBefore(host, document.getElementById('render-recorder-control') || right.firstChild);
  return host;
}

function _buildPill(sid, title, timerAt) {
  const flag = _pillFlag(sid);
  const labelText = flagLabel(flag);
  const name = title || 'New Session';
  // The closer's own short note for this run (the call-to-action it wrote), when
  // the session carries one.
  const note = closingCta(_sessionsCache.find((s) => s.id === sid));

  const pill = document.createElement('div');
  pill.className = 'sn-pill';
  pill.dataset.sid = sid;
  pill.dataset.flag = flag;
  pill.setAttribute('role', 'button');
  pill.tabIndex = 0;
  pill.title = [labelText, name, note].filter(Boolean).join(' · ');
  pill.setAttribute('aria-label', `${labelText}: ${name}. Open session.`);

  // The flag's own glyph, as the item's LEFT RAIL — the same shape the panel
  // rows use, so one glyph names one flag across both notification surfaces.
  const glyphEl = document.createElement('span');
  glyphEl.className = 'sn-pill-icon';
  glyphEl.innerHTML = _flagIcon(flag);
  pill.appendChild(glyphEl);

  // Two stacked text lines beside the rail — the same anatomy as a panel row and
  // a sessions-page row: line 1 is the flag's chip + the session name, line 2
  // carries the closer's note and the live time-ago BELOW the name.
  const textEl = document.createElement('span');
  textEl.className = 'sn-pill-text';

  const topLine = document.createElement('span');
  topLine.className = 'sn-pill-line';

  const flagEl = document.createElement('span');
  flagEl.className = 'sn-pill-flag';
  flagEl.textContent = labelText;
  topLine.appendChild(flagEl);

  // The session name is truncated by CSS ellipsis inside the pill's max width.
  const titleEl = document.createElement('span');
  titleEl.className = 'sn-pill-name';
  titleEl.textContent = name;
  topLine.appendChild(titleEl);
  textEl.appendChild(topLine);

  const subLine = document.createElement('span');
  subLine.className = 'sn-pill-line sn-pill-line-sub';

  // The closer's own note for this run (the call-to-action it wrote), beneath the
  // name. Absent for a session that wrote none, leaving the timer alone on the line.
  if (note) {
    const noteEl = document.createElement('span');
    noteEl.className = 'sn-pill-note';
    noteEl.textContent = note;
    subLine.appendChild(noteEl);
  }

  const timeEl = document.createElement('time');
  timeEl.className = 'sn-pill-time';
  timeEl.dataset.ts = timerAt || new Date().toISOString();
  timeEl.dateTime = timeEl.dataset.ts;
  timeEl.textContent = _formatRelativeTime(timeEl.dataset.ts);
  timeEl.title = timeEl.dataset.ts;
  subLine.appendChild(timeEl);
  textEl.appendChild(subLine);

  pill.appendChild(textEl);

  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'sn-pill-close';
  closeBtn.setAttribute('aria-label', `Dismiss notification: ${name}`);
  closeBtn.innerHTML = X_SVG;
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _dismissRow(sid);
  });
  pill.appendChild(closeBtn);

  // Opening the session never acknowledges it — the pill stays put until the
  // user clears it explicitly (its ✕, "Clear all", or the session's flag chip).
  const openSession = () => {
    switchToSession(sid);
  };
  // Click the pill (but not its dismiss button) → open that session, exactly
  // like a notification row in the mobile panel.
  pill.addEventListener('click', (e) => {
    if (e.target.closest('button')) return;
    openSession();
  });
  pill.addEventListener('keydown', (e) => {
    if (e.target.closest('button')) return;
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      openSession();
    }
  });

  return pill;
}

/** Reconcile the header pill strip against the active notification set, newest
 *  activity first — the same order the panel rows use. */
function _renderInline() {
  const host = _inlineHost();
  if (!host) return;
  if (!_isDesktopLayout()) {
    host.textContent = '';
    host.hidden = true;
    return;
  }
  const entries = [..._activeRows.entries()]
    .map(([sid, entry], index) => ({
      sid,
      entry,
      index,
      at: Date.parse((entry && entry.timerAt) || '') || 0,
    }))
    .sort((a, b) => (b.at - a.at) || (a.index - b.index));

  host.textContent = '';
  for (const { sid, entry } of entries) {
    host.appendChild(_buildPill(sid, entry && entry.title, entry && entry.timerAt));
  }
  host.hidden = entries.length === 0;
}

/** The live 1s relative-time tick runs while ANY notification label is on
 *  screen: the mobile panel while it is open, or the desktop pills whenever they
 *  have rows. */
function _syncTicker() {
  const panelOpen = !!(_panel && !_panel.hidden);
  const pillsVisible = _isDesktopLayout() && _activeRows.size > 0;
  _setTimeAgoTicker(panelOpen || pillsVisible);
}

function _updateCount() {
  const count = _activeRows.size;
  if (_panel) {
    const countEl = _panel.querySelector('.sn-count');
    if (countEl) countEl.textContent = String(count);
    // "Clear all" only makes sense with rows to clear — same gate as the badge.
    const clearAllBtn = _panel.querySelector('.sn-clear-all');
    if (clearAllBtn) clearAllBtn.hidden = count === 0;
  }

  // The counter beside the "Chat" label is the notification target on BOTH
  // layouts: desktop keeps its inline pills AND gains the counter, so clicking
  // it opens the same panel mobile uses (dual notifications). At zero the icon
  // keeps its plain chat-opener role on either layout.
  const notificationToggle = document.getElementById('chat-notification-toggle-btn');
  const counter = notificationToggle
    && notificationToggle.querySelector('.chat-notification-counter');
  const chatAction = 'Open chat panel';
  if (notificationToggle) {
    if (counter) counter.textContent = String(count);
    notificationToggle.classList.toggle('has-notifications', count > 0);

    const notificationAction = 'Toggle notification panel';
    const notificationLabel = count === 1 ? '1 notification' : `${count} notifications`;
    notificationToggle.title = count > 0
      ? `${notificationLabel} — ${notificationAction}`
      : chatAction;
    notificationToggle.setAttribute(
      'aria-label',
      count > 0 ? `${notificationLabel}. ${notificationAction}` : chatAction,
    );
  }

  _renderInline();
  _syncTicker();
}

/** Hide the notification panel without affecting any notification rows. */
function _hidePanel() {
  if (_panel) _panel.hidden = true;
  _syncTicker();
}

/** Close an open notification panel when the user interacts outside it. */
function _onOutsidePointerDown(event) {
  if (!_panel || _panel.hidden) return;
  const target = event.target;
  if (!(target instanceof Node)) return;
  if (_panel.contains(target)
      || target.closest('#chat-toggle-btn, #chat-notification-toggle-btn')) return;
  _hidePanel();
}

function _installOutsideClose() {
  if (_outsideCloseListening || typeof document === 'undefined') return;
  _outsideCloseListening = true;
  document.addEventListener('pointerdown', _onOutsidePointerDown);
}

/** Crossing the desktop/mobile breakpoint swaps which notification surface
 *  applies, so re-render the one that now fits (pills ↔ panel). Both layouts
 *  host the panel now, so a crossing only re-renders — the panel is not dropped. */
function _installResizeListener() {
  if (_resizeListening || typeof window === 'undefined') return;
  _resizeListening = true;
  let lastDesktop = _isDesktopLayout();
  window.addEventListener('resize', () => {
    const nowDesktop = _isDesktopLayout();
    if (nowDesktop === lastDesktop) return;
    lastDesktop = nowDesktop;
    _updateCount();
  });
}

/** An EXPLICIT acknowledgement — the flag chip's cycle, a row's ✕, or "Clear
 *  all" — dispatches session:visited, and this local removal keeps the counter
 *  responsive immediately instead of waiting for the next reconcile. Opening a
 *  session no longer dispatches it (see ui/chat/js/session-load.js), so a glance
 *  through sessions leaves their notification rows in place. */
function _installSessionVisitListener() {
  if (_sessionVisitListening || typeof window === 'undefined') return;
  _sessionVisitListening = true;
  window.addEventListener('session:visited', (event) => {
    const sid = event && event.detail && event.detail.sessionId;
    if (typeof sid === 'string' && sid) _removeRow(sid);
  });
  window.addEventListener('session:unread', () => {
    // Marking a session unread again restores its notification. session-list.js
    // applies the read-state override in its OWN listener on this same event, so
    // defer a tick and then reconcile against the already-updated cache.
    window.setTimeout(_syncRowsFromCache, 0);
  });
}

/**
 * Toggle the notification panel. This is exposed to the main header so the
 * counter is the only way notification rows are shown; panel visibility never
 * changes the underlying notification count.
 */
export function toggleSessionNotificationPanel() {
  // Both layouts open the same panel: desktop additionally shows the inline
  // pills, and the header counter is the shared toggle for the panel.
  if (_activeRows.size === 0) return;
  const panel = _ensurePanel();
  const opening = panel.hidden;
  panel.hidden = !opening;
  _syncTicker();
}

window.__toggleSessionNotificationPanel = toggleSessionNotificationPanel;

// ── Drag to move (same pattern as chat-widget.js) ──────────────────────────

function _makeFloating() {
  if (_panelFloating) return;
  const r = _panel.getBoundingClientRect();
  _panelFloating = true;
  _panel.classList.add('sn-floating');
  _panel.style.left = r.left + 'px';
  _panel.style.top = r.top + 'px';
  _panel.style.right = 'auto';
}

function _dragLoop(onMove) {
  const up = () => {
    document.removeEventListener('pointermove', onMove);
    document.removeEventListener('pointerup', up);
    document.body.classList.remove('sn-dragging');
  };
  document.body.classList.add('sn-dragging');
  document.addEventListener('pointermove', onMove);
  document.addEventListener('pointerup', up);
}

function _onHeaderDown(e) {
  if (e.target.closest('.sn-icon-btn, .sn-clear-all')) return;  // header buttons
  if (e.button !== 0) return;
  e.preventDefault();
  _makeFloating();
  const r = _panel.getBoundingClientRect();
  const ox = e.clientX - r.left, oy = e.clientY - r.top;
  _dragLoop((ev) => {
    const nx = Math.max(0, Math.min(ev.clientX - ox, window.innerWidth - 48));
    const ny = Math.max(0, Math.min(ev.clientY - oy, window.innerHeight - 30));
    _panel.style.left = nx + 'px';
    _panel.style.top = ny + 'px';
  });
}

// ── Row lifecycle ──────────────────────────────────────────────────────────

/** Keep notification rows in the same newest-activity-first order as the
 *  session dropdown. Ties retain their existing DOM order. */
function _sortRowsByRecency() {
  const body = _panel && _panel.querySelector('.sn-body');
  if (!body) return;

  [...body.querySelectorAll('.sn-row')]
    .map((row, index) => {
      const entry = _activeRows.get(row.dataset.sid);
      return {
        row,
        index,
        timestamp: Date.parse((entry && entry.timerAt) || '') || 0,
      };
    })
    .sort((a, b) => (b.timestamp - a.timestamp) || (a.index - b.index))
    .forEach(({ row }) => body.appendChild(row));
}

function _addRow(sid, title, timerAt) {
  if (_activeRows.has(sid)) return;
  _ensurePanel();
  const timestamp = timerAt || new Date().toISOString();
  const body = _panel.querySelector('.sn-body');
  const row = _buildRow(sid, title, timestamp);
  body.appendChild(row);
  _activeRows.set(sid, { title, timerAt: timestamp });
  _sortRowsByRecency();
  _refreshTimeAgoLabels();
  _updateCount();
}

function _removeRow(sid) {
  const row = _panel && _panel.querySelector(`.sn-row[data-sid="${CSS.escape(sid)}"]`);
  if (row) row.remove();
  _activeRows.delete(sid);
  _updateCount();
  // Hide panel when empty and stop its live relative-time ticker.
  if (_activeRows.size === 0) _hidePanel();
}

/** User-initiated dismissal: remove the row immediately (optimistic) and mark
 *  the session read in the per-user DB. That is the SAME acknowledgement the
 *  Sessions-page chip writes, so dismissing here clears the chip there, and the
 *  clear follows the user to every device that reads the same session state. */
function _dismissRow(sid) {
  _removeRow(sid);
  _markSessionRead(sid).catch(() => {
    // Transient — the next reconcile re-shows the row so the user can retry.
  });
}

/** User-initiated "Clear all": drop every visible row (optimistic) and mark each
 *  one's session read, so one click acknowledges the whole backlog here and the
 *  clear follows the user to every other device.
 *
 *  Reuses the single-row read POST — the active set is bounded by the panel's own
 *  rows, so a bulk endpoint would add API surface for no gain. Removing the
 *  last row hides the panel via _removeRow, which is the intended end state. */
function _clearAll() {
  if (_activeRows.size === 0) return;
  const cleared = [..._activeRows.keys()];
  for (const sid of cleared) _removeRow(sid);
  for (const sid of cleared) {
    _markSessionRead(sid).catch(() => {
      // Transient — the next reconcile re-shows the rows so the user can retry.
    });
  }
}

// ── Row set = the unread, settled sessions ─────────────────────────────────

/** The sessions that should currently carry a notification: every session the
 *  user has NOT read whose resolved flag is a settled state. This is the SAME
 *  predicate the Sessions-page chip uses (sessionAckFlag), which is what keeps
 *  the panel and that page listing the same sessions. */
function _desiredRows() {
  const out = [];
  for (const s of _sessionsCache) {
    if (!sessionAckFlag(s)) continue;
    out.push({ sid: s.id, title: s.title, timerAt: _sessionTimerAt(s) });
  }
  return out;
}

/** Reconcile the visible rows against the cache: show every unread settled
 *  session, drop any row whose session is now read, and re-stamp the survivors.
 *  Runs at init, at the end of every poll, and whenever the session list
 *  republishes. */
function _syncRowsFromCache() {
  const desired = _desiredRows();
  const wanted = new Set(desired.map((d) => d.sid));

  for (const sid of [..._activeRows.keys()]) {
    if (!wanted.has(sid)) _removeRow(sid);
  }
  for (const { sid, title, timerAt } of desired) {
    if (!_activeRows.has(sid)) _addRow(sid, title, timerAt);
  }
  _refreshActiveFlags();
  _updateCount();
}

/** Reconcile as soon as the session list republishes. The initial list load
 *  lands AFTER init, so this is what paints the panel then instead of waiting
 *  for the next 15s tick; it also covers every mark-read/unread re-render. */
function _subscribeToSessionList() {
  if (_listSubscribed) return;
  _listSubscribed = true;
  try {
    sessionDropdownStore.subscribe(() => { _syncRowsFromCache(); });
  } catch (_) { /* the poll still reconciles */ }
}

// ── Public API ─────────────────────────────────────────────────────────────

/**
 * Reconcile the notification surfaces with the current session list. Called from
 * the 15s poll immediately after populateSessionSelect(), so the cache it reads
 * is fresh. The row set is STATE — every unread settled session — not a set of
 * observed transitions; see the module header for why that matters.
 */
export async function checkSessionCompletions() {
  if (!_enabled()) return;
  _syncRowsFromCache();
}

/**
 * Initialize the notification system. Call once from initSessions().
 */
export async function initSessionNotification() {
  _loadEnabledFlag();
  if (!_enabled()) return;
  _installOutsideClose();
  _installSessionVisitListener();
  _installResizeListener();
  _subscribeToSessionList();
  // Clear any rows left from a prior hot init
  _activeRows.clear();

  if (_panel) {
    const body = _panel.querySelector('.sn-body');
    if (body) body.innerHTML = '';
  }
  _hidePanel();
  _updateCount();

  // Seed from the cache: every unread settled session is a notification. The
  // initial list load usually lands after this, so the store subscription above
  // re-runs it the moment the cache is populated.
  _syncRowsFromCache();
}

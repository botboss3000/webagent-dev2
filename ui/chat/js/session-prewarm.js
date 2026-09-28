'use strict';

import { compareSessionsByPinnedOrder } from '../../shared/js/session-ordering.js';

// Session pre-warm — pull sessions' transcripts into the hybrid IndexedDB
// cache WITHOUT navigating to them, so opening any saved session is an instant
// (and, once cached, offline-capable) cache hit.
//
// PRIORITY TIERS (lower number = warms first):
//   0 CURRENT    — the session being viewed: FULL transcript (all history).
//   1 COMPLETED  — a turn just finished (live WS or reconcile/automation):
//                  FULL transcript of that session.
//   2 NEW        — a session gained rows elsewhere (revision bump):
//                  newest-N tail window (the "newly arrived messages").
//   3 LIST ORDER — every remaining session: FULL slim transcript, in the same
//                  order the user sees. Age never changes cache priority.
//
// All warms are idle-scheduled, serialized one-at-a-time, low network
// priority, and skipped while the app is busy (a turn streaming, a session
// loading, tab hidden) or outside hybrid mode. Full-transcript warms stop
// early once the device cache approaches the configured byte quota. Every
// cache write slims tool bodies (see _slimForCacheWrite in storage-adapter)
// so user / system / summary rows and tool-call headings are kept — at the
// same priority as normal messages — while heavy tool content stays view-once.
//
// "User opens the app and does nothing" behavior: the bootstrap pass enqueues
// the whole session list, and the 2-minute idle poll keeps re-enqueuing anything
// not cached yet, so eventually every retained session is resident.

import { app } from '../../shared/js/state.js';
import { storageAdapter } from './storage/storage-adapter.js';
import defaultSessionDB from './storage/indexeddb.js';

const TURN_COMPLETED = 'webagent-turn-completed';
const SESSIONS_DELTA = 'sessions-delta';
const WARM_NEWEST = 40;               // revision-bump tail window
const WARM_RECENT_MS = 30000;         // window-warm dedupe per session
const FULL_RECENT_MS = 5 * 60 * 1000; // bulk full-warm dedupe per session
const FULL_RECENT_FAST_MS = 90000;    // P0/P1 full-warm dedupe (fresher)
const MAX_PER_PASS = 3;               // full warms per sweep
const IDLE_POLL_MS = 120000;          // background list re-sync + sweep cadence
const QUOTA_FRACTION = 0.9;           // stop bulk full warms at 90% of quota

const P_CURRENT = 0;
const P_COMPLETED = 1;
const P_HOVER = P_COMPLETED;  // hover-peek warms at the same tier as a just-finished turn (both precede the sweep)
const P_NEW = 2;
const P_IN_HORIZON = 3;

let _knownRevisions = new Map();  // sid -> last-seen authority_revision
let _seeded = false;              // first delta records revisions without warming
let _lastWarmAt = new Map();      // sid -> epoch ms of the last window warm
let _fullWarmedAt = new Map();    // sid -> epoch ms of the last full warm
let _queue = [];                  // { sid, priority, full }
let _warming = false;
let _pollTimer = null;
let _bootstrapDone = false;

function _isHybrid() { return storageAdapter.isHybrid; }

/** Recent warm dedupe. */
function _recentlyWarmed(sid, map, ms) {
  const t = map.get(sid);
  return !!(t && (Date.now() - t) < ms);
}

function _appBusy() {
  if (app.isProcessing) return true;          // a turn is streaming
  if (app._sessionLoadInFlight) return true;  // a session is loading
  if (typeof document !== 'undefined' && document.hidden) return true;
  return false;
}

function _idle(cb) {
  if (typeof window !== 'undefined' && window.requestIdleCallback) {
    return window.requestIdleCallback(cb, { timeout: 3000 });
  }
  return setTimeout(cb, 1500);
}

async function _quotaOk() {
  try {
    const pol = storageAdapter.browserStoragePolicy;
    const size = await defaultSessionDB.estimateSizeBytes();
    return size < (Number(pol && pol.max_bytes) || 50 * 1024 * 1024) * QUOTA_FRACTION;
  } catch (_) { return true; }
}

// Mirror the dropdown's pinned/manual ordering so prewarming follows the same
// sequence the user sees. In-place, like the reference implementation.
function _sortLikeSessionList(rows) {
  return rows.sort(compareSessionsByPinnedOrder);
}

function _enqueue(sid, priority, full) {
  if (!sid || !_isHybrid()) return;
  const existing = _queue.find(q => q.sid === sid);
  if (existing) {
    if (priority < existing.priority) existing.priority = priority;
    if (full) existing.full = true;
    return;
  }
  _queue.push({ sid, priority, full });
  _idle(_drain);
}

/** Enqueue a session for warming (deduped; upgrades priority/full). */
export function prewarmSession(sessionId, { priority = P_NEW, full = false } = {}) {
  _enqueue(sessionId, priority, full);
}

// A turn just ended. Defer a beat so the final DB row + manifest settle
// server-side, then warm the FULL transcript for a fast re-open.
function _onTurnCompleted(e) {
  const sid = e && e.detail && (e.detail.sessionId || e.detail.session_id);
  if (!sid) return;
  setTimeout(() => { _enqueue(sid, P_COMPLETED, true); }, 2500);
}

// The session list refreshed. Diff authority_revision against what we last
// saw: sessions that gained rows (completed elsewhere / automation / new
// session) get their newest tail warmed, most-recently-active first.
function _onSessionsDelta(e) {
  const sessions = e && e.detail && e.detail.sessions;
  if (!Array.isArray(sessions) || !_isHybrid()) return;
  const bumped = [];
  for (const s of sessions) {
    if (!s || !s.id) continue;
    const rev = Number(s.authority_revision || s.revision || 0);
    const known = _knownRevisions.get(s.id) || 0;
    _knownRevisions.set(s.id, Math.max(known, rev));
    if (!_seeded) continue; // first pass: record baselines, warm nothing
    if (rev > known && rev > 0 && s.id !== app.currentSessionId) {
      bumped.push(s);
    }
  }
  _seeded = true;
  if (!bumped.length) return;
  bumped.sort(compareSessionsByPinnedOrder);
  for (const s of bumped.slice(0, MAX_PER_PASS)) _enqueue(s.id, P_NEW, false);
}

// Hover peek: session rows (the chat dropdown AND the floating launcher's
// session popup) carry the session id. Warm the session under the pointer
// (FULL) at hover tier so it is cached before the click lands. pointerover
// bubbles and fires on every boundary crossing — closest() + the queue/recency
// dedupes keep it cheap (a hovered row enqueues once and is not re-fetched
// within the fast full-warm window).
function _onRowHover(e) {
  if (!_isHybrid() || _appBusy()) return;
  const t = e.target;
  const row = t && t.closest
    ? t.closest('#session-dropdown-menu .session-row[data-id], .webagent-launcher-sessions-item[data-sid]')
    : null;
  if (!row) return;
  const sid = row.dataset.sid || row.dataset.id;
  if (!sid || sid === app.currentSessionId) return;
  _enqueue(sid, P_HOVER, true);
}

// Serial warm loop, highest-priority first. Peek (don't shift) while busy so
// a deferred warm is retried on the next idle beat instead of being dropped.
async function _drain() {
  if (_warming || _appBusy() || !_queue.length) return;
  _warming = true;
  try {
    _queue.sort((a, b) => a.priority - b.priority);
    while (_queue.length && !_appBusy()) {
      const item = _queue[0];
      const { sid, priority, full } = item;
      const dedupeMs = full
        ? (priority < P_IN_HORIZON ? FULL_RECENT_FAST_MS : FULL_RECENT_MS)
        : WARM_RECENT_MS;
      if (full && _recentlyWarmed(sid, _fullWarmedAt, dedupeMs)) { _queue.shift(); continue; }
      if (!full && _recentlyWarmed(sid, _lastWarmAt, dedupeMs)) { _queue.shift(); continue; }
      // Quota guard: bulk list-order tiers yield when the device is
      // nearly full; CURRENT/COMPLETED/NEW still warm (they are small or vital).
      if (priority >= P_IN_HORIZON && !(await _quotaOk())) { _queue.shift(); continue; }
      try {
        if (full) {
          const r = await storageAdapter.warmFullTranscript(sid);
          if (r && r.warmed) {
            _fullWarmedAt.set(sid, Date.now());
            _lastWarmAt.set(sid, Date.now());
          }
        } else {
          const r = await storageAdapter.warmSessionIntoCache(sid, WARM_NEWEST);
          if (r && r.warmed) _lastWarmAt.set(sid, Date.now());
        }
      } catch (_) {
        // offline / restricted / server failure — a later delta or poll pass
        // may retry. Not fatal: the session simply opens from the server.
      }
      _queue.shift();
    }
    // The app became busy mid-pass with work still queued — reschedule so the
    // remaining sessions warm on the next idle beat instead of stalling.
    if (_queue.length) _idle(_drain);
  } finally {
    _warming = false;
  }
}

// Boot / first-idle pass: enqueue the current session (full), then the whole
// list as full slim transcripts. Retried by the idle poll until it completes,
// so a cold offline boot
// that can't list still gets a second chance when connectivity returns.
async function _bootstrap() {
  if (_bootstrapDone || !_isHybrid()) return;
  if (_appBusy()) { _idle(_bootstrap); return; }
  try {
    if (app.currentSessionId) _enqueue(app.currentSessionId, P_CURRENT, true);
    const sessions = await storageAdapter.listSessions(app.currentUserId || '');
    const rows = _sortLikeSessionList(
      (Array.isArray(sessions) ? sessions : [])
        .filter(s => s && s.id && s.id !== app.currentSessionId),
    );
    for (const s of rows) _enqueue(s.id, P_IN_HORIZON, true);
    _bootstrapDone = true;
  } catch (_) { /* offline — leave _bootstrapDone false; the poll retries */ }
  _idle(_drain);
}

// Slow background re-sync + continuous sweep so the whole session list ends up
// cached without any user action, and completions outside the open session
// (automations, other devices) are detected and pre-warmed.
function _startIdlePoll() {
  if (_pollTimer) return;
  _pollTimer = window.setInterval(() => {
    if (!_isHybrid() || _appBusy()) return;
    if (!_bootstrapDone) { _bootstrap(); return; }
    storageAdapter.listSessions(app.currentUserId || '').then(sessions => {
      const rows = _sortLikeSessionList(
        (Array.isArray(sessions) ? sessions : [])
          .filter(s => s && s.id && s.id !== app.currentSessionId),
      );
      let queued = 0;
      for (const s of rows) {
        if (queued >= MAX_PER_PASS) break;
        if (_recentlyWarmed(s.id, _fullWarmedAt, FULL_RECENT_MS)) continue;
        _enqueue(s.id, P_IN_HORIZON, true);
        queued += 1;
      }
    }).catch(() => {});
  }, IDLE_POLL_MS);
}

/** Bind the pre-warm listeners + idle poll + bootstrap. Idempotent. */
export function initSessionPrewarm() {
  if (initSessionPrewarm.__bound) return;
  initSessionPrewarm.__bound = true;
  window.addEventListener(TURN_COMPLETED, _onTurnCompleted);
  window.addEventListener(SESSIONS_DELTA, _onSessionsDelta);
  // Capture phase so hover is seen even if the dropdown stops propagation.
  window.addEventListener('pointerover', _onRowHover, true);
  _startIdlePoll();
  _idle(_bootstrap);
}

/**
 * Kick the pre-warm loop NOW (idempotent). Called by the boot sequence once
 * storage mode resolves to hybrid — the first bootstrap attempt may have run
 * while the adapter was still 'normal' (autoSelectMode is async), and waiting
 * for the 2-minute idle poll would leave a fresh login uncached for that long.
 * No-ops outside hybrid mode.
 */
export function kickSessionPrewarm() {
  if (!_isHybrid()) return;
  if (!_bootstrapDone) _idle(_bootstrap);
  _idle(_drain);
}

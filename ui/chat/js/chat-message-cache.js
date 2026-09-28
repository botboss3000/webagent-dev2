'use strict';

import { app } from '../../shared/js/state.js';
import { sortTranscriptCanonical } from './transcript-order.js';
import { apiPath } from '../../shared/js/config.js';
import { storageAdapter } from './storage/storage-adapter.js';
import browserRouter from './storage/browser-router.js';
import { kvCache } from './storage/kv-cache.js';
import { browserPersistenceAllowed } from '../../shared/js/browser-storage-policy.js';

// ── Message cache + infinite-scroll state ──────────────────────────────────
// Keyed by sessionId. Each entry: { messages: [...], hasMore: bool, loadedAll: bool }
const _messageCache = new Map();
const _CACHE_TTL_MS = 60000; // 60 seconds
const _projectionTimers = new Map();

function _persistCachedProjection(sessionId, projection, immediate) {
  if (!storageAdapter.isHybrid || !sessionId || !projection?.id) return;
  const previous = _projectionTimers.get(sessionId);
  if (previous) clearTimeout(previous);
  const write = () => {
    _projectionTimers.delete(sessionId);
    storageAdapter.cacheInteractionProjection(sessionId, projection).catch(() => {});
  };
  if (immediate) write();
  else _projectionTimers.set(sessionId, setTimeout(write, 180));
}

// After a tenant purge/logout, the old tenant's focus/manifest state must not be
// written back to the shared localStorage bucket or IndexedDB — the next
// tenant's boot would migrate it into THEIR database (the unload-mirror below is
// synchronous and bypasses kvCache's epoch guard). Set on the purge event; also
// drops the in-memory maps so a late visibilitychange/scroll can't resurrect
// stale rows after the sweep has run.
let _purged = false;
if (typeof window !== 'undefined') {
  window.addEventListener('webagent-browser-storage-purge', () => {
    _purged = true;
    _sessionFocus.clear();
    _sessionManifest.clear();
    _messageCache.clear();
  });
}

// ── Saved focus per session (for fast windowed switching) ──────────────────
const _sessionFocus = new Map(); // sessionId -> { atBottom: bool, anchorMsgId?: string }
const _FOCUS_MAX = 50;
const _WINDOW_RADIUS = 20;
const _loadingSessions = new Set();

// Migrated from the localStorage blob 'sessionFocus.v1' (one blob holding all
// sessions) to one app_cache row per session ('chat:focus:<sessionId>').
// registerLegacyMap keeps boot-time reads working until hydration migrates it.
kvCache.registerLegacyMap('sessionFocus.v1', 'chat:focus:');

/** Load persisted per-session focus into _sessionFocus (sync — legacy fallback until hydrated). */
function _loadSessionFocus() {
  const obj = kvCache.getAll('chat:focus:');
  for (const [k, v] of Object.entries(obj)) {
    // Merge without clobbering: an entry captured during this page's lifetime
    // (e.g. current-session focus on pagehide) is newer than persisted state.
    if (v && typeof v === 'object' && !_sessionFocus.has(k)) _sessionFocus.set(k, v);
  }
}

/** Persist _sessionFocus, keeping only the most-recent entries. */
function _persistSessionFocus() {
  if (_purged) return; // tenant gone — never mirror old-tenant focus to the next tenant
  const entries = Array.from(_sessionFocus.entries()).slice(-_FOCUS_MAX);
  _sessionFocus.clear();
  const obj = {};
  for (const [k, v] of entries) { _sessionFocus.set(k, v); obj[k] = v; }
  kvCache.setAll('chat:focus:', obj);
  // Synchronous unload-mirror: _captureSessionFocus fires on pagehide /
  // visibilitychange, where the page can be killed before the async IndexedDB
  // flush completes — the old localStorage write was guaranteed to land, and a
  // lost scroll position on refresh is a real regression. Write the same blob
  // synchronously as a backup; kvCache.migrateLegacyMap treats any surviving
  // blob as authoritative on the next boot and copies it into IndexedDB.
  // Skipped in memory_only/disabled where nothing persists anyway.
  if (browserPersistenceAllowed()) {
    try { localStorage.setItem('sessionFocus.v1', JSON.stringify(obj)); } catch (_) { /* non-fatal */ }
  }
}

// The boot-time _loadSessionFocus runs before storage hydration resolves, so
// after the legacy blobs are migrated away there is nothing to fall back to on
// cold loads. Re-read once hydration completes and merge the persisted rows.
if (typeof window !== 'undefined') {
  window.addEventListener('webagent-kv-cache-hydrated', () => {
    _loadSessionFocus();
    _manifestsLoaded = false;
    _loadSessionManifests();
  });
}

/**
 * Record where the user is currently looking in the on-screen transcript for
 * `sessionId`. Stores either { atBottom: true } or { atBottom: false, anchorMsgId }.
 */
function _captureSessionFocus(sessionId) {
  if (!sessionId) return;
  const container = app.chatMessages;
  if (!container) return;
  const scroller = app._chatScroller || container.parentElement;
  if (!scroller) return;
  const nearBottom =
    (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight) < 60;
  let focus = { atBottom: true };
  if (!nearBottom) {
    const st = scroller.scrollTop;
    let anchorMsgId = null;
    for (const el of Array.from(container.children)) {
      if (!el.classList) continue;
      const isBubble = el.classList.contains('chat-bubble');
      const isPlaceholder = el.classList.contains('chat-bubble-placeholder');
      if (!isBubble && !isPlaceholder) continue;
      const id = el.getAttribute('data-msg-id');
      if (!id) continue;
      const top = el.offsetTop;
      const h = isPlaceholder ? (parseInt(el.style.height, 10) || 0) : el.offsetHeight;
      if (top + h > st + 4) { anchorMsgId = id; break; }
    }
    if (anchorMsgId) focus = { atBottom: false, anchorMsgId };
  }
  _sessionFocus.delete(sessionId);
  _sessionFocus.set(sessionId, focus);
  _persistSessionFocus();
}

/**
 * Append or update a message in the in-memory cache for a session (called from WS paths).
 */
function _cacheAppendMessage(sessionId, msg) {
  const cached = _messageCache.get(sessionId);
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('cache.memory.write', {
      session_id: sessionId,
      interaction_id: msg.id,
      decision: cached?'merge':'discard',
      reason: cached?'existing-cache':'cache-absent',
      streaming: !!msg._streaming,
      finalized: !!msg._finalized,
      interaction_seq: msg.session_seq,
      text_length: typeof msg.content==='string'?msg.content.length:null,
    });
  } catch (_) {}
  if (!cached) return;

  if (msg._streaming && msg.id) {
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = (existing.content || '') + msg.content;
      if (typeof msg.session_seq === 'number') existing.session_seq = msg.session_seq;
      if (msg.created_at && !existing.created_at) existing.created_at = msg.created_at;
    } else {
      cached.messages.push({
        role: 'assistant', content: msg.content, id: msg.id,
        created_at: msg.created_at, status: 'streaming',
      });
    }
  } else if (msg._finalized && msg.id) {
    const existing = cached.messages.find(m => m.id === msg.id && m.role === 'assistant');
    if (existing) {
      existing.content = msg.content;
      if (typeof msg.session_seq === 'number') existing.session_seq = msg.session_seq;
      if (msg.created_at && !existing.created_at) existing.created_at = msg.created_at;
      delete existing.status;
    } else {
      cached.messages.push({
        role: 'assistant', content: msg.content, id: msg.id,
        created_at: msg.created_at,
      });
    }
  } else {
    const existing = msg.id ? cached.messages.find(m => m.id === msg.id) : null;
    if (existing) Object.assign(existing, msg);
    else cached.messages.push(msg);
  }

  // Live WS events and DB-tail polls can interleave. Keep the cache in the same
  // authoritative order as a cold transcript load instead of preserving arrival
  // order. Legacy rows without a sequence predate sequenced rows.
  if (typeof msg.session_seq === 'number') {
    _sortMessagesCanonical(cached.messages, cached.timestampOrderRecovery === true);
  }

  if (typeof msg.session_seq === 'number') {
    cached.maxSeq = Math.max(Number(cached.maxSeq || 0), msg.session_seq);
  }

  cached.loadedAt = Date.now();
  const projection = msg.id ? cached.messages.find(m => m && m.id === msg.id) : null;
  // Persist the accumulated projection, not the latest text chunk. Streaming
  // writes are coalesced; terminal/tool/update rows flush immediately.
  if (projection) _persistCachedProjection(sessionId, { ...projection }, !msg._streaming);
  if (typeof app.setContextFromMessages === 'function') {
    app.setContextFromMessages(cached.messages);
  }
}

app._cacheAppendMessage = _cacheAppendMessage;

function _sortMessagesCanonical(messages, timestampOrderRecovery = app.timestampOrderRecovery === true) {
  if (!Array.isArray(messages)) return messages;
  messages.splice(0, messages.length, ...sortTranscriptCanonical(messages, {
    timestampOrderRecovery,
  }));
  return messages;
}

// ── Transcript manifests — remote-change detection ─────────────────────────
// The server maintains a per-session transcript manifest (authority_revision +
// content_hash, see app/db/session_manifest.py) and validates it against the
// browser's last-known values via /session-messages?manifest_only=1
// (returns `not_modified: true` when identical). Every refresh path consults
// this FIRST: if the server says nothing changed, the caller MUST NOT touch
// the screen — no cache drop, no teardown, no re-render. The rubber-band pull
// animation is driven by applyRubberBand and is unaffected by the skip.
const _sessionManifest = new Map(); // sessionId -> { revision, hash }
const _MANIFEST_MAX = 50;
let _manifestsLoaded = false;

// Migrated from the localStorage blob 'sessionManifest.v1' to one app_cache
// row per session ('chat:manifest:<sessionId>').
kvCache.registerLegacyMap('sessionManifest.v1', 'chat:manifest:');

function _loadSessionManifests() {
  if (_manifestsLoaded) return;
  _manifestsLoaded = true;
  const obj = kvCache.getAll('chat:manifest:');
  for (const [k, v] of Object.entries(obj)) {
    // Merge without clobbering: a manifest remembered during this page's
    // lifetime is newer than persisted state.
    if (v && typeof v === 'object' && typeof v.revision === 'number' && typeof v.hash === 'string' && !_sessionManifest.has(k)) {
      _sessionManifest.set(k, { revision: v.revision, hash: v.hash });
    }
  }
}

function _persistSessionManifests() {
  const entries = Array.from(_sessionManifest.entries()).slice(-_MANIFEST_MAX);
  _sessionManifest.clear();
  const obj = {};
  for (const [k, v] of entries) { _sessionManifest.set(k, v); obj[k] = v; }
  kvCache.setAll('chat:manifest:', obj);
}

/** Remember the server's manifest for a session after any fetch/validation. */
function _rememberSessionManifest(sessionId, manifest) {
  if (!sessionId || !manifest) return;
  const rev = Number(manifest.authority_revision);
  const hash = String(manifest.content_hash || '');
  if (!Number.isFinite(rev) || !hash) return;
  _loadSessionManifests();
  _sessionManifest.set(sessionId, { revision: rev, hash });
  _persistSessionManifests();
}

/**
 * Ask the REMOTE server whether the session transcript changed since the last
 * render. Returns:
 *   false — the server explicitly says NOT modified → the caller must do
 *           nothing to the screen.
 *   true  — changed, unknown (no manifest recorded yet), or the check failed
 *           (network/parse/browser-only mode) → the caller should refresh.
 * Only an explicit `not_modified: true` skips the refresh; every uncertain
 * path stays conservative and refreshes, preserving today's behavior.
 */
async function _transcriptChangedRemotely(sessionId) {
  if (!sessionId) return true;
  _loadSessionManifests();
  const known = _sessionManifest.get(sessionId);
  if (!known) return true; // never validated — a refresh is the first sync
  if (storageAdapter.isBrowser) return true; // no remote authority — refresh locally
  try {
    const token = localStorage.getItem('auth_token') || '';
    let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=1&manifest_only=true&known_revision=${known.revision}&known_hash=${encodeURIComponent(known.hash)}`);
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(url);
    if (!res.ok) return true;
    const data = await res.json();
    // Always record whatever the server reports so the next check starts from
    // the new baseline (keeps the store in sync with server truth).
    if (data.manifest) _rememberSessionManifest(sessionId, data.manifest);
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('cache.manifest', {
        session_id: sessionId,
        decision: data.not_modified===true?'reuse':'refresh',
        current_revision: known.revision,
        incoming_revision: data.manifest?.authority_revision,
      });
    } catch (_) {}
    return data.not_modified !== true;
  } catch (_) {
    return true; // network/parse failure — conservative: proceed with refresh
  }
}

/**
 * Persist a mode-change notice into the browser-authority IndexedDB transcript.
 * In browser mode the WHOLE transcript lives in IndexedDB (the server's
 * system:mode row is never read on reload there), so the durable twin must be
 * written locally too. No-op in server/hybrid modes, where the server row
 * persists instead. Best-effort — the live notice is already on screen.
 */
async function persistModeNoticeToCache(sessionId, msg) {
  if (!sessionId || !storageAdapter.isBrowser || !msg) return;
  try {
    await browserRouter.addInteraction(sessionId, msg);
  } catch (_) { /* non-fatal */ }
}

export {
  _messageCache,
  _CACHE_TTL_MS,
  _sessionFocus,
  _loadSessionFocus,
  _persistSessionFocus,
  _captureSessionFocus,
  _cacheAppendMessage,
  persistModeNoticeToCache,
  _sortMessagesCanonical,
  _rememberSessionManifest,
  _transcriptChangedRemotely,
};

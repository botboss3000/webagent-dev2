'use strict';

/**
 * StorageAdapter — routes all data operations between browser-authority
 * (IndexedDB) and normal (server API) mode.
 *
 * Server-published capabilities and routing select the mode at boot; callers
 * cannot use a URL or direct assignment to upgrade storage authority.
 *
 * The chat *send* path is fundamentally different between modes (SSE stream
 * vs outbox+WS+reconcile), so that gets a conditional branch in chat-send.js
 * rather than a thin wrapper here.
 *
 * All data methods return the same shapes the existing chat UI expects,
 * so callers need no mode-specific branches.
 */

import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { app } from '../../../shared/js/state.js';
import browserRouter from './browser-router.js';
import defaultSessionDB from './indexeddb.js';
import { sortTranscriptCanonical } from '../transcript-order.js';
import kvCache from './kv-cache.js';
import { setAttachmentOwnerScope } from '../../../shared/js/attachments-idb.js';
import {
  configureBrowserStoragePolicy,
  DISABLED,
  MEMORY_ONLY,
  PERSISTENT_CACHE,
} from '../../../shared/js/browser-storage-policy.js';

// ── Mode ───────────────────────────────────────────────────────────────────

const MODE_NORMAL = 'normal';
const MODE_BROWSER = 'browser';
const MODE_HYBRID = 'hybrid';

let _mode = MODE_NORMAL;
let _cleanupTimer = null;
const _sessionListRefreshes = new Map();
let _capabilities = {
  browser_authority: false,
  browser_session_cache: false,
};
let _cachePolicy = {
  schema_version: 4,
  metadata_ttl_seconds: 0,
  transcript_ttl_seconds: 0,
  run_state_ttl_seconds: 0,
  generated_html_ttl_seconds: 0,
  max_bytes: 512 * 1024 * 1024,
  persistence_mode: PERSISTENT_CACHE,
  policy_epoch: 0,
};

// The IndexedDB owner scope / policy are issued ONLY by the /browser/routing
// server response. Persist them so an OFFLINE boot can reopen the same
// tenant-scoped database instead of falling back to server-only mode. Swept
// automatically on purge (browser-lifecycle removes all non-allowlisted keys).
const _CACHE_CTX_KEY = 'webagent.browserCacheCtx.v1';

export const storageAdapter = {

  /** 'normal' (server API), 'browser' (IndexedDB), or 'hybrid' (server auth + IDB cache) */
  get mode() { return _mode; },
  set mode(v) {
    if (v !== MODE_NORMAL && v !== MODE_BROWSER && v !== MODE_HYBRID) return;
    if (v === MODE_BROWSER && !_capabilities.browser_authority) return;
    if (v === MODE_HYBRID && !_capabilities.browser_session_cache) return;
    _mode = v;
  },

  get isBrowser() { return _mode === MODE_BROWSER; },
  get isNormal() { return _mode === MODE_NORMAL; },
  get isHybrid() { return _mode === MODE_HYBRID; },
  get capabilities() { return { ..._capabilities }; },
  get browserStoragePolicy() { return { ..._cachePolicy }; },
  get persistenceMode() { return _cachePolicy.persistence_mode; },
  get canUseBrowserAuthority() { return _capabilities.browser_authority; },
  get canUseBrowserCache() { return _capabilities.browser_session_cache; },

  // ── Initialisation ────────────────────────────────────────────────────

  /**
   * Boot into browser mode for a given agent. Fetches + caches config.
   * Must be called once (e.g. at app boot) before using other methods.
   * @param {string} agentId
   */
  async initBrowser(agentId) {
    if (!_capabilities.browser_authority) {
      throw new Error('Browser authority is disabled by server policy');
    }
    await browserRouter.init(agentId);
    _startLifecycleSweep();
    _mode = MODE_BROWSER;
  },

  /**
   * Fetch the storage routing config from the server and auto-select
   * browser mode if session_data is set to "browser".
   * Called at boot — if the server routing says browser, the adapter
   * enters browser mode without needing a URL parameter.
   * @param {string} agentId
   * @returns {Promise<string>} 'browser' or 'normal'
   */
  async autoSelectMode(agentId) {
    // A URL may force the safer server mode, but can never upgrade authority.
    const urlParam = new URLSearchParams(window.location.search).get('storage');
    if (urlParam === 'server') {
      _mode = MODE_NORMAL;
      return 'normal';
    }

    // Otherwise, ask the server for the routing config
    try {
      const resp = await fetch(apiPath('/api/v1/browser/routing'), {
        headers: authHeaders(),
      });
      if (resp.ok) {
        const data = await resp.json();
        const routing = data.routing || {};
        _capabilities = {
          browser_authority: data.capabilities?.browser_authority === true,
          browser_session_cache: data.capabilities?.browser_session_cache === true,
        };
        _cachePolicy = { ..._cachePolicy, ...(data.cache_policy || {}) };
        if (!data.cache_scope) throw new Error('Server did not issue a browser cache scope');
        defaultSessionDB.setOwnerScope(data.cache_scope);
        defaultSessionDB.configureLifecyclePolicy(_cachePolicy);
        setAttachmentOwnerScope(data.cache_scope);
        const persistenceMode = [
          PERSISTENT_CACHE, MEMORY_ONLY, DISABLED,
        ].includes(_cachePolicy.persistence_mode)
          ? _cachePolicy.persistence_mode
          : DISABLED;
        configureBrowserStoragePolicy({
          mode: persistenceMode,
          ownerScope: data.cache_scope,
          policyEpoch: _cachePolicy.policy_epoch,
          schemaVersion: _cachePolicy.schema_version,
          maxBytes: _cachePolicy.max_bytes,
        });
        // Persist the cache context so a later OFFLINE boot can reopen the
        // same IndexedDB (the scope/epoch come only from this response).
        try {
          localStorage.setItem(_CACHE_CTX_KEY, JSON.stringify({
            mode: persistenceMode,
            owner_scope: data.cache_scope,
            policy_epoch: _cachePolicy.policy_epoch,
            schema_version: _cachePolicy.schema_version,
            max_bytes: _cachePolicy.max_bytes,
          }));
        } catch (_) { /* non-fatal */ }
        // Warm the app_cache in-memory front (no-op / memory-only when the
        // policy mode does not permit persistence). Chat bookkeeping reads are
        // synchronous, so this must resolve before those paths rely on IDB.
        await kvCache.hydrate();
        if (persistenceMode !== PERSISTENT_CACHE) {
          _capabilities.browser_authority = false;
          _capabilities.browser_session_cache = false;
          // Policy transition cleanup is coordinated and reported. The normal
          // server chat remains available in memory_only mode.
          const { purgeBrowserData } = await import('../../../shared/js/browser-lifecycle.js');
          const purge = await purgeBrowserData(data.cache_scope);
          // purgeBrowserData itself dispatches webagent-browser-storage-purge.
          if (!purge.complete) {
            console.warn('[StorageAdapter] Browser policy transition purge is incomplete', purge);
          }
          _mode = MODE_NORMAL;
          return 'normal';
        }
        if (_capabilities.browser_authority && routing.session_data === 'browser' && agentId) {
          await this.initBrowser(agentId);
          return 'browser';
        }
        if (_capabilities.browser_session_cache && routing.session_cache === 'browser') {
          await this.initHybrid(agentId);
          return 'hybrid';
        }
      }
    } catch (e) {
      console.warn('[StorageAdapter] Could not fetch routing config:', e);
      // Offline boot: reopen the last-known browser cache scope so cached
      // sessions/transcripts stay readable. Server writes will fail, but every
      // read path degrades gracefully to IndexedDB (see getInteractions).
      try {
        const saved = JSON.parse(localStorage.getItem(_CACHE_CTX_KEY) || 'null');
        if (saved && saved.mode === PERSISTENT_CACHE && saved.owner_scope) {
          defaultSessionDB.setOwnerScope(saved.owner_scope);
          setAttachmentOwnerScope(saved.owner_scope);
          configureBrowserStoragePolicy({
            mode: PERSISTENT_CACHE,
            ownerScope: saved.owner_scope,
            policyEpoch: saved.policy_epoch || 0,
            schemaVersion: saved.schema_version || 0,
            maxBytes: saved.max_bytes || 512 * 1024 * 1024,
          });
          await kvCache.hydrate();
          const cached = await defaultSessionDB.listSessions();
          if (cached.length > 0) {
            _capabilities = { browser_authority: false, browser_session_cache: true };
            await this.initHybrid(agentId);
            return 'hybrid';
          }
        }
      } catch (_) { /* non-fatal — fall back to server mode */ }
    }

    _mode = MODE_NORMAL;
    return 'normal';
  },

  /**
   * Boot into hybrid mode. Opens IndexedDB for read caching but keeps
   * the server as the write authority.
   * @param {string} agentId
   */
  async initHybrid(agentId) {
    if (!_capabilities.browser_session_cache) {
      throw new Error('Browser session cache is disabled by server policy');
    }
    // Warm up IndexedDB without starting a full browser session.
    // We don't call browserRouter.init() — that would enter browser mode.
    // We just ensure the DB is open and our stores exist.
    await browserRouter._ensureReady();
    try { await navigator.storage?.persist?.(); } catch (_) { /* best-effort */ }
    await defaultSessionDB.enforceCachePolicy({
      maxBytes: await _effectiveCacheBudget(),
      protectedSessionId: app.currentSessionId || null,
    });
    _startLifecycleSweep();
    _mode = MODE_HYBRID;
  },

  /**
   * Switch back to normal (server) mode.
   */
  switchToNormal() {
    _mode = MODE_NORMAL;
  },

  // ── Sessions ─────────────────────────────────────────────────────────

  /**
   * Fetch the session list. Returns the same shape as the existing server API.
   * @param {string} userId — ignored in browser mode
   * @param {Object} [opts]
   * @param {boolean} [opts.includeHidden]
   * @returns {Promise<Array<Object>>} sessions
   */
  async listSessions(userId, opts = {}) {
    if (_mode === MODE_BROWSER) {
      const sessions = await browserRouter.listSessions();
      return _shapeSessions(sessions);
    }

    if (_mode === MODE_HYBRID) {
      // Try IndexedDB first for an instant render. TTL no longer gates SERVING:
      // any structurally valid cached list is returned immediately (offline, the
      // last-known sessions stay browsable) and a background refresh re-syncs
      // freshness from the server.
      const cached = await defaultSessionDB.listSessions();
      const usable = cached.filter(row =>
        row._authority === 'server' &&
        Number(row.cache_schema_version || 0) === Number(_cachePolicy.schema_version) &&
        !!row.content_hash
      );
      if (usable.length > 0) {
        // Fire background refresh for freshness (silently no-ops offline)
        _refreshSessionListFromServer(userId).catch(() => {});
        return _shapeSessions(usable);
      }
      // Cache empty — fetch from server and cache
      const fromServer = await _fetchSessionListFromServer(userId, opts);
      await _cacheSessionList(fromServer);
      return fromServer;
    }

    // Normal mode — existing server API call (mirrors session-list.js)
    return _fetchSessionListFromServer(userId, opts);
  },

  /**
   * Persist the pinned-session drag order. Hybrid mode updates IndexedDB first
   * so the next dropdown open sees the user's completed gesture immediately;
   * the server remains authoritative and is reconciled on an uncertain write.
   */
  async reorderSessions(userId, orderedIds) {
    if (window.__webagentOfflineReadOnly === true) {
      throw new Error('Offline · cached data is read-only');
    }
    const ids = [...new Set((orderedIds || []).filter(Boolean))];
    if (!userId || !ids.length) return { success: true, updated: 0 };

    let localChange = null;
    if (_mode === MODE_HYBRID || _mode === MODE_BROWSER) {
      localChange = await defaultSessionDB.setSessionOrder(ids);
    }
    if (_mode === MODE_BROWSER) {
      if (localChange.updated !== ids.length) {
        await defaultSessionDB.restoreSessionOrder(localChange.previous);
        throw new Error(`Session reorder saved ${localChange.updated} of ${ids.length} rows`);
      }
      return { success: true, updated: localChange.updated };
    }

    try {
      const result = await _persistSessionOrderToServer(userId, ids);
      if (!result.success || result.updated !== ids.length) {
        throw new Error(`Session reorder saved ${result.updated || 0} of ${ids.length} rows`);
      }
      return result;
    } catch (error) {
      if (_mode === MODE_HYBRID && localChange) {
        // A lost response can mean the server committed successfully. Prefer an
        // authoritative list reconciliation; only roll back the optimistic cache
        // when that check is also unavailable.
        try {
          const authoritative = await _fetchSessionListFromServer(userId, { includeHidden: true });
          await _cacheSessionList(authoritative);
        } catch (_) {
          await defaultSessionDB.restoreSessionOrder(localChange.previous);
        }
      }
      throw error;
    }
  },

  /**
   * Delete a session.
   * @param {string} sessionId
   * @returns {Promise<{ok: boolean, error?: string}>}
   */
  async deleteSession(sessionId) {
    if (window.__webagentOfflineReadOnly === true) {
      return { ok: false, error: 'Offline · cached data is read-only' };
    }
    if (_mode === MODE_BROWSER) {
      try {
        await browserRouter.deleteSession(sessionId);
        return { ok: true };
      } catch (e) {
        return { ok: false, error: e.message };
      }
    }

    // Server-first then remove from local cache (hybrid + normal)
    try {
      const res = await fetch(
        apiPath('/api/v1/db/sessions/' + encodeURIComponent(sessionId) + '?db=user.db'),
        { method: 'DELETE', headers: { ...authHeaders() } }
      );
      if (!res.ok) {
        const body = await res.text().catch(() => '');
        return { ok: false, error: `Server error (${res.status}): ${body.substring(0, 120)}` };
      }
      // In hybrid mode, also remove from IndexedDB cache
      if (_mode === MODE_HYBRID) {
        try { await defaultSessionDB.deleteCachedSession(sessionId); } catch (_) {}
      }
      return { ok: true };
    } catch (e) {
      return { ok: false, error: e.message || 'Network error' };
    }
  },

  /**
   * Patch session metadata (pin, title, etc).
   * @param {string} sessionId
   * @param {Object} body
   * @returns {Promise<Object>}
   */
  async patchSession(sessionId, body) {
    if (window.__webagentOfflineReadOnly === true) {
      throw new Error('Offline · cached data is read-only');
    }
    if (_mode === MODE_BROWSER) {
      await browserRouter.updateSession(sessionId, body);
      return { ok: true };
    }

    // Server-first then update local cache (hybrid + normal)
    const res = await fetch(
      apiPath('/api/v1/db/sessions/' + encodeURIComponent(sessionId) + '?db=user.db'),
      {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      }
    );
    // In hybrid mode, also update IndexedDB cache
    if (_mode === MODE_HYBRID && res.ok) {
      try { await defaultSessionDB.updateSession(sessionId, body); } catch (_) {}
    }
    return await res.json();
  },

  /**
   * Update ONLY the local IndexedDB cache entry for a session — no server
   * write. Hybrid-mode callers that wrote through their own fetch (e.g.
   * session-core.patchSession) use this after a successful server PATCH so
   * the next list render serves the new state instead of the stale cached
   * row (the "pin doesn't stick until the dropdown is reopened" bug).
   * @param {string} sessionId
   * @param {Object} changes
   * @returns {Promise<boolean>} true if the session existed in the cache
   */
  async updateSessionCache(sessionId, changes) {
    if (_mode !== MODE_HYBRID) return false;
    try {
      return await defaultSessionDB.updateSession(sessionId, changes);
    } catch (_) {
      return false;
    }
  },

  /**
   * Interrupt a running session turn. No-op in browser mode.
   * @param {string} sessionId
   */
  async interruptSession(sessionId) {
    if (_mode === MODE_BROWSER) return;
    fetch(apiPath('/api/v1/chat/interrupt'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ session_id: sessionId }),
    }).catch(() => {});
  },

  /**
   * Refresh session metadata from the shared authority.
   * No-op in browser mode (no remote authority).
   * @param {string} userId
   * @returns {Promise<boolean>}
   */
  async refreshSessionMetadata(userId) {
    if (_mode === MODE_BROWSER) return false;

    try {
      let url = `/api/v1/db/sessions/refresh?db=user.db&user_id=${encodeURIComponent(userId)}`;
      const res = await fetch(apiPath(url), { method: 'POST', headers: authHeaders() });
      if (!res.ok) return false;
      const data = await res.json();
      return !!data.changed;
    } catch (e) {
      console.warn('[StorageAdapter] refreshSessionMetadata failed:', e);
      return false;
    }
  },

  /**
   * Create a new session.
   * In browser mode, creates in IndexedDB. In normal mode, returns the id
   * (session created server-side on first message).
   * @param {Object} opts — { agent_id, title, id }
   * @returns {Promise<string>} session id
   */
  async createSession(opts = {}) {
    if (window.__webagentOfflineReadOnly === true) return '';
    if (_mode === MODE_BROWSER) {
      return browserRouter.createSession(opts);
    }
    return opts.id || '';
  },

  // ── Interactions / Messages ─────────────────────────────────────────

  /**
   * Fetch interactions (messages) for a session.
   * Returns the same shape as GET /api/v1/db/session-messages.
   * @param {string} sessionId
   * @param {number} limit
   * @param {Object} [opts]
   * @param {string} [opts.beforeId]
   * @param {string} [opts.afterId]
   * @param {string} [opts.aroundId]
   * @returns {Promise<{messages: Array, has_more?: boolean}>}
   */
  async getInteractions(sessionId, limit, opts = {}) {
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('cache.storage.read', {
        session_id: sessionId,
        limit,
        mode: this.isBrowser?'browser':this.isHybrid?'hybrid':'server',
      });
    } catch (_) {}

    // Portal sessions are virtual references to Codex's native task store.
    // Bypass every IndexedDB/server-cache branch so native transcripts are
    // never copied into WebAgent persistence, even when hybrid mode is active.
    if (typeof sessionId === 'string' && sessionId.startsWith('codex:')) {
      const qs = new URLSearchParams({
        user_id: app.currentUserId || '',
        agent_id: app.currentAgentId || '',
      });
      const response = await fetch(apiPath(`/api/v1/engines/codex/portal/threads/${encodeURIComponent(sessionId)}/messages?${qs}`), { headers: authHeaders() });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.detail || `Codex Portal returned HTTP ${response.status}`);
      return data;
    }
    if (_mode === MODE_BROWSER) {
      // Browser authority holds the WHOLE transcript locally, so every window
      // (including the jump-nav modes at_start / nearest_user_before_id) can be
      // sliced client-side. This also reports accurate has_more/has_newer, which
      // the jump nav needs to know whether still-older rows exist.
      const all = await browserRouter.getInteractions(sessionId, Infinity);
      const { messages } = _shapeInteractions(all);
      // Opening the selected browser-authority session hydrates its complete
      // compact projection; session-load still virtualizes the DOM separately.
      const requestedLimit = opts.fullCompact === true ? Infinity : limit;
      return _windowInteractionsClientSide(messages, requestedLimit, opts);
    }

    if (_mode === MODE_HYBRID) {
      // Explicit refresh is server-authoritative and must not depend on a
      // readable/open local cache. This keeps per-message repair working after
      // tenant scope changes or an IndexedDB failure.
      if (opts.refresh) return _fetchAndCacheInteractions(sessionId, limit, opts);
      // Offline-first: a structurally valid IndexedDB transcript is served
      // IMMEDIATELY — no server validation round trip and no age gate. A
      // background refresh can still reconcile it when the server is reachable
      // (session-load renders the cached messages at once and shows a small
      // "updating" skeleton bubble until the refresh lands). Explicit refreshes (opts.refresh, e.g. the
      // header refresh button / pull-to-refresh) always hit the server.
      const all = await defaultSessionDB.getInteractions(sessionId, Infinity);
      const manifest = await defaultSessionDB.getSession(sessionId);
      // Cheap fast-path: when the stored row count doesn't match the manifest's,
      // the payload hash cannot match — skip the (slower) hash computation.
      const countMatches = Number(manifest?.cached_message_count || 0) === all.length;
      const payloadValid = manifest?.cache_projection_dirty === true
        || (countMatches && manifest?.cache_payload_hash === await _hashCachedInteractions(all));
      const structurallyValid =
        manifest?._authority === 'server' &&
        Number(manifest.cache_schema_version || 0) === Number(_cachePolicy.schema_version) &&
        !!manifest.content_hash &&
        manifest.cache_complete_turn === true &&
        payloadValid &&
        all.length > 0;

      if (structurallyValid && !opts.refresh) {
        const { messages } = _shapeInteractions(all);
        // Windowed / navigation opens may only be served from the cache when
        // the cache can actually satisfy the window (e.g. scroll-up paging
        // needs rows OLDER than the cached tail — the server must answer those
        // when online; the offline fallback below still degrades to the cache).
        const knownCount = Number(manifest.interaction_count || 0);
        // A full compact open is truthful only when IndexedDB contains every
        // interaction advertised by the manifest. Never turn an incomplete
        // hybrid tail into a fake complete transcript.
        if (opts.fullCompact === true && knownCount > messages.length) {
          return _fetchAndCacheInteractions(sessionId, limit, opts);
        }
        const requestedLimit = opts.fullCompact === true ? Infinity : limit;
        if (!_cacheCanServeWindow(messages, requestedLimit, opts, knownCount)) {
          return _fetchAndCacheInteractions(sessionId, limit, opts);
        }
        const windowed = _windowInteractionsClientSide(messages, requestedLimit, opts);
        // The cached transcript may be only a partial window (newest-N was
        // stored on the last open): report older rows truthfully from the
        // manifest so scroll-up paging still queries the server when online.
        if (knownCount > all.length) windowed.has_more = true;
        let maxSeq = 0;
        for (const m of all) {
          if (Number(m.session_seq || 0) > maxSeq) maxSeq = Number(m.session_seq);
        }
        windowed.max_session_seq = maxSeq;
        // Touch access telemetry. Actual quota eviction follows the authoritative
        // visible session list, not transcript age or this access timestamp.
        defaultSessionDB.updateSession(sessionId, {
          last_accessed_at: new Date().toISOString(),
        }).catch(() => {});
        return {
          ...windowed,
          manifest,
          light: true,
          cache_status: 'cached-hit',
          // Every visit validates in parallel. Cache freshness never blocks
          // the immediate IndexedDB paint and never controls retention.
          refresh_pending: true,
        };
      }

      // Cache miss / corrupt / explicit refresh — replace it from the
      // authoritative response.
      try {
        return await _fetchAndCacheInteractions(sessionId, limit, opts);
      } catch (err) {
        // Offline / server failure — degrade to whatever is cached rather than
        // leaving the loading skeleton on screen. The caller renders it as a
        // normal window; the reconcile loop keeps trying to catch up later.
        if (structurallyValid) {
          const { messages } = _shapeInteractions(all);
          const windowed = _windowInteractionsClientSide(messages, limit, opts);
          windowed.max_session_seq = 0;
          for (const m of all) {
            if (Number(m.session_seq || 0) > windowed.max_session_seq) {
              windowed.max_session_seq = Number(m.session_seq);
            }
          }
          windowed.cache_status = 'cached-fallback';
          return windowed;
        }
        throw err;
      }
    }

    // Normal mode — existing server API call
    return _fetchInteractionsFromServer(sessionId, limit, opts);
  },

  /**
   * Pre-warm a session's newest window into the hybrid IndexedDB cache without
   * opening it. See module-level warmSessionIntoCache.
   */
  warmSessionIntoCache(sessionId, limit) {
    return warmSessionIntoCache.call(this, sessionId, limit);
  },

  /**
   * Pre-warm a session's ENTIRE transcript into the hybrid IndexedDB cache
   * without opening it. See module-level warmFullTranscript.
   */
  warmFullTranscript(sessionId, pageSize, maxPages) {
    return warmFullTranscript.call(this, sessionId, pageSize, maxPages);
  },

  /**
   * Merge fetched messages into the hybrid IndexedDB cache for a session,
   * preserving existing rows. See module-level mergeInteractionsIntoCache.
   */
  mergeInteractionsIntoCache(sessionId, messages, manifest) {
    return mergeInteractionsIntoCache.call(this, sessionId, messages, manifest);
  },

  /**
   * Return the whole compact IndexedDB projection without contacting the
   * server. Callers use this only after manifest validation: it is a
   * cache-coherence snapshot, not an authority read.
   */
  async getCachedTranscriptProjection(sessionId) {
    if (_mode !== MODE_HYBRID || !sessionId) return null;
    const [session, rows] = await Promise.all([
      defaultSessionDB.getSession(sessionId),
      defaultSessionDB.getInteractions(sessionId, Infinity),
    ]);
    if (!session || !Array.isArray(rows) || !rows.length) return null;
    const { messages } = _shapeInteractions(rows);
    let firstSeq = null;
    let lastSeq = null;
    for (const row of messages) {
      const seq = Number(row && row.session_seq);
      if (!Number.isFinite(seq)) continue;
      if (firstSeq == null || seq < firstSeq) firstSeq = seq;
      if (lastSeq == null || seq > lastSeq) lastSeq = seq;
    }
    return {
      messages,
      cache_projection: {
        row_count: messages.length,
        first_session_seq: firstSeq,
        last_session_seq: lastSeq,
        complete: session.cache_complete_transcript === true,
      },
    };
  },

  /** Persist a live projection into the hybrid cache (throttled by caller). */
  async cacheInteractionProjection(sessionId, message) {
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('cache.storage.projection', {
        session_id: sessionId,
        interaction_id: message?.id,
        interaction_seq: message?.session_seq,
        text_length: typeof message?.content==='string'?message.content.length:null,
      });
    } catch (_) {}

    if (_mode !== MODE_HYBRID || !sessionId || !message?.id) return false;
    try {
      const slim = _slimForCacheWrite([{ ...message }])[0];
      return await defaultSessionDB.upsertCachedInteraction(sessionId, slim);
    } catch (_) { return false; }
  },

  /** Validate the cached manifest, downloading a full slim replacement only
   * when server authority changed. */
  async revalidateTranscript(sessionId) {
    if (_mode !== MODE_HYBRID) return { not_modified: true };
    const cachedSession = await defaultSessionDB.getSession(sessionId);
    const check = await _fetchInteractionsFromServer(sessionId, 1, {
      light: true,
      manifestOnly: true,
      knownRevision: Number(cachedSession?.authority_revision || 0),
      knownHash: cachedSession?.content_hash || '',
      refresh: true,
    });
    if (check.not_modified === true) {
      // A legacy/newest-N cache can have the correct authority revision while
      // still beginning in the middle of an activity phase.  Such a window is
      // not render-complete: the closer (or user boundary) just before it is
      // required to keep phase ownership deterministic. Repair it even when
      // the manifest itself has not changed.
      const cached = await defaultSessionDB.getInteractions(sessionId, Infinity);
      const { messages } = _shapeInteractions(cached);
      if (messages.length && !_isHardRenderBoundary(messages[0])) {
        return _fetchAndCacheInteractions(sessionId, 40, {
          light: true,
          refresh: true,
          completeTurnBoundary: true,
        });
      }
      await defaultSessionDB.updateSession(sessionId, {
        last_validated_at: new Date().toISOString(),
      });
      const projection = await this.getCachedTranscriptProjection(sessionId);
      return {
        ...check,
        messages: projection ? projection.messages : null,
        cache_projection: projection ? projection.cache_projection : null,
      };
    }
    return _fetchFullTranscriptReplacement(sessionId, check.manifest || null);
  },
};

async function _effectiveCacheBudget() {
  const configured = Number(_cachePolicy.max_bytes) || 512 * 1024 * 1024;
  try {
    const estimate = await navigator.storage?.estimate?.();
    const quota = Number(estimate?.quota || 0);
    if (quota > 0) return Math.max(1024 * 1024, Math.min(configured, Math.floor(quota * 0.25)));
  } catch (_) { /* configured ceiling is the fallback */ }
  return configured;
}

function _startLifecycleSweep() {
  if (_cleanupTimer !== null) return;
  _cleanupTimer = window.setInterval(async () => {
    try {
      const detail = await defaultSessionDB.enforceLifecyclePolicy();
      window.dispatchEvent(new CustomEvent('webagent-browser-storage-cleanup', { detail }));
    } catch (error) {
      window.dispatchEvent(new CustomEvent('webagent-browser-storage-cleanup', {
        detail: {
          last_cleanup_at: new Date().toISOString(),
          rows_removed: 0,
          bytes_removed: 0,
          errors: [String(error?.message || error)],
        },
      }));
    }
  }, 60_000);
}

// ── Internal helpers (shared across modes) ──────────────────────────────────

function _shapeSessions(sessions) {
  return sessions.map(s => ({
    id: s.id,
    title: s.title || 'New Session',
    agent_id: s.agent_id || null,
    agent_name: s.agent_name || '',
    agent_icon: s.agent_icon || '',
    agent_engine: s.agent_engine || '',
    created_at: s.created_at,
    updated_at: s.updated_at || s.created_at,
    activity_at: s.activity_at || null,
    activity_count: Number(s.activity_count ?? s.turn_count ?? s.interaction_count ?? 0),
    pinned: !!s.pinned,
    sort_order: Number.isFinite(s.sort_order) ? s.sort_order : null,
    hidden: !!s.hidden,
    run_status: s.run_status || null,
    run_updated_at: s.run_updated_at || null,
    // The closer's own verdict + note for the session's latest run
    // (app/api/db_viewer.py, _fetch_closing_flags). Read by
    // ui/shared/js/session-flag.js as the preferred flag, and by the
    // notification pill/row for its note. Absent when the gate is off.
    closing_flag: s.closing_flag || null,
    closing_cta: s.closing_cta || null,
    // Not persisted to IndexedDB (see _cacheSessionList) — the cached rehydrate
    // path falls back to run_status, which the shared flag resolver handles.
    execution_mode: s.execution_mode || null,
    queue_position: typeof s.queue_position === 'number' ? s.queue_position : null,
    queue_total: typeof s.queue_total === 'number' ? s.queue_total : null,
    has_unread: !!s.has_unread,
    read_at: s.read_at || null,
    unread_at: s.unread_at || null,
    child_count: s.child_count || 0,
    interaction_count: Number(s.interaction_count || 0),
    authority_revision: Number(s.authority_revision || s.revision || 0),
    content_hash: s.content_hash || '',
    cache_schema_version: Number(s.cache_schema_version || 0),
    cache_expires_at: s.cache_expires_at || null,
    last_validated_at: s.last_validated_at || null,
    _authority: s._authority || null,
  }));
}

async function _fetchSessionListFromServer(userId, opts = {}) {
  // limit=0 = no cap, so the session list matches the Sessions page and the
  // chat-header dropdown (both fetch the full list).
  let url = `/api/v1/db/sessions?db=user.db&user_id=${encodeURIComponent(userId)}&limit=0`;
  if (opts.includeHidden) url += '&include_hidden=1';
  const res = await fetch(apiPath(url), { headers: authHeaders() });
  if (!res.ok) throw new Error(`Session list failed (HTTP ${res.status})`);
  const data = await res.json();
  return _shapeSessions(
    (data.sessions || []).map(s => ({
      ...s,
      sort_order: Number.isFinite(s.sort_order) ? s.sort_order : null,
    }))
  );
}

async function _persistSessionOrderToServer(userId, orderedIds) {
  const res = await fetch(apiPath('/api/v1/db/sessions/reorder?db=user.db'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ user_id: userId, order: orderedIds }),
  });
  if (!res.ok) throw new Error(`Session reorder failed (HTTP ${res.status})`);
  return res.json();
}

async function _cacheSessionList(sessions) {
  // Cache sessions in IndexedDB via direct DB access (browserRouter methods
  // are mode-guarded, but the DB is always available).
  await defaultSessionDB.ready();
  // Prune cached rows whose session is no longer in the server list — deleted
  // or binned elsewhere (admin Sessions page, another device, bin restore or
  // purge). Never prune dirty rows (pending local mutations — the sync layer
  // owns those) or browser-authority rows. Hidden sessions ARE in the list
  // because the background refresh fetches include_hidden, so they survive.
  try {
    const cached = await defaultSessionDB.listSessions();
    const serverIds = new Set((sessions || []).map(s => s.id));
    for (const row of cached) {
      if (row._authority !== 'server' || row._dirty) continue;
      if (!serverIds.has(row.id)) {
        await defaultSessionDB.deleteCachedSession(row.id);
      }
    }
  } catch (_) { /* best-effort */ }
  for (const s of sessions) {
    const revision = Number(s.authority_revision || s.revision || 0);
    if (!revision || !s.content_hash) continue;
    // Preserve the client-only manifest fields the pre-warm / open-refresh
    // paths write. The server list payload does NOT carry cache_payload_hash
    // or cached_message_count — a bare upsert would wipe them, which then
    // fails structural validation on every open and forces a server re-fetch
    // that truncates the full cached transcript down to a newest-N window.
    let existing = null;
    try { existing = await defaultSessionDB.getSession(s.id); } catch (_) {}
    const cacheFields = {
      authority_revision: revision,
      content_hash: s.content_hash,
      cache_schema_version: Number(_cachePolicy.schema_version),
      cache_expires_at: null,
      last_validated_at: existing?.last_validated_at || null,
      last_accessed_at: new Date().toISOString(),
      _authority: 'server',
      _dirty: false,
      activity_count: Number(s.activity_count ?? s.turn_count ?? s.interaction_count ?? 0),
      interaction_count: Number(s.interaction_count || 0),
      cache_payload_hash: existing?.cache_payload_hash ?? null,
      cached_message_count: existing?.cached_message_count ?? null,
    };
    try {
      await defaultSessionDB.createSession({
        id: s.id,
        agent_id: s.agent_id || '',
        title: s.title || '',
        created_at: s.created_at,
        updated_at: s.updated_at,
        activity_at: s.activity_at || null,
        pinned: s.pinned,
        hidden: s.hidden,
        sort_order: s.sort_order,
        run_status: s.run_status || null,
        run_updated_at: s.run_updated_at || null,
        queue_position: typeof s.queue_position === 'number' ? s.queue_position : null,
        queue_total: typeof s.queue_total === 'number' ? s.queue_total : null,
        has_unread: !!s.has_unread,
        read_at: s.read_at || null,
        unread_at: s.unread_at || null,
        child_count: s.child_count || 0,
        // Display fields — the dropdown renders agent name/icon from the
        // cached list, so they must survive the round trip through IDB.
        agent_name: s.agent_name || '',
        agent_icon: s.agent_icon || '',
        agent_engine: s.agent_engine || '',
        ...cacheFields,
      });
    } catch (_) {
      // Already exists — update instead
      try { await defaultSessionDB.updateSession(s.id, {
        title: s.title || '',
        updated_at: s.updated_at,
        activity_at: s.activity_at || null,
        pinned: s.pinned,
        hidden: s.hidden,
        sort_order: s.sort_order,
        run_status: s.run_status || null,
        run_updated_at: s.run_updated_at || null,
        queue_position: typeof s.queue_position === 'number' ? s.queue_position : null,
        queue_total: typeof s.queue_total === 'number' ? s.queue_total : null,
        has_unread: !!s.has_unread,
        read_at: s.read_at || null,
        unread_at: s.unread_at || null,
        child_count: s.child_count || 0,
        agent_name: s.agent_name || '',
        agent_icon: s.agent_icon || '',
        agent_engine: s.agent_engine || '',
        ...cacheFields,
      }); } catch (__) { /* best-effort */ }
    }
  }
  try {
    await defaultSessionDB.enforceCachePolicy({
      maxBytes: await _effectiveCacheBudget(),
      orderedIds: (sessions || []).map(session => session.id),
      protectedSessionId: app.currentSessionId || null,
    });
  } catch (_) { /* cache retention is best-effort */ }
}

function _refreshSessionListFromServer(userId) {
  const key = String(userId || '');
  const existing = _sessionListRefreshes.get(key);
  if (existing) return existing;

  const refresh = (async () => {
    try {
      // include_hidden keeps hidden sessions in the server list so the cache
      // prune in _cacheSessionList removes only truly-deleted/binned rows.
      const fromServer = await _fetchSessionListFromServer(userId, { includeHidden: true });
      await _cacheSessionList(fromServer);
      // One delta per completed authority read. Concurrent cache consumers all
      // share this refresh, so they cannot multiply network/cache/event work.
      window.dispatchEvent(new CustomEvent('sessions-delta', {
        detail: { sessions: fromServer },
      }));
    } catch (_) { /* background — silence failures */ }
    finally {
      if (_sessionListRefreshes.get(key) === refresh) {
        _sessionListRefreshes.delete(key);
      }
    }
  })();
  _sessionListRefreshes.set(key, refresh);
  return refresh;
}

/**
 * Decide whether a cached transcript can truthfully answer a windowed request.
 * Plain newest-N opens are always served from the cache; navigation/paging
 * windows (at_start, around_id, nearest_user_before_id, nearest_user_after_id,
 * before_id, after_id) are only served when the cached rows actually contain the requested anchor
 * (or, for at_start, the whole transcript). Otherwise the server must answer
 * and the cache acts purely as the offline fallback.
 */
function _cacheCanServeWindow(messages, limit, opts, knownCount) {
  if (!opts.beforeId && !opts.afterId && !opts.aroundId
      && !opts.atStart && !opts.nearestUserBeforeId && !opts.nearestUserAfterId) {
    // A plain newest-N cache is only safe for canonical chat rendering when
    // its first row is a durable boundary. Otherwise the omitted predecessor
    // may be a closer/user row which changes how every following activity
    // bubble is grouped.
    if (opts.completeTurnBoundary && messages.length
        && !_isHardRenderBoundary(messages[0])) return false;
    return true; // plain newest-N open — always served from cache
  }
  if (opts.atStart) {
    // Cache holds the whole transcript (or at least everything the manifest
    // knows about) — the oldest cached row IS the session start.
    return knownCount <= messages.length;
  }
  if (opts.aroundId || opts.nearestUserBeforeId || opts.nearestUserAfterId) {
    const anchor = opts.aroundId || opts.nearestUserBeforeId || opts.nearestUserAfterId;
    return anchor ? messages.some(m => m.id === anchor) : false;
  }
  if (opts.beforeId) {
    // Scroll-up paging: only satisfiable when older rows exist in the cache.
    const idx = messages.findIndex(m => m.id === opts.beforeId);
    return idx > 0;
  }
  if (opts.afterId) {
    // Scroll-down paging: only satisfiable when newer rows exist in the cache.
    const idx = messages.findIndex(m => m.id === opts.afterId);
    return idx !== -1 && idx < messages.length - 1;
  }
  return true;
}

/**
 * Client-side windowing for browser-authority mode, where the full transcript
 * lives in IndexedDB and the server API isn't consulted. Mirrors the server's
 * `/session-messages` cursor semantics: newest-N by default, plus at_start,
 * nearest_user_before_id, nearest_user_after_id, around_id, before_id and after_id windows. `messages`
 * must be sorted oldest-first (session_seq ascending).
 */
function _windowInteractionsClientSide(messages, limit, opts = {}) {
  const lim = limit || 20;
  const sorted = sortTranscriptCanonical(messages);

  if (opts.atStart) {
    const slice = sorted.slice(0, lim);
    return { messages: slice, has_more: false, has_newer: slice.length < sorted.length };
  }
  if (opts.nearestUserBeforeId) {
    const idx = sorted.findIndex(m => m.id === opts.nearestUserBeforeId);
    const end = idx === -1 ? sorted.length : idx;
    const user = [...sorted.slice(0, end)].reverse().find(m => m.role === 'user');
    return { messages: user ? [user] : [], has_more: false, has_newer: false };
  }
  if (opts.nearestUserAfterId) {
    const idx = sorted.findIndex(m => m.id === opts.nearestUserAfterId);
    const start = idx === -1 ? 0 : idx + 1;
    const user = sorted.slice(start).find(m => m.role === 'user');
    return { messages: user ? [user] : [], has_more: false, has_newer: false };
  }
  if (opts.aroundId) {
    const idx = sorted.findIndex(m => m.id === opts.aroundId);
    if (idx === -1) {
      const slice = sorted.slice(-lim);
      return { messages: slice, has_more: sorted.length > lim, has_newer: false };
    }
    const start = Math.max(0, idx - lim);
    const end = Math.min(sorted.length, idx + lim + 1);
    return {
      messages: sorted.slice(start, end),
      has_more: start > 0,
      has_newer: end < sorted.length,
    };
  }
  if (opts.beforeId) {
    const idx = sorted.findIndex(m => m.id === opts.beforeId);
    const end = idx === -1 ? sorted.length : idx;
    const start = Math.max(0, end - lim);
    return { messages: sorted.slice(start, end), has_more: start > 0, has_newer: idx !== -1 };
  }
  if (opts.afterId) {
    const idx = sorted.findIndex(m => m.id === opts.afterId);
    const start = idx === -1 ? 0 : idx + 1;
    return {
      messages: sorted.slice(start, start + lim),
      has_more: idx !== -1,
      has_newer: start + lim < sorted.length,
    };
  }
  // Default: newest N.
  let start = Math.max(0, sorted.length - lim);
  if (opts.completeTurnBoundary) {
    while (start > 0 && !_isHardRenderBoundary(sorted[start])) start -= 1;
  }
  const slice = sorted.slice(start);
  return { messages: slice, has_more: start > 0, has_newer: false };
}

function _isHardRenderBoundary(message) {
  if (!message) return false;
  return message.role === 'user';
}

// Heal legacy cached rows that predate source persistence (the original cache
// writer dropped `source`): a role='system' row whose metadata carries
// kind:'summary' IS an Output Summarizer recap. Restore its source +
// message_type so every consumer (visibility lanes, Summary bubble renderers,
// dedup, virtual scroll) treats it as a summary — not a plain system notice.
function _rowIsSummary(ix) {
  if (!ix || ix.role !== 'system') return false;
  const src = ix.source || '';
  if (src === 'system:summary' || src === 'system:overview') return true;
  if (ix.message_type === 'summary') return true;
  try {
    const meta = typeof ix.metadata === 'string' ? JSON.parse(ix.metadata) : ix.metadata;
    return !!(meta && meta.kind === 'summary');
  } catch (_) { /* non-fatal */ }
  return false;
}

function _shapeInteractions(interactions) {
  // Canonical transcript order must match the server exactly: session_seq is
  // authoritative, created_at is the deterministic tie-breaker, id is the
  // final stable tie-break (server uses rowid; we don't persist it, and UUID
  // order is at least deterministic). IndexedDB's index order can disagree on
  // ties (equal seqs from legacy rows), so always sort on read.
  const sorted = sortTranscriptCanonical(interactions);
  return {
    messages: sorted.map(ix => {
      const isSummary = _rowIsSummary(ix);
      return {
        id: ix.id,
        session_id: ix.session_id,
        role: ix.role,
        content: ix.content,
        tool_name: ix.tool_name,
        output: ix.output,
        session_seq: ix.session_seq,
        created_at: ix.created_at,
        metadata: ix.metadata,
        status: ix.status,
        // Ordering/rendering metadata the UI depends on: source drives the
        // Summary bubble (system:summary/system:overview), turn_id groups tool
        // calls, message_phase/message_type preserve the server's classification.
        source: isSummary ? (ix.source || 'system:summary') : (ix.source || null),
        turn_id: ix.turn_id || null,
        turn_seq: ix.turn_seq || null,
        parent_id: ix.parent_id || null,
        tool_call_id: ix.tool_call_id || null,
        tool_calls: ix.tool_calls || null,
        message_phase: ix.message_phase || null,
        message_type: ix.message_type || (isSummary ? 'summary' : null),
      };
    }),
    has_more: false,
  };
}

async function _fetchInteractionsFromServer(sessionId, limit, opts = {}) {
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${limit}`);
  if (opts.light !== false) url += '&light=1';
  if (opts.compactActivity !== false) url += '&compact_activity=1';
  if (opts.beforeId) url += `&before_id=${encodeURIComponent(opts.beforeId)}`;
  if (opts.afterId) url += `&after_id=${encodeURIComponent(opts.afterId)}`;
  if (opts.aroundId) url += `&around_id=${encodeURIComponent(opts.aroundId)}`;
  if (opts.atStart) url += '&at_start=1';
  if (opts.nearestUserBeforeId) url += `&nearest_user_before_id=${encodeURIComponent(opts.nearestUserBeforeId)}`;
  if (opts.nearestUserAfterId) url += `&nearest_user_after_id=${encodeURIComponent(opts.nearestUserAfterId)}`;
  if (opts.completeTurnBoundary) url += '&complete_turn_boundary=true';
  if (typeof opts.afterSeq === 'number') url += `&after_seq=${opts.afterSeq}`;
  if (opts.manifestOnly) url += '&manifest_only=true';
  if (typeof opts.knownRevision === 'number') {
    url += `&known_revision=${encodeURIComponent(opts.knownRevision)}`;
  }
  if (opts.knownHash) url += `&known_hash=${encodeURIComponent(opts.knownHash)}`;
  if (opts.refresh) url += `&_refresh=${Date.now()}`;
  const res = await fetch(url, {
    headers: authHeaders(),
    cache: 'no-store',
    ...(opts.priority ? { priority: opts.priority } : {}),
  });
  if (!res.ok) {
    const error = new Error(`Session history request failed (${res.status})`);
    error.status = res.status;
    throw error;
  }
  return await res.json();
}

async function _fetchFullTranscriptReplacement(sessionId, initialManifest = null) {
  const rows = [];
  let beforeId = null;
  let manifest = initialManifest;
  let first = true;
  for (;;) {
    const opts = { light: true, refresh: true };
    if (beforeId) opts.beforeId = beforeId;
    const page = await _fetchInteractionsFromServer(sessionId, 500, opts);
    const batch = Array.isArray(page.messages) ? page.messages : [];
    if (first && page.manifest) manifest = page.manifest;
    first = false;
    if (!batch.length) break;
    rows.unshift(...batch);
    const oldest = batch[0];
    if (!page.has_more || !oldest?.id) break;
    beforeId = oldest.id;
  }
  const ordered = sortTranscriptCanonical(rows);
  const slim = _slimForCacheWrite(ordered.map(row => ({ ...row })));
  await defaultSessionDB.replaceInteractions(sessionId, slim);
  const stored = await defaultSessionDB.getInteractions(sessionId, Infinity);
  if (manifest) await _updateCachedManifest(sessionId, manifest, stored);
  await defaultSessionDB.updateSession(sessionId, {
    cache_complete_transcript: true,
  });
  return {
    messages: stored,
    manifest,
    light: true,
    has_more: false,
    has_newer: false,
    max_session_seq: stored.reduce((max, row) => Math.max(max, Number(row.session_seq || 0)), 0),
    not_modified: false,
  };
}

/**
 * Fetch the authoritative window from the server and replace the IndexedDB
 * cached transcript + manifest with it (hybrid mode cache write-back).
 */
async function _fetchAndCacheInteractions(sessionId, limit, opts) {
  const data = await _fetchInteractionsFromServer(sessionId, limit, opts);
  try {
    // Cache-write slim: never persist heavy tool-call bodies regardless of what
    // the fetch returned (light fetches arrive pre-slimmed; this is the
    // defense-in-depth guarantee). The caller still receives the ORIGINAL
    // `data.messages` for rendering.
    const toStore = _slimForCacheWrite((data.messages || []).map(m => ({ ...m })));
    // The fetched rows are server-authoritative. Replace atomically and retain a
    // genuinely missing sequence as NULL; assigning a browser-local number here
    // makes IndexedDB disagree with the served transcript and causes reordering.
    await defaultSessionDB.replaceInteractions(sessionId, toStore);
    // The server may expand newest-N backwards to a hard render boundary, so
    // read every row we just stored. Recording only `limit` rows in the cache
    // manifest makes a valid expanded cache look corrupt on the next visit.
    const stored = await defaultSessionDB.getInteractions(sessionId, Infinity);
    await _updateCachedManifest(sessionId, data.manifest, stored);
    // A cache may start at a user boundary yet still stop in the middle of its
    // activity run. Keep it eligible for instant rendering only when the server
    // explicitly supplied the complete owning turn.
    await defaultSessionDB.updateSession(sessionId, {
      cache_complete_turn: data.turn_boundary_complete === true
        && data.turn_boundary_truncated !== true,
      // A normal open is a newest window unless the authority explicitly says
      // both sides are exhausted. Do not mislabel it as a complete projection.
      cache_complete_transcript: data.has_more !== true && data.has_newer !== true,
    });
  } catch (_) { /* best-effort */ }
  return data;
}

/**
 * Cache-write slim: guarantee that HEAVY tool-call content never persists in
 * IndexedDB, no matter what was fetched. Tool result bodies (tool rows) and
 * tool-call arguments (assistant output) are view-once — they are re-fetched
 * lazily via /session-tool-detail when one row is opened, and blanking
 * them at the write boundary keeps the cache small and secret-free. What IS
 * kept is exactly the visible transcript: the tool-call HEADING (name +
 * duration) and the assistant's progress UPDATES. Synthetic tool rows obey the
 * same rule; their stable interaction id is enough for per-call detail fetch.
 */
function _slimForCacheWrite(messages) {
  if (!Array.isArray(messages)) return messages;
  for (const m of messages) {
    if (!m || typeof m !== 'object') continue;
    if (m.role === 'tool') {
      m.content = '';
      m.output = null;
      let metadata = {};
      try { metadata = typeof m.metadata === 'string' ? JSON.parse(m.metadata) : (m.metadata || {}); } catch (_) {}
      const kept = {};
      for (const key of ['duration_ms', 'error', 'brain', 'skipped', 'message_phase', 'message_type']) {
        if (metadata[key] !== undefined) kept[key] = metadata[key];
      }
      m.metadata = Object.keys(kept).length ? JSON.stringify(kept) : null;
    } else if (m.role === 'assistant' && m.output) {
      let parsed = null;
      try { parsed = typeof m.output === 'string' ? JSON.parse(m.output) : m.output; } catch (_) { parsed = null; }
      if (parsed && Array.isArray(parsed.tool_calls)) {
        const slim = {
          tool_calls: parsed.tool_calls.map(tc => ({
            id: tc.id || null,
            function: { name: (tc.function || {}).name, arguments: '' },
          })),
        };
        if (parsed._sent_messages) slim._has_sent_schema = true;
        m.output = JSON.stringify(slim);
      } else {
        m.output = null;
      }
    }
  }
  return messages;
}

/**
 * Pre-warm a session's newest window into the hybrid IndexedDB cache WITHOUT
 * opening it (session-prewarm.js + the swipe neighbour warmer). Fetches the
 * authoritative newest window (light, low network priority) from the server,
 * MERGES it over whatever is already cached (never clears — older rows stay
 * readable offline), and refreshes the manifest so the next open is an
 * instant, non-stale cache hit. No-op outside hybrid mode.
 * @returns {Promise<{warmed: boolean, messages?: Array, manifest?: Object|null}>}
 */
async function warmSessionIntoCache(sessionId, limit = 40) {
  if (_mode !== MODE_HYBRID) return { warmed: false };
  const data = await _fetchInteractionsFromServer(sessionId, limit, {
    light: true,
    priority: 'low',
    completeTurnBoundary: true,
  });
  if (!data || data.restricted || !Array.isArray(data.messages)) {
    return { warmed: false };
  }
  if (!data.messages.length) return { warmed: false, messages: [] };
  try {
    // No row cap: merging a newest-40 window must never shrink an
    // already-fully-warmed transcript (see mergeInteractionsIntoCache).
    await this.mergeInteractionsIntoCache(sessionId, data.messages, data.manifest || null, Infinity);
  } catch (_) {
    // The IndexedDB write failed — report unwarmed so the caller (pre-warm)
    // doesn't mark this session as freshly warmed and can retry next event.
    return { warmed: false, messages: data.messages, manifest: data.manifest || null };
  }
  return { warmed: true, messages: data.messages, manifest: data.manifest || null };
}

/**
 * Pre-warm a session's ENTIRE transcript into the hybrid IndexedDB cache
 * (paginated newest-first, merged per page so partial progress survives an
 * interruption). Fetches light rows in pages of `pageSize` and keeps walking
 * older via before_id until has_more is false, the page cap is hit, or the app
 * turns busy. Every page is slimmed at the write boundary and merged WITHOUT
 * clearing existing rows, so user/system/summary rows and tool-call headings
 * accumulate and older history stays readable. No-op outside hybrid mode.
 * @returns {Promise<{warmed: boolean, messages: number, pages: number}>}
 */
async function warmFullTranscript(sessionId, pageSize = 200, maxPages = 50) {
  if (_mode !== MODE_HYBRID) return { warmed: false, messages: 0, pages: 0 };
  let beforeId = null;
  let pages = 0;
  let total = 0;
  let complete = false;
  for (;;) {
    const opts = { light: true, priority: 'low' };
    if (beforeId) opts.beforeId = beforeId;
    const data = await _fetchInteractionsFromServer(sessionId, pageSize, opts);
    if (!data || data.restricted || !Array.isArray(data.messages) || !data.messages.length) break;
    try {
      // Full warm = no per-session row cap: the device quota guard in the
      // pre-warm loop is the real limiter, so long sessions keep ALL history.
      await this.mergeInteractionsIntoCache(sessionId, data.messages, data.manifest || null, Infinity);
      total += data.messages.length;
    } catch (_) {
      break; // IndexedDB write failed — stop; the caller can retry later
    }
    pages += 1;
    const oldest = data.messages[0]; // response is oldest-first
    if (!data.has_more) {
      complete = true;
      break;
    }
    if (!oldest || !oldest.id || pages >= maxPages) break;
    beforeId = oldest.id;
  }
  if (total > 0) {
    try {
      await defaultSessionDB.updateSession(sessionId, {
        cache_complete_transcript: complete,
      });
    } catch (_) { /* best-effort completeness metadata */ }
  }
  return { warmed: total > 0, messages: total, pages };
}

/**
 * Merge fetched messages into the hybrid IndexedDB cache for a session,
 * preserving whatever is already stored (dedupe by interaction id) and
 * refreshing the manifest over the merged set. Used by pre-warming; never
 * clears the whole transcript the way the open/refresh path does. No-op
 * outside hybrid mode.
 *
 * maxRows is intentionally UNBOUNDED by default (Infinity): a merge must
 * never silently shrink an already-fully-warmed transcript back to a window
 * (a later newest-40 merge over a 15k-row session would otherwise re-cap it
 * to 1000). The device cache quota — enforced by the pre-warm loop's quota
 * guard and the boot-time enforceCachePolicy LRU — is the real limiter.
 */
async function mergeInteractionsIntoCache(sessionId, messages, manifest, maxRows = Infinity) {
  if (_mode !== MODE_HYBRID) return false;
  await defaultSessionDB.ready();
  const existing = await defaultSessionDB.getInteractions(sessionId, Infinity);
  const byId = new Map();
  for (const r of existing) {
    if (r && r.id) byId.set(r.id, r);
  }
  // Slim the incoming rows at the write boundary — tool bodies are view-once
  // and never persist (see _slimForCacheWrite).
  const incoming = _slimForCacheWrite(Array.isArray(messages) ? messages.map(m => ({ ...m })) : []);
  // Incoming rows are newer server projections. Replace matching cached IDs as
  // well as adding unseen IDs so a streaming/truncated row can become complete.
  for (const m of incoming) {
    if (m && m.id) byId.set(m.id, m);
  }
  let merged = [...byId.values()];
  if (Number.isFinite(maxRows) && maxRows > 0 && merged.length > maxRows) {
    merged = sortTranscriptCanonical(merged);
    merged = merged.slice(merged.length - maxRows);
  }
  await defaultSessionDB.replaceInteractions(sessionId, merged);
  // Always refresh the client-side manifest hash over the merged set. Paged /
  // windowed server fetches may omit the manifest — the cached rows are still
  // authoritative for what's stored, so recompute the payload hash and keep
  // the existing server manifest fields (revision/content_hash) when this
  // fetch didn't carry one. Without this, a manifest-less merge leaves the
  // session row without cache_payload_hash → validation fails on open.
  const stored = await defaultSessionDB.getInteractions(sessionId, Infinity);
  if (manifest) {
    await _updateCachedManifest(sessionId, manifest, stored);
  } else {
    let existingRow = null;
    try { existingRow = await defaultSessionDB.getSession(sessionId); } catch (_) {}
    if (existingRow && existingRow.content_hash) {
      await _updateCachedManifest(sessionId, {
        content_hash: existingRow.content_hash,
        authority_revision: existingRow.authority_revision,
        cache_schema_version: existingRow.cache_schema_version,
        interaction_count: existingRow.interaction_count,
      }, stored);
    }
  }
  return true;
}

async function _hashCachedInteractions(rows) {
  const canonical = (rows || []).map(row => ({
    id: row.id || '',
    session_id: row.session_id || '',
    role: row.role || '',
    content: row.content || '',
    tool_name: row.tool_name || null,
    tool_call_id: row.tool_call_id || null,
    output: row.output || null,
    session_seq: Number(row.session_seq || 0),
    created_at: row.created_at || '',
    metadata: row.metadata || null,
    status: row.status || null,
    parent_id: row.parent_id || null,
  }));
  const bytes = new TextEncoder().encode(JSON.stringify(canonical));
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return Array.from(new Uint8Array(digest))
    .map(byte => byte.toString(16).padStart(2, '0'))
    .join('');
}

async function _updateCachedManifest(sessionId, manifest, cachedRows = null) {
  if (!manifest?.content_hash) return;
  const changes = {
    authority_revision: Number(manifest.authority_revision || 0),
    content_hash: manifest.content_hash,
    cache_schema_version: Number(manifest.cache_schema_version || _cachePolicy.schema_version),
    interaction_count: Number(manifest.interaction_count || 0),
    last_validated_at: new Date().toISOString(),
    next_validation_at: new Date().toISOString(),
    cache_expires_at: null,
    last_accessed_at: new Date().toISOString(),
    _authority: 'server',
    _dirty: false,
    cache_projection_dirty: false,
  };
  if (cachedRows) {
    changes.cache_payload_hash = await _hashCachedInteractions(cachedRows);
    changes.cached_message_count = cachedRows.length;
  }
  // Upsert: a full warm can write interactions BEFORE the session row exists
  // (the warm fetch + session-list cache are independent paths). A silent
  // updateSession(false) would leave the row without cache_payload_hash and
  // every subsequent open would fail structural validation → server fetch.
  const updated = await defaultSessionDB.updateSession(sessionId, changes);
  if (!updated) {
    try {
      await defaultSessionDB.createSession({ id: sessionId, ...changes });
    } catch (_) { /* best-effort */ }
  }
}

async function _syncInteractionsFromServer(sessionId) {
  // Fetch only messages newer than what we have cached
  try {
    const cached = await defaultSessionDB.getInteractions(sessionId);
    let maxSeq = 0;
    for (const m of cached) {
      if (typeof m.session_seq === 'number' && m.session_seq > maxSeq) {
        maxSeq = m.session_seq;
      }
    }
    // Fetch only rows with session_seq > maxSeq via the server filter
    const data = await _fetchInteractionsFromServer(sessionId, 200, { afterSeq: maxSeq });
    const newMsgs = data.messages || [];
    if (newMsgs.length > 0) {
      // Slim at the write boundary — tool bodies are view-once and never
      // persist (see _slimForCacheWrite).
      await defaultSessionDB.addInteractions(sessionId, _slimForCacheWrite(newMsgs));
      // Dispatch delta event so the UI can merge new messages
      window.dispatchEvent(new CustomEvent('messages-delta', {
        detail: { sessionId, messages: newMsgs },
      }));
    }
  } catch (_) { /* background — silence failures */ }
}

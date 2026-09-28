'use strict';

/**
 * BrowserRouter — routes between IndexedDB (browser-authority) and server API
 * (normal) chat flows based on the current user's storage mode.
 *
 * The browser storage tier stores sessions, interactions, and agent config
 * in IndexedDB. The server is called only for:
 *   - agent config fetch (cached in IndexedDB)
 *   - LLM inference (stateless POST, no DB writes)
 *
 * Usage
 * -----
 *   import browserRouter from './storage/browser-router.js';
 *   await browserRouter.init('default');
 *   const sessions = await browserRouter.listSessions();
 *   const sid = await browserRouter.createSession({ agent_id: 'default' });
 *   const reply = await browserRouter.sendMessage(sid, 'hello');
 */

import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { randomUUID } from '../../../shared/js/uuid.js';
import defaultSessionDB from './indexeddb.js';
import { syncEngine } from './sync.js';

// ── Constants ──────────────────────────────────────────────────────────────

const MODE_BROWSER = 'browser';
const MODE_NORMAL = 'normal';

// ── State ──────────────────────────────────────────────────────────────────

let _mode = MODE_NORMAL;
let _agentId = '';
let _configHash = '';

// ── Helpers ────────────────────────────────────────────────────────────────

/**
 * Fetch the agent config from the server browser endpoint and cache it in
 * IndexedDB. Returns the config + config_hash.
 */
async function _fetchAndCacheAgentConfig(agentId) {
  const url = apiPath(`/api/v1/browser/config/${encodeURIComponent(agentId)}`);
  try {
    const resp = await fetch(url, { headers: authHeaders() });
    if (!resp.ok) throw new Error(`Browser config fetch failed: ${resp.status}`);
    const data = await resp.json();

    await defaultSessionDB.cacheAgentConfig({
      id: agentId,
      config_hash: data.config_hash,
      system_prompt: data.agent?.context_documents || [],
      tools: data.tools || [],
      abilities: data.abilities || [],
      agent_name: data.agent?.name || '',
      agent_description: data.agent?.description || '',
      model: data.agent?.model || '',
      cached_at: new Date().toISOString(),
    });

    _configHash = data.config_hash;
    return data;
  } catch (e) {
    console.warn('[BrowserRouter] Failed to fetch agent config:', e);
    const cached = await defaultSessionDB.getCachedAgentConfig(agentId);
    if (cached) {
      _configHash = cached.config_hash || '';
      return cached;
    }
    throw e;
  }
}

// ── Public API ─────────────────────────────────────────────────────────────

export class BrowserRouter {

  // ── Lifecycle ────────────────────────────────────────────────────────

  /**
   * Ensure the IndexedDB is open. Used by hybrid mode to warm the stores
   * without entering full browser-authority mode.
   * @returns {Promise<void>}
   */
  async _ensureReady() {
    await defaultSessionDB.ready();
  }

  /**
   * Initialise the router for the given agent.
   * If an agent_id is provided, the router enters browser-authority mode and
   * fetches the agent config. Without one, it stays in normal (server-backed) mode.
   * @param {string} [agentId]
   */
  async init(agentId) {
    if (agentId) {
      _mode = MODE_BROWSER;
      _agentId = agentId;
      await defaultSessionDB.ready();
      await _fetchAndCacheAgentConfig(agentId);
      console.log('[BrowserRouter] Initialised in browser mode for agent:', agentId);
    } else {
      _mode = MODE_NORMAL;
      _agentId = '';
      console.log('[BrowserRouter] Normal (server) mode');
    }
  }

  /**
   * Check whether the router is in browser-authority mode.
   * @returns {boolean}
   */
  get isBrowser() {
    return _mode === MODE_BROWSER;
  }

  /**
   * Switch to normal (server-backed) mode.
   */
  async switchToNormal() {
    _mode = MODE_NORMAL;
    _agentId = '';
    console.log('[BrowserRouter] Switched to normal mode');
  }

  /**
   * Get the current agent id.
   * @returns {string}
   */
  get agentId() {
    return _agentId;
  }

  // ── Session operations (IndexedDB) ───────────────────────────────────

  /**
   * List all sessions, sorted by updated_at descending.
   * @returns {Promise<Array>}
   */
  async listSessions() {
    if (_mode !== MODE_BROWSER) return [];
    return defaultSessionDB.listSessions();
  }

  /**
   * Get a single session by id.
   * @param {string} sessionId
   * @returns {Promise<Object|null>}
   */
  async getSession(sessionId) {
    if (_mode !== MODE_BROWSER) return null;
    return defaultSessionDB.getSession(sessionId);
  }

  /**
   * Create a new browser session in IndexedDB.
   * @param {Object} opts
   * @param {string} [opts.agent_id] — defaults to the init'd agent
   * @param {string} [opts.title]
   * @param {string} [opts.id] — optional explicit session id
   * @returns {Promise<string>} session id
   */
  async createSession(opts = {}) {
    if (_mode !== MODE_BROWSER) throw new Error('Not in browser mode');

    const agentId = opts.agent_id || _agentId;
    if (!agentId) throw new Error('No agent_id set for browser session');

    const sessionId = opts.id || randomUUID();
    await defaultSessionDB.createSession({
      id: sessionId,
      agent_id: agentId,
      title: opts.title || 'New Session',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      pinned: false,
      hidden: false,
      sync_level: 0,
      _dirty: true,    // mark for sync
    });

    syncEngine.markDirty(sessionId).catch(() => {});
    return sessionId;
  }

  /**
   * Update a session's metadata in IndexedDB.
   * @param {string} sessionId
   * @param {Object} changes
   * @returns {Promise<boolean>}
   */
  async updateSession(sessionId, changes) {
    if (_mode !== MODE_BROWSER) return false;
    const updated = await defaultSessionDB.updateSession(sessionId, changes);
    if (updated) await syncEngine.markDirty(sessionId);
    return updated;
  }

  /**
   * Delete a session and all its interactions from IndexedDB.
   * @param {string} sessionId
   */
  async deleteSession(sessionId) {
    if (_mode !== MODE_BROWSER) return;
    return defaultSessionDB.deleteSession(sessionId);
  }

  /**
   * Count total sessions.
   * @returns {Promise<number>}
   */
  async countSessions() {
    if (_mode !== MODE_BROWSER) return 0;
    return defaultSessionDB.countSessions();
  }

  // ── Interaction operations (IndexedDB) ───────────────────────────────

  /**
   * Get all interactions for a session.
   * @param {string} sessionId
   * @param {number} [limit]
   * @returns {Promise<Array>}
   */
  async getInteractions(sessionId, limit) {
    if (_mode !== MODE_BROWSER) return [];
    return defaultSessionDB.getInteractions(sessionId, limit);
  }

  /**
   * Append a single interaction to the local store.
   * @param {string} sessionId
   * @param {Object} msg — { role, content, ... }
   * @returns {Promise<Object>} saved doc
   */
  async addInteraction(sessionId, msg) {
    if (_mode !== MODE_BROWSER) return null;
    return defaultSessionDB.addInteraction(sessionId, msg);
  }

  /**
   * Append multiple interactions atomically.
   * @param {string} sessionId
   * @param {Array<Object>} messages
   * @returns {Promise<Array<Object>>}
   */
  async addInteractions(sessionId, messages) {
    if (_mode !== MODE_BROWSER) return [];
    return defaultSessionDB.addInteractions(sessionId, messages);
  }

  /**
   * Update a single interaction (e.g. finalize streaming).
   * @param {string} interactionId
   * @param {Object} changes
   * @returns {Promise<boolean>}
   */
  async updateInteraction(interactionId, changes) {
    if (_mode !== MODE_BROWSER) return false;
    return defaultSessionDB.updateInteraction(interactionId, changes);
  }

  /**
   * Count interactions for a session.
   * @param {string} sessionId
   * @returns {Promise<number>}
   */
  async countInteractions(sessionId) {
    if (_mode !== MODE_BROWSER) return 0;
    return defaultSessionDB.countInteractions(sessionId);
  }

  /**
   * Clear all interactions for a session (keeps the session).
   * @param {string} sessionId
   */
  async clearInteractions(sessionId) {
    if (_mode !== MODE_BROWSER) return;
    return defaultSessionDB.clearInteractions(sessionId);
  }

  // ── Send message (LLM call) ─────────────────────────────────────────

  /**
   * Send a message and get a streaming response.
   * In browser mode, the full interaction history is sent alongside the new
   * message. The server calls the LLM and streams back the response.
   *
   * SSE events yielded:
   *   'stream'   — incremental content chunk
   *   'response' — final complete content
   *   'error'    — error message
   *
   * @param {string} sessionId
   * @param {string} message
   * @param {Object} [opts]
   * @param {Function} [opts.onStream] — fn(chunk)
   * @param {Function} [opts.onResponse] — fn(content)
   * @param {Function} [opts.onError] — fn(err)
   * @returns {Promise<string>} the full assistant response
   */
  async sendMessage(sessionId, message, opts = {}) {
    if (_mode !== MODE_BROWSER) throw new Error('Not in browser mode');

    // 1. Snapshot the authoritative prior history before saving the new turn.
    // The new message is sent separately and must not appear twice.
    const interactions = await defaultSessionDB.getInteractions(sessionId);
    const session = await defaultSessionDB.getSession(sessionId);
    const historyRevision = Number(session?.authority_revision || 0);
    const historyToken = session?.history_token || null;
    const idempotencyKey = opts.idempotencyKey || randomUUID();

    // 2. Save the user message locally
    await defaultSessionDB.addInteraction(sessionId, {
      id: opts.userInteractionId || undefined,
      role: 'user',
      content: message,
      status: 'complete',
      chat_render_kind: 'user',
    });

    // 3. Send the complete prior transcript. Cache presence is not sufficient
    // proof that the server has an identical copy.
    const payload = {
      agent_id: _agentId,
      new_message: message,
      session_id: sessionId,
      config_hash: _configHash,
      execution_mode: 'auto',
      history_revision: historyRevision,
      history_token: historyToken,
      idempotency_key: idempotencyKey,
    };
    const serializedHistory = interactions.map(ix => ({
        id: ix.id,
        role: ix.role,
        content: ix.content,
        tool_name: ix.tool_name,
        output: ix.output,
        tool_calls: ix.tool_calls || null,
        tool_call_id: ix.tool_call_id || null,
        created_at: ix.created_at,
        session_seq: ix.session_seq,
      }));
    if (!historyToken) payload.interactions = serializedHistory;

    // 3. POST to the stateless browser endpoint
    const url = apiPath('/api/v1/browser/chat');
    try {
      const send = body => fetch(url, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify(body),
      });
      let resp = await send(payload);

      // A token is an optimization, never authority. On eviction, expiry,
      // concurrent consumption, or revision mismatch, retry once with the same
      // idempotency key and the complete IndexedDB transcript.
      if (resp.status === 409 && historyToken) {
        const problem = await resp.json().catch(() => ({}));
        if (problem?.detail?.code === 'history_required') {
          payload.history_token = null;
          payload.interactions = serializedHistory;
          resp = await send(payload);
        }
      }

      if (!resp.ok) {
        const errMsg = `Browser chat failed: ${resp.status} ${resp.statusText}`;
        opts.onError?.(errMsg);
        throw new Error(errMsg);
      }

      // 5. Stream the SSE response from the REAL agent loop
      const reader = resp.body.getReader();
      const decoder = new TextDecoder();
      let fullContent = '';
      let buffer = '';
      let currentRunId = null;
      const protocolInteractions = [];
      let pendingToolCalls = [];
      let finalAssistantId = null;
      let historyAck = null;
      let generatedTitle = null;
      const lifeCycleEvents = [];  // session_run, session_title, memory_saved

      function _flushPendingToolCalls() {
        if (!pendingToolCalls.length) return;
        protocolInteractions.push({
          id: `browser-asst-${pendingToolCalls[0].id}`,
          role: 'assistant',
          content: '',
          tool_calls: pendingToolCalls,
          status: 'complete',
        });
        pendingToolCalls = [];
      }

      /**
       * Process one SSE event matching the real agent loop's vocabulary.
       */
      function _handleEvent(event) {
        const t = event.type;
        if (t === 'session_run') {
          lifeCycleEvents.push(event);
          if (event.status === 'started') {
            currentRunId = event.turn_id;
            defaultSessionDB.startRun(sessionId, {
              agent_id: _agentId,
              turn_id: event.turn_id,
            }).catch(() => {});
          } else if (event.status === 'complete' && currentRunId) {
            defaultSessionDB.finishRun(currentRunId, 'complete', {
              final_response: event.final_response || fullContent,
            }).catch(() => {});
          } else if (event.status === 'interrupted' && currentRunId) {
            defaultSessionDB.finishRun(currentRunId, 'interrupted').catch(() => {});
          } else if (event.status === 'error' && currentRunId) {
            defaultSessionDB.finishRun(currentRunId, 'error', {
              error_message: event.error,
            }).catch(() => {});
          }
        } else if (t === 'stream') {
          fullContent += event.content || '';
          opts.onStream?.(event.content || '');
        } else if (t === 'response') {
          fullContent = event.content || '';
          finalAssistantId = event.asst_id || finalAssistantId;
          opts.onResponse?.(fullContent);
        } else if (t === 'tool_call') {
          const toolName = event.tool || event.tool_name || '';
          pendingToolCalls.push({
            id: event.tool_call_id || randomUUID(),
            type: 'function',
            function: {
              name: toolName,
              arguments: JSON.stringify(event.args || {}),
            },
          });
          opts.onStream?.('\uD83D\uDD27 ' + toolName + ' ');
        } else if (t === 'tool_result') {
          _flushPendingToolCalls();
          const toolName = event.tool || event.tool_name || '';
          const result = event.result;
          const resultStr = typeof result === 'string' ? result : JSON.stringify(result);
          protocolInteractions.push({
            id: `browser-tool-${event.tool_call_id || randomUUID()}`,
            role: 'tool',
            tool_name: toolName,
            tool_call_id: event.tool_call_id || null,
            content: resultStr,
            output: resultStr,
            status: event.error ? 'error' : 'complete',
          });
        } else if (t === 'session_title') {
          lifeCycleEvents.push(event);
          generatedTitle = event.title || null;
        } else if (t === 'history_ack') {
          historyAck = event;
        } else if (t === 'memory_saved') {
          lifeCycleEvents.push(event);
          if (event.memory) {
            defaultSessionDB.upsertMemory(event.memory).catch(() => {});
          }
        } else if (t === 'error') {
          opts.onError?.(event.message || 'Unknown error');
        } else if (t === 'interrupted') {
          opts.onError?.(event.message || 'Turn interrupted');
        }
      }

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (!line.startsWith('data: ')) continue;
          const data = line.slice(6).trim();
          if (!data) continue;

          try {
            _handleEvent(JSON.parse(data));
          } catch (parseError) {
            console.warn('[BrowserRouter] SSE parse error:', parseError, line);
          }
        }
      }

      // Flush final buffered line
      if (buffer.startsWith('data: ')) {
        const data = buffer.slice(6).trim();
        if (data) {
          try {
            _handleEvent(JSON.parse(data));
          } catch (e) { /* ignore malformed final line */ }
        }
      }

      // 6. Save all interactions to IndexedDB
      _flushPendingToolCalls();
      const allNewInteractions = protocolInteractions;
      if (fullContent) {
        allNewInteractions.push({
          id: finalAssistantId || undefined,
          role: 'assistant',
          content: fullContent,
          status: 'complete',
        });
      }
      if (allNewInteractions.length > 0) {
        await defaultSessionDB.addInteractions(sessionId, allNewInteractions);
        const acceptedAck =
          historyAck && Number(historyAck.revision || 0) >= historyRevision
            ? historyAck
            : null;
        await defaultSessionDB.updateSession(sessionId, {
          updated_at: new Date().toISOString(),
          authority_revision: acceptedAck?.revision ?? historyRevision,
          history_token: acceptedAck?.token || null,
          content_hash: acceptedAck?.content_hash || session?.content_hash || '',
          ...(generatedTitle ? { title: generatedTitle } : {}),
        });
      }

      // 7. Mark session dirty for background sync
      syncEngine.markDirty(sessionId).catch(() => {});

      return fullContent;
    } catch (e) {
      if (!opts.onError) throw e;
      opts.onError(e.message);
      return '';
    }
  }

  // ── Agent config ────────────────────────────────────────────────────

  /**
   * Refresh the cached agent config (e.g. after admin edits).
   * @returns {Promise<Object>}
   */
  async refreshConfig() {
    if (_mode !== MODE_BROWSER) throw new Error('Not in browser mode');
    return _fetchAndCacheAgentConfig(_agentId);
  }

  /**
   * Get the cached agent config from IndexedDB.
   * @returns {Promise<Object|null>}
   */
  async getCachedConfig() {
    if (_mode !== MODE_BROWSER) return null;
    return defaultSessionDB.getCachedAgentConfig(_agentId);
  }

  // ── Lifecycle: interrupt & resume ────────────────────────────────────

  /**
   * Request interruption of an in-progress browser chat session.
   * Calls POST /api/v1/browser/interrupt/{sessionId} to set the server-side
   * asyncio.Event that the agent loop polls.
   *
   * @param {string} sessionId — the server-assigned browser session id
   * @returns {Promise<{ok: boolean, message: string}>}
   */
  async interrupt(sessionId) {
    try {
      const resp = await fetch(apiPath(`/api/v1/browser/interrupt/${sessionId}`), {
        method: 'POST',
        headers: authHeaders(),
      });
      const body = await resp.json().catch(() => ({}));
      return { ok: resp.ok && body.status === 'ok', message: body.message || '' };
    } catch (e) {
      return { ok: false, message: e.message };
    }
  }

  /**
   * Check whether a session has an active (running) turn in IndexedDB.
   * Called on page load to decide whether to render a partial transcript
   * or show a "resuming..." indicator.
   *
   * @param {string} sessionId
   * @returns {Promise<Object|null>} the active run row, or null
   */
  async getActiveRun(sessionId) {
    return defaultSessionDB.getActiveRun(sessionId);
  }

  /**
   * Return the stored partial events from an active run so the UI can
   * replay already-received stream chunks and tool results.
   *
   * @param {string} runId
   * @returns {Promise<Array>}
   */
  async getRunEvents(runId) {
    // Read directly from the session_runs store via the indexeddb instance
    const runs = await defaultSessionDB.listRuns(null);
    // listRuns filters by session; we need a different path. Just delegate.
    const db = await defaultSessionDB.ready();
    return new Promise((resolve, reject) => {
      const tx = db.transaction('session_runs', 'readonly');
      const store = tx.objectStore('session_runs');
      const req = store.get(runId);
      req.onsuccess = () => resolve(req.result ? (req.result.partial_events || []) : []);
      req.onerror = () => reject(req.error);
    });
  }

  // ── Gen UI pages (IndexedDB) ───────────────────────────────────────

  /**
   * List genui pages from local storage.
   * @returns {Promise<Array>}
   */
  async listGenuis() {
    return defaultSessionDB.listGenuis();
  }

  /**
   * Get a genui page by slug.
   * @param {string} slug
   * @returns {Promise<Object|null>}
   */
  async getGenui(slug) {
    return defaultSessionDB.getGenui(slug);
  }

  /**
   * Save a genui page's metadata and HTML locally.
   * @param {Object} page — { slug, title, description, html, ... }
   */
  async saveGenui(page) {
    const { html, ...meta } = page;
    await defaultSessionDB.saveGenui(meta);
    if (html) {
      await defaultSessionDB.saveGenuiHtml(page.slug, html);
    }
  }

  /**
   * Get a genui page's HTML.
   * @param {string} slug
   * @returns {Promise<string|null>}
   */
  async getGenuiHtml(slug) {
    return defaultSessionDB.getGenuiHtml(slug);
  }

  /**
   * Delete a genui page.
   * @param {string} slug
   */
  async deleteGenui(slug) {
    return defaultSessionDB.deleteGenui(slug);
  }

  // ── User files (IndexedDB) ─────────────────────────────────────────

  /**
   * List saved user files.
   * @param {string} [room]
   * @returns {Promise<Array>}
   */
  async listUserFiles(room) {
    return defaultSessionDB.listUserFiles(room);
  }

  /**
   * Get a saved user file.
   * @param {string} path
   * @returns {Promise<Object|null>}
   */
  async getUserFile(path) {
    return defaultSessionDB.getUserFile(path);
  }

  /**
   * Save a user file locally.
   * @param {Object} file — { path, room, filename, content, ... }
   * @returns {Promise<Object>}
   */
  async saveUserFile(file) {
    return defaultSessionDB.saveUserFile(file);
  }

  /**
   * Delete a user file.
   * @param {string} path
   */
  async deleteUserFile(path) {
    return defaultSessionDB.deleteUserFile(path);
  }

  // ── Maintenance ────────────────────────────────────────────────────

  /**
   * Clear all IndexedDB data (sessions, interactions, configs).
   */
  async clearAll() {
    return defaultSessionDB.clearAll();
  }

  /**
   * Get storage usage statistics.
   * @returns {Promise<{sessions: number, interactions: number, agent_config: number}>}
   */
  async stats() {
    return defaultSessionDB.stats();
  }

  /**
   * Get estimated size in bytes.
   * @returns {Promise<number>}
   */
  async estimateSizeBytes() {
    return defaultSessionDB.estimateSizeBytes();
  }
}

/**
 * Singleton instance — use this throughout the app.
 */
const browserRouter = new BrowserRouter();
export default browserRouter;

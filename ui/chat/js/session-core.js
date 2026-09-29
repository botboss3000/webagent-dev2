'use strict';

// Session lifecycle actions — switch / delete / pin / inline-rename a session
// (teardown, loading spinner, load chat, drain replayed events, WS resume).
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { advanceDeleteBtn, resetDeleteBtn } from '../../shared/js/delete-control.js';
import { notifySessionsChanged } from '../../shared/js/session-events.js';
import { chatActivitySessionChanged } from '../../shared/js/chat-activity.js';
import { consumeReplayedEventsFor } from '../../shared/js/agentWs.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble } from './chat-bubble.js';
import { _messageCache, _cacheAppendMessage, _captureSessionFocus } from "./chat-message-cache.js";
import { _teardownVirtualScroll, _installVirtualScroll } from './chat-virtual-scroll.js';
import { loadSessionChat } from './session-load.js?v=509';
import { _sessionsCache, populateSessionSelect, _renderSessionRows, _setTriggerLabel, _clearSessionHeader, _fetchRelatedSessions } from './session-list.js';
import { _refreshAgentAbilities, _agentsCache, _lastSessionPerAgent, _saveLastSessionMap, _agentIconFor, _renderAgentRows, _setAgentTriggerLabel } from './session-agent.js';
import { storageAdapter } from './storage/storage-adapter.js';
import { replaceRouteParams } from '../../shared/js/url-state.js';

// ── Interrupt ──────────────────────────────────────────────────────────────

function interruptSession(sessionId) {
  if (!sessionId) return;
  if (storageAdapter.isBrowser) return; // no server-side run
  fetch(apiPath('/api/v1/chat/interrupt'), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...authHeaders() },
    body: JSON.stringify({ session_id: sessionId }),
  }).catch(() => { /* best-effort */ });
}

// ── Switch session ─────────────────────────────────────────────────────────

// The phantom-skeleton shown in the chat area while a session's messages are
// fetched: a compact, deterministic set of agent / user / agent bubbles that
// mirrors a short conversation (agent greeting → user turn → agent reply).
// Only the per-bubble line widths are randomized; the composition is fixed and
// deliberately short. Shared with session-swipe.js so the swipe handoff can
// pre-render the same markup behind the arrow+text panel (single source of truth).
const _skeletonRand = (min, max) => Math.floor(Math.random() * (max - min + 1)) + min;
const _skeletonLine = (w) => '<span class="chat-skeleton-line" style="width:' + w + '%"></span>';

// One phantom bubble; the last line is always shorter (classic skeleton look).
function _skeletonBubble(role) {
  const n = _skeletonRand(2, 4);
  let out = '<div class="chat-skeleton-bubble ' + role + '">';
  for (let i = 0; i < n - 1; i++) out += _skeletonLine(_skeletonRand(55, 98));
  out += _skeletonLine(_skeletonRand(35, 55));
  return out + '</div>';
}

export function loadingSkeletonMarkup() {
  // A compact, deterministic phantom conversation: agent greeting → user turn →
  // agent reply. Reduced to a few skeleton bubbles (no randomized extras and no
  // tool cards) so the loading state reads as a clean, short placeholder.
  let html = '<div class="chat-loading-wrap chat-skeleton" role="status" aria-label="Loading conversation">';
  html += _skeletonBubble('agent');
  html += _skeletonBubble('user');
  html += _skeletonBubble('agent');
  html += '</div>';
  return html;
}

let _sessionSwitchEpoch = 0;
const _sessionProjectionCache = new Map();
const _SESSION_PROJECTION_CACHE_MAX = 6;

function _stashSessionProjection(sessionId) {
  if (!sessionId || !app.chatMessages || !app.chatMessages.childElementCount) return;
  if (app.chatMessages.querySelector('.chat-loading-wrap')) return;
  const messageCache = _messageCache.get(sessionId);
  if (!messageCache) return;
  const fragment = document.createDocumentFragment();
  while (app.chatMessages.firstChild) {
    fragment.appendChild(app.chatMessages.firstChild);
  }
  _sessionProjectionCache.delete(sessionId);
  _sessionProjectionCache.set(sessionId, {
    fragment,
    loadedAt: messageCache.loadedAt || 0,
  });
  while (_sessionProjectionCache.size > _SESSION_PROJECTION_CACHE_MAX) {
    _sessionProjectionCache.delete(_sessionProjectionCache.keys().next().value);
  }
}

function _takeSessionProjection(sessionId) {
  const entry = _sessionProjectionCache.get(sessionId);
  if (!entry) return null;
  _sessionProjectionCache.delete(sessionId);
  const messageCache = _messageCache.get(sessionId);
  if (!messageCache || (messageCache.loadedAt || 0) !== entry.loadedAt) return null;
  return entry.fragment;
}

if (typeof document !== 'undefined') {
  document.addEventListener('webagent-auth-transition-start', () => {
    _sessionProjectionCache.clear();
  });
}

async function switchToSession(sid,{agentId=null}={}) {
  if(!sid || sid===app.currentSessionId) return;
  const generation=++_sessionSwitchEpoch;
  const panel=document.getElementById('chat-panel');
  panel?.classList.add('session-transitioning');
  app.abortChatStream?.();
  _teardownVirtualScroll();
  app._clearLanding?.();
  _clearSessionHeader();
  const target=_sessionsCache.find(row=>row.id===sid);
  const targetAgent=agentId || target?.agent_id || app.currentAgentId;
  app.isNewSession=false;
  app.currentSessionId=sid;
  localStorage.setItem('terminalSessionId',sid);
  replaceRouteParams({session:sid});
  if(targetAgent!==app.currentAgentId) {
    const automatic=app._reapplyChatControls;
    app._reapplyChatControls=null;
    try {app.currentAgentId=targetAgent;} finally {app._reapplyChatControls=automatic;}
    localStorage.setItem('selectedAgentId',targetAgent);
    _refreshAgentAbilities(targetAgent);
    const {reapplyChatControlsConfig}=await import('../../chat-controls/chat-controls-config.js');
    await reapplyChatControlsConfig();
  }
  if(generation!==_sessionSwitchEpoch) return;
  _lastSessionPerAgent.set(targetAgent,sid);_saveLastSessionMap();
  app.reloadExecutionMode?.();app.reloadTargetDevice?.();
  chatActivitySessionChanged();_setTriggerLabel();
  const reveal=()=>{
    if(generation!==_sessionSwitchEpoch || sid!==app.currentSessionId) return;
    panel?.classList.remove('session-transitioning');
    _renderSessionRows();_setTriggerLabel();
  };
  try {
    await loadSessionChat(sid,{onReady:reveal});
    reveal();
    if(generation===_sessionSwitchEpoch) window.dispatchEvent(new CustomEvent('webagent-loop-session-changed'));
  } catch(error) {
    if(generation===_sessionSwitchEpoch) {
      reveal();
      app.chatActivityStart?.('Conversation unavailable. Please retry.');
    }
  }
}

// ── Delete session ─────────────────────────────────────────────────────────
// Returns { ok: boolean, error?: string }. Retries up to `retries` times with
// exponential backoff so transient blips don't silently lose the button state.
// The caller (session-init.js) shows the error in the session title on failure.

async function deleteSession(sid, { retries = 1 } = {}) {
  if (window.__webagentOfflineReadOnly === true) {
    return { ok: false, error: 'Offline · cached data is read-only' };
  }
  if (typeof sid === 'string' && sid.startsWith('codex:')) {
    try {
      const qs = new URLSearchParams({ user_id: app.currentUserId || '', agent_id: app.currentAgentId || '' });
      const res = await fetch(apiPath(`/api/v1/engines/codex/portal/links/${encodeURIComponent(sid)}?${qs}`), { method: 'DELETE', headers: authHeaders() });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) return { ok: false, error: data.detail || `Server error (${res.status})` };
      await populateSessionSelect(app.currentUserId);
      const stub = _sessionsCache.findIndex(s => s.id === sid && s.created_at === null);
      if (stub !== -1) _sessionsCache.splice(stub, 1);
      if (sid === app.currentSessionId) {
        const others = _sessionsCache.filter(s => s.id !== sid);
        if (others.length) await switchToSession(others[0].id);
        else {
          app.isNewSession = true;
          app.currentSessionId = randomUUID();
          localStorage.setItem('terminalSessionId', app.currentSessionId);
  replaceRouteParams({ session: app.currentSessionId });
          _clearSessionHeader();
          await loadSessionChat(app.currentSessionId);
        }
      }
      return { ok: true };
    } catch (error) {
      return { ok: false, error: error.message || 'Network error' };
    }
  }
  interruptSession(sid);
  if (storageAdapter.isBrowser) {
    const result = await storageAdapter.deleteSession(sid);
    if (!result.ok) return result;
    notifySessionsChanged({ action: 'delete', ids: [sid] });
    await populateSessionSelect(app.currentUserId);
    const _stubIdx = _sessionsCache.findIndex(s => s.id === sid && s.created_at === null);
    if (_stubIdx !== -1) _sessionsCache.splice(_stubIdx, 1);
    if (sid === app.currentSessionId) {
      const others = _sessionsCache.filter(s => s.id !== sid);
      if (others.length > 0) {
        await switchToSession(others[0].id);
      } else {
        app.isNewSession = true;
        app.currentSessionId = randomUUID();
        localStorage.setItem('terminalSessionId', app.currentSessionId);
  replaceRouteParams({ session: app.currentSessionId });
        // Fresh session — blank the header the same way a switch does.
        _clearSessionHeader();
        if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
        if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
        await loadSessionChat(app.currentSessionId);
      }
    }
    return { ok: true };
  }
  let lastErr = '';
  for (let attempt = 0; attempt <= retries; attempt++) {
    try {
      const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), { method: 'DELETE', headers: { ...authHeaders() } });
      if (res.ok) {
        // Tell the admin Sessions table (and any other listener) to refresh so a
        // session deleted here also disappears there.
        notifySessionsChanged({ action: 'delete', ids: [sid] });
        // Re-fetch the session list and VERIFY the session is actually gone.
        // The API can return 200 even when the delete didn't take effect (e.g.
        // phantom session with no `sessions` row, or a hybrid sync that silently
        // fails). If the session is still in the cache after refresh, the delete
        // did NOT persist — return an error so the caller shows it in the UI.
        await populateSessionSelect(app.currentUserId);
        // poplateSessionSelect stubs the currentSessionId if it's missing from
        // the server list (line ~580 of session-list.js). When deleting the
        // current session that stub has id === sid, which would make the
        // verification below think the delete failed. Remove it so the
        // switching logic runs and the next session takes over.
        const _stubIdx = _sessionsCache.findIndex(s => s.id === sid && s.created_at === null);
        if (_stubIdx !== -1) _sessionsCache.splice(_stubIdx, 1);
        const stillPresent = _sessionsCache.some(s => s.id === sid);
        if (stillPresent) {
          lastErr = 'Session still exists after delete';
          // Fall through to retry logic below (or return error after exhausting).
          // This acts as an extra "attempt" using verification rather than a
          // raw HTTP call — avoids an infinite retry loop on persistent failures.
          if (attempt < retries) {
            await new Promise(r => setTimeout(r, 400 * (attempt + 1)));
            continue;
          }
          console.warn('Failed to delete session (session still present after refresh):', sid);
          return { ok: false, error: lastErr };
        }
        if (sid === app.currentSessionId) {
          const others = _sessionsCache.filter(s => s.id !== sid);
          if (others.length > 0) {
            await switchToSession(others[0].id);
          } else {
            app.isNewSession = true;
            app.currentSessionId = randomUUID();
            localStorage.setItem('terminalSessionId', app.currentSessionId);
  replaceRouteParams({ session: app.currentSessionId });
            // Fresh session → reset the execution-mode pill (per-session key, defaults
            // to Ask) so it doesn't inherit the deleted session's leftover mode.
            if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
            if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
            await loadSessionChat(app.currentSessionId);
          }
        }
        return { ok: true };
      }
      lastErr = `Server error (${res.status})`;
      const body = await res.text().catch(() => '');
      if (body) lastErr = body.substring(0, 120);
    } catch (e) {
      lastErr = e.message || 'Network error';
    }
    if (attempt < retries) await new Promise(r => setTimeout(r, 400 * (attempt + 1)));
  }
  console.warn('Failed to delete session after retries:', lastErr);
  return { ok: false, error: lastErr };
}

// ── Patch session ──────────────────────────────────────────────────────────

async function patchSession(sid, body) {
  if (window.__webagentOfflineReadOnly === true) return false;
  if (typeof sid === 'string' && sid.startsWith('codex:')) {
    // Link-level display state belongs to the plugin sidecar. Native Codex owns
    // the task title; WebAgent must never manufacture a sessions row for it.
    if (Object.prototype.hasOwnProperty.call(body || {}, 'title')) return false;
    try {
      const res = await fetch(apiPath(`/api/v1/engines/codex/portal/links/${encodeURIComponent(sid)}`), {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ user_id: app.currentUserId, agent_id: app.currentAgentId, ...body }),
      });
      if (res.ok) await populateSessionSelect(app.currentUserId);
      return res.ok;
    } catch (_) { return false; }
  }
  if (storageAdapter.isBrowser) {
    await storageAdapter.patchSession(sid, body);
    notifySessionsChanged({ action: 'patch', ids: [sid] });
    return true;
  }
  try {
    const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(body),
    });
    if (res.ok) {
      // Hybrid mode serves the session list from the IndexedDB mirror, so
      // sync the cache entry NOW — otherwise the next populateSessionSelect
      // paints the stale pre-patch row and the change (pin/hide/title) only
      // shows up after the dropdown is closed and reopened.
      if (storageAdapter.isHybrid) {
        await storageAdapter.updateSessionCache(sid, body);
      }
      notifySessionsChanged({ action: 'patch', ids: [sid] });
    }
    return res.ok;
  } catch (e) {
    console.warn('Failed to patch session:', e);
    return false;
  }
}

// ── Auto rename (Session Namer app function) ────────────────────────────────
// Calls POST /sessions/{id}/auto-title so the background Session Namer re-titles
// the session on demand — forced past any lock or special prefix (optimizer-/
// closer-/slash-), sampling more of the conversation than the 3-turn background
// hook. The server returns the new name and emits session_title WS events so the
// header spinner + live rename work exactly like a real turn. In browser-storage
// mode there is no server: fall back to a client-side title from the first user
// message (same rule as the server's fallback naming).

async function autoRenameSession(sid) {
  if (window.__webagentOfflineReadOnly === true) {
    return { ok: false, error: 'Offline · cached data is read-only' };
  }
  if (!sid) return { ok: false, error: 'No session' };
  console.log('[auto-rename] autoRenameSession called for', sid);
  if (storageAdapter.isBrowser) {
    let title = '';
    try {
      const msgs = await storageAdapter.getInteractions(sid, 6, {});
      const firstUser = (msgs || []).find(
        m => m && m.role === 'user' && typeof m.content === 'string' && m.content.trim()
      );
      if (firstUser && firstUser.content) {
        title = firstUser.content.trim().split(/\s+/).slice(0, 6)
          .join(' ').replace(/[.,!?;:]+$/, '').slice(0, 60);
      }
    } catch (_) { /* fall through */ }
    if (!title) return { ok: false, error: 'No messages to name from' };
    const ok = await patchSession(sid, { title });
    if (ok) {
      const sess = _sessionsCache.find(s => s.id === sid);
      if (sess) sess.title = title;
      await populateSessionSelect(app.currentUserId);
    }
    return { ok: !!ok, title };
  }
  try {
    const res = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '/auto-title?db=user.db'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
    });
    if (!res.ok) {
      let detail = 'Auto rename failed';
      try { const j = await res.json(); if (j && j.detail) detail = String(j.detail); } catch (_) {}
      console.warn('[auto-rename] server returned', res.status, detail);
      return { ok: false, error: detail };
    }
    const data = await res.json();
    console.log('[auto-rename] server response:', data);
    if (data && data.status === 'ok' && data.title) {
      const sess = _sessionsCache.find(s => s.id === sid);
      if (sess) sess.title = data.title;
      notifySessionsChanged({ action: 'patch', ids: [sid] });
      await populateSessionSelect(app.currentUserId);
    }
    return { ok: !!(data && data.status === 'ok'), title: data && data.title, status: data && data.status, message: data && data.message };
  } catch (e) {
    console.warn('Auto rename failed:', e);
    return { ok: false, error: e.message || 'Network error' };
  }
}

// ── Toggle pin ─────────────────────────────────────────────────────────────

async function togglePin(sid) {
  if (window.__webagentOfflineReadOnly === true) return;
  const sess = _sessionsCache.find(s => s.id === sid);
  if (!sess) return;
  const newPinned = !sess.pinned;
  const ok = await patchSession(sid, { pinned: newPinned });
  if (ok) {
    sess.pinned = newPinned;
    // Match the server's append-on-pin default until its authoritative refresh
    // arrives. A user can then drag any pinned row to its saved final position.
    sess.sort_order = newPinned
      ? _sessionsCache.reduce((max, row) => (
        row.pinned && Number.isFinite(row.sort_order) ? Math.max(max, row.sort_order) : max
      ), -1) + 1
      : null;
    if (storageAdapter.isHybrid) {
      await storageAdapter.updateSessionCache(sid, {
        pinned: newPinned,
        sort_order: sess.sort_order,
      });
    }
    await populateSessionSelect(app.currentUserId);
  }
}

// ── Toggle hidden ──────────────────────────────────────────────────────────

async function toggleHidden(sid) {
  if (window.__webagentOfflineReadOnly === true) return;
  const sess = _sessionsCache.find(s => s.id === sid);
  if (!sess) return;
  const newHidden = !sess.hidden;
  const ok = await patchSession(sid, { hidden: newHidden });
  if (ok) {
    sess.hidden = newHidden;
    await populateSessionSelect(app.currentUserId);
  }
}

// ── Rename (dropdown row and header) ───────────────────────────────────────

function startRename(sid, row) {
  if (window.__webagentOfflineReadOnly === true) return;
  const titleEl = row.querySelector('.session-row-title');
  if (!titleEl) return;
  const sess = _sessionsCache.find(s => s.id === sid);
  const current = (sess && sess.title) || '';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-row-title-input';
  input.value = current;
  titleEl.replaceWith(input);
  input.focus();
  input.select();

  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    // Detach the input BEFORE the re-render: _renderSessionRows() skips its
    // rebuild while a rename input is live, so the commit must clear it or
    // the fresh title never paints.
    input.remove();
    const newTitle = input.value.trim();
    if (commit && newTitle && newTitle !== current) {
      const ok = await patchSession(sid, { title: newTitle });
      if (ok && sess) sess.title = newTitle;
    }
    await populateSessionSelect(app.currentUserId);
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  const _docHandler = (e) => {
    if (input && !input.contains(e.target)) {
      document.removeEventListener('pointerdown', _docHandler, true);
      input.blur();
    }
  };
  document.addEventListener('pointerdown', _docHandler, true);
}

function _headerRenameSession() {
  if (window.__webagentOfflineReadOnly === true) return;
  const labelEl = document.getElementById('session-dropdown-label');
  if (!labelEl) return;
  const sid = app.currentSessionId;
  if (!sid) return;
  const sess = _sessionsCache.find(s => s.id === sid);
  const current = (sess && sess.title) || 'New Session';
  const input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-row-title-input';
  input.value = current;
  input.style.width = '140px';
  labelEl.replaceWith(input);
  input.style.pointerEvents = 'auto';
  input.focus();
  input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    done = true;
    // Restore the label span IN THE DOM before any await. Simply removing the
    // input leaves the trigger with NO label element, and _setTriggerLabel's
    // recovery only kicks in when a live input is still a child of the
    // trigger — after removal it finds neither and bails, so the session name
    // stays blank until a refresh (the "name blanks out after rename" bug).
    // The input keeps its value once detached, so read it after the swap.
    const labelEl = document.createElement('span');
    labelEl.id = 'session-dropdown-label';
    labelEl.className = 'session-row-title';
    input.replaceWith(labelEl);
    const newTitle = input.value.trim();
    if (commit && newTitle && newTitle !== current) {
      const ok = await patchSession(sid, { title: newTitle });
      if (ok && sess) sess.title = newTitle;
    }
    _setTriggerLabel();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    else if (e.key === 'Escape') { e.preventDefault(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
  const _docHandler = (e) => {
    if (input && !input.contains(e.target)) {
      document.removeEventListener('pointerdown', _docHandler, true);
      input.blur();
    }
  };
  document.addEventListener('pointerdown', _docHandler, true);
}

export {
  switchToSession,
  interruptSession,
  patchSession,
  deleteSession,
  togglePin,
  toggleHidden,
  startRename,
  _headerRenameSession,
  autoRenameSession,
};

// Expose the cache-aware session switch to the floating chat launcher (the
// mobile "not on chat panel" entry). The widget calls this hook INSTEAD of its
// own raw /api/v1/db/sessions/{id} fetch, so switching uses the hybrid
// IndexedDB cache — zero server round trips when the transcript is resident.
if (typeof window !== 'undefined') {
  window.__switchToSession = (sid) => switchToSession(sid);
}

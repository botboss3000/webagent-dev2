'use strict';

// Session working-tree data feed. The server records paths as agent tools
// change them, so browser refreshes and other chat sessions cannot blur the
// ownership boundary.
//
// This module owns the single fetch + broadcast loop for the chat's working
// tree: it polls for session changes (resilient across session switches),
// broadcasts every result as a 'chat-changes:updated' CustomEvent, and exposes
// app.refreshSessionChanges() for on-demand refreshes. It renders nothing.
//
// Consumers:
//   - the 'changes' chip (ui/chat/elements/changes.js) — count badge + toggle,
//   - the 'local_changes' panel (ui/chat/elements/local-changes/index.js) —
//     the new-design working-tree panel (file list + commit), shown above the
//     pill when the chip is clicked.
// The legacy session-changes drawer this feed used to render (#chat-changes-
// panel in ui/chat/chat-side-panel.html, wired in the old chat-session-
// changes.js) is gone from the app.

import { app } from '../../shared/js/state.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _sessionsCache } from './session-list.js';

let _lastSessionId = '';
let _lastWasStub = false;
let _current = [];

// A session the frontend mints client-side (New Session, agent switch, or
// deleting the last session) has no `sessions` row until the first message is
// sent — session-list.js marks those cache entries with created_at === null.
// Querying the API for one would 403 ("session not found or not owned"), so a
// session that isn't a real server row is a fresh session with no changes yet.
function _isStubSession(sid) {
  const cached = _sessionsCache.find((s) => s.id === sid);
  return !cached || cached.created_at === null;
}

async function _request(path, options = {}) {
  const res = await fetch(path, { ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || body.message || `Request failed (${res.status})`);
  return body;
}

function _broadcast() {
  document.dispatchEvent(new CustomEvent('chat-changes:updated', { detail: { files: _current } }));
}

export async function refreshSessionChanges() {
  const sid = app.currentSessionId;
  if (!sid) {
    _current = [];
    _lastSessionId = '';
    _lastWasStub = false;
    _broadcast();
    return;
  }
  if (_isStubSession(sid)) {
    // Fresh client-side session, no DB row yet — nothing to show or claim.
    _lastSessionId = sid;
    _lastWasStub = true;
    _current = [];
    _broadcast();
    return;
  }
  try {
    const data = await _request(`/api/v1/github/chat-changes?session_id=${encodeURIComponent(sid)}`);
    _current = data.files || [];
    _broadcast();
  } catch (_) {
    // Keep the last known list; the 1s poll below stops re-firing the same
    // failing request every second (tracked via _lastSessionId).
  } finally {
    _lastSessionId = sid;
    _lastWasStub = false;
  }
}

export async function initChatChanges() {
  // Admin gate — the whole feature is admin-only (same check the panel's
  // commit button uses).
  try {
    const access = await _request('/api/v1/github/check-access');
    if (!access.is_admin) return;
  } catch (_) { return; }
  app.refreshSessionChanges = refreshSessionChanges;
  // Session switches are spread across a few modules; polling the id is a
  // resilient single integration point and has no network cost while unchanged.
  // Also refresh when a stub session becomes real (first message sent → its DB
  // row appears) so the panel picks up the session's changes without a manual
  // refresh.
  setInterval(() => {
    if (!app.currentSessionId) return;
    const wasStub = _lastWasStub;
    const isStub = _isStubSession(app.currentSessionId);
    if (app.currentSessionId !== _lastSessionId || (wasStub && !isStub)) refreshSessionChanges();
  }, 1000);
  refreshSessionChanges();
}

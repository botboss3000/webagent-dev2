'use strict';

// Session-scoped working-tree panel. The server records paths as agent tools
// change them, so browser refreshes and other chat sessions cannot blur the
// ownership boundary.

import { app } from '../../shared/js/state.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _sessionsCache } from './session-list.js';

let _lastSessionId = '';
let _lastWasStub = false;
let _visible = false;
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

function _setResult(text, isError = false) {
  const el = document.getElementById('chat-changes-result');
  if (!el) return;
  el.textContent = text || '';
  el.classList.toggle('error', !!isError);
}

function _render() {
  const list = document.getElementById('chat-changes-list');
  const counts = document.querySelectorAll('.chat-changes-count');
  const undo = document.getElementById('chat-changes-undo');
  const commit = document.getElementById('chat-changes-commit');
  if (!list) return;
  list.replaceChildren();
  if (!_current.length) {
    const empty = document.createElement('span'); empty.className = 'chat-changes-empty'; empty.textContent = 'No changes in this session.'; list.append(empty);
  } else {
    for (const file of _current) {
      const row = document.createElement('div'); row.className = 'chat-change-row';
      const path = document.createElement('span'); path.className = 'chat-change-path'; path.textContent = file.path;
      const stat = document.createElement('span'); stat.className = 'chat-change-stat';
      if (file.conflict) {
        stat.textContent = 'shared with another session';
      } else if (!file.added && !file.removed) {
        stat.textContent = 'changed';
      } else {
        const add = document.createElement('span'); add.className = 'chat-change-stat-add';
        add.textContent = file.added ? `+${file.added}` : '';
        const del = document.createElement('span'); del.className = 'chat-change-stat-del';
        del.textContent = file.removed ? `-${file.removed}` : '';
        stat.append(add);
        if (file.added && file.removed) stat.append(' ');
        stat.append(del);
      }
      row.classList.toggle('conflict', !!file.conflict);
      row.append(path, stat); list.append(row);
    }
  }
  const actionable = _current.filter((file) => !file.conflict);
  for (const badge of counts) { badge.textContent = String(_current.length); badge.hidden = !_current.length; }
  if (undo) undo.disabled = !actionable.length;
  if (commit) commit.disabled = !actionable.length;
  // Broadcast so above-pill mirrors (ui/chat/elements/local-changes/) stay in
  // lockstep with the panel from this single fetch.
  document.dispatchEvent(new CustomEvent('chat-changes:updated', { detail: { files: _current } }));
}

export async function refreshSessionChanges() {
  const sid = app.currentSessionId;
  if (!sid) {
    _current = [];
    _lastSessionId = '';
    _lastWasStub = false;
    _render();
    return;
  }
  if (_isStubSession(sid)) {
    // Fresh client-side session, no DB row yet — nothing to show or claim.
    _setResult('');
    _lastSessionId = sid;
    _lastWasStub = true;
    _current = [];
    _render();
    return;
  }
  try {
    const data = await _request(`/api/v1/github/chat-changes?session_id=${encodeURIComponent(sid)}`);
    _current = data.files || [];
    _render();
  } catch (err) {
    _setResult(err.message || 'Could not load changes.', true);
  } finally {
    // Track the id on every attempt (success or failure) so the 1s poll below
    // doesn't re-fire the same failing request every second.
    _lastSessionId = sid;
    _lastWasStub = false;
  }
}

export async function initSessionChanges() {
  const panel = document.getElementById('chat-changes-panel');
  const refresh = document.getElementById('chat-changes-refresh');
  const undo = document.getElementById('chat-changes-undo');
  const commit = document.getElementById('chat-changes-commit');
  const message = document.getElementById('chat-changes-message');
  if (!panel) return;
  try {
    const access = await _request('/api/v1/github/check-access');
    if (!access.is_admin) return;
  } catch (_) { return; }
  // The toggle is the dynamic 'changes' chat control (ui/chat/elements/
  // changes.js), placed via chat_ui.json → active_footer.below_pill rows.
  // It dispatches chat-control:toggle-changes; wire that to this panel.
  document.addEventListener('chat-control:toggle-changes', () => { _visible = !_visible; panel.hidden = !_visible; if (_visible) refreshSessionChanges(); });
  refresh?.addEventListener('click', () => refreshSessionChanges());
  undo?.addEventListener('click', async () => {
    const files = _current.filter((file) => !file.conflict);
    if (!files.length || !confirm(`Undo ${files.length} changed file(s) from this chat session?`)) return;
    undo.disabled = true;
    try { await _request('/api/v1/github/chat-changes/undo', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ session_id: app.currentSessionId, paths: files.map((f) => f.path) }) }); _setResult('Changes undone.'); await refreshSessionChanges(); }
    catch (err) { _setResult(err.message || 'Undo failed.', true); }
    finally { _render(); }
  });
  commit?.addEventListener('click', async () => {
    const files = _current.filter((file) => !file.conflict);
    if (!files.length) return;
    commit.disabled = true;
    try {
      const result = await _request('/api/v1/github/chat-changes/commit', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ session_id: app.currentSessionId, paths: files.map((f) => f.path), message: message?.value || '' }) });
      _setResult(result.status === 'committed' ? `Committed ${result.hash}.` : result.message || 'Nothing to commit.');
      if (result.status === 'committed' && message) message.value = '';
      await refreshSessionChanges();
    } catch (err) { _setResult(err.message || 'Commit failed.', true); }
    finally { _render(); }
  });
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

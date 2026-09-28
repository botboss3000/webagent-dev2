'use strict';

// Update / Selective-Sync — drop-in Admin Tools view module.
// Discovered via ui/admin-tools/update/page.json and driven generically by the
// admin shell (ui/shared/js/files.js applySidebarView), which dynamically
// imports this module and calls the exported startView / stopView when the view
// is shown / hidden. Talks to this folder's server.py (/api/v1/update/*).
// Related Git Control view module: ui/main-panel/explorer/explorer-view.js.
// REMOVE-WHEN: the Update view is dropped from the admin page catalog.
import { apiPath } from '../../shared/js/config.js';
import { app } from '../../shared/js/state.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _escAttr as _esc } from '../../shared/js/dom-utils.js';

function _qs(id) { return document.getElementById(id); }
function _jsonHeaders() { return Object.assign({ 'Content-Type': 'application/json' }, authHeaders()); }

const state = { wiredSidebar: false, wiredMain: false, status: null, lastApply: null };

async function _api(path, opts) {
  const res = await fetch(apiPath('/api/v1/update' + path), opts || { headers: authHeaders() });
  let body = null;
  try { body = await res.json(); } catch (_) {}
  if (!res.ok) { const msg = (body && body.detail) ? body.detail : ('HTTP ' + res.status); throw new Error(msg); }
  return body;
}

function _toast(text) { const el = _qs('upd-stats'); if (el) el.textContent = text; }

// ── Lifecycle (called by files.js applySidebarView) ──
export function startView() {
  _wireSidebar();
  _wireMain();
  refreshStatus().then(() => _renderMainFromStatus());
}
export function stopView() { /* no polling — nothing to tear down */ }

// ── Status / config ──
async function refreshStatus() {
  try { const s = await _api('/status'); state.status = s; _renderConfig(s); _renderFolders(s); }
  catch (e) { _renderError(e.message); }
}
function _renderConfig(s) {
  const url = _qs('upd-url'), branch = _qs('upd-branch'), hint = _qs('upd-mode-hint'), initBtn = _qs('upd-init');
  if (url && document.activeElement !== url) url.value = s.upstream_url || '';
  if (branch && document.activeElement !== branch) branch.value = s.upstream_branch || 'main';
  if (hint) {
    if (!s.is_git) { hint.textContent = 'Not a git repository — initialize git to enable selective updates.'; hint.className = 'upd-hint upd-hint-warn'; }
    else if (!s.has_upstream) { hint.textContent = 'Git repo detected. Save an upstream URL, then Fetch.'; hint.className = 'upd-hint'; }
    else { hint.textContent = 'Git mode — ' + (s.upstream_url || '') + (s.last_synced_commit ? (' · last synced ' + s.last_synced_commit.slice(0, 7)) : ''); hint.className = 'upd-hint'; }
  }
  if (initBtn) initBtn.hidden = !!s.is_git;
}
function _renderFolders(s) {
  const host = _qs('upd-folders'); if (!host) return;
  if (!s.is_git) { host.innerHTML = '<div class="upd-empty">Initialize git first to choose folders.</div>'; return; }
  const folders = s.folders || [];
  if (!folders.length) { host.innerHTML = '<div class="upd-empty">' + (s.has_upstream ? 'No syncable folders found.' : 'Save an upstream repo, then Fetch.') + '</div>'; return; }
  host.innerHTML = '';
  for (const f of folders) {
    const row = document.createElement('label');
    row.className = 'upd-folder-row' + (f.present_locally ? '' : ' upd-folder-absent');
    const changed = f.changed_files > 0 ? '<span class="upd-badge">' + f.changed_files + '</span>' : '';
    row.innerHTML =
      '<input type="checkbox" class="upd-folder-cb" data-path="' + _esc(f.path) + '"' + (f.tracked ? ' checked' : '') + ' />' +
      '<span class="upd-folder-name">' + _esc(f.path) + '</span>' +
      (f.present_locally ? '' : '<span class="upd-folder-tag">not local</span>') + changed;
    host.appendChild(row);
  }
}
function _renderError(msg) { const host = _qs('upd-folders'); if (host) host.innerHTML = '<div class="upd-empty upd-hint-warn">' + _esc(msg) + '</div>'; }
function _trackedFromUi() { return Array.from(document.querySelectorAll('.upd-folder-cb')).filter((cb) => cb.checked).map((cb) => cb.dataset.path); }

// ── Sidebar wiring ──
function _wireSidebar() {
  if (state.wiredSidebar) return; state.wiredSidebar = true;
  const save = _qs('upd-save-cfg');
  if (save) save.addEventListener('click', async () => {
    save.disabled = true;
    try {
      await _api('/config', { method: 'POST', headers: _jsonHeaders(), body: JSON.stringify({
        upstream_url: (_qs('upd-url') || {}).value || '', upstream_branch: (_qs('upd-branch') || {}).value || 'main', tracked_paths: _trackedFromUi(),
      })});
      _toast('Saved.'); await refreshStatus();
    } catch (e) { _toast('Save failed: ' + e.message); } finally { save.disabled = false; }
  });
  const init = _qs('upd-init');
  if (init) init.addEventListener('click', async () => {
    if (!confirm('Initialize a git repository here and add the upstream remote?')) return;
    init.disabled = true; _toast('Initializing…');
    try {
      await _api('/init', { method: 'POST', headers: _jsonHeaders(), body: JSON.stringify({
        upstream_url: (_qs('upd-url') || {}).value || '', upstream_branch: (_qs('upd-branch') || {}).value || 'main',
      })});
      _toast('Initialized.'); await refreshStatus();
    } catch (e) { _toast('Init failed: ' + e.message); } finally { init.disabled = false; }
  });
  const fetchBtn = _qs('upd-fetch');
  if (fetchBtn) fetchBtn.addEventListener('click', () => doFetch());
}

async function doFetch() {
  _toast('Fetching upstream…');
  try {
    const r = await _api('/fetch', { method: 'POST', headers: _jsonHeaders() });
    _toast('Upstream @ ' + (r.upstream_tip || '?') + ' · ' + (r.total_changed_files || 0) + ' file(s) differ.');
    await refreshStatus(); _renderMainFromStatus();
  } catch (e) { _toast('Fetch failed: ' + e.message); }
}

// ── Main area ──
function _wireMain() {
  if (state.wiredMain) return; state.wiredMain = true;
  const apply = _qs('upd-apply'); if (apply) apply.addEventListener('click', () => doApply());
  const body = _qs('upd-main-body'); if (body) body.addEventListener('click', _onMainClick);
}
function _renderMainFromStatus() {
  const body = _qs('upd-main-body'), apply = _qs('upd-apply'); if (!body) return;
  // #upd-main-body is the scrollable changeset column (overflow-y:auto); rebuilding
  // its innerHTML on a Fetch/Apply zeroes its scrollTop. Preserve the offset so a
  // refresh doesn't yank the list back to the top.
  const _prevTop = body.scrollTop;
  const s = state.status;
  const folders = (s && s.folders) ? s.folders.filter((f) => f.tracked && f.changed_files > 0) : [];
  if (!s || !s.has_upstream) { body.innerHTML = '<div class="upd-hint upd-main-hint">Configure an upstream repo and Fetch to see changes.</div>'; if (apply) apply.disabled = true; return; }
  if (!folders.length) { body.innerHTML = '<div class="upd-hint upd-main-hint">No changes in your tracked folders. You’re up to date.</div>'; if (apply) apply.disabled = true; return; }
  body.innerHTML = folders.map((f) =>
    '<div class="upd-card">' +
      '<label class="upd-card-head">' +
        '<input type="checkbox" class="upd-apply-cb" data-path="' + _esc(f.path) + '" checked />' +
        '<span class="upd-card-title">' + _esc(f.path) + '</span>' +
        '<span class="upd-badge">' + f.changed_files + ' changed</span>' +
        '<button class="upd-link upd-diff-btn" data-path="' + _esc(f.path) + '">view diff</button>' +
      '</label>' +
      '<pre class="upd-diff" data-path="' + _esc(f.path) + '" hidden></pre>' +
    '</div>'
  ).join('');
  if (apply) apply.disabled = false;
  if (_prevTop) body.scrollTop = _prevTop;   // restore scroll after the rebuild
}
async function _onMainClick(e) {
  const diffBtn = e.target.closest('.upd-diff-btn');
  if (diffBtn) {
    e.preventDefault();
    const path = diffBtn.dataset.path;
    const pre = document.querySelector('.upd-diff[data-path="' + CSS.escape(path) + '"]'); if (!pre) return;
    if (!pre.hidden) { pre.hidden = true; return; }
    pre.hidden = false; pre.textContent = 'Loading diff…';
    try { const r = await _api('/diff?path=' + encodeURIComponent(path)); pre.textContent = (r.diff || '(no diff)') + (r.truncated ? '\n… (truncated)' : ''); }
    catch (err) { pre.textContent = 'Diff failed: ' + err.message; }
    return;
  }
  const resolveBtn = e.target.closest('.upd-resolve-btn');
  if (resolveBtn) { e.preventDefault(); _handToAgent(); }
}
async function doApply() {
  const paths = Array.from(document.querySelectorAll('.upd-apply-cb')).filter((cb) => cb.checked).map((cb) => cb.dataset.path);
  if (!paths.length) { _toast('Select at least one folder.'); return; }
  if (!confirm('Apply upstream changes to ' + paths.length + ' folder(s)? Local files are backed up first.')) return;
  const apply = _qs('upd-apply'); if (apply) apply.disabled = true; _toast('Applying…');
  try {
    const r = await _api('/apply', { method: 'POST', headers: _jsonHeaders(), body: JSON.stringify({ paths, create_backup: true }) });
    state.lastApply = r; _renderApplyResult(r); await refreshStatus();
  } catch (e) { _toast('Apply failed: ' + e.message); if (apply) apply.disabled = false; }
}
function _renderApplyResult(r) {
  const body = _qs('upd-main-body'); if (!body) return;
  const res = r.results || {}; const n = (k) => (res[k] || []).length; const conflicts = r.conflict_files || [];
  const lines = [
    ['Merged cleanly', n('clean')], ['Added', n('added')], ['Binary replaced', n('binary')],
    ['Upstream-deleted (kept local)', n('deleted-upstream')], ['Conflicts', conflicts.length], ['Errors', n('error')],
  ].filter(([, c]) => c > 0).map(([l, c]) => '<li>' + _esc(l) + ': <strong>' + c + '</strong></li>').join('');
  let html = '<div class="upd-result"><div class="upd-result-title">Update applied' +
    (r.backup_dir ? ' · backup: <code>' + _esc(r.backup_dir) + '</code>' : '') + '</div>' +
    '<ul class="upd-result-list">' + (lines || '<li>No changes.</li>') + '</ul>';
  if (conflicts.length) {
    html += '<div class="upd-conflict-block">' +
      '<div class="upd-conflict-title">' + conflicts.length + ' file(s) have merge conflicts (markers written in place):</div>' +
      '<ul class="upd-conflict-list">' + conflicts.slice(0, 50).map((f) => '<li><code>' + _esc(f) + '</code></li>').join('') +
      (conflicts.length > 50 ? '<li>… and ' + (conflicts.length - 50) + ' more</li>' : '') + '</ul>' +
      '<button class="upd-btn upd-btn-primary upd-resolve-btn">Resolve with web agent</button>' +
      '<div class="upd-hint">Or edit them in File Manager / Git Control. Conflicts use standard ' +
        '<code>&lt;&lt;&lt;&lt;&lt;&lt;&lt;</code> markers.</div></div>';
  }
  if (r.backend_changed) {
    html += '<div class="upd-hint upd-hint-warn">Backend (.py) files changed — restart the server to load them.</div>';
  }
  html += '</div>'; body.innerHTML = html;
  _toast('Done — ' + n('clean') + ' merged, ' + conflicts.length + ' conflict(s).');
}
function _handToAgent() {
  const r = state.lastApply; const conflicts = (r && r.conflict_files) || []; if (!conflicts.length) return;
  const prompt =
    'I just applied a selective update from the upstream public repo and several files now ' +
    'have git merge conflict markers (<<<<<<< local / ======= / >>>>>>> upstream). Please ' +
    'resolve each conflict, preserving this fork’s local customizations where they ' +
    'intentionally differ while taking upstream’s improvements, then commit. Conflicted files:\n' +
    conflicts.map((f) => '- ' + f).join('\n');
  if (app && app.chatInput) {
    app.chatInput.value = prompt; app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    try { app.chatInput.focus(); } catch (_) {}
    _toast('Prompt loaded into chat — review and send to the web agent.');
  } else { alert('Send this to the web agent (Git Control ability must be enabled):\n\n' + prompt); }
}

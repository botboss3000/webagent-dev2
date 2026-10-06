'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

// Compact GitHub Git Control manager for the File Editor sidebar.
// Reuses the existing GitHub API endpoints; renders a VS Code-style
// commit graph drawn next to the commit list so users can see branches
// other than `main` (merges, side branches, unpushed work).
//
// Public:
//   renderGitPanel(rootEl)     — paint into the given container
//   refreshGit(rootEl)         — re-fetch + repaint
//   openGitPanel(rootEl)       — first activation hook (fetches status/log)
//   startGitAutoRefresh(rootEl)— begin live polling while the view is visible
//   stopGitAutoRefresh()       — stop the poll when leaving the view

import { apiPath, authHeaders, _esc, copyText } from './files-shims.js';

// ── DOM building blocks ────────────────────────────────────────────

const GRAPH_LANE_W = 14;   // pixels per graph lane (column width)
const GRAPH_ROW_H  = 38;   // pixels per commit row (must match CSS)
const GRAPH_DOT_R  = 4;    // commit dot radius

// Max lane index this commit row actually touches (dot, in/out/parent/merge
// lanes). Used to size the SVG column per-row so the text snaps as close to
// the tree as possible — VS-Code style, instead of reserving the global
// max-lane width for every single row.
function rowLaneSpan(c) {
  let max = c.lane || 0;
  const arrIn  = c.lanes_in  || [];
  const arrOut = c.lanes_out || [];
  for (let i = 0; i < arrIn.length;  i++) if (arrIn[i]  && i > max) max = i;
  for (let i = 0; i < arrOut.length; i++) if (arrOut[i] && i > max) max = i;
  if (c.parent_lanes)    for (const p of c.parent_lanes)    if (p > max) max = p;
  if (c.merge_in_lanes)  for (const m of c.merge_in_lanes)  if (m > max) max = m;
  return max + 1;
}

// Compact relative-time formatter: "now", "12m", "2h", "3d", "2w", "5mo",
// "1y". Falls back to whatever git's %ar string is if the ISO is missing
// or unparseable.
function shortRelativeTime(iso, fallback) {
  if (!iso) return fallback || '';
  const t = Date.parse(iso);
  if (isNaN(t)) return fallback || '';
  const sec = Math.max(0, (Date.now() - t) / 1000);
  if (sec < 45)              return 'now';
  if (sec < 60 * 60)         return Math.round(sec / 60) + 'm';
  if (sec < 60 * 60 * 24)    return Math.round(sec / 3600) + 'h';
  const day = sec / 86400;
  if (day < 7)               return Math.round(day) + 'd';
  if (day < 30)              return Math.round(day / 7) + 'w';
  if (day < 365)             return Math.round(day / 30.4375) + 'mo';
  return Math.round(day / 365.25) + 'y';
}

// VS-Code-ish lane colour palette (works on dark + light backgrounds)
const LANE_COLORS = [
  '#3b82f6', // blue (main / HEAD)
  '#f59e0b', // amber
  '#10b981', // emerald
  '#ec4899', // pink
  '#8b5cf6', // violet
  '#ef4444', // red
  '#06b6d4', // cyan
  '#eab308', // yellow
];

function laneColor(i) {
  return LANE_COLORS[((i % LANE_COLORS.length) + LANE_COLORS.length) % LANE_COLORS.length];
}

const REPO_KEY = 'v3:file-manager:repositories';
const ROOT_REPO = {id:'webagent', label:'WebAgent development', folder:'/live-checkout', builtin:true, active:true};
let lastGitStatus = null;
function repoState() {
  let entries;
  try { entries = JSON.parse(localStorage.getItem(REPO_KEY)); } catch {}
  if (!Array.isArray(entries)) entries = [ROOT_REPO];
  entries = entries.filter(row => row && typeof row.id === 'string' && typeof row.folder === 'string');
  if (!entries.some(row => row.id === ROOT_REPO.id)) entries.unshift({...ROOT_REPO});
  if (!entries.some(row => row.active)) entries[0].active = true;
  return entries;
}
function saveRepoState(entries) {
  localStorage.setItem(REPO_KEY, JSON.stringify(entries));
  return {repos: entries};
}
function selectedRepo() {
  const row = repoState().find(item => item.active) || ROOT_REPO;
  if (row.folder === '/live-checkout') return '__root__';
  if (!row.folder.startsWith('/live-checkout/')) throw Error('Repository outside development checkout');
  return row.folder.slice('/live-checkout/'.length);
}
async function v3Files(body) {
  const response = await fetch('/api/v3/file-manager', {
    method:'POST', credentials:'same-origin',
    headers:{'Content-Type':'application/json', ...authHeaders()},
    body:JSON.stringify(body)
  });
  const value = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(value.error || value.detail || 'Git Control request failed (' + response.status + ')');
  return value;
}
async function saveConnection(id, url, token, revoked=false) {
  const catalogResponse = await fetch('/api/v3/agents/shared_default/repository-connections',
    {credentials:'same-origin'});
  const catalog = await catalogResponse.json().catch(() => ({}));
  if (!catalogResponse.ok) throw Error(catalog.error || 'Repository connections unavailable');
  const current = (catalog.connections || []).find(item => item.id === id);
  if (revoked && !current) return;
  const body = {expected_revision:catalog.revision, url:url || current?.url,
    username:current?.username || 'git', revoked};
  if (token) body.token = token;
  const response = await fetch('/api/v3/agents/shared_default/repository-connections/' +
      encodeURIComponent(id), {method:'PUT',credentials:'same-origin',
      headers:{'Content-Type':'application/json', ...authHeaders()}, body:JSON.stringify(body)});
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(result.error || 'Could not save repository connection');
}
async function applyRepoSettings(id, folder, payload) {
  const repo = folder === '/live-checkout' ? '__root__' :
    folder.startsWith('/live-checkout/') ? folder.slice('/live-checkout/'.length) : '';
  if (!repo) throw Error('Repository outside development checkout');
  const status = await v3Files({action:'git_status', repo});
  const remote = String(payload.remote_url || '');
  const token = String(payload.token || '');
  if (token && !remote) throw Error('A remote URL is required to save a token');
  if (remote) await saveConnection(id, remote, token);
  await v3Files({action:'git_settings', repo, expected_head:status.head,
    remote_url:remote, git_user_name:String(payload.git_user_name || ''),
    git_user_email:String(payload.git_user_email || '')});
  if (!remote) await saveConnection(id, '', '', true);
}
async function ghFetch(path, opts = {}) {
  const url = new URL(path, location.origin);
  const endpoint = url.pathname.replace('/api/v1/github/', '');
  const payload = opts.body ? JSON.parse(opts.body) : {};
  const repo = selectedRepo();
  if (endpoint === 'repos' && !opts.method) return {repos:repoState()};
  if (endpoint === 'repos/select') {
    const entries = repoState();
    if (!entries.some(row => row.id === payload.id)) throw Error('Repository not found');
    for (const row of entries) row.active = row.id === payload.id;
    lastGitStatus = null;
    return saveRepoState(entries);
  }
  if (endpoint === 'repos/validate') {
    const folder = url.searchParams.get('path') || '';
    const value = folder === '/live-checkout' ? '__root__' :
      folder.startsWith('/live-checkout/') ? folder.slice('/live-checkout/'.length) : '';
    if (!value) return {is_repo:false, message:'Select a folder inside the development checkout'};
    try {
      const status = await v3Files({action:'git_status', repo:value});
      return {is_repo:true, origin:status.remote_url || '', branch:status.branch};
    } catch (error) { return {is_repo:false, message:error.message}; }
  }
  if (url.pathname === '/api/v1/files/tree') {
    return v3Files({action:'tree', path:url.searchParams.get('path') || ''});
  }
  if (endpoint === 'repos' && opts.method === 'POST') {
    const folder = String(payload.folder || '');
    const validation = await ghFetch('/api/v1/github/repos/validate?path=' + encodeURIComponent(folder));
    if (!validation.is_repo) throw Error(validation.message || 'Repository unavailable');
    const entries = repoState();
    const id = 'repo-' + crypto.randomUUID();
    await applyRepoSettings(id, folder, payload);
    entries.push({id, label:String(payload.label || folder.split('/').pop()).slice(0,80),
      folder, remote_url:String(payload.remote_url || ''), builtin:false, active:false});
    return saveRepoState(entries);
  }
  if (endpoint.startsWith('repos/') && opts.method === 'DELETE') {
    const id = decodeURIComponent(endpoint.slice('repos/'.length));
    if (id === ROOT_REPO.id) throw Error('The development repository cannot be removed');
    await saveConnection(id, '', '', true);
    return saveRepoState(repoState().filter(row => row.id !== id));
  }
  if (endpoint.startsWith('repos/') && opts.method === 'POST') {
    const id = decodeURIComponent(endpoint.slice('repos/'.length));
    const entries = repoState();
    const row = entries.find(item => item.id === id);
    if (!row) throw Error('Repository not found');
    const folder = String(payload.folder || '');
    const validation = await ghFetch('/api/v1/github/repos/validate?path=' + encodeURIComponent(folder));
    if (!validation.is_repo) throw Error(validation.message || 'Repository unavailable');
    await applyRepoSettings(id, folder, payload);
    row.folder = folder;
    row.label = String(payload.label || row.label).slice(0,80);
    row.remote_url = String(payload.remote_url || '');
    return saveRepoState(entries);
  }
  if (endpoint === 'status') {
    lastGitStatus = await v3Files({action:'git_status', repo});
    if (url.searchParams.get('wait') === '1' && lastGitStatus.has_remote) {
      await v3Files({action:'git_tool', repo, operation:'fetch',
        expected_head:lastGitStatus.head});
      lastGitStatus = await v3Files({action:'git_status', repo});
    }
    return lastGitStatus;
  }
  if (endpoint === 'log-graph') {
    return v3Files({action:'git_graph', repo, limit:Number(url.searchParams.get('limit') || 80)});
  }
  if (endpoint.startsWith('commit/')) {
    return v3Files({action:'git_commit_detail', repo, hash:decodeURIComponent(endpoint.slice(7))});
  }
  if (endpoint === 'branches') return v3Files({action:'git_branches', repo});
  if (['push','pull','merge','checkout'].includes(endpoint)) {
    if (!lastGitStatus) lastGitStatus = await v3Files({action:'git_status', repo});
    const expectedHead = lastGitStatus.head;
    lastGitStatus = null;
    const operation = endpoint;
    const body = {action:'git_tool', repo, operation, expected_head:expectedHead};
    if (endpoint === 'merge' || endpoint === 'checkout') {
      const branches = await v3Files({action:'git_branches', repo});
      const row = (branches.branches || []).find(item => item.name === payload.branch);
      if (!row) throw Error('Branch changed; refresh before continuing');
      if (!row.local && row.remote && endpoint === 'checkout') {
        await v3Files({action:'git_tool', repo, operation:'branch_create',
          name:row.name, ref:'origin/' + row.name, expected_head:expectedHead});
        body.ref = row.name;
      } else {
        body.ref = row.local ? row.name : 'origin/' + row.name;
      }
    }
    const result = await v3Files(body);
    lastGitStatus = null;
    return {status:'ok', message:result.text || operation + ' completed', backend_changed:false};
  }
  if (endpoint === 'repos/validate' || endpoint.startsWith('production/')) {
    throw Error('Production actions are unavailable in V3 File Manager');
  }
  throw Error('Unsupported V3 Git Control request: ' + endpoint);
}

function flagClass(flag) {
  return ({ M: 'fg-flag-m', A: 'fg-flag-a', D: 'fg-flag-d', R: 'fg-flag-r', '??': 'fg-flag-u' })[flag] || 'fg-flag-m';
}

function flagLabel(flag) {
  return ({ M: 'Modified', A: 'Added', D: 'Deleted', R: 'Renamed', '??': 'Untracked', U: 'Updated' })[flag] || flag;
}

// ── Public render entry ────────────────────────────────────────────

let _state = {
  status: null,
  graph: null,
  loading: false,
  err: null,
  graphErr: null,
};
let _remoteRefreshPromise = null;

// Production-mirror status (the "Release to production" panel). Loaded lazily on
// open + after a release + on the manual refresh — NOT on every auto-poll tick,
// since it runs git in the production folder. Cached here so each repaint of the
// git body re-renders the Production section without re-fetching.
let _prodState = null;

// ── Multi-repo selector ─────────────────────────────────────────────
// The Git Control page can manage MORE than this app's own repo: point it at
let _commitSelectionHandler = null;
let _selectedCommitKey = '';
// any local git folder + its own GitHub remote + key and the whole page (Changes,
// commit graph, commit/push/pull) re-points to THAT repo. The list comes from
export function setSelectedGitCommit(repoId, hash) {
  _selectedCommitKey = repoId && hash ? repoId + ':' + hash : '';
  const body = _autoRootEl?.querySelector('#fg-body');
  const visibleRepoId = _activeRepo()?.id || 'webagent';
  body?.querySelectorAll('.fg-graph-row').forEach((row) => {
    const rowHash = row.dataset.hash || row.dataset.short || '';
    row.classList.toggle('selected', _selectedCommitKey === visibleRepoId + ':' + rowHash);
  });
}

// GET /api/v1/github/repos; each entry is {id,label,folder,remote_url,has_token,
// builtin,active}. WebAgent (builtin) is always first and can't be removed —
// selecting it behaves exactly like before. Selecting another repo is a server-side
// switch (app/api/github.py _use_active_repo), so a refresh shows that repo's data.
let _repos = [];          // last-loaded repo list
let _repoMenuOpen = false; // dropdown open? (kept across repaints so a poll won't close it)
let _repoFormMode = null;  // null = form hidden, 'add' = add, or a repo id = edit
let _repoBrowsePath = '';  // current folder shown in the Browse… popover
let _repoError = '';       // transient error shown under the selector (switch/remove)

function _activeRepo() { return _repos.find(r => r.active) || _repos[0] || null; }

// The token row + Production section are WebAgent-only concepts, so they hide when
// a different repo is active (that repo's key is managed in its own Edit form).
function _isActiveBuiltin() { const a = _activeRepo(); return !a || !!a.builtin; }

async function loadRepos() {
  try {
    const r = await ghFetch('/api/v1/github/repos');
    _repos = (r && r.repos) || [];
  } catch (_) { /* keep the last list on a transient error */ }
  return _repos;
}

// ── Sticky result lines & button-working state ──────────────────────
//
// The status lines (#fg-commit-result …) and the action buttons live INSIDE
// #fg-body, which renderGitPanel() rebuilds wholesale on every repaint. So a
// message written mid-commit, or the spinner on a clicked button, used to vanish
// the instant the panel refreshed — and stay blank through the slow live remote
// fetch that follows a commit, which is exactly the "message disappears then
// nothing happens for ages" the user sees on large commits.
//
// Fix: remember the active message / working button in module state and RE-APPLY
// it after every rebuild (see the tail of renderGitPanel), so the line stays put
// until the flow explicitly releases it — normally once the FINAL graph update
// has landed. Keyed by element id so independent lines don't clobber each other.
const _stickyResults = new Map();   // id → { msg, tone, opts }
let _workingBtnId = null;           // selector of the button to keep spinning, or null
let _commitAbortController = null;  // owns the currently streamed commit request

function setStickyResult(rootEl, id, msg, tone, opts = {}) {
  _stickyResults.set(id, { msg, tone, opts });
  if (rootEl) showResult(rootEl.querySelector('#' + id), msg, tone, opts);
}

function clearStickyResult(id) {
  _stickyResults.delete(id);
}

// Re-apply any pinned result lines / working-button state onto a freshly-rebuilt
// body so a repaint never blanks an in-progress flow. Called at the end of
// renderGitPanel(), after the new markup (and its buttons/result slots) exist.
function reapplyStickyState(body) {
  if (!body) return;
  for (const [id, r] of _stickyResults) {
    const el = body.querySelector('#' + id);
    if (el) showResult(el, r.msg, r.tone, r.opts);
  }
  if (_workingBtnId) {
    const btn = body.querySelector(_workingBtnId);
    if (btn) { btn.disabled = true; btn.classList.add('is-working'); }
  }
}

// ── Skeleton-first paint ────────────────────────────────────────────
//
// The panel used to blank to a single "Loading…" line until BOTH the git status
// and the commit graph had returned — so on a slow / remote repo the user stared
// at nothing and couldn't even press Commit & Push. Instead we paint the whole
// panel STRUCTURE on the first frame — repo selector, commit box, and every action
// button (live and clickable immediately) — with a shimmer over the two areas that
// still have to load: the Changes list and the commit graph. renderGitPanel() then
// swaps each shimmer for real data the moment its own fetch lands. The commit /
// push / pull actions run entirely server-side (they never read _state.status), so
// the buttons are safe to enable optimistically — pressing Commit & Push before the
// Changes list has painted just runs the server flow, which cleanly reports
// "Nothing to commit" when there was nothing to do.
// REMOVE-WHEN: the Git Control panel drops skeleton-first loading.
function renderChangesSkeleton() {
  const rows = ['62%', '48%', '70%'].map((w) => `
      <div class="fg-file fg-skel-file">
        <span class="fg-skel fg-skel-flag"></span>
        <span class="fg-skel fg-skel-line" style="width:${w};"></span>
      </div>`).join('');
  return `
    <details class="fg-section fg-changes" open>
      <summary><span>Changes</span><span class="fg-skel fg-skel-count"></span></summary>
      <div class="fg-file-list">${rows}</div>
    </details>`;
}

function renderGraphSkeleton() {
  const rows = ['72%', '54%', '80%', '46%', '66%', '58%'].map((w) => `
      <div class="fg-graph-row fg-skel-graph-row">
        <div class="fg-graph-row-head">
          <div class="fg-graph-col" style="width:${GRAPH_LANE_W}px;">
            <span class="fg-skel fg-skel-dot"></span>
          </div>
          <div class="fg-graph-meta">
            <div class="fg-graph-msg-line"><span class="fg-skel fg-skel-line" style="width:${w};"></span></div>
            <div class="fg-graph-sub"><span class="fg-skel fg-skel-line fg-skel-line-sm" style="width:34%;"></span></div>
          </div>
        </div>
      </div>`).join('');
  return `
    <div class="fg-section fg-graph">
      <div class="fg-section-title">Commit graph <span class="fg-graph-hint">(all branches)</span></div>
      <div class="fg-graph-list" style="--lane-w:${GRAPH_LANE_W}px; --row-h:${GRAPH_ROW_H}px;">${rows}</div>
    </div>`;
}

// Paint the full panel structure with shimmers over the not-yet-loaded data areas
// and wire every button so the panel is usable from the first frame. Called on
// open (before any fetch) and re-called whenever we still have no status to show.
function renderGitSkeleton(rootEl) {
  if (!rootEl) return;
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;
  body.innerHTML =
    renderRepoSelector() +
    renderSyncSection({ file_count: 1, ahead: 0, has_remote: true }) +            // Pull / Push / Merge / Release / Refresh clickable now
    renderActionResults() +
    renderChangesSkeleton() +
    renderGraphSkeleton();
  wireEvents(rootEl, null, null);
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
}

function renderGitPanel(rootEl) {
  if (!rootEl) return;
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;
  if (_state.loading && !_state.status) {
    renderGitSkeleton(rootEl);
    return;
  }
  // Status error AND no cached status = can't render anything useful. Still show
  // the repo selector so the user can switch back to a working repo (e.g. WebAgent)
  // when the selected one errors (bad folder, missing key, etc.).
  if (_state.err && !_state.status) {
    body.innerHTML = renderRepoSelector() +
      `<div class="fg-error fg-click-copy">${_esc(_state.err)}</div>
      <button class="fg-btn" data-act="retry">Retry</button>`;
    const r = body.querySelector('[data-act="retry"]');
    if (r) r.addEventListener('click', () => refreshGit(rootEl, { remote: true }));
    wireRepoSelector(rootEl);
    if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
    return;
  }
  const statusError = _state.err ? `<div class="fg-error fg-click-copy">${_esc(_state.err)}</div>` : '';
  const s = _state.status;
  const g = _state.graph;
  if (!s) {
    renderGitSkeleton(rootEl);
    return;
  }
  body.innerHTML = renderRepoSelector() +
                   statusError +
                   renderSyncSection(s) +
                   renderActionResults() +
                   renderChangesSection(s) +
                   renderGraphSection(s, g, _state.graphErr);
  wireEvents(rootEl, s, g);
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(body.querySelectorAll('[data-lucide]:not(.lucide)')) });
  if (!_state.graphErr) drawGraph(rootEl, g);
  // Restore any in-progress commit/push status line + button spinner that this
  // rebuild would otherwise have wiped (see _stickyResults / _workingBtnId).
  reapplyStickyState(body);
}

// ── Sections ───────────────────────────────────────────────────────

// The repo selector strip sits at the very top of the panel. It shows the active
// repo as a dropdown trigger; the menu lists every configured repo + an "Add
// repository…" action, and (for non-default repos) per-row Edit / Remove.
function renderRepoSelector() {
  const active = _activeRepo();
  const label = _esc(active ? active.label : 'WebAgent (this app)');
  const folder = _esc(active ? active.folder : '');
  const rows = _repos.map((r) => {
    const isActive = !!r.active;
    // The built-in repo gets the same pencil (its GitHub address + key are
    // editable) but no trash — it can't be removed.
    const editBtn = `
        <button type="button" class="fg-repo-edit" data-id="${_esc(r.id)}" title="Edit this repository" aria-label="Edit repository"><i data-lucide="pencil" class="lucide-icon"></i></button>`;
    const tools = r.builtin ? editBtn : `${editBtn}
        <button type="button" class="fg-repo-remove" data-id="${_esc(r.id)}" title="Remove this repository" aria-label="Remove repository"><i data-lucide="trash-2" class="lucide-icon"></i></button>`;
    return `
      <div class="fg-repo-row${isActive ? ' active' : ''}" role="option" aria-selected="${isActive}">
        <button type="button" class="fg-repo-pick" data-id="${_esc(r.id)}" title="${_esc(r.folder)}">
          <i data-lucide="${isActive ? 'check' : 'folder'}" class="lucide-icon fg-repo-row-icon"></i>
          <span class="fg-repo-row-text">
            <span class="fg-repo-row-label">${_esc(r.label)}${r.builtin ? ' <span class="fg-repo-tag">default</span>' : ''}</span>
            <span class="fg-repo-row-folder">${_esc(r.folder)}</span>
          </span>
        </button>${tools}
      </div>`;
  }).join('');
  // Edit / Remove for the *active* repo live right on the always-visible header
  // (next to the dropdown trigger) so the user can edit — or, for an added repo,
  // remove — the current repo without expanding the list. Same classes as the
  // in-menu buttons, so wireRepoSelector picks them up with no extra wiring. The
  // built-in repo shows only the pencil (it can't be removed).
  const activeId = active ? _esc(active.id) : '';
  const activeTools = active ? (active.builtin ? `
        <button type="button" class="fg-repo-edit" data-id="${activeId}" title="Edit this repository" aria-label="Edit repository"><i data-lucide="pencil" class="lucide-icon"></i></button>` : `
        <button type="button" class="fg-repo-edit" data-id="${activeId}" title="Edit this repository" aria-label="Edit repository"><i data-lucide="pencil" class="lucide-icon"></i></button>
        <button type="button" class="fg-repo-remove" data-id="${activeId}" title="Remove this repository" aria-label="Remove repository"><i data-lucide="trash-2" class="lucide-icon"></i></button>`) : '';
  return `
    <div class="fg-section fg-repo-selector">
      <div class="fg-repo-trigger-row">
        <button type="button" class="fg-repo-trigger" id="fg-repo-trigger" title="Switch repository" aria-haspopup="listbox" aria-expanded="${_repoMenuOpen}">
          <i data-lucide="folder-git-2" class="lucide-icon fg-repo-trigger-icon"></i>
          <span class="fg-repo-active">
            <span class="fg-repo-active-label">${label}</span>
            <span class="fg-repo-active-folder" title="${folder}">${folder || 'No folder'}</span>
          </span>
          <i data-lucide="chevron-down" class="lucide-icon fg-repo-chev"></i>
        </button>${activeTools}
      </div>
      <div class="fg-repo-menu" id="fg-repo-menu" ${_repoMenuOpen ? '' : 'hidden'} role="listbox" aria-label="Repositories">
        ${rows || '<div class="fg-branch-menu-loading">Loading…</div>'}
        <button type="button" class="fg-repo-add" id="fg-repo-add-btn"><i data-lucide="plus" class="lucide-icon"></i> Add repository…</button>
      </div>
      ${renderRepoForm()}
      ${_repoError ? `<div class="fg-repo-error">${_esc(_repoError)}</div>` : ''}
    </div>
  `;
}

// The Add / Edit form. Shown inline below the selector when adding or editing a
// repo. The folder field has a Browse… button that walks the host's folders via
// the existing GET /api/v1/files/tree endpoint.
function renderRepoForm() {
  if (!_repoFormMode) return '';
  const editing = _repoFormMode !== 'add';
  const r = editing ? _repos.find((x) => x.id === _repoFormMode) : null;
  const builtin = !!(r && r.builtin);
  const title = editing ? (builtin ? 'Edit WebAgent repository' : 'Edit repository') : 'Add repository';
  const saveLbl = editing ? 'Save' : 'Add';
  const label = _esc(r ? r.label : '');
  const folder = _esc(r ? r.folder : '');
  const remote = _esc(r ? r.remote_url : '');
  const gitUserName = _esc(r ? (r.git_user_name || '') : '');
  const gitUserEmail = _esc(r ? (r.git_user_email || '') : '');
  const tokPlaceholder = (editing && r && r.has_token)
    ? 'GitHub key saved — leave blank to keep it'
    : 'GitHub key for this repo…';
  // The built-in repo's name + folder are this app's own and can't be changed —
  // only its GitHub address and shared key are editable, so lock the first two.
  const nameField = builtin
    ? `<input type="text" id="fg-repo-f-label" class="fg-input" value="${label}" autocomplete="off" disabled>`
    : `<input type="text" id="fg-repo-f-label" class="fg-input" placeholder="Name (e.g. My website)" value="${label}" autocomplete="off">`;
  const folderRow = builtin
    ? `<input type="text" id="fg-repo-f-folder" class="fg-input" value="${folder}" autocomplete="off" disabled title="This app's own folder — fixed">`
    : `<div class="fg-repo-folder-row">
        <input type="text" id="fg-repo-f-folder" class="fg-input" placeholder="Local folder path…" value="${folder}" autocomplete="off" data-lpignore="true">
        <button type="button" class="fg-btn fg-repo-browse-btn" id="fg-repo-browse-btn" title="Browse folders on this computer">Browse…</button>
      </div>
      <div class="fg-repo-browser" id="fg-repo-browser" hidden></div>`;
  return `
    <div class="fg-repo-form" id="fg-repo-form">
      <div class="fg-repo-form-title">${title}</div>
      ${nameField}
      ${folderRow}
      <input type="text" id="fg-repo-f-remote" class="fg-input" placeholder="GitHub address (https://github.com/you/repo.git)" value="${remote}" autocomplete="off" data-lpignore="true">
      <!-- GitHub key + a "?" that toggles the token-creation help below (classic +
           fine-grained steps). This help moved here from the old inline token row
           under the repo selector, so the Edit form is now the single place a
           repo's key is set. Wired in wireRepoSelector() → #fg-repo-token-help-btn. -->
      <div class="fg-repo-token-row">
        <input type="password" id="fg-repo-f-token" class="fg-input" placeholder="${tokPlaceholder}" autocomplete="off" data-lpignore="true" data-1p-ignore="true">
        <button type="button" class="fg-token-help-link" id="fg-repo-token-help-btn" aria-expanded="false" aria-controls="fg-repo-token-help" title="How to create a GitHub token" aria-label="How to create a GitHub token">?</button>
      </div>
      <div id="fg-repo-token-help" class="fg-token-help" hidden>
        <p class="fg-token-help-h">Create a <strong>classic</strong> token</p>
        <ol>
          <li>Open <a href="https://github.com/settings/tokens" target="_blank" rel="noopener">github.com/settings/tokens</a></li>
          <li>Click <strong>Generate new token (classic)</strong></li>
          <li>Select the <strong>repo</strong> scope (full control)</li>
          <li>Select the <strong>workflow</strong> scope if this repo contains files under <code>.github/workflows/</code></li>
          <li>Generate, copy it, and paste it above</li>
        </ol>
        <p class="fg-token-help-h">Or a <strong>fine-grained</strong> token</p>
        <ol>
          <li>Open <a href="https://github.com/settings/tokens?type=beta" target="_blank" rel="noopener">tokens (fine-grained)</a></li>
          <li>Select this repository only</li>
          <li>Permissions → <strong>Contents: Read and write</strong></li>
          <li>Permissions → <strong>Workflows: Read and write</strong> if this repo contains GitHub Actions workflows</li>
          <li>Generate, copy it, and paste it above</li>
        </ol>
      </div>
      <div class="fg-repo-form-section-title">Git author identity</div>
      <input type="text" id="fg-repo-f-user-name" class="fg-input" placeholder="Git user name (e.g. Your Name)" value="${gitUserName}" autocomplete="off" data-lpignore="true">
      <input type="text" id="fg-repo-f-user-email" class="fg-input" placeholder="Git user email (e.g. you@example.com)" value="${gitUserEmail}" autocomplete="off" data-lpignore="true">
      <div class="fg-repo-form-actions">
        <button type="button" class="fg-btn fg-btn-primary" id="fg-repo-save-btn">${saveLbl}</button>
        <button type="button" class="fg-btn" id="fg-repo-cancel-btn">Cancel</button>
      </div>
      <div id="fg-repo-form-result" class="fg-result" hidden></div>
    </div>
  `;
}

// ── Repo selector behaviour ─────────────────────────────────────────
// Attached from wireEvents() (normal paint) and the error branch of
// renderGitPanel() so the user can always switch repos. State (open/form/error)
// lives in module vars and drives renderRepoSelector(), so action handlers just
// mutate the vars and repaint via renderGitPanel().
function wireRepoSelector(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;

  const trigger = body.querySelector('#fg-repo-trigger');
  const menu = body.querySelector('#fg-repo-menu');
  if (trigger && menu) {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      _repoMenuOpen = !_repoMenuOpen;
      menu.hidden = !_repoMenuOpen;
      trigger.setAttribute('aria-expanded', String(_repoMenuOpen));
    });
  }

  body.querySelectorAll('.fg-repo-pick').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); if (btn.dataset.id) selectRepo(rootEl, btn.dataset.id); });
  });
  body.querySelectorAll('.fg-repo-edit').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); openRepoForm(rootEl, btn.dataset.id); });
  });
  body.querySelectorAll('.fg-repo-remove').forEach((btn) => {
    btn.addEventListener('click', (e) => { e.stopPropagation(); removeRepoEntry(rootEl, btn.dataset.id); });
  });
  const addBtn = body.querySelector('#fg-repo-add-btn');
  if (addBtn) addBtn.addEventListener('click', (e) => { e.stopPropagation(); openRepoForm(rootEl, 'add'); });

  // Form controls (present only while the add/edit form is open).
  const saveBtn = body.querySelector('#fg-repo-save-btn');
  if (saveBtn) saveBtn.addEventListener('click', (e) => { e.stopPropagation(); submitRepoForm(rootEl); });
  const cancelBtn = body.querySelector('#fg-repo-cancel-btn');
  if (cancelBtn) cancelBtn.addEventListener('click', (e) => { e.stopPropagation(); closeRepoForm(rootEl); });
  const browseBtn = body.querySelector('#fg-repo-browse-btn');
  if (browseBtn) browseBtn.addEventListener('click', (e) => { e.stopPropagation(); toggleFolderBrowser(rootEl); });
  // "?" toggles the inline token-creation help below the key field.
  const tokHelpBtn = body.querySelector('#fg-repo-token-help-btn');
  const tokHelp = body.querySelector('#fg-repo-token-help');
  if (tokHelpBtn && tokHelp) tokHelpBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = tokHelp.hidden;             // about to open if currently hidden
    tokHelp.hidden = !open;
    tokHelpBtn.setAttribute('aria-expanded', open ? 'true' : 'false');
    tokHelpBtn.classList.toggle('is-open', open);
  });

  // Close the dropdown on an outside click (registered once per body element).
  if (!body.__fgRepoCloser) {
    body.__fgRepoCloser = (ev) => {
      if (ev.target.closest('.fg-repo-selector')) return;
      if (!_repoMenuOpen) return;
      _repoMenuOpen = false;
      const m = body.querySelector('#fg-repo-menu');
      const t = body.querySelector('#fg-repo-trigger');
      if (m) m.hidden = true;
      if (t) t.setAttribute('aria-expanded', 'false');
    };
    document.addEventListener('click', body.__fgRepoCloser);
  }
}

// Switch the page to another repo. The server re-points every git action, so we
// just reload the list and pull this repo's changes + commit graph.
async function selectRepo(rootEl, id) {
  _repoError = '';
  const active = _activeRepo();
  if (active && active.id === id) { _repoMenuOpen = false; renderGitPanel(rootEl); return; }
  _repoMenuOpen = false;
  _repoFormMode = null;
  try {
    const r = await ghFetch('/api/v1/github/repos/select', { method: 'POST', body: JSON.stringify({ id }) });
    _repos = (r && r.repos) || _repos;
  } catch (e) {
    _repoError = e.message || 'Could not switch repository';
    renderGitPanel(rootEl);
    return;
  }
  _prodState = null;                       // production status is WebAgent-only
  await refreshGit(rootEl, { remote: false });
  loadProdState(rootEl);                    // refills the card if we switched back to WebAgent
}

function openRepoForm(rootEl, mode) {
  _repoError = '';
  _repoFormMode = mode;                     // 'add' or a repo id
  _repoMenuOpen = false;
  _repoBrowsePath = '';
  renderGitPanel(rootEl);
  const body = rootEl.querySelector('#fg-body');
  const first = body && body.querySelector('#fg-repo-f-label');
  if (first) { try { first.focus(); } catch (_) {} }
}

function closeRepoForm(rootEl) {
  _repoFormMode = null;
  _repoBrowsePath = '';
  renderGitPanel(rootEl);
}

async function submitRepoForm(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;
  const val = (sel) => { const el = body.querySelector(sel); return el ? el.value.trim() : ''; };
  const label = val('#fg-repo-f-label');
  const folder = val('#fg-repo-f-folder');
  const remote = val('#fg-repo-f-remote');
  const token = val('#fg-repo-f-token');
  const gitUserName = val('#fg-repo-f-user-name');
  const gitUserEmail = val('#fg-repo-f-user-email');
  const resultEl = body.querySelector('#fg-repo-form-result');
  const editing = _repoFormMode && _repoFormMode !== 'add';
  const saveBtn = body.querySelector('#fg-repo-save-btn');
  if (saveBtn) { saveBtn.disabled = true; saveBtn.classList.add('is-working'); }
  try {
    let r;
    if (editing) {
      const payload = { label, folder, remote_url: remote, git_user_name: gitUserName, git_user_email: gitUserEmail };
      if (token) payload.token = token;     // blank keeps the existing key
      r = await ghFetch('/api/v1/github/repos/' + encodeURIComponent(_repoFormMode), { method: 'POST', body: JSON.stringify(payload) });
    } else {
      r = await ghFetch('/api/v1/github/repos', { method: 'POST', body: JSON.stringify({ label, folder, remote_url: remote, token, git_user_name: gitUserName, git_user_email: gitUserEmail }) });
    }
    _repos = (r && r.repos) || _repos;
    _repoFormMode = null;
    _repoBrowsePath = '';
    // Load local status and graph. A repository without a remote has no
    // network transport to refresh after it is added.
    await refreshGit(rootEl, { remote: false });
  } catch (e) {
    if (saveBtn) { saveBtn.disabled = false; saveBtn.classList.remove('is-working'); }
    if (resultEl) {
      resultEl.hidden = false;
      resultEl.textContent = e.message || 'Could not save repository';
      resultEl.classList.add('err'); resultEl.classList.remove('ok');
    }
  }
}

async function removeRepoEntry(rootEl, id) {
  const repo = _repos.find((r) => r.id === id);
  const name = repo ? repo.label : 'this repository';
  if (!window.confirm('Remove “' + name + '” from the list?\n\nThis only forgets it here — your local folder and its GitHub repo are left untouched.')) return;
  _repoError = '';
  try {
    const r = await ghFetch('/api/v1/github/repos/' + encodeURIComponent(id), { method: 'DELETE' });
    _repos = (r && r.repos) || _repos;
  } catch (e) {
    _repoError = e.message || 'Could not remove repository';
    renderGitPanel(rootEl);
    return;
  }
  // Removing the active repo makes the server fall back to WebAgent — refresh.
  _prodState = null;
  await refreshGit(rootEl, { remote: true });
  loadProdState(rootEl);
}

// Folder browser popover — reuses GET /api/v1/files/tree (admin-gated) to walk the
// host's folders so the user can pick a repo folder without typing a full path.
function toggleFolderBrowser(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const browser = body && body.querySelector('#fg-repo-browser');
  if (!browser) return;
  if (!browser.hidden) { browser.hidden = true; return; }
  browser.hidden = false;
  const folderInput = body.querySelector('#fg-repo-f-folder');
  const start = (folderInput && folderInput.value.trim()) || _repoBrowsePath || '';
  loadFolderBrowser(rootEl, start);
}

async function loadFolderBrowser(rootEl, path) {
  const body = rootEl.querySelector('#fg-body');
  const browser = body && body.querySelector('#fg-repo-browser');
  if (!browser) return;
  browser.innerHTML = '<div class="fg-branch-menu-loading">Loading…</div>';
  let data;
  try {
    data = await ghFetch('/api/v1/files/tree?path=' + encodeURIComponent(path || ''));
  } catch (e) {
    browser.innerHTML = '<div class="fg-repo-browser-err">' + _esc(e.message || 'Could not list folders') + '</div>';
    return;
  }
  _repoBrowsePath = data.path || '';
  const dirs = (data.entries || []).filter((en) => en.is_dir);
  const up = data.parent
    ? `<button type="button" class="fg-repo-browser-item up" data-path="${_esc(data.parent)}"><i data-lucide="corner-left-up" class="lucide-icon"></i> ..</button>`
    : '';
  const list = dirs.map((d) =>
    `<button type="button" class="fg-repo-browser-item" data-path="${_esc(d.path)}"><i data-lucide="folder" class="lucide-icon"></i> ${_esc(d.name)}</button>`
  ).join('');
  browser.innerHTML = `
    <div class="fg-repo-browser-path" title="${_esc(_repoBrowsePath)}">${_esc(_repoBrowsePath)}</div>
    <div class="fg-repo-browser-list">${up}${list || '<div class="fg-repo-browser-empty">No sub-folders here</div>'}</div>
    <div class="fg-repo-browser-actions">
      <button type="button" class="fg-btn fg-btn-primary fg-repo-browser-use">Use this folder</button>
    </div>`;
  browser.querySelectorAll('.fg-repo-browser-item').forEach((it) => {
    it.addEventListener('click', (e) => { e.stopPropagation(); loadFolderBrowser(rootEl, it.dataset.path); });
  });
  const useBtn = browser.querySelector('.fg-repo-browser-use');
  if (useBtn) useBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    const fi = body.querySelector('#fg-repo-f-folder');
    if (fi) fi.value = _repoBrowsePath;
    browser.hidden = true;
    prefillFromFolder(rootEl, _repoBrowsePath);
  });
  if (window.lucide) window.lucide.createIcons({ nodes: Array.from(browser.querySelectorAll('[data-lucide]:not(.lucide)')) });
}

// After a folder is picked, validate it server-side and pre-fill the GitHub
// address + name from the repo's own origin when those fields are still blank.
async function prefillFromFolder(rootEl, path) {
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;
  let info;
  try { info = await ghFetch('/api/v1/github/repos/validate?path=' + encodeURIComponent(path || '')); }
  catch (_) { return; }
  const resultEl = body.querySelector('#fg-repo-form-result');
  if (resultEl) {
    if (!info.is_repo) {
      resultEl.hidden = false; resultEl.classList.add('err'); resultEl.classList.remove('ok');
      resultEl.textContent = info.message || 'That folder is not a git repository.';
    } else {
      resultEl.hidden = true; resultEl.textContent = '';
    }
  }
  if (info.is_repo) {
    const remoteEl = body.querySelector('#fg-repo-f-remote');
    if (remoteEl && !remoteEl.value.trim() && info.origin) remoteEl.value = info.origin;
    const labelEl = body.querySelector('#fg-repo-f-label');
    if (labelEl && !labelEl.value.trim()) {
      const parts = (path || '').replace(/\/+$/, '').split('/');
      labelEl.value = parts[parts.length - 1] || '';
    }
  }
}

// The branch picker (+ ahead/behind sync state) used to sit in its own header
// directly under the repo selector, alongside the remote-URL and token rows. Those
// two rows were redundant — the remote address and GitHub key are now edited from
// the repo selector's Edit form — so they were removed, and the branch picker moved
// DOWN next to the Commit graph (renderGraphSection), where switching branch and
// reading history belong together. Returns the trigger + the (absolutely-positioned)
// branch menu; the graph section wrapper is `position: relative` so the menu anchors.
function renderBranchBar(s) {
  const branch = _esc((s && s.branch) || '—');
  let sync = '';
  if (s && s.has_remote && (s.ahead > 0 || s.behind > 0)) {
    sync = `<span class="fg-ahead" title="ahead">↑${s.ahead}</span><span class="fg-behind" title="behind">↓${s.behind}</span>`;
  } else if (s && s.has_remote) {
    sync = '<span class="fg-sync-clean">in sync</span>';
  }
  return `
    <div class="fg-branch-bar">
      <button type="button" class="fg-branch-row fg-branch-picker-trigger" id="fg-branch-picker-trigger" title="Switch branch">
        <i data-lucide="git-branch" class="lucide-icon"></i>
        <span class="fg-branch-name">${branch}</span>
        <i data-lucide="chevron-down" class="lucide-icon fg-branch-chev"></i>
        ${sync}
      </button>
      <div class="fg-branch-menu" id="fg-branch-menu" hidden role="listbox" aria-label="Switch branch">
        <div class="fg-branch-menu-loading">Loading branches…</div>
      </div>
    </div>
  `;
}

function renderChangesSection(s) {
  const items = [
    ...(s.staged || []).map(f => ({ ...f, area: 'Staged' })),
    ...(s.unstaged || []).map(f => ({ ...f, area: 'Unstaged' })),
    ...(s.untracked || []).map(f => ({ ...f, flag: '??', area: 'Untracked' })),
  ];
  const count = items.length;
  const list = count === 0
    ? '<div class="fg-clean">No local changes</div>'
    : items.map(f => `
        <div class="fg-file" title="${_esc(f.path)}">
          <span class="fg-file-flag ${flagClass(f.flag)}" title="${_esc(flagLabel(f.flag))}">${_esc(f.flag)}</span>
          <span class="fg-file-path">${_esc(f.path)}</span>
          ${Number.isFinite(f.added) && f.added > 0 ? `<span class="fg-file-stat fg-file-stat-add">+${f.added}</span>` : ''}
          ${Number.isFinite(f.removed) && f.removed > 0 ? `<span class="fg-file-stat fg-file-stat-del">-${f.removed}</span>` : ''}
        </div>
      `).join('');
  return `
    <details class="fg-section fg-changes" ${count ? 'open' : ''}>
      <summary><span>Changes</span><span class="fg-count">${count}</span></summary>
      <div class="fg-file-list">${list}</div>
    </details>
  `;
}

function renderCommitButtons(s) {
  const enabled = s.file_count > 0;
  const starEnabled = s.file_count > 0 || (s.ahead || 0) > 0;
  const starTitle = starEnabled
    ? 'Commit & push now — auto-writes the note, safety-checks, commits & pushes instantly'
    : 'Nothing to commit or push';
  return `
    <button class="fg-btn" id="fg-commit-btn" ${enabled ? '' : 'disabled'} title="Save changes locally — commits everything, auto-writes the note, and does not push">
      <i data-lucide="check" class="lucide-icon"></i> Commit
    </button>
    <button class="fg-btn" id="fg-sc-star-btn" ${starEnabled ? '' : 'disabled'} title="${starTitle}" aria-label="Commit and push now">
      <i data-lucide="star" class="lucide-icon"></i> Commit &amp; Push
    </button>
  `;
}

function renderSyncSection(s) {
  return `
    <div class="fg-section fg-sync">
      ${renderCommitButtons(s)}
      ${s.has_remote ? `
        <button class="fg-btn" id="fg-pull-btn" title="Pull current branch"><i data-lucide="arrow-up" class="lucide-icon"></i> Pull</button>
        <button class="fg-btn" id="fg-push-btn" title="Push current branch"><i data-lucide="arrow-down" class="lucide-icon"></i> Push</button>
        <button class="fg-btn" id="fg-merge-btn" title="Merge another branch into the current branch"><i data-lucide="git-merge" class="lucide-icon"></i> Merge…</button>
        ${renderReleaseButton()}
      ` : ''}
      <button class="fg-btn fg-refresh-btn" id="fg-refresh-btn" title="Refresh Git Control — re-check recent changes and reload the commit graph" aria-label="Refresh Git Control"><i data-lucide="refresh-cw" class="lucide-icon"></i></button>
      <div class="fg-merge-menu" id="fg-merge-menu" hidden role="listbox" aria-label="Pick branch to merge in">
        <div class="fg-branch-menu-loading">Loading branches…</div>
      </div>
    </div>
  `;
}

// One shared feedback area follows every commit/sync/release action. Keeping both
// stable result ids here preserves the independent async flows while ensuring all
// progress and errors appear in the same place above the branch picker.
function renderActionResults() {
  return `
    <div class="fg-action-results" aria-live="polite">
      <div id="fg-commit-result" class="fg-result" hidden></div>
      <div id="fg-sync-result" class="fg-result" hidden></div>
    </div>
  `;
}

// The "Release to production" button now lives in the sync row next to Merge —
// the old standalone Production section was removed, so the button alone drives a
// release (trim → copy → commit → push to the production repo). It's a WebAgent-only
// action (production mirrors THIS app's repo), so it hides when another repo is
// active. Its enabled state tracks _prodState.configured once that has loaded; we
// keep it enabled while _prodState is still null (open / first paint) so it's never
// stuck disabled, and the release flow itself reports if no production repo is set.
function renderReleaseButton() {
  return '';
}

function renderGraphSection(s, g, graphErr) {
  // The branch picker rides at the top of the graph section now (see renderBranchBar).
  const branchBar = renderBranchBar(s);
  if (graphErr) {
    return `<div class="fg-section fg-graph">
      ${branchBar}
      <div class="fg-section-title">Commit graph <span class="fg-graph-hint">(all branches)</span></div>
      <div class="fg-graph-list">
        <div class="fg-error fg-click-copy">${_esc(graphErr)} <button class="fg-btn" data-act="retry-graph">Retry</button></div>
      </div>
    </div>`;
  }
  if (!g || !g.commits || !g.commits.length) {
    return `<div class="fg-section fg-graph">
      ${branchBar}
      <div class="fg-section-title">Commit graph</div>
      <div class="fg-loading">Loading…</div>
    </div>`;
  }
  const rows = g.commits.map((c, idx) => renderGraphRow(c, idx, g)).join('');
  return `
    <div class="fg-section fg-graph">
      ${branchBar}
      <div class="fg-section-title">Commit graph <span class="fg-graph-hint">(all branches)</span></div>
      <div class="fg-graph-list" id="fg-graph-list" style="--lane-w:${GRAPH_LANE_W}px; --row-h:${GRAPH_ROW_H}px;">
        ${rows}
      </div>
    </div>
  `;
}

function renderGraphRow(c, idx, g) {
  const baseCls = c.is_head
    ? 'fg-graph-row head'
    : (c.is_pullable
        ? 'fg-graph-row unpulled'
        : (c.is_pulled ? 'fg-graph-row pulled' : 'fg-graph-row other-branch'));
  const activeRepo = _activeRepo();
  const commitKey = (activeRepo?.id || 'webagent') + ':' + c.full_hash;
  const selected = _selectedCommitKey === commitKey;
  // `unpushed` is additive (a commit can be HEAD *and* unpushed) — it drives the
  // dotted connector + green halo painted in paintRow().
  const cls = baseCls + (c.is_unpushed ? ' unpushed' : '') + (selected ? ' selected' : '');
  const badge = c.is_head
    ? '<span class="fg-graph-badge head">HEAD</span>'
    : (c.is_pullable
        ? '<span class="fg-graph-badge unpulled" title="On origin/' + _esc(g.current_branch || 'current branch') + ' but not yet pulled — click Pull to bring it in">↓</span>'
        : '');
  // Branch tip labels — show every branch whose tip is this commit.
  // Skip bare "main" and "origin" since "origin/main" already covers both.
  const branchLabels = (g.branches && Object.keys(g.branches).length)
    ? Object.entries(g.branches)
        .filter(([_, h]) => h === c.hash)
        .filter(([name]) => name !== 'main' && name !== 'origin')
        .map(([name]) => `<span class="fg-branch-tag" title="branch tip">${_esc(name)}</span>`)
        .join('')
    : '';
  // Per-row graph width: only as wide as the lanes this row actually
  // touches, so single-lane rows snap their text right next to the dot
  // and rows with branches push the text out only as far as needed.
  const graphW = rowLaneSpan(c) * GRAPH_LANE_W;
  const shortDate = shortRelativeTime(c.date_iso, c.date_relative);
  return `
    <div class="${cls}" data-hash="${_esc(c.full_hash)}" data-short="${_esc(c.hash)}" data-lane="${c.lane}" data-idx="${idx}" tabindex="0" role="button">
      <div class="fg-graph-row-head">
        <div class="fg-graph-col" style="width:${graphW}px;">
          <svg class="fg-graph-svg" width="${graphW}" height="${GRAPH_ROW_H}" viewBox="0 0 ${graphW} ${GRAPH_ROW_H}" aria-hidden="true"></svg>
        </div>
        <div class="fg-graph-meta">
          <div class="fg-graph-msg-line">
            <span class="fg-graph-msg">${_esc(c.message || '')}</span>
          </div>
          <div class="fg-graph-sub">
            ${badge || branchLabels ? `<span class="fg-graph-tags">${badge}${branchLabels}</span>` : ''}
            <code class="fg-hash">${_esc(c.hash)}</code>
            <span class="fg-graph-date" title="${_esc(c.date_iso || '')} — ${_esc(c.date_relative || '')}">${_esc(shortDate)}</span>
            <span class="fg-graph-author" title="${_esc(c.author || '')}">${_esc(c.author || '')}</span>
          </div>
        </div>
      </div>
    </div>
  `;
}

// ── SVG graph drawing ──────────────────────────────────────────────

function drawGraph(rootEl, g) {
  if (!g) return;
  // The Instances page ("Local changes" card) draws the same graph in a second
  // location, where the list carries the same class but a different id — match
  // either so both call sites share one painter.
  const list = rootEl.querySelector('#fg-graph-list, .fg-graph-list');
  if (!list) return;
  const rows = list.querySelectorAll('.fg-graph-row');
  const commits = g.commits || [];
  rows.forEach((rowEl, i) => {
    const c = commits[i];
    if (!c) return;
    const svg = rowEl.querySelector('.fg-graph-svg');
    if (!svg) return;
    paintRow(svg, c, g.max_lane || 1);
  });
}

// Reused by the Instances page's "Local changes" card (This device Overview) to
// render the commit graph in a second location — see ui/admin-tools/instances/
// instances.js (_gitGraphSectionHtml / _wireGitGraph). Keep these in sync when
// the graph renderer changes.
export { renderGraphRow, drawGraph, GRAPH_LANE_W, GRAPH_ROW_H };

function laneX(i) { return i * GRAPH_LANE_W + GRAPH_LANE_W / 2; }

function paintRow(svg, c, maxLane) {
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  const ns = 'http://www.w3.org/2000/svg';
  const top = 0;
  const mid = GRAPH_ROW_H / 2;
  const bot = GRAPH_ROW_H;
  const lanesIn  = c.lanes_in  || [];
  const lanesOut = c.lanes_out || [];
  const dot = c.lane;
  const parentLanes = c.parent_lanes || [];
  const mergeInLanes = c.merge_in_lanes || [];

  // Helper to add a path with a given stroke colour. `dashed` draws a dotted
  // line — used for an unpushed commit's own connectors, marking the segment of
  // the tree that isn't on the remote yet.
  function line(d, color, dashed) {
    const p = document.createElementNS(ns, 'path');
    p.setAttribute('d', d);
    p.setAttribute('stroke', color);
    p.setAttribute('stroke-width', '1.6');
    p.setAttribute('fill', 'none');
    p.setAttribute('stroke-linecap', 'round');
    if (dashed) p.setAttribute('stroke-dasharray', '2 3');
    svg.appendChild(p);
  }

  // Dot this commit's *own* connectors (incoming child line + outgoing parent
  // lines) when it hasn't been pushed. Pass-through lanes for other branches
  // stay solid — only the unpushed commit's trunk goes dotted.
  const unpushed = !!c.is_unpushed;

  // 1. Pass-through lanes (line in AND line out, not the dot).
  const lanesCount = Math.max(lanesIn.length, lanesOut.length, maxLane);
  for (let i = 0; i < lanesCount; i++) {
    if (i === dot) continue;
    const hasIn  = !!lanesIn[i];
    const hasOut = !!lanesOut[i];
    if (hasIn && hasOut) {
      line(`M ${laneX(i)} ${top} L ${laneX(i)} ${bot}`, laneColor(i));
    } else if (hasIn && !hasOut) {
      // Line came in but didn't continue — only happens for merge_in_lanes
      // (a sibling child's line bending into the dot).
      if (mergeInLanes.includes(i)) {
        // top of lane i → mid, then curve to dot
        line(`M ${laneX(i)} ${top} L ${laneX(i)} ${mid - 6} Q ${laneX(i)} ${mid} ${laneX(i) + (dot > i ? 6 : -6)} ${mid} L ${laneX(dot)} ${mid}`, laneColor(i));
      } else {
        // Defensive: stray inbound line
        line(`M ${laneX(i)} ${top} L ${laneX(i)} ${mid}`, laneColor(i));
      }
    } else if (!hasIn && hasOut) {
      // Outgoing line that didn't come from this row's dot (= a parent
      // placed on a fresh lane that's not in parent_lanes? shouldn't
      // happen, but draw mid→bottom defensively).
      if (parentLanes.includes(i)) {
        // bend from dot center down to lane i, then to bottom
        const x1 = laneX(dot);
        const x2 = laneX(i);
        const yBend = mid + 6;
        line(`M ${x1} ${mid} L ${x2 + (i > dot ? -6 : 6)} ${mid} Q ${x2} ${mid} ${x2} ${yBend} L ${x2} ${bot}`, laneColor(i));
      } else {
        line(`M ${laneX(i)} ${mid} L ${laneX(i)} ${bot}`, laneColor(i));
      }
    }
  }

  // 2. Dot lane: incoming top→mid if a child placed our hash here.
  const childAbove = !!lanesIn[dot];
  if (childAbove) {
    line(`M ${laneX(dot)} ${top} L ${laneX(dot)} ${mid}`, laneColor(dot), unpushed);
  }

  // 3. Outgoing lines: from dot center to each parent's lane at bottom.
  parentLanes.forEach((pl) => {
    if (pl === dot) {
      // Straight down — same lane
      line(`M ${laneX(dot)} ${mid} L ${laneX(dot)} ${bot}`, laneColor(dot), unpushed);
    } else {
      // Bend horizontally from dot mid out to parent lane, then down.
      const xD = laneX(dot);
      const xP = laneX(pl);
      const yBend = mid + 6;
      const adj  = (pl > dot) ? -6 : 6;
      line(`M ${xD} ${mid} L ${xP + adj} ${mid} Q ${xP} ${mid} ${xP} ${yBend} L ${xP} ${bot}`, laneColor(pl), unpushed);
    }
  });

  // 4. Green "unpushed" halo — a bubble around commits committed locally but not
  // yet on the remote, so they pop out of pushed history. Drawn behind the dot.
  if (unpushed) {
    const halo = document.createElementNS(ns, 'circle');
    halo.setAttribute('cx', laneX(dot));
    halo.setAttribute('cy', mid);
    halo.setAttribute('r', GRAPH_DOT_R + 3);
    halo.setAttribute('fill', 'rgba(var(--success-rgb), 0.22)');
    halo.setAttribute('stroke', 'var(--success)');
    halo.setAttribute('stroke-width', '1.5');
    svg.appendChild(halo);
  }

  // 5. The commit dot itself.
  const circle = document.createElementNS(ns, 'circle');
  circle.setAttribute('cx', laneX(dot));
  circle.setAttribute('cy', mid);
  circle.setAttribute('r', GRAPH_DOT_R);
  circle.setAttribute('fill', laneColor(dot));
  if (c.is_head) {
    circle.setAttribute('stroke', 'var(--files-text-strong)');
    circle.setAttribute('stroke-width', '2');
  }
  svg.appendChild(circle);
}

// ── Wire events ────────────────────────────────────────────────────

function wireEvents(rootEl, s, g) {
  const body = rootEl.querySelector('#fg-body');
  if (!body) return;

  wireRepoSelector(rootEl);

  const commitBtn = body.querySelector('#fg-commit-btn');
  if (commitBtn) commitBtn.addEventListener('click', () => doCommit(rootEl));

  const scStarBtn = body.querySelector('#fg-sc-star-btn');
  if (scStarBtn) scStarBtn.addEventListener('click', () => handoffToSourceController(rootEl));

  const pushBtn = body.querySelector('#fg-push-btn');
  if (pushBtn) pushBtn.addEventListener('click', () => doPush(rootEl));
  const pullBtn = body.querySelector('#fg-pull-btn');
  if (pullBtn) pullBtn.addEventListener('click', () => doPull(rootEl));
  const mergeBtn = body.querySelector('#fg-merge-btn');
  if (mergeBtn) mergeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleMergeMenu(rootEl);
  });
  const refreshBtn = body.querySelector('#fg-refresh-btn');
  if (refreshBtn) refreshBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    // Re-read LOCAL changes + commit graph and repaint only this sidebar —
    // fast, no blocking network round-trip. refreshGit({ remote: false }) does
    // an instant local-only read (fetch=0) first, paints, THEN quietly catches
    // up ahead/behind with a background `git fetch` that never blocks the UI.
    // (We deliberately don't use { remote: true } here: it forces two
    // sequential 20s-capped `git fetch` calls — including a heavy `--all` over
    // every remote branch — before anything paints, which made the button feel
    // slow.) The spinner clears automatically when refreshGit() repaints the
    // panel with a fresh button.
    refreshBtn.classList.add('is-spinning');
    refreshGit(rootEl, { remote: false });
  });

  // ── Release to production (sync row, next to Merge) ──
  const prodReleaseBtn = body.querySelector('#fg-prod-release-btn');
  if (prodReleaseBtn) prodReleaseBtn.addEventListener('click', () => doReleaseToProduction(rootEl));

  // Retry graph button — refetches only the graph, preserving status
  const retryGraphBtn = body.querySelector('[data-act="retry-graph"]');
  if (retryGraphBtn) retryGraphBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    retryGraphBtn.disabled = true;
    retryGraphBtn.textContent = 'Loading…';
    try {
      _state.graph = await ghFetch('/api/v1/github/log-graph?limit=80');
      _state.graphErr = null;
    } catch (err) {
      _state.graphErr = err.message || 'Failed to load commit graph';
    }
    renderGitPanel(rootEl);
  });
  const branchTrig = body.querySelector('#fg-branch-picker-trigger');
  if (branchTrig) branchTrig.addEventListener('click', (e) => {
    e.stopPropagation();
    toggleBranchMenu(rootEl);
  });

  // Close any open picker menu when clicking outside
  if (!body.__fgMenuCloser) {
    body.__fgMenuCloser = (ev) => {
      const inMerge = ev.target.closest('#fg-merge-menu') || ev.target.closest('#fg-merge-btn');
      const inBranch = ev.target.closest('#fg-branch-menu') || ev.target.closest('#fg-branch-picker-trigger');
      if (!inMerge) {
        const m = body.querySelector('#fg-merge-menu');
        if (m) m.hidden = true;
      }
      if (!inBranch) {
        const b = body.querySelector('#fg-branch-menu');
        if (b) b.hidden = true;
      }
    };
    document.addEventListener('click', body.__fgMenuCloser);
  }

  // Commit graph rows open repository-aware detail tabs in File Manager.
  // The row stays selected while its corresponding tab is active/opened.
  body.querySelectorAll('.fg-graph-row').forEach((row) => {
    const select = () => {
      const hash = row.dataset.hash || row.dataset.short;
      if (!hash) return;
      body.querySelectorAll('.fg-graph-row.selected').forEach((r) => r.classList.remove('selected'));
      const repo = _activeRepo();
      const commit = (g?.commits || []).find((item) => item.full_hash === hash || item.hash === hash) || {};
      setSelectedGitCommit(repo?.id || 'webagent', hash);
      row.classList.add('selected');
      if (typeof _commitSelectionHandler === 'function') {
        _commitSelectionHandler({
          repoId: repo?.id || 'webagent',
          repoLabel: repo?.label || 'WebAgent (this app)',
          hash,
          shortHash: row.dataset.short || hash.slice(0, 7),
          subject: commit.message || '',
        });
      }
    };
    const head = row.querySelector('.fg-graph-row-head');
    if (head) head.addEventListener('click', select);
    row.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); select(); }
    });
  });

  // Click a commit hash to copy it to clipboard.
  body.querySelectorAll('.fg-hash').forEach((el) => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const text = el.textContent.trim();
      copyText(text).catch(() => {});
    });
  });

  // Click an error/warning result line to copy its text to clipboard.
  body.addEventListener('click', (e) => {
    const copyEl = e.target.closest('.fg-click-copy');
    if (!copyEl) return;
    const text = copyEl.textContent.trim();
    if (text) {
      copyText(text).catch(() => {});
      // Brief visual feedback — dim flash
      copyEl.style.transition = 'opacity 0.15s';
      copyEl.style.opacity = '0.5';
      setTimeout(() => { try { copyEl.style.opacity = ''; } catch (_) {} }, 200);
    }
  });

  // (The remote-URL and GitHub-token rows that used to live here were removed —
  // both are now edited from the repo selector's Edit form.)
}

export async function loadCommitDetail(hash) {
  return ghFetch(`/api/v1/github/commit/${encodeURIComponent(hash)}`);
}

export function renderCommitDetail(d) {
  const files = (d.files || []).map(f => {
    const added = f.binary ? '—' : `+${f.added}`;
    const removed = f.binary ? '—' : `-${f.removed}`;
    return `<div class="fg-cd-file"><span class="fg-cd-added">${_esc(added)}</span><span class="fg-cd-removed">${_esc(removed)}</span><span class="fg-cd-path">${_esc(f.path)}</span></div>`;
  }).join('') || '<div class="fg-loading">No file changes</div>';
  const body = (d.body || '').trim() ? `<pre class="fg-cd-body">${_esc(d.body.trim())}</pre>` : '';
  const parents = (d.parents || []).map(p => _esc(p.slice(0, 7))).join(', ') || '(none)';
  const subject = (d.subject || '').trim() ? `<div class="fg-cd-subject">${_esc(d.subject)}</div>` : '';
  return `
    <div class="fg-cd">
      <div class="fg-cd-fields">
        <div class="fg-cd-field"><span class="fg-cd-label">Full hash</span><code class="fg-cd-val">${_esc(d.full_hash || '')}</code></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Author</span><span class="fg-cd-val">${_esc(d.author || '')}${d.author_email ? ' &lt;' + _esc(d.author_email) + '&gt;' : ''}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Authored</span><span class="fg-cd-val">${_esc(d.author_date || '')}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Committer</span><span class="fg-cd-val">${_esc(d.committer || '')}${d.committer_email ? ' &lt;' + _esc(d.committer_email) + '&gt;' : ''}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Committed</span><span class="fg-cd-val">${_esc(d.commit_date || '')}</span></div>
        <div class="fg-cd-field"><span class="fg-cd-label">Parents</span><code class="fg-cd-val">${parents}</code></div>
      </div>
      ${subject}
      ${body}
      <div class="fg-cd-files-title">Files (${(d.files || []).length})</div>
      <div class="fg-cd-files">${files}</div>
    </div>
  `;
}

// ── Branch picker / merge picker ───────────────────────────────────

async function fetchBranches() {
  return ghFetch('/api/v1/github/branches');
}

function renderBranchMenu(menu, data, opts) {
  const { current, branches } = data;
  const { mode, onPick } = opts; // mode: 'switch' | 'merge'
  const items = (branches || []).filter(b => {
    if (mode === 'merge') return b.name !== current;
    return true;
  });
  if (!items.length) {
    menu.innerHTML = '<div class="fg-branch-menu-empty">No other branches</div>';
    return;
  }
  menu.innerHTML = items.map((b) => {
    const isCurrent = b.name === current;
    const tag = b.local && b.remote
      ? '<span class="fg-branch-tag-local" title="local + remote">local</span>'
      : (b.local
          ? '<span class="fg-branch-tag-local" title="local only">local only</span>'
          : '<span class="fg-branch-tag-remote" title="remote only">remote</span>');
    const cur = isCurrent ? '<span class="fg-branch-tag-cur">current</span>' : '';
    return `
      <button type="button" class="fg-branch-item${isCurrent ? ' is-current' : ''}" data-branch="${_esc(b.name)}" ${isCurrent && mode === 'switch' ? 'disabled' : ''}>
        <span class="fg-branch-item-name">${_esc(b.name)}</span>
        <span class="fg-branch-item-meta">${cur}${tag}<code>${_esc(b.hash || '')}</code></span>
      </button>
    `;
  }).join('');
  menu.querySelectorAll('.fg-branch-item').forEach((btn) => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const name = btn.dataset.branch;
      if (!name) return;
      menu.hidden = true;
      onPick(name);
    });
  });
}

async function toggleBranchMenu(rootEl) {
  const menu = rootEl.querySelector('#fg-branch-menu');
  if (!menu) return;
  // Close the other menu if open
  const otherMenu = rootEl.querySelector('#fg-merge-menu');
  if (otherMenu) otherMenu.hidden = true;
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.hidden = false;
  menu.innerHTML = '<div class="fg-branch-menu-loading">Loading branches…</div>';
  try {
    const data = await fetchBranches();
    renderBranchMenu(menu, data, {
      mode: 'switch',
      onPick: (name) => doCheckout(rootEl, name),
    });
  } catch (e) {
    menu.innerHTML = `<div class="fg-branch-menu-err">${_esc(e.message)}</div>`;
  }
}

async function toggleMergeMenu(rootEl) {
  const menu = rootEl.querySelector('#fg-merge-menu');
  if (!menu) return;
  const otherMenu = rootEl.querySelector('#fg-branch-menu');
  if (otherMenu) otherMenu.hidden = true;
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.hidden = false;
  menu.innerHTML = '<div class="fg-branch-menu-loading">Loading branches…</div>';
  try {
    const data = await fetchBranches();
    renderBranchMenu(menu, data, {
      mode: 'merge',
      onPick: (name) => doMerge(rootEl, name),
    });
  } catch (e) {
    menu.innerHTML = `<div class="fg-branch-menu-err">${_esc(e.message)}</div>`;
  }
}

// ── Actions ────────────────────────────────────────────────────────

function showResult(el, msg, type, opts = {}) {
  if (!el) return;
  el.hidden = false;
  el.classList.remove('ok', 'err', 'info', 'warn');
  el.classList.add(
    type === 'error' ? 'err'
    : type === 'success' ? 'ok'
    : type === 'warning' ? 'warn'
    : 'info');
  // Make error/warning messages clickable to copy to clipboard
  const isCopyable = type === 'error' || type === 'warning' || type === 'success';
  el.classList.toggle('fg-click-copy', isCopyable);
  // Rebuild the content so an optional leading spinner can sit before the text
  // (the live commit/push step line). textContent for the message keeps it
  // injection-safe.
  el.textContent = '';
  if (opts.spin) {
    const s = document.createElement('span');
    s.className = 'fg-result-spin';
    el.appendChild(s);
  }
  el.appendChild(document.createTextNode(msg));
  if (opts.onCancel) {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'fg-result-cancel';
    cancel.textContent = 'Cancel';
    cancel.setAttribute('aria-label', 'Cancel commit');
    cancel.addEventListener('click', opts.onCancel, { once: true });
    el.appendChild(cancel);
  }
}

// ── Live commit/push progress (⭐ and Commit buttons) ──────────────
//
// The server streams granular phase events as NDJSON from
// POST /api/v1/github/commit-and-push (app/api/github._commit_and_push_events).
// We surface each as a step in the result line (with a spinner), put a spinner
// over the clicked button, and on success flash a green check on it — so the user
// gets feedback the whole way instead of one long silent wait.

// phase key → user-facing step label (the backend's keys are deliberately
// generic; the wording lives here).
const _COMMIT_PHASE_LABELS = {
  analyzing:  'Analyzing changes...',
  scanning:   'Safety-checking the diff...',
  committing: 'Committing changes...',
  pushing:    'Pushing to remote...',
};

// POST commit-and-push as a live NDJSON stream; calls onEvent(evt) per event.
// Degrades gracefully: a backend that ignores `stream` (e.g. not yet restarted)
// answers with a single JSON object, which we deliver as one
// {phase:'done', result:<obj>} so callers see one consistent shape either way.
async function cancelCommitOperation(operationId) {
  if (!operationId) return;
  await ghFetch('/api/v1/github/commit-and-push/' + encodeURIComponent(operationId) + '/cancel', {
    method: 'POST',
  }).catch(() => {});
}

async function recoverCommitOperation(operationId, onEvent, signal, originalError) {
  if (!operationId) throw originalError || new Error('The Git response ended before a result was received.');
  onEvent({ phase: 'reconnecting' });
  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    if (signal && signal.aborted) {
      await cancelCommitOperation(operationId);
      throw originalError || new Error('Commit cancelled.');
    }
    try {
      const state = await ghFetch('/api/v1/github/commit-and-push/' + encodeURIComponent(operationId) + '/status');
      if (state && state.status === 'done') {
        onEvent({ phase: 'done', result: state.result || {
          status: 'error', message: 'Commit operation ended without a result.'
        }});
        return;
      }
    } catch (_) {
      // The connection may still be returning; retry until the bounded deadline.
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }
  throw new Error('Connection interrupted; the Git operation is still running. Refresh Git Control to check its result.');
}

async function streamCommitPush(payload, onEvent, signal) {
  const repo = selectedRepo();
  const status = await v3Files({action:'git_status', repo});
  lastGitStatus = status;
  const paths = [...new Set([...(status.staged || []), ...(status.unstaged || []),
    ...(status.untracked || [])].map(item => item.path))];
  if (!paths.length) {
    onEvent({phase:'done', result:{status:'nothing_to_commit', message:'Nothing to commit.'}});
    return;
  }
  if (signal?.aborted) throw Error('Commit was cancelled before it started');
  onEvent({phase:'analyzing'});
  let message = String(payload.message || '').trim();
  if (!message) {
    message = 'Update ' + paths.slice(0, 3).join(', ') + (paths.length > 3 ? ' and more' : '');
    onEvent({phase:'message_fallback'});
  }
  onEvent({phase:'scanning'});
  onEvent({phase:'committing'});
  const committed = await v3Files({action:'git_commit', repo, paths, message,
    expected_head:status.head, expected_review_sha256:status.review_sha256});
  lastGitStatus = null;
  let push = {attempted:false};
  if (!payload.skip_push) {
    onEvent({phase:'pushing'});
    try {
      const current = await v3Files({action:'git_status', repo});
      await v3Files({action:'git_tool', repo, operation:'push', expected_head:current.head});
      push = {attempted:true, ok:true};
    } catch (error) {
      push = {attempted:true, ok:false, detail:error.message};
    }
  }
  onEvent({phase:'done', result:{...committed, push}});
}

// Render one streamed phase event into the status line. Goes through the sticky
// helper so the step label survives any repaint that happens mid-stream (e.g. the
// instant local graph redraw fired when the commit lands and the push begins).
function onCommitPhase(rootEl, ev, progressOpts = { spin: true }) {
  const phase = ev.phase;
  const set = (msg, tone) => setStickyResult(rootEl, 'fg-commit-result', msg, tone, progressOpts);
  if (phase === 'message') {
    set(ev.source === 'user' ? 'Using your message...' : 'Writing a commit message...', 'info');
    return;
  }
  if (phase === 'reconnecting') {
    set('Connection interrupted — checking the Git operation...', 'warning');
    return;
  }
  if (phase === 'message_ready') {
    set(ev.title ? `Message: "${ev.title}"` : 'Message ready', 'info');
    return;
  }
  if (phase === 'message_fallback') {
    // Non-fatal: the AI auto-writer was unavailable, so a deterministic file-list
    // summary was used instead — surfaced so the user knows why the note is plain.
    set('AI message writer unavailable - using a file-list summary', 'warning');
    return;
  }
  const label = _COMMIT_PHASE_LABELS[phase];
  if (label) set(label, 'info');
}

// Flash the success check on the (freshly repainted) action button, then clear.
function flashBtnDone(rootEl, btnId) {
  const btn = rootEl.querySelector(btnId);
  if (!btn) return;
  btn.classList.remove('is-working');
  btn.classList.add('is-done');
  setTimeout(() => { try { btn.classList.remove('is-done'); } catch (_) {} }, 1600);
}

// ── Production mirror: release ──────────────────────────────────────

// phase key → user-facing step label for the production streams.
// Exported so the File Explorer's production menu (Copy / Push) can label the
// SAME trim→copy→commit→push engine the Git page's "Release" button drives.
// The split Copy and Push paths reuse this set: Copy ends at "committing",
// Push only ever emits "pushing".
export const _RELEASE_PHASE_LABELS = {
  preparing:  'Preparing…',
  scanning:   'Listing project files…',
  copying:    'Building the trimmed copy…',
  safety:     'Safety-checking for secrets…',
  message:    'Writing a release message…',
  message_fallback: 'AI message writer unavailable — using a file-list summary',
  committing: 'Committing the mirror…',
  pushing:    'Pushing to GitHub…',
};

// Internal: POST a production action as a live NDJSON stream and call
// onEvent(evt) per event, degrading to one {phase:'done'} if not streamed.
// Shared by streamRelease / streamCopy / streamPush so the three entry points
// can never drift in their stream handling.
async function _streamProd(endpoint, payload, onEvent) {
  const res = await fetch(apiPath(endpoint), {
    method: 'POST',
    headers: Object.assign({ 'Content-Type': 'application/json' }, authHeaders()),
    body: JSON.stringify(Object.assign({ stream: true }, payload)),
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { const j = await res.json(); detail = j.detail || detail; }
    catch (_) { try { detail = await res.text(); } catch (_) {} }
    throw new Error(detail || ('HTTP ' + res.status));
  }
  const ctype = res.headers.get('content-type') || '';
  if (!ctype.includes('ndjson') || !res.body || !res.body.getReader) {
    const obj = await res.json();
    onEvent({ phase: 'done', result: obj });
    return;
  }
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = '';
  const drain = (chunk) => {
    buf += chunk;
    let nl;
    while ((nl = buf.indexOf('\n')) >= 0) {
      const line = buf.slice(0, nl).trim();
      buf = buf.slice(nl + 1);
      if (line) { try { onEvent(JSON.parse(line)); } catch (_) {} }
    }
  };
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    drain(dec.decode(value, { stream: true }));
  }
  drain(dec.decode());
  const tail = buf.trim();
  if (tail) { try { onEvent(JSON.parse(tail)); } catch (_) {} }
}

// Full one-click release (copy + push). Used by the Git page's Release button.
export async function streamRelease(payload, onEvent) {
  return _streamProd('/api/v1/github/production/release', payload || {}, onEvent);
}

// Copy-only: build the trimmed tree in the production folder + commit locally
// (no push). Used by the File Explorer's "Copy to production" menu item.
export async function streamCopy(payload, onEvent) {
  return _streamProd('/api/v1/github/production/copy', payload || {}, onEvent);
}

// Push-only: send the production folder's local commit to GitHub. Used by the
// File Explorer's "Push to GitHub" menu item.
export async function streamPush(payload, onEvent) {
  return _streamProd('/api/v1/github/production/push', payload || {}, onEvent);
}

// Fetch the production status into _prodState and repaint the panel. Called on
// open, after a release, and on the Production refresh button — never per poll.
async function loadProdState(rootEl) {
  try {
    _prodState = await ghFetch('/api/v1/github/production/status');
  } catch (e) {
    _prodState = { configured: false, error: e.message || 'failed' };
  }
  try { if (rootEl) renderGitPanel(rootEl); } catch (_) {}
}

// The Release button: stream the trim→copy→commit→push, showing each step with a
// spinner over the button (pinned across repaints), then flash a check / report.
async function doReleaseToProduction(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const btnId = '#fg-prod-release-btn';
  const btn = body ? body.querySelector(btnId) : null;
  if (btn && btn.disabled) return;

  if (!confirm('Release a trimmed copy to the production repo and push it to GitHub now?')) return;

  _workingBtnId = btnId;
  if (btn) { btn.disabled = true; btn.classList.add('is-working'); }
  // The release note field went away with the Production section, so the note is
  // always auto-written from the diff. Progress + outcome show on the shared sync
  // result line (the release button lives in the sync row now).
  setStickyResult(rootEl, 'fg-sync-result', 'Starting release…', 'info', { spin: true });

  let result = null;
  try {
    await streamRelease({ message: '' }, (ev) => {
      if (ev.phase === 'done') { result = ev.result || {}; return; }
      if (ev.phase === 'message_ready') {
        // Show the AI-written title (mirrors the ⭐ commit&push step display).
        setStickyResult(rootEl, 'fg-sync-result',
          ev.title ? `Message: "${ev.title}"` : 'Message ready', 'info', { spin: true });
        return;
      }
      const label = _RELEASE_PHASE_LABELS[ev.phase];
      if (label) setStickyResult(rootEl, 'fg-sync-result', label, 'info', { spin: true });
    });
  } catch (e) {
    result = { status: 'error', message: e.message || 'release failed' };
  }

  _workingBtnId = null;
  const r = result || { status: 'error', message: 'no result' };
  const tone = r.status === 'released' ? 'success'
    : r.status === 'nothing' ? 'info'
    : (r.status === 'blocked' || r.status === 'error') ? 'error' : 'info';
  setStickyResult(rootEl, 'fg-sync-result', r.message || r.status || 'done', tone);
  setTimeout(() => clearStickyResult('fg-sync-result'), 8000);

  const liveBtn = rootEl.querySelector(btnId);
  if (liveBtn) { liveBtn.classList.remove('is-working'); liveBtn.disabled = false; }
  if (r.status === 'released') flashBtnDone(rootEl, btnId);
  await loadProdState(rootEl);
}

// Shared driver for both commit buttons (⭐ commit+push, and plain Commit with
// skipPush). Streams the work, shows each step with a spinner over the button,
// then repaints the panel and flashes a check on success.
async function runCommitFlow(rootEl, { skipPush } = {}) {
  const body = rootEl.querySelector('#fg-body');
  const btnId = skipPush ? '#fg-commit-btn' : '#fg-sc-star-btn';
  const btn = body ? body.querySelector(btnId) : null;
  const msgInput = body ? body.querySelector('#fg-commit-msg') : null;
  const typedNote = ((msgInput || {}).value || '').trim();
  if (_commitAbortController) return;

  const controller = new AbortController();
  _commitAbortController = controller;
  const progressOpts = { spin: true };

  // Pin the button's working state and the status line so neither blanks out when
  // the panel repaints mid-flow (the mid-push local graph redraw, the auto-poll, or
  // the final refresh). Both are released once everything has settled below.
  _workingBtnId = btnId;
  if (btn) { btn.disabled = true; btn.classList.add('is-working'); }
  setStickyResult(rootEl, 'fg-commit-result', 'Starting...', 'info', progressOpts);

  let final = null, usedFallback = false, localShown = false;
  try {
    await streamCommitPush({ message: typedNote, skip_push: !!skipPush }, (ev) => {
      if (ev.phase === 'done') { final = ev.result || null; return; }
      if (ev.phase === 'message_fallback') usedFallback = true;
      onCommitPhase(rootEl, ev, progressOpts);
      // The commit has ALREADY landed by the time the server reports it's pushing —
      // so repaint the graph from a fast local read now: the new commit appears
      // immediately (unpushed / ahead of the remote) while the push runs, instead
      // of the graph sitting stale until the whole push finishes. Fire-and-forget
      // so we don't stall reading the rest of the stream; the sticky line + working
      // button are re-applied by the repaint.
      if (ev.phase === 'pushing' && !localShown) {
        localShown = true;
        refreshGit(rootEl, { localOnly: true }).catch(() => {});
      }
    }, controller.signal);
  } catch (e) {
    _commitAbortController = null;
    _workingBtnId = null;
    const cancelled = controller.signal.aborted;
    setStickyResult(rootEl, 'fg-commit-result',
      cancelled ? 'Commit cancelled.' : `Error: ${e.message}`,
      cancelled ? 'info' : 'error');
    const liveBtn = rootEl.querySelector(btnId);
    if (liveBtn) { liveBtn.disabled = false; liveBtn.classList.remove('is-working'); }
    return;
  }

  _commitAbortController = null;

  if (!final) {
    _workingBtnId = null;
    setStickyResult(rootEl, 'fg-commit-result', 'Error: the server sent no result.', 'error');
    if (btn) { btn.disabled = false; btn.classList.remove('is-working'); }
    return;
  }

  let outMsg, outTone, ok = false;
  if (final.status === 'nothing_to_commit') {
    outMsg = final.message || 'Nothing to commit.'; outTone = 'info';
  } else if (final.status === 'blocked') {
    outMsg = final.message || 'Commit blocked by safety checks.'; outTone = 'error';
  } else if (final.status === 'error') {
    outMsg = 'Error: ' + (final.message || 'git operation failed'); outTone = 'error';
  } else {
    const push = final.push || {};
    const pushTxt = !push.attempted
      ? 'committed (not pushed)'
      : (push.ok ? 'committed & pushed' : 'committed, but PUSH FAILED: ' + (push.detail || ''));
    outTone = (push.attempted && !push.ok) ? 'error' : 'success';
    ok = outTone === 'success';
    outMsg = `✓ ${final.title} (${final.hash}) - ${pushTxt}`;
    if (usedFallback) outMsg += ' | note auto-written from the file list (AI writer unavailable)';
  }

  // Pin the outcome (sticky, so the repaints below never blank it) and clear the
  // note box. The button's working spinner is no longer wanted — the flash takes
  // over once the very last update lands.
  setStickyResult(rootEl, 'fg-commit-result', outMsg, outTone);
  if (msgInput && final.status === 'committed') msgInput.value = '';
  _workingBtnId = null;

  // 1. Instant LOCAL repaint: the Changes list empties and the new commit shows on
  //    the graph right away — no waiting on the network.
  await refreshGit(rootEl, { localOnly: true });
  // 2. Then reconcile origin ahead/behind with a live fetch. This is the slow part,
  //    but the graph + status line are already up and stay up (sticky), so it no
  //    longer looks like a hang — the ahead/behind indicator just settles when it
  //    lands. Awaited so we release the line and flash the check only at the very
  //    end, exactly when this final graph update has painted.
  await refreshGit(rootEl, { remote: true }).catch(() => {});
  clearStickyResult('fg-commit-result');
  showResult(rootEl.querySelector('#fg-commit-result'), outMsg, outTone);
  if (ok) flashBtnDone(rootEl, btnId);
}

// Commit-only (no push). Same one-shot core as the ⭐ — stages everything,
// safety-checks, and auto-writes the note when the box is blank — but holds the
// push (skip_push). Routes through runCommitFlow so it gets the identical live
// step display, button spinner and success check; the fresh commit then shows on
// the graph as unpushed (green halo + dotted line) until the user pushes.
async function doCommit(rootEl) {
  return runCommitFlow(rootEl, { skipPush: true });
}

async function doPush(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, 'Pushing…', 'info');
  try {
    const r = await ghFetch('/api/v1/github/push', { method: 'POST' });
    showResult(result, r.message || 'Push successful', 'success');
    await refreshGit(rootEl, { remote: true });
  } catch (e) {
    showResult(result, `Push failed: ${e.message}`, 'error');
  }
}

async function doPull(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, 'Pulling…', 'info');
  let r;
  try {
    r = await ghFetch('/api/v1/github/pull', { method: 'POST' });
  } catch (e) {
    showResult(result, `Pull failed: ${e.message}`, 'error');
    return;
  }
  await applyPostSync(rootEl, result, r, 'Pulled');
}

async function doMerge(rootEl, branch) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, `Merging ${branch}…`, 'info');
  let r;
  try {
    r = await ghFetch('/api/v1/github/merge', {
      method: 'POST',
      body: JSON.stringify({ branch }),
    });
  } catch (e) {
    showResult(result, `Merge failed: ${e.message}`, 'error');
    return;
  }
  await applyPostSync(rootEl, result, r, `Merged ${branch}`);
}

async function doCheckout(rootEl, branch) {
  const body = rootEl.querySelector('#fg-body');
  const result = body.querySelector('#fg-sync-result');
  showResult(result, `Switching to ${branch}…`, 'info');
  let r;
  try {
    r = await ghFetch('/api/v1/github/checkout', {
      method: 'POST',
      body: JSON.stringify({ branch }),
    });
  } catch (e) {
    showResult(result, `Checkout failed: ${e.message}`, 'error');
    return;
  }
  await applyPostSync(rootEl, result, r, `Switched to ${branch}`);
}

// Restart the server, wait for /health to come back, then hard-reload
// the page. Used by the refresh buttons in the Git Control and
// terminal sidebars. Safe to call without confirmation — the caller
// is responsible for that.
export async function restartServerAndReload({ timeoutMs = 60000 } = {}) {
  try {
    await fetch(apiPath('/api/v1/restart'), { method: 'POST', headers: authHeaders() });
  } catch (_) { /* expected: server cut the connection */ }
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    await new Promise(r => setTimeout(r, 1000));
    try {
      const h = await fetch(apiPath('/health'));
      if (h.ok) {
        window.location.reload();
        return true;
      }
    } catch (_) {}
  }
  return false;
}

// Shared follow-through for any sync action (pull / merge / checkout).
// Always just refreshes the sidebar — no whole-app reload, no automatic
// server restart. If the response indicates backend code came in we
// surface a hint so the user can restart manually when convenient.
async function applyPostSync(rootEl, resultEl, response, doneLabel) {
  const summary = response.message || `${doneLabel} successful`;
  const hint = response.backend_changed
    ? ' (backend code changed — restart server when convenient)'
    : '';
  showResult(resultEl, summary + hint, 'success');
  await refreshGit(rootEl, { remote: true });
}

// (doSaveToken / refreshTokenStatus were removed with the inline token row — the
//  GitHub key is now saved through the repo selector's Edit form, which posts to
//  POST /api/v1/github/repos/{id} and, for the built-in repo, writes the same
//  shared provider.json key the old inline field did.)

// ── Git Control ⭐: instant one-shot commit + push ───────────────
//
// Clicking the star runs the whole commit+push job IMMEDIATELY on the server in
// a single request — no chat agent, no waiting through turns. The backend
// (POST /api/v1/github/commit-and-push → app/api/github._commit_and_push_events)
// stages everything, auto-writes a commit note from the diff (unless the user
// typed one in the box), runs a secret-scan safety check, then commits and
// pushes — STREAMING a progress event per phase so runCommitFlow can show live
// status (spinner over the button + stepped status line + success check). The
// same core helper backs the Git Control ability's `commit_and_push` agent tool,
// so the button and the agent behave identically. The function name is kept
// (handoffToSourceController) so wireEvents() stays untouched.
async function handoffToSourceController(rootEl) {
  return runCommitFlow(rootEl, { skipPush: false });
}

// ── Refresh / open hooks ───────────────────────────────────────────

// Load the git status, then the graph, painting after each. `remote` controls
// whether the backend does a live `git fetch` first (slow, accurate) or serves
// last-known local refs (instant).
//
// The two reads run SEQUENTIALLY on purpose: status carries the header + changes
// list (the most-used part, and the cheaper query) and paints the moment it
// lands, before the graph is even requested; the graph — a heavier all-refs walk
// — then fills into its own "Loading…" slot. We don't fire them together because
// the backend serializes git calls anyway (the async handlers run blocking
// subprocesses, so they can't truly overlap), and parallel requests would let the
// heavier graph block the status response — making both appear together. A graph
// failure doesn't take down the panel.
async function _loadGit(rootEl, remote, waitForFresh = false) {
  const remoteArgs = waitForFresh ? '?wait=1' : '';
  const statusUrl = '/api/v1/github/status' + (remote ? remoteArgs : '?fetch=0');
  const graphUrl = '/api/v1/github/log-graph?limit=80' +
    (remote ? (waitForFresh ? '&wait=1' : '') : '&fetch=0');

  // Clear the loading flag on the first paint so renderGitPanel shows the
  // error+retry state (not a stuck "Loading…") if a read failed with no cached
  // data to fall back on.
  const paint = () => {
    _state.loading = false;
    // Keep the auto-refresh signature in lock-step with what we just painted,
    // so a manual refresh / commit / push doesn't make the next poll tick
    // repaint identical data (which would steal focus or flicker).
    _autoSig = _gitSignature(_state.status, _state.graph);
    renderGitPanel(rootEl);
  };

  // 1. Status first → paint the changes list immediately.
  try {
    _state.status = await ghFetch(statusUrl);
    _state.err = null;
  } catch (e) {
    // Keep any existing status so a failed refresh doesn't wipe a good view.
    _state.err = e?.message || 'Failed to load';
  }
  paint();

  // 2. Graph next → fills into its own slot when the heavier walk returns.
  try {
    _state.graph = await ghFetch(graphUrl);
    _state.graphErr = null;
    paint();
  } catch (e) {
    // Keep any existing graph so a failed refresh doesn't wipe a good view.
    if (!_state.graph) { _state.graphErr = e?.message || 'Failed to load commit graph'; paint(); }
  }
}

async function _guardRemoteRefresh(operation) {
  if (_remoteRefreshPromise) return _remoteRefreshPromise;
  _remoteRefreshPromise = Promise.resolve().then(operation);
  try {
    return await _remoteRefreshPromise;
  } finally {
    _remoteRefreshPromise = null;
  }
}

// Refresh the git sidebar. Panel-open is local-only; a live remote fetch happens
// only after an explicit action or the bounded low-frequency background poll.
async function refreshGit(rootEl, { remote = false, localOnly = false } = {}) {
  if (!rootEl) return;
  _state.loading = true;
  _state.err = null;
  _state.graphErr = null;
  // If we have no data yet, show the skeleton (structure + live buttons + shimmer);
  // otherwise keep the current panel painted and let the refresh repaint it in place.
  const body = rootEl.querySelector('#fg-body');
  if (body && !_state.status) {
    renderGitSkeleton(rootEl);
  }

  if (localOnly) {
    // Fast LOCAL-only read, no network and no background remote sync. Used right
    // after a commit so the Changes list + commit graph repaint INSTANTLY (the new
    // commit shows as unpushed / ahead) without waiting on a live `git fetch`.
    await _loadGit(rootEl, false);
    _state.loading = false;
    return;
  }

  if (remote) {
    // Explicit refresh waits for fresh data, with one in-flight request globally.
    await _guardRemoteRefresh(() => _loadGit(rootEl, true, true));
    _state.loading = false;
    return;
  }

  // Open/lazy-load: cached/local state only. Never trigger remote I/O here.
  await _loadGit(rootEl, false);
  _state.loading = false;
}

export async function openGitPanel(rootEl, { onCommitSelected } = {}) {
  if (!rootEl) return;
  if (typeof onCommitSelected === 'function') _commitSelectionHandler = onCommitSelected;
  // Paint the full panel skeleton right now — repo selector, commit box and every
  // action button appear (and work) on the first frame, with a shimmer over the
  // Changes list + commit graph until their data lands. See renderGitSkeleton.
  renderGitSkeleton(rootEl);
  // Load the repo list (cheap; reads the registry + the active repo's origin) then
  // repaint the selector with the real active repo — unless status already landed.
  await loadRepos();
  if (!_state.status) renderGitSkeleton(rootEl);
  await refreshGit(rootEl);
  // Load the Production section's status once the dev panel has painted (this
  // repaints the body with the Production card filled in). Kept off the auto-poll
  // path since it runs git in the production folder.
  loadProdState(rootEl);
}

// ── Live auto-refresh ──────────────────────────────────────────────
//
// While the Git Control view is on screen we poll git so the Changes
// list and commit graph track the working tree live — no manual Refresh
// needed. Each tick does a LOCAL-ONLY read (fast, no network) and repaints
// ONLY when the data actually changed AND the user isn't mid-interaction
// (typing a commit note, editing the remote URL inline, or with a branch /
// merge menu open) — so the poll never steals focus or wipes a draft. Every
// REMOTE_EVERY ticks it also requests a background refresh so ahead/behind + origin tips
// catch up. Polling pauses while the browser tab is hidden and whenever the
// view isn't the active git view. Started/stopped from files.js
// applySidebarView as the view shows / hides.

let _autoTimer = null;
let _autoRootEl = null;
let _autoTickCount = 0;
let _autoSig = '';
const AUTO_REFRESH_MS = 15000;
const REMOTE_EVERY = 20;  // ~ every 5m request a coalesced remote refresh

// A compact fingerprint of the bits we render — branch, sync counts, the
// change list, the commit graph head + branch tips. Identical fingerprints
// across ticks mean "nothing changed" → no repaint.
function _gitSignature(s, g) {
  const parts = [];
  if (s) {
    parts.push(s.branch || '', s.ahead || 0, s.behind || 0, s.remote_url || '');
    const files = [...(s.staged || []), ...(s.unstaged || []), ...(s.untracked || [])];
    parts.push(files.map(f => (f.flag || '') + ':' + (f.path || '')).join(','));
  }
  if (g && g.commits) {
    parts.push(g.commits.length);
    parts.push((g.commits[0] || {}).full_hash || '');
    if (g.branches) parts.push(Object.entries(g.branches).map(([k, v]) => k + '=' + v).join(','));
  }
  return parts.join('|');
}

// True when repainting now would disrupt the user — an open picker menu, a
// typed-but-unsent commit note, or focus sitting in any panel input. The poll
// defers (keeps the old signature) and retries on the next tick once they're done.
function _userIsBusy(rootEl) {
  const body = rootEl.querySelector('#fg-body');
  if (!body) return false;
  const bm = body.querySelector('#fg-branch-menu');
  const mm = body.querySelector('#fg-merge-menu');
  if ((bm && !bm.hidden) || (mm && !mm.hidden)) return true;
  // Don't repaint out from under an open repo dropdown or its add/edit form.
  const rm = body.querySelector('#fg-repo-menu');
  if (rm && !rm.hidden) return true;
  if (body.querySelector('#fg-repo-form')) return true;
  const cm = body.querySelector('#fg-commit-msg');
  if (cm && cm.value.trim()) return true;            // don't wipe a draft note
  const active = document.activeElement;
  if (active && body.contains(active) &&
      (active.tagName === 'INPUT' || active.tagName === 'TEXTAREA')) return true;
  return false;
}

async function _autoTick() {
  const rootEl = _autoRootEl;
  if (!rootEl) return;
  if (document.hidden) return;                        // paused while tab hidden
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar || sidebar.offsetParent === null
      || sidebar.dataset.view !== 'explorer'
      || sidebar.dataset.explorerMode !== 'git') return;
  if (_state.loading) return;                         // a manual refresh is busy

  _autoTickCount++;
  const remote = (_autoTickCount % REMOTE_EVERY) === 0;
  const statusUrl = '/api/v1/github/status' + (remote ? '' : '?fetch=0');
  const graphUrl = '/api/v1/github/log-graph?limit=80' + (remote ? '' : '&fetch=0');

  let s, g;
  const load = async () => {
    const status = await ghFetch(statusUrl);
    const graph = await ghFetch(graphUrl);
    return [status, graph];
  };
  try {
    [s, g] = remote ? await _guardRemoteRefresh(load) : await load();
  } catch (_) {
    return;   // transient (offline / mid-operation) — keep the last good view
  }

  const sig = _gitSignature(s, g);
  if (sig === _autoSig) return;        // nothing changed → no repaint, no flicker
  if (_userIsBusy(rootEl)) return;     // changed, but user mid-edit → retry later

  _autoSig = sig;
  _state.status = s;
  _state.graph = g;
  _state.err = null;
  _state.graphErr = null;
  _state.loading = false;
  renderGitPanel(rootEl);
}

export function startGitAutoRefresh(rootEl) {
  _autoRootEl = rootEl || document.getElementById('files-sidebar');
  // Seed the signature from what's already on screen so the first tick only
  // repaints if something genuinely changed since the panel was opened.
  _autoSig = _gitSignature(_state.status, _state.graph);
  _autoTickCount = 0;
  if (_autoTimer) return;              // already polling
  _autoTimer = setInterval(() => { _autoTick().catch(() => {}); }, AUTO_REFRESH_MS);
}

export function stopGitAutoRefresh() {
  if (_autoTimer) { clearInterval(_autoTimer); _autoTimer = null; }
}

export function resetGitForRemount() {
  stopGitAutoRefresh();
  const body = _autoRootEl?.querySelector('#fg-body');
  if (body?.__fgRepoCloser) {
    document.removeEventListener('click', body.__fgRepoCloser);
    body.__fgRepoCloser = null;
  }
  _autoRootEl = null;
  _state.status = null;
  _state.graph = null;
  _repos = [];
  _repoMenuOpen = false;
}

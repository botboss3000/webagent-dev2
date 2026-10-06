'use strict';

// ── FEATURE: File Manager (Explorer admin sub-view) ───────────────────────
// FILE: ui/main-panel/explorer/explorer-view.js
//
// Self-contained Explorer / File Manager drop-in. Owns everything file-side:
// the directory tree, breadcrumb, file tabs + editor panes, markdown preview,
// find/replace, the production-mirror tools ("Dev" badges, dev-only marking,
// copy-to-production / push) and the git +N/-N line badges. It shares NOTHING
// with the Terminal view — the two were split out of the old monolithic
// files.js so each is independent. It leans only on app-wide infrastructure
// (the generic floating-menu + lucide helpers in shared/js/dom-utils.js, the
// production-release streamers in shared/js/files-git.js, clipboard) and the
// main-page shell (which hosts and dispatches it).
//
// Lifecycle: this module was once its own main-panel page (explorer/page.json,
// startView()/stopView()); that descriptor is now gone. It is embedded as a TAB
// inside the Instances page's "This device" tile (ui/main-panel/app/),
// which dynamically imports it and calls startView()/stopView() — plus
// resetExplorerForRemount() before a fresh mount — to show the File Manager
// between Dashboard and Settings. The exported hooks are unchanged.
// KEEP breadcrumb comments; see docs/claude/ui-guidance.md.

import { copyText, _refreshLucideIcons, openFloatingMenu, closeFloatingMenu,
  applyRubberBand, authHeaders, isAdmin, kvRead, kvWrite, kvDelete } from './files-shims.js';
import {
  streamCopy, streamPush, streamRelease, restartServerAndReload, _RELEASE_PHASE_LABELS,
  openGitPanel, startGitAutoRefresh, stopGitAutoRefresh, resetGitForRemount, loadCommitDetail, renderCommitDetail, setSelectedGitCommit,
} from './files-git.js';

// ── Module state ──────────────────────────────────────────────────
const API_BASE = '/api/v3/file-manager';
// Open FILE tabs live in one array; the tab actions (close, rename, drag,
// persistence) all operate on this single store and render into #files-tabs /
// #files-content.
let openTabs = [];          // { path, name, content, dirty, binary, encoding, size }
let activeFilePath = null;       // path of the active FILE tab (in explorer main)
let commitTabs = [];        // session-only Git commit details; never persisted
let activeCommitKey = null;
let gitPanelOpened = false;
let expandedDirs = new Set();  // absolute paths of currently expanded directories
let dragSrcPath = null;        // path of the tab being dragged
let currentRoot = '';          // absolute path of the directory the tree is rooted at
let projectRoot = '';          // absolute path of the project root (server-reported)
let prodExcluded = new Set();  // absolute paths of dev-only folders (production mirror)
let prodViewMode = 'dev';      // 'dev' | 'prod' (production preview hides dev-only)
let gitLineStats = {};   // absolute file path -> { added, removed } since HEAD
let gitFolderStats = {}; // absolute folder path -> summed { added, removed } of its descendants
let gitStatsRoot = '';   // repo root the stats are keyed under (aggregation boundary)
// Git tracked/ignored view state — the path sets fetched from /git-status,
// remapped to absolute paths so the tree rows can be marked in place.
let gitRepoRoot = '';          // repo root the tracked/ignored sets are keyed under
let gitTreeHead = '';         // revision fence for tracking toggles
let gitTracked = new Set();    // absolute paths of tracked files (git ls-files)
let gitIgnored = new Set();    // absolute paths of ignored files / collapsed dirs
let _gitTrackedSorted = [];    // sorted absolute tracked paths (descendant lookup)
let _gitDescCache = new Map(); // 'tracked|path' / 'ignored|path' → bool
let _gitStatusLoaded = false;  // true once a git-status fetch has resolved
let _prodActionBusy = false;   // guards against a double-fire while syncing/pushing
let _prodFolder = '';          // cached destination folder (editable in the More menu)
let _prodFolderDefault = '';   // backend's suggested default (placeholder when unset)
let _prodRemote = '';          // cached production GitHub remote (editable in the More menu)
let _prodRemoteDefault = '';   // backend's stored remote (for blank-field repopulation)
let _prodToken = '';           // GitHub key typed this session (secret — sent as a backstop, never displayed)
let _prodTokenSet = false;     // whether a key is stored in the vault (drives the field's placeholder)

// Persisted state (across tab switches and reloads)
const LS_OPEN_TABS    = 'files.openTabs';
const LS_ACTIVE_TAB   = 'files.activeTab';        // legacy unified key (still read for migration)
const LS_ACTIVE_FILE  = 'files.activeFile';
const LS_EXPANDED     = 'files.expandedDirs';
const LS_CURRENT_ROOT = 'files.currentRoot';
const LS_PROD_VIEW    = 'files.prodView';         // 'dev' | 'prod' explorer view
const LS_SCROLL_POS  = 'files.scrollPos';        // file path → scrollTop (persisted across refreshes)
// kvCache rows (tenant-scoped app_cache); the LS_* keys above are the sync
const LS_SIDEBAR_MODE = 'files.explorerSidebarMode'; // 'files' | 'git'
// localStorage mirrors written alongside (see shared/js/kv-ui-state.js).
const KC_OPEN_TABS    = 'explorer:openTabs';
const KC_ACTIVE_FILE  = 'explorer:activeFile';
const KC_EXPANDED     = 'explorer:expandedDirs';
const KC_CURRENT_ROOT = 'explorer:currentRoot';
const KC_PROD_VIEW    = 'explorer:prodView';
const KC_SCROLL_POS   = 'explorer:scrollPos';

// ── Shared fetch helper (auth headers + user_id), duplicated verbatim from the
// frame so the module has no dependency on the frame's internals. ──
const fileDigests = new Map();
const EMPTY_SHA256 = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855';

async function apiFetch(path, opts = {}) {
  const [route, query = ''] = path.split('?');
  const params = new URLSearchParams(query);
  const gitRoute = route.startsWith('/api/v1/github/') ?
    route.slice('/api/v1/github/'.length) : '';
  const action = gitRoute ?
    ({'git-status':'git_tree_status', 'line-stats':'git_line_stats',
      'git-ignore':'git_ignore'})[gitRoute] :
    (route.startsWith('/') ? route.slice(1) : route);
  if (!['tree', 'read', 'write', 'create', 'rename', 'delete',
        'git_tree_status', 'git_line_stats', 'git_ignore'].includes(action)) {
    throw Error('Unsupported File Manager request');
  }
  const payload = opts.body ? JSON.parse(opts.body) : {};
  payload.action = action;
  if (gitRoute) payload.repo = '__root__';
  if (action === 'tree' || action === 'read') payload.path = params.get('path') || '';
  if (action === 'tree') payload.show_hidden = params.get('show_hidden') === 'true';
  if (action === 'write') payload.expected_sha256 = fileDigests.get(payload.path) || '';
  const response = await fetch(API_BASE, {
    method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json', ...authHeaders()},
    body: JSON.stringify(payload)
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw Error(data.error || data.detail || 'File Manager request failed (' + response.status + ')');
  if (action === 'read' || action === 'write') fileDigests.set(data.path, data.sha256);
  if (action === 'create' && data.kind === 'file') fileDigests.set(data.path, EMPTY_SHA256);
  if (action === 'rename') {
    const digest = fileDigests.get(data.from);
    if (digest) { fileDigests.delete(data.from); fileDigests.set(data.to, digest); }
  }
  if (action === 'delete') fileDigests.delete(data.path);
  return data;
}

// ══════════════════════════════════════════════════════════════════


// ── Tree ──────────────────────────────────────────────────────────

function fileIconName(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  if (['js', 'mjs', 'ts', 'tsx', 'jsx'].includes(ext)) return 'file-code';
  if (['py', 'rb', 'go', 'rs', 'java', 'c', 'cpp', 'h'].includes(ext)) return 'file-code';
  if (['html', 'css', 'scss', 'json', 'yaml', 'yml', 'xml'].includes(ext)) return 'file-code';
  if (['md', 'txt', 'rst'].includes(ext)) return 'file-text';
  if (['png', 'jpg', 'jpeg', 'gif', 'svg', 'webp'].includes(ext)) return 'image';
  if (['mp3', 'wav', 'ogg', 'm4a'].includes(ext)) return 'music';
  if (['mp4', 'webm', 'mov', 'avi'].includes(ext)) return 'video';
  if (['zip', 'tar', 'gz', '7z', 'rar'].includes(ext)) return 'archive';
  return 'file';
}

// ── Production mirror (dev-only folder marking) ───────────────────
// The exclude list lives in the backend (data/config/production-mirror.json)
// and is shared with the Git page's Production panel. We fetch it once on open
// and POST a single path on toggle, so both surfaces stay in sync.

async function loadProdExcludes() {
  try {
    const data = await apiFetch('/api/v1/github/production/exclude');
    const root = String(data.project_root || projectRoot || '').replace(/\/+$/, '');
    const abs = (data.exclude_paths || []).map((rel) => (root ? root + '/' + rel : rel));
    prodExcluded = new Set(abs);
  } catch (_) {
    // Non-fatal — without the list we simply render no badges.
  }
}

// Fetch the production destination folder (shown + editable in the More menu's
// folder field). The backend fills in its sibling default (…-dev → …-prod) when
// nothing is stored, so prod_folder is always populated. Cached so the menu can
// build synchronously; refreshed when the menu opens.
async function loadProdConfig() {
  try {
    const cfg = await apiFetch('/api/v1/github/production/config');
    _prodFolder = String(cfg.prod_folder || '');
    _prodFolderDefault = _prodFolder;
    _prodRemote = String(cfg.prod_remote_url || '');
    _prodRemoteDefault = _prodRemote;
    _prodTokenSet = !!cfg.token_set;
  } catch (_) {
    // Non-fatal — the field just starts blank with its placeholder.
  }
}

// Persist the production folder to the shared config (called on field blur/Enter).
async function saveProdFolder(value) {
  const folder = String(value || '').trim();
  _prodFolder = folder;              // keep the cache current immediately
  try {
    await apiFetch('/api/v1/github/production/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prod_folder: folder }),
    });
  } catch (_) {
    // Non-fatal — sync/push also send the folder in their payload as a backstop.
  }
}

// Persist the production GitHub remote to the shared config (field blur/Enter).
// Accepts a friendly owner/repo shorthand; the backend expands it to a full URL.
async function saveProdRemote(value) {
  const remote = String(value || '').trim();
  _prodRemote = remote;              // keep the cache current immediately
  try {
    await apiFetch('/api/v1/github/production/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ prod_remote_url: remote }),
    });
  } catch (_) {
    // Non-fatal — sync/push also send the remote in their payload as a backstop.
  }
}

// Persist the production GitHub **key** to the encrypted vault (field blur/Enter).
// A blank value is ignored so an untouched save never wipes the stored key; the
// key is never read back from the server, so the field is never pre-filled.
async function saveProdToken(value) {
  const token = String(value || '').trim();
  if (!token) return;                // blank = keep the stored key
  _prodToken = token;                // cache for the action backstop
  try {
    await apiFetch('/api/v1/github/production/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ github_token: token }),
    });
    _prodTokenSet = true;            // a key is now stored
  } catch (_) {
    // Non-fatal — sync/push also send the key in their payload as a backstop.
  }
}

// Placeholder for the GitHub key field — reflects whether a key is already
// stored (without ever revealing it).
function _prodTokenPlaceholder() {
  return _prodTokenSet ? '•••••••• saved — blank keeps it' : 'ghp_… or fine-grained token';
}

// Friendly display of a GitHub remote: a standard https://github.com/owner/repo(.git)
// collapses to "owner/repo"; anything else (SSH, self-hosted) shows in full.
function _shortRemote(url) {
  const u = String(url || '').trim();
  const m = u.match(/^https?:\/\/github\.com\/(.+?)(?:\.git)?\/?$/i);
  return m ? m[1] : u;
}

// This EXACT path was marked dev-only (folder OR file — both are supported).
function isProdExcluded(path) {
  return prodExcluded.has(path);
}

// The excluded ancestor folder of `path`, if any — a parent folder that is
// itself dev-only implicitly drops everything beneath it. Returns the ancestor
// path or '' . Used so a child shows unticked + locked under an excluded
// parent (you re-include it by re-including the parent, not the child).
function prodExcludedAncestor(path) {
  for (const ex of prodExcluded) {
    if (path !== ex && path.startsWith(ex + '/')) return ex;
  }
  return '';
}

// True when `path` actually travels to the production mirror: not excluded
// itself and not sitting under an excluded folder.
function prodWillShip(path) {
  return !isProdExcluded(path) && !prodExcludedAncestor(path);
}

// The excluded paths that live *inside* this folder (its dev-only descendants) —
// what makes a folder show a partial/indeterminate tick.
function prodExcludedDescendants(path) {
  const out = [];
  for (const ex of prodExcluded) {
    if (ex !== path && ex.startsWith(path + '/')) out.push(ex);
  }
  return out;
}

// Tri-state for a FOLDER checkbox:
//   'checked'       — the whole folder ships (nothing inside is dev-only)
//   'indeterminate' — the folder ships but SOME item inside is dev-only (partial)
//   'unchecked'     — the folder itself is dev-only, or it sits under an excluded
//                     parent (then it is also locked)
function prodFolderState(path) {
  if (isProdExcluded(path) || prodExcludedAncestor(path)) return 'unchecked';
  return prodExcludedDescendants(path).length ? 'indeterminate' : 'checked';
}

function prodCheckTitle(ships, underExcluded) {
  if (underExcluded) return 'Excluded — a parent folder is marked dev-only';
  return ships ? 'Ships to production — uncheck to keep it dev-only'
               : 'Dev-only — check to include it in production';
}

function prodFolderTitle(state, underExcluded) {
  if (underExcluded) return 'Excluded — a parent folder is marked dev-only';
  if (state === 'indeterminate')
    return 'Some items inside are dev-only — click to include everything, click again to exclude everything';
  if (state === 'checked') return 'Everything here ships — click to keep the whole folder dev-only';
  return 'Folder is dev-only — click to include it in production';
}

// Persist one path's include/exclude state, then repaint the marks in place (no
// tree reload, so the admin can rattle through many checkboxes smoothly). The
// exclude list lives in the backend and is shared with the Git page.
async function setProdExclude(path, excluded) {
  try {
    await apiFetch('/api/v1/github/production/exclude', {
      method: 'POST',
      body: JSON.stringify({ path, excluded }),
    });
    if (excluded) prodExcluded.add(path); else prodExcluded.delete(path);
    refreshTreeMarks();
  } catch (e) {
    alert('Could not update production exclude: ' + (e.message || 'failed'));
  }
}

// Apply several exclude changes in one round-trip (used when a folder checkbox
// includes / excludes its whole subtree). add/remove are absolute paths; we keep
// the local set authoritative from the same lists so no rel→abs remap is needed.
async function setProdExcludeBulk(addPaths, removePaths) {
  const add = addPaths || [];
  const remove = removePaths || [];
  if (!add.length && !remove.length) return;
  try {
    await apiFetch('/api/v1/github/production/exclude-bulk', {
      method: 'POST',
      body: JSON.stringify({ add, remove }),
    });
    remove.forEach((p) => prodExcluded.delete(p));
    add.forEach((p) => prodExcluded.add(p));
    refreshTreeMarks();
  } catch (e) {
    alert('Could not update production exclude: ' + (e.message || 'failed'));
  }
}

// Include a folder's WHOLE subtree: clear the folder's own mark plus every
// dev-only descendant, so everything inside ships again.
function prodIncludeSubtree(path) {
  setProdExcludeBulk([], [path, ...prodExcludedDescendants(path)]);
}

// Exclude a folder's WHOLE subtree: mark the folder dev-only (which covers
// everything beneath it) and drop the now-redundant per-descendant marks.
function prodExcludeSubtree(path) {
  setProdExcludeBulk([path], prodExcludedDescendants(path));
}

// Right-click entry point — flip dev-only state. For a folder this mirrors the
// checkbox's first-click rule: a fully-shipping folder is excluded wholesale,
// anything else (dev-only or partially-excluded) is included wholesale.
function toggleProdExclude(entry) {
  if (entry.is_dir) {
    if (prodFolderState(entry.path) === 'checked') prodExcludeSubtree(entry.path);
    else prodIncludeSubtree(entry.path);
  } else {
    setProdExclude(entry.path, !isProdExcluded(entry.path));
  }
}

// ── Git tracked/ignored view ───────────────────────────────────────
// Fetch the repo's tracked + ignored path sets from the backend (one call,
// whole repo, pinned to the project root) and remap them to absolute paths so
// the tree's rows can be marked. Non-fatal throughout: no data → no marks.

// Remap the backend's repo-relative sets to absolute paths and repaint every
// rendered row — shared by the initial load and every interactive toggle.
function applyGitStatusData(data) {
  const root = String(data.repo_root || projectRoot || '').replace(/\/+$/, '');
  gitRepoRoot = root;
  gitTreeHead = String(data.head || '');
  const tracked = new Set();
  const ignored = new Set();
  const add = (set, rel) => {
    rel = String(rel || '').trim();
    if (rel) set.add(root ? root + '/' + rel : rel);
  };
  (data.tracked || []).forEach((r) => add(tracked, r));
  (data.ignored || []).forEach((r) => add(ignored, r));
  gitTracked = tracked;
  gitIgnored = ignored;
  _gitTrackedSorted = Array.from(tracked).sort();
  _gitDescCache.clear();
  refreshTreeMarks();
}

async function loadGitStatus() {
  _gitStatusLoaded = true;
  try {
    const data = await apiFetch('/api/v1/github/git-status');
    applyGitStatusData(data);
  } catch (_) {
    // Non-fatal — without the sets we simply render no git marks.
  }
}

// An absolute tree path → repo-relative path (what the git endpoints expect).
function absToRepoRel(absPath) {
  if (!gitRepoRoot) return absPath;
  if (absPath === gitRepoRoot) return '';
  if (absPath.startsWith(gitRepoRoot + '/')) return absPath.slice(gitRepoRoot.length + 1);
  return absPath;
}

// Flip one path's tracked/ignored state in git (the git-view checkboxes are
// live toggles: ticking = un-ignore + track, unticking = ignore + untrack).
// The backend edits .gitignore / the index and returns the fresh sets; we
// repaint every row from those so the tree always reflects reality.
async function toggleGitTracked(path, isDir, tracked) {
  const rel = absToRepoRel(path);
  if (!rel) return;
  try {
    const data = await apiFetch('/api/v1/github/git-ignore', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: rel, tracked, expected_head: gitTreeHead }),
    });
    applyGitStatusData(data);
  } catch (e) {
    refreshTreeMarks();
    alert('Could not update git ignore state: ' + (e.message || 'failed'));
  }
}

// True when the path itself is listed as ignored (the set holds individual
// files and whole collapsed directories from `git status --ignored`).
function gitIsIgnored(path) {
  return gitIgnored.has(path);
}

// True when any ancestor directory of the path is ignored — everything beneath
// a collapsed ignored folder is ignored too, even though only the folder
// itself appears in the set.
function gitUnderIgnored(path) {
  let i = path.lastIndexOf('/');
  while (i > 0) {
    if (gitIgnored.has(path.slice(0, i))) return true;
    i = path.lastIndexOf('/', i - 1);
  }
  return false;
}

function gitIsIgnoredOrUnder(path) {
  return gitIsIgnored(path) || gitUnderIgnored(path);
}

// First index in `sorted` whose value is >= prefix (binary search). Path
// comparison is byte-wise, so 'folder/x' sorts after both 'folder.txt' and
// 'folder' — a startsWith check on the hit distinguishes real descendants.
function _firstGte(sorted, prefix) {
  let lo = 0, hi = sorted.length;
  while (lo < hi) {
    const mid = (lo + hi) >> 1;
    if (sorted[mid] < prefix) lo = mid + 1; else hi = mid;
  }
  return lo;
}

// Whether any entry of `kind` ('tracked' | 'ignored') lives inside this folder.
// Tracked uses a binary search over the sorted list; ignored scans the (small,
// collapsed) set. Results are cached per folder+kind because the same folders
// re-appear on every render/refresh.
function gitHasDescendant(kind, folderPath) {
  const key = kind + '|' + folderPath;
  const cached = _gitDescCache.get(key);
  if (cached !== undefined) return cached;
  const prefix = folderPath + '/';
  let has = false;
  if (kind === 'tracked') {
    const i = _firstGte(_gitTrackedSorted, prefix);
    has = i < _gitTrackedSorted.length && _gitTrackedSorted[i].startsWith(prefix);
  } else {
    for (const p of gitIgnored) {
      if (p.startsWith(prefix)) { has = true; break; }
    }
  }
  _gitDescCache.set(key, has);
  return has;
}

// Tri-state for a FOLDER checkbox in git view:
//   'checked'       — git tracks something inside (nothing here is ignored)
//   'indeterminate' — some contents are tracked and some are git-ignored
//   'unchecked'     — the folder itself is ignored (or under one), or it holds
//                     only untracked, non-ignored files
function gitFolderState(path) {
  if (gitIsIgnoredOrUnder(path)) return 'unchecked';
  if (gitHasDescendant('ignored', path)) return 'indeterminate';
  if (gitHasDescendant('tracked', path)) return 'checked';
  return 'unchecked';
}

// A FILE's git state: 'tracked' | 'ignored' | 'untracked'
function gitFileState(path) {
  if (gitIsIgnoredOrUnder(path)) return 'ignored';
  return gitTracked.has(path) ? 'tracked' : 'untracked';
}

function gitFolderTitle(state, underIgnored) {
  if (underIgnored) return 'A parent folder is git-ignored — check to track everything inside';
  if (state === 'indeterminate') return 'Partially ignored — click to track everything, click again to ignore everything';
  if (state === 'checked') return 'Tracked by git — uncheck to ignore the whole folder';
  return 'Ignored by git — check to track everything inside';
}

function gitFileTitle(state, underIgnored) {
  if (state === 'ignored') return underIgnored
    ? 'A parent folder is git-ignored — check to track this file'
    : 'Ignored by git — check to track it';
  if (state === 'tracked') return 'Tracked by git — uncheck to ignore it';
  return 'Not tracked by git yet — check to track it';
}

// Paint one checkbox from the git sets. Unlike prod view these are LIVE
// toggles: every box stays clickable (even under an ignored parent — checking
// it un-ignores the chain), and the change handler posts the flip to git.
function applyGitCheckState(check, path, isDir) {
  const under = gitUnderIgnored(path);
  if (isDir) {
    const st = gitFolderState(path);
    check.checked = st === 'checked';
    check.indeterminate = st === 'indeterminate';
    check.disabled = false;
    check.title = gitFolderTitle(st, under);
  } else {
    const st = gitFileState(path);
    check.checked = st === 'tracked';
    check.indeterminate = false;
    check.disabled = false;
    check.title = gitFileTitle(st, under);
  }
}

// Paint one checkbox for the CURRENT view mode: git marks in git view, the
// production exclude marks otherwise.
function applyTreeCheckState(check, path, isDir) {
  if (prodViewMode === 'git') { applyGitCheckState(check, path, isDir); return; }
  const underExcluded = !!prodExcludedAncestor(path);
  if (isDir) {
    // Folders carry a tri-state: a partial tick when some items inside are
    // dev-only. The indeterminate flag is visual-only and must be re-set here.
    const st = prodFolderState(path);
    check.checked = st === 'checked';
    check.indeterminate = st === 'indeterminate';
    check.disabled = underExcluded;
    check.title = prodFolderTitle(st, underExcluded);
  } else {
    const ships = prodWillShip(path);
    check.checked = ships;
    check.indeterminate = false;
    check.disabled = underExcluded;
    check.title = prodCheckTitle(ships, underExcluded);
  }
}

// Walk every rendered row and resync its checkbox, "Dev" badge and dropped
// styling to the current view mode's state — called after any single toggle or
// a fresh git-status load so the whole visible subtree reflects the change
// without a server round-trip.
function refreshTreeMarks() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.querySelectorAll('.files-tree-node').forEach((node) => {
    const path = node.dataset.path;
    if (!path) return;
    const isDir = node.dataset.kind === 'dir';
    const row = node.querySelector(':scope > .files-tree-row');
    if (!row) return;
    const check = row.querySelector('.files-tree-check');
    if (check) applyTreeCheckState(check, path, isDir);
    // Dimming: prod view drops non-shipping rows, git view drops ignored rows.
    const dropped = prodViewMode === 'git' ? gitIsIgnoredOrUnder(path) : !prodWillShip(path);
    node.classList.toggle('is-prod-dropped', dropped);
    const badge = row.querySelector('.files-tree-badge.dev-only');
    if (badge) badge.classList.toggle('show', prodViewMode !== 'git' && isProdExcluded(path));
  });
}

// True when the tree is rooted at the project root ("home"). The production
// folder is defined relative to that root, so the dev-only checkboxes only make
// sense there — once the user navigates into a subfolder we hide the preview.
function _atProjectRoot() {
  return !projectRoot || currentRoot === projectRoot;
}

// Reflect the dev/production/git view mode onto the tree container. In prod
// preview the dev-only checkboxes appear; in git view the tracked/ignored
// checkboxes appear; dev view hides them again. Both only apply at the project
// root (home) — away from home the checkboxes stay hidden even when the mode is
// set, so the persisted preference resumes when the user returns home. The
// toggles live in the "More" popover now, so there's no toolbar button to
// restyle here — just the tree + the status line.
function applyProdViewClass() {
  const tree = document.getElementById('files-tree');
  const atHome = _atProjectRoot();
  if (tree) {
    tree.classList.toggle('prod-view', prodViewMode === 'prod' && atHome);
    tree.classList.toggle('git-view', prodViewMode === 'git' && atHome);
  }
  // Leaving production-preview clears any lingering copy/push status line.
  const status = document.getElementById('files-prod-status');
  if (status && prodViewMode !== 'prod') status.classList.remove('show');
}

// Flip the tree's checkbox view mode ('dev' = none, 'prod' = dev-only marks,
// 'git' = tracked/ignored marks). Used by the More menu's two toggle rows.
function setTreeViewMode(mode) {
  prodViewMode = mode;
  kvWrite(KC_PROD_VIEW, LS_PROD_VIEW, prodViewMode);
  applyProdViewClass();
  refreshTreeMarks();
}

// Fetch how dev and the production folder differ and write a one-line summary
// into the More menu's diff row (#files-prod-diff). Read-only; called when the
// menu opens. The backend walks + byte-compares the shipping set, so this is a
// touch slower than the other reads — the row shows "Checking…" until it lands.
async function loadProdDiff() {
  const set = (txt) => {
    const el = document.getElementById('files-prod-diff');
    if (el) el.textContent = txt;
  };
  try {
    const d = await apiFetch('/api/v1/github/production/diff');
    if (!d) { set('Could not compare with production.'); return; }
    if (d.configured === false) { set('Set a production repo to compare.'); return; }
    if (d.first_sync) { set(`Not synced yet — ${d.shipping_count} files would be sent.`); return; }
    if (d.in_sync) { set('In sync — production matches dev.'); return; }
    const parts = [];
    if (d.added)   parts.push(`${d.added} to add`);
    if (d.updated) parts.push(`${d.updated} to update`);
    if (d.removed) parts.push(`${d.removed} to remove`);
    set(parts.length ? `Differs: ${parts.join(', ')}.` : 'Differences found.');
  } catch (_) {
    set('Could not compare with production.');
  }
}

// Write the header status line under the toolbar (tones: info/success/error).
function setProdStatus(text, tone) {
  const el = document.getElementById('files-prod-status');
  if (!el) return;
  el.textContent = text;
  el.dataset.tone = tone || 'info';
  el.classList.add('show');
}

// Run one production action (Copy or Push) as a live NDJSON stream, driving the
// header status line with phase labels then the terminal result. Both halves of a
// release share this so they can't drift in confirm/progress/result handling.
//   streamFn   — streamCopy or streamPush (from files-git.js)
//   confirmMsg — confirmation prompt (these are outward-facing / hard to undo)
//   okStatuses — backend terminal statuses treated as success (green)
async function _runProdAction(streamFn, confirmMsg, okStatuses) {
  if (_prodActionBusy) return;
  if (confirmMsg && !confirm(confirmMsg)) return;

  _prodActionBusy = true;
  setProdStatus('Starting…', 'info');

  let result = null;
  try {
    // Send the current destination folder so the backend uses the freshest value
    // even if the field's auto-save is still in flight. The backend also auto-
    // generates the commit message. The stream helper adds {stream:true}.
    await streamFn({ prod_folder: _prodFolder || undefined, prod_remote_url: _prodRemote || undefined, github_token: _prodToken || undefined }, (ev) => {
      if (ev.phase === 'done') { result = ev.result || {}; return; }
      // The first copy clones the existing repo (~20s) — say so instead of a
      // generic "Building…" label that looks frozen.
      if (ev.phase === 'copying' && ev.first_setup) {
        setProdStatus('Setting up the production repo (first run — cloning, ~20s)…', 'info');
        return;
      }
      if (ev.phase === 'message_ready') {
        // Show the AI-written title (mirrors the ⭐ commit&push step display).
        setProdStatus(ev.title ? `Message: "${ev.title}"` : 'Message ready', 'info');
        return;
      }
      const label = _RELEASE_PHASE_LABELS[ev.phase];
      if (label) setProdStatus(label, 'info');
    });
  } catch (e) {
    result = { status: 'error', message: (e && e.message) || 'action failed' };
  }

  const r = result || { status: 'error', message: 'no result' };
  const ok = okStatuses.includes(r.status);
  const tone = ok ? 'success'
    : r.status === 'nothing' ? 'info'
    : (r.status === 'blocked' || r.status === 'error') ? 'error' : 'info';
  setProdStatus(r.message || r.status || 'Done', tone);

  _prodActionBusy = false;
  // Auto-clear a successful / neutral result; leave errors up so they can be read.
  if (tone !== 'error') {
    const el = document.getElementById('files-prod-status');
    setTimeout(() => {
      if (el && el.dataset.tone !== 'error') el.classList.remove('show');
    }, 9000);
  }
}

// "Sync to production": one-way sync the shipping set (every file NOT marked
// dev-only) from dev into the sister production folder — adds new/changed files
// and drops files no longer shipped — then commit there LOCALLY (no push). Lands
// the files so they can be reviewed before publishing. The backend's secret-scan
// still guards the commit.
function syncToProduction() {
  return _runProdAction(
    streamCopy,
    'Sync to production?\n\nThis one-way syncs the shipping set (every file NOT marked dev-only) from dev into the production folder — adding changed files and removing files no longer shipped — then commits there locally. Nothing is pushed to GitHub yet.',
    ['copied'],
  );
}

// "Push to GitHub": send whatever Copy last committed in the production folder up
// to the production GitHub remote.
function pushToProduction() {
  return _runProdAction(
    streamPush,
    null,  // no confirm — push runs immediately
    ['pushed'],
  );
}

// "Sync and push": the full one-click release — sync the shipping set into the
// production folder AND push it to GitHub in a single step (the same trim→copy→
// commit→push the Git page's Release button runs). Saves doing Sync then Push by
// hand when you just want to publish.
function syncAndPushToProduction() {
  return _runProdAction(
    streamRelease,
    null,  // no confirm — sync + push runs immediately
    ['released'],
  );
}

// The toolbar "More" popover: the production-preview toggle, the production repo
// (GitHub remote) + GitHub key + folder fields, a live in-sync/diff line, and the
// two release halves. Reuses the shared floating-menu builder so it matches the
// tab/context menus; the eye row shows a ✓ while production-preview is on.
function openDevelopmentMenu(anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const atHome = _atProjectRoot();
  openFloatingMenu([{
    icon: 'git-branch',
    label: atHome ? 'Show Git tracking' : 'Git tracking only at project root',
    checked: prodViewMode === 'git' && atHome,
    disabled: !atHome,
    action: () => {
      const next = prodViewMode === 'git' ? 'dev' : 'git';
      setTreeViewMode(next);
      if (next === 'git') loadGitStatus();
    }
  }], rect.bottom + 2, rect.right - 180);
}

function openProductionMenu(anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const atHome = _atProjectRoot();
  const previewing = prodViewMode === 'prod' && atHome;
  const gitViewing = prodViewMode === 'git' && atHome;
  const items = [
    // Git tracked/ignored status: read-only checkboxes showing what git tracks
    // vs ignores, at the project root (the app's own repo).
    atHome
      ? { icon: gitViewing ? 'eye-off' : 'git-branch', label: 'Show git status',
          checked: gitViewing, action: () => setTreeViewMode(gitViewing ? 'dev' : 'git') }
      : { icon: 'git-branch', label: 'Git status only in Project root',
          disabled: true },
    // The production folder is defined relative to the project root, so the
    // dev-only checkboxes only work at home. Away from home the row turns into a
    // disabled hint telling the user where to go.
    atHome
      ? { icon: previewing ? 'eye-off' : 'eye', label: 'Show dev checkboxes',
          checked: previewing, action: () => setTreeViewMode(previewing ? 'dev' : 'prod') }
      : { icon: 'eye-off', label: 'Dev checkboxes only in Project root',
          disabled: true },
    { separator: true },
    // Production GitHub remote (owner/repo shorthand accepted). Persists on blur;
    // Sync/Push also send it as a backstop.
    { field: true, label: 'Production repo', value: _shortRemote(_prodRemote),
      placeholder: 'owner/repo', fieldKey: 'remote',
      onInput: (v) => { _prodRemote = v.trim(); },
      onSave: (v) => saveProdRemote(v) },
    // GitHub key (secret) → encrypted vault. Masked, NEVER pre-filled/echoed;
    // blank keeps the stored key. Sync/Push also send it as a backstop.
    { field: true, label: 'GitHub key', value: '', fieldType: 'password', fieldKey: 'token',
      placeholder: _prodTokenPlaceholder(),
      onInput: (v) => { _prodToken = v.trim(); },
      onSave: (v) => saveProdToken(v) },
    // Destination folder field. Updates the cache per keystroke and persists on
    // blur/Enter; Sync/Push also send it as a backstop.
    { field: true, label: 'Production folder', value: _prodFolder,
      placeholder: _prodFolderDefault || '…/your-repo-prod', fieldKey: 'folder',
      onInput: (v) => { _prodFolder = v.trim(); },
      onSave: (v) => saveProdFolder(v) },
    { separator: true },
    // Live diff line (filled in by loadProdDiff once the menu is up).
    { info: true, id: 'files-prod-diff', label: 'Checking for changes…' },
    { icon: 'refresh-cw',   label: 'Sync to production', disabled: _prodActionBusy, action: syncToProduction },
    { icon: 'upload-cloud', label: 'Push to GitHub',     disabled: _prodActionBusy, action: pushToProduction },
    { icon: 'cloud-upload', label: 'Sync and push',      disabled: _prodActionBusy, action: syncAndPushToProduction },
  ];
  openFloatingMenu(items, rect.bottom + 2, rect.right - 240);
  loadProdDiff();
  // Refresh the folder + remote from the backend in case they changed elsewhere;
  // update an untouched, still-blank field in place if the value arrives while the
  // menu is up. Fields are matched by data-field (not index) so inserting the key
  // field can't misalign them; the key field is never populated (it's a secret).
  loadProdConfig().then(() => {
    const menu = document.getElementById('files-floating-menu');
    if (!menu) return;
    const remoteInp = menu.querySelector('input[data-field="remote"]');
    const folderInp = menu.querySelector('input[data-field="folder"]');
    const tokenInp  = menu.querySelector('input[data-field="token"]');
    if (remoteInp && document.activeElement !== remoteInp && !remoteInp.value) remoteInp.value = _shortRemote(_prodRemote);
    if (folderInp && document.activeElement !== folderInp && !folderInp.value) folderInp.value = _prodFolder;
    if (tokenInp  && document.activeElement !== tokenInp  && !tokenInp.value)  tokenInp.placeholder = _prodTokenPlaceholder();
  });
}

// ── Git line-change badges (+N / -N since last commit) ────────────
// Fetch the per-file added/removed map from the backend (one call, whole repo),
// remap it to absolute paths, roll the files up into folder sums, then repaint
// the badges on every rendered row. Non-fatal throughout: no data → no badges.

let _lineStatsPending = false;

async function loadLineStats() {
  if (_lineStatsPending) return;
  _lineStatsPending = true;
  try {
    const data = await apiFetch('/api/v1/github/line-stats');
    const root = String(data.project_root || projectRoot || '').replace(/\/+$/, '');
    gitStatsRoot = root;
    const raw = data.stats || {};
    const map = {};
    for (const rel in raw) {
      const abs = root ? root + '/' + rel : rel;
      map[abs] = { added: raw[rel].added || 0, removed: raw[rel].removed || 0 };
    }
    gitLineStats = map;
    rebuildFolderStats();
    refreshLineStatMarks();
  } catch (_) {
    _lineStatsPending = false;
    // Non-fatal — without the stats we simply render no counts.
  }
  _lineStatsPending = false;
}

// Sum each changed file's counts into every ancestor folder up to (and
// including) the repo root, so a collapsed folder can show its subtree total.
function rebuildFolderStats() {
  const folders = {};
  const boundary = gitStatsRoot || projectRoot || '';
  for (const abs in gitLineStats) {
    const st = gitLineStats[abs];
    let idx = abs.lastIndexOf('/');
    while (idx > 0) {
      const folder = abs.slice(0, idx);
      // Stop once we'd climb above the repo root (those folders aren't shown).
      if (boundary && (folder.length < boundary.length || !folder.startsWith(boundary))) break;
      const agg = folders[folder] || (folders[folder] = { added: 0, removed: 0 });
      agg.added += st.added;
      agg.removed += st.removed;
      idx = folder.lastIndexOf('/');
    }
  }
  gitFolderStats = folders;
}

// Paint one row's badge span from a { added, removed } record (or clear it when
// there's no change). The span is always present in the DOM so it can be
// updated in place after a refresh without rebuilding the row.
function applyLineStat(span, st) {
  if (!span) return;
  span.textContent = '';
  if (!st || (!st.added && !st.removed)) { span.classList.remove('show'); return; }
  if (st.added) {
    const a = document.createElement('span');
    a.className = 'files-tree-stat-add';
    a.textContent = '+' + st.added;
    span.appendChild(a);
  }
  if (st.removed) {
    const r = document.createElement('span');
    r.className = 'files-tree-stat-del';
    r.textContent = '-' + st.removed;
    span.appendChild(r);
  }
  const parts = [];
  if (st.added) parts.push(st.added + ' added');
  if (st.removed) parts.push(st.removed + ' removed');
  span.title = parts.join(', ') + ' since last commit';
  span.classList.add('show');
}

// The current stat record for a path: files use their own count, folders the
// rolled-up subtree sum.
function lineStatFor(path, isDir) {
  return isDir ? gitFolderStats[path] : gitLineStats[path];
}

// Resync every rendered row's badge to the current maps — called after a fresh
// fetch (the tree itself isn't rebuilt). Lazily-expanded children pick up their
// badge at render time straight from the maps, so this only needs to touch what
// is already on screen.
function refreshLineStatMarks() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.querySelectorAll('.files-tree-node').forEach((node) => {
    const path = node.dataset.path;
    if (!path) return;
    const row = node.querySelector(':scope > .files-tree-row');
    if (!row) return;
    const span = row.querySelector('.files-tree-stat');
    if (span) applyLineStat(span, lineStatFor(path, node.dataset.kind === 'dir'));
  });
}

// Keep the +/- badges live while the Explorer tree is open: re-fetch the counts
// on a timer so commits/edits made elsewhere (the agent, the terminal, another
// device) show up without a manual reload. Paused while the browser tab is
// hidden or the active sidebar view isn't the tree, and only one timer ever
// runs (start is idempotent). Mirrors the Git Control auto-refresh.
const LINE_STATS_REFRESH_MS = 60000;
let _lineStatsTimer = null;

function startLineStatsAutoRefresh() {
  if (_lineStatsTimer) return;          // already polling
  _lineStatsTimer = setInterval(() => {
    if (document.hidden) return;        // browser tab not visible → skip the round-trip
    if (!isAdmin()) return;
    const sb = document.getElementById('files-sidebar');
    if (!sb || sb.offsetParent === null) return;
    if (sb && sb.dataset.view !== 'explorer') return;  // only while the tree is the active view
    loadLineStats();                    // cheap re-fetch; repaints badges in place
  }, LINE_STATS_REFRESH_MS);
}

function stopLineStatsAutoRefresh() {
  if (_lineStatsTimer) { clearInterval(_lineStatsTimer); _lineStatsTimer = null; }
}

function renderTreeRow(entry, depth) {
  const row = document.createElement('div');
  row.className = 'files-tree-row';
  row.style.paddingLeft = (depth * 12 + 4) + 'px';
  row.dataset.path = entry.path;
  // Make every row draggable so the user can drop its path onto a terminal
  // pane (handled in buildPaneForTab). text/plain is the canonical type
  // both DataTransfer.types and our drop handlers look for.
  row.draggable = true;
  row.addEventListener('dragstart', (e) => {
    try {
      e.dataTransfer.setData('text/plain', entry.path);
      e.dataTransfer.effectAllowed = 'copy';
    } catch (_) {}
  });

  // Production-preview / git-status checkbox (hidden unless the tree is in a
  // checkbox view). In prod view: ticked = ships to the production mirror,
  // unticking marks it dev-only, a path under an already-excluded folder shows
  // unticked + locked. In git view: checked = git-tracked, blank = ignored,
  // partial dash = some contents ignored — and the boxes are LIVE toggles that
  // change the actual .gitignore rules / index (see toggleGitTracked). The prod
  // exclude list is shared with the Git page's Production panel. Created here
  // but appended AFTER the chevron, so the row reads: chevron → checkbox →
  // name.
  const check = document.createElement('input');
  check.type = 'checkbox';
  check.className = 'files-tree-check';
  applyTreeCheckState(check, entry.path, entry.is_dir);
  // A checkbox click must not open the file / toggle the folder. (The checkbox
  // also re-enables pointer-events in CSS, since the row sets them off on every
  // child so the whole row is one click target.)
  check.addEventListener('click', (e) => e.stopPropagation());
  check.addEventListener('change', (e) => {
    e.stopPropagation();
    if (prodViewMode === 'git') {
      // Git view checkboxes are live: ticking includes the path in git
      // (tracked), unticking makes it ignored. A folder's tri-state click
      // intent works like prod view — tick = whole subtree tracked, untick =
      // whole subtree ignored.
      if (entry.is_dir) {
        if (check.checked) toggleGitTracked(entry.path, true, true);
        else toggleGitTracked(entry.path, true, false);
      } else {
        toggleGitTracked(entry.path, false, check.checked);
      }
      return;
    }
    if (entry.is_dir) {
      // The browser has already resolved the tri-state on click: ticking a
      // partial (or empty) folder includes the whole subtree, unticking a full
      // one excludes it. So check.checked now carries the intent.
      if (check.checked) prodIncludeSubtree(entry.path);
      else prodExcludeSubtree(entry.path);
    } else {
      setProdExclude(entry.path, !check.checked);
    }
  });

  const chev = document.createElement('span');
  chev.className = 'files-tree-chev';
  if (entry.is_dir) {
    const i = document.createElement('i');
    i.setAttribute('data-lucide', 'chevron-right');
    i.className = 'lucide-icon';
    chev.appendChild(i);
  } else {
    chev.classList.add('invisible');
  }
  row.appendChild(chev);
  row.appendChild(check);

  const icon = document.createElement('span');
  icon.className = 'files-tree-icon ' + (entry.is_dir ? 'dir' : 'file');
  const ic = document.createElement('i');
  ic.setAttribute('data-lucide', entry.is_dir ? 'folder' : fileIconName(entry.name));
  ic.className = 'lucide-icon';
  icon.appendChild(ic);
  row.appendChild(icon);

  const label = document.createElement('span');
  label.className = 'files-tree-label';
  label.textContent = entry.name;
  row.appendChild(label);

  // Dev-only badge — shown (via the `show` class) when this exact path is
  // excluded from the production mirror, for files and folders alike. Always
  // present but hidden otherwise, so refreshTreeMarks() can toggle it live
  // without rebuilding the row.
  const badge = document.createElement('span');
  badge.className = 'files-tree-badge dev-only' + (isProdExcluded(entry.path) ? ' show' : '');
  badge.textContent = 'Dev';
  badge.title = 'Excluded from production';
  row.appendChild(badge);

  // Git +N/-N change badge (filled now from the loaded maps; an expanded folder's
  // own badge is hidden by CSS so its visible children carry the counts instead).
  const stat = document.createElement('span');
  stat.className = 'files-tree-stat';
  applyLineStat(stat, lineStatFor(entry.path, entry.is_dir));
  row.appendChild(stat);
  return row;
}

function renderTreeNode(entry, depth) {
  const node = document.createElement('div');
  node.className = 'files-tree-node';
  node.dataset.path = entry.path;
  node.dataset.kind = entry.is_dir ? 'dir' : 'file';
  // Mark dropped nodes so the checkbox views can dim + strike them: in prod
  // view that's non-shipping (excluded) paths, in git view it's ignored ones.
  if (prodViewMode === 'git' ? gitIsIgnoredOrUnder(entry.path) : !prodWillShip(entry.path)) {
    node.classList.add('is-prod-dropped');
  }

  const row = renderTreeRow(entry, depth);
  node.appendChild(row);

  // Right-click → context menu with the advanced actions
  row.addEventListener('contextmenu', (e) => {
    e.preventDefault();
    e.stopPropagation();
    selectTreeRow(row);
    showTreeContextMenu(entry, e.clientX, e.clientY);
  });

  if (entry.is_dir) {
    const children = document.createElement('div');
    children.className = 'files-tree-children';
    node.appendChild(children);
    if (expandedDirs.has(entry.path)) {
      node.classList.add('expanded');
      // load children async
      loadDirInto(entry.path, children, depth + 1);
    }
    row.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleDir(node, entry.path, depth);
    });
    // ── Drag-and-drop file upload from the OS ──
    // The user drags one or more files from their OS file manager onto
    // a folder row in the tree; we upload each via /api/v1/files/write.
    wireFolderDropTarget(row, entry.path);
  } else {
    row.addEventListener('click', (e) => {
      e.stopPropagation();
      selectTreeRow(row);
      openFile(entry.path, entry.name);
    });
  }
  return node;
}

function selectTreeRow(row) {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.querySelectorAll('.files-tree-row.selected').forEach((r) => r.classList.remove('selected'));
  if (row) row.classList.add('selected');
}

// ── Drag-and-drop file upload ──────────────────────────────────────

function wireFolderDropTarget(row, folderPath) {
  // Counter for dragenter/leave pairs — these events fire on every child
  // boundary crossing too, so plain add/remove is unreliable.
  let depth = 0;

  function hasFiles(e) {
    if (!e.dataTransfer) return false;
    const types = e.dataTransfer.types;
    if (!types) return false;
    // DOMStringList in Firefox, array-like elsewhere
    for (let i = 0; i < types.length; i++) {
      if (types[i] === 'Files') return true;
    }
    return false;
  }

  row.addEventListener('dragenter', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    depth++;
    row.classList.add('files-drop-target');
  });
  // dragover must preventDefault on *every* fire or the drop event never
  // happens. We don't gate on hasFiles here because some browsers only
  // expose `types` reliably on dragenter/drop.
  row.addEventListener('dragover', (e) => {
    if (!hasFiles(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'copy';
  });
  row.addEventListener('dragleave', () => {
    if (depth > 0) depth--;
    if (depth === 0) row.classList.remove('files-drop-target');
  });
  row.addEventListener('drop', async (e) => {
    if (!e.dataTransfer || !e.dataTransfer.files || !e.dataTransfer.files.length) return;
    e.preventDefault();
    e.stopPropagation();
    depth = 0;
    row.classList.remove('files-drop-target');
    await uploadFilesToFolder(folderPath, Array.from(e.dataTransfer.files));
  });
}

// Catch file drops anywhere in the Files page that *aren't* on a folder
// row so the browser doesn't navigate to / download the dropped file
// (which is the default behaviour when no handler preventDefaults).
let _filesDropGuardInstalled = false;
function installFilesDropGuard() {
  if (_filesDropGuardInstalled) return;
  _filesDropGuardInstalled = true;
  const editor = document.getElementById('file-manager');
  if (!editor) return;
  editor.addEventListener('dragover', (e) => {
    if (e.dataTransfer && Array.from(e.dataTransfer.types).indexOf('Files') !== -1) {
      e.preventDefault();
    }
  });
  editor.addEventListener('drop', (e) => {
    if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length &&
        !(e.target.closest && e.target.closest('.files-tree-row[data-kind="dir"], .files-tree-node[data-kind="dir"] > .files-tree-row'))) {
      // Dropped on the editor but not on a folder row — swallow so the
      // browser doesn't navigate away from the page.
      e.preventDefault();
    }
  });
}

async function uploadFilesToFolder(folderPath, fileList) {
  // Make sure the destination folder is expanded so the user can see
  // the new entries arrive after the tree refresh below.
  expandedDirs.add(folderPath);
  persistExpanded();

  let okCount = 0;
  let failCount = 0;
  const errors = [];

  for (const file of fileList) {
    const destPath = folderPath.replace(/\/+$/, '') + '/' + file.name;
    try {
      const { content, encoding } = await readFileForUpload(file);
      await apiFetch('/write', {
        method: 'POST',
        body: JSON.stringify({ path: destPath, content, encoding }),
      });
      okCount++;
    } catch (err) {
      failCount++;
      errors.push(file.name + ': ' + (err.message || err));
    }
  }

  await loadRoot();

  if (failCount) {
    alert(
      'Uploaded ' + okCount + ' of ' + fileList.length + ' file' + (fileList.length === 1 ? '' : 's') + '.\n\n' +
      'Failed:\n' + errors.join('\n')
    );
  }
}

function readFileForUpload(file) {
  return new Promise((resolve, reject) => {
    // Decide text-vs-binary up front from MIME type. Text-ish types go
    // up as utf-8, everything else as base64. The backend accepts both.
    const looksTexty =
      !file.type ||
      file.type.startsWith('text/') ||
      /\b(json|xml|javascript|x-sh|x-python|yaml|toml|csv|svg\+xml)\b/.test(file.type);

    const reader = new FileReader();
    reader.onerror = () => reject(new Error('read failed'));
    if (looksTexty) {
      reader.onload = () => resolve({ content: String(reader.result || ''), encoding: 'utf-8' });
      reader.readAsText(file);
    } else {
      reader.onload = () => {
        // readAsDataURL returns "data:<mime>;base64,<payload>"; strip the prefix
        const s = String(reader.result || '');
        const comma = s.indexOf(',');
        resolve({ content: comma >= 0 ? s.slice(comma + 1) : s, encoding: 'base64' });
      };
      reader.readAsDataURL(file);
    }
  });
}

async function loadDirInto(path, container, depth) {
  container.innerHTML = '<div class="files-tree-loading">Loading…</div>';
  try {
    const data = await apiFetch('/tree?path=' + encodeURIComponent(path));
    container.innerHTML = '';
    if (!data.entries.length) {
      const empty = document.createElement('div');
      empty.className = 'files-tree-loading';
      empty.textContent = '(empty)';
      container.appendChild(empty);
    } else {
      for (const entry of data.entries) {
        container.appendChild(renderTreeNode(entry, depth));
      }
    }
    _refreshLucideIcons(container);
  } catch (e) {
    container.innerHTML = '<div class="files-tree-loading">Error: ' + (e.message || 'failed') + '</div>';
  }
}

async function toggleDir(node, path, depth) {
  const children = node.querySelector(':scope > .files-tree-children');
  if (!children) return;
  if (node.classList.contains('expanded')) {
    node.classList.remove('expanded');
    expandedDirs.delete(path);
  } else {
    node.classList.add('expanded');
    expandedDirs.add(path);
    if (!children.dataset.loaded) {
      await loadDirInto(path, children, depth + 1);
      children.dataset.loaded = 'true';
    }
  }
  persistExpanded();
}

// Phantom shimmer rows for the file tree while /tree loads — indented bars that
// echo the tree's shape instead of a plain "Loading…" line. `.sk-shimmer` (the
// animated fill) is shared in ui/shared/css/app3.css.
function _treeSkeletonHtml(rows = 9) {
  // [indent-level, bar-width%] — a plausible folder/file mix.
  const shape = [[0, 62], [1, 48], [1, 70], [2, 40], [0, 55], [1, 66], [1, 44], [0, 72], [1, 50]];
  let html = '';
  for (let i = 0; i < rows; i++) {
    const [lvl, w] = shape[i % shape.length];
    html += `<div class="files-tree-sk-row" style="padding-left:${12 + lvl * 16}px;" aria-hidden="true">`
          + `<span class="files-tree-sk-bar sk-shimmer" style="width:${w}%;"></span></div>`;
  }
  return html;
}

async function loadRoot() {
  const tree = document.getElementById('files-tree');
  if (!tree) return;
  tree.innerHTML = _treeSkeletonHtml();
  try {
    const data = await apiFetch('/tree?path=' + encodeURIComponent(currentRoot || ''));
    currentRoot = data.path || currentRoot;
    if (data.project_root) projectRoot = data.project_root;
    renderBreadcrumb(currentRoot, data.parent);
    // Hide/show the checkbox views now we know where the tree is rooted — they
    // only apply at the project root (home).
    applyProdViewClass();
    // Git status marks must be in hand before the first paint in git view,
    // otherwise every row would render blank and then jump when the fetch lands.
    if (prodViewMode === 'git' && !_gitStatusLoaded) await loadGitStatus();

    tree.innerHTML = '';
    if (!data.entries.length) {
      tree.innerHTML = '<div class="files-tree-empty">Empty directory</div>';
    } else {
      for (const entry of data.entries) {
        tree.appendChild(renderTreeNode(entry, 0));
      }
    }
    _refreshLucideIcons(tree);
    // Fire-and-forget: fetch the git +/- line counts and paint the badges once
    // they land (the tree is already visible — counts fill in a beat later).
    loadLineStats();
    kvWrite(KC_CURRENT_ROOT, LS_CURRENT_ROOT, currentRoot);
  } catch (e) {
    tree.innerHTML = '<div class="files-tree-empty">Error: ' + (e.message || 'failed') + '</div>';
    // If the saved root no longer exists, fall back to the project root
    // so the user isn't stranded with a dead breadcrumb.
    if (currentRoot && currentRoot !== projectRoot) {
      currentRoot = '';
      kvDelete(KC_CURRENT_ROOT, LS_CURRENT_ROOT);
    }
  }
}

// ── Breadcrumb ────────────────────────────────────────────────────
// The clickable path segments were removed in favour of the simpler
// Up + Home buttons; we just render the current path as a read-only
// label so the user can still see where the tree is rooted.

function renderBreadcrumb(absPath, parentPath) {
  const pathEl = document.getElementById('files-current-path');
  if (pathEl) {
    pathEl.textContent = absPath || '';
    pathEl.title = absPath || '';
  }

  const upBtn = document.getElementById('files-breadcrumb-up');
  if (upBtn) {
    upBtn.disabled = !parentPath;
    upBtn.title = parentPath ? 'Go to parent: ' + parentPath : 'No parent directory';
    upBtn.onclick = () => { if (parentPath) setRoot(parentPath); };
  }
  const homeBtn = document.getElementById('files-breadcrumb-home');
  if (homeBtn) {
    const atHome = !projectRoot || projectRoot === absPath;
    homeBtn.disabled = atHome;
    homeBtn.title = atHome ? 'Already at project root' : 'Back to project root: ' + projectRoot;
    homeBtn.onclick = () => { if (projectRoot) setRoot(projectRoot); };
  }
}

async function setRoot(absPath) {
  if (!absPath || absPath === currentRoot) return;
  currentRoot = absPath;
  // Keep expandedDirs intact — entries are keyed by absolute path, so
  // a stale path simply doesn't match anything in the new tree, while
  // navigating up then back down preserves the user's expanded folders.
  await loadRoot();
}

// ── Tabs ──────────────────────────────────────────────────────────

function renderTabs() {
  const fileBar = document.getElementById('files-tabs');
  if (!fileBar) return;
  fileBar.innerHTML = '';
  for (const tab of openTabs) {
    const bar = fileBar;
    const isActive = !activeCommitKey && tab.path === activeFilePath;
    const el = document.createElement('div');
    el.className = 'files-tab'
      + (isActive ? ' active' : '')
      + (tab.dirty ? ' dirty' : '')
      + (tab.closing ? ' closing' : '');
    el.dataset.path = tab.path;
    el.draggable = true;
    el.title = tab.path;

    const iconWrap = document.createElement('span');
    iconWrap.className = 'files-tab-icon';
    const iconI = document.createElement('i');
    iconI.setAttribute('data-lucide', fileIconName(tab.name));
    iconI.className = 'lucide-icon';
    iconWrap.appendChild(iconI);
    el.appendChild(iconWrap);

    const label = document.createElement('span');
    label.className = 'files-tab-label';
    label.textContent = tab.name;
    el.appendChild(label);

    // ── 3-dot "more" menu button (rename/delete/wrap/preview/find) ──
    const more = document.createElement('button');
    more.className = 'files-tab-more';
    more.type = 'button';
    more.title = 'More actions';
    more.draggable = false;
    const moreI = document.createElement('i');
    moreI.setAttribute('data-lucide', 'more-vertical');
    moreI.className = 'lucide-icon';
    more.appendChild(moreI);
    more.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0) {
        e.preventDefault();
        showTabMenu(tab, more);
      }
    });
    more.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });
    more.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(more);

    const close = document.createElement('button');
    close.className = 'files-tab-close';
    close.type = 'button';
    close.title = tab.closing ? 'Closing…' : 'Close (middle-click also works)';
    close.draggable = false;
    close.disabled = !!tab.closing;
    const xI = document.createElement('i');
    // Swap the X for a spinner while the backend DELETE is in flight so the
    // user can see the close is being verified.
    xI.setAttribute('data-lucide', tab.closing ? 'loader-2' : 'x');
    xI.className = 'lucide-icon' + (tab.closing ? ' files-tab-spin' : '');
    xI.style.pointerEvents = 'none';
    close.appendChild(xI);
    // The parent `el` is draggable, which can swallow the click into a
    // potential drag operation on some browsers. Closing on mousedown is
    // the most reliable path; stop propagation so the tab's mousedown
    // (middle-click handler) and dragstart don't also fire.
    close.addEventListener('mousedown', (e) => {
      e.stopPropagation();
      if (e.button === 0 || e.button === 1) {
        e.preventDefault();
        closeTab(tab.path);
      }
    });
    close.addEventListener('click', (e) => {
      e.stopPropagation();
      e.preventDefault();
    });
    close.addEventListener('dragstart', (e) => { e.preventDefault(); e.stopPropagation(); });
    el.appendChild(close);

    el.addEventListener('click', () => activateTab(tab.path));
    el.addEventListener('mousedown', (e) => {
      if (e.button === 1) {
        e.preventDefault();
        closeTab(tab.path);
      }
    });

    // ── Drag-and-drop reordering ──
    el.addEventListener('dragstart', (e) => {
      dragSrcPath = tab.path;
      el.classList.add('dragging');
      try { e.dataTransfer.setData('text/plain', tab.path); } catch (_) {}
      e.dataTransfer.effectAllowed = 'move';
    });
    el.addEventListener('dragend', () => {
      dragSrcPath = null;
      el.classList.remove('dragging');
      document.querySelectorAll('.files-tab.drop-before, .files-tab.drop-after')
        .forEach((t) => t.classList.remove('drop-before', 'drop-after'));
    });
    el.addEventListener('dragover', (e) => {
      if (!dragSrcPath || dragSrcPath === tab.path) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      el.classList.toggle('drop-before', before);
      el.classList.toggle('drop-after', !before);
    });
    el.addEventListener('dragleave', () => {
      el.classList.remove('drop-before', 'drop-after');
    });
    el.addEventListener('drop', (e) => {
      e.preventDefault();
      if (!dragSrcPath || dragSrcPath === tab.path) return;
      const rect = el.getBoundingClientRect();
      const before = (e.clientX - rect.left) < rect.width / 2;
      reorderTab(dragSrcPath, tab.path, before);
    });

    bar.appendChild(el);
  }
  for (const tab of commitTabs) fileBar.appendChild(buildCommitTabElement(tab));
  _refreshLucideIcons(fileBar);
  updateTabCarousel();
}

function buildCommitTabElement(tab) {
  const el = document.createElement('div');
  el.className = 'files-tab files-commit-tab' + (tab.key === activeCommitKey ? ' active' : '');
  el.dataset.commitKey = tab.key;
  el.title = tab.repoLabel + ': ' + (tab.subject || tab.hash);

  const iconWrap = document.createElement('span');
  iconWrap.className = 'files-tab-icon';
  const icon = document.createElement('i');
  icon.setAttribute('data-lucide', tab.state === 'loading' ? 'loader-2' : 'git-commit-horizontal');
  icon.className = 'lucide-icon' + (tab.state === 'loading' ? ' files-tab-spin' : '');
  iconWrap.appendChild(icon);
  el.appendChild(iconWrap);

  const label = document.createElement('span');
  label.className = 'files-tab-label';
  label.textContent = 'Commit ' + tab.shortHash;
  el.appendChild(label);

  const close = document.createElement('button');
  close.className = 'files-tab-close';
  close.type = 'button';
  close.title = 'Close commit details';
  const closeIcon = document.createElement('i');
  closeIcon.setAttribute('data-lucide', 'x');
  closeIcon.className = 'lucide-icon';
  close.appendChild(closeIcon);
  close.addEventListener('mousedown', (event) => {
    event.stopPropagation();
    if (event.button === 0 || event.button === 1) {
      event.preventDefault();
      closeCommitTab(tab.key);
    }
  });
  close.addEventListener('click', (event) => {
    event.stopPropagation();
    event.preventDefault();
  });
  el.appendChild(close);

  el.addEventListener('click', () => activateCommitTab(tab.key));
  el.addEventListener('mousedown', (event) => {
    if (event.button === 1) {
      event.preventDefault();
      closeCommitTab(tab.key);
    }
  });
  return el;
}

function reorderTab(srcPath, destPath, before) {
  const srcIdx = openTabs.findIndex((t) => t.path === srcPath);
  const destIdx = openTabs.findIndex((t) => t.path === destPath);
  if (srcIdx < 0 || destIdx < 0) return;
  const [src] = openTabs.splice(srcIdx, 1);
  let insertAt = openTabs.findIndex((t) => t.path === destPath);
  if (!before) insertAt += 1;
  openTabs.splice(insertAt, 0, src);
  renderTabs();
  persistTabs();
}


function showTabMenu(tab, anchorBtn) {
  const rect = anchorBtn.getBoundingClientRect();
  const items = [
    { icon: 'pencil',     label: 'Rename…', action: () => renameTab(tab.path) },
    { icon: 'trash-2',    label: 'Delete…', danger: true, action: () => deleteTab(tab.path) },
    { icon: 'refresh-cw', label: 'Refresh',  action: () => refreshTab(tab.path) },
    { icon: 'wrap-text',  label: 'Wrap',     checked: !!tab.wrap, action: () => toggleWrap(tab.path) },
  ];
  if (isMarkdownFile(tab.name)) {
    items.push({ icon: 'eye', label: 'Markdown preview', checked: !!tab.preview, action: () => togglePreview(tab.path) });
  }
  // Fold level — number of bracket nesting levels to keep open (for .json files)
  if (/\.json$/i.test(tab.name)) {
    items.push({
      field: true, label: 'Fold level',
      fieldType: 'number',
      value: String(tab.foldLevel != null ? tab.foldLevel : 10),
      placeholder: '10',
      onSave: (v) => {
        const level = Math.max(0, parseInt(v, 10) || 10);
        tab.foldLevel = level;
        // Re-render the pane's fold highlights
        const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(tab.path) + '"]');
        if (pane) {
          const ta = pane.querySelector('textarea.files-textarea-overlay');
          if (ta) scheduleHighlight(pane);
        }
        persistTabs();
      }
    });
  }
  const findDisabled = !!tab.binary || isImageFile(tab.name) || (isMarkdownFile(tab.name) && tab.preview);
  items.push({ icon: 'search', label: 'Find / Replace…', disabled: findDisabled, action: () => openFindBarForActiveTab(tab.path, false) });
  // Right-align under the button
  openFloatingMenu(items, rect.bottom + 2, rect.right - 180);
}

function togglePreview(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  tab.preview = !tab.preview;
  // Rebuild this pane on next render
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
  if (pane) pane.remove();
  renderEditorPanes();
  persistTabs();
}

function openFindBarForActiveTab(path, withReplace) {
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
  if (pane) openFindBar(pane, !!withReplace);
}

function showTreeContextMenu(entry, x, y) {
  const isOpen = !!openTabs.find((t) => t.path === entry.path);

  let items;
  if (entry.is_dir) {
    items = [
      { icon: 'file-plus',   label: 'New File…',   action: () => newInFolder(entry.path, 'file') },
      { icon: 'folder-plus', label: 'New Folder…', action: () => newInFolder(entry.path, 'dir')  },
      { separator: true },
      { icon: 'pencil',      label: 'Rename…',     action: () => renameEntry(entry) },
      { icon: 'trash-2',     label: 'Delete…',     danger: true, action: () => deleteEntry(entry) },
      { separator: true },
      (prodFolderState(entry.path) === 'checked'
        ? { icon: 'package-x',     label: 'Exclude from production', action: () => toggleProdExclude(entry) }
        : prodFolderState(entry.path) === 'indeterminate'
          ? { icon: 'package-check', label: 'Include everything in production', action: () => toggleProdExclude(entry) }
          : { icon: 'package-check', label: 'Include in production', checked: true, action: () => toggleProdExclude(entry) }),
      { separator: true },
      { icon: 'refresh-cw',  label: 'Refresh',     action: () => loadRoot() },
      { icon: 'copy',        label: 'Copy path',   action: () => copyPath(entry.path) },
    ];
  } else {
    items = [
      { icon: 'file',     label: isOpen ? 'Focus tab' : 'Open', action: () => openFile(entry.path, entry.name) },
      { separator: true },
      { icon: 'pencil',   label: 'Rename…', action: () => renameEntry(entry) },
      { icon: 'trash-2',  label: 'Delete…', danger: true, action: () => deleteEntry(entry) },
      { separator: true },
      isProdExcluded(entry.path)
        ? { icon: 'package-check', label: 'Include in production', checked: true, action: () => toggleProdExclude(entry) }
        : { icon: 'package-x',     label: 'Exclude from production', action: () => toggleProdExclude(entry) },
      { separator: true },
      { icon: 'copy',     label: 'Copy path', action: () => copyPath(entry.path) },
    ];
  }
  openFloatingMenu(items, y, x);
}

// ── Tree actions (rename / delete / new-in-folder / copy path) ────

async function renameEntry(entry) {
  const newName = prompt('Rename to:', entry.name);
  if (!newName || newName === entry.name) return;
  // Replace the basename in the absolute path
  const parts = entry.path.split('/');
  parts[parts.length - 1] = newName;
  const newPath = parts.join('/');
  try {
    const r = await apiFetch('/rename', {
      method: 'POST',
      body: JSON.stringify({ path: entry.path, new_path: newPath }),
    });
    const finalPath = r.to || newPath;
    // If the renamed entry (or any descendant under a renamed folder) is
    // currently open as a tab, update those tabs in place so saves still
    // hit the right file.
    const prefix = entry.path + '/';
    for (const tab of openTabs) {
      if (tab.path === entry.path) {
        tab.path = finalPath;
        tab.name = finalPath.split('/').pop();
      } else if (entry.is_dir && tab.path.startsWith(prefix)) {
        tab.path = finalPath + tab.path.slice(entry.path.length);
      }
    }
    if (activeFilePath === entry.path) activeFilePath = finalPath;
    renderTabs();
    renderEditorPanes();
    persistTabs();
    await loadRoot();
  } catch (e) {
    alert('Rename failed: ' + e.message);
  }
}

async function deleteEntry(entry) {
  const what = entry.is_dir ? 'folder (and all its contents)' : 'file';
  if (!confirm('Delete ' + entry.name + ' ' + what + '?\n\n' + entry.path + '\n\nThis cannot be undone.')) return;
  try {
    await apiFetch('/delete', {
      method: 'POST',
      body: JSON.stringify({ path: entry.path }),
    });
    // Close any open tabs for this entry (or anything under it if folder)
    const prefix = entry.path + '/';
    const toClose = openTabs
      .filter((t) => t.path === entry.path || (entry.is_dir && t.path.startsWith(prefix)))
      .map((t) => t.path);
    for (const p of toClose) {
      const tab = openTabs.find((t) => t.path === p);
      if (tab) tab.dirty = false;
      closeTab(p);
    }
    await loadRoot();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

async function newInFolder(folderPath, kind) {
  const name = prompt('New ' + (kind === 'dir' ? 'folder' : 'file') + ' name:');
  if (!name) return;
  const newPath = folderPath.replace(/\/+$/, '') + '/' + name;
  try {
    await apiFetch('/create', {
      method: 'POST',
      body: JSON.stringify({ path: newPath, kind }),
    });
    // Make sure the parent folder is expanded so the new entry is visible
    expandedDirs.add(folderPath);
    persistExpanded();
    await loadRoot();
    if (kind === 'file') openFile(newPath, name);
  } catch (e) {
    alert('Create failed: ' + e.message);
  }
}

async function copyPath(path) {
  // copyText handles insecure http://<ip> contexts (e.g. phones on the LAN)
  // via an execCommand fallback; prompt() is the last resort if even that fails.
  try {
    await copyText(path);
  } catch (_) {
    prompt('Copy path:', path);
  }
}

async function renameTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  const newPath = prompt('Rename to (absolute or relative to project root):', tab.path);
  if (!newPath || newPath === tab.path) return;
  try {
    const r = await apiFetch('/rename', {
      method: 'POST',
      body: JSON.stringify({ path: tab.path, new_path: newPath }),
    });
    const newAbs = r.to || newPath;
    // Update the tab in place
    tab.path = newAbs;
    tab.name = newAbs.split('/').pop();
    if (activeFilePath === path) activeFilePath = newAbs;
    // The editor pane keys panes by data-path; update it too
    const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.dataset.path = newAbs;
    renderTabs();
    renderEditorPanes();
    persistTabs();
    await loadRoot();
  } catch (e) {
    alert('Rename failed: ' + e.message);
  }
}

async function deleteTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (!confirm('Delete ' + tab.name + ' from disk?\n\n' + tab.path + '\n\nThis cannot be undone.')) return;
  try {
    await apiFetch('/delete', {
      method: 'POST',
      body: JSON.stringify({ path: tab.path }),
    });
    // File is gone — drop the dirty flag so closeTab doesn't prompt
    tab.dirty = false;
    closeTab(path);
    await loadRoot();
  } catch (e) {
    alert('Delete failed: ' + e.message);
  }
}

async function refreshTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.dirty && !confirm('Discard unsaved changes and reload from disk?')) return;
  try {
    const data = await apiFetch('/read?path=' + encodeURIComponent(tab.path));
    tab.content = data.content;
    tab.binary = data.binary;
    tab.encoding = data.encoding;
    tab.size = data.size;
    tab.dirty = false;
    // Replace the pane so the textarea picks up the new content cleanly
    const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.remove();
    renderTabs();
    renderEditorPanes();
  } catch (e) {
    alert('Refresh failed: ' + e.message);
  }
}

function toggleWrap(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  tab.wrap = !tab.wrap;
  const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
  const ta = pane && pane.querySelector('textarea.files-textarea');
  if (ta) ta.classList.toggle('wrap', tab.wrap);
  // Highlight overlay also needs to wrap so it lines up with the textarea
  const hl = pane && pane.querySelector('.files-code-highlight');
  if (hl) hl.classList.toggle('wrap', tab.wrap);
  persistTabs();
}

// ── Tab carousel ──────────────────────────────────────────────────

const CAROUSEL_BARS = [
  { bar: 'files-tabs',      prev: 'files-tabs-prev',      next: 'files-tabs-next' },
];

function _updateCarouselFor(barId, prevId, nextId) {
  const bar = document.getElementById(barId);
  const prev = document.getElementById(prevId);
  const next = document.getElementById(nextId);
  if (!bar || !prev || !next) return;
  const overflow = bar.scrollWidth > bar.clientWidth + 1;
  if (!overflow) {
    prev.style.display = 'none';
    next.style.display = 'none';
    return;
  }
  prev.style.display = 'inline-flex';
  next.style.display = 'inline-flex';
  prev.disabled = bar.scrollLeft <= 0;
  next.disabled = bar.scrollLeft + bar.clientWidth >= bar.scrollWidth - 1;
}

function updateTabCarousel() {
  for (const c of CAROUSEL_BARS) _updateCarouselFor(c.bar, c.prev, c.next);
}

let carouselObservers = [];
function initTabCarousel() {
  const SCROLL_STEP = 160;
  for (const c of CAROUSEL_BARS) {
    const bar = document.getElementById(c.bar);
    const prev = document.getElementById(c.prev);
    const next = document.getElementById(c.next);
    if (!bar || !prev || !next) continue;
    prev.addEventListener('click', () => { bar.scrollBy({ left: -SCROLL_STEP, behavior: 'smooth' }); });
    next.addEventListener('click', () => { bar.scrollBy({ left:  SCROLL_STEP, behavior: 'smooth' }); });
    bar.addEventListener('scroll', updateTabCarousel, { passive: true });
    applyRubberBand(bar);

    // Auto-scroll while dragging a tab past either edge
    [c.prev, c.next].forEach((id) => {
      const btn = document.getElementById(id);
      if (!btn) return;
      btn.addEventListener('dragover', (e) => {
        e.preventDefault();
        const dir = id === c.prev ? -1 : 1;
        bar.scrollBy({ left: dir * 40, behavior: 'auto' });
      });
    });

    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(updateTabCarousel);
      observer.observe(bar);
      carouselObservers.push(observer);
    }
  }
  window.addEventListener('resize', updateTabCarousel);
}

function renderEditorPanes() {
  const fileContent = document.getElementById('files-content');
  if (!fileContent) return;

  const fileTabs = openTabs;

  // Welcome placeholder — shown when there are no open workspace tabs. Skip if a
  // welcome is already on screen, so it doesn't flicker on every render.
  function showWelcome(host, html) {
    if (!host) return;
    if (host.querySelector('.files-welcome')) return;
    host.innerHTML = html;
    _refreshLucideIcons(host);
  }
  if (!fileTabs.length && !commitTabs.length) {
    showWelcome(fileContent, `
      <div class="files-welcome">
        <i data-lucide="folder-tree" class="lucide-icon files-welcome-icon"></i>
        <div class="files-welcome-title">File Editor</div>
        <div class="files-welcome-text">Pick a file from the Explorer on the left to open it here.</div>
      </div>`);
  }

  // Re-use existing panes; create missing ones; remove closed ones.
  function syncContainer(host, tabs) {
    if (!host) return;
    const existing = new Map();
    host.querySelectorAll('.files-editor-pane[data-path]').forEach((p) => existing.set(p.dataset.path, p));
    for (const [path, pane] of existing) {
      if (!tabs.find((t) => t.path === path)) {
        pane.remove();
        existing.delete(path);
      }
    }
    if (tabs.length) {
      const welcome = host.querySelector('.files-welcome');
      if (welcome) welcome.remove();
    }
    for (const tab of tabs) {
      let pane = existing.get(tab.path);
      const wantMode = paneModeForTab(tab);
      if (pane && pane.dataset.mode !== wantMode) {
        pane.remove();
        pane = null;
      }
      if (pane && wantMode === 'text') {
        const ta = pane.querySelector('textarea.files-textarea');
        if (ta && ta.value !== tab.content) {
          ta.value = tab.content || '';
          updateHighlightOverlay(pane);
        }
      }
      if (!pane) {
        pane = buildPaneForTab(tab, wantMode);
        host.appendChild(pane);
      }
      pane.classList.toggle('active', !activeCommitKey && tab.path === activeFilePath);
    }
  }
  syncContainer(fileContent, fileTabs);

  const commitPanes = new Map();
  fileContent.querySelectorAll('.files-commit-pane').forEach((pane) => {
    commitPanes.set(pane.dataset.commitKey, pane);
  });
  for (const [key, pane] of commitPanes) {
    if (!commitTabs.find((tab) => tab.key === key)) pane.remove();
  }
  if (commitTabs.length) {
    const welcome = fileContent.querySelector('.files-welcome');
    if (welcome) welcome.remove();
  }
  for (const tab of commitTabs) {
    let pane = commitPanes.get(tab.key);
    if (pane && pane.dataset.state !== tab.state) {
      pane.remove();
      pane = null;
    }
    if (!pane) {
      pane = document.createElement('div');
      pane.className = 'files-editor-pane files-commit-pane';
      pane.dataset.commitKey = tab.key;
      pane.dataset.state = tab.state;
      const heading = document.createElement('div');
      heading.className = 'files-commit-heading';
      heading.textContent = tab.repoLabel + ' · Commit ' + tab.shortHash;
      pane.appendChild(heading);
      const body = document.createElement('div');
      body.className = 'files-commit-body';
      if (tab.state === 'loading') body.innerHTML = '<div class="fg-main-loading">Loading commit...</div>';
      else if (tab.state === 'error') { body.className += ' fg-error'; body.textContent = tab.error || 'Could not load commit.'; }
      else body.innerHTML = '<div class="fg-main-commit">' + renderCommitDetail(tab.detail || {}) + '</div>';
      pane.appendChild(body);
      fileContent.appendChild(pane);
    }
    pane.classList.toggle('active', tab.key === activeCommitKey);
  }

  const activeFile = !activeCommitKey && activeFilePath
    ? openTabs.find((t) => t.path === activeFilePath)
    : null;
  updateStatusBar(activeFile || null);
}

// ── Per-tab pane mode ─────────────────────────────────────────────

function paneModeForTab(tab) {
  if (isImageFile(tab.name)) return 'image';
  if (tab.binary)             return 'binary';
  if (isMarkdownFile(tab.name) && tab.preview) return 'markdown';
  return 'text';
}

function isImageFile(name) {
  return /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i.test(name);
}
function isMarkdownFile(name) {
  return /\.(md|markdown|mdx)$/i.test(name);
}

function imageDataUrl(tab) {
  const ext = (tab.name.split('.').pop() || '').toLowerCase();
  const mime = {
    png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg',
    gif: 'image/gif', webp: 'image/webp', bmp: 'image/bmp',
    ico: 'image/x-icon', svg: 'image/svg+xml', avif: 'image/avif',
  }[ext] || 'application/octet-stream';
  if (tab.encoding === 'base64') return 'data:' + mime + ';base64,' + tab.content;
  // utf-8 SVG: percent-encode and inline
  return 'data:' + mime + ';utf8,' + encodeURIComponent(tab.content || '');
}

function buildPaneForTab(tab, mode) {
  const pane = document.createElement('div');
  pane.className = 'files-editor-pane';
  pane.dataset.path = tab.path;
  pane.dataset.mode = mode;

  if (mode === 'image') {
    pane.classList.add('files-image-pane');
    const img = document.createElement('img');
    img.className = 'files-image-preview';
    img.alt = tab.name;
    img.src = imageDataUrl(tab);
    pane.appendChild(img);
  } else if (mode === 'binary') {
    pane.innerHTML =
      '<div class="files-binary-msg">' +
        '<i data-lucide="file-warning" class="lucide-icon"></i>' +
        '<div>Binary file (' + formatBytes(tab.size) + ')</div>' +
        '<div style="margin-top:6px;font-size:11px;opacity:0.7;">Editing binary files is not supported here.</div>' +
      '</div>';
    _refreshLucideIcons(pane);
  } else if (mode === 'markdown') {
    const md = document.createElement('div');
    md.className = 'files-markdown-preview';
    md.innerHTML = renderMarkdown(tab.content || '');
    pane.appendChild(md);
  } else {
    // text mode: textarea (transparent) overlaid on a <pre> for Prism
    buildTextEditorPane(pane, tab);
  }
  return pane;
}

// ── Text editor pane (textarea + Prism overlay + find bar) ────────

function buildTextEditorPane(pane, tab) {
  const findBar = buildFindBar(tab);
  pane.appendChild(findBar);

  const wrap = document.createElement('div');
  wrap.className = 'files-code-wrap';

  const highlight = document.createElement('pre');
  highlight.className = 'files-code-highlight';
  highlight.setAttribute('aria-hidden', 'true');
  const code = document.createElement('code');
  code.className = 'language-' + getPrismLang(tab.name);
  highlight.appendChild(code);
  wrap.appendChild(highlight);

  // Fold gutter — holds clickable triangle buttons for code folding
  const gutter = document.createElement('div');
  gutter.className = 'files-fold-gutter';
  gutter.setAttribute('aria-hidden', 'true');
  wrap.appendChild(gutter);
  pane._foldPairs = [];
  pane._foldedSet = new Set();

  const ta = document.createElement('textarea');
  ta.className = 'files-textarea files-textarea-overlay' + (tab.wrap ? ' wrap' : '');
  ta.spellcheck = false;
  ta.autocomplete = 'off';
  ta.autocapitalize = 'off';
  ta.value = tab.content || '';

  function syncCursorLine() {
    if (!ta || !tab || tab.binary) return;
    const before = ta.value.slice(0, ta.selectionStart);
    const line = before.split('\n').length;
    if (tab._cursorLine !== line) {
      tab._cursorLine = line;
      return true; // changed
    }
    return false;
  }
  ta.addEventListener('input', () => {
    tab.content = ta.value;
    if (!tab.dirty) {
      tab.dirty = true;
      renderTabs();
    }
    syncCursorLine();
    updateStatusBar(tab);
    scheduleHighlight(pane);
  });
  ta.addEventListener('scroll', () => {
    highlight.scrollTop = ta.scrollTop;
    highlight.scrollLeft = ta.scrollLeft;
    const gutter = wrap.querySelector('.files-fold-gutter');
    if (gutter) gutter.scrollTop = ta.scrollTop;
    // Save scroll position for persistence across refreshes
    const pos = kvRead(KC_SCROLL_POS, LS_SCROLL_POS) || {};
    pos[tab.path] = ta.scrollTop;
    kvWrite(KC_SCROLL_POS, LS_SCROLL_POS, pos);
  });
  ta.addEventListener('click', () => { if (syncCursorLine()) updateStatusBar(tab); });
  ta.addEventListener('keyup', () => { if (syncCursorLine()) updateStatusBar(tab); });
  ta.addEventListener('focus', () => { if (syncCursorLine()) updateStatusBar(tab); });
  ta.addEventListener('keydown', (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      saveTab(tab.path);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault();
      openFindBar(pane, /* withReplace */ false);
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'h') {
      e.preventDefault();
      openFindBar(pane, /* withReplace */ true);
      return;
    }
    if (e.key === 'Tab' && !e.shiftKey) {
      e.preventDefault();
      const start = ta.selectionStart;
      const end = ta.selectionEnd;
      ta.value = ta.value.slice(0, start) + '  ' + ta.value.slice(end);
      ta.selectionStart = ta.selectionEnd = start + 2;
      tab.content = ta.value;
      if (!tab.dirty) { tab.dirty = true; renderTabs(); }
      updateStatusBar(tab);
      scheduleHighlight(pane);
    }
  });
  // JSON error popover — click on the error line shows the parse error
  wireJsonErrorClick(ta, pane);
  // Code folding auto-unfold when cursor enters a folded region
  wireFoldAutoUnfold(ta, pane);
  wrap.appendChild(ta);
  pane.appendChild(wrap);

  // Restore scroll position after layout settles
  try {
    const pos = kvRead(KC_SCROLL_POS, LS_SCROLL_POS) || {};
    const saved = pos[tab.path];
    if (saved != null) {
      requestAnimationFrame(() => {
        ta.scrollTop = saved;
        highlight.scrollTop = saved;
        const g = wrap.querySelector('.files-fold-gutter');
        if (g) g.scrollTop = saved;
      });
    }
  } catch (_) {}

  // Initial highlight pass — done lazily so the autoloader has a tick
  // to fetch the grammar.
  scheduleHighlight(pane);
}

// ── JSON validation ───────────────────────────────────────────────
// Validates .json file content with JSON.parse and returns structured
// error info (message, position, line, column) or null when valid.

function validateJsonContent(text) {
  if (typeof text !== 'string') return null;
  try {
    JSON.parse(text);
    return null;
  } catch (e) {
    const msg = e.message;
    const posMatch = msg.match(/position\s+(\d+)/);
    const position = posMatch ? parseInt(posMatch[1], 10) : 0;
    const clampedPos = Math.max(0, Math.min(position, text.length));
    const before = text.slice(0, clampedPos);
    const lines = before.split('\n');
    const line = lines.length;
    const column = lines[lines.length - 1].length + 1;
    return { message: msg, position: clampedPos, line, column };
  }
}

// ── Syntax highlighting (Prism, debounced) ────────────────────────

function getPrismLang(name) {
  const ext = (name.split('.').pop() || '').toLowerCase();
  return {
    js: 'javascript', mjs: 'javascript', cjs: 'javascript',
    ts: 'typescript', tsx: 'tsx', jsx: 'jsx',
    py: 'python', rb: 'ruby', go: 'go', rs: 'rust', java: 'java',
    c: 'c', cpp: 'cpp', cc: 'cpp', h: 'c', hpp: 'cpp',
    html: 'markup', htm: 'markup', xml: 'markup', svg: 'markup',
    css: 'css', scss: 'scss', sass: 'sass', less: 'less',
    json: 'json', json5: 'json5', yaml: 'yaml', yml: 'yaml', toml: 'toml',
    md: 'markdown', mdx: 'markdown',
    sh: 'bash', bash: 'bash', zsh: 'bash',
    sql: 'sql', php: 'php', swift: 'swift', kt: 'kotlin',
    dockerfile: 'docker', makefile: 'makefile',
  }[ext] || 'plain';
}

const _highlightTimers = new WeakMap();
function scheduleHighlight(pane) {
  const prev = _highlightTimers.get(pane);
  if (prev) cancelAnimationFrame(prev);
  const id = requestAnimationFrame(() => updateHighlightOverlay(pane));
  _highlightTimers.set(pane, id);
}

function updateHighlightOverlay(pane) {
  const code = pane && pane.querySelector('.files-code-highlight > code');
  const ta = pane && pane.querySelector('textarea.files-textarea-overlay');
  if (!code || !ta) return;
  const value = ta.value;
  const langClass = (code.className.match(/language-(\S+)/) || [])[1] || 'plain';

  // Recalculate foldable bracket pairs from the raw text
  pane._foldPairs = langClass === 'json' ? findBracketPairs(value) : [];

  // Apply fold level from the tab — fold pairs deeper than the level
  const tab = openTabs.find(t => t.path === pane.dataset.path);
  const foldLevel = (tab && tab.foldLevel != null) ? tab.foldLevel : 10;
  if (pane._foldPairs.length) {
    pane._foldedSet = new Set();
    for (let i = 0; i < pane._foldPairs.length; i++) {
      if (pane._foldPairs[i].depth > foldLevel) {
        pane._foldedSet.add(i);
      }
    }
  } else {
    pane._foldedSet = new Set();
  }

  // Build modified text with folded regions replaced by markers
  const { text: foldText, markers } = buildFoldMarkersText(value, pane._foldPairs, pane._foldedSet);

  // Append a newline so the highlight has the same trailing-line height
  // as the textarea (which always reserves a blank line for the caret).
  if (window.Prism && window.Prism.highlight) {
    if (langClass !== 'plain') {
      const grammar = window.Prism.languages[langClass];
      if (grammar) {
        try {
          code.innerHTML = window.Prism.highlight(foldText + '\n', grammar, langClass);
          postProcessFoldMarkers(code, markers);
          applyJsonErrorMarkers(pane, code, value, langClass);
          applyFoldGutter(pane, pane._foldPairs, pane._foldedSet, langClass);
          return;
        } catch (_) {}
      } else if (window.Prism.plugins && window.Prism.plugins.autoloader) {
        window.Prism.plugins.autoloader.loadLanguages([langClass], () => {
          if (document.body.contains(pane)) updateHighlightOverlay(pane);
        });
      }
    }
  }
  // Fallback / pre-Prism state / plain text: just show the raw value.
  code.textContent = foldText + '\n';
  postProcessFoldMarkers(code, markers);
  applyJsonErrorMarkers(pane, code, value, langClass);
  applyFoldGutter(pane, pane._foldPairs, pane._foldedSet, langClass);
}

// ── JSON error markers (red wavy underline + click popover) ──────

function clearJsonErrorMarkers(pane) {
  const existing = pane && pane.querySelector('.json-error-squiggle');
  if (existing) existing.remove();
  const popover = pane && pane.querySelector('.json-error-popover');
  if (popover) popover.remove();
  delete pane._jsonError;
}

function applyJsonErrorMarkers(pane, code, value, langClass) {
  clearJsonErrorMarkers(pane);
  if (langClass !== 'json') return;

  const error = validateJsonContent(value);
  if (!error) return;

  // Store error on the pane so click handlers can read it
  pane._jsonError = error;

  // Inject a squiggle marker inside the <code> element so it scrolls
  // with the content. The squiggle spans the full width of the error
  // line and shows a red wavy underline.
  const squiggle = document.createElement('div');
  squiggle.className = 'json-error-squiggle';
  // Position the squiggle at the error line's Y offset.
  // The <code> element sits inside the <pre> which has 8px padding-top;
  // each line is 13px font × 1.5 line-height = 19.5px.
  squiggle.style.top = ((error.line - 1) * 19.5) + 'px';
  code.appendChild(squiggle);
}

// ── JSON error popover (floating tooltip anchored to textarea) ───

function showJsonErrorPopover(pane, error, textarea) {
  // Remove any existing popover
  const old = document.querySelector('.json-error-popover');
  if (old) old.remove();

  const popover = document.createElement('div');
  popover.className = 'json-error-popover';
  popover.textContent = error.message;
  document.body.appendChild(popover);

  // Position below the error line
  const wrapRect = textarea.getBoundingClientRect();
  const lineY = 8 + (error.line - 1) * 19.5;
  const lineTop = wrapRect.top + lineY;
  const lineLeft = wrapRect.left + 36; // match padding-left (36px)

  popover.style.top  = Math.min(lineTop + 24, window.innerHeight - popover.offsetHeight - 8) + 'px';
  popover.style.left = Math.max(8, Math.min(lineLeft, window.innerWidth - popover.offsetWidth - 8)) + 'px';

  // Dismiss on click-away or Escape
  const dismiss = (e) => {
    if (!popover.contains(e.target) && e.target !== textarea) {
      popover.remove();
      cleanup();
    }
  };
  const onKey = (e) => {
    if (e.key === 'Escape') { popover.remove(); cleanup(); }
  };
  const cleanup = () => {
    document.removeEventListener('mousedown', dismiss, true);
    document.removeEventListener('keydown', onKey, true);
  };
  setTimeout(() => {
    document.addEventListener('mousedown', dismiss, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}

// Wire JSON error popover click on the textarea (called once per textarea creation)
function wireJsonErrorClick(ta, pane) {
  ta.addEventListener('click', () => {
    const error = pane._jsonError;
    if (!error) return;
    // Only show if the cursor is on the error line
    const taValue = ta.value;
    const before = taValue.slice(0, ta.selectionStart);
    const cursorLine = before.split('\n').length;
    if (cursorLine === error.line) {
      showJsonErrorPopover(pane, error, ta);
    }
  });
}

// ── Code folding (bracket collapse) ──────────────────────────────

// Find all multi-line bracket pairs ({…} or […]) in JSON text.
// Returns [{ startPos, startLine, startCol, endPos, endLine, endCol, type, depth }]
function findBracketPairs(text) {
  const pairs = [];
  const stack = [];
  let line = 1, col = 0;
  let inString = false;

  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (ch === '\\') { i++; continue; }
      if (ch === '"') inString = false;
      continue;
    }

    col++;
    if (ch === '\n') { line++; col = 0; continue; }

    if (ch === '"') { inString = true; continue; }
    if (ch === '{' || ch === '[') {
      stack.push({ char: ch, pos: i, line, col, depth: stack.length + 1 });
    } else if (ch === '}' || ch === ']') {
      const expected = ch === '}' ? '{' : '[';
      if (stack.length && stack[stack.length - 1].char === expected) {
        const start = stack.pop();
        if (start.line < line) { // multi-line only
          pairs.push({
            startPos: start.pos, startLine: start.line, startCol: start.col,
            endPos: i, endLine: line, endCol: col,
            type: start.char === '{' ? 'brace' : 'bracket',
            depth: start.depth
          });
        }
      }
    }
  }
  return pairs;
}

// Marker prefix/suffix — ASCII-only, Prism JSON won't tokenize these chars
const _FOLD_PREF = '___FOLD_';
const _FOLD_SUFF = '___';

// Replace the inner content of each folded region with a unique text marker.
// Returns { text, markers[] } where markers carry the info to post-process.
function buildFoldMarkersText(value, folds, foldedSet) {
  if (!foldedSet || !foldedSet.size) return { text: value, markers: [] };

  const active = folds
    .map((f, i) => ({ ...f, idx: i }))
    .filter(f => foldedSet.has(f.idx))
    .sort((a, b) => b.startPos - a.startPos); // right-to-left so offsets stay valid

  if (!active.length) return { text: value, markers: [] };

  const markers = [];
  let result = value;
  for (const f of active) {
    const lines = f.endLine - f.startLine;
    const marker = `${_FOLD_PREF}${f.startPos}_${lines}_${f.type}${_FOLD_SUFF}`;
    markers.push({ idx: f.idx, marker, line: f.startLine, endLine: f.endLine, type: f.type, lines });
    // Keep the opening bracket, replace inner content + closing bracket with marker
    result = result.slice(0, f.startPos + 1) + marker + result.slice(f.endPos);
  }
  return { text: result, markers };
}

// After Prism highlighting, replace marker strings in the code element's innerHTML
// with styled fold-placeholder spans.
function postProcessFoldMarkers(code, markers) {
  if (!markers || !markers.length) return;
  let html = code.innerHTML;
  for (const m of markers) {
    const label = m.lines === 1 ? '1 line' : `${m.lines} lines`;
    html = html.replace(
      m.marker,
      `<span class="fold-collapsed" data-fold-idx="${m.idx}">${m.type === 'brace' ? '…}' : '…]'}</span>`
    );
  }
  code.innerHTML = html;
}

// Populate the fold gutter with clickable triangle buttons for each foldable line.
function applyFoldGutter(pane, folds, foldedSet, langClass) {
  const gutter = pane && pane.querySelector('.files-fold-gutter');
  if (!gutter) return;
  gutter.innerHTML = '';

  if (langClass !== 'json' || !folds.length) return;

  const LINE_H = 19.5; // 13px font × 1.5 line-height

  for (let i = 0; i < folds.length; i++) {
    const f = folds[i];
    const isFolded = foldedSet.has(i);
    const btn = document.createElement('button');
    btn.className = 'fold-btn' + (isFolded ? ' folded' : '');
    btn.dataset.foldIdx = i;
    btn.setAttribute('aria-label', isFolded ? 'Expand' : 'Collapse');
    // Position at the foldable line's Y offset (the opening bracket's line)
    btn.style.top = ((f.startLine - 1) * LINE_H) + 'px';
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleFoldState(pane, parseInt(btn.dataset.foldIdx, 10));
    });
    gutter.appendChild(btn);
  }
}

// Toggle a fold on/off and re-render the highlight.
function toggleFoldState(pane, idx) {
  if (!pane._foldedSet) return;
  if (pane._foldedSet.has(idx)) {
    pane._foldedSet.delete(idx);
  } else {
    pane._foldedSet.add(idx);
  }
  // Re-trigger highlight on the textarea
  const ta = pane.querySelector('textarea.files-textarea-overlay');
  if (ta) scheduleHighlight(pane);
}

// Auto-unfold when the cursor lands inside a folded region.
function wireFoldAutoUnfold(ta, pane) {
  ta.addEventListener('click', () => {
    if (!pane._foldedSet || !pane._foldedSet.size) return;
    const before = ta.value.slice(0, ta.selectionStart);
    const cursorLine = before.split('\n').length;

    // Check if any fold range spans the cursor line
    for (const idx of pane._foldedSet) {
      const f = pane._foldPairs[idx];
      if (!f) continue;
      // Folded region covers lines f.startLine+1 to f.endLine-1
      if (cursorLine > f.startLine && cursorLine < f.endLine) {
        pane._foldedSet.delete(idx);
        scheduleHighlight(pane);
        return; // unfold one at a time
      }
    }
  });
}

// ── Markdown rendering ────────────────────────────────────────────

function renderMarkdown(src) {
  try {
    if (window.marked && typeof window.marked.parse === 'function') {
      return window.marked.parse(src || '');
    }
  } catch (_) {}
  // Fallback if marked hasn't loaded: escape and show as preformatted text.
  const escaped = (src || '').replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));
  return '<pre style="white-space:pre-wrap;">' + escaped + '</pre>';
}

// ── Find / Replace bar ────────────────────────────────────────────

function buildFindBar(tab) {
  const bar = document.createElement('div');
  bar.className = 'files-findbar';
  bar.hidden = true;
  bar.innerHTML =
    '<input type="text" class="files-findbar-input files-findbar-find"    placeholder="Find" spellcheck="false" autocomplete="off" data-lpignore="true" data-1p-ignore="true">' +
    '<span class="files-findbar-count">0 / 0</span>' +
    '<button type="button" class="files-findbar-btn" data-act="prev"  title="Previous (Shift+Enter)"><i data-lucide="chevron-up" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-findbar-btn" data-act="next"  title="Next (Enter)"><i data-lucide="chevron-down" class="lucide-icon"></i></button>' +
    '<button type="button" class="files-findbar-btn" data-act="case"  title="Match case">Aa</button>' +
    '<input type="text" class="files-findbar-input files-findbar-replace" placeholder="Replace" spellcheck="false" autocomplete="off" data-lpignore="true" data-1p-ignore="true">' +
    '<button type="button" class="files-findbar-btn" data-act="replace"     title="Replace">↩</button>' +
    '<button type="button" class="files-findbar-btn" data-act="replace-all" title="Replace all">↩↩</button>' +
    '<button type="button" class="files-findbar-btn" data-act="close" title="Close (Esc)"><i data-lucide="x" class="lucide-icon"></i></button>';

  const findInput    = bar.querySelector('.files-findbar-find');
  const replaceInput = bar.querySelector('.files-findbar-replace');
  const countEl      = bar.querySelector('.files-findbar-count');
  const caseBtn      = bar.querySelector('[data-act="case"]');
  let caseSensitive = false;

  function getTextarea() {
    const pane = bar.closest('.files-editor-pane');
    return pane && pane.querySelector('textarea.files-textarea-overlay');
  }

  function allMatches() {
    const ta = getTextarea();
    const needle = findInput.value;
    if (!ta || !needle) return [];
    const hay = caseSensitive ? ta.value : ta.value.toLowerCase();
    const n   = caseSensitive ? needle   : needle.toLowerCase();
    const out = [];
    let i = 0;
    while ((i = hay.indexOf(n, i)) !== -1) {
      out.push(i);
      i += Math.max(1, n.length);
    }
    return out;
  }

  function refresh(active) {
    const matches = allMatches();
    countEl.textContent = matches.length ? ((typeof active === 'number' ? active + 1 : 1) + ' / ' + matches.length) : '0 / 0';
    return matches;
  }

  function findCurrentIndex(matches) {
    const ta = getTextarea();
    if (!ta || !matches.length) return -1;
    const caret = ta.selectionStart;
    for (let i = 0; i < matches.length; i++) {
      if (matches[i] >= caret) return i;
    }
    return matches.length - 1;
  }

  function jumpTo(matches, idx) {
    const ta = getTextarea();
    if (!ta || !matches.length) return;
    const i = ((idx % matches.length) + matches.length) % matches.length;
    const start = matches[i];
    const end = start + findInput.value.length;
    ta.focus();
    ta.setSelectionRange(start, end);
    // Make sure the selection is visible (textarea doesn't auto-scroll on programmatic selection)
    const before = ta.value.slice(0, start);
    const line = before.split('\n').length;
    const lineHeight = parseFloat(getComputedStyle(ta).lineHeight) || 18;
    const target = (line - 3) * lineHeight;
    if (ta.scrollTop > target || ta.scrollTop + ta.clientHeight < target + lineHeight * 4) {
      ta.scrollTop = Math.max(0, target);
    }
    refresh(i);
  }

  function next() {
    const matches = refresh();
    if (!matches.length) return;
    jumpTo(matches, findCurrentIndex(matches));
  }
  function prev() {
    const matches = refresh();
    if (!matches.length) return;
    const cur = findCurrentIndex(matches);
    jumpTo(matches, cur - 1);
  }
  function replaceOne() {
    const ta = getTextarea();
    if (!ta || !findInput.value) return;
    const selected = ta.value.substring(ta.selectionStart, ta.selectionEnd);
    const eq = caseSensitive ? selected === findInput.value : selected.toLowerCase() === findInput.value.toLowerCase();
    if (eq) {
      const start = ta.selectionStart;
      const replacement = replaceInput.value;
      ta.setRangeText(replacement, start, ta.selectionEnd, 'end');
      ta.dispatchEvent(new Event('input', { bubbles: true }));
    }
    next();
  }
  function replaceAll() {
    const ta = getTextarea();
    if (!ta || !findInput.value) return;
    const needle = findInput.value;
    const flags = caseSensitive ? 'g' : 'gi';
    const escaped = needle.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
    const re = new RegExp(escaped, flags);
    const newVal = ta.value.replace(re, replaceInput.value);
    if (newVal !== ta.value) {
      ta.value = newVal;
      ta.dispatchEvent(new Event('input', { bubbles: true }));
      refresh();
    }
  }

  findInput.addEventListener('input', () => refresh());
  findInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')  { e.preventDefault(); e.shiftKey ? prev() : next(); }
    if (e.key === 'Escape') { e.preventDefault(); closeFindBar(bar); }
  });
  replaceInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter')  { e.preventDefault(); replaceOne(); }
    if (e.key === 'Escape') { e.preventDefault(); closeFindBar(bar); }
  });
  bar.querySelector('[data-act="next"]').addEventListener('click', next);
  bar.querySelector('[data-act="prev"]').addEventListener('click', prev);
  bar.querySelector('[data-act="replace"]').addEventListener('click', replaceOne);
  bar.querySelector('[data-act="replace-all"]').addEventListener('click', replaceAll);
  bar.querySelector('[data-act="close"]').addEventListener('click', () => closeFindBar(bar));
  caseBtn.addEventListener('click', () => {
    caseSensitive = !caseSensitive;
    caseBtn.classList.toggle('active', caseSensitive);
    refresh();
  });
  return bar;
}

function openFindBar(pane, withReplace) {
  const bar = pane && pane.querySelector('.files-findbar');
  if (!bar) return;
  const wasHidden = bar.hidden;
  bar.classList.toggle('with-replace', !!withReplace);
  bar.hidden = false;
  _refreshLucideIcons(bar);
  const input = withReplace
    ? bar.querySelector('.files-findbar-replace')
    : bar.querySelector('.files-findbar-find');
  // Pre-fill find input with the current selection only when opening fresh
  if (wasHidden) {
    const ta = pane.querySelector('textarea.files-textarea-overlay');
    if (ta) {
      const sel = ta.value.substring(ta.selectionStart, ta.selectionEnd);
      if (sel && !sel.includes('\n')) {
        bar.querySelector('.files-findbar-find').value = sel;
      }
    }
  }
  setTimeout(() => { if (input) { input.focus(); input.select(); } }, 0);
}

function closeFindBar(bar) {
  bar.hidden = true;
  bar.classList.remove('with-replace');
  const pane = bar.closest('.files-editor-pane');
  const ta = pane && pane.querySelector('textarea.files-textarea-overlay');
  if (ta) ta.focus();
}

function formatBytes(n) {
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' KB';
  return (n / 1024 / 1024).toFixed(2) + ' MB';
}

function updateStatusBar(tab) {
  const info = document.getElementById('files-status-info');
  if (!info) return;
  if (!tab) {
    info.textContent = '';
    info.classList.remove('dirty');
    return;
  }
  info.classList.toggle('dirty', !!tab.dirty);
  info.innerHTML = '';
  if (!tab.binary) {
    const lineNum = document.createElement('span');
    lineNum.className = 'files-cursor-line';
    lineNum.textContent = 'line ' + (tab._cursorLine || 1);
    info.appendChild(lineNum);
    info.appendChild(document.createTextNode(' '));
  }
  const meta = document.createElement('span');
  meta.textContent = tab.dirty ? 'Unsaved changes' : (tab.binary ? 'binary' : 'Saved');
  info.appendChild(meta);
  if (!tab.binary) {
    const btn = document.createElement('button');
    btn.className = 'files-save-btn';
    btn.textContent = 'Save';
    btn.disabled = !tab.dirty;
    btn.addEventListener('click', () => saveTab(tab.path));
    info.appendChild(btn);
  }
}

// ── Tab actions ───────────────────────────────────────────────────

async function openFile(path, name) {
  const existing = openTabs.find((t) => t.path === path);
  if (existing) {
    activateTab(path);
    return;
  }

  let data;
  try {
    data = await apiFetch('/read?path=' + encodeURIComponent(path));
  } catch (e) {
    alert('Failed to open file: ' + e.message);
    return;
  }

  const tabName = name || path.split('/').pop();
  openTabs.push({
    path: data.path || path,
    name: tabName,
    content: data.content,
    dirty: false,
    binary: data.binary,
    encoding: data.encoding,
    size: data.size,
    _cursorLine: 1,
    // .md files open in rendered preview by default; toggle in tab menu
    // switches back to raw editing. Other types open as plain text.
    preview: isMarkdownFile(tabName),
  });
  activeFilePath = path;
  activeCommitKey = null;
  setSelectedGitCommit(null, null);
  renderTabs();
  renderEditorPanes();
  persistTabs();
}

function activateTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  activeFilePath = path;
  activeCommitKey = null;
  setSelectedGitCommit(null, null);
  renderTabs();
  // Just flip active class on the matching pane instead of re-rendering.
  const host = document.getElementById('files-content');
  if (host) {
    host.querySelectorAll('.files-editor-pane').forEach((p) => {
      p.classList.toggle('active', p.dataset.path === path);
    });
  }
  updateStatusBar(tab);
  // Bring the activated tab into view if it's outside the visible window.
  const tabEl = document.querySelector('#files-tabs .files-tab[data-path="' + cssEscape(path) + '"]');
  if (tabEl && typeof tabEl.scrollIntoView === 'function') {
    tabEl.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }
  kvWrite(KC_ACTIVE_FILE, LS_ACTIVE_FILE, path);
}

async function closeTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab) return;
  if (tab.closing) return;          // already in-flight; ignore repeat clicks
  if (tab.dirty) {
    if (!confirm('Discard unsaved changes to ' + tab.name + '?')) return;
  }
  const idx = openTabs.findIndex((t) => t.path === path);
  if (idx < 0) return;              // another close finished first
  openTabs.splice(idx, 1);
  const host = document.getElementById('files-content');
  if (host) {
    const pane = host.querySelector('.files-editor-pane[data-path="' + cssEscape(path) + '"]');
    if (pane) pane.remove();
  }
  // Promote the neighbouring file tab to active.
  if (activeFilePath === path) {
    activeFilePath = openTabs.length ? openTabs[Math.min(idx, openTabs.length - 1)].path : null;
  }
  renderTabs();
  renderEditorPanes();
  persistTabs();
}

async function openCommitTab(info) {
  if (!info || !info.hash) return;
  const key = (info.repoId || 'webagent') + ':' + info.hash;
  const existing = commitTabs.find((tab) => tab.key === key);
  if (existing) {
    activateCommitTab(key);
    return;
  }
  const tab = {
    key,
    repoId: info.repoId || 'webagent',
    repoLabel: info.repoLabel || 'WebAgent (this app)',
    hash: info.hash,
    shortHash: info.shortHash || info.hash.slice(0, 7),
    subject: info.subject || '',
    state: 'loading',
    detail: null,
    error: '',
  };
  commitTabs.push(tab);
  activateCommitTab(key);
  try {
    tab.detail = await loadCommitDetail(tab.hash);
    tab.state = 'ready';
  } catch (error) {
    tab.state = 'error';
    tab.error = error?.message || String(error || 'Could not load commit.');
  }
  if (commitTabs.includes(tab)) {
    renderTabs();
    renderEditorPanes();
  }
}

function activateCommitTab(key) {
  const tab = commitTabs.find((item) => item.key === key);
  if (!tab) return;
  activeCommitKey = key;
  renderTabs();
  setSelectedGitCommit(tab.repoId, tab.hash);
  renderEditorPanes();
  const tabEl = document.querySelector('#files-tabs .files-tab[data-commit-key="' + cssEscape(key) + '"]');
  if (tabEl && typeof tabEl.scrollIntoView === 'function') {
    tabEl.scrollIntoView({ inline: 'nearest', block: 'nearest', behavior: 'smooth' });
  }
}

function closeCommitTab(key) {
  const index = commitTabs.findIndex((tab) => tab.key === key);
  if (index < 0) return;
  const wasActive = activeCommitKey === key;
  commitTabs.splice(index, 1);
  if (wasActive) {
    const neighbour = commitTabs[Math.min(index, commitTabs.length - 1)] || null;
    activeCommitKey = neighbour ? neighbour.key : null;
  }
  renderTabs();
  const selected = activeCommitKey ? commitTabs.find((tab) => tab.key === activeCommitKey) : null;
  setSelectedGitCommit(selected?.repoId || null, selected?.hash || null);
  renderEditorPanes();
  if (!activeCommitKey && activeFilePath) updateStatusBar(openTabs.find((tab) => tab.path === activeFilePath) || null);
}

function cssEscape(s) {
  if (window.CSS && CSS.escape) return CSS.escape(s);
  return s.replace(/(["\\])/g, '\\$1');
}

async function saveTab(path) {
  const tab = openTabs.find((t) => t.path === path);
  if (!tab || tab.binary) return;
  try {
    await apiFetch('/write', {
      method: 'POST',
      body: JSON.stringify({ path: tab.path, content: tab.content, encoding: 'utf-8' }),
    });
    tab.dirty = false;
    renderTabs();
    updateStatusBar(tab);
    // Saving changes the file's line count vs HEAD — refresh the tree badges.
    loadLineStats();
    // If the saved file is chat_ui.json, notify the chat panel so in-memory
    // snapshots and pill toggles re-read the fresh value without a refresh.
    if (path === 'data/config/chat_ui.json' || path.endsWith('/data/config/chat_ui.json')) {
      try { window.dispatchEvent(new CustomEvent('chat-ui-file-saved', { detail: { path } })); } catch (_) {}
    }
  } catch (e) {
    alert('Save failed: ' + e.message);
  }
}

// ── New file / folder ─────────────────────────────────────────────

async function promptNew(kind) {
  const name = prompt('New ' + kind + ' path (relative to project root):');
  if (!name) return;
  try {
    await apiFetch('/create', {
      method: 'POST',
      body: JSON.stringify({ path: name, kind }),
    });
    // Refresh root listing; if the parent of `name` is currently expanded,
    // refresh that subtree instead so the new entry shows up in context.
    await loadRoot();
    if (kind === 'file') {
      openFile(name, name.split('/').pop());
    }
  } catch (e) {
    alert('Create failed: ' + e.message);
  }
}


// ── Persistence ───────────────────────────────────────────────────

function persistTabs() {
  const minimal = openTabs.map((t) => (
    { path: t.path, name: t.name, wrap: !!t.wrap, preview: !!t.preview, foldLevel: t.foldLevel }
  ));
  kvWrite(KC_OPEN_TABS, LS_OPEN_TABS, minimal);
  kvWrite(KC_ACTIVE_FILE, LS_ACTIVE_FILE, activeFilePath || '');
}

function persistExpanded() {
  kvWrite(KC_EXPANDED, LS_EXPANDED, Array.from(expandedDirs));
}

function restoreState() {
  const saved = kvRead(KC_EXPANDED, LS_EXPANDED);
  if (Array.isArray(saved)) expandedDirs = new Set(saved);
  const r = kvRead(KC_CURRENT_ROOT, LS_CURRENT_ROOT);
  if (r) currentRoot = r;
  const v = kvRead(KC_PROD_VIEW, LS_PROD_VIEW);
  if (v === 'dev' || v === 'git') prodViewMode = v;
}

async function restoreOpenTabs() {
  try {
    const saved = kvRead(KC_OPEN_TABS, LS_OPEN_TABS) || [];
    // Fall back to the legacy unified active-tab key for users who saved their
    // state before the explorer/terminal split.
    const legacyActive = localStorage.getItem(LS_ACTIVE_TAB) || '';
    let wantFile = kvRead(KC_ACTIVE_FILE, LS_ACTIVE_FILE) || '';
    if (!wantFile && legacyActive && !legacyActive.startsWith('terminal:')) {
      wantFile = legacyActive;
    }
    if (!Array.isArray(saved) || !saved.length) return;
    // Open in order, swallow failures (a file may have been deleted). Terminal
    // tabs in a pre-split saved list are ignored — the Terminal drop-in owns
    // and restores those from its own key now.
    for (const t of saved) {
      if (t && t.kind === 'terminal') continue;
      try {
        await openFile(t.path, t.name);
        const opened = openTabs.find((o) => o.path === t.path);
        if (!opened) continue;
        if (t.wrap)       opened.wrap = true;
        if (t.preview)    opened.preview = true;
        if (t.foldLevel != null) opened.foldLevel = t.foldLevel;
        if (t.wrap || t.preview || t.foldLevel != null) {
          // Re-render so the wrap class / preview mode / fold level get applied
          const pane = document.querySelector('.files-editor-pane[data-path="' + cssEscape(opened.path) + '"]');
          if (pane) pane.remove();
        }
      } catch (_) {}
    }
    renderEditorPanes();
    if (wantFile && openTabs.find((t) => t.path === wantFile)) {
      activateTab(wantFile);
    }
  } catch (_) {}
}


function restoreExplorerSidebarMode() {
  let mode = localStorage.getItem(LS_SIDEBAR_MODE);
  const legacyView = localStorage.getItem('files.sidebarView');
  if (mode !== 'files' && mode !== 'git') mode = legacyView === 'git' ? 'git' : 'files';
  if (legacyView === 'git') {
    try { localStorage.setItem('files.sidebarView', 'explorer'); } catch (_) {}
  }
  return mode;
}

function syncExplorerSidebarPanels() {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  const mode = sidebar.dataset.explorerMode || 'files';
  const hiddenByShell = sidebar.dataset.view !== 'explorer' || sidebar.dataset.state === 'strip';
  sidebar.querySelectorAll('.files-sidebar-panel[data-view="explorer"][data-explorer-mode]').forEach((panel) => {
    panel.hidden = hiddenByShell || panel.dataset.explorerMode !== mode;
  });
}

function activateSidebarModeWork() {
  stopLineStatsAutoRefresh();
  stopGitAutoRefresh();
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar || sidebar.dataset.view !== 'explorer' || sidebar.dataset.state === 'strip') return;
  if ((sidebar.dataset.explorerMode || 'files') === 'git') {
    if (!gitPanelOpened) {
      gitPanelOpened = true;
      openGitPanel(sidebar, { onCommitSelected: openCommitTab }).catch((error) => {
        gitPanelOpened = false;
        const body = document.getElementById('fg-body');
        if (body) {
          body.innerHTML = '';
          const message = document.createElement('div');
          message.className = 'fg-error';
          message.textContent = error?.message || 'Could not load Git Control.';
          body.appendChild(message);
        }
      });
    }
    startGitAutoRefresh(sidebar);
  } else {
    loadLineStats();
    startLineStatsAutoRefresh();
  }
}

function setExplorerSidebarMode(mode, { persist = true } = {}) {
  const sidebar = document.getElementById('files-sidebar');
  if (!sidebar) return;
  const next = mode === 'git' ? 'git' : 'files';
  sidebar.dataset.explorerMode = next;
  if (persist) {
    try { localStorage.setItem(LS_SIDEBAR_MODE, next); } catch (_) {}
  }
  syncExplorerSidebarPanels();
  activateSidebarModeWork();
}

// ── Lifecycle (drop-in view hooks called by the Admin-Tools frame) ─
// The main-page shell dispatches these via explorer/page.json's start/stop.
// One-time wiring
// (toolbar buttons, tab carousel, tree/prod load, tab restore) is guarded by
// _explInit; the git +/- badge poll (re)starts on every activation.
let _explInit = false;

export async function startView() {
  if (!_explInit) {
    _explInit = true;
    restoreState();

    // Toolbar buttons (previously wired in the frame's initFiles).
    setExplorerSidebarMode(restoreExplorerSidebarMode(), { persist: true });
    const refreshBtn = document.getElementById('files-refresh');
    if (refreshBtn) refreshBtn.addEventListener('click', loadRoot);

    const showGitBtn = document.getElementById('files-show-git');
    if (showGitBtn) showGitBtn.addEventListener('click', () => setExplorerSidebarMode('git'));

    const showExplorerBtn = document.getElementById('files-show-explorer');
    if (showExplorerBtn) {
      showExplorerBtn.addEventListener('click', () => setExplorerSidebarMode('files'));
    }

    const newFileBtn = document.getElementById('files-new-file');
    if (newFileBtn) newFileBtn.addEventListener('click', () => promptNew('file'));

    const newFolderBtn = document.getElementById('files-new-folder');
    if (newFolderBtn) newFolderBtn.addEventListener('click', () => promptNew('dir'));

    const collapseBtn = document.getElementById('files-collapse-all');
    if (collapseBtn) {
      collapseBtn.addEventListener('click', () => {
        expandedDirs.clear();
        persistExpanded();
        loadRoot();
      });
    }

    // Toolbar "More" → production popover (preview toggle + Copy + Push).
    const moreBtn = document.getElementById('files-more-btn');
    if (moreBtn) {
      moreBtn.addEventListener('click', (e) => {
        e.stopPropagation();
        openDevelopmentMenu(moreBtn);
      });
    }

    initTabCarousel();
    installFilesDropGuard();
    renderTabs();
    renderEditorPanes();

    // Production-mirror exclude list first (dev-only badges render on first
    // paint), then the tree + dev/prod view mode, then restore open file tabs.
    await loadProdExcludes();
    loadGitStatus();    // fire-and-forget — fills tracked/ignored marks when they land
    loadProdConfig();   // fire-and-forget — populates the More menu's folder field
    await loadRoot();
    applyProdViewClass();
    if (!openTabs.length) await restoreOpenTabs();
  }

  syncExplorerSidebarPanels();
  activateSidebarModeWork();

  // Deep-link: chat controls set __explorerOpenFileOnStart before switching
  // to File Manager; open that file now. Consumed once.
  const pending = window.__explorerOpenFileOnStart;
  if (pending && pending.path) {
    window.__explorerOpenFileOnStart = null;
    openFile(pending.path, pending.name || pending.path.split('/').pop());
    setExplorerSidebarMode('files');
  }
}

export function stopView() {
  stopLineStatsAutoRefresh();
  stopGitAutoRefresh();
}

// Re-arm the one-time init guard so a RE-MOUNT into a fresh container re-wires
// its (fresh) toolbar and restores persisted state. The File Manager is now
// embedded as a tab inside the Instances page, where the host is created/destroyed
// on each tile open — see ui/main-panel/app/instances.js _unmountFileManager.
// The standalone main-panel page usage never calls this: its #tab-explorer mount
// persists, so _explInit stays true and listeners are never double-wired.
export function resetExplorerForRemount() {
  _explInit = false;
  gitPanelOpened = false;
  _filesDropGuardInstalled = false;
  closeFloatingMenu();
  resetGitForRemount();
  window.removeEventListener('resize', updateTabCarousel);
  for (const observer of carouselObservers) observer.disconnect();
  carouselObservers = [];
}

// Deep-link helper for other modules (e.g. the chat Thinking pill): open a
// file in the Explorer editor by absolute path. Idempotent — re-opening an
// already-open tab just activates it.
export function openFileAt(path, name) {
  if (!path) return;
  return openFile(path, name || path.split('/').pop());
}

// Pending-file deep link: chat controls set window.__explorerOpenFileOnStart
// before switching to File Manager; Explorer opens the file on start. One-shot.
window.__explorerOpenFileOnStart = window.__explorerOpenFileOnStart || null;

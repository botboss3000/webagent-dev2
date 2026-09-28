'use strict';

/* git_changes — Local changes + commit graph card plugin.
   MOVED from the Instances Overview tab ("Local changes" section) to a dashboard
   card. Uses the SAME endpoints as the Git Control page (/api/v1/github/status
   + /log-graph, both ?fetch=0 local-only so it's fast and works offline) and the
   SAME shared graph renderer (renderGraphRow / drawGraph from shared/files-git.js),
   so rows/badges/lanes look identical to the Git Control page.

   This card is self-contained (no snapshot section): it fetches on mount and
   re-fetches on an internal interval (30s, pauseable via the Auto toggle) + the
   Refresh button. The shell re-renders card bodies every poll, so wiring is
   re-applied through a MutationObserver on the grid; only the persistent
   .dash-card element carries listeners (delegated click), so nothing double-fires.
   REMOVE-WHEN: the Dashboard tab is dropped from the Instances page. */

import { apiPath } from '../../../../../shared/js/config.js';
import { _esc, _escAttr, _refreshLucideIcons } from '../../../../../shared/js/dom-utils.js';
import { renderGraphRow, drawGraph } from '../../../../../shared/js/files-git.js';
import { applyRubberBand } from '../../../../../shared/js/rubber-band.js';

const CARD_TYPE = 'git_changes';
const POLL_MS = 30000;   // internal git refresh (the shell's 5s poll re-renders, it does not re-fetch)

// ── module state (survives shell re-renders; shared by every git_changes card) ──
const GIT = {
  status: null,     // /api/v1/github/status body
  graph: null,      // /api/v1/github/log-graph body
  loading: false,   // true while a fetch is in flight
  started: false,   // initial fetch fired
  polling: true,    // auto-refresh interval enabled
  timer: null,
  abort: null,      // AbortController for the in-flight action
  result: null,     // { msg, tone: 'ok'|'err'|'info'|'warn', spin } — survives shell re-renders
};

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch (e) { return ''; } }

async function _get(path) {
  const sep = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(path + sep + 'requesting_user_id=' + encodeURIComponent(_uid())));
  let body = null;
  try { body = await res.json(); } catch (e) {}
  return { ok: res.ok, status: res.status, body };
}

async function _post(path, payload, opts) {
  const res = await fetch(apiPath(path), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ requesting_user_id: _uid(), ...(payload || {}) }),
    signal: (opts && opts.signal) || null,
  });
  let body = null;
  try { body = await res.json(); } catch (e) {}
  return { ok: res.ok, status: res.status, body };
}

// ── data loading ─────────────────────────────────────────────────────────────
// Same pair the Overview fetched: status + commit graph, both local-only. A silent
// background refresh keeps the current data on screen (no skeleton flash); a first
// load shows the skeleton.
async function _fetchGit(silent) {
  if (silent && GIT.status && GIT.graph) {
    try {
      const [st, gr] = await Promise.all([
        _get('/api/v1/github/status?fetch=0'),
        _get('/api/v1/github/log-graph?limit=50&fetch=0'),
      ]);
      GIT.status = (st.ok && st.body) || GIT.status;
      GIT.graph = (gr.ok && gr.body) || GIT.graph;
      _rerender();
    } catch (e) {}
    return;
  }
  GIT.loading = true;
  _rerender();
  try {
    const [st, gr] = await Promise.all([
      _get('/api/v1/github/status?fetch=0'),
      _get('/api/v1/github/log-graph?limit=50&fetch=0'),
    ]);
    GIT.status = (st.ok && st.body) || null;
    GIT.graph = (gr.ok && gr.body) || null;
  } catch (e) {
    GIT.status = null;
    GIT.graph = null;
  }
  GIT.loading = false;
  _rerender();
}

function _start() {
  if (GIT.started) return;
  GIT.started = true;
  _fetchGit(false);
  GIT.timer = setInterval(() => {
    if (!GIT.polling) return;
    if (document.hidden) return;
    if (!document.querySelector('.dash-card[data-type="' + CARD_TYPE + '"]')) return;
    _fetchGit(true);
  }, POLL_MS);
}

// ── render (the plugin contract — re-called by the shell on every poll) ──────
function _bodyHtml() {
  const s = GIT.status;
  const g = GIT.graph;
  const loading = GIT.loading;

  // Changes in order: staged → unstaged → untracked (same as the Overview section)
  const items = s ? [
    ...(s.staged || []).map(f => ({ ...f, area: 'staged' })),
    ...(s.unstaged || []).map(f => ({ ...f, area: 'unstaged' })),
    ...(s.untracked || []).map(f => ({ flag: '??', path: f.path || f, area: 'untracked' })),
  ] : [];
  const count = items.length;

  let filesBody = '';
  if (loading && !s) {
    filesBody = '<div class="gc-file-list">'
      + '<div class="gc-file gc-skel"><span class="gc-skel-line gc-skel-w1"></span></div>'
      + '<div class="gc-file gc-skel"><span class="gc-skel-line gc-skel-w2"></span></div>'
      + '<div class="gc-file gc-skel"><span class="gc-skel-line gc-skel-w3"></span></div>'
      + '</div>';
  } else if (!s) {
    filesBody = '<div class="gc-clean">Could not load working tree status.</div>';
  } else if (count === 0) {
    filesBody = '<div class="gc-clean">Working tree clean</div>';
  } else {
    filesBody = '<div class="gc-file-list">'
      + items.map(f => {
          const flag = f.flag || 'M';
          const flagClsMap = { M: 'gc-flag-m', A: 'gc-flag-a', D: 'gc-flag-d', R: 'gc-flag-r', '??': 'gc-flag-u' };
          const cls = flagClsMap[flag] || flagClsMap.M;
          const label = ({ M: 'M', A: 'A', D: 'D', R: 'R', '??': '?' })[flag] || flag;
          return '<div class="gc-file" title="' + _esc(f.path) + '">'
            + '<span class="gc-file-flag ' + cls + '">' + label + '</span>'
            + '<span class="gc-file-path">' + _esc(f.path) + '</span>'
            + (Number.isFinite(f.added) && f.added > 0 ? '<span class="gc-file-stat gc-stat-add">+' + f.added + '</span>' : '')
            + (Number.isFinite(f.removed) && f.removed > 0 ? '<span class="gc-file-stat gc-stat-del">-' + f.removed + '</span>' : '')
            + '</div>';
        }).join('')
      + '</div>';
  }

  let graphBody = '';
  if (loading && !g) {
    let skel = '';
    for (let i = 0; i < 4; i++) {
      skel += '<div class="gc-file gc-skel"><span class="gc-skel-line gc-skel-w' + ((i % 3) + 1) + '"></span></div>';
    }
    graphBody = '<div class="gc-file-list">' + skel + '</div>';
  } else if (!g || !g.commits || !g.commits.length) {
    graphBody = '<div class="gc-clean">Could not load commit graph.</div>';
  } else {
    // Rows are painted by _wire() once layout gives us the column; the lane/row
    // vars come from card.css (GRAPH_LANE_W / GRAPH_ROW_H in shared/files-git.js).
    graphBody = '<div class="fg-graph-list"></div>';
  }

  const statusCls = s ? (count ? 'warn' : 'ok') : (loading ? '' : 'err');
  const hint = s
    ? (count ? count + ' uncommitted change' + (count === 1 ? '' : 's') : 'Working tree clean')
    : (loading ? 'Loading working tree…' : 'Could not load working tree status.');

  // Result line — rendered from state so the shell's 5s re-render can't wipe an
  // in-flight commit/push message (the DOM copy is rebuilt on every poll anyway).
  let resultHtml = '<div class="gc-result" hidden></div>';
  if (GIT.result) {
    resultHtml = '<div class="gc-result ' + GIT.result.tone + '">'
      + (GIT.result.spin ? '<span class="gc-result-spin"></span>' : '')
      + _esc(GIT.result.msg)
      + '</div>';
  }

  return '<div class="gc-root">'
    + '<div class="gc-toolrow">'
    +   '<span class="gc-status-dot ' + statusCls + '"></span>'
    +   '<span class="gc-hint">' + hint + '</span>'
    +   '<button type="button" class="gc-toggle" data-gc-act="toggle-poll" title="Auto-refresh working tree status">'
    +     '<span class="gc-toggle-track' + (GIT.polling ? ' on' : '') + '"><span class="gc-toggle-knob"></span></span>'
    +     (GIT.polling ? 'Auto' : 'Paused')
    +   '</button>'
    + '</div>'
    + '<div class="gc-cols">'
    +   '<div class="gc-col gc-col-files">'
    +     '<div class="gc-col-title">Changes' + (s ? '<span class="gc-count">' + count + '</span>' : '') + '</div>'
    +     filesBody
    +   '</div>'
    +   '<div class="gc-divider" role="separator" aria-hidden="true"></div>'
    +   '<div class="gc-col gc-col-graph">'
    +     '<div class="gc-col-title">Commit graph <span class="gc-graph-hint">(all branches)</span></div>'
    +     graphBody
    +   '</div>'
    + '</div>'
    + _actionsHtml(s)
    + resultHtml
    + '</div>';
}

function _actionsHtml(s) {
  const hasChanges = s && s.file_count > 0;
  const hasAhead = s && (s.ahead || 0) > 0;
  const hasRemote = s && s.has_remote;
  const starEnabled = hasChanges || hasAhead;
  return '<div class="gc-actions-wrap">'
    + '<button type="button" class="gc-chev left" aria-label="Scroll actions left" tabindex="-1">❮</button>'
    + '<button type="button" class="gc-chev right" aria-label="Scroll actions right" tabindex="-1">❯</button>'
    + '<div class="gc-actions">'
    + '<button type="button" class="gc-action-btn" data-gc-act="git-commit" ' + (hasChanges ? '' : 'disabled') + ' title="Save changes locally — commits everything, auto-writes the note, and does not push"><i data-lucide="check"></i> Commit</button>'
    + '<button type="button" class="gc-action-btn gc-star" data-gc-act="git-commit-push" ' + (starEnabled ? '' : 'disabled') + ' title="Commit & push now — auto-writes the note, safety-checks, commits & pushes instantly"><i data-lucide="star"></i> Commit &amp; Push</button>'
    + (hasRemote ? '<button type="button" class="gc-action-btn" data-gc-act="git-pull" title="Pull current branch">'
      + (s && s.behind > 0
        ? '<i data-lucide="arrow-up"></i> Pull <span class="gc-ahead-badge">↓' + s.behind + '</span>'
        : '<i data-lucide="arrow-up"></i> Pull')
      + '</button>' : '')
    + (hasRemote ? '<button type="button" class="gc-action-btn" data-gc-act="git-push" title="Push current branch"><i data-lucide="arrow-down"></i> Push</button>' : '')
    + (hasRemote ? '<button type="button" class="gc-action-btn" data-gc-act="git-merge" title="Merge another branch into the current branch"><i data-lucide="git-merge"></i> Merge…</button>' : '')
    + '<button type="button" class="gc-action-btn" data-gc-act="git-refresh" title="Refresh local changes"><i data-lucide="refresh-cw"></i></button>'
    + '</div>'
    + '<div class="gc-merge-menu" hidden role="listbox" aria-label="Pick branch to merge in"><div class="gc-branch-loading">Loading branches…</div></div>'
    + '</div>';
}

// ── wiring ───────────────────────────────────────────────────────────────────
// The shell rebuilds card bodies every poll, so per-element listeners live on the
// persistent .dash-card element (one delegated click) and everything else is
// re-applied by _wire() after each body replacement (MutationObserver on the grid).
function _cardEls() {
  return Array.from(document.querySelectorAll('.dash-card[data-type="' + CARD_TYPE + '"]'));
}

function _rerender() {
  for (const el of _cardEls()) {
    const body = el.querySelector('.dash-card-body');
    if (body) body.innerHTML = _bodyHtml();
  }
  _ensureObserver();
  _wireAll();
}

function _wireAll() {
  for (const el of _cardEls()) _wire(el);
}

function _wire(el) {
  if (!el.dataset.gcClick) {
    el.dataset.gcClick = '1';
    el.addEventListener('click', _onClick);
  }
  // Commit-graph rows: painted by the shared renderer. Repaint ONLY when the
  // commit data actually changed (signature = commit hashes + lane count), NOT
  // by comparing innerHTML: drawGraph() paints <path> children into each row's
  // svg AFTER innerHTML is set, so the serialized DOM always differs from the
  // empty-svg `want` string. With the grid-wide MutationObserver re-invoking
  // _wire() on every change, an innerHTML comparison would spin an endless
  // wipe-and-repaint loop and freeze the tab. The signature rides on the
  // .fg-graph-list element so a shell re-render (fresh list node) repaints once
  // and then converges.
  const list = el.querySelector('.fg-graph-list');
  const g = GIT.graph;
  if (list && g && g.commits && g.commits.length) {
    const sig = g.commits.map(c => c.full_hash).join(',') + '|' + (g.max_lane || 1);
    if (list.dataset.gcSig !== sig) {
      list.dataset.gcSig = sig;
      list.innerHTML = g.commits.map((c, i) => renderGraphRow(c, i, g)).join('');
      drawGraph(el.querySelector('.gc-col-graph') || el, g);
    }
    applyRubberBand(list, { axis: 'y', pullThreshold: 20, maxPull: 50 });
  }
  _wireCarousel(el);
  _refreshLucideIcons(el);
}

let _observedGrid = null;
let _observer = null;
function _ensureObserver() {
  const grid = document.querySelector('.dash-grid');
  if (!grid) return;
  if (_observedGrid !== grid) {
    if (_observer) { _observer.disconnect(); _observer = null; }
    _observedGrid = grid;
    _observer = new MutationObserver(() => _wireAll());
    _observer.observe(grid, { childList: true, subtree: true });
  }
  _wireAll();
}

// Delegated click — survives every body rebuild.
function _onClick(e) {
  const btn = e.target.closest('[data-gc-act]');
  if (!btn) return;
  const el = btn.closest('.dash-card[data-type="' + CARD_TYPE + '"]');
  if (!el) return;
  const act = btn.dataset.gcAct;
  if (act === 'toggle-poll') { GIT.polling = !GIT.polling; _rerender(); return; }
  if (act === 'git-refresh') { GIT.result = null; _fetchGit(false); return; }
  if (act === 'git-commit') { _gitDo(el, 'commit'); return; }
  if (act === 'git-commit-push') { _gitDo(el, 'commit-push'); return; }
  if (act === 'git-pull') { _gitDo(el, 'pull'); return; }
  if (act === 'git-push') { _gitDo(el, 'push'); return; }
  if (act === 'git-merge') { _mergeMenu(el); return; }
}

// Actions carousel — chevrons + drag-to-scroll (ported from the Overview section).
// Guarded by a flag on the wrap: the wrap is rebuilt whenever the shell replaces
// the card body, but the observer may fire several times per rebuild (body swap +
// Lucide icon replacement), which would otherwise stack duplicate listeners.
function _wireCarousel(el) {
  const wrap = el.querySelector('.gc-actions-wrap'); if (!wrap) return;
  if (wrap.dataset.gcCarousel === '1') return;
  wrap.dataset.gcCarousel = '1';
  const scroller = wrap.querySelector('.gc-actions'); if (!scroller) return;
  const chevLeft = wrap.querySelector('.gc-chev.left');
  const chevRight = wrap.querySelector('.gc-chev.right');

  const updateAffordances = () => {
    const maxScroll = scroller.scrollWidth - scroller.clientWidth;
    const atStart = scroller.scrollLeft <= 1;
    const atEnd = scroller.scrollLeft >= maxScroll - 1;
    const overflowing = maxScroll > 1;
    wrap.classList.toggle('can-scroll-left', overflowing && !atStart);
    wrap.classList.toggle('can-scroll-right', overflowing && !atEnd);
  };
  scroller.addEventListener('scroll', updateAffordances, { passive: true });
  applyRubberBand(scroller);
  requestAnimationFrame(updateAffordances);
  setTimeout(updateAffordances, 120);
  if (typeof ResizeObserver !== 'undefined') new ResizeObserver(updateAffordances).observe(scroller);

  const page = () => Math.max(scroller.clientWidth * 0.65, 120);
  if (chevLeft) chevLeft.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left: -page(), behavior: 'smooth' }); });
  if (chevRight) chevRight.addEventListener('click', e => { e.stopPropagation(); scroller.scrollBy({ left: page(), behavior: 'smooth' }); });

  let dragging = false, startX = 0, startScroll = 0, moved = false;
  scroller.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;
    dragging = true; moved = false;
    startX = e.clientX; startScroll = scroller.scrollLeft;
  });
  scroller.addEventListener('pointermove', e => {
    if (!dragging) return;
    const dx = e.clientX - startX;
    if (Math.abs(dx) > 4) { moved = true; try { scroller.setPointerCapture(e.pointerId); } catch (_) {} }
    if (moved) { scroller.scrollLeft = startScroll - dx; e.preventDefault(); }
  });
  const endDrag = e => {
    if (!dragging) return;
    dragging = false;
    try { scroller.releasePointerCapture(e.pointerId); } catch (_) {}
  };
  scroller.addEventListener('pointerup', endDrag);
  scroller.addEventListener('pointercancel', endDrag);
  scroller.addEventListener('click', e => {
    if (moved) { e.stopPropagation(); e.preventDefault(); moved = false; }
  }, true);
}

// ── git actions (mirrored from the Overview's helpers; same /api/v1/github/*) ──
function _showResult(el, msg, tone, opts) {
  GIT.result = {
    msg: msg,
    tone: (tone === 'error' ? 'err' : tone === 'success' ? 'ok' : tone === 'warning' ? 'warn' : 'info'),
    spin: !!(opts && opts.spin),
  };
  const rEl = el.querySelector('.gc-result');
  if (!rEl) return;
  rEl.hidden = false;
  rEl.className = 'gc-result ' + (tone === 'error' ? 'err' : tone === 'success' ? 'ok' : tone === 'warning' ? 'warn' : 'info');
  rEl.textContent = '';
  if (opts && opts.spin) {
    const s = document.createElement('span');
    s.className = 'gc-result-spin';
    rEl.appendChild(s);
  }
  rEl.appendChild(document.createTextNode(msg));
  if (opts && opts.onCancel) {
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'gc-result-cancel';
    cancel.textContent = 'Cancel';
    cancel.setAttribute('aria-label', 'Cancel operation');
    cancel.addEventListener('click', opts.onCancel, { once: true });
    rEl.appendChild(cancel);
  }
}

const _DONE_MSG = {
  commit: 'Committed successfully',
  'commit-push': 'Committed & pushed successfully',
  pull: 'Pull successful',
  push: 'Push successful',
};

async function _gitDo(el, kind, branch) {
  if (GIT.abort) return;
  const controller = new AbortController();
  GIT.abort = controller;
  const busy = kind === 'merge' ? 'Merging ' + branch + '…'
    : kind === 'commit' ? 'Committing…'
    : kind === 'commit-push' ? 'Committing & pushing…'
    : kind === 'pull' ? 'Pulling…' : 'Pushing…';
  const failPrefix = kind === 'merge' ? 'Merge failed: ' : kind === 'commit' ? 'Commit failed: ' : kind === 'commit-push' ? 'Commit & push failed: ' : kind === 'pull' ? 'Pull failed: ' : 'Push failed: ';
  _showResult(el, busy, 'info', { spin: true, onCancel: () => controller.abort() });
  try {
    let r;
    if (kind === 'commit') r = await _post('/api/v1/github/commit-and-push', { skip_push: true, include_untracked: true }, { signal: controller.signal });
    else if (kind === 'commit-push') r = await _post('/api/v1/github/commit-and-push', { include_untracked: true }, { signal: controller.signal });
    else if (kind === 'pull') r = await _post('/api/v1/github/pull', {}, { signal: controller.signal });
    else if (kind === 'push') r = await _post('/api/v1/github/push', {}, { signal: controller.signal });
    else r = await _post('/api/v1/github/merge', { branch }, { signal: controller.signal });
    if (GIT.abort !== controller) return;
    if (r.ok && r.body) {
      _showResult(el, (r.body.message) || _DONE_MSG[kind] || ('Merged ' + branch), 'success');
    } else {
      _showResult(el, failPrefix + ((r.body && r.body.detail) || 'Server error'), 'error');
    }
  } catch (e) {
    if (GIT.abort !== controller) return;
    _showResult(el, controller.signal.aborted ? 'Cancelled.' : failPrefix + e.message,
      controller.signal.aborted ? 'info' : 'error');
  }
  GIT.abort = null;
  _fetchGit(true);
}

async function _mergeMenu(el) {
  const menu = el.querySelector('.gc-merge-menu');
  if (!menu) return;
  if (!menu.hidden) { menu.hidden = true; return; }
  menu.hidden = false;
  menu.innerHTML = '<div class="gc-branch-loading">Loading branches…</div>';
  try {
    const r = await _get('/api/v1/github/branches');
    const branches = (r.ok && r.body && r.body.branches) || [];
    const current = (r.ok && r.body && r.body.current) || '';
    if (!branches.length) {
      menu.innerHTML = '<div class="gc-branch-empty">No other branches.</div>';
      return;
    }
    menu.innerHTML = branches.filter(b => {
      const name = typeof b === 'string' ? b : (b.name || '');
      return name && name !== current;
    }).map(b => {
      const name = typeof b === 'string' ? b : (b.name || '');
      if (!name) return '';
      return '<button type="button" class="gc-branch-opt" data-branch="' + _escAttr(name) + '">' + _esc(name) + '</button>';
    }).join('');
    menu.querySelectorAll('.gc-branch-opt').forEach(opt => {
      opt.addEventListener('click', () => { menu.hidden = true; _gitDo(el, 'merge', opt.dataset.branch); });
    });
  } catch (e) {
    menu.innerHTML = '<div class="gc-branch-err">' + _esc(e.message) + '</div>';
  }
}

// ── plugin contract ─────────────────────────────────────────────────────────
export default {
  render() {
    _start();
    _ensureObserver();
    return _bodyHtml();
  },
};

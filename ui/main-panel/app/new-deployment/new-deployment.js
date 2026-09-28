'use strict';

// New Deployment — mount module for the Instances page's "New instance" tile.
//
// Keeps the big install-a-new-copy machinery OUT of the lean instances.js: the
// markup is a separate fragment (new-deployment.html) and the heavy logic already
// lives in the shared deploy.js / dns.js. This module is the thin glue that:
//   • fetches new-deployment.html ONCE and builds a persistent DOM node,
//   • re-parents that same node into whatever host the current render hands it
//     (so the admin's typed form + expanded rows survive the Instances page's
//     wholesale re-renders — moving a live node keeps its state + listeners),
//   • wires each bar's expand but LAZY-loads that bar's heavy contents only on its
//     first touch — nothing network-bound (and none of the heavy deploy.js/dns.js
//     code) runs on mount, so the tab + its collapsed bars paint instantly. Those
//     two modules are DYNAMIC-imported inside the per-bar loaders (not statically at
//     the top), so opening the tile never downloads them. The deploy catalog
//     (deploy.js initDeploy +
//     __refreshDeploy: provider list, repo prefill, GitHub probe, local checkouts)
//     warms on the first touch of the Repo OR Deploy-target bar (both feed off the
//     one /catalog fetch); the DNS catalog (dns.js initDns) warms on the first touch
//     of the Connect-a-domain bar. Each subsystem loads at most once.
//
// The three bars (Repo to deploy, Deploy target, Connect a domain) were moved
// here out of Data Settings → Deployment; their element ids are unchanged, so
// deploy.js/dns.js drive them from their new home with no edits. deploy.js still
// lives in the data-settings folder because that page keeps using it for the
// current-deployment list + Export/Import codes; we just import it across folders.
// REMOVE-WHEN: the New instance tile drops its New Deployment tab.

import { _refreshLucideIcons } from '../../../shared/js/dom-utils.js';
// deploy.js (~2.5k lines) and dns.js are NOT imported statically — that would make
// them ride THIS module's dynamic import and download+parse before the tab could
// even paint its bars (the old cause of the empty "loading…" gap). They are now
// dynamic-imported inside the per-bar loaders below (_ensureDeploy / _ensureDns),
// so nothing heavy loads until the admin actually opens a bar. This module stays
// tiny → the four collapsed bars appear almost immediately.

// The synthetic tile / active-selection id for the "New instance" tile (never a
// real instance id, so instances.js `_find` returns null for it and branches to
// the New Deployment detail instead).
export const NEW_DEPLOY_ID = '__new_deployment__';

let _node = null;        // the persistent, re-parented deploy DOM node
let _building = null;    // in-flight build promise (dedupes concurrent mounts)
let _wired = false;      // bars + their first-open lazy-loaders wired once
let _deployLoaded = false;  // deploy catalog (initDeploy + __refreshDeploy) fired once
let _dnsLoaded = false;     // DNS catalog (initDns) fired once

// Fetch the fragment + build the detached node (once).
async function _build() {
  const res = await fetch(new URL('./new-deployment.html', import.meta.url));
  const html = await res.text();
  const wrap = document.createElement('div');
  wrap.className = 'inst-nd';
  wrap.innerHTML = html;
  _node = wrap;
}

// ── Lazy loaders ─────────────────────────────────────────────────────────────
// The heavy work (deploy /catalog fetch, GitHub repo probe, local-checkout list,
// DNS provider catalog) is deferred until a bar is actually touched, so the tab
// paints instantly. Each subsystem loads at most once. Repo + Deploy-target share
// the ONE deploy catalog (deploy.js `_load` fills both the repo prefill and the
// provider dropdown), so a touch of either warms deploy; the domain bar warms DNS.
async function _ensureDeploy() {
  if (_deployLoaded) return;
  _deployLoaded = true;
  try {
    const m = await import('../settings/data-settings/deploy.js');
    m.initDeploy();
    // initDeploy sets window.__refreshDeploy = the catalog + repo-prefill + probe load.
    try { window.__refreshDeploy && window.__refreshDeploy(); } catch {}
  } catch (e) { _deployLoaded = false; console.error('[new-deployment] initDeploy failed', e); }
}
async function _ensureDns() {
  if (_dnsLoaded) return;
  _dnsLoaded = true;
  try {
    // initDns wires its listeners AND fetches its provider catalog itself.
    const m = await import('../settings/data-settings/dns.js');
    m.initDns();
  } catch (e) { _dnsLoaded = false; console.error('[new-deployment] initDns failed', e); }
}

// Wire each bar's expand + its first-touch lazy trigger, exactly once.
//   • The three chevron bars (Repo #ac-deploy-repo-row, Configuration
//     #ac-deploy-config-row, Connect-a-domain #ac-dns-main-row) toggle on their
//     header (mirrors data-settings.js `_wireBootRow`). The Deploy-target row has
//     no chevron — its in-header
//     <select> drives the reveal via deploy.js `_syncTargetPanel` (wired by
//     initDeploy), so it is intentionally NOT header-toggled here.
//   • A separate first-touch `warm` fires each bar's loader on the first
//     pointerdown/focus ANYWHERE in that bar — so it also covers always-visible
//     header controls (e.g. Repo's "Clone current repo" button) and the target
//     <select>, whose change handler must be wired by the time the admin picks.
function _wireRows(root) {
  if (_wired) return;
  _wired = true;

  // All four bars now toggle on their header (chevron-on-the-left). The Deploy-target
  // row's picker moved into its body, so clicking its "Deploy target" title opens the
  // row and reveals the selection panel (deploy.js _syncTargetPanel then swaps panels).
  ['ac-deploy-repo-row', 'ac-deploy-config-row', 'ac-deploy-target-row', 'ac-dns-main-row'].forEach((id) => {
    const row = root.querySelector('#' + id);
    if (!row) return;
    const head = row.querySelector(':scope > .ac-ability-row');
    if (!head || head.dataset.rowWired) return;
    head.dataset.rowWired = '1';
    head.addEventListener('click', (e) => {
      if (e.target.closest('select, input, button, a, label, textarea')) return;
      row.classList.toggle('expanded');
    });
  });

  // The Repo header's clone button is a single-click TOGGLE. It lives in the header
  // (so the row-toggle above ignores it, being a <button>); here we warm deploy.js on
  // demand, then call its exported toggle — so the very first click acts, with no
  // second click needed. deploy.js does NOT also wire this button, so no double-fire.
  const cloneBtn = root.querySelector('#ac-deploy-repo-clone');
  if (cloneBtn && !cloneBtn.dataset.cloneWired) {
    cloneBtn.dataset.cloneWired = '1';
    cloneBtn.addEventListener('click', async () => {
      await _ensureDeploy();
      try { window.__deployToggleClone && window.__deployToggleClone(); } catch (e) { console.error('[new-deployment] clone toggle failed', e); }
    });
  }

  const warm = (id, load) => {
    const el = root.querySelector('#' + id);
    if (!el) return;
    // load() self-guards after the first call; `once` just drops the listeners.
    el.addEventListener('pointerdown', load, { once: true });
    el.addEventListener('focusin', load, { once: true });
  };
  warm('ac-deploy-repo-row', _ensureDeploy);
  warm('ac-deploy-config-row', _ensureDeploy);
  warm('ac-deploy-target-row', _ensureDeploy);
  warm('ac-dns-main-row', _ensureDns);
}

// Re-parent bookkeeping only: refresh icons + ensure the bars are wired. No
// network work here — that's deferred to the per-bar loaders above.
function _afterAttach() {
  _refreshLucideIcons(_node);
  _wireRows(_node);
}

// Drop the instant skeleton instances.js paints into the host (see _ndSkeletonHtml)
// once the real form is attached, so the two never show at once.
function _clearSkeleton(host) {
  host.querySelectorAll('.inst-nd-skeleton').forEach((n) => n.remove());
}

// Mount (or re-parent) the New Deployment form into `host`. Called on every
// Instances render while the New instance tile is active; cheap after the first.
export async function mountNewDeployment(host) {
  if (!host) return;
  if (_node) { host.appendChild(_node); _clearSkeleton(host); _afterAttach(); return; }
  if (!_building) _building = _build();
  await _building;
  // A later render may have replaced `host`; only attach if it's still on-page.
  if (_node && host.isConnected) { host.appendChild(_node); _clearSkeleton(host); _afterAttach(); }
}

// Warm the fragment build in the background (called on idle by instances.js after
// the page's first paint) so the four bars are ready the instant the tile is
// opened — the skeleton then swaps to the real form with no perceptible wait. Only
// builds the lightweight fragment; deploy.js/dns.js still wait for a bar touch.
export function prewarmNewDeployment() {
  if (!_node && !_building) _building = _build();
}

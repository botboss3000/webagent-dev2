'use strict';

/*
 * page-access-gate.js — explains when a page requires a different access tier.
 *
 * When a visitor navigates to a main page their access level can't see (a
 * registered-only page while signed out, or an off/admins-only page), the
 * navigation layer (ui/shared/js/tabs.js) calls showPageAccessGate(pageId)
 * instead of mounting the page. The visitor signs in through the header
 * avatar button (user-panel.js); the page reload reveals the content.
 *
 * Mounts into #main-panel (index.html). Peers: tabs.js (the only caller).
 */

const GATE_ID = 'page-access-gate';

// Lazily create the overlay inside #main-panel (which is position:relative, so
// inset:0 fills exactly the page area — never the header or the chat panel).
function _ensureGate() {
  let el = document.getElementById(GATE_ID);
  if (el) return el;
  const panel = document.getElementById('main-panel');
  if (!panel) return null;
  el = document.createElement('div');
  el.id = GATE_ID;
  el.style.cssText = 'position:absolute;inset:0;z-index:40;display:none;'
    + 'align-items:center;justify-content:center;padding:24px;overflow:auto;';
  el.addEventListener('click', (event) => {
    const action = event.target.closest('[data-page-gate-action="account"]');
    if (!action) return;
    const account = document.getElementById('top-user-id');
    if (account) account.click();
  });
  panel.appendChild(el);
  return el;
}

/** Tear the gate down (no-op if it was never shown). Called on every normal
 *  navigation so leaving a gated page reveals the next page cleanly. */
export function hidePageAccessGate() {
  const el = document.getElementById(GATE_ID);
  if (el) el.style.display = 'none';
}

/**
 * Render a stable access explanation into `host`. The old indefinite spinner
 * made an intentionally gated page look like a slow or broken load.
 */
export function mountSignInForm(host, pageId = '') {
  if (!host) return;
  const isAnonymous = (() => {
    try { return String(localStorage.getItem('auth_user_id') || '').indexOf('anon_') === 0; }
    catch (_) { return false; }
  })();
  const label = pageId === 'instances' ? 'App' : 'This page';
  const title = isAnonymous
    ? `${label} is unavailable for guest chats`
    : `${label} is unavailable for this account`;
  const hint = isAnonymous
    ? 'Sign in with an account that has access to continue.'
    : 'Ask an administrator for access if you believe this is unexpected.';
  host.innerHTML = `
    <div role="status" data-page-access-state="blocked" style="max-width:460px;display:flex;flex-direction:column;align-items:center;gap:12px;padding:36px 28px;text-align:center;border:1px solid var(--border-strong);border-radius:18px;background:var(--panel-bg, var(--surface));box-shadow:0 18px 48px rgba(0,0,0,.18);">
      <i data-lucide="shield" aria-hidden="true" style="width:30px;height:30px;color:var(--accent);"></i>
      <strong style="font-size:18px;line-height:1.3;">${title}</strong>
      <span style="color:var(--text-muted);line-height:1.5;">${hint}</span>
      ${isAnonymous ? '<button type="button" data-page-gate-action="account" class="primary-btn" style="margin-top:6px;">Sign in</button>' : ''}
    </div>
  `;
  if (window.lucide && window.lucide.createIcons) {
    try { window.lucide.createIcons({ nodes: [host] }); } catch (_) {}
  }
}

/** Show an access explanation on the page area instead of mounting the page. */
export function showPageAccessGate(pageId = '') {
  const el = _ensureGate();
  if (!el) return;
  mountSignInForm(el, pageId);
  el.style.display = 'flex';
}

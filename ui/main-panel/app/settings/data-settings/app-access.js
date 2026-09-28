'use strict';

/**
 * App Access — App Configuration → Data Settings → App Access card.
 *
 * The access-MODE selection (Private / Open Registration) MOVED to the
 * Instances page → "This device" → Users tab, where it renders as an
 * "Access policy" card mirroring the Agents page Members tab (see
 * ui/main-panel/app/users/users.js — it owns the /admin/settings/app
 * access_mode load + auto-save and the /admin/users/* actions).
 *
 * What remains in this card is SOCIAL SIGN-IN: the expandable "Sign in
 * options" header, with the provider rows rendered by the sibling
 * social-auth.js (initSocialAuth). This module only wires the header's
 * expand/collapse. initAppAccess() keeps its name so the data-settings.js
 * init list is unchanged.
 */

/** "Sign in options" sub-head — expand/collapse the social-provider list below.
 *  Toggling `.expanded` on the head drives the adjacent-sibling reveal + the
 *  left-chevron rotate (app3.css). Collapsed by default. */
function _wireSocialHead() {
  const head = document.getElementById('ac-social-head');
  if (!head || head.dataset.wired) return;
  head.dataset.wired = '1';
  const toggle = () => {
    const open = head.classList.toggle('expanded');
    head.setAttribute('aria-expanded', open ? 'true' : 'false');
  };
  head.addEventListener('click', toggle);
  head.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); }
  });
}

export function initAppAccess() {
  _wireSocialHead();
}

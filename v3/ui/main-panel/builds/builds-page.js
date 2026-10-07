'use strict';
import { mountBuilds } from '../app/builds.js';

export function start(mount) {
  const headerTargets = ['tab-button-builds', 'mobile-nav-builds']
    .map(id => document.getElementById(id)).filter(Boolean);
  for (const target of headerTargets) {
    const spinner = document.createElement('span');
    spinner.className = 'build-header-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    spinner.hidden = true;
    target.append(spinner);
  }
  function setActive(busy) {
    const active = Boolean(busy);
    for (const target of headerTargets) {
      target.querySelector('.build-header-spinner').hidden = !active;
      if (active) target.setAttribute('aria-label', 'Builds — build in progress');
      else target.removeAttribute('aria-label');
    }
    document.body.classList.toggle('v3-build-active', active);
  }
  let dispose = null;
  let generation = 0;
  async function syncIdentity() {
    const current = ++generation;
    setActive(false);
    const response = await fetch('/api/v3/auth/me', {credentials: 'same-origin'});
    const me = response.ok ? await response.json() : null;
    if (current !== generation) return;
    dispose?.();
    dispose = null;
    mount.replaceChildren();
    if (me?.is_admin) dispose = mountBuilds(mount, setActive);
  }
  window.addEventListener('v3-identity-changed', () => { void syncIdentity(); });
  void syncIdentity();
}

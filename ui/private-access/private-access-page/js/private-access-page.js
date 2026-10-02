/** Private Access presentation; authentication is owned by the shared account form. */
import { mountAccountSignInForm } from '../../../shared/js/user-panel.js';

export function wirePrivateAccessPage() {
  const host = document.querySelector('[data-private-access-form]');
  const reveal = document.querySelector('[data-private-access-reveal]');
  if (!host || !reveal) return false;
  if (!host.dataset.mounted) {
    mountAccountSignInForm(host, 'private');
    const login = host.querySelector('#private-login-email');
    login.placeholder = 'Email';
    login.setAttribute('aria-label', 'Email');
    host.dataset.mounted = 'true';
    reveal.addEventListener('click', () => {
      host.hidden = false;
      reveal.hidden = true;
      reveal.setAttribute('aria-expanded', 'true');
      // Keep focus in the real click handler for browser credential suggestions.
      login.focus({ preventScroll: true });
    });
  }
  return true;
}

export function setPrivateAccessVisible(visible) {
  const restricted = Boolean(visible);
  const wasRestricted = document.body?.classList.contains('app-restricted');
  document.body?.classList.toggle('app-restricted', restricted);
  const overlay = document.getElementById('app-restricted-overlay');
  if (overlay) overlay.setAttribute('aria-hidden', restricted ? 'false' : 'true');
  if (!restricted || !wasRestricted) {
    const host = document.querySelector('[data-private-access-form]');
    const reveal = document.querySelector('[data-private-access-reveal]');
    if (host) host.hidden = true;
    if (reveal) {
      reveal.hidden = false;
      reveal.setAttribute('aria-expanded', 'false');
    }
  }
}

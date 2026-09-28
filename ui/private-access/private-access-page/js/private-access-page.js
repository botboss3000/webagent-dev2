/**
 * Private Access page presentation.
 *
 * Authentication remains authoritative in ui/shared/js/main.js. This module
 * owns only the restricted shell surface and its sign-in/register action.
 */

let _wiredButton = null;
let _signInHandler = null;

export function wirePrivateAccessPage(onSignIn) {
  const overlay = document.getElementById('app-restricted-overlay');
  const button = overlay?.querySelector('[data-private-access-sign-in]');
  if (!button) return false;

  _signInHandler = typeof onSignIn === 'function' ? onSignIn : null;
  if (_wiredButton !== button) {
    button.addEventListener('click', () => {
      if (_signInHandler) _signInHandler();
    });
    _wiredButton = button;
  }
  return true;
}

export function setPrivateAccessVisible(visible) {
  const restricted = Boolean(visible);
  document.body?.classList.toggle('app-restricted', restricted);
  const overlay = document.getElementById('app-restricted-overlay');
  if (overlay) {
    overlay.setAttribute('aria-hidden', restricted ? 'false' : 'true');
  }
}

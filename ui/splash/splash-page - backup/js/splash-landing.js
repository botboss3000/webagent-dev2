/* ============================================================================
   Welcome landing — standalone bootstrap (drop-in plugin)
   ----------------------------------------------------------------------------
   Loaded ONLY by the server-rendered landing page that app/main.py serves at the
   front door (/). The markup is already in the document (inside #splash-root,
   with `is-ready` set inline so it shows even before JS), so this module just:
     • runs the shared splash effects (splash-effects.js), and
     • wires the call-to-action: any "Enter app" button remembers the visit and
       sends them to /app — the app's stable home, which bypasses this landing.

   "Remember the visit" sets the wa_seen_splash cookie (read SERVER-SIDE by the /
   front door so returning visitors skip straight to the app). It is PERSISTENT
   (1 year) only when the "Don't show this again" box is ticked; otherwise it's a
   session cookie, so the landing returns in a fresh browser session. A localStorage
   mirror is written on the persistent path for parity with the Manage Account
   "Show welcome screen" toggle (window.WA_SPLASH). Delete ui/splash/splash-page/
   and the landing, this script, and the front-door wiring all go together.
   ========================================================================== */

import { initSplashEffects } from './splash-effects.js';

const BASE = '/ui/splash/splash-page/';
const SEEN_KEY = 'webagent.splashSeen.v1';

function _reduceMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

// Remember that this device has seen the welcome. persist=true → never show again
// on this device (1-year cookie + localStorage mirror); persist=false → skip for
// this browser session only.
function _setSeen(persist) {
  try {
    const maxAge = persist ? '; max-age=31536000' : '';   // 1 year, else session
    document.cookie = 'wa_seen_splash=1; path=/; samesite=Lax' + maxAge;
  } catch (_) {}
  if (persist) { try { localStorage.setItem(SEEN_KEY, '1'); } catch (_) {} }
}

function _enterApp(dontShowAgain) {
  _setSeen(!!dontShowAgain);
  window.location.assign('/app');
}

function _boot() {
  const root = document.getElementById('splash-root');
  if (!root) return;
  const scrollEl = root.querySelector('[data-splash-scroll]');

  // CTA: every "Enter app" / "Get started" / "Proceed to app" button.
  root.querySelectorAll('[data-splash-enter]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const cb = root.querySelector('[data-splash-dontshow]');
      _enterApp(cb && cb.checked);
    });
  });

  try {
    initSplashEffects(root, scrollEl, BASE, { reduceMotion: _reduceMotion() });
  } catch (e) {
    console.warn('[landing] effects failed', e);
  }
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _boot);
} else {
  _boot();
}

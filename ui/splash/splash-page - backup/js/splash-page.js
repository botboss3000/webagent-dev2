/* ============================================================================
   Splash / Welcome — app-shell hook (drop-in plugin)
   ----------------------------------------------------------------------------
   The welcome screen is now a real, SERVER-RENDERED LANDING PAGE at the front
   door (/), built from THIS folder's markup + CSS by app/main.py
   (_render_landing_page) — not a client-side overlay. That makes it crawlable
   (its copy is in the initial HTML) and gives it a proper link-preview card.

   This module stays loaded by the shell's boot hook (ui/shared/js/partial-
   loader.js → bootSplashPlugins) for ONE job: exposing window.WA_SPLASH so the
   Manage Account → Preferences → "Show welcome screen" toggle can turn the
   landing front door on/off FOR THIS DEVICE. `startSplash` is now a no-op (kept
   only for the boot-hook contract); there is no overlay to mount.

   "Seen" lives in BOTH places, on purpose:
     • a cookie (wa_seen_splash) — read SERVER-SIDE by the / front door so a
       returning visitor skips the landing and lands straight in the app;
     • a localStorage mirror (SEEN_KEY) — kept for parity with the account toggle.
   The landing's own "Enter app" button (js/splash-landing.js) writes the same
   cookie. Delete ui/splash/splash-page/ and the landing, this hook, and
   WA_SPLASH all disappear together (drop-in clean).
   ========================================================================== */

const SEEN_KEY = 'webagent.splashSeen.v1';
// Cache of the app-wide master switch (admin App Settings → Startup & Boot →
// Welcome screen, persisted server-side as splash_enabled). Reconciled from
// /api/v1/auth/ui-config so an admin's change lands on the next load — mirrors
// the cache-first/reconcile pattern in ui/shared/js/appearance.js.
const ENABLED_KEY = 'webagent.splashEnabled.v1';
// Fired on window whenever the per-device preference flips, so the linked Manage
// Account checkbox can stay in sync if it is live.
const PREF_EVENT = 'wa-splash-pref-changed';

// ── Per-device "seen" flag — cookie (server-readable) + localStorage mirror ──
function _cookieSeen() {
  try { return /(?:^|;\s*)wa_seen_splash=1(?:;|$)/.test(document.cookie); } catch (_) { return false; }
}
function _setCookieSeen(on) {
  try {
    if (on) document.cookie = 'wa_seen_splash=1; path=/; samesite=Lax; max-age=31536000';
    else    document.cookie = 'wa_seen_splash=; path=/; samesite=Lax; max-age=0';
  } catch (_) {}
}
function _hasSeen() {
  if (_cookieSeen()) return true;
  try { return localStorage.getItem(SEEN_KEY) === '1'; } catch (_) { return false; }
}
function _markSeen() {
  try { localStorage.setItem(SEEN_KEY, '1'); } catch (_) {}
  _setCookieSeen(true);
}
function _clearSeen() {
  try { localStorage.removeItem(SEEN_KEY); } catch (_) {}
  _setCookieSeen(false);
}

// App-wide master switch (cached). Defaults to enabled when unknown.
function _globallyEnabled() {
  try { return localStorage.getItem(ENABLED_KEY) !== '0'; } catch (_) { return true; }
}

// Fire-and-forget: refresh the cached master switch from the public ui-config so
// an admin's enable/disable lands on the next visit.
function _refreshEnabled() {
  let headers = {};
  try {
    const t = localStorage.getItem('auth_token');
    if (t) headers = { Authorization: 'Bearer ' + t };
  } catch (_) {}
  fetch('/api/v1/auth/ui-config', { headers })
    .then((r) => (r.ok ? r.json() : null))
    .then((cfg) => {
      if (!cfg || typeof cfg.splash_enabled !== 'boolean') return;
      try { localStorage.setItem(ENABLED_KEY, cfg.splash_enabled ? '1' : '0'); } catch (_) {}
    })
    .catch(() => {});
}

// ── Per-device "show the welcome screen" preference (window.WA_SPLASH) ───────
// The single source of truth the Manage Account "Show welcome screen" toggle
// drives. Turning it off marks the device "seen" (sets the skip cookie), so the
// / front door serves the app directly; turning it on clears that, so the landing
// shows again. Exposed at module load; absent when the splash folder is deleted,
// so the account row hides itself. Drop-in safe.
window.WA_SPLASH = {
  // Will the welcome landing show on this device? (false once "seen"/skipped.)
  isShown() { return !_hasSeen(); },
  // Flip the per-device preference; notifies any live listener.
  setShown(show) {
    if (show) _clearSeen(); else _markSeen();
    try {
      window.dispatchEvent(new CustomEvent(PREF_EVENT, { detail: { shown: !!show } }));
    } catch (_) {}
  },
  // App-wide master switch (admin). When false the landing never shows for anyone.
  enabled() { return _globallyEnabled(); },
};

// Kept for the boot-hook contract (partial-loader calls the descriptor's `start`
// export). The welcome screen is now the server-rendered landing page at /, so
// there is no overlay to mount — we only refresh the cached master switch.
export function startSplash() {
  _refreshEnabled();
}
// Also expose as a window global, for parity with the other page modules.
window.startSplash = startSplash;

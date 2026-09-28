'use strict';

// App boot orchestrator — imports every top-level module and runs the boot
// sequence (bindDom → chat → sessions → tabs → loop, billing, account, …).
// Admin Tools and the Agents page init lazily on first open (see note below),
// not at boot.

import { app, bindDom } from './state.js';
import { initStorageUi } from './storage.js';
import { initChat } from '../../chat/js/chat-ui.js?v=257';
import { initSuggestions } from '../../chat/js/suggestions.js';
import { initChatChanges } from '../../chat/js/chat-changes.js';
import { initAbilities } from '../../chat/js/abilities.js';
import { initChatActivity } from './chat-activity.js';
import { initReconnect } from './reconnect.js';
import { ensureAttachmentsInit } from './attachments.js';
import { connectAgent } from './agentWs.js';
import { initTabs } from './tabs.js?v=276';
import { registerSessionApi, initSessions } from '../../chat/js/session-init.js?v=515';
import { initBilling } from './billing.js';
import { initAccount } from './account.js';
import { relocateAdminToolsContainers } from './files.js';
// The whole Admin Tools panel — the file manager (initFiles) and the sub-panels
// (initDbViewer / initDataManagement / initRemoteAccess / initAppConfig) — is
// initialised lazily on first Admin Tools open: initFiles via startAdminTools(),
// the sub-panels via _ensureAdminInit() in tabs.js. None run at boot. initAgents
// runs via startAgents() on Agents-tab activation.
import { initChatResize } from './chatResize.js';
import { initChatZoom } from '../../chat/js/chat-zoom.js';
// RECORDER-HOOK: recorder boot moved to application-plugins.js; no hard plugin import.
import { applyChatControlsConfig } from '../../chat-controls/chat-controls-config.js';
import { applyChatFooterConfig } from '../../chat/js/chat-footer-config.js';
import { applyChatHeaderConfig } from '../../chat/js/chat-header-config.js';
import { fetchAdminStatus, isAdmin, fetchAccessMode, getAccessMode, isAuthenticated, isAnonGuest, showLeftOverlay, authHeaders } from './left-login.js?v=253';
import {
  setPrivateAccessVisible, wirePrivateAccessPage,
} from '../../private-access/private-access-page/js/private-access-page.js';
import { installAuthRefresh, ensureFreshToken, startTokenAutoRefresh } from './auth-refresh.js';
import { getActive, removeAccount } from './accounts.js';
import './icons.js'; // auto-renders Lucide icons on DOM mutations
import { randomUUID } from './uuid.js';
import { initClipboard } from './clipboard.js';
import { initWakeLock, applyAlwaysOnDisplay } from './screen-wake.js';
import { apiPath } from './config.js';
import { loadUiMessages } from './app-prompts.js';
import { initBrowserPopup } from '../../browser-popup/js/browser-popup.js';
import { initWebagentLauncher } from '../../chat-widget/js/webagent-launcher.js';
import { initMobileNavigation } from './mobile-navigation.js';
import { initOfflineReader, isOfflineReadOnly } from './offline-reader.js';
import { initRouteError } from './route-error.js';

bindDom();
initOfflineReader();

let _anonIdentityLimitReached = false;

async function _captureAnonIdentityLimit(response) {
  if (!response || response.status !== 429) return;
  try {
    const payload = await response.clone().json();
    if (['anonymous_identity_source', 'anonymous_global_session'].includes(payload?.detail?.scope)) {
      _anonIdentityLimitReached = true;
    }
  } catch (_) { /* a non-JSON edge response still follows normal signed-out UI */ }
}

// Mount after authenticated boot has revealed the shell; the initializer is a
// no-op for signed-out/public visitors.
window.addEventListener('webagent-app-ready', initWebagentLauncher, { once: true });

// Pull the editable chat UI strings (welcome bubble, system bubbles, composer
// placeholders) from data/config/chat_ui.json as early as possible so they
// drive the first chat render. Fire now; the boot path awaits it below. Has its
// own fallbacks, so this can never blank a string or stall boot. See app-prompts.js.
const _uiMessagesReady = loadUiMessages();

// Guarantee Ctrl/Cmd + C / X / V / A work in every input, textarea,
// contenteditable and text selection across the whole app (terminals keep
// their own handling). Installed first so it sits ahead of every panel's key
// handlers. See clipboard.js for the why.
initClipboard();

// Keep the 24h access token fresh. When it expires while a tab is open (or
// between visits), authenticated features break two ways: the session-messages
// participant check sees no identity and returns `{restricted}`, so switching
// sessions silently bounces you into a new empty one ("I can see the chat but
// can't choose a different session"); and require_db_auth / _require_auth
// routes 401. The already-open agent WebSocket keeps live chat working, which
// is why only a re-login (a fresh token) used to fix it. installAuthRefresh()
// adds a 401 → recall → retry backstop on window.fetch; startTokenAutoRefresh()
// keeps the token valid proactively (timer + on focus). See auth-refresh.js.
installAuthRefresh();
startTokenAutoRefresh();

// Screen wake lock — wires the visibilitychange re-acquire listener so a
// later enableWakeLock() (genui pages, Always-on display setting) survives
// tab hide/show. See screen-wake.js.
initWakeLock();

// App-wide "Always-on display" (App Settings → App Functions) — when the
// admin has switched it on, keep the device screen awake while the app tab
// is visible. Best-effort read of the public ui-config at boot; any
// failure just leaves the screen sleeping (fail-off — the default).
// A per-device override set from the chat session-dropdown ⋯ switch wins over
// the server default (see screen-wake.js applyAlwaysOnDisplay).
_uiMessagesReady.then((cfg) => {
  applyAlwaysOnDisplay(cfg && cfg.always_on_display === true);
}).catch(() => { /* keep the screen sleeping */ });

// ── Anonymous session ─────────────────────────────────────────────────────
// Resolve a boot-time identity when no auth token exists yet. The behaviour
// depends on the app's access mode:
//   • open              → auto-login as the default admin (localhost-only,
//                         enforced server-side). Local-use convenience.
//   • any mode, but on a public agent URL (/{agentId}) → try a per-agent
//                         anonymous session. The agent's OWN user_mode decides
//                         whether anonymous is allowed (anonymous permissions
//                         are controlled per-agent, not at the app level), so
//                         this is attempted regardless of the app access mode.
//   • otherwise         → leave unauthenticated; the visitor signs in / registers.
// Redirects to the first-run setup wizard if the app isn't set up yet (no admin).
async function _initBootAuth() {
  const _store = (token, userId) => {
    if (!token) return;
    localStorage.setItem('auth_token', token);
    localStorage.setItem('auth_user_id', userId);
    app.currentUserId = userId;
    app.localUserId = userId;
  };

  const _requestGuestAdmission = () => {
    let browserId = localStorage.getItem('anon_browser_id');
    if (!browserId) {
      browserId = randomUUID();
      localStorage.setItem('anon_browser_id', browserId);
    }
    const guestCredential = localStorage.getItem('anon_guest_credential') || '';
    return fetch('/api/v1/auth/guest-login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ browser_id: browserId, guest_credential: guestCredential || null }),
    }).catch(() => null);
  };

  const storedUid = localStorage.getItem('auth_user_id') || '';
  const haveToken = !!localStorage.getItem('auth_token');
  // Guest admission is independently server-gated by the current access mode,
  // so it is safe to overlap with the public mode read. In Private mode the
  // server rejects it and this result is ignored; in Open Registration it
  // removes a full serialized round trip from the critical path.
  const guestAdmission = (!window.__agentId && (!haveToken || storedUid.startsWith('anon_')))
    ? _requestGuestAdmission()
    : null;

  // Resolve the app's access mode before committing an identity decision. The
  // independently server-gated guest request above may run in parallel, but its
  // result is consumed only when this authoritative mode permits it.
  await fetchAccessMode();
  const mode = getAccessMode();

  // An existing token means we're already signed in and the keep-fresh timer +
  // 401 backstop maintain it from here. One case re-establishes identity instead
  // of trusting a stored token blindly: Open Registration guests (anon_* token)
  // have no remember-token to drive the 401 refresh, so a stale guest token would
  // silently break — fall through to re-mint one below against the SAME
  // browser-keyed identity (their sessions survive). A real signed-in member
  // token is trusted as-is.
  if (haveToken) {
    const _uid = localStorage.getItem('auth_user_id') || '';
    const isAnon = _uid.indexOf('anon_') === 0;
    if (isAnon && mode !== 'public_registered') {
      // A LEFTOVER anonymous guest token from a prior Open-Registration session
      // that the admin has since switched to a stricter mode. An anon guest is
      // NOT an approved member, so it must not be trusted here — drop the stale
      // identity entirely and fall through to the "require sign-in" path so the
      // app-wide Private lock fires (otherwise the guest would silently keep
      // access: the profile page, chat, and WebSocket would all treat the
      // lingering token as a signed-in user). removeAccount() clears the legacy
      // auth_token/auth_user_id keys when no account remains, or switches to a
      // real signed-in member if one is also present — so we never wipe a
      // genuine member's token here.
      try { removeAccount(_uid); } catch (_) {}
      // fall through (no return): no token is minted in admin_approval mode.
    } else if (!(mode === 'public_registered' && isAnon)) {
      // A signed-in member. Don't trust the stored token blindly — if it's
      // already expired, try one refresh now; ensureFreshToken() signs the user
      // out cleanly (fires webagent-auth-expired) when it can't be recovered,
      // instead of letting the app boot into the powerless-zombie state. A still
      // valid token makes this a no-op.
      await ensureFreshToken();
      return;
    }
    // (public_registered + anon falls through below to re-mint a fresh guest.)
  }

  // Only the private root needs the first-run admin redirect. Open Registration
  // and public-agent URLs have authoritative server-side session gates of their
  // own, so an extra setup-status DB read just serializes cold guest login behind
  // unrelated work and can leave the composer looking closed during the wait.
  if (!window.__agentId && mode !== 'public_registered') {
    try {
      const statusRes = await fetch('/api/v1/auth/setup-status');
      if (statusRes.ok) {
        const statusData = await statusRes.json();
        if (!statusData.initialized) {
          if (!location.pathname.endsWith('/setup.html')) {
            window.location.href = '/setup.html';
          }
          return;  // app not set up yet
        }
      }
    } catch (e) {
      // A transient setup probe failure must not manufacture an authenticated
      // identity. Leave private-mode visitors on the signed-out shell.
      return;
    }
  }

  // ── Public agent URL: per-agent anonymous, gated by the agent itself. ──
  if (window.__agentId) {
    let browserId = localStorage.getItem('anon_browser_id');
    if (!browserId) {
      browserId = randomUUID();
      localStorage.setItem('anon_browser_id', browserId);
    }
    let publicAdmissionTimer = null;
    try {
      const credentialKey = `anon_guest_credential:${window.__agentId}`;
      const guestCredential = localStorage.getItem(credentialKey) || '';
      const controller = typeof AbortController === 'function' ? new AbortController() : null;
      // A public shell must not remain permanently readonly if its first
      // admission request is swallowed during service-worker startup. Release
      // this attempt so the bounded boot recovery below can make one retry.
      if (controller) publicAdmissionTimer = setTimeout(() => controller.abort(), 1500);
      const res = await fetch(`/api/v1/agents/${window.__agentId}/anon-session`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ browser_id: browserId, guest_credential: guestCredential || null }),
        ...(controller ? { signal: controller.signal } : {}),
      });
      if (res.ok) {
        const data = await res.json();
        if (data.guest_credential) localStorage.setItem(credentialKey, data.guest_credential);
        _store(data.access_token || data.token, data.user_id);
        // The header may have started its tenant-scoped catalog request before
        // this public-agent guest token existed, which correctly failed closed
        // as anonymous. Reload now that this server-authorized per-agent guest
        // identity is stored so the public chat shell can mount its authorized
        // views. Private unauthenticated pages never take this route.
        if (typeof window.__reloadPagesCatalog === 'function') {
          await window.__reloadPagesCatalog();
        }
      } else {
        await _captureAnonIdentityLimit(res);
      }
      // A 403 here means the agent requires a registered/authorized account —
      // leave unauthenticated so the sign-in UI takes over.
    } catch (e) {
      console.warn('anon session init failed:', e);
    } finally {
      if (publicAdmissionTimer) clearTimeout(publicAdmissionTimer);
    }
    return;
  }

  // ── Open Registration: anonymous visitors are first-class guests. ──
  // The app is open to anonymous use, with what each anon may do decided
  // per-agent (the agent's user_mode), not at the app level. Hand the visitor a
  // persistent anonymous identity (a web_public guest keyed to this browser) so
  // the WebSocket handshake + chat gate accept them and their sessions survive a
  // reload. A real signed-in member returned early above; this path is only for
  // anonymous visitors and stale-guest re-mints.
  if (mode === 'public_registered') {
    try {
      const res = await (guestAdmission || _requestGuestAdmission());
      if (!res) throw new Error('guest admission unavailable');
      if (res.ok) {
      const data = await res.json();
      if (data.guest_credential) localStorage.setItem('anon_guest_credential', data.guest_credential);
        _store(data.access_token || data.token, data.user_id);
      } else {
        await _captureAnonIdentityLimit(res);
      }
      // A non-OK response leaves the visitor unauthenticated; the app-access
      // gate stays open in this mode (browsing works) and the header sign-in
      // form is available. The fallback below keeps any prior guest id usable.
    } catch (e) {
      console.warn('guest session init failed:', e);
    }
    // If guest-login was unreachable but we still hold a prior guest id, keep
    // using it so an offline blip doesn't drop the WebSocket identity.
    if (!app.currentUserId) {
      const priorUid = localStorage.getItem('auth_user_id');
      if (priorUid) { app.currentUserId = priorUid; app.localUserId = priorUid; }
    }
    return;
  }

  // Otherwise (main page, admin_approval): require sign-in.
}

// ── App-wide access gate ───────────────────────────────────────────────────
// Show the full-screen Restricted Access lock (hiding every page AND the chat
// panel — body.app-restricted, styled in ui/shared/css/index.css) whenever the
// visitor isn't signed in / authorized to use this instance. A valid auth token
// is the authorization signal: in "private" (admin-approval) mode a guest has no
// token and an unapproved account can't get one, so both are locked out of every
// page; after sign-in user-panel.js reloads the page, which re-runs this gate.
//
// Public agent URLs (/{agentId}) run their OWN per-agent anonymous access flow,
// so they are intentionally never gated here.
function _applyAppRestricted(restricted) {
  setPrivateAccessVisible(restricted);
  if (restricted) wirePrivateAccessPage(showLeftOverlay);
}
function _evaluateAppAccess() {
  if (window.__agentId) { _applyAppRestricted(false); return; }
  // 'public_registered' (Open Registration) is open to anonymous visitors — the
  // app is usable without a member sign-in (Open Registration hands anons a guest
  // token). Per-page visibility (App Settings) and the Admin Tools gate decide
  // what an anon can actually see, so the whole-app lock must NOT fire here. Only
  // 'admin_approval' (Private) walls the entire app behind sign-in.
  const mode = getAccessMode();
  if (mode === 'public_registered') { _applyAppRestricted(false); return; }
  // Private (admin_approval): only an approved MEMBER passes. A token alone isn't
  // enough — an anonymous guest token (a leftover from a prior Open-Registration
  // session) belongs to a non-approved identity, so it must NOT unlock the app.
  _applyAppRestricted(!(isAuthenticated() && !isAnonGuest()));
}


// Run boot auth, then continue init. For visitors who must sign in this resolves immediately.
const _anonReady = _initBootAuth();
// A public page's first per-agent guest request can race the initial shell and
// service-worker startup. Retry exactly once only when no token was stored; it
// is intentionally scoped to an already server-authorized public-agent route.
// Private pages and successful guest boots never take this recovery path.
const _publicGuestReady = _anonReady.then(async () => {
  if (!window.__agentId || localStorage.getItem('auth_token')) return;
  await new Promise((resolve) => setTimeout(resolve, 300));
  await _initBootAuth();
});
// Evaluate access once boot auth settles (so any minted guest token is already
// in place and the visitor is never momentarily locked).
_publicGuestReady.then(_evaluateAppAccess).catch(() => _evaluateAppAccess());

// _initBootAuth is the sole identity barrier for the rest of initialization.
// _initBootAuth already refreshes signed-in member credentials before it
// resolves. Guests have no recall credential and are freshly minted above, so
// a second freshness barrier adds no protection to the first render.
const _bootReady = _publicGuestReady;

// ── JS error debugging ────────────────────────────────────────────────────
// Catches unhandled JS errors and module load failures. Each error is emitted
// via console.error, which the in-app debug console panel captures and shows;
// the header bug-toggle turns red while the panel holds an error entry (see
// debugConsole.js), so a problem is visible without opening the panel or
// DevTools. The page title is flagged too, so it shows in the browser tab.
//
// Yellow status dots in the header mean the WebSocket is trying to connect
// (state: "Connecting…"). They turn green once connected, red if the server
// is unreachable. Persistent yellow flashing = the connection keeps dropping
// and retrying — usually caused by a JS module error that prevents the page
// from initialising fully (blocking connectAgent() from completing), or a
// server that is down/unreachable.
const _JS_ERRORS = [];

// Benign browser notifications that fire as "error" events but are NOT real
// errors — they have no functional impact and are not actionable. The
// "ResizeObserver loop…" pair is the canonical one: the browser raises it
// whenever a ResizeObserver callback itself changes layout in the same frame
// (e.g. the file-editor's resizable panes settling when the File Explorer
// opens). Suppress them so they don't trip the red banner or pollute logs.db.
const _BENIGN_ERROR_PATTERNS = [
  /ResizeObserver loop completed with undelivered notifications/i,
  /ResizeObserver loop limit exceeded/i,
];
function _isBenignError(msg) {
  const s = String(msg || '');
  return _BENIGN_ERROR_PATTERNS.some((re) => re.test(s));
}

function _showJsErrorBanner(msg, source, errorEvent) {
  if (_isBenignError(msg)) return;  // non-actionable browser noise — drop it
  _JS_ERRORS.push({ msg, source, ts: new Date().toISOString() });

  // Push the error to the server's diagnostic log (fire-and-forget). Route
  // through the shared pre-module funnel in index.html (window.__reportJsError)
  // so this richer handler and the early catcher collapse to a single logs.db
  // row (deduped by message). Fall back to a direct POST only if the funnel was
  // stripped out of the page shell.
  try {
    var _payload = { message: String(msg).slice(0, 4000), source: source || '' };
    if (errorEvent) {
      var _stackSrc = errorEvent.error || errorEvent.reason;
      if (_stackSrc && _stackSrc.stack) _payload.stack = String(_stackSrc.stack).slice(0, 8000);
      if (typeof errorEvent.lineno === 'number') _payload.source = (source || '') + ':' + errorEvent.lineno + ':' + (errorEvent.colno || 0);
    }
    if (typeof window.__reportJsError === 'function') {
      window.__reportJsError(_payload);
    } else {
      if (typeof app !== 'undefined' && app) {
        if (app.currentUserId) _payload.user_id = app.currentUserId;
        if (app.currentSessionId) _payload.session_id = app.currentSessionId;
        if (app.currentAgentId) _payload.agent_id = app.currentAgentId;
      }
      _payload.url = location.pathname + location.search;
      fetch('/api/v1/log/browser-error', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(_payload),
      }).catch(function(){});  // best-effort
    }
  } catch (_) {}

  // Title prefix — immediately visible in the browser tab
  document.title = '[JS ERR] ' + document.title.replace(/^\[JS ERR\] /, '');

  // The old fixed top-of-page red banner is gone. Every error routed through
  // here also emits console.error(...) below, which the debug console panel
  // captures (debugConsole.js); its header bug-toggle turns red (`.has-errors`)
  // while the log holds an error entry, so problems stay visible without
  // opening the panel — and the panel is a strict superset of the banner
  // (it also shows app-level console.error/warn calls), so the banner was
  // redundant.

  // Structured console output for DevTools inspection
  console.error('[WebAgent JS error]', { msg, source, allErrors: _JS_ERRORS });
}

// Catches synchronous errors thrown by scripts (including missing variables,
// type errors, etc.). Also catches resource-load errors (404 scripts,
// stylesheets, images, etc.) which have no .message — extract the failed URL
// from e.target so the debug console panel shows something useful.
window.addEventListener('error', (e) => {
  const msg = e.message || (e.error && e.error.message);
  if (!msg) {
    // Resource-load error — build a description from the failed element
    const target = e.target;
    if (target) {
      const failedUrl = target.src || target.href || target.data || '';
      const tagName = (target.tagName || 'RESOURCE').toLowerCase();
      _showJsErrorBanner('Failed to load ' + tagName + ': ' + (failedUrl || '(unknown)'), 'resource', e);
    }
    return;
  }
  const src = e.filename ? e.filename.split('/').pop() + ':' + e.lineno : 'unknown';
  _showJsErrorBanner(msg, src, e);
});

// Catches async failures — broken dynamic imports, unhandled promise rejections,
// fetch errors that aren't caught, etc.
window.addEventListener('unhandledrejection', (e) => {
  const msg = (e.reason && e.reason.message) ? e.reason.message : String(e.reason);
  _showJsErrorBanner(msg, 'unhandledrejection', e);
});

// Wrap each init call so one broken module can't silently prevent the rest
// from loading. Any error is surfaced in the banner above.
function _safeInit(name, fn) {
  try {
    fn();
  } catch (e) {
    _showJsErrorBanner(e.message, name);
    console.error('[WebAgent] ' + name + ' threw during init:', e);
  }
}

// Admin Tools and static diagnostic header controls are admin-only. Visibility
// is fail-closed: all controls ship hidden and are revealed only after
// fetchAdminStatus() confirms the server-side app-admin profile.
function _applyAdminToolsVisibility() {
  const admin = isAdmin();
  document.body.classList.toggle('is-admin', admin);
  const opt = document.querySelector('#main-tab-select option[value="admin-tools"]');
  if (opt) opt.disabled = !admin;
  ['debug-console-toggle', 'header-restart-server', 'header-refresh-sw'].forEach((id) => {
    const control = document.getElementById(id);
    if (control) control.hidden = !admin;
  });
}

window.addEventListener('admin-status-loaded', _applyAdminToolsVisibility);

// Wait for anon session (if needed) AND a valid access token before running
// the rest of init, so auth_token + user_id are available — and current — for
// all downstream modules.
_bootReady.then(async () => {
  // Make sure the editable chat strings are in before the first session render
  // (the welcome bubble reads them). Capped so a hung config fetch can't stall
  // boot; chatMsg() falls back to its built-in defaults either way.
  await Promise.race([
    _uiMessagesReady,
    new Promise((res) => setTimeout(res, 250)),
  ]);

  // Hide Admin Tools immediately (synchronous check covers admin
  // bootstrap); fetchAdminStatus() will re-check against the server profile
  // and dispatch 'admin-status-loaded' to refine the visibility.
  _applyAdminToolsVisibility();
  fetchAdminStatus();

  // If we just auto-signed-out an expired session (see webagent-auth-expired
  // handler), open the sign-in form so the user can immediately sign back in
  // rather than landing on a silent signed-out shell.
  try {
    if (sessionStorage.getItem('webagent_signed_out_expired')) {
      sessionStorage.removeItem('webagent_signed_out_expired');
      setTimeout(() => { try { showLeftOverlay(); } catch (_) {} }, 400);
    }
  } catch (_) { /* private mode — non-fatal */ }

  // Fire profile + app settings early so initAgents() can use cached results
  // instead of fetching them sequentially later. These are fire-and-forget;
  // they store results on window.__agentsProfileData / __agentsAppSettingsData.
  if (app.currentUserId) {
    fetch(`/api/v1/user/profile?user_id=${encodeURIComponent(app.currentUserId)}`, { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) window.__agentsProfileData = d; })
      .catch(() => {});
    fetch('/admin/settings/app', { headers: authHeaders() })
      .then(r => r.ok ? r.json() : null)
      .then(d => {
        if (d) {
          window.__agentsAppSettingsData = d;
          // Apply hide_header_on_keyboard at boot so the CSS rule
          // (body.hide-header-kb.chat-pill-focused) can gate header hiding
          // on mobile. Default OFF — opt-in via App Settings → App Functions.
          if (d.hide_header_on_keyboard === true && document.body) {
            document.body.classList.add('hide-header-kb');
          }
          // Store the threshold as a data attribute so the viewport
          // tracker in index.html can read it at resize time.
          if (document.body) {
            var t = parseInt(d.hide_header_kb_threshold, 10);
            document.body.dataset.hhkThreshold = (!isNaN(t) && t >= 0) ? t : 200;
          }
        }
      })
      .catch(() => {});
  }

  _safeInit('initStorageUi',        initStorageUi);
  _safeInit('initChat',             initChat);
  _safeInit('initSuggestions',      initSuggestions);
  _safeInit('initAbilities',        initAbilities);
  _safeInit('initChatActivity',     initChatActivity);
  _safeInit('ensureAttachmentsInit', ensureAttachmentsInit);
  _safeInit('initReconnect',        initReconnect);
  _safeInit('registerSessionApi',   registerSessionApi);

  try {
    connectAgent();
  } catch (e) {
    _showJsErrorBanner(e.message, 'connectAgent');
  }

  _safeInit('initTabs',        initTabs);
  _safeInit('initRouteError',  initRouteError);
  _safeInit('initMobileNavigation', initMobileNavigation);
  // Move parked Admin Tools markup (App Config + Database viewer) into the
  // admin-tools layout. Stays eager (cheap DOM reparent) so the markup is in
  // its final position before the now-lazy db viewer / app config init bind
  // to it on first Admin Tools open.
  _safeInit('relocateAdminToolsContainers', relocateAdminToolsContainers);
  // ── Lazy main-panel init ──────────────────────────────────────────────
  // The entire Admin Tools panel — file manager (initFiles, via
  // startAdminTools) and the sub-panels (Database viewer, Data Management,
  // Remote Access, App Config, via _ensureAdminInit() in tabs.js) — is now
  // built only on first Admin Tools open, not at boot. This avoids building
  // DOM/handlers for a panel that's rarely opened, especially on mobile where
  // only one panel is ever visible. initAgents is covered by startAgents()
  // when the Agents tab activates. Kept eager below:
  //  • initBilling / initAccount — cheap, no tab-gated heavy work.
  _safeInit('initAccount',     initAccount);
  _safeInit('initSessions',    initSessions);
  _safeInit('initChatChanges',    initChatChanges);
  _safeInit('initChatResize',  initChatResize);
  // Pinch / Ctrl+wheel zoom for the chat transcript only (ui/chat/js/chat-zoom.js).
  _safeInit('initChatZoom',    initChatZoom);
  // Floating agent-openable browser window (agent's browser_popup tool). Just
  // registers window.__browserPopup for the shared WS handler; no UI until used.
  _safeInit('initBrowserPopup', initBrowserPopup);

  // Apply chat_ui.json config (textarea rows, header, footer) — reads from the
  // already-fetched ui-config payload. Best-effort; HTML fallback is in place.
  // NEW: unified chat-controls plugin system (replaces header/footer config over time)
  // The boot curtain stays up until the async control builder has put every
  // header/footer control in its final flex row. Without awaiting it, the
  // browser can paint the flat fallback controls as a vertical stack for a
  // frame before the builder reparents them.
  const controlsReady = Promise.resolve(applyChatControlsConfig()).catch(() => {});
  // Static header/footer/composer markup is already complete and interactive.
  // Do not keep the whole app behind the curtain when an optional control
  // module or stylesheet is slow; finish reconciliation when it arrives.
  await Promise.race([
    controlsReady,
    new Promise((resolve) => setTimeout(resolve, 750)),
  ]);
  // The chat panel partial is injected asynchronously (partialsReady), so the
  // boot-time initChatActivity() (above) may have run before #chat-above-pill
  // existed — leaving the activity bar's element refs null and progress notes
  // silently dropped until a refresh re-rolls the race. Re-init now that the
  // controls builder has settled the DOM; the re-init is idempotent (refs
  // refreshed, one-shot wiring kept). See chat-activity.js _acquireElements.
  _safeInit('initChatActivity', initChatActivity);
  controlsReady.then(() => _safeInit('initChatActivity', initChatActivity));
  // LEGACY: keep the old header/footer config running during migration
  applyChatFooterConfig();
  applyChatHeaderConfig();

  // ── Deferred, non-critical boot work ────────────────────────────────────
  // These touch the network but aren't needed for first paint or first
  // interaction, so they're pushed PAST the boot burst to free the browser's
  // ~6 HTTP/1.1 sockets for the calls that DO gate the first render (session
  // roster, message history, agent roster). requestIdleCallback runs them in
  // the first idle gap after paint; setTimeout is the fallback for engines
  // without it. Each still self-registers its own timers/handlers when it runs.
  //   • initBilling   — installs the HTTP 402 payment interceptor.
  // Optional recorder initializes independently through application-plugins.js.
  const _deferNonCriticalBoot = () => {
    _safeInit('initBilling',  initBilling);
  };
  // Run it in the first idle gap AFTER the boot curtain lifts (webagent-app-ready,
  // fired from reveal() in index.html) — not on the first idle gap outright, which
  // lands mid-burst. If the curtain already lifted before this listener attaches,
  // run immediately. requestIdleCallback yields to any still-in-flight critical
  // work; setTimeout is the fallback for engines without it.
  const _runDeferred = () => {
    if (window.requestIdleCallback) requestIdleCallback(_deferNonCriticalBoot, { timeout: 2000 });
    else setTimeout(_deferNonCriticalBoot, 200);
  };
  if (window.__boot && window.__boot.revealed) {
    _runDeferred();
  } else {
    window.addEventListener('webagent-app-ready', _runDeferred, { once: true });
  }

  // Boot complete: the app shell + active tab + chat panel are mounted, so the
  // page is now in its intended view. Lift the black boot curtain — unless a
  // splash has claimed it (then the splash hands off when it's faded in). Two
  // rAFs so the freshly mounted DOM paints before we start fading.
  try {
    if (window.__boot && window.__boot.appReady) {
      requestAnimationFrame(() => requestAnimationFrame(() => window.__boot.appReady()));
    }
  } catch (_) {}
});

// ── Visibility change: reconnect when user returns to this tab ──
// Terminal tabs manage their own per-instance WebSocket reconnect inside
// createTerminalInstance — only the agent WS is global.
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible') {
    const agentOk = app.agentWs && app.agentWs.readyState === WebSocket.OPEN;
    if (!agentOk) {
      connectAgent();
      // Dead-WS fallback: pull the current session straight from the DB so an
      // in-progress (or just-finished) answer shows even before the socket is
      // back. The DB is the source of truth; live events are an accelerant.
      if (typeof app.reloadCurrentSession === 'function') {
        try { app.reloadCurrentSession(); } catch (_) {}
      }
    }
  }
});

// ── Fallback poll every 10s in case visibility change misses something ──
setInterval(() => {
  if (!app.agentWs || app.agentWs.readyState > 1) connectAgent();
}, 10000);

// ── Token was silently refreshed (expired JWT recalled via remember_token) ──
// The agent WebSocket authenticates with the token only at connect time, so a
// socket that dropped/failed while the token was stale must reconnect to pick
// up the fresh one. An already-open socket is left alone (don't disturb a
// working live-chat stream). See auth-refresh.js.
window.addEventListener('webagent-token-refreshed', () => {
  const open = app.agentWs && app.agentWs.readyState === WebSocket.OPEN;
  if (!open) {
    try { connectAgent(); } catch (_) {}
  }
});

// ── Session is dead and cannot be recovered → sign out cleanly ──────────────
// Fired by auth-refresh.js when a signed-in member's access token has expired
// and there's no usable remember-token to recall a fresh one (or it was
// revoked). Without this the admin drifts into a "signed-in but powerless"
// state: the token is still in storage so the UI looks logged-in, but the
// server rejects it — Admin Tools disappear and chat errors with "Invalid or
// missing token…". Re-login was the only fix. Now we do that automatically:
// wipe every identity key (so the reload can't re-enter the zombie state) and
// reload to a clean signed-out app with the sign-in form open. Once-only.
let _authExpiredHandled = false;
window.addEventListener('webagent-auth-expired', async () => {
  if (_authExpiredHandled) return;
  // An unreachable server cannot validate or refresh a token. Keep the
  // tenant-scoped browser cache readable; a real 401 after reconnect will fire
  // this event again and perform the normal security purge.
  if (isOfflineReadOnly()) return;
  _authExpiredHandled = true;
  try {
    const [{ default: sessionDB }, { syncEngine }] = await Promise.all([
      import('../../chat/js/storage/indexeddb.js'),
      import('../../chat/js/storage/sync.js'),
    ]);
    syncEngine.stopAutoSync();
    if (sessionDB.ownerScope) await sessionDB.clearAll();
    sessionDB.close();
  } catch (purgeError) {
    console.warn('[Auth] Browser storage expiry purge failed:', purgeError);
  }
  try {
    const active = getActive();
    if (active) removeAccount(active.user_id);
  } catch (_) {}
  ['auth_token', 'auth_username', 'auth_user_id', 'auth_display_name',
   'remember_token', 'anonUserId', 'terminalUserId']
    .forEach((k) => { try { localStorage.removeItem(k); } catch (_) {} });
  // Tell the next boot to surface the sign-in form (and why) instead of a silent
  // signed-out shell.
  try { sessionStorage.setItem('webagent_signed_out_expired', '1'); } catch (_) {}
  try { window.location.reload(); } catch (_) {}
});

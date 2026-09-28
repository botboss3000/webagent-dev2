'use strict';

/**
 * Keep the logged-in user's access token fresh.
 *
 * The access token (JWT) lives for 24h (see app/auth/jwt.py). Nothing on the
 * normal path refreshes it — a plain page load just trusts whatever
 * `auth_token` is in localStorage. So once the token lapses while a tab is
 * open (or the user comes back later in the day), the backend can no longer
 * identify the caller from the stale token, and authenticated features break
 * in two different ways:
 *
 *   1. Routes that resolve identity for an ACL check but don't hard-fail
 *      return HTTP 200 with a "restricted" body. The worst offender is
 *      GET /api/v1/db/session-messages: an unreadable token means the
 *      participant check sees no identity and returns `{restricted: true}`,
 *      and loadSessionChat() then *silently bounces the user into a fresh
 *      empty session*. That is the visible "I can see the current chat but I
 *      can't switch to a different session" bug — every session you pick
 *      throws you into a new one.
 *   2. Routes guarded by require_db_auth / _require_auth (DB viewer, agent
 *      edits, profile, billing…) return HTTP 401.
 *
 * The already-open agent WebSocket authenticates only once at connect time,
 * so live chat keeps working the whole time — which is why it looks like
 * "chat is fine but everything else is broken", and why only a re-login (a
 * fresh token) fixes it while a plain refresh does not (it reuses the stale
 * token).
 *
 * This module fixes both at the source by keeping the token valid:
 *
 *   • Proactive refresh — decode the token's `exp` and, when it is expired or
 *     about to expire, exchange the stored remember_token for a fresh JWT via
 *     /api/v1/auth/recall (the same mechanism the multi-account switcher in
 *     accounts.js already uses). Run at boot, on a periodic timer, and when
 *     the tab regains focus. A valid token means the participant check and
 *     every authed route just work — no restricted bounce, no 401.
 *
 *   • Reactive backstop — a window.fetch wrapper that, on any 401 from a
 *     same-origin /api/ or /admin/ call, refreshes once and retries. Catches
 *     the boundary race (token expires between the freshness check and the
 *     call) and any path the proactive timer missed.
 *
 * On a successful refresh it fires `webagent-token-refreshed` (main.js
 * reconnects the agent WebSocket with the new token); on an unrecoverable
 * failure it fires `webagent-auth-expired`. It composes with the billing 402
 * interceptor (billing.js): both wrap window.fetch and each guards against
 * double-install with its own flag.
 */

import { getActive, upsertAccount } from './accounts.js';
import { apiPath, appBasePath } from './config.js';

// Real fetch, captured at module-load (before any wrapping). This module's
// own calls — and the interceptor's retry — go straight here so we never
// recurse through our own (or billing's) wrapper.
const _nativeFetch = window.fetch.bind(window);

// Refresh when the token has this much (or less) time left, so a request
// never races a same-second expiry.
const _SKEW_MS = 120000; // 2 minutes
// How often the background timer re-checks freshness.
const _CHECK_INTERVAL_MS = 60000; // 1 minute
// Back-off after a recall genuinely fails (revoked/missing remember token, or
// the server erred) so we don't hammer /auth/recall.
const _COOLDOWN_MS = 30000;

let _refreshPromise = null; // single-flight recall
let _cooldownUntil = 0;
let _started = false;       // startTokenAutoRefresh() idempotency
let _lastRefreshNetworkFailure = false;

// A token can be rejected by the server (401) while its own `exp` is still in
// the future — the classic case is a signing-key change after a redeploy: the
// browser's pass looks valid to the client (decodes, unexpired) but fails the
// server's signature check, so it is unrecoverable without a fresh login. We
// cannot tell that from a one-off transient 401, so we require a short CASCADE
// of refresh-failed 401s before concluding the session is dead — enough that a
// genuine bad-signature boot (which fires many authed calls at once) trips it,
// while a single blip never signs anyone out spuriously.
let _unrecoverable401s = 0;
const _MAX_UNRECOVERABLE_401S = 3;

// ── JWT helpers ──────────────────────────────────────────────────────────

/** Read the `exp` claim (epoch ms) from a JWT without verifying it. */
function _decodeJwtExpMs(token) {
  if (!token || typeof token !== 'string') return null;
  const parts = token.split('.');
  if (parts.length < 2) return null;
  try {
    let b64 = parts[1].replace(/-/g, '+').replace(/_/g, '/');
    while (b64.length % 4) b64 += '=';
    const claims = JSON.parse(atob(b64));
    return typeof claims.exp === 'number' ? claims.exp * 1000 : null;
  } catch (_) {
    return null;
  }
}

function _activeToken(acct) {
  return (acct && acct.access_token) || localStorage.getItem('auth_token') || '';
}

/** A real signed-in member account — not an anonymous/guest identity, and not
 *  absent. Only members can be (and should be) signed out when their session
 *  dies; guests are re-minted at boot and the open-mode admin needs no token. */
function _isMemberAccount(acct) {
  return !!(acct && acct.user_id && acct.user_id.indexOf('anon_') !== 0);
}

/** True only when the active token's `exp` is provably in the past. Conservative
 *  on purpose: an unknown/malformed exp does NOT count as "expired" here, so the
 *  auto-sign-out below never fires on a token we merely can't read. */
function _tokenDefinitelyExpired(acct) {
  const expMs = _decodeJwtExpMs(_activeToken(acct));
  return typeof expMs === 'number' && expMs <= Date.now();
}

// ── Unrecoverable session → announce once so the app can sign out cleanly ────
//
// A signed-in member whose access token has expired with no way to refresh it
// (no remember-token, or the remember-token itself was revoked) is stuck in a
// "signed-in but powerless" state: the token is still in localStorage so the UI
// looks logged-in, but the server rejects it, so Admin Tools vanish and the chat
// WebSocket errors with "Invalid or missing token…". We fire `webagent-auth-expired`
// exactly once; a single global handler (main.js) does the clean sign-out +
// reload to the sign-in form. Guarded so it can NEVER sign out the wrong caller:
// not anonymous/guest, not the open-mode local admin (server skips JWT there),
// and not a public per-agent page (its anon session re-mints itself).
let _expiredAnnounced = false;
function _announceAuthExpired(acct) {
  if (_expiredAnnounced) return;
  if (!_isMemberAccount(acct)) return;
  if (!localStorage.getItem('auth_token')) return;
  try { if (typeof window !== 'undefined' && window.__agentId) return; } catch (_) {}
  _expiredAnnounced = true;
  try { window.dispatchEvent(new CustomEvent('webagent-auth-expired')); } catch (_) {}
}

// ── Core: exchange remember_token → fresh JWT ──────────────────────────────

/**
 * Recall a fresh JWT using the active account's remember_token. Returns the
 * new access token, or null if we can't (no remember token, revoked, server
 * error). Single-flight + short cooldown on failure.
 */
function _refreshToken() {
  if (_refreshPromise) return _refreshPromise;
  if (Date.now() < _cooldownUntil) return Promise.resolve(null);

  const p = (async () => {
    _lastRefreshNetworkFailure = false;
    const acct = getActive();
    const rememberToken = acct && acct.remember_token;
    if (!rememberToken) {
      // Anonymous visitors and "Remember me"-unchecked logins have no
      // persistent credential — nothing we can silently do here.
      _cooldownUntil = Date.now() + _COOLDOWN_MS;
      return null;
    }
    try {
      const res = await _nativeFetch(apiPath('/api/v1/auth/recall'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ remember_token: rememberToken }),
      });
      if (!res.ok) {
        _cooldownUntil = Date.now() + _COOLDOWN_MS;
        // A 401 means the remember token itself is dead (e.g. rotated by a
        // password change) — re-auth is genuinely required. Announce it so the
        // global handler signs the member out cleanly (guarded inside).
        if (res.status === 401) {
          _announceAuthExpired(getActive());
        }
        return null;
      }
      const data = await res.json();
      if (!data || !data.access_token) {
        _cooldownUntil = Date.now() + _COOLDOWN_MS;
        return null;
      }
      // Persist the fresh token. upsertAccount() also mirrors the legacy
      // auth_token key, so the next WS (re)connect picks it up automatically.
      upsertAccount({
        ...acct,
        access_token: data.access_token,
        username: data.username || acct.username,
        display_name: data.display_name || acct.display_name,
      });
      try {
        window.dispatchEvent(new CustomEvent('webagent-token-refreshed', {
          detail: { access_token: data.access_token },
        }));
      } catch (_) {}
      return data.access_token;
    } catch (_) {
      _lastRefreshNetworkFailure = true;
      _cooldownUntil = Date.now() + _COOLDOWN_MS;
      return null;
    }
  })();

  _refreshPromise = p;
  // Reset single-flight once this burst's recall settles so a later expiry
  // can recall again.
  p.finally(() => { if (_refreshPromise === p) _refreshPromise = null; });
  return p;
}

// ── Proactive freshness ────────────────────────────────────────────────────

/**
 * Ensure the access token is valid for at least the skew window; refresh it
 * if it is missing/expired/about-to-expire. No-op (resolves immediately) when
 * the token is still good or when there's nothing we can refresh from. Safe
 * to call as often as you like.
 */
export async function ensureFreshToken() {
  const acct = getActive();
  // Without a persistent credential we cannot recall a fresh token. Do not
  // purge solely from the unverified client-side expiry claim: the app may be
  // opening its cache offline. A real server 401 below remains authoritative
  // and performs the normal sign-out/purge when connectivity exists.
  if (!acct || !acct.remember_token) {
    return;
  }
  const expMs = _decodeJwtExpMs(_activeToken(acct));
  // Unknown exp (malformed/missing token) → refresh; otherwise refresh only
  // when we're inside the skew window.
  if (expMs === null || (expMs - Date.now()) <= _SKEW_MS) {
    const fresh = await _refreshToken();
    // Recall couldn't recover it and the token is provably dead → unrecoverable.
    if (!fresh && !_lastRefreshNetworkFailure && _tokenDefinitelyExpired(getActive())) {
      _announceAuthExpired(getActive());
    }
  }
}

// If token expiry was noticed while the server was unreachable, main.js keeps
// the tenant cache open instead of purging it. Once health recovers, allow one
// fresh validation attempt; a real revocation/401 then follows the normal
// sign-out and browser-data purge path.
window.addEventListener('webagent-offline-readonly-changed', (event) => {
  if (event?.detail?.active !== false) return;
  _expiredAnnounced = false;
  ensureFreshToken();
});

/**
 * Start the background keep-fresh loop: a periodic freshness check plus a
 * re-check whenever the tab regains focus (covers timers throttled while the
 * tab was backgrounded / the machine asleep). Idempotent.
 */
export async function connectionTicket(path) {
  await ensureFreshToken();
  const token = _activeToken(getActive());
  if (!token) return '';
  try {
    const response = await _nativeFetch('/api/v1/auth/connection-ticket', {
      method: 'POST',
      headers: {
        'Authorization': 'Bearer ' + token,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ path }),
    });
    if (!response.ok) return '';
    const data = await response.json();
    return typeof data.ticket === 'string' ? data.ticket : '';
  } catch (_) {
    return '';
  }
}

export function startTokenAutoRefresh() {
  if (_started) return;
  _started = true;
  setInterval(() => { ensureFreshToken(); }, _CHECK_INTERVAL_MS);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') ensureFreshToken();
  });
}

// ── Reactive backstop: refresh + retry on 401 ──────────────────────────────

/**
 * Decide whether a 401 from this request is worth a refresh + retry. Only
 * same-origin app API calls; never /api/v1/auth/* (that would recurse, and
 * switchTo()/_probeToken in accounts.js rely on seeing a raw 401 there).
 */
function _isRefreshableApiCall(input) {
  const urlStr = (typeof input === 'string')
    ? input
    : (input && typeof input.url === 'string' ? input.url : '');
  if (!urlStr) return false;

  let path;
  try {
    const u = new URL(urlStr, window.location.origin);
    if (u.origin !== window.location.origin) return false; // same-origin only
    path = u.pathname;
  } catch (_) {
    path = urlStr.split('?')[0];
  }

  // Strip the subdirectory base (e.g. /webagent project-site hosting).
  const base = appBasePath();
  if (base && path.startsWith(base)) path = path.slice(base.length) || '/';

  if (path.startsWith('/api/v1/auth/')) return false;
  return path.startsWith('/api/') || path.startsWith('/admin/');
}

/** Remove bearer credentials from same-origin fetch URLs. */
function _withoutTokenInUrl(urlStr) {
  try {
    const u = new URL(urlStr, window.location.origin);
    if (!u.searchParams.has('token')) return urlStr;
    u.searchParams.delete('token');
    return u.toString();
  } catch (_) {
    return urlStr;
  }
}

/** Return a copy of init with a fresh Authorization: Bearer header. */
function _setAuthHeader(init, token) {
  const next = Object.assign({}, init);
  const headers = next.headers;
  const bearer = 'Bearer ' + token;

  if (headers instanceof Headers) {
    const h = new Headers(headers);
    h.set('Authorization', bearer);
    next.headers = h;
  } else if (Array.isArray(headers)) {
    const h = headers.filter(([k]) => String(k).toLowerCase() !== 'authorization');
    h.push(['Authorization', bearer]);
    next.headers = h;
  } else {
    const h = Object.assign({}, headers);
    for (const k of Object.keys(h)) {
      if (k.toLowerCase() === 'authorization') delete h[k];
    }
    h['Authorization'] = bearer;
    next.headers = h;
  }
  return next;
}

/** True if `init` already carries an Authorization header (any header shape). */
function _hasAuthHeader(init) {
  const h = init && init.headers;
  if (!h) return false;
  if (h instanceof Headers) return h.has('Authorization');
  if (Array.isArray(h)) return h.some(([k]) => String(k).toLowerCase() === 'authorization');
  return Object.keys(h).some((k) => k.toLowerCase() === 'authorization');
}

async function _patchedFetch(input, init) {
  const method = String(
    (init && init.method) || (typeof Request !== 'undefined' && input instanceof Request && input.method) || 'GET'
  ).toUpperCase();
  if (window.__webagentOfflineReadOnly === true && method !== 'GET' && method !== 'HEAD') {
    return new Response(JSON.stringify({
      detail: 'Offline · cached data is read-only',
      offline_readonly: true,
    }), {
      status: 503,
      statusText: 'Offline Read Only',
      headers: { 'Content-Type': 'application/json' },
    });
  }
  // Proactively attach the signed token to our OWN same-origin /api/ and /admin/
  // calls that don't already carry one. Admin endpoints now require a
  // cryptographically-verified caller (app/auth/identity.resolve_admin_uid) — but
  // many admin pages call raw fetch() with only `?requesting_user_id=` and no
  // token. Without this they'd 403 on the first try and rely on the 401-retry
  // (which only fires when a remember_token recall succeeds). Same allowlist as
  // the retry (_isRefreshableApiCall excludes /api/v1/auth/*). String inputs
  // only, so a Request object's own headers are never clobbered. Public
  // endpoints simply ignore the extra Authorization header.
  if (typeof input === 'string' && _isRefreshableApiCall(input)) {
    input = _withoutTokenInUrl(input);
    if (!_hasAuthHeader(init)) {
      const tok = _activeToken(getActive());
      if (tok) init = _setAuthHeader(init || {}, tok);
    }
  }
  let resp;
  try {
    resp = await _nativeFetch(input, init);
  } catch (e) {
    // Network error (navigation, server restart, brief hiccup) — return a
    // sentinel so every polling/background call handles it gracefully rather
    // than throwing TypeError through every wrapper. 503 = Service Unavailable
    // captures the intent and is a valid Response constructor status.
    return new Response(null, { status: 503, statusText: "Server Unreachable" });
  }
  if (resp.status !== 401) {
    // A refreshable authed call that did NOT 401 means our credentials work —
    // clear any accumulated unrecoverable-401 streak so an earlier blip can't
    // add up to a false sign-out later.
    if (_unrecoverable401s && _isRefreshableApiCall(input)) _unrecoverable401s = 0;
    return resp;
  }
  // Guard: never retry a request we already retried (belt-and-suspenders —
  // the retry below goes through native fetch and won't re-enter here).
  if (init && init.__webagentAuthRetried) return resp;
  if (!_isRefreshableApiCall(input)) return resp;

  const token = await _refreshToken();
  if (!token) {
    // Couldn't refresh. The session is unrecoverable when the token is provably
    // expired OR the server keeps rejecting a still-"unexpired" token — a
    // bad-signature cascade after a redeploy changed the signing key (the token
    // decodes and looks valid to us, but fails the server's check). Either way,
    // announce a clean sign-out rather than letting every authed call 401 behind
    // a UI that still looks logged in. The streak threshold keeps a lone
    // transient 401 from ever tripping it.
    _unrecoverable401s += 1;
    if (_tokenDefinitelyExpired(getActive()) || _unrecoverable401s >= _MAX_UNRECOVERABLE_401S) {
      _announceAuthExpired(getActive());
    }
    return resp; // surface the original 401
  }
  // Refresh recovered a fresh token — the session is alive again.
  _unrecoverable401s = 0;

  const retryInput = (typeof input === 'string')
    ? _withoutTokenInUrl(input)
    : input;
  const retryInit = _setAuthHeader(init || {}, token);
  retryInit.__webagentAuthRetried = true;
  return _nativeFetch(retryInput, retryInit);
}

/**
 * Wrap window.fetch with the 401 → recall → retry behavior. Idempotent.
 * Call once, early in boot, before any authenticated fetch fires.
 */
export function installAuthRefresh() {
  if (window.fetch && window.fetch.__webagentAuthPatched) return;
  _patchedFetch.__webagentAuthPatched = true;
  window.fetch = _patchedFetch;
}

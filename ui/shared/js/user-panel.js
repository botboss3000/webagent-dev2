'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * User Panel — header user dropdown, inline login, account switching, theme.
 *
 * Controls the #user-dropdown element in the main header: avatar, inline login
 * form, account list, theme buttons, manage/add/signout actions.
 */

import { app } from './state.js';
import { renderAvatar } from './user-avatar.js';
import {
  listAccounts,
  getActive,
  removeAccount,
  switchTo,
  upsertAccount,
  onChange as onAccountsChange,
} from './accounts.js';
import { purgeAndAcknowledge, startDevicePurgePolling } from './device-purge.js';
import { authHeaders, showLeftOverlay } from './left-login.js';
import { socialIconSvg } from './social-icons.js';
import { closeMobileNavMenu } from './mobile-navigation.js';

function loginDeviceId() {
  const key = 'webagent_device_id';
  let value = localStorage.getItem(key) || '';
  if (!value) {
    try {
      const activeToken = getActive()?.access_token || localStorage.getItem('auth_token') || '';
      const encoded = activeToken.split('.')[1].replace(/-/g, '+').replace(/_/g, '/');
      const padded = encoded.padEnd(Math.ceil(encoded.length / 4) * 4, '=');
      value = String(JSON.parse(atob(padded)).device_id || '');
    } catch (_) {}
  }
  if (!value) {
    value = globalThis.crypto?.randomUUID?.().replace(/-/g, '')
      || `${Date.now().toString(36)}${Math.random().toString(36).slice(2)}`;
  }
  localStorage.setItem(key, value);
  return value;
}

// ── Server-reachability guard ──────────────────────────────────────────────
// Used by setChatHeaderReachable to avoid redundant DOM writes.
let _serverReachable = true;

/**
 * Toggle the session-dropdown between normal and offline-spinner states.
 * Called by reconnect.js's health poll.
 */
export function setChatHeaderReachable(reachable) {
  reachable = reachable !== false;
  if (reachable === _serverReachable) return;
  _serverReachable = reachable;
  const dropdown = document.getElementById('session-dropdown');
  if (dropdown) {
    if (_serverReachable) dropdown.removeAttribute('data-offline');
    else dropdown.dataset.offline = 'true';
  }
}

// ── Header avatar ─────────────────────────────────────────────────────────

export async function populateUserSelect() {
  // Header avatar (letter icon) + tooltip with full username
  const slot = document.getElementById('top-user-avatar-slot');
  if (slot) {
    slot.innerHTML = '';
    const active = getActive();
    const acct = active || {
      display_name: app.currentUserId || 'None',
      username: app.currentUserId || '',
    };
    slot.appendChild(renderAvatar(acct, 'sm'));
    const trigger = document.getElementById('top-user-id');
    if (trigger) trigger.title = acct.username || acct.display_name || '';
  }
  // Re-render the dropdown contents so the current-row + other accounts stay fresh
  renderUserDropdown();
  // Refresh the agent list, which sets currentAgentId. Sessions are now
  // loaded lazily when the user first opens the session dropdown (see
  // openMenu in initSessions). Skipped while the session menu is open so
  // the user doesn't lose their place mid-click.
  if (app.currentUserId) {
    const menu = document.getElementById('session-dropdown-menu');
    const isOpen = menu && !menu.hidden;
    if (!isOpen) {
      // populateAgentSelect is set on app by sessions.js's registerSessionApi
      if (typeof app.populateAgentSelect === 'function') {
        // Initial session hydration depends on the selected agent being final.
        // Await this so populateUserSelect() is a real boot barrier instead of
        // resolving while the agent selector is still changing app state.
        await app.populateAgentSelect(app.currentUserId);
      }
    }
  }
}

// Export as app.populateUserSelect (set in initUserPanel)

// ── User dropdown contents ────────────────────────────────────────────────

/** An Open-Registration guest holds an anon_* identity — NOT a real member.
 *  The dropdown treats such a visitor exactly like a logged-out one: it shows
 *  the inline Sign-in form instead of the account row + management actions. */
function isAnonAccount(acct) {
  return !!acct && String(acct.user_id || '').indexOf('anon_') === 0;
}

let _creditsRequestId = 0;

// HOUSE CREDITS — the app-wide spend unit the model catalog publishes model
// prices in (ui/shared/js/model-table.js). The wallet stores USD cents and the
// platform converts at USD × 1000 — i.e. 10 credits per cent — the same scale
// plugins/billing/charge.py locks per call and the chat footer's ◇ total sums.
// The account summary reports the balance in that unit, not dollars, so it
// matches the model table the user compares it against.
const HOUSE_CREDITS_PER_CENT = 10;

function _formatCredits(cents) {
  const credits = Math.round(Number(cents || 0) * HOUSE_CREDITS_PER_CENT);
  return credits.toLocaleString(undefined, { maximumFractionDigits: 0 });
}

/** Paint every credits surface from ONE wallet read so the account-panel row
 *  (#dropdown-credits) and the compact-sidebar counter (#mobile-nav-credits)
 *  can never disagree. Both render the same house-credit unit. */
function _setCreditsText(text) {
  for (const id of ['dropdown-credits-value', 'mobile-nav-credits-value']) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
  }
}

async function refreshUserDropdownCredits(userId) {
  const row = document.getElementById('dropdown-credits');
  const mobileRow = document.getElementById('mobile-nav-credits');
  if ((!row && !mobileRow) || !userId) return;

  const requestId = ++_creditsRequestId;
  _setCreditsText('Loading…');
  try {
    const response = await fetch(`/api/v1/billing/wallet?user_id=${encodeURIComponent(userId)}`, {
      headers: authHeaders(),
      cache: 'no-store',
    });
    if (!response.ok) throw new Error(`Wallet request failed (${response.status})`);
    const wallet = await response.json();
    // The account may have changed while the request was in flight.
    if (requestId !== _creditsRequestId || getActive()?.user_id !== userId) return;
    _setCreditsText(_formatCredits(wallet.available_cents ?? wallet.balance_cents));
  } catch (_) {
    if (requestId === _creditsRequestId) _setCreditsText('Unavailable');
  }
}

/** Render the contents of #user-dropdown-menu's dynamic sections. */
function renderUserDropdown() {
  const active = getActive();
  const all = listAccounts();
  // `member` = signed in as a real account. Anonymous guests render as logged-out.
  const member = !!active && !isAnonAccount(active);

  // Current user row (large avatar + name + email) — or inline login form
  const cur = document.getElementById('dropdown-current-row');
  if (cur) {
    cur.innerHTML = '';
    if (member) {
      // Reset any inline styles that the login form may have set
      cur.style.padding = '';
      cur.style.flexDirection = '';
      cur.style.alignItems = '';
      cur.style.gap = '';
      cur.appendChild(renderAvatar(active, 'lg'));
      const info = document.createElement('div');
      info.className = 'user-dropdown-current-info';
      const nameEl = document.createElement('div');
      nameEl.className = 'user-dropdown-current-name';
      nameEl.textContent = active.display_name || active.username || 'User';
      const emailEl = document.createElement('div');
      emailEl.className = 'user-dropdown-current-email';
      emailEl.textContent = active.username || '';
      info.appendChild(nameEl);
      info.appendChild(emailEl);
      cur.appendChild(info);
    } else {
      // Inline login form inside the dropdown — no full-page modal
      cur.style.padding = '10px 14px 6px';
      cur.style.flexDirection = 'column';
      cur.style.alignItems = 'stretch';
      cur.style.gap = '8px';
      mountAccountSignInForm(cur);
    }
  }

  // Other accounts (non-active)
  const list = document.getElementById('dropdown-other-accounts');
  if (list) {
    list.innerHTML = '';
    const others = all.filter((a) => !isAnonAccount(a) && (!active || a.user_id !== active.user_id));
    for (const acct of others) {
      const row = document.createElement('button');
      row.type = 'button';
      row.className = 'user-dropdown-account-row';
      row.dataset.userId = acct.user_id;
      row.appendChild(renderAvatar(acct, 'md'));
      const text = document.createElement('div');
      text.className = 'user-dropdown-account-info';
      text.innerHTML = `
        <div class="user-dropdown-account-name"></div>
        <div class="user-dropdown-account-email"></div>
      `;
      text.querySelector('.user-dropdown-account-name').textContent = acct.display_name || acct.username || acct.user_id;
      text.querySelector('.user-dropdown-account-email').textContent = acct.username || '';
      row.appendChild(text);
      row.addEventListener('click', async (e) => {
        e.stopPropagation();
        const ok = await switchTo(acct.user_id);
        if (ok) {
          window.location.reload();
        } else {
          // recall failed — fall back to login overlay
          showLeftOverlay();
        }
      });
      list.appendChild(row);
    }
  }

  // Toggle visibility of signed-in-only elements
  const manageBtn = document.getElementById('btn-manage-account');
  const creditsRow = document.getElementById('dropdown-credits');
  const mobileCreditsRow = document.getElementById('mobile-nav-credits');
  const addBtn = document.getElementById('btn-add-account');
  const signoutBtn = document.getElementById('btn-signout-header');
  const dividers = document.querySelectorAll('#user-dropdown-menu > .user-dropdown-divider');
  const otherAccounts = document.getElementById('dropdown-other-accounts');
  if (member) {
    if (manageBtn) manageBtn.style.display = '';
    if (creditsRow) creditsRow.hidden = false;
    // The compact-sidebar counter mirrors the account-panel row (mobile mode).
    if (mobileCreditsRow) mobileCreditsRow.hidden = false;
    void refreshUserDropdownCredits(active.user_id);
    if (addBtn) addBtn.style.display = '';
    if (signoutBtn) signoutBtn.style.display = '';
    dividers.forEach(d => d.style.display = '');
    if (otherAccounts) otherAccounts.style.display = '';
  } else {
    if (manageBtn) manageBtn.style.display = 'none';
    if (creditsRow) creditsRow.hidden = true;
    if (mobileCreditsRow) mobileCreditsRow.hidden = true;
    _setCreditsText('—');
    if (addBtn) addBtn.style.display = 'none';
    if (signoutBtn) signoutBtn.style.display = 'none';
    dividers.forEach(d => d.style.display = 'none');
    if (otherAccounts) otherAccounts.style.display = 'none';
  }

  // Refresh Lucide icons for any newly-inserted SVG containers
  try {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons();
    }
  } catch (_) {}
}


/** Shared account sign-in/register form. Instance-scoped for the private gate and dropdown. */
export function mountAccountSignInForm(cur, prefix = 'dd') {
  cur.innerHTML = `
    <form id="dd-login-form" action="/api/v1/auth/login" method="post" autocomplete="on" aria-label="Account sign in" style="display:flex;flex-direction:column;gap:inherit;width:100%;margin:0;">
    <div id="dd-login-title" style="font-size:13px;font-weight:600;color:var(--fg-2);margin-bottom:2px;">Sign in</div>
    <div id="dd-social-login" style="display:none;flex-direction:column;gap:8px;"></div>
    <div id="dd-social-or" style="display:none;align-items:center;gap:8px;color:var(--fg-3);font-size:11px;">
      <span style="flex:1;height:1px;background:var(--border);"></span>or<span style="flex:1;height:1px;background:var(--border);"></span>
    </div>
    <input type="text" name="username" id="dd-login-email" placeholder="Email or username" aria-label="Email or username" autocomplete="username" style="
      width:100%;padding:8px 10px;background:var(--bg-0);border:1px solid var(--border);
      border-radius:6px;color:var(--fg-1);font-size:13px;font-family:inherit;
      outline:none;box-sizing:border-box;
    ">
    <input type="password" name="password" id="dd-login-pass" placeholder="Password" aria-label="Password" autocomplete="current-password" style="
      width:100%;padding:8px 10px;background:var(--bg-0);border:1px solid var(--border);
      border-radius:6px;color:var(--fg-1);font-size:13px;font-family:inherit;
      outline:none;box-sizing:border-box;
    ">
    <input type="text" id="dd-register-name" placeholder="Display name" aria-label="Display name" autocomplete="name" hidden style="
      width:100%;padding:8px 10px;background:var(--bg-0);border:1px solid var(--border);
      border-radius:6px;color:var(--fg-1);font-size:13px;font-family:inherit;
      outline:none;box-sizing:border-box;
    ">
    <div style="display:flex;align-items:center;gap:6px;">
      <input type="checkbox" id="dd-login-remember" checked style="accent-color:var(--brand);margin:0;">
      <label for="dd-login-remember" style="font-size:12px;color:var(--fg-3);cursor:pointer;">Remember me</label>
    </div>
    <button id="dd-login-btn" type="submit" style="
      width:100%;padding:8px;background:var(--brand);border:none;border-radius:6px;
      color:var(--bg-0);font-size:14px;font-weight:700;cursor:pointer;
    ">Sign In</button>
    <button id="dd-register-toggle" type="button" style="
      width:100%;padding:6px;background:transparent;border:none;
      color:var(--accent);font-size:12px;font-weight:600;cursor:pointer;
    ">Create an account</button>
    <div id="dd-login-error" style="color:var(--danger);font-size:12px;display:none;"></div>
    <div id="dd-login-loading" style="color:var(--fg-3);font-size:12px;text-align:center;display:none;">Signing in…</div>
    </form>
  `;
  cur.querySelectorAll('[id]').forEach(el => { el.id = el.id.replace('dd-', prefix + '-'); });
  cur.querySelectorAll('[for]').forEach(el => { el.htmlFor = el.htmlFor.replace('dd-', prefix + '-'); });
  const field = id => cur.querySelector('#' + id.replace('dd-', prefix + '-'));

  // Wire up the inline login form
  const emailInput = field('dd-login-email');
  const passInput = field('dd-login-pass');
  const loginBtn = field('dd-login-btn');
  const titleEl = field('dd-login-title');
  const nameInput = field('dd-register-name');
  const registerToggle = field('dd-register-toggle');
  const errorEl = field('dd-login-error');
  const loadingEl = field('dd-login-loading');
  let registerMode = false;

  registerToggle.addEventListener('click', () => {
    registerMode = !registerMode;
    titleEl.textContent = registerMode ? 'Create account' : 'Sign in';
    loginBtn.textContent = registerMode ? 'Create Account' : 'Sign In';
    registerToggle.textContent = registerMode ? 'Back to sign in' : 'Create an account';
    nameInput.hidden = !registerMode;
    passInput.autocomplete = registerMode ? 'new-password' : 'current-password';
    errorEl.style.display = 'none';
  });

  async function doInlineLogin() {
    const email = emailInput.value.trim();
    const password = passInput.value;
    const rememberMe = field('dd-login-remember').checked;
    errorEl.style.display = 'none';
    errorEl.style.color = 'var(--danger)';
    loginBtn.disabled = true;
    loadingEl.style.display = 'block';
    try {
      if (registerMode) {
        const anonToken = localStorage.getItem('auth_token') || '';
        const registerRes = await fetch('/api/v1/auth/register', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            ...(anonToken ? { Authorization: `Bearer ${anonToken}` } : {}),
          },
          body: JSON.stringify({
            email,
            password,
            display_name: nameInput.value.trim(),
          }),
        });
        const registerData = await registerRes.json().catch(() => ({}));
        if (!registerRes.ok) {
          errorEl.style.display = 'block';
          errorEl.textContent = registerRes.status === 409
            ? 'That email is already registered'
            : (registerData.detail || 'Could not create account');
          loginBtn.disabled = false;
          loadingEl.style.display = 'none';
          return;
        }
        if (registerData.pending_approval) {
          errorEl.style.display = 'block';
          errorEl.style.color = 'var(--fg-2)';
          errorEl.textContent = 'Account created and waiting for administrator approval.';
          loginBtn.disabled = false;
          loadingEl.style.display = 'none';
          return;
        }
        upsertAccount({
          user_id: registerData.user_id,
          username: registerData.username,
          display_name: registerData.display_name,
          access_token: registerData.access_token,
          remember_token: '',
        });
        window.location.reload();
        return;
      }
      const res = await fetch('/api/v1/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          email,
          password,
          remember_me: rememberMe,
          device_id: loginDeviceId(),
        }),
      });
      if (!res.ok) {
        errorEl.style.display = 'block';
        if (res.status === 429) {
          const seconds = Number(res.headers.get('Retry-After'));
          const wait = Number.isFinite(seconds) && seconds > 0
            ? ` Wait about ${Math.ceil(seconds / 60)} minute(s) before trying again.`
            : ' Please wait before trying again.';
          errorEl.textContent = 'Too many sign-in attempts.' + wait;
        } else if (res.status === 401) {
          errorEl.textContent = 'Invalid email or password';
        } else if (res.status === 403) {
          errorEl.textContent = 'Account pending approval';
        } else {
          errorEl.textContent = 'Sign-in is temporarily unavailable. Please try again later.';
        }
        loginBtn.disabled = false;
        loadingEl.style.display = 'none';
        return;
      }
      const data = await res.json();
      const { upsertAccount } = await import('./accounts.js');
      upsertAccount({
        user_id: data.user_id,
        username: data.username,
        display_name: data.display_name,
        access_token: data.access_token,
        remember_token: data.remember_token || '',
      });
      window.location.reload();
    } catch (err) {
      errorEl.style.display = 'block';
      errorEl.textContent = 'Connection error';
      loginBtn.disabled = false;
      loadingEl.style.display = 'none';
    }
  }

  // Native form ownership and stable field names let password managers classify
  // both instances identically. Keep authentication in the existing API handler.
  field('dd-login-form').addEventListener('submit', (e) => {
    e.preventDefault();
    if (!loginBtn.disabled) void doInlineLogin();
  });
  emailInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); passInput.focus(); }
  });

  // Populate the "Continue with …" buttons for any enabled social providers.
  _renderSocialButtons(cur);
}

// ── Social sign-in buttons ──────────────────────────────────────────────────
// Shown in the header login form when the admin has enabled + configured any
// drop-in providers (plugins/auth_providers/). A button opens the provider in a
// popup; the server callback page postMessages the minted token back to this
// window, which _onSocialMessage stores as a signed-in account.

let _socialMsgWired = false;

async function _renderSocialButtons(root) {
  const box = root.querySelector('[id$="-social-login"]');
  const orDiv = root.querySelector('[id$="-social-or"]');
  if (!box) return;
  let providers = [];
  try {
    const res = await fetch('/api/v1/auth/social/providers', { cache: 'no-store' });
    if (res.ok) providers = (await res.json()).providers || [];
  } catch (_) { providers = []; }
  if (!providers.length) {
    box.style.display = 'none';
    if (orDiv) orDiv.style.display = 'none';
    return;
  }
  box.innerHTML = '';
  for (const p of providers) {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.style.cssText = `display:flex;align-items:center;justify-content:center;gap:9px;` +
      `width:100%;padding:9px 10px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;` +
      `font-family:inherit;background:${p.brand_color};color:${p.text_color};` +
      `border:1px solid ${p.border ? 'var(--border)' : 'transparent'};`;
    btn.innerHTML = `<span style="display:flex;align-items:center;">${socialIconSvg(p.icon)}</span>` +
      `<span>Continue with ${_escText(p.display_name)}</span>`;
    btn.addEventListener('click', () => _openSocialLogin(p.id));
    box.appendChild(btn);
  }
  box.style.display = 'flex';
  if (orDiv) orDiv.style.display = 'flex';
  _wireSocialMessages();
}

function _escText(s) {
  const d = document.createElement('div');
  d.textContent = s == null ? '' : String(s);
  return d.innerHTML;
}

function _openSocialLogin(providerId) {
  const w = 520, h = 640;
  let feat = `width=${w},height=${h}`;
  try {
    const x = (window.outerWidth - w) / 2 + (window.screenX || 0);
    const y = (window.outerHeight - h) / 2 + (window.screenY || 0);
    feat += `,left=${Math.max(0, Math.round(x))},top=${Math.max(0, Math.round(y))}`;
  } catch (_) {}
  const deviceId = encodeURIComponent(loginDeviceId());
  window.open(`/api/v1/auth/social/${encodeURIComponent(providerId)}/start?device_id=${deviceId}`,
    'webagent_social_login', feat);
}

function _wireSocialMessages() {
  if (_socialMsgWired) return;
  _socialMsgWired = true;
  window.addEventListener('message', _onSocialMessage);
}

function _onSocialMessage(event) {
  if (event.origin !== window.location.origin) return;
  const data = event.data;
  if (!data || data.type !== 'social-login-success' || !data.access_token) return;
  try {
    upsertAccount({
      user_id: data.user_id,
      username: data.username,
      display_name: data.display_name,
      access_token: data.access_token,
      remember_token: '',
    });
  } catch (_) {}
  window.location.reload();
}

// ── Theme system ──────────────────────────────────────────────────────────

const _STORAGE_KEY = 'webagent_theme';

/** Set theme on <body>: 'light', 'dark', or 'system' (follow OS).
 *  Also updates the PWA theme-color meta tag to match. */
function applyTheme(theme) {
  const body = document.body;
  if (theme === 'light') {
    body.classList.add('light-mode');
  } else if (theme === 'dark') {
    body.classList.remove('light-mode');
  } else {
    // system — follow OS preference
    const prefersLight = window.matchMedia('(prefers-color-scheme: light)').matches;
    body.classList.toggle('light-mode', prefersLight);
  }
  // Sync PWA theme-color with current background
  const isLight = body.classList.contains('light-mode');
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = isLight ? '#faf5ee' : '#0d0d1a';
}

/** Highlight the matching theme button. Queries the WHOLE document, so every
 *  `.theme-option` set on the page (the user dropdown AND the App Settings →
 *  Design header switch) is kept in sync from one call. Exported so other panels
 *  can re-affirm the highlight on their own buttons when they mount. */
export function highlightThemeOption(theme) {
  document.querySelectorAll('.theme-option').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.theme === theme);
  });
}

/** The visitor's saved theme choice ('light' | 'dark' | 'system'); 'system' on
 *  first load. Per-browser (localStorage), NOT an app-wide setting. */
export function getSavedTheme() {
  try { return localStorage.getItem(_STORAGE_KEY) || 'system'; } catch (_) { return 'system'; }
}

/** The one entry point to change the live theme: apply it to <body>, sync every
 *  `.theme-option` highlight, and persist the choice. Shared by the user-dropdown
 *  toggle and the App Settings → Design header switch so they never diverge. */
export function setTheme(theme) {
  if (!theme) return;
  applyTheme(theme);
  highlightThemeOption(theme);
  try { localStorage.setItem(_STORAGE_KEY, theme); } catch (_) {}
}

// ── Init ──────────────────────────────────────────────────────────────────

export function initUserPanel() {
  // A revoked device can still receive one narrowly-scoped purge directive and
  // acknowledge that its tenant IndexedDB was physically erased.
  startDevicePurgePolling();
  // ── Theme system ──

  // Load saved theme on init (default 'system' on first load)
  const savedTheme = getSavedTheme();
  applyTheme(savedTheme);
  highlightThemeOption(savedTheme);

  // Listen to system preference changes when in 'system' mode
  const mq = window.matchMedia('(prefers-color-scheme: light)');
  mq.addEventListener('change', () => {
    if (getSavedTheme() === 'system') applyTheme('system');
  });

  // Wire theme buttons — pointerdown is more reliable than click for small
  // targets. Only the dropdown's buttons exist at boot; the App Settings →
  // Design header switch is wired by app-settings.js when that page mounts
  // (it reuses the exported setTheme, so both stay in sync).
  // `dataset.wired` is the SHARED guard with app-settings.js's _initThemeSwitch:
  // the Design-header buttons are in the DOM at boot, so whichever pass runs
  // first wires + flags them and the other skips — never double-bound.
  document.querySelectorAll('.theme-option').forEach(btn => {
    if (btn.dataset.wired) return;
    btn.dataset.wired = '1';
    btn.addEventListener('pointerdown', (e) => {
      e.stopPropagation();
      e.preventDefault();
      setTheme(btn.dataset.theme);
    });
  });

  // ── User dropdown toggle ──
  // The trigger lives inside #main-tabs, a horizontally-scrolling carousel
  // whose overflow clips any absolutely-positioned descendant. The menu is
  // therefore position:fixed (see app1.css) and anchored under the trigger by
  // JS each time it opens, so it escapes the carousel's clip region.
  const userDropdown = document.getElementById('user-dropdown');
  const dropdownMenu = document.getElementById('user-dropdown-menu');
  const trigger = document.querySelector('.user-dropdown-trigger');

  function positionUserMenu() {
    if (!trigger || !dropdownMenu) return;
    const mobileNavigation = document.body.classList.contains('mobile-mode');
    const anchor = mobileNavigation
      ? (document.getElementById('main-header') || trigger)
      : trigger;
    const r = anchor.getBoundingClientRect();
    dropdownMenu.style.marginTop = '0';
    dropdownMenu.style.top = Math.round(r.bottom + 6) + 'px';
    // Clamp horizontally so the menu never spills past the viewport edges.
    const w = dropdownMenu.offsetWidth || 360;
    let left = mobileNavigation ? r.left + 4 : r.left;
    const maxLeft = window.innerWidth - w - 8;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    dropdownMenu.style.left = Math.round(left) + 'px';
  }

  function openUserMenu() {
    const active = getActive();
    if (active && !isAnonAccount(active)) void refreshUserDropdownCredits(active.user_id);
    if (document.body.classList.contains('mobile-mode')) {
      document.dispatchEvent(new CustomEvent('mobile-nav:user-menu-opening'));
    }
    dropdownMenu.style.display = 'block';
    userDropdown.classList.add('open');
    positionUserMenu();
  }

  function closeUserMenu() {
    dropdownMenu.style.display = 'none';
    userDropdown.classList.remove('open');
  }

  if (trigger && dropdownMenu) {
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      if (dropdownMenu.style.display === 'block') closeUserMenu();
      else openUserMenu();
    });

    // Close dropdown on outside press/click.
    // Check both the trigger wrapper AND the menu itself — the menu is a portal
    // (sibling of #main-tabs-wrap) so it is no longer a descendant of #user-dropdown.
    // Both listeners run in the CAPTURE phase: many app elements (chat message
    // sections, turn gutters, action buttons) call e.stopPropagation() in their
    // own bubble-phase click handlers, which would swallow the click before it
    // reached a bubble-phase document listener and leave the panel stuck open.
    const _isOutsideUserMenu = (e) =>
      !userDropdown.contains(e.target) && !dropdownMenu.contains(e.target);
    document.addEventListener('pointerdown', (e) => { if (_isOutsideUserMenu(e)) closeUserMenu(); }, true);
    document.addEventListener('click', (e) => { if (_isOutsideUserMenu(e)) closeUserMenu(); }, true);

    // Close on Escape
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && dropdownMenu.style.display === 'block') closeUserMenu();
    });

    // Re-anchor the fixed menu to the trigger if the viewport changes size.
    window.addEventListener('resize', () => {
      if (dropdownMenu.style.display === 'block') positionUserMenu();
    });
  }

  // ── Render dropdown contents (current row + other accounts) ──
  renderUserDropdown();

  // Re-render whenever the accounts list/active user changes
  onAccountsChange(() => renderUserDropdown());

  // ── Manage Account button (in dropdown) → switch to account tab ──
  const manageBtn = document.getElementById('btn-manage-account');
  if (manageBtn) {
    manageBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const sel = document.getElementById('main-tab-select');
      if (sel) {
        sel.value = 'account';
        sel.dispatchEvent(new Event('change'));
      }
      const menu = document.getElementById('user-dropdown-menu');
      if (menu) menu.style.display = 'none';
      const dd = document.getElementById('user-dropdown');
      if (dd) dd.classList.remove('open');
    });
  }

  // ── Available-credits row → raise the buy-credits panel ──
  // The credits figure is actionable: clicking it dismisses the account panel
  // and opens the shared buy-credits modal (ui/shared/js/billing.js) — the same
  // purchase surface the chat paywall uses.
  const creditsRow = document.getElementById('dropdown-credits');
  if (creditsRow) {
    creditsRow.addEventListener('click', (e) => {
      e.stopPropagation();
      closeUserMenu();
      if (window.AppBilling && typeof window.AppBilling.openBuyCreditsModal === 'function') {
        window.AppBilling.openBuyCreditsModal();
      }
    });
  }

  // ── Compact-sidebar credits counter → raise the buy-credits panel ──
  // Mirrors the account-panel row above for mobile users: the counter renders
  // directly above the account ("A") row in the sidebar footer, so dismiss the
  // sidebar first, then open the same shared buy-credits modal.
  const mobileCreditsBtn = document.getElementById('mobile-nav-credits');
  if (mobileCreditsBtn) {
    mobileCreditsBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      closeMobileNavMenu();
      if (window.AppBilling && typeof window.AppBilling.openBuyCreditsModal === 'function') {
        window.AppBilling.openBuyCreditsModal();
      }
    });
  }

  // ── Add account button → show login overlay for a fresh sign-in ──
  const addBtn = document.getElementById('btn-add-account');
  if (addBtn) {
    addBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const menu = document.getElementById('user-dropdown-menu');
      if (menu) menu.style.display = 'none';
      const dd = document.getElementById('user-dropdown');
      if (dd) dd.classList.remove('open');
      showLeftOverlay();
    });
  }

  // ── Sign-out button: remove active account; switch to another if possible ──
  const signoutBtn = document.getElementById('btn-signout-header');
  if (signoutBtn) {
    signoutBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      // Hide privileged state synchronously. Logout revocation and tenant purge
      // may take a couple of seconds; the outgoing account's transcript/admin
      // panels must not remain readable while those requests finish.
      signoutBtn.disabled = true;
      document.dispatchEvent(new CustomEvent('webagent-auth-transition-start'));
      let transition = document.getElementById('webagent-auth-transition');
      if (!transition) {
        transition = document.createElement('div');
        transition.id = 'webagent-auth-transition';
        transition.setAttribute('role', 'status');
        transition.setAttribute('aria-live', 'polite');
        transition.textContent = 'Signing out…';
        transition.style.cssText = [
          'position:fixed', 'inset:0', 'z-index:2147483647',
          'display:grid', 'place-items:center',
          'background:var(--bg-primary, #111)', 'color:var(--text-primary, #fff)',
          'font:500 14px system-ui, sans-serif',
        ].join(';');
        document.body.appendChild(transition);
      }
      // Revoke only this token's device before discarding the local credentials.
      const logoutToken = localStorage.getItem('auth_token') || getActive()?.access_token || '';
      try {
        const token = logoutToken;
        if (token) {
          await fetch('/api/v1/auth/logout', {
            method: 'POST',
            headers: { Authorization: `Bearer ${token}` },
          });
        }
      } catch (logoutError) {
        console.warn('[UserPanel] Device logout notification failed:', logoutError);
      }
      // Browser transcript/config databases are physically tenant-scoped.
      // Purge the active tenant before credentials change so a later account
      // can never render the previous user's device-local data.
      await purgeAndAcknowledge(logoutToken, {
        reload: false,
        forgetAccount: false,
      });
      const active = getActive();
      if (active) {
        removeAccount(active.user_id);
        const remaining = listAccounts();
        if (remaining.length > 0) {
          const next = remaining[0];
          const ok = await switchTo(next.user_id);
          if (!ok) {
            // recall failed for next — fall through to full logout reload
          }
        }
      } else {
        // No tracked accounts — clear legacy keys directly
        localStorage.removeItem('auth_token');
        localStorage.removeItem('auth_username');
        localStorage.removeItem('auth_user_id');
        localStorage.removeItem('auth_display_name');
        localStorage.removeItem('remember_token');
      }
      localStorage.removeItem('anonUserId');
      localStorage.removeItem('terminalUserId');
      window.location.reload();
    });
  }

  // ── Expose on app ──
  app.populateUserSelect = populateUserSelect;
}

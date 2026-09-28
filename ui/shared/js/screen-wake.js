'use strict';

// Screen Wake Lock — keeps the device screen on while the tab is visible.
// Uses the Screen Wake Lock API (navigator.wakeLock.request("screen")).
// The lock is automatically released when the tab is hidden and re-acquired
// on visibilitychange, which is the standard web-platform pattern.
//
// Supported: Chrome 84+, Edge 84+, Firefox 126+, Safari/iOS 16.4+.
// Silent no-op when unavailable (non-secure context, old browser).
//
// OWNER-SCOPED (ref-counted): more than one surface may independently ask for
// the screen to stay on — the app-wide "Always-on display" setting, the chat
// session-dropdown ⋯ switch, and a live Gen UI page. Each caller passes a
// stable owner id; the lock is held while at least one owner is active and
// released only when the LAST owner withdraws. This stops one surface tearing
// down (e.g. leaving a Gen UI page) from cancelling a lock another surface
// still owns.
//
// Import: import { enableWakeLock, disableWakeLock, isWakeLockActive, initWakeLock } from './screen-wake.js';

const OWNER_DEFAULT = 'default';
// The effective "Always-on display" owner. The app-wide admin setting and the
// chat session-dropdown ⋯ switch BOTH drive this one owner, so the two controls
// can never fight over the lock. Gen UI keeps its own separate owner.
const OWNER_ALWAYS_ON = 'app-setting';
// Per-device override of the app-wide always-on setting (localStorage). The
// admin setting is the server DEFAULT every visitor inherits at boot; a user
// may override it for one device from the chat session-dropdown ⋯ menu.
const ALWAYS_ON_KEY = 'webagent.always_on_display';

let _lock = null;
const _owners = new Set();    // owner ids currently wanting the screen on
let _listening = false;
let _alwaysOn = false;        // effective always-on intent (device override ?? server default)

// The screen should stay on while any owner wants it.
function _wanted() { return _owners.size > 0; }

// ── Re-acquire (visibilitychange handler) ────────────────────────────────

async function _reacquire() {
  if (!_wanted()) return;
  if (_lock !== null) return;   // still held — can happen on quick hide/show
  try {
    if (!('wakeLock' in navigator)) return;
    _lock = await navigator.wakeLock.request('screen');
    _lock.addEventListener('release', () => { _lock = null; });
    _dispatch(true);
  } catch (_) {
    // Permission denied, browser not supported, or tab not visible
    _dispatch(false);
  }
}

function _startListening() {
  if (_listening) return;
  _listening = true;
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') {
      _reacquire();
    }
  });
}

// ── Public API ────────────────────────────────────────────────────────────

/**
 * Register `owner` as wanting the screen on. Re-acquires automatically after
 * tab hide/show. Idempotent per owner. Pass a stable id naming the surface
 * (e.g. 'app-setting', 'genui') so an unrelated teardown can't
 * cancel it.
 */
export async function enableWakeLock(owner = OWNER_DEFAULT) {
  if (!('wakeLock' in navigator)) return false;
  _owners.add(owner);
  _startListening();
  await _reacquire();
  return _lock !== null;
}

/**
 * Withdraw `owner`'s request. The lock is released only once the LAST owner
 * has withdrawn — other owners holding the screen on are unaffected.
 */
export async function disableWakeLock(owner = OWNER_DEFAULT) {
  _owners.delete(owner);
  if (_owners.size > 0) return;   // another owner still wants the screen on
  try {
    if (_lock) {
      await _lock.release();
      _lock = null;
    }
  } catch (_) {
    _lock = null;
  }
  _dispatch(false);
}

/** Whether a wake lock is currently held / wanted. */
export function isWakeLockActive() {
  return _wanted() && _lock !== null;
}

export function isWakeLockSupported() {
  return 'wakeLock' in navigator;
}

// ── Always-on display: device preference ──────────────────────────────────
// The app-wide admin setting (app-settings.json → always_on_display) is the
// server DEFAULT every visitor inherits at boot. A user may override it for
// THIS device from the chat session-dropdown ⋯ switch; the device override wins
// over the server default and persists in localStorage.

/** Stored per-device override: true / false / null (no override → use server). */
export function getAlwaysOnPreference() {
  try {
    const v = localStorage.getItem(ALWAYS_ON_KEY);
    if (v === '1') return true;
    if (v === '0') return false;
  } catch (_) { /* storage unavailable — fall back to the server default */ }
  return null;
}

export function setAlwaysOnPreference(on) {
  try { localStorage.setItem(ALWAYS_ON_KEY, on ? '1' : '0'); } catch (_) {}
}

/**
 * Apply the always-on state at boot. `serverDefault` is the admin's app-wide
 * value from the public ui-config; a stored device override wins over it.
 * Returns the effective boolean (also readable via isAlwaysOnEnabled()).
 */
export async function applyAlwaysOnDisplay(serverDefault) {
  const pref = getAlwaysOnPreference();
  const on = pref === null ? serverDefault === true : pref;
  _alwaysOn = on;
  if (on) await enableWakeLock(OWNER_ALWAYS_ON);
  else await disableWakeLock(OWNER_ALWAYS_ON);
  return on;
}

/**
 * User flipped the chat switch: persist this device's choice and apply it
 * immediately. Returns the effective boolean.
 */
export async function setAlwaysOnDisplay(on) {
  _alwaysOn = on === true;
  setAlwaysOnPreference(_alwaysOn);
  if (_alwaysOn) await enableWakeLock(OWNER_ALWAYS_ON);
  else await disableWakeLock(OWNER_ALWAYS_ON);
  return _alwaysOn;
}

/**
 * Whether always-on display is currently in effect (device override or server
 * default). Reflects intent, so it reads true even where the wake-lock API is
 * unavailable and the lock cannot actually be held.
 */
export function isAlwaysOnEnabled() { return _alwaysOn; }

// ── Initialisation (call once at boot) ────────────────────────────────────

export function initWakeLock() {
  _startListening();
}

// ── Internal ──────────────────────────────────────────────────────────────

function _dispatch(active) {
  try {
    window.dispatchEvent(new CustomEvent('webagent-wakelock-change', {
      detail: { active: active && _lock !== null },
    }));
  } catch (_) {}
}

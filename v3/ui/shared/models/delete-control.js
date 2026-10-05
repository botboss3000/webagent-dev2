'use strict';

import { icon } from './icons.js';

/*
 * ============================================================================
 * SHARED-DELETE-CONTROL
 * ============================================================================
 * The two-click "trash → single pulse → hazard → pulsing trash → gone" delete affordance. This is the
 * SINGLE source of truth for that interaction and is shared by THREE call sites
 * that must stay in sync. Grep `SHARED-DELETE-CONTROL` to find them:
 *
 *   1) Chat header session menu  →  ui/chat/js/session-core.js + session-init.js
 *        - the trash button on every session row in the dropdown
 *        - the trash button in the chat header itself (#session-delete-header)
 *   2) Agents page agent card    →  ui/main-panel/agents/js/view.js
 *        - the trash button on each custom agent's card
 *   3) Agents page Config tab    →  ui/main-panel/agents/js/tab-config.js
 *        - the "Delete agent" button at the top of the Config tab (soft-delete
 *          to the recycling bin, sessions included)
 *
 * If you change the states, icons, timing, or behaviour, do it HERE so both
 * places pick it up. Do NOT re-implement the state machine inline in either
 * call site — call advanceDeleteBtn()/resetDeleteBtn() instead.
 *
 * Interaction (driven by the button's data-state attribute):
 *   "trash"   --click-->  "arming"   : one delete pulse (0.4s, clicks ignored)
 *   "arming"  --auto--->  "warning"  : swap to hazard icon, mark .warning
 *   "warning" --click-->  "deleting" : swap to a pulsing-aura trash icon,
 *                                      run onConfirm()
 *   "deleting" --click--> "undoing"  : if onUndo() is set, cancel the delete and
 *                                      swap to a green pulsing triangle while the
 *                                      session is restored, run onUndo()
 *   any reset (menu open/close, a sibling arming, a timeout, a failed delete)
 *             ------------ "trash"   : back to the trash icon
 *
 * Colour for the .warning and busy states lives in CSS next to each call site
 * (also tagged SHARED-DELETE-CONTROL). The arming state shows a single delete
 * pulse (`.delete-pulse-aura.arm` — one expanding ring, no spinner). The busy
 * state keeps the trash icon and wraps it in `.delete-pulse-aura` which pulses
 * a danger ring while the delete is in flight — so the button reads as
 * "deleting", not "loading". The undoing state reuses the same aura but green
 * (`.delete-pulse-aura.undo`) on the hazard triangle, signalling "restoring".
 * ============================================================================
 */

const TRASH = 'trash';
const WARN  = 'warning';
const BUSY  = 'deleting';
const UNDOING = 'undoing';
const ARMING = 'arming';

const ARM_DELAY_MS = 400; // one-pulse duration before the hazard appears

/**
 * Return a button to its resting trash state. Safe to call on any state.
 *
 * Options:
 *   size   icon size for the trash icon      (default '14px')
 *   title  resting tooltip                    (default 'Delete')
 *   label  resting accessible name (aria-label). Only set when provided, so
 *          call sites that manage their own aria-label keep it.
 */
export function resetDeleteBtn(btn, {
  size = '14px', title = 'Delete', label = null, iconName = 'trash-2',
} = {}) {
  if (!btn) return;
  clearTimeout(btn._armTimer);
  btn.dataset.state = TRASH;
  btn.classList.remove('warning', 'deleting', 'undoing');
  btn.title = title;
  if (label) btn.setAttribute('aria-label', label);
  btn.innerHTML = icon(iconName, { size });
}

/**
 * Advance a delete button by one click. Returns the NEW state string:
 *   'arming'   — first click: one delete pulse shown for 0.4s, clicks ignored
 *   'warning'  — after 0.4s: armed, showing the hazard icon
 *   'deleting' — second click: confirmed; the pulsing-aura trash is shown and
 *                onConfirm() has been scheduled (microtask, so the aura paints first)
 *   'trash'    — no-op (the button was already mid-delete)
 *
 * Options:
 *   size      icon size for trash/hazard      (default '14px')
 *   spinSize  icon size for the pulse/busy spans (default matches `size`)
 *   armTitle  tooltip while armed
 *   busyTitle tooltip while deleting
 *   armLabel  accessible name (aria-label) while armed — describe the
 *             destructive confirm action. Only set when provided.
 *   busyLabel accessible name (aria-label) while deleting. Only set when
 *             provided.
 *   busyText  visible text appended while deleting (optional).
 *   onArm(btn)     fired after the 1s arming delay, when the button shows the hazard
 *                  icon — use it to reset siblings / set an auto-revert timeout
 *   onConfirm(btn) fired (async) when the delete is confirmed
 *   onUndo(btn)    optional: when provided, a SECOND click while the delete is in
 *                  flight (the "deleting" pulse) cancels it and fires this — swap
 *                  to a green pulsing triangle and restore the session. Omit to
 *                  keep the classic "second clicks ignored while deleting" behaviour.
 */
export function advanceDeleteBtn(btn, {
  size = '14px',
  spinSize = null,
  iconName = 'trash-2',
  armTitle = 'Click again to delete',
  busyTitle = 'Deleting\u2026',
  undoTitle = 'Click to restore',
  armLabel = null,
  busyLabel = null,
  busyText = null,
  undoLabel = null,
  onArm = null,
  onConfirm = null,
  onUndo = null,
} = {}) {
  if (!btn) return TRASH;
  const spin = spinSize || size;
  const state = btn.dataset.state || TRASH;

  if (state === TRASH) {
    // --- ARMING phase: one delete pulse for ARM_DELAY_MS, clicks ignored ---
    // (trash icon + a single expanding ring via .delete-pulse-aura.arm —
    // replaces the old spinner so it reads as a "delete beat", not "loading")
    btn.dataset.state = ARMING;
    btn.classList.remove('warning', 'deleting', 'undoing');
    btn.title = 'Arming\u2026';
    btn.innerHTML =
      '<span class="delete-pulse-aura arm" style="display:inline-flex;width:' +
      spin + ';height:' + spin + ';">' +
      icon(iconName, { size: spin }) + '</span>';
    clearTimeout(btn._armTimer);
    btn._armTimer = setTimeout(() => {
      if (btn.dataset.state !== ARMING) return; // was reset in the meantime
      btn.dataset.state = WARN;
      btn.classList.add('warning');
      btn.classList.remove('deleting');
      btn.title = armTitle;
      if (armLabel) btn.setAttribute('aria-label', armLabel);
      btn.innerHTML = icon('alert-triangle', { size });
      if (onArm) onArm(btn);
    }, ARM_DELAY_MS);
    return ARMING;
  }

  if (state === ARMING) {
    return ARMING; // still arming — ignore clicks
  }

  if (state === WARN) {
    btn.dataset.state = BUSY;
    btn.classList.remove('warning');
    btn.classList.add('deleting');
    btn.title = busyTitle;
    if (busyLabel) btn.setAttribute('aria-label', busyLabel);
    // Busy state: keep the trash icon, wrap it in .delete-pulse-aura so a
    // danger aura pulses around it while the delete is in flight (CSS lives
    // next to .session-status-running, tagged SHARED-DELETE-CONTROL). The
    // delete affordance stays visible — it reads "deleting", not "loading".
    btn.innerHTML =
      '<span class="delete-pulse-aura" style="display:inline-flex;width:' +
      spin + ';height:' + spin + ';">' +
      icon(iconName, { size: spin }) + '</span>' +
      (busyText ? '<span class="delete-busy-label">' + busyText + '</span>' : '');
    if (onConfirm) Promise.resolve().then(() => onConfirm(btn));
    return BUSY;
  }

  if (state === BUSY) {
    // Undoable delete: a second click while the delete is in flight cancels it
    // and swaps to a green pulsing triangle while the session is restored.
    // Only when the call site opted in via onUndo; otherwise extra clicks are
    // ignored (the delete is already committed).
    if (onUndo) {
      btn.dataset.state = UNDOING;
      btn.classList.remove('deleting');
      btn.classList.add('undoing');
      btn.title = undoTitle;
      if (undoLabel) btn.setAttribute('aria-label', undoLabel);
      btn.innerHTML =
        '<span class="delete-pulse-aura undo" style="display:inline-flex;width:' +
        spin + ';height:' + spin + ';">' +
        icon('alert-triangle', { size: spin }) + '</span>';
      if (onUndo) Promise.resolve().then(() => onUndo(btn));
      return UNDOING;
    }
    return TRASH; // already deleting and no undo configured — ignore extra clicks
  }

  return TRASH; // already deleting, undoing, or arming — ignore extra clicks
}

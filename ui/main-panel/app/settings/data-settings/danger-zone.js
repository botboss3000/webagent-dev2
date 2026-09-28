'use strict';

/**
 * Danger Zone — App Configuration → Data Settings → Danger Zone card (bottom).
 *
 * Two destructive, admin-only actions on the running install:
 *   1. RESET DATA — tick any of five groups (database · vault · attachments ·
 *      genui · logs) and wipe them. The app can't erase its own open DB/vault
 *      in-process, so this writes a one-shot marker + self-restarts; the fresh
 *      boot clears the picked groups before anything opens them (server side:
 *      POST /admin/storage/reset → app/util/reset_boot.py). A backup lands under
 *      temp/. After the reboot, GET /admin/storage/reset/info reports the outcome,
 *      shown in the #ac-dz-last banner.
 *   2. DELETE INSTALLATION — self-destruct. Deletes the whole app folder from
 *      disk (stops + destroys this server). Armed only when the admin types the
 *      install folder's name (POST /admin/storage/delete-install).
 *
 * Sibling of deploy.js / app-access.js / social-auth.js; init'd by
 * data-settings.js. Markup: the #ac-danger-card block in data-settings.html.
 * Uses the shared hazard-confirm dialog + the auth-aware _fetch from utils.js.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { app } from '../../../../shared/js/state.js';
import { isAdmin } from '../../../../shared/js/left-login.js?v=253';
import { _refreshLucideIcons } from '../../../../shared/js/dom-utils.js';
import { _fetch, _qs, _esc, _fmtDate } from '../utils.js';

function uid() { return app.currentUserId || localStorage.getItem('auth_user_id') || ''; }

let _folderName = '';   // install folder name — the phrase that arms the delete
let _autoRestart = true;

// ── Hold-to-fire gesture (press + hold → a danger fill sweeps the button → fire) ──
// Mirrors the deploy card's hold-to-restart button. The deliberate hold IS the
// confirmation, so these destructive actions have no extra dialog. Per-button hold
// length (ms) — the delete is longer since it's irreversible. Set as the CSS
// `--dz-hold` var on the button at init so the fill animation matches this timer.
const _HOLD_MS = { reset: 1500, nuke: 2200 };
const _ARM_FRACTION = 0.25;   // show the "keep holding…" armed state after this much
let _hold = null;             // {btn, fireTimer, armTimer, restLabel, onFire} while live

function _holdLabel(btn) { return btn.querySelector('.ac-dz-hold-label'); }

// Stop the active hold. `revert=true` snaps the fill back + restores the label (an
// abort on early release); pass false when the action is firing (leave it as-is).
function _holdCancel(revert) {
  const h = _hold; _hold = null;
  if (!h) return;
  clearTimeout(h.fireTimer);
  clearTimeout(h.armTimer);
  window.removeEventListener('pointerup', _holdRelease, true);
  window.removeEventListener('pointercancel', _holdRelease, true);
  h.btn.classList.remove('holding');
  if (revert) {
    h.btn.classList.remove('warning');
    const lbl = _holdLabel(h.btn);
    if (lbl && h.restLabel != null) lbl.textContent = h.restLabel;
  }
}
function _holdRelease() { _holdCancel(true); }

function _beginHold(btn, ms, armLabel, onFire) {
  if (_hold || btn.disabled || !isAdmin()) return;
  const lbl = _holdLabel(btn);
  const restLabel = lbl ? lbl.textContent : '';
  const armTimer = setTimeout(() => {
    btn.classList.add('warning');
    if (lbl) lbl.textContent = armLabel;
  }, Math.round(ms * _ARM_FRACTION));
  const fireTimer = setTimeout(() => {
    _holdCancel(false);      // clear timers/listeners; keep the button state
    onFire();
  }, ms);
  _hold = { btn, fireTimer, armTimer, restLabel, onFire };
  // Kick the CSS fill on the next frame so the transition actually animates.
  requestAnimationFrame(() => { if (_hold && _hold.btn === btn) btn.classList.add('holding'); });
  window.addEventListener('pointerup', _holdRelease, true);
  window.addEventListener('pointercancel', _holdRelease, true);
}

// Resting labels — restored when a fired action fails and the button re-enables.
const _REST_LABEL = {
  'ac-dz-reset-btn': 'Hold to reset selected data',
  'ac-dz-nuke-btn': 'Hold to delete installation',
};
function _restoreHold(btn) {
  if (!btn) return;
  btn.classList.remove('warning', 'holding');
  const lbl = _holdLabel(btn);
  if (lbl && _REST_LABEL[btn.id]) lbl.textContent = _REST_LABEL[btn.id];
}

// ── Info fetch (folder name · provider · auto-restart · last-reset outcome) ──
async function _loadInfo() {
  const u = uid();
  if (!u) return;
  let data;
  try {
    const res = await _fetch(apiPath(`/admin/storage/reset/info?requesting_user_id=${encodeURIComponent(u)}`));
    if (!res.ok) return;
    data = await res.json();
  } catch { return; }
  if (!data || !data.ok) return;

  _folderName = data.folder_name || '';
  _autoRestart = data.auto_restart !== false;

  const phrase = _qs('ac-dz-nuke-phrase');
  if (phrase && _folderName) phrase.placeholder = `type "${_folderName}" to confirm`;

  // Host can't self-restart → the reset can't run; say so + disable the button.
  const norestart = _qs('ac-dz-norestart');
  if (norestart) {
    if (_autoRestart) {
      norestart.classList.add('ac-hidden');
    } else {
      norestart.textContent = 'This host can’t restart the server automatically, so a data reset can’t run from here. '
        + (data.restart_reason || '') + ' Start the server via run.py or the supervisor, then retry.';
      norestart.classList.remove('ac-hidden');
    }
  }

  _renderLast(data.last);
  _syncResetBtn();
}

// Render the outcome of the reset that last ran (after the reboot that did it).
function _renderLast(last) {
  const box = _qs('ac-dz-last');
  if (!box) return;
  if (!last || !Array.isArray(last.groups) || !last.groups.length) {
    box.classList.add('ac-hidden');
    return;
  }
  const parts = [];
  parts.push(`<strong>Last reset (${_esc(_fmtDate(last.at) || last.at)}):</strong> cleared ${last.groups.map(_esc).join(', ')}.`);
  if (last.postgres) {
    parts.push(last.postgres.ok
      ? ` Postgres ${_esc(last.postgres.detail || 'wiped')}.`
      : ` <span class="ac-fg-danger">Postgres wipe failed: ${_esc(last.postgres.detail || '')}.</span>`);
  }
  if (Array.isArray(last.failed) && last.failed.length) {
    parts.push(` <span class="ac-fg-danger">${last.failed.length} item(s) could not be removed.</span>`);
  }
  if (last.backup) parts.push(`<br><span class="ac-fg3">Backup: ${_esc(last.backup)}</span>`);
  if (last.note) parts.push(`<br><span class="ac-fg3">${_esc(last.note)}</span>`);
  parts.push(' <a href="#" id="ac-dz-last-dismiss" class="ac-fg3">Dismiss</a>');
  box.innerHTML = parts.join('');
  box.classList.remove('ac-hidden');

  const dismiss = _qs('ac-dz-last-dismiss');
  if (dismiss) dismiss.addEventListener('click', async (e) => {
    e.preventDefault();
    box.classList.add('ac-hidden');
    try {
      await _fetch(apiPath('/admin/storage/reset/dismiss'), {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ requesting_user_id: uid() }),
      });
    } catch { /* cosmetic — the banner is already hidden */ }
  });
}

// ── Reset targets ─────────────────────────────────────────────────────────
function _checks() { return Array.from(document.querySelectorAll('#ac-dz-list .ac-dz-check')); }
function _selected() { return _checks().filter(c => c.checked).map(c => c.value); }

function _syncResetBtn() {
  const btn = _qs('ac-dz-reset-btn');
  if (!btn) return;
  btn.disabled = _selected().length === 0 || !_autoRestart;
}

// Fire the reset (called after a full hold — no dialog, the hold was the confirm).
async function _runReset() {
  const groups = _selected();
  if (!groups.length) return;

  const status = _qs('ac-dz-reset-status');
  const btn = _qs('ac-dz-reset-btn');
  if (btn) { btn.disabled = true; btn.classList.remove('warning'); }
  if (status) { status.classList.add('ac-fg3'); status.classList.remove('ac-fg-success','ac-fg-danger','ac-fg-warning'); status.textContent = 'Scheduling reset & restarting…'; }

  try {
    const res = await _fetch(apiPath('/admin/storage/reset'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), groups }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      if (status) { status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg3','ac-fg-success','ac-fg-warning'); status.textContent = data.error || 'Reset failed.'; }
      if (btn) { btn.disabled = false; _restoreHold(btn); }
      return;
    }
    // The server is now restarting; it will wipe the data on the way up. Wait for
    // it to come back, then reload so the admin lands on the fresh instance.
    if (status) { status.classList.add('ac-fg-success'); status.classList.remove('ac-fg3','ac-fg-danger','ac-fg-warning'); status.textContent = 'Resetting… the app will reload when the server is back.'; }
    _waitForServerThenReload();
  } catch (e) {
    if (status) { status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg3','ac-fg-success','ac-fg-warning'); status.textContent = 'Reset request failed: ' + (e?.message || e); }
    if (btn) { btn.disabled = false; _restoreHold(btn); }
  }
}

// Poll /health until the restarted server answers, then hard-reload.
function _waitForServerThenReload() {
  let tries = 0;
  const tick = async () => {
    tries += 1;
    try {
      const res = await fetch(apiPath('/health'), { cache: 'no-store' });
      if (res.ok) { location.reload(); return; }
    } catch { /* still down */ }
    if (tries < 60) setTimeout(tick, 1500);
    else {
      const status = _qs('ac-dz-reset-status');
      if (status) { status.classList.add('ac-fg-warning'); status.classList.remove('ac-fg3','ac-fg-success','ac-fg-danger'); status.textContent = 'Reset sent, but the server hasn’t come back yet — reload the page in a moment.'; }
    }
  };
  setTimeout(tick, 3000);
}

// ── Delete installation (self-destruct) ─────────────────────────────────────
function _syncNukeBtn() {
  const btn = _qs('ac-dz-nuke-btn');
  const phrase = _qs('ac-dz-nuke-phrase');
  if (!btn || !phrase) return;
  btn.disabled = !_folderName || phrase.value.trim() !== _folderName;
}

// Fire the self-destruct (called after a full hold — the typed folder name armed
// the button, the hold is the final confirm; no dialog).
async function _runNuke() {
  const phrase = _qs('ac-dz-nuke-phrase');
  if (!phrase || phrase.value.trim() !== _folderName) return;

  const status = _qs('ac-dz-nuke-status');
  const btn = _qs('ac-dz-nuke-btn');
  if (btn) { btn.disabled = true; btn.classList.remove('warning'); }
  if (status) { status.classList.add('ac-fg3'); status.classList.remove('ac-fg-success','ac-fg-danger','ac-fg-warning'); status.textContent = 'Deleting installation…'; }

  try {
    const res = await _fetch(apiPath('/admin/storage/delete-install'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ requesting_user_id: uid(), confirm_phrase: phrase.value.trim() }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok || !data.ok) {
      if (status) { status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg3','ac-fg-success','ac-fg-warning'); status.textContent = data.error || 'Delete failed.'; }
      if (btn) { btn.disabled = false; _restoreHold(btn); }
      return;
    }
    if (status) { status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg3','ac-fg-success','ac-fg-warning'); status.textContent = 'Installation is being deleted and the server is shutting down. This app will stop responding.'; }
  } catch (e) {
    // A dropped connection here can simply mean the server already exited mid-delete.
    if (status) { status.classList.add('ac-fg-warning'); status.classList.remove('ac-fg3','ac-fg-success','ac-fg-danger'); status.textContent = 'Delete triggered — the server may have already shut down.'; }
  }
}

// ── Init ────────────────────────────────────────────────────────────────────
export function initDangerZone() {
  const card = _qs('ac-danger-card');
  if (!card || card.dataset.dzWired) return;
  card.dataset.dzWired = '1';

  // Reset target checkboxes → enable/disable the reset button.
  _checks().forEach(c => c.addEventListener('change', _syncResetBtn));
  // Reset: press-and-HOLD to fire (the hold is the confirm). The fill duration
  // (--dz-hold) is set from the same constant that drives the JS fire timer, so
  // the animation and the action can never drift.
  const resetBtn = _qs('ac-dz-reset-btn');
  if (resetBtn) {
    resetBtn.style.setProperty('--dz-hold', _HOLD_MS.reset + 'ms');
    resetBtn.addEventListener('pointerdown', (e) => {
      if (e.button && e.button !== 0) return;   // primary / touch only
      e.preventDefault();
      _beginHold(resetBtn, _HOLD_MS.reset, 'Keep holding to reset…', _runReset);
    });
    // A click without a completed hold does nothing (Enter/Space fall through here
    // too) — swallow it so the browser's default button activation can't fire it.
    resetBtn.addEventListener('click', (e) => e.preventDefault());
  }

  // Delete-install: typing the folder name ARMS the button; then press-and-HOLD
  // (longer) to fire. Two independent guards before anything is deleted.
  const phrase = _qs('ac-dz-nuke-phrase');
  phrase?.addEventListener('input', _syncNukeBtn);
  const nukeBtn = _qs('ac-dz-nuke-btn');
  if (nukeBtn) {
    nukeBtn.style.setProperty('--dz-hold', _HOLD_MS.nuke + 'ms');
    nukeBtn.addEventListener('pointerdown', (e) => {
      if (e.button && e.button !== 0) return;   // primary / touch only
      e.preventDefault();
      _beginHold(nukeBtn, _HOLD_MS.nuke, 'Keep holding to delete…', _runNuke);
    });
    nukeBtn.addEventListener('click', (e) => e.preventDefault());
  }

  _refreshLucideIcons(card);
  _loadInfo();
  // Re-fetch on each section-show so the last-reset banner appears after a reboot.
  window.__refreshDangerZone = _loadInfo;
}

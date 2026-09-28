'use strict';

/**
 * Memory Cache (RAM) row — Data Settings → DATABASE group.
 *
 * Drives the app-wide On/Off + memory-budget controls for the in-browser media
 * cache (ui/shared/js/media-cache.js). Both are plain app settings delivered to
 * every visitor through /api/v1/auth/ui-config and applied at boot by
 * ui/shared/js/appearance.js (keys: media_cache_enabled / media_cache_budget_mb).
 *
 * - Loads current values from GET /admin/settings/app.
 * - Saves each change with the merge-friendly POST /admin/settings/app (the
 *   server keeps every other key), same as the App-Access / social-auth rows.
 * - Previews live in THIS browser via window.WA_APPEARANCE.setOverrides, which
 *   re-runs appearance.js apply() → reconfigures window.WA_MEDIA_CACHE instantly.
 *
 * Row markup + ids live in data-settings.html (#ac-row-mediacache). Wired from
 * data-settings.js init(). The badge shows the live enabled/budget summary.
 */

import { _qs } from '../utils.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { apiPath } from '../../../../shared/js/config.js';

function _uid() {
  try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; }
}

function _badge(text, tone) {
  const b = _qs('ac-mediacache-badge');
  if (!b) return;
  b.textContent = text;
  b.classList.remove('ac-fg-success','ac-fg3'); b.classList.add(tone === 'off' ? 'ac-fg3' : 'ac-fg-success');
}

// Human-readable byte size for the live stats read-out.
function _fmtBytes(n) {
  if (!n) return '0 MB';
  const mb = n / (1024 * 1024);
  return mb >= 1024 ? (mb / 1024).toFixed(2) + ' GB' : mb.toFixed(1) + ' MB';
}

function _renderStats() {
  const el = _qs('ac-mediacache-stats');
  if (!el) return;
  let s = null;
  try { s = window.WA_MEDIA_CACHE && window.WA_MEDIA_CACHE.stats(); } catch { s = null; }
  if (!s) { el.textContent = ''; return; }
  el.innerHTML = s.enabled
    ? `In use now: <b>${s.entries}</b> item(s), <b>${_fmtBytes(s.bytes)}</b> of <b>${_fmtBytes(s.budgetBytes)}</b>.`
    : 'Cache is off — media is fetched from its store every time.';
}

function _syncBadge() {
  const enabled = (_qs('ac-mediacache-enabled')?.value || 'on') === 'on';
  const budget = _qs('ac-mediacache-budget')?.value || '128';
  _badge(enabled ? `On · ${budget} MB` : 'Off', enabled ? 'on' : 'off');
}

// Push the current control values into the live appearance layer so the running
// browser reconfigures its cache immediately (no reload), mirroring how the
// Appearance panel previews palette/border changes.
function _preview() {
  const patch = {
    media_cache_enabled: (_qs('ac-mediacache-enabled')?.value === 'off') ? 'off' : 'on',
    media_cache_budget_mb: String(parseInt(_qs('ac-mediacache-budget')?.value, 10) || 128),
  };
  try {
    if (window.WA_APPEARANCE && typeof window.WA_APPEARANCE.setOverrides === 'function') {
      window.WA_APPEARANCE.setOverrides(patch);
    }
  } catch { /* preview is best-effort */ }
  _syncBadge();
  _renderStats();
  return patch;
}

async function _save() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const patch = _preview();
  try {
    const res = await fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
  } catch (e) {
    console.warn('media-cache-settings: save failed', e);
  }
}

export function initMediaCacheSettings() {
  const enabledSel = _qs('ac-mediacache-enabled');
  const budgetInp = _qs('ac-mediacache-budget');
  if (!enabledSel || !budgetInp) return;

  enabledSel.addEventListener('change', _save);
  // Save the budget when the user finishes editing (blur / Enter), not on every
  // keystroke, so a partly-typed number never persists.
  budgetInp.addEventListener('change', _save);

  // Load current app-wide values. Non-admins can still see the row but the GET is
  // admin-gated; on failure we just fall back to the on/128 defaults already in
  // the markup, and the live cache keeps whatever appearance.js configured.
  fetch(apiPath('/admin/settings/app?requesting_user_id=' + encodeURIComponent(_uid())))
    .then(r => (r.ok ? r.json() : null))
    .then(cfg => {
      if (cfg) {
        enabledSel.value = (cfg.media_cache_enabled === 'off') ? 'off' : 'on';
        budgetInp.value = String(parseInt(cfg.media_cache_budget_mb, 10) || 128);
      }
      _syncBadge();
      _renderStats();
    })
    .catch(() => { _syncBadge(); _renderStats(); });
}

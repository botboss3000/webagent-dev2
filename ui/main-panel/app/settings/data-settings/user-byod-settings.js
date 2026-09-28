'use strict';

/**
 * User BYOD row — Data Settings → DATABASE group → User Data.
 *
 * App-wide master switch for per-user bring-your-own-database routing
 * (user_byod in app-settings.json). OFF (default) = single shared database,
 * unchanged for self-hosters and existing installs. ON = each signed-in user's
 * interaction data (sessions, chats, memories, agents, their own LLM keys) routes
 * to a Postgres database THEY connect from their account page
 * (ui/shared/js/tenant-db.js), while the central database keeps only accounts, the
 * shared agent catalog and billing; backend routing lives in app/db/router.py +
 * app/db/tenant.py.
 *
 * Mirrors the media-cache-settings.js pattern: it loads current state from GET
 * /admin/settings/app and autosaves each flip with the merge-friendly POST
 * /admin/settings/app (the server keeps every other key). Row markup + ids live
 * in data-settings.html (#ac-row-user-byod). The row is a plain on/off toggle
 * (like the App Settings rows) — it has no expandable body, so no row-expand
 * wiring is needed. Admin-only save (mirrors the App-Access / media-cache rows).
 */

import { _qs, _fetch } from '../utils.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { apiPath } from '../../../../shared/js/config.js';
import { _markSaving, _flashSaveCheck } from '../../../../shared/js/dom-utils.js';

// Keep the inline label + header badge in sync with the toggle.
function _reflect(chk) {
  const lbl = _qs('ac-user-byod-label');
  const badge = _qs('ac-user-byod-badge');
  const on = !!chk.checked;
  if (lbl) lbl.textContent = on ? 'On — each user uses their own database' : 'Off — single shared database';
  if (badge) badge.textContent = on ? 'On' : 'Off';
}

export function initUserByod() {
  const chk = _qs('ac-user-byod');
  if (!chk) return;
  const wrap = chk.closest('.ac-ability-toggle-wrap');

  chk.addEventListener('change', async () => {
    if (!isAdmin()) { showRestrictedModal(); chk.checked = !chk.checked; return; }
    _markSaving(wrap);
    try {
      const res = await _fetch(apiPath('/admin/settings/app'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ user_byod: chk.checked }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      _reflect(chk);
      _flashSaveCheck(wrap, true);
    } catch (e) {
      console.warn('user-byod-settings: save user_byod failed', e);
      chk.checked = !chk.checked;  // revert on failure
      _reflect(chk);
      _flashSaveCheck(wrap, false, e.message);
    }
  });

  // Load current app-wide value. The GET is admin-gated; on failure we fall back
  // to the OFF default already reflected in the markup.
  _fetch(apiPath('/admin/settings/app'))
    .then((r) => (r.ok ? r.json() : null))
    .then((data) => {
      if (!data) return;
      chk.checked = data.user_byod === true;
      _reflect(chk);
    })
    .catch(() => { /* keep the OFF default */ });
}

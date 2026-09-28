'use strict';

/**
 * Data Settings tab — data-oriented app configuration. Groups:
 *   • APP ACCESS (access mode + social sign-in)
 *   • DATABASE — Storage Routing table (Browser / Server / Postgres per function)
 *   • REMOTE ACCESS (Wi-Fi / tunnel card)
 *
 * More data-related settings groups can be added over time.
 *
 * Mirrors the other App Config section modules (Data Management, App Settings): a
 * section partial (data-settings.html) fills the #ac-section-slot-data-settings
 * slot, and this module wires it. The Deployment card's own /admin/deploy/*
 * fetching + NDJSON deploy stream live in the sibling deploy.js (initDeploy),
 * which sets window.__refreshDeploy (called on section-show in nav.js).
 *
 * The DATABASE group's Storage Routing table is driven by
 * storage-routing.js — it loads/saves the routing config from
 * GET/POST /admin/storage/routing.
 */

import { _qs } from '../utils.js';
import { initDeploy } from './deploy.js';
// initDns moved with the domain card to the Instances page's New Deployment tab
// (ui/main-panel/app/new-deployment/new-deployment.js). Not used here now.
import { initAppAccess } from './app-access.js';
import { initSocialAuth } from './social-auth.js';
import { initDangerZone } from './danger-zone.js';
import { initMediaCacheSettings } from './media-cache-settings.js';
import { initUserByod } from './user-byod-settings.js';
import { initStorageRouting } from './storage-routing.js';

// Wire one expandable row: clicking the head expands/collapses it, except when
// the click lands on a control inside the head. Same behaviour as App Settings'
// _wireBootRow (the Deployment rows were wired there before this tab existed).
// -- Page-assistant chat pill --------------------------------------------------
// The floating "advanced chat pill" at the bottom of Data Settings (a composer
// twin of App Settings' pill). The shared page-assistant engine (../page-
// assistant.js) runs in COMPOSER mode here too, owning this page's static
// #ac-ds-pa-* pill's send / Enter / voice / file-uploads. The prompt it hands
// WebAgent changes with the section the mouse is over (the data-pa-area groups
// in data-settings.html: deployment · app_access · database · remote_access); on send it opens a
// floating WebAgent (the manager) chat via spawnWebagentPageChat. The whole
// assistant is steered toward helping the admin get people SIGNED IN to this
// instance. Text catalog (placeholders + idea hints + prompts) lives in
// app/defaults/app-prompts.json -> page_assistants.data_settings (edit + reload).
function _wireBootRow(rowId) {
  const row = _qs(rowId);
  if (!row) return;
  const head = row.querySelector(':scope > .ac-ability-row');
  head?.addEventListener('click', (e) => {
    if (e.target.closest('select, input, button, a, label')) return;
    row.classList.toggle('expanded');
  });
}

export function init() {
  // The DEPLOYMENT card was REMOVED from this page. Its local-deployments list —
  // this app's hold-to-restart + port editor, plus any sibling checkouts on this
  // machine — MOVED to the Admin Tools → Instances page, into the "This device"
  // tile's Overview as a "Server" section (ui/main-panel/app/instances.js).
  // The "+ New deployment" flow had already moved to that page's "New instance"
  // tile. So deploy.js's repo/target/instances elements are all absent here now.
  //
  // initDeploy() is STILL called — but ONLY for the Export / Import setup-bundle
  // rows, which live in the DATABASE card's "Data Migration" row: initDeploy's
  // _initBootRows wires their Generate/Copy/Preview/Accept actions by id. Every
  // other element it looks for (cloud form, deploy target, #ac-deploy-instances)
  // is absent on this page, so its `?.` guards make those a no-op (and _loadInstances
  // returns early with no #ac-deploy-instances host). The boot rows' head-expand is
  // handled by _wireDatabaseRows() below (it walks every `.ac-row` in
  // #ac-database-card, incl. these nested ones) — NOT wired here, or the two
  // handlers would cancel each other out.
  initDeploy();
  // App Access card — now only the Social Sign-In providers' expandable header:
  // the access-mode selection moved to the Instances → Users tab, which owns
  // /admin/settings/app + /admin/users/* itself. See ./app-access.js.
  initAppAccess();
  // Social sign-in providers, nested in the App Access card (moved out of the
  // Users page). Owns its own load + save; sets window.__refreshSocialAuth
  // (called on section-show in nav.js). See ./social-auth.js.
  initSocialAuth();
  // Database card — the Storage Routing table (replaces the old Application Data /
  // Secrets / Encryption / Hybrid rows). Renders radio-selectable Browser / Server /
  // Postgres columns for each data function. Owns GET/POST /admin/storage/routing.
  initStorageRouting();
  // Remote Access card — moved here from App Settings, sits directly below the
  // Database card. Its own `.ac-category-group` (#ac-ra-card), so it is NOT
  // covered by _wireDatabaseRows (scoped to #ac-database-card); wire its two
  // expandable rows here. The bodies' control ids are owned by remote-access.js,
  // which loads + fetches on App-Config open via settings-view.js — unchanged.
  _wireBootRow('ac-ra-row-sn');
  _wireBootRow('ac-ra-row-net');
  // Memory Cache (RAM) row — On/Off + memory budget for the in-browser media
  // cache (media-cache.js). Owns its own /admin/settings/app load + auto-save and
  // live preview via WA_APPEARANCE. Row expand/collapse comes from _wireDatabaseRows
  // above (the row lives in the same DATABASE ability-list). See ./media-cache-settings.js.
  initMediaCacheSettings();
  // User BYOD row — the per-user bring-your-own-database master switch, at the
  // bottom of the DATABASE card (moved here from App Settings → Advanced). Owns
  // its own /admin/settings/app load + autosave. A plain on/off toggle (no
  // expandable body), so no row-expand wiring is needed — the old
  // _wireDatabaseRows() no longer exists. See ./user-byod-settings.js.
  initUserByod();
  // Danger Zone card (very bottom) — reset selected data groups (self-restart +
  // boot-time wipe) or delete the whole install. Owns its own /admin/storage/reset*
  // fetch + confirm dialogs; sets window.__refreshDangerZone (called on section-show
  // in nav.js) so the last-reset banner appears after the reboot. See ./danger-zone.js.
  initDangerZone();
}

export function load() {
  // Storage Routing table loads its own config in init() via initStorageRouting().
  // No other deferred loading needed for the Database card.
  // initDeploy() (in init above) sets window.__refreshDeploy, which nav.js calls
  // on section-show for a fresh deploy catalog — now only relevant to the
  // Export/Import setup-bundle bars, since the local-deployments list moved to
  // the Instances page's "This device" tile.
}

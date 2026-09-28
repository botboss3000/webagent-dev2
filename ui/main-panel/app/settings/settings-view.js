'use strict';

/**
 * Settings — drop-in admin view entry (embedded in the Instances page).
 *
 * The Settings UI (formerly "App Config") is only available as the Settings
 * tab inside the Instances page's "This device" tile. The view is booted via
 * window.initSettings() / window.startSettings() called from instances.js's
 * _onSettingsTabRendered().
 *
 * tabs.js `_ensureAdminInit` still calls initSettings() at boot to attach
 * listeners. startView/stopView are exposed for the module interface but the
 * standalone page descriptor (page.json) has been removed.
 *
 * Settings bundles several sections whose data/pollers live in sibling modules
 * (Data Management, Remote Access, Tunnel Link, Storage, Billing). They are
 * started together because they share the one view.
 */

import { initSettings, startSettings, stopSettings, openPage, closePage } from './settings.js?v=5';
import { startDataManagement } from '../../../shared/js/data-management.js';
import { startRemoteAccess } from '../../../shared/js/remote-access.js';
import { startTunnelLink } from '../../../shared/js/tunnel-link.js';
import { startStorageUi } from '../../../shared/js/storage.js';
import { startBilling, stopBilling } from '../../../shared/js/billing.js';

// Expose settings functions globally so other views (e.g. the Instances
// Settings tab) can use them.
window.initSettings = initSettings;
window.startSettings = startSettings;
window.stopSettings = stopSettings;
// Instances uses this handoff after the App page catalog resolves a settings
// sub-page requested by a direct URL.
window.openSettingsPage = openPage;
// The already-active Settings tab uses the same path as the page chrome's
// “All settings” control to return from a section to the landing list.
window.closeSettingsPage = closePage;

export function startView() {
  try { startSettings(); } catch (_) {}
  try { startDataManagement(); } catch (_) {}
  try { startRemoteAccess(); } catch (_) {}
  try { startTunnelLink(); } catch (_) {}
  try { startStorageUi(); } catch (_) {}
  try { startBilling(); } catch (_) {}
}

export function stopView() {
  try { stopSettings(); } catch (_) {}
  try { stopBilling(); } catch (_) {}
}

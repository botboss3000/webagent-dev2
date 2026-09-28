'use strict';

// Drop-in entry for the Data Management (database viewer) admin view. The parked
// db-viewer markup is relocated into this view's hosts once at boot by
// relocateAdminToolsContainers() (called from ui/shared/js/main.js); this
// adapter just drives the per-show lifecycle (load + auto-refresh polling).
import { startDbViewer } from '../../shared/js/index.js';
import { startAutoRefresh, stopAutoRefresh } from '../../shared/js/pagination.js';

export function startView() {
  try { startDbViewer(); } catch (_) {}
  try { startAutoRefresh(); } catch (_) {}
}

export function stopView() {
  try { stopAutoRefresh(); } catch (_) {}
}

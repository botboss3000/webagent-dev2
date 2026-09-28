'use strict';

// Drop-in entry for the Diagnostics admin view. The Admin Tools shell imports
// this module and calls startView / stopView when the view is shown / hidden
// (see ui/shared/js/files.js → _startDynAdminView). The actual logic lives in
// the shared diagnostics module; this is just the lifecycle adapter so the view
// is a self-contained folder with no hardcoded hook in files.js.
import { startDiagnostics, stopDiagnostics, renderDiagnosticsSidebar } from '../../shared/js/diagnostics.js';

export function startView() {
  try { startDiagnostics(); } catch (_) {}
  try { renderDiagnosticsSidebar(); } catch (_) {}
}

export function stopView() {
  try { stopDiagnostics(); } catch (_) {}
}

'use strict';

// Standalone entry for the Optimizer Runs admin page.
// Logic lives in the shared optimizer-stats module; this adapter wires its
// lifecycle so the view is a self-contained drop-in folder.
import { init, load } from '../instances/settings/optimizer/optimizer-stats.js';

let _initialized = false;

export function startView() {
  if (!_initialized) { try { init(); } catch (_) {} _initialized = true; }
  try { load(); } catch (_) {}
}

export function stopView() {}

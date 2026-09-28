'use strict';

// Drop-in entry for the Interactions admin view (the agent pipeline visualizer).
// Logic lives in the agent-loop module under the Agents page; this adapter wires
// its lifecycle so the view is a self-contained drop-in folder.
import { startLoop, stopLoop, renderInteractionsSidebar } from '../../main-panel/agents/agent-loop/js/loop.js';

export function startView() {
  try { startLoop(); } catch (_) {}
  try { renderInteractionsSidebar(); } catch (_) {}
}

export function stopView() {
  try { stopLoop(); } catch (_) {}
}

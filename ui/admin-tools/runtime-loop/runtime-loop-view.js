'use strict';

// Drop-in entry for the Runtime Loop admin view (the agent decision-graph
// visualizer). Logic lives in the agent-loop module under the Agents page; this
// adapter wires its lifecycle so the view is a self-contained drop-in folder.
import { startLoopVisual, stopLoopVisual, renderRuntimeLoopSidebar } from '../../main-panel/agents/agent-loop/js/loop-logic.js';

export function startView() {
  try { startLoopVisual(); } catch (_) {}
  try { renderRuntimeLoopSidebar(); } catch (_) {}
}

export function stopView() {
  try { stopLoopVisual(); } catch (_) {}
}

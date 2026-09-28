'use strict';

/**
 * Agents Management — orchestrator module.
 *
 * Wires together all sub-modules (grid, card, tabs, etc.) following the same
 * pattern as ui/main-panel/app/settings/.
 * Called by tabs.js (startAgents, stopAgents).
 */

export { startAgents, stopAgents } from './app.js?v=784';

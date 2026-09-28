'use strict';

/**
 * Agent Management panel.
 *
 * This file is now a thin re-export index that delegates to the modular
 * structure in ui/agents/js/ (following the same pattern as ui/main-panel/app/settings/).
 *
 * The modules are:
 *   index.js    — Re-exports the public API
 *   app.js      — initAgents, startAgents, stopAgents
 *   state.js    — Shared state and constants
 *   data.js     — Data loading (fetch agents, profile, settings)
 *   utils.js    — Shared utilities (esc, btn, autosave helpers)
 *   view.js     — Grid/carousel/card rendering (combined to avoid circular deps)
 *   icon-picker.js  — Agent icon picker popover
 *   mock-agent.js   — Create-in-place agent card
 *   tab-config.js   — Config tab
 *   tab-prompts.js  — Prompts tab + skills
 *   tab-abilities.js    — Abilities/connections tab
 *   tab-members.js      — Members tab
 *   tab-agent-loop.js   — Agent Loop / test tab
 */

export { startAgents, stopAgents } from './index.js?v=784';

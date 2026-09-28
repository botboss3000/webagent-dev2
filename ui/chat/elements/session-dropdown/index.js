'use strict';

// Chat element: session_dropdown — the session selector chip in the chat
// header (trigger row + dropdown menu).
//
// All session-dropdown logic is centralized in this directory:
//   • index.js      — element contract (this file)
//   • controller.js — menu lifecycle: open/close/position/animation, loading
//                     skeleton, trigger wiring, row actions, search/manage
//                     footer, reorder + long-press rename (mountSessionDropdown)
//   • list.js       — session data + rendering: cache, validated fetch with
//                     bounded retry, row rendering, manage footer, trigger
//                     label, related chips, group tree
//
// init() mounts the controller (idempotent per element). The static
// chat-side-panel.html markup is wired at boot by session-init.js calling
// mountSessionDropdown() directly — the loader reuses existing DOM and does
// NOT call init() for it, so both paths converge on the same controller.

import { mountReactSessionDropdown, unmountReactSessionDropdown } from './react-island.js';

export function html(cfg = {}) {
  return `<div class="session-dropdown" data-element-name="session_dropdown">
    <div class="session-dropdown-trigger" role="button" tabindex="0" title="Show sessions" aria-label="Show sessions">
      <span class="session-row-agent-icon" id="session-dropdown-icon"><i data-lucide="bot" style="width:14px;height:14px;"></i></span>
      <span class="session-row-title" id="session-dropdown-label">—</span>
      <span class="session-row-status" id="session-dropdown-status"></span>
      <button class="session-row-kebab" id="session-dropdown-kebab" title="More…" data-id=""><i data-lucide="more-vertical" style="width:14px;height:14px;"></i></button>
      <button class="session-row-delete" id="session-dropdown-delete" title="Delete session" data-id=""><i data-lucide="trash-2" style="width:14px;height:14px;"></i></button>
    </div>
    <div class="session-dropdown-menu" id="session-dropdown-menu" hidden></div>
  </div>`;
}

export function init(el, cfg = {}) {
  // session-init.js may already have mounted the controller at boot (static
  // markup path); mountSessionDropdown is idempotent per root element.
  mountReactSessionDropdown(el, cfg);
}

export function destroy(el) {
  unmountReactSessionDropdown(el);
}

export function style() { return ''; }

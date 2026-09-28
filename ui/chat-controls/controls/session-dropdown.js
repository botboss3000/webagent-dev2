'use strict';

// Chat control: session_dropdown — session selector with full session-row trigger + menu

export function html(cfg = {}) {
  return `<div class="session-dropdown" data-controls-name="session_dropdown">
    <div class="session-dropdown-trigger" role="button" tabindex="0" title="Show sessions" aria-label="Show sessions">
      <span class="session-row-agent-icon"><i data-lucide="bot" style="width:14px;height:14px;"></i></span>
      <span class="session-row-title">—</span>
      <span class="session-row-status"></span>
      <button class="session-row-kebab" title="More…" data-id=""><i data-lucide="more-vertical" style="width:14px;height:14px;"></i></button>
      <button class="session-row-delete" title="Delete session" data-id=""><i data-lucide="trash-2" style="width:14px;height:14px;"></i></button>
    </div>
    <div class="session-dropdown-menu" hidden></div>
  </div>`;
}

export function init(el, cfg = {}) {
  // session-init.js already wires up .session-dropdown elements by class/id.
  // The IDs are generated dynamically, so we rely on class-based selectors.
}

export function destroy(el) {}

export function style() { return ''; }

'use strict';

// Chat element: mode — cycles the active agent's configured execution modes.

export function html(cfg = {}) {
  return `<button type="button" class="chat-mode-btn" title="Cycle execution mode" data-element-name="mode">Ask</button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-mode-btn';
}

export function destroy(el) {}

export function style() { return ''; }

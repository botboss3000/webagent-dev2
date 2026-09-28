'use strict';

// Chat element: status — connection/agent status indicator (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-status" data-element-name="status">idle</span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }

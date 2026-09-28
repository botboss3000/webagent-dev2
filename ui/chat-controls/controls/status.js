'use strict';

// Chat control: status — connection/agent status indicator (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-header-status" data-controls-name="status"></span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }
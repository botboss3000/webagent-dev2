'use strict';

// Chat control: dot — small status indicator dot (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-header-dot" data-controls-name="dot"></span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }
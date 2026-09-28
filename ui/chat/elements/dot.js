'use strict';

// Chat element: dot — small status indicator dot (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-dot" data-element-name="dot"></span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }

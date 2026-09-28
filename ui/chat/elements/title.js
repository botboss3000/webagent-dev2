'use strict';

// Chat element: title — chat title text (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-title" data-element-name="title">Chat</span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }

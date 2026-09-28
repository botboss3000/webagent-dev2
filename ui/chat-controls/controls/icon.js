'use strict';

// Chat control: icon — agent avatar/icon (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-header-icon" data-controls-name="icon"></span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }
'use strict';

// Chat control: title — chat title text (widget/embed use)

export function html(cfg = {}) {
  return `<span class="chat-header-title" data-controls-name="title"></span>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }
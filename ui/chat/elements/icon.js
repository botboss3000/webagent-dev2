'use strict';

// Chat element: icon — agent avatar/icon (widget/embed use)

export function html(cfg = {}) {
  const size = cfg.element_size || '24px';
  return `<i data-lucide="bot" class="chat-icon" style="width:${size};height:${size};" data-element-name="icon"></i>`;
}

export function init(el, cfg = {}) {}

export function destroy(el) {}

export function style() { return ''; }

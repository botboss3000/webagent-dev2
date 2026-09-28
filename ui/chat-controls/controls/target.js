'use strict';

// Chat control: target — choose which device runs the next message

export function html(cfg = {}) {
  const size = cfg.element_size || '16px';
  return `<button type="button" class="chat-target-btn" title="Remote Control — choose which device runs your next message" data-controls-name="target">
    <i data-lucide="monitor" style="width:${size};height:${size};"></i>
    <span class="chat-target-label"></span>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-target-btn';
}

export function destroy(el) {}

export function style() { return ''; }

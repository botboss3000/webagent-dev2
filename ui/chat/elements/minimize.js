'use strict';

// Chat element: minimize — minimize the chat widget

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="chat-header-minimize" title="Minimize" data-element-name="minimize">
    <i data-lucide="minus" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:minimize'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

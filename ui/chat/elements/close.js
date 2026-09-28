'use strict';

// Chat element: close — close the chat panel (widget/embed use)

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="chat-header-close" title="Close" data-element-name="close">
    <i data-lucide="x" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:close'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

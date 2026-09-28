'use strict';

// Chat element: refresh — refresh the chat transcript

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="chat-refresh-btn" title="Refresh the chat transcript" data-element-name="refresh">
    <i data-lucide="refresh-cw" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:refresh-transcript'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

'use strict';

// Chat element: new_session — create a new chat session

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="session-new-header-btn" title="New session" data-element-name="new_session">
    <i data-lucide="plus" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:new-session'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

'use strict';

// Chat control: delete_session — delete the current session

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="header-delete-btn" title="Delete session" data-state="trash" data-controls-name="delete_session">
    <i data-lucide="trash-2" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:delete-session'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

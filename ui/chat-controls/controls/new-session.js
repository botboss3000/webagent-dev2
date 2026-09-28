'use strict';

// Chat control: new_session — create a new chat session
// Can be placed in header or footer via chat_ui.json config.

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="session-new-header-btn" title="New session" data-controls-name="new_session">
    <i data-lucide="plus" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    // Dispatch a custom event that session-init.js listens for
    document.dispatchEvent(new CustomEvent('chat-control:new-session'));
  });
}

export function destroy(el) {
  // No special cleanup needed
}

export function style() {
  return '';
}

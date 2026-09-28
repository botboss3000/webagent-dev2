'use strict';

// Chat control: session_prev — navigate to previous pinned session

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="session-nav-btn" title="Previous pinned session" data-controls-name="session_prev">
    <i data-lucide="arrow-big-left" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:session-prev'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

'use strict';

// Chat control: session_next — navigate to next pinned session

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="session-nav-btn" title="Next pinned session" data-controls-name="session_next">
    <i data-lucide="arrow-big-right" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:session-next'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

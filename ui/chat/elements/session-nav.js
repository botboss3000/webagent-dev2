'use strict';

// Chat element: session_prev + session_next — navigate pinned sessions
// Both prev and next use this single module (closely related pair).

export function html(cfg = {}) {
  const prevSize = cfg.prev?.element_size || cfg.element_size || '22px';
  const nextSize = cfg.next?.element_size || cfg.element_size || '22px';
  return `<button type="button" class="session-nav-btn" title="Previous session" data-element-name="session_prev">
    <i data-lucide="arrow-big-left" style="width:${prevSize};height:${prevSize};"></i>
  </button>
  <button type="button" class="session-nav-btn" title="Next session" data-element-name="session_next">
    <i data-lucide="arrow-big-right" style="width:${nextSize};height:${nextSize};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  const prev = el.querySelector('[data-element-name="session_prev"]');
  const next = el.querySelector('[data-element-name="session_next"]');
  if (prev) prev.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:session-prev'));
  });
  if (next) next.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:session-next'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

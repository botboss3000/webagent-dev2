'use strict';

// Chat control: changes — toggle the session file changes panel

export function html(cfg = {}) {
  const size = cfg.element_size || '18px';
  return `<button type="button" class="chat-changes-toggle" title="Show files changed in this chat session" data-controls-name="changes">
    <i data-lucide="files" style="width:${size};height:${size};"></i>
    <span class="chat-changes-count" style="display:none;">0</span>
  </button>`;
}

export function init(el, cfg = {}) {
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    document.dispatchEvent(new CustomEvent('chat-control:toggle-changes'));
  });
}

export function destroy(el) {}

export function style() { return ''; }

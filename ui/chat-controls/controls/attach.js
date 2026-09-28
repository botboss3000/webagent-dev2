'use strict';

// Chat control: attach — attach files to the current message

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="chat-pill-attach" title="Attach files (or paste / drop onto the chat pill)" data-controls-name="attach">
    <i data-lucide="plus" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-attach-btn';
}

export function destroy(el) {}

export function style() { return ''; }

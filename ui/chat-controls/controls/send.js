'use strict';

// Chat control: send — send the current message

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="chat-pill-send" title="Send message" disabled data-controls-name="send">
    <i data-lucide="send" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-send';
}

export function destroy(el) {}

export function style() { return ''; }

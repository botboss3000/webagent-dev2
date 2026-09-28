'use strict';

// Chat element: stop — stop the current generation

export function html(cfg = {}) {
  return `<button type="button" class="chat-stop-btn" title="Stop generation" data-element-name="stop">
    <i data-lucide="square" class="ui-icon" style="width:12px;height:12px;display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;flex-shrink:0;" aria-hidden="true"></i> Stop
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-stop-btn';
}

export function destroy(el) {}

export function style() { return ''; }

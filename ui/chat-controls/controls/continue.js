'use strict';

// Chat control: continue — send 'continue' to resume the agent

export function html(cfg = {}) {
  return `<button type="button" class="chat-continue-btn" title="Send 'continue' to resume the agent" data-controls-name="continue">
    <i data-lucide="play" class="ui-icon" style="width:12px;height:12px;display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;flex-shrink:0;" aria-hidden="true"></i> Continue
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-continue-btn';
}

export function destroy(el) {}

export function style() { return ''; }

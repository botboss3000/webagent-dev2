'use strict';

// Chat element: activity — tool-call activity chip with expandable panel

export function html(cfg = {}) {
  return `<div class="chat-activity-wrapper" style="display:contents">
    <div class="chat-activity-panel" role="region" aria-label="Tool calls this turn" hidden></div>
    <button type="button" class="chat-activity-bar" data-element-name="activity" aria-expanded="false" aria-controls="chat-activity-panel" title="Show tool calls from this turn">
      <span class="chat-activity-text" aria-live="polite" aria-atomic="true"></span>
      <span class="chat-activity-chevron" aria-hidden="true">›</span>
    </button>
  </div>`;
}

export function init(el, cfg = {}) {
  // el is the wrapper div — find the bar and panel inside
  const bar = el.querySelector('.chat-activity-bar');
  if (bar) bar.id = 'chat-activity-bar';
  const panel = el.querySelector('.chat-activity-panel');
  if (panel) panel.id = 'chat-activity-panel';
}

export function destroy(el) {}

export function style() { return ''; }

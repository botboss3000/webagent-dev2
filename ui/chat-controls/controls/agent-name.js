'use strict';

// Chat control: agent_name — current agent name with agent-switcher dropdown

export function html(cfg = {}) {
  return `<span class="chat-header-name-row" data-controls-name="agent_name">
    <span class="chat-header-agent-name" style="display:none;"></span>
  </span>`;
}

export function init(el, cfg = {}) {
  // The agent name is populated by session-init.js which looks for
  // #chat-header-agent-name. We keep that working by exposing the inner span.
  // The click-to-switch logic is already wired in session-init.js on
  // .chat-header-name-row, so it works automatically.
}

export function destroy(el) {}

export function style() { return ''; }

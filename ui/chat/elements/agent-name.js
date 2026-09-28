'use strict';

// Chat element: agent_name — current agent name with agent-switcher dropdown

export function html(cfg = {}) {
  return `<span class="chat-header-name-row" data-element-name="agent_name">
    <span class="chat-header-agent-name" style="display:none;"></span>
  </span>`;
}

export function init(el, cfg = {}) {
  // Agent name is populated by session-init.js which looks for
  // #chat-header-agent-name. Click-to-switch is wired on .chat-header-name-row.
}

export function destroy(el) {}

export function style() { return ''; }

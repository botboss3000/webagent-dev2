'use strict';

// Chat control: abilities — toggle abilities on/off for the current chat

export function html(cfg = {}) {
  const size = cfg.element_size || '16px';
  return `<button type="button" class="chat-skills-btn" title="Abilities — click to turn each on/off for this chat" data-controls-name="abilities">
    <i data-lucide="blocks" style="width:${size};height:${size};"></i>
    <span class="csp-badge">0</span>
  </button>`;
}

export function init(el, cfg = {}) {
  // abilities.js looks for #chat-abilities-btn and #chat-abilities-badge
  // Set IDs for backward compat
  el.id = 'chat-abilities-btn';
  const badge = el.querySelector('.csp-badge');
  if (badge) badge.id = 'chat-abilities-badge';
}

export function destroy(el) {}

export function style() { return ''; }

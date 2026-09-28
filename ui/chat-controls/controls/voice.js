'use strict';

// Chat control: ephemeral voice conversation

import { isVoiceInputSupported } from '../../shared/js/attachments.js';

export function html(cfg = {}) {
  const size = cfg.element_size || '22px';
  return `<button type="button" class="chat-pill-voice" title="Voice conversation" aria-label="Voice conversation" data-controls-name="voice">
    <i data-lucide="mic" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el, cfg = {}) {
  el.id = 'chat-voice-btn';
  if (!isVoiceInputSupported()) {
    el.hidden = true;
    el.style.setProperty('display', 'none', 'important');
  }
}

export function destroy(el) {}

export function style() { return ''; }

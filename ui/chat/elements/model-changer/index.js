'use strict';

// Chat element: model_changer — cycle through available models with chevron arrows

export function html(cfg = {}) {
  return `<button type="button" class="chat-model-changer" title="Click for model list — ◀ ▶ arrows to cycle" data-element-name="model_changer">
    <svg class="cmc-chev cmc-chev-left" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
    <span class="cmc-name">model</span>
    <span class="cmc-status"></span>
    <svg class="cmc-chev cmc-chev-right" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"/></svg>
  </button>
  <div class="cmc-toast" style="display:none;"></div>`;
}

export function init(el, cfg = {}) {
  const btn = el.querySelector('.chat-model-changer');
  const toast = el.querySelector('.cmc-toast');
  if (btn) btn.id = 'chat-model-changer';
  if (toast) toast.id = 'cmc-toast';
}

export function destroy(el) {}

export function style() { return ''; }

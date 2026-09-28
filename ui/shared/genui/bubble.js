/* ════════════════════════════════════════════
   shared/genui/bubble.js — ES module
   Floating AI chat bubble. Import { ICON } from icons.js.
   Served at /ui/shared/genui/bubble.js
   ════════════════════════════════════════════ */

import { ICON } from './icons.js';

export const Bubble = {
  /**
   * Create a bubble instance wired to the given shadow root.
   * 
   * @param {ShadowRoot} root  - genui shadow root (for DOM queries)
   * @param {Function}   opts.send(msg, ctx) - called when user hits Send
   * @returns {{ open, close, sparkleBtn }}
   */
  create(root, opts) {
    const $ = (sel) => root.querySelector(sel);
    const sendFn = opts && opts.send;

    /* ---- DOM refs ---- */
    let el, backdrop, ctxEl, input, charCount, sendBtn, closeBtn, arrow;

    /* ---- State ---- */
    let triggerEl = null;
    let context = null;

    /* ---- Init ---- */
    function init() {
      el        = $('#aiBubble');
      backdrop  = $('#aiBackdrop');
      ctxEl     = $('#aiCtx');
      input     = $('#aiInput');
      charCount = $('#aiCharCount');
      sendBtn   = $('#aiSendBtn');
      closeBtn  = $('#aiBubbleClose');
      arrow     = $('#aiBubbleArrow');

      if (!el) return;

      closeBtn.addEventListener('click', () => close());
      backdrop.addEventListener('click', () => close());

      input.addEventListener('input', () => {
        charCount.textContent = input.value.length + ' / 500';
      });

      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.shiftKey) {
          // On touch devices (mobile), let Enter insert a newline; send via the button.
          if (window.matchMedia?.('(pointer: coarse)').matches) return;
          e.preventDefault();
          send();
        }
      });

      sendBtn.addEventListener('click', () => send());

      document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && el && el.classList.contains('visible')) close();
      });

      window.addEventListener('scroll', repositionIfOpen, { passive: true });
      window.addEventListener('resize', repositionIfOpen, { passive: true });
    }

    /* ---- Open ---- */
    function open(trigger, ctx) {
      if (!el) return;
      if (triggerEl === trigger && el.classList.contains('visible')) { close(); return; }
      if (el.classList.contains('visible')) {
        close(true);
        setTimeout(() => open(trigger, ctx), 200);
        return;
      }

      context = ctx || {};
      triggerEl = trigger;

      const project = context.projectName || 'Project';
      const label   = context.elementLabel || '';
      ctxEl.textContent = label ? `${project} \u203A ${label}` : project;

      input.value = '';
      charCount.textContent = '0 / 500';
      sendBtn.classList.remove('sent');
      sendBtn.innerHTML = ICON.send + ' Send';
      input.disabled = false;

      position();
      el.classList.remove('closing');
      el.classList.add('visible');
      backdrop.classList.add('visible');
      setTimeout(() => input.focus(), 150);
    }

    /* ---- Close ---- */
    function close(instant) {
      if (!el) return;
      if (instant) {
        el.classList.remove('visible', 'closing');
        backdrop.classList.remove('visible');
        triggerEl = null;
        context = null;
        return;
      }
      el.classList.add('closing');
      backdrop.classList.remove('visible');
      setTimeout(() => {
        el.classList.remove('visible', 'closing');
      }, 180);
      triggerEl = null;
      context = null;
    }

    /* ---- Position ---- */
    function position() {
      if (!triggerEl || !el) return;
      const rect = triggerEl.getBoundingClientRect();
      const bw = 320;
      const bh = el.offsetHeight || 200;
      const gap = 10;
      const vw = window.innerWidth;
      const vh = window.innerHeight;

      let top = rect.top - bh - gap;
      let arrowSide = 'bottom';
      if (top < 10) { top = rect.bottom + gap; arrowSide = 'top'; }
      if (top + bh > vh - 10) top = vh - bh - 10;
      if (top < 10) top = 10;

      let left = rect.left + rect.width / 2 - bw / 2;
      if (left < 10) left = 10;
      if (left + bw > vw - 10) left = vw - bw - 10;

      let arrowOffset = rect.left + rect.width / 2 - left;
      arrowOffset = Math.max(20, Math.min(bw - 20, arrowOffset));

      el.style.top = top + 'px';
      el.style.left = left + 'px';
      arrow.className = 'ai-bubble-arrow ' + arrowSide;
      arrow.style.left = arrowOffset + 'px';
    }

    function repositionIfOpen() {
      if (el && el.classList.contains('visible') && triggerEl) position();
    }

    /* ---- Send ---- */
    function send() {
      if (!input || !sendBtn) return;
      const msg = input.value.trim();
      if (!msg || sendBtn.classList.contains('sent')) return;

      const prefix = context && context.projectName ? `[${context.projectName}]` : '';
      const suffix = context && context.elementLabel ? ` \u2014 ${context.elementLabel}` : '';
      const fullMessage = `${prefix}${suffix}: ${msg}`;

      sendBtn.classList.add('sent');
      sendBtn.innerHTML = ICON.check + ' Sent';
      input.disabled = true;

      if (sendFn) {
        try { sendFn(fullMessage, context); } catch (_) {}
      }

      setTimeout(() => close(), 1200);
    }

    /* ---- Sparkle button factory ---- */
    function sparkleBtn(size, ctx) {
      const btn = document.createElement('span');
      btn.className = 'sparkle-btn sz-' + size;
      btn.innerHTML = ICON.sparkle;
      btn.title = 'Ask AI';
      btn.addEventListener('click', (e) => {
        e.stopPropagation();
        open(btn, ctx);
      });
      return btn;
    }

    init();
    return { open, close, sparkleBtn };
  }
};

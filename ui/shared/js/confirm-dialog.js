'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// This component styles itself only with design-system CSS variables (no hex),
// so it adapts to dark/light automatically.

// Hazard glyph as a RAW inline SVG (stroke = currentColor, so the tone class
// recolours it) — drawn directly so it paints without a lucide.createIcons()
// pass, which dynamically-appended nodes don't reliably get.
const HAZARD_SVG =
  '<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24"' +
  ' fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"' +
  ' stroke-linejoin="round" aria-hidden="true">' +
  '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/>' +
  '<line x1="12" x2="12" y1="9" y2="13"/><line x1="12" x2="12.01" y1="17" y2="17"/></svg>';

/*
 * ============================================================================
 * SHARED-HAZARD-CONFIRM
 * ============================================================================
 * The app's in-house replacement for the native browser `confirm()` popup: a
 * centred, theme-safe notification panel with a HAZARD ICON, a title, a
 * message, and Cancel / Confirm buttons. Returns a Promise<boolean> that
 * resolves true when the user confirms, false on Cancel / Esc / backdrop click.
 *
 *   import { hazardConfirm } from './confirm-dialog.js';
 *   if (!await hazardConfirm({ title: 'Encrypt everything?', message: '…' })) return;
 *
 * Use this for destructive / important confirmations instead of `confirm()` so
 * the prompt looks like the rest of the app. The look (panel, hazard badge,
 * buttons) lives in app3.css under "SHARED-HAZARD-CONFIRM"; both tones
 * (warning = amber, danger = red) read from design-system tokens.
 * ============================================================================
 */

/**
 * Show the hazard confirmation panel.
 * @param {object}  o
 * @param {string}  o.title         heading (bold)
 * @param {string}  o.message       body text (muted; supports plain text)
 * @param {string} [o.confirmLabel] confirm button label (default "Confirm")
 * @param {string} [o.cancelLabel]  cancel button label  (default "Cancel")
 * @param {'warning'|'danger'} [o.tone]  hazard colour (default "warning")
 * @returns {Promise<boolean>}      true = confirmed
 */
export function hazardConfirm({
  title = 'Are you sure?',
  message = '',
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  tone = 'warning',
} = {}) {
  return new Promise((resolve) => {
    const toneClass = tone === 'danger' ? 'wa-confirm-danger' : 'wa-confirm-warning';

    const overlay = document.createElement('div');
    overlay.className = 'wa-confirm-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    const panel = document.createElement('div');
    panel.className = `wa-confirm-panel ${toneClass}`;
    panel.innerHTML = `
      <div class="wa-confirm-head">
        <span class="wa-confirm-icon">${HAZARD_SVG}</span>
        <div class="wa-confirm-title"></div>
      </div>
      <div class="wa-confirm-msg"></div>
      <div class="wa-confirm-actions">
        <button type="button" class="ac-btn ac-btn-ghost wa-confirm-cancel"></button>
        <button type="button" class="ac-btn wa-confirm-go"></button>
      </div>`;
    // Text via textContent so a message can never inject markup.
    panel.querySelector('.wa-confirm-title').textContent = title;
    panel.querySelector('.wa-confirm-msg').textContent = message;
    const cancelBtn = panel.querySelector('.wa-confirm-cancel');
    const goBtn = panel.querySelector('.wa-confirm-go');
    cancelBtn.textContent = cancelLabel;
    goBtn.textContent = confirmLabel;

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    // Trigger the enter transition on the next frame.
    requestAnimationFrame(() => overlay.classList.add('show'));

    let settled = false;
    const close = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.remove('show');
      // Remove after the fade-out so it doesn't snap away.
      setTimeout(() => overlay.remove(), 160);
      resolve(result);
    };

    // Esc always cancels. Enter is intentionally NOT a global confirm — it
    // activates whatever button has focus (Cancel by default below), so a stray
    // Enter can never trigger a hazard action; tab to Confirm to use the keyboard.
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close(false); }
    }

    cancelBtn.addEventListener('click', () => close(false));
    goBtn.addEventListener('click', () => close(true));
    // Backdrop click (outside the panel) cancels.
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(false); });
    document.addEventListener('keydown', onKey, true);

    // Focus the SAFE action by default so a stray Enter/Space doesn't confirm a
    // hazard — the user must deliberately click or tab to Confirm.
    cancelBtn.focus();
  });
}

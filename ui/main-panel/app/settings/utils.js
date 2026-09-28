'use strict';

/**
 * Shared utilities for App Config tab modules.
 *
 * Exports:
 *   _fetch(url, opts)     — auth-aware fetch with Bearer token
 *   _qs(id)               — document.getElementById shorthand
 *   _esc(str)             — single-pass HTML escaping for inline strings
 *   _setIntStatus         — one-line status message under a card
 *   _markCheckSaving      — auto-save spinner indicator
 *   _flashCheck           — auto-save green-check / red-! indicator
 *   _copyToClipboard      — clipboard write with fallback for non-secure context
 *   _makeCopyBtn          — create a Lucide copy-button element
 *   _bindUri              — wire a copy-button to a dynamic text getter
 *   _attachUriCopy        — attach/update a copy button next to a <code> element
 */

import { icon } from '../../../shared/js/icons.js';
import { isAdmin, authHeaders } from '../../../shared/js/left-login.js?v=253';
import { copyText as _clipboardCopy } from '../../../shared/js/clipboard.js';

// ── Auth-aware fetch ──────────────────────────────────────────────────────
export function _fetch(url, opts = {}) {
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
  return fetch(url, opts);
}

export function _qs(id) { return document.getElementById(id); }

// Show a one-line status message under an integration/config card.
//   ok:true → green (success), else red (error). autoHide:true clears it after 3s.
export function _setIntStatus(el, msg, { ok = false, autoHide = false } = {}) {
  if (!el) return;
  el.textContent = msg;
  el.classList.toggle('ac-int-status-ok', ok);
  el.classList.toggle('ac-int-status-err', !ok);
  el.classList.add('ac-int-status-visible');
  if (autoHide) setTimeout(() => { el.classList.remove('ac-int-status-visible'); }, 3000);
}

// Single standardized HTML escaping function. Delegates to the shared
// attribute-safe escaper (dom-utils _escAttr) — these strings are interpolated
// into HTML attribute values across the App Config tables, so quotes must be
// escaped too (the plain text-only _esc would not).
export { _escAttr as _esc } from '../../../shared/js/dom-utils.js';

// ── Per-field auto-save indicator (mirrors agents.js _flashSaved) ────────────
export function _markCheckSaving(el) {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '<span class="agents-spinner"></span>';
}
export function _flashCheck(el, ok, errMsg = '') {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '';
  if (ok) {
    el.classList.add('saved');
    el.textContent = '✓';
    el.title = 'Saved';
    el._fadeT = setTimeout(() => { el.classList.remove('saved'); el.textContent = ''; }, 2200);
  } else {
    el.classList.add('error');
    el.textContent = '!';
    el.title = errMsg || 'Save failed';
    el._fadeT = setTimeout(() => { el.classList.remove('error'); el.textContent = ''; }, 4000);
  }
}

// ── URI copy-button helpers ───────────────────────────────────────────────

export function _makeCopyBtn() {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ac-uri-copy-btn';
  btn.title = 'Copy';
  btn.innerHTML = `<span class="copy-btn-icon">${icon('copy', { size: '13px' })}</span>`;
  return btn;
}

export function _copyToClipboard(text) {
  // Delegates to the shared clipboard module which handles the
  // secure-context + execCommand fallback across the whole app.
  return _clipboardCopy(text);
}

export function _bindUri(btn, getText) {
  btn.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const text = getText();
    _copyToClipboard(text).then(() => {
      btn.innerHTML = `<span class="copy-btn-icon">${icon('check', { size: '13px' })}</span>`;
      btn.classList.add('ac-uri-copied');
      setTimeout(() => {
        btn.innerHTML = `<span class="copy-btn-icon">${icon('copy', { size: '13px' })}</span>`;
        btn.classList.remove('ac-uri-copied');
      }, 1500);
    }).catch((err) => {
      console.error('[settings] copy failed:', err);
      btn.title = 'Copy failed: ' + (err?.message || err);
    });
  });
}

// Attaches (or updates) a Lucide copy button next to a redirect-URI <code> element.
// ── Formatter helpers ────────────────────────────────────────────────────
// Used across multiple tab modules (optimizer-stats, users).
export function _fmtDate(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' })
      + ' ' + d.toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  } catch { return ts; }
}

export function _attachUriCopy(codeEl, uri) {
  if (!codeEl) return;
  const single = uri || '';

  if (codeEl.dataset.uriCopy === 'block') {
    codeEl.querySelector('.ac-uri-text').textContent = single;
    return;
  }

  if (codeEl.dataset.uriCopy === 'inline') {
    codeEl.textContent = single;
    return;
  }

  if (codeEl.dataset.uriCopy !== 'block' && codeEl.dataset.uriCopy !== 'inline') {
    // First call — decide layout direction from computed style
    const isBlock = window.getComputedStyle(codeEl).display === 'block';
    if (isBlock) {
      codeEl.dataset.uriCopy = 'block';
      codeEl.classList.add('ac-uri-code-flex');
      const span = document.createElement('span');
      span.className = 'ac-uri-text';
      span.textContent = single;
      codeEl.appendChild(span);
      const btn = _makeCopyBtn();
      codeEl.appendChild(btn);
      _bindUri(btn, () => span.textContent);
    } else {
      codeEl.dataset.uriCopy = 'inline';
      codeEl.textContent = single;
      const btn = _makeCopyBtn();
      codeEl.insertAdjacentElement('afterend', btn);
      _bindUri(btn, () => codeEl.textContent);
      if (codeEl.parentElement) {
        codeEl.parentElement.classList.add('ac-uri-code-wrap');
      }
    }
  }
}

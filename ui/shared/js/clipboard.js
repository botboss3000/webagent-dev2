'use strict';

// ── Global clipboard safety net ───────────────────────────────────────────
//
// Goal: Ctrl/Cmd + C / X / V / A work EVERYWHERE in the app — in every text
// input, textarea and contenteditable field, and for any selected text in the
// page — regardless of which panel owns the keystroke or which (mis)behaving
// handler sits between the field and the browser.
//
// Why this is needed: native browser copy/paste only fires when the browser
// itself sees the shortcut and nothing has cancelled it. Across the app there
// are many areas where a container-level key handler, a focus quirk, or a
// non-selectable display element means the native action either does nothing or
// gets eaten before it runs. Rather than audit every panel, we install ONE
// document-level handler (capture phase) that, for the four clipboard
// shortcuts, performs the operation itself and stops anything downstream from
// interfering. The result: the same predictable copy/cut/paste/select-all
// behaviour in every corner of the UI.
//
// Two areas are deliberately left to their own logic:
//   • Terminals (xterm / the web-terminal iframe). There Ctrl+C must send an
//     interrupt to the shell, not copy, and the terminal already implements
//     its own copy/paste. We bail out the moment the event comes from inside a
//     terminal surface.
//   • First-class genui (ui/main-panel/genui). A genui now renders in a
//     shadow root in THIS document (no iframe), so its keydown events DO reach
//     here — but they're retargeted to the genui host, not the inner field, so
//     editableField() sees a non-editable element and the net bails. That hands
//     the field back to the browser's native copy/paste, which is correct: a
//     genui runs local-only over a secure (localhost) origin where native
//     clipboard works, and the genui owns its own inputs.
//
// The copy/read helpers fall back to the legacy execCommand path when the
// modern async Clipboard API is unavailable (non-secure http://<ip> contexts),
// so the net keeps working when the app is opened over a LAN address too.

/** Copy text to the clipboard. Resolves on success; never throws to callers. */
export function copyText(text) {
  text = text == null ? '' : String(text);
  if (navigator.clipboard && window.isSecureContext && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text);
  }
  // Fallback for non-secure contexts (http://<ip>:8080 etc.)
  return new Promise((resolve, reject) => {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.cssText = 'position:fixed;left:-9999px;top:0;opacity:0;';
      document.body.appendChild(ta);
      ta.focus();
      ta.select();
      const ok = document.execCommand('copy');
      document.body.removeChild(ta);
      ok ? resolve() : reject(new Error('execCommand copy failed'));
    } catch (e) { reject(e); }
  });
}

/**
 * Read text from the clipboard. Resolves to the string, or null when the
 * environment can't programmatically read (no API / permission denied). A null
 * return is the signal to let the browser's own native paste happen instead.
 */
function readText() {
  if (navigator.clipboard && navigator.clipboard.readText) {
    return navigator.clipboard.readText().catch(() => null);
  }
  return Promise.resolve(null);
}

// Input types that support text selection (selectionStart/End). Other types
// (number, email, date, checkbox, …) either throw on selectionStart or aren't
// free-text, so we leave them to the browser's native handling.
const TEXTLIKE_INPUT = new Set(['text', 'search', 'url', 'tel', 'password', '']);

/** Is this event coming from inside a terminal surface? (leave those alone) */
function inTerminal(el) {
  return !!(el && el.closest && el.closest(
    '.xterm, .terminal, .terminal-frame, .terminal-host, [data-terminal]'
  ));
}

/** Return the editable field the event targets, or null. */
function editableField(el) {
  if (!el || !el.tagName) return null;
  const tag = el.tagName;
  if (tag === 'TEXTAREA') return el;
  if (tag === 'INPUT') {
    const type = (el.type || 'text').toLowerCase();
    return TEXTLIKE_INPUT.has(type) ? el : null;
  }
  if (el.isContentEditable) return el;
  return null;
}

/** Text currently selected within a field (value-based) or in the page. */
function selectedText(field) {
  if (field && !field.isContentEditable && typeof field.selectionStart === 'number') {
    return field.value.slice(field.selectionStart, field.selectionEnd);
  }
  const sel = window.getSelection && window.getSelection();
  return sel ? String(sel) : '';
}

/** Insert text at the caret of a field, replacing any selection. */
function insertIntoField(field, text) {
  if (field.isContentEditable) {
    // execCommand('insertText') keeps the caret position and undo history; it's
    // the same path the contenteditable name field already uses for paste.
    if (!document.execCommand('insertText', false, text)) {
      field.textContent = (field.textContent || '') + text;
    }
    field.dispatchEvent(new Event('input', { bubbles: true }));
    return;
  }
  const hasSel = typeof field.selectionStart === 'number';
  const start = hasSel ? field.selectionStart : field.value.length;
  const end = hasSel ? field.selectionEnd : field.value.length;
  field.value = field.value.slice(0, start) + text + field.value.slice(end);
  const caret = start + text.length;
  try { field.selectionStart = field.selectionEnd = caret; } catch (_) {}
  // Let the app react (autosize, validation, suggestions, etc.).
  field.dispatchEvent(new Event('input', { bubbles: true }));
}

/** Delete the current selection within a field after a cut. */
function deleteSelection(field) {
  if (field.isContentEditable) {
    document.execCommand('delete');
    field.dispatchEvent(new Event('input', { bubbles: true }));
    return;
  }
  if (typeof field.selectionStart !== 'number') return;
  const start = field.selectionStart, end = field.selectionEnd;
  if (start === end) return;
  field.value = field.value.slice(0, start) + field.value.slice(end);
  try { field.selectionStart = field.selectionEnd = start; } catch (_) {}
  field.dispatchEvent(new Event('input', { bubbles: true }));
}

function onKeyDown(e) {
  const mod = e.ctrlKey || e.metaKey;
  if (!mod || e.altKey) return;            // only Ctrl/Cmd combos, no Alt
  const key = (e.key || '').toLowerCase();
  if (key !== 'c' && key !== 'x' && key !== 'v' && key !== 'a') return;

  const target = e.target;
  if (inTerminal(target)) return;          // terminals own these keys
  const field = editableField(target);

  // ── Select-all ──────────────────────────────────────────────────────────
  if (key === 'a') {
    if (!field) return;                    // outside a field: leave native page select-all
    e.preventDefault();
    if (field.isContentEditable) {
      const range = document.createRange();
      range.selectNodeContents(field);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(range);
    } else if (typeof field.select === 'function') {
      field.select();
    }
    return;
  }

  // ── Copy / Cut ──────────────────────────────────────────────────────────
  if (key === 'c' || key === 'x') {
    const text = selectedText(field);
    if (!text) return;                     // nothing selected → nothing to do
    e.preventDefault();
    e.stopImmediatePropagation();
    copyText(text).catch(() => {});
    if (key === 'x' && field) deleteSelection(field);
    return;
  }

  // ── Paste ─────────────────────────────────────────────────────────────────
  if (key === 'v') {
    if (!field) return;                    // paste only makes sense into a field
    // Chat pills (composer, agent builder, page/admin prompt bars) accept
    // pasted IMAGES via their own native `paste` handler (see attachments.js).
    // That handler only fires if the browser's native paste runs — so we must
    // NOT cancel it here. Re-implementing paste as text-only below would
    // silently swallow image pastes. Native paste handles their text fine too,
    // so bow out for any field inside a chat pill. (`.chat-pill` is on every
    // such row in markup; `data-chat-pill-uploads-wired` is set once the
    // image/drop handler is attached.)
    if (field.closest && field.closest('.chat-pill, [data-chat-pill-uploads-wired]')) return;
    // If we can't read the clipboard programmatically (non-secure context with
    // no API), DON'T cancel — let the browser's native paste do its job.
    if (!(navigator.clipboard && navigator.clipboard.readText)) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    readText().then((text) => {
      if (text == null || text === '') return;
      insertIntoField(field, text);
    }).catch(() => {});
  }
}

let _installed = false;

/** Install the global clipboard safety net. Idempotent. */
export function initClipboard() {
  if (_installed) return;
  _installed = true;
  // Capture phase so we run before any panel-level handler can swallow or
  // cancel the shortcut.
  document.addEventListener('keydown', onKeyDown, true);
}

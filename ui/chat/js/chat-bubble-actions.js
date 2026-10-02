'use strict';

// Per-bubble action row — model tag, collapse/expand, read-aloud
// (SpeechSynthesis, with live paragraph highlighting on every supported
// browser), copy, and two-click per-turn delete. Sets
// app._addBubbleActions. The footer shows on STREAMING bubbles too (live or
// being recovered from the DB), minus the delete button until the turn finalizes.
// Module map for this folder: ui/chat/js/README.md.

import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { icon } from '../../shared/js/icons.js';
import { copyText } from '../../shared/js/clipboard.js';
import { getAgentChatUi } from '../../shared/js/app-prompts.js';
import { showLockedControlPopover } from '../../shared/js/chat-pill-config.js';
import { _formatRelativeTime, addChatBubble, _setBubbleCreatedAt, _fillAgentBubble } from './chat-bubble.js';
import { openChatElementFullView } from './chat-full-view.js';

const _MESSAGE_GUTTER_DEFAULTS = {
  user: {
    enabled: true,
    order: ['time', 'full_view', 'copy', 'undo', 'delete', 'more'],
    controls: {},
  },
  agent: {
    enabled: true,
    order: ['time', 'full_view', 'collapse', 'read_aloud', 'copy', 'fork', 'delete', 'more'],
    controls: {},
  },
  more_menu: {
    enabled: true,
    order: ['context', 'cost', 'model', 'message_id', 'row_details', 'schema', 'refresh'],
    controls: {},
  },
};

function _mergeGutterConfig(base, override) {
  if (!override || typeof override !== 'object' || Array.isArray(override)) return base;
  const out = { ...(base || {}) };
  Object.entries(override).forEach(([key, value]) => {
    out[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? _mergeGutterConfig(out[key], value)
      : value;
  });
  return out;
}

function _messageGuttersConfig() {
  const ui = getAgentChatUi();
  const surfaceKey = window.__CHAT_PORTAL__
    ? 'chat_widget'
    : (window.innerWidth <= 768 ? 'chat_mobile' : 'chat_desktop');
  const common = ui?.chat_common?.message_gutters || {};
  const surface = ui?.[surfaceKey]?.message_gutters || {};
  return _mergeGutterConfig(_mergeGutterConfig(_MESSAGE_GUTTER_DEFAULTS, common), surface);
}

function _gutterControlEnabled(spec, name) {
  const cfg = spec?.controls?.[name];
  return spec?.enabled !== false && cfg !== false && cfg?.enabled !== false;
}

function _applyLockedGutterControl(button, cfg) {
  if (!button || !cfg || cfg.locked !== true) return button;
  button.classList.add('tier-locked');
  button.setAttribute('aria-disabled', 'true');
  button.title = cfg.locked_feature
    ? `${cfg.locked_feature} feature is disabled for anonymous users.`
    : (cfg.locked_message || 'Register to unlock this feature.');
  button.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopImmediatePropagation();
    showLockedControlPopover(button, cfg);
  }, true);
  return button;
}

function _appendConfiguredGutterControls(gutter, spec, factories) {
  if (!gutter) return gutter;
  if (spec?.enabled === false) {
    gutter.hidden = true;
    return gutter;
  }
  const order = Array.isArray(spec?.order) ? spec.order : Object.keys(factories);
  order.forEach((name) => {
    if (!_gutterControlEnabled(spec, name) || typeof factories[name] !== 'function') return;
    const element = factories[name]();
    if (!element) return;
    element.dataset.gutterControl = name;
    _applyLockedGutterControl(element, spec.controls?.[name]);
    gutter.appendChild(element);
  });
  return gutter;
}

function _insertConfiguredGutterControl(gutter, spec, name, element) {
  if (!element || !_gutterControlEnabled(spec, name)) return;
  const order = Array.isArray(spec?.order) ? spec.order : [];
  const position = order.indexOf(name);
  if (position < 0) return;
  element.dataset.gutterControl = name;
  _applyLockedGutterControl(element, spec.controls?.[name]);
  const before = [...gutter.children].find((child) => {
    const childPosition = order.indexOf(child.dataset.gutterControl);
    return childPosition >= 0 && childPosition > position;
  });
  gutter.insertBefore(element, before || null);
}

// ── Per-turn model tag ──────────────────────────────────────────────────────
// Each agent turn's footer shows which model (and reasoning-effort mode) ran it,
// e.g. "deepseek-v4-flash HIGH". The model + effort are persisted in the
// assistant interaction's metadata by the loop (_build_meta) and surfaced live
// via the pipeline llm_call_end event (captured in chat-activity.js →
// app._activeTurnModel / app._activeTurnEffort).

// Drop the provider prefix: "deepseek/deepseek-v4-flash" → "deepseek-v4-flash".
function _shortModelName(name) {
  if (!name) return '';
  return name.includes('/') ? name.slice(name.lastIndexOf('/') + 1) : name;
}

// The label a model is presented under in the ⋮ menu: its configured display
// name (the roster alias) when one exists, otherwise the real model name with
// its provider prefix dropped. Mirrors the server's public slot label
// (_public_model_slot in app/admin/settings.py) — the same rule the footer
// model picker already uses. The server records the real model id as
// `display_name` when a row has no alias, so a value equal to the id is not an
// alias and must still lose its provider path here.
function _publicModelLabel(model, alias) {
  const raw = String(model || '').trim();
  const name = String(alias || '').trim();
  if (name && name !== raw) return name;
  return _shortModelName(name || raw);
}

// "model EFFORT" — effort suffix only when a non-default level is set.
function _buildModelLabel(model, effort) {
  const short = _shortModelName(model);
  if (!short) return '';
  const eff = (effort || '').trim().toLowerCase();
  if (eff && eff !== 'default') return short + ' ' + eff.toUpperCase();
  return short;
}

// Parse an interaction's metadata JSON → display label (or '').
function _modelLabelFromMeta(metaStr) {
  if (!metaStr) return '';
  let m;
  try { m = JSON.parse(metaStr); } catch (_) { return ''; }
  if (!m || typeof m !== 'object') return '';
  return _buildModelLabel(_publicModelLabel(m.model, m.display_name), m.effort);
}

// Stamp a bubble with its raw model identity and its optional user-defined
// display name separately. The More menu needs both: the name follows the
// public alias-first rule, while the raw id remains an internal lookup key for
// roster position and parent-message inheritance.
function _setBubbleModel(bubble, model, effort, displayName = '') {
  if (!bubble) return;
  const raw = String(model || '').trim();
  const alias = String(displayName || '').trim();
  const label = _buildModelLabel(_publicModelLabel(raw, alias), effort);
  if (!label) return;
  bubble.dataset.modelLabel = label;
  bubble.dataset.modelTitle = raw || label;
  if (alias && alias !== raw) bubble.dataset.modelDisplayName = alias;
  else delete bubble.dataset.modelDisplayName;
  // Premium model — fire glow on the bubble
  const eff = (effort || '').trim().toLowerCase();
  bubble.classList.toggle('premium', eff === 'high');
}

// Same, sourced from a metadata JSON string (persisted-history path).
function _setBubbleModelFromMeta(bubble, metaStr) {
  if (!bubble || !metaStr) return;
  let m;
  try { m = JSON.parse(metaStr); } catch (_) { return; }
  if (m && typeof m === 'object') _setBubbleModel(bubble, m.model, m.effort, m.display_name);
}

// Build the .bubble-model span and place it at the front of the action row (it
// carries the auto-margin that pushes the buttons to the right — see app1.css).
function _injectModelTag(actions, bubble) {
  if (!actions || !bubble || !bubble.dataset.modelLabel) return;
  if (actions.querySelector('.bubble-model')) return;
  const tag = document.createElement('span');
  tag.className = 'bubble-model';
  tag.title = bubble.dataset.modelTitle || bubble.dataset.modelLabel;
  const inner = document.createElement('bdi');
  inner.textContent = bubble.dataset.modelLabel;
  tag.appendChild(inner);
  // After the timestamp (if any) so order reads: time · model … buttons.
  const time = actions.querySelector(':scope > .bubble-time');
  if (time && time.nextSibling) actions.insertBefore(tag, time.nextSibling);
  else if (time) actions.appendChild(tag);
  else actions.insertBefore(tag, actions.firstChild);
}

// ── Per-bubble action row helpers ──────────────────────────────────────────

// Bubble chrome that is never part of the message content. Copy, read-aloud,
// and the undo/edit flows ALL share this one list, so a prompt clamped by
// chat_ui.json still yields its FULL preserved text: the `.user-message-expand`
// toggle only reveals hidden lines (CSS max-height) and must never leak into
// copied or spoken text.
const _BUBBLE_TEXT_SKIP_SELECTOR = '.label, .bubble-actions, .stop-btn, .persistence-details, .turn-gutter, .tts-pause-btn, .user-message-expand';

// Extracts the readable text from a bubble, excluding labels, the action row,
// the turn-gutter footer (timestamp, model tag, buttons), and the user-message
// truncation toggle.
function _getBubbleText(bubble) {
  if (!bubble) return '';
  const clone = bubble.cloneNode(true);
  clone.querySelectorAll(_BUBBLE_TEXT_SKIP_SELECTOR).forEach(el => el.remove());
  return clone.textContent.trim();
}

// Text to put on the clipboard when Copy is pressed.
function _getBubbleCopyText(bubble) {
  if (bubble && typeof bubble.__mdSource === 'string' && bubble.__mdSource.trim()) {
    return bubble.__mdSource;
  }
  return _getBubbleText(bubble);
}

function _renderActionIcons(container) {
  if (container && window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      window.lucide.createIcons({
        nodes: Array.from(container.querySelectorAll('[data-lucide]:not(.lucide)')),
      });
    } catch (_) {}
  }
}

function _setActionIcon(btn, iconName) {
  const i = btn.querySelector('i');
  if (!i) return;
  i.setAttribute('data-lucide', iconName);
  i.classList.remove('lucide');
  i.removeAttribute('stroke');
  while (i.firstChild) i.removeChild(i.firstChild);
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try { window.lucide.createIcons({ nodes: [i] }); } catch (_) {}
  }
}

// ── Action confirmation notices ───────────────────────────────────────────
// Small floating pills ("Copied", "Messages undone", "Turn deleted", …) that
// confirm a bubble-footer action actually happened. Stacked above the chat
// pill, auto-dismissed, pointer-events none so they never block clicks.
// Styled by .bubble-action-notice in app1.css (design-system vars, both themes).
function _showActionNotice(text, isError) {
  let wrap = document.getElementById('bubble-action-notices');
  if (!wrap) {
    wrap = document.createElement('div');
    wrap.id = 'bubble-action-notices';
    wrap.setAttribute('aria-live', 'polite');
    wrap.style.cssText = 'position:fixed;left:0;right:0;bottom:88px;display:flex;flex-direction:column;align-items:center;gap:6px;pointer-events:none;z-index:10000;';
    document.body.appendChild(wrap);
  }
  const el = document.createElement('div');
  el.className = 'bubble-action-notice' + (isError ? ' err' : '');
  el.innerHTML = '<i data-lucide="' + (isError ? 'circle-alert' : 'check') + '" style="width:13px;height:13px;flex:none;"></i><span></span>';
  el.querySelector('span').textContent = text;
  wrap.appendChild(el);
  _renderActionIcons(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => {
    el.classList.remove('show');
    setTimeout(() => {
      el.remove();
      if (!wrap.children.length) wrap.remove();
    }, 220);
  }, 1800);
}

// ── Read-aloud paragraph highlighting ───────────────────────────────────────
// The bubble text is split into PARAGRAPHS (DOM block elements — markdown
// <p>/<li>/<pre>/headings — or blank lines in plain pre-wrap text) and
// each paragraph is spoken as its own utterance. Utterances are CHAINED (the
// next is sent from the previous one's onend) rather than all enqueued up
// front: Chromium's speech queue is known to drop utterances when several are
// queued at once (especially on Android's Google-TTS bridge), and chaining
// also isolates a failing paragraph so the rest still play. Each paragraph is
// highlighted the moment its utterance starts, so the karaoke effect works on
// EVERY browser that supports speechSynthesis — no word-boundary events
// needed. Highlight = soft brand wash, NOT bold. Read-aloud also stops the
// moment the user engages with the chat composer (focus or typing) or
// switches sessions — see _initTtsComposerStop at the end of this section.
// Shared with _getBubbleText via _BUBBLE_TEXT_SKIP_SELECTOR — one list, so the
// exclusions can never drift apart.
const _TTS_SKIP_SELECTOR = _BUBBLE_TEXT_SKIP_SELECTOR;

// The bubble's text nodes in reading order — mirrors _getBubbleText's exclusions
// so paragraph offsets map 1:1 onto the utterance text.
function _ttsTextNodes(bubble) {
  const walker = document.createTreeWalker(bubble, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      let el = node.parentElement;
      while (el && el !== bubble) {
        if (el.matches && el.matches(_TTS_SKIP_SELECTOR)) return NodeFilter.FILTER_REJECT;
        el = el.parentElement;
      }
      return node.textContent ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const nodes = [];
  while (walker.nextNode()) nodes.push(walker.currentNode);
  return nodes;
}

const _TTS_BLOCK_TAGS = new Set(['P','DIV','LI','UL','OL','PRE','BLOCKQUOTE','H1','H2','H3','H4','H5','H6','TABLE','TR','TD','TH','SECTION','ARTICLE','FIGURE']);

// Nearest block-level ancestor of a text node — the visual "paragraph" unit.
function _ttsBlockAncestor(node) {
  let el = node.parentElement;
  while (el) {
    if (_TTS_BLOCK_TAGS.has(el.tagName)) return el;
    el = el.parentElement;
  }
  return null;
}

// Split the bubble's text into paragraph ranges [start, end) over the RAW
// concatenated text (the same string the utterances are sliced from).
// Paragraph boundaries come from the DOM, not from blank lines:
//   1. Block-element boundaries — markdown renders each paragraph as its own
//      <p>/<li>/<pre>/heading, so consecutive text runs in different blocks
//      are separate paragraphs. Each <li> bullet is its own paragraph. This
//      is the main path for agent replies, where textContent joins blocks
//      with NO separator (the old blank-line scan collapsed the whole message
//      into one range).
//   2. Blank lines inside a single block — plain pre-wrap bubbles keep their
//      literal newlines, so "\n\s*\n" separates paragraphs there.
//   3. Bullet items — a newline followed by a bullet marker, or an inline
//      bullet glyph (•) mid-paragraph. The latter covers markdown rendered
//      with `breaks:true`, which turns "• item" lines into <br>-joined text
//      with no newline in textContent — the glyph itself is the only
//      boundary, so each bullet still reads + highlights independently.
// Whitespace-only gaps between blocks are dropped.
function _ttsParagraphRanges(nodes, text) {
  const blockRanges = [];
  let start = 0;
  let prevBlock = null;
  let pos = 0;
  for (const node of nodes) {
    const len = node.textContent.length;
    if (!len) continue;
    const block = _ttsBlockAncestor(node);
    if (prevBlock !== null && block !== prevBlock) {
      if (pos > start) blockRanges.push({ start, end: pos });
      start = pos;
    }
    prevBlock = block;
    pos += len;
  }
  if (pos > start) blockRanges.push({ start, end: pos });
  const ranges = [];
  for (const r of blockRanges) {
    let s = r.start;
    let i = r.start;
    while (i < r.end) {
      const ch = text[i];
      if (ch === '\n') {
        let j = i + 1;
        while (j < r.end && (text[j] === ' ' || text[j] === '\t' || text[j] === '\r')) j++;
        if (j < r.end && text[j] === '\n') {
          // Blank line — hard paragraph break.
          if (i > s) ranges.push({ start: s, end: i });
          let k = j + 1;
          while (k < r.end && text[k] === '\n') k++;
          s = k;
          i = k;
          continue;
        }
        // Single newline: split only if the next line starts a bullet item.
        if (j < r.end && _ttsIsBulletStart(text, j)) {
          if (i > s) ranges.push({ start: s, end: i });
          s = i + 1;
        }
        i = j;
        continue;
      }
      // Inline bullet glyph mid-paragraph (<br>-joined list lines).
      if ((ch === '\u2022' || ch === '\u2023' || ch === '\u25E6' || ch === '\u25AA')
          && i > s && /\s/.test(text[i + 1] || '')) {
        if (i > s) ranges.push({ start: s, end: i });
        s = i;
      }
      i++;
    }
    if (r.end > s) ranges.push({ start: s, end: r.end });
  }
  // Trim edges to non-whitespace and drop empties.
  return ranges.filter((r) => {
    while (r.start < r.end && /\s/.test(text[r.start])) r.start++;
    while (r.end > r.start && /\s/.test(text[r.end - 1])) r.end--;
    return r.end > r.start;
  });
}

// Is the text at index i the start of a bullet item? Matches markdown-style
// bullet lines ("- x", "* x", "+ x", "1. x", "- [ ] x") and Unicode bullet
// glyphs (• ‣ ◦ ▪). Used to split plain-text / <br>-joined lists that arrive
// as a single block so each bullet gets its own highlight + pause control.
function _ttsIsBulletStart(text, i) {
  const ch = text[i];
  if (!ch) return false;
  if (ch === '\u2022' || ch === '\u2023' || ch === '\u25E6' || ch === '\u25AA') return true;
  if (ch === '-' || ch === '*' || ch === '+') {
    if (/\s/.test(text[i + 1] || '')) return true;
    if (text[i + 1] === '[' && /[ xX]/.test(text[i + 2] || '') && text[i + 3] === ']') return true;
  }
  if (/\d/.test(ch)) return /^\d{1,3}[.)]\s/.test(text.slice(i, i + 6));
  return false;
}

// Character offset in the bubble's raw text at a viewport point — used for
// click-to-jump (clicking another paragraph while reading). Works via
// caretRangeFromPoint (Chromium/WebKit) / caretPositionFromPoint (Firefox).
// Returns null when the click didn't land on a text node.
function _ttsOffsetFromPoint(bubble, x, y) {
  const nodes = _ttsTextNodes(bubble);
  if (!nodes.length) return null;
  let range = null;
  if (document.caretRangeFromPoint) {
    range = document.caretRangeFromPoint(x, y);
  } else if (document.caretPositionFromPoint) {
    const cp = document.caretPositionFromPoint(x, y);
    if (cp) {
      range = document.createRange();
      range.setStart(cp.offsetNode, cp.offset);
      range.collapse(true);
    }
  }
  if (!range) return null;
  const startNode = range.startContainer;
  let pos = 0;
  for (const node of nodes) {
    if (node === startNode) return pos + range.startOffset;
    pos += node.textContent.length;
  }
  // startContainer may be an element (e.g. <strong>): locate the first text
  // node within it that belongs to the bubble's text-node list.
  if (startNode.nodeType === Node.ELEMENT_NODE) {
    const sub = document.createTreeWalker(startNode, NodeFilter.SHOW_TEXT);
    const first = sub.nextNode();
    if (first) {
      pos = 0;
      for (const node of nodes) {
        if (node === first) return pos;
        pos += node.textContent.length;
      }
    }
  }
  return null;
}

// Remove any active paragraph-highlight spans and pause buttons, and merge the
// split text nodes back so the bubble's DOM is pristine after playback.
function _ttsClearHighlights(bubble) {
  if (!bubble) return;
  bubble.querySelectorAll('.tts-paragraph-active').forEach((span) => {
    const parent = span.parentNode;
    while (span.firstChild) parent.insertBefore(span.firstChild, span);
    parent.removeChild(span);
  });
  if (_ttsPauseBtnBubble === bubble) _ttsHidePauseBtn();
  bubble.normalize();
}

// Wrap the text range [start, end) — relative to _getBubbleText's trimmed
// text — in a .tts-paragraph-active span, clearing any previous highlight.
function _ttsHighlightRange(bubble, start, end) {
  _ttsClearHighlights(bubble);
  const nodes = _ttsTextNodes(bubble);
  if (!nodes.length) return;
  const raw = nodes.map((n) => n.textContent).join('');
  // Paragraph offsets are relative to the trimmed text — shift past any
  // leading whitespace in the raw DOM text.
  const lead = raw.length - raw.trimStart().length;
  start += lead;
  end += lead;
  if (start < 0 || end > raw.length || start >= end) return;
  let pos = 0;
  for (const node of nodes) {
    const len = node.textContent.length;
    const nodeStart = pos;
    pos += len;
    const nodeEnd = pos;
    if (nodeEnd <= start || nodeStart >= end) continue;
    const from = Math.max(0, start - nodeStart);
    const to = Math.min(len, end - nodeStart);
    if (from >= to) continue;
    const after = node.splitText(from);
    if (to < nodeEnd) after.splitText(to - from);
    const span = document.createElement('span');
    span.className = 'tts-paragraph-active';
    span.appendChild(after);
    node.parentNode.insertBefore(span, node.nextSibling);
  }
}

// ── Floating pause/resume control ──────────────────────────────────────────
// A single circular button that hovers at the right edge of the bubble —
// center-x aligned with the gutter's chevron buttons — and rides the vertical
// center of the paragraph currently being read, dropping down as the
// highlight advances to the next paragraph. Clicking it pauses the shared
// speech queue (icon → play, title "Resume"); clicking again resumes
// (icon → pause). Native speechSynthesis pause()/resume() only affect the
// currently speaking utterance — exactly our chained model, since only one
// paragraph is ever queued at a time. Removed by _ttsHidePauseBtn whenever
// playback stops or the highlight moves on; a fresh one is created for each
// paragraph.
let _ttsPauseBtn = null;
let _ttsPauseBtnBubble = null;

function _ttsHidePauseBtn() {
  if (_ttsPauseBtn) {
    _ttsPauseBtn.remove();
    _ttsPauseBtn = null;
    _ttsPauseBtnBubble = null;
  }
}

// Anchor the floating button to the currently active paragraph. It floats in
// the ~20% right band of the viewport OUTSIDE the bubble (the bubble occupies
// ~80% width): position:fixed, vertically centered on the paragraph being
// read, horizontally just right of the bubble's right edge — the same column
// the gutter's chevron buttons sit in. Re-anchored on scroll/resize via the
// document-level capture listener in _initTtsComposerStop.
function _ttsRepositionPauseBtn() {
  if (!_ttsPauseBtn || !_ttsPauseBtnBubble) return;
  const bubble = _ttsPauseBtnBubble;
  const span = bubble.querySelector('.tts-paragraph-active');
  if (!span) { _ttsHidePauseBtn(); return; }
  const sRect = span.getBoundingClientRect();
  const w = _ttsPauseBtn.offsetWidth || 26;
  const h = _ttsPauseBtn.offsetHeight || 26;
  const top = Math.max(4, sRect.top + sRect.height / 2 - h / 2);
  // Preferred: just right of the bubble's right edge (the 20% band). If that
  // would overflow the viewport (mobile full-width bubble), pull it in — but
  // never further left than the bubble's edge.
  const bRect = bubble.getBoundingClientRect();
  let left = bRect.right + 8;
  const maxLeft = window.innerWidth - w - 6;
  if (left > maxLeft) left = Math.max(bRect.right - w - 4, maxLeft);
  _ttsPauseBtn.style.top = top + 'px';
  _ttsPauseBtn.style.left = left + 'px';
}

function _ttsShowPauseBtn(bubble, token) {
  _ttsHidePauseBtn();
  if (!bubble || !token) return;
  if (!bubble.querySelector('.tts-paragraph-active')) return;
  const pb = document.createElement('button');
  pb.type = 'button';
  pb.className = 'tts-pause-btn';
  pb.title = 'Pause reading';
  pb.innerHTML = '<i data-lucide="pause" style="width:15px;height:15px;"></i>';
  const setIcon = (iconName, title) => {
    pb.title = title;
    pb.innerHTML = '<i data-lucide="' + iconName + '" style="width:15px;height:15px;"></i>';
    _renderActionIcons(pb);
  };
  pb.addEventListener('click', (event) => {
    event.stopPropagation();
    if (token.cancelled || token.finished) return;
    if (token.paused) {
      if (token.resume) token.resume();
    } else {
      if (token.pause) token.pause();
      setIcon('play', 'Resume reading');
    }
  });
  bubble.appendChild(pb);
  _ttsPauseBtn = pb;
  _ttsPauseBtnBubble = bubble;
  _renderActionIcons(pb);
  _ttsRepositionPauseBtn();
}

// Stop ALL read-aloud (any bubble): mark chains dead, restore buttons, clear
// highlights, then cancel the shared speech queue. Also silences fire-and-
// forget speech (activity-notice rows) since it shares the same queue.
function _ttsStopAll() {
  if (!('speechSynthesis' in window)) return;
  document.querySelectorAll('[data-speaking="true"]').forEach((other) => {
    if (other.__ttsToken) {
      other.__ttsToken.cancelled = true;
      if (other.__ttsToken.finish) other.__ttsToken.finish();
    }
    delete other.dataset.speaking;
    other.title = 'Read aloud';
    _setActionIcon(other, 'volume-2');
    const host = other.closest('.chat-bubble');
    if (host) _ttsClearHighlights(host);
  });
  _ttsHidePauseBtn();
  try { window.speechSynthesis.cancel(); } catch (_) {}
}

// Speak a bubble's text one paragraph at a time. Paragraphs are chained from
// the previous utterance's onend (see header note on why not a single big
// queue); each paragraph's highlight is applied in its onstart, so the
// highlight stays in sync with the audio on every browser that supports
// speechSynthesis.
function _speakBubble(btn, bubble) {
  if (!('speechSynthesis' in window)) {
    alert('Text-to-speech is not supported in this browser.');
    return;
  }
  if (btn.dataset.speaking === 'true') {
    _ttsStopAll(); // Same-button stop: kill the whole chain + queue.
    return;
  }
  const nodes = _ttsTextNodes(bubble);
  const raw = nodes.map((n) => n.textContent).join('');
  if (!raw.trim()) return;
  const paragraphs = _ttsParagraphRanges(nodes, raw);
  if (!paragraphs.length) return;
  _ttsStopAll(); // Reset any other bubble that is currently speaking.
  const token = { cancelled: false, finished: false, paused: false, index: 0, currentIdx: 0 };
  btn.__ttsToken = token;
  bubble.__ttsToken = token;
  const restore = () => {
    if (token.finished) return;
    token.finished = true;
    if (bubble.__ttsToken === token) bubble.__ttsToken = null;
    delete btn.dataset.speaking;
    btn.title = 'Read aloud';
    _setActionIcon(btn, 'volume-2');
    _ttsClearHighlights(bubble);
  };
  token.finish = restore;
  const speakNext = () => {
    if (token.cancelled || token.index >= paragraphs.length) {
      restore();
      return;
    }
    const r = paragraphs[token.index];
    const u = new SpeechSynthesisUtterance(raw.slice(r.start, r.end).trim());
    u.onstart = () => {
      if (token.cancelled) return;
      token.paused = false;
      token.currentIdx = token.index;
      _ttsHighlightRange(bubble, r.start, r.end);
      _ttsShowPauseBtn(bubble, token);
    };
    u.onend = () => {
      // On stop or pause-cancel the chain must HOLD — never advance.
      if (token.cancelled || token.paused) return;
      token.index++;
      speakNext();
    };
    // A genuinely failing paragraph must not kill the rest of the reading —
    // skip it. 'canceled'/'interrupted' are OUR cancels (pause, jump, stop,
    // new playback) — never advance the chain for those.
    u.onerror = (e) => {
      if (token.cancelled || token.paused) return;
      const err = e && e.error;
      if (err === 'canceled' || err === 'interrupted') return;
      token.index++;
      speakNext();
    };
    window.speechSynthesis.speak(u);
  };
  // Pause = cancel the queue and hold the chain on the current paragraph.
  // (The engines' native pause()/resume() is unreliable — Chrome in
  // particular often ignores resume() — so pause is implemented as
  // cancel-and-hold, and resume re-speaks the paragraph from its start.)
  token.pause = () => {
    if (token.cancelled || token.finished || token.paused) return;
    token.paused = true;
    try { window.speechSynthesis.cancel(); } catch (_) {}
  };
  token.resume = () => {
    if (token.cancelled || token.finished || !token.paused) return;
    token.paused = false;
    token.index = token.currentIdx;
    speakNext();
  };
  // Jump to a specific paragraph: stop the current audio and start reading
  // from the clicked bullet/paragraph onward.
  token.jumpTo = (idx) => {
    if (token.cancelled || token.finished) return;
    if (idx < 0 || idx >= paragraphs.length || idx === token.currentIdx) return;
    token.paused = false;
    token.index = idx;
    token.currentIdx = idx;
    try { window.speechSynthesis.cancel(); } catch (_) {}
    _ttsClearHighlights(bubble);
    speakNext();
  };
  // While this bubble is being read, clicking any of its paragraphs/bullets
  // stops the current audio and plays from the clicked one. Bound once per
  // bubble; reads bubble.__ttsToken so it only acts during active playback
  // (never interferes with idle text selection or copy).
  if (!bubble.__ttsClickBound) {
    bubble.__ttsClickBound = true;
    bubble.addEventListener('click', (e) => {
      const t = bubble.__ttsToken;
      if (!t || !t.jumpTo || t.cancelled || t.finished) return;
      if (e.target.closest('.tts-pause-btn, .turn-gutter, .bubble-actions, .label, a, button')) return;
      // Dragging to select text should not trigger a jump.
      const sel = window.getSelection && window.getSelection();
      if (sel && !sel.isCollapsed) return;
      const offset = _ttsOffsetFromPoint(bubble, e.clientX, e.clientY);
      if (offset === null) return;
      const ns = _ttsTextNodes(bubble);
      const rt = ns.map((n) => n.textContent).join('');
      const ps = _ttsParagraphRanges(ns, rt);
      const lead = rt.length - rt.trimStart().length;
      const idx = ps.findIndex((r) => offset - lead >= r.start && offset - lead < r.end);
      if (idx >= 0) t.jumpTo(idx);
    });
  }
  btn.dataset.speaking = 'true';
  btn.title = 'Stop reading';
  _setActionIcon(btn, 'square');
  speakNext();
}

// Stop read-aloud the moment the user engages with the chat composer (focus
// or typing) — speech should never compete with the user's own input.
function _initTtsComposerStop() {
  const input = document.getElementById('chat-input');
  if (input) {
    const stop = () => _ttsStopAll();
    input.addEventListener('focusin', stop);
    input.addEventListener('input', stop);
    input.addEventListener('keydown', stop);
  }
  // Keep the floating pause button anchored: re-anchor on ANY scroll (capture
  // catches container scrolls too — scroll doesn't bubble) and on resize.
  document.addEventListener('scroll', () => { if (_ttsPauseBtn) _ttsRepositionPauseBtn(); }, true);
  window.addEventListener('resize', () => { if (_ttsPauseBtn) _ttsRepositionPauseBtn(); });
  // Switching sessions re-renders the transcript — loadSessionChat fires
  // 'tts:stop' before any load/switch, so a previous session's speech never
  // keeps playing (see session-load.js loadSessionChat).
  document.addEventListener('tts:stop', () => _ttsStopAll());
}
// type="module" scripts run before DOMContentLoaded, so this always fires
// after the composer exists (guarded anyway for other embed contexts).
document.addEventListener('DOMContentLoaded', _initTtsComposerStop);

async function _copyBubble(btn, bubble) {
  const text = _getBubbleCopyText(bubble);
  if (!text) return;
  try {
    // copyText handles insecure http://<ip> contexts (e.g. phones on the LAN),
    // where navigator.clipboard is undefined, via an execCommand fallback.
    await copyText(text);
    const origTitle = btn.title;
    btn.title = 'Copied!';
    btn.classList.add('copied');
    _setActionIcon(btn, 'check');
    _showActionNotice('Copied');
    setTimeout(() => {
      btn.title = origTitle;
      btn.classList.remove('copied');
      _setActionIcon(btn, 'copy');
    }, 1200);
  } catch (e) {
    console.warn('Copy failed:', e);
    _showActionNotice('Copy failed', true);
  }
}

function _toggleBubbleCollapse(btn, bubble) {
  const isCollapsed = bubble.classList.toggle('collapsed');
  btn.title = isCollapsed ? 'Expand message' : 'Collapse message';
  _setActionIcon(btn, isCollapsed ? 'chevron-right' : 'chevron-down');
}

// ── Per-turn delete (two-click confirm) ────────────────────────────────────

function _bubbleAnchorId(bubble) {
  return bubble && (bubble.getAttribute('data-msg-id') || bubble.getAttribute('data-turn-id'));
}

function _setDeleteIcon(btn, name) {
  btn.innerHTML = '<i data-lucide="' + name + '" style="width:14px;height:14px;"></i>';
  _renderActionIcons(btn);
}

function _resetBubbleDeleteBtn(btn) {
  if (!btn) return;
  btn.dataset.state = 'trash';
  btn.classList.remove('warning');
  btn.title = 'Delete this message';
  _setDeleteIcon(btn, 'trash-2');
}

function _resetAllBubbleDeleteBtns(except) {
  document.querySelectorAll('.bubble-delete-btn[data-state="warning"]').forEach((b) => {
    if (b !== except) _resetBubbleDeleteBtn(b);
  });
}

function _handleBubbleDeleteClick(btn, bubble) {
  if (btn.dataset.state === 'warning') {
    btn.dataset.state = 'deleting';
    const anchor = btn.dataset.deleteAnchor || _bubbleAnchorId(bubble);
    const section = btn.dataset.deleteSection ? 
      bubble.querySelector(`.turn-section.llm-section[data-section-idx="${btn.dataset.deleteSection}"]`) 
      : null;
    _deleteMessage(bubble, btn, anchor, section);
    return;
  }
  _resetAllBubbleDeleteBtns(btn);
  btn.dataset.state = 'warning';
  btn.classList.add('warning');
  btn.title = 'Click again to delete this message';
  _setDeleteIcon(btn, 'alert-triangle');
}

async function _deleteMessage(bubble, btn, anchorOverride, sectionEl) {
  const anchor = anchorOverride || _bubbleAnchorId(bubble);
  if (!anchor) { _resetBubbleDeleteBtn(btn); return; }
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  // Agent bubbles with tool calls: recycle the assistant interaction + its tool children.
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + '&include_children=' + (hasToolCalls ? 'true' : 'false')
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=user.db');
  try {
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.deleted_ids)) {
      console.warn('Delete message failed:', resp.status, data);
      _resetBubbleDeleteBtn(btn);
      if (resp.status === 404 || resp.status === 405) {
        alert('Couldn\'t delete this message: the server doesn\'t have this feature yet. Restart the app server and try again.');
      } else {
        alert('Couldn\'t delete this message (server responded ' + resp.status + ').');
      }
      return;
    }
    _showActionNotice('Message deleted');
    bubble.classList.remove('streaming', 'premium');

    if (sectionEl) {
      // ── Section-level delete (merged agent turn) ──
      sectionEl.classList.add('deleted');
      // Mark the gutter after this section as deleted too
      const gutter = sectionEl.nextElementSibling;
      if (gutter && gutter.classList.contains('turn-gutter')) {
        gutter.classList.add('deleted');
        _injectSectionDeletedActions(gutter, bubble, anchor);
      }
      // Only mark tool rows belonging to this interaction
      if (hasToolCalls) {
        bubble.querySelectorAll('.ca-tool-row').forEach(row => {
          if (row.dataset.detailMsgId === anchor) row.classList.add('deleted');
        });
      }
      // Only mark the whole bubble deleted if ALL sections are deleted
      const allSections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
      const allDeleted = allSections.length > 0 && 
        Array.from(allSections).every(s => s.classList.contains('deleted'));
      if (allDeleted) {
        bubble.classList.add('deleted');
        // Keep per-section restore buttons — they have the correct per-section anchors.
        // A bubble-level restore would only reference the last anchor.
      }
    } else {
      // ── Whole-bubble delete (user message or single-section agent) ──
      _injectDeletedActions(bubble, anchor);
      // Mark only the tool-call rows that belong to THIS interaction
      if (hasToolCalls) {
        bubble.querySelectorAll('.ca-tool-row').forEach(row => {
          if (!row.dataset.detailMsgId || row.dataset.detailMsgId === anchor) {
            row.classList.add('deleted');
          }
        });
        // Only mark the whole bubble deleted if ALL its tool rows are now deleted
        const remaining = bubble.querySelectorAll('.ca-tool-row:not(.deleted)');
        if (remaining.length === 0) {
          bubble.classList.add('deleted');
        }
      } else {
        bubble.classList.add('deleted');
      }
    }
    // Refresh grouping
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (e) {
    console.warn('Delete message error:', e);
    _resetBubbleDeleteBtn(btn);
    alert('Couldn\'t delete this message — no response from the server. Is the app server running?');
  }
}

/**
 * Inject restore + permanent-delete buttons into a deleted SECTION's gutter.
 */
function _injectSectionDeletedActions(gutter, bubble, anchor) {
  if (!gutter) return;
  const existing = gutter.querySelector('.section-deleted-actions');
  if (existing) existing.remove();

  const actions = document.createElement('span');
  actions.className = 'section-deleted-actions';
  actions.style.cssText = 'display:inline-flex;gap:3px;margin-left:auto;flex:0 0 auto;';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'turn-gutter-btn restore-btn';
  restoreBtn.title = 'Restore this message';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:14px;height:14px;"></i>';
  restoreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore';
      _setActionIcon(restoreBtn, 'alert-triangle');
      restoreBtn.classList.add('warning');
      const t = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this message';
        _setActionIcon(restoreBtn, 'undo-2');
        restoreBtn.classList.remove('warning');
      }, 4000);
      return;
    }
    _restoreSection(gutter, bubble, anchor);
  });
  actions.appendChild(restoreBtn);

  const permBtn = document.createElement('button');
  permBtn.type = 'button';
  permBtn.className = 'turn-gutter-btn';
  permBtn.title = 'Permanently delete this message';
  permBtn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  permBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (permBtn.dataset.state !== 'warning') {
      permBtn.dataset.state = 'warning';
      permBtn.title = 'Click again to permanently erase';
      _setActionIcon(permBtn, 'alert-triangle');
      permBtn.classList.add('warning');
      const t = setTimeout(() => {
        permBtn.dataset.state = '';
        permBtn.title = 'Permanently delete this message';
        _setActionIcon(permBtn, 'trash-2');
        permBtn.classList.remove('warning');
      }, 4000);
      return;
    }
    _permanentDeleteSection(gutter, bubble, anchor);
  });
  actions.appendChild(permBtn);

  gutter.appendChild(actions);
  _renderActionIcons(actions);
}

async function _restoreSection(gutter, bubble, anchor) {
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  try {
    const url = apiPath('/api/v1/db/interaction/restore?session_id=' + encodeURIComponent(sid)
      + '&interaction_id=' + encodeURIComponent(anchor)
      + '&include_children=' + (hasToolCalls ? 'true' : 'false')
      + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
      + '&db=user.db');
    const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
    if (resp.ok) {
      // Unmark the section before this gutter
      const section = gutter.previousElementSibling;
      if (section && section.classList.contains('turn-section')) {
        section.classList.remove('deleted');
      }
      gutter.classList.remove('deleted');
      gutter.querySelector('.section-deleted-actions')?.remove();
      // Unmark matching tool rows
      bubble.querySelectorAll('.ca-tool-row.deleted').forEach(row => {
        if (row.dataset.detailMsgId === anchor) row.classList.remove('deleted');
      });
      // Unmark bubble if not all sections are deleted
      bubble.classList.remove('deleted');
      bubble.querySelector('.bubble-actions.deleted-actions')?.remove();
    }
  } catch (_) {}
}

async function _permanentDeleteSection(gutter, bubble, anchor) {
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  try {
    const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
      + '&interaction_id=' + encodeURIComponent(anchor)
      + '&include_children=' + (hasToolCalls ? 'true' : 'false')
      + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
      + '&db=user.db&permanent=true');
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    if (resp.ok) {
      // Remove the section
      const section = gutter.previousElementSibling;
      if (section && section.classList.contains('turn-section')) section.remove();
      gutter.remove();
      // Remove matching tool rows
      bubble.querySelectorAll('.ca-tool-row').forEach(row => {
        if (row.dataset.detailMsgId === anchor) row.remove();
      });
      // If no sections remain, remove the bubble
      const remaining = bubble.querySelectorAll(':scope > .turn-section.llm-section');
      if (remaining.length === 0) bubble.remove();
    }
  } catch (_) {}
}

/**
 * Replace a deleted bubble's action row with restore + permanent-delete buttons.
 * Both use the two-click hazard confirm pattern.
 */
function _injectDeletedActions(bubble, anchor) {
  if (!bubble || !anchor) return;
  const existing = bubble.querySelector(':scope > .bubble-actions');
  if (existing) existing.remove();

  const actions = document.createElement('div');
  actions.className = 'bubble-actions deleted-actions';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'bubble-action-btn restore-btn';
  restoreBtn.title = 'Restore this message';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:14px;height:14px;"></i>';
  restoreBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      _resetAllBubbleDeleteBtns(null);
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore this message';
      _setActionIcon(restoreBtn, 'alert-triangle');
      restoreBtn.classList.add('warning');
      const timer = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore this message';
        _setActionIcon(restoreBtn, 'undo-2');
        restoreBtn.classList.remove('warning');
      }, 4000);
      restoreBtn._timer = timer;
      return;
    }
    if (restoreBtn._timer) { clearTimeout(restoreBtn._timer); restoreBtn._timer = null; }
    _restoreMessage(bubble, restoreBtn);
  });
  actions.appendChild(restoreBtn);

  const permDeleteBtn = document.createElement('button');
  permDeleteBtn.type = 'button';
  permDeleteBtn.className = 'bubble-action-btn bubble-delete-btn';
  permDeleteBtn.title = 'Permanently delete this message';
  permDeleteBtn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  permDeleteBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (permDeleteBtn.dataset.state !== 'warning') {
      _resetAllBubbleDeleteBtns(null);
      permDeleteBtn.dataset.state = 'warning';
      permDeleteBtn.title = 'Click again to permanently erase this message';
      _setActionIcon(permDeleteBtn, 'alert-triangle');
      permDeleteBtn.classList.add('warning');
      const timer = setTimeout(() => {
        permDeleteBtn.dataset.state = '';
        permDeleteBtn.title = 'Permanently delete this message';
        _setActionIcon(permDeleteBtn, 'trash-2');
        permDeleteBtn.classList.remove('warning');
      }, 4000);
      permDeleteBtn._timer = timer;
      return;
    }
    if (permDeleteBtn._timer) { clearTimeout(permDeleteBtn._timer); permDeleteBtn._timer = null; }
    _permanentDeleteMessage(bubble, permDeleteBtn);
  });
  actions.appendChild(permDeleteBtn);

  bubble.appendChild(actions);
  _renderActionIcons(actions);
}

async function _restoreMessage(bubble, btn) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  const url = apiPath('/api/v1/db/interaction/restore?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + '&include_children=' + (hasToolCalls ? 'true' : 'false')
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=user.db');
  try {
    const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.restored_ids)) {
      console.warn('Restore message failed:', resp.status, data);
      alert('Could not restore this message (server responded ' + resp.status + ').');
      return;
    }
    _showActionNotice('Message restored');
    bubble.classList.remove('deleted');
    bubble.querySelector('.bubble-actions')?.remove();
    // Unmark all sections and section gutters within this bubble
    bubble.querySelectorAll('.turn-section.llm-section.deleted').forEach(s => s.classList.remove('deleted'));
    bubble.querySelectorAll('.turn-gutter.deleted').forEach(g => {
      g.classList.remove('deleted');
      g.querySelector('.section-deleted-actions')?.remove();
    });
    // Unmark only the tool-call rows that belong to THIS interaction
    bubble.querySelectorAll('.ca-tool-row.deleted').forEach(row => {
      if (!row.dataset.detailMsgId || row.dataset.detailMsgId === anchor) {
        row.classList.remove('deleted');
      }
    });
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
  } catch (e) {
    console.warn('Restore message error:', e);
    alert('Could not restore this message — no response from the server.');
  }
}

async function _permanentDeleteMessage(bubble, btn) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  const hasToolCalls = bubble.classList.contains('agent') && bubble.querySelector('.bubble-tool-calls');
  const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
    + '&interaction_id=' + encodeURIComponent(anchor)
    + '&include_children=' + (hasToolCalls ? 'true' : 'false')
    + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
    + '&db=user.db&permanent=true');
  try {
    const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !Array.isArray(data.deleted_ids)) {
      console.warn('Permanent delete message failed:', resp.status, data);
      alert('Could not permanently delete this message (server responded ' + resp.status + ').');
      return;
    }
    _showActionNotice('Message permanently deleted');
    bubble.remove();
    if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (e) {
    console.warn('Permanent delete message error:', e);
    alert('Could not permanently delete this message — no response from the server.');
  }
}

function _makeBubbleDeleteBtn(bubble, anchorOverride, sectionIdx) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-delete-btn';
  btn.dataset.state = 'trash';
  btn.title = 'Delete this message';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:14px;height:14px;"></i>';
  if (anchorOverride) btn.dataset.deleteAnchor = anchorOverride;
  if (sectionIdx != null) btn.dataset.deleteSection = String(sectionIdx);
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _handleBubbleDeleteClick(btn, bubble);
  });
  return btn;
}

// Clicking anywhere that isn't an armed delete button disarms them all.
document.addEventListener('click', (e) => {
  if (!e.target.closest || !e.target.closest('.bubble-delete-btn')) {
    _resetAllBubbleDeleteBtns(null);
  }
}, true);

// ── Undo bubble (delete from this message forward, put text in composer) ──

// Interactive choice panel shown when the composer already has text before an
// undo operation. Reuses the wa-confirm-overlay / wa-confirm-panel structure
// from confirm-dialog.js but neutral-styled — no hazard icon or tone class.
// Returns a Promise<string> resolving to 'replace', 'keep', or 'append'.
function _showComposerChoicePanel(undoText) {
  const preview = undoText.length > 80 ? undoText.slice(0, 77) + '…' : undoText;
  return new Promise((resolve) => {
    const overlay = document.createElement('div');
    overlay.className = 'wa-confirm-overlay';
    overlay.setAttribute('role', 'dialog');
    overlay.setAttribute('aria-modal', 'true');

    const panel = document.createElement('div');
    panel.className = 'wa-confirm-panel';
    panel.innerHTML = `
      <div class="wa-confirm-head">
        <div class="wa-confirm-title">Composer has content</div>
      </div>
      <div class="wa-confirm-msg">The composer already has text. How should the undone message be handled?</div>
      <div class="wa-confirm-preview" style="font-size:12px;color:var(--fg-muted);margin:8px 0;padding:8px;border-radius:6px;background:var(--bg-1);max-height:72px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">Undo text: “${preview}”</div>
      <div class="wa-confirm-actions" style="flex-wrap:wrap;gap:6px;">
        <button type="button" class="ac-btn ac-btn-ghost wa-undo-clear">Clear &amp; replace</button>
        <button type="button" class="ac-btn ac-btn-ghost wa-undo-keep">Keep current</button>
        <button type="button" class="ac-btn ac-btn-primary wa-undo-append">Append</button>
      </div>`;

    const clearBtn = panel.querySelector('.wa-undo-clear');
    const keepBtn  = panel.querySelector('.wa-undo-keep');
    const appendBtn = panel.querySelector('.wa-undo-append');

    overlay.appendChild(panel);
    document.body.appendChild(overlay);
    requestAnimationFrame(() => overlay.classList.add('show'));

    let settled = false;
    const close = (result) => {
      if (settled) return;
      settled = true;
      document.removeEventListener('keydown', onKey, true);
      overlay.classList.remove('show');
      setTimeout(() => overlay.remove(), 160);
      resolve(result);
    };

    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); close('keep'); }
    }

    clearBtn.addEventListener('click', () => close('replace'));
    keepBtn.addEventListener('click', () => close('keep'));
    appendBtn.addEventListener('click', () => close('append'));
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close('keep'); });
    document.addEventListener('keydown', onKey, true);

    keepBtn.focus();
  });
}

async function _undoBubble(bubble) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';

  // Extract the user message text
  const userText = _getBubbleText(bubble);

  // If composer already has content, let the user decide what to do
  const hasExisting = app.chatInput && app.chatInput.value.trim();
  let composerAction = 'replace';
  if (hasExisting) {
    composerAction = await _showComposerChoicePanel(userText);
  }

  // Collect all sibling bubbles from this one forward in the DOM
  const toDelete = [];
  let next = bubble;
  while (next) {
    const id = _bubbleAnchorId(next);
    if (id) toDelete.push({ el: next, id, isAgent: next.classList.contains('agent') });
    next = next.nextElementSibling;
  }

  // Soft-delete each interaction via the API (recycle bin)
  const failed = [];
  for (const item of toDelete) {
    const hasToolCalls = item.isAgent && item.el.querySelector('.bubble-tool-calls');
    const url = apiPath('/api/v1/db/interaction?session_id=' + encodeURIComponent(sid)
      + '&interaction_id=' + encodeURIComponent(item.id)
      + '&include_children=' + (hasToolCalls ? 'true' : 'false')
      + (uid ? '&user_id=' + encodeURIComponent(uid) : '')
      + '&db=user.db');
    try {
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (!resp.ok) {
        console.warn('Undo delete failed for', item.id, resp.status);
        failed.push(item.id);
      }
    } catch (e) {
      console.warn('Undo delete error for', item.id, e);
      failed.push(item.id);
    }
  }

  if (failed.length === toDelete.length && toDelete.length > 0) {
    _showActionNotice('Undo failed', true);
    return;
  }

  // Remove deleted bubbles from the DOM
  for (const item of toDelete) {
    item.el.remove();
  }

  // Fill composer based on the user's choice
  if (composerAction !== 'keep' && app.chatInput) {
    if (composerAction === 'replace') {
      app.chatInput.value = userText;
    } else if (composerAction === 'append') {
      app.chatInput.value = app.chatInput.value.trim() + '\n' + userText;
    }
    app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
    app.chatInput.focus();
  }

  _showActionNotice('Messages undone');

  // Refresh grouping and session list
  if (typeof app._regroupBubbles === 'function') { try { app._regroupBubbles(); } catch (_) {} }
  if (typeof app.populateSessionSelect === 'function') {
    try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
  }
}

function _makeBubbleUndoBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-undo-btn';
  btn.title = 'Undo from this message';
  btn.innerHTML = '<i data-lucide="undo-2" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _undoBubble(bubble);
  });
  return btn;
}

// ── Fork bubble (fork session at this message) ─────────────────────────────
// Agent bubbles only — the fork button lives in the agent turn gutter. User
// bubbles keep the Undo button instead (see _buildUserTurnGutter).

async function _forkBubble(bubble) {
  const anchor = _bubbleAnchorId(bubble);
  if (!anchor) return;
  const sid = app.currentSessionId;
  const uid = app.currentUserId || '';
  try {
    const resp = await fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '/fork?db=user.db'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ up_to_interaction_id: anchor, user_id: uid }),
    });
    const data = await resp.json().catch(() => null);
    if (!resp.ok || !data || !data.session_id) {
      console.warn('Fork failed:', resp.status, data);
      alert('Couldn\'t fork session at this message (server responded ' + resp.status + ').');
      return;
    }
    _showActionNotice('Session forked');
    // Switch to the new forked session
    const { switchToSession } = await import('./session-core.js?v=515');
    await switchToSession(data.session_id);
  } catch (e) {
    console.warn('Fork error:', e);
    alert('Couldn\'t fork session — no response from the server.');
  }
}

function _makeBubbleForkBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-fork-btn';
  btn.title = 'Fork session at this message';
  btn.innerHTML = '<i data-lucide="git-branch-plus" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _forkBubble(bubble);
  });
  return btn;
}

// ── Schema inspect (3-line icon — view the full schema sent to the LLM) ────────

function _hasSentSchema(bubble) {
  if (!bubble || !bubble.classList.contains('agent')) return false;
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return false;
  // The slimmed output on initial load sets _has_sent_schema=true when the
  // full turn detail has a _sent_messages snapshot to fetch.
  try {
    const raw = bubble.getAttribute('data-output');
    if (!raw) return true; // unknown → show the button, let fetch decide
    const o = JSON.parse(raw);
    return !!o._has_sent_schema;
  } catch (_) { return true; }
}

function _toggleSchemaBubble(bubble) {
  const existing = bubble._schemaBubble;
  if (existing) {
    existing.remove();
    bubble._schemaBubble = null;
    bubble.classList.remove('schema-inspect-open');
    return;
  }
  _fetchAndShowSchema(bubble);
}

async function _fetchAndShowSchema(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return;
  const sid = app.currentSessionId;
  if (!sid) return;
  const url = apiPath(`/api/v1/db/session-turn-detail?db=user.db&session_id=${encodeURIComponent(sid)}&ids=${encodeURIComponent(msgId)}`);
  let data;
  try {
    const resp = await fetch(url, { headers: { ...authHeaders() } });
    data = await resp.json().catch(() => null);
    if (!resp.ok || !data) throw new Error('fetch failed');
  } catch (_) {
    return; // silently fail — don't block the UI
  }
  const detail = data?.details?.[msgId];
  if (!detail) return;
  let output;
  try { output = JSON.parse(detail.output || '{}'); } catch (_) { output = {}; }
  const messages = output._sent_messages;
  if (!messages || !messages.length) return;
  _renderSchemaBubble(bubble, messages, output);
}

function _renderSchemaBubble(bubble, messages, output) {
  // Build the schema preview bubble — rendered above the agent bubble,
  // styled as a right-aligned technical overlay with a monospace code block.
  const schemaBubble = document.createElement('div');
  schemaBubble.className = 'chat-bubble schema-inspect-bubble';

  const header = document.createElement('div');
  header.className = 'schema-inspect-header';
  header.innerHTML = '<span class="schema-inspect-title">Sent to LLM</span>';
  const closeBtn = document.createElement('button');
  closeBtn.type = 'button';
  closeBtn.className = 'schema-inspect-close';
  closeBtn.innerHTML = '&times;';
  closeBtn.title = 'Close';
  closeBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    schemaBubble.remove();
    bubble._schemaBubble = null;
    bubble.classList.remove('schema-inspect-open');
  });
  header.appendChild(closeBtn);
  schemaBubble.appendChild(header);

  // Build a display of the messages + tools in a readable format
  const body = document.createElement('div');
  body.className = 'schema-inspect-body';

  // System prompt is often huge — collapse it by default
  let hasSystem = false;
  for (const msg of messages) {
    if (msg.role === 'system') hasSystem = true;
    const block = document.createElement('div');
    block.className = 'schema-msg';

    const roleLabel = document.createElement('span');
    roleLabel.className = 'schema-msg-role';
    roleLabel.textContent = msg.role.toUpperCase();
    block.appendChild(roleLabel);

    if (msg.tool_calls) {
      const tcs = document.createElement('span');
      tcs.className = 'schema-msg-tool-calls';
      tcs.textContent = ' [tool_calls: ' + msg.tool_calls.map(tc => tc.function?.name || '?').join(', ') + ']';
      block.appendChild(tcs);
    } else if (msg.role === 'system' && msg.content && msg.content.length > 800) {
      const details = document.createElement('details');
      details.className = 'schema-system-fold';
      const summary = document.createElement('summary');
      summary.textContent = msg.content.slice(0, 200).replace(/\n/g, ' ') + '…';
      details.appendChild(summary);
      const pre = document.createElement('pre');
      pre.textContent = msg.content;
      details.appendChild(pre);
      block.appendChild(details);
    } else if (msg.content) {
      const pre = document.createElement('pre');
      pre.textContent = typeof msg.content === 'string' ? msg.content : JSON.stringify(msg.content, null, 2);
      block.appendChild(pre);
    }
    body.appendChild(block);
  }

  // Tool definitions
  const tools = output._sent_tools;
  if (tools && tools.length > 0) {
    const toolsBlock = document.createElement('div');
    toolsBlock.className = 'schema-msg';
    const toolsLabel = document.createElement('span');
    toolsLabel.className = 'schema-msg-role';
    toolsLabel.textContent = 'TOOLS (' + tools.length + ')';
    toolsBlock.appendChild(toolsLabel);
    const details = document.createElement('details');
    details.className = 'schema-system-fold';
    const summary = document.createElement('summary');
    summary.textContent = tools.map(t => t.function?.name || '?').join(', ');
    details.appendChild(summary);
    const pre = document.createElement('pre');
    pre.textContent = JSON.stringify(tools, null, 2);
    details.appendChild(pre);
    toolsBlock.appendChild(details);
    body.appendChild(toolsBlock);
  }

  schemaBubble.appendChild(body);
  bubble.parentNode.insertBefore(schemaBubble, bubble);
  bubble._schemaBubble = schemaBubble;
  bubble.classList.add('schema-inspect-open');
}

function _makeBubbleSchemaBtn(bubble) {
  if (!_hasSentSchema(bubble)) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn bubble-schema-btn';
  btn.title = 'View schema sent to LLM';
  btn.innerHTML = '<i data-lucide="menu" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    _toggleSchemaBubble(bubble);
  });
  return btn;
}

function _makeGutterTime(bubble) {
  const createdAtMs = bubble.getAttribute('data-created-at');
  const timeError = bubble.getAttribute('data-time-error');
  if (!createdAtMs && !(app.isDebug && timeError)) return null;
  const timeEl = document.createElement('span');
  timeEl.className = 'turn-gutter-time bubble-time';
  if (createdAtMs) {
    timeEl.setAttribute('data-created-at', createdAtMs);
    timeEl.textContent = _formatRelativeTime(Number(createdAtMs));
  } else {
    timeEl.setAttribute('data-time-error', timeError);
    timeEl.title = timeError;
    timeEl.textContent = `time error: ${timeError}`;
  }
  return timeEl;
}

function _makeGutterFullViewBtn(source) {
  if (!source) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'turn-gutter-btn';
  btn.title = 'Open full view';
  btn.setAttribute('aria-label', 'Open full view');
  btn.innerHTML = '<i data-lucide="maximize-2" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (event) => {
    event.stopPropagation();
    openChatElementFullView(source, btn, 'Full message');
  });
  return btn;
}

function _makeGutterCopyBtn(bubble) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'turn-gutter-btn';
  btn.title = 'Copy text';
  btn.innerHTML = '<i data-lucide="copy" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (event) => {
    event.stopPropagation();
    _copyBubble(btn, bubble);
  });
  return btn;
}

// Build a gutter for user bubbles — time, copy, undo, delete (no model/speak/collapse/schema).
function _buildUserTurnGutter(bubble) {
  const gutter = document.createElement('div');
  gutter.className = 'turn-gutter';
  gutter.dataset.sectionIdx = '0';
  const anchor = _bubbleAnchorId(bubble);
  const spec = _messageGuttersConfig().user;
  _appendConfiguredGutterControls(gutter, spec, {
    time: () => _makeGutterTime(bubble),
    full_view: () => _makeGutterFullViewBtn(
      bubble.querySelector(':scope > .turn-section.llm-section') || bubble,
    ),
    copy: () => _makeGutterCopyBtn(bubble),
    undo: () => anchor ? _makeBubbleUndoBtn(bubble) : null,
    delete: () => anchor ? _makeBubbleDeleteBtn(bubble) : null,
    more: () => _makeBubbleMoreBtn(gutter, bubble),
  });

  _renderActionIcons(gutter);
  return gutter;
}

// Build a gutter for system/update notice bubbles (info class — compaction
// notices, slash results, recovery notices, errors, genui labels). Carries the
// SAME action set as the standard footers — time, copy, delete (when the row
// has an anchor), and the ⋮ more menu with the per-message context readout —
// so notices behave like every other bubble. Only Undo (user-specific: fills
// the composer with the message text) and the agent-only actions
// (speak/collapse/fork/schema/model) are omitted. Notices are a single block
// with no sections to toggle, so the footer is always visible.
function _buildSystemNoticeGutter(bubble) {
  const gutter = document.createElement('div');
  // 'notice-gutter' marks this as an ALWAYS-VISIBLE footer: the global
  // click-to-close handler skips it (see the document click listener below), so
  // clicking the notice body can never hide its own actions/context menu.
  gutter.className = 'turn-gutter notice-gutter';

  // Time
  const createdAtMs = bubble.getAttribute('data-created-at');
  const timeError = bubble.getAttribute('data-time-error');
  if (createdAtMs || (app.isDebug && timeError)) {
    const timeEl = document.createElement('span');
    timeEl.className = 'turn-gutter-time bubble-time';
    if (createdAtMs) {
      timeEl.setAttribute('data-created-at', createdAtMs);
      timeEl.textContent = _formatRelativeTime(Number(createdAtMs));
    } else {
      timeEl.setAttribute('data-time-error', timeError);
      timeEl.title = timeError;
      timeEl.textContent = `time error: ${timeError}`;
    }
    gutter.appendChild(timeEl);
  }

  // Copy
  const copyBtn = document.createElement('button');
  copyBtn.type = 'button';
  copyBtn.className = 'turn-gutter-btn';
  copyBtn.title = 'Copy text';
  copyBtn.innerHTML = '<i data-lucide="copy" style="width:14px;height:14px;"></i>';
  copyBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _copyBubble(copyBtn, bubble);
  });
  gutter.appendChild(copyBtn);

  // Delete (two-click confirm) — only when the row carries an anchor, exactly
  // like user/agent bubbles.
  if (_bubbleAnchorId(bubble)) gutter.appendChild(_makeBubbleDeleteBtn(bubble));

  // More (⋮) — context readout menu
  gutter.appendChild(_makeBubbleMoreBtn(gutter, bubble));

  _renderActionIcons(gutter);
  return gutter;
}

// ── Per-bubble "more" menu (⋮) ────────────────────────────────────────────
// A three-dot button at the end of the footer opens a small popover whose
// Context row shows the SAME ctx readout as the pill's stats strip
// (chat-activity.js _renderCtxIndicator) — exact token counts, the model's
// window, and live updates while the menu is open.

let _openMoreMenu = null;   // the currently open .bubble-more-menu (or null)

function _closeBubbleMoreMenu() {
  if (_openMoreMenu) {
    if (_openMoreMenu._anchorBtn) _openMoreMenu._anchorBtn.classList.remove('open');
    _openMoreMenu.remove();
    _openMoreMenu = null;
  }
}

// Format a context count for the more menu: x.xk below 1M (1 decimal), x.xxM
// at/above 1M (2 decimals) — mirrors chat-activity.js _fmtCtxNum so the row
// reads identically to the pill.
function _fmtBubbleCtx(n) {
  if (!n || typeof n !== 'number') return '';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(2)}M`;
  return `${(n / 1000).toFixed(1)}k`;
}

// Render the Context row for the menu on the given bubble. Per-message: shows
// the ACTUAL provider prompt size behind THIS message (its own LLM call), from
// the server's per-message `context_tokens` — so an old bubble keeps the
// context it was answered under, and a Summary-lane / compaction message shows
// the size of the SUMMARISER's own prompt (the folded span + instructions), not
// the session's full context. When the ledger has no per-message row, the row
// shows an explicit n/a — it never inherits the session-latest readout (that
// value describes the session window, not this message).
function _renderMoreMenuCtx(menu, bubble) {
  const row = menu && menu.querySelector('.bubble-more-row[data-ctx-row]');
  if (!row) return;
  let s = {};
  try {
    s = (typeof app.getContextStats === 'function') ? (app.getContextStats() || {}) : {};
  } catch (_) {}
  const maxNum = s.maxLabel || '';
  const maxRaw = s.max || 0;

  let perMsg = null;
  let perMsgNote = '';
  let msgSrc = '';
  const mid = bubble ? _bubbleAnchorId(bubble) : null;
  if (mid && typeof app.getMessageById === 'function') {
    try {
      const m = app.getMessageById(String(mid));
      if (m) {
        msgSrc = m.source || '';
        if (typeof m.context_tokens === 'number' && m.context_tokens > 0) {
          perMsg = m.context_tokens;
          if (msgSrc === 'system:summary' || msgSrc === 'system:overview' || msgSrc === 'system:closer' || msgSrc === 'system:compaction') {
            perMsgNote = 'closer prompt';
          }
        }
      }
    } catch (_) {}
  }

  const val = row.querySelector('.bubble-more-value');
  if (perMsg != null) {
    const num = _fmtBubbleCtx(perMsg);
    val.innerHTML = (num || maxNum)
      ? `<span class="chat-token-label">ctx</span> ${num || '—'} <span class="chat-ctx-sep">/</span> ${maxNum || '—'}`
      : '—';
    row.classList.remove('bubble-more-pollable');
    row.title = perMsgNote
      ? `${perMsgNote} — ctx ${perMsg.toLocaleString()} / max ${maxRaw ? maxRaw.toLocaleString() : '?'} (this message's summary was generated from its own prompt, not the session context)`
      : `${s.model || '??'} — ctx ${perMsg.toLocaleString()} / max ${maxRaw ? maxRaw.toLocaleString() : '?'} (context when this message was generated)`;
    return;
  }

  // No per-message context recorded (system notices have no model prompt of
  // their own; live-streamed rows may not be adopted into the cache yet). Show
  // an explicit n/a rather than inheriting the session-latest value.
  val.innerHTML = maxNum
    ? `<span class="chat-token-label">ctx</span> n/a <span class="chat-ctx-sep">/</span> ${maxNum}`
    : '<span class="chat-token-label">ctx</span> n/a';
  row.title = (msgSrc === 'system:summary' || msgSrc === 'system:overview' || msgSrc === 'system:closer' || msgSrc === 'system:compaction')
    ? 'No closer usage row is linked to this close-out — the closer prompt size is unavailable.'
    : 'No per-message context recorded for this message — it did not consume a model prompt (or no usage row is linked).';
  _makeMoreMenuPollable(row, bubble);
}

// Format a USD amount for the Cost row in CENTS with 3-decimal precision so
// sub-cent calls read precisely (e.g. $0.00346 → 0.346¢): rounds UP to the
// 3rd decimal of a cent so a real charge never displays as a misleading zero,
// and hides zero/unknown values (the row then shows n/a, never 0.000¢).
function _fmtBubbleCost(usd) {
  const v = Number(usd);
  if (!isFinite(v) || v <= 0) return '';
  return (Math.ceil(v * 100 * 1000) / 1000).toFixed(3) + '¢';
}

// Render the Cost row for the menu on the given bubble. Per-message: shows the
// locked-in cost of the ACTUAL LLM call behind THIS message, priced at that
// call's own model rate (roster model × the catalog's published $/1M — the
// same "calculator" price the usage ledger and the session cost chip use), so
// an old bubble keeps the cost it was answered under and a mid-session model
// switch never re-prices earlier messages. Priority: the server's per-message
// `cost_usd` enrichment (usage_events row matched to this message's turn), then
// the row metadata's recorded `cost` (live-streamed rows). No linked call →
// explicit n/a, mirroring the Context row.
function _renderMoreMenuCost(menu, bubble) {
  const row = menu && menu.querySelector('.bubble-more-row[data-cost-row]');
  if (!row) return;
  const val = row.querySelector('.bubble-more-value');
  const mid = bubble ? _bubbleAnchorId(bubble) : null;
  let costUsd = null;
  let source = '';
  if (mid && typeof app.getMessageById === 'function') {
    try {
      const m = app.getMessageById(String(mid));
      if (m) {
        if (typeof m.cost_usd === 'number' && m.cost_usd > 0) {
          costUsd = m.cost_usd;
          source = 'usage';
        } else {
          let meta = m.metadata;
          if (typeof meta === 'string') { try { meta = JSON.parse(meta); } catch (_) { meta = null; } }
          if (meta && typeof meta.cost === 'number' && meta.cost > 0) {
            costUsd = meta.cost;
            source = 'meta';
          }
        }
      }
    } catch (_) {}
  }
  const text = _fmtBubbleCost(costUsd);
  if (text) {
    val.textContent = text;
    row.classList.remove('bubble-more-pollable');
    row.title = source === 'usage'
      ? `Cost of this message's LLM call — ${text} (published $/1M rate × this call's input/output tokens at its model, locked in when it ran)`
      : `Cost of this message's LLM call — ${text} (recorded at run time)`;
    return;
  }
  val.textContent = 'n/a';
  row.title = 'No cost recorded for this message — its LLM call has no linked usage row (or the model has no published price).';
  _makeMoreMenuPollable(row, bubble);
}

// ── Click-to-poll for n/a rows ─────────────────────────────────────────────
// A row showing n/a may be a stale cache, a late ledger write, or a row whose
// usage enrichment landed after the cache was populated. Making the row
// clickable re-asks the server for THAT message's usage
// (app.refreshMessageUsage refetches around the id and updates the in-memory
// cache), then re-renders the ctx / cost / model rows so a landed value
// appears without reopening the menu. Genuinely usage-less messages (system
// notices with no LLM call) confirm n/a — the tooltip already says so.

let _pollingMessageId = null;

function _makeMoreMenuPollable(row, bubble) {
  if (!row || !bubble) return;
  const mid = _bubbleAnchorId(bubble);
  if (!mid || typeof app.refreshMessageUsage !== 'function') return;
  if (!row.dataset.pollWired) {
    row.dataset.pollWired = '1';
    row.addEventListener('click', (e) => {
      e.stopPropagation();
      _pollMoreMenuRow(row, bubble);
    });
  }
  // Re-applied on every render so a fresh tooltip keeps the hint (the render
  // functions rewrite row.title each pass; only append it once).
  if (row.title.indexOf('Click to re-check') === -1) {
    row.title = (row.title || '') + ' Click to re-check with the server.';
  }
  row.classList.add('bubble-more-pollable');
}

async function _pollMoreMenuRow(row, bubble) {
  const mid = _bubbleAnchorId(bubble);
  if (!mid || _pollingMessageId === String(mid)) return;
  _pollingMessageId = String(mid);
  const val = row.querySelector('.bubble-more-value');
  if (val) val.textContent = '…';
  row.classList.add('bubble-more-polling');
  row.classList.remove('bubble-more-pollable');
  try {
    await app.refreshMessageUsage(String(mid));
  } catch (_) { /* the re-render below restores the honest n/a */ }
  // Whatever the server says, re-render all three rows so a landed value shows.
  const menu = row.closest('.bubble-more-menu');
  if (menu && menu.isConnected) {
    _renderMoreMenuCtx(menu, bubble);
    _renderMoreMenuCost(menu, bubble);
    _renderMoreMenuModel(menu, bubble);
  }
  row.classList.remove('bubble-more-polling');
  _pollingMessageId = null;
}

// Resolve the model details behind a bubble, in priority order:
//   1. the bubble's own stamp (_setBubbleModel — live agent bubbles);
//   2. the enclosing activity-group bubble's stamp (live update rows);
//   3. the per-message metadata in the cache (persisted rows of any kind —
//      agent replies AND tool-panel update rows);
//   4. for a USER bubble, the model of the NEXT assistant message (the answer
//      that consumed it — the same inheritance the Context row uses).
// Returns { model, effort, displayName } — model may be '' when nothing is
// known; the row then shows '—' (never the session's current model: that
// belongs to the session, not to this message). displayName is the roster
// alias recorded on the message, '' when that row had none.
function _resolveBubbleModel(bubble) {
  let model = '';
  let effort = '';
  let displayName = '';
  let cached = null;
  const fromMeta = (m) => {
    if (!m || !m.metadata) return;
    let meta = m.metadata;
    if (typeof meta === 'string') { try { meta = JSON.parse(meta); } catch (_) { meta = null; } }
    if (meta && typeof meta === 'object' && meta.model) {
      if (!model) model = String(meta.model);
      if (!effort) effort = meta.effort || '';
      // The alias this turn's roster row carried. The server stores the real
      // model id in display_name when the row had no alias, so only a value
      // that differs from the id counts as a display name.
      const alias = String(meta.display_name || '').trim();
      if (!displayName && alias && alias !== String(meta.model).trim()) displayName = alias;
    }
  };
  if (bubble) {
    model = bubble.dataset.modelTitle || '';
    displayName = bubble.dataset.modelDisplayName || '';
    if (!model && bubble.dataset.modelLabel) model = bubble.dataset.modelLabel;
    if (!model && bubble.closest) {
      const host = bubble.closest('.chat-bubble');
      if (host && host !== bubble) {
        model = host.dataset.modelTitle || '';
        displayName = host.dataset.modelDisplayName || displayName;
        if (!model && host.dataset.modelLabel) model = host.dataset.modelLabel;
      }
    }
  }
  const mid = bubble ? _bubbleAnchorId(bubble) : null;
  if (mid && typeof app.getMessageById === 'function') {
    try {
      cached = app.getMessageById(String(mid));
      fromMeta(cached);
      // A terminal closer is its own persisted system row. Newer rows record the
      // caller's display name directly; older ones carry only the raw model id.
      // Inherit the parent's saved name ONLY when the parent ran on the same
      // model, so a closer that ran on a different roster model is never
      // mislabelled, and never adopt the parent's model or effort.
      if (!displayName && cached && cached.parent_id) {
        const parent = app.getMessageById(String(cached.parent_id));
        let pm = parent && parent.metadata;
        if (typeof pm === 'string') { try { pm = JSON.parse(pm); } catch (_) { pm = null; } }
        if (pm && typeof pm === 'object' && pm.model && String(pm.model) === String(model)) {
          const alias = String(pm.display_name || '').trim();
          if (alias && alias !== String(pm.model).trim()) displayName = alias;
        }
      }
    } catch (_) {}
  }
  if (!model && bubble && bubble.classList.contains('user')
      && mid && typeof app.getSessionMessages === 'function') {
    try {
      const msgs = app.getSessionMessages() || [];
      const i = msgs.findIndex(m => m && String(m.id) === String(mid));
      for (let k = i + 1; k < msgs.length; k++) {
        if (msgs[k] && msgs[k].role === 'assistant') { fromMeta(msgs[k]); break; }
      }
    } catch (_) {}
  }
  return { model, effort, displayName };
}

// Render the Model row for the menu: roster position (Standard / Premium /
// Vision / Image / Custom N — resolved from the same slot list the pill's
// model picker uses) plus the model's DISPLAY NAME (its roster alias), falling
// back to the real model name only when the row carries no alias — e.g.
// "Standard · V4-Flash". The provider is always the WebAgent Catalog: the real
// upstream provider is never presented on these bubbles. The row is SHARED
// across every bubble type (agent replies, user messages, tool-panel updates,
// notices) because they all open the same ⋮ menu.
function _renderMoreMenuModel(menu, bubble) {
  const row = menu && menu.querySelector('.bubble-more-row[data-model-row]');
  if (!row) return;
  const val = row.querySelector('.bubble-more-value');
  const { model, effort, displayName } = _resolveBubbleModel(bubble);
  // Display name when the roster row has one, else the real model name.
  const name = _publicModelLabel(model, displayName);
  if (!name) {
    val.textContent = '—';
    row.title = 'No model recorded for this message.';
    return;
  }
  let pos = '';
  try {
    const info = (typeof app.getModelRosterInfo === 'function') ? app.getModelRosterInfo(model) : null;
    pos = (info && info.position) ? info.position : '';
  } catch (_) {}
  const eff = (effort || '').trim().toLowerCase();
  val.textContent = (pos ? pos + ' · ' : '') + name;
  // Provider attribution is always the WebAgent Catalog — neither the real
  // upstream provider nor the backend model id is ever named here.
  row.title = (pos ? pos + ' · ' : '') + 'WebAgent Catalog · ' + name
    + (eff && eff !== 'default' ? ' (effort ' + eff + ')' : '');
}

function _positionBubbleMoreMenu(menu, anchorBtn) {
  const r = anchorBtn.getBoundingClientRect();
  const mw = menu.offsetWidth || 240;
  const mh = menu.offsetHeight || 120;
  const left = Math.min(Math.max(6, r.right - mw), window.innerWidth - mw - 6);
  let top = r.bottom + 6;
  if (top + mh > window.innerHeight - 6) top = Math.max(6, r.top - mh - 6);
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

function _openBubbleMoreMenu(btn, bubble) {
  _closeBubbleMoreMenu();
  const menu = document.createElement('div');
  menu.className = 'bubble-more-menu';
  menu.setAttribute('role', 'menu');

  const title = document.createElement('div');
  title.className = 'bubble-more-title';
  title.textContent = 'More';
  menu.appendChild(title);

  const ctxRow = document.createElement('div');
  ctxRow.className = 'bubble-more-row';
  ctxRow.dataset.moreControl = 'context';
  ctxRow.dataset.ctxRow = '1';
  ctxRow.setAttribute('role', 'menuitem');
  ctxRow.setAttribute('aria-disabled', 'true');
  const label = document.createElement('span');
  label.className = 'bubble-more-label';
  label.innerHTML = '<i data-lucide="gauge" style="width:13px;height:13px;"></i><span>Context</span>';
  ctxRow.appendChild(label);
  const value = document.createElement('span');
  value.className = 'bubble-more-value';
  ctxRow.appendChild(value);
  menu.appendChild(ctxRow);

  // Cost row — the locked-in cost of THIS message's LLM call (the model's
  // published $/1M rate × its input/output tokens at the time it ran, from the
  // server's per-message `cost_usd` enrichment — the same usage-ledger figure
  // the session cost chip sums). Falls back to the row metadata's recorded
  // `cost` for live messages; shows n/a when no call is linked.
  const costRow = document.createElement('div');
  costRow.className = 'bubble-more-row';
  costRow.dataset.moreControl = 'cost';
  costRow.dataset.costRow = '1';
  costRow.setAttribute('role', 'menuitem');
  costRow.setAttribute('aria-disabled', 'true');
  const costLabel = document.createElement('span');
  costLabel.className = 'bubble-more-label';
  costLabel.innerHTML = '<i data-lucide="coins" style="width:13px;height:13px;"></i><span>Cost</span>';
  costRow.appendChild(costLabel);
  const costValue = document.createElement('span');
  costValue.className = 'bubble-more-value';
  costRow.appendChild(costValue);
  menu.appendChild(costRow);

  // Model row — roster position (Standard / Premium / Vision / Image /
  // Custom N) + model name. One shared row for every bubble type.
  const modelRow = document.createElement('div');
  modelRow.className = 'bubble-more-row';
  modelRow.dataset.moreControl = 'model';
  modelRow.dataset.modelRow = '1';
  modelRow.setAttribute('role', 'menuitem');
  modelRow.setAttribute('aria-disabled', 'true');
  const modelLabel = document.createElement('span');
  modelLabel.className = 'bubble-more-label';
  modelLabel.innerHTML = '<i data-lucide="cpu" style="width:13px;height:13px;"></i><span>Model</span>';
  modelRow.appendChild(modelLabel);
  const modelValue = document.createElement('span');
  modelValue.className = 'bubble-more-value';
  modelRow.appendChild(modelValue);
  menu.appendChild(modelRow);

  // Message id row — the WebAgent interaction id, or the headless engine's OWN
  // message id (Codex item id / Claude msg_… id, written by the engine adapters
  // in plugins/engines/*/ into metadata.engine_message_id) when this bubble came
  // from an alternate-engine turn. Click copies the full id — handy for
  // recalling a message inside the CLI (`claude --resume` / `codex exec resume`
  // transcripts) when something went wrong.
  const idRow = document.createElement('div');
  idRow.className = 'bubble-more-row';
  idRow.dataset.moreControl = 'message_id';
  idRow.dataset.idRow = '1';
  idRow.setAttribute('role', 'menuitem');
  idRow.setAttribute('tabindex', '0');
  const idLabel = document.createElement('span');
  idLabel.className = 'bubble-more-label';
  idLabel.innerHTML = '<i data-lucide="hash" style="width:13px;height:13px;"></i><span>Message</span>';
  const idValue = document.createElement('span');
  idValue.className = 'bubble-more-value';
  idRow.append(idLabel, idValue);
  const _anchorId = _bubbleAnchorId(bubble) || '';
  if (_anchorId) {
    let idLabelText = 'Message';
    let idFull = _anchorId;
    try {
      const cachedMsg = (typeof app.getMessageById === 'function') ? app.getMessageById(_anchorId) : null;
      if (cachedMsg && cachedMsg.metadata) {
        const meta = JSON.parse(cachedMsg.metadata);
        if (meta && typeof meta === 'object' && meta.engine_message_id) {
          if (meta.engine === 'codex') idLabelText = 'Codex';
          else if (meta.engine === 'claude_code') idLabelText = 'Claude';
          idFull = String(meta.engine_message_id);
        }
      }
    } catch (_) { /* metadata parse failure → fall back to the WebAgent id */ }
    idLabel.querySelector('span').textContent = idLabelText;
    idValue.textContent = idFull.length > 12 ? idFull.slice(0, 12) + '…' : idFull;
    idRow.title = 'Copy ' + idLabelText + ' id';
    idRow.addEventListener('click', (e) => {
      e.stopPropagation();
      copyText(idFull).then(() => {
        const orig = idValue.textContent;
        idValue.textContent = idLabelText + ' ID copied ✓';
        setTimeout(() => { idValue.textContent = orig; }, 1200);
      }).catch(() => {});
    });
  } else {
    idRow.style.display = 'none';
  }
  menu.appendChild(idRow);
  const moreRows = {
    context: ctxRow,
    cost: costRow,
    model: modelRow,
    message_id: idRow,
  };

  if (bubble.classList.contains('agent') && bubble.dataset.publicId) {
    const detailsRow = document.createElement('button');
    detailsRow.type = 'button';
    detailsRow.className = 'bubble-more-row bubble-more-action';
    detailsRow.dataset.moreControl = 'row_details';
    detailsRow.setAttribute('role', 'menuitem');
    detailsRow.style.cssText = 'width:100%;border:0;background:transparent;color:inherit;font:inherit;text-align:left;';
    detailsRow.innerHTML = '<span class="bubble-more-label"><i data-lucide="database" style="width:13px;height:13px;"></i><span>row detials</span></span>';
    detailsRow.addEventListener('click', async (event) => {
      event.stopPropagation();
      const sessionId = app.currentSessionId;
      const messageId = bubble.dataset.publicId;
      const userId = app.currentUserId;
      detailsRow.disabled = true;
      detailsRow.querySelector('span span').textContent = 'Loading…';
      try {
        const response = await fetch(apiPath('/api/v2/chat/sessions/' +
          encodeURIComponent(sessionId) + '/messages/' + encodeURIComponent(messageId) + '/row-details'),
          { headers: authHeaders(), cache: 'no-store' });
        if (!response.ok) throw new Error(response.status === 404 ? 'Message not found' :
          response.status === 401 || response.status === 403 ? 'Access denied' : 'Could not load row details');
        const details = await response.json();
        if (!menu.isConnected || sessionId !== app.currentSessionId || userId !== app.currentUserId) return;
        const content = document.createElement('div');
        const summary = document.createElement('p');
        summary.textContent = 'Table: ' + details.table + ' · Render type (kind): ' + details.render_type;
        const note = document.createElement('p');
        note.textContent = 'Complete stored row. payload contains the exact decrypted JSON string; parsed_payload expands it below.';
        const pre = document.createElement('pre');
        pre.style.cssText = 'white-space:pre-wrap;overflow-wrap:anywhere;font-family:var(--font-mono);';
        pre.textContent = JSON.stringify(details, null, 2);
        content.append(summary, note, pre);
        _closeBubbleMoreMenu();
        openChatElementFullView(content, btn, 'Row details');
      } catch (error) {
        if (menu.isConnected) detailsRow.querySelector('span span').textContent = error.message + ' — retry';
      } finally {
        detailsRow.disabled = false;
      }
    });
    menu.appendChild(detailsRow);
    moreRows.row_details = detailsRow;
  }

  // Schema snapshots are available only on agent messages that sent one to the
  // LLM. Keep this technical action in More rather than the visible gutter.
  if (_hasSentSchema(bubble)) {
    const schemaRow = document.createElement('div');
    schemaRow.className = 'bubble-more-row bubble-more-action';
    schemaRow.dataset.moreControl = 'schema';
    schemaRow.setAttribute('role', 'menuitem');
    schemaRow.setAttribute('tabindex', '0');
    const schemaLabel = document.createElement('span');
    schemaLabel.className = 'bubble-more-label';
    schemaLabel.innerHTML = '<i data-lucide="menu" style="width:13px;height:13px;"></i><span>View schema sent to LLM</span>';
    schemaRow.appendChild(schemaLabel);
    schemaRow.addEventListener('click', (e) => {
      e.stopPropagation();
      _closeBubbleMoreMenu();
      _toggleSchemaBubble(bubble);
    });
    menu.appendChild(schemaRow);
    moreRows.schema = schemaRow;
  }

  document.body.appendChild(menu);

  // Refresh row — re-fetch THIS message from the server and re-render it in
  // place. Agent message bubbles AND tool-group update/response rows both
  // carry a saved assistant interaction id (user/system rows do not).
  if ((bubble.classList.contains('agent') || bubble.classList.contains('ca-tool-row'))
      && _bubbleAnchorId(bubble)) {
    const refreshRow = document.createElement('div');
    refreshRow.className = 'bubble-more-row bubble-more-action';
    refreshRow.dataset.moreControl = 'refresh';
    refreshRow.dataset.refreshRow = '1';
    refreshRow.setAttribute('role', 'menuitem');
    refreshRow.setAttribute('tabindex', '0');
    const refreshLabel = document.createElement('span');
    refreshLabel.className = 'bubble-more-label';
    refreshLabel.innerHTML = '<i data-lucide="refresh-cw" style="width:13px;height:13px;"></i><span>Refresh message</span>';
    refreshRow.appendChild(refreshLabel);
    refreshRow.addEventListener('click', (e) => {
      e.stopPropagation();
      _refreshBubbleMessage(refreshRow, bubble);
    });
    menu.appendChild(refreshRow);
    moreRows.refresh = refreshRow;
  }

  const moreSpec = _messageGuttersConfig().more_menu;
  const moreOrder = Array.isArray(moreSpec?.order) ? moreSpec.order : Object.keys(moreRows);
  Object.entries(moreRows).forEach(([name, row]) => {
    if (!_gutterControlEnabled(moreSpec, name) || !moreOrder.includes(name)) row.remove();
  });
  moreOrder.forEach((name) => {
    const row = moreRows[name];
    if (row?.isConnected && _gutterControlEnabled(moreSpec, name)) menu.appendChild(row);
  });

  _renderActionIcons(menu);
  _renderMoreMenuCtx(menu, bubble);
  _renderMoreMenuCost(menu, bubble);
  _renderMoreMenuModel(menu, bubble);
  _positionBubbleMoreMenu(menu, btn);

  // Live refresh — keep the ctx row in step with the pill while the menu is
  // open; the cost row heals itself once a late stamp / cache row lands, and
  // the model row once a late stamp / cache row lands.
  const timer = setInterval(() => {
    if (!menu.isConnected) { clearInterval(timer); return; }
    _renderMoreMenuCtx(menu, bubble);
    _renderMoreMenuCost(menu, bubble);
    _renderMoreMenuModel(menu, bubble);
  }, 800);

  // Close on outside click / Escape / scroll / resize / re-click of the ⋮.
  const onDocClick = (e) => {
    if (!e.target || !e.target.closest) return;
    if (e.target.closest('.bubble-more-menu') || e.target.closest('.turn-gutter-more')) return;
    _closeBubbleMoreMenu();
  };
  const onKey = (e) => { if (e.key === 'Escape') _closeBubbleMoreMenu(); };
  const onMove = () => _closeBubbleMoreMenu();
  document.addEventListener('click', onDocClick, true);
  document.addEventListener('keydown', onKey, true);
  window.addEventListener('scroll', onMove, true);
  window.addEventListener('resize', onMove);

  // Wrap removal so listeners are cleaned up on every close path.
  const origRemove = menu.remove.bind(menu);
  menu.remove = () => {
    if (menu._cleanup) { menu._cleanup(); menu._cleanup = null; }
    origRemove();
  };
  menu._cleanup = () => {
    clearInterval(timer);
    document.removeEventListener('click', onDocClick, true);
    document.removeEventListener('keydown', onKey, true);
    window.removeEventListener('scroll', onMove, true);
    window.removeEventListener('resize', onMove);
  };

  menu.addEventListener('click', (e) => e.stopPropagation());
  menu._anchorBtn = btn;
  btn.classList.add('open');
  _openMoreMenu = menu;
}

function _makeBubbleMoreBtn(gutter, bubble) {
  if (_messageGuttersConfig().more_menu?.enabled === false) return null;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'turn-gutter-btn turn-gutter-more';
  btn.title = 'More';
  btn.setAttribute('aria-label', 'More options');
  btn.setAttribute('aria-haspopup', 'menu');
  btn.innerHTML = '<i data-lucide="ellipsis-vertical" style="width:14px;height:14px;"></i>';
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (_openMoreMenu && _openMoreMenu.isConnected) _closeBubbleMoreMenu();
    else _openBubbleMoreMenu(btn, bubble);
  });
  return btn;
}

// ── Refresh message (⋮ menu) ────────────────────────────────────────────────
// Re-fetch THIS message's saved row from the server and re-render it in place
// from the authoritative copy: fresh text sections (agent bubbles) or a fresh
// row body (tool-group update/response rows), plus re-stamped context/cost/
// model rows in the open menu. Uses app.refreshMessageFull (session-load) so
// the full body comes back.
let _refreshingMsgId = null;

async function _refreshBubbleMessage(row, bubble) {
  const mid = _bubbleAnchorId(bubble);
  if (!mid || _refreshingMsgId === String(mid)) return;
  _refreshingMsgId = String(mid);

  // Streamed bubbles can carry the USER turn id (data-turn-id) instead of the
  // assistant interaction id (data-msg-id). Pass the other one as a fallback
  // so refresh still resolves the agent's reply for that turn.
  const _msgId = bubble.getAttribute('data-msg-id') || '';
  const _turnId = bubble.getAttribute('data-turn-id') || '';
  const altMid = (_msgId && _turnId && _msgId !== _turnId)
    ? (_msgId === String(mid) ? _turnId : _msgId)
    : undefined;

  const label = row.querySelector('.bubble-more-label span');
  const origText = label ? label.textContent : '';
  const setState = (text, cls) => {
    if (label) label.textContent = text;
    row.classList.remove('bubble-more-action', 'bubble-more-done', 'bubble-more-error');
    if (cls) row.classList.add(cls);
  };

  setState('Refreshing…', 'bubble-more-polling');
  try {
    const fresh = (typeof app.refreshMessageFull === 'function')
      ? await app.refreshMessageFull(String(mid), altMid)
      : null;
    if (!fresh || fresh.role !== 'assistant') {
      setState('Not found', 'bubble-more-error');
      setTimeout(() => { setState(origText, 'bubble-more-action'); }, 1400);
      return;
    }
    _applyFreshMessageToBubble(bubble, fresh);
    // Re-stamp the open menu rows from the updated cache.
    const menu = row.closest('.bubble-more-menu');
    if (menu && menu.isConnected) {
      _renderMoreMenuCtx(menu, bubble);
      _renderMoreMenuCost(menu, bubble);
      _renderMoreMenuModel(menu, bubble);
    }
    setState('Refreshed ✓', 'bubble-more-done');
    setTimeout(() => { setState(origText, 'bubble-more-action'); }, 1400);
  } catch (_) {
    setState('Failed', 'bubble-more-error');
    setTimeout(() => { setState(origText, 'bubble-more-action'); }, 1400);
  } finally {
    _refreshingMsgId = null;
  }
}

// Re-render a bubble's LLM text sections from the authoritative saved row.
// Strips the trailing "[Tool calls: …]" trailer (same as the load path),
// refills each section with the fresh markdown, and marks tool-call panels
// stale so their bodies re-fetch on the next open. Also handles tool-group
// activity entry rows (updates/responses), which refill their row body.
function _applyFreshMessageToBubble(bubble, msg) {
  if (!bubble || !msg) return;
  let text = msg.content || '';
  const idx = text.indexOf('\n\n[Tool calls: ');
  if (idx !== -1) text = text.slice(0, idx);

  // Activity entry row (update/response inside a tool group): refill EVERY DOM
  // representation of this interaction. The clicked row may be the collapsed
  // preview clone; updating only it leaves the panel source stale, and the next
  // preview sync would bring the shortened/corrupt text straight back.
  if (bubble.classList.contains('ca-tool-row')) {
    const id = String(msg.id || bubble.dataset.msgId || '');
    let rows = [bubble];
    if (id && app.chatMessages) {
      rows = Array.from(app.chatMessages.querySelectorAll(
        `.ca-tool-row[data-msg-id="${CSS.escape(id)}"]`,
      ));
      if (!rows.includes(bubble)) rows.push(bubble);
    }
    rows.forEach(row => {
      const body = row.querySelector('.ca-activity-entry-body');
      if (body) {
        while (body.firstChild) body.removeChild(body.firstChild);
        const isMd = _fillAgentBubble(body, text, false);
        body.classList.toggle('md', isMd);
      }
      if (row.__entry) row.__entry = { ...row.__entry, content: text };
      row.__mdSource = text || '';
    });
    if (id && app.chatMessages) {
      app.chatMessages.querySelectorAll('.bubble-tool-calls').forEach(container => {
        if (!Array.isArray(container.__calls)) return;
        container.__calls.forEach(entry => {
          if (entry && String(entry.id || '') === id) entry.content = text;
        });
      });
    }
    return;
  }

  const sections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  sections.forEach(section => {
    while (section.firstChild) section.removeChild(section.firstChild);
    const isMd = _fillAgentBubble(section, text, true);
    section.classList.toggle('md', isMd);
  });
  bubble.__mdSource = text || '';

  // Tool panels: drop the lazy-detail flag so the next open re-fetches bodies.
  bubble.querySelectorAll('.bubble-tool-calls').forEach(c => { c.__detailLoaded = false; });
}

// ── Turn-section gutter system ────────────────────────────────────────────
// Each agent turn is ONE bubble containing multiple .turn-section blocks
// (LLM text + tool calls). Every section carries an always-visible
// .turn-gutter below it, showing time, model, and action buttons.

// Build a gutter element (hidden) for placement between turn sections.
function _buildTurnGutter(bubble, sectionIdx) {
  const gutter = document.createElement('div');
  gutter.className = 'turn-gutter';
  gutter.dataset.sectionIdx = sectionIdx;

  // Find the corresponding section in the bubble to get its interaction ID
  const sections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  const section = [...sections].find(s => s.dataset.sectionIdx === String(sectionIdx));
  const sectionAnchor = (section && (section.dataset.msgId || section.dataset.turnId))
    || _bubbleAnchorId(bubble);

  const anchor = _bubbleAnchorId(bubble);
  const streaming = bubble.classList.contains('streaming');
  const spec = _messageGuttersConfig().agent;
  _appendConfiguredGutterControls(gutter, spec, {
    time: () => _makeGutterTime(bubble),
    full_view: () => _makeGutterFullViewBtn(section || bubble),
    collapse: () => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'turn-gutter-btn bubble-collapse-btn';
      btn.title = 'Collapse message';
      btn.innerHTML = '<i data-lucide="chevron-down" style="width:14px;height:14px;"></i>';
      btn.addEventListener('click', (event) => {
        event.stopPropagation();
        _toggleBubbleCollapse(btn, bubble);
      });
      return btn;
    },
    read_aloud: () => {
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'turn-gutter-btn';
      btn.title = 'Read aloud';
      btn.innerHTML = '<i data-lucide="volume-2" style="width:14px;height:14px;"></i>';
      btn.addEventListener('click', (event) => {
        event.stopPropagation();
        _speakBubble(btn, bubble);
      });
      return btn;
    },
    copy: () => _makeGutterCopyBtn(bubble),
    fork: () => anchor && bubble.classList.contains('agent') ? _makeBubbleForkBtn(bubble) : null,
    delete: () => sectionAnchor && !streaming
      ? _makeBubbleDeleteBtn(bubble, sectionAnchor, sectionIdx)
      : null,
    schema: () => bubble.classList.contains('agent') ? _makeBubbleSchemaBtn(bubble) : null,
    more: () => _makeBubbleMoreBtn(gutter, bubble),
  });

  _renderActionIcons(gutter);
  return gutter;
}

// Wire an LLM section and ensure a gutter exists after it.
function _wireLLMSection(section, bubble) {
  if (!section || section._turnWired) return;
  section._turnWired = true;

  // Ensure a gutter exists after this section
  let gutter = section.nextElementSibling;
  if (!gutter || !gutter.classList.contains('turn-gutter')) {
    const idx = section.dataset.sectionIdx || '0';
    gutter = _buildTurnGutter(bubble, idx);
    section.after(gutter);
  }
}

// Wrap raw bubble content in an LLM section (used for legacy/streaming bubbles
// that don't yet have turn-section markup). Works for both agent and user bubbles.
function _ensureLLMSections(bubble) {
  if (!bubble || bubble._sectionsEnsured) return;
  bubble._sectionsEnsured = true;

  const relevant = bubble.classList.contains('agent') || bubble.classList.contains('user');
  if (!relevant) return;

  // Collect direct children that aren't sections, gutters, or legacy footers
  const raw = [];
  for (const child of Array.from(bubble.children)) {
    if (child.classList.contains('turn-section') || child.classList.contains('turn-gutter')
        || child.classList.contains('bubble-actions') || child.classList.contains('label')) continue;
    raw.push(child);
  }
  if (!raw.length) return;

  // Wrap them in an LLM section
  const section = document.createElement('div');
  section.className = 'turn-section llm-section';
  section.dataset.sectionIdx = '0';
  raw.forEach(c => section.appendChild(c));
  bubble.appendChild(section);
}

// ── Bubble actions (section-aware) ────────────────────────────────────────

function _addBubbleActions(bubble) {
  if (!bubble) return;

  const isAgent = bubble.classList.contains('agent');
  const isUser = bubble.classList.contains('user');

  // Wire once across streaming/finalization/reconciliation. Hidden action rows
  // use display:none, so the message itself remains the reveal target.
  if ((isAgent || isUser) && !bubble._gutterRevealWired) {
    bubble._gutterRevealWired = true;
    bubble.addEventListener('click', (event) => {
      if (event.target.closest('a, button, input, textarea, select, .turn-gutter, .bubble-actions')) return;
      // Selecting message text should not change its layout.
      if (window.getSelection()?.toString()) return;
      bubble.classList.toggle('gutters-open');
    });
  }

  // Tool-only groups own no response message. Their update rows render their
  // own footers within the disclosure.
  if (isAgent && bubble.classList.contains('tool-only')) return;

  // System/update notice bubbles (info class) get a lightweight footer with
  // the ⋮ more button so their per-message context readout is reachable —
  // compaction notices show the summariser prompt size, everything else falls
  // back to the session-latest ctx. Idempotent: reconcile / live WS paths can
  // call this more than once on the same bubble.
  if (bubble.classList.contains('info')) {
    if (!bubble.querySelector(':scope > .turn-gutter')) {
      bubble.appendChild(_buildSystemNoticeGutter(bubble));
    }
    return;
  }

  if (isUser) {
    const txt = _getBubbleText(bubble);
    if (!txt) return;

    // Remove legacy footer if present
    const oldFooter = bubble.querySelector(':scope > .bubble-actions');
    if (oldFooter) oldFooter.remove();

    // Wrap raw content in LLM sections if needed
    _ensureLLMSections(bubble);

    // Wire user LLM sections with a user-appropriate gutter
    bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(s => {
      if (s._turnWired) return;
      s._turnWired = true;

      // Ensure a user-appropriate gutter exists after this section
      let gutter = s.nextElementSibling;
      if (!gutter || !gutter.classList.contains('turn-gutter')) {
        gutter = _buildUserTurnGutter(bubble);
        s.after(gutter);
      }
    });

    return;
  }

  if (!isAgent) return;

  const streaming = bubble.classList.contains('streaming');
  const txt = _getBubbleText(bubble);
  if (!txt || txt === '\u2026') return;

  // Remove legacy footer if present
  const oldFooter = bubble.querySelector(':scope > .bubble-actions');
  if (oldFooter) oldFooter.remove();

  // Wrap raw content in LLM sections if needed
  _ensureLLMSections(bubble);

  // Wire all existing LLM sections
  bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(s => {
    _wireLLMSection(s, bubble);
  });

  // If streaming, mark that sections need rewiring on finalize
  if (streaming) bubble._needsSectionWire = true;
}

// Called after a bubble is finalized (streaming → done) to ensure sections are wired.
function _finalizeBubbleSections(bubble) {
  if (!bubble) return;
  if (bubble._needsSectionWire) {
    bubble._needsSectionWire = false;
    _ensureLLMSections(bubble);
    bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(s => {
      _wireLLMSection(s, bubble);
    });
  }

  // Streaming gutters intentionally omit destructive actions. Once the saved
  // interaction is final, add the same per-section delete control a cold-loaded
  // bubble receives without rebuilding the gutter or duplicating listeners.
  if (!bubble.classList.contains('streaming')) {
    const spec = _messageGuttersConfig().agent;
    const sections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
    sections.forEach(section => {
      const gutter = section.nextElementSibling;
      if (!gutter || !gutter.classList.contains('turn-gutter')) return;
      if (gutter.querySelector(':scope > .bubble-delete-btn')) return;
      const sectionIdx = section.dataset.sectionIdx || '0';
      const sectionAnchor = section.dataset.msgId || section.dataset.turnId || _bubbleAnchorId(bubble);
      if (!sectionAnchor) return;
      const deleteBtn = _makeBubbleDeleteBtn(bubble, sectionAnchor, sectionIdx);
      _insertConfiguredGutterControl(gutter, spec, 'delete', deleteBtn);
      _renderActionIcons(gutter);
    });
  }
}

// ── Server-row user bubble (footer inseparable from creation) ──────────────
// Render a user message from a server row and GUARANTEE it has a footer. The
// base addChatBubble never builds one (the footer is lifecycle-dependent), so a
// bare "create user bubble" call is how renderer paths silently lost the gutter.
// This is the single place the two steps stay together: dedupe by data-msg-id,
// adopt an untagged optimistic bubble by matching text, otherwise create + wire.
function ensureUserBubble(content, msgId, createdAt, extraClass) {
  if (!app.chatMessages) return null;
  const cont = content || '';
  let canonical = null;
  if (msgId) {
    // Contract: exactly one top-level user bubble per durable id. Collect every
    // same-id bubble and keep the first; a race between the optimistic send and
    // the durable reconcile must never leave two bubbles for one message.
    const sameId = Array.from(app.chatMessages.querySelectorAll(
      `.chat-bubble.user[data-msg-id="${CSS.escape(String(msgId))}"]`,
    ));
    canonical = sameId[0] || null;
    for (let i = 1; i < sameId.length; i++) sameId[i].remove();
    if (!canonical) {
      // The optimistic send is the newest user row and carries either the
      // outbox's provisional data-msg-id or a pending marker. Adopt only that
      // run-local candidate; never sweep an older identical prompt.
      const users = app.chatMessages.querySelectorAll(':scope > .chat-bubble.user');
      const candidate = users.length ? users[users.length - 1] : null;
      const provisionalId = candidate?.getAttribute('data-msg-id') || '';
      const isPending = !!candidate && (
        candidate.classList.contains('pending')
        || candidate.classList.contains('saving')
        || candidate.hasAttribute('data-pending-id')
        || provisionalId.startsWith('msg_')
      );
      const candidateText = (candidate?.querySelector('.bubble-body')?.textContent || '').trim();
      if (isPending && candidateText === cont.trim()) {
        candidate.setAttribute('data-msg-id', String(msgId));
        canonical = candidate;
      }
    }
  }
  if (!canonical) {
    // Create through app.addChatBubble (not the raw import) so virtual-scroll's
    // height-tracking wrapper still runs when it's installed.
    canonical = (typeof app.addChatBubble === 'function')
      ? app.addChatBubble('user', cont, extraClass, undefined, undefined, msgId || undefined, createdAt)
      : addChatBubble('user', cont, extraClass, undefined, undefined, msgId || undefined, createdAt);
    if (canonical && canonical.nodeType === 1) _addBubbleActions(canonical);
  }
  if (canonical && msgId) {
    if (createdAt) _setBubbleCreatedAt(canonical, createdAt);
    if (!canonical.querySelector(':scope > .turn-gutter')) _addBubbleActions(canonical);
    // Atomic uniqueness by durable id. Provisional adoption above is scoped to
    // the newest pending run, so historical identical prompts remain intact.
    Array.from(app.chatMessages.querySelectorAll(
      `:scope > .chat-bubble.user[data-msg-id="${CSS.escape(String(msgId))}"]`,
    )).forEach((b) => { if (b !== canonical) b.remove(); });
  }
  return canonical;
}

// Expose on app immediately for session-load.js / virtual scroll
app._addBubbleActions = _addBubbleActions;
app._ensureUserBubble = ensureUserBubble;
app._finalizeBubbleSections = _finalizeBubbleSections;
app._ensureLLMSections = _ensureLLMSections;
app._wireLLMSection = _wireLLMSection;
app._injectDeletedActions = _injectDeletedActions;

export {
  _addBubbleActions,
  ensureUserBubble,
  _finalizeBubbleSections,
  _ensureLLMSections,
  _wireLLMSection,
  _bubbleAnchorId,
  _getBubbleText,
  _setActionIcon,
  _setBubbleModel,
  _setBubbleModelFromMeta,
  _modelLabelFromMeta,
  _makeBubbleMoreBtn,
  _makeBubbleDeleteBtn,
};

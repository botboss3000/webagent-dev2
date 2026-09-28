'use strict';

// Chat element: visibility — filter which message lane types are shown in the
// transcript (main replies, progress steps, tool calls, Summary lane, system
// notices). Writes overrides via shared/js/chat-visibility.js (localStorage),
// then re-renders the current transcript from the cache. Config defaults live
// in chat_ui.json → chat_common.message_visibility.defaults.

import {
  getMessageVisibility,
  setMessageTypeVisible,
  onMessageVisibilityChange,
} from '../../shared/js/chat-visibility.js';
import { reprojectForVisibilityChange } from '../../chat/js/session-load.js?v=509';
import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';

const TYPES = [
  ['main',     'Main',       'Agent reply bubbles'],
  ['progress', 'Progress',   'Mid-turn update steps'],
  ['tool',     'Tool calls', 'Tool-call panels'],
  ['summary',  'Closer',     'Output Closer lane'],
  ['system',   'System',     'Errors and status notices'],
];

let _popover = null;
let _unsub = null;

export function html(cfg = {}) {
  const size = cfg.element_size || '16px';
  return `<button type="button" class="chat-visibility-btn" title="Show / hide message types" data-element-name="visibility">
    <i data-lucide="list-filter" style="width:${size};height:${size};"></i>
  </button>`;
}

export function init(el) {
  el.id = 'chat-visibility-btn';
  el.addEventListener('click', (e) => {
    e.stopPropagation();
    _togglePopover(el);
  });
  if (!_unsub) {
    _unsub = onMessageVisibilityChange(() => {
      try { reprojectForVisibilityChange(); } catch (_) { /* render healed on next event */ }
      _syncRows();
    });
  }
  try { _refreshLucideIcons(); } catch (_) { /* icons hydrate at boot */ }
}

export function destroy(el) {
  _closePopover();
  if (_unsub) { _unsub(); _unsub = null; }
}

export function style() { return ''; }

function _togglePopover(anchor) {
  if (_popover && _popover.isConnected) {
    _popover.remove();
    _popover = null;
    return;
  }
  const pop = document.createElement('div');
  pop.className = 'chat-visibility-popover';
  pop.setAttribute('role', 'menu');

  const title = document.createElement('div');
  title.className = 'chat-visibility-title';
  title.textContent = 'Show / hide message types';
  pop.appendChild(title);

  const map = getMessageVisibility();
  for (const [type, label, hint] of TYPES) {
    const row = document.createElement('label');
    row.className = 'chat-visibility-row';

    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.checked = map[type] !== false;
    cb.dataset.type = type;
    cb.addEventListener('change', () => setMessageTypeVisible(type, cb.checked));

    const span = document.createElement('span');
    span.textContent = label;
    const sub = document.createElement('small');
    sub.textContent = hint;

    row.appendChild(cb);
    row.appendChild(span);
    row.appendChild(sub);
    pop.appendChild(row);
  }

  _popover = pop;
  anchor.appendChild(pop);

  // Close on any outside click. Deferred so the opening click (still bubbling)
  // isn't caught and immediately closed. The listener is PERSISTENT (removed on
  // close) — a `{once:true}` listener here would be consumed by a click INSIDE
  // the popover and never re-arm, permanently breaking outside-click-to-close.
  setTimeout(() => {
    document.removeEventListener('click', _onDocClick);
    document.addEventListener('click', _onDocClick);
  }, 0);
}

function _onDocClick(e) {
  if (_popover && !_popover.contains(e.target)) _closePopover();
}

function _closePopover() {
  if (_popover) { _popover.remove(); _popover = null; }
  document.removeEventListener('click', _onDocClick);
}

function _syncRows() {
  if (!_popover || !_popover.isConnected) return;
  const map = getMessageVisibility();
  _popover.querySelectorAll('input[type="checkbox"]').forEach(cb => {
    cb.checked = map[cb.dataset.type] !== false;
  });
}

'use strict';

// ──────────────────────────────────────────────────────────────────────────
// Terminal Chat — on-screen key grid (PLUGIN-OWNED frontend)
// ──────────────────────────────────────────────────────────────────────────
// Ships WITH the terminal_chat engine, NOT with the core chat panel. Served off
// disk at /plugins/engines/terminal_chat/ui/terminal-keys.js and loaded lazily
// (dynamic import) by the core engine adapter (ui/chat/js/
// chat-terminal-engine.js) ONLY when a Terminal Chat session mounts. A normal
// (LLM-loop) agent never fetches, builds, or sees any of this — so the footer
// keyboard button is only present for terminal-chat agents.
//
// What it does: an on-screen key VIEW the chat pill takes on. mountTerminalKeys()
// injects (a) a keyboard toggle button into the chat footer (#chat-term-keys-btn),
// (b) a fixed 3-row × 8-col grid (#chat-term-keypad) into the pill (#chat-input-row),
// and (c) this folder's terminal-keys.css. The toggle adds .term-keys-open to
// #chat-input-area; the pill then hides its normal controls and the grid overlays
// the whole pill box (number pad 0-9, arrows, Esc/Tab/⇧Tab/Enter/⌫/^C, Home/End/
// PgUp/PgDn). Each chip fires a SINGLE keystroke straight to the live PTY (no Enter
// appended, unlike the pill) so the user can drive TUI menus, move the cursor, pick
// numbered options, and interrupt.
//
// Fully decoupled from the core: it holds NO import of app state — the engine hands
// it the two callables it needs (sendKey / focus) at mount time. unmountTerminalKeys()
// removes the button + keypad and restores the pill. Sibling of the admin terminal's
// keybar (ui/shared/js/files.js → initTerminalKeybar); the key→byte map mirrors it.
//
// REMOVE-WHEN: the terminal_chat engine is dropped — delete this whole ui/ folder;
// the core carries none of it.

// key id → raw bytes the PTY expects. Arrows / Home / End / PgUp-Dn are ANSI
// cursor escapes; Ctrl-combos are the matching control codes; digits are literal.
// Backspace is DEL (0x7f), the PTY convention.
const KEY_BYTES = {
  up: '\x1b[A', down: '\x1b[B', right: '\x1b[C', left: '\x1b[D',
  home: '\x1b[H', end: '\x1b[F', pgup: '\x1b[5~', pgdn: '\x1b[6~',
  esc: '\x1b', tab: '\t', 'shift-tab': '\x1b[Z', enter: '\r',
  backspace: '\x7f', del: '\x1b[3~',
  'ctrl-c': '\x03', 'ctrl-d': '\x04', 'ctrl-z': '\x1a',
  'ctrl-l': '\x0c', 'ctrl-r': '\x12',
};
for (let d = 0; d <= 9; d++) KEY_BYTES['num-' + d] = String(d);

// The grid, in reading order: 3 rows × 8 columns.
//   row 1 — digits 1-8
//   row 2 — digits 9-0, then Esc / Tab / ⇧Tab / Enter / Backspace / interrupt
//   row 3 — the four arrows, then Home / End / PgUp / PgDn
const LAYOUT = [
  { key: 'num-1', label: '1' }, { key: 'num-2', label: '2' }, { key: 'num-3', label: '3' }, { key: 'num-4', label: '4' },
  { key: 'num-5', label: '5' }, { key: 'num-6', label: '6' }, { key: 'num-7', label: '7' }, { key: 'num-8', label: '8' },
  { key: 'num-9', label: '9' }, { key: 'num-0', label: '0' },
  { key: 'esc', label: 'Esc' }, { key: 'tab', label: 'Tab' }, { key: 'shift-tab', label: '⇧Tab', title: 'Shift+Tab (back-tab)' },
  { key: 'enter', label: '↵', title: 'Enter' }, { key: 'backspace', label: '⌫', title: 'Backspace' },
  { key: 'ctrl-c', label: '^C', title: 'Interrupt (Ctrl+C)' },
  { key: 'left', icon: 'arrow-left', title: 'Left' }, { key: 'up', icon: 'arrow-up', title: 'Up' },
  { key: 'down', icon: 'arrow-down', title: 'Down' }, { key: 'right', icon: 'arrow-right', title: 'Right' },
  { key: 'home', label: 'Home' }, { key: 'end', label: 'End' },
  { key: 'pgup', label: 'PgUp' }, { key: 'pgdn', label: 'PgDn' },
];

// The <link> id + href for this folder's stylesheet (absolute from origin root,
// so it resolves the same under /app or /setup base paths).
const CSS_ID = 'terminal-chat-keys-css';
const CSS_HREF = '/plugins/engines/terminal_chat/ui/terminal-keys.css';

let _btn = null;      // footer keyboard toggle
let _panel = null;    // in-pill keypad grid
let _area = null;     // #chat-input-area (owns the .term-keys-open class)
let _sendKey = null;  // callable: raw-bytes → PTY (from the engine)
let _focus = null;    // callable: return focus to the terminal (from the engine)

function _injectCss() {
  if (document.getElementById(CSS_ID)) return;
  const link = document.createElement('link');
  link.id = CSS_ID;
  link.rel = 'stylesheet';
  link.href = CSS_HREF;
  document.head.appendChild(link);
}

function _renderIcons(root) {
  try {
    if (window.lucide && typeof window.lucide.createIcons === 'function') {
      window.lucide.createIcons({ nodes: [root] });
    }
  } catch (_) { /* icons are cosmetic — never block */ }
}

function _keypadHtml() {
  return LAYOUT.map((k) => {
    const inner = k.icon ? '<i data-lucide="' + k.icon + '"></i>' : k.label;
    const title = k.title || k.label || k.key;
    return '<button type="button" class="ctk-chip" data-key="' + k.key + '" '
      + 'title="' + title + '" aria-label="' + title + '">' + inner + '</button>';
  }).join('');
}

function _send(key) {
  if (!Object.prototype.hasOwnProperty.call(KEY_BYTES, key)) return;
  if (typeof _sendKey === 'function') _sendKey(KEY_BYTES[key]);
  // A footer/grid tap steals focus from the terminal — hand it back so mouse and
  // direct keyboard interaction keep landing where the user expects.
  if (typeof _focus === 'function') _focus();
}

// Swap pill ↔ keys grid: the input area carries .term-keys-open while the grid
// shows (CSS hides #chat-input-row's normal controls and reveals #chat-term-keypad
// in their place).
function _setOpen(open) {
  if (!_area || !_btn) return;
  _area.classList.toggle('term-keys-open', open);
  _btn.dataset.active = open ? '1' : '';
}

// Build + wire the footer toggle and in-pill keypad, injecting them into the core
// chat-pill nodes. Called by the engine on mount. opts: { sendKey, focus }.
export function mountTerminalKeys(opts) {
  opts = opts || {};
  _sendKey = opts.sendKey || null;
  _focus = opts.focus || null;

  _area = document.getElementById('chat-input-area');
  const row = document.getElementById('chat-input-row');
  const footerRight = document.querySelector('#chat-footer-row .chat-footer-right');
  if (!_area || !row || !footerRight) return;

  _injectCss();

  // Keypad grid — lives inside the pill so the CSS can overlay it across the box.
  if (!_panel) {
    _panel = document.createElement('div');
    _panel.id = 'chat-term-keypad';
    _panel.className = 'chat-term-keypad';
    _panel.innerHTML = _keypadHtml();
    row.appendChild(_panel);
    _renderIcons(_panel);

    // One delegated handler — closest() finds the owning button even when the tap
    // lands on an inner Lucide <svg>.
    _panel.addEventListener('click', (e) => {
      const chip = e.target.closest('[data-key]');
      if (!chip) return;
      e.preventDefault();
      _send(chip.getAttribute('data-key'));
    });
  }

  // Footer toggle — first control in the footer's right cluster (its old spot,
  // just before the Abilities button).
  if (!_btn) {
    _btn = document.createElement('button');
    _btn.id = 'chat-term-keys-btn';
    _btn.className = 'chat-skills-btn chat-term-keys-btn';
    _btn.type = 'button';
    _btn.title = 'Terminal keys — arrows, number pad & shortcuts';
    _btn.innerHTML = '<i data-lucide="keyboard" style="width:20px;height:20px;"></i>';
    footerRight.insertBefore(_btn, footerRight.firstChild);
    _renderIcons(_btn);

    // Toggle the grid in/out of the pill's spot.
    _btn.addEventListener('click', (e) => {
      e.preventDefault();
      _setOpen(!_area.classList.contains('term-keys-open'));
    });
  }
}

// Tear down: close the view (restore the pill) and remove the injected nodes so a
// normal agent's chat panel carries nothing of this. The <link> is left in the head
// (harmless, cached) so a later re-mount doesn't reflow-flash.
export function unmountTerminalKeys() {
  _setOpen(false);
  if (_panel && _panel.parentNode) _panel.parentNode.removeChild(_panel);
  if (_btn && _btn.parentNode) _btn.parentNode.removeChild(_btn);
  _panel = null;
  _btn = null;
  _sendKey = null;
  _focus = null;
}

'use strict';

// Terminal Chat — live xterm.js replaces the entire chat transcript area.
// When the user opens a Terminal Chat agent session, this module:
//   * hides the chat bubble area (#chat-messages-inner) completely
//   * mounts a full-screen xterm.js terminal in its place
//   * connects to the terminal WebSocket for live output
//   * the chat pill sends text directly to the terminal's stdin
//
// No agent loop, no chat bubbles, no message history — just a terminal.
//
// The PTY is created immediately on session load via an API call; the terminal
// WS streams the program's stdout (ANSI colours, cursor, everything) into xterm.

import { app } from '../../shared/js/state.js';
import { termWsUrl, apiPath } from '../../shared/js/config.js';
import { authHeaders } from "../../shared/js/left-login.js";
import { ensureXterm } from "../../shared/js/terminal.js";

let _term = null;
let _fitAddon = null;
let _ws = null;
let _host = null;
let _active = false;
let _tsid = null;

// The on-screen keys are PLUGIN-OWNED: their JS + CSS ship with the terminal_chat
// engine (plugins/engines/terminal_chat/ui/) and are fetched lazily the first time
// a Terminal Chat session mounts, so a normal agent never loads them and the footer
// keyboard button only exists for terminal-chat agents. Absolute URL so it resolves
// the same under /app or /setup base paths. Cached after the first import.
const _KEYS_MODULE_URL = '/plugins/engines/terminal_chat/ui/terminal-keys.js';
let _keysMod = null;

async function _ensureKeysModule() {
  if (_keysMod) return _keysMod;
  try {
    _keysMod = await import(_KEYS_MODULE_URL);
  } catch (_) {
    _keysMod = null;   // plugin absent / stripped edition — terminal still works, just no on-screen keys
  }
  return _keysMod;
}

// ── Build the terminal host ────────────────────────────────────────────────

function _buildHost() {
  const messages = document.getElementById('chat-messages');
  if (!messages) return null;

  // Hide the bubble area completely.
  const inner = document.getElementById('chat-messages-inner');
  if (inner) inner.style.display = 'none';

  // Hide the activity bar and action row above the pill — just the pill.
  const above = document.getElementById('chat-above-pill');
  if (above) above.style.display = 'none';

  // Create the terminal host that fills the message area.
  const host = document.createElement('div');
  host.id = 'chat-terminal-host';
  host.style.cssText = 'position:absolute;top:0;left:0;right:0;bottom:0;overflow:hidden;background:#0d0d1a;z-index:5;';
  messages.style.position = 'relative';
  messages.appendChild(host);
  return host;
}

function _removeHost() {
  if (_host && _host.parentNode) _host.parentNode.removeChild(_host);
  _host = null;

  const inner = document.getElementById('chat-messages-inner');
  if (inner) inner.style.display = '';

  const above = document.getElementById('chat-above-pill');
  if (above) above.style.display = '';
}

// ── Terminal WebSocket ─────────────────────────────────────────────────────

function _connect(tsid) {
  if (_ws) { try { _ws.close(); } catch (_) {} _ws = null; }

  const token = localStorage.getItem('auth_token');
  const url = termWsUrl() + '?session_id=' + encodeURIComponent(tsid)
    + (token ? '&token=' + encodeURIComponent(token) : '');

  const ws = new WebSocket(url);
  ws.binaryType = 'arraybuffer';
  _ws = ws;

  ws.onmessage = (evt) => {
    if (!_term) return;
    try {
      if (evt.data instanceof ArrayBuffer) {
        // Real PTY output — the only thing that should ever paint the terminal.
        _term.write(new Uint8Array(evt.data));
      } else if (typeof evt.data === 'string') {
        // String frames are ALWAYS JSON control messages (the PTY's bytes arrive
        // as ArrayBuffer, set via binaryType above) — ping/auth_ok/error. They
        // must never be written to the screen, or their raw JSON shows up as
        // `{"type":"ping"}` noise at the prompt. Reply to the server's heartbeat
        // ping so its inbound-silence watchdog keeps the socket alive (the pill
        // sends input over HTTP, so the WS otherwise never sends a frame and the
        // server would force-close it after ~45s of no typing).
        let msg = null;
        try { msg = JSON.parse(evt.data); } catch (_) { /* unknown text — drop */ }
        if (msg && msg.type === 'ping') {
          try { if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: 'pong' })); } catch (_) {}
        }
        // All other control frames (auth_ok, pong, error, …) are swallowed.
      }
    } catch (_) {}
  };

  ws.onclose = () => { _ws = null; };
}

// ── Mount / unmount ────────────────────────────────────────────────────────

async function _mount(tsid) {
  if (_active) _unmount();
  if (!tsid) return;

  _tsid = tsid;
  _host = _buildHost();
  if (!_host) return;

  try {
    await ensureXterm();
    _term = new Terminal({
      cursorBlink: true,
      cursorStyle: 'block',
      fontSize: 14,
      fontFamily: '"Fira Code", Menlo, Monaco, "Courier New", monospace',
      allowTransparency: true,
      theme: {
        background: '#0d0d1a', foreground: '#c0caf5', cursor: '#c0caf5',
        selectionBackground: '#2a2a4a', black: '#1d202f', red: '#f7768e',
        green: '#9ece6a', yellow: '#e0af68', blue: '#7aa2f7',
        magenta: '#bb9af7', cyan: '#7dcfff', white: '#c0caf5',
        brightBlack: '#565f89', brightRed: '#f7768e', brightGreen: '#9ece6a',
        brightYellow: '#e0af68', brightBlue: '#7aa2f7', brightMagenta: '#bb9af7',
        brightCyan: '#7dcfff', brightWhite: '#c0caf5',
      },
    });

    _fitAddon = new FitAddon.FitAddon();
    _term.loadAddon(_fitAddon);
    _term.open(_host);
    setTimeout(() => { try { _fitAddon.fit(); } catch (_) {} }, 80);

    // Forward keystrokes AND mouse events from the xterm widget straight to the
    // PTY over the same WS. This is the single hook that carries MOUSE support
    // over from the admin terminal (ui/shared/js/terminal.js term.onData): xterm
    // encodes clicks/drags/scroll as escape sequences here whenever the running
    // program enables mouse reporting (Claude Code, vim, tmux `mouse on`). It
    // also makes the terminal directly typable — click into it and type — while
    // the chat pill stays on its own HTTP /write path (chat-send.js) for longer
    // text. Both channels reach the same PTY.
    _term.onData((data) => {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        try { _ws.send(data); } catch (_) {}
      }
    });
    // Tell the PTY the new viewport size when xterm reflows so full-screen TUIs
    // don't wrap at the wrong width (same message the admin terminal sends).
    _term.onResize(({ rows, cols }) => {
      if (_ws && _ws.readyState === WebSocket.OPEN) {
        try { _ws.send(JSON.stringify({ type: 'resize', rows, cols })); } catch (_) {}
      }
    });
    // Shift+Tab: the browser steals it for focus traversal and a modern TUI
    // ignores the legacy back-tab xterm would emit, so intercept at keydown and
    // inject CSI Z ourselves (mirrors the admin terminal's handler).
    _term.attachCustomKeyEventHandler((e) => {
      if (e.type !== 'keydown') return true;
      const isShiftTab = (e.key === 'Tab' || e.code === 'Tab' || e.keyCode === 9)
        && e.shiftKey && !e.ctrlKey && !e.altKey && !e.metaKey;
      if (!isShiftTab) return true;
      e.preventDefault();
      if (_ws && _ws.readyState === WebSocket.OPEN) { try { _ws.send('\x1b[Z'); } catch (_) {} }
      return false;
    });

    const ro = new ResizeObserver(() => { try { _fitAddon.fit(); } catch (_) {} });
    ro.observe(_host);
    _term._resizeObserver = ro;
  } catch (e) {
    const error = document.createElement('div');
    error.className = 'chat-terminal-error';
    error.textContent = `Could not start terminal: ${e && e.message || e}`;
    _host.replaceChildren(error);
    return;
  }

  _connect(tsid);
  app.terminalChat = { active: true, tsid: tsid };
  if (app.chatInput) app.chatInput.placeholder = 'Type and press Enter to send to terminal';
  // Lazily load the plugin-owned on-screen keys and inject the footer toggle +
  // in-pill number-pad. Handing it the two callables it needs keeps it fully
  // decoupled from core app state.
  _ensureKeysModule().then((m) => {
    // Bail if the session was torn down before the module finished loading.
    if (!_active || !m || typeof m.mountTerminalKeys !== 'function') return;
    try { m.mountTerminalKeys({ sendKey: sendTerminalInput, focus: focusTerminalChat }); } catch (_) {}
  });
  _active = true;
}

function _unmount() {
  if (_ws) { try { _ws.close(); } catch (_) {} _ws = null; }
  if (_term) {
    try { if (_term._resizeObserver) _term._resizeObserver.disconnect(); } catch (_) {}
    try { _term.dispose(); } catch (_) {}
    _term = null;
  }
  _fitAddon = null;
  _removeHost();
  _tsid = null;
  _active = false;
  app.terminalChat = { active: false };
  // Remove the plugin-owned footer toggle + in-pill keypad (restores the pill).
  if (_keysMod && typeof _keysMod.unmountTerminalKeys === 'function') {
    try { _keysMod.unmountTerminalKeys(); } catch (_) {}
  }
  if (app.chatInput) app.chatInput.placeholder = 'Message…';
}

// ── Activate the terminal for a session (called from session-load.js) ──────

export async function activateTerminalChat(sessionId, agentId) {
  if (!sessionId || !agentId) return false;
  if (_active) _unmount();

  try {
    const res = await fetch(apiPath('/api/v1/chat/terminal-chat/activate'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ session_id: sessionId, agent_id: agentId }),
    });
    const data = await res.json();
    if (res.ok && data.terminal_session_id) {
      await _mount(data.terminal_session_id);
      return true;
    }
    return false;
  } catch (_) {
    return false;
  }
}

export function deactivateTerminalChat() {
  if (_active) _unmount();
}

// Send raw bytes to the live PTY. Prefers the WS (already open for output);
// falls back to the HTTP /write endpoint the chat pill uses. Handed to the
// plugin-owned on-screen keys (plugins/engines/terminal_chat/ui/terminal-keys.js)
// at mount so each key chip fires a single keystroke to the PTY.
export function sendTerminalInput(data) {
  if (!data) return;
  if (_ws && _ws.readyState === WebSocket.OPEN) {
    try { _ws.send(data); return; } catch (_) {}
  }
  if (_tsid) {
    fetch(apiPath('/api/v1/terminal/sessions/' + encodeURIComponent(_tsid) + '/write'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ input: data }),
    }).catch(() => {});
  }
}

// Return keyboard focus to the terminal (after a footer-key tap steals it).
export function focusTerminalChat() {
  try { if (_term) _term.focus(); } catch (_) {}
}

export function initTerminalChatEngine() {
  app.terminalChat = app.terminalChat || { active: false };

  // Called from session-load.js when a session for a terminal_chat agent loads.
  app.activateTerminalChat = activateTerminalChat;
  app.deactivateTerminalChat = deactivateTerminalChat;
  // Kept on app for any external caller; the plugin-owned on-screen keys are now
  // handed these callables directly at mount (they no longer read app state).
  app.terminalChatKey = sendTerminalInput;
  app.terminalChatFocus = focusTerminalChat;

  // Handler for WS events (if the engine fires any mid-session).
  app._terminalChatHandler = (event) => {
    if (!event) return;
    switch (event.type) {
      case 'terminal_chat_state':
        if (event.state === "active" && event.terminal_session_id) void _mount(event.terminal_session_id);
        else if (event.state !== 'active') _unmount();
        break;
      case 'terminal_stream':
        if (_term) { try { _term.write(event.data || ''); } catch (_) {} }
        break;
    }
  };
}

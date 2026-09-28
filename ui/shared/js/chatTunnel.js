'use strict';

// Terminal-tunnel UI for chat.
//
// When a chat session is bound to a terminal (the user drives a program
// directly — see app/agent/terminal_tunnel.py), this module:
//   * overlays a read-only live terminal across the chat message area so the
//     program renders properly (full-screen TUIs included),
//   * shows a banner with a key palette (Enter/Esc/Ctrl+C/arrows/Tab/Y/N) and a
//     "Hand back" button — the ONLY way out, since typed text goes to the
//     program, not the agent,
//   * flips the composer into tunnel mode (Enter sends a keystroke line to the
//     program instead of a prompt to the agent).
//
// Display is the embedded terminal's own WebSocket; input is the chat composer
// (routed server-side so it's persisted but kept out of the agent's context).

import { app } from './state.js';
import { apiPath } from './config.js';
import { authHeaders } from './left-login.js';
import { createTerminalInstance } from './terminal.js';

let _term = null;            // the read-only embedded terminal instance
let _overlay = null;         // host covering the message area
let _banner = null;          // status + palette + hand-back, above the pill
let _origPlaceholder = '';

// Named special keys offered in the palette → sent to the keys endpoint.
const _KEY_BUTTONS = [
  { label: 'Enter', keys: ['enter'] },
  { label: 'Esc', keys: ['esc'] },
  { label: '^C', keys: ['ctrl+c'], title: 'Ctrl+C — interrupt' },
  { label: 'Tab', keys: ['tab'] },
  { label: '↑', keys: ['up'] },
  { label: '↓', keys: ['down'] },
  { label: '←', keys: ['left'] },
  { label: '→', keys: ['right'] },
];

function _buildOverlay() {
  const messages = app.chatMessages || document.getElementById('chat-messages');
  if (!messages) return null;
  // The overlay sits inside the (relatively-positioned) message area and
  // covers it while the tunnel is live.
  if (getComputedStyle(messages).position === 'static') {
    messages.style.position = 'relative';
  }
  const host = document.createElement('div');
  host.id = 'chat-tunnel-overlay';
  host.className = 'chat-tunnel-overlay';
  host.hidden = true;
  messages.appendChild(host);
  return host;
}

function _buildBanner() {
  const area = document.getElementById('chat-input-area');
  const row = document.getElementById('chat-input-row');
  if (!area || !row) return null;
  const banner = document.createElement('div');
  banner.id = 'chat-tunnel-banner';
  banner.className = 'chat-tunnel-banner';
  banner.hidden = true;
  banner.innerHTML = `
    <div class="chat-tunnel-banner-top">
      <span class="chat-tunnel-dot" aria-hidden="true"></span>
      <span class="chat-tunnel-label">You're driving <strong class="chat-tunnel-cmd">the terminal</strong></span>
      <button type="button" class="chat-tunnel-handback" title="Hand control back to the agent">Hand back</button>
    </div>
    <div class="chat-tunnel-keys" role="group" aria-label="Terminal keys"></div>`;
  // Palette buttons.
  const keysWrap = banner.querySelector('.chat-tunnel-keys');
  _KEY_BUTTONS.forEach((k) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chat-tunnel-key';
    b.textContent = k.label;
    if (k.title) b.title = k.title;
    b.addEventListener('click', (e) => { e.preventDefault(); sendKeys(k.keys); _focusComposer(); });
    keysWrap.appendChild(b);
  });
  // Y / N answer shortcuts type a letter + Enter (handy for confirm prompts).
  [['Y', 'y'], ['N', 'n']].forEach(([label, ch]) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'chat-tunnel-key';
    b.textContent = label;
    b.addEventListener('click', (e) => { e.preventDefault(); sendLine(ch); _focusComposer(); });
    keysWrap.appendChild(b);
  });
  banner.querySelector('.chat-tunnel-handback')
    .addEventListener('click', (e) => { e.preventDefault(); handBack(); });
  area.insertBefore(banner, row);
  return banner;
}

function _focusComposer() {
  if (app.chatInput) { try { app.chatInput.focus(); } catch (_) {} }
}

function _ensureEls() {
  if (!_overlay) _overlay = _buildOverlay();
  if (!_banner) _banner = _buildBanner();
}

async function _mount(state) {
  _ensureEls();
  if (!_overlay || !_banner) return;
  const tsid = state.terminal_session_id;
  if (!tsid) return;

  // (Re)create the read-only terminal bound to the live session.
  if (_term) { try { _term.dispose(); } catch (_) {} _term = null; }
  _overlay.innerHTML = '';
  _overlay.hidden = false;
  try {
    _term = await createTerminalInstance(_overlay, tsid, {
      readOnly: true,
      name: state.name || state.command || 'tunnel',
    });
    // Fit once laid out.
    setTimeout(() => { try { _term && _term.fit && _term.fit(); } catch (_) {} }, 60);
  } catch (e) {
    const error = document.createElement('div');
    error.className = 'chat-tunnel-error';
    error.textContent = `Could not attach terminal: ${e && e.message || e}`;
    _overlay.replaceChildren(error);
  }

  const cmd = state.command || state.name || 'the terminal';
  const cmdEl = _banner.querySelector('.chat-tunnel-cmd');
  if (cmdEl) cmdEl.textContent = cmd;
  _banner.hidden = false;

  // Flip the composer into tunnel mode.
  app.tunnel = { active: true, terminalSessionId: tsid, command: cmd };
  if (app.chatInput) {
    if (!_origPlaceholder) _origPlaceholder = app.chatInput.placeholder || '';
    app.chatInput.placeholder = 'Type to ' + cmd + ' · Enter sends';
    app.chatInput.disabled = false;
  }
  if (app.chatSend) app.chatSend.disabled = !(app.chatInput && app.chatInput.value.trim());
}

function _unmount() {
  if (_term) { try { _term.dispose(); } catch (_) {} _term = null; }
  if (_overlay) { _overlay.hidden = true; _overlay.innerHTML = ''; }
  if (_banner) _banner.hidden = true;
  app.tunnel = { active: false };
  if (app.chatInput && _origPlaceholder) {
    app.chatInput.placeholder = _origPlaceholder;
  }
}

// ── Network actions ──────────────────────────────────────────────────────────

async function sendLine(text) {
  // Route a keystroke line to the bound program. The backend /send endpoint
  // detects the tunnel binding and types it into the PTY (persisted but kept
  // out of the agent context). We don't add a chat bubble — the embedded
  // terminal echoes the input.
  const sid = app.currentSessionId;
  if (!sid) return;
  try {
    await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        message: text,
        session_id: sid,
        user_id: app.currentUserId,
        agent_id: app.currentAgentId,
      }),
    });
  } catch (_) { /* best-effort; the terminal shows the actual state */ }
}

async function sendKeys(keys) {
  const sid = app.currentSessionId;
  if (!sid) return;
  try {
    await fetch(apiPath('/api/v1/terminal/tunnel/' + encodeURIComponent(sid) + '/keys'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({ keys }),
    });
  } catch (_) { /* best-effort */ }
}

async function handBack() {
  const sid = app.currentSessionId;
  if (!sid) { _unmount(); return; }
  try {
    await fetch(apiPath('/api/v1/terminal/tunnel/' + encodeURIComponent(sid) + '/disable'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
    });
  } catch (_) { /* fall through — tear down locally regardless */ }
  // The server also emits a tunnel_state "ended" event; tearing down here too
  // makes the button feel instant.
  _unmount();
}

// Check the current session's tunnel binding on (re)load and mount if active.
async function refreshForSession() {
  const sid = app.currentSessionId;
  if (!sid) { _unmount(); return; }
  let cfg = null;
  try {
    const r = await fetch(apiPath('/api/v1/terminal/tunnel/' + encodeURIComponent(sid)), {
      headers: { ...authHeaders() },
    });
    if (r.ok) cfg = await r.json();
  } catch (_) { /* ignore */ }
  if (cfg && cfg.enabled) {
    await _mount({
      terminal_session_id: cfg.terminal_session_id,
      command: cfg.command,
      name: cfg.name,
    });
  } else {
    _unmount();
  }
}

export function initChatTunnel() {
  app.tunnel = app.tunnel || { active: false };
  // WS dispatch from agentWs.js lands here.
  app.onTunnelState = (event) => {
    if (!event) return;
    if (event.state === "active") void _mount(event);
    else _unmount();
  };
  // Composer helpers used by chat.js sendMessage when a tunnel is active.
  app.sendTunnelLine = (text) => sendLine(text);
  // Re-evaluate on session open (called from sessions.loadSessionChat).
  app.refreshTunnelForSession = () => refreshForSession();
  // Initial check for the session we boot into.
  setTimeout(() => { try { refreshForSession(); } catch (_) {} }, 400);
}

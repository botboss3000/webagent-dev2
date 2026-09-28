'use strict';

// ── Browser page: live "browser_live" event subscriber ─────────────────────
// REMOVE-WHEN: the Browser page is dropped from the page catalog — this whole
// folder (ui/main-panel/browser/) is a drop-in page and dies with it.
//
// Opens its OWN dedicated, receive-only WebSocket (mode 'user_subscriber') to
// the per-user agent event stream — separate from the shared one in
// ui/shared/js/agentWs.js — so the Browser page can listen for the agent's
// live browser-control telemetry without touching shared code. The agent's
// browser-control ability publishes `browser_live` events (telegraph, click,
// type, nav, scroll, backend, busy, idle) for the page it's driving; this
// module filters to the tab the panel is currently viewing and fans each one
// out to the matching overlay/ticker handler.
//
// Called by ui/main-panel/browser/js/browser.js: startLiveEvents() when the
// Browser page mounts/becomes active, stopLiveEvents() when it deactivates.
// The integrator wires the handlers to ui/main-panel/browser/js/overlay.js
// (telegraph/click/type boxes) and ticker.js (label feed). Connection,
// handshake and reconnect/backoff mirror agentWs.js (simplified).

import { app } from '../../../shared/js/state.js';
import { agentWsUrl } from '../../../shared/js/config.js';
import { getAuthToken } from '../../../shared/js/left-login.js';

// ── Module-level connection state (one socket + one timer at a time) ────────
let ws = null;
let active = false;             // true between start and stop — gates reconnect
let reconnectTimer = null;
let reconnectAttempts = 0;
const INITIAL_RECONNECT_DELAY = 500;
const MAX_RECONNECT_DELAY = 30000;

// Caller-supplied accessor + handler bag for the current run.
let _getCurrentBsId = null;     // () => string|null
let _handlers = {};             // { onTelegraph, onClick, … , onAny }

function cancelReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
}

function scheduleReconnect() {
  if (!active) return;          // stop() was called — don't come back
  if (reconnectTimer) return;   // already pending
  const delay = Math.min(
    INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttempts),
    MAX_RECONNECT_DELAY
  );
  reconnectAttempts++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connect();
  }, delay);
}

// Run a single optional handler defensively — a render error in the overlay or
// ticker must never bubble out and kill the socket.
function safe(fn, ...args) {
  if (typeof fn !== 'function') return;
  try {
    fn(...args);
  } catch (_) {
    /* overlay/ticker render error — non-fatal, keep the socket alive */
  }
}

// Dispatch one validated `browser_live` event to the matching handler with a
// tidy, kind-specific payload. onAny already fired before this is called.
function dispatch(msg) {
  const h = _handlers || {};
  switch (msg.kind) {
    case 'telegraph':
      // Pre-action hint: the agent is ABOUT to act here — outline the target.
      safe(h.onTelegraph, msg.box, msg);
      break;
    case 'click':
      safe(h.onClick, {
        box: msg.box, point: msg.point, label: msg.label, actor: msg.actor,
      }, msg);
      break;
    case 'type':
      safe(h.onType, { box: msg.box, text: msg.text, label: msg.label }, msg);
      break;
    case 'nav':
      safe(h.onNav, { url: msg.url, label: msg.label }, msg);
      break;
    case 'scroll':
      safe(h.onScroll, msg);
      break;
    case 'backend':
      // Server-side / headless step with no on-page geometry — ticker only.
      safe(h.onBackend, { label: msg.label }, msg);
      break;
    case 'busy':
      safe(h.onBusy, msg);
      break;
    case 'idle':
      safe(h.onIdle, msg);
      break;
    default:
      // Unknown kind — onAny already saw it; nothing else to do.
      break;
  }
}

function connect() {
  // No signed-in user → nothing to subscribe to. The caller restarts after
  // login, so just no-op (and don't schedule a reconnect storm).
  if (!active) return;
  if (!app.currentUserId) return;

  // Tear down any prior socket without letting its onclose trigger a reconnect.
  if (ws) {
    try {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      ws.close();
    } catch (_) { /* already closing — ignore */ }
    ws = null;
  }

  let sock;
  try {
    sock = new WebSocket(agentWsUrl());
  } catch (_) {
    scheduleReconnect();
    return;
  }
  ws = sock;

  sock.onopen = () => {
    // Same handshake as the shared subscriber: prove identity with the JWT so
    // the server scopes the broadcast to this user only.
    if (sock.readyState !== WebSocket.OPEN) return;
    try {
      sock.send(JSON.stringify({
        mode: 'user_subscriber',
        user_id: app.currentUserId,
        token: getAuthToken() || '',
      }));
    } catch (_) { /* send raced a close — onclose handles recovery */ }
  };

  sock.onmessage = (ev) => {
    // Never throw out of here — a bad frame or handler must not drop the socket.
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch (_) {
      return;                   // non-JSON / malformed frame
    }
    if (!msg || typeof msg !== 'object') return;

    // Handshake / keepalive noise from the server.
    if (msg.type === 'ping' || msg.type === 'subscribed') {
      if (msg.type === 'subscribed') reconnectAttempts = 0;
      return;
    }
    if (msg.type === 'error') return;

    // We only care about browser-control telemetry.
    if (msg.type !== 'browser_live') return;

    // Filter to the tab the panel is currently viewing. If the caller reports a
    // current bs_id, drop events for any OTHER session (a different tab).
    let curBs = null;
    try {
      curBs = typeof _getCurrentBsId === 'function' ? _getCurrentBsId() : null;
    } catch (_) {
      curBs = null;
    }
    if (curBs && msg.bs_id !== curBs) return;

    // Global tap first (e.g. raw-feed logging), then the per-kind handler.
    safe(_handlers && _handlers.onAny, msg);
    dispatch(msg);
  };

  sock.onclose = () => {
    if (ws === sock) ws = null;
    scheduleReconnect();        // no-ops when inactive (stop() ran)
  };

  sock.onerror = () => {
    // Let onclose drive reconnect — just don't surface the error.
  };
}

// ── Public API ──────────────────────────────────────────────────────────────

// Open (or re-open) the dedicated subscriber and route browser_live events to
// `handlers`. Idempotent: a second call cleanly replaces the previous run's
// accessor, handlers and socket. No-ops harmlessly when no user is signed in;
// the caller should restart after login.
export function startLiveEvents(getCurrentBsId, handlers) {
  _getCurrentBsId = typeof getCurrentBsId === 'function' ? getCurrentBsId : null;
  _handlers = handlers && typeof handlers === 'object' ? handlers : {};

  active = true;
  cancelReconnect();
  connect();
}

// Close the socket, cancel any pending reconnect, and mark inactive so the
// onclose handler does NOT reconnect.
export function stopLiveEvents() {
  active = false;
  cancelReconnect();
  if (ws) {
    try {
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null;
      ws.close();
    } catch (_) { /* already closing — ignore */ }
    ws = null;
  }
}

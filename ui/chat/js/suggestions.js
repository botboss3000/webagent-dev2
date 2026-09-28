'use strict';

// Suggested-reply chips above the chat pill.
//
// Calls the SILENT /api/v1/chat/suggestions endpoint (backed by the
// user-impersonator system agent) and renders the returned messages as
// tappable chips. Tapping a chip drops its text into the pill (editable) and
// focuses it. None of this touches the visible chat, the agent loop, or the WS.
//
// Refresh timing follows the engine's mode (read once at init, refreshed by the
// endpoint response):
//   - "off"        → never fetch, never show chips
//   - "on"         → refresh after each agent reply + on conversation open
//   - "scheduler"  → "on" PLUS refresh after the user sits idle

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import {
  DEFAULT_WEBAGENT_SUGGESTION_PROMPTS,
  getAgentChatUi,
} from '../../shared/js/app-prompts.js';

let _mode = 'on';
let _idleSeconds = 25;
let _idleTimer = null;
let _inFlight = false;
let _row = null;
let _transcriptObserver = null;
let _refreshQueued = false;

function _canChat() {
  // Mirror chat.js gating: only suggest when the visitor can actually chat.
  // A token is present for every visitor allowed to chat (members, per-agent
  // anonymous visitors, and the 'open'-mode auto-admin), so token = can chat.
  try {
    return !!localStorage.getItem('auth_token');
  } catch (_) { return true; }
}

function _clearChips() {
  if (_row) _row.innerHTML = '';
}

function _isEmptyConversation() {
  if (!app.chatMessages) return true;
  return !app.chatMessages.querySelector(
    '.chat-bubble.user:not(.session-placeholder), .chat-bubble.agent:not(.session-placeholder)'
  );
}

function _starterPrompts() {
  const agentId = app.currentAgentId || '';
  let configured;
  try {
    const agents = window.__agentsSharedData && window.__agentsSharedData.agents;
    const agent = Array.isArray(agents) ? agents.find(item => item && item.id === agentId) : null;
    configured = agent?.chat_ui?.chat_common?.suggestion_prompts;
  } catch (_) { /* fall through to the merged profile */ }
  if (!Array.isArray(configured)) {
    configured = getAgentChatUi(agentId)?.chat_common?.suggestion_prompts;
  }
  if (!Array.isArray(configured) && (agentId === 'shared_default' || agentId === 'default')) {
    configured = DEFAULT_WEBAGENT_SUGGESTION_PROMPTS;
  }
  if (!Array.isArray(configured)) return [];
  return configured
    .map(item => String(item || '').trim())
    .filter(Boolean)
    .slice(0, 8);
}

function _syncEmptyState() {
  const panel = document.getElementById('chat-panel');
  const empty = _isEmptyConversation();
  // Keep this semantic state for starter-prompt styling. Positioning is owned
  // solely by the footer profile so an empty session remains bottom-docked.
  if (panel) panel.classList.toggle('chat-empty-session', empty);
  // A session stops being new as soon as its first real transcript bubble is
  // present. This also prevents prompts from returning after a turn completes.
  if (!empty) app.isNewSession = false;
  return empty;
}

function _renderChips(items, kind = 'reply') {
  if (!_row) return;
  _row.innerHTML = '';
  _row.dataset.suggestionKind = kind;
  if (!items || !items.length) return;
  // Don't clobber text the user is actively composing.
  if (app.chatInput && app.chatInput.value.trim()) return;
  for (const text of items) {
    const chip = document.createElement('button');
    chip.type = 'button';
    chip.className = `chat-suggest-chip${kind === 'starter' ? ' chat-starter-prompt' : ''}`;
    chip.textContent = text;
    chip.title = text;
    chip.addEventListener('click', () => _applyChip(text));
    _row.appendChild(chip);
  }
}

function _applyChip(text) {
  if (!app.chatInput) return;
  app.chatInput.value = text;
  // Trigger chat.js listeners (send-button enable, auto-resize, draft save).
  app.chatInput.dispatchEvent(new Event('input', { bubbles: true }));
  app.chatInput.focus();
  _clearChips();
}

async function refreshSuggestions() {
  // Starter prompts are for a session created from the New Session flow only.
  // An existing session can be temporarily empty while its transcript swaps in;
  // it must never display the agent's new-conversation prompts.
  if (_syncEmptyState()) {
    if (app.isNewSession === true) _renderChips(_starterPrompts(), 'starter');
    else _clearChips();
    return;
  }
  if (_mode === 'off') { _clearChips(); return; }
  if (_inFlight) return;
  if (!app.currentUserId) return;
  if (!_canChat()) { _clearChips(); return; }
  // Never compete with an in-progress turn, or with text the user is typing.
  if (app.isProcessing) return;
  if (app.chatInput && app.chatInput.value.trim()) return;
  _inFlight = true;
  try {
    const resp = await fetch(apiPath('/api/v1/chat/suggestions'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: app.currentSessionId || null,
      }),
    });
    if (!resp.ok) { _clearChips(); return; }
    const data = await resp.json().catch(() => ({}));
    // Keep mode/idle in sync with the server in case it changed in the config panel.
    if (data.mode) _mode = data.mode;
    if (typeof data.idle_seconds === 'number') _idleSeconds = data.idle_seconds;
    if (_mode === 'off') { _clearChips(); return; }
    _renderChips(data.suggestions || []);
  } catch (_) {
    _clearChips();
  } finally {
    _inFlight = false;
  }
}

// ── Idle handling (scheduler mode) ──────────────────────────────────────────
function _resetIdleTimer() {
  if (_idleTimer) clearTimeout(_idleTimer);
  if (_mode !== 'scheduler') return;
  _idleTimer = setTimeout(() => {
    // Only refresh when the box is empty and nothing is running.
    if (!app.isProcessing && !(app.chatInput && app.chatInput.value.trim())) {
      refreshSuggestions();
    }
  }, Math.max(5, _idleSeconds) * 1000);
}

function _wireIdleResetters() {
  // Any sign of activity resets the idle countdown.
  ['keydown', 'mousedown', 'touchstart'].forEach(evt => {
    document.addEventListener(evt, _resetIdleTimer, { passive: true });
  });
  // Typing in the pill clears stale chips immediately.
  if (app.chatInput) {
    app.chatInput.addEventListener('input', () => {
      if (app.chatInput.value.trim()) _clearChips();
      else refreshSuggestions();
      queueMicrotask(_syncEmptyState);
    });
    // Footer idle/active mode swaps also rewrite the inline bottom offset.
    // Re-assert centring just after those focus handlers have run.
    app.chatInput.addEventListener('focus', () => queueMicrotask(_syncEmptyState));
    app.chatInput.addEventListener('blur', () => queueMicrotask(_syncEmptyState));
  }
}

function _wireTranscriptObserver() {
  if (!app.chatMessages || _transcriptObserver) return;
  _transcriptObserver = new MutationObserver(() => {
    if (_refreshQueued) return;
    _refreshQueued = true;
    queueMicrotask(() => {
      _refreshQueued = false;
      _syncEmptyState();
      if (!(app.chatInput && app.chatInput.value.trim())) refreshSuggestions();
    });
  });
  _transcriptObserver.observe(app.chatMessages, { childList: true });
}

export async function initSuggestions() {
  _row = document.getElementById('chat-suggest-row');
  if (!_row) return;

  // Expose so chat.js can refresh after a reply finalizes, and clear on send.
  app.refreshSuggestions = refreshSuggestions;
  app.clearSuggestions = _clearChips;

  // Establish the empty-session layout synchronously, before initTabs clears
  // the boot class and measures the FLIP destination in the side panel.
  _wireIdleResetters();
  _wireTranscriptObserver();
  refreshSuggestions();

  // Load current mode/idle from the server.
  try {
    const resp = await fetch(apiPath('/api/v1/chat/suggestions/config'), { headers: { ...authHeaders() } });
    if (resp.ok) {
      const cfg = await resp.json();
      if (cfg.mode) _mode = cfg.mode;
      if (typeof cfg.idle_seconds === 'number') _idleSeconds = cfg.idle_seconds;
    }
  } catch (_) { /* keep defaults */ }

  if (_mode !== 'off') _resetIdleTimer();
  // Reconcile after the server config arrives as well.
  refreshSuggestions();
}

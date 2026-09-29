'use strict';

// Message-send pipeline — outbox retry queue (survives refresh), draft persistence,
// chat gate (sign-in check), stop/interrupt, pill textarea auto-resize + scroll dot.
// Sets app.abortChatStream / app.refreshChat. Module map: ui/chat/js/README.md.

import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { app } from '../../shared/js/state.js';
import {queueSubmission,stopPanel,disconnectPanel,refreshPanel} from '../core/panel.js';
import * as controlledChat from '../core/client.js';
import { apiPath } from '../../shared/js/config.js';
import { addAttachmentsToMessage, getPendingAttachments, renderAttachmentElement } from '../../shared/js/attachments.js';
import { getAccessMode, fetchAccessMode, authHeaders, showLeftOverlay } from '../../shared/js/left-login.js';
import { addChatBubble } from './chat-bubble.js';
import { _addBubbleActions, _getBubbleText } from './chat-bubble-actions.js';
import { startReconcileLoop } from './chat-reconcile.js';
import { _transcriptChangedRemotely } from './chat-message-cache.js';
import { agentChatMsg } from '../../shared/js/app-prompts.js';
import { showLockedControlPopover } from '../../shared/js/chat-pill-config.js';
import { storageAdapter } from './storage/storage-adapter.js';
import browserRouter from './storage/browser-router.js';
import { kvCache } from './storage/kv-cache.js';
import { browserPersistenceAllowed } from '../../shared/js/browser-storage-policy.js';
import { createChatWidget } from '../../chat-widget/js/chat-widget.js';

// After a tenant purge/logout the old tenant's outbox/draft must not be written
// back to the shared localStorage bucket or IndexedDB — the next tenant's boot
// would migrate them into THEIR database (the synchronous mirrors below bypass
// kvCache's own epoch guard, so chat-send keeps its own latch, mirroring
// chat-message-cache). The page reloads after logout, so the latch never needs
// re-arming.
let _purged = false;
let _btwPending = false;
if (typeof window !== 'undefined') {
  window.addEventListener('webagent-browser-storage-purge', () => { _purged = true; });
}

// ── Chat gate ──────────────────────────────────────────────────────────────

function _canChat() {
  // A token is present for every visitor allowed to chat: signed-in members,
  // per-agent anonymous visitors (public agent URLs), and the auto-admin in
  // 'open' mode (localhost). No token ⇒ this app requires sign-in here.
  return !!localStorage.getItem('auth_token');
}

function _wireSignInPopover() {
  if (!app.chatInput || app.chatInput._chatSignInPopoverBound) return;
  app.chatInput._chatSignInPopoverBound = true;
  const show = (event) => {
    if (_canChat()) return;
    if (event.type === 'keydown' && event.key !== 'Enter' && event.key.length !== 1) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    showLockedControlPopover(
      document.getElementById('chat-input-row') || app.chatInput,
      {
        locked_message: 'Please sign in to use the app',
        locked_cta: 'Sign in',
      },
    );
  };
  app.chatInput.addEventListener('click', show, true);
  app.chatInput.addEventListener('keydown', show, true);
}

// The placeholder resolves per-agent: the current agent's Config-tab override
// (metadata.chat_ui) → the app-wide chat_ui default → a built-in fallback.
function applyChatGate() {
  _syncBillingPanel();
  if (!app.chatInput || !app.chatSend) return;
  _wireSignInPopover();
  const billingBlock = app._billingBlocked;
  const billingBlockedHere = billingBlock
    && (!billingBlock.agentId || billingBlock.agentId === app.currentAgentId);
  if (billingBlockedHere) {
    app.chatInput.disabled = true;
    app.chatInput.readOnly = false;
    app.chatInput.removeAttribute('aria-disabled');
    app.chatInput.value = '';
    app.chatInput.placeholder = 'Your trial has ended — pick an option above.';
    app.chatSend.disabled = true;
    _updateInputRowState();
    return;
  }
  const allowed = _canChat();
  if (allowed) {
    if (app._composerLocked) {
      // A blocking built-in command (e.g. /compact) is running synchronously.
      // Keep the pill USABLE so the user can type and queue a message; it will
      // be sent automatically when the command finishes. The Stop button stays
      // live as the escape hatch. Progress (e.g. "Compacting…") is shown on the
      // activity bar above the pill, NOT in the placeholder — the user's
      // in-progress text must stay untouched.
      app.chatInput.disabled = false;
      app.chatInput.readOnly = false;
      app.chatInput.removeAttribute('aria-disabled');
      app.chatSend.disabled = !app.chatInput.value.trim();
      _updateInputRowState();
      return;
    }
    app.chatInput.disabled = false;
    app.chatInput.readOnly = false;
    app.chatInput.removeAttribute('aria-disabled');
    app.chatInput.placeholder = agentChatMsg('pill_placeholder');
    app.chatSend.disabled = !app.chatInput.value.trim();
  } else {
    // Keep the normal placeholder. The read-only input can still receive a
    // click/focus so the sign-in explanation is presented as a popover.
    app.chatInput.disabled = false;
    app.chatInput.readOnly = true;
    app.chatInput.setAttribute('aria-disabled', 'true');
    app.chatInput.value = '';
    app.chatInput.placeholder = agentChatMsg('pill_placeholder');
    app.chatSend.disabled = true;
  }
  _updateInputRowState();
}
// Exposed so the session modules can re-apply the gate (and thus refresh the
// per-agent placeholder) when the user switches agents or starts a new session.
app.applyChatGate = applyChatGate;

window.addEventListener('webagent-billing-blocked', (event) => {
  const detail = event?.detail || {};
  if (detail.reason !== 'trial_expired') return;
  app._billingBlocked = {
    agentId: detail.agentId || app.currentAgentId || null,
    reason: detail.reason,
    accepted: detail.accepted || null,
  };
  applyChatGate();
});

// ── Trial-ended billing panel ─────────────────────────────────────────────
// When a user's trial is exhausted the composer is disabled and this panel
// appears above the pill: a "Buy credits" button (opens the buy-credits modal,
// which offers the agent's accepted processors — Bitcoin in this deployment)
// and a "Continue using for free" button that expands info about bringing your
// own LLM. Hidden whenever the current agent is not trial-blocked.
function _syncBillingPanel() {
  const panel = document.getElementById('chat-billing-panel');
  if (!panel) return;
  const billingBlock = app._billingBlocked;
  const blockedHere = billingBlock
    && billingBlock.reason === 'trial_expired'
    && (!billingBlock.agentId || billingBlock.agentId === app.currentAgentId);
  if (!blockedHere) {
    panel.hidden = true;
    panel.innerHTML = '';
    return;
  }
  const agentKey = billingBlock.agentId || '';
  if (panel.dataset.renderedFor === agentKey && panel.innerHTML) return;
  panel.dataset.renderedFor = agentKey;
  panel.innerHTML = '';
  panel.hidden = false;

  const card = _bEl('div', { class: 'billing-panel-card' });
  card.appendChild(_bEl('div', { class: 'billing-panel-title' }, 'Your trial has ended'));
  card.appendChild(_bEl('div', { class: 'billing-panel-desc' },
    'This agent’s free trial is over. Pick how you’d like to keep chatting.'));

  card.appendChild(_bEl('button', {
    type: 'button',
    class: 'billing-panel-btn billing-panel-btn-primary',
    onclick: () => window.AppBilling.openBuyCreditsModal({
      agentId: billingBlock.agentId,
      reason: 'trial_expired',
      accepted: billingBlock.accepted,
    }),
  }, 'Buy credits'));

  card.appendChild(_bEl('button', {
    type: 'button',
    class: 'billing-panel-btn',
    onclick: () => { info.hidden = !info.hidden; },
  }, 'Continue using for free'));

  const info = _bEl('div', { class: 'billing-panel-info', hidden: true });
  info.appendChild(_bEl('div', { class: 'billing-panel-info-title' }, 'Use your own LLM — free'));
  info.appendChild(_bEl('div', { class: 'billing-panel-info-text' },
    'You can keep chatting with this agent for free by bringing your own LLM. ' +
    'Connect your own model provider (OpenAI, Anthropic, or any OpenAI-compatible API) ' +
    'with your own key, and your messages run on your key instead of this agent’s credits. ' +
    'Use the model picker in the chat footer to switch to your own model.'));
  info.appendChild(_bEl('div', { class: 'billing-panel-info-text', style: { marginTop: '8px' } },
    'Once your key is connected — or after buying credits — press “Check again” below.'));
  card.appendChild(info);

  // Re-evaluate access without sending a message. The composer is disabled while
  // blocked, so this is the recovery path after the user buys credits (balance
  // > 0) or connects their own LLM key (own-llm allow). On success the block is
  // cleared and the gate re-applied, re-enabling the composer.
  const checkBtn = _bEl('button', {
    type: 'button',
    class: 'billing-panel-btn billing-panel-btn-ghost',
  }, 'Check again');
  checkBtn.addEventListener('click', async () => {
    checkBtn.disabled = true;
    checkBtn.textContent = 'Checking…';
    try {
      const decision = await window.AppBilling.checkAccess(billingBlock.agentId);
      if (decision && decision.allow) {
        app._billingBlocked = null;
      } else {
        // Still blocked: re-assert the current block so the panel stays put.
        app._billingBlocked = billingBlock;
      }
    } catch (_) {
      app._billingBlocked = billingBlock; // couldn't check — keep blocking
    } finally {
      checkBtn.disabled = false;
      checkBtn.textContent = 'Check again';
      applyChatGate();
    }
  });
  card.appendChild(checkBtn);

  panel.appendChild(card);
}

// Minimal DOM builder for the billing panel (kept local — chat-send.js does
// not import the shared _el helper).
function _bEl(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === 'class') e.className = v;
    else if (k === 'hidden') { if (v) e.hidden = true; }
    else if (k.startsWith('on') && typeof v === 'function') e.addEventListener(k.slice(2), v);
    else if (v === false || v == null) continue;
    else e.setAttribute(k, v);
  }
  for (const c of children) {
    if (c == null) continue;
    e.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
  }
  return e;
}

// Built-in commands that run SYNCHRONOUSLY on the server (the /chat/send POST
// blocks until they finish) and so should lock the composer until they reply.
// Mirror of app/api/chat.py _is_compact_command (`/compact` + optional args,
// rejecting `/compactfoo`). Also covers /optimize and other slash commands.
// The Stop button stays live as the escape hatch.
function _isBlockingCommand(text) {
  return /^\/compact(?:\s+.*)?$/i.test((text || '').trim());
}

// Slash commands whose result is persisted as a system interaction — the
// reconcile loop will render the permanent info bubble; skip the transient one.
function _isSystemSlashCommand(text) {
  return /^\/[a-zA-Z]/.test((text || '').trim());
}

// Messages typed while a blocking command (e.g. /compact) is running are
// deferred DURABLY, not held in memory: _queueMessageDuringCommand writes the
// entry to the outbox (IndexedDB + localStorage mirror — survives refresh and
// tab close) and surfaces it as a pending "You (queued…)" bubble. It is POSTed
// when the command finishes; if the server is still compacting at that point
// (e.g. after a reload), the server persists it as status='queued' with
// metadata queued_for='compact' and runs it when the compaction drains — so a
// server restart in between cannot lose it either (boot recovery drains the
// leftover rows). Stop/abort discards deferred entries via _discardDeferredMessages.

// A message sent while a blocking command (e.g. /compact) is running is
// surfaced as a "You (queued…)" bubble — the same pending look used for
// gate-queued messages — instead of an announcement in the pill placeholder.
// At most one deferred message is shown at a time; a newer send replaces the
// older bubble (mirrors the outbox holding at most one deferred entry).
function _renderQueuedBubble(text) {
  if (!app.chatMessages) return;
  app.chatMessages.querySelectorAll(':scope > .chat-bubble.user[data-blocked-queued]')
    .forEach(el => el.remove());
  const bubble = addChatBubble('user', text);
  if (!bubble) return;
  bubble.setAttribute('data-blocked-queued', '1');
  bubble.classList.add('pending', 'saving');
  const label = bubble.querySelector('.label');
  if (label) label.textContent = 'You (queued\u2026)';
}

// Defer a message typed while a blocking command (e.g. /compact) is running.
// The entry goes into the durable outbox with defer_while_locked so the poll
// holds it until the composer unlocks; the pending bubble is rendered now and
// the pill is cleared (the message is queued — not still being typed).
function _queueMessageDuringCommand(text) {
  const entry = {
    id: 'msg_' + Date.now() + '_' + (++_outboxIdCounter),
    text: String(text || ''),
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
    agent_id: app.currentAgentId,
    timestamp: new Date().toISOString(),
    defer_while_locked: true,  // skip the outbox poll until the command finishes
  };
  if (app.targetDevice) entry.target_device = app.targetDevice;
  _addToOutbox(entry);
  _renderQueuedBubble(text);
  if (app.chatInput) {
    app.chatInput.value = '';
    if (app.chatSend) app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
  }
  _clearDraft();
}

function _setOfflineComposerNotice(message) {
  const notice = document.getElementById('offline-reader-composer-notice');
  if (notice) notice.textContent = message;
}

function _renderOfflineQueuedBubble(entry) {
  if (!app.chatMessages || !entry?.id) return null;
  const existing = app.chatMessages.querySelector(
    `:scope > .chat-bubble.user[data-pending-id="${CSS.escape(entry.id)}"]`,
  );
  if (existing) return existing;
  const bubble = addChatBubble(
    'user', entry.text, undefined, undefined, undefined, undefined, entry.timestamp,
  );
  if (!bubble) return null;
  bubble.setAttribute('data-pending-id', entry.id);
  bubble.setAttribute('data-offline-queued', '1');
  bubble.classList.add('pending', 'saving');
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (queued offline…)';
  return bubble;
}

function _queueMessageOffline(text, textOverride) {
  if (app.tunnel?.active || app.terminalChat?.active) {
    _setOfflineComposerNotice('Offline · terminal commands cannot be queued');
    return false;
  }
  if (!app.currentUserId || !app.currentAgentId || !app.currentSessionId) {
    _setOfflineComposerNotice('Offline · open a cached session before queuing a message');
    return false;
  }
  if (String(app.currentSessionId).startsWith('codex:')) {
    _setOfflineComposerNotice('Offline · Codex task messages cannot be queued safely');
    return false;
  }
  if (getPendingAttachments().length > 0) {
    _setOfflineComposerNotice('Offline · remove attachments before queuing this message');
    return false;
  }
  const entry = {
    id: 'msg_' + Date.now() + '_' + (++_outboxIdCounter),
    text: String(text || ''),
    session_id: app.currentSessionId,
    user_id: app.currentUserId,
    agent_id: app.currentAgentId,
    execution_mode: app.executionMode || 'ask',
    timestamp: new Date().toISOString(),
    queued_offline: true,
    transport: storageAdapter.isBrowser ? 'browser' : 'server',
  };
  if (app.targetDevice) entry.target_device = app.targetDevice;
  _addToOutbox(entry);
  _renderOfflineQueuedBubble(entry);
  _recordPersistence(entry.id, 'queued', 'Queued offline; waiting for confirmed server reconnection.');
  if (typeof textOverride !== 'string' && app.chatInput) {
    app.chatInput.value = '';
    if (app.chatSend) app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
    _clearDraft();
    if (typeof app.dismissMobileComposerAfterSend === 'function') {
      app.dismissMobileComposerAfterSend();
    }
    if (typeof app.clearSuggestions === 'function') {
      try { app.clearSuggestions(); } catch (_) {}
    }
  }
  _setOfflineComposerNotice('Offline · message queued and will send after reconnect');
  return true;
}

// Stop/abort escape hatch: drop messages deferred behind a blocking command
// (mirrors the old "Stop clears the queued message" behaviour).
function _discardDeferredMessages() {
  const q = _readOutbox();
  const kept = q.filter(e => !e.defer_while_locked);
  if (kept.length !== q.length) _writeOutbox(kept);
  if (app.chatMessages) {
    app.chatMessages.querySelectorAll(':scope > .chat-bubble.user[data-blocked-queued]')
      .forEach(el => el.remove());
  }
}

// Composer lock + "Compacting…" progress, driven by the server's agent_status
// 'compacting'/'compact_done' broadcasts or by transcript restore (a durable
// queued_for='compact' row). Uses the same _composerLocked flag as the live
// /compact path, so the pill stays usable (type + queue) with Stop live as the
// escape hatch.
app._lockComposerForCompaction = () => {
  if (typeof app.showCompactionProgress === 'function') {
    app.showCompactionProgress();
  }
  if (app._composerLocked) return;
  app._composerLocked = true;
  applyChatGate();
  if (app.chatActivityStart) app.chatActivityStart('Compacting...');
};
app._unlockComposerForCompaction = () => {
  if (typeof app.clearCompactionProgress === 'function') {
    app.clearCompactionProgress();
  }
  if (!app._composerLocked) return;
  app._composerLocked = false;
  applyChatGate();
  if (app.chatActivityStop) app.chatActivityStop();
  // A compaction just finished — flush any deferred messages now.
  _flushOutbox().catch(() => { /* retried by the outbox poll */ });
};

// Register a server-persisted compaction-queued message (status='queued',
// metadata queued_for='compact') in the gate-queue state so the 'running'
// broadcast clears it, stamp the pending bubble with the real turn id, and
// lock the composer until the drain runs. On a fresh page there may be no
// deferred bubble in the DOM (the entry was flushed by the outbox poll) — one
// is created here; the reconcile loop adopts it by id when it renders the row.
function _stampCompactionQueued(entry, turnId) {
  const sid = String(entry.session_id || app.currentSessionId || '');
  if (turnId) {
    _gateQueueBySession.set(sid, {
      turnId,
      queuePosition: null,
      text: String(entry.text || ''),
    });
    let qb = app.chatMessages
      ? app.chatMessages.querySelector(':scope > .chat-bubble.user[data-blocked-queued]')
      : null;
    if (!qb && sid === String(app.currentSessionId || '') && entry.text && app.chatMessages) {
      qb = addChatBubble('user', String(entry.text || ''));
      if (qb) {
        qb.setAttribute('data-blocked-queued', '1');
        qb.classList.add('pending', 'saving');
        const lbl = qb.querySelector('.label');
        if (lbl) lbl.textContent = 'You (queued\u2026)';
      }
    }
    if (qb) {
      qb.setAttribute('data-msg-id', String(turnId));
      qb.setAttribute('data-gate-queued', '1');
    }
  }
  // Only lock the composer when the queued message belongs to the session
  // currently on screen — a deferred entry flushed from another session (user
  // switched away mid-compaction) must not lock the wrong view.
  if (sid === String(app.currentSessionId || '')
      && typeof app._lockComposerForCompaction === 'function') {
    try { app._lockComposerForCompaction(); } catch (_) { /* ignore */ }
  }
}

// Lock the pill while a blocking built-in runs; Stop stays clickable. The pill
// keeps its normal placeholder and the user's in-progress text untouched —
// progress (e.g. "Compacting…") is shown on the activity bar above the pill.
function _lockComposerForCommand() {
  app._composerLocked = true;
  applyChatGate();
}
function _unlockComposer() {
  if (typeof app.clearCompactionProgress === 'function') {
    app.clearCompactionProgress();
  }
  if (!app._composerLocked) return;
  app._composerLocked = false;
  applyChatGate();
  // Any bubble queued behind the blocking command is stale the moment the
  // composer unlocks: the deferred message is sent below (the normal send
  // pipeline renders a fresh bubble) or the user stopped/aborted and the
  // queue was discarded. Drop it either way — never leave a phantom "queued"
  // bubble in the transcript.
  if (app.chatMessages) {
    app.chatMessages.querySelectorAll(':scope > .chat-bubble.user[data-blocked-queued]')
      .forEach(el => el.remove());
  }
  // Send messages deferred behind the blocking command (durable outbox entries
  // written by _queueMessageDuringCommand). Fire-and-forget: the POST happens
  // here; the reply arrives via the normal WS / reconcile path. If the server
  // is somehow still compacting it answers 'queued' and the drain runs it.
  _flushOutbox().catch(() => { /* retried by the outbox poll */ });
}
app._unlockComposer = _unlockComposer;

// The "new session" / "welcome" / "switched agent" bubbles are empty-state
// PLACEHOLDERS (marked `.session-placeholder` where they're rendered in
// session-init.js / session-load.js). They stand in for a conversation that
// hasn't started yet — so the instant the user sends their first message they're
// stale and must be cleared, leaving the session to read as a clean start
// (their message, then the reply). A later full DB re-render never re-adds them
// once the session has real turns, so this only matters on the optimistic path.
function _removeSessionPlaceholders() {
  if (!app.chatMessages) return;
  app.chatMessages
    .querySelectorAll(':scope > .chat-bubble.session-placeholder')
    .forEach(el => el.remove());
}

function _updateInputRowState() {
  if (!app.chatInput) return;
  const row = document.getElementById('chat-input-row');
  if (!row) return;
  const hasText = !!app.chatInput.value.trim();
  row.classList.toggle('has-text', hasText);
}

// ── session_seq persistence ──
// Migrated from the localStorage blob 'webagent.lastSessionSeq.v1' (a map of
// sessionId → seq) to one app_cache row per session ('chat:lastSeq:<sessionId>').
// The module-scope load below runs before storage hydration, so kvCache reads
// the registered legacy blob until the migration copies it into IndexedDB.
kvCache.registerLegacyMap('webagent.lastSessionSeq.v1', 'chat:lastSeq:');
function _persistLastSessionSeq() {
  kvCache.setAll('chat:lastSeq:', app.lastSessionSeq || {});
}
function _loadLastSessionSeq() {
  const parsed = kvCache.getAll('chat:lastSeq:');
  if (parsed && typeof parsed === 'object' && Object.keys(parsed).length) {
    app.lastSessionSeq = { ...(app.lastSessionSeq || {}), ...parsed };
  }
}
_loadLastSessionSeq();

// Boot-time load runs before hydration; re-merge persisted rows once storage
// is ready, preferring any newer in-memory values captured this page's lifetime.
if (typeof window !== 'undefined') {
  window.addEventListener('webagent-kv-cache-hydrated', () => {
    const parsed = kvCache.getAll('chat:lastSeq:');
    app.lastSessionSeq = { ...parsed, ...(app.lastSessionSeq || {}) };
  });
}

// ── Outgoing message queue (outbox) ─────────────────────────────────
// Migrated from the localStorage blob 'webagent.pendingMessages.v1' to a single
// app_cache row 'chat:outbox' (the whole queue — order matters for FIFO flush).
// registerLegacyValue keeps boot-time reads working until hydration migrates the
// blob; the synchronous localStorage mirror below preserves the old crash-
// durability guarantee (an async IndexedDB flush can be lost if the tab dies
// first), and migrateLegacyValue treats any surviving mirror as authoritative
// on the next boot.
const _OUTBOX_LS_KEY = 'webagent.pendingMessages.v1';
const _OUTBOX_CACHE_KEY = 'chat:outbox';
kvCache.registerLegacyValue(_OUTBOX_LS_KEY, _OUTBOX_CACHE_KEY, { parseJson: true });
// Persistence receipts were migrated to app_cache rows ('chat:receipt:<msgId>')
// with a 24h TTL.
const _PERSISTENCE_RECEIPTS_TTL_MS = 24 * 60 * 60 * 1000;
let _outboxIdCounter = 0;
// Queue status travels over the websocket independently of the send response.
// Keep the small amount of state needed to reconnect the two paths: an event
// can beat the acknowledgement, a reconcile can replace the optimistic node,
// or an ephemeral/GenUI send may not have created a normal user bubble at all.
const _gateQueueBySession = new Map();
const _recentOutgoingBySession = new Map();

function _rememberOutgoingMessage(sessionId, text, bubble = null) {
  if (!sessionId) return;
  _recentOutgoingBySession.set(String(sessionId), { text: String(text || ''), bubble });
}

function _queuedStateFor(sessionId, turnId) {
  const state = _gateQueueBySession.get(String(sessionId || ''));
  return state && (!turnId || !state.turnId || String(state.turnId) === String(turnId))
    ? state : null;
}
// Entries currently being POSTed by this page. The recovery poll must not retry
// a request merely because its first acknowledgement is taking longer than the
// five-second poll interval.
const _outboxInFlight = new Set();

function _recordPersistence(id, state, detail) {
  try {
    const current = kvCache.get('chat:receipt:' + id);
    const events = Array.isArray(current?.events) ? current.events : [];
    events.push({ state, detail, at: new Date().toISOString() });
    // Retain a compact audit trail for recent messages without allowing this
    // browser-side recovery aid to grow without bound. TTL sweeps stale rows.
    kvCache.set('chat:receipt:' + id, { events: events.slice(-12) }, { ttlMs: _PERSISTENCE_RECEIPTS_TTL_MS });
  } catch (_) { /* receipts are a recovery aid, never a send blocker */ }
}

function _persistenceDetails(id) {
  try {
    const current = kvCache.get('chat:receipt:' + id);
    return Array.isArray(current?.events) ? current.events : [];
  } catch (_) { return []; }
}

function _readOutbox() {
  const q = kvCache.get(_OUTBOX_CACHE_KEY);
  // Never silently discard unsaved user text. The explicit dismiss control is
  // the only removal path other than a durable server acknowledgement.
  return Array.isArray(q) ? q : [];
}

function _writeOutbox(queue) {
  if (_purged) return; // tenant gone — never write old-tenant outbox to the next tenant
  const q = Array.isArray(queue) ? queue : [];
  if (q.length === 0) kvCache.del(_OUTBOX_CACHE_KEY);
  else kvCache.set(_OUTBOX_CACHE_KEY, q);
  // Synchronous crash-durability mirror (see header). Skipped in
  // memory_only/disabled where nothing persists anyway.
  if (!browserPersistenceAllowed()) return;
  try {
    if (q.length === 0) localStorage.removeItem(_OUTBOX_LS_KEY);
    else localStorage.setItem(_OUTBOX_LS_KEY, JSON.stringify(q));
  } catch (_) { /* quota / private mode — non-fatal */ }
}

function _addToOutbox(entry) {
  const q = _readOutbox();
  q.push(entry);
  _writeOutbox(q);
  _recordPersistence(entry.id, 'queued', 'Saved in this browser recovery queue; waiting for database confirmation.');
  _startOutboxPoll();
}

function _removeFromOutbox(id) {
  const q = _readOutbox().filter(e => e.id !== id);
  _writeOutbox(q);
  if (q.length === 0) _stopOutboxPoll();
}

function _updateOutboxEntry(id, patch) {
  _writeOutbox(_readOutbox().map(e => e.id === id ? { ...e, ...patch } : e));
}

function _outboxHasPending() {
  const q = _readOutbox();
  return q.length > 0;
}

async function _retryEntry(entry, manual = false) {
  // Reachability alone is insufficient: cached-reader mode clears only after
  // authoritative permission/catalog reconciliation has completed.
  if (window.__webagentOfflineReadOnly === true) return false;
  if (entry.manual_only && !manual) return false;
  // Deferred entries (typed while a blocking command like /compact is running)
  // are held until the command finishes — the composer lock is the gate. A
  // manual retry always goes through.
  if (entry.defer_while_locked && !manual && app._composerLocked) return false;
  if (_outboxInFlight.has(entry.id)) return false;
  _outboxInFlight.add(entry.id);
  try {
    _recordPersistence(entry.id, 'retrying', manual ? 'Manual database-save retry requested.' : 'Automatic database-save retry started.');
    if (entry.transport === 'browser') {
      await browserRouter.sendMessage(entry.session_id, entry.text, {
        userInteractionId: entry.id,
        idempotencyKey: entry.id,
      });
      _removeFromOutbox(entry.id);
      _recordPersistence(entry.id, 'saved', 'Offline-queued browser message completed after reconnect.');
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => _markBubbleSaved(el, entry.id, entry.id));
      if (app.currentSessionId === entry.session_id && typeof app.loadSessionChat === 'function') {
        await app.loadSessionChat(entry.session_id, { refresh: true });
      }
      return true;
    }
    const payload = {
      message: entry.text,
      session_id: entry.session_id || app.currentSessionId,
      user_id: entry.user_id || app.currentUserId,
      execution_mode: entry.execution_mode || app.executionMode || 'ask',
      // Idempotency key: the outbox entry id rides along on every retry so the
      // server can recognise a re-send of an already-accepted message and skip
      // the duplicate insert / second run (see app/api/chat.py _find_interaction_by_cmid).
      client_msg_id: entry.id,
    };
    if (entry.agent_id || app.currentAgentId) {
      payload.agent_id = entry.agent_id || app.currentAgentId;
    }
    if (entry.target_device) payload.target_device = entry.target_device;
    // RECORDER-HOOK: plugins/recorder/frontend/toggle.js subscribes backend evidence before send.
    // Removal: delete this try block and comment; payload/send behavior remains unchanged.
    try { if (window.__recorderScope) await window.__recorderScope(payload.session_id,payload.agent_id); } catch (_) {}
    const resp = await fetch(apiPath('/api/v1/chat/send'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify(payload),
    });
    const registrationDetail = await _anonymousRegistrationDetail(resp);
    if (registrationDetail) {
      _removeFromOutbox(entry.id);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => el.remove());
      _showAnonymousRegistrationRequired(registrationDetail.message);
      return false;
    }
    if (resp.status === 402) {
      // Billing denial is permanent until account state changes. Retrying it
      // would reopen the paywall on every recovery poll.
      _removeFromOutbox(entry.id);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => el.remove());
      return false;
    }
    if (resp.ok) {
      const data = await resp.json().catch(() => ({}));
      // The message is durably accepted. Move its session now instead of
      // repainting the stale hybrid cache and waiting for background sync.
      app.markSessionRecentlyActive?.(entry.session_id, { persist: true });
      if (data && data.status === 'queued') {
        // The server accepted the message but a compaction is still running —
        // it persisted the row durably as status='queued' (queued_for='compact')
        // and will start the turn when the compaction drains. The outbox copy is
        // no longer needed; keep the pending bubble + composer lock until the
        // drain's 'running' broadcast clears it.
        _removeFromOutbox(entry.id);
        _recordPersistence(entry.id, 'queued', 'Persisted as queued (compaction in progress); runs when compaction finishes.');
        _stampCompactionQueued(entry, data.turn_id);
        return true;
      }
      _removeFromOutbox(entry.id);
      _recordPersistence(entry.id, 'saved', `Database acknowledged the message${data.turn_id ? ` as ${data.turn_id}` : ''}.`);
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => {
          _markBubbleSaved(el, data && data.turn_id, entry.id);
          if (data && data.turn_id) {
            el.setAttribute('data-msg-id', data.turn_id);
          }
        });
      // A deferred message that was finally sent resolves its queued bubble.
      if (app.chatMessages) {
        app.chatMessages.querySelectorAll(':scope > .chat-bubble.user[data-blocked-queued]')
          .forEach(el => {
            _markBubbleSaved(el, data && data.turn_id, entry.id);
            if (data && data.turn_id) el.setAttribute('data-msg-id', data.turn_id);
          });
      }
      if (typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      return true;
    }
    document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
      .forEach(el => _convertBubbleToPending(el, entry, `Save failed (${resp.status}) — retry`));
    _recordPersistence(entry.id, 'error', `Database save returned HTTP ${resp.status}.`);
    // Permanent client errors are paused for manual retry; the message is never
    // silently discarded from the browser recovery journal.
    if ([400, 401, 403, 404, 409, 410, 422].includes(resp.status)) {
      _updateOutboxEntry(entry.id, { manual_only: true });
      document.querySelectorAll(`.chat-bubble.user.pending[data-pending-id="${CSS.escape(entry.id)}"]`)
        .forEach(el => _convertBubbleToPending(el, entry, `Save failed (${resp.status}) — retry`));
    }
  } catch (error) {
    _recordPersistence(entry.id, 'error', `Database save could not be reached: ${error?.message || 'network error'}.`);
  } finally {
    _outboxInFlight.delete(entry.id);
  }
  return false;
}

async function _anonymousRegistrationDetail(response) {
  if (!response || response.status !== 429) return null;
  try {
    const body = await response.clone().json();
    const detail = body && body.detail;
    return detail && detail.code === 'registration_required' ? detail : null;
  } catch (_) {
    return null;
  }
}

function _showAnonymousRegistrationRequired(message) {
  const text = message || 'Anonymous chat is unavailable. Register and sign in to continue.';
  addChatBubble('agent', text, 'info');
  showLeftOverlay();
}

async function _flushOutbox() {
  const queue = _readOutbox();
  if (queue.length === 0) return 0;
  if (!app.currentUserId) return 0;
  let ok = 0;
  for (const entry of queue) {
    if (await _retryEntry(entry)) ok++;
  }
  return ok;
}

function _renderPendingBubble(entry) {
  // Pending/retry bubbles are debug UI — hidden when debug is off
  if (!app.isDebug) return;
  const bubble = document.createElement('div');
  bubble.className = `chat-bubble user pending${entry.manual_only ? ' save-error' : ''}`;
  bubble.setAttribute('data-pending-id', entry.id);
  const label = document.createElement('span');
  label.className = 'label';
  label.textContent = entry.manual_only ? 'You (save failed — retry)' : 'You (saving / retrying)';
  bubble.appendChild(label);
  const body = document.createElement('div');
  body.className = 'bubble-body';
  body.textContent = entry.text;
  bubble.appendChild(body);
  _appendPendingActions(bubble, entry);
  _refreshLucideIcons(bubble);
  if (app.chatMessages) {
    app.chatMessages.appendChild(bubble);
    // scroll handled by caller or chat-ui
  }
  return bubble;
}

function _convertBubbleToPending(bubble, entry, labelText = 'You (save failed — retry)') {
  // Pending/retry bubbles are debug UI — silently remove the bubble when debug is off.
  // The outbox still tracks the entry and retries in the background.
  if (!app.isDebug) {
    if (bubble) bubble.remove();
    return;
  }
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = labelText;
  bubble.className = 'chat-bubble user pending save-error';
  bubble.setAttribute('data-pending-id', entry.id);
  bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
  bubble.removeAttribute('data-msg-id');
  bubble.removeAttribute('data-turn-id');
  _appendPendingActions(bubble, entry);
  _refreshLucideIcons(bubble);
}

function _markBubbleSaving(bubble, entry) {
  if (!bubble) return;
  if (!app.isDebug) return;
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (saving…)';
  bubble.classList.add('pending', 'saving');
  bubble.setAttribute('data-pending-id', entry.id);
}

function _attachPersistenceDetails(bubble, messageId) {
  if (!bubble || !messageId) return;
  const label = bubble.querySelector('.label');
  if (!label) return;
  label.classList.add('persistence-status');
  label.tabIndex = 0;
  label.setAttribute('role', 'button');
  label.title = 'Show message persistence details';
  const existing = bubble.querySelector('.persistence-details');
  if (existing) existing.remove();
  const details = document.createElement('div');
  details.className = 'persistence-details';
  const events = _persistenceDetails(messageId);
  details.textContent = events.length
    ? events.map(e => `${new Date(e.at).toLocaleTimeString()}: ${e.detail}`).join('\n')
    : 'No browser-side persistence details are available for this message.';
  bubble.appendChild(details);
  const toggle = () => details.classList.toggle('open');
  label.onclick = toggle;
  label.onkeydown = (event) => {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      toggle();
    }
  };
}

function _markBubbleSaved(bubble, turnId, messageId = turnId) {
  if (!bubble) return;
  const provisionalTurnId = bubble.getAttribute('data-msg-id') || '';
  // The durable acknowledgement is the atomic ownership boundary. Stamp the
  // optimistic node and remove any reconcile-created durable twin BEFORE the
  // activity group is rekeyed/repositioned, so no frame can show two users.
  if (turnId) {
    bubble.setAttribute('data-msg-id', String(turnId));
    const selector = `:scope > .chat-bubble.user[data-msg-id="${CSS.escape(String(turnId))}"]`;
    Array.from(app.chatMessages?.querySelectorAll(selector) || []).forEach((other) => {
      if (other !== bubble) other.remove();
    });
  }
  if (!app.isDebug) {
    // Preserve transcript presentation state (including configured user-message
    // truncation) when the optimistic bubble becomes durable.
    bubble.classList.remove('pending', 'saving', 'save-error');
    bubble.classList.add('saved');
    bubble.removeAttribute('data-pending-id');
    bubble.querySelectorAll('.label').forEach(el => el.remove());
    bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
    if (turnId) bubble.setAttribute('data-msg-id', turnId);
    if (turnId && provisionalTurnId && typeof app.rekeyActivityGroupOwner === 'function') {
      app.rekeyActivityGroupOwner(provisionalTurnId, turnId);
    }
    _addBubbleActions(bubble);
    _restoreGateQueueBubble(bubble, turnId);
    return;
  }
  bubble.classList.remove('pending', 'saving', 'save-error');
  bubble.classList.add('saved');
  bubble.removeAttribute('data-pending-id');
  const label = bubble.querySelector('.label');
  if (label) label.textContent = 'You (saved)';
  bubble.querySelectorAll('.bubble-actions').forEach(el => el.remove());
  if (turnId) bubble.setAttribute('data-msg-id', turnId);
  if (turnId && provisionalTurnId && typeof app.rekeyActivityGroupOwner === 'function') {
    app.rekeyActivityGroupOwner(provisionalTurnId, turnId);
  }
  _addBubbleActions(bubble);
  _attachPersistenceDetails(bubble, messageId);
  _restoreGateQueueBubble(bubble, turnId);
}

function _restorePersistenceStatus(bubble, messageId) {
  if (!bubble || !messageId || _persistenceDetails(messageId).length === 0) return;
  if (!app.isDebug) return;
  bubble.classList.add('saved');
  const label = bubble.querySelector('.label');
  if (label) label.textContent = 'You (saved)';
  _attachPersistenceDetails(bubble, messageId);
}

function _appendPendingActions(bubble, entry) {
  if (!app.isDebug) return;
  // Standard footer (collapse / speaker / copy) — same as every other bubble.
  _addBubbleActions(bubble);
  let actions = bubble.querySelector(':scope > .bubble-actions');
  if (!actions) {
    actions = document.createElement('div');
    bubble.appendChild(actions);
  }
  actions.classList.add('bubble-actions', 'pending-actions');
  const retryBtn = document.createElement('button');
  retryBtn.type = 'button';
  retryBtn.className = 'bubble-action-btn pending-retry';
  retryBtn.innerHTML = '<i data-lucide="refresh-cw" style="width:14px;height:14px;"></i>';
  retryBtn.title = 'Retry database save';
  retryBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    retryBtn.disabled = true;
    retryBtn.innerHTML = '<span style="font-size:12px;">\u21BB</span>';
    const ok = await _retryEntry(entry, true);
    if (!ok) {
      retryBtn.disabled = false;
      retryBtn.innerHTML = '<i data-lucide="refresh-cw" style="width:14px;height:14px;"></i>';
      if (window.lucide && typeof window.lucide.createIcons === 'function') {
        try { window.lucide.createIcons({ nodes: [retryBtn.querySelector('[data-lucide]')] }); } catch (_) {}
      }
    }
  });
  actions.appendChild(retryBtn);
  const dismissBtn = document.createElement('button');
  dismissBtn.type = 'button';
  dismissBtn.className = 'bubble-action-btn pending-dismiss';
  dismissBtn.innerHTML = '<i data-lucide="x" style="width:14px;height:14px;"></i>';
  dismissBtn.title = 'Dismiss (discard this unsaved message)';
  dismissBtn.addEventListener('click', (e) => {
    e.stopPropagation();
    _removeFromOutbox(entry.id);
    bubble.remove();
  });
  actions.appendChild(dismissBtn);
}

let _outboxPollTimer = null;
function _startOutboxPoll() {
  if(_outboxPollTimer) return;
  _outboxPollTimer=setInterval(()=>{
    if(app.currentUserId) controlledChat.initialize(app.currentUserId)
      .then(()=>controlledChat.drainOutbox()).catch(()=>{});
  },5000);
}
function _stopOutboxPoll() {
  if (_outboxPollTimer) {
    clearInterval(_outboxPollTimer);
    _outboxPollTimer = null;
  }
}

function _renderPendingBubbles() {
  // Pending rows belong to the controlled chat projection.
}

// The V2 send path still uses the composer draft cache until its own draft
// store is installed. Keep these keys defined so clearing an accepted send
// cannot throw after the message has already entered the durable outbox.
const _DRAFT_LS_KEY = 'webagent.chatDraft.v1';
const _DRAFT_CACHE_KEY = 'chat:draft';
kvCache.registerLegacyValue(_DRAFT_LS_KEY, _DRAFT_CACHE_KEY, { parseJson: false });

function _saveDraft() {
  _debouncedSaveDraft();
}
function _clearDraft() {
  if (_draftTimer) { clearTimeout(_draftTimer); _draftTimer = null; }
  if (_purged) return; // tenant gone — never write old-tenant draft to the next tenant
  kvCache.del(_DRAFT_CACHE_KEY);
  try { localStorage.removeItem(_DRAFT_LS_KEY); } catch (_) { /* non-fatal */ }
}
function _restoreDraft() {
  try {
    const v = kvCache.get(_DRAFT_CACHE_KEY);
    if (!v || typeof v !== 'string' || !app.chatInput) return;
    if (app.chatInput.value) return;
    if (!_canChat()) return;
    app.chatInput.value = v;
    _updateInputRowState();
    if (app.chatSend) app.chatSend.disabled = !v.trim();
    if (app.autoResizeChatInput) app.autoResizeChatInput();
  } catch (_) { /* non-fatal */ }
}

let _draftTimer = null;
function _debouncedSaveDraft() {
  if (_draftTimer) clearTimeout(_draftTimer);
  _draftTimer = setTimeout(() => {
    _draftTimer = null;
    if (_purged) return; // tenant gone — never write old-tenant draft to the next tenant
    try {
      const v = app.chatInput ? app.chatInput.value : '';
      if (v) kvCache.set(_DRAFT_CACHE_KEY, v);
      else kvCache.del(_DRAFT_CACHE_KEY);
      if (!browserPersistenceAllowed()) return;
      if (v) localStorage.setItem(_DRAFT_LS_KEY, v);
      else localStorage.removeItem(_DRAFT_LS_KEY);
    } catch (_) { /* quota / private mode — non-fatal */ }
  }, 150);
}

// On a cold load the legacy blobs are gone, so the boot-time _restoreDraft and
// outbox check (which run before storage hydration resolves) see nothing. Re-run
// both once hydration completes so a persisted draft/outbox repopulates from the
// IndexedDB rows. Both are idempotent (draft skips a non-empty input; the poll
// timer is already guarded).
if (typeof window !== 'undefined') {
  window.addEventListener('webagent-kv-cache-hydrated', () => {
    _restoreDraft();
    if (_outboxHasPending()) {
      _renderPendingBubbles();
      _startOutboxPoll();
    }
  });
  window.addEventListener('webagent-offline-readonly-changed', (event) => {
    if (event?.detail?.active !== false) return;
    _setOfflineComposerNotice('');
    _flushOutbox().catch(() => { /* the normal poll retains and retries failures */ });
  });
}

// ── Send / Stop / Abort ────────────────────────────────────────────────────

async function sendStopMessage() {
  _unlockComposer();
  app._stopPending=true;
  app.chatActivityStart?.('Stopping…');
  try {await stopPanel();}
  catch (_) {app.chatActivityStart?.('Stop could not be confirmed. Retry Stop.');}
}

function _btwUnavailable(message) {
  alert('Could not start BTW session: ' + message);
}

async function _startBtwSession(initialMessage, textOverride) {
  if (_btwPending) return;
  if (window.__webagentOfflineReadOnly === true) {
    _btwUnavailable('BTW requires a live server connection.');
    return;
  }
  if (storageAdapter.isBrowser) {
    _btwUnavailable('browser-local sessions are not supported yet.');
    return;
  }
  if (app.tunnel?.active || app.terminalChat?.active) {
    _btwUnavailable('terminal sessions are not supported yet.');
    return;
  }
  const sourceSessionId = String(app.currentSessionId || '');
  if (!sourceSessionId) {
    _btwUnavailable('open a persisted WebAgent session first.');
    return;
  }
  if (sourceSessionId.startsWith('codex:')) {
    _btwUnavailable('Codex Portal sessions are not supported yet.');
    return;
  }
  if (!app.currentUserId) {
    _btwUnavailable('sign in and open an owned session first.');
    return;
  }

  _btwPending = true;
  if (app.chatActivityStart) app.chatActivityStart('Creating BTW session…');
  try {
    const resp = await fetch(apiPath(
      '/api/v1/db/sessions/' + encodeURIComponent(sourceSessionId) + '/fork?db=user.db'
    ), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        fork_kind: 'btw',
      }),
    });
    const data = await resp.json().catch(() => ({}));
    if (!resp.ok || !data.session_id || !data.agent_id) {
      throw new Error(data.detail || `server responded ${resp.status}`);
    }

    const widget = createChatWidget({
      title: data.title || 'btw: session',
      iconName: 'messages-square',
      agentId: data.agent_id,
      sessionId: data.session_id,
      initialSessionSeq: data.snapshot_session_seq ?? data.interactions_copied ?? 0,
      initialMessage,
      executionMode: 'ask',
    });
    widget.open();

    // The command is a UI action, not a message in the source session. Clear it
    // only after both the durable fork and widget creation have succeeded.
    if (typeof textOverride !== 'string' && app.chatInput) {
      app.chatInput.value = '';
      app.chatSend.disabled = true;
      _updateInputRowState();
      _autoResizePill(app.chatInput);
      _clearDraft();
    }
    if (typeof app.populateSessionSelect === 'function') {
      try { await app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
  } catch (error) {
    _btwUnavailable(error?.message || String(error));
  } finally {
    _btwPending = false;
    if (app.chatActivityStop) app.chatActivityStop();
  }
}

async function sendMessage(textOverride) {
  if(!_canChat()) {applyChatGate();return;}
  const text=typeof textOverride==='string'?textOverride.trim():(app.chatInput?.value || '').trim();
  if(!text) return;
  // Terminal tunnel
  if (app.tunnel && app.tunnel.active) {
    app.chatInput.value = '';
    app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
    _clearDraft();
    if (typeof app.sendTunnelLine === 'function') {
      try { await app.sendTunnelLine(text); } catch (_) { /* terminal reflects state */ }
    }
    return;
  }

  // Terminal Chat — pill input goes straight to the PTY, no agent.
  if (app.terminalChat && app.terminalChat.active) {
    app.chatInput.value = '';
    app.chatSend.disabled = true;
    _updateInputRowState();
    _autoResizePill(app.chatInput);
    _clearDraft();
    try {
      // A PTY's Enter key is carriage return (\r), NOT line feed (\n) — the same
      // byte the xterm keyboard and the agent's terminal_send both use. Sending
      // \n makes PowerShell's PSReadLine treat it as a line continuation (the
      // ">>" prompt) instead of executing, so the command never runs. Normalise
      // any in-textarea newlines (multi-line paste) to \r too, and end with \r.
      const ptyInput = text.replace(/\r?\n/g, '\r') + '\r';
      await fetch(apiPath('/api/v1/terminal/sessions/' + encodeURIComponent(app.terminalChat.tsid) + '/write'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ input: ptyInput }),
      });
    } catch (_) { /* terminal reflects state */ }
    return;
  }

  if (!app.currentAgentId) {
    if (!app.currentUserId) { applyChatGate(); return; }
    try {
      if (typeof app.startWebagentSession === 'function') {
        await app.startWebagentSession();
      }
    } catch (e) {
      addChatBubble('agent', '\u274C Could not start WebAgent: ' + (e.message || e), 'error');
      return;
    }
    if (!app.currentAgentId) return;
  }


  const sessionId=app.currentSessionId;
  try {
    const payload=await addAttachmentsToMessage({message:text,session_id:sessionId,user_id:app.currentUserId});
    if(sessionId!==app.currentSessionId) throw new Error('Session changed during attachment upload');
    await queueSubmission({client_message_id:crypto.randomUUID(),text,
      attachment_ids:payload.attachment_ids || [],execution_mode:app.executionMode || 'ask'});
    if(typeof textOverride!=='string' && app.chatInput?.value.trim()===text) {
      app.chatInput.value='';
      _autoResizePill(app.chatInput);_clearDraft();
      if (app.chatSend) app.chatSend.disabled = true;
    }
    app.clearPendingAttachments?.();
    _updateInputRowState();
    app.dismissMobileComposerAfterSend?.();
  } catch(error) {
    // A local submission failure is UI state, never a fabricated agent answer.
    app.chatActivityStart?.('Message was not queued. '+(error.message || 'Please retry.'));
  }
}

function abortChatStream() {
  disconnectPanel();
  app.agentBuffer = '';
  app.isProcessing = false;
  app._stopPending = false;
  _discardDeferredMessages();
  _unlockComposer();
  if (app.chatSend) app.chatSend.disabled = false;
}

// Exposed on `app` so session modules can call it without importing this module
app.abortChatStream = abortChatStream;

app.refreshChat = refreshPanel;

function _resolvePillMaxWidth(rawValue, availableW) {
  const value = String(rawValue || '').trim();
  if (value.endsWith('%')) {
    const percentage = parseFloat(value);
    return Number.isFinite(percentage) ? availableW * percentage / 100 : availableW;
  }
  const pixels = parseFloat(value);
  return Number.isFinite(pixels) ? pixels : availableW;
}

// Needed by chat-ui.js for auto-resize
function _autoResizePill(el) {
  if (!el || el.tagName !== 'TEXTAREA') return;
  // During pill rebuild (applyChatPillLayout), elements are reparented and
  // clientWidth/clientHeight are transient — skip to avoid writing garbage
  // dimensions (e.g. ~20px) into --chat-pill-current-width.
  if (app.__rebuildingPill) return;
  const cs = getComputedStyle(el);
  const minH = parseFloat(cs.minHeight) || 32;
  const maxH = parseFloat(cs.maxHeight) || 160;

  const pill = el.closest('.chat-pill');
  const isMainPill = pill && pill.id === 'chat-input-row';

  if (!isMainPill) {
    // ── Vertical resize (non-main pills: 1-line, search, builder, etc.) ──
    if (!el.value) {
      el.style.height = minH ? minH + 'px' : 'auto';
      el.style.overflowY = 'hidden';
      _updateScrollIndicator(el);
      return;
    }
    el.style.height = '1px';
    const scrollH = el.scrollHeight;
    const padT = parseFloat(cs.paddingTop) || 0;
    const padB = parseFloat(cs.paddingBottom) || 0;
    const lnH  = parseFloat(cs.lineHeight) || 22;
    const oneLineH = padT + lnH + padB;
    const targetH = scrollH <= oneLineH
      ? minH
      : Math.max(minH, Math.min(scrollH, maxH));
    el.style.height = targetH + 'px';
    el.style.overflowY = scrollH > maxH ? 'auto' : 'hidden';
    _updateScrollIndicator(el);
    return;
  }

  // ── Main composer (#chat-input-row): hybrid horizontal + vertical ──

  // Compute bounds
  const inputArea = pill.closest('#chat-input-area');
  const padX = 24; // 12px left + 12px right on #chat-input-area
  const availableW = inputArea ? inputArea.clientWidth - padX : 600;
  const configuredMaxW = _resolvePillMaxWidth(
    getComputedStyle(pill).getPropertyValue('--chat-surface-max-width'),
    availableW,
  );
  const maxPillW = Math.min(availableW, configuredMaxW);
  const configuredPillW = parseFloat(
    getComputedStyle(pill).getPropertyValue('--chat-pill-width')
  ) || 500;
  const restingPillW = Math.min(configuredPillW, maxPillW);

  if (!el.value) {
    inputArea?.style.setProperty('--chat-pill-current-width', restingPillW + 'px');
    // Empty: clear overrides so the pill and control bands share the configured
    // resting width again.
    pill.style.width = '';
    pill.style.minWidth = '';
    pill.style.maxWidth = '';
    el.style.whiteSpace = '';
    el.style.overflowWrap = '';
    el.style.wordBreak = '';
    el.style.height = minH + 'px';
    el.style.overflowY = 'hidden';
    return;
  }

  // ── Phase 1: measure text content width ──
  const pillOverhead = pill.clientWidth - el.clientWidth;

  // scrollWidth bottoms out at clientWidth, so narrow glyphs can accumulate
  // without changing it. Measure the rendered line itself instead.
  const canvas = _autoResizePill._measureCanvas
    || (_autoResizePill._measureCanvas = document.createElement('canvas'));
  const ctx = canvas.getContext('2d');
  ctx.font = cs.font;
  const letterSpacing = parseFloat(cs.letterSpacing) || 0;
  const lines = el.value.split('\n');
  const naturalW = Math.max(0, ...lines.map(line => {
    const measured = ctx.measureText(line.replace(/\t/g, '    ')).width;
    return measured + Math.max(0, line.length - 1) * letterSpacing;
  })) + (parseFloat(cs.paddingLeft) || 0)
      + (parseFloat(cs.paddingRight) || 0)
      + 16; // keep the caret ahead of the textarea's native clipping edge

  const targetPillW = Math.max(restingPillW, Math.min(naturalW + pillOverhead, maxPillW));
  inputArea?.style.setProperty('--chat-pill-current-width', targetPillW + 'px');

  // ── Phase 2: apply width + height atomically ──
  const savedPillT = pill.style.transition;
  const savedElT = el.style.transition;
  pill.style.transition = 'none';
  el.style.transition = 'none';

  // Only set an explicit width while expanded; at rest the shared CSS variable
  // keeps the pill and surrounding control bands aligned.
  if (targetPillW > restingPillW) {
    pill.style.width = targetPillW + 'px';
    pill.style.maxWidth = 'none';
  } else {
    // Within resting bounds → clear overrides, same as footer
    pill.style.width = '';
    pill.style.maxWidth = '';
  }

  // ── Phase 3: toggle wrapping on/off ──
  // When the pill is at max width and content still overflows, enable text
  // wrapping (pre-wrap). Otherwise keep pre (no-wrap) so the pill widens
  // before any line break.
  const atMaxWidth = targetPillW >= maxPillW;
  const contentOverflows = naturalW + pillOverhead > maxPillW;
  if (atMaxWidth && contentOverflows) {
    el.style.whiteSpace = 'pre-wrap';
    el.style.overflowWrap = 'break-word';
    el.style.wordBreak = 'break-word';
  } else {
    el.style.whiteSpace = '';
    el.style.overflowWrap = '';
    el.style.wordBreak = '';
  }

  // A textarea scrolls horizontally to reveal the caret before this input
  // handler gets a chance to widen the pill. Once the content fits again,
  // discard that stale offset or the leading text remains visibly clipped.
  if (!contentOverflows) el.scrollLeft = 0;

  // Measure height at the new width.
  el.style.height = '1px';
  const scrollH = el.scrollHeight;
  const padT = parseFloat(cs.paddingTop) || 8;
  const padB = parseFloat(cs.paddingBottom) || 2;
  const lnH  = parseFloat(cs.lineHeight) || 22;
  const oneLineH = padT + lnH + padB;

  const targetH = scrollH <= oneLineH
    ? minH
    : Math.max(minH, Math.min(scrollH, maxH));
  el.style.height = targetH + 'px';
  el.style.overflowY = scrollH > maxH ? 'auto' : 'hidden';

  pill.offsetHeight; // force reflow
  pill.style.transition = savedPillT;
  el.style.transition = savedElT;
}

function _updateScrollIndicator(_el) {
  // Removed — scroll indicator dot inside the pill is no longer used.
}

// ── Prewarm ──────────────────────────────────────────────────────────────────
// While the user is typing (or the moment they focus the pill), ask the server
// to build the read-only prep for the NEXT send — the agent's tool set, the chat
// history, and supporting metadata. On a remote DB those reads cost seconds;
// doing them during the typing window means the send can skip them. Best-effort:
// throttled per session, fire-and-forget, and a failure just means the turn
// builds the prep live as before. Backend: POST /api/v1/chat/prewarm.
const _PREWARM_THROTTLE_MS = 15000;
let _lastPrewarmAt = Object.create(null);

function _prewarm() {
  try {
    if (!_canChat()) return;
    if (!app.currentUserId || !app.currentSessionId || !app.currentAgentId) return;
    // Tunnels / Terminal Chat don't run the normal agent turn — nothing to warm.
    if (app.tunnel && app.tunnel.active) return;
    if (app.terminalChat && app.terminalChat.active) return;
    const sid = app.currentSessionId;
    const now = Date.now();
    if (_lastPrewarmAt[sid] && (now - _lastPrewarmAt[sid]) < _PREWARM_THROTTLE_MS) return;
    _lastPrewarmAt[sid] = now;
    fetch(apiPath('/api/v1/chat/prewarm'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        user_id: app.currentUserId,
        session_id: sid,
        agent_id: app.currentAgentId,
      }),
      keepalive: true,
    }).catch(() => { /* best-effort: a miss just builds the prep live on send */ });
  } catch (_) { /* never let prewarm interfere with typing */ }
}

// ── Gate-queue bubble styling ──────────────────────────────────────────
// Called from agentWs.js when agent_status: queued fires — the session cap
// was reached and this session is waiting in the FIFO queue. Apply the
// same pending (dashed-border + ⏳) look used for outbox-retry messages.

function _findGateQueuedBubble(turnId) {
  if (!app.chatMessages) return null;
  let bubble = null;
  if (turnId) {
    bubble = app.chatMessages.querySelector(
      `.chat-bubble.user[data-turn-id="${CSS.escape(String(turnId))}"], .chat-bubble.user[data-msg-id="${CSS.escape(String(turnId))}"]`
    );
  }
  return bubble;
}

function _applyGateQueuedStyle(bubble, state) {
  if (!bubble || !state) return null;
  // Don't re-mark a bubble that's already in save-error state
  if (bubble.classList.contains('save-error')) return null;
  bubble.classList.add('pending', 'saving');
  bubble.setAttribute('data-gate-queued', String(state.queuePosition || '1'));
  let label = bubble.querySelector('.label');
  if (!label) {
    label = document.createElement('span');
    label.className = 'label';
    bubble.insertBefore(label, bubble.firstChild);
  }
  label.textContent = 'You (queued' + (state.queuePosition ? ' #' + state.queuePosition : '') + '\u2026)';
  _addForceRunButton(bubble, state.turnId);
  return bubble;
}

function _restoreGateQueueBubble(bubble, turnId) {
  const state = _queuedStateFor(app.currentSessionId, turnId);
  if (state) _applyGateQueuedStyle(bubble, state);
}

// Reload / navigate-back restore: re-apply the queued bubble + Force run
// button from a persisted message that either carries the durable
// status='queued' marker (DB fix) or was annotated with live gate queue info
// by /session-messages (in-memory backup). Registers the state in
// _gateQueueBySession so the WS "running" event (clearBubbleQueued) and a
// later force-run clean it up exactly like the live path. Safe to call on any
// session's transcript; only affects the current one.
function restoreGateQueueBubble(bubble, state) {
  if (!bubble || !state) return;
  const sid = String(app.currentSessionId || '');
  const turnId = state.turnId || null;
  const remembered = _gateQueueBySession.get(sid);
  if (!remembered || (turnId && remembered.turnId && String(remembered.turnId) !== String(turnId))) {
    _gateQueueBySession.set(sid, {
      turnId,
      queuePosition: Number.isFinite(state.queuePosition) ? state.queuePosition : null,
      text: (state.text || remembered?.text || ''),
    });
  }
  _applyGateQueuedStyle(bubble, state);
}

function markBubbleQueued(turnId, queuePosition, sessionId = app.currentSessionId) {
  const sid = String(sessionId || app.currentSessionId || '');
  if (!sid) return;
  const remembered = _recentOutgoingBySession.get(sid);
  const state = {
    turnId: turnId || null,
    queuePosition: Number.isFinite(queuePosition) ? queuePosition : null,
    text: remembered?.text || '',
  };
  _gateQueueBySession.set(sid, state);

  // Do not touch a different session's transcript. Its title-bar queue state
  // is updated by the dropdown cache; when this session is opened, a normal
  // persisted user message can still be styled by a later status event.
  if (sid !== String(app.currentSessionId || '')) return;

  let bubble = _findGateQueuedBubble(turnId);
  // Prefer the bubble registered by this send over an unqualified DOM scan:
  // an ephemeral/GenUI turn can otherwise accidentally mark an older user
  // bubble while its own node is still being reconciled.
  if (!bubble && remembered?.bubble?.isConnected
      && remembered.bubble.classList.contains('user')) {
    bubble = remembered.bubble;
  }
  // Last-resort rendering is intentional: GenUI and ephemeral messages may
  // have no durable user node at the instant the queue event arrives. Showing
  // the queued turn is more useful than silently dropping its only feedback.
  if (!bubble && app.chatMessages && state.text) {
    bubble = addChatBubble('user', state.text);
  }
  _applyGateQueuedStyle(bubble, state);
}

// "Force run" button on a queued bubble: bypasses the session gate so this
// turn starts immediately instead of waiting for a slot to free.
function _addForceRunButton(bubble, turnId) {
  if (!bubble || bubble.querySelector('.force-run-btn')) return;
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-action-btn force-run-btn';
  btn.innerHTML = '<i data-lucide="zap" style="width:13px;height:13px;"></i> Force run';
  btn.title = 'Bypass the session queue and run this message now';
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    btn.disabled = true;
    try {
      const resp = await fetch(apiPath('/api/v1/chat/force-run'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({
          user_id: app.currentUserId,
          session_id: app.currentSessionId,
        }),
      });
      if (!resp.ok) {
        btn.disabled = false;
        btn.title = 'Force run failed — try again';
        return;
      }
      // Success: clear the queued styling immediately. The run wakes and
      // emits agent_status: running (via run_state_begin), which also calls
      // clearBubbleQueued — idempotent, so no double-handling.
      clearBubbleQueued(turnId);
      // Patch the dropdown row back to running state right away.
      if (typeof app.onSessionGateForce === 'function') {
        try { app.onSessionGateForce(); } catch (_) { /* ignore */ }
      }
    } catch (_) {
      btn.disabled = false;
      btn.title = 'Force run failed — try again';
    }
  });
  // Place into the existing actions footer if present, else make a small one.
  let actions = bubble.querySelector(':scope > .bubble-actions');
  if (!actions) {
    actions = document.createElement('div');
    actions.className = 'bubble-actions';
    bubble.appendChild(actions);
  }
  actions.appendChild(btn);
  _refreshLucideIcons(bubble);
}

// Clear the queued styling when the run starts (agent_status: running) or the
// user forces the run. Clears every gate-queued bubble (safe: at most one per
// session) and removes the force button.
function clearBubbleQueued(turnId, sessionId = app.currentSessionId) {
  const sid = String(sessionId || app.currentSessionId || '');
  const state = _gateQueueBySession.get(sid);
  if (state && (!turnId || !state.turnId || String(state.turnId) === String(turnId))) {
    _gateQueueBySession.delete(sid);
  }
  if (!app.chatMessages) return;
  const marked = app.chatMessages.querySelectorAll('[data-gate-queued]');
  marked.forEach((bubble) => {
    if (turnId && bubble.getAttribute('data-msg-id') && bubble.getAttribute('data-msg-id') !== String(turnId)) return;
    bubble.classList.remove('pending', 'saving');
    bubble.removeAttribute('data-gate-queued');
    bubble.querySelectorAll('.force-run-btn').forEach((el) => el.remove());
    const label = bubble.querySelector('.label');
    if (label && /queued/.test(label.textContent || '')) {
      label.textContent = 'You';
    }
  });
}

app.markBubbleQueued = markBubbleQueued;
app.clearBubbleQueued = clearBubbleQueued;
app.restoreGateQueueBubble = restoreGateQueueBubble;

export {
  sendMessage,
  sendStopMessage,
  _prewarm,
  abortChatStream,
  applyChatGate,
  _canChat,
  _autoResizePill,
  _updateScrollIndicator,
  _updateInputRowState,
  _saveDraft,
  _clearDraft,
  _restoreDraft,
  _renderPendingBubbles,
  _startOutboxPoll,
  _outboxHasPending,
  _restorePersistenceStatus,
  restoreGateQueueBubble,
};

'use strict';

// Per-user receive-only WebSocket subscriber — streams live agent events (tokens,
// tool calls, status) for the signed-in user, with reconnect/backoff. Exposes
// registerSessionSubscriber() so a chat widget can claim a session's live events.

import { app } from './state.js';
import { agentWsUrl, apiPath } from './config.js';
import { logTool } from './toolLog.js';
import { getAuthToken } from './left-login.js';
import { renderAvatar } from './user-avatar.js';
import { getActive } from './accounts.js';
import { handleAttachmentEvent } from './attachments.js';
import { isMessageTypeVisible } from './chat-visibility.js';

let reconnectTimer = null;
let reconnectAttempts = 0;
let _connectionLostTimer = null;    // 5-min timer for user-facing "Connection lost" message
let _connLostShown = false;         // whether the user-facing bubble was already emitted
const MAX_RECONNECT_DELAY = 30000;
const INITIAL_RECONNECT_DELAY = 500;
/** How long (ms) before showing a user-facing connection-lost message. */
const CONNECTION_LOST_TIMEOUT = 300000; // 5 minutes

// Pipeline telemetry arrives in bursts around every LLM and tool transition.
// Preserve every event and its order, but render the burst once per animation
// frame. Streamed assistant text remains on the immediate path below.
let _pipelineRenderQueue = [];
let _pipelineRenderFrame = 0;

function _renderObservers(event, forCurrent) {
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('stream.observers.apply', {
      type: event.type,
      step: event.step,
      session_id: event.session_id,
      run_id: event.turn_id,
      event_seq: event.session_seq,
      for_current: forCurrent,
    });
  } catch (_) {}

  try { logTool(event); } catch (_) { /* not mounted */ }
  if (forCurrent) {
    try { window.dispatchEvent(new CustomEvent("webagent-loop-event", { detail: event })); } catch (_) { /* ignore */ }
  }
  if (app._genuiHandler) {
    try { app._genuiHandler(event); } catch (_) { /* ignore */ }
  }
  if (forCurrent && app._chatActivityHandler) {
    try { app._chatActivityHandler(event); } catch (_) { /* ignore */ }
  }
}

function _queuePipelineRender(event, forCurrent) {
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('stream.queued', {
      type: event.type,
      step: event.step,
      session_id: event.session_id,
      run_id: event.turn_id,
      event_seq: event.session_seq,
      for_current: forCurrent,
    });
  } catch (_) {}

  _pipelineRenderQueue.push({ event, forCurrent });
  if (_pipelineRenderFrame) return;
  _pipelineRenderFrame = requestAnimationFrame(() => {
    _pipelineRenderFrame = 0;
    const batch = _pipelineRenderQueue;
    _pipelineRenderQueue = [];
    for (const item of batch) _renderObservers(item.event, item.forCurrent);
  });
}

// ── Pending replay buffer ──
// When the WS reconnects (or a fresh page load completes its handshake) the
// server can replay buffered `stream` / `response` / `interrupted` events
// for any of the user's sessions that still have an in-flight run. If those
// events arrive while the UI is on a DIFFERENT session — or before the
// user has navigated anywhere — we used to drop them. Now we stash them
// per-session and `consumeReplayedEventsFor(sid)` lets the chat module
// flush them when the user switches in. Capped per session to avoid leaks
// for runaway runs.
const _pendingReplay = new Map(); // session_id -> Array<event>
const PENDING_REPLAY_CAP = 500;

function _stashReplay(sid, event) {
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('stream.replay.stashed', {
      session_id: sid,
      type: event.type,
      event_seq: event.session_seq,
    });
  } catch (_) {}

  if (!sid) return;
  let arr = _pendingReplay.get(sid);
  if (!arr) {
    arr = [];
    _pendingReplay.set(sid, arr);
  }
  if (arr.length >= PENDING_REPLAY_CAP) return; // drop oldest-overflow silently
  arr.push(event);
}

export function consumeReplayedEventsFor(sid) {
  if (!sid) return [];
  const arr = _pendingReplay.get(sid) || [];
  _pendingReplay.delete(sid);
  return arr;
}

// ── Session subscribers (chat widgets) ──
// A floating chat widget (ui/chat-widget/) runs its OWN session alongside the
// main panel. It claims that session's events here so they reach the widget
// instead of being dropped/stashed as "foreign". One handler per session_id;
// registering drains any events already stashed for that session (a reconnect
// replay can land before the widget finishes registering).
const _sessionSubscribers = new Map(); // session_id -> handler(event)

export function registerSessionSubscriber(sessionId, handler) {
  if (!sessionId || typeof handler !== 'function') return () => {};
  _sessionSubscribers.set(sessionId, handler);
  for (const ev of consumeReplayedEventsFor(sessionId)) {
    try { handler(ev); } catch (_) { /* widget render error — non-fatal */ }
  }
  return () => {
    if (_sessionSubscribers.get(sessionId) === handler) _sessionSubscribers.delete(sessionId);
  };
}

function setAgentStatus(state) {
  if (!app.aDot || !app.aStat) return;
  app.aDot.className = 'status-dot ' + state;
  app.aStat.textContent =
    state === 'green' ? 'Agent' : state === 'yellow' ? 'Connecting...' : 'Disconnected';
}

function cancelAgentReconnect() {
  if (reconnectTimer) {
    clearTimeout(reconnectTimer);
    reconnectTimer = null;
  }
  reconnectAttempts = 0;
}

function _cancelConnectionLostTimer() {
  if (_connectionLostTimer) {
    clearTimeout(_connectionLostTimer);
    _connectionLostTimer = null;
  }
  _connLostShown = false;
}

function _showConnectionLost() {
  if (_connLostShown) return;
  _connLostShown = true;
  if (!app.chatMessages) return;
  // Don't show if we're already back online
  if (app.agentWs && app.agentWs.readyState === WebSocket.OPEN) return;
  // Show a simple separate bubble for everyone
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble agent connection-lost-global';
  bubble.textContent = 'Connection lost. Trying to reconnect\u2026';
  app.chatMessages.appendChild(bubble);
  bubble.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function scheduleAgentReconnect() {
  cancelAgentReconnect();
  const delay = Math.min(
    INITIAL_RECONNECT_DELAY * Math.pow(2, reconnectAttempts),
    MAX_RECONNECT_DELAY
  );
  reconnectAttempts++;
  reconnectTimer = setTimeout(() => {
    reconnectTimer = null;
    connectAgent();
  }, delay);
}

export function connectAgent() {
  if (!app.currentUserId) {
    setAgentStatus('red');
    return;
  }

  setAgentStatus('yellow');

  if (app.agentWs) {
    app.agentWs.onclose = null;
    app.agentWs.onerror = null;
    app.agentWs.close();
  }

  app.agentWs = new WebSocket(agentWsUrl());

  app.agentWs.onopen = () => {
    // RECORDER-HOOK: optional reconnect witness; remove this block with recorder.
    try { window.__recorderTrace?.('connection.opened', {session_id:app.currentSessionId}); } catch (_) {}

    // Send the JWT alongside the user_id so the server can prove the caller
    // is who they claim to be — without this the per-user broadcast
    // registration would accept any user_id and leak cross-tenant events.
    // Also send the per-session "last session_seq" map so the server can
    // replay any events we missed during the WS gap before going live.
    const resume = (app.lastSessionSeq && typeof app.lastSessionSeq === 'object')
      ? app.lastSessionSeq
      : {};
    // Derive the handshake user_id from the SAME source as the token (the active
    // account) so the pair can never desync — e.g. after an account switch or a
    // token refresh that updated auth_token but not app.currentUserId (a boot-
    // time snapshot). A mismatched pair is rejected by the server with "token
    // subject does not match user_id". Fall back to app.currentUserId for flows
    // with no tracked account (open mode, public per-agent anon sessions).
    const _active = getActive();
    const _uid = (_active && _active.user_id) || app.currentUserId;
    app.agentWs.send(JSON.stringify({
      mode: 'user_subscriber',
      user_id: _uid,
      token: getAuthToken() || '',
      resume,
    }));
  };

  app.agentWs.onmessage = (ev) => {
    let event;
    try {
      event = JSON.parse(ev.data);
    } catch {
      return;
    }

    if (event.type === 'ping') return;
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('stream.received', {
        type: event.type,
        step: event.step,
        session_id: event.session_id,
        run_id: event.turn_id,
        interaction_id: event.asst_id,
        event_seq: event.session_seq,
        replayed: !!event.replayed,
        text_length: typeof event.content==='string'?event.content.length:null,
      });
    } catch (_) {}


    if (event.type === 'subscribed') {
      setAgentStatus('green');
      reconnectAttempts = 0;
      // Cancel the 5-min connection-lost timer and clean up any shown bubble
      _cancelConnectionLostTimer();
      if (app.chatMessages) {
        app.chatMessages.querySelectorAll('.connection-lost-global').forEach(el => el.remove());
      }
      // Lightweight refresh on WS (re)connect — don't re-fetch agents/sessions,
      // initSessions() already loaded them eagerly. Just update the user avatar
      // in case the account changed in another tab.
      const slot = document.getElementById('top-user-avatar-slot');
      if (slot) {
        const active = getActive();
        const acct = active || { display_name: app.currentUserId || 'None', username: app.currentUserId || '' };
        slot.innerHTML = '';
        slot.appendChild(renderAvatar(acct, 'sm'));
      }
      // Server tells us which sessions still have an in-flight run buffered.
      // Reconcile that authoritative reconnect snapshot into the same state path
      // used by live `agent_status: running` broadcasts, so a run that started
      // while this browser was disconnected still shows its session spinner and
      // the open chat's Working… activity bar.
      if (Array.isArray(event.active_sessions)) {
        app._activeServerSessions = event.active_sessions;
        if (typeof app.onActiveServerSessions === 'function') {
          try { app.onActiveServerSessions(event.active_sessions); } catch(_) {}
        }
      }
      return;
    }

    if (event.type === 'device_revoked') {
      import('./device-purge.js')
        .then(({ handleRemoteDeviceRevocation }) => handleRemoteDeviceRevocation(event))
        .catch(() => {});
      return;
    }

    // Track the highest session_seq we've ever seen per session, so the
    // next WS reconnect can ask the server for only-newer events.
    const _sid = event.session_id || event.sessionId || '';
    if (_sid && typeof event.session_seq === 'number') {
      if (!app.lastSessionSeq) app.lastSessionSeq = {};
      const prev = app.lastSessionSeq[_sid] || 0;
      if (event.session_seq > prev) {
        app.lastSessionSeq[_sid] = event.session_seq;
        // Mirror to localStorage so a hard refresh doesn't lose the cursor
        // and the server can replay-from-correct-seq on next handshake.
        try {
          localStorage.setItem(
            'webagent.lastSessionSeq.v1',
            JSON.stringify(app.lastSessionSeq),
          );
        } catch (_) { /* quota / private mode — non-fatal */ }
      }
    }

    // Track WebSocket liveness per session: when did we last receive a chat
    // event for this session? The DB-reconcile poll (chat-reconcile.js) only
    // engages after the WS has been silent for a couple seconds, so when the WS
    // is delivering (same-process) the poll never fires, and when it can't
    // (browser and agent on different workers/instances) the poll takes over.
    if (_sid && (event.type === 'stream' || event.type === 'response'
                 || event.type === 'agent_step_end' || event.type === 'interrupted'
                 || event.type === 'summary' || event.type === 'overview')) {
      if (!app._lastWsEventAt) app._lastWsEventAt = {};
      app._lastWsEventAt[_sid] = Date.now();
    }

    // The Loop / Flow debug panels and the chat-pill "thinking" glow reflect the
    // session you're VIEWING, so only feed them events for the active session.
    // Events now carry session_id; untagged ones pass through defensively.
    const _forCurrent = !_sid || _sid === app.currentSessionId;

    // Forward to tool log panel (global — intentionally shows all tool activity)
    if (event.type === 'pipeline') {
      _queuePipelineRender(event, _forCurrent);
    } else {
      try { logTool(event); } catch(e) { /* not mounted */ }
    }

    // Forward to stream/loop/flow debug panels (handles ALL event types)
    if (event.type !== 'pipeline' && _forCurrent && app._loopHandler) {
      try { app._loopHandler(event); } catch(e) { /* ignore */ }
    }
    if (event.type !== 'pipeline' && _forCurrent && app._loopVisualHandler) {
      try { app._loopVisualHandler(event); } catch(e) { /* ignore */ }
    }
    // Gen UI has its own per-visualizer-session logic — leave unguarded.
    if (event.type !== 'pipeline' && app._genuiHandler) {
      try { app._genuiHandler(event); } catch(e) { /* ignore */ }
    }
    // Drive the chat pill "thinking" glow + activity-note ticker (current session).
    if (event.type !== 'pipeline' && _forCurrent && app._chatActivityHandler) {
      try { app._chatActivityHandler(event); } catch(e) { /* ignore */ }
    }

    // ── Session subscribers (chat widgets) get first claim ──
    // If a widget owns this event's session, hand the event over. When the
    // widget's session is ALSO the main panel's current session, fall through
    // so the panel renders too. session_title always falls through — it
    // updates the global session-name cache, not just the panel.
    // Note ui_command stays gated to the CURRENT session below, so a widget
    // task can never rearrange the user's screen.
    const _claimed = _sid ? _sessionSubscribers.get(_sid) : null;
    if (_claimed) {
      try { _claimed(event); } catch (_) { /* widget render error — non-fatal */ }
      if (event.type !== 'session_title' && _sid !== app.currentSessionId) return;
    }

    // ── Chat bubble display — the WS is the sole real-time renderer ──
    // Every agent turn emits stream, agent_step_end, and response events
    // through this socket. The DB is the source of truth for cold loads
    // (refresh / device switch); the WS is the live accelerant. Events
    // for foreign sessions are stashed via _stashReplay so the session
    // loader can drain them when the user navigates in.

    const eventSessionId = event.session_id || event.sessionId || '';
    const _eventInteractionId = event.asst_id || event.id || '';
    const _interactionAnchor = _eventInteractionId && app._interactionAnchors
      ? app._interactionAnchors.get(String(_eventInteractionId)) : null;
    const _interactionSeq = event.interaction_seq != null
      ? Number(event.interaction_seq)
      : (_interactionAnchor ? _interactionAnchor.interactionSeq : NaN);
    const _ownerTurnId = event.turn_id
      || (_interactionAnchor && _interactionAnchor.turnId)
      || '';

    switch (event.type) {
      case 'error':
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        // A terminal loop error is a semantic boundary for the turn it names.
        // Render exactly one fallback closer (using the last complete working
        // message) after the run's activity group and end active controls so
        // Stop can never coexist with the errored run — the same terminal
        // treatment the 'interrupted' (Stopped) event gets. Keep the fallback
        // only for errors that end the current turn; transient auto-recoverable
        // loop errors are surfaced (debug) but do not terminate the run.
        if (typeof app.markAgentInterrupted === 'function' && (_eventInteractionId || _ownerTurnId)) {
          try {
            app.markAgentInterrupted(
              _eventInteractionId,
              event.created_at || event.emit_time,
              _interactionSeq,
              _ownerTurnId,
              'Error',
            );
          } catch (_) {}
        } else if (app.isDebug) {
          // No step identity to scope the closer to — surface the message.
          app.updateLastBubble('Error: ' + event.message, 'error');
        }
        app.agentBuffer = '';
        app.isProcessing = false;
        if (app.chatSend) app.chatSend.disabled = false;
        break;

      case 'interrupted':
        // Terminal for the step it names. Marks that step's bubble interrupted
        // (keeping its partial text). A new message may have already started a
        // replacement run, which re-engages on its own.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (typeof app.markAgentInterrupted === 'function') {
          try {
            app.markAgentInterrupted(
              event.asst_id,
              event.created_at || event.emit_time,
              _interactionSeq,
              _ownerTurnId,
            );
          } catch(_) {}
        } else {
          app.updateLastBubble('(interrupted)', 'interrupted');
          app.isProcessing = false;
          if (app.chatSend) app.chatSend.disabled = false;
        }
        break;

      case 'resumed':
        // The self-healing layer re-ignited a run that had stopped involuntarily
        // (server restart / frozen / zombie). Clear any stale "interrupted" or
        // "Connection lost" state and re-engage the thinking indicator — the
        // resumed turn streams in as a fresh bubble right after this.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        // The recovery notice is now persisted as a role='system' interaction
        // and rendered by the reconcile loop + session-load — no need for an
        // ephemeral WS-only DOM element that vanishes on refresh.
        app._stopPending = false; // any prior stop request is obsolete
        app.isProcessing = true;
        if (app.chatSend) app.chatSend.disabled = true;
        break;

      case 'user_message': {
        // Every user message (typed by this user, sent from another device, or
        // injected by an event-triggered run) is broadcast here so ALL devices
        // viewing the session render it live — the same way agent messages do.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (!app.chatMessages) break;
        const mid = event.id || event.interaction_id || '';
        if (typeof app.beginRunControls === 'function') {
          try { app.beginRunControls(mid); } catch (_) {}
        }
        const cont = event.content || '';
        // Most user events are emitted before their durable row sequence is
        // assigned. Keep a provisional timestamp so mixed live/saved nodes can
        // still be placed sensibly until the active DB-tail poll stamps the seq.
        const createdAt = event.created_at || event.emit_time || new Date().toISOString();
        // GenUI-originated page sends (field/button prompts) carry a friendly
        // label — render a green notice instead of a "You" bubble; the raw
        // prompt only surfaces under the debug toggle.
        const genuiLabel = (event.genui_label || '').trim();
        if (typeof app._cacheAppendMessage === 'function') {
          app._cacheAppendMessage(eventSessionId || app.currentSessionId, {
            role: 'user', content: cont, id: mid || undefined,
            created_at: createdAt,
            ...(genuiLabel ? { genui: true, genui_label: genuiLabel } : {}),
          });
        }
        // Already shown (by interaction id)? Dedup.
        if (mid) {
          const sel = genuiLabel
            ? `.chat-bubble.info.system-genui[data-msg-id="${CSS.escape(String(mid))}"]`
            : `.chat-bubble.user[data-msg-id="${CSS.escape(String(mid))}"]`;
          if (app.chatMessages.querySelector(sel)) {
            if (!genuiLabel && typeof app.positionActivityGroupAfterOwner === 'function') {
              try { app.positionActivityGroupAfterOwner(mid); } catch (_) {}
            }
            break;
          }
        }
        if (genuiLabel) {
          if (typeof app.addChatBubble === 'function') {
            // Adopt the sender's own optimistic label notice (rendered locally
            // before the id was known) by matching text on an untagged one.
            if (mid) {
              const cands = app.chatMessages.querySelectorAll('.chat-bubble.info.system-genui:not([data-msg-id])');
              for (let i = cands.length - 1; i >= 0; i--) {
                const b = cands[i];
                const t = (b.querySelector('.bubble-body')?.textContent || '').trim();
                if (t === genuiLabel) {
                  b.setAttribute('data-msg-id', String(mid));
                  if (typeof app._setBubbleCreatedAt === 'function') app._setBubbleCreatedAt(b, createdAt);
                  if (typeof app._addBubbleActions === 'function') {
                    try { app._addBubbleActions(b); } catch (_) { /* reconcile heals later */ }
                  }
                  break;
                }
              }
              if (app.chatMessages.querySelector(
                    `.chat-bubble.info.system-genui[data-msg-id="${CSS.escape(String(mid))}"]`)) break;
            }
            const _gb = app.addChatBubble('info', genuiLabel, 'system-genui', undefined, undefined, mid || undefined, createdAt);
            if (_gb && typeof app._addBubbleActions === 'function') {
              try { app._addBubbleActions(_gb); } catch (_) { /* reconcile heals later */ }
            }
            if (app.isDebug && (cont || '').trim()) {
              app.addChatBubble('info', 'Raw prompt:\n' + cont, 'system-debug');
            }
          }
          break;
        }
        // Adopt the sender's optimistic bubble by text/id, or render a fresh one
        // — with its footer — through the shared helper. (addChatBubble never
        // builds a footer; renderer paths must go through _ensureUserBubble so
        // "user bubble" and "gutter" stay inseparable.)
        if (typeof app._ensureUserBubble === 'function') {
          try {
            const cls = event.source === 'event_trigger' ? 'event-trigger' : undefined;
            app._ensureUserBubble(cont, mid || undefined, createdAt, cls);
          } catch (_) { /* reconcile heals later */ }
        } else if (typeof app.addChatBubble === 'function') {
          const cls = event.source === 'event_trigger' ? 'event-trigger' : undefined;
          app.addChatBubble('user', cont, cls, undefined, undefined, mid || undefined, createdAt);
        }
        if (typeof app.positionActivityGroupAfterOwner === 'function') {
          try { app.positionActivityGroupAfterOwner(mid); } catch (_) {}
        }
        break;
      }

      case 'stream':
        // Live (or replayed) assistant text for the current session.
        // Routes to the bubble for this event's asst_id so each step
        // renders as its own bubble; falls back to turn_id for legacy
        // events. Lets a refresh / session-switch mid-stream reattach
        // to an in-flight run.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (typeof app.appendStreamToActiveBubble === 'function') {
          try {
            app.appendStreamToActiveBubble(
              event.content || '',
              event.asst_id || event.turn_id,
              event.created_at || event.emit_time,
              _ownerTurnId,
              _interactionSeq,
            );
          } catch(_) {}
        } else {
          console.warn('DEBUG-TAG:agentWs-stream-no-appender');
        }
        break;

      case 'agent_step_end':
        // An intermediate assistant step (text before tool calls) finished —
        // finalize its own bubble so the user sees every step, not just the last.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (!isMessageTypeVisible('progress')) break;
        if (typeof app.finalizeAgentStep === 'function') {
          try {
            app.finalizeAgentStep(
              event.content || '', event.asst_id || event.turn_id,
              event.created_at || event.emit_time,
              _ownerTurnId,
              _interactionSeq,
              event.progress_kind || '',
            );
          } catch(_) {}
        }
        break;

      case 'response':
        // Final assistant reply — the turn completed without more tool
        // calls. Can arrive for user-typed turns, event-triggered runs,
        // or replayed from a reconnect mid-stream.
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          console.debug('DEBUG-TAG:agentWs-response-skipped', { eventSessionId, currentSessionId: app.currentSessionId, replayed: event.replayed });
          break;
        }
        if (typeof app.finalizeAgentResponse === 'function') {
          try {
            // The closing gate's durable/live verdict is chat_render_kind.
            // message_phase remains only as a legacy fallback: using it first
            // folds a promoted closer into activity until a reload rehydrates
            // the durable promoted_answer row.
            app.finalizeAgentResponse(
              event.content || '', event.asst_id || event.turn_id,
              !!event.replayed, event.created_at || event.emit_time,
              _ownerTurnId,
              _interactionSeq,
              event.chat_render_kind || 'activity',
              !!event.mid_run,
            );
          } catch(_) {}
        } else if (typeof app.addChatBubble === 'function') {
          app.addChatBubble('agent', event.content || '');
        } else {
          console.warn('DEBUG-TAG:agentWs-response-no-finalizer');
        }
        break;

      case 'summary':
      case 'overview': // legacy event name — queued/replayed pre-rename events
        // User-facing final message from the Output Summarizer — a parallel
        // LLM loop that rewords the agent's messages from the finished run
        // (no tool calls) into one concise final message.
        // Persisted as a role='system' row (source 'system:overview' legacy /
        // 'system:summary'), so the DB-reconcile poll re-renders it identically
        // (dedup by data-msg-id).
        if (eventSessionId && eventSessionId !== app.currentSessionId) {
          if (event.replayed) _stashReplay(eventSessionId, event);
          break;
        }
        if (typeof app.endRunControls === 'function') {
          try { app.endRunControls(); } catch (_) {}
        }
        if (!isMessageTypeVisible('summary')) break;
        if (typeof app.renderSummary === 'function') {
          try {
            app.renderSummary(
              event.content || '',
              event.id || event.asst_id || '',
              event.created_at || event.emit_time,
              event.asst_id || '',
            );
          } catch(_) {}
        }
        break;

      case 'execution_mode': {
        // The agent switched Ask/Plan/Auto mid-conversation (set_execution_mode
        // tool — typically Plan→Auto once the user approved a plan). Update the
        // pill for the session being viewed so the user SEES it flip, and so the
        // next message carries the new mode. Don't honor on replay (a stale
        // reconnect switch shouldn't silently change the user's mode).
        if (event.replayed) break;
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        // The mode in force before this flip — the activity group's netting base,
        // so a flip that returns the group to where it began cancels the notice.
        const fromMode = app.executionMode;
        if (typeof app.setExecutionMode === 'function') {
          try { app.setExecutionMode(event.mode); } catch (_) { /* ignore */ }
        }
        // Live transcript notice: announce the flip where the bubbles are,
        // anchored to the running turn so it lands between that turn's tool
        // calls (mirrors markAgentInterrupted's placement).
        if (typeof app.notifyExecutionMode === 'function') {
          try { app.notifyExecutionMode(event.mode, { turnId: _ownerTurnId, from: fromMode }); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'repo_change_notice':
        // Passive heads-up from the Git Control "Notify me about repo changes"
        // watcher (plugins/abilities/Administrator/git_control). The working tree
        // newly diverged from the last commit (uncommitted edits and/or unpushed
        // commits). No agent run happened — it's an FYI. The notice is also
        // persisted into the session, so if it's not the one being viewed it
        // shows when the user opens that chat. Render live only when it targets
        // the session currently on screen, and never on replay (a stale
        // reconnect shouldn't re-announce an old change).
        if (event.replayed) break;
        if (eventSessionId && eventSessionId === app.currentSessionId
            && typeof app.addChatBubble === 'function') {
          // role 'agent' so it renders on the assistant side (matches how the
          // persisted role='assistant' row is re-rendered on session load).
          try {
            const _rb = app.addChatBubble('agent', event.message || 'Repo changes detected.', 'git-watch-notice');
            if (_rb && typeof app._addBubbleActions === 'function') app._addBubbleActions(_rb);
          } catch (_) { /* render error — the persisted row still covers it */ }
        }
        break;

      case 'compaction_completed':
        if (event.replayed) break;
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (typeof app.notifyCompactionCompleted === 'function') {
          try { app.notifyCompactionCompleted(event); } catch (_) { /* durable row remains */ }
        }
        break;

      case 'agent_created':
      case 'agent_trashed':
      case 'agent_deleted':
      case 'agent_restored':
      case 'agent_status':
        // An agent started/stopped a run, a session entered the gate queue, or
        // a /compact started/finished folding this session.
        if (event.status === 'compacting' && event.session_id) {
          // Compaction in progress (initiated here or on another device) — lock
          // the composer and show "Compacting…" above the pill until the
          // compact_done broadcast or the drain's 'running' event.
          if (event.session_id === app.currentSessionId
              && typeof app._lockComposerForCompaction === 'function') {
            try { app._lockComposerForCompaction(); } catch (_) { /* ignore */ }
          }
          break;
        }
        if (event.status === 'compact_done' && event.session_id) {
          if (event.session_id === app.currentSessionId
              && typeof app._unlockComposerForCompaction === 'function') {
            try { app._unlockComposerForCompaction(); } catch (_) { /* ignore */ }
          }
          break;
        }
        if (event.status === 'queued' && event.session_id) {
          // Session cap reached — this session waits in the FIFO queue.
          // Mark the user's bubble in the CURRENT view only (a bubble can only
          // exist for the session being viewed); refresh the dropdown always.
          if (event.session_id === app.currentSessionId
              && typeof app.markBubbleQueued === 'function') {
            try { app.markBubbleQueued(event.turn_id, event.queue_position, event.session_id); } catch (_) { /* ignore */ }
          }
          if (typeof app.onSessionGateQueue === 'function') {
            try { app.onSessionGateQueue(event); } catch (_) { /* ignore */ }
          }
          break;
        }
        if (event.status === 'running' && event.session_id) {
          // Run started — clear any queued styling from the bubble in the
          // current view (only relevant when this session IS the current one).
          if (event.session_id === app.currentSessionId
              && typeof app.clearBubbleQueued === 'function') {
            try { app.clearBubbleQueued(event.turn_id, event.session_id); } catch (_) { /* ignore */ }
          }
          // A drained compaction-queued message starting its turn means the
          // compaction is over — release any restore-driven composer lock.
          if (event.session_id === app.currentSessionId
              && typeof app._unlockComposerForCompaction === 'function') {
            try { app._unlockComposerForCompaction(); } catch (_) { /* ignore */ }
          }
          if (typeof app.onSessionGateRunning === 'function') {
            try { app.onSessionGateRunning(event); } catch (_) { /* ignore */ }
          }
          // Fall through to onAgentLifecycleEvent for the agents-grid status dot.
        }
        // An agent was created / trashed / permanently deleted / restored in
        // another tab or device, or one started/stopped a run. Let the Agents
        // page sync its grid (and per-card status dot) without a manual refresh.
        if (typeof app.onAgentLifecycleEvent === 'function') {
          try { app.onAgentLifecycleEvent(event); } catch (_) { /* ignore */ }
        }
        break;

      case 'ui_command': {
        // Agent-driven screen control (the "App Control" ability). The agent's
        // set_app_view tool emits this to rearrange the viewer's own screen:
        // switch the main view, show/hide the chat panel, resize it. Only obey
        // commands for the session the user is currently viewing, and never on
        // replay — a stale view-switch on reconnect would yank the user around.
        if (event.replayed) break;
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (event.action === 'set_view') {
          if (event.view && typeof window.__setMainTab === 'function') {
            try { window.__setMainTab(event.view); } catch (_) { /* ignore */ }
          }
          if (typeof event.show_chat === 'boolean' && typeof window.__applyChatVisible === 'function') {
            try { window.__applyChatVisible(event.show_chat); } catch (_) { /* ignore */ }
          }
          if (typeof event.chat_width === 'number' && typeof window.__setChatPanelWidth === 'function') {
            try { window.__setChatPanelWidth(event.chat_width); } catch (_) { /* ignore */ }
          }
        } else if (event.action === 'browser_popup') {
          // Agent opened/closed a floating in-app browser window for the user
          // (browser_control's browser_popup tool). Handler registered by
          // ui/browser-popup/js/browser-popup.js at boot.
          if (typeof window.__browserPopup === 'function') {
            try { window.__browserPopup(event); } catch (_) { /* ignore */ }
          }
        }
        break;
      }

      case 'tunnel_state': {
        // Terminal-tunnel mode toggled (user drives a terminal through chat).
        // The chat module mounts/unmounts the tunnel banner + embedded terminal.
        // Honor on replay too, so a reconnect re-mounts an active tunnel.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (typeof app.onTunnelState === 'function') {
          try { app.onTunnelState(event); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'terminal_chat_state':
      case 'terminal_stream':
      case 'terminal_step_end': {
        // Terminal Chat engine — live terminal view replaces chat bubbles.
        // The engine (plugins/engines/terminal_chat/) emits these events;
        // the frontend module (chat-terminal-engine.js) handles the xterm.js
        // mount/unmount and data piping.
        if (eventSessionId && eventSessionId !== app.currentSessionId) break;
        if (typeof app._terminalChatHandler === 'function') {
          try { app._terminalChatHandler(event); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'session_title': {
        // Auto-generated session name (Session Namer app function,
        // plugins/app_functions/session_titler/). status
        // 'generating' lights the header spinner; 'done' swaps in the new name.
        // Not gated on the active session — the cache is updated for any session
        // so the sidebar reflects the new name on its next render too.
        if (typeof app.applySessionTitle === 'function') {
          try { app.applySessionTitle(event); } catch (_) { /* ignore */ }
        }
        break;
      }

      case 'attachment':
        // Image/file attachment reference from the server (both user and agent).
        handleAttachmentEvent(event);
        break;

      case 'session_deleted':
        // A session was PERMANENTLY deleted elsewhere (another tab, or another
        // device via the hybrid tombstone sync) and its transcript is gone. Tell
        // the chat panel so, if this is the open session, it drops the transcript
        // and shows "Session not found" instead of a stale/empty view. The handler
        // itself decides whether it targets the open session and refreshes the
        // sidebar. Ignore on replay (a reconnect shouldn't re-fire an old delete).
        if (event.replayed) break;
        if (typeof app.onSessionDeleted === 'function') {
          try { app.onSessionDeleted(event); } catch (_) { /* ignore */ }
        }
        break;

      case 'kill_switch':
        // The kill switch was toggled from another tab/device of this user.
        // Repaint the header button, session dropdown and Sessions page right
        // away (they listen for kill-switch-changed) instead of waiting for
        // the 15s/30s polls. Ignore on replay (a reconnect shouldn't re-fire
        // an old toggle).
        if (event.replayed) break;
        try {
          window.dispatchEvent(new CustomEvent('kill-switch-changed', {
            detail: { engaged: !!event.engaged },
          }));
        } catch (_) { /* ignore */ }
        break;

      // All other event types (tool_call, tool_result, pipeline, db) are handled by app._loopHandler etc.
      default:
        break;
    }
  };

  app.agentWs.onclose = () => {
    // RECORDER-HOOK: optional reconnect witness; remove this block with recorder.
    try { window.__recorderTrace?.('connection.closed', {session_id:app.currentSessionId}); } catch (_) {}

    setAgentStatus('red');
    // Debug mode: show immediate "Connection lost." on the last bubble
    if (app.isDebug && app.isProcessing) {
      if (app.updateLastBubble) {
        app.updateLastBubble('Connection lost.', 'error');
      }
      app.isProcessing = false;
      if (app.chatSend) app.chatSend.disabled = false;
    }
    // Start 5-min timer for a user-facing connection-lost message (all modes).
    // The timer is cancelled on reconnect.
    _cancelConnectionLostTimer();
    if (app.isProcessing) {
      _connectionLostTimer = setTimeout(() => {
        if (!app.isProcessing) { _connectionLostTimer = null; return; }
        _showConnectionLost();
        _connectionLostTimer = null;
      }, CONNECTION_LOST_TIMEOUT);
    } else {
      // Even when nothing is processing, start the timer so a mid-session
      // disconnect still notifies after 5 minutes.
      _connectionLostTimer = setTimeout(() => {
        _showConnectionLost();
        _connectionLostTimer = null;
      }, CONNECTION_LOST_TIMEOUT);
    }
    // Drop the thinking glow — a live turn (if any) keeps running server-side
    // and re-lights via replay on reconnect.
    if (app.chatActivityStop) { try { app.chatActivityStop(); } catch (_) {} }
    scheduleAgentReconnect();
  };

  app.agentWs.onerror = () => {
    setAgentStatus('red');
  };

  // ── Output Summarizer: canonical terminal lane ──
  // A summary is an authoritative terminal update, not a second terminal
  // bubble. Locate its response's activity group and update/create the one
  // closer immediately after that group. Every live/reconcile/reload caller
  // uses this same route so response + summary converge idempotently.
  app.renderSummary = (content, rowId, createdAt, finalAsstId) => {
    if (!content || !app.chatMessages) return null;
    try {
      let group = null;
      if (finalAsstId) {
        const response = app.chatMessages.querySelector(
          `.ca-response-row[data-msg-id="${CSS.escape(String(finalAsstId))}"]`,
        );
        group = response && response.closest('.activity-group');
      }
      if (!group) {
        const terminal = finalAsstId && app.chatMessages.querySelector(
          `.terminal-closer[data-msg-id="${CSS.escape(String(finalAsstId))}"]`,
        );
        group = terminal && terminal.previousElementSibling
          && terminal.previousElementSibling.classList.contains('activity-group')
          ? terminal.previousElementSibling : null;
      }
      if (group && typeof app._renderCompletionCloser === 'function') {
        return app._renderCompletionCloser(
          content, rowId || finalAsstId || '', group.dataset.ownerTurnId || '',
          createdAt || undefined, group,
        );
      }
      // A group can be absent only for a standalone legacy summary. Keep that
      // fallback terminal-shaped, never a second summary bubble.
      if (typeof app.addChatBubble !== 'function') return null;
      return app.addChatBubble(
        'agent', content, 'terminal-closer', undefined, undefined,
        rowId || undefined, createdAt || undefined,
      );
    } catch (_) { return null; }
  };
}

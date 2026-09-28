'use strict';

// ── DB-tail reconcile (the durable "always-loaded" path) ────────────────────
// The chat WebSocket is the *smooth* real-time renderer, but it only works when
// the browser's socket and the running agent live in the SAME server process's
// memory. It also only carries turns this browser KNOWS are happening. Neither
// is true when a run STARTS somewhere else and writes into the session you're
// already looking at — most commonly an AUTOMATION / scheduled task / event
// trigger delivering "here", but also a message sent from another device, or a
// cross-worker (`gunicorn --workers N`) run. In all those cases the agent still
// streams its reply into the shared DB (assistant row `status='streaming'`,
// updated ~0.6s, and registered as the run's `assistant_interaction_id`).
//
// This module closes the gap by polling a cheap DB-tail endpoint for the OPEN
// session and rendering new/updated rows through the SAME bubble helpers the WS
// path uses. It runs at two cadences off one timer:
//   • a turn this browser is tracking (`isProcessing`) → poll every tick (fast).
//   • otherwise → poll slowly, just to DETECT a run that began elsewhere and
//     then light the live indicator + escalate to the fast cadence.
// While idle, a WebSocket-silence gate keeps this poll dormant on the happy
// (same-process) path. During an active turn we still poll: WS events provide
// smooth text, but only DB rows provide the durable interaction sequence. The
// time a session is open — it never stops itself when a turn finishes — so the
// NEXT externally-started run is still picked up without a manual refresh.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { isDebugMode } from '../../shared/js/app-prompts.js';
import { isMessageTypeVisible, messageTypeOf, isSummaryRow } from '../../shared/js/chat-visibility.js';
import { kvCache } from './storage/kv-cache.js';
import { addChatBubble, _setBubbleCreatedAt, _setBubbleSessionSeq } from './chat-bubble.js';
import { _setBubbleModelFromMeta, ensureUserBubble } from './chat-bubble-actions.js';
import { _cacheAppendMessage } from './chat-message-cache.js';
import {
  _stripToolCalls,
  seedStreamingBubble,
  finalizeAgentResponse,
  finalizeAgentStep,
  markAgentInterrupted,
  ensureStreamingBubbleForActiveTurn,
  _findAgentBubbleForTurn,
  _wsTurnBuffers,
  _setTurnBufferMonotonic,
} from './chat-stream.js?v=260';

const RECONCILE_INTERVAL_MS = 800;   // base timer cadence (the fast/active rate)
const WS_SILENCE_MS = 1200;          // only poll once the WS has been quiet this long
const IDLE_POLL_EVERY = 4;           // when idle, only fetch every Nth tick (~3.2s)

let _timer = null;
let _idleTicks = 0;

// An idle session only needs the durable tail while its chat surface can be
// seen.  Previously every app tab kept polling session-tail after the user
// collapsed Chat or switched the browser tab away, multiplying SQLite opens
// across otherwise inactive clients. Active turns remain exempt below: a
// hidden cross-worker run still needs durable completion/notification state.
function shouldPollIdleTail() {
  if (document.visibilityState === 'hidden') return false;
  try {
    if (typeof window.__getChatVisible === 'function') {
      return window.__getChatVisible() === true;
    }
  } catch (_) { /* fall through to DOM state */ }
  const panel = document.getElementById('chat-panel');
  return !panel || getComputedStyle(panel).display !== 'none';
}

function startReconcileLoop() {
  if (_timer) return;
  _idleTicks = 0;
  _timer = setInterval(_tick, RECONCILE_INTERVAL_MS);
}

function stopReconcileLoop() {
  if (_timer) {
    clearInterval(_timer);
    _timer = null;
  }
}

function _tick() {
  const sid = app.currentSessionId;
  if (!sid) return;
  const last = (app._lastWsEventAt && app._lastWsEventAt[sid]) || 0;
  if (app.isProcessing) {
    _idleTicks = 0;
    // Rendering is idempotent by interaction id. Polling alongside WS is how
    // live bubbles promptly acquire their authoritative interaction sequence.
    _reconcileOnce(sid);
    return;
  }
  if (!shouldPollIdleTail()) {
    // Prime the cadence so reopening Chat gets one verification within the next
    // 800ms tick rather than waiting a full idle cycle (~3.2s).
    _idleTicks = IDLE_POLL_EVERY - 1;
    return;
  }
  if (Date.now() - last < WS_SILENCE_MS) return;       // WS is delivering → stay dormant

  // Idle: poll on a slower cadence purely to DETECT a run that began elsewhere
  // (an automation, a cross-device message). Cheap query — usually returns
  // nothing — but it's what keeps the open session "always updated".
  _idleTicks += 1;
  if (_idleTicks < IDLE_POLL_EVERY) return;
  _idleTicks = 0;
  _reconcileOnce(sid);
}

// Cheap fingerprint of an assistant row so an idle poll doesn't repaint a row
// it already drew. The server unions the in-progress assistant row — and, since
// run-state keeps `assistant_interaction_id` after the run ends, the COMPLETED
// row too — into every tail, so without this guard a finished bubble would be
// re-finalized on every idle tick.
function _renderSig(msg) {
  return (msg.status || '') + ':' + ((msg.content || '').length);
}

// Signal session pre-warming (session-prewarm.js) that a turn in this open
// session reached a terminal row (complete / error / interrupted) — including
// runs started elsewhere (automation, event, another device) that landed here
// via the reconcile poll. Fire-and-forget; no listeners = no-op. The pre-warm
// module's recency gate dedupes the overlap with chat-activity.js's live-WS
// dispatch.
function _dispatchTurnCompleted(sid) {
  try {
    window.dispatchEvent(new CustomEvent('webagent-turn-completed', {
      detail: { sessionId: sid },
    }));
  } catch (_) { /* non-fatal */ }
}

async function _reconcileOnce(sid) {
  if (app._reconcileInFlight) return;                  // never overlap fetches
  app._reconcileInFlight = true;
  const wasProcessing = app.isProcessing;
  try {
    // Do not use the WebSocket replay cursor here. session_seq is allocated to
    // every stream/pipeline event, but this endpoint returns interaction rows.
    // Sharing the cursor lets a later non-row event skip an earlier row forever.
    const after = (app.lastInteractionSeq && app.lastInteractionSeq[sid]) || 0;
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('cache.reconcile.request', {
        session_id: sid,
        interaction_cursor: after,
      });
    } catch (_) {}
    window.__chatPollLastAt = new Date().toISOString();

    const token = localStorage.getItem('auth_token');
    let url = apiPath(
      `/api/v1/db/session-tail?db=user.db&session_id=${encodeURIComponent(sid)}`
      + `&after_session_seq=${after}`
      + (app.currentUserId ? `&user_id=${encodeURIComponent(app.currentUserId)}` : '')
      + (token ? `&token=${encodeURIComponent(token)}` : ''),
    );
    let data;
    try {
      const res = await fetch(url);
      if (!res.ok) return;
      data = await res.json();
    } catch (_) {
      return;                                          // network blip — try again next tick
    }
    if (!data || data.restricted) return;
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('cache.reconcile.response', {
        session_id: sid,
        decision: sid===app.currentSessionId?'merge':'discard',
        reason: sid===app.currentSessionId?'active-session':'session-changed',
        message_count: Array.isArray(data.messages)?data.messages.length:0,
        interaction_cursor: after,
      });
    } catch (_) {}
    if (sid !== app.currentSessionId) return;          // user switched away mid-fetch

    if (!app._reconcileSig) app._reconcileSig = {};

    let maxSeq = after;
    const msgs = Array.isArray(data.messages) ? data.messages : [];
    for (const msg of msgs) {
      if (typeof msg.session_seq === 'number') maxSeq = Math.max(maxSeq, msg.session_seq);
      _cacheAppendMessage(sid, msg);

      if (msg.role === 'user') {
        _renderUserRow(msg);
        continue;
      }
      if (msg.role === 'system') {
        // Debug-only system messages are hidden when debug mode is off.
        const src = (msg.source || '');
        if (src.startsWith('system:debug:') && !isDebugMode()) continue;
        // Durable mode/model notices (system:mode / system:model) are NOT
        // rendered per-row here: the live pill/WS notice already announced the
        // switch on this session, and the re-projection below
        // (_appendMessagesToTranscript) renders the row with live-dedup when no
        // notice is on screen (e.g. a cold reload).
        if (src === 'system:mode' || src === 'system:model') continue;
        // Per-type visibility: skip lanes the user filtered out (e.g. Summary).
        const mtype = messageTypeOf(msg);
        if (mtype && !isMessageTypeVisible(mtype)) continue;
        const key = msg.id;
        const sigKey = sid + '|' + key;
        const sig = _renderSig(msg);
        if (msg.status !== 'streaming' && app._reconcileSig[sigKey] === sig) continue;
        app._reconcileSig[sigKey] = sig;
        _renderSystemRow(msg);
        continue;
      }
      if (msg.role !== 'assistant') continue;          // tool rows are handled elsewhere

      // Per-type visibility: skip lanes the user filtered out (main/progress).
      const mtype = messageTypeOf(msg);
      if (mtype && !isMessageTypeVisible(mtype)) continue;

      const key = msg.id;
      const existingBubble = _findAgentBubbleForTurn(key);
      if (existingBubble) _setBubbleCreatedAt(existingBubble, msg.created_at);
      // Skip a re-render of an assistant row whose content/status is unchanged
      // since we last drew it (streaming always re-renders — its text grows).
      const sigKey = sid + '|' + key;
      const sig = _renderSig(msg);
      if (msg.status !== 'streaming' && app._reconcileSig[sigKey] === sig) continue;
      app._reconcileSig[sigKey] = sig;

      const text = _stripToolCalls(msg.content || '');
      if (msg.status === 'streaming') {
        // SET (not append) absolute content keyed by asst_id → idempotent, and
        // keep the WS appender's buffer in sync so it can resume cleanly later.
        // Use the monotonic writer: the durable snapshot lags the live WS stream
        // (~0.6s persist throttle), so it must never overwrite a longer live
        // buffer with a shorter "RED" fragment and truncate the step at finalize.
        seedStreamingBubble(
          key, text, msg.created_at, msg.turn_id,
          msg.interaction_seq ?? msg.session_seq,
        );
        _setTurnBufferMonotonic(key, text);
      } else if (msg.status === 'interrupted') {
        markAgentInterrupted(
          key, msg.created_at, msg.interaction_seq ?? msg.session_seq, msg.turn_id,
        );
        _dispatchTurnCompleted(sid);
      } else if (msg.status === 'error') {
        markAgentInterrupted(
          key, msg.created_at, msg.interaction_seq ?? msg.session_seq,
          msg.turn_id, 'Error',
        );
        _dispatchTurnCompleted(sid);
        // markAgentInterrupted renders the terminal closer + ends active controls;
        // nothing else in this iteration should run for an errored row.
        continue;
      } else {
        // 'complete' (or legacy null status) — finalize this step's bubble.
        const renderKind = String(msg.chat_render_kind || 'activity').toLowerCase();
        // Terminal assistant row — signal session pre-warming so the finished
        // transcript lands in the IndexedDB cache for a fast re-open. Fired
        // for every complete row (main/final AND progress steps); error and
        // interrupted rows dispatch from their own branches. (Live WS turns
        // already fire this via chat-activity.js; the pre-warm module's
        // recency gate dedupes the overlap.)
        _dispatchTurnCompleted(sid);
        if (renderKind === 'promoted_answer' || renderKind === 'notice') {
          finalizeAgentResponse(
            text, key, false, msg.created_at, msg.turn_id,
            msg.interaction_seq ?? msg.session_seq,
            renderKind,
            renderKind === 'notice',
          );
        } else {
          let progressKind = String(msg.progress_kind || '').toLowerCase();
          if (!progressKind && msg.metadata) {
            try {
              progressKind = String(JSON.parse(msg.metadata).progress_kind || '').toLowerCase();
            } catch (_) {}
          }
          finalizeAgentStep(
            text, key, msg.created_at, msg.turn_id,
            msg.interaction_seq ?? msg.session_seq,
            progressKind,
          );
        }
        const b = _findAgentBubbleForTurn(key);
        if (b && msg.metadata) _setBubbleModelFromMeta(b, msg.metadata);
      }
      const renderedBubble = _findAgentBubbleForTurn(key);
      if (renderedBubble) {
        _setBubbleCreatedAt(renderedBubble, msg.created_at);
        _setBubbleSessionSeq(renderedBubble, msg.session_seq);
      }
    }

    // Re-project persisted assistant/tool rows through the turn-owned activity
    // renderer. Entry and tool-call ids make this idempotent with the WS path.
    if (msgs.length && typeof app._appendMessagesToTranscript === 'function') {
      app._appendMessagesToTranscript(msgs, true, true);
    }
    const run = data.run || null;

    // Make sure a placeholder exists for an in-flight turn even if no row text yet.
    if (run && run.active && run.assistant_interaction_id) {
      const activeRow = msgs.find(m => m.id === run.assistant_interaction_id);
      ensureStreamingBubbleForActiveTurn(
        run.assistant_interaction_id,
        activeRow && activeRow.created_at,
      );
    }

    // Advance the durable-row cursor only after the rows were consumed.
    if (!app.lastInteractionSeq) app.lastInteractionSeq = {};
    app.lastInteractionSeq[sid] = Math.max(app.lastInteractionSeq[sid] || 0, maxSeq);

    // The event cursor remains independent and is used only by WS replay.
    // Persist only the CURRENT session's row: the map can hold dozens of
    // sessions and this runs on every reconcile tick (fast cadence while a
    // turn is active) — a full-map setAll would queue a put per session per
    // tick and flood the flush chain.
    if (!app.lastSessionSeq) app.lastSessionSeq = {};
    const floor = run && typeof run.latest_session_seq === 'number' ? run.latest_session_seq : 0;
    app.lastSessionSeq[sid] = Math.max(app.lastSessionSeq[sid] || 0, floor);
    kvCache.set('chat:lastSeq:' + sid, app.lastSessionSeq[sid]);

    const runActive = !!(run && run.active);
    // A run parked in the auto-resume ladder (stopped involuntarily, waiting for
    // the watchdog/boot sweep to re-ignite it) is active but silent. Hold a mock
    // "re-assessing…" note on it and RE-ARM on every tick — the in-turn watchdog
    // would otherwise self-clear the bar during a long backoff, which is exactly
    // the "goes idle then starts working again" flicker. It clears the moment
    // `run.active` goes false, i.e. when the revival finally fails.
    const runRecovering = !!(run && run.recovering);

    // A server-active run needs a visible chat progress note even during gaps
    // between detailed activity events. Restore the generic "Working…" note
    // only on a new active run or after the local activity indicator went quiet;
    // live tool/writing/sending notes remain authoritative while it is active.
    const chatActivityLive = typeof app.chatActivityIsLive === 'function'
      ? app.chatActivityIsLive()
      : false;
    if (runActive && (runRecovering || !wasProcessing || !chatActivityLive)
        && typeof app.chatActivityRestore === 'function') {
      try {
        app.chatActivityRestore(run.current_op || null,
          runRecovering ? (app.chatActivityMockLabel?.('reassessing') || 'Re-assessing…') : null);
      } catch (_) {}
    }

    // Turn finished — clear the working state, but KEEP the loop alive so the
    // next externally-started run on this open session is still picked up live
    // (the whole point: no manual refresh). Only act on the active→idle edge.
    if (run && run.active === false && wasProcessing) {
      app.isProcessing = false;
      if (typeof app.endRunControls === 'function') {
        try { app.endRunControls(); } catch (_) {}
      }

      if (app.chatActivityStop) { try { app.chatActivityStop(); } catch (_) {} }
      if (app.chatSend) app.chatSend.disabled = !(app.chatInput && app.chatInput.value.trim());
      if (app._healingTimer) { clearTimeout(app._healingTimer); app._healingTimer = null; }
    }

    // Stop-pending fallback: a stop was clicked but no system signal cleared
    // it — the WS event never arrived, or the stop landed on a run that was
    // already finished (a no-op stop). Once the server reports the run
    // inactive, the stop is complete: release the pin so "Stopping…" can never
    // linger past the turn. (The interrupted DB row, if any, already rendered
    // its "Stopped" transcript message in the loop above and cleared the flag.)
    if (app._stopPending && (!run || run.active === false)) {
      app._stopPending = false;
      app.isProcessing = false;
      if (app.chatActivityStop) { try { app.chatActivityStop(); } catch (_) {} }
    }
  } finally {
    app._reconcileInFlight = false;
  }
}

// Extract the friendly label for a GenUI-originated user row (page field/button
// sends). Returns null for normal user messages. Handles metadata as either a
// JSON string (DB endpoints) or an object (live WS path).
function _genuiLabelFromMeta(metaRaw) {
  if (!metaRaw) return null;
  let m = metaRaw;
  if (typeof m === 'string') { try { m = JSON.parse(m); } catch (_) { return null; } }
  if (!m || typeof m !== 'object' || !m.genui) return null;
  return String(m.genui_label || '').trim() || null;
}

// Render a user row the same way agentWs.js does: dedup by interaction id, and
// adopt an existing optimistic bubble (sent locally before its id was known).
function _renderUserRow(msg) {
  if (!app.chatMessages) return;
  const mid = msg.id || '';
  const cont = msg.content || '';
  // ── GenUI-originated page sends (field/button prompts) ──
  // Render the page's friendly label as a green notice instead of a "You"
  // bubble; the raw prompt only surfaces under the debug toggle.
  const genuiLabel = _genuiLabelFromMeta(msg.metadata);
  if (genuiLabel) {
    if (mid && app.chatMessages.querySelector(
          `.chat-bubble.info.system-genui[data-msg-id="${CSS.escape(String(mid))}"]`)) {
      return;
    }
    // Adopt the sender's own optimistic label notice (rendered locally before
    // the id was known) by matching text on an untagged one.
    if (mid) {
      const cands = app.chatMessages.querySelectorAll('.chat-bubble.info.system-genui:not([data-msg-id])');
      for (let i = cands.length - 1; i >= 0; i--) {
        const b = cands[i];
        const t = (b.querySelector('.bubble-body')?.textContent || '').trim();
        if (t === genuiLabel) {
          b.setAttribute('data-msg-id', String(mid));
          _setBubbleCreatedAt(b, msg.created_at);
          _setBubbleSessionSeq(b, msg.session_seq);
          return;
        }
      }
    }
    const gb = (typeof app.addChatBubble === 'function')
      ? app.addChatBubble('info', genuiLabel, 'system-genui', undefined, undefined, mid || undefined, msg.created_at)
      : addChatBubble('info', genuiLabel, 'system-genui', undefined, undefined, mid || undefined, msg.created_at);
    if (isDebugMode() && (cont || '').trim()) {
      const dbg = (typeof app.addChatBubble === 'function')
        ? app.addChatBubble('info', 'Raw prompt:\n' + cont, 'system-debug', undefined, undefined, undefined, msg.created_at)
        : addChatBubble('info', 'Raw prompt:\n' + cont, 'system-debug', undefined, undefined, undefined, msg.created_at);
      if (mid) dbg.setAttribute('data-debug-for', String(mid));
    }
    _setBubbleSessionSeq(gb, msg.session_seq);
    return;
  }
  // One shared helper: dedupe by id, adopt an untagged optimistic bubble by
  // text, or create + wire the footer — a user row can never render bare.
  const bubble = ensureUserBubble(cont, mid || undefined, msg.created_at);
  if (bubble) {
    _setBubbleSessionSeq(bubble, msg.session_seq);
    if (typeof app.positionActivityGroupAfterOwner === 'function') {
      try { app.positionActivityGroupAfterOwner(mid); } catch (_) {}
    }
  }
}

function _isSuccessfulCompactionRow(msg) {
  if (!msg || (msg.source || '') !== 'system:compaction') return false;
  let metadata = msg.metadata || {};
  if (typeof metadata === 'string') {
    try { metadata = JSON.parse(metadata); } catch (_) { return false; }
  }
  const compact = metadata && metadata.compaction;
  return !!(compact && compact.status === 'success');
}

// Render a system message. Debug/error/retry rows render as muted info
// bubbles — visible to the user, never fed to the agent. A closer row renders
// as its own content-only final-output bubble after the folded working response.
function _renderSystemRow(msg) {
  if (!app.chatMessages) return;
  const mid = msg.id || '';
  const cont = msg.content || '';
  const src = msg.source || '';
  if (isSummaryRow(msg) && typeof app.endRunControls === 'function') {
    try { app.endRunControls(); } catch (_) {}
  }
  // Successful compactions have a structured presentation contract: a short
  // two-second notice plus a durable row inside the collapsed activity group.
  // Sending this row through the generic system renderer first leaves the full
  // detail behind as a permanent standalone info bubble during live reconcile.
  if (_isSuccessfulCompactionRow(msg)) {
    if (typeof app._appendMessagesToTranscript === 'function') {
      app._appendMessagesToTranscript([msg], true, true);
    }
    return;
  }
  // Summary rows are agent-class bubbles now, so the dedup must match any
  // bubble class, not just .info.
  if (mid) {
    const rendered = app.chatMessages.querySelector(
      `.chat-bubble[data-msg-id="${CSS.escape(String(mid))}"]`,
    );
    if (rendered) {
      _setBubbleCreatedAt(rendered, msg.created_at);
      _setBubbleSessionSeq(rendered, msg.session_seq);
      return;
    }
  }
  if (isSummaryRow(msg)) {
    const bubble = typeof app.renderSummary === 'function'
      ? app.renderSummary(cont, mid || undefined, msg.created_at, msg.parent_id || '')
      : (typeof app.addChatBubble === 'function'
        ? app.addChatBubble('agent', cont, 'terminal-closer', undefined, undefined, mid || undefined, msg.created_at)
        : addChatBubble('agent', cont, 'terminal-closer', undefined, undefined, mid || undefined, msg.created_at));
    _setBubbleSessionSeq(bubble, msg.session_seq);
    return;
  }
  const extraClass = src.startsWith('system:debug:') ? 'system-debug'
    : src === 'system:usage_limit' ? 'system-usage-limit'
    : src === 'system:error' ? 'system-error'
    : src === 'system:retry' ? 'system-retry'
    : undefined;
  const bubble = (typeof app.addChatBubble === 'function')
    ? app.addChatBubble('info', cont, extraClass, undefined, undefined, mid || undefined, msg.created_at)
    : addChatBubble('info', cont, extraClass, undefined, undefined, mid || undefined, msg.created_at);
  _setBubbleSessionSeq(bubble, msg.session_seq);
  if (src === "system:usage_limit") app.renderUsageLimitNotice?.(bubble, msg.metadata);
  // Live system notices (compaction done, slash results, recovery) need the
  // same footer the session-load path gives them — reconcile is their renderer.
  if (typeof app._addBubbleActions === 'function') {
    try { app._addBubbleActions(bubble); } catch (_) { /* render healed on next tick */ }
  }
}

app.startReconcileLoop = startReconcileLoop;
app.stopReconcileLoop = stopReconcileLoop;

// Pre-seed the reconcile signature cache with already-rendered assistant rows
// so the first reconcile tick doesn't destroy-and-rebuild every bubble.
// Called from session-load.js after _renderSessionWindowed.
function seedReconcileSig(sid, messages) {
  if (!sid || !Array.isArray(messages)) return;
  if (!app._reconcileSig) app._reconcileSig = {};
  for (const msg of messages) {
    if ((msg.role !== 'assistant' && msg.role !== 'system') || !msg.id) continue;
    const key = sid + '|' + msg.id;
    app._reconcileSig[key] = _renderSig(msg);
  }
}
app.seedReconcileSig = seedReconcileSig;

export {
  startReconcileLoop,
  seedReconcileSig,
  shouldPollIdleTail,
  _renderSystemRow,
};

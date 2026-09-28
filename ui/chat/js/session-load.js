'use strict';

// Session loader — fetch session messages, handle `restricted` (token refresh +
// retry, auto-create fresh session), windowed virtual-scroll render, attach
// persisted tool-call panels, infinite scroll. Sets app.loadSessionChat.
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { notifyActiveSessionChanged } from '../../shared/js/session-events.js';
import { getActive } from '../../shared/js/accounts.js';
import { ensureFreshToken } from '../../shared/js/auth-refresh.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { addChatBubble, _renderMarkdownBody, linkifyText, _setBubbleCreatedAt, _setBubbleSessionSeq, markMidRunOpening } from './chat-bubble.js';
import { _restorePersistenceStatus } from './chat-send.js';
import { renderAttachmentElement } from '../../shared/js/attachments.js';
import { storageAdapter } from './storage/storage-adapter.js';
import {
  _addBubbleActions,
  _bubbleAnchorId,
  _getBubbleText,
  _setBubbleModelFromMeta,
} from './chat-bubble-actions.js';
import {
  _stripToolCalls,
  _isSubstantiveAnswer,
  seedStreamingBubble,
  ensureStreamingBubbleForActiveTurn,
  attachToolCallsToLastBubble,
  _setBubbleText,
  MODE_NOTICE_LABEL,
  MODEL_NOTICE_LABEL,
  _projectModeNotices,
  _projectModelNotices,
} from './chat-stream.js?v=260';
import {
  _messageCache,
  _CACHE_TTL_MS,
  _sessionFocus,
  _loadSessionFocus,
  _persistSessionFocus,
  _captureSessionFocus,
  _cacheAppendMessage,
  _sortMessagesCanonical,
  _rememberSessionManifest,
  _transcriptChangedRemotely,
} from './chat-message-cache.js';
import {
  _installVirtualScroll,
  _teardownVirtualScroll,
  _storeBubbleHeight,
  _makePlaceholder,
  _getBubbleHeight,
  _placeholderIds,
} from './chat-virtual-scroll.js';
import { _fetchRelatedSessions, _sessionsCache } from './session-list.js';
import { startReconcileLoop } from './chat-reconcile.js';
import { isDebugMode, chatUiFlag } from '../../shared/js/app-prompts.js';
import { isMessageTypeVisible, messageTypeOf, isSummaryRow } from '../../shared/js/chat-visibility.js';
import { _agentEngineFor } from './session-agent.js';
import { replaceRouteParams } from '../../shared/js/url-state.js';

// ── Open-fetch sizing ───────────────────────────────────────────────────────
// The opening download is kept small (and, when a saved scroll position exists,
// CENTRED on it) so a session reopens fast instead of hauling its whole tail.
// Older/newer rows then page in on scroll. Tool-call BODIES aren't downloaded at
// all here (light mode) — only headings; bodies load on expand.
const _OPEN_NEWEST_LIMIT = 40;   // newest-N slice when opening at the bottom
// A current activity disclosure must include every durable row in its owning
// user turn: a cap produces plausible but false tool/message counts and can
// strand its terminal closer. Virtual rendering and light payloads keep the
// completed turn inexpensive once it is fetched.
const _OPEN_BOUNDARY_ROW_CAP = 0;
const _OPEN_AROUND_RADIUS = 25;  // rows each side of the saved anchor
const _OLDER_BATCH = 20;         // scroll-up page size; keep tool-heavy pagination responsive
const _NEWER_BATCH = 40;         // scroll-down page size

// ── Infinite scroll guards ──────────────────────────────────────────────────
let _loadingMoreMessages = false;   // loading OLDER (scroll up)
let _loadingNewerMessages = false;  // loading NEWER (scroll down, anchored open)
let _sessionLoadEpoch = 0;
// Activity disclosures are first-class virtual rows too. Keeping every collapsed
// group live defeated message virtualization on tool-heavy sessions and could
// expose an old group's heading while its owning user bubble was a placeholder.
const _virtualActivityGroups = new Map();

if (typeof document !== 'undefined') {
  document.addEventListener('webagent-auth-transition-start', () => {
    _sessionLoadEpoch += 1;
    _messageCache.clear();
    _removeUpdateSkeleton();
    if (_syncRetryTimer) clearTimeout(_syncRetryTimer);
    _syncRetryTimer = null;
    if (app.chatMessages) app.chatMessages.replaceChildren();
    app._lastLoadedSessionId = null;
  });
}

function _maxRenderedSeq(messages) {
  let max = 0;
  for (const msg of messages || []) {
    const seq = Number(msg && msg.session_seq);
    if (Number.isFinite(seq)) max = Math.max(max, seq);
  }
  return max;
}

// Arm the always-on DB-reconcile watcher for the session now on screen. Seeds
// the poll cursor to the newest seq we just rendered — OR to `floorSeq` (the
// session's true latest, passed when we opened mid-history) so the idle poll
// only ever renders genuinely NEW rows (e.g. an automation writing into this
// session), never re-renders what's already shown and never backfills the gap
// between an anchored window and the tail — then starts the loop (idempotent).
function _armSessionWatch(sessionId, messages, floorSeq) {
  if (!sessionId) return;
  if (!app.lastSessionSeq) app.lastSessionSeq = {};
  if (!app.lastInteractionSeq) app.lastInteractionSeq = {};
  // A full/cache load is an authoritative reset (important after rollback or
  // replacement), so never retain a stale higher DB cursor from an older view.
  let mx = typeof floorSeq === 'number' ? floorSeq : 0;
  if (Array.isArray(messages)) {
    for (const m of messages) {
      if (typeof m.session_seq === 'number' && m.session_seq > mx) mx = m.session_seq;
    }
  }
  app.lastInteractionSeq[sessionId] = mx;
  // A persisted interaction sequence is also a safe lower bound for replay,
  // but the event cursor may legitimately be much higher.
  app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, mx);
  try { startReconcileLoop(); } catch (_) { /* best-effort */ }
}

function _parseDuration(metadataStr) {
  if (!metadataStr) return null;
  try {
    const meta = JSON.parse(metadataStr);
    return meta.duration_ms != null ? meta.duration_ms : null;
  } catch (_) { return null; }
}

// Group tool-result messages by the assistant message that triggered them.
function _buildToolResultsByParent(messages) {
  const map = {};
  for (const msg of messages) {
    if (msg.role === 'tool' && msg.parent_id) {
      if (!map[msg.parent_id]) map[msg.parent_id] = [];
      map[msg.parent_id].push(msg);
    }
  }
  return map;
}

// Reconstruct the backend's inference-turn numbers for a durable transcript.
// The live indicator numbers turns from the loop's turn_count (one per LLM
// call, reset per user exchange), so its rows show "Turn N" badges. Each LLM
// call persists exactly one role='assistant' row, so the durable renderer
// reproduces the SAME badges by counting finalized assistant rows (status
// !== 'streaming' — an in-flight or rolled-back row was never a completed
// loop turn) since the last user message, in canonical order — the same order
// in which turn_start events fired during streaming. Returns Map<msgId, turn>.
function _computeTurnNumbers(messages) {
  const map = new Map();
  let n = 0;
  for (const msg of messages) {
    if (!msg || typeof msg !== 'object') continue;
    if (msg.role === 'user') { n = 0; continue; }
    if (msg.role !== 'assistant') continue;
    if (msg.status === 'streaming') continue;
    n += 1;
    if (msg.id) map.set(String(msg.id), n);
  }
  return map;
}

// Build the per-call objects for one assistant message's saved tool_calls.
// `turnNum` > 0 makes each row show a "Turn N" badge when expanded.
// `light`: the payload carries only tool-call NAMES (+ durations) — the heavy
// bodies (arguments, results, LLM output) were never downloaded, so each
// call is stamped with `_needsDetail` + `_detailMsgId`/`_detailIdx` so the panel
// can lazy-fetch exactly one body via /session-tool-detail when that row opens.
function _buildCallsForMessage(msg, toolResultsByParent, turnNum, light) {
  if (!msg.output) return [];
  let outputData;
  try { outputData = JSON.parse(msg.output); } catch (_) { return []; }
  const toolCalls = outputData.tool_calls;
  if (!toolCalls || toolCalls.length === 0) return [];

  // Remote skeleton — tool names + IDs only, no real data. Build minimal
  // entries with status 'remote_placeholder' so the UI shows headings but
  // the body says "not saved to remote".
  if (outputData._remote_placeholder) {
    return toolCalls.map((tc, i) => ({
      tool: tc.function ? tc.function.name : '(unknown)',
      toolCallId: tc.id || null,
      args: {},
      status: 'remote_placeholder',
      result: null,
      durationMs: null,
      errorType: null,
      turn: turnNum || 0,
      open: false,
      _savedOutput: null,
      _savedToolOutput: null,
      _savedToolMetadata: null,
      _detailMsgId: null,
      _detailIdx: 0,
      _needsDetail: false,
      _remotePlaceholder: true,
      interactionSeq: msg.interaction_seq != null ? msg.interaction_seq : msg.session_seq,
    }));
  }

  return toolCalls.map((tc, i) => {
    let args = {};
    try { args = JSON.parse(tc.function.arguments); } catch (_) {}
    const toolName = tc.function.name;
    const results = toolResultsByParent[msg.id] || [];
    // Pair by tool name, then fall back to positional order within this parent's
    // results. Both lists are per-assistant-row and in call order, so results[i]
    // is the i-th call's result — this covers tool rows saved WITHOUT a tool_name
    // (e.g. older Local Claude Code turns, whose engine didn't stamp it).
    const resultEntry = results.find(r => r.tool_name === toolName) || results[i] || null;
    const deleted = resultEntry && resultEntry.status === 'deleted';
    return {
      tool: toolName,
      toolCallId: tc.id || null,
      args: args,
      status: deleted ? 'deleted' : 'done',
      result: resultEntry ? resultEntry.content : null,
      durationMs: resultEntry ? _parseDuration(resultEntry.metadata) : null,
      errorType: null,
      turn: turnNum || 0,
      open: false,
      _savedOutput: msg.output || null,
      _savedToolOutput: resultEntry ? resultEntry.output : null,
      _savedToolMetadata: resultEntry ? resultEntry.metadata : null,
      _detailMsgId: msg.id || null,
      _detailIdx: i,
      _needsDetail: !!light,
      _deleted: deleted,
      interactionSeq: msg.interaction_seq != null ? msg.interaction_seq : msg.session_seq,
    };
  });
}

// Reattach tool calls to assistant TEXT bubbles after a reload. Tool-only turns
// are rendered as grouped bubbles during the windowed pass, so they're skipped
// here. Out-of-window messages (placeholders) are skipped to avoid attaching
// their calls to the wrong bubble.
function _attachToolCallsFromMessages(messages, light, turnNumbers) {
  if (!app.attachToolCallsToLastBubble) return;
  const toolResultsByParent = _buildToolResultsByParent(messages);
  if (!turnNumbers) {
    // Append/reconcile paths hand over a partial batch; count across the full
    // cached transcript so mid-exchange batches keep the true turn numbers.
    const cached = app.currentSessionId ? _messageCache.get(app.currentSessionId) : null;
    turnNumbers = _computeTurnNumbers(
      cached && cached.messages && cached.messages.length ? cached.messages : messages,
    );
  }

  for (const msg of messages) {
    if (msg.role !== 'assistant') continue;
    let text = msg.content || '';
    text = _stripToolCalls(text);
    if (!text.trim()) continue;          // tool-only / empty → handled elsewhere
    const calls = _buildCallsForMessage(msg, toolResultsByParent, turnNumbers.get(String(msg.id)) || 0, light);
    if (calls.length === 0) continue;
    const targetBubble = msg.id
      ? app.chatMessages.querySelector(`.chat-bubble.agent[data-msg-id="${msg.id}"]`)
      : null;
    if (!targetBubble) continue;         // out of window → don't misattribute
    app.attachToolCallsToLastBubble(calls, targetBubble);
  }
}

// Called by the virtual scroller after reconstructing a finalized text bubble.
// Reattach only that bubble's lightweight tool headings; detail bodies remain
// lazy and are fetched on expansion exactly as they are on the initial render.
function _rehydrateVirtualBubble(msgId, bubble) {
  const cached = app.currentSessionId ? _messageCache.get(app.currentSessionId) : null;
  if (!cached || !bubble || !app.attachToolCallsToLastBubble) return;
  const msg = cached.messages.find(m => m.id === msgId);
  if (!msg || msg.role !== 'assistant') return;
  const text = _stripToolCalls(msg.content || '');
  if (!text.trim()) return;
  const calls = _buildCallsForMessage(
    msg,
    _buildToolResultsByParent(cached.messages),
    _computeTurnNumbers(cached.messages).get(String(msg.id)) || 0,
    cached.light !== false,
  );
  if (calls.length) app.attachToolCallsToLastBubble(calls, bubble);
}

app._rehydrateVirtualBubble = _rehydrateVirtualBubble;

// Synthetic STANDALONE tool rows — written outside the normal assistant
// tool_call pairing, so the parent-lookup renderer skips them. Two kinds:
//   • vision ingestion — process_image (describe path) / route_attachment
//     (unreadable path), parented to the USER turn before the reply exists;
//   • loop-node memory — memory_search (runs before the turn) / memory_save
//     (runs after), tagged metadata.brain on the backend.
// The backend flags every one `_synth_tool` (vision also has the parent's role
// as a fallback signal). Vision renders its own foldable tool-call bubble in
// chronological position. Memory renders a small debug note instead.
const _SYNTH_TOOLS = {
  process_image: true, route_attachment: true,
  memory_search: true, memory_save: true,  // shown as debug notes, not bubbles
  app_control: true,   // Launcher element-pickup fingerprint
  resume_session: true, // Automatic recovery retry
};

// Recovery retries are synth-flagged — the backend writes them outside the normal
// assistant tool_call/result pairing — but they are NOT out-of-band actions: a
// retry belongs to the run it resumed. When that run's activity group is still
// open (no closer reply and no user turn in between) the retry folds into it like
// any other tool call; only a genuinely new run gets its own bubble.
const _RECOVERY_TOOLS = { resume_session: true };

function _isSynthToolRow(msg, idToRole) {
  if (!msg || msg.role !== 'tool') return false;
  // Native Codex history exposes tool items as chronological standalone rows,
  // rather than WebAgent's assistant-tool_call/result pairing. Admit those
  // rows to the same foldable renderer used for other persisted synth tools.
  if (msg.source === 'codex:portal') return true;
  if (!_SYNTH_TOOLS[msg.tool_name]) return false;
  if (msg._synth_tool || msg._vision_synth) return true;   // backend tag (new / legacy)
  // Fallback for legacy payloads with no backend tag: VISION rows are parented to
  // the user turn. Memory rows must NOT use this — a SKIPPED memory_search is also
  // user-parented but is deliberately left untagged so it doesn't render an empty
  // bubble; the tag is the only signal that admits a memory row.
  const visionOnly = msg.tool_name === 'process_image' || msg.tool_name === 'route_attachment';
  return (visionOnly && idToRole) ? (idToRole[msg.parent_id] === 'user') : false;
}

// Build one tool-call panel entry straight from a persisted synthetic tool row.
// The args (image prompt / search query) live in the row's metadata, which is
// never slimmed, so the panel shows args + result on reload with no lazy fetch.
function _buildSynthCall(msg) {
  let meta = {};
  try { if (msg.metadata) meta = JSON.parse(msg.metadata) || {}; } catch (_) {}
  return {
    tool: msg.tool_name,
    args: (meta.args && typeof meta.args === 'object') ? meta.args : {},
    status: meta.error ? 'error' : 'done',
    result: null,
    durationMs: (typeof meta.duration_ms === 'number') ? meta.duration_ms : null,
    errorType: null,
    turn: 0,
    open: false,
    _savedToolOutput: msg.output || null,
    _savedToolMetadata: msg.metadata || null,
    _detailMsgId: msg.id || null,
    _detailToolId: msg.id || null,
    _detailIdx: 0,
    _needsDetail: true,
  };
}

// Parse compaction detail only after the activity projection selected its range.
function _compactionMetadata(msg) {
  if (!msg || (msg.source || '') !== 'system:compaction') return null;
  let metadata = msg.metadata || {};
  if (typeof metadata === 'string') {
    try { metadata = JSON.parse(metadata); } catch (_) { return null; }
  }
  const compact = metadata && metadata.compaction;
  return compact && compact.status === 'success' ? compact : null;
}

// The transcript receives only direct durable render kinds plus synthetic activity summaries.
// It deliberately does not infer visibility from metadata, phase, role, or
// output shape; storage made that decision.
function _renderKindForReplay(msg) {
  // chat_render_kind is the complete, authoritative display contract.
  // Missing labels are a producer fault and stay safely folded as activity.
  return String(msg?.chat_render_kind || 'activity').toLowerCase();
}

function _buildV2Renderables(messages) {
  const renderables = [];
  for (const msg of messages || []) {
    if (!msg) continue;
    if (msg._activity_summary === true) {
      const startSeq = Number(msg.activity_start_seq);
      const owner = String(msg.owner_turn_id || '');
      renderables.push({
        _activityGroup: true,
        _toolGroup: true,
        id: msg.activity_group_id || msg.id,
        ownerTurnId: owner,
        entries: [{ kind: 'activity-summary', id: msg.id, interactionSeq: msg.session_seq }],
        calls: [],
        created_at: msg.created_at,
        session_seq: msg.session_seq,
        firstInteractionId: msg.first_interaction_id || msg.id,
        deferEntries: true,
        summaryCounts: msg.summary_counts || {},
        loadEntries: async () => {
          const token = localStorage.getItem('auth_token');
          let url = apiPath(`/api/v1/db/session-activity-group-detail?db=user.db&session_id=${encodeURIComponent(app.currentSessionId || msg.session_id || '')}&activity_group_id=${encodeURIComponent(msg.activity_group_id || msg.id)}`);
          if (token) url += `&token=${encodeURIComponent(token)}`;
          const res = await fetch(url, { cache: 'no-store', headers: authHeaders() });
          if (!res.ok) throw new Error('Activity detail request failed (' + res.status + ')');
          const data = await res.json();
          if (data.restricted) throw new Error('Activity detail is restricted');
          return Array.isArray(data.entries) ? data.entries : [];
        },
      });
      continue;
    }
    const renderKind = _renderKindForReplay(msg);
    if (['user', 'promoted_answer', 'notice', 'error'].includes(renderKind)) {
      // Preserve the durable lane when replaying direct rows. In particular a
      // notice is a mid-run boundary, not ordinary agent prose: if its class is
      // lost here, addChatBubble merges it into the following terminal answer
      // and the activity summaries are left together above both messages.
      // A promoted answer is the terminal bubble and must likewise stay out of
      // the normal prose merge lane.
      const direct = {
        ...msg,
        _midRunOpening: renderKind === 'notice',
        _openingOwner: msg.turn_id || '',
        _agentCloser: renderKind === 'promoted_answer',
        _closerOwner: msg.turn_id || '',
        _closerKey: '',
      };
      renderables.push(direct);
    }
  }
  return renderables;
}

// Every transcript uses the single durable render contract.
function _buildRenderables(messages, toolResultsByParent, light, turnNumbers) {
  return _buildV2Renderables(messages);
}

// Append a batch of newer messages to the bottom of the live transcript (the
// scroll-down / forward-pagination path). Renders real bubbles + grouped
// tool-only bubbles + reattaches tool-call panels, mirroring the initial pass.
function _appendMessagesToTranscript(messages, light, announceCompactions = false) {
  const container = app.chatMessages;
  if (!container || !messages.length) return;
  if (announceCompactions && typeof app.notifyCompactionCompleted === 'function') {
    for (const raw of messages) {
      const compact = _compactionMetadata(raw);
      if (!compact) continue;
      // A racing DB-tail reconcile from older code may already have painted the
      // durable detail as a generic info bubble. The structured projection owns
      // successful compactions, so remove that incompatible twin before dedup.
      if (raw.id) {
        const key = CSS.escape(String(raw.id));
        container.querySelector(
          '.chat-bubble.info[data-msg-id="' + key + '"]',
        )?.remove();
      }
      app.notifyCompactionCompleted({
        type: 'compaction_completed',
        session_id: app.currentSessionId,
        interaction_id: raw.id,
        detail: raw.content || '',
        skip_durable: true,
        ...compact,
      });
    }
  }
  const toolResultsByParent = _buildToolResultsByParent(messages);
  // The batch may be a partial tail of an exchange; count turn numbers across
  // the full cached transcript so the badges continue, not restart.
  const cached = app.currentSessionId ? _messageCache.get(app.currentSessionId) : null;
  const turnNumbers = _computeTurnNumbers(
    cached && cached.messages && cached.messages.length ? cached.messages : messages,
  );
  const renderables = _buildRenderables(messages, toolResultsByParent, light, turnNumbers);
  for (const msg of renderables) {
    // Reconcile, forward pagination, and an outbox acknowledgement can all race
    // to present the same persisted row. Treat the interaction UUID as the DOM
    // primary key so every append path is idempotent.
    if (msg.id && !msg._activityGroup) {
      const key = CSS.escape(String(msg.id));
      const existing = container.querySelector(
        `[data-msg-id="${key}"], [data-turn-id="${key}"]`,
      );
      if (existing) continue;
    }
    if (msg._activityGroup) {
      msg.entries = (msg.entries || []).filter(entry => {
        if (!entry.id) return true;
        return !Array.from(container.querySelectorAll('[data-activity-entry-id]'))
          .some(row => String(row.dataset.activityEntryId || '') === String(entry.id));
      });
      if (!msg.entries.length && !(msg.calls || []).length) continue;
    }
    if (msg._memoryNote) {
      if (typeof app._appendMemoryNote === 'function') app._appendMemoryNote(msg.message);
      continue;
    }
    if (msg._toolGroup) { _emitToolGroupBubble(msg); continue; }
    const el = _emitRealBubble(msg);
    if (el && el.nodeType === 1) {
      _addBubbleActions(el);
      // A durable mid-run opening is a transcript BOUNDARY: stamp the shared mark
      // so the ordering authority (chat-bubble.js `sortTranscriptCanonical`) pins
      // the run's NEXT activity phase directly below this bubble, instead of
      // dragging it back up to the prompt and stranding the opening at the tail.
      if (msg._midRunOpening) markMidRunOpening(el, msg._openingOwner || '');
    }
    // Keep the incremental path explicit: its source contract verifies that
    // the exact run key is handed to canonical closer promotion.
    if (el && el.nodeType === 1 && msg._agentCloser
        && typeof app._promoteRunCloserBubble === 'function') {
      app._promoteRunCloserBubble(el, {
        owner: msg._closerOwner || msg.turn_id || '',
        asstId: msg.id || '',
        createdAt: msg.created_at,
        runKey: msg._closerKey || '',
        emptyCloser: msg._emptyCloser === true,
      });
    }
  }
  _attachToolCallsFromMessages(messages, light, turnNumbers);
  // Canonical placement above the anchor moves a focused turn down the page;
  // re-assert the landing for this stage.
  if (_landing) _reLandActive();
}

app._appendMessagesToTranscript = _appendMessagesToTranscript;
// Prepend through the same renderer as initial/forward loads so grouped
// tool-only turns cannot diverge. Render at the tail, collect the new nodes,
// then move the ordered block before the previous first child.
function _prependMessagesToTranscript(messages, light) {
  const container = app.chatMessages;
  if (!container || !messages.length) return;
  // Historical rendering must not inherit the live/current assistant merge
  // pointer. Otherwise the first older final on a page can be appended as a
  // section of the newest final already on screen.
  app._agentTurnBubble = null;
  _appendMessagesToTranscript(messages, light);
  // _setBubbleSessionSeq performs canonical placement while each node renders.
  // Do not infer node identity from its post-sort array position.
}

function _reprojectCachedTranscript(sessionId, cache, preservePrependViewport) {
  const container = app.chatMessages;
  if (!container || !cache) return;
  const scroller = app._chatScroller || container.parentElement;
  const oldHeight = scroller ? scroller.scrollHeight : 0;
  const oldTop = scroller ? scroller.scrollTop : 0;
  // `oldTop` only means "the reader's position" when the window already shows
  // THIS session. On a cross-session reproject — a switch that restored a
  // stashed projection and then refreshed — the scroller still carries the
  // PREVIOUS session's offset, so preserving it both scrolls the reader to a
  // meaningless place and, because the restore is applied AFTER the landing,
  // silently discards the session-open anchor. Treat it as a fresh open instead
  // so the newest user turn lands at the readable top.
  const crossSession = _windowSessionId !== sessionId;
  cache.messages = _sortMessagesCanonical(cache.messages || []);
  _renderSessionWindowed(
    cache.messages, sessionId, null, crossSession, false, cache.light !== false,
  );
  if (preservePrependViewport && scroller && !crossSession) {
    // A settle landing owns the viewport for its brief window: the render above
    // already re-landed the anchored row by id, so re-deriving a scrollTop from
    // the pre-render offset would discard it (this is the 60→104 oscillation
    // seen in the writer log). Keep the landing instead.
    if (_landing && _landing.sessionId === sessionId && performance.now() < _landing.deadline) {
      _reLandActive();
    } else {
      scroller.scrollTop = oldTop + Math.max(0, scroller.scrollHeight - oldHeight);
    }
  }
}

// Land the newest user turn at the readable top of the CURRENT projection, via
// chat-ui's viewport-policy seam so the write is marked programmatic and cannot
// re-arm tail-follow (see app.setTranscriptScrollTop). Used when a session
// switch restores a stashed DOM projection instead of re-rendering: the scroller
// still carries the previous session's offset, so without this the restored chat
// opens wherever the last one happened to be scrolled.
// Returns false when the newest user turn is not materialized (virtual scroll
// recycled it to a placeholder), so the caller can fall back to an anchored
// windowed render.
function _landNewestUserTurnInProjection() {
  const container = app.chatMessages;
  const scroller = app._chatScroller || (container && container.parentElement);
  if (!container || !scroller) return false;
  const sessionId = app.currentSessionId;
  // Which turn is newest comes from the durable canonical order, not from a class
  // scan: a recycled user row is a `chat-bubble-placeholder` (no `chat-bubble` and
  // no `user`), so the old selector silently answered with an OLDER user turn and
  // then reported success — the restored projection anchored one turn too high,
  // with the agent reply above the newest user message at the readable top.
  let newestId = null;
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  if (cached && Array.isArray(cached.messages)) {
    for (let i = cached.messages.length - 1; i >= 0; i--) {
      const m = cached.messages[i];
      if (m && m.role === 'user' && m.id) { newestId = m.id; break; }
    }
  }
  if (!newestId) {
    const users = container.querySelectorAll('.chat-bubble.user[data-msg-id]');
    const newest = users[users.length - 1];
    if (!newest) return false;
    newestId = newest.getAttribute('data-msg-id');
  }
  // The turn has to be in THIS projection — as a bubble or as the placeholder
  // standing in for it. Absent means the stashed window was taken around a
  // different part of the transcript, and only the anchored windowed render can
  // put the turn on screen.
  if (!_findAnchorRow(container, newestId)) return false;
  const pad = app._USER_STEP_PAD || 62;
  // Register a durable TURN FOCUS rather than a one-shot write. The restored
  // projection keeps receiving rows (older batches page in, virtual scroll
  // swaps placeholders back to bubbles), and each of those stages could
  // otherwise drift the turn down the page while the reader is still reading it.
  if (sessionId && typeof _setTurnFocus === 'function') {
    _setTurnFocus(sessionId, newestId);
    // The focus is only real if it survived its own session guard — on this path
    // no render has updated the projection identity yet. If it did not take, the
    // anchored windowed render is the correct fallback, so say so rather than
    // letting the caller keep a projection that was never landed.
    if (typeof app._hasTurnFocus !== 'function' || app._hasTurnFocus(sessionId)) {
      return true;
    }
  }
  const row = _findAnchorRow(container, newestId);
  if (!row) return false;
  const target = Math.max(0,
    scroller.scrollTop + row.getBoundingClientRect().top
      - scroller.getBoundingClientRect().top - pad);
  if (typeof app.setTranscriptScrollTop === 'function') {
    app.setTranscriptScrollTop(target);
  } else {
    container.scrollTop = target;
    scroller.scrollTop = target;
  }
  return true;
}

// ── Background refresh of a cached (hybrid) transcript ──────────────────────
// When the storage adapter serves the IndexedDB transcript instantly but flags
// it stale (refresh_pending), we re-sync the tail from the server in the
// background: the mock progress note ("Assessing…") ticks in the FOOTER activity
// element until the fresh window lands, then the new rows are merged in
// (canonically placed) and the note is retired. Offline, the refresh fails
// silently and the note is retired anyway — the cached messages stay on screen
// and the reconcile loop keeps trying to catch up later.
let _updateSkeletonEl = null;   // the standalone tail notice (unavailable only)
let _syncRetryTimer = null;
let _syncRetryDelay = 1000;

function _showUpdateSkeleton() {
  _setSyncTailState('checking');
}

function _setSyncTailState(state) {
  _removeUpdateSkeleton();
  const container = app.chatMessages;
  if (!container || state === 'idle') return;
  if (state === 'unavailable') {
    // A real connection failure is not progress — it keeps the flat, non-ticking
    // tail notice instead of the mock progress note.
    const wrap = document.createElement('div');
    wrap.className = 'chat-sync-tail chat-sync-unavailable';
    wrap.dataset.syncTail = state;
    wrap.setAttribute('role', 'status');
    wrap.textContent = 'Server connection is currently unavailable.';
    container.appendChild(wrap);
    _updateSkeletonEl = wrap;
    return;
  }
  // Mock progress note ("Assessing…") for this cached re-sync. It renders in the
  // FOOTER activity element — the same configured `activity` control the run
  // notes tick through (chat-activity.js → app.chatActivityMockNote) — and NOT
  // as a transcript row of its own: activity notes are not configured to render
  // in the chat (chat_ui.json → mirror_activity_in_transcript), so this mock
  // note obeys the same rule instead of forcing a row. It ticks alongside the
  // note that the live run stream would show, so there is exactly one activity
  // surface. A real run owns the bar, so this no-ops while one is streaming.
  if (typeof app.chatActivityMockNote === 'function') {
    app.chatActivityMockNote('assessing', { sessionId: app.currentSessionId });
  }
}

function _removeUpdateSkeleton() {
  if (_updateSkeletonEl && _updateSkeletonEl.parentNode) {
    _updateSkeletonEl.parentNode.removeChild(_updateSkeletonEl);
  }
  _updateSkeletonEl = null;
  // Hand the footer activity element back (no-op when no mock note is showing).
  if (typeof app.chatActivityMockClear === 'function') {
    app.chatActivityMockClear();
  }
}

function _projectionScope(messages, data = null) {
  let firstSeq = null;
  let lastSeq = null;
  for (const msg of messages || []) {
    const seq = Number(msg && msg.session_seq);
    if (!Number.isFinite(seq)) continue;
    if (firstSeq == null || seq < firstSeq) firstSeq = seq;
    if (lastSeq == null || seq > lastSeq) lastSeq = seq;
  }
  return {
    rowCount: Array.isArray(messages) ? messages.length : 0,
    firstSeq,
    lastSeq,
    complete: data?.cache_projection?.complete === true
      || (data?.has_more === false && data?.has_newer === false),
  };
}

function _canAdoptValidatedProjection(cache, data) {
  if (!cache || !data?.cache_projection || !Array.isArray(data.messages)) return false;
  // Preserve a volatile live assistant/open activity phase until it becomes
  // durable. Stable cached history may replace only a strict row superset.
  if ((cache.messages || []).some(msg => msg && (msg.status === 'streaming' || msg._streaming))) return false;
  const current = cache.messages || [];
  if (data.messages.length <= current.length) return false;
  const candidateIds = new Set(data.messages.map(msg => msg && msg.id).filter(Boolean));
  return current.every(msg => !msg?.id || candidateIds.has(msg.id));
}

async function _refreshCachedTranscript(sessionId) {
  try {
    if (sessionId !== app.currentSessionId
        || sessionId !== app._lastLoadedSessionId) return;
    const data = storageAdapter.isHybrid
      ? await storageAdapter.revalidateTranscript(sessionId)
      : await _fetchMessages(sessionId, _OPEN_NEWEST_LIMIT, {
        light: true, refresh: true, completeTurnBoundary: true,
      });
    if (sessionId !== app.currentSessionId) return; // user switched away mid-fetch
    if (data.not_modified === true) {
      const cache = _messageCache.get(sessionId);
      if (_canAdoptValidatedProjection(cache, data)) {
        _applyAuthoritativeCachedRefresh(sessionId, data.messages, data.manifest);
      }
      _syncRetryDelay = 1000;
      return;
    }
    if (data.restricted || !Array.isArray(data.messages)) return;
    if (storageAdapter.isHybrid) {
      _applyAuthoritativeCachedRefresh(sessionId, data.messages, data.manifest);
      _syncRetryDelay = 1000;
      return;
    }
    // Re-apply the owning-user boundary so the merged tail never looks orphaned.
    let extended = data;
    try {
      extended = await _extendTailToOwningUser(sessionId, data, true);
    } catch (_) { /* best-effort — merge the window as-is */ }
    if (sessionId !== app.currentSessionId) return;
    _mergeCachedRefresh(sessionId, extended.messages || [], !!extended.has_more);
  } catch (e) {
    console.warn('Background transcript refresh failed (cached messages kept):', e);
    const unavailable = e?.status == null || Number(e.status) >= 500;
    if (sessionId === app.currentSessionId && unavailable) {
      _setSyncTailState('unavailable');
      if (_syncRetryTimer) clearTimeout(_syncRetryTimer);
      const delay = _syncRetryDelay;
      _syncRetryDelay = Math.min(30000, _syncRetryDelay * 2);
      _syncRetryTimer = setTimeout(() => {
        _syncRetryTimer = null;
        if (sessionId !== app.currentSessionId) return;
        _showUpdateSkeleton();
        _refreshCachedTranscript(sessionId);
      }, delay);
    }
  } finally {
    if (sessionId === app.currentSessionId
        && (!_updateSkeletonEl || _updateSkeletonEl.dataset.syncTail !== 'unavailable')) {
      _removeUpdateSkeleton();
    }
  }
}

function _applyAuthoritativeCachedRefresh(sessionId, messages, manifest) {
  const cache = _messageCache.get(sessionId);
  if (!cache) return;
  const scroller = app._chatScroller || app.chatMessages?.parentElement;
  const nearBottom = !!scroller
    && (scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight) < 60;
  cache.messages = _sortMessagesCanonical([...(messages || [])]);
  cache.hasMore = false;
  cache.hasNewer = false;
  cache.light = true;
  cache.manifest = manifest || cache.manifest || null;
  cache.maxSeq = _maxRenderedSeq(cache.messages);
  cache.loadedAt = Date.now();
  cache.projection = _projectionScope(cache.messages, { cache_projection: { complete: true } });
  if (manifest) _rememberSessionManifest(sessionId, manifest);
  _reprojectCachedTranscript(sessionId, cache, !nearBottom);
  if (nearBottom && scroller) scroller.scrollTop = scroller.scrollHeight;
  _armSessionWatch(sessionId, cache.messages, cache.maxSeq);
  if (typeof app.seedReconcileSig === 'function') app.seedReconcileSig(sessionId, cache.messages);
  if (typeof app.setContextFromMessages === 'function') app.setContextFromMessages(cache.messages);
}

function _mergeCachedRefresh(sessionId, incoming, hasMore) {
  const cache = _messageCache.get(sessionId);
  if (!cache) return;
  const byId = new Map(cache.messages.map((m, i) => [m && m.id, i]).filter(([id]) => id));
  const fresh = [];
  let replaced = false;
  for (const msg of incoming) {
    if (!msg || !msg.id) continue;
    const idx = byId.get(msg.id);
    if (idx == null) {
      byId.set(msg.id, cache.messages.length);
      cache.messages.push(msg);
      fresh.push(msg);
      continue;
    }
    const current = cache.messages[idx];
    // Cached streaming rows are commonly shorter than the authoritative row.
    // Replace existing projections too; only merging unseen ids permanently
    // stranded truncated bodies in both the panel and collapsed preview.
    if (JSON.stringify(current) !== JSON.stringify(msg)) {
      cache.messages[idx] = msg;
      replaced = true;
    }
  }
  if (!fresh.length && !replaced) return;
  cache.messages = _sortMessagesCanonical(cache.messages);
  cache.maxSeq = Math.max(
    cache.maxSeq || 0,
    ...incoming.map(m => Number((m && m.session_seq) || 0)),
  );
  if (hasMore) cache.hasMore = true;
  cache.loadedAt = Date.now();
  // Canonical placement: older rows (boundary back-fill) land above the current
  // window, newer rows below — never appended blindly at the tail.
  // Any authoritative delta can complete a formerly partial persisted segment
  // or add a closer boundary. Reproject the cached window atomically so folded
  // counts, previews, summaries and duplicate-id suppression all share one
  // canonical projection.
  _reprojectCachedTranscript(sessionId, cache, true);
  if (typeof app.seedReconcileSig === 'function') {
    app.seedReconcileSig(sessionId, cache.messages);
  }
  // Advance the reconcile cursor past the merged rows so the poll never
  // re-renders them (it continues from here for genuinely NEW rows).
  if (app.lastInteractionSeq) app.lastInteractionSeq[sessionId] = cache.maxSeq;
  if (app.lastSessionSeq) {
    app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, cache.maxSeq);
  }
  if (typeof app.setContextFromMessages === 'function') {
    app.setContextFromMessages(cache.messages);
  }
}

// Fetch a window of session messages. `opts`: { beforeId, afterId, aroundId,
// light }. Light defaults ON for the chat (headings only; bodies load on
// expand). beforeId/afterId page older/newer; aroundId opens centred on a saved
// position.
async function _fetchMessages(sessionId, limit, opts) {
  opts = opts || {};
  if (typeof sessionId === 'string' && sessionId.startsWith('codex:')) {
    const userId = app.currentUserId;
    const agentId = app.currentAgentId;
    if (!userId || !agentId) throw new Error('Codex Portal session is missing its user or agent context.');
    let url = apiPath(
      `/api/v1/engines/codex/portal/threads/${encodeURIComponent(sessionId)}/messages`
      + `?user_id=${encodeURIComponent(userId)}&agent_id=${encodeURIComponent(agentId)}`,
    );
    if (opts.refresh) url += `&_refresh=${Date.now()}`;
    const res = await fetch(url, {
      headers: authHeaders(),
      ...(opts.refresh ? { cache: 'no-store' } : {}),
    });
    if (!res.ok) throw new Error('Codex Portal transcript HTTP ' + res.status);
    return await res.json();
  }
  // Hybrid + browser + normal all route through the adapter, which
  // handles IndexedDB caching transparently based on the active mode.
  if (storageAdapter.isBrowser || storageAdapter.isHybrid) {
    return storageAdapter.getInteractions(sessionId, limit, opts);
  }
  const token = localStorage.getItem('auth_token');
  let url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sessionId)}&limit=${limit}`);
  if (opts.light !== false) url += '&light=1';
  if (opts.compactActivity !== false) url += '&compact_activity=1';
  if (opts.beforeId) url += `&before_id=${encodeURIComponent(opts.beforeId)}`;
  if (opts.afterId) url += `&after_id=${encodeURIComponent(opts.afterId)}`;
  if (opts.aroundId) url += `&around_id=${encodeURIComponent(opts.aroundId)}`;
  if (opts.atStart) url += '&at_start=1';
  if (opts.nearestUserBeforeId) url += `&nearest_user_before_id=${encodeURIComponent(opts.nearestUserBeforeId)}`;
  if (opts.nearestUserAfterId) url += `&nearest_user_after_id=${encodeURIComponent(opts.nearestUserAfterId)}`;
  if (opts.maxCompleteTurnRows) url += `&max_complete_turn_rows=${encodeURIComponent(opts.maxCompleteTurnRows)}`;
  if (opts.completeTurnBoundary) url += '&complete_turn_boundary=true';
  if (token) url += `&token=${encodeURIComponent(token)}`;
  // A user-requested refresh must bypass the browser/HTTP/service-worker cache,
  // not merely the in-memory transcript cache. The nonce also protects older
  // service workers whose route cache ignored Request.cache.
  if (opts.refresh) url += `&_refresh=${Date.now()}`;
  const res = await fetch(url, { cache: 'no-store', headers: authHeaders() });
  if (res.status === 403 || res.status === 404) return { restricted: true };
  return await res.json();
}

// A synthetic transcript row is a server-side PROJECTION of other rows — the
// compact-activity summary (`activity-summary:<child id|seq>`) or a
// builder-made closer substitute. It owns no interaction row, so it can never be
// a paging cursor: the server cannot resolve the id and answers such a request
// with the NEWEST window, which wedges load-older permanently (the oldest cached
// row never changes, `has_more` stays true, and paging never advances). The
// server resolves a synthetic cursor defensively (see db_viewer._cursor_for);
// the client must still never DEPEND on that, because a cursor is a claim about a
// durable row.
const _SYNTHETIC_ID_PREFIXES = [
  'activity-summary:', 'activity-', 'empty-closer-', 'terminal-closer-',
  'status-', 'synth-',
];
function _isSyntheticMessageId(id) {
  const value = String(id == null ? '' : id);
  if (!value) return true;
  return _SYNTHETIC_ID_PREFIXES.some(prefix => value.startsWith(prefix));
}
// Oldest/newest DURABLE id in a message list — the only ids a paging cursor may
// use. Edge rows are skipped rather than wedging the direction, so one synthetic
// summary at the top of a cached window cannot freeze scroll-back.
function _durableCursorId(messages, edge) {
  const list = Array.isArray(messages) ? messages : [];
  if (edge === 'newest') {
    for (let i = list.length - 1; i >= 0; i -= 1) {
      const id = list[i] && list[i].id;
      if (id && !_isSyntheticMessageId(id)) return id;
    }
    return null;
  }
  for (const msg of list) {
    const id = msg && msg.id;
    if (id && !_isSyntheticMessageId(id)) return id;
  }
  return null;
}

function _hasLeadingUserBoundary(messages) {
  const first = Array.isArray(messages) ? messages.find(Boolean) : null;
  return !!(first && first.role === 'user');
}

async function _extendTailToOwningUser(sessionId, data, light) {
  if (!data || !Array.isArray(data.messages) || !data.messages.length) return data;
  let messages = _sortMessagesCanonical([...data.messages]);
  const firstOwned = messages.find(msg => msg && msg.turn_id);
  const ownerTurnId = firstOwned && firstOwned.turn_id;
  // Older transcripts can lack turn_id on their leading tool/assistant rows.
  // In that case fetch back to a user boundary instead of returning an orphaned
  // “1 tool” disclosure above the first visible prompt.
  const ownsBoundary = () => ownerTurnId
    ? messages.some(msg => msg && msg.role === 'user'
      && (String(msg.id) === String(ownerTurnId)
        || String(msg.turn_id || '') === String(ownerTurnId)))
    : _hasLeadingUserBoundary(messages);
  const seen = new Set(messages.map(msg => msg && msg.id).filter(Boolean));
  let hasMore = !!data.has_more;
  // A tool-heavy turn can exceed one page. Bound the expansion to prevent a
  // malformed legacy turn from turning session open into an unbounded fetch.
  for (let page = 0; hasMore && !ownsBoundary() && page < 8; page += 1) {
    const oldestId = _durableCursorId(messages, 'oldest');
    if (!oldestId) break;
    const older = await _fetchMessages(
      sessionId, _OLDER_BATCH, { beforeId: oldestId, light },
    );
    const incoming = (older.messages || []).filter(msg => msg.id && !seen.has(msg.id));
    if (!incoming.length) {
      hasMore = false;
      break;
    }
    incoming.forEach(msg => seen.add(msg.id));
    messages = _sortMessagesCanonical([...incoming, ...messages]);
    hasMore = !!older.has_more;
  }
  data.messages = messages;
  data.has_more = hasMore;
  return data;
}

async function _maybeLoadMoreOnScrollTop(sessionId) {
  if (!sessionId) return;
  if (_loadingMoreMessages) return;
  // A landing in progress reads the scroller at scrollTop 0 for a frame after
  // the window is rebuilt (the write is clamped until layout measures the new
  // content). Without this guard that zero looks like "the reader is at the very
  // top", the older batch pages in, and its reproject recomputes the anchor —
  // the landing this function is meant to be independent of. Paging up is a
  // reader action; never let it start from a projection that is still settling.
  if (_landing && _landing.sessionId === sessionId && performance.now() < _landing.deadline) return;
  const container = app.chatMessages;
  if (!container) return;
  const cache = _messageCache.get(sessionId);
  if (!cache || !cache.hasMore) return;
  const scroller = app._chatScroller || container.parentElement;
  if (scroller.scrollTop > 150) return;

  _loadingMoreMessages = true;
  try {
    const oldestId = _durableCursorId(cache.messages, 'oldest');
    if (!oldestId) { cache.hasMore = false; return; }
    const data = await _fetchMessages(sessionId, _OLDER_BATCH, { beforeId: oldestId, light: true });
    if (!data.messages || data.messages.length === 0) {
      cache.hasMore = false;
      return;
    }
    const seen = new Set(cache.messages.map(m => m.id));
    const incoming = data.messages.filter(m => m.id && !seen.has(m.id));
    cache.hasMore = !!data.has_more;
    if (incoming.length === 0) return;
    cache.messages = _sortMessagesCanonical([...incoming, ...cache.messages]);
    _reprojectCachedTranscript(sessionId, cache, true);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
  } catch (e) {
    console.warn('Failed to load earlier messages:', e);
  } finally {
    _loadingMoreMessages = false;
  }
}

// Forward pagination: when a session was opened CENTRED on a saved position
// (so the newest messages weren't downloaded), scrolling to the bottom pages
// the newer rows in until caught up with the tail. The live reconcile cursor is
// already seeded at the true latest seq, so this never double-renders with it.
async function _maybeLoadMoreOnScrollBottom(sessionId) {
  if (!sessionId) return;
  if (_loadingNewerMessages) return;
  const container = app.chatMessages;
  if (!container) return;
  const cache = _messageCache.get(sessionId);
  if (!cache || !cache.hasNewer) return;
  const scroller = app._chatScroller || container.parentElement;
  const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  if (distanceFromBottom > 150) return;

  _loadingNewerMessages = true;
  try {
    const newestId = _durableCursorId(cache.messages, 'newest');
    if (!newestId) { cache.hasNewer = false; return; }
    const data = await _fetchMessages(sessionId, _NEWER_BATCH, { afterId: newestId, light: true });
    const seen = new Set(cache.messages.map(m => m.id));
    const incoming = (data.messages || []).filter(m => m.id && !seen.has(m.id));
    cache.hasNewer = !!data.has_newer;
    if (incoming.length === 0) return;
    cache.messages = _sortMessagesCanonical([...cache.messages, ...incoming]);
    _reprojectCachedTranscript(sessionId, cache, false);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
  } catch (e) {
    console.warn('Failed to load newer messages:', e);
  } finally {
    _loadingNewerMessages = false;
  }
}

// ── Jump-nav repositioning ─────────────────────────────────────────────────
// Reposition the transcript window to a DB-truthful target: the true session
// start (`{atStart:true}`) or a specific anchor message (`{anchorId}`),
// fetched from the DB when it isn't already in the cache. Reuses
// _renderSessionWindowed's anchor machinery and re-arms the reconcile cursor
// at the session's true tail so the mid-history gap is never backfilled.
let _repositionInFlight = false;
async function _repositionTranscript(sessionId, target) {
  if (!sessionId || !target || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;

  _repositionInFlight = true;
  try {
    const cached = _messageCache.get(sessionId);
    // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
    // Removal: delete this comment and the following try block; keep application logic.
    try {
      window.__recorderTrace?.('cache.memory.read', {
        session_id: sessionId,
        hit: !!cached,
        age_ms: cached?.loadedAt?Date.now()-cached.loadedAt:null,
      });
    } catch (_) {}

    // Fast path — the target is already materialised in the cache (or the whole
    // transcript is loaded for a jump-to-start): re-render without a fetch.
    if (cached && cached.messages.length) {
      const inCache = target.atStart
        ? !cached.hasMore
        : cached.messages.some(m => m.id === target.anchorId);
      if (inCache) {
        _renderSessionWindowed(
          cached.messages, sessionId, null, false, false, cached.light !== false, target,
        );
        _installVirtualScroll();
        _armSessionWatch(
          sessionId,
          cached.messages,
          cached.hasNewer ? (cached.authorityMaxSeq || cached.maxSeq) : cached.maxSeq,
        );
        container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
        return true;
      }
    }

    // Target is outside the loaded window — fetch a window around it.
    let data;
    if (target.atStart) {
      data = await _fetchMessages(sessionId, _OLDER_BATCH, { atStart: true, light: true });
    } else {
      data = await _fetchMessages(sessionId, _OPEN_AROUND_RADIUS, { aroundId: target.anchorId, light: true });
    }
    if (!data || !Array.isArray(data.messages) || !data.messages.length) return false;

    const msgs = _sortMessagesCanonical(data.messages);
    _messageCache.set(sessionId, {
      messages: msgs,
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: data.light !== false,
      maxSeq: _maxRenderedSeq(msgs),
      authorityMaxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
      contextModel: data.context_model || '',
      usage: data.usage || null,
      manifest: data.manifest || null,
      loadedAt: Date.now(),
      projection: _projectionScope(msgs, data),
    });
    if (typeof app.setContextFromMessages === 'function') app.setContextFromMessages(msgs);
    _renderSessionWindowed(msgs, sessionId, data.run || null, false, false, data.light !== false, target);
    _installVirtualScroll();
    _armSessionWatch(sessionId, msgs, data.max_session_seq);
    if (typeof app.seedReconcileSig === 'function') app.seedReconcileSig(sessionId, msgs);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
    return true;
  } catch (e) {
    console.warn('Failed to reposition transcript:', e);
    return false;
  } finally {
    _repositionInFlight = false;
  }
}

// Single-chevron jump: find the most recent USER message strictly before the
// message currently at the top of the viewport — against the DB, not just the
// loaded window. The DOM-only fast path (_prevUserTarget) lives in chat-ui.js;
// this is the cache-scan + DB-probe fallback. Returns true when a jump happened.
async function _stepToPrevUserMessage(sessionId) {
  if (!sessionId || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;
  const cached = _messageCache.get(sessionId);
  if (!cached || !cached.messages.length) return false;

  // Anchor = the message at the top of the viewport (first child whose bottom
  // edge is below the scroll line), mirroring _captureSessionFocus.
  const scroller = app._chatScroller || container.parentElement;
  const st = scroller ? scroller.scrollTop : 0;
  let anchorId = null;
  for (const el of Array.from(container.children)) {
    if (!el.classList) continue;
    const isBubble = el.classList.contains('chat-bubble');
    const isPlaceholder = el.classList.contains('chat-bubble-placeholder');
    if (!isBubble && !isPlaceholder) continue;
    const id = el.getAttribute('data-msg-id');
    if (!id) continue;
    const top = el.offsetTop;
    const h = isPlaceholder ? (parseInt(el.style.height, 10) || 0) : el.offsetHeight;
    if (top + h > st + 4) { anchorId = id; break; }
  }
  if (!anchorId) anchorId = _durableCursorId(cached.messages, 'oldest');

  const anchorIdx = cached.messages.findIndex(m => m.id === anchorId);
  const scanEnd = anchorIdx === -1 ? cached.messages.length : anchorIdx;

  // Cache scan: most recent user message strictly before the anchor.
  for (let i = scanEnd - 1; i >= 0; i--) {
    const m = cached.messages[i];
    if (m && m.role === 'user') {
      return app._repositionTranscript(sessionId, { anchorId: m.id });
    }
  }

  // Cache exhausted — ask the DB for the nearest user row before the oldest
  // loaded message, then centre a window on it.
  if (!cached.hasMore) return false;
  try {
    const oldestId = _durableCursorId(cached.messages, 'oldest');
    if (!oldestId) return false;
    const probe = await _fetchMessages(sessionId, 1, { nearestUserBeforeId: oldestId, light: true });
    const userMsg = (probe && probe.messages || []).find(m => m && m.role === 'user');
    if (!userMsg || !userMsg.id) return false;
    return app._repositionTranscript(sessionId, { anchorId: userMsg.id });
  } catch (e) {
    console.warn('Failed to find previous user message:', e);
    return false;
  }
}

// Header Down jump: find the nearest USER message strictly after the viewport
// anchor. The DOM fast path lives in chat-ui.js; this cache/DB fallback covers
// an unloaded next turn without changing the existing double-chevron behavior.
async function _stepToNextUserMessage(sessionId) {
  if (!sessionId || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;
  const cached = _messageCache.get(sessionId);
  if (!cached || !cached.messages.length) return false;
  const scroller = app._chatScroller || container.parentElement;
  const st = scroller ? scroller.scrollTop : 0;
  let anchorId = null;
  for (const el of Array.from(container.children)) {
    if (!el.classList) continue;
    const isBubble = el.classList.contains('chat-bubble');
    const isPlaceholder = el.classList.contains('chat-bubble-placeholder');
    if (!isBubble && !isPlaceholder) continue;
    const id = el.getAttribute('data-msg-id');
    if (!id) continue;
    const top = el.offsetTop;
    const h = isPlaceholder ? (parseInt(el.style.height, 10) || 0) : el.offsetHeight;
    if (top + h > st + 4) { anchorId = id; break; }
  }
  if (!anchorId) anchorId = cached.messages[cached.messages.length - 1].id;
  const anchorIdx = cached.messages.findIndex(m => m.id === anchorId);
  const scanStart = anchorIdx === -1 ? 0 : anchorIdx + 1;
  for (let i = scanStart; i < cached.messages.length; i++) {
    const m = cached.messages[i];
    if (m && m.role === 'user') return app._repositionTranscript(sessionId, { anchorId: m.id });
  }
  if (!cached.hasNewer) return false;
  try {
    const newestId = cached.messages[cached.messages.length - 1].id;
    const probe = await _fetchMessages(sessionId, 1, { nearestUserAfterId: newestId, light: true });
    const userMsg = (probe && probe.messages || []).find(m => m && m.role === 'user');
    if (!userMsg || !userMsg.id) return false;
    return app._repositionTranscript(sessionId, { anchorId: userMsg.id });
  } catch (e) {
    console.warn('Failed to find next user message:', e);
    return false;
  }
}

// Load the true latest transcript window when the current projection is mid-history.
// The newest slice is fetched when needed; otherwise the loaded tail is simply scrolled
// into view.
async function _repositionToLatest(sessionId) {
  if (!sessionId || _repositionInFlight) return false;
  const container = app.chatMessages;
  if (!container) return false;
  const scroller = app._chatScroller || container.parentElement;

  _repositionInFlight = true;
  try {
    const cached = _messageCache.get(sessionId);
    // Fast path — we already hold the true tail (no newer rows beyond the
    // window): just scroll the loaded transcript to its bottom.
    if (cached && cached.messages.length && !cached.hasNewer) {
      if (scroller) scroller.scrollTop = scroller.scrollHeight;
      return true;
    }
    // The session was opened centred mid-history (or the cache is empty) —
    // fetch the true newest window from the DB and render at the bottom.
    let data = await _fetchMessages(sessionId, _OPEN_NEWEST_LIMIT, {
      light: true, completeTurnBoundary: true,
    });
    if (!data || !Array.isArray(data.messages) || !data.messages.length) return false;
    // Same coherence pass as a normal bottom-open: don't present an orphaned
    // tail that begins halfway through a tool-heavy turn.
    if (data.turn_boundary_complete !== true) {
      data = await _extendTailToOwningUser(sessionId, data, true);
    }
    const msgs = _sortMessagesCanonical(data.messages || []);
    _messageCache.set(sessionId, {
      messages: msgs,
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: data.light !== false,
      maxSeq: _maxRenderedSeq(msgs),
      authorityMaxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
      contextModel: data.context_model || '',
      usage: data.usage || null,
      manifest: data.manifest || null,
      loadedAt: Date.now(),
    });
    if (typeof app.setContextFromMessages === 'function') app.setContextFromMessages(msgs);
    // No anchor override + no focus → the window renders at the bottom.
    _renderSessionWindowed(msgs, sessionId, data.run || null, false, false, data.light !== false);
    // Rendering replaces the transcript projection but deliberately preserves
    // the scroller's prior offset. This path is an explicit user jump to the
    // true tail, so commit the requested bottom anchor after the new DOM exists.
    if (scroller) scroller.scrollTop = scroller.scrollHeight;
    _installVirtualScroll();
    _armSessionWatch(sessionId, msgs, data.max_session_seq);
    if (typeof app.seedReconcileSig === 'function') app.seedReconcileSig(sessionId, msgs);
    container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));
    return true;
  } catch (e) {
    console.warn('Failed to reposition to bottom:', e);
    return false;
  } finally {
    _repositionInFlight = false;
  }
}

function _adjustScrollForPrepend(container, oldScrollHeight) {
  const scroller = app._chatScroller || (container && container.parentElement);
  if (!scroller) return;
  const newScrollHeight = scroller.scrollHeight;
  const delta = newScrollHeight - oldScrollHeight;
  scroller.scrollTop += delta;
}

function _createBubble(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
  const bubble = document.createElement('div');
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (turnId) bubble.setAttribute('data-turn-id', turnId);
  if (msgId) bubble.setAttribute('data-msg-id', msgId);
  _setBubbleCreatedAt(bubble, createdAt);
  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    bubble.appendChild(label);
  }
  // Mirror addChatBubble: normal agent lanes keep a label; Closer output is
  // deliberately content-only with no development sender heading.
  if (role === 'agent' && extraClass !== 'notice' && extraClass !== 'session-placeholder'
      && extraClass !== 'tool-only' && extraClass !== 'summary-bubble') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'Agent';
    bubble.appendChild(label);
  }
  bubble.appendChild(document.createTextNode(text || ''));
  requestAnimationFrame(() => _storeBubbleHeight(bubble));
  return bubble;
}

// Re-render a reloaded user turn's pasted images/files into its bubble. The
// live bubble gets these from the send flow (chat-send.js); a cold reload has
// only the stored row, so /session-messages now ships a resolved `attachments`
// array we render here with the same component the live path uses.
function _appendUserAttachments(bubble, msg) {
  if (!bubble || bubble.nodeType !== 1) return;
  const atts = msg && msg.attachments;
  if (!Array.isArray(atts) || atts.length === 0) return;
  for (const att of atts) {
    const el = renderAttachmentElement(att, { thumbnail: true });
    if (el) bubble.appendChild(el);
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

// Promote every durable terminal reply through one path. Windowed cold loads and
// incremental pagination must use the same owner/run key so canonical ordering
// can collapse retry duplicates into the run's single closer.
function _promotePersistedCloser(msg, el) {
  if (!el || el.nodeType !== 1 || !msg || !msg._agentCloser
      || typeof app._promoteRunCloserBubble !== 'function') return;
  app._promoteRunCloserBubble(el, {
    owner: msg._closerOwner || msg.turn_id || '',
    asstId: msg.id || '',
    createdAt: msg.created_at,
    runKey: msg._closerKey || '',
    emptyCloser: msg._emptyCloser === true,
  });
}

function _emitRealBubble(msg) {
  if (msg.role === 'system') {
    const src = msg.source || '';
    // Output Closer recap: its own content-only bubble after the folded working
    // response. The summary-bubble class prevents merging and keeps full
    // Markdown rendering without a development sender heading.
    if (isSummaryRow(msg)) {
      if (typeof app.renderSummary === 'function') {
        const el = app.renderSummary(msg.content || '', msg.id, msg.created_at, msg.parent_id || '');
        if (el) _setBubbleSessionSeq(el, msg.session_seq);
        return el;
      }
      return app.addChatBubble('agent', msg.content || '', 'terminal-closer', undefined, undefined, msg.id, msg.created_at);
    }
    const compact = _compactionMetadata(msg);
    if (compact) {
      // Compaction metadata measures local history, not the provider's next
      // prompt. Keep the footer on its last exact provider measurement.
      const alreadyRendered = app.chatMessages && Array.from(
        app.chatMessages.querySelectorAll('.ca-compaction-row'),
      ).some(row => String(row.dataset.activityEntryId || '') === String(msg.id || ''));
      if (alreadyRendered) return null;
      if (typeof app.attachActivityEntries !== 'function') return null;
      const owner = compact.owner_turn_id || msg.turn_id || '';
      app.attachActivityEntries([{
        kind: 'system', notice: 'compaction', id: msg.id,
        label: 'Compaction details', content: msg.content || '',
        createdAt: msg.created_at,
        interactionSeq: msg.interaction_seq != null ? msg.interaction_seq : msg.session_seq,
      }], null, {
        id: msg.id,
        createdAt: msg.created_at,
        turnId: owner,
        interactionSeq: msg.interaction_seq != null ? msg.interaction_seq : msg.session_seq,
      }, owner);
      return null;
    }
    if (src === 'system:mode') {
      // Durable twin of the live ⚠ mode notice: rendered from the DB on cold
      // load (and via the IndexedDB cache). While a live ca-mode-row is still
      // on screen, don't paint a duplicate bubble — the live notice already
      // announced it. The wording is now generic, so PRESENCE of the row (never
      // its text) is what dedupes.
      const live = !!document.querySelector('.ca-mode-row');
      if (!live) {
        // Compatibility fallback for a per-row cold-load append: keep the
        // current visible selection rather than accumulating stale mode
        // bubbles. The shared activity path applies the same projection.
        const prior = Array.from(document.querySelectorAll('.chat-bubble.info.system-mode')).pop();
        if (prior) {
          prior.textContent = MODE_NOTICE_LABEL;
          prior.dataset.msgId = String(msg.id || '');
          _setBubbleSessionSeq(prior, msg.session_seq);
          return prior;
        }
        const el = app.addChatBubble('info', MODE_NOTICE_LABEL, 'system-mode', undefined, undefined, msg.id, msg.created_at);
        _setBubbleSessionSeq(el, msg.session_seq);
        return el;
      }
      return null;
    }
    if (src === 'system:model') {
      // Compatibility fallback for a per-row cold-load append — the grouped path
      // above folds model rows into their run's disclosure, exactly like the
      // mode notice. The wording is always generic: a legacy row's content may
      // hold a provider model ID, which must never be shown. Presence of the
      // live row (not its text) is what dedupes, since every row reads the same.
      if (!document.querySelector('.ca-model-row')) {
        const prior = Array.from(document.querySelectorAll('.chat-bubble.info.system-model')).pop();
        if (prior) {
          prior.textContent = MODEL_NOTICE_LABEL;
          prior.dataset.msgId = String(msg.id || '');
          _setBubbleSessionSeq(prior, msg.session_seq);
          return prior;
        }
        const el = app.addChatBubble('info', MODEL_NOTICE_LABEL, 'system-model', undefined, undefined, msg.id, msg.created_at);
        _setBubbleSessionSeq(el, msg.session_seq);
        return el;
      }
      return null;
    }
    const extraClass = src.startsWith('system:debug:') ? 'system-debug'
      : src === 'system:usage_limit' ? 'system-usage-limit'
      : src === 'system:error' ? 'system-error'
      : src === 'system:retry' ? 'system-retry'
      : undefined;
    const el = app.addChatBubble('info', msg.content || '', extraClass, undefined, undefined, msg.id, msg.created_at);
    _setBubbleSessionSeq(el, msg.session_seq);
    if (src === "system:usage_limit") app.renderUsageLimitNotice?.(el, msg.metadata);
    return el;
  }
  if (msg.role === 'user') {
    // ── GenUI-originated page sends (field/button prompts) ──
    // The row is a real user turn (the agent received the raw prompt), but the
    // chat panel shows the page's friendly label as a green notice instead of a
    // "You" bubble. The raw prompt only surfaces under the debug toggle.
    const _genuiLabel = _genuiLabelFromMeta(msg.metadata);
    if (_genuiLabel) {
      const _g = app.addChatBubble('info', _genuiLabel, 'system-genui', undefined, undefined, msg.id, msg.created_at);
      if (isDebugMode() && (msg.content || '').trim()) {
        app.addChatBubble('info', 'Raw prompt:\n' + msg.content, 'system-debug', undefined, undefined, undefined, msg.created_at);
      }
      _setBubbleSessionSeq(_g, msg.session_seq);
      return _g;
    }
    const _ub = app.addChatBubble('user', msg.content, undefined, undefined, undefined, msg.id, msg.created_at);
    let _meta = {};
    try {
      _meta = msg.metadata ? JSON.parse(msg.metadata) : {};
      if (_meta?.cmid) _restorePersistenceStatus(_ub, _meta.cmid);
    } catch (_) { /* legacy or malformed metadata has no persistence receipt */ }
    _appendUserAttachments(_ub, msg);
    _setBubbleSessionSeq(_ub, msg.session_seq);
    // Gate-queued restore: a user row persisted as status='queued' (durable
    // DB marker) or annotated with live gate queue info (in-memory backup)
    // must come back as the queued bubble with its Force run button —
    // identical to the live WS path — so navigating away and back, or a full
    // reload, preserves the queue UI instead of showing a normal message.
    if ((msg.status === 'queued' || msg.queue_position != null)
        && typeof app.restoreGateQueueBubble === 'function') {
      app.restoreGateQueueBubble(_ub, {
        turnId: msg.id,
        queuePosition: (msg.queue_position != null) ? msg.queue_position : 1,
        text: msg.content || '',
      });
    }
    // Compaction-queued restore: a durable queued_for='compact' row means a
    // /compact was folding this session when the message was sent. Re-lock the
    // composer + show "Compacting…" so the user can't stack more messages —
    // the server drains the queue when the compaction finishes (the drain's
    // 'running' broadcast or the compact_done event unlocks).
    if (msg.status === 'queued' && _meta?.queued_for === 'compact'
        && typeof app._lockComposerForCompaction === 'function') {
      try { app._lockComposerForCompaction(); } catch (_) { /* ignore */ }
    }
    return _ub;
  }
  let text = msg.content || '';
  text = _stripToolCalls(text);
  const hasText = !!text.trim();
  let el;
  if (msg.status === 'streaming') {
    el = (typeof app.seedStreamingBubble === 'function')
      ? app.seedStreamingBubble(
        msg.id, text, msg.created_at, msg.turn_id,
        msg.interaction_seq ?? msg.session_seq,
      )
      : app.addChatBubble('agent', text || '', 'streaming', undefined, msg.id, msg.id, msg.created_at);
  } else if (!hasText && !msg._emptyCloser) {
    return null;
  } else if (msg._emptyCloser) {
    // Empty/no-output completion: the run still needs its one terminal closer.
    // Create the agent bubble now with a deterministic placeholder; the
    // _promoteRunCloserBubble step immediately fills it with the last non-empty
    // working message from this run's group (or a failure label), so the reload
    // reproduces the live empty-completion closer exactly.
    el = app.addChatBubble('agent', text || ' ', undefined, undefined, msg.id, msg.id, msg.created_at);
  } else if (msg.status === 'interrupted') {
    el = app.addChatBubble('agent', text + '\n\n(interrupted)', 'interrupted', undefined, msg.id, msg.id, msg.created_at);
  } else if (msg.status === 'error') {
    el = app.addChatBubble('agent', text, 'error', undefined, msg.id, msg.id, msg.created_at);
  } else {
    const laneClass = msg._midRunOpening ? 'notice'
      : (msg._agentCloser ? 'terminal-closer' : undefined);
    el = app.addChatBubble('agent', text, laneClass, undefined,
      msg.id, msg.id, msg.created_at);
  }
  // Stamp the per-turn model tag before the action row is built by the caller.
  if (el && el.nodeType === 1 && msg.metadata) _setBubbleModelFromMeta(el, msg.metadata);
  if (el && msg.status === 'deleted') {
    el.classList.add('deleted');
    if (typeof app._injectDeletedActions === 'function') {
      app._injectDeletedActions(el, msg.id);
    }
  }
  _setBubbleSessionSeq(el, msg.session_seq);
  return el;
}

// Render tool calls as a section INSIDE the preceding agent bubble,
// merging consecutive tool-only turns into the same turn bubble.
function _settleCompletedRunGroup(group, bubble, owner) {
  // A run is authoritative-Complete when its turn group carries a folded final
  // 'response' entry (the final answer). That run must end active controls and
  // render exactly one separate closer after its activity group — so a
  // completed run is never left as "the group's preview" alone, and Stop can
  // never outlive the turn it belongs to.
  const entries = (group && group.entries) || [];
  const finished = entries.filter(e =>
    e && e.kind === 'response' && e._finalized === true && (e.content || '').trim(),
  );
  if (!finished.length) return;
  const last = finished[finished.length - 1];
  const closerGroup = (bubble && bubble.isConnected) ? bubble : null;
  if (typeof app._renderCompletionCloser === 'function') {
    try {
      app._renderCompletionCloser(
        last.content || '',
        last.id || (group.firstInteractionId || ''),
        owner || '',
        last.createdAt || group.created_at,
        closerGroup,
      );
    } catch (_) { /* reconcile heals later */ }
  }
  if (typeof app.endRunControls === 'function') {
    try { app.endRunControls(owner || ''); } catch (_) { /* best-effort */ }
  }
}

function _emitToolGroupBubble(group) {
  if (group._activityGroup && typeof app.attachActivityEntries === 'function') {
    const owner = group.ownerTurnId || '';
    const bubble = app.attachActivityEntries(
      group.entries || group.calls || [],
      null,
      {
        id: group.firstInteractionId,
        activityGroupId: group.id,
        createdAt: group.created_at,
        turnId: owner,
        interactionSeq: group.session_seq == null ? null : Number(group.session_seq),
        runKey: group.runKey || '',
        deferEntries: group.deferEntries === true,
        summaryCounts: group.summaryCounts || null,
        loadEntries: group.loadEntries || null,
      },
      owner,
    );
    if (group.activityGlimmer && bubble) {
      bubble.classList.add('live-activity');
    }
    _settleCompletedRunGroup(group, bubble, owner);
    return bubble;
  }
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  let targetBubble = bubbles[bubbles.length - 1];

  // If no preceding bubble exists, or the last bubble is a user bubble or
  // a deleted/error bubble, create a fresh agent bubble for the tool group.
  if (!targetBubble || targetBubble.classList.contains('user')
      || targetBubble.classList.contains('error')
      || targetBubble.classList.contains('deleted')
      || targetBubble.classList.contains('interrupted')) {
    targetBubble = app.addChatBubble('agent', '', 'tool-only', undefined, group.id, undefined, group.created_at);
  }

  if (targetBubble && targetBubble.nodeType === 1 && typeof app.attachToolCallsToLastBubble === 'function') {
    app.attachToolCallsToLastBubble(group.calls, targetBubble);
  }
  return targetBubble;
}

// Expose activity-group rehydration for virtual-scroll. It owns no message
// cache row, so using the group id as a normal message id would leave an empty
// placeholder when the user scrolls back to it.
app._rehydrateVirtualActivityGroup = function(groupId, placeholder) {
  const group = _virtualActivityGroups.get(groupId);
  if (!group || !placeholder || !placeholder.parentNode) return null;
  const parent = placeholder.parentNode;
  const bubble = _emitToolGroupBubble(group);
  if (!bubble || bubble.nodeType !== 1) return null;
  // attachActivityEntries appends the canonical group; move that same node into
  // the placeholder's chronological slot instead of leaving a duplicate at tail.
  parent.replaceChild(bubble, placeholder);
  return bubble;
};

// Expose for virtual-scroll
app._maybeLoadMoreOnScrollTop = _maybeLoadMoreOnScrollTop;
app._maybeLoadMoreOnScrollBottom = _maybeLoadMoreOnScrollBottom;
// Expose the DB-truthful jump-nav helpers for chat-ui.js (single/double chevrons).
app._repositionTranscript = _repositionTranscript;
app._stepToPrevUserMessage = _stepToPrevUserMessage;
app._stepToNextUserMessage = _stepToNextUserMessage;
app._repositionToLatest = _repositionToLatest;
app._hasOlderMessages = function(sessionId) {
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  return !!(cached && cached.hasMore);
};
app._hasNewerMessages = function(sessionId) {
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  return !!(cached && cached.hasNewer);
};
/** Sync predicate: is the jump target already materialised in the cache? Used
 *  by chat-ui.js to show the busy spinner only when a reposition will actually
 *  hit the DB. Targets: {atStart:true}, {atBottom:true}, or {anchorId}. */
app._jumpTargetInCache = function(sessionId, target) {
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  if (!cached || !cached.messages.length) return false;
  if (target && target.atStart) return !cached.hasMore;
  if (target && target.atBottom) return !cached.hasNewer;
  if (target && target.anchorId) return cached.messages.some(m => m.id === target.anchorId);
  return false;
};

/**
 * Hard-boundary trigger: when the user scrolls to the absolute top or bottom
 * of the messages container, force-load older/newer messages directly from
 * the API. Fully self-contained — bypasses all proximity gates, caching
 * checks, and state flags in _maybeLoadMoreOnScrollTop/_maybeLoadMoreOnScrollBottom.
 * Each direction has its own loading guard to prevent concurrent fetches.
 */
let _hbtLoadingOlder = false;
let _hbtLoadingNewer = false;
app._checkHardScrollBoundary = async function(sessionId) {
  if (!sessionId) return;
  const container = app.chatMessages;
  if (!container) return;
  const scroller = app._chatScroller || container.parentElement;
  const st = scroller.scrollTop;
  const sh = scroller.scrollHeight;
  const ch = scroller.clientHeight;
  const cache = _messageCache.get(sessionId);
  if (!cache) return;

  const atTop = st <= 0;
  // Don't fire the hard-boundary while the rubber band is actively pulling
  // — the pull-to-refresh gesture handles loading instead.
  const rbActive = typeof app._rbPullActive !== 'undefined' && app._rbPullActive;
  const atBottom = (sh - st - ch) <= 10;

  // ── Top boundary ──
  if (atTop && cache.hasMore && !rbActive && !_hbtLoadingOlder && !_loadingMoreMessages) {
    _hbtLoadingOlder = true;
    try {
      const oldestId = _durableCursorId(cache.messages, 'oldest');
      if (!oldestId) { cache.hasMore = false; return; }
      cache.hasMore = false;
      const data = await _fetchMessages(sessionId, 60, { beforeId: oldestId, light: true });
      const msgs = data && data.messages;
      if (!msgs || msgs.length === 0) return;
      const seen = new Set(cache.messages.map(m => m.id));
      const incoming = msgs.filter(m => m.id && !seen.has(m.id));
      cache.hasMore = !!data.has_more;
      if (incoming.length === 0) return;
      cache.messages = [...incoming, ...cache.messages];
      const scroller2 = app._chatScroller || container.parentElement;
      const oldScrollHeight = scroller2.scrollHeight;
      _prependMessagesToTranscript(incoming, true);
      _adjustScrollForPrepend(container, oldScrollHeight);
      container.querySelectorAll('.chat-bubble').forEach(b => {
        if (typeof _storeBubbleHeight === 'function') _storeBubbleHeight(b);
      });
    } catch (e) {
      console.warn('Hard-boundary load (older) failed:', e);
    } finally {
      _hbtLoadingOlder = false;
    }
    return;
  }

  // ── Bottom boundary ──
  if (atBottom && cache.hasNewer && !_hbtLoadingNewer && !_loadingNewerMessages) {
    _hbtLoadingNewer = true;
    try {
      const newestId = _durableCursorId(cache.messages, 'newest');
      if (!newestId) { cache.hasNewer = false; return; }
      cache.hasNewer = false;
      const data = await _fetchMessages(sessionId, 40, { afterId: newestId, light: true });
      const msgs = data && data.messages;
      if (!msgs || msgs.length === 0) return;
      const seen = new Set(cache.messages.map(m => m.id));
      const incoming = msgs.filter(m => m.id && !seen.has(m.id));
      cache.hasNewer = !!data.has_newer;
      if (incoming.length === 0) return;
      cache.messages = [...cache.messages, ...incoming];
      if (typeof _appendMessagesToTranscript === 'function') {
        _appendMessagesToTranscript(incoming, true);
      } else {
        for (const msg of incoming) {
          if (msg.role === 'user') {
            const bubble = _createBubble('user', msg.content, null, null, null, msg.id, msg.created_at);
            container.appendChild(bubble);
          } else if (msg.role === 'assistant') {
            let text = msg.content || '';
            text = _stripToolCalls(text);
            if (!text.trim()) continue;
            const bubble = _createBubble('agent', text, null, null, msg.id, null, msg.created_at);
            container.appendChild(bubble);
          }
        }
      }
      container.querySelectorAll('.chat-bubble').forEach(b => {
        if (typeof _storeBubbleHeight === 'function') _storeBubbleHeight(b);
      });
    } catch (e) {
      console.warn('Hard-boundary load (newer) failed:', e);
    } finally {
      _hbtLoadingNewer = false;
    }
  }
};

// The admin-only Agent/Session diagnostic line no longer renders inside the
// transcript. It now lives in the chat-header "more" (\u22EF) menu \u2014 built + wired in
// ui/chat/js/session-init.js (_wireHeaderMoreMenu), gated to admins
// via the `body.is-admin` class. Removed from here so it can't drift off the top
// of the conversation when older messages page in.

// The session the current windowed projection belongs to. A same-session
// refresh must never re-derive the tail-follow lock from a default `atBottom`
// — that re-armed the follow against a reader who had scrolled up, and the
// next arriving chunk yanked them back to the bottom.
let _windowSessionId = null;

// ── Durable-id landing (session-open / reproject anchor) ────────────────────
// The window is re-projected wholesale (`container.replaceChildren()`), so the
// row we want at the readable top is only valid until the next layout change:
// virtual scroll swaps placeholders for real bubbles (and back) as the
// projection settles, and every swap changes the height of the rows BELOW it.
// A single scrollTop write therefore leaves the anchored turn wherever those
// deltas happened to push it — measured drift on a lazy-loading session put the
// newest user turn 193px ABOVE the fold with one of its own activity-summary
// rows parked at the top instead.
//
// So remember the anchor's durable message id and re-assert the landing from its
// LIVE rect for a short settle window. Resolve the readable-top pad at each
// re-assert so this path always agrees with chat-ui's current header-aware pad.
// A reader gesture clears it immediately, so a re-assert can never fight the
// person reading the transcript.
let _landing = null; // { sessionId, msgId, fallbackEl, at, deadline, newestUser }
const _LANDING_SETTLE_MS = 700;
// A TURN FOCUS is a landing that owns the viewport for as long as its turn is
// live: a send, or the session-open/switch default (the newest user turn). It
// has to outlive the 700ms layout settle because the projection keeps changing
// underneath it — older rows are placed above the anchor by canonical order,
// virtual scroll swaps placeholders back to real bubbles, and a reconcile
// reproject rebuilds the whole window. Each of those stages re-asserts the
// focus (see app._reLandAnchor callers), so a staged window can no longer push
// the newest user turn below the fold: a cross-agent switch measured the turn
// 8205px below a scrollTop of 56 with the single landing write already spent.
// A reader gesture clears it instantly (chat-ui.js markTranscriptScrollIntent),
// as does a tail render and a session change, so it never fights real browsing.
const _LANDING_FOCUS_MS = 6000;
// Cadence for the bounded focus re-assert loop (see _setTurnFocus). Slow enough
// to be invisible, fast enough that a stage we do not hook cannot leave the turn
// off-screen for more than a frame or two of reading.
const _LANDING_FOCUS_TICK_MS = 300;
let _focusTimer = null;
function _stopFocusTimer() {
  if (_focusTimer) { window.clearInterval(_focusTimer); _focusTimer = null; }
}
const _LANDING_REASSERT_MS = [120, 320, 700];

function _findAnchorRow(container, msgId) {
  if (!container || !msgId) return null;
  // Attribute-equality scan rather than a selector: activity-group ids contain
  // ':' (e.g. `activity-summary:<uuid>`) and this keeps escaping out of the
  // picture entirely.
  //
  // A RECYCLED row matches too. Virtual scroll replaces the bubble with a
  // `chat-bubble-placeholder`, which keeps data-msg-id (and the correct slot plus
  // the stored height) but drops the `chat-bubble`/`user` classes — so a
  // class-restricted scan reports "not found" for a row that is on screen, and
  // every caller then falls back to an OLDER user turn or to the tail.
  for (const el of container.querySelectorAll(
      '.chat-bubble[data-msg-id], .chat-bubble-placeholder[data-msg-id]')) {
    if (el.getAttribute('data-msg-id') === msgId) return el;
  }
  return null;
}

function _reLandActive() {
  const L = _landing;
  if (!L) return;
  // The landing dies when its session leaves the screen. `_windowSessionId` names
  // the last session that went through a full render — and a restored stashed
  // projection deliberately skips that render (see
  // _landNewestUserTurnInProjection), so it can still name the OUTGOING session
  // while the target session's own DOM is what is on screen. Judging the landing
  // dead there discarded a switch focus the instant it was registered: the
  // restored transcript kept the previous session's offset and opened on the
  // agent turn above the newest user message instead of on that turn. Either
  // signal naming the landing's session means the landing is still relevant.
  if (_windowSessionId !== L.sessionId && app.currentSessionId !== L.sessionId) {
    _landing = null; return;
  }
  // A live turn keeps its focus: every projection stage that re-asserts also
  // extends the window while the run is streaming, so a long reply can never
  // outlive the anchor and then get pulled to the tail by the next reproject.
  // An idle session lets the window lapse, so a switch focus cannot pin a
  // reader who has simply been reading for a while.
  if (L.newestUser && app.isProcessing) L.deadline = performance.now() + _LANDING_FOCUS_MS;
  if (performance.now() > L.deadline) { _landing = null; return; }
  const container = app.chatMessages;
  const scroller = app._chatScroller || (container && container.parentElement);
  if (!container || !scroller) return;
  // Durable id is the identity, but an optimistic user bubble is re-stamped with
  // the server's interaction id once the send is acknowledged, so a focus
  // landing falls back to the projection's newest user row — the same turn in
  // canonical order, and still the top of the visible window.
  const el = _findAnchorRow(container, L.msgId)
    || (L.newestUser ? _newestUserRow(container) : null)
    || (L.fallbackEl && L.fallbackEl.isConnected ? L.fallbackEl : null);
  if (!el) return; // recycled to a placeholder — nothing to land on
  const target = Math.max(0,
    scroller.scrollTop + el.getBoundingClientRect().top
      - scroller.getBoundingClientRect().top - (app._USER_STEP_PAD || 62));
  if (typeof app.setTranscriptScrollTop === 'function') app.setTranscriptScrollTop(target);
  else scroller.scrollTop = target;
}

// Land `msgId` at the readable top. `fallbackEl` keeps the plain offsetTop write
// working for any row that carries no durable id.
function _landByMsgId(sessionId, msgId, fallbackEl) {
  const container = app.chatMessages;
  const scroller = app._chatScroller || (container && container.parentElement);
  if (!container || !scroller) return;
  const at = performance.now();
  _landing = { sessionId, msgId: msgId || null, fallbackEl: fallbackEl || null, at, deadline: at + _LANDING_SETTLE_MS };
  _reLandActive();
  for (const delay of _LANDING_REASSERT_MS) {
    window.setTimeout(() => {
      if (_landing && _landing.at === at) _reLandActive();
    }, delay);
  }
}

/** Newest user row in the current projection (bubble or its recycled placeholder). */
function _newestUserRow(container) {
  if (!container) return null;
  // Which turn is newest is owned by the DURABLE canonical order, not by the DOM.
  // A class scan answers with the newest *materialized* row, which is an OLDER
  // user turn whenever virtual scroll has recycled the real one — and landing on
  // that turn parks the reader one turn too high (the agent reply above the
  // newest user message at the readable top). So resolve the newest user id from
  // the cache and take its row in either form; only when that id is absent from
  // the projection at all does the class scan apply.
  const sessionId = app.currentSessionId;
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  if (cached && Array.isArray(cached.messages)) {
    for (let i = cached.messages.length - 1; i >= 0; i--) {
      const m = cached.messages[i];
      if (m && m.role === 'user' && m.id) {
        const row = _findAnchorRow(container, m.id);
        if (row) return row;
        break; // newest user turn exists but is not in this projection
      }
    }
  }
  const users = container.querySelectorAll('.chat-bubble.user');
  return users.length ? users[users.length - 1] : null;
}

// Make the newest user turn the viewport anchor for `sessionId` for as long as
// its turn is live. Used by the session-open/switch default (the newest user
// turn owns the readable top, exactly where a send lands its own message) and
// by the send path in chat-ui.js, which cannot hand over an element reference
// because a reconcile/cache reproject replaces every bubble.
function _setTurnFocus(sessionId, msgId) {
  const container = app.chatMessages;
  const scroller = app._chatScroller || (container && container.parentElement);
  if (!container || !scroller || !sessionId) return;
  const at = performance.now();
  _landing = {
    sessionId,
    msgId: msgId || null,
    fallbackEl: null,
    newestUser: true,
    at,
    deadline: at + _LANDING_FOCUS_MS,
  };
  _reLandActive();
  for (const delay of _LANDING_REASSERT_MS) {
    window.setTimeout(() => {
      if (_landing && _landing.at === at) _reLandActive();
    }, delay);
  }
  // Bounded re-assert cadence. The explicit hooks cover the projection stages
  // this module drives; this covers the ones it does not (a canonical placement
  // driven from chat-bubble.js, a late async batch, a recycled row whose stored
  // height was wrong). It stops the moment the focus ends, and any reader
  // gesture clears the focus outright (chat-ui.js markTranscriptScrollIntent →
  // app._clearLanding), so it can never fight someone reading the transcript.
  _stopFocusTimer();
  _focusTimer = window.setInterval(() => {
    if (!_landing || !_landing.newestUser) { _stopFocusTimer(); return; }
    _reLandActive();
  }, _LANDING_FOCUS_TICK_MS);
}

// Cleared by any real reader gesture (chat-ui.js markTranscriptScrollIntent) so
// a settle re-assert never overrides the reader, and by the tail path below.
app._clearLanding = () => { _landing = null; _stopFocusTimer(); };
// Re-assert seam for the projection stages that mutate the transcript AFTER a
// landing: canonical appends/prepends (rows placed above the anchor push it
// down the page) and virtual-scroll materialisation (placeholder→bubble swaps
// change the heights above it). No-op unless a landing is live.
app._reLandAnchor = _reLandActive;
app._setTurnFocus = _setTurnFocus;
app._hasTurnFocus = sessionId => !!(_landing && _landing.newestUser
  && _landing.sessionId === (sessionId || app.currentSessionId)
  && performance.now() < _landing.deadline);

function _renderSessionWindowed(messages, sessionId, run, useFocus, manageRunState, light, anchorOverride) {
  const container = app.chatMessages;
  if (!container) return;

  // Where the reader actually is, captured before the DOM is replaced so a
  // same-session refresh can preserve their viewport (see the anchor override
  // after the window is computed).
  const _prevScroller = app._chatScroller || container.parentElement;
  const _prevTop = _prevScroller ? _prevScroller.scrollTop : 0;
  const _prevRange = _prevScroller
    ? Math.max(0, _prevScroller.scrollHeight - _prevScroller.clientHeight) : 0;
  const _prevAtTail = _prevRange <= 0 || (_prevRange - _prevTop) <= 120;
  let _prevAnchorId = null;
  if (_prevScroller && !_prevAtTail) {
    for (const el of container.querySelectorAll('.chat-bubble[data-msg-id]')) {
      if (el.offsetTop + el.offsetHeight > _prevTop) {
        _prevAnchorId = el.getAttribute('data-msg-id');
        break;
      }
    }
  }

  // This is a full-window render, not an append. It is also used for cache hits
  // and same-session refreshes, so replace the prior projection every time.
  // Previously those paths appended a second copy whose assistant rows had
  // unstable DOM ids, making virtualization appear to "re-render" messages as
  // the user scrolled through overlapping copies.
  container.replaceChildren();
  _placeholderIds.clear();
  _virtualActivityGroups.clear();

  // A fresh render never continues a live tool-call group.
  app._activeToolGroupBubble = null;
  app._agentTurnBubble = null;

  // Build the render list. Text/user messages become their own bubbles;
  // consecutive tool-only assistant turns are merged into a single grouped
  // tool-call bubble (one "Turn N" section per turn when expanded).
  const toolResultsByParent = _buildToolResultsByParent(messages);
  const renderables = _buildRenderables(messages, toolResultsByParent, light);

  // A durable terminal assistant reply is stronger than a stale
  // lease left behind by a reconnect/reload. Never restore Stop/streaming UI for
  // a turn whose completed reply is already present in the transcript.
  const runTurnId = run && run.turn_id != null ? String(run.turn_id) : '';
  const transcriptSettledRun = !!(runTurnId && messages.some(msg =>
    msg && String(msg.turn_id || '') === runTurnId
      && msg.role === 'assistant' && msg.status !== 'streaming'
      && (msg.content || '').trim(),
  ));
  const runUpdatedAt = run && run.updated_at ? Date.parse(run.updated_at) : NaN;
  // A running lease must be renewed by the supervisor. Do not resurrect a
  // Stop control from an abandoned lease after a reload; active work emits
  // heartbeats well inside this conservative two-minute window.
  const staleRunLease = Number.isFinite(runUpdatedAt) && (Date.now() - runUpdatedAt) > 120000;
  const activeRun = !!(run && run.active) && !transcriptSettledRun && !staleRunLease;
  const focus = useFocus ? _sessionFocus.get(sessionId) : null;

  let anchorIdx = renderables.length - 1;
  let atBottom = true;
  if (anchorOverride) {
    // Explicit jump-nav target (session start or a specific anchor id) wins
    // over any saved focus. Not the bottom — anchor the window around it.
    atBottom = false;
    const fi = anchorOverride.atStart
      ? 0
      : renderables.findIndex(m => m.id === anchorOverride.anchorId);
    anchorIdx = fi === -1 ? renderables.length - 1 : fi;
  } else if (useFocus) {
    // Session-open default: land the newest USER turn at the readable top —
    // the same place a send lands its own message (see chat-ui.js
    // app.anchorUserTurn / _USER_STEP_PAD) — instead of dropping the reader at
    // the tail. Activity and the reply then read downward from that turn.
    //
    // This deliberately runs AHEAD of any saved reader focus. Switching chats is
    // a fresh read, and the stored focus is normally a by-product of the
    // previous visit's own landing: _captureSessionFocus snapshots whichever row
    // sat at the viewport top, which is the `activity-summary:` disclosure just
    // above the turn — so returning parked the reader ~60px below their own
    // message and read as "not anchored at all". When that saved id no longer
    // matched a renderable inside the window it was worse: the previous
    // `else if` chain consumed the branch, matched nothing, and fell straight
    // through to the tail.
    //
    // Only a genuine session switch sets useFocus, so reconciles, cache
    // reprojects and refreshes (all useFocus=false) keep their own anchor.
    // A session with no user turn at all keeps the saved focus, then the tail.
    for (let i = renderables.length - 1; i >= 0; i--) {
      const m = renderables[i];
      if (m && !m._toolGroup && m.role === 'user' && m.id) {
        anchorIdx = i;
        atBottom = false;
        break;
      }
    }
    if (atBottom && !activeRun && focus && focus.atBottom === false && focus.anchorMsgId) {
      const fi = renderables.findIndex(m => m.id === focus.anchorMsgId);
      if (fi !== -1) { anchorIdx = fi; atBottom = false; }
    }
  }
  // A LIVE TURN FOCUS outranks the default tail window. While a sent turn (or a
  // just-opened session's newest turn) owns the viewport, a full projection —
  // reconcile reproject, cache refresh, reconnect — has to rebuild the window
  // around that turn rather than dropping the reader at the bottom of a long
  // reply. The focus lives in _landing (see _setTurnFocus) and a reader gesture,
  // a tail render, or a session change clears it, so it never fights browsing.
  const _focusLanding = (!anchorOverride && _landing && _landing.newestUser
    && _landing.sessionId === sessionId && performance.now() < _landing.deadline)
    ? _landing : null;
  if (_focusLanding) {
    for (let i = renderables.length - 1; i >= 0; i--) {
      const m = renderables[i];
      if (m && !m._toolGroup && m.role === 'user' && m.id) {
        anchorIdx = i;
        atBottom = false;
        break;
      }
    }
  }
  // Follow the newest entry for a tail render, but keep any restored or
  // explicitly navigated historical position stable. This also prevents a
  // released follow state from one session leaking into the next chat.
  //
  // Re-derive the lock ONLY for a different session, or for an explicit
  // jump-nav target. This render also serves cache hits, reconciles and
  // reconnects for the *same* session (see the replaceChildren note above);
  // re-deriving there reset the lock to `atBottom` (default true) on every
  // refresh, so a reader who had scrolled up was re-armed and the next
  // arriving chunk pulled them straight back to the bottom.
  const sameSessionWindow = _windowSessionId === sessionId;
  if ('_scrollLocked' in app && (!sameSessionWindow || anchorOverride)) {
    app._scrollLocked = atBottom;
  }
  _windowSessionId = sessionId;

  // A same-session re-render (refresh, reconnect, cache miss) must not discard
  // the reader's position. The default `atBottom` window rebuilds the tail and
  // the landing below then jumps them to the bottom. Re-anchor the window
  // around the message that was at the top of their viewport instead, so a
  // refresh is invisible to a reader who had scrolled up.
  if (sameSessionWindow && !anchorOverride && !_prevAtTail && _prevAnchorId) {
    const prevIdx = renderables.findIndex(m => m.id === _prevAnchorId);
    if (prevIdx !== -1) { anchorIdx = prevIdx; atBottom = false; }
  }

  let realStart, realEnd;
  const _WINDOW_RADIUS = 20;
  if (atBottom) {
    realEnd = renderables.length - 1;
    realStart = Math.max(0, renderables.length - _WINDOW_RADIUS * 2);
  } else {
    realStart = Math.max(0, anchorIdx - _WINDOW_RADIUS);
    realEnd = Math.min(renderables.length - 1, anchorIdx + _WINDOW_RADIUS);
  }

  let anchorEl = null;
  for (let i = 0; i < renderables.length; i++) {
    const msg = renderables[i];
    if (msg._toolGroup) {
      // A collapsed disclosure is small only in isolation. A long session can
      // have hundreds of them, so it must share the same window as message
      // bubbles. This also preserves the visible order: a group never appears
      // above an owning user bubble that is still virtualized.
      _virtualActivityGroups.set(msg.id, msg);
      if (i >= realStart && i <= realEnd) {
        const el = _emitToolGroupBubble(msg);
        if (i === anchorIdx && el && el.nodeType === 1) anchorEl = el;
      } else {
        _placeholderIds.add(msg.id);
        const placeholder = _makePlaceholder(msg.id, _getBubbleHeight(msg.id));
        _setBubbleSessionSeq(placeholder, msg.session_seq);
        container.appendChild(placeholder);
      }
      continue;
    }
    if (i >= realStart && i <= realEnd) {
      const el = _emitRealBubble(msg);
      if (el && el.nodeType === 1) _addBubbleActions(el);
      _promotePersistedCloser(msg, el);
      if (i === anchorIdx && el && el.nodeType === 1) anchorEl = el;
    } else if (msg.id) {
      _placeholderIds.add(msg.id);
      const placeholder = _makePlaceholder(msg.id, _getBubbleHeight(msg.id));
      _setBubbleSessionSeq(placeholder, msg.session_seq);
      container.appendChild(placeholder);
    } else {
      const el = _emitRealBubble(msg);
      if (el && el.nodeType === 1) _addBubbleActions(el);
    }
  }

  if (manageRunState) {
    if (activeRun) {
      app.isProcessing = true;
      const seeded = renderables.some(m => m.status === 'streaming');
      if (!seeded && run.assistant_interaction_id
          && typeof app.ensureStreamingBubbleForActiveTurn === 'function') {
        app.ensureStreamingBubbleForActiveTurn(run.assistant_interaction_id);
      }
      if (!app.lastSessionSeq) app.lastSessionSeq = {};
      const floor = typeof run.latest_session_seq === 'number' ? run.latest_session_seq : 0;
      app.lastSessionSeq[sessionId] = Math.max(app.lastSessionSeq[sessionId] || 0, floor);
      // Re-light the live "in-process" indicator from the durable snapshot. A
      // recovering run (stopped involuntarily, awaiting the auto-resume sweep)
      // gets the mock "re-assessing…" note instead of the generic one.
      if (typeof app.chatActivityRestore === 'function') {
        try {
          app.chatActivityRestore(run.current_op || null,
            run.recovering ? (app.chatActivityMockLabel?.('reassessing') || 'Re-assessing…') : null);
        } catch (_) {}
      }
    } else {
      app.isProcessing = false;
    }
  }

  // Activity groups already own their persisted tool rows. Attach tool calls to
  // any assistant TEXT bubble that carries them (final/main answers from the
  // cleanup path, and C-promoted substantive mid-turn messages) — reload must
  // show the same collapsed tool section the live stream does.
  _attachToolCallsFromMessages(messages, light);
  const timestampAnomalies = new Set(messages.filter(msg => msg && msg.timestamp_order_anomaly).map(msg => String(msg.id || '')));
  container.querySelectorAll('.chat-bubble[data-msg-id]').forEach(bubble => {
    if (timestampAnomalies.has(String(bubble.getAttribute('data-msg-id') || ''))) {
      bubble.setAttribute('data-timestamp-order-anomaly', 'true');
    }
  });
  if (typeof app._reorderTranscriptCanonical === 'function') {
    app._reorderTranscriptCanonical();
  }

  if (atBottom || !anchorEl) {
    _landing = null; // tail render owns the viewport; drop any settle chain
    const scroller3 = app._chatScroller || container.parentElement;
    scroller3.scrollTop = scroller3.scrollHeight;
  } else {
    // Land the anchored turn at exactly the height a send lands its own
    // message — read from chat-ui.js's single constant, with a literal
    // fallback for the case where chat-ui has not initialised yet.
    //
    // Tracked by durable message id so the landing survives the layout changes
    // that follow this render (placeholder↔bubble swaps above the anchor shift
    // it off the top; see the _landing notes above). Routes through chat-ui's
    // viewport-policy seam so the open gets the same treatment a send gets in
    // land(): tail-follow disarmed, and the write marked programmatic. A raw
    // scrollTop assignment here reads as reader movement to the transcript
    // scroll listener, which re-arms _scrollLocked from the short first-paint
    // geometry; the appends that follow then pull the reader straight to the
    // tail and the session-open anchor is lost.
    // A session open/switch — and any render that is already carrying a live
    // turn focus (a send's reconcile reproject) — records a TURN FOCUS rather
    // than a one-shot landing, so the turn stays at the readable top through
    // every later projection stage. An explicit jump-nav target keeps the
    // one-shot landing: its row is not the newest user turn, so a focus would
    // re-resolve to the wrong message.
    const _anchorMsgId = anchorEl.getAttribute ? anchorEl.getAttribute('data-msg-id') : null;
    if (useFocus || _focusLanding) {
      _setTurnFocus(sessionId, _anchorMsgId);
    } else {
      _landByMsgId(sessionId, _anchorMsgId, anchorEl);
    }
  }

  // Swipe-committed load: fade the freshly rendered transcript in over the
  // phantom-skeleton. Flag set by session-swipe.js _commitSwipe, consumed here.
  if (app._swipeFadeIn) {
    app._swipeFadeIn = false;
    container.classList.remove('chat-messages-fadein');
    void container.offsetWidth; // restart the animation if it ran before
    container.classList.add('chat-messages-fadein');
  }
}

// ── Terminal Chat auto-activation ──
// When the loaded session's agent runs the terminal_chat engine, replace the
// chat bubbles with a live xterm.js terminal the MOMENT the session is selected
// — no message needed. Must run on EVERY load path (empty, cached, restricted,
// fresh-with-messages), so it lives here and is called near the top of
// loadSessionChat, BEFORE the early returns. (A brand-new terminal session has
// zero messages, so the old end-of-function placement was unreachable for it —
// the terminal only appeared after the first sent message lit the engine's WS
// event.) Always tears down a previous session's terminal first, so switching to
// a normal agent restores the chat bubbles.
export function _syncTerminalChat(sessionId) {
  if (typeof app.activateTerminalChat !== 'function'
      || typeof app.deactivateTerminalChat !== 'function') return;

  const agentId = app.currentAgentId;
  // Tear down a previous session's terminal before (maybe) mounting a new one.
  try { app.deactivateTerminalChat(); } catch (_) {}
  if (!agentId || !sessionId) return;

  // Only the terminal_chat engine gets a terminal. '' = the agent isn't in the
  // dropdown cache yet (cold boot) — attempt anyway; the backend ignores the
  // request for non-terminal agents, so a normal agent never mounts one.
  const engine = _agentEngineFor(agentId);
  // Do not probe when this agent is absent from the cache: stale/cold state
  // must not activate terminal chat for an ordinary session.
  if (engine !== 'terminal_chat') return;

  // Defer a tick so the (now-hidden) bubble render settles first.
  setTimeout(() => {
    try { app.activateTerminalChat(sessionId, agentId); } catch (_) {}
  }, 50);
}

export async function loadSessionChat(sessionId, opts = {}) {
  const loadEpoch = ++_sessionLoadEpoch;
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('cache.session.load', {
      session_id: sessionId,
      epoch: loadEpoch,
    });
  } catch (_) {}
  // RECORDER-HOOK: toggle.js registers backend scope before this load.
  // Removal: delete this try block only; epoch guard preserves normal load ownership.
  try { if (window.__recorderScope) await window.__recorderScope(sessionId,app.currentAgentId); } catch (_) {}
  if (loadEpoch !== _sessionLoadEpoch) return;
  // Any session load/switch re-renders the transcript — stop read-aloud from a
  // previous session so its speech never keeps playing. chat-bubble-actions.js
  // listens for this and resets speaker buttons, highlights, and the pause
  // control (see _initTtsComposerStop).
  try { document.dispatchEvent(new CustomEvent('tts:stop')); } catch (_) {}
  const hasPersistedSession = _sessionsCache.some((session) => session.id === sessionId && session.created_at !== null);
  // A client-created session has no server record yet. Do not issue saved-session
  // requests until the first send persists it.
  if (hasPersistedSession && typeof app.loadChatComponents === 'function') {
    try { app.loadChatComponents(sessionId); } catch (_) { /* optional UI */ }
  }
  // Lift any "Session not found" lock from a previously-open deleted session so
  // the composer works again on this live one (no-op when nothing was locked).
  if (typeof app.clearSessionNotFound === 'function') app.clearSessionNotFound();
  // Restore per-session execution mode (Ask/Plan/Auto) when loading any session
  if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
  if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();

  if (hasPersistedSession && typeof app.refreshActiveAbilities === 'function') {
    try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
  }
  if (hasPersistedSession && typeof app.refreshTunnelForSession === 'function') {
    try { app.refreshTunnelForSession(); } catch (_) { /* best-effort */ }
  }

  const isSwitch = sessionId !== app._lastLoadedSessionId;
  // The Sessions table and any future session-listing surface must reflect the
  // chat panel's accepted active session immediately. Guard against background
  // loads: only the session already owned by the chat may move this highlight.
  if (isSwitch && sessionId === app.currentSessionId) {
    notifyActiveSessionChanged(sessionId);
  }
  // Opening a session does NOT acknowledge it by default. A visit is a glance, not
  // a read: the unread flag (and its notification row) must survive a session
  // switch so the user can flip through sessions without silently clearing them.
  // The read state is therefore written ONLY by an explicit acknowledgement — the
  // flag chip's own cycle (ui/shared/js/needs-input-chip.js), a notification row's
  // ✕ / "Clear all" (ui/chat/js/session-notification.js), or the row menu
  // (ui/chat/elements/session-dropdown/controller.js).
  //
  // Set `mark_read_on_visit` to true in data/config/chat_ui.json to restore the
  // older behaviour, where landing on a session recorded the visit as a read.
  // Fire-and-forget: a read-state write must never delay a cached transcript
  // render or force a cache refresh.
  if (chatUiFlag('mark_read_on_visit', false) && isSwitch && sessionId === app.currentSessionId) {
    const visitedAt = new Date().toISOString();
    fetch(apiPath(`/api/v1/db/sessions/${encodeURIComponent(sessionId)}/read?db=user.db&user_id=${encodeURIComponent(app.currentUserId || '')}`), {
      method: 'POST',
      headers: authHeaders(),
    }).then(async (res) => {
      if (!res.ok) return;
      // Use the database-confirmed timestamp rather than the timestamp from
      // before the request, so the local grey/read state stays aligned with the
      // session-list refresh that follows a visit.
      const payload = await res.json().catch(() => ({}));
      window.dispatchEvent(new CustomEvent('session:visited', {
        detail: { sessionId, visitedAt: payload.read_at || visitedAt },
      }));
    }).catch(() => { /* visit-state sync is best-effort */ });
  }
  if (isSwitch) {
    _removeUpdateSkeleton();
    if (_syncRetryTimer) clearTimeout(_syncRetryTimer);
    _syncRetryTimer = null;
    _syncRetryDelay = 1000;
  }
  if (isSwitch && app._lastLoadedSessionId) {
    _captureSessionFocus(app._lastLoadedSessionId);
  }
  // Every session switch must reset live-activity state (note text, tick
  // timer, processing flag, transcript mirror). switchToSession does this
  // itself, but the Sessions admin page, genui and the optimizer stats page
  // set currentSessionId and call loadSessionChat directly — this choke point
  // is the one place ALL paths are covered, so a run in one session can never
  // bleed its progress into another session's transcript.
  if (isSwitch && app._lastLoadedSessionId && typeof app.chatActivitySessionChanged === 'function') {
    try { app.chatActivitySessionChanged(); } catch (_) { /* non-fatal */ }
  }
  // A session switch cancels any pending step-finalize timers so a quiet-period
  // flush can never publish a run's pending progress into a DIFFERENT session's
  // transcript after the user navigates away.
  if (isSwitch && typeof app.cancelAllPendingStepFlushes === 'function') {
    try { app.cancelAllPendingStepFlushes(); } catch (_) { /* non-fatal */ }
  }

  // The on-screen session ALWAYS has priority: flag the load as in-flight and
  // cancel any background neighbour pre-load immediately, so a warm-up fetch can
  // never make the session the user is opening wait behind it.
  app._sessionLoadInFlight = true;
  try { if (typeof app.abortNeighborWarm === 'function') app.abortNeighborWarm(); } catch (_) {}

  // Mount/tear down the live terminal up front so it survives every early return
  // below (empty session, cache hit, restricted) — not just the full-fetch path.
  _syncTerminalChat(sessionId);

  try {
    const cached = _messageCache.get(sessionId);
    if (!opts.refresh && cached) {
      app.timestampOrderRecovery = cached.timestampOrderRecovery === true;
      if (!opts.preserveProjection && sessionId !== app._lastLoadedSessionId) {
        _teardownVirtualScroll();
        app.chatMessages.innerHTML = '';
      }
      app._lastLoadedSessionId = sessionId;

      const oldBtn = document.getElementById(`load-earlier-${sessionId}`);
      if (oldBtn) oldBtn.remove();

      if (cached.messages.length === 0) {
        _installVirtualScroll();
        _armSessionWatch(sessionId, [], cached.maxSeq);
        // An empty transcript can still carry ledger usage (e.g. a turn that
        // errored before any bubble) — reconcile so ctx/cost still surface.
        if (typeof app.fetchSessionCost === 'function') app.fetchSessionCost();
        return;
      }

      if (!opts.preserveProjection) {
        _renderSessionWindowed(cached.messages, sessionId, null, isSwitch, false, cached.light !== false);
      } else if (isSwitch && !_landNewestUserTurnInProjection()) {
        // A restored stashed projection keeps its own DOM — and the scroller
        // keeps the PREVIOUS session's scroll offset — so switching chats opened
        // wherever the last one happened to be scrolled instead of on the newest
        // user turn. Land it in place; when virtual scroll has recycled that turn
        // to a placeholder there is nothing to land on, so fall back to the
        // anchored windowed render.
        _renderSessionWindowed(cached.messages, sessionId, null, true, false, cached.light !== false);
      }

      if (typeof app.setContextFromMessages === 'function') {
        app.setContextFromMessages(cached.messages);
      }
      if (cached.contextTokens && typeof app.setContextTokens === 'function') {
        app.setContextTokens(cached.contextTokens, cached.contextModel || '');
      }
      // Restore the session's cost + token usage from cache too — otherwise a
      // cache-hit load re-shows ctx but leaves the cost chip at $0.
      if (cached.usage && typeof app.setSessionUsage === 'function') {
        app.setSessionUsage(cached.usage);
      }
      // Self-heal against a stale/zeroed load payload: reconcile cost + ctx
      // from the authoritative ledger right after the cached restore.
      if (typeof app.fetchSessionCost === 'function') app.fetchSessionCost();
      // Record the manifest from the cache so a later refresh check is cheap.
      if (cached.manifest) _rememberSessionManifest(sessionId, cached.manifest);

      _installVirtualScroll();
      _armSessionWatch(
        sessionId,
        cached.messages,
        cached.hasNewer ? (cached.authorityMaxSeq || cached.maxSeq) : cached.maxSeq,
      );
      if (typeof app.seedReconcileSig === 'function') {
        app.seedReconcileSig(sessionId, cached.messages);
      }
      if (!storageAdapter.isBrowser) {
        _showUpdateSkeleton();
        _refreshCachedTranscript(sessionId);
      }
      return;
    }

    // Cache miss — fetch from API. Open CENTRED on the saved scroll position
    // (a small window around it) when there is one; otherwise the newest slice.
    const savedFocus = _sessionFocus.get(sessionId);
    const openOpts = {
      light: true,
      refresh: !!opts.refresh,
      completeTurnBoundary: true,
      maxCompleteTurnRows: _OPEN_BOUNDARY_ROW_CAP,
      // IndexedDB already holds compact rows (not expanded tool payloads). Give
      // the selected session's complete local projection to the virtualizer at
      // once; incomplete hybrid caches retain the bounded server fallback.
      fullCompact: storageAdapter.isBrowser || storageAdapter.isHybrid,
    };
    let openLimit = _OPEN_NEWEST_LIMIT;
    if (savedFocus && savedFocus.atBottom === false && savedFocus.anchorMsgId) {
      openOpts.aroundId = savedFocus.anchorMsgId;
      delete openOpts.completeTurnBoundary;
      openLimit = _OPEN_AROUND_RADIUS;
    }
    let data = await _fetchMessages(sessionId, openLimit, openOpts);
    if (loadEpoch !== _sessionLoadEpoch || sessionId !== app.currentSessionId) return;

    // Stale-focus guard: when the saved anchor is far from the session's true
    // tail (e.g. turns arrived AFTER the focus was captured, like during crash
    // recovery), discard the stale position and re-fetch the newest messages.
    if (openOpts.aroundId && data && data.has_newer && data.max_session_seq) {
      const msgs = data.messages || [];
      let maxWindowSeq = 0;
      for (const m of msgs) {
        if (typeof m.session_seq === 'number' && m.session_seq > maxWindowSeq) {
          maxWindowSeq = m.session_seq;
        }
      }
      if (maxWindowSeq > 0 && (data.max_session_seq - maxWindowSeq) > _OPEN_AROUND_RADIUS * 2) {
        // More new messages exist beyond the window than the window itself -
        // the saved scroll position is stale. Discard it and load the tail.
        _sessionFocus.delete(sessionId);
        _persistSessionFocus();
        delete openOpts.aroundId;
        openOpts.completeTurnBoundary = true;
        openLimit = _OPEN_NEWEST_LIMIT;
        data = await _fetchMessages(sessionId, openLimit, openOpts);
        if (loadEpoch !== _sessionLoadEpoch || sessionId !== app.currentSessionId) return;
      }
    }

    if (data.restricted && getActive() && getActive().remember_token) {
      await ensureFreshToken();
      if (loadEpoch !== _sessionLoadEpoch || sessionId !== app.currentSessionId) return;
      data = await _fetchMessages(sessionId, openLimit, openOpts);
      if (loadEpoch !== _sessionLoadEpoch || sessionId !== app.currentSessionId) return;
    }

    // A newest-N row window can begin halfway through a tool-heavy turn. Load
    // backward through that turn's user boundary so refresh never presents an
    // orphaned tail that appears to be an incomplete conversation. Skipped for
    // cache-served loads (cached-hit / cached-fallback): the whole point of
    // those is an instant, offline-capable render, so no network extension is
    // allowed to block it — the background refresh re-applies the boundary.
    const leadingBoundaryMissing = !_hasLeadingUserBoundary(data.messages);
    if (!openOpts.aroundId && !data.restricted && !data.cache_status
        && (leadingBoundaryMissing || (data.turn_boundary_complete !== true
          && data.turn_boundary_truncated !== true))) {
      data = await _extendTailToOwningUser(sessionId, data, true);
      if (loadEpoch !== _sessionLoadEpoch || sessionId !== app.currentSessionId) return;
    }

    // The server records the mode every message runs in, making the SESSION the
    // source of truth. Apply it so the pill matches what the server will do —
    // correct across devices/reloads, not just in the browser that last toggled
    // it. This overrides the localStorage-seeded reloadExecutionMode() above.
    // (setExecutionMode persists to the per-session key, so the cached-load path
    // stays right too.) currentSessionId is the loaded id by now (switchToSession
    // sets it before loadSessionChat), so the key is written for the right session.
    if (!data.restricted && data.execution_mode && typeof app.setExecutionMode === 'function') {
      try { app.setExecutionMode(data.execution_mode); } catch (_) { /* best-effort */ }
    }

    // Remote Control executor is likewise a SESSION property (metadata), so the
    // server is the source of truth across devices — apply it on load, overriding
    // the localStorage seed from reloadTargetDevice() above. Null ⇒ run locally.
    if (!data.restricted && typeof app.setTargetDevice === 'function') {
      const _re = data.remote_executor;
      try {
        if (_re && _re.instance_id) {
          // Session has an explicitly stored executor → it wins (across devices).
          app.setTargetDevice(_re.instance_id, _re.label || '');
        } else {
          // No stored executor on the server. If the user already has a local
          // choice (from localStorage, loaded by reloadTargetDevice above),
          // keep it — don't clear it. Otherwise fall back to the agent's
          // configured default device (applied only if it's online; else stays
          // local). The agent default is non-persisting and online-checked.
          if (!app.targetDevice) {
            if (typeof app.applyAgentDefaultTarget === 'function') {
              app.applyAgentDefaultTarget(sessionId);
            }
          }
        }
      } catch (_) { /* best-effort */ }
    }

    if (loadEpoch !== _sessionLoadEpoch || sessionId !== app.currentSessionId) return;
    if (sessionId !== app._lastLoadedSessionId) {
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
    }
    app._lastLoadedSessionId = sessionId;

    if (data.restricted) {
      app.isNewSession = true;
      app.currentSessionId = randomUUID();
      localStorage.setItem('terminalSessionId', app.currentSessionId);
      replaceRouteParams({ session: app.currentSessionId });
      // This fresh fallback session replaces the restricted one AFTER the earlier
      // reloadExecutionMode() ran for the old id — re-sync the pill for the new id
      // (per-session key, defaults to Ask) so it can't carry a stale mode.
      if (typeof app.reloadExecutionMode === 'function') app.reloadExecutionMode();
      if (typeof app.reloadTargetDevice === 'function') app.reloadTargetDevice();
      _teardownVirtualScroll();
      app.chatMessages.innerHTML = '';
      // The fallback session intentionally starts with an empty transcript.
      if (app.currentUserId && typeof app.populateSessionSelect === 'function') {
        app.populateSessionSelect(app.currentUserId);
      }
      _messageCache.delete(sessionId);
      if (typeof app.setContextFromMessages === 'function') {
        app.setContextFromMessages([]);
      }
      return;
    }

    app.timestampOrderRecovery = data.timestamp_order_recovery === true;
    const msgs = _sortMessagesCanonical(data.messages || [], app.timestampOrderRecovery);
    const isLight = data.light !== false;
    // Record the server's transcript manifest so a later refresh can ask
    // "did anything change?" without re-downloading the transcript.
    _rememberSessionManifest(sessionId, data.manifest);
    _messageCache.set(sessionId, {
      messages: [...msgs],
      hasMore: !!data.has_more,
      hasNewer: !!data.has_newer,
      light: isLight,
      timestampOrderRecovery: app.timestampOrderRecovery,
      maxSeq: _maxRenderedSeq(msgs),
      authorityMaxSeq: data.max_session_seq || 0,
      contextTokens: data.context_tokens || 0,
      contextModel: data.context_model || '',
      usage: data.usage || null,
      manifest: data.manifest || null,
      loadedAt: Date.now(),
      // This is projection coverage, not merely a cache-hit marker. A later
      // manifest-only validation compares actual identities before it adopts
      // a broader durable IndexedDB projection.
      projection: _projectionScope(msgs, data),
    });

    if (typeof app.setContextFromMessages === 'function') {
      app.setContextFromMessages(msgs);
    }
    // Prefer the server's whole-session token estimate — the small open window
    // alone would under-report the ctx indicator.
    if (data.context_tokens && typeof app.setContextTokens === 'function') {
      app.setContextTokens(data.context_tokens, data.context_model || '');
    }
    if (data.usage && typeof app.setSessionUsage === 'function') {
      app.setSessionUsage(data.usage);
    }
    // Reconcile against the authoritative ledger — the load payload can carry
    // zeros (transient ledger failure); this restores cost + ctx within ~1s.
    if (typeof app.fetchSessionCost === 'function') app.fetchSessionCost();

    if (msgs.length === 0) {
      if (typeof app.setContextFromMessages === 'function') {
        app.setContextFromMessages([]);
      }
      _armSessionWatch(sessionId, [], data.max_session_seq);
      return;
    }

    const run = data.run || null;
    _renderSessionWindowed(msgs, sessionId, run, isSwitch, true, isLight);

    if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));

    _installVirtualScroll();
    _armSessionWatch(
      sessionId,
      msgs,
      data.has_newer ? data.max_session_seq : _maxRenderedSeq(msgs),
    );
    if (typeof app.seedReconcileSig === 'function') {
      app.seedReconcileSig(sessionId, msgs);
    }

    // Offline-first cached render: when the IndexedDB transcript was served
    // instantly but is stale, show a mock progress note while the background
    // refresh re-syncs from the server (see _refreshCachedTranscript).
    if (data.cache_status === 'cached-hit' && data.refresh_pending) {
      _showUpdateSkeleton();
      _refreshCachedTranscript(sessionId);
    }

    try { _fetchRelatedSessions(); } catch (_) {}
  } catch (e) {
    console.warn('Failed to load session messages:', e);
  } finally {
    if (loadEpoch === _sessionLoadEpoch) app._sessionLoadInFlight = false;
    // Active session has settled — warm the next/prev pinned neighbours, but only
    // when the browser is idle (the warmer self-gates on isProcessing / load state).
    try { if (typeof app.warmNeighborSessions === 'function') app.warmNeighborSessions(); } catch (_) {}
  }
  // (Terminal Chat is activated up front by _syncTerminalChat near the top of
  // this function, so it survives every early return above.)
}

app.loadSessionChat = loadSessionChat;

// Hard-refresh the transcript area for the CURRENT session: drop the cached
// messages, clear the rendered bubbles, and re-fetch + re-render from the DB.
// Resets ONLY the chat transcript — it never reloads the page. Wired to the
// refresh button beside the agent name in the chat header (chat-side-panel.html
// → session-init.js). Differs from app.reloadCurrentSession (which reuses the
// cache and is a no-op clear on the already-loaded session): this forces a
// clean rebuild from the database.
export async function refreshTranscript(force) {
  const sessionId = app.currentSessionId;
  if (!sessionId) return;
  // Remote-change guard: ask the server whether the transcript actually changed
  // since we last rendered it (manifest_only validation against the stored
  // revision/hash). If it is NOT modified, do NOTHING — no cache drop, no
  // virtual-scroll teardown, no re-render. The user's screen must not update,
  // and the rubber-band pull animation (applyRubberBand) is independent of this
  // function, so it keeps its effect either way. Only an explicit server-side
  // change (or an unknown/unreachable state) proceeds to a real refresh.
  // force=true (an explicit user gesture, e.g. the bottom pull-to-refresh)
  // skips the guard: the user asked for a refresh, so always re-sync from the DB.
  if (!force) {
    try {
      const changed = await _transcriptChangedRemotely(sessionId);
      if (!changed) return;
    } catch (_) { /* helper swallows errors — proceed conservatively */ }
  }
  // Remember where the user was so the reload lands in the same place.
  try { _captureSessionFocus(sessionId); } catch (_) {}
  // Drop the cache so loadSessionChat re-fetches fresh from the DB.
  try { _messageCache.delete(sessionId); } catch (_) {}
  // Tear down the virtual-scroll hooks so the re-render starts clean, but leave
  // the existing bubbles in place — _renderSessionWindowed does a synchronous
  // replaceChildren() right before it populates, so the user sees no white flash.
  _teardownVirtualScroll();
  // Keep _lastLoadedSessionId set so loadSessionChat treats this as a same-session
  // operation and skips its own clear / re-teardown. `refresh: true` also makes
  // the adapter skip the IndexedDB cached transcript — an explicit refresh
  // always re-syncs from the authoritative server response.
  await loadSessionChat(sessionId, { refresh: true });
}

app.refreshTranscript = refreshTranscript;

// Per-message context lookup for the bubble "more" menu: given a message id
// (interaction id), return the cached message object — which carries the
// server's per-message `context_tokens` (the actual provider prompt behind that
// turn, or the summariser prompt for summary/compaction messages). Returns null
// when the id isn't in the current session's cache; the menu then falls back to
// the session-latest context readout.
app.getMessageById = function (id) {
  try {
    const sid = app.currentSessionId;
    const cache = sid ? _messageCache.get(sid) : null;
    if (!cache || !id) return null;
    const needle = String(id);
    return cache.messages.find((m) => m && String(m.id) === needle) || null;
  } catch (_) { return null; }
};

// Full current-session message list (cache order) — lets shared UI (the ⋮
// menu's Model row) walk messages for inheritance, e.g. a user bubble adopts
// the model of the NEXT assistant message that answered it.
app.getSessionMessages = function () {
  try {
    const sid = app.currentSessionId;
    const cache = sid ? _messageCache.get(sid) : null;
    return cache ? (cache.messages || []) : [];
  } catch (_) { return []; }
};

// Click-to-poll for the bubble "more" menu: when the Context or Cost row reads
// n/a, the user can click it to re-ask the server for THAT message's
// per-message usage. The server enriches every transcript row with
// `context_tokens` + `cost_usd` from the usage ledger, so a small refetch
// around this message id either lands the value (late ledger write, stale
// cache, row adopted late) or confirms the row genuinely has none. The cache
// entry is updated in place (or inserted in canonical order) so the open menu
// re-renders on its next live tick. Returns the fresh message row, or null
// when the id can't be found / the fetch fails.
app.refreshMessageUsage = async function (mid) {
  const sid = app.currentSessionId;
  if (!sid || !mid) return null;
  try {
    const data = await _fetchMessages(sid, 40, { aroundId: String(mid), light: true });
    const msgs = (data && Array.isArray(data.messages)) ? data.messages : [];
    const fresh = msgs.find((m) => m && String(m.id) === String(mid)) || null;
    if (!fresh) return null;
    const cache = sid ? _messageCache.get(sid) : null;
    if (cache) {
      const idx = cache.messages.findIndex((m) => m && String(m.id) === String(mid));
      if (idx >= 0) Object.assign(cache.messages[idx], fresh);
      else {
        cache.messages.push(fresh);
        cache.messages = _sortMessagesCanonical(cache.messages);
      }
    }
    return fresh;
  } catch (_) { return null; }
};

// Full-row refetch for the bubble "more" menu's Refresh action: re-fetch THIS
// message's complete saved row (content + status + metadata) from the server
// — same around-id window as refreshMessageUsage but with light=false so the
// bodies come back intact. Updates the cache in place so the open menu's
// Context/Cost/Model rows re-render on their next tick. Returns the fresh
// row, or null when the id can't be found / the fetch fails.
// Pick the assistant row a refresh should re-render from. `id` normally IS
// the assistant interaction id (exact match); but streamed bubbles may carry
// the USER's turn id as their anchor (events stamped turn_id, not asst_id).
// With `byTurn` the anchor is treated as a turn id and the first assistant
// row carrying it is returned — the agent's reply to that turn.
function _pickRefreshTarget(msgs, id, byTurn) {
  if (!msgs || !id) return null;
  const s = String(id);
  for (const m of msgs) {
    if (!m) continue;
    if (byTurn) {
      if (m.role === 'assistant' && String(m.turn_id || '') === s) return m;
    } else if (String(m.id) === s && m.role === 'assistant') {
      return m;
    }
  }
  return null;
}

app.refreshMessageFull = async function (mid, altMid) {
  const sid = app.currentSessionId;
  if (!sid || !mid) return null;
  try {
    const find = (msgs) =>
      _pickRefreshTarget(msgs, mid)
      || (altMid ? _pickRefreshTarget(msgs, altMid) : null)
      || _pickRefreshTarget(msgs, mid, true);
    // Refresh is a repair operation: never accept the already-present IndexedDB
    // or browser/SW cache row as proof that the message is healthy.
    let data = await _fetchMessages(sid, 40, {
      aroundId: String(mid), light: false, refresh: true,
    });
    let msgs = (data && Array.isArray(data.messages)) ? data.messages : [];
    let fresh = find(msgs);
    // A streamed bubble can be anchored by its user turn while the assistant
    // row lies outside that around-id window. Retry around the alternate id.
    if (!fresh && altMid) {
      data = await _fetchMessages(sid, 40, {
        aroundId: String(altMid), light: false, refresh: true,
      });
      msgs = (data && Array.isArray(data.messages)) ? data.messages : [];
      fresh = find(msgs);
    }
    if (!fresh) return null;
    const cache = sid ? _messageCache.get(sid) : null;
    if (cache) {
      const idx = cache.messages.findIndex((m) => m && String(m.id) === String(fresh.id));
      if (idx >= 0) Object.assign(cache.messages[idx], fresh);
      else {
        cache.messages.push(fresh);
        cache.messages = _sortMessagesCanonical(cache.messages);
      }
    }
    return fresh;
  } catch (_) { return null; }
};

// Narrow test/debug surface for exercising pagination as a projection problem
// without reaching through scroll event timing.
/** Re-render the current session's transcript from the cache — used when the
 *  user toggles a message-lane visibility filter so hidden lanes disappear and
 *  re-shown lanes reappear without a refetch. */
export function reprojectForVisibilityChange() {
  const sid = app.currentSessionId;
  const cache = sid ? _messageCache.get(sid) : null;
  if (!cache) return;
  _reprojectCachedTranscript(sid, cache, true);
}

export {
  _emitRealBubble,
  _mergeCachedRefresh,
  _prependMessagesToTranscript,
  _reprojectCachedTranscript,
  _renderSessionWindowed,
};

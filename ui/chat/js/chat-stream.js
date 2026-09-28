'use strict';

// Live WebSocket streaming into bubbles — accumulates chunks per turn, finalizes
// Terminal guards refine the historical rule: if (!app._stopPending) app.isProcessing = true.
// or interrupts agent steps, attaches tool-call accordion panels to agent bubbles.
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { renderSavedFileAttachmentCard } from '../../shared/js/attachments.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _refreshLucideIcons } from '../../shared/js/dom-utils.js';
import { copyText } from '../../shared/js/clipboard.js';
import { closeChatElementFullView, openChatElementFullView } from './chat-full-view.js';
import { addChatBubble, _fillAgentBubble, _formatRelativeTime, _applyGrouping, _setBubbleCreatedAt, _setBubbleSessionSeq, isMidRunOpeningEl, markMidRunOpening, openingOwnerOf, activityGroupPrecedesOpening, stripClosingEnvelope } from './chat-bubble.js';
import { _addBubbleActions, _getBubbleText, _setBubbleModel, _makeBubbleMoreBtn, _makeBubbleDeleteBtn } from './chat-bubble-actions.js';
import { _cacheAppendMessage, persistModeNoticeToCache } from './chat-message-cache.js';
import { buildToolRow } from '../../shared/js/chat-activity.js';
import { chatUiFlag, isDebugMode } from '../../shared/js/app-prompts.js';
import { isMessageTypeVisible } from '../../shared/js/chat-visibility.js';

function _stripToolCalls(text) {
  const idx = text.indexOf('\n\n[Tool calls: ');
  return idx !== -1 ? text.slice(0, idx) : text;
}

// C: does this assistant text read like a real answer rather than a short
// transitional line ('Now switching to premium…')? Long text always counts;
// short text counts when it is structurally an answer (markdown heading or
// bullet list). Mirrors the backend _is_substantive_answer in loop.py.
function _isSubstantiveAnswer(text) {
  if (!text) return false;
  const stripped = String(text).replace(/[\s#>_*`\[\]()|]/g, '');
  if (stripped.length >= 200) return true;
  return /^[ \t]*#{1,4}[ \t]+\S|^[ \t]*[-*+][ \t]+\S/m.test(text);
}

function _setActivityGroupExpanded(container, expanded) {
  if (!container) return;
  const head = container.querySelector('.bubble-tool-calls-head');
  const panel = container.querySelector('.bubble-tool-calls-panel');
  const isExpanded = Boolean(expanded);
  head.setAttribute('aria-expanded', String(isExpanded));
  panel.hidden = !isExpanded;
  panel.classList.toggle('open', isExpanded);
  panel.classList.remove('opening', 'closing');
  panel.style.maxHeight = '';
  container.classList.toggle('open', isExpanded);
  // The chevrons are threshold-driven: a collapsed panel has no scrollable
  // view at all, so re-evaluate the nav on every expand/collapse transition.
  panel.__activityJumpNav?.__refreshDisabledState?.();
}

function _findAgentBubbleForTurn(turnId) {
  if (!app.chatMessages) return null;
  if (turnId) {
    // A merged bubble's top-level id belongs to its newest section. Search the
    // per-section durable ids too, or a WS replay can miss an earlier section
    // and create a second visible copy of that same assistant interaction.
    const key = CSS.escape(String(turnId));
    const selector = [
      `.chat-bubble.agent[data-turn-id="${key}"]`,
      `.chat-bubble.agent[data-msg-id="${key}"]`,
      `.chat-bubble.agent > .turn-section.llm-section[data-turn-id="${key}"]`,
      `.chat-bubble.agent > .turn-section.llm-section[data-msg-id="${key}"]`,
    ].join(', ');
    const matches = Array.from(app.chatMessages.querySelectorAll(selector))
      .map(node => node.closest('.chat-bubble.agent'))
      .filter((bubble, index, all) => bubble && all.indexOf(bubble) === index);
    if (!matches.length) return null;

    // Live WS, replay, and durable reconciliation may all arrive for one
    // interaction. Keep one canonical bubble (the terminal closer wins) and
    // remove only duplicate sections when their parent also owns other turns.
    const canonical = matches.find(bubble => bubble.classList.contains('terminal-closer')) || matches[0];
    for (const duplicate of matches) {
      if (duplicate === canonical) continue;
      const sections = Array.from(duplicate.querySelectorAll(':scope > .turn-section.llm-section'));
      const matchingSections = sections.filter(section =>
        section.dataset.turnId === String(turnId) || section.dataset.msgId === String(turnId),
      );
      if (matchingSections.length && matchingSections.length < sections.length) {
        for (const section of matchingSections) {
          const gutter = section.nextElementSibling;
          if (gutter?.classList.contains('turn-gutter')) gutter.remove();
          section.remove();
        }
      } else {
        // Keep future consecutive agent sections attached to the surviving
        // bubble; otherwise a removed live merge anchor could absorb a later
        // message while detached from the transcript.
        if (app._agentTurnBubble === duplicate) app._agentTurnBubble = canonical;
        duplicate.remove();
      }
    }
    _applyGrouping(canonical);
    if (canonical.nextElementSibling) _applyGrouping(canonical.nextElementSibling);
    return canonical;
  }
  const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
  return bubbles[bubbles.length - 1] || null;
}

function _fillStreamingText(container, text) {
  container.textContent = text || '';
  container.classList.remove('md');
  container.__mdSource = text || '';
  return false;
}

function _setBubbleText(bubble, text, extraClass) {
  if (!bubble) return;

  // The closing envelope is machine metadata, never transcript copy (see
  // chat-bubble.stripClosingEnvelope). This writer is the LIVE path that bypasses
  // addChatBubble — most importantly when a run finalizes: the freshly created
  // (already stripped) closer is immediately repainted here with the RAW response
  // text, so without this strip a streamed closer shows `FLAG: <name>` and its
  // call-to-action until a reload re-projects the bubble from the durable row.
  text = stripClosingEnvelope(text);

  // A FINAL (non-streaming) agent message landing in a bubble that already
  // carries a tool grouping is that grouping's closer — stand its collapsed
  // preview down (see _syncToolCallPreviews). Must run BEFORE the same-content
  // guard: streaming already painted the same text, so the guard would return
  // and the marker would never be set. In fold mode the closer is a response
  // row inside the grouping instead, so this marker is not set — the preview
  // keeps showing the latest row (including the final response).
  if (extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    if ((text || '').trim() && bubble.querySelector(':scope > .turn-section.tool-section')) {
      bubble.querySelectorAll('.bubble-tool-calls').forEach(c => { c.__closedByCloser = true; });
    }
  }

  // Same-content guard: skip the DOM destroy/rebuild when a non-streaming
  // bubble already displays this exact text. Without this the reconcile loop
  // and finalize paths destroy every bubble's DOM subtree and rebuild it from
  // scratch (Markdown re-parse, DOMPurify, Lucide re-instantiation) on every
  // tick — visible as flash/re-layout of the entire transcript.
  if (extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    const existing = _getBubbleText(bubble) || bubble.__mdSource || '';
    if (existing && existing.trim() === (text || '').trim()) return;
  }
  // Any agent text landing after the latest tool grouping is the closer
  // message — stand down the collapsed preview (see _syncToolCallPreviews).
  _syncToolCallPreviews();

  const directLlmSections = Array.from(
    bubble.querySelectorAll(':scope > .turn-section.llm-section'),
  );
  const activeLlmSection = directLlmSections[directLlmSections.length - 1] || null;

  // A streamed bubble already has the same section structure as a saved bubble.
  // Rewrite only its active LLM section. The previous implementation rendered
  // the new text directly into the bubble and then restored the old LLM section,
  // producing two visible copies and discarding the footer on every chunk.
  if (activeLlmSection) {
    while (activeLlmSection.firstChild) activeLlmSection.removeChild(activeLlmSection.firstChild);
    // During streaming, keep one plain text node. Parsing/sanitizing the entire
    // accumulated Markdown on every token is quadratic; finalization below runs
    // the full Markdown renderer exactly once for the authoritative response.
    const isMd = extraClass === 'streaming'
      ? _fillStreamingText(activeLlmSection, text)
      : _fillAgentBubble(activeLlmSection, text, true);
    activeLlmSection.classList.toggle('md', isMd);
    bubble.__mdSource = text || '';

    // PRESERVE the bubble's identity classes (terminal-closer, activity-group,
    // grouped-*, …) across the content rewrite. Rebuilding the whole className
    // from `extraClass` alone silently dropped `terminal-closer` — the handle
    // every finalize/reconcile path uses to FIND and REUSE the run's one closer
    // (_renderCompletionCloser, _groupClosersByOwner). Losing it made the next
    // finalize for the same interaction create a SECOND copy of the message,
    // which only a reload cleared (the durable re-projection paints the row
    // once). Only the transient streaming flag is dropped here.
    const preserved = Array.from(bubble.classList)
      .filter(c => c !== 'chat-bubble' && c !== 'agent' && c !== 'streaming' && c !== extraClass);
    bubble.className = 'chat-bubble agent' + (extraClass ? ' ' + extraClass : '');
    for (const cls of preserved) bubble.classList.add(cls);
    if (isMd) bubble.classList.add('md');
    _applyGrouping(bubble);
    if (bubble.nextElementSibling) _applyGrouping(bubble.nextElementSibling);

    // Keep the saved-message footer visible while content grows. This is
    // idempotent: existing gutters are reused, then finalized in place.
    _addBubbleActions(bubble);
    if (extraClass !== 'streaming' && typeof app._finalizeBubbleSections === 'function') {
      app._finalizeBubbleSections(bubble);
    }
    return;
  }

  // If this bubble already has tool sections, append a NEW LLM section instead
  // of replacing content (multi-step turn: text → tools → more text).
  const hasToolSection = bubble.querySelector(':scope > .turn-section.tool-section');
  if (hasToolSection && extraClass !== 'streaming' && !bubble.classList.contains('streaming')) {
    const section = document.createElement('div');
    section.className = 'turn-section llm-section';
    const idx = bubble.querySelectorAll(':scope > .turn-section.llm-section').length;
    section.dataset.sectionIdx = String(idx);
    const isMd = _fillAgentBubble(section, text, extraClass !== 'streaming');
    if (isMd) { section.classList.add('md'); bubble.__mdSource = text; }
    bubble.appendChild(section);
    if (typeof app._wireLLMSection === 'function') app._wireLLMSection(section, bubble);
    return;
  }

  // Keep built sections, tool calls, and gutters across content rewrite.
  const keptSections = Array.from(bubble.querySelectorAll(':scope > .turn-section'));
  const keptGutters = Array.from(bubble.querySelectorAll(':scope > .turn-gutter'));
  const keptComponents = Array.from(bubble.querySelectorAll(':scope > .chat-component'));
  const hadPremium = bubble.classList.contains('premium');
  [...keptSections, ...keptGutters, ...keptComponents].forEach(el => el.remove());

  // Remove legacy footer if present
  const legacyFooter = bubble.querySelector(':scope > .bubble-actions');
  if (legacyFooter) legacyFooter.remove();

  // PRESERVE identity classes on this rebuild too. This is the no-LLM-section
  // twin of the active-section rewrite above: rebuilding the className from
  // `extraClass` alone dropped `terminal-closer` (and activity-group / grouped-*
  // markers), which then made the summary lookup miss the closer's group and
  // fall into the merging fallback — re-rendering the same reply as a second
  // section. Only the transient streaming flag is dropped here.
  const preserved = Array.from(bubble.classList)
    .filter(c => c !== 'chat-bubble' && c !== 'agent' && c !== 'streaming' && c !== extraClass);
  while (bubble.firstChild) bubble.removeChild(bubble.firstChild);

  const isMd = extraClass === 'streaming'
    ? _fillStreamingText(bubble, text)
    : _fillAgentBubble(bubble, text, true);
  if (extraClass === 'streaming') {
    bubble.className = 'chat-bubble agent streaming';
  } else if (extraClass) {
    bubble.className = 'chat-bubble agent ' + extraClass;
  } else {
    bubble.className = 'chat-bubble agent';
  }
  for (const cls of preserved) bubble.classList.add(cls);
  if (isMd) bubble.classList.add('md');
  if (hadPremium) bubble.classList.add('premium');
  _applyGrouping(bubble);
  if (bubble.nextElementSibling) _applyGrouping(bubble.nextElementSibling);

  // Restore sections and gutters after the LLM body
  [...keptSections, ...keptGutters].forEach(el => bubble.appendChild(el));
  keptComponents.forEach(el => bubble.appendChild(el));

  // During streaming, skip section wiring — it will be done at finalization.
  if (extraClass !== 'streaming') {
    _addBubbleActions(bubble);
  }
}

const _wsTurnBuffers = new Map();   // turnId → accumulated content string
// Per step, `stream` and `agent_step_end` share ONE durable asst_id; the run
// turn (ownerTurnId) is constant across every step in a run, so it must never
// be the accumulator key — keying by it collapses every step into one segment
// and merges their text. We key every buffer by the step asst_id. agent_step_end
// is a PENDING-finalization signal, not an immediate publish boundary: after it
// we keep accepting ordered deltas and publish (flush) the step only once the
// deltas go quiet (debounce, reset by each delta) or a semantic boundary fires
// (tool call, final response, interruption, completion). A pending step is never
// exposed to preview/counters until it is flushed.
const _pendingStepFinalize = new Map();  // stepId (asst_id) → { content, createdAt, ownerTurnId, interactionSeq, progressKind, sessionId, timer }
const _PENDING_FLUSH_DEBOUNCE_MS = 250;

function _pendingStep(stepId) {
  return stepId ? _pendingStepFinalize.get(String(stepId)) : null;
}

function _cancelStepFlush(stepId) {
  if (!stepId) return;
  const key = String(stepId);
  const pending = _pendingStepFinalize.get(key);
  if (pending && pending.timer != null) clearTimeout(pending.timer);
  _pendingStepFinalize.delete(key);
}

function _cancelAllPendingStepFlushes() {
  for (const pending of _pendingStepFinalize.values()) {
    if (pending.timer != null) clearTimeout(pending.timer);
  }
  _pendingStepFinalize.clear();
}

function _armStepFlush(stepId) {
  if (!stepId) return;
  const key = String(stepId);
  const pending = _pendingStepFinalize.get(key);
  if (!pending) return;
  if (pending.timer != null) clearTimeout(pending.timer);
  // Each ordering delta re-arms the quiet period; the step publishes once the
  // deltas stop for _PENDING_FLUSH_DEBOUNCE_MS (or a boundary forces it sooner).
  pending.timer = setTimeout(() => _flushPendingStep(key, 'quiet-period'), _PENDING_FLUSH_DEBOUNCE_MS);
}

// Resolve the authoritative content for a step. The step_end payload
// (eventText) is the authoritative snapshot for this durable asstId; the WS
// buffer is an ordered accumulation of delta chunks (or a reconcile snapshot
// that lags the live stream). We must never let a mismatched/partial fragment
// override the complete authoritative payload, but we may extend it when the
// buffer is a genuine superset (buffer starts with the same authoritative text
// and also carries a trailing chunk the step_end hadn't captured yet). A buffer
// that is merely longer but is NOT a prefix-superset is a different/aligned
// fragment (e.g. a reconcile snapshot for a different step key) and is dropped.
function _resolveStepContent(content, stepId) {
  const eventText = String(content || '');
  const buffered = stepId ? String(_wsTurnBuffers.get(stepId) || '') : '';
  if (!buffered) return eventText;
  if (!eventText) return buffered;
  // The buffer is only trustworthy when it begins with the authoritative text
  // (prefix-superset). Any longer-but-mismatched buffer is a fragment and must
  // not override the complete step_end payload.
  // Neither is a prefix of the other: the buffer is a fragment or a different
  // step's snapshot. Trust the authoritative step_end payload.
  if (buffered.indexOf(eventText) === 0) return buffered;
  if (eventText.indexOf(buffered) === 0) return eventText;
  return eventText;
}

// Monotonic buffer write for the DB-tail reconcile path. The WS appender grows
// _wsTurnBuffers by ordered deltas, while the reconcile poll reads the durable
// row that is persisted on a ~0.6s throttle — so its snapshot LAGS the live
// stream. Overwriting the buffer with a shorter snapshot (e.g. "RED" while the
// live stream already holds "RED APPLE\nGREEN PEAR\nBLUE PLUM") truncates the
// step right before finalize. Only ever advance to a longer/later snapshot;
// never shrink a buffer the live stream has already completed.
function _setTurnBufferMonotonic(stepId, text) {
  if (!stepId) return;
  const key = String(stepId);
  const incoming = String(text || '');
  const current = String(_wsTurnBuffers.get(key) || '');
  // RECORDER-HOOK: plugins/recorder/frontend/recorder.js consumes this optional trace.
  // Removal: delete this comment and the following try block; keep application logic.
  try {
    window.__recorderTrace?.('cache.stream.monotonic', {
      interaction_id: key,
      decision: incoming.length>current.length?'apply':'discard',
      reason: incoming.length>current.length?'longer-snapshot':'not-longer',
      incoming_length: incoming.length,
      current_length: current.length,
    });
  } catch (_) {}
  if (incoming.length > current.length) {
    _wsTurnBuffers.set(key, incoming);
  }
}

function consumeFinalizedStepContent(content, asstId, ownerTurnId) {
  // Per-step consume: the accumulator is keyed by the durable step asst_id,
  // which stream and agent_step_end share. ownerTurnId is grouping metadata only.
  const resolved = _resolveStepContent(content, asstId);
  // The durable entry or terminal closer is about to become authoritative.
  // Retire its in-group provisional twin first so the DOM never has both.
  _removeLiveActivityPreview(asstId);
  if (asstId) _wsTurnBuffers.delete(asstId);
  _dropPendingStreamRender(asstId);
  return resolved;
}

const _pendingStreamRenders = new Map();
let _streamRenderTimer = null;
const _STREAM_RENDER_INTERVAL_MS = 100;

function _flushStreamRenders() {
  _streamRenderTimer = null;
  const pending = Array.from(_pendingStreamRenders.entries());
  _pendingStreamRenders.clear();
  let shouldFollowTail = false;
  for (const [key, item] of pending) {
    const text = key ? (_wsTurnBuffers.get(key) || '') : (app.agentBuffer || '');
    const bubble = _findAgentBubbleForTurn(key) || item.bubble;
    // RECORDER-HOOK: recorder.js observes decisions only. Remove this try block and comment together.
    try {
      window.__recorderTrace?.('stream.render', {
        interaction_id: key,
        decision: bubble?.isConnected?'apply':'discard',
        reason: bubble?.isConnected?'connected':'detached',
        text_length: text.length,
      });
    } catch (_) {}
    if (!bubble || !bubble.isConnected) continue;
    _setBubbleText(bubble, text, 'streaming');
    _addBubbleActions(bubble);
    if (app._activeTurnModel) {
      try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort, app._activeTurnDisplayName); } catch (_) {}
    }
    shouldFollowTail = true;
  }
  if (shouldFollowTail && typeof app._scrollToBottomIfNear === 'function') {
    app._scrollToBottomIfNear(
      app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
    );
  }
}

function _scheduleStreamRender(key, bubble, options = {}) {
  // RECORDER-HOOK: recorder.js observes decisions only. Remove this try block and comment together.
  try {
    window.__recorderTrace?.('stream.render.queued', {
      interaction_id: key,
      coalesced: _pendingStreamRenders.has(key || ''),
    });
  } catch (_) {}

  _pendingStreamRenders.set(key || '', { bubble, ...options });
  if (_streamRenderTimer !== null) return;
  _streamRenderTimer = setTimeout(_flushStreamRenders, _STREAM_RENDER_INTERVAL_MS);
}

function _dropPendingStreamRender(key) {
  // RECORDER-HOOK: recorder.js observes decisions only. Remove this try block and comment together.
  try {
    window.__recorderTrace?.('stream.render.discard', {
      interaction_id: key,
      reason: 'pending-render-cancelled',
      was_pending: _pendingStreamRenders.has(key || ''),
    });
  } catch (_) {}

  _pendingStreamRenders.delete(key || '');
  if (_pendingStreamRenders.size === 0 && _streamRenderTimer !== null) {
    clearTimeout(_streamRenderTimer);
    _streamRenderTimer = null;
  }
}

// A streamed assistant step is unclassified until its agent_step_end or final
// event arrives. Keep activity metadata in the live activity group, while the
// response text itself stays visible in a draft bubble keyed by its durable id.
function _livePreviewEntryId(asstId) {
  return asstId ? `live-preview:${String(asstId)}` : '';
}

function _upsertLiveActivityPreview(asstId, content, createdAt, ownerTurnId, interactionSeq) {
  const previewId = _livePreviewEntryId(asstId);
  if (!previewId || !app.chatMessages) return null;
  const owner = ownerTurnId || asstId;
  const group = attachActivityEntries([
    {
      kind: 'progress',
      id: previewId,
      content: String(content || ''),
      createdAt,
      livePreview: true,
      // A token buffer is provisional, never terminal prose. It is retained as
      // the live activity row for manual inspection and counted in the collapsed
      // summary while streaming, then removed before authoritative settlement.
      _finalized: false,
    },
  ], null, {
    id: previewId,
    createdAt,
    turnId: owner,
    interactionSeq: Number.isFinite(Number(interactionSeq)) ? Number(interactionSeq) : undefined,
  }, owner, _runKey(owner, interactionSeq));
  // The live row updates the collapsed header only. Deliberately do not open the
  // disclosure: the user alone decides when its mini transcript is revealed.
  //
  // Mark the group live for the duration of the run. THIS is the path that
  // actually feeds the summary line while a run streams, so the sheen trigger in
  // app1.css keyed on `.live-activity` has to be applied here. The sibling
  // marker in attachProgressToActivity is not enough on its own: that function
  // only runs from a settled step boundary and returns early when mid-turn
  // progress is hidden, so with mid-turn output off the summary line would stay
  // visually settled for the whole run. Live-only by construction — persisted
  // history replays through segments and never reaches this function.
  // Run end clears it via mirrorActivityEnd, which strips the class from every
  // group that carries tool rows (the preview always lives in .bubble-tool-calls).
  // Routed through the singleton marker so this stays the session's only sheen.
  _markLiveActivityGroup(group);
  return group;
}

function _removeLiveActivityPreview(asstId) {
  const previewId = _livePreviewEntryId(asstId);
  if (!previewId || !app.chatMessages) return;
  const escaped = CSS.escape(previewId);
  app.chatMessages.querySelectorAll(`[data-activity-entry-id="${escaped}"]`).forEach(row => {
    const panel = row.parentElement;
    const container = row.closest('.bubble-tool-calls');
    row.remove();
    if (container && Array.isArray(container.__calls)) {
      container.__calls = container.__calls.filter(entry => String(entry?.id || '') !== previewId);
    }
    const head = container && container.querySelector('.bubble-tool-calls-head');
    if (head && panel) _updateToolCallsHead(head, panel);
  });
}

function appendStreamToActiveBubble(textChunk, turnId, createdAt, ownerTurnId, interactionSeq) {
  if (textChunk == null) return;
  // A terminal answer closes its owner run. Late WS/reconcile stream events
  // for another step in that completed run must never resurrect a draft.
  if (ownerTurnId && app._runEndedFor?.[String(ownerTurnId)]) return;
  // Stream deltas do not carry the durable render contract. Keep them inside
  // the activity group until a settled event supplies chat_render_kind; a
  // provisional chunk must never claim the direct-answer lane on its own.
  // The activity preview is collapsed by default and marks its group live,
  // which keeps the newest changing summary under the sheen.
  const hidden = !isMessageTypeVisible('progress');
  app._turnHasBubble = true;
  if (turnId) {
    // Accumulate into the PER-STEP buffer (keyed by the durable asst_id that
    // this step's stream and its agent_step_end both carry). Never key by the
    // run turn — that is constant across the run and merges every step.
    const cur = _wsTurnBuffers.get(turnId) || '';
    const next = cur + textChunk;
    _wsTurnBuffers.set(turnId, next);
    // A delta for a NEW step supersedes any still-pending prior step in the
    // same run: flush it at the boundary before the new step's deltas land.
    _flushPreviousPendingStep(ownerTurnId, turnId);
    // If this step is already pending (its agent_step_end arrived early), a
    // fresh delta re-arms the quiet-period debounce so the full ordered stream
    // accumulates before we publish.
    _armStepFlush(turnId);
  } else {
    if (app.agentBuffer === undefined) app.agentBuffer = '';
    app.agentBuffer += textChunk;
  }
  // A stream is unclassified. Render only an in-group live activity preview;
  // the explicit settled render kind later decides whether to fold it or
  // publish a promoted answer.
  if (!hidden && turnId) {
    _upsertLiveActivityPreview(
      turnId,
      _wsTurnBuffers.get(turnId) || '',
      createdAt,
      ownerTurnId,
      interactionSeq,
    );
    if (typeof app._scrollToBottomIfNear === 'function') {
      app._scrollToBottomIfNear(
        app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
      );
    }
  }
  // Don't re-engage processing state if a stop was requested — the backend is
  // still unwinding but the user chose to stop, so keep isProcessing=false so the
  // activity indicator doesn't revert from "Stopping…" to "Writing reply…".
  if (!app._stopPending) {
    if (!(turnId && app._runEndedFor && app._runEndedFor[String(turnId)])) app.isProcessing = true;
  }
  if (turnId && app.currentSessionId) {
    _cacheAppendMessage(app.currentSessionId, {
      role: 'assistant', content: textChunk, id: turnId,
      created_at: createdAt, _streaming: true,
      chat_render_kind: 'activity',
    });
  }
}

// A mid-run ``main`` response — the pre-tool progress message, or the assertions
// contract's introduction — is the run's OPENING message, not its answer. The
// backend marks it ``mid_run`` precisely because this lane otherwise means "the
// run is over": rendering it through the terminal path would close the run's
// controls and paint it as the run's one closer, which the real answer then
// overwrites in place, so the opening never survives on screen. Instead it gets
// its own bubble now and the run keeps going. Reload agrees: session-load's
// phased builder renders an opening row as its own standalone bubble too (see
// `mid_run_opening` on the projected row), never as a closer.
//
// The opening is also a transcript BOUNDARY, exactly like a durable summary row:
// the work published after it is the run's NEXT phase and belongs to its OWN
// activity group BELOW this bubble, not to the group above it. So the bubble is
// stamped with the shared opening mark (chat-bubble.js `markMidRunOpening`) and
// the phase above it is retired — without that the pre-opening group keeps
// absorbing the rest of the run, the whole run collapses into one disclosure, and
// the summary line reads as if nothing happened after the opening.
function _publishMidRunAnnouncement(text, asstId, createdAt, interactionSeq, ownerTurnId) {
  const body = (text || '').trim();
  if (!body || !isMessageTypeVisible('main')) return;
  // Neither joins an earlier agent bubble nor lets the run's answer join it: the
  // opening message must stay ONE bubble of its own — that is the whole point of
  // promoting it, and merging is exactly how the introduction previously lost its
  // identity to the step that came after it.
  app._agentTurnBubble = null;
  _removeLiveActivityPreview(asstId);
  const bubble = addChatBubble(
    'agent', body, 'notice', undefined,
    asstId || undefined, asstId || undefined, createdAt,
  );
  app._agentTurnBubble = null;
  if (!bubble) return;
  markMidRunOpening(bubble, ownerTurnId || '');
  // Close the phase that ended at this opening: its live group must never adopt
  // the work that follows (the entry would land ABOVE the opening it came after),
  // and the next live lookup must open a fresh disclosure for the new phase.
  _forgetLiveActivityGroup(ownerTurnId);
  if (app._activeToolGroupBubble) app._activeToolGroupBubble = null;
  if (createdAt) _setBubbleCreatedAt(bubble, createdAt);
  if (Number.isFinite(Number(interactionSeq))) {
    _setBubbleSessionSeq(bubble, Number(interactionSeq));
  }
  _applyGrouping(bubble);
  app._turnHasBubble = true;
  if (asstId) _wsTurnBuffers.delete(asstId);
  if (typeof app._scrollToBottomIfNear === 'function') {
    try {
      app._scrollToBottomIfNear(
        app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
      );
    } catch (_) { /* scrolling is best-effort */ }
  }
}

function finalizeAgentResponse(content, turnId, isReplayed, createdAt,
                               ownerTurnId, interactionSeq, chatRenderKind, midRun) {
  // A final response is a semantic boundary: first flush any already-pending
  // preceding progress segment for this run, then start a fresh final segment.
  _flushPreviousPendingStep(ownerTurnId, turnId);
  // The final response owns a fresh post-step segment and is the only terminal
  // authority. Consume it independently from every prior progress segment.
  content = consumeFinalizedStepContent(content, turnId, ownerTurnId);
  _dropPendingStreamRender(turnId);
  const text = (content || '').trim();
  if (text) {
    try {
      window.dispatchEvent(new CustomEvent('webagent-agent-response', {
        detail: {
          sessionId: app.currentSessionId || '',
          turnId: ownerTurnId || turnId || '',
          content: text,
          createdAt: createdAt || '',
          replayed: !!isReplayed,
        },
      }));
    } catch (_) { /* voice is an optional presentation layer */ }
  }
  // Mid-run openings take the bubble lane and leave the run running: every bit
  // of the terminal machinery below (closer, endRunControls, isProcessing) belongs
  // to the answer that lands later in this same run.
  // The server render kind is the complete live display contract.
  // A missing kind is deliberately folded instead of inferred from phase/text.
  const renderKind = String(chatRenderKind || 'activity').toLowerCase();
  if (midRun && renderKind === 'notice') {
    _publishMidRunAnnouncement(text, turnId, createdAt, interactionSeq, ownerTurnId);
    return;
  }
  // Hidden 'main' lane: skip all bubble DOM work but keep the state cleanup
  // below (buffers, isProcessing, send re-enable) so the UI never sticks.
  const hidden = !isMessageTypeVisible('main');
  // Empty response with no existing bubble → nothing to render.
  // Empty response with a bubble but no tool calls → remove the bubble
  // (the agent finished with no output).
  if (!text) {
    if (turnId) _wsTurnBuffers.delete(turnId);
    const bubble = _findAgentBubbleForTurn(turnId);
    if (bubble && !bubble.querySelector('.bubble-tool-calls')) {
      const prev = bubble.previousElementSibling;
      const next = bubble.nextElementSibling;
      if (app._activeToolGroupBubble === bubble) app._activeToolGroupBubble = null;
      bubble.remove();
      if (next && typeof _applyGrouping === 'function') _applyGrouping(next);
      else if (prev && prev.classList) prev.classList.remove('grouped-open');
    }
    app.agentBuffer = '';
    app._activeToolGroupBubble = null;
    app.isProcessing = false;
    // An empty/no-output completion is STILL an authoritative terminal state.
    // The submitted user turn must survive reconciliation: never remove it here
    // (we only ever remove an empty AGENT bubble above), and the run must be
    // left as a complete closed unit. Assign this run's activity group a stable
    // per-run key even when there are no durable assistant rows. The terminal
    // fallback is diagnostic detail, matching stopped/error closers: render it
    // only when chat_ui.json enables chat_common.debug.enabled.
    try {
      const emptyGroup = _coalesceLiveActivityGroups();
      if (emptyGroup) {
        const owner = ownerTurnId || (emptyGroup.dataset && emptyGroup.dataset.activityTurnId) || '';
        const key = _runKey(owner, interactionSeq);
        if (key && !emptyGroup.dataset.runKey) {
          emptyGroup.dataset.runKey = String(key);
        }
        if (isDebugMode()) {
          _renderTerminalCloser(
            emptyGroup, turnId, ownerTurnId || '', '', createdAt, 'No response',
          );
        }
      }
    } catch (_) { /* best-effort: the empty completion must not rethrow */ }
    // A completed run with no output is still an authoritative terminal state.
    if (typeof app.endRunControls === 'function') {
      try { app.endRunControls(ownerTurnId || turnId); } catch (_) {}
    }
    // A completed run must retire the live "Sending… / Wrote…" activity note
    // and its transcript mirror — normal completion never invokes stop(), so
    // the note would otherwise linger after the run settles.
    if (typeof app.mirrorActivityEnd === 'function') {
      try { app.mirrorActivityEnd(app.currentSessionId || ''); } catch (_) {}
    }
    if (app.chatSend) app.chatSend.disabled = false;
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
    if (typeof app.refreshSuggestions === 'function') {
      try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
    }
    if (typeof app.refreshActiveAbilities === 'function') {
      try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
    }
    return;
  }

  // A response becomes a transcript bubble only after storage persisted the
  // explicit promoted-answer contract. Every other terminal event is activity.
  if (renderKind !== 'promoted_answer') {
    _flushPreviousPendingStep(ownerTurnId, turnId);
    _dropPendingStreamRender(turnId);
    try {
      _publishFinalizedStep(text, turnId, createdAt, ownerTurnId, interactionSeq, '');
    } catch (_) { /* best-effort: a demoted reply must never break the run tail */ }
    if (app._runEndedFor) {
      if (ownerTurnId) app._runEndedFor[String(ownerTurnId)] = true;
      if (turnId) app._runEndedFor[String(turnId)] = true;
    }
    app.agentBuffer = '';
    app._activeToolGroupBubble = null;
    app.isProcessing = false;
    if (typeof app.endRunControls === 'function') {
      try { app.endRunControls(ownerTurnId || turnId); } catch (_) {}
    }
    if (typeof app.mirrorActivityEnd === 'function') {
      try { app.mirrorActivityEnd(app.currentSessionId || ''); } catch (_) {}
    }
    if (app.chatSend) app.chatSend.disabled = false;
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
    if (typeof app.refreshSuggestions === 'function') {
      try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
    }
    if (typeof app.refreshActiveAbilities === 'function') {
      try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
    }
    return;
  }

  // Folding mode: the final reply becomes a 'response' row inside the same
  // tools/updates group (head label: "N tool calls / M updates / K responses")
  // instead of its own main bubble. attachActivityEntries reuses the live
  // group for this turn or creates one; entry ids make it idempotent.
  const fold = !hidden && chatUiFlag('fold_main_messages', false);
  if (fold) {
    attachActivityEntries(
      [{ kind: 'response', id: turnId, content: text, createdAt, _finalized: true }],
      null,
      {
        id: turnId,
        createdAt,
        turnId: ownerTurnId,
        interactionSeq: Number.isFinite(Number(interactionSeq))
          ? Number(interactionSeq) : undefined,
      },
      ownerTurnId,
      ownerTurnId ? _runKey(ownerTurnId, interactionSeq) : '',
    );
    app._turnHasBubble = true;
    if (turnId) _wsTurnBuffers.delete(turnId);
    app.agentBuffer = '';
    app._activeToolGroupBubble = null;
    app.isProcessing = false;
    // A completed folded run still needs the authoritative terminal closer: the
    // final reply must not remain only as the in-group preview. End controls so
    // Stop→Continue immediately, and render exactly one separate closer after
    // the run's activity group using the final answer as its content.
    try {
      const closerGroup = _coalesceLiveActivityGroups();
      _renderCompletionCloser(text, turnId, ownerTurnId, createdAt, closerGroup, true);
    } catch (_) { /* closer is best-effort on completion */ }
    if (typeof app.endRunControls === 'function') {
      try { app.endRunControls(ownerTurnId || turnId); } catch (_) {}
    }
    // A completed folded run must also retire the live activity note/mirror:
    // normal completion never invokes stop(), so the "Sending…" note would
    // otherwise linger after the run settles.
    if (typeof app.mirrorActivityEnd === 'function') {
      try { app.mirrorActivityEnd(app.currentSessionId || ''); } catch (_) {}
    }
    if (app.chatSend) app.chatSend.disabled = false;
    if (typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
    }
    if (turnId && app.currentSessionId && content) {
      _cacheAppendMessage(app.currentSessionId, {
        role: 'assistant', content, id: turnId,
        created_at: createdAt, _finalized: true,
        chat_render_kind: 'promoted_answer',
      });
    }
    if (typeof app.refreshSuggestions === 'function') {
      try { app.refreshSuggestions(); } catch (_) {}
    }
    if (typeof app.refreshActiveAbilities === 'function') {
      try { app.refreshActiveAbilities(); } catch (_) {}
    }
    return;
  }

  // A real final promotes the provisional preview into the one terminal closer
  // for its activity group. Render that closer directly rather than first
  // creating a generic agent bubble and removing it during reconciliation.
  let completionGroup = null;
  let bubble = null;
  if (!hidden) {
    try { completionGroup = _coalesceLiveActivityGroups(); } catch (_) {}
    bubble = completionGroup
      ? _renderCompletionCloser(text, turnId, ownerTurnId, createdAt, completionGroup, true)
      : _findAgentBubbleForTurn(turnId);
  }
  // Id-miss safety net: if the id lookup failed (e.g. the bubble was created
  // by the DB-load path with a different id shape), scan for an existing bubble
  // whose text matches this content. Adopt it in place instead of creating a
  // duplicate. This is the common cause of "2 copies of every message" — the
  // WS replay / reconcile poll finds no bubble by id and creates a second one.
  if (!bubble && turnId && text && !hidden) {
    const all = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    for (const b of all) {
      // __mdSource is the raw markdown — the authoritative pre-render text.
      // _getBubbleText returns rendered DOM text (stripped of markdown syntax).
      // Check raw first so markdown-formatted content matches correctly.
      const t = (b.__mdSource || _getBubbleText(b) || '').trim();
      if (t && t === text) {
        b.setAttribute('data-turn-id', turnId);
        b.setAttribute('data-msg-id', turnId);
        bubble = b;
        break;
      }
    }
  }
  const fresh = !bubble;
  if (!hidden && fresh) {
    bubble = addChatBubble(
      'agent', content || '', undefined, undefined,
      turnId || undefined, turnId || undefined, createdAt,
    );
  }
  if (createdAt) _setBubbleCreatedAt(bubble, createdAt);
  if (bubble && Number.isFinite(Number(interactionSeq))) {
    _setBubbleSessionSeq(bubble, Number(interactionSeq));
  }
  // Tag the bubble with the model/effort that ran this turn (set in
  // chat-activity.js from the live llm_call_end event) before its footer is
  // built by _setBubbleText → _addBubbleActions.
  if (bubble && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort, app._activeTurnDisplayName); } catch (_) {}
  }
  if (!fresh) {
    _setBubbleText(bubble, content || '');
  } else if (bubble && typeof app._addBubbleActions === 'function') {
    app._addBubbleActions(bubble);
  }
  if (bubble && typeof app.attachPendingChatComponents === 'function') app.attachPendingChatComponents(bubble);
  app._turnHasBubble = true;
  if (turnId) _wsTurnBuffers.delete(turnId);
  app.agentBuffer = '';
  app._activeToolGroupBubble = null;
  app.isProcessing = false;
  // A group may have arrived between the initial promotion lookup and this
  // closer, so re-resolve the canonical group before rendering.
  if (app._runEndedFor) {
    if (ownerTurnId) app._runEndedFor[String(ownerTurnId)] = true;
    if (turnId) app._runEndedFor[String(turnId)] = true;
  }
  // cleanup (for example a late tool event). Only then perform the fallback
  // promotion; the normal group path already rendered its closer in place.
  if (!completionGroup) {
    try {
      const closerGroup = _coalesceLiveActivityGroups();
      if (closerGroup) _renderCompletionCloser(text, turnId, ownerTurnId, createdAt, closerGroup, true);
    } catch (_) { /* reconcile can retry the canonical projection */ }
  }
  // A normal completed run is the authoritative terminal state: end the active
  // controls so Stop→Continue immediately, and ensure the final reply is the
  // separate closer after the run's activity group (never only a preview).
  if (typeof app.endRunControls === 'function') {
    try { app.endRunControls(ownerTurnId || turnId); } catch (_) {}
  }
  // A normal completion never invokes stop(), so retire the live activity note
  // and its transcript mirror here or the "Sending…" note lingers.
  if (typeof app.mirrorActivityEnd === 'function') {
    try { app.mirrorActivityEnd(app.currentSessionId || ''); } catch (_) {}
  }
  if (app.chatSend) app.chatSend.disabled = false;
  if (typeof app.populateSessionSelect === 'function') {
    try { app.populateSessionSelect(app.currentUserId); } catch (_) {}
  }
  if (turnId && app.currentSessionId && content) {
    _cacheAppendMessage(app.currentSessionId, {
      role: 'assistant', content, id: turnId,
      created_at: createdAt, _finalized: true,
      chat_render_kind: 'promoted_answer',
    });
  }
  if (typeof app.refreshSuggestions === 'function') {
    try { app.refreshSuggestions(); } catch (_) { /* best-effort */ }
  }
  if (typeof app.refreshActiveAbilities === 'function') {
    try { app.refreshActiveAbilities(); } catch (_) { /* best-effort */ }
  }
}

function _renderCompletionCloser(text, turnId, ownerTurnId, createdAt, group, canonicalFinal = false) {
  if (!app.chatMessages) return null;
  const closerText = (text || '').trim();
  if (!closerText) return null;
  _stopActivityElapsed(group, createdAt);
  // Summary/overview events may arrive after the canonical final response.
  // They are metadata, never a replacement terminal output for this run.
  if (group && group.dataset.terminalCanonicalId && !canonicalFinal) {
    const existing = group.nextElementSibling;
    return existing && existing.classList.contains('terminal-closer') ? existing : null;
  }
  let bubble = null;
  // Scope the closer to THIS run's activity group, never to a global message id.
  // The closer is inserted directly after the group, so a late explicit final
  // must replace the group's own fallback closer in place — keying by turnId
  // alone can miss a fallback whose asst_id differs (a mismatch across the
  // interrupted step vs the final interaction), producing a second closer and
  // merging text from a prior run. There is exactly one closer per run, after
  // that run's activity group.
  if (group && group.isConnected) {
    const after = group.nextElementSibling;
    if (after && after.classList && after.classList.contains('terminal-closer')) {
      bubble = after;
    }
  }
  if (!bubble && turnId) {
    const idKey = CSS.escape(String(turnId));
    // Id-scoped, class-agnostic fallback: a closer whose terminal-closer class
    // was lost (older build, or a re-render that rewrote the class) must still be
    // ADOPTED here. Creating a fresh bubble in that case is exactly the "same
    // live message shown twice" defect — the duplicate disappears on reload
    // because the durable projection paints the interaction row once.
    bubble = app.chatMessages.querySelector(
      `.chat-bubble.agent.terminal-closer[data-msg-id="${idKey}"]`,
    ) || app.chatMessages.querySelector(
      `.chat-bubble.agent[data-msg-id="${idKey}"]:not(.tool-only)`,
    );
  }
  const isNew = !bubble;
  if (!bubble) {
    bubble = addChatBubble(
      'agent', closerText, 'terminal-closer', undefined,
      turnId || undefined, turnId || undefined, createdAt,
    );
  } else {
    // Re-assert the closer identity on a reused node (idempotent).
    bubble.classList.add('terminal-closer');
    _setBubbleText(bubble, closerText);
  }
  let excluded = false;
  if (group && group.isConnected && bubble) {
    // The response that becomes a separate terminal closer is terminal output,
    // not a progress preview candidate. Exclude it before the next preview sync.
    const detail = group.querySelector('.bubble-tool-calls-details');
    if (detail) {
      // Exclude BOTH response and progress rows, mirroring _promoteRunCloserBubble
      // (the durable reload path). A run whose final text also landed as a
      // progress row — e.g. the same content delivered live once as `progress`
      // (rendered in-group) and once as `main` (this closer) — must never keep
      // that row as the group's preview. Excluding only `.ca-response-row` here
      // left the progress twin showing the SAME text beside the closer, i.e. the
      // "live message shown doubled" defect, which a reload cleared because the
      // durable promotion excludes progress rows too. The group's own head count
      // (via _updateToolCallsHead below) and preview are then re-synced.
      for (const row of detail.querySelectorAll('.ca-response-row, .ca-progress-row')) {
        const entry = row.__entry;
        if (entry && (String(entry.id || '') === String(turnId || '')
            || String(entry.content || '').trim() === closerText)) {
          entry.previewExcluded = true;
          excluded = true;
        }
      }
    }
    group.insertAdjacentElement('afterend', bubble);
    // A canonical closer completes only its own immediately preceding activity
    // group. Other in-flight groups remain expanded.
    _setActivityGroupExpanded(group.querySelector('.bubble-tool-calls'), false);
    // The contiguous transcript segment after this group belongs to this run.
    // Replace ALL prior terminal candidates there, not only text-identical ones.
    let node = group.nextElementSibling;
    while (node && !node.classList?.contains('activity-group')
        && !node.classList?.contains('user')) {
      const next = node.nextElementSibling;
      const terminalCandidate = node.classList?.contains('summary-bubble')
        || node.classList?.contains('terminal-closer')
        || (node.classList?.contains('agent') && !node.classList?.contains('tool-only'));
      if (node !== bubble && terminalCandidate) node.remove();
      node = next;
    }
    if (canonicalFinal) group.dataset.terminalCanonicalId = String(turnId || ownerTurnId || 'final');
    // A late explicit final that replaced a fallback closer in place must claim
    // the closer node for its own interaction id, so a later reconcile/live
    // event for this run finds it by id (and never mistakes it for a prior run).
    if (canonicalFinal && bubble && turnId) {
      bubble.setAttribute('data-msg-id', String(turnId));
      bubble.setAttribute('data-turn-id', String(turnId));
    }
    // Anchor this closer to the SAME owner run/interaction sequence as its
    // activity group, so the canonical transcript reorder (which sorts by
    // data-session-seq) can always place the closer immediately after its
    // group — never detached or above it.
    if (bubble && group) {
      const owner = ownerTurnId || (group.dataset && group.dataset.activityTurnId) || '';
      if (owner) {
        bubble.setAttribute('data-activity-turn-id', String(owner));
        bubble.setAttribute('data-owner-run', String(owner));
      }
      // Exact per-run key (owner + leading durable seq) — the authoritative match
      // for this group. Never the ambiguous nearest/current fallback.
      const runKey = group.dataset && group.dataset.runKey;
      if (runKey) bubble.setAttribute('data-run-key', String(runKey));
    }
  }
  if (excluded && group && group.isConnected) {
    const head = group.querySelector('.bubble-tool-calls-head');
    const detail = group.querySelector('.bubble-tool-calls-details');
    const panel = detail && detail.querySelector('.bubble-tool-calls-panel');
    if (head && panel) _updateToolCallsHead(head, panel);
  }
  if (isNew && bubble && typeof app._addBubbleActions === 'function') {
    try { app._addBubbleActions(bubble); } catch (_) {}
  }
  return bubble;
}

function ensureStreamingBubbleForActiveTurn(turnId, createdAt) {
  if (!turnId || (app._runEndedFor && app._runEndedFor[String(turnId)])) return;
  // If a tool-only group already exists and is in the DOM, don't create a
  // streaming bubble — the tool group is already visible and will absorb
  // further tool calls from the WS stream.
  if (app._activeToolGroupBubble && app._activeToolGroupBubble.isConnected) {
    if (!app._stopPending) {
    if (!(turnId && app._runEndedFor && app._runEndedFor[String(turnId)])) app.isProcessing = true;
  }
    return;
  }
  let existing = _findAgentBubbleForTurn(turnId);
  if (existing) {
    _setBubbleCreatedAt(existing, createdAt);
    if (!app._stopPending) {
    if (!(turnId && app._runEndedFor && app._runEndedFor[String(turnId)])) app.isProcessing = true;
  }
    return;
  }
  // No existing bubble and no tool group — the turn hasn't produced any
  // visible content yet.  Do NOT create an empty streaming placeholder;
  // an empty bubble can stay orphaned if no text or tool calls ever arrive
  // before the run finishes (a split that disappears on refresh).  The WS
  // stream will create a bubble naturally when content arrives via
  // appendStreamToActiveBubble, or tool calls via attachToolCallsToLastBubble.
  // Just mark the processing state so the "thinking" indicator is lit.
  if (!app._stopPending) {
    if (!(turnId && app._runEndedFor && app._runEndedFor[String(turnId)])) app.isProcessing = true;
  }
}

// Flush a still-pending prior step of the same run when a NEW step (stream
// delta / agent_step_end) or a final response supersedes it — a semantic
// boundary. The new step id is excluded so its own pending row is untouched.
function _flushPreviousPendingStep(ownerTurnId, currentStepId) {
  const cur = currentStepId ? String(currentStepId) : '';
  for (const [stepId, pending] of Array.from(_pendingStepFinalize.entries())) {
    const key = String(stepId || '');
    if (cur && key === cur) continue;
    if (ownerTurnId && pending.ownerTurnId && pending.ownerTurnId !== ownerTurnId) continue;
    _flushPendingStep(key, 'boundary');
  }
}

function _flushPendingStep(stepId, reason) {
  const key = String(stepId || '');
  const pending = _pendingStepFinalize.get(key);
  if (!pending) return;
  // Late-timer guard: never publish a step that was left pending by a session
  // the user has switched away from — cancel it instead of touching the DOM.
  if (pending.sessionId && app.currentSessionId && pending.sessionId !== app.currentSessionId) {
    _cancelStepFlush(key);
    return;
  }
  if (pending.timer != null) clearTimeout(pending.timer);
  _pendingStepFinalize.delete(key);
  // Resolve the authoritative text (prefer the longer ordered stream) then
  // publish this step's complete payload through the same progress/promote path.
  const content = _resolveStepContent(pending.content || '', key);
  if (key) _wsTurnBuffers.delete(key);
  _dropPendingStreamRender(key);
  _publishFinalizedStep(content, key, pending.createdAt, pending.ownerTurnId,
    pending.interactionSeq, pending.progressKind);
}

// The actual publish/form decision for a settled step. Kept separate from
// finalizeAgentStep so the quiet-period/boundary flush can run it atomically.
function _publishFinalizedStep(content, asstId, createdAt, ownerTurnId, interactionSeq, progressKind) {
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  // Streamed progress is deliberately not transcript content. Durable DB events
  // provide the phase summary and durable-only expansion after the write lands.
  if (draft && draft.classList.contains('streaming')) draft.remove();
  if (asstId) _wsTurnBuffers.delete(asstId);
}


function finalizeAgentStep(content, asstId, createdAt, ownerTurnId, interactionSeq, progressKind) {
  // A completed owner run cannot accept late progress from a stale stream.
  if (ownerTurnId && app._runEndedFor?.[String(ownerTurnId)]) return;
  // agent_step_end is a PENDING-finalization signal, not an immediate publish
  // boundary. Register the step so its ordered deltas keep accumulating; it is
  // atomically published only at a semantic boundary (tool call, final response,
  // interruption, completion) or after a quiet-period debounce reset by each delta.
  if (!asstId) return;
  const key = String(asstId);
  // A freshly arriving step_end supersedes any still-pending prior step in the
  // same run — flush it at the boundary before registering this one.
  _flushPreviousPendingStep(ownerTurnId, asstId);
  _cancelStepFlush(asstId);
  _pendingStepFinalize.set(key, {
    content,
    createdAt,
    ownerTurnId,
    interactionSeq,
    progressKind,
    sessionId: app.currentSessionId || '',
    timer: null,
  });
  _armStepFlush(asstId);
}

function markAgentInterrupted(asstId, createdAt, interactionSeq, ownerTurnId, statusLabel) {
  // An interruption is a semantic boundary: cancel any still-pending step flush
  // so a later quiet-period timer can never publish into the DOM after the user
  // stopped. The interrupt path builds its own partial + terminal closer.
  _cancelAllPendingStepFlushes();
  // The transcript "Stopped" message is the terminal trigger: the moment it
  // renders (live WS event OR DB reconcile fallback), release the stop-pin so
  // "Stopping…" can never outlive the turn it belongs to.
  const stoppedByUser = app._stopPending === true;
  app._stopPending = false;
  _dropPendingStreamRender(asstId);
  _removeLivePreview(asstId);
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  const entries = [];
  // Interrupted/error status rows are diagnostic detail. Keep the activity
  // boundary for lifecycle cleanup, but expose the visible "Stopped" row only
  // when chat_ui.json enables chat_common.debug.enabled.
  if (isDebugMode()) {
    entries.push({
      kind: 'system',
      id: 'stopped-' + (asstId || ownerTurnId || Date.now()),
      label: statusLabel || 'Stopped',
      content: '',
    });
  }
  const group = attachActivityEntries(entries, null, {
    id: asstId,
    createdAt,
    turnId: ownerTurnId,
    interactionSeq: Number(interactionSeq),
  }, ownerTurnId, _runKey(ownerTurnId, interactionSeq));
  _stopActivityElapsed(group, createdAt);
  // Exactly one separate terminal closer after the activity group, using the
  // last complete working message as the fallback (never a partial fragment).
  // Terminal stopped/error closers are diagnostics, not user-facing transcript
  // copy. Keep the durable activity boundary and control cleanup, but show a
  // closer only when chat_ui.json explicitly enables debug mode.
  if (isDebugMode()) {
    _renderTerminalCloser(group, asstId, ownerTurnId, partial, createdAt, statusLabel);
  }
  if (draft) draft.remove();
  if (asstId) _wsTurnBuffers.delete(asstId);
  app.agentBuffer = '';
  app.isProcessing = false;
  // Authoritative terminal state: end active controls immediately so Stop can
  // never coexist with a settled/errored run, and remember this run id so a
  // stale later event cannot re-engage Stop.
  if (app._runEndedFor) {
    if (ownerTurnId) app._runEndedFor[String(ownerTurnId)] = true;
    if (asstId) app._runEndedFor[String(asstId)] = true;
  }
  if (typeof app.endRunControls === 'function') {
    try { app.endRunControls(ownerTurnId); } catch (_) {}
  }
  if (app.chatSend) app.chatSend.disabled = !((app.chatInput && app.chatInput.value.trim()));
  // Interruption is a terminal boundary too: retire the live "Sending… · Ns"
  // activity note and its transcript mirror so it can never outlive the turn.
  if (typeof app.mirrorActivityEnd === 'function') {
    try { app.mirrorActivityEnd(app.currentSessionId || ''); } catch (_) {}
  }
}

// Render exactly one terminal agent bubble (the "closer") AFTER this run's
// activity group. On failure/interruption use the last complete working message
// as the fallback content; if only a partial token fragment or punctuation
// draft exists, fall back to the terminal label so a fragment is never published
// as the closer.
function _renderTerminalCloser(group, asstId, ownerTurnId, partial, createdAt, statusLabel) {
  if (!app.chatMessages) return null;
  let text = (partial || '').trim();
  const fallback = (statusLabel || 'Stopped').trim();
  // A punctuation-only draft ("…", "-", ">", etc.) is not a complete message.
  const placeholder = /^[\s\u2026\u00b7.\-*/_~`#>|\[\]()]{1,12}$/;
  // On a terminal error/interrupt the live buffer/draft fragment is often not a
  // complete message. Fall back to the LAST COMPLETE WORKING MESSAGE in this
  // run's activity group (a ca-progress-row / ca-response-row text) so the
  // closer never publishes a fragment or a bare label when a real message
  // exists. Only when no complete working message is present do we use the
  // terminal label (Stopped / Error / Errored).
  if (!text || placeholder.test(text)) {
    const last = _lastCompleteMessageInRun(group);
    if (last) text = last;
  }
  const content = (text && !placeholder.test(text)) ? text : fallback;
  // Idempotent: if a terminal closer for this run already exists, update it in
  // place instead of creating a second bubble (live + reconcile can both fire).
  // Scope the lookup to this run's activity group so a fallback closer and a
  // late explicit final share the SAME closer node (exactly one per run after
  // the group), and an id mismatch (interrupted step asst_id vs final id) can
  // never spawn a second closer or merge another run's text.
  let bubble = null;
  if (group && group.isConnected) {
    const after = group.nextElementSibling;
    if (after && after.classList && after.classList.contains('terminal-closer')) {
      bubble = after;
    }
  }
  if (!bubble && asstId) {
    bubble = app.chatMessages.querySelector(
      `.chat-bubble.agent.terminal-closer[data-msg-id="${CSS.escape(String(asstId))}"]`,
    );
  }
  const created = !bubble;
  if (created) {
    bubble = addChatBubble(
      'agent', content, 'terminal-closer', undefined,
      asstId || undefined, asstId || undefined, createdAt,
    );
  } else {
    _setBubbleText(bubble, content);
  }
  if (group && group.isConnected && bubble) {
    group.insertAdjacentElement('afterend', bubble);
    // Interrupt/error closers complete only their own activity group.
    _setActivityGroupExpanded(group.querySelector('.bubble-tool-calls'), false);
    // Anchor this terminal closer to the SAME owner run/interaction sequence as
    // its activity group, so the canonical transcript reorder keeps each group
    // immediately before its one closer.
    const owner = ownerTurnId || (group.dataset && group.dataset.activityTurnId) || '';
    if (owner) {
      bubble.setAttribute('data-activity-turn-id', String(owner));
      bubble.setAttribute('data-owner-run', String(owner));
    }
    // Exact per-run key from the group — never an ambiguous nearest/current guess.
    const runKey = group.dataset && group.dataset.runKey;
    if (runKey) bubble.setAttribute('data-run-key', String(runKey));
  }
  if (created && bubble && typeof app._addBubbleActions === 'function') {
    app._addBubbleActions(bubble);
  }
  return bubble;
}

/**
 * Promote a reload-emitted standalone assistant bubble into this run's terminal
 * closer. The phased builder tagged the run's final (main/final) answer with
 * _agentCloser; this gives the bubble the terminal-closer class and anchors it to
 * the SAME owner run/interaction sequence as its activity group, so the canonical
 * transcript reorder keeps the group immediately before its one closer. It also
 * ends the run's active controls so a reloaded completed run never shows Stop.
 */
function _promoteRunCloserBubble(bubble, info) {
  if (!bubble || !bubble.classList) return bubble;
  // EXACT per-run key (owner turn id + leading durable seq), established in the
  // phased builder BEFORE rendering. Promotion is STRICTLY gated on it: if the
  // key is missing, or no activity group carries that exact key, we do NOT
  // promote — a bubble with no unambiguous run is left as ordinary prose. We
  // NEVER fall back to an ambiguous "nearest/current group" crossing a user
  // boundary, which is what previously merged an older orphan final into the
  // newest run's closer.
  const runKey = info && info.runKey ? String(info.runKey) : '';
  if (!runKey) return bubble;
  let group = null;
  if (app.chatMessages) {
    // A run can own MORE THAN ONE activity group: a mid-run opening (the plan
    // announcement / introduction) divides it into a before and an after phase.
    // The closer belongs to the LAST one — the phase the answer actually landed
    // in — exactly the rule chat-bubble's `_groupClosersByOwner` applies. Taking
    // the first match would re-anchor the closer to the pre-opening group and
    // pull it up above the opening it followed.
    const matches = app.chatMessages.querySelectorAll(
      `.chat-bubble.activity-group[data-run-key="${CSS.escape(runKey)}"]`,
    );
    group = matches.length ? matches[matches.length - 1] : null;
  }
  if (!group) return bubble;
  bubble.classList.add('terminal-closer');
  // Empty/no-output completion: the closer bubble starts empty. Fill it with the
  // last non-empty working message from this run's group so a reloaded empty
  // run still shows a real terminal closer (never a bare blank bubble), and fall
  // back to a deterministic failure label when no working message exists.
  if (info && info.emptyCloser && !((bubble.querySelector(':scope > .turn-section.llm-section, :scope > .bubble-body')?.textContent || '').trim())) {
    const lastMsg = _lastCompleteMessageInRun(group);
    const label = lastMsg || 'No response';
    _setBubbleText(bubble, label);
  }
  const asstId = info && info.asstId ? String(info.asstId) : '';
  const owner = info && info.owner ? String(info.owner) : '';
  if (asstId) {
    bubble.setAttribute('data-msg-id', asstId);
    bubble.setAttribute('data-turn-id', asstId);
  }
  if (owner) {
    bubble.setAttribute('data-activity-turn-id', owner);
    bubble.setAttribute('data-owner-run', owner);
  }
  if (runKey) bubble.setAttribute('data-run-key', runKey);
  // Mark the NOW-CONFIRMED activity group for this run so the closer is treated as
  // this run's terminal output (not a preview candidate), and so a late explicit
  // final replaces this in place.
  group.dataset.terminalCanonicalId = asstId || owner || 'final';
  // A persisted closer is authoritative completion for this exact activity group.
  _setActivityGroupExpanded(group.querySelector('.bubble-tool-calls'), false);
  const detail = group.querySelector('.bubble-tool-calls-details');
  if (detail) {
    for (const row of detail.querySelectorAll('.ca-response-row, .ca-progress-row')) {
      const entry = row.__entry;
      if (entry && entry.previewExcluded !== true
          && (String(entry.id || '') === asstId
            || (entry.content || '').trim() === (bubble.textContent || '').trim())) {
        entry.previewExcluded = true;
      }
    }
    const head = group.querySelector('.bubble-tool-calls-head');
    const panel = detail.querySelector('.bubble-tool-calls-panel');
    if (head && panel) _updateToolCallsHead(head, panel);
  }
  if (typeof app.endRunControls === 'function' && owner) {
    try { app.endRunControls(owner); } catch (_) {}
  }
  return bubble;
}

// Find the LAST COMPLETE working message within a run's activity group. On a
// terminal error/interrupt the live buffer often holds only a fragment, so the
// closer falls back to the newest substantive progress/response row already
// rendered for this run. Scoped strictly to the group so a prior run's message
// can never leak in; system/tool rows and placeholder fragments are skipped.
function _lastCompleteMessageInRun(group) {
  if (!group || !group.isConnected) return '';
  const rows = group.querySelectorAll(
    '.ca-progress-row, .ca-response-row, .ca-activity-entry-body',
  );
  const placeholder = /^[\s\u2026\u00b7.\-*/_~`#>|\[\]()]{1,12}$/;
  for (let i = rows.length - 1; i >= 0; i--) {
    const el = rows[i];
    // Prefer the body node's own text; skip a row that is only container markup.
    const body = el.classList && el.classList.contains('ca-activity-entry-body')
      ? el : el.querySelector('.ca-activity-entry-body');
    const source = body || el;
    const text = (source.textContent || '').trim();
    if (text && !placeholder.test(text)) return text;
  }
  return '';
}

// The one user-facing wording for a mode flip. Deliberately direction-agnostic:
// the COLLAPSED GROUP LINE must never name the mode (ask/plan/auto/workspace) —
// repeated clicks are one change, not one line per click. The EXPANDED row body
// still spells the change out ("Plan mode enabled"): the generic wording is for
// the summary inside the group line, not for the message in the mini transcript.
const MODE_NOTICE_LABEL = 'Mode change';

// The model-switch twin, same split: the group line says "Model change", the
// expanded row body says which model came on ("Premium model enabled").
const MODEL_NOTICE_LABEL = 'Model change';

// Notice types that net per activity group (see _projectNotices): the group
// shows the NET change it contains, so a flip back to where it began cancels.
const NOTICE_TYPES = ['mode', 'model'];

// Full sentence for a mode notice's row body ("Plan mode enabled"). Prefers the
// built-in wording, then the agent's configured execution_modes label, then the
// mode id. Only the body uses this — the group line uses MODE_NOTICE_LABEL.
function _modeNoticeBody(mode) {
  const key = String(mode || '').trim().toLowerCase();
  const labels = {
    ask: 'Ask mode enabled',
    plan: 'Plan mode enabled',
    auto: 'Auto mode enabled',
    wkspc: 'Workspace mode enabled',
  };
  let label = labels[key];
  try {
    const agents = window.__agentsSharedData && window.__agentsSharedData.agents;
    const active = Array.isArray(agents)
      ? agents.find(agent => agent && agent.id === app.currentAgentId) : null;
    ((active && active.execution_modes) || []).forEach(item => {
      if (item && item.id && String(item.id).toLowerCase() === key) {
        label = (item.label || item.id) + ' mode enabled';
      }
    });
  } catch (_) { /* fall back to the built-in wording below */ }
  return label || ((mode ? String(mode) : '') + ' mode enabled');
}

// Full sentence for a model notice's row body ("Standard model enabled",
// "Premium model enabled", "Custom 2 model enabled"). Derived from the notice's
// own state/slot/tool rather than the caller's prose, so the live row, the
// durable row and a reload all read the same. Never names a provider model id.
function _modelNoticeBody(anchor, fallback) {
  const a = anchor || {};
  const state = String(a.state || '').trim().toLowerCase();
  const tool = String(a.tool || '').trim().toLowerCase();
  const slot = String(a.slot || '').trim();
  const lower = slot.toLowerCase();
  const role = lower.indexOf('role:') === 0 ? slot.slice(5).trim() : '';
  if (state === 'default' || tool === 'reset' || tool === 'reset_to_default'
      || role.toLowerCase() === 'default') {
    return 'Standard model enabled';
  }
  if (tool === 'use_premium_model' || role.toLowerCase() === 'premium') {
    return 'Premium model enabled';
  }
  if (role) return role.charAt(0).toUpperCase() + role.slice(1) + ' model enabled';
  if (lower.indexOf('custom:') === 0) {
    const pos = slot.slice(7).trim();
    return pos ? 'Custom ' + pos + ' model enabled' : 'Custom model enabled';
  }
  if (lower.indexOf('entry:') === 0 || state || tool) return 'Custom model enabled';
  const text = String(fallback || '').trim();
  return text || 'Custom model enabled';
}

function notifyExecutionMode(mode, anchor) {
  // Live "Mode change" notice in the transcript — the mode-switch twin of the
  // "Stopped" system row. A warning-themed row rendered through the same
  // activity machinery, so an agent-driven switch (set_execution_mode tool
  // mid-run) lands between the tool calls of the turn that flipped it
  // (anchor.turnId), while a user pill click appends at the live activity group.
  // `anchor.from` is the mode in force BEFORE this flip: the per-group netting
  // (_projectModeNotices) cancels the notice when a flip returns the group to
  // the mode it began in. `label` is the row BODY ("Plan mode enabled"); the
  // collapsed group line shows the generic MODE_NOTICE_LABEL instead.
  const label = _modeNoticeBody(mode);
  const newMode = String(mode || '').trim().toLowerCase();
  const fromMode = String((anchor && anchor.from) || '').trim().toLowerCase();
  const entry = {
    kind: 'system',
    notice: 'mode',
    // status- prefix: the notice is a live-only row, not a persisted
    // interaction — the footer must not offer a delete button for it. Stable
    // per (mode, turn) so repeated flips within one turn don't stack rows.
    id: 'status-mode-' + (mode || 'x') + '-' + ((anchor && anchor.turnId) || Date.now()),
    label,
    content: '',
    // Netting inputs: the mode this flip ended in, and the mode it came from.
    // Both travel into the durable row's metadata so a reload nets a group
    // exactly like the live view did.
    mode: newMode,
    from: fromMode,
  };
  attachActivityEntries([entry], null, anchor || null, anchor && anchor.turnId,
    (anchor && anchor.runKey) || (anchor && anchor.turnId
      ? _runKey(anchor.turnId, anchor.interactionSeq) : ''));

  // Durable twin: in browser-authority mode the whole transcript lives in
  // IndexedDB, so the notice must be written there too or a reload loses it
  // (server/hybrid modes persist via the server system:mode row instead).
  // Best-effort and async — never blocks the live notice above.
  try {
    persistModeNoticeToCache(app.currentSessionId, {
      id: entry.id,
      role: 'system',
      content: label,
      source: 'system:mode',
      metadata: JSON.stringify({
        mode: newMode, from: fromMode, reason: (anchor && anchor.reason) || '',
      }),
      turn_id: (anchor && anchor.turnId) || null,
      status: 'complete',
    });
  } catch (_) { /* non-fatal */ }
}

function notifyModelSwitch(label, anchor) {
  // Live "Model change" notice in the transcript — the model-switch twin of the
  // mode notice, rendered through the same activity machinery. An agent-driven
  // switch (set_model / use_premium_model / reset_to_default mid-run) lands
  // between that turn's tool calls (anchor.turnId); a user footer-picker
  // selection appends at the live activity group.
  //
  // The row BODY spells the change out ("Premium model enabled") and the
  // collapsed group line shows the generic MODEL_NOTICE_LABEL. `anchor.from` /
  // `anchor.state` are the model STATE before and after ('default' | 'override');
  // the group netting nets on those, so an upgrade followed by a revert cancels.
  const text = _modelNoticeBody(anchor, label);
  const toState = String((anchor && anchor.state) || 'override').trim().toLowerCase();
  const fromState = String((anchor && anchor.from) || '').trim().toLowerCase();
  const entry = {
    kind: 'system',
    notice: 'model',
    // status- prefix: the notice is a live-only row, not a persisted
    // interaction — the footer must not offer a delete button for it. Stable
    // per turn so repeated flips within one turn don't stack rows.
    id: 'status-model-' + ((anchor && anchor.turnId) || Date.now()),
    label: text,
    content: '',
    model: toState,
    from: fromState,
  };
  attachActivityEntries([entry], null, anchor || null, anchor && anchor.turnId);

  // Durable twin (browser-authority mode): the whole transcript lives in
  // IndexedDB, so the notice must be written there too or a reload loses it
  // (server/hybrid modes persist via the server system:model row instead).
  // Best-effort and async — never blocks the live notice above.
  try {
    persistModeNoticeToCache(app.currentSessionId, {
      id: entry.id,
      role: 'system',
      content: text,
      source: 'system:model',
      metadata: JSON.stringify({
        initiator: (anchor && anchor.initiator) || '',
        tool: (anchor && anchor.tool) || '',
        model: (anchor && anchor.model) || '',
        slot: (anchor && anchor.slot) || '',
        // The model-state pair the reload netting needs; the row body text above
        // is the durable message the expanded transcript shows.
        from: fromState,
        to: toState,
      }),
      turn_id: (anchor && anchor.turnId) || null,
      status: 'complete',
    });
  } catch (_) { /* non-fatal */ }
}

function seedStreamingBubble(turnId, content, createdAt, ownerTurnId, interactionSeq) {
  if (!turnId) return;
  // Reconciliation may lag behind a terminal WS event. Preserve the terminal
  // projection rather than reviving a streaming draft from that stale snapshot.
  if (ownerTurnId && app._runEndedFor?.[String(ownerTurnId)]) return;
  const text = content || '';
  _setTurnBufferMonotonic(turnId, text);
  // Reconcile/replay uses the same visible draft representation as the live
  // socket, so reconnecting during a response does not hide its text.
  if (isMessageTypeVisible('main') && text) {
    let bubble = _findAgentBubbleForTurn(turnId);
    if (!bubble) {
      app._agentTurnBubble = null;
      bubble = addChatBubble(
        'agent', _wsTurnBuffers.get(turnId) || text, 'streaming', undefined,
        turnId, turnId, createdAt,
      );
    }
    if (bubble) {
      if (Number.isFinite(Number(interactionSeq))) {
        _setBubbleSessionSeq(bubble, Number(interactionSeq));
      }
      _scheduleStreamRender(turnId, bubble);
    }
  }
  app._turnHasBubble = true;
  if (!app._stopPending) {
    if (!(turnId && app._runEndedFor && app._runEndedFor[String(turnId)])) app.isProcessing = true;
  }
}

// Collapsed activity groups lead with counters. Once a live run reaches a full
// minute, the same compact line adds the latest activity notice plus a rounded
// duration; detailed streamed text remains available only after expansion.
function _setActivityGroupHead(head, label, panel, elapsedText = '') {
  head.replaceChildren();
  const summary = document.createElement('span');
  summary.className = 'bubble-tool-calls-label';
  summary.textContent = label || 'Activity';
  head.appendChild(summary);
  if (elapsedText) head.appendChild(document.createTextNode(` (${elapsedText})`));
  const chevron = document.createElement('span');
  chevron.className = 'bubble-tool-calls-chevron';
  chevron.setAttribute('aria-hidden', 'true');
  chevron.textContent = '›';
  head.appendChild(chevron);
}

function _formatActivityElapsed(ms) {
  const minutes = Math.floor(Math.max(0, Number(ms) || 0) / 60000);
  if (minutes < 1) return '';
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return hours ? `${hours}h${remainder ? ` ${remainder}m` : ''}` : `${minutes}m`;
}

function _refreshActivityElapsed(group, schedule = true) {
  if (!group?.isConnected) return;
  const startedAt = Number(group.__activityElapsedStartedAt);
  if (!Number.isFinite(startedAt)) return;
  const finishedAt = Number(group.__activityElapsedFinishedAt);
  const now = Number.isFinite(finishedAt) ? finishedAt : Date.now();
  const head = group.querySelector('.bubble-tool-calls-head');
  const panel = group.querySelector('.bubble-tool-calls-panel');
  if (!head || !panel) return;
  head.__activityElapsedText = _formatActivityElapsed(now - startedAt);
  _updateToolCallsHead(head, panel);
  if (!schedule || Number.isFinite(finishedAt)) return;
  const elapsed = Math.max(0, now - startedAt);
  const delay = 60000 - (elapsed % 60000) || 60000;
  group.__activityElapsedTimer = window.setTimeout(() => {
    _refreshActivityElapsed(group, true);
  }, delay);
}

function _activityTimestampMs(value) {
  if (typeof value === 'number') return Number.isFinite(value) ? value : NaN;
  if (typeof value !== 'string' || !value.trim()) return NaN;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? parsed : NaN;
}

// A live group can be created by a progress or notice event after a tool has
// already started. Always prefer the earliest event timestamp we have, so the
// compact duration means elapsed wall-clock time for the run—not time since a
// later summary row happened to render.
function _earliestActivityStartedAt(entries, anchor) {
  const values = [
    anchor?.startedAt, anchor?.started_at, anchor?.createdAt, anchor?.created_at,
    ...(entries || []).flatMap(entry => [
      entry?.startedAt, entry?.started_at, entry?.createdAt, entry?.created_at,
    ]),
  ].map(_activityTimestampMs).filter(Number.isFinite);
  return values.length ? Math.min(...values) : NaN;
}

function _startActivityElapsed(group, startedAt) {
  if (!group) return;
  const candidate = _activityTimestampMs(startedAt);
  const existing = Number(group.__activityElapsedStartedAt);
  if (Number.isFinite(existing)) {
    // Late tool metadata may carry the real start time. Move backward only;
    // never let a later notice reset a run that is already timing.
    if (Number.isFinite(candidate) && candidate < existing) {
      if (group.__activityElapsedTimer) window.clearTimeout(group.__activityElapsedTimer);
      group.__activityElapsedTimer = null;
      group.__activityElapsedStartedAt = candidate;
      _refreshActivityElapsed(group, true);
    }
    return;
  }
  group.__activityElapsedStartedAt = Number.isFinite(candidate) ? candidate : Date.now();
  _refreshActivityElapsed(group, true);
}

function _stopActivityElapsed(group, finishedAt) {
  if (!group || !Number.isFinite(Number(group.__activityElapsedStartedAt))) return;
  if (group.__activityElapsedTimer) window.clearTimeout(group.__activityElapsedTimer);
  group.__activityElapsedTimer = null;
  const parsed = Date.parse(finishedAt || '');
  group.__activityElapsedFinishedAt = Number.isFinite(parsed) ? parsed : Date.now();
  _refreshActivityElapsed(group, false);
}

// "N tool calls" label on the collapsible head, recomputed whenever rows change.
function _updateToolCallsHead(head, panel) {
  const rows = Array.from(panel.children).filter(row => !row.classList.contains('bubble-tool-calls-jump-nav'));
  const summaryCounts = panel.__summaryCounts || null;
  const deferred = rows.length === 0 && Array.isArray(panel.__summaryCalls) ? panel.__summaryCalls : null;
  if (summaryCounts || (deferred && deferred.length)) {
    const tools = summaryCounts ? Number(summaryCounts.tools || 0)
      : deferred.filter(entry => entry && !['progress', 'system', 'response'].includes(entry.kind)).length;
    const messages = summaryCounts ? Number(summaryCounts.messages || 0)
      : deferred.filter(entry => entry && ['progress', 'response'].includes(entry.kind) && entry.previewExcluded !== true).length;
    const mode = summaryCounts ? Number(summaryCounts.mode || 0)
      : deferred.filter(entry => entry && entry.notice === 'mode').length;
    const model = summaryCounts ? Number(summaryCounts.model || 0)
      : deferred.filter(entry => entry && entry.notice === 'model').length;
    const compactions = summaryCounts ? Number(summaryCounts.compactions || 0)
      : deferred.filter(entry => entry && entry.notice === 'compaction').length;
    const updates = summaryCounts ? Math.max(1, Number(summaryCounts.updates || 1)) : 0;
    const parts = [];
    if (tools) parts.push(tools === 1 ? '1 tool' : tools + ' tools');
    if (messages) parts.push(messages + (messages === 1 ? ' message' : ' messages'));
    if (mode) parts.push(mode + (mode === 1 ? ' mode change' : ' mode changes'));
    if (model) parts.push(model + (model === 1 ? ' model change' : ' model changes'));
    if (compactions) parts.push(compactions + (compactions === 1 ? ' compaction' : ' compactions'));
    if (!parts.length && updates) parts.push(updates + (updates === 1 ? ' update' : ' updates'));
    _setActivityGroupHead(head, parts.join(' / '), panel, head.__activityElapsedText || '');
    return;
  }
  // Derive every total from the exact direct rows the disclosure opens. Named
  // collections keep the label and expanded content on one semantic source.
  const toolRows = rows.filter(row => !row.classList.contains('ca-progress-row')
    && !row.classList.contains('ca-system-row')
    && !row.classList.contains('ca-response-row'));
  // Progress updates and folded final replies are both agent messages in the
  // activity group. Keep their visual row types, but summarize them together.
  // Count every distinct message/progress row (NOT only _finalized): a
  // progress-only run group must still read "1 message", and distinct progress
  // steps must keep their own counters instead of collapsing to a blank label.
  const messageRows = rows.filter(row => (row.classList.contains('ca-progress-row')
      || row.classList.contains('ca-response-row'))
    && row.__entry?.previewExcluded !== true);
  const modeRows = rows.filter(row => row.classList.contains('ca-mode-row'));
  const modelRows = rows.filter(row => row.classList.contains('ca-model-row'));
  const compactionRows = rows.filter(row => row.classList.contains('ca-compaction-row'));
  const n = toolRows.length;
  const messages = messageRows.length;
  const compactions = compactionRows.length;
  const summaryParts = [];
  if (n) summaryParts.push(n === 1 ? '1 tool' : n + ' tools');
  if (messages) summaryParts.push(messages + (messages === 1 ? ' message' : ' messages'));
  // One wording for every mode flip, whatever the row stored: the mode itself is
  // never named here (the pill shows it), and legacy durable rows that still say
  // "Ask mode enabled" render as the same generic notice.
  if (modeRows.length) summaryParts.push(modeRows.length
    + (modeRows.length === 1 ? ' mode change' : ' mode changes'));
  if (modelRows.length) summaryParts.push(modelRows.length
    + (modelRows.length === 1 ? ' model change' : ' model changes'));
  if (compactions) summaryParts.push(compactions
    + (compactions === 1 ? ' compaction' : ' compactions'));
  _setActivityGroupHead(head, summaryParts.join(' / '), panel, head.__activityElapsedText || '');
  _syncToolCallPreviews();
}

// ── Collapsed preview synchronization ───────────────────────────────────────
// The current finalized update remains visible beside its run until the run's
// separate closer is present. This preserves progress context while the full
// activity disclosure stays available, without ever duplicating terminal text.

// Monotonic build stamp: "latest" is the newest-BUILT container, not merely
// the last one in DOM order (append/coalesce order can lag construction).
let _previewGroupSeq = 0;

function _syncToolCallPreviews() {
  if (!app.chatMessages) return;
  app.chatMessages.querySelectorAll(".bubble-tool-calls").forEach(container => {
    const head = container.querySelector(".bubble-tool-calls-head");
    const panel = container.querySelector(".bubble-tool-calls-panel");
    if (!head || !panel) return;
    const group = container.closest('.chat-bubble.activity-group');
    const hasCloser = _hasCloserAfter(container);
    // Live groups are counter-only until a real terminal response arrives. Do
    // not let a profile flag create a sibling mini-preview beside the collapsed
    // disclosure; the preserved rows are available only after manual expansion.
    const liveUnclosed = group && !group.dataset.activitySegmentId && !hasCloser;
    _setPreviewVisible(head, panel, !liveUnclosed && !hasCloser);
  });
}

function _removeActivityPreviews() {
  if (!app.chatMessages) return;
  if (typeof _closeActivityPreviewPopover === 'function') _closeActivityPreviewPopover();
  app.chatMessages.querySelectorAll('.bubble-tool-calls-preview-slot').forEach(slot => {
    const container = slot.__previewContainer;
    if (container) container.__previewSlot = null;
    slot.remove();
  });
  app.chatMessages.querySelectorAll('.chat-bubble.activity-group').forEach(group => {
    _clearActivityPreviewTailReserve(group);
  });
}

// A preview replacement may become shorter. Preserve the active activity
// bubble's rendered height by putting only the lost height after its final
// visible child (normally the live progress note). This is geometry-only: it
// never owns scroll position, scheduling, or user input.
const _ACTIVITY_PREVIEW_TAIL_RESERVE = '--activity-preview-tail-reserve';

function _activityPreviewBubble(container) {
  const bubble = container && container.closest('.chat-bubble.agent.activity-group');
  return bubble && bubble.isConnected ? bubble : null;
}

function _clearActivityPreviewTailReserve(target) {
  const bubble = target && target.classList && target.classList.contains('activity-group')
    ? target : _activityPreviewBubble(target);
  if (bubble) bubble.style.removeProperty(_ACTIVITY_PREVIEW_TAIL_RESERVE);
}

function _captureActivityPreviewTailWatermark(container) {
  const bubble = _activityPreviewBubble(container);
  if (!bubble) return null;
  // Capture the already-watermarked total first, then expose the natural layout
  // for the replacement measurement so old reserve cannot compound.
  const height = bubble.getBoundingClientRect().height;
  bubble.style.setProperty(_ACTIVITY_PREVIEW_TAIL_RESERVE, '0px');
  return {bubble, height};
}

function _restoreActivityPreviewTailWatermark(snapshot) {
  if (!snapshot || !snapshot.bubble || !snapshot.bubble.isConnected) return;
  const naturalHeight = snapshot.bubble.getBoundingClientRect().height;
  const reserve = Math.max(0, snapshot.height - naturalHeight);
  snapshot.bubble.style.setProperty(_ACTIVITY_PREVIEW_TAIL_RESERVE, reserve + 'px');
}

// Stable, increasing key for an activity row so the preview can be replaced in
// a strictly monotonic direction and never regress to an older fragment. Prefers
// the entry's event/created ordering, falling back to DOM order.
function _activityRowMonoKey(row) {
  const entry = row && row.__entry;
  if (entry) {
    const seq = Number(entry.interactionSeq ?? entry.sessionSeq ?? entry.session_seq ?? 0);
    if (Number.isFinite(seq) && seq > 0) return seq * 1000;
    const rawCreated = entry.createdAt ?? entry.created_at;
    const created = typeof rawCreated === 'number' ? rawCreated : Date.parse(String(rawCreated || ''));
    if (Number.isFinite(created) && created > 0) return created;
  }
  const panel = row && row.parentNode;
  if (!panel) return 0;
  return Array.prototype.indexOf.call(panel.children, row);
}

function _setPreviewVisible(head, panel, show) {
  const container = head.parentNode;
  let slot = container.__previewSlot;
  let prevEl = slot && slot.isConnected
    ? slot.querySelector('.bubble-tool-calls-preview') : null;
  const removePrevious = () => {
    if (!prevEl && !slot) return;
    if (slot && slot.isConnected) slot.remove();
    container.__previewSlot = null;
    slot = null;
    prevEl = null;
  };
  // An expanded disclosure has no visible collapsed preview. Do not retain tail
  // space for hidden content; a later collapsed replacement establishes a fresh
  // natural baseline.
  if (container.classList.contains('open')) _clearActivityPreviewTailReserve(container);
  // The transcript activity group remains available; this optional sibling preview
  // is independently controlled by the chat UI profile.
  const source = show && chatUiFlag('show_activity_preview', false)
    ? _latestActivityRow(panel) : null;
  if (!source) {
    removePrevious();
    _clearActivityPreviewTailReserve(container);
    return;
  }
  // Reuse the EXACT row builder used for the panel entries — same classes,
  // same markdown-rendered body, same footer gutter (time + actions) — so the
  // preview is pixel-identical to the real update/response inside the panel.
  const entry = source.__entry;
  if (!entry) {
    removePrevious();
    _clearActivityPreviewTailReserve(container);
    return;
  }
  // A reconcile/DB response row can arrive as a partial fragment before the
  // message is finalized. Never let a partial response replace the last
  // COMPLETE preview; retain it until the row is explicitly finalized.
  if ((source.classList.contains('ca-response-row') || source.classList.contains('ca-progress-row'))
      && (entry._finalized !== true || entry.previewExcluded === true)) return;
  // The preview must move strictly forward in event/created order — never
  // regress to an older fragment once a newer complete message was shown.
  const newMonoKey = _activityRowMonoKey(source);
  const sourceText = entry.content || '';
  if (prevEl && prevEl.__monoKey > newMonoKey) return;
  // Keep the existing preview only while it mirrors the SAME source row AND the
  // SAME content — re-build when the row grows to its complete form instead of
  // leaving a stale partial fragment (e.g. "RED" from "RED LINE / GREEN LINE").
  if (prevEl && prevEl.__sourceRow === source && prevEl.__sourceText === sourceText) return;

  // Initial previews start naturally. A genuine replacement snapshots the
  // group's current total height before its old preview disappears.
  const watermark = prevEl ? _captureActivityPreviewTailWatermark(container) : null;
  if (!prevEl) _clearActivityPreviewTailReserve(container);
  removePrevious();

  const fresh = _buildActivityEntryRow(entry);
  fresh.classList.add('bubble-tool-calls-preview');
  fresh.__sourceRow = source;
  fresh.__monoKey = newMonoKey;
  fresh.__sourceText = sourceText;
  const fullView = document.createElement('button');
  fullView.type = 'button';
  fullView.className = 'bubble-tool-calls-preview-full';
  fullView.title = 'Open full preview';
  fullView.setAttribute('aria-label', 'Open full preview');
  fullView.innerHTML = '<i data-lucide="maximize-2" style="width:14px;height:14px;"></i>';
  fullView.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    _openActivityPreviewPopover(fresh, fullView);
  });
  fresh.prepend(fullView);
  _refreshLucideIcons(fullView);
  slot = document.createElement('div');
  slot.className = 'bubble-tool-calls-preview-slot';
  slot.__previewContainer = container;
  slot.appendChild(fresh);
  const group = _activityPreviewBubble(container);
  if (!group) return;
  // PREVIEW-SIBLING: keep the preview outside the activity group so it remains
  // a distinct transcript bubble directly after its owner.
  group.insertAdjacentElement('afterend', slot);
  container.__previewSlot = slot;
  _restoreActivityPreviewTailWatermark(watermark);
}

let _activityPreviewPopover = null;

function _closeActivityPreviewPopover() {
  _activityPreviewPopover = null;
  closeChatElementFullView();
}

function _removeLivePreview(asstId) {
  // Interruption is a terminal boundary for the step it names. Dismiss any open
  // full-preview popover and stand down this run's collapsed live preview so a
  // stale streaming fragment can never linger after the turn.
  if (typeof _closeActivityPreviewPopover === 'function') {
    try { _closeActivityPreviewPopover(); } catch (_) {}
  }
  if (!app.chatMessages) return;
  _removeActivityPreviews();
}

function _openActivityPreviewPopover(preview, returnFocus) {
  const body = preview && preview.querySelector('.ca-activity-entry-body');
  if (!body) return;
  // The shared viewer clones this rendered body, preserving the frozen-preview
  // behavior while making the same accessible dialog available to all gutters.
  _activityPreviewPopover = true;
  openChatElementFullView(body, returnFocus, 'Full preview');
}

// True when a REAL final agent message (the closer) renders AFTER this
// grouping's bubble — it stands the preview down. Tool-only groups, streaming
// drafts, interrupted/error markers, recovery notices and placeholder text are
// NOT closers; only a substantive final agent bubble counts.
function _hasCloserAfter(container) {
  if (!app.chatMessages) return false;
  const groupBubble = container.closest('.chat-bubble');
  if (!groupBubble || !groupBubble.isConnected) return false;
  const agents = Array.from(app.chatMessages.querySelectorAll(':scope > .chat-bubble.agent'));
  let seenGroup = false;
  for (const b of agents) {
    if (b === groupBubble) { seenGroup = true; continue; }
    if (!seenGroup) continue;
    if (b.classList.contains('tool-only')) continue;
    if (b.classList.contains('streaming')) continue;
    if (b.classList.contains('interrupted')
        || b.classList.contains('error')
        || b.classList.contains('recovery-notice')) continue;
    if (b.querySelector('.bubble-tool-calls')) continue;
    const text = (b.__mdSource || _getBubbleText(b) || '').trim();
    if (!text) continue;
    // Placeholder/punctuation-only drafts ("…", "-", ">", etc.) are not a closer.
    if (/^[\s\u2026\u00b7.\-*/_~`#>|\[\]()]{1,12}$/.test(text)) continue;
    return true;
  }
  return false;
}

// The newest update/response row (progress or response) in the panel, or null.
// Scans backwards so the most recently appended entry wins. Only LLM OUTPUT
// rows are considered — tool-call and system rows are skipped entirely (a
// grouping with no updates/responses shows no preview).

function _latestActivityRow(panel) {
  const rows = Array.from(panel.children).filter(row => !row.classList.contains('bubble-tool-calls-jump-nav'));
  for (let i = rows.length - 1; i >= 0; i--) {
    const row = rows[i];
    if (row.classList.contains('ca-progress-row')
        || row.classList.contains('ca-response-row')) {
      // Never surface an unfinalized partial response fragment — keep looking
      // for the newest COMPLETE finalized row instead.
      const entry = row.__entry;
      if (!entry || entry._finalized !== true || entry.previewExcluded === true) {
        continue;
      }
      return row;
    }
  }
  return null;
}

// Scroll a row into view inside the panel only (never the transcript
// scroller): pins the row's bottom to the panel's visible bottom edge.
function _scrollPanelToRow(panel, row) {
  if (!panel || !row || !row.parentNode) return;
  const pr = panel.getBoundingClientRect();
  const rr = row.getBoundingClientRect();
  const relTop = rr.top - pr.top + panel.scrollTop;
  const relBottom = relTop + rr.height;
  if (relTop < panel.scrollTop || relBottom > panel.scrollTop + pr.height) {
    panel.scrollTop = relBottom - pr.height;
  }
}

// ── Tool-calls heading delete button ────────────────────────────────────────
// Two-click confirm that recycles ALL assistant interactions whose tool children
// appear in this container (collected from container.__calls._detailMsgId).

// Collect unique parent interaction IDs represented in this tool-calls container.
function _toolCallsInteractionIds(container) {
  const ids = new Set();
  const calls = container && container.__calls;
  if (Array.isArray(calls)) {
    calls.forEach(c => { if (c && c._detailMsgId) ids.add(c._detailMsgId); });
  }
  // Fallback: use the bubble's id
  if (ids.size === 0) {
    const bubble = container && container.closest('.chat-bubble');
    const id = bubble && (bubble.getAttribute('data-msg-id') || bubble.getAttribute('data-turn-id'));
    if (id) ids.add(id);
  }
  return [...ids];
}

async function _deleteToolCallsInteractions(container, permanent) {
  const ids = _toolCallsInteractionIds(container);
  if (!ids.length) return false;
  const sid = app.currentSessionId;
  if (!sid) return false;
  const uid = app.currentUserId || '';
  let ok = true;
  for (const id of ids) {
    try {
      const url = apiPath('/api/v1/db/interaction?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(id)
        + '&include_children=true'
        + (permanent ? '&permanent=true' : '')
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'DELETE', headers: { ...authHeaders() } });
      if (!resp.ok) ok = false;
    } catch (_) { ok = false; }
  }
  return ok;
}

async function _restoreToolCallsInteractions(container) {
  const ids = _toolCallsInteractionIds(container);
  if (!ids.length) return false;
  const sid = app.currentSessionId;
  if (!sid) return false;
  const uid = app.currentUserId || '';
  let ok = true;
  for (const id of ids) {
    try {
      const url = apiPath('/api/v1/db/interaction/restore?db=user.db'
        + '&session_id=' + encodeURIComponent(sid)
        + '&interaction_id=' + encodeURIComponent(id)
        + '&include_children=true'
        + (uid ? '&user_id=' + encodeURIComponent(uid) : ''));
      const resp = await fetch(url, { method: 'POST', headers: { ...authHeaders() } });
      if (!resp.ok) ok = false;
    } catch (_) { ok = false; }
  }
  return ok;
}

function _makeToolCallsDeleteBtn(container) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'bubble-tool-calls-delete';
  btn.title = 'Delete all tool calls in this group';
  btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
  let _armedTimer = null;

  function _reset() {
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }
    btn.dataset.state = '';
    btn.title = 'Delete all tool calls in this group';
    btn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
    btn.classList.remove('warning');
  }

  btn.addEventListener('click', async (e) => {
    e.stopPropagation(); // don't toggle the panel
    if (btn.dataset.state !== 'warning') {
      document.querySelectorAll('.bubble-tool-calls-delete[data-state="warning"]').forEach(b => {
        b.dataset.state = '';
        b.title = 'Delete all tool calls in this group';
        b.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        b.classList.remove('warning');
      });
      btn.dataset.state = 'warning';
      btn.title = 'Click again to delete all tool calls';
      btn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      btn.classList.add('warning');
      if (_armedTimer) clearTimeout(_armedTimer);
      _armedTimer = setTimeout(_reset, 4000);
      return;
    }
    if (_armedTimer) { clearTimeout(_armedTimer); _armedTimer = null; }

    const ok = await _deleteToolCallsInteractions(container, false);
    if (!ok) { _reset(); return; }
    // Mark the tool-calls container + all rows as deleted
    container.classList.add('deleted');
    container.querySelectorAll('.ca-tool-row').forEach(row => row.classList.add('deleted'));
    // Replace delete btn with restore + permanent-delete
    _injectToolCallsDeletedActions(container, btn);
  });
  return btn;
}

// Replace the delete button with restore + permanent-delete in a deleted tool-calls heading.
function _injectToolCallsDeletedActions(container, oldBtn) {
  if (!container || !oldBtn) return;
  const head = container.querySelector('.bubble-tool-calls-head');
  if (!head) return;
  oldBtn.remove();

  const wrap = document.createElement('span');
  wrap.style.cssText = 'display:inline-flex;gap:3px;margin-left:auto;flex:0 0 auto;';

  const restoreBtn = document.createElement('button');
  restoreBtn.type = 'button';
  restoreBtn.className = 'bubble-tool-calls-delete';
  restoreBtn.title = 'Restore all tool calls';
  restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
  restoreBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (restoreBtn.dataset.state !== 'warning') {
      restoreBtn.dataset.state = 'warning';
      restoreBtn.title = 'Click again to restore';
      restoreBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      restoreBtn.classList.add('warning');
      const t = setTimeout(() => {
        restoreBtn.dataset.state = '';
        restoreBtn.title = 'Restore all tool calls';
        restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
        restoreBtn.classList.remove('warning');
      }, 4000);
      restoreBtn._timer = t;
      return;
    }
    if (restoreBtn._timer) { clearTimeout(restoreBtn._timer); restoreBtn._timer = null; }
    const ok = await _restoreToolCallsInteractions(container);
    if (ok) {
      container.classList.remove('deleted');
      container.querySelectorAll('.ca-tool-row').forEach(row => row.classList.remove('deleted'));
      wrap.remove();
      const newBtn = _makeToolCallsDeleteBtn(container);
      head.appendChild(newBtn);
      _refreshLucideIcons(head);
    } else {
      restoreBtn.dataset.state = '';
      restoreBtn.title = 'Restore all tool calls';
      restoreBtn.innerHTML = '<i data-lucide="undo-2" style="width:11px;height:11px;"></i>';
      restoreBtn.classList.remove('warning');
    }
  });
  wrap.appendChild(restoreBtn);

  const permBtn = document.createElement('button');
  permBtn.type = 'button';
  permBtn.className = 'bubble-tool-calls-delete';
  permBtn.title = 'Permanently delete all tool calls';
  permBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
  permBtn.addEventListener('click', async (e) => {
    e.stopPropagation();
    if (permBtn.dataset.state !== 'warning') {
      permBtn.dataset.state = 'warning';
      permBtn.title = 'Click again to permanently erase';
      permBtn.innerHTML = '<i data-lucide="alert-triangle" style="width:12px;height:12px;"></i>';
      permBtn.classList.add('warning');
      const t = setTimeout(() => {
        permBtn.dataset.state = '';
        permBtn.title = 'Permanently delete all tool calls';
        permBtn.innerHTML = '<i data-lucide="trash-2" style="width:11px;height:11px;"></i>';
        permBtn.classList.remove('warning');
      }, 4000);
      permBtn._timer = t;
      return;
    }
    if (permBtn._timer) { clearTimeout(permBtn._timer); permBtn._timer = null; }
    const ok = await _deleteToolCallsInteractions(container, true);
    if (ok) container.remove();
  });
  wrap.appendChild(permBtn);

  head.appendChild(wrap);
}

// Append tool-call rows to an existing panel (used for both first render and
// merging later turns into the same grouped bubble). `entry.open` is honoured so
// a lazy-detail rebuild can restore which rows were expanded.
function _buildActivityEntryRow(entry, { messageActions = true } = {}) {
  const row = document.createElement('div');
  const statusRow = entry.kind === 'system';
  const responseRow = entry.kind === 'response';
  const noticeRow = statusRow
    && (entry.notice === 'mode' || entry.notice === 'model' || entry.notice === 'compaction');
  const noticeClass = entry.notice === 'model'
    ? ' ca-model-row'
    : entry.notice === 'compaction'
      ? ' ca-compaction-row'
      : ' ca-mode-row';
  row.className = 'ca-tool-row '
    + (statusRow ? 'ca-system-row' : responseRow ? 'ca-response-row' : 'ca-progress-row')
    + (noticeRow ? noticeClass : '');
  if (entry.livePreview) row.classList.add('ca-live-preview-row');
  if (entry.id) row.dataset.activityEntryId = String(entry.id);
  // Keep the source entry on the row so the collapsed preview can rebuild the
  // EXACT same row (markdown body + footer gutter) via this same builder.
  row.__entry = entry;

  // Updates, responses, and execution-mode changes are content-only. Other
  // operational notices retain their heading because it carries independent
  // meaning beyond the body text.
  if (statusRow && entry.notice !== 'mode' && entry.notice !== 'model') {
    const head = document.createElement('div');
    head.className = 'ca-activity-entry-head';
    const icon = document.createElement('span');
    icon.className = 'ca-activity-entry-icon';
    icon.textContent = entry.notice === 'compaction' ? '\u21BB' : noticeRow ? '\u26A0' : '?';
    const label = document.createElement('span');
    label.className = 'ca-activity-entry-label';
    label.textContent = entry.label || 'Stopped';
    head.append(icon, label);
    row.appendChild(head);
  }

  const modeNotice = entry.notice === 'mode';
  const modelNotice = entry.notice === 'model';
  // The row BODY carries the full sentence ("Plan mode enabled", "Premium model
  // enabled") — that is the message the expandable mini transcript shows. Only
  // the COLLAPSED group line is generic, so the generic constant is pinned onto
  // the dataset below where _updateToolCallsHead reads it, instead of being
  // allowed to fall through to the body text.
  const rowLabel = entry.label || 'Stopped';
  const body = document.createElement('div');
  body.className = 'ca-activity-entry-body';
  if (statusRow && !(entry.content || '').trim()) {
    body.textContent = rowLabel;
  } else {
    // Activity rows carry the same streamed text as the bubble lanes, so the
    // closing envelope must be stripped here too or the live preview row / the
    // expanded group prints the machine `FLAG:` line until a reload re-projects
    // the row. `entry.content` itself stays RAW: the preview/exclusion
    // comparisons downstream key on the stored text, not the painted one.
    _fillAgentBubble(body, stripClosingEnvelope(entry.content || ''), false);
  }
  row.appendChild(body);

  if (modeNotice || modelNotice) {
    // Content-only transient heads-up, exactly like the mode notice: no
    // ⚠ heading repeating the text, no per-message action footer. The dataset
    // carries the GROUP-LINE wording ("Mode change" / "Model change") and is
    // what _updateToolCallsHead reads, so the body's full sentence can never be
    // promoted into the group line.
    row.dataset[modeNotice ? 'modeNoticeLabel' : 'modelNoticeLabel'] =
      modeNotice ? MODE_NOTICE_LABEL : MODEL_NOTICE_LABEL;
    return row;
  }

  // Updates are assistant messages in their own right. Give each one the SAME
  // footer as every other bubble — time, collapse, speak, copy, delete (when
  // the row has a real interaction id), and the ⋮ context menu with the
  // per-message context readout. The footer is marked always-visible
  // ('notice-gutter') so the global click-to-close handler never hides it:
  // clicking the update body must not react.
  const realId = entry.id && !String(entry.id).startsWith('status-') ? String(entry.id) : '';
  if (realId) row.dataset.msgId = realId;

  const footer = document.createElement('div');
  footer.className = 'turn-gutter ca-activity-entry-footer notice-gutter';
  const createdAt = entry.createdAt || entry.created_at;
  const createdAtMs = typeof createdAt === 'number' ? createdAt : Date.parse(
    String(createdAt || '').replace(' ', 'T')
      + (/(?:Z|[+-]\d{2}:?\d{2})$/i.test(String(createdAt || '')) ? '' : 'Z'),
  );
  if (Number.isFinite(createdAtMs)) {
    const time = document.createElement('span');
    time.className = 'turn-gutter-time bubble-time';
    time.setAttribute('data-created-at', String(createdAtMs));
    time.textContent = _formatRelativeTime(createdAtMs);
    footer.appendChild(time);
  }
  const addButton = (title, iconName, action) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'turn-gutter-btn';
    button.title = title;
    button.innerHTML = '<i data-lucide="' + iconName + '" style="width:14px;height:14px;"></i>';
    button.addEventListener('click', event => { event.stopPropagation(); action(button); });
    footer.appendChild(button);
  };
  addButton('Collapse update', 'chevron-down', button => {
    const collapsed = row.classList.toggle('collapsed');
    body.hidden = collapsed;
    button.title = collapsed ? 'Expand update' : 'Collapse update';
    button.innerHTML = '<i data-lucide="' + (collapsed ? 'chevron-right' : 'chevron-down') + '" style="width:14px;height:14px;"></i>';
    _refreshLucideIcons(button);
  });
  // Activity-detail rows are hidden inside the group until it is expanded.
  // Reuse the same dialog as ordinary message gutters for their full content.
  addButton('Open full view', 'maximize-2', button => {
    openChatElementFullView(body, button, 'Full message');
  });
  addButton('Read aloud', 'volume-2', () => {
    if (!('speechSynthesis' in window)) return;
    try {
      window.speechSynthesis.cancel();
      window.speechSynthesis.speak(new SpeechSynthesisUtterance((entry.content || '').trim()));
    } catch (_) {}
  });
  addButton('Copy text', 'copy', async button => {
    try {
      await copyText((entry.content || '').trim());
      button.title = 'Copied!';
      setTimeout(() => { button.title = 'Copy text'; }, 1200);
    } catch (_) {}
  });
  if (realId && messageActions) footer.appendChild(_makeBubbleDeleteBtn(row));
  if (messageActions) footer.appendChild(_makeBubbleMoreBtn(footer, row));
  row.appendChild(footer);
  _refreshLucideIcons(footer);
  return row;
}

function _appendToolRows(panel, calls) {
  // Navigation overlays are panel children too, but activity-row indexes must
  // remain stable across live appends and reconciliation.
  const start = panel.querySelectorAll(':scope > .ca-tool-row').length;
  calls.forEach((entry, i) => {
    const rowEntry = entry;
    rowEntry.open = !!rowEntry.open;
    if (entry && (entry.kind === 'progress' || entry.kind === 'system'
        || entry.kind === 'response')) {
      panel.appendChild(_buildActivityEntryRow(entry));
      return;
    }
    const row = buildToolRow(rowEntry, start + i);
    const rowHead = row.querySelector('.ca-tool-head');
    if (rowHead) {
      rowHead.addEventListener('click', (e) => {
        e.stopPropagation();
        rowEntry.open = !rowEntry.open;
        row.classList.toggle('open', rowEntry.open);
        rowHead.setAttribute('aria-expanded', rowEntry.open ? 'true' : 'false');
        if (rowEntry.open && rowEntry._needsDetail) {
          _ensureToolCallDetail(panel.closest('.bubble-tool-calls'), rowEntry);
        }
      });
    }
    panel.appendChild(row);
    // File cards are user-facing deliverables, not tool-detail content. Keep
    // them visible on the enclosing agent bubble even while this activity panel
    // stays collapsed. Deferred file results must hydrate eagerly—requiring a
    // user to open diagnostic tool detail would defeat that delivery contract.
    // _attachSavedFileCards validates trusted results and de-duplicates by
    // tool-call ID during live reconciliation.
    const isFileDelivery = rowEntry.tool === 'save_file' || rowEntry.tool === 'attach_file';
    if (isFileDelivery && rowEntry._needsDetail) {
      _ensureToolCallDetail(panel.closest('.bubble-tool-calls'), rowEntry);
    }
    if (isFileDelivery) {
      _attachSavedFileCards(panel.closest('.chat-bubble.agent'), [rowEntry]);
    }
  });
}

// Preserve a reader's exact mini-transcript position across row replacement or
// append. Only the explicit activity navigation controls may move this panel.
function _restoreActivityPanelScroll(panel, savedScrollTop) {
  if (!panel || !Number.isFinite(savedScrollTop)) return;
  const maxScrollTop = Math.max(0, panel.scrollHeight - panel.clientHeight);
  panel.scrollTop = Math.min(Math.max(0, savedScrollTop), maxScrollTop);
}

// Add a batch to the one shared tool-call disclosure owned by a bubble. Live
// inference turns arrive separately, but the persisted projection groups their
// calls into one summary after refresh. Reusing the bubble's existing container
// keeps both paths structurally identical while the response is still running.
function _appendCallsToToolContainer(container, calls) {
  if (!container || !calls || calls.length === 0) return false;
  const panel = container.querySelector('.bubble-tool-calls-panel');
  const head = container.querySelector('.bubble-tool-calls-head');
  if (!panel || !head) return false;
  const savedPanelScrollTop = panel.scrollTop;
  if (container.__deferredRows) {
    const known = new Set((container.__calls || []).map(entry => String(entry && (entry.id || entry.toolCallId || ''))));
    const fresh = calls.filter(entry => entry && !known.has(String(entry.id || entry.toolCallId || '')));
    if (fresh.length) container.__calls.push(...fresh);
    panel.__summaryCalls = container.__calls;
    _updateToolCallsHead(head, panel);
    _restoreActivityPanelScroll(panel, savedPanelScrollTop);
    return true;
  }
  const knownEntries = new Set(
    Array.from(panel.querySelectorAll('[data-activity-entry-id]'))
      .map(row => row.dataset.activityEntryId),
  );
  const knownTools = new Set(
    (container.__calls || []).map(c => c && c.toolCallId).filter(Boolean),
  );
  // A durable message id is unique per response/update row. Deduplicate by it
  // too, so a reconcile pass that replays the same durable id (but with a
  // different transient entry key) can never leave two rows for one message.
  const knownDurableMsgIds = new Set(
    Array.from(panel.querySelectorAll('[data-msg-id]'))
      .map(row => row.dataset.msgId)
      .filter(Boolean),
  );
  // A live stream can occasionally miss its first token while the durable DB
  // row still contains the complete text. Reconcile replays the same entry id;
  // update that row in place instead of letting id-deduplication preserve the
  // truncated preview forever.
  let replacedActivityEntry = false;
  for (const entry of calls) {
    if (!entry || !entry.id) continue;
    const key = CSS.escape(String(entry.id));
    const durableKey = entry.id ? CSS.escape(String(entry.id)) : '';
    const existing = panel.querySelector(`[data-activity-entry-id="${key}"]`)
      || (durableKey ? panel.querySelector(`[data-msg-id="${durableKey}"]`) : null);
    if (!existing) continue;
    const prior = existing.__entry || {};
    const contentChanged = _comparableOutput(prior.content) !== _comparableOutput(entry.content);
    const finalityChanged = prior._finalized !== entry._finalized;
    if (!contentChanged && !finalityChanged) continue;
    const replacement = _buildActivityEntryRow(entry);
    existing.replaceWith(replacement);
    replacedActivityEntry = true;
    if (Array.isArray(container.__calls)) {
      const idx = container.__calls.findIndex(item => item && String(item.id || '') === String(entry.id));
      if (idx >= 0) container.__calls[idx] = entry;
    }
  }
  // The collapsed hidden bubble is a clone of the panel row, not the row
  // itself. Rebuild that clone after reconcile swaps a source row, otherwise it
  // can keep showing an early streamed fragment (often the first word).
  if (replacedActivityEntry) _syncToolCallPreviews();
  const fresh = calls.filter(entry => {
    if (!entry) return false;
    if (entry.id && knownEntries.has(String(entry.id))) return false;
    if (entry.id && knownDurableMsgIds.has(String(entry.id))) return false;
    if (entry.toolCallId && knownTools.has(String(entry.toolCallId))) return false;
    return true;
  });
  // Mode and model notices are durable audit rows, but the transcript projects
  // the NET change of one activity group. A new selection supersedes the
  // previous visible row instead of making the disclosure count clicks — and a
  // flip that lands back on the mode/model the group began in cancels the
  // notice outright (ask → plan → auto → ask, or upgrade → revert).
  container.__noticeBase = container.__noticeBase || {};
  const freshNotices = {};
  const cancelledNotices = {};
  NOTICE_TYPES.forEach(notice => {
    const latest = fresh.filter(entry => entry.notice === notice).pop() || null;
    freshNotices[notice] = latest;
    if (!latest) return;
    if (!container.__noticeBase[notice]) {
      const prior = (container.__calls || []).filter(entry => entry?.notice === notice);
      container.__noticeBase[notice] = _noticeKey(
        (prior[0] && prior[0].from) || latest.from || '',
      );
    }
    const base = container.__noticeBase[notice];
    cancelledNotices[notice] = !!base && _noticeKey(latest[notice]) === base;
    panel.querySelectorAll(notice === 'mode' ? '.ca-mode-row' : '.ca-model-row')
      .forEach(row => row.remove());
    if (Array.isArray(container.__calls)) {
      container.__calls = container.__calls.filter(entry => entry?.notice !== notice);
    }
  });
  const projectedFresh = fresh.filter(entry => {
    if (!entry || !NOTICE_TYPES.includes(entry.notice)) return true;
    return !cancelledNotices[entry.notice] && entry === freshNotices[entry.notice];
  });
  if (!projectedFresh.length) {
    // A cancelled notice leaves no row to append, but the disclosure's heading
    // must still drop its "Mode change" / "Model change" part — otherwise the
    // group head keeps advertising a change the group already netted back out.
    if (freshNotices.mode || freshNotices.model) _updateToolCallsHead(head, panel);
    _restoreActivityPanelScroll(panel, savedPanelScrollTop);
    return true;
  }
  _appendToolRows(panel, projectedFresh);
  _updateToolCallsHead(head, panel);
  if (Array.isArray(container.__calls)) container.__calls.push(...projectedFresh);
  else container.__calls = projectedFresh.slice();
  if (projectedFresh.some(c => c && c._needsDetail)) container.__detailLoaded = false;
  if (container.classList.contains('open')) {
    panel.style.maxHeight = Math.min(panel.scrollHeight, _ACTIVITY_PANEL_MAX_PX) + 'px';
  }
  _pinMirrorNoteToBottom(container.closest('.chat-bubble'));
  _restoreActivityPanelScroll(panel, savedPanelScrollTop);
  return true;
}

function _comparableOutput(text) {
  return String(text || '').replace(/\r\n?/g, '\n').trim();
}

// Once a durable Closer arrives, the folded response is only a preview of the
// same output. Keep the activity disclosure and its inspectable panel row, but
// remove the duplicate collapsed preview. Restrict by final interaction id when
// available so two identical short replies in different turns cannot interfere.
function suppressMatchingResponsePreview(content, finalAsstId) {
  if (!app.chatMessages) return false;
  const expected = _comparableOutput(content);
  if (!expected) return false;
  const expectedId = String(finalAsstId || '');
  let suppressed = false;
  app.chatMessages.querySelectorAll('.bubble-tool-calls').forEach(container => {
    const panel = container.querySelector('.bubble-tool-calls-panel');
    const rows = panel ? Array.from(panel.querySelectorAll(':scope > .ca-response-row')) : [];
    const match = rows.find(row => {
      const entry = row.__entry || {};
      if (expectedId && String(entry.id || '') !== expectedId) return false;
      return _comparableOutput(entry.content) === expected;
    });
    if (!match) return;
    container.__closedByCloser = true;
    const head = container.querySelector('.bubble-tool-calls-head');
    if (head && panel) _setPreviewVisible(head, panel, false);
    suppressed = true;
  });
  return suppressed;
}

// ── Lazy tool-call bodies ───────────────────────────────────────────────────
// A transcript loaded from history arrives "light": each call has its heading
// (tool name + duration) but NOT its body (arguments, result, LLM output)
// — those were never downloaded. The first time a panel is opened, fetch the
// full bodies for just that panel's turns and rebuild its rows in place.

// Rebuild a panel's rows from container.__calls, preserving which were expanded.
function _rebuildToolPanel(container) {
  const panel = container.querySelector('.bubble-tool-calls-panel');
  const head = container.querySelector('.bubble-tool-calls-head');
  if (!panel) return;
  const savedPanelScrollTop = panel.scrollTop;
  // JUMP-NAV-OVERFLOW-THRESHOLD: the jump nav is a zero-layout sticky overlay,
  // not a transcript row. It is also the panel's first child, so counting it as
  // row 0 would shift every preserved open-state onto the wrong entry.
  const nav = panel.querySelector(':scope > .bubble-tool-calls-jump-nav');
  const openIdx = new Set();
  Array.from(panel.children).forEach((row, i) => {
    if (row === nav) return;
    if (row.classList && row.classList.contains('open')) openIdx.add(nav ? i - 1 : i);
  });
  const calls = container.__calls || [];
  calls.forEach((c, i) => { if (c) c.open = openIdx.has(i); });
  while (panel.firstChild) panel.removeChild(panel.firstChild);
  // Re-attach the overlay the clear above just removed; losing it would strip
  // the oldest/latest controls from this group for the rest of the session.
  if (nav) panel.appendChild(nav);
  _appendToolRows(panel, calls);
  if (head) _updateToolCallsHead(head, panel);
  if (container.classList.contains('open')) {
    panel.style.maxHeight = Math.min(panel.scrollHeight, _ACTIVITY_PANEL_MAX_PX) + 'px';
  }
  _restoreActivityPanelScroll(panel, savedPanelScrollTop);
}

async function _ensureToolCallDetail(container, call) {
  if (!container || !call || !call._needsDetail || call._detailLoading) return;
  if (!call._detailMsgId && !call._detailToolId) return;
  call._detailLoading = true;
  try {
    const sid = app.currentSessionId;
    const params = new URLSearchParams({ db: 'user.db', session_id: sid });
    if (call._detailToolId) params.set('tool_id', call._detailToolId);
    else {
      params.set('assistant_id', call._detailMsgId);
      params.set('tool_index', String(call._detailIdx || 0));
    }
    if (app.currentUserId) params.set('user_id', app.currentUserId);
    const res = await fetch(apiPath(`/api/v1/db/session-tool-detail?${params}`), {
      headers: authHeaders(),
    });
    if (!res.ok) throw new Error(`Tool detail fetch failed: ${res.status}`);
    const data = await res.json();
    const detail = data && data.detail;
    if (!detail) return;
    const rawArgs = detail.arguments;
    if (typeof rawArgs === 'string') {
      try { call.args = JSON.parse(rawArgs); } catch (_) { call.args = {}; }
    } else call.args = rawArgs || {};
    call.result = detail.content != null ? detail.content : null;
    call._savedToolOutput = detail.output || null;
    call._savedToolMetadata = detail.metadata || call._savedToolMetadata || null;
    call._needsDetail = false;
    _rebuildToolPanel(container);
  } catch (_) {
    // Leave the heading in place; the next row expansion retries.
  } finally {
    call._detailLoading = false;
  }
}

// Height cap of the expanded mini transcript, in CSS pixels. Mirrors
// `.bubble-tool-calls-panel.open { max-height: 250px }` in app1.css — and
// JUMP-NAV-OVERFLOW-THRESHOLD: the jump chevrons stay hidden while the
// content fits under this cap. Change both sides together.
const _ACTIVITY_PANEL_MAX_PX = 250;

function _buildActivityJumpNav(panel) {
  const nav = document.createElement('div');
  nav.className = 'bubble-tool-calls-jump-nav';
  nav.setAttribute('aria-label', 'Activity transcript navigation');

  // Preview slots are siblings after the activity group. Restrict the chevrons
  // to direct panel rows so they can never escape into the full transcript.
  const activityRows = () => Array.from(panel.children)
    .filter(child => child.classList.contains('ca-tool-row'));
  const makeButton = (icon, title, handler) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'bubble-tool-calls-jump-btn';
    button.title = title;
    button.setAttribute('aria-label', title);
    button.innerHTML = `<i data-lucide="${icon}" style="width:15px;height:15px;"></i>`;
    button.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      if (button.disabled) return;
      button.blur();
      button.classList.add('busy');
      button.setAttribute('aria-busy', 'true');
      try {
        handler();
      } finally {
        button.classList.remove('busy');
        button.setAttribute('aria-busy', 'false');
      }
    });
    return button;
  };

  let jumpToStart;
  let jumpToEnd;
  // A newly presented activity group prioritizes its newest information.
  // Reader-driven scrolling away remains the explicit opt-out.
  let followingLatest = true;
  let programmaticScrolls = 0;
  const isAtEnd = () => {
    const maxScrollTop = Math.max(0, panel.scrollHeight - panel.clientHeight);
    return panel.scrollTop >= maxScrollTop - 1;
  };
  // JUMP-NAV-OVERFLOW-THRESHOLD: the chevrons exist only to move a mini
  // transcript that actually overflows its 250px cap. While every row already
  // fits on screen there is nothing to scroll to, so the nav is hidden instead
  // of being shown disabled. Measured from the panel's own content height, so
  // the decision follows the real layout rather than a row count.
  const hasScrollableContent = () => {
    if (panel.hidden || !panel.classList.contains('open')) return false;
    return panel.scrollHeight - panel.clientHeight > 1;
  };
  const refreshNavVisibility = () => {
    const show = hasScrollableContent();
    if (nav.hidden !== !show) nav.hidden = !show;
  };
  const refreshDisabledState = () => {
    refreshNavVisibility();
    const maxScrollTop = Math.max(0, panel.scrollHeight - panel.clientHeight);
    const hasRows = activityRows().length > 0;
    jumpToStart.disabled = !hasRows || maxScrollTop <= 1 || panel.scrollTop <= 1;
    // Keep the follow-latest toggle available even when already at the end.
    // A reader must be able to turn off an active follow state without first
    // moving the mini transcript away from its latest row.
    jumpToEnd.disabled = !hasRows || (maxScrollTop <= 1 && !followingLatest);
  };
  const scrollToBoundary = atEnd => {
    const rows = activityRows();
    if (!rows.length) return;
    const target = atEnd ? rows[rows.length - 1] : rows[0];
    const panelRect = panel.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();
    const targetTop = panel.scrollTop + targetRect.top - panelRect.top;
    programmaticScrolls += 1;
    panel.scrollTop = Math.max(0, atEnd
      ? targetTop + targetRect.height - panel.clientHeight
      : targetTop);
    requestAnimationFrame(() => {
      programmaticScrolls = Math.max(0, programmaticScrolls - 1);
      refreshDisabledState();
    });
  };
  const setFollowingLatest = next => {
    followingLatest = !!next;
    jumpToEnd.classList.toggle('is-following', followingLatest);
    jumpToEnd.setAttribute('aria-pressed', followingLatest ? 'true' : 'false');
    jumpToEnd.title = followingLatest
      ? 'Stop following the latest activity'
      : 'Jump to the end of this activity and follow new entries';
    jumpToEnd.setAttribute('aria-label', jumpToEnd.title);
    if (followingLatest) scrollToBoundary(true);
    else refreshDisabledState();
  };

  jumpToStart = makeButton('chevrons-up', 'Jump to the start of this activity', () => { setFollowingLatest(false); scrollToBoundary(false); });
  jumpToEnd = makeButton('chevrons-down', 'Stop following the latest activity', () => {
    setFollowingLatest(!followingLatest);
  });
  jumpToEnd.classList.add('is-following');
  jumpToEnd.setAttribute('aria-pressed', 'true');
  nav.append(jumpToStart, jumpToEnd);
  nav.__refreshDisabledState = refreshDisabledState;
  nav.__followLatest = () => {
    if (followingLatest) scrollToBoundary(true);
  };
  // A tool row expanding or collapsing its body changes the panel's content
  // height WITHOUT any childList mutation, which is the only other signal this
  // disclosure emits. Observe the direct rows so the chevrons appear the moment
  // the transcript crosses the cap and leave again when it drops back under it.
  const rowObserver = typeof ResizeObserver === 'function'
    ? new ResizeObserver(() => refreshDisabledState()) : null;
  nav.__syncRowObservation = () => {
    if (!rowObserver) return;
    activityRows().forEach(row => {
      if (row.__jumpNavObserved) return;
      row.__jumpNavObserved = true;
      rowObserver.observe(row);
    });
  };
  panel.addEventListener('scroll', () => {
    // A direct reader scroll away from the latest row releases the follow
    // state. Programmatic pinning deliberately emits the same scroll event.
    if (followingLatest && !programmaticScrolls && !isAtEnd()) {
      setFollowingLatest(false);
      return;
    }
    refreshDisabledState();
  }, { passive: true });
  // Wait until the rows and their container are laid out, then begin at
  // the same latest entry the down chevron targets.
  requestAnimationFrame(() => {
    nav.__syncRowObservation();
    if (followingLatest) scrollToBoundary(true);
    else refreshDisabledState();
  });
  // Establish the threshold state synchronously for a rebuilt panel that
  // already holds overflowing rows, instead of waiting for the observer tick.
  nav.hidden = true;
  nav.__syncRowObservation();
  refreshDisabledState();
  _refreshLucideIcons(nav);
  return nav;
}

// Mode/model notices net per activity group. A group's flips are measured
// against the state the group BEGAN in, so ask → plan → auto → ask (or upgrade →
// revert) cancels the group's notice and leaves that group exactly as it was
// before the first flip, while a group that ends somewhere else shows ONE
// "Mode change" / "Model change" row no matter how many flips got there. `from`
// records the state before each flip (live anchor, or the durable row's
// metadata) and the notice's own field (`mode` / `model`) the state it landed
// on; a group whose surviving rows predate `from` simply never cancels.
// Normalise a netting key. Case-insensitive, and every spelling of "the default
// model role" collapses to the same identity, so a pick that returns to the
// session's default still reads as a round trip and cancels.
function _noticeKey(value) {
  const key = String(value == null ? '' : value).trim().toLowerCase();
  return key === 'role:default' ? 'default' : key;
}

function _projectNotices(entries, notice) {
  const notices = (entries || []).filter(entry => entry && entry.notice === notice);
  if (!notices.length) return { notices, base: '', cancelled: false };
  const base = _noticeKey(notices[0].from);
  const current = _noticeKey(notices[notices.length - 1][notice]);
  return { notices, base, cancelled: !!(base && current && base === current) };
}

function _projectModeNotices(entries) { return _projectNotices(entries, 'mode'); }

function _projectModelNotices(entries) { return _projectNotices(entries, 'model'); }

// Build the "N tool calls" heading and initially collapsed panel for a bubble.
// Live events update the compact head only; activity details open solely after a
// user disclosure click and remain available for inspection afterward.
function _buildToolCallsContainer(calls, options) {
  options = options || {};
  const deferRows = options.deferRows === true;
  // Persisted groups preserve their established initial presentation. A live
  // shell is different: it must stay summary-only until the user opens it.
  const initiallyCollapsed = deferRows || options.collapsed === true;
  const container = document.createElement('div');
  container.className = 'bubble-tool-calls';
  // Mode and model notices are all retained in durable storage, but a transcript
  // group represents the NET change of that group. Project the newest notice of
  // each type on first construction — or none at all when the group's flips
  // netted back to where it began; live appends use the matching rule in
  // _appendCallsToToolContainer. NOTICE_TYPES mirrors _buildActivityEntryRow's
  // notice classes and the MODE/MODEL_NOTICE_LABEL constants.
  const sourceCalls = Array.isArray(calls) ? calls : [];
  container.__noticeBase = {};
  let projectedCalls = sourceCalls;
  NOTICE_TYPES.forEach(notice => {
    const plan = _projectNotices(sourceCalls, notice);
    // The mode/model the group began in — the base later flips net against.
    container.__noticeBase[notice] = plan.base;
    const latest = plan.cancelled ? null : plan.notices.pop();
    projectedCalls = projectedCalls.filter(entry => {
      if (!entry || entry.notice !== notice) return true;
      return latest ? entry === latest : false;
    });
  });
  // Keep the call objects so the bodies can be lazy-filled on first open.
  container.__calls = projectedCalls.slice();
  // Build stamp — the newest-built container owns the collapsed preview.
  container.__previewSeq = ++_previewGroupSeq;

  const head = document.createElement('button');
  head.type = 'button';
  head.className = 'bubble-tool-calls-head';
  head.setAttribute('aria-expanded', initiallyCollapsed ? 'false' : 'true');
  container.appendChild(head);

  const panel = document.createElement('div');
  panel.className = 'bubble-tool-calls-panel';
  panel.hidden = initiallyCollapsed;
  if (!initiallyCollapsed) panel.classList.add('open');
  container.appendChild(panel);
  // The nav stays structurally inside the expanded activity group, while its
  // zero-layout sticky CSS overlay keeps it out of the tool-row reading order.
  const activityJumpNav = _buildActivityJumpNav(panel);
  panel.__activityJumpNav = activityJumpNav;
  panel.appendChild(activityJumpNav);

  panel.__summaryCalls = deferRows ? projectedCalls : null;
  panel.__summaryCounts = deferRows ? (options.summaryCounts || null) : null;
  container.__deferredRows = deferRows;
  container.__loadDeferredEntries = typeof options.loadEntries === 'function' ? options.loadEntries : null;
  container.__deferredLoading = false;
  if (!deferRows) _appendToolRows(panel, projectedCalls);
  _updateToolCallsHead(head, panel);
  // Deferred and live activity start closed. Persisted, fully present history
  // retains its established initial state; deferred details fetch only on click.
  // Reconcile, lazy detail hydration, and live coalescing can all replace or
  // append disclosure rows asynchronously. Observe the authoritative panel so
  // the collapsed counter can never lag behind what opening Activity reveals.
  let counterSyncQueued = false;
  const counterObserver = new MutationObserver(() => {
    if (counterSyncQueued) return;
    counterSyncQueued = true;
    queueMicrotask(() => {
      counterSyncQueued = false;
      _updateToolCallsHead(head, panel);
      panel.__activityJumpNav?.__syncRowObservation?.();
      panel.__activityJumpNav?.__followLatest?.();
      panel.__activityJumpNav?.__refreshDisabledState?.();
    });
  });
  counterObserver.observe(panel, { childList: true });
  container.__counterObserver = counterObserver;

  // Activity stays open while work is in progress. Once a closer collapses a
  // completed group, a reader can reopen it without affecting other groups.
  head.addEventListener('click', async () => {
    if (panel.hidden && container.__loadDeferredEntries && container.__deferredRows) {
      if (container.__deferredLoading) return;
      container.__deferredLoading = true;
      head.setAttribute('aria-busy', 'true');
      try {
        const entries = await container.__loadDeferredEntries();
        if (!Array.isArray(entries)) throw new Error('Invalid activity details');
        const displayEntries = entries.length ? entries : [{ kind: 'system', notice: 'activity', label: 'No displayable activity details', content: '', counted: false }];
        container.__calls = displayEntries.slice();
        panel.__summaryCalls = null;
        container.__deferredRows = false;
        container.__loadDeferredEntries = null;
        _appendToolRows(panel, displayEntries);
        _updateToolCallsHead(head, panel);
        _setActivityGroupExpanded(container, true);
      } catch (_) {
        // Keep summary state intact so a later click safely retries without
        // showing a misleading empty activity disclosure.
        head.setAttribute('aria-label', 'Could not load activity details. Retry.');
      } finally {
        container.__deferredLoading = false;
        head.removeAttribute('aria-busy');
      }
      return;
    }
    _setActivityGroupExpanded(container, panel.hidden);
  });

  return container;
}

function _addToolCallsToBubble(bubble, calls, options) {
  options = options || {};
  // Cards are first-class deliverables and must survive tool-only turns, whose
  // bubble is created immediately before this helper runs.
  _attachSavedFileCards(bubble, calls);
  // A response may span several streamed inference turns. They all belong to
  // the same agent bubble, so extend its existing disclosure instead of adding
  // one "N tool calls" line per turn.
  const existing = bubble.querySelector(':scope > .turn-section.tool-section .bubble-tool-calls');
  if (existing && _appendCallsToToolContainer(existing, calls)) {
    // A durable snapshot may arrive after the provisional collapsed shell was
    // built. Its counters are authoritative and must replace the shell's
    // initial one-row estimate before returning through this fast path.
    if (options.summaryCounts && existing.__deferredRows) {
      const panel = existing.querySelector(':scope > .bubble-tool-calls-panel');
      const head = existing.querySelector(':scope > .bubble-tool-calls-head');
      if (panel && head) {
        panel.__summaryCounts = options.summaryCounts;
        _updateToolCallsHead(head, panel);
      }
    }
    _pinMirrorNoteToBottom(bubble);
    return;
  }

  // Wrap tool calls in a turn-section and append to the bubble, before any
  // existing footer (which will be removed by _addBubbleActions).
  const section = document.createElement('div');
  section.className = 'turn-section tool-section';
  const container = _buildToolCallsContainer(calls, options);
  section.appendChild(container);
  bubble.appendChild(section);
  _pinMirrorNoteToBottom(bubble);
  // The new container is now the most recent grouping — it takes the preview
  // and any older grouping's preview is removed.
  _syncToolCallPreviews();

  // Tool-only groups are disclosures, not empty LLM responses. Remove their
  // placeholder section and any normal response gutter it created.
  if (bubble.classList.contains('tool-only')) {
    bubble.querySelectorAll(':scope > .turn-section.llm-section').forEach(llm => {
      if (!llm.textContent.trim()) {
        const gutter = llm.nextElementSibling;
        if (gutter && gutter.classList.contains('turn-gutter')) gutter.remove();
        llm.remove();
      }
    });
  }

  // Wire the preceding LLM section if it hasn't been wired yet
  const llmSections = bubble.querySelectorAll(':scope > .turn-section.llm-section');
  const lastLLM = llmSections[llmSections.length - 1];
  if (lastLLM && typeof app._wireLLMSection === 'function') {
    app._wireLLMSection(lastLLM, bubble);
  }
}

function _activityGroupIsLiveTail(group) {
  // A group is only the current run's open activity shell when it sits AFTER the
  // last user row. The moment a new user message is appended, any group before
  // that row is a closed prior run and must never receive new-run counters,
  // tools, modes, or compactions (immutable prior-run counters). A mid-run
  // OPENING closes the run's own earlier phase the same way: work published after
  // the opening is a new disclosure, so a group that precedes an opening is never
  // the live tail either.
  if (!app.chatMessages || !group || !group.isConnected) return false;
  const users = app.chatMessages.querySelectorAll(':scope > .chat-bubble.user');
  if (!users.length) return true;
  const lastUser = users[users.length - 1];
  const owner = String(group.dataset.activityTurnId || '');
  let node = lastUser.nextElementSibling;
  while (node) {
    if (node === group) return true;
    if (isMidRunOpeningEl(node)) {
      const stamped = openingOwnerOf(node);
      if (!owner || !stamped || String(stamped) === owner) return false;
    }
    node = node.nextElementSibling;
  }
  return false;
}

// Live activity is owned by the user turn, never by a single assistant step.
// Keep that identity outside the DOM so reconcile adoption cannot split one run.
// Historical groups never enter this registry and keep their saved boundaries.
const _liveActivityGroups = new Map();

function _liveActivityGroupKey(ownerTurnId, sessionId = app.currentSessionId) {
  const owner = String(ownerTurnId || '').trim();
  const session = String(sessionId || '').trim();
  return owner && session ? session + '::' + owner : '';
}

function _rememberLiveActivityGroup(ownerTurnId, bubble) {
  const key = _liveActivityGroupKey(ownerTurnId);
  if (!key || !bubble || !bubble.isConnected) return bubble || null;
  _liveActivityGroups.set(key, bubble);
  return bubble;
}

function _liveActivityGroupForTurn(ownerTurnId) {
  const key = _liveActivityGroupKey(ownerTurnId);
  if (!key) return null;
  const bubble = _liveActivityGroups.get(key);
  if (!bubble || !bubble.isConnected || !app.chatMessages
      || bubble.parentElement !== app.chatMessages) {
    _liveActivityGroups.delete(key);
    return null;
  }
  return bubble;
}

function _clearLiveActivityGroupsForSession(sessionId = app.currentSessionId) {
  const session = String(sessionId || '').trim();
  if (!session) return;
  const prefix = session + '::';
  for (const key of _liveActivityGroups.keys()) {
    if (key.startsWith(prefix)) _liveActivityGroups.delete(key);
  }
}

// Retire ONE owner's live-group registration without touching its DOM. A mid-run
// opening ends the phase that group belonged to, so the next live lookup must not
// re-adopt it — re-adoption is exactly what would drag the post-opening work back
// up above the opening. Historical groups never enter the registry, so this can
// only ever forget a still-open group.
function _forgetLiveActivityGroup(ownerTurnId) {
  const key = _liveActivityGroupKey(ownerTurnId);
  if (key) _liveActivityGroups.delete(key);
}

window.addEventListener('webagent-loop-session-changed', () => {
  _liveActivityGroups.clear();
  // The sheen belongs to one run in one session: disarm the rule so nothing can
  // re-light on the way out, then clear any class the outgoing transcript held.
  _setLiveActivityArmed(false);
  if (app.chatMessages) {
    const marked = app.chatMessages.querySelectorAll(
      '.chat-bubble.' + _LIVE_ACTIVITY_CLS,
    );
    for (const b of marked) b.classList.remove(_LIVE_ACTIVITY_CLS);
  }
});

function _activityGroupForTurn(ownerTurnId) {
  if (!app.chatMessages) return null;
  const registered = _liveActivityGroupForTurn(ownerTurnId);
  if (registered) return registered;
  const active = app._activeToolGroupBubble;
  if (!ownerTurnId && active && active.isConnected && _activityGroupIsLiveTail(active)) return active;
  if (ownerTurnId) {
    // A mid-run OPENING closes the phase above it (activityGroupPrecedesOpening).
    // That group is finished: re-adopting it as the live group is exactly what
    // drags the run's post-opening work back ABOVE the opening and leaves the
    // opening stranded at the tail with no activity line after it — so a closed
    // phase is dropped here and the next entry opens a fresh disclosure below.
    const groups = Array.from(app.chatMessages.querySelectorAll(
      `.chat-bubble.activity-group[data-activity-turn-id="${CSS.escape(String(ownerTurnId))}"]`,
    )).filter(group => !group.dataset.activitySegmentId
      && !activityGroupPrecedesOpening(group, ownerTurnId));
    // DB-tail may materialize the current durable segment before the matching
    // live response arrives. Adopt that open segment by owner so the live event
    // cannot create a second shell containing the same response.
    const persisted = _persistedActivityGroupForOwner(String(ownerTurnId));
    if (persisted && !groups.includes(persisted)) groups.push(persisted);
    if (active && active.isConnected
        && active.dataset.activityTurnId === String(ownerTurnId)
        && !activityGroupPrecedesOpening(active, ownerTurnId)
        && !groups.includes(active)) groups.push(active);
    groups.sort((a, b) => {
      if (a === b) return 0;
      return a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING ? -1 : 1;
    });
    // A run owns exactly one activity bubble. Reconcile can briefly materialise
    // more than one shell when backend step ids or persisted segment ids differ;
    // fold all of them into the earliest shell before returning it.
    if (groups.length) {
      const group = _coalesceActivityGroups(groups);
      return _rememberLiveActivityGroup(ownerTurnId, group);
    }
    // A provisional group holding ONLY a pre-send mode/model notice was just
    // re-anchored behind the newest prompt (adoptPreSendNoticeGroup). Adopt it as
    // this turn's disclosure — restamping it with the durable owner — instead of
    // creating a second group beside it, which would strand the notice outside
    // the run it belongs to. Never adopts a group that already owns work.
    if (active && active.isConnected && !active.dataset.activitySegmentId
        && active.querySelector('.ca-mode-row, .ca-model-row')
        && !active.querySelector('.ca-tool-row:not(.ca-system-row)')) {
      const prev = active.previousElementSibling;
      if (prev && prev.classList && prev.classList.contains('chat-bubble')
          && prev.classList.contains('user')) {
        // Owned by the prompt it follows (adoptPreSendNoticeGroup stamps the
        // PROMPT's id) or not owned yet — either way this run's first live entry
        // claims it, so a pre-send notice lives inside its run's disclosure
        // instead of stranded in a group of its own. An existing prompt stamp is
        // KEPT: the ordering authority relocates a group to owner + 1 by matching
        // that stamp against the prompt row, so overwriting it here would let the
        // createdAt/render-order sort hoist the notice above the message again.
        const promptId = String(prev.dataset.msgId || prev.dataset.turnId || '');
        if (!String(active.dataset.activityTurnId || '')) {
          active.dataset.activityTurnId = String(promptId || ownerTurnId);
        }
        return _rememberLiveActivityGroup(ownerTurnId, active);
      }
    }
  }
  return null;
}

// The run buffer's turn id is the owning USER interaction id. Use that durable
// relationship directly instead of letting a provisional timestamp/render
// order place the activity group before its prompt.
function positionActivityGroupAfterOwner(ownerTurnId, bubble) {
  if (!app.chatMessages) return bubble || null;
  const owner = ownerTurnId ? String(ownerTurnId) : '';
  const group = bubble || _activityGroupForTurn(owner);
  if (!ownerTurnId) {
    // An ownerless provisional live-activity shell belongs to the current run.
    // Place it immediately after the most recent user row so it can never
    // appear before its owner, and rekey it to that owner so a later durable
    // shell adopts it instead of rendering a second group.
    if (!group || !group.isConnected) return group || null;
    if (group.classList.contains(_LIVE_ACTIVITY_CLS)) {
      const users = app.chatMessages.querySelectorAll(':scope > .chat-bubble.user');
      if (users.length) {
        const lastUser = users[users.length - 1];
        const lastOwner = lastUser.dataset.msgId || lastUser.dataset.turnId || '';
        if (lastOwner) group.dataset.activityTurnId = String(lastOwner);
        if (group !== lastUser.nextElementSibling) {
          lastUser.insertAdjacentElement('afterend', group);
        }
      }
    }
    return group;
  }
  const user = app.chatMessages.querySelector(
    `.chat-bubble.user[data-msg-id="${CSS.escape(owner)}"], `
      + `.chat-bubble.user[data-turn-id="${CSS.escape(owner)}"]`,
  );
  const scout = app.chatMessages.querySelector(
    `.chat-bubble.agent.scout-preview[data-scout-owner="${CSS.escape(owner)}"]`,
  );
  const anchor = scout || user;
  if (!user || !group || !group.isConnected || group === anchor.nextElementSibling) return group;
  anchor.insertAdjacentElement('afterend', group);
  return group;
}

function rekeyActivityGroupOwner(provisionalTurnId, durableTurnId) {
  if (!app.chatMessages || !provisionalTurnId || !durableTurnId) return null;
  const oldOwner = CSS.escape(String(provisionalTurnId));
  const groups = Array.from(app.chatMessages.querySelectorAll(
    `.chat-bubble.activity-group[data-activity-turn-id="${oldOwner}"]`,
  ));
  let last = null;
  for (const group of groups) {
    group.dataset.activityTurnId = String(durableTurnId);
    last = positionActivityGroupAfterOwner(durableTurnId, group);
  }
  return last;
}

function _coalesceActivityGroups(groups) {
  const connected = Array.from(groups || []).filter(group => group && group.isConnected);
  if (!connected.length) return null;
  // Prefer the durable shell so its segment identity survives future DB-tail
  // passes; otherwise reconcile can recreate the segment we just removed.
  const primary = connected.find(group => group.dataset.activitySegmentId) || connected[0];
  for (const extra of connected.filter(group => group !== primary)) {
    const entries = Array.from(extra.querySelectorAll('.bubble-tool-calls'))
      .flatMap(container => Array.isArray(container.__calls) ? container.__calls : []);
    if (entries.length) {
      const container = primary.querySelector('.bubble-tool-calls');
      if (!container) {
        _addToolCallsToBubble(primary, entries);
      } else {
        _appendCallsToToolContainer(container, entries);
      }
    }
    const note = _mirrorNoteRow(extra);
    if (note && !_mirrorNoteRow(primary)) primary.appendChild(note);
    extra.remove();
  }
  app._activeToolGroupBubble = primary;
  return primary;
}

function _coalesceLiveActivityGroups() {
  if (!app.chatMessages) return null;
  const users = app.chatMessages.querySelectorAll(':scope > .chat-bubble.user');
  const boundary = users.length ? users[users.length - 1] : null;
  if (!boundary) {
    const active = app._activeToolGroupBubble;
    return active && active.isConnected ? active : null;
  }
  const groups = [];
  // A phase an opening has already closed keeps its own slot ABOVE that opening:
  // folding it into the live disclosure would merge the run's two phases into the
  // group above the opening, strand the opening at the tail, and hang the closer
  // above it. Only the run's CURRENT phase (anything after the newest opening) is
  // ever coalesced.
  const boundaryOwner = boundary.dataset
    ? (boundary.dataset.msgId || boundary.dataset.turnId || '') : '';
  let node = boundary.nextElementSibling;
  while (node) {
    if (node.classList && node.classList.contains('activity-group')
        && !activityGroupPrecedesOpening(node, boundaryOwner)) groups.push(node);
    node = node.nextElementSibling;
  }
  if (!groups.length) return null;

  // The first disclosure owns the chronological position. Fold every later
  // live disclosure into it in DOM order, then remove the redundant shells.
  const primary = _coalesceActivityGroups(groups);
  const boundaryId = boundary && (
    boundary.dataset.msgId || boundary.dataset.turnId || boundary.dataset.renderOrder
  );
  if (boundaryId) primary.dataset.activityBoundaryId = String(boundaryId);
  app._activeToolGroupBubble = primary;
  return primary;
}

function _persistedActivityGroupForOwner(owner) {
  if (!owner || !app.chatMessages) return null;
  const groups = Array.from(app.chatMessages.querySelectorAll(
    '.chat-bubble.activity-group',
  )).filter(group => group.parentElement === app.chatMessages);
  for (let i = groups.length - 1; i >= 0; i--) {
    const candidate = groups[i];
    if (candidate.dataset.activityTurnId !== String(owner)) continue;
    let node = candidate.nextElementSibling;
    let blocked = false;
    while (node) {
      const userBoundary = node.classList?.contains('user');
      const boundaryId = userBoundary
        ? (node.dataset.msgId || node.dataset.turnId || '') : '';
      const foldedNotice = node.classList?.contains('system-model')
        || node.classList?.contains('system-mode')
        || node.classList?.contains('system-compaction-transient');
      const infoBoundary = node.classList?.contains('info') && !foldedNotice;
      // Model, mode, and compaction notices belong to the active disclosure.
      // Other standalone system notices remain chronological boundaries. A mid-run
      // OPENING is a boundary too: the phase ABOVE it is finished, so a persisted
      // segment arriving after the opening must never be adopted into it — that
      // merge is what puts the post-opening work (and its closer) above the plan.
      if ((userBoundary && boundaryId !== String(owner))
          || isMidRunOpeningEl(node)
          || node.classList?.contains('summary-bubble')
          || infoBoundary) {
        blocked = true;
        break;
      }
      node = node.nextElementSibling;
    }
    if (!blocked) return candidate;
  }
  return null;
}

// Exact per-run key (owner turn id + leading durable seq) shared by the builder
// (session-load.js) and the live WS path. Both must produce IDENTICAL keys so a
// group and its closer match without an ambiguous nearest/current fallback.
const _runKey = (ownerTurnId, leadingSeq) => {
  const o = (ownerTurnId == null ? '' : String(ownerTurnId)).trim();
  const s = (leadingSeq == null ? '' : String(leadingSeq)).trim();
  return o ? (o + '::' + s) : '';
};

// The run's leading durable sequence: the earliest interaction_seq across the
// rows being attached (falling back to the anchor's interaction_seq). Used only
// to DERIVE a run key when no exact key was supplied.
const _leadingDurableSeq = (entries, anchor) => {
  const seqs = (entries || [])
    .map(e => Number(e && e.interactionSeq))
    .filter(Number.isFinite);
  if (seqs.length) return String(Math.min(...seqs));
  const anchorSeq = anchor && anchor.interactionSeq;
  return anchorSeq != null ? String(anchorSeq) : '';
};

function applyDurableActivitySummary(summary, event = {}) {
  if (!summary?.group_id || !app.chatMessages) return null;
  const groupId = String(summary.group_id);
  const owner = String(summary.owner_turn_id || event.turn_id || '');
  const escaped = CSS.escape(groupId);
  const existing = app.chatMessages.querySelector(
    `.chat-bubble.activity-group[data-activity-segment-id="${escaped}"]`,
  );
  const loadEntries = async () => {
    const token = localStorage.getItem('auth_token');
    let url = apiPath(`/api/v1/db/session-activity-group-detail?db=user.db&session_id=${encodeURIComponent(app.currentSessionId || summary.session_id || '')}&activity_group_id=${encodeURIComponent(groupId)}`);
    if (token) url += `&token=${encodeURIComponent(token)}`;
    const res = await fetch(url, { cache: 'no-store', headers: authHeaders() });
    if (!res.ok) throw new Error('Activity detail request failed (' + res.status + ')');
    const data = await res.json();
    if (data.restricted) throw new Error('Activity detail is restricted');
    return Array.isArray(data.entries) ? data.entries : [];
  };
  const bubble = attachActivityEntries(
    [{ kind: 'activity-summary', id: event.id || groupId, interactionSeq: Number(summary.start_seq) }],
    existing,
    {
      id: event.id || groupId,
      activityGroupId: groupId,
      createdAt: event.created_at || event.emit_time,
      turnId: owner,
      interactionSeq: Number(summary.start_seq),
      summaryCounts: summary.summary_counts || {},
      deferEntries: true,
      loadEntries,
    },
    owner,
    groupId,
  );
  return bubble;
}

function attachActivityEntries(entries, targetBubble, anchor, ownerTurnId, runKey) {
  if (!entries || !entries.length || !app.chatMessages) return null;
  const owner = ownerTurnId || (anchor && anchor.turnId) || '';
  const anchorId = anchor && (anchor.id || anchor.asstId);
  const activityId = (anchor && anchor.activityGroupId)
    || ('activity-' + (anchorId || owner || ''));
  const persistedSegment = !!(anchor && anchor.activityGroupId);
  let bubble = targetBubble;
  const registeredLiveGroup = !persistedSegment && owner ? _liveActivityGroupForTurn(owner) : null;
  if (!bubble && registeredLiveGroup) bubble = registeredLiveGroup;
  // Persisted history already carries a boundary-safe segment id. Resolve that
  // exact shell before considering live owner-turn coalescing: one user turn can
  // contain several persisted phases separated by closer/summary rows.
  if (!bubble && activityId !== 'activity-') {
    const key = CSS.escape(String(activityId));
    bubble = app.chatMessages.querySelector(
      '.chat-bubble.activity-group[data-msg-id="' + key + '"], '
        + '.chat-bubble.activity-group[data-turn-id="' + key + '"]',
    );
  }
  if (!bubble && persistedSegment && anchorId) {
    // Reconcile can receive a row just after its live WS projection was drawn.
    // Adopt that provisional shell by its durable entry id, then stamp the
    // boundary-safe persisted segment id. Creating a second shell here leaves
    // one zero-tool "live" copy beside the canonical persisted disclosure.
    const entryKey = CSS.escape(String(anchorId));
    const liveEntry = app.chatMessages.querySelector(
      '.chat-bubble.activity-group:not([data-activity-segment-id]) '
        + `[data-activity-entry-id="${entryKey}"]`,
    );
    const liveBubble = liveEntry && liveEntry.closest('.chat-bubble.activity-group');
    if (liveBubble) {
      bubble = liveBubble;
      bubble.dataset.msgId = String(activityId);
      bubble.dataset.turnId = String(activityId);
    }
  }
  if (!bubble && !persistedSegment) bubble = _activityGroupForTurn(owner);
  if (!bubble && !persistedSegment) bubble = _coalesceLiveActivityGroups();
  if (!bubble && !persistedSegment) {
    // Single-run invariant: adopt the current run's provisional live-activity
    // shell before creating a durable one, so one run never renders two groups.
    let liveMirror = null;
    try { liveMirror = _mirrorBubble(String(app.currentSessionId || '')); } catch (_) {}
    if (liveMirror && liveMirror.isConnected && !liveMirror.dataset.activitySegmentId) {
      const mirrorOwner = liveMirror.dataset.activityTurnId || '';
      if (!mirrorOwner || (owner && mirrorOwner === String(owner))) {
        bubble = liveMirror;
        if (owner) bubble.dataset.activityTurnId = String(owner);
        if (activityId && activityId !== 'activity-') {
          bubble.dataset.msgId = String(activityId);
          bubble.dataset.turnId = String(activityId);
        }
      }
    }
  }
  if (!bubble) {
    const newActivityId = activityId === 'activity-' ? 'activity-' + Date.now() : activityId;
    bubble = addChatBubble(
      'agent', '', 'tool-only activity-group', undefined,
      newActivityId, newActivityId, anchor && anchor.createdAt,
    );
  }
  bubble.classList.add('activity-group', 'tool-only');
  // A pre-send mode/model notice group is owned by the PROMPT it was re-anchored
  // behind (adoptPreSendNoticeGroup). Keep that stamp: the transcript's ordering
  // authority places an owned provisional group at owner + 1 by matching the
  // stamp against the prompt row, so overwriting it with the run's durable id
  // would orphan the group and hoist the notice above the message it belongs to.
  const noticeOnlyStamp = !!(bubble.dataset.activityTurnId
    && bubble.querySelector('.ca-mode-row, .ca-model-row')
    && !bubble.querySelector('.ca-tool-row:not(.ca-system-row)'));
  if (owner && !noticeOnlyStamp) bubble.dataset.activityTurnId = String(owner);
  if (owner && !persistedSegment) {
    _rememberLiveActivityGroup(owner, bubble);
  }
  // Exact per-run key: carry owner turn id + the run's leading durable seq so a
  // group/closer can be matched without an ambiguous nearest/current fallback.
  // Prefer the explicit runKey (live WS path); fall back to the builder's
  // anchor.runKey; derive from owner+leading seq only as a last resort. Never
  // overwrite an already-stamped exact key with a looser/derived one.
  const resolvedRunKey = runKey
    || (anchor && anchor.runKey)
    || (owner && _runKey(owner, _leadingDurableSeq(entries, anchor)));
  if (resolvedRunKey) {
    if (!bubble.dataset.runKey) bubble.dataset.runKey = String(resolvedRunKey);
  }
  if (persistedSegment && !bubble.dataset.activitySegmentId) {
    bubble.dataset.activitySegmentId = String(activityId);
  }
  // Live path: stamp the group with the running turn's model so each update
  // row's ⋮ menu can show it. Persisted projections are deliberately NOT
  // stamped here — app._activeTurnModel describes the CURRENT run, which would
  // mislabel older update rows (their menu reads per-message metadata instead).
  if (!persistedSegment && app._activeTurnModel) {
    try { _setBubbleModel(bubble, app._activeTurnModel, app._activeTurnEffort, app._activeTurnDisplayName); } catch (_) {}
  }
  app._activeToolGroupBubble = bubble;
  _addToolCallsToBubble(bubble, entries, {
    collapsed: !persistedSegment,
    deferRows: persistedSegment && !!(anchor && anchor.deferEntries),
    summaryCounts: anchor && anchor.summaryCounts,
    loadEntries: anchor && anchor.loadEntries,
  });
  if (!persistedSegment) {
    _startActivityElapsed(bubble, _earliestActivityStartedAt(entries, anchor));
  }
  _anchorToolBubble(bubble, anchor);
  if (persistedSegment) {
    const durableEntrySeqs = entries
      .map(entry => Number(entry && entry.interactionSeq))
      .filter(Number.isFinite);
    if (durableEntrySeqs.length) {
      // The persisted segment itself, not its owning user/turn, determines its
      // durable slot. This matters when one user request contains multiple run
      // phases: stamping both groups with the owner's first sequence reverses
      // later phases on reload and lets their timestamps disagree with the DB.
      const currentSeq = Number(bubble.dataset.sessionSeq);
      const entrySeq = Math.min(...durableEntrySeqs);
      _setBubbleSessionSeq(
        bubble,
        Number.isFinite(currentSeq) ? Math.min(currentSeq, entrySeq) : entrySeq,
      );
    }
  }
  // Durable persisted rows are positioned solely by session_seq. Moving each
  // segment directly after its user would jump later phases across their closer.
  if (!persistedSegment) positionActivityGroupAfterOwner(owner, bubble);
  return bubble;
}

function attachProgressToActivity(content, asstId, createdAt, ownerTurnId, interactionSeq) {
  const text = (content || '').trim();
  const draft = asstId ? _findAgentBubbleForTurn(asstId) : null;
  if (!text) {
    if (draft && draft.classList.contains('streaming')) draft.remove();
    return null;
  }
  const anchor = {
    id: asstId,
    createdAt,
    turnId: ownerTurnId,
    interactionSeq: Number(interactionSeq),
  };
  const carried = draft
    ? Array.from(draft.querySelectorAll('.bubble-tool-calls'))
      .flatMap(container => Array.isArray(container.__calls) ? container.__calls : [])
    : [];
  // agent_step_end is the commit boundary: only its complete payload may
  // become a preview/counter message. Earlier stream chunks remain drafts.
  const entries = [{
    kind: 'progress', id: asstId, content: text, createdAt,
    interactionSeq: Number(interactionSeq), _finalized: true,
  }, ...carried];
  if (draft && app._activeToolGroupBubble === draft) {
    app._activeToolGroupBubble = null;
  }
  const group = attachActivityEntries(
    entries,
    null,
    anchor,
    ownerTurnId,
    _runKey(ownerTurnId, interactionSeq),
  );
  // Mark the run's live activity group so its transcript summary line shows the
  // "still updating" sheen (`.live-activity` → app1.css). This must NOT depend on
  // the transcript mirror: mirrorActivityNote is gated on the
  // mirror_activity_in_transcript flag, and with that off NOTHING else ever added
  // the class — so the summary line stayed visually settled for the whole run and
  // no CSS trigger keyed on liveness could ever match in the normal chat panel.
  // Run-end cleanup is already unconditional (mirrorActivityEnd strips the class
  // from every group that holds tool calls), and this function is reached only
  // from the live step-publish path, so replayed history never gains the class.
  // Singleton marker: a late step commit landing behind a newer user row must not
  // relight its own finished summary — it is no longer the live tail, so the
  // marker skips it and sweeps any class it still carried.
  _markLiveActivityGroup(group);
  if (draft && draft !== group) draft.remove();
  if (asstId) _wsTurnBuffers.delete(asstId);
  return group;
}

// ── Live-activity mirror (mirror_activity_in_transcript) ────────────────────
// The progress text shown above the composer pill (activity bar) is mirrored
// into the transcript as an agent bubble that also carries this turn's tool
// calls and update rows. The bar is untouched — this is a deliberate duplicate.
// The mirror bubble IS the live activity group (app._activeToolGroupBubble),
// so the standard tool-attach paths (attachToolCallsToLastBubble /
// attachActivityEntries) land this turn's calls and updates inside it, and
// every row opens exactly like the other tool-call disclosures. The note row
// disappears at turn end; the bubble persists only if it holds calls/updates.

const _LIVE_ACTIVITY_CLS = 'live-activity';

// ── ONE live summary line per session ───────────────────────────────────────
// The sheen is a visual claim that a line is STILL CHANGING, so at most one
// activity summary line may carry it, and it must be the newest one. This is an
// INVARIANT of the transcript, not a property of any single call site: the class
// is re-derived from document order after every mutation instead of being left
// to whoever marked last. Three earlier failure modes it closes:
//   • a mark that bailed out because its freshly created group was not yet
//     connected, leaving the previous line lit beside it;
//   • a late step commit re-lighting a superseded group behind a newer prompt;
//   • a stray .live-activity-note row lighting an OLDER chip through the second
//     CSS trigger (`:has(> .live-activity-note)`) after a missed teardown.
// Liveness is the arm flag, not app.isProcessing: a run only ever produces a
// line through the live mark paths, so "was marked, not yet retired" is exactly
// the old (correct) condition — and it cannot re-light a finished line during
// the run-end race, when isProcessing may still be true.
let _liveActivityArmed = false;
let _liveSheenTarget = null;
let _liveSheenObserver = null;
let _liveSheenSyncQueued = false;
let _requestedLiveActivityGroup = null;

function _setLiveActivityArmed(on) {
  _liveActivityArmed = !!on;
}

// The ONE line allowed to glimmer: the last activity group that sits AFTER the
// newest user row. Resolved by document position, never by turn id — a group
// above the newest prompt belongs to a closed run and can never reclaim it.
function _liveTailActivityGroup() {
  if (!app.chatMessages) return null;
  const groups = Array.from(
    app.chatMessages.querySelectorAll('.chat-bubble.activity-group'),
  ).filter(group => group.parentElement);
  if (!groups.length) return null;
  const users = app.chatMessages.querySelectorAll('.chat-bubble.user');
  const lastUser = users.length ? users[users.length - 1] : null;
  let tail = null;
  for (const group of groups) {
    if (lastUser
        && !(lastUser.compareDocumentPosition(group) & Node.DOCUMENT_POSITION_FOLLOWING)) {
      continue;
    }
    tail = group;
  }
  // During a transcript reorder the active group's owner can briefly be ahead
  // of its user row. The stream itself identifies which group is changing, so
  // retain that explicit target until normal DOM order catches up.
  return tail || (_requestedLiveActivityGroup?.isConnected
    ? _requestedLiveActivityGroup : null);
}

function _clearLiveSheen() {
  if (!app.chatMessages) return;
  const marked = app.chatMessages.querySelectorAll('.chat-bubble.' + _LIVE_ACTIVITY_CLS);
  for (const group of marked) group.classList.remove(_LIVE_ACTIVITY_CLS);
}

// The progress-note row is the sheen's SECOND trigger, for the surfaces that
// render the note instead of the class (chat widget, mirrored notes). Only the
// live line may keep one; every other mirrored note row is retired so it cannot
// light an older chip. The cached-resync "Assessing…" note is no longer a
// transcript row at all — it renders in the footer activity element.
function _retireStrayLiveNotes(tail) {
  if (!app.chatMessages) return;
  const notes = app.chatMessages.querySelectorAll('.live-activity-note');
  for (const note of notes) {
    if (tail && tail.contains(note)) continue;
    note.remove();
  }
}

function _syncLiveSheen() {
  if (!app.chatMessages) return;
  const tail = _liveActivityArmed ? _liveTailActivityGroup() : null;
  const marked = app.chatMessages.querySelectorAll('.chat-bubble.' + _LIVE_ACTIVITY_CLS);
  for (const group of marked) {
    if (group !== tail) group.classList.remove(_LIVE_ACTIVITY_CLS);
  }
  if (tail) tail.classList.add(_LIVE_ACTIVITY_CLS);
  if (tail) _retireStrayLiveNotes(tail);
}

// Transcript mutations arrive in bursts while a run streams, so coalesce the
// re-assertion into one microtask per burst. The sync is idempotent and only
// runs while armed, so it cannot revive a finished line.
function _queueLiveSheenSync() {
  if (_liveSheenSyncQueued) return;
  _liveSheenSyncQueued = true;
  Promise.resolve().then(() => {
    _liveSheenSyncQueued = false;
    if (_liveActivityArmed) _syncLiveSheen();
  });
}

function _ensureLiveSheenObserver() {
  if (!app.chatMessages) return;
  if (_liveSheenObserver && _liveSheenTarget === app.chatMessages) return;
  if (_liveSheenObserver) _liveSheenObserver.disconnect();
  _liveSheenTarget = app.chatMessages;
  _liveSheenObserver = new MutationObserver(() => {
    if (_liveActivityArmed) _queueLiveSheenSync();
  });
  _liveSheenObserver.observe(app.chatMessages, { childList: true, subtree: true });
}

// Every live-only path funnels through here (streamed preview, settled step
// publish, mirrored progress note). Arming + one sync is all a caller needs;
// which line actually glimmers is decided by the invariant above, so a caller
// that is NOT the newest line simply never gets lit.
function _markLiveActivityGroup(group) {
  if (!group || !group.isConnected) return group || null;
  _setLiveActivityArmed(true);
  _requestedLiveActivityGroup = group;
  _ensureLiveSheenObserver();
  _syncLiveSheen();
  return group;
}

function _mirrorBubble(sessionId) {
  if (!app.chatMessages) return null;
  const owner = sessionId || app.currentSessionId || '';
  const bubbles = app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.agent.activity-group.' + _LIVE_ACTIVITY_CLS,
  );
  // Prefer a bubble stamped for this session; never reuse another session's.
  for (let i = bubbles.length - 1; i >= 0; i--) {
    const b = bubbles[i];
    const sid = b.dataset.sessionId || '';
    if (owner && sid && sid !== owner) continue;
    return b;
  }
  return null;
}

function _mirrorNoteRow(bubble) {
  return bubble ? bubble.querySelector(':scope > .live-activity-note') : null;
}

// The transcript mirror is a live ticker, not durable turn history. Keep its
// one row after every LLM/tool section so it is always the newest visible item
// in the current agent output bubble.
function _pinMirrorNoteToBottom(bubble) {
  const note = _mirrorNoteRow(bubble);
  if (note && note !== bubble.lastElementChild) bubble.appendChild(note);
  return note;
}

function _retireMirrorBubble(bubble) {
  if (!bubble) return;
  _clearActivityPreviewTailReserve(bubble);
  const note = _mirrorNoteRow(bubble);
  if (note) note.remove();
  const hasOutput = Array.from(
    bubble.querySelectorAll(':scope > .turn-section.llm-section'),
  ).some(section => section.textContent.trim());
  if (!bubble.querySelector('.bubble-tool-calls') && !hasOutput) {
    if (app._activeToolGroupBubble === bubble) app._activeToolGroupBubble = null;
    bubble.remove();
    return;
  }
  bubble.classList.remove(_LIVE_ACTIVITY_CLS);
}

function mirrorActivityNote(text, sessionId, ownerTurnId, options = {}) {
  // Every activity note — a run note or a client mock note — follows the
  // transcript-mirror preference. `force` is still honoured for a caller that
  // must paint a row regardless, but no shipped caller forces one any more.
  const force = Boolean(options && options.force);
  if (!force && !chatUiFlag('mirror_activity_in_transcript', false)) return null;
  if (!force && !isMessageTypeVisible('progress')) return null;
  if (!app.chatMessages) return null;
  // Session-scope: only paint the note into the transcript of the session the
  // note belongs to. A run in another session must never bleed into the view
  // on screen (covers unreset switch paths like the Sessions admin page).
  const owner = sessionId || app.currentSessionId || '';
  if (owner && app.currentSessionId && owner !== app.currentSessionId) return null;
  const txt = (text || '').trim();
  if (!txt) return null;

  // Prefer the current turn's activity/output bubble. If tools caused the
  // active pointer to move, migrate the singleton note instead of leaving a
  // stale progress bubble behind.
  let bubble = _activityGroupForTurn(ownerTurnId);
  if (!bubble) {
    const mirror = _mirrorBubble(owner);
    const mirrorTurn = mirror && mirror.dataset.activityTurnId;
    // A replacement message owns a fresh group while its predecessor unwinds.
    if (!ownerTurnId || !mirrorTurn || mirrorTurn === String(ownerTurnId)) {
      bubble = mirror;
    }
  }
  if (bubble) {
    const sid = bubble.dataset.sessionId || '';
    if (owner && sid && sid !== owner) bubble = null;
  }
  if (!bubble) {
    bubble = addChatBubble(
      'agent', '', 'tool-only activity-group ' + _LIVE_ACTIVITY_CLS, undefined,
      undefined, undefined, Date.now(),
    );
    if (owner) bubble.dataset.sessionId = owner;
    // This bubble is the live tool group for the current turn — tool calls and
    // update rows will attach here via the standard paths.
    app._activeToolGroupBubble = bubble;
  }
  _markLiveActivityGroup(bubble);
  if (owner) bubble.dataset.sessionId = owner;
  app._activeToolGroupBubble = bubble;
  // The activity ticker often appears before the first assistant row is
  // durable.  Stamp it with the user-turn id as soon as we know it so the
  // streamed preview and subsequent tool calls adopt this exact bubble rather
  // than creating a visually identical sibling.
  if (ownerTurnId) bubble.dataset.activityTurnId = String(ownerTurnId);
  if (ownerTurnId) {
    const owned = Array.from(app.chatMessages.querySelectorAll(
      `.chat-bubble.activity-group[data-activity-turn-id="${CSS.escape(String(ownerTurnId))}"]`,
    ));
    if (owned.length > 1) {
      bubble = _coalesceActivityGroups(owned);
      _markLiveActivityGroup(bubble);
    }
  }

  let note = _mirrorNoteRow(bubble);
  // There is exactly one live transcript progress row. Retire every older
  // mirror, including shells created before the active activity group changed.
  const mirrors = Array.from(app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.agent .live-activity-note',
  ));
  for (const oldNote of mirrors) {
    const oldBubble = oldNote.closest('.chat-bubble');
    if (oldBubble === bubble) continue;
    _retireMirrorBubble(oldBubble);
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'live-activity-note';
    note.title = 'Click to open this turn\'s tool calls';
    const label = document.createElement('span');
    label.className = 'live-activity-label';
    note.appendChild(label);
    note.addEventListener('click', (e) => {
      e.stopPropagation();
      // A run-level coalesce may migrate this singleton note from a redundant
      // shell into the surviving bubble. Resolve its owner at click time.
      const ownerBubble = note.closest('.chat-bubble');
      const container = ownerBubble && ownerBubble.querySelector('.bubble-tool-calls');
      const head = container && container.querySelector('.bubble-tool-calls-head');
      if (head) head.click();
    });
    bubble.appendChild(note);
  }
  const label = note.querySelector('.live-activity-label');
  if (label && label.textContent !== txt) label.textContent = txt;
  _pinMirrorNoteToBottom(bubble);
  // Keep the live group directly after the user turn that owns it.
  positionActivityGroupAfterOwner(ownerTurnId, bubble);
  if (typeof app._scrollToBottomIfNear === 'function') {
    try {
      app._scrollToBottomIfNear(
        app._chatScroller || (app.chatMessages && app.chatMessages.parentElement),
      );
    } catch (_) {}
  }
  return note;
}

function mirrorActivityEnd(sessionId) {
  if (!app.chatMessages) return;
  // Retire the rule FIRST: a run that has ended must never leave a line
  // glimmering, and no later mutation may re-light one. The sweep below then
  // tears down the surviving shells.
  _setLiveActivityArmed(false);
  _requestedLiveActivityGroup = null;
  _clearLiveSheen();
  const owner = sessionId || app.currentSessionId || '';
  const bubbles = app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.agent.activity-group.' + _LIVE_ACTIVITY_CLS,
  );
  for (const b of bubbles) {
    const sid = b.dataset.sessionId || '';
    // A bubble stamped for a DIFFERENT session is stale — remove it outright so
    // a leftover mirror can never linger in another view. (On a session switch
    // the re-render wipes the container anyway; this is defense in depth.)
    if (owner && sid && sid !== owner) {
      _stopActivityElapsed(b);
      b.remove();
      continue;
    }
    // Strip the live marker FIRST: a throw in the elapsed/preview teardown below
    // must never leave a finished run's summary shimmering.
    b.classList.remove(_LIVE_ACTIVITY_CLS);
    try {
      _stopActivityElapsed(b);
      _clearActivityPreviewTailReserve(b);
    } catch (_) {}
    const note = _mirrorNoteRow(b);
    if (note) note.remove();
    // Keep the bubble only when it actually carries tool calls / update rows —
    // otherwise it was a bare note ticker (the bar clears too) and should go.
    if (!b.querySelector('.bubble-tool-calls')) {
      if (app._activeToolGroupBubble === b) app._activeToolGroupBubble = null;
      b.remove();
    }
  }
  // Completed runs must not hand their historical shell to a later live run.
  _clearLiveActivityGroupsForSession(owner);
  if (typeof app._reorderTranscriptCanonical === 'function') {
    app._reorderTranscriptCanonical();
  }
}
function _anchorToolBubble(bubble, anchor) {
  if (!bubble || !anchor) return;
  const anchorId = anchor.id || anchor.asstId;
  if (anchorId && !bubble.hasAttribute('data-msg-id')) {
    bubble.setAttribute('data-msg-id', String(anchorId));
  }
  if (anchor.createdAt && !bubble.hasAttribute('data-created-at')) {
    _setBubbleCreatedAt(bubble, anchor.createdAt);
  }
  const durableSeq = Number.isFinite(anchor.interactionSeq)
    ? anchor.interactionSeq : anchor.sessionSeq;
  if (Number.isFinite(durableSeq)) {
    const existing = bubble.hasAttribute('data-session-seq')
      ? Number(bubble.getAttribute('data-session-seq'))
      : NaN;
    // A disclosure may combine consecutive tool-only assistant rows. Anchor
    // the combined line at the earliest durable row represented by the group.
    _setBubbleSessionSeq(
      bubble,
      Number.isFinite(existing) ? Math.min(existing, durableSeq) : durableSeq,
    );
  }
}

function _attachSavedFileCards(bubble, calls) {
  if (!bubble || !calls) return;
  for (const call of calls) {
    const card = renderSavedFileAttachmentCard(call);
    if (!card) continue;
    const callId = card.dataset.fileAttachmentCallId;
    if (callId && bubble.querySelector(`[data-file-attachment-call-id="${CSS.escape(callId)}"]`)) continue;
    bubble.appendChild(card);
  }
}

function attachToolCallsToLastBubble(calls, targetBubble, anchor) {
  if (!calls || calls.length === 0) return;
  if (!app.chatMessages) return;

  // Download cards are assistant-facing deliverables, not diagnostic tool UI.
  // If tool details are hidden, still mount cards on an existing response bubble;
  // tool-only calls without a bubble are handled by attachActivityEntries.
  if (!chatUiFlag('show_tool_calls', true) || !isMessageTypeVisible('tool')) {
    _attachSavedFileCards(targetBubble || app._agentTurnBubble, calls);
    return;
  }
  // Durable live runs are grouped by their owning user turn, never by the
  // visually last bubble. This is the primary guard against late tool events
  // attaching to a previous or resumed response.
  if (!targetBubble && anchor && anchor.turnId) {
    attachActivityEntries(
      calls,
      _activityGroupForTurn(anchor.turnId),
      anchor,
      anchor.turnId,
      anchor.runKey || _runKey(anchor.turnId, anchor.interactionSeq),
    );
    return;
  }

  // 1. If a tool-only group already exists in the same bubble, append rows to it.
  if (!targetBubble && app._activeToolGroupBubble && app._activeToolGroupBubble.isConnected) {
    const section = app._activeToolGroupBubble.querySelector(':scope > .turn-section.tool-section');
    const container = section ? section.querySelector('.bubble-tool-calls') : app._activeToolGroupBubble.querySelector('.bubble-tool-calls');
    if (_appendCallsToToolContainer(container, calls)) {
      _anchorToolBubble(app._activeToolGroupBubble, anchor);
      return;
    }
  }

  let last = targetBubble || null;
  if (!last) {
    const bubbles = app.chatMessages.querySelectorAll('.chat-bubble.agent');
    last = bubbles[bubbles.length - 1];
  }

  // 2. Determine if this is a tool-only turn (no target, and either no last
  //    bubble, no text was written yet, or the last bubble is a streaming
  //    placeholder with no content).
  const isToolOnlyTurn = !targetBubble && (
    !last
    || !app._turnHasBubble
    || (last.classList.contains('streaming') && !_getBubbleText(last))
  );

  if (!last && !isToolOnlyTurn) return;

  if (isToolOnlyTurn) {
    // A status marker belongs after the calls that were in flight when the
    // user stopped the run. Never absorb those calls into the "Stopped"/error
    // bubble; create or reuse their own durably anchored disclosure before it.
    if (last && (last.classList.contains('interrupted') || last.classList.contains('error'))) {
      last = null;
    }
    // Merge into an existing tool-call bubble group (if one is already live).
    const group = app._activeToolGroupBubble;
    if (group && group.isConnected) {
      const section = group.querySelector(':scope > .turn-section.tool-section');
      const container = section ? section.querySelector('.bubble-tool-calls') : group.querySelector('.bubble-tool-calls');
      if (_appendCallsToToolContainer(container, calls)) {
        _anchorToolBubble(group, anchor);
        return;
      }
    }
    // No existing group and no last bubble to attach to — create a fresh
    // tool-only bubble so the calls are visible during streaming (required
    // when the exchange's first turn is tool-only with no prior agent bubble).
    if (!last) {
      const anchorId = anchor && (anchor.id || anchor.asstId);
      last = addChatBubble(
        'agent', '', 'tool-only', undefined,
        anchorId || ('tool-turn-' + Date.now()), anchorId || undefined,
        anchor && anchor.createdAt,
      );
      app._turnHasBubble = true;
      app._activeToolGroupBubble = last;
      _addToolCallsToBubble(last, calls);
      _anchorToolBubble(last, anchor);
      return;
    }
    // Append a tool section to the last bubble
    if (last.classList.contains('streaming')) last.classList.remove('streaming');
    app._activeToolGroupBubble = last;
    _addToolCallsToBubble(last, calls);
    _anchorToolBubble(last, anchor);
    return;
  }

  // 3. This turn has text — append tool section to the same bubble.
  _addToolCallsToBubble(last, calls);
  _anchorToolBubble(last, anchor);
}

let _compactionProgressNote = null;

function clearCompactionProgress() {
  const notes = new Set();
  if (_compactionProgressNote && _compactionProgressNote.isConnected) {
    notes.add(_compactionProgressNote);
  }
  // Reconcile can replace/reparent transcript nodes while compaction is in
  // flight, leaving the original reference stale. Clear any surviving clone so
  // the progress note cannot coexist with the completion notice.
  if (app.chatMessages) {
    app.chatMessages.querySelectorAll('.compaction-progress-note')
      .forEach(note => notes.add(note));
    // Remove legacy standalone markers left by an older cached client.
    app.chatMessages.querySelectorAll('.system-compaction-progress')
      .forEach(el => el.remove());
  }
  for (const note of notes) {
    const bubble = note.closest('.chat-bubble');
    note.classList.remove('compaction-progress-note');
    _retireMirrorBubble(bubble);
  }
  _compactionProgressNote = null;
}

function showCompactionProgress() {
  clearCompactionProgress();
  // "Compacting…" is a mock activity note — the server compacts, the client
  // narrates it — so it obeys the SAME rule as every other activity note: it
  // ticks in the footer activity element (the caller's chatActivityStart) and
  // reaches the transcript ONLY when that mirror is configured on
  // (chat_ui.json → mirror_activity_in_transcript). It used to pass force:true,
  // which leaked a note into the chat while activity notes were configured OFF —
  // the same defect class as the cached-resync "Assessing…" note.
  const note = mirrorActivityNote('Compacting...', app.currentSessionId);
  if (note) note.classList.add('compaction-progress-note');
  _compactionProgressNote = note || null;
  return note;
}

const _seenCompactionEvents = new Set();

function markCompactionSeen(interactionId) {
  if (interactionId) _seenCompactionEvents.add(String(interactionId));
}

function _formatCompactionNumber(value) {
  if (typeof app.formatContextTokens === 'function') return app.formatContextTokens(value);
  const n = Number(value) || 0;
  if (n >= 1000000) return (n / 1000000).toFixed(1).replace(/\.0$/, '') + 'm';
  if (n >= 1000) return (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return String(Math.round(n));
}

function notifyCompactionCompleted(event) {
  if (!event || event.status !== 'success' || event.replayed) return null;
  if (event.session_id && app.currentSessionId
      && String(event.session_id) !== String(app.currentSessionId)) return null;
  const interactionId = String(event.interaction_id || '');
  if (!interactionId) return null;
  const alreadySeen = _seenCompactionEvents.has(interactionId);
  // The completion can arrive over WS before the blocking /compact POST returns.
  // A transcript reconcile in between may replace the DOM and erase the short
  // notice. Let the local response restore only that missing notice; ordinary
  // duplicate/replayed events remain suppressed.
  if (alreadySeen && !event.local_response) {
    // A duplicate completion still closes a newly restored progress marker for
    // the same result, but it never replays the short completion notice.
    if (app.chatMessages && app.chatMessages.querySelector(
      '.system-compaction-transient[data-compaction-interaction-id="'
      + CSS.escape(interactionId) + '"]',
    )) clearCompactionProgress();
    return null;
  }
  if (!alreadySeen) _seenCompactionEvents.add(interactionId);

  // Compaction measures only locally estimated history. The footer is updated
  // exclusively by exact provider usage from llm_call_end.
  if (!alreadySeen && !event.skip_durable
      && typeof app.attachActivityEntries === 'function') {
    const manual = event.trigger === 'manual';
    const owner = manual ? '' : (event.owner_turn_id || '');
    // A compaction belongs to the run still open at the transcript tail, so all
    // notices for one run share exactly one activity group. Resolve that shell
    // before attaching; only fall back to a fresh shell when no run is open
    // (e.g. /compact on an empty chat).
    const liveGroup = (typeof _coalesceLiveActivityGroups === 'function')
      ? _coalesceLiveActivityGroups() : null;
    let target = null;
    let resolvedOwner = owner;
    if (manual && liveGroup) {
      target = liveGroup;
      resolvedOwner = liveGroup.dataset.activityTurnId || '';
    } else if (manual) {
      target = addChatBubble(
        'agent', '', 'tool-only activity-group', undefined,
        'activity-' + interactionId, 'activity-' + interactionId,
        event.created_at,
      );
    } else if (!owner && liveGroup) {
      resolvedOwner = liveGroup.dataset.activityTurnId || '';
    }
    app.attachActivityEntries([{
      kind: 'system',
      notice: 'compaction',
      id: interactionId,
      label: 'Compaction details',
      content: event.detail || '',
      createdAt: event.created_at,
      interactionSeq: event.interaction_seq,
    }], target, {
      id: interactionId,
      createdAt: event.created_at,
      turnId: resolvedOwner,
      interactionSeq: event.interaction_seq,
    }, resolvedOwner, resolvedOwner
      ? _runKey(resolvedOwner, event.interaction_seq) : '');
  }
  if (alreadySeen && event.local_response && app.chatMessages) {
    const existing = app.chatMessages.querySelector(
      '.system-compaction-transient[data-compaction-interaction-id="'
      + CSS.escape(interactionId) + '"]',
    );
    if (existing) {
      clearCompactionProgress();
      return existing;
    }
  }
  // Version 1 events remain readable during a rolling upgrade, but neither
  // version may update the provider-context footer.
  const historyBefore = Number(event.history_tokens_before ?? event.tokens_before);
  const historyAfter = Number(event.history_tokens_after ?? event.tokens_after);
  const message = 'Compacted history '
    + _formatCompactionNumber(historyBefore)
    + ' to '
    + _formatCompactionNumber(historyAfter)
    + ' tokens';
  // Insert the completion first so the progress note remains present until its
  // replacement is visible; then remove only the transient progress row.
  const bubble = addChatBubble('info', message, 'system-compaction-transient');
  clearCompactionProgress();
  if (bubble) {
    bubble.dataset.compactionInteractionId = interactionId;
    window.setTimeout(() => {
      if (bubble.isConnected) bubble.remove();
    }, 2000);
  }
  return bubble;
}

// Expose on app immediately for session-load.js
app.appendStreamToActiveBubble = appendStreamToActiveBubble;
app.finalizeAgentResponse = finalizeAgentResponse;
app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;
app.seedStreamingBubble = seedStreamingBubble;
app.finalizeAgentStep = finalizeAgentStep;
app.cancelAllPendingStepFlushes = _cancelAllPendingStepFlushes;
app.markAgentInterrupted = markAgentInterrupted;
// A mode/model notice raised BEFORE its run starts lands above the prompt if the
// user then sends a message: the notice builds a provisional group at click time
// and the optimistic user bubble is appended after it. Treat the change as
// applied AFTER the send — move that notice-only provisional group behind the new
// user bubble, so it becomes the FIRST entry of the activity group that follows
// the message (which is also where a reload attributes it, because the durable
// row is owned by that turn).
// Deliberately conservative: only a provisional group (no persisted segment id,
// no owner yet) that holds notice rows and NO tool rows is moved — a group with
// real work in it already owns its turn and must never be dragged across a prompt.
function adoptPreSendNoticeGroup(userBubble) {
  if (!app.chatMessages || !userBubble || !userBubble.isConnected) return null;
  const candidates = Array.from(app.chatMessages.querySelectorAll(
    ':scope > .chat-bubble.activity-group:not([data-activity-segment-id])',
  )).filter(group => group !== userBubble
    && !group.dataset.activityTurnId
    && group.querySelector('.ca-mode-row, .ca-model-row')
    && !group.querySelector('.ca-tool-row:not(.ca-system-row)'));
  const group = candidates[candidates.length - 1];
  if (!group) return null;
  // Stamp and nudge BEFORE the move test. Even when addChatBubble already left the
  // group on the prompt's tail — so no move is needed — the stamp is essential:
  // the run's first tool row gives the group a durable sequence, and the
  // transcript's ordering authority sorts sequenced nodes ahead of sequence-less
  // ones, so without an owner match the notice group is hoisted ABOVE the prompt.
  // Owned by the prompt, it is pinned to that prompt's tail instead.
  const promptId = userBubble.dataset.msgId || userBubble.dataset.turnId || '';
  if (promptId && !group.dataset.activityTurnId) {
    group.dataset.activityTurnId = String(promptId);
  }
  // Belt and braces for the same sort: keep the provisional timestamp on the
  // prompt's tail when both nodes are still sequence-less.
  const promptCreatedAt = Number(userBubble.dataset.createdAt || NaN);
  if (Number.isFinite(promptCreatedAt)) {
    try { _setBubbleCreatedAt(group, promptCreatedAt + 1); } catch (_) { /* order key optional */ }
  }
  const precedes = !!(group.compareDocumentPosition(userBubble)
    & Node.DOCUMENT_POSITION_FOLLOWING);
  if (precedes) userBubble.insertAdjacentElement('afterend', group);
  app._activeToolGroupBubble = group;
  return group;
}
app.adoptPreSendNoticeGroup = adoptPreSendNoticeGroup;
app.notifyExecutionMode = notifyExecutionMode;
app.notifyModelSwitch = notifyModelSwitch;
app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;
app.attachActivityEntries = attachActivityEntries;
app.attachProgressToActivity = attachProgressToActivity;
app.applyDurableActivitySummary = applyDurableActivitySummary;
app.mirrorActivityNote = mirrorActivityNote;
app.mirrorActivityEnd = mirrorActivityEnd;
app.positionActivityGroupAfterOwner = positionActivityGroupAfterOwner;
app.rekeyActivityGroupOwner = rekeyActivityGroupOwner;
app.suppressMatchingResponsePreview = suppressMatchingResponsePreview;
app.notifyCompactionCompleted = notifyCompactionCompleted;
app.markCompactionSeen = markCompactionSeen;
app.showCompactionProgress = showCompactionProgress;
app.clearCompactionProgress = clearCompactionProgress;
app._renderCompletionCloser = _renderCompletionCloser;
app._promoteRunCloserBubble = _promoteRunCloserBubble;

export {
  _stripToolCalls,
  _isSubstantiveAnswer,
  _buildActivityEntryRow as buildActivityEntryRow,
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  MODE_NOTICE_LABEL,
  MODEL_NOTICE_LABEL,
  _projectModeNotices,
  _projectModelNotices,
  notifyExecutionMode,
  notifyModelSwitch,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
  _findAgentBubbleForTurn,
  attachActivityEntries,
  attachProgressToActivity,
  applyDurableActivitySummary,
  mirrorActivityNote,
  mirrorActivityEnd,
  positionActivityGroupAfterOwner,
  rekeyActivityGroupOwner,
  suppressMatchingResponsePreview,
  notifyCompactionCompleted,
  showCompactionProgress,
  clearCompactionProgress,
  _wsTurnBuffers,
  _setBubbleText,
  _setTurnBufferMonotonic,
};

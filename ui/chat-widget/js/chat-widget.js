'use strict';

// ── Chat widget — floating mini chat for task-scoped agent sessions ────────
// createChatWidget(options) spawns a small floating card (bottom-right) that
// runs an agent task in its OWN session, completely independent of the main
// chat side-panel. It streams the agent's progress live (bubbles + tool-call
// rows), has a mini chat-pill input so the user can answer agent questions,
// Continue / Stop buttons, and collapses to a compact "Done" chip when the
// task finishes (click the chip to re-expand). The card is draggable by its
// header and resizable from any edge or corner.
//
// The widget consumes the same committed V2 public items and durable
// subscription as the main panel. Private execution output is never rendered
// here. Its session is independent of the main panel's session.
//
// Close ≠ Stop: closing the widget only detaches it — a running task keeps
// going server-side and the session stays in the normal session list. Stop
// interrupts the run.
//
// Widgets do not survive a page reload (deliberate): the task keeps running
// server-side and its history is reachable from the main panel's session
// dropdown. Widgets capture the user id at creation; close them on account
// switch.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { randomUUID } from '../../shared/js/uuid.js';
import { renderPublishedArtifact, requestPublishedArtifactGrant } from '../../shared/js/published-artifact.js';
import * as controlledChat from '../../chat/core/client.js';
import { icon } from '../../shared/js/icons.js';
import { chatMsg } from '../../shared/js/app-prompts.js';
import { _fillAgentBubble } from '../../chat/js/chat-bubble.js';
import { buildToolRow } from '../../shared/js/chat-activity.js';
import { buildActivityEntryRow } from '../../chat/js/chat-stream.js?v=260';
import {
  appendChatSurfaceBubble,
  applyChatSurfaceProfile,
  applyChatSurfaceHeader,
  chatSurfaceStatsHtml,
  createChatSurfaceUsage,
  wireChatSurfaceComposer,
  wireStatsCarousel,
} from '../../shared/js/chat-surface.js';

// ── Widget registry (enumerate / close / restore all open widgets) ─────────
const _openWidgets = new Set();

/** @returns {Array} snapshot of all open widget instances */
export function getOpenWidgets() { return Array.from(_openWidgets); }

/** Close every open widget. Idempotent — already-closed widgets are skipped. */
export function closeAllWidgets() {
  const all = Array.from(_openWidgets);
  all.forEach(w => { try { w.close(); } catch (_) {} });
}

/** Restore every minimized widget back to full card view. */
export function restoreAllMinimized() {
  _openWidgets.forEach(w => {
    if (w.minimized) try { w.restore(); } catch (_) {}
  });
}

let _chatUiProfilePromise;
function _mergeChatProfile(base, overrides) {
  const out = { ...(base || {}) };
  for (const [key, value] of Object.entries(overrides || {})) {
    out[key] = value && typeof value === 'object' && !Array.isArray(value)
      ? _mergeChatProfile(base?.[key], value) : value;
  }
  return out;
}
export function _widgetChatUiProfile() {
  if (!_chatUiProfilePromise) {
    _chatUiProfilePromise = fetch(apiPath('/api/v1/auth/ui-config'), { headers: { ...authHeaders() } })
      .then(r => r.ok ? r.json() : null)
      .then(data => _mergeChatProfile(data?.chat_ui?.chat_common, data?.chat_ui?.chat_widget))
      .catch(() => null);
  }
  return _chatUiProfilePromise;
}

// ── Floating layer (singleton, stacks all docked widgets) ───────────────────

let _layer = null;
let _layerWired = false;

function _repositionLayer() {
  if (!_layer) return;
  // Keep the docked widget stack clear of the main chat panel when it's open.
  let inset = 16;
  const panel = document.getElementById('chat-panel');
  if (panel) {
    const r = panel.getBoundingClientRect();
    const visible = r.width > 0 && getComputedStyle(panel).display !== 'none';
    if (visible) inset = Math.max(16, Math.round(window.innerWidth - r.left) + 16);
  }
  _layer.style.right = inset + 'px';
}

function _ensureLayer() {
  // Already mounted on <body> → reuse it.
  if (_layer && document.body.contains(_layer)) return _layer;
  // The layer node still exists but got detached from <body> (a pathological
  // page rebuild wiped its subtree). Re-attach the SAME node so any widget
  // already running inside it survives — NEVER orphan a live task by spinning
  // up a fresh empty layer.
  if (_layer && _layer.querySelector('.chat-widget')) {
    document.body.appendChild(_layer);
    _guardLayer();
    _repositionLayer();
    return _layer;
  }
  _layer = document.createElement('div');
  _layer.id = 'chat-widget-layer';
  document.body.appendChild(_layer);
  if (!_layerWired) {
    _layerWired = true;
    window.addEventListener('resize', _repositionLayer);
    const panel = document.getElementById('chat-panel');
    if (panel && typeof ResizeObserver === 'function') {
      new ResizeObserver(_repositionLayer).observe(panel);
    }
  }
  _guardLayer();
  _repositionLayer();
  return _layer;
}

// HARDENING — the widget layer must outlive ANY page navigation. It lives on
// <body>, OUTSIDE every page (each page is just shown/hidden under #stage), so
// a normal tab switch can't touch it. This watchdog covers the pathological
// case where some code rebuilds <body>'s subtree and removes the layer out
// from under a running widget: if the layer is removed while it STILL holds a
// widget, we put it straight back as the last child (so it also stays on top).
// It never fights a legitimate teardown — close()/_maybeRemoveLayer empty the
// layer FIRST, so a removal with no .chat-widget child is left alone. One
// observer for the app lifetime; the body-childList callback is a cheap guard
// (tab content lives under #stage, not directly on <body>, so it rarely fires).
let _layerGuard = null;
function _guardLayer() {
  if (_layerGuard || typeof MutationObserver !== 'function') return;
  _layerGuard = new MutationObserver(() => {
    if (_layer && !document.body.contains(_layer) && _layer.querySelector('.chat-widget')) {
      document.body.appendChild(_layer);   // re-attach as last child → always on top
      _repositionLayer();
    }
  });
  _layerGuard.observe(document.body, { childList: true });
}

function _maybeRemoveLayer() {
  if (_layer && !_layer.querySelector('.chat-widget')) {
    _layer.remove();
    _layer = null;
  }
}

// ── Widget factory ──────────────────────────────────────────────────────────

const STATUS_LABEL = {
  starting: 'Starting…',
  running: 'Working…',
  done: 'Done',
  interrupted: 'Stopped',
  error: 'Error',
  idle: '',
};

const RESIZE_DIRS = ['n', 's', 'e', 'w', 'ne', 'nw', 'se', 'sw'];

export function createChatWidget(opts = {}) {
  const {
    title = 'Agent task',
    iconName = 'sparkles',
    agentId = null,
    ensureAgent = null,        // async () => agentId — caller handles ability setup
    initialMessage = '',
    initialAttachmentIds = null,  // attachment_ids to ride the FIRST message only
    executionMode = 'ask',
    transformMessage = null,   // async (text) => sentText — wrap each outgoing message
                               //   (e.g. Gen UI tags every send with its page header);
                               //   the user bubble still shows the raw typed text.
    sessionId = null,          // explicit session id (genui session contract)
    initialSessionSeq = 0,     // existing-session rows to keep out of this widget view
    hydrateExistingSession = false, // fetch prior rows when opening an existing session
    sessionTargetName = '',    // title the deployed session gets after first send
    rememberSessionKey = '',   // localStorage key to remember the session id under
    onDone = null,             // (finalText) => void — fires once per finished turn
    onClose = null,            // () => void
  } = opts;

  const st = {
    sessionId: sessionId || randomUUID(),
    userId: app.currentUserId,
    agentId: agentId || null,
    sessionTargetName: sessionTargetName || '',
    rememberSessionKey: rememberSessionKey || '',
    status: 'idle',
    closed: false,
    minimized: false,
    floating: false,           // user has dragged/resized → fixed positioning
    hadTurn: false,            // at least one turn has run (controls Continue)
    settled: true,             // current turn has reached a terminal state
    stopPending: false,        // stop clicked — pin "Stopping…" until a terminal state
    unregisterPublic: null,
    publicSeen: new Map(),
    publicNodes: new Map(),
    pendingBubbles: new Map(),
    onSendStatus: null,
    ready: null,               // promise resolving when agentId is known
    turnBuffers: new Map(),    // key -> accumulated/absolute stream text
    bubbles: new Map(),        // key -> agent bubble element
    lastAgentText: '',         // most recent finalized agent text (for onDone)
    toolCalls: [],             // current exchange's entries for buildToolRow
    activityItems: [],         // chronological tools + panel-identical progress/response rows
    toolGroup: null,           // current exchange's shared activity disclosure bubble
    profile: null,             // merged chat_common + chat_widget profile
    collapseTimer: null,
    usage: null,
    activityTimer: null,
    activityNote: '',
    activityStartedAt: 0,
    els: {},
  };

  // ── DOM ──

  function _build() {
    const root = document.createElement('div');
    root.className = 'chat-widget chat-surface';

    // Resize handles (one per edge/corner). Appended first so the header and
    // its buttons paint above them and stay clickable.
    RESIZE_DIRS.forEach((dir) => {
      const h = document.createElement('div');
      h.className = 'cw-resize cw-resize-' + dir;
      h.dataset.dir = dir;
      h.addEventListener('pointerdown', _onResizeDown);
      root.appendChild(h);
    });

    const header = document.createElement('div');
    header.className = 'cw-header';
    header.innerHTML =
      icon(iconName, { size: '16px', cls: 'cw-head-icon' }) +
      '<span class="cw-title"></span>' +
      '<span class="cw-status"></span>' +
      '<span class="cw-dot idle" aria-hidden="true"></span>';
    const minBtn = document.createElement('button');
    minBtn.type = 'button';
    minBtn.className = 'cw-icon-btn cw-min-btn';
    minBtn.title = 'Minimize';
    minBtn.innerHTML = icon('minus', { size: '15px' });
    minBtn.addEventListener('click', (e) => { e.stopPropagation(); minimize(); });
    header.appendChild(minBtn);
    const closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'cw-icon-btn cw-close-btn';
    closeBtn.title = 'Close (task keeps running; session stays in your history)';
    closeBtn.innerHTML = icon('x', { size: '15px' });
    closeBtn.addEventListener('click', (e) => { e.stopPropagation(); close(); });
    header.appendChild(closeBtn);
    // Drag to move; click (no drag) on a minimized chip restores it.
    header.addEventListener('pointerdown', _onHeaderDown);
    header.addEventListener('click', () => { if (st.minimized) restore(); });
    root.appendChild(header);

    const body = document.createElement('div');
    body.className = 'cw-body';
    root.appendChild(body);

    const footer = document.createElement('div');
    footer.className = 'cw-footer';

    const actions = document.createElement('div');
    actions.className = 'chat-above-pill cw-actions';
    // Spinning loader — the widget's echo of the main panel's live tool-call
    // activity. Real loader icon (not a pulsing dot); the spin from .cw-spinner.
    const spinner = document.createElement('button');
    spinner.type = 'button';
    spinner.className = 'chat-activity-bar cw-spinner';
    spinner.dataset.footerControl = 'activity';
    spinner.innerHTML = '<span class="chat-activity-dot"></span><span class="chat-activity-text">Working</span><span class="chat-activity-chevron">›</span>';
    spinner.style.display = 'none';
    actions.appendChild(spinner);
    // Stop and Continue are the SAME in-pill controls used by the panel. The
    // shared chat-pill layout renderer reparents them into the JSON-authored
    // row/zone, so widget placement changes whenever chat_ui.json changes.
    const contBtn = document.createElement('button');
    contBtn.type = 'button';
    contBtn.className = 'chat-pill-continue cw-continue-btn';
    contBtn.dataset.footerControl = 'continue';
    contBtn.innerHTML = icon('play', { size: '18px' });
    contBtn.title = 'Ask the agent to keep going';
    contBtn.hidden = true;
    contBtn.addEventListener('click', () => send('Continue'));
    const stopBtn = document.createElement('button');
    stopBtn.type = 'button';
    stopBtn.className = 'chat-pill-stop cw-stop-btn';
    stopBtn.dataset.footerControl = 'stop';
    stopBtn.innerHTML = icon('octagon', { size: '18px' });
    stopBtn.title = 'Stop generation';
    stopBtn.hidden = true;
    stopBtn.addEventListener('click', () => interrupt());
    footer.appendChild(actions);

    const pill = document.createElement('div');
    pill.className = 'chat-pill cw-pill';
    const input = document.createElement('textarea');
    input.className = 'chat-pill-input';
    input.rows = 1;
    input.placeholder = 'Reply to the agent…';
    input.autocomplete = 'off';
    pill.appendChild(input);
    const stats = document.createElement('div');
    stats.className = 'chat-pill-stats cw-stats';
    stats.innerHTML = chatSurfaceStatsHtml();
    wireStatsCarousel(stats);
    pill.appendChild(stats);
    pill.appendChild(stopBtn);
    pill.appendChild(contBtn);
    const pillButtons = document.createElement('div');
    pillButtons.className = 'chat-pill-buttons cw-pill-buttons';
    const voiceBtn = document.createElement('button');
    voiceBtn.type = 'button';
    voiceBtn.className = 'chat-pill-voice';
    voiceBtn.title = 'Record voice';
    voiceBtn.innerHTML = icon('mic', { size: '32px' });
    pillButtons.appendChild(voiceBtn);
    const sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'chat-pill-send';
    sendBtn.title = 'Send';
    sendBtn.disabled = true;
    sendBtn.innerHTML = icon('send', { size: '18px' });
    pillButtons.appendChild(sendBtn);
    pill.appendChild(pillButtons);
    const attachBtn = document.createElement('button');
    attachBtn.type = 'button';
    attachBtn.className = 'chat-pill-attach';
    attachBtn.title = 'Attach files';
    attachBtn.innerHTML = icon('plus', { size: '22px' });
    pill.appendChild(attachBtn);
    const footerHandle = document.createElement('div');
    footerHandle.className = 'chat-footer-handle expanded';
    footerHandle.setAttribute('aria-expanded', 'true');
    const handleLine = document.createElement('div');
    handleLine.className = 'chat-footer-handle-line';
    footerHandle.appendChild(handleLine);
    pill.appendChild(footerHandle);
    footer.appendChild(pill);
    const below = document.createElement('div');
    below.className = 'chat-footer-row expanded cw-below';
    below.innerHTML = '<button class="chat-skills-btn" type="button" data-footer-control="abilities">' + icon('blocks', { size: '16px' }) + '<span class="csp-badge">0</span></button><button class="chat-target-btn" type="button" data-footer-control="target">' + icon('monitor', { size: '16px' }) + '<span class="chat-target-label"></span></button><button class="chat-mode-btn" type="button" data-footer-control="mode">Ask</button>';
    footer.appendChild(below);
    root.appendChild(footer);

    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        // On touch devices (mobile), let Enter insert a newline; send via the button.
        if (window.matchMedia?.('(pointer: coarse)').matches) return;
        e.preventDefault();
        if (!sendBtn.disabled) _sendFromInput();
      }
    });
    sendBtn.addEventListener('click', _sendFromInput);

    // A user reading/interacting with the card shouldn't be yanked into a chip.
    root.addEventListener('pointerenter', () => {
      if (st.collapseTimer) { clearTimeout(st.collapseTimer); st.collapseTimer = null; }
    });

    st.els = { root, header, body, footer, pill, input, sendBtn, send: sendBtn, stopBtn, stop: stopBtn, contBtn, continue: contBtn,
               actions, above: actions, spinner, stats, pillButtons, voiceBtn, voice: voiceBtn,
               attachBtn, attach: attachBtn, below, footerToggle: footerHandle,
               title: header.querySelector('.cw-title'),
               statusEl: header.querySelector('.cw-status'),
               dot: header.querySelector('.cw-dot'),
               headIcon: header.querySelector('.cw-head-icon'),
               minBtn, closeBtn };
    st.els.title.textContent = title;
    st.usage = createChatSurfaceUsage(stats);
    wireChatSurfaceComposer(st.els, { isBusy: () => st.status === 'running' || st.status === 'starting' });
    _widgetChatUiProfile().then(profile => {
      st.profile = profile || {};
      applyChatSurfaceProfile(st.els, st.profile);
      applyChatSurfaceHeader(st.els, st.profile);
      const placeholder = st.profile?.messages?.pill_placeholder;
      if (placeholder) input.placeholder = placeholder;
      // Layout application reveals configured controls; immediately restore
      // their live running/idle visibility from widget state.
      _setStatus(st.status);
    });
    spinner.addEventListener('click', () => {
      const head = st.toolGroup?.querySelector('.bubble-tool-calls-head');
      if (!head) return;
      head.click();
      st.toolGroup.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    });
    return root;
  }

  function _ensureActivityGroup() {
    if (st.toolGroup?.isConnected) return st.toolGroup;
    const group = _addBubble('agent', '');
    group.classList.add('tool-only', 'activity-group', 'cw-activity-group');
    st.toolGroup = group;
    return group;
  }

  function _syncActivityMirror(labelText) {
    if (st.profile?.mirror_activity_in_transcript === false) return;
    const text = (labelText || '').trim();
    let note = st.toolGroup?.querySelector(':scope > .live-activity-note');
    if (!text) {
      note?.remove();
      if (st.toolGroup && !st.activityItems.length) {
        st.toolGroup.remove();
        st.toolGroup = null;
      }
      return;
    }
    const group = _ensureActivityGroup();
    if (!note) {
      note = document.createElement('div');
      note.className = 'live-activity-note';
      note.title = 'Click to open this turn\'s activity';
      const label = document.createElement('span');
      label.className = 'live-activity-label';
      note.appendChild(label);
      note.addEventListener('click', () => {
        group.querySelector('.bubble-tool-calls-head')?.click();
      });
      group.appendChild(note);
    }
    note.querySelector('.live-activity-label').textContent = text;
    if (note !== group.lastElementChild) group.appendChild(note);
    _scrollToBottom();
  }

  function _setActivity(note, hasTools = st.toolCalls.length > 0) {
    const bar = st.els.spinner;
    const wrap = bar?.parentElement;  // .chat-above-pill or .cw-actions
    const text = bar?.querySelector('.chat-activity-text');
    if (!bar || !wrap || !text) return;
    // While a stop is pending, the activity text stays pinned to "Stopping…" —
    // the backend keeps emitting pipeline/tool events as it unwinds, and none
    // of them may show over the stopping note.
    if (st.stopPending) note = 'Stopping…';
    if (note && note !== st.activityNote) {
      st.activityNote = note;
      st.activityStartedAt = performance.now();
    }
    bar.classList.toggle('has-tools', hasTools);
    wrap.classList.add('visible');
    bar.style.display = '';
    const render = () => {
      const elapsedSecs = st.activityStartedAt
        ? Math.floor((performance.now() - st.activityStartedAt) / 1000)
        : 0;
      const display = st.activityNote + (elapsedSecs >= 1 ? `  ·  ${elapsedSecs}s` : '');
      text.textContent = display;
      _syncActivityMirror(display);
    };
    render();
    if (!st.activityTimer) st.activityTimer = setInterval(render, 1000);
  }

  function _clearActivity() {
    if (st.activityTimer) { clearInterval(st.activityTimer); st.activityTimer = null; }
    st.activityNote = '';
    st.activityStartedAt = 0;
    const wrap = st.els.spinner?.parentElement;
    wrap?.classList.remove('visible');
    if (st.els.spinner) st.els.spinner.style.display = 'none';
    _syncActivityMirror('');
  }

  function _pipelineActivity(step) {
    return ({
      load_context: 'Loading context', prep_tools: 'Building tools', prep_history: 'Loading history',
      memory_search_start: 'Searching memory', build_prompt: 'Preparing', attachment: 'Reading attachment',
      load_tools: 'Building tools', turn_start: 'Thinking…', llm_call_start: 'Thinking…',
      memory_save_start: 'Saving memory',
      closer_start: 'Sending to Closer…', closer_audit_start: 'Closer auditing…',
      closer_llm_start: 'Closer writing…',
    })[step] || '';
  }

  function _setStatus(status) {
    if (st.closed) return;
    st.status = status;
    // A terminal state ends any pending stop — the label may revert to normal.
    if (status === 'done' || status === 'interrupted' || status === 'error' || status === 'idle') {
      st.stopPending = false;
    }
    const { dot, statusEl, pill, stopBtn, contBtn, sendBtn, input, root, actions, spinner } = st.els;
    dot.className = 'cw-dot ' + status;
    const busy = status === 'running' || status === 'starting';
    // While a stop is pending, keep "Stopping…" pinned — the backend is still
    // unwinding (stream tail, tool results, reconcile polls) and nothing may
    // flip the label back to "Working…" until a terminal state arrives.
    const stopping = st.stopPending && busy;
    statusEl.textContent = stopping ? 'Stopping…' : (STATUS_LABEL[status] || '');
    pill.classList.toggle('thinking', busy);
    spinner.style.display = busy ? '' : 'none';
    if (status === 'running') _setActivity(st.activityNote || 'Thinking…');
    else _clearActivity();
    const setControlVisible = (control, visible) => {
      control.hidden = !visible;
      if (visible) control.style.removeProperty('display');
      else control.style.setProperty('display', 'none', 'important');
    };
    const configured = st.els.pillConfiguredControls;
    const pillAllows = name => !configured || configured.has(name);
    setControlVisible(stopBtn, status === 'running' && pillAllows('stop'));
    // Continue is offered once a turn has finished and the agent is ready.
    const canContinue = st.hadTurn && !!st.agentId && !busy && pillAllows('continue');
    setControlVisible(contBtn, canContinue);
    // The above-pill row now contains activity only; Stop/Continue belong to
    // the pill and follow its chat_ui.json row/zone configuration.
    actions.style.display = status === 'running' ? '' : 'none';
    sendBtn.disabled = busy || !input.value.trim();
    root.classList.toggle('cw-done', status === 'done');
  }

  function _scrollToBottom() {
    const { body } = st.els;
    body.scrollTop = body.scrollHeight;
  }

  // ── Bubble helpers (shared by the WS path AND the DB-reconcile path) ──

  function _addBubble(role, text) {
    const b = appendChatSurfaceBubble(st.els.body, role, text || '');
    b.classList.add('cw-bubble');
    return b;
  }


  // SET absolute streaming text for `key` (idempotent). Creates the bubble if
  // needed. Used by WS stream (accumulated buffer) and the DB poll (absolute
  // row text) — both converge on the same element.
  function _seedStreaming(key, text) {
    if (!key) return;
    let bubble = st.bubbles.get(key);
    if (!bubble) {
      bubble = _addBubble('agent', '');
      bubble.classList.add('streaming');
      bubble.dataset.cwKey = key;
      st.bubbles.set(key, bubble);
    }
    st.turnBuffers.set(key, text);
    bubble.textContent = text;        // plain text while streaming; markdown on finalize
    _scrollToBottom();
  }

  function _finalizeKey(key, text) {
    let bubble = key ? st.bubbles.get(key) : null;
    const clean = (text || '').trim();
    if (!clean) { if (bubble) { bubble.remove(); if (key) st.bubbles.delete(key); } return; }
    if (!bubble) bubble = _addBubble('agent', '');
    while (bubble.firstChild) bubble.removeChild(bubble.firstChild);
    bubble.classList.remove('streaming');
    if (key) bubble.dataset.cwKey = key;
    _fillAgentBubble(bubble, clean);
    if (key) { st.bubbles.set(key, bubble); st.turnBuffers.delete(key); }
    st.lastAgentText = clean;
    _scrollToBottom();
  }

  function _markInterruptedKey(key) {
    let bubble = key ? st.bubbles.get(key) : null;
    if (!bubble) {
      const streaming = st.els.body.querySelectorAll('.cw-bubble.agent.streaming');
      bubble = streaming[streaming.length - 1] || null;
    }
    if (bubble) {
      while (bubble.firstChild) bubble.removeChild(bubble.firstChild);
      bubble.classList.remove('streaming');
      _fillAgentBubble(bubble, 'Stopped');
    }
    if (key) st.turnBuffers.delete(key);
  }

  // Any agent bubble still in the streaming state at turn end → finalize it
  // from its buffer so a missed step/response event never leaves a stale stub.
  function _finalizeDanglingStreams() {
    st.els.body.querySelectorAll('.cw-bubble.agent.streaming').forEach((b) => {
      const key = b.dataset.cwKey || '';
      _finalizeKey(key, (key && st.turnBuffers.get(key)) || b.textContent || '');
    });
  }

  // ── Shared transcript activity disclosure ──────────────────────────────
  // The shell, rows, and classes are the panel's real activity UI. The widget
  // owns only the session-local collection and header/embed chrome.
  function _renderToolGroup() {
    if (!st.activityItems.length) return;
    const group = _ensureActivityGroup();
    let container = group.querySelector('.bubble-tool-calls');
    if (!container) {
      const section = document.createElement('div');
      section.className = 'turn-section tool-section';
      container = document.createElement('div');
      container.className = 'bubble-tool-calls';
      const head = document.createElement('button');
      head.type = 'button';
      head.className = 'bubble-tool-calls-head';
      head.setAttribute('aria-expanded', 'false');
      const panel = document.createElement('div');
      panel.className = 'bubble-tool-calls-panel';
      head.addEventListener('click', (event) => {
        event.stopPropagation();
        const open = !container.classList.contains('open');
        container.classList.toggle('open', open);
        panel.classList.toggle('open', open);
        panel.style.maxHeight = open ? Math.min(panel.scrollHeight, 250) + 'px' : '';
        head.setAttribute('aria-expanded', String(open));
      });
      container.append(head, panel);
      section.appendChild(container);
      group.prepend(section);
    }
    const head = container.querySelector('.bubble-tool-calls-head');
    const panel = container.querySelector('.bubble-tool-calls-panel');
    panel.innerHTML = '';
    let toolIndex = 0;
    for (const entry of st.activityItems) {
      const isActivity = entry.kind === 'progress' || entry.kind === 'response' || entry.kind === 'system';
      const row = isActivity
        ? buildActivityEntryRow(entry, { messageActions: false })
        : buildToolRow(entry, toolIndex++);
      const rowHead = row.querySelector('.ca-tool-head');
      if (rowHead) {
        rowHead.addEventListener('click', (event) => {
          event.stopPropagation();
          entry.open = !entry.open;
          row.classList.toggle('open', entry.open);
          rowHead.setAttribute('aria-expanded', String(entry.open));
        });
      }
      panel.appendChild(row);
    }
    const tools = st.activityItems.filter(entry => !entry.kind).length;
    const updates = st.activityItems.filter(entry => entry.kind === 'progress').length;
    const responses = st.activityItems.filter(entry => entry.kind === 'response').length;
    const running = st.toolCalls.some(entry => entry.status === 'running');
    let label = (tools === 1 ? '1 tool call' : `${tools} tool calls`)
      + (updates ? ` / ${updates} ${updates === 1 ? 'update' : 'updates'}` : '')
      + (responses ? ` / ${responses} ${responses === 1 ? 'response' : 'responses'}` : '')
      + (running ? '…' : '');
    head.innerHTML = '<span class="bubble-tool-calls-label">' + label + '</span>'
      + ' <span class="bubble-tool-calls-chevron" aria-hidden="true">›</span>';
    const note = group.querySelector(':scope > .live-activity-note');
    if (note && note !== group.lastElementChild) group.appendChild(note);
    if (container.classList.contains('open')) {
      panel.style.maxHeight = Math.min(panel.scrollHeight, 250) + 'px';
    }
    _scrollToBottom();
  }

  // ── Turn lifecycle ──

  function _beginTurn() {
    st.settled = false;
    st.stopPending = false;    // a fresh turn — any earlier stop is moot
    st.toolCalls = [];
    st.activityItems = [];
    st.toolGroup = null;
    st.usage?.begin();
    // An expanded footer stays expanded through sends. In particular, an async
    // profile refresh must never leave the row hidden with its chevron open.
    if (st.els.below?.classList.contains('expanded')) st.els.below.hidden = false;
    if (st.collapseTimer) { clearTimeout(st.collapseTimer); st.collapseTimer = null; }
  }

  function _removeStreamingKey(key) {
    const bubble = key ? st.bubbles.get(key) : null;
    if (bubble?.classList.contains('streaming')) bubble.remove();
    if (key) {
      st.bubbles.delete(key);
      st.turnBuffers.delete(key);
    }
  }

  function _recordAgentStep(ev, key) {
    const content = (ev.content || st.turnBuffers.get(key) || '').trim();
    if (!content) { _removeStreamingKey(key); return; }
    // A mid-turn step is activity, never a visible message. The closing gate's
    // verdict is the ONLY promotion authority (see ``case 'response'``), so the
    // old substantive-answer heuristic — which promoted an answer-like step into
    // a standalone agent bubble — is gone.
    _removeStreamingKey(key);
    const entry = {
      kind: 'progress',
      id: key,
      content,
      createdAt: ev.created_at || ev.emit_time,
    };
    const existing = key
      ? st.activityItems.findIndex(item => item.kind && String(item.id || '') === String(key))
      : -1;
    if (existing >= 0) st.activityItems[existing] = entry;
    else st.activityItems.push(entry);
    _renderToolGroup();
    st.hadTurn = true;
  }


  // Normal completion. Idempotent per turn (guarded by st.settled).
  function _settleTurn(finalText) {
    if (st.settled || st.closed) return;
    st.settled = true;
    st.hadTurn = true;
    st.usage?.finish();
    _finalizeDanglingStreams();
    if (typeof finalText === 'string' && finalText.trim()) st.lastAgentText = finalText.trim();
    if (st.status !== 'interrupted' && st.status !== 'error') _setStatus('done');
    if (typeof onDone === 'function') {
      try { onDone(st.lastAgentText); } catch (_) { /* consumer callback — non-fatal */ }
    }
    // Shrink to the Done chip after a beat (cancelled if the pointer is over
    // the card — see the pointerenter handler).
    if (st.status === 'done') {
      if (st.collapseTimer) clearTimeout(st.collapseTimer);
      st.collapseTimer = setTimeout(() => {
        st.collapseTimer = null;
        if (!st.closed && st.status === 'done' && !st.els.root.matches(':hover')) minimize();
      }, 1500);
    }
  }

  // Only committed V2 public items may become widget bubbles. The item ID is
  // the reconciliation key across snapshots, replay, and live delivery.
  function _renderPublic(state) {
    if (st.closed || !st.els.root || state.sessionId !== st.sessionId) return;
    if (state.revoked) {
      _setStatus('error');
      st.els.input.disabled = true;
      return;
    }
    const currentIds = new Set(state.items.filter(item => !item.deleted).map(item => item.id));
    for (const [id, node] of st.publicNodes) {
      if (currentIds.has(id)) continue;
      node.remove();
      st.publicNodes.delete(id);
      st.publicSeen.delete(id);
      st.bubbles.delete(id);
    }
    for (const item of state.items) {
      if (item.deleted || st.publicSeen.get(item.id) === item.revision) continue;
      if (item.kind === 'attachment') {
        const artifact = renderPublishedArtifact(item.payload, payload =>
          requestPublishedArtifactGrant(payload, { headers: authHeaders() }));
        if (!artifact) continue;
        const previous = st.publicNodes.get(item.id);
        if (previous) previous.remove();
        const bubble = _addBubble('agent', '');
        bubble.appendChild(artifact);
        bubble.dataset.publicId = item.id;
        bubble.dataset.publicPosition = String(item.position);
        bubble.dataset.publicRevision = String(item.revision);
        const next = [...st.els.body.children].find(node =>
          Number(node.dataset.publicPosition || 0) > item.position);
        if (next) st.els.body.insertBefore(bubble, next);
        st.publicSeen.set(item.id, item.revision);
        st.publicNodes.set(item.id, bubble);
        st.hadTurn = true;
        continue;
      }
      if (!['user', 'answer', 'notice', 'failure'].includes(item.kind)) continue;
      if (typeof item.payload.text !== 'string') continue;
      const previous = st.publicNodes.get(item.id);
      if (previous) {
        previous.remove();
        st.publicNodes.delete(item.id);
        st.bubbles.delete(item.id);
      }
      let bubble = item.kind === 'user'
        ? st.pendingBubbles.get(item.payload.client_message_id) : null;
      if (bubble) st.pendingBubbles.delete(item.payload.client_message_id);
      else if (item.kind === 'user') bubble = _addBubble('user', item.payload.text);
      else {
        _finalizeKey(item.id, item.payload.text);
        bubble = st.bubbles.get(item.id);
      }
      if (!bubble) continue;
      bubble.dataset.publicId = item.id;
      bubble.dataset.publicPosition = String(item.position);
      bubble.dataset.publicRevision = String(item.revision);
      const next = [...st.els.body.children].find(node =>
        Number(node.dataset.publicPosition || 0) > item.position);
      if (next) st.els.body.insertBefore(bubble, next);
      st.publicSeen.set(item.id, item.revision);
      st.publicNodes.set(item.id, bubble);
      st.hadTurn = true;
    }
    const active = state.runs.some(run =>
      ['queued', 'running', 'finalizing', 'stopping'].includes(run.state));
    if (active) {
      st.settled = false;
      _setStatus('running');
    } else if (!st.settled) {
      const last = state.runs.at(-1);
      if (last?.state === 'cancelled') {
        st.settled = true;
        st.usage?.finish();
        _setStatus('interrupted');
      } else if (last?.state === 'failed') {
        st.settled = true;
        st.usage?.finish();
        _setStatus('error');
      } else if (last?.state === 'completed') _settleTurn(st.lastAgentText);
    }
  }

  function _sendStatus(event) {
    const detail = event.detail;
    if (st.closed || detail?.sessionId !== st.sessionId) return;
    const bubble = st.pendingBubbles.get(detail.id);
    if (!bubble) return;
    const labels = {
      pending: 'Pending acceptance', offline: 'Waiting for connection',
      accepted: 'Accepted', rejected: 'Message was not accepted',
    };
    bubble.title = labels[detail.status] || '';
    bubble.classList.toggle('pending', detail.status === 'pending' || detail.status === 'offline');
    if (detail.status === 'rejected') _markFailed(bubble, bubble.textContent || '');
  }

  // ── Send / interrupt ──

  async function send(text, attachmentIds) {
    const msg = (text || '').trim();
    // A file-only first message (no text) is still a valid send.
    const atts = Array.isArray(attachmentIds) ? attachmentIds.filter(Boolean) : [];
    if ((!msg && !atts.length) || st.closed || !st.els.root) return; // open() first
    if (st.ready) {
      try { await st.ready; } catch (_) { return; } // open() already showed the error
    }
    if (!st.agentId || st.closed) return;
    _beginTurn();
    const userBubble = _addBubble('user', msg || (atts.length ? '(file attached)' : ''));
    const clientMessageId = randomUUID();
    userBubble.dataset.clientMessageId = clientMessageId;
    st.pendingBubbles.set(clientMessageId, userBubble);
    _setStatus('running');
    // The user bubble shows what they typed (`msg`); the agent may receive a
    // wrapped form (e.g. Gen UI tags it with the page header) via transformMessage.
    let outgoing = msg;
    if (msg && typeof transformMessage === 'function') {
      try { outgoing = await transformMessage(msg); } catch (_) { outgoing = msg; }
      if (st.closed) return;
    }
    // Element pickup: if the launcher element pickup toggle is active, inject
    // the current fingerprint on EVERY send so the agent sees what the dot is
    // pointing at.
    const pickupFp = (app.elementPickupActive && app.elementPickupFingerprint) || null;
    try {
      await controlledChat.queueMessage(st.sessionId, st.agentId, {
          client_message_id: clientMessageId,
          text: outgoing || '(file attached)',
          display_text: msg || '(file attached)',
          execution_mode: executionMode,
          attachment_ids: atts,
          ...(pickupFp ? { app_control: pickupFp } : {}),
      });
      if (!st._sessionInitialized) {
        st._sessionInitialized = true;
        if (st.rememberSessionKey && st.sessionId) {
          try { localStorage.setItem(st.rememberSessionKey, st.sessionId); } catch (_) {}
        }
      }
      void controlledChat.drainOutbox().catch(() => {
        if (!st.closed) userBubble.title = 'Waiting for connection';
      });
    } catch (_) {
      _markFailed(userBubble, msg);
    }
  }

  function _markFailed(bubble, msg) {
    if (st.closed) return;
    st.settled = true;
    _setStatus('error');
    bubble.classList.add('failed');
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'cw-retry-btn';
    retry.innerHTML = icon('refresh-cw', { size: '12px' }) + ' Retry';
    retry.addEventListener('click', () => {
      bubble.remove();
      send(msg);
    });
    bubble.appendChild(retry);
  }

  function _sendFromInput() {
    const { input, pill, sendBtn } = st.els;
    const text = input.value.trim();
    if (!text) return;
    input.value = '';
    pill.classList.remove('has-text');
    sendBtn.disabled = true;
    send(text);
  }

  async function interrupt() {
    st.stopPending = true;   // pin "Stopping…" — no progress note may replace it
    if (st.els.statusEl) st.els.statusEl.textContent = 'Stopping…';
    _setActivity('Stopping…');
    try {
      await controlledChat.command('/sessions/' + encodeURIComponent(st.sessionId) + '/stop', {});
      await controlledChat.openSession(st.sessionId);
    } catch (_) { /* the committed run state determines the final control state */ }
  }

  // ── Drag to move / resize from edges & corners ──

  function _makeFloating() {
    const { root } = st.els;
    if (st.floating) return;
    const r = root.getBoundingClientRect();
    st.floating = true;
    root.classList.add('cw-floating');
    root.style.left = r.left + 'px';
    root.style.top = r.top + 'px';
    root.style.right = 'auto';
    root.style.bottom = 'auto';
    root.style.width = r.width + 'px';
    root.style.height = r.height + 'px';
  }

  function _dragLoop(onMove) {
    const up = () => {
      document.removeEventListener('pointermove', onMove);
      document.removeEventListener('pointerup', up);
      document.body.classList.remove('cw-dragging');
    };
    document.body.classList.add('cw-dragging');
    document.addEventListener('pointermove', onMove);
    document.addEventListener('pointerup', up);
  }

  function _onHeaderDown(e) {
    if (st.minimized) return;                          // chip click → restore
    if (e.target.closest('.cw-icon-btn')) return;      // header buttons
    if (e.button !== 0) return;
    e.preventDefault();
    _makeFloating();
    const r = st.els.root.getBoundingClientRect();
    const ox = e.clientX - r.left, oy = e.clientY - r.top;
    _dragLoop((ev) => {
      const nx = Math.max(0, Math.min(ev.clientX - ox, window.innerWidth - 48));
      const ny = Math.max(0, Math.min(ev.clientY - oy, window.innerHeight - 30));
      st.els.root.style.left = nx + 'px';
      st.els.root.style.top = ny + 'px';
    });
  }

  function _onResizeDown(e) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    _makeFloating();
    const dir = e.currentTarget.dataset.dir;
    const r = st.els.root.getBoundingClientRect();
    const s = { x: e.clientX, y: e.clientY, l: r.left, t: r.top, w: r.width, h: r.height };
    const MINW = 260, MINH = 200;
    _dragLoop((ev) => {
      const dx = ev.clientX - s.x, dy = ev.clientY - s.y;
      let l = s.l, t = s.t, w = s.w, h = s.h;
      if (dir.includes('e')) w = s.w + dx;
      if (dir.includes('s')) h = s.h + dy;
      if (dir.includes('w')) { w = s.w - dx; l = s.l + dx; }
      if (dir.includes('n')) { h = s.h - dy; t = s.t + dy; }
      if (w < MINW) { if (dir.includes('w')) l = s.l + (s.w - MINW); w = MINW; }
      if (h < MINH) { if (dir.includes('n')) t = s.t + (s.h - MINH); h = MINH; }
      const root = st.els.root;
      root.style.left = l + 'px'; root.style.top = t + 'px';
      root.style.width = w + 'px'; root.style.height = h + 'px';
    });
  }

  // ── Lifecycle ──

  function open() {
    if (st.closed || st.els.root) return widget;
    const layer = _ensureLayer();
    layer.appendChild(_build());
    _openWidgets.add(widget);
    _setStatus('starting');
    st.onSendStatus = _sendStatus;
    window.addEventListener('webagent-chat-send-status', st.onSendStatus);
    st.ready = (async () => {
      if (!st.agentId) {
        if (typeof ensureAgent !== 'function') throw new Error('No agent for chat widget');
        st.agentId = await ensureAgent();
      }
      await controlledChat.initialize(st.userId);
      await controlledChat.command('/sessions', {
        id: st.sessionId, agent_id: st.agentId,
        title: (st.sessionTargetName || 'New Session').slice(0, 120),
      });
      st.unregisterPublic = controlledChat.subscribe(st.sessionId, _renderPublic);
      await controlledChat.openSession(st.sessionId);
      return st.agentId;
    })();
    st.ready
      .then(() => {
        if (st.closed) return;
        if (initialMessage || (Array.isArray(initialAttachmentIds) && initialAttachmentIds.length)) {
          send(initialMessage, initialAttachmentIds);
        } else if (st.status === 'starting') _setStatus('idle');
      })
      .catch((e) => {
        if (st.closed) return;
        _setStatus('error');
        st.els.statusEl.title = 'Could not start the agent: ' + (e && e.message || e);
      });
    return widget;
  }

  function minimize() {
    if (st.closed || st.minimized) return;
    st.minimized = true;
    const { root } = st.els;
    // A floating card keeps its corner but lets the chip size to its content.
    if (st.floating) {
      st._floatSize = { w: root.style.width, h: root.style.height };
      root.style.width = '';
      root.style.height = '';
    }
    root.classList.add('minimized');
  }

  function restore() {
    if (st.closed || !st.minimized) return;
    st.minimized = false;
    const { root } = st.els;
    root.classList.remove('minimized');
    if (st.floating && st._floatSize) {
      root.style.width = st._floatSize.w;
      root.style.height = st._floatSize.h;
    }
    if (st.collapseTimer) { clearTimeout(st.collapseTimer); st.collapseTimer = null; }
    _scrollToBottom();
  }

  function close() {
    if (st.closed) return;
    st.closed = true;
    _openWidgets.delete(widget);
    if (st.collapseTimer) { clearTimeout(st.collapseTimer); st.collapseTimer = null; }
    _clearActivity();
    if (st.unregisterPublic) { st.unregisterPublic(); st.unregisterPublic = null; }
    if (st.onSendStatus) window.removeEventListener('webagent-chat-send-status', st.onSendStatus);
    if (st.els.root) st.els.root.remove();
    _maybeRemoveLayer();
    if (typeof onClose === 'function') {
      try { onClose(); } catch (_) { /* consumer callback — non-fatal */ }
    }
  }

  const widget = {
    open, close, send, interrupt, minimize, restore,
    get sessionId() { return st.sessionId; },
    get status() { return st.status; },
    get minimized() { return st.minimized; },
    get el() { return st.els.root || null; },
  };
  return widget;
}

// ── Shared: spawn a WebAgent chat from an ability-table pill ───────────────────
//
// The hybrid search/chat pill above BOTH ability tables (per-agent Abilities tab
// and admin Agent Settings) calls this on send. It opens a floating chat-widget
// talking to WebAgent the MANAGER (so it can build/configure agents and their
// abilities), carrying any attached files on the first message. The text is
// wrapped with the `agents_page_handoff` template from
// app/defaults/app-prompts.json (same tag the old footer pill used), with a
// hard-coded fallback. Resolving the manager + its session is delegated to
// `app.startWebagentSession` via the widget's `ensureAgent` hook.
//
// @param {object} arg
// @param {string} arg.text             - the user's typed message (may be empty if files attached)
// @param {string[]} [arg.attachmentIds] - attachment_ids to ride the first message
export async function spawnWebagentAbilityChat({ text, attachmentIds } = {}) {
  const raw = (text || '').trim();
  const atts = Array.isArray(attachmentIds) ? attachmentIds.filter(Boolean) : [];
  if (!raw && !atts.length) return null;

  // Tag with the source header from app-prompts.json (hard-coded fallback).
  let tagged = raw;
  try {
    const resp = await fetch(apiPath('/api/v1/app-prompts'));
    if (resp.ok) {
      const data = await resp.json();
      const tpl = (data.ui_handoffs || {}).agents_page_handoff?.template || '';
      tagged = tpl ? tpl.replace(/\{text\}/g, raw) : `[WebAgent Request | Source: Agents Page]: ${raw}`;
    } else {
      tagged = `[WebAgent Request | Source: Agents Page]: ${raw}`;
    }
  } catch (_) {
    tagged = `[WebAgent Request | Source: Agents Page]: ${raw}`;
  }

  const w = createChatWidget({
    title: 'WebAgent',
    iconName: 'bot',
    ensureAgent: app.startWebagentSession,
    initialMessage: tagged,
    initialAttachmentIds: atts,
  });
  w.open();
  return w;
}

// ── Shared: spawn a WebAgent chat from a PAGE-ASSISTANT pill ───────────────────
//
// Generic sibling of spawnWebagentAbilityChat for the per-page "advanced chat
// pill" assistants (App Settings, etc.). The CALLER assembles the full message —
// typically a page-context intro + the hovered area's prompt + the user's words,
// drawn from app/defaults/app-prompts.json → page_assistants.* — and passes it as
// `message`. This just opens a floating chat-widget talking to WebAgent the
// MANAGER (so it can edit pages, look and config), carrying any attachments on
// the first message. Resolving the manager + its session is delegated to
// `app.startWebagentSession` via the widget's `ensureAgent` hook.
//
// @param {object} arg
// @param {string} arg.message            - the fully-assembled message to send
// @param {string[]} [arg.attachmentIds]  - attachment_ids to ride the first message
// @param {string} [arg.title]            - widget title (defaults to "WebAgent")
export async function spawnWebagentPageChat({ message, attachmentIds, title } = {}) {
  const msg = (message || '').trim();
  const atts = Array.isArray(attachmentIds) ? attachmentIds.filter(Boolean) : [];
  if (!msg && !atts.length) return null;

  const w = createChatWidget({
    title: title || 'WebAgent',
    iconName: 'bot',
    ensureAgent: app.startWebagentSession,
    initialMessage: msg,
    initialAttachmentIds: atts,
  });
  w.open();
  return w;
}

'use strict';

// Task frames — draws a border around every run of chat bubbles that belong to
// the same "task" (one request + the agent's work + any approval/feedback the
// user adds along the way).
//
// SOURCE OF TRUTH = the backend grouping endpoint
// (GET /admin/settings/tasks/session/{id} in app/admin/tasks.py). That endpoint
// runs the SAME high-precision keyword rule this file used to mirror, PLUS a fast
// LLM tie-breaker that folds on-subject follow-ups ("make it bigger", "why did it
// do that") onto the current task — something a pure client-side rule can't do.
// So instead of re-deriving the grouping here, we fetch it and map each task to
// its user-message interaction ids; the draw then assigns every bubble to a task.
//
// Until that fetch lands (first paint, or endpoint unavailable) we fall back to a
// LOCAL text-only rule so a border still shows instantly. Both paths IGNORE
// synthetic "user" rows the runtime injects to drive the agent (orchestration
// spawn-finished / follow-up timers, event/automation triggers) — those must
// never open a new task, or a background wake-up shatters a session into one task
// per event. Kept in sync with _is_synthetic_user in app/admin/tasks.py.
//
// The frames are a NON-REPARENTING decorative overlay: absolutely-positioned
// boxes appended into the scroller (#chat-messages-inner) behind the bubbles.
// Nothing in the bubble DOM is touched, so live streaming, the DB-tail
// reconcile, the per-bubble actions, and the virtual scroller are all untouched.
// Because off-window bubbles become height-preserving placeholders (which keep
// their data-msg-id + height), a task's vertical extent stays correct even when
// part of it is virtualised.
// Module map for this folder: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { _messageCache } from './chat-message-cache.js';
import { _containerZoom } from './chat-virtual-scroll.js';

// ── Synthetic-user detection (mirror of app/admin/tasks.py) ──────────────────
// Injected driver messages stored as role='user' — never a task boundary.
const _SYNTHETIC_PREFIXES = [
  '[orchestration event]', '[event trigger]', '[automation event]', '[scheduled event]',
];
function _isSynthetic(text) {
  const low = (text || '').replace(/^\s+/, '').toLowerCase();
  return _SYNTHETIC_PREFIXES.some(p => low.startsWith(p));
}

// ── Local text-only fallback lexicon (mirror of app/admin/tasks.py) ──────────
// Only used until the backend grouping is fetched (or if it's unavailable).
const _REACTION_PHRASES = new Set([
  'yes', 'yep', 'yeah', 'y', 'ok', 'okay', 'k', 'sure', 'go', 'go ahead',
  'proceed', 'continue', 'do it', 'do that', 'please do', 'yes please',
  'sounds good', 'looks good', 'lgtm', 'perfect', 'great', 'nice', 'cool',
  'thanks', 'thank you', 'ty', 'approved', 'approve', 'confirm', 'confirmed',
  'make it so', 'ship it', 'no', 'nope', 'stop', 'wait', 'cancel', 'redo',
  'try again', 'fix it', 'not quite', "that's wrong", 'thats wrong',
]);
const _REACTION_PREFIXES = [
  'yes', 'yep', 'yeah', 'ok', 'okay', 'sure', 'go ahead', 'proceed',
  'do it', 'do that', 'please do', 'thanks', 'thank you', 'perfect',
  'great', 'no ', 'nope', 'redo', 'try again', 'fix ', 'looks good',
  'sounds good', 'approved', 'confirm',
];
const _CONTINUATION_PREFIXES = [
  'also', 'and also', 'plus', 'additionally', 'what about', 'how about',
  'one more', 'another', 'btw', 'by the way', 'oh and', 'can you also',
  'could you also',
];
const _ANAPHORIC_CONTINUATION_PREFIXES = [
  'what does that', 'what does this', 'what did that', 'what did this',
  'why did that', 'why did this', 'why does that', 'why does this',
  'how did that', 'how did this', 'how does that', 'how does this',
  'where did that', 'where did this', 'when did that', 'when did this',
  'can you explain that', 'can you explain this', 'explain that', 'explain this',
  'make that', 'make this', 'change that', 'change this',
  'update that', 'update this', 'fix that', 'fix this',
  'do the same', 'use that', 'use this',
];

function _norm(text) {
  return (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
}
function _isReaction(text) {
  const low = _norm(text);
  const stripped = low.replace(/^[\s.!?,]+|[\s.!?,]+$/g, '');
  const words = low ? low.split(' ').length : 0;
  const hit = words <= 2
    || _REACTION_PHRASES.has(stripped)
    || _REACTION_PREFIXES.some(p => stripped === p || stripped.startsWith(p));
  return hit && words <= 6;
}
function _isContinuation(text) {
  const low = _norm(text);
  const words = low ? low.split(' ').length : 0;
  return _CONTINUATION_PREFIXES.some(p => low.startsWith(p))
    || (words <= 18 && _ANAPHORIC_CONTINUATION_PREFIXES.some(p => low.startsWith(p)));
}
// A user message OPENS a new task unless it reads as a reaction or continuation.
function _startsNewTask(text) {
  return !_isReaction(text) && !_isContinuation(text);
}

// ── Backend grouping cache (userMsgId → task index, per session) ─────────────
let _grouping = { sessionId: null, map: new Map() };
let _refreshTimer = null;
let _refreshing = false;

function _scheduleRefresh(sid) {
  if (!sid || _refreshTimer) return;
  _refreshTimer = setTimeout(() => { _refreshTimer = null; _refreshGrouping(sid); }, 600);
}

// Fetch the authoritative (LLM-refined) grouping and rebuild the id→task map.
async function _refreshGrouping(sid) {
  if (_refreshing || !sid) return;
  _refreshing = true;
  try {
    const headers = { ...authHeaders() };
    const url = apiPath('/admin/settings/tasks/session/' + encodeURIComponent(sid) + '?include_turns=true');
    const resp = await fetch(url, { headers });
    if (!resp.ok) throw new Error('status ' + resp.status);
    const data = await resp.json();
    const map = new Map();
    if (Array.isArray(data.tasks)) {
      for (const tk of data.tasks) {
        for (const tn of (tk.turns || [])) {
          if (tn && tn.root_id != null) map.set(String(tn.root_id), tk.index);
        }
      }
    }
    if (app.currentSessionId === sid) {
      _grouping = { sessionId: sid, map };
      _scheduleDraw();
    }
  } catch (_) {
    // Endpoint unavailable → mark this session as "no map" so the draw falls
    // back to the local text-only rule (don't retry until the session changes).
    if (app.currentSessionId === sid) _grouping = { sessionId: sid, map: new Map() };
  } finally {
    _refreshing = false;
  }
}

// ── Element text fallback (when a bubble isn't in the message cache yet) ─────
function _userElementText(el) {
  // The "You" label is a separate <span class="label">; the rest is the text.
  const body = el.querySelector(':scope > .bubble-body');
  if (body) return body.textContent || '';
  const clone = el.cloneNode(true);
  clone.querySelectorAll('.label, .bubble-actions').forEach(n => n.remove());
  return clone.textContent || '';
}

// ── The draw pass ───────────────────────────────────────────────────────────
let _observer = null;
let _rafPending = false;

function _scheduleDraw() {
  if (_rafPending) return;
  _rafPending = true;
  requestAnimationFrame(() => { _rafPending = false; _drawTaskFrames(); });
}

function _drawTaskFrames() {
  const inner = app.chatMessages;
  if (!inner) return;
  const sid = app.currentSessionId;

  // Session changed since our last fetch → pull its grouping (async). Meanwhile
  // we draw with the local fallback so a border still appears immediately.
  if (sid && _grouping.sessionId !== sid) _scheduleRefresh(sid);

  // Map msgId → message so we can classify (and identify) placeholdered turns
  // whose text isn't in the DOM.
  const cache = _messageCache.get(sid);
  const byId = new Map();
  if (cache && Array.isArray(cache.messages)) {
    for (const m of cache.messages) if (m && m.id) byId.set(m.id, m);
  }

  const haveMap = _grouping.sessionId === sid && _grouping.map.size > 0;

  // Walk the rendered bubbles/placeholders in order, assigning each to a task.
  // Frame extents are accumulated per task KEY ("m<idx>" from the backend map,
  // "x<n>" from the local fallback) so the two paths never collide.
  // Coordinate space: the frames are absolutely positioned inside `inner`,
  // and CSS zoom (chat-zoom.js) scales everything inside `inner` — INCLUDING
  // the `top`/`height` we set on positioned children. So frame geometry must
  // be in UNZOOMED local px: take measured (zoomed) viewport offsets and
  // divide by the zoom factor. Mixing spaces (e.g. adding the scroller's
  // unzoomed scrollTop) makes frames drift with zoom AND scroll position.
  const innerRect = inner.getBoundingClientRect();
  const zoom = _containerZoom();
  const frames = new Map();          // key → {top, bottom, count}
  let curKey = null;
  let textCounter = -1;
  let sawUnmappedRealUser = false;

  const _ensure = (key) => {
    let f = frames.get(key);
    if (!f) { f = { top: Infinity, bottom: -Infinity, count: 0 }; frames.set(key, f); }
    return f;
  };

  for (const el of inner.children) {
    const isBubble = el.classList && el.classList.contains('chat-bubble');
    const isPlaceholder = el.classList && el.classList.contains('chat-bubble-placeholder');
    if (!isBubble && !isPlaceholder) continue;       // skip our own frames, etc.

    const msgId = el.getAttribute('data-msg-id');
    const msg = msgId ? byId.get(msgId) : null;
    const isUser = (el.classList.contains('user')) || (msg && msg.role === 'user');

    if (isUser) {
      const text = msg ? msg.content : _userElementText(el);
      if (haveMap) {
        if (msgId && _grouping.map.has(msgId)) {
          curKey = 'm' + _grouping.map.get(msgId);          // authoritative task
        } else if (_isSynthetic(text)) {
          if (curKey === null) curKey = 'm0';               // absorbed → stay put
        } else {
          // A genuine user turn the map hasn't seen yet (just arrived). Give it
          // its own box for now and refresh so the LLM can fold it properly.
          sawUnmappedRealUser = true;
          textCounter += 1;
          curKey = 'x' + textCounter;
        }
      } else if (!_isSynthetic(text)) {
        if (curKey === null || _startsNewTask(text)) {
          textCounter += 1;
          curKey = 'x' + textCounter;
        }
      } else if (curKey === null) {
        curKey = 'x0';                                       // leading synthetic
        textCounter = Math.max(textCounter, 0);
      }
    }
    if (curKey === null) continue;                            // leading non-user (welcome) — unframed

    // Viewport rects are zoom-scaled; the scroller's scrollTop is NOT (the
    // content div itself doesn't scroll). rect differences are scroll-invariant,
    // so divide them by zoom → natural local px that `top`/`height` expect.
    const r = el.getBoundingClientRect();
    const top = (r.top - innerRect.top) / zoom;
    const bottom = (r.bottom - innerRect.top) / zoom;
    const f = _ensure(curKey);
    if (top < f.top) f.top = top;
    if (bottom > f.bottom) f.bottom = bottom;
    f.count += 1;
  }

  if (sawUnmappedRealUser && sid) _scheduleRefresh(sid);

  // Rewrite the frame layer. Disconnect the observer so our own DOM writes don't
  // re-trigger the draw (infinite loop guard).
  if (_observer) _observer.disconnect();
  try {
    const old = inner.querySelectorAll(':scope > .task-frame');
    old.forEach(n => n.remove());
    for (const f of frames.values()) {
      if (!f || f.count === 0 || !isFinite(f.top) || f.bottom <= f.top) continue;
      const PAD = 5;  // breathing room above/below the bubbles
      const frame = document.createElement('div');
      frame.className = 'task-frame';
      frame.style.top = (f.top - PAD) + 'px';
      frame.style.height = (f.bottom - f.top + PAD * 2) + 'px';
      // Keep overlay-only nodes before transcript content.  If a frame is
      // appended between bubbles, the canonical bubble reorder and the frame
      // redraw continuously move the live activity group across it, producing
      // a visible vertical oscillation during streaming.
      inner.insertBefore(frame, inner.firstChild);
    }
  } finally {
    if (_observer) _observer.observe(inner, { childList: true, subtree: true });
  }
}

export function initTaskFrames() {
  const inner = app.chatMessages;
  if (!inner) return;

  // The frames are absolutely positioned inside the scroller's content box.
  if (getComputedStyle(inner).position === 'static') inner.style.position = 'relative';

  // Redraw whenever bubbles are added/removed/finalised (covers session render,
  // streaming finalise, reconcile, prepend, and placeholder ⇄ bubble recycle),
  // and whenever the panel or a bubble changes size.
  _observer = new MutationObserver(_scheduleDraw);
  _observer.observe(inner, { childList: true, subtree: true });

  inner.addEventListener('scroll', _scheduleDraw, { passive: true });
  if (typeof ResizeObserver !== 'undefined') {
    try { new ResizeObserver(_scheduleDraw).observe(inner); } catch (_) {}
  }
  window.addEventListener('resize', _scheduleDraw);

  app.redrawTaskFrames = _scheduleDraw;
  // Let other code (e.g. a turn finishing) force a re-pull of the grouping.
  app.refreshTaskGrouping = (sid) => _refreshGrouping(sid || app.currentSessionId);
  _scheduleDraw();
}

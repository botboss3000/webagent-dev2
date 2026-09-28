'use strict';

import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { _sessionsCache, _sortSessionsByPinnedOrder } from './session-list.js';
import { _messageCache, _CACHE_TTL_MS, _sessionFocus } from './chat-message-cache.js';
import { storageAdapter } from './storage/storage-adapter.js';
import { switchToSession, loadingSkeletonMarkup } from './session-core.js?v=515';

/**
 * Horizontal swipe navigation between sessions, in dropdown order.
 * Attaches touch/pointer handlers to the transcript wrapper for swipe
 * gestures, plus desktop prev/next buttons.
 */
function _initPinSwipeNavigation() {
  // Idempotent: initSessions / config reapplies must never stack a second set
  // of swipe + button handlers (each duplicate set would step the list again).
  if (_initPinSwipeNavigation.__bound) return;
  _initPinSwipeNavigation.__bound = true;

  const inner = app.chatMessages;
  const wrapper = inner && inner.parentElement;
  if (!inner || !wrapper) return;

  let _startX = 0, _startY = 0;
  let _active = false;
  let _activePointers = 0; // live multi-touch count — a 2nd finger aborts the swipe
  let _stashedDirection = 0;
  let _committing = false; // a past-threshold release is finishing its swipe
  let _nextPanel = null;
  let _warmAbort = null;       // AbortController for the in-flight neighbour warm
  let _warmScheduled = false;  // an idle warm pass is already queued
  const _WARM_NEWEST = 40;     // newest-N slice to pre-warm a neighbour at the bottom
  const _WARM_AROUND = 25;     // rows each side of a neighbour's saved scroll anchor

  function _sessionsInOrder() {
    // Mirror the dropdown's visible order: manually ordered live pins first,
    // then recent activity. Hidden sessions are excluded so navigation never
    // lands on a row the dropdown hides. Filter creates a fresh array, so the
    // in-place sort below never mutates the cache.
    return _sortSessionsByPinnedOrder(_sessionsCache.filter(s => !s.hidden));
  }

  function _targetSid(direction) {
    const all = _sessionsInOrder();
    if (!all.length) return null;
    const cur = all.findIndex(s => s.id === app.currentSessionId);
    let idx;
    if (cur === -1) idx = direction === 1 ? 0 : all.length - 1;
    else idx = (cur + direction + all.length) % all.length;
    return all[idx].id;
  }

  // ── Background neighbour warming ──────────────────────────────────────────
  // Pre-load the next + previous sessions (the two the header arrows / a swipe
  // can reach next), and ONLY when the app is idle — so arrow/swipe navigation
  // feels instant without ever competing with the active session.
  // Every warm fetch is idle-scheduled, runs one-at-a-time at low network
  // priority, is skipped while a turn is running or the active session is still
  // loading, and is aborted the instant a real load begins.

  function _idle(cb) {
    // requestIdleCallback only runs when the main thread is free; Safari / older
    // mobile lack it, so fall back to a short timer.
    if (typeof window !== 'undefined' && window.requestIdleCallback) {
      return window.requestIdleCallback(cb, { timeout: 2000 });
    }
    return setTimeout(cb, 1200);
  }

  function _appBusy() {
    if (app.isProcessing) return true;          // a turn is streaming → backend busy
    if (app._sessionLoadInFlight) return true;  // the on-screen session is loading
    if (typeof document !== 'undefined' && document.hidden) return true; // tab hidden
    return false;
  }

  function _isFreshlyCached(sid) {
    const c = _messageCache.get(sid);
    return !!(c && c.messages && (Date.now() - (c.loadedAt || 0)) < _CACHE_TTL_MS);
  }

  function _abortWarm() {
    try { if (_warmAbort) _warmAbort.abort(); } catch (_) {}
    _warmAbort = null;
  }

  async function fetchMessagesForCache(sid, signal) {
    const token = localStorage.getItem('auth_token');
    // Mirror what a real open would download so navigation is a pure cache hit:
    // centre on the neighbour's saved scroll position when it has one, else the
    // newest slice. Always light (headings only — no tool-call bodies). Low
    // network priority so it yields to the active session's own requests.
    const focus = _sessionFocus.get(sid);
    let limit = _WARM_NEWEST;
    let extra = '';
    if (focus && focus.atBottom === false && focus.anchorMsgId) {
      limit = _WARM_AROUND;
      extra = `&around_id=${encodeURIComponent(focus.anchorMsgId)}`;
    } else {
      extra = '&complete_turn_boundary=true';
    }
    const url = apiPath(`/api/v1/db/session-messages?db=user.db&session_id=${encodeURIComponent(sid)}&limit=${limit}&light=1${extra}${token ? '&token=' + encodeURIComponent(token) : ''}`);
    try {
      const res = await fetch(url, { signal: signal || undefined, priority: 'low' });
      const data = await res.json();
      if (!data.restricted && data.messages) {
        _messageCache.set(sid, {
          messages: data.messages,
          hasMore: !!data.has_more,
          hasNewer: !!data.has_newer,
          light: data.light !== false,
          maxSeq: data.messages.reduce(
            (max, msg) => Math.max(max, Number(msg && msg.session_seq) || 0), 0,
          ),
          authorityMaxSeq: data.max_session_seq || 0,
          contextTokens: data.context_tokens || 0,
          loadedAt: Date.now(),
        });
        // Persist the warm into the hybrid IndexedDB cache too, so a neighbour
        // stays pre-loaded across a page reload / tab close / offline session.
        // No-op outside hybrid mode; best-effort.
        try {
          await storageAdapter.mergeInteractionsIntoCache(
            sid, data.messages, data.manifest || null,
          );
        } catch (_) { /* non-fatal — memory warm already applied */ }
      }
    } catch (_) { /* aborted, offline, or restricted — non-fatal */ }
  }

  function _warmNeighbors() {
    if (_warmScheduled) return;        // coalesce rapid arrow-scrubbing into one pass
    _warmScheduled = true;
    _idle(async () => {
      _warmScheduled = false;
      if (_appBusy()) return;          // try again on the next switch / idle trigger
      const ids = [];
      for (const id of [_targetSid(1), _targetSid(-1)]) {
        if (id && id !== app.currentSessionId && !ids.includes(id) && !_isFreshlyCached(id)) {
          ids.push(id);
        }
      }
      if (!ids.length) return;
      _abortWarm();
      _warmAbort = (typeof AbortController !== 'undefined') ? new AbortController() : null;
      const signal = _warmAbort && _warmAbort.signal;
      for (const id of ids) {
        if (_appBusy()) break;         // active work appeared mid-pass — yield now
        await fetchMessagesForCache(id, signal);
      }
    });
  }

  // Let the active-session loader drive warming: it calls warmNeighborSessions
  // once a load settles, and abortNeighborWarm the instant a new load begins.
  app.warmNeighborSessions = _warmNeighbors;
  app.abortNeighborWarm = () => { _abortWarm(); _warmScheduled = false; };

  function _renderNextPanel(sid, container, direction) {
    // Directional hint screen — no mock bubbles. The real session content
    // loads when switchToSession runs after the swipe commits.
    const sessions = _sessionsCache;
    const sess = sessions.find(s => s.id === sid);
    const label = sess && sess.title ? sess.title : (sess && sess.id ? sess.id.slice(0, 8) : '');
    // A large thick arrow points the way the swipe is travelling, centred in
    // the panel. Beside it, "Session: <name>" reads top-to-bottom and hugs the
    // edge that becomes visible first — the LEFT edge when the next session
    // rides in from the right (direction 1), the RIGHT edge when the previous
    // session rides in from the left (direction -1).
    const edge = direction === 1 ? 'left' : 'right';
    const arrowPath = direction === 1
      ? '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/>'   // ← next
      : '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>';    // → previous
    container.innerHTML =
      '<div class="pin-swipe-arrow">' +
        '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + arrowPath + '</svg>' +
      '</div>' +
      (label ? '<span class="pin-swipe-edge-hint" data-edge="' + edge + '">Session: ' + label + '</span>' : '');
  }

  function _ensureNextPanel(direction) {
    const sid = _targetSid(direction);
    if (!sid) { _cleanNextPanel(); return null; }
    if (_nextPanel && _nextPanel.dataset.sid === sid) return _nextPanel;

    _cleanNextPanel();
    const panel = document.createElement('div');
    panel.className = 'chat-messages-next';
    panel.dataset.sid = sid;
    // Fixed positioning escapes ALL ancestor overflow clipping (#chat-messages,
    // #chat-panel both have overflow:hidden). The panel sits exactly over the
    // chat messages area, tracked live so it stays aligned if the layout shifts.
    const r = wrapper.getBoundingClientRect();
    panel.style.position = 'fixed';
    panel.style.top = r.top + 'px';
    panel.style.left = r.left + 'px';
    panel.style.width = r.width + 'px';
    panel.style.height = r.height + 'px';
    panel.style.zIndex = '1000';
    panel.style.transform = 'translateX(' + (direction === 1 ? '100%' : '-100%') + ')';
    panel.scrollTop = 0;
    _renderNextPanel(sid, panel, direction);
    document.body.appendChild(panel);
    _nextPanel = panel;
    return panel;
  }

  function _cleanNextPanel() {
    if (_nextPanel) { _nextPanel.remove(); _nextPanel = null; }
  }

  function _toast(msg) {
    const old = wrapper.querySelector('.pin-swipe-toast');
    if (old) old.remove();
    const t = document.createElement('div');
    t.className = 'pin-swipe-toast';
    t.textContent = msg;
    wrapper.prepend(t);
    requestAnimationFrame(() => t.classList.add('show'));
    setTimeout(() => t.classList.remove('show'), 1800);
    setTimeout(() => t.remove(), 2300);
  }

  function _go(direction) {
    _cleanNextPanel();
    const all = _sessionsInOrder();
    if (!all.length) { _toast('No sessions available'); return; }
    const cur = all.findIndex(s => s.id === app.currentSessionId);
    let idx;
    if (cur === -1) idx = direction === 1 ? 0 : all.length - 1;
    else idx = (cur + direction + all.length) % all.length;
    switchToSession(all[idx].id);
  }

  // Land the swipe on the arrow+text screen: swap the hidden old transcript
  // for the phantom-skeleton, then crossfade the panel away so the skeleton
  // fades in beneath it. Only then switch sessions — the loaded transcript
  // fades in on top of the skeleton (see _renderSessionWindowed).
  function _commitSwipe(dir) {
    _committing = false; // commit is terminal — nothing left to cancel
    _stashedDirection = 0;
    // The old transcript is fully off-screen by now (commit fires at 500ms,
    // after the 400ms slide). Swap its content for the skeleton BEFORE bringing
    // the container back to center — content swap and transform reset are
    // synchronous, so the old session never renders at center for a frame.
    inner.style.transition = 'none';
    inner.innerHTML = loadingSkeletonMarkup();
    inner.style.transform = 'translateX(0)';
    inner.style.opacity = '1';
    inner.style.willChange = '';
    const panel = _nextPanel;
    const go = () => {
      app._swipeFadeIn = true;
      // Backstop: never let the fade flag leak into a later, unrelated render.
      setTimeout(() => { app._swipeFadeIn = false; }, 6000);
      _go(dir);
    };
    if (panel) {
      panel.style.transition = 'opacity 0.22s ease';
      panel.style.opacity = '0';
      setTimeout(() => { _cleanNextPanel(); go(); }, 240);
    } else {
      go();
    }
  }

  // Animate to the session in `direction` with the same springy overshoot
  // used by the swipe gesture, then switch.
  function _animateToSession(direction) {
    _cleanNextPanel();
    const all = _sessionsInOrder();
    if (!all.length) { _toast('No sessions available'); return; }
    const cur = all.findIndex(s => s.id === app.currentSessionId);
    if (cur === -1) { _go(direction); return; }
    _stashedDirection = direction;
    _ensureNextPanel(direction);

    const w = wrapper.clientWidth || 380;
    const outPx = -direction * w;  // direction: 1 = next (-w), -1 = prev (w)

    _committing = true; // guard the reset/cancel paths — this must finish
    // Clean ease-out finish (no overshoot): the swipe completes fully and
    // nothing bounces back toward center.
    inner.style.transition = 'transform 0.4s cubic-bezier(.25,.8,.25,1), opacity 0.35s ease';
    inner.style.transform = 'translateX(' + outPx + 'px)';
    inner.style.opacity = '0.3';
    if (_nextPanel) {
      _nextPanel.style.transition = 'transform 0.4s cubic-bezier(.25,.8,.25,1), opacity 0.35s ease';
      _nextPanel.style.transform = 'translateX(0)';
      _nextPanel.style.opacity = '1';
    }
    // Commit on a single timeout AFTER both transitions settle (transform
    // ~400ms, opacity ~350ms). A transitionend listener is unreliable here:
    // it fires once PER property, so the opacity end (~350ms) would trigger
    // the commit ~50ms early, mid-slide, snapping the old transcript back to
    // center. One 500ms timeout is exact and immune to that race.
    setTimeout(() => _commitSwipe(direction), 500);
  }

  function _applyOffset(dx) {
    // Edge-swipe semantics: a swipe STARTING on the right edge travels LEFT
    // (dx < 0) → next session (down the list); a swipe from the left edge
    // travels RIGHT (dx > 0) → previous session (up the list).
    const dir = dx > 0 ? -1 : 1;
    if (dir !== _stashedDirection) {
      _stashedDirection = dir;
      _cleanNextPanel();
      _ensureNextPanel(dir);
    }
    const w = wrapper.clientWidth || 380;
    const frac = Math.max(-1, Math.min(1, dx / w));
    const px = Math.round(frac * w);
    inner.style.transition = 'none';
    inner.style.transform = 'translateX(' + px + 'px)';
    inner.style.opacity = String(Math.max(0.3, 1 - Math.abs(frac)));
    if (_nextPanel) {
      _nextPanel.style.transition = 'none';
      // Panel rides in from the edge the swipe started on: next from the
      // right (+100%), prev from the left (-100%), easing toward 0 as the
      // drag progresses.
      const offset = dir === 1 ? (1 + frac) * 100 : (-1 + frac) * 100;
      _nextPanel.style.transform = 'translateX(' + Math.round(offset) + '%)';
      _nextPanel.style.opacity = String(Math.min(1, Math.abs(frac) * 2));
    }
  }

  function _resetOffset(instant) {
    if (_committing) return; // a committed swipe must finish — never spring back
    inner.style.transition = instant ? 'none' : 'transform 0.3s cubic-bezier(.22,.68,0,1), opacity 0.3s ease';
    inner.style.transform = 'translateX(0)';
    inner.style.opacity = '1';
    inner.style.willChange = '';
    if (_nextPanel) {
      _nextPanel.style.transition = instant ? 'none' : 'transform 0.3s cubic-bezier(.22,.68,0,1), opacity 0.25s ease';
      _nextPanel.style.transform = 'translateX(' + (_stashedDirection === 1 ? '100%' : '-100%') + ')';
      _nextPanel.style.opacity = '0';
    }
    _stashedDirection = 0;
  }

  setTimeout(_warmNeighbors, 2000);

  wrapper.addEventListener('pointerdown', (e) => {
    _activePointers++;
    if (e.pointerType !== 'touch' && e.pointerType !== 'pen') return;
    if (e.target.closest('#chat-input-area, button, input, textarea, select, .chat-pill, #chat-header')) return;
    // A second finger has landed — that's a pinch, owned by chat-zoom.js.
    // Abort any swipe in progress and ignore further moves until all fingers
    // lift, so a pinch can never flip to the next/previous session.
    if (_activePointers > 1) { _active = false; _resetOffset(true); return; }
    // Capture the pointer so the drag keeps tracking — and pointerup always
    // lands here — even when the finger drifts off the wrapper. This stops
    // stray pointerleave / pointercancel events from springing the swipe back.
    try { wrapper.setPointerCapture(e.pointerId); } catch (_) {}
    _startX = e.clientX; _startY = e.clientY;
    _active = true;
    _warmNeighbors();
  });

  wrapper.addEventListener('pointermove', (e) => {
    if (!_active || _activePointers > 1) return;
    const dx = e.clientX - _startX;
    const dy = e.clientY - _startY;
    if (Math.abs(dy) > Math.abs(dx) + 10) { _active = false; _resetOffset(); return; }
    if (Math.abs(dx) > 5) _applyOffset(dx);
  });

  wrapper.addEventListener('pointerup', (e) => {
    _activePointers = Math.max(0, _activePointers - 1);
    try { wrapper.releasePointerCapture(e.pointerId); } catch (_) {}
    if (!_active) { _resetOffset(true); return; }
    _active = false;
    const dx = e.clientX - _startX;
    const w = wrapper.clientWidth || 380;
    const frac = dx / w;
    const THRESHOLD = 0.35;  // 35% of width triggers a session change

    if (Math.abs(frac) > THRESHOLD) {
      // Edge-swipe semantics (same as _applyOffset): right-edge swipe travels
      // LEFT (dx < 0) → next (down); left-edge swipe travels RIGHT (dx > 0)
      // → previous (up).
      const dir = dx > 0 ? -1 : 1;
      // The next panel is already built from _applyOffset, so reuse it.
      // Animate both panels to completion with a clean ease-out finish.
      // Content continues in the drag direction (outPx = -dir * w), so a
      // right-edge swipe (content slides left, outPx = -w) reveals the next
      // session riding in from the right, and a left-edge swipe mirrors it.
      const outPx = -dir * w;
      _committing = true; // guard the reset/cancel paths — this must finish
      // Clean ease-out finish (no overshoot): the swipe completes fully and
      // nothing bounces back toward center.
      inner.style.transition = 'transform 0.4s cubic-bezier(.25,.8,.25,1), opacity 0.35s ease';
      inner.style.transform = 'translateX(' + outPx + 'px)';
      inner.style.opacity = '0.3';
      if (_nextPanel) {
        _nextPanel.style.transition = 'transform 0.4s cubic-bezier(.25,.8,.25,1), opacity 0.35s ease';
        _nextPanel.style.transform = 'translateX(0)';
        _nextPanel.style.opacity = '1';
      }
      // Commit on a single timeout AFTER both transitions settle — same
      // per-property transitionend hazard as _animateToSession (opacity ends
      // at ~350ms, before the transform's ~400ms), so no transitionend
      // listener here either. One 500ms timeout is exact and race-free.
      setTimeout(() => _commitSwipe(dir), 500);
    } else {
      _resetOffset(false);
    }
  });

  wrapper.addEventListener('pointercancel', (e) => {
    _activePointers = Math.max(0, _activePointers - 1);
    try { wrapper.releasePointerCapture(e.pointerId); } catch (_) {} _active = false; _resetOffset(false); });
  wrapper.addEventListener('pointerleave', () => { _active = false; _resetOffset(false); });

  const prevBtn = document.getElementById('session-prev-btn');
  const nextBtn = document.getElementById('session-next-btn');
  if (prevBtn) {
    prevBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _animateToSession(-1);   // left arrow → previous session (up the list)
    });
  }
  if (nextBtn) {
    nextBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      _animateToSession(1);    // right arrow → next session (down the list)
    });
  }

  // Also route the dynamic chat-control element events. ui/chat/elements/
  // session-nav.js and ui/chat-controls/controls/session-{prev,next}.js
  // dispatch these when the config system mounts the element modules instead
  // of the static data-header-control buttons, so whichever header control is
  // active drives the same direction mapping.
  document.addEventListener('chat-control:session-prev', (e) => {
    e.stopPropagation();
    _animateToSession(-1);
  });
  document.addEventListener('chat-control:session-next', (e) => {
    e.stopPropagation();
    _animateToSession(1);
  });
}

export { _initPinSwipeNavigation };

'use strict';

// Chat panel wiring — initChat(): binds chat input/keyboard/send, execution-mode
// toggle, continue/stop buttons (500ms poll), scroll-to-bottom chevron + the
// top-of-panel jump nav (start of session / last user message), gate +
// draft restore. The top-level entry for the chat side panel.
// Module map for this folder: ui/chat/js/README.md.

import { _refreshLucideIcons, keepPillFocusOnFooterTap } from '../../shared/js/dom-utils.js';
import { applyRubberBand } from '../../shared/js/rubber-band.js';
import { app } from '../../shared/js/state.js';
import { apiPath } from '../../shared/js/config.js';
import { getAccessMode, fetchAccessMode, authHeaders } from '../../shared/js/left-login.js';
import { chatMsg, isDebugMode, getAgentChatUi } from '../../shared/js/app-prompts.js';
import { addChatBubble, updateLastBubble, linkifyText, _renderMarkdownBody, _refreshAllBubbleTimes } from './chat-bubble.js';
import { _addBubbleActions, _finalizeBubbleSections } from './chat-bubble-actions.js';
import {
  appendStreamToActiveBubble,
  finalizeAgentResponse,
  ensureStreamingBubbleForActiveTurn,
  finalizeAgentStep,
  markAgentInterrupted,
  seedStreamingBubble,
  attachToolCallsToLastBubble,
} from './chat-stream.js?v=260';
import {
  sendMessage,
  sendStopMessage,
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
  _prewarm,
} from './chat-send.js';
import { initChatTunnel } from '../../shared/js/chatTunnel.js';
import { initTerminalChatEngine } from './chat-terminal-engine.js';
import { kvRead, kvWrite, kvDelete } from '../../shared/js/kv-ui-state.js';
import { initChatComponents } from './chat-components.js';
import { switchFooterMode, getFooterMode } from '../../chat-controls/chat-controls-config.js';
import { initComposerStore } from './composer-store.js';
import { mountReactComposerIsland } from './composer-react-island.js';
import { forwardFooterGestures } from './footer-gesture-forward.js';

/** Whether the user has explicitly locked auto-scroll. */
let _scrollLocked = true;
/** Set to true while transcript code is doing a programmatic scroll. */
let _programmaticScroll = false;

// The newest user turn owns the viewport while the agent works. This is an
// element reference rather than a message id because optimistic bubbles are
// deliberately adopted/re-stamped by the websocket and reconcile paths.
let _activeTurnAnchor = null;
let _pendingTurnAnchor = false;
let _turnAnchorReleasedByUser = false;
// When the current turn was anchored. A re-land requested by a later layout
// change (composer resize, keyboard) must not drag a reader who has scrolled
// away back into place — see the drift guard in the land() closure below.
let _turnAnchorArmedAt = 0;
// The scroll listener uses this to distinguish the anchor's own placement from
// the reader's first intentional move down through the current run.
let _lastObservedScrollTop = 0;
// A scroll event alone is not reader intent: mobile keyboard focus, visual
// viewport changes, and browser reflow can all move the transcript.
let _transcriptScrollIntentAt = 0;
let _transcriptTouchY = null;
// Timestamp of the last intentional, non-programmatic transcript movement. Used
// to keep a soft-anchor settle inside the same gesture (see _queueUserAnchorSnap).
let _lastTranscriptScrollDirectionAt = 0;
const _TRANSCRIPT_SCROLL_INTENT_MS = 800;
// The reader counts as "at the tail" within this many px of the bottom. Tail-follow
// is armed from this live position, never from a flag set once at session load.
const _TAIL_FOLLOW_NEAR_PX = 40;

// A programmatic scrollTop write still emits a real scroll event, and that event
// must not read as reader intent. Arm the guard for the write and clear it on a
// short timer. EVERY writer must clear it — a writer that leaves it set makes all
// later gestures read as programmatic, which silently kills anchor release, scroll
// direction tracking and the soft snap (the "glitchy, won't stay put" symptom).
let _programmaticScrollTimer = null;
function _beginProgrammaticScroll(durationMs = 100) {
  _programmaticScroll = true;
  window.clearTimeout(_programmaticScrollTimer);
  _programmaticScrollTimer = window.setTimeout(() => {
    _programmaticScrollTimer = null;
    _programmaticScroll = false;
  }, durationMs);
}

function _hasActiveTurnAnchor() {
  if (_activeTurnAnchor && _activeTurnAnchor.isConnected) return true;
  _activeTurnAnchor = null;
  return false;
}

function _releaseTurnAnchor({ userScrolled = false } = {}) {
  _pendingTurnAnchor = false;
  if (userScrolled) _turnAnchorReleasedByUser = true;
  if (_activeTurnAnchor) _activeTurnAnchor.classList.remove('turn-anchor');
  _activeTurnAnchor = null;
}

// Keyboard focus and composer reflow can move the scroller without reader
// intent. Keep the active turn landed after that layout settles.
function _reanchorActiveTurnAfterLayout() {
  if (!_hasActiveTurnAnchor() || typeof app.anchorUserTurn !== 'function') return;
  requestAnimationFrame(() => {
    if (_hasActiveTurnAnchor()) app.anchorUserTurn(_activeTurnAnchor);
  });
}

/** Gap (px) the landed message sits below the viewport top — also the slack
 *  used when deciding which messages count as "above" the current top. */
// The active profile supplies a minimum landing gap. The actual readable top
// also clears the live floating-header stack, which may be taller on desktop.
let _USER_STEP_PAD = 62;
let _configuredUserStepPad = 62;

function _applyUserTurnAnchorConfig() {
  const ui = getAgentChatUi();
  const panel = document.getElementById('chat-panel');
  const surface = window.__CHAT_PORTAL__
    ? ui.chat_widget
    : ((panel ? panel.clientWidth : window.innerWidth) <= 768 ? ui.chat_mobile : ui.chat_desktop);
  const config = {
    ...(ui.chat_common?.user_turn_anchor || {}),
    ...(surface?.user_turn_anchor || {}),
  };
  const configuredGap = Number(config.landing_gap_px);
  _configuredUserStepPad = Number.isFinite(configuredGap) && configuredGap >= 0
    ? configuredGap
    : 62;
  _USER_STEP_PAD = _configuredUserStepPad;
  app._USER_STEP_PAD = _USER_STEP_PAD;
}

// Grace window after arming during which the anchor may land regardless of how
// far the target still is: the very first landing has to carry the new user
// bubble from the tail up to the readable top. After this, a re-land only
// proceeds while the anchored turn is still within a viewport of the reader.
const _TURN_ANCHOR_GRACE_MS = 500;

// Settles releases near a user turn without imposing CSS snap points globally.
const _USER_ANCHOR_SNAP_PX = 100;
const _USER_ANCHOR_SNAP_SETTLE_MS = 120;
const _USER_ANCHOR_SNAP_TOLERANCE_PX = 2;
// A smooth scroll has no duration contract. Keep its ownership until scrollend,
// with a conservative fallback, rather than releasing it after a guessed timer.
const _USER_ANCHOR_SNAP_COMPLETE_FALLBACK_MS = 1000;
let _userAnchorSnapTimer = null;
let _userAnchorSnapInFlight = null;
let _userAnchorSnapFallbackTimer = null;

function _finishUserAnchorSnap() {
  if (!_userAnchorSnapInFlight) return false;
  const { el } = _userAnchorSnapInFlight;
  _userAnchorSnapInFlight = null;
  window.clearTimeout(_userAnchorSnapFallbackTimer);
  _userAnchorSnapFallbackTimer = null;
  if (el) _lastObservedScrollTop = el.scrollTop;
  return true;
}

function _cancelUserAnchorSnap() {
  window.clearTimeout(_userAnchorSnapTimer);
  _userAnchorSnapTimer = null;
  _finishUserAnchorSnap();
}

function _snapToNearbyUserAnchor(el) {
  if (!el || _programmaticScroll || _userAnchorSnapInFlight) return;
  const viewportTop = el.getBoundingClientRect().top;
  const current = el.scrollTop;
  const maxScrollTop = Math.max(0, el.scrollHeight - el.clientHeight);
  let nearest = null;
  for (const bubble of el.querySelectorAll('.chat-bubble.user')) {
    const target = Math.min(maxScrollTop, Math.max(0,
      current + bubble.getBoundingClientRect().top - viewportTop - _USER_STEP_PAD));
    // Settle to whichever user turn sits nearest the readable-top pin, on EITHER
    // side of the current position. Gating this on the reader's direction left an
    // upward scroll that stopped with a turn just past the pin (its landing
    // target only just below the current position) with no pull at all, so
    // scrolling up read as a free scroll while scrolling down anchored. A settle
    // must anchor the message identically in both directions; the
    // ±_USER_ANCHOR_SNAP_PX window is what keeps the pull gentle and local.
    const distance = Math.abs(target - current);
    if (distance > _USER_ANCHOR_SNAP_TOLERANCE_PX
        && distance <= _USER_ANCHOR_SNAP_PX
        && (!nearest || distance < nearest.distance)) nearest = { target, distance };
  }
  if (!nearest) return;
  const { target } = nearest;
  const behavior = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth';
  _userAnchorSnapInFlight = { el, target };
  if (behavior === 'auto') {
    el.scrollTop = target;
    window.setTimeout(_finishUserAnchorSnap, 0);
  } else {
    el.scrollTo({ top: target, behavior: 'smooth' });
    _userAnchorSnapFallbackTimer = window.setTimeout(
      _finishUserAnchorSnap, _USER_ANCHOR_SNAP_COMPLETE_FALLBACK_MS,
    );
  }
}

function _queueUserAnchorSnap(el) {
  if (_userAnchorSnapInFlight) return;
  window.clearTimeout(_userAnchorSnapTimer);
  _userAnchorSnapTimer = window.setTimeout(() => {
    _userAnchorSnapTimer = null;
    if (!_userAnchorSnapInFlight
        && performance.now() - _lastTranscriptScrollDirectionAt < _TRANSCRIPT_SCROLL_INTENT_MS) {
      _snapToNearbyUserAnchor(el);
    }
  }, _USER_ANCHOR_SNAP_SETTLE_MS);
}

/** The scrollTop that brings the nearest user message *above* the current
 *  viewport top to the top — i.e. one step up. Returns null when there's no
 *  user message above the current position (nothing more to step to). Uses
 *  live rects (the bubbles' offsetParent isn't the scroller, so offsetTop
 *  would be wrong). Each click re-anchors on wherever the user now is, so
 *  repeated clicks walk up one user message at a time. */
function _prevUserTarget(el) {
  if (!el) return null;
  const innerTop = el.getBoundingClientRect().top;
  const cur = el.scrollTop;
  const users = el.querySelectorAll('.chat-bubble.user');
  let best = null;
  for (const ub of users) {
    const offset = cur + (ub.getBoundingClientRect().top - innerTop);
    // Strictly above the current top (small slack absorbs float jitter and the
    // landing pad, so the just-landed message never re-selects itself).
    if (offset < cur - 2) {
      const target = Math.max(0, offset - _USER_STEP_PAD);
      if (best === null || target > best) best = target;
    }
  }
  return best;
}

/** Mirror _prevUserTarget for callers that need the next rendered user row. */
function _nextUserTarget(el) {
  if (!el) return null;
  const innerTop = el.getBoundingClientRect().top;
  const cur = el.scrollTop;
  const users = el.querySelectorAll('.chat-bubble.user');
  let best = null;
  for (const ub of users) {
    const offset = cur + (ub.getBoundingClientRect().top - innerTop);
    const target = Math.max(0, offset - _USER_STEP_PAD);
    // Compare canonical landing positions, not raw bubble tops. The current
    // user turn sits _USER_STEP_PAD below the viewport top, and must not be
    // selected again when the reader asks for the next turn.
    if (target > cur + 2) {
      if (best === null || target < best) best = target;
    }
  }
  return best;
}

function _scrollToBottomIfNear(el) {
  if (!el) return;
  // The active user turn deliberately owns the viewport while activity and
  // response content arrive below it. Do not tail-follow until the user
  // explicitly scrolls away from that turn.
  if (_pendingTurnAnchor || _hasActiveTurnAnchor()) return;
  // 2-stage: new content arriving while pinned at the bottom invalidates the
  // stage-2 arm — the user's next over-scroll is a fresh stage 1.
  if (typeof app._resetBottomArm === 'function') app._resetBottomArm('content added near bottom');
  if (!_scrollLocked) return;
  const _ = el.offsetHeight;
  _beginProgrammaticScroll();
  el.scrollTop = el.scrollHeight;
}

// ── Delegated pill resize listeners ────────────────────────────────────────
let _resizeTimer = null;
function _debouncedAutoResizePill(el) {
  if (_resizeTimer) clearTimeout(_resizeTimer);
  _resizeTimer = setTimeout(() => {
    _resizeTimer = null;
    _autoResizePill(el);
  }, 100);
}

document.addEventListener('input', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _debouncedAutoResizePill(t);
  }
}, true);

document.addEventListener('scroll', (e) => {
  const t = e.target;
  if (t && t.classList && t.classList.contains('chat-pill-input')) {
    _updateScrollIndicator(t);
  }
}, true);

function _resizeAllPills() {
  document.querySelectorAll('.chat-pill-input').forEach(_autoResizePill);
}
window.addEventListener('load', _resizeAllPills);
window.addEventListener('resize', _resizeAllPills);

window.addEventListener('access-mode-loaded',  applyChatGate);
window.addEventListener('access-mode-changed', applyChatGate);

export function initChat() {
  app.isDebug = isDebugMode();
  _applyUserTurnAnchorConfig();
  initComposerStore();
  mountReactComposerIsland();

  // ── DOM refs ───────────────────────────────────────────────────────────────
  // ── Relative-time ticker ──
  // Rendered timestamps derive from their real created-at values. Refresh only
  // visible rows and sleep until the next label boundary: each second below a
  // minute, then each minute. A newly rendered or scrolled-in row wakes the
  // scheduler immediately instead of needing background ticking.
  let _bubbleTimeTimer = null;
  let _bubbleTimeRaf = null;
  const _refreshVisibleBubbleTimes = () => {
    if (_bubbleTimeTimer) { clearTimeout(_bubbleTimeTimer); _bubbleTimeTimer = null; }
    if (document.visibilityState !== 'visible') return;
    const nextDelay = _refreshAllBubbleTimes();
    if (nextDelay != null) {
      // Add a tiny cushion so a timer firing fractionally before the boundary
      // cannot re-render the same floor-rounded value in a tight loop.
      _bubbleTimeTimer = setTimeout(_refreshVisibleBubbleTimes, nextDelay + 10);
    }
  };
  const _queueBubbleTimeRefresh = () => {
    if (_bubbleTimeRaf != null) return;
    _bubbleTimeRaf = requestAnimationFrame(() => {
      _bubbleTimeRaf = null;
      _refreshVisibleBubbleTimes();
    });
  };
  document.addEventListener('visibilitychange', _refreshVisibleBubbleTimes);
  document.addEventListener('scroll', _queueBubbleTimeRefresh, true);
  window.addEventListener('resize', _queueBubbleTimeRefresh);
  if (app.chatMessages) {
    new MutationObserver(_queueBubbleTimeRefresh).observe(app.chatMessages, {
      childList: true,
      subtree: true,
    });
  }
  _refreshVisibleBubbleTimes();

  // ── Expose chat functions on app (core functions set at module scope) ──
  app.updateLastBubble = updateLastBubble;
  app.appendStreamToActiveBubble = appendStreamToActiveBubble;
  app.finalizeAgentResponse = finalizeAgentResponse;
  app.ensureStreamingBubbleForActiveTurn = ensureStreamingBubbleForActiveTurn;
  app.seedStreamingBubble = seedStreamingBubble;
  app.finalizeAgentStep = finalizeAgentStep;
  app.markAgentInterrupted = markAgentInterrupted;
  app.attachToolCallsToLastBubble = attachToolCallsToLastBubble;
  app.autoResizeChatInput = () => _autoResizePill(app.chatInput);
  app.focusChatInput = () => { if (app.chatInput) app.chatInput.focus(); };
  app._sendStopMessage = sendStopMessage;
  // Let shared modules fire a chat send programmatically (e.g. the footer
  // compaction panel's "Compact now" button, which sends "/compact" through the
  // pipeline WITHOUT staging it in the pill — the user's draft stays untouched).
  app.sendChatMessage = sendMessage;

  // ── "Session not found" (open session permanently deleted elsewhere) ──
  // Fired from agentWs on a `session_deleted` event — another tab, or another
  // device via the hybrid tombstone sync. When it targets the OPEN session we
  // drop the (now-erased) transcript, show a centred notice, and lock the
  // composer. clearSessionNotFound() undoes the lock when the user navigates to a
  // live session (called at the top of loadSessionChat). A recycle does NOT fire
  // this — a binned session's transcript is still viewable.
  app.onSessionDeleted = function (event) {
    const targetId = (event && (event.session_id || event.sessionId)) || '';
    // Its local mirror row is already gone — refresh the sidebar so it drops out.
    if (app.currentUserId && typeof app.populateSessionSelect === 'function') {
      try { app.populateSessionSelect(app.currentUserId); } catch (_) { /* best-effort */ }
    }
    if (!targetId || targetId !== app.currentSessionId) return;
    app._sessionNotFound = targetId;
    if (app.chatMessages) {
      app.chatMessages.innerHTML = '';
      addChatBubble('agent', chatMsg('session_deleted_notice'), 'session-deleted-notice');
    }
    if (app.chatInput) { app.chatInput.value = ''; app.chatInput.disabled = true; }
    if (app.chatSend) app.chatSend.disabled = true;
  };
  app.clearSessionNotFound = function () {
    if (!app._sessionNotFound) return;
    app._sessionNotFound = null;
    if (app.chatInput) app.chatInput.disabled = false;
    // The send button's enabled state is re-derived by loadSessionChat.
  };

  // ── Event wiring ──
  app.chatSend.addEventListener('click', sendMessage);
  app.chatInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      // On touch devices (mobile), let Enter insert a newline; send via the button.
      if (window.matchMedia?.('(pointer: coarse)').matches) return;
      e.preventDefault();
      sendMessage();
    }
  });

  // ── Idle/active footer mode (rebuilds pill rows on mode switch) ──
  let _idleDebounce = null;

  // Last pointerdown target. The idle check uses it to tell "clicked a footer
  // control / floating panel" (keep active) from "clicked into the chat
  // content area" (collapse). Focus alone can't tell: floating-panel rows and
  // the chat content are non-focusable, so activeElement either stays pinned
  // on the footer button or falls back to <body> — neither reveals where the
  // user actually clicked.
  let _lastPointerTarget = null;
  let _pointerGestureActive = false;
  let _pendingFooterMode = null;
  let _footerModeSettleTimer = null;

  // Changing footer modes reparents the textarea and every pill button. If that
  // happens between pointerdown and click, the browser cancels the click because
  // its original target moved to a different DOM parent. Defer pointer-initiated
  // mode changes until the complete pointerup -> click sequence has settled.
  function _switchFooterModeSafely(mode) {
    if (_pointerGestureActive) {
      _pendingFooterMode = mode;
      return;
    }
    _pendingFooterMode = null;
    switchFooterMode(mode);
  }

  function _settlePointerGesture() {
    _pointerGestureActive = false;
    if (!_pendingFooterMode) return;
    if (_footerModeSettleTimer) clearTimeout(_footerModeSettleTimer);
    _footerModeSettleTimer = setTimeout(() => {
      _footerModeSettleTimer = null;
      if (_pointerGestureActive || !_pendingFooterMode) return;
      const mode = _pendingFooterMode;
      _pendingFooterMode = null;
      switchFooterMode(mode);
    }, 0);
  }

  document.addEventListener('pointerdown', (e) => {
    _lastPointerTarget = e.target;
    _pointerGestureActive = true;
    if (_footerModeSettleTimer) {
      clearTimeout(_footerModeSettleTimer);
      _footerModeSettleTimer = null;
    }
  }, true);
  document.addEventListener('pointerup', _settlePointerGesture, true);
  document.addEventListener('pointercancel', _settlePointerGesture, true);
  document.addEventListener('keydown', (e) => { if (e.key === 'Tab') _lastPointerTarget = null; }, true);

  function _isFooterInteractionTarget(t) {
    if (!t || typeof t.closest !== 'function') return false;
    const footerRow = document.getElementById('chat-footer-row');
    if (footerRow && footerRow.contains(t)) return true;
    const pill = document.getElementById('chat-input-row');
    if (pill && pill.contains(t)) return true;
    const abovePill = document.getElementById('chat-above-pill');
    if (abovePill && abovePill.contains(t)) return true;
    return !!t.closest('.chat-model-list-panel, .chat-model-picker, .chat-skill-picker, .chat-claude-panel');
  }

  function _scheduleIdleCheck() {
    if (_idleDebounce) clearTimeout(_idleDebounce);
    _idleDebounce = setTimeout(() => {
      _idleDebounce = null;
      // Keep the active footer when the last interaction was with a footer
      // control or one of its floating panels; collapse to idle only when the
      // user clicked away into the chat content area and the pill is empty.
      if (_isFooterInteractionTarget(_lastPointerTarget)
          || _isFooterInteractionTarget(document.activeElement)) return;
      if (!app.chatInput.value.trim()) {
        _switchFooterModeSafely('idle');
      }
    }, 150);
  }

  // Clicking the idle pill background focuses the textarea → triggers keyboard
  // → focus handler switches to active mode. No pointer capture needed since
  // the active layout is always present in the DOM (just hidden via CSS).
  const _pillEl = document.getElementById('chat-input-row');
  if (_pillEl) {
    _pillEl.addEventListener('pointerdown', (e) => {
      if (getFooterMode() !== 'idle') return;
      const el = e.target;
      const tag = el?.tagName?.toLowerCase();
      if (tag === 'button' || tag === 'textarea' || tag === 'input') return;
      if (el?.closest?.('button')) return;
      // Presses in the right action zone (stop/continue/mic) must never be
      // swallowed by the pointer capture: a press on the zone's padding or
      // gaps would otherwise focus the textarea and synchronously rebuild
      // the pill into the active layout, hiding + reparenting the buttons
      // mid-press — the browser then retargets the click to the pill and
      // the first click on Continue silently does nothing (desktop-only).
      if (el?.closest?.('.chat-pill-layout-zone-right')) return;
      if (!app.chatInput) return;
      // Capture the pointer so that pointerup / click fire on the pill
      // element and NOT on active-layout buttons that appear under the
      // cursor after the synchronous mode switch below.
      _pillEl.setPointerCapture(e.pointerId);
      e.preventDefault();
      app.chatInput.focus();
    });
    // Release pointer capture once the finger lifts
    _pillEl.addEventListener('pointerup', (e) => {
      if (_pillEl.hasPointerCapture?.(e.pointerId)) {
        _pillEl.releasePointerCapture(e.pointerId);
      }
    });
    _pillEl.addEventListener('lostpointercapture', (e) => {
      // Belt-and-suspenders: release if the browser took it away
    });
  }

  app.chatInput.addEventListener('focus', () => {
    if (_idleDebounce) { clearTimeout(_idleDebounce); _idleDebounce = null; }
    _prewarm();
    document.body.classList.add('chat-pill-focused');
    _switchFooterModeSafely('active');
  });
  app.chatInput.addEventListener('blur', () => {
    document.body.classList.remove('chat-pill-focused');
    // Suppress idle-debounce while the pill is being rebuilt
    // (hideElement during applyChatPillLayout causes a synchronous blur).
    if (app.__rebuildingPill) return;
    _scheduleIdleCheck();
  });

  // A successful mobile send should return the composer to its compact idle
  // position and dismiss the OS keyboard. Clear the button pointer target first:
  // otherwise the normal blur idle check intentionally treats Send as a footer
  // interaction and keeps the full composer open.
  app.dismissMobileComposerAfterSend = () => {
    const panel = document.getElementById('chat-panel');
    const isMobile = panel ? panel.clientWidth <= 768 : window.innerWidth <= 768;
    if (!isMobile || !app.chatInput || app.chatInput.value.trim()) return;
    _lastPointerTarget = null;
    app.chatInput.blur();
    _switchFooterModeSafely('idle');
  };

  // When focus finally leaves the footer row — e.g. the user clicked into the
  // chat content area after using the model changer / abilities / mode button —
  // run the same idle check. Without this, the footer stays expanded forever
  // once a footer control holds focus (the textarea blur above was suppressed).
  const _footerRowEl = document.getElementById('chat-footer-row');
  if (_footerRowEl) {
    _footerRowEl.addEventListener('focusout', (e) => {
      if (e.relatedTarget && _isFooterInteractionTarget(e.relatedTarget)) return;
      if (app.__rebuildingPill) return;
      _scheduleIdleCheck();
    });
  }

  // On mobile, the Android OS nav buttons can dismiss the keyboard without
  // firing blur on the input — the input stays focused but the keyboard is
  // gone.  Detect this via visualViewport resize: when the viewport returns
  // to full height (keyboard closed) while the input still has focus, blur
  // the input so the natural focus/blur cycle stays intact for the next tap.
  if (window.visualViewport) {
    let _lastVpHeight = window.visualViewport.height;
    window.visualViewport.addEventListener('resize', () => {
      const vp = window.visualViewport;
      if (!vp) return;
      const grew = vp.height > _lastVpHeight + 80;
      if (grew && document.body.classList.contains('chat-pill-focused')) {
        // Blur the input so the header reappears AND next tap fires a fresh
        // focus event that re-hides it.  requestAnimationFrame avoids fighting
        // the resize cycle / focus re-entry.
        requestAnimationFrame(() => {
          if (document.body.classList.contains('chat-pill-focused') && app.chatInput) {
            // Keyboard-dismiss blur isn't a user click — clear the tracked
            // pointer target so the normal idle collapse still runs.
            _lastPointerTarget = null;
            app.chatInput.blur();
          }
        });
      }
      _lastVpHeight = vp.height;
      _reanchorActiveTurnAfterLayout();
    });
  }

  // Mobile keyboard: tapping a footer control (mode / model / abilities /
  // stop / continue / suggestion chips) while typing must NOT dismiss the
  // keyboard. These are typing-adjacent interactions — keep focus on the pill
  // so the user can pick a model / flip a mode and keep typing. The floating
  // picker panels are mounted on <body>, so listen on document with the full
  // footer-area selector (mirrors _isFooterInteractionTarget above).
  keepPillFocusOnFooterTap(
    document,
    app.chatInput,
    '#chat-input-area, .chat-model-list-panel, .chat-model-picker, .chat-skill-picker, .chat-claude-panel'
  );

  // Terminal-tunnel UI
  try { initChatTunnel(); } catch (_) { /* non-fatal */ }
  try { initChatComponents(); } catch (_) { /* non-fatal */ }
  // Terminal Chat engine — live xterm.js view when agent uses terminal_chat engine.
  // Its on-screen keys (footer toggle + number-pad) are plugin-owned and loaded
  // lazily by the engine only when a terminal-chat session mounts.
  try { initTerminalChatEngine(); } catch (_) { /* non-fatal */ }

  app.chatInput.addEventListener('input', () => {
    if (!_canChat()) { app.chatSend.disabled = true; _updateInputRowState(); return; }
    const hasText = !!app.chatInput.value.trim();
    app.chatSend.disabled = !hasText;
    _updateInputRowState();
    _saveDraft();
    _updateActionBtns();
    // If user starts typing while idle, switch to active footer.
    // Skip when the continue button set the value programmatically — the
    // pill rebuild would move the button under the cursor and the 300ms
    // pill-no-clicks block would eat a natural double-click.
    if (hasText && getFooterMode() === 'idle' && !app._continueClick) {
      if (_idleDebounce) { clearTimeout(_idleDebounce); _idleDebounce = null; }
      _switchFooterModeSafely('active');
    }
    // Keep the warmed prep fresh while a long message is composed (throttled).
    _prewarm();
  });

  // ── Execution mode toggle (per-session key) ──
  // The pill is ENGINE-AWARE. Native WebAgent agents cycle their configured
  // modes (Ask / Plan / Auto by default);
  // Codex engine agents cycle Ask (read-only) → Wkspc (workspace-write) → Auto
  // (full access), because `codex exec` has no GUI confirmation — for Codex,
  // 'ask' means read-only and workspace-write is its own mode. Legacy saved
  // values (the old Read/Write/Auto) are migrated on read: read→plan, write→ask.
  const MODE_LEGACY = { read: 'plan', write: 'ask' };
  const MODE_CODEX_CYCLE = ['ask', 'wkspc', 'auto'];
  const MODE_CODEX_LABELS = { ask: 'Ask', wkspc: 'Wkspc', auto: 'Auto' };
  const MODE_CODEX_TITLES = { ask: 'Read-only (no writes)', wkspc: 'Workspace-write', auto: 'Full access' };
  app.executionMode = 'ask';
  const modeBtn = document.getElementById('chat-mode-btn');

  // Engine of the currently selected agent ('' / 'claude_code' / 'codex' …),
  // read from the same shared agents list the mode seeding uses.
  function _agentEngine() {
    try {
      const id = app.currentAgentId;
      if (!id) return '';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        return (a && a.engine) || '';
      }
    } catch (_) { /* non-fatal */ }
    return '';
  }

  function _activeAgent() {
    try {
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      return Array.isArray(list) ? list.find(x => x && x.id === app.currentAgentId) : null;
    } catch (_) { return null; }
  }

  // Mode vocabulary for the current agent's engine — cycle order, labels, hints,
  // and coercion of legacy/foreign saved values ('plan' on a codex session was
  // the old read-only slot → ask; 'wkspc' on a native session → invalid).
  function _modeCtx() {
    const codex = _agentEngine() === 'codex';
    let cycle = MODE_CODEX_CYCLE;
    let labels = MODE_CODEX_LABELS;
    let titles = MODE_CODEX_TITLES;
    if (!codex) {
      const configured = (_activeAgent()?.execution_modes || []).filter(
        mode => mode && typeof mode.id === 'string' && mode.id
      );
      const modes = configured.length ? configured : [
        { id: 'ask', label: 'Ask', description: 'Proposal only', permission_policy: 'read_only' },
        { id: 'plan', label: 'Plan', description: 'Read-only planning', permission_policy: 'read_only' },
        { id: 'auto', label: 'Auto', description: 'Full access', permission_policy: 'write' },
      ];
      cycle = modes.map(mode => mode.id);
      labels = Object.fromEntries(modes.map(mode => [mode.id, mode.label || mode.id]));
      titles = Object.fromEntries(modes.map(mode => [
        mode.id,
        `${mode.description || mode.label || mode.id} · ${mode.permission_policy === 'write' ? 'Write-capable' : 'Read-only'}`,
      ]));
    }
    const coerce = (v) => {
      if (!v) return undefined;
      v = String(v).toLowerCase();
      if (MODE_LEGACY[v]) v = MODE_LEGACY[v];
      if (cycle.includes(v)) return v;
      if (codex && v === 'plan') return 'ask';
      return undefined;
    };
    return { codex, cycle, labels, titles, coerce };
  }

  // The active agent's configured "Default chat mode" (Config tab →
  // metadata.default_execution_mode, served on the agent record as
  // default_execution_mode). A FRESH session with no per-session saved mode
  // STARTS in this; once a session has its own saved mode (the user touched the
  // pill or sent a message) that wins. Falls back to 'ask' when unset.
  function _agentDefaultMode() {
    const ctx = _modeCtx();
    try {
      const id = app.currentAgentId;
      if (!id) return 'ask';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        const m = ctx.coerce(a && a.default_execution_mode);
        if (m) return m;
      }
    } catch (_) { /* non-fatal — fall through to 'ask' */ }
    return 'ask';
  }

  function _applyExecutionMode() {
    if (!modeBtn) return;
    const ctx = _modeCtx();
    const fallback = _agentDefaultMode();
    const sid = app.currentSessionId;
    if (!sid) {
      app.executionMode = fallback;
      modeBtn.textContent = ctx.labels[fallback] || 'Ask';
      modeBtn.title = ctx.titles[fallback] || '';
      return;
    }
    const saved = kvRead('chat:execMode:' + sid, 'chat_execution_mode_' + sid);
    app.executionMode = ctx.coerce(saved) || fallback;
    modeBtn.textContent = ctx.labels[app.executionMode] || 'Ask';
    modeBtn.title = ctx.titles[app.executionMode] || '';
  }
  app.refreshExecutionMode = _applyExecutionMode;

  if (modeBtn) {
    _applyExecutionMode();
    modeBtn.addEventListener('click', () => {
      const ctx = _modeCtx();
      const idx = ctx.cycle.indexOf(app.executionMode);
      const fromMode = app.executionMode;
      app.executionMode = ctx.cycle[(idx + 1) % ctx.cycle.length];
      modeBtn.textContent = ctx.labels[app.executionMode];
      modeBtn.title = ctx.titles[app.executionMode] || '';
      // Live transcript notice next to the bubbles ("Mode change"). NOT inside
      // setExecutionMode — that path also runs on session load, which must not
      // spam notices. `from` is the mode the activity group nets against:
      // clicking back to where the group started cancels the notice.
      if (typeof app.notifyExecutionMode === 'function') {
        try { app.notifyExecutionMode(app.executionMode, {from: fromMode}); } catch (_) {}
      }
      const sid = app.currentSessionId;
      if (sid) {
        kvWrite('chat:execMode:' + sid, 'chat_execution_mode_' + sid, app.executionMode);
        // Persist server-side so the loop can re-read mid-turn (mirrors _selectModel).
        fetch(apiPath('/api/v1/chat/session-mode'), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({
            user_id: app.currentUserId || '',
            session_id: sid,
            mode: app.executionMode,
          }),
        }).catch(() => { /* fire-and-forget — best-effort */ });
      }
    });
  }

  app.reloadExecutionMode = _applyExecutionMode;

  // ── Target-device pill (per-session) ──
  // Picks which device in the fleet runs the next message. '' = this device
  // (runs locally, exactly like before); a chosen instance-id is sent as
  // request.target_device so the backend hands the turn to that device's worker
  // to run inside THIS session (the reply flows back over the shared DB). The
  // choice is remembered per session, like the execution mode above.
  app.targetDevice = '';
  const targetBtn = document.getElementById('chat-target-btn');
  // Friendly-name fallback: the server (and the picker) hand us a device's label
  // when the executor is chosen/applied. Cache it so the pill can show the name
  // even for a device that's offline or not yet in the picker's loaded list
  // (DevicePicker.labelFor would otherwise fall back to the raw instance id).
  const _targetLabelCache = {};

  function _renderTargetPill() {
    if (!targetBtn) return;
    const labelEl = targetBtn.querySelector('.chat-target-label');
    const inst = app.targetDevice || '';
    if (inst) {
      targetBtn.classList.add('targeting');
      let lbl = (window.DevicePicker && window.DevicePicker.labelFor)
        ? window.DevicePicker.labelFor(inst) : inst;
      if ((!lbl || lbl === inst) && _targetLabelCache[inst]) lbl = _targetLabelCache[inst];
      if (labelEl) labelEl.textContent = lbl;
      targetBtn.title = 'Remote Control: running on ' + lbl + ' — click to change';
    } else {
      targetBtn.classList.remove('targeting');
      if (labelEl) labelEl.textContent = '';
      targetBtn.title = 'Remote Control — choose which device runs your next message';
    }
  }

  // The active agent's configured "Default device" (Config tab →
  // metadata.default_target_device, served on the agent record as
  // default_target_device). Returns the instance-id or '' (none configured).
  function _agentDefaultDevice() {
    try {
      const id = app.currentAgentId;
      if (!id) return '';
      const list = window.__agentsSharedData && window.__agentsSharedData.agents;
      if (Array.isArray(list)) {
        const a = list.find(x => x && x.id === id);
        const d = a && a.default_target_device;
        if (d && typeof d === 'string') return d;
      }
    } catch (_) { /* non-fatal — no configured default */ }
    return '';
  }

  // Apply the agent's configured default device for a session that has NO explicit
  // per-session executor. Only takes effect if that device is currently ONLINE;
  // when it's offline (or unknown) the chat stays on "this device". Async — needs
  // the live fleet list to check reachability. Deliberately does NOT persist (no
  // localStorage / session-metadata write): it's a soft default re-evaluated on
  // every open, so a device coming back online is auto-picked next time and the
  // user's own pick (which does persist) always wins. Guards on the session id +
  // an unset target both before and after the await so it can't clobber a choice
  // made while the fleet list was loading.
  async function _applyAgentDefaultTarget(sid) {
    const want = _agentDefaultDevice();
    if (!want) return;
    if (app.currentSessionId !== sid || app.targetDevice) return;
    try {
      const devices = (window.DevicePicker && window.DevicePicker.load)
        ? await window.DevicePicker.load() : [];
      if (app.currentSessionId !== sid || app.targetDevice) return;
      const dev = Array.isArray(devices) ? devices.find(d => d && d.instance_id === want) : null;
      if (dev && dev.online && !dev.is_self) {
        app.targetDevice = dev.instance_id;
        _targetLabelCache[dev.instance_id] = dev.label || dev.instance_id;
        _renderTargetPill();
      }
      // Offline / unknown → leave it on "this device" (the fall-back the user chose).
    } catch (_) { /* best-effort — stay local */ }
  }
  // Exposed so session-load.js can apply the agent default when a fetched session
  // has no stored Remote Control executor.
  app.applyAgentDefaultTarget = _applyAgentDefaultTarget;

  function _applyTargetDevice() {
    const sid = app.currentSessionId;
    app.targetDevice = '';
    if (sid) {
      const saved = kvRead('chat:targetDevice:' + sid, 'chat_target_device_' + sid);
      if (saved) app.targetDevice = saved;
    }
    _renderTargetPill();
    // No explicit per-session choice → fall back to the agent's configured default
    // device (applied only when it's online; otherwise the pill stays on "this
    // device"). Non-persisting, so it re-evaluates each open.
    if (!app.targetDevice) _applyAgentDefaultTarget(sid);
  }

  if (targetBtn) {
    _applyTargetDevice();
    // Warm the device list so the pill can show the chosen device's real name.
    if (window.DevicePicker && window.DevicePicker.load) {
      window.DevicePicker.load().then(_renderTargetPill).catch(() => {});
    }
    targetBtn.addEventListener('click', (e) => {
      e.preventDefault();
      if (!window.DevicePicker || !window.DevicePicker.open) return;
      window.DevicePicker.open(targetBtn, {
        title: 'Remote Control — run this chat on',
        currentInstance: app.targetDevice || '',
        onSelect: (sel) => {
          const inst = sel.is_self ? '' : (sel.instance_id || '');
          const label = sel.is_self ? '' : (sel.label || '');
          app.setTargetDevice(inst, label);
          // Persist on the SESSION (not just this browser) so any device that
          // opens this session routes to the same executor. Best-effort — the
          // local pill already reflects the choice.
          _persistTargetDeviceToSession(app.currentSessionId, inst, label);
        },
      });
    });
  }

  // Export reload so session switches re-read the per-session target (mirrors
  // app.reloadExecutionMode).
  app.reloadTargetDevice = _applyTargetDevice;

  // Set the Remote Control executor for the CURRENT session locally: update the
  // pill + the fast localStorage seed. Mirrors app.setExecutionMode. The server
  // (session metadata) is the cross-device source of truth — session-load.js
  // applies data.remote_executor through here on open, and the picker's onSelect
  // both calls this AND persists to the session. '' = run locally.
  app.setTargetDevice = function (instance, label) {
    const inst = instance || '';
    app.targetDevice = inst;
    if (inst && label) _targetLabelCache[inst] = label;
    const sid = app.currentSessionId;
    if (sid) {
      if (inst) kvWrite('chat:targetDevice:' + sid, 'chat_target_device_' + sid, inst);
      else kvDelete('chat:targetDevice:' + sid, 'chat_target_device_' + sid);
    }
    _renderTargetPill();
  };

  // Persist the chosen executor onto the session row (metadata) so it follows the
  // session across devices, not just this browser. Fire-and-forget.
  function _persistTargetDeviceToSession(sid, inst, label) {
    if (!sid) return;
    fetch(apiPath('/api/v1/db/sessions/' + encodeURIComponent(sid) + '?db=user.db'), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        remote_executor_instance: inst || '',
        remote_executor_label: label || '',
      }),
    }).catch(() => { /* pill already reflects the choice; sync is best-effort */ });
  }

  // ── Agent-driven mode switch (set_execution_mode tool → WS echo) ──
  // The backend broadcasts an `execution_mode` event when the agent flips the
  // mode mid-conversation (e.g. Plan→Auto after the user approves a plan).
  // agentWs.js routes it here so the pill below the chat visibly switches and
  // the next message the user sends carries the new mode (persisted per-session).
  app.setExecutionMode = function (mode) {
    const ctx = _modeCtx();
    const m = ctx.coerce(mode);
    if (m === undefined) return;
    app.executionMode = m;
    if (modeBtn) { modeBtn.textContent = ctx.labels[m]; modeBtn.title = ctx.titles[m] || ''; }
    const sid = app.currentSessionId;
    if (sid) {
      kvWrite('chat:execMode:' + sid, 'chat_execution_mode_' + sid, m);
    }
  };

  // ── Continue button ──
  const continueBtn = document.getElementById('chat-continue-btn');
  if (continueBtn) {
    continueBtn.addEventListener('click', () => {
      if (!_canChat()) { applyChatGate(); return; }
      if (!app.currentAgentId) return;
      app._continueClick = true;
      const saved = app.chatInput ? app.chatInput.value : '';
      // A visible Continue is exactly the same fresh user turn as typing it.
      // It must not replay an exhausted run via the special recovery endpoint.
      app.chatInput.value = 'Continue';
      app._continueClick = false;
      sendMessage();
      // Restore whatever the user was typing so it isn't lost
      setTimeout(() => { app.chatInput.value = saved; _updateInputRowState(); _autoResizePill(app.chatInput); }, 100);
    });
  }

  // ── Stop button ──
  const stopBtn = document.getElementById('chat-stop-btn');
  if (stopBtn) stopBtn.addEventListener('click', () => {
    if (!app.isProcessing && !app._runControlsActive) return;
    abortChatStream();
    sendStopMessage();
  });

  // ── Global Escape: stop conversation ──
  document.addEventListener('keydown', function _escHandler(e) {
    if (e.key !== 'Escape') return;

    // Let dropdown/overlay Esc handlers take priority (agent menu, session menu, picker)
    const agentMenu = document.getElementById('chat-agent-menu');
    const sessionMenu = document.getElementById('chat-session-menu');
    if ((agentMenu && !agentMenu.hidden) || (sessionMenu && !sessionMenu.hidden)) return;

    if (!app.isProcessing && !app._runControlsActive) return;

    e.preventDefault();
    abortChatStream();
    sendStopMessage();
  });

  // ── In-pill stop / continue visibility ──
  function _updateActionBtns() {
    if (!stopBtn || !continueBtn) return;
    const hasAgent = !!app.currentAgentId;
    const hasInput = app.chatInput && app.chatInput.value.trim();
    const runActive = !!(app._runControlsActive || app.isProcessing);
    if (hasAgent) {
      stopBtn.hidden = !runActive;
      continueBtn.hidden = runActive || !!hasInput;
    } else {
      stopBtn.hidden = true;
      continueBtn.hidden = true;
    }
  }
  app.beginRunControls = (ownerTurnId) => {
    // A stale WS/progress/user event for a turn that has already terminally
    // ended (errored/interrupted) must never re-engage the Stop control. Each
    // run's owner id is unique, so once a run is marked terminal it stays
    // suppressed for the life of the session.
    const owner = ownerTurnId ? String(ownerTurnId) : '';
    if (owner && app._runEndedFor && app._runEndedFor[owner]) return;
    app._runControlsActive = true;
    if (ownerTurnId) app._runControlsOwner = String(ownerTurnId);
    _updateActionBtns();
  };
  app.endRunControls = (ownerTurnId) => {
    const owner = ownerTurnId ? String(ownerTurnId) : '';
    // A predecessor can report interruption after its replacement message has
    // already begun. Never let that stale terminal event hide the new Stop.
    if (owner && app._runControlsOwner && owner !== app._runControlsOwner && app.isProcessing) return;
    app._runControlsActive = false;
    app._runControlsOwner = '';
    _updateActionBtns();
  };
  app.refreshRunControls = _updateActionBtns;
  _updateActionBtns();

  // ── Gate + draft restore ──
  applyChatGate();
  fetchAccessMode().then(() => {
    applyChatGate();
    _restoreDraft();
    if (_outboxHasPending()) {
      _renderPendingBubbles();
      _startOutboxPoll();
    }
  });
  _restoreDraft();

  // ── Input area padding observer ──
  const inputArea = document.getElementById('chat-input-area');
  const messagesInner = document.getElementById('chat-messages-inner');
  const messagesEl = document.getElementById('chat-messages');
  var _pullingBottom = false;  // true while a bottom-pull gesture is active
  var _prefetchWarming = false;  // true once we've pre-fetched for this gesture
  var _bottomArmed = false;  // 2-stage: true only after stage 1 reaches the true transcript tail; the next pull refreshes (stage 2)
  var _bottomSettling = false;  // true while stage 1 is loading/repositioning to the true transcript tail
  var _armedSession = null;  // session id the stage-2 arm belongs to — arm dies on session switch
  var _tailPadPx = 100;  // live tail-clearance padding of #chat-messages-inner (updated by syncPad)
  var _turnAnchorPadPx = 0;  // uncapped extra clearance while the newest user turn owns the viewport
  function pulLog(msg) {
    // Diagnostic only — no-op in production. Enable live from the console:
    //   window.__CHAT_PULL_DEBUG__ = true
    if (!(app && app.isDebug) && !window.__CHAT_PULL_DEBUG__) return;
    console.log('DEBUG-TAG:[chat-pull] ' + msg);
  }
  // Reset the stage-2 arm whenever the context the stage-1 settle happened in
  // is invalidated: user/content scrolled away from the bottom, new messages
  // arrived, the session changed, or the chat view was hidden. Stage 2 must
  // only ever fire directly after a stage-1 settle in the SAME session.
  function _resetBottomArm(reason) {
    if (_refreshPillHeld || _bottomArmed || _armedSession) {
      _bottomArmed = false;
      _armedSession = null;
      if (_refreshPillHeld) _hideRefreshPill();
      pulLog('bottom arm RESET — ' + reason);
    }
  }
  app._resetBottomArm = _resetBottomArm;  // module-scope hooks (content add, scroll) call this

  // ── Stage-2 "Click here to refresh" pill (the bottom pull spinner) ──
  // The 2nd bottom pull NO LONGER auto-refreshes. Instead the pill it raises is
  // HELD UP, relabeled "Click here to refresh", and made clickable. Clicking the
  // pill re-syncs the transcript; scrolling away or a session change dismisses
  // it without refreshing.
  var _refreshPillHeld = false;  // true once the held "Click here to refresh" pill is up
  var _refreshPillWired = false;  // one-time click wiring guard
  var _refreshPillHideTimer = null;
  function _getRefreshPill() { return document.getElementById('chat-spinner-bottom'); }
  function _clearRefreshPillHideTimer() {
    if (_refreshPillHideTimer !== null) {
      clearTimeout(_refreshPillHideTimer);
      _refreshPillHideTimer = null;
    }
  }
  function _hideRefreshPill() {
    _clearRefreshPillHideTimer();
    _refreshPillHeld = false;
    var s = _getRefreshPill();
    if (s) { s.classList.remove('held'); s.classList.remove('visible'); }
  }
  function _holdRefreshPill() {
    var s = _getRefreshPill();
    if (!s) return;
    _clearRefreshPillHideTimer();
    _refreshPillHeld = true;
    s.classList.add('visible', 'held');
    s.classList.remove('loading');
    var label = s.querySelector('.chat-spinner-label');
    if (label) label.textContent = 'Click here to refresh';
    _refreshPillHideTimer = setTimeout(function() {
      _refreshPillHideTimer = null;
      if (_refreshPillHeld) {
        _hideRefreshPill();
        pulLog('stage 2 refresh pill auto-hidden after 2 seconds');
      }
    }, 2000);
    pulLog('stage 2 → "Click here to refresh" pill HELD UP (auto-hides after 2 seconds)');
  }
  function _wireRefreshPill() {
    if (_refreshPillWired) return;
    _refreshPillWired = true;
    var s = _getRefreshPill();
    if (s) {
      s.addEventListener('click', function(e) {
        e.stopPropagation();
        if (!_refreshPillHeld) return;
        pulLog('"Click here to refresh" pill clicked → refreshTranscript(true)');
        _hideRefreshPill();
        if (typeof app.refreshTranscript === 'function') {
          app.refreshTranscript(true).catch(function() {});
        } else {
          document.dispatchEvent(new CustomEvent('chat-control:refresh-transcript'));
        }
      });
    }
    // Dismiss the held pill if the user scrolls away from the bottom.
    var scroller = document.getElementById('chat-messages');
    if (scroller) {
      scroller.addEventListener('scroll', function() {
        var maxS = scroller.scrollHeight - scroller.clientHeight;
        if (scroller.scrollTop < maxS - 1) _hideRefreshPill();
      }, { passive: true });
    }
  }

  // Stage 1 must do more than bounce: it lands at the true transcript tail.
  // _repositionToLatest fetches the newest window when the currently rendered
  // slice has newer rows, or simply scrolls to its bottom when it already is
  // the tail. Do not arm stage 2 until that operation succeeds in this session.
  function _settleBottomStageOne() {
    const sessionId = app.currentSessionId;
    if (!sessionId || _bottomSettling) return;
    _bottomSettling = true;
    _bottomArmed = false;
    _armedSession = null;
    pulLog('stage 1 settle → repositioning to true transcript bottom');

    const reposition = typeof app._repositionToLatest === 'function'
      ? app._repositionToLatest(sessionId)
      : Promise.resolve(false);
    Promise.resolve(reposition).then(function(ok) {
      if (ok && app.currentSessionId === sessionId) {
        _bottomArmed = true;
        _armedSession = sessionId;
        pulLog('stage 1 reached true transcript bottom → ARMED for stage 2 refresh');
      } else {
        pulLog('stage 1 did not reach transcript bottom → refresh remains unarmed');
      }
    }).catch(function() {
      pulLog('stage 1 reposition failed → refresh remains unarmed');
    }).finally(function() {
      _bottomSettling = false;
    });
  }

  // 2-stage: hiding the chat view (body.chat-hidden — navigating to other
  // views) invalidates the stage-2 arm.
  if (document.body && typeof MutationObserver !== 'undefined') {
    const _bodyObs = new MutationObserver(function() {
      if (document.body.classList.contains('chat-hidden')) _resetBottomArm('view hidden');
    });
    _bodyObs.observe(document.body, { attributes: true, attributeFilter: ['class'] });
  }
  // Follow the same common-plus-active-surface merge as the rest of chat_ui.json.
  // One configured distance intentionally controls maximum stretch and both
  // vertical action thresholds, so the pull never arms before its visual cap.
  const _chatUi = window.__chatUiSnapshot || {};
  const _overscrollOverrides = window.__CHAT_PORTAL__
    ? _chatUi.chat_widget
    : (_isNarrowPanel() ? _chatUi.chat_mobile : _chatUi.chat_desktop);
  const _overscrollConfig = {
    ...(_chatUi.chat_common?.transcript_overscroll || {}),
    ...(_overscrollOverrides?.transcript_overscroll || {}),
  };
  const _configuredMaxStretch = Number(_overscrollConfig.max_stretch_px);
  const _maxTranscriptStretch = Number.isFinite(_configuredMaxStretch) && _configuredMaxStretch > 0
    ? _configuredMaxStretch
    : 50;
  const _tailClearanceConfig = {
    ...(_chatUi.chat_common?.transcript_tail_clearance || {}),
    ...(_overscrollOverrides?.transcript_tail_clearance || {}),
  };
  const _configuredTailGap = Number(_tailClearanceConfig.gap_px);
  const _tailGapPx = Number.isFinite(_configuredTailGap) && _configuredTailGap >= 0
    ? _configuredTailGap
    : 24;

  applyRubberBand(messagesEl, {
    axis: 'y',
    pullThreshold: _maxTranscriptStretch,
    pullThresholdBottom: _maxTranscriptStretch,
    // Chat has a deliberately short, responsive pull. This avoids the sticky
    // near-cap feel and the prolonged overshoot of the shared default spring.
    maxPull: _maxTranscriptStretch,
    resistance: 0.5,
    snapDuration: 220,
    snapEasing: 'cubic-bezier(0.2, 0.85, 0.3, 1)',
    // Refresh arms ONLY at the very bottom (within ~4px of the true maxS).
    // If you're still short of the bottom, the swipe is left to the native
    // scroll — it scrolls you the rest of the way instead of refreshing —
    // and the pull engages on a later move once the scroller reaches the
    // bottom. No jump, no hijack of normal scrolling, no refresh unless
    // you're actually at the bottom.
    bottomEdgeTol: 4,
    onPullTop: function(ratio) {
      var spinner = document.getElementById('chat-spinner-top');
      pulLog('onPullTop ratio=' + ratio + ' spinner=' + (spinner ? 'OK' : 'MISSING') + ' isDebug=' + app.isDebug);
      if (ratio > 0) {
        app._rbPullActive = true;
        if (app.isDebug && spinner) {
          spinner.classList.add('visible');
          spinner.classList.remove('loading');
          var label = spinner.querySelector('.chat-spinner-label');
          if (label) label.textContent = ratio >= 1 ? 'Release to load' : 'Pull to load';
          pulLog('top pill VISIBLE — label="' + (ratio >= 1 ? 'Release to load' : 'Pull to load') + '"');
        }
      } else {
        // When snapping back without having triggered, clear the flag and hide.
        // If loading has already started (onTriggerTop fired), don't touch the spinner.
        var loading = spinner && spinner.classList.contains('loading');
        if (!loading) {
          app._rbPullActive = false;
          if (app.isDebug && spinner) spinner.classList.remove('visible');
          pulLog('top pill HIDDEN (ratio=0, loading=' + loading + ')');
        }
      }
    },
    onTriggerTop: function() {
      var spinner = document.getElementById('chat-spinner-top');
      pulLog('onTriggerTop fired → loading older messages');
      if (app.isDebug && spinner) {
        spinner.classList.add('visible', 'loading');
        var label = spinner.querySelector('.chat-spinner-label');
        if (label) label.textContent = 'Loading...';
      }
      if (typeof app._checkHardScrollBoundary === 'function') {
        app._rbPullActive = false;  // allow the boundary check to pass through
        app._checkHardScrollBoundary(app.currentSessionId).then(function() {
          if (app.isDebug && spinner) {
            spinner.classList.remove('visible', 'loading');
          }
        });
      } else if (typeof app._maybeLoadMoreOnScrollTop === 'function') {
        app._rbPullActive = false;
        app._maybeLoadMoreOnScrollTop(app.currentSessionId);
        setTimeout(function() {
          if (app.isDebug && spinner) spinner.classList.remove('visible', 'loading');
        }, 1000);
      }
    },
    onPullBottom: function(ratio, absPull) {
      var spinner = document.getElementById('chat-spinner-bottom');
      pulLog('onPullBottom ratio=' + ratio + ' absPull=' + absPull + ' spinner=' + (spinner ? 'OK' : 'MISSING') + ' _pullingBottom=' + _pullingBottom + ' _prefetchWarming=' + _prefetchWarming + ' _bottomArmed=' + _bottomArmed);
      // 2-stage: the arm belongs to the session it was settled in. Any
      // session switch (dropdown, swipe, new-session, delete → navigate)
      // invalidates it — the first over-scroll in the new session is stage 1.
      if (_armedSession !== app.currentSessionId) _resetBottomArm('session changed');
      if (ratio > 0 && !_pullingBottom) {
        _pullingBottom = true;
        _prefetchWarming = false;
        // Keep the hard-boundary loader out of this gesture. Stage 1 owns
        // tail navigation and stage 2 owns the explicit refresh action.
        app._rbPullActive = true;
      } else if (ratio === 0) {
        app._rbPullActive = false;
        if (_pullingBottom && !_bottomArmed) {
          // Stage 1 completes when its spring settles. It must land at the
          // true newest transcript window before it can arm stage 2; merely
          // bouncing at the bottom of a mid-history slice is not sufficient.
          _settleBottomStageOne();
        }
        _pullingBottom = false;
      }
      // Stage 1 (unarmed): rubber band + spring, then land at the true tail —
      // NO pill. Stage 2 (armed): pill shows and prefetch warms.
      var show = ratio > 0 && _bottomArmed;
      if (show && spinner) {
        spinner.classList.add('visible');
        spinner.classList.remove('loading');
        var label = spinner.querySelector('.chat-spinner-label');
        if (label) label.textContent = ratio >= 1 ? 'Click here to refresh' : 'Pull to refresh';
        pulLog('bottom pill VISIBLE — label="' + (ratio >= 1 ? 'Click here to refresh' : 'Pull to refresh') + '" classes="' + spinner.className + '"');
      } else if (spinner) {
        var loading = spinner.classList.contains('loading');
        if (!loading && !_refreshPillHeld) spinner.classList.remove('visible');
        pulLog('bottom pill HIDDEN (ratio=' + ratio + ', loading=' + loading + ')');
      }
      // At threshold, pre-fetch the refresh data so the HTTP response is
      // cached in the browser by the time the user releases — no network
      // wait, only the DOM update happens on release. Stage 2 only: never
      // warm on the stage-1 settle pull.
      if (ratio >= 1 && _bottomArmed && !_prefetchWarming) {
        _prefetchWarming = true;
        pulLog('threshold reached → prefetch warming (session ' + app.currentSessionId + ')');
        var sid = app.currentSessionId;
        if (sid) {
          var token = localStorage.getItem('auth_token') || '';
          var url = apiPath('/api/v1/db/session-messages?db=user.db&session_id=' + encodeURIComponent(sid) + '&limit=60&light=1');
          if (token) url += '&token=' + encodeURIComponent(token);
          fetch(url).catch(function() {});
        }
      }
    },
    onTriggerBottom: function(absPull) {
      var spinner = document.getElementById('chat-spinner-bottom');
      pulLog('onTriggerBottom absPull=' + absPull + ' _bottomArmed=' + _bottomArmed + ' → REFRESH TRIGGERED');
      if (!_bottomArmed) {
        // Stage 1: the FIRST over-scroll must never refresh, even past the
        // threshold. Its trailing ratio-0 callback performs the true-tail
        // landing and arms stage 2 only after that succeeds.
        pulLog('stage 1 hard pull swallowed (no refresh) → awaiting true-tail landing');
        if (spinner) spinner.classList.remove('visible', 'loading');
        return;
      }
      // Stage 2: armed — consume the arm and reset _pullingBottom BEFORE the
      // trailing ratio-0 callback so it doesn't re-arm after the refresh.
      _bottomArmed = false;
      // Keep _armedSession set to the CURRENT session id (not null) so the
      // trailing ratio-0 onPullBottom's session-changed guard (_armedSession !==
      // app.currentSessionId) does not falsely fire on the same session and
      // instantly hide the pill we just held up. A REAL session switch still
      // makes that guard true (old id !== new id) and dismisses the pill.
      _armedSession = app.currentSessionId;
      _pullingBottom = false;
      // No auto-refresh: the second pull KEEPS the pill up, relabeled
      // "Click here to refresh", and makes it clickable. Clicking it re-syncs
      // the transcript; scrolling away or a session change dismisses it.
      pulLog('stage 2 → holding "Click here to refresh" pill (no auto-refresh)');
      _wireRefreshPill();
      _holdRefreshPill();
    }
  });
  // Defensive spinner dismissal — belt-and-braces on top of the rubber-band's
  // own release notification (snap() always reports ratio 0 now). If a pull
  // spinner is ever left visible without an active pull, any real scroll away
  // from the edge it was pulled at removes it. Loading spinners are left
  // alone — their completion handler dismisses them. During an active pull
  // the scroller stays pinned at the edge (the rubber band translates the
  // element, it doesn't scroll), so this can't fight an in-progress gesture.
  if (messagesEl) {
    messagesEl.addEventListener('scroll', function() {
      var maxS = messagesEl.scrollHeight - messagesEl.clientHeight;
      if (messagesEl.scrollTop > 1) {
        var topSpin = document.getElementById('chat-spinner-top');
        if (topSpin && !topSpin.classList.contains('loading')) topSpin.classList.remove('visible');
      }
      if (messagesEl.scrollTop < maxS - 1) {
        // 2-stage: any scroll away from the bottom invalidates the stage-2
        // arm — the next approach to the bottom is a fresh stage 1.
        if (typeof app._resetBottomArm === 'function') app._resetBottomArm('scrolled away from bottom');
        var botSpin = document.getElementById('chat-spinner-bottom');
        if (botSpin && !botSpin.classList.contains('loading')) botSpin.classList.remove('visible');
      }
    }, { passive: true });
  }

  // Pill textarea rubberband — springy overscroll on TOUCH. Wheel is left
  // alone (wheel: false) so a wheel tick can chain to footer-gesture-forward.js
  // instead of being swallowed here. The textarea has no scrollable range until
  // a draft outgrows its max height, and applyRubberBand only arms a boundary
  // pull when there is real range to pull past — so an ordinary drag over a
  // short draft is not claimed here and reaches the transcript via the footer
  // forwarder. (Before that guard the pill sat at both boundaries at once and
  // preventDefault()'d every touch drag, which read as "scrolling snaps me back
  // down" over the footer.)
  if (app.chatInput) {
    applyRubberBand(app.chatInput, { axis: 'y', pullThreshold: 20, maxPull: 50, wheel: false });
  }

  if (inputArea && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncPad = () => {
      const h = inputArea.offsetHeight;
      if (messagesEl) messagesEl.style.setProperty('--chat-input-h', h + 'px');
      // Also set on #chat-panel so the bottom pull-to-refresh spinner can
      // position itself right above the input area.
      const panelEl = document.getElementById('chat-panel');
      if (panelEl) panelEl.style.setProperty('--chat-input-h', h + 'px');
      _tailPadPx = Math.max(100, h + _tailGapPx);
      messagesInner.style.paddingBottom = Math.max(_tailPadPx, _turnAnchorPadPx) + 'px';
      _reanchorActiveTurnAfterLayout();
    };
    new ResizeObserver(syncPad).observe(inputArea);
    syncPad();
  }

  // ── Header padding observer — messages scroll behind the floating header ──
  const headerEl = document.getElementById('chat-header');
  const subHeaderEl = document.getElementById('chat-sub-header');
  const changesPanel = document.getElementById('chat-changes-panel');
  if ((headerEl || subHeaderEl) && messagesInner && typeof ResizeObserver !== 'undefined') {
    const syncHeaderPad = () => {
      // Check live DOM to detect if sub-header was reparented into the header
      // (via sub_agent_tabs control) after this observer was created.
      const subInHeader = subHeaderEl && headerEl && headerEl.contains(subHeaderEl);
      const hh = headerEl ? headerEl.offsetHeight : 0;
      const ch = changesPanel && !changesPanel.hidden ? changesPanel.offsetHeight : 0;
      const sh = (!subInHeader && subHeaderEl && subHeaderEl.style.display !== 'none') ? subHeaderEl.offsetHeight : 0;
      const totalH = hh + ch + sh;
      // A landed user turn scrolls behind this floating stack. Its readable top
      // must therefore clear the stack itself, not merely use the profile's
      // minimum gap; publish the same value to every anchoring path.
      _USER_STEP_PAD = Math.max(_configuredUserStepPad, totalH + 8);
      app._USER_STEP_PAD = _USER_STEP_PAD;
      messagesInner.style.paddingTop = _USER_STEP_PAD + 'px';
      // Position changes panel below the header
      if (changesPanel) {
        changesPanel.style.top = hh + 'px';
      }
      // Position sub-header below header + changes panel (only if NOT inside header).
      // When inside the header (reparented), clear stale top — the CSS zone rules
      // handle positioning; a leftover inline top would push the row open.
      if (subHeaderEl) {
        if (!subInHeader) {
          subHeaderEl.style.top = (hh + ch) + 'px';
        } else {
          subHeaderEl.style.removeProperty('top');
        }
      }
    };
    const obs = new ResizeObserver(syncHeaderPad);
    if (headerEl) obs.observe(headerEl);
    if (subHeaderEl) obs.observe(subHeaderEl);
    if (changesPanel) obs.observe(changesPanel);
    syncHeaderPad();
  }

  // Helper: use #chat-panel width for mobile/desktop breakpoint so that a narrow
  // side-panel on a wide viewport still gets the compact mobile layout.
  function _isNarrowPanel() {
    const panel = document.getElementById('chat-panel');
    return panel ? panel.clientWidth <= 768 : window.innerWidth <= 768;
  }

  // ── Chat fade mask — reads from chat_ui.json ──
  (async function applyChatFade() {
    try {
      const resp = await window.__fetchUiConfig({ headers: { ...authHeaders() } });
      if (!resp.ok) return;
      const data = await resp.json().catch(() => null);
      if (!data || !data.chat_ui?.chat_common) return;
      const isMobile = _isNarrowPanel();
      const overrides = window.__CHAT_PORTAL__
        ? data.chat_ui.chat_widget
        : (isMobile ? data.chat_ui.chat_mobile : data.chat_ui.chat_desktop);

      // ── Message scroller fade ──
      const baseFade = data.chat_ui.chat_common.fade || {};
      const ovrFade = overrides?.fade || {};
      const fade = { ...baseFade, ...ovrFade };
      const topPx = fade.top != null ? String(fade.top) + 'px' : '0px';
      const botPx = fade.bottom != null ? String(fade.bottom) + 'px' : '0px';
      // Avoid coincident transparent/opaque mask stops when top fade is off:
      // browsers anti-alias that zero-length transition into a horizontal seam.
      const topStops = Number(fade.top) > 0 ? `transparent 0, #000 ${topPx}` : '#000 0';
      if (messagesEl) {
        messagesEl.style.setProperty('--chat-fade-top', topPx);
        messagesEl.style.setProperty('--chat-fade-top-stops', topStops);
        messagesEl.style.setProperty('--chat-fade-bottom', botPx);
      }

      // ── Pill textarea fade — set vars on the stationary row so the
      //     mask stays put while the text rubberbands behind it.
      const basePillFade = data.chat_ui.chat_common.pill_fade || {};
      const ovrPillFade = overrides?.pill_fade || {};
      const pillFade = { ...basePillFade, ...ovrPillFade };
      if (app.chatInput) {
        const pillRow = app.chatInput.closest('[data-pill-row="0"]');
        const target = pillRow || app.chatInput;
        if (pillFade.top != null) target.style.setProperty('--chat-pill-fade-top', String(pillFade.top) + 'px');
        if (pillFade.bottom != null) target.style.setProperty('--chat-pill-fade-bottom', String(pillFade.bottom) + 'px');
        if (pillFade.bottom_offset != null) target.style.setProperty('--chat-pill-fade-bottom-offset', String(pillFade.bottom_offset) + 'px');
      }
    } catch (_) { /* best-effort */ }
  })();

  // ── Footer drag handle (abilities / device / mode chips) ──
  const _footerHandle = document.getElementById('chat-footer-handle');
  const _footerRow = document.getElementById('chat-footer-row');

  // Hide the handle entirely when below_pill has no visible content.
  // Check after the row has been populated by chat-controls-config.
  function _updateFooterHandleVisibility() {
    if (!_footerHandle || !_footerRow) return;
    const controls = _footerRow.querySelectorAll('[data-header-control], [data-element-origin]');
    const hasContent = Array.from(controls).some(
      el => el.style.display !== 'none' && !el.hidden
    );
    _footerHandle.style.display = hasContent ? '' : 'none';
  }
  _updateFooterHandleVisibility();
  // Re-check when chat controls are re-applied (agent switch, theme change)
  if (app._reapplyChatControls) {
    const _origReapply = app._reapplyChatControls;
    const _patchedReapply = function() {
      const result = _origReapply.apply(this, arguments);
      setTimeout(_updateFooterHandleVisibility, 50);
      return result;
    };
    app._reapplyChatControls = _patchedReapply;
  }

  function _saveFooterExpanded(expanded) {
    const sid = app.currentSessionId;
    if (sid) {
      kvWrite('chat:footerExpanded:' + sid, 'chat_footer_expanded_' + sid, expanded ? '1' : '0');
    }
  }

  function _loadFooterExpanded() {
    const sid = app.currentSessionId;
    if (sid) {
      const saved = kvRead('chat:footerExpanded:' + sid, 'chat_footer_expanded_' + sid);
      if (saved !== undefined && saved !== null) return saved === '1';
    }
    return true; // default to expanded
  }

  function _applyFooterExpanded(expanded) {
    if (_footerHandle) {
      _footerHandle.classList.toggle('expanded', expanded);
      _footerHandle.setAttribute('aria-expanded', String(expanded));
    }
    if (_footerRow) _footerRow.classList.toggle('expanded', expanded);
    _saveFooterExpanded(expanded);
  }

  function _toggleFooter() {
    const current = _footerRow ? _footerRow.classList.contains('expanded') : false;
    _applyFooterExpanded(!current);
  }

  if (_footerHandle && _footerRow) {
    // Apply saved state on init
    _applyFooterExpanded(_loadFooterExpanded());

    // ── Drag to reveal / hide with spring-back ──
    let _dragStartY = 0;
    let _dragOpen = false;
    let _dragOffset = 0;       // cumulative vertical drag offset (negative = up)
    const _dragThreshold = 48;  // px of vertical drag to trigger expand/collapse
    const _dragMax = 80;        // px of linear travel before rubber band kicks in
    const _rubberResistance = 40; // higher = stiffer band past the limit
    const _pillPortion = 0.2;   // fraction of rubber-banded offset applied to the pill edge
    const _pillEl = _footerHandle.parentElement;

    // Asymptotic rubber-band: beyond `limit`, the output grows slower and slower
    // rather than stopping abruptly. Gives a springy feel.
    function _rubberBand(value, limit, resistance) {
      const sign = value < 0 ? -1 : 1;
      const abs = Math.abs(value);
      if (abs <= limit) return value;
      return sign * (limit + (abs - limit) * resistance / ((abs - limit) + resistance));
    }

    _footerHandle.addEventListener('pointerdown', (e) => {
      _dragStartY = e.clientY;
      _dragOpen = _footerRow.classList.contains('expanded');
      _dragOffset = 0;
      _footerHandle.classList.remove('spring-back');
      _pillEl.classList.remove('spring-back');
      _footerHandle.setPointerCapture(e.pointerId);
      e.preventDefault();
    });

    _footerHandle.addEventListener('pointermove', (e) => {
      if (!_footerHandle.hasPointerCapture(e.pointerId)) return;
      const dy = _dragStartY - e.clientY; // positive = dragging up
      const banded = _rubberBand(dy, _dragMax, _rubberResistance);
      _dragOffset = banded;

      // Split the visual offset: the pill edge gets a portion, the handle gets
      // the rest. Since the handle sits inside the pill, splitting keeps the
      // handle at the correct total displacement when both move together.
      const pillOffset = banded * _pillPortion;
      const handleOffset = banded * (1 - _pillPortion);

      // The pill edge shows the stretch (rubber-band visible on the pill border)
      _pillEl.style.setProperty('--pill-drag-y', (-pillOffset) + 'px');
      // The handle tracks the finger more aggressively
      _footerHandle.style.setProperty('--handle-drag-y', (-handleOffset) + 'px');

      // Toggle when threshold crossed
      if (!_dragOpen && dy > _dragThreshold) {
        _applyFooterExpanded(true);
        _dragOpen = true;
        _dragStartY = e.clientY; // reset to avoid re-trigger
      } else if (_dragOpen && dy < -_dragThreshold) {
        _applyFooterExpanded(false);
        _dragOpen = false;
        _dragStartY = e.clientY;
      }
    });

    _footerHandle.addEventListener('pointerup', (e) => {
      if (!_footerHandle.hasPointerCapture(e.pointerId)) return;
      _footerHandle.releasePointerCapture(e.pointerId);
      // Spring back to origin — on both pill edge and handle
      _footerHandle.classList.add('spring-back');
      _footerHandle.style.removeProperty('--handle-drag-y');
      _pillEl.classList.add('spring-back');
      _pillEl.style.removeProperty('--pill-drag-y');
      _dragOffset = 0;
    });

    // Click to toggle (when there was no significant drag)
    _footerHandle.addEventListener('click', (e) => {
      if (Math.abs(_dragOffset) > 4) {
        e.preventDefault();
        e.stopPropagation();
        return;
      }
      e.preventDefault();
      const wasExpanded = _footerRow.classList.contains('expanded');
      _toggleFooter();
      // Impulse: boot the pill in the direction it should go, then spring-back
      // with overshoot so it bounces past the target and settles.
      //   Expanding  → pull UP  (negative) to reveal buttons, overshoot upward
      //   Collapsing → push DOWN (positive) to hide buttons, overshoot downward
      const impulse = wasExpanded ? 24 : -24;
      _footerHandle.classList.remove('spring-back', 'spring-back-click');
      _pillEl.classList.remove('spring-back', 'spring-back-click');
      _pillEl.style.setProperty('--pill-drag-y', impulse + 'px');
      _footerHandle.style.setProperty('--handle-drag-y', impulse + 'px');
      requestAnimationFrame(() => {
        _pillEl.classList.add('spring-back-click');
        _footerHandle.classList.add('spring-back-click');
        _pillEl.style.removeProperty('--pill-drag-y');
        _footerHandle.style.removeProperty('--handle-drag-y');
      });
    });
  }

  // Export for session switch reload
  app.reloadFooterExpanded = () => _applyFooterExpanded(_loadFooterExpanded());

  // Current-turn viewport policy. Begin before the optimistic user bubble is
  // inserted (so addChatBubble cannot tail-follow), then land the completed
  // bubble at the readable top after synchronous attachments are present.
  app.beginUserTurnAnchor = () => {
    if (_activeTurnAnchor) _activeTurnAnchor.classList.remove('turn-anchor');
    _activeTurnAnchor = null;
    _turnAnchorReleasedByUser = false;
    _turnAnchorArmedAt = performance.now();
    _turnAnchorPadPx = 0;
    if (messagesInner) messagesInner.style.paddingBottom = _tailPadPx + 'px';
    _pendingTurnAnchor = true;
    // A new turn replaces whatever turn owned the viewport before it (the
    // previous send, or the session-open landing) — drop that landing so its
    // settle chain cannot pull the reader back to the old message.
    if (typeof app._clearLanding === 'function') app._clearLanding();
  };
  app.anchorUserTurn = (bubble) => {
    _pendingTurnAnchor = false;
    if (!bubble || !messagesEl) return;
    _activeTurnAnchor = bubble;
    bubble.classList.add('turn-anchor');
    // Hand the same intent to the projection layer. An element reference does not
    // survive a reconcile/cache reproject (session-load.js _renderSessionWindowed
    // does a full container.replaceChildren), which is why the bubble could end up
    // hundreds of pixels below the fold mid-run. The focus is registered by
    // durable id — or, until the send is acknowledged and the optimistic bubble is
    // re-stamped, by "the projection's newest user row" — and re-asserted after
    // every projection stage that moves rows above the turn.
    if (typeof app._setTurnFocus === 'function') {
      const durableId = bubble.getAttribute ? bubble.getAttribute('data-msg-id') : null;
      app._setTurnFocus(app.currentSessionId, durableId || null);
    }
    requestAnimationFrame(() => requestAnimationFrame(() => {
      if (!_hasActiveTurnAnchor() || !messagesEl) return;
      const scrollerRect = messagesEl.getBoundingClientRect();
      const bubbleRect = bubble.getBoundingClientRect();
      const target = Math.max(0, messagesEl.scrollTop + bubbleRect.top - scrollerRect.top - _USER_STEP_PAD);
      const maxScrollTop = messagesEl.scrollHeight - messagesEl.clientHeight;
      const clearance = target - maxScrollTop;
      const land = () => {
        if (!_hasActiveTurnAnchor() || !messagesEl) return;
        // A reader gesture that happened AFTER the anchor was armed always wins.
        // `target` is derived from a LIVE read of the scroller, so applying it
        // once the reader has scrolled away recomputes a position near the tail
        // and flings them to the bottom — and the scroll event from that write
        // re-arms tail-follow, pinning them at the bottom for the rest of the
        // run ("the scroll wants to reset to the bottom whenever I'm there").
        // The drift guard below does NOT cover this: it only runs past
        // _TURN_ANCHOR_GRACE_MS, whereas the rAF chain that computes `target`
        // can fire long after the send, so a scroll-up in the first moments
        // after sending hits the unguarded window. Bail out entirely instead —
        // the scroll gesture has already released the anchor via the transcript
        // scroll listener, and the next send re-arms a fresh one.
        if (_transcriptScrollIntentAt > _turnAnchorArmedAt) return;
        // Past the arming grace, a re-land that fires after the reader has
        // scrolled away computes a target far below the viewport and the clamp
        // flings them to the bottom. Only re-assert while the anchored turn is
        // still within a viewport of the reader's top.
        if ((performance.now() - _turnAnchorArmedAt) >= _TURN_ANCHOR_GRACE_MS) {
          const sRect = messagesEl.getBoundingClientRect();
          const bRect = bubble.getBoundingClientRect();
          const drift = Math.abs(bRect.top - sRect.top - _USER_STEP_PAD);
          if (drift > messagesEl.clientHeight) return;
        }
        _scrollLocked = false;
        _programmaticScroll = true;
        messagesEl.scrollTop = target;
        _lastObservedScrollTop = target;
      };
      // A new user bubble is normally the last transcript row. Without
      // enough content below it, browsers clamp scrollTop at the tail and
      // cannot place the bubble at the viewport top. Add the exact uncapped
      // shortfall, then land after layout incorporates the new tail space.
      if (clearance > 0 && messagesInner) {
        const currentPad = Number.parseFloat(getComputedStyle(messagesInner).paddingBottom) || 0;
        _turnAnchorPadPx = Math.max(_turnAnchorPadPx, currentPad + clearance);
        messagesInner.style.paddingBottom = _turnAnchorPadPx + 'px';
        requestAnimationFrame(land);
      } else {
        land();
      }
      // A real scroll event consumes this guard. When the bubble was already
      // perfectly landed and no event fires, clear it shortly afterwards.
      setTimeout(() => {
        if (!_programmaticScroll) return;
        _programmaticScroll = false;
      }, 100);
    }));
  };
  app.hasActiveTurnAnchor = _hasActiveTurnAnchor;
  // session-load.js reads this for the session-open anchor so an open and a
  // send land the user turn at the identical height. Its value was resolved
  // from the active responsive profile at chat initialization.
  // Place the transcript at an exact scrollTop as a VIEWPORT POLICY, not as
  // reader movement — the same treatment land() gives a sent turn. Disarm
  // tail-follow, mark the write as ours, and sync the observed position so the
  // next gesture's direction is measured from where we actually are.
  //
  // session-load.js's session-open anchor MUST use this rather than assigning
  // scrollTop directly: a raw assignment emits a scroll event that the
  // transcript listener reads as reader movement, so it re-derives
  // _scrollLocked from the still-short first-paint geometry (the anchored
  // position sits within _TAIL_FOLLOW_NEAR_PX of the bottom while rows are
  // still appending), tail-follow re-arms, and every following append drags the
  // reader to the tail — the session-open anchor never kept the newest user
  // turn in place.
  app.setTranscriptScrollTop = (target) => {
    if (!messagesEl || !Number.isFinite(target)) return;
    // A landing whose target lies past the current tail is UNREACHABLE: the
    // browser clamps scrollTop and the anchored turn stops short of the readable
    // top, leaving the tail of the previous row occupying it. A SEND covers this
    // by growing an uncapped tail clearance before it lands (see
    // app.anchorUserTurn), but a session open/switch, a reconcile reproject and a
    // header jump all route through here instead — so without the same top-up a
    // switch lands measurably lower than the identical send (measured on a
    // restored stashed projection: asked for 1099, clamped to 1026, newest user
    // turn at 135px with the previous turn's row at the top of the viewport,
    // where a send puts the same turn at 62px).
    if (messagesInner) {
      const maxScrollTop = messagesEl.scrollHeight - messagesEl.clientHeight;
      const clearance = target - maxScrollTop;
      if (clearance > 0) {
        const currentPad = Number.parseFloat(getComputedStyle(messagesInner).paddingBottom) || 0;
        _turnAnchorPadPx = Math.max(_turnAnchorPadPx, currentPad + clearance);
        messagesInner.style.paddingBottom = _turnAnchorPadPx + 'px';
        // Force the new tail space into layout before the write, so the scroll is
        // not clamped against the pre-top-up range. Bounded: once the target is
        // reachable the clearance goes non-positive, so this cannot keep growing.
        void messagesInner.offsetHeight;
      }
    }
    _scrollLocked = false;
    _beginProgrammaticScroll();
    messagesEl.scrollTop = target;
    _lastObservedScrollTop = messagesEl.scrollTop;
  };
  app.getUserTurnAnchorState = () => ({
    active: _hasActiveTurnAnchor(),
    released_by_user: _turnAnchorReleasedByUser,
  });

  // ── Transcript scroll intent and user-turn anchoring ─────────────────────
  if (messagesEl && messagesInner) {
    const markTranscriptScrollIntent = () => {
      _transcriptScrollIntentAt = performance.now();
      // Reader input cancels both a pending settle and a smooth settle already in
      // flight. Without this, the animation's remaining scroll events can be
      // mistaken for a second reader scroll and queue another snap.
      _cancelUserAnchorSnap();
      // A real gesture outranks any pending programmatic marker: the reader is
      // driving now, so the scroll event this gesture produces must be read as
      // intent rather than attributed to a write we armed moments earlier. The
      // marker is otherwise only cleared by its own short timer, which a steady
      // stream of chunks keeps re-arming.
      _programmaticScroll = false;
      window.clearTimeout(_programmaticScrollTimer);
      _programmaticScrollTimer = null;
      // A settle landing (session-load.js _landing) re-asserts the anchored row
      // for a brief window after a window rebuild. The reader always outranks
      // it: drop it on the first real gesture so the settle chain cannot pull
      // them back to the anchor.
      if (typeof app._clearLanding === 'function') app._clearLanding();
    };
    // Only input that begins in the transcript can hand viewport ownership
    // back to the reader. Composer focus/blur and keyboard resize do not.
    messagesEl.addEventListener('wheel', (event) => {
      if (event.deltaY !== 0) markTranscriptScrollIntent();
    }, { passive: true });
    messagesEl.addEventListener('pointerdown', markTranscriptScrollIntent, { passive: true });
    messagesEl.addEventListener('touchstart', (event) => {
      _transcriptTouchY = event.touches[0]?.clientY ?? null;
    }, { passive: true });
    messagesEl.addEventListener('touchmove', (event) => {
      const nextY = event.touches[0]?.clientY;
      if (Number.isFinite(nextY) && Number.isFinite(_transcriptTouchY)
          && Math.abs(nextY - _transcriptTouchY) > 2) markTranscriptScrollIntent();
      _transcriptTouchY = nextY ?? _transcriptTouchY;
    }, { passive: true });
    messagesEl.addEventListener('keydown', (event) => {
      if (['ArrowUp', 'ArrowDown', 'PageUp', 'PageDown', 'Home', 'End', ' '].includes(event.key)) markTranscriptScrollIntent();
    });
    messagesEl.addEventListener('scroll', () => {
      const scrollTop = messagesEl.scrollTop;
      const movedDown = scrollTop > _lastObservedScrollTop + 1;
      const movedUp = scrollTop < _lastObservedScrollTop - 1;
      const snapInFlight = !!_userAnchorSnapInFlight;
      const programmatic = _programmaticScroll || snapInFlight;
      const transcriptIntent = performance.now() - _transcriptScrollIntentAt < _TRANSCRIPT_SCROLL_INTENT_MS;
      _lastObservedScrollTop = scrollTop;
      // A soft snap owns every scroll event until its own scrollend. In
      // particular, do not use the tail of its smooth animation as fresh reader
      // intent and schedule another settle inside the same 800ms gesture window.
      if (snapInFlight) return;
      // Arm or disarm tail-follow from where the reader actually is. Previously
      // this flag was only ever set at session load, so a session opened at the
      // bottom kept following for the rest of its life: every arriving chunk
      // called _scrollToBottomIfNear and yanked the reader back down.
      const atTail = messagesEl.scrollHeight - messagesEl.clientHeight - scrollTop
        <= _TAIL_FOLLOW_NEAR_PX;
      // Arm when the reader is at the tail, disarm when they are not.
      //
      // Intent vs. programmatic marker: a real gesture in the transcript always
      // counts, because markTranscriptScrollIntent clears the marker for the
      // event the gesture produces. Without a gesture — a scrollbar drag, the
      // tail of a fling, or a reflow — the geometry is trusted only when the
      // event was not one of our own writes. Neither signal alone is enough:
      // while a reply streams, every chunk calls _beginProgrammaticScroll() for
      // its own write, so gating solely on the marker read a reader's swipe as
      // programmatic and never released the follow (the next chunk then yanked
      // them back down); and gating solely on intent would never re-arm the
      // follow when the reader reaches the tail without a wheel/touch/key
      // gesture. Either signal is therefore sufficient to re-derive the lock
      // from where the reader actually is.
      if (transcriptIntent || !programmatic) {
        _scrollLocked = atTail;
      }
      // Keyboard focus, visual-viewport resize, and reflow can emit trusted
      // scroll events. Release only after a reader gesture within the
      // transcript, so mobile composer focus cannot discard the anchor.
      //
      // Any deliberate gesture that actually moves the transcript releases the
      // anchor — up as well as down. Gating this on movedDown alone left the
      // anchor armed when the reader scrolled UP to re-read, and the next
      // composer/visual-viewport reflow then re-landed the turn and dragged
      // them back down ("the scroll resets whenever I'm there").
      if (!programmatic && transcriptIntent && (movedDown || movedUp) && _hasActiveTurnAnchor()) {
        _releaseTurnAnchor({ userScrolled: true });
      }
      if (!programmatic && transcriptIntent && (movedDown || movedUp)) {
        _lastTranscriptScrollDirectionAt = performance.now();
        _queueUserAnchorSnap(messagesEl);
      }
    }, { passive: true });
    messagesEl.addEventListener('scrollend', () => {
      // Completion belongs to the snap that issued the smooth scroll. It must not
      // be reinterpreted as a second settled reader scroll.
      if (_finishUserAnchorSnap()) return;
      if (performance.now() - _lastTranscriptScrollDirectionAt < _TRANSCRIPT_SCROLL_INTENT_MS) {
        _snapToNearbyUserAnchor(messagesEl);
      }
    }, { passive: true });

    // ── Footer gesture forwarding (wheel + touch) ──────────────────────────
    // A gesture that begins over the footer is routed to the transcript and
    // reported as transcript intent, so the turn-anchor and soft-snap logic
    // treats it exactly like a gesture over the messages themselves (the reader
    // is scrolling; the current turn must not reclaim the viewport). See
    // footer-gesture-forward.js — the footer has no scrollable ancestor, so
    // without this the gesture has nowhere to go.
    forwardFooterGestures({
      targets: [
        document.getElementById('chat-input-area'),
        document.getElementById('chat-footer-row'),
      ],
      scroller: messagesEl,
      markIntent: markTranscriptScrollIntent,
    });

  }

  // ── Header user-turn navigation (#chat-anchor-nav) ──────────────────────
  // The paired arrows share _prev/_nextUserTarget with transcript scrolling.
  // When a target is virtualized, session-load repositions from durable history.
  const _configureAnchorNav = () => {
    const nav = document.getElementById('chat-anchor-nav');
    const prev = document.getElementById('chat-anchor-prev-btn');
    const next = document.getElementById('chat-anchor-next-btn');
    if (!nav || !prev || !next) return;
    const ui = getAgentChatUi();
    const surface = window.__CHAT_PORTAL__
      ? ui.chat_widget
      : (_isNarrowPanel() ? ui.chat_mobile : ui.chat_desktop);
    const cfg = {
      ...(ui.chat_common?.chat_header?.anchor_navigation || {}),
      ...(surface?.chat_header?.anchor_navigation || {}),
    };
    nav.hidden = cfg.enabled === false;
    if (cfg.up_aria_label) {
      prev.setAttribute('aria-label', cfg.up_aria_label);
      prev.title = cfg.up_aria_label;
    }
    if (cfg.down_aria_label) {
      next.setAttribute('aria-label', cfg.down_aria_label);
      next.title = cfg.down_aria_label;
    }
    if (nav.dataset.wired === 'true') return;
    nav.dataset.wired = 'true';
    const jump = async (direction, button) => {
      // Header navigation is deliberate reader navigation, so it must supersede
      // both a nearby settle and the live newest-turn focus before choosing a row.
      _cancelUserAnchorSnap();
      if (typeof app._clearLanding === 'function') app._clearLanding();
      _releaseTurnAnchor({ userScrolled: true });
      const target = direction === 'up'
        ? _prevUserTarget(messagesEl)
        : _nextUserTarget(messagesEl);
      if (target !== null) {
        _scrollLocked = false;
        _beginProgrammaticScroll();
        messagesEl.scrollTop = target;
        _lastObservedScrollTop = target;
        return;
      }
      const sessionId = app.currentSessionId;
      const fallback = direction === 'up' ? app._stepToPrevUserMessage : app._stepToNextUserMessage;
      if (!sessionId || typeof fallback !== 'function') return;
      button.disabled = true;
      button.setAttribute('aria-busy', 'true');
      try {
        await fallback(sessionId);
      } finally {
        button.disabled = false;
        button.removeAttribute('aria-busy');
      }
    };
    prev.addEventListener('click', () => { prev.blur(); jump('up', prev); });
    next.addEventListener('click', () => { next.blur(); jump('down', next); });
  };
  _configureAnchorNav();

  // Expose the tail-follow helper for session-load.js.
  app._scrollToBottomIfNear = _scrollToBottomIfNear;
  Object.defineProperty(app, '_scrollLocked', {
    get: () => _scrollLocked,
    set: (v) => { _scrollLocked = v; },
  });
}

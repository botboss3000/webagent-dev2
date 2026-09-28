'use strict';

// Chat virtual scrolling — swaps off-window bubbles for height-preserving
// placeholders and materialises only the window around the viewport.
// Hooks app.addChatBubble. Module map: ui/chat/js/README.md.

import { app } from '../../shared/js/state.js';
import {
  addChatBubble,
  linkifyText,
  _renderMarkdownBody,
  _setBubbleCreatedAt,
  _setBubbleSessionSeq,
  applyUserMessageTruncation,
} from './chat-bubble.js';
import { _addBubbleActions } from './chat-bubble-actions.js';
import { _messageCache } from './chat-message-cache.js';
import { _stripToolCalls } from './chat-stream.js?v=260';
import { renderAttachmentElement } from '../../shared/js/attachments.js';
import { isMessageTypeVisible, messageTypeOf } from '../../shared/js/chat-visibility.js';

// ── Virtual-scroll state ──────────────────────────────────────────────────
const _bubbleHeights = new Map();
// Set of msgIds currently rendered as placeholders
const _placeholderIds = new Set();
let _virtualScrollHandler = null;
const _VS_BUFFER = 400;
let _origAddChatBubble = null;

// Guard flag for concurrent scroll-triggered loads
let _loadingMoreMessages = false;

function _storeBubbleHeight(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId) return;
  const h = bubble.offsetHeight;
  if (h > 0) _bubbleHeights.set(msgId, h);
}

function _getBubbleHeight(msgId) {
  const h = _bubbleHeights.get(msgId);
  return h || 80; // fallback for unmeasured bubbles
}

// Chat zoom (ui/chat/js/chat-zoom.js) applies CSS `zoom` to #chat-messages-inner,
// which scales every inline length of the children. Bubble heights are stored in
// ZOOMED px (offsetHeight), so a placeholder's inline height must be the
// UNZOOMED value — otherwise it would render zoom² too tall. offsetTop / offsetHeight
// are already zoomed, so all the bounds math below stays in zoomed px.
function _containerZoom() {
  const z = app.chatMessages && parseFloat(app.chatMessages.style.zoom);
  return Number.isFinite(z) && z > 0 ? z : 1;
}

function _makePlaceholder(msgId, height) {
  const el = document.createElement('div');
  el.className = 'chat-bubble-placeholder';
  el.dataset.msgId = msgId;
  el.style.height = (height / _containerZoom()) + 'px';
  return el;
}

function _recyclePlaceholderToBubble(placeholder, msgId) {
  const sessionId = app.currentSessionId;
  if (!sessionId) return;
  // Activity groups have synthetic ids, not raw transcript message ids.
  // Rehydrate them through the projection owner before falling back to a normal
  // cached message bubble.
  if (typeof app._rehydrateVirtualActivityGroup === 'function') {
    const activity = app._rehydrateVirtualActivityGroup(msgId, placeholder);
    if (activity) {
      _placeholderIds.delete(msgId);
      _storeBubbleHeight(activity);
      return;
    }
  }
  const cached = _messageCache.get(sessionId);
  if (!cached) return;
  const msg = cached.messages.find(m => m.id === msgId);
  if (!msg) return;
  // Hidden lane: keep the placeholder standing so scroll geometry stays stable;
  // a visibility re-toggle re-renders the transcript from the cache.
  const mtype = messageTypeOf(msg);
  if (mtype && !isMessageTypeVisible(mtype)) return;

  _placeholderIds.delete(msgId);

  const _container = app.chatMessages;
  const _phTop = placeholder.offsetTop;
  const _phHeight = placeholder.offsetHeight;
  const _scroller = app._chatScroller || (_container && _container.parentElement);
  const _scrollTop = _scroller ? _scroller.scrollTop : 0;

  const bubble = document.createElement('div');
  let role = msg.role === 'user' ? 'user' : 'agent';
  let extraClass = null;
  // Output Closer rows use the same standalone content-only bubble as the live
  // path, including after virtual-scroll recycling.
  const isOverview = msg.role === 'system'
    && (msg.source === 'system:summary' || msg.source === 'system:overview' || msg.source === 'system:closer');
  if (msg.role === 'assistant') {
    if (msg.status === 'streaming') extraClass = 'streaming';
    else if (msg.status === 'interrupted') extraClass = 'interrupted';
    else if (msg.status === 'error') extraClass = 'error';
  }
  if (isOverview && typeof app.renderSummary === 'function') {
    const canonical = app.renderSummary(msg.content || '', msg.id || msgId, msg.created_at, msg.parent_id || '');
    if (canonical) {
      placeholder.remove();
      return;
    }
  }
  if (isOverview) extraClass = 'terminal-closer';
  bubble.className = 'chat-bubble ' + role + (extraClass ? ' ' + extraClass : '');
  if (msg.id) bubble.setAttribute('data-msg-id', msg.id);

  _setBubbleCreatedAt(bubble, msg.created_at);
  _setBubbleSessionSeq(bubble, msg.session_seq);

  if (role === 'user') {
    const label = document.createElement('span');
    label.className = 'label';
    label.textContent = 'You';
    const body = document.createElement('div');
    body.className = 'bubble-body';
    body.appendChild(linkifyText(msg.content || ''));
    bubble.append(label, body);
    // Recycled rows bypass addChatBubble, so rebuild the canonical attachment
    // children before the shared footer renderer wraps the user turn.
    for (const attachment of Array.isArray(msg.attachments) ? msg.attachments : []) {
      const element = renderAttachmentElement(attachment, { thumbnail: true });
      if (element) bubble.appendChild(element);
    }
  } else {
    let text = msg.content || '';
    text = _stripToolCalls(text);
    // Same rule as addChatBubble: normal agent prose keeps its label; Closer,
    // tool rows, and empty shells remain unlabeled.
    if (!isOverview && text.trim() && msg.role !== 'tool' && msg.role !== 'tool_result') {
      const label = document.createElement('span');
      label.className = 'label';
      label.textContent = 'Agent';
      bubble.appendChild(label);
    }
    if (text.trim()) {
      const body = _renderMarkdownBody(text, true);
      if (body) {
        bubble.appendChild(body);
        bubble.classList.add('md');
        bubble.__mdSource = text;
      } else {
        bubble.appendChild(linkifyText(text));
      }
    }
    if (extraClass === 'streaming') {
      const stopBtn = document.createElement('button');
      stopBtn.className = 'stop-btn';
      stopBtn.textContent = '\u23F9';
      stopBtn.addEventListener('click', () => {
        if (typeof app._sendStopMessage === 'function') app._sendStopMessage();
      });
      bubble.appendChild(stopBtn);
    }
  }

  if (typeof app._addBubbleActions === 'function') {
    try { app._addBubbleActions(bubble); } catch(_) {}
  }

  placeholder.parentNode.replaceChild(bubble, placeholder);
  if (role === 'user') applyUserMessageTruncation(bubble);
  if (isOverview && typeof app.suppressMatchingResponsePreview === 'function') {
    try { app.suppressMatchingResponsePreview(msg.content || '', msg.parent_id || ''); } catch (_) {}
  }
  _storeBubbleHeight(bubble);

  // Persisted tool bodies are deliberately absent from the light transcript.
  // Restore just this bubble's lightweight headings when it enters the window;
  // expanding a heading still performs the existing lazy detail fetch.
  if (typeof app._rehydrateVirtualBubble === 'function') {
    try { app._rehydrateVirtualBubble(msgId, bubble); } catch (_) {}
  }

  if (_container && (_phTop + _phHeight) <= _scrollTop) {
    const delta = bubble.offsetHeight - _phHeight;
    // During a live response, chat-stream.js owns the viewport: it keeps the
    // tail visible as each token changes the streaming bubble. Do not counter
    // that with a virtual-scroll anchor correction for an older placeholder;
    // alternating scrollTop writes here and in tail-follow caused the transcript
    // to oscillate vertically. Manual browsing and idle transcript restoration
    // still preserve their viewport with the normal correction below.
    const tailFollowOwnsScroll = app.isProcessing && app._scrollLocked === true;
    if (delta && !tailFollowOwnsScroll) {
      if (_scroller) _scroller.scrollTop = _scrollTop + delta;
    }
  }
}

function _recycleBubbleToPlaceholder(bubble) {
  const msgId = bubble.getAttribute('data-msg-id');
  if (!msgId || !bubble.parentNode) return false;

  // Never evict mutable or interactive state. Finalized transcript bubbles are
  // reproducible from the message cache; a live stream, focused control, open
  // menu, or expanded details panel is not.
  if (bubble.classList.contains('streaming')
      || bubble.classList.contains('session-placeholder')
      || bubble.contains(document.activeElement)
      || bubble.querySelector('details[open], [aria-expanded="true"], .is-open, .open')) {
    return false;
  }

  const sessionId = app.currentSessionId;
  const cached = sessionId ? _messageCache.get(sessionId) : null;
  if (!cached || !cached.messages.some(m => m.id === msgId)) return false;

  _storeBubbleHeight(bubble);
  const placeholder = _makePlaceholder(msgId, _getBubbleHeight(msgId));
  const sessionSeq = bubble.getAttribute('data-session-seq');
  if (sessionSeq != null) placeholder.setAttribute('data-session-seq', sessionSeq);
  bubble.parentNode.replaceChild(placeholder, bubble);
  _placeholderIds.add(msgId);
  return true;
}

function _recycleVisible() {
  const container = app.chatMessages;
  if (!container) return;
  const scroller = app._chatScroller || container.parentElement;
  const rect = scroller.getBoundingClientRect();
  const scrollTop = scroller.scrollTop;
  const bufferTop = scrollTop - _VS_BUFFER;
  const bufferBottom = scrollTop + rect.height + _VS_BUFFER;

  // Evict finalized bubbles that have left the buffered viewport. Without this
  // pass, walking through a long session permanently accumulated every parsed
  // Markdown tree in the DOM even though the initial load was windowed.
  const bubbles = Array.from(container.children).filter(
    c => c.classList.contains('chat-bubble')
  );
  for (const el of bubbles) {
    const elTop = el.offsetTop;
    const elBottom = elTop + el.offsetHeight;
    if (elBottom <= bufferTop || elTop >= bufferBottom) {
      _recycleBubbleToPlaceholder(el);
    }
  }

  const placeholders = Array.from(container.children).filter(
    c => c.classList.contains('chat-bubble-placeholder')
  );

  for (const el of placeholders) {
    const msgId = el.getAttribute('data-msg-id');
    if (!msgId) continue;

    const z = _containerZoom();
    const elTop = el.offsetTop;
    // Inline height is stored UNZOOMED (see _makePlaceholder) — convert back
    // to zoomed px so the viewport comparison stays in one space.
    const elBottom = elTop + ((parseInt(el.style.height, 10) || 0) * z || _getBubbleHeight(msgId));
    const isVisible = elBottom > bufferTop && elTop < bufferBottom;

    if (isVisible) _recyclePlaceholderToBubble(el, msgId);
  }

  // A placeholder→bubble swap changes the heights above the anchor, so a live
  // turn focus (send or session-open default) re-asserts its landing here. No-op
  // when no landing is live — e.g. after the reader's own gesture cleared it.
  if (typeof app._reLandAnchor === 'function') app._reLandAnchor();
}

function _hookAddChatBubble() {
  if (_origAddChatBubble) return;
  _origAddChatBubble = app.addChatBubble;
  app.addChatBubble = function(role, text, extraClass, imageUrl, turnId, msgId, createdAt) {
    const bubble = _origAddChatBubble.call(app, role, text, extraClass, imageUrl, turnId, msgId, createdAt);
    requestAnimationFrame(() => _storeBubbleHeight(bubble));
    return bubble;
  };
}

function _installVirtualScroll() {
  const container = app.chatMessages;
  if (!container) return;
  // #chat-messages-inner is the content; #chat-messages is the scrollable parent.
  const scroller = container.parentElement;
  if (!scroller) return;

  _hookAddChatBubble();

  if (_virtualScrollHandler) {
    scroller.removeEventListener('scroll', _virtualScrollHandler);
  }

  container.querySelectorAll('.chat-bubble').forEach(b => _storeBubbleHeight(b));

  let _vsTimer = null;
  _virtualScrollHandler = () => {
    if (_vsTimer) cancelAnimationFrame(_vsTimer);
    _vsTimer = requestAnimationFrame(() => {
      _recycleVisible();
      if (typeof app._maybeLoadMoreOnScrollTop === 'function') {
        app._maybeLoadMoreOnScrollTop(app.currentSessionId);
      }
      if (typeof app._maybeLoadMoreOnScrollBottom === 'function') {
        app._maybeLoadMoreOnScrollBottom(app.currentSessionId);
      }
      _vsTimer = null;
    });
  };
  scroller.addEventListener('scroll', _virtualScrollHandler, { passive: true });

  // Direct (non-debounced) scroll listener — fires on every scroll event so
  // the hard-boundary check catches the moment the user hits the very top or
  // bottom, before browser momentum/overscroll can shift scrollTop away.
  let _hbtListener = () => {
    if (typeof app._checkHardScrollBoundary === 'function') {
      app._checkHardScrollBoundary(app.currentSessionId);
    }
  };
  scroller.addEventListener('scroll', _hbtListener, { passive: true });
  // Store the scrollable parent so other modules can read scrollTop correctly.
  // app.chatMessages (#chat-messages-inner) is the content; its parent
  // (#chat-messages) is the element that actually scrolls.
  app._chatScroller = scroller;
  // Stash for teardown
  container._hbtListener = _hbtListener;

  _recycleVisible();
}

// Chat zoom (ui/chat/js/chat-zoom.js) rescales the cached bubble heights by
// the zoom ratio after a level change. Heights are stored in zoomed px, so
// without this, placeholders recycled after a zoom would be sized for the old
// level. Live bubbles reflow on their own; only the cache needs the ratio.
function scaleVirtualScrollHeights(ratio) {
  if (!Number.isFinite(ratio) || ratio <= 0) return;
  for (const [msgId, h] of _bubbleHeights) _bubbleHeights.set(msgId, h * ratio);
}

function _teardownVirtualScroll() {
  const container = app.chatMessages;
  const scroller = app._chatScroller || (container && container.parentElement);
  if (_virtualScrollHandler && scroller) {
    scroller.removeEventListener('scroll', _virtualScrollHandler);
  }
  if (scroller && container && container._hbtListener) {
    scroller.removeEventListener('scroll', container._hbtListener);
    container._hbtListener = null;
  }
  _virtualScrollHandler = null;
  if (_origAddChatBubble) {
    app.addChatBubble = _origAddChatBubble;
    _origAddChatBubble = null;
  }
  // Every caller tears down immediately before replacing or hiding this
  // transcript. Do not materialize the entire virtualized history just to throw
  // it away; that was an O(session-size) Markdown parse on every session switch.
  _placeholderIds.clear();
}

export {
  _placeholderIds,
  _storeBubbleHeight,
  _getBubbleHeight,
  _makePlaceholder,
  _containerZoom,
  _installVirtualScroll,
  _teardownVirtualScroll,
  scaleVirtualScrollHeights,
};

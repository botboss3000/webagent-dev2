/**
 * footer-gesture-forward.js — route wheel and touch gestures that begin over the
 * chat footer to the transcript scroller.
 *
 * The footer (composer pill + control rows) has NO scrollable ancestor —
 * #chat-panel, #stage and <body> are all overflow:hidden — so a gesture that
 * starts there has nowhere to go and never reaches the transcript. Forward both
 * wheel and touch drags to the transcript and report them as transcript intent,
 * so the turn-anchor and soft-snap logic treats them exactly like a gesture over
 * the messages themselves (the reader is scrolling; the current turn must not
 * reclaim the viewport).
 *
 * Keep in sync with the composer pill's `applyRubberBand({ wheel: false })`
 * call (chat-ui.js): that guard is what stops the pill preventDefault()ing the
 * same gesture at a boundary it can never satisfy.
 *
 * Options:
 *   targets    iterable of footer elements to wire (wiring is idempotent)
 *   scroller   the transcript scroller that owns the scroll position
 *   markIntent () => void — records reader scroll intent for the caller
 */
'use strict';

// A touch drag must travel this far before it counts as a scroll rather than a
// tap, so a tap still focuses the composer and a long-press still selects text.
const TOUCH_ENGAGE_PX = 6;

/**
 * True when the gesture target sits inside a scrollable descendant of `root` —
 * a nested scroller that should own the gesture. Mirrors the guard in
 * rubber-band.js so e.g. a scrollable textarea inside the footer keeps its swipe.
 */
function _isInsideScrollableChild(root, target) {
  let node = target && target.nodeType === 1 ? target : null;
  while (node && node !== root && node !== document.body && node !== document.documentElement) {
    const cs = window.getComputedStyle(node);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll' || cs.overflowY === 'overlay')
        && node.scrollHeight > node.clientHeight + 1) return true;
    node = node.parentElement;
  }
  return false;
}

export function forwardFooterGestures(opts) {
  const options = opts || {};
  const scroller = options.scroller;
  const markIntent = options.markIntent;
  const targets = options.targets || [];
  if (!scroller) return;

  for (const el of targets) {
    if (!el || el.dataset.chatFooterGestureWired === '1') continue;
    el.dataset.chatFooterGestureWired = '1';

    // ── Wheel ──────────────────────────────────────────────────────────────
    el.addEventListener('wheel', (event) => {
      // Ctrl+wheel is the chat pinch-zoom gesture (chat-zoom owns it).
      if (event.ctrlKey || event.defaultPrevented || !event.deltaY) return;
      // A nested scroller inside the footer keeps its own gesture.
      if (_isInsideScrollableChild(el, event.target)) return;
      event.preventDefault();
      if (markIntent) markIntent();
      const delta = event.deltaMode === 1 ? event.deltaY * 16
        : event.deltaMode === 2 ? event.deltaY * scroller.clientHeight
        : event.deltaY;
      scroller.scrollBy({ top: delta, behavior: 'auto' });
    }, { passive: false });

    // ── Touch ──────────────────────────────────────────────────────────────
    // Touch has no wheel-style chaining: the browser only scrolls the touch
    // target's scrollable ANCESTOR, and the footer has none. So the drag must be
    // translated into scrollBy() by hand, 1:1 with the finger.
    let active = false;
    let engaged = false;
    let startY = 0;
    let lastY = 0;
    el.addEventListener('touchstart', (event) => {
      active = false;
      engaged = false;
      const t = event.touches && event.touches[0];
      if (!t) return;
      // A nested scroller inside the footer keeps its own gesture.
      if (_isInsideScrollableChild(el, event.target)) return;
      active = true;
      startY = lastY = t.clientY;
    }, { passive: true });
    el.addEventListener('touchmove', (event) => {
      if (!active) return;
      // A descendant forwarder (e.g. #chat-footer-row inside #chat-input-area)
      // already claimed this move — never double-scroll.
      if (event.defaultPrevented) { active = false; return; }
      const t = event.touches && event.touches[0];
      if (!t) return;
      if (!engaged) {
        if (Math.abs(t.clientY - startY) < TOUCH_ENGAGE_PX) return;
        engaged = true;
        // The drag is a scroll, not a caret placement — drop any selection the
        // touchstart began in the composer so text does not smear while scrolling.
        const sel = window.getSelection && window.getSelection();
        if (sel && sel.removeAllRanges) sel.removeAllRanges();
      }
      const dy = lastY - t.clientY;   // > 0 = finger up = scroll down
      lastY = t.clientY;
      if (!dy) return;
      event.preventDefault();
      if (markIntent) markIntent();
      scroller.scrollBy({ top: dy, behavior: 'auto' });
    }, { passive: false });
    const release = () => { active = false; engaged = false; };
    el.addEventListener('touchend', release, { passive: true });
    el.addEventListener('touchcancel', release, { passive: true });
  }
}

/**
 * rubber-band.js — attach a spring-back overscroll effect to any scrollable
 * element. Same cubic-bezier as the chat footer drag handle.
 *
 * Usage:
 *   import { applyRubberBand } from '../../shared/js/rubber-band.js';
 *   applyRubberBand(scrollerEl);                       // horizontal default
 *   applyRubberBand(scrollerEl, { axis: 'y' });        // vertical
 *   applyRubberBand(scrollerEl, { axis: 'both' });     // both axes
 *   applyRubberBand(scrollerEl, { axis: 'y', pullThreshold: 40, maxPull: 50, onTriggerTop: fn });
 *
 * Adds wheel + touch handlers that at scroll boundaries apply a diminishing
 * translate to the element, then snap back with an overshoot spring curve.
 *
 * Options:
 *   axis             'x' | 'y' | 'both'   scroll axis (default 'x')
 *   pullThreshold    number               px of pull before a trigger fires on release (default 30)
 *   pullThresholdTop    number            px of pull before onTriggerTop fires (default: pullThreshold)
 *   pullThresholdBottom number            px of pull before onTriggerBottom fires (default: pullThreshold)
 *   bottomEdgeTol    number|fn            float-slop tolerance, in px, below the true scroll
 *                                         bottom that still counts as "at the bottom edge" for a
 *                                         bottom pull (default 0.5), or a function returning
 *                                         that value live. The pull arms ONLY when the scroller
 *                                         is actually at the bottom; if the user is still short
 *                                         of the bottom, the gesture scrolls natively and the
 *                                         pull engages once the scroller reaches it. Never
 *                                         jumps the scroll position.
 *   maxPull          number               max px the element can be pulled (default 150)
 *   resistance       number               input-to-pull response multiplier (default 0.35)
 *   snapDuration     number               spring-back duration in ms (default 450)
 *   snapEasing       string               CSS timing function for spring-back (default overshoot spring)
 *   wheel            boolean              false = NEVER intercept wheel gestures (the touch
 *                                         spring-back still applies). Use for an element with no
 *                                         scrollable range of its own that sits over a real scroller —
 *                                         e.g. the chat composer pill over the transcript — so the
 *                                         wheel can chain to that scroller instead of being swallowed
 *                                         by a boundary pull the element can never satisfy.
 *                                         (default true)
 *   onTriggerTop     () => void           called when user pulls past threshold at the top and releases
 *   onPullTop        (ratio: 0..1)        called continuously while pulling at the top
 *   onTriggerBottom  () => void           called when user pulls past threshold at the bottom and releases
 *   onPullBottom     (ratio: 0..1)        called continuously while pulling at the bottom
 */
'use strict';

var SPRING = 'cubic-bezier(0.34, 1.56, 0.64, 1)';
var RESIST  = 0.35;

function makeAxis(axis) {
  var isX = axis === 'x';
  return {
    isX: isX,
    deltaWheel:  isX ? 'deltaX' : 'deltaY',
    deltaTouch:  isX ? 0 : 1,
    scrollSize:  isX ? function (e) { return e.scrollWidth  - e.clientWidth;  } : function (e) { return e.scrollHeight - e.clientHeight; },
    scrollPos:   isX ? function (e) { return e.scrollLeft; } : function (e) { return e.scrollTop; }
  };
}

/**
 * True when the gesture target sits inside a scrollable descendant of `el`
 * (a nested scroller that should own the gesture). Walks from the target up
 * to `el`, matching any overflow-y auto/scroll/overlay box that can actually
 * scroll. Lets nested scrollables (e.g. the bubble tool-call accordion inside
 * a chat bubble) keep their swipes instead of the rubber band stealing them
 * at `el`'s scroll boundaries with preventDefault.
 */
function _isInsideNestedScroller(el, target) {
  var node = target && target.nodeType === 1 ? target : null;
  while (node && node !== el && node !== document.body && node !== document.documentElement) {
    var cs = window.getComputedStyle(node);
    if ((cs.overflowY === 'auto' || cs.overflowY === 'scroll' || cs.overflowY === 'overlay')
        && node.scrollHeight > node.clientHeight + 1) {
      return true;
    }
    node = node.parentElement;
  }
  return false;
}

export function applyRubberBand(el, opts) {
  if (!el || el.dataset.rbApplied === '1') return;

  opts = opts || {};
  var axis = opts.axis === 'y' ? 'y' : opts.axis === 'both' ? 'both' : 'x';
  var pullThreshold = opts.pullThreshold || 30;
  var pullThresholdTop    = opts.pullThresholdTop    || pullThreshold;
  var pullThresholdBottom = opts.pullThresholdBottom || pullThreshold;
  var bottomEdgeTol = opts.bottomEdgeTol != null ? opts.bottomEdgeTol : 0.5;
  var maxPull = opts.maxPull != null ? opts.maxPull : 150;
  // Keep the legacy feel unless a surface explicitly opts into tighter,
  // faster feedback (the chat transcript does).
  var resistance = opts.resistance != null ? opts.resistance : RESIST;
  var snapDuration = opts.snapDuration != null ? opts.snapDuration : 450;
  var snapEasing = opts.snapEasing || SPRING;
  // See the `wheel` option above. Guarded (not just documented) because a
  // boundary pull on a non-scrolling element turns every wheel tick into a
  // preventDefault() — which strands the gesture before any outer scroller can
  // use it.
  var wheelEnabled = opts.wheel !== false;
  var onTriggerTop = opts.onTriggerTop || null;
  var onPullTop    = opts.onPullTop    || null;
  var onTriggerBottom = opts.onTriggerBottom || null;
  var onPullBottom    = opts.onPullBottom    || null;

  // ── Per-axis state ──
  var axes = axis === 'both' ? [makeAxis('x'), makeAxis('y')] : [makeAxis(axis)];
  var rbPos     = axes.map(function() { return 0; });
  var triggeredTop    = axes.map(function() { return false; });
  var triggeredBottom = axes.map(function() { return false; });
  var rbTimer   = axes.map(function() { return null; });
  var touchLastX = 0, touchLastY = 0, touchActive = false, touchInsideNested = false;

  function rbLog(msg) {
    // Diagnostic only — no-op in production. Enable live from the console:
    //   window.__RUBBER_BAND_DEBUG__ = true
    if (!window.__RUBBER_BAND_DEBUG__) return;
    console.log('DEBUG-TAG:[rubber-band] ' + msg);
  }

  function notifyPull(i) {
    if (!onPullTop && !onPullBottom) return;
    var absPull = Math.abs(rbPos[i]);
    var th = rbPos[i] > 0 ? pullThresholdTop : pullThresholdBottom;
    var ratio = Math.min(absPull / th, 1);
    if (rbPos[i] > 0 && onPullTop) onPullTop(ratio, absPull);
    else if (rbPos[i] < 0 && onPullBottom) onPullBottom(ratio, absPull);
  }

  function snap(i) {
    if (Math.abs(rbPos[i]) < 0.5) {
      // Sub-threshold release (e.g. a single trackpad tick left a <0.5px
      // residual pull). STILL report the release: consumers that showed an
      // indicator while pulling (e.g. the chat "Pull to refresh" spinner)
      // must get the ratio-0 callback or they can stay visible with no
      // active pull. This is the root cause of a stuck spinner.
      rbLog('snap() sub-threshold release (rbPos=' + rbPos[i] + ') → notifying consumers ratio=0');
      triggeredTop[i] = false;
      triggeredBottom[i] = false;
      _applyTransform();
      clearTimeout(rbTimer[i]);
      if (onPullTop) onPullTop(0, 0);
      if (onPullBottom) onPullBottom(0, 0);
      return;
    }
    var wasTriggeredTop = triggeredTop[i];
    var wasTriggeredBottom = triggeredBottom[i];
    var wasAbsPull = Math.abs(rbPos[i]);
    rbLog('snap() firing — rbPos=' + rbPos[i] + ' wasTriggeredTop=' + wasTriggeredTop + ' wasTriggeredBottom=' + wasTriggeredBottom + (wasTriggeredBottom ? ' → onTriggerBottom WILL fire' : ''));
    el.style.transition = 'transform ' + (snapDuration / 1000) + 's ' + snapEasing;
    rbPos[i] = 0;
    triggeredTop[i] = false;
    triggeredBottom[i] = false;
    _applyTransform();
    clearTimeout(rbTimer[i]);
    if (wasTriggeredTop && onTriggerTop) onTriggerTop(wasAbsPull);
    if (wasTriggeredBottom && onTriggerBottom) onTriggerBottom(wasAbsPull);
    if (onPullTop) onPullTop(0, 0);
    if (onPullBottom) onPullBottom(0, 0);
    rbTimer[i] = setTimeout(function() { el.style.transition = ''; }, snapDuration + 50);
  }

  function _applyTransform() {
    var x = rbPos[0] || 0, y = axes.length > 1 ? (rbPos[1] || 0) : 0;
    if (Math.abs(x) < 0.5 && Math.abs(y) < 0.5) {
      el.style.transform = '';
    } else if (axes.length > 1) {
      el.style.transform = 'translate(' + x + 'px, ' + y + 'px)';
    } else {
      el.style.transform = (axes[0].isX ? 'translateX(' : 'translateY(') + (x || y) + 'px)';
    }
  }

  /**
   * Bottom-edge check. Returns true when `d` is a downward pull (delta > 0)
   * and the scroller is within bottomEdgeTol (default 0.5) px of the true
   * bottom — i.e. ACTUALLY at the bottom, allowing only float slop.
   * bottomEdgeTol may be a function returning the current tolerance. If the
   * user is still short of the bottom, return false so the caller lets the
   * native scroll finish the approach; the pull arms on a later move once
   * the scroller reaches the bottom. Never jumps the scroll position.
   */
  function atBottomEdge(a, d) {
    if (d <= 0) return false;
    var maxS = a.scrollSize(el);
    var tol = typeof bottomEdgeTol === 'function' ? bottomEdgeTol() : bottomEdgeTol;
    var remaining = maxS - a.scrollPos(el);
    if (remaining > tol) return false;
    rbLog('bottom edge: remaining=' + remaining.toFixed(1) + 'px ≤ tol=' + tol + ' → arm bottom pull (no jump)');
    return true;
  }

  /**
   * True when the element actually has scrollable range on this axis. An element
   * with NO range (scrollSize 0) is pinned at BOTH boundaries at once, so the
   * top test (`scrollPos <= 0`) is true for every downward gesture and the
   * bottom test for every upward one. Arming a pull there preventDefault()s the
   * gesture and springs the element instead of letting the gesture chain — the
   * chat composer pill over the transcript did exactly that on every touch
   * drag, which the reader perceives as "scrolling snaps me back down". A
   * boundary pull is only meaningful when there is range to be pulled past.
   */
  function hasScrollRange(a) {
    return a.scrollSize(el) > 0;
  }

  function pull(axisIdx, delta) {
    var a = axes[axisIdx];
    var i = axisIdx;
    clearTimeout(rbTimer[i]);
    el.style.transition = 'none';
    var maxS = a.scrollSize(el);
    var atStart = hasScrollRange(a) && a.scrollPos(el) <= 0 && delta < 0;
    var atEnd   = hasScrollRange(a) && atBottomEdge(a, delta);
    rbLog('pull(axis=' + (a.isX ? 'x' : 'y') + ', delta=' + delta + ') atStart=' + atStart + ' atEnd=' + atEnd + ' scrollPos=' + a.scrollPos(el).toFixed(1) + '/' + maxS.toFixed(1));
    if (!atStart && !atEnd) {
      // The gesture left the boundary (e.g. the user scrolled back the other
      // way mid-pull). Release ALWAYS — the pending 120ms timer was cleared
      // above, so skipping the snap here would strand the pull state (and any
      // visible indicator) forever when the residual pull is <0.5px. snap()
      // is a no-op-safe release at any residual size.
      rbLog('pull() left boundary → immediate snap() release');
      snap(i);
      return false;
    }
    rbPos[i] -= delta * resistance;
    rbPos[i] = Math.max(-maxPull, Math.min(maxPull, rbPos[i]));
    _applyTransform();
    if (atStart && Math.abs(rbPos[i]) >= pullThresholdTop)    triggeredTop[i] = true;
    if (atEnd   && Math.abs(rbPos[i]) >= pullThresholdBottom) triggeredBottom[i] = true;
    notifyPull(i);
    // Wheel auto-snap: release the pull after wheel events stop. 400ms (not
    // 120ms) so slow mouse-wheel / trackpad ticks — which can arrive >120ms
    // apart — accumulate into a pull instead of snapping back between ticks.
    rbTimer[i] = setTimeout(function() { snap(i); }, 400);
    rbLog('pull() → rbPos=' + rbPos[i].toFixed(1) + ' trigTop=' + triggeredTop[i] + ' trigBottom=' + triggeredBottom[i] + ' | 400ms wheel auto-snap ARMED');
    return true;
  }

  if (wheelEnabled) el.addEventListener('wheel', function (e) {
    // A wheel gesture over a nested scrollable belongs to THAT scroller —
    // never steal it for the rubber band.
    if (_isInsideNestedScroller(el, e.target)) {
      rbLog('wheel SKIPPED — target inside nested scroller: ' + (e.target && (e.target.className || e.target.tagName)));
      return;
    }
    rbLog('wheel event deltaMode=' + e.deltaMode + ' deltaY=' + e.deltaY + ' target=' + (e.target && (e.target.className || e.target.tagName)));
    var handled = false;
    for (var i = 0; i < axes.length; i++) {
      var a = axes[i];
      var d = e[a.deltaWheel];
      // Normalise line / page deltaMode so mouse-wheel ticks produce
      // meaningful pull distances comparable to trackpad gestures.
      if (e.deltaMode === 1) d *= 20;
      else if (e.deltaMode === 2) d *= el.clientHeight;

      if (pull(i, d)) {
        handled = true;
      }
    }
    if (handled) e.preventDefault();
  }, { passive: false });

  el.addEventListener('touchstart', function (e) {
    var t = e.touches[0];
    if (!t) return;
    touchLastX = t.clientX;
    touchLastY = t.clientY;
    touchActive = true;
    // Remember whether this touch began inside a nested scrollable so the
    // touchmove handler leaves the gesture to it instead of rubber-banding.
    touchInsideNested = _isInsideNestedScroller(el, e.target);
    rbLog('touchstart target=' + (e.target && (e.target.className || e.target.tagName)) + ' touchInsideNested=' + touchInsideNested + (touchInsideNested ? ' ← gesture will be SKIPPED' : ''));
  }, { passive: true });

  el.addEventListener('touchmove', function (e) {
    if (!touchActive || touchInsideNested) {
      if (touchInsideNested) rbLog('touchmove SKIPPED — touchInsideNested=true');
      return;
    }
    var t = e.touches[0];
    if (!t) return;
    var dx = touchLastX - t.clientX;
    var dy = touchLastY - t.clientY;
    touchLastX = t.clientX;
    touchLastY = t.clientY;

    var handled = false;
    for (var i = 0; i < axes.length; i++) {
      var a = axes[i];
      var d = a.isX ? dx : dy;
      var maxS = a.scrollSize(el);
      var atStart = hasScrollRange(a) && a.scrollPos(el) <= 0 && d < 0;
      var atEnd   = hasScrollRange(a) && atBottomEdge(a, d);
      if (atStart || atEnd) {
        clearTimeout(rbTimer[i]);
        el.style.transition = 'none';
        rbPos[i] -= d * resistance;
        rbPos[i] = Math.max(-maxPull, Math.min(maxPull, rbPos[i]));
        _applyTransform();
        if (atStart && Math.abs(rbPos[i]) >= pullThresholdTop)    triggeredTop[i] = true;
        if (atEnd   && Math.abs(rbPos[i]) >= pullThresholdBottom) triggeredBottom[i] = true;
        notifyPull(i);
        // NOTE: no auto-snap timer on the TOUCH path — touchend/touchcancel
        // releases the gesture. A 120ms timer here would fire mid-gesture on
        // any pause (finger still down, just not moving) and snap the pull
        // back to 0, killing the refresh indicator. The wheel path keeps its
        // timer because wheel has no release event.
        rbLog('touchmove axis=' + (a.isX ? 'x' : 'y') + ' d=' + d.toFixed(1) + ' atStart=' + atStart + ' atEnd=' + atEnd + ' → rbPos=' + rbPos[i].toFixed(1) + ' trigBottom=' + triggeredBottom[i] + ' (no auto-snap on touch)');
        handled = true;
      } else {
        // Left the boundary — release unconditionally (same reasoning as the
        // wheel path: snap() is a no-op-safe release at any residual pull).
        rbLog('touchmove left boundary → snap() release');
        snap(i);
      }
    }
    if (handled) e.preventDefault();
  }, { passive: false });

  function touchEnd() {
    touchActive = false;
    rbLog('touchend/touchcancel → releasing all axes');
    // Release every axis — snap() is a no-op-safe release at any residual
    // pull size and always reports ratio 0, so a sub-threshold pull can't
    // leave an indicator (e.g. "Pull to refresh") visible after lift-off.
    for (var i = 0; i < axes.length; i++) snap(i);
  }
  el.addEventListener('touchend',   touchEnd, { passive: true });
  el.addEventListener('touchcancel', touchEnd, { passive: true });

  el.dataset.rbApplied = '1';
}

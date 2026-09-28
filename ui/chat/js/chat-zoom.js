'use strict';

// Chat pinch-to-zoom — two-finger pinch (touch) and Ctrl+wheel (trackpad
// pinch / desktop scroll) over the chat transcript scale ONLY the message
// content, never the whole app. Applied as CSS `zoom` on #chat-messages-inner
// (layout-affecting, so the scroller's scrollbar and the virtual-scroll
// placeholders keep matching real bubbles). The level persists in
// localStorage and restores on boot; a small floating chip shows the current
// level with − / + / reset controls while active.
// Module map: ui/chat/js/README.md. Wired in ui/shared/js/main.js.

import { app } from '../../shared/js/state.js';
import { scaleVirtualScrollHeights } from './chat-virtual-scroll.js';

const ZOOM_KEY = 'chatZoomLevel';
const MIN_ZOOM = 0.7;
const MAX_ZOOM = 2.5;
const CHIP_HIDE_MS = 2000;
// Gesture sensitivity: how much zoom per unit of gesture. Both are heavily
// damped — trackpad pinch events arrive as Ctrl+wheel with deltas around
// ±100px per notch, so a naive rate zooms ~65% per notch. 0.001 ≈ 9% per
// notch; PINCH_SENSITIVITY 0.12 means a 2x finger spread zooms to 1.12x
// (an eighth of native feel — deliberately slow so small pinches fine-tune).
const PINCH_SENSITIVITY = 0.12;
const WHEEL_SENSITIVITY = 0.001;

// CSS zoom is layout-affecting (scrollHeight + scrollbar reflow with the
// content), which is exactly what "zoom the chat window" needs. It is
// supported in Chromium/Safari and Firefox 126+. If it is missing we disable
// the feature rather than ship a half-working transform fallback (a transform
// would scale the pixels but not the scroll geometry).
const ZOOM_SUPPORTED = (() => {
  try {
    return 'zoom' in document.documentElement.style;
  } catch (_) {
    return false;
  }
})();

let _zoom = 1;         // current level (1 = 100%)
let _lastApplied = 1;  // level at which virtual-scroll heights were last rescaled
let _chipTimer = null;

function _clamp(v) {
  return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, v));
}

function _target() {
  return app.chatMessages || document.getElementById('chat-messages-inner');
}

function _scroller() {
  const t = _target();
  return app._chatScroller || (t && t.parentElement) || null;
}

export function setChatZoom(next) {
  next = _clamp(next);
  if (next === _zoom) return;
  const ratio = next / _lastApplied;
  _zoom = next;
  _lastApplied = next;
  try { localStorage.setItem(ZOOM_KEY, String(next)); } catch (_) { /* storage may be unavailable */ }
  const t = _target();
  if (t) {
    t.style.zoom = next === 1 ? '' : String(next);
    // Bubble heights are cached in zoomed px; rescale them so placeholders
    // keep matching real bubbles at the new level.
    if (ratio !== 1) scaleVirtualScrollHeights(ratio);
    // CSS zoom does not fire resize; nudge listeners that redraw on it
    // (task frames, scroll indicators, image decoders).
    window.dispatchEvent(new Event('resize'));
  }
  _showChip();
}

export function resetChatZoom() {
  setChatZoom(1);
}

// ── Floating level chip (− / % / + / reset) ───────────────────────────
function _positionChip(chip) {
  // Top centre of the chat: centred horizontally over the panel, sitting just
  // below the config-driven header (whose height varies per chat_ui.json).
  // Refreshed on every show so a header reconfig/reflow keeps it aligned.
  const header = document.getElementById('chat-header');
  const h = header ? header.getBoundingClientRect().height : 0;
  chip.style.top = Math.round(h + 8) + 'px';
}

function _ensureChip() {
  const panel = document.getElementById('chat-panel');
  if (!panel) return null;
  let chip = document.getElementById('chat-zoom-chip');
  if (chip) return chip;
  chip = document.createElement('div');
  chip.id = 'chat-zoom-chip';
  chip.className = 'chat-zoom-chip';
  chip.setAttribute('role', 'group');
  chip.setAttribute('aria-label', 'Chat zoom');
  chip.innerHTML =
    '<button type="button" class="cz-btn" data-cz="out" title="Zoom out" aria-label="Zoom out"><i data-lucide="minus"></i></button>' +
    '<span class="cz-level" aria-live="polite">100%</span>' +
    '<button type="button" class="cz-btn" data-cz="in" title="Zoom in" aria-label="Zoom in"><i data-lucide="plus"></i></button>' +
    '<button type="button" class="cz-btn" data-cz="reset" title="Reset to 100%" aria-label="Reset zoom"><i data-lucide="rotate-ccw"></i></button>';
  chip.addEventListener('click', (e) => {
    const btn = e.target.closest('button[data-cz]');
    if (!btn) return;
    if (btn.dataset.cz === 'in') setChatZoom(_zoom + 0.1);
    else if (btn.dataset.cz === 'out') setChatZoom(_zoom - 0.1);
    else resetChatZoom();
  });
  panel.appendChild(chip);
  _positionChip(chip);
  return chip;
}

function _showChip() {
  const chip = _ensureChip();
  if (!chip) return;
  _positionChip(chip); // header height can change between shows
  const level = chip.querySelector('.cz-level');
  if (level) level.textContent = Math.round(_zoom * 100) + '%';
  chip.hidden = false;
  chip.classList.remove('cz-hide');
  clearTimeout(_chipTimer);
  _chipTimer = setTimeout(() => chip.classList.add('cz-hide'), CHIP_HIDE_MS);
}

// ── Gestures ──────────────────────────────────────────────────────────
function _wireGestures() {
  const scroller = _scroller();
  if (!scroller || scroller.dataset.chatZoomWired) return;
  scroller.dataset.chatZoomWired = '1';

  // Trackpad pinch (and desktop Ctrl+scroll) arrive as ctrlKey wheel events.
  // Intercept them so the browser zooms the CHAT, not the whole page.
  scroller.addEventListener('wheel', (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    // Normalize deltaMode (0 = pixels, 1 = lines, 2 = pages) to pixels.
    const dy = e.deltaMode === 1 ? e.deltaY * 16 : e.deltaMode === 2 ? e.deltaY * 100 : e.deltaY;
    setChatZoom(_zoom * Math.exp(-dy * WHEEL_SENSITIVITY));
  }, { passive: false });

  // Touch pinch: two-finger distance ratio against the level at gesture start.
  let startDist = 0;
  let startZoom = 1;
  const dist = (t0, t1) => Math.hypot(t0.clientX - t1.clientX, t0.clientY - t1.clientY);

  scroller.addEventListener('touchstart', (e) => {
    if (!e.touches || e.touches.length < 2) return;
    // Stop the OS-level pinch zoom from kicking in on top of ours.
    if (e.cancelable) e.preventDefault();
    startDist = dist(e.touches[0], e.touches[1]);
    startZoom = _zoom;
  }, { passive: false });

  scroller.addEventListener('touchmove', (e) => {
    if (!e.touches || e.touches.length < 2 || startDist === 0) return;
    if (e.cancelable) e.preventDefault();
    const d = dist(e.touches[0], e.touches[1]);
    if (d === 0) return;
    // Scale zoom by the pinch ratio, damped by PINCH_SENSITIVITY so a 2x
    // finger spread only zooms 1.5x instead of the full 2x.
    const ratio = d / startDist;
    setChatZoom(startZoom * (1 + PINCH_SENSITIVITY * (ratio - 1)));
  }, { passive: false });

  const endPinch = () => { startDist = 0; };
  scroller.addEventListener('touchend', endPinch, { passive: true });
  scroller.addEventListener('touchcancel', endPinch, { passive: true });

  // Keep native single-finger panning for scrolling, but hand two-finger
  // pinches to us instead of the browser.
  scroller.style.touchAction = 'pan-x pan-y';
}

export function initChatZoom() {
  if (!ZOOM_SUPPORTED) return;
  _zoom = _load();
  _lastApplied = _zoom;
  const t = _target();
  if (t) {
    t.style.zoom = _zoom === 1 ? '' : String(_zoom);
    if (_zoom !== 1) window.dispatchEvent(new Event('resize'));
  }
  _wireGestures();
  // No chip at boot — it only appears once the user actually zooms.
}

function _load() {
  try {
    const saved = parseFloat(localStorage.getItem(ZOOM_KEY));
    return Number.isFinite(saved) ? _clamp(saved) : 1;
  } catch (_) {
    return 1;
  }
}

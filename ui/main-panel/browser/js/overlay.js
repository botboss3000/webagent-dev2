'use strict';

// ── Browser page: live interaction overlay (cursors, ripples, telegraphs) ──
// REMOVE-WHEN: the Browser page is dropped from the page catalog — this whole
// folder (ui/main-panel/browser/) is a drop-in page and dies with it.
//
// A fully self-contained, drop-in ES module that paints transient interaction
// visuals ON TOP of the Browser page's live pixel mirror (the
// <genui class="browser-mirror">). It draws: agent INTENT telegraph halos
// (where the agent is about to act), click ripples (agent or user), typing
// highlights on form fields, and a ghost agent cursor with a fading trail.
//
// Called by:
//   • ui/main-panel/browser/js/browser.js — owns the genui + mirror lifecycle;
//     calls initOverlay/setSpace/setGenui/setEnabled and clears on view change.
//   • ui/main-panel/browser/js/live-events.js — the agent action feed; turns
//     incoming events into showTelegraph/showClick/showType/setAgentCursor calls.
//
// SELF-CONTAINED: this module owns all of its own DOM (absolutely-positioned
// children appended into the supplied `layerEl`) and injects a ONE-TIME scoped
// <style> block (id-guarded). Every colour comes from design-system CSS vars so
// it is automatically correct in dark AND light mode. Everything is
// pointer-events:none so it never blocks the genui or the user.
//
// COORDINATE MATH: the page coordinate space (default 1280x720) is the genui's
// intrinsic size; the genui displays object-fit:contain (letterboxed) inside
// .browser-stage. This module computes the INVERSE of browser.js `_mapPt`:
// given a page-space point/box it returns the on-screen pixel position within
// the overlay layer. See `_project()` below.

// ── Module state (single overlay instance; the page only has one mirror) ─────
let _layer = null;        // the absolutely-positioned overlay layer (== stage box)
let _genui = null;       // the <genui class="browser-mirror"> we project onto
let _space = { width: 1280, height: 720 };  // page CSS-pixel coord space
let _enabled = true;      // when false everything is hidden/cleared
let _ro = null;           // ResizeObserver on the genui
let _onResize = null;     // window resize listener (kept for removal)

// Agent ghost cursor: a persistent element (unlike the transient ripples), so
// we hold a ref + its current page-space point + an idle-hide timer.
let _cursorEl = null;
let _cursorPt = null;     // last page-space point of the agent cursor
let _cursorIdleTimer = null;

// One-time injected style block.
const _STYLE_ID = 'browser-overlay-style';

// ── Scoped class / animation names (reported to the integrator + CSS owner) ──
// All prefixed `bovl-` so they never collide with page or design-system rules.
const _CLS = {
  layer:      'bovl-layer',       // applied to the supplied layerEl
  telegraph:  'bovl-telegraph',   // pulsing intent halo
  ripple:     'bovl-ripple',      // expanding click ring
  dot:        'bovl-dot',         // tiny labelled click dot
  dotLabel:   'bovl-dot-label',   // 'agent' / 'user' caption on the dot
  type:       'bovl-type',        // field highlight box (outline + faint fill)
  typeText:   'bovl-type-text',   // floating typed-text caption
  cursor:     'bovl-cursor',      // ghost agent cursor
  cursorGlyph:'bovl-cursor-glyph',// the pointer SVG inside the cursor
  agent:      'bovl-agent',       // actor modifier → var(--purple)
  user:       'bovl-user',        // actor modifier → var(--accent)
};

// ── One-time scoped stylesheet ───────────────────────────────────────────────
// All geometry is set inline per element (position/size from _project); this
// sheet owns only the LOOK + the GPU-friendly @keyframes (transform/opacity).
function _injectStyle() {
  if (document.getElementById(_STYLE_ID)) return;
  const css = `
.${_CLS.layer} {
  position: absolute;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  z-index: 4;               /* above the genui, below the footer (z-index:5) */
}
.${_CLS.layer} * { pointer-events: none; }

/* ── Agent intent telegraph — a pulsing rounded outline + soft glow ── */
.${_CLS.telegraph} {
  position: absolute;
  border-radius: 10px;
  border: 2px solid var(--purple);
  box-shadow: 0 0 0 3px rgba(var(--purple-rgb), 0.18),
              0 0 18px 2px rgba(var(--purple-rgb), 0.35);
  background: rgba(var(--purple-rgb), 0.06);
  opacity: 0;
  will-change: transform, opacity;
  animation: bovl-telegraph-pulse 520ms ease-out forwards;
}
@keyframes bovl-telegraph-pulse {
  0%   { opacity: 0;    transform: scale(0.82); }
  35%  { opacity: 1;    transform: scale(1.04); }
  70%  { opacity: 0.85; transform: scale(1.0);  }
  100% { opacity: 0;    transform: scale(1.06); }
}

/* ── Click ripple — expanding ring that fades ── */
.${_CLS.ripple} {
  position: absolute;
  border-radius: 50%;
  border: 2px solid var(--accent);
  opacity: 0;
  will-change: transform, opacity;
  animation: bovl-ripple-expand 600ms ease-out forwards;
}
.${_CLS.ripple}.${_CLS.agent} { border-color: var(--purple); }
.${_CLS.ripple}.${_CLS.user}  { border-color: var(--accent); }
@keyframes bovl-ripple-expand {
  0%   { opacity: 0.9; transform: scale(0.2); }
  100% { opacity: 0;   transform: scale(1.0); }
}

/* ── Tiny labelled click dot ── */
.${_CLS.dot} {
  position: absolute;
  width: 10px;
  height: 10px;
  margin: -5px 0 0 -5px;          /* centre on the projected point */
  border-radius: 50%;
  opacity: 0;
  will-change: transform, opacity;
  animation: bovl-dot-pop 600ms ease-out forwards;
}
.${_CLS.dot}.${_CLS.agent} { background: var(--purple); box-shadow: 0 0 8px 1px rgba(var(--purple-rgb), 0.5); }
.${_CLS.dot}.${_CLS.user}  { background: var(--accent); box-shadow: 0 0 8px 1px rgba(var(--brand-rgb), 0.5); }
@keyframes bovl-dot-pop {
  0%   { opacity: 1; transform: scale(0.5); }
  30%  { opacity: 1; transform: scale(1.0); }
  100% { opacity: 0; transform: scale(0.8); }
}
.${_CLS.dotLabel} {
  position: absolute;
  left: 9px;
  top: -3px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1;
  letter-spacing: 0.02em;
  white-space: nowrap;
  text-shadow: 0 1px 2px rgba(var(--shadow-rgb), 0.4);
}
.${_CLS.dot}.${_CLS.agent} .${_CLS.dotLabel} { color: var(--purple); }
.${_CLS.dot}.${_CLS.user}  .${_CLS.dotLabel} { color: var(--accent); }

/* ── Typing highlight — field outline + faint fill, with floating text ── */
.${_CLS.type} {
  position: absolute;
  border-radius: 6px;
  border: 2px solid var(--purple);
  background: rgba(var(--purple-rgb), 0.10);
  box-shadow: 0 0 0 2px rgba(var(--purple-rgb), 0.14);
  opacity: 0;
  will-change: opacity;
  animation: bovl-type-glow 1200ms ease-in-out forwards;
}
.${_CLS.type}.${_CLS.user} {
  border-color: var(--accent);
  background: rgba(var(--brand-rgb), 0.10);
  box-shadow: 0 0 0 2px rgba(var(--brand-rgb), 0.14);
}
@keyframes bovl-type-glow {
  0%   { opacity: 0; }
  12%  { opacity: 1; }
  82%  { opacity: 1; }
  100% { opacity: 0; }
}
.${_CLS.typeText} {
  position: absolute;
  bottom: calc(100% + 4px);
  left: 0;
  max-width: 280px;
  padding: 2px 7px;
  border-radius: 6px;
  background: var(--bg-1);
  border: var(--border-width) solid var(--border);
  color: var(--fg-1);
  font-size: 11px;
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  box-shadow: 0 2px 10px rgba(var(--shadow-rgb), 0.25);
}
.${_CLS.type}.${_CLS.agent} .${_CLS.typeText} { color: var(--purple); }

/* ── Ghost agent cursor — pointer glyph + subtle trail ── */
.${_CLS.cursor} {
  position: absolute;
  width: 22px;
  height: 22px;
  margin: -2px 0 0 -2px;          /* hotspot ~ glyph tip (top-left) */
  opacity: 0;
  transform: translateZ(0);
  transition: left 120ms linear, top 120ms linear, opacity 180ms ease;
  filter: drop-shadow(0 0 6px rgba(var(--purple-rgb), 0.55));
  will-change: left, top, opacity;
}
.${_CLS.cursor}.bovl-on { opacity: 1; }
.${_CLS.cursorGlyph} {
  width: 22px;
  height: 22px;
  display: block;
  color: var(--purple);
}
/* The trailing echo of the cursor — a faint, slightly-larger ghost behind it. */
.${_CLS.cursor}::after {
  content: "";
  position: absolute;
  left: 2px;
  top: 2px;
  width: 14px;
  height: 14px;
  border-radius: 50%;
  background: radial-gradient(circle, rgba(var(--purple-rgb), 0.30), transparent 70%);
  opacity: 0.7;
  animation: bovl-cursor-trail 900ms ease-out infinite;
}
@keyframes bovl-cursor-trail {
  0%   { opacity: 0.5; transform: scale(0.6); }
  100% { opacity: 0;   transform: scale(1.8); }
}
`;
  const style = document.createElement('style');
  style.id = _STYLE_ID;
  style.textContent = css;
  document.head.appendChild(style);
}

// ── Coordinate projection: page-space → overlay-screen-space ─────────────────
// The INVERSE of browser.js `_mapPt`. Returns null when refs aren't ready.
//   pt  : { x, y } in page CSS pixels
//   box : { x, y, width, height } in page CSS pixels
// Both recomputed on every render so resize / letterbox shifts stay accurate.
function _metrics() {
  if (!_layer || !_genui) return null;
  const rect = _genui.getBoundingClientRect();
  const layerRect = _layer.getBoundingClientRect();
  const cw = _space.width || _genui.width || 1280;
  const ch = _space.height || _genui.height || 720;
  const scale = Math.min(rect.width / cw, rect.height / ch) || 1;
  const dispW = cw * scale, dispH = ch * scale;
  const offX = (rect.left - layerRect.left) + (rect.width - dispW) / 2;
  const offY = (rect.top - layerRect.top) + (rect.height - dispH) / 2;
  return { scale, offX, offY };
}

function _projectPoint(pt) {
  const m = _metrics();
  if (!m || !pt) return null;
  return { x: m.offX + pt.x * m.scale, y: m.offY + pt.y * m.scale };
}

function _projectBox(box) {
  const m = _metrics();
  if (!m || !box) return null;
  return {
    x: m.offX + box.x * m.scale,
    y: m.offY + box.y * m.scale,
    width: (box.width || 0) * m.scale,
    height: (box.height || 0) * m.scale,
  };
}

// Centre point (page-space) of a {x,y,width,height} box.
function _boxCenter(box) {
  if (!box) return null;
  return { x: box.x + (box.width || 0) / 2, y: box.y + (box.height || 0) / 2 };
}

// Resolve a click `target` ({box} or {point}) to a page-space point.
function _targetPoint(target) {
  if (!target) return null;
  if (target.point) return target.point;
  if (target.box) return _boxCenter(target.box);
  // Allow a bare {x,y} or {x,y,width,height} too, for caller convenience.
  if (typeof target.x === 'number' && typeof target.y === 'number') {
    return ('width' in target) ? _boxCenter(target) : { x: target.x, y: target.y };
  }
  return null;
}

// ── Element lifecycle helper: append, then auto-remove after its animation ───
function _spawnTransient(el, ttlMs) {
  if (!_layer) return;
  _layer.appendChild(el);
  let removed = false;
  const kill = () => {
    if (removed) return;
    removed = true;
    if (el.parentNode) el.parentNode.removeChild(el);
  };
  el.addEventListener('animationend', kill, { once: true });
  // Fallback in case animationend never fires (display:none, reduced-motion…).
  setTimeout(kill, (ttlMs || 700) + 120);
}

// ── Public API ───────────────────────────────────────────────────────────────

// Store refs, create the layer, attach observers. Safe to call repeatedly.
export function initOverlay(layerEl, genuiEl) {
  if (!layerEl) return;
  _injectStyle();
  _layer = layerEl;
  _layer.classList.add(_CLS.layer);
  if (genuiEl) _genui = genuiEl;

  // Observe genui display-box changes so projection metrics stay current.
  if (_ro) { try { _ro.disconnect(); } catch (_) {} _ro = null; }
  if (_genui && typeof ResizeObserver !== 'undefined') {
    _ro = new ResizeObserver(() => { /* metrics recomputed lazily per render */ });
    try { _ro.observe(_genui); } catch (_) {}
  }
  if (!_onResize) {
    _onResize = () => { /* projection is recomputed on next render; nothing to cache */ };
    window.addEventListener('resize', _onResize);
  }
}

// Update the page coordinate space ({width,height}); default 1280x720.
export function setSpace(space) {
  if (space && space.width && space.height) {
    _space = { width: space.width, height: space.height };
  }
}

// Swap the genui ref (e.g. if the mirror element is recreated).
function setGenui(genuiEl) {
  _genui = genuiEl || null;
  if (_ro) { try { _ro.disconnect(); } catch (_) {} _ro = null; }
  if (_genui && typeof ResizeObserver !== 'undefined') {
    _ro = new ResizeObserver(() => {});
    try { _ro.observe(_genui); } catch (_) {}
  }
}

// Enable/disable the whole overlay. When off, hide + clear everything.
export function setEnabled(on) {
  _enabled = !!on;
  if (!_layer) return;
  _layer.style.display = _enabled ? '' : 'none';
  if (!_enabled) clearOverlay();
}

// Agent INTENT: a pulsing halo around `box` (~500ms). No-op if box is null.
export function showTelegraph(box) {
  if (!_enabled || !_layer || !box) return;
  const b = _projectBox(box);
  if (!b) return;
  const el = document.createElement('div');
  el.className = _CLS.telegraph;
  // Pad the halo slightly outside the field so it reads as "around" it.
  const pad = 4;
  el.style.left = (b.x - pad) + 'px';
  el.style.top = (b.y - pad) + 'px';
  el.style.width = (b.width + pad * 2) + 'px';
  el.style.height = (b.height + pad * 2) + 'px';
  _spawnTransient(el, 520);
}

// Click ripple + tiny labelled dot. target = {box}|{point}; actor agent|user.
export function showClick(target, actor) {
  if (!_enabled || !_layer) return;
  const pt = _targetPoint(target);
  const sp = _projectPoint(pt);
  if (!sp) return;
  const actorCls = (actor === 'user') ? _CLS.user : _CLS.agent;
  const label = (actor === 'user') ? 'user' : 'agent';

  // Ripple ring — sized to a fixed on-screen radius, centred on the point.
  const R = 46;  // final diameter in screen px
  const ring = document.createElement('div');
  ring.className = `${_CLS.ripple} ${actorCls}`;
  ring.style.left = (sp.x - R / 2) + 'px';
  ring.style.top = (sp.y - R / 2) + 'px';
  ring.style.width = R + 'px';
  ring.style.height = R + 'px';
  _spawnTransient(ring, 600);

  // Tiny labelled dot at the exact point.
  const dot = document.createElement('div');
  dot.className = `${_CLS.dot} ${actorCls}`;
  dot.style.left = sp.x + 'px';
  dot.style.top = sp.y + 'px';
  const cap = document.createElement('span');
  cap.className = _CLS.dotLabel;
  cap.textContent = label;
  dot.appendChild(cap);
  _spawnTransient(dot, 600);
}

// Highlight a form field (~1.2s) and float the typed text above it.
export function showType(box, text, actor) {
  if (!_enabled || !_layer || !box) return;
  const b = _projectBox(box);
  if (!b) return;
  const actorCls = (actor === 'user') ? _CLS.user : _CLS.agent;
  const el = document.createElement('div');
  el.className = `${_CLS.type} ${actorCls}`;
  el.style.left = b.x + 'px';
  el.style.top = b.y + 'px';
  el.style.width = b.width + 'px';
  el.style.height = b.height + 'px';

  if (text != null && String(text).length) {
    let t = String(text);
    if (t.length > 30) t = t.slice(0, 30) + '…';
    const cap = document.createElement('span');
    cap.className = _CLS.typeText;
    cap.textContent = t;
    el.appendChild(cap);
  }
  _spawnTransient(el, 1200);
}

// Show/move the ghost agent cursor at page-space `point`. null → hide (idle).
export function setAgentCursor(point) {
  if (!_enabled || !_layer) return;

  // null → schedule an idle hide (fade out, keep the element for reuse).
  if (!point) {
    if (_cursorEl) _cursorEl.classList.remove('bovl-on');
    _cursorPt = null;
    return;
  }

  if (!_cursorEl) {
    _cursorEl = document.createElement('div');
    _cursorEl.className = _CLS.cursor;
    // Inline pointer glyph (no external icon dependency → self-contained).
    _cursorEl.innerHTML =
      `<svg class="${_CLS.cursorGlyph}" viewBox="0 0 24 24" fill="currentColor" ` +
      `aria-hidden="true"><path d="M5 3l14 7-6 1.6L9.6 18 5 3z"/></svg>`;
    _layer.appendChild(_cursorEl);
  }

  _cursorPt = point;
  const sp = _projectPoint(point);
  if (!sp) return;
  _cursorEl.style.left = sp.x + 'px';
  _cursorEl.style.top = sp.y + 'px';
  _cursorEl.classList.add('bovl-on');

  // Auto-fade after an idle period if no further movement arrives.
  if (_cursorIdleTimer) clearTimeout(_cursorIdleTimer);
  _cursorIdleTimer = setTimeout(() => {
    if (_cursorEl) _cursorEl.classList.remove('bovl-on');
    _cursorPt = null;
  }, 2500);
}

// Remove all transient visuals (ripples, telegraphs, type highlights) + cursor.
export function clearOverlay() {
  if (_cursorIdleTimer) { clearTimeout(_cursorIdleTimer); _cursorIdleTimer = null; }
  if (!_layer) { _cursorEl = null; _cursorPt = null; return; }
  // Remove every overlay-owned child.
  const sel = `.${_CLS.telegraph}, .${_CLS.ripple}, .${_CLS.dot}, .${_CLS.type}, .${_CLS.cursor}`;
  _layer.querySelectorAll(sel).forEach((n) => { if (n.parentNode) n.parentNode.removeChild(n); });
  _cursorEl = null;
  _cursorPt = null;
}

// Disconnect observers + remove all DOM. Leaves the one-time <style> in place
// (cheap, id-guarded) so a later re-init is instant.
function teardownOverlay() {
  clearOverlay();
  if (_ro) { try { _ro.disconnect(); } catch (_) {} _ro = null; }
  if (_onResize) { window.removeEventListener('resize', _onResize); _onResize = null; }
  if (_layer) _layer.classList.remove(_CLS.layer);
  _layer = null;
  _genui = null;
}

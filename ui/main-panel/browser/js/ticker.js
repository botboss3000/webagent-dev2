'use strict';

// ── Browser action ticker: live caption strip + "who's driving" badge ───────
// REMOVE-WHEN: the Browser page is dropped from the page catalog — this is a
// drop-in module for the Browser page (ui/main-panel/browser/) and dies with it.
//
// A self-contained, drop-in ES module that floats two informational overlays on
// top of the live mirror inside `.browser-stage`:
//   • a caption stack (top-left) that narrates each agent/user action in plain
//     words — e.g. "Clicking “Log in”" — each line animates in, lives ~3.5s,
//     then fades out and removes itself;
//   • a driver badge (top-right) showing whether the AGENT or the USER is
//     currently acting ("Agent is acting" / "You're in control").
//
// Called by ui/main-panel/browser/js/browser.js (init/teardown on page
// mount/unmount) and ui/main-panel/browser/js/live-events.js (pushes captions +
// flips the driver as agent/user actions stream in).
//
// THEMING: design-system CSS variables ONLY (no raw hex/rgb) so it reads in dark
// + light. Agent accent = var(--purple) / rgba(var(--purple-rgb), a); user
// accent = var(--accent) / rgba(var(--brand-rgb), a). Surfaces var(--bg-1),
// text var(--fg-1/2/3), borders var(--border-width) solid var(--border).
//
// The overlay container is pointer-events:none so it never steals input from the
// live mirror genui underneath; it is purely informational (v1 needs no
// interactive child).

// ── Module state ───────────────────────────────────────────────────────────
let _host = null;        // the .browser-stage element we mount into
let _root = null;        // overlay container appended into _host
let _stack = null;       // caption stack element (lines prepend to top)
let _badge = null;       // driver badge element
let _badgeText = null;   // text node holder inside the badge
let _lines = [];         // active caption entries { el, timer }

const _STYLE_ID = 'browser-ticker-style';
const _MAX_LINES = 5;          // most captions kept on screen at once
const _LINE_TTL = 3500;        // ms a caption stays before it fades out
const _EXIT_MS = 420;          // must match --tk-exit animation duration

// ── One-time scoped styles ──────────────────────────────────────────────────
// Class names (for the integrator):
//   .browser-ticker            overlay container (pointer-events:none)
//   .browser-ticker-stack      caption stack (top-left)
//   .browser-ticker-line       one caption row
//   .browser-ticker-line.is-agent / .is-user   actor tint
//   .browser-ticker-line.is-exiting            exit animation state
//   .browser-ticker-dot        leading coloured glyph on a caption
//   .browser-ticker-actor      subtle actor label on a caption
//   .browser-ticker-label      the action text
//   .browser-ticker-badge      driver badge (top-right)
//   .browser-ticker-badge.is-agent / .is-user  driver tint
//   .browser-ticker-badge.is-show              visible state
//   .browser-ticker-badge-dot  pulsing indicator inside the badge
//   .browser-ticker-badge-text the "Agent is acting" / "You're in control" text
// Animation names:
//   tkLineIn   caption enter (slide + fade in)
//   tkLineOut  caption exit (fade out + collapse)
//   tkPulse    gentle pulse for the agent driver indicator
function _injectStyle() {
  if (document.getElementById(_STYLE_ID)) return;
  const style = document.createElement('style');
  style.id = _STYLE_ID;
  style.textContent = `
.browser-ticker {
  position: absolute;
  inset: 0;
  z-index: 6;
  pointer-events: none;
  overflow: hidden;
}
.browser-ticker-stack {
  position: absolute;
  top: 12px;
  left: 12px;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-width: min(60%, 420px);
}
.browser-ticker-line {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 11px 6px 9px;
  border-radius: 11px;
  background: color-mix(in srgb, var(--bg-1) 86%, transparent);
  border: var(--border-width) solid var(--border);
  box-shadow: 0 4px 16px rgba(var(--shadow-rgb), 0.22);
  -webkit-backdrop-filter: blur(7px);
  backdrop-filter: blur(7px);
  font-size: 12.5px;
  line-height: 1.35;
  color: var(--fg-1);
  transform-origin: left center;
  animation: tkLineIn 0.34s cubic-bezier(0.22, 1, 0.36, 1) both;
  overflow: hidden;
}
.browser-ticker-line.is-exiting {
  animation: tkLineOut ${_EXIT_MS}ms ease forwards;
}
.browser-ticker-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg-3);
  box-shadow: 0 0 0 3px transparent;
}
.browser-ticker-line.is-agent .browser-ticker-dot {
  background: var(--purple);
  box-shadow: 0 0 0 3px rgba(var(--purple-rgb), 0.22);
}
.browser-ticker-line.is-user .browser-ticker-dot {
  background: var(--accent);
  box-shadow: 0 0 0 3px rgba(var(--brand-rgb), 0.22);
}
.browser-ticker-actor {
  flex: 0 0 auto;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  color: var(--fg-3);
}
.browser-ticker-line.is-agent .browser-ticker-actor { color: var(--purple); }
.browser-ticker-line.is-user .browser-ticker-actor { color: var(--accent); }
.browser-ticker-label {
  min-width: 0;
  color: var(--fg-1);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.browser-ticker-badge {
  position: absolute;
  top: 12px;
  right: 12px;
  display: inline-flex;
  align-items: center;
  gap: 7px;
  padding: 5px 11px 5px 9px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--bg-1) 88%, transparent);
  border: var(--border-width) solid var(--border);
  box-shadow: 0 4px 16px rgba(var(--shadow-rgb), 0.22);
  -webkit-backdrop-filter: blur(7px);
  backdrop-filter: blur(7px);
  font-size: 12px;
  font-weight: 600;
  color: var(--fg-2);
  opacity: 0;
  transform: translateY(-4px);
  transition: opacity 0.24s ease, transform 0.24s ease, border-color 0.24s ease;
  pointer-events: none;
}
.browser-ticker-badge.is-show {
  opacity: 1;
  transform: translateY(0);
}
.browser-ticker-badge-dot {
  flex: 0 0 auto;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--fg-3);
}
.browser-ticker-badge.is-agent {
  border-color: color-mix(in srgb, var(--purple) 45%, var(--border));
  color: var(--fg-1);
}
.browser-ticker-badge.is-agent .browser-ticker-badge-dot {
  background: var(--purple);
  animation: tkPulse 1.5s ease-in-out infinite;
}
.browser-ticker-badge.is-user {
  border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
  color: var(--fg-1);
}
.browser-ticker-badge.is-user .browser-ticker-badge-dot {
  background: var(--accent);
}
@keyframes tkLineIn {
  from { opacity: 0; transform: translateX(-14px) scale(0.98); }
  to   { opacity: 1; transform: translateX(0) scale(1); }
}
@keyframes tkLineOut {
  from { opacity: 1; transform: translateX(0); max-height: 60px; margin-bottom: 0; }
  to   { opacity: 0; transform: translateX(-10px); max-height: 0; margin-bottom: -6px; padding-top: 0; padding-bottom: 0; }
}
@keyframes tkPulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(var(--purple-rgb), 0.45); }
  50%      { box-shadow: 0 0 0 5px rgba(var(--purple-rgb), 0); }
}
@media (prefers-reduced-motion: reduce) {
  .browser-ticker-line,
  .browser-ticker-badge,
  .browser-ticker-badge.is-agent .browser-ticker-badge-dot {
    animation-duration: 0.001s !important;
    transition-duration: 0.001s !important;
  }
}
`;
  document.head.appendChild(style);
}

// ── Public: initialise the overlay ──────────────────────────────────────────
// Mounts a caption stack + driver badge into the stage. Idempotent: a second
// call with the same host is a no-op; with a new host it re-homes the overlay.
export function initTicker(hostEl) {
  if (!hostEl) return;
  if (_root && _host === hostEl) return;   // already initialised here
  if (_root) teardownTicker();             // re-homing to a new stage

  _injectStyle();
  _host = hostEl;

  _root = document.createElement('div');
  _root.className = 'browser-ticker';

  _stack = document.createElement('div');
  _stack.className = 'browser-ticker-stack';

  _badge = document.createElement('div');
  _badge.className = 'browser-ticker-badge';
  const badgeDot = document.createElement('span');
  badgeDot.className = 'browser-ticker-badge-dot';
  _badgeText = document.createElement('span');
  _badgeText.className = 'browser-ticker-badge-text';
  _badge.appendChild(badgeDot);
  _badge.appendChild(_badgeText);

  _root.appendChild(_stack);
  _root.appendChild(_badge);
  _host.appendChild(_root);
}

// ── Public: narrate one action ──────────────────────────────────────────────
// Adds a caption to the TOP of the stack. `actor` is 'agent' | 'user'
// (anything else defaults to 'agent'). The label is inserted as textContent —
// never innerHTML — so page-derived text can't inject markup.
export function pushTicker(label, actor) {
  if (!_stack) return;
  const text = label == null ? '' : String(label);
  if (!text) return;
  const who = actor === 'user' ? 'user' : 'agent';

  const line = document.createElement('div');
  line.className = 'browser-ticker-line ' + (who === 'user' ? 'is-user' : 'is-agent');

  const dot = document.createElement('span');
  dot.className = 'browser-ticker-dot';

  const actorLabel = document.createElement('span');
  actorLabel.className = 'browser-ticker-actor';
  actorLabel.textContent = who === 'user' ? 'You' : 'Agent';

  const labelEl = document.createElement('span');
  labelEl.className = 'browser-ticker-label';
  labelEl.textContent = text;   // escape-safe: textContent, not innerHTML

  line.appendChild(dot);
  line.appendChild(actorLabel);
  line.appendChild(labelEl);

  // Newest on top.
  _stack.insertBefore(line, _stack.firstChild);

  const entry = { el: line, timer: null };
  _lines.push(entry);
  entry.timer = setTimeout(() => _retireLine(entry), _LINE_TTL);

  // Trim overflow: drop the oldest (lowest in the stack) immediately.
  while (_lines.length > _MAX_LINES) {
    const oldest = _lines.shift();
    if (oldest) _removeLineNow(oldest);
  }
}

// ── Public: who's driving ────────────────────────────────────────────────────
// 'agent' → purple "Agent is acting" with a pulsing dot.
// 'user'  → accent "You're in control".
// null / anything else → fade the badge out (idle).
export function setDriver(actor) {
  if (!_badge || !_badgeText) return;

  if (actor === 'agent') {
    _badge.classList.remove('is-user');
    _badge.classList.add('is-agent', 'is-show');
    _badgeText.textContent = 'Agent is acting';
  } else if (actor === 'user') {
    _badge.classList.remove('is-agent');
    _badge.classList.add('is-user', 'is-show');
    _badgeText.textContent = "You're in control";
  } else {
    // idle / hidden
    _badge.classList.remove('is-show');
  }
}

// ── Public: clear captions + reset badge ─────────────────────────────────────
export function clearTicker() {
  for (const entry of _lines) {
    if (entry.timer) clearTimeout(entry.timer);
    if (entry.el && entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
  }
  _lines = [];
  if (_badge) _badge.classList.remove('is-show', 'is-agent', 'is-user');
  if (_badgeText) _badgeText.textContent = '';
}

// ── Public: remove everything this module created ───────────────────────────
function teardownTicker() {
  clearTicker();
  if (_root && _root.parentNode) _root.parentNode.removeChild(_root);
  _root = null;
  _stack = null;
  _badge = null;
  _badgeText = null;
  _host = null;
  // The scoped <style> is intentionally left in <head>: cheap, id-guarded, and
  // reused on the next initTicker without a re-inject.
}

// ── Internals ────────────────────────────────────────────────────────────────
// Begin the exit animation, then remove on animationend (setTimeout fallback).
function _retireLine(entry) {
  if (!entry || !entry.el) return;
  const idx = _lines.indexOf(entry);
  if (idx !== -1) _lines.splice(idx, 1);
  const el = entry.el;
  if (!el.parentNode) return;
  el.classList.add('is-exiting');
  let done = false;
  const finish = () => {
    if (done) return;
    done = true;
    if (el.parentNode) el.parentNode.removeChild(el);
  };
  el.addEventListener('animationend', finish, { once: true });
  setTimeout(finish, _EXIT_MS + 120);   // fallback so nothing leaks
}

// Remove a line immediately (no exit animation) — used when over the cap.
function _removeLineNow(entry) {
  if (!entry) return;
  if (entry.timer) clearTimeout(entry.timer);
  if (entry.el && entry.el.parentNode) entry.el.parentNode.removeChild(entry.el);
}

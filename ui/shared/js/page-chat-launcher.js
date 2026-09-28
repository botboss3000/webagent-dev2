'use strict';

/**
 * Shared route-scoped "Ask or Search" launcher.
 *
 * One pill, mounted as a non-flow sibling of a route's page box, that expands in
 * place for two related flows:
 * - typing forwards the live text to that route's OWN search control (its
 *   existing filter / controller function — never a second copy of the filter);
 * - Send opens the standard WebAgent widget with the route's page-context
 *   wrapper and handoff tag.
 *
 * An expanded pill collapses again on Escape and on any press outside the pill,
 * so an open composer never strands itself over the page underneath it.
 *
 * The launcher itself is route-agnostic: everything surface-specific (where it
 * mounts, which search control it drives, how it describes the page, which
 * handoff template tags the message) comes from a ROUTE SPEC. Current bindings:
 *
 *   ui/shared/js/agents-page-chat-widget.js   → Agents route   (#tab-agents)
 *   ui/shared/js/settings-page-chat-widget.js → Settings route (#main-panel)
 *
 * A route with mutually-exclusive sub-views (e.g. Agents, which also embeds the
 * Sessions table) lists EVERY visible-at-most-one search control in its
 * `searchSelectors`; the launcher forwards to whichever one the sub-view has on
 * screen. A sub-view whose engine input is staged off-screen — the Sessions
 * table keeps its field in a hidden toolbar — names it in
 * `hiddenSearchSelector`, consulted only after the visible fields and the
 * controller. Typing in the pill therefore still filters sessions exactly as
 * that input does (title + content, with per-session hit counts).
 *
 * A sub-view can also change what the pill IS, not merely what it filters. A
 * route names `searchOnly()` when one of its sub-views has no chat at all: while
 * it is on screen the pill advertises a search-only label and the trailing control
 * becomes a red clear button — the composer never opens the task widget, and the
 * filter already runs as the user types, so the click goes to the field instead:
 * it empties the field, or collapses the pill when there is nothing to empty.
 * Every other sub-view keeps the shared ask-and-search behaviour. The launcher
 * re-reads the mode whenever the host's structure flips, so switching sub-views
 * swaps the pill back and forth without a remount.
 *
 * The launcher stays a SIBLING of the route's scroller, never a child, so the
 * scroll viewport ends above the pill. Its stored position is anchored to the
 * nearest horizontal edge after a drag, so a resize never strands it mid-page.
 *
 * PAGE-CHAT-LAUNCHER-SYNC: this module is the single implementation shared by
 * both routes; add new page bindings here rather than copying the pill markup,
 * drag handling or the search-forwarding logic into a route module.
 */

import { app } from './state.js';
import { apiPath } from './config.js';
import { icon } from './icons.js';
import { getAgentChatUi } from './app-prompts.js';
import { createChatWidget } from '../../chat-widget/js/chat-widget.js';

const EDGE_GUTTER = 16;
/** Live task widget per mounted launcher, keyed by root id (one per route). */
const _widgets = new Map();
/** Handoff-template promise per route, so a route fetches app-prompts once. */
const _templates = new Map();

/* PAGE-CHAT-LAUNCHER-SPRING — the expand/collapse motion of the pill.
   The width is animated in JS rather than by a CSS transition: collapsed the
   root is shrink-to-fit (an `auto` width wrapping the button) and expanded it is
   `min(400px, calc(100vw - 32px))`, and CSS still cannot interpolate between
   `auto` and a length portably. Measuring both ends in pixels and driving them
   with the Web Animations API works on every engine, and lets the animated value
   overshoot the resting width — which is what reads as spring. */
const PILL_SPRING_MS = 250;
/** Expanding: peaks ~8% past the target, then settles onto it. */
const PILL_SPRING_EXPAND = 'linear(0, 0.42 9.1%, 0.84 19.8%, 1.055 30.6%, 1.086 38%, 1.077 46%, 1.045 57%, 1.017 69%, 1.004 82%, 1)';
/** Collapsing: a gentler peak (~4%) — the pill is closing onto its own label. */
const PILL_SPRING_COLLAPSE = 'linear(0, 0.5 11%, 0.92 24%, 1.031 36%, 1.041 45%, 1.02 61%, 1.003 79%, 1)';
/** Engines without `linear()` still get one clean overshoot. */
const PILL_SPRING_FALLBACK = 'cubic-bezier(0.34, 1.56, 0.64, 1)';

function _config(spec) {
  const cfg = getAgentChatUi()?.[spec.configKey];
  return cfg && typeof cfg === 'object' ? cfg : {};
}

function _storageKey(spec) {
  return `webagent.${spec.storageNamespace}.v1:${app.currentUserId || 'anonymous'}`;
}

function _readPosition(spec) {
  try {
    const stored = JSON.parse(localStorage.getItem(_storageKey(spec)) || 'null');
    if (stored && (stored.side === 'left' || stored.side === 'right')
      && Number.isFinite(stored.inline) && Number.isFinite(stored.bottom)) return stored;
  } catch (_) { /* invalid or unavailable storage → configured default */ }
  const cfg = _config(spec);
  return {
    side: cfg.default_side === 'left' ? 'left' : 'right',
    inline: Math.max(0, Number(cfg.edge_offset) || EDGE_GUTTER),
    bottom: Math.max(0, Number(cfg.bottom_offset) || EDGE_GUTTER),
  };
}

function _writePosition(spec, position) {
  try { localStorage.setItem(_storageKey(spec), JSON.stringify(position)); } catch (_) { /* non-fatal */ }
}

function _applyPosition(root, position) {
  root.dataset.edge = position.side;
  root.style.top = 'auto';
  root.style.left = position.side === 'left' ? `${position.inline}px` : 'auto';
  root.style.right = position.side === 'right' ? `${position.inline}px` : 'auto';
  root.style.bottom = `${position.bottom}px`;
}

async function _handoffTemplate(spec) {
  if (!_templates.has(spec.rootId)) {
    const fallback = spec.handoffFallback || '{text}';
    _templates.set(spec.rootId, fetch(apiPath('/api/v1/app-prompts'))
      .then((response) => response.ok ? response.json() : null)
      .then((data) => data?.ui_handoffs?.[spec.handoffKey]?.template || fallback)
      .catch(() => fallback));
  }
  return _templates.get(spec.rootId);
}

async function _wrapMessage(spec, text) {
  const template = await _handoffTemplate(spec);
  const contextualText = `${spec.pageContext()}\n\nUser message: ${text}`;
  return template.replace(/\{text\}/g, contextualText);
}

function _openWidget(spec, initialMessage = '') {
  const text = String(initialMessage || '').trim();
  const existing = _widgets.get(spec.rootId);
  if (existing?.el) {
    existing.restore();
    if (text) existing.send(text);
    return;
  }
  const cfg = _config(spec);
  const widget = createChatWidget({
    title: cfg.title || 'WebAgent',
    iconName: cfg.icon || 'bot',
    ensureAgent: app.startWebagentSession,
    transformMessage: (value) => _wrapMessage(spec, value),
    initialMessage: text,
    sessionTargetName: spec.sessionTargetName,
    onClose: () => { _widgets.delete(spec.rootId); },
  });
  _widgets.set(spec.rootId, widget);
  widget.open();
}

/**
 * Return the page-owned search control for this route — never the launcher's own
 * input. Prefers a real inline field (`spec.searchSelectors`) and otherwise falls
 * back to the route's programmatic filter exposed on the controller element.
 */
function _resolveSearchTarget(spec, root) {
  const host = document.getElementById(spec.hostId);
  if (!host) return null;
  const own = root.querySelector('.page-chat-launcher-input');
  const selectors = spec.searchSelectors || [];
  if (selectors.length) {
    const input = Array.from(host.querySelectorAll(selectors.join(',')))
      .find((candidate) => candidate !== own
        && candidate.getClientRects().length > 0 && !candidate.disabled);
    if (input) return { input };
  }
  // Routes whose tables render without their old inline composer expose the same
  // native filter as a function on the body element; the launcher calls that
  // rather than reimplementing the filtering rules.
  if (spec.controllerSelector && spec.controllerProp) {
    const hosts = Array.from(host.querySelectorAll(spec.controllerSelector))
      .filter((candidate) => typeof candidate[spec.controllerProp] === 'function');
    const controllerHost = hosts.find((candidate) => candidate.getClientRects().length > 0) || hosts[0];
    if (controllerHost) return { runFilter: controllerHost[spec.controllerProp] };
  }
  // Last resort: a control the route deliberately stages off-screen — its real
  // engine input, kept in a hidden toolbar so a sub-view can drop its own
  // visible copy. Tried only after every visible field AND the controller, so
  // mutually-exclusive sub-views keep resolving to the control on screen rather
  // than to a permanent hidden one.
  if (spec.hiddenSearchSelector) {
    const hidden = Array.from(host.querySelectorAll(spec.hiddenSearchSelector))
      .find((candidate) => candidate !== own && !candidate.disabled);
    if (hidden) return { input: hidden };
  }
  return null;
}

/** Delegate filtering to the existing current-page input / controller. */
function _syncSearch(spec, root, value) {
  const target = _resolveSearchTarget(spec, root);
  if (!target) {
    root.dataset.searchTarget = 'unavailable';
    return;
  }
  if (target.input) {
    root.dataset.searchTarget = 'linked';
    if (target.input.value === value) return;
    target.input.value = value;
    target.input.dispatchEvent(new Event('input', { bubbles: true }));
    return;
  }
  root.dataset.searchTarget = 'controller';
  target.runFilter(value);
}

/**
 * Apply the route's current widget MODE to an already-mounted pill.
 *
 * A route whose sub-views differ in what the pill IS exposes `spec.searchOnly()`.
 * When it returns true the pill becomes search-only: a search label and icon, no
 * chat (the composer never opens the task widget) and a red clear/collapse button
 * in the send slot — the filter already runs as the user types, so the press goes
 * to the field: clear it, or collapse the pill once it is empty. Every other
 * sub-view keeps the shared ask-and-search behaviour.
 * Idempotent: it writes only when the mode actually changes, so it is safe to call
 * on any DOM mutation.
 */
function _applyMode(spec, root) {
  const searchOnly = typeof spec.searchOnly === 'function' && !!spec.searchOnly();
  const mode = searchOnly ? 'search' : 'ask';
  if (root.dataset.mode === mode) return;
  root.dataset.mode = mode;
  const cfg = _config(spec);
  const fb = spec.fallbacks || {};
  const button = root.querySelector('.page-chat-launcher-button');
  const input = root.querySelector('.page-chat-launcher-input');
  const send = root.querySelector('.page-chat-launcher-send');
  if (searchOnly) {
    const label = cfg.search_label || fb.search_label || 'Search for a session';
    if (button) {
      button.innerHTML = `${icon(cfg.search_icon || fb.search_icon || 'search', { size: '18px' })}<span>${label}</span>`;
      button.setAttribute('aria-label', label);
      button.title = cfg.search_tooltip || fb.search_tooltip || label;
    }
    if (input) {
      input.placeholder = cfg.search_placeholder || fb.search_placeholder || label;
      input.setAttribute('aria-label', label);
    }
    // Search-only: the send slot becomes a CLEAR control. There is no chat to
    // open here and the filter already runs as the user types, so the press is
    // spent on the field instead — clear it, or collapse the pill once it is
    // already empty. See the is-clear rule in app3.css for the red affordance
    // and the click handler in _wirePill for the two-step behaviour.
    if (send) {
      send.type = 'button';
      send.classList.add('is-clear');
      send.innerHTML = icon(cfg.search_clear_icon || fb.search_clear_icon || 'x', { size: '16px' });
    }
    // Title and aria-label follow the field's contents rather than being written
    // once here, so they are derived — see _syncClearControl.
    _syncClearControl(spec, root, input, send);
    return;
  }
  if (button) {
    const label = cfg.label || fb.label || 'Ask or Search';
    button.innerHTML = `${icon(cfg.icon || 'bot', { size: '18px' })}<span>${label}</span>`;
    button.setAttribute('aria-label', cfg.aria_label || fb.aria_label || label);
    button.title = cfg.tooltip || fb.tooltip || button.getAttribute('aria-label');
  }
  if (input) {
    input.placeholder = cfg.input_placeholder || fb.input_placeholder || 'Ask or search';
    // Same fallback chain as mountPageChatLauncher, so a pill that flips BACK to
    // ask mode is byte-identical to one that was never search-only.
    input.setAttribute('aria-label', cfg.input_aria_label || fb.input_aria_label
      || (button && button.getAttribute('aria-label')) || 'Ask WebAgent or search');
  }
  if (send) {
    send.type = 'submit';
    send.classList.remove('is-clear');
    send.innerHTML = icon('send', { size: '16px' });
    send.title = 'Ask WebAgent';
    send.setAttribute('aria-label', 'Ask WebAgent');
  }
}

/**
 * Watch the host for the structural flips that switch a sub-view (a tab class, a
 * section's visibility) and re-apply the mode. Attribute-filtered and rAF-
 * throttled, so a busy table only ever costs one cheap re-check per frame.
 * Returns the observer for unmount, or null when the route has no mode to track.
 */
function _observeMode(spec, root, host) {
  if (typeof spec.searchOnly !== 'function' || typeof MutationObserver !== 'function') return null;
  let queued = false;
  const observer = new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => {
      queued = false;
      _applyMode(spec, root);
    });
  });
  observer.observe(host, { subtree: true, attributes: true, attributeFilter: ['class', 'style', 'hidden'] });
  return observer;
}

/** True when the OS asks for reduced motion — the pill then snaps as it always did. */
function _reducedMotion() {
  return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
}

/** The damped-spring timing for one direction, with a universal fallback. */
function _springTiming(collapsing) {
  const curve = collapsing ? PILL_SPRING_COLLAPSE : PILL_SPRING_EXPAND;
  const supported = typeof CSS !== 'undefined' && CSS.supports?.('animation-timing-function', curve);
  return supported ? curve : PILL_SPRING_FALLBACK;
}

/**
 * Spring the pill between two measured widths. `fill: 'none'` is deliberate: the
 * animation ends exactly on the resting width, so dropping it is seamless and no
 * inline width is ever left behind. Returns null when the pill must snap
 * (reduced motion, no WAAPI, or no measurable travel — a hidden root measures 0).
 */
function _springWidth(root, from, to, collapsing) {
  if (_reducedMotion() || typeof root.animate !== 'function') return null;
  if (!Number.isFinite(from) || !Number.isFinite(to)) return null;
  if (from < 1 || to < 1 || Math.abs(to - from) < 1) return null;
  return root.animate(
    [{ width: `${from}px` }, { width: `${to}px` }],
    { duration: PILL_SPRING_MS, easing: _springTiming(collapsing), fill: 'none' },
  );
}

function _setExpanded(root, expanded) {
  const button = root.querySelector('.page-chat-launcher-button');
  const form = root.querySelector('.page-chat-launcher-form');
  // Measure before the flip: mid-spring this is the width on screen right now,
  // so re-toggling springs on from wherever the pill currently is.
  const fromWidth = root.getBoundingClientRect().width;
  root._pageChatLauncherMotion?.cancel();
  // The pill chrome belongs to the root while expanded and to the button while
  // collapsed. Hold it still for the length of the spring so the pill the user
  // sees moving is the element actually being resized — see
  // PAGE-CHAT-LAUNCHER-SPRING in ui/shared/css/app3.css.
  root.dataset.animating = expanded ? 'expand' : 'collapse';
  root.dataset.expanded = expanded ? 'true' : 'false';
  button?.setAttribute('aria-expanded', String(expanded));
  form.hidden = !expanded;
  const toWidth = root.getBoundingClientRect().width;
  const motion = _springWidth(root, fromWidth, toWidth, !expanded);
  root._pageChatLauncherMotion = motion;
  if (!motion) {
    // Reduced motion, no travel, or no WAAPI: snap, and send the chrome home now.
    root.removeAttribute('data-animating');
  } else {
    motion.finished
      .catch(() => { /* superseded by a newer spring */ })
      .then(() => {
        if (root._pageChatLauncherMotion !== motion) return;
        root._pageChatLauncherMotion = null;
        root.removeAttribute('data-animating');
      });
  }
  if (expanded) {
    requestAnimationFrame(() => form.querySelector('input')?.focus());
  }
}

/**
 * Keep a search-only pill's clear control honest about its two roles.
 *
 * With text in the field the press clears it; with an empty field the same press
 * collapses the pill. The control looks identical either way, so its title and
 * aria-label are re-derived from the field rather than written once — otherwise a
 * screen reader would announce "Clear search" for a press that just closes the
 * pill. A no-op outside search mode, where the slot is an ordinary send button.
 */
function _syncClearControl(spec, root, input, send) {
  if (!send || !input || root.dataset.mode !== 'search') return;
  const cfg = _config(spec);
  const fb = spec.fallbacks || {};
  const empty = !input.value;
  const label = empty
    ? (cfg.search_collapse_label || fb.search_collapse_label || 'Collapse search')
    : (cfg.search_clear_label || fb.search_clear_label || 'Clear search');
  const tooltip = empty
    ? (cfg.search_collapse_tooltip || fb.search_collapse_tooltip || label)
    : (cfg.search_clear_tooltip || fb.search_clear_tooltip || label);
  send.title = tooltip;
  send.setAttribute('aria-label', label);
}

function _wirePill(spec, root, button, host) {
  const form = root.querySelector('.page-chat-launcher-form');
  const input = root.querySelector('.page-chat-launcher-input');
  const send = root.querySelector('.page-chat-launcher-send');
  let drag = null;
  let suppressClick = false;

  button.addEventListener('pointerdown', (event) => {
    // Re-check the sub-view the moment the user reaches for the pill, so the
    // label they see matches what the press will do even if the host flipped
    // between animation frames.
    _applyMode(spec, root);
    if (event.button !== 0) return;
    const rect = root.getBoundingClientRect();
    drag = {
      startX: event.clientX,
      startY: event.clientY,
      originLeft: rect.left,
      originTop: rect.top,
      moved: false,
    };
    button.setPointerCapture?.(event.pointerId);
  });

  button.addEventListener('pointermove', (event) => {
    if (!drag) return;
    const dx = event.clientX - drag.startX;
    const dy = event.clientY - drag.startY;
    if (!drag.moved && Math.hypot(dx, dy) < 5) return;
    drag.moved = true;
    const hostRect = host.getBoundingClientRect();
    const rootRect = root.getBoundingClientRect();
    const left = Math.max(EDGE_GUTTER, Math.min(hostRect.width - rootRect.width - EDGE_GUTTER,
      drag.originLeft - hostRect.left + dx));
    const top = Math.max(EDGE_GUTTER, Math.min(hostRect.height - rootRect.height - EDGE_GUTTER,
      drag.originTop - hostRect.top + dy));
    root.dataset.dragging = 'true';
    root.style.left = `${left}px`;
    root.style.right = 'auto';
    root.style.top = `${top}px`;
    root.style.bottom = 'auto';
  });

  const finish = (event) => {
    if (!drag) return;
    button.releasePointerCapture?.(event.pointerId);
    if (drag.moved) {
      const hostRect = host.getBoundingClientRect();
      const rect = root.getBoundingClientRect();
      const side = rect.left + rect.width / 2 < hostRect.left + hostRect.width / 2 ? 'left' : 'right';
      const position = {
        side,
        inline: Math.max(EDGE_GUTTER, Math.round(side === 'left' ? rect.left - hostRect.left : hostRect.right - rect.right)),
        bottom: Math.max(EDGE_GUTTER, Math.round(hostRect.bottom - rect.bottom)),
      };
      _applyPosition(root, position);
      _writePosition(spec, position);
      suppressClick = true;
      setTimeout(() => { suppressClick = false; }, 0);
    }
    root.dataset.dragging = 'false';
    drag = null;
  };
  button.addEventListener('pointerup', finish);
  button.addEventListener('pointercancel', finish);
  button.addEventListener('click', (event) => {
    if (suppressClick) { event.preventDefault(); return; }
    _setExpanded(root, root.dataset.expanded !== 'true');
  });

  input.addEventListener('input', () => {
    _syncSearch(spec, root, input.value);
    // The trailing control clears or collapses depending on whether there is
    // anything to clear, so its title and aria-label follow every keystroke.
    _syncClearControl(spec, root, input, send);
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      _setExpanded(root, false);
      button.focus();
    }
  });

  form.addEventListener('submit', (event) => {
    event.preventDefault();
    // A search-only sub-view has no chat: the composer never opens the task
    // widget and the trailing button works the field instead (see _applyMode).
    // The filter already runs as the user types, so there is nothing to submit.
    if (root.dataset.mode === 'search') return;
    const message = input.value.trim();
    if (!message) { input.focus(); return; }
    _openWidget(spec, message);
  });

  // In search-only mode the trailing control is a clear/collapse button, not a
  // send one (its type is "button", so it never submits). It is TWO-STEP: with
  // text it empties the field down the same forward path the typing uses, so the
  // filter resets exactly as if the user had deleted the text; with the field
  // already empty the press means "done" and collapses the pill back to its idle
  // size — the same _setExpanded path the outside-press and Escape guards take.
  send.addEventListener('click', () => {
    if (root.dataset.mode !== 'search') return;
    if (!input.value) {
      _setExpanded(root, false);
      button.focus();
      return;
    }
    input.value = '';
    _syncSearch(spec, root, '');
    _syncClearControl(spec, root, input, send);
    input.focus();
  });

  // Collapse when a press lands anywhere outside the pill (its button and its
  // input both live inside `root`, so a press inside the pill is left alone and
  // the button's own click toggle stays the only way it re-opens).
  // Runs in the CAPTURE phase, matching the app's other outside-press guards
  // (user panel, mobile nav): page elements below call stopPropagation() in
  // their own bubble-phase handlers, which would swallow the press before a
  // bubble-phase document listener saw it and leave the pill stuck open.
  const _onOutsidePointerDown = (event) => {
    if (root.dataset.expanded !== 'true') return;
    if (root.contains(event.target)) return;
    _setExpanded(root, false);
  };
  document.addEventListener('pointerdown', _onOutsidePointerDown, true);

  // Returned so unmount can drop the document listener with the root.
  return () => document.removeEventListener('pointerdown', _onOutsidePointerDown, true);
}

/** Mount a route's launcher as a non-flow sibling overlay. Idempotent. */
export function mountPageChatLauncher(spec) {
  const host = document.getElementById(spec.hostId);
  if (!host || _config(spec).enabled === false) return null;
  const existing = document.getElementById(spec.rootId);
  if (existing) return existing;

  const cfg = _config(spec);
  const fb = spec.fallbacks || {};
  const root = document.createElement('div');
  root.id = spec.rootId;
  root.className = 'page-chat-launcher';
  root.dataset.route = spec.route;
  root.dataset.edge = 'right';
  root.dataset.expanded = 'false';

  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'page-chat-launcher-button';
  button.setAttribute('aria-label', cfg.aria_label || fb.aria_label || 'Ask WebAgent or search');
  button.setAttribute('aria-controls', `${spec.rootId}-form`);
  button.setAttribute('aria-expanded', 'false');
  button.title = cfg.tooltip || fb.tooltip || button.getAttribute('aria-label');
  button.innerHTML = `${icon(cfg.icon || 'bot', { size: '18px' })}<span>${cfg.label || fb.label || 'Ask or Search'}</span>`;

  const form = document.createElement('form');
  form.id = `${spec.rootId}-form`;
  form.className = 'page-chat-launcher-form';
  form.hidden = true;
  const input = document.createElement('input');
  input.type = 'search';
  input.id = `${spec.rootId}-input`;
  input.className = 'page-chat-launcher-input';
  input.placeholder = cfg.input_placeholder || fb.input_placeholder || 'Ask or search';
  input.autocomplete = 'off';
  input.setAttribute('aria-label', cfg.input_aria_label || fb.input_aria_label || button.getAttribute('aria-label'));
  const send = document.createElement('button');
  send.type = 'submit';
  send.className = 'page-chat-launcher-send';
  send.title = 'Ask WebAgent';
  send.setAttribute('aria-label', 'Ask WebAgent');
  send.innerHTML = icon('send', { size: '16px' });
  form.append(input, send);
  root.append(button, form);
  host.appendChild(root);
  _applyPosition(root, _readPosition(spec));
  // A sub-view can change what the pill IS (see _applyMode). Set the mode before
  // the pill is ever painted, then keep it current as the host flips between
  // sub-views — the launcher is mounted once per route, not per sub-view.
  _applyMode(spec, root);
  root._pageChatLauncherModeObserver = _observeMode(spec, root, host);
  // The outside-press guard is document-level, so it has to be disposable: the
  // root is dropped on route change but a document listener would outlive it.
  root._pageChatLauncherTeardown = _wirePill(spec, root, button, host);
  return root;
}

/** Remove a route's launcher. A separately opened task widget remains intact. */
export function unmountPageChatLauncher(rootId) {
  const root = document.getElementById(rootId);
  if (!root) return;
  root._pageChatLauncherMotion?.cancel();
  root._pageChatLauncherModeObserver?.disconnect();
  root._pageChatLauncherTeardown?.();
  root.remove();
}

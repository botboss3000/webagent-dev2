'use strict';

// Chat Controls Config — unified engine for header AND footer layout.
// Replaces chat-header-config.js and chat-footer-config.js.
//
// Loads elements from ui/chat/elements/ using _manifest.json,
// calls their html(cfg) to generate DOM, mounts into zones/carousels, and
// injects per-element CSS. Falls back to static DOM children with
// data-header-control for backward compatibility (the old pattern).
//
// ⚠️ DEFAULTS ONLY — do NOT edit these sizes here.
// The runtime source of truth is data/config/chat_ui.json.
// Any value you change here will be overridden by that JSON file at boot.
// Edit chat_ui.json instead. Search for "idle_footer" and "active_footer".
//
// Config: reads chat_ui.json via getAgentChatUi().
//
// Usage:
//   import { applyChatControlsConfig } from '../../chat-controls/chat-controls-config.js';
//   applyChatControlsConfig();

import { apiPath } from '../shared/js/config.js';
import { authHeaders } from '../shared/js/left-login.js';
import { partialsReady } from '../shared/js/partial-loader.js';
import { app } from '../shared/js/state.js';
import { getAgentChatUi } from '../shared/js/app-prompts.js';
import { applyRubberBand } from '../shared/js/rubber-band.js';
import { applyChatPillLayout } from '../shared/js/chat-pill-config.js';
import { applyMessageBubbleConfig } from '../shared/js/chat-surface.js';
import { init as initStatsControl } from './controls/stats.js';

let _applied = false;
let _lastWasNarrow = null;  // tracks panel width state for ResizeObserver

// Helper: use #chat-panel width for mobile/desktop breakpoint so that a narrow
// side-panel on a wide viewport still gets the compact mobile layout.
function _isNarrowPanel() {
  const panel = document.getElementById('chat-panel');
  return panel ? panel.clientWidth <= 768 : window.innerWidth <= 768;
}

// ── Content-width cap ──
// chat_common.content_max_width is a HOST rule — how wide the transcript and
// composer column may run — so it is resolved by VIEWPORT, never by
// _isNarrowPanel(). That helper deliberately treats the default desktop side
// panel (≈420px, under the 768px breakpoint) as narrow so it gets the compact
// layout; resolving content width from it applied chat_mobile's "100%" on
// desktop, so the column lost its 1000px cap the moment the panel was dragged
// wider than that. app-prompts.js and chat-header-config.js already resolve the
// same rule from window.innerWidth, so this keeps header and content in step.
const MOBILE_VIEWPORT_MAX = 768;

function _contentWidthProfile(chatUi) {
  if (!chatUi?.chat_common) return null;
  return mergeProfile(
    chatUi.chat_common,
    window.innerWidth <= MOBILE_VIEWPORT_MAX ? chatUi.chat_mobile : chatUi.chat_desktop,
  );
}

function _applyContentMaxWidth(chatUi) {
  const surface = document.getElementById('chat-panel');
  const width = _contentWidthProfile(chatUi)?.content_max_width;
  if (surface && width) surface.style.setProperty('--chat-surface-max-width', width);
}

// ── Footer mode state ──
// Resolved active and idle footer profiles (chat_pill + above_pill + below_pill).
// Set once at boot, then switchFooterMode() re-applies the right one.
let _activeFooterProfile = null;
let _idleFooterProfile = null;
let _currentFooterMode = 'active';
let _footerTransitionEpoch = 0;
const _FOOTER_ACTIONS_TRANSITION_MS = 220;

function _waitForFooterActions() {
  return new Promise((resolve) => setTimeout(resolve, _FOOTER_ACTIONS_TRANSITION_MS));
}

// ── Footer handle visibility helper (used by _buildActiveFooter and switchFooterMode) ──
function _footerRowHasVisibleControl(r) {
  if (!r || r.classList.contains('idle-collapsed')) return false;
  const controls = r.querySelectorAll('[data-header-control], [data-element-origin]');
  return Array.from(controls).some(
    el => el.style.display !== 'none' && !el.hidden
  );
}

// ── Element manifest (loaded once from disk) ──
const ELEMENTS_BASE = '/ui/chat/elements/';
let _manifest = null;
let _cssInjected = new Set();

async function _loadManifest() {
  if (_manifest) return _manifest;
  try {
    const resp = await fetch(ELEMENTS_BASE + '_manifest.json');
    if (!resp.ok) throw new Error('manifest not found');
    _manifest = await resp.json();
    return _manifest;
  } catch (_) {
    _manifest = { elements: {} };
    return _manifest;
  }
}

function _getElementEntry(name) {
  if (!_manifest || !_manifest.elements) return null;
  return _manifest.elements[name] || null;
}

async function _injectElementCSS(entry) {
  if (!entry || !entry.css || _cssInjected.has(entry.css)) return;
  _cssInjected.add(entry.css);
  try {
    const resp = await fetch(ELEMENTS_BASE + entry.css);
    if (!resp.ok) return;
    const css = await resp.text();
    const style = document.createElement('style');
    style.setAttribute('data-element-css', entry.css);
    style.textContent = css;
    document.head.appendChild(style);
  } catch (_) {}
}

let _moduleCache = new Map();

async function _loadElementModule(entry) {
  if (!entry) return null;
  if (_moduleCache.has(entry.entry)) return _moduleCache.get(entry.entry);
  try {
    const mod = await import(/* @vite-ignore */ ELEMENTS_BASE + entry.entry);
    // The boot curtain treats this builder's promise as the chat panel's
    // readiness signal, so include element styles in that promise as well.
    if (entry.css) await _injectElementCSS(entry);
    _moduleCache.set(entry.entry, mod);
    return mod;
  } catch (_) {
    return null;
  }
}

// ── Element factory ──
// Creates or finds a DOM node for the named control.
// Priority: 1) existing data-header-control element (backward compat),
//           2) element module loaded from manifest.
async function _ensureElement(name, cfg) {
  // 1. Check for existing DOM element (old pattern OR a dynamic element created
  //    by a previous apply). Matching data-element-origin too makes creation
  //    idempotent — without it, two concurrent applies both miss this query and
  //    create a second instance of the same control.
  const existing = document.querySelector(`[data-header-control="${name}"], [data-element-origin="${name}"]`);
  if (existing) return existing;

  // 2. Load from manifest
  const entry = _getElementEntry(name);
  if (!entry) return null;
  const mod = await _loadElementModule(entry);
  if (!mod || typeof mod.html !== 'function') return null;

  const html = mod.html(cfg || {});
  const temp = document.createElement('div');
  temp.innerHTML = html;
  const el = temp.firstElementChild;
  if (!el) return null;

  // Store the element name for lookup
  el.setAttribute('data-element-origin', name);

  // Wire it up
  if (typeof mod.init === 'function') {
    try { mod.init(el, cfg || {}); } catch (_) {}
  }

  return el;
}

// ── Deep-merge ──

function mergeProfile(base, overrides) {
  const out = { ...(base || {}) };
  for (const [key, value] of Object.entries(overrides || {})) {
    if (value && typeof value === 'object' && !Array.isArray(value)) {
      out[key] = mergeProfile(base?.[key], value);
    } else {
      out[key] = value;
    }
  }
  return out;
}

// ── Control-name → DOM element lookup ──

let _elementCache = new Map();

export function _findControl(name) {
  return document.querySelector(`[data-header-control="${name}"], [data-element-origin="${name}"]`);
}

// Async version: finds existing element OR loads it from the manifest.
async function _findOrCreateControl(name, cfg) {
  // Check cache first — ALWAYS call _showControl since _resetAllControls
  // may have hidden the element since it was cached (e.g. on reapply).
  if (_elementCache.has(name)) {
    const cached = _elementCache.get(name);
    if (name === 'session_dropdown') {
      const entry = _getElementEntry(name);
      const mod = await _loadElementModule(entry);
      try { mod?.init?.(cached, cfg || {}); } catch (_) {}
    }
    _showControl(cached);
    return cached;
  }
  const found = _findControl(name);
  if (found) {
    // The session dropdown is statically mounted during early chat boot. Its
    // idempotent init doubles as a live config update once the tier-resolved
    // chat_ui payload arrives (and again after an agent/surface switch).
    if (name === 'session_dropdown') {
      const entry = _getElementEntry(name);
      const mod = await _loadElementModule(entry);
      try { mod?.init?.(found, cfg || {}); } catch (_) {}
    }
    _showControl(found);
    _elementCache.set(name, found);
    return found;
  }
  const el = await _ensureElement(name, cfg);
  if (el) _elementCache.set(name, el);
  return el;
}

// ── Admin-only controls ──

const ADMIN_ONLY_CONTROLS = new Set();

export function _shouldShow(controlName, cfg) {
  if (!cfg || cfg.enabled === false) return false;
  const adminOnly = cfg.admin_only !== undefined
    ? cfg.admin_only
    : ADMIN_ONLY_CONTROLS.has(controlName);
  if (adminOnly && !document.body.classList.contains('is-admin')) return false;
  return true;
}

// ── Sizing helpers ──

function elSize(cfg) { return cfg?.element_size || cfg?.size || null; }
function containerSize(cfg) { return cfg?.container_size || cfg?.box_size || null; }

export function _sizeControl(el, cfg) {
  if (!el || !cfg) return;
  const es = elSize(cfg);
  const cs = containerSize(cfg);
  if (es) {
    const icon = el.querySelector('svg') || el.querySelector('i');
    if (icon) {
      icon.style.setProperty('width', es, 'important');
      icon.style.setProperty('height', es, 'important');
    }
  } else if (cs && cs !== 'auto') {
    // Auto-derive element_size from container_size when not explicitly set.
    // For a container_size like 28px, use ~70% of it (rounded down) so the
    // icon fills the button nicely without touching the edges.
    const num = parseFloat(cs);
    if (!isNaN(num)) {
      const derived = Math.floor(num * 0.7) + 'px';
      const icon = el.querySelector('svg') || el.querySelector('i');
      if (icon) {
        icon.style.setProperty('width', derived, 'important');
        icon.style.setProperty('height', derived, 'important');
      }
    }
  }
  if (cs && cs !== 'auto') {
    el.style.setProperty('height', cs, 'important');
    if (cfg.square !== false) {
      el.style.setProperty('width', cs, 'important');
      el.style.setProperty('padding', '0', 'important');
      el.style.setProperty('justify-content', 'center', 'important');
    }
  }
  if (cfg.min_width) el.style.setProperty('min-width', cfg.min_width, 'important');
  if (cfg.max_width) el.style.setProperty('max-width', cfg.max_width, 'important');
}

// ── Footer profile helpers ──
// Resolve a named footer profile (active_footer / idle_footer) from the merged
// profile. For active_footer, falls back to legacy top-level keys for backward
// compatibility with existing custom chat_ui.json files.
function _resolveFooterProfile(profile, key) {
  if (profile[key]) return profile[key];
  // Backward compat: if active_footer isn't defined, check old top-level keys
  if (key === 'active_footer') {
    const fallback = {};
    if (profile.chat_pill)  fallback.chat_pill  = profile.chat_pill;
    if (profile.above_pill) fallback.above_pill = profile.above_pill;
    if (profile.below_pill) fallback.below_pill = profile.below_pill;
    return Object.keys(fallback).length > 0 ? fallback : null;
  }
  return null;
}

// Default idle footer used when the admin hasn't configured one.
// ⚠️ DEFAULTS ONLY — do NOT edit these sizes here.
// The runtime source of truth is data/config/chat_ui.json.
// Any value you change here will be overridden by that JSON file at boot.
// Edit chat_ui.json instead. Search for "idle_footer" and "active_footer".
function _defaultIdleFooter() {
  return {
    margin_bottom: '24px',
    chat_pill: {
      pill_width: '500px',
      pill_radius: '41px',
      rows: [
        { left:   { controls: ['attach'],   align: 'center', padding: '20px 0 20px 20px' },
          center: { controls: ['textarea'], align: 'center', padding: '0' },
          right:  { controls: ['stop', 'continue', 'mic_send'], align: 'center', padding: '20px 20px 20px 0' } }
      ],
      controls: {
        textarea: { enabled: true, min_height: '42px', font_size: '16px', max_height: '42px', padding: '13px 8px 13px 8px' },
        stop:     { enabled: true, element_size: '36px', container_size: '42px' },
        continue: { enabled: true, element_size: '36px', container_size: '42px' },
        mic_send: { enabled: true, element_size: '36px', container_size: '42px' },
        mic:      { enabled: true, element_size: '36px', container_size: '42px' },
        send:     { enabled: true, element_size: '36px', container_size: '42px' },
        attach:   { enabled: true, element_size: '36px', container_size: '42px' },
      },
    },
    above_pill: { enabled: false },
    below_pill: { enabled: false },
  };
}

// The boot composer has a static plus button so file upload works before the
// config engine hydrates. Apply the attach element module once the resolved
// footer configuration is available, preserving that no-flash fallback.
async function _initStaticPillAttach(cfg) {
  const attach = document.getElementById('chat-attach-btn');
  if (!attach || attach.dataset.popover === 'true') return;
  const mod = await _loadElementModule(_getElementEntry('attach'));
  try { mod?.init?.(attach, cfg || {}); } catch (_) {}
}

// Build the full active footer infrastructure once. On an initial idle boot,
// keep the static loading pill in place while the off-pill controls hydrate;
// switchFooterMode('idle') then replaces it directly with the idle pill.
//
// ⚠️  All layout decisions (which controls, in which zones, at what sizes)
// come from data/config/chat_ui.json. This function is a PURE APPLIER —
// don't add fallback layout decisions here. Edit chat_ui.json instead.
// Search for "active_footer" and "idle_footer".
async function _buildActiveFooter({ initialIdle = false } = {}) {
  const profile = _activeFooterProfile;
  if (!profile) return;

  // Set pill width on the surface
  if (profile.chat_pill?.pill_width) {
    const surface = document.getElementById('chat-panel');
    if (surface) {
      surface.style.setProperty('--chat-pill-width', profile.chat_pill.pill_width);
      surface.style.setProperty('--chat-pill-current-width', profile.chat_pill.pill_width);
    }
  }
  if (profile.chat_pill?.pill_radius) {
    const surface = document.getElementById('chat-panel');
    if (surface) {
      surface.style.setProperty('--chat-pill-radius', profile.chat_pill.pill_radius);
    }
  }

  // An idle session resolves its final dock before controls hydrate. Do not
  // briefly overwrite it with the active offset during that async rebuild.
  if (!initialIdle && profile.margin_bottom !== undefined) {
    const area = document.getElementById('chat-input-area');
    if (area) {
      area.style.setProperty('bottom', profile.margin_bottom, 'important');
    }
  }

  // Build component_panel (#chat-component-panel) — the agent component panel.
  // Its FULL VIEW (the only open state, CHAT-COMPONENT-FULL-VIEW) is measured by
  // js/chat-components.js and follows chat_common.content_max_width, so there is
  // no panel width to set here; only an optional radius (structural default in
  // app1.css). Hidden entirely when disabled in chat_ui.json.
  const cp = profile.component_panel;
  const cpEl = document.getElementById('chat-component-panel');
  if (cp && cpEl) {
    if (cp.enabled === false) {
      cpEl.style.display = 'none';
    } else {
      if (cp.radius) cpEl.style.setProperty('--component-panel-radius', cp.radius);
    }
  }

  // Do not paint the active pill between the static loading pill and the
  // initial idle pill. The active profile remains available for first focus.
  if (profile.chat_pill && !initialIdle) {
    applyChatPillLayout({
      pill: document.getElementById('chat-input-row'),
      input: document.getElementById('chat-input'),
      stats: document.getElementById('chat-pill-stats-row'),
      pillButtons: document.querySelector('#chat-input-row .chat-pill-buttons'),
      mic: document.getElementById('chat-voice-btn'),
      send: document.getElementById('chat-send'),
      attach: document.getElementById('chat-attach-btn'),
      stop: document.getElementById('chat-stop-btn'),
      continue: document.getElementById('chat-continue-btn'),
    }, profile.chat_pill);
  }

  // Build below_pill (#chat-footer-row)
  // ⚠️  Always use the "rows" format in chat_ui.json. The flat left/center/right
  // shorthand is converted here for backward compat only. Edit chat_ui.json, not
  // this fallback. Search for "below_pill" in data/config/chat_ui.json.
  const below = profile.below_pill;
  const belowEl = document.getElementById('chat-footer-row');
  if (below && below.enabled !== false) {
    belowEl?.removeAttribute('hidden');
    if (!initialIdle) {
      belowEl?.classList.remove('idle-collapsed');
      belowEl?.classList.add('expanded');
    }
    if (below.rows === undefined && (below.left || below.center || below.right)) {
      below.rows = [{ left: below.left || [], center: below.center || [], right: below.right || [] }];
    }
    await _applyToContainer('chat-footer-row', below);
  }

  // Build above_pill (#chat-above-pill)
  // ⚠️  Always use the "rows" format in chat_ui.json (same as below_pill and
  // chat_header). The flat left/center/right shorthand is converted here for
  // backward compat, but rows is the canonical format — edit chat_ui.json, not
  // this fallback. Search for "above_pill" in data/config/chat_ui.json.
  const above = profile.above_pill;
  const aboveEl = document.getElementById('chat-above-pill');
  if (above && above.enabled !== false) {
    if (!initialIdle) aboveEl?.classList.remove('idle-collapsed');
    if (above.rows === undefined && (above.left || above.center || above.right)) {
      above.rows = [{ left: above.left || [], center: above.center || [], right: above.right || [] }];
    }
    await _applyToContainer('chat-above-pill', above);
  }

  // Footer handle visibility
  setTimeout(() => {
    const h = document.getElementById('chat-footer-handle');
    const r = document.getElementById('chat-footer-row');
    if (h && r) {
      h.style.display = _footerRowHasVisibleControl(r) ? '' : 'none';
    }
  }, 50);
}

// Compute CSS custom properties from the idle profile and set them on
// #chat-input-area so CSS rules can pick them up.
function _computeIdleCSSVars() {
  const idle = _idleFooterProfile;
  const active = _activeFooterProfile;
  if (!idle || !active) return;

  const area = document.getElementById('chat-input-area');

  // Pill width: use absolute px value from the idle profile
  if (idle.chat_pill?.pill_width && area) {
    area.style.setProperty('--idle-pill-width', idle.chat_pill.pill_width);
  }

  // Bottom offset
  if (idle.margin_bottom !== undefined) {
    let val = idle.margin_bottom;
    if (typeof val === 'string' && val.endsWith('vh')) {
      val = Math.round(window.innerHeight * parseFloat(val) / 100) + 'px';
    }
    if (area) area.style.setProperty('--idle-margin-bottom', val);
  }
}

/**
 * Switch the footer between 'active' and 'idle' mode.
 * Rebuilds the chat_pill with the appropriate row layout so the idle
 * single-row config (attach | textarea | stop/continue) is rendered
 * instead of just collapsing parts of the active 2-row layout.
 */
export async function switchFooterMode(mode) {
  if (mode !== 'active' && mode !== 'idle') return;
  if (mode === _currentFooterMode) return;
  _currentFooterMode = mode;
  const transitionEpoch = ++_footerTransitionEpoch;

  const area = document.getElementById('chat-input-area');
  const surface = document.getElementById('chat-panel');
  const activeProfile = _activeFooterProfile;

  // Shared element references for pill rebuild
  const pillEls = () => ({
    pill: document.getElementById('chat-input-row'),
    input: document.getElementById('chat-input'),
    stats: document.getElementById('chat-pill-stats-row'),
    pillButtons: document.querySelector('#chat-input-row .chat-pill-buttons'),
    mic: document.getElementById('chat-voice-btn'),
    send: document.getElementById('chat-send'),
    attach: document.getElementById('chat-attach-btn'),
    stop: document.getElementById('chat-stop-btn'),
    continue: document.getElementById('chat-continue-btn'),
  });

  if (mode === 'idle') {
    // Let the below-pill actions leave while the active composer remains
    // stable; collapsing both at once creates a visually abrupt jump.
    const footerRow = document.getElementById('chat-footer-row');
    footerRow?.classList.remove('footer-actions-reserved');
    footerRow?.classList.add('footer-actions-exiting');
    await _waitForFooterActions();
    if (transitionEpoch !== _footerTransitionEpoch || _currentFooterMode !== 'idle') return;
    footerRow?.classList.remove('footer-actions-exiting');

    _computeIdleCSSVars();
    area?.classList.add('footer-mode-idle');
    // Override inline !important bottom from init with idle offset
    const idleBottom = area?.style.getPropertyValue('--idle-margin-bottom');
    if (idleBottom && area) area.style.setProperty('bottom', idleBottom, 'important');
    // Drive above/below collapse via the idle-collapsed classes — but keep
      // the activity bar + panel visible in idle mode.
      const abovePill = document.getElementById('chat-above-pill');
      if (abovePill) abovePill.classList.add('idle-collapsed');
    document.getElementById('chat-footer-row')?.classList.remove('expanded');
    document.getElementById('chat-footer-row')?.classList.add('idle-collapsed');
    document.getElementById('chat-footer-handle')?.style.setProperty('display', 'none');
    // Shrink pill width CSS var
    const idleW = area?.style.getPropertyValue('--idle-pill-width');
    if (idleW && surface) surface.style.setProperty('--chat-pill-current-width', idleW);
    // Rebuild pill with idle 1-row layout (attach | textarea | stop/continue/mic_send)
    if (_idleFooterProfile?.chat_pill) {
      app.__rebuildingPill = true;
      applyChatPillLayout(pillEls(), _idleFooterProfile.chat_pill);
      app.__rebuildingPill = false;
    }
  } else {
    const fr = document.getElementById('chat-footer-row');
    fr?.removeAttribute('hidden');
    // Reserve the final below-pill footprint without painting its controls,
    // then apply dock + active geometry in this same layout turn. The browser
    // therefore paints one direct idle-to-active expansion, never an idle pill
    // moving down/up before it expands.
    fr?.classList.remove('idle-collapsed');
    fr?.classList.add('footer-actions-reserved');
    if (activeProfile?.margin_bottom !== undefined && area) {
      area.style.setProperty('bottom', activeProfile.margin_bottom, 'important');
    }

    // Expand directly into the final, space-reserved active position.
    area?.classList.remove('footer-mode-idle');
    document.getElementById('chat-above-pill')?.classList.remove('idle-collapsed');
    if (activeProfile?.chat_pill?.pill_width && surface) {
      surface.style.setProperty('--chat-pill-current-width', activeProfile.chat_pill.pill_width);
    }
    // Rebuild pill with active 2-row layout (textarea row + attach/stats/buttons row)
    if (activeProfile?.chat_pill) {
      app.__rebuildingPill = true;
      applyChatPillLayout(pillEls(), activeProfile.chat_pill);
      app.__rebuildingPill = false;
      // The rebuild suppresses autoresize while controls are reparented.
      // Restore chat_ui.json's configured resting width before any input event.
      app.autoResizeChatInput?.();
      // Focus the textarea so the cursor is ready — the rebuild hides & reparents it.
      if (app.chatInput && document.activeElement !== app.chatInput) {
        app.chatInput.focus();
      }
      // Block clicks on the newly-appeared buttons for a short window
      // so the user's lingering finger can't accidentally hit one.
      const pill = document.getElementById('chat-input-row');
      if (pill) {
        pill.classList.add('pill-no-clicks');
        clearTimeout(app.__pillNoClicksTimer);
        app.__pillNoClicksTimer = setTimeout(() => {
          pill.classList.remove('pill-no-clicks');
        }, 300);
      }
    }
    // The active pill expands first. Reveal its below-pill controls only after
    // that geometry has settled, so they do not lead the transition.
    await _waitForFooterActions();
    if (transitionEpoch !== _footerTransitionEpoch || _currentFooterMode !== 'active') return;
    fr?.classList.remove('footer-actions-reserved');
    fr?.classList.add('expanded');

    // Footer handle visibility
    setTimeout(() => {
      const h = document.getElementById('chat-footer-handle');
      const r = document.getElementById('chat-footer-row');
      if (h && r) {
        h.style.display = _footerRowHasVisibleControl(r) ? '' : 'none';
      }
    }, 50);
  }
}

/** Get the current footer mode ('active' or 'idle'). */
export function getFooterMode() { return _currentFooterMode; }

// ── Zone creation ──

function _createZone(className) {
  const div = document.createElement('div');
  div.className = className;
  return div;
}

// ── Carousel row builder ──

export async function _buildCarouselRow(names, rowIdx, controlsCfg, containerEl) {
  const rowEl = document.createElement('div');
  rowEl.className = 'chat-header-row chat-header-carousel';
  rowEl.setAttribute('data-controls-row', String(rowIdx));

  const chevLeft = document.createElement('button');
  chevLeft.type = 'button';
  chevLeft.className = 'chat-header-chev left';
  chevLeft.setAttribute('aria-label', 'Scroll left');
  chevLeft.innerHTML = '&#10094;';

  const strip = document.createElement('div');
  strip.className = 'chat-header-carousel-strip';

  const chevRight = document.createElement('button');
  chevRight.type = 'button';
  chevRight.className = 'chat-header-chev right';
  chevRight.setAttribute('aria-label', 'Scroll right');
  chevRight.innerHTML = '&#10095;';

  rowEl.appendChild(chevLeft);
  rowEl.appendChild(strip);
  rowEl.appendChild(chevRight);

  let anyVisible = false;

  for (const name of names) {
    const controlCfg = controlsCfg?.[name] || {};
    const show = _shouldShow(name, controlCfg);
    if (!show) continue;

    const el = await _findOrCreateControl(name, controlCfg);
    if (!el) continue;
    anyVisible = true;
    el.style.setProperty('display', 'inline-flex', 'important');
    el.style.setProperty('align-items', 'center', 'important');
    el.style.setProperty('gap', '4px', 'important');
    el.style.setProperty('background', 'var(--chat-pill-bg, color-mix(in oklab, var(--bg-elev) 82%, transparent))', 'important');
    el.style.setProperty('border', 'var(--border-width) solid color-mix(in oklab, currentColor 12%, transparent)', 'important');
    el.style.setProperty('backdrop-filter', 'blur(12px) saturate(140%)', 'important');
    el.style.setProperty('-webkit-backdrop-filter', 'blur(12px) saturate(140%)', 'important');
    el.style.setProperty('border-radius', '14px', 'important');
    el.style.setProperty('padding', '2px 10px', 'important');
    el.style.setProperty('min-height', '28px', 'important');
    el.style.setProperty('box-sizing', 'border-box', 'important');
    el.style.setProperty('flex-shrink', '0', 'important');
    el.style.setProperty('position', 'relative', 'important');
    el.style.setProperty('color', 'var(--fg-3)', 'important');
    el.style.setProperty('font-size', '14px', 'important');
    el.style.setProperty('font-weight', '600', 'important');
    el.style.setProperty('cursor', 'pointer', 'important');
    el.style.setProperty('user-select', 'none', 'important');
    el.style.setProperty('-webkit-user-select', 'none', 'important');
    _sizeControl(el, controlCfg);

    strip.appendChild(el);
  }

  if (!anyVisible) return null;
  containerEl.appendChild(rowEl);

  // Wire chevrons
  const _update = () => {
    const overflow = strip.scrollWidth - strip.clientWidth > 1;
    rowEl.classList.toggle('has-overflow', overflow);
    chevLeft.classList.toggle('visible', overflow && strip.scrollLeft > 1);
    chevRight.classList.toggle('visible', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
  };
  const scrollStep = () => Math.max(80, Math.floor(strip.clientWidth * 0.6));
  chevLeft.addEventListener('click', () => { strip.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
  chevRight.addEventListener('click', () => { strip.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
  strip.addEventListener('scroll', _update, { passive: true });
  applyRubberBand(strip);
  requestAnimationFrame(_update);
  if (typeof ResizeObserver !== 'undefined') {
    let rp = false;
    const ro = new ResizeObserver(() => { if (!rp) { rp = true; requestAnimationFrame(() => { rp = false; _update(); }); } });
    ro.observe(strip);
    ro.observe(rowEl);
    window.addEventListener('resize', _update);
  }
  return rowEl;
}


// ── Build a carousel strip inside a single zone ──

export async function _buildZoneCarousel(names, zoneEl, controlsCfg) {
  zoneEl.classList.add('chat-header-zone-carousel');

  const chevLeft = document.createElement('button');
  chevLeft.type = 'button';
  chevLeft.className = 'chat-header-zone-chev left';
  chevLeft.setAttribute('aria-label', 'Scroll left');
  chevLeft.innerHTML = '&#10094;';

  const chevRight = document.createElement('button');
  chevRight.type = 'button';
  chevRight.className = 'chat-header-zone-chev right';
  chevRight.setAttribute('aria-label', 'Scroll right');
  chevRight.innerHTML = '&#10095;';

  const strip = document.createElement('div');
  strip.className = 'chat-header-zone-carousel-strip';

  zoneEl.appendChild(chevLeft);
  zoneEl.appendChild(strip);
  zoneEl.appendChild(chevRight);

  for (const name of names) {
    const controlCfg = controlsCfg?.[name] || {};
    if (!_shouldShow(name, controlCfg)) continue;

    const el = await _findOrCreateControl(name, controlCfg);
    if (!el) continue;
    el.style.setProperty('display', 'inline-flex', 'important');
    el.style.setProperty('align-items', 'center', 'important');
    el.style.setProperty('gap', '4px', 'important');
    el.style.setProperty('background', 'var(--chat-pill-bg, color-mix(in oklab, var(--bg-elev) 82%, transparent))', 'important');
    el.style.setProperty('border', 'var(--border-width) solid color-mix(in oklab, currentColor 12%, transparent)', 'important');
    el.style.setProperty('backdrop-filter', 'blur(12px) saturate(140%)', 'important');
    el.style.setProperty('-webkit-backdrop-filter', 'blur(12px) saturate(140%)', 'important');
    el.style.setProperty('border-radius', '14px', 'important');
    el.style.setProperty('padding', '2px 10px', 'important');
    el.style.setProperty('min-height', '28px', 'important');
    el.style.setProperty('box-sizing', 'border-box', 'important');
    el.style.setProperty('flex-shrink', '0', 'important');
    el.style.setProperty('position', 'relative', 'important');
    el.style.setProperty('color', 'var(--fg-3)', 'important');
    el.style.setProperty('font-size', '14px', 'important');
    el.style.setProperty('font-weight', '600', 'important');
    el.style.setProperty('cursor', 'pointer', 'important');
    el.style.setProperty('user-select', 'none', 'important');
    el.style.setProperty('-webkit-user-select', 'none', 'important');
    _sizeControl(el, controlCfg);

    strip.appendChild(el);
  }

  // Wire chevrons
  const _update = () => {
    const overflow = strip.scrollWidth - strip.clientWidth > 1;
    zoneEl.classList.toggle('has-overflow', overflow);
    chevLeft.classList.toggle('visible', overflow && strip.scrollLeft > 1);
    chevRight.classList.toggle('visible', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
  };
  const scrollStep = () => Math.max(60, Math.floor(strip.clientWidth * 0.6));
  chevLeft.addEventListener('click', () => { strip.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
  chevRight.addEventListener('click', () => { strip.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
  strip.addEventListener('scroll', _update, { passive: true });
  applyRubberBand(strip);
  requestAnimationFrame(_update);
  if (typeof ResizeObserver !== 'undefined') {
    let rp = false;
    const ro = new ResizeObserver(() => { if (!rp) { rp = true; requestAnimationFrame(() => { rp = false; _update(); }); } });
    ro.observe(strip);
    ro.observe(zoneEl);
    window.addEventListener('resize', _update);
  }
}


// ── Build a zone row (left/center/right) ──

async function _buildZoneRow(rowCfg, rowIdx, controlsCfg, containerEl) {
  const _flatten = (v) => Array.isArray(v) ? v : (v && typeof v === 'object' ? v.carousel || [] : []);
  const wanted = new Set([
    ..._flatten(rowCfg.left),
    ..._flatten(rowCfg.center),
    ..._flatten(rowCfg.right),
  ]);
  if (wanted.size === 0) return null;

  const rowEl = document.createElement('div');
  rowEl.className = 'chat-header-row';
  rowEl.setAttribute('data-controls-row', String(rowIdx));

  const leftEl = _createZone('chat-header-zone chat-header-zone-left');
  const centerEl = _createZone('chat-header-zone chat-header-zone-center');
  const rightEl = _createZone('chat-header-zone chat-header-zone-right');

  rowEl.appendChild(leftEl);
  rowEl.appendChild(centerEl);
  rowEl.appendChild(rightEl);

  let anyVisible = false;

  for (const zone of ['left', 'center', 'right']) {
    const zoneNames = rowCfg[zone] || [];
    const zoneEl = zone === 'left' ? leftEl : zone === 'center' ? centerEl : rightEl;

    if (zoneNames && typeof zoneNames === 'object' && !Array.isArray(zoneNames) && Array.isArray(zoneNames.carousel)) {
      // Zone-level carousel
      if (zoneNames.carousel.some(n => _shouldShow(n, controlsCfg?.[n] || {}))) anyVisible = true;
      await _buildZoneCarousel(zoneNames.carousel, zoneEl, controlsCfg);
    } else {
      // Flat array — inline controls
      for (const name of (zoneNames || [])) {
        const controlCfg = controlsCfg?.[name] || {};
        const show = _shouldShow(name, controlCfg);
        if (!show) continue;

        const el = await _findOrCreateControl(name, controlCfg);
        if (!el) continue;
        anyVisible = true;
        _sizeControl(el, controlCfg);
        if (el.parentNode !== zoneEl) {
          zoneEl.appendChild(el);
        }
      }
    }
  }

  if (!anyVisible) return null;
  if (rowCfg.height) {
    rowEl.style.setProperty('height', rowCfg.height, 'important');
  }
  containerEl.appendChild(rowEl);
  return rowEl;
}

// ── Reset ALL controls in a container to hidden state (before builders reveal wanted ones) ──

function _resetAllControls(containerEl) {
  const allControls = containerEl.querySelectorAll(
    '[data-header-control], [data-element-origin]',
  );
  for (const el of allControls) {
    el.hidden = true;
    el.style.setProperty('display', 'none', 'important');
    // Reseat into the container so it can be rediscovered for reparenting.
    if (el.parentNode !== containerEl) {
      containerEl.appendChild(el);
    }
  }
}

// Show a previously-hidden control — must clear the display:none!important
// set by _resetAllControls, since inline !important beats the [hidden] attribute.
export function _showControl(el) {
  el.hidden = false;
  el.style.removeProperty('display');
}

// ── Apply config to a specific container ──

function _dedupeActivityControl(containerEl) {
  // The static composer owns the canonical activity chip. During a partial
  // control hydration, a generated wrapper can coexist with it.
  const staticBar = document.querySelector('#chat-activity-bar[data-header-control="activity"]');
  if (!staticBar) return;

  for (const wrapper of containerEl.querySelectorAll('[data-element-origin="activity"]')) {
    if (!wrapper.contains(staticBar)) wrapper.remove();
  }

  for (const bar of document.querySelectorAll('#chat-activity-bar')) {
    if (bar === staticBar) continue;
    const wrapper = bar.closest('[data-element-origin="activity"]');
    if (wrapper) wrapper.remove();
    else bar.remove();
  }
}

async function _applyToContainer(containerId, headerCfg) {
  const containerEl = document.getElementById(containerId);
  if (!containerEl) return;

  if (headerCfg.enabled === false) {
    containerEl.style.display = 'none';
    return;
  }
  containerEl.style.display = '';
  _dedupeActivityControl(containerEl);

  // Apply container-level max-width
  if (headerCfg.max_width) {
    containerEl.style.setProperty('--chat-header-max-width', headerCfg.max_width);
  } else {
    containerEl.style.removeProperty('--chat-header-max-width');
  }

  // Clear existing rows (dynamic elements are regenerated; static controls with
  // data-header-control are rescued back to the container for re-use).
  const existingRows = containerEl.querySelectorAll('[data-controls-row]');
  for (const r of existingRows) {
    const controls = r.querySelectorAll('[data-header-control], [data-element-origin]');
    for (const c of controls) containerEl.appendChild(c);
    r.remove();
  }

  // Blanket-reset: hide everything so only the builders' wanted controls appear.
  _resetAllControls(containerEl);

  // Defensive dedupe: a raced apply (two builders creating the same dynamic
  // element before the cache was set) can leave duplicate data-element-origin
  // nodes in the container. Keep the first, drop the rest — heals a page that
  // already has duplicates on its next reapply. The kept node also becomes the
  // authoritative cache entry so no stale reference can resurrect a removed one.
  const seenOrigins = new Set();
  for (const el of [...containerEl.querySelectorAll('[data-element-origin]')]) {
    const o = el.getAttribute('data-element-origin');
    if (seenOrigins.has(o)) {
      el.remove();
    } else {
      seenOrigins.add(o);
      _elementCache.set(o, el);
    }
  }

  const rows = headerCfg.rows;
  if (!Array.isArray(rows)) return;

  const controlsCfg = headerCfg.controls || {};

  for (let i = 0; i < rows.length; i++) {
    const rowCfg = rows[i];

    if (Array.isArray(rowCfg.carousel) && rowCfg.carousel.length > 0) {
      await _buildCarouselRow(rowCfg.carousel, i, controlsCfg, containerEl);
      continue;
    }

    await _buildZoneRow(rowCfg, i, controlsCfg, containerEl);
  }
}

// ── Public entry point ──
// Serialized: boot (main.js) and agent-switch reapplies (session-core.js /
// session-agent.js / session-init.js) can overlap — two concurrent applies
// both miss the element cache and race _ensureElement, creating duplicate
// dynamic elements. All callers share one in-flight promise; a reapply waits
// for it to settle before rebuilding.

let _applying = null;

export async function applyChatControlsConfig() {
  if (_applied) return;
  _applied = true;
  if (_applying) return _applying;
  _applying = _applyOnce();
  try {
    return await _applying;
  } finally {
    _applying = null;
  }
}

async function _applyOnce() {
  await partialsReady;

  try {
    // Load element manifest before building controls
    await _loadManifest();

    // getAgentChatUi does client-side deep-merge: global chat_ui.json +
    // the current agent's metadata.chat_ui override from the agents list.
    const chatUi = getAgentChatUi();

    if (!chatUi || !chatUi.chat_common) {
      _applied = false;
      return;
    }

    const isMobile = _isNarrowPanel();
    const overrides = window.__CHAT_PORTAL__
      ? chatUi.chat_widget
      : (isMobile ? chatUi.chat_mobile : chatUi.chat_desktop);
    const profile = mergeProfile(chatUi.chat_common, overrides);
    if (!profile) return;

    // ── Content-width cap (host rule) ──
    // Applied here rather than in _buildActiveFooter so it is re-established on
    // every re-apply (agent switch, panel-width crossing), not just at boot.
    _applyContentMaxWidth(chatUi);
    applyMessageBubbleConfig(document.getElementById('chat-panel'), profile);

    // ── Resolve footer profiles (active + idle) ──
    _activeFooterProfile = _resolveFooterProfile(profile, 'active_footer');
    _idleFooterProfile = _resolveFooterProfile(profile, 'idle_footer');
    if (!_idleFooterProfile) {
      _idleFooterProfile = _defaultIdleFooter();
    }
    await _initStaticPillAttach(
      _activeFooterProfile?.chat_pill?.controls?.attach
      || _idleFooterProfile?.chat_pill?.controls?.attach,
    );

    // ── Initialize stats control (chevron scroll + overflow visibility) ──
    initStatsControl();

    // ── Apply header (#chat-header) ──
    if (profile.chat_header) {
      await _applyToContainer('chat-header', profile.chat_header);
    }

    // Resolve the first footer mode BEFORE building. Previously the active pill
    // was painted here and only then replaced by idle, producing a full-layout
    // flash between the static loading pill and the intended idle composer.
    const ta = document.getElementById('chat-input');
    const shouldStartIdle = ta && !ta.value.trim() && document.activeElement !== ta;
    if (shouldStartIdle) {
      _computeIdleCSSVars();
      const area = document.getElementById('chat-input-area');
      // Establish the final idle dock synchronously, before any async control
      // imports can repaint the active profile during a session switch.
      const idleBottom = area?.style.getPropertyValue('--idle-margin-bottom');
      if (idleBottom && area) area.style.setProperty('bottom', idleBottom, 'important');
      area?.classList.add('footer-mode-idle');
      document.getElementById('chat-above-pill')?.classList.add('idle-collapsed');
      const footerRow = document.getElementById('chat-footer-row');
      footerRow?.classList.remove('expanded');
      footerRow?.classList.add('idle-collapsed');
      document.getElementById('chat-footer-handle')?.style.setProperty('display', 'none');
    }

    // Build shared active-only controls in their collapsed containers, but
    // leave the visible boot pill untouched when idle is the initial target.
    await _buildActiveFooter({ initialIdle: shouldStartIdle });

    // Re-check after async control imports: a user may have focused or typed in
    // the still-visible loading pill while those modules were hydrating.
    const commitIdle = shouldStartIdle
      && ta
      && !ta.value.trim()
      && document.activeElement !== ta;

    // ── Commit the resolved initial footer mode ──
    if (commitIdle) {
      // Force state so switchFooterMode doesn't early-return
      _currentFooterMode = 'active';
      await switchFooterMode('idle');
    } else if (shouldStartIdle) {
      // The active build was deliberately deferred, but the user interacted
      // during hydration. Build it now and preserve their focus/draft.
      _currentFooterMode = 'idle';
      await switchFooterMode('active');
    } else {
      _currentFooterMode = 'active';
    }

    document.body.setAttribute('data-chat-controls-applied', 'true');

  } catch (_) {
    // Best-effort — HTML fallback is already in place.
  }
}

// ── Re-apply (for theme/surface changes) ──

export async function reapplyChatControlsConfig() {
  // Wait for any in-flight apply so the rebuild starts from a settled DOM
  // (avoids two builders racing the same rows / element cache).
  while (_applying) {
    try { await _applying; } catch (_) {}
  }
  _applied = false;
  return applyChatControlsConfig();
}

// Register on app so the currentAgentId setter can trigger it automatically
// from a single choke point — every agent-switch path flows through there.
app._reapplyChatControls = reapplyChatControlsConfig;

// ── ResizeObserver: re-apply when panel width crosses the 768px threshold ──
// Lets the header/footer switch between desktop and mobile layouts when the
// user resizes the side panel without needing a page refresh.
(function _initPanelResizeWatch() {
  const panel = document.getElementById('chat-panel');
  if (!panel) return;
  if (typeof ResizeObserver === 'undefined') return;
  let debounceTimer = null;
  const ro = new ResizeObserver(() => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      const nowNarrow = panel.clientWidth <= 768;
      if (_lastWasNarrow !== null && _lastWasNarrow !== nowNarrow) {
        reapplyChatControlsConfig();
      }
      _lastWasNarrow = nowNarrow;
    }, 150);
  });
  ro.observe(panel);
})();

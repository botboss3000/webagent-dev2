'use strict';

// Chat UI config — LEGACY footer config. Superseded by the unified
// chat-controls system (chat-controls-config.js). This runs only when
// data-chat-controls-applied is not set (fallback path).
//
// Reads the independent panel profile from chat_ui.json boot payload and
// applies the canonical two-row zone layout, button sizes, stat visibility,
// and textarea sizing to the live DOM. chat_ui.json is the single source of
// truth; do NOT add layout decisions here — edit data/config/chat_ui.json.
// Search for "active_footer" and "idle_footer".
//
// Keys — universal across all cell types:
//   element_size   — icon (svg) height, or font-size for text elements
//   container_size — button box size (square, width=height)
//
// Old keys (size, box_size) are still recognized for backward compat.
//
// CELL POSITIONING: each cell key ("row,col") drives the element's
// grid-column and grid-row in the 3×2 grid. The JSON fully controls
// placement — the CSS fallback rules are overwritten at boot.

import { apiPath } from '../../shared/js/config.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { partialsReady } from '../../shared/js/partial-loader.js';
import { applyChatPillLayout } from '../../shared/js/chat-pill-config.js';
import { applyRubberBand } from '../../shared/js/rubber-band.js';

let _applied = false;

// Helper: use #chat-panel width for mobile/desktop breakpoint so that a narrow
// side-panel on a wide viewport still gets the compact mobile layout.
function _isNarrowPanel() {
  const panel = document.getElementById('chat-panel');
  return panel ? panel.clientWidth <= 768 : window.innerWidth <= 768;
}

// Deep-merge a surface override onto chat_common. Arrays deliberately replace
// their base values, while nested objects (such as chat_pill) inherit per key.
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

// Known stats-element ids so we can show/hide based on config. The HTML has
// them all present; the JSON picks the subset to make visible.
const KNOWN_STATS_IDS = ['chat-token-bar', 'chat-model-ctx', 'chat-cost'];

function _applyNamedControls(names, idByName) {
  const wanted = new Set(names || []);
  for (const [name, id] of Object.entries(idByName)) {
    const el = document.getElementById(id);
    if (el) el.hidden = !wanted.has(name);
  }
}

// ── Stats carousel: wire chevron scroll + visibility (FOOTER-STATS-CAROUSEL) ─────
function _wireStatsCarousel() {
  const strip = document.getElementById('chat-pill-stats-strip');
  const chevLeft = document.querySelector('.chat-stats-chev.left');
  const chevRight = document.querySelector('.chat-stats-chev.right');
  if (!strip || !chevLeft || !chevRight) return;

  const _update = () => {
    const overflow = strip.scrollWidth - strip.clientWidth > 1;
    chevLeft.classList.toggle('visible', overflow && strip.scrollLeft > 1);
    chevRight.classList.toggle('visible', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
  };

  const scrollStep = () => Math.max(60, Math.floor(strip.clientWidth * 0.5));
  chevLeft.addEventListener('click', () => { strip.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
  chevRight.addEventListener('click', () => { strip.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
  strip.addEventListener('scroll', _update, { passive: true });
  applyRubberBand(strip);

  requestAnimationFrame(_update);
  if (typeof ResizeObserver !== 'undefined') {
    let rp = false;
    const ro = new ResizeObserver(() => { if (!rp) { rp = true; requestAnimationFrame(() => { rp = false; _update(); }); } });
    ro.observe(strip);
    window.addEventListener('resize', _update);
  }
}

export async function applyChatFooterConfig() {
  if (_applied) return;
  // If the new unified chat-controls system has already configured the footer,
  // skip the legacy config entirely (same guard as chat-header-config.js).
  if (document.body.getAttribute('data-chat-controls-applied') === 'true') return;
  _applied = true;

  // Wait for the chat panel partial to be in the DOM before querying elements.
  await partialsReady;

  try {
    const resp = await window.__fetchUiConfig({ headers: { ...authHeaders() } });
    if (!resp.ok) return;
    const data = await resp.json().catch(() => null);
    if (!data || !data.chat_ui?.chat_common) return;

    // Pick the responsive panel profile.
    const isMobile = _isNarrowPanel();
    const overrides = window.__CHAT_PORTAL__
      ? data.chat_ui.chat_widget
      : (isMobile ? data.chat_ui.chat_mobile : data.chat_ui.chat_desktop);
    const profile = mergeProfile(data.chat_ui.chat_common, overrides);
    if (!profile) return;

    // Support both new active_footer.chat_pill and legacy top-level chat_pill
    const activeFooter = profile.active_footer || {};
    const pill = activeFooter.chat_pill || profile.chat_pill || {};

    const surface = document.getElementById('chat-panel');
    if (surface && profile.content_max_width) surface.style.setProperty('--chat-surface-max-width', profile.content_max_width);
    if (surface && pill.pill_width) {
      surface.style.setProperty('--chat-pill-width', pill.pill_width);
      surface.style.setProperty('--chat-pill-current-width', pill.pill_width);
    }
    if (surface && pill.pill_radius) {
      surface.style.setProperty('--chat-pill-radius', pill.pill_radius);
    }

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
    }, pill);

    // ── Above-pill zone: left/right button groups ────────────────────
    // Skip if the new chat-controls system handled it
    if (document.body.getAttribute('data-chat-controls-applied') !== 'true') {
      // Support both new active_footer.above_pill and legacy top-level above_pill
      const above = activeFooter.above_pill || profile.above_pill;
      const aboveEl = document.getElementById('chat-above-pill');
      if (above && aboveEl) {
        if (above.enabled === false) {
          aboveEl.style.display = 'none';
        } else {
          aboveEl.style.display = '';
          _applyNamedControls([...(above.left || []), ...(above.right || [])], {
            activity: 'chat-activity-bar', stop: 'chat-stop-btn', continue: 'chat-continue-btn',
          });
        }
      }
    }

    // ── Below-pill zone: left/right button groups ────────────────────
    // Support both new active_footer.below_pill and legacy top-level below_pill
    const below = activeFooter.below_pill || profile.below_pill;
    const belowEl = document.getElementById('chat-footer-row');
    // Skip below_pill if the new chat-controls system handled it
    if (document.body.getAttribute('data-chat-controls-applied') !== 'true') {
      if (below && belowEl) {
        if (below.enabled === false) {
          belowEl.style.display = 'none';
        } else {
          belowEl.removeAttribute('hidden');
          belowEl.style.display = '';
          _applyNamedControls([...(below.left || []), ...(below.right || [])], {
            abilities: 'chat-abilities-btn', target: 'chat-target-btn', mode: 'chat-mode-btn',
          });
        }
      }
    }

    // ── Debug borders ────────────────────────────────────────────────
    // Set chat_desktop.debug.borders in chat_ui.json to visualise the grid,
    // container, and element boundaries with coloured outlines + live
    // height labels (px).
    if (profile.debug?.borders) {
      const pill = document.getElementById('chat-input-row');
      if (!pill) return;

      // Inject a tiny stylesheet for the debug labels
      const _styleId = 'wa-debug-size-labels';
      if (!document.getElementById(_styleId)) {
        const s = document.createElement('style');
        s.id = _styleId;
        s.textContent = [
          '.dbg-label { position:absolute; z-index:9999; pointer-events:none;',
          '  font:10px/1 "JetBrains Mono",monospace; color:#fff;',
          '  background:rgba(0,0,0,0.75); padding:1px 4px; border-radius:3px;',
          '  white-space:nowrap; }',
          '.dbg-label-grid { left:4px; top:4px; }',
          '.dbg-label-cell { left:4px; bottom:4px; }',
          '.dbg-label-container { right:4px; top:4px; }',
          '.dbg-label-icon { right:4px; bottom:4px; }',
        ].join(' ');
        document.head.appendChild(s);
      }

      function _makeLabel(text, className, target) {
        const lbl = document.createElement('div');
        lbl.className = 'dbg-label ' + className;
        lbl.textContent = text;
        target.style.position = 'relative';
        target.appendChild(lbl);
        return lbl;
      }

      function _readHeight(el) { return Math.round(el.getBoundingClientRect().height) + 'px'; }

      // Grid outline — cyan
      pill.style.setProperty('outline', '2px solid cyan', 'important');
      pill.style.setProperty('outlineOffset', '-2px', 'important');
      const gridLabel = _makeLabel(_readHeight(pill), 'dbg-label-grid', pill);

      // Grid cells — each direct child gets a distinct colour
      const cellEls = pill.children;
      const gridColors = ['rgba(255,0,0,0.6)', 'rgba(0,255,0,0.6)', 'rgba(255,255,0,0.6)', 'rgba(255,0,255,0.6)'];
      const cellLabels = [];
      for (let i = 0; i < cellEls.length; i++) {
        const el = cellEls[i];
        if (el.id === 'chat-preview-bar' || el.id === 'chat-file-input') continue;
        el.style.setProperty('outline', `2px solid ${gridColors[i % gridColors.length]}`, 'important');
        el.style.setProperty('outlineOffset', '-2px', 'important');
        cellLabels.push({ el, lbl: _makeLabel(_readHeight(el), 'dbg-label-cell', el) });
      }

      // Container borders — orange on button elements
      const containerEls = document.querySelectorAll('.chat-pill-voice, .chat-pill-send, .chat-pill-attach');
      const containerLabels = [];
      containerEls.forEach(el => {
        el.style.setProperty('outline', '2px solid orange', 'important');
        el.style.setProperty('outlineOffset', '-2px', 'important');
        containerLabels.push({ el, lbl: _makeLabel(_readHeight(el), 'dbg-label-container', el) });
      });

      // Element (icon) borders — magenta on SVG icons
      const iconEls = document.querySelectorAll('.chat-pill-voice svg, .chat-pill-send svg, .chat-pill-attach svg');
      const iconLabels = [];
      iconEls.forEach(el => {
        el.style.setProperty('outline', '2px solid magenta', 'important');
        el.style.setProperty('outlineOffset', '-2px', 'important');
        iconLabels.push({ el, lbl: _makeLabel(_readHeight(el), 'dbg-label-icon', el) });
      });

      // Live-resize observer — updates all labels on every layout change
      const ro = new ResizeObserver(() => {
        gridLabel.textContent = _readHeight(pill);
        for (const { el, lbl } of cellLabels) lbl.textContent = _readHeight(el);
        for (const { el, lbl } of containerLabels) lbl.textContent = _readHeight(el);
        for (const { el, lbl } of iconLabels) lbl.textContent = _readHeight(el);
      });
      ro.observe(pill);
    }

    // ── Stats carousel: wire chevrons to scroll the stats strip (FOOTER-STATS-CAROUSEL) ──
    _wireStatsCarousel();

  } catch (_) {
    // Best-effort — the HTML fallback is already in place.
  }
}

'use strict';

// Chat header config — LEGACY header config. Superseded by the unified
// chat-controls system (chat-controls-config.js). This runs only when
// data-chat-controls-applied is not set (fallback path).
//
// Reads the independent panel profile from chat_ui.json boot payload and
// builds the header rows dynamically. chat_ui.json is the single source of
// truth; do NOT add layout decisions here — edit data/config/chat_ui.json.
// Search for "chat_header".
//
// Each row in chat_header.rows[] has either:
//   - three zones (left, center, right), OR
//   - a single "carousel" array — all controls flow into a scrollable
//     horizontal strip with left/right chevron buttons.
// Controls are moved into the correct zone container; sizing and visibility
// are applied per the config.

import { apiPath } from '../../shared/js/config.js';
import { applyRubberBand } from '../../shared/js/rubber-band.js';
import { authHeaders } from '../../shared/js/left-login.js';
import { partialsReady } from '../../shared/js/partial-loader.js';

let _applied = false;

// Helper: use #chat-panel width for mobile/desktop breakpoint so that a narrow
// side-panel on a wide viewport still gets the compact mobile layout.
function _isNarrowPanel() {
  const panel = document.getElementById('chat-panel');
  return panel ? panel.clientWidth <= 768 : window.innerWidth <= 768;
}

// Deep-merge a surface override onto chat_common. Arrays deliberately replace
// their base values, while nested objects inherit per key.
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

// Control-name → DOM element lookup. Elements must have data-header-control.
function _findControl(name) {
  return document.querySelector(`[data-header-control="${name}"]`);
}

// Controls that should only show when the user is an admin.
const ADMIN_ONLY_CONTROLS = new Set(['more']);

// Helper: read element_size, falling back to old 'size' key.
function elSize(cfg) {
  return cfg?.element_size || cfg?.size || null;
}
// Helper: read container_size, falling back to old 'box_size' key.
function containerSize(cfg) {
  return cfg?.container_size || cfg?.box_size || null;
}

// Size an element's icon and/or container based on a control config object.
function _sizeControl(el, cfg) {
  if (!el || !cfg) return;
  const es = elSize(cfg);
  if (es) {
    const icon = el.querySelector('svg') || el.querySelector('i');
    if (icon) {
      icon.style.setProperty('width', es, 'important');
      icon.style.setProperty('height', es, 'important');
    }
  }
  const cs = containerSize(cfg);
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

// Determine if a control should be shown, considering admin gating.
function _shouldShow(controlName, cfg) {
  if (!cfg || cfg.enabled === false) return false;
  const adminOnly = cfg.admin_only !== undefined
    ? cfg.admin_only
    : ADMIN_ONLY_CONTROLS.has(controlName);
  if (adminOnly && !document.body.classList.contains('is-admin')) return false;
  return true;
}

// Reposition a control element into the target zone container.
// Returns true if the control was placed.
function _placeInZone(el, zoneEl) {
  if (!el || !zoneEl) return false;
  // Only reparent if it's not already in this zone.
  if (el.parentNode !== zoneEl) {
    zoneEl.appendChild(el);
  }
  return true;
}

// Build a full-row carousel — all controls flow into one horizontal
// scrollable strip with chevron buttons on each side.
// Reuses the same fade-mask + chevron pattern as #main-tabs-wrap.
function _buildCarouselRow(names, i, headerCfg, headerEl) {
  const rowEl = document.createElement('div');
  rowEl.className = 'chat-header-row chat-header-carousel';
  rowEl.setAttribute('data-header-row', String(i));

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
    const el = _findControl(name);
    if (!el) continue;

    const controlCfg = headerCfg.controls?.[name] || {};
    const show = _shouldShow(name, controlCfg);

    if (!show) {
      el.hidden = true;
      el.remove();
      continue;
    }

    el.hidden = false;
    anyVisible = true;
    _sizeControl(el, controlCfg);

    // Apply the same glass-chip styling that zone children get.
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
    el.style.setProperty('font-size', '13px', 'important');
    el.style.setProperty('font-weight', '600', 'important');
    el.style.setProperty('cursor', 'pointer', 'important');
    el.style.setProperty('user-select', 'none', 'important');
    el.style.setProperty('-webkit-user-select', 'none', 'important');

    strip.appendChild(el);
  }

  if (!anyVisible) return null;

  headerEl.appendChild(rowEl);

  // ── Wire chevrons + scroll ──────────────────────────
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

// Build a zone container div with the given class name.
function _createZone(className) {
  const div = document.createElement('div');
  div.className = className;
  return div;
}

export async function applyChatHeaderConfig() {
  // Skip if the new chat-controls system already handled this
  if (document.body.getAttribute('data-chat-controls-applied') === 'true') return;
  if (_applied) return;
  _applied = true;

  // Wait for the chat panel partial to be in the DOM before querying elements.
  await partialsReady;

  try {
    let url = apiPath('/api/v1/auth/ui-config');
    const agentId = (typeof app !== 'undefined' && app.currentAgentId) || '';
    if (agentId) url += '?agent_id=' + encodeURIComponent(agentId);
    const resp = agentId
      ? await fetch(url, { headers: { ...authHeaders() } })
      : await window.__fetchUiConfig({ headers: { ...authHeaders() } });
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

    const headerCfg = profile.chat_header;
    if (!headerCfg) return;

    const headerEl = document.getElementById('chat-header');
    if (!headerEl) return;

    // Disable the entire header.
    if (headerCfg.enabled === false) {
      headerEl.style.display = 'none';
      return;
    }
    headerEl.style.display = '';

    // Apply header-level max-width from config, falling back to the surface max width.
    if (headerCfg.max_width) {
      headerEl.style.setProperty('--chat-header-max-width', headerCfg.max_width);
    } else {
      headerEl.style.removeProperty('--chat-header-max-width');
    }

    const rows = headerCfg.rows;
    if (!Array.isArray(rows)) return;

    // ── Build the row DOM from the config ──────────────────────────
    // Clear existing rows (keep controls that are direct children).
    const existingRows = headerEl.querySelectorAll('[data-header-row]');
    for (const r of existingRows) r.remove();

    // Collect all flat controls so we can re-parent them.
    // (Don't remove tui-bridge-indicator, it's managed elsewhere.)

    for (let i = 0; i < rows.length; i++) {
      const rowCfg = rows[i];

      // ── Carousel row — delegate to the carousel builder ────────────
      if (Array.isArray(rowCfg.carousel) && rowCfg.carousel.length > 0) {
        _buildCarouselRow(rowCfg.carousel, i, headerCfg, headerEl);
        continue;
      }

      // Build the set of ALL wanted control names in this row.
      const wanted = new Set([
        ...(rowCfg.left || []),
        ...(rowCfg.center || []),
        ...(rowCfg.right || []),
      ]);

      // Skip rows with nothing.
      if (wanted.size === 0) continue;

      // Create the row element.
      const rowEl = document.createElement('div');
      rowEl.className = 'chat-header-row';
      rowEl.setAttribute('data-header-row', String(i));

      // Create zone containers.
      const leftEl = _createZone('chat-header-zone chat-header-zone-left');
      const centerEl = _createZone('chat-header-zone chat-header-zone-center');
      const rightEl = _createZone('chat-header-zone chat-header-zone-right');

      // Append zones to the row.
      rowEl.appendChild(leftEl);
      rowEl.appendChild(centerEl);
      rowEl.appendChild(rightEl);

      // Place controls into zones.
      let anyVisible = false;

      for (const zone of ['left', 'center', 'right']) {
        const zoneNames = rowCfg[zone] || [];
        const zoneEl = zone === 'left' ? leftEl : zone === 'center' ? centerEl : rightEl;

        for (const name of zoneNames) {
          const el = _findControl(name);
          if (!el) continue;

          const controlCfg = headerCfg.controls?.[name] || {};
          const show = _shouldShow(name, controlCfg);

          if (!show) {
            el.hidden = true;
            // Don't append hidden elements — keep them out of the flow.
            el.remove();
            continue;
          }

          el.hidden = false;
          anyVisible = true;
          _sizeControl(el, controlCfg);
          _placeInZone(el, zoneEl);
        }
      }

      // If nothing is visible in this row, skip it entirely.
      if (!anyVisible) continue;

      // Apply row-level sizing from config.
      if (rowCfg.height) {
        rowEl.style.setProperty('height', rowCfg.height, 'important');
      }

      headerEl.appendChild(rowEl);
    }
  } catch (_) {
    // Best-effort — the HTML fallback is already in place.
  }
}

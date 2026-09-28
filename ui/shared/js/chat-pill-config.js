'use strict';

// Canonical chat-pill schema and renderer.
//
// ⚠️ JSON DRIVES SIZING — all button sizes (element_size/container_size),
// textarea properties (font_size, min_height, max_height, padding), and
// zone padding are authored in data/config/chat_ui.json. This file applies
// them as inline styles. Change the JSON, not the CSS or JS defaults.
//
// The public schema is always two rows with left/center/right zones. Older
// row/column configs are normalized here so stored per-agent overrides keep
// working while every surface consumes one layout shape.

import { showLeftOverlay } from './left-login.js';

const ZONES = ['left', 'center', 'right'];
const ALIGN_VALUES = { top: 'flex-start', center: 'center', bottom: 'flex-end' };
const CONTROL_ALIASES = { voice: 'mic' };
const MODE_CLASSES = [
  'chat-pill-mic-send',
  'chat-pill-distinct-buttons',
  'chat-pill-mic-only',
  'chat-pill-send-only',
  'chat-pill-no-action',
];

function mergeObjects(base, overrides) {
  return { ...(base || {}), ...(overrides || {}) };
}

function canonicalName(name) {
  return CONTROL_ALIASES[name] || name;
}

function emptyZoneMeta() {
  return { controls: [], carousel: null, align: null, padding: null };
}

function emptyRows() {
  return [
    { left: emptyZoneMeta(), center: emptyZoneMeta(), right: emptyZoneMeta() },
    { left: emptyZoneMeta(), center: emptyZoneMeta(), right: emptyZoneMeta() },
  ];
}

function normalizeZone(value) {
  // Legacy: bare array of control names → default align/padding
  if (Array.isArray(value)) return { controls: value.map(canonicalName), carousel: null, align: null, padding: null };
  // Object form: { controls, carousel, align, padding }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    return {
      controls: Array.isArray(value.controls) ? value.controls.map(canonicalName) : [],
      carousel: Array.isArray(value.carousel) ? value.carousel.map(canonicalName) : null,
      align: ALIGN_VALUES[value.align] || null,
      padding: value.padding || null,
    };
  }
  return emptyZoneMeta();
}

function normalizeRows(rows) {
  const out = emptyRows();
  for (let index = 0; index < Math.min(2, rows?.length || 0); index += 1) {
    for (const zone of ZONES) {
      out[index][zone] = normalizeZone(rows[index]?.[zone]);
    }
  }
  return out;
}

function legacyButtonName(buttons) {
  const micEnabled = buttons?.voice?.enabled !== false;
  const sendEnabled = buttons?.send?.enabled !== false;
  if (micEnabled && sendEnabled) return 'mic_send';
  if (micEnabled) return 'mic';
  if (sendEnabled) return 'send';
  return null;
}

function placeLegacyControl(rows, cell, name) {
  if (!cell || !name) return;
  const [rowRaw, colRaw] = String(cell).split(',');
  const row = Number.parseInt(rowRaw, 10) - 1;
  const col = Number.parseInt(colRaw, 10);
  if (row < 0 || row > 1) return;
  const zone = col === 1 ? 'left' : col === 3 ? 'right' : 'center';
  rows[row][zone].controls.push(name);
}

function finalizeRows(rows) {
  const hasCombined = rows.some(row => ZONES.some(
    zone => row[zone].controls.includes('mic_send') || (row[zone].carousel || []).includes('mic_send'),
  ));
  const seen = new Set();
  for (const row of rows) {
    for (const zone of ZONES) {
      row[zone].controls = row[zone].controls.filter(name => {
        if (hasCombined && (name === 'mic' || name === 'send')) return false;
        if (seen.has(name)) return false;
        seen.add(name);
        return true;
      });
      if (row[zone].carousel) {
        row[zone].carousel = row[zone].carousel.filter(name => {
          if (hasCombined && (name === 'mic' || name === 'send')) return false;
          if (seen.has(name)) return false;
          seen.add(name);
          return true;
        });
      }
    }
  }
  return rows;
}

export function normalizeChatPill(pill = {}) {
  const legacyButtons = pill.buttons || {};
  const canonicalControls = pill.controls || {};
  const hasLegacyShape = Boolean(
    pill.layout || pill.buttons || pill.textarea || pill.stats || pill.attach,
  );
  const micCfg = mergeObjects(
    canonicalControls.mic || canonicalControls.voice,
    legacyButtons.voice,
  );
  const sendCfg = mergeObjects(canonicalControls.send, legacyButtons.send);
  const combinedCfg = mergeObjects(micCfg, sendCfg);
  const controls = {
    ...canonicalControls,
    textarea: mergeObjects(canonicalControls.textarea, pill.textarea),
    stats: mergeObjects(canonicalControls.stats, pill.stats),
    attach: mergeObjects(canonicalControls.attach, pill.attach),
    mic: micCfg,
    send: sendCfg,
    // A switching control uses one common box. Preserve old configurations
    // by preferring the send dimensions when the two legacy buttons differ.
    mic_send: hasLegacyShape
      ? mergeObjects(canonicalControls.mic_send, combinedCfg)
      : mergeObjects(combinedCfg, canonicalControls.mic_send),
  };
  delete controls.voice;

  let rows;
  if (hasLegacyShape) {
    rows = emptyRows();
    const layout = pill.layout || {};
    placeLegacyControl(rows, layout.textarea || '1,2', 'textarea');
    placeLegacyControl(rows, layout.stats || '2,2', 'stats');
    placeLegacyControl(rows, layout.buttons || '1,3', legacyButtonName(legacyButtons));
    if (pill.attach?.enabled !== false) {
      placeLegacyControl(rows, layout.attach || '2,3', 'attach');
    }
  } else if (Array.isArray(pill.rows)) {
    rows = normalizeRows(pill.rows);
  } else {
    rows = normalizeRows([]);
  }

  return { ...pill, rows: finalizeRows(rows), controls };
}

export function configuredRowControls(config) {
  const names = [];
  for (const row of config?.rows || []) {
    for (const zone of ZONES) {
      for (const name of row?.[zone]?.controls || []) {
        if (config?.controls?.[name]?.enabled !== false) names.push(name);
      }
      for (const name of row?.[zone]?.carousel || []) {
        if (config?.controls?.[name]?.enabled !== false) names.push(name);
      }
    }
  }
  return names;
}

function showElement(el) {
  if (!el) return;
  el.hidden = false;
  el.style.removeProperty('display');
}

function hideElement(el) {
  if (!el) return;
  el.hidden = true;
  el.style.setProperty('display', 'none', 'important');
}

function sizeControl(el, cfg = {}) {
  if (!el) return;
  const containerSize = cfg.container_size || cfg.box_size;
  const elementSize = cfg.element_size || cfg.size;
  if (containerSize && containerSize !== 'auto') {
    el.style.setProperty('width', containerSize, 'important');
    el.style.setProperty('height', containerSize, 'important');
  }
  if (elementSize) {
    const icon = el.querySelector('svg') || el.querySelector('i');
    if (icon) {
      icon.style.setProperty('width', elementSize, 'important');
      icon.style.setProperty('height', elementSize, 'important');
    }
  }
}

let _lockedControlPopover = null;
let _lockedControlPopoverCleanup = null;

function _closeLockedControlPopover() {
  _lockedControlPopoverCleanup?.();
  _lockedControlPopoverCleanup = null;
  _lockedControlPopover?.remove();
  _lockedControlPopover = null;
}

function _positionLockedControlPopover(popover, anchor) {
  const rect = anchor.getBoundingClientRect();
  const padding = 8;
  const gap = 8;
  const width = popover.offsetWidth;
  const height = popover.offsetHeight;
  const right = rect.right + gap;
  const left = rect.left - width - gap;
  const preferredLeft = right + width <= window.innerWidth - padding
    ? right
    : (left >= padding ? left : rect.left + (rect.width - width) / 2);
  const x = Math.max(padding, Math.min(preferredLeft, window.innerWidth - width - padding));
  const y = Math.max(padding, Math.min(
    rect.top + (rect.height - height) / 2,
    window.innerHeight - height - padding,
  ));
  popover.style.left = `${x}px`;
  popover.style.top = `${y}px`;
}

export function showLockedControlPopover(anchor, cfg = {}) {
  _closeLockedControlPopover();
  const popover = document.createElement('div');
  popover.className = 'chat-control-lock-popover';
  popover.setAttribute('role', 'dialog');
  const message = document.createElement('div');
  message.className = 'chat-control-lock-popover-message';
  if (cfg.locked_feature) {
    const disabled = document.createElement('div');
    disabled.className = 'chat-control-lock-popover-line';
    const feature = document.createElement('strong');
    feature.textContent = cfg.locked_feature;
    disabled.append(feature, ' feature is disabled for anonymous users');
    const description = document.createElement('div');
    description.className = 'chat-control-lock-popover-line';
    description.textContent = cfg.locked_description || 'Register to unlock the full features of the app.';
    message.append(disabled, description);
  } else {
    message.textContent = cfg.locked_message || 'Register to unlock this feature.';
  }
  popover.appendChild(message);
  const cta = document.createElement('button');
  cta.type = 'button';
  cta.className = 'chat-control-lock-popover-cta';
  cta.textContent = cfg.locked_cta || 'Register';
  cta.addEventListener('click', () => {
    _closeLockedControlPopover();
    showLeftOverlay();
  });
  popover.appendChild(cta);
  document.body.appendChild(popover);
  const reposition = () => _positionLockedControlPopover(popover, anchor);
  reposition();
  window.addEventListener('resize', reposition);
  document.addEventListener('scroll', reposition, true);
  _lockedControlPopoverCleanup = () => {
    window.removeEventListener('resize', reposition);
    document.removeEventListener('scroll', reposition, true);
  };
  _lockedControlPopover = popover;
  setTimeout(() => document.addEventListener('click', _closeLockedControlPopover, { once: true }), 0);
}

function applyControlLock(el, cfg = {}) {
  if (!el) return;
  el._chatControlLockConfig = cfg;
  const locked = cfg.locked === true;
  el.classList.toggle('tier-locked', locked);
  if (locked) {
    el.setAttribute('aria-disabled', 'true');
    el.title = cfg.locked_feature
      ? `${cfg.locked_feature} feature is disabled for anonymous users.`
      : (cfg.locked_message || 'Register to unlock this feature.');
  } else {
    el.removeAttribute('aria-disabled');
  }
  if (el._chatControlLockBound) return;
  el._chatControlLockBound = true;
  el.addEventListener('click', (event) => {
    const liveCfg = el._chatControlLockConfig || {};
    if (liveCfg.locked !== true) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    showLockedControlPopover(el, liveCfg);
  }, true);
}

function setButtonMode(pillEl, names) {
  pillEl.classList.remove(...MODE_CLASSES);
  const hasCombined = names.includes('mic_send');
  const hasMic = names.includes('mic');
  const hasSend = names.includes('send');
  if (hasCombined) pillEl.classList.add('chat-pill-mic-send');
  else if (hasMic && hasSend) pillEl.classList.add('chat-pill-distinct-buttons');
  else if (hasMic) pillEl.classList.add('chat-pill-mic-only');
  else if (hasSend) pillEl.classList.add('chat-pill-send-only');
  else pillEl.classList.add('chat-pill-no-action');
}

// Stat types → element lookup. 'ctx' and 'ctx-max' share the same element;
// the renderer picks the presentation (compact number vs current/max) from
// whichever entry is active in the config.
const STATS_TYPE_EL = {
  'token-bar': '#chat-token-bar, .chat-token-bar',
  ctx: '#chat-model-ctx, .chat-model-ctx',
  'ctx-max': '#chat-model-ctx, .chat-model-ctx',
  cost: '#chat-cost, .chat-cost',
};

/**
 * Resolve chat_ui.json → controls.stats.visible into the live set of stat
 * chips. Each entry is either a plain string ('token-bar', 'ctx', 'ctx-max',
 * 'cost') or an object { type, decimals } — decimals overrides the per-type
 * default (ctx: 1 below 1M / 2 above; cost: 2, rounded up).
 *
 * The resolved list [{ type, decimals, el }] is stashed on the stats container
 * as `_statsConfig` so the renderers (chat-activity.js for the panel,
 * chat-surface.js for the widget/embed) can read which types are enabled and
 * what decimals to use. Elements for disabled types are hidden; enabled ones
 * are value-gated by their renderers (hidden while their value is zero).
 */
function applyStatsConfig(statsEl, cfg = {}) {
  if (!statsEl || !Array.isArray(cfg.visible)) return;
  const resolved = [];
  const seenEls = new Set();
  for (const raw of cfg.visible) {
    const entry = (typeof raw === 'string') ? { type: raw }
      : (raw && typeof raw === 'object' && typeof raw.type === 'string') ? { ...raw } : null;
    if (!entry) continue;
    const selector = STATS_TYPE_EL[entry.type];
    if (!selector) continue;
    const el = statsEl.querySelector(selector);
    if (!el || seenEls.has(el)) continue;   // drop dupes sharing one element (ctx/ctx-max)
    seenEls.add(el);
    resolved.push({
      type: entry.type,
      decimals: (typeof entry.decimals === 'number' && Number.isFinite(entry.decimals)) ? entry.decimals : null,
      el,
    });
  }
  // Stash on the canonical stats container (the passed element may be the row
  // wrapper; the container is .chat-pill-stats itself).
  const container = statsEl.classList.contains('chat-pill-stats')
    ? statsEl
    : statsEl.querySelector('.chat-pill-stats');
  if (container) container._statsConfig = resolved;
  statsEl._statsConfig = resolved;
  for (const selector of new Set(Object.values(STATS_TYPE_EL))) {
    const el = statsEl.querySelector(selector);
    if (!el) continue;
    const enabled = seenEls.has(el);
    el.hidden = !enabled;
    if (enabled) {
      // Clear any inline display a previous disabled-pass left behind —
      // otherwise the inline 'none' beats the CSS flex rule and the stat
      // never comes back after being re-enabled. Renderers re-hide on zero.
      el.style.display = '';
    } else {
      // Inline display beats the author CSS display rules (e.g.
      // .chat-token-bar { display:flex }) that would otherwise override the
      // hidden attribute. Enabled stats keep their renderer-controlled display.
      el.style.display = 'none';
    }
  }
}

export function applyChatPillLayout(els, rawPill = {}) {
  const pill = normalizeChatPill(rawPill);
  const pillEl = els?.pill;
  if (!pillEl) return pill;

  // A focused textarea is temporarily parked as a direct pill child below the
  // generated rows while they are replaced. Chromium can leave its blinking
  // caret painted at that transient position. Blur before the move, then put
  // focus and the selection back only after the textarea reaches its final
  // zone so no stray caret remains beneath the composer.
  const inputHadFocus = Boolean(els.input && document.activeElement === els.input);
  const inputSelection = inputHadFocus ? {
    start: els.input.selectionStart,
    end: els.input.selectionEnd,
    direction: els.input.selectionDirection,
  } : null;
  els.input?._chatPillRevealCaret?.();
  pillEl.classList.remove('chat-pill-caret-suppressed');
  if (inputHadFocus) {
    // Chromium can retain the textarea's caret as a detached compositor layer
    // even after blur if the focused node is reparented in the same frame. Hide
    // the caret before that frame and keep it hidden through programmatic focus
    // restoration. Genuine pointer/keyboard input below reveals it immediately.
    pillEl.classList.add('chat-pill-caret-suppressed');
    void els.input.offsetHeight;
    els.input.blur();
  }

  // The static shell uses the active two-row shape while boot/auth/config
  // settle. From here onward the normalized profile owns all geometry.
  pillEl.classList.remove('chat-pill-boot');

  const known = [
    els.input,
    els.stats,
    els.pillButtons,
    els.mic,
    els.send,
    els.attach,
    els.stop,
    els.continue,
  ].filter(Boolean);

  // Rescue stable nodes before removing the old generated rows. Moving the
  // nodes preserves all listeners registered by chat-send/attachments.
  for (const el of known) {
    if (pillEl.contains(el)) pillEl.appendChild(el);
  }
  pillEl.querySelector(':scope > .chat-pill-layout-rows')?.remove();

  if (els.pillButtons) {
    if (els.mic) els.pillButtons.appendChild(els.mic);
    if (els.send) els.pillButtons.appendChild(els.send);
  }

  const controls = {
    textarea: els.input,
    stats: els.stats,
    mic_send: els.pillButtons,
    mic: els.mic,
    send: els.send,
    attach: els.attach,
    stop: els.stop,
    continue: els.continue,
  };
  for (const el of Object.values(controls)) hideElement(el);

  const names = configuredRowControls(pill);
  setButtonMode(pillEl, names);
  pillEl.classList.add('chat-pill-row-layout');

  const rowsEl = document.createElement('div');
  rowsEl.className = 'chat-pill-layout-rows';

  for (let rowIndex = 0; rowIndex < 2; rowIndex += 1) {
    const rowCfg = pill.rows[rowIndex];
    const rowEl = document.createElement('div');
    rowEl.className = 'chat-pill-layout-row';
    rowEl.setAttribute('data-pill-row', String(rowIndex));

    for (const zone of ZONES) {
      const zoneCfg = rowCfg[zone];
      const zoneEl = document.createElement('div');
      zoneEl.className = `chat-pill-layout-zone chat-pill-layout-zone-${zone}`;
      // Apply per-zone alignment from config (overrides CSS default of flex-end)
      if (zoneCfg.align) zoneEl.style.alignItems = zoneCfg.align;
      // Apply per-zone padding from config
      if (zoneCfg.padding) zoneEl.style.padding = zoneCfg.padding;

      if (zoneCfg.carousel) {
        // ── Zone-level carousel (scrollable strip with chevrons) ──
        zoneEl.classList.add('chat-pill-zone-carousel');
        const chevLeft = document.createElement('button');
        chevLeft.type = 'button';
        chevLeft.className = 'chat-pill-zone-chev left';
        chevLeft.setAttribute('aria-label', 'Scroll left');
        chevLeft.innerHTML = '&#10094;';

        const strip = document.createElement('div');
        strip.className = 'chat-pill-zone-carousel-strip';

        const chevRight = document.createElement('button');
        chevRight.type = 'button';
        chevRight.className = 'chat-pill-zone-chev right';
        chevRight.setAttribute('aria-label', 'Scroll right');
        chevRight.innerHTML = '&#10095;';

        zoneEl.appendChild(chevLeft);
        zoneEl.appendChild(strip);
        zoneEl.appendChild(chevRight);

        for (const name of zoneCfg.carousel) {
          if (!names.includes(name)) continue;
          const el = controls[name];
          if (!el) continue;
          showElement(el);
          strip.appendChild(el);
        }

        // Wire chevrons
        const update = () => {
          const overflow = strip.scrollWidth - strip.clientWidth > 1;
          zoneEl.classList.toggle('has-overflow', overflow);
          chevLeft.classList.toggle('visible', overflow && strip.scrollLeft > 1);
          chevRight.classList.toggle('visible', overflow && strip.scrollLeft < strip.scrollWidth - strip.clientWidth - 1);
        };
        const scrollStep = () => Math.max(60, Math.floor(strip.clientWidth * 0.6));
        chevLeft.addEventListener('click', () => { strip.scrollBy({ left: -scrollStep(), behavior: 'smooth' }); });
        chevRight.addEventListener('click', () => { strip.scrollBy({ left: scrollStep(), behavior: 'smooth' }); });
        strip.addEventListener('scroll', update, { passive: true });
        requestAnimationFrame(update);
        if (typeof ResizeObserver !== 'undefined') {
          new ResizeObserver(update).observe(strip);
        }
      } else {
        // ── Standard inline controls ──
        for (const name of zoneCfg.controls) {
          if (!names.includes(name)) continue;
          const el = controls[name];
          if (!el || zoneEl.contains(el)) continue;
          showElement(el);
          if (name === 'mic_send') {
            showElement(els.mic);
            showElement(els.send);
          }
          zoneEl.appendChild(el);
        }
      }

      rowEl.appendChild(zoneEl);
    }
    rowsEl.appendChild(rowEl);
  }

  const preview = pillEl.querySelector(':scope > #chat-preview-bar');
  if (preview?.nextSibling) pillEl.insertBefore(rowsEl, preview.nextSibling);
  else pillEl.prepend(rowsEl);

  const textareaCfg = pill.controls.textarea || {};
  if (els.input) {
    if (textareaCfg.min_height) {
      els.input.style.minHeight = textareaCfg.min_height;
      els.input.removeAttribute('rows');
    }
    if (textareaCfg.max_height) els.input.style.maxHeight = textareaCfg.max_height;
    if (textareaCfg.font_size) {
      els.input.style.setProperty('--chat-pill-font-size', textareaCfg.font_size);
    }
    // padding is applied as inline-style so it overrides the CSS default
    // (8px 8px 2px 0). Authored in data/config/chat_ui.json.
    if (textareaCfg.padding) els.input.style.padding = textareaCfg.padding;
  }
  applyStatsConfig(els.stats, pill.controls.stats);
  applyControlLock(els.attach, pill.controls.attach);
  sizeControl(els.attach, pill.controls.attach);
  sizeControl(els.stop, pill.controls.stop);
  sizeControl(els.continue, pill.controls.continue);
  sizeControl(els.mic, names.includes('mic_send') ? pill.controls.mic_send : pill.controls.mic);
  sizeControl(els.send, names.includes('mic_send') ? pill.controls.mic_send : pill.controls.send);

  // Clear stale inline display values left by older/static shell versions.
  pillEl.style.removeProperty('display');

  if (inputHadFocus && els.input) {
    try {
      els.input.focus({ preventScroll: true });
      if (inputSelection.start !== null && inputSelection.end !== null) {
        els.input.setSelectionRange(
          inputSelection.start,
          inputSelection.end,
          inputSelection.direction || 'none',
        );
      }
    } catch (_) { /* Focus restoration is best-effort during teardown. */ }

    const revealCaret = () => {
      pillEl.classList.remove('chat-pill-caret-suppressed');
      els.input.removeEventListener('pointerdown', revealCaret, true);
      els.input.removeEventListener('keydown', revealCaret, true);
      els.input.removeEventListener('beforeinput', revealCaret, true);
      els.input.removeEventListener('blur', revealCaret, true);
      if (els.input._chatPillRevealCaret === revealCaret) {
        delete els.input._chatPillRevealCaret;
      }
    };
    els.input._chatPillRevealCaret = revealCaret;
    els.input.addEventListener('pointerdown', revealCaret, true);
    els.input.addEventListener('keydown', revealCaret, true);
    // Virtual keyboards may emit beforeinput without a keydown event.
    els.input.addEventListener('beforeinput', revealCaret, true);
    els.input.addEventListener('blur', revealCaret, true);
  }

  return pill;
}

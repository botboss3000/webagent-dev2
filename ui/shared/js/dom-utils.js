'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Shared DOM utilities extracted from duplicate definitions across the codebase.
 *
 * Whenever you add a function here, replace all duplicate local definitions
 * in individual files with an import from this module. This keeps escaping,
 * formatting, and DOM-building consistent and avoids the clone-family
 * duplication flagged by duplication scanners.
 *
 * ╔══════════════════════════════════════════════════════════════════════════╗
 * ║  IMPORTANT — when you REMOVE or RENAME a function in this file, you    ║
 * ║  must also UPDATE EVERY FILE that imports and uses it. Search for      ║
 * ║  the old name across the entire `ui/` tree to find all call sites.     ║
 * ║  The same applies when you ADD a function: prefer importing here       ║
 * ║  over duplicating the implementation in another file.                  ║
 * ╚══════════════════════════════════════════════════════════════════════════╝
 */

import { wireChatPillUploads, uploadAndPreview, startSpeechDictation, isVoiceInputSupported } from './attachments.js';
import { authHeaders } from './left-login.js';
import { openTextEditor } from './file-text-viewer.js';

// ── HTML escaping ─────────────────────────────────────────────────────────────

/**
 * Escape a string for safe insertion into innerHTML.
 * Uses a DOM-based approach (createTextNode → innerHTML) which avoids
 * regex injection and handles all edge cases (null, undefined, numbers).
 */
export function _esc(str) {
  const d = document.createElement('div');
  d.appendChild(document.createTextNode(str == null ? '' : String(str)));
  return d.innerHTML;
}

/**
 * Attribute-safe HTML escaping. Unlike _esc (which only escapes & < >, the way
 * a text node serializes), this also escapes " and ' so the result is safe to
 * drop inside a double- or single-quoted HTML attribute value. Use this when
 * interpolating into `title="…"`, `data-x="…"`, `value="…"`, etc.
 *
 * It is the single-source replacement for the hand-rolled char-map escapers
 * that several modules used in attribute contexts (app-config, update view).
 */
export function _escAttr(str) {
  return String(str == null ? '' : str).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

// ── Ability catalog (shared by both ability-table sister panels) ──────────────
// GET /api/v1/abilities/catalog is the drop-in data source: a file dropped into
// plugins/abilities/ shows up with no JS edit. Fetched once and cached globally,
// then shared by admin-ability-table.js and agent-ability-table.js, which each
// previously kept a byte-identical copy of this loader + cache. On any failure it
// returns an empty fallback ({groups:[], abilities:{}}) so callers still render.
let _abilityCatPromise = null;
let _abilityCatCached = null;

export async function loadAbilityCatalog() {
  if (_abilityCatCached) return _abilityCatCached;
  if (_abilityCatPromise) return _abilityCatPromise;
  _abilityCatPromise = (async () => {
    try {
      const res = await fetch('/api/v1/abilities/catalog', { headers: { ...authHeaders() } });
      if (!res.ok) throw new Error('HTTP ' + res.status);
      const cat = await res.json();
      if (!cat || !Array.isArray(cat.groups) || !cat.groups.length) {
        throw new Error('Empty catalog');
      }
      _abilityCatCached = cat;
      _abilityCatPromise = null;
      return cat;
    } catch (e) {
      _abilityCatPromise = null;
      console.warn('Ability catalog fetch failed', e);
      return { groups: [], abilities: {} };
    }
  })();
  return _abilityCatPromise;
}

// Drop the cached catalog so the next loadAbilityCatalog() re-fetches (used after
// a credential save changes an ability's configured-state).
export function invalidateAbilityCatalog() {
  _abilityCatCached = null;
}

// ── Button factory ────────────────────────────────────────────────────────────

/** Create a <button> with the given label and CSS class(es). */
export function _btn(label, className = 'agents-btn') {
  const b = document.createElement('button');
  b.type = 'button';
  b.className = className;
  b.textContent = label;
  return b;
}

// ── Relative time formatting ──────────────────────────────────────────────────

/** Format an ISO date string as a human-friendly relative time (e.g. "3m ago"). */
export function _fmtTime(iso) {
  if (!iso) return '\u2014';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diffMs = now - d;
    const diffMin = Math.floor(diffMs / 60000);
    if (diffMin < 1) return 'Just now';
    if (diffMin < 60) return diffMin + 'm ago';
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return diffH + 'h ago';
    const diffD = Math.floor(diffH / 24);
    if (diffD < 7) return diffD + 'd ago';
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
  } catch (_) { return '\u2014'; }
}

// ── Status badge ──────────────────────────────────────────────────────────────

/**
 * Render a status badge HTML string.
 * @param {string} status - Machine-readable status (ok, error, running, etc.)
 * @param {string} [clsPrefix='auto-status'] - CSS class prefix for the badge
 * @param {object} [customLabels] - Custom label lookup (falls back to defaults)
 */
export function _statusBadge(status, clsPrefix = 'auto-status') {
  if (!status) return `<span class="${clsPrefix} unknown">\u2014</span>`;
  const cls = String(status).replace(/\s+/g, '_');
  const labels = {
    ok: 'OK', error: 'Error', pending: 'Pending',
    running: 'Running', done: 'Done', stopped: 'Stopped',
    queued: 'Queued',
  };
  const label = labels[status] || status;
  return `<span class="${clsPrefix} ${_esc(cls)}">${_esc(label)}</span>`;
}

// ── Automation type icon ─────────────────────────────────────────────────────

/** Return inline HTML for a Lucide-based icon matching an automation type. */
export function _typeIcon(type) {
  switch (type) {
    case 'scheduled': return '<i data-lucide="clock" style="width:14px;height:14px;color:var(--accent);"></i>';
    case 'event':     return '<i data-lucide="zap" style="width:14px;height:14px;color:var(--warning);"></i>';
    case 'worker':    return '<i data-lucide="share-2" style="width:14px;height:14px;color:var(--success);"></i>';
    case 'clone':     return '<i data-lucide="copy" style="width:14px;height:14px;color:var(--fg-3);"></i>';
    case 'webhook':   return '<i data-lucide="webhook" style="width:14px;height:14px;color:var(--fg-3);"></i>';
    default:          return '<i data-lucide="circle" style="width:14px;height:14px;"></i>';
  }
}

// ── Enable/disable toggle switch ──────────────────────────────────────────────

/** Return HTML for a toggle-switch checkbox label. */
export function _enabledToggle(enabled, id, type) {
  const checked = enabled ? 'checked' : '';
  return `<label class="auto-toggle">
    <input type="checkbox" class="auto-toggle-input" data-id="${_esc(id)}" data-type="${_esc(type)}" ${checked}>
    <span class="auto-toggle-slider"></span>
  </label>`;
}

// ── Lucide icon helper ─────────────────────────────────────────────────────────

/** Return inline HTML for a Lucide icon element. */
export function _iconHtml(name, size) {
  const px = size || '18px';
  return `<i data-lucide="${name}" style="width:${px};height:${px};"></i>`;
}

// ── Chevron SVG ────────────────────────────────────────────────────────────────

/** Return inline SVG for a right-pointing chevron. */
export function _chevronSvg() {
  return '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>';
}

// ── "More" (⋯) row control + group status card (SISTER-PANEL) ─────────────────
// Two shared builders used by BOTH ability tables (agent-ability-table.js and
// admin-ability-table.js) — keep them mirrored:
//   • `_buildMoreButton` — the ⋯ button that sits in a row's RIGHT slot, the
//     exact mirror of the LEFT chevron column (13px, flex-shrink:0, align-self:
//     flex-start, padding-top:3px, and a -10px margin cancelling the flex
//     column-gap so it sits flush against the toggle). Opens the shared floating
//     menu (`openFloatingMenu`) with items built fresh on every open.
//   • `_buildGroupStatusCard` / `_paintGroupStatus` — the read-only group status
//     pill (Disabled · Partial · Enabled) that replaced the interactive group
//     tri-toggle; bulk group toggling is intentionally gone.
// Styling: `.ac-row-more` / `.ac-group-status` in app3.css.

/** Inline SVG for the three-dot "more" glyph (13px, matches `_chevronSvg`). */
function _moreDotsSvg() {
  return '<svg width="13" height="13" viewBox="0 0 24 24" fill="currentColor" stroke="none" aria-hidden="true"><circle cx="5" cy="12" r="1.8"/><circle cx="12" cy="12" r="1.8"/><circle cx="19" cy="12" r="1.8"/></svg>';
}

/**
 * Build a "more" (⋯) button for the RIGHT side of an `.ac-ability-row`.
 * Clicking it opens the shared floating menu (`openFloatingMenu`) built from
 * `items` — a function called fresh on EVERY open (so the menu reflects the
 * row's live state) or a static array. Items follow the floating-menu spec:
 * { icon, label, action, danger?, checked?, disabled? } | { separator: true }.
 *
 * @param {() => object[] | object[]} items
 * @param {object} [opts]  { title }  hover/aria label
 * @returns {HTMLButtonElement} the `.ac-row-more` button
 */
export function _buildMoreButton(items, opts = {}) {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'ac-row-more';
  const title = opts.title || 'More options';
  btn.title = title;
  btn.setAttribute('aria-label', title);
  btn.innerHTML = _moreDotsSvg();
  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    if (btn.disabled) return;
    const list = typeof items === 'function' ? items() : items;
    if (!list || !list.length) return;
    const point = btn._floatingMenuAnchorPoint;
    if (point && Number.isFinite(point.x) && Number.isFinite(point.y)) {
      // A hidden proxy button can forward its caller's pointer position so the
      // body-level menu still opens beside the control the user clicked.
      btn._floatingMenuAnchorPoint = null;
      openFloatingMenu(list, point.y + 8, point.x + 8);
      return;
    }
    const r = btn.getBoundingClientRect();
    // Anchor the menu's right edge at the button's right edge (menu width 180).
    openFloatingMenu(list, r.bottom + 4, r.right - 180);
  });
  return btn;
}

/**
 * Build a read-only group status card — Disabled · Partial · Enabled — that
 * replaces the old interactive group tri-toggle on an ability group head.
 * `state`: 'off' → Disabled, 'mixed' → Partial, 'on' → Enabled, 'na' → "—"
 * (group has no on/off members; e.g. credential/integration groups, which the
 * host page may re-paint with its own configured-count).
 * @param {string} id      element id (e.g. `ac-group-status-<groupId>`)
 * @param {'off'|'mixed'|'on'|'na'} [state]
 * @param {string} [title] hover text
 */
export function _buildGroupStatusCard(id, state = 'off', title) {
  const card = document.createElement('span');
  card.className = 'ac-group-status';
  card.id = id;
  card.dataset.state = state;
  if (title) card.title = title;
  _paintGroupStatus(card);
  return card;
}

/** Paint a group status card's label from its `data-state` ('off'|'mixed'|'on'|'na'). */
export function _paintGroupStatus(card) {
  if (!card) return;
  const s = card.dataset.state || 'off';
  card.textContent = s === 'on' ? 'Enabled' : (s === 'mixed' ? 'Partial' : (s === 'na' ? '—' : 'Disabled'));
}

// ── Number stepper (horizontal arrows) ──────────────────────────────────────────
// A `.ac-config-num` field renders as a STEPPER: a back (left) + forward (right)
// Lucide arrow sit INSIDE the field at its two edges, replacing the browser's
// native up/down spin-buttons. Clicking an arrow steps the value by the field's
// `step` (clamped to its min/max) and dispatches the SAME `input` + `change`
// events the native spinner did, so every existing autosave / clamp listener keeps
// working untouched. One delegated, capture-phase document handler drives every
// stepper on the page (wired once, lazily) — so BOTH render paths get it for free:
// the string builder `_renderSettingInput` (both ability tables) and the element
// builder `_wrapNumberStepper` (the agent Config tab). Inline SVG (not a
// `data-lucide` placeholder) so the arrows paint immediately without a
// `lucide.createIcons` pass — the agent Config tab never runs one.

/** Inline SVG for a Lucide arrow — left (dir<0, "back") or right (dir>0, "forward"). */
function _numArrowSvg(dir) {
  const path = dir > 0
    ? '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>'      // arrow-right
    : '<path d="m12 19-7-7 7-7"/><path d="M19 12H5"/>';    // arrow-left
  return `<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${path}</svg>`;
}

/** The two stepper-button HTML strings (back / forward) that flank a number input. */
function _numStepBtns() {
  return {
    down: `<button type="button" class="ac-num-step ac-num-down" tabindex="-1" aria-label="Decrease">${_numArrowSvg(-1)}</button>`,
    up: `<button type="button" class="ac-num-step ac-num-up" tabindex="-1" aria-label="Increase">${_numArrowSvg(1)}</button>`,
  };
}

/** Step a number input by ±step, clamp to min/max, then fire input + change. */
function _stepNumberInput(input, dir) {
  if (!input || input.disabled || input.readOnly) return;
  const step = parseFloat(input.step) || 1;
  const min = (input.min !== '' && input.min != null) ? parseFloat(input.min) : null;
  const max = (input.max !== '' && input.max != null) ? parseFloat(input.max) : null;
  let val = parseFloat(input.value);
  if (isNaN(val)) val = (min != null ? min : 0);
  val = dir > 0 ? val + step : val - step;
  // Round to the step's own precision so 0.1 + 0.2-style float drift never shows.
  const decimals = (String(step).split('.')[1] || '').length;
  if (decimals) val = parseFloat(val.toFixed(decimals));
  if (min != null && val < min) val = min;
  if (max != null && val > max) val = max;
  input.value = String(val);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
}

let _numStepperWired = false;
/** Wire the single delegated handler that drives every number stepper (idempotent). */
function _ensureNumStepperWiring() {
  if (_numStepperWired || typeof document === 'undefined') return;
  _numStepperWired = true;
  // Capture phase + stopPropagation so an arrow click never bubbles to an ancestor
  // row/ability toggle (the stepper lives inside an expanded ability body).
  document.addEventListener('click', (e) => {
    const btn = e.target.closest && e.target.closest('.ac-num-step');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    const wrap = btn.closest('.ac-num-stepper');
    const input = wrap && wrap.querySelector('input.ac-config-num');
    _stepNumberInput(input, btn.classList.contains('ac-num-up') ? 1 : -1);
  }, true);
}

let _configRangeWired = false;

function _rangeDisplay(value, mode) {
  const n = Number(value);
  if (!Number.isFinite(n)) return String(value ?? '');
  if (mode === 'percent') return `${Math.round(n * 100)}%`;
  if (mode === 'tokens') {
    if (n >= 1000000) {
      const m = (n / 1000000).toFixed(2).replace(/0+$/, '').replace(/\.$/, '');
      return `${m}M`;
    }
    if (n >= 1000) return `${Math.round(n / 1000)}K`;
    return Math.round(n).toLocaleString();
  }
  return String(n);
}

/** Keep the live value badge beside descriptor-driven range controls current. */
function _ensureConfigRangeWiring() {
  if (_configRangeWired || typeof document === 'undefined') return;
  _configRangeWired = true;
  document.addEventListener('input', (e) => {
    const input = e.target && e.target.closest && e.target.closest('input.ac-config-range');
    if (!input) return;
    const output = input.closest('.ac-range-control')?.querySelector('.ac-range-value');
    if (output) output.textContent = _rangeDisplay(input.value, input.dataset.display);
  });
}

/** Wrap a number `<input>` ELEMENT in a left/right arrow stepper; returns the wrapper. */
export function _wrapNumberStepper(inputEl) {
  _ensureNumStepperWiring();
  const wrap = document.createElement('span');
  wrap.className = 'ac-num-stepper';
  const { down, up } = _numStepBtns();
  wrap.insertAdjacentHTML('afterbegin', down);
  wrap.appendChild(inputEl);
  wrap.insertAdjacentHTML('beforeend', up);
  if (inputEl.readOnly || inputEl.disabled) {
    wrap.querySelectorAll('.ac-num-step').forEach((b) => { b.disabled = true; });
  }
  return wrap;
}

// ── Tri-state toggle ───────────────────────────────────────────────────────────

/** Build a 3-position toggle element (Off · Mixed · On). */
export function _buildTriToggle(id, title) {
  const tri = document.createElement('span');
  tri.className = 'ac-tri';
  tri.id = id;
  tri.dataset.state = 'off';
  tri.setAttribute('role', 'button');
  tri.setAttribute('tabindex', '0');
  tri.title = title || 'Off \u00b7 Mixed \u00b7 On — click the left half for all-off, the right half for all-on';
  tri.innerHTML = '<span class="ac-tri-knob"></span>';
  return tri;
}

/** Wire a tri-toggle: left half → all off, right half → all on. */
export function _wireTriToggle(tri, togglable, onSetAll) {
  const sync = () => {
    const onCount = togglable.filter(m => m.isEnabled()).length;
    tri.dataset.state = onCount === 0 ? 'off'
      : (onCount === togglable.length ? 'on' : 'mixed');
  };
  sync();

  tri.addEventListener('click', e => {
    e.stopPropagation();
    const r = tri.getBoundingClientRect();
    onSetAll((e.clientX - r.left) > r.width / 2);
  });
  tri.addEventListener('keydown', e => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      onSetAll(tri.dataset.state !== 'on');
    }
  });

  return sync;
}

/**
 * Build a per-row 3-position VISIBILITY toggle, styled identically to the group
 * `.ac-tri` (Off · Discoverable · Visible) so the two read as one component.
 * This REPLACES the old [enable switch + eye button] pair on an agent ability
 * row — one control now expresses all three states:
 *   • left  third → 'off'          (ability disabled)              — knob left,   grey
 *   • middle third → 'discoverable' (enabled, loaded on demand)    — knob middle, amber
 *   • right third → 'visible'      (enabled, tools & skill shown)  — knob right,  accent
 * The `data-state` is mapped onto the existing tri detents (off/mixed/on) so it
 * reuses the group tri's CSS verbatim. Optimistic: flips immediately, shows the
 * shared spinner → ✓ / ⚠ overlay ON TOP of the toggle while `onChange` runs, and
 * reverts to the prior state if it rejects. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
 *
 * The returned element carries `_apply(mode)` (drive it programmatically — used
 * by the group bulk toggle, runs the same save+confirm path) and `_mode()`.
 *
 * @param {object} cfg
 * @param {'off'|'discoverable'|'visible'} [cfg.value]  initial state
 * @param {boolean} [cfg.canEdit]
 * @param {boolean} [cfg.lockedOn]  ability can't be turned off (safety device)
 * @param {(mode:string, prevMode:string) => Promise<void>} cfg.onChange  performs the save(s); throws on failure → revert
 * @param {(msg:string) => void} [cfg.onBlocked]  called with a reason when an edit is rejected (e.g. an off attempt on a locked-on ability)
 * @returns {HTMLElement} the `.ac-tri` element
 */
export function _buildVisibilityTri(cfg) {
  const { value = 'off', canEdit = true, lockedOn = false, onChange, onBlocked } = cfg || {};
  const ORDER = ['off', 'discoverable', 'visible'];
  // Map the visibility mode onto the group tri's off/mixed/on detents so the CSS
  // (knob position + colour) is shared 1:1.
  const _toState = m => (m === 'visible' ? 'on' : (m === 'discoverable' ? 'mixed' : 'off'));

  const tri = document.createElement('span');
  tri.className = 'ac-tri ac-tri-vis';
  tri.setAttribute('role', 'slider');
  tri.setAttribute('tabindex', canEdit ? '0' : '-1');
  tri.innerHTML = '<span class="ac-tri-knob"></span>';

  let current = ORDER.includes(value) ? value : 'off';
  const _titleFor = m =>
    'Off · Discoverable · Visible — '
    + (m === 'off' ? 'disabled' : m === 'discoverable' ? 'enabled, loaded on demand' : 'enabled, tools & skill shown');
  const paint = () => {
    tri.dataset.state = _toState(current);
    tri.title = _titleFor(current);
    tri.setAttribute('aria-valuetext', current);
  };
  paint();

  tri._mode = () => current;

  if (!canEdit) {
    tri.classList.add('ac-tri-disabled');
    tri.setAttribute('aria-disabled', 'true');
    tri.removeAttribute('tabindex');
    return tri;
  }

  let _busy = false;
  async function _go(next) {
    if (_busy || next === current || !ORDER.includes(next)) return;
    // Safety device — can't be turned off. Snap back and report instead of saving.
    if (lockedOn && next === 'off') {
      if (onBlocked) onBlocked('Cannot be deactivated');
      paint();
      return;
    }
    const prev = current;
    _busy = true;
    current = next;
    paint();                       // optimistic
    _markSaving(tri);
    try {
      await onChange(next, prev);
      _flashSaveCheck(tri, true);
    } catch (e) {
      current = prev;
      paint();                     // revert — the save didn't land
      _flashSaveCheck(tri, false);
    } finally {
      _busy = false;
    }
  }
  tri._apply = _go;

  // Click zone → detent (left / middle / right third).
  tri.addEventListener('click', e => {
    e.stopPropagation();
    const r = tri.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;
    _go(frac < 1 / 3 ? 'off' : (frac < 2 / 3 ? 'discoverable' : 'visible'));
  });
  // Keyboard: ←/↓ step left, →/↑ step right, Home/End jump to the ends.
  tri.addEventListener('keydown', e => {
    const i = ORDER.indexOf(current);
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); _go(ORDER[Math.min(ORDER.length - 1, i + 1)]); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); _go(ORDER[Math.max(0, i - 1)]); }
    else if (e.key === 'Home') { e.preventDefault(); _go('off'); }
    else if (e.key === 'End') { e.preventDefault(); _go('visible'); }
  });

  return tri;
}

/**
 * Build a per-tool 3-position PERMISSION toggle — Deny · Ask · Auto — styled
 * identically to the ability-group `.ac-tri` so the two read as ONE component
 * (this REPLACES the old Auto/Ask/Deny segmented button strip). The three modes
 * map onto the group tri's off/mixed/on detents, so it reuses that CSS verbatim
 * (knob position + colour). Order is least → most permissive, left → right:
 *   • left   third → 'deny' (off)   — the tool is removed from the agent's
 *                                     toolset entirely: it never appears, the
 *                                     agent isn't aware of it, and it cannot be
 *                                     called (backend `load_tools` deletes it).
 *   • middle third → 'ask' (mixed)  — the tool is loaded, but every call pauses
 *                                     for the user's confirmation first.
 *   • right  third → 'auto' (on)    — the tool is loaded and runs without a prompt.
 * Optimistic: flips immediately, shows the shared spinner → ✓ / ⚠ overlay ON TOP
 * of the toggle while `onChange` runs, and reverts if it rejects (throws) OR
 * resolves `false` — matching the existing onPermissionChange contract used by
 * both ability panels. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
 *
 * The returned element carries `_apply(mode)` and `_mode()`.
 *
 * CEILING (global limit): `cfg.ceiling` is the LOOSEST permission the admin
 * allows for this tool (Auto < Ask < Deny in strictness). The agent may match it
 * or be stricter, never looser — a looser pick is clamped UP to the ceiling. The
 * track is NOT decorated for the limit (it keeps the plain 3-position graphic);
 * the greyed, disabled looser-than-ceiling row in the tool-state panel is what
 * shows the limit to the user.
 * `ceiling: 'auto'` (default) = no limit. This is the per-agent enforcement of
 * the admin's per-tool global default; the runtime already caps the effective
 * permission the same way (app/tools/tool_defaults.resolve_permission).
 *
 * @param {object} cfg
 * @param {'deny'|'ask'|'auto'} [cfg.value]  initial mode
 * @param {'deny'|'ask'|'auto'} [cfg.ceiling]  loosest allowed (admin global limit)
 * @param {boolean} [cfg.canEdit]
 * @param {(mode:string, prevMode:string) => (Promise<boolean>|boolean|void)} cfg.onChange
 *        performs the save; reject/throw OR resolve `false` → revert.
 * @returns {HTMLElement} the `.ac-tri` element
 */
export function _buildPermissionTri(cfg) {
  const { value = 'auto', canEdit = true, onChange = _noop, ceiling = 'auto', displayOnly = false } = cfg || {};
  const ORDER = ['deny', 'ask', 'auto'];
  // Strictness rank — higher = stricter. The agent's pick must rank >= the
  // ceiling's rank (i.e. be at least as strict as the admin's global limit).
  const RANK = { auto: 0, ask: 1, deny: 2 };
  const ceilRank = RANK[ceiling] ?? 0;
  // Map the permission mode onto the group tri's off/mixed/on detents so the
  // knob position + colour are shared 1:1: deny→off (left, grey), ask→mixed
  // (middle, amber), auto→on (right, accent on a filled track).
  const _toState = m => (m === 'auto' ? 'on' : (m === 'ask' ? 'mixed' : 'off'));

  const tri = document.createElement('span');
  tri.className = 'ac-tri ac-tri-perm';
  tri.setAttribute('role', 'slider');
  tri.setAttribute('tabindex', canEdit ? '0' : '-1');
  tri.innerHTML = '<span class="ac-tri-knob"></span>';
  // Carry the ceiling on the element as an introspection hook (the old app3.css
  // hatch that consumed it is gone — the panel's greyed row shows the limit now).
  if (ceilRank > 0) tri.dataset.ceiling = ceiling;

  let current = ORDER.includes(value) ? value : 'auto';
  // Never display a value looser than the admin ceiling.
  if ((RANK[current] ?? 0) < ceilRank) current = ceiling;
  const _ceilNote = ceilRank > 0
    ? ` — admin global limit: ${ceiling === 'deny' ? 'Deny (locked)' : 'Ask'}, agents can only be stricter`
    : '';
  const _titleFor = m =>
    'Deny · Ask · Auto — '
    + (m === 'deny' ? 'blocked: the agent never sees this tool'
      : m === 'ask' ? 'allowed, but asks for confirmation before each use'
      : 'allowed, runs automatically')
    + _ceilNote;
  const paint = () => {
    tri.dataset.state = _toState(current);
    tri.title = _titleFor(current);
    tri.setAttribute('aria-valuetext', current);
  };
  paint();

  tri._mode = () => current;
  // Repaint from a new mode WITHOUT saving — a display-only caller (the tool row)
  // drives this from the tool's own state, never from clicks on the tri.
  tri._paint = (m) => { current = ORDER.includes(m) ? m : 'auto'; paint(); };

  // DISPLAY-ONLY — the tri renders the three detents (Deny · Ask · Auto) but OWNS
  // no selection: the tool row's choice is made in the panel this graphic opens,
  // so it must not swallow the click with its own detent zones. The row repaints
  // it from the tool's live permission via `_paint()`.
  if (displayOnly) {
    tri.classList.add('ac-tri-readonly');
    tri.setAttribute('role', 'button');
    tri.setAttribute('aria-haspopup', 'menu');
    tri.removeAttribute('aria-valuetext');
    tri.setAttribute('tabindex', canEdit ? '0' : '-1');
    return tri;
  }

  if (!canEdit) {
    tri.classList.add('ac-tri-disabled');
    tri.setAttribute('aria-disabled', 'true');
    tri.removeAttribute('tabindex');
    return tri;
  }

  let _busy = false;
  async function _go(next) {
    if (_busy || !ORDER.includes(next)) return;
    // Clamp a looser-than-ceiling request UP to the admin ceiling.
    if ((RANK[next] ?? 0) < ceilRank) next = ceiling;
    if (next === current) return;
    const prev = current;
    _busy = true;
    current = next;
    paint();                       // optimistic
    _markSaving(tri);
    let ok = true;
    try {
      const r = onChange(next, prev);
      if (r && typeof r.then === 'function') ok = (await r) !== false;
    } catch (_) { ok = false; }
    if (!ok) { current = prev; paint(); }   // revert — the save didn't land
    _flashSaveCheck(tri, ok);
    _busy = false;
  }
  tri._apply = _go;

  // Click zone → detent (left / middle / right third).
  tri.addEventListener('click', e => {
    e.stopPropagation();
    const r = tri.getBoundingClientRect();
    const frac = (e.clientX - r.left) / r.width;
    _go(frac < 1 / 3 ? 'deny' : (frac < 2 / 3 ? 'ask' : 'auto'));
  });
  // Keyboard: ←/↓ step toward Deny, →/↑ step toward Auto, Home/End jump to ends.
  tri.addEventListener('keydown', e => {
    const i = ORDER.indexOf(current);
    if (e.key === 'ArrowRight' || e.key === 'ArrowUp') { e.preventDefault(); _go(ORDER[Math.min(ORDER.length - 1, i + 1)]); }
    else if (e.key === 'ArrowLeft' || e.key === 'ArrowDown') { e.preventDefault(); _go(ORDER[Math.max(0, i - 1)]); }
    else if (e.key === 'Home') { e.preventDefault(); _go('deny'); }
    else if (e.key === 'End') { e.preventDefault(); _go('auto'); }
  });

  return tri;
}

/**
 * Call lucide.createIcons on all [data-lucide] children of host that lucide
 * hasn't already replaced. Safe to call even if lucide is not loaded yet.
 * Deduplicated from ~15 identical call sites in files.js, chat.js, etc.
 *
 * IDEMPOTENT — the vendored lucide's createIcons IGNORES the `nodes` option and
 * scans every [data-lucide] in the whole document, and the <svg> it generates
 * KEEPS the data-lucide attribute. So a bare call is not a no-op when there is
 * nothing to do: it re-replaces every already-rendered icon, and each
 * replacement is a DOM mutation. Under a MutationObserver (e.g. the git_changes
 * dashboard card's grid observer) that becomes an endless
 * replace → mutate → observer → replace loop that freezes the tab.
 * Two guards make this safe:
 *   1. bail out entirely when no unreplaced placeholders exist in scope;
 *   2. strip the data-lucide marker off the svgs we (re)rendered so a later
 *      call can never match them again.
 */
export function _refreshLucideIcons(host) {
  if (window.lucide && typeof window.lucide.createIcons === 'function') {
    try {
      const scope = host || document;
      const nodes = Array.from(scope.querySelectorAll('[data-lucide]:not(.lucide)'));
      if (!nodes.length) return;
      window.lucide.createIcons({ nodes });
      // The vendored lucide copies data-lucide onto the svgs it renders (and
      // renders them document-wide regardless of `nodes`). Remove the marker so
      // those svgs never match a future [data-lucide] scan again.
      scope.querySelectorAll('svg[data-lucide].lucide').forEach(function (svg) {
        svg.removeAttribute('data-lucide');
      });
    } catch (_) {}
  }
}

// ── No-op stub ─────────────────────────────────────────────────────────────────

/** No-op function for default callback slots. */
export function _noop() {}

// ── "Coming soon" pill badge ────────────────────────────────────────────────────

/** Build a "Coming soon" badge pill element. */
export function _buildComingSoonPill() {
  const pill = document.createElement('span');
  pill.className = 'ac-soon-pill';
  pill.textContent = 'Coming soon';
  return pill;
}

// ── Per-tool row (PERMISSION + VISIBILITY controls) ──────────────────────────────
//
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SISTER-PANEL: AGENT-ABILITY-TABLE — shared per-tool row builder.       ║
// ║  Used by BOTH ability panels (admin-ability-table.js + agent-ability-    ║
// ║  table.js) so the two stay byte-identical. The row is the SAME stacked    ║
// ║  `.ac-ability-row` as a config-setting row (title + control inline,       ║
// ║  description below), so config knobs + tools read as ONE list. PERMISSION ║
// ║  is a 3-position Deny · Ask · Auto tri-toggle (the `.ac-tri` group-toggle ║
// ║  look); VISIBILITY is the eye toggle (Sent/Discover). Styling lives in    ║
// ║  app3.css (`.ac-ability-row` / `.ac-tool-name` + `.ac-tri`), theme-safe   ║
// ║  via design-system tokens, so it adapts to dark + light with no hex.      ║
// ╚══════════════════════════════════════════════════════════════════════════╝

// The irreducible meta tools every agent keeps — never gated. Mirrors
// app/tools/loader.py TIER_1_ALWAYS_ON so a locked tool reads "always on"
// even when the caller's toolMeta forgot to set `locked` (the admin table builds
// tool rows with locked:false). KEEP IN LOCKSTEP with the backend set — the old
// list_tools/search_tools/get_tool_definition names were removed there and
// load_ability/list_skills/load_skill/set_execution_mode added.
const _TOOL_ALWAYS_ON = new Set([
  'load_tool', 'load_ability', 'list_skills', 'load_skill',
  'set_execution_mode',
  'get_time', 'get_date', 'calculate', 'read_attachment', 'register_user',
]);

/**
 * Render the input control for one ability config setting, shared by both
 * ability panels (per-agent + admin) so they stay in lockstep. A `boolean`
 * setting renders as an Enabled/Disabled dropdown (NOT a free-text box — a
 * text box let users type "false" which the backend then read as truthy);
 * `number` renders as a number input; anything else as a text input.
 *
 * @param {object} field   schema entry { key, type, default, ... }
 * @param {*} currentValue the saved value for this key (may be undefined)
 * @param {string} cls      class the collector queries on (e.g. 'ability-setting-field')
 * @param {string} keyAttr  data-attribute holding the key (e.g. 'data-setting-key')
 * @returns {string} HTML string for the control
 */
function _renderSettingInput(field, currentValue, cls, keyAttr) {
  const has = currentValue !== undefined && currentValue !== null && currentValue !== '';
  const raw = has ? currentValue : (field.default ?? '');
  // CEILING data — when a field declares a `ceiling` rule (off/on/max/min), carry
  // it + the admin's app-level value onto the control so the PER-AGENT tree can
  // clamp it (lock a boolean, cap a number). Markup only: the admin table emits
  // the same attributes but ignores them (it IS the ceiling). See agent-ability
  // -table.js's settings-wiring loop. (SISTER-PANEL: AGENT-ABILITY-TABLE.)
  const ceilAttr = field.ceiling
    ? ` data-ceiling="${_esc(String(field.ceiling))}" data-ceiling-value="${_esc(String(field.ceiling_value ?? field.default ?? ''))}"`
    : '';
  // Controls carry the SHARED config-table classes (`.ac-input` + an `.ac-config-*`
  // size class), NOT bespoke inline widths, so an ability's settings render as the
  // SAME neat row controls as the agent card's Config tab (`_cfgRow`). The slot
  // widths live in ui/shared/css/app3.css under `.ac-config-control`.
  if (field.type === 'boolean') {
    // Accept real bools or the string forms the backend coerces.
    const on = (raw === true) ||
      (typeof raw === 'string' && ['1', 'true', 'yes', 'on', 'enabled'].includes(raw.trim().toLowerCase()));
    return `<select class="ac-input ac-input-sm ac-config-sel ${cls}" ${keyAttr}="${_esc(field.key)}"${ceilAttr}>
        <option value="true"${on ? ' selected' : ''}>Enabled</option>
        <option value="false"${on ? '' : ' selected'}>Disabled</option>
      </select>`;
  }
  if (field.type === 'select') {
    // A fixed small set of named choices (e.g. a 3-level posture). Options may be
    // plain strings or {value,label} objects. Renders as the SAME compact select
    // as the boolean control, so it sits flush in the shared config row.
    const opts = Array.isArray(field.options) ? field.options : [];
    const curStr = String(raw);
    const optsHtml = opts.map((o) => {
      const val = (o && typeof o === 'object') ? String(o.value) : String(o);
      const lbl = (o && typeof o === 'object') ? String(o.label ?? o.value) : String(o);
      return `<option value="${_esc(val)}"${val === curStr ? ' selected' : ''}>${_esc(lbl)}</option>`;
    }).join('');
    return `<select class="ac-input ac-input-sm ac-config-sel ${cls}" ${keyAttr}="${_esc(field.key)}"${ceilAttr}>${optsHtml}</select>`;
  }
  if (field.type === 'number') {
    const numAttrs = (field.min != null ? ` min="${_esc(String(field.min))}"` : '')
      + (field.max != null ? ` max="${_esc(String(field.max))}"` : '')
      + (field.step != null ? ` step="${_esc(String(field.step))}"` : '');
    // Number field = a horizontal stepper: back/forward arrows INSIDE the field,
    // the value centred between them (see `.ac-num-stepper` in app3.css).
    _ensureNumStepperWiring();
    const { down, up } = _numStepBtns();
    return `<span class="ac-num-stepper">${down}<input type="number"
                 class="ac-input ac-input-sm ac-config-num ${cls}"
                 ${keyAttr}="${_esc(field.key)}"
                 value="${_esc(String(raw))}"${numAttrs}${ceilAttr}>${up}</span>`;
  }
  if (field.type === 'range') {
    const rangeAttrs = (field.min != null ? ` min="${_esc(String(field.min))}"` : '')
      + (field.max != null ? ` max="${_esc(String(field.max))}"` : '')
      + (field.step != null ? ` step="${_esc(String(field.step))}"` : '');
    const display = String(field.display || 'number');
    _ensureConfigRangeWiring();
    return `<span class="ac-range-control"><input type="range"
                 class="ac-config-range ${cls}"
                 ${keyAttr}="${_esc(field.key)}"
                 data-display="${_esc(display)}"
                 aria-label="${_esc(field.label || field.key)}"
                 value="${_esc(String(raw))}"${rangeAttrs}${ceilAttr}>
              <output class="ac-range-value">${_esc(_rangeDisplay(raw, display))}</output></span>`;
  }
  if (field.type === 'textarea') {
    const rows = Number.isFinite(Number(field.rows)) ? Math.max(2, Math.min(16, Number(field.rows))) : 5;
    return `<textarea class="ac-input ac-input-sm ac-config-key ${cls}" ${keyAttr}="${_esc(field.key)}"` +
      ` rows="${rows}" style="resize:vertical;font-family:monospace;font-size:11px;width:100%;min-width:240px;line-height:1.4;"${ceilAttr}>` +
      _esc(String(raw)) + '</textarea>';
  }
  return `<input type="text"
                 class="ac-input ac-input-sm ac-config-key ${cls}"
                 ${keyAttr}="${_esc(field.key)}"
                 value="${_esc(String(raw))}"${ceilAttr}>`;
}

/**
 * Build an ability's config settings as BARE ROWS (no wrapping list / box) — each
 * setting is the SAME stacked `.ac-ability-row` as an ability row OR a tool row:
 * a bold name + its control on the title line, and its one-line hint dropping to
 * its own full-width line below. The caller drops these rows into the SINGLE
 * unified `.ac-list` that also holds the ability's TOOL rows, so config knobs and
 * tools read as ONE continuous table (not two separate boxes).
 *
 * Used by BOTH ability tables (agent-ability-table.js & admin-ability-table.js)
 * so their expanded settings stay mirrored (SISTER-PANEL: AGENT-ABILITY-TABLE).
 *
 * @param {object} schema  { settings:[{key,label,type,hint,default,...}], note? }
 * @param {object} [current] map of key -> current value (defaults used otherwise)
 * @param {string} cls      class on each input so the caller can collect values
 * @param {string} keyAttr  data-attr holding the field key (e.g. 'data-setting-key')
 * @returns {string} HTML of the row divs ('' when the schema has no settings)
 */
export function _buildSettingsRowsHtml(schema, current, cls, keyAttr) {
  if (!schema || !Array.isArray(schema.settings) || !schema.settings.length) return '';
  const cur = current || {};
  let html = '';
  for (const field of schema.settings) {
    html += `<div class="ac-ability-row ac-config-row">
      <span class="ac-ability-label">
        <span class="ac-ability-name">${_esc(field.label || field.key)}</span>
        ${field.hint ? `<span class="ac-ability-desc">${_esc(field.hint)}</span>` : ''}
      </span>
      <span class="ac-config-control">
        ${_renderSettingInput(field, cur[field.key], cls, keyAttr)}
      </span>
    </div>`;
  }
  return html;
}

/**
 * Build a gate's parameter controls as STACKED rows — the layout gates use on
 * BOTH surfaces: the app-level Gates table (gates-section.js) and an agent's
 * Modes-page gate body (agents/configs/content.js).
 *
 * Each row reads TOP-DOWN, unlike the horizontal `.ac-config-row` the ability
 * tables use:
 *
 *     name          (own line)
 *     control       (next line, full column width)
 *     description   (below the control)
 *
 * Rationale: gate params are mostly MULTILINE guidance boxes. In the shared
 * horizontal row the field name and the box compete for one line, so the name
 * collapsed to a sliver and ellipsized — "Declaration guidance (app-wide)" needs
 * ~207px but was allotted 82px — and the hint, which is the ONLY explanation of
 * what belongs in the box, was clamped to a single line with no way to read it.
 * Stacking gives the name its own line, the box the full column width, and the
 * hint room to be read.
 *
 * The description stays clamped for table rhythm and becomes click-to-expand
 * ONLY when it genuinely overflows — the same interaction `_buildToolRow` gives
 * a tool description. Wire it with `wireGateParamRows`.
 *
 * @param {object} schema  { settings:[{key,label,type,hint,default,min,max,rows}] }
 * @param {object} [current] map of key -> saved value
 * @param {string} cls      class on each control so the caller can collect values
 * @param {string} keyAttr  data-attribute holding the field key
 * @returns {string} HTML of the `.ac-gate-param` rows ('' when there are none)
 */
export function buildGateParamRowsHtml(schema, current, cls, keyAttr) {
  const settings = (schema && Array.isArray(schema.settings)) ? schema.settings : [];
  if (!settings.length) return '';
  const cur = current || {};
  let html = '';
  for (const field of settings) {
    html += `<div class="ac-gate-param">
      <span class="ac-gate-param-name">${_esc(field.label || field.key)}</span>
      <span class="ac-gate-param-control">${_renderSettingInput(field, cur[field.key], cls, keyAttr)}</span>
      ${field.hint ? `<span class="ac-gate-param-desc">${_esc(field.hint)}</span>` : ''}
    </div>`;
  }
  return html;
}

/**
 * Wire the interactive parts of `buildGateParamRowsHtml` rows inside `root`:
 *
 *  - the field NAME focuses its control. The row is a column, so the name cannot
 *    be a wrapping `<label>` the way the old inline row's was — the affordance is
 *    restored explicitly instead;
 *  - a DESCRIPTION that actually overflows becomes click / Enter / Space to
 *    expand, identical to the tool-description interaction in `_buildToolRow`.
 *
 * Idempotent per row (`data-gate-param-wired`), so a caller may run it after
 * every re-render. The overflow test is re-derived from the live layout (see the
 * ResizeObserver note in the body) rather than latched from one frame, so a row
 * wired before it was visible still becomes expandable once it is.
 */
export function wireGateParamRows(root) {
  if (!root) return;
  root.querySelectorAll('.ac-gate-param').forEach((row) => {
    if (row.dataset.gateParamWired === '1') return;
    row.dataset.gateParamWired = '1';
    const name = row.querySelector('.ac-gate-param-name');
    const control = row.querySelector(
      '.ac-gate-param-control input, .ac-gate-param-control textarea, .ac-gate-param-control select',
    );
    // Clicking the name focuses the field (the wrapping <label> the inline row
    // relied on is gone once the name and control are siblings in a column).
    if (name && control) {
      name.addEventListener('click', () => { try { control.focus(); } catch (_) {} });
    }
    const desc = row.querySelector('.ac-gate-param-desc');
    if (!desc) return;
    // Interactive only while the text is genuinely clamped, so short hints stay
    // inert text. The state is RE-DERIVED from the live layout rather than latched
    // by a single frame: measuring once in requestAnimationFrame read 0x0 when the
    // body was filled while its settings page was still `[hidden]`, which marked a
    // genuinely 2-line-clamped hint permanently inert — the description stayed cut
    // off with no way to read it. A ResizeObserver re-checks on every layout /
    // resize, so the affordance appears as soon as the row is actually visible and
    // keeps tracking later width changes.
    let descInteractive = false;
    const toggleDesc = () => {
      const expanded = desc.classList.toggle('is-open');
      desc.setAttribute('aria-expanded', String(expanded));
    };
    const syncDesc = () => {
      // While open the text is unclamped, so there is nothing to measure.
      if (desc.classList.contains('is-open')) return;
      const overflows = desc.scrollHeight > desc.clientHeight + 1;
      if (overflows === descInteractive) return;
      descInteractive = overflows;
      desc.classList.toggle('ac-gate-param-desc-toggle', overflows);
      if (overflows) {
        desc.tabIndex = 0;
        desc.setAttribute('role', 'button');
        desc.setAttribute('aria-expanded', 'false');
      } else {
        desc.removeAttribute('tabindex');
        desc.removeAttribute('role');
        desc.removeAttribute('aria-expanded');
      }
    };
    desc.addEventListener('click', (event) => {
      if (!descInteractive) return;
      event.stopPropagation();
      toggleDesc();
    });
    desc.addEventListener('keydown', (event) => {
      if (!descInteractive) return;
      if (event.key !== 'Enter' && event.key !== ' ') return;
      event.preventDefault();
      event.stopPropagation();
      toggleDesc();
    });
    if (typeof ResizeObserver === 'function') new ResizeObserver(syncDesc).observe(desc);
    requestAnimationFrame(syncDesc);
  });
}

/**
 * Build ONE standard nested row as an ELEMENT — the element twin of
 * `_buildSettingsRowsHtml`: the SAME stacked `.ac-ability-row ac-config-row`
 * (name + control on the title line, an optional description dropping to its own
 * full-width line below, control right-aligned in `.ac-config-control`).
 *
 * For the ability-body sections that are assembled in CODE rather than markup —
 * the credentials form here, plus the provider / SQL / knowledge connection
 * sections in their own modules. Using the shared row makes those sections read
 * as ONE list together with the ability's own settings + tool rows instead of a
 * bespoke mini-table with its own fonts, spacing and alignment.
 *
 * Drop the rows into `buildConfigSectionList()` so they also pick up the shared
 * `.ac-list` hairline dividers (with the list frame stripped).
 *
 * @param {object} opts
 *   { name?, desc?, descTitle?, control?, className?, nameClass? }
 *   `name` / `desc` render as the row's label; `descTitle` supplies the hover
 *   text for a description clamped to one line; `control` is an element appended
 *   to the right-aligned control slot; `className` adds row modifiers (e.g.
 *   'ac-config-empty' for a guidance-only row); `nameClass` styles the name
 *   (e.g. the monospace tool-name class).
 * @returns {HTMLElement}
 */
export function buildConfigRow(opts = {}) {
  const row = document.createElement('div');
  row.className = 'ac-ability-row ac-config-row' + (opts.className ? (' ' + opts.className) : '');
  if (opts.name != null || opts.desc) {
    const label = document.createElement('span');
    label.className = 'ac-ability-label';
    if (opts.name != null) {
      const nameEl = document.createElement('span');
      nameEl.className = 'ac-ability-name' + (opts.nameClass ? (' ' + opts.nameClass) : '');
      nameEl.textContent = String(opts.name);
      nameEl.title = String(opts.name);
      label.appendChild(nameEl);
    }
    if (opts.desc) {
      const descEl = document.createElement('span');
      descEl.className = 'ac-ability-desc';
      descEl.textContent = String(opts.desc);
      // Clamped to one line by default, so keep the full text reachable on hover
      // (the same treatment `_buildToolRow` gives a tool description).
      if (opts.descTitle) descEl.title = opts.descTitle;
      label.appendChild(descEl);
    }
    row.appendChild(label);
  }
  const ctrl = document.createElement('span');
  ctrl.className = 'ac-config-control';
  if (opts.control) ctrl.appendChild(opts.control);
  row.appendChild(ctrl);
  return row;
}

/**
 * The frame-less list `buildConfigRow` rows go into: the shared `.ac-list`
 * hairline dividers between rows, with the box (border / radius / background)
 * stripped by `.ac-config-settings` — exactly what the ability's own settings +
 * tool rows use, so a code-built section is visually the same list.
 * (SISTER-PANEL: AGENT-ABILITY-TABLE.)
 */
export function buildConfigSectionList() {
  const list = document.createElement('div');
  list.className = 'ac-list ac-config-settings';
  return list;
}

/**
 * Build one tool row — the SAME stacked `.ac-ability-row` as a config-setting row
 * (`_buildSettingsRowsHtml`) so config knobs and tools render as ONE unified
 * list: the tool NAME + its controls share the title line, and the tool
 * description (if any) drops to its own full-width line below. The row carries a
 * PERMISSION control (a Deny · Ask · Auto tri-toggle, the `.ac-tri` group-toggle
 * look) and a VISIBILITY control (Sent / Discover eye toggle), both sitting in
 * the shared right-aligned `.ac-config-control` slot so they line up exactly with
 * a config row's field / menu.
 *
 * @param {object} toolMeta
 *   { name, description, mode ("core"|"always"|"discoverable"), locked,
 *     destructive, permMode? ("auto"|"ask"|"deny") }
 * @param {object} opts
 *   { canEdit, toolSkillEditable, showMissingSkill, hasToolSkill, showPerm,
 *     onPermissionChange(name, mode), onVisibilityChange(name, mode) -> Promise }
 * @returns {HTMLElement} the row element
 */
export function _buildToolRow(toolMeta, opts) {
  const { canEdit = false, showPerm = false } = opts || {};
  const onPermissionChange = (opts && opts.onPermissionChange) || _noop;
  const onVisibilityChange = (opts && opts.onVisibilityChange) || (async () => {});

  const row = document.createElement('div');
  row.className = 'ac-ability-row ac-tool-row';

  // Name (+ description) — promoted to flex children of the row by the shared
  // `.ac-ability-label` (display:contents), so the name sits on the title line
  // and the description drops to its own full-width line below it.
  const label = document.createElement('span');
  label.className = 'ac-ability-label';
  // A skill editor is available only when this exact tool has a current Markdown
  // association. The App caller also uses the same association for its purely
  // informational missing-skill chip; per-agent callers leave it disabled.
  const hasToolSkill = opts && opts.hasToolSkill === true;
  const canOpenSkill = !!(opts && opts.abilityId && hasToolSkill);
  const nameEl = document.createElement(canOpenSkill ? 'button' : 'span');
  nameEl.className = 'ac-ability-name ac-tool-name' + (canOpenSkill ? ' ac-tool-skill-link' : '');
  nameEl.textContent = toolMeta.name;
  nameEl.title = canOpenSkill ? `Open ${toolMeta.name} tool skill` : toolMeta.name;
  if (canOpenSkill) {
    nameEl.type = 'button';
    nameEl.addEventListener('click', (event) => {
      event.stopPropagation();
      void openToolSkillEditor(opts.abilityId, toolMeta.name, {
        editable: opts.toolSkillEditable === true,
      });
    });
  }
  // The semantic header keeps the name and optional App-only chip as siblings
  // while `display: contents` preserves the shared stacked-row flex layout.
  const header = document.createElement('span');
  header.className = 'ac-tool-header';
  header.appendChild(nameEl);
  if (opts && opts.showMissingSkill === true && !hasToolSkill) {
    const chip = document.createElement('span');
    chip.className = 'ac-tool-missing-skill-chip';
    chip.textContent = 'No skill';
    chip.title = 'No dedicated Markdown skill is associated with this tool. Add one only when operational guidance would be useful.';
    header.appendChild(chip);
  }
  label.appendChild(header);
  if (toolMeta.description) {
    // The description line is a separate, full-width flex item. The optional
    // App-admin edit button is its sibling, so it stays at the end of the text
    // line without becoming part of the clamped/click-to-expand description.
    const descLine = document.createElement('span');
    descLine.className = 'ac-tool-description-line';
    const descEl = document.createElement('span');
    descEl.className = 'ac-ability-desc ac-tool-description';
    descEl.textContent = toolMeta.description;
    descEl.title = toolMeta.description;
    descLine.appendChild(descEl);
    if (opts && opts.toolDescriptionEditable === true && opts.abilityId) {
      const editButton = document.createElement('button');
      editButton.type = 'button';
      editButton.className = 'ac-tool-description-edit';
      editButton.textContent = 'Edit';
      editButton.title = `Edit description for ${toolMeta.name}`;
      editButton.setAttribute('aria-label', `Edit description for ${toolMeta.name}`);
      editButton.addEventListener('click', (event) => {
        event.stopPropagation();
        void openToolDescriptionEditor(opts.abilityId, toolMeta.name, toolMeta.description, (updated) => {
          toolMeta.description = updated;
          descEl.textContent = updated;
          descEl.title = updated;
        });
      });
      descLine.appendChild(editButton);
    }
    label.appendChild(descLine);

    // Only a genuinely clamped description becomes interactive. This keeps
    // short descriptions inert while making the full canonical tool text
    // available with a click (or Enter/Space) on the text itself.
    requestAnimationFrame(() => {
      if (descEl.scrollHeight <= descEl.clientHeight + 1) return;
      descEl.classList.add('ac-tool-description-toggle');
      descEl.tabIndex = 0;
      descEl.setAttribute('role', 'button');
      descEl.setAttribute('aria-expanded', 'false');
      descEl.setAttribute('aria-label', `Expand description for ${toolMeta.name}`);
      const toggleDescription = () => {
        const expanded = row.classList.toggle('ac-tool-desc-expanded');
        descEl.setAttribute('aria-expanded', String(expanded));
        descEl.setAttribute(
          'aria-label',
          `${expanded ? 'Collapse' : 'Expand'} description for ${toolMeta.name}`,
        );
      };
      descEl.addEventListener('click', (event) => {
        event.stopPropagation();
        toggleDescription();
      });
      descEl.addEventListener('keydown', (event) => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        event.stopPropagation();
        toggleDescription();
      });
    });
  }
  row.appendChild(label);

  // The control slot — the SAME right-aligned `.ac-config-control` a config row
  // uses, so a tool's control and a setting's field/menu land on the same edge.
  const ctrl = document.createElement('span');
  ctrl.className = 'ac-config-control';
  row.appendChild(ctrl);

  // TOOL-STATE-PANEL: the row owns NO interactive toggle. Its only control is a
  // launcher that opens a panel holding the tool's WHOLE state — Permission
  // (Off · Ask · Auto) and, when this caller tunes discovery, Visibility
  // (Discoverable · Visible) — with exactly one selection per section. State is
  // only ever changed by a selection inside that panel, mirroring the ability
  // row's panel-driven selection.
  const permLocked = toolMeta.locked || _TOOL_ALWAYS_ON.has(toolMeta.name);
  if (showPerm && permLocked) {
    const pill = document.createElement('span');
    pill.className = 'agents-tool-always-on';
    pill.textContent = 'always on';
    pill.title = 'Essential tool — always permitted, cannot be gated.';
    ctrl.appendChild(pill);
  }

  // Core tools are always sent, so their state is fixed — no panel at all.
  if (toolMeta.locked) {
    const pill = document.createElement('span');
    pill.className = 'agents-tool-always-on';
    pill.textContent = 'core';
    pill.title = 'Core tool — always sent, cannot be changed.';
    ctrl.appendChild(pill);
    return row;
  }

  // Per-agent discovery only; the admin panel passes showVisibility:false (it
  // sets the ceiling — enable + permission — not discovery).
  const showVisibility = !(opts && opts.showVisibility === false);
  const usePerm = showPerm && !permLocked;
  if (usePerm || showVisibility) {
    ctrl.appendChild(_buildToolStateControl(toolMeta, {
      canEdit,
      showPerm: usePerm,
      showVisibility,
      onPermissionChange,
      onVisibilityChange,
    }));
  }
  return row;
}

/** Open a tool's canonical Markdown mini-skill in the shared attachment editor. */
export async function openToolSkillEditor(abilityId, toolName, { editable = false } = {}) {
  if (!abilityId || !toolName) return;
  const base = '/api/v1/abilities/' + encodeURIComponent(abilityId)
    + '/tools/' + encodeURIComponent(toolName) + '/skill';
  let data;
  try {
    const res = await fetch(base, { headers: { ...authHeaders() } });
    if (!res.ok) throw new Error(`Could not load tool skill (HTTP ${res.status})`);
    data = await res.json();
  } catch (error) {
    window.alert(error && error.message ? error.message : 'Could not load this tool skill.');
    return;
  }
  const canSave = editable && data.editable !== false;
  openTextEditor({
    filename: `${toolName}.skill.md`,
    text: data.content || '',
    metaLabel: `Tool skill · ${abilityId}`,
    onSave: canSave ? async (content) => {
      const res = await fetch(base, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ content }),
      });
      if (!res.ok) {
        let message = `Could not save tool skill (HTTP ${res.status})`;
        try { const body = await res.json(); if (body && body.detail) message = body.detail; } catch (_) {}
        throw new Error(message);
      }
    } : null,
  });
}

/** Open the shared text editor for an App-admin tool-description override. */
export async function openToolDescriptionEditor(abilityId, toolName, description, onSaved = null) {
  if (!abilityId || !toolName) return;
  const base = '/api/v1/abilities/' + encodeURIComponent(abilityId)
    + '/tools/' + encodeURIComponent(toolName) + '/description';
  openTextEditor({
    filename: `${toolName}.description.txt`,
    text: description || '',
    metaLabel: `Tool description · ${abilityId}`,
    onSave: async (content) => {
      const res = await fetch(base, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ description: content }),
      });
      if (!res.ok) {
        let message = `Could not save tool description (HTTP ${res.status})`;
        try { const body = await res.json(); if (body && body.detail) message = body.detail; } catch (_) {}
        throw new Error(message);
      }
      const saved = await res.json();
      const updated = String(saved && saved.description ? saved.description : '');
      if (typeof onSaved === 'function') onSaved(updated);
      invalidateAbilityCatalog();
    },
  });
}

// ── Tool STATE control — a 3-way Permission tri, the whole state in its panel ──
//
// TOOL-STATE-PANEL (SISTER-PANEL: shared by admin-ability-table.js and
// agent-ability-table.js): the single control a tool row puts in its
// `.ac-config-control` slot. It is the 3-position PERMISSION `.ac-tri`
// (Off · Ask · Auto) the row used before — but DISPLAY-ONLY: clicking it opens a
// panel holding the tool's entire state as two sections, exactly one selection
// in each:
//
//     Permission          Visibility
//     ├ Off               ├ Discoverable
//     ├ Ask               └ Visible
//     └ Auto
//     ────────────────────────────────  ← hairline separator
//
// The tri's BORDER carries the tool's visibility (neutral → green once
// discoverable or visible) so the row still reads at a glance, while its knobs
// show the permission. The row owns no interactive toggle: state only ever
// changes via a selection in the panel. Persistence is UNCHANGED: the panel
// funnels into the same `onPermissionChange(toolName, mode)` (mode:
// 'deny'|'ask'|'auto') and `onVisibilityChange(toolName, mode)` (mode:
// 'discoverable'|'visible') contracts, so each table keeps its own endpoint. The
// toggle keeps the shared save language — spinner → ✓ / ⚠ overlay, with revert —
// via `_markSaving` / `_flashSaveCheck`.
const _TOOL_STATE_OFF_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="m5.6 5.6 12.8 12.8"/></svg>';
const _TOOL_STATE_ASK_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="9"/><path d="M9.4 9.2a2.7 2.7 0 1 1 3.7 2.5c-.7.3-1.1.9-1.1 1.6v.3"/><path d="M12 17h.01"/></svg>';
const _TOOL_STATE_AUTO_SVG = '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M13 2 4.1 12.6a1 1 0 0 0 .8 1.6h5.4l-.7 7.8 8.9-10.6a1 1 0 0 0-.8-1.6h-5.4z"/></svg>';

function _buildToolStateControl(toolMeta, opts) {
  const {
    canEdit = false,
    showPerm = true,
    showVisibility = true,
    onPermissionChange = _noop,
    onVisibilityChange = _noop,
  } = opts || {};

  const ORDER = ['deny', 'ask', 'auto'];          // least → most permissive
  const RANK = { auto: 0, ask: 1, deny: 2 };      // higher = stricter
  const ceiling = toolMeta.ceiling || 'auto';     // loosest allowed (admin limit)
  const ceilRank = RANK[ceiling] ?? 0;            // looser picks clamp UP to this

  let perm = ORDER.includes(toolMeta.permMode) ? toolMeta.permMode : 'auto';
  if ((RANK[perm] ?? 0) < ceilRank) perm = ceiling;
  let vis = toolMeta.mode === 'discoverable' ? 'discoverable' : 'visible';

  // ── The control — the shared 3-position PERMISSION tri (Off · Ask · Auto) ───
  // The tool's toggle IS the `.ac-tri` its row used before, so it reads as one
  // component with the ability tables' toggles. It is DISPLAY-ONLY: clicking it
  // opens the panel holding BOTH sections, and the choice is made there. The
  // knobs show the permission; the BORDER carries the visibility — neutral when
  // the tool has no visibility, green once it is discoverable or visible.
  const tri = _buildPermissionTri({
    value: perm,
    ceiling,
    canEdit,
    displayOnly: true,
  });
  tri.classList.add('ac-tool-perm-tri');

  const _permText = m => (m === 'deny' ? 'Off' : (m === 'ask' ? 'Ask' : 'Auto'));
  const _visText = m => (m === 'discoverable' ? 'Discoverable' : 'Visible');
  const _permSvg = m => (m === 'deny' ? _TOOL_STATE_OFF_SVG : (m === 'ask' ? _TOOL_STATE_ASK_SVG : _TOOL_STATE_AUTO_SVG));

  function paint() {
    tri._paint(perm);                                  // knobs = the permission
    // Border = the tool's VISIBILITY. An Off (deny) tool has none — it is neither
    // discoverable nor visible — so it reads neutral grey; Ask/Auto read green.
    tri.dataset.vis = (showVisibility && perm !== 'deny') ? vis : 'none';
    const tip = 'Permission: ' + _permText(perm)
      + (showVisibility ? ' · Visibility: ' + _visText(vis) : '')
      + ' — click to choose';
    tri.title = tip;
    tri.setAttribute('aria-label', tip);
  }
  paint();

  // ── Persist one section's pick. Optimistic, with revert on failure. ────────
  async function _save(kind, next) {
    const isPerm = kind === 'perm';
    const cur = isPerm ? perm : vis;
    if (isPerm && (RANK[next] ?? 0) < ceilRank) next = ceiling;   // clamp to ceiling
    if (next === cur) return true;
    if (isPerm) perm = next; else vis = next;
    paint();                                   // optimistic
    _markSaving(tri);
    let ok = true;
    try {
      const r = isPerm
        ? onPermissionChange(toolMeta.name, next)
        : onVisibilityChange(toolMeta.name, next);
      // Same contract as before: resolve `false` or throw → revert.
      if (r && typeof r.then === 'function') ok = (await r) !== false;
    } catch (_) { ok = false; }
    if (!ok) { if (isPerm) perm = cur; else vis = cur; paint(); }
    _flashSaveCheck(tri, ok);
    return ok;
  }

  // ── The panel body. Rebuilt on every open/repaint from live state, so the
  //    ✓ marks always show exactly one selection per section. ─────────────────
  const _menuItems = () => {
    const out = [];
    if (showPerm) {
      out.push({ info: true, className: 'ac-tool-state-head', label: 'Permission' });
      for (const m of ORDER) {
        out.push({
          iconHtml: _permSvg(m),
          label: _permText(m),
          checked: perm === m,
          // A pick looser than the admin ceiling is not offered (same lock the
          // old tri-toggle showed on its locked detent).
          disabled: !canEdit || (RANK[m] ?? 0) < ceilRank,
          keepOpen: true,
          action: () => _save('perm', m),
        });
      }
    }
    if (showVisibility) {
      if (out.length) out.push({ separator: true });   // the hairline divider
      out.push({ info: true, className: 'ac-tool-state-head', label: 'Visibility' });
      for (const v of ['discoverable', 'visible']) {
        out.push({
          iconHtml: v === 'discoverable' ? _EYE_OFF_SVG : _EYE_ON_SVG,
          label: _visText(v),
          checked: vis === v,
          disabled: !canEdit,
          keepOpen: true,
          action: () => _save('vis', v),
        });
      }
    }
    return out;
  };

  tri.addEventListener('click', (e) => {
    e.stopPropagation();
    if (!canEdit) return;
    const r = tri.getBoundingClientRect();
    // Anchor the panel just under the toggle, right-edge aligned. 180 is the
    // width `openFloatingMenu` clamps against, so the two stay consistent, and
    // the click's own Y keeps the panel beside the detent the user hit.
    const top = e.clientY ? e.clientY + 8 : r.bottom + 4;
    openFloatingMenu(_menuItems, top, Math.max(8, r.right - 180));
  });

  return tri;
}

// ── Eye-on / eye-off visibility toggle (visible ⇄ discoverable) ───────────────
// The single shared control for tool, skill, and ability visibility. Eye-on =
// "visible" (shown/sent now); eye-off = "discoverable" (withheld until loaded).
// Optimistic: flips immediately, reverts if onChange rejects. Theme-safe via CSS
// vars (works in dark + light). Inline SVGs so the icon swap needs no lucide pass.
const _EYE_ON_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/></svg>';
const _EYE_OFF_SVG = '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/></svg>';

export function _buildEyeToggle(cfg) {
  const {
    value = 'visible', canEdit = true, onChange,
    titleVisible = 'Visible', titleDiscoverable = 'Discoverable',
  } = cfg || {};
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'agents-eye-toggle';
  // Plain inline icon — no border/box, so it sits cleanly beside the toggle.
  btn.style.cssText = 'background:none;border:none;border-radius:4px;'
    + 'cursor:pointer;padding:2px 4px;display:inline-flex;align-items:center;justify-content:center;'
    + 'flex-shrink:0;transition:color .15s;';
  let current = value === 'discoverable' ? 'discoverable' : 'visible';

  function paint() {
    const on = current === 'visible';
    btn.innerHTML = on ? _EYE_ON_SVG : _EYE_OFF_SVG;
    btn.title = on ? titleVisible : titleDiscoverable;
    btn.style.color = on ? 'var(--accent)' : 'var(--fg-3)';
    btn.classList.toggle('eye-on', on);
    btn.classList.toggle('eye-off', !on);
  }
  paint();

  if (canEdit && onChange) {
    btn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const prev = current;
      current = current === 'visible' ? 'discoverable' : 'visible';
      paint();
      // Spinner while the visibility save is in flight, then a brief confirm —
      // a green check on success, an orange warning triangle on failure (after
      // which the eye reverts to its prior state) — before settling back to the
      // eye icon. So every ability-tree control shows progress → confirm, the
      // same save language as the toggles/fields. (SISTER-PANEL contract.)
      btn.disabled = true;
      btn.innerHTML = '<span class="agents-spinner"></span>';
      try {
        await onChange(current);
        btn.innerHTML = '<span style="color:var(--success);font-weight:700;font-size:13px;line-height:1;">✓</span>';
        btn.title = 'Saved';
        setTimeout(() => { paint(); btn.disabled = false; }, 850);
      } catch (_) {
        current = prev;
        btn.innerHTML = '<span style="color:var(--warning);font-weight:700;font-size:13px;line-height:1;">⚠</span>';
        btn.title = 'Save failed — reverted';
        setTimeout(() => { paint(); btn.disabled = false; }, 1100);
      }
    });
  } else if (!canEdit) {
    btn.disabled = true;
    btn.style.opacity = '0.6';
    btn.style.cursor = 'default';
  }
  return btn;
}

// ── Status flash beside a toggle — typewriter "Saved" / "Visible" / "Hidden" ──
//
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SISTER-PANEL: AGENT-ABILITY-TABLE — shared toggle-status feedback.       ║
// ║  Types a short status word into a row's `.ac-ability-status` slot (which  ║
// ║  sits left of the toggle, in line with the ability title), holds it, then ║
// ║  erases it left→right and clears. `tone` picks the colour via a class:    ║
// ║  'ok' → green (--success), 'warn' → orange (--warning). Both ability      ║
// ║  tables call this so the on-toggle feedback is identical. Styling lives in ║
// ║  `.ac-ability-status` in app3.css. Mirrors the in-body `_setVisInfo`      ║
// ║  typewriter used for the skill/tool visibility eyes.                      ║
// ╚══════════════════════════════════════════════════════════════════════════╝
/** Type `message` into a status element char-by-char, hold ~1.5s, then erase it.
 *  `tone` is 'ok' (green) or 'warn' (orange). Re-entrant: cancels any flash
 *  already running on the same element. */
export function _flashStatusText(statusEl, message, tone) {
  if (!statusEl) return;
  if (statusEl._flashType) { clearInterval(statusEl._flashType); statusEl._flashType = null; }
  if (statusEl._flashHold) { clearTimeout(statusEl._flashHold); statusEl._flashHold = null; }
  if (statusEl._saveFade) { clearTimeout(statusEl._saveFade); statusEl._saveFade = null; }
  // Drop any save-indicator badge state so this renders as plain text.
  statusEl.classList.remove('ac-status-ind', 'ac-status-saved', 'ac-status-error');
  statusEl.title = '';
  statusEl.classList.remove('tone-ok', 'tone-warn');
  statusEl.classList.add(tone === 'warn' ? 'tone-warn' : 'tone-ok');
  const msg = String(message || '');
  let i = 0;
  statusEl.textContent = '';
  statusEl._flashType = setInterval(() => {               // type in, left → right
    statusEl.textContent = msg.slice(0, ++i);
    if (i >= msg.length) {
      clearInterval(statusEl._flashType); statusEl._flashType = null;
      statusEl._flashHold = setTimeout(() => {            // hold, then erase from the left
        let j = 0;
        statusEl._flashType = setInterval(() => {
          statusEl.textContent = msg.slice(++j);
          if (j >= msg.length) {
            clearInterval(statusEl._flashType); statusEl._flashType = null;
            statusEl.textContent = '';
          }
        }, 16);
      }, 1500);
    }
  }, 24);
}

// ── Unified save-status indicator (SISTER-PANEL: AGENT-ABILITY-TABLE) ─────────
//
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  ONE save-status language for every config control in the ability tree.   ║
// ║  `_markSaving(slot)` shows a spinner in the row's status slot while the    ║
// ║  save is in flight; `_flashSaveCheck(slot, ok)` then settles it to a green ║
// ║  check ✓ (saved) or an orange warning triangle ⚠ (failed — the CALLER     ║
// ║  reverts the control to its prior value). Both toggle the `.ac-status-ind` ║
// ║  badge class (styled in app3.css to match the .agents-autosave-check tick  ║
// ║  the model + appearance tables use). Use these for SAVE confirmations;     ║
// ║  `_flashStatusText` stays for plain text messages (e.g. a locked-on        ║
// ║  ability's "Cannot be deactivated").                                      ║
// ╚══════════════════════════════════════════════════════════════════════════╝
/** Ensure (and cache) the save-overlay that covers `container`, return it. The
 *  overlay centers the spinner / ✓ / ⚠ ON TOP of the control rather than beside
 *  it. The container is made position:relative so the overlay can cover it. Pass
 *  the control's container: the toggle wrap, the `.ac-config-control` slot, the
 *  permission/visibility `.ac-tri`, or a button. */
export function _saveOverlay(container) {
  if (!container) return null;
  if (container._saveOv && container.contains(container._saveOv)) return container._saveOv;
  if (getComputedStyle(container).position === 'static') container.style.position = 'relative';
  const ov = document.createElement('span');
  ov.className = 'ac-save-overlay';
  ov.setAttribute('aria-hidden', 'true');
  container.appendChild(ov);
  container._saveOv = ov;
  return ov;
}

// Minimum time the spinner stays visible, so even an instant save/failure shows
// a perceptible spinner → check/triangle sequence rather than a blink.
const _MIN_SPIN_MS = 400;

/** Show the in-flight spinner OVER `container` (cancels any prior flash/timer). */
export function _markSaving(container) {
  const ov = _saveOverlay(container);
  if (!ov) return;
  if (ov._fade) { clearTimeout(ov._fade); ov._fade = null; }
  if (ov._delay) { clearTimeout(ov._delay); ov._delay = null; }
  ov._savingAt = Date.now();
  ov.classList.remove('ok', 'err');
  ov.classList.add('show');
  ov.title = 'Saving…';
  ov.innerHTML = '<span class="agents-spinner"></span>';
}

/** Settle the overlay OVER `container` to a green check ✓ (ok) or an orange
 *  warning triangle ⚠ (fail), hold, then fade away. `errMsg` is the failure
 *  tooltip. The CALLER reverts the control's value when `ok` is false. */
export function _flashSaveCheck(container, ok, errMsg) {
  const ov = _saveOverlay(container);
  if (!ov) return;
  // Hold the spinner for at least _MIN_SPIN_MS so it's always seen before the
  // check/triangle, even if the save resolved/failed instantly.
  const since = ov._savingAt ? (Date.now() - ov._savingAt) : _MIN_SPIN_MS;
  if (since < _MIN_SPIN_MS) {
    if (ov._delay) clearTimeout(ov._delay);
    ov._delay = setTimeout(() => { ov._savingAt = 0; _flashSaveCheck(container, ok, errMsg); }, _MIN_SPIN_MS - since);
    return;
  }
  if (ov._fade) { clearTimeout(ov._fade); ov._fade = null; }
  ov._savingAt = 0;
  ov.classList.add('show');
  ov.innerHTML = '';
  _clearSavePop(ov);
  if (ok) {
    ov.classList.remove('err'); ov.classList.add('ok');
    ov.textContent = '✓';       // ✓ check
    ov.title = 'Saved';
    ov._fade = setTimeout(() => { ov.classList.remove('show', 'ok'); ov.textContent = ''; ov.title = ''; }, 1600);
  } else {
    ov.classList.remove('ok'); ov.classList.add('err');
    ov.textContent = '⚠';       // ⚠ warning triangle
    ov.title = errMsg || 'Save failed — reverted';
    // Small 1–2 word popup beside the triangle so the failure is unmissable.
    _showSavePop(ov, _shortFailLabel(errMsg));
    ov._fade = setTimeout(() => { ov.classList.remove('show', 'err'); ov.textContent = ''; ov.title = ''; _clearSavePop(ov); }, 2600);
  }
}

/** Condense a failure message to a 1–2 word popup label. */
function _shortFailLabel(errMsg) {
  if (!errMsg) return 'Not saved';
  const m = String(errMsg).toLowerCase();
  if (m.includes('clear')) return 'Not cleared';
  return 'Not saved';
}

/** Pop a tiny warning label next to the overlay (auto-removed by _clearSavePop). */
function _showSavePop(ov, text) {
  _clearSavePop(ov);
  const pop = document.createElement('span');
  pop.className = 'ac-save-pop';
  pop.setAttribute('aria-hidden', 'true');
  pop.textContent = text;
  ov.appendChild(pop);
  ov._pop = pop;
  // Trigger the entrance transition on the next frame.
  requestAnimationFrame(() => { if (ov._pop === pop) pop.classList.add('show'); });
}

/** Remove any popup label attached to the overlay. */
function _clearSavePop(ov) {
  if (ov && ov._pop) {
    if (ov._pop.parentNode) ov._pop.parentNode.removeChild(ov._pop);
    ov._pop = null;
  }
}

// ── Per-ability "settings + tools" unified list ──────────────────────────────
//
// An ability's expanded body shows its config knobs AND its tool rows as ONE
// continuous run of rows — both are the SAME stacked `.ac-ability-row` (title +
// control inline, description below). The rows sit DIRECTLY in the expanded body
// (like the skill row), NOT inside their own bordered "sub-table" box: the list
// keeps the shared `.ac-list` class only for its hairline row dividers, while CSS
// (`.ac-list.ac-config-settings` in app3.css) strips the frame — border, radius,
// fill, clip — so there's no box-in-a-box nesting. The caller fills it config-
// rows-first, then tool rows. (There is no inner collapse / header: the parent
// ability row's own expand gates it.) Styling: the shared `.ac-ability-row` rules
// + the `.ac-config-settings` frame-strip in app3.css.

/** Build the empty unified settings+tools list (frame-stripped) the caller fills. */
export function _buildAbilitySettingsList() {
  const list = document.createElement('div');
  list.className = 'ac-list ac-config-list ac-config-settings';
  return list;
}

// ── Ability-tree loading spinner + staggered group reveal ─────────────────────
//
// ╔══════════════════════════════════════════════════════════════════════════╗
// ║  SISTER-PANEL: AGENT-ABILITY-TABLE — shared loading/reveal helpers.      ║
// ║  Both ability panels (admin + agent) show this spinner while the catalog ║
// ║  loads, then reveal their groups one-by-one via the same stagger helper, ║
// ║  so the entrance animation is identical in both. Styling (`.ac-tree-*`,  ║
// ║  `.ac-group-reveal`, `@keyframes ac-spin/ac-group-reveal`) is in app3.css.║
// ╚══════════════════════════════════════════════════════════════════════════╝

/** Build the centered spinner shown in the ability-tree container while the
 *  catalog (and admin tool-defaults) are loading. */
export function _buildAbilityTreeLoader(label) {
  const wrap = document.createElement('div');
  wrap.className = 'ac-tree-loading';

  const spinner = document.createElement('div');
  spinner.className = 'ac-tree-spinner';
  wrap.appendChild(spinner);

  const lbl = document.createElement('div');
  lbl.className = 'ac-tree-loading-label';
  lbl.textContent = label || 'Loading abilities…';
  wrap.appendChild(lbl);

  return wrap;
}

/** A placeholder occupying the group tri-toggle's footprint (54×20) and showing a
 *  small spinner, used on the per-agent Abilities tab while a group's bulk toggle
 *  status (Off · Mixed · On) is still loading from the per-agent connections
 *  fetch. Swapped for the real `.ac-tri` once that lands, so the row never shifts.
 *  (SISTER-PANEL: AGENT-ABILITY-TABLE — agent tab ONLY: the admin table reads its
 *  enable states from the single catalog fetch, so it has no separate status
 *  phase and renders its tris immediately.) */
export function _buildGroupTriSpinner() {
  const ph = document.createElement('span');
  ph.className = 'ac-tri ac-tri-loading';
  ph.setAttribute('aria-hidden', 'true');
  ph.title = 'Loading status…';
  ph.innerHTML = '<span class="ac-mini-spinner"></span>';
  return ph;
}

/** A compact inline "Loading…" row (small spinner + label) shown inside a group
 *  body while its member cards are built on first expand (agent tab). */
export function _buildGroupBodyLoading(label) {
  const row = document.createElement('div');
  row.className = 'conn-loading ac-group-body-loading';
  row.innerHTML = '<span class="ac-mini-spinner"></span><span>' + _esc(label || 'Loading…') + '</span>';
  return row;
}

// ── Generic credentials form for credential-bearing abilities ────────────────
// Shared by BOTH ability panels (admin Agent Settings + per-agent Abilities tab)
// so a `credentials`-declaring ability renders ONE identical secret-entry form in
// each — the SISTER-PANEL contract, with a single implementation. Fields +
// non-secret values + which secrets are set come from
// GET /api/v1/abilities/{id}/credentials; Save POSTs them; Clear DELETEs. Secrets
// are never echoed back, so a secret field shows a "saved" hint and a blank box.
// The backend resolves the caller from the Bearer token and decides `can_edit`
// from scope (admin-scope → admin only; user/agent-scope → the caller's own).
// This is the common-credential framework (app/abilities/credentials.py).

// Auth-aware fetch (mirrors app-config utils._fetch; this shared file can't
// import that page-local helper).
function _credFetch(url, opts = {}) {
  opts.headers = { ...(opts.headers || {}), ...authHeaders() };
  return fetch(url, opts);
}

function _setCredBadge(badge, configured) {
  badge.textContent = configured ? 'Configured' : 'Not configured';
  badge.style.background = configured ? 'rgba(var(--success-rgb), 0.15)' : 'rgba(var(--danger-rgb), 0.12)';
  badge.style.color = configured ? 'var(--success)' : 'var(--danger)';
}

/**
 * Build the credentials entry form for an ability that declares a `credentials`
 * block. Returns a wrapper element immediately; it fetches and fills itself, and
 * self-removes when the ability declares no credential fields (so callers can add
 * it unconditionally).
 *
 * @param {string} abilityId
 * @param {object} [opts]
 * @param {function} [opts.onSaved] - called after a successful Save/Clear so the
 *        caller can invalidate its catalog cache (the toggle's configured-state
 *        depends on it).
 * @param {function} [opts.onRendered] - called after credential rows are loaded.
 * @param {string}   [opts.agentId] - per-agent scope target (user-scope ignores it).
 */
export function buildCredentialsSection(abilityId, opts = {}) {
  const onSaved = opts.onSaved || _noop;
  const onRendered = opts.onRendered || _noop;
  const agentId = opts.agentId || null;

  const wrap = document.createElement('div');
  wrap.className = 'ac-cred-section';
  wrap.style.cssText = 'margin:8px 0;';
  wrap.innerHTML = '<div class="conn-loading" style="padding:8px;font-size:11px;">Loading credentials…</div>';
  // Clicks inside never toggle the row/group.
  wrap.addEventListener('click', (e) => e.stopPropagation());

  (async () => {
    let data = null;
    try {
      const res = await _credFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/credentials');
      data = res.ok ? await res.json() : null;
    } catch (_) { data = null; }
    if (!data || !Array.isArray(data.fields) || !data.fields.length) { wrap.remove(); return; }

    // A host can impose a stricter read-only surface than the credential's own
    // scope (the All Agents global table does this for every non-app-admin).
    const canEdit = opts.canEdit !== false && data.can_edit !== false;
    const values = data.values || {};
    const secretsSet = data.secrets_set || {};

    wrap.innerHTML = '';
    const badge = document.createElement('span');
    badge.className = 'ac-cred-badge';
    badge.style.cssText = 'font-size:10px;padding:2px 8px;border-radius:10px;';
    _setCredBadge(badge, !!data.configured);

    // The form is a STANDARD nested config list, not a bespoke mini-table: one
    // `.ac-ability-row` per field (label + control) plus a heading row carrying the
    // section name and its configured-state badge, so the credentials form reads as
    // part of the same list as the ability's own settings + tool rows. The inputs
    // carry the shared `.ac-config-*` size classes for the same reason.
    const list = buildConfigSectionList();
    list.appendChild(buildConfigRow({ name: 'Credentials', control: badge }));

    const inputs = {};
    for (const f of data.fields) {
      let input;
      if (f.type === 'select' && Array.isArray(f.options)) {
        input = document.createElement('select');
        input.className = 'ac-input ac-input-sm ac-config-sel';
        for (const opt of f.options) {
          const o = document.createElement('option');
          o.value = opt; o.textContent = opt;
          input.appendChild(o);
        }
        input.value = values[f.key] || f.default || f.options[0];
      } else if (f.type === 'textarea') {
        input = document.createElement('textarea');
        input.className = 'ac-input ac-input-sm ac-config-key';
        input.rows = 4;
        input.style.cssText = 'resize:vertical;font-family:monospace;font-size:11px;width:100%;';
        if (!f.secret) input.value = values[f.key] || '';
      } else {
        input = document.createElement('input');
        input.type = f.secret ? 'password' : (f.type === 'number' ? 'number' : 'text');
        input.className = 'ac-input ac-input-sm ac-config-key';
        input.autocomplete = 'off';
        if (!f.secret) input.value = values[f.key] || '';
      }
      if (f.placeholder) input.placeholder = f.placeholder;
      if (f.secret && secretsSet[f.key]) input.placeholder = 'saved — leave blank to keep';
      if (!canEdit) input.disabled = true;
      inputs[f.key] = input;

      list.appendChild(buildConfigRow({
        name: f.label || f.key,
        desc: f.hint || '',
        control: input,
      }));
    }

    if (!canEdit) {
      list.appendChild(buildConfigRow({
        desc: 'Only an admin can edit these credentials.',
        className: 'ac-config-empty',
      }));
    }
    wrap.appendChild(list);
    if (!canEdit) { onRendered(); return; }

    const actions = document.createElement('div');
    actions.style.cssText = 'display:flex;gap:8px;align-items:center;margin-top:8px;';
    const saveBtn = document.createElement('button');
    saveBtn.className = 'ac-btn ac-btn-primary';
    saveBtn.textContent = 'Save';
    const clearBtn = document.createElement('button');
    clearBtn.className = 'ac-btn ac-btn-ghost';
    clearBtn.textContent = 'Clear';
    clearBtn.style.color = 'var(--danger)';
    // The shared spinner → ✓ / ⚠ indicator overlays ON TOP of the clicked button
    // (the same save language as every other config control).
    actions.appendChild(saveBtn);
    actions.appendChild(clearBtn);
    wrap.appendChild(actions);
    onRendered();

    saveBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      const payload = { values: {} };
      for (const [k, el] of Object.entries(inputs)) payload.values[k] = el.value;
      if (agentId) payload.agent_id = agentId;
      _markSaving(saveBtn);
      saveBtn.disabled = true;
      try {
        const res = await _credFetch('/api/v1/abilities/' + encodeURIComponent(abilityId) + '/credentials', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        const out = res.ok ? await res.json() : null;
        if (out && out.status === 'ok') {
          _setCredBadge(badge, !!out.configured);
          onSaved();  // let the caller refresh its catalog cache
          _flashSaveCheck(saveBtn, true);
        } else {
          _flashSaveCheck(saveBtn, false, (out && out.detail) || 'Save failed');
        }
      } catch (_) { _flashSaveCheck(saveBtn, false); }
      finally { saveBtn.disabled = false; }
    });

    clearBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      _markSaving(clearBtn);
      clearBtn.disabled = true;
      const url = '/api/v1/abilities/' + encodeURIComponent(abilityId) + '/credentials'
        + (agentId ? ('?agent_id=' + encodeURIComponent(agentId)) : '');
      try {
        await _credFetch(url, { method: 'DELETE' });
        for (const el of Object.values(inputs)) { if (el.tagName !== 'SELECT') el.value = ''; }
        _setCredBadge(badge, false);
        _flashSaveCheck(clearBtn, true);
        onSaved();
      } catch (_) { _flashSaveCheck(clearBtn, false, 'Clear failed'); }
      finally { clearBtn.disabled = false; }
    });
  })();

  return wrap;
}

/** Reveal each `.ac-group` in `grid` one-by-one, each unfurling vertically with
 *  a small per-group delay. The inline delay + class are cleaned up on
 *  animation end so later re-renders and the search filter (which toggles
 *  `.expanded`) are unaffected. `stepMs` defaults to 70ms between groups. */
export function _staggerRevealGroups(grid, stepMs) {
  if (!grid) return;
  const step = typeof stepMs === 'number' ? stepMs : 70;
  const groups = grid.querySelectorAll('.ac-group');
  groups.forEach((g, i) => {
    g.style.animationDelay = (i * step) + 'ms';
    g.classList.add('ac-group-reveal');
    g.addEventListener('animationend', function _done() {
      g.style.animationDelay = '';
      g.classList.remove('ac-group-reveal');
      g.removeEventListener('animationend', _done);
    });
  });
}

// ── Ability-table search / chat pill (shared by both ability tables) ───────────
//
// One hybrid control sits above the ability table in BOTH the per-agent Abilities
// tab (agent-ability-table.js) and the admin Agent Settings table
// (admin-ability-table.js) — keeping the SISTER-PANELs mirrored. It does double
// duty:
//   • TYPE  → live SEARCH. Every keystroke calls `onFilter(query)` (the table's
//             existing filter), exactly like the old plain search box.
//   • SEND / ATTACH → CHAT. Clicking send (or attaching a file and sending, or
//             pressing Enter) calls `onChatSend({ text, attachmentIds })`, then
//             clears the field — which restores the full, unfiltered list.
//
// It opts into the shared `.chat-pill .chat-pill-1line` design (geometry/buttons
// in app1.css; the floating-glass skin for `.ability-search-pill` lives in
// index.css alongside the other floating pills). Attachments are isolated to this
// pill via their own pending array (never the global composer's), and voice
// dictation lands in this pill's own input.

/**
 * Build the hybrid ability search/chat pill into `container`.
 * @param {HTMLElement} container
 * @param {object} cfg
 * @param {function} cfg.onFilter   - (query:string) => void, run on every keystroke
 * @param {function} cfg.onChatSend - ({text, attachmentIds}) => void, run on send
 * @param {string}  [cfg.placeholder]
 * @returns {{wrap:HTMLElement, pill:HTMLElement, input:HTMLElement, focus:function}}
 */
export function buildAbilitySearchPill(container, cfg = {}) {
  const { onFilter, onChatSend } = cfg;
  const chatEnabled = cfg.chatEnabled !== false;

  const wrap = document.createElement('div');
  wrap.className = 'conn-search-wrap ability-search-pill-wrap';

  const pill = document.createElement('div');
  pill.className = chatEnabled
    ? 'chat-pill chat-pill-1line ability-search-pill'
    : 'ability-search-field';

  const attachBtn = document.createElement('button');
  attachBtn.type = 'button';
  attachBtn.className = 'chat-pill-attach';
  attachBtn.title = 'Attach a file (switches to chat)';
  attachBtn.innerHTML = _iconHtml('plus', '18px');

  const input = document.createElement('textarea');
  input.className = 'chat-pill-input';
  input.rows = 1;
  input.placeholder = cfg.placeholder || (chatEnabled ? 'Search abilities, or ask WebAgent…' : 'Search abilities…');
  input.autocomplete = 'off';

  const voiceBtn = document.createElement('button');
  voiceBtn.type = 'button';
  voiceBtn.className = 'chat-pill-voice';
  voiceBtn.title = 'Voice dictation';
  voiceBtn.innerHTML = _iconHtml('mic', '18px');

  const sendBtn = document.createElement('button');
  sendBtn.type = 'button';
  sendBtn.className = 'chat-pill-send';
  sendBtn.title = 'Ask WebAgent';
  sendBtn.innerHTML = _iconHtml('send', '16px');

  if (chatEnabled) pill.appendChild(attachBtn);
  pill.appendChild(input);
  if (chatEnabled) {
    pill.appendChild(voiceBtn);
    pill.appendChild(sendBtn);
  }
  wrap.appendChild(pill);

  // Isolated attachment preview bar + pending list (never the global composer's).
  const previewBar = document.createElement('div');
  previewBar.className = 'chat-preview-bar ability-search-preview';
  previewBar.style.display = 'none';
  wrap.appendChild(previewBar);

  const fileInput = document.createElement('input');
  fileInput.type = 'file';
  fileInput.multiple = true;
  fileInput.style.display = 'none';
  wrap.appendChild(fileInput);

  const pending = [];
  const updateArmed = () => {
    if (!chatEnabled) return;
    const armed = input.value.trim().length > 0 || pending.length > 0;
    pill.classList.toggle('has-text', armed);   // swaps the mic glyph → send
  };
  const uploadOpts = {
    previewBar,
    pending,
    onChange: () => {
      previewBar.style.display = pending.length ? 'flex' : 'none';
      updateArmed();
    },
  };

  // TYPE → live search (and arm/disarm send).
  input.addEventListener('input', () => {
    if (typeof onFilter === 'function') onFilter(input.value);
    updateArmed();
  });

  // SEND → chat. Enter sends (Shift+Enter = newline), mirroring every chat pill.
  const doSend = () => {
    const text = input.value.trim();
    const attachmentIds = pending.map(a => a.attachment_id).filter(Boolean);
    if (!text && !attachmentIds.length) return;
    if (typeof onChatSend === 'function') onChatSend({ text, attachmentIds });
    // Reset → also restores the full (unfiltered) list via onFilter('').
    input.value = '';
    pending.length = 0;
    previewBar.innerHTML = '';
    previewBar.style.display = 'none';
    updateArmed();
    if (typeof onFilter === 'function') onFilter('');
    input.focus();
  };
  if (chatEnabled) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); doSend(); }
    });
    sendBtn.addEventListener('click', doSend);
  }

  // ATTACH → isolated upload (own picker + drag/paste), routed to this pill's
  // pending list so the files ride the chat-widget's first message, not the
  // main composer.
  if (chatEnabled) attachBtn.addEventListener('click', () => fileInput.click());
  if (chatEnabled) fileInput.addEventListener('change', async () => {
    const files = Array.from(fileInput.files);
    fileInput.value = '';
    for (const file of files) await uploadAndPreview(file, uploadOpts);
  });
  if (chatEnabled) wireChatPillUploads(pill, input, uploadOpts);

  // VOICE → native recognition or the cross-browser recorder fallback. Where
  // NO voice path can work (insecure context / unsupported browser), degrade
  // to send-only via the shared no-voice hook instead of an erroring mic.
  if (chatEnabled && !isVoiceInputSupported()) {
    pill.classList.add('no-voice');
  } else if (chatEnabled) {
    voiceBtn.addEventListener('click', () => startSpeechDictation(voiceBtn, input));
  }

  container.appendChild(wrap);

  // Render the icon placeholders we just inserted (scoped + :not(.lucide) guard,
  // per the Lucide rules in ui/shared/js/icons.js — never a bare createIcons()).
  if (typeof lucide !== 'undefined' && lucide.createIcons) {
    const nodes = Array.from(pill.querySelectorAll('[data-lucide]:not(.lucide)'));
    if (nodes.length) {
      lucide.createIcons({ nodes });
      // Vendored lucide copies data-lucide onto the svg it renders — strip it so
      // a later [data-lucide] scan (a MutationObserver call site, e.g. the
      // git_changes dashboard card) never re-replaces this icon in a loop.
      pill.querySelectorAll('svg[data-lucide].lucide').forEach(function (svg) {
        svg.removeAttribute('data-lucide');
      });
    }
  }

  return { wrap, pill, input, focus: () => input.focus() };
}


// ── Floating menu (shared) ────────────────────────────────────────────────
// Generic anchored popup menu used by the File Manager and Terminal drop-ins
// (tab "more" menus, tree/terminal context menus, the production tools popover).
// Hoisted out of the old files.js so the two views share it as infrastructure
// rather than sharing code with each other. `items` is an array of:
//   { icon, label, action, danger?, checked?, disabled? }  — a button
//   { separator: true }                                     — a divider
//   { info: true, label, id? }                              — a non-interactive line
//   { field: true, label?, value?, placeholder?, fieldType?, fieldKey?, onInput?, onSave? }
export function closeFloatingMenu() {
  const m = document.getElementById('files-floating-menu');
  if (m) m.remove();
}

export function openFloatingMenu(items, top, left) {
  closeFloatingMenu();

  const menu = document.createElement('div');
  menu.className = 'files-tab-menu';
  menu.id = 'files-floating-menu';

  // `items` may be a FACTORY rather than a plain array: a multi-section panel
  // (e.g. the tool Permission + Visibility picker) repaints itself in place
  // after each selection, so the factory is re-invoked on every open/repaint to
  // read the live values.
  const list = (typeof items === 'function' ? items() : items) || [];

  for (const item of list) {
    if (item.separator) {
      const hr = document.createElement('div');
      hr.className = 'files-tab-menu-sep';
      menu.appendChild(hr);
      continue;
    }
    if (item.info) {
      const row = document.createElement('div');
      // `className` lets a section header opt out of the info row's fixed
      // min-width (used by the tool-state panel's section labels).
      row.className = 'files-tab-menu-info' + (item.className ? ' ' + item.className : '');
      if (item.id) row.id = item.id;
      row.textContent = item.label || '';
      menu.appendChild(row);
      continue;
    }
    if (item.field) {
      const wrap = document.createElement('div');
      wrap.className = 'files-tab-menu-field';
      if (item.label) {
        const lab = document.createElement('label');
        lab.textContent = item.label;
        wrap.appendChild(lab);
      }
      const inp = document.createElement('input');
      inp.type = item.fieldType || 'text';
      inp.value = item.value || '';
      inp.placeholder = item.placeholder || '';
      inp.spellcheck = false;
      inp.autocomplete = item.fieldType === 'password' ? 'new-password' : 'off';
      if (item.fieldKey) inp.dataset.field = item.fieldKey;
      inp.addEventListener('click', (e) => e.stopPropagation());
      if (typeof item.onInput === 'function') {
        inp.addEventListener('input', () => item.onInput(inp.value));
      }
      if (typeof item.onSave === 'function') {
        inp.addEventListener('change', () => item.onSave(inp.value));
        inp.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') { e.preventDefault(); item.onSave(inp.value); inp.blur(); }
        });
      }
      wrap.appendChild(inp);
      menu.appendChild(wrap);
      continue;
    }
    const btn = document.createElement('button');
    btn.className = 'files-tab-menu-item' + (item.danger ? ' danger' : '') + (item.checked ? ' checked' : '');
    btn.type = 'button';
    btn.disabled = !!item.disabled;
    // An item may ship its own inline SVG (`iconHtml`) instead of a lucide name.
    // The ability tables' controls use inline SVGs so they never depend on a
    // lucide pass having run on the page.
    let i;
    if (item.iconHtml) {
      i = document.createElement('span');
      i.className = 'files-tab-menu-icon';
      i.setAttribute('aria-hidden', 'true');
      i.innerHTML = item.iconHtml;
    } else {
      i = document.createElement('i');
      i.setAttribute('data-lucide', item.icon || 'circle');
      i.className = 'lucide-icon';
    }
    btn.appendChild(i);
    const lbl = document.createElement('span');
    lbl.textContent = item.label;
    btn.appendChild(lbl);
    const check = document.createElement('span');
    check.className = 'files-tab-menu-check';
    check.textContent = '\u2713';
    btn.appendChild(check);
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const actionable = !item.disabled && typeof item.action === 'function';
      // A `keepOpen` item belongs to a MULTI-SECTION panel: the user still has
      // another section to set, so the panel stays up and repaints in place (the
      // ✓ marks follow the new selection) instead of dismissing on the first pick.
      if (actionable && item.keepOpen) {
        const r = item.action();
        const repaint = () => {
          // Only repaint if the user hasn't already dismissed the panel.
          if (!document.getElementById('files-floating-menu')) return;
          openFloatingMenu(items, top, left);
        };
        repaint();                                        // optimistic
        if (r && typeof r.then === 'function') r.then(repaint, repaint);   // settle/revert
        return;
      }
      closeFloatingMenu();
      if (actionable) item.action();
    });
    menu.appendChild(btn);
  }

  document.body.appendChild(menu);

  const menuWidth = 180;
  const rect = menu.getBoundingClientRect();
  const menuHeight = rect.height || 8;
  const clampedTop  = Math.max(8, Math.min(window.innerHeight - menuHeight - 8, top));
  const clampedLeft = Math.max(8, Math.min(window.innerWidth  - menuWidth  - 8, left));
  menu.style.top  = clampedTop + 'px';
  menu.style.left = clampedLeft + 'px';

  _refreshLucideIcons(menu);

  // Dismiss handlers test the LIVE menu (by id), not this call's closure, so a
  // stale handler self-heals instead of closing a freshly-reopened menu.
  const cleanup = () => {
    document.removeEventListener('mousedown', outside, true);
    document.removeEventListener('contextmenu', outside, true);
    document.removeEventListener('keydown', onKey, true);
  };
  const outside = (ev) => {
    const cur = document.getElementById('files-floating-menu');
    if (!cur) { cleanup(); return; }
    if (!cur.contains(ev.target)) { closeFloatingMenu(); cleanup(); }
  };
  const onKey = (ev) => {
    if (!document.getElementById('files-floating-menu')) { cleanup(); return; }
    if (ev.key === 'Escape') { closeFloatingMenu(); cleanup(); }
  };
  setTimeout(() => {
    document.addEventListener('mousedown', outside, true);
    document.addEventListener('contextmenu', outside, true);
    document.addEventListener('keydown', onKey, true);
  }, 0);
}

// ── Keep the mobile keyboard open while tapping footer controls ──────────────
// On touch devices a tap on any button blurs the focused chat pill and
// dismisses the keyboard. The mode / model / abilities / stop / continue /
// suggestion controls are typing-adjacent interactions — the user reaches for
// them mid-sentence and expects to keep typing. Canceling the focus-shift
// default action of pointerdown / mousedown (the click still fires and runs
// the control) keeps focus on the pill so the keyboard stays open.
// Both events are needed: mobile browsers shift focus on pointerdown;
// desktop browsers shift focus on mousedown.
//
// scope        — element to listen on (capture). Pass `document` plus an
//                `areaSelector` when the interactive area includes floating
//                panels mounted on <body> (e.g. the model / skills pickers).
// inputEl      — the chat pill textarea that must keep focus.
// areaSelector — optional CSS selector: only button-like taps matching this
//                area are intercepted (default: the scope itself).
export function keepPillFocusOnFooterTap(scope, inputEl, areaSelector) {
  if (!scope || !inputEl) return;
  const isEditable = (el) => {
    if (!el || el.nodeType !== 1) return false;
    const tag = el.tagName.toLowerCase();
    return tag === 'input' || tag === 'textarea' || tag === 'select' || !!el.isContentEditable;
  };
  const guard = (e) => {
    // Only while the pill holds focus — i.e. the keyboard is up.
    if (document.activeElement !== inputEl) return;
    const t = e.target;
    // Editable fields (e.g. a picker's search box) legitimately take focus.
    if (isEditable(t)) return;
    if (!t || typeof t.closest !== 'function') return;
    if (areaSelector && !t.closest(areaSelector)) return;
    if (!t.closest('button, [role="button"], a, [tabindex], .cml-item, .csp-item, .cmp-item, .chat-footer-handle')) return;
    // Stop the focus-shift default action; the click still runs the control.
    e.preventDefault();
  };
  scope.addEventListener('pointerdown', guard, true);
  scope.addEventListener('mousedown', guard, true);
}

// ── Body-mounted kebab popups: keep the panel inside the viewport ────────────
/**
 * Position a body-mounted, `position: fixed` popup next to the element that
 * opened it (a row's ⋯ kebab) without ever letting it leave the viewport.
 *
 * The popup opens just below the anchor, or just above it when that fits
 * better. When neither side has room — a short window, or a mobile viewport
 * shrunk by the on-screen keyboard — it anchors to the edge with more space and
 * caps its own height so every row stays reachable by scrolling. Measuring
 * against `window.visualViewport` (not `window.innerHeight`) is what stops a
 * keyboard-shrunk viewport from pushing the panel off the top of the screen.
 *
 * Call AFTER the popup has been appended to the document, so offsetWidth /
 * offsetHeight are measurable. Shared by every `.session-row-actions` "more"
 * panel — the chat session dropdown (elements/session-dropdown/controller.js),
 * the Agents → Sessions page copy (DESIGN SYNC CONTRACT) and the genui page-row
 * actions (genui-toolbar.js) — which must all stay in lockstep.
 *
 * @param {HTMLElement} popup    the popup, already appended to the document
 * @param {HTMLElement} anchor   the element it hangs off (its kebab button)
 * @param {{gap?:number, margin?:number, minHeight?:number}} [opts]
 *        gap       distance from the anchor (default 4)
 *        margin    minimum distance kept from each viewport edge (default 4)
 *        minHeight floor for the height cap on very short viewports (default 48)
 */
export function positionKebabPopup(popup, anchor, opts = {}) {
  if (!popup || !anchor) return;
  const gap = opts.gap == null ? 4 : opts.gap;
  const margin = opts.margin == null ? 4 : opts.margin;
  const minHeight = opts.minHeight == null ? 48 : opts.minHeight;

  // visualViewport tracks the on-screen keyboard; innerWidth/Height do not.
  const vv = window.visualViewport;
  const vTop = vv ? vv.offsetTop : 0;
  const vLeft = vv ? vv.offsetLeft : 0;
  const vRight = vLeft + (vv ? vv.width : window.innerWidth);
  const vBottom = vTop + (vv ? vv.height : window.innerHeight);

  const kb = anchor.getBoundingClientRect();
  const pw = popup.offsetWidth;
  const ph = popup.offsetHeight;

  // Horizontal: right-align to the anchor (the kebab sits at a row's right
  // edge), then clamp inside the viewport.
  let left = kb.right - pw;
  if (left + pw > vRight - margin) left = vRight - pw - margin;
  if (left < vLeft + margin) left = vLeft + margin;

  // Vertical: prefer below the anchor; flip above only when the popup genuinely
  // fits there. If neither side fits, anchor to whichever edge leaves more room.
  // `top` must never end up above the viewport top.
  let top = kb.bottom + gap;
  if (top + ph > vBottom - margin) {
    const above = kb.top - ph - gap;
    if (above >= vTop + margin) {
      top = above;
    } else if (kb.top - vTop >= vBottom - kb.bottom) {
      top = vTop + margin;
    } else {
      top = kb.bottom + gap;
    }
  }
  if (top < vTop + margin) top = vTop + margin;

  // Cap the height so the whole panel is reachable (scrolls) rather than being
  // pushed past the viewport edge.
  const maxHeight = (vBottom - margin) - top;
  if (ph > maxHeight) {
    popup.style.maxHeight = Math.max(minHeight, maxHeight) + 'px';
    popup.style.overflowY = 'auto';
  }

  popup.style.left = left + 'px';
  popup.style.top = top + 'px';
}

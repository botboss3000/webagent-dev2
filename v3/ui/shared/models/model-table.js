'use strict';

import { makeRowsReorderable } from './ordering.js';
import { tipBadge } from './field-tip.js';
import { copyText } from './clipboard.js';
import { icon } from './icons.js';
import { advanceDeleteBtn, resetDeleteBtn } from './delete-control.js';
import { modelAddGuard } from './model-add-guard.js';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'.

/**
 * Shared LLM model-table component — the "Models" configurator + saved-models
 * list (Standard / Premium / Vision / Image capability columns + token usage) used in TWO places:
 *
 *   • Admin → Agent Settings → Models   (ui/admin-tools/instances/app-config/agent-settings/agent-settings.js)
 *   • Agent card → Config tab → LLM     (ui/main-panel/agents/js/tab-config.js)
 *
 * ╔══════════════════════════════════════════════════════════════════════════╗
 * ║  SISTER-PANEL: MODEL-TABLE — these two mounts MUST stay mirrored in look   ║
 * ║  and behaviour. They are the SAME component (this file). Change the table  ║
 * ║  here and BOTH update together; only the data source differs (the admin    ║
 * ║  default-provider config vs. a per-agent llm_config blob), injected as an   ║
 * ║  `adapter`. Don't fork a second copy. Grep this banner to find both mounts. ║
 * ╚══════════════════════════════════════════════════════════════════════════╝
 *
 * The markup (configurator rows + advanced row) is built here, not in HTML, so
 * the single source of truth is one file. All DOM refs are instance-scoped
 * closures (NO document.getElementById) so two instances never collide on ids.
 * Styling reuses the app-wide `.ac-*` model-table classes in ui/shared/css/app3.css.
 *
 * Contract — mountModelTable(host, opts) → { reload(), destroy(), getState() }
 *   opts.adapter (required):
 *     loadConfig()              → { provider, base_url, api_key,
 *                                   text_capable, image_capable, image_out_capable,
 *                                   voice_capable, video_capable, providers:[...] }   (the saved roster)
 *     saveRoster({providers})   → persist the saved-models list
 *     saveSingle?(single)       → optional configurator draft persistence
 *     loadPresets?()            → { key: {name, base_url} }     (default: /admin/settings/providers)
 *     fetchModels?({provider, api_key, base_url}) → { models:[...], error? }
 *     fetchModelInfo?(model)    → { info }                       (default: /admin/settings/model-info)
 *     fetchModelUsage?({model, provider}) → usage                (default: /admin/settings/model-usage)
 *   opts.fetchFn   — fetch-like (default window.fetch); admin passes its _fetch
 *   opts.apiPath   — path mapper (default identity); admin passes apiPath
 *   opts.advancedExtra(bodyEl) — optional: append extra controls to the advanced body
 *   opts.onModelChange()       — optional: fired after a model add/clear (e.g. refresh footer)
 *   opts.onRoster(providers)   — optional: fired after every roster load with the
 *                                normalised rows, so a sibling chart can label its
 *                                model series by display name (MODEL-LEGEND-ALIAS)
 *   opts.onModelFocus(model)   — optional: fired with the model id of the saved row
 *                                the user just opened, and again with '' once no row
 *                                is open, so a sibling chart can show that one
 *                                model's series instead of the whole group
 *                                (MODEL-ROW-FOCUS)
 *   opts.loadDirectives?()     — optional: resolve the admin's roster-role prompt
 *                                directives → {standard:'', premium:'', ...} (grep
 *                                ROLE-DIRECTIVE-INJECT). Absent (or a rejected
 *                                promise) hides the "Prompt injections" strip.
 *   opts.saveDirectives?(dict) — optional: persist the full directives dict (admin
 *                                only). Absent ⇒ the editor opens read-only.
 *   opts.directivesEditable?   — bool: whether the directive editor may save.
 *   opts.directivePresentation? — 'popup' (default) or 'inline'. The Agent Config
 *                                mount uses frameless inline rows below its model table;
 *                                sister mounts retain popup chips.
 */

// Unified save-status indicator (spinner → green ✓ / orange ⚠ + popup, centered
// ON TOP of the clicked control). Shared with the ability tree + appearance panel
// so every saved-model box speaks the same save language. SISTER-PANEL note in
// dom-utils.js. (dom-utils.js is the shared utility hub — importing it is the
// sanctioned alternative to cloning these helpers, NOT a cross-page dependency.)
import { _markSaving, _flashSaveCheck } from './dom-utils.js';

// ── tiny local helpers (self-contained — no cross-page imports) ──────────────
// (These lowercase markSaving/flashCheck drive the configurator's side-slot tick
// — the `.agents-autosave-check` span beside Provider / URL / Key / Add. The
// imported `_markSaving`/`_flashSaveCheck` above drive the on-top overlay used by
// the saved-models capability boxes + Default radio.)
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}
function makeCheck() {
  const el = document.createElement('span');
  el.className = 'agents-autosave-check';
  el.setAttribute('aria-hidden', 'true');
  return el;
}
function markSaving(el) {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '<span class="agents-spinner"></span>';
}
function flashCheck(el, ok, errMsg = '') {
  if (!el) return;
  clearTimeout(el._fadeT);
  el.classList.remove('saved', 'error');
  el.innerHTML = '';
  if (ok) {
    el.classList.add('saved'); el.textContent = '✓'; el.title = 'Saved';
    el._fadeT = setTimeout(() => { el.classList.remove('saved'); el.textContent = ''; }, 2200);
  } else {
    el.classList.add('error'); el.textContent = '!'; el.title = errMsg || 'Save failed';
    el._fadeT = setTimeout(() => { el.classList.remove('error'); el.textContent = ''; }, 4000);
  }
}
function fmtTokens(n) {
  if (!n) return '—';
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1000) return `${(n / 1000).toFixed(1)}k`;
  return String(n);
}
function fmtContext(ctx) {
  if (!ctx || typeof ctx !== 'number') return '';
  if (ctx >= 1_000_000) return `${(ctx / 1_000_000).toFixed(ctx % 1_000_000 ? 1 : 0)}M ctx`;
  if (ctx >= 1000) return `${Math.round(ctx / 1000)}K ctx`;
  return `${ctx} ctx`;
}
function fmtPrice(inCost, outCost) {
  const f = (v) => (v === 0 ? 'free' : (typeof v === 'number' ? `$${v}` : null));
  const a = f(inCost), b = f(outCost);
  if (a === 'free' && b === 'free') return 'free';
  if (a && b) return `${a}/${b} per 1M`;
  if (a) return `${a} in /1M`;
  if (b) return `${b} out /1M`;
  return '';
}
// MODEL-PRICE-EDIT: a catalog row's price is the app admin's house-credit rate, so it
// is rendered in credits rather than dollars — the same unit the model dropdown uses
// for the same rows. Rates arrive per 1M tokens.
function fmtCredits(inCredits, outCredits) {
  const f = (v) => (typeof v === 'number' && Number.isFinite(v) ? String(Math.round(v * 1000) / 1000) : null);
  const a = f(inCredits), b = f(outCredits);
  if (a == null && b == null) return '';
  return `${a ?? '—'} credits / 1M in · ${b ?? '—'} credits / 1M out`;
}
// A stored cost is an override only when it is a real, non-negative number; a blank
// or malformed value falls back to whatever the model catalog itself publishes.
function numericCost(value) {
  if (value === null || value === undefined || value === '') return null;
  const n = Number(value);
  return Number.isFinite(n) && n >= 0 ? n : null;
}

// ── Hold-to-cancel guard for the saved-model capability checkboxes ───────────
// A capability box (Txt / Txt+ / In / Out) commits its change to the server the
// instant it's ticked. To stop an accidental DOUBLE-CLICK from silently flipping
// it straight back, the box LOCKS the moment it's toggled: the underlying
// checkbox is disabled and a hazard triangle drops on TOP of it (blocking a
// second click) while the save runs. The ONLY way to reverse the change is a
// deliberate LONG-PRESS of that triangle — the same hold-to-confirm affordance
// the delete buttons use (ui/shared/js/admin-ability-table.js) — which reverts
// the box and persists the reverted value. Once the save lands (or the hold
// cancels it) the triangle settles to the shared green ✓ / orange ⚠ save overlay
// (dom-utils `_flashSaveCheck`), so it still speaks the app-wide save language.
const _HAZ_MIN_MS = 900;    // keep the hazard (its cancel window) up at least this long
const _HAZ_HOLD_MS = 600;   // long-press duration that triggers a cancel

const _HAZ_SVG = '<svg viewBox="0 0 24 24" width="13" height="13" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>';

// Drop the hazard cover onto a cap cell and wire its long-press. `onHoldComplete`
// fires after a full _HAZ_HOLD_MS press; releasing early resets the fill. The
// returned node carries `_holding` (a hold is in progress) and `_teardown()`.
function _buildCapHazard(cell, onHoldComplete) {
  const haz = document.createElement('span');
  haz.className = 'ac-cap-hazard';
  haz.title = 'Hold to undo';
  const fill = document.createElement('span'); fill.className = 'ac-cap-hazard-fill';
  const ico = document.createElement('span'); ico.className = 'ac-cap-hazard-icon'; ico.innerHTML = _HAZ_SVG;
  haz.appendChild(fill); haz.appendChild(ico);
  cell.appendChild(haz);
  requestAnimationFrame(() => haz.classList.add('show'));

  let raf = null, startAt = 0;
  haz._holding = false;
  const _stop = () => { if (raf) { cancelAnimationFrame(raf); raf = null; } };
  const step = () => {
    const pct = Math.min(100, ((Date.now() - startAt) / _HAZ_HOLD_MS) * 100);
    fill.style.height = pct + '%';
    if (pct >= 100) { _stop(); haz._holding = false; onHoldComplete(); return; }
    raf = requestAnimationFrame(step);
  };
  const startHold = (e) => {
    if (e && e.button != null && e.button !== 0) return;   // left button / touch only
    haz._holding = true; startAt = Date.now(); fill.style.height = '0%'; step();
  };
  const cancelHold = () => { if (!haz._holding) return; haz._holding = false; _stop(); fill.style.height = '0%'; };
  haz.addEventListener('pointerdown', startHold);
  haz.addEventListener('pointerup', cancelHold);
  haz.addEventListener('pointerleave', cancelHold);
  haz.addEventListener('pointercancel', cancelHold);
  // Swallow the click so a completed/aborted hold never bubbles to the row's
  // expand toggle or re-triggers the checkbox.
  haz.addEventListener('click', (e) => { e.stopPropagation(); e.preventDefault(); });

  haz._teardown = () => { _stop(); haz.remove(); };
  return haz;
}

// Run a capability-checkbox change behind the hold-to-cancel guard.
//   writeFlag(v)  — set the model's in-memory flag to v (applied, then prev on revert)
//   save()        — returns the persist Promise for the current in-memory state
//   afterChange() — optional: re-sync dependent UI (e.g. default-radio eligibility)
function guardCapToggle(cb, { writeFlag, save, afterChange }) {
  const cell = cb && cb.closest('.ac-saved-cap');
  const applied = !!(cb && cb.checked);
  const prev = !applied;
  writeFlag(applied);
  if (afterChange) afterChange();
  if (!cell) { Promise.resolve(save()); return; }   // no cell to cover — just persist

  cb.disabled = true;                 // block a flip-back click for the whole window
  let cancelled = false, settled = false;
  const unlock = () => { cb.disabled = false; };

  const haz = _buildCapHazard(cell, () => {
    if (settled || cancelled) return;
    cancelled = true;
    haz._teardown();
    writeFlag(prev); cb.checked = prev; if (afterChange) afterChange();
    _markSaving(cell);                // compensating save of the reverted value
    Promise.resolve(save())
      .then(() => _flashSaveCheck(cell, true))
      .catch((e) => _flashSaveCheck(cell, false, (e && e.message) || 'Save failed'))
      .finally(unlock);
  });

  const startAt = Date.now();
  Promise.resolve(save())
    .then(() => {
      if (cancelled) return;
      const settle = () => {
        if (cancelled) return;
        if (haz._holding) { setTimeout(settle, 120); return; }   // don't yank the box mid-hold
        settled = true; haz._teardown(); _flashSaveCheck(cell, true); unlock();
      };
      setTimeout(settle, Math.max(0, _HAZ_MIN_MS - (Date.now() - startAt)));
    })
    .catch((e) => {
      if (cancelled) return;
      settled = true; haz._teardown();
      writeFlag(prev); cb.checked = prev; if (afterChange) afterChange();
      _flashSaveCheck(cell, false, (e && e.message) || 'Save failed');
      unlock();
    });
}

/* CAPABILITY-FLAGS — the single capability list for the whole app.
   The primary model table's Flags column (below) and the Free Model Ranking
   capability column (ui/main-panel/app/settings/agent-settings/agent-settings.js)
   render the SAME squares from this one list, so the two can never disagree about
   what "Vision" or "Voice" means. Fields:
   [symbol, label, tooltip, primaryKey, capKey, onByDefault]
     primaryKey  — the flat boolean on a model-table row (text_capable, ...)
     capKey      — the same capability inside a free-catalog `capabilities` object
     onByDefault — used when neither shape reports the capability */
export const CAPABILITY_FLAGS = [
  ['D', 'Decision', 'Decision — structured decisions', 'decision_capable', 'decision', false],
  ['T', 'Text', 'Text — chat', 'text_capable', 'text', true],
  ['V', 'Vision', 'Vision — read images', 'image_capable', 'vision', false],
  ['I', 'Image', 'Image — generate images', 'image_out_capable', 'image_out', false],
  ['🎤', 'Voice', 'Voice — live voice input', 'voice_capable', 'voice', false],
];

/** Resolve every capability flag for a model row into `{title, on}` entries, in
 *  CAPABILITY_FLAGS order. Accepts either the primary table's flat `*_capable`
 *  booleans or the free catalog's `capabilities` object. */
export function capabilityStates(row) {
  const caps = row && typeof row.capabilities === 'object' ? row.capabilities : null;
  return CAPABILITY_FLAGS.map(([, , title, primaryKey, capKey, onByDefault]) => {
    let value;
    if (caps && capKey in caps) value = caps[capKey];
    else if (row && row[primaryKey] !== undefined) value = row[primaryKey];
    if (value === undefined || value === null) value = onByDefault;
    return { title, on: !!value };
  });
}

/** The capability squares as HTML, for rows rendered from a template string.
 *  `all: true` (default) shows the complete option set — capable squares marked on,
 *  the rest muted; `all: false` shows only the capabilities the model holds. */
export function capabilityFlagsHtml(row, { all = true } = {}) {
  const states = capabilityStates(row);
  return CAPABILITY_FLAGS.map(([symbol], i) => {
    const state = states[i];
    if (!all && !state.on) return '';
    return `<span class="ac-role-flag${state.on ? '' : ' off'}" title="${state.title}">${symbol}</span>`;
  }).join('');
}

export function mountModelTable(host, opts = {}) {
  const adapter = opts.adapter || {};
  const fetchFn = opts.fetchFn || ((...a) => window.fetch(...a));
  const apiPath = opts.apiPath || ((p) => p);
  const features = opts.features || {};
  const onModelChange = opts.onModelChange || (() => {});
  // MODEL-LEGEND-ALIAS: the usage chart mounted beside this table labels its model
  // series with the same display names these rows render, so the roster is published
  // to it after every load instead of being re-derived inside the chart.
  const onRoster = typeof opts.onRoster === 'function' ? opts.onRoster : null;
  // MODEL-ROW-FOCUS: the usage chart above this table follows the row the user
  // opens, so the widening box answers "what did THIS model cost over time" — the
  // read the removed per-row chart used to give — and falls back to the whole group
  // when no row is open. Fired with the row's model id, or '' to release the filter.
  const onModelFocus = typeof opts.onModelFocus === 'function' ? opts.onModelFocus : null;
  const subscriptionScope = opts.subscriptionScope || 'platform';
  // 'user' (default) shows only the current user's usage for each model; 'global'
  // (admin Agent Settings) sums every agent, user, and background task app-wide.
  const usageScope = opts.usageScope || 'user';
  // Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT): editable only
  // when the mount both allows it AND provides a save adapter (admin mounts).
  const directivesEditable = !!(opts.directivesEditable && adapter.saveDirectives);
  // Keep popup chips as the shared default; Agent Config opts into inline rows.
  const directivePresentation = opts.directivePresentation === 'inline' ? 'inline' : 'popup';
  // App-level "Model Catalog" mounts (admin Settings) treat the saved list as a
  // catalog of provider-derived capabilities only. Role assignment is disabled:
  // rows render read-only capability badges instead of a role popover, because
  // roles belong to the agent Config tab, not to the app-global catalog.
  const catalogOnly = opts.catalogOnly === true;
  // The user-facing All Agents catalog is informational. It shares the table
  // renderer but must never expose provider, credential, roster, or role writes.
  const catalogReadOnly = opts.catalogReadOnly === true;
  const userCatalog = opts.userCatalog === true;
  const inlineCatalogAdd = opts.inlineCatalogAdd === true;
  const agentLocalRoster = opts.agentLocalRoster === true;

  // ── instance state ──
  const S = {
    presets: {},
    providerConfigs: {},        // per-provider remembered single config (key/model/base_url)
    currentProvider: userCatalog ? 'webagent_catalog' : '_custom',
    allModels: [],
    selectedModel: '',
    selectedCapFilters: new Set(), // empty shows every catalog model; keys are capabilities (Text/Vision/Image/Voice/Video), never roster roles
    selCaps: { text: false, image: false, imageOut: false },
    focusedModel: '',           // MODEL-ROW-FOCUS: model id of the open row a sibling chart is following ('' = none)
    providers: [],              // the saved roster (each gets a _uid)
    roleAssignments: {},        // semantic role -> stable roster entry_id (sole role authority)
    defaultEntryId: '',         // user-catalog default for newly created agents only
    directives: null,           // per-agent roster-role prompt directives (null = unavailable)
    uid: 0,
  };
  // Sole role authority: `roleAssignments` (semantic role -> roster entry_id).
  // An unassigned role has no target, so a row with a blank entry_id must never
  // match it — without this guard an entry_id-less row would appear to hold
  // every unassigned role.
  const hasRole = (provider, role) => {
    const target = String(S.roleAssignments[role] || '');
    return target !== '' && target === String(provider?.entry_id || '');
  };
  // ── MODEL-NAME-LEVELS ──
  // Every row carries two names. The MODEL NAME is the model's canonical name —
  // the WebAgent Catalog's own alias for a catalog model, the real model id for a
  // BYO row. The DISPLAY NAME is the user's own name layered on top of it, one per
  // scope (this agent's roster row, or the account catalog entry). The display name
  // wins wherever the model is presented; the catalog alias is the fallback; the
  // real model id is the last resort for a BYO row. The real provider and real
  // model id otherwise stay internal (they key the Usage column and per-model meta
  // lookups). This is the ONE place a row resolves its identity.
  const isCatalogRow = (p) => p?.catalog_origin === 'platform';
  const catalogProviderName = () => S.presets?.webagent_catalog?.name || 'WebAgent Catalog';
  // Level 1 — the model name. A catalog row shows the catalog's alias; a BYO row
  // shows its real model id. A blank catalog alias shows the neutral label below
  // rather than ever revealing the real upstream model.
  const catalogModelName = (p) => String(p?.catalog_name ?? p?.display_name ?? '').trim() || 'WebAgent Catalog model';
  const modelNameOf = (p) => (isCatalogRow(p) ? catalogModelName(p) : (String(p?.model || '').trim() || '—'));
  // Level 2 — the user's own display name, blank when they have set none. A
  // catalog row's rehydration retains the catalog name when the caller set no
  // display name, so a value equal to the model name is not one. BYO rows store
  // only the caller's own value.
  const userDisplayName = (p) => {
    const stored = String(p?.display_name || '').trim();
    return isCatalogRow(p) && stored === modelNameOf(p) ? '' : stored;
  };
  const rowDisplayName = (p) => userDisplayName(p) || modelNameOf(p) || '—';
  const saveRoster = () => adapter.saveRoster({
    providers: S.providers,
    role_assignments: { ...S.roleAssignments },
    default_entry_id: S.defaultEntryId,
  });
  let fetchDebounce = null, autosaveDebounce = null;
  let modelsBusy = 0;   // >0 while the provider's available-model list is loading (drives the + button spinner)
  let catalogAddBusy = false;
  let modelLoadRevision = 0;
  let pendingSubscriptionRow = null;
  const subscriptionCountdowns = new WeakMap();
  const subscriptionCountdownHandles = new Set();
  let _directiveModalCleanup = null;  // closes an open directive editor on destroy
  const docListeners = [];

  // ── default endpoint helpers (adapter may override any) ──
  async function fetchPresets() {
    if (adapter.loadPresets) return adapter.loadPresets();
    try {
      const r = await fetchFn(apiPath('/admin/settings/providers'));
      return r.ok ? await r.json() : {};
    } catch { return {}; }
  }
  async function fetchModels(args) {
    if (adapter.fetchModels) return adapter.fetchModels(args);
    // Subscription discovery is shared and scoped, including catalog adapters.
    if (args.provider === 'openai_subscription') {
      try {
        const r = await subscriptionRequest('/api/v1/openai-subscription/models');
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return await r.json();
      } catch (e) { return { models: [], error: e.message }; }
    }
    if (adapter.fetchModels) return adapter.fetchModels(args);
    const params = new URLSearchParams({ provider: args.provider || '' });
    if (args.api_key && args.base_url) { params.set('api_key', args.api_key); params.set('base_url', args.base_url); }
    try {
      const r = await fetchFn(apiPath(`/admin/settings/models?${params.toString()}`));
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      return await r.json();
    } catch (e) { return { error: e.message }; }
  }
  async function fetchModelInfo(model, provider) {
    if (adapter.fetchModelInfo) return adapter.fetchModelInfo(model, provider);
    try {
      const params = new URLSearchParams({ model: model || '', provider: provider || '' });
      const r = await fetchFn(apiPath(`/admin/settings/model-info?${params.toString()}`));
      const d = r.ok ? await r.json() : null;
      return (d && d.info) || {};
    } catch { return {}; }
  }
  async function fetchModelUsage(args) {
    if (adapter.fetchModelUsage) return adapter.fetchModelUsage(args);
    try {
      const params = new URLSearchParams({ model: args.model || '', provider: args.provider || '', scope: usageScope });
      if (args.entry_id) {
        params.set('entry_id', args.entry_id);
        if (args.include_unassigned) params.set('include_unassigned', '1');
      }
      const r = await fetchFn(apiPath(`/admin/settings/model-usage?${params.toString()}`));
      const d = r.ok ? await r.json() : null;
      return (d && !d.error) ? d : null;
    } catch { return null; }
  }

  // ─────────────────────────────────────────────────────────────────────────
  // ── Build the markup (mirrors agent-settings.html's Models block) ──────────
  // ─────────────────────────────────────────────────────────────────────────
  host.innerHTML = '';
  const root = document.createElement('div');
  root.className = 'mt-root';

  const table = document.createElement('div');
  // Start expanded so the add-model form is immediately available. The + / ×
  // control below remains the user's manual hide/show toggle.
  table.className = 'ac-list ac-model-config';
  table.style.display = 'none';

  // CHEVRON-CONSISTENCY: Provider's in-field dropdown arrow and every
  // prompt-injection-row expander below use this exact 16px / 1.8-stroke glyph.
  // Provider rotates it 90° in CSS (see `.ac-model-search-chevron` in app3.css)
  // so the right-pointing row chevron reads as down; Model Name intentionally
  // has no chevron because its catalog is a persistent inline panel.
  const ROW_CHEVRON_SVG = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m9 18 6-6-6-6"/></svg>';

  // Provider row. The visible control is a searchable COMBOBOX that mirrors the
  // Model name field (same input + in-field chevron + dropdown), so both fields
  // read and behave alike: click to open the full list, type to filter it. The
  // native <select> stays in the DOM as the hidden store of options and remains
  // the single authority every existing code path already reads and writes
  // (`provSel.value` plus its `change` event) — the combobox only mirrors it and
  // commits back through it, so nothing downstream changes behaviour.
  const provRow = document.createElement('div');
  provRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  provRow.innerHTML = `<span class="ac-ability-label"><span class="ac-ability-name">Provider</span></span>`;
  const provCtrl = document.createElement('span');
  provCtrl.className = 'ac-model-cfg-control';
  const provSel = document.createElement('select');
  provSel.className = 'ac-input ac-input-sm ac-model-provider-native';
  provSel.setAttribute('aria-hidden', 'true');
  provSel.tabIndex = -1;
  const provField = document.createElement('span');
  provField.className = 'ac-model-search-field';
  const provInput = document.createElement('input');
  provInput.type = 'search'; provInput.className = 'ac-input ac-input-sm';
  provInput.autocomplete = 'off'; provInput.spellcheck = false;
  // No placeholder and no empty state: the field always carries a real
  // selection — WebAgent Catalog by default — so it never reads "choose one".
  provInput.setAttribute('role', 'combobox');
  provInput.setAttribute('aria-expanded', 'false');
  provInput.setAttribute('aria-autocomplete', 'list');
  provInput.setAttribute('aria-label', 'Provider');
  const provDd = document.createElement('div');
  provDd.className = 'ac-model-dropdown'; provDd.style.display = 'none';
  const provChevron = document.createElement('span');
  provChevron.className = 'ac-model-search-chevron';
  provChevron.setAttribute('aria-hidden', 'true');
  provChevron.innerHTML = ROW_CHEVRON_SVG;
  provField.append(provInput, provChevron);
  const provCheck = makeCheck();
  provCtrl.appendChild(provSel); provCtrl.appendChild(provField); provCtrl.appendChild(provCheck);
  provRow.appendChild(provCtrl);
  table.appendChild(provRow);

  // Base URL row
  const urlRow = document.createElement('div');
  urlRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  urlRow.innerHTML = `<span class="ac-ability-label"><span class="ac-ability-name">Base URL</span></span>`;
  const urlCtrl = document.createElement('span');
  urlCtrl.className = 'ac-model-cfg-control';
  const urlInput = document.createElement('input');
  urlInput.type = 'text'; urlInput.className = 'ac-input ac-input-sm';
  urlInput.placeholder = 'https://openrouter.ai/api/v1'; urlInput.autocomplete = 'off';
  const urlCheck = makeCheck();
  urlCtrl.appendChild(urlInput); urlCtrl.appendChild(urlCheck);
  urlRow.appendChild(urlCtrl);
  table.appendChild(urlRow);

  // API Key row
  const keyRow = document.createElement('div');
  keyRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  keyRow.innerHTML = `<span class="ac-ability-label"><span class="ac-ability-name">API Key</span></span>`;
  const keyCtrl = document.createElement('span');
  keyCtrl.className = 'ac-model-cfg-control';
  const keyInput = document.createElement('input');
  keyInput.type = 'text'; keyInput.className = 'ac-input ac-input-sm';
  keyInput.placeholder = 'sk-...'; keyInput.autocomplete = 'off';
  const keyCheck = makeCheck();
  const loadCredentialBtn = document.createElement('button');
  loadCredentialBtn.type = 'button'; loadCredentialBtn.className = 'ac-btn ac-btn-ghost ac-btn-sm';
  loadCredentialBtn.textContent = 'Load'; loadCredentialBtn.title = 'Load a saved credential for this provider';
  const saveCredentialBtn = document.createElement('button');
  saveCredentialBtn.type = 'button'; saveCredentialBtn.className = 'ac-btn ac-btn-ghost ac-btn-sm';
  saveCredentialBtn.textContent = 'Save'; saveCredentialBtn.title = 'Validate a credential before saving it';
  saveCredentialBtn.disabled = true;
  let validatedCredential = '';
  let credentialValidationTimer = null;
  const credentialSaveCheck = makeCheck();
  const credentialSelect = document.createElement('select');
  credentialSelect.className = 'ac-input ac-input-sm'; credentialSelect.setAttribute('aria-label', 'Saved credentials');
  credentialSelect.hidden = true;
  keyCtrl.append(keyInput, loadCredentialBtn, saveCredentialBtn, credentialSaveCheck, credentialSelect, keyCheck);
  keyRow.appendChild(keyCtrl);
  table.appendChild(keyRow);

  // OpenAI subscription account row. Issuing a code never opens a browser;
  // opening OpenAI is a separate, explicit user action.
  const subscriptionRow = document.createElement('div');
  subscriptionRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  subscriptionRow.style.display = 'none';
  subscriptionRow.innerHTML = '<span class="ac-ability-label"><span class="ac-ability-name">Connection</span></span>';
  const subscriptionCtrl = document.createElement('span');
  subscriptionCtrl.className = 'ac-model-cfg-control';
  subscriptionCtrl.style.cssText = 'display:flex;align-items:flex-start;gap:8px;flex-wrap:wrap;flex-direction:column;';
  const subscriptionState = document.createElement('span');
  subscriptionState.className = 'ac-hint ac-subscription-auth';
  subscriptionState.style.margin = '0';
  const subscriptionConnect = document.createElement('button');
  subscriptionConnect.type = 'button';
  subscriptionConnect.className = 'ac-btn ac-btn-primary ac-btn-sm';
  subscriptionConnect.textContent = 'Click to Issue verification code';
  const subscriptionActions = document.createElement('span');
  subscriptionActions.style.cssText = 'display:flex;align-items:center;gap:8px;flex-wrap:wrap;';
  const subscriptionReset = document.createElement('button');
  subscriptionReset.type = 'button';
  subscriptionReset.className = 'ac-btn ac-btn-ghost ac-btn-sm';
  subscriptionReset.textContent = 'Reset connection';
  subscriptionReset.title = 'Clear this agent’s ChatGPT subscription credentials and cancel pending authorization';
  subscriptionActions.appendChild(subscriptionConnect);
  subscriptionActions.appendChild(subscriptionReset);
  subscriptionCtrl.append(subscriptionState, subscriptionActions);
  subscriptionRow.appendChild(subscriptionCtrl);
  table.appendChild(subscriptionRow);
  const displayNameHelp = 'Your own name for this model, shown to users wherever the model appears. '
    + 'Use a friendly name such as Basic or Pro, or rename it for a customer-facing agent. '
    + 'Leave blank to keep the model\'s own name.';


  // Optional nickname for the model row being added. The saved-row details
  // retain their existing editor so the name can also be changed later.
  const displayNameRow = document.createElement('div');
  displayNameRow.className = 'ac-ability-row ac-model-cfg-row ac-row-static';
  displayNameRow.innerHTML = '<span class="ac-ability-label"><span class="ac-ability-name">Display name</span></span>';
  const displayNameTip = tipBadge(displayNameHelp);
  if (displayNameTip) displayNameRow.querySelector('.ac-ability-label').appendChild(displayNameTip);
  const displayNameCtrl = document.createElement('span');
  displayNameCtrl.className = 'ac-model-cfg-control';
  const displayNameInput = document.createElement('input');
  displayNameInput.type = 'text'; displayNameInput.className = 'ac-input ac-input-sm';
  displayNameInput.maxLength = 120;
  displayNameInput.placeholder = 'Optional';
  displayNameInput.autocomplete = 'off';
  displayNameCtrl.appendChild(displayNameInput); displayNameRow.appendChild(displayNameCtrl);
  table.appendChild(displayNameRow);

  // Model row (search + caps + Add/Clear)
  const modelRow = document.createElement('div');
  modelRow.className = 'ac-ability-row ac-model-cfg-row ac-model-cfg-model ac-row-static';
  const modelLabel = document.createElement('span');
  modelLabel.className = 'ac-ability-label';
  const searchWrap = document.createElement('span');
  searchWrap.className = 'ac-model-search-wrap ac-model-catalog-control';
  const capFilters = document.createElement('div');
  capFilters.className = 'ac-model-cap-filters';
  capFilters.setAttribute('aria-label', 'Filter models by capability');
  // The chips are CAPABILITIES — Text / Vision / Image / Voice / Video — never
  // roster roles. A text model can serve several roles at once (Standard /
  // Premium / System), so those role names are neither chips nor result badges
  // here. Selecting several chips narrows the catalog to models that have ALL.
  const MODEL_CAPABILITY_FILTERS = [
    { key: 'decision', label: 'Decision', matches: (m) => !!m.decision_capable },
    { key: 'text',   label: 'Text',   matches: (m) => m.text_capable !== false },
    { key: 'vision', label: 'Vision', matches: (m) => !!m.image_capable },
    { key: 'image',  label: 'Image',  matches: (m) => !!m.image_out_capable },
    { key: 'voice',  label: 'Voice',  matches: (m) => !!m.voice_capable },
    { key: 'video',  label: 'Video',  matches: (m) => !!m.video_capable },
  ];
  const capFilterByKey = new Map(MODEL_CAPABILITY_FILTERS.map((cap) => [cap.key, cap]));
  MODEL_CAPABILITY_FILTERS.forEach((cap) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'ac-model-cap-filter';
    button.textContent = cap.label;
    button.setAttribute('aria-pressed', 'false');
    button.addEventListener('click', () => {
      const active = S.selectedCapFilters.has(cap.key);
      if (active) S.selectedCapFilters.delete(cap.key);
      else S.selectedCapFilters.add(cap.key);
      button.classList.toggle('active', !active);
      button.setAttribute('aria-pressed', String(!active));
      renderModelDropdown(modelSearch.value.toLowerCase());
    });
    capFilters.appendChild(button);
  });
  const modelSearch = document.createElement('input');
  modelSearch.type = 'search'; modelSearch.className = 'ac-input ac-input-sm';
  modelSearch.placeholder = 'Select or search by model name…'; modelSearch.autocomplete = 'off';
  modelSearch.name = 'model-catalog-query';
  modelSearch.setAttribute('aria-label', 'Filter models by name');
  // This field follows an API-key input, which can make browser/password-manager
  // heuristics misclassify it as a username field. It is only a catalog search.
  modelSearch.setAttribute('data-lpignore', 'true');
  modelSearch.setAttribute('data-1p-ignore', 'true');
  modelSearch.setAttribute('data-bwignore', 'true');
  modelSearch.setAttribute('data-form-type', 'other');
  modelSearch.setAttribute('spellcheck', 'false');
  // The model picker is a persistent, in-page catalog panel directly below the
  // search field. Unlike the Provider menu, it stays in the search wrapper,
  // never portals to the document body, and is always visible while this form is.
  const modelDd = document.createElement('div');
  modelDd.className = 'ac-model-results-panel';
  const modelSearchField = document.createElement('span');
  modelSearchField.className = 'ac-model-search-field';
  modelSearchField.appendChild(modelSearch);
  searchWrap.append(capFilters, modelSearchField, modelDd);
  const modelBar = document.createElement('div');
  modelBar.className = 'ac-model-cfg-modelbar';
  const capsEl = document.createElement('span');
  capsEl.className = 'ac-model-caps';
  const addControl = document.createElement('span');
  addControl.className = 'ac-model-cfg-control ac-model-add-control';
  const addBtn = document.createElement('button');
  addBtn.className = 'ac-btn ac-btn-primary ac-btn-sm'; addBtn.textContent = 'Add';
  const freeBtn = document.createElement('button');
  freeBtn.type = 'button'; freeBtn.className = 'ac-btn ac-btn-ghost ac-btn-sm';
  freeBtn.textContent = 'OpenRouter Free (Auto)';
  freeBtn.title = 'Add an F-number alias routed by OpenRouter to its current free model pool.';
  // Hide synchronously for catalog mounts; renderSubscriptionAuth keeps this
  // invariant during later provider changes.
  if (userCatalog) freeBtn.style.display = 'none';
  const clearBtn = document.createElement('button');
  clearBtn.className = 'ac-btn ac-btn-ghost ac-btn-sm'; clearBtn.textContent = 'Clear';
  const modelCheck = makeCheck();
  addControl.appendChild(addBtn); addControl.appendChild(freeBtn); addControl.appendChild(clearBtn); addControl.appendChild(modelCheck);
  if (inlineCatalogAdd) addControl.hidden = true;
  modelBar.appendChild(capsEl); modelBar.appendChild(addControl);
  modelLabel.appendChild(searchWrap); modelLabel.appendChild(modelBar);
  modelRow.appendChild(modelLabel);
  table.appendChild(modelRow);

  // MODEL-ALIASES: reuse the live configurator (including credentials and model
  // validation), never clone its fields or handlers. Restore it before any row
  // rebuild so a destroyed detail panel cannot take the form with it.
  const configuratorRows = [provRow, urlRow, keyRow, subscriptionRow, displayNameRow, modelRow];
  let aliasTarget = null;
  let aliasForm = null;
  let aliasTrigger = null;
  function closeAliasForm() {
    if (!aliasForm) return;
    configuratorRows.forEach(row => table.insertBefore(row, advRow));
    aliasForm.remove();
    aliasTrigger?.setAttribute('aria-expanded', 'false');
    aliasForm = null; aliasTarget = null; aliasTrigger = null;
    addBtn.textContent = 'Add';
  }
  function openAliasForm(p, destination, trigger) {
    if (aliasTarget === p) { closeAliasForm(); trigger.focus(); return; }
    closeAliasForm();
    aliasTarget = p; aliasTrigger = trigger;
    aliasForm = document.createElement('div');
    aliasForm.className = 'ac-list ac-model-alias-form';
    aliasForm.setAttribute('role', 'group');
    aliasForm.setAttribute('aria-label', `New backup connection for ${rowDisplayName(p)}`);
    const heading = document.createElement('div');
    heading.className = 'ac-ability-row';
    const text = document.createElement('span');
    text.className = 'ac-ability-name';
    text.textContent = `New alias for ${rowDisplayName(p)}`;
    const cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'ac-btn ac-btn-ghost ac-btn-sm';
    cancel.textContent = 'Cancel';
    cancel.addEventListener('click', () => { closeAliasForm(); trigger.focus(); });
    heading.append(text, cancel); aliasForm.appendChild(heading);
    configuratorRows.forEach(row => aliasForm.appendChild(row));
    destination.appendChild(aliasForm);
    table.classList.remove('ac-cfg-collapsed');
    // A backup is a NEW connection; never reuse the primary's credential or
    // pending subscription entry. Provider changes keep their existing flow.
    pendingSubscriptionRow = null;
    clearModel(); keyInput.value = '';
    provSel.value = userCatalog ? 'webagent_catalog' : '_custom';
    provSel.dispatchEvent(new Event('change'));
    addBtn.textContent = 'Save alias';
    trigger.setAttribute('aria-expanded', 'true');
    provInput.focus();
  }

  // Advanced / + action row
  const advRow = document.createElement('div');
  advRow.className = 'ac-row'; advRow.dataset.role = 'mt-advanced-row';
  const advHead = document.createElement('div');
  advHead.className = 'ac-ability-row ac-model-advanced-head';
  const advActions = document.createElement('span');
  advActions.className = 'ac-model-row-actions';
  const advToggle = document.createElement('button');
  advToggle.type = 'button'; advToggle.className = 'ac-model-icon-btn';
  advToggle.title = 'Advanced options'; advToggle.setAttribute('aria-label', 'Advanced options');
  advToggle.innerHTML = `<i data-lucide="sliders-horizontal" class="lucide-icon" style="width:16px;height:16px;"></i>`;
  const addToggle = document.createElement('button');
  addToggle.type = 'button'; addToggle.className = 'ac-model-add-toggle';
  addToggle.title = 'Add a model'; addToggle.setAttribute('aria-label', 'Add a model');
  addToggle.innerHTML = `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>`;
  // Spinner shown ON the + button while the provider's available-model list loads
  // (the one genuinely slow fetch — it powers the "Search models…" box for adding
  // a model). Hidden by default; CSS swaps the + glyph for it while `.loading`.
  const addSpin = document.createElement('span');
  addSpin.className = 'ac-model-add-spinner'; addSpin.setAttribute('aria-hidden', 'true');
  addToggle.appendChild(addSpin);
  advActions.appendChild(advToggle); advActions.appendChild(addToggle);
  advHead.appendChild(advActions);
  const advBody = document.createElement('div');
  advBody.className = 'ac-ability-body';
  advRow.appendChild(advHead); advRow.appendChild(advBody);

  // Advanced body content: caller extras only (parallel model racing removed —
  // the agent runs a single default model, switchable per-chat by the user).
  if (typeof opts.advancedExtra === 'function') opts.advancedExtra(advBody);
  const savedCredentialsSection = document.createElement('div');
  savedCredentialsSection.className = 'ac-row ac-saved-credentials';
  const savedCredentialsHead = document.createElement('div');
  savedCredentialsHead.className = 'ac-ability-row';
  savedCredentialsHead.innerHTML = '<span class="ac-ability-label"><span class="ac-ability-name">Saved credentials</span><span class="ac-ability-desc">API keys and connected subscriptions saved in this model table’s vault.</span></span>';
  const savedCredentialsBody = document.createElement('div');
  savedCredentialsBody.className = 'ac-ability-body';
  savedCredentialsSection.append(savedCredentialsHead, savedCredentialsBody);
  advBody.appendChild(savedCredentialsSection);

  // ── Skeleton placeholder — shown while saved-models data loads ────────────
  // Mimics the saved-models header + 3 phantom rows with sk-shimmer so the user
  // sees structure (not just the Advanced/+ buttons) during the async fetch.
  const GRID = '20px minmax(0, 1fr) 38px 20px';
  const skelRoot = document.createElement('div');
  skelRoot.className = 'ac-model-skeleton';
  skelRoot.style.display = 'none';
  // Header
  const skelHead = document.createElement('div');
  skelHead.className = 'ac-ability-row ac-saved-head ac-saved-row';
  skelHead.style.gridTemplateColumns = GRID;
  skelHead.innerHTML = '<span></span><span class="ac-ability-label"><span class="ac-saved-th">Model</span></span>'
    + '<span class="ac-saved-th ac-saved-usage-th">Usage</span>'
    + '<span class="ac-saved-cap ac-saved-th" style="font-size:10px;font-weight:700;letter-spacing:0.4px;color:var(--fg-3);">More</span>';
  skelRoot.appendChild(skelHead);
  // Phantom rows — variable-width shimmer lines for model names, a short line for
  // the flags, stacked short lines for usage, and a line for the More button.
  const SKEL_NAMES = ['gpt-4o', 'claude-sonnet-4', 'claude-3-haiku'];
  for (let i = 0; i < 3; i++) {
    const row = document.createElement('div');
    row.className = 'ac-ability-row ac-saved-row';
    row.style.gridTemplateColumns = GRID;
    const nmSpan = 50 + Math.round((SKEL_NAMES[i].length / 14) * 40);
    row.innerHTML = '<span></span>'
      + `<span class="ac-ability-label"><span class="sk-shimmer sk-line" style="width:${nmSpan}%"></span></span>`
      + '<span class="ac-saved-usage" style="display:flex;flex-direction:column;align-items:flex-end;gap:4px">'
      + '<span class="sk-shimmer sk-line" style="width:70%;height:8px"></span>'
      + '<span class="sk-shimmer sk-line" style="width:55%;height:8px"></span>'
      + '<span class="sk-shimmer sk-line" style="width:40%;height:8px"></span></span>'
      + '<span class="ac-saved-cap"><span class="sk-shimmer sk-line" style="width:18px;height:12px"></span></span>';
    skelRoot.appendChild(row);
  }
  table.appendChild(skelRoot);

  table.appendChild(advRow);
  if (catalogReadOnly) {
    // Keep the hidden configurator as the insertion anchor for saved rows, but
    // remove every authoring affordance from the user-facing catalog surface.
    table.querySelectorAll('.ac-model-cfg-row').forEach(row => {
      row.hidden = true;
      // `.ac-ability-row { display:flex }` is intentionally high-specificity in
      // shared CSS, so the HTML hidden attribute alone is not sufficient here.
      row.style.display = 'none';
    });
    advRow.hidden = true;
    advRow.style.display = 'none';
  }
  root.appendChild(table);

  // Agent Config can render roster prompt editors as a distinct section below
  // the model table. Other mounts retain the compact in-table strip/modal.
  const directivesSection = document.createElement('section');
  directivesSection.className = 'ac-directives-section';
  directivesSection.hidden = true;
  const directivesTitle = document.createElement('div');
  directivesTitle.className = 'ac-directives-section-title';
  directivesTitle.textContent = directivePresentation === 'inline'
    ? 'Model Prompt Injections'
    : 'Prompt injections';
  directivesTitle.title = 'Admin-authored prompt text injected per roster role (grep ROLE-DIRECTIVE-INJECT)';
  // Inline Agent Config rows remain plain content below the model card; the
  // heading explains their purpose without introducing a framed surface.
  directivesTitle.hidden = false;
  const directivesDescription = document.createElement('p');
  directivesDescription.className = 'ac-directives-section-description';
  directivesDescription.textContent = 'These instructions are added to the system prompt when a session uses the corresponding model role.';
  directivesDescription.hidden = directivePresentation !== 'inline';
  const directivesList = document.createElement('div');
  directivesList.className = directivePresentation === 'inline'
    ? 'ac-directives-list ac-directives-list--plain'
    : 'ac-list ac-directives-list';
  if (directivePresentation === 'inline') directivesSection.classList.add('ac-directives-section--plain');
  directivesSection.append(directivesTitle, directivesDescription, directivesList);

  // Status line lives INSIDE the table (as a footer row of the .ac-list) so it
  // reads as part of the model table and hides together with it while loading —
  // never floats as orphan text above/below a not-yet-visible table.
  const statusEl = document.createElement('div');
  statusEl.className = 'ac-status ac-model-cfg-status'; statusEl.style.cssText = 'display:none;padding:8px 14px;';
  table.appendChild(statusEl);

  host.appendChild(root);
  // Inline Agent Config directives are a plain host-level sibling below the
  // model-table card, within the existing Model topic.
  host.appendChild(directivesSection);

  if (window.lucide && advRow.querySelector('[data-lucide]:not(.lucide)')) {
    try { window.lucide.createIcons({ nodes: Array.from(advRow.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch {}
  }

  // ─────────────────────────────────────────────────────────────────────────
  // ── Behaviour ──────────────────────────────────────────────────────────────
  // ─────────────────────────────────────────────────────────────────────────
  function setModelStatus(text, ok) {
    // No dedicated status line in the configurator; reuse the caps area's title.
    modelSearch.title = text || '';
    if (text) { statusEl.textContent = text; statusEl.style.display = 'block'; statusEl.style.color = ok ? 'var(--success)' : 'var(--fg-3)'; }
    else { statusEl.style.display = 'none'; }
  }

  function renderDetectedCaps(m) {
    if (!m) { capsEl.innerHTML = ''; return; }
    // Detected CAPABILITIES only — never roster roles. One text model can serve
    // several roles, so no Standard/Premium/System badge belongs in this strip.
    const caps = [
      ['Decision', !!m.decision_capable],
      ['Text', m.text_capable !== false],
      ['Vision', !!m.image_capable],
      ['Image', !!m.image_out_capable],
      ['Voice', !!m.voice_capable],
      ['Video', !!m.video_capable],
    ];
    capsEl.innerHTML = caps.map(([label, on]) => `<span class="ac-cap-badge ${on ? 'on' : 'off'}">${label}</span>`).join('');
  }

  // One positioner for every in-field dropdown (Model name + Provider), so the two
  // fields can never place their menus differently. The pending frame handle is
  // kept per-dropdown in a WeakMap, so scheduling one never cancels the other.
  const dropdownRafs = new WeakMap();


  function positionDropdown(dd, anchor, onOutOfView) {
    if (dd.style.display === 'none') return;
    const rect = anchor.getBoundingClientRect();
    const viewport = window.visualViewport;
    const viewportTop = viewport ? viewport.offsetTop : 0;
    const viewportLeft = viewport ? viewport.offsetLeft : 0;
    const viewportHeight = viewport ? viewport.height : window.innerHeight;
    const viewportWidth = viewport ? viewport.width : window.innerWidth;
    const viewportBottom = viewportTop + viewportHeight;
    const viewportRight = viewportLeft + viewportWidth;
    if (rect.bottom <= viewportTop || rect.top >= viewportBottom) {
      if (onOutOfView) onOutOfView(); else dd.style.display = 'none';
      return;
    }
    const gap = 6;
    const spaceBelow = Math.max(0, viewportBottom - rect.bottom - gap);
    const spaceAbove = Math.max(0, rect.top - viewportTop - gap);
    const openAbove = spaceBelow < 160 && spaceAbove > spaceBelow;
    const available = openAbove ? spaceAbove : spaceBelow;
    const dropdownMaxHeight = Math.max(0, Math.min(260, available));
    const dropdownWidth = Math.min(Math.max(160, rect.width), Math.max(0, viewportWidth - 16));
    const left = Math.max(viewportLeft + 8, Math.min(rect.left, viewportRight - dropdownWidth - 8));

    dd.style.position = 'fixed';
    dd.style.left = `${left}px`;
    dd.style.right = 'auto';
    dd.style.width = `${dropdownWidth}px`;
    dd.style.maxHeight = `${dropdownMaxHeight}px`;
    const renderedHeight = Math.min(dd.scrollHeight, dropdownMaxHeight);
    dd.style.top = `${openAbove ? rect.top - gap - renderedHeight : rect.bottom + gap}px`;
    dd.style.borderRadius = '6px';
    dd.style.zIndex = '10000';
  }

  function scheduleDropdownPosition(dd, anchor, onOutOfView) {
    if (dd.style.display === 'none') return;
    const pending = dropdownRafs.get(dd);
    if (pending) cancelAnimationFrame(pending);
    dropdownRafs.set(dd, requestAnimationFrame(() => {
      dropdownRafs.delete(dd);
      positionDropdown(dd, anchor, onOutOfView);
    }));
  }

  function renderModelDropdown(filter) {
    modelDd.innerHTML = '';
    const manualId = inlineCatalogAdd && opts.manualModelEntry && S.currentProvider !== 'webagent_catalog'
      && !isSubscriptionProvider() ? modelSearch.value.trim() : '';
    if (!S.allModels.length && !manualId) {
      modelDd.innerHTML = '<div class="ac-model-results-empty">Models will appear here when available.</div>';
      return;
    }
    const selectedCaps = [...S.selectedCapFilters]
      .map((key) => capFilterByKey.get(key))
      .filter(Boolean);
    const filtered = S.allModels.filter((m) => {
      const matchesText = !filter || m.id.toLowerCase().includes(filter) || (m.name || '').toLowerCase().includes(filter);
      return matchesText && selectedCaps.every((cap) => cap.matches(m));
    });
    // Providers without a list endpoint still need an explicit row for a
    // caller-entered upstream ID now that the old Add button is gone.
    if (manualId && !S.allModels.some(m => m.id.toLowerCase() === manualId.toLowerCase())
        && selectedCaps.every(cap => cap.matches({id:manualId,text_capable:true}))) {
      filtered.unshift({id:manualId,name:'Manual model ID',text_capable:true});
    }
    if (!filtered.length) {
      modelDd.innerHTML = '<div class="ac-model-results-empty">No models match your search.</div>';
      return;
    }
    filtered.forEach(m => {
      const item = document.createElement('div');
      item.className = 'ac-model-item';
      if (inlineCatalogAdd) item.classList.add('ac-model-item-selectable');
      if (m.id === S.selectedModel) item.style.background = 'rgba(var(--brand-rgb),0.12)';
      const isPlatformCatalogModel = m.catalog_origin === 'platform';
      const ctx = fmtContext(m.context);
      const creditInput = m.credit_input_per_million;
      const creditOutput = m.credit_output_per_million;
      const credits = isPlatformCatalogModel && (creditInput !== undefined || creditOutput !== undefined)
        ? `${creditInput ?? '—'} credits / 1M in · ${creditOutput ?? '—'} credits / 1M out`
        : '';
      const price = isPlatformCatalogModel ? credits : fmtPrice(m.cost_input, m.cost_output);
      const badges = [ctx, price].filter(Boolean).map(t => `<span class="ac-model-badge">${esc(t)}</span>`).join('');
      // MODEL-CATALOG-IDENTITY: catalog items are labelled by the catalog name only —
      // never the real "provider / model" string.
      const visibleModelName = isPlatformCatalogModel
        ? (m.display_name || 'WebAgent Catalog model')
        : m.id;
      const secondaryModelName = isPlatformCatalogModel ? '' : (m.name || '');
      const capBadges = [
        ['Decision', !!m.decision_capable],
        ['Text', m.text_capable !== false],
        ['Vision', !!m.image_capable],
        ['Image', !!m.image_out_capable],
        ['Voice', !!m.voice_capable],
        ['Video', !!m.video_capable],
      ].filter(([, capable]) => capable)
        .map(([label]) => `<span class="ac-cap-badge on" style="font-size:9px;">${label}</span>`)
        .join('');
      item.innerHTML = `<div class="ac-model-item-row"><span style="font-weight:500;">${esc(visibleModelName)}</span>`
        + (secondaryModelName ? `<span style="color:var(--fg-3);font-size:11px;margin-left:6px;">${esc(secondaryModelName)}</span>` : '')
        + `</div>`
        + (badges ? `<div class="ac-model-item-badges">${badges}</div>` : '')
        + `<div class="ac-model-item-caps">${capBadges}</div>`;
      if (inlineCatalogAdd) {
        const selectedRow = S.providers.find(row => S.currentProvider === 'webagent_catalog'
          ? row.catalog_source_id === m.id || row.entry_id === m.id
          : row.provider === S.currentProvider && row.model === m.id);
        const added = !!selectedRow;
        const selector = document.createElement('button');
        selector.type = 'button';
        selector.className = 'ac-model-row-selector';
        selector.classList.toggle('is-added', added);
        selector.disabled = catalogReadOnly || catalogAddBusy;
        selector.setAttribute('aria-pressed', String(added));
        selector.setAttribute('aria-label', `${added ? 'Remove' : 'Add'} ${visibleModelName} ${added ? 'from' : 'to'} available models`);
        selector.title = added ? 'Remove from available models' : 'Add to available models';
        selector.innerHTML = icon(added ? 'check-square' : 'plus-square', { size: '18px' });
        selector.addEventListener('click', (event) => {
          event.stopPropagation();
          if (catalogAddBusy) return;
          if (added) {
            advanceDeleteBtn(selector, {
              iconName: 'check-square',
              size: '18px',
              armTitle: 'Click again to remove from this agent',
              armLabel: `Confirm removal of ${visibleModelName} from available models`,
              busyTitle: 'Removing from agent…',
              busyLabel: `Removing ${visibleModelName} from available models`,
              onConfirm: async () => {
                catalogAddBusy = true;
                modelDd.querySelectorAll('.ac-model-row-selector').forEach(control => { control.disabled = true; });
                const previousProviders = [...S.providers];
                const previousRoles = { ...S.roleAssignments };
                S.providers = S.providers.filter(row => row !== selectedRow);
                for (const [role, entryId] of Object.entries(S.roleAssignments)) {
                  if (entryId === selectedRow.entry_id) delete S.roleAssignments[role];
                }
                try {
                  await saveRoster();
                  renderSaved({ animate: true });
                  onModelChange();
                  setModelStatus('');
                } catch (error) {
                  S.providers = previousProviders;
                  S.roleAssignments = previousRoles;
                  setModelStatus(error?.message || 'Unable to remove model');
                } finally {
                  catalogAddBusy = false;
                  renderModelDropdown(modelSearch.value.toLowerCase());
                }
              },
            });
            return;
          }
          catalogAddBusy = true;
          modelDd.querySelectorAll('.ac-model-row-selector').forEach(control => { control.disabled = true; });
          S.selectedModel = m.id;
          S.selCaps = { text: m.text_capable !== false, image: !!m.image_capable, imageOut: !!m.image_out_capable, voice: !!m.voice_capable, video: !!m.video_capable };
          (async () => {
            try {
              const saved = await addModel();
              if (saved) setModelStatus('');
            } catch (error) {
              setModelStatus(error?.message || 'Unable to add model');
            } finally {
              catalogAddBusy = false;
              renderModelDropdown(modelSearch.value.toLowerCase());
            }
          })();
        });
        item.appendChild(selector);
      } else {
        item.addEventListener('click', () => {
          S.selectedModel = m.id; modelSearch.value = isPlatformCatalogModel ? visibleModelName : (m.name || visibleModelName);
          S.selCaps = { text: m.text_capable !== false, image: !!m.image_capable, imageOut: !!m.image_out_capable, voice: !!m.voice_capable, video: !!m.video_capable };
          renderDetectedCaps(m);
          setModelStatus(`Selected: ${visibleModelName}`, true);
          autosaveSingle(modelCheck);
          renderSubscriptionAuth();
          renderModelDropdown(modelSearch.value.toLowerCase());
        });
      }
      modelDd.appendChild(item);
    });
  }

  // ── Provider combobox ──────────────────────────────────────────────────────
  // Mirrors the Model name field: click to open the whole provider list, type to
  // filter it. Committing a choice only sets `provSel` and fires its `change`
  // event, so every existing consequence — the remembered per-provider config,
  // the credential/URL fields, the model reload and the auto-save check — runs
  // exactly as it did for the native dropdown.
  function providerLabel(key) {
    if (!key) return '';
    if (key === '_custom') return 'Custom';
    return S.presets?.[key]?.name || key;
  }

  function syncProviderField() {
    provSel.value = S.currentProvider;
    provInput.value = providerLabel(provSel.value);
    provInput.setAttribute('aria-expanded', provDd.style.display === 'none' ? 'false' : 'true');
  }

  function hideProviderDropdown() {
    provDd.style.display = 'none';
    provInput.setAttribute('aria-expanded', 'false');
  }

  function renderProviderDropdown(filter) {
    const q = String(filter || '').trim().toLowerCase();
    const matches = [...provSel.options].filter(o => !q
      || providerLabel(o.value).toLowerCase().includes(q)
      || String(o.value).toLowerCase().includes(q));
    // A filter that matches nothing closes the list rather than leaving an empty
    // panel, the same way the model field behaves.
    if (!matches.length) { hideProviderDropdown(); return; }
    provDd.innerHTML = '';
    if (provDd.parentElement !== document.body) document.body.appendChild(provDd);
    provDd.style.display = 'block';
    provInput.setAttribute('aria-expanded', 'true');
    matches.forEach((o) => {
      const item = document.createElement('div');
      item.className = 'ac-model-item';
      item.setAttribute('role', 'option');
      if (o.value === provSel.value) {
        item.style.background = 'rgba(var(--brand-rgb),0.12)';
        item.setAttribute('aria-selected', 'true');
      }
      const base = S.presets?.[o.value]?.base_url || '';
      item.innerHTML = `<div class="ac-model-item-row"><span style="font-weight:500;">${esc(providerLabel(o.value))}</span>`
        + (base ? `<span style="color:var(--fg-3);font-size:11px;margin-left:6px;">${esc(base)}</span>` : '')
        + `</div>`;
      // Keep the input focused, so committing never reads as leaving the field.
      item.addEventListener('mousedown', (e) => e.preventDefault());
      item.addEventListener('click', () => commitProvider(o.value));
      provDd.appendChild(item);
    });
    positionDropdown(provDd, provInput, hideProviderDropdown);
  }

  function commitProvider(key) {
    hideProviderDropdown();
    if (provSel.value === key) { syncProviderField(); return; }
    provSel.value = key;
    provSel.dispatchEvent(new Event('change'));   // the existing handler does the rest
  }

  provInput.addEventListener('focus', () => { syncProviderField(); renderProviderDropdown(''); });
  provInput.addEventListener('input', () => renderProviderDropdown(provInput.value));
  provInput.addEventListener('blur', () => { hideProviderDropdown(); syncProviderField(); });
  provInput.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { hideProviderDropdown(); syncProviderField(); provInput.blur(); }
    else if (e.key === 'Enter' && provDd.style.display !== 'none') {
      const first = provDd.querySelector('.ac-model-item');
      if (first) { e.preventDefault(); first.click(); }
    }
  });
  // Same pointer contract as the model field's chevron: the arrow takes the
  // pointer (for its click cursor) and forwards the press to the field.
  provChevron.addEventListener('mousedown', (e) => {
    e.preventDefault();
    if (document.activeElement === provInput) renderProviderDropdown('');
    else provInput.focus();
  });

  // Reflect the in-flight provider-model fetch on the + button (counter-based so
  // overlapping debounced calls don't clear the spinner early).
  function setAddBusy(on) {
    modelsBusy = Math.max(0, modelsBusy + (on ? 1 : -1));
    addToggle.classList.toggle('loading', modelsBusy > 0);
  }
  function isSubscriptionProvider() {
    return S.currentProvider === 'openai_subscription';
  }
  async function subscriptionRequest(path, entryId = '', options = {}) {
    const joiner = path.includes('?') ? '&' : '?';
    const rowPath = entryId
      ? `${path}${joiner}entry_id=${encodeURIComponent(entryId)}`
      : path;
    if (adapter.subscriptionRequest) return adapter.subscriptionRequest(rowPath, options);
    const scopeJoiner = rowPath.includes('?') ? '&' : '?';
    return fetchFn(apiPath(`${rowPath}${scopeJoiner}scope=${encodeURIComponent(subscriptionScope)}`), options);
  }
  function clearSubscriptionCountdown(target) {
    const timer = subscriptionCountdowns.get(target);
    if (timer) {
      clearInterval(timer);
      subscriptionCountdownHandles.delete(timer);
    }
    subscriptionCountdowns.delete(target);
  }
  function renderSubscriptionAuthorization(target, data, {
    onCheck = async () => {},
    onReset = async () => {},
    onExpire = async () => {},
  } = {}) {
    clearSubscriptionCountdown(target);
    target.textContent = '';

    const codeLine = document.createElement('span');
    codeLine.style.cssText = 'display:flex;align-items:center;gap:6px;flex-wrap:wrap;';
    const code = document.createElement('span');
    code.textContent = data.user_code;
    code.title = 'Copy verification code';
    code.tabIndex = 0;
    code.setAttribute('role', 'button');
    code.setAttribute('aria-label', 'Copy verification code ' + data.user_code);
    code.style.cssText = 'font-family:var(--font-mono,monospace);font-weight:600;cursor:pointer;user-select:all;';
    const copy = document.createElement('button');
    copy.type = 'button';
    copy.title = 'Copy verification code';
    copy.setAttribute('aria-label', 'Copy verification code');
    copy.style.cssText = 'border:0;background:none;padding:0;display:inline-flex;align-items:center;color:var(--fg-3);cursor:pointer;';
    copy.innerHTML = '<i data-lucide="copy" style="width:15px;height:15px;"></i>';
    const countdown = document.createElement('span');
    countdown.className = 'ac-hint';
    countdown.style.margin = '0';

    const copyCode = async () => {
      try {
        await copyText(data.user_code);
        countdown.textContent = 'Copied';
      } catch {
        countdown.textContent = 'Copy failed';
      }
    };
    code.addEventListener('click', copyCode);
    code.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        copyCode();
      }
    });
    copy.addEventListener('click', copyCode);
    codeLine.append(code, copy, countdown);

    const open = document.createElement('a');
    open.href = data.verification_url;
    open.target = '_blank';
    open.rel = 'noopener noreferrer';
    open.textContent = 'Open Verification page and paste code';

    const actions = document.createElement('span');
    actions.style.cssText = 'display:flex;gap:8px;flex-wrap:wrap;';
    const check = document.createElement('button');
    check.type = 'button';
    check.className = 'ac-btn ac-btn-primary ac-btn-sm';
    check.textContent = 'Check Authorization';
    const reset = document.createElement('button');
    reset.type = 'button';
    reset.className = 'ac-btn ac-btn-ghost ac-btn-sm';
    reset.textContent = 'Reset';
    check.addEventListener('click', async () => {
      check.disabled = true;
      try { await onCheck(); } finally { check.disabled = false; }
    });
    reset.addEventListener('click', async () => {
      reset.disabled = true;
      try { await onReset(); } finally { reset.disabled = false; }
    });
    actions.append(check, reset);
    target.append(codeLine, open, actions);

    if (window.lucide) {
      try { window.lucide.createIcons({ nodes: Array.from(target.querySelectorAll('[data-lucide]:not(.lucide)')) }); } catch {}
    }

    const ttlSeconds = Math.max(1, Number(data.expires_in_seconds) || 900);
    const expiresAt = Number(data.expires_at) > 0
      ? Number(data.expires_at) * 1000
      : Date.now() + (ttlSeconds * 1000);
    let expired = false;
    const tick = () => {
      const remaining = Math.max(0, Math.ceil((expiresAt - Date.now()) / 1000));
      if (!remaining) {
        if (expired) return;
        expired = true;
        clearSubscriptionCountdown(target);
        Promise.resolve(onExpire()).catch(() => {});
        return;
      }
      const minutes = Math.floor(remaining / 60);
      const seconds = String(remaining % 60).padStart(2, '0');
      countdown.textContent = 'Use within ' + minutes + ':' + seconds;
    };
    tick();
    const timer = setInterval(tick, 1000);
    subscriptionCountdowns.set(target, timer);
    subscriptionCountdownHandles.add(timer);
  }
  function matchingSubscriptionRow() {
    if (pendingSubscriptionRow) return pendingSubscriptionRow;
    const rows = S.providers.filter(row => row.provider === 'openai_subscription');
    if (S.selectedModel) {
      const matches = rows.filter(row => row.model === S.selectedModel);
      if (matches.length) return matches[matches.length - 1];
    }
    return rows.find(row => row.credential_configured) || null;
  }
  let subscriptionVerification = null;
  let subscriptionCheckBusy = false;
  let subscriptionLoginBusy = false;
  let subscriptionDestroyed = false;
  function activeSubscriptionVerification(row = matchingSubscriptionRow()) {
    return row?.entry_id && subscriptionVerification?.entry_id === row.entry_id
      ? subscriptionVerification
      : null;
  }
  function renderSubscriptionAuth(message = '') {
    // OpenRouter Free is an App → Model Catalog authoring shortcut. It must not
    // appear in the user's Available models flow, regardless of provider.
    if (userCatalog) freeBtn.style.display = 'none';
    if (userCatalog && S.currentProvider === 'webagent_catalog') {
      [urlRow, keyRow, subscriptionRow].forEach(row => { row.hidden = true; row.style.display = 'none'; });
      return;
    }
    if (!userCatalog) freeBtn.style.display = '';
    if (catalogReadOnly) {
      // This helper is also called after the initial catalog hide pass. Keep it
      // from restoring Base URL/API Key while reconciling subscription state.
      [urlRow, keyRow, subscriptionRow].forEach(row => { row.hidden = true; row.style.display = 'none'; });
      return;
    }
    const subscription = isSubscriptionProvider();
    // Reuse the API row's credential controls, feedback, and picker in the
    // subscription row; the OAuth secret stays entirely server-side.
    const credentialHost = subscription ? subscriptionActions : keyCtrl;
    [loadCredentialBtn, saveCredentialBtn, credentialSaveCheck, credentialSelect].forEach(control => credentialHost.appendChild(control));
    const vaultName = subscriptionScope === 'agent' ? "this agent's" : subscriptionScope === 'platform' ? "the app's" : 'your personal';
    saveCredentialBtn.title = `Save this validated credential to ${vaultName} vault`;
    updateCredentialSaveState();
    [urlRow, keyRow, subscriptionRow].forEach(row => { row.hidden = false; });
    urlRow.style.display = subscription ? 'none' : '';
    keyRow.style.display = subscription ? 'none' : '';
    subscriptionRow.style.display = subscription ? '' : 'none';
    if (!subscription) {
      clearSubscriptionCountdown(subscriptionState);
      return;
    }
    const row = matchingSubscriptionRow();
    const verification = activeSubscriptionVerification(row);
    const connected = !!row?.credential_configured;
    const email = row?.credential_email || '';
    subscriptionReset.style.display = adapter.resetSubscriptionCredentials && !verification ? '' : 'none';
    subscriptionConnect.disabled = false;
    if (verification) {
      subscriptionConnect.style.display = 'none';
      renderSubscriptionAuthorization(subscriptionState, verification, {
        onCheck: () => refreshSubscriptionAccount(row),
        onReset: () => resetSubscriptionCredentials(),
        onExpire: () => resetSubscriptionLogin(row, { expired: true }),
      });
      return;
    }
    clearSubscriptionCountdown(subscriptionState);
    subscriptionConnect.style.display = '';
    subscriptionConnect.textContent = connected
      ? 'Use other account'
      : 'Retry verification code';
    const connectedLabel = connected
      ? 'Connected to ' + (email || 'ChatGPT account')
        + (row.credential_plan_type ? ' (' + row.credential_plan_type + ')' : '')
      : '';
    subscriptionState.textContent = [connectedLabel, message].filter(Boolean).join(' · ');
  }
  async function refreshSubscriptionAccount(row = matchingSubscriptionRow()) {
    if (subscriptionCheckBusy || subscriptionDestroyed) return;
    if (!row?.entry_id) {
      renderSubscriptionAuth('Issue a verification code first.');
      return;
    }
    subscriptionCheckBusy = true;
    try {
      const response = await subscriptionRequest(
        '/api/v1/openai-subscription/auth/status', row.entry_id,
      );
      const state = response && await response.json().catch(() => ({}));
      if (!response || !response.ok) throw new Error(state.detail || 'Could not check authorization.');
      if (subscriptionDestroyed || row !== matchingSubscriptionRow() || !isSubscriptionProvider()) return;
      row.credential_configured = !!state.chatgpt_authorized;
      row.credential_email = state?.account?.email || '';
      row.credential_plan_type = state?.plan_type || '';
      if (row.credential_configured) {
        subscriptionVerification = null;
        await loadModels();
        setModelStatus('');
        renderSaved();
      } else {
        setModelStatus(state.detail || 'Authorization is not complete yet.');
      }
      if (row.credential_configured) renderSubscriptionAuth();
    } catch (error) {
      setModelStatus(error?.message || 'Could not check authorization.');
    } finally {
      subscriptionCheckBusy = false;
    }
  }
  async function resetSubscriptionLogin(row = matchingSubscriptionRow(), { expired = false } = {}) {
    if (!row?.entry_id) {
      subscriptionVerification = null;
      renderSubscriptionAuth();
      return;
    }
    try {
      const response = await subscriptionRequest(
        '/api/v1/openai-subscription/login/reset', row.entry_id, { method: 'POST' },
      );
      const data = response && await response.json().catch(() => ({}));
      if (!response || (!response.ok && response.status !== 404)) {
        throw new Error(data.detail || 'Could not reset authorization.');
      }
      if (pendingSubscriptionRow?.entry_id === row.entry_id
          && !S.providers.some(item => item.entry_id === row.entry_id)) {
        pendingSubscriptionRow = null;
      }
      subscriptionVerification = null;
      renderSubscriptionAuth(expired ? 'Verification code expired.' : '');
    } catch (error) {
      renderSubscriptionAuth(error?.message || 'Could not reset authorization.');
    }
  }
  async function resetSubscriptionCredentials() {
    if (!adapter.resetSubscriptionCredentials) return;
    subscriptionReset.disabled = true;
    try {
      const cleared = new Set(await adapter.resetSubscriptionCredentials());
      for (const row of S.providers) {
        if (!cleared.has(row.connection_id)) continue;
        row.credential_configured = false;
        row.credential_email = '';
        row.credential_plan_type = '';
      }
      pendingSubscriptionRow = null;
      subscriptionVerification = null;
      renderSaved();
      await loadModels();
      renderSubscriptionAuth('ChatGPT connection reset. Use Retry verification code to reconnect.');
    } catch (error) {
      renderSubscriptionAuth(error?.message || 'Could not reset connection.');
      setModelStatus(error?.message || 'Could not reset connection.');
    } finally {
      subscriptionReset.disabled = false;
    }
  }
  subscriptionReset.addEventListener('click', resetSubscriptionCredentials);
  async function startSubscriptionLogin() {
    if (subscriptionLoginBusy || subscriptionDestroyed) return;
    subscriptionLoginBusy = true;
    subscriptionConnect.disabled = true;
    subscriptionConnect.style.display = 'none';
    subscriptionState.textContent = 'Issuing verification code…';
    try {
      // The add form always authorizes a fresh connection. A matching model
      // name in the saved roster must never rotate that connection's credential.
      let row = pendingSubscriptionRow;
      const response = await subscriptionRequest(
        '/api/v1/openai-subscription/login/start', row?.entry_id || '', { method: 'POST' },
      );
      const data = response && await response.json().catch(() => ({}));
      if (!response || !response.ok) throw new Error(data.detail || 'Could not issue verification code.');
      if (!data.entry_id || !data.verification_url || !data.user_code) {
        throw new Error('Device authorization details were missing');
      }
      if (!row) {
        pendingSubscriptionRow = {
          entry_id: data.entry_id,
          provider: 'openai_subscription',
          auth_mode: 'chatgpt',
          credential_scope: subscriptionScope,
          credential_configured: false,
          credential_email: '',
          credential_plan_type: '',
        };
        row = pendingSubscriptionRow;
      }
      subscriptionVerification = { ...data, entry_id: row.entry_id };
      renderSubscriptionAuth();
    } catch (error) {
      renderSubscriptionAuth(error?.message || 'Authorization failed');
    } finally {
      subscriptionLoginBusy = false;
      subscriptionConnect.disabled = false;
    }
  }
  subscriptionConnect.addEventListener('click', startSubscriptionLogin);
  async function loadModels() {
    const revision = ++modelLoadRevision;
    const provider = S.currentProvider === '_custom' ? '' : S.currentProvider;
    const key = keyInput.value.trim();
    const base = urlInput.value.trim() || S.presets[S.currentProvider]?.base_url || '';
    const platformCatalog = userCatalog && S.currentProvider === 'webagent_catalog';
    if (!isSubscriptionProvider() && !platformCatalog && !key) {
      setModelStatus('Enter an API key to see available models.');
      S.allModels = [];
      renderModelDropdown(modelSearch.value.toLowerCase());
      return;
    }
    setModelStatus('Loading models…');
    setAddBusy(true);
    try {
      const data = await fetchModels({ provider, api_key: key, base_url: base,
        entry_id: isSubscriptionProvider() ? matchingSubscriptionRow()?.entry_id : undefined });
      if (revision !== modelLoadRevision) return;
      if (!data || data.error) {
        setModelStatus(data && data.error === 'No API key configured' ? 'Enter an API key to see available models.' : `Error: ${(data && data.error) || 'failed'}`);
        S.allModels = [];
        renderModelDropdown(modelSearch.value.toLowerCase());
        return;
      }
      S.allModels = data.models || [];
      setModelStatus(data.preview
        ? 'Current GPT-6 models. Connect ChatGPT to confirm account access.'
        : S.allModels.length ? (S.allModels.length + ' models available. Type to filter.')
        : isSubscriptionProvider() ? 'No models available for this ChatGPT account.' : 'No models available.');
      if (S.selectedModel) {
        const sel = S.allModels.find(m => m.id === S.selectedModel);
        if (sel) { S.selCaps = { text: sel.text_capable !== false, image: !!sel.image_capable, imageOut: !!sel.image_out_capable, voice: !!sel.voice_capable, video: !!sel.video_capable }; renderDetectedCaps(sel); }
      }
      renderModelDropdown(modelSearch.value.toLowerCase());
    } catch (error) {
      if (revision !== modelLoadRevision) return;
      S.allModels = [];
      setModelStatus(error?.message || 'Could not load models');
      renderModelDropdown(modelSearch.value.toLowerCase());
    } finally {
      setAddBusy(false);
    }
  }
  function scheduleFetch() { clearTimeout(fetchDebounce); fetchDebounce = setTimeout(loadModels, 500); }

  // Remember the configurator's current single config under its provider key.
  function rememberProvider(key) {
    if (!key || key === '_custom') return;
    S.providerConfigs[key] = { api_key: keyInput.value || '', model: S.selectedModel, base_url: urlInput.value.trim() || '' };
  }

  // Auto-save the configurator (provider/url/key/model + detected caps) as the
  // single "default" config. Debounced; flashes `checkEl` on confirm.
  function autosaveSingle(checkEl) {
    rememberProvider(S.currentProvider);
    if (!adapter.saveSingle) return;
    const single = {
      provider: S.currentProvider === '_custom' ? 'custom' : S.currentProvider,
      auth_mode: isSubscriptionProvider() ? 'chatgpt' : 'api_key',
      base_url: urlInput.value.trim(),
      api_key: keyInput.value.trim(),
      model: S.selectedModel || '',
      providers: S.providerConfigs,
      text_capable: S.selCaps.text, image_capable: S.selCaps.image,
      image_out_capable: S.selCaps.imageOut,
      voice_capable: S.selCaps.voice,
    };
    if (!single.api_key && !isSubscriptionProvider()) return; // nothing persistable yet
    if (isSubscriptionProvider() && !single.model) return;
    markSaving(checkEl);
    Promise.resolve(adapter.saveSingle(single))
      .then(() => flashCheck(checkEl, true))
      .catch(e => flashCheck(checkEl, false, e.message));
  }
  function scheduleAutosave(checkEl) { clearTimeout(autosaveDebounce); autosaveDebounce = setTimeout(() => autosaveSingle(checkEl), 700); }

  async function credentialResponse(response) {
    const data = response && await response.json().catch(() => ({}));
    if (!response || !response.ok) throw new Error(data?.detail?.message || data?.detail || `HTTP ${response?.status || 0}`);
    return data;
  }
  async function listSavedCredentials() {
    if (adapter.listCredentials) return adapter.listCredentials();
    return credentialResponse(await subscriptionRequest('/api/v1/entitlements/me/model-credentials'));
  }
  async function loadSavedCredential(label) {
    if (adapter.loadCredential) return adapter.loadCredential(label);
    return credentialResponse(await subscriptionRequest(`/api/v1/entitlements/me/model-credentials/${encodeURIComponent(label)}`));
  }
  async function saveSavedCredential(payload) {
    if (adapter.saveCredential) return adapter.saveCredential(payload);
    return credentialResponse(await subscriptionRequest('/api/v1/entitlements/me/model-credentials', '', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload),
    }));
  }
  async function deleteSavedCredential(label) {
    if (adapter.deleteCredential) return adapter.deleteCredential(label);
    return credentialResponse(await subscriptionRequest(`/api/v1/entitlements/me/model-credentials/${encodeURIComponent(label)}`, '', { method: 'DELETE' }));
  }
  const currentCredentialProvider = () => S.currentProvider === '_custom' ? 'custom' : S.currentProvider;
  async function renderSavedCredentials() {
    savedCredentialsBody.replaceChildren();
    const loading = document.createElement('span'); loading.className = 'ac-hint'; loading.textContent = 'Loading saved credentials…';
    savedCredentialsBody.appendChild(loading);
    let credentials = [];
    try {
      const data = await listSavedCredentials();
      credentials = Array.isArray(data?.credentials) ? data.credentials : [];
    } catch (error) {
      loading.textContent = `Unable to load saved credentials: ${error.message || 'request failed'}`;
      return [];
    }
    savedCredentialsBody.replaceChildren();
    if (!credentials.length) {
      const empty = document.createElement('span'); empty.className = 'ac-hint'; empty.textContent = 'No saved credentials yet.';
      savedCredentialsBody.appendChild(empty);
      return credentials;
    }
    credentials.forEach((credential) => {
      const row = document.createElement('div');
      row.className = 'ac-ability-row ac-row-static';
      const label = document.createElement('span'); label.className = 'ac-ability-label';
      const name = document.createElement('span'); name.className = 'ac-ability-name'; name.textContent = credential.label || 'Unnamed credential';
      const details = document.createElement('span'); details.className = 'ac-ability-desc';
      if (credential.kind === 'subscription') {
        details.textContent = `ChatGPT subscription${credential.credential_email ? ` · ${credential.credential_email}` : ''}${credential.credential_plan_type ? ` · ${credential.credential_plan_type}` : ''}${credential.credential_configured ? ' · Connected' : ' · Not connected'}`;
      } else {
        details.textContent = `${credential.provider || 'Provider'}${credential.base_url ? ` · ${credential.base_url}` : ''}${credential.credential_suffix ? ` · ••••${credential.credential_suffix}` : ''}`;
      }
      label.append(name, details); row.appendChild(label);
      if (credential.kind === 'api_key') {
        const remove = document.createElement('button'); remove.type = 'button'; remove.className = 'ac-btn ac-btn-ghost ac-btn-sm'; remove.textContent = 'Remove';
        remove.addEventListener('click', async () => {
          if (!window.confirm(`Remove saved credential “${credential.label}”?`)) return;
          remove.disabled = true;
          try { await deleteSavedCredential(credential.label); await renderSavedCredentials(); setModelStatus('Saved credential removed.', true); }
          catch (error) { setModelStatus(error.message || 'Could not remove credential.'); remove.disabled = false; }
        });
        row.appendChild(remove);
      }
      savedCredentialsBody.appendChild(row);
    });
    return credentials;
  }
  async function showCredentialPicker() {
    const provider = currentCredentialProvider();
    if (!provider || provider === 'webagent_catalog') {
      setModelStatus('Select a provider to load its saved credentials.');
      return;
    }
    loadCredentialBtn.disabled = true;
    try {
      const data = isSubscriptionProvider()
        ? await credentialResponse(await subscriptionRequest('/api/v1/openai-subscription/credentials'))
        : await listSavedCredentials();
      const kind = isSubscriptionProvider() ? 'subscription' : 'api_key';
      const matches = (Array.isArray(data?.credentials) ? data.credentials : []).filter((credential) => credential.kind === kind && credential.provider === provider);
      credentialSelect.replaceChildren();
      const placeholder = document.createElement('option'); placeholder.value = ''; placeholder.textContent = matches.length ? 'Select saved credential…' : `No saved ${provider} credentials`;
      credentialSelect.appendChild(placeholder);
      matches.forEach((credential) => {
        const option = document.createElement('option'); option.value = credential.label;
        option.textContent = `${credential.label}${credential.credential_email ? ` (${credential.credential_email})` : ''}${credential.credential_suffix ? ` (••••${credential.credential_suffix})` : ''}`;
        credentialSelect.appendChild(option);
      });
      credentialSelect.hidden = false;
      if (!matches.length) setModelStatus(`No saved credentials for ${provider}.`);
      else credentialSelect.focus();
    } catch (error) { setModelStatus(error.message || 'Could not load saved credentials.'); }
    finally { loadCredentialBtn.disabled = false; }
  }
  async function applySavedCredential(label) {
    if (!label) return;
    credentialSelect.disabled = true;
    try {
      if (isSubscriptionProvider()) {
        const data = await credentialResponse(await subscriptionRequest(
          '/api/v1/openai-subscription/credentials/load', '', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ label }),
          },
        ));
        if (!data.entry_id || !data.credential?.credential_configured) throw new Error('Saved subscription is unavailable.');
        pendingSubscriptionRow = {
          entry_id: data.entry_id, provider: 'openai_subscription', auth_mode: 'chatgpt',
          credential_scope: subscriptionScope, credential_configured: true,
          credential_email: data.credential.credential_email || '',
          credential_plan_type: data.credential.credential_plan_type || '',
        };
        subscriptionVerification = null;
        credentialSelect.hidden = true; credentialSelect.value = '';
        renderSubscriptionAuth();
        setModelStatus(`Loaded saved credential “${data.credential.label}”.`, true);
        await loadModels();
        return;
      }
      const data = await loadSavedCredential(label);
      const credential = data?.credential || {};
      if (!data?.api_key) throw new Error('Saved credential is unavailable.');
      keyInput.value = data.api_key;
      if (credential.base_url) urlInput.value = credential.base_url;
      credentialSelect.hidden = true; credentialSelect.value = '';
      keyInput.dispatchEvent(new Event('input', { bubbles: true }));
      if (credential.base_url) urlInput.dispatchEvent(new Event('input', { bubbles: true }));
      setModelStatus(`Loaded saved credential “${credential.label}”.`, true);
    } catch (error) { setModelStatus(error.message || 'Could not load saved credential.'); }
    finally { credentialSelect.disabled = false; }
  }
  function pulseAdvancedCredential() {
    clearTimeout(advToggle._credentialPulseT);
    advToggle.classList.remove('ac-model-credential-pulse');
    // Restart the animation when a user saves again before its previous cue ends.
    void advToggle.offsetWidth;
    advToggle.classList.add('ac-model-credential-pulse');
    advToggle._credentialPulseT = setTimeout(() => advToggle.classList.remove('ac-model-credential-pulse'), 1800);
  }
  function credentialFingerprint() {
    return JSON.stringify([S.currentProvider, urlInput.value.trim(), keyInput.value.trim()]);
  }
  function updateCredentialSaveState() {
    saveCredentialBtn.disabled = isSubscriptionProvider()
      ? !matchingSubscriptionRow()?.credential_configured || !!activeSubscriptionVerification()
      : !keyInput.value.trim() || !urlInput.value.trim() || validatedCredential !== credentialFingerprint();
  }
  function scheduleCredentialValidation() {
    validatedCredential = '';
    updateCredentialSaveState();
    clearTimeout(credentialValidationTimer);
    if (isSubscriptionProvider() || !keyInput.value.trim() || !urlInput.value.trim()) return;
    const fingerprint = credentialFingerprint();
    credentialValidationTimer = setTimeout(async () => {
      try {
        const data = await credentialResponse(await subscriptionRequest(
          '/api/v1/entitlements/me/model-credentials/validate', '', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              label: 'Credential validation', provider: currentCredentialProvider(),
              base_url: urlInput.value.trim(), api_key: keyInput.value.trim(),
            }),
          },
        ));
        if (data.validated && credentialFingerprint() === fingerprint) validatedCredential = fingerprint;
      } catch (_) { /* Save remains disabled until this exact credential validates. */ }
      if (credentialFingerprint() === fingerprint) updateCredentialSaveState();
    }, 500);
  }

  async function saveCurrentSubscriptionCredential() {
    const row = matchingSubscriptionRow();
    if (!row?.entry_id || !row.credential_configured || activeSubscriptionVerification(row)) return setModelStatus('Connect and validate the subscription before saving its credential.');
    const label = `${S.presets[S.currentProvider]?.name || 'ChatGPT Subscription'} credential`;
    saveCredentialBtn.disabled = true;
    try {
      const data = await credentialResponse(await subscriptionRequest(
        '/api/v1/openai-subscription/credentials/save', row.entry_id, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ label }),
        },
      ));
      flashCheck(credentialSaveCheck, true);
      pulseAdvancedCredential();
      setModelStatus(`Saved credential “${data?.credential?.label || label}”.`, true);
      if (advRow.classList.contains('expanded')) await renderSavedCredentials();
    } catch (error) {
      row.credential_configured = false;
      renderSubscriptionAuth();
      setModelStatus(error.message || 'Could not save subscription credential.');
    }
    finally { updateCredentialSaveState(); }
  }

  async function saveCurrentCredential() {
    if (isSubscriptionProvider()) return saveCurrentSubscriptionCredential();
    const apiKey = keyInput.value.trim();
    const provider = currentCredentialProvider();
    if (!apiKey || validatedCredential !== credentialFingerprint()) return setModelStatus('Enter and validate the provider credential before saving.');
    if (!provider || provider === 'webagent_catalog' || isSubscriptionProvider()) return setModelStatus('Select an API-key provider before saving.');
    const label = `${S.presets[provider]?.name || provider} credential`;
    saveCredentialBtn.disabled = true;
    try {
      const data = await saveSavedCredential({ label, provider, base_url: urlInput.value.trim(), api_key: apiKey });
      flashCheck(credentialSaveCheck, true);
      pulseAdvancedCredential();
      setModelStatus(`Saved credential “${data?.credential?.label || label}”.`, true);
      if (advRow.classList.contains('expanded')) await renderSavedCredentials();
    } catch (error) {
      scheduleCredentialValidation();
      setModelStatus(error.message || 'Could not save credential.');
    }
    finally { updateCredentialSaveState(); }
  }

  // ── Saved-models (roster) rendering ──
  // readOnly rows are inherited app-default models (shown for reference on the
  // per-agent table): their capability boxes are disabled and they carry no
  // delete control.
  function capCell(capable, checked, title, onChange, readOnly) {
    const cell = document.createElement('span');
    cell.className = 'ac-saved-cap';
    cell.addEventListener('click', e => e.stopPropagation());
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = capable && checked; cb.title = title;
    cb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;';
    // A model that doesn't support this capability (or an inherited app-default row)
    // shows the box GREYED + disabled rather than an empty slot, so every column
    // reads as a real (just locked) control.
    if (!capable) { cb.disabled = true; cb.checked = false; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = title + ' (not supported by this model)'; }
    else if (readOnly) { cb.disabled = true; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = title + ' (app default — read-only)'; }
    else cb.addEventListener('change', () => onChange(cb.checked, cb));
    cell.appendChild(cb); return cell;
  }
  function usageLine(title) {
    const line = document.createElement('span'); line.className = 'ac-saved-usage-line'; line.title = title;
    const val = document.createElement('span'); val.className = 'ac-saved-usage-v'; val.textContent = '—';
    line.appendChild(val); return { line, val };
  }
  // Canonical cost string: prefer the published-price total_cost_usd (computed
  // per call at its own model's rate, summed across chat + background), falling
  // back to the provider-billed cents only when no USD figure is present.
  function fmtUsageCost(u) {
    if (u && typeof u.total_cost_usd === 'number' && u.total_cost_usd > 0) {
      return u.total_cost_usd >= 1 ? `$${u.total_cost_usd.toFixed(2)}` : `$${u.total_cost_usd.toFixed(4)}`;
    }
    const c = (u && u.total_cost_cents) || 0;
    return c >= 100 ? `$${(c / 100).toFixed(2)}` : `${c}¢`;
  }
  // Build one role toggle row inside the floating popover: label + capability checkbox.
  // Mirrors the look of the session-dropdown more-menu info rows.
  // `locked` disables the checkbox; `lockHint` explains WHY it's locked (or, for
  // an editable inherited row, what editing it does).
  function _buildRoleToggle(parent, capable, checked, label, tooltip, onChange, locked, lockHint, incapableHint) {
    const row = document.createElement('div'); row.className = 'ac-saved-role-row';
    const lbl = document.createElement('span'); lbl.className = 'ac-saved-role-label';
    lbl.textContent = label;
    const cb = document.createElement('input');
    cb.type = 'checkbox'; cb.checked = capable && checked; cb.title = tooltip;
    cb.style.cssText = 'width:15px;height:15px;accent-color:var(--accent);cursor:pointer;flex-shrink:0;';
    if (!capable) { cb.disabled = true; cb.checked = false; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = tooltip + ' (' + (incapableHint || 'not supported by this model') + ')'; }
    else if (locked) { cb.disabled = true; cb.style.cursor = 'default'; cb.style.opacity = '0.4'; cb.title = tooltip + (lockHint || ' (app default — read-only)'); }
    else cb.addEventListener('change', () => onChange(cb.checked, cb));
    row.appendChild(lbl); row.appendChild(cb);
    parent.appendChild(row);
  }

  // Keep the fixed roles menu inside the visible viewport. Fixed positioning
  // lets it escape short/scrolling model-table ancestors; the menu opens on
  // whichever side of its button has more usable room and scrolls internally
  // when neither side can fit its full content.
  function _positionRolesPopover(button, popover) {
    if (!button || !popover || popover.hidden) return;
    const margin = 8;
    const gap = 4;
    popover.style.top = '0px';
    popover.style.left = '0px';
    popover.style.right = 'auto';
    popover.style.bottom = 'auto';
    popover.style.maxHeight = 'none';
    popover.style.overflowY = 'hidden';

    const anchor = button.getBoundingClientRect();
    const width = popover.offsetWidth;
    const naturalHeight = popover.scrollHeight;
    const below = Math.max(0, window.innerHeight - anchor.bottom - gap - margin);
    const above = Math.max(0, anchor.top - gap - margin);
    const openBelow = below >= Math.min(naturalHeight, 160) || below >= above;
    const available = Math.max(64, openBelow ? below : above);
    const maxHeight = Math.min(320, available);
    const renderedHeight = Math.min(naturalHeight, maxHeight);
    const maxLeft = Math.max(margin, window.innerWidth - width - margin);
    const left = Math.min(Math.max(margin, anchor.right - width), maxLeft);
    const top = openBelow
      ? Math.min(window.innerHeight - margin - renderedHeight, anchor.bottom + gap)
      : Math.max(margin, anchor.top - gap - renderedHeight);

    popover.style.left = Math.round(left) + 'px';
    popover.style.top = Math.round(Math.max(margin, top)) + 'px';
    popover.style.maxHeight = Math.round(maxHeight) + 'px';
    popover.style.overflowY = naturalHeight > maxHeight ? 'auto' : 'visible';
  }
  const usageCache = {}, metaCache = {};
  // A model can appear as BOTH an inherited (app-wide) row and the agent's own
  // row. Those are separate usage calculations — the inherited row reports the
  // app-wide total, the own row reports just this agent's spend — so the cache
  // key (and the fetch) are scoped by `inherited` to keep them apart.
  // MODEL-ROW-USAGE: identity is the saved row's entry_id, NOT provider+model.
  // The same model can be saved twice with different credentials / effort /
  // injected prompts, and each row must accrue its own tokens and cost. A row
  // with no persisted entry_id (still unsaved) falls back to provider+model —
  // it has no identity to split on yet.
  function _usageKey(p, inherited) {
    const id = String(p?.entry_id || '');
    const scope = inherited ? 'inh' : 'own';
    return id
      ? `usage|${scope}|id|${id}`
      : `usage|${scope}|mp|${p?.provider || ''}|${p?.model || ''}`;
  }
  async function fillUsage(p, statIn, statOut, statCost, inherited) {
    const k = _usageKey(p, inherited);
    if (!usageCache[k]) {
      // Rows written before entry_id existed carry no row identity. Fold those
      // legacy events in only when this is the ONLY roster row for the model —
      // with two same-model rows the unassigned bucket would be double-counted.
      const sameModel = (S.providers || []).filter(row =>
        String(row.provider || '') === String(p.provider || '')
        && String(row.model || '') === String(p.model || ''));
      const includeUnassigned = !!p.entry_id && sameModel.length <= 1;
      const d = await fetchModelUsage({
        model: p.model, provider: p.provider, entry_id: p.entry_id || '',
        inherited: !!inherited, include_unassigned: includeUnassigned,
      });
      if (d) usageCache[k] = d;
    }
    const u = usageCache[k] || {};
    if (u.total_input_tokens != null) {
      if (statIn) statIn.textContent = fmtTokens(u.total_input_tokens);
      if (statOut) statOut.textContent = fmtTokens(u.total_output_tokens);
      if (statCost) statCost.textContent = fmtUsageCost(u);
    }
  }

  // Saved rows are rebuilt when their role-based sort order changes. Keep their
  // stable _uid positions so those rebuilds can use a FLIP-style transition:
  // existing rows slide to their new slots while a newly added row fades in.
  // The advanced action row is included because it moves with the list length.
  const SAVED_MOTION_MS = 220;
  function _savedMotionEnabled() {
    return typeof window !== 'undefined'
      && typeof window.matchMedia === 'function'
      && !window.matchMedia('(prefers-reduced-motion: reduce)').matches
      && typeof advRow.animate === 'function';
  }
  function _savedMotionNodes() {
    return [
      ...table.querySelectorAll('.ac-saved-injected[data-mt-uid]'),
      advRow,
    ];
  }
  function _savedMotionKey(node) {
    return node === advRow ? '__advanced__' : String(node.dataset.mtUid || '');
  }
  function _snapshotSavedLayout() {
    const positions = new Map();
    _savedMotionNodes().forEach(node => positions.set(_savedMotionKey(node), node.getBoundingClientRect()));
    return positions;
  }
  function _animateSavedLayout(before, enteringUids = []) {
    if (!_savedMotionEnabled()) return;
    const entering = new Set(enteringUids.map(String));
    _savedMotionNodes().forEach(node => {
      const key = _savedMotionKey(node);
      if (entering.has(key)) {
        node.animate(
          [{ opacity: 0, transform: 'translateY(6px)' }, { opacity: 1, transform: 'translateY(0)' }],
          { duration: SAVED_MOTION_MS, easing: 'ease-out' },
        );
        return;
      }
      const previous = before.get(key);
      if (!previous) return;
      const current = node.getBoundingClientRect();
      const deltaY = previous.top - current.top;
      if (Math.abs(deltaY) < 0.5) return;
      node.animate(
        [{ transform: `translateY(${deltaY}px)` }, { transform: 'translateY(0)' }],
        { duration: SAVED_MOTION_MS, easing: 'cubic-bezier(.2,.8,.2,1)' },
      );
    });
  }
  function _fadeSavedRow(node, fadeIn = false) {
    if (!node || !_savedMotionEnabled()) return Promise.resolve();
    node.getAnimations().forEach(animation => animation.cancel());
    const frames = fadeIn
      ? [{ opacity: 0, transform: 'translateX(-8px)' }, { opacity: 1, transform: 'translateX(0)' }]
      : [{ opacity: 1, transform: 'translateX(0)' }, { opacity: 0, transform: 'translateX(-8px)' }];
    const animation = node.animate(frames, {
      duration: 180,
      easing: 'ease-out',
      fill: fadeIn ? 'none' : 'forwards',
    });
    return animation.finished.catch(() => {});
  }
  // A rename happens INSIDE an expanded detail, but renderSaved() rebuilds every
  // row collapsed. Re-find the row by uid, re-expand it and re-open its detail —
  // the same re-open the roles popover uses — returning the rebuilt Display-name
  // line so the caller can settle its ✓ on the fresh DOM (null when unreachable).
  async function _reopenSavedRow(uid) {
    const wrap = table.querySelector(`.ac-saved-injected[data-mt-uid="${uid}"]`);
    const provider = S.providers.find(x => x._uid === uid);
    const body = wrap && wrap.querySelector('.ac-ability-body');
    if (!wrap || !provider || !body) return null;
    wrap.classList.add('expanded');
    const stats = wrap.querySelectorAll('.ac-saved-usage-v');
    await loadSavedDetail(provider, body, stats[0], stats[1], stats[2], false);
    return body.querySelector('.ac-model-meta-nameline');
  }
  async function loadSavedDetail(p, body, statIn, statOut, statCost, readOnly) {
    await fillUsage(p, statIn, statOut, statCost, readOnly);
    const usage = usageCache[_usageKey(p, readOnly)] || {};
    if (body.dataset.loaded === '1') return;
    body.dataset.loaded = '1';
    if (catalogReadOnly) {
      const origin = p.catalog_origin === 'byo' ? 'Your BYO catalog' : 'Your entitled app catalog';
      body.innerHTML = `<div class="ac-hint" style="margin:0;">${esc(origin)} entry. Add it to an agent from that agent’s Config → Model page; roles and credentials are never configured here.</div>`;
      return;
    }
    if (readOnly) {
      body.innerHTML = '<div class="ac-hint" style="margin:0;">Inherited model details are managed by the app administrator.</div>';
      return;
    }
    const subscriptionAuth = p.provider === 'openai_subscription' || p.auth_mode === 'chatgpt';
    if (subscriptionAuth && p.entry_id && !p.credential_email) {
      try {
        const response = await subscriptionRequest(
          '/api/v1/openai-subscription/auth/status', p.entry_id,
        );
        const state = response && await response.json().catch(() => ({}));
        if (response?.ok) {
          p.credential_configured = !!state.chatgpt_authorized;
          p.credential_email = state?.account?.email || '';
          p.credential_plan_type = state?.plan_type || '';
        }
      } catch (_) {
        // The detail still renders a useful disconnected state when status fails.
      }
    }
    const ck = `${p.provider || ''}|${p.model || ''}`;
    let meta = metaCache[ck];
    if (!meta) { meta = await fetchModelInfo(p.model, p.provider); metaCache[ck] = meta; }
    // ── MODEL-META-LINES ────────────────────────────────────────────────────
    // The identity trio below (Actual model / Provider / Auth) is the REFERENCE
    // format for this entire panel: a muted grey label followed by the value in
    // the bright detail tone. Specs, token usage + cost, capabilities and the
    // editable Display name all render through these two helpers, so a new field
    // can never grow its own look. Styles: MODEL-META-LINES in app3.css.
    const metaLine = (label, valueHtml) =>
      `<div class="ac-model-meta-line"><span class="ac-model-meta-k">${esc(label)}</span> ${valueHtml}</div>`;
    const metaValue = (text, cls) => `<span class="ac-model-meta-v${cls || ''}">${esc(text)}</span>`;

    const ctx = (typeof meta.context === 'number') ? fmtContext(meta.context).replace(' ctx', '') : '';
    const out = (typeof meta.max_output === 'number') ? fmtContext(meta.max_output).replace(' ctx', '') : '';
    // The admin credit-rate editor further down still states the catalog's own USD
    // figure, so `price` stays the raw meta price and is never an override.
    const price = fmtPrice(meta.cost_input, meta.cost_output);
    // ── MODEL-PRICE-EDIT ────────────────────────────────────────────────────
    // The price is a REFERENCE figure, and who owns it decides its unit and whether
    // it can be changed at all:
    //   • a catalog row's price is set by the app admin and shown in house credits —
    //     the buyer may read it and can never edit it;
    //   • a bring-your-own row's price is the owner's own estimate, shown in dollars
    //     and click-to-edit, stored on the row as reference_cost_input/_output.
    const catalogRow = isCatalogRow(p);
    const refIn = numericCost(p.reference_cost_input);
    const refOut = numericCost(p.reference_cost_output);
    const ownPrice = fmtPrice(refIn, refOut);
    const priceLine = (valueHtml) =>
      `<div class="ac-model-meta-line ac-model-meta-priceline"><span class="ac-model-meta-k">Price:</span> ${valueHtml}</div>`;
    let priceHtml = '';
    if (catalogRow) {
      const credits = fmtCredits(
        numericCost(p.credit_input_per_million)
          ?? (typeof meta.cost_input === 'number' ? meta.cost_input * 1000 : null),
        numericCost(p.credit_output_per_million)
          ?? (typeof meta.cost_output === 'number' ? meta.cost_output * 1000 : null),
      );
      const shown = credits || price;
      if (shown) {
        priceHtml = priceLine(metaValue(shown)
          + '<span class="ac-model-price-note">set by the app admin</span>');
      }
    } else {
      priceHtml = priceLine(
        '<span class="ac-model-meta-v ac-model-meta-editable" role="button" tabindex="0"'
        + ' title="Click to set your own reference price">'
        + `${esc(ownPrice || price || '—')}</span>`);
    }
    // The old spec pills became labelled lines: the label now carries the meaning
    // ("Max output"), the value only the number — the same shape as the trio above.
    const specHtml = [
      ctx ? metaLine('Context:', metaValue(ctx)) : '',
      out ? metaLine('Max output:', metaValue(out)) : '',
      priceHtml,
    ].filter(Boolean).join('');
    // Usage is a plain summary value. The per-row cost-trend chart that used to
    // expand from this line is gone — the row's own totals already sit in the
    // Usage column, and the app-wide trend lives on the Models analytics chart.
    let usageHtml = '';
    if (usage.total_input_tokens != null) {
      const inT = usage.total_input_tokens || 0, outT = usage.total_output_tokens || 0;
      const scopeLabel = (usageScope === 'global' || readOnly) ? ' (all agents & users)' : '';
      const usageSummary = `${fmtTokens(inT)} in · ${fmtTokens(outT)} out · ${fmtUsageCost(usage)}`;
      usageHtml = metaLine(`Usage${scopeLabel}:`, metaValue(usageSummary));
    }
    // Capabilities are the five user-facing classes — Text / Vision / Image /
    // Voice / Video. They are NOT roster roles (one text model can serve several
    // roles at once), so no Standard/Premium/System or "Text +" label belongs
    // here. The on/off meaning stays: a supported capability is a bright value,
    // an unsupported one drops to the muted label tone.
    const capHtml = [
      ['Decision', !!p.decision_capable],
      ['Text', p.text_capable !== false],
      ['Vision', !!p.image_capable],
      ['Image', !!p.image_out_capable],
      ['Voice', !!p.voice_capable],
      ['Video', !!p.video_capable],
    ].map(([name, on]) => metaValue(name, on ? '' : ' is-off')).join('<span class="ac-model-meta-sep"> · </span>');
    const capsHtml = metaLine('Capabilities:', capHtml);
    const desc = meta.description ? `<div class="ac-model-meta-desc" style="margin-top:8px;">${esc(meta.description)}</div>` : '';
    // Provider and true model identifiers are visible only in the owning row's
    // expanded details. Inherited rows return above before either is rendered.
    // MODEL-CATALOG-IDENTITY: catalog rows are branded as the WebAgent Catalog and
    // never disclose the real upstream provider.
    const provName = isCatalogRow(p) ? catalogProviderName() : (S.presets[p.provider]?.name || p.provider || 'custom');
    const automaticRouting = p.routing_mode === 'openrouter_free'
      ? '<div class="ac-hint" style="margin:8px 0 0;"><strong>Automatically routed:</strong> OpenRouter selects a currently available free model for each request. This slot is not pinned to a static model.</div>'
      : '';
    // MODEL-NAME-LEVELS: the Model name line is the model's canonical name — the
    // catalog's alias for a catalog row, the real model id for a BYO row. A catalog
    // row never prints its real upstream id. The Auth line is gone: catalog
    // credentials are app-managed and a BYO key belongs to its own credential
    // control, so this panel only names models.
    const identityHtml = metaLine('Model name:', metaValue(modelNameOf(p)))
      + metaLine('Provider:', metaValue(provName));
    // One continuous list — model name → provider → specs → usage/cost →
    // capabilities → the editable Display name — so every row shares the same
    // grey-label / bright-value pair. The Display name is the user's own alias
    // layered over the model name; the value is a click target (startAliasEdit).
    const aliasLineHtml = `<div class="ac-model-meta-line ac-model-meta-nameline">`
      + `<span class="ac-model-meta-k">Display name:</span> `
      + `<span class="ac-model-meta-v ac-model-meta-editable" role="button" tabindex="0" title="Click to rename">`
      + `${esc(userDisplayName(p) || '—')}</span></div>`;
    // DEFAULT-FOR-NEW-AGENTS (grep MODEL-DEFAULT-ACCOUNT): the account-level
    // catalog default — which model a newly created agent starts on — is a
    // per-user choice, not a roster role, so it no longer shares the ⋯ roles
    // popover. It renders as one more line in this unified detail list, directly
    // under Display name, on every user-catalog mount (an individual agent's
    // Model page and the account catalog alike).
    const defaultLineHtml = userCatalog
      ? `<div class="ac-model-meta-line ac-model-meta-defaultline">`
        + `<span class="ac-model-meta-k">Default for new agents:</span> `
        + `<label class="ac-model-default-choice" title="New agents will start with this model selected">`
        + `<input type="checkbox" class="ac-model-default-check" data-entry-id="${esc(String(p.entry_id || ''))}"`
        + `${S.defaultEntryId === String(p.entry_id || '') ? ' checked' : ''}>`
        + `<span>New agents start on this model</span></label></div>`
      : '';
    // The "no data" hint keys off the model's own published figures, not off the
    // price line: a BYO row always renders that line so its owner can set an estimate.
    const hasModelSpec = !!(ctx || out || price);
    body.innerHTML = automaticRouting + `<div class="ac-model-meta-list">${identityHtml + specHtml + usageHtml + capsHtml + aliasLineHtml + defaultLineHtml}</div>` + desc
      + (hasModelSpec ? '' : '<div class="ac-hint" style="margin:8px 0 0;">No token or cost data available for this model.</div>');

    // MODEL-ALIASES: relationship IDs are independent of provider model names
    // and display names. List members in roster order (the runtime backup order).
    const backupLine = document.createElement('div');
    backupLine.className = 'ac-model-meta-line ac-model-meta-aliases';
    const aliasLabel = document.createElement('span');
    aliasLabel.className = 'ac-model-meta-k'; aliasLabel.textContent = 'Aliases: ';
    const aliasAdd = document.createElement('button');
    aliasAdd.type = 'button'; aliasAdd.className = 'ac-btn ac-btn-ghost ac-btn-sm ac-model-alias-add';
    aliasAdd.textContent = 'Add backup connection';
    aliasAdd.setAttribute('aria-expanded', 'false');
    aliasAdd.disabled = !p.entry_id;
    const aliasRegion = document.createElement('div');
    aliasRegion.className = 'ac-model-alias-region';
    const groupId = String(p.alias_group_id || '');
    const members = groupId ? S.providers.filter(other => other !== p && String(other.alias_group_id || '') === groupId) : [];
    if (members.length) {
      const list = document.createElement('div'); list.className = 'ac-list ac-model-alias-members';
      list.setAttribute('aria-label', 'Alias group connections');
      members.forEach(member => {
        const row = document.createElement('div'); row.className = 'ac-ability-row';
        const name = document.createElement('button');
        name.type = 'button'; name.className = 'ac-btn ac-btn-ghost ac-btn-sm';
        name.textContent = rowDisplayName(member);
        name.title = 'Open this model connection';
        name.addEventListener('click', () => {
          closeAliasForm();
          const target = table.querySelector(`.ac-saved-injected[data-mt-uid="${member._uid}"]`);
          if (target && !target.classList.contains('expanded')) target.querySelector('.ac-saved-name-click')?.click();
          target?.scrollIntoView({ block: 'nearest' });
        });
        const unlink = document.createElement('button');
        unlink.type = 'button'; unlink.className = 'ac-btn ac-btn-ghost ac-btn-sm'; unlink.textContent = 'Unlink';
        unlink.title = 'Keep the model connection, but remove it from this alias group';
        unlink.addEventListener('click', async () => {
          const before = S.providers.map(row => [row, row.alias_group_id]);
          member.alias_group_id = '';
          const remaining = S.providers.filter(row => row.alias_group_id === groupId);
          if (remaining.length < 2) remaining.forEach(row => { row.alias_group_id = ''; });
          unlink.disabled = true; _markSaving(unlink);
          try { await saveRoster(); renderSaved(); onModelChange(); }
          catch (error) {
            before.forEach(([row, value]) => { row.alias_group_id = value; });
            unlink.disabled = false; _flashSaveCheck(unlink, false, error?.message || 'Not saved');
          }
        });
        row.append(name, unlink); list.appendChild(row);
      });
      aliasRegion.appendChild(list);
    }
    const aliasHint = document.createElement('div'); aliasHint.className = 'ac-hint';
    aliasHint.textContent = members.length
      ? 'Connections in this group are tried in roster order when the selected model fails before a response starts.'
      : 'Add another model connection as a backup for this model.';
    aliasRegion.appendChild(aliasHint);
    backupLine.append(aliasLabel, aliasAdd);
    body.querySelector('.ac-model-meta-list').appendChild(backupLine);
    body.appendChild(aliasRegion);
    aliasAdd.addEventListener('click', () => openAliasForm(p, aliasRegion, aliasAdd));

    // ── MODEL-PRICE-EDIT: the BYO price is click-to-edit ────────────────────
    // One click swaps the value for a PAIR of rate fields, because a price is a pair
    // (RENAME-FIELD PATTERN: Enter commits, Escape cancels, blur commits, and a `done`
    // guard stops a double commit). Clearing both drops the override, so the line
    // falls back to whatever the model catalog itself publishes. Catalog rows never
    // reach here — their price line carries no editable value.
    const priceLineEl = body.querySelector('.ac-model-meta-priceline');
    const priceValueEl = priceLineEl ? priceLineEl.querySelector('.ac-model-meta-editable') : null;
    if (priceValueEl) {
      const startPriceEdit = () => {
        if (priceLineEl.querySelector('.ac-model-meta-edit-input')) return;   // already editing
        const current = { in: numericCost(p.reference_cost_input), out: numericCost(p.reference_cost_output) };
        const group = document.createElement('span');
        group.className = 'ac-model-meta-edit-group';
        const makeField = (label, value) => {
          const wrap = document.createElement('label');
          wrap.className = 'ac-model-meta-edit-field';
          const input = document.createElement('input');
          input.type = 'number';
          input.min = '0';
          input.step = '0.0001';
          input.autocomplete = 'off';
          input.className = 'ac-input ac-model-meta-edit-input';
          input.placeholder = '0';
          input.value = value == null ? '' : String(value);
          const unit = document.createElement('span');
          unit.textContent = label;
          wrap.append(input, unit);
          return { wrap, input };
        };
        const inField = makeField('USD / 1M in', current.in);
        const outField = makeField('USD / 1M out', current.out);
        const note = document.createElement('span');
        note.className = 'ac-model-meta-edit-note';
        note.textContent = 'your reference estimate';
        group.append(inField.wrap, outField.wrap, note);
        priceValueEl.replaceWith(group);
        inField.input.focus();
        inField.input.select();
        const readRate = (input) => {
          const raw = input.value.trim();
          if (!raw) return null;                        // blank = no override
          const n = Number(raw);
          return Number.isFinite(n) && n >= 0 ? n : NaN;
        };
        let done = false;
        const finish = async (commit) => {
          if (done) return;                             // Escape, then the trailing blur
          done = true;
          const nextIn = readRate(inField.input);
          const nextOut = readRate(outField.input);
          const invalid = Number.isNaN(nextIn) || Number.isNaN(nextOut);
          if (!commit || invalid) {
            if (commit && invalid) _flashSaveCheck(priceLineEl, false, 'Enter a positive number, or leave blank');
            group.replaceWith(priceValueEl);
            return;
          }
          if (nextIn === current.in && nextOut === current.out) {
            group.replaceWith(priceValueEl);            // nothing to save
            return;
          }
          const previous = { in: p.reference_cost_input, out: p.reference_cost_output };
          if (nextIn == null) delete p.reference_cost_input; else p.reference_cost_input = nextIn;
          if (nextOut == null) delete p.reference_cost_output; else p.reference_cost_output = nextOut;
          _markSaving(priceLineEl);
          try {
            await saveRoster();
            renderSaved();                              // the row is rebuilt collapsed
            const reopened = await _reopenSavedRow(p._uid);
            if (reopened) _flashSaveCheck(reopened, true);
          } catch (error) {
            if (previous.in == null) delete p.reference_cost_input; else p.reference_cost_input = previous.in;
            if (previous.out == null) delete p.reference_cost_output; else p.reference_cost_output = previous.out;
            group.replaceWith(priceValueEl);
            _flashSaveCheck(priceLineEl, false, error?.message || 'Save failed');
          }
        };
        [inField.input, outField.input].forEach((input) => {
          input.addEventListener('keydown', (event) => {
            if (event.key === 'Enter') { event.preventDefault(); finish(true); }
            else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
          });
          input.addEventListener('click', (event) => event.stopPropagation());
        });
        // The pair behaves as ONE control: stepping between the two fields must not
        // commit, so only a blur that leaves the group finishes the edit.
        outField.input.addEventListener('blur', () => finish(true));
        inField.input.addEventListener('blur', () => {
          if (document.activeElement !== outField.input) finish(true);
        });
      };
      priceValueEl.addEventListener('click', (event) => { event.stopPropagation(); startPriceEdit(); });
      priceValueEl.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); startPriceEdit(); }
      });
    }

    if (usageScope === 'global') {
      const findBaseline = (kind) => S.providers.find(row => {
        const name = `${row.display_name || ''} ${row.label || ''} ${row.model || ''}`.toLowerCase();
        return name.includes('deepseek') && name.includes(kind);
      });
      const baselineRates = async (kind) => {
        const row = findBaseline(kind);
        if (!row) return { input: Number(meta.cost_input || 0) * 1000, output: Number(meta.cost_output || 0) * 1000 };
        const key = `${row.provider || ''}|${row.model || ''}`;
        const info = metaCache[key] || await fetchModelInfo(row.model, row.provider);
        metaCache[key] = info;
        return { input: Number(info.cost_input || 0) * 1000, output: Number(info.cost_output || 0) * 1000 };
      };
      const schedule = p.credit_schedule || (p.high_effort_capable ? 'pro' : 'flash');
      const defaults = await baselineRates(schedule === 'custom' ? 'flash' : schedule);
      if (p.credit_input_per_million == null) p.credit_input_per_million = defaults.input;
      if (p.credit_output_per_million == null) p.credit_output_per_million = defaults.output;
      p.credit_schedule = schedule;
      const creditBox = document.createElement('div'); creditBox.className = 'model-credit-editor';
      creditBox.innerHTML = `<strong>House credits</strong><div class="ac-hint">Admin USD reference: ${esc(price || 'pricing unavailable')}</div>`;
      const select = document.createElement('select'); select.className = 'ac-input';
      [['flash','DeepSeek Flash equivalent'],['pro','DeepSeek Pro equivalent'],['custom','Custom']].forEach(([value, label]) => {
        const option = document.createElement('option'); option.value = value; option.textContent = label; option.selected = value === schedule; select.appendChild(option);
      });
      const rates = document.createElement('div'); rates.className = 'model-credit-rates';
      const inputRate = document.createElement('input'); inputRate.type = 'number'; inputRate.min = '0'; inputRate.step = '0.000001'; inputRate.className = 'ac-input';
      const outputRate = inputRate.cloneNode();
      const paintRates = () => { inputRate.value = String(p.credit_input_per_million || 0); outputRate.value = String(p.credit_output_per_million || 0); };
      paintRates();
      rates.append('Input ◇ / 1M ', inputRate, ' Output ◇ / 1M ', outputRate);
      const stage = async () => { await saveRoster(); };
      select.addEventListener('change', async () => {
        p.credit_schedule = select.value;
        if (select.value !== 'custom') {
          const next = await baselineRates(select.value);
          p.credit_input_per_million = next.input; p.credit_output_per_million = next.output; paintRates();
        }
        inputRate.disabled = outputRate.disabled = select.value !== 'custom'; await stage();
      });
      const updateCustom = () => {
        p.credit_schedule = 'custom'; select.value = 'custom';
        p.credit_input_per_million = Math.max(0, Number(inputRate.value) || 0);
        p.credit_output_per_million = Math.max(0, Number(outputRate.value) || 0);
        stage();
      };
      inputRate.addEventListener('change', updateCustom); outputRate.addEventListener('change', updateCustom);
      inputRate.disabled = outputRate.disabled = schedule !== 'custom';
      creditBox.append(select, rates); body.appendChild(creditBox);
    }

    if (typeof adapter.canDeleteCredential === 'function' && adapter.canDeleteCredential(p)) {
      const credentialRow = document.createElement('div'); credentialRow.className = 'model-credential-row';
      const credentialState = document.createElement('span');
      credentialState.className = `model-credential-state ${p.credential_configured ? 'is-configured' : 'is-missing'}`;
      credentialState.textContent = p.credential_configured ? 'Provider credential configured' : 'Provider credential missing';
      const removeCredential = document.createElement('button');
      removeCredential.type = 'button'; removeCredential.className = 'ac-btn ac-btn-ghost ac-btn-sm';
      removeCredential.style.color = 'var(--danger)'; removeCredential.textContent = 'Remove credential';
      removeCredential.disabled = !p.credential_configured;
      removeCredential.addEventListener('click', async event => {
        event.stopPropagation();
        if (!window.confirm(`Remove the stored credential for ${rowDisplayName(p) === '—' ? 'this model' : rowDisplayName(p)}?`)) return;
        removeCredential.disabled = true;
        try {
          await adapter.deleteCredential(p);
          renderSaved();
        } catch (error) {
          removeCredential.disabled = false;
          _flashSaveCheck(credentialRow, false, error?.message || 'Credential removal failed');
        }
      });
      credentialRow.append(credentialState, removeCredential); body.appendChild(credentialRow);
    }

    // Display name lives in the unified list above as ONE editable row. Clicking
    // its value swaps in an input (RENAME-FIELD PATTERN: Enter commits, Escape
    // cancels, blur commits, a `done` guard stops a double commit). The save
    // re-renders the table collapsed, so the row is re-opened afterwards and the
    // ✓ settles on the rebuilt line — a rename reads as an edit IN PLACE.
    const aliasLine = body.querySelector('.ac-model-meta-nameline');
    const aliasValue = aliasLine.querySelector('.ac-model-meta-editable');
    const aliasTip = tipBadge(displayNameHelp);
    if (aliasTip) aliasLine.querySelector('.ac-model-meta-k').appendChild(aliasTip);
    const startAliasEdit = () => {
      if (aliasLine.querySelector('.ac-model-meta-edit-input')) return;   // already editing
      // MODEL-NAME-LEVELS: this field is the user's OWN display name, so edit the
      // value they set — never the stored fallback copy of the catalog model name.
      const current = userDisplayName(p);
      const aliasInput = document.createElement('input');
      aliasInput.type = 'text';
      aliasInput.className = 'ac-input ac-model-meta-edit-input';
      aliasInput.maxLength = 120;
      aliasInput.placeholder = 'Optional';
      aliasInput.autocomplete = 'off';
      aliasInput.value = current;
      aliasValue.replaceWith(aliasInput);
      aliasInput.focus();
      aliasInput.select();
      let done = false;
      const finish = async (commit) => {
        if (done) return;                        // Escape, then the trailing blur
        done = true;
        const next = aliasInput.value.trim();
        if (commit && next !== current) {
          const previous = current;
          p.display_name = next;
          _markSaving(aliasLine);
          try {
            await saveRoster();
            renderSaved();                       // the row header carries the new name
            const reopened = await _reopenSavedRow(p._uid);
            if (reopened) _flashSaveCheck(reopened, true);
          } catch (error) {
            p.display_name = previous;
            aliasInput.replaceWith(aliasValue);
            _flashSaveCheck(aliasLine, false, error?.message || 'Save failed');
          }
          return;
        }
        aliasInput.replaceWith(aliasValue);      // cancelled, or the name is unchanged
      };
      aliasInput.addEventListener('keydown', (event) => {
        if (event.key === 'Enter') { event.preventDefault(); finish(true); }
        else if (event.key === 'Escape') { event.preventDefault(); finish(false); }
      });
      aliasInput.addEventListener('blur', () => finish(true));
      aliasInput.addEventListener('click', (event) => event.stopPropagation());
    };
    aliasValue.addEventListener('click', (event) => { event.stopPropagation(); startAliasEdit(); });
    aliasValue.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); startAliasEdit(); }
    });
    // ── DEFAULT-FOR-NEW-AGENTS (grep MODEL-DEFAULT-ACCOUNT) ─────────────────
    // The account catalog default persists through the adapter's dedicated
    // user-catalog write when it has one (an individual agent's Model page owns
    // only that agent's roster), otherwise through the shared roster save (the
    // account catalog mount). Exactly one entry can hold it, so after a
    // successful write every row's box is re-synced in place — no full re-render,
    // so the expanded detail the user is reading stays open.
    const defaultCheck = body.querySelector('.ac-model-default-check');
    if (defaultCheck) {
      defaultCheck.addEventListener('click', (event) => event.stopPropagation());
      defaultCheck.addEventListener('change', async () => {
        const line = defaultCheck.closest('.ac-model-meta-defaultline') || defaultCheck;
        const before = S.defaultEntryId;
        S.defaultEntryId = defaultCheck.checked ? String(p.entry_id || '') : '';
        defaultCheck.disabled = true;
        _markSaving(line);
        try {
          if (typeof adapter.saveDefaultEntryId === 'function') await adapter.saveDefaultEntryId(S.defaultEntryId);
          else await saveRoster();
          table.querySelectorAll('.ac-model-default-check').forEach((cb) => {
            cb.checked = String(cb.dataset.entryId || '') === S.defaultEntryId;
          });
          _flashSaveCheck(line, true);
        } catch (error) {
          S.defaultEntryId = before;
          defaultCheck.checked = before === String(p.entry_id || '');
          _flashSaveCheck(line, false, (error && error.message) || 'Save failed');
        } finally {
          defaultCheck.disabled = false;
        }
      });
    }
    const delWrap = document.createElement('div'); delWrap.className = 'ac-saved-del-row';
    const delBtn = document.createElement('button'); delBtn.className = 'ac-btn ac-btn-ghost ac-saved-del-btn';
    const removeLabel = agentLocalRoster ? 'Remove from agent' : 'Remove model';
    delBtn.textContent = removeLabel; delBtn.style.color = 'var(--danger)';
    const resetAgentRemoval = () => {
      clearTimeout(delBtn._armTimer);
      delete delBtn.dataset.state;
      delBtn.classList.remove('warning', 'deleting', 'undoing');
      delBtn.disabled = false;
      delBtn.title = removeLabel;
      delBtn.setAttribute('aria-label', removeLabel);
      delBtn.textContent = removeLabel;
    };
    const removeFromRoster = async () => {
      delBtn.disabled = true;
      const removedIndex = S.providers.findIndex(x => x._uid === p._uid);
      const removed = S.providers[removedIndex];
      S.providers = S.providers.filter(x => x._uid !== p._uid);
      const node = table.querySelector(`.ac-saved-injected[data-mt-uid="${p._uid}"]`);
      const fade = _fadeSavedRow(node);
      try {
        await saveRoster();
        await fade;
        // Rebuild after the faded row leaves so every remaining row animates to
        // its new position, including role-sorted rows above/below the deletion.
        renderSaved({ animate: true });
      } catch (err) {
        await fade;
        // Restore the exact roster position and fade the still-present row back
        // in when persistence fails.
        if (removed) S.providers.splice(Math.max(0, removedIndex), 0, removed);
        await _fadeSavedRow(node, true);
        if (agentLocalRoster) resetAgentRemoval(); else delBtn.disabled = false;
        _flashSaveCheck(node, false, (err && err.message) || 'Remove failed');
      }
    };
    if (agentLocalRoster) {
      delBtn.title = removeLabel;
      delBtn.setAttribute('aria-label', removeLabel);
      delBtn.addEventListener('click', event => {
        event.stopPropagation();
        advanceDeleteBtn(delBtn, {
          armTitle: 'Click again to remove from this agent',
          armLabel: 'Confirm removal from this agent',
          busyTitle: 'Removing from agent…',
          busyLabel: 'Removing model from this agent',
          onConfirm: removeFromRoster,
        });
      });
    } else {
      delBtn.addEventListener('click', async event => {
        event.stopPropagation();
        await removeFromRoster();
      });
    }
    delWrap.appendChild(delBtn); body.appendChild(delWrap);
  }
  function colCells(parent, cols) {
    for (const [label, title] of cols) {
      const cell = document.createElement('span'); cell.className = 'ac-saved-cap ac-saved-th';
      cell.textContent = label; cell.title = title; parent.appendChild(cell);
    }
  }

  // Column template shared by the saved-list header and every row.
  // The "More" column is a ⋮ button that opens a floating popover with
  // role toggles and space for future per-model controls. Order: Drag · Model · Role icons · Usage · More
  const SAVED_GRID = '20px minmax(0, 1fr) 38px 20px';

  // ── Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT) ────────────
  // Per-agent prompt injections, shown/edited from the "Prompt injections"
  // strip under the saved list. Fixed roster roles have their own key; EACH
  // custom roster slot ('custom:<position>') carries its own directive too
  // (see _customDirectiveSlots). Keys mirror MODEL_DIRECTIVE_KEYS in
  // app/admin/settings.py.
  const DIRECTIVE_ROLES = {
    standard:  { symbol: 'S',  label: 'Standard', hint: 'Injected into the system prompt when the session runs on the Standard (default) brain.' },
    premium:   { symbol: 'P',  label: 'Premium',  hint: 'Injected when the session runs on the Premium brain. An agent-authored "Model Upgrade Directive" slot wins over this text when set.' },
    image_in:  { symbol: '👁', label: 'Vision',   hint: 'Injected into the system prompt when the session runs on the Vision brain (reads attached images).' },
    image_out: { symbol: '🖼', label: 'Image',    hint: 'Prepended to every generated-image prompt (Image role).' },
    system:    { symbol: '⚙', label: 'System',   hint: 'Prepended to app misc-LLM task prompts (session naming, context management).' },
    voice:     { symbol: '🎤', label: 'Voice',    hint: 'Reserved — LLM voice transcription is audio→text, so there is no prompt-injection point yet.' },
  };

  /** Custom roster slots in footer-picker order, mirroring _assign_slots in
   *  app/admin/settings.py (own rows first, then inherited app-roster rows;
   *  first matching role wins). Each custom
   *  slot gets its own directive key 'custom:<position>' — Custom 1, Custom 2,
   *  ... — so the admin can inject per-custom-model prompts. */
  function _customDirectiveSlots() {
    const union = [...(S.providers || [])];
    const seen = new Set();
    const rows = [];
    union.forEach(p => {
      const key = `${p.entry_id || ''}`;
      if (seen.has(key)) return;
      seen.add(key);
      rows.push(p);
    });
    const keyOf = (p) => `${p.entry_id || ''}`;
    const roles = { standard: null, premium: null, image_in: null, image_out: null };
    rows.forEach(p => {
      if (hasRole(p, 'standard')) roles.standard = p;
      if (hasRole(p, 'premium')) roles.premium = p;
      if (hasRole(p, 'image_in')) roles.image_in = p;
      if (hasRole(p, 'image_out')) roles.image_out = p;
    });
    const roleKeys = new Set(Object.values(roles).filter(Boolean).map(keyOf));
    return rows.filter(p => !roleKeys.has(keyOf(p)))
      .map((p, i) => ({ slot_ref: `custom:${i + 1}`, label: `Custom ${i + 1}`, model: p.model || '' }));
  }

  // Build + insert ONE saved-model row (just before the configurator anchor) and
  // kick off its usage fetch. Capability toggles and the
  // default radio persist IN PLACE — no list rebuild — so editing a row never
  // collapses the table or disturbs the scroll position.
  function _buildSavedRow(p, readOnly, checked, cfgAnchor, rolesEditable) {
    const isRoleAssigned = ['standard', 'premium', 'image_in', 'image_out', 'system', 'voice'].some(role => hasRole(p, role));
    const rowWrap = document.createElement('div'); rowWrap.className = 'ac-row ac-saved-injected' + (readOnly ? ' ac-saved-inherited' : ' ac-saved-own') + (isRoleAssigned ? ' ac-saved-role' : '');
    rowWrap.dataset.mtUid = p._uid;
    rowWrap.dataset.id = String(p._uid);  // for makeRowsReorderable's rowIdList()
    const row = document.createElement('div'); row.className = 'ac-ability-row ac-saved-row';
    row.style.gridTemplateColumns = SAVED_GRID;
    // Every writable roster row shares one persisted order. Role assignment is
    // metadata shown beside its model name, never a separate fixed row class.
    if (!readOnly) {
      const grip = document.createElement('span');
      grip.className = 'ac-saved-drag-handle';
      grip.title = 'Drag to reorder';
      grip.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="8" y1="6" x2="16" y2="6"/><line x1="8" y1="12" x2="16" y2="12"/><line x1="8" y1="18" x2="16" y2="18"/></svg>';
      row.appendChild(grip);
    } else {
      row.appendChild(document.createElement('span')); // read-only slot
    }
    // Single-line label: just the model name (the provider moved into the expanded
    // detail panel, so the row stays one line tall).
    const label = document.createElement('span'); label.className = 'ac-ability-label ac-saved-name-click';
    label.title = 'Show usage details';
    const badge = readOnly
      ? ' <span class="ac-cap-badge on" title="Inherited from the app default — read-only reference" style="margin-left:6px;">Inherited</span>'
      : (catalogReadOnly
        ? ` <span class="ac-cap-badge on" title="${p.catalog_origin === 'byo' ? 'Your bring-your-own model' : 'Available through your app entitlement'}" style="margin-left:6px;">${p.catalog_origin === 'byo' ? 'BYO' : 'App catalog'}</span>`
        : '');
    const subscriptionMeta = p.provider === 'openai_subscription' && !catalogReadOnly
      ? `<span class="ac-model-credential-email">${readOnly ? 'Managed by app admin' : esc(p.credential_email || (p.credential_configured ? 'ChatGPT account connected' : 'ChatGPT account not connected'))}</span>`
      : '';
    label.innerHTML = `<span class="ac-ability-name ac-saved-model-name"><span class="ac-saved-model-text"><bdi dir="ltr">${esc(rowDisplayName(p))}</bdi></span>${badge}</span>${subscriptionMeta}`;
    row.appendChild(label);

    // Compact role markers follow the model name, keeping the usage column clear.
    const flagsCell = document.createElement('span');
    flagsCell.className = 'ac-saved-flags';
    [
      ['S', 'Standard', 'standard'],
      ['P', 'Premium', 'premium'],
      ['V', 'Vision', 'image_in'],
      ['I', 'Image', 'image_out'],
      ['⚙', 'System', 'system'],
      ['🎤', 'Voice', 'voice'],
    ].forEach(([symbol, title, role]) => {
      if (!hasRole(p, role)) return;
      const flag = document.createElement('span');
      flag.className = 'ac-role-flag';
      flag.title = title;
      flag.textContent = symbol;
      flagsCell.appendChild(flag);
    });
    if (flagsCell.childElementCount) label.querySelector('.ac-saved-model-name').appendChild(flagsCell);

    // Roles more-button — opens a floating popover with the roster-role toggles
    // (Standard · Premium · Vision · Image · System · Voice). The account-level
    // "Default for new agents" choice is NOT a roster role: it lives in the
    // expanded detail below Display name (see loadSavedDetail), so this popover
    // stays roles-only.
    const rolesCell = document.createElement('span');
    rolesCell.className = 'ac-saved-cap ac-saved-roles-cell';
    rolesCell.style.position = 'relative';
    if (!catalogOnly) {
    // Build a badge string showing assigned roles
    const roleLabels = [];
    if (hasRole(p, 'standard')) roleLabels.push('Standard');
    if (hasRole(p, 'premium')) roleLabels.push('Premium');
    if (hasRole(p, 'image_in')) roleLabels.push('Vision');
    if (hasRole(p, 'image_out')) roleLabels.push('Image');
    if (hasRole(p, 'system')) roleLabels.push('System');
    if (hasRole(p, 'voice')) roleLabels.push('Voice');
    const rolesBtn = document.createElement('button');
    rolesBtn.type = 'button';
    rolesBtn.className = 'ac-saved-roles-btn';
    rolesBtn.title = roleLabels.length ? roleLabels.join(' · ') : 'No roles assigned';
    rolesBtn.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="5" r="1.5"/><circle cx="12" cy="12" r="1.5"/><circle cx="12" cy="19" r="1.5"/></svg>';
    rolesCell.appendChild(rolesBtn);
    // Floating popover — section-based like the chat header more-menu,
    // so future per-model controls can be added as their own sections.
    const popover = document.createElement('div');
    popover.className = 'ac-saved-roles-popover';
    popover.hidden = true;
    popover.setAttribute('role', 'menu');
    rolesCell.appendChild(popover);

    // ── Roles section ──
    const rolesSection = document.createElement('div');
    rolesSection.className = 'ac-pm-section';
    const rolesHead = document.createElement('div');
    rolesHead.className = 'ac-pm-section-head';
    const rolesTitle = document.createElement('span');
    rolesTitle.className = 'ac-pm-section-title';
    rolesTitle.textContent = 'Roles';
    rolesHead.appendChild(rolesTitle);
    rolesSection.appendChild(rolesHead);

    // ── Popover content ──
    // Roles belong only to this explicit local roster row.
    const persistRole = async (role, v, cb) => {
      const target = p;
      const before = { ...S.roleAssignments };
      if (v) S.roleAssignments[role] = String(target.entry_id || '');
      else delete S.roleAssignments[role];
      cb.disabled = true;
      _markSaving(cb.closest('.ac-saved-cap'));
      try {
        if (S.providers.length) await saveRoster();
        _flashSaveCheck(cb.closest('.ac-saved-cap'), true);
        renderSaved();
        // Re-open this row's popover — renderSaved destroys the DOM, so we
        // re-find the new row by uid and open its popover.
        const newRow = table.querySelector(`.ac-saved-injected[data-mt-uid="${target._uid}"]`);
        if (newRow) {
          const newBtn = newRow.querySelector('.ac-saved-roles-btn');
          const newPop = newRow.querySelector('.ac-saved-roles-popover');
          if (newBtn && newPop) {
            newPop.hidden = false;
            newBtn.classList.add('open');
            _positionRolesPopover(newBtn, newPop);
          }
        }
      } catch (e) {
        S.roleAssignments = before;
        _flashSaveCheck(cb.closest('.ac-saved-cap'), false, e.message || 'Save failed');
        cb.disabled = false;
      }
    };
    // Inherited rows are read-only references to the app default; their role
    // toggles stay locked (the lock hint explains why).
    const locked = readOnly && !rolesEditable;
    const lockHint = ' (app default — read-only)';
    _buildRoleToggle(rolesSection, p.text_capable !== false, hasRole(p, 'standard'), 'Standard', 'The primary chat / brain model',
      (v, cb) => persistRole('standard', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, p.text_capable !== false, hasRole(p, 'premium'), 'Premium', 'A premium model for harder tasks',
      (v, cb) => persistRole('premium', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, !!p.image_capable, hasRole(p, 'image_in'), 'Vision', 'Use to read attached images',
      (v, cb) => persistRole('image_in', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, !!p.image_out_capable, hasRole(p, 'image_out'), 'Image', 'Use to generate images',
      (v, cb) => persistRole('image_out', v, cb), locked, lockHint);
    _buildRoleToggle(rolesSection, p.text_capable !== false, hasRole(p, 'system'), 'System', 'App misc. LLM tasks (session naming, context mgmt)',
      (v, cb) => persistRole('system', v, cb), locked, lockHint);
    const liveVoiceCapable = !!p.voice_capable && !String(p.model || '').toLowerCase().endsWith(':batch');
    _buildRoleToggle(rolesSection, liveVoiceCapable, hasRole(p, 'voice'), 'Voice', 'Live voice input in chat',
      (v, cb) => persistRole('voice', v, cb), locked, lockHint,
      String(p.model || '').toLowerCase().endsWith(':batch') ? 'batch-only models cannot serve live voice' : '');
    popover.appendChild(rolesSection);

    // Wire popover open/close
    // ── Wire popover open/close ──
    rolesBtn.addEventListener('click', (e) => {
      e.stopPropagation();
      const isOpen = !popover.hidden;
      // Close all other popovers first
      table.querySelectorAll('.ac-saved-roles-popover:not([hidden])').forEach(m => {
        if (m !== popover) m.hidden = true;
      });
      table.querySelectorAll('.ac-saved-roles-btn.open').forEach(b => b.classList.remove('open'));
      popover.hidden = isOpen;
      rolesBtn.classList.toggle('open', !isOpen);
      if (!isOpen) _positionRolesPopover(rolesBtn, popover);
    });
    // Click-away closes
    const closeRoles = (e) => {
      if (!rolesCell.contains(e.target)) {
        popover.hidden = true;
        rolesBtn.classList.remove('open');
      }
    };
    document.addEventListener('pointerdown', closeRoles);
    docListeners.push(['pointerdown', closeRoles]);
    } // end !catalogOnly role-popover block
    const usageCell = document.createElement('span'); usageCell.className = 'ac-saved-usage';
    const statIn = usageLine('Total input tokens'), statOut = usageLine('Total output tokens'), statCost = usageLine('Total provider cost');
    usageCell.appendChild(statIn.line); usageCell.appendChild(statOut.line); usageCell.appendChild(statCost.line);
    row.appendChild(usageCell);
    row.appendChild(rolesCell);
    const body = document.createElement('div'); body.className = 'ac-ability-body';
    body.innerHTML = '<div class="ac-hint" style="margin:0;">Loading details…</div>';
    label.addEventListener('click', () => {
      const open = rowWrap.classList.toggle('expanded');
      // MODEL-ROW-FOCUS: the chart above follows the row the user just opened, and
      // closing the row that owns the focus hands the whole group back. A row with no
      // model id has no series to plot, so it never moves the chart.
      const focusKey = String(p.model || '').trim();
      if (focusKey && (open || S.focusedModel === focusKey)) {
        if (open) _publishModelFocus(focusKey);
        else _releaseModelFocus();
      }
      if (open) loadSavedDetail(p, body, statIn.val, statOut.val, statCost.val, readOnly);
    });
    rowWrap.appendChild(row); rowWrap.appendChild(body);
    table.insertBefore(rowWrap, cfgAnchor);
    fillUsage(p, statIn.val, statOut.val, statCost.val, readOnly);
    return rowWrap;
  }

  // Build the saved-list header (Model · Usage · (empty for More)) once.
  function _buildSavedHead(cfgAnchor) {
    const head = document.createElement('div');
    head.className = 'ac-ability-row ac-saved-head ac-saved-row ac-saved-injected';
    head.style.gridTemplateColumns = SAVED_GRID;
    // Empty slot for the drag-handle column
    head.appendChild(document.createElement('span'));
    const headName = document.createElement('span'); headName.className = 'ac-ability-label';
    headName.innerHTML = '<span class="ac-saved-th">Model</span>'; head.appendChild(headName);
    const headUsage = document.createElement('span'); headUsage.className = 'ac-saved-th ac-saved-usage-th';
    headUsage.textContent = 'Usage'; headUsage.title = 'Total tokens in / out and provider cost'; head.appendChild(headUsage);
    head.appendChild(document.createElement('span')); // empty — More button has no header label
    table.insertBefore(head, cfgAnchor);
  }

  // ── Roster-role prompt directives (grep ROLE-DIRECTIVE-INJECT) ──────────
  // Admin-authored prompt injections per roster role, edited from the "Prompt
  // injections" strip under the saved list. Writes are admin-only (the mount
  // decides via `directivesEditable` + the presence of `adapter.saveDirectives`);
  // non-admin mounts show the strip read-only (or hidden when the read fails).
  function _directivePreview(key) {
    let v = '';
    if (S.directives) {
      v = key.startsWith('custom:')
        ? ((S.directives.custom || {})[key] || '')
        : (S.directives[key] || '');
    }
    return v ? (v.length > 42 ? v.slice(0, 42) + '…' : v) : '';
  }

  function _directiveMeta(key) {
    const isCustom = key.startsWith('custom:');
    const role = isCustom ? null : DIRECTIVE_ROLES[key];
    const slot = isCustom ? (_customDirectiveSlots().find(s => s.slot_ref === key) || null) : null;
    if (!role && !isCustom) return null;
    return {
      isCustom,
      symbol: role ? role.symbol : '★',
      label: role ? role.label : (slot ? slot.label : key),
      hint: role
        ? role.hint
        : (slot
          ? `Injected into the system prompt when the session runs on ${slot.label} (${slot.model}).`
          : 'Injected into the system prompt when the session runs on this custom roster model.'),
    };
  }

  function _directiveValue(key) {
    return key.startsWith('custom:')
      ? ((S.directives && S.directives.custom) ? (S.directives.custom[key] || '') : '')
      : ((S.directives && S.directives[key]) || '');
  }

  function _buildInlineDirectiveRow(key, destination) {
    const meta = _directiveMeta(key);
    if (!meta) return;
    const wrap = document.createElement('div');
    wrap.className = 'ac-row ac-saved-injected ac-directive-row';
    const head = document.createElement('div');
    head.className = 'ac-ability-row';
    head.tabIndex = 0;
    head.setAttribute('role', 'button');
    head.setAttribute('aria-expanded', 'false');
    head.title = `${meta.label} — click to ${directivesEditable ? 'edit' : 'view'} the prompt injection`;
    const label = document.createElement('span');
    label.className = 'ac-ability-label';
    const preview = document.createElement('span');
    preview.className = 'ac-ability-desc ac-directive-preview';
    preview.textContent = _directivePreview(key);
    label.innerHTML = `<span class="ac-ability-name"><span class="ac-directive-symbol">${esc(meta.symbol)}</span>${esc(meta.label)} prompt</span>`;
    label.appendChild(preview);
    const chevron = document.createElement('span');
    chevron.className = 'ac-row-chevron';
    chevron.setAttribute('aria-hidden', 'true');
    chevron.innerHTML = ROW_CHEVRON_SVG;   // grep CHEVRON-CONSISTENCY — shared with both fields
    head.appendChild(label); head.appendChild(chevron);

    const body = document.createElement('div');
    body.className = 'ac-ability-body ac-directive-body';
    const hint = document.createElement('p');
    hint.className = 'ac-hint ac-directive-hint';
    hint.textContent = meta.hint;
    const ta = document.createElement('textarea');
    ta.className = 'ac-input ac-directive-textarea';
    ta.value = _directiveValue(key);
    ta.placeholder = 'No injection set — this role runs with its own prompt.';
    ta.spellcheck = false;
    ta.readOnly = !directivesEditable;
    body.appendChild(hint); body.appendChild(ta);
    if (directivesEditable) {
      const foot = document.createElement('div');
      foot.className = 'ac-directive-actions';
      const err = document.createElement('span');
      err.className = 'ac-directive-error';
      const save = document.createElement('button');
      save.type = 'button'; save.className = 'ac-btn ac-directive-save'; save.textContent = 'Save';
      save.addEventListener('click', async (event) => {
        event.stopPropagation();
        const next = { ...(S.directives || {}), custom: { ...((S.directives && S.directives.custom) || {}) } };
        if (meta.isCustom) next.custom[key] = ta.value;
        else next[key] = ta.value;
        _markSaving(save); err.textContent = '';
        try {
          await adapter.saveDirectives(next);
          S.directives = next;
          preview.textContent = _directivePreview(key);
          _flashSaveCheck(save, true);
        } catch (error) {
          const message = (error && error.message) || 'Save failed';
          _flashSaveCheck(save, false, message); err.textContent = message;
        }
      });
      foot.appendChild(err); foot.appendChild(save); body.appendChild(foot);
    }
    const toggle = () => {
      const open = wrap.classList.toggle('expanded');
      head.setAttribute('aria-expanded', String(open));
      if (open && directivesEditable) ta.focus();
    };
    head.addEventListener('click', toggle);
    head.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); toggle(); }
    });
    wrap.appendChild(head); wrap.appendChild(body);
    destination.appendChild(wrap);
  }

  function _buildDirectiveStrip(cfgAnchor) {
    if (directivePresentation === 'inline') {
      directivesList.replaceChildren();
      Object.keys(DIRECTIVE_ROLES).forEach(key => _buildInlineDirectiveRow(key, directivesList));
      _customDirectiveSlots().forEach(slot => _buildInlineDirectiveRow(slot.slot_ref, directivesList));
      directivesSection.hidden = directivesList.childElementCount === 0;
      return;
    }
    const strip = document.createElement('div');
    strip.className = 'ac-saved-injected ac-directives-strip';
    strip.style.cssText = 'display:flex;align-items:center;gap:6px;flex-wrap:wrap;padding:7px 14px;border-bottom:var(--border-width) solid var(--border);';
    const lab = document.createElement('span');
    lab.className = 'ac-saved-th';
    lab.textContent = 'Prompt injections';
    lab.title = 'Admin-authored prompt text injected per roster role (grep ROLE-DIRECTIVE-INJECT)';
    lab.style.cssText = 'font-size:11px;letter-spacing:0.4px;color:var(--fg-3);margin-right:4px;white-space:nowrap;';
    strip.appendChild(lab);
    for (const [key, role] of Object.entries(DIRECTIVE_ROLES)) {
      const chip = document.createElement('button');
      chip.type = 'button'; chip.className = 'ac-directive-chip';
      chip.style.cssText = 'display:inline-flex;align-items:center;gap:5px;max-width:230px;padding:2px 9px;border:var(--border-width) solid var(--border);border-radius:999px;background:var(--bg-tint);color:var(--fg-2);font-size:11px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
      chip.title = `${role.label} — click to ${directivesEditable ? 'edit' : 'view'} the prompt injection`;
      chip.innerHTML = `<span style="font-weight:700;">${role.symbol}</span><span style="opacity:.85;">${role.label}</span><span style="opacity:.55;overflow:hidden;text-overflow:ellipsis;">${esc(_directivePreview(key))}</span>`;
      chip.addEventListener('click', () => openDirectiveEditor(key)); strip.appendChild(chip);
    }
    _customDirectiveSlots().forEach(slot => {
      const chip = document.createElement('button');
      chip.type = 'button'; chip.className = 'ac-directive-chip';
      chip.style.cssText = 'display:inline-flex;align-items:center;gap:5px;max-width:230px;padding:2px 9px;border:var(--border-width) solid var(--border);border-radius:999px;background:var(--bg-tint);color:var(--fg-2);font-size:11px;cursor:pointer;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;';
      chip.title = `${slot.label} (${slot.model}) — click to ${directivesEditable ? 'edit' : 'view'} the prompt injection`;
      chip.innerHTML = `<span style="font-weight:700;">★</span><span style="opacity:.85;">${slot.label}</span><span style="opacity:.55;overflow:hidden;text-overflow:ellipsis;">${esc(_directivePreview(slot.slot_ref))}</span>`;
      chip.addEventListener('click', () => openDirectiveEditor(slot.slot_ref)); strip.appendChild(chip);
    });
    table.insertBefore(strip, cfgAnchor);
  }

  /** Open the directive editor for one roster slot — a fixed role key
   *  ('standard', 'premium', ...) or a custom slot ref ('custom:1', ...). */
  function openDirectiveEditor(key) {
    const isCustom = key.startsWith('custom:');
    const role = isCustom ? null : DIRECTIVE_ROLES[key];
    if (!role && !isCustom) return;
    const customSlot = isCustom
      ? (_customDirectiveSlots().find(s => s.slot_ref === key) || null)
      : null;
    const symbol = role ? role.symbol : '★';
    const label = role ? role.label : (customSlot ? customSlot.label : key);
    const hint = role
      ? role.hint
      : (customSlot
          ? `Injected into the system prompt when the session runs on ${customSlot.label} (${customSlot.model}).`
          : 'Injected into the system prompt when the session runs on this custom roster model.');
    const editable = directivesEditable;
    const ov = document.createElement('div');
    ov.className = 'ac-directive-overlay';
    ov.style.cssText = 'position:fixed;inset:0;z-index:1200;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;';
    const panel = document.createElement('div');
    panel.className = 'ac-directive-modal';
    panel.style.cssText = 'width:min(660px,92vw);max-height:82vh;display:flex;flex-direction:column;background:var(--bg-elev);border:var(--border-width) solid var(--border);border-radius:12px;box-shadow:0 20px 60px rgba(0,0,0,.45);overflow:hidden;';
    const head = document.createElement('div');
    head.style.cssText = 'display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:var(--border-width) solid var(--border);';
    head.innerHTML = `<span style="font-weight:700;">${symbol} ${esc(label)}</span><span style="color:var(--fg-3);font-size:12px;">prompt injection</span>`;
    const closeX = document.createElement('button');
    closeX.type = 'button';
    closeX.textContent = '✕';
    closeX.title = 'Close';
    closeX.style.cssText = 'margin-left:auto;background:none;border:none;color:var(--fg-3);font-size:14px;cursor:pointer;padding:4px;';
    head.appendChild(closeX);
    const body = document.createElement('div');
    body.style.cssText = 'padding:14px 16px;overflow-y:auto;display:flex;flex-direction:column;gap:10px;';
    const hintEl = document.createElement('div');
    hintEl.style.cssText = 'font-size:12px;color:var(--fg-3);line-height:1.45;';
    hintEl.textContent = hint;
    body.appendChild(hintEl);
    const ta = document.createElement('textarea');
    ta.value = isCustom
      ? ((S.directives && S.directives.custom) ? (S.directives.custom[key] || '') : '')
      : ((S.directives && S.directives[key]) || '');
    ta.placeholder = 'No injection set — this role runs with its own prompt.';
    ta.spellcheck = false;
    ta.style.cssText = 'width:100%;min-height:150px;resize:vertical;background:var(--bg-0);color:var(--fg-1);border:var(--border-width) solid var(--border);border-radius:8px;padding:10px;font-family:var(--font-mono, monospace);font-size:12px;line-height:1.5;';
    ta.readOnly = !editable;
    if (!editable) ta.style.opacity = '.85';
    body.appendChild(ta);
    const foot = document.createElement('div');
    foot.style.cssText = 'display:flex;align-items:center;gap:10px;padding:12px 16px;border-top:var(--border-width) solid var(--border);';
    const err = document.createElement('span');
    err.style.cssText = 'font-size:12px;color:var(--danger);flex:1;';
    foot.appendChild(err);
    const cancel = document.createElement('button');
    cancel.type = 'button';
    cancel.className = 'ac-btn ac-btn-ghost';
    cancel.textContent = 'Close';
    foot.appendChild(cancel);
    let saveBtn = null;
    if (editable) {
      saveBtn = document.createElement('button');
      saveBtn.type = 'button';
      saveBtn.className = 'ac-btn';
      saveBtn.textContent = 'Save';
      saveBtn.style.cssText = 'background:var(--accent);color:var(--bg-0);border:none;border-radius:8px;padding:6px 16px;font-weight:600;cursor:pointer;';
      foot.insertBefore(saveBtn, cancel);
    }
    panel.appendChild(head); panel.appendChild(body); panel.appendChild(foot);
    ov.appendChild(panel);
    document.body.appendChild(ov);
    if (editable) ta.focus();

    const close = () => {
      document.removeEventListener('keydown', onKey);
      if (_directiveModalCleanup === close) _directiveModalCleanup = null;
      ov.remove();
    };
    const onKey = (e) => { if (e.key === 'Escape') close(); };
    document.addEventListener('keydown', onKey);
    _directiveModalCleanup = close;
    closeX.addEventListener('click', close);
    cancel.addEventListener('click', close);
    ov.addEventListener('pointerdown', (e) => { if (e.target === ov) close(); });
    if (saveBtn) {
      saveBtn.addEventListener('click', async () => {
        const next = { ...(S.directives || {}), custom: { ...((S.directives && S.directives.custom) || {}) } };
        if (isCustom) next.custom[key] = ta.value;
        else next[key] = ta.value;
        _markSaving(saveBtn);
        err.textContent = '';
        try {
          await adapter.saveDirectives(next);
          S.directives = next;
          _flashSaveCheck(saveBtn, true);
          renderSaved();
          setTimeout(close, 350);
        } catch (e2) {
          _flashSaveCheck(saveBtn, false, (e2 && e2.message) || 'Save failed');
          err.textContent = (e2 && e2.message) || 'Save failed';
        }
      });
    }
  }

  // Full (re)build of the saved list. Initial loads render immediately; role,
  // add, and remove changes can opt into keyed layout animation.
  // MODEL-ROW-FOCUS: the usage chart above the table follows whichever saved row the
  // user opens, so the widening box answers "what did THIS model cost over time" —
  // the read the removed per-row chart used to give — and falls back to the whole
  // group once no row is open. Fire-and-forget with a swallowed error: a mount with
  // no sibling chart (or a page whose chart is not up yet) simply has no subscriber.
  function _publishModelFocus(model) {
    // A subscriber refuses the focus when it has nothing to show for that model — the
    // usage chart does that for a model with no recorded usage, keeping its baseline
    // series and showing a note instead of going empty. The row is still open, but the
    // table does not claim the focus, so closing it releases nothing on the chart.
    if (onModelFocus) {
      try { if (onModelFocus(model) === false) return false; } catch (_) {}
    }
    S.focusedModel = model;
    return true;
  }

  function _releaseModelFocus() {
    if (!S.focusedModel) return;
    S.focusedModel = '';
    if (onModelFocus) { try { onModelFocus(''); } catch (_) {} }
  }

  function renderSaved({ animate = false, enteringUids = [] } = {}) {
    closeAliasForm();
    const before = animate ? _snapshotSavedLayout() : null;
    // MODEL-ROW-FOCUS: this rebuild collapses every row detail, so the chart's
    // single-model focus goes with them. Without this, saving the roster would leave
    // the chart narrowed to a model whose row no longer reads as open.
    _releaseModelFocus();
    table.querySelectorAll('.ac-saved-injected, .ac-model-catalog-empty').forEach(n => n.remove());
    if (directivePresentation === 'inline') {
      directivesList.replaceChildren();
      directivesSection.hidden = true;
    }
    // Every rendered row belongs to this mount's persisted roster.
    // The provider array is the single user-visible order. Roles remain row
    // metadata and are deliberately not used to sort or group models.
    const rows = S.providers.map(p => ({ p, readOnly: false, rolesEditable: !catalogOnly }));
    if (!rows.length) {
      if (catalogReadOnly) {
        const empty = document.createElement('div');
        empty.className = 'ac-model-catalog-empty ac-hint';
        empty.style.margin = '12px 0';
        empty.textContent = 'No models are available in your catalog yet. Add a BYO model or obtain an app entitlement to make models available to your agents.';
        table.insertBefore(empty, table.querySelector('.ac-model-cfg-row'));
      } else {
        // An empty roster should never leave the user staring at only the + row.
        // This shared renderer serves both the admin-global and per-agent tables,
        // and also runs after deletion, so enforce the expanded composer here.
        table.classList.remove('ac-cfg-collapsed');
      }
      if (before) _animateSavedLayout(before, enteringUids);
      return;
    }
    const cfgAnchor = table.querySelector('.ac-model-cfg-row');
    _buildSavedHead(cfgAnchor);
    if (S.directives) _buildDirectiveStrip(cfgAnchor);
    rows.forEach(({ p, readOnly, rolesEditable }) => _buildSavedRow(p, readOnly, false, cfgAnchor, rolesEditable));
    // keep the advanced/+ row pinned last
    table.appendChild(advRow);
    // Wire drag-to-reorder on the agent's own rows (not inherited/read-only).
    // The handle is `.ac-saved-drag-handle`; rows are identified by `data-mt-uid`.
    if (!catalogReadOnly) makeRowsReorderable(table, {
      rowSelector: '.ac-saved-own[data-mt-uid]',
      handleSelector: '.ac-saved-drag-handle',
      dropBefore: advRow,
      onReorder: (orderedUids) => {
        // Reorder S.providers to match the new DOM order. Inherited rows are
        // read-only and never have a drag handle, so they stay put; only the
        // agent's own rows (which carry _uid matching data-mt-uid) are reordered.
        const uidToIdx = new Map();
        S.providers.forEach((p, i) => uidToIdx.set(String(p._uid), i));
        const ownUids = orderedUids.filter(uid => uidToIdx.has(uid));
        if (!ownUids.length) return;
        // Build the new providers array in the dragged order.
        const reordered = ownUids.map(uid => S.providers[uidToIdx.get(uid)]);
        // Preserve any own-rows that somehow weren't in the DOM (shouldn't
        // happen, but defensive) at the end in their original order.
        const seen = new Set(ownUids);
        S.providers.forEach((p, i) => {
          if (!seen.has(String(p._uid))) reordered.push(p);
        });
        S.providers = reordered;
        saveRoster().catch(error => { setModelStatus(error.message); void reload(); });
      },
    });
    if (before) _animateSavedLayout(before, enteringUids);
  }

  // ── Add / Clear ──
  function nextFreeSlotLabel() {
    const used = [...S.providers]
      .map(p => /^F(\d+)$/i.exec(String(p.display_name || '').trim()))
      .filter(Boolean).map(match => Number(match[1]));
    return `F${(used.length ? Math.max(...used) : 0) + 1}`;
  }
  async function addModel({ free = false } = {}) {
    const platformCatalog = userCatalog && S.currentProvider === 'webagent_catalog';
    const provider = free ? 'openrouter' : (platformCatalog ? 'webagent_catalog' : (S.currentProvider === '_custom' ? 'custom' : S.currentProvider));
    const baseUrl = free ? 'https://openrouter.ai/api/v1' : urlInput.value.trim();
    const apiKey = keyInput.value.trim();
    const subscription = isSubscriptionProvider();
    if (free && S.currentProvider !== 'openrouter') return setModelStatus('Select OpenRouter and enter its API key before adding an automatic free slot.');
    if (!subscription && !platformCatalog && !baseUrl) return setModelStatus('Please enter a Base URL');
    if (!subscription && !platformCatalog && !apiKey) return setModelStatus('Please enter an API Key');
    if (!free && !S.selectedModel) return setModelStatus('Please select a model');
    if (subscription && !matchingSubscriptionRow()?.credential_configured) return setModelStatus('Connect ChatGPT before adding a model.');
    let savedRow = null;
    const capText = S.selCaps.text !== false, capImage = !!S.selCaps.image, capImageOut = !!S.selCaps.imageOut, capVoice = !!S.selCaps.voice;
    // The first agent-roster model claims Standard so the app has an explicit chat
    // model. Catalog-only mounts have no roles, and subscription entries already
    // carry a server-reserved ID before they can be added.
    const becomesStandard = !catalogOnly && !subscription && capText && !S.providers.length;
    // Platform entries already have server-owned IDs. Picking one already in
    // this roster links that connection rather than submitting a duplicate ID.
    // MODEL-ADD-GUARD keeps the alias-only self-link validation from firing on
    // a normal Add (where both the duplicate lookup and the alias target are
    // null) — see ui/shared/js/model-add-guard.js.
    const { dupe, message: guardMessage } = modelAddGuard({
      platformCatalog, providers: S.providers, selectedModel: S.selectedModel, aliasTarget,
    });
    if (guardMessage) return setModelStatus(guardMessage);
    if (!dupe) {
      const chosen = S.allModels.find(row => row.id === S.selectedModel) || {};
      const subscriptionConnection = subscription ? matchingSubscriptionRow() : null;
      // Persisted IDs are owned by the server. `_uid` is the only client-side row
      // identity; adapters hydrate `entry_id` into this same object after save.
      const newP = { entry_id: platformCatalog ? S.selectedModel : (subscription && pendingSubscriptionRow?.entry_id ? pendingSubscriptionRow.entry_id : ''), connection_id: subscriptionConnection?.connection_id || '', catalog_source_id: platformCatalog ? S.selectedModel : '', display_name: displayNameInput.value.trim() || (free ? nextFreeSlotLabel() : ''), provider, auth_mode: subscription ? 'chatgpt' : 'api_key', base_url: platformCatalog ? '' : (subscription ? 'codex://chatgpt' : baseUrl), api_key: platformCatalog ? '' : apiKey, model: free ? 'openrouter/free' : S.selectedModel, catalog_origin: platformCatalog ? 'platform' : 'byo', catalog_name: platformCatalog ? (chosen.catalog_name || chosen.display_name || '') : '', routing_mode: free ? 'openrouter_free' : '', text_capable: free || capText, image_capable: free ? false : capImage, image_out_capable: free ? false : capImageOut, voice_capable: free ? false : capVoice, high_effort_capable: false, credential_scope: subscription ? subscriptionScope : '', credential_configured: subscription ? !!subscriptionConnection?.credential_configured : true, credential_email: subscription ? (subscriptionConnection?.credential_email || '') : '', credential_plan_type: subscription ? (subscriptionConnection?.credential_plan_type || '') : '', _uid: ++S.uid };
      newP.video_capable = free ? false : !!chosen.video_capable;
      newP.decision_capable = !!chosen.decision_capable;
      const previousRoles = { ...S.roleAssignments };
      const parentModel = aliasTarget;
      const previousGroups = S.providers.map(row => [row, row.alias_group_id]);
      if (parentModel) {
        const groupId = String(parentModel.alias_group_id || parentModel.entry_id || '');
        if (!groupId) throw new Error('Save the primary model before adding an alias');
        parentModel.alias_group_id = groupId;
        newP.alias_group_id = groupId;
      }
      S.providers.push(newP);
      try {
        await saveRoster();
        // Role assignments may only reference persistent IDs. Save the new row
        // first, let the adapter merge the server UUID, then persist Standard.
        if (becomesStandard) {
          if (!newP.entry_id) throw new Error('The server did not assign a model entry ID');
          S.roleAssignments.standard = String(newP.entry_id);
          await saveRoster();
        }
      } catch (error) {
        S.providers = S.providers.filter(row => row !== newP);
        S.roleAssignments = previousRoles;
        previousGroups.forEach(([row, group]) => { row.alias_group_id = group; });
        throw error;
      }
      // Keep the parent group visible after a grouped add; saved rows retain
      // independent stable IDs and credentials for runtime failover.
      renderSaved({ animate: true, enteringUids: [newP._uid] });
      if (parentModel) {
        table.querySelector(`.ac-saved-injected[data-mt-uid="${parentModel._uid}"] .ac-saved-name-click`)?.click();
      }
      savedRow = newP;
    } else {
      const parentModel = aliasTarget;
      const groupId = String(parentModel.alias_group_id || parentModel.entry_id || '');
      if (dupe.alias_group_id && dupe.alias_group_id !== groupId) {
        return setModelStatus('Unlink this connection from its current alias group first');
      }
      const previousGroups = S.providers.map(row => [row, row.alias_group_id]);
      parentModel.alias_group_id = groupId; dupe.alias_group_id = groupId;
      try { await saveRoster(); }
      catch (error) {
        previousGroups.forEach(([row, group]) => { row.alias_group_id = group; });
        throw error;
      }
      renderSaved();
      table.querySelector(`.ac-saved-injected[data-mt-uid="${parentModel._uid}"] .ac-saved-name-click`)?.click();
      savedRow = dupe;
    }
    if (subscription && pendingSubscriptionRow?.entry_id === savedRow?.entry_id) {
      pendingSubscriptionRow = null;
    }
    setModelStatus(inlineCatalogAdd ? '' : 'Saved', true);
    displayNameInput.value = '';
    renderSubscriptionAuth();
    onModelChange();
    return savedRow;
  }
  async function clearModel() {
    keyInput.value = ''; modelSearch.value = ''; S.selectedModel = ''; renderDetectedCaps(null); setModelStatus('');
    displayNameInput.value = '';
    renderSubscriptionAuth();
    renderModelDropdown('');
    onModelChange();
  }

  // ── wire configurator events ──
  provSel.addEventListener('change', () => {
    rememberProvider(S.currentProvider);
    S.currentProvider = provSel.value;
    // Keep the visible combobox in step with the authority. This fires for a
    // committed choice, for the reset when the add form opens, and for every
    // other programmatic change, so the field can never show a stale provider.
    syncProviderField();
    const saved = S.providerConfigs[S.currentProvider];
    urlInput.value = (saved && saved.base_url) || S.presets[S.currentProvider]?.base_url || '';
    keyInput.value = ''; keyInput.placeholder = 'sk-...';
    validatedCredential = ''; clearTimeout(credentialValidationTimer);
    credentialSelect.hidden = true; credentialSelect.replaceChildren();
    S.selectedModel = inlineCatalogAdd ? '' : ((saved && saved.model) || '');
    modelSearch.value = inlineCatalogAdd ? '' : S.selectedModel;
    setModelStatus(inlineCatalogAdd ? '' : (S.selectedModel ? `Selected: ${S.selectedModel}` : ''), true);
    renderDetectedCaps(null); S.allModels = [];
    renderModelDropdown(modelSearch.value.toLowerCase());
    renderSubscriptionAuth();
    // The V3 connection bridge starts the device flow on the user's provider
    // selection. A failed request restores Retry verification code.
    if (isSubscriptionProvider() && !activeSubscriptionVerification()
        && !matchingSubscriptionRow()?.credential_configured) startSubscriptionLogin();
    loadModels();
    autosaveSingle(provCheck);
  });
  keyInput.addEventListener('input', () => { modelLoadRevision++; S.allModels = []; renderModelDropdown(modelSearch.value.toLowerCase()); scheduleCredentialValidation(); scheduleFetch(); scheduleAutosave(keyCheck); });
  urlInput.addEventListener('input', () => { modelLoadRevision++; S.allModels = []; renderModelDropdown(modelSearch.value.toLowerCase()); scheduleCredentialValidation(); scheduleFetch(); scheduleAutosave(urlCheck); });
  loadCredentialBtn.addEventListener('click', showCredentialPicker);
  saveCredentialBtn.addEventListener('click', saveCurrentCredential);
  credentialSelect.addEventListener('change', () => applySavedCredential(credentialSelect.value));
  modelSearch.addEventListener('focus', async () => {
    // The panel is already visible; focus only retries the catalog load if needed.
    if (!S.allModels.length && S.currentProvider === 'webagent_catalog') await loadModels();
  });
  modelSearch.addEventListener('input', () => {
    if (opts.manualModelEntry && S.currentProvider !== 'webagent_catalog') {
      S.selectedModel = modelSearch.value.trim(); S.selCaps = {text:true,image:false,imageOut:false,voice:false}; renderDetectedCaps(null);
    }
    renderModelDropdown(modelSearch.value.toLowerCase());
  });
  async function submitModel(options = {}) {
    if (addBtn.disabled) return;
    addBtn.disabled = true; freeBtn.disabled = true;
    markSaving(modelCheck);
    try {
      const saved = await addModel(options);
      if (saved) flashCheck(modelCheck, true);
      else { modelCheck.innerHTML = ''; }
    } catch (error) {
      setModelStatus(error?.message || 'Unable to save model');
      flashCheck(modelCheck, false, error?.message || 'Save failed');
    } finally { addBtn.disabled = false; freeBtn.disabled = false; }
  }
  addBtn.addEventListener('click', () => submitModel());
  freeBtn.addEventListener('click', () => submitModel({ free: true }));
  clearBtn.addEventListener('click', clearModel);
  advToggle.addEventListener('click', () => {
    const expanded = advRow.classList.toggle('expanded');
    if (expanded) void renderSavedCredentials();
  });
  addToggle.addEventListener('click', () => {
    const opening = table.classList.contains('ac-cfg-collapsed');
    // Available models always opens on the app picker. A prior BYO selection
    // must not leak into the next add flow.
    if (opening && userCatalog && provSel.value !== 'webagent_catalog') {
      // The visible option is the authority here. Reset any prior BYO selection
      // before the user catalog add form becomes visible.
      provSel.value = 'webagent_catalog';
      provSel.dispatchEvent(new Event('change'));
    }
    table.classList.toggle('ac-cfg-collapsed');
  });

  // The inline model panel needs no geometry tracking. The Provider menu remains
  // a floating combobox, so only it re-anchors on scroll and resize.
  const repositionDropdowns = () => {
    scheduleDropdownPosition(provDd, provInput, hideProviderDropdown);
  };
  const ddAway = (e) => {
    if (!provField.contains(e.target) && !provDd.contains(e.target)) hideProviderDropdown();
  };
  document.addEventListener('click', ddAway);
  document.addEventListener('scroll', repositionDropdowns, true);
  window.addEventListener('resize', repositionDropdowns);
  window.visualViewport?.addEventListener('resize', repositionDropdowns);
  window.visualViewport?.addEventListener('scroll', repositionDropdowns);
  docListeners.push(['click', ddAway]);

  // ── load / reload ──
  // Progressive load: show the table FRAME immediately (kept collapsed, so only
  // the slim +/advanced action row is visible) with a lightweight inline spinner
  // over the saved-models area — instead of hiding the whole panel behind one
  // centered spinner until the per-user config read returns.
  //
  // The provider list (fetchPresets — has an INSTANT local fallback) and the saved
  // config (the slow per-user vault+DB read) are INDEPENDENT, so they're fetched
  // together rather than one after the other; the config read, in turn, is a
  // single combined bundle (see the admin adapter) instead of two round-trips.
  async function reload() {
    table.style.display = '';
    // Keep the add-model form visible while its data loads; the user can still
    // collapse it with the + / × toggle.
    table.classList.remove('ac-cfg-collapsed');
    // Show skeleton immediately so the user sees structure (header + phantom rows)
    // instead of just the Advanced/+ buttons during the async fetch.
    if (skelRoot) skelRoot.style.display = '';
    try {
      const [presets, cfg, directives] = await Promise.all([
        Promise.resolve(fetchPresets()).catch(() => ({})),
        Promise.resolve(adapter.loadConfig()).catch((e) => { console.warn('DEBUG-TAG: model-table: load failed', e); return {}; }),
        Promise.resolve(adapter.loadDirectives ? adapter.loadDirectives() : null).catch(() => null),
      ]);
      if (subscriptionDestroyed) return;
      // Per-agent roster-role prompt directives — null hides the strip entirely
      // (mounts without a directives read).
      const d = (directives && typeof directives === 'object') ? directives : null;
      if (d) d.custom = (d.custom && typeof d.custom === 'object') ? d.custom : {};
      S.directives = d;
      // Hide skeleton before rendering real data so there's no overlap flash
      if (skelRoot) skelRoot.style.display = 'none';
      S.presets = presets || {};
      provSel.innerHTML = '';
      const appendProviderOption = (key, preset) => {
        const option = document.createElement('option');
        option.value = key;
        option.textContent = preset.name;
        provSel.appendChild(option);
      };
      if (userCatalog) {
        // User catalog ordering is intentional: app catalog first, Custom
        // second, then explicit BYO providers. Do not make all providers look
        // like platform catalog sources.
        if (S.presets.webagent_catalog) appendProviderOption('webagent_catalog', S.presets.webagent_catalog);
        const custom = document.createElement('option'); custom.value = '_custom'; custom.textContent = 'Custom'; provSel.appendChild(custom);
        for (const [key, preset] of Object.entries(S.presets)) {
          if (key !== 'webagent_catalog') appendProviderOption(key, preset);
        }
      } else {
        for (const [key, preset] of Object.entries(S.presets)) appendProviderOption(key, preset);
        const custom = document.createElement('option'); custom.value = '_custom'; custom.textContent = 'Custom'; provSel.appendChild(custom);
      }

      S.providerConfigs = cfg.providerConfigs || {};
      S.currentProvider = (cfg.provider && S.presets[cfg.provider])
        ? cfg.provider
        : (userCatalog && S.presets.webagent_catalog ? 'webagent_catalog' : '_custom');
      // Drives both the hidden option store and the visible combobox, so the
      // field always opens on the configured provider — WebAgent Catalog by
      // default — instead of an empty or placeholder state.
      syncProviderField();
      urlInput.value = cfg.base_url || '';
      keyInput.value = cfg.api_key || '';
      S.selectedModel = inlineCatalogAdd ? '' : (cfg.model || '');
      modelSearch.value = inlineCatalogAdd ? '' : S.selectedModel;
      S.selCaps = { text: cfg.text_capable !== false, image: !!cfg.image_capable, imageOut: !!cfg.image_out_capable, voice: !!cfg.voice_capable };
      renderDetectedCaps(S.selectedModel ? { text_capable: S.selCaps.text, image_capable: S.selCaps.image, image_out_capable: S.selCaps.imageOut, voice_capable: S.selCaps.voice } : null);
      setModelStatus(S.selectedModel ? `Selected: ${S.selectedModel}` : '', true);

      const norm = (p) => ({
        entry_id: p.entry_id || '',
        connection_id: p.connection_id || '',
        catalog_source_id: p.catalog_source_id || '',
        alias_group_id: p.alias_group_id || '',
        display_name: p.display_name || '',
        // MODEL-NAME-LEVELS: the catalog's own model name for a catalog row (its
        // authored alias). Blank on a BYO row, whose model name is the real model id.
        catalog_name: p.catalog_name || '',
        // MODEL-CATALOG-IDENTITY: keep the catalog marker on the normalized row.
        // Without it, catalog-adopted rows are indistinguishable from BYO rows in
        // S.providers, so catalog presentation never fires and the My Agents save
        // path computes an empty platform_entry_ids set.
        catalog_origin: p.catalog_origin || '',
        // An agent-derived BYO row is shown as available but is not part of the
        // account roster write lane (the adapter retains it for display only).
        catalog_entry_source: p.catalog_entry_source || '',
        provider: p.provider || 'openrouter', auth_mode: p.auth_mode || (p.provider === 'openai_subscription' ? 'chatgpt' : 'api_key'), base_url: p.base_url || '', api_key: p.api_key || '', model: p.model || '',
        decision_capable: !!p.decision_capable, text_capable: p.text_capable !== false, image_capable: !!p.image_capable,
        image_out_capable: !!p.image_out_capable,
        high_effort_capable: !!p.high_effort_capable, voice_capable: !!p.voice_capable, video_capable: !!p.video_capable,
        routing_mode: p.routing_mode || '',
        credential_scope: p.credential_scope || (p.provider === 'openai_subscription' ? subscriptionScope : ''),
        credential_configured: !!p.credential_configured,
        credential_suffix: p.credential_suffix || '',
        credential_email: p.credential_email || '',
        credential_plan_type: p.credential_plan_type || '',
        use_for_system: !!p.use_for_system,
        use_for_voice: !!p.use_for_voice, _uid: ++S.uid,
      });
      S.providers = (Array.isArray(cfg.roster) ? cfg.roster : []).map(norm);
      // MODEL-LEGEND-ALIAS: publish the normalised roster so the sibling analytics
      // chart can replace raw model ids with the display names shown on these rows.
      if (onRoster) { try { onRoster(S.providers); } catch (_) {} }
      S.roleAssignments = (cfg.role_assignments && typeof cfg.role_assignments === 'object')
        ? { ...cfg.role_assignments } : {};
      S.defaultEntryId = userCatalog ? String(cfg.default_entry_id || '') : '';
      // Per-agent table: inherited (app-default) rows ALWAYS stay visible, even
      // when the agent also owns a row for the same (provider|base_url|model) —
      // the redundant pair is intentional, so the agent admin can see the app
      // default and their own copy side by side. The backend union keeps the
      // agent's own row authoritative for that model at runtime; editing roles
      // on the inherited row still materializes a per-agent override.
      renderSaved();
      // Always expose the add-model form on initial load, including for rosters
      // that already have saved models. The + / × toggle still controls manual
      // collapse and re-expansion.
      table.classList.remove('ac-cfg-collapsed');
      renderSubscriptionAuth();
      if (userCatalog || isSubscriptionProvider() || keyInput.value) loadModels();
    } catch (e) {
      console.warn('DEBUG-TAG: model-table: render failed', e);
    } finally {
      table.style.display = '';   // frame was shown up front; nothing to un-hide
    }
  }

  function destroy() {
    subscriptionDestroyed = true;
    table.__reorderCleanup?.();
    clearTimeout(fetchDebounce); clearTimeout(autosaveDebounce);
    subscriptionCountdownHandles.forEach(timer => clearInterval(timer));
    subscriptionCountdownHandles.clear();
    // Cancel whichever dropdown repaint is still pending, per dropdown, since the
    // frame handles now live in the shared WeakMap rather than one module-level var.
    [modelDd, provDd].forEach((dd) => {
      const pending = dropdownRafs.get(dd);
      if (pending) { cancelAnimationFrame(pending); dropdownRafs.delete(dd); }
    });
    if (_directiveModalCleanup) { try { _directiveModalCleanup(); } catch (_) {} _directiveModalCleanup = null; }
    docListeners.forEach(([t, fn]) => document.removeEventListener(t, fn));
    document.removeEventListener('scroll', repositionDropdowns, true);
    window.removeEventListener('resize', repositionDropdowns);
    window.visualViewport?.removeEventListener('resize', repositionDropdowns);
    window.visualViewport?.removeEventListener('scroll', repositionDropdowns);
    modelDd.remove();
    provDd.remove();   // portaled to body when open, so it needs removing too
    clearTimeout(credentialValidationTimer);
    host.innerHTML = '';
  }

  reload();
  return { reload, destroy, getState: () => ({ ...S }) };
}

'use strict';

/**
 * icons.js — lightweight Lucide icon helper
 *
 * Usage in innerHTML / template literals:
 *   el.innerHTML = `${icon('trash-2')} Delete`;
 *
 * The MutationObserver at the bottom automatically calls lucide.createIcons()
 * whenever new <i data-lucide="..."> elements appear in the DOM, so callers
 * never need to call createIcons() manually.
 */

/**
 * Returns an <i> tag string that Lucide will replace with an inline SVG.
 * @param {string} name  Lucide icon name in kebab-case (e.g. 'trash-2')
 * @param {Object} [opts]
 * @param {string} [opts.size='1em']   CSS width/height (e.g. '14px', '1em')
 * @param {string} [opts.cls='']       Extra CSS class names
 * @param {string} [opts.style='']     Extra inline CSS appended to the style attr
 */
export function icon(name, opts = {}) {
  const size  = opts.size  || '1em';
  const cls   = opts.cls   ? ` ${opts.cls}` : '';
  const extra = opts.style || '';
  return `<i data-lucide="${name}" class="ui-icon${cls}" style="width:${size};height:${size};display:inline-flex;align-items:center;justify-content:center;vertical-align:middle;flex-shrink:0;${extra}" aria-hidden="true"></i>`;
}

// ── Chat-element type icons (COMPONENT-TYPE-ICON-STANDARD) ────────────────
// One icon per chat-component type, used by the session-dropdown chips and the
// agent panel head. todo_list (Checklist) → check in a box; status
// (Overview/plan) → square-menu (a square of text lines — the pinned Lucide
// 0.469.0 has no `square-text`, so that name warned and rendered blank);
// scratchpad (the user's notes) → notepad-text; choice / approval / form
// (elements that have buttons) → square-square.
export function componentTypeIcon(type) {
  return type === 'todo_list' ? 'check-square'
       : type === 'status' ? 'square-menu'
       : type === 'scratchpad' ? 'notepad-text'
       : 'square-square';
}

// ── Claude spark mark (NOT a Lucide icon) ───────────────────────────────────────
// The REAL Claude logo, straight from Anthropic's official asset — the orange
// (#D97757) radial sunburst of tapered petals on a transparent tile. Ships as a
// static file in the claude_code engine's own asset folder and is served by the
// generic /plugins/engines mount (see app/main.py "_UIStaticFiles" — engines may
// ship a ui/ folder). Referenced here by URL so the engine owns its brand mark
// and we never re-draw it from memory. The auto-render observer below ignores it
// (no data-lucide attribute).
const CLAUDE_MARK_URL = '/plugins/engines/claude_code/ui/claude-color.svg';

/**
 * Returns an <img> string of the real Claude logo (orange spark).
 * @param {Object} [opts]
 * @param {string} [opts.size='1em']  CSS width/height (e.g. '24px', '1em')
 * @param {string} [opts.cls='']      Extra CSS class names
 * @param {string} [opts.style='']    Extra inline CSS appended to the style attr
 */
export function claudeMark(opts = {}) {
  const size  = opts.size  || '1em';
  const cls   = opts.cls   ? ` ${opts.cls}` : '';
  const extra = opts.style || '';
  return `<img src="${CLAUDE_MARK_URL}" alt="" aria-hidden="true" draggable="false" class="ui-icon claude-mark${cls}" `
    + `style="width:${size};height:${size};display:inline-block;flex-shrink:0;vertical-align:middle;object-fit:contain;${extra}">`;
}

// ── Codex knot mark (NOT a Lucide icon) ────────────────────────────────────────
// The REAL Codex logo, straight from OpenAI's official asset — a white rounded
// square tile with the purple-blue gradient knot inside (the square sibling of
// ChatGPT's hexagonal knot). Ships as a static file in the codex engine's own
// asset folder and is served by the generic /plugins/engines mount (see
// app/main.py "_UIStaticFiles" — engines may ship a ui/ folder). Referenced
// here by URL so the engine owns its brand mark and we never re-draw it from
// memory. The auto-render observer below ignores it (no data-lucide attribute).
const CODEX_MARK_URL = '/plugins/engines/codex/ui/codex-color.svg';

/**
 * Returns an <img> string of the real Codex logo (tile + gradient knot).
 * @param {Object} [opts]
 * @param {string} [opts.size='1em']  CSS width/height (e.g. '24px', '1em')
 * @param {string} [opts.cls='']      Extra CSS class names
 * @param {string} [opts.style='']    Extra inline CSS appended to the style attr
 */
export function codexMark(opts = {}) {
  const size  = opts.size  || '1em';
  const cls   = opts.cls   ? ` ${opts.cls}` : '';
  const extra = opts.style || '';
  return `<img src="${CODEX_MARK_URL}" alt="" aria-hidden="true" draggable="false" class="ui-icon codex-mark${cls}" `
    + `style="width:${size};height:${size};display:inline-block;flex-shrink:0;vertical-align:middle;object-fit:contain;${extra}">`;
}

// ── Auto-render ────────────────────────────────────────────────────────────────
// Debounced via requestAnimationFrame so rapid DOM mutations are batched into
// one createIcons() call.

let _rafId = null;

// Some older renderers still call lucide.createIcons() without a `nodes` list.
// Lucide interprets that as "replace every [data-lucide] node", including SVGs
// it already rendered. Replacing an SVG between pointerdown and pointerup makes
// its parent button lose the click. Keep the public API compatible while making
// an unscoped call process only fresh placeholders. Explicitly-scoped calls are
// passed through unchanged.
function _installSafeCreateIcons() {
  const api = globalThis.lucide;
  if (!api || typeof api.createIcons !== 'function' || api.createIcons.__waPendingOnly) return;

  const original = api.createIcons.bind(api);
  function safeCreateIcons(options = {}) {
    const opts = options && typeof options === 'object' ? { ...options } : {};
    if (!Object.prototype.hasOwnProperty.call(opts, 'nodes')) {
      const root = opts.root && typeof opts.root.querySelectorAll === 'function'
        ? opts.root
        : document;
      const nodes = Array.from(root.querySelectorAll('[data-lucide]:not(.lucide)'));
      if (root !== document && root.matches?.('[data-lucide]:not(.lucide)')) {
        nodes.unshift(root);
      }
      delete opts.root;
      if (!nodes.length) return;
      opts.nodes = nodes;
    }
    return original(opts);
  }
  safeCreateIcons.__waPendingOnly = true;
  safeCreateIcons.__waOriginal = original;
  api.createIcons = safeCreateIcons;
}

// Convert ONLY unprocessed placeholders. Lucide copies data-lucide onto the
// <svg> it generates AND tags it with class "lucide", so a bare
// createIcons() (which scans every [data-lucide]) re-replaces already-rendered
// icons on every call. Because each replacement is itself a DOM mutation, the
// MutationObserver below would re-fire and we'd spin in a once-per-frame loop —
// constantly tearing icon nodes out from under in-flight clicks (a pointerdown
// on an icon whose node is replaced before mouseup never produces a click).
// Selecting [data-lucide]:not(.lucide) and passing it as `nodes` touches only
// the fresh <i> placeholders, exactly like the rest of the codebase does.
function _renderPending() {
  _installSafeCreateIcons();
  if (typeof lucide === 'undefined') return;
  const nodes = document.querySelectorAll('[data-lucide]:not(.lucide)');
  if (!nodes.length) return;
  lucide.createIcons({ attrs: { 'stroke-width': 1.5 }, nodes: Array.from(nodes) });
}

function _scheduleRender() {
  if (_rafId) return;
  _rafId = requestAnimationFrame(() => {
    _rafId = null;
    _renderPending();
  });
}

function _setup() {
  _installSafeCreateIcons();
  // Initial render for static HTML
  _renderPending();
  // Watch for dynamically inserted icons
  new MutationObserver(_scheduleRender).observe(document.body, {
    childList: true,
    subtree: true,
  });
}

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', _setup);
} else {
  _setup();
}

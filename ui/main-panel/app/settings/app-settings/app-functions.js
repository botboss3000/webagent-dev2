'use strict';

/**
 * App Functions table (App Settings tab).
 *
 * Renders the BACKGROUND app services — Session Namer, Context Control's
 * compaction train, the Render Recorder — that run for the app itself rather
 * than being agent-invoked abilities. They are the ability catalog entries
 * marked `"app_function": true`, copied by `ui_catalog()`
 * (app/abilities/__init__.py) into the catalog's `app_functions` list. A
 * dual-surface function may also expose per-agent preferences. A drop-in
 * ability that sets that flag appears here automatically — no
 * edit to this file or the HTML. See docs/claude/production-editions.md
 * "App functions".
 *
 * Each entry renders as an expandable `.ac-row` (icon + name + description, an
 * on/off toggle, and — for a non-simple function — a settings body). It reuses
 * the SAME app-level config endpoints and shared row/setting helpers as the
 * admin ability table, so an app function's on/off and knobs persist to
 * data/config/agent-abilities.json exactly like an ability's:
 *   • on/off  → POST / DELETE /admin/integrations/abilities/{id}
 *   • config  → GET / PUT     /api/v1/abilities/{id}/config  (+ /config-schema)
 * A `locked_on` function (Context Control) renders its toggle fixed ON.
 */

import { _fetch } from '../utils.js';
import {
  _iconHtml,
  _esc,
  _buildSettingsRowsHtml,
  _markSaving,
  _flashSaveCheck,
} from '../../../../shared/js/dom-utils.js';

const _LIST_ID = 'ac-app-functions-list';
const _CHEVRON =
  '<span class="ac-row-chevron ac-row-chevron--left"><svg width="13" height="13" viewBox="0 0 24 24" ' +
  'fill="none" stroke="currentColor" stroke-width="2.5">' +
  '<polyline points="9 18 15 12 9 6"/></svg></span>';

// Only re-render once per page open — the catalog is stable for the session.
let _built = false;

export async function initAppFunctions() {
  const host = document.getElementById(_LIST_ID);
  if (!host || _built) return;

  let cat;
  try {
    const res = await fetch('/api/v1/abilities/catalog');
    if (!res.ok) return;
    cat = await res.json();
  } catch (_) {
    return;
  }
  const fns = Array.isArray(cat && cat.app_functions) ? cat.app_functions : [];
  if (!fns.length) return;

  _built = true;
  host.innerHTML = '';
  for (const fn of fns) host.appendChild(_buildRow(fn));

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

// Build one expandable app-function row (head + settings body).
function _buildRow(fn) {
  const lockedOn = !!fn.locked_on;
  const enabled = lockedOn ? true : !!fn.enabled;
  const hasBody = fn.simple === false; // non-simple → has a config schema

  const row = document.createElement('div');
  row.className = 'ac-row';
  row.id = 'ac-appfn-' + fn.id;

  const head = document.createElement('div');
  head.className = 'ac-ability-row';
  head.innerHTML =
    (hasBody ? _CHEVRON : '') +
    `<span class="ac-ability-icon ac-ability-icon-colored">` +
      _iconHtml(fn.icon || 'cog', '18px') + '</span>' +
    '<div class="ac-ability-label">' +
      `<div class="ac-ability-name">${_esc(fn.display_name || fn.id)}</div>` +
      `<div class="ac-ability-desc">${_esc(fn.description || '')}</div>` +
    '</div>';

  const acIcon = head.querySelector('.ac-ability-icon');
  if (acIcon) acIcon.style.setProperty('--ac-icon-color', fn.color || 'var(--brand)');

  head.appendChild(_buildToggle(fn, enabled, lockedOn));
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ac-ability-body';
  row.appendChild(body);

  // Expand/collapse on head click — but never when the click lands on the
  // toggle (or any control), so flipping the switch doesn't also toggle the row.
  // EVERY row is clickable, including simple functions with no config body:
  // with no chevron to signal it, the click alone still un-clamps the clamped
  // description so the full text can be read. The empty body stays hidden
  // (app3.css `.ac-row.expanded > .ac-ability-body:empty`), so a simple row
  // just grows vertically — its settings body is never filled. Opening and
  // closing animate smoothly (see `_toggleRow` below).
  head.addEventListener('click', (e) => {
    if (e.target.closest('.ac-config-control, .conn-toggle-wrap, input, button, select, a, label')) return;
    _toggleRow(row, head, body, fn, hasBody);
  });

  return row;
}

// Smooth expand/collapse for an app-function row.
//
// The row's description is clamped to one line (`.ac-ability-desc`) and, for a
// non-simple function, a settings body (`.ac-ability-body`) sits below. On open
// both animate to their exact content height (measured via scrollHeight); on
// close they animate back down and the `expanded` class is only dropped once
// the transition ends, so the 1-line clamp + ellipsis return at the end of the
// slide-up rather than snapping early. The CSS supplying the transitions and
// clipping lives in app3.css scoped to `#ac-app-functions-list`. Under
// `prefers-reduced-motion` the row just snaps open/closed (no transition to
// wait on, so no transitionend dependency).
const _DESC_COLLAPSED_MAXH = '1.4em'; // one clamped line (10.5px font × 1.4 line-height)
const _REDUCED_MOTION =
  typeof window.matchMedia === 'function' &&
  window.matchMedia('(prefers-reduced-motion: reduce)').matches;

function _toggleRow(row, head, body, fn, hasBody) {
  if (_REDUCED_MOTION) {
    const open = !row.classList.contains('expanded');
    row.classList.toggle('expanded');
    if (open && hasBody) _fillBody(fn, body);
    return;
  }

  const desc = head.querySelector('.ac-ability-desc');
  const opening = !row.classList.contains('expanded');
  if (!opening && row.dataset.acAnimating === '1') return; // ignore clicks mid-close

  if (opening) {
    // Pin the collapsed height first so the transition has a definite start
    // point, then un-clamp and measure the full text height.
    if (desc) {
      desc.style.maxHeight = _DESC_COLLAPSED_MAXH;
      row.classList.add('expanded');
      desc.style.maxHeight = desc.scrollHeight + 'px'; // 1 line → full text
    } else {
      row.classList.add('expanded');
    }
    if (hasBody && body) {
      body.style.display = 'block'; // show, starting collapsed at 0
      body.style.maxHeight = '0px';
      void body.offsetHeight; // commit the 0 state so the slide-open has a start point
      body.style.maxHeight = body.scrollHeight + 'px';
      _fillBody(fn, body).then(() => {
        // Settings load async — grow the body to its real height smoothly.
        if (row.classList.contains('expanded')) body.style.maxHeight = body.scrollHeight + 'px';
      });
    }
  } else {
    // Close: keep `expanded` (desc stays un-clamped) while the heights animate
    // down, then drop the class once the transition ends so the clamp and
    // ellipsis return with the collapsed state.
    row.dataset.acAnimating = '1';
    if (desc) desc.style.maxHeight = _DESC_COLLAPSED_MAXH;
    if (hasBody && body) {
      body.style.maxHeight = '0px';
      const hideBody = (e) => {
        if (e.propertyName !== 'max-height') return;
        body.removeEventListener('transitionend', hideBody);
        body.style.display = 'none';
      };
      body.addEventListener('transitionend', hideBody);
    }
    const finish = (e) => {
      if (e.propertyName !== 'max-height') return;
      row.removeEventListener('transitionend', finish);
      row.classList.remove('expanded');
      delete row.dataset.acAnimating;
    };
    row.addEventListener('transitionend', finish);
  }
}

// The right-aligned on/off switch (the same `.conn-toggle` the ability tables
// use). A locked-on function is fixed ON and cannot be changed.
function _buildToggle(fn, enabled, lockedOn) {
  const ctrl = document.createElement('span');
  ctrl.className = 'ac-config-control';
  const title = lockedOn ? 'Always on — cannot be deactivated' : '';
  ctrl.innerHTML =
    `<label class="conn-toggle-wrap ac-ability-toggle-wrap${lockedOn ? ' ac-ability-toggle-locked' : ''}"` +
      (title ? ` title="${_esc(title)}"` : '') + '>' +
      `<input type="checkbox" class="conn-toggle"${enabled ? ' checked' : ''}${lockedOn ? ' disabled' : ''}>` +
      '<span class="conn-toggle-track"></span>' +
    '</label>';

  if (lockedOn) return ctrl;

  const input = ctrl.querySelector('input');
  input.addEventListener('change', async () => {
    const desired = input.checked;
    input.disabled = true;
    _markSaving(ctrl);
    try {
      const res = await _fetch(
        '/admin/integrations/abilities/' + encodeURIComponent(fn.id),
        { method: desired ? 'POST' : 'DELETE' },
      );
      if (!res.ok) throw new Error('save failed');
      try {
        window.dispatchEvent(new CustomEvent('app-function-changed', {
          detail: { id: fn.id, enabled: desired },
        }));
      } catch (_) {}
      _flashSaveCheck(ctrl, true);
    } catch (_) {
      input.checked = !desired; // revert
      _flashSaveCheck(ctrl, false);
    } finally {
      input.disabled = false;
    }
  });
  return ctrl;
}

// Fill the settings body on first expand: the function's config-schema rows
// (pre-filled with saved app-level values), each saving on change. Cheap
// functions with no schema show a short note instead.
async function _fillBody(fn, body) {
  if (body.dataset.filled) return;
  body.dataset.filled = '1';
  body.innerHTML = '<div class="ac-hint">Loading…</div>';

  // App Functions include their non-secret schema in the catalog so the
  // expanded settings appear even if the separate schema request is briefly
  // unavailable. This is especially important for quota controls: an admin
  // must always be able to see and edit the active size limit.
  let schema = (fn.config && Array.isArray(fn.config.settings)) ? fn.config : null;
  let current = {};
  try {
    const [sRes, cRes] = await Promise.all([
      fetch('/api/v1/abilities/' + encodeURIComponent(fn.id) + '/config-schema?scope=app'),
      _fetch('/api/v1/abilities/' + encodeURIComponent(fn.id) + '/config'),
    ]);
    if (sRes.ok) schema = await sRes.json();
    if (cRes.ok) {
      const cData = await cRes.json();
      if (cData && cData.ability_settings && typeof cData.ability_settings === 'object') {
        current = cData.ability_settings;
      }
    }
  } catch (_) { /* fall through to the empty note */ }

  if (!schema || !Array.isArray(schema.settings) || !schema.settings.length) {
    body.innerHTML = '<div class="ac-hint">No additional settings.</div>';
    return;
  }

  body.innerHTML = _buildSettingsRowsHtml(schema, current, 'ac-appfn-field', 'data-config-key');
  const schemaNote = schema.note || schema.notes;
  if (schemaNote) {
    body.insertAdjacentHTML('afterbegin', `<div class="ac-hint">${_esc(schemaNote)}</div>`);
  }

  const fields = [...body.querySelectorAll('.ac-appfn-field')];
  let quotaSummary = null;
  const updateQuotaSummary = () => {
    if (!quotaSummary) return;
    const byKey = Object.fromEntries(fields.map((f) => [f.dataset.configKey, f]));
    const total = Math.max(0, Number(byKey.max_size_mb?.value || 100));
    const tool = Math.max(0, Math.min(80, Number(byKey.tool_output_share_percent?.value || 0)));
    let memory = Math.max(0, Math.min(50, Number(byKey.memory_share_percent?.value || 0)));
    memory = Math.min(memory, Math.max(0, 90 - tool));
    const conversation = 100 - tool - memory;
    const mb = (pct) => (total * pct / 100).toLocaleString(undefined, { maximumFractionDigits: 1 });
    quotaSummary.textContent = `Effective split: Messages ${conversation}% (${mb(conversation)} MB) · `
      + `Tool outputs ${tool}% (${mb(tool)} MB) · Memory ${memory}% (${mb(memory)} MB)`;
  };
  if (fn.id === 'user_database_size_limit') {
    quotaSummary = document.createElement('div');
    quotaSummary.className = 'ac-hint';
    body.insertAdjacentElement('afterbegin', quotaSummary);
    updateQuotaSummary();
    fields.forEach((f) => f.addEventListener('input', updateQuotaSummary));
  }
  const collect = () => {
    const out = {};
    fields.forEach((f) => { out[f.dataset.configKey] = f.value; });
    return out;
  };
  fields.forEach((f) => {
    const fieldCtrl = f.closest('.ac-config-control');
    let prev = f.value;
    f.addEventListener('change', async () => {
      f.disabled = true;
      _markSaving(fieldCtrl);
      try {
        const res = await _fetch(
          '/api/v1/abilities/' + encodeURIComponent(fn.id) + '/config',
          {
            method: 'PUT',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ ability_settings: collect() }),
          },
        );
        if (!res.ok) throw new Error('save failed');
        prev = f.value;
        updateQuotaSummary();
        _flashSaveCheck(fieldCtrl, true);
      } catch (_) {
        f.value = prev; // revert to last saved
        updateQuotaSummary();
        _flashSaveCheck(fieldCtrl, false);
      } finally {
        f.disabled = false;
      }
    });
  });

  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

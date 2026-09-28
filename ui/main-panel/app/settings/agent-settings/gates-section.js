'use strict';

/**
 * Gates table (App Settings → "Agent tools & abilities").
 *
 * Lists every run-contract gate TYPE discovered in plugins/gates/ — the same
 * drop-in catalog the per-agent Modes page renders per agent
 * (ui/main-panel/agents/js/content.js). A gate dropped into the gates
 * directory appears here with no edit to this file or to the markup: the rows
 * come from GET /admin/settings/gates, which serves the registry catalog
 * (app/agent/gate_registry.py) directly.
 *
 * Every row is FIXED ON for now. Gate types are discovered once at process
 * start (`gate_types()` caches the registry), so a type is always available to
 * agents and cannot be switched off — there is nothing to persist. The row
 * therefore shows a locked, disabled toggle instead of one that would silently
 * fail to save. See the `locked_on` flag the endpoint sets.
 *
 * Expanding a row reveals that gate's APP-LEVEL params — exactly the params the
 * gate declares, nothing generic. It writes data/config/app-settings.json →
 * gate_settings[<gate id>] via PUT /admin/settings/gates/{id}, and the gate's own
 * resolver stacks them app-wide BEFORE any per-agent value appends
 * (app/agent/closing_message.py). There is deliberately no generic "guidance"
 * blob: for closing_message these rows ARE the app-wide guidance — the flag
 * taxonomy plus the notification and output-format guidance.
 *
 * Breadcrumbs: caller ui/main-panel/app/settings/agent-settings/
 * agent-settings.js (init); markup ui/main-panel/app/settings/pages/
 * agent-tools/page.html (#ac-cat-gates); row layout from dom-utils.js
 * (`buildGateParamRowsHtml` / `wireGateParamRows` — the SAME stacked
 * name → control → description rows an agent's Modes-page gate body uses, so the
 * two surfaces stay mirrored); styling shared/css/app3.css (`.ac-gate-param*`;
 * `.ac-row` / `.ac-ability-body` reused for the row shell).
 */

import { _fetch } from '../utils.js';
import {
  _esc,
  buildGateParamRowsHtml,
  wireGateParamRows,
  _markSaving,
  _flashSaveCheck,
} from '../../../../shared/js/dom-utils.js';

const _LIST_ID = 'ac-gates-list';
const _COUNT_ID = 'ac-cat-gates-count';
const _CHEVRON =
  '<span class="ac-row-chevron ac-row-chevron--left"><svg width="13" height="13" viewBox="0 0 24 24" ' +
  'fill="none" stroke="currentColor" stroke-width="2.5">' +
  '<polyline points="9 18 15 12 9 6"/></svg></span>';

// Only build once per page open — the catalog is stable for the session.
let _built = false;

export async function init() {
  const host = document.getElementById(_LIST_ID);
  if (!host || _built) return;

  let data;
  try {
    const res = await _fetch('/admin/settings/gates');
    if (!res.ok) return;
    data = await res.json();
  } catch (_) {
    return;
  }
  const gates = Array.isArray(data && data.gates) ? data.gates : [];
  if (!gates.length) return;

  _built = true;
  host.innerHTML = '';
  for (const gate of gates) host.appendChild(_buildRow(gate));

  const count = document.getElementById(_COUNT_ID);
  if (count) count.textContent = String(gates.length);
  if (window.lucide) { try { lucide.createIcons(); } catch (_) {} }
}

// Build one expandable gate row (head + app-level settings body).
function _buildRow(gate) {
  const row = document.createElement('div');
  row.className = 'ac-row';
  row.id = 'ac-gate-' + gate.id;

  const head = document.createElement('div');
  head.className = 'ac-ability-row';
  head.innerHTML =
    _CHEVRON +
    '<span class="ac-ability-icon"><i data-lucide="door-closed" ' +
      'class="lucide-icon ac-ico-18 ac-fg-accent"></i></span>' +
    '<div class="ac-ability-label">' +
      `<div class="ac-ability-name">${_esc(gate.display_name || gate.id)}</div>` +
      `<div class="ac-ability-desc">${_esc(gate.description || '')}</div>` +
    '</div>' +
    _lockedToggle();
  row.appendChild(head);

  const body = document.createElement('div');
  body.className = 'ac-ability-body';
  row.appendChild(body);

  // Expand/collapse on head click — never when the click lands on a control.
  head.addEventListener('click', (e) => {
    if (e.target.closest('.ac-config-control, .conn-toggle-wrap, input, button, select, a, label')) return;
    const open = !row.classList.contains('expanded');
    row.classList.toggle('expanded');
    if (open) _fillBody(gate, body);
  });

  return row;
}

// The right-aligned switch, fixed ON. Every gate type is loaded at process
// start and cannot be turned off yet, so the control is disabled and titled to
// say why rather than pretending to be interactive.
function _lockedToggle() {
  return '<span class="ac-config-control">' +
    '<label class="conn-toggle-wrap ac-ability-toggle-wrap ac-ability-toggle-locked" ' +
      'title="Always on — gate types load at startup and cannot be turned off yet">' +
      '<input type="checkbox" class="conn-toggle" checked disabled>' +
      '<span class="conn-toggle-track"></span>' +
    '</label>' +
  '</span>';
}

// Fill the app-level settings body on first expand, then save on change.
async function _fillBody(gate, body) {
  if (body.dataset.filled) return;
  body.dataset.filled = '1';

  const schema = gate.app_schema && Array.isArray(gate.app_schema.settings)
    ? gate.app_schema : null;
  if (!schema || !schema.settings.length) {
    body.innerHTML = '<div class="ac-hint">This gate has no app-level settings.</div>';
    return;
  }

  body.innerHTML = buildGateParamRowsHtml(
    schema, gate.values || {}, 'ac-gate-setting', 'data-gate-key',
  );
  wireGateParamRows(body);

  for (const input of body.querySelectorAll('.ac-gate-setting')) {
    input.addEventListener('change', () => _save(gate, body));
  }
}

// Persist every control in the body — the endpoint merges, so sending the whole
// set keeps clearing a field and setting another in one save consistent.
async function _save(gate, body) {
  const params = {};
  const types = new Map(((gate.app_schema && gate.app_schema.settings) || []).map(
    setting => [setting.key, setting.type],
  ));
  for (const input of body.querySelectorAll('.ac-gate-setting')) {
    const key = input.getAttribute('data-gate-key');
    params[key] = types.get(key) === 'boolean' ? input.value === 'true' : input.value;
  }
  const ctrl = body.querySelector('.ac-gate-param-control') || body;
  _markSaving(ctrl);
  try {
    const res = await _fetch('/admin/settings/gates/' + encodeURIComponent(gate.id), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ params }),
    });
    if (!res.ok) throw new Error('save failed');
    // Keep the local snapshot in step with what was persisted, so re-opening
    // the row in this session shows the saved state.
    const data = await res.json().catch(() => null);
    if (data && data.values && typeof data.values === 'object') {
      gate.values = data.values;
    }
    _flashSaveCheck(ctrl, true);
  } catch (_) {
    _flashSaveCheck(ctrl, false);
  }
}

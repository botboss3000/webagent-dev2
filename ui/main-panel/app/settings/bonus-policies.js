'use strict';

import { _fetch } from './utils.js';
import { _esc } from '../../../shared/js/dom-utils.js';

const API = '/api/v1/active-session-bonus/policies';
let state = null;
let saveTimer = null;
let initialized = false;

function host() { return document.getElementById('bonus-policies-list'); }
function hint() { return document.getElementById('bonus-policies-hint'); }

function actionOptions(selected) {
  const options = [
    ['open_reset', 'Open and reset flag'],
    ['continue', 'Open and continue chat'],
    ['complete', 'Open and complete task'],
  ];
  return options.map(([value, label]) => `<option value="${value}"${value === selected ? ' selected' : ''}>${label}</option>`).join('');
}

function render() {
  const target = host();
  if (!target || !state) return;
  target.innerHTML = state.policies.map((policy) => `
    <div class="ac-ability-row" data-bonus-flag="${_esc(policy.flag)}">
      <span class="ac-ability-icon"><i data-lucide="flag" class="lucide-icon ac-ico-16 ac-fg-accent"></i></span>
      <div class="ac-ability-label">
        <div class="ac-ability-name">${_esc(policy.flag)}</div>
        <div class="ac-ability-desc">Maximum bonus decays linearly to the payable floor over this timer; required action settles it.</div>
      </div>
      <span class="ac-config-control" style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <label class="ac-hint">Max <input class="ac-gate-param-control" data-policy-field="max_bonus" type="number" min="1" max="10000" value="${Number(policy.max_bonus)}" aria-label="Maximum bonus for ${_esc(policy.flag)}"></label>
        <label class="ac-hint">Timer (minutes) <input class="ac-gate-param-control" data-policy-field="resolution_minutes" type="number" min="1" max="43200" step="1" value="${Number(policy.resolution_seconds) / 60}" aria-label="Resolution timer in minutes for ${_esc(policy.flag)}"></label>
        <label class="ac-hint">Required <select class="ac-gate-param-control" data-policy-field="resolution_action" aria-label="Required resolution for ${_esc(policy.flag)}">${actionOptions(policy.resolution_action)}</select></label>
      </span>
    </div>`).join('');
  target.querySelectorAll('[data-policy-field]').forEach((input) => input.addEventListener('change', queueSave));
  if (window.lucide) { try { window.lucide.createIcons(); } catch (_) {} }
}

function readRows() {
  return [...host().querySelectorAll('[data-bonus-flag]')].map((row) => ({
    flag: row.dataset.bonusFlag,
    max_bonus: Number(row.querySelector('[data-policy-field="max_bonus"]').value),
    resolution_seconds: Number(row.querySelector('[data-policy-field="resolution_minutes"]').value) * 60,
    resolution_action: row.querySelector('[data-policy-field="resolution_action"]').value,
  }));
}

function setHint(message, error = false) {
  const node = hint();
  if (!node) return;
  node.textContent = message;
  node.classList.toggle('ac-fg-warning', error);
}

function queueSave() {
  clearTimeout(saveTimer);
  setHint('Saving notification flag policies…');
  saveTimer = setTimeout(save, 300);
}

async function save() {
  try {
    const response = await _fetch(API, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ policies: readRows() }) });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Policy save failed');
    state = data;
    render();
    setHint(`Saved ${state.policies.length} ${state.policies.length === 1 ? 'policy' : 'policies'} matching the current closing-message flags.`);
  } catch (error) {
    setHint(error.message || 'Policy save failed. Refresh the flag list and try again.', true);
  }
}

export async function loadBonusPolicies() {
  const target = host();
  if (!target) return;
  try {
    const response = await _fetch(API, { cache: 'no-store' });
    if (response.status === 403) { target.innerHTML = ''; setHint('Administrator access is required to configure bonus policies.', true); return; }
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.detail || 'Could not load notification flag policies');
    state = data;
    render();
    setHint(!data.matches ? 'The saved policy list no longer matches the current closing-message flags. Refresh and save the complete list.' : `Editing ${data.policies.length} policy rows from the current closing-message flag list.`);
  } catch (error) {
    target.innerHTML = '';
    setHint(error.message || 'Could not load notification flag policies.', true);
  }
}

export function initBonusPolicies() {
  if (initialized) return;
  initialized = true;
}

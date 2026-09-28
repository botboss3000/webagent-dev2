'use strict';

import { apiPath } from '../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js?v=253';
import { _fetch, _esc } from './utils.js';
import { attachExperienceTierEditor } from './entitlements/entitlements.js';

let _initialized = false;
let _mode = '';
function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }

function _accessMarkup() {
  const options = [
    ['admin_approval', 'Private', 'New users can register, but an administrator must approve access.'],
    ['public_registered', 'Open Registration', 'Anyone may create an account and sign in.'],
  ];
  return '<div class="members-policy-title">Access policy</div><div class="members-policy-choices">'
    + options.map(([value, label, hint]) => '<label class="members-policy-opt' + (_mode === value ? ' active' : '') + '">'
      + '<input type="radio" name="user-policy-access-mode" value="' + value + '"' + (_mode === value ? ' checked' : '') + '>'
      + '<div class="members-policy-opt-body"><div class="members-policy-opt-label">' + _esc(label) + '</div><div class="members-policy-opt-hint">' + _esc(hint) + '</div></div></label>').join('')
    + '</div>';
}

async function _loadAccess() {
  const host = document.getElementById('user-policies-access');
  if (!host) return;
  try {
    const response = await _fetch(apiPath('/admin/settings/app'), { cache: 'no-store' });
    const data = response.ok ? await response.json() : {};
    _mode = data.access_mode || 'admin_approval';
  } catch (_) { _mode = _mode || 'admin_approval'; }
  host.innerHTML = _accessMarkup();
  host.querySelectorAll('input[name="user-policy-access-mode"]').forEach(input => input.addEventListener('change', async event => {
    if (!isAdmin()) { showRestrictedModal(); await _loadAccess(); return; }
    const selected = event.currentTarget.value;
    const response = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ access_mode: selected }),
    });
    if (!response.ok) { alert('Failed to update access policy.'); return; }
    _mode = (await response.json().catch(() => ({}))).access_mode || selected;
    await _loadAccess();
  }));
}

export function init() {
  if (_initialized) return;
  _initialized = true;
  document.getElementById('user-policies-manage-usage')?.addEventListener('click', () => {
    document.querySelector('[data-tab="users"]')?.click();
  });
}

export async function load() {
  init();
  const tiers = document.getElementById('user-policies-tiers');
  await Promise.all([
    _loadAccess(),
    tiers ? attachExperienceTierEditor(tiers) : Promise.resolve(),
  ]);
}

'use strict';

/**
 * Scheduler engine panel — provider configuration, test/save/push/status.
 *
 * Relocated from the former App Config "Automation" tab; now rendered inside
 * Agent Settings → "Automation Engine" (the scheduler half). Wired by
 * agent-settings.js, which calls init() once and load() lazily on first expand.
 */

import { apiPath } from '../../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { _fetch, _qs, _esc } from '../utils.js';

let _schedProviders = [];
let _schedConfig = { provider: 'local', providers: {} };

// ── SECTION 6b: Automation (scheduler provider) ──────────────────────────
// ─────────────────────────────────────────────────────────────────────────

export function init() {
  _qs('ac-sched-save')?.addEventListener('click', _saveAutomation);
  _qs('ac-sched-refresh')?.addEventListener('click', _loadAutomationStatus);
  _qs('ac-sched-test')?.addEventListener('click', _testAutomation);
  _qs('ac-sched-sync')?.addEventListener('click', _syncAutomation);
  _qs('ac-sched-provider')?.addEventListener('change', _renderSchedFields);
}

function _findProvider(id) {
  return (_schedProviders || []).find(p => p.id === id) || null;
}

function _renderSchedFields() {
  const provSel = _qs('ac-sched-provider');
  const descEl = _qs('ac-sched-provider-desc');
  const host = _qs('ac-sched-fields');
  if (!provSel || !host) return;
  const provId = provSel.value || 'local';
  const meta = _findProvider(provId);
  if (descEl) descEl.textContent = meta?.description || '';

  host.innerHTML = '';
  const saved = (_schedConfig.providers || {})[provId] || {};

  if (provId === 'local' || !meta || !(meta.fields || []).length) {
    const empty = document.createElement('div');
    empty.className = 'ac-hint';
    empty.classList.add('ac-fs11');
    empty.textContent = meta?.fields?.length
      ? ''
      : 'No credentials required.';
    host.appendChild(empty);
    return;
  }

  for (const f of meta.fields) {
    const wrap = document.createElement('div');
    const lbl = document.createElement('label');
    lbl.className = 'ac-label';
    lbl.textContent = f.label + (f.required ? ' *' : '');
    wrap.appendChild(lbl);

    let input;
    if (f.type === 'textarea') {
      input = document.createElement('textarea');
      input.className = 'ac-input';
      input.classList.add('ac-sched-input');
    } else {
      input = document.createElement('input');
      input.className = 'ac-input';
      input.type = (f.type === 'password' || f.secret) ? 'password' : 'text';
    }
    input.dataset.key = f.key;
    input.value = saved[f.key] ?? '';
    if (f.placeholder) input.placeholder = f.placeholder;
    wrap.appendChild(input);

    if (f.secret) {
      const hint = document.createElement('div');
      hint.className = 'ac-hint';
      hint.classList.add('ac-sched-hint');
      hint.textContent = 'Stored in scheduler_config.json (plaintext).';
      wrap.appendChild(hint);
    }

    host.appendChild(wrap);
  }
}

function _collectSchedSettings() {
  const host = _qs('ac-sched-fields');
  const out = {};
  if (!host) return out;
  host.querySelectorAll('[data-key]').forEach(el => {
    out[el.dataset.key] = el.value || '';
  });
  return out;
}

export async function load() {
  await _loadAutomationProviders();
  await _loadAutomationConfig();
  _renderSchedFields();
  await _loadAutomationStatus();
}

async function _loadAutomationProviders() {
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler/providers'));
    if (!r.ok) return;
    const d = await r.json();
    _schedProviders = d.providers || [];
    const sel = _qs('ac-sched-provider');
    if (sel) {
      sel.innerHTML = '';
      for (const p of _schedProviders) {
        const opt = document.createElement('option');
        opt.value = p.id;
        opt.textContent = p.display_name;
        sel.appendChild(opt);
      }
    }
  } catch (_) {}
}

async function _loadAutomationConfig() {
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler'));
    if (!r.ok) return;
    const d = await r.json();
    _schedConfig = {
      provider: d.provider || 'local',
      providers: d.providers || {},
    };
    const sel = _qs('ac-sched-provider');
    if (sel) sel.value = _schedConfig.provider;
  } catch (_) {}
}

async function _loadAutomationStatus() {
  const badge = _qs('ac-sched-active-badge');
  const statusEl = _qs('ac-sched-status');
  try {
    const r = await _fetch(apiPath('/admin/scheduler/status'));
    if (r.ok) {
      const d = await r.json();
      const running = !!d.running;
      if (badge) {
        badge.textContent = running ? `${d.provider} · running` : `${d.provider || 'unknown'} · stopped`;
        badge.classList.add(running ? 'ac-fg-success' : 'ac-fg-danger'); badge.classList.remove(running ? 'ac-fg-danger' : 'ac-fg-success');
      }
      if (statusEl) statusEl.textContent = JSON.stringify(d, null, 2);
    }
  } catch (e) {
    if (statusEl) statusEl.textContent = `Error fetching status: ${e.message}`;
  }
}

async function _saveAutomation() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provider = _qs('ac-sched-provider')?.value || 'local';
  const settings = _collectSchedSettings();
  const statusEl = _qs('ac-sched-status');
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, settings }),
    });
    if (!r.ok) {
      const txt = await r.text();
      throw new Error(`HTTP ${r.status}: ${txt}`);
    }
    if (statusEl) statusEl.textContent = 'Saved. Reloading…';
    _schedConfig.provider = provider;
    _schedConfig.providers = { ..._schedConfig.providers, [provider]: settings };
    setTimeout(_loadAutomationStatus, 400);
  } catch (e) {
    if (statusEl) statusEl.textContent = `Save failed: ${e.message}`;
  }
}

async function _testAutomation() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const provider = _qs('ac-sched-provider')?.value || 'local';
  const settings = _collectSchedSettings();
  const out = _qs('ac-sched-test-result');
  if (out) { out.textContent = 'Testing…'; out.classList.add('ac-fg3'); out.classList.remove('ac-fg-success','ac-fg-danger'); }
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler/test'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ provider, settings }),
    });
    const d = await r.json();
    if (out) {
      if (d.ok) {
        out.textContent = '✓ ' + (d.message || 'Connection OK');
        out.classList.add('ac-fg-success'); out.classList.remove('ac-fg3','ac-fg-danger');
      } else {
        out.textContent = '✗ ' + (d.error || 'Failed');
        out.classList.add('ac-fg-danger'); out.classList.remove('ac-fg3','ac-fg-success');
      }
    }
  } catch (e) {
    if (out) {
      out.textContent = `Test failed: ${e.message}`;
      out.classList.add('ac-fg-danger'); out.classList.remove('ac-fg3','ac-fg-success');
    }
  }
}

async function _syncAutomation() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const out = _qs('ac-sched-test-result');
  if (out) { out.textContent = 'Pushing jobs…'; out.classList.add('ac-fg3'); out.classList.remove('ac-fg-success','ac-fg-danger'); }
  try {
    const r = await _fetch(apiPath('/admin/settings/scheduler/sync'), { method: 'POST' });
    const d = await r.json();
    if (out) {
      if (d.ok) {
        out.textContent = '✓ Jobs pushed';
        out.classList.add('ac-fg-success'); out.classList.remove('ac-fg3','ac-fg-danger');
      } else {
        out.textContent = '✗ ' + (d.error || 'Sync failed');
        out.classList.add('ac-fg-danger'); out.classList.remove('ac-fg3','ac-fg-success');
      }
    }
    setTimeout(_loadAutomationStatus, 400);
  } catch (e) {
    if (out) {
      out.textContent = `Sync failed: ${e.message}`;
      out.classList.add('ac-fg-danger'); out.classList.remove('ac-fg3','ac-fg-success');
    }
  }
}

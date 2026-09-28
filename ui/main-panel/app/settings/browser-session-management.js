'use strict';

import { apiPath } from '../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../shared/js/left-login.js?v=253';
import { _fetch, _qs } from './utils.js';
import { _markSaving, _flashSaveCheck } from '../../../shared/js/dom-utils.js';

let _initialized = false;

function _usageText(browser) {
  const live = Number(browser?.live_sessions || 0);
  const headless = Number(browser?.headless_sessions || 0);
  const attached = Number(browser?.attached_sessions || 0);
  const active = Number(browser?.active_sessions || 0);
  const max = Number(browser?.policy?.max_concurrent_sessions || 3);
  return `${headless} of ${max} headless sessions live · ${active} active/protected · ${attached} local browser connection${attached === 1 ? '' : 's'} · ${live} total server connection${live === 1 ? '' : 's'}`;
}

async function _refreshStatus(message = '') {
  const status = _qs('ac-browser-policy-status');
  try {
    const res = await _fetch(apiPath('/api/v1/control/status'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    if (status) status.textContent = (message ? message + ' ' : '') + _usageText(data.browser);
  } catch (error) {
    if (status) status.textContent = `Could not read browser usage: ${error.message}`;
  }
}

async function _save() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const button = _qs('ac-browser-policy-save');
  const maximum = Math.max(1, Math.min(20, parseInt(_qs('ac-browser-max-sessions')?.value || '3', 10) || 3));
  const timeout = Math.max(60, Math.min(86400, parseInt(_qs('ac-browser-idle-timeout')?.value || '300', 10) || 300));
  _markSaving(button);
  try {
    const res = await _fetch(apiPath('/admin/settings/app'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        browser_max_concurrent_sessions: maximum,
        browser_idle_timeout_seconds: timeout,
        browser_idle_cleanup_enabled: _qs('ac-browser-idle-enabled')?.checked !== false,
      }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(button, true);
    await _refreshStatus('Policy saved.');
  } catch (error) {
    _flashSaveCheck(button, false, error.message);
  }
}

async function _reapIdle() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const button = _qs('ac-browser-reap-idle');
  _markSaving(button);
  try {
    const res = await _fetch(apiPath('/api/v1/control/browser/reap-idle'), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _flashSaveCheck(button, true);
    await _refreshStatus(`Closed ${Number(data.closed || 0)} idle session${Number(data.closed || 0) === 1 ? '' : 's'}.`);
  } catch (error) {
    _flashSaveCheck(button, false, error.message);
  }
}

export function init() {
  if (_initialized) return;
  _initialized = true;
  _qs('ac-browser-policy-save')?.addEventListener('click', _save);
  _qs('ac-browser-reap-idle')?.addEventListener('click', _reapIdle);
  _qs('ac-browser-status-refresh')?.addEventListener('click', () => _refreshStatus());
}

export async function load() {
  try {
    const res = await _fetch(apiPath('/admin/settings/app'));
    if (res.ok) {
      const data = await res.json();
      const maximum = _qs('ac-browser-max-sessions');
      const timeout = _qs('ac-browser-idle-timeout');
      const enabled = _qs('ac-browser-idle-enabled');
      if (maximum) maximum.value = String(data.browser_max_concurrent_sessions ?? 3);
      if (timeout) timeout.value = String(data.browser_idle_timeout_seconds ?? 300);
      if (enabled) enabled.checked = data.browser_idle_cleanup_enabled !== false;
    }
  } catch (_) { /* usage status below carries the visible error */ }
  await _refreshStatus();
}

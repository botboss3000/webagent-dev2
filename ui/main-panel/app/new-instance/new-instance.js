'use strict';

// New Instance — independent bare-clone + scoped P2P bootstrap wizard.
// The legacy New Deployment tab is not imported or mutated by this module.

import { apiPath } from '../../../shared/js/config.js';
import { authHeaders } from '../../../shared/js/left-login.js?v=253';
import { _esc, _escAttr, _refreshLucideIcons } from '../../../shared/js/dom-utils.js';
import { openCredentialPopup } from '../../../credential-popup/credential-popup.js';

let _node = null;
let _building = null;
let _wired = false;
let _loading = null;
let _catalog = null;
let _accountProviders = [];
let _busy = false;
const _credentialReady = new Set();
const _selectedCredentials = new Map();

function _uid() {
  try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; }
}

function _qs(root, id) { return root.querySelector('#' + id); }

async function _json(path) {
  const join = path.includes('?') ? '&' : '?';
  const res = await fetch(apiPath(path + join + 'requesting_user_id=' + encodeURIComponent(_uid())), {
    headers: { ...authHeaders() },
  });
  let body = {};
  try { body = await res.json(); } catch {}
  if (!res.ok) throw new Error(body.detail || body.error || ('HTTP ' + res.status));
  return body;
}

async function _build() {
  const res = await fetch(new URL('./new-instance.html', import.meta.url));
  const html = await res.text();
  const wrap = document.createElement('div');
  wrap.className = 'ni';
  wrap.innerHTML = html;
  _node = wrap;
}

function _setLocked(step, locked, open) {
  if (!step) return;
  step.classList.toggle('ni-step--locked', locked);
  if (locked) step.classList.remove('ni-step--open');
  else if (open) step.classList.add('ni-step--open');
}

function _optionHtml(field, selected) {
  return (field.options || []).map((raw) => {
    const opt = typeof raw === 'object' ? raw : { value: raw, label: raw };
    const value = String(opt.value == null ? '' : opt.value);
    return '<option value="' + _escAttr(value) + '"' + (String(selected) === value ? ' selected' : '') + '>'
      + _esc(opt.label == null ? value : opt.label) + '</option>';
  }).join('');
}

function _fieldHtml(field, value) {
  const key = String(field.key || '');
  const id = 'ni-cfg-' + key.replace(/[^a-z0-9_-]/gi, '-');
  const required = field.required ? ' <span class="ni-required">*</span>' : '';
  const label = '<label class="ac-label" for="' + id + '">' + _esc(field.label || key) + required + '</label>';
  const attrs = ' id="' + id + '" class="ac-input" data-ni-cfg="' + _escAttr(key) + '"'
    + (field.required ? ' data-required="1"' : '');
  let control = '';
  if (field.type === 'checkbox') {
    control = '<label class="ni-inline-check"><input type="checkbox" data-ni-cfg="' + _escAttr(key) + '" data-type="checkbox"'
      + (value ? ' checked' : '') + '> <span>' + _esc(field.label || key) + '</span></label>';
    return '<div class="ni-provider-field">' + control + '</div>';
  }
  if (field.type === 'select') {
    control = '<select' + attrs + '>' + _optionHtml(field, value) + '</select>';
  } else {
    const type = field.type === 'number' ? 'number' : 'text';
    control = '<input' + attrs + ' type="' + type + '" value="' + _escAttr(value == null ? '' : value) + '"'
      + ' placeholder="' + _escAttr(field.placeholder || '') + '" autocomplete="off" spellcheck="false">';
  }
  return '<div class="ni-provider-field">' + label + control
    + (field.tip && typeof field.tip === 'string' ? '<div class="ni-field-note">' + _esc(field.tip) + '</div>' : '')
    + '</div>';
}

function _cloudProviders() {
  const manageable = new Set(_accountProviders.map((p) => p.id));
  return ((_catalog && _catalog.providers) || []).filter((p) => !p.manual && manageable.has(p.id));
}

function _currentProvider(root) {
  const id = (_qs(root, 'ni-cloud-provider') || {}).value || '';
  return _cloudProviders().find((p) => p.id === id) || null;
}

function _accountProvider(id) {
  return _accountProviders.find((p) => p.id === id) || null;
}

function _defaultInstanceName(projectId) {
  return projectId ? String(projectId).toLowerCase() : '';
}

function _credentialStatus(root, message, kind) {
  const status = _qs(root, 'ni-credential-status');
  if (!status) return;
  status.textContent = message || '';
  status.className = 'ni-status' + (kind ? ' ni-status--' + kind : '');
}

function _renderCredentialButton(root) {
  const btn = _qs(root, 'ni-use-saved-credential');
  const label = _qs(root, 'ni-credential-btn-label');
  if (!btn || !label) return;
  const provider = _currentProvider(root);
  const active = !!(provider && (_selectedCredentials.has(provider.id) || _credentialReady.has(provider.id)));
  label.textContent = active ? 'Reset credential' : 'Use a saved credential';
  const ico = btn.querySelector('.ni-btn-ico');
  if (ico) {
    const fresh = document.createElement('i');
    fresh.className = 'ni-btn-ico';
    fresh.setAttribute('data-lucide', active ? 'rotate-ccw' : 'key-round');
    ico.replaceWith(fresh);
  }
}

function _resetCredential(root, providerId) {
  _selectedCredentials.delete(providerId);
  _credentialReady.delete(providerId);
  _credentialStatus(root, 'Saved credential cleared — paste a service-account key or choose one again.', '');
  _renderProvider(root);
}

function _renderProvider(root) {
  const select = _qs(root, 'ni-cloud-provider');
  const providers = _cloudProviders();
  if (select && !select.options.length) {
    select.innerHTML = providers.map((p) => '<option value="' + _escAttr(p.id) + '"'
      + (p.available ? '' : ' disabled') + '>' + _esc(p.id === 'google_vm' ? 'Google Cloud VM' : p.display_name)
      + (p.available ? '' : ' — unavailable') + '</option>').join('')
      + '<option value="" disabled>More coming soon...</option>';
  }
  const provider = _currentProvider(root);
  const account = provider ? _accountProvider(provider.id) : null;
  const connected = !!(provider && account && account.connected && _credentialReady.has(provider.id));
  const jsonField = _qs(root, 'ni-json-credential-field');
  if (jsonField) jsonField.hidden = !!(provider
    && (_selectedCredentials.has(provider.id) || _credentialReady.has(provider.id)));
  const details = _qs(root, 'ni-cloud-details');
  if (details) details.hidden = !connected;
  const status = _qs(root, 'ni-cloud-account-status');
  if (status) {
    status.className = 'ni-status ni-status--ok';
    status.textContent = connected ? 'Connected' : '';
  }
  const project = _qs(root, 'ni-cloud-project');
  if (project) {
    const projectId = connected ? String(account.project || '') : '';
    const href = 'https://console.cloud.google.com/welcome/new?authuser=2&project=' + encodeURIComponent(projectId);
    project.innerHTML = projectId
      ? 'Google Cloud project ID:<a href="' + _escAttr(href) + '" target="_blank" rel="noopener noreferrer">' + _esc(projectId) + '</a>'
      : '';
  }
  const host = _qs(root, 'ni-cloud-config');
  if (host) {
    const skip = new Set(['project_id', 'repo_url', 'visibility', 'branch', 'domain', 'forget_keys']);
    const fields = connected && provider ? (provider.config_fields || []).filter((f) => !skip.has(f.key)) : [];
    const instanceName = connected ? _fieldHtml({
      key: 'instance_name', label: 'Instance name', type: 'text', required: true,
      placeholder: 'project-id',
      tip: 'Google Cloud VM name: lowercase letters, numbers and hyphens.',
    }, _defaultInstanceName(account.project)) : '';
    host.innerHTML = instanceName + fields.map((f) => {
      const saved = (provider.config || {})[f.key];
      const value = saved == null && Object.prototype.hasOwnProperty.call(f, 'default') ? f.default : saved;
      return _fieldHtml(f, value);
    }).join('');
  }
  _renderCredentialButton(root);
  _refreshLucideIcons(root);
}

async function _load(root, force) {
  if (_loading && !force) return _loading;
  _loading = Promise.all([
    _json('/admin/deploy/catalog'),
    _json('/admin/instances/providers'),
  ]).then(([catalog, accounts]) => {
    _catalog = catalog;
    _accountProviders = accounts.providers || [];
    const select = _qs(root, 'ni-cloud-provider');
    if (select && force) select.innerHTML = '';
    _renderProvider(root);
  }).catch((error) => {
    const status = _qs(root, 'ni-deploy-status');
    if (status) { status.textContent = error.message; status.className = 'ni-status ni-status--err'; }
  }).finally(() => { _loading = null; });
  return _loading;
}

function _useSavedCredential(root) {
  const saved = _accountProviders.filter((account) => account.connected);
  if (!saved.length) {
    _credentialStatus(root, 'No saved credential is available for this provider.', 'err');
    return;
  }
  const anchor = _qs(root, 'ni-use-saved-credential');
  openCredentialPopup({
    title: 'Choose a saved credential',
    hint: 'Select a cloud credential already stored in the encrypted vault.',
    anchor,
    providers: saved,
    mode: 'summary',
    summaryOnly: true,
    popoverAlign: 'after',
    onUseSaved: (providerId, popup) => {
      const account = _accountProvider(providerId);
      if (!account) return;
      const select = _qs(root, 'ni-cloud-provider');
      if (select) select.value = providerId;
      _selectedCredentials.set(providerId, account);
      const jsonField = _qs(root, 'ni-json-credential-field');
      if (jsonField) jsonField.hidden = true;
      _credentialStatus(root, 'Selected ' + (account.project || account.display_name || providerId)
        + ' from the vault. Click Save & connect.', 'ok');
      _renderProvider(root);
      popup.close();
    },
  });
}

async function _saveAndConnect(root) {
  const provider = _currentProvider(root);
  const input = _qs(root, 'ni-service-account-json');
  const button = _qs(root, 'ni-save-connect');
  const raw = String((input && input.value) || '').trim();
  if (!provider) return;
  const selected = _selectedCredentials.get(provider.id);
  if (selected) {
    if (button) button.disabled = true;
    _credentialStatus(root, 'Connecting with the selected vault credential…', '');
    _credentialReady.add(provider.id);
    _selectedCredentials.delete(provider.id);
    _renderProvider(root);
    _credentialStatus(root, 'Connected with the saved vault credential.', 'ok');
    if (button) button.disabled = false;
    return;
  }
  if (_credentialReady.has(provider.id)) {
    // Already connected through a saved credential — the JSON field is hidden
    // on purpose, so never demand a pasted key here.
    _credentialStatus(root, 'Already connected with the saved vault credential.', 'ok');
    return;
  }
  if (!raw) {
    _credentialStatus(root, 'Paste the service-account JSON first.', 'err');
    if (input) input.focus();
    return;
  }
  try {
    const parsed = JSON.parse(raw);
    if (!parsed || !parsed.project_id) throw new Error('The JSON does not contain a project_id.');
  } catch (error) {
    _credentialStatus(root, error.message || 'Enter valid service-account JSON.', 'err');
    if (input) input.focus();
    return;
  }
  if (button) button.disabled = true;
  _credentialStatus(root, 'Saving credential to the encrypted vault…', '');
  try {
    const res = await fetch(apiPath('/admin/instances/connect'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeaders() },
      body: JSON.stringify({
        requesting_user_id: _uid(),
        provider: provider.id,
        values: { service_account_json: raw },
      }),
    });
    let body = {};
    try { body = await res.json(); } catch {}
    if (!res.ok) throw new Error(body.detail || body.error || ('HTTP ' + res.status));
    _credentialReady.add(provider.id);
    if (input) input.value = '';
    await _load(root, true);
    _credentialStatus(root, 'Saved to the vault and connected.', 'ok');
  } catch (error) {
    _credentialStatus(root, error.message || 'Could not save the credential.', 'err');
  } finally {
    if (button) button.disabled = false;
  }
}

function _gatherConfig(root) {
  const out = {};
  root.querySelectorAll('[data-ni-cfg]').forEach((field) => {
    const key = field.dataset.niCfg;
    if (!key) return;
    out[key] = field.dataset.type === 'checkbox' ? !!field.checked : String(field.value || '').trim();
  });
  return out;
}

function _syncOptions(root) {
  return {
    app_db: !!_qs(root, 'ni-sync-app-db').checked,
    app_secrets: !!_qs(root, 'ni-sync-app-secrets').checked,
    app_configs: !!_qs(root, 'ni-sync-configs').checked,
  };
}

function _log(root, message, level) {
  const log = _qs(root, 'ni-deploy-log');
  if (!log) return;
  log.hidden = false;
  const mark = level === 'ok' ? '✓ ' : level === 'warn' ? '! ' : level === 'err' ? '× ' : '· ';
  log.textContent += mark + message + '\n';
  log.scrollTop = log.scrollHeight;
}

async function _deploy(root) {
  if (_busy) return;
  const provider = _currentProvider(root);
  const account = provider && _accountProvider(provider.id);
  const status = _qs(root, 'ni-deploy-status');
  if (!provider) { status.textContent = 'Choose a cloud provider.'; return; }
  if (!account || !account.connected) {
    status.textContent = 'Connect the cloud account first.';
    status.className = 'ni-status ni-status--err';
    return;
  }
  const missing = Array.from(root.querySelectorAll('[data-ni-cfg][data-required="1"]'))
    .find((field) => !String(field.value || '').trim());
  if (missing) { status.textContent = 'Complete the required cloud settings.'; status.className = 'ni-status ni-status--err'; missing.focus(); return; }
  if (!window.confirm('Deploy a new, billable ' + provider.display_name + ' instance?\n\nThe repo is installed bare, then the selected data is sent over a P2P handshake.')) return;

  _busy = true;
  const button = _qs(root, 'ni-deploy');
  const log = _qs(root, 'ni-deploy-log');
  if (button) button.disabled = true;
  if (log) { log.textContent = ''; log.hidden = false; }
  status.textContent = 'Deploying…';
  status.className = 'ni-status';
  const payload = {
    requesting_user_id: _uid(),
    provider: provider.id,
    config: _gatherConfig(root),
    sync_options: _syncOptions(root),
  };
  try {
    const res = await fetch(apiPath('/admin/instances/new-instance/deploy'), {
      method: 'POST', headers: { 'Content-Type': 'application/json', ...authHeaders() }, body: JSON.stringify(payload),
    });
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch {}
      throw new Error(detail || ('HTTP ' + res.status));
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let final = null;
    let lastLogMessage = '';
    const drain = (chunk) => {
      buffer += chunk;
      let newline;
      while ((newline = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, newline).trim();
        buffer = buffer.slice(newline + 1);
        if (!line) continue;
        let event;
        try { event = JSON.parse(line); } catch { continue; }
        if (event.phase === 'done') final = event.result || {};
        else if (event.message) {
          lastLogMessage = event.message;
          _log(root, event.message, event.level);
        }
      }
    };
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      drain(decoder.decode(value, { stream: true }));
    }
    drain(decoder.decode() + '\n');
    if (!final || !final.ok) throw new Error((final && final.message) || 'Deployment failed.');
    const p2pOk = !final.p2p || final.p2p.ok !== false;
    status.textContent = final.message || (p2pOk ? 'Deployed and synchronized.' : 'Deployed; P2P sync needs attention.');
    status.className = 'ni-status ' + (p2pOk ? 'ni-status--ok' : 'ni-status--warn');
    if (status.textContent !== lastLogMessage) {
      _log(root, status.textContent, p2pOk ? 'ok' : 'warn');
    }
  } catch (error) {
    status.textContent = error.message;
    status.className = 'ni-status ni-status--err';
    const existingLines = String(log && log.textContent || '').trimEnd().split('\n');
    const lastLine = existingLines[existingLines.length - 1] || '';
    if (!lastLine.endsWith(error.message)) _log(root, error.message, 'err');
  } finally {
    _busy = false;
    if (button) button.disabled = false;
  }
}

function _wire(root) {
  if (_wired) return;
  _wired = true;

  root.querySelectorAll('[data-ni-step]').forEach((step) => {
    const head = step.querySelector(':scope > .ni-step-head');
    if (!head) return;
    head.addEventListener('click', (event) => {
      if (event.target.closest('input, button, a, label, select, textarea')) return;
      if (!step.classList.contains('ni-step--locked')) step.classList.toggle('ni-step--open');
    });
  });

  const clone = _qs(root, 'ni-opt-clone');
  clone.addEventListener('change', () => {
    _setLocked(_qs(root, 'ni-step-2'), !clone.checked, clone.checked);
    _setLocked(_qs(root, 'ni-step-3'), !clone.checked, clone.checked);
    if (clone.checked) _load(root, false);
  });

  _qs(root, 'ni-target').addEventListener('change', (event) => {
    const cloud = event.target.value === 'cloud';
    _qs(root, 'ni-cloud-panel').hidden = !cloud;
    _qs(root, 'ni-manual-note').hidden = !event.target.value || cloud;
    if (cloud) _load(root, false);
  });
  _qs(root, 'ni-cloud-provider').addEventListener('change', () => _renderProvider(root));
  _qs(root, 'ni-use-saved-credential').addEventListener('click', () => {
    const provider = _currentProvider(root);
    if (provider && (_selectedCredentials.has(provider.id) || _credentialReady.has(provider.id))) {
      _resetCredential(root, provider.id);
    } else {
      _useSavedCredential(root);
    }
  });
  _qs(root, 'ni-save-connect').addEventListener('click', () => _saveAndConnect(root));
  _qs(root, 'ni-deploy').addEventListener('click', () => _deploy(root));
}

function _afterAttach() {
  _refreshLucideIcons(_node);
  _wire(_node);
}

export async function mountNewInstance(host) {
  if (!host) return;
  if (_node) { host.appendChild(_node); _afterAttach(); return; }
  if (!_building) _building = _build();
  await _building;
  if (_node && host.isConnected) { host.appendChild(_node); _afterAttach(); }
}

export function prewarmNewInstance() {
  if (!_node && !_building) _building = _build();
}

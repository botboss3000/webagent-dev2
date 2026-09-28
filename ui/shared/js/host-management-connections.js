'use strict';

import { authHeaders } from './left-login.js';

const BASE = '/api/v1/host-management';

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

async function api(method, path, body) {
  const options = { method, headers: { ...authHeaders() } };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  const response = await fetch(BASE + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  return data;
}

function setStatus(el, message, good = false) {
  el.textContent = message || '';
  el.style.color = good ? 'var(--success)' : 'var(--warning)';
}

function inputFor(def, value, { credential = false, isSet = false } = {}) {
  const wrap = node('label', 'conn-field');
  wrap.style.cssText = 'display:block;margin:0 0 9px;min-width:0;';
  const cap = node('span', 'conn-field-label', def.label || def.key);
  cap.style.cssText = 'display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;';
  let input;
  if (def.type === 'select') {
    input = node('select', 'agents-input');
    (def.options || []).forEach(raw => {
      const option = node('option', '', typeof raw === 'object' ? (raw.label || raw.value) : raw);
      option.value = typeof raw === 'object' ? raw.value : raw;
      input.appendChild(option);
    });
    input.value = value == null ? (def.default ?? '') : value;
  } else if (def.type === 'textarea') {
    input = node('textarea', 'agents-input');
    input.rows = 4;
    input.value = value == null ? '' : String(value);
  } else if (def.type === 'checkbox') {
    input = node('input'); input.type = 'checkbox';
    input.checked = value == null ? !!def.default : !!value;
  } else {
    input = node('input', 'agents-input');
    input.type = (credential || def.secret || def.type === 'password') ? 'password' : (def.type || 'text');
    input.value = credential ? '' : (value == null ? (def.default ?? '') : String(value));
    if (input.type === 'password') input.autocomplete = 'new-password';
  }
  input.dataset.fieldKey = def.key;
  input.dataset.fieldKind = credential ? 'credential' : 'config';
  if (def.placeholder) input.placeholder = credential && isSet ? 'Leave blank to keep saved value' : def.placeholder;
  if (def.required) input.required = true;
  if (input.type !== 'checkbox') {
    input.style.width = '100%'; input.style.boxSizing = 'border-box';
  }
  wrap.append(cap, input);
  const hintText = def.hint || (credential && isSet ? 'A saved value is already in the encrypted vault.' : '');
  if (hintText || (def.link && def.link.url)) {
    const hint = node('span', 'conn-field-hint');
    hint.style.cssText = 'display:block;font-size:10px;color:var(--fg-3);margin-top:3px;';
    if (hintText) hint.appendChild(document.createTextNode(hintText + ' '));
    if (def.link && def.link.url) {
      const link = node('a', '', def.link.label || 'Open provider');
      link.href = def.link.url; link.target = '_blank'; link.rel = 'noopener noreferrer';
      hint.appendChild(link);
    }
    wrap.appendChild(hint);
  }
  wrap.input = input;
  return wrap;
}

function readFields(form, kind) {
  const out = {};
  form.querySelectorAll(`[data-field-kind="${kind}"]`).forEach(input => {
    out[input.dataset.fieldKey] = input.type === 'checkbox' ? input.checked
      : (input.type === 'number' ? Number(input.value) : input.value);
  });
  return out;
}

function connectionForm(profile, providers, { agentId, userId, onDone }) {
  const form = node('div', 'host-connection-form');
  form.style.cssText = 'margin:8px 0 12px;padding:10px;border:1px solid var(--border);border-radius:7px;background:var(--bg-1);';
  form.addEventListener('click', event => event.stopPropagation());

  const top = node('div'); top.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 10px;';
  const nameWrap = inputFor({ key: 'name', label: 'Connection name', placeholder: 'Production Cloudflare' }, profile?.name || '');
  const providerWrap = node('label', 'conn-field'); providerWrap.style.cssText = 'display:block;margin:0 0 9px;';
  const cap = node('span', 'conn-field-label', 'Provider'); cap.style.cssText = 'display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;';
  const providerSelect = node('select', 'agents-input'); providerSelect.style.width = '100%';
  providers.forEach(provider => {
    const option = node('option', '', provider.display_name + (provider.available ? '' : ' (unavailable)'));
    option.value = provider.id; option.disabled = !provider.available;
    providerSelect.appendChild(option);
  });
  providerSelect.value = profile?.provider || providers.find(p => p.available)?.id || '';
  providerSelect.disabled = !!profile;
  providerWrap.append(cap, providerSelect); top.append(nameWrap, providerWrap); form.appendChild(top);

  const providerSummary = node('div'); providerSummary.style.cssText = 'font-size:10px;color:var(--fg-3);margin:-2px 0 10px;';
  form.appendChild(providerSummary);
  const dynamic = node('div'); form.appendChild(dynamic);

  function renderProvider() {
    dynamic.replaceChildren();
    const provider = providers.find(item => item.id === providerSelect.value);
    if (!provider) return;
    providerSummary.textContent = provider.summary || '';
    const grid = node('div'); grid.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 10px;';
    (provider.config_fields || []).forEach(def => grid.appendChild(inputFor(def, profile?.provider_config?.[def.key])));
    (provider.credential_fields || []).forEach(def => grid.appendChild(inputFor(def, '', {
      credential: true, isSet: !!profile?.credentials_set?.[def.key],
    })));
    if (!grid.childElementCount) {
      const message = node('div', '', provider.manual ? 'No account key is required for manual DNS.' : 'This provider has no connection fields.');
      message.style.cssText = 'font-size:11px;color:var(--fg-3);padding:6px 0 10px;'; dynamic.appendChild(message);
    } else dynamic.appendChild(grid);
  }
  providerSelect.addEventListener('change', renderProvider); renderProvider();

  const actions = node('div'); actions.style.cssText = 'display:flex;align-items:center;gap:7px;';
  const save = node('button', 'agents-btn', 'Save connection'); save.type = 'button';
  const cancel = node('button', 'agents-btn', 'Cancel'); cancel.type = 'button';
  const status = node('span'); status.style.fontSize = '11px';
  actions.append(save, cancel, status); form.appendChild(actions);
  cancel.addEventListener('click', onDone);
  save.addEventListener('click', async () => {
    save.disabled = true; setStatus(status, 'Saving…');
    try {
      await api('PUT', `/${encodeURIComponent(agentId)}`, {
        user_id: userId, id: profile?.id || null, name: nameWrap.input.value.trim(),
        provider: providerSelect.value, provider_config: readFields(form, 'config'),
        credentials: readFields(form, 'credential'),
      });
      setStatus(status, 'Saved', true); setTimeout(onDone, 350);
    } catch (error) { setStatus(status, error.message || 'Save failed'); }
    finally { save.disabled = false; }
  });
  return form;
}

export function buildHostConnectionsSection({ agent, userId, canEdit }) {
  const root = node('div', 'host-connections-panel');
  root.style.cssText = 'margin:4px 0 10px;'; root.addEventListener('click', event => event.stopPropagation());
  const titleRow = node('div'); titleRow.style.cssText = 'display:flex;align-items:center;gap:8px;margin-bottom:7px;';
  const title = node('strong', '', 'Hosting provider connections'); title.style.fontSize = '11px';
  const add = node('button', 'agents-btn', '+ Add connection'); add.type = 'button'; add.style.marginLeft = 'auto'; add.disabled = !canEdit;
  titleRow.append(title, add); root.appendChild(titleRow);
  const body = node('div'); root.appendChild(body);
  let providers = []; let connections = [];

  async function load() {
    body.innerHTML = '<div class="conn-loading" style="font-size:11px;padding:8px;">Loading hosting connections…</div>';
    try {
      const [providerData, connectionData] = await Promise.all([
        api('GET', '/providers'),
        api('GET', `/${encodeURIComponent(agent.id)}?user_id=${encodeURIComponent(userId)}`),
      ]);
      providers = providerData.providers || []; connections = connectionData.connections || []; render();
    } catch (error) {
      body.textContent = canEdit ? (error.message || 'Could not load hosting connections.') : 'Only agent admins can view hosting connections.';
      body.style.cssText = 'font-size:11px;color:var(--fg-3);padding:8px;';
    }
  }

  function render() {
    body.replaceChildren();
    if (!connections.length) {
      const empty = node('div', '', 'No hosting provider connected. Add Cloudflare or another installed provider.');
      empty.style.cssText = 'font-size:11px;color:var(--fg-3);padding:8px;border:1px dashed var(--border);border-radius:7px;';
      body.appendChild(empty); return;
    }
    connections.forEach(connection => {
      const row = node('div'); row.style.cssText = 'padding:8px 0;border-top:1px solid var(--border);';
      const head = node('div'); head.style.cssText = 'display:flex;align-items:center;gap:7px;';
      const summary = node('div'); summary.style.flex = '1';
      const name = node('div', '', connection.name || connection.provider_name); name.style.cssText = 'font-size:11px;font-weight:600;';
      const tested = connection.last_test?.ok ? ' · tested' : '';
      const meta = node('div', '', `${connection.provider_name || connection.provider}${connection.configured ? ' · connected' : ' · setup needed'}${tested}`);
      meta.style.cssText = 'font-size:10px;color:var(--fg-3);margin-top:2px;'; summary.append(name, meta);
      const status = node('span'); status.style.fontSize = '10px';
      const zones = node('button', 'agents-btn', 'Domains'); const test = node('button', 'agents-btn', 'Test');
      const edit = node('button', 'agents-btn', 'Edit'); const remove = node('button', 'agents-btn', 'Delete');
      [zones, test, edit, remove].forEach(button => { button.type = 'button'; button.style.fontSize = '10px'; button.disabled = !canEdit; });
      head.append(summary, status, zones, test, edit, remove); row.appendChild(head); body.appendChild(row);
      zones.addEventListener('click', async () => {
        zones.disabled = true; setStatus(status, 'Loading…');
        try {
          const data = await api('GET', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(connection.id)}/zones?user_id=${encodeURIComponent(userId)}`);
          const names = (data.zones || []).map(zone => zone.name).filter(Boolean);
          setStatus(status, data.ok === false ? (data.detail || 'Could not list domains') : `${names.length} domain(s)`, data.ok !== false);
          if (names.length) {
            const list = node('div', '', names.join(', ')); list.style.cssText = 'font-size:10px;color:var(--fg-3);margin-top:6px;word-break:break-word;';
            row.querySelector('.host-zone-list')?.remove(); list.className = 'host-zone-list'; row.appendChild(list);
          }
        } catch (error) { setStatus(status, error.message || 'Could not list domains'); }
        finally { zones.disabled = false; }
      });
      test.addEventListener('click', async () => {
        test.disabled = true; setStatus(status, 'Testing…');
        try {
          const data = await api('POST', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(connection.id)}/test?user_id=${encodeURIComponent(userId)}`);
          setStatus(status, data.detail || (data.ok ? 'Connected' : 'Test failed'), !!data.ok);
        } catch (error) { setStatus(status, error.message || 'Test failed'); }
        finally { test.disabled = false; }
      });
      edit.addEventListener('click', () => row.replaceWith(connectionForm(connection, providers, { agentId: agent.id, userId, onDone: load })));
      remove.addEventListener('click', async () => {
        if (!window.confirm(`Delete hosting connection “${connection.name}”?`)) return;
        try { await api('DELETE', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(connection.id)}?user_id=${encodeURIComponent(userId)}`); await load(); }
        catch (error) { setStatus(status, error.message || 'Delete failed'); }
      });
    });
  }

  add.addEventListener('click', () => {
    body.replaceChildren(connectionForm(null, providers, { agentId: agent.id, userId, onDone: load }));
  });
  load();
  return root;
}

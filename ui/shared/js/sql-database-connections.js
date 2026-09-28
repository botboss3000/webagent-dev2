'use strict';

import { authHeaders } from './left-login.js';
import { buildConfigRow, buildConfigSectionList } from './dom-utils.js';

const BASE = '/api/v1/knowledge/sql-databases';

function node(tag, cls, text) {
  const el = document.createElement(tag);
  if (cls) el.className = cls;
  if (text != null) el.textContent = text;
  return el;
}

function field(label, value = '', type = 'text') {
  const wrap = node('label', 'conn-field');
  wrap.style.cssText = 'display:block;margin:0 0 9px;min-width:0;';
  const cap = node('span', 'conn-field-label', label);
  cap.style.cssText = 'display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;';
  const input = node('input', 'agents-input');
  input.type = type;
  input.value = value == null ? '' : String(value);
  input.style.width = '100%';
  input.style.boxSizing = 'border-box';
  wrap.append(cap, input);
  wrap.input = input;
  return wrap;
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

function tableNames(profile) {
  return (((profile.schema_cache || {}).tables) || []).map(t => t.name).filter(Boolean);
}

function columnsFor(profile, tableName) {
  const table = (((profile.schema_cache || {}).tables) || []).find(t => t.name === tableName);
  return (table && table.columns) || [];
}

function setStatus(el, message, good = false) {
  el.textContent = message;
  el.style.color = good ? 'var(--success)' : 'var(--warning)';
}

function connectionForm(profile, { agentId, userId, onDone }) {
  const p = profile || {
    provider: 'postgres', port: 5432, ssl_mode: 'require', schemas: ['public'],
    row_limit: 100, timeout_seconds: 10, allowed_tables: [],
    recall_content_columns: [], auto_recall: false,
  };
  const form = node('div', 'sql-connection-form');
  form.style.cssText = 'margin:8px 0 12px;padding:10px;border:1px solid var(--border);border-radius:7px;background:var(--bg-1);';
  form.addEventListener('click', e => e.stopPropagation());

  const grid = node('div');
  grid.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 10px;';
  const name = field('Connection name', p.name || '');
  const provider = field('Provider', 'PostgreSQL'); provider.input.disabled = true;
  const host = field('Host', p.host || '');
  const port = field('Port', p.port || 5432, 'number');
  const database = field('Database', p.database || '');
  const username = field('Read-only username', p.username || '');
  const password = field(p.password_set ? 'Password (leave blank to keep saved)' : 'Password', '', 'password');
  password.input.autocomplete = 'new-password';
  const ssl = node('label', 'conn-field');
  ssl.style.cssText = 'display:block;margin:0 0 9px;';
  ssl.innerHTML = '<span class="conn-field-label" style="display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;">SSL mode</span>';
  const sslSelect = node('select', 'agents-input');
  ['disable', 'prefer', 'require', 'verify-ca', 'verify-full'].forEach(v => {
    const option = node('option', '', v); option.value = v; option.selected = v === (p.ssl_mode || 'require'); sslSelect.appendChild(option);
  });
  ssl.appendChild(sslSelect);
  [name, provider, host, port, database, username, password, ssl].forEach(v => grid.appendChild(v));
  form.appendChild(grid);

  const schemas = field('Schemas to inspect (comma separated)', (p.schemas || ['public']).join(', '));
  const rowLimit = field('Maximum rows', p.row_limit || 100, 'number');
  rowLimit.input.min = '1'; rowLimit.input.max = '1000';
  const timeout = field('Timeout seconds', p.timeout_seconds || 10, 'number');
  timeout.input.min = '1'; timeout.input.max = '60';
  const policyGrid = node('div');
  policyGrid.style.cssText = 'display:grid;grid-template-columns:2fr 1fr 1fr;gap:0 10px;';
  policyGrid.append(schemas, rowLimit, timeout);
  form.appendChild(policyGrid);

  const tableBlock = node('div');
  tableBlock.style.cssText = 'margin:5px 0 10px;';
  tableBlock.appendChild(node('div', 'conn-field-label', 'Approved tables/views'));
  const names = tableNames(p);
  if (!names.length) {
    const hint = node('div', '', 'Save, then inspect the schema to choose which tables the agent may read.');
    hint.style.cssText = 'font-size:11px;color:var(--fg-3);margin-top:4px;';
    tableBlock.appendChild(hint);
  } else {
    const checks = node('div');
    checks.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px 10px;margin-top:5px;max-height:180px;overflow:auto;';
    names.forEach(table => {
      const label = node('label'); label.style.cssText = 'display:flex;gap:6px;align-items:center;font-size:11px;';
      const check = node('input'); check.type = 'checkbox'; check.value = table;
      check.checked = (p.allowed_tables || []).includes(table);
      check.dataset.allowedTable = '1';
      label.append(check, document.createTextNode(table)); checks.appendChild(label);
    });
    tableBlock.appendChild(checks);
  }
  form.appendChild(tableBlock);

  const recallToggle = node('label');
  recallToggle.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;margin:8px 0;';
  const recallCheck = node('input'); recallCheck.type = 'checkbox'; recallCheck.checked = !!p.auto_recall;
  recallToggle.append(recallCheck, document.createTextNode('Automatically recall relevant rows before a turn'));
  form.appendChild(recallToggle);

  const recallGrid = node('div');
  recallGrid.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 10px;';
  const recallTableWrap = node('label', 'conn-field');
  recallTableWrap.innerHTML = '<span class="conn-field-label" style="display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;">Recall table</span>';
  const recallTable = node('select', 'agents-input');
  const noRecallTable = node('option', '', 'Choose an approved table');
  noRecallTable.value = '';
  recallTable.appendChild(noRecallTable);
  recallTableWrap.appendChild(recallTable);
  const identity = field('Identity column (optional)', p.recall_identity_column || '');
  const title = field('Title column', p.recall_title_column || '');
  const content = field('Content columns (comma separated)', (p.recall_content_columns || []).join(', '));
  recallGrid.append(recallTableWrap, identity, title, content);
  form.appendChild(recallGrid);

  const columnHint = node('div');
  columnHint.style.cssText = 'font-size:10px;color:var(--fg-3);margin:-5px 0 9px;';
  const refreshColumnHint = () => {
    const cols = columnsFor(p, recallTable.value).map(c => c.name);
    columnHint.textContent = cols.length ? `Available columns: ${cols.join(', ')}` : '';
  };
  const refreshRecallTables = () => {
    const selected = recallTable.value || p.recall_table || '';
    const approved = [...form.querySelectorAll('[data-allowed-table]')]
      .filter(check => check.checked).map(check => check.value);
    recallTable.replaceChildren(noRecallTable);
    approved.forEach(table => {
      const option = node('option', '', table); option.value = table;
      recallTable.appendChild(option);
    });
    recallTable.value = approved.includes(selected) ? selected : '';
    refreshColumnHint();
  };
  form.querySelectorAll('[data-allowed-table]').forEach(check => {
    check.addEventListener('change', refreshRecallTables);
  });
  recallTable.addEventListener('change', refreshColumnHint);
  refreshRecallTables();
  form.appendChild(columnHint);

  const status = node('span'); status.style.cssText = 'font-size:11px;margin-left:8px;';
  const actions = node('div'); actions.style.cssText = 'display:flex;gap:7px;align-items:center;';
  const save = node('button', 'agents-btn', 'Save connection'); save.type = 'button';
  const cancel = node('button', 'agents-btn', 'Cancel'); cancel.type = 'button';
  actions.append(save, cancel, status); form.appendChild(actions);
  cancel.addEventListener('click', () => onDone());
  save.addEventListener('click', async () => {
    save.disabled = true; setStatus(status, 'Saving…');
    const allowed = [...form.querySelectorAll('[data-allowed-table]')].filter(c => c.checked).map(c => c.value);
    try {
      await api('PUT', `/${encodeURIComponent(agentId)}`, {
        user_id: userId, id: p.id || null, name: name.input.value.trim(), provider: 'postgres',
        host: host.input.value.trim(), port: Number(port.input.value || 5432),
        database: database.input.value.trim(), username: username.input.value.trim(), password: password.input.value,
        ssl_mode: sslSelect.value, schemas: schemas.input.value.split(',').map(v => v.trim()).filter(Boolean),
        allowed_tables: allowed, row_limit: Number(rowLimit.input.value || 100),
        timeout_seconds: Number(timeout.input.value || 10), auto_recall: recallCheck.checked,
        recall_table: recallTable.value, recall_identity_column: identity.input.value.trim(),
        recall_title_column: title.input.value.trim(),
        recall_content_columns: content.input.value.split(',').map(v => v.trim()).filter(Boolean),
      });
      setStatus(status, 'Saved', true); setTimeout(() => onDone(), 350);
    } catch (error) { setStatus(status, error.message || 'Save failed'); }
    finally { save.disabled = false; }
  });
  return form;
}

export function buildSqlConnectionsSection({ agent, userId, canEdit }) {
  const root = node('div', 'sql-connections-panel');
  root.style.cssText = 'margin:4px 0 10px;';
  root.addEventListener('click', e => e.stopPropagation());
  // Standard nested rows: the section heading, its saved connections and its
  // empty / error / loading states are the SAME `.ac-ability-row` as the ability's
  // own settings + tool rows (shared `.ac-list` dividers with the frame stripped),
  // so this section reads as one more block of that list instead of a bespoke
  // mini-table with its own fonts and alignment.
  // (buildConfigRow / buildConfigSectionList — SISTER-PANEL: AGENT-ABILITY-TABLE.)
  const list = buildConfigSectionList();
  const add = node('button', 'agents-btn', '+ Add connection');
  add.type = 'button';
  add.disabled = !canEdit;
  const headRow = buildConfigRow({ name: 'PostgreSQL connections', control: add });
  list.appendChild(headRow);
  root.appendChild(list);
  let profiles = [];
  // Everything below the heading row is re-rendered (rows, forms, states).
  const clearRows = () => {
    while (list.lastChild && list.lastChild !== headRow) list.removeChild(list.lastChild);
  };

  async function load() {
    clearRows();
    const loading = node('div', 'conn-loading');
    loading.style.cssText = 'padding:8px;font-size:11px;';
    loading.textContent = 'Loading SQL connections…';
    list.appendChild(loading);
    try {
      profiles = (await api('GET', `/${encodeURIComponent(agent.id)}?user_id=${encodeURIComponent(userId)}`)).profiles || [];
      render();
    } catch (error) {
      clearRows();
      list.appendChild(buildConfigRow({
        desc: canEdit ? (error.message || 'Could not load SQL connections.') : 'Only agent admins can view database connections.',
        className: 'ac-config-empty',
      }));
    }
  }

  function render() {
    clearRows();
    if (!profiles.length) {
      list.appendChild(buildConfigRow({
        desc: 'No business database connected. Add a read-only PostgreSQL connection.',
        className: 'ac-config-empty',
      }));
      return;
    }
    profiles.forEach(profile => {
      const meta = `${profile.host}:${profile.port} / ${profile.database} · ${(profile.allowed_tables || []).length} approved table(s)${profile.auto_recall ? ' · recall on' : ''}`;
      const status = node('span'); status.style.cssText = 'font-size:10px;';
      const test = node('button', 'agents-btn', 'Test'); const inspect = node('button', 'agents-btn', 'Inspect schema');
      const edit = node('button', 'agents-btn', 'Edit'); const remove = node('button', 'agents-btn', 'Delete');
      [test, inspect, edit, remove].forEach(b => {
        b.type = 'button'; b.style.fontSize = '10px'; b.disabled = !canEdit;
      });
      const ctrl = node('span');
      ctrl.style.cssText = 'display:flex;align-items:center;gap:6px;';
      ctrl.append(status, test, inspect, edit, remove);
      const row = buildConfigRow({ name: profile.name || 'PostgreSQL', desc: meta, control: ctrl });
      list.appendChild(row);
      test.addEventListener('click', async () => {
        test.disabled = true; setStatus(status, 'Testing…');
        try { const d = await api('POST', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(profile.id)}/test?user_id=${encodeURIComponent(userId)}`); setStatus(status, d.ok ? 'Connected' : d.message, !!d.ok); }
        catch (e) { setStatus(status, e.message || 'Test failed'); } finally { test.disabled = false; }
      });
      inspect.addEventListener('click', async () => {
        inspect.disabled = true; setStatus(status, 'Inspecting…');
        try { await api('POST', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(profile.id)}/introspect?user_id=${encodeURIComponent(userId)}`); setStatus(status, 'Schema ready', true); await load(); }
        catch (e) { setStatus(status, e.message || 'Inspection failed'); } finally { inspect.disabled = false; }
      });
      edit.addEventListener('click', () => { row.replaceWith(connectionForm(profile, { agentId: agent.id, userId, onDone: load })); });
      remove.addEventListener('click', async () => {
        if (!window.confirm(`Delete SQL connection “${profile.name}”?`)) return;
        try { await api('DELETE', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(profile.id)}?user_id=${encodeURIComponent(userId)}`); await load(); }
        catch (e) { setStatus(status, e.message || 'Delete failed'); }
      });
    });
  }

  add.addEventListener('click', () => { clearRows(); list.appendChild(connectionForm(null, { agentId: agent.id, userId, onDone: load })); });
  load();
  return root;
}

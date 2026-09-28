'use strict';

import { authHeaders } from './left-login.js';
import { buildConfigRow, buildConfigSectionList } from './dom-utils.js';

function el(tag, cls = '', text = null) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function field(label, value = '', type = 'text') {
  const wrap = el('label', 'conn-field');
  wrap.style.cssText = 'display:block;margin:0 0 9px;min-width:0;';
  const caption = el('span', 'conn-field-label', label);
  caption.style.cssText = 'display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;';
  const input = el('input', 'agents-input');
  input.type = type; input.value = value == null ? '' : String(value);
  input.style.cssText = 'width:100%;box-sizing:border-box;';
  wrap.append(caption, input); wrap.input = input;
  return wrap;
}

function selectField(label, options, value) {
  const wrap = el('label', 'conn-field');
  wrap.style.cssText = 'display:block;margin:0 0 9px;min-width:0;';
  const caption = el('span', 'conn-field-label', label);
  caption.style.cssText = 'display:block;font-size:10px;color:var(--fg-3);margin-bottom:3px;';
  const select = el('select', 'agents-input');
  options.forEach(([key, name]) => {
    const option = el('option', '', name); option.value = key; option.selected = key === value;
    select.appendChild(option);
  });
  wrap.append(caption, select); wrap.input = select;
  return wrap;
}

function toggle(label, checked) {
  const wrap = el('label'); wrap.style.cssText = 'display:flex;align-items:center;gap:7px;font-size:11px;margin:5px 0 10px;';
  const input = el('input'); input.type = 'checkbox'; input.checked = !!checked;
  wrap.append(input, document.createTextNode(label)); wrap.input = input;
  return wrap;
}

function status(node, message, good = false) {
  node.textContent = message;
  node.style.color = good ? 'var(--success)' : 'var(--warning)';
}

async function api(base, method, path, body) {
  const options = { method, headers: { ...authHeaders() } };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json'; options.body = JSON.stringify(body);
  }
  const response = await fetch(base + path, options);
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.detail || data.message || `HTTP ${response.status}`);
  return data;
}

function actionRow(onCancel, onSave) {
  const row = el('div'); row.style.cssText = 'display:flex;gap:7px;align-items:center;';
  const save = el('button', 'agents-btn', 'Save connection'); save.type = 'button';
  const cancel = el('button', 'agents-btn', 'Cancel'); cancel.type = 'button';
  const message = el('span'); message.style.cssText = 'font-size:11px;margin-left:8px;';
  cancel.addEventListener('click', onCancel);
  save.addEventListener('click', async () => {
    save.disabled = true; status(message, 'Saving…');
    try { await onSave(); status(message, 'Saved', true); }
    catch (error) { status(message, error.message || 'Save failed'); }
    finally { save.disabled = false; }
  });
  row.append(save, cancel, message);
  return row;
}

function documentForm(profile, context) {
  const p = profile || { provider: 'google_drive', auto_recall: false, allowed_extensions: [] };
  const form = el('div');
  form.style.cssText = 'margin:8px 0 12px;padding:10px;border:1px solid var(--border);border-radius:7px;background:var(--bg-1);';
  form.addEventListener('click', event => event.stopPropagation());
  const grid = el('div'); grid.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 10px;';
  const name = field('Connection name', p.name || '');
  const provider = selectField('Provider', [
    ['google_drive', 'Google Drive'], ['onedrive', 'OneDrive'],
    ['sharepoint', 'SharePoint'], ['dropbox', 'Dropbox'],
  ], p.provider || 'google_drive');
  const root = field('Approved folder ID or path (blank = provider root)', p.root || '');
  const extensions = field('Allowed extensions (optional, comma separated)', (p.allowed_extensions || []).join(', '));
  const site = field('SharePoint site ID', p.site_id || '');
  const drive = field('SharePoint document-library drive ID', p.drive_id || '');
  [name, provider, root, extensions, site, drive].forEach(item => grid.appendChild(item));
  form.appendChild(grid);
  const refreshProviderFields = () => {
    const sharepoint = provider.input.value === 'sharepoint';
    site.style.display = sharepoint ? 'block' : 'none'; drive.style.display = sharepoint ? 'block' : 'none';
    root.querySelector('.conn-field-label').textContent = provider.input.value === 'google_drive'
      ? 'Approved Google folder ID (blank = whole Drive)'
      : 'Approved folder path (blank = provider root)';
  };
  provider.input.addEventListener('change', refreshProviderFields); refreshProviderFields();
  const recall = toggle('Automatically recall relevant documents before a turn', p.auto_recall);
  form.appendChild(recall);
  form.appendChild(actionRow(context.onDone, async () => {
    await api(context.base, 'PUT', `/${encodeURIComponent(context.agentId)}`, {
      user_id: context.userId, id: p.id || null, name: name.input.value.trim(), provider: provider.input.value,
      root: root.input.value.trim(), site_id: site.input.value.trim(), drive_id: drive.input.value.trim(),
      allowed_extensions: extensions.input.value.split(',').map(value => value.trim()).filter(Boolean),
      auto_recall: recall.input.checked,
    });
    setTimeout(context.onDone, 300);
  }));
  return form;
}

function workspaceForm(profile, context) {
  const p = profile || { provider: 'notion', auto_recall: false, approved_roots: [], space_keys: [] };
  const form = el('div');
  form.style.cssText = 'margin:8px 0 12px;padding:10px;border:1px solid var(--border);border-radius:7px;background:var(--bg-1);';
  form.addEventListener('click', event => event.stopPropagation());
  const grid = el('div'); grid.style.cssText = 'display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 10px;';
  const name = field('Connection name', p.name || '');
  const provider = selectField('Provider', [['notion', 'Notion'], ['confluence', 'Confluence']], p.provider || 'notion');
  const baseUrl = field('Confluence tenant base URL', p.base_url === 'https://api.notion.com' ? '' : (p.base_url || ''));
  const username = field('Confluence account email', p.username || '');
  const token = field(p.secret_set ? 'API token (leave blank to keep saved)' : 'Integration/API token', '', 'password');
  token.input.autocomplete = 'new-password';
  const roots = field('Approved Notion root page/database IDs (comma separated)', (p.approved_roots || []).join(', '));
  const spaces = field('Approved Confluence space keys (comma separated)', (p.space_keys || []).join(', '));
  [name, provider, baseUrl, username, token, roots, spaces].forEach(item => grid.appendChild(item));
  form.appendChild(grid);
  const refreshProviderFields = () => {
    const notion = provider.input.value === 'notion';
    baseUrl.style.display = notion ? 'none' : 'block'; username.style.display = notion ? 'none' : 'block';
    roots.style.display = notion ? 'block' : 'none'; spaces.style.display = notion ? 'none' : 'block';
    token.querySelector('.conn-field-label').textContent = p.secret_set && provider.input.value === p.provider
      ? 'API token (leave blank to keep saved)'
      : 'Integration/API token';
  };
  provider.input.addEventListener('change', refreshProviderFields); refreshProviderFields();
  const recall = toggle('Automatically recall relevant workspace pages before a turn', p.auto_recall);
  form.appendChild(recall);
  form.appendChild(actionRow(context.onDone, async () => {
    await api(context.base, 'PUT', `/${encodeURIComponent(context.agentId)}`, {
      user_id: context.userId, id: p.id || null, name: name.input.value.trim(), provider: provider.input.value,
      base_url: baseUrl.input.value.trim(), username: username.input.value.trim(), token: token.input.value,
      approved_roots: roots.input.value.split(',').map(value => value.trim()).filter(Boolean),
      space_keys: spaces.input.value.split(',').map(value => value.trim()).filter(Boolean),
      auto_recall: recall.input.checked,
    });
    setTimeout(context.onDone, 300);
  }));
  return form;
}

function buildPanel({ agent, userId, canEdit, base, title, note, emptyText, formBuilder, metaBuilder }) {
  const root = el('div', 'knowledge-connections-panel'); root.style.cssText = 'margin:4px 0 10px;';
  root.addEventListener('click', event => event.stopPropagation());
  // Standard nested rows: the section heading (+ its guidance line), the saved
  // connections and the empty / loading / error states are the SAME
  // `.ac-ability-row` as the ability's own settings + tool rows (shared `.ac-list`
  // dividers, frame stripped), so this section reads as one more block of that
  // list instead of a bespoke mini-table with its own fonts and alignment.
  // (buildConfigRow / buildConfigSectionList — SISTER-PANEL: AGENT-ABILITY-TABLE.)
  const list = buildConfigSectionList();
  const add = el('button', 'agents-btn', '+ Add connection'); add.type = 'button'; add.disabled = !canEdit;
  const headRow = buildConfigRow({ name: title, desc: note || '', control: add, className: 'ac-config-head' });
  list.appendChild(headRow);
  root.appendChild(list);
  let profiles = [];
  // Everything below the heading row is re-rendered (rows, forms, states).
  const clearRows = () => {
    while (list.lastChild && list.lastChild !== headRow) list.removeChild(list.lastChild);
  };

  async function load() {
    clearRows();
    const loading = el('div', 'conn-loading');
    loading.style.cssText = 'padding:8px;font-size:11px;';
    loading.textContent = 'Loading connections…';
    list.appendChild(loading);
    try {
      profiles = (await api(base, 'GET', `/${encodeURIComponent(agent.id)}?user_id=${encodeURIComponent(userId)}`)).profiles || [];
      render();
    } catch (error) {
      clearRows();
      list.appendChild(buildConfigRow({
        desc: canEdit ? (error.message || 'Could not load connections.') : 'Only agent admins can view knowledge connections.',
        className: 'ac-config-empty',
      }));
    }
  }

  function render() {
    clearRows();
    if (!profiles.length) {
      list.appendChild(buildConfigRow({ desc: emptyText, className: 'ac-config-empty' }));
      return;
    }
    profiles.forEach(profile => {
      const message = el('span'); message.style.cssText = 'font-size:10px;';
      const test = el('button', 'agents-btn', 'Test'); const edit = el('button', 'agents-btn', 'Edit');
      const remove = el('button', 'agents-btn', 'Delete');
      [test, edit, remove].forEach(button => { button.type = 'button'; button.style.fontSize = '10px'; button.disabled = !canEdit; });
      const ctrl = el('span'); ctrl.style.cssText = 'display:flex;align-items:center;gap:6px;';
      ctrl.append(message, test, edit, remove);
      const row = buildConfigRow({
        name: profile.name || profile.provider,
        desc: metaBuilder(profile),
        control: ctrl,
      });
      list.appendChild(row);
      test.addEventListener('click', async () => {
        test.disabled = true; status(message, 'Testing…');
        try {
          const result = await api(base, 'POST', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(profile.id)}/test?user_id=${encodeURIComponent(userId)}`);
          status(message, result.ok ? result.message : result.message, !!result.ok);
        } catch (error) { status(message, error.message || 'Test failed'); }
        finally { test.disabled = false; }
      });
      edit.addEventListener('click', () => row.replaceWith(formBuilder(profile, { base, agentId: agent.id, userId, onDone: load })));
      remove.addEventListener('click', async () => {
        if (!window.confirm(`Delete knowledge connection “${profile.name}”?`)) return;
        try { await api(base, 'DELETE', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(profile.id)}?user_id=${encodeURIComponent(userId)}`); await load(); }
        catch (error) { status(message, error.message || 'Delete failed'); }
      });
    });
  }

  add.addEventListener('click', () => {
    clearRows(); list.appendChild(formBuilder(null, { base, agentId: agent.id, userId, onDone: load }));
  });
  load();
  return root;
}

export function buildDocumentLibrariesSection(context) {
  return buildPanel({
    ...context,
    base: '/api/v1/knowledge/document-libraries', title: 'Document-library connections',
    note: 'Uses the user’s existing Google, Microsoft, or Dropbox OAuth connection from Productivity.',
    emptyText: 'No document library connected. Add Google Drive, OneDrive, SharePoint, or Dropbox.',
    formBuilder: documentForm,
    metaBuilder: profile => `${profile.provider.replaceAll('_', ' ')} · ${profile.root || 'provider root'}${profile.auto_recall ? ' · recall on' : ''}`,
  });
}

export function buildWorkspacesWikisSection(context) {
  return buildPanel({
    ...context,
    base: '/api/v1/knowledge/workspaces-wikis', title: 'Workspace and wiki connections',
    note: 'Notion uses an internal integration token; Confluence uses an account email plus API token.',
    emptyText: 'No third-party workspace connected. Add Notion or Confluence.',
    formBuilder: workspaceForm,
    metaBuilder: profile => {
      const scopes = profile.provider === 'notion' ? (profile.approved_roots || []) : (profile.space_keys || []);
      return `${profile.provider} · ${scopes.length ? `${scopes.length} approved scope(s)` : 'whole connected workspace'}${profile.auto_recall ? ' · recall on' : ''}`;
    },
  });
}

function byteSize(value) {
  const bytes = Math.max(0, Number(value) || 0);
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1048576) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1048576).toFixed(1)} MB`;
}

export function buildManagedKnowledgeFilesSection({ agent, userId, canEdit }) {
  const base = '/api/v1/knowledge/files';
  const root = el('div'); root.style.cssText = 'margin:4px 0 10px;';
  root.addEventListener('click', event => event.stopPropagation());

  // Standard nested rows (see buildPanel above): the heading row carries the
  // section name, its guidance line and the upload controls, and every managed
  // file is one more `.ac-ability-row` of the SAME list, so the section reads as
  // part of the ability's config list rather than a bespoke mini-table.
  // (buildConfigRow / buildConfigSectionList — SISTER-PANEL: AGENT-ABILITY-TABLE.)
  const list = buildConfigSectionList();
  const usage = el('span'); usage.style.cssText = 'font-size:10px;color:var(--fg-3);';
  const upload = el('button', 'agents-btn', '+ Upload files'); upload.type = 'button'; upload.disabled = !canEdit;
  const picker = el('input'); picker.type = 'file'; picker.multiple = true; picker.hidden = true;
  picker.accept = '.pdf,.docx,.pptx,.xlsx,.txt,.md,.csv,.tsv,.json,.xml,.yaml,.yml,.html,.htm,.log';
  const ctrl = el('span'); ctrl.style.cssText = 'display:flex;align-items:center;gap:6px;';
  ctrl.append(usage, upload, picker);
  const headRow = buildConfigRow({
    name: 'Agent knowledge files',
    desc: 'Stored in this agent’s knowledge/files directory—not in a user’s chat attachments.',
    control: ctrl,
    className: 'ac-config-head',
  });
  list.appendChild(headRow);
  root.appendChild(list);
  // Transient "Uploading …" / "N files uploaded." status line, below the rows.
  const message = el('div'); message.style.cssText = 'font-size:10px;min-height:14px;margin-top:4px;'; root.appendChild(message);
  const body = list;  // file rows share the heading's list
  const clearRows = () => {
    while (body.lastChild && body.lastChild !== headRow) body.removeChild(body.lastChild);
  };

  async function load() {
    clearRows();
    const loading = el('div', 'conn-loading');
    loading.style.cssText = 'padding:8px;font-size:11px;';
    loading.textContent = 'Loading files…';
    body.appendChild(loading);
    try {
      const data = await api(base, 'GET', `/${encodeURIComponent(agent.id)}?user_id=${encodeURIComponent(userId)}`);
      render(data.files || []);
      const storage = data.storage || {};
      usage.textContent = storage.maximum_bytes
        ? `${byteSize(storage.used_bytes)} of ${byteSize(storage.maximum_bytes)}` : '';
    } catch (error) {
      clearRows();
      body.appendChild(buildConfigRow({
        desc: canEdit ? (error.message || 'Could not load files.') : 'Only agent admins can view managed knowledge files.',
        className: 'ac-config-empty',
      }));
    }
  }

  function render(files) {
    clearRows();
    if (!files.length) {
      body.appendChild(buildConfigRow({
        desc: 'No managed files yet. Upload documents, presentations, spreadsheets, PDFs, or text files.',
        className: 'ac-config-empty',
      }));
      return;
    }
    files.forEach(file => {
      const state = file.status === 'ready' ? `${file.text_chars || 0} searchable characters` : 'No extractable text';
      const meta = `${String(file.extension || '').toUpperCase()} · ${byteSize(file.size_bytes)} · ${state}`;
      const recall = toggle('Auto recall', file.auto_recall); recall.style.margin = '0 4px 0 0'; recall.input.disabled = !canEdit;
      const remove = el('button', 'agents-btn', 'Delete'); remove.type = 'button'; remove.style.fontSize = '10px'; remove.disabled = !canEdit;
      const ctrl = el('span'); ctrl.style.cssText = 'display:flex;align-items:center;gap:6px;';
      ctrl.append(recall, remove);
      const row = buildConfigRow({ name: file.name || 'Untitled', desc: meta, control: ctrl });
      body.appendChild(row);
      recall.input.addEventListener('change', async () => {
        recall.input.disabled = true;
        try {
          await api(base, 'PATCH', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(file.id)}`, {
            user_id: userId, auto_recall: recall.input.checked,
          });
          status(message, 'Recall preference saved.', true);
        } catch (error) {
          recall.input.checked = !recall.input.checked; status(message, error.message || 'Save failed');
        } finally { recall.input.disabled = !canEdit; }
      });
      remove.addEventListener('click', async () => {
        if (!window.confirm(`Delete managed knowledge file “${file.name}”?`)) return;
        remove.disabled = true;
        try {
          await api(base, 'DELETE', `/${encodeURIComponent(agent.id)}/${encodeURIComponent(file.id)}?user_id=${encodeURIComponent(userId)}`);
          status(message, 'File deleted.', true); await load();
        } catch (error) { status(message, error.message || 'Delete failed'); remove.disabled = !canEdit; }
      });
    });
  }

  upload.addEventListener('click', () => picker.click());
  picker.addEventListener('change', async () => {
    const files = Array.from(picker.files || []); picker.value = '';
    if (!files.length) return;
    upload.disabled = true;
    let completed = 0;
    try {
      for (const file of files) {
        status(message, `Uploading ${file.name}…`);
        const form = new FormData(); form.append('user_id', userId); form.append('auto_recall', 'true'); form.append('file', file);
        const response = await fetch(`${base}/${encodeURIComponent(agent.id)}`, {
          method: 'POST', headers: { ...authHeaders() }, body: form,
        });
        const data = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(data.detail || `Upload failed (HTTP ${response.status})`);
        completed += 1;
      }
      status(message, `${completed} file${completed === 1 ? '' : 's'} uploaded.`, true); await load();
    } catch (error) {
      status(message, `${completed ? `${completed} uploaded. ` : ''}${error.message || 'Upload failed'}`);
      if (completed) await load();
    } finally { upload.disabled = !canEdit; }
  });

  load();
  return root;
}

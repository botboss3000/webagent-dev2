'use strict';

import { apiPath } from '../../../../shared/js/config.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { _fetch, _esc, _fmtDate } from '../utils.js';

let PAGES = ['agents', 'automations', 'browser', 'genui', 'instances', 'wiki', 'admin-tools'];
let FEATURES = ['chat', 'agent_create', 'agent_clone', 'automations', 'connectors', 'model_picker', 'image_input', 'image_generation', 'voice_llm', 'attachments', 'genui', 'user_byod', 'llm_byo'];
let ABILITIES = ['chat_core', 'memory', 'user_files', 'web_read', 'browser_control', 'ssh_control', 'image_vision', 'image_generation', 'model_switching', 'automation', 'agent_orchestration', 'personal_integrations', 'financial_actions', 'developer_write', 'tool_creation', 'platform_admin', 'platform_infra'];
let EFFORTS = ['default', 'low', 'medium', 'high'];
let LIMITS = [
  ['max_agents', 'Agents', 'maximum owned agents'],
  ['max_automations', 'Automations', 'maximum active automations'],
  ['max_connections', 'Connections', 'maximum enabled integrations'],
  ['concurrent_sessions_per_user', 'Concurrent sessions', 'running agent sessions per user'],
  ['messages_per_window', 'Messages per window', 'messages'],
  ['window_seconds', 'Rate window', 'seconds'],
  ['max_attachment_bytes', 'Attachment size', 'bytes per file'],
  ['max_storage_bytes', 'Storage', 'bytes per user'],
  ['weekly_credit_limit', 'Weekly app-credit budget', 'app credits'],
  ['monthly_credit_limit', 'Monthly app-credit budget', 'app credits'],
];
let PAGE_LABELS = {};

const ANONYMOUS_LIMIT_FIELDS = [
  ['identity_messages', 'Messages per anonymous identity', 0],
  ['identity_window_seconds', 'Identity rate window (seconds)', 1],
  ['daily_messages', 'Daily messages per anonymous identity', 0],
  ['daily_window_seconds', 'Daily allowance window (seconds)', 1],
  ['network_messages', 'Messages per anonymous network', 0],
  ['network_window_seconds', 'Network rate window (seconds)', 1],
];
const GLOBAL_ANONYMOUS_FIELDS = [
  ['anonymous_chat_enabled', 'Anonymous chat enabled', 'checkbox'],
  ['anon_session_max', 'Session minting per network', 'number'], ['anon_session_window', 'Session minting window (seconds)', 'number'],
  ['anon_identity_max', 'Distinct identities per network', 'number'], ['anon_identity_window', 'Identity window (seconds)', 'number'],
  ['anon_global_session_max', 'New identities app-wide', 'number'], ['anon_global_session_window', 'Global identity window (seconds)', 'number'],
  ['public_registration_ip_max', 'Registrations per network', 'number'], ['public_registration_global_max', 'Registrations app-wide', 'number'],
  ['public_registration_window', 'Registration window (seconds)', 'number'],
  ['anon_chat_max', 'Legacy identity message ceiling', 'number'], ['anon_chat_window', 'Legacy identity window (seconds)', 'number'],
  ['anon_daily_chat_max', 'Legacy daily message ceiling', 'number'], ['anon_daily_chat_window', 'Legacy daily window (seconds)', 'number'],
  ['anon_chat_ip_max', 'Legacy network message ceiling', 'number'], ['anon_chat_ip_window', 'Legacy network window (seconds)', 'number'],
  ['anon_global_chat_max', 'App-wide anonymous messages', 'number'], ['anon_global_chat_window', 'Global message window (seconds)', 'number'],
  ['anon_budget_max', 'Hard admitted-turn budget', 'number'], ['anon_budget_window', 'Hard-budget window (seconds)', 'number'],
  ['anon_max_concurrent_runs', 'Concurrent model runs', 'number'],
  ['anon_estimated_output_tokens', 'Reserved output tokens', 'number'], ['anon_estimated_cost_per_1k_microusd', 'Estimate μ$ / 1K tokens', 'number'],
  ['anon_token_user_max', 'Guest token budget', 'number'],
  ['anon_token_source_max', 'Network token budget', 'number'], ['anon_token_global_max', 'Global token budget', 'number'],
  ['anon_cost_user_microusd_max', 'Guest cost budget (μ$)', 'number'], ['anon_cost_source_microusd_max', 'Network cost budget (μ$)', 'number'],
  ['anon_cost_global_microusd_max', 'Global cost budget (μ$)', 'number'], ['anon_spend_window', 'Guest/global budget window (seconds)', 'number'],
  ['anon_risk_delay_score', 'Delay risk score', 'number'], ['anon_risk_cooldown_score', 'Cooldown risk score', 'number'],
  ['anon_risk_delay_ms', 'Progressive delay (ms)', 'number'], ['anon_risk_cooldown_seconds', 'Cooldown (seconds)', 'number'],
  ['anon_auto_close_enabled', 'Automatic temporary closure', 'checkbox'], ['anon_auto_close_seconds', 'Auto-close (seconds)', 'number'],
  ['anon_error_max', 'Model errors before pause', 'number'], ['anon_error_window', 'Error window (seconds)', 'number'],
];

let _anonymousSettings = {};
let _anonymousSettingsAvailable = false;
let _initialized = false;
let _loading = null;
let _tab = 'rosters';
let _rosters = [];
let _tiers = [];
let _assignments = [];
let _selected = { rosters: null, tiers: null };
let _settingsHome = null;
let _surface = 'rosters';
let _tierSavedDraft = null;

function _uid() { try { return localStorage.getItem('auth_user_id') || ''; } catch { return ''; } }
function _el(id) { return document.getElementById(id); }
function _csv(value) { return String(value || '').split(',').map(v => v.trim()).filter(Boolean); }
function _slug(value) { return String(value || '').trim().toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, ''); }
function _entryId() { return globalThis.crypto?.randomUUID?.() || `entry-${Date.now()}-${Math.random().toString(16).slice(2)}`; }
function _selectedRecord() { return (_tab === 'rosters' ? _rosters : _tiers).find(row => row.id === _selected[_tab]) || null; }

function _applySchema(schema) {
  if (!schema || schema.unavailable) return;
  const pages = Array.isArray(schema.pages) ? schema.pages : [];
  if (pages.length) {
    PAGES = pages.map(page => typeof page === 'string' ? page : page.id).filter(Boolean);
    PAGE_LABELS = Object.fromEntries(pages.map(page => [
      typeof page === 'string' ? page : page.id,
      typeof page === 'string' ? page : (page.label || page.id),
    ]));
  }
  if (Array.isArray(schema.features) && schema.features.length) FEATURES = schema.features;
  if (Array.isArray(schema.ability_groups) && schema.ability_groups.length) ABILITIES = schema.ability_groups;
  if (Array.isArray(schema.reasoning_levels) && schema.reasoning_levels.length) EFFORTS = schema.reasoning_levels;
  if (schema.limits && typeof schema.limits === 'object') {
    LIMITS = Object.entries(schema.limits).filter(([key]) => key !== 'weekly_credit_renews').map(([key, spec]) => [
      key, spec.meaning || key.replaceAll('_', ' '), spec.unit || 'count',
    ]);
  }
}

function _status(message = '', kind = '') {
  const node = _el('ent-status');
  if (!node) return;
  node.textContent = message;
  node.className = 'ent-status' + (kind ? ` is-${kind}` : '');
}

function _detail(payload, fallback) {
  const detail = payload?.detail;
  if (typeof detail === 'string') return detail;
  if (detail?.code) return String(detail.code).replaceAll('_', ' ');
  if (Array.isArray(detail)) return detail.map(item => item?.msg || String(item)).join('; ');
  return fallback;
}

async function _request(path, { method = 'GET', body, optional = false } = {}) {
  const uid = _uid();
  const isQueryIdentity = method === 'GET' || method === 'DELETE';
  const join = path.includes('?') ? '&' : '?';
  const url = apiPath(path + (isQueryIdentity ? `${join}requesting_user_id=${encodeURIComponent(uid)}` : ''));
  const options = { method, cache: 'no-store' };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify({ requesting_user_id: uid, ...body });
  }
  const response = await _fetch(url, options);
  let payload = null;
  try { payload = await response.json(); } catch {}
  if (!response.ok) {
    if (optional && (response.status === 404 || response.status === 405)) return { unavailable: true };
    throw new Error(_detail(payload, `Request failed (HTTP ${response.status})`));
  }
  return payload || {};
}

function _guard() {
  if (isAdmin()) return true;
  showRestrictedModal();
  return false;
}

function _recordButton(row) {
  const revision = row.published_revision ?? row.revision;
  return `<button type="button" class="ent-record${row.id === _selected[_tab] ? ' is-active' : ''}" data-ent-select="${_esc(row.id)}">
    <span class="ent-record-name">${_esc(row.name || row.slug || row.id)}</span>
    <span class="ent-record-meta"><span class="ent-badge is-${_esc(row.status || 'draft')}">${_esc(row.status || 'draft')}</span><span>rev ${_esc(revision || 1)}</span>${row.has_draft ? '<span>draft changes</span>' : ''}</span>
  </button>`;
}

function _renderShell() {
  const title = _el('ent-list-title');
  const create = _el('ent-create');
  if (title) title.textContent = _tab === 'rosters' ? 'Model rosters' : 'Experience tiers';
  if (create) create.textContent = _tab === 'rosters' ? 'New roster' : 'New tier';
  document.querySelectorAll('[data-ent-tab]').forEach(button => {
    const active = button.dataset.entTab === _tab;
    button.classList.toggle('is-active', active);
    button.setAttribute('aria-selected', String(active));
  });
  const rows = _tab === 'rosters' ? _rosters : _tiers;
  const list = _el('ent-record-list');
  if (list) list.innerHTML = rows.length ? rows.map(_recordButton).join('') : '<div class="ent-muted">No records yet.</div>';
  const tierSelect = _el('ent-tier-select');
  if (tierSelect && _tab === 'tiers') {
    const options = rows.map(row => {
      const label = row.name || row.slug || (row._new ? 'New tier (unsaved)' : row.id);
      return `<option value="${_esc(row.id)}">${_esc(label)}</option>`;
    }).join('');
    tierSelect.innerHTML = `${options}<option value="__new__">New tier…</option>`;
    tierSelect.value = _selected.tiers || '__new__';
  }
  _renderEditor();
}

function _field(label, name, value = '', { wide = false, textarea = false, disabled = false, placeholder = '' } = {}) {
  const tag = textarea ? 'textarea' : 'input';
  const extra = textarea ? '' : ' type="text"';
  const content = textarea ? _esc(value) : '';
  const val = textarea ? '' : ` value="${_esc(value)}"`;
  return `<label class="ent-field${wide ? ' is-wide' : ''}"><span>${_esc(label)}</span><${tag}${extra} class="${textarea ? 'ent-textarea' : 'ent-input'}" data-field="${_esc(name)}"${val} placeholder="${_esc(placeholder)}"${disabled ? ' disabled' : ''}>${content}</${tag}></label>`;
}

function _anonymousUsageControls(tier, policy, locked) {
  if (String(tier.slug || '').toLowerCase() !== 'anonymous') return '';
  const anonymous = policy.anonymous || {};
  const tierFields = ANONYMOUS_LIMIT_FIELDS.map(([key, label, minimum]) => `<label class="ent-field"><span>${_esc(label)} <span class="ent-unit">tier value; 0 disables this tier check</span></span><input type="number" min="${minimum}" step="1" class="ent-input" data-anonymous-limit="${key}" value="${anonymous[key] ?? ''}"${locked ? ' disabled' : ''}></label>`).join('');
  const globalFields = _anonymousSettingsAvailable ? GLOBAL_ANONYMOUS_FIELDS.map(([key, label, type]) => type === 'checkbox'
    ? `<label class="ent-check is-wide"><input type="checkbox" data-global-anonymous="${key}"${_anonymousSettings[key] !== false ? ' checked' : ''}${locked ? ' disabled' : ''}> ${_esc(label)}</label>`
    : `<label class="ent-field"><span>${_esc(label)} <span class="ent-unit">global safety ceiling</span></span><input type="number" min="0" step="1" class="ent-input" data-global-anonymous="${key}" value="${_esc(_anonymousSettings[key] ?? '')}"${locked ? ' disabled' : ''}></label>`
  ).join('') : '<div class="ent-field is-wide"><span>Global safety settings are unavailable; no global values can be changed until they load.</span></div>';
  return `<div class="ent-panel-body ent-limit-grid"><div class="ent-field is-wide"><span><strong>Anonymous tier allowances</strong> <span class="ent-unit">applied to anonymous visitors assigned to this tier</span></span></div>${tierFields}<div class="ent-field is-wide"><span><strong>Global safety ceiling</strong> <span class="ent-unit">platform-wide protections; these cap every anonymous tier and are not tier overrides</span></span></div>${globalFields}</div>`;
}

function _renderEditor() {
  const host = _el('ent-editor');
  if (!host) return;
  const record = _selectedRecord();
  if (!record) {
    host.innerHTML = `<div class="ent-empty">Select a ${_tab === 'rosters' ? 'model roster' : 'tier'}, or create a new one.</div>`;
    return;
  }
  _tierSavedDraft = _tab === 'tiers' ? _canonicalTier(record) : null;
  host.innerHTML = _tab === 'rosters' ? _rosterForm(record) : _tierForm(record);
  if (globalThis.lucide?.createIcons) { try { globalThis.lucide.createIcons(); } catch {} }
}

function _rosterForm(roster) {
  const isNew = Boolean(roster._new);
  const entries = Array.isArray(roster.entries) ? roster.entries : [];
  return `<form class="ent-form" data-ent-form="roster">
    <div class="ent-form-head"><div><h3>${isNew ? 'New model roster' : _esc(roster.name || roster.slug)}</h3><p>${isNew ? 'Create a keyless draft. Credentials are stored separately.' : `Stable ID ${_esc(roster.id)} · draft rev ${_esc(roster.draft_revision || roster.revision || 1)}`}</p></div><span class="ent-badge is-${_esc(roster.status || 'draft')}">${_esc(roster.status || 'draft')}</span></div>
    <div class="ent-grid">
      ${_field('Name', 'name', roster.name)}
      ${_field('Slug', 'slug', roster.slug, { placeholder: 'pro-models' })}
      ${_field('Description', 'description', roster.description, { wide: true, textarea: true })}
    </div>
    <section class="ent-panel"><div class="ent-panel-head"><h4>Roster entries</h4><button type="button" class="ac-btn" data-ent-action="roster-add-entry">Add model</button></div>
      <div class="ent-panel-body" id="ent-roster-entries">${entries.length ? entries.map((entry, index) => _entryForm(roster, entry, index)).join('') : '<div class="ent-muted">Add at least one model before publishing.</div>'}</div>
    </section>
    <div class="ent-actions">
      <button type="submit" class="ac-btn ac-btn-primary">${isNew ? 'Create draft' : 'Save draft'}</button>
      ${isNew ? '' : '<button type="button" class="ac-btn" data-ent-action="roster-validate">Validate</button><button type="button" class="ac-btn" data-ent-action="roster-preview">Preview changes</button><button type="button" class="ac-btn" data-ent-action="roster-history">History</button><span class="ent-actions-spacer"></span><button type="button" class="ac-btn" data-ent-action="roster-publish">Publish</button><button type="button" class="ac-btn is-danger" data-ent-action="roster-retire">Retire</button>'}
    </div>
    <div id="ent-result" aria-live="polite"></div>
  </form>`;
}

function _entryForm(roster, entry, index) {
  const id = entry.entry_id || _entryId();
  const configured = Boolean(entry.credential_configured || roster.credential_state_by_entry?.[id] === 'configured');
  const checks = [
    ['text_capable', 'Text'], ['image_capable', 'Image input'], ['image_out_capable', 'Image output'],
    ['voice_capable', 'Voice'], ['high_effort_capable', 'High reasoning'], ['use_for_image_out', 'Prefer for image output'],
  ];
  return `<article class="ent-entry" data-entry-id="${_esc(id)}">
    <div class="ent-entry-head"><label class="ent-check"><input type="radio" name="ent-default-entry" value="${_esc(id)}"${roster.default_entry_id === id ? ' checked' : ''}> Default model</label><span class="ent-entry-id" title="${_esc(id)}">${_esc(id)}</span><button type="button" class="ac-btn is-danger" data-ent-action="roster-remove-entry" aria-label="Remove model ${index + 1}">Remove</button></div>
    <div class="ent-entry-grid">
      ${_field('Display name', 'display_name', entry.display_name || entry.name || '')}
      ${_field('Provider', 'provider', entry.provider || '', { placeholder: 'openai' })}
      ${_field('Model ID', 'model', entry.model || '', { placeholder: 'gpt-5' })}
      ${_field('Base URL', 'base_url', entry.base_url || '', { placeholder: 'https://api.example.com/v1' })}
      ${_field('Input modalities', 'input_modalities', (entry.input_modalities || []).join(', '), { placeholder: 'text, image' })}
      ${_field('Output modalities', 'output_modalities', (entry.output_modalities || []).join(', '), { placeholder: 'text' })}
      ${_field('Capability tags', 'capabilities', (entry.capabilities || []).join(', '), { wide: true, placeholder: 'tools, json' })}
    </div>
    <div class="ent-check-grid">${checks.map(([key, label]) => `<label class="ent-check"><input type="checkbox" data-field="${key}"${entry[key] ? ' checked' : ''}> ${label}</label>`).join('')}</div>
    <div class="ent-credential-row">
      <label class="ent-field"><span>Provider credential · <span class="ent-credential ${configured ? 'is-set' : 'is-missing'}">${configured ? 'configured' : 'missing'}</span></span><input type="password" class="ent-input" data-credential-for="${_esc(id)}" autocomplete="new-password" placeholder="Enter a new credential"></label>
      <button type="button" class="ac-btn" data-ent-action="credential-set" data-entry="${_esc(id)}"${roster._new ? ' disabled title="Save the roster first"' : ''}>Set</button>
      <button type="button" class="ac-btn is-danger" data-ent-action="credential-delete" data-entry="${_esc(id)}"${!configured || roster._new ? ' disabled' : ''}>Delete</button>
    </div>
  </article>`;
}

function _policyDisclosure(title, body) {
  return `<details class="ent-panel ent-policy-disclosure"><summary class="ent-panel-head"><h4>${_esc(title)}</h4></summary>${body}</details>`;
}

function _checkGroup(title, prefix, values, selected, disabled) {
  const chosen = new Set(selected || []);
  return _policyDisclosure(title, `<div class="ent-panel-body ent-check-grid">${values.map(value => `<label class="ent-check"><input type="checkbox" data-policy-list="${prefix}" value="${_esc(value)}"${chosen.has(value) ? ' checked' : ''}${disabled ? ' disabled' : ''}> ${_esc(prefix === 'pages' ? (PAGE_LABELS[value] || value) : value.replaceAll('_', ' '))}</label>`).join('')}</div>`);
}

function _tierForm(tier) {
  const isNew = Boolean(tier._new);
  const locked = Boolean(tier.is_locked);
  const policy = tier.policy || _blankPolicy(_rosters[0]?.id || '');
  const models = policy.models || {};
  const limits = policy.limits || {};
  const rosterId = tier.roster_id || models.roster_id || '';
  const roster = _rosters.find(row => row.id === rosterId);
  const allowed = new Set(models.allowed_entry_ids || []);
  const badge = _tierBadge(tier);
  return `<form class="ent-form" data-ent-form="tier">
    <div class="ent-form-head"><div><h3>${isNew ? 'New experience tier' : _esc(tier.name || tier.slug)}</h3><p>${isNew ? 'Draft an app experience and publish it when validation passes.' : `Stable ID ${_esc(tier.id)} · revision ${_esc(tier.revision || 1)}`}</p></div><span class="ent-badge is-${badge.state}" data-tier-status>${badge.label}${locked ? ' · locked' : ''}</span></div>
    ${locked ? '<div class="ent-inline-note">This system tier is locked. You can inspect it, but its draft cannot be edited.</div>' : ''}
    <div class="ent-grid">
      ${_field('Name', 'name', tier.name, { disabled: locked })}
      ${_field('Slug', 'slug', tier.slug, { disabled: locked, placeholder: 'team' })}
      ${_field('Description', 'description', tier.description, { wide: true, textarea: true, disabled: locked })}
      <label class="ent-field is-wide"><span>Model roster</span><select class="ent-select" data-field="roster_id"${locked ? ' disabled' : ''}>${_rosters.filter(row => !row._new).map(row => `<option value="${_esc(row.id)}"${row.id === rosterId ? ' selected' : ''}>${_esc(row.name || row.slug)} · ${_esc(row.status || 'draft')}</option>`).join('')}</select></label>
    </div>
    ${_checkGroup('Pages', 'pages', PAGES, policy.pages, locked)}
    ${_checkGroup('Features', 'features', FEATURES, policy.features, locked)}
    ${_checkGroup('Ability groups', 'ability_groups', ABILITIES, policy.ability_groups, locked)}
    ${_policyDisclosure('Agent templates', `<div class="ent-panel-body">${_field('Template IDs', 'agent_templates', (policy.agent_templates || []).join(', '), { wide: true, disabled: locked, placeholder: 'default, research — or * for all' })}</div>`)}
    ${_policyDisclosure('Models and BYO access', `<div class="ent-panel-body ent-grid">
      <label class="ent-field"><span>Maximum reasoning effort</span><select class="ent-select" data-field="max_reasoning_effort"${locked ? ' disabled' : ''}>${EFFORTS.map(level => `<option value="${level}"${models.max_reasoning_effort === level ? ' selected' : ''}>${level}</option>`).join('')}</select></label>
      <label class="ent-field"><span>Maximum BYO entries <span class="ent-unit">blank = unlimited</span></span><input type="number" min="0" class="ent-input" data-field="max_byo_entries" value="${models.max_byo_entries ?? ''}"${locked ? ' disabled' : ''}></label>
      <label class="ent-check is-wide"><input type="checkbox" data-field="allow_byo"${models.allow_byo ? ' checked' : ''}${locked ? ' disabled' : ''}> Allow users to bring their own model credentials</label>
      <div class="ent-field is-wide"><span>Allowed roster entries <span class="ent-unit">choose none to use the roster default only; * allows every entry</span></span><div id="ent-allowed-models" class="ent-check-grid">${_allowedEntryChecks(roster, allowed, locked)}</div></div>
    </div>`)}
    ${_policyDisclosure('Usage limits', `<div class="ent-panel-body ent-limit-grid"><label class="ent-field"><span>Weekly app-credit allowance <span class="ent-unit">blank = unlimited</span></span><input type="number" min="0" step="1" class="ent-input" data-limit="weekly_credit_limit" value="${limits.weekly_credit_limit ?? ''}"${locked ? ' disabled' : ''}></label><label class="ent-check is-wide"><input type="checkbox" data-limit-renewal="weekly_credit_renews"${limits.weekly_credit_renews !== false ? ' checked' : ''}${locked ? ' disabled' : ''}> Renew weekly app-credit allowance. When cleared, the allowance is one-time until an administrator resets it.</label>${LIMITS.filter(([key]) => key !== 'weekly_credit_limit').map(([key, label, unit]) => `<label class="ent-field"><span>${label} <span class="ent-unit">${unit}; blank = unlimited</span></span><input type="number" min="0" step="1" class="ent-input" data-limit="${key}" value="${limits[key] ?? ''}"${locked ? ' disabled' : ''}></label>`).join('')}</div>${_anonymousUsageControls(tier, policy, locked)}`)}
    <div class="ent-actions">
      ${locked ? '' : `<button type="submit" class="ac-btn ac-btn-primary">${isNew ? 'Create draft' : 'Save draft'}</button>`}
      ${isNew ? '' : '<button type="button" class="ac-btn" data-ent-action="tier-validate">Validate</button><button type="button" class="ac-btn" data-ent-action="tier-preview">Preview affected users</button><button type="button" class="ac-btn" data-ent-action="tier-history">History</button><span class="ent-actions-spacer"></span>'}
      ${isNew || locked ? '' : '<button type="button" class="ac-btn" data-ent-action="tier-publish">Publish</button><button type="button" class="ac-btn is-danger" data-ent-action="tier-retire">Retire</button>'}
    </div>
    <div id="ent-result" aria-live="polite"></div>
  </form>`;
}

function _allowedEntryChecks(roster, allowed, disabled) {
  const entries = roster?.entries || [];
  return `<label class="ent-check"><input type="checkbox" data-allowed-entry value="*"${allowed.has('*') ? ' checked' : ''}${disabled ? ' disabled' : ''}> All current and future entries (*)</label>` + entries.map(entry => `<label class="ent-check"><input type="checkbox" data-allowed-entry value="${_esc(entry.entry_id)}"${allowed.has(entry.entry_id) ? ' checked' : ''}${disabled ? ' disabled' : ''}> ${_esc(entry.display_name || entry.name || entry.model || entry.entry_id)}</label>`).join('');
}

function _blankPolicy(rosterId) {
  return { schema_version: 1, pages: [], features: ['chat'], ability_groups: ['chat_core'], agent_templates: ['default'], models: { roster_id: rosterId, allowed_entry_ids: [], allow_byo: false, max_byo_entries: 0, max_reasoning_effort: 'low' }, limits: { ...Object.fromEntries(LIMITS.map(([key]) => [key, 0])), weekly_credit_renews: true } };
}

function _newRoster() {
  const temp = { id: `new-roster-${Date.now()}`, _new: true, slug: '', name: '', description: '', entries: [], default_entry_id: null, status: 'draft' };
  _rosters = [temp, ..._rosters.filter(row => !row._new)];
  _selected.rosters = temp.id;
  _renderShell();
}

function _newTier() {
  const rosterId = _rosters.find(row => !row._new)?.id || '';
  const temp = { id: `new-tier-${Date.now()}`, _new: true, slug: '', name: '', description: '', roster_id: rosterId, policy: _blankPolicy(rosterId), status: 'draft' };
  _tiers = [temp, ..._tiers.filter(row => !row._new)];
  _selected.tiers = temp.id;
  _renderShell();
}

function _readValue(root, name) { return root.querySelector(`[data-field="${name}"]`)?.value?.trim() || ''; }
function _nullableInt(value) { return String(value ?? '').trim() === '' ? null : Number.parseInt(value, 10); }

function _canonicalTier(data) {
  const policy = data.policy || {};
  const models = policy.models || {};
  const ordered = values => [...(values || [])].map(String).sort();
  return JSON.stringify({
    name: String(data.name || '').trim(),
    slug: _slug(data.slug),
    description: String(data.description || '').trim(),
    roster_id: data.roster_id || models.roster_id || '',
    policy: {
      schema_version: 1,
      pages: ordered(policy.pages),
      features: ordered(policy.features),
      ability_groups: ordered(policy.ability_groups),
      agent_templates: ordered(policy.agent_templates),
      models: {
        roster_id: models.roster_id || data.roster_id || '',
        allowed_entry_ids: ordered(models.allowed_entry_ids),
        allow_byo: Boolean(models.allow_byo),
        max_byo_entries: _nullableInt(models.max_byo_entries),
        max_reasoning_effort: models.max_reasoning_effort || 'default',
      },
      limits: { ...Object.fromEntries(LIMITS.map(([key]) => [key, _nullableInt(policy.limits?.[key])])), weekly_credit_renews: policy.limits?.weekly_credit_renews !== false },
      anonymous: Object.fromEntries(ANONYMOUS_LIMIT_FIELDS.map(([key]) => [key, _nullableInt(policy.anonymous?.[key])])),
    },
  });
}

function _tierBadge(tier) {
  if (tier._new) return { label: 'Unsaved', state: 'unsaved' };
  if (tier.status === 'retired') return { label: 'Retired', state: 'retired' };
  if (tier.has_draft || tier.published_revision !== (tier.draft_revision ?? tier.revision)) return { label: 'Saved draft', state: 'saved-draft' };
  return { label: 'Published', state: 'published' };
}

function _syncTierBadge() {
  const form = _el('ent-editor')?.querySelector('[data-ent-form="tier"]');
  const badge = form?.querySelector('[data-tier-status]');
  if (!form || !badge || !_tierSavedDraft) return;
  const dirty = _canonicalTier(_collectTier()) !== _tierSavedDraft;
  const { label, state } = dirty ? { label: 'Unsaved', state: 'unsaved' } : _tierBadge(_selectedRecord());
  badge.textContent = label;
  badge.className = `ent-badge is-${state}`;
}

function _collectRoster() {
  const form = _el('ent-editor').querySelector('[data-ent-form="roster"]');
  const entries = [...form.querySelectorAll('[data-entry-id]')].map(row => {
    const read = name => _readValue(row, name);
    const item = { entry_id: row.dataset.entryId, provider: read('provider'), model: read('model'), base_url: read('base_url') };
    const display = read('display_name');
    if (display) item.display_name = display;
    for (const key of ['input_modalities', 'output_modalities', 'capabilities']) {
      const values = _csv(read(key));
      if (values.length) item[key] = values;
    }
    for (const key of ['text_capable', 'image_capable', 'image_out_capable', 'voice_capable', 'high_effort_capable', 'use_for_image_out']) item[key] = Boolean(row.querySelector(`[data-field="${key}"]`)?.checked);
    return item;
  });
  return { name: _readValue(form, 'name'), slug: _slug(_readValue(form, 'slug')), description: _readValue(form, 'description'), entries, default_entry_id: form.querySelector('[name="ent-default-entry"]:checked')?.value || null };
}

function _collectTier() {
  const form = _el('ent-editor').querySelector('[data-ent-form="tier"]');
  const list = name => [...form.querySelectorAll(`[data-policy-list="${name}"]:checked`)].map(input => input.value);
  const rosterId = _readValue(form, 'roster_id');
  const limits = {};
  form.querySelectorAll('[data-limit]').forEach(input => { limits[input.dataset.limit] = _nullableInt(input.value); });
  limits.weekly_credit_renews = Boolean(form.querySelector('[data-limit-renewal="weekly_credit_renews"]')?.checked);
  const anonymous = {};
  form.querySelectorAll('[data-anonymous-limit]').forEach(input => { anonymous[input.dataset.anonymousLimit] = _nullableInt(input.value); });
  const globalAnonymous = {};
  form.querySelectorAll('[data-global-anonymous]').forEach(input => { globalAnonymous[input.dataset.globalAnonymous] = input.type === 'checkbox' ? input.checked : Math.max(0, Math.round(Number(input.value) || 0)); });
  const policy = {
    schema_version: 1,
    pages: list('pages'), features: list('features'), ability_groups: list('ability_groups'),
    agent_templates: _csv(_readValue(form, 'agent_templates')),
    models: {
      roster_id: rosterId,
      allowed_entry_ids: [...form.querySelectorAll('[data-allowed-entry]:checked')].map(input => input.value),
      allow_byo: Boolean(form.querySelector('[data-field="allow_byo"]')?.checked),
      max_byo_entries: _nullableInt(_readValue(form, 'max_byo_entries')),
      max_reasoning_effort: _readValue(form, 'max_reasoning_effort') || 'default',
    },
    limits,
    anonymous,
  };
  return { name: _readValue(form, 'name'), slug: _slug(_readValue(form, 'slug')), description: _readValue(form, 'description'), roster_id: rosterId, policy, globalAnonymous };
}

function _clientTierErrors(data) {
  const errors = [];
  if (!data.name) errors.push('Name is required.');
  if (!/^[a-z0-9]+(?:[a-z0-9-]*[a-z0-9])?$/.test(data.slug)) errors.push('Slug must use lowercase letters, numbers and hyphens.');
  if (!data.roster_id) errors.push('Choose a model roster.');
  for (const [key] of LIMITS) {
    const value = data.policy.limits[key];
    if (value !== null && (!Number.isInteger(value) || value < 0)) errors.push(`${key} must be a non-negative integer or blank.`);
  }
  const messages = data.policy.limits.messages_per_window;
  const seconds = data.policy.limits.window_seconds;
  if ((messages === null) !== (seconds === null)) errors.push('Messages per window and rate-window seconds should both be set or both be unlimited.');
  return errors;
}

function _showResult(payload, heading = '') {
  const host = _el('ent-result');
  if (!host) return;
  host.innerHTML = '';
  const panel = document.createElement('section');
  panel.className = 'ent-panel';
  const head = document.createElement('div');
  head.className = 'ent-panel-head';
  const title = document.createElement('h4');
  title.textContent = heading || 'Result';
  head.appendChild(title);
  const body = document.createElement('div');
  body.className = 'ent-panel-body';
  const pre = document.createElement('pre');
  pre.className = 'ent-preview';
  pre.textContent = JSON.stringify(payload, null, 2);
  body.appendChild(pre); panel.append(head, body); host.appendChild(panel);
}

function _showHistory(payload, entity) {
  const host = _el('ent-result');
  if (!host) return;
  const revisions = payload.revisions || payload.events || [];
  host.innerHTML = `<section class="ent-panel"><div class="ent-panel-head"><h4>History</h4></div><div class="ent-panel-body ent-history">${revisions.length ? revisions.map(row => {
    const revision = row.revision || row.id || 'event';
    const action = row.action || 'changed';
    const date = row.created_at || row.timestamp;
    const rollback = entity === 'roster' && row.revision ? `<button type="button" class="ac-btn" data-ent-action="roster-rollback" data-revision="${_esc(row.revision)}">Roll back</button>` : entity === 'tier' && row.revision ? `<button type="button" class="ac-btn" data-ent-action="tier-rollback" data-revision="${_esc(row.revision)}">Roll back</button>` : '';
    return `<div class="ent-history-row"><span><strong>${_esc(action)}</strong> · ${_esc(revision)} <span class="ent-muted">${_esc(_fmtDate(date))}</span></span>${rollback}</div>`;
  }).join('') : '<div class="ent-muted">No history is available yet.</div>'}</div></section>`;
}

async function _reload({ preserve = true } = {}) {
  if (!isAdmin()) return;
  const old = { ..._selected };
  const [schema, rosters, tiers, assignments, settings] = await Promise.all([
    _request('/admin/entitlements/schema', { optional: true }),
    _request('/admin/entitlements/rosters'), _request('/admin/entitlements/tiers'), _request('/admin/entitlements/assignments'),
    _request('/admin/settings/app', { optional: true }),
  ]);
  _applySchema(schema);
  _anonymousSettingsAvailable = Boolean(settings && !settings.unavailable);
  _anonymousSettings = _anonymousSettingsAvailable ? settings : {};
  _rosters = rosters.rosters || [];
  _tiers = tiers.tiers || [];
  _assignments = assignments.assignments || [];
  _selected.rosters = preserve && _rosters.some(row => row.id === old.rosters) ? old.rosters : (_rosters[0]?.id || null);
  _selected.tiers = preserve && _tiers.some(row => row.id === old.tiers) ? old.tiers : (_tiers[0]?.id || null);
  _renderShell();
}

async function _saveRoster(event) {
  event.preventDefault();
  if (!_guard()) return;
  const current = _selectedRecord();
  const data = _collectRoster();
  if (!data.name || !data.slug) { _status('Name and slug are required.', 'error'); return; }
  _status('Saving roster draft…');
  const saved = await _request(current._new ? '/admin/entitlements/rosters' : `/admin/entitlements/rosters/${encodeURIComponent(current.id)}`, { method: current._new ? 'POST' : 'PUT', body: data });
  _selected.rosters = saved.id;
  await _reload();
  _status('Roster draft saved.', 'ok');
}

async function _saveTier(event) {
  event.preventDefault();
  if (!_guard()) return;
  const current = _selectedRecord();
  const data = _collectTier();
  const errors = _clientTierErrors(data);
  if (errors.length) { _status(errors.join(' '), 'error'); return; }
  _status('Saving tier draft…');
  const { globalAnonymous, ...tierData } = data;
  const saved = await _request(current._new ? '/admin/entitlements/tiers' : `/admin/entitlements/tiers/${encodeURIComponent(current.id)}`, { method: current._new ? 'POST' : 'PUT', body: { ...tierData, ...(current._new ? { is_system: false, is_locked: false } : { expected_revision: current.draft_revision || current.revision || 1 }) } });
  if (Object.keys(globalAnonymous || {}).length) {
    try {
      await _request('/admin/settings/app', { method: 'POST', body: globalAnonymous });
    } catch (error) {
      _selected.tiers = saved.id;
      await _reload();
      _status(`Tier draft saved, but Global safety ceiling was not saved: ${error.message}`, 'error');
      return;
    }
  }
  _selected.tiers = saved.id;
  await _reload();
  _status(Object.keys(globalAnonymous || {}).length ? 'Tier draft and Global safety ceiling saved.' : 'Tier draft saved.', 'ok');
}

async function _rosterAction(action, button) {
  const roster = _selectedRecord();
  if (!roster || roster._new) return;
  const id = encodeURIComponent(roster.id);
  if (action === 'validate' || action === 'preview') {
    const result = await _request(`/admin/entitlements/rosters/${id}/${action}`, { method: 'POST', body: {} });
    _showResult(result, action === 'preview' ? 'Draft diff and impact' : 'Validation');
    _status(action === 'validate' && result.valid === false ? 'Validation found errors.' : `${action === 'preview' ? 'Preview' : 'Validation'} complete.`, result.valid === false ? 'error' : 'ok');
    return;
  }
  if (action === 'history') {
    _showHistory(await _request(`/admin/entitlements/rosters/${id}/history`), 'roster');
    return;
  }
  if (!_guard()) return;
  if (action === 'retire' && !globalThis.confirm('Retire this roster? Tiers that reference it may stop publishing.')) return;
  const body = { expected_revision: roster.draft_revision || roster.revision || 1, reason: `${action} from Settings UI` };
  await _request(`/admin/entitlements/rosters/${id}/${action}`, { method: 'POST', body });
  await _reload();
  _status(`Roster ${action === 'publish' ? 'published' : 'retired'}.`, 'ok');
}

async function _tierAction(action) {
  const tier = _selectedRecord();
  if (!tier || tier._new) return;
  const id = encodeURIComponent(tier.id);
  if (action === 'validate') {
    const draft = _collectTier();
    const clientErrors = _clientTierErrors(draft);
    const remote = await _request(`/admin/entitlements/tiers/${id}/validate`, { method: 'POST', body: {}, optional: true });
    const result = remote.unavailable ? { valid: !clientErrors.length, errors: clientErrors, note: 'Server-side tier validation is not available in this build; local schema checks were applied.' } : remote;
    _showResult(result, 'Tier validation');
    _status(result.valid === false ? 'Validation found errors.' : 'Validation complete.', result.valid === false ? 'error' : 'ok');
    return;
  }
  if (action === 'preview') {
    const remote = await _request(`/admin/entitlements/tiers/${id}/preview`, { method: 'POST', body: {}, optional: true });
    const affected = new Set(_assignments.filter(row => row.tier_id === tier.id).map(row => row.user_id).filter(Boolean));
    const fallback = { impact: { user_count: affected.size, user_ids: [...affected].sort() }, draft: _collectTier(), note: 'The backend diff endpoint is not available in this build; affected assignments and the current editor draft are shown.' };
    _showResult(remote.unavailable ? fallback : remote, 'Tier impact preview');
    _status('Impact preview ready.', 'ok');
    return;
  }
  if (action === 'history') {
    let remote = await _request(`/admin/entitlements/tiers/${id}/history`, { optional: true });
    if (remote.unavailable) remote = await _request(`/admin/entitlements/audit?entity_type=experience_tier&entity_id=${id}&limit=100`);
    _showHistory(remote, 'tier');
    return;
  }
  if (!_guard()) return;
  if (action === 'retire' && !globalThis.confirm('Retire this experience tier? Existing assignments will retain their audit history.')) return;
  if (action === 'publish') await _request(`/admin/entitlements/tiers/${id}/publish`, { method: 'POST', body: { expected_revision: tier.draft_revision || tier.revision || 1, reason: 'Published from Settings UI' } });
  else {
    const remote = await _request(`/admin/entitlements/tiers/${id}/retire`, { method: 'POST', body: { expected_revision: tier.revision || 1, reason: 'Retired from Settings UI' }, optional: true });
    if (remote.unavailable) await _request(`/admin/entitlements/tiers/${id}`, { method: 'PUT', body: { status: 'retired' } });
  }
  await _reload();
  _status(`Tier ${action === 'publish' ? 'published' : 'retired'}.`, 'ok');
}

async function _credential(action, entryId) {
  if (!_guard()) return;
  const roster = _selectedRecord();
  const input = _el('ent-editor').querySelector(`[data-credential-for="${CSS.escape(entryId)}"]`);
  const credential = input?.value || '';
  if (input) input.value = '';
  if (action === 'set' && !credential) { _status('Enter a credential first.', 'error'); return; }
  if (action === 'delete' && !globalThis.confirm('Delete this model credential from the platform vault?')) return;
  const path = `/admin/entitlements/rosters/${encodeURIComponent(roster.id)}/credentials/${encodeURIComponent(entryId)}`;
  await _request(path, { method: action === 'set' ? 'PUT' : 'DELETE', body: action === 'set' ? { credential, reason: 'Updated from Settings UI' } : undefined });
  await _reload();
  _status(`Credential ${action === 'set' ? 'stored' : 'deleted'}; its value is not readable from this page.`, 'ok');
}

async function _rollback(entity, revision) {
  if (!_guard()) return;
  const record = _selectedRecord();
  const reason = globalThis.prompt('Reason for rollback:', `Restore revision ${revision}`)?.trim();
  if (!reason) return;
  const result = await _request(`/admin/entitlements/${entity === 'roster' ? 'rosters' : 'tiers'}/${encodeURIComponent(record.id)}/rollback`, { method: 'POST', body: { revision: Number(revision), reason }, optional: entity === 'tier' });
  if (result.unavailable) { _status('Tier rollback is not available in this server build.', 'error'); return; }
  await _reload();
  _status(`Revision ${revision} restored. Review the draft before publishing.`, 'ok');
}

async function _handleAction(button) {
  const action = button.dataset.entAction;
  if (action === 'roster-add-entry') {
    const roster = _selectedRecord();
    Object.assign(roster, _collectRoster());
    roster.entries = [...(roster.entries || []), { entry_id: _entryId(), text_capable: true }];
    _renderEditor(); return;
  }
  if (action === 'roster-remove-entry') {
    const roster = _selectedRecord();
    const row = button.closest('[data-entry-id]');
    Object.assign(roster, _collectRoster());
    roster.entries = (roster.entries || []).filter(entry => entry.entry_id !== row.dataset.entryId);
    if (roster.default_entry_id === row.dataset.entryId) roster.default_entry_id = null;
    _renderEditor(); return;
  }
  if (action?.startsWith('credential-')) { await _credential(action.slice('credential-'.length), button.dataset.entry); return; }
  if (action?.startsWith('roster-') && action.endsWith('rollback')) { await _rollback('roster', button.dataset.revision); return; }
  if (action?.startsWith('tier-') && action.endsWith('rollback')) { await _rollback('tier', button.dataset.revision); return; }
  if (action?.startsWith('roster-')) { await _rosterAction(action.slice(7), button); return; }
  if (action?.startsWith('tier-')) await _tierAction(action.slice(5));
}

function _wire() {
  document.querySelectorAll('[data-ent-tab]').forEach(button => button.addEventListener('click', () => {
    _tab = button.dataset.entTab;
    _renderShell();
  }));
  _el('ent-create')?.addEventListener('click', () => {
    if (!_guard()) return;
    _tab === 'rosters' ? _newRoster() : _newTier();
  });
  _el('ent-record-list')?.addEventListener('click', event => {
    const button = event.target.closest('[data-ent-select]');
    if (!button) return;
    _selected[_tab] = button.dataset.entSelect;
    _renderShell();
  });
  _el('ent-tier-select')?.addEventListener('change', event => {
    if (event.target.value === '__new__') {
      if (!_guard()) { _renderShell(); return; }
      _newTier();
      return;
    }
    _selected.tiers = event.target.value;
    _renderShell();
  });
  _el('ent-editor')?.addEventListener('submit', event => {
    const form = event.target.closest('[data-ent-form]');
    if (!form) return;
    Promise.resolve(form.dataset.entForm === 'roster' ? _saveRoster(event) : _saveTier(event)).catch(error => _status(error.message, 'error'));
  });
  _el('ent-editor')?.addEventListener('click', event => {
    const button = event.target.closest('[data-ent-action]');
    if (!button) return;
    event.preventDefault();
    button.disabled = true;
    Promise.resolve(_handleAction(button)).catch(error => _status(error.message, 'error')).finally(() => { if (button.isConnected) button.disabled = false; });
  });
  _el('ent-editor')?.addEventListener('input', event => {
    if (event.target.closest('[data-ent-form="tier"]')) _syncTierBadge();
  });
  _el('ent-editor')?.addEventListener('change', event => {
    if (!event.target.closest('[data-ent-form="tier"]')) return;
    if (event.target.matches('[data-field="roster_id"]')) {
      const host = _el('ent-allowed-models');
      const roster = _rosters.find(row => row.id === event.target.value);
      if (host) host.innerHTML = _allowedEntryChecks(roster, new Set(), false);
    }
    _syncTierBadge();
  });
}

function _setSurface(surface, host) {
  const page = _el('ent-title')?.closest('.ent-page');
  if (!page) return;
  if (!_settingsHome) _settingsHome = page.parentElement;
  const destination = host || _settingsHome;
  if (destination && page.parentElement !== destination) destination.appendChild(page);
  _surface = surface === 'tiers' ? 'tiers' : 'rosters';
  _tab = _surface;

  const title = _el('ent-title');
  const intro = title?.parentElement?.querySelector('p');
  const segmented = page.querySelector('.ent-segmented');
  const tierSurface = _surface === 'tiers';
  if (segmented) segmented.hidden = true;
  page.querySelector('[data-ent-admin-note]')?.remove();
  const tierPicker = _el('ent-tier-picker');
  if (tierPicker) tierPicker.hidden = !tierSurface;
  const workspace = page.querySelector('.ent-workspace');
  if (workspace) workspace.classList.toggle('is-tier-surface', tierSurface);
  const sidebar = page.querySelector('.ent-sidebar');
  if (sidebar) sidebar.hidden = tierSurface;
  if (tierSurface) {
    if (title) title.textContent = 'App user policies';
    if (intro) intro.hidden = true;
  } else {
    if (title) title.textContent = 'Model rosters';
    if (intro) {
      intro.textContent = 'Build and publish the model catalogs that app user policies can select.';
      intro.hidden = false;
    }
  }
  _renderShell();
}

export function init() {
  if (_initialized) return;
  const page = _el('ent-title')?.closest('.ent-page');
  if (page) _settingsHome = page.parentElement;
  _initialized = true;
  _wire();
  _setSurface('rosters', _settingsHome);
}

export function showModelRosters() {
  if (!_initialized) init();
  _setSurface('rosters', _settingsHome);
}

export async function attachExperienceTierEditor(host) {
  if (!_initialized) init();
  _setSurface('tiers', host);
  return load();
}

export async function load() {
  if (!_initialized) init();
  if (!isAdmin()) {
    const host = _el('ent-editor');
    if (host) host.innerHTML = '<div class="ent-empty">Administrator access is required to manage experience tiers.</div>';
    return;
  }
  if (_loading) return _loading;
  _status('Loading experience tiers…');
  _loading = _reload().then(() => _status('')).catch(error => {
    _status(`Could not load entitlements: ${error.message}`, 'error');
    const host = _el('ent-editor');
    if (host) host.innerHTML = '<div class="ent-empty">Entitlement settings could not be loaded.</div>';
  }).finally(() => { _loading = null; });
  return _loading;
}

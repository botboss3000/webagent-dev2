'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// Don't write hex/rgb colour literals when styling elements. CSS variables resolve
// inside inline styles, so use e.g. el.style.background = 'rgba(var(--brand-rgb), 0.12)'
// or el.style.color = 'var(--accent)'. New colour? Add a token to the palette there first.

/**
 * Agent Settings tab — Models, Integrations, Agent Tools, Admin Chat.
 *
 * Sections:
 *   1. Models        — providers, model search, default-model pick, auto-save
 *   2. Integrations  — OAuth scopes, generic providers, config cards
 *   3. Agent Tools   — ability compact rows, enable/disable
 *   4. Admin Chat    — integration admin assistant chat
 *
 * This is the largest App Config tab (~2,200 lines). Sub-split candidates:
 * agent-settings-models.js, agent-settings-integrations.js, agent-settings-abilities.js
 */

import { apiPath } from '../../../../shared/js/config.js';
import { app } from '../../../../shared/js/state.js';
import { isAdmin, showRestrictedModal } from '../../../../shared/js/left-login.js?v=253';
import { build as buildAdminAbilityTable } from '../../../../shared/js/admin-ability-table.js';
import { wireChatPillUploads, uploadPendingAttachments } from '../../../../shared/js/attachments.js';
import { mountModelTable, capabilityFlagsHtml } from '../../../../shared/js/model-table.js?v=15';
import { reconcileSavedModelRows } from '../../../../shared/js/model-roster-reconcile.js';
import { initModelAnalytics } from './model-analytics.js?v=7';
// Automation Engine panels (relocated from the standalone App Config "Automation"
// and "Event Sources" tabs) — the scheduler + event-sources halves rendered in
// this page's "Automation Engine" section. init() wires their buttons; load()
// runs lazily on first expand (see init()).
import { init as initSchedulerPanel, load as loadSchedulerPanel } from './scheduler.js';
import { init as initEventSourcesPanel, load as loadEventSourcesPanel } from './event-sources.js';
// App-level Gates table (App Settings → Agent tools & abilities). Lists every
// gate type in plugins/gates/ with its app-wide guidance — see the module header.
import { init as initGatesSection } from './gates-section.js';
import {
  _fetch, _qs, _esc, _setIntStatus,
  _makeCopyBtn, _copyToClipboard, _bindUri, _attachUriCopy,
} from '../utils.js';
// Unified save-status indicator (spinner → ✓ / ⚠-with-revert, centred ON TOP of
// the control) — one save language for every toggle / dropdown / input / Save
// button across the config panels. See the ui_toggle_list skill (Codebase Admin).
import { _markSaving, _flashSaveCheck, _paintGroupStatus } from '../../../../shared/js/dom-utils.js';

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 1: Models ─────────────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────
// SISTER-PANEL: MODEL-TABLE — the Models configurator + saved-models table is
// the shared component ui/shared/js/model-table.js, mounted here AND on the agent
// card's Config tab; keep the two mirrored (change the component, not a copy).
// This file only supplies the admin data adapter (provider-form drafts + the
// model roster) and relocates the app-global extras into the Advanced panel.

let _modelTable = null;
let _modelAnalytics = null;

// Timestamp of the last full Agent-Settings data load (model table + suggestions
// + integrations). init() kicks these off when the Admin tab initialises; load()
// fires again the moment the App Config view is activated — on a first open both
// happen within the same tick, which used to double every fetch (including the
// slow /admin/integrations and the model-usage N+1). load() consults this stamp
// and skips the redundant re-fetch when init() just triggered one, while still
// refreshing on genuine later re-activations.
let _lastAgentLoadAt = 0;

function _liveAdminModelAdapter() {
  const headers = { 'Content-Type': 'application/json' };
  return {
    loadConfig: async () => {
      // ONE combined read (single vault+DB resolve on the server) instead of two
      // sequential round-trips for provider drafts + the roster. Falls back to
      // the two reads IN PARALLEL if the bundle endpoint isn't available yet
      // (older backend / server not yet restarted after this change).
      let single = {}, multi = {};
      try {
        const r = await _fetch(apiPath('/admin/settings/provider-bundle'));
        if (r.ok) {
          const b = await r.json();
          single = b.provider || {};
          multi = { providers: b.roster || [] };
        } else {
          throw new Error('no-bundle');   // fall through to the legacy parallel pair
        }
      } catch (_) {
        const [r1, r2] = await Promise.all([
          _fetch(apiPath('/admin/settings/provider')).catch(() => null),
          _fetch(apiPath('/admin/settings/multi-providers')).catch(() => null),
        ]);
        try { if (r1 && r1.ok) single = await r1.json(); } catch (_e) {}
        try { if (r2 && r2.ok) multi = await r2.json(); } catch (_e) {}
      }
      return {
        provider: single.provider, auth_mode: single.auth_mode, base_url: single.base_url, api_key: single.api_key, model: single.model,
        text_capable: single.text_capable, image_capable: single.image_capable, image_out_capable: single.image_out_capable, voice_capable: single.voice_capable,
        providerConfigs: single.providers || {},
        roster: multi.providers || [],
      };
    },
    saveSingle: async (s) => {
      if (!isAdmin()) { showRestrictedModal(); throw new Error('Not an admin'); }
      const payload = { ...s, use_for_image_out: s.image_out_capable };
      const r = await _fetch(apiPath('/admin/settings/provider'), { method: 'POST', headers, body: JSON.stringify(payload) });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
    },
    saveRoster: async ({ providers }) => {
      if (!isAdmin()) { showRestrictedModal(); throw new Error('Not an admin'); }
      const payload = {
        providers: providers.map(p => ({
          entry_id: p.entry_id || '',
          alias_group_id: p.alias_group_id || '',
          provider: p.provider === '_custom' ? 'custom' : p.provider,
          auth_mode: p.auth_mode || (p.provider === 'openai_subscription' ? 'chatgpt' : 'api_key'),
          base_url: p.base_url, api_key: p.api_key, model: p.model,
          display_name: p.display_name || '',
          enabled: p.enabled,
          text_capable: p.text_capable !== false, image_capable: !!p.image_capable, use_for_image: !!p.use_for_image,
          image_out_capable: !!p.image_out_capable, use_for_image_out: !!p.use_for_image_out,
          voice_capable: !!p.voice_capable,
          routing_mode: p.routing_mode || '',
          credential_scope: p.credential_scope || '',
          high_effort_capable: !!p.high_effort_capable,
          use_for_system: !!p.use_for_system,
          use_for_voice: !!p.use_for_voice,
        })),
      };
      // Surface a real failure so the saved-model box settles to the orange ⚠
      // (and reverts) instead of falsely flashing a green ✓.
      const r = await _fetch(apiPath('/admin/settings/multi-providers'), { method: 'POST', headers, body: JSON.stringify(payload) });
      const data = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(data.detail || `HTTP ${r.status}`);
      reconcileSavedModelRows(providers, data.providers || []);
      return data;
    },
    // Provider presets — try the server first, fall back to a hardcoded mirror
    // of PROVIDER_PRESETS in app/admin/settings.py so the dropdown always shows
    // every known provider even when the backend endpoint is unreachable.
    loadPresets: async () => {
      try {
        const r = await _fetch(apiPath('/admin/settings/providers'));
        if (r.ok) { const d = await r.json(); if (d && Object.keys(d).length) return d; }
      } catch (_) {}
      return {
        openrouter:  { name: 'OpenRouter',      base_url: 'https://openrouter.ai/api/v1' },
        openai:      { name: 'ChatGPT API',       base_url: 'https://api.openai.com/v1' },
        openai_subscription: { name: 'ChatGPT Subscription', base_url: 'codex://chatgpt', auth_mode: 'chatgpt' },
        gemini:      { name: 'Google Gemini',    base_url: 'https://generativelanguage.googleapis.com/v1beta/openai' },
        groq:        { name: 'Groq',             base_url: 'https://api.groq.com/openai/v1' },
        together:    { name: 'Together AI',       base_url: 'https://api.together.xyz/v1' },
        deepseek:    { name: 'DeepSeek',          base_url: 'https://api.deepseek.com/v1' },
        mistral:     { name: 'Mistral AI',        base_url: 'https://api.mistral.ai/v1' },
        fireworks:   { name: 'Fireworks AI',      base_url: 'https://api.fireworks.ai/inference/v1' },
        xai:         { name: 'xAI (Grok)',        base_url: 'https://api.x.ai/v1' },
        perplexity:  { name: 'Perplexity',        base_url: 'https://api.perplexity.ai' },
        ollama:      { name: 'Ollama (local)',    base_url: 'http://localhost:11434/v1' },
        deepinfra:   { name: 'DeepInfra',         base_url: 'https://api.deepinfra.com/v1/openai' },
        lmstudio:    { name: 'LM Studio (local)', base_url: 'http://localhost:1234/v1' },
      };
    },
  };
}

// Settings → Models scope + revision state.  Model-table mutations are staged
// in memory through this adapter; only the toolbar actions cross the roster API.
const _BASE_MODEL_ROSTER_ID = 'roster-admin';
let _modelScopeSelection = 'base';
let _modelScopeId = _BASE_MODEL_ROSTER_ID;
let _modelScopeRow = null;
let _modelScopesLoaded = false;
let _modelScopeDirty = false;
let _modelSingleDraft = null;
let _modelStagedProviders = null;

function _modelActorId() {
  try { return localStorage.getItem('auth_user_id') || app.currentUserId || ''; }
  catch { return app.currentUserId || ''; }
}

async function _modelScopeRequest(path, { method = 'GET', body } = {}) {
  const actor = _modelActorId();
  const queryIdentity = method === 'GET' || method === 'DELETE';
  const join = path.includes('?') ? '&' : '?';
  const url = apiPath(path + (queryIdentity ? `${join}requesting_user_id=${encodeURIComponent(actor)}` : ''));
  const options = { method, cache: 'no-store' };
  if (body !== undefined) {
    options.headers = { 'Content-Type': 'application/json' };
    options.body = JSON.stringify({ requesting_user_id: actor, ...body });
  }
  const response = await _fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = typeof payload.detail === 'string'
      ? payload.detail
      : (payload.detail?.code || `Request failed (HTTP ${response.status})`);
    throw new Error(detail);
  }
  return payload;
}

function _setModelScopeStatus(state, message = '') {
  const badge = _qs('ac-model-scope-status');
  const publish = _qs('ac-model-publish');
  const save = _qs('ac-model-save-draft');
  if (badge) {
    const labels = { published: 'Published', 'saved-draft': 'Saved draft', unsaved: 'Unsaved', loading: 'Loading…', error: 'Error' };
    badge.textContent = message || labels[state] || state;
    badge.className = `model-scope-status is-${state}`;
  }
  if (publish) publish.disabled = state === 'unsaved' || state === 'loading' || state === 'error';
  if (save) save.disabled = state === 'loading';
}

function _persistedModelScopeState(row = _modelScopeRow) {
  if (!row) return 'loading';
  return row.has_draft || row.status !== 'published' ? 'saved-draft' : 'published';
}

function _markModelScopeDirty() {
  _modelScopeDirty = true;
  _setModelScopeStatus('unsaved');
}

function _modelRosterEntry(provider) {
  return {
    entry_id: provider.entry_id || '',
    alias_group_id: provider.alias_group_id || '',
    display_name: provider.display_name || '',
    provider: provider.provider === '_custom' ? 'custom' : provider.provider,
    auth_mode: provider.auth_mode || (provider.provider === 'openai_subscription' ? 'chatgpt' : 'api_key'),
    base_url: provider.base_url || '',
    model: provider.model || '',
    enabled: !!provider.enabled,
    text_capable: provider.text_capable !== false,
    image_capable: !!provider.image_capable,
    use_for_image: !!provider.use_for_image,
    image_out_capable: !!provider.image_out_capable,
    use_for_image_out: !!provider.use_for_image_out,
    voice_capable: !!provider.voice_capable,
    credential_scope: provider.credential_scope || '',
    high_effort_capable: !!provider.high_effort_capable,
    use_for_system: !!provider.use_for_system,
    use_for_voice: !!provider.use_for_voice,
    ...(provider.stateful_responses ? { stateful_responses: provider.stateful_responses } : {}),
    ...(provider.routing_mode ? { routing_mode: provider.routing_mode } : {}),
    ...(provider.credit_schedule ? { credit_schedule: provider.credit_schedule } : {}),
    ...(Number.isFinite(Number(provider.credit_input_per_million))
      ? { credit_input_per_million: Number(provider.credit_input_per_million) } : {}),
    ...(Number.isFinite(Number(provider.credit_output_per_million))
      ? { credit_output_per_million: Number(provider.credit_output_per_million) } : {}),
    // MODEL-PRICE-EDIT: the OWNER's own reference price for a BYO row, USD per 1M.
    // Only a real number is persisted; a blank value means "no override", and a
    // catalog row never carries one because its price belongs to the app admin.
    ...(provider.reference_cost_input !== null && provider.reference_cost_input !== undefined
      && provider.reference_cost_input !== '' && Number.isFinite(Number(provider.reference_cost_input))
      ? { reference_cost_input: Number(provider.reference_cost_input) } : {}),
    ...(provider.reference_cost_output !== null && provider.reference_cost_output !== undefined
      && provider.reference_cost_output !== '' && Number.isFinite(Number(provider.reference_cost_output))
      ? { reference_cost_output: Number(provider.reference_cost_output) } : {}),
  };
}

function _modelConfigFromRoster(row) {
  const roster = Array.isArray(row?.entries) ? row.entries.map(item => ({ ...item })) : [];
  const standard = roster.find(item => item.enabled && item.text_capable !== false) || roster[0] || {};
  return {
    provider: standard.provider || '', auth_mode: standard.auth_mode || '',
    base_url: standard.base_url || '', api_key: '', model: standard.model || '',
    text_capable: standard.text_capable !== false,
    image_capable: !!standard.image_capable,
    image_out_capable: !!standard.image_out_capable,
    voice_capable: !!standard.voice_capable,
    providerConfigs: {}, roster,
  };
}

function _paintModelScopeHint() {
  const hint = _qs('ac-model-scope-hint');
  const extras = _qs('ac-model-adv-extras');
  const base = _modelScopeId === _BASE_MODEL_ROSTER_ID;
  const select = _qs('ac-model-scope');
  const selected = select?.selectedOptions?.[0];
  const label = selected?.textContent || 'this tier';
  const shared = select
    ? [...select.options].filter(option => option !== selected && option.dataset.rosterId === _modelScopeId)
      .map(option => option.textContent)
    : [];
  if (hint) hint.textContent = base
    ? 'Base is the global roster for administrators. User tiers remain unchanged.'
    : shared.length
      ? `Editing the ${label} model roster, shared with ${shared.join(', ')}. Changes affect those tiers too; Base remains unchanged.`
      : `Editing the ${label} model roster. Base and other user tiers remain unchanged.`;
  if (extras) extras.hidden = !base;
  const retire = document.querySelector('[data-model-lifecycle="retire"]');
  if (retire) retire.hidden = base;
}

async function _ensureModelScopes() {
  if (_modelScopesLoaded) return;
  const data = await _modelScopeRequest('/admin/entitlements/tiers');
  const select = _qs('ac-model-scope');
  if (select) {
    select.innerHTML = '';
    const base = document.createElement('option');
    base.value = 'base';
    base.dataset.rosterId = _BASE_MODEL_ROSTER_ID;
    base.textContent = 'Base — Admin only';
    select.appendChild(base);
    (data.tiers || [])
      .filter(tier => tier.roster_id && tier.slug !== 'admin')
      .forEach(tier => {
        const option = document.createElement('option');
        option.value = `tier:${tier.id}`;
        option.dataset.rosterId = tier.roster_id;
        option.dataset.tierSlug = tier.slug || '';
        option.textContent = tier.name || tier.slug || tier.id;
        select.appendChild(option);
      });
    select.value = _modelScopeSelection;
  }
  _modelScopesLoaded = true;
}

async function _loadModelScope({ refresh = false } = {}) {
  await _ensureModelScopes();
  if (!_modelScopeRow || refresh) {
    _setModelScopeStatus('loading');
    _modelScopeRow = await _modelScopeRequest(`/admin/entitlements/rosters/${encodeURIComponent(_modelScopeId)}`);
  }
  _modelScopeDirty = false;
  _modelSingleDraft = null;
  _modelStagedProviders = null;
  _setModelScopeStatus(_persistedModelScopeState());
  _paintModelScopeHint();
  return _modelScopeRow;
}

function _scopedAdminModelAdapter() {
  const live = _liveAdminModelAdapter();
  return {
    ...live,
    loadConfig: async () => _modelConfigFromRoster(await _loadModelScope()),
    saveSingle: async (single) => {
      if (!isAdmin()) { showRestrictedModal(); throw new Error('Not an admin'); }
      _modelSingleDraft = { ...single };
      _markModelScopeDirty();
    },
    saveRoster: async ({ providers }) => {
      if (!isAdmin()) { showRestrictedModal(); throw new Error('Not an admin'); }
      _modelStagedProviders = providers;
      _markModelScopeDirty();
      return { providers };
    },
    canDeleteCredential: (provider) => Boolean(
      provider?.entry_id && provider.provider !== 'openai_subscription' && provider.auth_mode !== 'chatgpt'
    ),
    deleteCredential: async (provider) => {
      if (!provider?.entry_id) throw new Error('Save the model before removing its credential');
      const row = await _modelScopeRequest(
        `/admin/entitlements/rosters/${encodeURIComponent(_modelScopeId)}/credentials/${encodeURIComponent(provider.entry_id)}?reason=${encodeURIComponent('Removed from Settings Models')}`,
        { method: 'DELETE' },
      );
      _modelScopeRow = row;
      provider.credential_configured = false;
      provider.credential_suffix = '';
      provider.api_key = '';
      return row;
    },
  };
}

async function _saveModelScopeDraft() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const button = _qs('ac-model-save-draft');
  if (button) _markSaving(button);
  try {
    const providers = _modelStagedProviders || _modelTable?.getState()?.providers || [];
    const entries = providers.map(_modelRosterEntry);
    const catalogDefault = entries.find(entry => entry.text_capable !== false);
    const row = await _modelScopeRequest(`/admin/entitlements/rosters/${encodeURIComponent(_modelScopeId)}`, {
      method: 'PUT',
      body: { entries, default_entry_id: catalogDefault?.entry_id || null },
    });
    reconcileSavedModelRows(providers, row.entries || []);
    for (const provider of providers) {
      const credential = String(provider.api_key || '').trim();
      if (!credential) continue;
      if (!provider.entry_id) throw new Error('The server did not assign this model an ID');
      await _modelScopeRequest(`/admin/entitlements/rosters/${encodeURIComponent(_modelScopeId)}/credentials/${encodeURIComponent(provider.entry_id)}`, {
        method: 'PUT', body: { credential },
      });
    }
    _modelScopeRow = row;
    _modelScopeDirty = false;
    _modelSingleDraft = null;
    _modelStagedProviders = null;
    _setModelScopeStatus(_persistedModelScopeState(row));
    if (button) _flashSaveCheck(button, true);
    await _modelTable?.reload();
  } catch (error) {
    _setModelScopeStatus('error', error.message || 'Save failed');
    if (button) _flashSaveCheck(button, false, error.message || 'Save failed');
  }
}

async function _publishModelScope() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  if (_modelScopeDirty) return _setModelScopeStatus('unsaved');
  const button = _qs('ac-model-publish');
  if (button) _markSaving(button);
  try {
    const row = await _modelScopeRequest(`/admin/entitlements/rosters/${encodeURIComponent(_modelScopeId)}/publish`, {
      method: 'POST', body: { expected_revision: _modelScopeRow?.revision },
    });
    _modelScopeRow = row;
    _setModelScopeStatus('published');
    if (button) _flashSaveCheck(button, true);
    app.refreshModelContext?.();
  } catch (error) {
    _setModelScopeStatus('error', error.message || 'Publish failed');
    if (button) _flashSaveCheck(button, false, error.message || 'Publish failed');
  }
}

function _modelLifecycleResult(title, payload, summary = []) {
  const host = _qs('ac-model-lifecycle-result');
  if (!host) return;
  host.hidden = false;
  host.replaceChildren();
  const heading = document.createElement('h4');
  heading.textContent = title;
  host.appendChild(heading);
  if (summary.length) {
    const line = document.createElement('div');
    line.className = 'model-lifecycle-summary';
    summary.forEach(text => { const chip = document.createElement('span'); chip.textContent = text; line.appendChild(chip); });
    host.appendChild(line);
  }
  const pre = document.createElement('pre');
  pre.textContent = JSON.stringify(payload, null, 2);
  host.appendChild(pre);
}

function _hideModelLifecycleResult() {
  const host = _qs('ac-model-lifecycle-result');
  if (host) { host.hidden = true; host.replaceChildren(); }
}

function _renderModelHistory(payload) {
  const host = _qs('ac-model-lifecycle-result');
  if (!host) return;
  host.hidden = false;
  host.replaceChildren();
  const heading = document.createElement('h4'); heading.textContent = 'Roster history'; host.appendChild(heading);
  const list = document.createElement('div'); list.className = 'model-lifecycle-history';
  const published = Number(payload?.published_revision || 0);
  const rows = Array.isArray(payload?.revisions) ? payload.revisions : [];
  if (!rows.length) { list.textContent = 'No published revisions are available yet.'; }
  rows.forEach(revision => {
    const row = document.createElement('div'); row.className = 'model-lifecycle-history-row';
    const label = document.createElement('span');
    const number = Number(revision.revision || 0);
    label.textContent = `Revision ${number}${number === published ? ' · current' : ''} · ${revision.action || 'published'} · ${revision.created_at || ''}`;
    const rollback = document.createElement('button'); rollback.type = 'button'; rollback.className = 'ac-btn'; rollback.textContent = 'Roll back';
    rollback.disabled = number === published || !number;
    rollback.addEventListener('click', () => _rollbackModelScope(number));
    row.append(label, rollback); list.appendChild(row);
  });
  host.appendChild(list);
}

async function _reloadModelScopeAfterLifecycle(row) {
  _modelScopeRow = row || null;
  _modelScopeDirty = false;
  _modelStagedProviders = null;
  _modelSingleDraft = null;
  await _modelTable?.reload();
  _setModelScopeStatus(_persistedModelScopeState());
  app.refreshModelContext?.();
}

async function _rollbackModelScope(revision) {
  const reason = window.prompt('Reason for rollback:', `Restore revision ${revision}`)?.trim();
  if (!reason) return;
  try {
    const row = await _modelScopeRequest(`/admin/entitlements/rosters/${encodeURIComponent(_modelScopeId)}/rollback`, {
      method: 'POST', body: { revision, reason },
    });
    await _reloadModelScopeAfterLifecycle(row);
    _modelLifecycleResult(`Revision ${revision} rolled back`, { roster: row }, ['Published']);
  } catch (error) {
    _modelLifecycleResult('Rollback failed', { error: error.message || 'Unknown error' });
  }
}

async function _runModelLifecycle(action) {
  if (!isAdmin()) { showRestrictedModal(); return; }
  if (_modelScopeDirty) {
    _modelLifecycleResult('Save draft first', { message: 'Lifecycle actions inspect the saved server draft. Save the current local changes first.' });
    return;
  }
  const actions = _qs('ac-model-lifecycle-actions');
  if (actions) actions.open = false;
  const id = encodeURIComponent(_modelScopeId);
  try {
    if (action === 'validate' || action === 'preview') {
      const payload = await _modelScopeRequest(`/admin/entitlements/rosters/${id}/${action}`, { method: 'POST', body: {} });
      const impact = payload.impact || {};
      const validation = payload.validation || payload;
      const summary = action === 'preview'
        ? [`${impact.tier_count || 0} tier${impact.tier_count === 1 ? '' : 's'}`, `${impact.user_count || 0} user${impact.user_count === 1 ? '' : 's'}`, validation.valid === false ? 'Validation errors' : 'Validation ready']
        : [validation.valid === false ? 'Validation errors' : 'Valid', `${(validation.warnings || []).length} warning(s)`];
      _modelLifecycleResult(action === 'preview' ? 'Draft diff and impact' : 'Validation', payload, summary);
      return;
    }
    if (action === 'history') {
      _renderModelHistory(await _modelScopeRequest(`/admin/entitlements/rosters/${id}/history`));
      return;
    }
    if (action === 'retire') {
      if (_modelScopeId === _BASE_MODEL_ROSTER_ID) {
        _modelLifecycleResult('Base cannot be retired', { message: 'Base — Admin only remains the required global model source.' });
        return;
      }
      if (!window.confirm('Retire this roster? Tiers that reference it may stop publishing.')) return;
      const row = await _modelScopeRequest(`/admin/entitlements/rosters/${id}/retire`, {
        method: 'POST', body: { expected_revision: _modelScopeRow?.revision, reason: 'Retired from Settings Models' },
      });
      await _reloadModelScopeAfterLifecycle(row);
      _modelLifecycleResult('Roster retired', { roster: row }, ['Retired']);
    }
  } catch (error) {
    _modelLifecycleResult(`${action[0].toUpperCase()}${action.slice(1)} failed`, { error: error.message || 'Unknown error' });
  }
}

async function _changeModelScope() {
  const select = _qs('ac-model-scope');
  if (!select || select.value === _modelScopeSelection) return;
  if (_modelScopeDirty && !window.confirm('Discard unsaved model-roster changes?')) {
    select.value = _modelScopeSelection;
    return;
  }
  _modelScopeSelection = select.value;
  _modelScopeId = select.selectedOptions[0]?.dataset.rosterId || _BASE_MODEL_ROSTER_ID;
  _modelScopeRow = null;
  _modelScopeDirty = false;
  _hideModelLifecycleResult();
  await _modelTable?.reload();
}

function _initModelScopeToolbar() {
  _qs('ac-model-scope')?.addEventListener('change', _changeModelScope);
  _qs('ac-model-save-draft')?.addEventListener('click', _saveModelScopeDraft);
  _qs('ac-model-publish')?.addEventListener('click', _publishModelScope);
  _qs('ac-model-lifecycle-actions')?.addEventListener('click', event => {
    const button = event.target.closest('[data-model-lifecycle]');
    if (button) _runModelLifecycle(button.dataset.modelLifecycle);
  });
}

export function mountGlobalModelTable(host, opts = {}) {
  return mountModelTable(host, {
    ...opts,
    adapter: _liveAdminModelAdapter(),
    fetchFn: (...a) => _fetch(...a),
    apiPath,
    // Both global mounts show app-wide usage: every agent, every user, and
    // background tasks (git messages, suggestion text, compaction, embeddings).
    usageScope: 'global',
  });
}

function _userCatalogModelAdapter() {
  let catalogModels = [];
  return {
    loadConfig: async () => {
      const response = await _fetch(apiPath('/api/v1/entitlements/me/model-catalog'), { cache: 'no-store' });
      if (!response.ok) throw new Error(`Unable to load Available models (HTTP ${response.status})`);
      const payload = await response.json();
      catalogModels = Array.isArray(payload?.available_platform_models) ? payload.available_platform_models : [];
      return {
        provider: '', auth_mode: '', base_url: '', api_key: '', providerConfigs: {},
        roster: Array.isArray(payload?.entries) ? payload.entries : [],
        default_entry_id: String(payload?.default_entry_id || ''),
      };
    },
    saveRoster: async ({ providers, default_entry_id = '' }) => {
      const platform_entry_ids = providers.filter(row => row.catalog_origin === 'platform').map(row => row.entry_id).filter(Boolean);
      // MODEL-NAME-LEVELS: the caller's own display name per adopted platform
      // entry. A stored value equal to the entry's catalog name is the server's own
      // fallback copy, not a name the caller chose, so it is sent as blank.
      const platform_aliases = {};
      providers.filter(row => row.catalog_origin === 'platform').forEach((row) => {
        const stored = String(row.display_name || '').trim();
        const catalogName = String(row.catalog_name || '').trim();
        platform_aliases[String(row.entry_id || '')] = stored && stored !== catalogName ? stored : '';
      });
      // MODEL-NAME-LEVELS: catalog_name is a catalog projection, not roster data,
      // and the BYO entries schema rejects unknown fields.
      const entries = providers
        .filter(row => row.catalog_origin !== 'platform' && row.catalog_entry_source !== 'agent')
        .map(row => ({ ...row, catalog_origin: undefined, catalog_name: undefined, catalog_entry_source: undefined }));
      const response = await _fetch(apiPath('/api/v1/entitlements/me/model-catalog'), {
        method: 'PUT', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ entries, platform_entry_ids, platform_aliases, default_entry_id }),
      });
      const payload = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(payload?.detail?.message || payload?.detail || `HTTP ${response.status}`);
      reconcileSavedModelRows(providers, payload.entries || []);
      return { providers };
    },
    // Available models supports the same explicit BYO providers as the admin
    // model editor. WebAgent Catalog stays first; the shared table puts Custom
    // second and these presets below it for this user-catalog mount.
    loadPresets: async () => ({
      webagent_catalog: { name: 'WebAgent Catalog', base_url: '' },
      ...(await _liveAdminModelAdapter().loadPresets()),
    }),
    fetchModels: async ({ provider }) => provider === 'webagent_catalog'
      ? { models: catalogModels.map(row => ({
        ...row,
        id: row.entry_id,
        // MODEL-CATALOG-IDENTITY: prefer the app-authored catalog name; a blank
        // catalog name shows a neutral label. The real provider/model string and
        // the opaque ID stay internal and are never picker labels.
        name: row.display_name || 'WebAgent Catalog model',
      })) }
      : { models: [] },
    fetchModelInfo: async () => ({}),
    fetchModelUsage: async ({ model }) => {
      // USAGE-ALL-AGENTS: this column reports what the signed-in user has spent
      // on THAT MODEL ACROSS ALL THEIR AGENTS — the same scope as the analytics
      // chart mounted above it (view.js passes scope: 'user'). Deliberately
      // provider-agnostic: a model billed under several provider strings, or a
      // row added through the WebAgent Catalog (provider 'webagent_catalog'),
      // still totals correctly when no provider filter is sent.
      try {
        const params = new URLSearchParams({ model: model || '', provider: '', scope: 'user' });
        const response = await _fetch(apiPath(`/admin/settings/model-usage?${params.toString()}`));
        const payload = response.ok ? await response.json() : null;
        return (payload && !payload.error && payload.total_input_tokens != null) ? payload : null;
      } catch (_) {
        return null;
      }
    },
  };
}

export function mountUserModelCatalog(host, opts = {}) {
  return mountModelTable(host, {
    ...opts,
    adapter: _userCatalogModelAdapter(),
    fetchFn: (...a) => _fetch(...a),
    apiPath,
    usageScope: 'user',
    catalogOnly: true,
    catalogReadOnly: false,
    userCatalog: true,
  });
}

function _renderFreeModelRanking() {
  const host = _qs('ac-model-host');
  if (!host || document.getElementById('ac-free-model-ranking')) return;
  // This is deliberately a sibling of the mounted model-table root. It must not
  // become a table row or advancedExtra descendant: ranking is app-wide control plane.
  const section = document.createElement('section');
  section.id = 'ac-free-model-ranking';
  section.className = 'ac-free-ranking ac-list';
  section.setAttribute('aria-labelledby', 'ac-free-ranking-title');
  section.innerHTML = `
    <div class="ac-free-ranking-head">
      <div><h3 id="ac-free-ranking-title">Free Model Ranking</h3><p>Benchmark OpenRouter’s zero-price catalog and route opted-in F-slots to the healthiest eligible candidate. Native OpenRouter Auto remains the safe fallback.</p></div>
      <span class="ac-free-ranking-badge">Admin control plane</span>
    </div>
    <div class="ac-ability-row ac-row-static ac-free-ranking-connect">
      <span class="ac-ability-label"><span class="ac-ability-name">OpenRouter API key</span><span class="ac-ability-desc">Used only for free-model benchmarks. It is encrypted in the existing credential vault and never displayed after saving.</span></span>
      <span class="ac-free-ranking-actions"><input id="ac-free-ranking-openrouter-vault-key" name="free_model_ranking_openrouter_key" class="ac-input ac-input-sm ac-free-ranking-key" type="password" autocomplete="off" spellcheck="false" placeholder="sk-or-v1-..." aria-label="OpenRouter API key for Free Model Ranking" data-free-ranking-openrouter-key><button id="ac-free-ranking-connect-vault" class="ac-btn ac-btn-primary ac-btn-sm" type="button" data-free-ranking-connect>Connect OpenRouter</button><span id="ac-free-ranking-credential" class="ac-free-ranking-credential">Not connected</span></span>
    </div>
    <div class="ac-ability-row ac-row-static ac-free-ranking-settings">
      <span class="ac-ability-label"><span class="ac-ability-name">Monitor schedule</span><span class="ac-ability-desc">Fixed synthetic probes only. No chats, files, memory, or tool data are sent.</span></span>
      <span class="ac-free-ranking-actions"><label class="ac-free-ranking-toggle"><input id="ac-free-ranking-enabled" type="checkbox"> Enable monitor</label><label class="ac-free-ranking-cadence">Every <input id="ac-free-ranking-cadence" class="ac-input ac-input-sm" type="number" min="0.1" max="168" step="0.1" value="1"> hours</label><button id="ac-free-ranking-save" class="ac-btn ac-btn-sm" type="button">Save</button><button id="ac-free-ranking-run" class="ac-btn ac-btn-primary ac-btn-sm" type="button">Run now</button></span>
    </div>
    <div class="ac-free-ranking-statusline"><span id="ac-free-ranking-status" role="status">Disabled — native OpenRouter Auto routing remains active.</span></div>
    <div class="ac-free-ranking-grid" role="table" aria-label="Free model ranking"><div class="ac-free-ranking-grid-head" role="row">
      <span>Candidate <button type="button" class="ac-free-ranking-info" title="The explicit OpenRouter model ID discovered with zero prompt and completion pricing. The native openrouter/free router is excluded." aria-label="Candidate details">i</button></span>
      <span>Capabilities <button type="button" class="ac-free-ranking-info" title="Detected media capabilities, shown for every free candidate: T text chat, V vision (reads images), I image generation, mic live voice input. A highlighted square means the capability is present; a muted square means the model does not advertise it. Same flags as the model table above." aria-label="Capability details">i</button></span>
      <span>Health <button type="button" class="ac-free-ranking-info" title="Passes when an authenticated explicit-ID request returns HTTP 200 with a usable non-empty completion within the timeout. No user data is used. The muted age after the verdict is how long the candidate has held its current health, measured from the probe that reported it." aria-label="Health measurement details">i</button></span>
      <span>Latency <button type="button" class="ac-free-ranking-info" title="End-to-end milliseconds from sending the fixed text probe until the complete response arrives. Lower is better; latency contributes up to 20 score points." aria-label="Latency measurement details">i</button></span>
      <span>Tokens/s <button type="button" class="ac-free-ranking-info" title="Completion tokens reported by OpenRouter divided by elapsed response seconds. Higher is better; throughput contributes up to 10 score points, reaching the cap at 20 tokens per second." aria-label="Tokens per second measurement details">i</button></span>
      <span>Reliability <button type="button" class="ac-free-ranking-info" title="Percentage of the 20 most recent retained probes that returned a healthy usable completion. At least 70 percent is required for eligibility; reliability contributes up to 30 points." aria-label="Reliability measurement details">i</button></span>
      <span>Agentic <button type="button" class="ac-free-ranking-info" title="Whether a second synthetic request returned an OpenAI-compatible tool_calls structure. The declared probe tool is harmless and is never executed. Contributes up to 20 score points." aria-label="Agentic ability measurement details">i</button></span>
      <span>Compatibility <button type="button" class="ac-free-ranking-info" title="Whether the synthetic tool call used the expected WebAgent function name and parseable required JSON arguments. At least 70 percent compatibility is required for eligibility and contributes up to 10 points." aria-label="WebAgent compatibility measurement details">i</button></span>
      <span>Score <button type="button" class="ac-free-ranking-info" title="Transparent 0–100 total: reliability 30, latency 20, throughput 10, agentic tool calling 20, WebAgent compatibility 10, and manual rating 10. Failed health or compatibility prevents eligibility." aria-label="Score calculation details">i</button></span>
      <span>Manual <button type="button" class="ac-free-ranking-info" title="Administrator rating from 1 to 5. It contributes 0, 2.5, 5, 7.5, or 10 points; an unrated model is neutral at 5. It can never override failed health or incompatibility." aria-label="Manual rating details">i</button></span>
      <span>Last result <button type="button" class="ac-free-ranking-info" title="Most recent redacted probe outcome or failure reason. Credentials and response bodies are never shown." aria-label="Last result details">i</button></span>
    </div><div class="ac-free-ranking-grid-empty" role="row">Connect OpenRouter to discover free candidates.</div></div>`;
  const tableRoot = host.firstElementChild;
  if (tableRoot) tableRoot.after(section); else host.after(section);
  const status = _qs('ac-free-ranking-status');
  const credential = _qs('ac-free-ranking-credential');
  const grid = section.querySelector('.ac-free-ranking-grid');
  const safe = value => String(value ?? '').replace(/[&<>"']/g, char => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
  // HEALTH-AGE: the server stamps `status_since` when a probe actually flips a
  // candidate's health, so the Health column can pair the verdict with how long
  // it has held ("Healthy 2d", "Healthy 4h", "Failed 7h"). Rows written before
  // that stamp existed, or by an older server, keep the bare verdict. Formatted
  // from the freshly loaded rows; the panel is never the source of the age.
  const healthAge = row => {
    const stamp = Number(row.status_since);
    if (!Number.isFinite(stamp) || stamp <= 0) return '';
    const seconds = Math.floor(Date.now() / 1000 - stamp);
    if (seconds < 0) return '';                       // clock skew: never a negative age
    if (seconds < 60) return `${seconds}s`;
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)}h`;
    return `${Math.floor(seconds / 86400)}d`;
  };
  const healthCell = row => {
    const status = row.status === 'ok' ? 'Healthy' : row.status === 'failed' ? 'Failed' : 'Pending';
    const age = healthAge(row);
    return age ? `${status} <span class="ac-free-ranking-age">${age}</span>` : status;
  };
  // SCORE-ORDER: the table reads best-first. The server returns candidates in catalog
  // order, so order a copy here by the Score column — highest at the top. Candidates with
  // no score at all (never benchmarked, or a legacy payload without one) sink below every
  // scored candidate; a reported zero still counts as scored, so it stays above them. The
  // model ID breaks ties so the order stays stable between loads.
  const scoreValue = row => {
    const raw = row?.score;
    if (raw === null || raw === undefined || raw === '') return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  };
  const orderByScore = rows => [...rows].sort((a, b) => {
    const left = scoreValue(a), right = scoreValue(b);
    if ((left === null) !== (right === null)) return left === null ? 1 : -1;
    if (left !== null && right !== left) return right - left;
    return String(a.model_id ?? '').localeCompare(String(b.model_id ?? ''));
  });
  const renderRows = rows => {
    grid.querySelectorAll('.ac-free-ranking-grid-row, .ac-free-ranking-grid-empty').forEach(node => node.remove());
    if (!rows?.length) { grid.insertAdjacentHTML('beforeend', '<div class="ac-free-ranking-grid-empty" role="row">No free models currently returned by OpenRouter.</div>'); return; }
    orderByScore(rows).forEach(row => grid.insertAdjacentHTML('beforeend', `<div class="ac-free-ranking-grid-row" role="row"><span title="${safe(row.model_id)}">${safe(row.display_name || row.model_id)}</span><span class="ac-free-ranking-caps"><span class="ac-saved-flags">${capabilityFlagsHtml(row)}</span></span><span>${healthCell(row)}</span><span>${row.latency_ms == null ? '—' : `${Math.round(row.latency_ms)} ms`}</span><span>${row.tokens_per_second == null ? '—' : safe(row.tokens_per_second)}</span><span>${row.reliability == null ? '—' : `${Math.round(Number(row.reliability) * 100)}%`}</span><span>${row.agentic_ok == null ? '—' : row.agentic_ok ? 'Pass' : 'Fail'}</span><span title="${safe(row.compatibility_reason || '')}">${row.compatibility_ok == null ? '—' : row.compatibility_ok ? 'Pass' : 'Fail'}</span><span title="${safe(row.score_components ? Object.entries(row.score_components).map(([name, points]) => `${name}: ${points}`).join(' · ') : '')}">${row.score == null ? '—' : safe(row.score)}</span><span>${row.manual_rating || '—'}</span><span title="${safe(row.last_result || row.reason || '')}">${safe(row.last_result || row.reason || 'Not benchmarked')}</span></div>`));
  };
  const loadRows = async () => {
    const res = await _fetch(apiPath('/admin/settings/free-model-ranking/candidates'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json(); renderRows(data.rows || []); return data;
  };
  const load = async () => {
    try {
      const res = await _fetch(apiPath('/admin/settings/free-model-ranking/config'));
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const cfg = await res.json();
      _qs('ac-free-ranking-enabled').checked = !!cfg.enabled;
      _qs('ac-free-ranking-cadence').value = cfg.cadence_hours || 1;
      if (cfg.openrouter_configured) { credential.textContent = 'Connected securely'; credential.classList.add('is-connected'); }
      status.textContent = cfg.enabled ? `Enabled — next run in ${cfg.cadence_hours}h.` : 'Disabled — native OpenRouter Auto routing remains active.';
      await loadRows();
    } catch (_) { /* Older server: keep the safe defaults visible. */ }
  };
  section.querySelector('[data-free-ranking-connect]')?.addEventListener('click', async () => {
    const keyInput = section.querySelector('[data-free-ranking-openrouter-key]');
    const apiKey = keyInput?.value.trim() || '';
    if (!apiKey) { status.textContent = 'Enter your OpenRouter API key to connect.'; keyInput?.focus(); return; }
    status.textContent = 'Saving OpenRouter credential securely…';
    try {
      const res = await _fetch(apiPath('/admin/settings/free-model-ranking/connect'), { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({api_key: apiKey}) });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || data.message || `HTTP ${res.status}`);
      keyInput.value = '';
      credential.textContent = data.credential_suffix ? `Connected securely · …${data.credential_suffix}` : 'Connected securely';
      credential.classList.add('is-connected');
      await loadRows();
      status.textContent = data.message || 'OpenRouter connected securely.';
    } catch (e) { status.textContent = `Unable to connect OpenRouter: ${e.message}`; }
  });
  const save = async () => {
    try {
      const body = { enabled: _qs('ac-free-ranking-enabled').checked, cadence_hours: Number(_qs('ac-free-ranking-cadence').value) };
      const res = await _fetch(apiPath('/admin/settings/free-model-ranking/config'), { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const cfg = await res.json(); status.textContent = cfg.enabled ? `Enabled — next run in ${cfg.cadence_hours}h.` : 'Disabled — native OpenRouter Auto routing remains active.';
    } catch (e) { status.textContent = `Unable to save ranking settings: ${e.message}`; }
  };
  _qs('ac-free-ranking-save')?.addEventListener('click', save);
  _qs('ac-free-ranking-run')?.addEventListener('click', async event => {
    const button = event.currentTarget; button.disabled = true;
    status.textContent = 'Running bounded free-model benchmarks…';
    try { const res = await _fetch(apiPath('/admin/settings/free-model-ranking/run'), {method:'POST'}); const data = await res.json(); if (data.rows) renderRows(data.rows); status.textContent = data.message || (res.ok ? 'Benchmark completed.' : 'Benchmark request was rejected.'); }
    catch (e) { status.textContent = `Unable to start benchmark: ${e.message}`; }
    finally { button.disabled = false; }
  });
  load();
}

function _initModelAnalytics() {
  const host = _qs('ac-model-analytics');
  if (!host) return;
  _modelAnalytics = initModelAnalytics(host, { fetchFn: (...args) => _fetch(...args), apiPath });
}

function _initModelTable() {
  const host = _qs('ac-model-host');
  if (!host) return;
  _modelTable = mountModelTable(host, {
    adapter: _scopedAdminModelAdapter(),
    fetchFn: (...args) => _fetch(...args),
    apiPath,
    usageScope: 'global',
    // App-level "Model Catalog": role assignment is disabled here. Rows render
    // provider-derived capability badges (read-only); roles live at the agent
    // Config tab only.
    catalogOnly: true,
    advancedExtra: (body) => {
      // Relocate the app-global extras into the table's advanced body. NOTE: this
      // runs while `body` is still DETACHED from the document (mountModelTable
      // attaches the table to the host only after this callback), so the moved
      // checkbox is NOT yet findable by document.getElementById — its change
      // listener is wired in init() right after _initModelTable() returns, once
      // the table (and this checkbox) are in the document.
      const extras = _qs('ac-model-adv-extras');
      if (extras) { extras.classList.remove('ac-hidden'); body.appendChild(extras); }
    },
    onModelChange: () => { app.refreshModelContext?.(); },
    // MODEL-LEGEND-ALIAS: the chart above this table labels its model series by the
    // same display names these rows render, so the roster is handed over on load.
    onRoster: (providers) => _modelAnalytics?.setModelLabels(providers),
    // MODEL-ROW-FOCUS: opening a saved row narrows the chart above to that model;
    // closing the row (or reloading the roster) hands the whole group back.
    onModelFocus: (model) => _modelAnalytics?.setModelFocus(model),
  });
  _renderFreeModelRanking();
}

// -- AI Message Suggestions checkbox ----------------------------------------
// Toggle on/off the silent suggestion-engine. "Off" by default.
function _initSuggestionsCheckbox() {
  const cb = _qs('ac-use-ai-suggestions');
  if (!cb) return;
  cb.addEventListener('change', _saveSuggestionsCheckbox);
}

async function _loadSuggestionsCheckbox() {
  const cb = _qs('ac-use-ai-suggestions');
  if (!cb) return;
  try {
    const res = await _fetch(apiPath('/api/v1/chat/suggestions/config'));
    if (res.ok) {
      const cfg = await res.json();
      cb.checked = cfg.mode === 'on' || cfg.mode === 'scheduler';
    }
  } catch (_) {}
}

async function _saveSuggestionsCheckbox() {
  const cb = _qs('ac-use-ai-suggestions');
  if (!cb) return;
  // Overlay sits on the whole checkbox row (the wrapping label).
  const ctrl = cb.closest('label');
  const mode = cb.checked ? 'on' : 'off';
  _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath('/api/v1/chat/suggestions/config'), {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode }),
    });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _flashSaveCheck(ctrl, true);
  } catch (e) {
    cb.checked = !cb.checked;  // revert on failure
    _flashSaveCheck(ctrl, false, e.message);
  }
}

// -- Memory embeddings source/model (app/agent/embed.py) --------------------
// APP-WIDE, admin-only. Relocated (with the other model extras) into the model
// table's Advanced panel. NOT per-agent: every stored memory vector shares one
// column + width, so all agents must embed identically. Applying a change
// re-embeds every saved memory via POST /admin/settings/embedding/reindex.
function _initEmbeddingControl() {
  const src = _qs('ac-embed-source');
  if (!src) return;
  src.addEventListener('change', () => {
    const localRow = _qs('ac-embed-local-row');
    if (localRow) localRow.classList.toggle('ac-hidden', src.value !== 'local');
  });
  const btn = _qs('ac-embed-reindex');
  if (btn) btn.addEventListener('click', _applyEmbeddingReindex);
}

async function _loadEmbeddingControl() {
  const src = _qs('ac-embed-source');
  if (!src) return;
  try {
    const res = await _fetch(apiPath('/admin/settings/embedding'));
    if (!res.ok) return;
    const cfg = await res.json();
    src.value = (cfg.source === 'local') ? 'local' : 'cloud';
    const modelSel = _qs('ac-embed-model');
    if (modelSel) {
      modelSel.innerHTML = '';
      (cfg.local_models || []).forEach((m) => {
        const o = document.createElement('option');
        o.value = m.id;
        o.textContent = `${m.id} · ${m.dim}-dim`;
        modelSel.appendChild(o);
      });
      if (cfg.source === 'local' && cfg.model) modelSel.value = cfg.model;
    }
    const localRow = _qs('ac-embed-local-row');
    if (localRow) localRow.classList.toggle('ac-hidden', cfg.source !== 'local');
    const warn = _qs('ac-embed-local-warn');
    if (warn) {
      if (!cfg.local_available) {
        warn.classList.remove('ac-hidden');
        warn.textContent = `In-process engine not installed — ${cfg.local_reason || 'run: uv sync --extra embeddings-local'}. Selecting Local needs it.`;
      } else {
        warn.classList.add('ac-hidden');
      }
    }
    const cur = _qs('ac-embed-current');
    if (cur) cur.innerHTML = `Active: <strong>${_esc(cfg.source)}</strong> · <code>${_esc(cfg.model)}</code> · ${cfg.dim}-dim`;
  } catch (_) {}
}

async function _applyEmbeddingReindex() {
  if (!isAdmin()) { showRestrictedModal(); return; }
  const src = _qs('ac-embed-source');
  const modelSel = _qs('ac-embed-model');
  const btn = _qs('ac-embed-reindex');
  const status = _qs('ac-embed-status');
  if (!src) return;
  const source = (src.value === 'local') ? 'local' : 'cloud';
  const model = (source === 'local' && modelSel) ? modelSel.value : '';
  if (!window.confirm('Re-embed every saved memory with the selected engine? During the rebuild, memory search falls back to keyword-only.')) return;
  if (btn) btn.disabled = true;
  if (status) { status.classList.remove('ac-hidden'); status.classList.add('ac-fg3'); status.classList.remove('ac-fg-success', 'ac-fg-danger'); status.textContent = 'Re-embedding all memories… this can take a moment.'; }
  try {
    const res = await _fetch(apiPath('/admin/settings/embedding/reindex'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ embedding_source: source, embedding_model: model }),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const parts = Object.entries((data.result && data.result.tables) || {})
      .map(([t, r]) => `${t}: ${r.written ?? 0}/${r.rows ?? 0}`).join(', ');
    if (status) { status.classList.add('ac-fg-success'); status.classList.remove('ac-fg3', 'ac-fg-danger'); status.textContent = `Done — now ${_esc(data.source)} · ${_esc(data.model)} · ${data.dim}-dim. Reindexed ${parts || 'nothing'}.`; }
    _loadEmbeddingControl();
  } catch (e) {
    if (status) { status.classList.add('ac-fg-danger'); status.classList.remove('ac-fg3', 'ac-fg-success'); status.textContent = `Failed: ${e.message}`; }
  } finally {
    if (btn) btn.disabled = false;
  }
}

// ─────────────────────────────────────────────────────────────────────────
// ── SECTION 2b: Integrations ─────────────────────────────────────────────
// ─────────────────────────────────────────────────────────────────────────

const _PROVIDER_SCOPES = {
  google: [
    { value: 'openid',                                              label: 'Sign in (OpenID)' },
    { value: 'email',                                               label: 'Email address' },
    { value: 'profile',                                             label: 'Basic profile' },
    { value: 'https://www.googleapis.com/auth/drive.file',          label: 'Drive: app-created files' },
    { value: 'https://www.googleapis.com/auth/drive',               label: 'Drive: all files (read & write)' },
    { value: 'https://www.googleapis.com/auth/drive.readonly',      label: 'Drive: all files (read only)' },
    { value: 'https://www.googleapis.com/auth/docs',                label: 'Docs: read & write' },
    { value: 'https://www.googleapis.com/auth/docs.readonly',       label: 'Docs: read only' },
    { value: 'https://www.googleapis.com/auth/calendar',            label: 'Calendar: read & write' },
    { value: 'https://www.googleapis.com/auth/calendar.readonly',   label: 'Calendar: read only' },
    { value: 'https://www.googleapis.com/auth/gmail.modify',        label: 'Gmail: read & modify' },
    { value: 'https://www.googleapis.com/auth/gmail.compose',       label: 'Gmail: compose' },
    { value: 'https://www.googleapis.com/auth/gmail.readonly',      label: 'Gmail: read only' },
  ],
  microsoft: [
    { value: 'openid',               label: 'Sign in (OpenID)' },
    { value: 'email',                label: 'Email address' },
    { value: 'profile',              label: 'Basic profile' },
    { value: 'offline_access',       label: 'Stay signed in (refresh tokens)' },
    { value: 'User.Read',            label: 'User profile' },
    { value: 'Mail.ReadWrite',       label: 'Mail: read & write' },
    { value: 'Mail.Send',            label: 'Mail: send' },
    { value: 'Calendars.ReadWrite',  label: 'Calendar: read & write' },
    { value: 'Files.ReadWrite.All',  label: 'OneDrive: read & write' },
    { value: 'Sites.ReadWrite.All',  label: 'SharePoint: read & write' },
  ],
  yahoo: [
    { value: 'openid',   label: 'Sign in (OpenID)' },
    { value: 'email',    label: 'Email address' },
    { value: 'profile',  label: 'Basic profile' },
    { value: 'mail-r',   label: 'Mail: read' },
    { value: 'mail-w',   label: 'Mail: write' },
  ],
  dropbox: [
    { value: 'account_info.read',      label: 'Account info' },
    { value: 'files.metadata.read',    label: 'Files: read metadata' },
    { value: 'files.metadata.write',   label: 'Files: write metadata' },
    { value: 'files.content.read',     label: 'Files: read content' },
    { value: 'files.content.write',    label: 'Files: write content' },
    { value: 'sharing.read',           label: 'Sharing: read' },
    { value: 'sharing.write',          label: 'Sharing: write' },
  ],
  meta: [
    { value: 'public_profile',              label: 'Public profile' },
    { value: 'email',                       label: 'Email address' },
    { value: 'pages_manage_posts',          label: 'Pages: manage posts' },
    { value: 'pages_read_engagement',       label: 'Pages: read engagement' },
    { value: 'instagram_basic',             label: 'Instagram: basic' },
    { value: 'instagram_content_publish',   label: 'Instagram: publish content' },
    { value: 'instagram_manage_comments',   label: 'Instagram: manage comments' },
    { value: 'instagram_manage_insights',   label: 'Instagram: insights' },
  ],
  twitter: [
    { value: 'tweet.read',          label: 'Tweets: read' },
    { value: 'tweet.write',         label: 'Tweets: write' },
    { value: 'tweet.moderate.write',label: 'Tweets: moderate' },
    { value: 'users.read',          label: 'Users: read' },
    { value: 'follows.read',        label: 'Follows: read' },
    { value: 'follows.write',       label: 'Follows: write' },
    { value: 'offline.access',      label: 'Stay signed in (refresh tokens)' },
    { value: 'like.read',           label: 'Likes: read' },
    { value: 'like.write',          label: 'Likes: write' },
    { value: 'dm.read',             label: 'DMs: read' },
    { value: 'dm.write',            label: 'DMs: write' },
  ],
  linkedin: [
    { value: 'openid',          label: 'Sign in (OpenID)' },
    { value: 'profile',         label: 'Basic profile' },
    { value: 'email',           label: 'Email address' },
    { value: 'w_member_social', label: 'Posts: write' },
    { value: 'r_liteprofile',   label: 'Profile: read' },
    { value: 'r_emailaddress',  label: 'Email: read' },
  ],
  tiktok: [
    { value: 'user.info.basic',    label: 'User: basic info' },
    { value: 'user.info.profile',  label: 'User: profile' },
    { value: 'video.list',         label: 'Videos: list' },
    { value: 'video.upload',       label: 'Videos: upload' },
    { value: 'video.publish',      label: 'Videos: publish' },
  ],
  pinterest: [
    { value: 'boards:read',         label: 'Boards: read' },
    { value: 'boards:write',        label: 'Boards: write' },
    { value: 'pins:read',           label: 'Pins: read' },
    { value: 'pins:write',          label: 'Pins: write' },
    { value: 'user_accounts:read',  label: 'Account: read' },
  ],
  reddit: [
    { value: 'identity',        label: 'Identity' },
    { value: 'read',            label: 'Posts: read' },
    { value: 'submit',          label: 'Posts: submit' },
    { value: 'history',         label: 'History' },
    { value: 'mysubreddits',    label: 'Subreddits' },
    { value: 'privatemessages', label: 'Messages' },
  ],
  snapchat: [
    { value: 'https://auth.snapchat.com/oauth2/api/user.display_name',   label: 'Display name' },
    { value: 'https://auth.snapchat.com/oauth2/api/user.bitmoji.avatar', label: 'Bitmoji avatar' },
    { value: 'https://auth.snapchat.com/oauth2/api/user.external_id',    label: 'External ID' },
  ],
  twitch: [
    { value: 'user:read:email',              label: 'Email address' },
    { value: 'user:read:follows',            label: 'Follows: read' },
    { value: 'channel:read:subscriptions',   label: 'Subscriptions: read' },
    { value: 'channel:manage:broadcast',     label: 'Broadcast: manage' },
    { value: 'clips:edit',                   label: 'Clips: edit' },
    { value: 'chat:read',                    label: 'Chat: read' },
    { value: 'chat:edit',                    label: 'Chat: write' },
  ],
  ebay: [
    { value: 'https://api.ebay.com/oauth/api_scope',                              label: 'Public APIs' },
    { value: 'https://api.ebay.com/oauth/api_scope/buy.order.readonly',           label: 'Buy: orders (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.inventory',               label: 'Sell: inventory' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.inventory.readonly',      label: 'Sell: inventory (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.account',                 label: 'Sell: account' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.account.readonly',        label: 'Sell: account (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.fulfillment',             label: 'Sell: fulfillment' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.fulfillment.readonly',    label: 'Sell: fulfillment (read)' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.marketing',               label: 'Sell: marketing' },
    { value: 'https://api.ebay.com/oauth/api_scope/sell.marketing.readonly',      label: 'Sell: marketing (read)' },
  ],
  etsy: [
    { value: 'email_r',          label: 'Email (read)' },
    { value: 'profile_r',        label: 'Profile (read)' },
    { value: 'shops_r',          label: 'Shops (read)' },
    { value: 'shops_w',          label: 'Shops (write)' },
    { value: 'listings_r',       label: 'Listings (read)' },
    { value: 'listings_w',       label: 'Listings (write)' },
    { value: 'listings_d',       label: 'Listings (delete)' },
    { value: 'transactions_r',   label: 'Transactions (read)' },
  ],
  shopify: [
    { value: 'read_products',    label: 'Products (read)' },
    { value: 'write_products',   label: 'Products (write)' },
    { value: 'read_inventory',   label: 'Inventory (read)' },
    { value: 'write_inventory',  label: 'Inventory (write)' },
    { value: 'read_orders',      label: 'Orders (read)' },
    { value: 'write_orders',     label: 'Orders (write)' },
    { value: 'read_locations',   label: 'Locations (read)' },
  ],
  amazon: [
    { value: 'sellingpartnerapi::client_credential:refresh_token', label: 'SP-API refresh token' },
  ],
};

function _renderScopeCheckboxes(provider) {
  const formEl = document.getElementById(`ac-int-${provider}-form`);
  if (!formEl) return;
  const scopes = _PROVIDER_SCOPES[provider];
  if (!scopes || scopes.length === 0) return;

  const container = document.createElement('div');
  container.id = `ac-int-${provider}-scopes-wrap`;
  container.classList.add('ac-mt14');

  const header = document.createElement('div');
  header.classList.add('ac-scopes-header');
  header.innerHTML = `
    <label class="ac-label ac-scopes-label">Enabled Scopes</label>
    <span class="ac-scopes-hint">
      <a href="#" id="ac-int-${provider}-scopes-all" class="ac-scopes-link">all</a>
      &nbsp;/&nbsp;
      <a href="#" id="ac-int-${provider}-scopes-none" class="ac-scopes-link">none</a>
    </span>`;
  container.appendChild(header);

  const grid = document.createElement('div');
  grid.id = `ac-int-${provider}-scopes-grid`;
  grid.classList.add('ac-scopes-grid');

  scopes.forEach((scope, i) => {
    const row = document.createElement('label');
    row.classList.add('ac-scopes-row');
    row.title = scope.value;
    const cb = document.createElement('input');
    cb.type = 'checkbox';
    cb.id = `ac-scope-${provider}-${i}`;
    cb.value = scope.value;
    cb.checked = true;
    cb.classList.add('ac-scopes-cb');
    row.appendChild(cb);
    row.appendChild(document.createTextNode(scope.label));
    grid.appendChild(row);
  });
  container.appendChild(grid);

  // Insert before the save button row
  const saveBtn = document.getElementById(`ac-int-${provider}-save`);
  const saveRow = saveBtn ? saveBtn.parentElement : null;
  if (saveRow && saveRow.parentElement === formEl) {
    formEl.insertBefore(container, saveRow);
  } else {
    formEl.appendChild(container);
  }

  // "all" / "none" links
  document.getElementById(`ac-int-${provider}-scopes-all`)?.addEventListener('click', e => {
    e.preventDefault();
    grid.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = true);
  });
  document.getElementById(`ac-int-${provider}-scopes-none`)?.addEventListener('click', e => {
    e.preventDefault();
    grid.querySelectorAll('input[type=checkbox]').forEach(cb => cb.checked = false);
  });
}

function _setScopeSelection(provider, enabledScopes) {
  const grid = document.getElementById(`ac-int-${provider}-scopes-grid`);
  if (!grid) return;
  const checkboxes = grid.querySelectorAll('input[type=checkbox]');
  if (!enabledScopes || enabledScopes.length === 0) {
    checkboxes.forEach(cb => cb.checked = true);
    return;
  }
  const set = new Set(enabledScopes);
  checkboxes.forEach(cb => { cb.checked = set.has(cb.value); });
}

function _getSelectedScopes(provider) {
  const grid = document.getElementById(`ac-int-${provider}-scopes-grid`);
  if (!grid) return null;
  const checked = [...grid.querySelectorAll('input[type=checkbox]')]
    .filter(cb => cb.checked)
    .map(cb => cb.value);
  return checked;
}

// ── Agent Tools — compact ability rows ─────────────────────────────────────
//
// Each ability is rendered as a single row (icon + name + desc + toggle).
// Simple abilities (no config needed) toggle directly.
// Complex abilities (need credentials/config) show a warning when toggled
// and expand to reveal the config panel. The toggle only activates once
// all required fields are filled.

// ── Ability render data — FALLBACK ONLY ────────────────────────────────────
// ── Ability catalog ───────────────────────────────────────────────────────
// The admin-ability-table.js component fetches and caches the catalog for its
// own rendering. This host file keeps a lightweight copy of abilities + groups
// for _compactifyAllIntegrations / _placeIntegrationGroupCards (the transitional
// path that still uses HTML config cards placed into group slots). Once those
// HTML cards move into the component, this can be removed.
let _catAbilities = {};  // populated from catalog, used by _catNote

// Fetch the server-built ability catalog ONCE and populate the minimum needed
// for the transitional integration-card path. The UI component (admin-ability-
// table.js) fetches the catalog independently.
let _abilityCatalogPromise = null;
function _ensureAbilityCatalog() {
  if (_abilityCatalogPromise) return _abilityCatalogPromise;
  _abilityCatalogPromise = (async () => {
    try {
      const res = await fetch('/api/v1/abilities/catalog');
      if (!res.ok) return;
      const cat = await res.json();
      if (!cat || !Array.isArray(cat.groups) || !cat.groups.length) return;

      _catAbilities = cat.abilities || {};

      // Rebuild integration groups from catalog data (used by _placeIntegrationGroupCards).
      const intGroups = [];
      for (const g of (cat.groups || [])) {
        const members = [];
        const soonCards = [];
        for (const mid of (g.members || [])) {
          const ab = cat.abilities && cat.abilities[mid];
          if (!ab) continue;
          if (ab.placeholder || ab.kind === 'placeholder') {
            soonCards.push(mid);
          } else if (ab.kind === 'oauth' || ab.kind === 'credential' || ab.kind === 'channel') {
            members.push({ id: mid, kind: ab.kind });
          }
        }
        intGroups.push({
          id: g.id, name: g.name, icon: g.icon, color: g.color, desc: g.desc,
          members: members, soonCards: soonCards, soonRowsKey: null,
        });
      }
      _INTEGRATION_GROUPS.length = 0;
      _INTEGRATION_GROUPS.push(...intGroups);
    } catch (e) {
      console.warn('Ability catalog fetch failed', e);
    }
  })();
  return _abilityCatalogPromise;
}

// ── Catalog-backed note lookup (used by _compactifyCard) ─────────────────────
function _catNote(id) {
  return (_catAbilities && _catAbilities[id] && _catAbilities[id].note) || '';
}

let _abilityStates = {}; // { [ability]: boolean }
let _adminTableHandle = null;  // returned by AdminAbilityTable.build()

// When the admin toggles an ability, we stamp the moment of that action here.
// `_loadIntegrations()` runs on every settings/admin-tools (re)activation and
// force-applies a fetched snapshot onto every toggle. That fetch is slow (it
// makes ~30 sequential DB lookups), so if the admin toggles an ability while a
// load is still in flight, the load would resolve with its PRE-toggle snapshot
// and silently revert the just-made change — the backend keeps the new value
// (so the agent's Abilities page is correct) but the admin toggle flips back,
// which reads as "it didn't save". We guard against that by skipping, in the
// load's apply step, any ability the admin changed after that load began.
let _abilityLastActionAt = {}; // { [ability]: DOMHighResTimeStamp }

function _markAbilityAction(ability) {
  _abilityLastActionAt[ability] = (typeof performance !== 'undefined' ? performance.now() : Date.now());
}

// Build one always-visible ability row (icon + name + desc + toggle + delete).
// Used as a member row inside a group body. The delete button uses a long-press
// (hold 1.5s) to prevent accidental deletion — it fills a progress ring then
// fires the delete API on completion.

// (Web Scraper + Browser Cookies are now ordinary drop-in abilities — their
// credential forms render inline in the Web group of the shared ability table,
// so the old _placeWebCredentialRows relocation is gone.)


function _applyAbilityRowStatus(ability, enabled) {
  _abilityStates[ability] = enabled;
  const toggle = _qs(`ac-ability-toggle-${ability}`);
  if (toggle) toggle.checked = !!enabled;
  const row = _qs(`ac-ability-row-${ability}`);
  if (row) row.classList.toggle('ac-ability-enabled', !!enabled);
}

function _initCollapsible(provider) {
  const card = document.getElementById(`ac-int-${provider}-card`);
  if (!card) return;
  const header = card.querySelector(':scope > div');
  if (!header) return;

  // Wrap everything after the header in a collapsible body div
  const body = document.createElement('div');
  body.id = `ac-int-${provider}-body`;
  body.classList.add('ac-hidden');
  [...card.children].slice(1).forEach(el => body.appendChild(el));
  card.appendChild(body);

  // Remove header bottom margin (restored when expanded)
  header.classList.add('ac-accordion-header');

  // Chevron icon — appended after the badge
  const chevron = document.createElement('span');
  chevron.id = `ac-int-${provider}-chevron`;
  chevron.className = 'ac-accordion-chevron';
  chevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;
  header.appendChild(chevron);

  header.addEventListener('click', e => {
    if (e.target.closest('button, a, input')) return;
    _toggleCard(provider);
  });
}

function _toggleCard(provider, forceOpen) {
  const body = document.getElementById(`ac-int-${provider}-body`);
  const chevron = document.getElementById(`ac-int-${provider}-chevron`);
  const card = document.getElementById(`ac-int-${provider}-card`);
  const header = card?.querySelector(':scope > div');
  if (!body) return;
  const isOpen = !body.classList.contains('ac-hidden');
  const open = forceOpen !== undefined ? forceOpen : !isOpen;
  body.classList.toggle('ac-hidden', !open);
  if (chevron) chevron.classList.toggle('ac-rotated', open);
  if (header) header.classList.toggle('ac-accordion-open', open);
}

function _expandCard(provider) {
  _toggleCard(provider, true);
}

// ── Unified compact rows for every integration / channel / generic ─────────
// Goal: the rest of the page looks like the Agent Tools table — one slim row
// (icon · name · requirement note · toggle · chevron) that expands to the
// existing config form. We reuse each card's collapsible body (built by
// _initCollapsible) and its existing save/OAuth/unconfigure wiring; only the
// row chrome + the requirement note are layered on top.

const _OAUTH_PROVIDER_IDS = ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon'];
const _GENERIC_PROVIDER_IDS = [];  // Web Scraper/Browser Cookies are now drop-in abilities
const _CHANNEL_IDS = ['telegram'];
const _SOON_CHANNEL_IDS = ['whatsapp', 'slack', 'discord', 'email', 'twilio'];

// One-to-1.5-sentence requirement note per row (cost / OAuth / special setup).
// Coming-soon integrations that have no config card — rendered as greyed rows
// into the per-category containers added in the section HTML.


// ── Former settings "categories" → expandable groups in the ONE Agent Tools
// table ──────────────────────────────────────────────────────────────────────
// Each entry renders as an `.ac-row.ac-group` appended into #ac-abilities-compact
// right after the host-ability groups (Administrator / Core / Web), so the whole
// tab reads as a single flush table instead of a stack of collapsible category
// sections. Members are existing integration cards (relocated into the group body
// at runtime by _placeIntegrationGroupCards) and/or coming-soon placeholder rows.
//
// The group's 3-position toggle (Off · Mixed · On) reflects and bulk-controls
// ONLY the available members — coming-soon rows never count, and a group with no
// available members yet (Payments / Developer / CRM) shows no toggle at all, just
// an expand chevron. Turning a group off disables/unconfigures its members;
// turning it on can only enable members that need no credentials (a channel) —
// an OAuth app still needs its keys entered on its own row.
const _INTEGRATION_GROUPS = [];  // populated from catalog at runtime

function _integrationGroupOf(id) {
  return _INTEGRATION_GROUPS.find(g => g.members.some(m => m.id === id));
}

// Build the empty group shells (head + body with a slot per card member) and
// append them to the single Agent Tools table, after the host-ability groups.
// The real cards are relocated into the slots later by _placeIntegrationGroupCards.
function _placeIntegrationGroupCards() {
  for (const group of _INTEGRATION_GROUPS) {
    const body = document.getElementById(`ac-group-body-${group.id}`);
    if (!body) continue;
    // Available member cards → their slots, in order.
    for (const m of group.members) {
      const slot = document.getElementById(`ac-group-slot-${m.id}`);
      const card = document.getElementById(`ac-int-${m.id}-card`);
      if (slot && card) slot.replaceWith(card);
    }
    // Coming-soon channel cards (real cards, compactified as 'soon' rows).
    for (const sid of (group.soonCards || [])) {
      const card = document.getElementById(`ac-int-${sid}-card`);
      if (card) body.appendChild(card);
    }
    // Coming-soon rows are now rendered by admin-ability-table.js from the catalog.
  }
  // Hide the emptied legacy category sections (their cards now live in groups).
  for (const cid of ['ac-cat-channels', 'ac-cat-productivity', 'ac-cat-social', 'ac-cat-marketplace', 'ac-cat-payments', 'ac-cat-developer', 'ac-cat-crm']) {
    const cat = document.getElementById(cid);
    if (cat) cat.classList.add('ac-hidden');
  }
}

// ── Integration-group 3-position toggle: reflect state + bulk on/off ─────────
// Only AVAILABLE members count (coming-soon never does). "On" = configured /
// enabled (the row's badge carries ac-int-badge-on, read by _rowConfigured).
function _compactifyCard(id, kind) {
  const card = _qs(`ac-int-${id}-card`);
  if (!card || card.dataset.compactified) return;
  if (!_qs(`ac-int-${id}-body`)) { try { _initCollapsible(id); } catch (_) {} }
  card.classList.add('ac-card-compact');

  const header = card.querySelector(':scope > div');
  const soon = (kind === 'soon');
  const note = _catNote(id) || '';

  // Clone the card's existing brand icon (keeps Google's multicolour mark etc.)
  const iconWrap = document.createElement('div');
  iconWrap.className = 'ac-ability-icon';
  const srcIcon = header ? header.querySelector('svg, i[data-lucide]') : null;
  if (srcIcon) {
    const ic = srcIcon.cloneNode(true);
    ic.setAttribute('width', '18'); ic.setAttribute('height', '18');
    ic.classList.add('ac-ico-18');
    iconWrap.appendChild(ic);
  }

  let name = id;
  if (header) {
    const nameNode = header.querySelector('div[style*="font-weight:600"]');
    if (nameNode) name = nameNode.textContent.trim();
  }

  const label = document.createElement('div');
  label.className = 'ac-ability-label';
  label.innerHTML = `<div class="ac-ability-name"></div><div class="ac-ability-desc"></div>`;
  label.querySelector('.ac-ability-name').textContent = name;
  label.querySelector('.ac-ability-desc').textContent = note;

  const row = document.createElement('div');
  row.className = 'ac-ability-row ac-int-row' + (soon ? ' ac-soon-row' : '');
  row.id = `ac-int-row-${id}`;
  row.appendChild(iconWrap);
  row.appendChild(label);

  if (soon) {
    const pill = document.createElement('span');
    pill.className = 'ac-soon-pill';
    pill.textContent = 'Coming soon';
    row.appendChild(pill);
    card.insertBefore(row, card.firstChild);
    for (const ch of [...card.children]) { if (ch !== row) ch.classList.add('ac-hidden'); }
    card.dataset.compactified = '1';
    return;
  }

  const toggleWrap = document.createElement('label');
  toggleWrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  toggleWrap.title = 'Enable';
  const toggle = document.createElement('input');
  toggle.type = 'checkbox';
  toggle.className = 'conn-toggle ac-int-row-toggle';
  toggle.id = `ac-int-row-toggle-${id}`;
  const track = document.createElement('span');
  track.className = 'conn-toggle-track';
  toggleWrap.appendChild(toggle);
  toggleWrap.appendChild(track);

  const chevron = document.createElement('span');
  chevron.className = 'ac-int-row-chevron';
  chevron.innerHTML = `<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><polyline points="9 18 15 12 9 6"/></svg>`;

  row.appendChild(toggleWrap);
  row.appendChild(chevron);
  if (header) header.classList.add('ac-hidden');
  card.insertBefore(row, card.firstChild);
  card.dataset.compactified = '1';

  // Row click (away from the toggle) expands / collapses the config body.
  row.addEventListener('click', (e) => {
    if (e.target.closest('.ac-ability-toggle-wrap')) return;
    const body = _qs(`ac-int-${id}-body`);
    const open = body && !body.classList.contains('ac-hidden');
    _toggleCard(id, !open);
    chevron.classList.toggle('ac-rotated', !open);
  });

  // Toggle semantics: channels enable/disable directly; OAuth/generic providers
  // expand the form when turned on unconfigured, and unconfigure when turned off.
  toggle.addEventListener('change', () => {
    const on = toggle.checked;
    // The save overlay sits on the row's toggle switch.
    if (kind === 'channel') {
      if (on) _enableChannel(id, toggleWrap); else _disableChannel(id, toggleWrap);
      return;
    }
    if (on) {
      if (!_rowConfigured(id)) {
        toggle.checked = false;
        _toggleCard(id, true);
        chevron.classList.add('ac-rotated');
      }
    } else if (_rowConfigured(id)) {
      if (!confirm(`Disable ${name}? This removes the saved app credentials.`)) {
        toggle.checked = true;
        return;
      }
      _unconfigureProvider(id, toggleWrap);
    }
  });
}

function _compactifyAllIntegrations() {
  for (const p of _OAUTH_PROVIDER_IDS) _compactifyCard(p, 'oauth');
  for (const p of _GENERIC_PROVIDER_IDS) _compactifyCard(p, 'generic');
  for (const c of _CHANNEL_IDS) _compactifyCard(c, 'channel');
  for (const c of _SOON_CHANNEL_IDS) _compactifyCard(c, 'soon');
  _placeIntegrationGroupCards();    // channels/providers into their groups; hide legacy categories
  if (typeof lucide !== 'undefined' && lucide.createIcons) lucide.createIcons();
}

function _rowConfigured(id) {
  const badge = _qs(`ac-int-${id}-badge`);
  return badge && badge.classList.contains('ac-int-badge-on');
}

function _syncRowToggle(id) {
  const toggle = _qs(`ac-int-row-toggle-${id}`);
  if (!toggle) return;
  const on = _rowConfigured(id);
  toggle.checked = on;
  const row = _qs(`ac-int-row-${id}`);
  if (row) row.classList.toggle('ac-ability-enabled', on);
  // Keep the parent group's header tri in step with this row's configured state.
  const grp = _integrationGroupOf(id);
  if (grp) _syncIntegrationGroupTri(grp);
}

function _syncAllRowToggles() {
  for (const id of [..._OAUTH_PROVIDER_IDS, ..._GENERIC_PROVIDER_IDS, ..._CHANNEL_IDS]) {
    _syncRowToggle(id);
  }
}

// ── Integration-group status card (reflect configured-count) ────────────────
// The shared admin-ability-table.js builds a neutral "—" status card for groups
// that hold no on/off "app abilities" — which is every credential-only group
// (Productivity, Social, Marketplace, …): their members are OAuth/credential
// providers and channels, which that component deliberately ignores. Here we
// paint each such card with how many of the group's providers/channels are
// configured (Off · Mixed · On → Disabled · Partial · Enabled). The group tri's
// bulk on/off behaviour was removed with the tri-toggle itself, so these cards
// are display-only. (The provider/channel state + actions all live in this file
// — `_rowConfigured`, `_unconfigureProvider`, `_enableChannel`, `_disableChannel`
// — so this stays an admin-page concern and the shared sister-panel component is
// untouched.)

// The configurable members of one integration group ([{id, kind}], coming-soon
// rows already excluded when _INTEGRATION_GROUPS is built from the catalog).
function _intGroupMembers(group) {
  return group.members || [];
}

// Paint one group's status card from how many of its members are configured.
function _syncIntegrationGroupTri(group) {
  const status = _qs(`ac-group-status-${group.id}`);
  if (!status) return;
  const members = _intGroupMembers(group);
  if (!members.length) return;
  const onCount = members.filter(m => _rowConfigured(m.id)).length;
  status.dataset.state = onCount === 0 ? 'off' : (onCount === members.length ? 'on' : 'mixed');
  _paintGroupStatus(status);
}

function _syncAllIntegrationGroupTris() {
  for (const g of _INTEGRATION_GROUPS) _syncIntegrationGroupTri(g);
}

// Adopt the shared component's neutral status card for each credential-only
// group: the card is read-only (bulk on/off was removed with the group tri), so
// this just reflects each group's configured-count onto it.
function _wireIntegrationGroupTris() {
  for (const group of _INTEGRATION_GROUPS) {
    if (!_intGroupMembers(group).length) continue;
    _syncIntegrationGroupTri(group);
  }
}

async function _initIntegrations() {
  // Load the drop-in ability catalog before any ability UI is built so the
  // panel reflects whatever lives in plugins/abilities/ (not the JS fallback).
  await _ensureAbilityCatalog();
  const providers = ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon'];
  for (const p of providers) {
    _initCollapsible(p);
    _renderScopeCheckboxes(p);
    _qs(`ac-int-${p}-save`)?.addEventListener('click', () => _saveProviderConfig(p));
    _qs(`ac-int-${p}-edit`)?.addEventListener('click', () => _editProviderConfig(p));
    _qs(`ac-int-${p}-unconfigure`)?.addEventListener('click', (e) => _unconfigureProvider(p, e.currentTarget));
  }
  // (Web Scraper + Browser Cookies are drop-in abilities now; their credential
  // forms are wired by the shared ability table, not here.)

  // Channels (admin enable/disable; per-agent creds live in the agent's Abilities tab)
  const channels = ['telegram'];
  for (const c of channels) {
    _initCollapsible(c);
    _qs(`ac-int-${c}-save`)?.addEventListener('click', (e) => _enableChannel(c, e.currentTarget));
    _qs(`ac-int-${c}-unconfigure`)?.addEventListener('click', (e) => _disableChannel(c, e.currentTarget));
  }
  // Coming-soon channel placeholders — searchable but not interactive.
  const comingSoonChannels = ['whatsapp', 'slack', 'discord', 'email', 'twilio'];

  // Complex ability config panels (image_generation, automation, visualizer)
  // have save/unconfigure buttons in #ac-ability-config-panels. Wire them.
  const complexAbilities = ['image_generation', 'automation', 'visualizer'];
  for (const a of complexAbilities) {
    _qs(`ac-int-${a}-save`)?.addEventListener('click', (e) => _enableAbility(a, e.currentTarget));
    _qs(`ac-int-${a}-unconfigure`)?.addEventListener('click', (e) => _disableAbility(a, e.currentTarget));
  }

  // Agent Tools — rendered by the shared admin-ability-table.js component.
  // See SISTER-PANEL: AGENT-ABILITY-TABLE in agent-ability-table.js.
  const container = _qs('ac-abilities-compact');
  if (container) {
    _adminTableHandle = await buildAdminAbilityTable(container, {
      abilityStates: _abilityStates,
      integrationStates: {},
      callbacks: {
        onEnableChannel: _enableChannel,
        onDisableChannel: _disableChannel,
        onUnconfigureProvider: _unconfigureProvider,
      },
      skipIntegrationGroups: true,  // integration groups still use _compactifyCard for now
      // The Agent Tools page is now a full page of its own; the shared hybrid
      // search/chat pill mounts inline above the table (the page-level search is
      // the unified pill at the container bottom).
      showSearch: true,
    });
    // Build integration group shells into the same .ac-list grid so
    // _compactifyAllIntegrations + _placeIntegrationGroupCards can relocate
    // the compactified cards into them.
    const grid = container.querySelector('.ac-list');
  }

  // Mount the Agent Tools table's own inline search/chat pill (built by the
  // shared component with showSearch above); the page-level search lives in the
  // unified pill at the container bottom.

  // ── Init collapsible for the remaining integration provider cards.
  // These are still in the section HTML and compactified by
  // _compactifyAllIntegrations below. Once they move into the shared table
  // this init + compactify step can be removed.

  // Unify every OAuth / channel / generic card into the same compact-row look,
  // and render the card-less coming-soon integrations as greyed rows.
  _compactifyAllIntegrations();
  // Adopt the shared component's otherwise-dead group tris so a credential-only
  // group (Productivity, Social, …) reflects + bulk-controls its providers.
  _wireIntegrationGroupTris();

  _initIntegrationsSearch([...providers, ...channels, ...comingSoonChannels]);
  _initIntegrationAdminChat();
}

// ── Integration Admin chat ───────────────────────────────────────────────
function _intAdminStorageKey() {
  const uid = app.currentUserId || 'anon';
  return `intAdminChat_session_${uid}`;
}

function _intAdminGetSession() {
  if (_intAdminSessionId) return _intAdminSessionId;
  try {
    const stored = localStorage.getItem(_intAdminStorageKey());
    if (stored) { _intAdminSessionId = stored; return stored; }
  } catch (_) {}
  const fresh = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : ('s-' + Date.now() + '-' + Math.random().toString(36).slice(2));
  _intAdminSessionId = fresh;
  try { localStorage.setItem(_intAdminStorageKey(), fresh); } catch (_) {}
  return fresh;
}

// Each visit to the Agent Settings section starts a brand-new WebAgent session,
// so the chat there is a fresh conversation (matching the page chat pills).
// Clears the cached session id + the inline transcript.
function _intAdminResetSession() {
  _intAdminSessionId = null;
  try { localStorage.removeItem(_intAdminStorageKey()); } catch (_) {}
  const box = _qs('ac-int-admin-chat-messages');
  if (box) { box.innerHTML = ''; box.classList.add('ac-hidden'); }
}

function _intAdminAppendBubble(role, text) {
  const box = _qs('ac-int-admin-chat-messages');
  if (!box) return null;
  box.classList.remove('ac-hidden');
  const bubble = document.createElement('div');
  bubble.classList.add('ac-detail-bubble')
    + (role === 'user'
       ? 'background:var(--bg-2);align-self:flex-end;'
       : 'background:var(--bg-1);border:1px solid var(--border);');
  bubble.dataset.role = role;
  bubble.textContent = text || '';
  box.appendChild(bubble);
  box.scrollTop = box.scrollHeight;
  return bubble;
}

function _intAdminShowError(msg) {
  const err = _qs('ac-int-admin-chat-error');
  if (!err) return;
  err.textContent = msg || '';
  err.classList.toggle('ac-hidden', !msg);
}

function _intAdminSetBusy(busy) {
  const input = _qs('ac-int-admin-chat-input');
  const send = _qs('ac-int-admin-chat-send');
  if (input) input.disabled = busy;
  if (send) send.disabled = busy || !(input && input.value.trim());
}

// Integration Admin chat has its own send flow (separate template, no main
// chat forwarding), so it keeps its own preview bar and pending-attachments
// list rather than sharing the main chat's.
let _intAdminSessionId = null;
let _intAdminAbort = null;
let _intAdminWired = false;
const _intAdminPending = [];
function _intAdminClearPending() {
  for (const entry of _intAdminPending) {
    if (entry._objectUrl) URL.revokeObjectURL(entry._objectUrl);
  }
  _intAdminPending.length = 0;
  const bar = _qs('ac-int-admin-preview-bar');
  if (bar) { bar.innerHTML = ''; bar.classList.add('ac-hidden'); }
}

async function _intAdminSend() {
  const input = _qs('ac-int-admin-chat-input');
  if (!input) return;
  const text = (input.value || '').trim();
  if (!text) return;
  if (!app.currentUserId) {
    _intAdminShowError('Sign in to use the Integration Admin chat.');
    return;
  }
  _intAdminShowError('');

  // ═══════════════════════════════════════════════════════════════════════
  // PROMPT / PRETEXT NOTE:
  // The message sent to the agent from this Integration Admin chat pill is
  // loaded from app/defaults/app-prompts.json → ui_handoffs.admin_integrations_chat.
  // To change the tag format, edit that JSON file — NOT the fallback below.
  // ═══════════════════════════════════════════════════════════════════════

  // Try to load tag template from app-prompts.json, fall back to raw text
  let message = text;
  try {
    const resp = await fetch(apiPath('/api/v1/app-prompts'));
    if (resp.ok) {
      const data = await resp.json();
      const tpl = (data.ui_handoffs || {}).admin_integrations_chat?.template || '';
      if (tpl && tpl !== '{text}') {
        message = tpl.replace(/\{text\}/g, text);
      }
    }
  } catch (_) { /* fall through — use raw text */ }

  // This chat targets the shared WebAgent (the `default` template), same as the
  // Agents / Gen UI / Git Control pills. Resolve (or create) that agent first.
  let agentId;
  try {
    agentId = await app.ensureWebagentAgent(app.currentUserId);
  } catch (e) {
    _intAdminShowError('Could not start WebAgent: ' + (e.message || e));
    return;
  }

  input.value = '';
  _intAdminSetBusy(true);
  _intAdminAppendBubble('user', text);
  const agentBubble = _intAdminAppendBubble('agent', '…');
  let agentBuffer = '';

  const payload = {
    message: message,
    session_id: _intAdminGetSession(),
    user_id: app.currentUserId,
    agent_id: agentId,
  };
  // Upload any locally-pending files before attaching their ids.
  if (_intAdminPending.length > 0) {
    const ids = await uploadPendingAttachments(_intAdminPending);
    payload.attachment_ids = ids;
    _intAdminClearPending();
  }

  if (_intAdminAbort) { try { _intAdminAbort.abort(); } catch (_) {} }
  _intAdminAbort = new AbortController();

  try {
    const resp = await _fetch(apiPath('/api/v1/chat/stream'), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
      signal: _intAdminAbort.signal,
    });
    if (!resp.ok) {
      const detail = resp.status === 403
        ? 'Admin access required.'
        : `Server error: ${resp.status}`;
      if (agentBubble) agentBubble.textContent = detail;
      _intAdminShowError(detail);
      _intAdminSetBusy(false);
      return;
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop() || '';
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        let event;
        try { event = JSON.parse(line.slice(6)); } catch { continue; }
        if (event.type === 'stream') {
          agentBuffer += (event.content || '');
          if (agentBubble) {
            agentBubble.textContent = agentBuffer;
            const box = _qs('ac-int-admin-chat-messages');
            if (box) box.scrollTop = box.scrollHeight;
          }
        } else if (event.type === 'response') {
          agentBuffer = '';
          if (agentBubble) agentBubble.textContent = event.content || '';
        } else if (event.type === 'error') {
          _intAdminShowError(event.message || 'Unknown error');
          if (agentBubble) agentBubble.textContent = 'Error: ' + (event.message || '');
        }
      }
    }
  } catch (e) {
    if (e.name !== 'AbortError') {
      _intAdminShowError('Request failed: ' + e.message);
      if (agentBubble) agentBubble.textContent = 'Error: ' + e.message;
    }
  } finally {
    _intAdminSetBusy(false);
    _intAdminAbort = null;
  }
}

function _initIntegrationAdminChat() {
  if (_intAdminWired) return;
  const input = _qs('ac-int-admin-chat-input');
  const send = _qs('ac-int-admin-chat-send');
  if (!input || !send) return;
  const sync = () => { send.disabled = !input.value.trim(); };
  send.addEventListener('click', () => { _intAdminSend(); });
  input.addEventListener('input', sync);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      _intAdminSend();
    }
  });

  const row = _qs('ac-int-admin-chat-input-row');
  const bar = _qs('ac-int-admin-preview-bar');
  if (row && bar) {
    wireChatPillUploads(row, input, {
      previewBar: bar,
      pending: _intAdminPending,
      onChange: (pending) => {
        bar.classList.toggle('ac-hidden', pending.length === 0);
      },
    });
  }

  sync();
  _intAdminWired = true;
}

function _initIntegrationsSearch(providers) {
  const input = _qs('ac-int-filter');
  if (!input) return;
  input.value = '';
  const emptyEl = _qs('ac-int-filter-empty');
  const cards = providers
    .map(p => {
      const card = document.getElementById(`ac-int-${p}-card`);
      if (!card) return null;
      const header = card.querySelector(':scope > div');
      const haystack = (p + ' ' + (header ? header.textContent : '')).toLowerCase();
      return { card, haystack };
    })
    .filter(Boolean);

  // Also include compact ability rows in search (member rows only — never the
  // group head rows, which always stay visible as section labels).
  const compactContainer = _qs('ac-abilities-compact');
  const compactRows = compactContainer
    ? [...compactContainer.querySelectorAll('.ac-ability-row:not(.ac-group-head):not(.ac-int-row)')]
    : [];
  const groupEls = compactContainer
    ? [...compactContainer.querySelectorAll('.ac-group')]
    : [];

  const apply = () => {
    const q = input.value.trim().toLowerCase();
    let visible = 0;

    // While searching, expand every group so member rows are reachable; collapse
    // them all when the query is cleared.
    for (const g of groupEls) g.classList.toggle('expanded', !!q);

    // Track which categories have visible items
    const catVisibility = {};

    for (const { card, haystack } of cards) {
      const match = !q || haystack.includes(q);
      card.classList.toggle('ac-hidden', !match);
      if (match) {
        visible++;
        // Mark parent category
        const cat = card.closest('.ac-category-group');
        if (cat) catVisibility[cat.id] = true;
      }
    }
    // Filter compact rows
    for (const row of compactRows) {
      const name = row.querySelector('.ac-ability-name')?.textContent || '';
      const desc = row.querySelector('.ac-ability-desc')?.textContent || '';
      const haystack = (name + ' ' + desc).toLowerCase();
      const match = !q || haystack.includes(q);
      row.classList.toggle('ac-hidden', !match);
      if (match) {
        visible++;
        // Parent is the agent-tools category
        const cat = _qs('ac-cat-agent-tools');
        if (cat) catVisibility[cat.id] = true;
      }
    }
    // Also filter config panels
    const configPanels = _qs('ac-ability-config-panels');
    if (configPanels) {
      const panels = configPanels.querySelectorAll('.ac-card');
      for (const panel of panels) {
        const text = (panel.textContent || '').toLowerCase();
        const match = !q || text.includes(q);
        panel.classList.toggle('ac-hidden', !match);
        if (match) {
          visible++;
          const cat = _qs('ac-cat-agent-tools');
          if (cat) catVisibility[cat.id] = true;
        }
      }
    }

    // Hide a group entirely when none of its member rows/cards survive the
    // filter, so a search doesn't leave empty expanded groups behind.
    for (const g of groupEls) {
      if (!q) { g.classList.remove('ac-hidden'); continue; }
      const body = g.querySelector('.ac-group-body');
      const anyVisible = body && [...body.children].some(el => el.style.display !== 'none');
      g.classList.toggle('ac-hidden', !anyVisible);
    }

    // When searching, auto-open categories with matches; when cleared, close all.
    // Collapse is class-driven now (`.ac-open`) — the category groups are plain
    // <div>s under the sticky-nav flatten (display:contents broke <details>), so
    // the old native `.open` attribute no longer controls them.
    for (const cat of document.querySelectorAll('.ac-category-group')) {
      if (q) {
        cat.classList.toggle('ac-open', !!catVisibility[cat.id]);
      } else {
        cat.classList.remove('ac-open');
      }
    }

    if (emptyEl) emptyEl.classList.toggle('ac-hidden', !(q && visible === 0));
  };
  input.addEventListener('input', apply);
}

async function _loadIntegrations() {
  // Stamp the instant this load begins, BEFORE the (slow) fetch. Any ability the
  // admin toggles after this point must not be clobbered by this load's stale
  // snapshot — see `_abilityLastActionAt` above.
  const _loadStart = (typeof performance !== 'undefined' ? performance.now() : Date.now());
  const _applyAbilityFromLoad = (ability, enabled) => {
    if ((_abilityLastActionAt[ability] || 0) > _loadStart) return; // user changed it mid-load
    _applyAbilityStatus(ability, enabled);
  };
  try {
    const res = await _fetch(apiPath('/admin/integrations'));
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _applyProviderStatus('google',    data.google_configured,    data.google_client_id,    data.redirect_uri,              data.google_scopes,    data.redirect_uri_suggested);
    _applyProviderStatus('microsoft', data.microsoft_configured, data.microsoft_client_id, data.microsoft_redirect_uri,    data.microsoft_scopes, data.microsoft_redirect_uri_suggested);
    _applyProviderStatus('yahoo',     data.yahoo_configured,     data.yahoo_client_id,     data.yahoo_redirect_uri,        data.yahoo_scopes,     data.yahoo_redirect_uri_suggested);
    _applyProviderStatus('dropbox',   data.dropbox_configured,   data.dropbox_client_id,   data.dropbox_redirect_uri,      data.dropbox_scopes,   data.dropbox_redirect_uri_suggested);
    _applyProviderStatus('meta',      data.meta_configured,      data.meta_client_id,      data.meta_redirect_uri,         data.meta_scopes,      data.meta_redirect_uri_suggested);
    _applyProviderStatus('twitter',   data.twitter_configured,   data.twitter_client_id,   data.twitter_redirect_uri,      data.twitter_scopes,   data.twitter_redirect_uri_suggested);
    _applyProviderStatus('linkedin',  data.linkedin_configured,  data.linkedin_client_id,  data.linkedin_redirect_uri,     data.linkedin_scopes,  data.linkedin_redirect_uri_suggested);
    _applyProviderStatus('tiktok',    data.tiktok_configured,    data.tiktok_client_id,    data.tiktok_redirect_uri,       data.tiktok_scopes,    data.tiktok_redirect_uri_suggested);
    _applyProviderStatus('pinterest', data.pinterest_configured, data.pinterest_client_id, data.pinterest_redirect_uri,    data.pinterest_scopes, data.pinterest_redirect_uri_suggested);
    _applyProviderStatus('reddit',    data.reddit_configured,    data.reddit_client_id,    data.reddit_redirect_uri,       data.reddit_scopes,    data.reddit_redirect_uri_suggested);
    _applyProviderStatus('snapchat',  data.snapchat_configured,  data.snapchat_client_id,  data.snapchat_redirect_uri,     data.snapchat_scopes,  data.snapchat_redirect_uri_suggested);
    _applyProviderStatus('twitch',    data.twitch_configured,    data.twitch_client_id,    data.twitch_redirect_uri,       data.twitch_scopes,    data.twitch_redirect_uri_suggested);
    _applyProviderStatus('ebay',      data.ebay_configured,      data.ebay_client_id,      data.ebay_redirect_uri,         data.ebay_scopes,      data.ebay_redirect_uri_suggested);
    _applyProviderStatus('etsy',      data.etsy_configured,      data.etsy_client_id,      data.etsy_redirect_uri,         data.etsy_scopes,      data.etsy_redirect_uri_suggested);
    _applyProviderStatus('shopify',   data.shopify_configured,   data.shopify_client_id,   data.shopify_redirect_uri,      data.shopify_scopes,   data.shopify_redirect_uri_suggested);
    _applyProviderStatus('amazon',    data.amazon_configured,    data.amazon_client_id,    data.amazon_redirect_uri,       data.amazon_scopes,    data.amazon_redirect_uri_suggested);
    _applyChannelStatus('telegram', data.telegram_configured);
    // Agent Tools — generic, catalog-driven. The backend returns an `abilities`
    // map {id: enabled} built from every kind="ability" drop-in, so a newly
    // dropped-in ability reflects its saved toggle here with NO per-ability
    // wiring. (Falls back to the legacy <id>_configured flags if the map is
    // absent, e.g. an older backend.)
    if (data.abilities && typeof data.abilities === 'object') {
      for (const [aid, enabled] of Object.entries(data.abilities)) {
        _applyAbilityFromLoad(aid, !!enabled);
      }
    } else {
      _applyAbilityFromLoad('codebase_admin',   data.codebase_admin_configured);
      _applyAbilityFromLoad('git_control',      data.git_control_configured);
      _applyAbilityFromLoad('ui_admin',         data.ui_admin_configured);
      _applyAbilityFromLoad('terminal_control', data.terminal_control_configured);
      _applyAbilityFromLoad('create_tools',     data.create_tools_configured);
      _applyAbilityFromLoad('automation',       data.automation_configured);
      _applyAbilityFromLoad('web_access',       data.web_access_configured);
      _applyAbilityFromLoad('browser_control',  data.browser_control_configured);
      _applyAbilityFromLoad('image_generation', data.image_generation_configured);
      _applyAbilityFromLoad('visualizer',       data.visualizer_configured);
      _applyAbilityFromLoad('agent_orchestration', data.agent_orchestration_configured);
      _applyAbilityFromLoad('diagnostics',         data.diagnostics_configured);
      _applyAbilityFromLoad('agent_management',    data.agent_management_configured);
      _applyAbilityFromLoad('app_control',         data.app_control_configured);
      _applyAbilityFromLoad('wiki_control',        data.wiki_control_configured);
      _applyAbilityFromLoad('image_vision',        data.image_vision_configured);
    }
    // Reflect each provider/channel's configured state onto its unified row toggle.
    _syncAllRowToggles();
    // Reflect ability states onto each group's 3-position toggle
    // (the admin-ability-table.js component manages these now).
    if (_adminTableHandle && _adminTableHandle.syncAllTriToggles) {
      _adminTableHandle.syncAllTriToggles();
    }
    // …then re-sync the credential-only group tris LAST so they win: the shared
    // syncAllTriToggles above counts a group's placeholder rows (which carry
    // data-ability) and would otherwise force these groups back to "off".
    _syncAllIntegrationGroupTris();
  } catch (e) {
    for (const p of ['google', 'microsoft', 'yahoo', 'dropbox', 'meta', 'twitter', 'linkedin', 'tiktok', 'pinterest', 'reddit', 'snapchat', 'twitch', 'ebay', 'etsy', 'shopify', 'amazon', 'telegram', 'codebase_admin', 'create_tools', 'automation', 'web_access', 'browser_control', 'image_generation', 'visualizer', 'agent_orchestration', 'diagnostics', 'agent_management', 'app_control', 'wiki_control', 'image_vision']) {
      const s = _qs(`ac-int-${p}-status`);
      _setIntStatus(s, `Failed to load: ${e.message}`);
    }
  }
}

function _applyChannelStatus(channel, enabled) {
  const badge        = _qs(`ac-int-${channel}-badge`);
  const configuredEl = _qs(`ac-int-${channel}-configured`);
  const form         = _qs(`ac-int-${channel}-form`);
  if (enabled) {
    if (badge) { badge.textContent = 'Enabled'; badge.className = 'ac-int-badge ac-int-badge-on'; }
    if (configuredEl) configuredEl.classList.remove('ac-hidden');
    if (form) form.classList.add('ac-hidden');
  } else {
    if (badge) { badge.textContent = 'Disabled'; badge.className = 'ac-int-badge ac-int-badge-off'; }
    if (configuredEl) configuredEl.classList.add('ac-hidden');
    if (form) form.classList.remove('ac-hidden');
  }
  _syncRowToggle(channel);
}

// `ctrl` (optional) is the control the user touched — the row toggle wrap or the
// in-card Enable/Disable button — so the unified spinner → ✓ / ⚠ overlay sits on
// it. Returns true on success / false on failure so the group-tri bulk path can
// reflect the outcome. On success the visible badge/form transition is the main
// confirmation; on failure the inline status text carries the detail too.
async function _enableChannel(channel, ctrl) {
  const statusEl = _qs(`ac-int-${channel}-status`);
  if (statusEl) statusEl.classList.add('ac-hidden');
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/channels/${channel}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _applyChannelStatus(channel, true);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

async function _disableChannel(channel, ctrl) {
  const statusEl = _qs(`ac-int-${channel}-status`);
  if (statusEl) statusEl.classList.add('ac-hidden');
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/channels/${channel}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _applyChannelStatus(channel, false);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

// ── Agent Tools (admin enable/disable; per-agent picks in Abilities tab) ──
function _applyAbilityStatus(ability, enabled) {
  // Same DOM contract as channel cards (badge / configured / form).
  _applyChannelStatus(ability, enabled);
  // Also update the compact row if it exists
  _applyAbilityRowStatus(ability, enabled);
  // Hide the config panel when disabled
  if (!enabled) {
    const card = _qs(`ac-int-${ability}-card`);
    if (card) card.classList.add('ac-hidden');
  }
}

async function _enableAbility(ability, ctrl) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (statusEl) statusEl.classList.add('ac-hidden');
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'POST' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _markAbilityAction(ability);
    _applyAbilityStatus(ability, true);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

async function _disableAbility(ability, ctrl) {
  const statusEl = _qs(`ac-int-${ability}-status`);
  if (ability === 'automation') {
    const ok = confirm(
      'Disable Automation?\n\n'
      + 'This will permanently delete every agent\'s scheduled tasks and event subscriptions. '
      + 'This cannot be undone.'
    );
    if (!ok) return false;
  }
  if (statusEl) statusEl.classList.add('ac-hidden');
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/abilities/${ability}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    _markAbilityAction(ability);
    _applyAbilityStatus(ability, false);
    if (ctrl) _flashSaveCheck(ctrl, true);
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Failed: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

// (Web Scraper + Browser Cookies credential entry now lives in the shared
// ability table's inline credentials form — see _buildCredentialsSection in
// ui/shared/js/admin-ability-table.js. The old bespoke load/save/unconfigure
// helpers and their /admin/integrations/scraper + /browser-session calls are gone.)

// ── Provider status ───────────────────────────────────────────────────────

// Update the soft warning shown under the redirect_uri input when the host
// in the URI differs from the host the admin is viewing the app on. The
// host is the legitimate degree of freedom (apex vs www, tunnel, custom
// domain), so this is informational — not blocking.
function _refreshRedirectUriWarning(provider) {
  const input = _qs(`ac-int-${provider}-input-redirect_uri`);
  const warn  = _qs(`ac-int-${provider}-form-uri-warn`);
  if (!input || !warn) return;
  const value = (input.value || '').trim();
  if (!value) { warn.classList.add('ac-hidden'); return; }
  let urlHost = '';
  try { urlHost = new URL(value).host.toLowerCase(); } catch { warn.classList.add('ac-hidden'); return; }
  const here = (window.location.host || '').toLowerCase();
  if (!urlHost || !here || urlHost === here) { warn.classList.add('ac-hidden'); return; }
  warn.textContent = `Heads up: ${urlHost} differs from your current host (${here}). Make sure DNS for ${urlHost} points to this app and that you've registered the URI with the provider.`;
  warn.classList.remove('ac-hidden');
}

function _applyProviderStatus(provider, configured, clientId, redirectUri, enabledScopes, suggestedUri) {
  const badge        = _qs(`ac-int-${provider}-badge`);
  const configuredEl = _qs(`ac-int-${provider}-configured`);
  const form         = _qs(`ac-int-${provider}-form`);
  // Populate the editable redirect-URI input with whatever the admin
  // saved, falling back to the request-derived suggestion as a placeholder
  // for unconfigured providers. Attach a one-time keystroke listener that
  // shows/hides the host-mismatch warning.
  const input = _qs(`ac-int-${provider}-input-redirect_uri`);
  if (input) {
    input.value = redirectUri || suggestedUri || '';
    if (!input.dataset.warnAttached) {
      input.addEventListener('input', () => _refreshRedirectUriWarning(provider));
      input.dataset.warnAttached = '1';
    }
    _refreshRedirectUriWarning(provider);
  }
  if (configured) {
    if (badge) { badge.textContent = 'Configured'; badge.className = 'ac-int-badge ac-int-badge-on'; }
    if (configuredEl) configuredEl.classList.remove('ac-hidden');
    if (form) form.classList.add('ac-hidden');
    const cidEl = _qs(`ac-int-${provider}-cid`);
    if (cidEl) cidEl.textContent = clientId || '';
    const uriEl = _qs(`ac-int-${provider}-uri`);
    _attachUriCopy(uriEl, redirectUri || '');
    // Show scope count in configured summary
    let scopeCountEl = document.getElementById(`ac-int-${provider}-scope-count`);
    if (!scopeCountEl && configuredEl) {
      scopeCountEl = document.createElement('div');
      scopeCountEl.id = `ac-int-${provider}-scope-count`;
      scopeCountEl.classList.add('ac-scope-count');
      configuredEl.querySelector('div')?.appendChild(scopeCountEl);
    }
    if (scopeCountEl) {
      const total = (_PROVIDER_SCOPES[provider] || []).length;
      const active = enabledScopes ? enabledScopes.length : total;
      scopeCountEl.textContent = `${active} of ${total} scopes enabled`;
    }
  } else {
    if (badge) { badge.textContent = 'Not configured'; badge.className = 'ac-int-badge ac-int-badge-off'; }
    if (configuredEl) configuredEl.classList.add('ac-hidden');
    if (form) form.classList.remove('ac-hidden');
  }
  _setScopeSelection(provider, enabledScopes || null);
  _syncRowToggle(provider);
}

function _editProviderConfig(provider) {
  const configuredEl = _qs(`ac-int-${provider}-configured`);
  const form         = _qs(`ac-int-${provider}-form`);
  const cidDisplay   = _qs(`ac-int-${provider}-cid`);
  const cidInput     = _qs(`ac-int-${provider}-input-cid`);
  const secInput     = _qs(`ac-int-${provider}-input-secret`);
  const statusEl     = _qs(`ac-int-${provider}-status`);

  // Pre-fill client ID from the displayed (masked) value so the user can see what's there
  if (cidInput && cidDisplay) cidInput.value = cidDisplay.textContent || '';
  // Clear the secret field so user must re-enter it
  if (secInput) secInput.value = '';
  // Clear any lingering status message
  if (statusEl) statusEl.classList.add('ac-hidden');

  // Swap panels: hide configured summary, show edit form
  if (configuredEl) configuredEl.classList.add('ac-hidden');
  if (form) form.classList.remove('ac-hidden');
}

async function _saveProviderConfig(provider) {
  if (!isAdmin()) { showRestrictedModal(); return false; }
  const cidInput = _qs(`ac-int-${provider}-input-cid`);
  const secInput = _qs(`ac-int-${provider}-input-secret`);
  const uriInput = _qs(`ac-int-${provider}-input-redirect_uri`);
  const statusEl = _qs(`ac-int-${provider}-status`);
  // The overlay lives on this provider's Save button.
  const saveBtn  = _qs(`ac-int-${provider}-save`);
  if (!cidInput?.value?.trim() || !secInput?.value?.trim()) {
    _setIntStatus(statusEl, 'Both Client ID and Client Secret are required.');
    if (saveBtn) _flashSaveCheck(saveBtn, false, 'Missing fields');
    return false;
  }
  const selectedScopes = _getSelectedScopes(provider);
  const body = {
    client_id: cidInput.value.trim(),
    client_secret: secInput.value.trim(),
    scopes: selectedScopes,
  };
  const uriValue = (uriInput?.value || '').trim();
  if (uriValue) body.redirect_uri = uriValue;
  if (saveBtn) _markSaving(saveBtn);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/${provider}`), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!res.ok) {
      // Surface server-side validation errors (e.g. wrong path, http instead
      // of https) with the actual reason string from the backend.
      let detail = `HTTP ${res.status}`;
      try {
        const data = await res.json();
        if (data && data.detail) detail = String(data.detail);
      } catch {}
      throw new Error(detail);
    }
    cidInput.value = '';
    secInput.value = '';
    if (saveBtn) _flashSaveCheck(saveBtn, true);
    _loadIntegrations();
    _expandCard(provider);
    return true;
  } catch (e) {
    // Keep the detailed reason inline (the short overlay popup can't carry it).
    _setIntStatus(statusEl, `Error: ${e.message}`);
    if (saveBtn) _flashSaveCheck(saveBtn, false, e.message);
    return false;
  }
}

async function _unconfigureProvider(provider, ctrl) {
  if (!isAdmin()) { showRestrictedModal(); return false; }
  const statusEl = _qs(`ac-int-${provider}-status`);
  if (ctrl) _markSaving(ctrl);
  try {
    const res = await _fetch(apiPath(`/admin/integrations/${provider}`), { method: 'DELETE' });
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    if (ctrl) _flashSaveCheck(ctrl, true);
    _loadIntegrations();
    return true;
  } catch (e) {
    _setIntStatus(statusEl, `Error: ${e.message}`);
    if (ctrl) _flashSaveCheck(ctrl, false, e.message);
    return false;
  }
}

// Keep legacy aliases (referenced by any older inline calls)
async function _saveGoogleConfig()    { return _saveProviderConfig('google'); }
async function _unconfigureGoogle()   { return _unconfigureProvider('google'); }

// ── Automation Engine rows ─────────────────────────────────────────────────
// The Automation Engine group is one flush `.ac-list` of expandable `.ac-row`s
// (Scheduler · Event runtime · Event sources · Recent deliveries — see the
// AUTOMATION ENGINE block in agent-settings.html). Each row toggles `.expanded`
// on a head click (ignoring clicks on the controls inside its body), mirroring
// _wireBootRow() in app-settings.js. The engine's admin-only data is fetched
// LAZILY the first time ANY row is expanded — both halves load together, so the
// scheduler status, event runtime and sources/deliveries are all populated the
// moment the admin opens any one row.
let _autoEngineLoaded = false;
function _loadAutomationEngineOnce() {
  if (_autoEngineLoaded) return;
  _autoEngineLoaded = true;
  loadSchedulerPanel().catch(() => {});
  loadEventSourcesPanel().catch(() => {});
}

function _wireAutomationRows() {
  const list = document.getElementById('ac-automation-engine-list');
  if (!list) return;
  for (const row of list.querySelectorAll(':scope > .ac-row')) {
    const head = row.querySelector(':scope > .ac-ability-row');
    head?.addEventListener('click', (e) => {
      // A click on a control inside the body must never collapse the row.
      if (e.target.closest('select, input, button, a, label, pre')) return;
      row.classList.toggle('expanded');
      if (row.classList.contains('expanded')) _loadAutomationEngineOnce();
    });
  }
}

// ── Templates and prompts rows ─────────────────────────────────────────────
// The "Templates and prompts" group (see agent-settings.html) is one `.ac-list`
// of expandable `.ac-row`s — the Global system prompt and the Agent Prompt
// Templates (moved here from Data Management). Same expand/collapse behaviour as
// _wireAutomationRows above; a click on a control inside a body never collapses
// the row. The row controls themselves bind by element id and are driven
// elsewhere (global-prompt textarea + Save → app-settings.js; the `ac-tpl-*`
// template controls → ui/shared/js/storage.js), so there is nothing to load
// lazily here — just the toggle.
function _wireTemplatesRows() {
  const list = document.getElementById('ac-templates-prompts-list');
  if (!list) return;
  for (const row of list.querySelectorAll(':scope > .ac-row')) {
    const head = row.querySelector(':scope > .ac-ability-row');
    head?.addEventListener('click', (e) => {
      if (e.target.closest('select, input, button, a, label, pre, textarea')) return;
      row.classList.toggle('expanded');
    });
  }
}

// ── Public API ───────────────────────────────────────────────────────────

export function init() {
  _initModelScopeToolbar();
  _initModelAnalytics();
  _initModelTable();
  // Wire the relocated app-global checkboxes AFTER the model table is mounted into
  // the document — _initModelTable()'s advancedExtra moves them in while the table
  // is still detached, so getElementById can't find them until now (the cause of
  // the AI-suggestions toggle never saving).
  _initSuggestionsCheckbox();
  _loadSuggestionsCheckbox();
  _initEmbeddingControl();
  _loadEmbeddingControl();

  // ── Automation Engine: wire buttons now, load data lazily on first expand ──
  // Both halves (scheduler + event sources) hit admin-only endpoints, so defer
  // the fetch until the admin actually expands one of the engine's table rows
  // instead of firing on every Agent Settings visit. Each panel keeps a manual
  // Refresh button for re-fetching after that.
  initSchedulerPanel();
  initEventSourcesPanel();
  _wireAutomationRows();
  _wireTemplatesRows();
  // App-level Gates table — one row per gate type in plugins/gates/, with the
  // app-level params for each (for closing_message: the flag taxonomy plus the Non-fatal: it fills
  // #ac-gates-list, resolves to nothing when the page markup has no such host,
  // and its own try/catch keeps a bad catalog fetch from breaking the page.
  initGatesSection().catch(() => {});

  // `_initIntegrations` is the async structural build (ability table +
  // integration relocation) that the `is-building` gate waits on. Drop the gate
  // the moment it settles — pass OR fail — so the finished tables reveal in one
  // step instead of flashing the raw markup. `.finally` guarantees we never get
  // stuck on the spinner if the build throws.
  //
  // init() owns the WHOLE reveal so it never depends on load() having run —
  // init() (gate up) fires when the Admin tab initialises, but load() only fires
  // when the App Config view is activated, so gating the reveal on load() could
  // leave the spinner stuck. Sequence: build the structure → load the first data
  // snapshot (so every toggle/badge is already in its real state) → lift the
  // gate. Revealing on the build alone would flash default toggle states for a
  // beat before the data lands — the "transient incorrect view". load() still
  // refreshes the data on each later activation; that's just an idempotent
  // re-fetch over the already-revealed tables.
  _lastAgentLoadAt = Date.now();
  _initIntegrations()
    .then(() => _loadIntegrations())
    .catch(() => {})
    .finally(() => {
      _clearBuildGate();
    });
}

// Remove the first-paint build gate so the section's real content is shown.
// Cleared directly (no requestAnimationFrame): the structure and the first data
// snapshot are both already applied by the time this runs, so the browser lays
// out and paints the finished tables in one frame — there's no half-built frame
// to hide. (rAF would be wrong here anyway: it's paused while the page isn't the
// foreground tab, which would leave the spinner stuck.) Idempotent: safe to call
// again on every revisit (the class is already gone).
function _clearBuildGate() {
  document.getElementById('settings-page-agent-tools')?.classList.remove('is-building');
}

export function load() {
  // Skip the re-fetch when init() just kicked off the same three loads (first
  // open fires init() then load() in the same tick). Anything past a couple of
  // seconds is a real re-activation, so refresh normally.
  if (Date.now() - _lastAgentLoadAt < 2000) return;
  _lastAgentLoadAt = Date.now();
  _modelTable?.reload();
  _modelAnalytics?.reload();
  _loadSuggestionsCheckbox();
  _loadEmbeddingControl();
  _loadIntegrations();
}

export function stop() {
  // Abort any pending admin chat request
  if (typeof _intAdminAbort !== 'undefined' && _intAdminAbort) {
    try { _intAdminAbort.abort(); } catch (_) {}
    _intAdminAbort = null;
  }
  _intAdminResetSession();
}


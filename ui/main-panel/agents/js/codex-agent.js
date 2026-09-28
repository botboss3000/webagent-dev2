'use strict';

// Codex deliberately shares Claude's card primitives: both are local headless
// coding CLIs, so their chat-mode, target-device, and persona controls
// mean the same thing. Authentication and skill-file management remain engine
// specific (Codex owns them through its normal CLI/config directory).
import { app } from '../../../shared/js/state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { apiPath } from '../../../shared/js/config.js';
import { _agents, _clearExpanded } from './state.js';
import { _debounced, _putAgentField } from './utils.js';
import { _markSaving as _ovMarkSaving, _flashSaveCheck as _ovFlashCheck } from '../../../shared/js/dom-utils.js';
import { _field, _modeRow, _deviceRow, _toggleRow } from './claude-agent.js';
import { setSessionsAgentContext } from '../sessions/js/sessions-page.js';
import { renderAgentIdentitySettings } from './identity-settings.js';

const _CODEX_AUTH = '/api/v1/codex';

export function mountCodexCardTabs(tabBar, body, agent) {
  if (!tabBar || !body) return;
  let active = 'sessions';

  function show(tab) {
    active = tab;
    tabBar.querySelectorAll('.agents-detail-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === tab));
    setSessionsAgentContext(null);
    body.innerHTML = '';
    if (tab === 'sessions') setSessionsAgentContext(agent.id, body, {
      nativeCodex: agent.codex_code?.context_mode === 'codex_portal',
      agentName: agent.name || 'Codex',
      agentIcon: agent.icon || '',
    });
    else if (tab === 'settings') renderCodexSettings(body, agent);
    else if (tab === 'prompts') import('./tab-prompts.js').then(m => m._renderPromptsTab(body, agent, body.closest('.agent-detail-panel')));
    else if (tab === 'abilities') import('./tab-abilities.js').then(m => m._renderConnectionsTab(body, agent));
  }

  tabBar.innerHTML = '';
  [['sessions', 'Sessions'], ['settings', 'Config'], ['prompts', 'Prompts'], ['abilities', 'Abilities']].forEach(([key, label]) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'agents-detail-tab'; b.dataset.tab = key; b.textContent = label;
    b.addEventListener('click', (e) => { e.stopPropagation(); if (active !== key) show(key); });
    tabBar.appendChild(b);
  });

  show('sessions');
}

function renderCodexAuth(parent) {
  const box = document.createElement('div'); box.className = 'claude-agent-auth';
  const status = document.createElement('div'); status.className = 'claude-auth-status';
  const dot = document.createElement('span'); dot.className = 'claude-auth-dot';
  const label = document.createElement('span'); label.className = 'claude-auth-text'; label.textContent = 'Checking Codex access…';
  status.append(dot, label); box.appendChild(status);
  const actions = document.createElement('div'); actions.className = 'claude-auth-actions'; box.appendChild(actions);
  const message = document.createElement('div'); message.className = 'claude-auth-msg'; box.appendChild(message); parent.appendChild(box);
  async function refresh() {
    try {
      const r = await fetch(`${_CODEX_AUTH}/auth/status`, { headers: { ...authHeaders() } }); const d = await r.json();
      dot.classList.toggle('on', !!d.signed_in); label.textContent = d.signed_in ? 'Signed in to Codex' : (d.installed ? 'Not signed in to Codex' : 'Codex is not installed');
      actions.innerHTML = ''; message.textContent = d.detail || ''; message.classList.toggle('err', !r.ok || !d.installed);
      if (d.signed_in) {
        const out = document.createElement('button'); out.type = 'button'; out.className = 'claude-auth-signout'; out.textContent = 'Sign out';
        out.addEventListener('click', async () => { out.disabled = true; await fetch(`${_CODEX_AUTH}/auth/signout`, { method: 'POST', headers: { ...authHeaders() } }).catch(() => {}); refresh(); }); actions.appendChild(out);
      } else if (d.installed) {
        const login = document.createElement('button'); login.type = 'button'; login.className = 'claude-auth-btn'; login.textContent = 'Sign in to Codex';
        login.addEventListener('click', async () => {
          login.disabled = true;
          const authWindow = typeof window.open === 'function' ? window.open('', '_blank') : null;
          if (authWindow) authWindow.opener = null;
          const r = await fetch(`${_CODEX_AUTH}/login/start`, { method: 'POST', headers: { ...authHeaders() } }).catch(() => null);
          const d = r && await r.json().catch(() => ({}));
          if (r?.ok && d?.verification_url && d?.user_code) {
            if (authWindow) authWindow.location.href = d.verification_url;
            message.innerHTML = '';
            const code = document.createElement('strong'); code.textContent = d.user_code;
            const link = document.createElement('a');
            link.href = d.verification_url; link.target = '_blank'; link.rel = 'noopener noreferrer';
            link.textContent = 'OpenAI verification page';
            message.append('Enter code ', code, ' at the ', link, ', then check sign-in.');
            message.classList.remove('err');
          } else {
            if (authWindow) authWindow.close();
            message.textContent = (d && (d.message || d.detail)) || 'Could not start Codex sign-in.';
            message.classList.add('err');
          }
          login.disabled = false;
        });
        actions.appendChild(login);
        const done = document.createElement('button'); done.type = 'button'; done.className = 'claude-auth-cancel'; done.textContent = 'I finished signing in'; done.addEventListener('click', refresh); actions.appendChild(done);
      }
    } catch (_) { dot.classList.remove('on'); label.textContent = 'Could not check Codex access'; }
  }
  refresh();
}

export function _isCodexAgent(agent) { return !!agent && agent.engine === 'codex'; }

export function _defaultCodexName() {
  const base = 'Codex';
  const names = new Set((_agents || []).map(a => (a.name || '').trim()));
  if (!names.has(base)) return base;
  for (let n = 2; n < 999; n++) if (!names.has(`${base} ${n}`)) return `${base} ${n}`;
  return base;
}

function _configFields(parent, draft, { onSave, skipModel } = {}) {
  _field(parent, { label: 'Extra flags', value: draft.extra_flags || '', placeholder: 'e.g. --profile safe',
    hint: 'Additional flags passed to codex exec.',
    onInput: v => { draft.extra_flags = v.trim(); }, onSave: onSave && (v => onSave({ extra_flags: v.trim() })) });
  // The live "Default model" selector replaces the plain text field on the
  // existing-agent Settings card (skipModel). The create form keeps the simple
  // free-text field — there's no agent id to query against yet, and the new
  // agent's default model can be refined after creation.
  if (!skipModel) {
    _field(parent, { label: 'Model (optional)', value: draft.model || '', placeholder: 'Codex default',
      hint: 'Leave blank to use Codex’s configured default model.',
      onInput: v => { draft.model = v.trim(); }, onSave: onSave && (v => onSave({ model: v.trim() })) });
  }
}

/** Fetch the LIVE Codex model catalog from the backend (which reads it from the
 *  local CLI). force=1 bypasses the backend's cache — the "Query CLI" button.
 *  Returns {ok, models, source} — models [] when the CLI couldn't be queried. */
async function _fetchCodexCatalog(force) {
  const url = apiPath('/api/v1/engines/model-catalog?engine=codex') + (force ? '&force=1' : '');
  try {
    const res = await fetch(url, { headers: { ...authHeaders() } });
    if (!res.ok) return { ok: false, models: [], source: 'fallback' };
    const d = await res.json();
    if (d && d.source === 'cli' && Array.isArray(d.catalog && d.catalog.models) && d.catalog.models.length) {
      return { ok: true, models: d.catalog.models, source: 'cli' };
    }
    return { ok: false, models: [], source: 'fallback' };
  } catch (_) {
    return { ok: false, models: [], source: 'fallback' };
  }
}

// One option row carrying the engine's DEFAULT MODEL <select>, fed by the live
// CLI catalog (`codex debug models` via /api/v1/engines/model-catalog). Mirrors
// _modeRow/_deviceRow chrome (label + .ac-config-control + save overlay).
//   • Options: "Default" (blank → Codex's own default), each model the CLI
//     reports, and "Custom…" which reveals a free-text input for arbitrary ids.
//   • The "Query CLI for latest model options" link forces a re-query (force=1)
//     so newly released models appear without a server restart.
//   • If the CLI can't be queried (no catalog hook / CLI missing), it collapses
//     to the plain free-text field so the admin can still type any model id.
function _modelRow(list, { value, onSave }) {
  const row = document.createElement('div'); row.className = 'ac-ability-row';
  const lab = document.createElement('span'); lab.className = 'ac-ability-label';
  const nameEl = document.createElement('span'); nameEl.className = 'ac-ability-name'; nameEl.textContent = 'Default model';
  lab.appendChild(nameEl);
  const descEl = document.createElement('span'); descEl.className = 'ac-ability-desc';
  lab.appendChild(descEl);
  const queryBtn = document.createElement('button'); queryBtn.type = 'button';
  queryBtn.className = 'claude-auth-cancel'; queryBtn.textContent = 'Query CLI for latest model options';
  lab.appendChild(queryBtn);
  const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
  const sel = document.createElement('select'); sel.className = 'ac-input ac-input-sm ac-config-sel';
  ctrl.appendChild(sel);
  const customInput = document.createElement('input'); customInput.type = 'text';
  customInput.className = 'ac-input'; customInput.placeholder = 'Any model id (e.g. gpt-5.6-sol)';
  customInput.style.marginTop = '6px'; customInput.style.display = 'none';
  ctrl.appendChild(customInput);
  row.appendChild(lab); row.appendChild(ctrl); list.appendChild(row);

  let confirmed = (typeof value === 'string') ? value : '';
  let models = [];          // last catalog from the CLI
  let live = false;         // whether the select is fed by the CLI (vs fallback)
  const CUSTOM = '__custom__';
  const _find = (v) => models.find(m => m.v === v);

  const _descFor = (v) => {
    if (!v) return 'Default — Codex’s configured default model.';
    const m = _find(v);
    return m ? `${m.label} — ${m.sub || ''}` : `${v} (custom)`;
  };

  const _setDesc = (v) => { descEl.textContent = _descFor(v); };

  /** Rebuild the select from `models`. Falls back to the plain text input when
   *  the CLI list is unavailable (the Query button stays so it can be retried). */
  const _rebuild = () => {
    if (!live || !models.length) {
      // Fallback: plain free-text field, no select (mirrors _field look).
      sel.style.display = 'none';
      queryBtn.style.display = '';
      customInput.style.display = '';
      customInput.value = confirmed;
      descEl.textContent = 'Leave blank to use Codex’s configured default model, or type any model id.';
      return;
    }
    sel.style.display = '';
    queryBtn.style.display = '';
    customInput.style.display = 'none';
    sel.innerHTML = '';
    const mk = (v, t, selected) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (selected) o.selected = true; sel.appendChild(o);
    };
    mk('', 'Default — Codex’s own default', !confirmed);
    models.forEach(m => mk(m.v, m.label, m.v === confirmed));
    mk(CUSTOM, 'Custom…', !!confirmed && !_find(confirmed));
    _setDesc(confirmed);
  };

  // Save a chosen value with the shared on-top overlay.
  const _save = async (v) => {
    if (v === confirmed) { _setDesc(v); return true; }
    sel.disabled = true;
    _ovMarkSaving(ctrl);
    let ok = false;
    try { ok = await onSave(v); } catch (_) { ok = false; }
    _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
    sel.disabled = false;
    if (ok) { confirmed = v; _setDesc(v); }
    return ok;
  };

  sel.addEventListener('change', async () => {
    const v = sel.value;
    if (v === CUSTOM) {
      customInput.style.display = '';
      customInput.value = confirmed && !_find(confirmed) ? confirmed : '';
      customInput.focus();
      _setDesc(confirmed || '');
      return;
    }
    customInput.style.display = 'none';
    _setDesc(v);
    const ok = await _save(v);
    if (!ok) { sel.value = confirmed || ''; _setDesc(confirmed || ''); }
  });

  // Custom value: debounced save, like _field.
  const saveCustom = _debounced(async () => {
    const v = customInput.value.trim();
    const ok = await _save(v);
    if (!ok) customInput.value = confirmed;
    else _rebuild();  // re-sync the select (may now match a preset)
  });
  customInput.addEventListener('input', saveCustom);
  customInput.addEventListener('blur', () => saveCustom.flush());

  queryBtn.addEventListener('click', async () => {
    const prev = queryBtn.textContent;
    queryBtn.textContent = 'Querying CLI…';
    queryBtn.disabled = true;
    const r = await _fetchCodexCatalog(true);
    queryBtn.disabled = false;
    if (r.ok) {
      models = r.models;
      live = true;
      _rebuild();
      const t = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
      descEl.textContent = `${r.models.length} models loaded from the CLI · ${t}`;
      // The footer caches its own copy — tell it to drop it so the freshly
      // queried list shows immediately on next open.
      try { window.dispatchEvent(new CustomEvent('engine-catalog-refreshed', { detail: { engine: 'codex' } })); } catch (_) {}
    } else {
      queryBtn.textContent = prev;
      descEl.textContent = 'CLI query failed — showing the last known list.';
    }
  });

  // Initial load: auto-fetch (non-forced — the backend caches, so this is
  // instant except the very first query ever), then paint.
  _fetchCodexCatalog(false).then(r => {
    if (r.ok) { models = r.models; live = true; }
    _rebuild();
  });
  _rebuild();  // paint the fallback immediately so the row is never empty
  return { row, sel, ctrl, customInput };
}

function _advancedRows(parent, agentOrDraft, { saving = false } = {}) {
  const list = document.createElement('div'); list.className = 'ac-list ac-config-list claude-agent-toggles'; parent.appendChild(list);
  const cfg = agentOrDraft.codex_code || agentOrDraft;
  _mountCodexSessionControls(list, parent, cfg, {
    onSave: saving ? patch => _putAgentField(agentOrDraft, { codex_code: patch }, null, { silent: true }) : null,
    agent: saving ? agentOrDraft : null,
  });
  const mode = agentOrDraft.default_execution_mode || 'auto';
  // Codex-specific mode set — ask = read-only, wkspc = workspace-write, auto =
  // full access. Mirrors the engine-aware chat pill for codex agents (chat-ui.js).
  _modeRow(list, { value: mode,
    // Codex-specific mode set — ask = read-only, wkspc = workspace-write, auto =
    // full access. Mirrors the engine-aware chat pill for codex agents (chat-ui.js).
    options: [
      ['ask', 'Ask (read-only)', 'Read-only — Codex researches and proposes changes without writing.'],
      ['wkspc', 'Wkspc (workspace)', 'Workspace-write — Codex can edit the repo but nothing outside it.'],
      ['auto', 'Auto (full)', 'Full access — Codex edits files and runs commands without asking.'],
    ],
    onChange: saving ? null : v => { agentOrDraft.default_execution_mode = v; },
    onSave: saving ? v => _putAgentField(agentOrDraft, { default_execution_mode: v }, null, { silent: true }).then(ok => { if (ok) agentOrDraft.default_execution_mode = v; return ok; }) : null });
  if (saving) _deviceRow(list, { value: agentOrDraft.default_target_device,
    onSave: v => _putAgentField(agentOrDraft, { default_target_device: v }, null, { silent: true }).then(ok => { if (ok) agentOrDraft.default_target_device = v; return ok; }) });
  _toggleRow(list, { label: "Forward this agent's persona", hint: 'Send this agent’s System prompt to Codex as developer instructions.',
    checked: !!cfg.append_persona,
    onChange: saving ? null : on => { cfg.append_persona = on; },
    onSave: saving ? on => _putAgentField(agentOrDraft, { codex_code: { append_persona: on } }, null, { silent: true }).then(ok => { if (ok) cfg.append_persona = on; return ok; }) : null });

  _toggleRow(list, { label: 'Expose tools via MCP', hint: 'Let Codex use WebAgent tools (web search, browser, genui, etc.) alongside its own native tools.',
    checked: !!cfg.mcp_enabled,
    onChange: saving ? null : on => { cfg.mcp_enabled = on; },
    onSave: saving ? on => _putAgentField(agentOrDraft, { codex_code: { mcp_enabled: on } }, null, { silent: true }).then(ok => { if (ok) cfg.mcp_enabled = on; return ok; }) : null });
}

/** Mount the two independent Codex context controls. Exported for the focused
 *  headless DOM contract test; the settings card and create draft share it. */
export function _mountCodexSessionControls(list, parent, cfg, { onSave = null, agent = null } = {}) {
  // Resolve legacy agents at read time: absent values retain the old behaviour
  // (native Codex thread resume with WebAgent's Output Closer enabled).
  const contextMode = ['native_codex', 'webagent_wrapper', 'codex_portal'].includes(cfg.context_mode)
    ? cfg.context_mode : 'native_codex';
  const closerEnabled = typeof cfg.closer_enabled === 'boolean' ? cfg.closer_enabled : true;

  const effective = document.createElement('div');
  effective.className = 'claude-agent-intro codex-effective-mode';
  effective.setAttribute('role', 'status');
  effective.setAttribute('aria-live', 'polite');
  const renderEffectiveMode = () => {
    const mode = ['native_codex', 'webagent_wrapper', 'codex_portal'].includes(cfg.context_mode)
      ? cfg.context_mode : 'native_codex';
    const closer = typeof cfg.closer_enabled === 'boolean' ? cfg.closer_enabled : true;
    effective.textContent = mode === 'codex_portal'
      ? 'Effective mode: Portal · Codex owns the task and transcript · WebAgent stores only your selected task links · Closer disabled.'
      : (mode === 'webagent_wrapper'
        ? `Effective mode: WebAgent owns context · fresh ephemeral Codex run per message · Closer ${closer ? 'enabled' : 'disabled'}.`
        : `Effective mode: Legacy mirror · native Codex session resumes · transcript is mirrored to WebAgent · Closer ${closer ? 'enabled' : 'disabled'}.`);
  };
  // These fields are persisted as independent partial metadata updates. The
  // backend merges each patch with the currently stored object, so overlapping
  // requests could both read the same old value and let the last writer erase
  // its sibling toggle. Keep one queue for this mounted pair and reconcile cfg
  // before allowing the next request to start.
  let saveQueue = Promise.resolve();
  const queueSave = (patch, apply) => {
    const operation = saveQueue.then(async () => {
      let ok = false;
      try { ok = await onSave(patch); } catch (_) { ok = false; }
      if (ok) {
        apply();
        renderEffectiveMode();
      }
      return ok;
    });
    // A rejected/failed request must not poison later saves in the pair.
    saveQueue = operation.then(() => undefined, () => undefined);
    return operation;
  };

  _modeRow(list, {
    value: contextMode,
    options: [
      ['native_codex', 'Legacy mirror', 'Resume a native Codex task while copying its transcript into WebAgent interactions.'],
      ['webagent_wrapper', 'WebAgent wrapper', 'Run a fresh headless Codex process per message using context curated by WebAgent.'],
      ['codex_portal', 'Portal', 'Render selected native Codex tasks directly; WebAgent stores no session or transcript rows.'],
    ],
    onChange: onSave ? null : value => { cfg.context_mode = value; if (value === 'codex_portal') cfg.closer_enabled = false; renderEffectiveMode(); },
    onSave: onSave ? value => queueSave(
      value === 'codex_portal' ? { context_mode: value, closer_enabled: false } : { context_mode: value },
      () => { cfg.context_mode = value; if (value === 'codex_portal') cfg.closer_enabled = false; },
    ) : null,
  });
  _toggleRow(list, {
    label: 'Run WebAgent Closer',
    hint: 'Generate WebAgent’s close-out summary and audit after each response. Off leaves the native or wrapped Codex response untouched.',
    checked: closerEnabled,
    onChange: onSave ? null : on => { cfg.closer_enabled = on; renderEffectiveMode(); },
    onSave: onSave ? on => queueSave(
      { closer_enabled: on },
      () => { cfg.closer_enabled = on; },
    ) : null,
  });
  renderEffectiveMode();
  parent.appendChild(effective);
  return { effective };
}

export function renderCodexCreateBody(body, draft = {}) {
  const intro = document.createElement('div'); intro.className = 'claude-agent-intro';
  intro.textContent = 'Answered by the Codex CLI on this machine. It uses Codex’s existing local sign-in, tools, and skills, and always works in this app’s live project repo. Admin only.';
  body.appendChild(intro);
  renderCodexAuth(body);
  if (!draft.codex_code) draft.codex_code = draft;
  _configFields(body, draft);
  _advancedRows(body, draft);
  return () => ({ codex_code: {
    model: (draft.model || '').trim(),
    extra_flags: (draft.extra_flags || '').trim(),
    append_persona: !!draft.append_persona,
    mcp_enabled: !!draft.mcp_enabled,
    context_mode: ['native_codex', 'webagent_wrapper', 'codex_portal'].includes(draft.context_mode) ? draft.context_mode : 'native_codex',
    closer_enabled: draft.context_mode === 'codex_portal' ? false : (typeof draft.closer_enabled === 'boolean' ? draft.closer_enabled : true),
  }, default_execution_mode: draft.default_execution_mode || 'auto' });
}

export function renderCodexSettings(body, agent) {
  renderAgentIdentitySettings(body, agent);
  const cfg = agent.codex_code && typeof agent.codex_code === 'object' ? agent.codex_code : {};
  const intro = document.createElement('div'); intro.className = 'claude-agent-intro';
  intro.textContent = 'Answered by the Codex CLI on this machine. Codex keeps its own sign-in and local skills; changes here apply to the next headless turn.';
  body.appendChild(intro);
  renderCodexAuth(body);
  const save = patch => _putAgentField(agent, { codex_code: patch }, null, { silent: true }).then(ok => { if (ok) agent.codex_code = { ...cfg, ...patch }; return ok; });
  _configFields(body, cfg, { onSave: save, skipModel: true });
  // Live default-model selector fed by the CLI (codex debug models), with the
  // "Query CLI for latest model options" button.
  _modelRow(body, { value: cfg.model || '', onSave: v => save({ model: v }) });
  _advancedRows(body, agent, { saving: true });
  const note = document.createElement('div'); note.className = 'claude-agent-intro';
  note.textContent = 'Skills are managed by Codex in its configured .codex skill directories and are automatically available to headless Codex runs.';
  body.appendChild(note);
  const danger = document.createElement('div'); danger.className = 'claude-agent-danger';
  const del = document.createElement('button'); del.type = 'button'; del.className = 'claude-agent-delete-btn'; del.textContent = 'Delete this Codex agent';
  let armed = false;
  del.addEventListener('click', async () => {
    if (!armed) { armed = true; del.classList.add('armed'); del.textContent = 'Click again to delete'; setTimeout(() => { armed = false; del.classList.remove('armed'); del.textContent = 'Delete this Codex agent'; }, 3000); return; }
    del.disabled = true;
    try { await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`, { method: 'DELETE', headers: { ...authHeaders() } }); } catch (_) {}
    _clearExpanded(); if (typeof window.__agentsReload === 'function') window.__agentsReload();
  });
  danger.appendChild(del); body.appendChild(danger);
}

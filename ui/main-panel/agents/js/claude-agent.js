'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// CSS variables resolve inside inline styles — use e.g. 'var(--accent)' or
// 'rgba(var(--brand-rgb), 0.16)'. Never hard-code a hex/rgb colour literal here.

/**
 * Agents — Local Claude Code: its OWN create tile + stripped-down agent card.
 *
 * A "Local Claude Code" agent runs the machine's `claude` CLI instead of the
 * WebAgent LLM loop (engine === 'claude_code'; see plugins/engines/claude_code/claude_code.py).
 * It is deliberately DISTINCT from a normal agent — it shares almost none of the
 * normal tabbed agent panel:
 *   • it is created from the unified "New Agent" card by picking the **Claude**
 *     segment of the type chooser (view.js _buildMockTypeToggle); the lower area
 *     then shows this module's `renderClaudeCreateBody` (sign-in + a few settings),
 *     and the card's "+" finalises it (view.js _acceptEngineCreate);
 *   • when opened it shows ONLY its handful of Claude settings, never the normal
 *     Config / Prompts / Agent Loop / Abilities / Members / Monetization tabs.
 * This module owns the inline create body and the post-create settings body. The
 * card *chrome* (icon, name, close/chat buttons) is built by
 * `_renderClaudeAgentCard` in ui/main-panel/agents/js/view.js, which diverts here
 * the moment it sees a claude_code agent and calls `renderClaudeSettings()`.
 *
 * Breadcrumbs: ui/main-panel/agents/js/view.js (builds the create form + diverts
 * the card here), ui/main-panel/agents/js/mock-agent.js (the shared create flow this
 * plugs into), app/defaults/agents/local-claude.json (the template it clones),
 * plugins/engines/claude_code/claude_code.py (the runtime that reads these settings).
 *
 * REMOVE-WHEN: the Local Claude Code engine (claude_code) is dropped.
 */

import { app } from '../../../shared/js/state.js';
import { icon } from '../../../shared/js/icons.js';
import { _agents, _clearExpanded } from './state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { _debounced, _putAgentField } from './utils.js';
import { _markSaving as _ovMarkSaving, _flashSaveCheck as _ovFlashCheck } from '../../../shared/js/dom-utils.js';
import { renderClaudeSkills } from './claude-skills.js';
import { setSessionsAgentContext } from '../sessions/js/sessions-page.js';
import { renderAgentIdentitySettings } from './identity-settings.js';

// The Claude segment of the create card clones app/defaults/agents/local-claude.json
// (template id 'local-claude', referenced in view.js _acceptEngineCreate); the new
// agent inherits its metadata.engine = 'claude_code' + metadata.claude_code {}.

/** True when an agent's runtime is the local Claude Code CLI. */
export function _isClaudeAgent(agent) {
  return !!agent && agent.engine === 'claude_code';
}

// Pick a non-colliding default name ("Claude Code", then "Claude Code 2", …) so
// repeated creates don't pile up identically-named agents. Used by view.js
// (_acceptEngineCreate) when the name field is left blank.
export function _defaultClaudeName() {
  const base = 'Claude Code';
  const names = new Set((_agents || []).map(a => ((a && a.name) || '').trim()));
  if (!names.has(base)) return base;
  for (let n = 2; n < 999; n++) {
    const cand = `${base} ${n}`;
    if (!names.has(cand)) return cand;
  }
  return base;
}

// ── Inline create body (the "Claude" segment of the unified create card) ─────────

/** Render the Claude create form into `body` (the create card's lower area) and
 *  return a `collect()` that reads the chosen values. Mirrors `renderClaudeSettings`
 *  below but writes into the passed `draft` object instead of saving per-field (no
 *  agent exists yet) — so values survive a re-render and are applied in one PUT
 *  after the agent is created (view.js _acceptEngineCreate). The sign-in block is
 *  agent-independent (machine-global login), so it works here before any agent. */
export function renderClaudeCreateBody(body, draft) {
  draft = draft || {};

  const intro = document.createElement('div');
  intro.className = 'claude-agent-intro';
  intro.textContent = 'Answered by the Claude Code app on this machine — your messages run through '
    + 'the local claude program (its login, its tools, its files). Sign in and set a working folder, '
    + 'then press + to create it. Admin only.';
  body.appendChild(intro);

  // Account sign-in (button + paste-a-code field). Machine-global, so it works in
  // the create form before the agent exists. See app/api/claude_auth.py.
  renderClaudeAuth(body);

  const folder = _field(body, {
    label: 'Working folder',
    value: draft.folder || '',
    placeholder: 'C:\\path\\to\\project',
    hint: "Where Claude starts (a starting point, not a fence). Defaults to this app's project folder.",
    onInput: (v) => { draft.folder = v.trim(); },
  });
  // Pre-fill the app's own project directory when nothing's chosen yet, so a fresh
  // Claude agent points at this project out of the box (matches the engine fallback).
  if (!(draft.folder || '').trim()) {
    fetch(`${_CC_AUTH}/auth/status`, { headers: { ...authHeaders() } })
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        const def = d && d.default_folder;
        if (def && !folder.el.value.trim()) { folder.el.value = def; draft.folder = def; }
      })
      .catch(() => {});
  }
  _field(body, {
    label: 'Extra flags',
    value: draft.extra_flags || '',
    placeholder: 'e.g. --add-dir ..\\sibling',
    hint: 'Extra command-line flags passed to claude.',
    onInput: (v) => { draft.extra_flags = v.trim(); },
  });
  _field(body, {
    label: 'Model (optional)',
    value: draft.model || '',
    placeholder: "Claude's default",
    hint: "Leave blank to use Claude's own configured default model.",
    onInput: (v) => { draft.model = v.trim(); },
  });

  const list = document.createElement('div');
  list.className = 'ac-list ac-config-list claude-agent-toggles';
  body.appendChild(list);

  if (draft.default_execution_mode == null) draft.default_execution_mode = 'auto';
  _modeRow(list, {
    value: ['ask', 'plan', 'auto'].includes(draft.default_execution_mode) ? draft.default_execution_mode : 'auto',
    onChange: (v) => { draft.default_execution_mode = v; },
  });
  _toggleRow(list, {
    label: "Forward this agent's persona",
    hint: "Send this agent's System prompt to Claude as an extra system prompt.",
    checked: !!draft.append_persona,
    onChange: (on) => { draft.append_persona = on; },
  });
  _toggleRow(list, {
    label: 'Expose tools via MCP',
    hint: 'Let Claude use WebAgent tools (web search, browser, genui, etc.) alongside its own.',
    checked: !!draft.mcp_enabled,
    onChange: (on) => { draft.mcp_enabled = on; },
  });

  return () => ({
    claude_code: {
      folder: (draft.folder || '').trim(),
      extra_flags: (draft.extra_flags || '').trim(),
      model: (draft.model || '').trim(),
      append_persona: !!draft.append_persona,
      mcp_enabled: !!draft.mcp_enabled,
    },
    default_execution_mode: draft.default_execution_mode || 'auto',
  });
}

// ── Card tabs (Settings | Skills) ───────────────────────────────────────────────
// The Claude card used to be entirely tab-less; it now carries a slim two-tab bar
// so it can host the Skills tab (claude-skills.js) beside its settings. The bar
// reuses the normal card's `.agent-card-tabs` / `.agents-detail-tab` look (it lives
// inside the `.agent-card`, like the normal tab strip), and swaps the panel body
// between the two renderers below. Called from `_renderClaudeAgentCard` in
// ui/main-panel/agents/js/view.js.

/** Populate the Claude card's tab bar and wire it to swap `body` between views. */
export function mountClaudeCardTabs(tabBar, body, agent) {
  if (!tabBar || !body) return;
  let active = 'sessions';

  function show(tab) {
    active = tab;
    tabBar.querySelectorAll('.agents-detail-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === tab));
    setSessionsAgentContext(null);
    body.innerHTML = '';
    if (tab === 'sessions') setSessionsAgentContext(agent.id, body, { agentName: agent.name || 'Claude' });
    else if (tab === 'skills') renderClaudeSkills(body, agent);
    else if (tab === 'prompts') import('./tab-prompts.js').then(m => m._renderPromptsTab(body, agent, body.closest('.agent-detail-panel')));
    else if (tab === 'abilities') import('./tab-abilities.js').then(m => m._renderConnectionsTab(body, agent));
    else renderClaudeSettings(body, agent);
  }

  tabBar.innerHTML = '';
  [['sessions', 'Sessions'], ['settings', 'Config'], ['prompts', 'Prompts'], ['abilities', 'Abilities'], ['skills', 'Skills']].forEach(([key, label]) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'agents-detail-tab'; b.dataset.tab = key; b.textContent = label;
    b.addEventListener('click', (e) => { e.stopPropagation(); if (active !== key) show(key); });
    tabBar.appendChild(b);
  });

  show('sessions');
}

// ── Settings body (the distinct, tab-less panel) ────────────────────────────────

/** Render the Claude settings into `body` (the open card's detail body): the few
 *  claude_code knobs + a delete — and nothing the normal agent panel has. Saves go
 *  through the agent PUT's `claude_code` lane (silent, so a single edit doesn't tear
 *  down the open card); the engine reads them on each turn. */
export function renderClaudeSettings(body, agent) {
  renderAgentIdentitySettings(body, agent);
  const cc = (agent.claude_code && typeof agent.claude_code === 'object') ? agent.claude_code : {};

  const intro = document.createElement('div');
  intro.className = 'claude-agent-intro';
  intro.textContent = 'Answered by the Claude Code app on this machine — your messages run through '
    + 'the local claude program (its login, its tools, its files) and stream back here. Admin only.';
  body.appendChild(intro);

  // Account sign-in (button + paste-a-code field, no terminal needed). Saves a
  // long-lived login so the agent keeps working. See app/api/claude_auth.py.
  renderClaudeAuth(body);

  // Patch the local cache after a successful silent save so a later rebuild keeps
  // the new value (the PUT is silent and does not re-fetch the agent).
  const _save = (patch) =>
    _putAgentField(agent, { claude_code: patch }, null, { silent: true })
      .then(ok => { if (ok) agent.claude_code = { ...(agent.claude_code || {}), ...patch }; return ok; });

  const folderField = _field(body, {
    label: 'Working folder',
    value: cc.folder || '',
    placeholder: 'C:\\path\\to\\project',
    hint: "Where Claude starts (a starting point, not a fence). Defaults to this app's project folder.",
    onSave: (val) => _save({ folder: val.trim() }),
  });
  // Auto-select this app's actual project directory when none is set yet, so the
  // agent points at this project out of the box (the same folder the engine falls
  // back to). Display-only — we don't force a save, so it always follows wherever
  // the app lives, and the admin can overwrite it. Pulled from the sign-in status.
  if (!(cc.folder || '').trim()) {
    fetch(`${_CC_AUTH}/auth/status`, { headers: { ...authHeaders() } })
      .then(r => (r.ok ? r.json() : null))
      .then(d => {
        const def = d && d.default_folder;
        if (def && !folderField.el.value.trim()) folderField.el.value = def;
      })
      .catch(() => {});
  }
  _field(body, {
    label: 'Extra flags',
    value: cc.extra_flags || '',
    placeholder: 'e.g. --add-dir ..\\sibling',
    hint: 'Extra command-line flags passed to claude.',
    onSave: (val) => _save({ extra_flags: val.trim() }),
  });
  _field(body, {
    label: 'Model (optional)',
    value: cc.model || '',
    placeholder: "Claude's default",
    hint: "Leave blank to use Claude's own configured default model.",
    onSave: (val) => _save({ model: val.trim() }),
  });

  const list = document.createElement('div');
  list.className = 'ac-list ac-config-list claude-agent-toggles';
  body.appendChild(list);

  // Default chat mode (Ask / Plan / Auto) — the same per-agent setting normal
  // agents have, here mapped onto `claude --permission-mode`. New chat sessions
  // START in this; the chat pill can still change it per session. "Auto" is the
  // old "Act freely" (edits + runs without asking → --dangerously-skip-permissions);
  // "Plan" runs read-only and proposes a plan; "Ask" won't change things without
  // approval. Stored in the shared metadata.default_execution_mode field; the
  // engine reads it via _resolve_permission_mode (plugins/engines/claude_code/claude_code.py).
  // Display value: an explicit mode if set, otherwise derive from the legacy
  // "act freely" toggle (off → Ask, on/default → Auto) so an un-migrated agent
  // shows the mode that matches how it actually behaves today.
  _modeRow(list, {
    value: ['ask', 'plan', 'auto'].includes(agent.default_execution_mode)
      ? agent.default_execution_mode
      : (cc.act_freely === false ? 'ask' : 'auto'),
    onSave: (val) => _putAgentField(agent, { default_execution_mode: val }, null, { silent: true })
      .then(ok => { if (ok) agent.default_execution_mode = val; return ok; }),
  });

  // Remote Control default — which machine in the shared fleet runs this agent's
  // chats. The whole turn is handed to the chosen device and ITS local `claude`
  // does the work, so a Claude agent gets the same default-device behaviour as a
  // normal agent. Stored in the shared metadata.default_target_device field
  // (mirrors tab-config.js); read by chat-ui.js on session open to pre-select the
  // chat's Remote Control pill. Only meaningful with a shared DB + a second device.
  _deviceRow(list, {
    value: agent.default_target_device,
    onSave: (val) => _putAgentField(agent, { default_target_device: val }, null, { silent: true })
      .then(ok => { if (ok) agent.default_target_device = val; return ok; }),
  });

  _toggleRow(list, {
    label: "Forward this agent's persona",
    hint: "Send this agent's System prompt to Claude as an extra system prompt.",
    checked: (cc.append_persona != null) ? !!cc.append_persona : false,
    onSave: (on) => _save({ append_persona: on }),
  });

  _toggleRow(list, {
    label: 'Expose tools via MCP',
    hint: 'Let Claude use WebAgent tools (web search, browser, genui, etc.) alongside its own native tools.',
    checked: !!cc.mcp_enabled,
    onSave: (on) => _save({ mcp_enabled: on }),
  });

  // Delete lives on the distinct card itself (the normal squares' trash selection
  // still works too). Two-tap confirm via the button's own armed state.
  const danger = document.createElement('div');
  danger.className = 'claude-agent-danger';
  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.className = 'claude-agent-delete-btn';
  const REST = icon('trash-2', { size: '14px' }) + ' Delete this Claude agent';
  delBtn.innerHTML = REST;
  let armed = false;
  let armTimer = null;
  delBtn.addEventListener('click', async () => {
    if (!armed) {
      armed = true;
      delBtn.classList.add('armed');
      delBtn.innerHTML = icon('trash-2', { size: '14px' }) + ' Click again to delete';
      armTimer = setTimeout(() => {
        armed = false;
        delBtn.classList.remove('armed');
        delBtn.innerHTML = REST;
      }, 3000);
      return;
    }
    if (armTimer) clearTimeout(armTimer);
    delBtn.disabled = true;
    delBtn.innerHTML = icon('loader-2', { size: '14px' }) + ' Deleting…';
    await _deleteClaudeAgent(agent);
  });
  danger.appendChild(delBtn);
  body.appendChild(danger);
}

// ── Account sign-in (inline button + paste-a-code field) ───────────────────────
// Drives the in-app `claude setup-token` flow (app/api/claude_auth.py): Sign in →
// shows the Claude authorize link + a code box → paste the code → a long-lived
// login is saved and injected into every turn. No modal, no terminal.

const _CC_AUTH = '/api/v1/claude-code';

/** Build the sign-in block into `parent`. Self-contained: checks status, runs
 *  the start → paste-code → finish flow, and supports sign-out. */
function renderClaudeAuth(parent) {
  let loginId = null;

  const box = document.createElement('div');
  box.className = 'claude-agent-auth';

  const statusRow = document.createElement('div');
  statusRow.className = 'claude-auth-status';
  const dot = document.createElement('span'); dot.className = 'claude-auth-dot';
  const statusText = document.createElement('span'); statusText.className = 'claude-auth-text';
  statusText.textContent = 'Checking sign-in…';
  statusRow.appendChild(dot); statusRow.appendChild(statusText);
  box.appendChild(statusRow);

  const actions = document.createElement('div');
  actions.className = 'claude-auth-actions';
  box.appendChild(actions);

  const boxMsg = document.createElement('div');
  boxMsg.className = 'claude-auth-msg';
  box.appendChild(boxMsg);

  parent.appendChild(box);

  const _signInBtnHtml = icon('log-in', { size: '14px' }) + ' Sign in to Claude';

  function setState(signedIn, label) {
    dot.classList.toggle('on', !!signedIn);
    statusText.textContent = label || (signedIn ? 'Signed in to Claude' : 'Not signed in');
    actions.innerHTML = '';
    boxMsg.textContent = '';
    boxMsg.classList.remove('err');
    if (signedIn) {
      const out = document.createElement('button');
      out.type = 'button'; out.className = 'claude-auth-signout';
      out.textContent = 'Sign out';
      out.addEventListener('click', async () => {
        out.disabled = true; out.textContent = 'Signing out…';
        try { await fetch(`${_CC_AUTH}/auth/signout`, { method: 'POST', headers: { ...authHeaders() } }); } catch (_) { /* ignore */ }
        refresh();
      });
      actions.appendChild(out);
    } else {
      const btn = document.createElement('button');
      btn.type = 'button'; btn.className = 'claude-auth-btn';
      btn.innerHTML = _signInBtnHtml;
      btn.addEventListener('click', () => startLogin(btn));
      actions.appendChild(btn);
    }
  }

  async function refresh() {
    try {
      const r = await fetch(`${_CC_AUTH}/auth/status`, { headers: { ...authHeaders() } });
      const d = await r.json();
      const label = d.signed_in
        ? (d.token_saved ? 'Signed in to Claude (saved login)' : 'Signed in to Claude')
        : 'Not signed in';
      setState(!!d.signed_in, label);
    } catch (_) {
      setState(false, 'Not signed in');
    }
  }

  async function startLogin(btn) {
    btn.disabled = true;
    btn.innerHTML = icon('loader-2', { size: '14px' }) + ' Starting…';
    boxMsg.textContent = ''; boxMsg.classList.remove('err');
    let d = null;
    try {
      const r = await fetch(`${_CC_AUTH}/login/start`, { method: 'POST', headers: { ...authHeaders() } });
      d = await r.json();
    } catch (e) {
      d = { ok: false, error: 'Network error: ' + e.message };
    }
    btn.disabled = false; btn.innerHTML = _signInBtnHtml;
    if (!d || !d.ok) {
      boxMsg.textContent = (d && (d.error || d.detail)) || 'Could not start sign-in.';
      boxMsg.classList.add('err');
      return;
    }
    loginId = d.login_id;
    buildForm(d.authorize_url, !!d.browser_opened);
  }

  function buildForm(url, browserOpened) {
    actions.innerHTML = '';
    boxMsg.textContent = '';

    const form = document.createElement('div');
    form.className = 'claude-auth-form';

    const s1 = document.createElement('div');
    s1.className = 'claude-auth-step';
    s1.appendChild(document.createTextNode(browserOpened
      ? '1. A Claude sign-in page should have opened in your browser. If it didn’t, '
      : '1. Open the Claude sign-in page: '));
    const link = document.createElement('a');
    link.href = url; link.target = '_blank'; link.rel = 'noopener';
    link.className = 'claude-auth-link';
    link.innerHTML = icon('external-link', { size: '12px' }) + ' open it here';
    s1.appendChild(link);
    form.appendChild(s1);

    const s2 = document.createElement('div');
    s2.className = 'claude-auth-step';
    s2.textContent = '2. Authorize, then copy the code Claude shows you and paste it below.';
    form.appendChild(s2);

    const row = document.createElement('div');
    row.className = 'claude-auth-input-row';
    const input = document.createElement('input');
    input.type = 'text'; input.className = 'ac-input claude-auth-code';
    input.placeholder = 'Paste the code here';
    input.autocomplete = 'off'; input.spellcheck = false;
    const finish = document.createElement('button');
    finish.type = 'button'; finish.className = 'claude-auth-btn';
    finish.textContent = 'Finish sign-in';
    row.appendChild(input); row.appendChild(finish);
    form.appendChild(row);

    const cancel = document.createElement('button');
    cancel.type = 'button'; cancel.className = 'claude-auth-cancel';
    cancel.textContent = 'Cancel';
    form.appendChild(cancel);

    const msg = document.createElement('div');
    msg.className = 'claude-auth-msg';
    form.appendChild(msg);

    actions.appendChild(form);
    input.focus();

    async function doFinish() {
      const code = input.value.trim();
      if (!code) { msg.textContent = 'Paste the code first.'; msg.classList.add('err'); return; }
      finish.disabled = true; input.disabled = true;
      finish.innerHTML = icon('loader-2', { size: '14px' }) + ' Finishing…';
      msg.textContent = 'Linking your account…'; msg.classList.remove('err');
      let d = null;
      try {
        const r = await fetch(`${_CC_AUTH}/login/submit`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ login_id: loginId, code }),
        });
        d = await r.json();
      } catch (e) {
        d = { ok: false, error: 'Network error: ' + e.message };
      }
      if (d && d.ok) {
        setState(true, d.message || 'Signed in to Claude');
        return;
      }
      finish.disabled = false; input.disabled = false; finish.textContent = 'Finish sign-in';
      msg.textContent = (d && (d.error || d.detail)) || 'Sign-in failed — check the code and try again.';
      msg.classList.add('err');
    }

    finish.addEventListener('click', doFinish);
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); doFinish(); } });
    cancel.addEventListener('click', async () => {
      try {
        await fetch(`${_CC_AUTH}/login/cancel`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...authHeaders() },
          body: JSON.stringify({ login_id: loginId }),
        });
      } catch (_) { /* ignore */ }
      refresh();
    });
  }

  refresh();
}

/** Soft-delete (to the recycling bin), then clear the open card and refresh the
 *  grid via the window callback registered in view.js. */
async function _deleteClaudeAgent(agent) {
  try {
    await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE', headers: { ...authHeaders() } });
  } catch (e) {
    console.warn('agents: delete Claude agent failed', e);
  }
  try {
    if (typeof app.populateAgentSelect === 'function') await app.populateAgentSelect(app.currentUserId);
  } catch (_) { /* non-fatal */ }
  _clearExpanded();
  if (typeof window.__agentsReload === 'function') window.__agentsReload();
}

// ── Small self-contained field/row builders ────────────────────────────────────
// Deliberately local — the Claude card shares none of the normal Config tab's
// layout code (tab-config.js). They reuse the shared `.ac-*` / `.conn-toggle-*`
// classes + the on-top save overlay so the look matches without the coupling.

// onSave → debounced save with the on-top green-tick overlay (the live settings
// card). onInput → plain capture into a draft, no overlay (the create form, where
// nothing is persisted until the "+" finalises it).
export function _field(parent, { label, value, placeholder, hint, onSave, onInput }) {
  const wrap = document.createElement('div'); wrap.className = 'ac-cfg-field';
  const lbl = document.createElement('label'); lbl.className = 'ac-label'; lbl.textContent = label;
  const ind = document.createElement('span'); ind.className = 'ac-cfg-ind'; lbl.appendChild(ind);
  wrap.appendChild(lbl);
  const el = document.createElement('input'); el.type = 'text'; el.className = 'ac-input';
  el.value = value || '';
  if (placeholder) el.placeholder = placeholder;
  wrap.appendChild(el);
  if (hint) { const h = document.createElement('div'); h.className = 'ac-hint'; h.textContent = hint; wrap.appendChild(h); }
  parent.appendChild(wrap);
  if (onSave) {
    const save = _debounced(async () => {
      _ovMarkSaving(ind);
      let ok = false;
      try { ok = await onSave(el.value); } catch (_) { ok = false; }
      _ovFlashCheck(ind, !!ok, ok ? '' : 'Save failed');
    });
    el.addEventListener('input', save);
    el.addEventListener('blur', () => save.flush());
  } else if (onInput) {
    el.addEventListener('input', () => onInput(el.value));
  }
  return { wrap, el, ind };
}

// One option row carrying the default-mode select for "Default chat mode".
// Mirrors _toggleRow's row chrome (label + .ac-config-control) but with a <select>
// and the shared save overlay; the hint under the label tracks the chosen mode.
// `options` overrides the whole vocabulary ([[value, label, hint?], …]) — the
// Codex card passes its engine-specific set (Ask read-only / Wkspc workspace /
// Auto full); without it the native Ask/Plan/Auto set is used. The hint text
// falls back to HINTS when an option doesn't carry its own.
export function _modeRow(list, { value, onSave, onChange, options }) {
  const HINTS = {
    ask:  'Asks before editing files or running commands; researches freely.',
    plan: 'Read-only — Claude researches and proposes a plan instead of acting.',
    auto: 'Edits files and runs commands without asking (like running claude yourself).',
    wkspc: 'Workspace-write — Codex can edit the repo but nothing outside it.',
  };
  const opts = Array.isArray(options) && options.length
    ? options : [['ask', 'Ask'], ['plan', 'Plan'], ['auto', 'Auto']];
  const cur = opts.some(([v]) => v === value) ? value : opts[0][0];
  const hintFor = (v) => {
    const o = opts.find(([ov]) => ov === v);
    if (o && o[2]) return o[2];
    return HINTS[v] || '';
  };
  const row = document.createElement('div'); row.className = 'ac-ability-row';
  const lab = document.createElement('span'); lab.className = 'ac-ability-label';
  const nameEl = document.createElement('span'); nameEl.className = 'ac-ability-name'; nameEl.textContent = 'Default chat mode';
  lab.appendChild(nameEl);
  const descEl = document.createElement('span'); descEl.className = 'ac-ability-desc'; descEl.textContent = hintFor(cur);
  lab.appendChild(descEl);
  const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
  const sel = document.createElement('select');
  sel.className = 'ac-input ac-input-sm ac-config-sel';
  opts.forEach(([v, t]) => {
    const o = document.createElement('option'); o.value = v; o.textContent = t;
    if (v === cur) o.selected = true; sel.appendChild(o);
  });
  ctrl.appendChild(sel);
  row.appendChild(lab); row.appendChild(ctrl); list.appendChild(row);
  let confirmed = cur;
  sel.addEventListener('change', async () => {
    const selected = sel.value;
    descEl.textContent = hintFor(selected);
    // Draft mode (create form): just record the choice, no persistence/overlay.
    if (onChange) { onChange(selected); return; }
    if (!onSave || selected === confirmed) return;
    sel.disabled = true;
    _ovMarkSaving(ctrl);
    const ok = await onSave(selected);
    _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
    sel.disabled = false;
    if (ok) { confirmed = selected; }
    else { sel.value = confirmed; descEl.textContent = hintFor(confirmed); }
  });
  return { row, sel, ctrl };
}

// One option row carrying the Remote Control default-device <select>. Mirrors
// _modeRow's chrome (label + .ac-config-control + save overlay) but populates from
// the shared device fleet (window.DevicePicker — the SAME list the chat pill uses)
// and saves metadata.default_target_device.
//   • "None" (the DEFAULT, blank '') = no pin — each chat runs on whichever device
//     is messaging the agent (it roams to wherever you are).
//   • "This device" pins the current machine's own fleet id (so the choice reads as
//     *this* device from any other one, not a relative '' each device saw as itself).
//   • any other device pins that device by its concrete id.
// Because the target-device hand-off happens ABOVE the engine — the whole turn is
// shipped to the chosen device and ITS local `claude` runs it — a Claude agent gets
// the same Remote Control default as a normal one. Mirrors the normal Config tab's
// "Target device" section (tab-config.js). The fleet loads async: we paint "None"
// (+ any saved-but-offline value) first, then fill in the rest.
export function _deviceRow(list, { value, onSave }) {
  const HINT_NONE   = 'By default a chat runs on whichever device is messaging the agent. Pick a machine to always run there.';
  const HINT_REMOTE = "New chats run on the chosen machine — its OWN Claude (login, files) does the work. "
    + "If it’s offline they fall back to the messaging device; you can still pick another per chat.";
  const row = document.createElement('div'); row.className = 'ac-ability-row';
  const lab = document.createElement('span'); lab.className = 'ac-ability-label';
  const nameEl = document.createElement('span'); nameEl.className = 'ac-ability-name'; nameEl.textContent = 'Target device';
  lab.appendChild(nameEl);
  const descEl = document.createElement('span'); descEl.className = 'ac-ability-desc';
  lab.appendChild(descEl);
  const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
  const sel = document.createElement('select'); sel.className = 'ac-input ac-input-sm ac-config-sel';
  ctrl.appendChild(sel);
  row.appendChild(lab); row.appendChild(ctrl); list.appendChild(row);

  let confirmed = (typeof value === 'string') ? value : '';
  let loaded = null;  // last fleet list, so a re-render after save keeps it

  // Rebuild the option list. `null` devices = pre-load paint (None + any saved value
  // only). Keeps a saved-but-offline device as an option so the choice still shows.
  const _rebuild = (devices) => {
    if (Array.isArray(devices)) loaded = devices;
    const all = Array.isArray(loaded) ? loaded : [];
    const selfDev = all.find(d => d && d.is_self);
    const selfId = (selfDev && selfDev.instance_id)
      || ((window.DevicePicker && window.DevicePicker.selfId && window.DevicePicker.selfId()) || '');
    const others = all.filter(d => d && !d.is_self);
    const val = confirmed;
    const isNone = !val;                         // blank/absent = roam to messenger
    const isSelf = !!selfId && val === selfId;   // pinned to THIS machine
    // Claude-readiness: a device can only run a Claude agent if it has the `claude`
    // CLI installed (reported in its presence capabilities). Flag any that can't so
    // a target is caught BEFORE you send, not just by the runtime notice. Unknown =
    // treat as ready (older server / not yet loaded) so we never cry wolf.
    const ready = (d) => (window.DevicePicker && window.DevicePicker.claudeReady)
      ? window.DevicePicker.claudeReady(d) : true;
    const NO_CLAUDE = ' — no Claude';
    sel.innerHTML = '';
    const mk = (v, t, selected) => {
      const o = document.createElement('option'); o.value = v; o.textContent = t;
      if (selected) o.selected = true; sel.appendChild(o);
    };
    // Default: no pin — the agent runs on whichever device is messaging it.
    mk('', 'None — runs on the messaging device', isNone);
    // "This device" pins the current machine's real fleet id; only offered once the
    // fleet has loaded (before that selfId is unknown). Name shown in parens.
    if (selfId) {
      const selfName = selfDev ? String(selfDev.label || '').trim() : '';
      const base = selfName ? ('This device (' + selfName + ')') : 'This device';
      mk(selfId, base + (selfDev && !ready(selfDev) ? NO_CLAUDE : ''), isSelf);
    }
    const seen = new Set(['', selfId]);
    others.forEach(d => {
      const suffix = (d.online ? '' : ' (offline)') + (ready(d) ? '' : NO_CLAUDE);
      mk(d.instance_id, (d.label || d.instance_id) + suffix, d.instance_id === val);
      seen.add(d.instance_id);
    });
    if (val && !seen.has(val)) {
      const lbl = (window.DevicePicker && window.DevicePicker.labelFor) ? window.DevicePicker.labelFor(val) : val;
      mk(val, (lbl && lbl !== val ? lbl : val) + ' (offline)', true);
    }
    // Warn loudly when the PINNED target is known and can't run Claude — chats sent
    // there will fail until Claude Code is installed on it.
    const pinnedDev = isNone ? null : (isSelf ? selfDev : others.find(d => d.instance_id === val));
    const warnNoClaude = !!(pinnedDev && !ready(pinnedDev));
    descEl.classList.toggle('ac-desc-warn', warnNoClaude);
    descEl.textContent = warnNoClaude
      ? 'Claude isn’t installed on the pinned device — chats sent there will fail until Claude Code is installed on it. Install it there, or pick another device.'
      : (isNone
          ? HINT_NONE
          : (!others.length
              ? 'Pinned to this machine — chats run here. A remote target must have Claude signed in and its server running.'
              : HINT_REMOTE));
  };
  _rebuild(null);
  try {
    if (window.DevicePicker && window.DevicePicker.load) {
      window.DevicePicker.load().then(_rebuild).catch(() => {});
    }
  } catch (_) { /* fleet list is best-effort; the saved value still shows */ }

  sel.addEventListener('change', async () => {
    const selected = sel.value;
    if (selected === confirmed) return;
    sel.disabled = true;
    _ovMarkSaving(ctrl);
    let ok = false;
    try { ok = await onSave(selected); } catch (_) { ok = false; }
    _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
    sel.disabled = false;
    if (ok) { confirmed = selected; _rebuild(loaded); }
    else { sel.value = confirmed; }
  });
  return { row, sel, ctrl };
}

export function _toggleRow(list, { label, hint, checked, onSave, onChange }) {
  const row = document.createElement('div'); row.className = 'ac-ability-row';
  const lab = document.createElement('span'); lab.className = 'ac-ability-label';
  const nameEl = document.createElement('span'); nameEl.className = 'ac-ability-name'; nameEl.textContent = label;
  lab.appendChild(nameEl);
  if (hint) { const d = document.createElement('span'); d.className = 'ac-ability-desc'; d.textContent = hint; lab.appendChild(d); }
  const ctrl = document.createElement('span'); ctrl.className = 'ac-config-control';
  const wrap = document.createElement('label'); wrap.className = 'conn-toggle-wrap ac-ability-toggle-wrap';
  const cb = document.createElement('input'); cb.type = 'checkbox'; cb.className = 'conn-toggle'; cb.checked = !!checked;
  const track = document.createElement('span'); track.className = 'conn-toggle-track';
  wrap.appendChild(cb); wrap.appendChild(track); ctrl.appendChild(wrap);
  row.appendChild(lab); row.appendChild(ctrl); list.appendChild(row);
  cb.addEventListener('change', async () => {
    // Draft mode (create form): just record the choice, no persistence/overlay.
    if (onChange) { onChange(cb.checked); return; }
    cb.disabled = true;
    _ovMarkSaving(ctrl);
    const ok = await onSave(cb.checked);
    _ovFlashCheck(ctrl, ok, ok ? '' : 'Save failed');
    cb.disabled = false;
    if (!ok) cb.checked = !cb.checked;
  });
  return { row, cb, ctrl };
}

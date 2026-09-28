'use strict';

// COLOR SCHEME → ui/shared/css/design-system.css (single source of truth).
// CSS variables resolve inside inline styles — use e.g. 'var(--accent)' or
// 'rgba(var(--brand-rgb), 0.16)'. Never hard-code a hex/rgb colour literal here.

/**
 * Agents — Terminal Chat: its OWN create tile + stripped-down agent card.
 *
 * A "Terminal Chat" agent runs a terminal program on this machine and streams
 * its raw output into the chat panel as a live xterm.js view (engine ===
 * 'terminal_chat'; see plugins/engines/terminal_chat/terminal_chat.py).
 * It is deliberately DISTINCT from a normal agent — it shares almost none of the
 * normal tabbed agent panel:
 *   • it is created from the unified "New Agent" card by picking the **Terminal**
 *     segment of the type chooser (view.js _buildMockTypeToggle); the lower area
 *     then shows this module's `renderTerminalChatCreateBody`, and the card's "+"
 *     finalises it (view.js _acceptEngineCreate);
 *   • when opened it shows ONLY its handful of terminal settings, never the normal
 *     Config / Prompts / Agent Loop / Abilities / Members / Monetization tabs.
 * This module owns the inline create body and the post-create settings body. The
 * card *chrome* (icon, name, close/chat buttons) is built by
 * `_renderTerminalChatAgentCard` in ui/main-panel/agents/js/view.js, which diverts here
 * the moment it sees a terminal_chat agent and calls `renderTerminalChatSettings()`.
 *
 * Breadcrumbs: ui/main-panel/agents/js/view.js (builds the create form + diverts
 * the card here), ui/main-panel/agents/js/mock-agent.js (the shared create flow this
 * plugs into), app/defaults/agents/terminal-chat.json (the template it clones),
 * plugins/engines/terminal_chat/terminal_chat.py (the runtime that reads these settings).
 *
 * REMOVE-WHEN: the Terminal Chat engine (terminal_chat) is dropped.
 */

import { app } from '../../../shared/js/state.js';
import { icon } from '../../../shared/js/icons.js';
import { _agents, _clearExpanded } from './state.js';
import { authHeaders } from '../../../shared/js/left-login.js';
import { _debounced, _putAgentField } from './utils.js';
import { setSessionsAgentContext } from '../sessions/js/sessions-page.js';
import { renderAgentIdentitySettings } from './identity-settings.js';
import { _markSaving as _ovMarkSaving, _flashSaveCheck as _ovFlashCheck } from '../../../shared/js/dom-utils.js';

// The Terminal segment of the create card clones app/defaults/agents/terminal-chat.json
// (template id 'terminal-chat', referenced in view.js _acceptEngineCreate); the new
// agent inherits metadata.engine = 'terminal_chat' + metadata.terminal_chat {}.

/** True when an agent's runtime is the Terminal Chat engine. */
export function _isTerminalChatAgent(agent) {
  return !!agent && agent.engine === 'terminal_chat';
}

// Pick a non-colliding default name ("Terminal Chat", then "Terminal Chat 2", …).
// Used by view.js (_acceptEngineCreate) when the name field is left blank.
export function _defaultTerminalChatName() {
  const base = 'Terminal Chat';
  const names = new Set((_agents || []).map(a => ((a && a.name) || '').trim()));
  if (!names.has(base)) return base;
  for (let n = 2; n < 999; n++) {
    const cand = `${base} ${n}`;
    if (!names.has(cand)) return cand;
  }
  return base;
}

// ── Inline create body (the "Terminal" segment of the unified create card) ───────

/** Render the Terminal Chat create form into `body` (the create card's lower area)
 *  and return a `collect()` that reads the chosen values. Mirrors
 *  `renderTerminalChatSettings` below but writes into the passed `draft` object
 *  instead of saving per-field (no agent exists yet) — applied in one PUT after the
 *  agent is created (view.js _acceptEngineCreate). */
export function renderTerminalChatCreateBody(body, draft) {
  draft = draft || {};
  if (draft.command == null) draft.command = 'claude';

  const intro = document.createElement('div');
  intro.className = 'claude-agent-intro';
  intro.textContent = 'Answered by a live terminal program on this machine — your messages go to the '
    + "program's stdin and its output streams back as a live terminal view. Set the command, then "
    + 'press + to create it. Admin only.';
  body.appendChild(intro);

  _field(body, {
    label: 'Command',
    value: draft.command,
    placeholder: 'claude',
    hint: 'The terminal program to run (e.g. claude, bash, python). Defaults to claude.',
    onInput: (v) => { draft.command = v; },
  });
  _field(body, {
    label: 'Working directory',
    value: draft.cwd || '',
    placeholder: 'C:\\path\\to\\project',
    hint: "Where the program starts. Leave blank for the app's project folder.",
    onInput: (v) => { draft.cwd = v.trim(); },
  });

  return () => ({
    terminal_chat: {
      command: (draft.command || 'claude').trim() || 'claude',
      cwd: (draft.cwd || '').trim(),
    },
  });
}

// ── Card tabs ──────────────────────────────────────────────────────────────────

/** Populate the Terminal Chat card's tab bar and wire it to swap `body`. */
export function mountTerminalChatCardTabs(tabBar, body, agent) {
  if (!tabBar || !body) return;
  let active = 'sessions';

  function show(tab) {
    active = tab;
    tabBar.querySelectorAll('.agents-detail-tab').forEach(b =>
      b.classList.toggle('active', b.dataset.tab === tab));
    setSessionsAgentContext(null);
    body.innerHTML = '';
    if (tab === 'sessions') setSessionsAgentContext(agent.id, body, { agentName: agent.name || 'Terminal' });
    else if (tab === 'settings') renderTerminalChatSettings(body, agent);
  }

  tabBar.innerHTML = '';
  [['sessions', 'Sessions'], ['settings', 'Config']].forEach(([key, label]) => {
    const b = document.createElement('button');
    b.type = 'button'; b.className = 'agents-detail-tab'; b.dataset.tab = key; b.textContent = label;
    b.addEventListener('click', (e) => { e.stopPropagation(); if (active !== key) show(key); });
    tabBar.appendChild(b);
  });

  show('sessions');
}

// ── Settings body ──────────────────────────────────────────────────────────────

/** Render the Terminal Chat settings into `body` (the open card's detail body):
 *  the terminal_chat knobs + a delete — and nothing the normal agent panel has. */
export function renderTerminalChatSettings(body, agent) {
  renderAgentIdentitySettings(body, agent);
  const tc = (agent.terminal_chat && typeof agent.terminal_chat === 'object') ? agent.terminal_chat : {};

  const intro = document.createElement('div');
  intro.className = 'claude-agent-intro';
  intro.textContent = 'Answered by a live terminal program on this machine — your messages go to '
    + 'the program\'s stdin and its output streams back as a live terminal view. Admin only.';
  body.appendChild(intro);

  // Patch the local cache after a successful silent save.
  const _save = (patch) =>
    _putAgentField(agent, { terminal_chat: patch }, null, { silent: true })
      .then(ok => { if (ok) agent.terminal_chat = { ...(agent.terminal_chat || {}), ...patch }; return ok; });

  _field(body, {
    label: 'Command',
    value: tc.command || 'claude',
    placeholder: 'claude',
    hint: 'The terminal program to run (e.g. claude, bash, python). Defaults to claude.',
    onSave: (val) => _save({ command: val.trim() || 'claude' }),
  });

  _field(body, {
    label: 'Working directory',
    value: tc.cwd || '',
    placeholder: 'C:\\path\\to\\project',
    hint: 'Where the program starts. Leave blank for the app\'s project folder.',
    onSave: (val) => _save({ cwd: val.trim() }),
  });

  // Delete — two-tap confirm.
  const danger = document.createElement('div');
  danger.className = 'claude-agent-danger';
  const delBtn = document.createElement('button');
  delBtn.type = 'button';
  delBtn.className = 'claude-agent-delete-btn';
  const REST = icon('trash-2', { size: '14px' }) + ' Delete this Terminal Chat agent';
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
    await _deleteTerminalChatAgent(agent);
  });
  danger.appendChild(delBtn);
  body.appendChild(danger);
}

/** Soft-delete (to the recycling bin), then clear the open card and refresh the grid. */
async function _deleteTerminalChatAgent(agent) {
  try {
    await fetch(`/api/v1/agents/${agent.id}?user_id=${encodeURIComponent(app.currentUserId)}`,
      { method: 'DELETE', headers: { ...authHeaders() } });
  } catch (e) {
    console.warn('agents: delete Terminal Chat agent failed', e);
    alert('Error deleting agent: ' + e.message);
    return;
  }
  try {
    if (typeof app.populateAgentSelect === 'function') await app.populateAgentSelect(app.currentUserId);
  } catch (_) { /* non-fatal */ }
  _clearExpanded();
  if (typeof window.__agentsReload === 'function') window.__agentsReload();
}

// ── Small self-contained field builder ─────────────────────────────────────────
// Deliberately local — the Terminal Chat card shares none of the normal Config
// tab's layout code. Reuses the shared `.ac-*` classes + the on-top save overlay
// so the look matches without the coupling.

// onSave → debounced save with the on-top green-tick overlay (the live settings
// card). onInput → plain capture into a draft, no overlay (the create form).
function _field(parent, { label, value, placeholder, hint, onSave, onInput }) {
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
